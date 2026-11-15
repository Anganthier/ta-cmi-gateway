#!/usr/bin/env python3
"""Erster Live-Cycle für S4N-LogPoll (kontrolliert, OHNE clear.log).

Führt exakt den Ingest-Teil des Pollers manuell aus (Index → neue
data_*.log → Download → Decode → upsert source="cmi-log" → cmi_log_mark),
sendet aber KEIN clear.log (Clear-Strategie A: nur nach maintainer-Freigabe
+ Rowcount-vs-Index-Plausibilität). Nutzt die selben App-Funktionen wie
der Poller (CMILogClient, fetch_log_index, fetch_cmi_log_files,
decode via fetch_cmi_log_files) gegen den Live-CMI (192.168.0.100).

Verwendung:
    .venv/bin/python analysis/cmi_log_first_cycle.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.cmi.cmi_log import (  # noqa: E402
    CMILogClient,
    fetch_cmi_log_files,
    fetch_log_index,
)
from app.config import Settings  # noqa: E402
from app.db import get_history_db, now_iso  # noqa: E402


async def main() -> int:
    s = Settings()
    client = CMILogClient(s.cmi_base_url, s.cmi_user, s.cmi_pass)
    try:
        year = 2026
        files = await fetch_log_index(client, year)
        print(f"Index /LOG/info{year}.log: {len(files)} Datei(en)")
        for f in files:
            print(f"   {f.name}  {f.size} B  (date {f.date})")

        db = get_history_db(s.history_db_path)
        db.init_schema()
        ingested = db.cmi_log_ingested_names()
        print(f"cmi_log_ingested (vorher): {len(ingested)} Dateien")

        decoded, now_ingested = await fetch_cmi_log_files(
            client, s.cmi_data_dir / "cmi_log", files, ingested=ingested
        )
        print(f"neu heruntergeladen/gedekodet: {len(decoded)}")
        total_rows = 0
        for data in decoded:
            if data is None:
                print("   !! eine Datei wurde nicht dekodiert (best-effort-Slot)")
                continue
            inserted = db.upsert(data.rows, source_name="cmi-log")
            db.cmi_log_mark(data.name, data.size, data.sha256, now_iso())
            total_rows += len(data.rows)
            print(f"   {data.name}: {data.n_decoded} records, {len(data.rows)} rows "
                  f"({inserted} new)  {data.rows[0][0]} .. {data.rows[-1][0]}")
        db.close()
        print(f"\nGesamt neu: {total_rows} rows; cmi-log-Gesamt in DB: siehe DB-Check")
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
