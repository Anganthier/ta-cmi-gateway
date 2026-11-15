"""Tests: M1-Columnar — vektorisierte 692-Spalten-Assembly (Produktions-Pfad).

Deckt (M1-Columnar, 2026-11):
- **Kern-DoD (stufenweise)**: columnar Full-Build (``store_features`` auf
  ``config.con()`` = Pi-Budget 500 MB) ≡ monolithischer Referenz-Build
  (``assemble_features`` auf unbeschränkter Verbindung, SQL-Orakel):
  Stufe 1 Grid/Gap/hourly_agg-Spalten, Stufe 2 LAG, Stufe 3 WX+Bias,
  Stufe 4 Runtime-SUM/SINCE, Stufe 5 Meta + Store-Ordnung/Typen (692 Spalten);
  diskret bit-exakt, Float rel ≤ ``VERIFY_FLOAT_REL_TOL`` (1e-9).
- ``store_features`` monats-chunked Resample (Pi-RAM-Budget):
  ``hzrkl_rule``-Seed-Carry-over über MEHRERE Monatsgrenzen (inkl.
  roh-leerer Zwischenmonat = Seed bleibt unverändert), Gap-Monate
  (fehlende Monatsdatei), RAS-Maske (vor/nach ``RAS_INSTALL_TS``),
  ``first_hour`` vor Roh-Start (Gap-Stunden), fehlende Rohdaten → ValueError;
- Inkrementell columnar (``assemble_features_incremental_columnar``) ≡
  Full-Build (geschlossene Stunden), ``out_dir=None`` (Kontext inklusive) und
  ``out_dir`` (nur aktueller Monat, Vormonat anfassfrei);
- ``columnar_month_reference`` (``--verify``-Referenz: Kontext außen);
- Helper: ``_lag_shift`` (vordere k = NULL, k ≥ n), ``_rolling_sum_nullskip``
  (n < w), ``_rolling_max_nullskip`` (Sentinel), ``_runtime_since`` (49-Cap),
  ``_wx_equi_join`` (searchsorted-Fallback bei Mid-Span-Lücke),
  ``_last_non_null``.

Synthetische Quellen (kein Netzwerk, keine Produktions-Pfadschreibzugriffe;
Store-Files nur nach Temp-Dirs). Das monolithische Referenz-Orakel läuft — wie
in ``test_incremental`` — auf einer unbeschränkten ``duckdb.connect()``
(RAM-reicher Host-Pfad; ``config.con()`` kann den Vollspan per Design NICHT
einhalten — genau deshalb ist das Resample monats-chunked).

@slow: kompletter Rebuild (Temp-Dir) ≡ Produktiv-``data/features``
(read-only; nur im Abnahmelauf, nicht im Standard-Suite-Run).
"""

import argparse
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from analysis.model.m1 import build, config, features
from analysis.model.m1.calendar_utils import BERLIN
from analysis.model.m1.features import (
    assemble_features,
    assemble_features_incremental_columnar,
    columnar_month_reference,
    load_context,
    month_file_path,
    month_raw_max_ts,
    prev_month_str,
    store_features,
)
from analysis.model.m1.tests.test_incremental import (
    _discrete_mismatch,
    _float_rel_diff,
    _rows_for_hours,
    _write_month_rows,
    two_month_env,
)

REL_TOL = build.VERIFY_FLOAT_REL_TOL  # 1e-9 (Audit-Kriterium)
PROD_FEATURES = config.DATA_DIR / "features"


def _stage1_columns(store_names: list[str]) -> list[str]:
    """Stufe 1: Grid + Gap + hourly_agg-Durchgänge (bis inkl. mode_known)."""
    end = store_names.index("offset_known")
    return store_names[: end + 1]


def _stage_lag_columns(store_names: list[str]) -> list[str]:
    return [c for c in store_names if "_lag" in c]


def _stage_wx_columns(store_names: list[str]) -> list[str]:
    return [c for c in store_names if c.startswith("wx_")]


def _stage_runtime_columns(store_names: list[str]) -> list[str]:
    return [c for c in store_names if "_sum" in c or c.endswith("_since_h")]


def _compare_stage(ref: pa.Table, got: pa.Table, cols: list[str], label: str) -> None:
    """DoD-Vergleich einer Spaltengruppe: diskret bit-exakt, Float rel ≤ TOL.

    Float-Spalten: zusätzlich exakt identisches NULL-Pattern (Gap-Zeilen =
    NULL, nicht Float-NaN — pyarrow hält NaN im Float64-Array sonst als Wert;
    das ist eine Diskret-Abweichung, die ``_float_rel_diff`` (beidseitig
    non-null-Komparision) NICHT fängt).
    """
    for col in cols:
        if pa.types.is_floating(ref[col].type):
            a_null = np.isnan(np.array(got[col], dtype=np.float64))
            b_null = np.isnan(np.array(ref[col], dtype=np.float64))
            assert bool((a_null != b_null).sum() == 0), (
                f"{label} {col!r}: NULL-Pattern-Abweichung ({int((a_null != b_null).sum())} Zeilen)"
            )
            d = _float_rel_diff(got, ref, col)
            assert d <= REL_TOL, f"{label} {col!r}: Float-Abweichung {d:.3e}"
        else:
            mm = _discrete_mismatch(got, ref, col)
            assert mm == 0, f"{label} {col!r}: {mm} diskrete Abweichung(en)"


def _ns(**kw) -> argparse.Namespace:
    base = dict(
        fetch_end="2024-04-30",
        force=False,
        quiet=False,
        full=False,
        verify=False,
        no_progress=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


# --- synthetische Umgebungen ----------------------------------------------------


def dst_env(tmp_path: Path) -> config.BuildConfig:
    """winsol 2024-02/03/04 (voll), cmi leer; Spanne enthält DST 2024-03-31.

    3 volle Kalendermonate → 2 echte Monats-Grenzen mit Seed-Carry-over im
    monats-chunked Resample.
    """
    winsol = tmp_path / "winsol"
    cmi = tmp_path / "cmi"
    for m, days in (("02", 29), ("03", 31), ("04", 30)):
        _write_month_rows(
            winsol,
            f"2024-{m}",
            _rows_for_hours(datetime(2024, int(m), 1, tzinfo=BERLIN), 24 * days),
        )
    cmi.mkdir()
    cache = tmp_path / "wx"
    from analysis.model.m1.tests.test_integration import make_wx_cache

    make_wx_cache(cache, [], [(2024, 2), (2024, 3), (2024, 4)])
    return config.BuildConfig(
        features_dir=tmp_path / "features",
        wx_cache_dir=cache,
        winsol_pq=winsol,
        cmi_log_pq=cmi,
        bias_profile=tmp_path / "bias.json",  # nicht vorhanden → Profil None
        fetch_end="2024-04-30",
        first_hour=datetime(2024, 2, 1, 0, 0, tzinfo=BERLIN),
    )


def _oracle_full(cfg: config.BuildConfig, bias_profile) -> pa.Table:
    """Monolithischer Referenz-Build (unbeschränkt; SQL-Orakel)."""
    con = duckdb.connect()
    try:
        return assemble_features(con, cfg, bias_profile, "t", "now")
    finally:
        con.close()


def _columnar_full_store(cfg: config.BuildConfig, bias_profile) -> list[Path]:
    """Columnar Full-Build (Pi-Budget) → Store-Dateien (Temp-Dir)."""
    con = config.con()
    try:
        return store_features(con, cfg, bias_profile, "t", "now")
    finally:
        con.close()


def _concat_store(parts: list[Path]) -> pa.Table:
    return pa.concat_tables([pq.read_table(p) for p in parts])


# --- Kern-DoD: stufenweiser Abgleich columnar vs SQL-Orakel ---------------------


@pytest.fixture(scope="module")
def dst_ref(tmp_path_factory):
    """3-Monats-Referenz: Orakel (unbeschränkt) + columnar Store (500 MB)."""
    tmp_path = tmp_path_factory.mktemp("columnar_dst")
    cfg = dst_env(tmp_path)
    ref = _oracle_full(cfg, None)
    parts = _columnar_full_store(cfg, None)
    return cfg, ref, parts


class TestColumnarStages:
    def test_store_files_and_rows(self, dst_ref):
        cfg, ref, parts = dst_ref
        assert [p.name for p in parts] == ["2024-02.parquet", "2024-03.parquet", "2024-04.parquet"]
        got = _concat_store(parts)
        assert got.num_rows == ref.num_rows
        assert got["ts"].to_pylist() == ref["ts"].to_pylist()
        # Metadaten-Block je Datei
        for p in parts:
            md = pq.read_metadata(p).metadata or {}
            assert md[b"data_version"] == b"m1.3"
            assert md[b"git_tag"] == b"t"

    def test_stage1_grid_gap_agg_columns(self, dst_ref):
        """Grid + Gap-Flags + Source + alle hourly_agg-Durchgänge (positionell)."""
        _, ref, parts = dst_ref
        got = _concat_store(parts)
        _compare_stage(ref, got, _stage1_columns(ref.column_names), "Stufe 1")

    def test_stage2_lags(self, dst_ref):
        _, ref, parts = dst_ref
        got = _concat_store(parts)
        _compare_stage(ref, got, _stage_lag_columns(ref.column_names), "Stufe 2 (LAG)")

    def test_stage3_wx_and_bias(self, dst_ref):
        _, ref, parts = dst_ref
        got = _concat_store(parts)
        _compare_stage(ref, got, _stage_wx_columns(ref.column_names), "Stufe 3 (WX+Bias)")

    def test_stage4_runtime_windows(self, dst_ref):
        _, ref, parts = dst_ref
        got = _concat_store(parts)
        _compare_stage(ref, got, _stage_runtime_columns(ref.column_names), "Stufe 4 (Runtime)")

    def test_stage5_store_order_and_types(self, dst_ref):
        """Store-Ordnung exakt (692) + Typen identisch zum Orakel."""
        _, ref, parts = dst_ref
        got = _concat_store(parts)
        assert list(got.column_names) == list(ref.column_names)
        assert len(got.column_names) == 692
        assert got.schema.equals(ref.schema)

    def test_dst_hour_grid(self, dst_ref):
        """DST 2024-03-31: 24 Kalenderstunden, kein dupl. 02:xx, is_dst-Wechsel."""
        _, ref, parts = dst_ref
        got = _concat_store(parts)
        mar = got.filter(pc.equal(pc.strftime(got["ts"], "%Y-%m"), "2024-03"))
        rows31 = mar.filter(pc.equal(pc.strftime(mar["ts"], "%Y-%m-%d"), "2024-03-31"))
        assert rows31.num_rows == 24
        assert rows31["hour"].to_pylist() == list(range(24))
        assert got["is_dst"].to_pylist()[got["ts"].to_pylist().index(datetime(2024, 3, 31, 0))] == 0
        assert got["is_dst"].to_pylist()[got["ts"].to_pylist().index(datetime(2024, 4, 1, 0))] == 1


# --- Inkrementell columnar ≡ Full (DoD) ------------------------------------------


class TestIncrementalColumnar:
    def test_inc_columnar_matches_full(self, tmp_path):
        """``assemble_features_incremental_columnar`` (out_dir=None, Kontext
        inklusive) ≡ Full-Build auf allen Feb-Stunden (geschlossene Stunden)."""
        cfg = two_month_env(tmp_path)
        ref = _oracle_full(cfg, None)
        full_feb = ref.filter(pc.equal(pc.strftime(ref["ts"], "%Y-%m"), "2024-02"))
        # Vormonat-Store muss existieren (load_context liest die Jan-Datei)
        _columnar_full_store(cfg, None)

        ctx = load_context(
            prev_month_str("2024-02"), month_file_path(cfg.features_dir, prev_month_str("2024-02"))
        )
        assert ctx is not None
        raw_max = max(
            t
            for t in (
                month_raw_max_ts(cfg.winsol_pq, "2024-02"),
                month_raw_max_ts(cfg.cmi_log_pq, "2024-02"),
            )
            if t is not None
        )
        con = config.con()
        try:
            inc = assemble_features_incremental_columnar(
                con, cfg, None, "t", "now", ctx, "2024-02", raw_max, out_dir=None
            )
        finally:
            con.close()
        assert inc is not None
        # Kontext-Zeilen (Jan) liegen im Output (out_dir=None, Grid-Start = Kontext)
        assert inc["ts"].to_pylist()[0] == datetime(2024, 1, 30, 0)
        ts = inc["ts"]
        feb = inc.filter(pc.greater_equal(ts, pa.scalar(datetime(2024, 2, 1), type=ts.type)))
        assert feb.num_rows == full_feb.num_rows
        _compare_stage(full_feb, feb, full_feb.column_names, "inkrementell vs full")

    def test_inc_columnar_writes_only_cur_month(self, tmp_path):
        """out_dir = features_dir: nur die Feb-Datei wird (neu) geschrieben;
        die Jan-Datei (aus dem vorangegangenen Full-Build) bleibt anfassfrei
        (Mtime-Check) und inhaltlich unverändert (744 Zeilen, 23:00-Ende)."""
        cfg = two_month_env(tmp_path)
        _columnar_full_store(cfg, None)
        jan_p = month_file_path(cfg.features_dir, "2024-01")
        feb_p = month_file_path(cfg.features_dir, "2024-02")
        jan_mtime = jan_p.stat().st_mtime_ns
        jan_rows = pq.ParquetFile(jan_p).metadata.num_rows

        ctx = load_context(
            prev_month_str("2024-02"), month_file_path(cfg.features_dir, prev_month_str("2024-02"))
        )
        assert ctx is not None
        raw_max = month_raw_max_ts(cfg.cmi_log_pq, "2024-02")
        con = config.con()
        try:
            written = assemble_features_incremental_columnar(
                con, cfg, None, "t", "now", ctx, "2024-02", raw_max, out_dir=cfg.features_dir
            )
        finally:
            con.close()
        assert written == [feb_p]
        assert jan_p.stat().st_mtime_ns == jan_mtime  # anfassfrei
        assert pq.ParquetFile(jan_p).metadata.num_rows == jan_rows

    def test_columnar_month_reference_no_context_rows(self, tmp_path):
        """``--verify``-Referenz: Kontext-Zeilen außen (nur aktueller Monat)."""
        cfg = two_month_env(tmp_path)
        _columnar_full_store(cfg, None)
        ctx = load_context(
            prev_month_str("2024-02"), month_file_path(cfg.features_dir, prev_month_str("2024-02"))
        )
        assert ctx is not None
        raw_max = month_raw_max_ts(cfg.cmi_log_pq, "2024-02")
        con = config.con()
        try:
            ref_tbl = columnar_month_reference(con, cfg, None, "t", "now", "2024-02", raw_max, ctx)
        finally:
            con.close()
        assert ref_tbl["ts"].to_pylist()[0] == datetime(2024, 2, 1, 0)
        assert ref_tbl["ts"].to_pylist()[-1] == datetime(2024, 2, 1, 14, 0)

    def test_context_schema_divergence_rejected(self, tmp_path):
        """Kontext mit anderem Schema als hourly_agg → ValueError (vor
        Daten-Zugriff, s. Assemble-Doku)."""
        cfg = two_month_env(tmp_path)
        _columnar_full_store(cfg, None)
        from analysis.model.m1.features import ContextBundle

        bad_table = pa.table({"ts_bucket": pa.array(["2024-01-30 00:00"] * 48, type=pa.string())})
        ctx = ContextBundle(table=bad_table, seeds={}, month="2024-01")
        con = duckdb.connect()
        try:
            with pytest.raises(ValueError, match="divergieren"):
                assemble_features_incremental_columnar(
                    con,
                    cfg,
                    None,
                    "t",
                    "now",
                    ctx,
                    "2024-02",
                    datetime(2024, 2, 1, 14, 0),
                    out_dir=None,
                )
        finally:
            con.close()


# --- store_features: monats-chunked Resample -------------------------------------


class TestStoreFeaturesMonthLoop:
    def test_seed_carryover_across_months_and_empty_month(self, tmp_path):
        """Kell-Event in 2024-02 (dev1_a1 = 19,0 → rule 1,0), 2024-03 KEINE
        Rohdaten (fehlende Monatsdatei), 2024-04 ohne Event (NULL) → der
        Regelzustand wird über die Monatsgrenze UND den roh-leeren Monat
        gehalten (Seed), identisch zum monolithischen Forward-Fill."""
        cfg = dst_env(tmp_path)
        winsol = cfg.winsol_pq
        # Feb mit Event, März gelöscht (roh-leer), April ohne Event (NULL).
        _write_month_rows(
            winsol,
            "2024-02",
            _rows_for_hours(datetime(2024, 2, 1, tzinfo=BERLIN), 24 * 29, dev1_analog_01=19.0),
        )
        (winsol / "2024" / "03" / "2024-03.parquet").unlink()
        _write_month_rows(
            winsol,
            "2024-04",
            _rows_for_hours(
                datetime(2024, 4, 1, tzinfo=BERLIN),
                24 * 30,
                dev1_analog_01=pa.array([None] * (24 * 30 * 360), type=pa.float64()),
            ),
        )
        ref = _oracle_full(cfg, None)
        parts = _columnar_full_store(cfg, None)
        got = _concat_store(parts)
        _compare_stage(ref, got, ref.column_names, "seed-carry-over (leerer Monat)")
        apr = got.filter(pc.equal(pc.strftime(got["ts"], "%Y-%m"), "2024-04"))
        rule = apr["hzrkl_kell_rule"].to_pylist()
        assert rule and all(v == 1.0 for v in rule), (
            f"kell-rule muss 1,0 über Monatsgrenzen + leeren Monat halten, erste: {rule[:3]}"
        )
        mar = got.filter(pc.equal(pc.strftime(got["ts"], "%Y-%m"), "2024-03"))
        assert mar["gap_full"].to_pylist() == [1] * mar.num_rows  # Gap-Monat
        assert mar["source"].to_pylist() == ["none"] * mar.num_rows

    def test_first_hour_before_raw_start(self, tmp_path):
        """first_hour vor Roh-Start: Grid-Monate ohne Rohdatei → Gap-Stunden
        (n_rows=0, source=none), monolithisch identisch."""
        import dataclasses

        cfg = two_month_env(tmp_path)  # winsol 2024-01 voll + cmi 2024-02 (15 h)
        first = datetime(2023, 12, 15, 0, 0, tzinfo=BERLIN)
        cfg_f = dataclasses.replace(cfg, first_hour=first)
        ref = _oracle_full(cfg_f, None)
        parts = _columnar_full_store(cfg_f, None)
        got = _concat_store(parts)
        _compare_stage(ref, got, ref.column_names, "first_hour vor Roh-Start")
        dec = got.filter(pc.equal(pc.strftime(got["ts"], "%Y-%m-%d"), "2023-12-15"))
        assert dec.num_rows == 24
        assert dec["gap_full"].to_pylist() == [1] * 24
        assert dec["source"].to_pylist() == ["none"] * 24

    def test_no_winsol_raises(self, tmp_path):
        """Leeres winsol_pq: DuckDB-``read_parquet`` (leerer Glob) schlägt fehl —
        die Abfrage des Quell-Endes ist die erste Quelle-Interaktion."""
        cfg = two_month_env(tmp_path)
        for f in cfg.winsol_pq.glob("*/*/*.parquet"):
            f.unlink()
        con = config.con()
        try:
            with pytest.raises((duckdb.IOException, ValueError)):
                store_features(con, cfg, None, "t", "now")
        finally:
            con.close()

    def test_log_lines(self, tmp_path):
        cfg = two_month_env(tmp_path)
        lines: list[str] = []
        con = config.con()
        try:
            store_features(con, cfg, None, "t", "now", log=lines.append)
        finally:
            con.close()
        assert any("Resample" in ln for ln in lines)
        assert any("Assemble" in ln for ln in lines)


# --- RAS-Maske (vor/nach RAS_INSTALL_TS) über den Full-Pfad ----------------------


class TestStoreFeaturesRasMask:
    def test_ras_mask_before_install(self, tmp_path):
        """12 h ab 2021-10-20 12:00 (5 h vor RAS-Einbau 17:00, 7 h danach):
        RAS-abgeleitete Spalten vor 17:00 NULL + offset_known=0; ab 17:00 Werte.
        (Deckt das monats-chunked Resample über ``store_features``.)"""
        from analysis.model.m1.tests.test_integration import make_source_rows, make_wx_cache

        start = datetime(2021, 10, 20, 12, 0, tzinfo=BERLIN)
        rows = pa.concat_tables(
            [make_source_rows(start + timedelta(hours=i), 360, base=20.0 + i) for i in range(12)]
        )
        p = tmp_path / "winsol" / "2021" / "10" / "2021-10.parquet"
        p.parent.mkdir(parents=True)
        pq.write_table(rows, p)
        cache = tmp_path / "wx"
        make_wx_cache(cache, [(2021, 10)], [])
        cfg = config.BuildConfig(
            features_dir=tmp_path / "features",
            wx_cache_dir=cache,
            winsol_pq=tmp_path / "winsol",
            cmi_log_pq=tmp_path / "cmi",
            fetch_end="2021-10-31",
            first_hour=start,
        )
        con = config.con()
        try:
            parts = store_features(con, cfg, None, "git", "ts")
        finally:
            con.close()
        assert len(parts) == 1
        d = pq.read_table(parts[0]).to_pydict()
        install = datetime.fromisoformat(config.RAS_INSTALL_TS)
        i17 = d["ts"].index(install)
        for i in range(i17):
            assert d["ras_kell_ist"][i] is None and d["offset_known"][i] == 0
        for i in range(i17, 12):
            assert d["offset_known"][i] == 1
            assert d["ras_kell_ist"][i] == 20.0 + i


# --- Helper-Einheiten -------------------------------------------------------------


class TestHelpers:
    def test_lag_shift(self):
        col = pa.array([1.0, 2.0, 3.0, 4.0])
        assert features._lag_shift(col, 1).to_pylist() == [None, 1.0, 2.0, 3.0]
        assert features._lag_shift(col, 4).to_pylist() == [None] * 4  # k = n
        assert features._lag_shift(col, 5).to_pylist() == [None] * 4  # k > n
        nul = pa.nulls(3, type=pa.float64())
        assert features._lag_shift(nul, 2).to_pylist() == [None] * 3

    def test_rolling_sum_below_window(self):
        col = pa.array([None, 2.0, None, 5.0], type=pa.float64())
        got = features._rolling_sum_nullskip(col, 12).to_pylist()
        assert got == [None, 2.0, 2.0, 7.0]  # n < w: generische Schleife

    def test_rolling_max_sentinel(self):
        vals = pa.array([10, 20, 30], type=pa.int64())
        keep = pa.array([False, True, False], type=pa.bool_())
        got = features._rolling_max_nullskip(vals, keep, 48).to_pylist()
        assert got == [-2, 20, 20]  # −2 = kein Treffer (Sentinel, s. _runtime_since)

    def test_runtime_since_cap_49(self):
        from decimal import Decimal

        row_idx = pa.array(range(50), type=pa.int64())
        on = pa.array([None] * 49 + [1.0], type=pa.float64())
        got = features._runtime_since(row_idx, on).to_pylist()
        assert got[48] == 49.0  # kein Treffer im 49-Window → 49.0
        assert got[49] == 0.0
        # Typ: DECIMAL(20,1) wie im Store (to_pylist → Decimal)
        assert got[48] == Decimal("49.0")

    def test_wx_equi_join_dense_and_sparse(self):
        """Dicht: Index-Arithmetik (Ziel außerhalb → NULL); lückig:
        searchsorted-Fallback (Mid-Span-Lücke → NULL, kein LEAD-Nachbarschaft)."""
        import numpy as np

        base = np.datetime64("2024-01-01T00:00")
        hours = base + np.arange(48) * np.timedelta64(1, "h")
        var_cols = {
            var: pa.array(np.arange(48, dtype=np.float64) * 0.1 + i, type=pa.float64())
            for i, var in enumerate(config.WX_HOURLY_VARS)
        }
        var_cols[config.WX_HOURLY_VARS[0]] = pa.array(
            np.arange(48, dtype=np.float64) * 0.5, type=pa.float64()
        )
        hour_utc_arr = pa.array(hours.astype("datetime64[us]"), type=pa.timestamp("us"))
        # dicht: alle 48 Stunden
        wx_dense = pa.table({"hour_utc": hour_utc_arr, **var_cols})
        dense = features._wx_equi_join(wx_dense, hour_utc_arr, 24)
        assert dense[(0, "t")].to_pylist() == [i * 0.5 for i in range(48)]
        assert dense[(23, "t")].to_pylist()[-1] is None  # Ziel 5.1. fehlt → NULL
        # lückig: Stunde 10 und 30 fehlen → searchsorted-Zweig
        keep = np.ones(48, dtype=bool)
        keep[10] = False
        keep[30] = False
        mask = pa.array(keep, type=pa.bool_())
        wx_sparse = pa.table(
            {
                "hour_utc": hour_utc_arr.filter(mask),
                **{short: arr.filter(mask) for short, arr in var_cols.items()},
            }
        )
        sparse = features._wx_equi_join(wx_sparse, hour_utc_arr, 24)
        got = sparse[(0, "t")].to_pylist()
        exp = [i * 0.5 if k else None for i, k in enumerate(keep)]
        assert got == exp, "Mid-Span-Lücke: exakt NULL (kein LEAD-Nachbarschaftswert)"

    def test_last_non_null(self):
        assert features._last_non_null(pa.array([1.0, None, 2.0])) == 2.0
        assert features._last_non_null(pa.array([None, None])) is None
        assert features._last_non_null(pa.array([], type=pa.float64())) is None


# --- (optional) @slow: kompletter Rebuild ≡ Produktiv-Store (read-only) -----------


def _prod_available() -> bool:
    if not PROD_FEATURES.exists() or not (config.DATA_DIR / "winsol_pq").exists():
        return False
    return any(PROD_FEATURES.glob("*/*/*.parquet")) and any(
        (config.DATA_DIR / "winsol_pq").glob("*/*/*.parquet")
    )


def _diff_closed_hours(store: pa.Table, ref: pa.Table) -> list[str]:
    """Zellen-/Zeilen-Diff auf geschlossenen Stunden (2-h-Offenes-Fenster).

    200-Spalten-Blöcke (R4'-RAM: parallele Partial-Agg-Status, s.
    ``build._diff_month_tables``); diskret bit-exakt, Float rel ≤ REL_TOL.
    """
    problems: list[str] = []
    con = duckdb.connect(config={"memory_limit": "500MB", "threads": "4"})
    try:
        cutoff = min(pc.max(store["ts"]).as_py(), pc.max(ref["ts"]).as_py()) - timedelta(hours=2)
        con.register("v_store", store)
        con.register("v_ref", ref)
        row = con.execute(
            f"""SELECT count(*) FILTER (WHERE r.ts IS NULL) AS only_store,
                       count(*) FILTER (WHERE s.ts IS NULL) AS only_ref
                FROM v_store s FULL OUTER JOIN v_ref r ON s.ts = r.ts
                WHERE (s.ts IS NOT NULL AND s.ts <= TIMESTAMP '{cutoff.isoformat()}')
                   OR (r.ts IS NOT NULL AND r.ts <= TIMESTAMP '{cutoff.isoformat()}')"""
        ).fetchone()
        if row[0] or row[1]:
            problems.append(f"ts-Abdeckung: only-store={row[0]} only-ref={row[1]}")
        cols = [c for c in store.column_names if c not in ("git_tag", "generated_at")]
        cut = f"s.ts <= TIMESTAMP '{cutoff.isoformat()}'"
        for lo in range(0, len(cols), 200):
            block = cols[lo : lo + 200]
            exprs = ", ".join(
                (
                    f"count(*) FILTER (WHERE NOT (s.{c} IS NOT DISTINCT FROM r.{c}) AND {cut}) AS n"
                    if not pa.types.is_floating(ref[c].type)
                    else f"count(*) FILTER (WHERE (((s.{c} IS NULL) <> (r.{c} IS NULL)) "
                    f"OR (s.{c} IS NOT NULL AND r.{c} IS NOT NULL "
                    f"AND abs(s.{c} - r.{c}) > {REL_TOL} * greatest(1.0, abs(r.{c})))) "
                    f"AND {cut}) AS n"
                )
                for c in block
            )
            r2 = con.execute(
                f"SELECT {exprs} FROM v_store s JOIN v_ref r ON s.ts = r.ts"
            ).fetchone()
            for c, n in zip(block, r2, strict=True):
                if int(n):
                    problems.append(f"{c}: {n} Zeile(n) weichen ab (geschl. Stunden)")
    finally:
        con.close()
    return problems


@pytest.mark.slow
@pytest.mark.skipif(
    not _prod_available(), reason="Produktiv-Daten (data/features + data/winsol_pq) fehlen"
)
class TestFullRebuildVsOracle:
    """@slow: kompletter columnar Rebuild (Temp-Dir) ≡ frisch gebautes SQL-Orakel.

    Harte Identitäts-Gate auf dem VOLLSPANN (alle 75 Monate): die columnar
    ``store_features`` (Pi-Budget, monats-chunked) muss das monolithische
    ``assemble_features``-Orakel (kanonische ``features_final``-Query,
    unbeschränkte Verbindung) auf geschlossenen Stunden reproduzieren
    (diskret bit-exakt, Float rel ≤ REL_TOL).

    Hinweis: Der Produktiv-``data/features``-Store ist NICHT die Referenz —
    er kann veraltet sein (gemessen: 2022-06 ``hzrkl_eg_rule`` weicht vom
    Orakel ab, nicht durch aktuelle Code-Logik reproduzierbar → Store-Drift,
    ``--full``-Rebuild nötig). Die Produktiv-Vergleiche bleiben informativ
    (``test_rebuild_prod_drift_report``, kein Assert), das Orakel ist kanonisch.

    NICHT im Standard-Suite-Run (``-m "not slow"``); nur im Abnahmelauf
    (Referenz-Host ~minuten).
    """

    @pytest.fixture(scope="class")
    def rebuild_and_oracle(self, tmp_path_factory):
        root = tmp_path_factory.mktemp("columnar_full_rebuild")
        cfg = config.BuildConfig(features_dir=root / "features")  # Produktion-Quellen
        from analysis.model.m1 import bias

        profile = bias.load_profile(cfg.bias_profile) if cfg.bias_profile.exists() else None
        con = config.con()
        try:
            parts = store_features(con, cfg, profile, "columnar-test", "2026-11-03T00:00:00")
        finally:
            con.close()
        # Kanonisches Orakel (monolithisch, unbeschränkt; RAM-reicher Host-Pfad).
        con = duckdb.connect()
        try:
            oracle = assemble_features(con, cfg, profile, "columnar-test", "2026-11-03T00:00:00")
        finally:
            con.close()
        return cfg, parts, oracle

    def test_rebuild_matches_oracle_all_months(self, rebuild_and_oracle):
        cfg, parts, oracle = rebuild_and_oracle
        # Monat-Abdeckung: columnar-Store deckt exakt die Orakel-Monate ab.
        oracle_ym = set(pc.strftime(oracle["ts"], "%Y-%m").to_pylist())
        ref_ym = {p.name[:7] for p in parts}
        assert oracle_ym == ref_ym, (
            f"Monat-Abdeckung: only-oracle={sorted(oracle_ym - ref_ym)[:5]} "
            f"only-ref={sorted(ref_ym - oracle_ym)[:5]}"
        )
        for rel in sorted(oracle_ym):
            store = pq.read_table(cfg.features_dir / rel[:4] / rel[5:7] / f"{rel}.parquet")
            mask = pc.equal(pc.strftime(oracle["ts"], "%Y-%m"), rel)
            ref = oracle.filter(mask)
            problems = _diff_closed_hours(store, ref)
            assert not problems, f"{rel} (vs Orakel): {problems[:5]}"

    def test_rebuild_prod_drift_report(self, rebuild_and_oracle):
        """Informativ: columnar-Store vs Produktiv-Store (vorherige Drifts).

        Kein Assert (Produktiv-Store darf veraltet sein); dokumentiert welche
        Monate vom Orakel-identischen Rebuild abweichen (Store-Drift →
        ``--full``-Rebuild nötig).
        """
        cfg, parts, _ = rebuild_and_oracle
        ref_ym = {p.name[:7] for p in parts}
        prod_ym = (
            {p.name[:7] for p in PROD_FEATURES.glob("*/*/*.parquet")}
            if PROD_FEATURES.exists()
            else set()
        )
        drift = []
        for rel in sorted(ref_ym & prod_ym):
            store = pq.read_table(cfg.features_dir / rel[:4] / rel[5:7] / f"{rel}.parquet")
            prod = pq.read_table(PROD_FEATURES / rel[:4] / rel[5:7] / f"{rel}.parquet")
            problems = _diff_closed_hours(prod, store)
            if problems:
                drift.append((rel, problems[:3]))
        if drift:
            print(
                f"[columnar@slow] Produktiv-Store-Drift in {len(drift)} Monat(en): "
                + "; ".join(m for m, _ in drift[:8])
            )
        # Keine Assertion — Drift ist ein Befund, kein Fehler des columnaren Pfads.


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
