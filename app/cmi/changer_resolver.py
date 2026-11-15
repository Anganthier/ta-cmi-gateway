"""Changer resolver: display address -> change address + option list.

When the CMI schematic shows a settable value, the visible text is bound to a
*display address* (``loadChanger('ADDR')`` or ``class="changex2"
adresse="ADDR"``).  To actually write it later (Sprint S3), the display
address must be translated into a *change address* and its option list
fetched from the read-only endpoints:

* ``GET /INCLUDE/changer.cgi?sadr=<displayAdr>``
* ``GET /INCLUDE/changerx2.cgi?sadrx2=<displayAdr>``

The response is a small HTML fragment with one hidden input (the change
address) and one ``<select>`` with the allowed options (or, for numeric/date/
time parameters, the corresponding inputs).

Results are cached in-memory with a TTL (``CMI_CHANGER_TTL_SEC``, default
24 h) and optionally on disk under ``data/changer_cache/`` so the cache
survives restarts.  This module only ever calls the two read-only changer
endpoints -- never ``change.cgi``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.cmi.client import CMIClient
from app.cmi.exceptions import CMIChangerError
from app.cmi.queue import CMIGuard

logger = logging.getLogger("cmi.changer")

_HIDDEN_RE = re.compile(r'<input[^>]*type="hidden"[^>]*>', re.IGNORECASE)
_ATTR_RE = re.compile(r'([a-zA-Z-]+)\s*=\s*"([^"]*)"')
_OPTION_RE = re.compile(r'<option([^>]*)>\s*(.*?)\s*</option>', re.IGNORECASE | re.DOTALL)
_SELECT_RE = re.compile(r"<select([^>]*)>.*?</select>", re.IGNORECASE | re.DOTALL)

#: Default in-memory TTL: 24 h.
DEFAULT_TTL_SEC = 86400.0


class ChangerOption(BaseModel):
    """One selectable option of a changer dialog."""

    value: str
    label: str
    selected: bool = False


class ChangerInfo(BaseModel):
    """Resolved changer data for one display address."""

    display_address: str
    x2: bool
    change_address: str | None = None
    option_type: str = "select"  # select | numeric | date | time | bit
    options: list[ChangerOption] = Field(default_factory=list)
    current_value: str | None = None
    min_value: str | None = None
    max_value: str | None = None
    resolved_at: float = 0.0
    source: str = "cmi"  # cmi | cache | in-memory

    @property
    def settable_values(self) -> list[str]:
        """The list of writable values (option values or [min,max] bounds)."""
        if self.option_type == "select":
            return [o.value for o in self.options]
        if self.option_type == "bit":
            return ["0", "1"]
        return []


def parse_changer_html(html: str, *, display_address: str, x2: bool) -> ChangerInfo:
    """Parse a changer/changerx2 CGI response into :class:`ChangerInfo`.

    Raises:
        CMIChangerError: when no change address or option type is found.
    """
    info = ChangerInfo(display_address=display_address, x2=x2)

    # 1) change address from the hidden input (id="changeadr")
    for match in _HIDDEN_RE.finditer(html):
        tag = match.group(0)
        attrs = dict(_ATTR_RE.findall(tag))
        if attrs.get("id") == "changeadr" and "value" in attrs:
            info.change_address = attrs["value"]
            break
    if info.change_address is None:
        raise CMIChangerError(f"no hidden changeadr input in changer response for {display_address}",
                              address=display_address)

    select_match = _SELECT_RE.search(html)
    if select_match:
        info.option_type = "select"
        for opt in _OPTION_RE.finditer(select_match.group(0)):
            attrs = dict(_ATTR_RE.findall(opt.group(1)))
            value = attrs.get("value", "")
            label = opt.group(2).strip()
            selected = "selected" in attrs
            info.options.append(ChangerOption(value=value, label=label, selected=selected))
            if selected:
                info.current_value = value
        return info

    # Numeric / date / time: look at the inputs of the dialog.
    inputs = re.findall(r'<input[^>]*>', html, flags=re.IGNORECASE)
    ids = [dict(_ATTR_RE.findall(i)).get("id", "") for i in inputs]
    if any("date" in i for i in ids) or "changetodatey" in ids:
        info.option_type = "date"
    elif any("time" in i for i in ids) or "changetotimeh" in ids:
        info.option_type = "time"
    elif any(i.startswith("changeto") and "time" not in i and "date" not in i for i in ids):
        info.option_type = "numeric"
    else:
        # Fallback: a select-less numeric field (min/max attributes).
        info.option_type = "numeric"

    min_match = re.search(r'min\s*=\s*"(-?\d+(?:\.\d+)?)"', html)
    max_match = re.search(r'max\s*=\s*"(-?\d+(?:\.\d+)?)\"', html)
    if min_match:
        info.min_value = min_match.group(1)
    if max_match:
        info.max_value = max_match.group(1)

    if not inputs and info.option_type == "numeric":
        raise CMIChangerError(
            f"changer response for {display_address} has no parsable option type",
            address=display_address,
        )
    return info


class ChangerResolver:
    """Resolves display addresses with in-memory + optional file cache."""

    def __init__(
        self,
        client: CMIClient,
        guard: CMIGuard,
        *,
        ttl_sec: float = DEFAULT_TTL_SEC,
        cache_dir: Path | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self._client = client
        self._guard = guard
        self._ttl = ttl_sec
        self._cache_dir = cache_dir
        self._clock = clock
        self._mem: dict[str, ChangerInfo] = {}
        self._in_flight: dict[str, Any] = {}  # address -> asyncio.Task (dedup)
        if self._cache_dir is not None:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    # -- public API -----------------------------------------------------------
    async def resolve(self, display_address: str, *, x2: bool = False, refresh: bool = False) -> ChangerInfo:
        """Return changer info for a display address (cached).

        Concurrent calls for the same address share one in-flight request.
        """
        key = f"{'x2:' if x2 else ''}{display_address}"
        cached = self._mem.get(key)
        if cached and not refresh and (self._clock() - cached.resolved_at) < self._ttl:
            return cached

        task = self._in_flight.get(key)
        if task is not None:
            return await task

        task = _create_task(self._fetch(display_address, x2=x2, key=key))
        self._in_flight[key] = task
        try:
            return await task
        finally:
            self._in_flight.pop(key, None)

    async def resolve_many(self, addresses: dict[str, bool]) -> dict[str, ChangerInfo]:
        """Resolve a set of display addresses sequentially (serial queue).

        Args:
            addresses: mapping display_address -> is_x2.
        """
        results: dict[str, ChangerInfo] = {}
        for address, x2 in addresses.items():
            try:
                results[address] = await self.resolve(address, x2=x2)
            except CMIChangerError:
                logger.exception("Failed to resolve changer %s (x2=%s)", address, x2)
        return results

    # -- internals ------------------------------------------------------------
    async def _fetch(self, display_address: str, *, x2: bool, key: str) -> ChangerInfo:
        # File cache first (survives restarts).
        file_cached = self._read_file_cache(key)
        if file_cached is not None and (self._clock() - file_cached.resolved_at) < self._ttl:
            self._mem[key] = file_cached
            file_cached.source = "cache"
            return file_cached

        html = await self._guard.run_schematic(
            lambda: self._client.get_changer(display_address, x2=x2)
        )
        info = parse_changer_html(html, display_address=display_address, x2=x2)
        info.resolved_at = self._clock()
        info.source = "cmi"
        self._mem[key] = info
        self._write_file_cache(key, info)
        return info

    def _safe_name(self, key: str) -> str:
        return key.replace(":", "_").replace("/", "_")

    def _read_file_cache(self, key: str) -> ChangerInfo | None:
        if self._cache_dir is None:
            return None
        path = self._cache_dir / f"{self._safe_name(key)}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return ChangerInfo.model_validate(data)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Corrupt changer cache %s: %s", path, exc)
            return None

    def _write_file_cache(self, key: str, info: ChangerInfo) -> None:
        if self._cache_dir is None:
            return
        path = self._cache_dir / f"{self._safe_name(key)}.json"
        try:
            path.write_text(json.dumps(info.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - disk problems
            logger.warning("Cannot write changer cache %s: %s", path, exc)


def _create_task(coro):
    """Create an asyncio task without import-cycle issues at module load."""
    import asyncio

    return asyncio.ensure_future(coro)
