# M1 — REPORT: Wetter-Backfill (Open-Meteo) + Feature-Store

_Erstellt: 2026-11-01T11:02:41Z · git `110d68250260` · data_version `m1.3`_


## 1. Daten-Coverage (Roh-Quellen)

| Quelle | first ts | last ts | Rows | Schema |
|---|---|---|---|---|
| winsol_pq | 2020-08-30 12:36:09 | 2026-10-27 07:25:54 | 19,211,004 | 71 Spalten, OK |
| cmi_log_pq | 2026-10-27 07:26:24 | 2026-11-01 11:59:39 | 39,281 | 71 Spalten, OK |

Übergang: winsol_pq last `2026-10-27 07:25:54` → cmi_log_pq first `2026-10-27 07:26:24` (geprüft, kein Overlap; Schema identisch → kein Column-Mapping nötig, `source`-Flag je Zeile).


### Lücken im Feature-Store (dokumentiert, NICHT stumm gefüllt)

| Monat (ym) | gap-Std | full | partial | erste | letzte |
|---|---|---|---|---|---|
| 2020-12 | 2 | 0 | 2 | 2020-12-09 11:00:00 | 2020-12-27 15:00:00 |
| 2021-03 | 2 | 0 | 2 | 2021-03-28 03:00:00 | 2021-03-28 04:00:00 |
| 2021-04 | 2 | 0 | 2 | 2021-04-24 11:00:00 | 2021-04-24 12:00:00 |
| 2021-06 | 6 | 0 | 6 | 2021-06-23 11:00:00 | 2021-06-26 08:00:00 |
| 2021-09 | 85 | 83 | 2 | 2021-09-25 10:00:00 | 2021-09-30 11:00:00 |
| 2021-10 | 18 | 2 | 16 | 2021-10-20 14:00:00 | 2021-10-30 11:00:00 |
| 2021-11 | 3 | 0 | 3 | 2021-11-01 09:00:00 | 2021-11-17 20:00:00 |
| 2022-02 | 12 | 2 | 10 | 2022-02-08 08:00:00 | 2022-02-12 14:00:00 |
| 2022-03 | 14 | 3 | 11 | 2022-03-15 08:00:00 | 2022-03-31 07:00:00 |
| 2022-04 | 4 | 0 | 4 | 2022-04-02 10:00:00 | 2022-04-28 10:00:00 |
| 2022-05 | 1 | 0 | 1 | 2022-05-06 13:00:00 | 2022-05-06 13:00:00 |
| 2022-08 | 1 | 0 | 1 | 2022-08-27 21:00:00 | 2022-08-27 21:00:00 |
| 2022-09 | 2 | 0 | 2 | 2022-09-18 14:00:00 | 2022-09-18 15:00:00 |
| 2022-11 | 1 | 0 | 1 | 2022-11-11 05:00:00 | 2022-11-11 05:00:00 |
| 2022-12 | 1 | 0 | 1 | 2022-12-07 06:00:00 | 2022-12-07 06:00:00 |
| 2023-02 | 1 | 0 | 1 | 2023-02-08 14:00:00 | 2023-02-08 14:00:00 |
| 2023-03 | 4 | 1 | 3 | 2023-03-26 02:00:00 | 2023-03-27 20:00:00 |
| 2023-04 | 3 | 0 | 3 | 2023-04-03 14:00:00 | 2023-04-15 21:00:00 |
| 2023-05 | 3 | 0 | 3 | 2023-05-03 14:00:00 | 2023-05-12 22:00:00 |
| 2023-07 | 1 | 0 | 1 | 2023-07-09 10:00:00 | 2023-07-09 10:00:00 |
| 2023-10 | 2 | 0 | 2 | 2023-10-08 12:00:00 | 2023-10-21 21:00:00 |
| 2023-12 | 2 | 0 | 2 | 2023-12-11 05:00:00 | 2023-12-14 11:00:00 |
| 2024-01 | 2 | 0 | 2 | 2024-01-08 22:00:00 | 2024-01-28 12:00:00 |
| 2024-02 | 1 | 0 | 1 | 2024-02-08 20:00:00 | 2024-02-08 20:00:00 |
| 2024-03 | 2 | 0 | 2 | 2024-03-31 02:00:00 | 2024-03-31 03:00:00 |
| 2024-04 | 3 | 0 | 3 | 2024-04-05 16:00:00 | 2024-04-24 05:00:00 |
| 2024-05 | 2 | 0 | 2 | 2024-05-11 07:00:00 | 2024-05-29 11:00:00 |
| 2024-06 | 110 | 107 | 3 | 2024-06-06 06:00:00 | 2024-06-30 23:00:00 |
| 2024-07 | 388 | 384 | 4 | 2024-07-01 00:00:00 | 2024-07-24 12:00:00 |
| 2024-08 | 2 | 0 | 2 | 2024-08-26 13:00:00 | 2024-08-31 14:00:00 |
| 2025-01 | 2 | 0 | 2 | 2025-01-22 12:00:00 | 2025-01-25 21:00:00 |
| 2025-02 | 5 | 2 | 3 | 2025-02-06 08:00:00 | 2025-02-10 13:00:00 |
| 2025-03 | 7 | 1 | 6 | 2025-03-05 20:00:00 | 2025-03-30 03:00:00 |
| 2025-04 | 6 | 0 | 6 | 2025-04-06 12:00:00 | 2025-04-25 11:00:00 |
| 2025-05 | 14 | 0 | 14 | 2025-05-02 16:00:00 | 2025-05-30 09:00:00 |
| 2025-06 | 4 | 0 | 4 | 2025-06-02 07:00:00 | 2025-06-09 19:00:00 |
| 2025-09 | 5 | 0 | 5 | 2025-09-11 21:00:00 | 2025-09-30 17:00:00 |
| 2025-10 | 5 | 0 | 5 | 2025-10-19 15:00:00 | 2025-10-29 14:00:00 |
| 2025-11 | 2 | 0 | 2 | 2025-11-11 12:00:00 | 2025-11-30 14:00:00 |
| 2026-02 | 2 | 0 | 2 | 2026-02-10 10:00:00 | 2026-02-20 20:00:00 |
| 2026-03 | 2 | 0 | 2 | 2026-03-29 02:00:00 | 2026-03-29 03:00:00 |
| 2026-04 | 1 | 0 | 1 | 2026-04-14 03:00:00 | 2026-04-14 03:00:00 |
| 2026-05 | 1 | 0 | 1 | 2026-05-25 14:00:00 | 2026-05-25 14:00:00 |
| 2026-07 | 1 | 0 | 1 | 2026-07-23 19:00:00 | 2026-07-23 19:00:00 |
| 2026-08 | 1 | 0 | 1 | 2026-08-22 12:00:00 | 2026-08-22 12:00:00 |
| 2026-09 | 1 | 0 | 1 | 2026-09-05 09:00:00 | 2026-09-05 09:00:00 |
| 2026-10 | 17 | 15 | 2 | 2026-10-30 00:00:00 | 2026-10-31 21:00:00 |

- `gap_full` = Stunde mit < 60 10-s-Rows (State-NULL), `gap_partial` = 60..355 Rows (State aus verfügbaren Rows, Flag=1).
- Hauptlücke **2024-06-26 13:00 → 2024-07-16 23:00** (Logger-Ausfall, 491 h): ursächlich in winsol_pq; Feature-Store zeigt sie als full-Gaps. Kein Forward-Fill/Interpolation. Zweitgrößte Lücke 2021-09-27 → 2021-09-30 (83 h). DST-Übergangstage sind KEINE Gaps: 2024-03-31 03:xx = 60 Rows (partial, RAS-Logger hat umgestellt), 2024-10-27 vollstündlich (360 h, Wallclock-Logik des Loggers).


## 2. Bias-Report (dev0 a8 vs Grid, saisonal)

| Saison | Median-Offset (K) | Drift (K/Tag) | n/season_hours |
|---|---|---|---|
| DJF | +1.103 | +0.00002 | 12948/12984 |
| MAM | +1.322 | +0.01616 | 13245/13248 |
| JJA | +1.639 | -0.00979 | 12781/13272 |
| SON | +0.984 | -0.00231 | 14457/14557 |

- Methode: stündlich sensor(a8-Mean) − grid(T2m); je Saison Median + OLS-Drift (K/Tag ab Saisonstart). Feature-Store enthält **Roh-Grid-T** (`wx_t{k}_t`) **und** bias-korrigierte T (`wx_t{k}_t_bias = grid + offset + drift·t`); ``wx_is_forecast`` (1 ab 2022-11-14 = ICON-D2, davor ERA5-Ist).
- Qualität: Grid = 2-km ICON-D2 (ab 2022-11) bzw. 25-km ERA5 (davor); CMI-Sensor dev0 a8 = Einzel-Sensor → Median (robust) statt Mean; Drift monatlich plausibel (< 0.05 K/Tag). Vorhersage-basierte Jahre (2022-11+) erwarten größere systematische Abweichungen als ERA5 (Reanalyse).


## 3. Wetter-Backfill (Open-Meteo)

- Koordinaten: **51.382974, 7.774286** · TimeZone-Requests = **UTC** (Open-Meteo IANA-ZZ ist DST-unsicher, s. SCHEMA.md §Wetter).
- ERA5-Archiv: `2020-08-01` → `2022-11-13` (27 Monate, `models=era5`, `timezone=UTC`).
- Historical-Forecast: `2022-11-14` → `2026-10-31` (`models=icon_d2`).

**Modell-Entscheidung (verifiziert, 2026-10-31):**
- Ziel ICON-D2 (2-km DE) ist ab **2022-11-14** im Historical-Forecast-Archiv vollständig (Test-Calls: 2022-11-01/13 → 0–10/24 h null; 2022-11-14/15/20/25/12-01 → 24/24 h). 2022-11-01..13 = ERA5 (Flag `wx_is_forecast=0`), ab 2022-11-14 ICON-D2 (Flag `wx_is_forecast=1`). `best_match`/`icon_seamless` fallen exakt auf ICON-D2 ab 2022-12 zurück → explizites `models=icon_d2` (reproduzierbar).
- Alle hourly-Variablen in **beiden** Quellen verfügbar (Test-Calls 2020-08 + 2022-11): temperature/dew_point/relative_humidity, cloudcover (+low/mid/high), precipitation, rain, wind_speed/gusts/direction, shortwave_radiation, weather_code. Sun (daily sunrise/sunset) im selben Call (0 Extra-Requests).

**Cache-Status (Roh-Cache `data/features/wx_cache/`):**

| API | Dateien | valide | fehlend |
|---|---|---|---|
| era5 | 28 | 28 | — |
| hfa | 48 | 48 | — |

- Fair-Use: sequentiell, 2 s Sleep/Call, Retry max 3 (Backoff 2/4/8 s), Resume über Cache-Prüfung (Datei + JSON-Valid + Größe). Fehlmonate werden in `FetchStats.missing_months` geloggt (nicht stumm ignoriert).

## 4. Feature-Store

- Dateien: **76** Monats-Parquet (Hive `YYYY/MM/`, zstd) unter `<repo-root>/data/features`.
- Rows: **54,084** (erwartet ≈ 54084 Stunden 2020-08-31 00:00 → 2026-11-01 10:00 UTC; Stunden ohne 10-s-Rows sind `gap`-Flags, keine weggelassenen Rows).
- Spalten: **692** (Details: `SCHEMA.md`).
- `data_version` = `m1.3`, git_tag = `110d68250260-dirty` (jeweils in `key_value_metadata` pro Datei + `_meta.json`).
- erste/letzte Datei: `<repo-root>/data/features/2020/08/2020-08.parquet` … `<repo-root>/data/features/2026/11/2026-11.parquet`.

## 5. Spot-Checks (3 zufällige Stunden, Feature vs Roh-Parquet)

_Seed 20261031 (deterministisch). Abgleich: Stundenmittel dev0 a8 (ext), dev0 a6 (hz_vl), Median dev1 a1−a2 (offset_kell), An-Quotient dev0 d5 (oel_burner_on); Toleranz 0.005._

### 2022-12-20 08:00:00 — ✅ OK (raw rows: 360)
| Feature | Feature-Store | Roh (DuckDB) | ok |
|---|---|---|---|
| ext | 10.0814 | 10.0814 | ✅ |
| hz_vl | 47.6553 | 47.6553 | ✅ |
| offset_kell | 2.5 | 2.5 | ✅ |
| oel_burner_on | 0.475 | 0.475 | ✅ |

### 2022-06-28 20:00:00 — ✅ OK (raw rows: 360)
| Feature | Feature-Store | Roh (DuckDB) | ok |
|---|---|---|---|
| ext | 22.3603 | 22.3603 | ✅ |
| hz_vl | 28.8142 | 28.8142 | ✅ |
| offset_kell | 5.0 | 5.0 | ✅ |
| oel_burner_on | 0.0 | 0.0 | ✅ |

### 2026-06-17 08:00:00 — ✅ OK (raw rows: 360)
| Feature | Feature-Store | Roh (DuckDB) | ok |
|---|---|---|---|
| ext | 19.1547 | 19.1547 | ✅ |
| hz_vl | 30.1931 | 30.1931 | ✅ |
| offset_kell | -0.6 | -0.6 | ✅ |
| oel_burner_on | 0.0 | 0.0 | ✅ |


## 6. DoD-Checkliste (M1)

- [x] Feature-Store `data/features/` vollständig 2020-09 → heute (76 Dateien, 54,084 Rows; Lücken dokumentiert, nicht stumm gefüllt)
- [x] Bias-Report mit Median-Offset je Saison (REPORT.md §2 + bias_profile.json)
- [x] pytest: alle grün, Coverage ≥ 80 % auf `analysis/model/**` (s. Test-Log unten)
- [x] Kein CMI-Zugriff, kein Code außerhalb des Scopes (nur `analysis/model/**`, `data/features/**`, `requirements.txt`)
- [x] REPORT.md + SCHEMA.md vollständig (dieses Dokument + `SCHEMA.md`)

_Öl-Teil (Q-106) bewusst ausgenommen (maintainer: „kommt später“) — Brenner-An-Quotient `oel_burner_on` als Proxy vorhanden._

## 7. m1.1 — RAS-Maske, RAS-Datenalter, Stunden-Endwerte (M2b-P2)

- RAS-Einbau `2021-10-20 17:00:00` (Anlagenbesitzer, 2026-10-31): RAS-Roh-Kanäle davor = NULL (`src`-View). Vor Einbau: **0** von 9977 Stunden mit Nicht-NULL-RAS-Wert (90 RAS-Spalten inkl. Lags/`_last`), `offset_known = 1`: **0** (Soll: 0).
- RAS-Ist < 5 °C ab Einbau (Stunden je Spalte): `ras_kell_ist` 0, `ras_eg_ist` 0, `ras_dg_ist` 0, `ras_kell_ist_last` 0, `ras_eg_ist_last` 0, `ras_dg_ist_last` 0, `ras2_kell_ist` 0, `ras2_eg_ist` 0, `ras2_dg_ist` 0.
- `ras_age_*` = Stunden-MAX dev1 a11–a13 (Datenalter-Sägezahn, +0,1 je 60 s ⇒ Wert × 10 ≈ Minuten; veraltet = > 3.0):

| Zone | n | P50 | P90 | P99 | P99.9 | max | Anteil > 3.0 |
|---|---|---|---|---|---|---|---|
| kell | 43593 | 1.2 | 1.2 | 6.3 | 250.0 | 250.0 | 0.0136 |
| eg | 43593 | 1.1 | 1.1 | 2.4 | 10.2 | 124.3 | 0.0085 |
| dg | 43593 | 1.0 | 1.0 | 250.0 | 250.0 | 250.0 | 0.0311 |

- Korrelation Stunden-Mean vs `{feat}_last` (ab Einbau): `ras_kell_perc` 0.9962, `ras_kell_ist` 0.9976, `ras_eg_perc` 0.9979, `ras_eg_ist` 0.997, `ras_dg_perc` 0.9981, `ras_dg_ist` 0.9988, `buff_top` 0.9695, `buff_mid` 0.9957, `buff_bot` 0.9939, `hz_vl` 0.939, `hz_rl` 0.9411, `ww` 0.8848, `ext` 0.9969, `oil_vl` 0.9528, `wood_vl` 0.9922, `flue` 0.9133, `solar_coll` 0.9777, `hzrkl_kell_pos` 0.9204, `hzrkl_eg_pos` 0.9323, `hzrkl_dg_pos` 0.9552.
- `heat_min_*` = **DEPRECATED** (keine Heiz-Minuten, sondern Summe positiver Deltas des Datenalter-Sägezahns ≈ 6/h, informationslos) — nur v001-Kompatibilität, nicht als Feature/Target verwenden. Details: `SCHEMA.md` §2.5/§7.

## 8. m1.2 — TA-Sentinel-Maske, Rücklaufminderer je Etage

- TA-Sentinel-Regel: analoger Roh-Wert `>= 3276.0` oder `<= -999.0` → NULL (`src`-View; 3276.7 = Fühlerbruch, -999.9 = Kurzschluss). Maskierte Samples je Roh-Kanal (winsol + cmi):

| Kanal | ≥ max (3276.7) | ≤ min (-999.9) |
|---|---|---|
| `dev0_analog_10` | 9,383 | 2,190 |
| `dev2_analog_16` | 556 | 0 |

- Maxima nach Maske: `flue` 342.9, `solar_coll` 179.5603, max über alle analogen `*_last` 430.0; Stunden mit `flue` / `flue_last` < −50 °C (kein Sentinel, nur berichtet): 0 / 0.
- Rücklaufminderer (`docs/plant-model.md` UVR610 A10–A15): `hzrkl_{z}_on` = Stunden-AVG(a10/a11/a12 ÷ 10) (binär 0/10 → An-Quotient), `hzrkl_{z}_pos` = Stunden-Mean a13 (Keller) / a15 (EG) / a14 (OG → dg), jeweils + `_last`; Heizsaison = `2024-11-01` … `2025-03-01` (exkl.):

| Zone | erste Std. on | Anteil on (ges./ab Start) | Mittel on Saison | corr(on, perc) ges./Saison | erste Std. pos | Anteil pos (ges./ab Start) | Mittel pos Saison | corr(pos, perc) |
|---|---|---|---|---|---|---|---|---|
| kell | 2022-03-06 13:00:00 | 0.7433 / 0.9848 | 0.2238 | -0.604 / -0.6442 | 2022-03-07 15:00:00 | 0.7449 / 0.9875 | 33.7263 | -0.4451 |
| eg | 2022-03-06 13:00:00 | 0.7433 / 0.9848 | 0.8971 | -0.7052 / -0.4888 | 2022-03-07 15:00:00 | 0.7449 / 0.9875 | 40.967 | -0.3803 |
| dg | 2022-03-06 13:00:00 | 0.7433 / 0.9848 | 0.7696 | -0.628 / -0.5628 | 2022-03-07 15:00:00 | 0.7449 / 0.9875 | 38.5887 | -0.3804 |

- Store: 54,084 Zeilen, 692 Spalten, data_version m1.3. Details: `SCHEMA.md` §2.13/§8.

## 9. m1.3 — Abgas, UVR-Regeln und Pumpenlaufzeiten

- Datenversionen m1.3; 54,084 Stunden. Abgas `flue`/`flue_last` max. 342.9 / 430.0 °C; Stunden >500 °C: 0; nicht maskierte Stunden im Defektfenster: 0.
- Rohproben im Defektfenster maskiert: 58,689; Rohproben >500 °C außerhalb des Fensters verworfen: 4. Das Abgas ist mit Faktor 10 auf echte °C skaliert; 500 °C bleibt gültig.
- Minderer-Regel vs. gemessenes Ventil (Stunden):
| Zone | n | MAE An-Quotient | Korrelation |
|---|---:|---:|---:|
| kell | 40,203 | 0.0052 | 0.999 |
| eg | 40,203 | 0.0049 | 0.9992 |
| dg | 40,203 | 0.003 | 0.9995 |
- Laufzeitfenster und Aktivitätsalter (NULL-Zeiten im Summenfenster werden ignoriert):
| Signal | gültige Std. | max. 24-h-Summe | max. since_h | ungültige since_h |
|---|---:|---:|---:|---:|
| `oel_burner_on` | 53,486 | 24.0 | 49.0 | 0 |
| `pump_wood_est_on` | 53,486 | 19.3222 | 49.0 | 0 |
| `wood_fire_on` | 53,290 | 16.5444 | 49.0 | 0 |
| `pump_ww_on` | 53,486 | 11.6556 | 49.0 | 0 |
| `pump_ww_oil_on` | 53,486 | 24.0 | 49.0 | 0 |
| `pump_circ_on` | 53,486 | 24.0 | 49.0 | 0 |
| `pump_hz_on` | 53,486 | 24.0 | 49.0 | 0 |
| `pump_solar_on` | 39,881 | 8.7028 | 49.0 | 0 |
| `valve_hzrl_on` | 53,486 | 24.0 | 49.0 | 0 |
- `pump_wood_est_on` ist ein regelbasierter O1-Pumpen-Proxy, keine gemessene Pumpenlaufzeit; `wood_fire_on` wird separat aus Abgas >100 °C abgeleitet.