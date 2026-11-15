"""CMI write-path discovery CLI (inventory + no-op write + 6-page probe).

Run from the repository root::

    .venv/bin/python scripts/cmi_write_discovery.py inventory
    .venv/bin/python scripts/cmi_write_discovery.py noop [--address 101301D0180]
    .venv/bin/python scripts/cmi_write_discovery.py probe6

Subcommands
-----------
* ``inventory`` -- read-only: fetch schematic pages 1-5, resolve every
  settable display address via the changer CGI, record changeadr + options +
  current value.  Never writes.  Output:
  ``scripts/cmi_write_discovery/INVENTAR.md`` + ``INVENTAR.json``.
* ``noop`` -- exactly ONE no-op write: read the current value of the target
  address (default: Betriebsmodus ``101301D0180``), re-send that same value
  via ``change.cgi``, read back and compare.  Output:
  ``scripts/cmi_write_discovery/NOOP.json``.
* ``probe6`` -- read-only 404 probe for ``/schematic_files/{6,7,8}.cgi``
  (documents that no sixth schematic page exists; does NOT create one).
  Output: ``scripts/cmi_write_discovery/PROBE6.json``.

Safety: no subcommand touches the RISK address for writing; the running
gateway app is not involved (this is a standalone CLI).  Credentials come
from ``.env`` (``CMI_USER``/``CMI_PASS``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:  # allow `python scripts/cmi_write_discovery.py`
    sys.path.insert(0, str(_REPO_ROOT))

from app.cmi.client import CMIClient  # noqa: E402
from app.cmi.exceptions import CMIGatewayError  # noqa: E402
from app.cmi.queue import CMISerialQueue  # noqa: E402
from app.cmi.write import (  # noqa: E402
    CMIWriteClient,
    Readback,
    SettableEntry,
    build_inventory_entry,
    collect_settable_addresses,
    format_options,
)
from app.config import get_settings  # noqa: E402

REPO_ROOT = _REPO_ROOT
OUTPUT_DIR = REPO_ROOT / "scripts" / "cmi_write_discovery"

#: Curated labels for the 9 settable addresses (docs/plant-model.json).
LABELS: dict[str, str] = {
    "101301D0180": "Betriebsmodus (HEIZKR. Betriebsart)",
    "10820A10180": "Pumpe Heizkreis",
    "10120A10180": "Pumpe Holzofen",
    "10620A10180": "Ladepumpe WW",
    "10220A10180": "WW vom Oel (Ladep.WW.5)",
    "10720A10180": "Zirkulation",
    "10520A10180": "Oelbrenner (Anfoerdung)",
    "10920A10180": "Ventil Heizungsruecklauf",
    "10040912091": "Solarpumpe Modus (x2)",
}

PAGES: tuple[int, ...] = (1, 2, 3, 4, 5)
PROBE_PAGES: tuple[int, ...] = (6, 7, 8)


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _dump_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  -> {path.relative_to(REPO_ROOT)}")


def _client_credentials() -> tuple[str, str, str]:
    cfg = get_settings()
    return cfg.base_url, cfg.cmi_user, cfg.cmi_pass


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------
async def run_inventory() -> list[SettableEntry]:
    base_url, user, password = _client_credentials()
    queue = CMISerialQueue()
    async with CMIWriteClient(base_url, user, password, queue=queue) as client:
        print(f"[inventory] {base_url} (read-only; 5 Seiten + Changer-Resolution)")
        pages: dict[int, str] = {}
        for page in PAGES:
            html = await client._run(lambda p=page: client.get_schematic(p))  # noqa: SLF001
            pages[page] = html
            print(f"  Seite {page}: {len(html)} Bytes")

        presence = collect_settable_addresses(pages)
        print(
            f"[inventory] {len(presence)} setzbare Adressen gefunden: "
            + ", ".join(sorted(presence, key=lambda a: (presence[a].pages[0], a)))
        )

        entries: list[SettableEntry] = []
        for address in sorted(presence, key=lambda a: (presence[a].pages[0], a)):
            readback = await client.read_current(address)
            entry = build_inventory_entry(
                presence[address],
                readback,
                label=LABELS.get(address),
            )
            entries.append(entry)
            mark = "RISK " if entry.risk else ""
            print(
                f"  {mark}{address}  changeadr={entry.change_address}  "
                f"aktuell={entry.current_value!r}  writeable={entry.writeable}"
            )

    _write_inventory_report(entries)
    return entries


def _write_inventory_report(entries: list[SettableEntry]) -> None:
    _dump_json(
        OUTPUT_DIR / "INVENTAR.json",
        {
            "generated": _now_iso(),
            "source": "live CMI (read-only: schematic 1-5 + changer CGI)",
            "entries": [e.model_dump() for e in entries],
        },
    )

    lines: list[str] = []
    lines.append("# CMI Write-Pfad-Inventar (read-only Discovery, 2026-11-02)")
    lines.append("")
    lines.append(
        f"Erzeugt: {_now_iso()} (live CMI `http://192.168.0.100`, read-only: "
        "`/schematic_files/1..5.cgi` + `/INCLUDE/changer[x2].cgi`)."
    )
    lines.append("")
    lines.append(
        "| Adresse | Bezeichnung | Seiten | changeadr | option_type | Optionen/Min-Max | Aktueller Wert | risk | anwendbar |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for e in entries:
        opts = format_options(
            Readback(
                display_address=e.address,
                x2=e.x2,
                change_address=e.change_address or "",
                option_type=e.option_type or "select",
                option_values=e.option_values,
                option_labels=e.option_labels,
                min_value=e.min_value,
                max_value=e.max_value,
            )
        )
        value = e.current_value if e.current_value is not None else "—"
        value = f"{value} ({e.current_label})" if e.current_label else value
        lines.append(
            f"| `{e.address}` | {e.label or '—'} | {', '.join(map(str, e.pages))} "
            f"| `{e.change_address or '—'}` | {e.option_type or '—'} | {opts} "
            f"| {value} | {'ja' if e.risk else 'nein'} | {'ja' if e.writeable else 'nein'} |"
        )
    lines.append("")
    not_writeable = [e for e in entries if not e.writeable]
    if not_writeable:
        lines.append("## Nicht schreibbar (Details)")
        lines.append("")
        for e in not_writeable:
            lines.append(f"- `{e.address}` ({e.label}): {e.note or 'siehe Tabelle'}")
        lines.append("")
    (OUTPUT_DIR / "INVENTAR.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  -> {(OUTPUT_DIR / 'INVENTAR.md').relative_to(REPO_ROOT)}")


# ---------------------------------------------------------------------------
# noop write
# ---------------------------------------------------------------------------
async def run_noop(address: str) -> dict:
    base_url, user, password = _client_credentials()
    queue = CMISerialQueue()
    async with CMIWriteClient(
        base_url, user, password, queue=queue, readback_delay_sec=1.0
    ) as client:
        print(f"[noop] ZIEL: {address} (Betriebsmodus) -- exakt EIN No-Op-Write")
        print("[noop] 1/3 Read-back VORHER (changer + Schematic-Text)")
        before = await client.read_current(address)
        print(
            f"       vorher: value={before.current_value!r} label={before.current_label!r} "
            f"schematic_text={before.schematic_text!r} consistent={before.consistent}"
        )

        print("[noop] 2/3 No-Op-Write (change.cgi, changeto = aktueller Wert)")
        result = await client.noop_write(address)

        print("[noop] 3/3 Read-back NACHHER")
        print(
            f"       nachher: value={result.after.current_value!r} "
            f"schematic_text={result.after.schematic_text!r}"
        )
        print(f"       HTTP-Response: {result.response_body!r}")
        print(
            f"       value_changed={result.value_changed} "
            f"label_changed={result.label_changed} noop_ok={result.noop_ok} "
            f"dauer={result.duration_sec}s"
        )
        print(f"       request: {result.request}")

    payload = result.model_dump()
    _dump_json(
        OUTPUT_DIR / "NOOP.json",
        {
            "generated": _now_iso(),
            "target": address,
            "result": payload,
        },
    )
    return payload


# ---------------------------------------------------------------------------
# 6-page probe (read-only)
# ---------------------------------------------------------------------------
async def run_probe6() -> dict:
    base_url, user, password = _client_credentials()
    queue = CMISerialQueue()
    print(
        f"[probe6] {base_url} -- 404-Pruefung fuer {list(map(str, PROBE_PAGES))}.cgi "
        f"(read-only, wird NICHTS angelegt)"
    )
    results: dict[str, dict] = {}
    async with CMIClient(base_url, user, password, timeout_sec=15.0) as client:
        for page in PROBE_PAGES:
            path = f"/schematic_files/{page}.cgi"
            try:
                html = await queue.run(lambda p=page: client.get_schematic(p), timeout=20.0)
                # An unexpected success would mean the CMI now has a page --
                # record it, do nothing else.
                results[str(page)] = {
                    "path": path,
                    "status": 200,
                    "body_head": html[:200],
                    "conclusion": "UNERWARTE: Seite existiert",
                }
                print(f"  {path}: HTTP 200 ({len(html)} Bytes) -- UNERWARTE, Seite existiert")
            except CMIGatewayError as exc:
                match = re.search(r"HTTP (\d+)", str(exc))
                status = int(match.group(1)) if match else None
                results[str(page)] = {
                    "path": path,
                    "status": status,
                    "error": str(exc),
                    "conclusion": "404 -- keine Seite" if status == 404 else "PRAEUFEN",
                }
                print(f"  {path}: HTTP {status} -- {results[str(page)]['conclusion']}")

    payload = {
        "generated": _now_iso(),
        "note": (
            "Read-only-Beobachtung: 6-8.cgi liefern 404 (CMI-Server-Error-Page). "
            "Eine 6. Schematic-Seite existiert NICHT und wurde NICHT angelegt. "
            "Die logische 'Seite 6' (KI-Einstellungen) ist ein Gateway-UI-Panel "
            "(S4N-UI-KI), kein CMI-Seiten-Upload."
        ),
        "results": results,
    }
    _dump_json(OUTPUT_DIR / "PROBE6.json", payload)
    return payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inventory", help="read-only Inventar aller setzbaren Adressen")
    noop_parser = sub.add_parser("noop", help="EIN No-Op-Write (aktueller Wert zurueckschreiben)")
    noop_parser.add_argument(
        "--address", default="101301D0180", help="Ziel-Adresse (Default: Betriebsmodus)"
    )
    sub.add_parser("probe6", help="read-only 404-Pruefung 6/7/8.cgi")
    args = parser.parse_args(argv)

    if args.command == "inventory":
        asyncio.run(run_inventory())
    elif args.command == "noop":
        asyncio.run(run_noop(args.address))
    elif args.command == "probe6":
        asyncio.run(run_probe6())
    return 0


if __name__ == "__main__":
    sys.exit(main())
