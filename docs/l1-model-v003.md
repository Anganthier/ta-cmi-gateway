# L1-Anlagenmodell v003 (`cmi-brain-v003`): Entscheidungen, Umsetzung, Nutzung

> Vorgänger: `docs/l1-model-v002.md` (Datenstand m1.2). Code: `analysis/model/m2b/`. Artefakt:
> `models/cmi-brain/v003/`. Vollständiger Lauf-Bericht mit allen Targets und Horizonten:
> `analysis/model/m2b/REPORT.md`. Der v002-Bericht ist als `analysis/model/m2b/REPORT_v002.md` archiviert.
> Stand: 2026-11-01, Training 68,5 min (16 Threads).

---

## 0. Kurzfassung (10 Zeilen)

1. **Was:** dasselbe stündliche Anlagenmodell wie v002 (31 Größen, +1/+4/+6/+12 h, Delta-Targets, ein LightGBM je Größe und Horizont), jetzt trainiert auf dem Feature-Store **m1.3**.
2. **Warum neu:** Erstens liefert das CMI-Live-Log seit 2026-10-27 die gemessenen Minderer-Zustände `hzrkl_{z}_on` nicht mehr; v003 nutzt stattdessen die **UVR610-Regel** `hzrkl_{z}_rule`, die sich aus der Raumtemperatur ergibt. Zweitens ist `flue` jetzt in echten °C. Drittens gibt es neue Laufzeit-, Holz- und UVR-Schaltmerkmale.
3. **Ergebnis +4 h:** Puffer Mitte Test 2,763 → **2,555 K** (Refit 2,747 → 2,542), Puffer unten 2,791 → 2,730, Puffer oben 1,505 → 1,535 (Refit 1,383 → 1,339). RAS bleibt auf v002-Niveau (±1 %).
4. **Deutlich besser** als v002 bei den Schaltern Ölbrenner (−12 % Valid), WW-Pumpe über Öl, Zirkulation und O9-Ventil (−17 bis −29 % Test, Refit) sowie beim Warmwasser (−8 % Test).
5. **DoD:** RAS < B0 auf Valid ✅ (alle drei Etagen). Auf Test liegen Keller und EG wie bei v002 über Persistence (Bewertungsmodell +2 %/+3 %, Refit +20 %/+9 %). Puffer ≤ 1,5 K nur oben ✅; Mitte und unten ❌ (der Ölbrenner ist der Engpass, s. §9).
6. **Offset-Responsivität EG:** Pearson **−0,615** (DoD ≤ −0,6 ✅, Vorzeichen 99,5 % korrekt). v002 lag bei −0,725. OG −0,256, Keller −0,110.
7. **Live-fähig:** Die Vorhersage braucht keine gemessenen `hzrkl_*_on`/`_last` mehr. Der Smoke-Test am 2026-10-30 18:00 lief trotz NULL in diesen Spalten.
8. **v002 bleibt nutzbar:** Feature-Namen und Rohspalten werden aus dem Artefakt geladen. Eine Versionssperre verhindert, dass v002 versehentlich gegen m1.3 rechnet.
9. **Gewinner:** LightGBM (mittlerer Skill gegenüber B0 auf Valid 0,324; Keras 0,182). Eingesetzt wird das **Refit** auf 2024+2025 (wie bei v002).
10. **Offen:** Rundenlimit 3000 bei `hzrkl_dg_on` (+1/+6 h), Q-112b (a10–a12 im /LOG), Szenario-Simulation geplanter Schaltungen (v004).

---

## 1. Ziel

v002 hat das L1-Ziel „besser als Persistence, responsiv auf Etagen-Offsets“ erreicht, hat aber zwei Probleme im Betrieb:

- **Live-Lücke:** v002 nutzt die gemessenen Minderer-Zustände `hzrkl_{z}_on`/`_last` und deren Lags als Eingang. Diese Werte sind im CMI-Live-Log seit 2026-10-27 NULL. LightGBM kann zwar mit fehlenden Werten umgehen, rechnet dann aber auf einem Pfad, den es im Training kaum gesehen hat.
- **Puffer Mitte und unten** liegen bei +4 h deutlich über 1,5 K (Test 2,76 / 2,79 K).

v003 soll ohne die gemessenen Minderer-Zustände auskommen, die neuen m1.3-Merkmale nutzen und dabei Genauigkeit und Responsivität von v002 halten.

---

## 2. Datenänderungen m1.3 (Details: `analysis/model/m1/SCHEMA.md` §2.14 und §9, Prüfwerte `analysis/model/m1/REPORT.md` §9)

| Änderung | Bedeutung für v003 |
|---|---|
| `flue`/`flue_last` = echte °C (Rohwert ×10). Das Defektfenster 2022-04-28 10:00 bis 2022-05-06 14:00 und Werte über 500 °C sind NULL. | Das flue-Target hat die 10-fache Skala von v002. In allen Vergleichen sind die flue-Metriken von v002 **×10** gerechnet. |
| `hzrkl_{kell,eg,dg}_rule` (Stundenanteil 0..1), `_rule_last`, `_rule_lag{1,2,6,12,24}` | Minderer-Zustand nach der UVR610-Regel: offen, wenn die perzipierte Temperatur unter 20,1 °C liegt, zu darüber, sonst unverändert. Er stimmt fast exakt mit dem gemessenen Zustand überein (Korrelation 0,999). **Live-Ersatz** für die gemessenen `_on`/`_last`. |
| `o9_switch_margin` = buff_mid − hz_rl − 5 | Abstand zur O9-Schaltschwelle |
| `pump_wood_est_on` | Proxy für die Holzpumpe O1 (wood_vl > 65, wood_vl > buff_bot + 3, buff_bot < 90) |
| `wood_fire_on` | Holzbrand (Abgas > 100 °C) |
| `{sig}_sum{3,6,12,24}h`, `{sig}_since_h` für 9 Signale | Laufzeit der letzten Stunden bzw. Stunden seit der letzten Aktivität (0..48, 49 = in 49 h nichts aktiv) |

Stores: `data/features/` = m1.3 (54.084 Stunden, 692 Spalten). Kopien der alten Stände liegen in `data/features_m1.2/` (für v002) und `data/features_m1.0/` (für v001).

---

## 3. Entscheidungen (mit Begründung und Messwert)

### 3.1 Gemessene Minderer-Zustände raus, Regel rein
- **Entfernt** (21 Eingänge): `hzrkl_{z}_on`, `hzrkl_{z}_last` und die Lags `hzrkl_{z}_on_lag{1,2,6,12,24}`.
- **Neu** (21 Eingänge): `hzrkl_{z}_rule`, `_rule_last`, `_rule_lag{1,2,6,12,24}`.
- **Bleibt:** `hzrkl_{z}_pos` samt `_last` und Lags. Achtung: Das ist trotz des Namens eine **Temperatur am Minderer in °C**, keine Ventilstellung (SCHEMA §2.13). Sie kommt live weiter an.
- `hzrkl_{z}_on` bleibt **Target**. Es ist die DoD-Größe der Responsivität, und für das Training liegen Messwerte bis 2026-10-27 vor.
- Messwert: Der Gain-Anteil von `hzrkl_eg_rule` am Modell `hzrkl_eg_on@4` beträgt 17,8 %, er ist dort das wichtigste Merkmal. Der RMSE von `hzrkl_eg_on@4` bleibt gleich (Valid 0,323 → 0,325, Test 0,400 → 0,404), obwohl der gemessene Zustand als Eingang fehlt.

### 3.2 An-Quotienten der aktuellen Stunde (`oel_burner_on`, `pump_hz_on`, `pump_solar_on`): aufgenommen
M2 hatte diese als „Ergebnis der Stunde“ ausgeschlossen. Bewertung für v003:
- **Kein Leakage im eigentlichen Sinn.** Die Eingangsstunde t ist bei der Vorhersage abgeschlossen, die Targets beginnen bei t+1. Die passenden `*_last`-Werte (Zustand am Stundenende) waren schon in v002 Eingang. Die Laufzeitsummen `{sig}_sum3h` und `_since_h` enthalten die Stunde t ohnehin.
- **Messung** (gleicher Code, Quotienten und `hz_flow_pump` einmal mit, einmal ausgeblendet; Valid / Test):

| Target@h | mit | ohne | Änderung |
|---|---|---|---|
| hzrkl_eg_on@1 (RMSE) | 0,198 / 0,219 | 0,208 / 0,234 | −5,1 % / −6,1 % |
| oel_burner_on@1 (RMSE) | 0,120 / 0,127 | 0,122 / 0,131 | −1,3 % / −3,0 % |
| buff_mid@4 (MAE) | 1,677 / 2,555 | 1,678 / 2,577 | ±0 / −0,8 % |
| buff_bot@4 (MAE) | 1,831 / 2,730 | 1,833 / 2,712 | ±0 / +0,7 % |
| hz_vl@4 (MAE) | 3,04 / 4,48 | 3,11 / 4,47 | −2,4 % / +0,2 % |
| Responsivität EG (Pearson) | −0,6148 | −0,6147 | gleich |

- Der kleine Nutzen ist auf Valid **und** Test gleichgerichtet, es gibt keinen unplausiblen Sprung, und die Offset-Wirkung bleibt unverändert. Der Gain-Anteil der drei Quotienten plus `hz_flow_pump` an buff_mid@4 beträgt zusammen nur 0,5 %. Deshalb sind die Quotienten aufgenommen.

### 3.3 Regler-Sicht (`excluded_features`) und Monotonie
- Für die Regler-Targets (`pump_hz_on`, `hzrkl_{z}_on`) blendet v003 zusätzlich `hzrkl_*_rule_lag*` und alle Laufzeitfenster (`*_sum{w}h`, `*_since_h`) aus. Diese Historie reagierte auf die **alte** perzipierte Temperatur und würde die Wirkung eines Offset-Schritts verwässern; das ist dieselbe Begründung wie bei den RAS-Lags in v002 (§2.5 dort).
- Monotonie für `hzrkl_{z}_on`: zusätzlich `hzrkl_{z}_rule` und `_rule_last` mit **+1** (Regel offen ⇒ Ventil-Anteil steigt), zusätzlich zu Offset und perzipierter Temperatur der eigenen Etage mit −1.

### 3.4 Offset-Schritt rechnet die Regel neu
- `apply_offset_step` verschiebt wie bisher Offset und perzipierte Temperatur. Neu bestimmt es außerdem den Minderer-Zustand der Zone aus `ras_{z}_perc_last + Δ` nach der 20,1-°C-Regel. Bei genau 20,1 °C oder NULL bleibt der bisherige Zustand.
- Ergebnis: `_rule_last` = neuer Zustand; `_rule` (Stundenanteil) wird nur dort auf den neuen Zustand gesetzt, wo er tatsächlich kippt. Δ = 0 ändert also nichts.
- Ohne Regel-Spalten (v002-Rohdaten) bleibt alles wie vorher.

### 3.5 Feature-Ableitung nach Namen und Versionssperre
- `derive(raw, ts, names=…)` liefert genau die verlangten Spalten in dieser Reihenfolge. Ein unbekannter Name führt zu `KeyError`. Abgeleitet wird nur, was gebraucht wird.
- Der Predictor liest `raw_inputs` und `feature_names` aus `features.json` des Artefakts und lädt aus dem Store nur die nötigen Spalten. Deshalb rechnet v002 mit seinen 509 m2b-f2-Namen unverändert weiter.
- **Versionssperre:** Die `data_version` des Stores muss zur `data_version` des Artefakts passen (aus metadata oder summary; fehlt beides, gilt `m1.2`). Andernfalls bricht der Predictor mit einem `ValueError` ab, der auf den richtigen Store verweist, z. B. „v002 → `--features-dir data/features_m1.2`“. Notausgang: `--no-version-check`.

### 3.6 Unverändert von v002 übernommen
Delta-Targets, direkte Horizonte, Splits (Train bis 2024, Valid 2025, Test 2026), Kausal-Modus für Regler-Targets, Rundenlimit 3000 mit Early-Stopping 100, Keras-Referenz, Q-101-Gewinnerregel und das **Refit auf 2024+2025** als Einsatzmodell (abschaltbar mit `--no-refit`).

---

## 4. Neue Merkmale (86 gegenüber v002)

Die Schwellen sind in `docs/uvr-programm-analyse.md` gegengeprüft: §2.2 (Heizkurve, O2-Vergleich), §2.3.1 (AnfWW, Soll 57 °C), §2.3.2 (O1), §2.1/§2.2 (O9). Ein Wert > 0 bedeutet: Die Schaltbedingung ist erfüllt.

| Merkmal | Formel | UVR-Bezug |
|---|---|---|
| `uvr_o9_margin` (+ `_last`) | buff_mid − hz_rl − 5 | O9 Heizungs-Rücklauf in den Puffer (a2 > a7 + 5 K) |
| `uvr_o2_margin` (+ `_last`) | oil_vl − buff_top − 24,6 | O2 Öl lädt Puffer oben (a5 > a1 + 24,6 K) |
| `uvr_ww_margin` (+ `_last`) | 57 − buff_top | WW-Anforderung → Ölbrenner O5 (a1 < 57 °C im Zeitfenster) |
| `uvr_o1_margin` (+ `_last`) | min(wood_vl − 65, wood_vl − buff_bot − 3) | Holzpumpe O1 (a9 > 65 °C und a9 > a3 + 3 K) |
| `vlst_est` | Heizkurve linear durch (+10 °C → 45 °C) und (−20 °C → 65 °C), geklemmt auf 25..70 | u1611FunkHeizkreis: +10 → VL45, −20 → VL65, VLMin 25, VLMax 70. Steilheit 60 und Raumeinfluss 70 % sind **nicht** nachgebildet. |
| `vl_err` | vlst_est − hz_vl | > 0 = Vorlauf liegt unter Soll |
| `buff_strat_top_mid`, `buff_strat_mid_bot` | buff_top − buff_mid, buff_mid − buff_bot | Pufferschichtung |
| `hz_flow_pump`, `hz_flow_o9` | (hz_vl − hz_rl) × pump_hz_on bzw. × valve_hzrl_on | Wärmestrom-Proxy |
| m1.3-Rohspalten | Regel (21), Laufzeitfenster (45), `pump_wood_est_on`, `wood_fire_on`, `o9_switch_margin`, 3 An-Quotienten | s. §2, §3.2 |

Umfang: **507 Rohspalten und 574 Features** (v002: 456 / 509). Ableitungsversion `m2b-f3` (v002: `m2b-f2`).

---

## 5. Ergebnisse

v003 und v002 sind jeweils das Bewertungsmodell (Training bis 2024, Early-Stopping auf Valid 2025). **Refit** ist das Einsatzmodell (Training auf 2024+2025), deshalb ist dort nur Test 2026 aussagekräftig. B0 = Persistence. Analoge Größen: MAE in K bzw. °C; v002-flue ×10.

### 5.1 +4 h (DoD-Horizont), Valid / Test

| Größe | v003 | v003 Refit (Test) | v002 | v002 Refit (Test) | B0 | DoD |
|---|---|---|---|---|---|---|
| RAS Keller Ist | 0,197 / 0,188 | 0,221 | 0,197 / 0,198 | 0,221 | 0,202 / 0,184 | Valid ✅, Test ❌ |
| RAS EG Ist | 0,140 / 0,133 | 0,141 | 0,141 / 0,132 | 0,141 | 0,142 / 0,129 | Valid ✅, Test ❌ |
| RAS OG Ist | 0,204 / 0,257 | 0,241 | 0,204 / 0,254 | 0,242 | 0,309 / 0,377 | ✅ |
| Puffer oben | 1,363 / 1,535 | 1,339 | 1,369 / 1,505 | 1,383 | 2,882 / 2,710 | ✅ (Valid, Refit-Test) |
| Puffer Mitte | 1,677 / 2,555 | 2,542 | 1,688 / 2,763 | 2,747 | 3,268 / 4,053 | ❌ |
| Puffer unten | 1,831 / 2,730 | 2,750 | 1,856 / 2,791 | 2,835 | 3,144 / 4,029 | ❌ |
| Heizkreis-VL `hz_vl` | 3,038 / 4,480 | 4,127 | 3,133 / 4,425 | 4,081 | 3,155 / 4,481 | – |
| Abgas `flue` (°C) | 1,722 / 4,590 | 4,605 | 1,760 / 4,637 | 4,623 | 2,165 / 5,984 | – |

DoD (Puffer ≤ 1,5 K, RAS ≤ B0): **teilweise erfüllt**. RAS auf Valid ✅ für alle drei Etagen. Auf Test ❌ für Keller und EG: das Bewertungsmodell liegt +2 % bzw. +3 % über Persistence, das Refit-Einsatzmodell +20 % bzw. +9 % (identisch zu v002). Puffer nur oben ✅. Keine Explosion (relative Prüfung; die maximale Vorhersage von 311 °C stammt vom Abgas und ist plausibel).

Weitere Änderungen bei +4 h gegenüber v002 (Test, Refit): Warmwasser −8,4 %, hz_rl −1,6 %, Ölbrenner −4,9 %, WW-Pumpe über Öl −28,8 %, Zirkulation −16,6 %, O9-Ventil −21,6 %. Etwas schlechter: Ölkessel-VL +0,7 %, Solar-Kollektor +3,0 %.

### 5.2 Alle Horizonte kompakt (MAE Valid | Test; v003 · v002 · B0)

| Größe | +1 h | +4 h | +6 h | +12 h |
|---|---|---|---|---|
| RAS Keller | 0,059·0,059·0,127 \| 0,060·0,059·0,103 | 0,197·0,197·0,202 \| 0,188·0,198·0,184 | 0,261·0,262·0,265 \| 0,239·0,237·0,228 | 0,355·0,354·0,359 \| 0,334·0,345·0,317 |
| RAS EG | 0,064·0,064·0,100 \| 0,040·0,040·0,059 | 0,140·0,141·0,142 \| 0,133·0,132·0,129 | 0,169·0,169·0,170 \| 0,169·0,169·0,168 | 0,216·0,215·0,219 \| 0,252·0,251·0,251 |
| RAS OG | 0,050·0,050·0,097 \| 0,055·0,055·0,114 | 0,204·0,204·0,309 \| 0,257·0,254·0,377 | 0,267·0,274·0,416 \| 0,353·0,350·0,507 | 0,369·0,372·0,609 \| 0,498·0,494·0,755 |
| Puffer oben | 0,38·0,40·1,21 \| 0,37·0,40·0,96 | 1,36·1,37·2,88 \| 1,54·1,51·2,71 | 1,60·1,62·3,24 \| 1,84·1,87·3,22 | 1,97·1,96·3,69 \| 2,33·2,35·3,93 |
| Puffer Mitte | 0,35·0,36·1,00 \| 0,57·0,62·1,38 | 1,68·1,69·3,27 \| 2,56·2,76·4,05 | 2,22·2,24·4,38 \| 3,27·3,49·5,30 | 2,80·2,81·6,07 \| 4,39·4,48·7,21 |
| Puffer unten | 0,46·0,46·1,08 \| 0,74·0,78·1,51 | 1,83·1,86·3,14 \| 2,73·2,79·4,03 | 2,29·2,32·4,11 \| 3,37·3,45·5,18 | 2,78·2,71·5,57 \| 4,40·4,44·6,88 |
| hz_vl | 1,16·1,17·2,09 \| 1,45·1,47·2,60 | 3,04·3,13·3,16 \| 4,48·4,43·4,48 | 3,64·3,74·3,76 \| 5,03·5,12·5,16 | 2,71·2,75·4,66 \| 4,55·4,57·6,38 |
| flue (°C) | 0,74·0,75·1,05 \| 1,88·1,91·2,68 | 1,72·1,76·2,17 \| 4,59·4,64·5,98 | 1,95·2,00·2,48 \| 5,30·5,39·7,25 | 2,38·2,49·3,15 \| 5,67·5,79·7,14 |

Alle 31 Größen mit Skill und Refit-Werten: `analysis/model/m2b/REPORT.md` §3–§5.

### 5.3 Wichtigste neue Merkmale (Gain-Anteil, Bewertungsmodell +4 h)

| Target | Anteil neue Merkmale gesamt | Top neu |
|---|---|---|
| Puffer Mitte | 20,9 % | `o9_switch_margin` 3,3 %, `buff_strat_mid_bot` 2,2 %, `uvr_o9_margin` 1,5 %, `uvr_o9_margin_last` 1,1 %, `pump_solar_on_sum12h` 1,0 % |
| Puffer unten | 19,8 % | `o9_switch_margin` 2,7 %, `uvr_o9_margin_last` 2,2 %, `buff_strat_mid_bot` 1,0 %, `pump_solar_on_sum12h` 0,9 %, `uvr_o9_margin` 0,8 % |
| Puffer oben | 20,5 % | `uvr_ww_margin` 7,2 %, `buff_strat_top_mid` 1,5 %, `uvr_ww_margin_last` 1,3 %, `pump_ww_on_since_h` 1,0 % |
| hz_vl | 17,2 % | `pump_hz_on_sum24h` 4,1 %, `vl_err` 3,3 %, `oel_burner_on_sum24h` 2,3 % |
| Ölbrenner | 52,0 % | `oel_burner_on_sum24h` 29,2 %, `valve_hzrl_on_sum24h` 13,1 % |
| Minderer EG | 21,3 % | `hzrkl_eg_rule` 17,8 %, `hzrkl_eg_rule_last` 2,2 % |

Bei Puffer Mitte und unten sind die O9-Bedingung und die Schichtung die stärksten neuen Merkmale. Bei Puffer oben ist es die WW-Anforderung (57 °C). Der Ölbrenner wird vor allem über seine Laufzeit der letzten 24 h erklärt.

---

## 6. Offset-Responsivität je Etage

Eingriff: Offset und perzipierte Temperatur der Etage um Δ ∈ {1; 2; 3,4; 5} K anheben; v003 bestimmt dabei zusätzlich den Regel-Zustand neu (§3.4). Wirkungsgröße = Minderer der Etage (`hzrkl_{z}_on`), gemittelt über +1/+4/+6 h, nur Heizsaison-Fälle. Werte: Pearson(Δ, Wirkung) / Anteil Fälle mit Wirkung < 0 bei +3,4 K. DoD: Pearson ≤ −0,6 und Anteil ≥ 0,8, gefordert nur für die EG-Zone.

| Modell (Fälle) | EG v003 | EG v002 | OG v003 | OG v002 | Keller v003 | Keller v002 |
|---|---|---|---|---|---|---|
| LightGBM (Valid) | **−0,615 / 0,995 ✅** | −0,725 / 0,995 ✅ | −0,256 / 0,98 | −0,271 / 0,98 | −0,110 / 0,98 | −0,118 / 0,98 |
| LightGBM (Test) | −0,436 / 1,0 | −0,553 / 1,0 | −0,270 / 1,0 | −0,254 / 1,0 | −0,208 / 0,98 | −0,221 / 0,98 |
| Refit (Test, Einsatzmodell) | −0,290 / 1,0 | −0,345 / 1,0 | −0,224 / 0,995 | −0,212 / 1,0 | −0,198 / 0,98 | −0,203 / 0,98 |
| Keras (Valid) | −0,401 / 1,0 | −0,713 / 0,98 | −0,460 / 0,985 | −0,804 / 0,98 | −0,290 / 0,985 | −0,561 / 1,0 |

- Die **Richtung stimmt fast immer** (98–100 % der Fälle). Die Stärke des linearen Zusammenhangs ist im EG kleiner als bei v002.
- Vermutete Ursache: Der Minderer hängt in v003 vor allem an der Regel (17,8 % Gain). Die Regel kippt aber nur, wenn die perzipierte Temperatur 20,1 °C überschreitet. Liegt eine Etage weit darüber oder darunter, ändern kleine Schritte nichts, größere schließen schlagartig. Das ergibt eine Stufen- statt einer Geraden-Wirkung und senkt den Pearson-Wert, obwohl die Wirkung physikalisch richtig ist.
- Smoke-Test 2026-10-30 18:00 (EG perzipiert 20,36 °C), Szenario `offset_eg +2`: Minderer EG +1 h 0,75 → 0,00, +4 h 0,35 → 0,02; Heizkreispumpe +1 h 0,92 → 0,05; RAS-EG-Ist +4 h −0,07 K; Puffer Mitte +4 h +1,0 K (weniger Wärmeabnahme).

---

## 7. Nutzung

```bash
# Training (Store m1.3, Artefakt models/cmi-brain/v003, REPORT.md; ca. 70 min bei 16 Threads)
.venv/bin/python -m analysis.model.m2b train --threads 16
#   Optionen: --no-refit (Einsatzmodell = Bewertungsmodell), --no-keras, --targets/--horizons, --num-boost

# Vorhersage ab einer Stunde (Default-Modell v003, Default-Store data/features)
.venv/bin/python -m analysis.model.m2b predict --ts "2026-10-30 18:00"

# Szenario: EG-Offset um +2 K (auch --offset-kell / --offset-dg), --json für maschinenlesbar
.venv/bin/python -m analysis.model.m2b predict --ts "2026-10-30 18:00" --offset-eg 2

# Bericht neu schreiben (aus models/cmi-brain/v003/summary.json)
.venv/bin/python -m analysis.model.m2b report
```

Python-API: `L1Predictor.load()` und dann `predict_at(ts, offset_delta={"eg": 2.0})` (s. `analysis/model/m2b/predict.py`).

---

## 8. Rollback

| Ziel | Befehl / Weg |
|---|---|
| **v002** | `.venv/bin/python -m analysis.model.m2b predict --ts "…" --model models/cmi-brain/v002 --features-dir data/features_m1.2` (gleicher Code; die Versionssperre verlangt den m1.2-Store). Ohne `--features-dir` erscheint der klare Fehler „… trainiert auf 'm1.2' → `--features-dir data/features_m1.2`“. |
| **v001** | Code `analysis/model/m2` mit dem Store `data/features_m1.0` (unverändert; die m2-Tests sind grün). |

---

## 9. Grenzen

- **Keine Kenntnis künftiger Zapfungen oder Brennerstarts.** Rund 78 % des Fehlers bei Puffer Mitte fallen in Stunden, in denen der Ölbrenner im 4-h-Fenster aktiv ist. Ob und wann er startet (WW-Anforderung im Zeitfenster, Heizanforderung), weiß das Modell nur statistisch. Deshalb bleiben Puffer Mitte und unten über 1,5 K.
- **Die Holzpumpe ist nur ein Proxy** (`pump_wood_est_on`, aus Temperaturen geschätzt). O1 wird nicht gemessen.
- **Regel statt Messung:** `hzrkl_*_rule` bildet die UVR610-Regel aus den Stundenwerten nach. Die Abweichung vom gemessenen Zustand beträgt im Mittel 0,003–0,005, kann aber in Einzelstunden (Schaltzeitpunkt innerhalb der Stunde) größer sein.
- **Q-112b offen:** Ob die /LOG-Rohdaten die Kanäle a10–a12 (gemessene Minderer-Zustände) enthalten, ist ungeklärt. Bis dahin hat das Target `hzrkl_{z}_on` ab 2026-10-27 keine neuen Messwerte, und die Modelle dafür lassen sich mit neuen Daten nicht nachtrainieren.
- RAS Keller und EG liegen auf Test 2026 wie bei v002 über Persistence (Refit +20 % bzw. +9 %): Die Raumtemperaturen ändern sich in 4 h kaum, und das Refit auf 2024+2025 passt 2026 schlechter.
- Das O9-Ventil (`valve_hzrl_on`) war 2026 fast dauerhaft an, deshalb ist Persistence dort auf Test sehr stark (RMSE 0,095 gegenüber 0,318 beim Modell). Dasselbe Bild zeigte schon v002.
- **Rundenlimit 3000:** `hzrkl_dg_on` +1 h und +6 h erreichen es; `hzrkl_kell_on@1` (2990), `hzrkl_dg_on@12` (2955), `solar_coll@1` (2997) und `hz_rl@1` (2805) liegen knapp darunter.

---

## 10. Nächste Schritte

1. **v004: UVR-Szenario-Simulation geplanter Schaltungen.** Brennerstarts und WW-Ladungen aus dem Zeitprogramm und den UVR-Regeln (Margen aus §4) als *geplante* Aktion über den Horizont einspeisen, statt sie nur aus dem Ist-Zustand zu erraten. Das ist der Hebel für Puffer Mitte und unten.
2. **Persistence-Blending für RAS Keller und EG.** Die Vorhersage bei +4/+6 h mit B0 mischen, wobei der Mischfaktor je Horizont auf Valid bestimmt wird, damit RAS auf Test nicht über Persistence liegt.
3. **Rundenlimit:** für die Minderer-Targets auf 6000 anheben oder die Lernrate leicht erhöhen; danach die Responsivität erneut prüfen.
4. Q-112b klären (a10–a12 im /LOG). Danach entscheiden, ob gemessene Minderer-Zustände wieder Eingang werden.

---

## Anhang: Umsetzung und Prüfungen

- Geändert: `analysis/model/m2b/{config,features,predict,pipeline,report,lgbm,__main__,__init__,data,keras_model}.py`, Tests `analysis/model/m2b/tests/{conftest,test_data_features}.py`, neu `tests/test_v003.py`.
- Tests: alle m2b-Tests grün; Coverage `analysis.model.m2b` (ohne Testdateien) **96 %**; `ruff check analysis/model/m2b` ohne Befund; `analysis/model/m2/tests` 110 passed (unverändert).
- Training 2026-11-01: 4.108,7 s gesamt (LightGBM 2.406 s, Keras 61 s, Refit 1.541 s), 16 Threads, Log `analysis/model/m2b/runs/train_v003.log`.
