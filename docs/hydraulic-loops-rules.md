# Heizungsanlage — Textuelle Kreise & Abhängigkeiten (kurze Regeln)

> **Status: zum Abgleich mit maintainer (2026-10-29, letzter Konsens-Schritt).**
> **Basis:** `docs/hydraulic-topology.md` (FINAL nach maintainer, 2026-10-28) +
> `docs/hydraulic-schematic.svg` V6.2 (WW-Modus + Paare oben/unten je getrennt, Korr. 2026-10-29) +
> `docs/plant-model.md` (Kanal-Belegung).
> **Zweck:** eindeutiger gemeinsamer Referenzpunkt für Mensch **und** Maschine
> (LLM-Kontext, S4N-2 animierter Build, S6-Training). Regeln = prüfbar formulierte
> Aussagen mit ID-Referenzen (H1–H3, P, W, O1–O9, SV, M, RM1–3, a/d-Kanäle).
>
> **Legende:** ✅ belegt/HITL-bestätigt · ⚠️ Ableitung/provisorisch · ❓ offen (K1–K3).

---

## 0. System in einem Satz

Der Pufferspeicher **P** wird von drei Wärmeerzeugern geladen — **H1 Holzofen** (eigener
Kreis, berührt nur P), **H3 Solar** (Spirale in P-unten) und **H2 Ölheizung** (umschaltbar:
Heiz-Modus speist die Heizkreise, WW-Modus speist P-ganz-oben) — und versorgt daraus die
**Heizung E** (3 Einrohr-Kreise über Mischer + Pumpe O8) und das **Warmwasser D**
(Waermetauscher aus P-oben, Zirkulation fest 50 °C). Der Heizungsrücklauf wird über
**O9** entweder zur Ölheizung oder zurück in P **„unten"** gerichtet — 10 cm **über** dem
eigenen H1-RL-Anschluss „ganz unten" (R-X7).

---

## Loop A — Holzofen (eigener Kreis)

**Beschreibung:** H1 (Festbrennstoff) bildet einen eigenen Kreis, der mit dem Rest der
Anlage **nur den Puffer P** teilt. Vorlauf (HT) geht in P **ganz oben** (eigener, vom
H2/WW-Modus getrennter Anschluss, s. R-X8). Rücklauf kommt aus P **ganz unten** (eigener
Anschluss, 10 cm unter dem O9-(b)-Anschluss „unten", s. R-X7). Die Pumpe O1 sitzt
im Kreis. Abgas führt in E1 (Schornstein). **Zwei H1-Fühler:** a9 im Wassertank (Wasserjacket)
des Kessels und a10 im Abgas (R-A3).

**Flussrichtung:** `H1 ─[O1]─► P (VL, ganz oben)   P (RL, ganz unten) ─► H1`

**Regeln:**
- **R-A1** ✅ O1 ist der einzige Antrieb von Loop A (AUS/EIN/AUTO, `10120A10180`).
- **R-A2** ✅ Es gibt keine hydraulische Verbindung von Loop A zu den Loops B–E außer über P.
- **R-A3** ✅ Messung: **2 Fühler am H1** — a9 = **Fühler im Wassertank/Wasserjacket des Holzofens** (dev0, „Holzkessel Vorlauf" WinSol `T.Holzk.VL`; sitzt im Kessel, NICHT auf der Leitung) und a10 = **Abgas-Temperatur** (dev0) + digital „Abgas AN/AUS" (dev0, Seite 5).

---

## Loop B — Ölheizung H2 (umschaltbar, zwischen Heizkreisen und Puffer)

**Beschreibung:** H2 (Oelbrenner-R1) hängt hydraulisch **zwischen den Heizkreisen
und dem Puffer** und ist auf zwei Betriebsarten umschaltbar:
- **HEIZ-Modus:** H2 bezieht Rücklauf aus **P-Mitte** (eigener Anschluss), H2-Vorlauf geht
  **direkt in den Mischer** der Heizkreise (Loop E).
- **WW-Modus:** H2-Vorlauf geht über die Ladepumpe **O6** in **P-ganz-oben** (erwärmt die
  obere Puffer-Hälfte, aus der Loop D das WW erzeugt). *(Korrektur maintainer 2026-10-29:
  „Vorlauf aus der Ölheizung, der im Warmwassermodus in den Puffer zurückführt".)*

**Flussrichtung (HEIZ):** `P (Mitte) ─► H2 ─► Mischer (E)` · Rücklauf: `zentrale RL ─[O9]─► (a) H2  |  (b) P unte (10 cm über „ganz unten")`
**Flussrichtung (WW-Modus):** `H2 ─[O6]─► P (ganz oben)`

**Regeln:**
- **R-B1** ✅ Im HEIZ-Modus: H2-VL geht direkt in den Mischer (keine Zwischenspeicher), H2-Rücklauf kommt aus P-Mitte.
- **R-B2** ✅ Im WW-Modus: H2-VL wird über O6 in P-ganz-oben geschickt; H2 speist dann nicht die Heizkreise.
- **R-B3** ✅ O5 = Ölbrenner-Anforderung (`10520A10180`, **RISK** ⚠️ Live-Re-Read: Option AUTO/AUS).
- **R-B4** ✅ Messung: a5 = Ölkessel-Vorlauf (dev0).

---

## Loop C — Solar

**Beschreibung:** H3 (Kollektor, Dach, seit 2021-10) wird über die Solarpumpe SV auf eine
**Spirale (Waermetauscher) in der unteren Puffer-Hälfte** gepumpt. Die Wärme geht direkt
in P, es gibt **kein** separates Solar-Behältnis.

**Flussrichtung:** `H3 ─[SV]─► Spirale (P unte)   Spirale-RL ─► H3`

**Regeln:**
- **R-C1** ⚠️ SV (Auto) läuft, wenn die Kollektortemperatur (dev2 a16) deutlich über der
  Speicher-Temperatur (untere Zone) liegt; Hand-Zwang möglich (Options: Hand/AUS, Hand/EIN, Auto).
- **R-C2** ✅ Die Spirale lädt nur die untere Puffer-Hälfte (Sonne → unten, Holz → oben).
- **R-C3** ✅ Messung: dev2 a16 = Kollektor-T, dev2 d1 = Solarpumpe (binär).

---

## Loop D — Warmwasser

**Beschreibung:** WW entsteht aus der **oberen Puffer-Hälfte**: Wasser fließt aus P-oben
über den **Waermetauscher** auf das **Kaltwasser** → Warmwasser (a4). Die Zirkulation
läuft über die Pumpe O7 zu den Zapfstellen; das Ziel ist **FEST 50 °C** (nicht setzbar)
und wird durch den **Stromungsschalter dev0 a16** überwacht.

**Flussrichtung (Lade):** `P (obere Haelfte) ─[O2]─► Waermetauscher ─► auf Kaltwasser ─► WW (a4)`
**Flussrichtung (Zirk):** `WW-RL ─[O7]─► Zapfstelle(n) ─► WW` (Ring)

**Regeln:**
- **R-D1** ⚠️ O2 = Ladepumpe P→Waermetauscher (`10620A10180`, Zuordnung provisorisch, K2).
- **R-D2** ⚠️ O7 = Zirkulationspumpe WW (`10720A10180`, Zuordnung provisorisch, K2).
- **R-D3** ✅ WW-Soll = fest 50 °C (nicht setzbar); dev0 a16 (Stromungsschalter) sitzt in der Zirkulation und überwacht sie.
- **R-D4** ✅ Im WW-Modus wird die obere Puffer-Hälfte zusätzlich/direkt von H2 geladen (R-B2, O6).

---

## Loop E — Heizung (3 Einrohr-Kreise + 1 Mischer + 1 Pumpe)

**Beschreibung:** Vorlauf: P-Mitte → H2 (HEIZ-Modus) → **Mischer** (2/3-Wege-Ventil O3/O4,
C1-intern; mischt Rücklauf in den Vorlauf zur Temperatursenkung, mit RL-Bypass) →
**Pumpe O8 (nach dem Mischer)** → **VL (a6)** → Verteilung an die drei Einrohr-Kreise
**R1 Keller / R2 EG / R3 DG** (nur Heizkörper, je Kreis RAS-ventile V1, C2). Rücklauf:
jeder Kreis → **Rücklaufminderer RM1/2/3 (0–10 V, C2-geregelt)** → **zentrale RL (a7)** →
**O9 (Rücklauf-Entscheidung)**.

**Flussrichtung (VL):** `P (Mitte) ─► H2 ─► Mischer (O3/O4) ─[O8]─► VL (a6) ─► R1 + R2 + R3`
**Flussrichtung (RL):** `R1/R2/R3 ─[RM1/RM2/RM3]─► zentrale RL (a7) ─[O9]─► (a) H2  |  (b) P unte (10 cm über „ganz unten")`

**Regeln:**
- **R-E1** ✅ O8 ist die einzige Heizkreis-Pumpe (`10820A10180`), sitzt **im Vorlauf nach dem Mischer** und bedient alle 3 Einrohr-Kreise.
- **R-E2** ✅ O3/O4 = Mischkreis-Ventil, **C1-intern geregelt** (Mischkreisregler UVR1611), nicht in den 9 setzbaren Adressen (lesbar dev0 d3/d4).
- **R-E3** ✅ O9 = Rücklauf-Entscheidung (`10920A10180`, AUS/EIN/AUTO): zentraler RL geht entweder **zurück zu H2** oder **nach P „unten"** (10 cm über „ganz unten"; Zustands-Zuordnung ❓, zu klären per Read-back). O9 ist der einzige HAND/EIN-Schalter der Anlage.
- **R-E4** ✅ RM1/2/3 = je 1 Rücklaufminderer 0–10 V pro Einrohr-Kreis, **auf der UVR610 (C2) geregelt** — ausdrücklich **separat** vom Mischkreisregler (C1). Nicht in der L1-Whitelist (K3).
- **R-E5** ✅ Rücklauf-Temperatur = **4 Werte**: 1 zentral (dev0 a7) + 3 je Einrohr-Kreis (Kanalmapping ❓ K1).
- **R-E6** ⚠️ V1 = HzRk-Ventile (je 2 pro Raum), C2-geregelt (RAS-Offset vs. Raum-Temp); lesbar dev2 a10–a15 (Zuordnung ⚠️).
- **R-E7** ✅ Jeder Einrohr-Kreis hat exakt **einen** Heizkörperfähigen Ast (nur Heizkörper, keine Fußboden-/Sonderkreise).

---

## Querschnitt: Abhängigkeiten & Kopplung („das System")

- **R-X1** ✅ **H2** ist der einzige Wärmeerzeuger, der zwei Ziele speist: Loop E (HEIZ-Modus) oder P (WW-Modus, R-B2). Umschaltung = Betriebsart.
- **R-X2** ✅ **P** ist der zentrale Kopplungspunkt aller Loops:
  | P-Anschluss/Zone | Zulauf | Quelle |
  |---|---|---|
  | ganz oben (2 getrennte) | H1-VL (Loop A) **und** H2-VL (WW-Modus, O6) — je eigener Anschluss | H1, H2 |
  | obere Hälfte | → Loop D (Waermetauscher) | P → W |
  | Mitte | → H2 (HEIZ-Modus) | P → H2 → E |
  | unte (Spirale) | H3 (Loop C) | H3 |
  | unten (getrennt) | O9-(b) (E) | zentrale RL |
  | ganz unten (getrennt) | H1-RL (A) | H1 |
- **R-X3** ✅ P-Zonen: „ganz oben"/„oben" und „ganz unten"/„unten" liegen je nur **wenige cm auseinander** (2 eng beieinanderliegende Paare) — nicht als getrennte Ebenen interpretieren.
- **R-X4** ⚠️ **M (Betriebsmodus, C1, 7 Optionen, `101301D0180`)** entscheidet, welche Loops aktiv sind (Heizung / WW-Modus / …); exakte Modus-Zuordnung je Option ❓ (Details aus BDB-Logik, nicht vollständig entschlüsselt).
- **R-X5** ✅ Aussen-Temperatur a8 (dev0) → C1 (Heizkurve) → VL-Soll (Grundregelung der Anlage).
- **R-X6** ✅ Die 3 Heizkreise sind **kaskadiert über einen Punkt** (Mischer + O8): ein Ausfall von O8/Mischer trifft **alle** Räume; die Rücklaufminderer entkoppeln die Kreise stromseitig (Durchflussbegrenzung), nicht druckseitig.
- **R-X7** ✅ **Beide P-Paare sind getrennt (symmetrisch):** Oben „ganz oben"/„oben" = 2 getrennte VL-Anschlüsse (H1-VL, H2-VL/WW); unten „unten"/„ganz unten" = **2 getrennte RL-Anschlüsse** — O9-(b) mündet in **„unten"**, der H1-RL in **„ganz unten"** (10 cm = ~10 px im Schema, nicht maßstäblich). *(HITL maintainer 2026-10-29 R2: „Holz-VL und H2-VL sind getrennt, genau wie dargestellt. Unten sind es auch zwei. Der Holzofen ist ganz unten, der Rücklauf nur unten, also 10 cm höher".)*
- **R-X8** ✅ **Oberes Paar = zwei getrennte Anschlüsse (bestätigt):** „ganz oben" hat **zwei** eigenständige VL-Anschlüsse — H1-VL (Loop A) und H2-VL im WW-Modus (O6) — symmetrisch zum unteren Paar (R-X7). *(HITL maintainer 2026-10-29 R2: „Holz-VL und H2-VL sind getrennt, genau wie dargestellt".)*

---

## Steuerung & Messung (Schnellreferenz)

| ID | Funktion | Controller | Setz-Adresse (CAN-L1) |
|---|---|---|---|
| M | Betriebsmodus (7 Optionen) | C1 | `101301D0180` |
| O1 | Pumpe Holzofen (A) | C1 | `10120A10180` |
| O5 | Ölbrenner-Anforderung (B, **RISK**) | C1 | `10520A10180` |
| O6 | Ladepumpe WW vom Öl (H2→P-oben) | C1 | `10220A10180` |
| O2 | Ladepumpe WW (P→Waermetauscher) ⚠️ | C1 | `10620A10180` |
| O7 | Zirkulationspumpe WW ⚠️ | C1 | `10720A10180` |
| O8 | Pumpe Heizkreis (E, nach Mischer) | C1 | `10820A10180` |
| O9 | Ventil Heizungsrücklauf (Entscheidung) | C1 | `10920A10180` |
| SV | Solarpumpe (C) | C2 | `10040912091` (x2, Node 32) |
| O3/O4 | Mischkreis-Ventil | C1-intern | — (lesbar d3/d4) |
| V1 | HzRk-Ventile je Raum (x2) | C2-intern | — (lesbar dev2 a10–a15 ⚠️) |
| RM1/2/3 | Rücklaufminderer 0–10 V je Kreis | C2-intern | — (K3) |

**Messpunkte (dev0 = UVR1611 DS1):** a1 P-Oben · a2 P-Mitte · a3 P-Unten · a4 WW ·
a5 Öl-VL · a6 VL (nach Mischer) · a7 RL zentral · a8 Aussen · a9 Holzkessel-Wassertank · a10 Abgas ·
a16 WW-Zirk-Stromungsschalter (binär, 50 °C-Ziel).
**dev2 (UVR610, Node 32):** a16 Kollektor · d1 Solarpumpe · a4–a9 RAS Raum-Temp Keller/EG/DG ·
a10–a15 HzRk-Ventil-Ergebnisse (⚠️) · Kreis-RL-T x3 (❓ K1).

---

## Offene Punkte (K1–K3) — bekannte Grenzen, bewusst offen

| # | Frage | Auswirkung auf Regeln |
|---|---|---|
| K1 | Kanalmapping der 3 Kreis-RL-T (dev1/dev2, welche Kanäle?) | R-E5: Werte bekannt, Kanalnamen provisorisch |
| K2 | Exakte Zuordnung O2/O6/O7 | R-D1/R-D2 ⚠️; O6→H2/P-oben per maintainer 2026-10-29 bestätigt |
| K3 | RM1/2/3 CMI-lesbar? (C2, nicht L1-Whitelist) | R-E4: Regelbar aber nicht aus CMI-L1 beobachtbar |

---

## Prüfprotokoll (Konsens)

- [ ] maintainer: Regeln R-A1 … R-X8 gegen Anlagen-Realität geprüft ✅ (R2 2026-10-29: P-Paare oben & unten je getrennt, O9-(b) → „unten", H1-RL → „ganz unten")
- [ ] maintainer: Fließrichtungen je Loop bestätigt (ggf. Korrekturen → V2 dieser Datei)
- [ ] Danach: Freigabe als **Kanon-Regelwerk** für S4N-2 (animierter Build) + S6 (Training-Kontext)
