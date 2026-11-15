# CMI Internes Logging — /LOG-Protokoll (Reverse-Engineering)

Stand: 2026-11-08 · Sprint-Tickets **S4N-LogPoll** / **S4N-LogFlush** /
**S4N-LogFull** / **Q-125** · Quellen:
`discovery/cmi-log/` (Ground-Truth: `cmi_raw_2026-10-{27,28,29}.log`,
`logx_*.log`, `info2026.log`, `infoh.log`, `clear.log` — tcpdump 2026-10-29,
Capture `/tmp/opencode/cmi-capture/`), Kreuzvalidierung (Rosetta) gegen
`analysis/winsol_logx.py` (selbst 100 % gegen WinSol-CSV validiert).
Implementierung: `app/cmi/cmi_log.py` (Decoder + Fetch-Schicht),
`app/main.py` (`_cmi_log_poller`), Tests: `tests/test_cmi_log.py`.
**S4N-LogFull (Q-112b, 2026-11-01):** Der P=169-Decoder deckt alle
**70 Wertkanäle** des Kanon-Schemas ab (48 analog + 22 digital, Kap. 3.1/3.2);
die 25 zuvor fehlenden Kanäle sind per Rosetta-25 belegt (Kap. 4.3).
Reproduktion: `scripts/rosetta_full.py`, Backfill:
`analysis/cmi_log_backfill.py`.

**Q-125 (P=181, 2026-11-07 ~16:14):** Am 2026-11-07 ~16:14 (nach dem
UVR-Programm-Upload) hat das CMI das /LOG-Record-Format von **P = 169** auf
**P = 181** Bytes umgestellt — ein *restrukturiertes* Record (nicht ein
uniformer +12 B-Versatz): der Header wuchs 75 → 78 Blöcke, die Device-Blöcke
wurden umgelagert und die Wert-Slots liegen jetzt auf einer **Mischung aus
geraden UND ungeraden** Byte-Offsets (dev0 a1–a10 ungerade @21–39, dev2
a5–a19 gerade @114–170; NICHT `2*slot` wie bei P=169). Die Time-Bytes
wanderten von den tiefen P=169-Offsets (ss@106/mm@107/hh@108) direkt hinter
das 3-Byte-Sync-Präfix (ss@3/mm@4/hh@5), am Record-Ende steht ein CRLF
(Byte 179/180). Dazu kommen **3 neue Minderer-Sollspannungs-Kanäle**
(dev2 a17–a19) (51 analog + 22 digital = 73 Wertkanäle pro Record). Der
Decoder ist jetzt **P-agnostisch** (Magic = 8 Hex + CRLF statt konstanter
Magic; P wird pro File per Time-Grid-Scan + Header-Kanalzähler u16@10
auto-erkannt, Kap. 3.0) und der P=181-Data-Start wird **layout-agnostisch**
per Sync-Scan (`0B 1A`-Tail + CRLF + 10 s-Grid) gefunden, NIE hartkodiert
(Kap. 3.0). Die P=181-Wert-Slots sind per Rosetta gegen die zeitlich
überlappenden JSON-Referenzen (json:1 = dev0, json:32 = dev2; 2784 Records
11-07, n=214/215 pro Kanal) **und** die zeitlich überlappende WinSol P=145-
GT (Q-125 Round 5: dev1 RAS + dev0 digital + dev2 d1) wertverifiziert
(Kap. 4.4). **Q-125 Round 3 (2026-11-08):** die dev0-Kanäle a11–a16 sind per
Full-Tag-Pattern + Header-Label (UTF-16LE) + Heizkurven-Physik auf den
ungeraden Bytes **41/43/45/47/49/51** belegt (a11 = VL.Solltemp °C,
a12 = T.raum.SOLL.eff °C {16, 20}, a13–a15 = „-----" 0-Fill,
a16 = Ström.Schalt5 0/1-Flag). **Q-125 Round 5 (2026-11-08):** dev1 a1–a16
(RAS) @ **gerade 66–96** + dev0 digital d2–d9 @ **byte 6/10–16** + dev2 d1
(Solarpumpe) @ **byte 174** sind per WinSol P=145-GT (100 % +
Toggle-Konsistenz, beide P=181-Files) belegt; **dev2 a1–a4 = entfallen**
(R2-Duplikat-Hack entfernt) → **PENDING-Rest = nur dev2 a1–a4 (4 NULLs pro
Record)**. Der Parquet-Store wuchs 71 → 74 Spalten (+dev2_analog_17/18/19,
Kap. 7).

## 1. Überblick

- **Gerät:** TA Infosol CMI, 192.168.0.100, HTTP Port 80.
- **Pfad:** Das CMI führt ein **eigenes internes Logging**, unabhängig von der
  WinSol-App (LogX, Port 40000). Alle **10 s** wird eine Record der drei
  CAN-Devices (`dev0` = UVR1611 Heizkreis, `dev1` = UVR1611 RAS,
  `dev2` = UVR610 Solar) in eine Tagesdatei unter `/LOG/<JJJJ>/` angehängt.
- **Auth:** Die bestehenden `CMI_USER`/`CMI_PASS` (HTTP Basic) funktionieren
  auf `/LOG/*` (HTTP 200 getestet, 2026-10-29, Lese-Test).
- **Rate-Limit:** Keines (3 aufeinanderfolgende Fetchs ohne Status 4,
  im Gegensatz zum 1/min-Limit von `/INCLUDE/api.cgi`). Der Gateway nutzt
  daher einen **eigenen httpx-Client** (`CMILogClient`) und geht **nicht**
  durch die `CMIGuard`-Serial-Queue.
- **Speicher:** Begrenzter Buffer (`storekrit: 10` im Index-Kopf = ~10
  Tages-Dateien). **Das CMI löscht nie selbst** — ist der SD-Buffer voll,
  hört es einfach auf zu loggen (sichtbar: ts-Abbruch/Lücke im
  cmi-log-Parquet-Store, Kap. 7). Die einzige Freigabe ist
  `clear.log` (state-mutating, Kap. 2). **Achtung:** ein `clear.log` löscht
  ALLE Blöcke im Buffer — **einschließlich der aktiven Block-Datei, die (noch)
  nicht im Index gelistet ist** — und startet einen neuen aktiven Block. Ein
  WinSol-Fetch OHNE Delete schließt den aktiven Block dagegen sauber: er
  erscheint im Index und bleibt im Buffer (live belegt:
  `data_2026_10_30_19_51_56.log` im Index seit 2026-10-30 20:39). Der
  Gateway-Poller ist daher **standardmäßig read-only**
  (`CMI_LOG_CLEAR=false`, Kap. 5) und sendet nie `clear.log`; die
  Buffer-Freigabe ist eine manuelle WinSol-Fetch-mit-Delete-Entscheidung
  (Benutzer-Verantwortung, Kap. 6).
- **RAM-Buffer-Flush (bewiesen 2026-10-31):** Das interne /LOG-Logging
  (10-s-Sampling) liegt zwischen den Flushes in einem **RAM-Buffer** im CMI.
  Der Flush auf Disk (aktive `data_*.log` + Index-Größe) erfolgt **nur** beim
  Read von `GET /LOG/infoh.log` (WinSol „Logger auslesen" macht genau diesen
  Read als First-Read); `GET /LOG/info<JJJJ>.log` (Index) und
  `GET /LOG/<JJJJ>/data_*.log` (Daten) triggern **keinen** Flush — der
  User-Agent ist irrelevant (Kontroll-Experiment: 3× data-GET + Index-Read
  ohne Effekt, nur der infoh-Read flushte +36 Records = 360 s bei
  10-s-Sampling). Beleg: 51-min-Daten-Lücke am 2026-10-31 zwischen den
  manuellen WinSol-Abgriffen (CIR-Log 09:22–09:38). Konsequenz: der
  Gateway-Poller beginnt jeden Zyklus mit dem infoh-Read (read-only, ~6 kB,
  rate-unlimited → Daten-Latenz ≤ Poll-Intervall, Kap. 5); ohne Flush bleibt
  der Poller flush-begrenzt (nur geflushte Daten sichtbar).

## 2. Endpunkte

| Methode / Pfad | Funktion | Body |
|---|---|---|
| `GET /LOG/info<JJJJ>.log` | Jahres-Index: alle `data_*.log` mit Pfad + Bytegröße | `CMI\r\n` + Revisionskopf + `storekrit: 10\r\n` + je Datei `/LOG/<JJJJ>/data_<JJJJ>_<MM>_<DD>_<HH>_<MM>_<SS>.log <Byte>\r\n` |
| `GET /LOG/<JJJJ>/data_<JJJJ>_<MM>_<DD>_<HH>_<MM>_<SS>.log` | Natives CMI-Log-Binary (Tagesdatei) | Binär (Kap. 3) |
| `GET /LOG/clear.log` | **Leert den internen Logging-Speicher** (einziger state-mutating Call) — **inkl. des aktiven Blockes, der noch nicht im Index steht** (Datenverlust-Risiko, Kap. 1) | `OK` |
| `GET /LOG/infoh.log` | Binäre Kanal-Bezeichnungs-Index (UTF-16LE-Blöcke, endet mit Jahreszahl `2026\r\n`) — **nicht** der Daten-Index — **UND der einzige Flush-Trigger des internen RAM-Buffers** (Kap. 1, Kap. 5.1) | Binär |

Index-Dateiformat (real, `info2026.log`):

```
CMI\r\n
1.47 1.06 2.00 Revision: 1803\r\n
storekrit: 10\r\n
\0\0\0\0\r\n
\r\n
/LOG/2026/data_2026_10_29_20_20_16.log 6863\r\n
```

**Achtung:** Das Jahr steht 4-stellig in **Pfad UND Dateiname**
(`/LOG/2026/data_2026_...log`); die CMI-Intern-Doku notiert den
Tages-Slot als `TT` — das ist eine gewöhnliche 2-stellige Tageszahl.

## 3. Binärlayout der Daten-Datei

### 3.0 P-Detektion (Q-125): Magic + Perioden-Grid

Das CMI /LOG ist seit Q-125 **P-agnostisch**: der Decoder kennt die zwei
bekannten Record-Perioden und erkennt P pro File:

| P | Wertkanäle | Header / Data-Start | Status |
|---|---|---|---|
| **169** | 70 (48 analog + 22 digital) | 75 Blöcke (Ende 6014), Raster @5915 | Legacy (bis 2026-11-07 ~16:14) |
| **181** | 73 (51 analog + 22 digital) | 78 Blöcke (Ende 6254), Data-Start @6258 | Seit 2026-11-07 ~16:14 (UVR-Programm-Upload) |

- **Magic = `8 Hex + CRLF`** (nicht konstant): die 8 Hex-Ziffern sind eine
  per-File-Header-CRC, kein konstanter Wert. Allein im P=169-Material sind
  vier Werte belegt (`8D312121`, `86147834`, `A8024640`, `8204D130`); dieselbe
  Menge öffnet P=181-Files. Der frühere hartkodierte `8D312121`-Check war
  von Haus aus fragil — die P=169→P=181-Umstellung hat ihn lediglich
  sichtbar gemacht. Der Decoder prüft jetzt `^[0-9A-Fa-f]{8}\r\n`.
- **P-Erkennung:** (a) **Header-Kanalzähler** u16LE @ Offset 10 (nach Magic)
  als schneller Hinweis: `48` → P=169, `51` → P=181 (Zahl der enumerierten
  analogen Descriptions-Blöcke). (b) **Time-Grid-Scan (autoritativ):** auf dem
  Kandidaten-P-Grid muss ein gültiges, monoton steigendes `hh:mm:ss`-Fenster
  (+9..+11 s) liegen; die Time-Bytes sitzen P-abhängig an unterschiedlichen
  Offsets (P=169: ss@106/mm@107/hh@108, P=181: ss@3/mm@4/hh@5), daher
  scheitert der falsche P-Grid-Scan. Proben (Q-125): P=169-Grid verwirft
  P=181-Files und umgekehrt (jeweils `None` → kein Valid-Fenster). P wird
  newest-first (181, dann 169) gesucht, so dass ein P=181-File nie als P=169
  erkannt wird.
- **P=181-Data-Start (Q-125 Round 2):** Das P=169-Record-Raster liegt auf
  einer 169-Grid (Multipel von 169 ab Byte 0). Das P=181-Record-Format ist
  **restrukturiert**: Data-Start = **6258** = 78×80 B Header-Blöcke + 2×CRLF,
  was **KEIN** Multipel von 181 ist (6258 = 34×181 + 84). Der P-Grid-Scan
  findet daher nichts; stattdessen nutzt der Decoder einen **layout-
  agnostischen Sync-Scan** (`_detect_data_start_181`): Suchen des konstanten
  3-Byte-Sync-Tails `0B 1A` (der vorangehende Sync-Byte variiert 07/08),
  dann Bestätigung, dass auf dem 181-Stride ein gültiges +10 s
  `hh:mm:ss`-Fenster mit CRLF-Ende @179/180 liegt. Die erste qualifizierte
  Offset-Position wird als Data-Start gewählt. **Hartkodiert wird NIE**
  6258 — ein anderer Header würde trotzdem gefunden werden.
- **Zero-Record-Toleranz (Q-125):** ein Post-Reboot-Fragment, der nur den
  Header (oder Header + abgebrochenen Record) trägt und **kein** gültiges
  +10-s-Fenster hat, wird zu **0 Records** dekodiert (kein `ValueError`); der
  Poller markiert es trotzdem ingested (wird also nicht ewig neu geholt).

```
P = 169 (legacy):
  Offset 0..9       ASCII-Magic  "8D312121\r\n"
  14..2334          29 analoge  Bezeichnungs-Blöcke  "43 01 01 80 ..." (80 B)
  2334..4654        29 digitale Bezeichnungs-Blöcke  "43 02 01 80 ..." (80 B)
  4654..6014        17 Geräte-Label-Blöcke           "43 00 20 91 ..." (80 B)
                    ---- Header-Ende (6014) ----
  5915..            Record-Raster, Perioden P = 169 B, eine Record = 10 s-Sample
                    (Time ss@106/mm@107/hh@108; u16-Slots an geraden Offsets 2N)

P = 181 (Q-125 Round 2, 2026-11-07 ~16:14):
  Offset 0..9       ASCII-Magic  (8 Hex + CRLF, per-File-CRC)
  10..11            Header-Kanalzähler u16LE = 51 (anal. Descriptions)
  14..6254          29 analog + 29 digital + 20 Label-Blöcke (80 B) = 78 Blöcke
                    ---- Header-Ende (6254) ----
  6258..            Record-Raster, Perioden P = 181 B (Data-Start = 6258,
                    NICHT auf einer 181-Grid; layout-agnostisch per
                    Sync-Scan `0B 1A` + CRLF + 10 s-Grid gefunden, Kap. 3.0)
                    (Sync XX 0B 1A @0-2; Time ss@3/mm@4/hh@5; Wert-Slots an
                     geraden UND ungeraden Offsets; CRLF @179/180)
```

- **Record (P = 169 B):**
  - Byte 106 = Sekunden, 107 = Minuten, 108 = Stunden (explizit, kein Delta).
  - Jeder **gerade** Byte-Offset `2N` trägt ein little-endian u16 „Slot N“.
- **Record (P = 181 B, Q-125 Round 2):**
  - Byte 0–2 = 3-Byte-Sync-Präfix `XX 0B 1A` (Tail `0B 1A` konstant,
    vorangehender Byte variiert 07/08); Byte 3 = Sekunden, 4 = Minuten,
    5 = Stunden (explizit, kein Delta).
  - Die analogen Wert-Slots sitzen an einer **Mischung gerader UND ungerader**
    Byte-Offsets: dev0 a1–a10 @ **ungerade** 21–39, dev0 a11–a16 @
    **ungerade** 41–51 (Q-125 Round 3), dev2 a5–a9 @ **gerade**
    114–130, dev2 a10–a12 @ 134–142, dev2 a1–a4/a13–a16 @ 146–158, dev2
    a17–a19 @ 162–170 — das Record wurde restrukturiert, die Slots sind NICHT
    `2*slot` (Kap. 3.1, Kap. 4.4).
  - Byte 179/180 = CRLF-Record-Terminator.
- **Header/Record-Überlappung (P=169):** Das P=169-Record-Raster startet bei
  5915, der letzte Label-Block endet erst bei 6014 — die „Solarpumpe
  1"-Label-Bytes übermalen die dev1/dev2-Wert-SLOTS von **Record 0** (seine
  Time-Bytes bleiben gültig). Record 0 wird daher **verworfen**; alle übrigen
  Records sind sauber und 1:1 auf Uhrzeit ausgerichtet. Der Decoder erkennt
  die Grenze **deterministisch** (P-Grid-Scan auf 30-Record-`hh:mm:ss`-Fenster
  mit +9..+11 s-Schritt; Header-Ende = Ende des zusammenhängenden
  80-B-`0x43`-Blocklaufs) und hardcodet nichts.
- **P=181: kein Header-Overlapp (Q-125 Round 2):** Das P=181-Data-Start (6258)
  liegt NACH dem CRLF-Paar (6254/6255) hinter dem letzten Label-Block —
  Record 0 ist daher **SAUBER** und wird dekodiert (nicht verworfen wie bei
  P=169). Der Decoder erkennt den Data-Start per layout-agnostischem
  Sync-Scan (Kap. 3.0) und hardcodet nichts.
- **Digital-Werte (S4N-LogParquet):** Im `/LOG`-Binary hält das CMI **einen
  Byte pro digitaler Kanal** in u16-Slots außerhalb der analog-Map
  (Kap. 3.2, Rosetta-binär-verifiziert 2026-10-30). Der Decoder emittiert
  sie als `ad="D"`-Rows (Wert 0/1, `unit=None`): dev0 d2–d9 + dev2 d1.
  **dev1 digital d1–d13 trägt kein Payload-Byte** (Kap. 3.2) und wird als
  **dokumentierter 0.0-Fill** emittiert (S4N-LogFull, Q-112b: keine NULL-
  Spalte im Store; Beleg: Slot-Scan + LogX-GT konstant 0 auf 344 188 Samples).
- **Volltag:** P=169: 8640 Records (8639 dekodiert + 1 Header-Overlapp).
  P=181: Data-Start nach dem letzten Label-Block, kein Overlapp — alle
  Records dekodiert (z. B. 2784/2784 im 11-07-Primärfile). Partial-Tag
  (z. B. 10-29): weniger Records, letzter Record = letzter 10-s-Sample;
  abgerissener Tail < P wird verworfen.
- **≠ LogX:** Kein `TA_WINSOL_LOG_FILE`-Magic, kein `>>>>`-Mark, P = 169
  (bzw. 181, Q-125; LogX: P = 137). Die **Messwerte sind aber identisch**
  (Rosetta, Kap. 4 / Kap. 4.4).

### 3.1 Analog-Kanal- → Slot-Map (48 Kanäle, Rosetta-validiert)

| Device | Kanäle → Slot (÷Skala) |
|---|---|
| dev0 UVR1611 Heizkreis | a1–a10 → 62–71 (÷10), **a11–a15 → 72–76 (÷10, S4N-LogFull)**, a16 (Stromungsschalter) → 77 (÷1) |
| dev1 UVR1611 RAS | a1→0, a2→1, a3 (raw)→2, a4→3, a5→4, a6 (signed)→5, a7→6, a8→7, a9 (signed)→8, **a10 (raw)→9 (÷1, S4N-LogFull)**, a11→10, a12→11, a13→12 (jeweils ÷10, außer a3/a6/a9/a10 ÷1), **a14–a16 → 13–15 (÷1, raw, S4N-LogFull)** |
| dev2 UVR610 Solar | a1–a9 → 16,18,20,22,24,26,28,30,32 (÷10), **a10–a12 → 34/36/38 (÷100, Minderer 0–10 V, S4N-LogFull)**, a13–a16 → 40,42,44,46 (÷10) |

**Units:** Nur Temperatur-Kanäle (dev0 a1–a10; dev1 a1,a2,a4,a5,a7,a8;
dev2 a1–a9,a16; **dev2 a13–a15 = Temperatur am Minderer (Q-112)**) erhalten
`unit="°C"` (gegrundet am WinSol-Kanalmodell
`discovery/winsol/Setup.xml` + Unit-Codes der La/Ld-Antworten). Raw-Counter
(dev1 a3/a6/a9, Minuten-Counter), **dev1 a10/a14–a16 (raw)**, dev0-a16-Flow-
Schalter, **dev0 a11–a15 (unbelegt)** und die **Minderer-Spannungen dev2
a10–a12 (0–10 V, KEIN °C)** bleiben `unit=None`. **Digital-Rows tragen immer
`unit=None`.**

**S4N-LogFull (25 neue Kanäle, Kap. 4.3):** Die fettgedruckten Einträge sind
die zuvor NULL-bleibenden Kanäle. Sie werden jetzt per Rosetta-25 belegt:
Minderer dev2 a10–a12 (slots 34/36/38, ÷100, 0–10 V) per Value/Toggle-Proof;
dev0 a11–a15 (slots 72–76) und dev1 a14–a16 (slots 13–15) als konstante 0.0-
Fills über den echten Slot (GT konstant 0, CMI-Slots konstant 0); dev1 a10
(slot 9) als 0.0-Fill mit dokumentierter Divergenz zum LogX-Kanon (−1.0).
Damit emittiert jeder dekoderte Record exakt 48 analoge + 22 digitale =
**70 Rows** und jede Wide-Row ist vollständig gefüllt.

**P=181 Analog-Map (Q-125 Round 2 + 3 + 5, Rosetta gegen json:1/json:32 +
WinSol P=145-GT, Kap. 4.4):** Das P=181-Record ist **restrukturiert** — die
Wert-Slots liegen an einer **Mischung gerader UND ungerader** Byte-Offsets
(nicht `2*slot`). Die wertverifizierten Kanäle (dev0 a1–a16, dev1 a1–a16,
dev2 a5–a19) werden wie in der Tabelle gelesen; der nicht-verifizierte Kanal
dev2 a1–a4 (entfallen, CAN-only) wird als **dokumentiertes NULL** emittiert
(nie geraten, Q-125 Round 5):

| Device | Kanäle → Byte-Offset (÷Skala) |
|---|---|
| dev0 UVR1611 Heizkreis | a1–a10 → **ungerade 21–39** (÷10, signed; json:1 ch1–ch10), **a11–a16 → ungerade 41–51** (Q-125 Round 3, ÷10 bzw. a16 ÷1, signed; Beleg unten) |
| dev1 UVR1611 RAS | **Q-125 Round 5: a1–a16 → gerade 66–96** (a1/a2/a4/a5/a7/a8 ÷10 °C, a3/a6/a9 ÷1 raw-Zähler, a11/a12/a13 ÷10 min, a10/a14–a16 0-Fill; signed; **Rosetta 100 % vs WinSol P=145-GT** 2784/2784 + 5755/5755) |
| dev2 UVR610 Solar | **a5–a9 → gerade 114–130** (÷10, signed; json:32 ch5–ch9, 215/215 bzw. 214/215), **a10–a12 (Minderer) → 134/138/142** (÷100, 0–10 V, unsigned), **a13–a16 → 146/150/154/158** (÷10, signed; Minderer-Ventil-Temperaturen, json:32 ch13–ch16), **a17–a19 (Sollspannung 1/2/3, NEU) → 162/166/170** (÷100, 0–10 V, unsigned; json:32 ch17–ch19, 215/215), **a1–a4 → entfallen** (CAN-only, UVR1611-Master; Q-125 Round 5, **dokumentiertes NULL**) |

**dev0 a11–a16 im P=181-Record (Q-125 Round 3, 2026-11-07 ~16:14 v0.6-Lernstart):**
json:1 liefert für ch11+ nur Digital (kein JSON-GT) → Beleg = **Binary-Pattern
(Full-Tag, 2784 Records) + Header-Label (UTF-16LE, Block-Header @ 14+80×k) +
Physik (Heizkurve)**. Kollisions-Check: die Bytes 41/43/45/47/49/51 liegen in
der dev0-Region (21–51) und sind von der dev2-Region (114–170) frei; kein
dev2-Kanal zeigt auf diese Bytes.

| Kanal | P181-Byte | Scale | Beleg-Quelle |
|---|---|---|---|
| dev0 a11 = **VL.Solltemp** | **41** | signed /10 (°C) | Full-day min 5.0 / max 49.8 °C, **23 Distinktwerte** → Heizkurve-Verhalten (~47.7 °C bei Außen 7 °C am Tag, → 5.0 °C nachts). Header-Label Block 23 = „VL.Solltemp". json:1 ch11 liefert 0.0 (JSON-API loggt diesen Kanal nicht — kein JSON-GT; Beleg = Binary-Pattern + Header-Label + Physik). |
| dev0 a12 = **T.raum.SOLL.eff** | **43** | signed /10 (°C) | Full-day **exakt 2 Distinktwerte: 16.0 und 20.0 °C** = ABSENK 16 / NORMAL 20 °C (CMI-Seite 6 pos4/pos5, Heizkreisregler). Header-Label Block 24 = „T.raum.SOLL.eff". Decisive evidence: kein anderes Slot-Byte im Record zeigt exakt {16, 20}. |
| dev0 a13 | **45** | signed /10 | Full-day konstant 0 (Header-Label Block 25 = „-----" = unassigned; 0-Fill wie P=169 a11–a15). |
| dev0 a14 | **47** | signed /10 | Full-day konstant 0 (Header „-----"). |
| dev0 a15 | **49** | signed /10 | Full-day konstant 0 (Header „-----"). |
| dev0 a16 = **Ström.Schalt5** | **51** | signed /1 (Flag) | Full-day Distinktwerte {0, 1} = Flow-Schalter-Flag (Semantik identisch zu P=169 dev0 a16 @ Slot 77). Header-Label Block 28 = „Ström.Schalt5". **Keine Temperatur** → `unit=None`. |

Nach Round 5 emittiert jeder P=181-Record **51 analoge + 22 digitale = 73
Rows**, davon **69 mit Wert** (47 wertverifizierte analoge Kanäle: dev0
a1–a16 + dev1 a1–a16 + dev2 a5–a19; + 13 dev1-digital 0.0-Fill + dev0
d2–d9 + dev2 d1) und **4 PENDING-NULLs** (dev2 a1–a4, entfallen --
Q-125 Round 5: die R2-Duplikat-Belegung a1–a4 ≡ a13–a16 wurde entfernt,
denn die Speicher-Temperaturen werden von der UVR1611 (dev0 a1–a3) geführt
und die Bytes @146–158 tragen die Minderer-Ventil-Temperaturen
(dev2 a13–a16)).

**dev1 a1–a16 im P=181-Record (Q-125 Round 5, Rosetta gegen WinSol P=145-GT):**
dev1-RAS hat **kein** JSON-Live-Ground-Truth (UVR1611-RAS nicht im
JSON-API-Node-1-Logging), dafür aber ein **zeitlich überlappendes
WinSol-LogX-Ground-Truth**: WinSol hat am 11-07 um 16:15:59 (exakt zum
UVR-Upload-Zeitpunkt) das Log-Format P=137 → P=145 (+8 B) gewechselt und
den dev0/dev1-Block 4 B früher gesetzt (dev0 digital-Maske u16 @ byte 44,
dev1 a1 @46 … a13 @78, a14–a16 @80–84, a10 @68 = 0xFFFF-Fill). Die P=181-
Slots @66–96 matchen die P=145-Werte mit **100,00 %** (2784/2784 auf 11-07
+ 5755/5755 auf 11-08, exakte-ts-Alignment, ±0 s) -- damit ist die frühere
P169-Continuity-Ambiguität (20.0 °C-Plateau) definit aufgelöst. Scales:
a1/a2/a4/a5/a7/a8 = /10 °C (RAS perzipiert/ist), a3/a6/a9 = /1 roher
Zähler (signed, negative Werte erlaubt), a11/a12/a13 = /10 Minuten
(Datenalter, = P169 dev1 a11–a13 ras_age-Referenz), a10/a14–a16 = 0-Fill
(„unused", full-day konstant 0 auf beiden P=181-Files).

### 3.2 Digital-Bitmap (Byte-pro-Kanal, Rosetta-binär-verifiziert 2026-10-30)

Digital-Werte des CMI-Records (P = 169) belegt das CMI **Byte-für-Byte pro
Kanal** -- NICHT als die permutierte u16-Maske, die WinSol/LogX schreibt.
Kanal `dN` von `dev0` liegt in Byte `110 + N` (Slot `55 + N`), Wert in Bit 0;
die High-Bytes (111, 113, ...) sind konstant 0. Kreuzvalidiert gegen die
WinSol-LogX-Digitalfelder (`analysis/winsol_logx.py`: dev0 u16-Maske @
LogX-Offset 48 mit permutierter Bitordnung `DEV0_DIGIT_BITS`, dev2-Byte @
Offset 136, Bit 7 = Solarpumpe) über **alle 3 GT-Tage**
(10-27: 5962, 10-28: 8639, 10-29: 7318 Samples):

| Kanal | Byte | Slot (u16) | Bit | Status (Rosetta) |
|---|---|---|---|---|
| dev0 d2 | 112 | 56 | 0 | **100 % werteidentisch + Toggle-Match** |
| dev0 d3 | 113 | 56 | 8 | **100 % werteidentisch + Toggle-Match** |
| dev0 d4 | 114 | 57 | 0 | **100 % werteidentisch + Toggle-Match** |
| dev0 d5 | 115 | 57 | 8 | **100 % werteidentisch + Toggle-Match** |
| dev0 d6 | 116 | 58 | 0 | **100 % werteidentisch + Toggle-Match** |
| dev0 d7 | 117 | 58 | 8 | **100 % werteidentisch + Toggle-Match** |
| dev0 d8 | 118 | 59 | 0 | **100 % werteidentisch + Toggle-Match** |
| dev0 d9 | 119 | 59 | 8 | **100 % werteidentisch + Toggle-Match** |
| dev2 d1 (Solarpumpe) | 96 | 48 | 0 | **99.5–100 % werteidentisch** (bekannte dev2-Sampling-Phase, wie dev2 a16) |

> **Entscheidender Toggle-Nachweis (2026-10-30, CMI↔WinSol-Doppelabgriff):**
> Um die auf den 3 GT-Tagen konstanten Kanäle zweifelsfrei zu belegen, wurden
> gezielt Toggles erzeugt (CMI-Rohdatei `data_2026_10_30_19_51_56.log` OHNE
> Clear + dieselben Records per WinSol, Löschen deaktiviert →
> `D2026-10-30.log`, 286 gemeinsame ts 19:52:06–20:39:36). Ergebnis:
> **dev0 d2 (WW-Pumpe Puffer, 2 Toggles), d4 (Mischkreis Zu), d5 (Anforderung
> Brenner), d6 (WW-Zirkulation), d7 (Zirkulationspumpe), d8 (Pumpe Heizkreis),
> d9 (Ventil Heizungsrücklauf)** — **alle 100 % werteidentisch AUF den
> echten Toggles** (nicht nur konstant). d3 (Mischkreis Auf) war nicht
> schaltbar (0 Toggles). **dev2 d1 (Solarpumpe):** am Toggle-Tag liefen sich
> `Byte96-Bit0` und `Byte32-Bit5` gleichauf (99,3 %, Abweichung nur an den
> letzten 2 Toggle-Samples am Fenster-Ende = Sampling-Phase); der
> **Tie-Breaker über die 3 GT-Tage fällt klar aus: Byte96-Bit0 99,5–100 %,
> Byte32-Bit5 47–60 % (zufällig)** → **dev2 d1 = Byte 96 Bit 0** (wie im
> Decoder). Damit sind **alle** dev0/d2-Digital-Kanäle mit echten Toggles
> belegt; es bleibt kein „inferred"-Kanal.

> **Hinweis (Korrektur 2026-10-30):** In einer früheren Version wurde ein
> „bit-packed" u16-Masken-Layout mit permutierter Bitordnung und einem
> *inferred* Kanal d8 angenommen. Falsch: Das CMI verwendet das obige
> Byte-für-Byte-Layout. Der frühere „d8 = inferred / konstant 0"-Befund war
> ein Artefakt des fehlerhaften Mappings (es hatte den Wert des echten d2
> -- konstant 0 -- in den d8-Slot gelesen). Mit korrektem Layout sind
> **alle** dev0-Kanäle d2–d9 zu 100 % verifiziert; es gibt keinen
> unverifizierbaren Kanal mehr.

**dev1 digital d1–d13 (S4N-LogFull, Q-112b):** Im `/LOG`-Record existiert
**kein Payload-Byte** für dev1 digital — ein Voll-Record-Scan (alle u16-Slots
über alle aufgezeichneten CMI-Dateien) findet keinen unzugewiesenen Slot,
dessen Inhalt mit der LogX dev1-digital-Maske korreliert (im Gegensatz zu
dev0 @ Slots 56–59 und dev2 @ Slot 48). Die WinSol-LogX-Ground-Truth ist
außerdem **durchgehend konstant 0** (0 von 344 188 Samples über alle vier
aufgezeichneten LogX-Tage 10-27 … 10-30). Konsequenz (Q-112b: keine NULL-
Spalte im Store): der Decoder emittiert dev1 digital als **dokumentierten
0.0-Fill** (kein Raten — der Fill-Wert ist auf 100 % der aufgezeichneten
Samples gleich dem GT-Wert). Die 13 `dev1_digital_01..13`-Spalten sind damit
gefüllt, aber als Fill (nicht als decodierter Wert) markiert.

**P=181 digital (Q-125 Round 2 + Round 5):** Die P=169-Digital-Byte-Offsets
(`_DIGITAL_MAP`: dev0 @ Slots 56–59 / Bytes 112–119, dev2 d1 @ Slot 48 /
Byte 96) sind auf dem **restrukturierten P=181-Record falsch** und werden
**nicht gelesen** — der Decoder nutzt stattdessen `_DIGITAL_MAP_181`
(absolute Byte-Offsets auf dem 181-Record). **Round 5 (Rosetta gegen die
WinSol P=145-GT, den zeitlich überlappenden +8-B-Bruder des P=181-Formats):**
das dev0 digital-Layout ist jetzt **vollständig byte-belegt** — die P=145-
dev0-digital-Maske ist das u16 @ byte 44 (P=137: @48; Bit-Permutation
`DEV0_DIGIT_BITS` = {d8:bit0, d7:bit1, d6:bit2, d5:bit3, d4:bit4, d3:bit5,
d2:bit6, d9:bit15} bleibt über den P=137→P=145-Wechsel erhalten). Jeder
Kanal matcht die GT mit **100,00 % werteidentisch + Toggle-Konsistenz**
(gleiche Toggle-Zahlen auf den gemeinsamen Timestamps; 11-07: 2784, 11-08:
5753 gemeinsame ts):

| Kanal | Byte | Bit | Status (Rosetta R5) |
|---|---|---|---|
| dev0 d2 (Pumpe WW zum Puffer) | 6 | 0 | **100 % + 0 Toggles** (P145 mask bit6; full-day konstant 0) |
| dev0 d3 (Mischkreis Auf) | 10 | 0 | **100 % + Toggle-Konsistenz** (P145 mask bit5; 28/104 Toggles) |
| dev0 d4 (Mischkreis Zu) | 11 | 0 | **100 % + Toggle-Konsistenz** (P145 mask bit4; 22/32 Toggles) |
| dev0 d5 (Anforderung Brenner) | 12 | 0 | **100 % + Toggle-Konsistenz** (P145 mask bit3) |
| dev0 d6 (Pumpe WW Zirkulation) | 13 | 0 | **100 % + Toggle-Konsistenz** (P145 mask bit2; 20/18 Toggles) |
| dev0 d7 (Zirkulationspumpe) | 14 | 0 | **100 % + Toggle-Konsistenz** (P145 mask bit1; 20/18 Toggles) |
| dev0 d8 (Pumpe Heizkreis) | 15 | 0 | **100 % + Toggle-Konsistenz** (P145 mask bit0; 3/2 Toggles, intermittent) |
| dev0 d9 (Ventil Heizungsrücklauf) | 16 | 0 | **100 % + Toggle-Konsistenz** (P145 mask bit15; const-ON-Signatur wie P169 byte119) |
| dev2 d1 (Solarpumpe) | **174** | 0 | **100 % werteidentisch** (P145 dev2-Byte @144 bit7: 2784/2784 + 5753/5753; json:32 ch1 D: 182/182 + 448/448) |

> **d8/d9-Identität (⚠ offene HITL-Frage):** Die P=169-Rosetta-golden
> (byte118 = d8 Pumpe Heizkreis, byte119 = d9 Ventil Heizungsrücklauf; P137
> bit0/d8 vs bit15/d9) ketten über P137→P145→P181 zu **byte15 = d8 /
> byte16 = d9**, die Signaturen stimmen überein (byte15 intermittent,
> byte16 const-ON). maintainer-q125-„byte16 = d8 (Pumpe)" wird dadurch
> widersprochen → R5 implementiert die Rosetta-golden-Belegung; die
> Abweichung ist #1-HITL-Open-Item vor Deploy.
>
> **dev0 d1 (Pumpe Holzofen, byte 8, P145 mask bit7):** 100 % werteidentisch
> + Toggle-Konsistent (beide P=181-Files), **aber NICHT PERSISTIERT** — das
> 74-Spalten-Store-Schema hat keine `dev0_digital_01`-Spalte (73-Zeilen-/
> 74-Spalten-Gate). Bleibt loggt-aber-unpersistiert (Store-Schema-Change,
> out-of-scope).
>
> **dev2 d1-Korrektur (R2 → R5):** R2 hatte dev2 d1 @ byte 10 (falsch —
> byte 10 ist dev0 d3, Mischkreis Auf). byte 174 bit0 ist der
> strukturell korrekte Slot (letztes Byte der dev2-Region, nach dev2
> a19 @170) und matcht die P=145 dev2-digital-GT @144 bit7 mit 100 %.

**dev1 digital d1–d13** bleibt auch in P=181 der dokumentierte **0.0-Fill**
(kein CMI-Payload, wie in P=169).

**Abdeckung vs. Store-Schema (74 Spalten, Q-125):** cmi-log liefert je
Record die Wertkanäle des jeweiligen P-Formats:

* **P=169 (legacy):** alle 70 Wertkanäle (48 analog + 22 digital: dev0
  a1–a16, dev1 a1–a16, dev2 a1–a16, dev0 d2–d9, dev1 d1–d13, dev2 d1).
  Keine Wertspalte bleibt NULL; die einzigen nicht-empirischen Fills sind
  die dokumentierten Konstanten (dev0 a11–a15 = 0.0, dev1 a10 = 0.0 mit −1.0-
  Kanon-Divergenz, dev1 a14–a16 = 0.0, dev1 d1–d13 = 0.0, dev2 a10–a12 =
  0.0 auf den GT-Tagen). Die drei Q-125-Spalten `dev2_analog_17/18/19`
  tragen im P=169-Record **kein Payload** → NULL.
* **P=181 (Q-125 Round 2 + 3 + 5):** 73 Wertkanäle (51 analog + 22 digital).
  Gefüllt (69 mit Wert): dev0 a1–a16 (a11–a16 = Round 3: a11 = VL.Solltemp,
  a12 = T.raum.SOLL.eff, a13–a15 = „-----"-0-Fill, a16 = Ström.Schalt5),
  **dev1 a1–a16 (Round 5: RAS, @66–96, Rosetta 100 % vs P=145-GT)**, dev2
  a5–a19 (darin a17–a19 = Sollspannung 1/2/3), **dev0 d2–d9 (Round 5,
  @6/10–16)**, dev2 d1 (Solarpumpe, Round 5 @byte174). **NULL** (dev2 a1–a4,
  entfallen — Round 5: R2-Duplikat-Hack entfernt, 4 NULLs/Record). dev1 d1–d13
  = 0.0-Fill. (dev0 d1 = Pumpe Holzofen @byte8: verifiziert, aber nicht
  persistiert — keine `dev0_digital_01`-Spalte im 74-Spalten-Schema.)

Mapping: analog Kanal N → `dev{dev}_analog_{N:02d}`, digital Kanal N →
`dev{dev}_digital_{N:02d}`. Das Wide-Store-Schema ist das 71-Spalten-Schema
von `winsol_pq` **PLUS** den drei Q-125-Kanälen `dev2_analog_17/18/19`
(direkt nach `dev2_analog_16`, `dev2_digital_01` bleibt die letzte Spalte)
= **74 Spalten** (Kap. 7).

## 4. Rosetta-Ergebnis (Decoder vs WinSol-LogX)

Validiert auf `discovery/cmi-log/cmi_raw_*.log` (CMI-roh) gegen
`logx_*.log` (WinSol-LogX, `analysis/winsol_logx.py`) für **alle 3 Tage**:

### 4.1 Analog (Kap. 3.1)

| Tag | Records | dekodiert | Ts-Überlapp | Werte exakt (gesamt) | stark |
|---|---|---|---|---|---|
| 2026-10-27 | 5963 | 5962 (rec 00:00-Overlapp verworfen) | 5962/5962 | 96.2 % | 99.78 % |
| 2026-10-28 | 8640 | 8639 | 8639/8639 | 96.0 % | 99.78 % |
| 2026-10-29 | 7319 | 7318 | 7318/7318 | 96.9 % | 99.87 % |

- **dev0 (Heizkreis) ist auf allen 3 Tagen zu 100 % exakt** (a1–a10, a16).
- **Starke Kanäle** (alle anderen): ≥ 99.78 % exakt (Differenz ≤ 1 Count
  vereinzelt, sub-second).
- **Dokumentierte Schwach-Kanäle** (Abweichung 1–2 Raw-Counts, Verteilung
  symmetrisch um 0 → **Sampling-Phase**, kein Mapping-Fehler):
  dev1 a4/a6/a9/a11/a12/a13, dev2 a1/a2/a3/a13/a14/a15/a16. Ursache: die
  CMI- und WinSol-Sampler haben sub-second-verschiedene Phasen bei
  schnellen Counter-Kanälen; z. B. dev2 a16 (Kollektortemperatur):
  100 % der Differenzen ≤ 50 Raw-Counts, 99 % ≤ 10 (÷10 = ≤ 1 °C).
  Diese Kanäle werden **nicht** „gefixt" (die LogX-Seite ist der Rosetta-Stein);
  sie sind im Decoder-Kommentar + `tests/test_cmi_log.py::WEAK_CHANNELS` notiert.

### 4.2 Digital (S4N-LogParquet, Kap. 3.2)

| Kanal | 2026-10-27 | 2026-10-28 | 2026-10-29 | 2026-10-30 (Toggle) | Status |
|---|---|---|---|---|---|
| dev0 d2–d9 | 100 % | 100 % | 100 % | **100 % (auf echten Toggles)** | **bestätigt** (werteidentisch + Toggle-Match, alle 8 Kanäle) |
| dev2 d1 (Solarpumpe) | 99.50 % | 99.84 % | 100 % | 99.30 % (Tie→GT-Tage) | **bestätigt** (dev2-Sampling-Phase) |
| dev1 d1–d13 | GT = 0 (0/21919) | wie links | wie links | wie links (0/344 188 gesamt) | **S4N-LogFull: 0.0-Fill** (kein CMI-Payload, Kap. 3.2; keine NULL im Store) |

- **Toggle-Tag 2026-10-30:** 286 gemeinsame ts (19:52:06–20:39:36).
  Manuell getoggelt: dev0 d2/d5/d6/d7/d8/d9 (2–7×); dev0 d4 (Mischkreis Zu)
  togglete zusätzlich über die Mischkreis-Automatik (manuell war es nicht
  schaltbar). **Alle 7 getoggelten Kanäle 100 % werteidentisch.** d3
  (Mischkreis Auf) blieb konstant (nicht schaltbar). dev2 d1 2× geschaltet,
  99.30 % (Abweichung an den letzten 2 Samples = Sampling-Phase;
  GT-Tage belegen eindeutig Byte96-Bit0, s. Kap. 3.2).

Rosetta-Regression: `tests/test_cmi_log.py::test_rosetta_digital`
(dev0 d2–d9 ≥ 99.9 % + dev0-Toggle-Konsistenz, dev2 d1 ≥ 99 %) +
`test_synthetic_digital_decode` (synthetisches Byte-für-Byte-Layout).

### 4.3 Rosetta-25: die 25 zuvor fehlenden Kanäle (S4N-LogFull, Q-112b)

Beleg der 25 Kanäle, die vor S4N-LogFull im Store NULL blieben —
`dev2 a10–a12` (Minderer), `dev0 a11–a15`, `dev1 a10 + a14–a16`,
`dev1 d1–d13` — gegen die WinSol-LogX-Ground-Truth.
**Reproduzierbar:** `scripts/rosetta_full.py` (druckt die ganze
Belegtabelle mit Match-%, Werteverteilungen und ≥5 Sample-Paaren je
Kanal/Tag; nur read-only-Inputs).

**Minderer dev2 a10–a12 (Rücklaufminderer Keller/EG/OG, 0–10 V, raw ÷100):**
die Slots 34/36/38 (die ungeraden Zwischen-Slots 35/37 sind konstant 0),
also strukturell zwischen a9 @32 und a13 @40 — wie alle dev2-Analog-Slots
jeweils geradzahlig mit einem konstanten 0-Slot dazwischen:

| Kanal | Slot ÷100 | 10-27 | 10-28 | 10-29 | 10-30 (Toggle) | Beleg |
|---|---|---|---|---|---|---|
| dev2 a10 (Keller) | 34 | 5962/5962 (100 %) | 8639/8639 | 7318/7318 | 286/286 | GT konstant 0; 10-V-Zustand real (winsol_pq: 27 808 Samples = 10.0 V, 2026-10-06..18) |
| dev2 a11 (EG) | 36 | 5960/5962 (99.97 %) | 8639/8639 | 7314/7318 (99.95 %) | 285/286 (99.65 %) | **variabel** (0/10 V, 5013× 10 V am 10-27) → Value-Proof; die 1–2 Abweichungen/Tag = dev2-Sampling-Phase (wie dev2 a16) |
| dev2 a12 (OG) | 38 | 5962/5962 | 8639/8639 | 7316/7318 (99.97 %) | 286/286 | 10-29 variabel (420× 10 V) → Value-Proof |

Abgleiche Samples (10-30-Toggle, a11): `19:51:56 cmi=0 gt=1000 [PHASE]`,
`19:52:06 cmi=1000 gt=1000`, `19:52:16 cmi=1000 gt=1000`, … — exakt wie bei
den übrigen dev2-Schwachkanälen ein 1-Sample-Phasenoffset, kein
Mapping-Fehler. **Units:** KEIN °C (Minderer-Spannung 0–10 V; die
Temperatur am Minderer ist dev2 a13–a15, Q-112).

**Konstante Fills (0.0 via echtem Slot):** dev0 a11–a15 (slots 72–76, ÷10)
und dev1 a14–a16 (slots 13–15, ÷1) liegen strukturell zwischen den
mappeden Nachbarn (dev0: a10 @71 / a16 @77; dev1: a13 @12). LogX-GT
konstant 0 über alle 4 Tage (100 % matchen) **und** CMI-Slots konstant 0
(Voll-Record-Scan) → 0.0-Fill, indem der reale Slot gelesen wird (kein
Hardcode). dev1 a10 (slot 9, ÷1, zwischen a9 @8 und a11 @10): **dokumentierte
Divergenz** — der LogX-Kanon ist konstant **−1.0** (raw 0xFFFF) über alle
4 Tage (winsol_pq 2026-10: 225 940 von 227 312 Samples = −1.0, vereinzelt
0.0), der CMI-Slot hält **0.0** → der Decoder füllt **0.0 (den CMI-Wert)**;
die Abweichung vom WinSol-Kanon ist hier bewusst und dokumentiert
(Store-Regel „kein Feld leer" + der CMI-Record enthält die einzige gültige
CMI-Seiten-Wert für diesen Slot).

**dev1 digital d1–d13 (0.0-Fill):** kein CMI-Payload-Byte (Voll-Record-Scan:
keine unzugeteilten Slots korrelieren; die einzigen nicht-kanalbelegten
nicht-konstanten Slots 49–55 sind interne Zähler) + LogX-GT konstant 0 auf
**0 von 344 188 Samples** (10-27: 8639, 10-28: 8640, 10-29: 7326,
10-30-Toggle: 1871 Records) → 0.0-Fill auf 100 % der aufgezeichneten
Samples, explizit als Fill (nicht decodierter Wert) markiert.

**Regression:** `tests/test_cmi_log.py::test_legacy_45_channels_bit_identical`
(Werte der alten 45 Kanäle bit-identisch), `test_rosetta_new_slots_vs_logx_gt`
(Minderer ≥ 99.5 %), `test_minderer_v_scaling_and_units` (0–10 V, kein °C),
`test_constant_fill_channels_read_their_slots`, `test_dev1_digital_fill_is_emitted_zero`,
`test_seventy_rows_per_record`.

### 4.4 Rosetta P=181: die 3 neuen Kanäle + P=181-Belegung (Q-125 Round 2, 2026-11-07)

Beleg der P=181-Wert-Slots gegen die **zeitlich überlappenden** JSON-
Referenzen (`data/history.db` `measurements`, `source='json:1'` = dev0,
`source='json:32'` = dev2; es gibt **keine** dev1-JSON-Serie). Primärfile
`data_2026_11_07_16_15_59.log` (510162 B = 6258 + 2784×181), 2784 Records
11-07 16:15:59 … 23:59:59, ±60 s Vergleichsfenster, n=214/215 pro Kanal:

| P=181-Kanal | Byte-Offset | ÷Skala | JSON-Ref | Beleg |
|---|---|---|---|---|
| dev0 a1–a10 | **ungerade 21–39** | 10 (signed) | json:1 A1–A10 | **214/215 exakt** (±60 s) |
| dev0 d9 (Ventil Heizungsrücklauf) | **16** (digital) | — | P145 mask bit15 | **100 % + Toggle-Konsistenz** (R5; const-ON-Signatur wie P169 byte119) |
| dev2 a5–a9 | **gerade 114–130** | 10 (signed) | json:32 A5–A9 | **215/215 bzw. 214/215 exakt** |
| dev2 a10–a12 (Minderer 0–10 V) | **134/138/142** | 100 (unsigned) | json:32 A10–A12 | exakt (0–10 V, Keller/EG/OG Minderer) |
| dev2 a13–a16 (Minderer-Ventil-Tempp) | **146/150/154/158** | 10 (signed) | json:32 A13–A16 | exakt (Keller/OG/EG Ventil-Temp + T.Kollektor) |
| dev2 a1–a4 | **entfallen (NULL)** | — | — | **R5: R2-Duplikat-Hack entfernt** (CAN-only, UVR1611-Master führt Speicher-Temp bei dev0) |
| **dev1 a1–a16 (RAS)** | **gerade 66–96** | 10 °C / 1 raw / 10 min (signed) | WinSol P=145-GT | **R5: 100 % werteidentisch** (2784/2784 + 5755/5755; a3/a6/a9 raw, a11–a13 min, a10/a14–a16 0-Fill) |
| **dev2 a17 (Sollspannung 1)** | **162** | **100 (unsigned)** | json:32 A17 | **215/215 exakt** (konstant 10.0 V) — **NEUER Q-125-Kanal** |
| **dev2 a18 (Sollspannung 2)** | **166** | **100 (unsigned)** | json:32 A18 | **215/215 exakt** (1.0 V @rec0 → 10.0 V ab 16:31:59, Sollwert-Wechsel) — **NEU** |
| **dev2 a19 (Sollspannung 3)** | **170** | **100 (unsigned)** | json:32 A19 | **215/215 exakt** (konstant 0.0 V) — **NEU** |
| dev2 d1 (Solarpumpe) | **174** (digital) | — | P145 @144 bit7 + json:32 ch1 D | **R5: 100 % werteidentisch** (Korrektur R2 @byte10 → @byte174; 2784/2784 + 5753/5753) |
| dev0 d2–d8 | **6/10/11/12/13/14/15** (digital) | — | P145 mask @44 | **R5: 100 % + Toggle-Konsistenz** (byte-belegt; d8 @15, d9 @16, s. Kap. 3.2) |

> **R5-dev0-digital-Hinweis:** dev0 d2–d9 sind jetzt **vollständig
> byte-belegt** (P145 dev0-digital-Maske u16 @ byte 44, Kap. 3.2). d9 @byte16
> matcht die P145-Maske bit15 mit 100 % (const-ON-Signatur); die frühere
> json:1-ch9-D-214/214-Beleg (R2) wurde durch die strengere P145-GT
> ersetzt (json:1-D-Nummerung ist nicht 1:1 mit CMI-d-Nummerung).

**Rec0-Spot-Check (16:15:59, byte-verifiziert):** dev0 a6 (VL) = 37.9 °C
(json:1 ch6 = 38.5 @16:16:33), dev0 a8 (Außen) = 7.4 °C (json:1 ch8 = 7.5),
dev0 d9 = 1.0 (Heizphase), dev2 a17 = 10.0 V, dev2 a18 = 1.0 V, dev2 a19 =
0.0 V, dev2 d1 = 0.0, dev2 a5 = 0.0 @rec0 (json:32 ch5: 0.0 @16:15:29 →
20.0 @16:17:39, Minderer Keller Temp), dev2 a13 = 29.1 °C (Minderer-Keller-
Ventil-Temp, @146). **R5-Neu:** dev1 a1 (ras_kell_perc) = 20.0 °C, dev1
a2 (ras_kell_ist) = 20.0 °C, dev1 a11 (ras_age_kell) = 6.1 min, dev0 d5
(Anforderung Brenner) = 1.0, dev0 d8 (Pumpe Heizkreis) = 1.0, dev0 d9
(Ventil Heizungsrücklauf) = 1.0, dev2 d1 (Solarpumpe @byte174) = 0.0,
dev2 a1 = NULL (entfallen, R5).

**P=181-PENDING (dokumentiertes NULL, nie geraten) — R5-Status:**

* ~~**dev0 a11–a16**~~: **Q-125 Round 3 (2026-11-08): NICHT mehr pending** —
  wertverifiziert auf den ungeraden Bytes 41/43/45/47/49/51 (dev0-Region,
  direkt nach a10 @39; kein JSON-GT, Beleg = Full-Tag-Pattern + Header-Label
  + Physik, s. Kap. 3.1 dev0-Tabelle). dev0 a11 = VL.Solltemp, a12 =
  T.raum.SOLL.eff, a13–a15 = „-----"-0-Fill, a16 = Ström.Schalt5.
* ~~**dev1 a1–a16**~~: **Q-125 Round 5 (2026-11-08): NICHT mehr pending** —
  wertverifiziert auf den geraden Bytes 66–96 gegen die zeitlich
  überlappende WinSol P=145-GT (100 % werteidentisch, 2784/2784 auf 11-07 +
  5755/5755 auf 11-08; a3/a6/a9 = rohe Zähler, a11–a13 = Datenalter in
  Minuten, a10/a14–a16 = 0-Fill). S. Kap. 3.1 dev1-Tabelle.
* ~~**dev0 d2–d8**~~: **Q-125 Round 5 (2026-11-08): NICHT mehr pending** —
  byte-belegt (dev0 d2–d9 @ byte 6/10/11/12/13/14/15/16, bit0) gegen die
  WinSol P=145 dev0-digital-Maske u16 @ byte 44 (100 % +
  Toggle-Konsistenz, beide P=181-Files). S. Kap. 3.2 P=181-digital-Tabelle.
* **dev2 a1–a4: ENTFALLEN (R5, dokumentiertes NULL)** — die R2-Duplikat-
  Belegung (a1–a4 ≡ a13–a16 @146/150/154/158) wurde entfernt: die
  Speicher-Temperaturen werden von der UVR1611-Master (dev0 a1–a3 @21–39)
  geführt, die Keller-RAS-Werte laufen über dev1 a1/a2; die Bytes
  @146/150/154/158 tragen die Minderer-Ventil-Temperaturen (dev2 a13–a16).
  maintainer (q125-Doku §2c): „Alles diese Werte sind hier nur per CAN BUS
  uebertragen ... die UVR1611 ist hier der Master." → **PENDING-Rest nach
  R5 = nur dev2 a1–a4 (4 NULLs/Record).**

**P=181 dev0 a11–a16 (Q-125 Round 3, 2026-11-08):** Die sechs dev0-Kanäle sind
am wahren Frame (data_start = 6258, Primärfile 11-07, 2784 Records)
byte-belegt und per Full-Tag-Pattern + Header-Label (UTF-16LE, Block-Header
@ 14 + 80×k) + Physik (Heizkurve) selbst verifiziert. json:1 liefert für
ch11+ nur Digital (kein JSON-GT: json:1 ch11 = 0.0; die JSON-API loggt diesen
Kanal nicht). Kollisions-Check: die Bytes 41/43/45/47/49/51 liegen in der
dev0-Region (21–51) und sind von der dev2-Region (114–170) frei — kein
dev2-Kanal zeigt auf diese Bytes; dev0 a12 @43 ist der kanonische
dev0-Region-Eintrag (die dev2-Duplikat-Region liegt @114–170).

| P=181-Kanal | Byte-Offset | ÷Skala | Beleg-Quelle (Full-Tag 2784 Records) |
|---|---|---|---|
| dev0 a11 = **VL.Solltemp** | **41** | 10 (signed, °C) | min 5.0 / max 49.8 °C, **23 Distinktwerte** → Heizkurve (~47.7 °C bei Außen 7 °C am Tag → 5.0 °C nachts); Header blk 23 = „VL.Solltemp". rec0/2 = 47.7 °C. |
| dev0 a12 = **T.raum.SOLL.eff** | **43** | 10 (signed, °C) | **exakt 2 Distinktwerte {16.0, 20.0}** = ABSENK 16 / NORMAL 20 (CMI-Seite 6 pos4/pos5); kein anderes Slot-Byte zeigt exakt {16, 20}; Header blk 24 = „T.raum.SOLL.eff". rec0/2 = 20.0 °C. |
| dev0 a13 | **45** | 10 (signed) | Full-day konstant 0; Header blk 25 = „-----" (unassigned, 0-Fill wie P=169 a11–a15). |
| dev0 a14 | **47** | 10 (signed) | Full-day konstant 0; Header blk 26 = „-----". |
| dev0 a15 | **49** | 10 (signed) | Full-day konstant 0; Header blk 27 = „-----". |
| dev0 a16 = **Ström.Schalt5** | **51** | 1 (signed, Flag) | Full-day Distinktwerte {0, 1} = Flow-Schalter-Flag (Semantik = P=169 dev0 a16 @ Slot 77); Header blk 28 = „Ström.Schalt5". **Keine Temperatur** → `unit=None`. |

Die Kanäle sind `_CHANNEL_MAP_181_PENDING` im Decoder nicht mehr notiert; sie
werden jetzt aus `_CHANNEL_MAP_181` gelesen (Q-125 Round 3). **dev1 digital
d1–d13** bleibt auch in P=181 der dokumentierte **0.0-Fill** (kein CMI-Payload,
wie in P=169). PENDING-Rest nach Round 5 = **nur dev2 a1–a4** (4
NULLs/Record; dev1 a1–a16 und dev0 d2–d9 wurden in R5 wertverifiziert,
dev2 a1–a4 entfielen).

**Kanal-Introduktion + Header-Kanalzähler (Q-125):** Die drei neuen
Minderer-Sollspannungs-Kanäle (dev2 a17–a19 = „Sollspannung 1/2/3", Q-102
Parameter-Logging) sind der **einzige** Inhalt der Header-Erweiterung
75 → 78 Blöcke (dev0 +2 Labels: VL.Solltemp @23, T.raum.SOLL.eff @24; dev2
+3 Labels: Sollspannung @74–76). Der **Header-Kanalzähler u16LE @ Offset 10**
steigt entsprechend 48 (P=169) → 51 (P=181) — im P-Detect (Kap. 3.0) als
schneller Hinweis belegt, auf beiden realen Files.

**Regression / Proof:** `tests/test_cmi_log.py::test_p181_*`
(`test_p181_magic_lenience_all_four_magics`, `test_p181_synthetic_roundtrip_
73_rows`, `test_p181_detect_period`, `test_p181_pending_channels_all_null`,
`test_p181_real_file_decodes_73_rows`, `test_p181_zero_record_tolerance`,
`test_p181_data_start_layout_agnostic`, `test_p181_dev0_a11_a16_full_day_
grounding`, `test_p181_unit_assignment_round3`, `test_p181_dev0_a11_a12_store_
fill`) +
`tests/test_cmi_log_pq.py::test_write_month_*` (74-Spalten-Schema, P=169 →
dev2 a17–19 NULL, P=181 → gefüllt, Legacy-71-Migration). Rosetta-Gate:
**≥5 P=181-Kanäle wertverifiziert** → erfüllt (dev0 a1–a10 + dev0 d9 + dev2
a1–a19 + dev2 d1 = 25 Kanäle vs json:1/json:32, inkl. dev2 a5–a9 = 215/215
exakt, Sollspannungen dev2 a17 = 10.0 V konstant, dev2 a19 = 0.0 V konstant,
dev2 a18 = 1.0 V @rec0 → 10.0 V ab 16:31:59 (Sollwert-Wechsel, 215/215 exakt)).

## 5. Poller-Verhalten (S4N-LogPoll, read-only Default)

`Gateway._cmi_log_poller` (alle `CMI_LOG_POLL_SEC`, Default 300 s),
best-effort wie die anderen Poller:

0. **RAM-Buffer-Flush (S4N-LogFlush):** `GET /LOG/infoh.log` (WinSol
   „Logger auslesen"-Semantik) VOR dem Index-Read — der Flush-Trigger des
   CMI (Kap. 5.1). Read-only, ~6 kB, rate-unlimited, jahresunabhängig
   (1× pro Zyklus, auch beim Jahres-Fallback). **Best-effort:** ein Fehler
   (4xx/5xx/Timeout/Connection) wird nur als `logger.warning` geloggt —
   **kein** `_cmi_log_error`-Latch, kein Zyklus-Abbruch; der Flush-Read ist
   ein Trigger-Hinweis, keine Teil der Ingestions-Garantie (ohne Flush
   bleibt der Poller flush-begrenzt: nur geflushte Daten sichtbar).
1. `GET /LOG/info<JJJJ>.log` (JJJJ = aktuelles Jahr). **Jahreswechsel (404):**
   der Index des Vorjahres wird gelesen und seine noch gepufferten
   `data_*.log` ingested.
2. Jede Index-Datei, deren **Basename + Index-Größe** nicht exakt in der
   Side-Table `cmi_log_ingested` (history.db) stehen: gestreamter Download
   (64 KiB-Chunks → `.part`-Temp + SHA-256 + Größe-Check gegen den Index +
   atomares Rename), Decode (analog + digital, Kap. 3.1/3.2), dann
   **Parquet-Monats-Rewrite** im cmi-log-Store (`data/cmi_log_pq/...`,
   S4N-LogParquet, Kap. 7): Wide-Records (1 Row/ts, 74-Spalten-Schema)
   mergen + ts-Dedup + atomares Rename. **Erst danach** `cmi_log_mark`
   (Name + Größe + SHA-256, `INSERT OR REPLACE` — überschreibt bei
   Re-Ingestion die gespeicherte Größe) — ein fehlgeschlagener Parquet-Write
   wird deshalb im nächsten Zyklus erneut decodiert und geschrieben.

   **Dedup = Name + Größe (S4N-LogPoll-DedupFix, 2026-10-30):** Der CMI hält
   im /LOG-Buffer einen **aktiven Block**, dessen Datei **während** sie im
   Index gelistet ist weiterwächst (Batch-Flushes). Ein rein name-only-Dedup
   (`name not in ingested`) würde die Datei nach der ersten Ingestion nie
   erneut abholen und damit jeden nachträglich geschriebenen Record
   verlieren. *Beleg (live, 2026-10-30):* `data_2026_10_30_19_51_56.log`
   wurde 22:25:18 per Poller ingested (Index-Größe 54521 B, Records bis
   20:39:36), wuchs danach auf 176539 B (Records bis 22:39:56); die 722
   fehlenden Records (20:39:46 → 22:39:56) mussten 23:1x per manuellem
   Backfill (`analysis/cmi_log_backfill.py`) nachgezogen werden. Neue Regel:
   `name not in meta or meta[name] != index_size` → holen (neu ODER
   gewachsen); gleiche Größe → überspringen (wie vorher). Die Re-Ingestion
   ist idempotent, weil `cmi_log_pq.write_month` ts-dedupliziert (bestehende
   ts bleiben, neue ts werden ergänzt — atomarer Rewrite; Kap. 7). Auch eine
   (praktisch nicht vorkommende) kleinere Index-Größe triggert ein
   Re-Fetch — die Regel ist ein einziger Equality-Check und das exakte Dual
   der Clear-Gate-Bedingung.
3. **`clear.log` standardmäßig NIEMALS** (`CMI_LOG_CLEAR=false`, Default):
   der Poller ist read-only und sendet den state-mutating
   `/LOG/clear.log`-Call **überhaupt nicht** (das Clear-Gate wird komplett
   übersprungen). Grund (HITL 2026-10-30, Kap. 6): ein Clear löscht alle
   Buffer-Blöcke **inkl. des aktiven, noch nicht indexierten Blockes** →
   Datenverlust unveröffentlichter Records. Die Buffer-Freigabe bleibt eine
   manuelle WinSol-Fetch-mit-Delete-Entscheidung (Benutzer-Verantwortung).
   **`CMI_LOG_CLEAR=true` (experimentell, legacy):** das alte
   last-data-first-Gate gilt — `clear.log` NUR wenn alle Index-Dateien in
   der Side-Table **in exakt der aktuellen Index-Größe** persistiert sind
   (`meta[name] == f.size` für jede Index-Datei; das Gate prüft die
   Side-Table, nicht nur den Fetch-Erfolg: eine Datei mit fehlgeschlagenem
   Parquet-Write wird niemals ungepuffert geclert, und ein gewachsener
   aktiver Block, dessen gespeicherte Größe hinter der Index-Größe
   zurückliegt, blockiert das Clear, bis er bei der aktuellen Größe
   persistiert ist — S4N-LogPoll-DedupFix). Antwort muss `OK` sein, sonst
   Fehler-Latch + Retry im nächsten Zyklus. Im Jahreswechsel-Fallback
   (Punkt 1) wird in beiden Modi **kein** Clear gesendet
   (Puffer-Zustand über die Jahresgrenze unverifizierbar; die Dateien
   verlassen den Buffer von selbst, sobald das CMI ins neue Jahr wechselt).

### 5.1 Flush-Mechanismus (bewiesen 2026-10-31)

Das CMI hält das interne /LOG-Logging (10-s-Sampling) zwischen den Flushes
in einem **RAM-Buffer**. Der Flush auf Disk — die aktive `data_*.log` wird
ergänzt und die Index-Größe aktualisiert — erfolgt **ausschließlich** beim
`GET /LOG/infoh.log` (exakt der First-Read, den WinSol „Logger auslesen"
ausführt). `GET /LOG/info<JJJJ>.log` (Index) und `GET /LOG/<JJJJ>/data_*.log`
(Daten) triggern **keinen** Flush; der User-Agent ist irrelevant (nur der
infoh-Read selbst zählt).

**Beleg (2026-10-31):**

- **51-min-Daten-Lücke:** Der Poller (index-gesteuert, Name+Größe-Dedup)
  sah nur, was ein manueller WinSol-Abgriff geflusht hatte — zwischen zwei
  WinSol-Klicks fehlten die in den RAM-Buffer geschriebenen Records
  (CIR-Log 09:22–09:38).
- **Kontroll-Experiment:** 3× `data_*.log`-GET + Index-Read allein lösten
  **keinen** Flush aus; der anschließende `GET /LOG/infoh.log` flushte
  **+36 Records = 360 s** bei 10-s-Sampling.

**Konsequenz:** Der Poller beginnt **jeden** Zyklus mit dem infoh-Read
(Punkt 0) — read-only, ~6 kB, rate-unlimited (288 Reads/Tag bei
5-min-Zyklus), daher **keine** neue Config-Umgebungsvariable. Damit sinkt
die Daten-Latenz auf ≤ ein Poll-Intervall (Default 5 min). Der
manuelle WinSol-Abgriff bleibt optional (er triggert denselben idempotenten
Flush); die Buffer-Freigabe via `clear.log` bleibt davon getrennt und
standardmäßig aus (`CMI_LOG_CLEAR=false`, Kap. 5/6).

**Health-Fix (S4N-LogParquet A):** `_cmi_log_last_poll` wird am Ende **JEDEN**
erfolgreichen Zyklus gesetzt — **auch bei leerem Index** (früherer
Early-Return ohne ts-Update ließ `/health` → `cmi_log.last_poll_ts` auf
15:16:48 stehen, während der Poller weiterlief). `last_ingested_files`
behält die Semantik „Anzahl im letzten Zyklus ingesteter Dateien". Ein
Parquet-Persist-Fehler im aktuellen Zyklus (`_cmi_log_persist_failed`)
hält den ts-Refresh weiterhin zurück (Failed-Month bleibt sichtbar; der
nicht gemarkte File wird im nächsten Zyklus retried).

**Decode-Blind-Spot-Latch (Q-125 R4, 2026-11-08):** Ein *totaler* Decode-Ausfall
sollte vorher wie ein erfolgreicher Zyklus aussehen (Format-Wechsel wie
2026-11-07 16:14: alle neuen Index-Files un-dekodierbar → per-File-Swallow,
`decoded == []` → `cmi_log.error=None`, `last_poll_ts=jetzt`, Store eingefroren —
unsichtbar 1,5 Tage). Deshalb: wenn ein Zyklus neue Index-Files komplett
heruntergeladen hat, aber **0** davon dekodiert (nachweislich: File liegt in
`data/cmi_log/` exakt in Index-Größe vor, fehlt aber im `decoded`-Ergebnis),
latcht der Cycle `_cmi_log_error` und **hält** Error-Clear UND `last_poll_ts`-
Refresh zurück, bis ein Zyklus ≥1 File erfolgreich dekodiert. Partial-Failure
(≥1 dekodiert) und reine Transport-Fehler (Download-500/-Timeout, kein File auf
Platte) bleiben Swallow wie bisher (DoD 2b/2d):

```json
// /api/v1/health → cmi_log, bei eingefrorenem Store:
{"error": "cmi-log: 1 new file(s) in index but 0 decoded - /LOG format change or corrupt files? (undecodable: data_2026_11_07_16_14_00.log)",
 "last_poll_ts": null, "last_ingested_files": 0}
```

Fehler (Fetch/Decode/**Parquet-Write**) landen in `gateway._cmi_log_error`
und erscheinen unter `/api/v1/health` → `cmi_log.error`; die Side-Table
`cmi_log_ingested` (in `history.db`) bleibt der Dedup-Beleg (und — nur bei
`CMI_LOG_CLEAR=true` — das Clear-Gate).

## 6. Betrieb / Randbedingungen

- **Buffer-Freigabe (manuell, Benutzer-Verantwortung):** Der Poller ist
  read-only (Kap. 5) und leert den CMI-Buffer nie. Die einzige Freigabe ist
  ein **WinSol-Fetch mit Delete** („Loeschen" aktiv): WinSol holt die Blöcke
  in WinSol-LogX-Format ab und das CMI startet danach einen neuen aktiven
  Block (alle Blöcke verschwinden aus dem CMI-Buffer). Ein **WinSol-Fetch
  OHNE Delete** ist dagegen unschädlich: das CMI schließt den aktiven Block
  (`data_*.log` taucht im Index auf) und hält ihn im Buffer. **Clear-
  Semantik (HITL 2026-10-30):** ein `clear.log` löscht ALLE Blöcke im
  Buffer — **einschließlich der aktiven Block-Datei, die noch nicht im Index
  gelistet ist** — und startet einen neuen aktiven Block. Das CMI selbst
  löscht nie; bei vollem SD-Buffer hört es einfach auf zu schreiben
  (visible: ts-Abbruch im cmi-log-Parquet-Store).
- **Datenverlust-Fall 2026-10-30 (nachgewiesen):** Der Gateway-Poller (damals
  inkl. Clear-Gate) hat 15:16:48 ein `clear.log` gesendet; der aktive Block
  `data_..._2026_10_30_00_00_05.log` (00:00:05 → 15:16:48, ~5400 Records)
  war zu dem Zeitpunkt noch nicht im Index und damit nicht ingestbar — er
  wurde ungeingested gelöscht. Konsequenz: `CMI_LOG_CLEAR` Default `false`
  (Poller read-only); Buffer-Freigabe nur noch manuell per WinSol.
- **Kein manuelles WinSol mit Delete während des Gateway-Polls (Clear-
  Gate):** WinSol startet nie automatisch (nur manuell von maintainer). Wären
  beide gleichzeitig aktiv, riete der jeweils andere um die Daten
  (gemeinsamer CMI-Speicher + Clear-Race). Präzisierung 2026-10-31
  (Flush-Mechanismus, Kap. 5.1): ein manueller **WinSol-Abgriff OHNE
  Löschen ist weiterhin sicher** — er ist read-only (nur der RAM-Buffer-
  Flush via infoh-Read) und die Re-Ingestion ist im cmi-log-Parquet-Store
  idempotent (ts-Dedup, Kap. 5/7). Ein WinSol-Fetch **MIT Löschen** bleibt
  während Poller-Betrieb untersagt (Clear-Race: es könnte ungepufferte
  aktive Blöcke löschen, bevor der Poller sie ingested hat).
- **Live-Run (HITL):** `/api/v1/history?device=winsol:2&channel=16&ad=A`
  (bzw. `channel=9`) muss ab 2026-10-27 07:26:24 10-s-Punkte aus dem
  cmi-log-Parquet-Store liefern (bis dahin das WinSol-Archiv), konsistent
  mit den Live-Schematic-Werten. `device=cmi-log:*` existiert nicht mehr
  (UI: über den WinSol-Merge, Kap. 7).

## 7. Store-Strategie (S4N-LogParquet, Decision Option 1 — maintainer 2026-10-30)

Die Historie ist **logisch eine Quelle pro Gerät** (`winsol:0/1/2`,
durchgehend 2020 → heute), **physikalisch zwei Stores**:

| Store | Inhalt | Lebenszyklus |
|---|---|---|
| `data/winsol_pq/YYYY/MM/YYYY-MM.parquet` | WinSol-Logger-Archiv (Rosetta-kanonisch), **read-only** | 2020 → 2026-10-27 07:25:54 (Migration S2-Parquet-Pivot; CMI-Interner-Buffer deckt die Vergangenheit nicht mehr — WinSol-Logger war seither die einzige Quelle) |
| `data/cmi_log_pq/YYYY/MM/YYYY-MM.parquet` | CMI-Interner-Logging (`/LOG/*`), **74-Spalten-Wide-Schema** = `winsol_pq`-71-Spalten **PLUS** den drei Q-125-Kanälen `dev2_analog_17/18/19` (direkt nach `dev2_analog_16`; `dev2_digital_01` bleibt die letzte Spalte). P=169-Records füllen alle 70 Werte (48 analog + 22 digital), `dev2_analog_17/18/19` = NULL (kein P=169-Payload); P=181-Records füllen dev0 a1–a16 (a11–a16 = Round 3) + **dev1 a1–a16 (Round 5, RAS @66–96)** + dev2 a5–a19 (darin a17–a19 = Sollspannung; **a1–a4 = entfallen, NULL** — Round 5, R2-Duplikat-Hack entfernt) + **dev0 d2–d9 (Round 5, @6/10–16)** + dev2 d1 (Round 5 @byte174); P=181-pending (dev2 a1–a4) = NULL (Kap. 3.1/3.2/4.4) | **live**: der Poller rewrites den **laufenden Monat** alle 5 min atomar (bestehende Datei lesen → neue Records mergen, ts-Dedup → `.part`-Write (zstd) → atomares `os.replace`); **abgeschlossene Monate werden nie angefasst** (byte-stabil nach Letztem Rewrite). Gemessen: ~0.2 s / ~2 MB pro Rewrite — vernachlässigbar. **Legacy-71- → 74-Migration (Q-125):** vorgefundenen 71-Spalten-Monats-Files (vor Q-125) werden im Write-Pfad transparent auf 74 migriert (neue 3 Spalten = NULL; Daten bleiben unverändert); jeder andere Schema ist ein Store-Fehler. |

**~10-Tage-Buffer-Limit (read-only Default):** Der CMI-interne Buffer
(`storekrit: 10`) hält nur ~10 Tagesdateien. Der Poller leert den Buffer
**nicht** mehr (`CMI_LOG_CLEAR=false`, Kap. 5) — er deckt also fortlaufend
die "letzten" ~10 Tage ab, BIS der Buffer voll ist; dann stoppt das CMI das
Logging (sichtbar: ts-Abbruch im Store) und wartet auf eine manuelle
WinSol-Fetch-mit-Delete-Freigabe (Kap. 6). Die Vor-27-10-Vergangenheit liegt
ausschließlich in `winsol_pq`.

**Wachsender aktiver Block + Name+Größe-Dedup (S4N-LogPoll-DedupFix,
2026-10-30):** Der im Index gelistete **aktive Block** wird vom CMI weiter
angehängt, während er im Buffer liegt (Batch-Flushes). Das Store-Modell ist
dafür ausgelegt: der Poller dedupliziert auf **Name + Index-Größe**
(`meta[name] != index_size` → Re-Fetch), und `write_month` macht
Re-Ingestion idempotent (ts-Dedup: bestehende ts bleiben, neue ts werden
ergänzt — atomarer Rewrite), `cmi_log_mark` überschreibt die gespeicherte
Größe (`INSERT OR REPLACE`). *Beleg:* `data_2026_10_30_19_51_56.log` wuchs
ab 22:25:18 von 54521 B (Records bis 20:39:36, per Poller ingested) auf
176539 B (Records bis 22:39:56); der name-only-Dedup holt sie danach nie
erneut → 722 Records (20:39:46 → 22:39:56) fehlten im `cmi_log_pq`-Store
und wurden 23:1x per manuellem Backfill (`analysis/cmi_log_backfill.py`)
gefüllt. Seit dem Fix wird ein solches Wachstum im nächsten
5-Minuten-Zyklus automatisch nachgeholt (und das Clear-Gate bei
`CMI_LOG_CLEAR=true` bleibt zu, solange die persistierte Größe hinter der
Index-Größe zurückliegt).

**Merge-Regel (Historie-API):** `/api/v1/history?device=winsol:<N>` liest
beide Stores pro (device, channel, ad) und liefert die **ts-Union**; bei
ts-Kollision gewinnt `winsol_pq` (Rosetta-kanonisches Archiv). Damit
deckt eine einzige Serie pro Gerät/Kanal die gesamte Historie ohne
Lücke über die Archiv-Grenze (winsol_pq endet 07:25:54, cmi-log beginnt
07:26:24 — keine ts-Überschneidung). Implementierung:
`app/cmi/cmi_log_pq.py` (`CmiLogParquet`), `app/api/routes.py`
(`_merge_wide_points`), `app/main.py` (`_persist_cmi_log`).

**Migration (Backfill, S4N-LogFull):** `analysis/cmi_log_backfill.py` dekodiert die
**10** lokalen Dateien (3 GT-Tage + 3 Fragmente + die drei 10-30/10-31-Dateien +
das aktiv wachsende 11-01-File, alle in `cmi_log_ingested` bzw. live) jetzt mit
dem vollen 70-Kanal-Decoder und re-schreibt die Monats-Parquets `2026-10` und
`2026-11` **vollständig** (Bestandsdatei löschen + `write_month`-Vollwrite,
vorab TS-Safety-Beleg: jeder bestehende Store-ts liegt in der Quelle — kein
Datenverlust, nur die 25 zuvor-NULL-Spalten werden gefüllt). Ergebnis:
`cmi_log_pq` 2026-10 = **34 964** Rows (CMI 33 379 + LogX-Gap 1 585), 2026-11 =
**6 158** Rows — **70/70 gefüllt, 0 NULLs, 0 ts-Duplikate** (Zielzeitpunkt
Backfill). Danach räumt der Backfill die `source='cmi-log'`-Long-Rows aus
`history.db` aus (idempotent; die Side-Table `cmi_log_ingested` bleibt intakt →
Poller-Dedup; das Clear-Gate gilt nur bei `CMI_LOG_CLEAR=true`, Default ist
read-only (Kap. 5/6)).

**Offenes Item (Gateway-Race):** Der laufende Gateway-Poller (Stand 2026-11-01
noch mit dem alten 45-Kanal-Decoder) re-schreibt den laufenden Monat 2026-11
alle 5 min — NEUE ts jenseits des hier dekodierten File-Stands tragen bis zum
Gateway-Neustart wieder die 25 NULL-Spalten. Nach dem Neustart mit dem
70-Kanal-Decoder sind alle zukünftigen Rows 70/70; der Backfill ist idempotent
und beliebig wiederholbar (re-Run nach dem Neustart = dauerhaft 70/70).
