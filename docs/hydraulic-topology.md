# Hydraulik-Topologie — TA Example-Street 1 (FINAL nach maintainer, 2026-10-28)

> **Status: Final (nach Anlage-Betreiber-Korrektur, 2026-10-28).** Basis:
> `docs/plant-model.json` (Confidence „hoch") + TA-Designer-Programme +
> **Korrektur des Anlage-Betreibers (maintainer)**. Die `.tdw` enthalten Steuerlogik,
> kein sauberes P&ID → die physische Hydraulik wurde vom Betreiber beigesteuert.
> Verbleibende Klein-Fragen (Kanalmapping) mit ❓.

**Legende:** ✅ belegt / HITL bestätigt · ⚠️ Ableitung · ❓ klein/offen.

---

## 1. Bausteine (Komponenten)

| ID | Baustein | Rolle | Konfidenz |
|---|---|---|---|
| H1 | **Holzofen** (Festbrennstoff) | Wärmeerzeuger, **eigener Kreis**, direkt am Puffer (ganz oben + ganz unten), Pumpe O1, Abgas | ✅ |
| H2 | **Ölheizung / Ölbrenner (Oelbrenner R1)** | Wärmeerzeuger, **zwischen Heizkreisen und Puffer**, umschaltbar (Heiz-/WW-Modus) | ✅ |
| H3 | **Solar-Kollektor** (Dach) | Wärmeerzeuger, **Spirale (Waermetauscher) in der unteren Puffer-Haelfte**, seit 2021-10 | ✅ |
| P | **Pufferspeicher** | zentrale Wärmesammlung, 3 PT1000 (Oben a1 / Mitte a2 / Unten a3); mehrere eigene Anschlüsse | ✅ |
| W | **Warmwasser** (Waermetauscher aus **oberer Puffer-Haelfte** auf Kaltwasser) | WW, dann **Zirkulation, fest 50 °C** | ✅ |
| HC | **3 Einrohr-Heizkreise** (Keller/EG/DG) + **1 Mischer + 1 Pumpe (O8)** | Heizung, **nur Heizkörper** | ✅ |
| R1 | **Raum Keller** (RAS + Raum-Temp + HzRk-Ventile) | Einrohr-Kreis 1 | ✅ |
| R2 | **Raum EG** (RAS + Raum-Temp + HzRk-Ventile) | Einrohr-Kreis 2 | ✅ |
| R3 | **Raum DG** (RAS + Raum-Temp + HzRk-Ventile) | Einrohr-Kreis 3 | ✅ |
| E1 | **Abgas / Schornstein** (Holzofen) | Abgastemp a10 + digital „Abgas AN/AUS" (Seite 5) | ✅ |
| E2 | **Aussensensor** | Außen-Temp a8, Regelung (Heizkurve) | ✅ |
| C1 | **UVR1611** (Node 1) | Hauptsteuerung (Grundregelung, O1–O9, Betriebsmodus, **Mischkreis-Regler**) | ✅ |
| C2 | **UVR610** (Node 32) | Solar + Solarpumpe + RAS/HzRk-Ventil-Logik + **3× Rücklaufminderer** | ✅ |

> **Puffer-Zonen (HITL):** „ganz oben" und „oben" liegen nur **wenige cm auseinander**;
> analog „unten" und „ganz unten" (2 Paare, je eng beieinander).

---

## 2. Messpunkte (PT-Fühler)

| Kanal | Messpunkt | Baustein | Einheit | Konfidenz |
|---|---|---|---|---|
| dev0 a1 | Speicher Oben | P | °C | ✅ |
| dev0 a2 | Speicher Mitte | P | °C | ✅ |
| dev0 a3 | Speicher Unten | P | °C | ✅ |
| dev0 a4 | Warmwasser | W | °C | ✅ |
| dev0 a5 | Ölkessel Vorlauf | H2 | °C | ✅ |
| dev0 a6 | Heizkreis **Vorlauf** (nach Mischer) | HC | °C | ✅ |
| dev0 a7 | Heizkreis **Rücklauf — ZENTRAL** | HC | °C | ✅ |
| (je Kreis) | **Rücklauf-Temp pro Einrohr-Kreis** (3×) | R1/R2/R3 | °C | ⚠️ 4 RL-Werte: 1 zentral (a7) + 3 pro Kreis (Kanalmapping ❓) |
| dev0 a8 | Temperatur Aussen | E2 | °C | ✅ |
| dev0 a9 | Holzkessel **Wassertank** (Fühler im Wasserjacket des H1, „Holzkessel Vorlauf") | H1 | °C | ✅ |
| dev0 a10 | Abgastemperatur Holz | E1 | °C | ✅ |
| dev0 a16 | **Stromungsschalter WW-Zirkulation** (Ziel 50 °C) | W | binär | ✅ (HITL) |
| dev2 a16 | T. Kollektor | H3 | °C | ✅ |
| dev2 a4–a9 | RAS Keller/EG/OG (je RAS + Temp) | R1/R2/R3 | °C | ✅ |
| dev2 a10–a12 | HzRk-Ventil 2 Ergebnis (Keller/EG/OG) | R1/R2/R3 | % | ⚠️ |
| dev2 a13–a15 | HzRk-Ventil 1 (Keller/OG/EG) | R1/R2/R3 | % | ⚠️ |
| dev2 d1 | Solarpumpe (An/Aus) | H3 | binär | ✅ |

> **Rücklauf-Temperatur: 4 Werte** — 1 zentral (dev0 a7) + 3 je Einrohr-Kreis. ✅ (HITL)
> Kanalmapping der 3 Kreis-Rücklauf-T ❓ (vermutlich UVR610).
> **Doppelte RAS-Belegung:** dev1 (UVR1611 DS2) loggt Keller/EG/DG-RAS seit 2020-08, dev2 (UVR610)
> seit 2021-10 über CAN → im Schema = ein Messpunkt pro Raum. ✅

---

## 3. Antriebe (Pumpen / Ventile / Brenner)

| ID | Antrieb | Baustein | Options | Setz-Adresse (CAN) | Konfidenz |
|---|---|---|---|---|---|
| O1 | **Pumpe Holzofen** (eigener Kreis) | H1 | AUS/EIN/AUTO | 10120A10180 | ✅ |
| O2 | **Ladepumpe WW** | W | AUS/EIN/AUTO | 10620A10180 | ✅ (Zuordnung ⚠️) |
| O3/O4 | **Mischkreis-2/3-Wege-Ventil** (Auf/Zu) — **Mischkreisregler auf UVR1611** | HC | 2/3-Wege | ❓ nicht in den 9 setzbaren (nur lesbar dev0 d3/d4) | ✅ |
| O5 | **Ölbrenner-Anforderung** | H2 | AUS/EIN/AUTO | 10520A10180 | ✅ **RISK** |
| O6 | **Ladepumpe „WW vom Öl"** | W/H2 | AUS/EIN/AUTO | 10220A10180 | ✅ (Zuordnung ⚠️) |
| O7 | **Zirkulationspumpe WW** (50 °C) | W | AUS/EIN/AUTO | 10720A10180 | ✅ (Zuordnung ⚠️) |
| O8 | **Pumpe Heizkreis** (die eine, alle 3 Einrohr-Kreise) — **im Vorlauf nach dem Mischer** | HC | AUS/EIN/AUTO | 10820A10180 | ✅ (HITL) |
| O9 | **Ventil Heizungsrücklauf** = **Rücklauf-Entscheidung** (zurück zur Ölheizung \| **unten** im Puffer, 10 cm über „ganz unten") | HC/H2 | AUS/EIN/AUTO | 10920A10180 | ✅ (HITL = Ja) |
| SV | **Solarpumpe** | H3 | Hand/AUS, Hand/EIN, Auto | 10040912091 (x2, Node 32) | ✅ |
| M | **Betriebsmodus** | C1 | 7 Optionen | 101301D0180 | ✅ |
| V1 | **HzRk-Ventile Keller/EG/OG** (je 2: Ventil 1 + 2) | R1/R2/R3 | — | ❓ nicht in den 9 Adressen; UVR610 | ⚠️ |
| RM1/2/3 | **3× Rücklaufminderer 0–10 V** (je Einrohr-Kreis) — **geregelt auf der UVR610, NICHT der Mischkreisregler (UVR1611)** | R1/R2/R3 | 0–10 V | ❓ (UVR610, nicht CMI-L1-Whitelist) | ✅ (HITL) |

> **Fazit:** 9 Antriebe CMI-L1-setzbar (Whitelist). Mischkreis (O3/O4) + 3× Rücklaufminderer
> (UVR610) + 3× Raum-Ventile = intern geregelt → im Schema als „C-intern" markieren.
> **Wichtig (HITL):** Die 3 Rücklaufminderer (UVR610) sind **separat** vom Mischkreisregler (UVR1611).

---

## 4. Die hydraulischen Loops (FINAL)

### Loop A — Holzofen (eigener Kreis, gemeinsam nur der Puffer)
```
H1 Holzofen
  VL (Hochtemperatur) ──► P **ganz oben** (eigener Anschluss)
  ◄── P **ganz unten** (eigener Anschluss; O9-(b) mündet 10 cm darüber in „unten") ── RL
  [Pumpe O1 im Kreis]
  Abgas ──► E1 Schornstein (a10 + digital „Abgas AN/AUS")
```
- Holzofen **eigener Kreis**, gemeinsame Schnittmenge mit dem Rest = **nur der Puffer**. ✅

### Loop B — Ölheizung (zwischen Heizkreisen und Puffer, umschaltbar)
```
HEIZ-MODUS:
  P **Mitte** (eigener Anschluss) ──► H2 Ölheizung ──► VL **direkt in den Mischer (HC)**
  (Rücklauf der 3 Einrohr-Kreise, s. Loop E)
     └─[O9 Rücklauf-Entscheidung]─► (a) **zurück zur Ölheizung**  |  (b) **unten im Puffer** (10 cm über „ganz unten")
WW-MODUS:
  H2 Ölheizung ──► P **ganz oben** über die **Warmwasser-Rücklauf**-Leitung   [Pumpe O6? ⚠️]
Brenner: O5 Anforderung (Öl-Verbrennung, RISK)
Messpunkt: Ölkessel-Vorlauf a5
```
- Ölheizung **zwischen** Heizkreisen und Puffer, umschaltbar. ✅
- Heiz-Modus: Vorlauf aus Puffer-**Mitte**; VL direkt in den **Mischer**. ✅
- Rücklauf-Entscheidung **O9**: zurück zur Ölheizung **oder unten** im Puffer (10 cm über „ganz unten"). ✅

### Loop C — Solar (Spirale im unteren Puffer)
```
H3 Kollektor ──► [SV Solarpumpe] ──► P **untere Haelfte** (Spirale / Waermetauscher IM Puffer)
                ◄───────────────────────────────────────────────────┘
Messpunkte: Kollektor a16 (dev2), Solarpumpe d1 (dev2)
```
- Solar **über eine Spirale in der unteren Haelfte**; Wärme direkt an die untere Puffer-Haelfte. ✅

### Loop D — Warmwasser (aus oberer Puffer-Haelfte)
```
P **obere Haelfte** ──► [Waermetauscher] ──► auf **Kaltwasser** ──► Warmwasser
Zirkulation: WW-RL ──► [O7 Zirkulationspumpe] ──► Zapfstelle(n)
   Ziel: **FEST 50 °C**  ·  **Stromungsschalter dev0 a16** (überwacht die Zirkulation)
Messpunkt: WW a4
```
- WW aus **oberer Haelfte** über **Waermetauscher** auf **Kaltwasser**; danach Zirkulation,
  **fest 50 °C** (nicht setzbar). ✅ Stromungsschalter a16 sitzt **in der WW-Zirkulation**. ✅

### Loop E — Heizung: 3 Einrohr-Kreise + 1 Mischer + 1 Pumpe
```
P **Mitte** ─► H2 Ölheizung (Heiz-Modus) ─► HC **Mischer** (2/3-Wege, O3/O4 — UVR1611)
HC Mischer ─► [O8 Pumpe Heizkreis — VORLAUF, nach Mischer] ─► **VL a6**
   ├► Einrohr-Kreis R1 Keller   (nur Heizkoerper, HzRk-Ventile V1)
   ├► Einrohr-Kreis R2 EG       (nur Heizkoerper, HzRk-Ventile V1)
   └► Einrohr-Kreis R3 DG       (nur Heizkoerper, HzRk-Ventile V1)
Jeder Kreis: RL ─► **Rücklaufminderer 0–10 V (RM1/2/3, UVR610)** ─► zentrale RL
Zentrale RL **a7** (1 zentraler + 3 Kreis-Rücklauf-T) ─► [O9 Rücklauf-Entscheidung]
   └─► (a) zurück zur Ölheizung  |  (b) P **unten** (getrennt; 10 cm über H1-RL „ganz unten")
```
- **Drei Einrohr-Heizkreise**, **1 Mischer + 1 Pumpe (O8)**, Pumpe im **Vorlauf nach dem Mischer**. ✅
- **3× Rücklaufminderer 0–10 V** (je Kreis, **UVR610-geregelt**, ≠ Mischkreisregler/UVR1611). ✅
- Rücklauf-Temperatur: **4 Werte** (1 zentral + 3 je Kreis). ✅
- Rücklauf-Entscheidung **O9**: zur Ölheizung oder unten im Puffer. ✅

### Sonderpunkte
- **P-Paare oben & unten je getrennt (HITL 2026-10-29, R2):** Oben „ganz oben"/„oben" = 2 getrennte VL-Anschlüsse (H1-VL + H2-VL/WW-Modus); unten „unten"/„ganz unten" = **2 getrennte RL-Anschlüsse** — O9-(b) in **„unten"**, H1-RL in **„ganz unten"** (10 cm Abstand, symmetrisch).
- **Rücklaufminderer 0–10 V:** 3 Stück, je Einrohr-Kreis, **auf der UVR610 geregelt**, **separat**
  vom Mischkreisregler (UVR1611). ✅
- **Stromungsschalter dev0 a16:** in der **WW-Zirkulation** (Ziel 50 °C). ✅
- **„Abgas AN/AUS"** (Seite 5 pos15, digital): **Schornstein/Abgas vom Holzofen**. ✅

---

## 5. Steuer-Abhängigkeiten (Regelung)

```
E2 (Aussen) ──► C1 (VL-Soll aus Aussen-Temp, Heizkurve)
C1 (UVR1611, Node 1):
   ├── M    Betriebsmodus (7 Modi)
   ├── O1   Holzofen-Pumpe (eigener Kreis)
   ├── O5   Ölbrenner-Anforderung
   ├── O8   Heizkreis-Pumpe (alle 3 Einrohr-Kreise)
   ├── O9   Ventil Heizungsrücklauf (Rücklauf-Entscheidung)
   ├── O3/O4 Mischkreis-Ventil (Mischkreisregler)
   └── O2/O6/O7 WW-Lade / WW-vom-Öl / Zirkulation
C2 (UVR610, Node 32):
   ├── SV    Solarpumpe (Kollektor a16 vs. Speicher)
   ├── V1    HzRk-Ventile Keller/EG/OG (RAS-Offset vs. Raum-Temp)
   └── RM1/2/3 Rücklaufminderer 0–10 V (je Einrohr-Kreis)
```
- ⚠️ Regelungs-Logik im Groben aus BDB-Labels + Loggen-Kanälen abgeleitet (Details ❓).

---

## 6. Verbleibende Klein-Fragen (nur Kanalmapping, nicht mehr Topologie)

| # | Frage |
|---|---|
| K1 | **Kanalmapping der 3 Kreis-Rücklauf-T** (wo im dev1/dev2 geloggt?) |
| K2 | **O2/O6/O7**: exakte Zuordnung (Puffer→Waermetauscher, Öl→Puffer-oben, Zirkulation)? |
| K3 | **3× Rücklaufminderer**: CMI-lesbar? (UVR610, nicht in L1-Whitelist → evtl. read-only über dev2?) |

> Topologie selbst: **vollständig / final** (HITL bestätigt).

---

## 7. Nächster Schritt

1. (Optional) K1–K3 für exakte Kanallabels klären.
2. **Statisches SVG-Hydraulikschaltplan** (technisches Design, DE-Labels, korrekte Symbole:
   3 Einrohr-Kreise, Puffer mit Zonen+Anschlüssen, Waermetauscher, Mischer, 3× Rücklaufminderer,
   alle Loops mit Fließrichtung) → `docs/hydraulic-schematic.svg` + Render-Check.
3. **Freigabe maintainer** → **animierter Web-Build** (Vanilla-JS+SVG, `/api/v1/state`-angetrieben,
   offline mit Demo-Daten) → **ersetzt die 5 CMI-PNG-Seiten**.
