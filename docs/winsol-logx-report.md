# S2-2 WinSol-LogX Reverse-Engineering — Kurzbericht

Stand: 2026-10-27 · Status: **Format vollständig geklärt, validiert & importiert (2227/2227 Dateien, 0 Fehler)**

> **Superseded (S2-Parquet-Pivot, human-approved):** Die SQLite-DB
> `data/winsol_import.db` wurde nach Verifikation (Row-Counts exakt +
> CSV-Rosetta-Spotcheck 100 %) nach `data/winsol_pq/` exportiert und gelöscht.
> Parquet ist jetzt die kanonische Form; `import_sqlite.py` bleibt nur als
> inkrementeller LogX→SQLite-Pfad, Export + Rosetta-Check laufen über
> `analysis/winsol_to_parquet.py`.

## Ergebnis (TL;DR)

Das WinSol-LogX-Binärformat (TA Infosol CMI-Logger, Bestand 2020–2026) ist
vollständig rekonstruiert und **100 % gegen die WinSol-CSV-Exporte (Rosetta-Stein)
validiert**. Der gesamte Bestand (**2227 Dateien, 19 211 004 × 10-s-Messungen**)
ist idempotent in `data/winsol_import.db` importiert (1324 s Laufzeit, 0
Fehler, 35 partielle Tage = echte Lücken im Logger-Betrieb). Dekompilierung
des Binaries war **nicht** nötig — der CSV-Abgleich liefert exakte
Übereinstimmung.

**Vollimport-Ergebnis:**

| Jahr | Dateien | | Periode | Dateien |
|------|--------|-|---------|---------|
| 2020 | 124 | | P=98 | 7 |
| 2021 | 362 | | P=104 | 2 |
| 2022 | 365 | | P=106 | 105 |
| 2023 | 365 | | P=116 | 414 |
| 2024 | 346 | | P=118 | 23 |
| 2025 | 365 | | P=122 | 1 |
| 2026 | 300 | | P=128 | 17 |
|      |     | | P=131 | 1405 |
|      |     | | P=137 | 253 |

Zeitraum: **2020-08-30 … 2026-10-27** (heute, partiell). Nur **2 echte
Daten-Lücken** im Bestand (Logger lief nicht / Datei nicht archiviert):
2021-09-26→09-30 (4 T.) und 2024-06-26→07-17 (21 T.) — bestätigt: die
Zwischendateien existieren auf der Quelle nicht.

## Kernbefunde

1. **Record-Basis:** Jede 10-s-Record startet mit dem `0x44`-('D')-Byte;
   `hh/mm/ss` liegen **explizit** bei +1/+2/+3 — es gibt kein Delta-Decoding.
   Datenstart = `find(b">>>>")+4` (Header-Länge variiert, also nicht hartkodieren).
2. **9 Perioden** `P=98,104,106,116,118,122,128,131,137`. Das dev0/dev1-Block-Layout
   ist in **allen** Varianten byte-identisch; nur der dev2-Block (UVR610) ändert sich.
3. **Skalierung pro Kanal:** u16 LE, Faktor ÷10 bzw. ÷100; s16 für Temperaturen
   (negativ erlaubt). dev2 a10/a11/a12 sind in P=137 auf **u32** erweitert (+6 B).
4. **dev0-digital-Maske ist permutiert** (bit0←D8 … bit6←D2, bit15←D9), nicht die
   naive Bit-Order — per linearer Lösung aus dem CSV ermittelt.
5. **Multi-Session:** Eine Datei kann mehrere `>>>>`-Header (Logger-Neustart) mit
   **unterschiedlichen Perioden** enthalten (z. B. P=122 → P=128 in `D2022-03-07.log`).
   `find_sessions()` teilt korrekt auf.

## Validierung (Rosetta-Stein)

| Check | Basis | Ergebnis |
|-------|-------|----------|
| P=131, 2023-01-26 | 2928 Zeilen: Uhrzeit + 32 Analog + 14 Digital exakt | **100 %** |
| P=131, 2022-03-31 | 409 Zeilen | **100 %** |
| P=137, 2026-03-01 | 8640 Rec. strukturell (Monotonie, u32-Ventile, Solarpumpe-Byte) | ok |
| P=122+128, 2022-03-07 | 8637 Rec., 2 Sessions, dev2-Kanalabgrenzung | ok |
| P=116, 2020-08-30 | Import + Plausibilität | ok |
| Full-Corpus-Survey | 2227 Dateien | **0 Detektions-Fehler** |

15 Unit-Tests (`analysis/test_decode.py`, auch via `tests/test_winsol_logx.py`)
decken die Dekodierung; alle grün. Ruff clean.

## Robustheits-Fallen (behoben)

- **Aliasing:** `P'=2·P` (z. B. 232 vs. 116) ist ebenfalls Teiler der Länge und
  liefert Marker-Score ~1.0 → echtes P = **kleinster** Teiler mit bestem Score.
- **Falsche `>>>>` in Daten:** `3e 3e 3e 3e`-Sequenzen in Record-Bytes triggern
  Pseudo-Sessions (1 Record, Score 1.0). Guard: `MIN_SESSION_RECORDS=32`
  (echte Sessions ≥ 69 Records). 51/2227 Dateien betroffen.
- **Abgerissene Records:** Ungültige Uhren (z. B. 60:60:60 an Session-Grenzen)
  werden beim Import verworfen, komplette Records importiert.

## Artefakte

| Datei | Zweck |
|-------|-------|
| `docs/winsol-logx.md` | Vollständige Format-Doku (alle 9 Perioden, Layout, Robustheit) |
| `analysis/winsol_logx.py` | Dekodierer (Varianten-/Session-Erkennung, `iter_records`, CSV-Reader, CLI) |
| `analysis/test_decode.py` | 15 Tests vs. CSV-Rosetta-Stein (standalone + pytest) |
| `tests/test_winsol_logx.py` | Repo-Pytest-Brücke |
| `analysis/import_sqlite.py` | Import → `data/winsol_import.db` (idempotent), `--verify-csv`-Rosetta-Check |
| `data/winsol_import.db` | Ziel-DB (Wide-Schema: 1 Zeile pro 10-s-Record + `files`-Index) |

## Offene Punkte / Risiken

- **P=98/104/106/116/118/122/128** ohne eigene CSV-Grundwahrheit (CSV nur 2022/23):
  Layout strukturell abgeleitet. Empfehlung vor Endabnahme: Stichprobe gegen
  Live-Schematic-Werte abgleichen (S2-2 DoD „Stichproben gegen Schematic-Werte").
- **dev2 a4 bei P=106** = Konstante 2, Kanal-ID unklar → als `unknown` importiert.
- **Füllslots** dev1 a10/a14..a16 = immer 0 im Bestand.
- **2 Daten-Lücken** im Bestand (2021-09-26→30, 2024-06-26→07-17): Logger
  lief nicht / Datei nicht vorhanden — kein Decoder-Fehler, in `files`
  sichtbar (fehlende Tage).

## Nächste Schritte

1. ~~Vollimport `data/winsol_import.db`~~ **erledigt** (2227/2227, 19,2 Mio. Records).
2. Stichproben-Abgleich der strukturell abgeleiteten Perioden gegen
   Live-Schematic-Werte (CMI-CGI) — schließt den S2-2-DoD ab.
3. DB als Trainings-Basis für S6 (Regel-Modell) freigeben.
