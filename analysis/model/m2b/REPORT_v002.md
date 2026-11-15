# M2b-Report — L1-Modell v002 (`cmi-brain-v002`)

Erzeugt 2026-11-01T08:06:39 · git `62a7048` · Daten `m1.2` · Features `m2b-f2` (509) · Seed 20261031

Doku/Entscheidungen: `docs/l1-model-v002.md` · Artefakt: `models/cmi-brain/v002/`

## 1. Ergebnis in einem Satz

Gewinner nach Q-101-Regel: **lgbm** (mittlerer Skill vs. Persistence B0 auf Valid: lgbm 0.318, lgbm_plain 0.319, keras 0.179). Deploy-LightGBM: `refit_train_valid`.

## 2. DoD (Valid 2025, +4 h)

| Modell | RAS-Ist < B0 und < 1,0 K | Puffer < 1,5 K | keine Explosion (max. abs. Vorhersage) | Responsivität |
|---|---|---|---|---|
| lgbm | ✅ | ❌ | ✅ (179.6) | ✅ |
| lgbm_plain | ✅ | ❌ | ✅ (179.6) | ✅ |
| keras | ❌ | ❌ | ✅ (167.6) | ✅ |

## 3. MAE +4 h je DoD-Kanal (Valid / Test) vs. Baselines und v001

| Target | v002-LGBM | v002-LGBM-Refit (Test) | Keras v2 | B0 Persistence | B0-last | B2 | v001 (Rollout) |
|---|---|---|---|---|---|---|---|
| ras_kell_ist | 0.197 / 0.198 | 0.221 | 0.209 / 0.263 | 0.202 / 0.184 | 0.200 / 0.177 | 0.299 / 0.254 | 0.421 / 0.717 |
| ras_eg_ist | 0.141 / 0.132 | 0.141 | 0.162 / 0.179 | 0.142 / 0.129 | 0.153 / 0.126 | 0.182 / 0.218 | 0.194 / 0.444 |
| ras_dg_ist | 0.204 / 0.254 | 0.242 | 0.219 / 0.258 | 0.309 / 0.377 | 0.282 / 0.343 | 0.394 / 0.482 | 0.270 / 0.545 |
| buff_top | 1.369 / 1.505 | 1.383 | 2.053 / 1.866 | 2.882 / 2.711 | 2.834 / 2.577 | 2.521 / 2.662 | 2.27 / 2.08 |
| buff_mid | 1.688 / 2.763 | 2.747 | 1.871 / 2.901 | 3.268 / 4.055 | 2.977 / 3.738 | 3.034 / 4.058 | 2.05 / 3.38 |
| buff_bot | 1.856 / 2.791 | 2.835 | 1.995 / 2.886 | 3.144 / 4.029 | 2.914 / 3.806 | 2.832 / 3.935 | – / – |

## 4. Alle Targets × Horizonte (LightGBM, Valid / Test, Skill vs. B0 Valid)

| Target | Metrik | +1 h | +4 h | +6 h | +12 h |
|---|---|---|---|---|---|
| ras_kell_ist | MAE | 0.059 / 0.059 (S 0.53) | 0.197 / 0.198 (S 0.02) | 0.262 / 0.237 (S 0.01) | 0.354 / 0.345 (S 0.02) |
| ras_eg_ist | MAE | 0.064 / 0.040 (S 0.36) | 0.141 / 0.132 (S 0.01) | 0.169 / 0.169 (S 0.00) | 0.215 / 0.251 (S 0.02) |
| ras_dg_ist | MAE | 0.050 / 0.055 (S 0.48) | 0.204 / 0.254 (S 0.34) | 0.274 / 0.350 (S 0.34) | 0.372 / 0.494 (S 0.39) |
| ras_kell_perc | MAE | 0.078 / 0.055 (S 0.47) | 0.245 / 0.196 (S 0.01) | 0.315 / 0.248 (S 0.01) | 0.377 / 0.344 (S 0.08) |
| ras_eg_perc | MAE | 0.067 / 0.043 (S 0.36) | 0.150 / 0.144 (S 0.01) | 0.182 / 0.184 (S 0.00) | 0.230 / 0.270 (S 0.03) |
| ras_dg_perc | MAE | 0.052 / 0.056 (S 0.47) | 0.208 / 0.257 (S 0.34) | 0.277 / 0.347 (S 0.35) | 0.379 / 0.492 (S 0.39) |
| hz_vl | MAE | 1.167 / 1.470 (S 0.44) | 3.133 / 4.425 (S 0.01) | 3.738 / 5.119 (S 0.01) | 2.745 / 4.571 (S 0.41) |
| hz_rl | MAE | 0.795 / 1.056 (S 0.57) | 2.170 / 3.255 (S 0.18) | 2.569 / 3.830 (S 0.16) | 2.301 / 3.809 (S 0.37) |
| buff_top | MAE | 0.397 / 0.396 (S 0.67) | 1.369 / 1.505 (S 0.52) | 1.618 / 1.869 (S 0.50) | 1.964 / 2.349 (S 0.47) |
| buff_mid | MAE | 0.358 / 0.622 (S 0.64) | 1.688 / 2.763 (S 0.48) | 2.242 / 3.493 (S 0.49) | 2.810 / 4.478 (S 0.54) |
| buff_bot | MAE | 0.461 / 0.784 (S 0.57) | 1.856 / 2.791 (S 0.41) | 2.324 / 3.454 (S 0.43) | 2.713 / 4.441 (S 0.51) |
| ww | MAE | 2.483 / 2.447 (S 0.34) | 4.461 / 4.694 (S 0.34) | 4.533 / 4.839 (S 0.42) | 4.733 / 4.617 (S 0.51) |
| solar_coll | MAE | 1.300 / 1.504 (S 0.71) | 2.883 / 3.233 (S 0.81) | 3.029 / 3.428 (S 0.86) | 3.113 / 3.472 (S 0.89) |
| wood_vl | MAE | 0.040 / 0.112 (S 0.54) | 0.203 / 0.632 (S 0.33) | 0.331 / 0.985 (S 0.23) | 0.603 / 1.661 (S 0.15) |
| flue | MAE | 0.075 / 0.190 (S 0.29) | 0.176 / 0.464 (S 0.19) | 0.200 / 0.539 (S 0.19) | 0.249 / 0.579 (S 0.21) |
| oil_vl | MAE | 1.599 / 1.808 (S 0.47) | 3.570 / 5.114 (S 0.33) | 4.127 / 6.329 (S 0.30) | 4.529 / 7.172 (S 0.33) |
| hzrkl_kell_pos | MAE | 0.727 / 1.131 (S 0.58) | 2.047 / 2.866 (S 0.01) | 2.470 / 3.212 (S 0.03) | 2.494 / 3.204 (S 0.15) |
| hzrkl_eg_pos | MAE | 1.058 / 1.719 (S 0.53) | 2.547 / 4.270 (S 0.00) | 2.848 / 4.866 (S 0.00) | 2.614 / 4.376 (S 0.21) |
| hzrkl_dg_pos | MAE | 0.583 / 0.882 (S 0.58) | 2.343 / 4.045 (S 0.00) | 2.577 / 4.297 (S -0.00) | 2.365 / 3.968 (S 0.20) |
| oel_burner_on | RMSE | 0.133 / 0.143 (S 0.36) | 0.211 / 0.216 (S 0.01) | 0.222 / 0.246 (S -0.02) | 0.198 / 0.225 (S 0.15) |
| pump_hz_on | RMSE | 0.250 / 0.243 (S 0.21) | 0.370 / 0.391 (S -0.02) | 0.393 / 0.410 (S -0.05) | 0.324 / 0.373 (S 0.16) |
| pump_ww_on | RMSE | 0.080 / 0.058 (S 0.56) | 0.119 / 0.080 (S 0.37) | 0.121 / 0.082 (S 0.33) | 0.123 / 0.082 (S 0.33) |
| mix_open_on | RMSE | 0.022 / 0.030 (S 0.35) | 0.025 / 0.032 (S 0.21) | 0.025 / 0.032 (S 0.23) | 0.024 / 0.032 (S 0.28) |
| mix_close_on | RMSE | 0.108 / 0.093 (S 0.37) | 0.121 / 0.105 (S 0.20) | 0.120 / 0.104 (S 0.23) | 0.117 / 0.102 (S 0.27) |
| pump_ww_oil_on | RMSE | 0.163 / 0.179 (S 0.09) | 0.189 / 0.241 (S 0.13) | 0.190 / 0.216 (S 0.18) | 0.186 / 0.195 (S 0.28) |
| pump_circ_on | RMSE | 0.162 / 0.155 (S 0.11) | 0.191 / 0.199 (S 0.14) | 0.191 / 0.189 (S 0.19) | 0.187 / 0.177 (S 0.29) |
| valve_hzrl_on | RMSE | 0.127 / 0.329 (S 0.49) | 0.286 / 0.367 (S 0.14) | 0.331 / 0.367 (S 0.12) | 0.252 / 0.443 (S 0.41) |
| pump_solar_on | RMSE | 0.089 / 0.096 (S 0.48) | 0.110 / 0.124 (S 0.70) | 0.111 / 0.125 (S 0.74) | 0.112 / 0.129 (S 0.75) |
| hzrkl_kell_on | RMSE | 0.161 / 0.179 (S 0.43) | 0.299 / 0.327 (S -0.01) | 0.324 / 0.349 (S 0.01) | 0.321 / 0.308 (S 0.08) |
| hzrkl_eg_on | RMSE | 0.205 / 0.254 (S 0.38) | 0.323 / 0.400 (S 0.05) | 0.341 / 0.424 (S 0.05) | 0.343 / 0.445 (S 0.09) |
| hzrkl_dg_on | RMSE | 0.135 / 0.132 (S 0.32) | 0.310 / 0.345 (S 0.08) | 0.317 / 0.361 (S 0.07) | 0.338 / 0.373 (S 0.02) |

S = Skill = 1 − Fehler/Fehler_B0 (> 0 besser als Persistence). Analog: MAE in K; Digital (An-Quotient 0..1): RMSE (Brier-artig, s. `evaluate.py`).

### 4b. Keras v2 (Referenz), +4 h, Valid / Test

| Target | Metrik | Keras v2 | LightGBM | B0 |
|---|---|---|---|---|
| ras_kell_ist | MAE | 0.209 / 0.263 | 0.197 / 0.198 | 0.202 / 0.184 |
| ras_eg_ist | MAE | 0.162 / 0.179 | 0.141 / 0.132 | 0.142 / 0.129 |
| ras_dg_ist | MAE | 0.219 / 0.258 | 0.204 / 0.254 | 0.309 / 0.377 |
| ras_kell_perc | MAE | 0.261 / 0.255 | 0.245 / 0.196 | 0.248 / 0.195 |
| ras_eg_perc | MAE | 0.172 / 0.193 | 0.150 / 0.144 | 0.152 / 0.141 |
| ras_dg_perc | MAE | 0.227 / 0.263 | 0.208 / 0.257 | 0.317 / 0.381 |
| hz_vl | MAE | 3.223 / 5.102 | 3.133 / 4.425 | 3.155 / 4.470 |
| hz_rl | MAE | 2.352 / 3.818 | 2.170 / 3.255 | 2.637 / 3.587 |
| buff_top | MAE | 2.053 / 1.866 | 1.369 / 1.505 | 2.882 / 2.711 |
| buff_mid | MAE | 1.871 / 2.901 | 1.688 / 2.763 | 3.268 / 4.055 |
| buff_bot | MAE | 1.995 / 2.886 | 1.856 / 2.791 | 3.144 / 4.029 |
| ww | MAE | 5.228 / 5.134 | 4.461 / 4.694 | 6.785 / 6.642 |
| solar_coll | MAE | 5.857 / 7.099 | 2.883 / 3.233 | 15.260 / 17.742 |
| wood_vl | MAE | 0.326 / 0.780 | 0.203 / 0.632 | 0.304 / 0.850 |
| flue | MAE | 0.216 / 0.528 | 0.176 / 0.464 | 0.217 / 0.589 |
| oil_vl | MAE | 4.301 / 6.052 | 3.570 / 5.114 | 5.308 / 6.208 |
| hzrkl_kell_pos | MAE | 2.125 / 3.289 | 2.047 / 2.866 | 2.065 / 2.933 |
| hzrkl_eg_pos | MAE | 2.688 / 4.632 | 2.547 / 4.270 | 2.559 / 4.304 |
| hzrkl_dg_pos | MAE | 2.496 / 4.055 | 2.343 / 4.045 | 2.349 / 4.096 |
| oel_burner_on | RMSE | 0.197 / 0.212 | 0.211 / 0.216 | 0.213 / 0.205 |
| pump_hz_on | RMSE | 0.332 / 0.381 | 0.370 / 0.391 | 0.363 / 0.437 |
| pump_ww_on | RMSE | 0.127 / 0.082 | 0.119 / 0.080 | 0.187 / 0.118 |
| mix_open_on | RMSE | 0.025 / 0.032 | 0.025 / 0.032 | 0.032 / 0.042 |
| mix_close_on | RMSE | 0.127 / 0.108 | 0.121 / 0.105 | 0.152 / 0.139 |
| pump_ww_oil_on | RMSE | 0.186 / 0.176 | 0.189 / 0.241 | 0.217 / 0.198 |
| pump_circ_on | RMSE | 0.188 / 0.174 | 0.191 / 0.199 | 0.222 / 0.204 |
| valve_hzrl_on | RMSE | 0.266 / 0.399 | 0.286 / 0.367 | 0.333 / 0.095 |
| pump_solar_on | RMSE | 0.154 / 0.157 | 0.110 / 0.124 | 0.365 / 0.389 |
| hzrkl_kell_on | RMSE | 0.263 / 0.281 | 0.299 / 0.327 | 0.296 / 0.301 |
| hzrkl_eg_on | RMSE | 0.300 / 0.369 | 0.323 / 0.400 | 0.340 / 0.378 |
| hzrkl_dg_on | RMSE | 0.280 / 0.319 | 0.310 / 0.345 | 0.337 / 0.407 |

## 5. Offset-Responsivität v2 (je Etage, Heizsaison-Fälle)

Eingriff: Offset + perzipierte T der Etage um Δ ∈ {1; 2; 3,4; 5} K. Wirkung = mittlere Änderung der Vorhersage über +1/+4/+6 h. DoD-Größe = Rücklaufminderer der Etage (`hzrkl_{z}_on`), DoD-Zone = **eg** (Q-110). Werte: Pearson(Δ, Wirkung) / Anteil Wirkung < 0 bei +3,4 K.

| Modell (Fälle) | Zone | Ventil an (DoD) | Ventilstellung | Pumpe d8 | RAS-Ist +6 h | bestanden |
|---|---|---|---|---|---|---|
| lgbm (valid) | eg (DoD) | -0.73 / 0.99 | -0.01 / 0.96 | -0.19 / 1.00 | -0.01 / 0.13 | ✅ |
| lgbm (valid) | dg | -0.27 / 0.98 | -0.01 / 0.76 | -0.27 / 0.99 | -0.40 / 1.00 | ❌ |
| lgbm (valid) | kell | -0.12 / 0.98 | -0.02 / 0.86 | -0.51 / 0.98 | -0.09 / 0.64 | ❌ |
| lgbm_plain (valid) | eg (DoD) | -0.73 / 0.99 | -0.01 / 0.96 | -0.10 / 0.89 | -0.00 / 0.20 | ✅ |
| lgbm_plain (valid) | dg | -0.25 / 0.97 | -0.01 / 0.76 | -0.30 / 0.94 | -0.25 / 1.00 | ❌ |
| lgbm_plain (valid) | kell | -0.14 / 0.98 | -0.02 / 0.86 | -0.36 / 0.84 | -0.02 / 0.55 | ❌ |
| keras (valid) | eg (DoD) | -0.71 / 0.98 | -0.58 / 0.96 | -0.57 / 0.95 | -0.86 / 1.00 | ✅ |
| keras (valid) | dg | -0.80 / 0.98 | -0.72 / 0.99 | -0.65 / 0.96 | -0.81 / 1.00 | ✅ |
| keras (valid) | kell | -0.56 / 1.00 | -0.63 / 0.97 | 0.05 / 0.46 | -0.75 / 1.00 | ❌ |
| lgbm_refit@test (test) | eg (DoD) | -0.34 / 1.00 | -0.22 / 0.98 | -0.29 / 1.00 | -0.16 / 0.94 | ❌ |
| lgbm_refit@test (test) | dg | -0.21 / 1.00 | -0.05 / 0.94 | -0.19 / 1.00 | -0.46 / 0.98 | ❌ |
| lgbm_refit@test (test) | kell | -0.20 / 0.98 | -0.10 / 0.88 | -0.15 / 0.95 | -0.12 / 0.85 | ❌ |
| lgbm@test (test) | eg (DoD) | -0.55 / 1.00 | -0.03 / 0.93 | -0.24 / 1.00 | -0.08 / 0.11 | ❌ |
| lgbm@test (test) | dg | -0.25 / 1.00 | -0.02 / 0.60 | -0.30 / 0.99 | -0.48 / 0.98 | ❌ |
| lgbm@test (test) | kell | -0.22 / 0.98 | -0.04 / 0.93 | -0.49 / 0.98 | -0.12 / 0.68 | ❌ |

## 6. Monotonie-Varianten (LightGBM, Valid-MAE frei vs. monoton)

| Target@h | frei | monoton | gewählt |
|---|---|---|---|
| ras_kell_ist@1 | 0.0585 | 0.0593 | monoton |
| ras_kell_ist@4 | 0.1940 | 0.1974 | monoton |
| ras_kell_ist@6 | 0.2599 | 0.2622 | monoton |
| ras_kell_ist@12 | 0.3500 | 0.3537 | monoton |
| ras_eg_ist@1 | 0.0645 | 0.0639 | monoton |
| ras_eg_ist@4 | 0.1382 | 0.1409 | monoton |
| ras_eg_ist@6 | 0.1692 | 0.1695 | monoton |
| ras_eg_ist@12 | 0.2152 | 0.2153 | monoton |
| ras_dg_ist@1 | 0.0501 | 0.0547 | frei |
| ras_dg_ist@4 | 0.2038 | 0.2085 | frei |
| ras_dg_ist@6 | 0.2705 | 0.2737 | monoton |
| ras_dg_ist@12 | 0.3720 | 0.3872 | frei |
| ras_kell_perc@1 | 0.0785 | 0.0810 | frei |
| ras_kell_perc@4 | 0.2408 | 0.2454 | monoton |
| ras_kell_perc@6 | 0.3136 | 0.3147 | monoton |
| ras_kell_perc@12 | 0.3767 | 0.3896 | frei |
| ras_eg_perc@1 | 0.0676 | 0.0667 | monoton |
| ras_eg_perc@4 | 0.1476 | 0.1503 | monoton |
| ras_eg_perc@6 | 0.1819 | 0.1816 | monoton |
| ras_eg_perc@12 | 0.2331 | 0.2299 | monoton |
| ras_dg_perc@1 | 0.0523 | 0.0567 | frei |
| ras_dg_perc@4 | 0.2084 | 0.2135 | frei |
| ras_dg_perc@6 | 0.2771 | 0.2846 | frei |
| ras_dg_perc@12 | 0.3791 | 0.3945 | frei |
| pump_hz_on@1 | 0.2367 | 0.2498 | monoton |
| pump_hz_on@4 | 0.3646 | 0.3702 | monoton |
| pump_hz_on@6 | 0.3891 | 0.3927 | monoton |
| pump_hz_on@12 | 0.3226 | 0.3240 | monoton |
| hzrkl_kell_on@1 | 0.1604 | 0.1607 | monoton |
| hzrkl_kell_on@4 | 0.3034 | 0.2987 | monoton |
| hzrkl_kell_on@6 | 0.3229 | 0.3235 | monoton |
| hzrkl_kell_on@12 | 0.3171 | 0.3209 | monoton |
| hzrkl_eg_on@1 | 0.2079 | 0.2054 | monoton |
| hzrkl_eg_on@4 | 0.3313 | 0.3228 | monoton |
| hzrkl_eg_on@6 | 0.3429 | 0.3408 | monoton |
| hzrkl_eg_on@12 | 0.3427 | 0.3430 | monoton |
| hzrkl_dg_on@1 | 0.1339 | 0.1349 | monoton |
| hzrkl_dg_on@4 | 0.3114 | 0.3101 | monoton |
| hzrkl_dg_on@6 | 0.3191 | 0.3170 | monoton |
| hzrkl_dg_on@12 | 0.3390 | 0.3376 | monoton |

## 7. Wichtigste Features (+4 h, Gain-Anteil)

- `ras_kell_ist@4`: hzrkl_kell_on 0.1061, hour 0.0806, hour_sin 0.0723, ras_kell_ist_trend 0.0681, hour_cos 0.068, wxa_swr_sum_6 0.0543, ras_kell_perc 0.0525, ras_kell_perc_last 0.0433
- `ras_eg_ist@4`: hour_cos 0.2264, ras_eg_perc 0.1043, wx_t15_swr 0.0711, hour_sin 0.0668, hz_vl_trend 0.0553, ras_eg_perc_lag1 0.0522, wx_t16_swr 0.0514, wxa_swr_sum_4 0.0383
- `ras_dg_ist@4`: wx_t4_swr 0.087, ras_dg_ist_trend 0.0563, hour_cos 0.0529, ras_dg_perc 0.0372, ras_dg_perc_trend 0.0351, wx_t5_swr 0.0321, wx_t16_swr 0.0251, wx_t15_swr 0.0209
- `ras_kell_perc@4`: hour_sin 0.1385, hzrkl_kell_on 0.1055, ras_kell_perc 0.0875, hour_cos 0.079, ras_kell_perc_lag1 0.0712, hour 0.0404, ras_kell_ist_trend 0.0401, wx_t6_swr 0.0334
- `ras_eg_perc@4`: hour_cos 0.2133, ras_eg_perc 0.1073, hz_vl_trend 0.0727, wx_t15_swr 0.0696, hour_sin 0.0694, ras_eg_perc_lag1 0.0528, wx_t16_swr 0.0512, ras_eg_ist 0.0295
- `ras_dg_perc@4`: wx_t4_swr 0.0742, ras_dg_perc_trend 0.0469, ras_dg_ist_trend 0.0331, ras_dg_perc 0.0326, wx_t5_swr 0.0323, hour_cos 0.0291, wx_t3_swr 0.0229, wx_t16_swr 0.0184
- `hz_vl@4`: hz_vl 0.1352, hz_vl_lag1 0.1011, wx_t5_swr 0.0746, hour 0.0383, hz_rl_trend 0.0321, mix_open_on 0.0317, hz_vl_trend 0.0298, ras_eg_ist_lag12 0.028
- `hz_rl@4`: hz_rl 0.0694, hour 0.0431, hz_rl_trend 0.0379, hz_vl_trend 0.0346, wx_t5_t_bias 0.0247, wx_t6_t_bias 0.0243, hour_sin 0.0242, hour_cos 0.0226
- `buff_top@4`: buff_top 0.0716, hour 0.0438, buff_top_last 0.042, hour_cos 0.0372, buff_top_d1 0.0326, buff_top_trend 0.0313, pump_ww_on 0.0232, wx_t16_swr 0.0186
- `buff_mid@4`: buff_mid_trend 0.0414, buff_bot_trend 0.0314, buff_mid 0.0304, valve_hzrl_on 0.0215, wx_t5_swr 0.0161, buff_mid_lag1 0.016, buff_mid_last 0.0146, solar_coll_trend 0.0143
- `buff_bot@4`: buff_bot_trend 0.0409, buff_bot 0.0307, valve_hzrl_on 0.0191, hour 0.0163, wx_t4_swr 0.0162, buff_bot_lag1 0.0123, buff_mid_trend 0.0123, hour_cos 0.0115
- `ww@4`: ww 0.1053, hour 0.101, hour_cos 0.0603, pump_circ_on 0.0255, ww_trend 0.0242, ww_lag12 0.0201, hour_sin 0.0184, wxa_swr_sum_6 0.018
- `solar_coll@4`: solar_coll 0.0475, solar_coll_trend 0.0405, wx_t21_swr 0.0368, hour_sin 0.0294, wx_t5_swr 0.0188, solar_coll_last 0.0183, wx_t22_swr 0.0137, wx_t6_swr 0.0126
- `wood_vl@4`: wood_vl_trend 0.0465, wood_vl 0.0399, hour 0.0376, wood_vl_last 0.0232, ext_lag6 0.0186, ras_eg_ist_d1 0.0151, ext_lag2 0.0131, ras_eg_ist_lag24 0.0114
- `flue@4`: flue 0.1554, hour 0.0433, wood_vl 0.028, flue_last 0.025, hour_sin 0.0211, flue_trend 0.0204, wood_vl_last 0.0174, hour_cos 0.0152
- `oil_vl@4`: oil_vl 0.119, oil_vl_trend 0.0441, oil_vl_last 0.0413, hour 0.0354, buff_top_last 0.025, hz_vl_last 0.0235, hour_cos 0.0227, wx_t5_t_bias 0.0189
- `hzrkl_kell_pos@4`: hz_vl_lag1 0.2224, hour 0.1041, wx_t17_swr 0.0659, hzrkl_eg_pos_lag2 0.0532, hzrkl_kell_pos 0.0525, hour_cos 0.0454, wx_t16_swr 0.0416, hz_vl_d1 0.0415
- `hzrkl_eg_pos@4`: hz_vl_lag1 0.2294, hzrkl_eg_pos 0.1265, hour 0.0807, hour_cos 0.06, mix_open_on 0.0546, hz_rl_d1 0.0417, wx_t16_swr 0.0314, wx_t18_swr 0.0287
- `hzrkl_dg_pos@4`: hz_vl_lag1 0.2215, hzrkl_eg_pos_lag2 0.1139, hour 0.0882, hour_cos 0.0666, wx_t16_swr 0.0499, hz_rl_d1 0.0481, mix_open_on 0.0352, wx_t18_swr 0.0344
- `oel_burner_on@4`: wxa_t_mean_24 0.1496, hour 0.1076, wx_t5_t_bias 0.0906, wx_t4_t_bias 0.0628, buff_mid_last 0.0537, wxa_t_mean_12 0.0321, wxa_t_mean_6 0.031, wx_t16_swr 0.0301
- `pump_hz_on@4`: pump_hz_last 0.1892, ras_perc_min 0.1067, hour 0.0873, ras_perc_min_last 0.0545, wx_t4_t_bias 0.0396, wx_t5_t_bias 0.0366, pump_hz_on_lag24 0.0351, wx_t16_swr 0.0298
- `pump_ww_on@4`: hour 0.1763, buff_mid_last 0.0931, day_of_week 0.0524, buff_top_last 0.0435, hour_cos 0.0423, buff_mid 0.0418, hour_sin 0.0417, buff_top 0.0331
- `mix_open_on@4`: hour 0.1793, wxa_t_mean_6 0.0645, pump_hz_on_lag2 0.0553, ras_perc_min_last 0.0544, hour_cos 0.0464, ext 0.0432, wxa_t_mean_12 0.0415, wx_t6_t_bias 0.0407
- `mix_close_on@4`: ras_perc_min_last 0.2376, hour 0.1767, ras_perc_min 0.1089, hour_cos 0.0516, hour_sin 0.0479, ras_eg_perc 0.0252, ras_eg_perc_last 0.0196, buff_top_last 0.0147
- `pump_ww_oil_on@4`: hour 0.2708, hour_cos 0.0656, solar_coll_last 0.0605, hour_sin 0.0437, wx_is_forecast 0.0353, wx_t14_swr 0.035, ww_lag6 0.0303, day_of_week 0.0287
- `pump_circ_on@4`: hour 0.2606, solar_coll_last 0.0807, hour_cos 0.0616, ww_lag6 0.0426, hour_sin 0.0419, wx_t14_swr 0.0332, solar_coll 0.0309, day_of_week 0.0291
- `valve_hzrl_on@4`: wx_t5_t_bias 0.1825, wx_t4_t_bias 0.1256, wx_t6_t_bias 0.0704, buff_mid_last 0.0574, hour_cos 0.042, pump_hz_last 0.0418, hour 0.036, wx_t3_t_bias 0.0338
- `pump_solar_on@4`: wx_t5_swr 0.3313, wx_t6_swr 0.1929, wx_t8_swr 0.0462, wxa_swr_sum_6 0.0368, hour 0.028, wx_t7_swr 0.0277, wx_t19_swr 0.0214, hour_sin 0.0194
- `hzrkl_kell_on@4`: ras_kell_perc_last 0.1697, ras_kell_perc 0.168, hzrkl_kell_on 0.0734, hzrkl_kell_last 0.0527, offset_kell 0.0476, hour_cos 0.0217, hzrkl_kell_pos 0.0195, hour 0.0169
- `hzrkl_eg_on@4`: hzrkl_eg_on 0.2521, ras_eg_perc_last 0.1815, ras_eg_perc 0.1571, hzrkl_eg_last 0.0488, ras_perc_min_last 0.0236, offset_eg 0.0213, hour_cos 0.0147, buff_mid_lag2 0.0053
- `hzrkl_dg_on@4`: ras_dg_perc 0.2114, ras_dg_perc_last 0.2036, wx_t4_t_bias 0.0563, hzrkl_dg_on 0.0405, hzrkl_dg_last 0.0371, wx_t4_swr 0.0301, wx_t3_swr 0.022, wxa_t_mean_12 0.0206

## 8. Lauf-Metadaten

- Stunden: 54079 · Splits: {'pre': 0, 'train': 38016, 'valid': 8760, 'test': 7303} · Nicht-1-h-Rasterschritte (DST): 0
- Laufzeiten: {'lgbm_s': 1973.7, 'keras_s': 55.1, 'refit_s': 1555.9, 'total_s': 3696.5}
- Konfiguration: {"ras_stale_age": 3.0, "controller_targets": ["pump_hz_on", "hzrkl_kell_on", "hzrkl_eg_on", "hzrkl_dg_on"], "controller_causal": true, "excluded_features": {"pump_hz_on": ["ras_kell_ist", "ras_eg_ist", "ras_dg_ist", "ras_kell_perc_lag1", "ras_kell_perc_lag2", "ras_kell_perc_lag6", "ras_kell_perc_lag12", "ras_kell_perc_lag24", "ras_kell_ist_lag1", "ras_kell_ist_lag2", "ras_kell_ist_lag6", "ras_kell_ist_lag12", "ras_kell_ist_lag24", "offset_kell_lag1", "offset_kell_lag2", "offset_kell_lag6", "offset_kell_lag12", "offset_kell_lag24", "ras_eg_perc_lag1", "ras_eg_perc_lag2", "ras_eg_perc_lag6", "ras_eg_perc_lag12", "ras_eg_perc_lag24", "ras_eg_ist_lag1", "ras_eg_ist_lag2", "ras_eg_ist_lag6", "ras_eg_ist_lag12", "ras_eg_ist_lag24", "offset_eg_lag1", "offset_eg_lag2", "offset_eg_lag6", "offset_eg_lag12", "offset_eg_lag24", "ras_dg_perc_lag1", "ras_dg_perc_lag2", "ras_dg_perc_lag6", "ras_dg_perc_lag12", "ras_dg_perc_lag24", "ras_dg_ist_lag1", "ras_dg_ist_lag2", "ras_dg_ist_lag6", "ras_dg_ist_lag12", "ras_dg_ist_lag24", "offset_dg_lag1", "offset_dg_lag2", "offset_dg_lag6", "offset_dg_lag12", "offset_dg_lag24", "ras_kell_ist_last", "ras_eg_ist_last", "ras_dg_ist_last", "hzrkl_kell_on_lag1", "hzrkl_eg_on_lag1", "hzrkl_dg_on_lag1", "hzrkl_kell_pos_lag1", "hzrkl_eg_pos_lag1", "hzrkl_dg_pos_lag1", "hzrkl_kell_on_lag2", "hzrkl_eg_on_lag2", "hzrkl_dg_on_lag2", "hzrkl_kell_pos_lag2", "hzrkl_eg_pos_lag2", "hzrkl_dg_pos_lag2", "hzrkl_kell_on_lag6", "hzrkl_eg_on_lag6", "hzrkl_dg_on_lag6", "hzrkl_kell_pos_lag6", "hzrkl_eg_pos_lag6", "hzrkl_dg_pos_lag6", "hzrkl_kell_on_lag12", "hzrkl_eg_on_lag12", "hzrkl_dg_on_lag12", "hzrkl_kell_pos_lag12", "hzrkl_eg_pos_lag12", "hzrkl_dg_pos_lag12", "hzrkl_kell_on_lag24", "hzrkl_eg_on_lag24", "hzrkl_dg_on_lag24", "hzrkl_kell_pos_lag24", "hzrkl_eg_pos_lag24", "hzrkl_dg_pos_lag24", "ras_ist_min", "ras_kell_ist_d1", "ras_eg_ist_d1", "ras_dg_ist_d1", "ras_kell_ist_trend", "ras_eg_ist_trend", "ras_dg_ist_trend"], "hzrkl_kell_on": ["ras_kell_ist", "ras_eg_ist", "ras_dg_ist", "ras_kell_perc_lag1", "ras_kell_perc_lag2", "ras_kell_perc_lag6", "ras_kell_perc_lag12", "ras_kell_perc_lag24", "ras_kell_ist_lag1", "ras_kell_ist_lag2", "ras_kell_ist_lag6", "ras_kell_ist_lag12", "ras_kell_ist_lag24", "offset_kell_lag1", "offset_kell_lag2", "offset_kell_lag6", "offset_kell_lag12", "offset_kell_lag24", "ras_eg_perc_lag1", "ras_eg_perc_lag2", "ras_eg_perc_lag6", "ras_eg_perc_lag12", "ras_eg_perc_lag24", "ras_eg_ist_lag1", "ras_eg_ist_lag2", "ras_eg_ist_lag6", "ras_eg_ist_lag12", "ras_eg_ist_lag24", "offset_eg_lag1", "offset_eg_lag2", "offset_eg_lag6", "offset_eg_lag12", "offset_eg_lag24", "ras_dg_perc_lag1", "ras_dg_perc_lag2", "ras_dg_perc_lag6", "ras_dg_perc_lag12", "ras_dg_perc_lag24", "ras_dg_ist_lag1", "ras_dg_ist_lag2", "ras_dg_ist_lag6", "ras_dg_ist_lag12", "ras_dg_ist_lag24", "offset_dg_lag1", "offset_dg_lag2", "offset_dg_lag6", "offset_dg_lag12", "offset_dg_lag24", "ras_kell_ist_last", "ras_eg_ist_last", "ras_dg_ist_last", "hzrkl_kell_on_lag1", "hzrkl_eg_on_lag1", "hzrkl_dg_on_lag1", "hzrkl_kell_pos_lag1", "hzrkl_eg_pos_lag1", "hzrkl_dg_pos_lag1", "hzrkl_kell_on_lag2", "hzrkl_eg_on_lag2", "hzrkl_dg_on_lag2", "hzrkl_kell_pos_lag2", "hzrkl_eg_pos_lag2", "hzrkl_dg_pos_lag2", "hzrkl_kell_on_lag6", "hzrkl_eg_on_lag6", "hzrkl_dg_on_lag6", "hzrkl_kell_pos_lag6", "hzrkl_eg_pos_lag6", "hzrkl_dg_pos_lag6", "hzrkl_kell_on_lag12", "hzrkl_eg_on_lag12", "hzrkl_dg_on_lag12", "hzrkl_kell_pos_lag12", "hzrkl_eg_pos_lag12", "hzrkl_dg_pos_lag12", "hzrkl_kell_on_lag24", "hzrkl_eg_on_lag24", "hzrkl_dg_on_lag24", "hzrkl_kell_pos_lag24", "hzrkl_eg_pos_lag24", "hzrkl_dg_pos_lag24", "ras_ist_min", "ras_kell_ist_d1", "ras_eg_ist_d1", "ras_dg_ist_d1", "ras_kell_ist_trend", "ras_eg_ist_trend", "ras_dg_ist_trend"], "hzrkl_eg_on": ["ras_kell_ist", "ras_eg_ist", "ras_dg_ist", "ras_kell_perc_lag1", "ras_kell_perc_lag2", "ras_kell_perc_lag6", "ras_kell_perc_lag12", "ras_kell_perc_lag24", "ras_kell_ist_lag1", "ras_kell_ist_lag2", "ras_kell_ist_lag6", "ras_kell_ist_lag12", "ras_kell_ist_lag24", "offset_kell_lag1", "offset_kell_lag2", "offset_kell_lag6", "offset_kell_lag12", "offset_kell_lag24", "ras_eg_perc_lag1", "ras_eg_perc_lag2", "ras_eg_perc_lag6", "ras_eg_perc_lag12", "ras_eg_perc_lag24", "ras_eg_ist_lag1", "ras_eg_ist_lag2", "ras_eg_ist_lag6", "ras_eg_ist_lag12", "ras_eg_ist_lag24", "offset_eg_lag1", "offset_eg_lag2", "offset_eg_lag6", "offset_eg_lag12", "offset_eg_lag24", "ras_dg_perc_lag1", "ras_dg_perc_lag2", "ras_dg_perc_lag6", "ras_dg_perc_lag12", "ras_dg_perc_lag24", "ras_dg_ist_lag1", "ras_dg_ist_lag2", "ras_dg_ist_lag6", "ras_dg_ist_lag12", "ras_dg_ist_lag24", "offset_dg_lag1", "offset_dg_lag2", "offset_dg_lag6", "offset_dg_lag12", "offset_dg_lag24", "ras_kell_ist_last", "ras_eg_ist_last", "ras_dg_ist_last", "hzrkl_kell_on_lag1", "hzrkl_eg_on_lag1", "hzrkl_dg_on_lag1", "hzrkl_kell_pos_lag1", "hzrkl_eg_pos_lag1", "hzrkl_dg_pos_lag1", "hzrkl_kell_on_lag2", "hzrkl_eg_on_lag2", "hzrkl_dg_on_lag2", "hzrkl_kell_pos_lag2", "hzrkl_eg_pos_lag2", "hzrkl_dg_pos_lag2", "hzrkl_kell_on_lag6", "hzrkl_eg_on_lag6", "hzrkl_dg_on_lag6", "hzrkl_kell_pos_lag6", "hzrkl_eg_pos_lag6", "hzrkl_dg_pos_lag6", "hzrkl_kell_on_lag12", "hzrkl_eg_on_lag12", "hzrkl_dg_on_lag12", "hzrkl_kell_pos_lag12", "hzrkl_eg_pos_lag12", "hzrkl_dg_pos_lag12", "hzrkl_kell_on_lag24", "hzrkl_eg_on_lag24", "hzrkl_dg_on_lag24", "hzrkl_kell_pos_lag24", "hzrkl_eg_pos_lag24", "hzrkl_dg_pos_lag24", "ras_ist_min", "ras_kell_ist_d1", "ras_eg_ist_d1", "ras_dg_ist_d1", "ras_kell_ist_trend", "ras_eg_ist_trend", "ras_dg_ist_trend"], "hzrkl_dg_on": ["ras_kell_ist", "ras_eg_ist", "ras_dg_ist", "ras_kell_perc_lag1", "ras_kell_perc_lag2", "ras_kell_perc_lag6", "ras_kell_perc_lag12", "ras_kell_perc_lag24", "ras_kell_ist_lag1", "ras_kell_ist_lag2", "ras_kell_ist_lag6", "ras_kell_ist_lag12", "ras_kell_ist_lag24", "offset_kell_lag1", "offset_kell_lag2", "offset_kell_lag6", "offset_kell_lag12", "offset_kell_lag24", "ras_eg_perc_lag1", "ras_eg_perc_lag2", "ras_eg_perc_lag6", "ras_eg_perc_lag12", "ras_eg_perc_lag24", "ras_eg_ist_lag1", "ras_eg_ist_lag2", "ras_eg_ist_lag6", "ras_eg_ist_lag12", "ras_eg_ist_lag24", "offset_eg_lag1", "offset_eg_lag2", "offset_eg_lag6", "offset_eg_lag12", "offset_eg_lag24", "ras_dg_perc_lag1", "ras_dg_perc_lag2", "ras_dg_perc_lag6", "ras_dg_perc_lag12", "ras_dg_perc_lag24", "ras_dg_ist_lag1", "ras_dg_ist_lag2", "ras_dg_ist_lag6", "ras_dg_ist_lag12", "ras_dg_ist_lag24", "offset_dg_lag1", "offset_dg_lag2", "offset_dg_lag6", "offset_dg_lag12", "offset_dg_lag24", "ras_kell_ist_last", "ras_eg_ist_last", "ras_dg_ist_last", "hzrkl_kell_on_lag1", "hzrkl_eg_on_lag1", "hzrkl_dg_on_lag1", "hzrkl_kell_pos_lag1", "hzrkl_eg_pos_lag1", "hzrkl_dg_pos_lag1", "hzrkl_kell_on_lag2", "hzrkl_eg_on_lag2", "hzrkl_dg_on_lag2", "hzrkl_kell_pos_lag2", "hzrkl_eg_pos_lag2", "hzrkl_dg_pos_lag2", "hzrkl_kell_on_lag6", "hzrkl_eg_on_lag6", "hzrkl_dg_on_lag6", "hzrkl_kell_pos_lag6", "hzrkl_eg_pos_lag6", "hzrkl_dg_pos_lag6", "hzrkl_kell_on_lag12", "hzrkl_eg_on_lag12", "hzrkl_dg_on_lag12", "hzrkl_kell_pos_lag12", "hzrkl_eg_pos_lag12", "hzrkl_dg_pos_lag12", "hzrkl_kell_on_lag24", "hzrkl_eg_on_lag24", "hzrkl_dg_on_lag24", "hzrkl_kell_pos_lag24", "hzrkl_eg_pos_lag24", "hzrkl_dg_pos_lag24", "ras_ist_min", "ras_kell_ist_d1", "ras_eg_ist_d1", "ras_dg_ist_d1", "ras_kell_ist_trend", "ras_eg_ist_trend", "ras_dg_ist_trend"]}, "mono_tolerance": 1.02, "lgbm_params_analog": {"objective": "regression_l1", "metric": "l1", "learning_rate": 0.05, "num_leaves": 31, "min_child_samples": 50, "feature_fraction": 0.5, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0, "max_bin": 255, "seed": 20261031, "deterministic": true, "force_row_wise": true, "num_threads": 16, "verbosity": -1}, "lgbm_num_boost": 3000, "keras_hidden": [256, 128], "keras_epochs_max": 150}
