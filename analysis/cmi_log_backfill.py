#!/usr/bin/env python3
"""One-shot (re-runnable), host-agnostic backfill: CMI /LOG-Rohdateien -> cmi-log Parquet-Store.

Kontext (S4N-LogParquet / S4N-LogFull / Q-112b / Q-121): Alle lokal vorliegenden
CMI /LOG-Data-Dateien (``data_YYYY_MM_DD_HH_MM_SS.log``) werden hier mit demselben
``decode_cmi_log`` wie der Poller dekodiert. Der Decoder emittiert je Record die
ganzen Wertkanäle des jeweiligen Record-Formats (P):

* **P=169 (legacy, bis 2026-11-07 16:14):** seit S4N-LogFull **alle 70
  Wertkanäle** (48 analog + 22 digital, inkl. der dokumentierten 0.0-Fills dev0
  a11-a15 / dev1 a10 / dev1 a14-a16 / dev1 d1-d13 und der Minderer dev2
  a10-a12) -> jeder P=169-Record ist ein **vollständiger 70-Spalten-Wide-Row**
  (keine NULL-Wertspalte mehr, außer den drei Q-125-Spalten dev2 a17-a19, die
  im P=169-Payload nicht vorkommen und dort NULL bleiben).
* **P=181 (seit 2026-11-07 ~16:14, Q-125 Round 2):** 73 Wertkanäle (51 analog
  + 22 digital).  Die wertverifizierten Kanäle (dev0 a1-a10, dev2 a1-a19,
  dev0 d9, dev2 d1) sind am KORRIGIERTEN Frame (sync @ rec 0-2, Zeit @ 3/4/5,
  data_start per Sync-Scan = 6258 = Header-Ende + 2×CRLF) per Rosetta gegen
  json:1 (dev0) / json:32 (dev2) verifiziert (±60 s, n=214/215).  Die
  P=181-PENDING-Kanäle (dev0 a11-a16, dev1 a1-a16, dev0 d2-d8 -- kein eindeutiges
  Ground-Truth; HITL-pending, nie ein geratenes Wert) tragen ein dokumentiertes
  **NULL**; dev1 d1-d13 ist der dokumentierte 0.0-Fill (wie P=169).  P=181-
  Records sind also 73-Spalten-Wide-Rows mit NULLs in den PENDING-Spalten; das
  74-Spalten-Store-Schema (71 winsol_pq + dev2 a17-a19) bleibt intakt.

HOST-AGNOSTIK (Q-121 Phase 2, Pi-Kompatibilität): Es gibt KEINE hartcodierte
File-Liste mehr. Die Quelle wird dynamisch erkannt:

* ``--raw-dir PATH`` (Default: ``$CMI_LOG_RAW_DIR`` bzw.
  ``<repo>/data/cmi_log``). Auf dem Dev-Host = lokale ``data/cmi_log``; im
  Pi-``cmi-m1-merge``-Container = ``/srv/cmi/data/cmi_log`` (exakt dort speichert
  der /LOG-Poller die heruntergeladenen Dateien: ``cmi_data_dir / "cmi_log"``).
  **Explizites ``--raw-dir`` (oder Env) = exakt nur dieses Verzeichnis**
  (Pi-Semantik: der Pi hat nur diese Host-Instanz-Dateien).
* Dekodiert werden NUR Dateien mit dem CMI-/LOG-Basenames-Muster
  ``data_\\d{4}_\\d{2}_\\d{2}_\\d{2}_\\d{2}_\\d{2}\\.log`` (exakt wie
  ``cmi_log._date_from_filename``); sonstiges im raw-dir (README, ``*.part``,
  andere ``*.log``) wird ignoriert.
* **Dev-Default (ohne ``--raw-dir``/Env) zusätzlich:** die 5 Rosetta-25
  CMI-/LOG-Captures aus ``discovery/cmi-log/`` (10-27/28/29 Volltage + die
  zwei 10-29-Abend-Fragmente), lokal umbenannt (``cmi_raw_*``/``cmi_*``),
  werden unter ihren **ursprünglichen CMI-/LOG-Basenames** als zusätzliche
  Quelle mitdekodiert -- der Dev-Store enthält diese Tage NUR aus diesen
  Captures (sie sind NICHT in ``data/cmi_log``). ``discovery/`` ist
  ``.dockerignore``d, daher liegt die Liste im Pi-Image leer -> der Pi-Run
  (mit ``--raw-dir``) dekodiert exakt nur die raw-dir-Files. (Abweichung von
  "nur raw-dir-Glob", dokumentiert: ohne die Captures würde der lokale
  Default-Run am TS-Safety-Guard für 2026-10 abbrechen.)
* ``--months YYYY-MM`` (repeatable): nur diese Monate werden geprüft +
  geschrieben (Default: alle Kalendermonate, die die Rohdateien abdecken).
* ``--expected-size FILE=BYTES`` (repeatable): **harter** Größen-Check
  (Mismatch = Abort). Ohne Angabe: **kein** Größen-Check -- die CMI-Index-Größe
  ist host-unabhängig nicht bekannt, wachsende Files sind normal (der Poller
  holt per Name+Größe-Dedup fortlaufend nach). Die alten Rosetta-25-Index-Größen
  (10-27/28/29/30/31) bleiben NUR als **Warnung** (kein Abort), wenn die
  betreffende Datei im raw-dir gefunden wird.
* ``cmi_name`` = Filename (Basenames der CMI-/LOG-Dateien); ``decode_cmi_log``
  (aus ``app.cmi.cmi_log``, der gefixte signed-Decoder) bleibt der einzige
  Decode-Pfad.

LogX-Gap (dev-spezifisch): Die physisch verlorene Periode 2026-10-30 00:00 ->
15:16:48 ist *teilweise* aus der WinSol LogX-Datei
``discovery/cmi-log/logx_2026-10-30_toggle.log`` auffüllbar (alle 70 Kanäle
Rosetta-kanonisch). Die Datei liegt NUR im Dev-Repo (``discovery/`` ist in
``.dockerignore``, also nicht im Image); fehlt sie (Pi), wird der Backfill
dazu einfach nicht ergänzt -- kein Fehler, keine Rows. Merge-Regel unverändert:
**CMI-nativ gewinnt bei ts-Kollision über LogX**.

WRITE-SEMANTIK (S4N-LogFull): Der Backfill löscht je (gewähltem) Monats-File
und schreibt den Monat **vollständig** neu -- ausschließlich über die
existente ``CmiLogParquet.write_month``-Pipeline (Schema-Check, ts-Dedup,
``.part`` + atomares ``os.replace``); es werden keine ad-hoc Parquet-Files
geschrieben.  **TS-Safety-Guard (Pi-Partial-Set-Schutz):** vor dem Rewrite wird
geprüft, dass jedes ts der bestehenden Monatsdatei in den dekodierten
Quelldateien liegt (kein Datenverlust); fehlt auch nur ein Store-ts in der
Quelle -> **Abort mit klarer Meldung + Exit 1**, der Store bleibt unverändert
(kein ``.part``-Rest, kein Write). Das ist der Schutz für den Pi, wo der
Roh-File-Satz (eigene Host-Instanz, Polling erst ab ~R1/10-30) nicht zwingend
alle Store-Tage abdeckt.

Zusätzlich: **Cleanup** des S4N-LogPoll-Zeitraums: alle
``measurements WHERE source='cmi-log'``-Rows werden gelöscht (idempotent,
die Historie lebt jetzt im Parquet-Store); die Side-Table
``cmi_log_ingested`` bleibt intakt (Poller-Dedup + Clear-Gate).

**Race-Hinweis (laufender Gateway-Poller):** Der laufende Monat wird vom
Gateway-Poller alle 5 min neu geschrieben (bestehende ts gewinnen, neue ts
werden angehängt). Zwischen TS-Safety-Check und dem atomaren Replace könnte
der Poller neue ts anhängen, die der Backfill nicht kennt -> sie gehen bei
diesem Rewrite verloren, werden aber vom Poller im nächsten Zyklus (gleiche
Roh-Datei) und/oder beim nächsten Backfill-Run wiederhergestellt. Idempotent
und beliebig wiederholbar; am Pi: den Run auf die Zeit zwischen den
M1-:15-Builds legen (atomare Writes machen das sicher, der M1-Build sieht nie
halbschriebene Dateien).

Nur Lese-Daten (lokal) + Parquet-Write (via write_month) + DB-Cleanup.
**Kein CMI-Zugriff, kein clear.log, kein git-Commit.**

Verwendung:
    .venv/bin/python -m analysis.cmi_log_backfill                # Dev: data/cmi_log, alle Monate
    .venv/bin/python -m analysis.cmi_log_backfill --dry-run      # nur dekodieren + Bericht
    .venv/bin/python -m analysis.cmi_log_backfill --months 2026-11
    .venv/bin/python -m analysis.cmi_log_backfill \
        --raw-dir /srv/cmi/data/cmi_log --months 2026-11         # Pi (Merge-Container)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Final
from urllib.parse import quote

import duckdb
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "analysis"))

from winsol_logx import Record, decode_record, find_sessions  # noqa: E402

from app.cmi.cmi_log import decode_cmi_log  # noqa: E402
from app.cmi.cmi_log_pq import (  # noqa: E402
    _NEW_DEV2_ANALOG_COLUMNS,
    WIDE_COLUMNS,
    CmiLogParquet,
)
from app.config import Settings  # noqa: E402
from app.db import get_history_db  # noqa: E402

#: Schema-Referenz: das winsol_pq-Monatsfile, das die kanonische
#: 71-Spalten-Wide-Schema (Namen + Reihenfolge) definiert.  Abweichung
#: zwischen WIDE_COLUMNS und dieser Referenz = Abbruch.
WINSOL_REF: Final[str] = "data/winsol_pq/2026/10/2026-10.parquet"

#: Default-raw-dir (host-agnostisch): dort, wo der /LOG-Poller die CMI-Data-
#: Files ablegt (``cmi_data_dir / "cmi_log"``).  Dev-Host: ``<repo>/data/cmi_log``
#: (``cmi_data_dir``-Default).  Pi-``cmi-m1-merge``-Container:
#: ``/srv/cmi/data/cmi_log`` (REPO = /srv/cmi im Image, ``cmi_data``-Volume
#: auf /srv/cmi/data gemountet).  Override: ``--raw-dir`` oder Env
#: ``CMI_LOG_RAW_DIR``.
DEFAULT_RAW_DIR: Final[Path] = REPO / "data" / "cmi_log"
RAW_DIR_ENV: Final[str] = "CMI_LOG_RAW_DIR"

#: Basenames der CMI /LOG-Data-Files (exakt wie ``cmi_log._date_from_filename``):
#: ``data_<JJJJ>_<MM>_<DD>_<HH>_<MM>_<SS>.log``.  Nur diese werden dekodiert.
DATA_FILE_BASE_RE: Final[re.Pattern[str]] = re.compile(
    r"^data_\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2}\.log$"
)

#: Die Rosetta-25-Index-Größen (CMI /LOG-Index, gegen die lokale Abholung
#: verifiziert, 2026-10-27/-28/-29/-30/-31): wenn eine dieser Dateien im
#: raw-dir gefunden wird und die lokale Größe abweicht, ist das **nur eine
#: Warnung** (informativ) -- kein Abort.  Die CMI-Index-Größe ist
#: host-unabhängig nicht bekannt (andere Host-Instanz, ggf. andere Abdeckung,
#: wachsende Files); harte Checks laufen ausschließlich über
#: ``--expected-size``.
ROSETTA_25_SIZES: Final[dict[str, int]] = {
    "data_2026_10_27_07_26_14.log": 1013765,
    "data_2026_10_28_00_00_04.log": 1466178,
    "data_2026_10_29_00_00_05.log": 1242929,
    "data_2026_10_29_20_19_56.log": 6356,
    "data_2026_10_29_20_20_16.log": 6863,
    "data_2026_10_29_20_21_06.log": 228084,
    "data_2026_10_30_19_40_56.log": 16665,
    "data_2026_10_30_19_51_56.log": 176539,
    "data_2026_10_31_00_00_06.log": 1458235,
}

MONTH_RE: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}$")

#: Dev-only Zusatzerkennung (NUR Default-Run ohne --raw-dir/Env): die
#: Rosetta-25 CMI /LOG-Captures (lokal umbenannt ``cmi_raw_*`` / ``cmi_*``),
#: dekodiert unter ihren **ursprünglichen CMI-/LOG-Basenames**.  Der
#: Dev-Store enthält die Tage 10-27/28/29 (+ die 10-29-Abend-Fragmente)
#: ausschließlich aus diesen Captures -- ohne sie würde der lokale
#: Default-Run am TS-Safety-Guard für 2026-10 abbrechen.  Im Pi-Image
#: existiert ``discovery/`` nicht (.dockerignore) -> Liste leer, der
#: Backfill dekodiert exakt nur die raw-dir-Files.  Ein expliziter
#: ``--raw-dir``/Env-Run (Pi-Semantik) blendet diese Captures aus.
DEV_CMI_CAPTURES: Final[tuple[tuple[str, str], ...]] = (
    ("discovery/cmi-log/cmi_raw_2026-10-27.log", "data_2026_10_27_07_26_14.log"),
    ("discovery/cmi-log/cmi_raw_2026-10-28.log", "data_2026_10_28_00_00_04.log"),
    ("discovery/cmi-log/cmi_raw_2026-10-29.log", "data_2026_10_29_00_00_05.log"),
    ("discovery/cmi-log/cmi_2026-10-29_20_19_56.log", "data_2026_10_29_20_19_56.log"),
    ("discovery/cmi-log/cmi_2026-10-29_20_20_16.log", "data_2026_10_29_20_20_16.log"),
)

#: Die LogX-Gap-Quelle (WinSol D2026-10-30.log, lokal umbenannt) -- liegt NUR
#: im Dev-Repo (discovery/ ist .dockerignore'd; auf dem Pi fehlt sie, dann
#: trägt der Backfill schlicht keine LogX-Gap-Rows).
LOGX_GAP_REL: Final[str] = "discovery/cmi-log/logx_2026-10-30_toggle.log"
#: Das Datum, das die LogX-Records tragen (aus dem ursprünglichen
#: WinSol-D-Dateinamen ``D2026-10-30.log``; die Records selbst sind
#: hh/mm/ss ohne Datum).
LOGX_GAP_DAY: Final[str] = "2026-10-30"

#: Die cmi-log-Wertkanäle als ``(dev, ad, channel)`` -- seit Q-125 exakt die
#: volle Abdeckung von ``WIDE_COLUMNS`` (73 Wertkanäle: dev0 a1-a16, dev1
#: a1-a16, dev2 a1-a19, dev0 d2-d9, dev1 d1-d13, dev2 d1).  P=169-Records
#: tragen alle 70 P=169-Kanäle + NULL in den drei Q-125-Kanälen dev2
#: a17-a19; P=181-Records tragen zusätzlich dev2 a17-a19 (Sollspannung
#: 1/2/3).  LogX-Werte außerhalb dieser Menge gibt es nicht (LogX P=137
#: trägt dieselben 70 P=169-Kanäle, aber keine dev2 a17-a19).
_CMI_LOG_CHANNELS: Final[tuple[tuple[int, str, int], ...]] = tuple(
    sorted(
        {(0, "A", c) for c in range(1, 17)}
        | {(1, "A", c) for c in range(1, 17)}
        | {(2, "A", c) for c in range(1, 20)}
        | {(0, "D", c) for c in range(2, 10)}
        | {(1, "D", c) for c in range(1, 14)}
        | {(2, "D", 1)}
    )
)
assert len(_CMI_LOG_CHANNELS) == 73, "cmi-log Abdeckung muss 73 Kanäle sein (Q-125)"

#: Die drei Q-125-Spalten (P=181 neu, dev2 a17-a19 = Sollspannung 1/2/3).
#: Für P=169-Records UND LogX-Gap-Rows ist das Payload NULL (diese Kanäle
#: existieren im P=169/LogX-Record nicht) -> in reinen P=169-Monaten sind
#: genau diese drei Spalten zulässigerweise NULL.
_NEW3_COLUMNS: Final[tuple[str, ...]] = tuple(_NEW_DEV2_ANALOG_COLUMNS)

#: P=181-PENDING-Kanäle (Q-125 Round 2): deren P=181-Slot ist nicht
#: wertverifiziert (HITL-pending) -> der Decoder emittiert ein dokumentiertes
#: NULL (nie ein geratenes Wert).  Betroffene Wide-Spalten:
#:   * dev0 analog a11-a16 (kein JSON-GT: json:1 trägt nur ch1-10 analog;
#:     a11=VL.Solltemp, a12=T.raum.SOLL.eff, a13-a15="-----", a16=Strömungsschalter);
#:   * dev1 analog a1-a16 (kein JSON-Bezug: UVR1611-RAS ist nicht im JSON-API;
#:     P169-Continuity ist wegen 6-min-Lücke + 20.0°C-Plateau nicht eindeutig);
#:   * dev0 digital d2-d8 (json:1 exponiert 12 digitale Kanäle ch3-16, CMI dev0
#:     loggt nur d2-d9 -> die json:1-D-Nummerung stimmt nicht 1:1 mit der CMI-
#:     d-Nummerung; nur d9 (O8-Pumpe) ist task-verankert).
#: dev0 d9 (O8-Pumpe @ byte 16) und dev2 d1 (Solarpumpe @ byte 10) sind
#: WERTVERIFIZIERT (json:1 ch9 D / json:32 ch1 D) und NICHT PENDING.
#: dev1 digital d1-d13 ist in P=181 wie in P=169 der dokumentierte 0.0-Fill.
_P181_PENDING_COLUMNS: Final[frozenset[str]] = frozenset(
    # Q-125 R5 (2026-11-08): dev1 a1-a16, dev0 a11-a16 und dev0 d2-d8 sind
    # wertverifiziert und tragen WERTE in P=181-Rows -> NICHT mehr toleriert.
    # Einzige verbleibende P=181-Strukturnulls: dev2 a1-a4 (ELIMINIERT,
    # CAN-only, UVR1611-Master; P=169 gefuellt, P=181 NULL).
    {f"dev2_analog_{c:02d}" for c in range(1, 5)}
)


def discover_raw_files(raw_dir: Path) -> list[Path]:
    """Alle CMI /LOG-Data-Files im ``raw_dir`` (Basenames-Muster, sortiert).

    Nur Dateien namens ``data_<JJJJ>_<MM>_<DD>_<HH>_<MM>_<SS>.log``
    (:data:`DATA_FILE_BASE_RE`, exakt wie ``cmi_log._date_from_filename``)
    werden berücksichtigt; alles andere (README, ``*.part``-Reste, andere
    ``*.log``) wird ignoriert.
    """
    out: list[Path] = []
    for p in sorted(raw_dir.glob("data_*.log")):
        if p.is_file() and DATA_FILE_BASE_RE.match(p.name):
            out.append(p)
    return out


def _parse_expected_sizes(values: list[str]) -> dict[str, int]:
    """``--expected-size FILE=BYTES``-Einträge -> dict (malformiert = Abort)."""
    out: dict[str, int] = {}
    for v in values:
        name, sep, size = v.partition("=")
        if not sep or not name or not size.isdigit():
            raise SystemExit(
                f"--expected-size: malformierter Eintrag {v!r} "
                f"(erwartet: 'data_YYYY_MM_DD_HH_MM_SS.log=<bytes>')"
            )
        out[name] = int(size)
    return out


def _parse_months(values: list[str]) -> list[str]:
    """``--months``-Einträge validieren (``YYYY-MM``), deduplizieren, sortieren."""
    months: set[str] = set()
    for v in values:
        if not MONTH_RE.match(v):
            raise SystemExit(f"--months: malformierter Monat {v!r} (erwartet: 'YYYY-MM')")
        months.add(v)
    return sorted(months)


def _wide_records(rows: list[tuple[str, str, int, str, float | None, str | None, str]]) -> list[tuple[str, dict[str, float | None]]]:
    """Pivot long cmi-log rows into one wide row per timestamp.

    ``dev``/``ad``/``channel`` map onto the canonical wide column names
    (``dev{dev}_analog_{N:02d}`` / ``dev{dev}_digital_{N:02d}``).  P=169 rows
    carry a value in every P=169 channel (S4N-LogFull); P=181 rows additionally
    populate dev2 a17-a19 and carry a documented ``None`` (-> NULL wide column)
    in the P=181-pending channels (dev1 a1-a16, dev2 a1-a4, P=181 digital).
    """
    wide: dict[str, dict[str, float | None]] = {}
    for ts, device, channel, ad, value, _unit, _src in rows:
        row = wide.setdefault(ts, {})
        dev = device.rsplit(":", 1)[-1]
        kind = "analog" if ad == "A" else "digital"
        row[f"dev{dev}_{kind}_{int(channel):02d}"] = value
    return [(ts, row) for ts, row in wide.items()]


def _logx_wide_rows(records: list[Record]) -> list[tuple[str, dict[str, float | None]]]:
    """Pivot LogX records into cmi-log wide rows (alle 70 Kanäle).

    ``ts`` = ``LOGX_GAP_DAY`` + hh:mm:ss des Records.  Es werden alle
    70 cmi-log-Wertkanäle aus ``record.values`` (Keys ``a1``..``a16`` /
    ``d1``..``d13``, Device-Int-Index) übernommen -- LogX P=137 trägt
    dieselben 70 Kanäle (Rosetta-kanonisch).  Fehlt ein Key im Record
    (``----``-Kanäle älterer Varianten), bleibt die Spalte NULL.
    """
    wide: dict[str, dict[str, float | None]] = {}
    for rec in records:
        if not rec.valid_clock:
            continue  # korrumpierte/abgerissene Record: verwerfen
        ts = f"{LOGX_GAP_DAY} {rec.hh:02d}:{rec.mm:02d}:{rec.ss:02d}"
        row = wide.setdefault(ts, {})
        for dev, ad, ch in _CMI_LOG_CHANNELS:
            key = ("a" if ad == "A" else "d") + str(ch)
            val = rec.values.get(dev, {}).get(key)
            if val is None:
                continue  # Kanal in dieser LogX-Record nicht vorhanden
            kind = "analog" if ad == "A" else "digital"
            row[f"dev{dev}_{kind}_{ch:02d}"] = float(val)
    return [(ts, row) for ts, row in wide.items()]


def _logx_selfcheck(
    rows: list[tuple[str, dict[str, float | None]]],
    cmi_ts: set[str],
) -> tuple[list[str], list[str]]:
    """Rosetta-Selbstcheck der LogX-Gap-Rows.

    Returns ``(problems, warnings)``: ``problems`` = Abbruch (leere Quelle,
    Row ohne jeden der 70 Werte, digital außerhalb 0/1, nicht-monotone
    ts); ``warnings`` = nur berichten (ts-Kollision LogX vs CMI -- CMI-nativ
    gewinnt dann die Merge).
    """
    problems: list[str] = []
    warnings: list[str] = []
    if not rows:
        return ["LogX-Gap-Quelle lieferte keine Wide-Rows"], warnings
    for ts, row in rows:
        if not row:
            problems.append(f"{ts}: kein Wert in irgendeinem der 70 cmi-log-Kanäle")
        for col, val in row.items():
            if col.split("_")[1] == "digital" and val not in (0.0, 1.0):
                problems.append(f"{ts}: digital {col}={val!r} nicht in 0/1")
    ts_seq = [ts for ts, _ in rows]
    if any(a > b for a, b in zip(ts_seq, ts_seq[1:], strict=False)):
        problems.append("ts-Sequenz nicht monoton nicht-deckreidend")
    collisions = sorted(cmi_ts & set(ts_seq))
    if collisions:
        warnings.append(
            f"{len(collisions)} ts-Kollisionen LogX vs CMI "
            f"({collisions[0]} .. {collisions[-1]}) -- CMI-nativ gewinnt"
        )
    return problems, warnings


def _schema_check() -> tuple[bool | None, str]:
    """Prüfe, dass ``WIDE_COLUMNS`` (74 Spalten, Q-125) exakt der winsol_pq-
    Referenz (71 Spalten) PLUS den drei Q-125-Kanälen ``dev2_analog_17/18/19``
    entspricht -- exakt in dieser Reihenfolge (Namen + Reihenfolge).

    Die winsol_pq-Referenz bleibt die kanonische 71-Spalten-Basis
    (``analysis/winsol_to_parquet.py``); der cmi-log-Store trägt seit Q-125
    die drei P=181-Sollspannungs-Kanäle zusätzlich, direkt nach
    ``dev2_analog_16`` (``WIDE_COLUMNS``-Reihenfolge).  Gleichsetzung der
    74-Spalten-Liste mit der 71-Spalten-Referenz wäre falsch -- gecheckt wird
    daher die exakte 74-Spalten-Erweiterung der Referenz.

    Rückgabe ``(None, ...)`` = Referenzfile nicht vorhanden (läuft nur auf
    Hosts ohne ``data/winsol_pq``; ein echtes Backfill-Write bleibt
    fail-closed, ein ``--dry-run`` meldet die Prüfung als nicht ausführbar).
    """
    ref = REPO / WINSOL_REF
    if not ref.exists():
        return None, f"Referenzfile {WINSOL_REF} fehlt (nicht prüfbar)"
    ref_names = pq.read_schema(ref).names
    # Die drei Q-125-Kanäle hängen exakt nach ``dev2_analog_16`` (der
    # dev2-analog-Block bleibt zusammen; ``dev2_digital_01`` bleibt die
    # letzte Spalte).
    anchor = ref_names.index("dev2_analog_16")
    expected = [*ref_names[: anchor + 1], *_NEW3_COLUMNS, *ref_names[anchor + 1:]]
    if tuple(expected) != WIDE_COLUMNS:
        return False, (
            f"WIDE_COLUMNS != {WINSOL_REF} + dev2_analog_17/18/19 "
            f"(Einfügen nach dev2_analog_16; erste Abweichung: "
            f"{next((i, a, b) for i, (a, b) in enumerate(zip(WIDE_COLUMNS, expected, strict=False)) if a != b)})"
        )
    return True, (
        f"OK: WIDE_COLUMNS ({len(WIDE_COLUMNS)} Spalten) == {WINSOL_REF} "
        f"({len(ref_names)} Spalten) + dev2_analog_17/18/19 nach dev2_analog_16"
    )


def _guard_ts_union(month_file: Path, my_ts: set[str]) -> tuple[bool, str]:
    """Safety-Beleg vor dem Löschen: jedes ts der bestehenden Monatsdatei
    muss im Quelldict liegen (kein Datenverlust bei der Voll-Rewrite).

    Das ist der Pi-Partial-Set-Schutz: decken die Rohdateien eines Monats
    den Store nicht vollständig ab (andere Host-Instanz, eigenes Polling),
    wird der Rewrite abgebrochen -- der Store bleibt unverändert.
    """
    if not month_file.exists():
        return True, "keine bestehende Datei (frischer Write)"
    have = set(pq.read_table(month_file, columns=["ts"]).column("ts").to_pylist())
    lost = sorted(have - my_ts)
    if lost:
        return False, (
            f"{len(lost)} Store-ts fehlen in der Quelle (erste: {lost[:3]}) -- "
            "Voll-Rewrite abgebrochen, Datenverlust ausgeschlossen"
        )
    new = len(my_ts - have)
    return True, (
        f"OK: alle {len(have)} Store-ts in der Quelle; {new} zusätzliche/gefällte "
        f"ts (25 Spalten werden gefüllt)"
    )


def _dod_check(store_dir: Path, months: list[str]) -> tuple[list[str], list[str]]:
    """P-aware Null-NULLs-DoD (Q-125): alle Wertspalten, alle Rows, 0 NULLs --
    AUSSER den Q-125-Transitions-Spalten, die strukturell P-abhängig NULL sind:

    * die drei Q-125-Kanäle ``dev2_analog_17/18/19`` (_NEW3_COLUMNS): NULL für
      alle P=169-Rows (kein Payload), gefüllt für P=181-Rows;
    * die P=181-Strukturnull-Kanäle (_P181_PENDING_COLUMNS): gefüllt für
      P=169-Rows, NULL für P=181-Rows (R5: nur noch dev2 a1-a4 = eliminiert,
      CAN-only/UVR1611-Master; dev1 a1-a16 + dev0 a11-a16 + dev0 d2-d8 sind
      seit R5 wertverifiziert und müssen in P=181-Rows gefüllt sein).

    In einem Monats-File, das P=169- und P=181-Rows mischt (z. B. 2026-11),
    tragen diese Spalten daher zulässigerweise NULLs; alle ANDEREN Spalten
    müssen in jeder Row gefüllt sein (ein NULL dort = Datenverlust).  Dazu
    ts-Deckung (keine Duplikate).  Liefert ``(problems, report_lines)``."""
    tolerated = set(_NEW3_COLUMNS) | _P181_PENDING_COLUMNS
    con = duckdb.connect()
    problems: list[str] = []
    lines: list[str] = []
    try:
        for month in sorted(months):
            f = store_dir / month[:4] / month[5:7] / f"{month}.parquet"
            n, ts0, ts1 = con.execute(
                f"SELECT COUNT(*), MIN(ts), MAX(ts) FROM read_parquet('{f}')"
            ).fetchone()
            exprs = ", ".join(
                f"SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END) AS \"{c}\""
                for c in WIDE_COLUMNS[1:]
            )
            row = con.execute(f"SELECT {exprs} FROM read_parquet('{f}')").fetchone()
            nulls = {c: int(row[i]) for i, c in enumerate(WIDE_COLUMNS[1:]) if row[i]}
            bad = {c: v for c, v in nulls.items() if c not in tolerated}
            transition = {c: v for c, v in nulls.items() if c in tolerated}
            dups = con.execute(
                f"SELECT COUNT(*) - COUNT(DISTINCT ts) FROM read_parquet('{f}')"
            ).fetchone()[0]
            lines.append(
                f"  {f.name}: {n} rows, ts {ts0} .. {ts1}, Duplikate: {dups}, "
                f"NULL-Wertspalten (außer Q-125-Transitions): "
                f"{bad if bad else 'KEINE (alle gefüllt)'}"
                + (
                    f", Q-125-Transitions-NULLs (P-abhängig, ok): {transition}"
                    if transition
                    else ""
                )
            )
            if bad:
                problems.append(f"{month}: unerwartete NULLs in {len(bad)} Spalten: {bad}")
            if dups:
                problems.append(f"{month}: {dups} ts-Duplikate")
    finally:
        con.close()
    return problems, lines


def _count_cmi_log_rows(db_path: Path) -> int | None:
    """Read-only: Anzahl ``measurements WHERE source='cmi-log'``.

    Liefert ``None``, wenn DB-File oder Tabelle nicht existiert -- ein
    Dry-Run darf das DB-File NICHT erzeugen.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    uri = "file:" + quote(str(db_path.resolve())) + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        try:
            return int(
                con.execute(
                    "SELECT count(*) FROM measurements WHERE source = 'cmi-log'"
                ).fetchone()[0]
            )
        finally:
            con.close()
    except sqlite3.Error:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="nur dekodieren + Bericht, keine Parquet/DB-Write")
    ap.add_argument("--raw-dir", metavar="PATH", default=None,
                    help=f"CMI /LOG-Data-Verzeichnis (Default: ${RAW_DIR_ENV} bzw. "
                         f"{DEFAULT_RAW_DIR})")
    ap.add_argument("--months", action="append", metavar="YYYY-MM", default=None,
                    help="nur diese Monate prüfen + schreiben (repeatable; "
                         "Default: alle Monate, die die Rohdateien abdecken)")
    ap.add_argument("--expected-size", action="append", metavar="FILE=BYTES",
                    default=None,
                    help="harter Größen-Check für eine Rohdatei (repeatable); "
                         "Mismatch = Abort. Ohne Angabe: kein Größen-Check "
                         "(Rosetta-25-Index-Größen nur als Warnung)")
    args = ap.parse_args(argv)

    env_raw = os.environ.get(RAW_DIR_ENV, "").strip()
    if args.raw_dir:
        raw_dir = Path(args.raw_dir).expanduser()
        explicit_raw = True
    elif env_raw:
        raw_dir = Path(env_raw).expanduser()
        explicit_raw = True
    else:
        raw_dir = DEFAULT_RAW_DIR
        explicit_raw = False
    if not raw_dir.is_dir():
        print(f"ABBRUCH (raw-dir): {raw_dir} existiert nicht / ist kein "
              f"Verzeichnis (--raw-dir bzw. ${RAW_DIR_ENV})", file=sys.stderr)
        return 2
    files = discover_raw_files(raw_dir)
    if not files:
        print(f"ABBRUCH (raw-dir): keine CMI /LOG-Data-Files in {raw_dir} "
              f"(erwartet: data_YYYY_MM_DD_HH_MM_SS.log)", file=sys.stderr)
        return 2
    expected_sizes = _parse_expected_sizes(args.expected_size or [])
    requested_months = _parse_months(args.months or [])

    settings = Settings()
    store = CmiLogParquet(settings.cmi_log_parquet_dir)

    print(f"cmi_log_parquet_dir: {settings.cmi_log_parquet_dir}")
    print(f"history_db: {settings.history_db_path}")
    print(f"raw-dir: {raw_dir} ({len(files)} Data-Files)")

    # Quelle 1: CMI /LOG-Data-Dateien.  ``sources`` = (Quelle-Label, Pfad,
    # CMI-Basenames).  Default-Run (ohne --raw-dir/Env) nimmt die Dev-
    # Rosetta-25-Captures (lokal umbenannt, .dockerignore'd) unter ihren
    # originalen CMI-/LOG-Basenames hinzu; expliziter Run (Pi) dekodiert
    # exakt nur die raw-dir-Files.
    sources: list[tuple[str, Path, str]] = [(fp.name, fp, fp.name) for fp in files]
    if not explicit_raw:
        for rel, cmi_name in DEV_CMI_CAPTURES:
            cap = REPO / rel
            if cap.is_file():
                sources.append((rel, cap, cmi_name))
            else:
                print(f"   Hinweis (Dev-Capture fehlt): {rel} (nur Default-Run; "
                      f"im Pi-Image discovery/ nicht vorhanden -> normal)")

    # ---- Quelle 1: CMI /LOG-Data-Dateien (P=169 70 Kanäle / P=181 73) ------
    cmi_by_ts: dict[str, dict[str, float | None]] = {}
    size_failures: list[str] = []
    print(f"{'datei':34} {'cmi-name':34} {'bytes':>8} {'rec':>6} {'rows':>8} "
          f"{'wide':>6} {'ts range'}")
    print("-" * 130)
    for label, fp, cmi_name in sources:
        raw = fp.read_bytes()
        if cmi_name in expected_sizes and len(raw) != expected_sizes[cmi_name]:
            size_failures.append(
                f"{cmi_name}: Größe {len(raw)} != erwartet "
                f"{expected_sizes[cmi_name]} (--expected-size)"
            )
        elif cmi_name in ROSETTA_25_SIZES and len(raw) != ROSETTA_25_SIZES[cmi_name]:
            print(f"  WARNUNG (Rosetta-25-Index, informativ): {cmi_name}: Größe "
                  f"{len(raw)} != CMI-Index {ROSETTA_25_SIZES[cmi_name]} "
                  f"(weiterarbeiten; harter Check: --expected-size)")
        sha = hashlib.sha256(raw).hexdigest()
        data = decode_cmi_log(raw, filename=cmi_name)
        data.name, data.size, data.sha256 = cmi_name, len(raw), sha
        wide = _wide_records(data.rows)
        if data.rows:
            ts0, ts1 = data.rows[0][0], data.rows[-1][0]
            ts_range = f"{ts0} .. {ts1}"
        else:
            # Zero-Record-Toleranz (Q-125): ein Post-Reboot-Fragment kann nur
            # den Header (oder den Header + einen abgeschnittenen Record)
            # tragen -> 0 dekoderte Records, kein Raise.  Der Backfill trägt
            # dann einfach keine Rows aus dieser Datei (die Datei bleibt
            # ingested-markiert, wird also nicht ewig neu geholt).
            ts_range = "(0 Records -- nur Header, Q-125 Zero-Record-Toleranz)"
        print(f"{label:34} {cmi_name:34} {len(raw):>8} {data.n_decoded:>6} "
              f"{len(data.rows):>8} {len(wide):>6}  {ts_range}")
        for ts, row in wide:
            cmi_by_ts.setdefault(ts, row)  # CMI-Dateien sind disjunkt; First-Wins
    cmi_ts_all = set(cmi_by_ts)
    if size_failures:
        print(f"\nABBRUCH (expected-size): {'; '.join(size_failures)}",
              file=sys.stderr)
        return 1

    # ---- Quelle 2: WinSol LogX-Gap-Datei (Rosetta-gleichwertig, 70 Kanäle) --
    # Dev-spezifisch: discovery/ ist .dockerignore'd, die Datei liegt im Pi-
    # Container nicht vor -> dann einfach ohne LogX-Gap-Rows weiterarbeiten.
    logx_path = REPO / LOGX_GAP_REL
    logx_records: list[Record] = []
    if logx_path.exists():
        buf = logx_path.read_bytes()
        sessions = find_sessions(buf)
        for sess in sessions:
            p = sess.variant.period
            for k in range(sess.n_records):
                logx_records.append(
                    decode_record(buf[sess.start + p * k: sess.start + p * (k + 1)],
                                  sess.variant, LOGX_GAP_DAY)
                )
        logx_wide = _logx_wide_rows(logx_records)
    else:
        sessions = []
        logx_wide: list[tuple[str, dict[str, float | None]]] = []
    logx_by_ts: dict[str, dict[str, float | None]] = {}
    for ts, row in logx_wide:
        logx_by_ts.setdefault(ts, row)

    # Gap-only = LogX-Rows vor dem ersten CMI-10-30-ts (die physisch verlorene
    # Periode); der Rest ist CMI-Überlappung (Kollision -> CMI gewinnt).
    day_cmi_ts = [t for t in cmi_ts_all if t[:10] == LOGX_GAP_DAY]
    first_day_cmi = min(day_cmi_ts) if day_cmi_ts else None
    gap_only = {ts for ts in logx_by_ts if first_day_cmi is None or ts < first_day_cmi}
    logx_added = {ts for ts in logx_by_ts if ts not in cmi_ts_all}
    collisions = cmi_ts_all & set(logx_by_ts)

    # ---- Merge: Month-Dict ts -> wide-Row; CMI-nativ gewinnt bei Kollision -
    merged: dict[str, dict[str, float | None]] = dict(logx_by_ts)
    merged.update(cmi_by_ts)
    months: dict[str, list[tuple[str, dict[str, float | None]]]] = {}
    for ts, row in merged.items():
        months.setdefault(ts[:7], []).append((ts, row))
    for recs in months.values():
        recs.sort(key=lambda r: r[0])

    source_months = set(months)
    if requested_months:
        for m in requested_months:
            if m not in source_months:
                print(f"   Hinweis: {m} wird von keinen Quelldateien abgedeckt "
                      f"(übersprungen)")
        active_months = [m for m in requested_months if m in source_months]
    else:
        active_months = sorted(source_months)

    # ---- Rosetta-Selbstcheck + Schema-Check (vor dem Write, auch Dry-Run) --
    problems, warnings = _logx_selfcheck(logx_wide, cmi_ts_all) if logx_wide else ([], [])
    schema_ok, schema_detail = _schema_check()

    print("-" * 130)
    if logx_wide:
        print(f"logx-gap: {LOGX_GAP_REL}")
        print(f"   P={sessions[0].variant.period} data_offset={sessions[0].start} "
              f"records={len(logx_records)} wide-rows={len(logx_wide)}")
        print(f"   ts {logx_wide[0][0]} .. {logx_wide[-1][0]}  "
              f"(Datum aus D-Dateinamen: {LOGX_GAP_DAY})")
        print(f"   gap-only (ts < {first_day_cmi}): {len(gap_only)} rows; "
              f"LogX-gesamt {len(logx_by_ts)}, davon {len(collisions)} ts-Kollisionen "
              f"(CMI gewinnt), {len(logx_added)} neue Rows für den Store")
        for w in warnings:
            print(f"   WARNUNG: {w}")
    else:
        print(f"logx-gap: {LOGX_GAP_REL} nicht vorhanden (Pi: normal -- die "
              f"Gap-Periode 2026-10-30 existiert nur im Dev-Repo; Backfill läuft "
              f"ohne LogX-Gap-Rows weiter)")
    print(f"   Selbstcheck (LogX-Gap-Quelle): {'OK (70 Kanäle besetzt, digital 0/1, ts monoton)' if not problems else 'FEHLGESCHLAGEN: ' + '; '.join(problems)}")
    print(f"   Schema-Check ({len(WIDE_COLUMNS)} Spalten = {WINSOL_REF} + dev2 a17/a18/a19): {schema_detail}")

    # ---- TS-Safety-Beleg (nur realer Lauf): Store-ts ⊆ Quelle ---------------
    if not args.dry_run:
        for month in active_months:
            mfile = Path(settings.cmi_log_parquet_dir) / month[:4] / month[5:7] / f"{month}.parquet"
            month_ts = {ts for ts, _ in months[month]}
            ok, detail = _guard_ts_union(mfile, month_ts)
            print(f"   TS-Safety {month}: {detail}")
            if not ok:
                print(f"\nABBRUCH (TS-Safety {month} fehlgeschlagen): {detail}",
                      file=sys.stderr)
                return 1

    for month in active_months:
        recs = months[month]
        n_cmi = sum(1 for ts, _ in recs if ts in cmi_ts_all)
        print(f"   erwartet Wide-Rows {month}: gesamt {len(recs)} "
              f"(CMI {n_cmi} + LogX {len(recs) - n_cmi})")
    cleanup = _count_cmi_log_rows(settings.history_db_path)
    print(f"   erwartet Cleanup-Rows (measurements source='cmi-log'): "
          f"{cleanup if cleanup is not None else 'unbekannt (kein DB-File -- dry-run erzeugt keins)'}")

    if args.dry_run:
        print("\n--dry-run: keine Parquet-/DB-Write.")
        # schema_ok=None (Referenzfile fehlt) ist im Dry-Run erlaubt;
        # ein echtes Write (unten) bleibt strikt fail-closed.
        return 0 if not problems and schema_ok is not False else 1

    if problems:
        print(f"\nABBRUCH (Selbstcheck fehlgeschlagen): {'; '.join(problems)}",
              file=sys.stderr)
        return 1
    if not schema_ok:
        print(f"\nABBRUCH (Schema-Check fehlgeschlagen): {schema_detail}",
              file=sys.stderr)
        return 1
    if not active_months:
        print("\nABBRUCH (nichts zu schreiben): --months-Monate werden von "
              "keinen Quelldateien abgedeckt", file=sys.stderr)
        return 1

    # ---- Echter Lauf: Parquet-Write (zstd) + DB-Cleanup ---------------------
    for month in active_months:
        recs = months[month]
        mfile = Path(settings.cmi_log_parquet_dir) / month[:4] / month[5:7] / f"{month}.parquet"
        before = pq.read_table(mfile, columns=["ts"]).num_rows if mfile.exists() else 0
        # S4N-LogFull: bestehende Rows tragen in den 25 neuen Spalten NULL und
        # werden von write_month (bestehende ts gewinnen) NIE aktualisiert ->
        # Monats-File löschen und den Monat voll über write_month neu schreiben
        # (TS-Safety oben hat den Datenverlust ausgeschlossen).
        mfile.unlink(missing_ok=True)
        added = store.write_month(month, recs)
        data = mfile.read_bytes()
        print(f"\nparquet: month={month} rows {before} -> {before + added} "
              f"(new {added}, ts-dedup) size {len(data)} B "
              f"sha256 {hashlib.sha256(data).hexdigest()}")

    # Cleanup: S4N-LogPoll long rows out of the DB (side table stays).
    db = get_history_db(settings.history_db_path)
    db.init_schema()
    deleted = db.cmi_log_cleanup()
    print(f"\nDB-Cleanup: {deleted} 'cmi-log'-rows aus measurements gelöscht "
          f"(cmi_log_ingested bleibt)")

    # Verifikation (real run only; --dry-run schreibt bewusst nichts).
    conn = db._ensure_conn()
    print("\n=== Verifikation ===")
    n_cmi = conn.execute(
        "SELECT count(*) FROM measurements WHERE source='cmi-log'"
    ).fetchone()[0]
    print(f"  measurements cmi-log: {n_cmi} rows (Ziel nach Backfill: 0)")
    ing = conn.execute(
        "SELECT name, size, ingested_at FROM cmi_log_ingested ORDER BY name"
    ).fetchall()
    print(f"  cmi_log_ingested: {len(ing)} Dateien (bleibt intakt)")
    for n in ing:
        print(f"     {n[0]:34} size={n[1]:>8} @ {n[2]}")
    db.close()

    dod_problems, dod_lines = _dod_check(Path(settings.cmi_log_parquet_dir), active_months)
    print("\n=== DoD: Null NULLs in allen Wertspalten (Q-125-Transitions-Spalten ausgenommen) ===")
    for line in dod_lines:
        print(line)
    if dod_problems:
        print(f"\nDoD FEHLGESCHLAGEN: {'; '.join(dod_problems)}")
        print("Hinweis: Q-125-Transitions-Spalten (dev2 a17-a19 für P=169-Rows; "
              "dev1 a1-a16 + dev2 a1-a4 + dev0/dev2-Digital für P=181-Rows) "
              "sind P-abhängig zulässigerweise NULL und werden NICHT gemeldet; "
              "ein NULL in einer ANDEREN Spalte = echter Datenverlust.")
        return 1
    print(f"\n  cmi_log_pq erwartet: {sum(len(r) for r in (months[m] for m in active_months))} "
          f"Wide-Rows gesamt (ts-dedupliziert), alle Wertspalten gefüllt "
          f"(Q-125-Transitions-Spalten P-abhängig NULL, siehe DoD oben)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
