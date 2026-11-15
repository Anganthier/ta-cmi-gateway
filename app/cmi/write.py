"""Isolated CMI write client (write-path capability proof, S3-1 foundation).

This module is the ONLY place in the gateway code that may issue CMI write
requests.  It is deliberately NOT wired into the running gateway app
(``app/main.py`` stays read-only): it is driven from a CLI script
(``scripts/cmi_write_discovery.py``) so that a live write always happens as a
deliberate, logged, single-shot action.

Safety rules (hard, enforced BEFORE any network call):

* the display address must be in the L1 whitelist (the 9 settable addresses
  from ``docs/plant-model.json`` ``settable.elements``);
* the RISK address ``10520A10180`` (Oelbrenner) is NEVER written -- not even
  as a no-op.  It may be resolved and read (inventory), never written;
* the current value must be readable unambiguously (the changer's selected
  option, cross-checked against the live schematic text).  No readable value
  -> no write;
* ``changeto`` must be one of the address's own option values (select) or
  within ``[min, max]`` (numeric).  Nothing else;
* a no-op write re-sends the exact current value.  After the write the value
  is re-read (read-back) and in strict mode any drift raises
  :class:`~app.cmi.exceptions.CMISafetyError`.

Endpoints (all verified against the CMI's own frontend ``discovery/scheme.js``):

* write (GET, not POST)::

      GET /INCLUDE/change.cgi?changeadr=<changeAdr>&changeto=<value>
      GET /INCLUDE/change.cgi?changeadrx2=<changeAdr>&changetox2=<value>  (x2)

* read-back: the same read-only endpoints the gateway already polls --
  schematic value pages (``/schematic_files/N.cgi``) plus the changer
  dialog (``/INCLUDE/changer[x2].cgi``, whose ``selected`` option is the
  CMI's own current value).  :class:`CMIWriteClient` therefore subclasses
  :class:`~app.cmi.client.CMIClient` (inheriting auth, timeouts and the
  single-choke-point allow-list) with its own allow-list of
  ``change.cgi`` + the two read-back changer endpoints; the schematic
  read-back remains allowed by the base class' read-only schematic-page
  rule.  The 1/min JSON-API (``api.cgi``) is NOT part of the write client
  and is not needed for read-back.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final

import httpx
from pydantic import BaseModel, Field

from app.cmi.changer_resolver import ChangerInfo, parse_changer_html
from app.cmi.client import _SCHEMATIC_PATH_RE, CMIClient
from app.cmi.exceptions import CMIConnectionError, CMIGatewayError, CMISafetyError
from app.cmi.queue import CMISerialQueue
from app.cmi.schematic_parser import parse_schematic_html

logger = logging.getLogger("cmi.write")

#: The single write endpoint this module may touch.
WRITE_ENDPOINT: Final[str] = "/INCLUDE/change.cgi"

#: Endpoints the write client's own allow-list permits: the write itself plus
#: the read-back changer endpoints (the schematic value pages are permitted
#: by the base class' read-only schematic rule, see :class:`CMIWriteClient`).
WRITE_ALLOWED_PATHS: Final[frozenset[str]] = frozenset(
    {
        WRITE_ENDPOINT,
        "/INCLUDE/changer.cgi",  # read-back (CMI's own current value)
        "/INCLUDE/changerx2.cgi",  # read-back (x2 devices)
    }
)

#: L1 whitelist -- the 9 settable display addresses (Discovery 2026-10-26/27,
#: ``docs/plant-model.json`` ``settable.elements``).
WRITABLE_ADDRESSES: Final[frozenset[str]] = frozenset(
    {
        "101301D0180",  # Betriebsmodus (7 options)
        "10820A10180",  # Pumpe Heizkreis
        "10120A10180",  # Pumpe Holzofen
        "10620A10180",  # Ladepumpe WW
        "10220A10180",  # WW vom Oel (Ladep.WW.5)
        "10720A10180",  # Zirkulation
        "10520A10180",  # Oelbrenner (RISK -- inventory only, never writable)
        "10920A10180",  # Ventil Heizkreis-Ruecklauf
        "10040912091",  # Solarpumpe Modus (x2)
    }
)

#: RISK address: oil burner.  Inventoryable (read-only), NEVER writable.
RISK_ADDRESSES: Final[frozenset[str]] = frozenset({"10520A10180"})

#: x2 (UVR610 node 32) display addresses -- resolved via ``changerx2.cgi``.
X2_ADDRESSES: Final[frozenset[str]] = frozenset({"10040912091"})

#: First schematic page on which each address is bound (read-back source).
#: Verified against ``discovery/live_{1..5}.cgi`` (2026-10-26).
ADDRESS_PAGES: Final[dict[str, int]] = {
    "101301D0180": 1,
    "10820A10180": 1,
    "10120A10180": 1,
    "10620A10180": 1,
    "10220A10180": 1,
    "10720A10180": 1,
    "10520A10180": 1,
    "10920A10180": 1,
    "10040912091": 1,  # also present on page 4; page 1 is canonical
}

#: Default delay between the change.cgi write and the read-back fetch, in
#: seconds.  The CMI's own frontend issues a full page reload right after the
#: write; one second leaves headroom for the CGI to settle.
DEFAULT_READBACK_DELAY_SEC: Final[float] = 1.0

#: Retry delay for the single write-attempt retry (transport-level errors only,
#: e.g. a collision with the running gateway poller).
_RETRY_DELAY_SEC: Final[float] = 2.0

_NUMBER_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class Readback(BaseModel):
    """The live state of one settable display address (before/after a write).

    Sources:

    * ``current_value`` / ``current_label`` -- the CMI's own changer dialog
      (the ``selected`` option), i.e. the value the CMI would send back if the
      user clicked "ok" without changing anything.  This is the canonical
      current value.
    * ``schematic_text`` -- the raw ``posN`` text on the live schematic page
      (secondary evidence; for switches it shows mode+output state, e.g.
      ``AUTO/EIN``).
    """

    display_address: str
    x2: bool
    change_address: str
    option_type: str
    option_values: list[str] = Field(default_factory=list)
    option_labels: list[str] = Field(default_factory=list)
    min_value: str | None = None
    max_value: str | None = None
    current_value: str | None = None
    current_label: str | None = None
    schematic_text: str | None = None
    consistent: bool = False
    read_at: str = ""


class NoOpResult(BaseModel):
    """Result of one no-op write (current value written back to itself)."""

    display_address: str
    x2: bool
    change_address: str
    request: str
    before: Readback
    changeto: str
    response_body: str
    after: Readback
    value_changed: bool
    label_changed: bool
    noop_ok: bool
    duration_sec: float = 0.0


class WriteResult(BaseModel):
    """Result of a value write (S3-1 foundation; strict read-back verified)."""

    display_address: str
    x2: bool
    change_address: str
    request: str
    requested_value: str
    response_body: str
    after: Readback
    applied: bool
    duration_sec: float = 0.0


class AddressPresence(BaseModel):
    """Where a settable display address appears on the schematic pages."""

    address: str
    x2: bool
    pages: list[int] = Field(default_factory=list)
    texts: dict[str, str] = Field(default_factory=dict)  # str(page) -> pos text


class SettableEntry(BaseModel):
    """One row of the write-path inventory (read-only discovery result)."""

    address: str
    x2: bool
    risk: bool
    label: str | None = None
    pages: list[int] = Field(default_factory=list)
    schematic_text: str | None = None
    change_address: str | None = None
    option_type: str | None = None
    option_values: list[str] = Field(default_factory=list)
    option_labels: list[str] = Field(default_factory=list)
    min_value: str | None = None
    max_value: str | None = None
    current_value: str | None = None
    current_label: str | None = None
    consistent: bool = False
    writeable: bool = False
    note: str | None = None


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable, no I/O)
# ---------------------------------------------------------------------------
def collect_settable_addresses(page_htmls: Mapping[int, str]) -> dict[str, AddressPresence]:
    """Collect every settable display address from schematic page HTML.

    Args:
        page_htmls: mapping schematic page number -> value-page HTML
            (``/schematic_files/<page>.cgi`` body).

    Returns:
        Display address -> :class:`AddressPresence` (all pages it appears on
        plus the raw ``posN`` text per page).
    """
    found: dict[str, AddressPresence] = {}
    for page in sorted(page_htmls):
        parsed = parse_schematic_html(page_htmls[page], page)
        for pos in parsed.positions:
            if pos.address is None:
                continue
            presence = found.get(pos.address)
            if presence is None:
                found[pos.address] = AddressPresence(
                    address=pos.address,
                    x2=pos.x2,
                    pages=[page],
                    texts={str(page): pos.text},
                )
            else:
                if page not in presence.pages:
                    presence.pages.append(page)
                presence.texts[str(page)] = pos.text
    return found


def _parse_numeric_token(token: str) -> float | None:
    try:
        return float(token.replace(",", "."))
    except ValueError:
        return None


def _extract_current(
    info: ChangerInfo, schematic_text: str | None
) -> tuple[str | None, str | None, bool]:
    """Derive (current_value, current_label, consistent) for one address.

    The changer dialog's ``selected`` option is canonical.  When the dialog
    marks no option (defensive), the live schematic text is matched against
    the option labels (exact match required, and it must be unique).

    Returns ``value=None`` when the current value cannot be read
    unambiguously -- callers must refuse to write in that case.
    """
    text = schematic_text.strip() if schematic_text is not None else None

    if info.option_type == "select":
        selected = [o for o in info.options if o.selected and o.value]
        if len(selected) == 1:
            value, label = selected[0].value, selected[0].label or None
        elif text is not None:
            matches = [o for o in info.options if o.value and o.label == text]
            if len(matches) == 1:
                value, label = matches[0].value, matches[0].label or None
            else:
                value, label = None, None
        else:
            value, label = None, None
        # "AUTO" in "AUTO/EIN", "NORMAL" in "NORMAL", "Auto" in "Auto", ...
        consistent = value is not None and text is not None and label is not None and label in text
        return value, label, consistent

    if info.option_type == "bit":
        if text in ("0", "1"):
            return text, text, True
        return None, None, False

    if info.option_type == "numeric":
        if text is None:
            return None, None, False
        match = _NUMBER_RE.search(text)
        if match is None:
            return None, None, False
        value = match.group(0)
        number = _parse_numeric_token(value)
        if number is None:
            return None, None, False
        lo = _parse_numeric_token(info.min_value) if info.min_value else None
        hi = _parse_numeric_token(info.max_value) if info.max_value else None
        in_range = (lo is None or number >= lo) and (hi is None or number <= hi)
        return value, None, in_range

    # date/time/unknown: not readable as a simple settable value here.
    return None, None, False


def build_inventory_entry(
    presence: AddressPresence,
    readback: Readback | None,
    *,
    label: str | None = None,
    risk_addresses: frozenset[str] = RISK_ADDRESSES,
    whitelist: frozenset[str] = WRITABLE_ADDRESSES,
) -> SettableEntry:
    """Assemble one inventory row from live presence + read-back data (pure)."""
    address = presence.address
    risk = address in risk_addresses
    readback_present = bool(readback is not None and readback.change_address.strip())
    supported_type = readback is not None and readback.option_type in {"select", "numeric", "bit"}
    readable = readback is not None and readback.current_value is not None
    writeable = (
        not risk and address in whitelist and readback_present and supported_type and readable
    )
    note = "RISK: Oelbrenner -- nur inventarisieren, NIE schreiben" if risk else None
    if not risk and address not in whitelist:
        note = "Address nicht in der L1-Whitelist"
    elif not risk and not readback_present:
        note = "changeadr nicht aufloesbar (nur inventariert)"
    elif not risk and writeable is False and readback_present:
        note = "aktueller Wert nicht eindeutig lesbar"
    return SettableEntry(
        address=address,
        x2=presence.x2,
        risk=risk,
        label=label,
        pages=list(presence.pages),
        schematic_text=readback.schematic_text if readback else None,
        change_address=readback.change_address if readback else None,
        option_type=readback.option_type if readback else None,
        option_values=list(readback.option_values) if readback else [],
        option_labels=list(readback.option_labels) if readback else [],
        min_value=readback.min_value if readback else None,
        max_value=readback.max_value if readback else None,
        current_value=readback.current_value if readback else None,
        current_label=readback.current_label if readback else None,
        consistent=readback.consistent if readback else False,
        writeable=writeable,
        note=note,
    )


def format_options(readback: Readback) -> str:
    """Compact option/min-max rendering for the inventory table."""
    if readback.option_type == "numeric":
        return f"[{readback.min_value or '?'}, {readback.max_value or '?'}]"
    return (
        ", ".join(
            f"{v}={lbl}" if lbl else v
            for v, lbl in zip(readback.option_values, readback.option_labels, strict=False)
        )
        or "?"
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class CMIWriteClient(CMIClient):
    """Write-capable CMI client, isolated from the read-only gateway.

    Subclasses :class:`CMIClient` (inheriting auth, timeouts, error mapping
    and the single choke-point allow-list) with its own allow-list:

    * :data:`WRITE_ENDPOINT` (``change.cgi``) -- the only write call;
    * the read-back changer endpoints (``changer.cgi``/``changerx2.cgi``);
    * schematic value pages -- permitted by the base class' read-only
      schematic-page rule.  Everything else (JSON-API, /LOG, ...) is
      rejected by :meth:`CMIClient._assert_allowed`.

    All requests are funneled through a shared serial queue, so at most one
    CMI request is in flight at a time (the CMI tolerates only a single
    outstanding request -- the running gateway's poller can therefore never
    be interleaved with a write).
    """

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        queue: CMISerialQueue | None = None,
        strict: bool = True,
        readback_delay_sec: float = DEFAULT_READBACK_DELAY_SEC,
        timeout_sec: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            user=user,
            password=password,
            allowed_paths=WRITE_ALLOWED_PATHS,
            timeout_sec=timeout_sec,
            transport=transport,
        )
        self._queue = queue if queue is not None else CMISerialQueue()
        self._strict = strict
        self._readback_delay_sec = readback_delay_sec

    # -- lifecycle ------------------------------------------------------------
    async def aclose(self) -> None:
        await super().aclose()

    async def __aenter__(self) -> CMIWriteClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- allow-list ------------------------------------------------------------
    def _assert_allowed(self, path: str) -> None:
        """Strict write-client allow-list (tighter than the read-only client).

        Permitted: :data:`WRITE_ALLOWED_PATHS` (``change.cgi`` + the two
        read-back changer endpoints) and the base class' read-only schematic
        value pages (read-back).  Everything else -- JSON-API, ``/LOG/*``,
        config endpoints -- is rejected before a connection is opened.
        """
        path = path.split("?", 1)[0]
        if path in self._allowed_paths or _SCHEMATIC_PATH_RE.match(path):
            return
        raise PermissionError(f"endpoint not allowed for the CMI write client: {path!r}")

    # -- internals ------------------------------------------------------------
    async def _run(self, func):
        return await self._queue.run(func, timeout=30.0)

    @staticmethod
    def _assert_whitelisted(display_address: str) -> None:
        if display_address not in WRITABLE_ADDRESSES:
            raise CMISafetyError(
                f"display address {display_address!r} is not in the L1 write whitelist",
                address=display_address,
            )

    @staticmethod
    def _assert_write_target(display_address: str) -> None:
        CMIWriteClient._assert_whitelisted(display_address)
        if display_address in RISK_ADDRESSES:
            raise CMISafetyError(
                f"display address {display_address!r} is RISK (Oelbrenner) and can never be written",
                address=display_address,
            )

    @staticmethod
    def _is_x2(display_address: str) -> bool:
        return display_address in X2_ADDRESSES

    @staticmethod
    def _assert_value_allowed(info: ChangerInfo, value: str) -> None:
        """Reject any ``changeto`` that is not a legal value of this address."""
        if not isinstance(value, str) or not value:
            raise CMISafetyError(
                f"changeto must be a non-empty string, got {value!r}",
                address=info.display_address,
            )
        if info.option_type == "select":
            if value not in {o.value for o in info.options}:
                raise CMISafetyError(
                    f"changeto {value!r} is not one of the allowed options "
                    f"{[o.value for o in info.options]} for {info.display_address!r}",
                    address=info.display_address,
                )
        elif info.option_type == "bit":
            if value not in ("0", "1"):
                raise CMISafetyError(
                    f"changeto {value!r} is not a valid bit value for {info.display_address!r}",
                    address=info.display_address,
                )
        elif info.option_type == "numeric":
            number = _parse_numeric_token(value)
            if number is None:
                raise CMISafetyError(
                    f"changeto {value!r} is not a number for {info.display_address!r}",
                    address=info.display_address,
                )
            lo = _parse_numeric_token(info.min_value) if info.min_value else None
            hi = _parse_numeric_token(info.max_value) if info.max_value else None
            if (lo is not None and number < lo) or (hi is not None and number > hi):
                raise CMISafetyError(
                    f"changeto {value!r} outside [{info.min_value}, {info.max_value}] "
                    f"for {info.display_address!r}",
                    address=info.display_address,
                )
        else:
            raise CMISafetyError(
                f"unsupported option type {info.option_type!r} for {info.display_address!r}",
                address=info.display_address,
            )

    async def resolve(self, display_address: str) -> ChangerInfo:
        """Resolve a display address (whitelist-gated, read-only changer CGI).

        RISK addresses may be resolved (inventory) -- only writes are blocked.
        """
        self._assert_whitelisted(display_address)
        x2 = self._is_x2(display_address)
        html = await self._run(lambda: self.get_changer(display_address, x2=x2))
        return parse_changer_html(html, display_address=display_address, x2=x2)

    async def _schematic_text(self, display_address: str) -> str | None:
        """The live ``posN`` text bound to the address on its schematic page."""
        page = ADDRESS_PAGES.get(display_address)
        if page is None:
            return None
        html = await self._run(lambda: self.get_schematic(page))
        parsed = parse_schematic_html(html, page)
        for pos in parsed.positions:
            if pos.address == display_address:
                return pos.text
        return None

    async def read_current(self, display_address: str) -> Readback:
        """Read the address's current state (changer dialog + live schematic)."""
        info = await self.resolve(display_address)
        text = await self._schematic_text(display_address)
        value, label, consistent = _extract_current(info, text)
        return Readback(
            display_address=display_address,
            x2=info.x2,
            change_address=info.change_address or "",
            option_type=info.option_type,
            option_values=[o.value for o in info.options],
            option_labels=[o.label for o in info.options],
            min_value=info.min_value,
            max_value=info.max_value,
            current_value=value,
            current_label=label,
            schematic_text=text,
            consistent=consistent,
            read_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    def _change_params(self, change_address: str, value: str, *, x2: bool) -> dict[str, str]:
        if x2:
            return {"changeadrx2": change_address, "changetox2": value}
        return {"changeadr": change_address, "changeto": value}

    async def _send_change(self, change_address: str, value: str, *, x2: bool) -> str:
        """Issue the single ``change.cgi`` GET; retry once on transport errors."""
        params = self._change_params(change_address, value, x2=x2)
        last_exc: CMIGatewayError | None = None
        for attempt in (1, 2):
            try:
                return await self._run(lambda: self._get_raw(WRITE_ENDPOINT, params))
            except CMIConnectionError as exc:
                # Transport-level error (e.g. the running gateway poller owns
                # the single CMI slot): retry once, then surface the error.
                last_exc = exc
                if attempt == 1:
                    logger.warning(
                        "write transport error (attempt %d), retrying in %.1f s: %s",
                        attempt,
                        _RETRY_DELAY_SEC,
                        exc,
                    )
                    await asyncio.sleep(_RETRY_DELAY_SEC)
            except CMIGatewayError as exc:
                last_exc = exc
                break
        assert last_exc is not None
        raise last_exc

    # -- public write API -------------------------------------------------------
    async def write(self, display_address: str, value: str) -> WriteResult:
        """Write ``value`` to a whitelisted address and verify via read-back.

        Raises:
            CMISafetyError: RISK address, non-whitelisted address, value
                outside the allowed option list / numeric range, or (strict)
                the read-back does not show the requested value.
        """
        started = _now()
        self._assert_write_target(display_address)
        info = await self.resolve(display_address)
        self._assert_value_allowed(info, value)
        x2 = info.x2
        body = await self._send_change(info.change_address or "", value, x2=x2)
        await asyncio.sleep(self._readback_delay_sec)
        after = await self.read_current(display_address)
        applied = after.current_value is not None and after.current_value == value
        if self._strict and not applied:
            raise CMISafetyError(
                f"write not applied / read-back mismatch on {display_address!r}: "
                f"expected {value!r}, read back {after.current_value!r}",
                address=display_address,
            )
        return WriteResult(
            display_address=display_address,
            x2=x2,
            change_address=info.change_address or "",
            request=_request_url(
                params=self._change_params(info.change_address or "", value, x2=x2)
            ),
            requested_value=value,
            response_body=body[:500],
            after=after,
            applied=applied,
            duration_sec=_elapsed(started),
        )

    async def noop_write(self, display_address: str) -> NoOpResult:
        """Write the address's exact current value back to itself.

        This is the state-preserving capability proof for the full
        ``change.cgi`` path: auth, changeadr resolution, the write call itself
        and the read-back -- without changing any plant state.

        Raises:
            CMISafetyError: RISK address, non-whitelisted address, current
                value not unambiguously readable (no write is performed), or
                (strict) the read-back shows a different value.
        """
        started = _now()
        self._assert_write_target(display_address)
        before = await self.read_current(display_address)
        if before.current_value is None:
            raise CMISafetyError(
                f"current value of {display_address!r} is not unambiguously readable "
                f"(schematic text: {before.schematic_text!r}); no write performed",
                address=display_address,
            )
        x2 = before.x2
        body = await self._send_change(before.change_address, before.current_value, x2=x2)
        await asyncio.sleep(self._readback_delay_sec)
        after = await self.read_current(display_address)
        if after.current_value is None:
            raise CMISafetyError(
                f"read-back after no-op write on {display_address!r} is not readable; "
                f"state unclear (schematic text: {after.schematic_text!r})",
                address=display_address,
            )
        value_changed = after.current_value != before.current_value
        if self._strict and value_changed:
            raise CMISafetyError(
                f"no-op write caused state drift on {display_address!r}: "
                f"{before.current_value!r} -> {after.current_value!r}",
                address=display_address,
            )
        label_changed = (after.schematic_text or "") != (before.schematic_text or "")
        return NoOpResult(
            display_address=display_address,
            x2=x2,
            change_address=before.change_address,
            request=_request_url(
                params=self._change_params(before.change_address, before.current_value, x2=x2)
            ),
            before=before,
            changeto=before.current_value,
            response_body=body[:500],
            after=after,
            value_changed=value_changed,
            label_changed=label_changed,
            noop_ok=not value_changed,
            duration_sec=_elapsed(started),
        )


def _now() -> float:
    return time.monotonic()


def _elapsed(started: float) -> float:
    return round(time.monotonic() - started, 3)


def _request_url(*, params: dict[str, str]) -> str:
    """Human-readable request line for reports (no credentials involved)."""
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"GET {WRITE_ENDPOINT}?{query}"


__all__ = [
    "ADDRESS_PAGES",
    "AddressPresence",
    "CMIWriteClient",
    "DEFAULT_READBACK_DELAY_SEC",
    "NoOpResult",
    "RISK_ADDRESSES",
    "Readback",
    "SettableEntry",
    "WRITE_ALLOWED_PATHS",
    "WRITE_ENDPOINT",
    "WRITABLE_ADDRESSES",
    "X2_ADDRESSES",
    "WriteResult",
    "build_inventory_entry",
    "collect_settable_addresses",
    "format_options",
]
