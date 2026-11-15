# WinSol LogX — Binärformat (Reverse-Engineering)

Stand: 2026-10-27 · Subtask **S2-2** · Quellen: `discovery/winsol/` (Kopien),
Original read-only unter `/home/user/Documents/Technische-Alternative/Winsol/Infosol/example-plant/`.
Kreuzvalidierung: WinSol-CSV-Exporte (UTF-32LE, Tab, 93 Spalten) als Rosetta-Stein —
**100 % Abgleich** auf 2928 + 409 Zeilen (P=131), siehe `analysis/test_decode.py`.

> **Superseded (S2-Parquet-Pivot, human-approved):** Parquet ist jetzt die
> kanonische Form. Die SQLite-Ziel-DB `data/winsol_import.db` wurde nach
> Verifikation nach `data/winsol_pq/` (Hive-partitioniert, zstd) exportiert und
> gelöscht. `analysis/import_sqlite.py` bleibt nur als inkrementeller LogX→SQLite
> Pfad; der Export/der Rosetta-Check laufen über
> `analysis/winsol_to_parquet.py`.

## 1. Überblick

- **Gerät:** TA Infosol CMI-Logger (CMI-Logger), `Logger=164`, IP 192.168.0.100.
- **Anlage:** 3 Logger-Devices (`AnzDev=3` in den meisten Jahren):
  - `dev0` (1/…): UVR1611 Heizkreis, 16 analog + 13 digital (Typ 128)
  - `dev1` (2/…): UVR1611 RAS Raumtemperaturen, 16 analog + 13 digital (Typ 128)
  - `dev2` (3/…): UVR610 Solar/HzRl, 16 analog + 1 digital (Typ 145)
  - 2020/2021 teils nur 2 Devices (`AnzDev=2`).
- **Sampling:** eine Record pro **10 s** (8640 Records bei vollem Tag). hh/mm/ss steht
  **explizit** in der Record — es gibt kein Delta-Decoding. Fehlende 10-s-Samples
  (Rechner-Neustart, Sommerzeitwechsel) führen zu Tagen mit < 8640 Records.
- **Endianness:** Little-Endian. Analogwerte: u16 (P=137: teils u32), pro Kanal
  **skalierungsfaktor** (÷10 bzw. ÷100). Signierte u16 (s16) bei Temperaturen
  (negativ erlaubt, z. B. Außen-/Kollektortemperatur).

## 2. Dateistruktur

```
Offset 0..15      Magic "TA_WINSOL_LOG_FILE"
Offset 16..?      Header, größe varianten-/geräteabhängig
                   (160/180/188/192/194/197 B je P bzw. Logger-Stand)
Header 0x19       AnzDev (Anzahl der Logger-Devices)
Header 0xb2..0xb6 dev2-Kanalbreiten: 02 = u16, 04 = u32 (P=137)
find(">>>>")      Datenstart-Mark, FOLGENDE Byte = 0x44 ('D')
Danach:           n × P-Byte-Records, P = Perioden-Größe (variabel)
```

- **Datenstart:** `find(b">>>>") + 4` — dort liegt immer das erste `0x44`
  ('D'-Marker). Die Header-Länge davor variiert (160/180/184/188/192/194/197 B,
  je P bzw. Logger-Stand), deshalb darf der Datenstart **nicht** hartkodiert
  werden, sondern aus dem Mark abgeleitet werden. (In P=131/137-Files:
  `off = 197`.)
- **Mehrfach-Session pro Datei:** Wenn der Logger/WinSol während eines Tages
  neu gestartet/aktualisiert wird, enthält ein `D....log` **mehrere**
  `>>>>`-Header und damit mehrere Sessions, die ggf. **unterschiedliche Perioden**
  haben (z. B. `2022/D2022-03-07.log` = P=122 vormittags + P=128 nachmittags).
  `find_sessions()` teilt die Datei am Mark auf und dekodiert jede Session
  eigenständig.
- **Perioden P:** `P × n_records` = (Session-Größe − Tail); ein abgerissener
  Tages-Ende (Tail < P) wird toleriert, gezaehlt werden nur komplette Records.
  Bekannte Werte: **98, 104, 106, 116, 118, 122, 128, 131, 137**.
- **Erkennung:** P-Kandidaten = Teiler der Session-Größe ∪ bekannte P mit
  `2·P ≤ Größe`; bester `0x44`-Marker-Score (≥ 90 % der Record-Starts tragen
  Marker + plausibles hh/mm/ss) gewinnt, bekannte P als Tie-Breaker.
  **Alias-Abwehr:** P'=k·P (k≥2) ist ebenfalls Teiler und trifft jeden k-ten
  Record (z. B. 232 = 2·116, 212/232/236 gegen 116) — die echte Periode ist
  der kleinste Kandidat mit (nahezu) bestem Score.
- **Falsche `>>>>`-Marker:** In Record-Daten vorkommende `3e 3e 3e 3e`-Sequenzen
  sehen aus wie der Datenstart-Mark. Sie sind nur dann eine Session-Grenze,
  wenn der folgende Block ≥ 32 komplette Records liefert
  (`MIN_SESSION_RECORDS`; echte Sessions haben ≥ 69 Records). In 51/2227
  Files kommen solche Marker vor; ohne die Guard erzeugen sie 1-Record-
  Pseudo-Sessions mit Score 1.0 und verschieben den Rest des Files.

- **Varianten-Tabelle** (dev0/dev1 sind in allen identisch, vgl. §4):

| P   | AnzDev | dev2-Block                          | Vorkommen                    |
|-----|--------|-------------------------------------|------------------------------|
| 98  | 2      | (keine)                             | 2021 (7 Files)               |
| 104 | 2      | (keine)                             | 2021 (2 Files)               |
| 106 | 3      | a1..a3 (u16 ÷10) + a4 (Konstante 2) | 2021 (59 Files)              |
| 116 | 2      | (keine)                             | 2020 (124), 2021 (289)       |
| 118 | 3      | a1..a9 (u16 ÷10)                    | 2022 (22 Files)              |
| 122 | 3      | a1..a12 (a10..a12 u16 ×100)         | 2022 (Session in 03-07)      |
| 128 | 3      | a1..a15, a10..a12 u16 ×100          | 2022 (16 Files)              |
| 131 | 3      | a1..a16 + 1 B digital               | 2022(03/31)–2026(02/16)      |
| 137 | 3      | a1..a9, **a10..a12 u32 ×100**, a13..a16 + 1 B | ab 2026 (~02/17) |

P=137 = P=131 + 6 Bytes: dev2 a10/a11/a12 wurden von u16→u32 verbreitert
(Header-Byte 0xb2/0xb4/0xb6: `02→04`), Skala ×100 bleibt (0…1000 = 0…10.00 %).

## 3. Record-Layout (Basis = 'D'-Marker, 0x44)

```
+0    1 B   0x44 'D'-Marker
+1..+3  3 B hh / mm / ss   (explizit, kein Delta)
```

### dev0 — UVR1611 Heizkreis (alle Varianten)

| Offs | Typ | Skalierung | Kanal (Setup.xml / CSV) |
|------|-----|-----------|--------------------------|
| +4 .. +22 | s16 ×10 | ÷10 | a1..a10: Speicher Oben/Mitte/Unten, Warmwasser, Oelkessel-Vorlauf, Heizkreis-Vorlauf/-Rücklauf, Temp Aussen, Holzkessel-Vorlauf, Abgastemp Holz |
| +24 .. +42 | s16 ×10 | ÷10 | a11..a15: `-----` (unbelegt) + „Test“ — **beobachtet: immer 0** |
| +44 | u16 | — | **a16 Stroemungsschalter, 0/1-Flag** |
| +48 | u16 | — | **digital-Maske, permutiert** (s. u.) |

**dev0-digital Permutation** (aus CSV gelöst, 2023-01-26 verifiziert):
`bit0←D8, bit1←D7, bit2←D6, bit3←D5, bit4←D4, bit5←D3, bit6←D2, bit15←D9`;
bit7..bit14 nie belegt, D1 und D10..D13 kommen nicht vor.
Kanalnamen: D2 „Pumpe WW zum Puffer“, D3 „Mischkreis Auf“, D4 „Mischkreis Zu“,
D5 „Anforderung Brenner“, D6 „Pumpe WW Zirkulation“, D7 „Zirkulationspumpe“,
D8 „Pumpe Heizkreis“, D9 „Ventil Heizungsrücklauf“.

### dev1 — UVR1611 RAS (alle Varianten)

| Offs | Typ | Skalierung | Kanal |
|------|-----|-----------|-------|
| +50, +52 | s16 | ÷10 | a1 Offset Keller, a2 Temp Keller |
| +54 | s16 | **÷1 (raw)** | a3 „ANALOG 3“ — **kein** 10-facher Wert! |
| +58, +60 | s16 | ÷10 | a4 Offset EG, a5 Temp EG |
| +62 | s16 | **÷1 (raw)** | a6 „ANALOG 6“ |
| +66, +68 | s16 | ÷10 | a7 Offset DG, a8 Temp DG |
| +70 | s16 | **÷1 (raw)** | a9 „ANALOG 9“ |
| +72..+76 | s16 | ÷1 | a10 (raw, beobachtet: 0, Füllslot) |
| +78, +80, +82 | s16 | ÷10 | a11 Keller-Minuten, a12 EG-Minuten, a13 DG-Minuten |
| +84..+88 | s16 | ÷1 | a14..a16 (raw, beobachtet: 0, Füllslots) |
| +90 | u16 | — | dev1 digital (plain Bit-Order; beobachtet: immer 0) |

Die „raw“-Kanäle a3/a6/a9 sind die im CSV als `ANALOG 3/6/9` geführten
unbenannten Kanäle; WinSol loggt dort s16-Originalwerte (z. B. 476 = 47.6,
nicht ×10). Das erklärt die frühere „10-fach-Täuschung“: nur a1/a2/a4/a5/
a7/a8/a11/a12/a13 sind ×10.

### dev2 — UVR610 Solar/HzRl (Varianten je P)

P=131 (CSV-validiert):

| Offs | Typ | Skalierung | Kanal |
|------|-----|-----------|-------|
| +98 .. +114 | s16 ×10 | ÷10 | a1..a9: T. Speicher oben/mitte/unten, Keller RAS/Temp, EG RAS/Temp, DG RAS/Temp |
| +116, +118, +120 | u16 | **÷100** | a10/a11/a12: „Ventil 2 Ergebnis“ Keller/EG/OG (0…10.00 %) |
| +122 .. +126 | s16 | ÷10 | a13/a14/a15: „Ventil 1“ Keller/OG/EG |
| +128 | s16 | ÷10 | a16 T. Kollektor (negativ ok) |
| +130 | 1 B | — | digital (beobachtet: immer 0) |

P=137: a1..a9 wie oben; **a10/a11/a12 u32 (÷100)** bei +116/+120/+124;
a13/a14/a15 bei +128/+130/+132; a16 bei +134; digital-Byte bei +136
(`0x80` = Solarpumpe aktiv — korreliert exakt mit den Betriebsintervallen
des Solarkreises im Tagesverlauf).

P=128: a1..a9 +10..14 (÷10), a10..a15 analog P=131 aber u16 (÷100 / ÷10), kein a16-Digital-Byte.
P=122: wie P=128, aber nur a1..a12 (kein a13..a16, kein digital-Byte) — 9 B kürzer.
P=118: nur a1..a9. P=106: a1..a3 + a4 (Konstante 2). P=116/98/104: kein dev2.

## 4. CSV-Rosetta-Stein (Fehlerkorridor)

`csv/` (WinSol-Export) nur für **2022 und 2023** vorhanden:

- `AE2023-01-26_080759.csv` — enthält 2023-01-23 (5542 Z.), 24/25 (je 8640 Z.),
  **2023-01-26 (2928 Z. bis 08:07)**; UTF-32LE (BOM `FF FE 00 00`), Tab-separiert, 93 Spalten.
- `AE2022-03-31_082509.csv` — **2022-03-31 (409 Z., Teiltag)**.
- Spalten (0-basiert): 0 Datum, 1 Uhrzeit, 2..16 dev0 a1..a15, 17 dev0 a16,
  18..21 dev0 Drz (leer), 22..25 dev0 kW/kWh (leer), 26..38 dev0 Dig1..13,
  39..54 dev1 a1..a16, 55..62 Drz/kW (leer), 63..75 dev1 Dig1..13,
  76..91 dev2 a1..a16, 92 dev2 Dig1.
- Drz/kW/kWh-Spalten sind in beiden CSVs **leer** (keine Energiezähler-Werte
  im Bestand) → im DB-Schema nicht vorhanden.

## 5. Verifikation (Rosetta-Stein)

| Check | Basis | Ergebnis |
|-------|-------|----------|
| P=131, 2023-01-26 (2928 Z.) | hh/mm/ss + alle 32 Analogkanäle + alle 14 Digitalkanäle exakt | **100 %** |
| P=131, 2022-03-31 (409 Z.) | gg. | **100 %** |
| P=137, 2026-03-01 (8640 Rec.) | strukturell: Uhrzeit-Monotonie (3 echte 8/11/12-s-Lücken), Temperaturplausibilität, u32-Ventile ∈ {0,1000}, digital ∈ {0,0x80} | ok |
| P=122+P=128, 2022-03-07 (8637 Rec., 2 Sessions) | Multi-Session-Split, Uhrzeit-Monotonie, Plausibilität (Vorlauf 34,9–66,2 °C), dev2-Kanalabgrenzung (a13 nur ab P=128) | ok |
| P=116, 2020-08-30 (4103 Rec.) | Import + Plausibilität (dev0/dev1) | ok |

Die bekannten WinSol-CSV-Werte („Stichproben“) stimmen also exakt mit dem
dekodierten Binär-Content überein (S2-2 DoD).

## 6. Artefakte

| Datei | Zweck |
|-------|-------|
| `analysis/winsol_logx.py` | Dekodierer (Varianten-Erkennung, `iter_records`, CSV-Reader, CLI) |
| `analysis/test_decode.py` | 15 Tests gegen CSV-Rosetta-Stein (standalone + pytest) |
| `tests/test_winsol_logx.py` | Repo-Pytest-Brücke zu den obigen Tests |
| `analysis/import_sqlite.py` | Import → `data/winsol_import.db` (idempotent, Wide-Schema), `--verify-csv`-Rosetta-Check |
| `discovery/winsol/*.py` | Discovery-Scripts (survey, canon2, p137tail, …) |

Aufrufe:

```bash
# Dekodieren (10 Records)
python3 analysis/winsol_logx.py D2023-01-26.log -n 5 --json

# Tests
python3 analysis/test_decode.py
pytest tests/test_winsol_logx.py

# Import + Rosetta-Check
python3 analysis/import_sqlite.py discovery/winsol/D2023-01-26.log
python3 analysis/import_sqlite.py --verify-csv AE2023-01-26_080759.csv D2023-01-26.log
python3 analysis/import_sqlite.py --year 2023 --limit 2
```

## 7. Offene Punkte / Risiken

- **P=98/104/106/116/118/122/128 ohne CSV-Grundwahrheit**: Layout strukturell
  abgeleitet (dev0/dev1-Block identisch, dev2-Regionen per Nicht-Null-Analyse
  + Plausibilitätsspannen). Vor dem Vollimport: Stichprobe mit Live-Werten
  (Schematic-CGI) abgleichen — S2-2 DoD.
- **dev2 a4 bei P=106**: Konstante 2 (120…200) über den ganzen Tag —
  Kanal-ID unklar; wird als `unknown` importiert (Skalierung ÷10).
- **Füllslots dev1 a10/a14..a16**: im Bestand immer 0; falls ein Tag andere
  Werte zeigt, wäre das ein Hinweis auf erweiterte RAS-Nutzung.
- **Truncated Files** (z. B. heute, partiell): Importer nutzt explizites
  hh/mm/ss und verwirft keinen Tag wegen <8640 Records; `files.n_records`
  erlaubt Nachprüfbarkeit. Abgerissener Session-Tail (< P Bytes) wird
  verworfen, komplette Records werden importiert.
- **Header-Bedeutung**: nur `AnzDev` (0x19), `>>>>`-Mark und dev2-Breiten
  (0xb2..0xb6) werden genutzt; übrige Header-Byte (Profile, Skal-Faktoren,
  Zeitstempel) sind nicht dekodiert — für den Import nicht nötig.
- **WinSol-Binary** (`/usr/share/Technische-Alternative/Winsol/Winsol`,
  Symbole `ReadNextDataFrame`/`m_FileFrameSize`): Dekompilierung *nicht*
  nötig, da CSV-Abgleich 100 % liefert. Bleibt als Fallback für die
  unbestätigten P=98/104-Varianten.
