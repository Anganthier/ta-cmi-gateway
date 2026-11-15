"""Feature-Store-Builder (Hive ``data/features/YYYY/MM.parquet``, zstd).

Pipeline (s. SCHEMA.md):
1. ``hour_grid`` (Python/pyarrow): alle Kalenderstunden Europe/Berlin 2020-08-31
   00:00 → letzte volle Stunde mit Daten (winsol_pq → cmi_log_pq, lückenlos).
   ``ts`` = naive Wallclock Berlin; ``hour_utc`` = aware UTC (für Wetter-Joins).
2. ``hourly_agg`` (DuckDB): stündliche Aggregation der Roh-Quellen
   (mean + last-non-NULL / median-OffsetΔ / An-Quotient+last / max-Datenalter /
   Sum-positiver-Deltas (DEPRECATED ``heat_min_*``) / Divergenz / Rücklaufminderer
   ``hzrkl_*``). RAS-Roh-Kanäle vor ``config.RAS_INSTALL_TS`` (m1.1) und
   TA-Sentinel-Werte aller analogen Roh-Kanäle (m1.2) sind bereits in der
   ``src``-View NULL.
3. ``wx_wide`` (DuckDB): Open-Meteo-Roh-Cache (ERA5 bis 2022-11-13, ICON-D2 ab
   2022-11-14) pro UTC-Stunde ge-pivottet (14 Variablen) → **24 Equi-Joins**
   ``wx_wide w{k} ON w{k}.hour_utc = at_time_zone(g.ts,'Europe/Berlin') + k h``
   liefern das 24-h-Fenster (t→t+23) als ``wx_t{k}_{var}`` (336 Spalten).
   (Kein Range-Join: 2× schneller als Range-Join+Pivot, exakt äquivalent —
   Benchmark s. REPORT.md; DuckDB 2.0.0 kann nicht INTERVAL/INTERVAL dividieren.)
4. Bias-Korrektur: ``wx_t{k}_t_bias`` = Grid-T + saisonales Bias-Profil
   (``bias_profile.json``), falls vorhanden (sonst = Roh-Grid-T).
5. Lags 1/2/6/12/24 h (SQL-Window ``LAG(col, k) OVER w``) auf ``LAG_COLUMNS``
   (m1.2: ``hzrkl_{z}_on/pos``, m1.3: abgeleiteter Minderer-Regelzustand;
   keine Lags auf ``ras_age_*``/``{feat}_last``/``hzrkl_{z}_last``).
6. Flags: gap / gap_partial / gap_full / mode_known=0 / offset_known /
   ras_divergence_* / wx_is_forecast / source / data_version / git_tag /
   generated_at.
7. Sun: sunrise/sunset (Minute-of-day) aus NOAA-Formel (``calendar_utils.sun_times``).

Schreibweise: pro Monat via pyarrow (zstd) + ``key_value_metadata``-Block
(data_version, git_tag, generated_at, rows). Meta-Summary →
``data/features/_meta.json``.
"""

from __future__ import annotations

import calendar
import contextlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from . import config, resample
from .calendar_utils import BERLIN, calendar_features, iter_hours, sun_times
from .weather import cache_path

# State-Spalten, für die Lags 1/2/6/12/24 h gebildet werden.
LAG_COLUMNS: list[str] = [
    "ras_kell_perc",
    "ras_kell_ist",
    "offset_kell",
    "ras_eg_perc",
    "ras_eg_ist",
    "offset_eg",
    "ras_dg_perc",
    "ras_dg_ist",
    "offset_dg",
    "hz_vl",
    "hz_rl",
    "buff_top",
    "buff_mid",
    "buff_bot",
    "ww",
    "solar_coll",
    "wood_vl",
    "flue",
    "oil_vl",
    "ext",
    "oel_burner_on",  # d5 Ölbrenner
    "pump_hz_on",  # d8 Pumpe Heizkreis
    # DEPRECATED (m1.1): keine Heiz-Minuten (Datenalter-Sägezahn); Lags nur für
    # v001-Kompatibilität erhalten, NICHT als Feature/Target verwenden.
    "heat_min_kell",
    "heat_min_eg",
    "heat_min_dg",
    # m1.2: Rücklaufminderer je Etage (An-Quotient a10–a12, Ventilstellung a13–a15)
    "hzrkl_kell_on",
    "hzrkl_eg_on",
    "hzrkl_dg_on",
    "hzrkl_kell_pos",
    "hzrkl_eg_pos",
    "hzrkl_dg_pos",
    # m1.3: UVR-Mindererregel aus der perzipierten Temperatur, mit Zustandserhalt.
    "hzrkl_kell_rule",
    "hzrkl_eg_rule",
    "hzrkl_dg_rule",
]

DATA_VERSION = config.DATA_VERSION
# Split-UTC: ICON-D2-Archivstart (2022-11-14 00:00 UTC; 2022-11-13 = 10/24 h, unvollständig).
WX_SPLIT_UTC = datetime(2022, 11, 14, 0, 0, tzinfo=UTC)
# Stunden mit < GAP_MIN_ROWS 10-s-Rows: Zustand unzuverlässig → State-NULL (harte Lücke).
GAP_MIN_ROWS = 60
# Stunden mit < GAP_FULL_ROWS 10-s-Rows: Partial (Flag; State aus verfügbaren Rows).
GAP_FULL_ROWS = 356


def last_full_hour_wall(last_ts: datetime) -> datetime:
    """Letzte volle Kalenderstunde (Wallclock-Berlin) ab ``last_ts`` (Wallclock)."""
    return datetime(last_ts.year, last_ts.month, last_ts.day, last_ts.hour, tzinfo=BERLIN)


# --- Gemeinsame Bausteine: Grid, Sun, Wetter (Phasen A–D der Pipeline) ---------


def build_hour_grid(start: datetime, end: datetime) -> pa.Table:
    """Stunden-Raster (Europe/Berlin-Wallclock) mit UTC-Äquivalent + Kalender-Features.

    ``start``/``end`` = aware Europe/Berlin, volle Stunden (inklusiv).
    Beide Datetime-Spalten sind NAIVE:
    - ``ts`` = Berliner Wallclock (Joins gegen ``hourly_agg.ts_bucket``);
    - ``hour_utc`` = UTC-Wallclock (Wetter-/Bias-Joins, exakt 0/+1 h zu ``ts``
      über die Zone hinweg). Naive UTC spart alle DuckDB-TZ-Funktionen
      (``AT TIME ZONE`` braucht in duckdb 2.0.0 das pytz-Modul — unnötig,
      weil das UTC-Äquivalent deterministisch in Python berechnet wird).
    ACHTUNG: ``pa.array`` konvertiert aware-Datetimes still zu UTC-Wallclock →
    ``ts`` MUSS ``replace(tzinfo=None)`` sein (sonst UTC statt Berlin).
    """
    ts_list: list[datetime] = []
    hour_utc_list: list[datetime] = []
    hour_of_day: list[int] = []
    dow: list[int] = []
    weekend: list[int] = []
    holiday_flag: list[int] = []
    holiday_name: list[str] = []
    dst: list[int] = []
    for h in iter_hours(start, end):
        ts_list.append(h.replace(tzinfo=None))  # naive Berliner Wallclock
        hour_utc_list.append(h.astimezone(UTC).replace(tzinfo=None))  # naive UTC
        d = h.date()
        cf = calendar_features(d)
        hour_of_day.append(h.hour)
        dow.append(int(cf["day_of_week"]))
        weekend.append(int(cf["is_weekend"]))
        holiday_flag.append(int(cf["is_holiday"]))
        holiday_name.append(str(cf["holiday"]))
        dst.append(int(cf["is_dst"]))
    return pa.table(
        {
            "ts": pa.array(ts_list, type=pa.timestamp("us")),
            "hour_utc": pa.array(hour_utc_list, type=pa.timestamp("us")),
            "hour": pa.array(hour_of_day, type=pa.int64()),
            "day_of_week": pa.array(dow, type=pa.int64()),
            "is_weekend": pa.array(weekend, type=pa.int64()),
            "is_holiday": pa.array(holiday_flag, type=pa.int64()),
            "holiday": pa.array(holiday_name, type=pa.string()),
            "is_dst": pa.array(dst, type=pa.int64()),
        }
    )


def sun_features_table(days: list[date]) -> pa.Table:
    """Sunrise/Sunset (Minute-of-day, Europe/Berlin) pro Tag (NOAA-Formel).

    Returns Table (day DATE, sunrise_min, sunset_min, daylight_min).
    Fehlender Tag / Polarexrem → -1.
    """
    out: dict[date, tuple[int, int]] = {}
    for day in days:
        st = sun_times(day)
        if st.sunrise is None or st.sunset is None:
            out[day] = (-1, -1)
            continue
        out[day] = (
            st.sunrise.hour * 60 + st.sunrise.minute,
            st.sunset.hour * 60 + st.sunset.minute,
        )
    rise = [v[0] for v in out.values()]
    set_ = [v[1] for v in out.values()]
    daylen = [s - r if (r >= 0 and s > r) else -1 for r, s in zip(rise, set_, strict=True)]
    return pa.table(
        {
            "day": pa.array(list(out.keys()), type=pa.date32()),
            "sunrise_min": pa.array(rise, type=pa.int64()),
            "sunset_min": pa.array(set_, type=pa.int64()),
            "daylight_min": pa.array(daylen, type=pa.int64()),
        }
    )


def load_wx_long(
    cache_dir: Path,
    era5_months: list[tuple[int, int]],
    hfa_months: list[tuple[int, int]],
) -> pa.Table:
    """Lädt den Open-Meteo-Roh-Cache → Long-Table (hour_utc UTC-aware, src, var, value).

    ERA5-Monate: nur Stunden < ``WX_SPLIT_UTC``. HFA-Monate (ICON-D2):
    nur Stunden >= ``WX_SPLIT_UTC``. Fehlende/leere Monate → keine Rows.
    """
    hour_col: list[datetime] = []
    src_col: list[str] = []
    var_col: list[str] = []
    val_col: list[float] = []
    for api, months, use_before in (("era5", era5_months, True), ("hfa", hfa_months, False)):
        for y, m in months:
            p = cache_path(cache_dir, api, y, m)
            if not p.exists():
                continue
            with p.open("rb") as f:
                data = json.load(f)
            times = data.get("hourly", {}).get("time")
            if not times:
                continue
            for var in config.WX_HOURLY_VARS:
                vals = data["hourly"].get(var)
                if vals is None:
                    continue
                for i, t in enumerate(times):
                    h = datetime.fromisoformat(t).replace(tzinfo=UTC)
                    if use_before and not (h < WX_SPLIT_UTC):
                        continue
                    if not use_before and not (h >= WX_SPLIT_UTC):
                        continue
                    v = vals[i]
                    if v is None:
                        continue
                    hour_col.append(h)  # aware UTC
                    src_col.append("era5" if use_before else "icon_d2")
                    var_col.append(var)
                    val_col.append(float(v))
    return pa.table(
        {
            "hour_utc": pa.array(
                [x.replace(tzinfo=None) for x in hour_col], type=pa.timestamp("us")
            ),  # naive UTC-Wallclock (Joins gegen naive g.hour_utc)
            "src": pa.array(src_col, type=pa.string()),
            "var": pa.array(var_col, type=pa.string()),
            "value": pa.array(val_col, type=pa.float64()),
        }
    )


def wx_wide_sql() -> str:
    """SQL: Table ``wx_wide`` (hour_utc + 14 Variablen) aus registriertem ``wx_long``.

    1 Row je UTC-Stunde; NULL wenn die Variable im Cache fehlt (z.B. Nullwerte
    des API-Chunks). Kein INTERVAL/INTERVAL — nur Gruppierung (DuckDB 2.0.0-kompatibel).
    """
    var_cols = ",\n    ".join(
        f"MAX(CASE WHEN var = '{var}' THEN value END) AS {var}" for var in config.WX_HOURLY_VARS
    )
    return f"""
CREATE OR REPLACE TABLE wx_wide AS
SELECT hour_utc,
    {var_cols}
FROM wx_long
GROUP BY hour_utc
"""


def wx_pivot_sql() -> str:
    """SQL: view ``wx_pivot`` (hour_utc + 24×14 ``wx_t{k}_{var}``) aus ``wx_window``.

    Alternative zum equi-join-Verfahren in ``assemble_features`` (gleiche
    Semantik, langsamer in DuckDB 2.0.0); v.a. für Tests.
    """
    cols: list[str] = []
    for k in range(config.WX_WINDOW_H):
        for var, short in config.WX_SHORT.items():
            cols.append(
                f"MAX(CASE WHEN var = '{var}' AND k = {k} THEN value END) AS wx_t{k}_{short}"
            )
    return f"""
CREATE OR REPLACE VIEW wx_pivot AS
SELECT hour_utc, {", ".join(cols)}
FROM wx_window
WHERE hour_utc IS NOT NULL
GROUP BY hour_utc
"""


def bias_terms(bias_profile: dict | None, n_hours: int) -> list[str]:
    """SQL-Ausdrücke ``wx_t{k}_t_bias`` (Roh-Grid-T + saisonales Bias-Profil).

    Ohne Bias-Profil: ``wx_t{k}_t_bias = wx_t{k}_t`` (identisch, dokumentiert).
    Bezug = ``g.ts`` (Europe/Berlin-Kalenderstunde der Feature-Zeile).
    """
    if not bias_profile:
        return [f"wx_t{k}_t AS wx_t{k}_t_bias" for k in range(n_hours)]
    season_expr = """CASE
        WHEN month(g.ts) IN (12, 1, 2) THEN 'DJF'
        WHEN month(g.ts) IN (3, 4, 5) THEN 'MAM'
        WHEN month(g.ts) IN (6, 7, 8) THEN 'JJA'
        ELSE 'SON' END"""
    season_start_expr = """CASE
        WHEN month(g.ts) = 12 THEN make_date(year(g.ts) - 1, 12, 1)
        WHEN month(g.ts) IN (1, 2) THEN make_date(year(g.ts), 1, 1)
        WHEN month(g.ts) IN (3, 4, 5) THEN make_date(year(g.ts), 3, 1)
        WHEN month(g.ts) IN (6, 7, 8) THEN make_date(year(g.ts), 6, 1)
        ELSE make_date(year(g.ts), 9, 1) END"""
    offset_terms = []
    drift_terms = []
    for s in config.BIAS_SEASONS:
        prof = bias_profile.get(s) or {}
        off = float(prof.get("offset_k", 0.0))
        drift = float(prof.get("drift_k_per_day", 0.0))
        offset_terms.append(f"WHEN {season_expr} = '{s}' THEN {off!r}")
        drift_terms.append(f"WHEN {season_expr} = '{s}' THEN {drift!r}")
    bias_case_offset = "CASE " + " ".join(offset_terms) + " ELSE 0 END"
    bias_case_drift = "CASE " + " ".join(drift_terms) + " ELSE 0 END"
    out: list[str] = []
    for k in range(n_hours):
        out.append(
            f"CASE WHEN wx_t{k}_t IS NULL THEN NULL "
            f"ELSE wx_t{k}_t + ({bias_case_offset}) + ({bias_case_drift}) * "
            f"datediff('day', {season_start_expr}, g.ts::DATE) END AS wx_t{k}_t_bias"
        )
    return out


def _cmi_glob_exists(cmi_dir: Path) -> bool:
    """True, wenn ``cmi_log_pq`` zumindest ein Monats-Parquet enthält.

    (``Path.glob`` mit dem wörtlichen Muster ``*/*/*.parquet`` findet NICHTS —
    das Muster darf nicht als Pfad übergeben werden, sondern muss an die
    Basis-Directory übergeben werden.)
    """
    return any(cmi_dir.glob("*/*/*.parquet"))


# --- M1-Inkremental (2026-11): aktueller Monat + 48-h-Kontext des Vormonats ---

CONTEXT_HOURS = 48  # LAG/SUM bis 24 h + SINCE 48 h → 48 Kontext-Stunden genügen


@dataclass(frozen=True)
class ContextBundle:
    """Kontext des Vormonats für den inkrementellen Build.

    ``table``: die letzten 48 Kalenderstunden des Vormonats im ``hourly_agg``-
    Schema (``ts_bucket`` + 98 Basis-Spalten, aus der Store-Datei) — dient nur
    als Window-/Seed-Brücke, wird NICHT in den Output geschrieben.
    ``seeds``: je Zone der gehaltene UVR-Regelzustand (Forward-Fill) am
    Vormonat-Ende = ``hzrkl_{z}_rule_last`` der letzten Kontext-Zeile.
    """

    table: pa.Table
    seeds: dict[str, float | None]
    month: str


def month_file_path(features_dir: Path, month: str) -> Path:
    """Hive-Pfad einer Feature-Store-Monatsdatei (``<YYYY>/<MM>/<YYYY-MM>.parquet``)."""
    y, m = month.split("-")
    return features_dir / y / m / f"{y}-{m}.parquet"


def prev_month_str(month: str) -> str:
    """'YYYY-MM' → Vormonat ('YYYY-MM')."""
    y, m = (int(x) for x in month.split("-"))
    return f"{(y - 1):04d}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"


def _month_end_last_hour(month: str) -> datetime:
    """Letzte volle Kalenderstunde des Monats (naive Berlin, z. B. 2026-10 → 31.10 23:00)."""
    y, m = (int(x) for x in month.split("-"))
    return datetime(y, m, calendar.monthrange(y, m)[1], 23)


def load_context(
    prev_month: str,
    store_path: Path,
    data_version: str = DATA_VERSION,
    context_hours: int = CONTEXT_HOURS,
) -> ContextBundle | None:
    """Lädt die letzten ``context_hours`` Stunden der Vor-Monat-Store-Datei.

    Returns ``None`` (→ ``--full``-Fallback) bei:
    - fehlender Datei (Fallback a),
    - ``data_version``-Mismatch (Store-Metadaten vs. Code, Fallback c),
    - Kontext < ``context_hours`` Zeilen (z. B. Store-Anfang, Fallback b),
    - Kontext endet nicht exakt bei Vormonat 23:00 (Datenstopp über die
      Monatsgrenze: Zwischenstunden fehlen → LAG-Brücke unzuverlässig),
    - nicht konsequentes stündliches Raster (Defekt der Store-Datei).
    """
    if not store_month_file_ok(store_path, data_version):
        return None
    tbl = pq.read_table(store_path)
    n = tbl.num_rows
    if n < context_hours:
        return None
    end = _month_end_last_hour(prev_month)
    ts_col = tbl["ts"]
    if ts_col.to_pylist()[-1] != end:
        return None
    ts_list = ts_col.slice(n - context_hours, context_hours).to_pylist()
    start = end - timedelta(hours=context_hours - 1)
    if ts_list[0] != start or any(
        b - a != timedelta(hours=1) for a, b in zip(ts_list, ts_list[1:], strict=False)
    ):
        return None
    ctx = tbl.slice(n - context_hours)
    seeds: dict[str, float | None] = {}
    for z in config.HZRKL_ZONES:
        v = ctx[f"hzrkl_{z}_rule_last"].to_pylist()[-1]
        seeds[z] = None if v is None else float(v)
    cols = resample.hourly_agg_columns()
    data: dict[str, pa.Array] = {
        "ts_bucket": pa.array([t.strftime("%Y-%m-%d %H:00") for t in ts_list], type=pa.string())
    }
    for c in cols[1:]:
        col = ctx[c]
        if c == "offset_known":  # Store: int32 (COALESCE), fresh hourly_agg: BOOL
            col = col.cast(pa.bool_())
        data[c] = col
    return ContextBundle(table=pa.table(data), seeds=seeds, month=prev_month)


def store_month_file_ok(store_path: Path, data_version: str = DATA_VERSION) -> bool:
    """Store-Datei vorhanden + Parquet-Metadaten ``data_version`` stimmt mit Code überein."""
    if not store_path.exists():
        return False
    meta = pq.read_metadata(store_path).metadata or {}
    return (meta.get(b"data_version") or b"").decode("utf-8") == data_version


def store_max_ts(store_path: Path) -> datetime | None:
    """Max-``ts`` einer Store-Monatsdatei (Parquet-Row-Group-Statistiken).

    None, wenn die Datei fehlt oder leere/korrupte Statistiken liefert.
    """
    if not store_path.exists():
        return None
    pf = pq.ParquetFile(store_path)
    md = pf.metadata
    if md.num_rows == 0:
        return None
    idx = pf.schema_arrow.get_field_index("ts")
    best: datetime | None = None
    for rg in range(md.num_row_groups):
        st = md.row_group(rg).column(idx).statistics
        if st is None:
            continue
        try:
            mx = st.max
        except Exception:  # noqa: BLE001 - korrupte Stats → konservativ None
            continue
        if mx is None:
            continue
        # Pyarrow decode stat.max je nach Arrow-Typ entweder als naive
        # ``datetime`` (timestamp[us] ohne tz, wie der Store) oder als
        # INT64 (us seit Epoche). Beides zu naive Wallclock normalisieren.
        if isinstance(mx, datetime):
            v = mx
        elif isinstance(mx, int):
            v = datetime.fromtimestamp(int(mx) / 1_000_000, tz=UTC).replace(tzinfo=None)
        else:
            continue
        if v is not None and (best is None or v > best):
            best = v
    return best


def _max_ts_in_glob(con: duckdb.DuckDBPyConnection, glob: str) -> datetime | None:
    """Max-Roh-ts einer Quelle (None, wenn der Glob leer ist)."""
    r = con.execute(f"SELECT max(cast(ts AS timestamp)) FROM read_parquet('{glob}')")
    row = r.fetchone()
    r.fetchall()  # duckdb 2.0.0: Resultat leeren, bevor register() kommt
    return row[0]


def _month_of_last_ts(last_ts: datetime) -> str:
    """Monat ('YYYY-MM') der letzten Datenstunde (Wallclock-Berlin)."""
    last_hour = last_full_hour_wall(last_ts)
    return f"{last_hour.year:04d}-{last_hour.month:02d}"


def register_wx_and_sun(
    con: duckdb.DuckDBPyConnection,
    cfg: config.BuildConfig,
    first_hour: datetime,
    last_hour: datetime,
) -> None:
    """Registriert ``wx_wide`` (Open-Meteo-Roh-Cache) + ``sun_daily`` (NOAA).

    Gemeinsamer Pfad für Full- und inkrementellen Build: dieselbe Query-Form,
    damit die Wetter-/Sun-Spalten bit-identisch sind (M1-Inkremental).
    """
    # --- Wetter-Fenster: wx_long → wx_wide (pivot) → 24 Equi-Joins ---
    era5_months = config.month_range(config.ERA5_BACKFILL_START, config.ERA5_BACKFILL_END)
    hfa_months = config.month_range(config.HFC_START[:7] + "01", cfg.fetch_end)
    wx_long = load_wx_long(cfg.wx_cache_dir, era5_months, hfa_months)
    con.register("wx_long", wx_long)
    con.execute(wx_wide_sql())
    # ``wx_wide`` ist eine materialisierte TABLE → das registrierte Arrow-
    # ``wx_long`` (765 k Rows × 4 Spalten, ~32 MB Arrow + DuckDB-Referenz) wird
    # hier NICHT mehr benötigt → deregistrieren, damit es nicht über den
    # gesamten Full-Build (76 Monats-Loops + Assembly) im RAM lebt.
    del wx_long
    with contextlib.suppress(duckdb.Error):
        con.unregister("wx_long")
    # --- Sun-Features (NOAA) ---
    days = [
        first_hour.date() + timedelta(days=i)
        for i in range((last_hour.date() - first_hour.date()).days + 1)
    ]
    sun = sun_features_table(days)
    con.register("sun_daily", sun)


# --- M1-Columnar (2026-11): vektorisierte PyArrow-Spalten-Assembly -------------
#
# Ersetzt die finale 692-Spalten-SQL-Query (Phase E: ``SELECT * FROM
# features_final ORDER BY ts`` + 24×``wx_wide``-Equi-Joins + Sort; R4'-
# Profiling: 82 s / Peak 12,6 GB, ~94 % davon die 24 WX-Joins × voller
# ``wx_wide``-Scan je Join) durch eine Spalten-Assembly auf PyArrow/NumPy.
#
#   * Phasen A–D laufen weiter in DuckDB (columnares Streaming, RAM-Sicher,
#     UNVERÄNDERT): ``hourly_agg`` (Raw→1-h), ``wx_wide`` (wx_long-Pivot),
#     ``sun_daily`` (NOAA-Formel), Grid (``build_hour_grid``).
#   * Diese liest die kleinen Zwischenschichten je EINMAL per
#     ``to_arrow_table()`` und berechnet ALLE 692 Feature-Spalten vektorisiert
#     (keine Python-Row-Loops über die 54 k Stunden):
#
#     - Grid (Driver, 1 Row/Kalenderstunde) ↔ ``hourly_agg`` key-basiert auf
#       ``ts_bucket`` (``strftime(ts,'%Y-%m-%d %H')||':00'``); Grid-Row ohne
#       Treffer = **Gap** → ``n_rows=0``/``source='none'``/``gap=1`` + alle
#       hourly_agg-Spalten **NULL im exakten Zieltyp** (Typ-Drift = häufigster
#       Bit-Identitäts-Bruch).
#     - LAG = Positions-Shift (vordere k Rows = NULL, kein Fill) ≡
#       ``LAG(col,k) OVER (ORDER BY ts)``.
#     - Runtime-SUM = Positions-Window ``ROWS w-1 PRECEDING..CURRENT`` mit
#       SQL-NULL-Semantik (Nicht-NULL-Summe, komplettes NULL-Window → NULL).
#     - SINCE = 49-Positions-Window-Max der maskierten ``row_idx``
#       (``col>0.05``), kein Treffer → 49.0 (≡ SQL, DECIMAL(20,1)).
#     - hzrkl-Regel: Fill-Forward **innerhalb** ``resample`` (Raw-``LAST_VALUE``
#       + Stunden-AVG), ``hourly_agg``-Spalte wird hier nur positionell
#       übernommen (≡ ``features_final``: ``agg.hzrkl_{z}_rule``).
#     - WX = **Index-Arithmetik** statt 24 Equi-Joins (``wx_wide`` = dichter
#       UTC-Stunden-Raster): Zielindex = Stundenoffset + k, out-of-range
#       (inkl. rechte Datenkante) → NULL — EXAKT wie der Equi-Join. KEIN LEAD:
#       bei fehlender Stunde lieferte ``LEAD(k)`` den nächsten vorhandenen Wert,
#       der Equi-Join NULLt (Semantik-Grenze s. R4′-E3). Bei nachgewiesener
#       Mid-Span-Lücke im wx-Cache fällt der Lookup automatisch auf
#       ``searchsorted`` (NULL bei fehlendem Ziel) zurück — exakt dieselbe
#       Semantik, per Dichte-Check pro Build entschieden.
#
# Datenidentitäts-Gate (tests/test_columnar.py): stufenweiser Abgleich gegen
# ``final_features_view_sql`` (SQL-Referenz/Orakel, bleibt erhalten) auf echten
# Daten (2026-09), dann Vollspan-Full-Rebuild (Temp-Dir) ≡ Produktiv-m1.3.


def _agg_target_types(hourly_agg: pa.Table) -> dict[str, pa.DataType]:
    """``hourly_agg``-Spalten → Feature-Store-Zieltypen.

    Das frische Aggregat liefert ``offset_known`` als BOOLEAN (``MAX(…) IS NOT
    NULL``); die Store-Spalte ist INT32 (``COALESCE(agg.offset_known, 0)``).
    Alle anderen Spalten behalten ihren Aggregat-Typ (DOUBLE/BIGINT/VARCHAR/
    DECIMAL(2,1) für ``hzrkl_{z}_rule_last``). Das inkrementelle
    ``load_context``-Schema (Store-Slice) liefert ``offset_known`` bereits als
    INT32 → auch dort Identität (``_agg_target_types`` mappt INT32 auf INT32).
    """
    out: dict[str, pa.DataType] = {}
    for f in hourly_agg.schema:
        if f.name == "ts_bucket":
            continue
        out[f.name] = pa.int32() if f.name == "offset_known" else f.type
    return out


def _pick_rows(col: pa.Array, row_pos: pa.Array) -> pa.Array:
    """Positionelles Gather: ``col[row_pos[i]]`` (``row_pos[i] < 0`` → NULL).

    Vektorisiert (ein Take pro Spalte statt Python-Row-Loop); -1-Indizes =
    Gap-Rows (kein ``hourly_agg``-Treffer) → NULL im Zieltyp. (pyarrow hat
    kein NULL-Default bei ``take`` → sicherer Index 0 + ``if_else``-Maske.)
    """
    ok = pc.greater_equal(row_pos, pa.scalar(0, type=pa.int64()))
    safe = pc.if_else(ok, row_pos, pa.scalar(0, type=pa.int64()))
    return pc.if_else(ok, pc.take(col, safe), pa.scalar(None, type=col.type))


def _lag_shift(col: pa.Array, k: int) -> pa.Array:
    """SQL-``LAG(col, k)`` ≡ Positions-Shift: die ersten ``k`` Rows = NULL,
    danach ``col[i − k]`` (keine Fill; NULL bleibt NULL).

    ``col`` muss denselben Typ wie das SQL-Quellfeld haben (hier: alle
    ``LAG_COLUMNS`` = DOUBLE) — dann sind NULL/Value-Transitionen und
    Bitwerte exakt.
    """
    n = len(col)
    if k >= n:
        return pa.nulls(n, type=col.type)
    return pa.concat_arrays([pa.nulls(k, type=col.type), col.slice(0, n - k)])


def _rolling_sum_nullskip(col: pa.Array, w: int) -> pa.Array:
    """``SUM(col) OVER (ORDER BY ts ROWS BETWEEN w-1 PRECEDING AND CURRENT ROW)``.

    Positions-Window (nicht Value-Window) mit SQL-NULL-Semantik: NULLs werden
    **übersprungen** (nicht als 0 gezählt); ist das ganze Window NULL → NULL.
    Implementierung: kumulierte Nicht-NULL-Summe + NULL-Count-Präfix;
    Window-Summe = ``prefix[i+1] − prefix[i+1−w]`` (endlich; die +inf−+inf-Falle
    kann nicht auftreten, weil nur Nicht-NULL-Werte akkumuliert werden).
    """
    import numpy as np

    v = np.array(col, dtype=np.float64)  # NULL → NaN
    n = v.size
    if n < w:  # n < w: slices degenerieren → generische Schleife
        out = np.full(n, np.nan, dtype=np.float64)
        counts = np.concatenate(([0], np.cumsum(~np.isnan(v), dtype=np.int64)))
        for i in range(n):
            lo = max(0, i - w + 1)
            if counts[i + 1] - counts[lo] > 0:
                # nan_to_num: NULL → 0 (sicher, da count > 0 ⇒ ≥1 Nicht-NULL-Wert)
                out[i] = np.nan_to_num(v[lo : i + 1]).sum()
        return pa.array(out, mask=np.isnan(out), type=pa.float64())
    mask = ~np.isnan(v)
    prefix = np.concatenate(([0.0], np.cumsum(np.where(mask, v, 0.0))))
    counts = np.concatenate(([0], np.cumsum(mask, dtype=np.int64)))
    # Window [i-w+1 .. i] (w Rows) → Sum = prefix[i+1] − prefix[max(0, i-w+1)]:
    # i < w → start 0.0 (w Werte); i ≥ w → prefix[i-w+1] = prefix[1 .. n-w].
    start = np.concatenate(([0.0] * w, prefix[1 : n - w + 1]))
    cstart = np.concatenate(([0] * w, counts[1 : n - w + 1]))
    out = prefix[1 : n + 1] - start
    out = np.where(counts[1 : n + 1] - cstart == 0, np.nan, out)
    return pa.array(out, mask=np.isnan(out), type=pa.float64())


def _rolling_max_nullskip(vals: pa.Array, keep: pa.Array, w: int) -> pa.Array:
    """``MAX(CASE WHEN keep THEN vals END) OVER (ROWS w PRECEDING..CURRENT)``
    mit SQL-NULL-Semantik (NULL-Skip; komplettes NULL-Window → NULL).

    ``keep`` = True für gültige (nicht-NULL) Rows; False → NULL-Skip.
    ``w PRECEDING`` = ``w`` Rows VOR der aktuellen + aktuelle = ``w + 1``-Row-
    Fenster (SINCE-Query: 48 PRECEDING → 49 Rows). numpy-Block-Max (O(n·w),
    w = 49, n ≤ 54 k → trivial). NULL-Marker = −2 (keine ``row_idx`` < 0;
    ``vals`` sind Positions-Indizes, daher kein legitimer Wert < 0).
    """
    import numpy as np

    sentinel = -2
    v = np.where(np.array(keep, dtype=bool), np.array(vals, dtype=np.int64), sentinel)
    n = v.size
    # Rückwärts-Fenster [i−w, i] per Präfix-Padding: c = sentinel^w ∥ v →
    # c[i+w+s] = v[i+s]; out[i] = max(c[i..i+w]) = max(v[i-w..i]) (voraus des
    # Spanns = sentinel = „keine NULL-Skip-Werte“ → komplettes NULL-Window).
    c = np.concatenate((np.full(w, sentinel, dtype=np.int64), v))
    out = np.full(n, sentinel, dtype=np.int64)
    for s in range(w + 1):
        out = np.fmax(out, c[s : s + n])
    return pa.array(out, type=pa.int64())


def _wx_equi_join(
    wx_wide: pa.Table, hour_utc: pa.Array, window_h: int
) -> dict[tuple[int, str], pa.Array]:
    """``wx_t{k}_{short}``-Spalten, EXAKT wie die 24 SQL-Equi-Joins
    ``wx_wide w{k} ON w{k}.hour_utc = g.hour_utc + INTERVAL k HOUR``
    (fehlende Ziel-Stunde → NULL, inkl. rechts der Datenkante).

    ``wx_wide`` ist ein dichter UTC-Stunden-Raster (einzelne Lücke = rechte
    Datenkante, s. R4′-E3: 2026-09 = 720/720 h, 0 Lücken) → **Index-
    Arithmetik** (Stundenoffset + k; O(N×K) vektorisiert). Dichte-Check bei
    jedem Aufruf: Mid-Span-Lücke → ``searchsorted``-Lookup (identische
    NULL-Semantik, andere Performance-Charakteristik).
    """
    import numpy as np

    wx = wx_wide.sort_by([("hour_utc", "ascending")])
    wx_h = np.array(wx["hour_utc"], dtype="datetime64[ns]")
    target = np.array(hour_utc, dtype="datetime64[ns]")
    m = wx_h.size
    dense = bool(np.all(np.diff(wx_h) == np.timedelta64(1, "h"))) if m > 1 else True
    var_np = {short: np.array(wx[var], dtype=np.float64) for var, short in config.WX_SHORT.items()}
    out: dict[tuple[int, str], pa.Array] = {}
    for k in range(window_h):
        tgt = target + np.timedelta64(k, "h")
        if dense:
            idx = (tgt - wx_h[0]) // np.timedelta64(1, "h")
        else:
            idx = np.searchsorted(wx_h, tgt, side="left")
        ok = (idx >= 0) & (idx < m)
        idx_safe = np.clip(idx, 0, m - 1)
        hit = ok & (wx_h[idx_safe] == tgt)
        for _var, short in config.WX_SHORT.items():
            res = np.full(target.size, np.nan)
            if hit.any():
                res[hit] = var_np[short][idx_safe[hit]]
            # fehlende Ziel-Stunde → NULL (nicht NaN): pyarrow hält NaN im
            # Float64-Array als Wert (nicht Null) → per Mask zu NULL.
            out[(k, short)] = pa.array(res, type=pa.float64(), mask=np.isnan(res))
    return out


def _bias_columns(
    bias_profile: dict | None,
    wx_arrays: dict[tuple[int, str], pa.Array],
    ts: pa.Array,
) -> dict[str, pa.Array]:
    """``wx_t{k}_t_bias`` (EXAKT wie ``bias_terms``: NULL-T → NULL; saisonaler
    Offset + Drift × ``datediff('day', saison_start, g.ts::DATE)``; Saison-
    Start = 1. des ersten Saison-Monats, DJF-Start = 01.12 des Vorjahres)."""
    import numpy as np

    months = np.array(pc.month(ts), dtype=np.int64)
    years = np.array(pc.year(ts), dtype=np.int64)
    season = np.where(
        (months == 12) | ((months == 1) | (months == 2)),
        "DJF",
        np.where(
            (months == 3) | ((months == 4) | (months == 5)),
            "MAM",
            np.where((months == 6) | ((months == 7) | (months == 8)), "JJA", "SON"),
        ),
    )
    offset = np.zeros(months.size, dtype=np.float64)
    drift = np.zeros(months.size, dtype=np.float64)
    if bias_profile:
        for s in config.BIAS_SEASONS:
            prof = bias_profile.get(s) or {}
            sel = season == s
            offset = np.where(sel, float(prof.get("offset_k", 0.0)), offset)
            drift = np.where(sel, float(prof.get("drift_k_per_day", 0.0)), drift)
    # Tage seit Saison-Start (≡ SQL datediff('day', season_start, g.ts::DATE)):
    # Ordinal-Differenz. ``ts`` = naive Wallclock-Berlin → Tag = Trunk auf die
    # Wallclock (``datetime64[D]``-Cast), NICHT UTC-Epoche-Tage (``//86400``
    # würde CET/CEST-Abende am falschen UTC-Tag anlegen → Bias-Tag ±1).
    ts_ord = np.array(ts, dtype="datetime64[D]").astype("int64") + date(1970, 1, 1).toordinal()
    # Saison-Start-Tag EXAKT wie ``bias_terms``/``season_start_expr``
    # (monatsbasiert, NICHT saisonbasiert):
    #   12 → 01.12 des VORJAHR;  1|2 → 01.01;  3|4|5 → 01.03;
    #   6|7|8 → 01.06;           9|10|11 → 01.09 (des Jahres).
    start_month = np.where(months == 12, 12, np.ones_like(months))
    start_month[(months >= 3) & (months <= 5)] = 3
    start_month[(months >= 6) & (months <= 8)] = 6
    start_month[(months >= 9) & (months <= 11)] = 9
    start_year = np.where(months == 12, years - 1, years)
    start_ord = np.array(
        [
            date(int(y), int(sm), 1).toordinal()
            for y, sm in zip(start_year, start_month, strict=True)
        ],
        dtype=np.int64,
    )
    days_since = (ts_ord - start_ord).astype(np.float64)
    out: dict[str, pa.Array] = {}
    for k in range(config.WX_WINDOW_H):
        t = np.array(wx_arrays[(k, "t")], dtype=np.float64)  # NULL → NaN
        res = np.where(np.isnan(t), np.nan, t + offset + drift * days_since) if bias_profile else t
        # fehlende Grid-T → NULL im Bias (≡ SQL ``CASE WHEN wx_t{k}_t IS NULL
        # THEN NULL``); NULL-Pattern = NaN-Positionen (Bias-Arithmetik auf
        # Nicht-NULL-Grid-T ist endlich).
        out[f"wx_t{k}_t_bias"] = pa.array(res, type=pa.float64(), mask=np.isnan(res))
    return out


def _runtime_since(row_idx: pa.Array, on_col: pa.Array) -> pa.Array:
    """SQL-SINCE: ``MAX(CASE WHEN col>0.05 THEN row_idx END) OVER (ROWS 48
    PRECEDING..CURRENT)`` → ``row_idx − last_active`` (kein Treffer → 49.0).

    Ergebnis = DECIMAL(20,1) (Store-Typ, exakt wie die SQL-Literale 49.0 bzw.
    die int-Subtraktion ``g.row_idx − last_active``).
    """
    import numpy as np

    v = np.array(on_col, dtype=np.float64)
    m = ~np.isnan(v)
    active = m & (v > 0.05)
    masked = np.where(active, np.array(row_idx, dtype=np.int64), -1)
    last = np.array(
        _rolling_max_nullskip(
            pa.array(masked, type=pa.int64()), pa.array(active, type=pa.bool_()), 48
        ),
        dtype=np.int64,
    )
    idx_arr = np.array(row_idx, dtype=np.int64)
    # SQL: ``CASE WHEN last_active IS NULL THEN 49.0 ELSE g.row_idx − last_active
    # END`` → DECIMAL(20,1). Int-Subtraktion ist exakt; der int64→DECIMAL(20,1)-
    # Cast skaliert bereits mit 10¹ (9 → 9.0) → KEINE Vorskalierung (×10 wäre
    # eine Doppelskalierung → 90.0 statt 9.0).
    since = np.where(last < 0, 49, idx_arr - last)
    return pa.array(since, type=pa.int64()).cast(pa.decimal128(20, 1))


def assemble_features_columnar(
    con: duckdb.DuckDBPyConnection,
    cfg: config.BuildConfig,
    bias_profile: dict | None,
    git_tag: str,
    generated_at: str,
    *,
    start_hour: datetime,
    last_hour: datetime,
    context: ContextBundle | None,
    out_dir: Path | None = None,
) -> pa.Table | None:
    """Columnare Feature-Assembly (Produktions-Pfad, M1-Columnar).

    Phasen A–D müssen vorher in ``con`` gelaufen sein (``hourly_agg``,
    ``wx_wide``, ``sun_daily`` + Grid per ``build_hour_grid``); diese Funktion
    liest die kleinen Zwischenschichten je einmal per ``to_arrow_table()`` und
    berechnet alle 692 Feature-Spalten vektorisiert (Details s. Block-Doku).

    - **Full-Build**: ``context=None``, Grid = ``[start_hour … last_hour]``
      (gesamte Historie).
    - **Inkrementell**: ``context`` (48 h Kontext aus der Store-Datei,
      ``hourly_agg``-Schema) wird VOR die frischen Monats-Zeilen gestellt
      (≡ SQL-``UNION ALL``); die Window-Funktionen (LAG/SUM/SINCE) laufen über
      die zusammengeführte Sequenz → exakt die Full-Build-Werte. Die Kontext-
      Stunden liegen vor ``start_hour + 48 h`` (Monatsstart) und bleiben über
      den Grid-Filter (Grid startet bei ``context.table``-Erst-Stunde) im
      Output — ``cmd_features`` schneidet per Monats-Split ab (die Kontext-
      Zeilen fallen in den Vormonat, ``out_dir`` schreibt sie nicht neu, weil
      der Vormonat nicht im Monats-Split vorkommt … ACHTUNG: s. unten).

    ``out_dir=None`` → Returns die komplette Table (``--verify``/Tests,
    Kontext-Zeilen INKLUSIVE, Grid-geordnet); ``out_dir`` → pro Kalendermonat
    ``write_month_parquet`` (atomar), Returns die Pfade. Im inkrementellen
    Build (``context`` gesetzt) gehört der Vormonat-Nachmittag (die Kontext-
    Stunden) NICHT in den Store → beim Schreiben wird der Vormonat übersprungen.

    (M1-Columnar-RAM, 2026-11-04: die eigentliche Spalten-Assembly läuft in
    ``_assemble_from_arrow`` (reines PyArrow, KEIN DuckDB) — dieser Wrapper
    holt nur die drei Zwischenschichten als Arrow aus ``con``. So kann der
    Full-Build (``store_features``) die registrierten Zwischenschichten aus
    ``con`` deregistrieren, bevor die 692-Spalten-Assembly läuft.)
    """
    hourly_agg = con.sql("SELECT * FROM hourly_agg ORDER BY ts_bucket").to_arrow_table()
    wx_wide = con.sql("SELECT * FROM wx_wide").to_arrow_table()
    sun_daily = con.sql("SELECT * FROM sun_daily").to_arrow_table()
    return _assemble_from_arrow(
        hourly_agg,
        wx_wide,
        sun_daily,
        bias_profile,
        git_tag,
        generated_at,
        start_hour=start_hour,
        last_hour=last_hour,
        context=context,
        out_dir=out_dir,
    )


def _assemble_from_arrow(
    hourly_agg: pa.Table,
    wx_wide: pa.Table,
    sun_daily: pa.Table,
    bias_profile: dict | None,
    git_tag: str,
    generated_at: str,
    *,
    start_hour: datetime,
    last_hour: datetime,
    context: ContextBundle | None,
    out_dir: Path | None = None,
) -> pa.Table | None:
    """Reine PyArrow/NumPy-Spalten-Assembly (692 Spalten) ohne DuckDB.

    ``hourly_agg`` muss zeitlich geordnet sein (``ORDER BY ts_bucket``-
    Äquivalent); Grid, Gap, LAG/SUM/SINCE, WX-Index-Arithmetik, Bias und
    Sun-Verknüpfung s. Block-Doku oben. Identisches Verhalten wie
    ``assemble_features_columnar`` minus der Arrow-Fetches aus ``con``.

    RAM-Optimierung (Pi-Budget): die Eingabe-Tabellen werden nach ihrer
    Nutzung freigeben (``del`` + ``gc.collect`` nur in Tests; CPython-
    Refcounting reicht), damit der 692-Spalten-Aufbau nicht auf die
    Eingaben stößt.
    """
    import numpy as np

    ctx_n = 0
    skip_month: str | None = None
    if context is not None:
        # Inkrementell: 48 h Kontext (hourly_agg-Schema) ∪ frische Monats-Zeilen
        # (≡ SQL-UNION ALL: beide Seiten exakt das hourly_agg-Schema,
        # Spaltenpositionskorrekt). Window-Funktionen laufen über die Merge-
        # Sequenz → exakt die Full-Build-Werte.
        ctx_cols = [f.name for f in context.table.schema]
        agg_cols_names = [f.name for f in hourly_agg.schema]
        if ctx_cols != agg_cols_names:
            raise ValueError("columnar: Kontext- und hourly_agg-Schema divergieren")
        hourly_agg = pa.concat_tables([context.table, hourly_agg])
        ctx_n = context.table.num_rows
        if ctx_n < CONTEXT_HOURS:
            raise ValueError(f"columnar: Kontext {ctx_n} h < {CONTEXT_HOURS} h")
        # Alle Kontext-Zeilen liegen im Kalendermonat der ERSTEN Kontext-Stunde
        # (48 h < jeder Monat; Kontext = letzte 48 h des Vormonats, endet exakt
        # bei Vormonat 23:00 → keine Monatsgrenze innerhalb des Kontexts).
        # ``start_hour`` = Kontext-Start, also im Vormonat → genau dieser Monat
        # wird beim Schreiben übersprungen (Vormonat-Datei bleibt anfassfrei).
        skip_month = f"{start_hour.year:04d}-{start_hour.month:02d}"

    grid = build_hour_grid(start_hour, last_hour)
    n = grid.num_rows

    # --- Grid ↔ hourly_agg (Key = ts_bucket = 'YYYY-MM-DD HH:00') ---
    bucket_pos = {b: i for i, b in enumerate(hourly_agg["ts_bucket"].to_pylist())}
    grid_ts = grid["ts"].to_pylist()
    row_pos = pa.array(
        [bucket_pos.get(t.strftime("%Y-%m-%d %H:00"), -1) for t in grid_ts],
        type=pa.int64(),
    )
    tgt_types = _agg_target_types(hourly_agg)
    agg_cols = {c: hourly_agg[c].combine_chunks() for c in tgt_types}
    del hourly_agg  # Arrow-Eingabe nicht mehr nötig (agg_cols hält die Spalten)

    cols: dict[str, pa.Array] = {}
    # --- (1) Kalender (Grid) + Gap-Flags + Source ---
    for c in ("ts", "hour", "day_of_week", "is_weekend", "is_holiday", "holiday", "is_dst"):
        cols[c] = grid[c]
    n_rows_arr = _pick_rows(agg_cols["n_rows"], row_pos)
    n_rows_arr = pa.compute.if_else(
        pc.greater_equal(row_pos, pa.scalar(0, type=pa.int64())),
        n_rows_arr,
        pa.scalar(0, type=pa.int64()),
    )
    cols["n_rows"] = n_rows_arr
    n_np = np.array(n_rows_arr, dtype=np.int64)
    cols["gap"] = pa.array((n_np < GAP_FULL_ROWS).astype(np.int32), type=pa.int32())
    cols["gap_full"] = pa.array((n_np < GAP_MIN_ROWS).astype(np.int32), type=pa.int32())
    cols["gap_partial"] = pa.array(
        ((n_np >= GAP_MIN_ROWS) & (n_np < GAP_FULL_ROWS)).astype(np.int32), type=pa.int32()
    )
    # --- (1b) hourly_agg-Durchgänge (positionelles Take; Gap → NULL im Zieltyp) ---
    for c, _t in tgt_types.items():
        if c in ("n_rows", "source", "offset_known"):
            continue
        vals = _pick_rows(agg_cols[c], row_pos)
        cols[c] = vals
    # SQL: ``COALESCE(agg.offset_known, 0)`` → Gap-Zeilen = 0 (nicht NULL).
    cols["offset_known"] = pa.compute.if_else(
        pc.greater_equal(row_pos, pa.scalar(0, type=pa.int64())),
        _pick_rows(agg_cols["offset_known"], row_pos).cast(pa.int32()),
        pa.scalar(0, type=pa.int32()),
    )
    cols["source"] = pa.compute.if_else(
        pc.greater_equal(row_pos, pa.scalar(0, type=pa.int64())),
        _pick_rows(agg_cols["source"], row_pos),
        pa.scalar("none", type=pa.string()),
    )
    # --- (1c) elementweise Ableitungen (Gap → NULL wie die SQL-Spalten) ---
    bm = np.array(cols["buff_mid"], dtype=np.float64)  # NULL → NaN
    hr = np.array(cols["hz_rl"], dtype=np.float64)
    o9 = np.where(np.isnan(bm) | np.isnan(hr), np.nan, bm - hr - 5.0)
    # SQL: ``buff_mid − hz_rl − 5.0`` (NULL-Propagation) → Gap-Zeilen NULL.
    # mask: pyarrow hält NaN im Float64-Array sonst als Wert (nicht Null).
    cols["o9_switch_margin"] = pa.array(o9, type=pa.float64(), mask=np.isnan(o9))
    cols["mode_known"] = pa.array(np.zeros(n, dtype=np.int32), type=pa.int32())
    # --- Sun (LEFT-JOIN auf Tag; fehlender Tag → NULL wie SQL-LEFT-JOIN) ---
    sun_by_day = {
        d: (r, s, dl)
        for d, r, s, dl in zip(
            sun_daily["day"].to_pylist(),
            sun_daily["sunrise_min"].to_pylist(),
            sun_daily["sunset_min"].to_pylist(),
            sun_daily["daylight_min"].to_pylist(),
            strict=True,
        )
    }
    for i, name in enumerate(("sunrise_min", "sunset_min", "daylight_min")):
        cols[name] = pa.array(
            [sun_by_day[t.date()][i] if t.date() in sun_by_day else None for t in grid_ts],
            type=pa.int64(),
        )
    # RAM (Pi-Budget): Stage-1-Inputs freigeben, bevor LAG/WX/Runtime +
    # Store-Table aufgebaut werden. ``cols`` hält die Kopien aus Stage 1;
    # ``agg_cols``/Lookup-Strukturen werden nicht mehr referenziert.
    del sun_daily, sun_by_day, grid_ts, bucket_pos, row_pos, agg_cols, tgt_types
    cols["wx_is_forecast"] = pa.array(
        (
            np.array(grid["hour_utc"], dtype="datetime64[ns]")
            >= np.datetime64("2022-11-14T00:00:00")
        ).astype(np.int32),
        type=pa.int32(),
    )
    # --- (2) LAG (Positions-Shift; Kontext-Zeilen zählen mit — Grid-Start =
    # Kontext-Start inkrementell, exakt wie der SQL-Window; LAG verschiebt
    # Positionen, NULL bleibt NULL, vordere k Rows = NULL) ---
    for k in config.LAG_HOURS:
        for c in LAG_COLUMNS:
            cols[f"{c}_lag{k}"] = _lag_shift(cols[c], k)
    # --- (3) WX + Bias ---
    wx_arrays = _wx_equi_join(wx_wide, grid["hour_utc"], config.WX_WINDOW_H)
    del wx_wide  # Arrow-Eingabe nach WX-Index-Verknüpfung nicht mehr nötig
    for (k, short), arr in wx_arrays.items():
        cols[f"wx_t{k}_{short}"] = arr
    cols.update(_bias_columns(bias_profile, wx_arrays, grid["ts"]))
    del wx_arrays, grid  # Bias-Input (ts) + Grid-Table danach überflüssig
    # --- (4) Runtime-SUM/SINCE (Positions-Window, NULL-Skip; exakt wie SQL) ---
    row_idx = pa.array(range(n), type=pa.int64())
    for c in config.RUNTIME_COLUMNS:
        for w in config.RUNTIME_WINDOWS:
            cols[f"{c}_sum{w}h"] = _rolling_sum_nullskip(cols[c], w)
        cols[f"{c}_since_h"] = _runtime_since(row_idx, cols[c])
    # --- Meta ---
    cols["data_version"] = pa.array([DATA_VERSION] * n, type=pa.string())
    cols["git_tag"] = pa.array([git_tag] * n, type=pa.string())
    cols["generated_at"] = pa.array([generated_at] * n, type=pa.string())

    table = _build_store_table(cols, n)
    if out_dir is None:
        return table
    parts: list[Path] = []
    for month, sub in _split_by_month(table):
        if month == skip_month:
            continue  # Kontext-Zeilen: Vormonat bleibt anfassfrei
        parts.append(write_month_parquet(sub, month, out_dir, git_tag, generated_at))
    return parts


def _build_store_table(cols: dict[str, pa.Array], n: int) -> pa.Table:
    """Spalten im exakten Store-Ordern (692 Spalten; s. Produktiv-Schema)."""
    order: list[str] = [
        "ts",
        "hour",
        "day_of_week",
        "is_weekend",
        "is_holiday",
        "holiday",
        "is_dst",
        "n_rows",
        "gap",
        "gap_full",
        "gap_partial",
        "source",
        "buff_top",
        "buff_mid",
        "buff_bot",
        "ww",
        "oil_vl",
        "hz_vl",
        "hz_rl",
        "ext",
        "wood_vl",
        "flue",
        "test",
        "ww_flow",
        "ras_kell_perc",
        "ras_kell_ist",
        "offset_kell",
        "ras_eg_perc",
        "ras_eg_ist",
        "offset_eg",
        "ras_dg_perc",
        "ras_dg_ist",
        "offset_dg",
        "heat_min_kell",
        "heat_min_eg",
        "heat_min_dg",
        "ras_age_kell",
        "ras_age_eg",
        "ras_age_dg",
        *[f"{f}_last" for f in config.LAST_VALUE_FEATURES],
        "solar_tank_top",
        "solar_tank_mid",
        "solar_tank_bot",
        "ras2_kell_ras",
        "ras2_kell_ist",
        "ras2_eg_ras",
        "ras2_eg_ist",
        "ras2_dg_ras",
        "ras2_dg_ist",
        "o9_switch_margin",
        "hzrkl_kell_on",
        "hzrkl_eg_on",
        "hzrkl_dg_on",
        "hzrkl_kell_last",
        "hzrkl_eg_last",
        "hzrkl_dg_last",
        "hzrkl_kell_pos",
        "hzrkl_eg_pos",
        "hzrkl_dg_pos",
        "hzrkl_kell_rule",
        "hzrkl_kell_rule_last",
        "hzrkl_eg_rule",
        "hzrkl_eg_rule_last",
        "hzrkl_dg_rule",
        "hzrkl_dg_rule_last",
        "pump_wood_est_on",
        "wood_fire_on",
        *[f"{c}_sum{w}h" for w in config.RUNTIME_WINDOWS for c in config.RUNTIME_COLUMNS],
        *[f"{c}_since_h" for c in config.SINCE_COLUMNS],
        "solar_coll",
        "pump_solar_on",
        "pump_solar_last",
        "pump_ww_on",
        "pump_ww_last",
        "mix_open_on",
        "mix_open_last",
        "mix_close_on",
        "mix_close_last",
        "oel_burner_on",
        "oel_burner_last",
        "pump_ww_oil_on",
        "pump_ww_oil_last",
        "pump_circ_on",
        "pump_circ_last",
        "pump_hz_on",
        "pump_hz_last",
        "valve_hzrl_on",
        "valve_hzrl_last",
        "ras_divergence_kell",
        "ras_divergence_eg",
        "ras_divergence_dg",
        "offset_known",
        "mode_known",
        "sunrise_min",
        "sunset_min",
        "daylight_min",
        "wx_is_forecast",
        *[
            f"wx_t{k}_{short}"
            for k in range(config.WX_WINDOW_H)
            for var, short in config.WX_SHORT.items()
        ],
        *[f"wx_t{k}_t_bias" for k in range(config.WX_WINDOW_H)],
        *[f"{c}_lag{k}" for k in config.LAG_HOURS for c in LAG_COLUMNS],
        "data_version",
        "git_tag",
        "generated_at",
    ]
    missing = [c for c in order if c not in cols]
    if missing:
        raise AssertionError(f"columnar: fehlende Spalten {missing[:8]}")
    assert len(order) == 692, f"Store-Schema erwartet 692 Spalten, war {len(order)}"
    return pa.table({c: cols[c] for c in order})


def final_features_view_sql(bias_profile: dict | None, git_tag: str, generated_at: str) -> str:
    """``CREATE OR REPLACE VIEW features_final AS …`` (kanonische Query-Form) als SQL-Text.

    Exakt dieselbe Query für den Full-Build (``hour_grid`` = ganze Historie) und
    den inkrementellen Build (``hour_grid`` = 48 Kontext-Stunden + aktueller
    Monat; die Window-Funktionen laufen über die zusammengeführte Tabelle).
    Gleiche Window-Expressionen → exakt reproduzierte Floats (Bit-Identität,
    s. M1-Inkremental). Voraussetzung: die Views/Tabs ``hour_grid``,
    ``hourly_agg``, ``wx_wide``, ``sun_daily`` existieren in ``con``;
    ``create_final_views`` legt zusätzlich ``hour_grid_index`` an.
    """
    bias_terms_exprs = bias_terms(bias_profile, config.WX_WINDOW_H)
    lag_cols = [
        f"LAG({c}, {k}) OVER w AS {c}_lag{k}" for k in config.LAG_HOURS for c in LAG_COLUMNS
    ]
    # Wetter-Join: naive g.hour_utc (UTC) = wx_wide.hour_utc (UTC) + k h (naive).
    # Kein AT TIME ZONE / at_time_zone (duckdb 2.0.0: 'AT TIME ZONE' braucht pytz,
    # at_time_zone(zone) mit IANA-Zone ist unsicher). DST-Korrektur liegt bereits
    # in g.hour_utc (Python/zoneinfo, s. build_hour_grid).
    wx_join_cols = [
        f"LEFT JOIN wx_wide w{k} ON w{k}.hour_utc = g.hour_utc + INTERVAL {k} HOUR"
        for k in range(config.WX_WINDOW_H)
    ]
    wx_sel = ",\n            ".join(
        f"w{k}.{var} AS wx_t{k}_{short}"
        for k in range(config.WX_WINDOW_H)
        for var, short in config.WX_SHORT.items()
    )
    # wx_is_forecast: 1 ab ICON-D2-Archivstart (naive UTC 2022-11-14 00:00 = Split).
    wx_is_forecast_expr = "IF(g.hour_utc >= TIMESTAMP '2022-11-14 00:00:00', 1, 0)"
    # m1.1: Stunden-Endwerte (letzter Nicht-NULL-10-s-Wert) der analogen Features.
    # Gap-Verhalten wie alle State-Spalten: direkt aus hourly_agg (gap → NULL).
    last_sel = ", ".join(f"agg.{f}_last" for f in config.LAST_VALUE_FEATURES)
    # m1.2: Rücklaufminderer je Etage (Gap-Verhalten wie alle State-Spalten).
    hzrkl_sel = ", ".join(
        f"agg.hzrkl_{z}_{kind}" for kind in ("on", "last", "pos") for z in config.HZRKL_ZONES
    )
    hzrkl_rule_sel = ", ".join(
        f"agg.hzrkl_{z}_rule, agg.hzrkl_{z}_rule_last" for z in config.HZRKL_ZONES
    )
    runtime_sum_sel = [
        f"SUM(agg.{col}) OVER (ORDER BY g.ts ROWS BETWEEN {window - 1} PRECEDING AND CURRENT ROW) "
        f"AS {col}_sum{window}h"
        for window in config.RUNTIME_WINDOWS
        for col in config.RUNTIME_COLUMNS
    ]
    since_sel = []
    for col in config.SINCE_COLUMNS:
        last_active = (
            f"MAX(CASE WHEN agg.{col} > 0.05 THEN g.row_idx END) OVER "
            "(ORDER BY g.ts ROWS BETWEEN 48 PRECEDING AND CURRENT ROW)"
        )
        since_sel.append(
            f"CASE WHEN {last_active} IS NULL THEN 49.0 "
            f"ELSE g.row_idx - {last_active} END AS {col}_since_h"
        )
    runtime_sel = ",\n            ".join(runtime_sum_sel + since_sel)
    return f"""
        CREATE OR REPLACE VIEW features_final AS
        SELECT
            g.ts,
            g.hour,
            g.day_of_week,
            g.is_weekend,
            g.is_holiday,
            g.holiday,
            g.is_dst,
            COALESCE(agg.n_rows, 0) AS n_rows,
            CASE WHEN COALESCE(agg.n_rows, 0) < {GAP_FULL_ROWS} THEN 1 ELSE 0 END AS gap,
            CASE WHEN COALESCE(agg.n_rows, 0) < {GAP_MIN_ROWS} THEN 1 ELSE 0 END AS gap_full,
            CASE WHEN COALESCE(agg.n_rows, 0) >= {GAP_MIN_ROWS}
                   AND COALESCE(agg.n_rows, 0) < {GAP_FULL_ROWS} THEN 1 ELSE 0 END AS gap_partial,
            COALESCE(agg.source, 'none') AS source,
            agg.buff_top, agg.buff_mid, agg.buff_bot, agg.ww,
            agg.oil_vl, agg.hz_vl, agg.hz_rl, agg.ext, agg.wood_vl, agg.flue,
            agg.test, agg.ww_flow,
            agg.ras_kell_perc, agg.ras_kell_ist, agg.offset_kell,
            agg.ras_eg_perc, agg.ras_eg_ist, agg.offset_eg,
            agg.ras_dg_perc, agg.ras_dg_ist, agg.offset_dg,
            agg.heat_min_kell, agg.heat_min_eg, agg.heat_min_dg,
            agg.ras_age_kell, agg.ras_age_eg, agg.ras_age_dg,
            {last_sel},
            agg.solar_tank_top, agg.solar_tank_mid, agg.solar_tank_bot,
            agg.ras2_kell_ras, agg.ras2_kell_ist,
            agg.ras2_eg_ras, agg.ras2_eg_ist,
            agg.ras2_dg_ras, agg.ras2_dg_ist,
            agg.buff_mid - agg.hz_rl - 5.0 AS o9_switch_margin,
            {hzrkl_sel},
            {hzrkl_rule_sel},
            agg.pump_wood_est_on, agg.wood_fire_on,
            {runtime_sel},
            agg.solar_coll,
            agg.pump_solar_on, agg.pump_solar_last,
            agg.pump_ww_on, agg.pump_ww_last,
            agg.mix_open_on, agg.mix_open_last,
            agg.mix_close_on, agg.mix_close_last,
            agg.oel_burner_on, agg.oel_burner_last,
            agg.pump_ww_oil_on, agg.pump_ww_oil_last,
            agg.pump_circ_on, agg.pump_circ_last,
            agg.pump_hz_on, agg.pump_hz_last,
            agg.valve_hzrl_on, agg.valve_hzrl_last,
            agg.ras_divergence_kell, agg.ras_divergence_eg, agg.ras_divergence_dg,
            COALESCE(agg.offset_known, 0) AS offset_known,
            0 AS mode_known,
            sun.sunrise_min, sun.sunset_min, sun.daylight_min,
            {wx_is_forecast_expr} AS wx_is_forecast,
            {wx_sel},
            {", ".join(bias_terms_exprs)},
            {", ".join(lag_cols)},
            '{DATA_VERSION}' AS data_version,
            '{git_tag}' AS git_tag,
            '{generated_at}' AS generated_at
        FROM hour_grid_index g
        LEFT JOIN hourly_agg agg
          ON agg.ts_bucket = strftime(g.ts, '%Y-%m-%d %H') || ':00'
        {chr(10).join(wx_join_cols)}
        LEFT JOIN sun_daily sun ON sun.day = date(g.ts)
        WINDOW w AS (PARTITION BY 1 ORDER BY g.ts)
        """


def create_final_views(
    con: duckdb.DuckDBPyConnection,
    bias_profile: dict | None,
    git_tag: str,
    generated_at: str,
) -> None:
    """Legt ``hour_grid_index`` + ``features_final`` an (kanonische Query-Form).

    Gemeinsamer Pfad für Full- und inkrementellen Build — die Query-Form (und
    damit die Float-Ergebnisse) ist für beide identisch (Bit-Identität).
    """
    con.execute(
        "CREATE OR REPLACE VIEW hour_grid_index AS "
        "SELECT *, row_number() OVER (ORDER BY ts) AS row_idx FROM hour_grid"
    )
    con.execute(final_features_view_sql(bias_profile, git_tag, generated_at))


def month_raw_max_ts(root: Path, month: str) -> datetime | None:
    """Max-Roh-ts eines Hive-Monats (Parquet-Statistiken, ohne Daten-Scan).

    None, wenn die Monatsdatei fehlt oder leer ist (eine Quelle darf im
    inkrementellen Build leer sein — WinSol-Bestand ist frozen).
    """
    p = Path(root) / month[:4] / month[5:7] / f"{month}.parquet"
    if not p.exists():
        return None
    pf = pq.ParquetFile(p)
    md = pf.metadata
    if md.num_rows == 0:
        return None
    idx = pf.schema_arrow.get_field_index("ts")
    best: str | None = None
    for rg in range(md.num_row_groups):
        st = md.row_group(rg).column(idx).statistics
        if st is None:
            continue
        try:
            mx = st.max
        except Exception:  # noqa: BLE001 - korrupte Stats → konservativ None
            continue
        if mx is None:
            continue
        v = mx.decode() if isinstance(mx, bytes) else mx
        if best is None or str(v) > best:
            best = str(v)
    if best is None:
        return None
    return datetime.fromisoformat(best)


def assemble_features(
    con: duckdb.DuckDBPyConnection,
    cfg: config.BuildConfig,
    bias_profile: dict | None,
    git_tag: str,
    generated_at: str,
) -> pa.Table:
    """Assembliert den kompletten Feature-Store (alle Monate) als PyArrow-Table.

    Full-Build: liest alle Roh-Quellen (``winsol_pq`` + ``cmi_log_pq``,
    ``*/*/*.parquet``) und baut alle Monate von ``cfg.first_hour`` bis zur
    letzten Datenstunde. Inkrementeller Build: s. ``assemble_features_incremental``.
    """
    # --- Quellen-Enden (read-only) ---
    winsol_glob = str(cfg.winsol_pq / "*/*/*.parquet")
    cmi_glob = str(cfg.cmi_log_pq / "*/*/*.parquet") if _cmi_glob_exists(cfg.cmi_log_pq) else None
    winsol_max = _max_ts_in_glob(con, winsol_glob)
    if winsol_max is None:
        raise ValueError("winsol_pq liefert keine Rohdaten")
    last_ts = winsol_max
    if cmi_glob:
        cmi_max = _max_ts_in_glob(con, cmi_glob)
        if cmi_max is not None and cmi_max > last_ts:
            last_ts = cmi_max

    first_hour = cfg.first_hour
    last_hour = last_full_hour_wall(last_ts)
    grid = build_hour_grid(first_hour, last_hour)
    con.register("hour_grid", grid)

    # --- stündliche Aggregation der Roh-Quellen ---
    con.execute(resample.build_hourly_sql(winsol_glob, cmi_glob))

    # --- Wetter + Sun + finale Features (gemeinsamer Pfad, s. dort) ---
    register_wx_and_sun(con, cfg, first_hour, last_hour)
    create_final_views(con, bias_profile, git_tag, generated_at)
    return con.sql("SELECT * FROM features_final ORDER BY ts").to_arrow_table()


def _last_ts_full_span(con: duckdb.DuckDBPyConnection, cfg: config.BuildConfig) -> datetime:
    """Letzter Roh-ts über winsol+cmi (wie ``assemble_features``, read-only)."""
    winsol_glob = str(cfg.winsol_pq / "*/*/*.parquet")
    cmi_glob = str(cfg.cmi_log_pq / "*/*/*.parquet") if _cmi_glob_exists(cfg.cmi_log_pq) else None
    winsol_max = _max_ts_in_glob(con, winsol_glob)
    if winsol_max is None:
        raise ValueError("winsol_pq liefert keine Rohdaten")
    last_ts = winsol_max
    if cmi_glob:
        cmi_max = _max_ts_in_glob(con, cmi_glob)
        if cmi_max is not None and cmi_max > last_ts:
            last_ts = cmi_max
    return last_ts


def _split_by_month(table: pa.Table) -> list[tuple[str, pa.Table]]:
    """Sortierte ``ts``-Zeilen nach Kalender-Monat ('YYYY-MM') aufteilen.

    Die Assemblierung (columnar) liefert eine durchlaufende Tabelle über
    mehrere Monate; die Store-Dateien sind Hive-``YYYY/MM`` partitioniert
    (eine Datei pro Monat). ``ts`` ist sortiert → die Slices sind monotone,
    aufeinanderfolgende Blöcke.
    """
    if table.num_rows == 0:
        return []
    ym = pc.strftime(table["ts"], "%Y-%m").to_pylist()
    out: list[tuple[str, pa.Table]] = []
    i0 = 0
    for i in range(1, len(ym) + 1):
        if i == len(ym) or ym[i] != ym[i0]:
            out.append((ym[i0], table.slice(i0, i - i0)))
            i0 = i
    return out


def assemble_features_incremental(
    con: duckdb.DuckDBPyConnection,
    cfg: config.BuildConfig,
    bias_profile: dict | None,
    git_tag: str,
    generated_at: str,
    context: ContextBundle,
    cur_month: str,
    last_ts: datetime,
) -> pa.Table:
    """Inkrementeller Build: **nur** der aktuelle Monat, datenidentisch mit dem
    Full-Build (M1-Inkremental, DoD 1 = Bit-Identität).

    Mechanik:
    1. Kontext = die letzten 48 Stunden des Vormonats aus der Store-Datei
       (``context``), in das frische ``hourly_agg``-Schema überführt.
    2. Roh-Quellen **nur** des aktuellen Monats (``<root>/<YYYY>/<MM>/<YYYY-MM>.parquet``,
       eine Quelle darf leer sein); ``hourly_agg`` läuft mit ``rule_seed`` =
       gehaltene UVR-Regelzustände des Vormonats (Cross-Month-Forward-Fill).
    3. ``hour_grid`` = 48 Kontext-Stunden + Kalenderstunden des aktuellen Monats;
       ``hourly_agg`` = Kontext-Zeilen UNION frische Monats-Zeilen. Die Window-
       Funktionen (LAG/SUM/SINCE) der kanonischen ``features_final``-Query laufen
       über die zusammengeführte Tabelle → exakt die Full-Build-Werte.
    4. Output = nur die Zeilen ab Monatsstart (Kontext-Zeilen bleiben draußen).
    """
    y, m = (int(x) for x in cur_month.split("-"))
    month_start = datetime(y, m, 1)  # naive Berlin

    # --- Roh-Quellen: aktueller Monat (eine darf fehlen) ---
    win_glob = str(cfg.winsol_pq / cur_month[:4] / cur_month[5:7] / f"{cur_month}.parquet")
    cmi_glob = str(cfg.cmi_log_pq / cur_month[:4] / cur_month[5:7] / f"{cur_month}.parquet")
    win_exists = Path(win_glob).exists()
    cmi_exists = Path(cmi_glob).exists()
    if not win_exists and not cmi_exists:
        raise ValueError(f"inkrementeller Build: keine Rohdaten für {cur_month}")
    # ``hourly_agg_cur`` = frische Aggregation des aktuellen Monats (mit rule_seed),
    # als TABLE materialisiert (unabhängig von der src-View-Kette, damit die
    # ``hourly_agg``-View unten nicht mehr auf sie referenziert).
    con.execute(
        resample.build_hourly_sql(
            win_glob if win_exists else None,
            cmi_glob if cmi_exists else None,
            rule_seed=context.seeds,
        )
    )
    con.execute("CREATE OR REPLACE TABLE hourly_agg_cur AS SELECT * FROM hourly_agg")
    con.execute("DROP VIEW hourly_agg")
    # Kontext-Zeilen (48 h Vormonat) + frische Monats-Zeilen → ``hourly_agg``.
    # Beide Seiten haben exakt das hourly_agg-Schema (ts_bucket + 98 Spalten),
    # daher UNION ALL spaltenpositionskorrekt. Kontext-Zeilen bleiben im Output
    # außen (``features_final`` filtert über das hour_grid, das ab month_start startet).
    con.register("hourly_agg_ctx", context.table)
    con.execute(
        "CREATE OR REPLACE VIEW hourly_agg AS "
        "SELECT * FROM hourly_agg_ctx UNION ALL SELECT * FROM hourly_agg_cur"
    )

    # --- Raster: 48 Kontext-Stunden + aktueller Monat ---
    ctx_ts = context.table["ts_bucket"].to_pylist()
    ctx_first = datetime.fromisoformat(ctx_ts[0]).replace(tzinfo=BERLIN)
    last_hour = last_full_hour_wall(last_ts)
    grid_full = build_hour_grid(ctx_first, last_hour)
    # Kontext-Zeilen müssen exakt die letzten 48 Kalenderstunden vor month_start sein.
    expected_ctx = [month_start - timedelta(hours=48 - i) for i in range(48)]
    got_ctx = [grid_full["ts"].to_pylist()[i] for i in range(48)]
    if got_ctx != expected_ctx:
        raise ValueError("inkrementeller Build: Kontext-Raster passt nicht zur Monatsgrenze")
    con.register("hour_grid", grid_full)

    # --- Wetter + Sun + finale Features (gemeinsamer Pfad, s. dort) ---
    register_wx_and_sun(con, cfg, ctx_first, last_hour)
    create_final_views(con, bias_profile, git_tag, generated_at)
    tbl = con.sql("SELECT * FROM features_final ORDER BY ts").to_arrow_table()
    # Kontext-Zeilen (vor month_start) aus dem Output entfernen.
    ts = tbl["ts"]
    mask = pc.greater_equal(ts, pa.scalar(month_start, type=ts.type))
    return tbl.filter(mask)


def assemble_features_incremental_columnar(
    con: duckdb.DuckDBPyConnection,
    cfg: config.BuildConfig,
    bias_profile: dict | None,
    git_tag: str,
    generated_at: str,
    context: ContextBundle,
    cur_month: str,
    last_ts: datetime,
    *,
    out_dir: Path | None = None,
) -> pa.Table | None:
    """Inkrementeller Build (columnar, M1-Columnar): aktueller Monat + 48 h
    Kontext, datenidentisch mit dem Full-Build (M1-Inkremental, DoD 1).

    Mechanik (≡ ``assemble_features_incremental`` + SQL-Orakel-Pfad, nur
    Phase E ersetzt):
    1. Kontext (48 h Vormonat, ``hourly_agg``-Schema, aus der Store-Datei)
       wird per ``load_context`` geladen;
    2. Roh-Quellen NUR des aktuellen Monats (``hourly_agg`` via
       ``resample.build_hourly_sql`` mit ``rule_seed`` = ``context.seeds``);
    3. Grid = Kontext-Start → ``last_hour`` (volle Stunden); die Window-
       Funktionen (LAG/SUM/SINCE) laufen über die zusammengeführte
       Kontext∪Monat-Sequenz → exakt die Full-Build-Werte;
    4. ``out_dir=None`` → komplette Table (Kontext INKLUSIVE, Grid-geordnet);
       ``out_dir`` → nur die Monats-Datei des aktuellen Monats (Kontext-Monat
       wird übersprungen, Vormonat bleibt anfassfrei).
    """
    y, m = (int(x) for x in cur_month.split("-"))
    month_start = datetime(y, m, 1)  # naive Berlin
    # --- Roh-Quellen: aktueller Monat (eine darf fehlen) ---
    win_glob = str(cfg.winsol_pq / cur_month[:4] / cur_month[5:7] / f"{cur_month}.parquet")
    cmi_glob = str(cfg.cmi_log_pq / cur_month[:4] / cur_month[5:7] / f"{cur_month}.parquet")
    win_exists = Path(win_glob).exists()
    cmi_exists = Path(cmi_glob).exists()
    if not win_exists and not cmi_exists:
        raise ValueError(f"inkrementeller Build: keine Rohdaten für {cur_month}")
    con.execute(
        resample.build_hourly_sql(
            win_glob if win_exists else None,
            cmi_glob if cmi_exists else None,
            rule_seed=context.seeds,
        )
    )
    con.execute("CREATE OR REPLACE TABLE hourly_agg_cur AS SELECT * FROM hourly_agg")
    con.execute("DROP VIEW hourly_agg")
    con.execute("CREATE OR REPLACE VIEW hourly_agg AS SELECT * FROM hourly_agg_cur")

    # --- Raster: 48 Kontext-Stunden + aktueller Monat (≡ SQL-Pfad) ---
    ctx_ts = context.table["ts_bucket"].to_pylist()
    ctx_first = datetime.fromisoformat(ctx_ts[0]).replace(tzinfo=BERLIN)
    last_hour = last_full_hour_wall(last_ts)
    grid_full = build_hour_grid(ctx_first, last_hour)
    expected_ctx = [month_start - timedelta(hours=48 - i) for i in range(48)]
    got_ctx = [grid_full["ts"].to_pylist()[i] for i in range(48)]
    if got_ctx != expected_ctx:
        raise ValueError("inkrementeller Build: Kontext-Raster passt nicht zur Monatsgrenze")
    del grid_full

    # --- Wetter + Sun (gemeinsamer Pfad, s. dort) ---
    register_wx_and_sun(con, cfg, ctx_first, last_hour)

    return assemble_features_columnar(
        con,
        cfg,
        bias_profile,
        git_tag,
        generated_at,
        start_hour=ctx_first,  # aware Europe/Berlin (build_hour_grid-Erwartung)
        last_hour=last_hour,  # aware Europe/Berlin
        context=context,
        out_dir=out_dir,
    )


def columnar_month_reference(
    con: duckdb.DuckDBPyConnection,
    cfg: config.BuildConfig,
    bias_profile: dict | None,
    git_tag: str,
    generated_at: str,
    cur_month: str,
    raw_max: datetime,
    context: ContextBundle,
) -> pa.Table:
    """``--verify``-Referenz: columnare Monats-Table (aktueller Monat + 48 h
    Kontext, in-memory; schreibt NIE). Kontext-Zeilen bleiben außen.

    ≡ dem monolithischen ``features_final``-Output auf demselben Fenster
    (Diskret bit-exakt, Float rel ≤ ``VERIFY_FLOAT_REL_TOL`` — s.
    tests/test_columnar.py).
    """
    tbl = assemble_features_incremental_columnar(
        con,
        cfg,
        bias_profile,
        git_tag,
        generated_at,
        context,
        cur_month,
        raw_max,
        out_dir=None,
    )
    assert tbl is not None
    y, m = (int(x) for x in cur_month.split("-"))
    month_start = datetime(y, m, 1)
    ts = tbl["ts"]
    mask = pc.greater_equal(ts, pa.scalar(month_start, type=ts.type))
    return tbl.filter(mask)


def write_month_parquet(
    table: pa.Table, month: str, out_dir: Path, git_tag: str, generated_at: str
) -> Path:
    """Schreibt einen Monats-Slice als zstd-Parquet mit Metadaten-Block.

    Atomic: schreibt ZUERST nach ``<name>.parquet.part`` in demselben
    Verzeichnis und macht dann ein atomares ``os.replace`` auf den
    Endpfad (etabliertes Muster, s. ``app/cmi/cmi_log_pq.py``). Damit
    koennen parallele Leser (L2 im Gateway, ``data/features`` ro-mountet)
    niemals eine halbschriebene Monatsdatei sehen, auch waehrend ein
    stuendlicher M1-Feature-Build laeuft (M1-Maint-Loop).
    """
    y, m = month.split("-")
    path = out_dir / y / m / f"{y}-{m}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "data_version": DATA_VERSION,
        "git_tag": git_tag,
        "generated_at": generated_at,
        "rows": str(table.num_rows),
        "source": "cmi-m1-feature-build",
    }
    schema = table.schema.with_metadata({k: v.encode("utf-8") for k, v in meta.items()})
    tmp = path.with_suffix(".parquet.part")
    pq.write_table(table.cast(schema), tmp, compression="zstd")
    os.replace(tmp, path)  # atomic rename within the same directory
    return path


def _last_non_null(col: pa.Array) -> Any:
    """Letzter Nicht-NULL-Wert einer Spalte (None, wenn alles NULL)."""
    for v in reversed(col.to_pylist()):
        if v is not None:
            return v
    return None


def _trim_native_memory() -> None:
    """Gibt freigegebenen Native- (C++/glibc) Speicher an das OS zurück.

    DuckDB/PyArrow/NumPy allozieren über native Allocatoren; freigegebene
    Blöcke landen im glibc-Arena-Pool und werden NICHT automatisch per
    ``brk``/``munmap`` zurückgegeben → die VmRSS-VmHWM bleibt hoch, auch wenn
    die Python-Objekte bereits referenzfrei sind. ``malloc_trim(0)`` (glibc)
    / ``je_mallctl`` (jemalloc) zwingt eine Rückgabe der obersten freien
    Arena-Seiten. Kein-Op auf Nicht-glibc (No-Op-Fallback), daher hier
    defensiv gewrapped. Reduziert den Full-Build-Peak um die zwischen den
    76 Monats-Connection-Cycles und nach ``con.close()`` angehäuften Arenen.
    """
    if os.name != "posix":
        return
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.malloc_trim(0)
    except (OSError, AttributeError):
        pass  # kein glibc / keine malloc_trim → ignorieren


def store_features(
    con: duckdb.DuckDBPyConnection,
    cfg: config.BuildConfig,
    bias_profile: dict | None,
    git_tag: str,
    generated_at: str,
    log: Callable[[str], None] | None = None,
) -> list[Path]:
    """Assembliert + schreibt alle Monats-Dateien (Full-Build). Returns Pfade.

    M1-Columnar (2026-11): **columnare** Assembly (``assemble_features_columnar``)
    statt der finalen 692-Spalten-SQL-Query (R4′-Profiling: Phase E = 82 s /
    Peak 12,6 GB; 24 WX-Equi-Joins × voller ``wx_wide``-Scan). Phasen A–D
    (``resample.build_hourly_sql`` + ``wx_wide`` + ``sun_daily`` + Grid)
    bleiben unverändert (columnares DuckDB-Streaming, RAM-Sicher); nur die
    finale Query wird durch die vektorisierte Spalten-Assembly ersetzt.

    M1-Columnar-Full (2026-11): das Roh-Resample (Phasen A–B) läuft **pro
    Kalendermonat**, nicht als Vollspan-Glob: Der Vollspan-``hourly_agg``
    (99 Spalten aus 19,2 Mio. Roh-Rows) überschreitet das Pi-RAM-Budget von
    500 MB (``config.con()``) — gemessen: OOM bei 477,0/476,8 MiB auch mit
    1 Thread; ein einzelner Monat ≈ 0,4 s (20 Threads) und hält sich
    komfortabel im Budget. Pro Monat M:
    ``build_hourly_sql(<winsol-Monatsdatei>, <cmi-Monatsdatei>,
    rule_seed=<Mitnahme>, ts_from=<M-Start>, ts_to=<M-Ende>)`` → Monats-
    ``hourly_agg`` als Arrow laden → ``hzrkl_{z}_rule_last`` (letzter
    Nicht-NULL-Wert des Monats) als Seed für den Folgemonat mitnehmen →
    Monats-Arrow freigeben. Äquivalenz zum monolithischen Build:
    alle Bucket-Aggregate sind lokal innerhalb M (10-s-Row fällt exakt in
    eine Kalenderstunde ⇒ in genau EINEN Monat); der einzige Cross-Month-
    Zustand ist das ``hzrkl``-Forward-Fill (``LAST_VALUE IGNORE NULLS`` →
    Monats-Endwert), der als Seed mitgenommen wird — derselbe Mechanismus,
    den der produktive inkrementelle Pfad (``load_context`` liest exakt
    ``hzrkl_{z}_rule_last`` der letzten Kontext-Zeile als Seed) bit-identisch
    nachweist.

    Datenidentität: diskret bit-exakt, Float rel ≤ ``VERIFY_FLOAT_REL_TOL``
    (stufenweiser Abgleich gegen ``final_features_view_sql`` + Vollspan-
    Rebuild ≡ Produktiv-m1.3, s. tests/test_columnar.py).
    Das monolithische ``assemble_features`` bleibt als Referenz/Test-Orakel
    (läuft nur auf unbeschränkten Verbindungen — es ist der RAM-Verursacher).
    ``log`` = sparsame Fortschrittszeilen (Default: keine).
    """
    logf = log or (lambda _msg: None)
    winsol_glob = str(cfg.winsol_pq / "*/*/*.parquet")
    cmi_glob = str(cfg.cmi_log_pq / "*/*/*.parquet") if _cmi_glob_exists(cfg.cmi_log_pq) else None
    winsol_max = _max_ts_in_glob(con, winsol_glob)
    if winsol_max is None:
        raise ValueError("winsol_pq liefert keine Rohdaten")
    last_ts = winsol_max
    if cmi_glob:
        cmi_max = _max_ts_in_glob(con, cmi_glob)
        if cmi_max is not None and cmi_max > last_ts:
            last_ts = cmi_max
    first_hour = cfg.first_hour
    last_hour = last_full_hour_wall(last_ts)

    # --- Phase D: WX + Sun (DuckDB, einmalig über die ganze Spanne) ---
    register_wx_and_sun(con, cfg, first_hour, last_hour)

    # --- Phasen A–B: Roh-Resample pro Kalendermonat (Pi-RAM-Budget 500 MB) ---
    logf("[features] Resample (Kalendermonate, 500-MB-Budget)")
    agg_parts: list[pa.Table] = []
    seeds: dict[str, float | None] = {z: None for z in config.HZRKL_ZONES}
    y, m = first_hour.year, first_hour.month
    last_ym = (last_hour.year, last_hour.month)
    while (y, m) <= last_ym:
        month = f"{y:04d}-{m:02d}"
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        win_file = cfg.winsol_pq / f"{y:04d}" / f"{m:02d}" / f"{month}.parquet"
        cmi_file = cfg.cmi_log_pq / f"{y:04d}" / f"{m:02d}" / f"{month}.parquet"
        if win_file.exists() or cmi_file.exists():
            # Einseitig offenes ts-Fenster: eine 10-s-Row gehört exakt zu EINER
            # Kalenderstunde/EINEM Monat (Wandzeit-Trunk) → keine Doppel-/
            # Lücken-Zeilen an der Monatsgrenze.
            #
            # **Frische Connection pro Monat** (Pi-Budget 500 MB + Threads):
            # eine geteilte Connection klettert über die 76 Monate (DuckDB-
            # Interna: Parquet-Reader, Hash-Aggregator, Opt-Plan halten pro
            # ``build_hourly_sql``-Lauf ~5–15 MB, nie freigegeben) bis ~900 MB
            # VmHWM; frische Connection pro Monat → Peak ≈ 350 MB (R4''-Probe).
            # Semantik unverändert: ``build_hourly_sql`` referenziert nur die
            # Monatsdatei(en) + ``rule_seed``, kein Shared-State in ``con``.
            mcon = config.con()
            try:
                mcon.execute(
                    resample.build_hourly_sql(
                        str(win_file) if win_file.exists() else None,
                        str(cmi_file) if cmi_file.exists() else None,
                        rule_seed=seeds,
                        ts_from=f"{y:04d}-{m:02d}-01 00:00:00",
                        ts_to=f"{ny:04d}-{nm:02d}-01 00:00:00",
                    )
                )
                part = mcon.sql("SELECT * FROM hourly_agg ORDER BY ts_bucket").to_arrow_table()
            finally:
                mcon.close()
            # glibc-Arenen der geschlossenen Connection ans OS zurückgeben
            # (sonst summiert sich der Peak über die 76 Monats-Zykeln).
            _trim_native_memory()
            # hzrkl-Regelzustand über die Monatsgrenze: letzter Nicht-NULL-
            # Monatswert IN ZEITREIHENFOLGE (≡ Store-Endzustand; die
            # GROUP-BY-Row-Reihenfolge ist NICHT zeitlich geordnet →
            # ORDER BY ts_bucket ist Pflicht). None → Seed bleibt None durch
            # leere/roh-leere Monate unverändert.
            for z in config.HZRKL_ZONES:
                v = _last_non_null(part[f"hzrkl_{z}_rule_last"])
                if v is not None:
                    seeds[z] = float(v)
            agg_parts.append(part)
        y, m = ny, nm
    if not agg_parts:
        raise ValueError("winsol_pq liefert keine Rohdaten")
    agg_all = pa.concat_tables(agg_parts)
    del agg_parts  # Monats-Arrows freigeben; nur die Kombination bleibt

    # --- Phase E-Input: drei Arrow-Zwischenschichten aus DuckDB holen ---
    con.register("hourly_agg", agg_all)
    hourly_agg = con.sql("SELECT * FROM hourly_agg ORDER BY ts_bucket").to_arrow_table()
    wx_wide = con.sql("SELECT * FROM wx_wide").to_arrow_table()
    sun_daily = con.sql("SELECT * FROM sun_daily").to_arrow_table()

    # --- RAM (Pi-Budget): registrierte Zwischenschichten aus ``con`` löschen ---
    # Die 692-Spalten-Assembly (``_assemble_from_arrow``) ist reines
    # PyArrow/NumPy und braucht ``con`` NICHT mehr. ``con`` bleibt offen
    # (außen gehaltene Connection; ``build.py``/Tests nutzen sie weiter), aber
    # die registrierten Arrow-Tabellen/Views (``hourly_agg`` = 42 MB,
    # ``wx_long``/``wx_wide``/``sun_daily``) werden hier deregistriert, damit
    # der 692-Spalten-``cols``-Aufbau + Store-Table + zstd-Write-Buffer nicht
    # auf ~100 MB registrierte Arrow-Referenzen stößt. (R4''-Probe: ohne
    # Deregistrierung +250–300 MB Peak.)
    for _rel in ("hourly_agg", "wx_long", "wx_wide", "sun_daily"):
        with contextlib.suppress(duckdb.Error):
            con.unregister(_rel)
    with contextlib.suppress(duckdb.Error):
        con.execute("DROP TABLE IF EXISTS wx_wide")
        con.execute("DROP TABLE IF EXISTS hourly_agg_cur")
    del agg_all
    _trim_native_memory()  # glibc/DuckDB-Arenen ans OS (vor dem Assembly-Aufbau)

    # --- Phase E: columnare Assembly (ersetzt die finale SQL-Query) ---
    logf("[features] columnar: Assemble (692 Spalten, vektorisiert, ohne DuckDB)")
    out = _assemble_from_arrow(
        hourly_agg,
        wx_wide,
        sun_daily,
        bias_profile,
        git_tag,
        generated_at,
        start_hour=first_hour,
        last_hour=last_hour,
        context=None,
        out_dir=cfg.features_dir,
    )
    return out or []


def store_meta(
    cfg: config.BuildConfig,
    coverage: dict,
    bias: dict,
    wx_stats: dict,
    git_tag: str,
    generated_at: str,
) -> Path:
    """Schreibt ``data/features/_meta.json`` (Coverage + Quellen + Lücken)."""
    meta = {
        "data_version": DATA_VERSION,
        "git_tag": git_tag,
        "generated_at": generated_at,
        "features_dir": str(cfg.features_dir),
        "coverage": coverage,
        "bias_profile": bias,
        "wx_cache": wx_stats,
        "schema_doc": "analysis/model/m1/SCHEMA.md",
    }
    p = cfg.features_dir / "_meta.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.part")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)  # atomic rename within the same directory
    return p


__all__ = [
    "LAG_COLUMNS",
    "DATA_VERSION",
    "GAP_MIN_ROWS",
    "GAP_FULL_ROWS",
    "WX_SPLIT_UTC",
    "CONTEXT_HOURS",
    "last_full_hour_wall",
    "build_hour_grid",
    "sun_features_table",
    "load_wx_long",
    "wx_wide_sql",
    "wx_pivot_sql",
    "bias_terms",
    "assemble_features",
    "assemble_features_columnar",
    "write_month_parquet",
    "store_features",
    "store_meta",
]
