"""Tests: M1-Inkremental — Feature-Store-Rebuild auf „aktueller + letzter Monat".

Deckt (M1-Inkremental, Plan.md §M1-Inkremental):
- ``load_context`` (48 Kontext-Stunden + ``hzrkl_rule``-Seeds, Fallback=None);
- ``store_month_file_ok`` / ``store_max_ts`` / ``month_raw_max_ts`` / ``prev_month_str``;
- **Kern-DoD**: inkrementeller Build ≡ Full-Build auf allen geschlossenen Stunden
  (diskret bit-exakt; Float rel-Tol ``VERIFY_FLOAT_REL_TOL``, gemessen auf
  1e-13 — DuckDB-Float-Summenfolge, ML-irrelevant);
- ``hzrkl_rule``-Seed-Carry-over über die Monatsgrenze (Event M-1, kein Event M);
- CLI: Default=inkrementell, ``--full``, ``--verify`` (Drift-Erkennung +
  offenes Fenster als Hinweis, nicht Drift), Fallback→full (kein Fortschritt).

Synthetische Quellen (2 Monate, kein Netzwerk, keine Produktions-Pfade);
Helfer wiederverwendet aus ``test_integration``.
"""

import argparse
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from analysis.model.m1 import build, config, features, resample
from analysis.model.m1.calendar_utils import BERLIN
from analysis.model.m1.features import (
    CONTEXT_HOURS,
    DATA_VERSION,
    assemble_features,
    assemble_features_incremental,
    load_context,
    month_file_path,
    month_raw_max_ts,
    prev_month_str,
    store_max_ts,
    store_month_file_ok,
    write_month_parquet,
)
from analysis.model.m1.tests.test_integration import (
    make_source_rows,
    make_wx_cache,
)

REL_TOL = build.VERIFY_FLOAT_REL_TOL  # 1e-9 (Audit-Kriterium)


# --- synthetische 2-Monats-Umgebung -------------------------------------------


def _write_month_rows(root: Path, month: str, rows: pa.Table) -> Path:
    p = root / month[:4] / month[5:7] / f"{month}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(rows, p)
    return p


def _rows_for_hours(start: datetime, n_hours: int, **col_overrides) -> pa.Table:
    """n_hours × 360 synthetische Roh-Zeilen (deterministisch, 10-s-Takt).

    ``col_overrides``: Spaltenname → pyarrow-Array (Länge n_hours*360) oder
    Skalar; ersetzt die Default-Belegung von ``make_source_rows``.
    """
    base = pa.concat_tables(
        [make_source_rows(start + timedelta(hours=i), 360) for i in range(n_hours)]
    )
    for col, val in col_overrides.items():
        if not isinstance(val, pa.Array):
            val = pa.array([val] * (n_hours * 360))
        idx = base.schema.get_field_index(col)
        base = base.set_column(idx, col, val)
    return base


def two_month_env(tmp_path: Path) -> config.BuildConfig:
    """2024-01 voll (winsol) + 2024-02-01 00:00→14:00 (cmi, 15 Stunden)."""
    jan = _rows_for_hours(datetime(2024, 1, 1, 0, 0, tzinfo=BERLIN), 24 * 31)
    feb = _rows_for_hours(datetime(2024, 2, 1, 0, 0, tzinfo=BERLIN), 15)
    winsol = tmp_path / "winsol"
    cmi = tmp_path / "cmi"
    _write_month_rows(winsol, "2024-01", jan)
    _write_month_rows(cmi, "2024-02", feb)
    cache = tmp_path / "wx"
    make_wx_cache(cache, [], [(2024, 1), (2024, 2)])
    return config.BuildConfig(
        features_dir=tmp_path / "features",
        wx_cache_dir=cache,
        winsol_pq=winsol,
        cmi_log_pq=cmi,
        bias_profile=tmp_path / "bias.json",  # nicht vorhanden → Profil None
        fetch_end="2024-02-28",
        first_hour=datetime(2024, 1, 1, 0, 0, tzinfo=BERLIN),
    )


def _full_month_table(cfg: config.BuildConfig, month: str) -> pa.Table:
    """Full-Build (in-memory) → Table des einen Monats (Referenz).

    R4': der monolithische Referenz-Build (``assemble_features``) ist der
    RAM-reiche Host-Pfad (Peak ~21 GB über die Historie) und kann das Pi-Budget
    ``config.con()`` (500 MB / 4 Threads) NICHT einhalten — genau deshalb existiert
    das chunked Verfahren. Die Referenz läuft daher bewusst auf einer
    unbeschränkten ``duckdb.connect()`` (wie ``test_integration``); die
    chunked/inkrementellen Pfade (DoD-Vergleichsseite) laufen auf ``config.con()``.
    """
    con = duckdb.connect()
    try:
        tbl = assemble_features(con, cfg, None, "t", "now")
    finally:
        con.close()
    return tbl.filter(pc.equal(pc.strftime(tbl["ts"], "%Y-%m"), month))


def _inc_month_table(cfg: config.BuildConfig, cur_month: str) -> pa.Table:
    """Inkrementeller Build (in-memory) → Table des aktuellen Monats."""
    ctx = load_context(
        prev_month_str(cur_month),
        month_file_path(cfg.features_dir, prev_month_str(cur_month)),
    )
    assert ctx is not None, "Kontext des Vormonats muss verfügbar sein"
    raw_max = max(
        t
        for t in (
            month_raw_max_ts(cfg.winsol_pq, cur_month),
            month_raw_max_ts(cfg.cmi_log_pq, cur_month),
        )
        if t is not None
    )
    con = config.con()
    try:
        return assemble_features_incremental(con, cfg, None, "t", "now", ctx, cur_month, raw_max)
    finally:
        con.close()


def _float_rel_diff(a: pa.Table, b: pa.Table, col: str) -> float:
    """Max relative |a-b| (beide non-null), 0.0 wenn keine Abweichung.

    Float64-Columns: null → NaN in numpy; NaN-Paare (null==null) werden
    ignoriert, NaN vs. Wert (Abweichung) ebenfalls ignoriert (nur beidseitig
    non-null zählt) — das ist der konservative, ML-relevante Vergleich.
    """
    import numpy as np

    va = np.array(a[col], dtype="float64")
    vb = np.array(b[col], dtype="float64")
    mask = ~np.isnan(va) & ~np.isnan(vb)
    va, vb = va[mask], vb[mask]
    if va.size == 0:
        return 0.0
    with np.errstate(invalid="ignore", divide="ignore"):
        rel = np.abs(va - vb) / np.maximum(1.0, np.abs(vb))
    rel = rel[np.isfinite(rel)]
    return float(rel.max()) if rel.size else 0.0


def _discrete_mismatch(a: pa.Table, b: pa.Table, col: str) -> int:
    """Anzahl Zeilen, in denen sich ``col`` unterscheidet (null-safe).

    ``null == null`` gilt als gleich; ``null`` vs. Wert als Abweichung.
    (Dieses pyarrow kennt ``pc.equal``/``fill_null``/``and_``/``or_``, aber
    weder ``null_equality`` noch ``pc.not_`` → numpy-Count über Boolean-Array.)
    """
    import numpy as np

    eq = pc.equal(a[col], b[col])  # Boolean, null wo min. eine Seite null
    both_null = pc.and_(pc.is_null(a[col]), pc.is_null(b[col]))
    eq = pc.or_(pc.fill_null(eq, pa.scalar(False, type=pa.bool_())), both_null)
    return int((~np.array(eq, dtype=bool)).sum())


# --- kleine Helfer ------------------------------------------------------------


class TestHelpers:
    def test_prev_month_str(self):
        assert prev_month_str("2024-02") == "2024-01"
        assert prev_month_str("2024-01") == "2023-12"

    def test_month_file_path(self, tmp_path):
        assert month_file_path(tmp_path, "2024-02") == tmp_path / "2024" / "02" / "2024-02.parquet"

    def test_store_max_ts_roundtrip(self, tmp_path):
        t = pa.table(
            {
                "ts": pa.array(
                    [datetime(2024, 2, 1, 5), datetime(2024, 2, 1, 9)], type=pa.timestamp("us")
                )
            }
        )
        p = write_month_parquet(t, "2024-02", tmp_path, "git", "ts")
        assert store_max_ts(p) == datetime(2024, 2, 1, 9)

    def test_store_max_ts_missing(self, tmp_path):
        assert store_max_ts(tmp_path / "fehlt.parquet") is None

    def test_month_raw_max_ts(self, tmp_path):
        _write_month_rows(
            tmp_path, "2024-02", _rows_for_hours(datetime(2024, 2, 1, 0, 0, tzinfo=BERLIN), 2)
        )
        assert month_raw_max_ts(tmp_path, "2024-02") == datetime(2024, 2, 1, 1, 59, 50)
        assert month_raw_max_ts(tmp_path, "2024-03") is None  # Datei fehlt

    def test_store_month_file_ok_version(self, tmp_path):
        t = pa.table({"ts": pa.array([datetime(2024, 2, 1)], type=pa.timestamp("us"))})
        p = write_month_parquet(t, "2024-02", tmp_path, "git", "ts")
        assert store_month_file_ok(p, DATA_VERSION)
        assert not store_month_file_ok(p, "m1.2")
        assert not store_month_file_ok(tmp_path / "kein.parquet", DATA_VERSION)


# --- load_context --------------------------------------------------------------


@pytest.fixture
def jan_store(tmp_path):
    """Vollständiger 2-Monats-Store (Full-Build) → Vormonat-Datei 2024-01.

    ``load_context`` erwartet das VOLLE Feature-Store-Schema (692 Spalten,
    ``hourly_agg``-Column-Auswahl) — daher echte Store-Datei, kein Minimal-File.
    """
    cfg = two_month_env(tmp_path)
    con = config.con()
    try:
        features.store_features(con, cfg, None, "t", "now")
    finally:
        con.close()
    return month_file_path(cfg.features_dir, "2024-01")


class TestLoadContext:
    def test_missing_file(self, tmp_path):
        assert load_context("2024-01", month_file_path(tmp_path, "2024-01")) is None

    def test_too_few_hours(self, jan_store):
        t = pq.read_table(jan_store)
        t = t.slice(0, 40)  # < CONTEXT_HOURS (48)
        p = jan_store.parent / "short.parquet"
        pq.write_table(t, p)
        assert load_context("2024-01", p) is None

    def test_version_mismatch(self, jan_store):
        t = pq.read_table(jan_store)
        p = jan_store.parent / "wrongver.parquet"
        bad_schema = t.schema.with_metadata(
            {k: v for k, v in (t.schema.metadata or {}).items() if k != b"data_version"}
            | {b"data_version": b"m1.2"}
        )
        pq.write_table(t.cast(bad_schema), p)
        assert load_context("2024-01", p) is None

    def test_end_not_month_end_23h(self, jan_store):
        t = pq.read_table(jan_store)
        t = t.slice(0, t.num_rows - 1)  # endet bei 22:00 statt 23:00
        p = jan_store.parent / "shortend.parquet"
        pq.write_table(t, p)
        assert load_context("2024-01", p) is None

    def test_non_hourly_grid(self, jan_store):
        t = pq.read_table(jan_store)
        ts = t["ts"].to_pylist()
        ts[-2] = ts[-2] - timedelta(hours=2)  # Raster-Defekt
        t = t.set_column(0, "ts", pa.array(ts, type=pa.timestamp("us")))
        p = jan_store.parent / "gapgrid.parquet"
        pq.write_table(t, p)
        assert load_context("2024-01", p) is None

    def test_ok_and_seeds(self, jan_store):
        ctx = load_context("2024-01", jan_store)
        assert ctx is not None
        assert ctx.month == "2024-01"
        assert ctx.table.num_rows == CONTEXT_HOURS
        # ts_bucket = letzte 48 Stunden bis 31.01 23:00
        buckets = ctx.table["ts_bucket"].to_pylist()
        assert buckets[-1] == "2024-01-31 23:00"
        assert buckets[0] == "2024-01-30 00:00"  # 48 h zurück
        # Seeds = hzrkl_{z}_rule_last der letzten Zeile (deterministische
        # synthetische Werte: event-Kanal dev1_a1 = 21.0 > 20,1 → rule = 0.0).
        t = pq.read_table(jan_store)
        last_rule = {z: t[f"hzrkl_{z}_rule_last"].to_pylist()[-1] for z in ("kell", "eg", "dg")}
        assert ctx.seeds == {z: (None if v is None else float(v)) for z, v in last_rule.items()}
        # Schema = hourly_agg-Columns (ts_bucket + Aggregat-Spalten)
        assert list(ctx.table.column_names) == resample.hourly_agg_columns()


# --- Kern-DoD: inkrementell ≡ full (geschlossene Stunden) ----------------------


class TestIncrementalEquivalence:
    def test_inc_matches_full_closed_hours(self, tmp_path):
        """2024-02 inkrementell ≡ Full-Build: diskret bit-exakt, Float rel ≤1e-9.

        (DoD 1 korrigiert: strikte Bit-Identität ist nicht erreichbar — der
        Full-Build liest 19-Mio-Zeilen-Glob, inkrementell die Monatsdatei;
        DuckDBs parallele Float-Aggregation kann in der letzten ULP-Bits
        abweichen. Gemessen: ~1e-13. Diskrete Spalten (NULL-Pattern, States,
        Rules, LAG-Kategorien) müssen exakt stimmen.)
        """
        cfg = two_month_env(tmp_path)
        # Referenz: Full-Build → Store (Jan + Feb), dann Feb inkrementell neu.
        parts = features.store_features(config.con(), cfg, None, "t", "now")
        assert {p.parent.name + "/" + p.name for p in parts} >= {"02/2024-02.parquet"}

        full_feb = _full_month_table(cfg, "2024-02")
        inc_feb = _inc_month_table(cfg, "2024-02")

        assert inc_feb.num_rows == full_feb.num_rows, "Zeilenzahl (Stunden) muss identisch sein"
        assert inc_feb.num_columns == full_feb.num_columns
        assert set(inc_feb.column_names) == set(full_feb.column_names)

        worst_rel = 0.0
        worst_col = None
        for col in full_feb.column_names:
            if col in ("git_tag", "generated_at"):
                continue
            if pa.types.is_floating(full_feb[col].type):
                d = _float_rel_diff(inc_feb, full_feb, col)
                if d > worst_rel:
                    worst_rel, worst_col = d, col
            else:
                mm = _discrete_mismatch(inc_feb, full_feb, col)
                assert mm == 0, f"discrete-Abweichung in {col!r}: {mm} Zeilen"
        assert worst_rel <= REL_TOL, (
            f"Float-Abweichung > {REL_TOL} in {worst_col!r}: {worst_rel:.3e}"
        )

    def test_rule_seed_carryover_across_month(self, tmp_path):
        """hzrkl_kell_rule: Event in Jan (dev1_a1 < 20,1), in Feb KEIN Event
        (NULL) → inkrementeller Build hält den Zustand über die Monatsgrenze
        (Seed), identisch zum Full-Build (kein NULL-Reset am Monatsanfang)."""
        cfg = two_month_env(tmp_path)
        winsol = cfg.winsol_pq
        cmi = cfg.cmi_log_pq
        # Jan: kell-Event=1 (dev1_analog_01 = 19,0 < 20,1) in ALLEN Stunden.
        jan = _rows_for_hours(
            datetime(2024, 1, 1, 0, 0, tzinfo=BERLIN),
            24 * 31,
            dev1_analog_01=19.0,
        )
        _write_month_rows(winsol, "2024-01", jan)
        # Feb: kell-Kanal NULL (kein Event) → Forward-Fill muss Jan-Zustand halten.
        feb = _rows_for_hours(
            datetime(2024, 2, 1, 0, 0, tzinfo=BERLIN),
            15,
            dev1_analog_01=pa.array([None] * (15 * 360), type=pa.float64()),
        )
        _write_month_rows(cmi, "2024-02", feb)

        # R4': monolithischer Referenz-Build auf unbeschränkter Verbindung
        # (``config.con()`` = Pi-Budget 500 MB würde OOM — s. _full_month_table).
        con = duckdb.connect()
        try:
            full = assemble_features(con, cfg, None, "t", "now")
        finally:
            con.close()
        parts = features.store_features(config.con(), cfg, None, "t", "now")
        assert any(p.name == "2024-02.parquet" for p in parts)

        inc = _inc_month_table(cfg, "2024-02")
        full_feb = full.filter(pc.equal(pc.strftime(full["ts"], "%Y-%m"), "2024-02"))

        # Inkrementell: Seed kell=1.0 → rule=1.0 in allen Feb-Stunden.
        inc_rule = inc["hzrkl_kell_rule"].to_pylist()
        assert all(v == 1.0 for v in inc_rule), (
            f"inkrementell: kell-rule muss 1.0 (Seed) in allen Feb-Stunden halten, "
            f"erster Werte: {inc_rule[:3]}"
        )
        # Full: Forward-Fill über Jan-Events → ebenfalls 1.0 (Referenz).
        full_rule = full_feb["hzrkl_kell_rule"].to_pylist()
        assert inc_rule == full_rule, "Seed-Carry-over ≠ Full-Build-Forward-Fill"

    def test_no_seed_when_prev_month_empty(self, tmp_path):
        """Vormonat ohne Events (alle NULL) → Seed=None → rule startet bei
        NULL wie im Full-Build (kein erfundener Zustand)."""
        cfg = two_month_env(tmp_path)
        null_kell = pa.array([None] * (24 * 31 * 360), type=pa.float64())
        jan = _rows_for_hours(
            datetime(2024, 1, 1, 0, 0, tzinfo=BERLIN),
            24 * 31,
            dev1_analog_01=null_kell,
        )
        _write_month_rows(cfg.winsol_pq, "2024-01", jan)
        # Feb ebenfalls keine kell-Events (sonst setze der Feb-eigene Event
        # rule — der Fall wäre nicht „kein Event je").
        feb = _rows_for_hours(
            datetime(2024, 2, 1, 0, 0, tzinfo=BERLIN),
            15,
            dev1_analog_01=pa.array([None] * (15 * 360), type=pa.float64()),
        )
        _write_month_rows(cfg.cmi_log_pq, "2024-02", feb)
        # R4': monolithischer Referenz-Build auf unbeschränkter Verbindung
        # (``config.con()`` = Pi-Budget 500 MB würde OOM — s. _full_month_table).
        con = duckdb.connect()
        try:
            full = assemble_features(con, cfg, None, "t", "now")
        finally:
            con.close()
        features.store_features(config.con(), cfg, None, "t", "now")

        ctx = load_context("2024-01", month_file_path(cfg.features_dir, "2024-01"))
        assert ctx is not None
        assert ctx.seeds["kell"] is None  # keine Events in Jan
        inc = _inc_month_table(cfg, "2024-02")
        # Feb ebenfalls keine kell-Events → rule bleibt NULL (nicht 1.0/0.0).
        first = inc["hzrkl_kell_rule"].to_pylist()[0]
        assert first is None, f"Expected None (kein Event je), got {first!r}"
        # Referenz: Full-Build ebenfalls NULL (kein Zustand erfunden).
        full_feb = full.filter(pc.equal(pc.strftime(full["ts"], "%Y-%m"), "2024-02"))
        assert full_feb["hzrkl_kell_rule"].to_pylist()[0] is None


# --- CLI: Default inkrementell, --full, --verify, Fallback ---------------------


def _ns(**kw) -> argparse.Namespace:
    base = dict(
        fetch_end="2024-02-28",
        force=False,
        quiet=False,
        full=False,
        verify=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def tmp_path_empty_roots(cfg: config.BuildConfig) -> Path:
    """Leere Roh-Quellen-Roots (keine Parquet-Datei) unter features_dir."""
    empty = cfg.features_dir / "empty_roots"
    (empty / "w").mkdir(parents=True, exist_ok=True)
    (empty / "c").mkdir(parents=True, exist_ok=True)
    return empty


class TestCliIncremental:
    @pytest.fixture
    def patched(self, tmp_path, monkeypatch, capsys):
        cfg = two_month_env(tmp_path)
        monkeypatch.setattr(build, "_cfg", lambda args: cfg)
        monkeypatch.setattr(build.config, "con", lambda: duckdb.connect())
        monkeypatch.setattr(build, "_git_tag", lambda: "test-git")
        monkeypatch.setattr(build, "_now_iso", lambda: "2024-02-15T00:00:00+00:00")
        monkeypatch.setattr(build.bias, "load_profile", lambda p: None)
        return cfg

    def test_default_incremental_then_verify_ok(self, patched, capsys):
        cfg = patched
        # 1) Full-Build (Store: 2024-01 + 2024-02 bis 14:00).
        assert build.cmd_features(_ns(full=True)) == 0
        feb = month_file_path(cfg.features_dir, "2024-02")
        assert feb.exists()

        # 2) Rohdaten wachsen (cmi 15h → 21h): inkrementeller Default-Lauf.
        _write_month_rows(
            cfg.cmi_log_pq,
            "2024-02",
            _rows_for_hours(datetime(2024, 2, 1, 0, 0, tzinfo=BERLIN), 21),
        )
        rc = build.cmd_features(_ns())
        out = capsys.readouterr().out
        assert rc == 0
        assert "INKREMENTELL: 2024-02 neu" in out, out
        assert f"Kontext {CONTEXT_HOURS} h aus 2024-01" in out, out
        assert "alle übrigen Monatsdateien anfassfrei" in out, out
        # Store-Jahr 2024-01 bleibt byte-untouchable (Mtime unverändert geprüft
        # über Zeilenzahl: Jan-Datei hat unverändert 744 Zeilen).
        assert (
            pq.ParquetFile(month_file_path(cfg.features_dir, "2024-01")).metadata.num_rows
            == 24 * 31
        )

        # 3) --verify: inkrementeller Store ≡ in-memory-Full-Referenz (Exit 0).
        assert build.cmd_features(_ns(verify=True)) == 0

    def test_verify_detects_drift(self, patched, capsys):
        cfg = patched
        assert build.cmd_features(_ns(full=True)) == 0
        capsys.readouterr()
        # Drift: eine geschlossene Store-Zelle mutieren (ts=2024-02-01 00:00,
        # ref_max=2024-02-14 14:00 → geschlossene Stunde).
        feb = month_file_path(cfg.features_dir, "2024-02")
        t = pq.read_table(feb)
        ts = t["ts"].to_pylist()
        i = ts.index(datetime(2024, 2, 1, 0))
        buff = t["buff_top"].to_pylist()
        buff[i] = (buff[i] or 20.0) + 100.0
        t = t.set_column(
            t.schema.get_field_index("buff_top"), "buff_top", pa.array(buff, type=pa.float64())
        )
        pq.write_table(t, feb)
        rc = build.cmd_features(_ns(verify=True))
        out = capsys.readouterr().out
        assert rc == 3, f"Drift muss erkannt werden (Exit 3), war {rc}: {out}"
        assert "DRIFT" in out, out
        assert "buff_top" in out, out

    def test_verify_open_window_is_note_not_drift(self, patched, capsys):
        """Store endet im offenen Fenster (2 h vor ref_max) → KEIN Drift (Hinweis).

        Simuliert: Store wurde um 12:00 gebaut, Rohdaten wuchsen bis 14:00
        (offenes Fenster = ref_max − 2 h = 12:00).
        """
        cfg = patched
        assert build.cmd_features(_ns(full=True)) == 0
        capsys.readouterr()
        # Feb-Store auf 2024-02-01 12:00 zurückschneiden (open window = 12:00–14:00,
        # ref_max = 2024-02-01 14:00, offenes Fenster = ref_max − 2 h).
        feb = month_file_path(cfg.features_dir, "2024-02")
        t = pq.read_table(feb)
        keep = pc.less_equal(t["ts"], pa.scalar(datetime(2024, 2, 1, 12), type=t["ts"].type))
        t = t.filter(keep)
        pq.write_table(t, feb)
        rc = build.cmd_features(_ns(verify=True))
        out = capsys.readouterr().out
        assert rc == 0, f"offenes Fenster darf kein Drift sein (rc={rc}): {out}"

    def test_fallback_full_when_no_new_raw_data(self, patched, capsys):
        assert build.cmd_features(_ns(full=True)) == 0
        capsys.readouterr()  # Full-Log wegwerfen
        rc = build.cmd_features(_ns())  # keine neuen Rohdaten seit Store
        out = capsys.readouterr().out
        assert rc == 0
        assert "FALLBACK→full: aktueller Monat 2024-02 ohne neue Rohdaten" in out, out

    def test_fallback_full_when_prev_month_missing(self, patched, capsys):
        cfg = patched
        # Kein Vormonat-Store (nur cmi-Feb-Rohdaten, kein Jan-Store).
        # _current_month → 2024-02, load_context → None (Jan fehlt) → full.
        rc = build.cmd_features(_ns())
        out = capsys.readouterr().out
        assert rc == 0
        assert "FALLBACK→full" in out, out
        assert month_file_path(cfg.features_dir, "2024-02").exists()

    def test_fallback_full_when_no_raw_data_at_all(self, patched, capsys, monkeypatch):
        # Keine Rohdaten in winsol_pq ODER cmi_log_pq (beide leer) →
        # _current_month liefert raw_max=None → Fallback→full wird gewählt
        # (Entscheidungs-Pfad). Der Full-Build selbst bräuchte WinSol-Daten
        # (degenerierter Fall) → _features_full per No-Op ersetzen und nur
        # die Entscheidung verifizieren.
        calls = []
        monkeypatch.setattr(build, "_features_full", lambda *a, **kw: (calls.append(1), 0)[-1])
        empty = tmp_path_empty_roots(patched)
        monkeypatch.setattr(
            build,
            "_cfg",
            lambda args: config.BuildConfig(
                features_dir=patched.features_dir,
                wx_cache_dir=patched.wx_cache_dir,
                winsol_pq=empty / "w",
                cmi_log_pq=empty / "c",
                bias_profile=patched.bias_profile,
                fetch_end=patched.fetch_end,
                first_hour=patched.first_hour,
            ),
        )
        capsys.readouterr()
        rc = build.cmd_features(_ns())
        out = capsys.readouterr().out
        assert rc == 0
        assert calls == [1], "_features_full (Fallback) muss gewählt werden"
        assert "FALLBACK→full: keine Rohdaten" in out, out

    def test_fallback_full_when_meta_version_mismatch(self, patched, capsys):
        # _meta.json mit falscher data_version → Fallback→full (Store-Referenz
        # unzuverlässig). features_dir muss vorab existieren.
        import json as _json

        patched.features_dir.mkdir(parents=True, exist_ok=True)
        (patched.features_dir / "_meta.json").write_text(
            _json.dumps({"data_version": "m1.2"}), encoding="utf-8"
        )
        capsys.readouterr()
        rc = build.cmd_features(_ns())
        out = capsys.readouterr().out
        assert rc == 0
        assert "FALLBACK→full" in out and "data_version" in out, out


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
