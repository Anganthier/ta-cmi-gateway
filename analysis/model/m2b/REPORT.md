# M2b-Report — L1-Modell v003 (`cmi-brain-v003`)

Erzeugt 2026-11-01T13:33:58 · git `110d682` · Daten `m1.3` · Features `m2b-f3` (574, davon 507 Rohspalten, 86 neu ggb. v002) · Seed 20261031

Doku/Entscheidungen: `docs/l1-model-v003.md` · Artefakt: `models/cmi-brain/v003/` · Referenz: `cmi-brain-v002` (Daten `m1.2`, Skalierung {'flue': 10.0})

## 1. Ergebnis in einem Satz

Gewinner nach Q-101-Regel: **lgbm** (mittlerer Skill vs. Persistence B0 auf Valid: lgbm 0.324, lgbm_plain 0.324, keras 0.182). Deploy-LightGBM: `refit_train_valid`.

## 2. DoD (Valid 2025, +4 h)

| Modell | RAS-Ist < B0 und < 1,0 K | Puffer < 1,5 K | keine Explosion (max. abs. Vorhersage) | Responsivität |
|---|---|---|---|---|
| lgbm | ✅ | ❌ | ✅ (310.7) | ✅ |
| lgbm_plain | ✅ | ❌ | ✅ (310.7) | ✅ |
| keras | ❌ | ❌ | ✅ (341.9) | ❌ |

## 3. Vergleich +4 h: v003 vs. v002 vs. B0 (Valid 2025 / Test 2026)

v003/v002 = Bewertungsmodell (Train bis 2024, Early-Stopping Valid); Refit = Deploy-Modell (Train+Valid, nur Test out-of-sample). B0 = Persistence auf den v003-Zeilen. Δ = Änderung v003 ggb. v002 (negativ = besser). v002-flue ×10.

| Target | Metrik | v003 | v003-Refit (Test) | v002 | v002-Refit (Test) | B0 | Δ Valid | Δ Test (Refit) | v001 (Rollout) |
|---|---|---|---|---|---|---|---|---|---|
| ras_kell_ist | MAE | 0.197 / 0.188 | 0.221 | 0.197 / 0.198 | 0.221 | 0.202 / 0.184 | -0.3 % | +0.3 % | 0.421 / 0.717 |
| ras_eg_ist | MAE | 0.140 / 0.133 | 0.141 | 0.141 / 0.132 | 0.141 | 0.142 / 0.129 | -0.6 % | +0.0 % | 0.194 / 0.444 |
| ras_dg_ist | MAE | 0.204 / 0.257 | 0.241 | 0.204 / 0.254 | 0.242 | 0.309 / 0.377 | +0.3 % | -0.5 % | 0.270 / 0.545 |
| ras_kell_perc | MAE | 0.240 / 0.189 | 0.192 | 0.245 / 0.196 | 0.209 | 0.248 / 0.195 | -2.4 % | -8.2 % | – / – |
| ras_eg_perc | MAE | 0.150 / 0.144 | 0.154 | 0.150 / 0.144 | 0.154 | 0.152 / 0.142 | -0.4 % | +0.1 % | – / – |
| ras_dg_perc | MAE | 0.212 / 0.262 | 0.249 | 0.208 / 0.257 | 0.244 | 0.317 / 0.381 | +1.5 % | +2.2 % | – / – |
| hz_vl | MAE | 3.038 / 4.480 | 4.127 | 3.133 / 4.425 | 4.081 | 3.155 / 4.481 | -3.0 % | +1.1 % | – / – |
| hz_rl | MAE | 2.062 / 3.182 | 2.991 | 2.170 / 3.255 | 3.042 | 2.637 / 3.593 | -5.0 % | -1.6 % | – / – |
| buff_top | MAE | 1.363 / 1.535 | 1.339 | 1.369 / 1.505 | 1.383 | 2.882 / 2.710 | -0.5 % | -3.2 % | 2.27 / 2.08 |
| buff_mid | MAE | 1.677 / 2.555 | 2.542 | 1.688 / 2.763 | 2.747 | 3.268 / 4.053 | -0.7 % | -7.4 % | 2.05 / 3.38 |
| buff_bot | MAE | 1.831 / 2.730 | 2.750 | 1.856 / 2.791 | 2.835 | 3.144 / 4.029 | -1.3 % | -3.0 % | – / – |
| ww | MAE | 4.382 / 4.217 | 4.122 | 4.461 / 4.694 | 4.499 | 6.785 / 6.646 | -1.8 % | -8.4 % | – / – |
| solar_coll | MAE | 2.909 / 3.250 | 3.086 | 2.883 / 3.233 | 2.997 | 15.260 / 17.733 | +0.9 % | +3.0 % | – / – |
| wood_vl | MAE | 0.206 / 0.621 | 0.603 | 0.203 / 0.632 | 0.611 | 0.304 / 0.851 | +1.4 % | -1.2 % | – / – |
| flue | MAE | 1.722 / 4.590 | 4.605 | 1.760 / 4.637 | 4.623 | 2.165 / 5.984 | -2.2 % | -0.4 % | – / – |
| oil_vl | MAE | 3.675 / 5.293 | 5.119 | 3.570 / 5.114 | 5.084 | 5.308 / 6.222 | +3.0 % | +0.7 % | – / – |
| hzrkl_kell_pos | MAE | 2.032 / 2.861 | 2.757 | 2.047 / 2.866 | 2.734 | 2.065 / 2.934 | -0.7 % | +0.8 % | – / – |
| hzrkl_eg_pos | MAE | 2.513 / 4.262 | 3.996 | 2.547 / 4.270 | 4.007 | 2.559 / 4.308 | -1.3 % | -0.3 % | – / – |
| hzrkl_dg_pos | MAE | 2.307 / 3.911 | 3.625 | 2.343 / 4.045 | 3.647 | 2.349 / 4.103 | -1.5 % | -0.6 % | – / – |
| oel_burner_on | RMSE | 0.185 / 0.209 | 0.162 | 0.211 / 0.216 | 0.170 | 0.213 / 0.205 | -12.2 % | -4.9 % | – / – |
| pump_hz_on | RMSE | 0.370 / 0.395 | 0.343 | 0.370 / 0.391 | 0.345 | 0.363 / 0.437 | -0.2 % | -0.6 % | – / – |
| pump_ww_on | RMSE | 0.118 / 0.077 | 0.076 | 0.119 / 0.080 | 0.077 | 0.187 / 0.118 | -0.7 % | -1.9 % | – / – |
| mix_open_on | RMSE | 0.025 / 0.032 | 0.032 | 0.025 / 0.032 | 0.031 | 0.032 / 0.042 | -0.5 % | +1.9 % | – / – |
| mix_close_on | RMSE | 0.120 / 0.105 | 0.102 | 0.121 / 0.105 | 0.102 | 0.152 / 0.139 | -0.8 % | +0.2 % | – / – |
| pump_ww_oil_on | RMSE | 0.170 / 0.168 | 0.146 | 0.189 / 0.241 | 0.205 | 0.217 / 0.198 | -10.3 % | -28.8 % | – / – |
| pump_circ_on | RMSE | 0.172 / 0.155 | 0.147 | 0.191 / 0.199 | 0.176 | 0.222 / 0.204 | -10.1 % | -16.6 % | – / – |
| valve_hzrl_on | RMSE | 0.269 / 0.289 | 0.318 | 0.286 / 0.367 | 0.405 | 0.333 / 0.095 | -6.0 % | -21.6 % | – / – |
| pump_solar_on | RMSE | 0.109 / 0.123 | 0.116 | 0.110 / 0.124 | 0.118 | 0.365 / 0.389 | -0.7 % | -1.2 % | – / – |
| hzrkl_kell_on | RMSE | 0.293 / 0.326 | 0.304 | 0.299 / 0.327 | 0.310 | 0.296 / 0.301 | -1.9 % | -1.8 % | – / – |
| hzrkl_eg_on | RMSE | 0.325 / 0.404 | 0.371 | 0.323 / 0.400 | 0.368 | 0.340 / 0.378 | +0.6 % | +1.0 % | – / – |
| hzrkl_dg_on | RMSE | 0.310 / 0.345 | 0.339 | 0.310 / 0.345 | 0.342 | 0.337 / 0.407 | -0.0 % | -0.7 % | – / – |

## 4. Alle Horizonte kompakt (v003 · v002 · B0; Valid | Test)

| Target | Metrik | +1 h Valid | +1 h Test | +4 h Valid | +4 h Test | +6 h Valid | +6 h Test | +12 h Valid | +12 h Test |
|---|---|---|---|---|---|---|---|---|---|
| ras_kell_ist | MAE | 0.059 · 0.059 · 0.127 | 0.060 · 0.059 · 0.103 | 0.197 · 0.197 · 0.202 | 0.188 · 0.198 · 0.184 | 0.261 · 0.262 · 0.265 | 0.239 · 0.237 · 0.228 | 0.355 · 0.354 · 0.359 | 0.334 · 0.345 · 0.317 |
| ras_eg_ist | MAE | 0.064 · 0.064 · 0.100 | 0.040 · 0.040 · 0.059 | 0.140 · 0.141 · 0.142 | 0.133 · 0.132 · 0.129 | 0.169 · 0.169 · 0.170 | 0.169 · 0.169 · 0.168 | 0.216 · 0.215 · 0.219 | 0.252 · 0.251 · 0.251 |
| ras_dg_ist | MAE | 0.050 · 0.050 · 0.097 | 0.055 · 0.055 · 0.114 | 0.204 · 0.204 · 0.309 | 0.257 · 0.254 · 0.377 | 0.267 · 0.274 · 0.416 | 0.353 · 0.350 · 0.507 | 0.369 · 0.372 · 0.609 | 0.498 · 0.494 · 0.755 |
| ras_kell_perc | MAE | 0.079 · 0.078 · 0.148 | 0.055 · 0.055 · 0.107 | 0.240 · 0.245 · 0.248 | 0.189 · 0.196 · 0.195 | 0.308 · 0.315 · 0.318 | 0.300 · 0.248 · 0.241 | 0.374 · 0.377 · 0.409 | 0.354 · 0.344 · 0.334 |
| ras_eg_perc | MAE | 0.067 · 0.067 · 0.104 | 0.044 · 0.043 · 0.065 | 0.150 · 0.150 · 0.152 | 0.144 · 0.144 · 0.142 | 0.182 · 0.182 · 0.182 | 0.185 · 0.184 · 0.185 | 0.233 · 0.230 · 0.237 | 0.273 · 0.270 · 0.274 |
| ras_dg_perc | MAE | 0.052 · 0.052 · 0.099 | 0.056 · 0.056 · 0.115 | 0.212 · 0.208 · 0.317 | 0.262 · 0.257 · 0.381 | 0.275 · 0.277 · 0.426 | 0.348 · 0.347 · 0.512 | 0.374 · 0.379 · 0.618 | 0.500 · 0.492 · 0.757 |
| hz_vl | MAE | 1.161 · 1.167 · 2.087 | 1.447 · 1.470 · 2.597 | 3.038 · 3.133 · 3.155 | 4.480 · 4.425 · 4.481 | 3.641 · 3.738 · 3.760 | 5.034 · 5.119 · 5.164 | 2.711 · 2.745 · 4.661 | 4.551 · 4.571 · 6.377 |
| hz_rl | MAE | 0.802 · 0.795 · 1.865 | 1.083 · 1.056 · 2.141 | 2.062 · 2.170 · 2.637 | 3.182 · 3.255 · 3.593 | 2.464 · 2.569 · 3.064 | 3.827 · 3.830 · 4.098 | 2.274 · 2.301 · 3.653 | 3.758 · 3.809 · 5.065 |
| buff_top | MAE | 0.380 · 0.397 · 1.212 | 0.367 · 0.396 · 0.961 | 1.363 · 1.369 · 2.882 | 1.535 · 1.505 · 2.710 | 1.602 · 1.618 · 3.242 | 1.843 · 1.869 · 3.221 | 1.968 · 1.964 · 3.692 | 2.331 · 2.349 · 3.933 |
| buff_mid | MAE | 0.348 · 0.358 · 0.998 | 0.574 · 0.622 · 1.381 | 1.677 · 1.688 · 3.268 | 2.555 · 2.763 · 4.053 | 2.215 · 2.242 · 4.375 | 3.268 · 3.493 · 5.295 | 2.804 · 2.810 · 6.071 | 4.389 · 4.478 · 7.206 |
| buff_bot | MAE | 0.457 · 0.461 · 1.078 | 0.744 · 0.784 · 1.505 | 1.831 · 1.856 · 3.144 | 2.730 · 2.791 · 4.029 | 2.287 · 2.324 · 4.108 | 3.374 · 3.454 · 5.176 | 2.775 · 2.713 · 5.566 | 4.395 · 4.441 · 6.884 |
| ww | MAE | 2.481 · 2.483 · 3.754 | 2.402 · 2.447 · 3.569 | 4.382 · 4.461 · 6.785 | 4.217 · 4.694 · 6.646 | 4.462 · 4.533 · 7.814 | 4.357 · 4.839 · 7.910 | 4.730 · 4.733 · 9.633 | 4.599 · 4.617 · 9.754 |
| solar_coll | MAE | 1.306 · 1.300 · 4.408 | 1.504 · 1.504 · 5.118 | 2.909 · 2.883 · 15.260 | 3.250 · 3.233 · 17.733 | 3.019 · 3.029 · 21.179 | 3.439 · 3.428 · 24.800 | 3.175 · 3.113 · 29.316 | 3.516 · 3.472 · 34.939 |
| wood_vl | MAE | 0.038 · 0.040 · 0.088 | 0.110 · 0.112 · 0.251 | 0.206 · 0.203 · 0.304 | 0.621 · 0.632 · 0.851 | 0.327 · 0.331 · 0.429 | 0.945 · 0.985 · 1.175 | 0.588 · 0.603 · 0.707 | 1.620 · 1.661 · 1.760 |
| flue | MAE | 0.735 · 0.746 · 1.045 | 1.878 · 1.905 · 2.678 | 1.722 · 1.760 · 2.165 | 4.590 · 4.637 · 5.984 | 1.951 · 2.003 · 2.484 | 5.297 · 5.389 · 7.245 | 2.378 · 2.489 · 3.153 | 5.666 · 5.792 · 7.142 |
| oil_vl | MAE | 1.628 · 1.599 · 3.004 | 1.828 · 1.808 · 3.040 | 3.675 · 3.570 · 5.308 | 5.293 · 5.114 · 6.222 | 4.064 · 4.127 · 5.857 | 6.476 · 6.329 · 7.094 | 4.507 · 4.529 · 6.716 | 7.374 · 7.172 · 8.500 |
| hzrkl_kell_pos | MAE | 0.730 · 0.727 · 1.724 | 1.112 · 1.131 · 2.329 | 2.032 · 2.047 · 2.065 | 2.861 · 2.866 · 2.934 | 2.428 · 2.470 · 2.537 | 3.226 · 3.212 · 3.307 | 2.484 · 2.494 · 2.936 | 3.217 · 3.204 · 3.868 |
| hzrkl_eg_pos | MAE | 1.046 · 1.058 · 2.265 | 1.657 · 1.719 · 3.106 | 2.513 · 2.547 · 2.559 | 4.262 · 4.270 · 4.308 | 2.831 · 2.848 · 2.853 | 4.813 · 4.866 · 4.924 | 2.602 · 2.614 · 3.290 | 4.447 · 4.376 · 5.447 |
| hzrkl_dg_pos | MAE | 0.592 · 0.583 · 1.387 | 0.885 · 0.882 · 1.914 | 2.307 · 2.343 · 2.349 | 3.911 · 4.045 · 4.103 | 2.553 · 2.577 · 2.576 | 4.178 · 4.297 · 4.328 | 2.375 · 2.365 · 2.972 | 3.907 · 3.968 · 5.301 |
| oel_burner_on | RMSE | 0.120 · 0.133 · 0.208 | 0.127 · 0.143 · 0.187 | 0.185 · 0.211 · 0.213 | 0.209 · 0.216 · 0.205 | 0.206 · 0.222 · 0.217 | 0.228 · 0.246 · 0.224 | 0.197 · 0.198 · 0.233 | 0.226 · 0.225 · 0.243 |
| pump_hz_on | RMSE | 0.243 · 0.250 · 0.318 | 0.238 · 0.243 · 0.313 | 0.370 · 0.370 · 0.363 | 0.395 · 0.391 · 0.437 | 0.392 · 0.393 · 0.373 | 0.413 · 0.410 · 0.446 | 0.322 · 0.324 · 0.384 | 0.372 · 0.373 · 0.464 |
| pump_ww_on | RMSE | 0.079 · 0.080 · 0.182 | 0.061 · 0.058 · 0.115 | 0.118 · 0.119 · 0.187 | 0.077 · 0.080 · 0.118 | 0.120 · 0.121 · 0.182 | 0.079 · 0.082 · 0.114 | 0.122 · 0.123 · 0.183 | 0.082 · 0.082 · 0.115 |
| mix_open_on | RMSE | 0.022 · 0.022 · 0.034 | 0.031 · 0.030 · 0.043 | 0.025 · 0.025 · 0.032 | 0.032 · 0.032 · 0.042 | 0.025 · 0.025 · 0.032 | 0.032 · 0.032 · 0.043 | 0.024 · 0.024 · 0.034 | 0.032 · 0.032 · 0.042 |
| mix_close_on | RMSE | 0.106 · 0.108 · 0.171 | 0.093 · 0.093 · 0.149 | 0.120 · 0.121 · 0.152 | 0.105 · 0.105 · 0.139 | 0.120 · 0.120 · 0.157 | 0.104 · 0.104 · 0.142 | 0.117 · 0.117 · 0.159 | 0.103 · 0.102 · 0.141 |
| pump_ww_oil_on | RMSE | 0.148 · 0.163 · 0.179 | 0.145 · 0.179 · 0.160 | 0.170 · 0.189 · 0.217 | 0.168 · 0.241 · 0.198 | 0.174 · 0.190 · 0.230 | 0.153 · 0.216 · 0.212 | 0.174 · 0.186 · 0.259 | 0.157 · 0.195 · 0.239 |
| pump_circ_on | RMSE | 0.149 · 0.162 · 0.183 | 0.136 · 0.155 · 0.165 | 0.172 · 0.191 · 0.222 | 0.155 · 0.199 · 0.204 | 0.176 · 0.191 · 0.236 | 0.151 · 0.189 · 0.218 | 0.178 · 0.187 · 0.265 | 0.156 · 0.177 · 0.246 |
| valve_hzrl_on | RMSE | 0.123 · 0.127 · 0.249 | 0.385 · 0.329 · 0.063 | 0.269 · 0.286 · 0.333 | 0.289 · 0.367 · 0.095 | 0.306 · 0.331 · 0.374 | 0.278 · 0.367 · 0.100 | 0.248 · 0.252 · 0.429 | 0.441 · 0.443 · 0.122 |
| pump_solar_on | RMSE | 0.088 · 0.089 · 0.173 | 0.094 · 0.096 · 0.179 | 0.109 · 0.110 · 0.365 | 0.123 · 0.124 · 0.389 | 0.111 · 0.111 · 0.423 | 0.125 · 0.125 · 0.448 | 0.110 · 0.112 · 0.449 | 0.128 · 0.129 · 0.466 |
| hzrkl_kell_on | RMSE | 0.160 · 0.161 · 0.280 | 0.177 · 0.179 · 0.277 | 0.293 · 0.299 · 0.296 | 0.326 · 0.327 · 0.301 | 0.316 · 0.324 · 0.328 | 0.336 · 0.349 · 0.315 | 0.336 · 0.321 · 0.349 | 0.322 · 0.308 · 0.325 |
| hzrkl_eg_on | RMSE | 0.198 · 0.205 · 0.329 | 0.219 · 0.254 · 0.293 | 0.325 · 0.323 · 0.340 | 0.404 · 0.400 · 0.378 | 0.329 · 0.341 · 0.358 | 0.414 · 0.424 · 0.410 | 0.344 · 0.343 · 0.377 | 0.446 · 0.445 · 0.437 |
| hzrkl_dg_on | RMSE | 0.134 · 0.135 · 0.199 | 0.131 · 0.132 · 0.233 | 0.310 · 0.310 · 0.337 | 0.345 · 0.345 · 0.407 | 0.319 · 0.317 · 0.342 | 0.360 · 0.361 · 0.419 | 0.339 · 0.338 · 0.346 | 0.373 · 0.373 · 0.449 |

## 5. v003 LightGBM: alle Targets × Horizonte (Valid / Test, Skill vs. B0 Valid)

| Target | Metrik | +1 h | +4 h | +6 h | +12 h |
|---|---|---|---|---|---|
| ras_kell_ist | MAE | 0.059 / 0.060 (S 0.53) | 0.197 / 0.188 (S 0.02) | 0.261 / 0.239 (S 0.01) | 0.355 / 0.334 (S 0.01) |
| ras_eg_ist | MAE | 0.064 / 0.040 (S 0.36) | 0.140 / 0.133 (S 0.02) | 0.169 / 0.169 (S 0.00) | 0.216 / 0.252 (S 0.01) |
| ras_dg_ist | MAE | 0.050 / 0.055 (S 0.48) | 0.204 / 0.257 (S 0.34) | 0.267 / 0.353 (S 0.36) | 0.369 / 0.498 (S 0.39) |
| ras_kell_perc | MAE | 0.079 / 0.055 (S 0.47) | 0.240 / 0.189 (S 0.04) | 0.308 / 0.300 (S 0.03) | 0.374 / 0.354 (S 0.09) |
| ras_eg_perc | MAE | 0.067 / 0.044 (S 0.36) | 0.150 / 0.144 (S 0.01) | 0.182 / 0.185 (S 0.00) | 0.233 / 0.273 (S 0.02) |
| ras_dg_perc | MAE | 0.052 / 0.056 (S 0.47) | 0.212 / 0.262 (S 0.33) | 0.275 / 0.348 (S 0.35) | 0.374 / 0.500 (S 0.39) |
| hz_vl | MAE | 1.161 / 1.447 (S 0.44) | 3.038 / 4.480 (S 0.04) | 3.641 / 5.034 (S 0.03) | 2.711 / 4.551 (S 0.42) |
| hz_rl | MAE | 0.802 / 1.083 (S 0.57) | 2.062 / 3.182 (S 0.22) | 2.464 / 3.827 (S 0.20) | 2.274 / 3.758 (S 0.38) |
| buff_top | MAE | 0.380 / 0.367 (S 0.69) | 1.363 / 1.535 (S 0.53) | 1.602 / 1.843 (S 0.51) | 1.968 / 2.331 (S 0.47) |
| buff_mid | MAE | 0.348 / 0.574 (S 0.65) | 1.677 / 2.555 (S 0.49) | 2.215 / 3.268 (S 0.49) | 2.804 / 4.389 (S 0.54) |
| buff_bot | MAE | 0.457 / 0.744 (S 0.58) | 1.831 / 2.730 (S 0.42) | 2.287 / 3.374 (S 0.44) | 2.775 / 4.395 (S 0.50) |
| ww | MAE | 2.481 / 2.402 (S 0.34) | 4.382 / 4.217 (S 0.35) | 4.462 / 4.357 (S 0.43) | 4.730 / 4.599 (S 0.51) |
| solar_coll | MAE | 1.306 / 1.504 (S 0.70) | 2.909 / 3.250 (S 0.81) | 3.019 / 3.439 (S 0.86) | 3.175 / 3.516 (S 0.89) |
| wood_vl | MAE | 0.038 / 0.110 (S 0.57) | 0.206 / 0.621 (S 0.32) | 0.327 / 0.945 (S 0.24) | 0.588 / 1.620 (S 0.17) |
| flue | MAE | 0.735 / 1.878 (S 0.30) | 1.722 / 4.590 (S 0.20) | 1.951 / 5.297 (S 0.21) | 2.378 / 5.666 (S 0.25) |
| oil_vl | MAE | 1.628 / 1.828 (S 0.46) | 3.675 / 5.293 (S 0.31) | 4.064 / 6.476 (S 0.31) | 4.507 / 7.374 (S 0.33) |
| hzrkl_kell_pos | MAE | 0.730 / 1.112 (S 0.58) | 2.032 / 2.861 (S 0.02) | 2.428 / 3.226 (S 0.04) | 2.484 / 3.217 (S 0.15) |
| hzrkl_eg_pos | MAE | 1.046 / 1.657 (S 0.54) | 2.513 / 4.262 (S 0.02) | 2.831 / 4.813 (S 0.01) | 2.602 / 4.447 (S 0.21) |
| hzrkl_dg_pos | MAE | 0.592 / 0.885 (S 0.57) | 2.307 / 3.911 (S 0.02) | 2.553 / 4.178 (S 0.01) | 2.375 / 3.907 (S 0.20) |
| oel_burner_on | RMSE | 0.120 / 0.127 (S 0.42) | 0.185 / 0.209 (S 0.13) | 0.206 / 0.228 (S 0.05) | 0.197 / 0.226 (S 0.15) |
| pump_hz_on | RMSE | 0.243 / 0.238 (S 0.24) | 0.370 / 0.395 (S -0.02) | 0.392 / 0.413 (S -0.05) | 0.322 / 0.372 (S 0.16) |
| pump_ww_on | RMSE | 0.079 / 0.061 (S 0.57) | 0.118 / 0.077 (S 0.37) | 0.120 / 0.079 (S 0.34) | 0.122 / 0.082 (S 0.33) |
| mix_open_on | RMSE | 0.022 / 0.031 (S 0.36) | 0.025 / 0.032 (S 0.22) | 0.025 / 0.032 (S 0.23) | 0.024 / 0.032 (S 0.28) |
| mix_close_on | RMSE | 0.106 / 0.093 (S 0.38) | 0.120 / 0.105 (S 0.21) | 0.120 / 0.104 (S 0.24) | 0.117 / 0.103 (S 0.27) |
| pump_ww_oil_on | RMSE | 0.148 / 0.145 (S 0.17) | 0.170 / 0.168 (S 0.22) | 0.174 / 0.153 (S 0.24) | 0.174 / 0.157 (S 0.33) |
| pump_circ_on | RMSE | 0.149 / 0.136 (S 0.18) | 0.172 / 0.155 (S 0.23) | 0.176 / 0.151 (S 0.25) | 0.178 / 0.156 (S 0.33) |
| valve_hzrl_on | RMSE | 0.123 / 0.385 (S 0.51) | 0.269 / 0.289 (S 0.19) | 0.306 / 0.278 (S 0.18) | 0.248 / 0.441 (S 0.42) |
| pump_solar_on | RMSE | 0.088 / 0.094 (S 0.49) | 0.109 / 0.123 (S 0.70) | 0.111 / 0.125 (S 0.74) | 0.110 / 0.128 (S 0.75) |
| hzrkl_kell_on | RMSE | 0.160 / 0.177 (S 0.43) | 0.293 / 0.326 (S 0.01) | 0.316 / 0.336 (S 0.04) | 0.336 / 0.322 (S 0.04) |
| hzrkl_eg_on | RMSE | 0.198 / 0.219 (S 0.40) | 0.325 / 0.404 (S 0.04) | 0.329 / 0.414 (S 0.08) | 0.344 / 0.446 (S 0.09) |
| hzrkl_dg_on | RMSE | 0.134 / 0.131 (S 0.33) | 0.310 / 0.345 (S 0.08) | 0.319 / 0.360 (S 0.07) | 0.339 / 0.373 (S 0.02) |

S = Skill = 1 − Fehler/Fehler_B0 (> 0 besser als Persistence). Analog: MAE in K; Digital (An-Quotient 0..1): RMSE (Brier-artig, s. `evaluate.py`).

### 5b. Keras v2 (Referenz), +4 h, Valid / Test

| Target | Metrik | Keras v2 | LightGBM | B0 |
|---|---|---|---|---|
| ras_kell_ist | MAE | 0.211 / 0.257 | 0.197 / 0.188 | 0.202 / 0.184 |
| ras_eg_ist | MAE | 0.167 / 0.197 | 0.140 / 0.133 | 0.142 / 0.129 |
| ras_dg_ist | MAE | 0.223 / 0.277 | 0.204 / 0.257 | 0.309 / 0.377 |
| ras_kell_perc | MAE | 0.262 / 0.259 | 0.240 / 0.189 | 0.248 / 0.195 |
| ras_eg_perc | MAE | 0.177 / 0.211 | 0.150 / 0.144 | 0.152 / 0.142 |
| ras_dg_perc | MAE | 0.230 / 0.283 | 0.212 / 0.262 | 0.317 / 0.381 |
| hz_vl | MAE | 3.169 / 5.209 | 3.038 / 4.480 | 3.155 / 4.481 |
| hz_rl | MAE | 2.366 / 3.800 | 2.062 / 3.182 | 2.637 / 3.593 |
| buff_top | MAE | 2.075 / 1.954 | 1.363 / 1.535 | 2.882 / 2.710 |
| buff_mid | MAE | 1.850 / 2.770 | 1.677 / 2.555 | 3.268 / 4.053 |
| buff_bot | MAE | 1.977 / 2.813 | 1.831 / 2.730 | 3.144 / 4.029 |
| ww | MAE | 5.187 / 5.061 | 4.382 / 4.217 | 6.785 / 6.646 |
| solar_coll | MAE | 5.573 / 6.905 | 2.909 / 3.250 | 15.260 / 17.733 |
| wood_vl | MAE | 0.283 / 0.722 | 0.206 / 0.621 | 0.304 / 0.851 |
| flue | MAE | 2.062 / 5.225 | 1.722 / 4.590 | 2.165 / 5.984 |
| oil_vl | MAE | 4.442 / 6.121 | 3.675 / 5.293 | 5.308 / 6.222 |
| hzrkl_kell_pos | MAE | 2.243 / 3.214 | 2.032 / 2.861 | 2.065 / 2.934 |
| hzrkl_eg_pos | MAE | 2.704 / 4.587 | 2.513 / 4.262 | 2.559 / 4.308 |
| hzrkl_dg_pos | MAE | 2.556 / 4.018 | 2.307 / 3.911 | 2.349 / 4.103 |
| oel_burner_on | RMSE | 0.178 / 0.203 | 0.185 / 0.209 | 0.213 / 0.205 |
| pump_hz_on | RMSE | 0.331 / 0.376 | 0.370 / 0.395 | 0.363 / 0.437 |
| pump_ww_on | RMSE | 0.128 / 0.082 | 0.118 / 0.077 | 0.187 / 0.118 |
| mix_open_on | RMSE | 0.025 / 0.033 | 0.025 / 0.032 | 0.032 / 0.042 |
| mix_close_on | RMSE | 0.128 / 0.111 | 0.120 / 0.105 | 0.152 / 0.139 |
| pump_ww_oil_on | RMSE | 0.161 / 0.167 | 0.170 / 0.168 | 0.217 / 0.198 |
| pump_circ_on | RMSE | 0.165 / 0.166 | 0.172 / 0.155 | 0.222 / 0.204 |
| valve_hzrl_on | RMSE | 0.268 / 0.363 | 0.269 / 0.289 | 0.333 / 0.095 |
| pump_solar_on | RMSE | 0.154 / 0.166 | 0.109 / 0.123 | 0.365 / 0.389 |
| hzrkl_kell_on | RMSE | 0.260 / 0.291 | 0.293 / 0.326 | 0.296 / 0.301 |
| hzrkl_eg_on | RMSE | 0.294 / 0.351 | 0.325 / 0.404 | 0.340 / 0.378 |
| hzrkl_dg_on | RMSE | 0.278 / 0.323 | 0.310 / 0.345 | 0.337 / 0.407 |

## 6. Offset-Responsivität v2 (je Etage, Heizsaison-Fälle)

Eingriff: Offset + perzipierte T der Etage um Δ ∈ {1; 2; 3,4; 5} K (v003 zusätzlich Minderer-Regel `hzrkl_{z}_rule` neu aus perzipiert_last + Δ). Wirkung = mittlere Änderung der Vorhersage über +1/+4/+6 h. DoD-Größe = Rücklaufminderer der Etage (`hzrkl_{z}_on`), DoD-Zone = **eg** (Q-110). Werte: Pearson(Δ, Wirkung) / Anteil Wirkung < 0 bei +3,4 K.

| Modell (Fälle) | Zone | Ventil an (DoD) | Minderer-Temp. (`_pos`, °C) | Pumpe d8 | RAS-Ist +6 h | bestanden | v002 (DoD-Größe) |
|---|---|---|---|---|---|---|---|
| lgbm (valid) | eg (DoD) | -0.61 / 0.99 | -0.01 / 0.99 | -0.19 / 1.00 | -0.00 / 0.12 | ✅ | -0.73 / 0.99 |
| lgbm (valid) | dg | -0.26 / 0.98 | -0.03 / 0.92 | -0.30 / 0.99 | -0.31 / 1.00 | ❌ | -0.27 / 0.98 |
| lgbm (valid) | kell | -0.11 / 0.98 | -0.02 / 0.81 | -0.48 / 0.98 | -0.02 / 0.39 | ❌ | -0.12 / 0.98 |
| lgbm_plain (valid) | eg (DoD) | -0.69 / 0.99 | -0.01 / 0.99 | -0.17 / 0.94 | -0.00 / 0.21 | ✅ | -0.73 / 0.99 |
| lgbm_plain (valid) | dg | -0.25 / 0.98 | -0.03 / 0.92 | -0.28 / 0.96 | -0.19 / 1.00 | ❌ | -0.25 / 0.97 |
| lgbm_plain (valid) | kell | -0.13 / 0.98 | -0.02 / 0.81 | -0.39 / 0.84 | -0.02 / 0.64 | ❌ | -0.14 / 0.98 |
| keras (valid) | eg (DoD) | -0.40 / 1.00 | -0.41 / 0.98 | -0.41 / 0.97 | -0.55 / 1.00 | ❌ | -0.71 / 0.98 |
| keras (valid) | dg | -0.46 / 0.98 | -0.49 / 0.96 | -0.41 / 0.83 | -0.58 / 1.00 | ❌ | -0.80 / 0.98 |
| keras (valid) | kell | -0.29 / 0.98 | -0.41 / 0.96 | 0.12 / 0.47 | -0.48 / 1.00 | ❌ | -0.56 / 1.00 |
| lgbm_refit@test (test) | eg (DoD) | -0.29 / 1.00 | -0.09 / 0.97 | -0.30 / 0.99 | -0.10 / 0.90 | ❌ | -0.34 / 1.00 |
| lgbm_refit@test (test) | dg | -0.22 / 0.99 | -0.04 / 0.97 | -0.15 / 0.99 | -0.32 / 0.98 | ❌ | -0.21 / 1.00 |
| lgbm_refit@test (test) | kell | -0.20 / 0.98 | -0.10 / 0.90 | -0.20 / 0.99 | -0.16 / 0.85 | ❌ | -0.20 / 0.98 |
| lgbm@test (test) | eg (DoD) | -0.44 / 1.00 | -0.03 / 0.93 | -0.20 / 0.99 | -0.00 / 0.10 | ❌ | -0.55 / 1.00 |
| lgbm@test (test) | dg | -0.27 / 1.00 | -0.04 / 0.88 | -0.28 / 0.99 | -0.31 / 0.98 | ❌ | -0.25 / 1.00 |
| lgbm@test (test) | kell | -0.21 / 0.98 | -0.04 / 0.92 | -0.43 / 0.98 | -0.07 / 0.24 | ❌ | -0.22 / 0.98 |

## 7. Monotonie-Varianten (LightGBM, Valid-Fehler frei vs. monoton)

| Target@h | frei | monoton | gewählt |
|---|---|---|---|
| ras_kell_ist@1 | 0.0585 | 0.0590 | monoton |
| ras_kell_ist@4 | 0.1939 | 0.1969 | monoton |
| ras_kell_ist@6 | 0.2591 | 0.2615 | monoton |
| ras_kell_ist@12 | 0.3534 | 0.3550 | monoton |
| ras_eg_ist@1 | 0.0648 | 0.0639 | monoton |
| ras_eg_ist@4 | 0.1394 | 0.1400 | monoton |
| ras_eg_ist@6 | 0.1692 | 0.1694 | monoton |
| ras_eg_ist@12 | 0.2172 | 0.2163 | monoton |
| ras_dg_ist@1 | 0.0501 | 0.0545 | frei |
| ras_dg_ist@4 | 0.2010 | 0.2045 | monoton |
| ras_dg_ist@6 | 0.2656 | 0.2672 | monoton |
| ras_dg_ist@12 | 0.3694 | 0.3821 | frei |
| ras_kell_perc@1 | 0.0787 | 0.0821 | frei |
| ras_kell_perc@4 | 0.2395 | 0.2463 | frei |
| ras_kell_perc@6 | 0.3082 | 0.3161 | frei |
| ras_kell_perc@12 | 0.3745 | 0.3911 | frei |
| ras_eg_perc@1 | 0.0676 | 0.0666 | monoton |
| ras_eg_perc@4 | 0.1488 | 0.1497 | monoton |
| ras_eg_perc@6 | 0.1817 | 0.1818 | monoton |
| ras_eg_perc@12 | 0.2354 | 0.2327 | monoton |
| ras_dg_perc@1 | 0.0521 | 0.0564 | frei |
| ras_dg_perc@4 | 0.2082 | 0.2116 | monoton |
| ras_dg_perc@6 | 0.2733 | 0.2754 | monoton |
| ras_dg_perc@12 | 0.3742 | 0.3910 | frei |
| pump_hz_on@1 | 0.2323 | 0.2426 | monoton |
| pump_hz_on@4 | 0.3644 | 0.3696 | monoton |
| pump_hz_on@6 | 0.3887 | 0.3916 | monoton |
| pump_hz_on@12 | 0.3194 | 0.3216 | monoton |
| hzrkl_kell_on@1 | 0.1607 | 0.1598 | monoton |
| hzrkl_kell_on@4 | 0.2979 | 0.2929 | monoton |
| hzrkl_kell_on@6 | 0.3202 | 0.3162 | monoton |
| hzrkl_kell_on@12 | 0.3202 | 0.3359 | monoton |
| hzrkl_eg_on@1 | 0.2014 | 0.1977 | monoton |
| hzrkl_eg_on@4 | 0.3307 | 0.3248 | monoton |
| hzrkl_eg_on@6 | 0.3384 | 0.3294 | monoton |
| hzrkl_eg_on@12 | 0.3448 | 0.3440 | monoton |
| hzrkl_dg_on@1 | 0.1334 | 0.1342 | monoton |
| hzrkl_dg_on@4 | 0.3111 | 0.3101 | monoton |
| hzrkl_dg_on@6 | 0.3186 | 0.3191 | monoton |
| hzrkl_dg_on@12 | 0.3390 | 0.3386 | monoton |

## 8. Feature-Importance (+4 h, Gain-Anteil, Bewertungsmodell)

### 8a. Neue v003-Merkmale (Rohspalten m1.3 + Ableitungen m2b-f3)

- **`buff_top@4`** — Anteil neue Merkmale gesamt 0.205: uvr_ww_margin 0.0718, buff_strat_top_mid 0.015, uvr_ww_margin_last 0.0131, pump_ww_on_since_h 0.0098, pump_ww_on_sum3h 0.0079, pump_circ_on_sum6h 0.0076, pump_circ_on_sum24h 0.0053, pump_circ_on_sum12h 0.0049, pump_ww_oil_on_sum24h 0.0042, buff_strat_mid_bot 0.0039
- **`buff_mid@4`** — Anteil neue Merkmale gesamt 0.209: o9_switch_margin 0.0332, buff_strat_mid_bot 0.0218, uvr_o9_margin 0.0151, uvr_o9_margin_last 0.0112, pump_solar_on_sum12h 0.0095, pump_solar_on_sum6h 0.006, buff_strat_top_mid 0.006, pump_ww_oil_on_sum6h 0.0053, pump_circ_on_sum6h 0.0051, hz_flow_o9 0.005
- **`buff_bot@4`** — Anteil neue Merkmale gesamt 0.198: o9_switch_margin 0.0268, uvr_o9_margin_last 0.0223, buff_strat_mid_bot 0.0096, pump_solar_on_sum12h 0.0094, uvr_o9_margin 0.0076, pump_ww_oil_on_sum6h 0.0073, pump_ww_oil_on_sum24h 0.007, pump_circ_on_sum24h 0.0067, pump_circ_on_sum6h 0.0062, buff_strat_top_mid 0.0047

| Target@h | Anteil neue Merkmale | Top 3 neu |
|---|---|---|
| ras_kell_ist@4 | 0.160 | hzrkl_kell_rule 0.0755, hzrkl_kell_rule_lag2 0.0237, oel_burner_on_sum24h 0.0181 |
| ras_eg_ist@4 | 0.048 | pump_hz_on 0.0091, pump_hz_on_sum24h 0.0076, hz_flow_pump 0.0059 |
| ras_dg_ist@4 | 0.096 | oel_burner_on 0.0279, hzrkl_dg_rule 0.0151, pump_hz_on_sum12h 0.006 |
| ras_kell_perc@4 | 0.176 | hzrkl_kell_rule 0.0805, pump_hz_on_sum6h 0.0203, hzrkl_kell_rule_lag1 0.0152 |
| ras_eg_perc@4 | 0.049 | pump_hz_on 0.0094, hz_flow_pump 0.0082, hzrkl_dg_rule_lag12 0.0049 |
| ras_dg_perc@4 | 0.092 | oel_burner_on 0.0254, hzrkl_dg_rule 0.0143, pump_hz_on 0.006 |
| hz_vl@4 | 0.172 | pump_hz_on_sum24h 0.0405, vl_err 0.0326, oel_burner_on_sum24h 0.0232 |
| hz_rl@4 | 0.165 | pump_hz_on_sum24h 0.0187, vl_err 0.0149, hz_flow_o9 0.0115 |
| buff_top@4 | 0.205 | uvr_ww_margin 0.0718, buff_strat_top_mid 0.015, uvr_ww_margin_last 0.0131 |
| buff_mid@4 | 0.209 | o9_switch_margin 0.0332, buff_strat_mid_bot 0.0218, uvr_o9_margin 0.0151 |
| buff_bot@4 | 0.198 | o9_switch_margin 0.0268, uvr_o9_margin_last 0.0223, buff_strat_mid_bot 0.0096 |
| ww@4 | 0.177 | pump_circ_on_sum24h 0.0361, pump_ww_oil_on_sum24h 0.0232, pump_ww_oil_on_since_h 0.0162 |
| solar_coll@4 | 0.071 | pump_solar_on_sum6h 0.0089, pump_solar_on_sum3h 0.0058, pump_solar_on_sum12h 0.0048 |
| wood_vl@4 | 0.295 | vl_err 0.0745, pump_wood_est_on_since_h 0.0277, uvr_o1_margin 0.0237 |
| flue@4 | 0.179 | pump_circ_on_sum6h 0.0122, pump_circ_on_sum24h 0.0105, pump_ww_oil_on_sum6h 0.0099 |
| oil_vl@4 | 0.198 | uvr_o2_margin 0.0324, oel_burner_on 0.0192, oel_burner_on_since_h 0.0179 |
| hzrkl_kell_pos@4 | 0.167 | pump_hz_on_sum6h 0.0663, pump_hz_on_since_h 0.0436, pump_hz_on_sum24h 0.0204 |
| hzrkl_eg_pos@4 | 0.110 | pump_hz_on_sum24h 0.0282, pump_hz_on_since_h 0.0218, hzrkl_eg_rule_last 0.0148 |
| hzrkl_dg_pos@4 | 0.157 | pump_hz_on_sum6h 0.0753, pump_hz_on_sum24h 0.0183, valve_hzrl_on_sum24h 0.0063 |
| oel_burner_on@4 | 0.520 | oel_burner_on_sum24h 0.2923, valve_hzrl_on_sum24h 0.1314, pump_hz_on_sum24h 0.0247 |
| pump_hz_on@4 | 0.157 | hz_flow_pump 0.0952, pump_hz_on 0.0498, uvr_o1_margin 0.0028 |
| pump_ww_on@4 | 0.203 | pump_ww_on_sum24h 0.0415, uvr_ww_margin 0.0142, pump_ww_on_sum3h 0.0114 |
| mix_open_on@4 | 0.193 | pump_hz_on_since_h 0.0589, valve_hzrl_on_sum24h 0.0281, pump_hz_on_sum3h 0.0251 |
| mix_close_on@4 | 0.120 | hzrkl_eg_rule 0.03, pump_hz_on_sum24h 0.0131, pump_hz_on_sum6h 0.0112 |
| pump_ww_oil_on@4 | 0.292 | pump_ww_oil_on_sum24h 0.1159, pump_circ_on_sum24h 0.0531, pump_ww_oil_on_sum12h 0.0245 |
| pump_circ_on@4 | 0.329 | pump_circ_on_sum24h 0.1534, pump_ww_oil_on_sum24h 0.0423, pump_circ_on_sum12h 0.0416 |
| valve_hzrl_on@4 | 0.491 | valve_hzrl_on_sum24h 0.2667, oel_burner_on_sum24h 0.1246, pump_hz_on_sum24h 0.0193 |
| pump_solar_on@4 | 0.021 | uvr_o1_margin_last 0.0015, valve_hzrl_on_sum3h 0.0013, buff_strat_top_mid 0.0012 |
| hzrkl_kell_on@4 | 0.184 | hzrkl_kell_rule 0.0978, hzrkl_kell_rule_last 0.065, uvr_o1_margin 0.0025 |
| hzrkl_eg_on@4 | 0.213 | hzrkl_eg_rule 0.1776, hzrkl_eg_rule_last 0.0217, uvr_o1_margin 0.002 |
| hzrkl_dg_on@4 | 0.082 | hzrkl_dg_rule_last 0.0563, hzrkl_dg_rule 0.0056, vl_err 0.0028 |

### 8b. Top 8 gesamt

- `ras_kell_ist@4`: hour 0.1575, ras_kell_perc 0.1299, wxa_swr_sum_6 0.0828, hzrkl_kell_rule 0.0755, hzrkl_kell_pos_lag2 0.0551, hour_cos 0.0518, hour_sin 0.0278, wx_t15_swr 0.0276
- `ras_eg_ist@4`: hour_cos 0.2679, wxa_swr_sum_6 0.1244, ras_eg_perc 0.0953, hour 0.0829, ras_eg_ist 0.0539, ras_eg_perc_lag1 0.0497, ext_lag1 0.0317, hour_sin 0.0291
- `ras_dg_ist@4`: wx_t4_swr 0.2072, wx_t5_swr 0.0849, wx_t15_swr 0.06, ras_dg_ist_trend 0.0459, ras_dg_perc 0.0305, oel_burner_on 0.0279, ras_dg_perc_trend 0.0237, wx_t16_swr 0.0225
- `ras_kell_perc@4`: ras_kell_perc_trend 0.1102, ras_kell_perc 0.0821, hzrkl_kell_rule 0.0805, ras_kell_ist_trend 0.0679, wxa_swr_sum_6 0.0645, hour 0.0626, hour_cos 0.0411, hzrkl_kell_pos_lag2 0.0329
- `ras_eg_perc@4`: hour_cos 0.2818, ras_eg_perc 0.1295, wxa_swr_sum_6 0.1053, hour 0.0693, ras_eg_ist 0.0574, ext_lag1 0.0467, ras_eg_perc_lag1 0.0428, hour_sin 0.0427
- `ras_dg_perc@4`: wx_t4_swr 0.199, wx_t5_swr 0.0953, wx_t15_swr 0.0383, ras_dg_perc_trend 0.0376, ras_dg_perc 0.0314, ras_dg_ist_trend 0.0302, wx_t16_swr 0.0265, oel_burner_on 0.0254
- `hz_vl@4`: hz_vl_lag1 0.0613, hz_vl_trend 0.0528, hour 0.0458, hz_vl 0.043, pump_hz_on_sum24h 0.0405, vl_err 0.0326, hour_sin 0.0319, wx_t5_swr 0.0302
- `hz_rl@4`: hz_rl 0.0665, hz_rl_trend 0.0331, hz_vl_trend 0.033, hour_sin 0.0313, hour 0.0309, wx_t5_t_bias 0.0292, hz_rl_lag1 0.0206, hour_cos 0.0201
- `buff_top@4`: uvr_ww_margin 0.0718, hour_cos 0.0464, hour 0.0458, buff_top 0.0385, buff_top_d1 0.0369, buff_top_trend 0.0339, buff_top_last 0.0294, wx_t15_swr 0.0263
- `buff_mid@4`: buff_mid_trend 0.048, buff_bot_trend 0.0479, o9_switch_margin 0.0332, buff_mid 0.0235, buff_strat_mid_bot 0.0218, buff_mid_lag2 0.0179, wx_t5_swr 0.0166, uvr_o9_margin 0.0151
- `buff_bot@4`: buff_bot_trend 0.0628, o9_switch_margin 0.0268, buff_bot 0.026, uvr_o9_margin_last 0.0223, wx_t4_swr 0.0155, hour_cos 0.0125, hour 0.0124, hour_sin 0.0114
- `ww@4`: hour 0.1105, ww 0.1009, hour_cos 0.091, pump_circ_on_sum24h 0.0361, wxa_swr_sum_6 0.0319, pump_circ_on 0.0286, pump_ww_oil_on_sum24h 0.0232, pump_ww_oil_on 0.0224
- `solar_coll@4`: solar_coll 0.0615, solar_coll_trend 0.0587, wx_t21_swr 0.0505, hour_sin 0.0367, wx_t22_swr 0.0298, wx_t5_swr 0.0292, solar_coll_last 0.023, wx_t6_swr 0.0188
- `wood_vl@4`: vl_err 0.0745, wood_vl_trend 0.0402, ext_lag6 0.0292, pump_wood_est_on_since_h 0.0277, uvr_o1_margin 0.0237, wood_vl 0.0229, hour 0.0218, rt_soll 0.021
- `flue@4`: flue 0.1612, hour 0.0265, hour_sin 0.0254, flue_last 0.0152, flue_trend 0.0148, hour_cos 0.0124, pump_circ_on_sum6h 0.0122, wood_vl 0.0113
- `oil_vl@4`: oil_vl 0.1099, oil_vl_trend 0.0394, uvr_o2_margin 0.0324, hour 0.0287, wx_t5_t_bias 0.0276, hour_cos 0.0269, oil_vl_last 0.026, buff_top_last 0.0204
- `hzrkl_kell_pos@4`: hz_vl_lag1 0.1638, wx_t17_swr 0.1213, hz_vl_d1 0.0705, hour 0.0692, pump_hz_on_sum6h 0.0663, hzrkl_kell_pos_lag1 0.0576, pump_hz_on_since_h 0.0436, wxa_swr_sum_6 0.0393
- `hzrkl_eg_pos@4`: hzrkl_eg_pos_lag1 0.2461, hz_vl_lag1 0.1211, wx_t17_swr 0.069, mix_open_on 0.0539, hz_vl_d1 0.0448, wx_t16_swr 0.0312, hour 0.0293, hour_cos 0.029
- `hzrkl_dg_pos@4`: hzrkl_dg_pos_lag1 0.0874, pump_hz_on_sum6h 0.0753, hz_vl_lag1 0.0583, hour 0.0451, hz_vl_d1 0.0415, hour_cos 0.0362, hz_vl_lag2 0.0354, wx_t17_swr 0.0353
- `oel_burner_on@4`: oel_burner_on_sum24h 0.2923, valve_hzrl_on_sum24h 0.1314, hour 0.0897, wx_t5_t_bias 0.0389, wx_t4_t_bias 0.0264, pump_hz_on_sum24h 0.0247, buff_mid_last 0.0226, hour_sin 0.021
- `pump_hz_on@4`: pump_hz_last 0.1248, hz_flow_pump 0.0952, hour 0.0755, ras_perc_min_last 0.0603, ras_perc_min 0.0581, pump_hz_on 0.0498, hour_sin 0.0407, wx_t5_t_bias 0.0335
- `pump_ww_on@4`: hour 0.1206, hour_sin 0.0573, day_of_week 0.0519, pump_ww_on_sum24h 0.0415, buff_mid_last 0.0407, buff_top_last 0.0318, hour_cos 0.0277, buff_mid 0.0239
- `mix_open_on@4`: hour 0.1781, wx_t5_t_bias 0.084, wxa_t_mean_6 0.0616, pump_hz_on_since_h 0.0589, hour_sin 0.0375, ext 0.0373, ras_perc_min_last 0.0343, valve_hzrl_on_sum24h 0.0281
- `mix_close_on@4`: ras_perc_min_last 0.1445, hour_sin 0.1238, hour 0.1028, ras_perc_min 0.0939, ras_eg_perc 0.0706, hzrkl_eg_rule 0.03, hour_cos 0.0272, ras_eg_perc_lag12 0.0212
- `pump_ww_oil_on@4`: hour 0.2258, pump_ww_oil_on_sum24h 0.1159, pump_circ_on_sum24h 0.0531, hour_sin 0.0523, hour_cos 0.0482, ww_lag12 0.0374, solar_coll_last 0.0348, day_of_week 0.0271
- `pump_circ_on@4`: hour 0.2235, pump_circ_on_sum24h 0.1534, hour_sin 0.0562, hour_cos 0.0488, pump_ww_oil_on_sum24h 0.0423, pump_circ_on_sum12h 0.0416, ww_lag12 0.0395, solar_coll_last 0.0298
- `valve_hzrl_on@4`: valve_hzrl_on_sum24h 0.2667, oel_burner_on_sum24h 0.1246, wx_t5_t_bias 0.0813, wx_t4_t_bias 0.0352, hour_sin 0.0337, hour 0.029, buff_mid_last 0.0237, buff_bot_last 0.0233
- `pump_solar_on@4`: wx_t5_swr 0.2661, wx_t6_swr 0.2287, wx_t7_swr 0.0797, wx_t4_swr 0.0501, hour 0.0366, hour_sin 0.0267, wxa_swr_sum_6 0.022, wx_t20_swr 0.0125
- `hzrkl_kell_on@4`: ras_kell_perc 0.1753, ras_kell_perc_last 0.1342, hzrkl_kell_rule 0.0978, hzrkl_kell_rule_last 0.065, offset_kell 0.0385, hzrkl_kell_pos 0.0192, hour 0.0184, hour_cos 0.0159
- `hzrkl_eg_on@4`: ras_eg_perc_last 0.2097, ras_eg_perc 0.1951, hzrkl_eg_rule 0.1776, ras_perc_min_last 0.0347, hzrkl_eg_rule_last 0.0217, offset_eg 0.0216, ras_perc_min 0.0159, hour_cos 0.0133
- `hzrkl_dg_on@4`: ras_dg_perc_last 0.2163, ras_dg_perc 0.2047, wx_t4_t_bias 0.0588, hzrkl_dg_rule_last 0.0563, wx_t4_swr 0.0366, wxa_t_mean_6 0.0153, wxa_t_mean_12 0.0145, wx_t5_swr 0.012

## 9. Lauf-Metadaten

- Stunden: 54084 · Splits: {'pre': 0, 'train': 38016, 'valid': 8760, 'test': 7308} · Nicht-1-h-Rasterschritte (DST): 0
- Laufzeiten: {'lgbm_s': 2405.7, 'keras_s': 61.0, 'refit_s': 1540.5, 'total_s': 4108.7}
- LightGBM-Runden: max 3000 · am Rundenlimit 3000: 2 Modelle
- Konfiguration: {"ras_stale_age": 3.0, "controller_targets": ["pump_hz_on", "hzrkl_kell_on", "hzrkl_eg_on", "hzrkl_dg_on"], "controller_causal": true, "excluded_features": {"pump_hz_on": ["ras_kell_ist", "ras_eg_ist", "ras_dg_ist", "ras_kell_perc_lag1", "ras_kell_perc_lag2", "ras_kell_perc_lag6", "ras_kell_perc_lag12", "ras_kell_perc_lag24", "ras_kell_ist_lag1", "ras_kell_ist_lag2", "ras_kell_ist_lag6", "ras_kell_ist_lag12", "ras_kell_ist_lag24", "offset_kell_lag1", "offset_kell_lag2", "offset_kell_lag6", "offset_kell_lag12", "offset_kell_lag24", "ras_eg_perc_lag1", "ras_eg_perc_lag2", "ras_eg_perc_lag6", "ras_eg_perc_lag12", "ras_eg_perc_lag24", "ras_eg_ist_lag1", "ras_eg_ist_lag2", "ras_eg_ist_lag6", "ras_eg_ist_lag12", "ras_eg_ist_lag24", "offset_eg_lag1", "offset_eg_lag2", "offset_eg_lag6", "offset_eg_lag12", "offset_eg_lag24", "ras_dg_perc_lag1", "ras_dg_perc_lag2", "ras_dg_perc_lag6", "ras_dg_perc_lag12", "ras_dg_perc_lag24", "ras_dg_ist_lag1", "ras_dg_ist_lag2", "ras_dg_ist_lag6", "ras_dg_ist_lag12", "ras_dg_ist_lag24", "offset_dg_lag1", "offset_dg_lag2", "offset_dg_lag6", "offset_dg_lag12", "offset_dg_lag24", "ras_kell_ist_last", "ras_eg_ist_last", "ras_dg_ist_last", "hzrkl_kell_pos_lag1", "hzrkl_eg_pos_lag1", "hzrkl_dg_pos_lag1", "hzrkl_kell_pos_lag2", "hzrkl_eg_pos_lag2", "hzrkl_dg_pos_lag2", "hzrkl_kell_pos_lag6", "hzrkl_eg_pos_lag6", "hzrkl_dg_pos_lag6", "hzrkl_kell_pos_lag12", "hzrkl_eg_pos_lag12", "hzrkl_dg_pos_lag12", "hzrkl_kell_pos_lag24", "hzrkl_eg_pos_lag24", "hzrkl_dg_pos_lag24", "hzrkl_kell_rule_lag1", "hzrkl_eg_rule_lag1", "hzrkl_dg_rule_lag1", "hzrkl_kell_rule_lag2", "hzrkl_eg_rule_lag2", "hzrkl_dg_rule_lag2", "hzrkl_kell_rule_lag6", "hzrkl_eg_rule_lag6", "hzrkl_dg_rule_lag6", "hzrkl_kell_rule_lag12", "hzrkl_eg_rule_lag12", "hzrkl_dg_rule_lag12", "hzrkl_kell_rule_lag24", "hzrkl_eg_rule_lag24", "hzrkl_dg_rule_lag24", "oel_burner_on_sum3h", "pump_wood_est_on_sum3h", "wood_fire_on_sum3h", "pump_ww_on_sum3h", "pump_ww_oil_on_sum3h", "pump_circ_on_sum3h", "pump_hz_on_sum3h", "pump_solar_on_sum3h", "valve_hzrl_on_sum3h", "oel_burner_on_sum6h", "pump_wood_est_on_sum6h", "wood_fire_on_sum6h", "pump_ww_on_sum6h", "pump_ww_oil_on_sum6h", "pump_circ_on_sum6h", "pump_hz_on_sum6h", "pump_solar_on_sum6h", "valve_hzrl_on_sum6h", "oel_burner_on_sum12h", "pump_wood_est_on_sum12h", "wood_fire_on_sum12h", "pump_ww_on_sum12h", "pump_ww_oil_on_sum12h", "pump_circ_on_sum12h", "pump_hz_on_sum12h", "pump_solar_on_sum12h", "valve_hzrl_on_sum12h", "oel_burner_on_sum24h", "pump_wood_est_on_sum24h", "wood_fire_on_sum24h", "pump_ww_on_sum24h", "pump_ww_oil_on_sum24h", "pump_circ_on_sum24h", "pump_hz_on_sum24h", "pump_solar_on_sum24h", "valve_hzrl_on_sum24h", "oel_burner_on_since_h", "pump_wood_est_on_since_h", "wood_fire_on_since_h", "pump_ww_on_since_h", "pump_ww_oil_on_since_h", "pump_circ_on_since_h", "pump_hz_on_since_h", "pump_solar_on_since_h", "valve_hzrl_on_since_h", "ras_ist_min", "ras_kell_ist_d1", "ras_eg_ist_d1", "ras_dg_ist_d1", "ras_kell_ist_trend", "ras_eg_ist_trend", "ras_dg_ist_trend"], "hzrkl_kell_on": ["ras_kell_ist", "ras_eg_ist", "ras_dg_ist", "ras_kell_perc_lag1", "ras_kell_perc_lag2", "ras_kell_perc_lag6", "ras_kell_perc_lag12", "ras_kell_perc_lag24", "ras_kell_ist_lag1", "ras_kell_ist_lag2", "ras_kell_ist_lag6", "ras_kell_ist_lag12", "ras_kell_ist_lag24", "offset_kell_lag1", "offset_kell_lag2", "offset_kell_lag6", "offset_kell_lag12", "offset_kell_lag24", "ras_eg_perc_lag1", "ras_eg_perc_lag2", "ras_eg_perc_lag6", "ras_eg_perc_lag12", "ras_eg_perc_lag24", "ras_eg_ist_lag1", "ras_eg_ist_lag2", "ras_eg_ist_lag6", "ras_eg_ist_lag12", "ras_eg_ist_lag24", "offset_eg_lag1", "offset_eg_lag2", "offset_eg_lag6", "offset_eg_lag12", "offset_eg_lag24", "ras_dg_perc_lag1", "ras_dg_perc_lag2", "ras_dg_perc_lag6", "ras_dg_perc_lag12", "ras_dg_perc_lag24", "ras_dg_ist_lag1", "ras_dg_ist_lag2", "ras_dg_ist_lag6", "ras_dg_ist_lag12", "ras_dg_ist_lag24", "offset_dg_lag1", "offset_dg_lag2", "offset_dg_lag6", "offset_dg_lag12", "offset_dg_lag24", "ras_kell_ist_last", "ras_eg_ist_last", "ras_dg_ist_last", "hzrkl_kell_pos_lag1", "hzrkl_eg_pos_lag1", "hzrkl_dg_pos_lag1", "hzrkl_kell_pos_lag2", "hzrkl_eg_pos_lag2", "hzrkl_dg_pos_lag2", "hzrkl_kell_pos_lag6", "hzrkl_eg_pos_lag6", "hzrkl_dg_pos_lag6", "hzrkl_kell_pos_lag12", "hzrkl_eg_pos_lag12", "hzrkl_dg_pos_lag12", "hzrkl_kell_pos_lag24", "hzrkl_eg_pos_lag24", "hzrkl_dg_pos_lag24", "hzrkl_kell_rule_lag1", "hzrkl_eg_rule_lag1", "hzrkl_dg_rule_lag1", "hzrkl_kell_rule_lag2", "hzrkl_eg_rule_lag2", "hzrkl_dg_rule_lag2", "hzrkl_kell_rule_lag6", "hzrkl_eg_rule_lag6", "hzrkl_dg_rule_lag6", "hzrkl_kell_rule_lag12", "hzrkl_eg_rule_lag12", "hzrkl_dg_rule_lag12", "hzrkl_kell_rule_lag24", "hzrkl_eg_rule_lag24", "hzrkl_dg_rule_lag24", "oel_burner_on_sum3h", "pump_wood_est_on_sum3h", "wood_fire_on_sum3h", "pump_ww_on_sum3h", "pump_ww_oil_on_sum3h", "pump_circ_on_sum3h", "pump_hz_on_sum3h", "pump_solar_on_sum3h", "valve_hzrl_on_sum3h", "oel_burner_on_sum6h", "pump_wood_est_on_sum6h", "wood_fire_on_sum6h", "pump_ww_on_sum6h", "pump_ww_oil_on_sum6h", "pump_circ_on_sum6h", "pump_hz_on_sum6h", "pump_solar_on_sum6h", "valve_hzrl_on_sum6h", "oel_burner_on_sum12h", "pump_wood_est_on_sum12h", "wood_fire_on_sum12h", "pump_ww_on_sum12h", "pump_ww_oil_on_sum12h", "pump_circ_on_sum12h", "pump_hz_on_sum12h", "pump_solar_on_sum12h", "valve_hzrl_on_sum12h", "oel_burner_on_sum24h", "pump_wood_est_on_sum24h", "wood_fire_on_sum24h", "pump_ww_on_sum24h", "pump_ww_oil_on_sum24h", "pump_circ_on_sum24h", "pump_hz_on_sum24h", "pump_solar_on_sum24h", "valve_hzrl_on_sum24h", "oel_burner_on_since_h", "pump_wood_est_on_since_h", "wood_fire_on_since_h", "pump_ww_on_since_h", "pump_ww_oil_on_since_h", "pump_circ_on_since_h", "pump_hz_on_since_h", "pump_solar_on_since_h", "valve_hzrl_on_since_h", "ras_ist_min", "ras_kell_ist_d1", "ras_eg_ist_d1", "ras_dg_ist_d1", "ras_kell_ist_trend", "ras_eg_ist_trend", "ras_dg_ist_trend"], "hzrkl_eg_on": ["ras_kell_ist", "ras_eg_ist", "ras_dg_ist", "ras_kell_perc_lag1", "ras_kell_perc_lag2", "ras_kell_perc_lag6", "ras_kell_perc_lag12", "ras_kell_perc_lag24", "ras_kell_ist_lag1", "ras_kell_ist_lag2", "ras_kell_ist_lag6", "ras_kell_ist_lag12", "ras_kell_ist_lag24", "offset_kell_lag1", "offset_kell_lag2", "offset_kell_lag6", "offset_kell_lag12", "offset_kell_lag24", "ras_eg_perc_lag1", "ras_eg_perc_lag2", "ras_eg_perc_lag6", "ras_eg_perc_lag12", "ras_eg_perc_lag24", "ras_eg_ist_lag1", "ras_eg_ist_lag2", "ras_eg_ist_lag6", "ras_eg_ist_lag12", "ras_eg_ist_lag24", "offset_eg_lag1", "offset_eg_lag2", "offset_eg_lag6", "offset_eg_lag12", "offset_eg_lag24", "ras_dg_perc_lag1", "ras_dg_perc_lag2", "ras_dg_perc_lag6", "ras_dg_perc_lag12", "ras_dg_perc_lag24", "ras_dg_ist_lag1", "ras_dg_ist_lag2", "ras_dg_ist_lag6", "ras_dg_ist_lag12", "ras_dg_ist_lag24", "offset_dg_lag1", "offset_dg_lag2", "offset_dg_lag6", "offset_dg_lag12", "offset_dg_lag24", "ras_kell_ist_last", "ras_eg_ist_last", "ras_dg_ist_last", "hzrkl_kell_pos_lag1", "hzrkl_eg_pos_lag1", "hzrkl_dg_pos_lag1", "hzrkl_kell_pos_lag2", "hzrkl_eg_pos_lag2", "hzrkl_dg_pos_lag2", "hzrkl_kell_pos_lag6", "hzrkl_eg_pos_lag6", "hzrkl_dg_pos_lag6", "hzrkl_kell_pos_lag12", "hzrkl_eg_pos_lag12", "hzrkl_dg_pos_lag12", "hzrkl_kell_pos_lag24", "hzrkl_eg_pos_lag24", "hzrkl_dg_pos_lag24", "hzrkl_kell_rule_lag1", "hzrkl_eg_rule_lag1", "hzrkl_dg_rule_lag1", "hzrkl_kell_rule_lag2", "hzrkl_eg_rule_lag2", "hzrkl_dg_rule_lag2", "hzrkl_kell_rule_lag6", "hzrkl_eg_rule_lag6", "hzrkl_dg_rule_lag6", "hzrkl_kell_rule_lag12", "hzrkl_eg_rule_lag12", "hzrkl_dg_rule_lag12", "hzrkl_kell_rule_lag24", "hzrkl_eg_rule_lag24", "hzrkl_dg_rule_lag24", "oel_burner_on_sum3h", "pump_wood_est_on_sum3h", "wood_fire_on_sum3h", "pump_ww_on_sum3h", "pump_ww_oil_on_sum3h", "pump_circ_on_sum3h", "pump_hz_on_sum3h", "pump_solar_on_sum3h", "valve_hzrl_on_sum3h", "oel_burner_on_sum6h", "pump_wood_est_on_sum6h", "wood_fire_on_sum6h", "pump_ww_on_sum6h", "pump_ww_oil_on_sum6h", "pump_circ_on_sum6h", "pump_hz_on_sum6h", "pump_solar_on_sum6h", "valve_hzrl_on_sum6h", "oel_burner_on_sum12h", "pump_wood_est_on_sum12h", "wood_fire_on_sum12h", "pump_ww_on_sum12h", "pump_ww_oil_on_sum12h", "pump_circ_on_sum12h", "pump_hz_on_sum12h", "pump_solar_on_sum12h", "valve_hzrl_on_sum12h", "oel_burner_on_sum24h", "pump_wood_est_on_sum24h", "wood_fire_on_sum24h", "pump_ww_on_sum24h", "pump_ww_oil_on_sum24h", "pump_circ_on_sum24h", "pump_hz_on_sum24h", "pump_solar_on_sum24h", "valve_hzrl_on_sum24h", "oel_burner_on_since_h", "pump_wood_est_on_since_h", "wood_fire_on_since_h", "pump_ww_on_since_h", "pump_ww_oil_on_since_h", "pump_circ_on_since_h", "pump_hz_on_since_h", "pump_solar_on_since_h", "valve_hzrl_on_since_h", "ras_ist_min", "ras_kell_ist_d1", "ras_eg_ist_d1", "ras_dg_ist_d1", "ras_kell_ist_trend", "ras_eg_ist_trend", "ras_dg_ist_trend"], "hzrkl_dg_on": ["ras_kell_ist", "ras_eg_ist", "ras_dg_ist", "ras_kell_perc_lag1", "ras_kell_perc_lag2", "ras_kell_perc_lag6", "ras_kell_perc_lag12", "ras_kell_perc_lag24", "ras_kell_ist_lag1", "ras_kell_ist_lag2", "ras_kell_ist_lag6", "ras_kell_ist_lag12", "ras_kell_ist_lag24", "offset_kell_lag1", "offset_kell_lag2", "offset_kell_lag6", "offset_kell_lag12", "offset_kell_lag24", "ras_eg_perc_lag1", "ras_eg_perc_lag2", "ras_eg_perc_lag6", "ras_eg_perc_lag12", "ras_eg_perc_lag24", "ras_eg_ist_lag1", "ras_eg_ist_lag2", "ras_eg_ist_lag6", "ras_eg_ist_lag12", "ras_eg_ist_lag24", "offset_eg_lag1", "offset_eg_lag2", "offset_eg_lag6", "offset_eg_lag12", "offset_eg_lag24", "ras_dg_perc_lag1", "ras_dg_perc_lag2", "ras_dg_perc_lag6", "ras_dg_perc_lag12", "ras_dg_perc_lag24", "ras_dg_ist_lag1", "ras_dg_ist_lag2", "ras_dg_ist_lag6", "ras_dg_ist_lag12", "ras_dg_ist_lag24", "offset_dg_lag1", "offset_dg_lag2", "offset_dg_lag6", "offset_dg_lag12", "offset_dg_lag24", "ras_kell_ist_last", "ras_eg_ist_last", "ras_dg_ist_last", "hzrkl_kell_pos_lag1", "hzrkl_eg_pos_lag1", "hzrkl_dg_pos_lag1", "hzrkl_kell_pos_lag2", "hzrkl_eg_pos_lag2", "hzrkl_dg_pos_lag2", "hzrkl_kell_pos_lag6", "hzrkl_eg_pos_lag6", "hzrkl_dg_pos_lag6", "hzrkl_kell_pos_lag12", "hzrkl_eg_pos_lag12", "hzrkl_dg_pos_lag12", "hzrkl_kell_pos_lag24", "hzrkl_eg_pos_lag24", "hzrkl_dg_pos_lag24", "hzrkl_kell_rule_lag1", "hzrkl_eg_rule_lag1", "hzrkl_dg_rule_lag1", "hzrkl_kell_rule_lag2", "hzrkl_eg_rule_lag2", "hzrkl_dg_rule_lag2", "hzrkl_kell_rule_lag6", "hzrkl_eg_rule_lag6", "hzrkl_dg_rule_lag6", "hzrkl_kell_rule_lag12", "hzrkl_eg_rule_lag12", "hzrkl_dg_rule_lag12", "hzrkl_kell_rule_lag24", "hzrkl_eg_rule_lag24", "hzrkl_dg_rule_lag24", "oel_burner_on_sum3h", "pump_wood_est_on_sum3h", "wood_fire_on_sum3h", "pump_ww_on_sum3h", "pump_ww_oil_on_sum3h", "pump_circ_on_sum3h", "pump_hz_on_sum3h", "pump_solar_on_sum3h", "valve_hzrl_on_sum3h", "oel_burner_on_sum6h", "pump_wood_est_on_sum6h", "wood_fire_on_sum6h", "pump_ww_on_sum6h", "pump_ww_oil_on_sum6h", "pump_circ_on_sum6h", "pump_hz_on_sum6h", "pump_solar_on_sum6h", "valve_hzrl_on_sum6h", "oel_burner_on_sum12h", "pump_wood_est_on_sum12h", "wood_fire_on_sum12h", "pump_ww_on_sum12h", "pump_ww_oil_on_sum12h", "pump_circ_on_sum12h", "pump_hz_on_sum12h", "pump_solar_on_sum12h", "valve_hzrl_on_sum12h", "oel_burner_on_sum24h", "pump_wood_est_on_sum24h", "wood_fire_on_sum24h", "pump_ww_on_sum24h", "pump_ww_oil_on_sum24h", "pump_circ_on_sum24h", "pump_hz_on_sum24h", "pump_solar_on_sum24h", "valve_hzrl_on_sum24h", "oel_burner_on_since_h", "pump_wood_est_on_since_h", "wood_fire_on_since_h", "pump_ww_on_since_h", "pump_ww_oil_on_since_h", "pump_circ_on_since_h", "pump_hz_on_since_h", "pump_solar_on_since_h", "valve_hzrl_on_since_h", "ras_ist_min", "ras_kell_ist_d1", "ras_eg_ist_d1", "ras_dg_ist_d1", "ras_kell_ist_trend", "ras_eg_ist_trend", "ras_dg_ist_trend"]}, "mono_tolerance": 1.02, "lgbm_params_analog": {"objective": "regression_l1", "metric": "l1", "learning_rate": 0.05, "num_leaves": 31, "min_child_samples": 50, "feature_fraction": 0.5, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0, "max_bin": 255, "seed": 20261031, "deterministic": true, "force_row_wise": true, "num_threads": 16, "verbosity": -1}, "lgbm_num_boost": 3000, "keras_hidden": [256, 128], "keras_epochs_max": 150}
