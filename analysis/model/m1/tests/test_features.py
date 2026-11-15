"""Tests: Feature-Store (Schema, Lücken-Flags, Bias-Terms, Parquet-Schreibweise).

Deckt:
- ``build_hour_grid`` (Raster-Bau, naive Berlin/UTC, Kalender-Features);
- ``load_wx_long`` (Cache-Load, Split-Filtering ERA5/ICON-D2);
- ``bias_terms`` (SQL-Generierung, Null-Fallback);
- Gap-Flags (gap/gap_partial/gap_full-Schwelle) am Feature-Store-Schema;
- Parquet-Schreibweise (zstd, key_value_Metadaten, Hive-Layout);
- ``store_meta`` (``_meta.json``).

Synthetisches Feature-Ende (kein echter Feature-Bau über alle Monate, aber
eine Mini-Pipeline: ``assemble_features``-SQL-Teile mit fiktiven Inputs).
"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from analysis.model.m1 import config
from analysis.model.m1.calendar_utils import BERLIN
from analysis.model.m1.features import (
    GAP_FULL_ROWS,
    GAP_MIN_ROWS,
    bias_terms,
    build_hour_grid,
    load_wx_long,
    sun_features_table,
    write_month_parquet,
)


def _make_wx_cache(tmp_path: Path, months: list[tuple[int, int]],
                   var: str = "temperature_2m", base: float = 10.0,
                   ns: str | None = None) -> None:
    """Künstlicher Open-Meteo-Cache (nur die benannte Variable).

    ``ns``: Cache-Namespace ('era5'/'hfa'). Default: 'era5' vor dem Split
    (2022-11), 'hfa' danach.
    """
    import calendar
    for y, m in months:
        last = calendar.monthrange(y, m)[1]
        times = [f"{y:04d}-{m:02d}-{d:02d}T{h:02d}:00"
                 for d in range(1, last + 1) for h in range(24)]
        data = {
            "hourly": {"time": times, var: [base + h * 0.1 for h in range(24)] * last},
            "daily": {"time": [f"{y:04d}-{m:02d}-{d:02d}" for d in range(1, last + 1)],
                      "sunrise": ["06:00"] * last, "sunset": ["18:00"] * last},
        }
        namespace = ns or ("era5" if (y, m) < (2022, 11) else "hfa")
        p = tmp_path / namespace
        p.mkdir(parents=True, exist_ok=True)
        (p / f"{y:04d}-{m:02d}.json").write_text(json.dumps(data), encoding="utf-8")


class TestBuildHourGrid:
    def test_range_and_columns(self):
        g = build_hour_grid(datetime(2024, 1, 1, tzinfo=BERLIN),
                            datetime(2024, 1, 1, 2, 0, tzinfo=BERLIN))
        assert g.num_rows == 3  # 00, 01, 02
        cols = set(g.column_names)
        for c in ("ts", "hour_utc", "hour", "day_of_week", "is_weekend",
                  "is_holiday", "holiday", "is_dst"):
            assert c in cols
        # 2024-01-01 = Montag (dow=0)
        assert g.column("day_of_week").to_pylist() == [0, 0, 0]

    def test_ts_is_naive_berlin_wallclock(self):
        g = build_hour_grid(datetime(2024, 6, 15, 10, 0, tzinfo=BERLIN),
                            datetime(2024, 6, 15, 10, 0, tzinfo=BERLIN))
        ts = g["ts"][0].as_py()
        assert ts.tzinfo is None  # naive
        assert ts == datetime(2024, 6, 15, 10, 0)
        # hour_utc = 08:00 (CEST = UTC+2)
        utc = g["hour_utc"][0].as_py()
        assert utc.tzinfo is None
        assert utc == datetime(2024, 6, 15, 8, 0)

    def test_dst_autumn_fallback_day(self):
        """DST-Rückfall (2024-10-27): 01:00 CEST und 01:00 CET = 2 Raster-Punkte."""
        g = build_hour_grid(datetime(2024, 10, 27, 0, 0, tzinfo=BERLIN),
                            datetime(2024, 10, 27, 2, 0, tzinfo=BERLIN))
        wall = [t.strftime("%Y-%m-%d %H") for t in g["ts"].to_pylist()]
        assert wall == ["2024-10-27 00", "2024-10-27 01", "2024-10-27 02"]
        # UTC-Abstand: 00→01 = 1 h, 01→02 = 1 h (wall-clock-Schritt)
        u = g["hour_utc"].to_pylist()
        assert (u[1] - u[0]).total_seconds() == 3600
        assert (u[2] - u[1]).total_seconds() == 3600

    def test_calendar_features(self):
        g = build_hour_grid(datetime(2024, 12, 25, 12, 0, tzinfo=BERLIN),
                            datetime(2024, 12, 25, 12, 0, tzinfo=BERLIN))
        assert g["is_holiday"][0].as_py() == 1
        assert g["holiday"][0].as_py() != "none"
        assert g["is_dst"][0].as_py() == 0

    def test_hour_of_day(self):
        g = build_hour_grid(datetime(2024, 1, 1, 20, 0, tzinfo=BERLIN),
                            datetime(2024, 1, 1, 22, 0, tzinfo=BERLIN))
        assert g["hour"].to_pylist() == [20, 21, 22]


class TestLoadWxLong:
    def test_era5_filtering(self, tmp_path):
        """ERA5-Monate: nur Stunden < WX_SPLIT_UTC (2022-11-14 00:00 UTC).

        Cache liegt im era5-Namespace (explizit, da (2022,11) per Default-Regel
        ins hfa-Namespace fiele).
        """
        months = [(2022, 11)]
        _make_wx_cache(tmp_path, months, ns="era5")
        tbl = load_wx_long(tmp_path, [(2022, 11)], [])
        # Alle Stunden 2022-11-01..13 (vor Split), keine ab 14.
        hours = tbl["hour_utc"].to_pylist()
        assert all(h < datetime(2022, 11, 14) for h in hours)
        assert all(h >= datetime(2022, 11, 1) for h in hours)
        # 13 Tage × 24 h = 312 (14. ist exkludiert)
        assert len(hours) == 13 * 24

    def test_hfa_filtering(self, tmp_path):
        """HFA-Monate (ICON-D2): nur Stunden >= WX_SPLIT_UTC.

        Cache-Datei liegt im hfa-Namespace (Default-Regel: (y,m) >= 2022-11).
        """
        months = [(2022, 11)]
        _make_wx_cache(tmp_path, months, base=5.0)
        tbl = load_wx_long(tmp_path, [], [(2022, 11)])
        hours = tbl["hour_utc"].to_pylist()
        assert all(h >= datetime(2022, 11, 14) for h in hours)
        # 17 Tage (14..30) × 24 h = 408
        assert len(hours) == 17 * 24
        # src = icon_d2
        assert set(tbl["src"].to_pylist()) == {"icon_d2"}

    def test_era5_src_label(self, tmp_path):
        _make_wx_cache(tmp_path, [(2020, 8)])
        tbl = load_wx_long(tmp_path, [(2020, 8)], [])
        assert set(tbl["src"].to_pylist()) == {"era5"}
        assert len(tbl["hour_utc"].to_pylist()) == 31 * 24

    def test_missing_month_skipped(self, tmp_path):
        """Keine Cache-Datei → leere Table (kein Fehler)."""
        tbl = load_wx_long(tmp_path, [(2020, 8)], [])
        assert tbl.num_rows == 0

    def test_value_conversion(self, tmp_path):
        """Werte werden zu Float konvertiert (None → ausgelassen)."""
        _make_wx_cache(tmp_path, [(2020, 8)])
        tbl = load_wx_long(tmp_path, [(2020, 8)], [])
        vals = tbl["value"].to_pylist()
        assert all(isinstance(v, float) for v in vals)
        # base=10.0, h=0 → 10.0
        assert 10.0 in vals

    def test_var_column(self, tmp_path):
        _make_wx_cache(tmp_path, [(2020, 8)], var="rain")
        tbl = load_wx_long(tmp_path, [(2020, 8)], [])
        assert set(tbl["var"].to_pylist()) == {"rain"}


class TestBiasTerms:
    def test_no_profile_identity(self):
        terms = bias_terms(None, 4)
        assert terms[0] == "wx_t0_t AS wx_t0_t_bias"
        assert terms[3] == "wx_t3_t AS wx_t3_t_bias"
        assert len(terms) == 4

    def test_with_profile(self):
        prof = {
            "DJF": {"offset_k": 1.0, "drift_k_per_day": 0.01},
            "MAM": {"offset_k": 0.0, "drift_k_per_day": 0.0},
            "JJA": {"offset_k": -0.5, "drift_k_per_day": 0.02},
            "SON": {"offset_k": 0.3, "drift_k_per_day": -0.01},
        }
        terms = bias_terms(prof, 4)
        assert len(terms) == 4
        # Offset + Drift pro Saison
        for t in terms:
            assert "1.0" in t   # DJF offset
            assert "0.01" in t  # DJF drift
            assert "0.3" in t   # SON offset
            assert "wx_t" in t

    def test_bias_computation_duckdb(self, con):
        """End-to-end: Bias-Offset + Drift werden korrekt angewendet."""
        prof = {
            "DJF": {"offset_k": 1.0, "drift_k_per_day": 0.0},
            "MAM": {"offset_k": 0.0, "drift_k_per_day": 0.0},
            "JJA": {"offset_k": 0.0, "drift_k_per_day": 0.0},
            "SON": {"offset_k": 0.0, "drift_k_per_day": 0.0},
        }
        terms = bias_terms(prof, 2)
        # g.ts = 2024-01-15 (DJF, Saison-Start = 2024-01-01, 14 Tage davor)
        con.execute("CREATE TABLE g AS SELECT TIMESTAMP '2024-01-15 00:00' ts")
        con.execute("CREATE TABLE wx AS SELECT 5.0 AS wx_t0_t, 6.0 AS wx_t1_t")
        row = con.execute(f"SELECT {', '.join(terms)} FROM g CROSS JOIN wx").fetchone()
        # DJF-Offset = 1.0, Drift = 0 → 5.0 + 1.0 = 6.0
        assert row[0] == 6.0
        assert row[1] == 7.0

    def test_bias_null_grid(self, con):
        prof = {s: {"offset_k": 1.0, "drift_k_per_day": 0.0} for s in config.BIAS_SEASONS}
        terms = bias_terms(prof, 1)
        con.execute("CREATE TABLE g AS SELECT TIMESTAMP '2024-01-15 00:00' ts")
        con.execute("CREATE TABLE wx AS SELECT NULL::DOUBLE AS wx_t0_t")
        row = con.execute(f"SELECT {terms[0]} FROM g CROSS JOIN wx").fetchone()
        assert row[0] is None


class TestGapFlags:
    """Gap-Flag-Logik (n_rows → gap/gap_partial/gap_full)."""

    def test_gap_flag_sql(self, con):
        """SQL-Logik: n_rows < 60 → gap_full; 60 ≤ n < 356 → gap_partial; else ok."""
        # Teste die Flag-Logik isoliert (ohne kompletten Feature-Bau)
        con.execute("CREATE TABLE agg AS SELECT * FROM (VALUES "
                    "('2024-01-01 00:00', 0), ('2024-01-01 01:00', 30), "
                    "('2024-01-01 02:00', 200), ('2024-01-01 03:00', 360)) v(ts_bucket, n_rows)")
        con.execute("CREATE TABLE g AS SELECT ts_bucket, n_rows FROM agg")
        sql = f"""
        SELECT ts_bucket,
               CASE WHEN COALESCE(n_rows, 0) = 0 THEN 1
                    WHEN COALESCE(n_rows, 0) < {GAP_FULL_ROWS} THEN 1
                    ELSE 0 END AS gap,
               CASE WHEN COALESCE(n_rows, 0) < {GAP_MIN_ROWS} THEN 1 ELSE 0 END AS gap_full,
               CASE WHEN COALESCE(n_rows, 0) >= {GAP_MIN_ROWS}
                     AND COALESCE(n_rows, 0) < {GAP_FULL_ROWS} THEN 1 ELSE 0 END AS gap_partial
        FROM g ORDER BY ts_bucket
        """
        rows = con.execute(sql).fetchall()
        # 0 Rows → gap=1, gap_full=1
        assert rows[0][1] == 1 and rows[0][2] == 1 and rows[0][3] == 0
        # 30 Rows → gap=1, gap_full=1 (unter 60)
        assert rows[1][1] == 1 and rows[1][2] == 1 and rows[1][3] == 0
        # 200 Rows → gap=1, gap_partial=1 (60..355)
        assert rows[2][1] == 1 and rows[2][2] == 0 and rows[2][3] == 1
        # 360 Rows → kein Gap
        assert rows[3][1] == 0 and rows[3][2] == 0 and rows[3][3] == 0

    def test_threshold_constants(self):
        """Schwellwerte: 60 und 356 (konfiguriert)."""
        assert GAP_MIN_ROWS == 60
        assert GAP_FULL_ROWS == 356


class TestWriteMonthParquet:
    def test_write_creates_file_with_metadata(self, tmp_path):
        """Hive-Layout + zstd + key_value_Metadaten."""
        n = 720
        t = pa.table({
            "ts": pa.array(
                [datetime(2024, 1, 1) + timedelta(hours=i) for i in range(n)],
                type=pa.timestamp("us")),
            "ext": pa.array([1.0] * n),
        })
        p = write_month_parquet(t, "2024-01", tmp_path, git_tag="abc123",
                                generated_at="2026-01-01T00:00:00")
        assert p.exists()
        assert p == tmp_path / "2024" / "01" / "2024-01.parquet"
        md = pq.read_metadata(p)
        kv = md.metadata
        assert kv is not None
        assert kv.get(b"data_version") == b"m1.3"
        assert kv.get(b"git_tag") == b"abc123"
        assert kv.get(b"rows") == str(n).encode()
        # zstd-Kompression
        pf = pq.ParquetFile(p)
        assert pf.metadata.row_group(0).column(0).compression == "ZSTD"
        # Row-Anzahl
        assert pf.metadata.num_rows == n

    def test_write_multiple_months(self, tmp_path):
        t1 = pa.table({"ts": pa.array([datetime(2024, 1, 15)], type=pa.timestamp("us")),
                       "ext": pa.array([1.0])})
        t2 = pa.table({"ts": pa.array([datetime(2024, 2, 15)], type=pa.timestamp("us")),
                       "ext": pa.array([2.0])})
        p1 = write_month_parquet(t1, "2024-01", tmp_path, "git", "ts")
        p2 = write_month_parquet(t2, "2024-02", tmp_path, "git", "ts")
        assert p1 != p2
        assert p1.parent != p2.parent  # verschiedene Monate

    def test_write_is_atomic_and_leaves_no_part_files(self, tmp_path):
        """S6-Maint-Loop: stündlicher Feature-Build überwriteet die Monate
        atomar (``.part`` + ``os.replace``, Muster s. ``app/cmi/cmi_log_pq.py``)
        — parallele Leser (L2 im Gateway, ``data/features`` ro-mountet) sehen
        nie eine halbschriebene Datei, und nach dem Lauf liegen keine
        ``*.parquet.part``-Reste im Store."""
        n1, n2 = 5, 9
        t1 = pa.table({"ts": pa.array(
            [datetime(2024, 1, 1) + timedelta(hours=i) for i in range(n1)],
            type=pa.timestamp("us")),
                       "ext": pa.array([1.0] * n1)})
        t2 = pa.table({"ts": pa.array(
            [datetime(2024, 1, 1) + timedelta(hours=i) for i in range(n2)],
            type=pa.timestamp("us")),
                       "ext": pa.array([2.0] * n2)})
        # Erstes Mal: Datei existiert noch nicht.
        p = write_month_parquet(t1, "2024-01", tmp_path, "git", "ts")
        assert pq.ParquetFile(p).metadata.num_rows == n1
        # Zweites Mal: In-Place-Overwrite (wie stündlicher Build-Refresh).
        p2 = write_month_parquet(t2, "2024-01", tmp_path, "git", "ts")
        assert p2 == p  # gleicher Endpfad
        assert pq.ParquetFile(p).metadata.num_rows == n2
        # Keine .part-Rückstände im Store-Verzeichnis (rekursiv).
        leftovers = [q for q in tmp_path.rglob("*.part") if q.is_file()]
        assert leftovers == []


class TestSunFeatures:
    def test_sun_features_table(self):
        days = [date(2024, 6, 21), date(2024, 12, 21)]
        t = sun_features_table(days)
        assert t.num_rows == 2
        day_col = t["day"]
        # Sommersonnenwende: lange Tagung
        summer = t.filter(pc.equal(day_col, pa.scalar(date(2024, 6, 21), type=pa.date32())))
        assert summer["daylight_min"][0].as_py() > 15 * 60
        # Wintersonnenwende: kurze Tagung
        winter = t.filter(pc.equal(day_col, pa.scalar(date(2024, 12, 21), type=pa.date32())))
        assert winter["daylight_min"][0].as_py() < 9 * 60


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect()
    yield c
    c.close()
