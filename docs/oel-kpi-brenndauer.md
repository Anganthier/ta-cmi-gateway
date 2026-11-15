# ÖL-KPI-1: Brenndauer × Ölverbrauch — Methodik und Befund

*Stand: 2026-11-10 (inkl. Okt-2025-Override, Neubrenner-Era +
**Q-127-Tank-Korrektur: Voll-Anker-Modell ab 01.06.23**, maintainer-Beleg
2026-11-10) ·
Datenfenster 2020-09-01 → 2026-11-10 · Implementierung:
`analysis/oil_burner_kpi.py` (CLI + Tests `tests/test_oil_burner_kpi.py`,
Artefakte `tmp/oel_kpi/`)*

## 1. Ziel

Die **effektive Brenndauer** des Ölbrenners (aus der 10-s-Timeseries abgeleitet)
mit dem **gemessenen Ölverbrauch** (Voll-Anker-Tankmodell, Q-127) in
Korrelation bringen: Wie viele Liter Öl pro effektiver Brennstunde, je
Anker-Segment, je Monat und je Heizsaison — und welche Ausfall-Fenster
(Brenner defekt / Zündausfall) die Brenndauer verfälschen.

Korrelationsfenster: **2020-09 → 2026-11**. Davor existieren keine
Brennerdaten (Timeseries startet 2020-08-30). **Für den KPI ist das Tank-Modell
erst ab 01.06.2023 verwertbar** (Q-127, §5): davor wurden die 2021/22-
Lieferungen nicht notiert.

## 2. Datenquellen (read-only)

| Quelle | Pfad | Inhalt |
|---|---|---|
| Winsol-PQ | `data/winsol_pq/YYYY/MM/YYYY-MM.parquet` | 10-s-Grid, 71 Spalten, 2020-08-30 → 2026-10-27 |
| CMI-Log-PQ | `data/cmi_log_pq/YYYY/MM/YYYY-MM.parquet` | Fortführung, 74 Spalten, 2026-10-27 → heute, identische `dev0`-Namen |
| Tank-CSV | `tmp/tank_zollstock_2015_2026.csv` | Zollstock-Messungen (Zeilen, `;`, **keine Header-Zeile**) — wird für den **Referenz-Historie-Block** geparst; das KPI-Tank-Modell selbst ist der **explizite Anker-Satz** (§5, maintainer-Beleg 2026-11-10) |

Kanäle (Kanal-Kanon, verifiziert im Recon 2026-11):

| Spalte | Bedeutung |
|---|---|
| `dev0_digital_05` | **O5 „Anforderung Ölbrenner“** (0/1) — UVR-Anforderung, O5 = ODER(AnfHeiz, AnfWW) |
| `dev0_analog_05` | **Öl-Kessel-Vorlauftemperatur** (°C) |
| `dev0_analog_08` | Außentemperatur (°C) |
| `dev0_analog_09` | Holz-Kessel-Vorlauftemperatur (°C) — Holz-Proxy |
| `dev0_digital_06` | **O6 „Ladep. WW. 5“** — Öl-Warmwasser (Kontext) |

Hinweis: `dev0_digital_01` existiert in den Daten nicht.

## 3. Effektive Brennstunde (Brennstunden-Proxy)

Ein 10-s-Sample zählt als „gebrannt“ IFF

```
dev0_digital_05 = 1  (Anforderung O5)  ODER-Kanal aktiv
UND
dev0_analog_05 >= T_IGN  (Kessel-Vorlauf warm = Kessel tatsächlich brennt)
```

mit **T_IGN = 30 °C (Default)**. Rationale: der Öl-Kessel-Vorlauf liegt im
Betrieb typischerweise bei 60–85 °C; bei Zündausfall bzw. im Kaltstart
vor der Zündung bleibt er bei 14–45 °C. 30 °C trennt die beiden Zustände
klar (Beleg: strikte Ausfall-Tage haben Tages-Max 14–31 °C, normale
Heiztage fast immer > 60 °C).

**Sensitivität (Aufgaben-Verpflichtung, Werte aus dem CLI-Run):**

| T_IGN | effektive h | Abweichung vs. 30 °C |
|---|---|---|
| 28 °C | 11.128,6 | +0,23 % |
| **30 °C** | **11.103,4** | — |
| 35 °C | 11.029,8 | −0,66 % |

Die Wahl von T_IGN verschiebt die Gesamtbrenndauer nur um < 1 % — die
Kern-KPIs sind gegen den Schwellwert robust.

**Grenzfälle, die bewusst NICHT zählen:**

- *Kalt-Start/Rampe* (d5=1, a5 < 30 °C): der Kessel brennt noch nicht.
- *Nachglühen* (d5=0, a5 warm): der Brenner ist aus — Vorlauf kühlt nur ab.
- *WW-Zyklen* (O6): sie stecken in O5 (ODER-Kanal) und zählen korrekt als
  Brenndauer (der Brenner läuft tatsächlich).

## 4. Ausfall-Tag-Regel (2 Stufen) + Beleg

„Kalt-Anforderung“ = d5=1 UND a5 < 30 °C (Anforderung vorhanden, Kessel
bleibt kalt = Zündung schlägt (weitgehend) fehl).

| Stufe | Kriterium | Konsequenz |
|---|---|---|
| **strikt** | Kalt-Anforderung **≥ 4 h/Tag** (Aufgaben-Schwelle) **UND** Tages-Max a5 < 40 °C | Tag wird **vollständig aus der effektiven Brenndauer entfernt** |
| **intermittent** | Kalt-Anforderung **≥ 2,5 h/Tag** UND Tages-Max a5 ≤ 78 °C | Tag liegt im **Ausfall-Fenster** (gelistet), wird aber **nicht** exkludiert („manchmal hat es geglückt“ — geglückte Samples zählen) |

**Beleg für die Schwellen (Daten):**

- Im bekannten **Jan-2026-Ausfall** (14.–29.01.) summieren sich
  **102.571 Samples mit d5=1 und a5 < 30 °C** (Q1-2026) vs. **< 1.700** in
  jedem Q1 von 2021–2025 — ein eindeutiges, 60×-stärkes Signal.
- Die täglichen Kalt-Anforderungsstunden an den Ausfall-Tagen liegen bei
  **3,1–24,0 h** (nahezu durchgehend unbediente Anforderung in
  kurzen Bursts, max. ~1,2 min). Die stärksten Nicht-Ausfall-Tage
  bleiben bei **≤ 2,4 h** Kalt-Anforderung (stärkster Kandidat:
  28.11.2025 mit 2,43 h; Tage darüber haben a5-Max > 78 °C und scheiden
  über die Temperatur-Obergrenze aus). Die **4-h-Schwelle der Aufgabe
  liegt also in der Lücke** und ist ohne Verfeinerung erreichbar —
  die strikte Klassifikation nutzt sie 1:1.
- Die **2,5-h-Kandidaten-Schwelle** (Fenster-Mitgliedschaft, nicht
  Exklusion) ist in dieselbe Lücke kalibriert: schwächster
  Ausfall-Kandidat-Tag = 19.10.2025 mit 3,12 h; stärkster Nicht-Ausfall-Tag
  mit a5-Max ≤ 78 °C = 28.11.2025 mit 2,43 h.
- Fenster-Gruppierung: Kandidat-Tage mit max. **1 Zwischen-Tag**
  (Gap-Bridge, z. B. ein Lucky-Ignition-Tag) bilden ein Fenster; ein
  Fenster zählt als echter Ausfall erst mit ≥ 1 striktem Tag.
  (Einzeltage wie 12./13.11.2024, 24.09.2025, 12.12.2025, 15.02.2026,
  22.08.2026 — intermittierend, aber kein strikter Tag in der Nähe —
  bilden daher keine Fenster: nur Zünd-Rampen an kalten Tagen.)

**Erkannte Ausfall-Fenster 2020–2026 (maintainer-Check!):**

| Fenster | Kandidaten-Tage | strikt (exkludiert) | intermittierend | Kalt-h gesamt |
|---|---|---|---|---|
| **12.10.2025 → 21.10.2025** | 9 | 5 (Override → 0 exkludiert) | 4 (+5 Override) | 164,0 h |
| **14.01.2026 → 29.01.2026** | 15 | 5 (15., 22., 24.–26.01.) | 10 | 259,1 h |

**⚠ maintainer-Override (2026-11-10, autoritativ):** Die 5 „strikten“ Tage
**13.–17.10.2025** sind **keine** Total-Ausfälle. maintainer-Beleg: „jeden
Tag/Nacht mindestens einer oder zwei Zündversuche mit erfolgreichen
Neustarts.“ Die 5 Tage werden daher per Override als *intermittierend*
geführt (gelistet im Fenster, **nicht** exkludiert). Konsequenz: Das
Okt-2025-Fenster hat **0 strikte Tage** und exkludiert **nichts** aus der
Brenndauer. Die Daten zeigen an den 5 Tagen a5-Max ≤ 22,2 °C und 0 warme
Samples — beides passt ohne Widerspruch zum maintainer-Beleg (kurze, wieder
erfolgreiche Neustarts, keine durchgehende Zündung > 30 °C). Der **einzige
echte Totalausfall** bleibt **Jan-2026** (14.–29.01., 5 strikt + 10
intermittierend).

Ausfall-Korrektur gesamt: **−117,6 h** roh d5 an den 5 strikten
Jan-2026-Tagen (Override aktiv, Default); −0,0 h warm (an den strikten
Tagen war a5-Max ≤ 29,2 °C, es gab keine warmen Samples, die exkludiert
werden müssten). Ohne Override (`--no-oct2025-override`) wären es
−237,6 h an 10 strengen Tagen (Okt + Jan) — die eff. h bleiben in beiden
Fällen 11.103,4 (die Okt-Tage hatten 0 warme Samples).

## 5. Tank-Segmente und Verbrauchs-Buchung (Q-127-Korrektur)

**Q-127 (maintainer, 2026-11-10, autoritativ):** Das KPI-Tank-Modell ist **kein**
naiver Verbrauch aus der Zollstock-Messreihenfolge mehr, sondern ein
**Voll-Anker-Modell ab 01.06.2023**. Alles davor ist unverwertbar, weil die
**2021/2022-Lieferungen nicht notiert** wurden.

- **Voll = 4.500 L.** Der Full-Sensor löst ein paar cm unter dem 4.800-L-
  Physikalischen-Maximum aus → jede Voll-Lieferung füllt auf genau
  **4.500 L / 1.500 mm**. Die CSV-Spalte `fehlmeng` ist auf allen Zeilen
  exakt `4800 − ltr` = **Formel-Noise** und wird — wie `ltr_pro_tag_owner` —
  **nie** verwertet.
- **Voll getankt exakt 5×:** 27.01.20, 16.11.20, 01.06.23 (3.500 L),
  31.10.24 (2.945 L), 23.10.25 (2.947 L). 2021 & 2022: Tank-Mengen **nicht
  notiert**.
- **KPI-Anker-Kanon (ab 01.06.23, die einzigen verwertbaren Segmente):**

  | Segment | Spanne | Liter | Präzision | eff. h | L/h |
  |---|---|---|---|---|---|
  | **S1** | 01.06.23 → 31.10.24 | **2.945 L exakt** (Voll→Voll) | exakt | 1.745,8 | **1,687** |
  | **S2** | 31.10.24 → 23.10.25 | **2.947 L exakt** (Voll→Voll) | exakt | 1.905,7 | **1,546** |
  | **S3** | 23.10.25 → 09.11.26 | **2.670 L grob** (= 4.500 − 1.830) | cm-Ablesung ±300 L | 1.629,6 | **1,638** [1,45–1,82] |
  | S3a (Sub) | 23.10.25 → 21.08.26 | 2.430 L (= 4.500 − 2.070) | cm-Ablesung ±300 L | 1.522,8 | 1,596 |
  | S3b (Sub / Mini) | 21.08.26 → 09.11.26 | 240 L (= 2.070 − 1.830) | cm-Ablesung ±300 L | 106,8 | 2,247 [0,00–5,06] |

  Konsistenz: 2.430 + 240 = 2.670 L = the maintainer's grober S3-Total ✓.
  **Gesamt ab 01.06.23:** 8.562 L / 5.280,7 eff. h = **1,621 L/h**.
- **Fenster-Konvention:** Start = Liefer-/Mess-Tag **+ 1** (exklusiv),
  Ende = nächster Liefer-/Mess-Tag (inklusive). S2 zählt daher
  01.11.24 → 23.10.25 — die Grenz-Tage angrenzender Segmente werden nicht
  doppelt gezählt (Tage-Anzahl = exklusiver Differenzwert: S1 518 d,
  S2 357 d, S3 382 d, S3a 302 d, S3b 80 d).
- **Sub-Segmente S3a/S3b** sind Zwischen-Ablesungen **innerhalb** von S3 und
  werden **nicht additiv** in KPIs (Saisons, Monats-Interpolation,
  Baseline, Trend) eingerechnet — nur S1/S2/S3 fließen ein; S3b dient als
  sauberes Mini-Segment im Era-Block.
- **Alter-Brenner-Baseline (S1+S2):** 5.892 L / 3.651,5 eff. h =
  **1,614 L/h**, davon O6 (WW-Öl) 2.850,2 h = **78,1 %** der Effektivzeit
  (Kernzahl, §7).
- **Alles vor 01.06.23 = NICHT verwertbar** (ungelöste 2021/22-Lieferungen):
  Die Zollstock-Messreihen-Segmente werden weiterhin geparst und im
  Report/Doku als **Referenz-Historie** gelistet (`usable=False`,
  „nicht verwertbar“), fließen aber in **keinen** KPI ein — keine Saisons,
  kein Trend, keine Monats-Interpolation, kein Alter-Brenner-Wert.
  Auch Zwischen-/Fehlmess-Zeilen ab 06/2023 (z. B. 07.02.24, 07.09.24,
  07.10.24, 19.10.25, 19.10.25→23.10.25) sind außerhalb des Anker-Kanons
  und nicht verwertbar (ersetzt durch S1/S2/S3).
- **Öl-Verbrauch je Kalendermonat** (Interpolation): Monats-Öl =
  Anker-Segment-Ø-L/d × Tage des Monats, **nur für Monate, die vollständig
  in einem Anker-Segment (S1/S2/S3) liegen** (39 Monate, ab 2023-07);
  jeder Eintrag trägt das **„geschätzt“-Flag**. Vor 06/2023: keine
  Interpolation (Q-127). L/eff. h (Monat) = geschätzte Liter / effektive
  Brennstunden des Monats.

## 6. Anomalien-Handling (Q-127: A1/A2 GELÖST)

- **A1 (Okt-2025) → GELÖST: Eintrags-Versatz.** Die 2.945-L-Menge in der
  CSV-Zeile 19.10.25 gehört zur Lieferung **31.10.24** (Voll getankt →
  4.500 L). Es gab **keine** Überfüllung — die 19.10.25-Zeile ist ein
  Fehleintrag/Rest und wird gelistet, aber **nicht** verwertet. Die alten
  A1-Varianten A/B entfallen damit komplett.
- **A2 („0 Verbrauch im Okt 2024“) → GELÖST: ungeloggte 31.10.24-Lieferung.**
  Das Referenz-Segment 07.10.24 → 19.10.25 (0 L/42 d) ist die ungeloggte
  31.10.24-Lieferung (2.945 L, Menge in die 19.10.25-Zeile eingetragen) —
  selbe Ursache wie A1.
- **A4 / „Zunahme“-Segmente (19.11.22→02.12.22 mit 29 L/d, −1.620 L,
  −2.280 L, −270 L …):** liegen **vor 06/2023** → außerhalb des
  KPI-Fensters, nicht verwertbar (Q-127). Der frühere A4-Bilanz-Wert
  (c = 2.000 L/181 d) entfällt als KPI.
- **Pre-2023-06-Segmente gesamt:** ungelöste 2021/22-Lieferungen
  (maintainer) → Referenz-Historie, `usable=False`, kein KPI.
- **Noise-Spalten:** `fehlmeng` (auf allen Zeilen = 4.800 − ltr) und
  `ltr_pro_tag_owner` sind Formel-Noise und werden nicht verwertet.

## 7. Befund (Kernzahlen, Default-Run T_IGN = 30 °C)

**Brenndauer gesamt (2020-09-01 → 2026-11-10, 2239 beobachtete Tage,
19.314.967 Samples):**

| Kennzahl | Wert |
|---|---|
| Roh-Brenndauer (d5=1) | **11.603,4 h** |
| Effektive Brenndauer (d5=1, a5 ≥ 30 °C, Ausfall-Tage exkludiert) | **11.103,4 h** |
| davon exkludiert (5 strikte Ausfall-Tage, Okt-Override aktiv) | −117,6 h roh / −0,0 h warm |

**Effektive Brennstunden je Jahr:**

| Jahr | eff. h | roh h |
|---|---|---|
| 2020 (ab 30.08., Teiljahr) | 898,4 | 898,4 |
| 2021 | 2.376,2 | 2.376,2 |
| 2022 | 1.561,1 | 1.563,5 |
| 2023 | 1.613,3 | 1.614,7 |
| 2024 | 1.726,7 | 1.739,2 |
| 2025 | 1.876,5 | 2.064,9 |
| 2026 (bis 10.11., Teiljahr) | 1.051,2 | 1.346,5 |

**L/h je Heizsaison (09.10. → 30.04., Anker-Liter pro-rata über
Segment-Tage / effektive h; Q-127: nur die 4 Saisons mit Tank-Deckung ab
06/2023):**

| Saison | Tage Deckung | Öl L | eff. h | **L/h** | Außen-Ø °C |
|---|---|---|---|---|---|
| 2023/24 (S1) | 213 | 1.211 | 1.451 | **0,84** | 8,6 |
| 2024/25 (S1+S2) | 213 | 1.679 | 1.714 | **0,98** | 7,6 |
| 2025/26 (S2+S3) | 213 | 1.518 | 1.543 | **0,98** | 7,8 |
| 2026/27 (S3, läuft) | 40 | 280 | 78 | 3,59* | 10,7 |

\* Teil-Saison (2026/27: nur 09.10. → 10.11., „läuft“) — kein
Voll-Saison-Wert. Die Saisons 2020/21–2022/23 entfallen aus den KPIs
(Q-127: ungelöste 2021/22-Lieferungen → kein verwertbares Öl-Liter-Wert,
§5).

**Holz-Anteil-Semantik:** Der Holz-Anteil (holz_h / Öl-Effektivzeit) ist
nur bis 100 % aussagekräftig. In 2026/27 war Holz die primäre
Wärmequelle im Zeitraum (Okt-2026 = Rekord-Holz-Monat, Holz-h 321,4 h,
Öl-Effektivzeit nur 77,8 h) → der Anteil wird **nicht** cappel, sondern
als **NULL gelistet mit Note** („Holz-h > Öl-h (Holz primäre Wärmequelle
im Zeitraum)“, Report-Zelle „— (Holz dominiert)“). Die Voll-Saisons liegen
bei 3–12 % (Kontext).

**Trend (Primary = nur Voll-Saisons, n = 3):** linearer Fit über die drei
Voll-Saisons mit Tank-Deckung (2023/24, 2024/25, 2025/26):
**Steigung ≈ +0,074 L/h pro Jahr (R² = 0,78)** — praktisch **flach**.
Die Teil-Saison 2026/27 (3,59 L/h auf nur 78 eff. h durch
S3-pro-rata-Interpolation + Rekord-Holz-Monat) ist **nicht vergleichbar**
und wird aus dem Primary-Fit ausgeschlossen. Inkl. Teil-Saison (n = 4)
würde der Fit +0,828 L/h/Jahr (R² = 0,64) ergeben — dieser „Anstieg“ ist
ein **Artefakt der Teil-Saison, kein Beleg** für Verhaltensänderung
(bestätigt die Q-126-Feststellung; saubere Trennung erst mit
v0.6 L/Heizgradtag). Honest-Disclaimer: n ist klein, und Saisons
unterscheiden sich in Außen-Temperatur (HDD-15: 1.406–1.829),
Holz-Anteil und WW-Nutzung (O6-Stunden im Sommer = Kontext, 442–459 h).
Höheres L/h in kalten Saisons / mit hohem WW-Anteil ist Physik, kein
Drift. Ein sauberer Drift-Beleg würde eine HDD-normierte
Vergleichsgröße brauchen. Der Trend-Wert ist daher nur ein Hinweis,
kein Beleg.

**Alter-Brenner-Baseline (Kernzahl, Q-127):** S1+S2 = **5.892 L / 3.651,5
eff. h = 1,614 L/h** (O6-Öl 2.850,2 h = 78,1 % der Effektivzeit). Das ist
der „normale“ Verbrauchsbereich des Ölbrenners: **1,55–1,69 L/h je
exaktem Anker-Segment (S1 1,687 / S2 1,546 / S3 1,638)**.

**Plausibilitätsband (Q-127):** Die Anker-Segmente liegen bei
**1,55–1,69 L/h** (Ø ≈ 1,62 L/h) → der Ölbrenner verbraucht im Mittel
rund **1,6 Liter pro effektiver Brennstunde**. (Vollast-Ölbrenner dieses
Grades: Nennverbrauch typ. 40–60 L/h; der Kessel läuft stark
zyklisch/teilbeladen → plausibel.)

## 7.5 Neubrenner-Era (01.02.2026 → 09.11.2026, Q-127)

Ab **01.02.2026** läuft ein kompletter **Ersatz-Brenner** (gleiche
Marke/Modell). Data-Sanity: seit 01.02.2026 sind **exakt 0 strikte
Ausfall-Tage** (verifiziert im Modul; im Era-Block als Sanity-Check
ausgeben). Die Era endet am **S3-Ende 09.11.2026** (letzte Tank-Ablesung) —
nicht am Fenster-Ende (10.11.2026): der S3-Anker ist die Öl-Basis der Era,
deshalb 281 Öl-Tage (01.02.26 → 09.11.26, Start exklusive).

- Effektive Brenndauer in der Era: **786,2 h** (roh 822,3 h)
- O6 (WW-Öl): **732,3 h = 93,1 % der Effektivzeit** · Außen-Ø 15,0 °C

**Öl-Verbrauch in der Era (drei Szenarien, Basis S3-Anker):**

1. **S3-Ø-Rate (grob, „Szenario A“):** S3-Rate 2.670 L / 382 d =
   **6,99 L/d × 281 d = 1.964,1 L** / 786,2 h = **2,498 L/h**
   (±300 L Band: **2,12–2,88 L/h**).
2. **Sub-Split über S3a/S3b („Szenario B“):** S3a-Rate 2.430 L / 302 d =
   8,05 L/d × 201 d (01.02. → 21.08.) + S3b 240 L = **1.857,3 L** /
   786,2 h = **2,362 L/h** (±300 L Band: **1,98–2,74 L/h**).
3. **Sauberes Mini-Segment S3b 21.08.26 → 09.11.26** (Zwischen-Ablesungen
   2.070 L → 1.830 L): 240 L / 106,8 eff. h = **2,247 L/h** (±300 L
   konservativ: **0,00–5,06 L/h**; bei typisch ±150 L/Messung: obere
   Grenze 3,65 L/h).

**Einordnung (ehrlich):** **2,36–2,50 L/h** (Mini-Segment 2,25 L/h;
Baseline alter Brenner 1,61 L/h) liegt **ca. 45–55 % über** der alten
Baseline (S1+S2). **Primärer Erklärungskandidat: der O6-Anteil in der
Effektivzeit** — in der Era brennt der Ölbrenner zu **93,1 %** für
Öl-Warmwasser (O6 732,3 h / 786,2 h; O6 23,0–109,4 h/Monat, im
Mini-Segment 176,7 h O6), während der **alte Brenner nur 78,1 %** O6-Anteil
hatte (2.850,2 h / 3.651,5 h). Weitere Verdachtsmischung: mildere
Witterung (Außen-Ø Era 15,0 °C vs. S1 14,3 °C / S2 12,2 °C —
Fenster-zu-Fenster, nicht direkt vergleichbar) und die grobe S3-Anker-
Messung (±300 L). **Interpretation als „Brenner verbraucht mehr“ mit
v0.6 (L/Heizgradtag, um Holz korrigiert) — erledigt mit v0.6-D, §7.6.**

**Effektiv-Brennstunden je Monat (Era, aus dem CLI-Run):**

| Monat | eff. h | O6-h (WW-Öl) | Außen-Ø °C |
|---|---|---|---|
| 2026-02 | 266,6 | 86,0 | 6,4 |
| 2026-03 | 206,1 | 109,4 | 8,6 |
| 2026-04 | 115,6 | 87,5 | 11,3 |
| 2026-05 | 63,9 | 77,7 | 16,2 |
| 2026-06 | 19,7 | 68,1 | 21,7 |
| 2026-07 | 5,2 | 70,5 | 21,5 |
| 2026-08 | 5,4 | 90,4 | 21,2 |
| 2026-09 | 27,4 | 48,2 | 17,8 |
| 2026-10 | 62,4 | 71,5 | 12,2 |
| 2026-11 (→09.11.) | 14,1 | 23,0 | 6,6 |

## 7.6 Kompass-Metrik v0.6: L/Heizgradtag (HDD-15), um Holz-Anteil korrigiert (v0.6-D)

**Kompass-Metrik (v0.6-D, maintainer 2026-11-09/12):** Der Öl-Verbrauch je
Heizgradtag ist erst dann zwischen Saisons/Monaten vergleichbar, wenn der
**Holz-Anteil der Wärmelieferung** sichtbar/korrigiert ist — der Holzkessel
(Aktivitäts-Proxy: Vorlauf `a9` > 65 °C, `WOOD_ACTIVE_DEG`) lieferte z. B.
im Okt-2026 **176,8 h auf 13 Tagen** (Rekord, davor 0–5 aktive Tage/Monat)
und übernahm einen großen Teil der Heizung. Pro KPI-Periode (Anker-Segment,
Heizsaison, Kalendermonat) werden deshalb **drei Werte immer
nebeneinander** ausgewiesen:

1. **roh:** `L/HDD-15 = Liter / HDD-15` (HDD-15 aus demselben Zeitraum,
   bestehende Tageslogik: `max(0, 15 − ext_Ø)` je Kalendertag).
2. **Holz-Kontext:** Öl-Heizstunden-Anteil
   `oel_heiz_share = oel_heiz_h / (oel_heiz_h + wood_h)` mit
   **`oel_heiz_h` = effektive Brennstunden EXKL. O6 (WW)** — O6-Submenge
   `d5=1 AND a5≥T_ign AND o6=1` (nur WW-Stunden, die ohnehin „effektiv“
   sind, werden abgezogen; Sommer-O6 bleibt Kontext wie im Rest des
   Moduls). `holz_share = 1 − oel_heiz_share` (= `wood_h /
   (wood_h + oel_heiz_h)`, 0–100 %).
3. **korrigiert:** `L/HDD-15_korr = L / (HDD-15 × oel_heiz_share)` =
   „Liter pro Heizgradtag, den der Ölbrenner eigentlich selbst liefern
   musste“ (bei 50/50-Teilung ≈ doppelte Roh-KPI).

**Spannen-Konventionen:** Segmente wie im Rest des Moduls (Start-Tag
exklusiv, End-Tag inklusiv); Heizsaisons 09.10. → 30.04. (Saisons ohne
Tank-Deckung = nicht gelistet, Q-127); Monate = **per-Tag-Interpolation**
aus den Top-Level-Anker-Segmenten (je Kalendertag die Rate aller
Segmente, deren Verbrauchsspanne den Tag abdeckt; Grenz-Monate dadurch
korrekt, z. B. 10/2025 = 23 d S2 + 8 d S3 (0 geschätzte Tage); 06/2023 hat
30 v. 31 Tagen S1-Deckung, wird aber wegen HDD-15 = 2,2 < 5 nicht
gelistet; `estimated_days` zählt Tage ohne Deckung). Sub-Segmente S3a/S3b werden nie
additiv verrechnet (Q-127); S3b erscheint als Mini-Segment-Zeile.

**Kernzahlen (CLI-Run 2026-11-12, Default T_ign = 30 °C):**

| Periode | Liter | Präzision | HDD-15 | Holz-h | Öl-Heiz-h | Holz-Share % | L/HDD roh | L/HDD korr. |
|---|---|---|---|---|---|---|---|---|
| S1 (01.06.23→31.10.24) | 2.945 | exakt | 1.531 | 172,0 | 1.360,4 | 11,2 | **1,92** | **2,17** |
| S2 (31.10.24→23.10.25) | 2.947 | exakt | 1.624 | 97,7 | 1.603,3 | 5,7 | **1,82** | **1,93** |
| S3 (23.10.25→09.11.26) | 2.670 | grob ±300 L | 1.682 | 377,4 | 1.384,0 | 21,4 | **1,59** | **2,02** [1,79–2,25] |
| S3b-Mini (21.08.26→09.11.26) | 240 | grob, geschätzt | 176 | 316,5 | 95,1 | 76,9 | **1,37** | **5,92** [0,00–13,31] |

| Heizsaison | Öl L (pro-rata) | HDD-15 | Holz-Share % | L/HDD roh | L/HDD korr. |
|---|---|---|---|---|---|
| 2023/24 | 1.211,0 | 1.404 | 12,9 | **0,86** | **0,99** |
| 2024/25 | 1.678,6 | 1.559 | 3,9 | **1,08** | **1,12** |
| 2025/26 | 1.517,9 | 1.497 | 7,8 | **1,01** | **1,10** (S3-Teil: ±300 L) |
| 2026/27 (Teil, läuft) | 279,6 | 179 | 84,7 | **1,56** | **10,23** (S3-Teil, Teil-Saison) |

- **n = Saisons = 4** (Q-127: Tank-Deckung ab 06/2023). 2026/27 ist die
  laufende Teil-Saison (Okt→Nov 2026) — pro-rata, kein Voll-Saison-Wert;
  ihr Wert driftet mit jedem neuen Daten-Tag (daher im Test
  Intervall-Assertion).
- Monats-Block: 27 Monate (2023-10 … 2026-11), degenerierte
  Sommer-/Schulter-Monate (< `MIN_MONTH_HDD15` = 5 HDD-15) werden
  übersprungen (z. B. 2025-08: 0,1 HDD — dort würde jede Standby-Literzahl
  Hunderte L/HDD ergeben).
- **±300-L-Band des S3/Tankmodells wird auf die KPI getragen** (Band ÷
  HDD ÷ Öl-Anteil; untere Grenze 0-geschnitten). S1/S2 exakt → kein Band.

**Beispiel Okt-2026 (Rekord-Holz-Monat) vs. Vorjahre:**

| Monat | HDD-15 | Holz-aktive Tage | Holz-h | Öl-Heiz-h | Holz-Share % | Öl L | L/HDD roh | L/HDD korr. |
|---|---|---|---|---|---|---|---|---|
| 2026-10 | 96 | **13** (alle Holz-primär) | 176,8 | 54,5 | 76,4 | 216,7 | **2,25** | **9,54** |
| 2025-10 | 106 | 4 | 46,0 | 140,2 | 24,7 | 245,8 | **2,31** | **3,07** |
| 2021-10 | 112 | 8 | 39,0 | 92,8 | 29,6 | — | — | — (kein Anker, Q-127) |

**Lesart:** Okt-2026 hatte **weniger** HDD (milder, Außen-Ø 12,2 °C) UND
einen Rekord-Holz-Anteil — ohne Korrektur wäre die Roh-KPI (2,25) fast
ebensogut wie 2025 (2,31), obwohl der Ölbrenner im Okt-2026 nur auf ~24 %
des Öl-Heiz-Auftrags kam (Holz übernahm ~76 % der Heizlast). Der
**korrigierte** Wert (÷ Öl-Heiz-Anteil) zeigt den „Öl-Leistungsdruck“ pro
Heizgradtag: 9,54 vs. 3,07 (Okt-2025) — d. h. an den Tagen, an denen der
Ölbrenner lief, war seine Last pro Heizgradtag ~3× so hoch. (Anmerkung:
die spec-Angabe „8 Holz-aktive Tage“ trifft die Daten nicht — bei
`a9 > 65 °C` sind es 13 Tage; 8 Tage waren es im Okt-2021.) Vor 06/2023
(2021-10) existiert kein Tank-Anker → nur Kontext, kein KPI (Q-127).

**Limitation (ehrlich, steht auch im Report/JSON):** die Korrektur ist ein
**Stunden-Proxy** (Holzkessel-Vorlauf `a9` als Aktivitätssignal), **keine
Wärme-Energiebilanz** — ein „Holz-Wärmenäquivalent abziehen“ ist aus
diesen Daten nicht verteidigbar; die Unsicherheit des Holz-Shares selbst
(VL-Proxy, keine Wärmemessung) wird nicht modelliert.

## 8. Limitationen

- **WW in d5 enthalten:** O5 = ODER(AnfHeiz, AnfWW) → Öl-Warmwasser-Zyklen
  zählen als Brenndauer (physikalisch korrekt: der Brenner läuft).
  Sommer-O6-Stunden sind deshalb Kontext, keine Exklusion.
- **ON/OFF-Physik (F3):** Ölbrenner ohne Lastmodulation → kurze Zyklen;
  Zünd-Rampen < T_IGN und < 10 s sind vernachlässigbar.
- **Tank-Modell (Q-127):** ±150–300 L Zollstock-Genauigkeit (±50–100 mm)
  gilt **nur** für das S3-Ende (1.830 L) und die Zwischen-Ablesungen
  (2.070 L / 1.830 L, cm-Präzision) → Segment-Band ±300 L (S3:
  1,45–1,82 L/h; Mini-Segment 0,00–5,06 L/h, konservativ). **S1/S2 sind
  exakt** (Voll→Voll, 4.500-L-Anker, ±0 L). Die CSV-Spalte `fehlmeng`
  (4.800 − ltr, auf allen Zeilen) ist Formel-Noise und nicht verwertet.
  Alles vor 01.06.2023 = Referenz-Historie (ungelöste 2021/22-Lieferungen),
  kein KPI.
- **„geschätzt“-Flag:** Monats-Öl ist eine Interpolation
  (Anker-Segment-L/d × Monatstage), gilt nur für vollständig im
  Anker-Segment liegende Monate; Monate über Segment-Grenzen/Lieferungen
  fehlen. Vor 06/2023: keine Interpolation (Q-127).
- **2015 → 2020-08: keine Brennerdaten** → Korrelation erst ab 2020-09;
  frühere Tank-Segmente sind für L/h unverwertbar (geliefert, gelistet).
- **Teil-Saison 2026/27** (09.10. → 10.11.): „läuft“, ihr L/h (3,59) ist
  kein Voll-Saison-Wert (pro-rata-Interpolation + kurzer Zeitraum) und
  wird aus dem **Primary-Trend-Fit ausgeschlossen** (n = 3 Voll-Saisons,
  flach +0,074 L/h/Jahr); inkl. Teil-Saison (n = 4) würde sie den Fit
  dominieren (+0,826 L/h/Jahr, Stand Live-Daten 2026-11-12 — driftet mit
   neuen Daten-Tagen) — Artefakt, kein Beleg für
  Verhaltensänderung.
- **Saisons n = 4 (Q-127):** nur 4 Saisons mit Tank-Deckung ab 06/2023
  (2023/24 S1, 2024/25 S1+S2, 2025/26 S2+S3, 2026/27 S3 teils) — der
  Primary-Trend-Fit (n = 3) hat entsprechend niedrige Statistik.
- **Holz-Anteil > 100 % (2026/27):** holz_h (321,4 h) übersteigt die
  Öl-Effektivzeit (77,8 h), weil Okt-2026 ein Rekord-Holz-Monat war
  (Holz primäre Wärmequelle) → Anteil wird NICHT cappel, sondern als
  NULL + Note gelistet („Holz-h > Öl-h (Holz primäre Wärmequelle im
  Zeitraum)“); Report-Zelle „— (Holz dominiert)“. Generisch: Anteil nur
  wenn holz_h ≤ eff_h, sonst NULL + Note.
- **Neubrenner-Era** (§7.5): beide Voll-Era-Szenarien stützen sich auf
  den groben S3-Anker (±300 L → Band 2,12–2,88 bzw. 1,98–2,74 L/h); das
  Mini-Segment (0,00–5,06 L/h) ist am weitesten von der Baseline
  entfernt. Auslegung als „Brenner verbraucht mehr“ mit v0.6
  (L/Heizgradtag, um Holz korrigiert) — **erledigt mit v0.6-D (§7.6):**
  der L/h-Verdrillung von ~45–55 % bleibt auch nach der
  Holz-Korrektur (S3 korr. 2,02 [1,79–2,25] vs. Baseline 1,61, exakte
  Basis S1+S2), primärer Erklärungskandidat bleibt der O6-Anteil
  (93 % in der Era vs. 78 % im alten Brenner) plus die mildere Witterung.
- **v0.6-Kompass-Metrik** (§7.6): die Holz-Korrektur ist ein
  **Stunden-Proxy** (Holzkessel-Vorlauf `a9` > 65 °C als
  Aktivitätssignal), **keine Wärme-Energiebilanz** — die Unsicherheit des
  Holz-Shares selbst (VL-Proxy, keine Wärmemessung) wird nicht
  modelliert. Der ±300-L-Band des S3/Tankmodells gilt auf die KPIs
  (S3: korr. 2,02 [1,79–2,25]; S3b-Mini: [0,00–13,31]); S1/S2 sind exakt
  (kein Band). n = Saisons = 4 (Q-127) → geringe Statistik. Monats-KPIs
  sind per-Tag-Interpolierte („geschätzt“-Tage möglich); Sommer-Monate
  mit < 5 HDD-15 (`MIN_MONTH_HDD15`) sind nicht gelistet (degenerierte
  Verhältnisse: bei 0,1 HDD würde jede Standby-Literzahl Hunderte L/HDD
  ergeben). Die laufende Teil-Saison 2026/27 (holz-Share 84,7 %) ist
  pro-rata + kurz — kein Voll-Saison-Wert, driftet mit neuen Daten-Tagen.
- **0,44-h-Deviation:** Die spec-Angabe „785,8 eff. h“ für die Era
  (read-only duckdb-Vergabe) weicht 0,44 h vom modulbasierten Wert
  (786,24 h, [01.02. → 09.11.] inkl., 282 Kalendertage) ab; pro-tagiger
  Abgleich fand keinen eindeutigen Grund (u. a. 22.08.26: 8,4 h roh, aber
  nur 0,35 h eff. — kurze Zündungsrampen). Der **datenbasierte Wert
  786,24 h gilt**.

## 9. Reproduktion

```bash
cd <repo-root>
.venv/bin/python -m analysis.oil_burner_kpi --out tmp/oel_kpi
#   → tmp/oel_kpi/{oel_kpi.json, burn_hours_monthly.csv, report.md}
# Optionen: --start 2020-09-01 --end 2026-11-10 --t-ign 30
#           --tank-csv tmp/tank_zollstock_2015_2026.csv
#           --era-start 2026-02-01 (Neubrenner-Era-Start, Default)
#           --no-oct2025-override (Okt-2025-maintainer-Override deaktivieren)

.venv/bin/python -m pytest tests/test_oil_burner_kpi.py -q -m "not slow"
# Synthetic-Fixtures (kleine 10-s-Parquet + Tank-CSV im pytest-Temp-Dir):
# T_ign-Grenze, Zünd-Rampe, Nachglühen, 2-Stufen-Ausfall-Regel,
# Fenster-Gruppierung, Q-127-Anker-Modell (S1/S2/S3/S3a/S3b, Historie,
# A1/A2-gelöst, Baseline, Sub-Segment-Filter), Aggregationen, Trend-Fit,
# Monats-CSV, End-to-End, v0.6-D KPI (Segment-/Saison-/Monat-Formel,
# Gauge-Band durch die Holz-Korrektur, per-Tag-Monats-Attribution,
# MIN_MONTH_HDD15-Filter, End-to-End v06_kpi-Block + Report-§v0.6).
# Real-Data-Smoke-Tests: -m slow (lädt die volle ~19-Mio-Sample-Datenmenge;
# Q-127-Anker/Baseline/Era/Saisons + v0.6-D-Kernzahlen aus
# oel_kpi.json-Referenz; laufende Teil-Saison 2026/27 driftet mit
# Live-Daten → Intervall-Assertions).
```
