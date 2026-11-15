# M1 — SCHEMA: Feature-Store & Daten-Pipeline

Stand: 2026-11-01 · data_version `m1.3` (Änderungen ggb. m1.0: §7, ggb. m1.1: §8, ggb. m1.2: §9) · Zeitzone überall **Europe/Berlin**
Roh-Quellen (read-only): `data/winsol_pq/*/*/*.parquet` (2020-08-30 → 2026-10-27),
`data/cmi_log_pq/*/*/*.parquet` (2026-10-27 → live). Identisches Schema
(geprüft, s. REPORT.md §1) → kein Column-Mapping, nur `source`-Flag.

## 1. Feature-Store-Layout

```
data/features/
├── _meta.json            # Coverage, Bias-Profil, WX-Cache-Übersicht, Versions-Meta (ab m1.1 von `features` geschrieben)
├── wx_cache/             # Open-Meteo-ROH-Cache (separater Raum, nicht Teil des Hive-Stores)
│   ├── era5/YYYY-MM.json # RAW-API-Antwort (archive-api, ERA5)
│   └── hfa/YYYY-MM.json  # RAW-API-Antwort (historical-forecast-api, ICON-D2)
├── 2020/08/2020-08.parquet
├── 2020/09/2020-09.parquet
└── …                     # Hive YYYY/MM/, zstd, 1 Zeile pro Kalenderstunde
```

- **1 Zeile = 1 Kalenderstunde Europe/Berlin**, beginnend 2020-08-31 00:00
  (erste volle Stunde nach Logger-Start 2020-08-30 12:36).
- **Hart-Gaps** (keine 10-s-Rows) sind **keine weggelassenen Zeilen**: Zeile
  existiert mit `gap=1`, State-Spalten = NULL (kein Forward-Fill, keine
  Interpolation; FF ≤ 30 min ist das einzige tolerierte Verhalten, dokumentiert
  als `gap_partial`).
- Pro Datei `key_value_metadata`: `data_version`, `git_tag`, `generated_at`,
  `rows`, `source`.

## 2. Spaltenkatalog (692 Spalten)

### 2.1 Zeit & Kalender (10)

| Spalte | Typ | Bedeutung |
|---|---|---|
| `ts` | TIMESTAMP | Kalenderstunde, **naive Berliner Wallclock** (`YYYY-MM-DD HH:00:00`) |
| `hour` | INT | Stunde des Tages (0–23) |
| `day_of_week` | INT | 0 = Mo … 6 = So |
| `is_weekend` | INT | 1 = Sa/So |
| `is_holiday` | INT | 1 = NRW-Feiertag (`holidays.DE(subdiv="NW")`) |
| `holiday` | VARCHAR | Feiertagsname oder `"none"` |
| `is_dst` | INT | 1 = Sommerzeit (CEST) |

Intern (nur im `hour_grid`-Raster, nicht im Store): `hour_utc` = naive
UTC-Äquivalent (für Wetter-Joins; deterministisch über `zoneinfo` berechnet,
**nicht** via DuckDB-TZ-Funktionen — duckdb 2.0.0 `AT TIME ZONE` verlangt
pytz, IANA-Zonen sind dort unsicher).

### 2.2 Datenqualität & Quelle (5)

| Spalte | Bedeutung |
|---|---|
| `n_rows` | Anzahl 10-s-Rows der Stunde (0 = keine) |
| `gap` | 1, wenn `n_rows < 356` (Lücke; ≤ 30 min = Teil-Lücke) |
| `gap_full` | 1, wenn `n_rows < 60` (State unzuverlässig → State-NULL) |
| `gap_partial` | 1, wenn `60 ≤ n_rows < 356` (State aus verfügbaren Rows) |
| `source` | `winsol_pq` / `cmi_log_pq` (MIN je Stunde) / `none` (Gap) |

### 2.3 State — analoge Kanäle, Stunden-**mean** (12)

`buff_top` a1, `buff_mid` a2, `buff_bot` a3, `ww` a4, `oil_vl` a5, `hz_vl` a6,
`hz_rl` a7, `ext` a8 (CMI-Außensensor, Bias-Referenz), `wood_vl` a9, `flue` a10,
`test` a15, `ww_flow` a16 (alle dev0).
**Abgas-Skalierung (m1.3):** `flue` (dev0 a10) wird mit Faktor 10 in echte °C
umgerechnet. Rohwerte im Kurzschlussfenster 2022-04-28 10:00 bis 2022-05-06 14:00
(Ende exklusiv) und korrigierte Werte >500 °C werden als NULL behandelt; 500 °C
bleibt gültig. Die übrigen TA-Sentinel-Grenzen gelten zusätzlich (§3).
**TA-Sentinel-Maske (m1.2):** analoge Roh-Werte `>= 3276.0` / `<= -999.0`
(TA-Fehlerwerte Fühlerbruch 3276.7 / Kurzschluss -999.9) sind NULL (§3, §8) — gilt für
alle analogen Kanäle dev0/dev1/dev2; betroffen waren `flue` (a10) und `solar_coll`.

### 2.4 State — RAS-Offset-Δ, Stunden-**median** (9)

`offset_kell` = MEDIAN(a1−a2), `offset_eg` = MEDIAN(a4−a5),
`offset_dg` = MEDIAN(a7−a8) (dev1); dazu `ras_*_perc`/`ras_*_ist` (mean, 6)
als Basis-Größen.
**RAS-Maske (m1.1):** vor `RAS_INSTALL_TS` = 2021-10-20 17:00 sind alle RAS-Spalten
NULL (§7).

### 2.5 State — RAS-Datenalter `ras_age_*` (3) + `heat_min_*` (3, DEPRECATED)

dev1 a11/a12/a13 sind **keine Heiz-Minuten**, sondern ein Sägezahn „Zeit seit
letzter Datenübertragung der RAS-Elemente“ (Datenalter, je Zone Keller/EG/DG):
Rohwert steigt um 0,1 je 60 s, Reset auf 0 nach ≈ 1,1 (≈ 11 min); bei
Funkausfall steigt er weiter (99,9-Perzentil des Stunden-Max ab Einbau:
Keller/DG 250, EG 10,2; Maximum 250,0 → vermutlich Sättigung des Zählers).

| Spalte | Bedeutung |
|---|---|
| `ras_age_kell/eg/dg` | **Stunden-MAX** von a11/a12/a13, Rohwert-Einheit. Annahme aus Rohdaten: 0,1 je Minute ⇒ Wert × 10 ≈ Minuten seit letzter RAS-Übertragung (normal ≈ 1,0–1,2 ≈ 11 min; > 3,0 ≈ 30 min = veraltet). NULL vor RAS-Einbau und in Stunden ohne Rohwert. |
| `heat_min_kell/eg/dg` | **DEPRECATED** — keine Heiz-Minuten; nur für v001-Kompatibilität, **nicht als Feature/Target verwenden**. Berechnung wie m1.0: Summe der positiven Deltas a11–a13 innerhalb der Stunde (`SUM(GREATEST(v − LAG(v) OVER (bucket), 0))`) → beim Sägezahn konstruktionsbedingt ≈ 6 je Stunde (P10/P50/P99 = 5,8/5,9/6,1) = informationslos. m1.1: NULL statt 0, wenn die Stunde keinen Rohwert hat (z. B. vor RAS-Einbau). |

**Kein AVG** des Sägezahns (unsinnig + Alias-Konflikt).

### 2.6 State — Digital, **An-Quotient + letzter Zustand** (18)

9 Kanäle × 2: `_on` = AVG (An-Quotient), `_last` = LAST (letztes 10-s-Sample):
`pump_ww`, `mix_open`, `mix_close`, `oel_burner`, `pump_ww_oil`, `pump_circ`,
`pump_hz`, `valve_hzrl` (dev0 d2–d9), `pump_solar` (dev2 d1).

### 2.6b State — analoge Stunden-Endwerte `{feat}_last` (20, m1.1/m1.2)

`{feat}_last` = **letzter Nicht-NULL-10-s-Wert der Stunde**
(`LAST(col ORDER BY ts) FILTER (WHERE col IS NOT NULL)`; eine NULL-Zeile am
Stundenende liefert den letzten gültigen Wert) für:
`ras_kell_perc`, `ras_kell_ist`, `ras_eg_perc`, `ras_eg_ist`, `ras_dg_perc`,
`ras_dg_ist`, `buff_top`, `buff_mid`, `buff_bot`, `hz_vl`, `hz_rl`, `ww`, `ext`,
`oil_vl`, `wood_vl`, `flue`, `solar_coll`, m1.2: `hzrkl_kell_pos`, `hzrkl_eg_pos`,
`hzrkl_dg_pos` (Liste: `config.LAST_VALUE_FEATURES`). TA-Sentinels zählen nicht als
gültiger Wert (m1.2).
Ergänzt den Stunden-Mean (Zustand am Stundenende, z. B. für Übergangs-Targets).
Gap-Verhalten wie alle State-Spalten (direkt aus `hourly_agg`: Stunde ohne Rows
→ NULL; `gap_full` mit 1–59 Rows → Wert aus den vorhandenen Rows, wie Mean).
Keine Namenskollision mit den Digital-`*_last` (§2.6; per Unit-Test geprüft).
Keine Lags auf `{feat}_last`.

### 2.7 State — Solar & RAS2 (7)

`solar_tank_top/mid/bot` (dev2 a1–a3, mean), `ras2_kell_ras/ist`,
`ras2_eg_ras/ist`, `ras2_dg_ras/ist` (dev2 a4–a9, mean; CAN-Transfer,
Konsistenz-Check; m1.1: vor RAS-Einbau NULL), `solar_coll` (dev2 a16, mean;
m1.2: Sentinel 3276.7 am 2022-03-25 maskiert). dev2 a10–a15 → `hzrkl_*` (§2.13).

### 2.8 Flags (6)

| Spalte | Bedeutung |
|---|---|
| `ras_divergence_kell/eg/dg` | 1, wenn \|dev1_ist − dev2_ist\| > 1 K (AVG je Stunde) |
| `offset_known` | 1, wenn dev1 a1 (RAS-Keller-perc) in der Stunde vorhanden (m1.1: vor RAS-Einbau immer 0) |
| `mode_known` | 0 (konstant; Betriebsmodul Q-106 bewusst ausgenommen, kommt später) |
| `wx_is_forecast` | 1 ab ICON-D2-Archivstart (UTC 2022-11-14 00:00), sonst 0 (ERA5-Ist) |
| `data_version` | `m1.3` (Vorstände gesichert unter `data/features_m1.0/`, `data/features_m1.1/`, `data/features_m1.2/`) |
| `git_tag` | Git-Revision des Feature-Builds (z. B. `569a1e593def-dirty`) |
| `generated_at` | ISO-8601-Zeitstempel des Builds |

### 2.9 Sonne (3)

`sunrise_min`, `sunset_min`, `daylight_min` — Minute-of-day Europe/Berlin.
Quelle: **geschlossene NOAA/Meeus-Formel** (alt = −0.833°), deterministisch &
lokal; gegen Open-Meteo daily-API validiert (±2 min, s. REPORT.md). Kein
PyEphem (in v4.2.1 sind `rise_set`/`next_*` entfernt), keine Kopplung an den
Wetter-Cache.

### 2.10 Wetter-Fenster 24 h (336 + 24)

`wx_t{k}_{var}`: Grid-Wert **k Stunden nach** `ts` (k = 0…23, „aktuelle“
Stunde zuerst), 14 Variablen (kurze Namen):

| Spalte | Open-Meteo-Variable |
|---|---|
| `wx_t{k}_t` | temperature_2m |
| `wx_t{k}_dew` | dew_point_2m |
| `wx_t{k}_rh` | relative_humidity_2m |
| `wx_t{k}_cc` | cloudcover |
| `wx_t{k}_cc_low` / `cc_mid` / `cc_high` | cloudcover_low/mid/high |
| `wx_t{k}_prec` | precipitation |
| `wx_t{k}_rain` | rain |
| `wx_t{k}_ws` | wind_speed_10m |
| `wx_t{k}_wg` | wind_gusts_10m |
| `wx_t{k}_wd` | wind_direction_10m |
| `wx_t{k}_swr` | shortwave_radiation |
| `wx_t{k}_wmo` | weather_code |

**Warum kompakte Spalten statt `list<struct>`?** 692 Spalten flach →
DuckDB/Arrow-native, direkt SQL- und ML-Verbrauchbar (pandas-frei),
Spalten-Selektion in Parquet ohne Dekodieren; 24×14 = 336 + 24 Bias-Spalten
sind statisch bekannt. `is_freezing` wird im Modell-Training abgeleitet
(`wx_t{k}_t < 0`), nicht gespeichert (redundant mit `t`).

### 2.11 Bias-korrigierte Temperatur (24)

`wx_t{k}_t_bias` = Roh-Grid-T + **saisonales Bias-Profil**
(`analysis/model/m1/bias_profile.json`):
`bias = median(sensor − grid)` + `drift · tage_ab_saisonstart`, je Saison
DJF/MAM/JJA/SON (Sensor = dev0 a8-Stundenmittel, grid = T2m).
Ohne Profil: `wx_t{k}_t_bias = wx_t{k}_t` (identisch, NULL-safe).
**Roh-Grid-T bleibt immer in `wx_t{k}_t`** — beide Varianten im Store.

### 2.12 Lags (170)

Für 31 State-Spalten (`features.LAG_COLUMNS`: RAS perc/ist/offset ×3, `hz_vl`,
`hz_rl`, `buff_*`, `ww`, `solar_coll`, `wood_vl`, `flue`, `oil_vl`, `ext`,
`oel_burner_on`, `pump_hz_on`, `heat_min_*` — letztere DEPRECATED, nur
v001-Kompatibilität —, m1.2: `hzrkl_{kell,eg,dg}_on/pos`, m1.3:
`hzrkl_{kell,eg,dg}_rule`):
`{col}_lag1`, `_lag2`, `_lag6`, `_lag12`, `_lag24`.
Keine Lags auf `ras_age_*` / `{feat}_last` / `hzrkl_{z}_last`; RAS-Lags, die
auf Stunden vor dem RAS-Einbau zeigen, sind NULL.
Semantik: **Index-Schritte auf dem lückenlosen Stunden-Raster**
(Kalenderstunde, nie Wall-Clock-Arithmetic) → DST-sicher
(Fallback-Tag zählt 1 Bucket, kein Duplikat). Rand: vor Raster-Start = NULL.

### 2.13 State — Rücklaufminderer je Etage `hzrkl_*` (12, m1.2)

Quelle: `docs/plant-model.md` §3.3 (UVR610 A10–A15, BDB-Labels „Keller/EG/OG HzRkl
Ventil“ im `.dat`-Image 2022-09-18) + Messbefund in `winsol_pq`.

| Spalte | Roh-Kanal | Bedeutung |
|---|---|---|
| `hzrkl_kell_on` / `hzrkl_eg_on` / `hzrkl_dg_on` | dev2 a10 / a11 / a12 („HzRkl Ventil 2 Ergebnis“ Keller/EG/OG) | Rohwert **binär 0 oder 10** → Stunden-AVG(Rohwert / 10) = **An-Quotient 0..1** (wie Digital-`_on`) |
| `hzrkl_kell_last` / `hzrkl_eg_last` / `hzrkl_dg_last` | dto. | letzter Nicht-NULL-Rohwert der Stunde / 10 (0 oder 1) |
| `hzrkl_kell_pos` / `hzrkl_eg_pos` / `hzrkl_dg_pos` | dev2 **a13 / a15 / a14** („HzRkl Ventil 1“ Keller / EG / OG) | Stunden-Mean, Rohwert-Einheit (Setup: %), stetig ≈ 11–65. **Achtung Reihenfolge:** a14 = OG → `dg`, a15 = EG → `eg`. |
| `hzrkl_kell_pos_last` / `hzrkl_eg_pos_last` / `hzrkl_dg_pos_last` | dto. | letzter Nicht-NULL-10-s-Wert der Stunde (§2.6b) |

- Werte erst ab **2022-03-06 13:00** (`_on`) bzw. **2022-03-07 15:00** (`_pos`), davor NULL
  (Roh-Kanäle NULL; keine explizite Maske nötig).
- **CMI-Live-Zeitraum:** `cmi_log_pq` enthält derzeit keine zugeordneten Werte für
  dev2 a10–a12. Das belegt eine Lücke im Parquet-Feature-Eingang, aber nicht, ob die
  Roh-`/LOG`-Nutzdaten diese Kanäle enthalten; der Decoder ordnet nur einen festen
  Analogkanal-Satz zu (Q-112b bleibt offen, kein Parser-Fix in diesem Ticket).
  Die JSON-API-Quelle in `history.db` enthält laut Prüfung json:32 ch10–12; für die
  L1-Inferenz werden die Minderer deshalb aus der bestätigten Regel in §2.14 abgeleitet.
- Gap-Verhalten wie alle State-Spalten; Lags auf `_on` und `_pos` (§2.12).
- Plausibilität (Heizsaison 2024-11..2025-02): An-Anteil Keller/EG/DG ≈ 22 % / 90 % / 77 %;
  corr(`hzrkl_{z}_on`, `ras_{z}_perc`) = −0,64 / −0,49 / −0,56 (alle Stunden: −0,60 / −0,71 / −0,63).
- **Semantik `_pos`:** Messwerte a13–a15 entsprechen Temperaturen am Minderer (°C),
  nicht einer Ventilstellung. Historische Spaltennamen bleiben unverändert, damit
  v002 und alte Store-Verbraucher kompatibel bleiben.

### 2.14 Abgeleitete UVR-Regeln und Laufzeitmerkmale (69 neue Spalten inkl. 15 Regel-Lags, m1.3)

| Spalte | Definition |
|---|---|
| `hzrkl_{z}_rule` / `_rule_last` | Geschätzter Minderer-Zustand aus `ras_{z}_perc`: offen (1) unter 20,1 °C, geschlossen (0) darüber; bei genau 20,1 °C und bei NULL den letzten Zustand halten. Stundenmittel bzw. letzter Zustand. Die Zustands-Lags 1/2/6/12/24 h stehen ebenfalls im Store. |
| `o9_switch_margin` | `buff_mid - hz_rl - 5 K`; positiv bedeutet nach der UVR-Schaltbedingung, dass der Heizungsrücklauf in den Puffer geleitet werden kann (O9 EIN). |
| `pump_wood_est_on` | O1-Holzpumpen-Proxy: 1, wenn Holz-Vorlauf >65 °C, mehr als 3 K über Puffer unten und Puffer unten <90 °C; kein gemessenes O1-Signal. |
| `wood_fire_on` | Holzbrand-Proxy: 1, wenn korrigiertes Abgas >100 °C; separat vom O1-Pumpen-Proxy. |
| `{signal}_sum{3,6,12,24}h` | Rollierende Summe stündlicher An-Quotienten (Zeilen inkl. aktueller Stunde); bei vollständigen Werten entspricht sie An-Stunden. Signale: `oel_burner_on`, `pump_wood_est_on`, `wood_fire_on`, `pump_ww_on` (O2), `pump_ww_oil_on` (O6), `pump_circ_on` (O7), `pump_hz_on`, `pump_solar_on`, `valve_hzrl_on` (O9). NULLs in einem Fenster werden von `SUM` ignoriert. |
| `{signal}_since_h` | Stunden seit dem letzten An-Quotienten >0,05; 0 in einer aktiven Stunde, maximal 48 bei Aktivität im zurückliegenden Fenster, 49 als „in 49 Stunden kein aktiver Wert bekannt“. |

Die UVR-Eingangsgrößen sind beobachtete Istwerte bzw. gekennzeichnete Proxies;
geplante zukünftige Schaltungen oder künftige Warmwasserzapfungen werden nicht
simuliert. Die Simulation geplanter Steueraktionen bleibt ein eigener Folgeschritt.

## 3. Resampling-Semantik (10 s → 1 h)

- Bucket = `substr(ts,1,13) || ':00'` (Wallclock-Trunk = Berliner Kalenderstunde).
- Aggregation: analog = **AVG** (+ **LAST non-NULL** für `{feat}_last`),
  Offset-Δ = **MEDIAN**, Digital = **AVG + LAST**, RAS-Datenalter = **MAX**,
  `heat_min_*` (DEPRECATED) = **SUM positiver Deltas**, Divergenz = **AVG des 0/1-Flags**.
- **RAS-Maske (m1.1)** bereits in `src`: RAS-Roh-Kanäle (`config.RAS_RAW_COLUMNS`
  = dev1 a1,a2,a4,a5,a7,a8,a11–a13 + dev2 a4–a9 → `ras2_*`) mit Roh-ts
  < `RAS_INSTALL_TS` → NULL (String-Vergleich, Roh-ts hat feste Länge).
- **TA-Sentinel-Maske (m1.2)** bereits in `src`: jeder analoge Roh-Kanal
  (`resample.ANALOG_SOURCE_COLUMNS`, 37 Kanäle dev0/dev1/dev2) mit
  `v >= config.SENTINEL_MAX (3276.0)` oder `v <= config.SENTINEL_MIN (-999.0)` → NULL
  (`resample.sentinel_condition`). Die Zeile bleibt erhalten (`n_rows` unverändert; der
  `WHERE`-Zeilenfilter referenziert die Roh-Spalten). Mean/`_last`/Median/Max/Lags
  ignorieren den Wert automatisch.
- `hzrkl_{z}_on` = AVG(a10–a12 / 10), `hzrkl_{z}_last` = LAST(a / 10) FILTER non-NULL.
- 6-View-Aufbau: `src` (Union winsol+cmi, RAS- + Sentinel- + Abgasmaske und -Skala)
  → `src_bucketed` → `src_delta` (Deltas und Minderer-Ereignisse) → `src_rules`
  (vorwärts gehaltener Mindererzustand; `LAST_VALUE IGNORE NULLS`) → `hourly_agg`
  → `features_final` (Laufzeitfenster). Die Mindererregel folgt aus den 10-s-RAS-Werten,
  bevor stündliche Werte aggregiert werden; damit bleiben Grenzwerte und Zustands-Halten
  zeitlich korrekt. `ras_age_*` (MAX), `{feat}_last` und die übrigen Mindererwerte sind
  reine Aggregate in `hourly_agg` (kein zusätzliches Window); die
  Kombination mit den Deltas (inkl. Sentinels/hzrkl) sichert ein
  Mehrstunden-Python-Referenztest ab.
- **DST-Fallback-Tag 2024-03-31**: Logger-Wallclock ergibt `02:xx` = 300 Rows,
  `03:xx` = 60 Rows (RAS-Logger stellte um) → beide Buckets existieren,
  kein Duplikat, kein verschobener Bucket (geprüft + Unit-Test). Rückfall-Tag
  2024-10-27: alle Buckets 360 Rows (Logger bleibt auf Wallclock).

## 4. Wetter-Backfill (Open-Meteo)

- Koordinaten **51.382974, 7.774286**; alle Requests `timezone=UTC`
  (Open-Meteo IANA-Parameter ist DST-unsicher — liefert konstant GMT+1).
- **ERA5** (`archive-api.open-meteo.com`, `models=era5`): 2020-08-01 →
  2022-11-13 (28 Monats-Chunks).
- **ICON-D2** (`historical-forecast-api.open-meteo.com`, `models=icon_d2`):
  2022-11-14 → Datenende (48 Monats-Chunks). *Verifiziert*: ICON-D2-Archiv
  startet vollständig am 2022-11-14 (2022-11-01/13 → 0–10/24 h NULL);
  `2022-11`-Chunk trägt 01–13 als NULL → Split-Flag `wx_is_forecast` ab
  2022-11-14 00:00 UTC.
- Fair-Use: sequentiell, 2 s Sleep/Call, Retry ×3 (Backoff 2/4/8 s),
  Resume über Cache (`is_valid_cache`: existiert + ≥ 200 B + JSON + `hourly`).
- Sun (daily sunrise/sunset) in denselben Calls (0 Extra-Requests).

## 5. Bias-Profil

`analysis/model/m1/bias_profile.json`:
```json
{"profile": {"DJF": {"offset_k": …, "drift_k_per_day": …, "n_hours": …, "season_hours": …}, …},
 "n_hours_total": …, "grid_coverage": {…}}
```
Methode: stündlich `resid = ext − grid_T` (gültig, wenn beide ∈ [−40, 60] K);
je Saison **Median** (Offset) + **OLS-Steigung** (Drift, K/Tag ab
Saisonstart). Season = DJF (Dez→Feb, Dez zählt ins Vorjahr), MAM, JJA, SON.

## 6. Reprodzierbarkeit

```bash
.venv/bin/python -m analysis.model.m1 weather   # Backfill (Fair-Use)
.venv/bin/python -m analysis.model.m1 bias      # bias_profile.json
.venv/bin/python -m analysis.model.m1 features  # data/features/ (Hive) + _meta.json, nur lokaler WX-Cache
.venv/bin/python -m analysis.model.m1 report    # REPORT.md
.venv/bin/python -m analysis.model.m1 all       # die obere Reihe
```

## 7. Datenversion m1.1 (M2b-P2, 2026-10-31)

Fakten vom Anlagenbesitzer bestätigt (2026-10-31):

1. **RAS-Einbau:** Die RAS-Raumsensoren wurden erst im Oktober 2021 eingebaut.
   Im m1.0-Store ist der letzte Stunden-Bucket mit `ras_kell_ist < 5 °C`
   `2021-10-20 15:00`, der erste plausible `2021-10-20 17:00` →
   `config.RAS_INSTALL_TS = "2021-10-20 17:00:00"`. Alle RAS-Rohwerte davor sind
   Müll und werden in der `src`-View als NULL behandelt. Folge: perc/ist/offset/
   `ras2_*`/`ras_divergence_*`/`ras_age_*`/`heat_min_*`/RAS-`_last` und deren
   Lags sind vor dem Einbau NULL, `offset_known = 0`.
2. **a11–a13 = Datenalter, keine Heiz-Minuten:** neue Spalten `ras_age_*`
   (§2.5); `heat_min_*` bleibt mit unveränderter Berechnung erhalten, ist aber
   **DEPRECATED** (nur v001-Kompatibilität, nicht als Feature/Target verwenden).
3. **Neu:** 17 analoge Stunden-Endwerte `{feat}_last` (§2.6b).

Neue Spalten (20): `ras_age_kell`, `ras_age_eg`, `ras_age_dg`,
`ras_kell_perc_last`, `ras_kell_ist_last`, `ras_eg_perc_last`, `ras_eg_ist_last`,
`ras_dg_perc_last`, `ras_dg_ist_last`, `buff_top_last`, `buff_mid_last`,
`buff_bot_last`, `hz_vl_last`, `hz_rl_last`, `ww_last`, `ext_last`, `oil_vl_last`,
`wood_vl_last`, `flue_last`, `solar_coll_last` (561 → 581 Spalten). Keine Spalte
entfernt/umbenannt; Zeilenraster unverändert.

Hinweise für Konsumenten (z. B. M2/v001, trainiert auf m1.0):
- RAS-Features/Targets vor 2021-10-20 17:00 sind jetzt NULL (m1.0: Müllwerte,
  u. a. `ras_kell_ist = 0`).
- Nicht-RAS-Spalten und RAS-Spalten ab Einbau sind gegenüber m1.0 unverändert
  (max. Abweichung ≤ 1e-9 = Float-Summationsreihenfolge, keine NULL-Abweichungen).
- Backup des m1.0-Stores: `data/features_m1.0/` (inkl. `wx_cache`).
- Verifikationszahlen: REPORT.md §7 (`report.m1_1_checks`).

## 8. Datenversion m1.2 (2026-10-31)

1. **TA-Sentinel-Maske (Bugfix):** Die UVR schreibt bei Fühlerbruch `3276.7`, bei
   Kurzschluss `-999.9` in den Analogkanal. Zentrale Regel `config.SENTINEL_MAX = 3276.0`,
   `config.SENTINEL_MIN = -999.0`: jeder analoge Roh-Wert `>= MAX` oder `<= MIN` ist in der
   `src`-View NULL (§3). Maskierte Samples (Roh-Scan aller analogen Kanäle, winsol + cmi):
   - `dev0_analog_10` (`flue`): 9.383 × 3276.7 (2021-12-15 15:53 → 2022-05-06 13:11) +
     2.190 × -999.9 (2022-04-28 10:25 → 2022-05-06 13:07);
   - `dev2_analog_16` (`solar_coll`): 556 × 3276.7 (2022-03-25 08:55 → 10:27);
   - alle übrigen analogen Kanäle (auch unbenutzte, cmi_log_pq): 0.
   Folge: max(`flue`) 624,1 → 163,2; max(`solar_coll`) 3276,7 → 179,6; max(`flue_last`)
   3276,7 → 248,3. Nur `flue*`/`solar_coll*` (inkl. Lags/`_last`) ändern sich ggb. m1.1;
   1 Stunde `solar_coll` (2022-03-25 09:00, nur Sentinels) ist jetzt NULL.
   **Nicht maskiert (Regel greift nicht):** 2.791 `flue`-Samples zwischen −84,8 und −50
   (+ 5.910 zwischen −50 und 0), ausschließlich im Kurzschluss-Fenster 2022-04-28 →
   2022-05-06 → 7 Stunden mit `flue_last < -50`. Vermutlich ebenfalls Fühlerdefekt;
   bei Bedarf Zeitfenster-Maske für `flue` (offen).
2. **Rücklaufminderer je Etage (neu, §2.13):** `hzrkl_{z}_on`/`_last` (dev2 a10–a12),
   `hzrkl_{z}_pos`/`_pos_last` (dev2 a13/a15/a14), Lags auf `_on`/`_pos`.

Neue Spalten (42): `hzrkl_kell_on`, `hzrkl_eg_on`, `hzrkl_dg_on`, `hzrkl_kell_last`,
`hzrkl_eg_last`, `hzrkl_dg_last`, `hzrkl_kell_pos`, `hzrkl_eg_pos`, `hzrkl_dg_pos`,
`hzrkl_kell_pos_last`, `hzrkl_eg_pos_last`, `hzrkl_dg_pos_last` + 30 Lags
`hzrkl_{z}_{on,pos}_lag{1,2,6,12,24}` (581 → 623 Spalten). Keine Spalte entfernt/umbenannt;
Zeilenraster unverändert. Roh-Spalten `dev2_analog_10..15` neu in
`resample.SOURCE_COLUMNS` (in winsol_pq und cmi_log_pq vorhanden).

Hinweise für Konsumenten:
- `flue`/`solar_coll` (und deren Lags/`_last`) in 2021-12 … 2022-05 bzw. am 2022-03-25
  weichen von m1.1 ab (Sentinels raus); alle übrigen gemeinsamen Spalten sind identisch
  (max. Abweichung ≤ 1e-9, keine NULL-Abweichung).
- `hzrkl_*_on` ist im CMI-Live-Zeitraum NULL: a10–a12 **stehen im CMI-Log**
  (WinSol zeigt sie; WinSol = einzige Quelle CMI-Log), werden aber von der
  Gateway-`/LOG`-Ingestion (`cmi_log_pq`) nicht erfasst (Ingestions-Lücke,
  kein Fehlen in der Quelle; Fix: `/LOG`-Parsing um a10–a12 erweitern, Q-112b) → für
  Live-Inferenz derzeit nicht verfügbar.
- Backup des m1.1-Stores: `data/features_m1.1/` (inkl. `wx_cache`, `_meta.json`).
- Verifikationszahlen: REPORT.md §8 (`report.m1_2_checks`, `report.sentinel_counts`).

## 9. Datenversion m1.3 (2026-11-01)

1. **Abgas korrigiert:** `flue` und `flue_last` in °C = Rohwert ×10. Das nachgewiesene
   Kurzschlussfenster 2022-04-28 10:00 bis 2022-05-06 14:00 (Ende exklusiv) ist maskiert.
   Korrigierte Werte >500 °C werden als Fühlerfehler verworfen; genau 500 °C bleibt gültig.
   Die allgemeine TA-Sentinel-Regel aus §8 bleibt aktiv.
2. **Anlagenregeln / Proxies:** Mindererregel aus `ras_{z}_perc`: <20,1 °C offen, >20,1 °C
   geschlossen, Gleichheit oder NULL hält den Zustand. O9-Schaltmarge ist
   `buff_mid - hz_rl - 5 K`; O9 EIN bedeutet Rücklauf → Puffer. O2 lädt den Puffer oben aus
   dem Ölkessel. O6 (Zirkulations-Wärmetauscher) ist an bedarfsabhängige O7 gekoppelt.
   Holzpumpe O1 wird nicht gemessen: `pump_wood_est_on` folgt der UVR-Regel
   (`wood_vl >65 °C`, `wood_vl > buff_bot +3 K`, `buff_bot <90 °C`); `wood_fire_on`
   ist ein separater Abgas-Proxy (>100 °C).
3. **Laufzeit-Kontext:** neun gemessene/proxy Signale erhalten `*_sum3h/6h/12h/24h`
   und `*_since_h`. Die Summen addieren stündliche An-Quotienten; `since_h=49` bedeutet,
   dass innerhalb der rückblickenden 49 Stunden kein aktiver Wert vorliegt. Fehlende
   künftige Schaltungen/Zapfungen werden nicht simuliert.
4. **Neue Store-Spalten:** 6 `hzrkl_{z}_rule`/`_rule_last`, 1 `o9_switch_margin`, 2
   Holz-Proxies, 36 Laufzeitsummen, 9 Aktivitätsalter und 15 Lags der Mindererregel
   (623 → 692 Spalten). Keine Spalte entfernt oder umbenannt; Zeilenraster unverändert.
5. **Live-Eingang:** dev2 a10–a12 sind im aktuellen `cmi_log_pq` nicht verfügbar; ob die
   Roh-`/LOG`-Nutzdaten sie enthalten, ist mit Q-112b noch nicht geklärt. Der bekannte
   Minderer-Schaltzustand wird für Live-Feature-Builds aus der RAS-Regel abgeleitet.
6. **Backup und Verifikation:** m1.2 bleibt unter `data/features_m1.2/` erhalten;
   M1.3-Zahlen (Rebuild, Abgas-Masken, Minderer-Übereinstimmung, Feature-Metadaten) stehen
   in `REPORT.md` §9.
