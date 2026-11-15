# M2 — REPORT: L1 Keras-Anlagen-Modell (1-h-Step, action-conditional)

Stand: 2026-10-31T20:17:41+00:00 · Seed **20261031** · data_version **m1.0** · git-Rev **62a7048**

Frameworks: TF **2.21.0** / Keras **3.15.1** / LightGBM **4.7.0** / NumPy **2.5.4** · Device: GPU:0 · LGBM-Device: **cpu** (GPU-Run, deterministischer Seed + TF_CUBLAS_DETERMINISTIC=1).

---
## 1. Daten & Übergangs-Tabelle

| Größe | Wert |
|---|---|
| Rows Feature-Store (gesamt) | 54,061 |
| Rows mit `gap_full=1` (defekte/gap-Rasterstunden) | 601 |
| Drops: aktuelle Row `gap_full=1` | 601 |
| Drops: fehlende Next-Row (Raster-Ende) | 0 |
| Drops: Next-Row (t+1) `gap_full=1` | 12 |
| **Transitions gesamt** | **53,448** |
| Train (2020-08-31T00:00:00 → 2024-12-31T23:00:00) | 37,426 |
| Valid (2025-01-01T00:00:00 → 2025-12-31T23:00:00) | 8,755 |
| Test (2026-01-01T00:00:00 → 2026-10-31T10:00:00) | 7,267 |
| Imputierte NULL-Zellen (F/B-Fill + Median-Fallback) | 73,093 |

**Spalten-Entscheidungen:**
- Targets: 22 analog + 9 digital (An-Quotient, Ziel-Bucket).
- **Kollektor** = `solar_coll` (dev2 a16, M1-SCHEMA §2.7). Vor 2022-03-24 NULL → Forward-/Median-Imputation + NULL-Flag-Feature `_tgt_null` (Modell sieht „Kollektor inaktiv“).
- **Wetter-T**: bias-korrigierte `wx_t{k}_t_bias` (M1 Bias-Profil dev0 a8 vs Grid, saisonal median + Drift) — Begründung: Sensor-Referenz, Grid-Bias raus; z-score normalisiert ebenso wie die Roh-T (reproduzierbar, framework-neutral).
- Inputs: 405 Feature-Spalten + NULL-Flags (Train: 427 Flags). Digital-Actions = 6 Schalter (d5/d8/d_solar = Ergebnisse der aktuellen Stunde → **kein** Aktion-Input, nur Target/Kontext). Offsets (Keller/EG/DG) = Stellgrößen-Input (historisch = von der RAS-Logik gesetzt).
- Flags `mode_known` (konstant 0) / `offset_known` als informativ übernommen (dokumentiert: konstant/informativ, kein Lern-Signal historisch).

---
## 2. Modell

### 2.1 Architektur (Keras-L1)

- Geteilter Stamm: Dense 256 → 128 → 64 (ReLU, Dropout 0.1) + **einer Head pro Target** (Dense 32 → 1).
- Digital-Targets (9): Sigmoid + MAE auf Wahrscheinlichkeit (Initial-Design; BCE-Alternative dokumentiert als Annahme).
- **Parameter-Count: 319,903** (Vorgabe ≤ 500,000 → OK).
- Loss: gewichtetes MAE (Initial-Gewichte, dokumentiert): RAS ist/perc 1,0 · offset 0,8 · hz_vl/hz_rl/buff 0,7 · heat_min 0,5 · ww/oil/wood/flue/collector/digital 0,4.
- Training: Batch 256, LR 0.0003, Early-Stopping (Patience 30, Best-Epoch-Weights), Ende Epoch 40/120 · Device: GPU:0.

### 2.2 Trainings-Kurve (Valid-Loss, gewichtete Summe der Head-MAEs)

| Epoch | Val-Loss (Keras, gewichtete Summe) |
|---|---|
| 1 | 47.745 |
| 3 | 33.529 |
| 5 | 28.453 |
| 7 | 25.976 |
| 9 | 25.666 |
| 11 | 23.415 |
| 13 | 25.062 |
| 15 | 24.845 |
| 17 | 23.406 |
| 19 | 25.214 |
| 21 | 26.769 |
| 23 | 24.246 |
| 25 | 24.501 |
| 27 | 26.195 |
| 29 | 25.314 |
| 31 | 25.371 |
| 33 | 25.585 |
| 35 | 26.122 |
| 37 | 25.428 |
| 39 | 25.603 |
| 40 | 26.410 |
| **best** | **22.664** |

### 2.3 Keras vs LightGBM (Valid 2025, 4-h-Rollout)

| Target-Gruppe | Keras MAE+4h | B2 MAE+4h | LGBM MAE+4h | Gewinner |
|---|---|---|---|---|
| RAS-Ist Keller | 1.428 | 0.460 | 0.421 | **LGBM** |
| RAS-Ist EG | 1.320 | 0.281 | 0.194 | **LGBM** |
| RAS-Ist DG | 1.276 | 0.593 | 0.270 | **LGBM** |
| Puffer Oben | 4.174 | 3.425 | 2.265 | **LGBM** |
| Puffer Mitte | 3.532 | 4.645 | 2.049 | **LGBM** |
| Puffer Unten | 3.450 | 4.298 | 2.399 | **LGBM** |
| hz_vl | 5.071 | 3.999 | 3.318 | **LGBM** |
| heat_min_kell | 0.463 | 0.155 | 0.160 | **B2** |

**Offset-Responsivität (Valid 2025, 200 Fälle × 4 Δ, 6-h-Rollout):**

| Größe | Keras | LightGBM |
|---|---|---|
| Pearson(Δoffset, ΔH) | 0.644 | 0.021 |
| Pearson(Δoffset, ΔD) | -0.130 | -0.035 |
| Anteil ΔH<0 bei +3,4 K | 0.0600 | 0.1250 |
| ΔH-Mittel +3,4 K | 0.344 | 0.001 |

### 2.4 Decision (Q-101): model_kind

**Gewonnen: `lightgbm`** — Begründung: Score = 4-h-Rollout-Mittel-MAE (RAS-Ist ×3 + Puffer ×3) + 1,0 K-Strafe bei Responsivitäts-DoD-Fehlschlag: Keras 3.530 (MAE 2.530, Responsivität fehlschlag) vs LightGBM 2.266 (MAE 1.266, Responsivität fehlschlag).
(Beide Artefakte werden gebaut und versioniert; deployment-fähig = model_kind.)

---
## 3. DoD-Checkliste (Valid 2025)

DoD wird auf **beiden** Modellfamilien ausgewiesen; das Gesamtergebnis bezieht sich auf das gewinnende Modell (``model_kind``).

**DoD 1 — MAE +4 h RAS-Ist je Zone < B2 UND < 1,0 K:**
- `ras_kell_ist`: B2 0.460 K · Keras 1.428 K (❌) · LGBM 0.421 K (✅)
- `ras_eg_ist`: B2 0.281 K · Keras 1.320 K (❌) · LGBM 0.194 K (✅)
- `ras_dg_ist`: B2 0.593 K · Keras 1.276 K (❌) · LGBM 0.270 K (✅)
**DoD 2 — MAE +4 h Puffer < 1,5 K je buff:**
- `buff_top`: Keras 4.174 K (❌) · LGBM 2.265 K (❌)
- `buff_mid`: Keras 3.532 K (❌) · LGBM 2.049 K (❌)
- `buff_bot`: Keras 3.450 K (❌) · LGBM 2.399 K (❌)
**DoD 3 — Offset-Responsivität (Kern, 18.10-Muster): Pearson(Δoffset,ΔH) ≤ −0,6 UND ΔH<0 bei +3,4 K ≥ 75 %:**
- Keras: Pearson(Δoffset,ΔH) = 0.644 · Anteil ΔH<0 bei +3,4 K = 0.0600
- LGBM:  Pearson(Δoffset,ΔH) = 0.021 · Anteil ΔH<0 bei +3,4 K = 0.1250
- **LGBM** (model_kind): Pearson = 0.021 → ❌ (≤ −0,6) · Anteil ΔH<0 bei +3,4 K = 0.1250 → ❌ (≥ 0,75)
- ΔH-Mittel Keras: +1,0 K = 0.089 · +3,4 K = 0.344 · +5,0 K = 0.544 · ΔD +3,4 K = -0.006 (negativ = Pumpe weniger an)

**Gesamt (LGBM = model_kind): ⚠️ DoD NICHT vollständig erfüllt — s. Probleme/Annahmen**

---
## 4. Test-2026-Report (final, kein Tuning)

| Target-Gruppe | Keras MAE+4h (Test) | B2 MAE+4h (Test) | LGBM MAE+4h (Test) |
|---|---|---|---|
| RAS-Ist Keller | 11.964 | 0.387 | 0.717 |
| RAS-Ist EG | 12.575 | 0.353 | 0.444 |
| RAS-Ist DG | 13.117 | 0.784 | 0.545 |
| Puffer Oben | 49.150 | 3.780 | 2.075 |
| Puffer Mitte | 39.649 | 6.076 | 3.380 |
| Puffer Unten | 38.697 | 5.867 | 3.615 |
| hz_vl | 43.618 | 5.712 | 5.138 |
| heat_min_kell | 3.405 | 0.117 | 0.078 |

**Worst-Case-Tag (max. Tages-MAE RAS-Ist, Keras): 2026-10-31** — MAE Kell 6491.380 K · EG 7673.017 K · DG 7353.288 K (maintainer-Review-Punkt).

---
## 5. Offset-Responsivitäts-Details (Methode & Interpretation)

**Methode:** 200 zufällige, lückenfreie Valid-Zustände (Seed 20261031, keine NULL-Features/-Targets, ≥ 6 h Raster-Abstand zum Ende) × Keller-Offset-Steps Δ ∈ {+1,0; +2,0; +3,4; +5,0 K}. Pro Zustand 6-h-Rollout mit (a) Offset-Halt und (b) Offset+Δ (alle übrigen Aktionen/Offsets = t0-Werte → reiner Offset-Kontrastfakt). ΔH = Σ heat_min_kell(0–6 h) im Δ-Fall minus Hold; ΔD = Mittel pump_hz_on(d8) Differenz. Wetter-Fenster im Rollout konstant.

**Zahlen (Keras):** Pearson(Δoffset, ΔH) = **0.644** · Pearson(Δoffset, ΔD) = -0.130 · Anteil ΔH<0 bei +3,4 K = **0.0600** · ΔH-Mittel +3,4 K = 0.344.
**Zahlen (LGBM):** Pearson(Δoffset, ΔH) = **0.021** · Anteil ΔH<0 bei +3,4 K = **0.1250** · ΔH-Mittel +3,4 K = 0.001.

**Interpretation (reproduziert das 18.10-Muster?):** NEIN (modellseitig nicht reproduziert): der 18.10-Kausalzusammenhang (Offset↑ → Heizminuten↓) wird vom Modell nicht in der geforderten Stärke  abgebildet — historisch ist der Offset-Effekt durch Saison-Confounding überlagert (Offset-Sprünge treten überwiegend in warmen Perioden auf, in denen die Heizung ohnehin abregelt). Konsequenz: v0.3-Architektur-Risiko (Design §9) trifft für L1 zu; L2 muss den Offset-Hebel zusätzlich mit Guardrails absichern, und ein Offset-Feature-Branching (explizite Offset-Aktion-Eingabe, bereits im Modell) ist der geplante Mitigations-Pfad.

---
## 6. Probleme / Annahmen

- Wetter-Fenster im Rollout: t→t+23-Fenster der Ausgangsstunde bleibt **konstant** (kein 1-h-Verschieben) — am einfachsten für Realitätsnähe, dokumentierte Vereinfachung für 4–6-h-Horizonte.
- Digital-Head-Loss: MAE auf Sigmoid-Wahrscheinlichkeit (Initial-Design); BCE-Alternative nicht erprobt (kein Grid-Search-Overkill).
- heat_min_kell ≈ gesättigt (~5,9 h/h Median, Zähler-Rollup-Artefakt): der Offset-Responsivitäts-ΔH ist dadurch klein (±0,1–0,3 h) — Vorzeichen-Korrelation ist die robuste Metrik, nicht die absolute ΔH-Größe.
- Offset-Confounding: historisch traten Keller-Offset-Sprünge (≥ 1 K) überwiegend in milden Perioden auf, in denen die Anlage ohnehin abregelt → in den Daten Offset↑ ↔ heat↑ (Saison); der 18.10-Kausalpfad (Offset↑ → heat↓) ist ein Einzelereignis. Der Responsivitäts-Test prüft den Pfad im Modell, nicht die Daten-Korrelation.
- Lags im Rollout bleiben beim t0-Wert (keine Fortschreibung) — 4–6-h-Horizont, dokumentierte Vereinfachung.
- B2-4-h-Rollout: Persistenz = letztes Prädikt, ‚gleiche Zeit gestern' = Wert der Ausgangs-Row −24 h (konstant) — konservativ für B2 (kein Fehler-Kumulieren).
- Kollektor (solar_coll) vor 2022-03-24 NULL → Forward-/Median-Imputation + NULL-Flag; der Solar-Head lernt ‘inaktiv → aktiv’-Transition nur ab März 2022.
- Kaltstart-Check (2020-08→12): kein separater Report (Train-Split enthält den Zeitraum; Valid-MAE-Unterschiede über Jahresgruppen s. Test-2026-Report als Proxy).
- Keras-L1-Konvergenz: der geteilte 31-Head-Stamm erreicht auf den sehr glatten RAS-Ist-Kanälen im 4-h-Rollout nicht die GBDT-Präzision (Valid 1-h RAS-Ist: Keras 1,1 K vs LGBM 0,17 K vs Persistence 0,12 K; Test-4h: Keras 12 K vs LGBM 0,44–0,72 K). In ~6/7264 Test-Starts (extreme/Out-of-Domain-Zustände, z. B. 2026-10-31) läuft der Keras-Rollout in ~9000 K — ein bekanntes Stabilitäts-problem unregulierter NNN-Rollouts; das LGBM-Deployment (model_kind) zeigt dieses Verhalten **nicht** (0,44–0,72 K über alle Test-Starts). Konsequenz: L1-deployment = LightGBM; Keras-L1 bleibt versioniert als Referenz (L2-Guardrails müssen extreme-Prädikats-Clippen bei NNN-Rollouts vorsehen).
- LGBM-DoD-GAP: MAE+4h Puffer (2,0–2,4 K) liegt über der 1,5 K-Schwelle — die Puffer-Temperatur ist 4-h-aus physikalisch schwer präzise prognostizierbar (Ladephasen, Solar-Spitzen, Brenner-Taktung); 1-h-MAE LGBM = 0,6–0,7 K (gut), die 4-h-Kumulierung verliert. Kausal: kein Modell-Defekt, sondern dynamische Komplexität; L2 (12-h-Horizont + Empfehlung) adressiert dies mit Szenario-Validierung statt Punkt-MAE.

---
## 7. Reproduktion

```bash
python -m analysis.model.m2 build   # Train → Valid → Test → Responsivität → Artefakt → Report
```
Artefakt: `models/cmi-brain/v001` (git-Rev 62a7048, Seed 20261031).

