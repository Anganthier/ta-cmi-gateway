# UVR1611 + UVR610 — Programm-Analyse der Heizungs-Steuerung

> **Status:** Analysedokument, **4. Revisionsstand (2026-10-31)**, Read-only. Revision 3 korrigiert die
> O-Ausgangs-/CAN-Belegung (Netlist-Revision), schließt **F1** (Diff = 0,1-K-Raster) und **F2**
> (Timer/Schaltuhr in Minuten, Manuel-belegt) und stellt §2.3 (Öl-Logik) auf die definitive Netliste.
> Revision 4 schließt **F3** (Ölbrenner = rein ON/OFF, keine Lastmodulation), **F4** (0–10 V linear
> = 0 % zu → 100 % auf), **F5** (Rücklaufminderer = TA-Compact-P, IMI Hydronic, DN 25, elektrischer
> Stellantrieb, stufenlos), **F6** (inverses Heizkreis-Fg = **bewusste WW-Priorität**, kein Bug) und
> **F8** (Rücklaufminderer **ohne Positions-Sensor** — nur Aktor + PT1000 pro Strang) durch
> HITL-Antworten (maintainer, 2026-10-31, verbatim in §8.1). Basis: TA-Designer-Programme (`.tdw`) +
> RAW-EEPROM-Images (`.dat`, 2022-09-18) + TA-Funktions-Manuals (UVR1611-N A4.05, UVR16x2 v1.50,
> UVR610S 1.22) + `docs/plant-model.md` + `docs/hydraulic-topology.md` + Produkt-Quellen (IMI
> TA-Compact-P).
> **Ziel:** Verständnis des bestehenden Steuerungs-Designs (Wie arbeiten die Programme? Regelung?
> Schwellen?), danach Optimierungs-/Redesign-Empfehlungen.
> **Vertrauenslegende:** ✅ = tdw + Manual + (anlageseitig) Plant-Modell deckungsgleich · ⚠️ = Ableitung · ❓ = offen/unsicher.
> **Skalierungs-Note (Manual-belegt, 2. Revisionsstand):** Die `.tdw`-`<wert>`-Elemente sind
> geräteeigene Rasterwerte. Die UVR610-Funktionsgrößen-Tabelle (Manual UVR16x2 v1.50 / UVR610S 1.22)
> belegt: FktGr-Index 4 = **Temperatur °C (1 Nachkommastelle)** → dort ist Wert 200 = 20,0 °C.
> Die UVR1611-Temperatur-Schwellen (VLMax=70, VLMin=25, Ladepumpe Min=65/Max=90, Vergleich
> Wb=52/60 …) sind **direkt °C** (Manual-Beispiel „Min-Schwelle = 60 °C" = Rohwert 60).
> Die **Diff-Parameter sind 0,1-K-Raster** (Manual-Beispiel Ladepumpe: „DIFF.EIN = 5.0 K"
> bei Rohwert 50) → Diff.Ein=30 = **3,0 K**, Diff.Aus=90 = **9,0 K** (✅, **F1 geschlossen**).
> **Timer-Zeiten (F2, gelöst):** Manual UVR1611 „Timer": Impulszeit „bis 90 Sekunden in
> Sekunden-schritten und darüber in verschiedenen Abstufungen bis zu 48 Stunden". Das
> Rohwert-Raster ist damit ≤90 = Sekunden, >90 = **Minuten** (Max 48 h = 2880 min) →
> Timer `Ein=97` = **97 min**, `Ein=95` = **95 min** (⚠️ Ableitung mit hoher Konfidenz).
> **Schaltuhr-/Zeitfenster-Raster (F2, gelöst):** Zeitfenster-Rohwerte = **Minuten**
> (0–1439 = 24 h): alle Zeitfenster beider Programme bilden konsistente Uhrzeiten (z. B.
> Heizkreis `ein0=36/aus0=135` = 06:00–22:30; Zirkulation-Schaltuhr `ein1=105/aus1=129`
> = 17:30–21:30; WW-Anforderung `ein1=75/aus1=132` = 12:30–22:00). `wt` = Wochentags-
> Bitmaske (1=Mo, 2=Di, 4=Mi, 8=Do, 16=Fr, 32=Sa, 64=So; 31 = Mo–Fr, 96 = Sa+So,
> 11 = Mo+Di+Do, 64 = nur So, 16 = nur Fr).

---

## 0. Quellen & Analyse-Grundlage

| Datei | Ära | Rolle |
|---|---|---|
| `house-heating/20211020_owner.tdw` | 2021-10-20 | **UVR1611-Hauptprogramm** (CAN-Node 1), TA-Designer v1.0.14. 26 Funktions-Module + 11 Analogeingänge + 9 Schaltausgänge + 2 Analogausgänge + Netzwerk + CanDatLog. |
| `house-heating/UVR610_32_20211029.tdw` | 2021-10-29 | **UVR610-Programm** (CAN-Node 32), Solar + RAS-Logger + 3× Rücklaufminderer (Analog-Ausgänge). 7 Funktions-Module. |
| `house-heating/20220918_owner.dat` | **2022-09-18** | RAW-EEPROM (D1.00) UVR1611 = aktuelles Image. BDB-Labels = 1:1 mit 2021er `.tdw` → Belegung identisch (Staleness-Check plant-model §6). |
| `house-heating/UVR610_32_20220918.dat` | **2022-09-18** | RAW-EEPROM (FW T1.19) UVR610. +3 BDB-Labels (Keller/EG/OG HzRkl Ventil), Belegung der Kanäle unverändert. |
| `Manual_UVR1611-N_A4.05_A5.02_Funktionen.pdf` | 2015 | UVR1611-Original-Funktionsdoku (Heizkreisregler, Anforderung HZ/WW, Ladepumpe, Timer, Logik, Analog, Schaltuhr, Vergleich, Zähler). |
| `Manual_UVR16x2_Funktionen_V1.50.pdf` / `Manual_UVR610S_Funktionen_1.22.pdf` | 2021 | UVR16x2/UVR610-Funktionsdoku (Solarregelung, Mathematik, Vergleich, Funktionsgrößen-Tabelle). |
| `docs/plant-model.md` / `hydraulic-topology.md` | 2026-10 | Kanon: Kanal-Belegung (winsol:0/1/2), Hydraulik (5 Loops), 9 Whitelist-Adressen. |

**Methode:** Die `.tdw`-XML (TA-Designer „Camel"-Format) wurde strukturiert geparst:
- **Module** = `<o classname="u1611*/u16x2*">` (Funktionsbausteine), jeweils mit `<wert>` (Parameter),
  `<ev>` (Eingangsvariablen: `qu`=Quelltyp, `fid`=Quell-Modul, `wert`=Fixwert, `idx`=Kanal-Index)
  und Pins (`FunkInPin`/`FunkOutPin`/`TacoPin`).
- **Verdrahtung** = `<o classname="a2dWirePolylineL">` (Wires), deren `TaPin begin/end` per
  `resolveid` auf die Pin-Elemente zeigen. Zusätzlich **Namen-Netze** (`u1611TacoSignal`,
  „Signal-Brücken"), die logische Busse zwischen Modulen darstellen (z. B. „Abgas Holz AN").
- **I/O-Belegung** = `block=Eingaenge/Ausgaenge` (`<ein nr=.. bnr=..>`, `<aus nr=..>`) und
  CAN-`<nwe>` (Netzwerk-Events) bzw. UVR610 `block=CAN/DL/DatLog`.
- **Parameter-Semantik** aus den TA-Manuals (Modul-für-Modul).

Ergebnis: **Beide Programme sind vollständig in Struktur + Parametrierung rekonstruiert.**
Die UVR1611 ist die Hauptsteuerung (Grundregelung + Heizkreis + Öl-Brenner + WW + Zirkulation +
Mischkreis-Regelung). Die UVR610 ist Spezialist (Solar-Pumpe + RAS-Logger + 3× Rücklaufminderer).

---

## 1. Das Design-Prinzip (TA-Designer / CAN-Touch)

**Wie werden UVR1611/UVR610 „programmiert"?** Es gibt keinen klassischen Code (C/ASM), sondern ein
**graphisches Funktionsblock-System** (TA-Designer, „Camel"-Kaskade), das auf dem CAN-Bus
(CAN-Touch) läuft. Ein Programm = eine **Netzwerk von Funktions-Modulen**, die über
Eingangs-/Ausgangsviervariablen und Signale verdrahtet sind.

```
 ┌──────────────────────────────────────────────────────────────────────┐
 │  EIN GANG  (11× UVR1611 / 4× UVR610 lokal)          │  AUSGANG       │
 │  Sensor PT1000 → Analogwert,  Digital → EIN/AUS    │  Schalter,     │
 │  (dev0 a1..a16, dev2 a1..a16)                     │  Pumpe, Ventil, │
 │                                                    │  0–10 V        │
 └──────────────┬─────────────────────────────────────┴────────┬───────┘
                │                                              │
        ┌───────▼──────────────────────────────────────────────▼───────┐
        │              F U N K T I O N S - M O D U L E                 │
        │  Heizkreisregler · Anforderung HZ/WW · Ladepumpe ·           │
        │  Solarregelung · Vergleich · Logik · Timer · Schaltuhr ·     │
        │  Analog · Mathematik · Zähler  (max. 128 Instanzen)         │
        │  jedes Modul: Eingangsvariablen + Parameter → Ausgangsvariablen│
        └───────┬──────────────────────────────────────────────┬───────┘
                │                                              │
        ┌───────▼──────────────────────────────────────────────▼───────┐
        │  SIGNALE (TacoSignal)  +  CAN-Netzwerk (nwe)                │
        │  logische Busse + Cross-Node-Events (UVR1611↔UVR610)        │
        └───────┬──────────────────────────────────────────────┬───────┘
                │                                              │
        ┌───────▼──────────────┐                      ┌─────────▼──────────┐
        │  Logging (CanDatLog/ │                      │  BDB (Events, 12)  │
        │  DatLog 30 s) → dev0/ │                      │  Meldungen/Benutzer │
        │  dev1/dev2 (WinSol)  │                      │  -Anzeigen          │
        └──────────────────────┘                      └────────────────────┘
```

**Schlüsseleigenschaften des Designs:**
- **Declarative Funktions-Blöcke:** Jedes Modul (z. B. „Heizkreisregelung", „Ladepumpe",
  „Vergleich") ist eine **fertig-parametrierte Regel-/Schalt-Function** aus dem TA-Katalog
  (43 Typen bei UVR610). Der Programmierer wählt Typ, legt Parameter (Schwellen, Hysteresen,
  Zeitfenster, Kennlinien) ein und **verdrahtet** Eingangs- und Ausgangsvariablen (Sensoren,
  andere Module, Fixwerte, CAN-Netzwerkeingänge).
- **Freigabe (Fg) pro Modul:** Jedes Modul hat einen `Freigabe`-Eingang (EIN/AUS), mit dem es
  zur Laufzeit (per CAN/Netzwerk/Signal) an- oder abgeschaltet wird. Das ist der zentrale
  „Schalter" für KI-Interventionen (siehe §7).
- **CAN-Netzwerk (nwe / NWEA):** Geräte tauschen **analoge** und **digitale** Werte über
  CAN. Die UVR1611 (Knot=1) *publiziert* `NWAA1/2/3` (Speicher Oben/Mitte/Unten) und
  `NWAA10..15` (RAS, 3× Ist + 3× Offset), die die UVR610 (Knot=32) *abruft*
  (`nwe A1..A3` + `A10..A15`). Das erklärt die doppelte RAS-Belegung: RAS-Werte werden
  von der UVR1611 (aus CAN ch17/18/20/21/23/24) berechnet, veröffentlicht und von der
  UVR610 per CAN abgezogen.
- **Logging (dev0/1/2):** `CanDatLog` (UVR1611: DS1→dev0, DS2→dev1) und `DatLog` (UVR610: dev2,
  30-s-Intervall) schreiben die analogen/digitalen Kanäle fortlaufend in den TA-Logger
  (WinSol LogX). Das ist die Datenbasis des KI-Projekts.
- **BDB (Bedienungs-Display):** 12 benannte Events/Anzeigen für das Bedien-Display +
  Benutzer-Gruppen (Anwender/Fachmann/Experte).
- **Fachmann-Sperre:** Parameter sind pro Benutzergruppe geschützbar (UVR1611: Code `2^6` für
  „alle Einstellparameter").

**Konsequenz für das KI-Projekt (S6):** Die Steuerung ist **nicht** ein geschlossener PID,
sondern eine **Diskrete-Logik + Regelungs-Netzwerk** aus Schaltern (EIN/AUS), Thermostaten
(Thermostat-Schwellen mit Ein-/Aus-Differenz) und einer **Heizkreis-Grundregelung**
(Außen-Temp → Vorlauf-Soll via Heizkurve). Die einzigen „reglerischen" Grade-of-Freedom sind:
(1) die 9 CMI-Whitelist-Schalter (Freigaben/Handbetriebe), (2) die **RAS-Offsets** (dev1, per
UVR1611/Netzwerk setzbar, v2), (3) die **Heizkurven-Parameter** (Vorlauf-Kurve, per UVR1611-
Programmupload anpassbar, v2). Das passt exakt zum S6-Design (L2 optimiert über Schalter +
Offsets + Kurven-Parameter, nicht über „einen PID-Gain").

---

## 2. UVR1611 (CAN-Node 1) — Hauptsteuerung

### 2.1 I/O-Belegung (dev0 = `winsol:0`)

**Analogeingänge** (PT1000, `block=Eingaenge`, `styp=2`):

| Ein-Nr | BNR | dev0-Kanal | Messpunkt | Rolle (verdrahtet, 3. Revisionsstand = definitive Netliste) |
|---|---|---|---|---|
| 1 | 12 | a1 | Speicher Oben | **WW-Soll** (AnfWW.WW, „WW-Kern") + **Öl-Ladepumpe-Komparator-Wb** (Vergleich#1635…) + publiziert `NWAA1` → UVR610 A1 (Solar-Begrenzung „Top") |
| 2 | 11 | a2 | Speicher Mitte | **O9-Komparator-Wa** (Vergleich#11223559: a2 > a7 + 5 K → O9 „Vent. HzRL") + publiziert `NWAA2` → UVR610 A2 (nicht weiterverdrahtet) |
| 3 | 10 | a3 | Speicher Unten | **Ladepumpe-Referenz** (Ref, „darf nachladen, wenn < 90 °C") + publiziert `NWAA3` → UVR610 A3 (Solar-Referenz) |
| 4 | 8 | a4 | Warmwasser (WW) | WW-Komparator-Wa (Vergleich#11246068, WW < 52 °C → O6) |
| 5 | 1 | a5 | Ölkessel Vorlauf | **AnfHeiz.AnfT (a5) UND AbT (a5)** → „gleitender Kesselbetrieb" + **Öl-LL-Komparator-Wa** (Vergleich#1635…Wa) |
| 6 | 6 | a6 | Heizkreis Vorlauf | **Heizkreis.VL (Ist)** — Grundregelung (VL-Mittelwert/VLR) |
| 7 | 7 | a7 | Heizkreis Rücklauf | **O9-Komparator-Wb** (Vergleich#11223559: a2 > a7 + 5 K → O9 „Vent. HzRL") |
| 8 | 15 | a8 | Außen | **Heizkreis.AT (Ist)** — Grundregelung (Heizkurve) |
| 9 | 5 | a9 | Holzkessel Vorlauf | **Ladepumpe-Zub** (Zubringertemperatur „Holz-Kessel") — an wenn a9 > 65 °C und a9 > a3 + 3 K |
| 10 | 4 | a10 | Abgas (styp=1) | Abgas-Komparator-Wa (Vergleich#11205958) → „Abgas Holz AN/AUS" |
| 16 | 2 | a16 | **Stromungsschalter** (typ=1, digital) | WW-Zirkulations-Präsenz (Logik#11115800.EV1 + Timer#11217412.Trig) |

**Schaltausgänge** (`block=Ausgaenge`, `lz=92` = 9,2 s Nachlaufzeit? bzw. Einschaltverzögerung):

| Aus-Nr | dev0-Kanal | Funktion (verdrahtet, 3. Revisionsstand = definitive Netliste) | CMI-Whitelist |
|---|---|---|---|
| O1 | d1 | **Holz-Ladepumpe** (a9 Holzkessel-VL > 65 °C **und** a9 > a3 + 3 K **und** a3 < 90 °C → Puffer nachladen) — `FunkLadepumpe#11238988.Pump` (BDB bdb1 „Pumpe Holzo.") | `10120A10180` |
| O2 | d2 | **Öl-Kessel→Puffer-Ladepumpe „ÖL-L"** (Vergleich#1635…: a5 Ök-VL > a1 Speich-Oben + 24,6 K, Daus=22,6 K; Fg = Timer-Signal „Anf. Ladepumpe WW" **invertiert**; CMI-Label „Ladepumpe WW", BDB bdb2) | `10620A10180` |
| O3 | d3 | **Mischkreis Auf** (Heizkreisregler `Misch`, dig=3) | ❓ nicht setzbar (nur dev0 d3/d4 lesbar) |
| O4 | d4 | Mischkreis Zu (nicht im .tdw verdrahtet) | ❓ |
| O5 | d5 | **Öl-Brenner-Freigabe** (ODER aus `AnfHeiz.Anf` + `AnfWW.Anf`: Brenner an bei Heiz- **ODER** WW-Anforderung. Zähler#11118676.EV1 liest ch5 zurück = Öl-Brenner-Betriebsstundenzähler, BDB bdb5 „Oel CNT") | `10520A10180` **RISK** |
| O6 | d6 | **WW vom Öl** (Vergleich#11246068.Kl: WW a4 < 52 °C) | `10220A10180` |
| O7 | d7 | **Zirkulationspumpe** (Logik#11115800.Erg: ODER aus a16 Stromungsschalter + Nachlauf 97 min + Schaltuhr-Fenster; Fg = „Kein Urlaubsmodus") | `10720A10180` |
| O8 | d8 | **Pumpe Heizkreis** (Heizkreisregler `HKP`, BDB bdb9 „P-Heizkreis") | `10820A10180` |
| O9 | d9 | **Ventil Heizungsrücklauf** (Vergleich#11223559.Gr: a2 Puffer-Mitte > a7 Hz-RL + **5 K**; Fg = NOT O2) | `10920A10180` |
| O14 | – | (dig=4, inaktiv) | – |
| O15/O16 | – | **Analog-Ausgänge 0–10 V** (typ=2, ana=1, mgrs2=100/pgrs2=200/spg=200) | **inaktiv/ungebraucht** (im UVR1611-Programm nicht verdrahtet) |

> **Korrektur zum 1. Revisionsstand (wichtig!):** Die **Holz-Ladepumpe** steuert **O1**
> (nicht O2): `FunkLadepumpe#11238988.Pump → AusSchalt ch1` = d1 „Pumpe Holzo." (BDB bdb1,
> CMI `10120A10180`). **O2 (d2)** ist die **Öl-Ladepumpe** (`Vergleich#1635….Gr → AusSchalt ch2`,
> CMI-Label `10620A10180` „Ladepumpe WW" — das Setup-Label bezeichnet die *Funktion* „Ladepumpe
> Warmwasser", der physikalische Kanal ist aber O2 = Öl→Puffer-Ladepumpe). **O9 (d9)** ist das
> **Ventil Heizkreis-Rücklauf** (`Vergleich#11223559.Gr`, CMI `10920A10180` „Vent. HzRL").

> **Wichtig (Korrektur zu `hydraulic-topology.md` §3/RM):** Die **3× Rücklaufminderer (0–10 V)**
> sitzen **Nicht** auf den UVR1611-Analogausgängen O15/O16 — diese sind im UVR1611-Programm
> **nicht verdrahtet** (inaktiv). Sie werden von der **UVR610** über ihre **3× DL-Analogausgänge**
> (`u16x2TacoDLAusgang #1/#2/#3`) geregelt (siehe §3). Das ist ein **Design-Fehler-Kandidat**
> bzw. eine bewusste Trennung (UVR610 = „Raum-Optimierer", UVR1611 = „Grundregelung").

**CAN-Netzwerk (UVR1611 als Publisher, `block=Netzwerk`):**
- `Knot=1`, `FgNW=1` (Freigabe Netzwerk aktiv), `Auto=1`.
- **NSB-Sende-Intervalle:** `DAInt=5` s (Digital), `AA0Int..AA3Int=5` s (Analoge Gruppen 0–3,
  `Send=30` → Sende-Abstand 30 s?), `DABl/AA0Bl..AA3Bl=10` (Blockade?). ⚠️ genaue Semantik ❓.
- **Timeouts** `TODE0..3=60`, `TOAE0..3=60` (60 s, „nach 60 s ohne CAN-Antwort = Störung").
- **Publizierte Analog-Netzwerkeingänge (NWAA):**
  - `NWAA1 (qu=1, idx=1)` = dev0-Eingang 1 = **a1 Speicher Oben**
  - `NWAA2 (qu=1, idx=2)` = **a2 Speicher Mitte**
  - `NWAA3 (qu=1, idx=3)` = **a3 Speicher Unten**
  - `NWAA10` = NWAusgang ch26 = `FunkAnalog#fid18.Erg`, Quelle **CAN ch17** (RAS-Zone 1)
  - `NWAA11` = ch27 = `fid19.Erg`, Quelle **CAN ch20** (RAS-Zone 2)
  - `NWAA12` = ch28 = `fid20.Erg`, Quellen **CAN ch18** (RAS-Zone 3)
  - `NWAA13` = ch29 = `fid21.Erg`, Quelle **CAN ch21** (RAS-Zone 4)
  - `NWAA14` = ch30 = `fid22.Erg`, Quelle **CAN ch23** (RAS-Zone 5)
  - `NWAA15` = ch31 = `fid23.Erg`, Quelle **CAN ch24** (RAS-Zone 6)
  - **Achtung Interleaving:** Die 6 RAS-Kanäle sind **nicht** fortlaufend: `ch17/18` (Zone-1-
    Paar), `ch20/21` (Zone-2), `ch23/24` (Zone-3). **ch17/ch20/ch23** speisen zusätzlich die
    MIN-Modul-Eingänge (→ „RAS Min Temp" → RT, §2.3.6); ch18/ch21/ch24 sind die Partner-Kanäle.
    Zone↔Raum-Zuordnung (Keller/EG/OG) und Ist-vs-Offset je Kanal = **❓ F7** (WinSol-Setup
    dev1-Labels vs. Programm-Semantik; Messung nötig).
  - + `NWEA11/12/13` = RAS-Status-Events (Knot 1/2/3, `aus=5`)

> Das ist der **Mechanismus der doppelten RAS-Belegung**: Die UVR1611 filtert die 6
> RAS-CAN-Werte (6× FunkAnalog, `F=2`=FILTER, **reiner Pass-through, Offset=0** — die
> UVR1611 rechnet hier **keine** Ist+Offset-Summe!) und **publiziert** sie als `NWAA10..15`
> (ch26–31). Die UVR610 zieht sie per CAN (`nwe A10..A15`) und loggt sie als dev2 a4..a9.
> **dev1 (UVR1611 DS2, ch17–32) = 10-s** · **dev2 (UVR610 DatLog) = 30-s** — bestätigt den
> S6-Entscheid (dev1 kanonisch, dev2 = Konsistenz-Check).

### 2.2 Funktions-Module der UVR1611 (26)

| # | Modul | id | Typ | Funktion (kurz) | Kern-Parameter (Rohwert → interpretiert) |
|---|---|---|---|---|---|
| 1 | u1611FunkHeizkreis | 10211297 | 1 | **Grundregelung**: Außen→VL-Soll (Heizkurve), Pumpe O8 + Mischer O3, Zeitprogramm, Frostschutz | Norm=20°C, Absenk=16°C, +10°C→VL45, -20°C→VL65, Steil=60, VLMax=70, VLMin=25, Raumeinfluss=70%, Vorh=30 min, RTFrost=5, ATFrost=5, MW-VLR=107(❓), MW-Ab=127(❓), SchwAT-Ab=19, HyAT-Ab=10, SchwAb-Ab=15, HyAb-Ab=10, Ueber=0%, Modus=1 |
| 2 | u1611FunkAnfHeiz | 11245104 | 2 | **Öl-Brenner-Anforderung, „gleitender Kesselbetrieb"**: Proz = Ölkessel-VL (a5), Schwellen beide = Vorlauf-Soll (VLST); an bei a5 < VLST + 3 K, aus bei a5 > VLST + 9 K. **Ölbrenner = rein ON/OFF (0 oder 100 %, keine Lastmodulation — maintainer 2026-10-31)** | Diff.Ein=30(3,0 K ✅), Diff.Aus=90(9,0 K ✅), Sock=0, AbT=fix 6(❓ Sockel/„unbenutzt"), Br=97 (Brennerleistung, **ungenutzt** bei ON/OFF ✅ F3), Oeko=0 (kein Öko-Unterdeckung) |
| 3 | u1611FunkAnfWW | 11228837 | 3 | **WW-Anforderung**: an bei WW-Temp (**a1 Speicher-Oben** = WW-Kern) < Soll (Zeitfenster); `Anf` → O5 | WwMin=51(51 °C), DEin=0, DAus=40(4,0 K), Br=100 (Brennerleistung, ungenutzt bei ON/OFF ✅ F3); Zeitprogramm 5 ZP×2 Fenster |
| 4 | u1611FunkLadepumpe | 11238988 | 4 | **Ladepumpe O1** („Pumpe Holzo."): an wenn **Holzkessel-VL (a9) > 65 °C** und **a9 > a3 (Speicher-Unten) + 3 K** und **a3 < 90 °C** → Puffer aus Holzkessel nachladen | T.Zub.Min=65(65 °C ✅), Diff.Ein=30(3,0 K ✅), Diff.Aus=0, T.Ref.Max=90(90 °C ✅), Diff.Ein(Ref)=10(1 K), Diff.Aus(Ref)=50(5 K) |
| 5 | u1611FunkVergleich ×5 | 11223559/11246068/11205958/11326563/1635142667404835 | 9 | Thermostaten: **11223559** a2>a7+5 K (→ O9) · **11246068** a4<52 (→ O6) · **11205958** a10 Abgas-AN/AUS · **11326563** „Kein Urlaubsmodus" · **1635…** a5>a1+24,6 K (→ O2 Öl-Ladepumpe) | Details §2.3 |
| 6 | u1611FunkTimer ×2 | 11217412/11325631 | 14 | **11217412** Zirkulation-Nachlauf (Mod=0, Ein=97 = **97 min** ✅, Trig=a16) · **11325631** WW-Ladepumpe (Mod=1 Mindestlaufzeit, Ein=95 = **95 min** ✅, Trig=AnfWW.Anf → Signal „Anforderung Ladepumpe Warmwasser") | Ein=97/95 (>90 = min, F2 ✅), Flanke=1/0, Retr=1, Mod=0/1 |
| 7 | u1611FunkLogik ×2 | 11115800/11328897 | 10 | **11115800** ODER (Zirkulation: a16 ODER Nachlauf ODER Schaltuhr → O7) · **11328897** UND (WW-An: „Abgas Holz AUS" UND „Kein Urlaubsmodus" → „WarmWasserAn") | Anz=3/2, F=1(ODER)/2(UND) |
| 8 | u1611FunkSchaltuhr | 11321718 | 15 | **Zeitprogramm Zirkulation** („Zirkulation_Timer", Stat → Logik#11115800.EV3) | Soll=5, BedS=60, BedT=0, BlS=60, BlT=0 |
| 9 | u1611FunkZaehler ×2 | 11118676/11313164 | 17 | **11118676** `EV1`=ch5 (qu=2, Schaltausgang O5) → **Öl-Brenner-Betriebsstunden** (passive Rückführung, BDB „Oel CNT") · **11313164** `EV1`=Signal „Abgas Holz AN" → **Holz-Brenner-Laufzeit** (BDB „Holz CNT") | Anz=1, StImp=0, Res=0, Teil=1 |
| 10–16 | u1611FunkAnalog ×8 | 11325938 + 6× RAS + 1× MIN | 11 | 1× Filter (Öl-VL-Mittel) + 6× RAS-Perzipiert/Ist/Offset (Keller/EG/OG) + 1× MIN-RAS-Temp | Anz=1/3, F=2/1, Off1..6 |

> Die **6× RAS-FunkAnalog-Module** (fid 18–23; alle `Anz=1, F=2`=FILTER, `Off1..6=0`)
> sind **reine, gefilterte Pass-throughs** (zeitliche Mittelwerte) über 6 CAN-Kanäle:
> - fid18 ← **ch17** · fid19 ← **ch20** · fid20 ← **ch18**
> - fid21 ← **ch21** · fid22 ← **ch23** · fid23 ← **ch24**
> - **fid16 (MIN-RAS-Temp):** `Anz=3, F=1` (MIN) über `EV1..EV3` = **ch17, ch20, ch23**
>   (qu=7) → **`RAS Min Temp`** → **Raumtemperatur-Eingang (RT)** des Heizkreis-Reglers.
> Zone↔Raum-Zuordnung (Keller/EG/OG) und „Ist"-vs-„Offset"-Semantik je Kanal = **❓ F7**:
> Programmseitig bewiesen sind nur die 3 MIN-Quellen (ch17/20/23 = die 3 Raumentemperatur-
> Quellen für RT) und die 6 Pass-throughs; die WinSol-Setup-Labels dev1 (a1/a4/a7 „Offset",
> a2/a5/a8 „Temp", dev1 aN = ch(16+N)) sind hier **nicht** eindeutig auflösbar.

> **Interpretation (S6-relevant):**
> - **MIN-RAS-Temp** = min(ch17, ch20, ch23) → **Raumtemperatur-Eingang (RT)** der
>   Grundregelung. D. h. die Grundregelung regelt auf den **kältesten Raum** (konservativ,
>   verhindert Überhitzung wärmerer Räume) → **O2-Optimierung MIN→MAX** (§6).
> - Die UVR1611 **addiert selbst keine Offsets** (FunkAnalog `Off=0`) — die RAS-Offset-Werte
>   (die 3 Partner-Kanäle ch18/21/24, Zone-Zuordnung ❓ F7) werden nur gefiltert und
>   weitergereicht. Die „Perzipiert-Temperatur" (Ist+Offset) wird **upstream** (RAS-System /
>   CAN-Quelle) oder in der WinSol-Beobachtung gebildet; der S6-Hebel „RAS-Offset" (v2) wirkt
>   daher auf die **Offset-Kanäle der RAS-Quelle**, nicht auf ein UVR1611-Modulparameter.
> - Die 6 Werte werden als `NWAA10..15` (ch26–31) **an die UVR610 zurück-übertragen**
>   (Rückkopplung, §3).

### 2.3 Öl-Brenner- & Kessel-Logik (Kern-Regelung, definitive Netliste)

> **Revisionsnote (3. Stand, netlist-bewiesen):** Alle Verbindungen unten stammen aus der
> korrigierten Netzliste (`docs/uvr/uvr1611-netlist-definitiv.txt` bzw.
> `docs/uvr/uvr610-netlist-definitiv.txt`, Wire- + `<connect>`-Auflösung, keine Koordinaten-Union). Die wichtigsten Korrekturen gegen den 1. Stand:
> **O5** ist **ODER** aus Heiz- **und** WW-Anforderung (kein Single-Source), die **Abschaltdiff**
> von **AnfHeiz** bezieht sich auf **a5** (gleitender Kesselbetrieb, nicht a6), und **O9**
> vergleicht **a2 (Speicher-Mitte) > a7 (Heizkreis-RL)** — nicht a5>a7.

#### 2.3.1 Signal-Fluss (verdrahtet)

```
 [a8 Außen] ───────►┌──────────────────────────┐
                    │ u1611FunkHeizkreis (AT)   │  Grundregelung (Heizkurve)
 [a6 Heizkreis-VL] ─►│   + RAS-Min-Temp (RT)    │  RT = MIN(Keller,EG,OG)-Ist
                    │   → VLST (Vorlauf-Soll)  │
                    └────────────┬─────────────┘
                                 │ VLST
        ┌────────────────────────┼─────────────────────────────┐
        │ (SAnf + SAb)          │ (VL = Ist)                    │ (Misch → O3)
        ▼                        ▼                             ▼
 ┌───────────────────────┐                                   [O3 Mischkreis Auf]
 │ u1611FunkAnfHeiz       │  Gleitender Kesselbetrieb:
 │  AnfT = a5 (Ök-VL)     │   AN  wenn a5 < VLST + 3 K (DEin=30)
 │  AbT  = a5 (Kesself.)  │   AUS wenn a5 > VLST + 9 K (DAb =90)
 │  SAnf=SAb=Heizkreis.   │   Oeko=0 (kein Zeitflächen-Öko)
 │         VLST           │   Fg = Benutzer/EIN
 └───────────┬───────────┘
             │ Anf
             │
 ┌───────────┴───────────────────────────────────────────────┐
 │ O5 = ODER( AnfHeiz.Anf , AnfWW.Anf )   →  AusSchalt ch5   │
 │  (+ Zähler#11118676.EV1 liest ch5 = Öl-Brenner-Betriebs-  │
 │    stunden, BDB „Oel CNT")                                │
 └───────────────────────────────────────────────────────────┘
             │
             ▼
   [O5] Öl-Brenner-Freigabe (CMI 10520A10180, RISK)
```

- **AnfHeiz (11245104)** — „gleitender Kesselbetrieb" (Manual-Beleg): `AnfT`=**a5** (Ölkessel-VL,
  ch5), `AbT`=Fixwert 6 (inaktiver 2. Kanal → beide Schwellen beziehen sich auf **T.anf=a5**).
  `SAnf`=`SAb`=**Heizkreis.VLST** (Vorlauf-Soll). `DEin`=30 → **3,0 K** (Einschaltdiff),
  `DAb`=90 → **9,0 K** (Abschaltdiff), `DAnf`=100, `Sock`=0, `Br`=97 (Brennerleistung, **ungenutzt**
  weil Ölbrenner rein ON/OFF ✅ F3), `Oeko`=0,
  `Fg`=Benutzer/EIN. **An** wenn `a5 < VLST + 3 K`; **Aus** wenn `a5 > VLST + 9 K`.
  **Brenner = rein ON/OFF** (maintainer 2026-10-31): keine Lastmodulation, keine Abstufung der
  Brennerleistung (nur 0 oder 100 %).
- **AnfWW (11228837)** — `WW`=**a1** (Speicher-Oben, ch1), `Soll`=Fixwert **57** °C (Benutzer),
  `WwMin`=51 °C, `DEin`=0, `DAus`=40 → **4,0 K**, `Br`=100 (Brennerleistung, ungenutzt bei ON/OFF
  ✅ F3), `Ext`=0; 5 Zeitprogramme.
  `Anf` = O5-ODER-Zweig. `Fg` = Signal **„WarmWasserAn"** (siehe 2.3.4).
- **O5 (ch5)** — getrieben durch **drei** Quellen auf einem Relay-Eingang (Netlist N36/N37/N38):
  `AnfHeiz.Anf`, `AnfWW.Anf` und `Zähler#11118676.EV1`. Letztere ist **qu=2, idx=5** =
  Schaltausgang ch5 (Rückführung auf O5 selbst) → **passiver Betriebsstundenzähler**
  (zählt O5-Einschaltflanken, BDB bdb5 „Oel CNT"), **kein** logischer ODER-Faktor.
  Semantik: **Brenner an bei Heiz- ODER WW-Anforderung.**

#### 2.3.2 Ladepumpe O1 (Puffer-Nachladen aus dem Holzkessel)

**FunkLadepumpe #11238988** → `Pump` → **O1 (ch1)** „Pumpe Holzo." (CMI `10120A10180`):
- `Zub`=**a9** (Holzkessel-VL, ch9), `Ref`=**a3** (Speicher-Unten, ch3), `Min`=Fix **65** °C,
  `Max`=Fix **90** °C. `DEZub`=30 → **3 K**, `DAZub`=0, `DERef`=10 → **1 K**, `DARef`=50 → **5 K**,
  `DEZR`=30 → **3 K**, `DAZR`=0.
- **Semantik:** O1 an, wenn `a9 > 65 °C` **und** `a9 > a3 + 3 K` **und** `a3 < 90 °C`
  → lädt den Puffer aus dem Holzkessel, solange der Holz-Kessel > 65 °C ist und der
  Puffer-Unten < 90 °C (keine Überladung).

#### 2.3.3 O2 Öl-Ladepumpe „ÖL-L" (Öl-Kessel → Puffer-Oben) & O9 Heizkreis-RL-Ventil

- **O2 (ch2)** = **Vergleich #1635142667404835.Gr** (Netlist N33):
  - `Wa`=**a5** (Ölkessel-VL, ch5), `Wb`=**a1** (Speicher-Oben, ch1). `DEin`=246 → **24,6 K**,
    `Daus`=226 → **22,6 K**, `PGr`=1. **An** wenn `a5 > a1 + 24,6 K` (Öl deutlich heißer als
    Puffer-Oben → Öl lädt den Puffer-Oben auf), Hysterese 22,6 K.
  - `Fg` = Signal **„Anforderung Ladepumpe Warmwasser"** (Timer#11325631.Ausg) → O2 wirkt nur
    im Nachlauf-Fenster der WW-Ladepumpe (siehe 2.3.5).
  - CMI-Label **`10620A10180` „Ladepumpe WW"** (Setup-Legende bezeichnet die Funktion, nicht
    den physikalischen Kanal; physikalische WW-Ladepumpe sitzt an O1).
- **O9 (ch9)** = **Vergleich #11223559.Gr** (Netlist N28):
  - `Wa`=**a2** (Speicher-Mitte, ch2), `Wb`=**a7** (Heizkreis-RL, ch7). `DEin`=50 → **5 K**,
    `Daus`=30 → **3 K**, `PGr`=1. **An** wenn `a2 > a7 + 5 K` (Puffer-Mitte wärmer als
    Heizkreis-Rücklauf → Rücklaufventil öffnen, Vorlauf mischen).
  - `Fg` = **ch2-Out, `inv=true`** → O9 ist **nur freigegeben, wenn O2 (Öl-Ladepumpe) AUS** ist
    (exklusive Belegung des gemeinsamen Rücklaufwegs).
  - CMI-Label **`10920A10180` „Ventil HzRL"** (live 2026-10-26: HAND/EIN).

#### 2.3.4 WW-Logik: O6 „WW vom Öl", O7 Zirkulation, Urlaubs-Gate

- **O6 (ch6)** = **Vergleich #11246068.Kl** (Netlist N30): `Wa`=**a4** (WW, ch4),
  `Wb`=Fix **52** °C, `Kl` = `a4 < 52 °C`. `Fg` = **Logik#11115800.Erg** (Zirkulations-Logik O7, s. u.).
  CMI `10220A10180` „WW vom Öl". (Semantik: WW-Sensor unter 52 °C + Zirkulation
  aktiv → „WW vom Öl"-Ausgang; ⚠️ physikalischer Aktor/Kanal ❓.)
- **O7 (ch7)** = **Logik #11115800.Erg** (Netlist N19): **ODER** (`Anz`=3, `F`=1) aus
  `EV1`=**a16** (Stromungsschalter, ch16), `EV2`=**Timer#11217412.Ausg** („Nachlauf_Stroemung"),
  `EV3`=**Schaltuhr#11321718.Stat** („Zirkulation_Timer"). `Fg` = Signal
  **„Kein Urlaubsmodus"** (Vergleich#11326563.Kl). → O7 an, wenn Fluss vorhanden ODER
  Nachlauf ODER im Zirkulations-Zeitfenster **und** kein Urlaubsmodus. CMI `10720A10180`.
- **Abgas-Holz-Gate:** **Vergleich #11205958** `Wa`=**a10** (Abgas, ch10), `Wb`=Fix **10**,
  `DEin`=0, `Daus`=255: `Gr` → Signal **„Abgas Holz AN"** (Kamin aktiv), `Kl` →
  **„Abgas Holz AUS"**. **Zähler #11313164** (`EV1`=Abgas-Holz-AN) = Holz-Brenner-Laufzeit
  (BDB „Holz CNT").
- **Urlaubs-Gate:** **Vergleich #11326563** `Wa`=**FunkAnalog#11325938.Erg** (Filter/Bedienung,
  `EV1`=Fix **131**), `Wb`=0, `DEin`=1, `Daus`=0, `PGr`=0: `Kl` → Signal **„Kein Urlaubsmodus"**.
  Dieses Signal ist `Fg` von **AnfWW** (über „WarmWasserAn"), **Logik#11115800** (O7) und
  `EV2` von **Logik#11328897**. Im Urlaubsmodus werden WW-/Zirkulations-Timer blockiert
  (BDB bdb8 „Urlaub"). ⚠️ exakte Urlaubs-Trigger-Quelle aus `.tdw` nicht 1:1 ableitbar.

#### 2.3.5 Timer-Kette & Öl-Ladepumpe-Nachlauf (Wechselwirkung Heizkreis-Freigabe)

- **Timer #11217412** (Zirkulation-**Nachlauf**, `Mod`=0, `Ein`=97 = **97 min** ⚠️, `Flank`=1,
  `Retr`=1): `Trig`=**a16** (Stromungsschalter). `Ausg` → Signal **„Nachlauf_Stroemung"**
  (→ O7-ODER). `Fg`=„Kein Urlaubsmodus".
- **Timer #11325631** (WW-Ladepumpe-**Mindestlaufzeit**, `Mod`=1, `Ein`=95 = **95 min** ⚠️ (F2)),
  `Flank`=0, `Retr`=1): `Trig`=**AnfWW.Anf**. `Ausg` → Signal **„Anforderung Ladepumpe
  Warmwasser"**. Dieses Signal speist **zwei** Verbraucher (Netlist N61/N63):
  - **Vergleich #1635….Fg** → schaltet O2 (Öl-Ladepumpe) frei (2.3.3).
  - **Heizkreis#10211297.Fg** mit **`inv=true`** (Netlist N12) → **Während die WW-Ladepumpe
    im Mindestlauf (95 min) aktiv ist, ist die Heizkreis-Grundregelung invers** (Fg = NOT Signal).
    D. h. die Öl-Lade-Phase **blockiert die Heizkreis-Grundregelung**.
    ✅ **F6 gelöst (HITL, maintainer 2026-10-31) — bewusste WW-Priorität, kein Bug:** Der
    Heizungsteil reagiert **sehr träge**, Warmwasser wird aber **sehr schnell benötigt** und
    wird daher **priorisiert**. Während der WW-Ladephase (O2 Öl-Ladepumpe aktiv) ziehen
    **zwei Pumpen** (O2 Öl-Ladepumpe + O8 Heizkreis-Pumpe) an dem Anschluss der Ölheizung;
    die Blockade der Grundregelung vermeidet diesen Pumpen-Konflikt. Netlist-Kette und
    physikalische Absicht sind damit beide bestätigt.
- **Rückkopplung:** Die Heizkreis-Grundregelung (VLST) ist wiederum Quelle von `SAnf`/`SAb`
  (AnfHeiz) und `Soll`=57 (AnfWW) — die Öl-/WW-Anforderung folgt dem Heizkurven-Vorlauf-Soll.

#### 2.3.6 RAS-Offset-Regelung & MIN-Raum-Temp (S6-relevant)

- **6× FunkAnalog (fid 18–23)** filtern 6 RAS-CAN-Kanäle (reine Pass-throughs, `F=2`,
  `Off=0`): **fid18←ch17**, **fid19←ch20**, **fid20←ch18**, **fid21←ch21**, **fid22←ch23**,
  **fid23←ch24**. (Achtung: die CAN-Kanäle sind **nicht** fortlaufend — RAS-„Paare" ch17/18,
  ch20/21, ch23/24; Zone↔Raum-Zuordnung und Ist-vs-Offset je Kanal = ❓ **F7**.)
- **FunkAnalog #1635754961948349** (`Anz`=3, `F`=1 = **MIN**, `PGr`=1):
  `EV1`=ch17 (Keller-Ist), `EV2`=ch20 (EG-Ist), `EV3`=ch23 (OG-Ist) → **`RAS Min Temp`**
  → **Raumtemperatur-Eingang (RT)** des Heizkreis-Reglers. Die Grundregelung regelt also auf
  den **kältesten Raum** (konservativ, verhindert Überhitzung wärmerer Räume) →
  **O2-Optimierung MIN→MAX** (siehe §6).
- Die 6 Zonenwerte werden als **`NWAA10..15`** (NWAusgang ch26–31) an die UVR610
  **publiziert** (Rückkopplung, §3). ⚠️ „Ist" vs. „Perzipiert (Ist+Offset)" pro Kanal-
  Zuordnung dev1/dev2 = F7 (WinSol-Setup-Check). Die UVR1611 addiert **keine** Offsets selbst
  (`Off1..6=0`) — der S6-Hebel „RAS-Offset (v2)" greift daher an der RAS-Quelle, nicht in der
  UVR1611.


## 3. UVR610 (CAN-Node 32) — Solar + RAS + Rücklaufminderer

### 3.1 I/O-Belegung (dev2 = `winsol:2`)

**Lokale Analogeingänge** (PT1000, `block=Eingaenge`):

| Ein-Nr | BNR | dev2-Kanal | Messpunkt |
|---|---|---|---|
| 1 | 6 | a1 | T. Speicher oben (lokal) |
| 2 | 8 | a2 | T. Speicher mitte (lokal) |
| 3 | 7 | a3 | T. Speicher unten (lokal) |
| 4 | 1 | a16 | **T. Kollektor** (skorr=-7 → -0,7 K Korrektur) |

**CAN-Netzwerkeingänge** (per CAN von Node 1 = UVR1611, `block=CAN`, `nwe`):

| CAN-Nr | knot | ausg | dev2-Kanal | Quelle (UVR1611) |
|---|---|---|---|---|
| A1 | 1 | 1 | (intern) | UVR1611 `NWAA1` = **Speicher Oben (a1)** → Solar-Begrenzung `Beg` |
| A2 | 1 | 2 | (intern) | `NWAA2` = Speicher Mitte (a2) (im Programm nicht weiterverdrahtet) |
| A3 | 1 | 3 | (intern) | `NWAA3` = **Speicher Unten (a3)** → Solar-Referenz `Ref` |
| A10 | 1 | 10 | a4 | `NWAA10` (ch26) ← `FunkAnalog#18` ← **CAN ch17** (MIN-Quelle 1, Zone 1) |
| A11 | 1 | 11 | a5 | `NWAA11` (ch27) ← `#19` ← **CAN ch20** (MIN-Quelle 2, Zone 2) |
| A12 | 1 | 12 | a6 | `NWAA12` (ch28) ← `#20` ← **CAN ch18** (Partner Kanal Zone 1) |
| A13 | 1 | 13 | a7 | `NWAA13` (ch29) ← `#21` ← **CAN ch21** (Partner Zone 2) |
| A14 | 1 | 14 | a8 | `NWAA14` (ch30) ← `#22` ← **CAN ch23** (MIN-Quelle 3, Zone 3) |
| A15 | 1 | 15 | a9 | `NWAA15` (ch31) ← `#23` ← **CAN ch24** (Partner Zone 3) |

> **Korrektur-Note (3. Stand, netlist-bewiesen):** Die `nwe`-Kanäle **A1..A3** (`knot=1,
> ausg=1/2/3`) sind die **UVR1611-Speicher-Sensoren** (a1 Oben / a2 Mitte / a3 Unten, publiziert
> als `NWAA1..3` = NWAusgang ch17/18/19) — **nicht** RAS. A1 dient der **Solar-Begrenzung**
> („Top"), A3 der **Solar-Referenz** (Speicher-Unten). Die **CAN-Eingänge A10..A15** (`aus=10..15`,
> `NWAA10..15` = NWAusgang ch26–31) sind die **RAS-Werte** (3× Ist + 3× Offset), die die UVR1611
> aus den CAN-Kanälen ch17/18/20/21/23/24 **filtert** (6× FunkAnalog, Pass-through,
> MIN(ch17,ch20,ch23) = Raumtemp-Eingang RT) und an die UVR610 weitergibt. Die **lokalen
> PT1000** (Ein-Nr 1..4) sind redundant die Speicher (a1..a3) + Kollektor (a16). Das passt zur
> `plant-model.md` dev2-Belegung (a4..a9 = RAS, a1..a3/a16 = lokal).

**Ausgänge:**
| Aus-Nr | Typ | dev2 | Funktion |
|---|---|---|---|
| O1 | Schaltausgang (typ=1, `lz=150` ms, `lzbgr=1`) | d1 | **Solarpumpe** (CMI: `10040912091` x2, Node 32) |
| **DL-Ausgang #1** | **Analog (DL-Bus)** | (a10?) | **Rücklaufminderer Keller (RM1, 0–10 V)** |
| **DL-Ausgang #2** | **Analog (DL-Bus)** | (a11?) | **Rücklaufminderer EG (RM2)** |
| **DL-Ausgang #3** | **Analog (DL-Bus)** | (a12?) | **Rücklaufminderer OG (RM3)** |

> **DL-Ausgänge** = „Datenlogger-Bus" analoge Ausgänge (0–10 V), die **separat** von den
> Schaltausgängen geregelt werden. Die 3× Rücklaufminderer (0–10 V, `hydraulic-topology.md`
> RM1/2/3) sind hier. Das ist der **Regelungs-Kern der Raum-Optimierung** (pro Raum).

### 3.2 Funktions-Module der UVR610 (7)

| # | Modul | id | Typ | Funktion | Kern-Parameter (Rohwert → interpretiert) |
|---|---|---|---|---|---|
| 1 | u16x2FunkSolReg | 1648128860391273 | 6 | **Solarregelung**: Differenz-Kollektor/Referenz → Solarpumpe O1 | KMax=1300(130,0 °C), KMaxDE=-100(-10 K), KMaxDA=0, KMinDE=50(5 K), KMinDA=0, RMaxDE=-30(-3 K), RMaxDA=0, BMaxDE=-30(-3 K), BMaxDA=0, DKRE=110(11 K), DKRA=70(7 K), StabZ=0, RefMax=900(90,0 °C), BegMax=800(80,0 °C), KollMin=100(10,0 °C) |
| 2 | u16x2FunkVergleich ×3 | …088703251/…7714173158/…7716619706 | 9 | 3× „RL > 20 °C?"-Thermostaten (je 1 Raum: CAN ch10/12/14) | FktGr=4 (°C mit 1 Nachkommastelle ✅), DEin=1 (0,1 K ⚠️), DAus=0, **WertB=200 = 20,0 °C ✅** |
| 3 | u16x2FunkMathematik ×3 | …6637850105/…7714173287/…7716619846 | 32 | 3× lineares 0–10-V-Signal für Rücklaufminderer (RL-Temp-Abweichung → Ventil-Öffnungsgrad 0–100 %) | FktGr=24, Funk=0, OpAB=0, EVarA=1000000 (erzeugt den linearen 0–10-V-Wert; exakte Funktionsgröße ohne Manual-Bezug, aber **0 % = zu / 100 % = auf** linear ✅ F4/F5) |
| 4 | u16x2TacoDLAusgang ×3 | #1/#2/#3 | – | 3× Analog-Ausgang (DL-Bus, 0–10 V) → RM1/RM2/RM3 (TA-Compact-P, IMI, Stellantrieb) | – |
| 5 | u16x2TacoCANEingang ×9 | #1/#2/#3/#10..#15 | – | CAN-Netzwerkeingänge (RAS-Ist/Offset) | – |
| 6 | u16x2TacoEingang ×4 | #1..#4 | – | Lokale PT1000 (Speicher a1..a3, Kollektor a16) | – |
| 7 | u16x2TacoAusgang ×1 | #1 | – | O1 Solarpumpe | – |

**Solarregelung (FunkSolReg) — im Detail:**
- **Kollektor (Koll)** = Ein-Nr 4 (a16, lokal).
- **Referenz (Ref)** = CAN-Eingang A3 (`nwe knot=1, aus=3`) = **UVR1611 `NWAA3` = Speicher-Unten (a3)**
  (die UVR610 zieht den Speicher-Unten-Wert von der UVR1611).
- **Begrenzung (Beg)** = CAN-Eingang A1 (`nwe knot=1, aus=1`) = **UVR1611 `NWAA1` = Speicher-Oben (a1)**.
- **Einschaltbedingungen (alle 4):**
  1. **T.Koll > T.Koll.Min (10 °C)** und **T.Koll < T.Koll.Max (130 °C)** — Kolloktor muss
     aktiv sein, aber unter Dampf-Grenze.
  2. **T.Koll - T.Ref > Diff.Ein (DKRE=11 K)** mit Hysterese (DKRA=7 K) — die
     Kolloktor-Speicher-Differenz muss > 11 K (Ein) / < 7 K (Aus) sein.
  3. **T.Ref < T.Ref.Max (90 °C)** (RMaxDE=-3 K, RMaxDA=0) — Speicher-Unten muss unter 90 °C
     sein (keine Überladung).
  4. **T.Beg < T.Beg.Max (80 °C)** (BMaxDE=-3 K, BMaxDA=0) — Speicher-Oben muss unter 80 °C
     sein (keine Überhitzung oben).
- **Pumpen-Ausgang:** O1 (d1, Solarpumpe).
- **Semantik:** **Klassische Solar-Differenzregelung** mit Kolloktor-Min/Max, Referenz-Max und
  optionaler Begrenzung (Top-Temp). **Kein Stabilisierungs-Zeitfenster (StabZ=0)**, kein
  Drainback (die UVR610-S-OD-Montage-Manual erwähnt Drainback, aber das Programm nutzt es nicht).
- **PWM/Analog:** O1 hat `awa=0, awe=1000` (PWM-Fenster) → die Solarpumpe kann **Drehzahl-
  geregelt** (PWM) werden, nicht nur ON/OFF. ⚠️ Ob die Drehzahlregelung aktiv ist (über
  einen Analog-/PWM-Ausgang) ist ❓ (im Programm ist O1 als Schaltausgang, aber die
  PWM-Parameter sind vorhanden).

**3× Rücklaufminderer-Logik (Vergleich + Mathematik → DL-Ausgang):**
Pro Raum (Keller/EG/OG) ein Paar:
- **Vergleich** (`Wa` = CAN-Eingang A10/A12/A14 (ch10/12/14) = **Raum-Rücklauf-Temp** von
  der UVR1611, `Wb` = **200 = 20,0 °C ✅** (FktGr-Index 4, 1 Nachkommastelle, Manual-Belegt),
  `DEin=1` (0,1 K ⚠️), `DAus=0`):
  - `AklB (kleiner)` = **RL < (Wb + Diff)** → digital EIN wenn die Raum-Rücklauf unter ~20 °C ist.
- **Mathematik** (`Fg` = Vergleich.AklB, `EVarA=1000000` (Rohwert, ❓ = 100,0000?),
  `FktGr=24` (❓), `OpAB=0` (Addition?)):
  - `Erg` = **0,1 × (RL - 20)** (vermutlich) → **analogen Wert für den Rücklaufminderer**.
- **DL-Ausgang #1/#2/#3** (0–10 V) ← Mathematik.Erg → **Rücklaufminderer RM1/RM2/RM3**.

> **Interpretation (✅ HITL-erweitert, maintainer 2026-10-31):** Die Rücklaufminderer sind **TA-Compact-P
> (IMI Hydronic Engineering)**, druckunabhängiges Regel- und Regulierventil, **DN 25**, mit
> **elektrischem Stellantrieb** (stufenlos, 0–10 V; Messnippel für Durchfluss/Temp/Δp) am
> **Rücklauf jedes Einrohr-Heizkreises** (Keller/EG/OG). Die Logik regelt den **Ventil-
> Öffnungsgrad** (0 % = komplett zu, 100 % = komplett auf, **linear**): die 3 Ventile sind auf
> **100 % voreingestellt** (so, dass der Wärmebedarf pro Strang korrekt ist); die UVR610
> moduliert in **kleinen Schritten** dazwischen. Das ist eine **Raum-abhängige
> Rücklauf-Misch-Regelung** — sie mischt den Vorlauf pro Raum (Raum-RL-Temp als Eingang), um
> Abstrahl-/Überschuss-Verluste zu minimieren (der Rücklaufminderer „minderert" = reduziert den
> Rücklauf, erhöht also den Vorlauf pro Raum).
>
> **Semantik (✅ geklärt):**
> - **Aktor-Typ (✅ F5):** TA-Compact-P DN 25 (IMI Hydronic), elektrischer Stellantrieb, stufenlos
>   0–10 V, lineare Charakteristik. Technische Daten (DN 25): 370–2150 l/h, PN 16, 10–90 °C,
>   Δp 25–400 kPa, Stellantrieb M30×1,5, Ventilhub 4 mm, Messnippel.
> - **0–10-V-Mapping (✅ F4):** **0 V = 0 % = komplett zu, 10 V = 100 % = komplett auf**,
>   **linear** (lineare Regelcharakteristik des TA-Compact-P). Die Ventile sind auf 100 %
>   voreingestellt (korrekter Wärmebedarf pro Strang); die UVR610 moduliert in kleinen Schritten.
> - **Sensoren (✅ F8):** Die 3 Ventile haben **keine Positions-Sensoren** (kein Positions-
>   Feedback); sie können **nur als Aktor** (0–10 V) bedient werden. Das **einzige Feedback** ist
>   der **PT1000 pro Strang** (Raum-Rücklauf-Temp, CAN ch10/12/14). maintainer: „Die drei Ventile
>   können nur als Aktoren bedient werden, haben aber keine Sensoren. Ich habe pro Strang nur
>   einen Extra-Fühler PT1000 angebracht."
> - **Verbleibend offen:**
>   - K1 (plant-model): **Kanalmapping der 3 Kreis-Rücklauf-T** (wo im dev1/dev2 geloggt?) →
>     Vermutung: **dev2 a10..a12** (HzRk-Ventil 2 Ergebnis) bzw. **a13..a15** (Ventil 1).
>     Hinweis: `a10–a15` = **befohlene** (logische) Ausgangswerte, **keine gemessenen**
>     Ventilpositionen (F8).
> - K3 (plant-model): **Sind die 3 Rücklaufminderer CMI-lesbar?** → **Nein** (keine
>   Positions-Sensoren; nur PT1000-RL-Temp als Feedback, F8).

---

## 4. Schwellwert-Kompendium (alle Thresholds, sortiert)

> **Skalierungs-Convention (3. Stand, Manual-Belegt):**
> - **UVR1611-Temperatur-Schwellen** (VLMax=70, VLMin=25, Ladepumpe 65/90, Vergleich Wb=52/60…)
>   = **direkt °C** (Manual-Beispiel „Min-Schwelle = 60 °C" = Rohwert 60).
> - **UVR1611-Diff-Parameter** (Diff.Ein/Aus, DEin/Daus aller Funktionsmodule) = **0,1-K-Raster**
>   (Rohwert/10) → 30 = 3,0 K, 246 = 24,6 K (✅ **F1 geschlossen**, Manual-Beispiel Ladepumpe).
> - **UVR610 FktGr-Index 4 (Temperatur)** = °C mit 1 Nachkommastelle (200 = 20,0 °C) →
>   Solar-Parameter (KMax=1300 = 130,0 °C, RefMax=900 = 90,0 °C) **und** Rücklaufminderer
>   (Wb=200 = **20,0 °C**).
> - **Timer-Impulszeiten** >90 = Minuten (97 = 97 min), **Zeitfenster** = Minuten (135 = 22:30)
>   (✅ **F2 gelöst**). „Rohwert" = der `.tdw`-Wert.

### 4.1 UVR1611 — Heizkreis (Grundregelung)

| Parameter | Rohwert | Interpretiert | Bedeutung |
|---|---|---|---|
| Norm (Raum Normal) | 20 | 20 °C | Raum-Soll Normalbetrieb |
| Absenk (Raum Absenk) | 16 | 16 °C | Raum-Soll Absenkbetrieb |
| +10 (VL bei AT=+10°C) | 45 | 45 °C | Heizkurve-Punkt 1 |
| -20 (VL bei AT=-20°C) | 65 | 65 °C | Heizkurve-Punkt 2 |
| Steil | 60 | 0,60 (❓) | Heizkurven-Steilheit (0,05–2,50) |
| VLMax | 70 | 70 °C | Vorlauf-Max (Obergrenze) |
| VLMin | 25 | 25 °C | Vorlauf-Min (Untergrenze) |
| Einfl (Raumeinfluss) | 70 | 70 % | Raum-Temp-Einfluss auf VL-Soll (0–90 %) |
| Vorh (Vorhaltezeit) | 30 | 30 min | Vorziehen des Schaltzeitpunkts bei -10 °C AT |
| RTFrost | 5 | 5 °C | Raum-Frostschutz-Grenze |
| ATFrost | 5 | 5 °C | Außen-Frostschutz-Grenze |
| MwVLR (Mittelwert-VL-Regelung) | 107 | **10,7 min** (0,1-min-Raster, Manual: „MW-Zeit: 10 Min") | AT-Mittelwert-Zeit für VL-Berechnung |
| MwAb (Mittelwert-Abschaltung) | 127 | **12,7 min** | AT-Mittelwert-Zeit für Pumpen-Abschaltung |
| SchwATAb (AT-Abschalt-Schwelle) | 19 | 19 °C❓ | AT-Max für Pumpen-Abschaltung |
| HyATAb | 10 | 1,0 K❓ | Hysterese AT-Abschaltung |
| SchwAbAb (Absenk-AT-Schwelle) | 15 | 15 °C❓ | AT-Max für Absenkbetrieb-Abschaltung |
| HyAbAb | 10 | 1,0 K❓ | Hysterese Absenk-AT-Abschaltung |
| Ueber (Einschalt-Überhöhung) | 0 | 0 % | Keine Einschalt-Überhöhung |
| Zeitprogramm | ZP0: wt=127, ein0=36/aus0=135/sw0=20; ein1=60/aus1=132/sw1=20 | **06:00–22:30, 7 Tage (wt=127), sw=20 °C**; Zusatzfenster 10:00–22:00 (sw=20 °C) | Normalbetrieb-Fenster (1 ZP, 2 Fenster; Minuten-Raster ✅ F2) |

### 4.2 UVR1611 — Öl-Brenner (Anforderung Heizung)

| Parameter | Rohwert | Interpretiert | Bedeutung |
|---|---|---|---|
| Diff.Ein | 30 | **3,0 K ✅** | Einschaltdifferenz: `a5 < VL-Soll + 3 K` → an (0,1-K-Raster, Manual-Belegt) |
| Diff.Aus (DAb) | 90 | **9,0 K ✅** | Abschaltdifferenz: `a5 > VL-Soll + 9 K` → aus (gleitender Kesselbetrieb, **a5** nicht a6) — große Hysterese |
| Sockel | 0 | 0 °C | Keine Mindesttemperatur |
| Br | 97 | **Brennerleistung, ungenutzt** ✅ (F3) | Ölbrenner = rein ON/OFF (0 oder 100 %), keine Lastmodulation (maintainer 2026-10-31) |
| Oeko | 0 | 0 % | **Kein Öko-Unterdeckungs-Betrieb** |

### 4.3 UVR1611 — WW (Anforderung Warmwasser)

| Parameter | Rohwert | Interpretiert | Bedeutung |
|---|---|---|---|
| WwMin | 51 | 51 °C | WW-Mindesttemperatur (außerhalb Zeitfenster, T.ww.MIN) |
| Soll | 57 (Fix) | 57 °C | WW-Speichersoll (Zeitfenster-Soll; `Ext`=0) |
| DEin | 0 | 0,0 K | Einschaltdifferenz |
| DAus | 40 | **4,0 K ✅** | Abschaltdifferenz (0,1-K-Raster) |
| Br | 100 | **Brennerleistung, ungenutzt** ✅ (F3) | Ölbrenner = rein ON/OFF (0 oder 100 %), keine Lastmodulation (maintainer 2026-10-31) |
| Zeitprogramm | 5 ZP (wt=11/4/16/32/64; ZP0: 06:00–08:00 + 12:30–22:00; ZP1: 06:00–12:00 + 14:30–22:00; ZP2: 06:00–23:00; ZP3: 08:00–23:00; ZP4: 08:00–22:00; alle sw=50 °C) | **Minuten-Raster ✅ (F2)** | WW-Soll 50 °C je Zeitfenster (T.ww.EFF), Wochentags-Maske pro ZP |

### 4.4 UVR1611 — Ladepumpe O1 („Pumpe Holzo.")

| Parameter | Rohwert | Interpretiert | Bedeutung |
|---|---|---|---|
| T.Zub.Min | 65 | 65 °C | Holzkessel-VL-Min (a9 > 65 °C) |
| Diff.Ein (Zub) | 30 | **3,0 K ✅** | a9 > a3 + 3 K |
| Diff.Aus (Zub) | 0 | 0,0 K | – |
| T.Ref.Max | 90 | 90 °C | Speicher-Unten-Max (a3 < 90 °C) |
| Diff.Ein (Ref) | 10 | 1,0 K | Hysterese Ref-Max (a3 < 90 °C) |
| Diff.Aus (Ref) | 50 | 5,0 K | Ref-Max-Aus-Schwelle |
| Diff.Ein (Z-R) | 30 | 3,0 K❓ | a9 - a3 > 3 K |
| Diff.Aus (Z-R) | 0 | 0,0 K | – |

### 4.5 UVR1611 — Timer / Schaltuhr / Logik

| Modul | Parameter | Rohwert | Interpretiert |
|---|---|---|---|
| Timer#11217412 (Zirkulation-Nachlauf, Mod=0) | Ein | 97 | **97 min** (Nachlaufzeit, >90 = Minuten-Raster, Manual-Belegt) |
| Timer#11325631 (WW-Ladepumpe, Mod=1 Mindestlauf) | Ein | 95 | **95 min** (Mindestlaufzeit) — Signal „Anforderung Ladepumpe WW" → O2.Fg + Heizkreis.Fg (invers) |
| Schaltuhr#11321718 (Zirkulation-Zeit) | Soll | 5 | 5 °C❓ (Soll außerhalb Fenster) |
| Schaltuhr | BedS / BedT | 60 / 0 | 6,0 min / 0 (Min.Zeit-Blockadebed, Manual „5.0 Min"-Raster) |
| Vergleich#11246068 (O6, WW<52) | Wb | 52 | 52 °C (WW-Temp-Schwelle, direkt °C) |
| Vergleich#11223559 (O9, a2>a7) | Diff.Ein / Daus | 50 / 30 | **5,0 K / 3,0 K ✅** (Speicher-Mitte > Heizkreis-RL) |
| Vergleich#1635… (O2, a5>a1) | Diff.Ein / Daus | 246 / 226 | **24,6 K / 22,6 K** (Öl-Ladepumpe, FktGr 0,1-K) |
| Vergleich#11205958 (Abgas-AN) | Wb | 10 | 10 °C ⚠️ (Abgas-Schwelle, direkt-°C-Vermutung) |

### 4.6 UVR610 — Solarregelung

| Parameter | Rohwert | Interpretiert | Bedeutung |
|---|---|---|---|
| KMax (Kolloktor-Max) | 1300 | 130,0 °C | Dampf-Grenze (Pumpen-Blockade) |
| KMaxDE / KMaxDA | -100 / 0 | -10 K / 0 K❓ | Hysterese Kolloktor-Max |
| KMinDE / KMinDA | 50 / 0 | 5 K / 0 K❓ | Hysterese Kolloktor-Min |
| RMaxDE / RMaxDA | -30 / 0 | -3 K / 0 K❓ | Hysterese Referenz-Max |
| BMaxDE / BMaxDA | -30 / 0 | -3 K / 0 K❓ | Hysterese Begrenzung-Max |
| DKRE (Diff.Ein Koll-Ref) | 110 | 11 K | **Einschaltdifferenz Kolloktor-Speicher** |
| DKRA (Diff.Aus Koll-Ref) | 70 | 7 K | Abschaltdifferenz |
| StabZ (Stabilisierungszeit) | 0 | 0 | Keine Stabilisierungszeit |
| KollMin (Eingangsvariable) | 100 | 10,0 °C | **Kolloktor-Minimum (Einschaltschwelle)** |
| RefMax (Eingangsvariable) | 900 | 90,0 °C | **Referenz-Max (Speicher-Unten-Max)** |
| BegMax (Eingangsvariable) | 800 | 80,0 °C | **Begrenzung-Max (Speicher-Oben-Max)** |

### 4.7 UVR610 — Rücklaufminderer (3×)

| Parameter | Rohwert | Interpretiert | Bedeutung |
|---|---|---|---|
| Vergleich.WertB (Wb) | 200 | **20,0 °C ✅** (FktGr-Index 4 = °C mit 1 Nachkommastelle, Manual-Belegt) | **Rücklauf-Schwelle** (Raum-RL < 20,0 °C + 0,1 K → EIN) |
| Vergleich.DEin | 1 | 0,1 K❓ | Einschaltdifferenz |
| Mathematik.EVarA | 1000000 | (Skalierung, erzeugt linearen 0–10-V-Wert) | Zusammen mit FktGr/OpAB erzeugt den linearen Ventil-Öffnungsgrad (0 % zu → 100 % auf, ✅ F4) |
| Mathematik.FktGr | 24 | (Funktionsgröße) | Erzeugt den linearen 0–10-V-Wert aus der RL-Temp-Abweichung (0 % = zu / 100 % = auf, ✅ F4) |

---

## 5. Regelungs-Logik (zusammenhängend, System-Sicht)

```
 ┌──────────────────────────────────────────────────────────────────────────┐
 │                         A U S S E N  (a8)                               │
 └──────────────────────────────────┬───────────────────────────────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │   UVR1611 — Heizkreis-Regler  │  ← RAS-Min-Temp (kältester Raum)
                    │   (Grundregelung, Heizkurve)  │
                    └───────┬───────────────────────┘
                            │ VL-Soll (Vorlauf-Soll)
        ┌───────────────────┼───────────────────────────┐
        │                   │                           │
        ▼                   ▼                           ▼
 ┌──────────────┐  ┌──────────────────┐     ┌──────────────────────┐
 │ UVR1611      │  │ UVR1611          │     │ UVR1611              │
 │ Anforderung  │  │ Heizkreis-Pumpe  │     │ Mischkreis-Regelung  │
 │ Öl-Brenner   │  │ O8 + Mischer     │     │ O3/O4 (2/3-Wege)     │
 │ (O5, RISK)   │  │ O8, O3/O4        │     │                      │
 └──────┬───────┘  └──────────────────┘     └──────────────────────┘
        │
        ▼ (Öl-Kessel VL, a5)
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  Öl-Kessel H2 (zwischen Heizkreis & Puffer)                             │
 │  ← Lädt aus Puffer-Mitte (a2), VL direkt in Mischer                    │
 │  O9 = Rücklauf-Entscheidung (zurück zur Ölheizung | unten im Puffer)   │
 └──────────────────────────────────────────────────────────────────────────┘
        │
        ▼ (Puffer)
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  Pufferspeicher P (a1 Oben / a2 Mitte / a3 Unten)                      │
 │  ← Lädt von: Holzkessel (O1, a9), Solar (O1/UVR610, a16), Öl (O5)      │
 │  → Lädt: WW-Wärmetauscher (a4), Heizkreis (a6/a7), Rücklaufminderer     │
 └──────────────────────────────────────────────────────────────────────────┘
        │
        ▼ (Raum-Rücklauf pro Einrohr-Kreis)
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  UVR610 — 3× Rücklaufminderer (RM1/RM2/RM3, 0–10 V, DL-Ausgang)        │
 │  Keller/EG/OG: RL-Temp (von UVR1611 per CAN) → 0–10 V → Aktor         │
 └──────────────────────────────────────────────────────────────────────────┘
```

**Stufen der Regelung (hierarchie, vom „grobsten" zum „feinsten"):**
1. **Grundregelung (UVR1611, Heizkreis-Regler):** Außen-T → Vorlauf-Soll via Heizkurve
   (+ Raum-Temp-Einfluss 70 %, + Zeitprogramm Normal/Absenk, + Frostschutz, + Abschalt-
   bedingungen). Dies ist die **einzige echte „Regelung"** (kontinuierliche Vorlauf-Soll-
   temperatur). Alle anderen sind **Diskrete-Schalt-Logik**.
2. **Öl-Brenner (UVR1611):** Diskrete Anforderung, gleitender Kesselbetrieb (beide
   Schwellen am Ök-VL a5: `a5 < VL-Soll + 3 K` → an; `a5 > VL-Soll + 9 K` → aus).
   **Große Hysterese (3 K / 9 K)** = bewusste Entkoppelung (verhindert Takten).
3. **WW (UVR1611):** Zeitfenster + Temperatur-Schwellen (WwMin=51 °C, WW-Soll je Fenster).
4. **Solar (UVR610):** Differenzregelung (Kolloktor > 10 °C, Diff > 11 K, Speicher < 90 °C,
   Top < 80 °C) + Kolloktor-Max 130 °C (Dampf-Schutz).
5. **Rücklaufminderer (UVR610):** Raum-abhängige 0–10 V-Regelung (RL-Temp → Aktor).
6. **Zirkulation (UVR1611):** Stromungsschalter + Nachlauf-Timer + Zeitfenster.
7. **Rücklauf-Entscheidung O9 (UVR1611):** a2 (Puffer-Mitte) > a7 (Heizkreis-RL) + 5 K →
   Heizkreis-Rücklauf-Ventil öffnen (Puffer-Rücklauf mischen, Vorlauf erhöhen).

**Wichtige Wechselwirkungen / „Fallen" im bestehenden Design:**
- **Heizkreis-Freigabe hängt am WW-Ladepumpe-Timer** (Signal „Anforderung Ladepumpe
  Warmwasser", Heizkreis.Fg mit `inv=true`, netlist-bewiesen). → Während der WW-Ladepumpe-
  Mindestlaufzeit (95 min) läuft, ist die Heizkreis-Grundregelung **invers** (Fg aus).
  Netlist-Kette: Timer#11325631 (Mod=1, Trig=AnfWW.Anf) → Signal → {Vergleich#1635….Fg (O2),
  Heizkreis.Fg(invers)}. ✅ **F6 gelöst (HITL):** bewusste **WW-Priorität** (träge Heizung +
  zwei Pumpen am Ölanschluss) — **kein Bug, kein Optimierungs-Punkt**.
- **Raum-Temp-Eingang = MIN(Keller, EG, OG)** (kältester Raum). → Die Grundregelung regelt
  immer auf den **kältesten Raum**. Das ist konservativ (gut für Komfort), aber **energie-
  ineffizient** (die wärmeren Räume werden überheizt, besonders bei RAS-Offset).
- **Öko-Betrieb = 0** (kein Zeitflächen-Unterdeckungs-Verfahren). → Der Öl-Brenner startet
  **sofort** bei Unterschreiten der Schwellen (kein „Warten", um kleine Lasten zu
  konsolidieren). **Optimierungs-Punkt** (siehe §6).
- **Kein Vorlauf-Max-Limiter im Öl-Brenner** (nur VLMax im Heizkreis). → Bei sehr niedriger
  Außen-Temp kann der Vorlauf an VLMax (70 °C) anstoßen, aber der Öl-Brenner hat keinen
  separaten Limit. **Prüf-Punkt.**
- **RAS-Offset ist der einzige „Raum-Komfort"-Hebel** (dev1, per UVR1611/CAN). Die
  Rücklaufminderer (UVR610, 3× TA-Compact-P, stufenlos 0–10 V) sind der **Rücklauf-Optimierer**
  (energie), aber **nicht CMI-lesbar** (✅ F8: keine Positions-Sensoren, nur PT1000-RL-Temp
  als Feedback; UVR610 `a10–a15` = befohlene, keine gemessenen Ventilwerte).

---

## 6. Optimierungs-Potenziale (konkret, priorisiert)

> **Prämisse:** Die Steuerung ist ein **TA-Designer-Funktionsnetzwerk**, kein offener PID.
> Optimierung bedeutet also: (a) **Parameter-Tuning** (Schwellen, Hysteresen, Zeitfenster,
> Kennlinien), (b) **Logik-Änderungen** (neue/änderte Module, Verdrahtung), (c) **KI-Intervention**
> über die CMI-Whitelist-Schalter + RAS-Offsets (S6). **Nichts** davon erfordert einen
> kompletten Regler-Tausch (die Hardware ist leistungsfähig und erweiterbar).

### 6.1 Kurze (Parameter-Tuning, kein Programm-Upload, per CMI/TA-Designer)

| # | Maßnahme | Modul | Hebel | Erwarteter Effekt | Risiko |
|---|---|---|---|---|---|
| O1 | **Öko-Betrieb aktivieren** (Oeko 0→ z. B. 15 %) | u1611FunkAnfHeiz | Unterdeckungs-% | Weniger Öl-Brenner-Starts bei kleinen Lasten (Zeitflächen-Verfahren konsolidiert Lasten). **Energie-Einsparung.** | Gering (bewährtes TA-Verfahren) |
| O2 | **Raum-Temp-Eingang: MIN → MAX** (oder „kältester + Offset") | u1611FunkAnalog#1635754961948349 (F=1 MIN→2 MAX) | Min-Max-Umschaltung | Weniger Überheizung wärmerer Räume (derzeit regelt auf den kältesten Raum = wärmerere Räume werden überheizt). **Komfort + Energie.** | Mittel (Kann zu Unterheizung des kältesten Raums führen, wenn MAX) |
| O3 | **Heizkurven-Neigung (Steil) optimieren** | u1611FunkHeizkreis (Steil=60) | 0,05–2,50 | Passgenauere Vorlauf-Soll-Temperatur (weniger „über/unter" → weniger Öl-Verbrauch). **Energie.** | Gering (Standard-Tuning) |
| O4 | **Vorlauf-Max (VLMax) senken** (70 → z. B. 65 °C) | u1611FunkHeizkreis | VLMax | Weniger Überhitzung bei niedriger AT, weniger Öl-Verbrauch, **verbessert Solar-Nutzung** (Puffer bleibt „kühler" für Solar). | Gering |
| O5 | **WW-Zeitfenster / WwMin anpassen** | u1611FunkAnfWW | Zeitfenster, WwMin=51 | Genaue WW-Produktion (weniger „überladen" bei Solar-Vorrang). | Gering |
| O6 | **Solar-Diff.Ein (DKRE=11 K) senken** (→ z. B. 8 K) | u16x2FunkSolReg | DKRE | Mehr Solar-Nutzung (früherer Pumpen-Start bei kleinerer Diff). **Solar-Ausbeute.** | Gering |
| O7 | **Rücklaufminderer-Schwelle (Wb=20,0 °C) optimieren** | u16x2FunkVergleich ×3 | Wb, Diff | Bessere Raum-Rücklauf-Misch-Regelung (weniger Rücklauf-Verluste). **Energie + Komfort.** | Mittel (0–10-V-Mapping linear = 0 % zu → 100 % auf ✅ F4; **keine Positions-Sensoren** ✅ F8 → Effekt nur über PT1000-RL-Temp pro Strang validierbar) |

### 6.2 Mittelfristige (Logik-Änderungen, Programm-Upload per TA-Designer)

| # | Maßnahme | Aufwand | Effekt |
|---|---|---|---|
| O8 | **Öko-Betrieb + „Gleiten-der-Schwellen"**: Öl-Brenner-Schwellen (Diff.Ein/Aus) als **funktion der AT** (statt fix). | Mittel | Adaptives Öl-Brenner-Verhalten (bei warmer AT kleinere Hysterese, bei kalter AT größere). |
| O9 | **Vorlauf-Soll direkt setzbar** (CAN-Adresse, v2, Q-104): den VL-Soll-Wert des Heizkreis-Reglers per CMI/KI **überschreiben** (außerhalb des Heizkurven-Berechnungs). | Mittel | **Stärkster Hebel** für KI-L2 (direkte Vorlauf-Stellgröße). |
| O10 | **Solar-Vorrang-Logik**: Wenn Solar aktiv (O1 EIN), **Öl-Brenner blockieren** (Freigabe-Logik ergänzen). | Gering | Weniger „Öl-Solar-Konkurrenz" (Solar lädt den Puffer, Öl heizt nicht gleichzeitig). **Energie.** |
| O11 | **Rücklaufminderer-Logik: RAS-Ist-Temp statt RL-Temp** als Eingang (bzw. Kombination). | Mittel | Bessere Raum-Komfort-Optimierung (die Rücklaufminderer würden auf die **Raum-Temperatur** (Ist) reagieren, nicht nur auf den Rücklauf). |
| O12 | **Wärmemengenzähler-Modul** (TA-Funktion „Wärmemengenzähler") ergänzen: VL-RL-Differenz + Durchfluss → kWh pro Heizkreis/Solarkreis/WW. | Gering | **Energie-Monitoring** (für KI-L2-Kostenfunktion + für maintainer: „wie viel Solar/Öl/Holz?"). |
| O13 | **Regelung der Rücklaufminderer nachführen** (z. B. PID statt offener linearer RL-Temp→0–10-V-Kennlinie; die 3× TA-Compact-P sind **stufenlos 0–10 V** ✅ F5, **ohne Positions-Sensor** ✅ F8 → Feedback nur über PT1000-RL-Temp). | Mittel | **Stufenlos, reglerische** Raum-Optimierung (weniger Takten, bessere Komfort-Nähe); die vorhandene offene lineare Kennlinie (Vergleich+Mathematik) ist bereits stufenlos, aber **offen** (kein geschlossener Regelkreis). |

### 6.3 KI-Intervention (S6, über CMI-Whitelist + RAS-Offsets)

> Dies ist **kein „Redesign der UVR"**, sondern die **Nutzung der bestehenden Freiheitsgrade**
> per CMI (S6-L2-Optimizer, 12-h-Horizont):
- **9 Whitelist-Schalter** (Betriebsmodus, 7 Pumpen/Ventile, Solarpumpe) = **diskrete
  Stellgrößen** (Betriebsart, Handbetrieb).
- **RAS-Offsets** (Offset-Kanäle ch18/21/24 bzw. dev1 a1/a4/a7 „Offset", an der
  **RAS-Quelle** setzbar, **v2 = CMI-writbar**; die UVR1611 rechnet selbst keine Offsets,
  §2.3.6): **kontinuierliche Stellgröße** (±0,5 K-Schritte, max. ±2 K/h, Guardrails).
  Der Offset verschiebt die „Perzipierte Raum-Temperatur", auf die die Grundregelung
  regelt (RT = MIN der 3 Raum-Quellen ch17/20/23) → **direkter Hebel für Raum-Komfort**
  (Q-105, Q-107; Kanal-Zuordnung ❓ F7).
- **Heizkurven-Parameter** (Steil, VLMax, +10/-20, v2 nach Programm-Upload): **kontinuierliche
  Stellgröße** für die Vorlauf-Grundregelung (Q-108).
- **Vorlauf-Soll** (v2, Q-104): **stärkster Hebel** (direkte VL-Stellgröße).

> **Fazit S6:** Die bestehenden Freiheitsgrade (Schalter + Offsets + Kurven + VL-Soll) sind
> **ausreichend** für eine KI-L2-Optimierung (12-h-Horizont, Kosten = Komfort + Energie +
> Änderung-Strafe). **Kein kompletter Regler-Tausch nötig.** Die UVR1611/UVR610 bleiben die
> „Reflex-Schicht" (schnelle Diskrete-Logik + Grundregelung), die KI ist die „Planungs-Schicht"
> (stündliche Empfehlung).

---

## 7. Redesign-Empfehlung (macht ein komplettes Redesign Sinn?)

**Kurze Antwort: NEIN (kein kompletter Regler-Tausch), JA (gezielte Nachbesserung + KI-Schicht).**

**Begründung:**
1. **Hardware ist leistungsfähig:** Die UVR1611 (CAN-Touch, 1611) + UVR610 (32) bilden ein
   **ausgewogenes, erweiterbares System** (CAN-Bus, 128 Funktions-Module, 9 Schalter + 2
   Analog-Ausgänge pro Gerät, M-Bus, CAN-Touch). Ein kompletter Tausch (z. B. auf ein
   „offenes" System) wäre **teuer, riskant** und würde die **erprobte TA-Logik** (die über
   Jahre abgestimmt wurde) ersetzen.
2. **Die vorhandenen Freiheitsgrade reichen** (siehe §6.3): Schalter + RAS-Offsets +
   Heizkurven + Vorlauf-Soll = **ausreichend** für eine KI-L2-Optimierung. Das S6-Design
   (12-h-Horizont, L1-Keras + L2-Optimizer) nutzt **genau diese** Freiheitsgrade.
3. **Die bestehenden „Fallen" (Wechselwirkungen, MIN-Raum-Temp, Öko=0)** sind **gezielt
   nachbesserbar** (O1–O13), ohne das System zu ersetzen.
4. **Die Datenbasis (WinSol LogX, dev0/1/2, 2020–2026)** ist **exzellent** und **direkt
   nutzbar** für die KI-L1/L2-Modelle (S6-M1–M7). Ein Redesign würde diese Daten-Kontinuität
   brechen.

**Was ein „Redesign" tatsächlich sein sollte (empfohlen):**
- **Phase 1 (S6, jetzt):** KI-Schicht (L1 + L2) über die **bestehenden** Freiheitsgrade
  (Schalter + RAS-Offsets + Kurven + VL-Soll). **Kein UVR-Programm-Change.**
- **Phase 2 (v2, nach Programm-Upload):** **Vorlauf-Soll + RAS-Offset-Write + Heizkurven-
  Parameter** per CMI (Q-104, Q-107, Q-108). **Gezielte UVR1611-Programm-Änderung**
  (TA-Designer), nicht „Redesign".
- **Phase 3 (optional):** **Öko-Betrieb + Solar-Vorrang + Wärmemengenzähler** (O1, O10, O12)
  per TA-Designer (Parameter-/Logik-Änderung, kein Hardware-Tausch).

**Empfohlene „Redesign"-Priorität (falls maintainer es will):**
1. **O1 (Öko-Betrieb)** + **O10 (Solar-Vorrang-Logik)** — **schnell, sicher, hoher Energie-
   -Effekt**.
2. **O12 (Wärmemengenzähler)** — **Energie-Monitoring** (für S6-Kostenfunktion + maintainer-
   Transparenz).
3. **O9 (Vorlauf-Soll setzbar)** — **stärkster KI-Hebel** (v2).
4. **O13 (PID für Rücklaufminderer)** — **Qualität** (nur wenn die 0–10 V-Aktoren
   stufenlos sind und die Logik gemessen ist).

---

## 8. Offene Fragen / ❓-Liste (für HITL / nächsten Lauf)

**Geschlossen in Revision 3 (2026-10-31):**
- ~~**F1** (Diff-Skalierung)~~ → **gelöst**: Diff-Parameter = 0,1-K-Raster (Manual-Belegt);
  Temperatur-Schwellen direkt °C (Header-Note + §4).
- ~~**F2** (Timer-/Zeitfenster-Raster)~~ → **gelöst**: Timer >90 = Minuten (97 = 97 min);
  Zeitfenster = Minuten (0–1439); `wt` = Wochentags-Bitmaske (Header-Note + §4.1/4.3/4.5).

**Geschlossen durch HITL-Antworten (maintainer, 2026-10-31, verbatim in §8.1):**
- ~~**F3** (Br-Parameter)~~ → **gelöst**: `Br` = Brennerleistung-Parameter; **in dieser Anlage
  ungenutzt/irrelevant**, weil der Ölbrenner **rein ON/OFF** ist (0 oder 100 %, keine
  Lastmodulation, keine Abstufung der Brennerleistung). `Br=97` (AnfHeiz) / `Br=100` (AnfWW)
  sind daher **No-Ops**. (§2.2, §2.3.1, §4.2/4.3)
- ~~**F4** (0–10-V-Mapping Rücklaufminderer)~~ → **gelöst**: 0–10 V = **0 % (komplett zu) →
  100 % (komplett auf)**, **linear** (TA-Compact-P, lineare Regelcharakteristik). Die 3
  Ventile sind auf **100 % voreingestellt** (korrekter Wärmebedarf pro Strang); die UVR610
  moduliert in kleinen Schritten dazwischen. Die `Mathematik`-Parameter (`FktGr=24`,
  `EVarA=1000000`) erzeugen diesen linearen 0–10-V-Wert aus der RL-Temp-Abweichung.
  (§3.2, §4.7)
- ~~**F5** (Rücklaufminderer-Aktor-Typ)~~ → **gelöst**: **TA-Compact-P (IMI Hydronic
  Engineering)**, druckunabhängiges Regel- und Regulierventil, **DN 25**, mit **elektrischem
  Stellantrieb** (stufenlos, 0–10 V; Messnippel für Durchfluss/Temp/Δp). Technische Daten
  (DN 25): 370–2150 l/h, PN 16, 10–90 °C, Δp 25–400 kPa, Stellantrieb M30×1,5, lineare
  Charakteristik, Ventilhub 4 mm. (§3.2, §4.7)
- ~~**F6** (Heizkreis-Freigabe invers)~~ → **gelöst (bewusste Design-Entscheidung, kein Bug):**
  **WW-Priorität** — der Heizungsteil reagiert **sehr träge**; Warmwasser wird **schnell
  benötigt** und daher **priorisiert**. Während der WW-Ladephase (O2 Öl-Ladepumpe aktiv,
  95-min-Timer) ziehen **zwei Pumpen** an dem Ölanschluss (O2 Öl-Ladepumpe + O8
  Heizkreis-Pumpe); die Heizkreis-Grundregelung wird daher **invers blockiert**, um den
  Pumpen-Konflikt am Ölanschluss zu vermeiden. Netlist-Kette (Timer#11325631 → `Heizkreis.Fg`
  `inv=true`) bleibt gültig; die **physikalische Absicht ist jetzt geklärt**. (§2.3.5, §5)
- ~~**F8** (Rücklaufminderer CMI-lesbar?)~~ → **gelöst**: **Nein — kein physikalischer
  Positions-Sensor.** Die 3 Ventile haben **keine Sensoren** (kein Positions-Feedback); sie
  können **nur als Aktor** (0–10 V) bedient werden. Das **einzige Feedback** ist der
  **PT1000 pro Strang** (Raum-Rücklauf-Temp). Die UVR610-Kanäle `a10–a15` („HzRkl Ventil
  1/2 Ergebnis") sind die **befohlenen** (logischen) Ausgangswerte der Vergleich+Mathematik-
  Module, **keine gemessenen Ventilpositionen**. (§3.1/3.2, §5, §6.2)

**Offen:**

| # | Frage | Relevanz |
|---|---|---|
| F7 | **RAS-Kanal-Zuordnung**: (a) die 6 UVR610-RM-WertA-Quellen (CAN ch10/12/14) = welche Kreis-RL-T?; (b) RAS-6er (ch17/18/20/21/23/24) = Keller/EG/OG × Ist/Offset? (MIN-Quellen = ch17/20/23). → **WinSol-Setup-Check (dev1/dev2-Labels) + Messung**. | K1, §2.3.6, §3 |
| F9 | **Öl-Brenner-Stand `10520A10180`**: live `AUTO/AUS` (2026-10-26) vs. `memory.md` „HAND/EIN" — **Live-Read-back** (plant-model §4 DISCREPANCY). → **CMI-Read** (nächster Lauf). | plant-model §4 |
| F10 | **UVR610-FW T1.18 → T1.19** (2022-09-18): welche Änderungen? (3 neue BDB-Labels „Keller/EG/OG HzRkl Ventil"). → **TA-Release-Notes-Check**. | Staleness |

### 8.1 HITL-Antworten (maintainer, 2026-10-31, verbatim im Doku)

> **Antwort 1 (→ F3 + F6):** „Der Heizungsteil reagiert sehr traege. Warmwasser wird sehr
> schnell benoetigt, daher wird Warmwasser priorisiert, falls es genutzt werden soll.
> Ausserdem ziehen dann zwei Pumpen an dem Anschluss der Oelheizung. Die Oelheizung selber
> kann nur ein ode ausgeschaltet werden, es gibt hier keine Abstufung der Brenner Leistung.
> Also nur 0 oder 100%."
>
> → **F3**: Ölbrenner = rein ON/OFF (0 oder 100 %), keine Lastmodulation → `Br` ungenutzt.
> → **F6**: WW-Priorität (träge Heizung + zwei Pumpen am Ölanschluss) → inverses
> `Heizkreis.Fg` = **bewusste Design-Entscheidung** (kein Optimierungs-Punkt).
>
> **Antwort 2 (→ F4 + F5 + F8):** „Zum Thema Ruecklaufminderer:
> https://www.baulinks.de/webplugin/2018/1877.php4 Bzw. es ist sogar ein TA-Compact-P eben nur
> mit Stellantrieb: https://www.meinhausshop.de/TA-Compact-P-DN25-druckunabhaengiges-Regel-und-
> Regulierventil-7738322550 --- d.h. die drei Ventile sind bei 100% so eingestellt, dass der
> entsprechende Waermebedarf pro Strang korrekt ist. Kann aber dann [mit] 0% = komplett zu und
> 100% komplett auf in kleinen Schritten feinjustiert werden. Ich habe aber mangels Sensoren
> dies nie machen koennen. D.h. die drei Ventile koennen nur als [A]ktoren bedient werden,
> haben aber keine Sensoren. Ich habe pro Strang nur einen Extra Fuehler PT1000 angebracht."
>
> → **F5**: Aktor = **TA-Compact-P (IMI Hydronic)**, DN 25, elektrischer Stellantrieb, stufenlos
> 0–10 V.
> → **F4**: 0–10 V = 0 % (zu) → 100 % (auf), **linear**; Ventile auf 100 % voreingestellt
> (korrekter Wärmebedarf pro Strang).
> → **F8**: Ventile **ohne Positions-Sensor** (nur Aktor); **einziges Feedback = PT1000 pro
> Strang** (Raum-Rücklauf-Temp).
>
> **Quellen (maintainer):** Baulinks-News (3.12.2018) „Automatischer hydraulischer Abgleich in
> Einrohr-Heizungsanlagen" (TA-Compact-P, IMI Hydronic Engineering); meinhausshop.de
> TA-Compact-P DN 25 (IMI Hydronic, druckunabhängiges Regel- und Regulierventil; DN 25,
> 370–2150 l/h, PN 16, 10–90 °C, Δp 25–400 kPa, Stellantrieb M30×1,5, lineare
> Charakteristik, Ventilhub 4 mm, Messnippel).

---

## 9. Dokumenten-Strategie (wo ablegen?)

**Empfehlung: Beides (komplett im Projekt, kuratiert im Wiki).**

### 9.1 Im Projekt (`<repo-root>/`)
- **Dieses Dokument:** `docs/uvr-programm-analyse.md` (komplett, mit allen Parametern,
  Verdrahtung, ❓-Liste). **Kanon** für die S6-AI-Session (L1/L2-Modelle brauchen die
  Schwellen + Freiheitsgrade).
- **Verwandt (bestehend):**
  - `docs/plant-model.md` (Kanal-Belegung, 9 Whitelist-Adressen) — **komplementär** (dieses
    Dokument: „Wie arbeitet das Programm?" · plant-model: „Was ist am Kabel?" ).
  - `docs/hydraulic-topology.md` (5 Loops, Komponenten) — **komplementär** (dieses Dokument:
    „Steuerlogik" · hydraulik: „Physisch-Topologie").
  - `docs/ai-model-design.md` (S6-Design) — **Verbraucher** (dieses Dokument liefert die
    Schwellen + Freiheitsgrade für L2).
- **`.tdw`-Dateien:** **bleiben in Nextcloud** (`house-heating/`), **nicht** im Repo (sie sind
  „Betriebs-Daten", nicht „Code"). Aber: **ein Symbolic-Link oder Pfad-Referenz** im
  `docs/`-Ordner wäre sinnvoll (damit die S6-Session sie findet).

### 9.2 Im Wiki (`/home/user/llm-wiki/`)
- **Kuratierte Zusammenfassung:** `wiki/uvr-programm-analyse.md` (oder als **Erweiterung**
  von `wiki/cmi-anzlage.md`): das **Design-Prinzip** (TA-Designer), die **Kern-Regelungs-
  Logik** (Grundregelung, Öl-Brenner, Solar, Rücklaufminderer), die **Freiheitsgrade**
  (Schalter + Offsets + Kurven + VL-Soll) und die **Redesign-Empfehlung**. **Ohne** die
  kompletten Parameter-Tabellen (die bleiben im Projekt).
- **`index.md`:** neue Zeile in „T&A-Systeme / CMI" (Verweis auf das Projekt-Dokument +
  Kurzfassung).
- **`log.md`:** Eintrag (Pflege, 2026-10-31).

### 9.3 agentmemory (Governance)
- **Kern-Decision:** „UVR1611/UVR610-Programm-Analyse abgeschlossen: kein komplettes Redesign,
  gezielte Nachbesserung + KI-Schicht (S6) über bestehende Freiheitsgrade. Freiheitsgrade =
  9 Schalter + RAS-Offsets + Heizkurven + VL-Soll (v2). Schwellen in `cmi/docs/uvr-programm-
  analyse.md`."
- **Concept-Tags:** `cmi-uvr`, `uvr1611`, `uvr610`, `ta-designer`, `heizungsregelung`,
  `freiheitsgrade`, `redesign`, `s6-l2`.

---

## 10. Zusammenfassung (TL;DR)

- **Design:** Die Heizung wird über **zwei TA-Designer-Geräte** geregelt: **UVR1611** (CAN-Node
  1, Hauptsteuerung: Grundregelung + Öl-Brenner + WW + Zirkulation + Mischkreis + RAS-
  Offset-Logik) und **UVR610** (CAN-Node 32, Solar + RAS-Logger + 3× Rücklaufminderer). Beide
  sind **graphische Funktions-Netzwerke** (kein Code), per **CAN-Bus** verdrahtet, mit
  **Logging** (dev0/1/2 → WinSol) und **CMI-Ansteuerung** (9 Whitelist-Schalter).
- **Regelung:** **Stufen-Logik** (1: Grundregelung/Außen→VL-Soll via Heizkurve,
  RT = MIN(ch17,ch20,ch23) = kältester Raum, 2: Öl-Brenner diskret, gleitender
  Kesselbetrieb (a5 < VL-Soll+3 K an / a5 > VL-Soll+9 K aus; **Ölbrenner rein ON/OFF, keine Lastmodulation** ✅ F3), 3: WW (Zeitfenster,
  Soll 57 °C, WwMin 51 °C; **WW-Priorität: Grundregelung wird während der 95-min-WW-Ladephase blockiert** ✅ F6 = bewusste Design-Entscheidung), 4: Solar (Kollektor>10 °C, Diff>11 K, Speicher<90 °C,
  Top<80 °C), 5: Rücklaufminderer (**3× TA-Compact-P DN 25 (IMI), stufenlos 0–10 V linear = 0 % zu → 100 % auf** ✅ F4/F5, Schwellwert 20,0 °C, **keine Positions-Sensoren, Feedback nur PT1000/Strang** ✅ F8), 6:
  Zirkulation (Stromungsschalter+97-min-Nachlauf+Zeitfenster), 7: O9 (Puffer-Mitte
  > Heizkreis-RL + 5 K)).
- **Schwellen:** Vollständig in §4 (Heizkreis: Norm=20/Absenk=16, +10°C→VL45,
  -20°C→VL65, VLMax=70, VLMin=25, Raumeinfluss=70 %, Vorh=30 min, Zeitfenster
  06:00–22:30; Öl-Brenner: Diff.Ein=3,0 K ✅/Diff.Aus=9,0 K ✅, Öko=0, Br=97
  ungenutzt (ON/OFF, ✅ F3);
  Solar: Kolloktor>10 °C, Diff>11 K, Speicher<90 °C, Top<80 °C, Kolloktor-Max=130 °C;
  Rücklaufminderer: Wb=**20,0 °C** ✅, Diff.Ein=0,1 K ⚠️, 0–10 V linear ✅ F4; Timer:
  97/95 min ✅(F2)).
- **Optimierung:** 7 kurze (Parameter-Tuning: Öko=15%, MIN→MAX, Steil, VLMax=65, WW-Fenster,
  Solar-Diff=8 K, Rücklaufminderer-Schwelle), 6 mittelfristige (Öko+Gleiten, Vorlauf-Soll
  setzbar, Solar-Vorrang, Rücklaufminderer auf RAS-Ist, Wärmemengenzähler, PID-Rücklauf-
  minderer), KI-L2 (S6) über Schalter+Offsets+Kurven+VL-Soll.
- **Redesign:** **Nein** (kein kompletter Tausch), **Ja** (gezielte Nachbesserung O1/O10/O12 +
  KI-Schicht S6). Die bestehenden Freiheitsgrade reichen; die Datenbasis (2020–2026) ist
  exzellent.
- **Dokumenten-Strategie:** Komplett im Projekt (`docs/uvr-programm-analyse.md`), kuratiert
  im Wiki (`wiki/uvr-programm-analyse.md` + `index.md` + `log.md`), Kern-Decision in
  agentmemory.

*(Ende UVR-Programm-Analyse.)*
