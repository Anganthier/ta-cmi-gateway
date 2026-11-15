"""Tests: 10-s → 1-h Resampling (synthetische 10-s-Daten, DuckDB).

Deckt: Bucket-Formation (Wallclock-Trunk), Mean (analog), Median (Offset-Δ),
Digital (An-Quotient + LAST), heat_min_* (DEPRECATED, Sum positiver Deltas, Reset-robust),
m1.1: RAS-Maske (vor/nach RAS_INSTALL_TS), ras_age_* (MAX inkl. Reset), {feat}_last
(letzter Nicht-NULL-Wert), Window+Aggregat-Kombination (DuckDB-2.0.0-Guard),
m1.2: TA-Sentinel-Maske (alle analogen Roh-Kanäle, Grenzwerte), hzrkl_* (a10–a15,
a14/a15-Vertauschung), Sentinel-Zähl-SQL,
Divergenz-Flag, offset_known, Source-Flag, DST-Fallback-Tag (2024-03-31:
Wallclock-Bucket 02:xx = 300 Rows, 03:xx = 60 Rows → kein duplizierter Bucket,
kein verschobener Bucket) + Quell-Schema-Check.
"""

import random
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from analysis.model.m1 import config, resample
from analysis.model.m1.calendar_utils import BERLIN
from analysis.model.m1.tests.test_incremental import _rows_for_hours, _write_month_rows


def make_rows(
    hour_start: datetime,
    n_rows: int,
    *,
    ext_val: float = 20.0,
    hz_vl_val: float = 50.0,
    ras_kell_perc_val: float = 21.0,
    ras_kell_ist_val: float = 20.5,
    ras_eg_perc_val: float = 22.0,
    ras_eg_ist_val: float = 21.0,
    ras_dg_perc_val: float = 23.0,
    ras_dg_ist_val: float = 22.5,
    ras2_kell_ist_val: float = 20.6,
    ras2_eg_ist_val: float = 21.2,
    ras2_dg_ist_val: float = 22.6,
    ras_kell_perc_vals=None,
    flue_vals=None,
    wood_vl_vals=None,
    buff_bot_vals=None,
    heat_kell_vals=None,
    heat_eg_vals=None,
    heat_dg_vals=None,
    oel_burner_vals=None,
    pump_hz_vals=None,
    hzrkl_on_vals=None,
    hzrkl_pos_vals=None,
) -> pa.Table:
    """n_rows 10-s-Rows für eine Stunde (oder Teil-Stunde) ab ``hour_start``.

    ``hzrkl_on_vals`` = (a10, a11, a12)-Listen (binär 0/10, Default 10/0/10),
    ``hzrkl_pos_vals`` = (a13, a14, a15)-Listen (Default 30/40/50).
    """
    heat_kell_vals = heat_kell_vals or [i for i in range(n_rows)]
    heat_eg_vals = heat_eg_vals or [0] * n_rows
    heat_dg_vals = heat_dg_vals or [0] * n_rows
    ras_kell_perc_vals = ras_kell_perc_vals or [ras_kell_perc_val] * n_rows
    flue_vals = flue_vals or [1.0] * n_rows
    wood_vl_vals = wood_vl_vals or [1.0] * n_rows
    buff_bot_vals = buff_bot_vals or [1.0] * n_rows
    oel_burner_vals = oel_burner_vals or [1] * n_rows
    pump_hz_vals = pump_hz_vals or [0] * n_rows
    hzrkl_on_vals = hzrkl_on_vals or ([10.0] * n_rows, [0.0] * n_rows, [10.0] * n_rows)
    hzrkl_pos_vals = hzrkl_pos_vals or ([30.0] * n_rows, [40.0] * n_rows, [50.0] * n_rows)
    ts = []
    for i in range(n_rows):
        t = hour_start + timedelta(seconds=10 * i)
        ts.append(t.strftime("%Y-%m-%d %H:%M:%S"))
    return pa.table(
        {
            "ts": pa.array(ts, type=pa.string()),
            "dev0_analog_01": pa.array([1] * n_rows, type=pa.float64()),
            "dev0_analog_02": pa.array([1] * n_rows, type=pa.float64()),
            "dev0_analog_03": pa.array(buff_bot_vals, type=pa.float64()),
            "dev0_analog_04": pa.array([1] * n_rows, type=pa.float64()),
            "dev0_analog_05": pa.array([1] * n_rows, type=pa.float64()),
            "dev0_analog_06": pa.array([hz_vl_val] * n_rows, type=pa.float64()),
            "dev0_analog_07": pa.array([1] * n_rows, type=pa.float64()),
            "dev0_analog_08": pa.array([ext_val] * n_rows, type=pa.float64()),
            "dev0_analog_09": pa.array(wood_vl_vals, type=pa.float64()),
            "dev0_analog_10": pa.array(flue_vals, type=pa.float64()),
            "dev0_analog_15": pa.array([1] * n_rows, type=pa.float64()),
            "dev0_analog_16": pa.array([1] * n_rows, type=pa.float64()),
            "dev0_digital_02": pa.array([1] * n_rows, type=pa.float64()),
            "dev0_digital_03": pa.array([1] * n_rows, type=pa.float64()),
            "dev0_digital_04": pa.array([1] * n_rows, type=pa.float64()),
            "dev0_digital_05": pa.array(oel_burner_vals, type=pa.float64()),
            "dev0_digital_06": pa.array([1] * n_rows, type=pa.float64()),
            "dev0_digital_07": pa.array([1] * n_rows, type=pa.float64()),
            "dev0_digital_08": pa.array(pump_hz_vals, type=pa.float64()),
            "dev0_digital_09": pa.array([1] * n_rows, type=pa.float64()),
            "dev1_analog_01": pa.array(ras_kell_perc_vals, type=pa.float64()),
            "dev1_analog_02": pa.array([ras_kell_ist_val] * n_rows, type=pa.float64()),
            "dev1_analog_04": pa.array([ras_eg_perc_val] * n_rows, type=pa.float64()),
            "dev1_analog_05": pa.array([ras_eg_ist_val] * n_rows, type=pa.float64()),
            "dev1_analog_07": pa.array([ras_dg_perc_val] * n_rows, type=pa.float64()),
            "dev1_analog_08": pa.array([ras_dg_ist_val] * n_rows, type=pa.float64()),
            "dev1_analog_11": pa.array(heat_kell_vals, type=pa.float64()),
            "dev1_analog_12": pa.array(heat_eg_vals, type=pa.float64()),
            "dev1_analog_13": pa.array(heat_dg_vals, type=pa.float64()),
            "dev2_analog_01": pa.array([1] * n_rows, type=pa.float64()),
            "dev2_analog_02": pa.array([1] * n_rows, type=pa.float64()),
            "dev2_analog_03": pa.array([1] * n_rows, type=pa.float64()),
            "dev2_analog_04": pa.array([1] * n_rows, type=pa.float64()),
            "dev2_analog_05": pa.array([ras2_kell_ist_val] * n_rows, type=pa.float64()),
            "dev2_analog_06": pa.array([1] * n_rows, type=pa.float64()),
            "dev2_analog_07": pa.array([ras2_eg_ist_val] * n_rows, type=pa.float64()),
            "dev2_analog_08": pa.array([1] * n_rows, type=pa.float64()),
            "dev2_analog_09": pa.array([ras2_dg_ist_val] * n_rows, type=pa.float64()),
            "dev2_analog_10": pa.array(hzrkl_on_vals[0], type=pa.float64()),
            "dev2_analog_11": pa.array(hzrkl_on_vals[1], type=pa.float64()),
            "dev2_analog_12": pa.array(hzrkl_on_vals[2], type=pa.float64()),
            "dev2_analog_13": pa.array(hzrkl_pos_vals[0], type=pa.float64()),
            "dev2_analog_14": pa.array(hzrkl_pos_vals[1], type=pa.float64()),
            "dev2_analog_15": pa.array(hzrkl_pos_vals[2], type=pa.float64()),
            "dev2_analog_16": pa.array([1] * n_rows, type=pa.float64()),
            "dev2_digital_01": pa.array([1] * n_rows, type=pa.float64()),
        }
    )


def write_parquet(table: pa.Table, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")


class TestResample:
    def test_hourly_mean_and_offset_median(self, tmp_path):
        """Mean (analog) + Median (Offset-Δ) + An-Quotient + LAST + Divergenz."""
        hour = datetime(2024, 1, 15, 10, 0, tzinfo=BERLIN)
        t = make_rows(
            hour,
            360,
            ext_val=5.0,
            hz_vl_val=6.0,
            ras_kell_perc_val=7.0,
            ras_kell_ist_val=2.0,
            ras_eg_perc_val=8.0,
            ras_eg_ist_val=3.0,
            ras_dg_perc_val=9.0,
            ras_dg_ist_val=4.0,
            ras2_kell_ist_val=3.0,
            ras2_eg_ist_val=4.5,
            ras2_dg_ist_val=5.0,
        )
        write_parquet(t, tmp_path / "2024" / "01" / "2024-01.parquet")
        con = duckdb.connect()
        con.execute(resample.build_hourly_sql(str(tmp_path / "*/*/*.parquet")))
        row = con.execute("""
            SELECT ts_bucket, ext, hz_vl, offset_kell, offset_eg, offset_dg,
                   oel_burner_on, oel_burner_last, pump_hz_on, pump_hz_last,
                   ras_divergence_kell, ras_divergence_eg, ras_divergence_dg,
                   n_rows, source, offset_known
            FROM hourly_agg WHERE ts_bucket = '2024-01-15 10:00'
        """).fetchone()
        assert row[0] == "2024-01-15 10:00"
        assert row[1] == 5.0  # ext Mean
        assert row[2] == 6.0  # hz_vl Mean
        assert row[3] == 5.0  # offset_kell = 7 - 2 = 5 (Median)
        assert row[4] == 5.0  # offset_eg = 8 - 3 = 5
        assert row[5] == 5.0  # offset_dg = 9 - 4 = 5
        assert row[6] == 1.0  # oel_burner_on = An-Quotient
        assert row[7] == 1.0  # oel_burner_last = letzter Zustand
        assert row[8] == 0.0  # pump_hz_on = 0 (alle 0)
        assert row[9] == 0.0  # pump_hz_last = 0
        # Divergenz-Flag (Schwelle: > 1.0 K):
        assert row[10] == 0.0  # kell: |2.0 − 3.0| = 1.0 K, NICHT > 1.0 K
        assert row[11] == 1.0  # eg: |3.0 − 4.5| = 1.5 K > 1.0 K
        assert row[12] == 0.0  # dg: |4.0 − 5.0| = 1.0 K, NICHT > 1.0 K
        assert row[13] == 360  # n_rows
        assert row[14] == "winsol_pq"  # source
        assert row[15] == 1  # offset_known
        con.close()

    def test_heat_min_sum_of_positive_deltas(self, tmp_path):
        """Heiz-Minuten = Summe positiver Deltas (robust gegen Zwischen-Resets).

        Referenz (reines Python): ``sum(max(v - prev, 0))`` über die 360 Samples,
        erste Zeile hat kein Vorgänger-Sample (≡ DuckDB LAG=NULL im Partition).
        """

        def _ref(vals: list[float]) -> float:
            total = 0.0
            prev = None
            for v in vals:
                if prev is not None:
                    total += max(v - prev, 0.0)
                prev = v
            return total

        hour = datetime(2024, 1, 15, 10, 0, tzinfo=BERLIN)
        # Keller: 0→199 steigen, dann stündlicher Reset auf 0 (negatives Delta → 0)
        heat_kell_vals = list(range(200)) + [0] + [0] * 159
        # EG: 0→99, Reset auf 0 (negatives Delta → 0), 0→258 steigen
        heat_eg_vals = list(range(100)) + [0] + list(range(259))
        # DG: konstant → kein Delta
        heat_dg_vals = [5.0] * 360
        assert all(len(v) == 360 for v in (heat_kell_vals, heat_eg_vals, heat_dg_vals))
        t = make_rows(
            hour,
            360,
            heat_kell_vals=heat_kell_vals,
            heat_eg_vals=heat_eg_vals,
            heat_dg_vals=heat_dg_vals,
        )
        write_parquet(t, tmp_path / "2024" / "01" / "2024-01.parquet")
        con = duckdb.connect()
        con.execute(resample.build_hourly_sql(str(tmp_path / "*/*/*.parquet")))
        row = con.execute("""
            SELECT heat_min_kell, heat_min_eg, heat_min_dg FROM hourly_agg
            WHERE ts_bucket = '2024-01-15 10:00'
        """).fetchone()
        assert row[0] == _ref(heat_kell_vals)  # 199
        assert row[1] == _ref(heat_eg_vals)  # 99 + 258 = 357
        assert row[2] == _ref(heat_dg_vals)  # 0
        # explizite Werte (Re-Check, unabhängig von der Referenz)
        assert row[0] == 199.0
        assert row[1] == 357.0
        assert row[2] == 0.0
        con.close()

    def test_heat_min_reset_is_not_subtracted(self, tmp_path):
        """Zwischen-Reset (Mid-hour): Abfall wird NICHT subtrahiert (nur positive Deltas)."""

        def _ref(vals):
            total, prev = 0.0, None
            for v in vals:
                if prev is not None:
                    total += max(v - prev, 0.0)
                prev = v
            return total

        hour = datetime(2024, 1, 15, 10, 0, tzinfo=BERLIN)
        # Zähler: 0,1,...,50 (51 Rows), Reset auf 0 (1 Row), dann 0,1,...,307 (308 Rows)
        vals = list(range(51)) + [0] + list(range(308))
        assert len(vals) == 360
        t = make_rows(hour, 360, heat_kell_vals=vals)
        write_parquet(t, tmp_path / "2024" / "01" / "2024-01.parquet")
        con = duckdb.connect()
        con.execute(resample.build_hourly_sql(str(tmp_path / "*/*/*.parquet")))
        row = con.execute(
            "SELECT heat_min_kell FROM hourly_agg WHERE ts_bucket = '2024-01-15 10:00'"
        ).fetchone()
        # Naives "last-first" wäre 307 - 0 = 307, aber der Reset auf 0 (von 50)
        # erzeugt ein negatives Delta (ignoriert) → korrekt = 50 + 307 = 357.
        assert row[0] == _ref(vals) == 357.0
        con.close()

    def test_digital_on_quotient_mixed(self, tmp_path):
        """Digital-Kanal mit gemischten Werten: An-Quotient = Mean, Last = letzter."""
        hour = datetime(2024, 1, 15, 10, 0, tzinfo=BERLIN)
        # 180 Ein (1), 180 Aus (0)
        oel_vals = [1] * 180 + [0] * 180
        pump_vals = [0] * 359 + [1]
        t = make_rows(hour, 360, oel_burner_vals=oel_vals, pump_hz_vals=pump_vals)
        write_parquet(t, tmp_path / "2024" / "01" / "2024-01.parquet")
        con = duckdb.connect()
        con.execute(resample.build_hourly_sql(str(tmp_path / "*/*/*.parquet")))
        row = con.execute("""
            SELECT oel_burner_on, oel_burner_last, pump_hz_on, pump_hz_last
            FROM hourly_agg WHERE ts_bucket = '2024-01-15 10:00'
        """).fetchone()
        assert row[0] == 0.5  # An-Quotient
        assert row[1] == 0.0  # letzter Zustand (0)
        assert abs(row[2] - 1 / 360) < 1e-9  # An-Quotient (1/360)
        assert row[3] == 1.0  # letzter Zustand (1)
        con.close()

    def test_dst_spring_fallback_day_buckets(self, tmp_path):
        """DST-Fallback-Tag 2024-03-31: 02:xx = 300 Rows, 03:xx = 60 Rows.

        Kein duplizierter Bucket (02:30 existiert wall-clock NICHT), kein
        verschobener Bucket (03:00 = 60 Rows, nicht 420).
        """
        # 2024-03-31 02:00 wall-clock: 300 Rows (02:00:08 → 02:49:58)
        h02 = datetime(2024, 3, 31, 2, 0, 8, tzinfo=BERLIN)
        t02 = make_rows(h02, 300)
        # 2024-03-31 03:00 wall-clock: 60 Rows (03:50:07 → 03:59:57)
        h03 = datetime(2024, 3, 31, 3, 50, 7, tzinfo=BERLIN)
        t03 = make_rows(h03, 60)
        # Vorherige volle Stunde: 01:00 → 360 Rows
        h01 = datetime(2024, 3, 31, 1, 0, 8, tzinfo=BERLIN)
        t01 = make_rows(h01, 360)
        # Nächste volle Stunde: 04:00 → 360 Rows
        h04 = datetime(2024, 3, 31, 4, 0, 8, tzinfo=BERLIN)
        t04 = make_rows(h04, 360)
        t = pa.concat_tables([t01, t02, t03, t04])
        write_parquet(t, tmp_path / "2024" / "03" / "2024-03.parquet")
        con = duckdb.connect()
        con.execute(resample.build_hourly_sql(str(tmp_path / "*/*/*.parquet")))
        rows = con.execute("""
            SELECT ts_bucket, n_rows FROM hourly_agg
            WHERE ts_bucket IN ('2024-03-31 01:00', '2024-03-31 02:00',
                                '2024-03-31 03:00', '2024-03-31 04:00')
            ORDER BY ts_bucket
        """).fetchall()
        assert rows == [
            ("2024-03-31 01:00", 360),
            ("2024-03-31 02:00", 300),
            ("2024-03-31 03:00", 60),
            ("2024-03-31 04:00", 360),
        ]
        # 03:00 = 60 Rows (Nicht 420 = 60+360, kein verschobener Bucket)
        assert rows[2][1] == 60
        con.close()

    def test_autumn_dst_repeat_day_full_buckets(self, tmp_path):
        """DST-Rückfall-Tag 2024-10-27: alle Buckets 360 Rows (Wallclock-Logik)."""
        h01a = datetime(2024, 10, 27, 1, 0, 6, tzinfo=BERLIN)  # CEST (erste 01:xx)
        h01b = datetime(2024, 10, 27, 1, 0, 6, tzinfo=BERLIN)  # CET (zweite 01:xx)
        # Beide schreiben in denselben Wallclock-Bucket 01:xx → 720 Rows
        t01 = pa.concat_tables([make_rows(h01a, 360), make_rows(h01b, 360)])
        h02 = datetime(2024, 10, 27, 2, 0, 6, tzinfo=BERLIN)
        t02 = make_rows(h02, 360)
        t = pa.concat_tables([t01, t02])
        write_parquet(t, tmp_path / "2024" / "10" / "2024-10.parquet")
        con = duckdb.connect()
        con.execute(resample.build_hourly_sql(str(tmp_path / "*/*/*.parquet")))
        rows = con.execute("""
            SELECT ts_bucket, n_rows FROM hourly_agg
            WHERE ts_bucket IN ('2024-10-27 01:00', '2024-10-27 02:00')
            ORDER BY ts_bucket
        """).fetchall()
        assert rows == [
            ("2024-10-27 01:00", 720),  # doppelter 01:xx (CEST + CET)
            ("2024-10-27 02:00", 360),
        ]
        con.close()

    def test_partial_hour(self, tmp_path):
        """Partial Stunde (60–355 Rows): State aus verfügbaren Rows, Flag im Feature-Store."""
        hour = datetime(2024, 1, 15, 10, 0, tzinfo=BERLIN)
        t = make_rows(hour, 200)  # nur 200 von 360 Rows
        write_parquet(t, tmp_path / "2024" / "01" / "2024-01.parquet")
        con = duckdb.connect()
        con.execute(resample.build_hourly_sql(str(tmp_path / "*/*/*.parquet")))
        row = con.execute(
            "SELECT n_rows, ext FROM hourly_agg WHERE ts_bucket = '2024-01-15 10:00'"
        ).fetchone()
        assert row[0] == 200
        assert row[1] == 20.0  # Mean aus 200 Rows
        con.close()

    def test_gap_hour_no_rows(self, tmp_path):
        """Fehlende Stunde (0 Rows): kein hourly_agg-Row → Feature-Store zeigt gap=1."""
        hour = datetime(2024, 1, 15, 10, 0, tzinfo=BERLIN)
        t = make_rows(hour, 360)
        # 11:00 fehlt komplett
        write_parquet(t, tmp_path / "2024" / "01" / "2024-01.parquet")
        con = duckdb.connect()
        con.execute(resample.build_hourly_sql(str(tmp_path / "*/*/*.parquet")))
        row = con.execute(
            "SELECT count(*) FROM hourly_agg WHERE ts_bucket = '2024-01-15 11:00'"
        ).fetchone()
        assert row[0] == 0  # kein Row (Fehlmonat = Gap)
        con.close()

    def test_multiple_hours_different_values(self, tmp_path):
        """Mehrere Stunden mit unterschiedlichen Werten: Mean je Stunde."""
        h1 = datetime(2024, 1, 15, 10, 0, tzinfo=BERLIN)
        h2 = datetime(2024, 1, 15, 11, 0, tzinfo=BERLIN)
        t1 = make_rows(h1, 360, ext_val=5.0)
        t2 = make_rows(h2, 360, ext_val=10.0)
        t = pa.concat_tables([t1, t2])
        write_parquet(t, tmp_path / "2024" / "01" / "2024-01.parquet")
        con = duckdb.connect()
        con.execute(resample.build_hourly_sql(str(tmp_path / "*/*/*.parquet")))
        rows = con.execute("""
            SELECT ts_bucket, ext FROM hourly_agg
            WHERE ts_bucket IN ('2024-01-15 10:00', '2024-01-15 11:00')
            ORDER BY ts_bucket
        """).fetchall()
        assert rows == [("2024-01-15 10:00", 5.0), ("2024-01-15 11:00", 10.0)]
        con.close()


class TestSourceSchema:
    def test_check_source_schema_ok(self, tmp_path):
        """Schema-Check: alle SOURCE_COLUMNS vorhanden, min/max ts korrekt."""
        hour = datetime(2024, 1, 15, 10, 0, tzinfo=BERLIN)
        t = make_rows(hour, 360)
        write_parquet(t, tmp_path / "2024" / "01" / "2024-01.parquet")
        con = duckdb.connect()
        info = resample.check_source_schema(con, str(tmp_path / "*/*/*.parquet"), "test")
        assert info["n_cols"] == len(resample.SOURCE_COLUMNS)
        assert info["missing"] == []
        assert info["n_rows"] == 360
        assert info["first"] == "2024-01-15 10:00:00"
        assert info["last"] == "2024-01-15 10:59:50"
        assert info["name"] == "test"
        con.close()

    def test_check_source_schema_missing_column(self, tmp_path):
        """Schema-Check: fehlende Spalte wird gemeldet."""
        hour = datetime(2024, 1, 15, 10, 0, tzinfo=BERLIN)
        t = make_rows(hour, 10)
        # dev0_analog_01 entfernen
        t2 = t.drop(columns=["dev0_analog_01"])
        write_parquet(t2, tmp_path / "2024" / "01" / "2024-01.parquet")
        con = duckdb.connect()
        info = resample.check_source_schema(con, str(tmp_path / "*/*/*.parquet"), "test")
        assert "dev0_analog_01" in info["missing"]
        con.close()

    def test_source_view_union_all(self, tmp_path):
        """Source-View: Union von zwei Quellen (winsol + cmi), Source-Flag je Zeile."""
        hour = datetime(2024, 1, 15, 10, 0, tzinfo=BERLIN)
        t1 = make_rows(hour, 10)
        t2 = make_rows(hour + timedelta(seconds=100), 10)
        write_parquet(t1, tmp_path / "winsol" / "2024" / "01" / "2024-01.parquet")
        write_parquet(t2, tmp_path / "cmi" / "2024" / "01" / "2024-01.parquet")
        con = duckdb.connect()
        con.execute(
            resample.build_source_view_sql(
                str(tmp_path / "winsol" / "*/*/*.parquet"),
                str(tmp_path / "cmi" / "*/*/*.parquet"),
            )
        )
        rows = con.execute("SELECT count(*), min(source), max(source) FROM src").fetchone()
        assert rows[0] == 20  # 10 + 10
        assert rows[1] == "cmi_log_pq"  # alphabetisch
        assert rows[2] == "winsol_pq"
        con.close()

    def test_build_hourly_sql_structure(self):
        """SQL-Struktur: 4 Views (src, src_bucketed, src_delta, hourly_agg)."""
        sql = resample.build_hourly_sql("glob_winsol", "glob_cmi")
        assert "CREATE OR REPLACE VIEW src AS" in sql
        assert "CREATE OR REPLACE VIEW src_bucketed AS" in sql
        assert "CREATE OR REPLACE VIEW src_delta AS" in sql
        assert "CREATE OR REPLACE VIEW hourly_agg AS" in sql
        # LAG-Window in src_delta (nicht in hourly_agg)
        delta_part = sql.split("CREATE OR REPLACE VIEW src_delta AS")[1].split(
            "CREATE OR REPLACE VIEW hourly_agg AS"
        )[0]
        assert "LAG(dev1_analog_11)" in delta_part
        # hourly_agg: keine LAG (nur SUM der Deltas)
        agg_part = sql.split("CREATE OR REPLACE VIEW hourly_agg AS")[1]
        assert "LAG(" not in agg_part
        assert "SUM(heat_min_kell_delta)" in agg_part

    def test_empty_hourly_pipeline_matches_real_types(self, tmp_path):
        """R4'-DoD: leeres ``hourly_agg`` (0-Row-src) ≙ echter Build im Schema-Typen.

        Chunks ohne Rohdaten (``build_empty_hourly_sql``) laufen über dieselbe
        4-Stufen-Pipeline wie der echte Build → die Aggregat-Typen müssen
        exakt identisch sein, sonst bricht ``pa.concat_tables`` der Monats-
        Dateien (DuckDB-Literal-Quirk: ``hzrkl_{z}_rule_last`` DECIMAL(2,1),
        da ``LAST(1.0/0.0 …)`` den DECIMAL-Typ der Event-Literale hält).
        """
        start = datetime(2024, 1, 1, 0, 0, tzinfo=BERLIN)
        winsol = tmp_path / "winsol"
        _write_month_rows(winsol, "2024-01", _rows_for_hours(start, 24))

        con = duckdb.connect()
        try:
            con.execute(resample.build_hourly_sql(str(winsol / "*/*/*.parquet")))
            real_cols = [r[0] for r in con.execute("DESCRIBE hourly_agg").fetchall()]
            real_types = {r[0]: r[1] for r in con.execute("DESCRIBE hourly_agg").fetchall()}
        finally:
            con.close()

        con2 = duckdb.connect()
        try:
            con2.execute(resample.build_empty_hourly_sql())
            empty_cols = [r[0] for r in con2.execute("DESCRIBE hourly_agg").fetchall()]
            empty_types = {r[0]: r[1] for r in con2.execute("DESCRIBE hourly_agg").fetchall()}
        finally:
            con2.close()

        assert real_cols == empty_cols, (
            f"Spalten-Reihenfolge/Name: only-real={set(real_cols) - set(empty_cols)}, "
            f"only-empty={set(empty_cols) - set(real_cols)}"
        )
        diffs = {
            c: (real_types[c], empty_types[c]) for c in real_cols if real_types[c] != empty_types[c]
        }
        assert not diffs, f"Typ-Divergenz zwischen echtem und leerem hourly_agg: {diffs}"
        # Kritische Spalte explizit (DuckDB-Quirk, s. Test-Doc):
        assert empty_types["hzrkl_kell_rule_last"].startswith("DECIMAL"), (
            f"hzrkl_kell_rule_last muss DECIMAL sein (war {empty_types['hzrkl_kell_rule_last']})"
        )

    def test_hourly_agg_parts_no_alias_collision(self):
        """Kein Alias-Konflikt: heat_min_* erscheint nicht doppelt (kein AVG + SUM)."""
        parts = resample.hourly_agg_parts()
        # heat_min_kell: nur als SUM(heat_min_kell_delta), nicht als AVG(dev1_analog_11)
        heat_min_parts = [p for p in parts if "heat_min_kell" in p]
        assert len(heat_min_parts) == 1
        assert "SUM(heat_min_kell_delta)" in heat_min_parts[0]
        # Kein AVG(dev1_analog_11) (Heiz-Minuten-Zähler)
        assert not any("AVG(dev1_analog_11)" in p for p in parts)
        # Aber: alle anderen analogen Kanäle haben AVG
        assert any("AVG(dev0_analog_08)" in p for p in parts)
        assert any("AVG(dev1_analog_01)" in p for p in parts)


# --- m1.1 (M2b-P2): RAS-Maske, ras_age_*, {feat}_last ---------------------------

RAS_DERIVED_AGG = [
    "ras_kell_perc",
    "ras_kell_ist",
    "ras_eg_perc",
    "ras_eg_ist",
    "ras_dg_perc",
    "ras_dg_ist",
    "offset_kell",
    "offset_eg",
    "offset_dg",
    "ras2_kell_ras",
    "ras2_kell_ist",
    "ras2_eg_ras",
    "ras2_eg_ist",
    "ras2_dg_ras",
    "ras2_dg_ist",
    "ras_divergence_kell",
    "ras_divergence_eg",
    "ras_divergence_dg",
    "ras_age_kell",
    "ras_age_eg",
    "ras_age_dg",
    "heat_min_kell",
    "heat_min_eg",
    "heat_min_dg",
    "ras_kell_perc_last",
    "ras_kell_ist_last",
    "ras_eg_perc_last",
    "ras_eg_ist_last",
    "ras_dg_perc_last",
    "ras_dg_ist_last",
]


def _replace(t: pa.Table, **cols) -> pa.Table:
    """Ersetzt Roh-Spalten (float64, None = NULL)."""
    for name, vals in cols.items():
        idx = t.schema.get_field_index(name)
        t = t.set_column(idx, name, pa.array(vals, type=pa.float64()))
    return t


def _agg_rows(con: duckdb.DuckDBPyConnection) -> dict[str, dict]:
    cur = con.execute("SELECT * FROM hourly_agg ORDER BY ts_bucket")
    names = [d[0] for d in cur.description]
    return {r[0]: dict(zip(names, r, strict=True)) for r in cur.fetchall()}


class TestRasMask:
    def test_src_view_masks_exactly_at_install_ts(self, tmp_path):
        """Roh-ts < RAS_INSTALL_TS → alle RAS-Kanäle NULL; ab RAS_INSTALL_TS → Rohwert.
        Nicht-RAS-Kanäle (dev0, dev2 Solar) bleiben unberührt."""
        start = datetime(2021, 10, 20, 16, 59, 30, tzinfo=BERLIN)  # 16:59:30 … 17:00:20
        write_parquet(make_rows(start, 6), tmp_path / "2021" / "10" / "2021-10.parquet")
        con = duckdb.connect()
        con.execute(resample.build_source_view_sql(str(tmp_path / "*/*/*.parquet")))
        ras = config.RAS_RAW_COLUMNS
        others = [
            "dev0_analog_08",
            "dev0_analog_06",
            "dev2_analog_01",
            "dev2_analog_16",
            "dev2_digital_01",
            "dev0_digital_05",
        ]
        rows = con.execute(
            f"SELECT ts, {', '.join(ras)}, {', '.join(others)} FROM src ORDER BY ts"
        ).fetchall()
        assert [r[0] for r in rows][2:4] == ["2021-10-20 16:59:50", "2021-10-20 17:00:00"]
        for r in rows:
            ras_vals = r[1 : 1 + len(ras)]
            other_vals = r[1 + len(ras) :]
            if r[0] < config.RAS_INSTALL_TS:
                assert all(v is None for v in ras_vals), r[0]
            else:
                assert all(v is not None for v in ras_vals), r[0]
            assert all(v is not None for v in other_vals), r[0]
        assert sum(r[0] < config.RAS_INSTALL_TS for r in rows) == 3
        con.close()

    def test_hourly_agg_before_and_after_install(self, tmp_path):
        """Stunde 16:00 (vor Einbau): alle RAS-abgeleiteten Aggregate NULL, offset_known
        falsch; Stunde 17:00: Werte aus Rohdaten. Nicht-RAS-Features in beiden Stunden."""
        h16 = datetime(2021, 10, 20, 16, 0, tzinfo=BERLIN)
        h17 = datetime(2021, 10, 20, 17, 0, tzinfo=BERLIN)
        t = pa.concat_tables(
            [
                make_rows(h16, 360, ras_kell_ist_val=-30.0, ras_kell_perc_val=-29.0),  # Müll
                make_rows(h17, 360),
            ]
        )
        write_parquet(t, tmp_path / "2021" / "10" / "2021-10.parquet")
        con = duckdb.connect()
        con.execute(resample.build_hourly_sql(str(tmp_path / "*/*/*.parquet")))
        rows = _agg_rows(con)
        pre, post = rows["2021-10-20 16:00"], rows["2021-10-20 17:00"]
        for c in RAS_DERIVED_AGG:
            assert pre[c] is None, f"vor Einbau nicht NULL: {c}"
            assert post[c] is not None, f"nach Einbau NULL: {c}"
        assert not pre["offset_known"]
        assert post["offset_known"]
        for c in (
            "ext",
            "ext_last",
            "hz_vl",
            "hz_vl_last",
            "solar_tank_top",
            "solar_coll",
            "solar_coll_last",
            "oel_burner_on",
            "pump_solar_last",
        ):
            assert pre[c] is not None and post[c] is not None, c
        assert pre["n_rows"] == post["n_rows"] == 360
        assert post["ras_kell_ist"] == 20.5
        assert post["ras_kell_ist_last"] == 20.5
        assert post["offset_kell"] == 0.5
        assert post["ras_age_kell"] == 359.0  # MAX des Zählers 0…359
        assert post["heat_min_kell"] == 359.0  # DEPRECATED, Berechnung wie m1.0
        assert post["ras_divergence_eg"] == 0.0  # |21.0 − 21.2| ≤ 1 K
        con.close()


class TestRasAge:
    def test_ras_age_is_hourly_max_with_reset(self, tmp_path):
        """ras_age = Stunden-MAX (Sägezahn mit Reset innerhalb der Stunde); NULL-Stunde → NULL
        (auch das DEPRECATED heat_min_* ist dann NULL statt 0)."""
        hour = datetime(2024, 1, 15, 10, 0, tzinfo=BERLIN)
        # Keller: +0,1 je 60 s (6 Rows), Reset auf 0 nach 1,1 → MAX 1,1
        kell = [round((i // 6) % 12 * 0.1, 1) for i in range(360)]
        # EG: Funkausfall → steigt bis 4,9, dann Reset und Neustart (Endwert 0,9) → MAX 4,9
        eg = [round((i // 6) * 0.1, 1) for i in range(300)] + [
            round((j // 6) * 0.1, 1) for j in range(60)
        ]
        dg = [None] * 360
        t = make_rows(hour, 360, heat_kell_vals=kell, heat_eg_vals=eg, heat_dg_vals=dg)
        write_parquet(t, tmp_path / "2024" / "01" / "2024-01.parquet")
        con = duckdb.connect()
        con.execute(resample.build_hourly_sql(str(tmp_path / "*/*/*.parquet")))
        r = _agg_rows(con)["2024-01-15 10:00"]
        assert r["ras_age_kell"] == pytest.approx(1.1)
        assert r["ras_age_eg"] == pytest.approx(4.9)
        assert eg[-1] == pytest.approx(0.9)  # Endwert ≠ MAX
        assert r["ras_age_dg"] is None
        assert r["heat_min_dg"] is None
        # heat_min_eg (DEPRECATED) = Summe positiver Deltas = 4,9 + 0,9
        assert r["heat_min_eg"] == pytest.approx(5.8)
        con.close()


class TestLastValue:
    def test_last_non_null_even_if_last_row_null(self, tmp_path):
        """{feat}_last = letzter Nicht-NULL-Wert nach ts (nicht Datei-Reihenfolge), auch wenn
        die letzten Zeilen NULL sind; Stunde ganz ohne Wert → NULL."""
        hour = datetime(2024, 1, 15, 10, 0, tzinfo=BERLIN)
        n = 360
        ext = [5.0] * 300 + [7.0] * 50 + [None] * 10
        ras_kell_ist = [float(i) for i in range(n - 1)] + [None]
        t = _replace(
            make_rows(hour, n),
            dev0_analog_08=ext,
            dev1_analog_02=ras_kell_ist,
            dev0_analog_01=[None] * n,
        )
        t = t.take(pa.array(list(range(n - 1, -1, -1))))  # umgekehrte Datei-Reihenfolge
        write_parquet(t, tmp_path / "2024" / "01" / "2024-01.parquet")
        con = duckdb.connect()
        con.execute(resample.build_hourly_sql(str(tmp_path / "*/*/*.parquet")))
        r = _agg_rows(con)["2024-01-15 10:00"]
        assert r["ext_last"] == 7.0
        assert r["ext"] == pytest.approx((300 * 5.0 + 50 * 7.0) / 350)
        assert r["ras_kell_ist_last"] == 358.0
        assert r["buff_top"] is None and r["buff_top_last"] is None
        assert r["hz_vl_last"] == 50.0
        con.close()

    def test_agg_aliases_unique_and_no_digital_collision(self):
        """Alle Aggregat-Aliase eindeutig; {feat}_last kollidiert nicht mit Digital-*_last;
        kein Window-Ausdruck in hourly_agg (DuckDB-2.0.0-Bug)."""
        parts = resample.hourly_agg_parts()
        aliases = [p.rsplit(" AS ", 1)[1] for p in parts]
        assert len(aliases) == len(set(aliases))
        last_names = {f"{f}_last" for f in config.LAST_VALUE_FEATURES}
        assert len(config.LAST_VALUE_FEATURES) == 20
        assert last_names <= set(aliases)
        digital_last = {
            f"{n}_last" for n in (*config.DEV0_DIGITAL.values(), *config.DEV2_DIGITAL.values())
        }
        assert digital_last <= set(aliases)
        assert not (digital_last & last_names)
        assert {"ras_age_kell", "ras_age_eg", "ras_age_dg"} <= set(aliases)
        assert not any(" OVER " in p for p in parts)
        assert "MAX(dev1_analog_11) AS ras_age_kell" in parts
        # m1.2: hzrkl-Aliase (keine Kollision, a14 → dg, a15 → eg)
        hz = {
            f"hzrkl_{z}_{k}"
            for z in ("kell", "eg", "dg")
            for k in ("on", "last", "pos", "pos_last")
        }
        assert hz <= set(aliases)
        assert "AVG(dev2_analog_10 / 10.0) AS hzrkl_kell_on" in parts
        assert "AVG(dev2_analog_14) AS hzrkl_dg_pos" in parts
        assert "AVG(dev2_analog_15) AS hzrkl_eg_pos" in parts


class TestWindowAggregateCombination:
    def test_multi_hour_matches_python_reference(self, tmp_path):
        """DuckDB-2.0.0-Guard: LAG-Deltas (src_delta) + MAX + LAST…FILTER + AVG über
        mehrere Stunden (geshuffelte Datei, NULL-Einstreuungen) == Python-Referenz.
        m1.2: inkl. TA-Sentinels (Maske in der src-View) und hzrkl_* (AVG/÷10, LAST)."""
        rng = random.Random(20261031)
        tables, ref = [], {}
        for k in range(4):
            h = datetime(2024, 1, 15, 10 + k, 0, tzinfo=BERLIN)
            saw, v = [], 0.0
            for _ in range(360):
                v = 0.0 if rng.random() < 0.02 else round(v + 0.1 * rng.random(), 3)
                saw.append(v)
            ext = [round(rng.uniform(-5, 15), 3) for _ in range(360)]
            for i in rng.sample(range(360), 20):
                saw[i] = None
            for i in rng.sample(range(360), 2):  # m1.2: TA-Sentinel im Sägezahn
                saw[i] = 3276.7
            for i in rng.sample(range(360), 3 + k):  # m1.2: TA-Sentinels → NULL
                ext[i] = rng.choice([3276.7, -999.9])
            for i in range(360 - 5 * k, 360):  # NULL-Ende (k Stunden-abhängig)
                ext[i] = None
            ext[-1] = 3276.7  # Sentinel als letzte Zeile → _last überspringt ihn
            on = [None if rng.random() < 0.05 else rng.choice([0.0, 10.0]) for _ in range(360)]
            pos = [round(rng.uniform(11, 65), 1) for _ in range(360)]
            for i in range(360 - 3 * k, 360):
                pos[i] = None
            tables.append(
                _replace(
                    make_rows(h, 360),
                    dev1_analog_11=saw,
                    dev0_analog_08=ext,
                    dev2_analog_10=on,
                    dev2_analog_14=pos,
                )
            )
            valid = [None if x is not None and (x >= 3276.0 or x <= -999.0) else x for x in saw]
            heat, prev = 0.0, None
            for x in valid:
                if x is not None and prev is not None:
                    heat += max(x - prev, 0.0)
                prev = x
            nn = [x for x in ext if x is not None and -999.0 < x < 3276.0]
            on_nn = [x for x in on if x is not None]
            pos_nn = [x for x in pos if x is not None]
            ref[h.strftime("%Y-%m-%d %H:00")] = {
                "heat_min_kell": heat,
                "ras_age_kell": max(x for x in valid if x is not None),
                "ext_last": nn[-1],
                "ext": sum(nn) / len(nn),
                "hzrkl_kell_on": sum(on_nn) / len(on_nn) / 10.0,
                "hzrkl_kell_last": on_nn[-1] / 10.0,
                "hzrkl_dg_pos": sum(pos_nn) / len(pos_nn),
                "hzrkl_dg_pos_last": pos_nn[-1],
            }
        t = pa.concat_tables(tables)
        t = t.take(
            pa.array(rng.sample(range(t.num_rows), t.num_rows))
        )  # Datei-Reihenfolge zufällig
        write_parquet(t, tmp_path / "2024" / "01" / "2024-01.parquet")
        con = duckdb.connect()
        con.execute(resample.build_hourly_sql(str(tmp_path / "*/*/*.parquet")))
        rows = _agg_rows(con)
        assert set(rows) == set(ref)
        for bucket, exp in ref.items():
            for key, val in exp.items():
                assert rows[bucket][key] == pytest.approx(val, abs=1e-9), f"{bucket} {key}"
        con.close()


# --- m1.2: TA-Sentinel-Maske, Rücklaufminderer hzrkl_* -------------------------

SENTINEL_PROBE = [3276.7, -999.9, 3276.0, -999.0, 3275.9, -998.9, 251.3]


class TestSentinelMask:
    def test_src_view_masks_sentinels_in_all_analog_columns(self, tmp_path):
        """Alle analogen Roh-Kanäle: >= 3276.0 / <= -999.0 → NULL (inkl. Grenzwerte),
        3275.9 / -998.9 / 251.3 bleiben. Digital unberührt. Die Zeile bleibt erhalten,
        auch wenn der Zeilenfilter-Kanal dev0 a1 selbst ein Sentinel ist."""
        hour = datetime(2024, 1, 15, 10, 0, tzinfo=BERLIN)
        n = len(SENTINEL_PROBE)
        t = make_rows(hour, n)
        t = _replace(t, **{c: list(SENTINEL_PROBE) for c in resample.ANALOG_SOURCE_COLUMNS})
        t = _replace(t, dev0_digital_02=[None] * n)
        write_parquet(t, tmp_path / "2024" / "01" / "2024-01.parquet")
        con = duckdb.connect()
        con.execute(resample.build_source_view_sql(str(tmp_path / "*/*/*.parquet")))
        cols = resample.ANALOG_SOURCE_COLUMNS
        assert len(cols) == 37 and not any("digital" in c for c in cols)
        assert {f"dev2_analog_{ch}" for ch in range(10, 16)} <= set(cols)
        rows = con.execute(
            f"SELECT {', '.join(cols)}, dev0_digital_05 FROM src ORDER BY ts"
        ).fetchall()
        assert len(rows) == n
        for i, r in enumerate(rows):
            for col, value in zip(cols, r[:-1], strict=True):
                exp = None if i < 4 else SENTINEL_PROBE[i]
                if col == "dev0_analog_10":
                    exp = (
                        None
                        if i < 4 or SENTINEL_PROBE[i] * config.FLUE_SCALE > config.FLUE_MAX_C
                        else SENTINEL_PROBE[i] * config.FLUE_SCALE
                    )
                assert value == exp, (i, col, value, exp)
            assert r[-1] == 1.0
        con.close()

    def test_hourly_aggregates_ignore_sentinels(self, tmp_path):
        """flue-Mean/-Last ohne Sentinels, solar_coll nur Sentinels → NULL, RAS-Median
        und ras_age-MAX ohne Sentinels; n_rows unverändert."""
        hour = datetime(2024, 1, 15, 10, 0, tzinfo=BERLIN)
        n = 360
        flue = [10.0] * 300 + [3276.7] * 50 + [-999.9] * 10
        coll = [3276.7] * n
        ist = [3276.7] * 200 + [20.5] * 160  # Sentinel-Mehrheit
        age = [float(i % 12) / 10 for i in range(n)]
        age[100] = 3276.7
        t = _replace(
            make_rows(hour, n),
            dev0_analog_10=flue,
            dev2_analog_16=coll,
            dev1_analog_02=ist,
            dev1_analog_11=age,
        )
        write_parquet(t, tmp_path / "2024" / "01" / "2024-01.parquet")
        con = duckdb.connect()
        con.execute(resample.build_hourly_sql(str(tmp_path / "*/*/*.parquet")))
        r = _agg_rows(con)["2024-01-15 10:00"]
        assert r["n_rows"] == n
        assert r["flue"] == 100.0 and r["flue_last"] == 100.0
        assert r["solar_coll"] is None and r["solar_coll_last"] is None
        assert r["ras_kell_ist"] == 20.5 and r["ras_kell_ist_last"] == 20.5
        assert r["offset_kell"] == 0.5
        assert r["ras_age_kell"] == pytest.approx(1.1)
        con.close()

    def test_sentinel_count_sql(self, tmp_path):
        hour = datetime(2024, 1, 15, 10, 0, tzinfo=BERLIN)
        t = _replace(
            make_rows(hour, 7), dev0_analog_10=list(SENTINEL_PROBE), dev2_analog_16=[3276.7] * 7
        )
        write_parquet(t, tmp_path / "2024" / "01" / "2024-01.parquet")
        con = duckdb.connect()
        cur = con.execute(resample.sentinel_count_sql(str(tmp_path / "*/*/*.parquet")))
        res = dict(zip([d[0] for d in cur.description], cur.fetchone(), strict=True))
        assert res["dev0_analog_10|hi"] == 2 and res["dev0_analog_10|lo"] == 2
        assert res["dev2_analog_16|hi"] == 7 and res["dev2_analog_16|lo"] == 0
        assert sum(res.values()) == 11
        con.close()


class TestHzrkl:
    def test_on_last_and_pos_mapping(self, tmp_path):
        """a10–a12 (0/10) → hzrkl_{z}_on = AVG/10 + _last; a13/a14/a15 → kell/dg/eg _pos
        (a14 = OG → dg, a15 = EG) + _pos_last; Kanal ganz NULL → NULL."""
        hour = datetime(2024, 1, 15, 10, 0, tzinfo=BERLIN)
        n = 360
        a10 = [10.0] * 90 + [0.0] * 270  # 25 % an, Endwert 0
        a11 = [10.0] * (n - 1) + [None]  # immer an, NULL am Ende
        a12 = [None] * n
        a13 = [30.0] * (n - 1) + [33.0]
        a14 = [40.0] * n
        a15 = [50.0] * (n - 2) + [55.0, None]
        t = make_rows(hour, n, hzrkl_on_vals=(a10, a11, a12), hzrkl_pos_vals=(a13, a14, a15))
        write_parquet(t, tmp_path / "2024" / "01" / "2024-01.parquet")
        con = duckdb.connect()
        con.execute(resample.build_hourly_sql(str(tmp_path / "*/*/*.parquet")))
        r = _agg_rows(con)["2024-01-15 10:00"]
        assert r["hzrkl_kell_on"] == pytest.approx(0.25)
        assert r["hzrkl_kell_last"] == 0.0
        assert r["hzrkl_eg_on"] == 1.0 and r["hzrkl_eg_last"] == 1.0
        assert r["hzrkl_dg_on"] is None and r["hzrkl_dg_last"] is None
        assert r["hzrkl_kell_pos"] == pytest.approx((30.0 * 359 + 33.0) / 360)
        assert r["hzrkl_kell_pos_last"] == 33.0
        assert r["hzrkl_dg_pos"] == 40.0 and r["hzrkl_dg_pos_last"] == 40.0  # a14
        assert r["hzrkl_eg_pos"] == pytest.approx((50.0 * 358 + 55.0) / 359)  # a15
        assert r["hzrkl_eg_pos_last"] == 55.0
        con.close()


class TestM13Rules:
    def test_flue_scale_defect_window_and_500_degree_limit(self, tmp_path):
        """Abgas ×10, Defektzeitraum NULL und Werte >500 °C maskiert; 500 bleibt gültig."""
        hours = [
            (datetime(2022, 4, 28, 9, 0), [36.0, 50.1]),
            (datetime(2022, 4, 28, 10, 0), [36.0, 40.0]),
            (datetime(2022, 5, 6, 13, 0), [36.0, 40.0]),
            (datetime(2022, 5, 6, 14, 0), [50.0, 50.1]),
        ]
        tables = [make_rows(h, len(v), flue_vals=v) for h, v in hours]
        write_parquet(pa.concat_tables(tables), tmp_path / "2022" / "04" / "2022-04.parquet")
        con = duckdb.connect()
        con.execute(resample.build_hourly_sql(str(tmp_path / "*/*/*.parquet")))
        rows = _agg_rows(con)
        assert rows["2022-04-28 09:00"]["flue"] == pytest.approx(360.0)
        assert rows["2022-04-28 09:00"]["flue_last"] == pytest.approx(360.0)
        assert rows["2022-04-28 10:00"]["flue"] is None
        assert rows["2022-05-06 13:00"]["flue"] is None
        assert rows["2022-05-06 14:00"]["flue"] == pytest.approx(500.0)
        assert rows["2022-05-06 14:00"]["flue_last"] == pytest.approx(500.0)
        con.close()

    def test_hzrkl_rule_hold_and_wood_proxies(self, tmp_path):
        """Minderer hält bei 20,1 °C; O1-Regel und Abgas-Holz-An sind Proxy-Signale."""
        hour = datetime(2024, 1, 15, 10, 0)
        sequence = [20.0, None, 20.1, 20.2] * 90
        t = make_rows(
            hour,
            360,
            ras_kell_perc_vals=sequence,
            wood_vl_vals=[70.0, 65.0] * 180,
            buff_bot_vals=[60.0] * 360,
            flue_vals=[10.1, 10.0] * 180,
        )
        write_parquet(t, tmp_path / "2024" / "01" / "2024-01.parquet")
        con = duckdb.connect()
        con.execute(resample.build_hourly_sql(str(tmp_path / "*/*/*.parquet")))
        r = _agg_rows(con)["2024-01-15 10:00"]
        assert r["hzrkl_kell_rule"] == pytest.approx(0.75)
        assert r["hzrkl_kell_rule_last"] == 0.0
        assert r["pump_wood_est_on"] == pytest.approx(0.5)
        assert r["wood_fire_on"] == pytest.approx(0.5)
        con.close()
