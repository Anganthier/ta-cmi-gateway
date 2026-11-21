"""Minderer-Sollspannungs-Write-Pfad (v0.6-A', Phase-1-Baustein) + Semi-Auto-CLI.

The Minderer fix value (Sollspannung a17/a18/a19, UVR610 node 32, schematic
page 6) becomes settable for the first time through the CMI ``change.cgi``
x2 path (``changeadrx2``/``changetox2``), built on
:class:`~app.cmi.write.CMIWriteClient` -- the ONLY place in the gateway that
may issue CMI writes.  Autonomous writing happens later (Phase 2, separate
task with its own loop + envelope); this module is the **manual**
semi-automatic path: maintainer confirms every write at the terminal
(y/N per write, 1/h rate limit, audit JSONL).

Zone mapping (DOKUMENTIERTE ANNAHME, v0.5 engine-rework 2026-11-12, maintainer;
Quellen: ``analysis/model/l2/config.py`` ``RM_RAW_MINDERER_SOLL`` +
Kommentar Z. 253-264, ``app/cmi/cmi_log.py`` ``_CHANNEL_MAP_181``
(json:32 ch17/18/19), Live-JSON a17-a19 konstant 10,0 V seit
UVR-Programm-Upload 2026-11-07):

* ``kell`` -> ``10044142091``  (Sollspannung 1 / a17, rohe Adresse ``2000004414D10D``)
* ``eg``   -> ``10144142091``  (Sollspannung 2 / a18, rohe Adresse ``2000014414D10D``, live bestaetigt)
* ``dg``   -> ``10244142091``  (Sollspannung 3 / a19, rohe Adresse ``2000024414D10D``)

Die harte Verifikation des Mappings = der **No-op-Test auf dem Pi**
(wertneutral: aktueller Wert zurueckgeschrieben, Strict-Drift-Check).

Volt semantics (Discovery 18.11, realer CMI-Dialog EG): the CMI dialog is
``option_type=numeric`` with ``min="2.00" max="10.00" step="0.01"`` and the
live value in the ``value`` attribute of the ``<input type="number">``
(2-decimal formatting, e.g. ``value="2.00"``).  All volt values are therefore
normalized to 2 decimals (``"2.5" -> "2.50"``) before they go out to
``change.cgi`` -- the normalized form is BOTH the sent form and the
read-back comparison form (the write client compares strings).

Role gate (maintainer-Forderung 18.11, kanonisiert 9aaeb00): ``CMI_ROLE``
(default ``dev``) forbids every REAL value write in this process --
enforced centrally in :func:`app.cmi.write.assert_role_allows_real_write`
BEFORE any HTTP.  The value-neutral no-op stays allowed in dev
(maintainer-approved one-time local path test).  ``prod`` runs only on the Pi
(``deploy/compose/compose-raspi.yml`` ``CMI_ROLE: prod``; the Pi ``.env`` is
a 1:1 mirror of the local one and must NOT contain it).

Safety extras (this module): 1/h rate limit for real value changes per
address (last audit line), interactive y/N confirm (Enter/other = N = abort;
no TTY = abort, NO ``--force`` in Phase 1), and an audit line per attempt
in the SAME JSONL as S3-1 (``settings.audit_jsonl_path``).

CLI::

    python -m app.cmi.minderer status <zone>   # read-only read-back
    python -m app.cmi.minderer noop <zone>     # value-neutral no-op (dev ok)
    python -m app.cmi.minderer write <zone> <volt>  # real write, y/N confirm

Exit codes: 0 ok/aborted-by-N, 1 error (CMISafetyError etc.),
2 interactive confirm required (no TTY).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from app.cmi.exceptions import CMIGatewayError, CMISafetyError
from app.cmi.write import (
    CMI_ROLE_PROD,
    DEFAULT_READBACK_DELAY_SEC,
    CMIWriteClient,
    NoOpResult,
    Readback,
    WriteResult,
    normalize_role,
)
from app.config import Settings, get_settings

logger = logging.getLogger("cmi.minderer")


class MindererAbortError(CMIGatewayError):
    """Der Nutzer hat den Minderer-Write per Confirm=N abgebrochen.

    Kein Sicherheitsvorfall (kein Audit, kein Write); die CLI mappt ihn auf
    exit 0 (sauberer Abbruch), alle anderen CMISafetyError-Quellen (Role-Gate,
    Rate-Limit, Range-Guard, Read-back-Drift) bleiben exit 1.
    """

#: Minderer zone -> CMI display address (page 6, UVR610 node 32, x2).
#: Mapping-Annahme + Quellen: Modul-Docstring (a17/a18/a19); harte
#: Verifikation = No-op-Test auf dem Pi.
MINDERER_ZONES: Mapping[str, str] = {
    "kell": "10044142091",  # Sollspannung 1 (a17), rohe 2000004414D10D
    "eg": "10144142091",  # Sollspannung 2 (a18), rohe 2000014414D10D
    "dg": "10244142091",  # Sollspannung 3 (a19), rohe 2000024414D10D
}

#: Native CMI range of the numeric dialog (``min="2.00" max="10.00"``);
#: redundant guard in front of the CMI's own min/max enforcement.
VOLT_MIN: Final[float] = 2.0
VOLT_MAX: Final[float] = 10.0

#: Rate limit for real value changes: max 1 write/hour per address.
#: Value-neutral writes (volt == current, i.e. a no-op via ``write()``)
#: are exempt.
RATE_LIMIT_SEC: Final[float] = 3600.0

#: Audit timestamps use Europe/Berlin (host TZ of the plant; the gateway
#: container sets ``TZ: Europe/Berlin``).
_TZ = ZoneInfo("Europe/Berlin")

#: Interactive confirm answers that count as "yes" (Enter/other = N).
_CONFIRM_YES = frozenset({"y", "yes"})


def address_for_zone(zone: str) -> str:
    """The CMI display address of a Minderer zone.

    Raises:
        ValueError: unknown zone (allowed: ``kell``, ``eg``, ``dg``).
    """
    if zone not in MINDERER_ZONES:
        raise ValueError(
            f"unknown Minderer-Zone {zone!r} (erlaubt: {', '.join(sorted(MINDERER_ZONES))})"
        )
    return MINDERER_ZONES[zone]


def normalize_volt(value: str | float) -> str:
    """Normalize a target volt value to the CMI's 2-decimal form + range guard.

    ``"2.5" -> "2.50"``, ``2 -> "2.00"``, ``"2.500" -> "2.50"`` (comma accepted
    as decimal separator).  The CMI formats numerics with exactly 2 decimals
    (``value="2.00"``) and :meth:`CMIWriteClient.write` compares the read-back
    as strings -- the normalized form is the form sent to ``change.cgi`` AND
    the comparison form for the read-back.

    Raises:
        CMISafetyError: not a number, or outside the native range
            ``[2.00, 10.00]`` V (redundant protection in front of the
            CMI's own dialog min/max).
    """
    raw = str(value).strip().replace(",", ".")
    try:
        volts = float(raw)
    except ValueError:
        raise CMISafetyError(
            f"spannung {value!r} ist keine Zahl (erwartet z. B. 2.5)",
        ) from None
    if not (VOLT_MIN <= volts <= VOLT_MAX):
        raise CMISafetyError(
            f"spannung {value!r} außerhalb des CMI-Bereichs "
            f"[{VOLT_MIN:.2f}, {VOLT_MAX:.2f}] V (Dialog-Min/Max)"
        )
    return f"{volts:.2f}"


def audit_append(entry: Mapping[str, Any], *, path: Path) -> None:
    """Append one Minderer audit line (JSONL) to ``path``.

    Line schema (same JSONL as S3-1, ``settings.audit_jsonl_path``)::

        {"ts": "2026-11-18T09:00:00+01:00" (Europe/Berlin, aware),
         "kind": "minderer_write" | "minderer_noop" | "minderer_verify",
         "zone": "kell" | "eg" | "dg",
         "address": "10044142091" (CMI display address),
         "from": "...", "to": "...",
         "role": "dev" | "prod",
         "source": "cli" | "route" | "loop",
         "ok": true | false,
         "detail": "..." | null,
         "a18": "..." | null,       # nur minder_verify (UVR-Ist, V)
         "delta_s": 123 | null}     # nur minder_verify (Write→verifiziert, s)

    A failed audit write is logged but never raises (the audit must not mask
    the write result, same discipline as S3-1).
    """
    record: dict[str, Any] = {
        "ts": datetime.now(_TZ).isoformat(timespec="seconds"),
        "kind": entry.get("kind"),
        "zone": entry.get("zone"),
        "address": entry.get("address"),
        "from": entry.get("from"),
        "to": entry.get("to"),
        "role": entry.get("role"),
        "source": entry.get("source", "cli"),
        "ok": entry.get("ok"),
        "detail": entry.get("detail"),
        "a18": entry.get("a18"),
        "delta_s": entry.get("delta_s"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.error("Minderer-Audit-Write nach %s fehlgeschlagen: %s", path, exc)


def build_write_client(
    settings: Settings | None = None,
    *,
    transport: Any | None = None,
    readback_delay_sec: float | None = None,
) -> CMIWriteClient:
    """Create a :class:`CMIWriteClient` from settings (minderer path).

    Same settings-driven construction as
    :func:`app.cmi.client.build_client`, but write-capable.  ``transport`` /
    ``readback_delay_sec`` exist for tests (MockTransport, zero delay).
    """
    cfg = settings or get_settings()
    return CMIWriteClient(
        cfg.base_url,
        cfg.cmi_user,
        cfg.cmi_pass,
        timeout_sec=cfg.cmi_timeout_sec,
        transport=transport,
        readback_delay_sec=(
            readback_delay_sec if readback_delay_sec is not None else DEFAULT_READBACK_DELAY_SEC
        ),
        # v0.6-A' role gate: die Rolle aus den DIESER Service gehörenden
        # Settings (CMI_ROLE, Default dev) -- nicht der globale Cache,
        # damit ein injizierter Role die Write-Gate-Entscheidung trifft.
        role=cfg.cmi_role,
    )


class MindererService:
    """Minderer write service: role gate + rate limit + audit + confirm.

    Stateful on purpose (client lifecycle: one httpx client per service;
    the task's readback/write/noop API is realized as
    :meth:`readback` / :meth:`write` / :meth:`noop`).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: CMIWriteClient | None = None,
        audit_path: Path | None = None,
        now_fn: Any | None = None,
        source: str = "cli",
    ) -> None:
        self.settings = settings
        self._client: CMIWriteClient | None = client
        #: Same audit JSONL as S3-1 (``settings.audit_jsonl_path``).
        self.audit_path = Path(audit_path) if audit_path is not None else settings.audit_jsonl_path
        self._now_fn = now_fn or time.time
        self._source = source

    # -- address / role helpers ------------------------------------------------
    def address(self, zone: str) -> str:
        """Display address of ``zone`` (``ValueError`` on unknown zone)."""
        return address_for_zone(zone)

    def role(self) -> str:
        """The effective CMI role for this service (``dev`` default, fail-safe).

        Read from the service's OWN settings (so an injected
        ``Settings(cmi_role="prod")`` shows ``prod`` even if the global
        settings cache still holds the default).
        """
        return normalize_role(self.settings.cmi_role)

    @property
    def client(self) -> CMIWriteClient:
        """Lazy write client (built from settings when not injected)."""
        if self._client is None:
            self._client = build_write_client(self.settings)
        return self._client

    # -- audit -------------------------------------------------------------
    def _audit(self, entry: Mapping[str, Any]) -> None:
        audit_append(entry, path=self.audit_path)

    def _last_write_epoch(self, address: str) -> float | None:
        """Epoch seconds of the LAST ``minderer_write`` audit line for
        ``address`` (success AND failure lines count -- conservative),
        or ``None`` when the audit file has no such line yet."""
        if not self.audit_path.is_file():
            return None
        last: float | None = None
        for line in self.audit_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("kind") != "minderer_write" or record.get("address") != address:
                continue
            try:
                ts = datetime.fromisoformat(str(record["ts"])).timestamp()
            except (KeyError, ValueError):
                continue
            if last is None or ts > last:
                last = ts
        return last

    def _rate_limit_guard(self, address: str, volt_normalized: str, current_value: str | None) -> None:
        """1/h rate limit for REAL value changes (value-neutral writes exempt).

        Raises:
            CMISafetyError: a real write to the same address happened less
                than :data:`RATE_LIMIT_SEC` seconds ago (per audit JSONL).
        """
        if volt_normalized == current_value:
            return  # wertneutral = no-op-äquivalent -> nicht gezählt
        last = self._last_write_epoch(address)
        if last is None:
            return
        elapsed = self._now_fn() - last
        if elapsed < RATE_LIMIT_SEC:
            raise CMISafetyError(
                f"Rate-Limit 1×/h: letzter Minderer-Write an {address!r} vor "
                f"{elapsed:.0f} s (< {int(RATE_LIMIT_SEC)} s); Wert-Change "
                f"{current_value!r} -> {volt_normalized!r} wird erst erlaubt, "
                f"wenn {max(0, int(RATE_LIMIT_SEC - elapsed))} s vergangen sind",
                address=address,
            )

    # -- public API ----------------------------------------------------------
    async def readback(self, zone: str) -> Readback:
        """Read-only read-back of one Minderer zone (value, min/max, address).

        Raises:
            ValueError: unknown zone.
        """
        return await self.client.read_current(self.address(zone))

    async def write(self, zone: str, volt: str | float, *, confirm: bool = True, source: str | None = None) -> WriteResult:
        """Write the target volt to one Minderer zone (role-gated, 1/h limited).

        Steps: normalize -> read-back -> rate-limit guard -> interactive
        confirm (``confirm=True``, TTY required) -> :meth:`CMIWriteClient.write`
        (the role gate runs THERE: in dev this raises ``CMISafetyError``
        before any HTTP and nothing is reported/audited further) -> audit
        line -> :class:`WriteResult`.

        Raises:
            ValueError: unknown zone.
            CMISafetyError: volt out of range, rate limit, confirm refused /
                missing TTY, role gate (dev), write-safety errors of the
                write client.
        """
        address = self.address(zone)
        volt_n = normalize_volt(volt)
        before = await self.readback(zone)
        self._rate_limit_guard(address, volt_n, before.current_value)
        role = self.role()
        if confirm:
            if not sys.stdin.isatty():
                raise MindererAbortError(
                    "interaktiver Confirm erforderlich: Minderer-Write läuft in "
                    "einem TTY mit y/N-Confirm (Phase 1 = Mensch am Terminal, "
                    "kein --force)"
                )
            real = volt_n != before.current_value
            print(f"Rolle:            {role} (prod = Writes frei, dev = kein echter Write)")
            print(f"Zone:             {zone} -> {address} (x2, Seite 6, UVR610)")
            print(f"Aktueller Wert:   {before.current_value!r} V  "
                  f"(Range {before.min_value}-{before.max_value} V)")
            print(f"Zielwert:         {volt_n!r} V  "
                  f"{'echt (!= Ist)' if real else 'wertneutral (= Ist)'}")
            print("Minderer-Write wirklich ausfuehren? [y/N] ", end="", flush=True)
            answer = input()
            if answer.strip().lower() not in _CONFIRM_YES:
                raise MindererAbortError("Minderer-Write abgebrochen (Confirm=N) -- kein Write")
        try:
            result = await self.client.write(address, volt_n)
        except CMISafetyError as exc:
            if role != CMI_ROLE_PROD:
                # Dev role gate: no HTTP happened at all -> no audit line
                # ("Audit-Zeile auch bei Fehlern, FALLS eine angelegt wurde").
                raise
            self._audit(
                {
                    "kind": "minderer_write",
                    "zone": zone,
                    "address": address,
                    "from": before.current_value,
                    "to": volt_n,
                    "role": role,
                    "source": source if source is not None else self._source,
                    "ok": False,
                    "detail": str(exc),
                }
            )
            raise
        self._audit(
            {
                "kind": "minderer_write",
                "zone": zone,
                "address": address,
                "from": before.current_value,
                "to": volt_n,
                "role": role,
                "source": source if source is not None else self._source,
                "ok": True,
                "detail": None,
            }
        )
        return result

    async def noop(self, zone: str, source: str | None = None) -> NoOpResult:
        """Value-neutral no-op write (current value written back to itself).

        Allowed in ``CMI_ROLE=dev`` (maintainer-approved one-time local test of the
        full ``change.cgi`` path; strict drift check stays).  Audited with
        ``kind="minderer_noop"``.

        Raises:
            ValueError: unknown zone.
            CMISafetyError: write-safety errors of the write client.
        """
        address = self.address(zone)
        try:
            result = await self.client.noop_write(address)
        except CMISafetyError as exc:
            # Safety-Rejection / Drift: Audit-Zeile (falls eine angelegt
            # wurde) mit ok=False + Weiterwerfen (CLI -> exit 1).
            self._audit(
                {
                    "kind": "minderer_noop",
                    "zone": zone,
                    "address": address,
                    "from": None,
                    "to": None,
                    "role": self.role(),
                    "source": source if source is not None else self._source,
                    "ok": False,
                    "detail": str(exc),
                }
            )
            raise
        self._audit(
            {
                "kind": "minderer_noop",
                "zone": zone,
                "address": address,
                "from": result.before.current_value,
                "to": result.changeto,
                "role": self.role(),
                "source": source if source is not None else self._source,
                "ok": result.noop_ok,
                "detail": None
                if result.noop_ok
                else f"Drift nach No-Op: {result.before.current_value!r} -> {result.after.current_value!r}",
            }
        )
        return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cmi.minderer",
        description=(
            "Minderer-Sollspannung (UVR610, Seite 6): read-back, No-op-Write, "
            "echter Write mit y/N-Confirm (v0.6-A'). Rolle aus CMI_ROLE "
            "(dev=Default: kein echter Write)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("status", "read-only Read-back (Wert, min/max, Adressen, Rolle)"),
        ("noop", "wertneutraler No-op-Write (in dev erlaubt)"),
        ("write", "echter Sollspannungs-Write (y/N-Confirm, Rate-Limit 1×/h)"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("zone", help="kell | eg | dg")
        if name == "write":
            p.add_argument("volt", help="Ziel-Spannung in V (2.00-10.00, z. B. 2.5)")
    return parser


def _fail(message: str) -> int:
    print(f"FEHLER: {message}", file=sys.stderr)
    return 1


async def _run_status(service: MindererService, zone: str) -> int:
    before = await service.readback(zone)
    print(f"Zone:             {zone} -> {before.display_address} "
          f"(x2={before.x2}, Seite 6, UVR610)")
    print(f"Roh-Write-Adresse: {before.change_address!r} (changeadrx2)")
    print(f"Optionstyp:       {before.option_type} "
          f"(Range {before.min_value}-{before.max_value} V)")
    print(f"Aktueller Wert:   {before.current_value!r} V "
          f"(Read-back, consistent={before.consistent})")
    print(f"Rolle:            {service.role()} "
          f"(prod = Writes frei, dev = kein echter Write)")
    return 0


async def _run_noop(service: MindererService, zone: str) -> int:
    print(f"No-op-Write {zone} -> {service.address(zone)} (wertneutral, Strict-Drift-Check)")
    result = await service.noop(zone)
    print(
        f"OK: No-op bestaetigt -- {result.changeto!r} vor und nach dem Write "
        f"(value_changed={result.value_changed}, {result.duration_sec} s)"
    )
    return 0


async def _run_write(service: MindererService, zone: str, raw_volt: str) -> int:
    # The interactive summary + y/N confirm happens INSIDE service.write()
    # (confirm=True) after the read-back; the audit line is written there.
    result = await service.write(zone, raw_volt, confirm=True)
    print(
        f"OK: Minderer-Write ausgefuehrt -- {result.request!r}, "
        f"Read-back {result.after.current_value!r} V, "
        f"applied={result.applied} ({result.duration_sec} s)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (``python -m app.cmi.minderer <cmd> <zone> [volt]``).

    Exit codes: 0 ok / aborted-by-N, 1 error (``ValueError`` / CMI errors),
    2 interactive confirm required (``write`` without TTY).
    """
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    service = MindererService(settings)
    try:
        if args.command == "status":
            return asyncio.run(_run_status(service, args.zone))
        if args.command == "noop":
            return asyncio.run(_run_noop(service, args.zone))
        # write: pre-checks BEFORE any CMI traffic / prompt.
        service.address(args.zone)  # ValueError -> exit 1
        normalize_volt(args.volt)  # CMISafetyError -> exit 1
        if not sys.stdin.isatty():
            print(
                "FEHLER: interaktiver Confirm erforderlich -- 'write' laeuft "
                "ohne TTY (gepiped stdin) nicht (Phase 1: Mensch am Terminal, "
                "kein --force)",
                file=sys.stderr,
            )
            return 2
        return asyncio.run(_run_write(service, args.zone, args.volt))
    except ValueError as exc:
        return _fail(str(exc))
    except MindererAbortError as exc:
        print(str(exc))  # sauberer Abbruch (Confirm=N) -> exit 0, kein Fehler
        return 0
    except CMISafetyError as exc:
        return _fail(f"Sicherheits-Abbruch: {exc}")
    except CMIGatewayError as exc:
        return _fail(f"CMI-Fehler: {exc}")


if __name__ == "__main__":
    sys.exit(main())
