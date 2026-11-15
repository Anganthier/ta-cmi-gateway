# L1-Anlagenmodell v002 (`cmi-brain-v002`) — Entscheidungen, Umsetzung, Nutzung

> **Nachfolger v003:** `docs/l1-model-v003.md` (Datenstand m1.3). v002 nutzt weiter den Store `data/features_m1.2/`, weil `flue` dort noch ungeskaliert ist; Nutzung: `--model models/cmi-brain/v002 --features-dir data/features_m1.2`.

> Stand 2026-11-01 08:15 (Nacht-Auftrag maintainer 2026-10-31, ohne HITL umgesetzt; Q-110 nachgezogen). Nachfolger von
> v001 (`analysis/model/m2/`, `docs/l1-model-im-klartext.md`). Ticket: `Plan.md` → **S6-M2b**,
> Protokoll: `CIR-Log.md` → Task „S6-M2b“ + Q-109.
> Messwerte des finalen Laufs: `analysis/model/m2b/REPORT.md` (automatisch erzeugt) und
> `models/cmi-brain/v002/summary.json`. Zahlen in diesem Dokument stammen aus diesen Dateien.

---

## 0. Übergabe in 10 Zeilen (für Menschen **und** nachfolgende KI-Agenten)

1. **Was:** stündliches Anlagenmodell L1, sagt 31 Größen für **+1/+4/+6/+12 h** voraus: Raum, Puffer, Vorlauf, Kessel, Rücklaufminderer je Etage und 12 Schalter-Anteile.
2. **Wie:** je (Größe, Horizont) ein **LightGBM-Modell auf die Änderung** `y(t+h) − y(t)` (Schalter: Anteil 0..1 direkt). Keras v2 = reparierte Referenz.
3. **Daten:** Feature-Store **`m1.2`** (`data/features/`). Backups: `data/features_m1.0/` (für v001) und `data/features_m1.1/`.
4. **Trainieren:** `.venv/bin/python -m analysis.model.m2b train` (ca. 60–90 min auf 16 Threads). Ausgabe `models/cmi-brain/v002/` plus `analysis/model/m2b/REPORT.md`.
5. **Vorhersagen:** `.venv/bin/python -m analysis.model.m2b predict --ts "2026-10-30 18:00" [--offset-kell 2]`.
6. **Tests:** `.venv/bin/python -m pytest analysis/model/m2b/tests analysis/model/m1/tests analysis/model/m2/tests -q`.
7. **v001 bleibt nutzbar:** Code `analysis/model/m2/` und Artefakt `models/cmi-brain/v001/` sind unverändert, nur der Normalisierungs-Bugfix kam hinzu (§2.1). v001 erwartet den m1.0-Store, also `data/features_m1.0/`.
8. **Wichtigste Fallstricke:**
   - dev1 a11–a13 sind **keine Heiz-Minuten**, sondern das Datenalter der RAS-Funkübertragung.
   - RAS-Werte vor dem 2021-10-20 sind ungültig.
   - Persistence ist die harte Messlatte.
9. **Gewinner:** wird per Q-101-Regel automatisch gewählt und steht in `metadata.json:winner`. Q-109 (Review durch maintainer) ist offen.
10. **Offset-Wirkung:** Ein Offset wirkt über den **Rücklaufminderer der eigenen Etage** (Q-110), nicht über die gemeinsame Pumpe. Geprüft wird je Etage, die DoD-Zone ist das **EG** (§2.5). Die Stärke bleibt unsicher, weil Offsets in der Historie selten verstellt wurden (§5).

---

## 1. Ausgangslage: warum v001 schlecht war (Diagnose 2026-10-31)

| # | Befund (gemessen) | Wirkung | Fix in v002 |
|---|---|---|---|
| D1 | NULL-Flags im Train konstant 0 → `std = 1e-4` → Flag = 1 im Test ⇒ z = 10.000 | Keras-Vorhersagen bis 41.710 (9 Teststunden > 150) | §2.1 Scaler-Fix |
| D2 | Absolute Targets, unskaliert (RAS-Level-std 8–9 K, 1-h-Δ-std ≈ 0,17 K; Abgas-std 38 K dominiert den Keras-Stamm) | Keras RAS 1 h 1,1 K vs. Persistence 0,12 K | §2.2 Delta-Targets, §2.6 Keras v2 |
| D3 | RAS-Werte < 5 °C bis 2021-10-20 15:00 | ≈ 14 Monate Müll-Labels | §2.4 m1.1-Maske |
| D4 | `heat_min_*` ≈ 5,9 ganzjährig, Sommer identisch | informationsloses Target und Responsivitäts-Messgröße | §2.4, a11–a13 = Datenalter |
| D5 | 1-h-Modell × 4-h-Rollout, Lags und Wetter eingefroren | RAS-Keller +4 h 0,421 K vs. Persistence 0,196 K | §2.3 direkte Horizonte |
| D6 | DoD-Baseline B2 (0,460 K) schwächer als Persistence | „DoD bestanden“ trügerisch | §2.5 B0 Pflicht |
| D7 | Responsivitäts-Test verschob nur den Offset, perzipiert blieb | widersprüchlicher Zustand | §2.5 Test v2 |
| D8 | LightGBM best_iter 491/497 von 500 | Rundendeckel | 3000 Runden, Early-Stop 100 |

## 2. Umgesetzte Änderungen (P0–P4) mit Begründung

### 2.1 P0 — Bugfix Normalisierung (`analysis/model/m2/normalize.py`, auch v001)
- Konstante Spalten (gespeichert `std ≤ 1e-4`) werden nur zentriert (effektive `std = 1`). Das wirkt auch beim Laden alter `scaler.json`, v001 profitiert also ohne Neu-Training.
- `transform()` kappt z-Werte auf ±10 (`CLIP_Z`); `transform(X, clip=None)` liefert ungekappte Werte.
- `analysis/model/m2/config.py`: `LGBM_NUM_BOOST` 500 → 3000, Early-Stopping 50 → 100.
- **Nachweis** (`python -m analysis.model.m2b.verify_p0`, Ergebnis `analysis/model/m2b/runs/verify_p0.json`), v001-Keras auf dem m1.0-Store:

| Split | max. Vorhersage alt → neu | Zeilen > 150 alt → neu | MAE 1 h RAS-Keller alt → neu | MAE 1 h Puffer oben alt → neu |
|---|---|---|---|---|
| Valid 2025 | 112 → 112 | 0 → 0 | 1,228 → 1,238 | 2,756 → 2,792 |
| Test 2026 | **41.710 → 106** | **9 → 0** | **11,74 → 1,94** | **39,8 → 2,74** |

v001-Keras bleibt auch nach dem Fix schwach (D2). Das ist ein Problem der Modellformulierung, kein Bug.

### 2.2 P1 — Delta-Targets
- Analoge Größen werden als Änderung `y(t+h) − y(t)` gelernt und für die Ausgabe wieder auf den aktuellen Wert addiert. So beginnt jedes Modell bei Persistence und lernt nur noch, wie stark sich der Wert ändert.
- Schalter-Anteile (`*_on`, 0..1) werden als Level mit `cross_entropy` gelernt. Die Ausgabe ist der Erwartungswert des An-Anteils. Bewertet werden sie mit **RMSE** (Brier-artig), denn MAE belohnt 0/1-Mediane und lässt Persistence systematisch besser aussehen (`evaluate.py`).
- RAS-Offsets sind in v002 **kein Target** mehr. Sie sind die Aktion und gelten über den Horizont als gehalten.

### 2.3 P1 — Direkte Horizonte statt Rollout
- Für h ∈ {1, 4, 6, 12} gibt es je ein eigenes Modell. Fehler summieren sich nicht, und Lags und Wetter müssen nicht künstlich fortgeschrieben werden.
- Das Wetter der nächsten h Stunden geht als Aggregate ein (`wxa_*_{4,6,12,24}`).
- Semantik: „Zustand jetzt, Aktion jetzt gesetzt und gehalten → Zustand in h Stunden“. Genau diese Frage stellt L2.
- Kosten: 124 Modelle (31 Größen × 4 Horizonte) statt 31, aber jedes einzelne ist klein (Text-Dateien in `models/cmi-brain/v002/lgbm/`).

### 2.4 P2 — Datenkorrekturen M1 (`m1.1`, `analysis/model/m1/SCHEMA.md` §7)
- **RAS-Maske:** Alle RAS-Rohkanäle (dev1 a1/a2/a4/a5/a7/a8/a11–a13, dev2-RAS) vor `RAS_INSTALL_TS = 2021-10-20 17:00` sind NULL. Lags und `_last` folgen automatisch.
  - Nachweis: 0 von 9.977 Stunden davor haben einen RAS-Wert, 0 RAS-Ist-Werte danach liegen unter 5 °C.
- **a11–a13 = Datenalter** (maintainer 2026-10-31): Sägezahn mit +0,1 je Minute seit der letzten Funkübertragung, Reset bei ≈ 1,1 (≈ 11 min).
  - Die neuen Spalten `ras_age_kell/eg/dg` enthalten das Stunden-MAX.
  - Nach dem Einbau: P50 ≈ 1,1, P99 Keller 6,3 / EG 2,4 / DG 250; Sättigung bei 250.
  - `heat_min_*` bleibt nur als **DEPRECATED**-Spalte für v001.
- **Eingefrorene RAS-Werte:** Ist `ras_age > 3,0` (≈ 30 min ohne Funk), gilt der Raumwert der Stunde als veraltet. Solche Stunden werden für RAS-Targets maskiert, und zwar sowohl in Stunde t als auch in t+h (`config.RAS_STALE_AGE`). Das betrifft ≈ 1–3 % der Stunden je Zone.
- **`*_last`:** letzter 10-s-Wert der Stunde für 17 analoge Größen. Das Stundenmittel hinkt im Schnitt 30 min hinterher, `_last` ist der aktuellste Zustand. Corr(`ras_kell_ist`, `_last`) = 0,998.
- Store-Rebuild dauert 15 s (≈ 10 GB RAM-Spitze). Das Wetter kam nur aus dem Cache (`data/features/wx_cache`, Prüfsumme unverändert).

**Nachtrag `m1.2` (2026-11-01, nach Q-110):**
- **TA-Fehlerwerte maskiert:** Werte `≥ 3276,0` oder `≤ −999,0` (Fühlerbruch/Kurzschluss) gelten in allen analogen Rohkanälen als NULL.
  - Betroffen: Abgas (dev0 a10) mit 9.383 × 3276,7 und 2.190 × −999,9 zwischen Dezember 2021 und Mai 2022, Kollektor (dev2 a16) mit 556 × 3276,7 am 2022-03-25.
  - Folge: max. Abgas 624 → 163 °C, max. Kollektor 3277 → 180 °C.
  - Genau diese Werte haben die „Explosion“ 3277 im ersten v002-Lauf verursacht, das Modell selbst war nicht schuld.
- **Rücklaufminderer je Etage** (UVR610 dev2, `docs/plant-model.md`), Daten ab 2022-03-06:
  - `hzrkl_{kell,eg,dg}_on` = „HzRkl Ventil 2 Ergebnis“ (a10/a11/a12, binär 0/10) als An-Anteil 0..1.
  - `hzrkl_{z}_pos` = „HzRkl Ventil 1“ (a13 Keller, a15 EG, a14 OG).
  - Zu beiden gibt es `_last` und Lags 1/2/6/12/24 h.
  - Heizsaison 2024/25, Ventil offen: Keller 22 %, EG 90 %, OG 77 %. Korrelation mit der perzipierten Temperatur der eigenen Etage: −0,64 / −0,49 / −0,56.
  - Das passt zu deinen Angaben: Keller mit kaum Heizbedarf, EG mit der größten Heizlast, OG kühlt schnell aus und wird aber wenig beheizt.
- **Klarstellung 2026-11-01 (Q-112 gelöst):** `hzrkl_*_pos` (a13/a15/a14) ist eine **Temperatur am Minderer (°C)** — Beleg: CMI-JSON-API (live-Endpunkt json:32 ch13–15) Einheit °C, Werte 18–61, Korrelation mit `hz_vl` **positiv** (+0,81 / +0,93 / +0,88). Spaltennamen `_pos` bleiben aus Kompatibilitätsgründen. Name „pos“ ist irreführend; die Größe läuft in v002 als physikalisches Target (°C) weiter, ohne Monotonie und ohne Regler-Sicht.
- **Live-Status (Korrektur 2026-11-01):** `cmi_log_pq` enthält dev2 a10–a12 nicht; unser Decoder `app/cmi/cmi_log.py` ordnet bewusst nur einen festen Satz von 36 Analogkanälen zu. Ob die Roh-/LOG-Daten a10–a12 enthalten, ist **offen** (passende Rohpositionen zeigen Werte um 21, 20–22 und 42–49, nicht 0/10). Live verfügbar über die JSON-API (`history.db`, json:32 ch10–12, seit 27.10.). Zusätzlich gilt die exakte Regel (offen < 20,1 °C perzipiert, zu > 20,1 °C, bei Gleichstand halten; Korrelation 0,999); m1.3 leitet `hzrkl_{z}_rule` ab → Q-112b ist nicht dringend.

### 2.5 P3 — Kausalität und Responsivität
- **Neue Merkmale aus dem Regler** (`docs/uvr-programm-analyse.md` §2.2, UVR1611 Heizkreis-Modul):
  - RT-Eingang = MIN der perzipierten RAS → `ras_perc_min`, `ras_perc_min_last`.
  - Zeitprogramm 06:00–22:30, Norm 20 °C, Absenk 16 °C → `rt_soll`.
  - Regelabweichung → `rt_err = rt_soll − ras_perc_min` (> 0 = zu kalt).
  - Ein Offset-Schritt in der kältesten Zone verschiebt `rt_err` direkt.
- **Regler-Sicht für die Heizkreis-Pumpe** (`config.CONTROLLER_TARGETS`, `excluded_features`):
  - Das Modell für `pump_hz_on` sieht keine Ist-Temperaturen und keine RAS-/Offset-Lags, weil der Regler nur die perzipierte Temperatur kennt.
  - Ausgeblendete Merkmale werden als „fehlt“ (NaN) übergeben, das gilt auch in `predict`.
  - Experiment: Pearson −0,34 → −0,50 bei gleicher Genauigkeit (§4.3, gemessen im Kausal-Modus).
- **Kausal-Modus für die Heizkreis-Pumpe** (`config.CONTROLLER_CAUSAL = True`):
  - Für `pump_hz_on` wird die monotone Variante immer genommen, und das Early-Stopping läuft auf L1 statt L2.
  - Grund: Mit L2-Stopp endet das Training nach 25–33 Runden (+4/+6 h). Das Modell liegt dann nahe am Mittelwert und reagiert kaum auf den Offset (Pearson −0,09 bis −0,13).
  - Mit L1-Stopp sind es 174–800 Runden, Pearson −0,46, Vorzeichen in 98 % der Fälle korrekt.
  - Preis: RMSE +4 h 0,346 → 0,373, Persistence liegt bei 0,363. Der Pumpen-Anteil bei +4 h ist damit minimal schlechter als „bleibt wie es ist“.
  - Abwägung: L2 braucht die *Wirkung einer Aktion*, nicht den exakten Pumpen-Anteil. Die DoD-Ziele (Raum, Puffer) sind davon nicht betroffen.
  - Zurückstellen: `CONTROLLER_CAUSAL = False` (genauigkeitsoptimal), dann neu trainieren.
- **Monotonie** (`config.monotone_spec`, LightGBM `monotone_constraints`):
  - `pump_hz_on` ist fallend in Offsets, perzipierter Temperatur und `ras_perc_min` und steigend in `rt_err`.
  - RAS-Targets sind fallend im eigenen Offset.
  - Die monotone Variante wird genommen, wenn ihre Valid-Fehlerkennzahl höchstens 2 % schlechter ist als die der freien Variante (analog MAE, Schalter-Anteile RMSE). Ausnahme: Regler-Targets im Kausal-Modus, dort ist die monotone Variante immer gesetzt.
  - Technischer Hinweis: LightGBM erlaubt Monotonie nicht mit `regression_l1`. Monotone analoge Varianten nutzen deshalb `huber` (alpha = 1,5·std).
- **Rücklaufminderer als Regler-Targets** (nach Q-110): `hzrkl_{z}_on` bekommt dieselbe Behandlung wie die Pumpe.
  - Regler-Sicht: keine Ist-Temperatur, keine Lag-Historie.
  - Kausal-Modus.
  - Monotonie: Offset bzw. perzipierte Temperatur *der eigenen Etage* steigt ⇒ Ventil geht zu.
- **Responsivitäts-Test v2** (`responsivity.py`, Stand nach Q-110):
  - **Je Etage** (EG, OG, Keller) 200 Heizsaison-Zustände aus Valid 2025 (Außen-T < 12 °C, RAS frisch).
  - Offset **und** perzipierte Temperatur der Etage werden gemeinsam um Δ ∈ {1; 2; 3,4; 5} K verschoben, Ist und Lags bleiben.
  - Wirkung = mittlere Änderung der Vorhersage über +1/+4/+6 h.
    - Prüfgröße: **Minderer der Etage** `hzrkl_{z}_on`.
    - Informativ berichtet: Minderer-Wert „Ventil 1“, Pumpe d8 und RAS-Ist +6 h.
  - **DoD (Zone EG):** Pearson(Δ, Wirkung) ≤ −0,6 **und** Wirkung < 0 in ≥ 80 % bei +3,4 K.
  - Warum das EG: Laut maintainer hat es die größte Heizlast. Ein Keller-Offset konnte über die Pumpe nur wirken, wenn der Keller der kälteste Raum war, das traf in ≈ 22 % der Fälle zu (§4.3).
  - Zusätzlich läuft der Test auf Test 2026 für das Refit-Modell (`lgbm_refit@test`), weil das Refit die Valid-Daten bereits kennt.

### 2.6 P4 — Keras v2 und Gewinner
- **Keras v2** (`keras_model.py`):
  - Ein Netz mit 124 Ausgängen (31 Targets × 4 Horizonte) und den Schichten 256-128.
  - NaN-Eingänge werden mit dem Train-Median ersetzt, dazu kommt ein NULL-Indikator, aber nur für Spalten, die im Train tatsächlich NaN enthalten.
  - Ziele werden **je Ausgang z-standardisiert**; der maskierte Huber-Loss ignoriert NaN-Targets.
  - Läuft auf CPU, deterministisch mit Seed 42.
- **Gewinner nach Q-101:**
  - Besteht ein Modell die Responsivitäts-DoD, hat es Vorrang.
  - Sonst entscheidet der höchste mittlere Skill gegenüber B0 auf Valid über alle analogen Targets × Horizonte.
  - „Keras-treu“ aus dem Design wird damit nicht verletzt: Keras bleibt als vollwertige Referenz erhalten.
- **Deploy-Refit:**
  - LightGBM wird nach der Bewertung mit `best_iter × 1,1` Runden auf Train + Valid (bis 2025-12-31) neu trainiert. So fließen die jüngsten Daten ein.
  - Test 2026 bleibt unangetastet und wird für die Refit-Modelle getrennt berichtet (`lgbm_refit`).

## 3. Merkmale (509) — Übersicht

| Gruppe | Beispiele | Herkunft |
|---|---|---|
| M2-Basis ohne `heat_min_*`/`mode_known` | Zustand + Lags 1/2/6/12/24 h, Schalter, Kalender, Wetter 24 h | `m2.config.FEATURE_INPUTS` |
| `*_last` (17) | `ras_kell_ist_last`, `buff_top_last`, … | m1.1 |
| RAS-Datenalter (3) | `ras_age_kell/eg/dg` | m1.1 |
| Rücklaufminderer (42) | `hzrkl_{z}_on/_pos`, `_last`, Lags 1–24 h | m1.2 |
| Extra (8) | `pump_hz_last`, `sunrise_min`, `wx_is_forecast`, `gap_partial` | m1 |
| Abgeleitet (≈ 53) | `ras_perc_min`, `rt_err`, `*_d1`, `*_trend`, `wxa_*`, `doy_sin/cos`, `hour_sin/cos` | `m2b/features.py` (`DERIVE_VERSION = m2b-f2`) |

Die vollständige Liste steht in `models/cmi-brain/v002/features.json`. Merkmale entstehen **nur** in `m2b/features.derive()`; Training, Vorhersage und Szenarien nutzen dieselbe Funktion.

## 4. Ergebnisse und Experimente

### 4.1 Finaler Lauf
→ `analysis/model/m2b/REPORT.md` (DoD-Tabelle, alle Targets × Horizonte, Responsivität, Monotonie, Feature-Wichtigkeit). Die Zusammenfassung steht in §4.5, sie wird nach dem Lauf ergänzt.

### 4.2 Gegenexperiment der Diagnose (vor v002, MAE +4 h Valid/Test)
| Ziel | Persistence | v001-LGBM (Rollout) | Delta direkt |
|---|---|---|---|
| RAS Keller | 0,196 / 0,182 | 0,421 / 0,717 | 0,194 / 0,184 |
| Puffer oben | 2,88 / 2,71 | 2,27 / 2,08 | 1,42 / 1,60 |

### 4.3 Responsivität — Varianten (Pumpe +1/+4/+6 h, Keller-Offset, 200 Valid-Fälle, 2026-11-01, **vor** Q-110)

Diese Experimente stammen aus der Zeit vor Q-110. Gemessen wurde über die gemeinsame Pumpe bei einem Keller-Offset. Sie begründen den Kausal-Modus und die Regler-Sicht; die DoD-Messung läuft inzwischen über den Etagen-Minderer im EG (§2.5).

**a) Merkmale** (Early-Stopping L1):

| Variante | Pearson(Δ, ΔD) | Anteil ΔD < 0 bei +3,4 K | ΔD̄ bei +3,4 K |
|---|---|---|---|
| alle Merkmale, monoton | −0,34 | 0,98 | −0,026 |
| ohne Ist-/Lag-Merkmale, monoton | −0,50 | 0,98 | −0,035 |
| + `rt_err`, monoton (= v002 Kausal-Modus) | −0,46 | 0,98 | −0,037 |
| + `rt_err`, ohne Monotonie | −0,39 | 0,84 | −0,027 |
| nur Fälle „Keller = kältester Raum“ (44) | −0,35 | 1,00 | −0,070 |

**b) Early-Stopping-Metrik × Monotonie** (Merkmalsstand v002; RMSE Valid je h, Runden in Klammern; Persistence-RMSE +4 h 0,363):

| Stopp | monoton | RMSE +1 / +4 / +6 h | Pearson | Anteil ΔD < 0 |
|---|---|---|---|---|
| L1 | ja (**v002**) | 0,240 (800) / 0,373 (250) / 0,392 (223) | **−0,46** | **0,98** |
| L1 | nein | 0,238 / 0,364 / 0,393 | −0,39 | 0,84 |
| L2 | ja | 0,236 (160) / 0,354 (26) / 0,378 (25) | −0,09 | 0,93 |
| L2 | nein (erster Gesamtlauf 06:00) | 0,233 / 0,346 / 0,369 | −0,13 | 0,85 |

Trainingsbeginn ab 2021-10-20 statt alle Daten (L2, monoton): Pearson −0,06, RMSE +4 h 0,373, also schlechter in beidem (§4.4).

Deutung:
- Die **Richtung** ist robust gelernt (≥ 98 %).
- Die **Stärke** ist klein (−0,04 An-Anteil ≈ −2,4 min Pumpenlauf je Stunde bei +3,4 K), und sie streut zwischen den Fällen stark. Die gepoolte Pearson-Korrelation bleibt deshalb unter der Grenze.
- Ursachen:
  - In der Historie wurde der Offset Keller nur in 277 von 37.425 Stunden um ≥ 0,5 K verstellt.
  - Der Keller ist nur in ≈ 22 % der Heizsaison-Fälle der kälteste Raum. Ein Keller-Offset wirkt über das MIN des Reglers aber nur dann.
- **Folgerung:** Aus der Historie allein ist das nicht zu lösen. Nötig sind gezielte Offset-Schritte als Experiment, mit Logging über Q-102 (Parameter-Logging), danach ein Retrain (§7).

### 4.4 Trainingsbeginn — Experiment (5 Targets, +4/+6 h)
| Trainingsbeginn | pump_hz_on +4 h RMSE (Valid) | B0 | Puffer oben +4 h MAE | RAS |
|---|---|---|---|---|
| alle Daten (2020-08) | **0,346** | 0,363 | 1,375 | identisch |
| ab 2021-10-20 17:00 (Regimewechsel) | 0,366 | 0,363 | 1,364 | identisch |

Entscheidung: alle Daten (`config.TRAIN_START = None`). Die Option bleibt per CLI erhalten (`--train-start`).

### 4.4b Hyperparameter — Vergleich (7 Leit-Targets, Valid / Test, 2026-11-01)
| Satz | Änderung gegenüber `config.lgbm_params` | RAS-Keller +4 h | RAS-DG +4 h | Puffer oben +4 h | Pumpe +4 h (RMSE, L2-Stopp) |
|---|---|---|---|---|---|
| **A (gewählt)** | lr 0,05 · 31 Blätter · min_child 50 · λ2 1 · ff 0,5 | 0,193 / 0,193 | 0,203 / 0,256 | **1,375 / 1,519** | **0,346** / 0,371 |
| B regularisiert | lr 0,02 · min_child 200 · λ2 10 · ff 0,3 | 0,195 / 0,184 | 0,202 / 0,256 | 1,391 / 1,510 | 0,355 / 0,374 |
| C B + extra_trees | wie B + `extra_trees` | 0,197 / 0,183 | 0,205 / 0,263 | 1,463 / 1,585 | 0,359 / 0,374 |
| D klein | lr 0,03 · 15 Blätter · min_child 300 · λ2 5 · ff 0,3 | 0,197 / 0,184 | 0,204 / 0,256 | 1,416 / 1,526 | 0,361 / 0,375 |

Persistence (B0) Valid / Test: RAS-Keller 0,202 / 0,184 · RAS-DG 0,309 / 0,377 · Puffer oben 2,882 / 2,708. Satz A ist auf Valid durchgehend am besten oder gleichauf. B und D sind im Test bei RAS-Keller etwas besser, das ist aber nicht für die Auswahl verwendbar, weil Test 2026 nur berichtet wird. Die Unterschiede liegen alle bei ≤ 0,01 K (RAS) bzw. ≤ 6 % (Puffer). Weitere Hyperparameter-Suche bringt hier also wenig, der Engpass ist die Information in den Daten.

### 4.5 Kurzfassung finaler Lauf (2026-11-01 08:06, Daten `m1.2`, 509 Merkmale, 124 Modelle, Laufzeit 62 min)

**Gewinner: LightGBM.** Mittlerer Skill gegenüber Persistence auf Valid: 0,318; Keras v2: 0,179.

**MAE +4 h, Valid 2025 / Test 2026** (Test: Refit-Modell = Einsatzmodell):

| Größe | v002 | Persistence B0 | v001 (Rollout) | DoD |
|---|---|---|---|---|
| RAS Keller | 0,197 / 0,221 | 0,202 / 0,184 | 0,421 / 0,717 | ✅ (knapp) |
| RAS EG | 0,141 / 0,141 | 0,142 / 0,129 | 0,194 / 0,444 | ✅ (knapp) |
| RAS OG | 0,204 / 0,242 | 0,309 / 0,377 | 0,270 / 0,545 | ✅ |
| Puffer oben | 1,369 / 1,383 | 2,882 / 2,711 | 2,27 / 2,08 | ✅ < 1,5 K |
| Puffer Mitte | 1,688 / 2,747 | 3,268 / 4,055 | 2,05 / 3,38 | ❌ (Ziel 1,5) |
| Puffer unten | 1,856 / 2,835 | 3,144 / 4,029 | – | ❌ (Ziel 1,5) |

- **Keine Explosionen:** Die höchste Vorhersage liegt bei 179,6 (Kollektor, real bis 180 °C).
- **Offset-Responsivität** (Wirkung über den Minderer der Etage, Pearson / Anteil mit richtigem Vorzeichen bei +3,4 K):

| Modell (Fälle) | EG (DoD) | OG | Keller |
|---|---|---|---|
| LightGBM (Valid) | **−0,73 / 99,5 %** ✅ | −0,27 / 98 % | −0,12 / 98 % |
| Keras v2 (Valid) | −0,71 / 98 % ✅ | −0,80 / 98 % | −0,56 / 100 % |
| LightGBM-Bewertungsmodell (Test 2026) | −0,55 / 100 % | −0,25 / 100 % | −0,22 / 98 % |
| LightGBM-Refit = Einsatzmodell (Test 2026) | −0,34 / 100 % | −0,21 / 100 % | −0,20 / 98 % |

**Deutung und Entscheidungen:**
- **Die Ursache der Offset-Probleme ist gefunden (Q-110).** Ein Etagen-Offset wirkt über den Rücklaufminderer der Etage. Über die gemeinsame Pumpe kam die Korrelation nie unter −0,46, über den EG-Minderer liegt sie bei −0,73. Im OG und im Keller ist der Minderer seltener im Regelbereich (Keller zu 78 % zu), deshalb fällt die Korrelation dort kleiner aus. Das Vorzeichen stimmt überall in ≥ 98 % der Fälle.
- **Refit als Einsatzmodell:**
  - Auf Test 2026 im Mittel um 3,4 % genauer (Skill 0,134 → 0,154), deutlich bei Puffer oben (1,505 → 1,383), Pumpe (0,391 → 0,345) und Minderer EG (0,400 → 0,368).
  - Die *Stärke* der Offset-Wirkung auf 2026 ist dafür kleiner (EG −0,55 → −0,34), das Vorzeichen stimmt weiterhin zu 100 %.
  - Ich habe mich für das Refit entschieden. Wer die stärkere Kausalität will, trainiert mit `--no-refit` (Bewertungsmodell = Einsatzmodell).
- **RAS Keller/EG auf 2026 schlechter als Persistence** (0,221 vs. 0,184 bzw. 0,141 vs. 0,129):
  - Beide Räume ändern sich über 4 h kaum, deshalb ist Persistence dort schwer zu schlagen.
  - Auf Valid 2025 gewinnt v002 knapp, auf Test 2026 nicht.
  - Für L2 heißt das: Für Keller und EG zählt die *Richtung* der Offset-Wirkung, nicht der absolute Wert.
  - Möglicher nächster Schritt ist ein Mischen mit Persistence (`ŷ = y(t) + α·Δ̂`, α auf Valid gefittet), siehe §7.
- **Keras v2** ist ohne Explosion und in der Offset-Wirkung über alle Etagen am stärksten. Bei der Genauigkeit liegt es klar unter LightGBM (RAS EG +4 h 0,162 vs. 0,141, Puffer oben 2,05 vs. 1,37). Es bleibt Referenz und lässt sich per `--backend keras` laden.
- `hzrkl_dg_on` erreichte das Rundenlimit (2994–3000 von 3000). Ein höheres Limit würde dort noch etwas bringen (§7).

## 5. Grenzen und bekannte Schwächen
- **RAS ab +4 h nahe Persistence:** Raumtemperaturen ändern sich über 4–12 h kaum vorhersagbar, der Skill gegenüber B0 ist klein. Die DoD „< B0“ ist trotzdem streng formuliert.
- **Offset-Wirkung:**
  - Im EG ist sie belastbar gelernt (§4.5), im OG und Keller nur schwach.
  - Die Wirkungs*stärke* bleibt unsicher, weil Offsets in der Historie selten verstellt wurden.
  - L2 sollte Offsets im OG und Keller vorerst nur als *Richtungshinweis* nutzen, mit Konfidenzhinweis.
- **Live-Inferenz:** `cmi_log_pq` enthält dev2 a10–a12 nicht (Decoder ordnet bewusst nur
  36 Analogkanäle zu; ob die Roh-/LOG-Daten sie enthalten, ist offen, Q-112b).
  Live stehen die Minderer über die JSON-API (`history.db`, json:32 ch10–12, seit 27.10.)
  und die exakte Regel (`hzrkl_{z}_rule`, 20,1 °C) zur Verfügung.
- **Betriebsmodus und Vorlauf-Soll** fehlen in der Historie (Q-102). Handbetrieb-Phasen sehen für das Modell wie Regelverhalten aus.
- **DST:** Das Raster ist an der Wanduhr ausgerichtet (M1-SCHEMA §3). An Zeitumstellungstagen ist ein Rasterschritt ≠ 60 min, wie in M2 akzeptiert.
- **Live-Store:** Das CMI-Log läuft weiter. Ein M1-Rebuild verändert die letzten Stunden, die Metriken verschieben sich dann minimal.

## 6. Nutzung

### 6.1 Python-API
```python
from analysis.model.m2b.predict import L1Predictor, format_table
p = L1Predictor.load()                                   # v002, Backend = metadata.json:winner
res = p.predict_at("2026-10-30 18:00")                   # {0: jetzt, 1: +1 h, 4: …, 6: …, 12: …}
print(format_table(res))
scen = p.predict_at("2026-10-30 18:00", offset_delta={"eg": 2.0})     # Szenario Offset EG +2 K
k = L1Predictor.load(backend="keras")                    # Referenz-Backend
```
`predict_raw(raw, ts)` nimmt eigene Rohspalten entgegen (Dict Spalte → Array), z. B. für L2-Kandidatenaktionen. Benötigte Spalten: `config.RAW_INPUTS` + `config.TARGETS`.

### 6.2 CLI
```bash
.venv/bin/python -m analysis.model.m2b train                     # alles (LGBM + Keras + Refit + Report)
.venv/bin/python -m analysis.model.m2b train --no-keras --targets buff_top,ras_kell_ist --horizons 4 --out /tmp/x
.venv/bin/python -m analysis.model.m2b predict --ts "2026-10-30 18:00" --offset-eg 3.4
.venv/bin/python -m analysis.model.m2b train --no-refit                  # Einsatzmodell = Bewertungsmodell (stärkere Kausalität, §4.5)
.venv/bin/python -m analysis.model.m2b report                    # REPORT.md aus summary.json neu
.venv/bin/python -m analysis.model.m2b.verify_p0                 # P0-Nachweis v001
```

### 6.3 v001 weiter nutzen
- Code: `analysis/model/m2/` (unverändert bis auf §2.1). Artefakt: `models/cmi-brain/v001/`.
- v001 wurde auf m1.0 trainiert. Zum Reproduzieren oder Bewerten den Store `data/features_m1.0/` übergeben (`build_transitions(features_dir=...)`). `m2/config.DATA_VERSION` bleibt `"m1.0"`.
- Der m1.1-Store ist für v001 nur bedingt geeignet: RAS vor 2021-10 ist dort NULL statt 0, und `heat_min_*` vor dem Einbau ebenfalls NULL.

### 6.4 Artefakt-Layout `models/cmi-brain/v002/`
```
metadata.json    Gewinner, DoD, Responsivität, Konfiguration, Monotonie je Modell
summary.json     zusätzlich alle Metriken (Target × Horizont × Split) + Feature-Wichtigkeit
features.json    derive_version, Roh-Inputs, Feature-Reihenfolge (muss zu derive() passen)
lgbm/<target>__h<h>.txt   124 LightGBM-Modelle (Deploy = Refit Train+Valid)
keras/model.keras, prep.json, history.json   Keras v2 (Referenz)
```

## 7. Nächste Schritte (Vorschlag, nicht umgesetzt; erledigte Punkte als „in m1.3/v003 umgesetzt“ markiert)
1. **Offset-Experimente** (HITL/S3): gezielte Offset-Schritte ±2–3 K in Heizsaison-Abenden, mit Parameter-Logging (Q-102). Danach Retrain, Ziel Pearson ≤ −0,6.
2. **M3-Backtest** auf v002-Basis über 12 h (Komfort-Band, Öl-Proxy).
3. **Quantil-Modelle** (P10/P90) für L2-Konfidenz (LightGBM `objective=quantile`).
4. `m2/config.DATA_VERSION`/v001-Neutraining auf m1.1 nur auf Wunsch.
5. **RAS Keller/EG mit Persistence mischen:** α je Zone und Horizont auf Valid fitten, damit das Modell auf Test mindestens so gut ist wie B0 (§4.5).
6. **Rundenlimit erhöhen** für `hzrkl_*_on` (OG am Limit 3000), z. B. auf 6000.
7. **Klären (Status 2026-11-01):** Ob die Roh-/LOG-Daten a10–a12 enthalten, ist offen (Q-112b, nicht dringend). **Minderer-Regel (`hzrkl_{z}_rule`), Abgas ×10 (Q-113) und Laufzeitsummen (`_sum{3,6,12,24}h`, `_since_h`) — in m1.3/v003 umgesetzt.**
8. Abgas (`flue`) Kurzschlussfenster 2022-04-28 10:00 bis 2022-05-06 14:00 — Fenster-Maske und 500-°C-Grenze in m1.3/v003 umgesetzt.

## 8. Dateien (Map für Agenten)
| Datei | Inhalt |
|---|---|
| `analysis/model/m2b/config.py` | Targets, Horizonte, Inputs, Regler-Konstanten, Ausblendung, Monotonie, Hyperparameter, DoD |
| `analysis/model/m2b/data.py` | Store laden, Splits (+ 'pre'), Target-Masken (Gap/Stale/Purge) |
| `analysis/model/m2b/features.py` | `derive()`, `rt_soll()`, `apply_offset_step()` |
| `analysis/model/m2b/lgbm.py` | Fit/Refit/Predict, Ausblendung, Monotonie (Huber-Workaround) |
| `analysis/model/m2b/keras_model.py` | Keras v2 |
| `analysis/model/m2b/evaluate.py` | MAE/RMSE, B0/B0-last/B2, Skill, DoD |
| `analysis/model/m2b/responsivity.py` | Responsivitäts-Test v2 (je Etage, Prüfgröße Minderer, DoD-Zone EG) |
| `analysis/model/m2b/pipeline.py` | Komplett-Lauf, Gewinner, Artefakt |
| `analysis/model/m2b/predict.py` | `L1Predictor` |
| `analysis/model/m2b/report.py` | REPORT.md |
| `analysis/model/m2b/verify_p0.py` | P0-Nachweis v001 |
| `analysis/model/m2b/tests/` | synthetischer m1.2-Store, 44 Unit- + E2E-Tests |
| `analysis/model/m1/` (m1.2) | RAS-Maske, `ras_age_*`, `*_last` (SCHEMA §7), TA-Fehlerwert-Maske, `hzrkl_*` (SCHEMA §2.13, §8) |
| `analysis/model/m2/normalize.py` | Scaler-Fix (P0) |
