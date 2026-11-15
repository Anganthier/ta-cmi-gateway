# Q-125 · P=181-Kanal-Belegung — Korrektur-Fassung für maintainer (2026-11-08)

> **Zweck dieser Datei:** Ich (Orchestrator) habe die P=181-Slot-Belegung byte-belegt
> und in R2/R3 umgesetzt. Diese Datei listet meine **gesamte Belegung + Beleg-Quelle +
> Konfidenz** auf. **Bitte jede Zeile prüfen und bei Bedarf korrigieren** — markiere
> Korrekturen direkt in der Spalte `Korrektur (maintainer)` oder streiche/ergänze Zeilen.
> Danach mache ich eine R5 (surgical-Flip), die ALLE deine Korrekturen in den Decoder
> bringt. **Bis dahin: die PENDING-Kanäle (dev1 RAS a1–a16 + dev0 d2–d8 + dev2 a1–a4)
> sind NULL im Store — kein geratenes Wert.**

> **R5-STATUS (2026-11-08, python-pro):** Alle offenen Blöcke sind jetzt per
> WinSol P=145-GT + json:32 byte-belegt und im Decoder umgesetzt:
> **dev1 a1–a16 @66–96** (100 % Rosetta), **dev0 d2–d9 @6/10–16** (100 % +
> Toggle-Konsistenz), **dev2 d1 @byte174** (Korrektur von R2 @byte10),
> **dev2 a1–a4 = entfallen (4 dokumentierte NULLs)**. Rest-PENDING im Decoder:
> **nur noch dev2 a1–a4** (4 NULLs/Record, 73 Zeilen/Record bleiben).
> **⚠ #1 offene HITL-Frage: d8/d9-Identität @byte15/16 — Rosetta-golden vs
> maintainer-Korrektur, s. §4.**

## 0. Frame (verifiziert, 4 unabhängige Belege — bitte nur bestätigen, nicht ändern)

P=181-Record = 181 B:
| Offset | Inhalt | Beleg |
|---|---|---|
| 0–2 | Sync `07`/`08` `0B` `1A` | Sync-Grid exakt 181 B (2784×/3630×) |
| **3 / 4 / 5** | **ss / mm / hh** | rec0 = `3B 0F 10` = 16:15:59 = Filename ✓ |
| 6–178 | u16-Wert-Slots (LE; value/padding-Interleave + Duplikate) | siehe unten |
| 179–180 | CRLF `0D 0A` | 100 % Terminator |
| 6258 | **data_start** | 78×80-B-Header ab byte 14 + CRLF; **6258+2784×181 = 510162 = exakt** (11-07) · 6258+3630×181 = 662926 = exakt (11-08) |

- Header-Kanalzähler u16@10 = **51** analog. P=169 bleibt ss@106/mm@107/hh@108 + bit-identisch.
- P=181 hat **Duplikat-Regionen**: z. B. dev0-Speicher-Temp liegt sowohl @21–39 als auch
  @98–106 (gleiche Werte). → **kanonische Region pro Kanal per Ground-Truth wählen.**

## 1. dev0 (UVR1611 Heizkreis) — 16 analog → **Konfidenz: HOCH** (json:1 ch6/8 + P169-Continuity + Header-Label)

| Kanal | P181-Byte | Scale | rec0-Wert | P169-Ref | Δ | Beleg | Korrektur (maintainer) |
|---|---|---|---|---|---|---|---|
| a1 T.SP.oben | 21 | /10 + | 73.7 | 73.7 | 0.0 | json:1 + P169 | |
| a2 T.SP.mitte | 23 | /10 + | 50.7 | 50.9 | -0.2 | P169 | |
| a3 T.SP.unten | 25 | /10 + | 48.2 | 48.2 | 0.0 | P169 | |
| a4 T.Warmwasser | 27 | /10 + | 47.5 | 50.0 | -2.5 | P169 (Δ etwas groß — prüfen) | |
| a5 T.Oelkess.VL | 29 | /10 + | 68.8 | 69.2 | -0.4 | P169 | |
| a6 T.Heizkr.VL | 31 | /10 + | 37.9 | 38.3 | -0.4 | json:1 ch6 + P169 | |
| a7 T.Heizkr.RL | 33 | /10 + | 40.5 | 40.7 | -0.2 | P169 | |
| a8 Temp.Aussen | 35 | /10 + | 7.4 | 7.8 | -0.4 | json:1 ch8 + P169 | |
| a9 T.Holzk.VL | 37 | /10 + | 63.8 | 64.1 | -0.3 | P169 | |
| a10 Abgas | 39 | /10 + | 4.5 | 4.4 | +0.1 | P169 | |
| **a11 VL.Solltemp** | **41** | /10 + | 47.7 | (P169=0) | neu | full-day 5.0–49.8 °C = Heizkurve; Header blk23; **json:1 ch11 liefert 0.0 (JSON-API loggt NICHT)** | |
| **a12 T.raum.SOLL.eff** | **43** | /10 + | 20.0 | (P169=0) | neu | full-day **exakt {16.0, 20.0}** = CMI-Seite-6 ABSENK/NORMAL; Header blk24 | |
| a13 (unassigned) | 45 | /10 + | 0.0 | 0.0 | 0.0 | Header „-----"; 0-Fill | |
| a14 (unassigned) | 47 | /10 + | 0.0 | 0.0 | 0.0 | Header „-----"; 0-Fill | |
| a15 (unassigned) | 49 | /10 + | 0.0 | 0.0 | 0.0 | Header „-----"; 0-Fill | |
| a16 Ström.Schalt5 | 51 | /1 flag | 0 (∈{0,1}) | 0.0 | 0.0 | full-day {0,1}, 24 Toggles; Header blk28 | |

## 2. dev2 (UVR610 Solar) — 19 analog

### 2a. dev2 a10–a19 → **Konfidenz: HOCH** (json:32 ch10–19 = 1:1-Labels + P169 + Sollspannung-Beleg)

| Kanal | P181-Byte | Scale | rec0-Wert | P169-Ref | Beleg | Korrektur (maintainer) |
|---|---|---|---|---|---|---|
| a10 (Ventil-2-Ergebnis / Minderer Keller) | 134 | /100 − | 10.0 V | 0.0 (P169=Minderer-Pos) | json:32 ch10 + P169 | |
| a11 (Ventil-2-Ergebnis / Minderer EG) | 138 | /100 − | 1.0 V | 10.0 (P169=Minderer-Pos) | json:32 ch11 | ⚠ P169↔P181: a10/a11 seemntlich getauscht? (Keller↔EG) prüfen |
| a12 (Ventil-2-Ergebnis / Minderer OG) | 142 | /100 − | 0.0 V | 0.0 | json:32 ch12 | |
| a13 (Keller HzRkl Ventil 1) | 146 | /10 + | 29.1 | 29.3 | json:32 ch13 + P169 | |
| a14 (OG HzRkl Ventil 1) | 150 | /10 + | 28.9 | 29.0 | json:32 ch14 + P169 | |
| a15 (EG HzRkl Ventil 1) | 154 | /10 + | 35.8 | 36.5 | json:32 ch15 + P169 | |
| a16 (T.Kollektor) | 158 | /10 + | 22.9 | 22.0 | json:32 ch16 + P169 | |
| **a17 Sollspannung 1 (Keller)** | **162** | /100 − | 10.0 V | neu | json:32 ch17; Header blk74 | |
| **a18 Sollspannung 2 (EG)** | **166** | /100 − | 1.0 V | neu | json:32 ch18; Header blk75 | |
| **a19 Sollspannung 3 (OG)** | **170** | /100 − | 0.0 V | neu | json:32 ch19; Header blk76 | |


Meine Antwort:
a10 - aktueller Wert keller Minderer 0 bis 10 Volt
a11 - aktueller Wert EG/Erdgeschoss Minderer 0 bis 10 Volt
a12 - aktueller Wert OG/Obergeschoss/DG/Dachgeschoss Minderer 0 bis 10 Volt
a13 - Keller Temperpatur am Minderer
a14 - OG/DG/Obergeschoss Temperatur am Minderer
a15 - EG Temperatur am Minderer

### 2b. dev2 a5–a9 @114–130 → **Konfidenz: HOCH (verifiziert)**

> **Beleg (Orchestrator, 2026-11-08):** dev2 a5–a9 @114/118/122/126/130 matchen
> json:32 ch5–9 (Keller-Temp/EG-RAS/EG-Temp/DG-RAS/DG-Temp) mit mean-Δ 0.6–0.7 K über
> ~200 Records. Die einzige max-Δ=20 K ist das **Reboot-Artefakt-Fenster 16:16:09–16:16:59**
> (json:32 liefert dort noch 0.0 vom CAN-BUSY nach dem UVR-Upload; /LOG loggt bereits
> den realen 20.0 °C). Danach 1:1. → **a5–a9 sind korrekt @114–130.**

| Kanal | P181-Byte | rec0-Wert | P169-Ref | json:32-GT | Status |
|---|---|---|---|---|---|
| a5 Keller Temp | 114 | 20.0 | 22.2 | ch5 (mean Δ0.7 K) | ✓ HOCH |
| a6 EG RAS | 118 | 20.0 | 20.0 | ch6 (mean Δ0.6 K) | ✓ HOCH |
| a7 EG Temp | 122 | 20.0 | 22.2 | ch7 (mean Δ0.6 K) | ✓ HOCH |
| a8 DG RAS | 126 | 20.0 | 20.1 | ch8 (mean Δ0.6 K) | ✓ HOCH |
| a9 DG Temp | 130 | 0.0→20.0 | 18.7 | ch9 (mean Δ0.6 K) | ✓ HOCH |

Antwort: Diese Werte kommen aus dem CAN bus von der UVR1611, wo die RAS Geraete angeschlossen sind. Es sind also die gleichen Werte wie in der UVR1611, aber verzoegert und nur 5 Minuten genau gegenueber den 10 Sekunden an der UVR1611

### 2c. dev2 a1–a4 → **R5 GELÖST: ENTFALLEN (dokumentierte NULL)**

> **Befund (Orchestrator, 2026-11-08):** In der dev2-Byte-Region tauchen
> **keine** Speicher-Temperaturwerte
> (≈73/51/48 °C) auf — die liegen nur bei dev0 (21–39). Die Bytes 146–158 halten
> Minderer-/Ventil-Temp-Werte (≈29/29/36/23 °C) = **a13–a16** (json:32 ch13–16, ✓ verifiziert).
> R2 hatte deshalb `a1–a4 ≡ a13–a16` (Duplikat-Hack, gleiche Bytes). **Das ist sehr
> wahrscheinlich semantisch falsch:** a1–a3 = T.Speicher (blk58–60) dürften in P181
> **nicht** separat bei dev2 loggen (sondern nur bei dev0), a4 = Keller RAS fehlt ebenfalls.
>
> **R5-Entscheidung (python-pro, 2026-11-08): dev2 a1–a4 = ENTFALLEN → 4
> dokumentierte NULL-Zeilen** (kein Duplikat-Hack mehr). maintainer (2c-Antwort):
> „Alles diese Werte sind hier nur per CAN BUS uebertragen ... die UVR1611 ist
> hier der Master" → die Speicher-Temperaturen werden von dev0 a1–a3 @21–39
> (UVR1611) geführt, a4 (Keller RAS) ist dev1 a1/a2 (RAS). Die Bytes
> @146/150/154/158 bleiben dev2 a13–a16 (Minderer-Temperaturen, value-verified).

| Kanal | P181-Byte (aktuell, R2) | rec0-Wert | P169-Ref | json:32 ch | MEINE FRAGE / Korrektur (maintainer) |
|---|---|---|---|---|---|
| a1 T.Speicher oben | 146 (≡a13) | 29.1 | 73.1 | ch1=73.1 | ⚠ 29.1 ≠ 73. Byte 146 ist a13, nicht a1. Wo liegt a1? ODER a1 entfallen (nur dev0)? |
| a2 T.Speicher mitte | 150 (≡a14) | 28.9 | 51.0 | ch2=51.0 | ⚠ dito |
| a3 T.Speicher unten | 154 (≡a15) | 35.8 | 48.2 | ch3=48.2 | ⚠ dito |
| a4 Keller RAS | 158 (≡a16) | 22.9 | 25.7 | ch4=25.7 | ⚠ dito |

**Fragen zu 2c (entscheidend für a1–a4):**
1. Loggen dev2 a1–a3 (T.Speicher) in P181 noch **eigenständig**, oder werden die
   Speicher-Temperaturen jetzt **nur** über dev0 (a1–a3 @21–39) geführt → dev2 a1–a3 = entfallen/0?
2. a4 (Keller RAS): eigener Slot vorhanden, oder entfallen?
3. **Wenn a1–a4 einen eigenen P181-Slot haben:** welchen Byte-Offset? (json:32 ch1–4
   liefern das Ground-Truth: ≈73/51/48/25.7 °C — ich kann dann gegen jede freie
   P181-Byte die Match-Prüfung laufen lassen.)
4. **Wenn a1–a4 entfallen:** dann a1–a4 = NULL (Channel-Elimination, wie dev2 a10–a12
   in P169 teils NULL) — bitte bestätigen.


Antwort: Alles diese Werte sind hier nur per Can BUS uebertragen. D.h. sie haben nur eine zeitliche Aufloesung von wenigen Minuten - die UVR1611 ist hier der Master


## 3. dev1 (UVR1611 RAS) — 16 analog → **R5 GELÖST (belegt @66–96, Wert-Beleg 100 %)**

> **R5-Befund (python-pro, 2026-11-08):** dev1 a1–a16 liegen im P=181-Record an
> **geraden Byte-Offsets 66–96** (signed i16). Rosetta gegen die zeitlich
> überlappende WinSol P=145-GT (beide P=181-Files, exakte-ts-Alignment,
> ≥500 Samples/Tag): **dev1 a1–a9 + a11–a13 = 100,00 % werteidentisch**
> (2784/2784 am 11-07 + 5755/5755 am 11-08); a10/a14–a16 = konstant 0 (0-Fill,
> "unused"). Die frühere P169-Continuity-Ambiguität (20.0 °C-Plateau) ist damit
> aufgelöst, weil die P=145-GT die Kanäle 1:1 belegt (P145-Layout
> a1@46 … a13@78, a10 fehlt dort). Scales: a1/a2/a4/a5/a7/a8 = /10 °C,
> a3/a6/a9 = /1 roher Zähler, a11/a12/a13 = /10 Minuten (Datenalter, = P169
> dev1 a11–a13 Referenz). **M1-RAS-Features (ras_*_ist/age) ab 16:14 = real
> (nicht mehr NULL).**

> **Warum PENDING:** dev1-RAS hat **kein Live-JSON-Ground-Truth** (json:1 = UVR1611-Heizkreis,
> RAS ist nicht im JSON-API-Node-1-logging), und die P169-Continuity ist **ambigu**: die
> RAS-Werte (Keller 25.7 / EG 22.2 / DG 20.1 °C + Datenalter a11–a13) zeigen auf Bytes,
> die **bereits dev0/dev2-Kanälen** zugeordnet sind (z. B. byte 43 = dev0 a12). → NICHT geraten.

P169-Referenz-Semantik (bekannt, aus M1-Config) + **R5-Besetzung im P=181**:
| P169 dev1 | Semantik | P181-Slot (R5) | Beleg (R5) |
|---|---|---|---|
| a1 | ras_kell_perc | **@66, /10 °C** | P145 a1, 100 % (11-07 + 11-08) |
| a2 | ras_kell_ist | **@68, /10 °C** | P145 a2, 100 % |
| a3 | (ungenutzt, Zähler) | **@70, /1 raw** | P145 a3, 100 % (signed raw, z. B. −391) |
| a4 | ras_eg_perc | **@72, /10 °C** | P145 a4, 100 % |
| a5 | ras_eg_ist | **@74, /10 °C** | P145 a5, 100 % |
| a6 | (ungenutzt, Zähler) | **@76, /1 raw** | P145 a6, 100 % |
| a7 | ras_dg_perc | **@78, /10 °C** | P145 a7, 100 % |
| a8 | ras_dg_ist | **@80, /10 °C** | P145 a8, 100 % |
| a9 | (ungenutzt, Zähler) | **@82, /1 raw** | P145 a9, 100 % |
| a10 | (unused) | **@84, 0-Fill** | full-day konstant 0 (beide Files) |
| a11 | ras_age_kell (Datenalter) | **@86, /10 min** | P145 a11, 100 % |
| a12 | ras_age_eg | **@88, /10 min** | P145 a12, 100 % |
| a13 | ras_age_dg | **@90, /10 min** | P145 a13, 100 % |
| a14–a16 | (unused) | **@92/94/96, 0-Fill** | full-day konstant 0 (beide Files) |

**Fragen zu dev1:** (1) Wo liegen dev1 a1–a16 im P181-Record (Byte-Offs)? (2) Sind die
RAS-Labels unverändert gegenüber P169 (Keller/EG/DG)? (3) Gibt es ein neues RAS-Logging-
Schema? — **Ohne deine Labels: dev1 bleibt NULL (M1-RAS-Features degradieren zu NULL,
kein Crash, P169-Files unverändert).**

Antwort:

a1: Offset Keller
a2: Temperatur Keller
a3: ungenutzt
a4: Offset Erdgeschoss
a5: Temperatur Erdgeschoss
a6: ungenutzt
a7: Offset OG/DG
a8: Temperatur OG/DG
a9, a10: unused
a11: Keller - alter der Daten in einer Zeiteinheit
a12: EG - alter der Daten in einer Zeiteinheit
a13: OG/DG - alter der Daten in einer Zeiteinheit

Rest ist unused

Dies sind die sauberen Daten in der UVR16111 zu den drei RAS Drehknopefen.

## 4. Digital → **R5 GELÖST (dev0 d2–d9 + dev2 d1 belegt; dev0 d1 loggt, nicht persistiert)**

> **R5-Befund (python-pro, 2026-11-08):** Das dev0 digital Layout ist per
> **WinSol P=145-GT byte-belegt**. WinSol hat am 11-07 um 16:15:59 (exakt zum
> UVR-Upload-Zeitpunkt) das Log-Format P=137 → P=145 (+8 B) gewechselt und dabei
> den dev0/dev1-Block 4 B früher gesetzt: **dev0 digital Mask u16 @ byte 44**
> (P=137: @48; Bit-Permutation `DEV0_DIGIT_BITS` = {d8:bit0, d7:bit1,
> d6:bit2, d5:bit3, d4:bit4, d3:bit5, d2:bit6, d9:bit15} bleibt erhalten). Die
> P=181-Kanäle matchen die P=145-Maske mit **100,00 % werteidentisch +
> Toggle-Konsistenz** (11-07: 2784, 11-08: 5753 gemeinsame ts). **dev2 d1
> (Solarpumpe) = byte 174 bit0** (Korrektur: R2 @byte 10 war dev0 d3,
> Mischkreis Auf) — 100,00 % vs P=145 dev2-Byte @144 bit7 (2784/2784 +
> 5753/5753) UND 100 % vs json:32 ch1 D (182/182 + 448/448).

| Kanal | P181 (R5) | Beleg (R5) | Konfidenz | maintainer (vorher) |
|---|---|---|---|---|
| dev0 d2 (Pumpe WW zum Puffer) | byte 6, bit0 | P145 mask bit6; full-day konstant 0 (beide Files) | HOCH | d2 = Pumpe WW zum Puffer ✓ |
| dev0 d3 (Mischkreis Auf) | byte 10, bit0 | P145 mask bit5, 100 % + 28/104 Toggles | HOCH | ⚠ maintainer hatte d3=Zu/d4=Auf getauscht → **Rosetta: d3=Auf, d4=Zu** (wie P169 byte113/114 golden) |
| dev0 d4 (Mischkreis Zu) | byte 11, bit0 | P145 mask bit4, 100 % + 22/32 Toggles | HOCH | (s. o.) |
| dev0 d5 (Anforderung Brenner) | byte 12, bit0 | P145 mask bit3, 100 % + 1/0 Toggles | HOCH | d5 = Anforderung Oelbrenner ✓ |
| dev0 d6 (Pumpe WW Zirkulation) | byte 13, bit0 | P145 mask bit2, 100 % + 20/18 Toggles | HOCH | d6 = Pumpe WW zur Zirkulation ✓ |
| dev0 d7 (Zirkulationspumpe) | byte 14, bit0 | P145 mask bit1, 100 % + 20/18 Toggles | HOCH | d7 = Zirkulationspumpe ✓ |
| dev0 d8 (Pumpe Heizkreis) | **byte 15**, bit0 | P145 mask bit0, 100 % + 3/2 Toggles (intermittent) | **HOCH (Rosetta-golden)** | ⚠ **maintainer: d8=Heizkreispumpe @byte16** → **Widerspruch, s. u.** |
| dev0 d9 (Ventil Heizungsrücklauf) | **byte 16**, bit0 | P145 mask bit15, 100 %, const-ON-Signatur (= P169 byte119) | **HOCH (Rosetta-golden)** | ⚠ maintainer: „d9 ist das Ventil Heizungsrücklauf (ist das verschoben?)" → **Rosetta: d9 @byte16 = Ventil, unverändert** |
| dev0 d1 (Pumpe Holzofen) | byte 8, bit0 | P145 mask bit7, 100 % + 161/76 Toggles | **verifiziert, ABER: kein Store-Spalten (`dev0_digital_01` fehlt) → nicht persistiert** | — |
| dev2 d1 (Solarpumpe) | **byte 174**, bit0 | P145 @144 bit7 100 % + json:32 ch1 D 100 % (Korrektur R2 @byte10) | HOCH | „Ja - stimmt" (byte10 war falsch) |
| dev1 d1–d13 | 0.0-Fill | wie P=169 (kein Payload, LogX GT konstant 0) | HOCH | „dev1 d1–d13 alle unbelegt" ✓ |

> **⚠ d8/d9-Identität (offene HITL-Frage, Rosetta-golden implementiert):**
> Die P=169-Rosetta-golden (byte118 = d8 Pumpe Heizkreis, byte119 = d9 Ventil
> Heizungsrücklauf; P137 bit0/d8 vs bit15/d9, 99,8–99,9 % über den 11-07-
> Morgen) ketten sich über die P137→P145→P181-Layoutwechsel zu **P181
> byte15 = d8 (Pumpe, intermittent) / byte16 = d9 (Ventil, const-ON)**; die
> Signaturen stimmen überein (byte15 intermittent, byte16 const-ON am 11-08
> 6859/6859). the maintainer's q125-Doku („byte16 = d8 Pumpe, d9 Ventil verschoben")
> wird dadurch **widersprochen** → R5 implementiert die Rosetta-golden-
> Belegung; die Abweichung wird vor Deploy als #1-HITL-Open-Item an
> maintainer/Orchestrator adressiert.

## 5. Wie du am einfachsten korrigieren kannst

- **Minimal:** Nur die `Korrektur (maintainer)`-Spalten ausfüllen / „OK" schreiben.
- **Präzise:** P181-Byte-Offset + Scale + Label je Kanal (falls du WinSol-LogX / UVR-Firmware
  zur Hand hast).
- **Dev1-RAS + dev0 d2–d8 + dev2 a1–a9** sind die kritischen drei Blöcke — die bestimmen,
  ob M1/RAS-Features ab 16:14 real sind oder NULL bleiben.

**Nächster Schritt nach deiner Korrektur:** R5-Subagent (python-pro) flippt alle deine
Korrekturen in `_CHANNEL_MAP_181` + PENDING-Set + Tests + Docs; dann Deploy
(Rebuild Pop!_OS + Pi + Re-Backfill ab 16:14 + Cache-Regeneration).

## 6. Offene Blöcke → **R5-Status (2026-11-08)**

> **R5 hat alle drei kritischen Blöcke gelöst** (Wert-Beleg s. §3/§4/§2c).
> Im Decoder: **73 Zeilen/Record = 69 mit Wert + 4 PENDING-NULLs** (dev2 a1–a4).

| Priorität | Block | R5-Status | Konsequenz |
|---|---|---|---|
| 1 | **d8/d9-Identität @byte15/16** (§4) | **OFFEN (HITL)**: Rosetta-golden implementiert (byte15=d8 Pumpe, byte16=d9 Ventil); the maintainer's „byte16=d8" wird widersprochen | Vor Deploy bestätigen: d8/d9-Labels — bei maintainer-Korrektur sind byte15/16 getauscht (surgical-Flip, 2 Zeilen). |
| 2 | **dev0 d1 (Pumpe Holzofen) @byte8** | **verifiziert (100 %), ABER nicht persistiert** (Store-Schema hat keine `dev0_digital_01`-Spalte; 73-Zeilen-/74-Spalten-Gate) | Kann im Log dekodiert werden, wird aber nicht gespeichert (keine Spalte). Store-Schema-Change = out-of-scope (R6/Kandidat). |
| — | dev1 RAS a1–a16 (§3) | **✓ GELÖST** @66–96 (100 % Rosetta vs P=145) | M1-RAS-Features ab 16:14 = real. |
| — | dev0 d2–d9 (§4) | **✓ GELÖST** @6/10–16 (100 % + Toggle) | Alle 8 UVR1611-Schalter ab 16:14 = real (d9 = Ventil). |
| — | dev2 a1–a4 (§2c) | **✓ GELÖST: entfallen** (4 dokumentierte NULLs) | Kein Duplikat-Hack mehr; Speicher-Temp läuft über dev0. |
| — | dev2 d1 (Solarpumpe) | **✓ GELÖST** @byte174 (Korrektur R2 @byte10) | Solarpumpe ab 16:14 = real (100 % vs P145 @144 bit7 + json:32 ch1 D). |
| — | dev0 a1–a16, dev2 a5–a19 | ✓ belegt (Konfidenz HOCH, s. §1/§2) | — |
