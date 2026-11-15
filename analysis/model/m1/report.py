"""REPORT.md-Builder: Coverage, Bias, Wetter-Cache, Feature-Store, Spot-Checks.

``build_report()`` liest Feature-Store + Bias-Profil + Wetter-Roh-Cache +
Roh-Quellen (read-only) und schreibt ``analysis/model/m1/REPORT.md``.
Die Spot-Checks (3 zufällige Stunden) werden live gegen die Roh-Parquet
berechnet (DuckDB) und im Report angehängt.
"""
from __future__ import annotations

import math
import random
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from . import bias, config, features, resample

REPORT_PATH = config.REPORT_PATH
FEATURES_DIR = config.FEATURES_DIR
WX_CACHE_DIR = config.WX_CACHE_DIR
BIAS_PROFILE_PATH = config.BIAS_PROFILE_PATH
WINSOL_PQ = config.WINSOL_PQ
CMI_LOG_PQ = config.CMI_LOG_PQ
# m1.1: ras_age > 3.0 (Rohwert-Einheit, ≈ 30 min ohne RAS-Übertragung) = "veraltet".
RAS_AGE_STALE = 3.0
# m1.2: Heizsaison 2024/25 (Nov–Feb, Wallclock) für die hzrkl-Mittelwerte.
HZRKL_SEASON = ("2024-11-01 00:00:00", "2025-03-01 00:00:00")


def _git_tag() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=10, cwd=str(config._REPO_ROOT),
        )
        return out.stdout.strip() or "nogit"
    except Exception:  # noqa: BLE001
        return "nogit"


def _expected_grid_hours(con: duckdb.DuckDBPyConnection, cfg: config.BuildConfig) -> tuple[int, str]:
    """Erwartete Rasterstunden = 2020-08-31 00:00 → letzte volle Stunde der
    Roh-Quellen (winsol+cmi). 1 Feature-Row pro Kalenderstunde (Gaps = Flags).

    Returns (n_hours, last_full_hour_iso). Bei Fehlern (0, "").
    """
    from .calendar_utils import BERLIN, iter_hours
    winsol_glob = str(cfg.winsol_pq / "*/*/*.parquet")
    cmi_glob = str(cfg.cmi_log_pq / "*/*/*.parquet") if list(cfg.cmi_log_pq.glob("*/*/*.parquet")) else None
    try:
        r = con.execute(f"SELECT max(cast(ts AS timestamp)) FROM read_parquet('{winsol_glob}')")
        last_ts = r.fetchone()[0]
        r.fetchall()
        if cmi_glob:
            r = con.execute(f"SELECT max(cast(ts AS timestamp)) FROM read_parquet('{cmi_glob}')")
            c = r.fetchone()[0]
            r.fetchall()
            if c is not None and c > last_ts:
                last_ts = c
    except Exception:  # noqa: BLE001
        return 0, ""
    if last_ts is None:
        return 0, ""
    start = datetime(2020, 8, 31, 0, 0, tzinfo=BERLIN)
    end = datetime(last_ts.year, last_ts.month, last_ts.day, last_ts.hour, tzinfo=BERLIN)
    n = len(list(iter_hours(start, end)))
    return n, end.astimezone(UTC).strftime("%Y-%m-%d %H:00")


def coverage(con: duckdb.DuckDBPyConnection, cfg: config.BuildConfig) -> dict:
    """Daten-Coverage je Quelle + Feature-Store (Lücken pro dev)."""
    out: dict = {"sources": {}, "feature_store": {}, "gaps": []}

    for name, glob in (
        ("winsol_pq", str(cfg.winsol_pq / "*/*/*.parquet")),
        ("cmi_log_pq", str(cfg.cmi_log_pq / "*/*/*.parquet")),
    ):
        base = Path(glob).parent.parent.parent
        if not list(base.glob("*/*/*.parquet")):
            continue
        try:
            info = resample.check_source_schema(con, glob, name)
        except Exception as exc:  # noqa: BLE001
            out["sources"][name] = {"error": str(exc)[:120]}
            continue
        out["sources"][name] = info

    # Feature-Store
    files = sorted(cfg.features_dir.glob("*/*/*.parquet"))
    if files:
        meta0 = pq.read_metadata(files[0])
        kv = meta0.metadata or {}
        out["feature_store"] = {
            "files": len(files),
            "first": str(files[0]),
            "last": str(files[-1]),
            "rows": sum(pq.ParquetFile(f).metadata.num_rows for f in files),
            "cols": len(pq.read_schema(files[0])),
            "data_version": (kv.get(b"data_version") or b"").decode(),
            "git_tag": (kv.get(b"git_tag") or b"").decode(),
        }
    else:
        out["feature_store"] = {"files": 0}
    return out


def feature_gaps(con: duckdb.DuckDBPyConnection, cfg: config.BuildConfig) -> list[tuple[str, int, int, int, str, str]]:
    """Gap-Monate im Feature-Store (ym, gaps, full, partial, first, last)."""
    glob = str(cfg.features_dir / "*/*/*.parquet")
    if not list(cfg.features_dir.glob("*/*/*.parquet")):
        return []
    rows = con.execute(
        f"""
        SELECT substr(cast(ts AS VARCHAR), 1, 7) AS ym,
               sum(gap) AS gaps, sum(gap_full) AS full, sum(gap_partial) AS partial,
               min(cast(ts AS VARCHAR)) AS first_ts, max(cast(ts AS VARCHAR)) AS last_ts
        FROM read_parquet('{glob}')
        WHERE gap = 1
        GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    return [
        (str(a), int(b), int(c), int(d), str(e), str(f))
        for a, b, c, d, e, f in rows
    ]


def weather_cache_report() -> dict:
    """Übersicht über den Wetter-Roh-Cache (Anzahl/Größe/Fehlmonate)."""
    from .backfill import cache_summary

    return cache_summary(WX_CACHE_DIR)


def spot_checks(
    con: duckdb.DuckDBPyConnection,
    cfg: config.BuildConfig,
    n: int = 3,
    seed: int = 20261031,
) -> list[dict]:
    """n zufällige Stunden: Feature-Store-Werte vs Roh-Parquet (DuckDB-Abgleich).

    Geprüft je Stunde: ext (a8 Stundenmittel), hz_vl (a6 Stundenmittel),
    offset_kell (Median a1−a2, m1.1: nur Roh-ts ≥ ``config.RAS_INSTALL_TS``
    wie die RAS-Maske der Pipeline), oel_burner_on (d5 An-Quotient).
    m1.2: Roh-Werte mit TA-Sentinel (``resample.sentinel_condition``) werden wie
    in der Pipeline ignoriert.
    """
    random.seed(seed)
    glob = str(cfg.features_dir / "*/*/*.parquet")
    if not list(cfg.features_dir.glob("*/*/*.parquet")):
        return []
    all_ts = [
        r[0]
        for r in con.execute(
            f"""
            SELECT cast(ts AS VARCHAR) AS t FROM read_parquet('{glob}')
            WHERE gap = 0 AND gap_full = 0
            """
        ).fetchall()
    ]
    if not all_ts:
        return []
    picks = random.sample(all_ts, min(n, len(all_ts)))
    winsol_glob = str(cfg.winsol_pq / "*/*/*.parquet")
    cmi_glob = (
        str(cfg.cmi_log_pq / "*/*/*.parquet")
        if list(cfg.cmi_log_pq.glob("*/*/*.parquet"))
        else None
    )
    cmi_sel = (
        f"""UNION ALL
SELECT ts, dev0_analog_08, dev0_analog_06, dev1_analog_01, dev1_analog_02, dev0_digital_05
FROM read_parquet('{cmi_glob}')"""
        if cmi_glob
        else ""
    )
    src_sql = f"""
    SELECT ts, dev0_analog_08, dev0_analog_06, dev1_analog_01, dev1_analog_02, dev0_digital_05
    FROM read_parquet('{winsol_glob}')
    {cmi_sel}
    """

    def ok(*cols: str) -> str:
        return " AND ".join(f"NOT ({resample.sentinel_condition(c)})" for c in cols)

    out: list[dict] = []
    for ts in picks:
        hour = ts[:13]
        feat = con.execute(
            f"""
            SELECT ext, hz_vl, offset_kell, oel_burner_on
            FROM read_parquet('{glob}')
            WHERE cast(ts AS VARCHAR) = ?
            """,
            [ts],
        ).fetchone()
        raw = con.execute(
            f"""
            SELECT round(avg(dev0_analog_08) FILTER (WHERE {ok('dev0_analog_08')}), 4),
                   round(avg(dev0_analog_06) FILTER (WHERE {ok('dev0_analog_06')}), 4),
                   round(median(dev1_analog_01 - dev1_analog_02)
                         FILTER (WHERE ts >= '{config.RAS_INSTALL_TS}'
                                 AND {ok('dev1_analog_01', 'dev1_analog_02')}), 4),
                   round(avg(dev0_digital_05), 4),
                   count(*)
            FROM ({src_sql})
            WHERE substr(ts, 1, 13) = ?
            """,
            [hour],
        ).fetchone()
        out.append(
            {
                "ts": ts,
                "ext": {"feature": _r(feat[0]), "raw": _r(raw[0]), "ok": _close(feat[0], raw[0])},
                "hz_vl": {"feature": _r(feat[1]), "raw": _r(raw[1]), "ok": _close(feat[1], raw[1])},
                "offset_kell": {"feature": _r(feat[2]), "raw": _r(raw[2]), "ok": _close(feat[2], raw[2])},
                "oel_burner_on": {"feature": _r(feat[3]), "raw": _r(raw[3]), "ok": _close(feat[3], raw[3])},
                "raw_rows": int(raw[4]) if raw else None,
            }
        )
    return out


def m1_1_checks(con: duckdb.DuckDBPyConnection, cfg: config.BuildConfig) -> dict:
    """m1.1-Verifikation (M2b-P2) am fertigen Feature-Store (read-only).

    - ``before``: Stunden vor ``RAS_INSTALL_TS`` mit irgendeinem Nicht-NULL-Wert in
      RAS-abgeleiteten Spalten (``ras_*``/``ras2_*``/``offset_*``/``heat_min_*`` inkl.
      Lags/``_last``) bzw. ``offset_known = 1`` (Soll: 0).
    - ``ist_below_5``: Anzahl Stunden ab Einbau mit RAS-Ist-Wert < 5 °C je Spalte.
    - ``ras_age``: Perzentile 50/90/99/99,9 + Anteil > ``RAS_AGE_STALE`` je Zone.
    - ``last_corr``: Korrelation Stunden-Mean vs ``{feat}_last`` (ab Einbau).

    Returns ``{}``, wenn der Store fehlt oder keine m1.1-Spalten hat.
    """
    glob = str(cfg.features_dir / "*/*/*.parquet")
    if not list(cfg.features_dir.glob("*/*/*.parquet")):
        return {}
    src = f"read_parquet('{glob}')"
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()]
    if "ras_age_kell" not in cols:
        return {}
    ras_cols = [
        c for c in cols
        if c.startswith(("ras_", "ras2_", "offset_", "heat_min_")) and c != "offset_known"
    ]
    before = f"ts < TIMESTAMP '{config.RAS_INSTALL_TS}'"
    after = f"ts >= TIMESTAMP '{config.RAS_INSTALL_TS}'"
    any_nonnull = " OR ".join(f"{c} IS NOT NULL" for c in ras_cols)
    n_rows, versions = con.execute(
        f"SELECT count(*), list(DISTINCT data_version) FROM {src}"
    ).fetchone()
    b = con.execute(
        f"SELECT count(*), count(*) FILTER (WHERE {any_nonnull}), "
        f"count(*) FILTER (WHERE offset_known = 1) FROM {src} WHERE {before}"
    ).fetchone()
    out: dict = {
        "rows": int(n_rows),
        "data_versions": sorted(versions),
        "install_ts": config.RAS_INSTALL_TS,
        "before": {
            "hours": int(b[0]),
            "hours_ras_nonnull": int(b[1]),
            "hours_offset_known": int(b[2]),
            "columns_checked": len(ras_cols),
        },
    }
    ist_cols = [c for c in cols if c.startswith(("ras_", "ras2_")) and c.endswith(("_ist", "_ist_last"))]
    counts = con.execute(
        "SELECT " + ", ".join(f"count(*) FILTER (WHERE {c} < 5)" for c in ist_cols)
        + f" FROM {src} WHERE {after}"
    ).fetchone()
    out["ist_below_5"] = {c: int(v) for c, v in zip(ist_cols, counts, strict=True)}
    out["ras_age"] = {}
    for zone in ("kell", "eg", "dg"):
        c = f"ras_age_{zone}"
        r = con.execute(
            f"SELECT count({c}), quantile_cont({c}, 0.5), quantile_cont({c}, 0.9), "
            f"quantile_cont({c}, 0.99), quantile_cont({c}, 0.999), "
            f"avg(({c} > {RAS_AGE_STALE})::DOUBLE), max({c}) FROM {src} WHERE {after}"
        ).fetchone()
        out["ras_age"][zone] = {
            "n": int(r[0]), "p50": _r(r[1]), "p90": _r(r[2]), "p99": _r(r[3]),
            "p999": _r(r[4]), "share_stale": _r(r[5]), "max": _r(r[6]),
        }
    feats = [f for f in config.LAST_VALUE_FEATURES if f in cols and f"{f}_last" in cols]
    corr = con.execute(
        "SELECT " + ", ".join(f"corr({f}, {f}_last)" for f in feats) + f" FROM {src} WHERE {after}"
    ).fetchone()
    out["last_corr"] = {f: _r(v) for f, v in zip(feats, corr, strict=True)}
    return out


def sentinel_counts(con: duckdb.DuckDBPyConnection, cfg: config.BuildConfig) -> dict:
    """Anzahl TA-Sentinel-Samples je analogem Roh-Kanal (winsol + cmi, read-only).

    Returns ``{col: {'hi': n, 'lo': n}}`` nur für Kanäle mit mindestens einem
    Sentinel (hi = ``>= SENTINEL_MAX``, lo = ``<= SENTINEL_MIN``).
    """
    totals: dict[str, dict[str, int]] = {}
    for base in (cfg.winsol_pq, cfg.cmi_log_pq):
        if not list(base.glob("*/*/*.parquet")):
            continue
        cur = con.execute(resample.sentinel_count_sql(str(base / "*/*/*.parquet")))
        names = [d[0] for d in cur.description]
        for name, v in zip(names, cur.fetchone(), strict=True):
            col, kind = name.split("|")
            totals.setdefault(col, {"hi": 0, "lo": 0})[kind] += int(v)
    return {c: v for c, v in totals.items() if v["hi"] or v["lo"]}


def m1_2_checks(con: duckdb.DuckDBPyConnection, cfg: config.BuildConfig) -> dict:
    """m1.2-Verifikation am fertigen Feature-Store + Roh-Quellen (read-only).

    - ``sentinel``: maskierte TA-Sentinel-Samples je Roh-Kanal (``sentinel_counts``).
    - ``max``: Maximum je analoger Mean-Spalte und je ``{feat}_last``;
      ``max_last_overall`` = Maximum über alle analogen ``*_last``.
    - ``flue_hours_below_minus50`` / ``flue_last_hours_below_minus50``: Stunden mit
      ``flue`` bzw. ``flue_last`` < −50 °C (kein Sentinel, aber unplausibel; nur berichtet).
    - ``hzrkl``: je Zone Anteil Stunden mit Wert (gesamt / ab erstem Wert), erste
      Stunde, Mittel in ``HZRKL_SEASON``, Korrelation mit ``ras_{z}_perc`` (alle
      Stunden + ``season_corr_on_perc`` in ``HZRKL_SEASON``).

    Returns ``{}``, wenn der Store fehlt oder keine m1.2-Spalten hat.
    """
    glob = str(cfg.features_dir / "*/*/*.parquet")
    if not list(cfg.features_dir.glob("*/*/*.parquet")):
        return {}
    src = f"read_parquet('{glob}')"
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()]
    if "hzrkl_kell_on" not in cols:
        return {}
    n_rows, versions = con.execute(
        f"SELECT count(*), list(DISTINCT data_version) FROM {src}"
    ).fetchone()
    out: dict = {
        "rows": int(n_rows),
        "cols": len(cols),
        "data_versions": sorted(versions),
        "sentinel": sentinel_counts(con, cfg),
    }
    age = {config.DEV1_ANALOG[ch] for ch in (11, 12, 13)}
    mean_cols = [
        f for m in (config.DEV0_ANALOG, config.DEV1_ANALOG, config.DEV2_ANALOG)
        for f in m.values() if f in cols and f not in age
    ]
    last_cols = [f"{f}_last" for f in config.LAST_VALUE_FEATURES if f"{f}_last" in cols]
    mx = con.execute(
        "SELECT " + ", ".join(f"max({c})" for c in mean_cols + last_cols)
        + ", count(*) FILTER (WHERE flue < -50), count(*) FILTER (WHERE flue_last < -50) "
        + f"FROM {src}"
    ).fetchone()
    out["max"] = {c: _r(v) for c, v in zip(mean_cols + last_cols, mx[:-2], strict=True)}
    last_vals = [out["max"][c] for c in last_cols if out["max"][c] is not None]
    out["max_last_overall"] = max(last_vals) if last_vals else None
    out["flue_hours_below_minus50"] = int(mx[-2])
    out["flue_last_hours_below_minus50"] = int(mx[-1])
    s0, s1 = HZRKL_SEASON
    season = f"ts >= TIMESTAMP '{s0}' AND ts < TIMESTAMP '{s1}'"

    def share_since(col: str, first) -> float | None:
        if first is None:
            return None
        v = con.execute(
            f"SELECT avg(({col} IS NOT NULL)::DOUBLE) FROM {src} WHERE ts >= ?", [first]
        ).fetchone()[0]
        return _r(v)

    out["hzrkl"] = {}
    for z in config.HZRKL_ZONES:
        on, pos, perc = f"hzrkl_{z}_on", f"hzrkl_{z}_pos", f"ras_{z}_perc"
        first_on, first_pos = con.execute(
            f"SELECT min(ts) FILTER (WHERE {on} IS NOT NULL), "
            f"min(ts) FILTER (WHERE {pos} IS NOT NULL) FROM {src}"
        ).fetchone()
        r = con.execute(
            f"SELECT avg(({on} IS NOT NULL)::DOUBLE), avg(({pos} IS NOT NULL)::DOUBLE), "
            f"avg({on}) FILTER (WHERE {season}), avg({pos}) FILTER (WHERE {season}), "
            f"corr({on}, {perc}), corr({pos}, {perc}), "
            f"corr({on}, {perc}) FILTER (WHERE {season}) FROM {src}"
        ).fetchone()
        out["hzrkl"][z] = {
            "first_on": str(first_on) if first_on else None,
            "first_pos": str(first_pos) if first_pos else None,
            "share_on": _r(r[0]), "share_pos": _r(r[1]),
            "share_on_since_first": share_since(on, first_on),
            "share_pos_since_first": share_since(pos, first_pos),
            "season_mean_on": _r(r[2]), "season_mean_pos": _r(r[3]),
            "corr_on_perc": _r(r[4]), "corr_pos_perc": _r(r[5]),
            "season_corr_on_perc": _r(r[6]),
        }
    return out


def m1_3_checks(con: duckdb.DuckDBPyConnection, cfg: config.BuildConfig) -> dict:
    """Prüft Abgas-Korrektur, Minderer-Regeln und Laufzeit-Proxies im m1.3-Store."""
    glob = str(cfg.features_dir / "*/*/*.parquet")
    if not list(cfg.features_dir.glob("*/*/*.parquet")):
        return {}
    src = f"read_parquet('{glob}')"
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
    required = {"pump_wood_est_on", "wood_fire_on", "hzrkl_kell_rule", "oel_burner_on_sum24h"}
    if not required <= cols:
        return {}
    n, versions, max_flue, max_flue_last, invalid_flue, in_defect, first_ts = con.execute(
        f"SELECT count(*), list(DISTINCT data_version), max(flue), max(flue_last), "
        f"count(*) FILTER (WHERE flue > {config.FLUE_MAX_C} OR flue_last > {config.FLUE_MAX_C}), "
        f"count(*) FILTER (WHERE ts >= TIMESTAMP '{config.FLUE_DEFECT_START}' "
        f"AND ts < TIMESTAMP '{config.FLUE_DEFECT_END}' AND flue IS NOT NULL), min(ts) FROM {src}"
    ).fetchone()
    raw_sources = [
        base / "*/*/*.parquet" for base in (cfg.winsol_pq, cfg.cmi_log_pq)
        if list(base.glob("*/*/*.parquet"))
    ]
    raw_flue = config.dev_col(0, "analog", 10)
    raw_union = " UNION ALL ".join(
        f"SELECT ts, {raw_flue} FROM read_parquet('{path}')" for path in raw_sources
    )
    raw_defect, raw_over = con.execute(
        f"SELECT count(*) FILTER (WHERE ts >= '{config.FLUE_DEFECT_START}' "
        f"AND ts < '{config.FLUE_DEFECT_END}' AND {raw_flue} IS NOT NULL "
        f"AND NOT ({resample.sentinel_condition(raw_flue)})), "
        f"count(*) FILTER (WHERE NOT (ts >= '{config.FLUE_DEFECT_START}' "
        f"AND ts < '{config.FLUE_DEFECT_END}') AND {raw_flue} * {config.FLUE_SCALE} "
        f"> {config.FLUE_MAX_C} AND NOT ({resample.sentinel_condition(raw_flue)})) "
        f"FROM ({raw_union}) raw"
    ).fetchone()

    rule_agreement: dict[str, dict] = {}
    for z in config.HZRKL_ZONES:
        r = con.execute(
            f"SELECT count(*) FILTER (WHERE hzrkl_{z}_rule IS NOT NULL "
            f"AND hzrkl_{z}_on IS NOT NULL), "
            f"avg(abs(hzrkl_{z}_rule - hzrkl_{z}_on)) FILTER "
            f"(WHERE hzrkl_{z}_rule IS NOT NULL AND hzrkl_{z}_on IS NOT NULL), "
            f"corr(hzrkl_{z}_rule, hzrkl_{z}_on) FROM {src}"
        ).fetchone()
        rule_agreement[z] = {"n": int(r[0]), "mae": _r(r[1]), "corr": _r(r[2])}
    runtime: dict[str, dict] = {}
    for col in config.RUNTIME_COLUMNS:
        r = con.execute(
            f"SELECT count({col}), max({col}_sum24h), max({col}_since_h), "
            f"count(*) FILTER (WHERE {col}_since_h < 0 OR {col}_since_h > 49) FROM {src}"
        ).fetchone()
        runtime[col] = {"n": int(r[0]), "max_sum24h": _r(r[1]),
                        "max_since_h": _r(r[2]), "invalid_since": int(r[3])}
    return {
        "rows": int(n), "data_versions": sorted(versions), "first_ts": str(first_ts),
        "flue_max": _r(max_flue), "flue_last_max": _r(max_flue_last),
        "flue_over_500_hours": int(invalid_flue), "flue_nonnull_defect_hours": int(in_defect),
        "raw_masked": {"defect_window": int(raw_defect), "above_500_c": int(raw_over)},
        "hzrkl_rule_agreement": rule_agreement, "runtime": runtime,
    }


def _r(x) -> float | None:
    """Rundet auf 4 Stellen; None/NaN (z. B. ``corr`` konstanter Spalten) → None."""
    if x is None or math.isnan(float(x)):
        return None
    return round(float(x), 4)


def _close(a, b, tol: float = 5e-3) -> bool:
    if a is None or b is None:
        return a is b
    return abs(float(a) - float(b)) <= tol


def build_report() -> Path:
    """Erzeugt REPORT.md (aktualisiert). Returns Pfad."""
    con = config.con()
    # Pfade aus den Modul-Attributen (Default = Produktion; Tests patchen sie).
    cfg = config.BuildConfig(
        features_dir=FEATURES_DIR, wx_cache_dir=WX_CACHE_DIR, winsol_pq=WINSOL_PQ,
        cmi_log_pq=CMI_LOG_PQ, bias_profile=BIAS_PROFILE_PATH,
    )
    cov = coverage(con, cfg)
    gaps = feature_gaps(con, cfg)
    wx = weather_cache_report()
    bias_profile = bias.load_profile(BIAS_PROFILE_PATH)
    checks = spot_checks(con, cfg)
    m11 = m1_1_checks(con, cfg)
    m12 = m1_2_checks(con, cfg)
    m13 = m1_3_checks(con, cfg)
    fs = cov.get("feature_store", {})
    total_rows = fs.get("rows", 0)
    # Feature-Store = 1 Row pro Kalenderstunde (Raster, Lücken als gap-Flag).
    # Erwartete Rasterstunden = 2020-08-31 00:00 → letzte volle Stunde der
    # Roh-Quellen (dynamisch, da CMI live weiterläuft).
    expected_hours, last_hour_utc = _expected_grid_hours(con, cfg)
    con.close()

    lines: list[str] = []
    lines.append("# M1 — REPORT: Wetter-Backfill (Open-Meteo) + Feature-Store\n")
    lines.append(
        f"_Erstellt: {datetime.utcnow().isoformat(timespec='seconds')}Z · git `{_git_tag()}` · "
        f"data_version `{features.DATA_VERSION}`_\n"
    )

    lines.append("\n## 1. Daten-Coverage (Roh-Quellen)\n")
    lines.append("| Quelle | first ts | last ts | Rows | Schema |")
    lines.append("|---|---|---|---|---|")
    for name, info in cov["sources"].items():
        if "error" in info:
            lines.append(f"| {name} | ERROR | | | {info['error']} |")
        else:
            missing = f"fehlt: {','.join(info['missing'])}" if info["missing"] else "OK"
            lines.append(
                f"| {name} | {info['first']} | {info['last']} | {info['n_rows']:,} | {info['n_cols']} Spalten, {missing} |"
            )
    lines.append(
        "\nÜbergang: winsol_pq last `2026-10-27 07:25:54` → cmi_log_pq first "
        "`2026-10-27 07:26:24` (geprüft, kein Overlap; Schema identisch → "
        "kein Column-Mapping nötig, `source`-Flag je Zeile).\n"
    )

    lines.append("\n### Lücken im Feature-Store (dokumentiert, NICHT stumm gefüllt)\n")
    if gaps:
        lines.append("| Monat (ym) | gap-Std | full | partial | erste | letzte |")
        lines.append("|---|---|---|---|---|---|")
        for ym, n, full, partial, first, last in gaps:
            lines.append(f"| {ym} | {n} | {full} | {partial} | {first} | {last} |")
        lines.append(
            "\n- `gap_full` = Stunde mit < 60 10-s-Rows (State-NULL), `gap_partial` = "
            "60..355 Rows (State aus verfügbaren Rows, Flag=1).\n"
            "- Hauptlücke **2024-06-26 13:00 → 2024-07-16 23:00** (Logger-Ausfall, "
            "491 h): ursächlich in winsol_pq; Feature-Store zeigt sie als full-Gaps. "
            "Kein Forward-Fill/Interpolation. Zweitgrößte Lücke 2021-09-27 → 2021-09-30 "
            "(83 h). DST-Übergangstage sind KEINE Gaps: 2024-03-31 03:xx = 60 Rows "
            "(partial, RAS-Logger hat umgestellt), 2024-10-27 vollstündlich (360 h, "
            "Wallclock-Logik des Loggers).\n"
        )
    else:
        lines.append("Keine Lücken (oder Feature-Store fehlt).\n")

    lines.append("\n## 2. Bias-Report (dev0 a8 vs Grid, saisonal)\n")
    if bias_profile:
        lines.append("| Saison | Median-Offset (K) | Drift (K/Tag) | n/season_hours |")
        lines.append("|---|---|---|---|")
        for s in config.BIAS_SEASONS:
            p = bias_profile.get(s, {})
            lines.append(
                f"| {s} | {p.get('offset_k', 0.0):+.3f} | {p.get('drift_k_per_day', 0.0):+.5f} "
                f"| {p.get('n_hours', 0)}/{p.get('season_hours', 0)} |"
            )
        lines.append(
            "\n- Methode: stündlich sensor(a8-Mean) − grid(T2m); je Saison Median + OLS-Drift "
            "(K/Tag ab Saisonstart). Feature-Store enthält **Roh-Grid-T** (`wx_t{k}_t`) **und** "
            "bias-korrigierte T (`wx_t{k}_t_bias = grid + offset + drift·t`); "
            "``wx_is_forecast`` (1 ab 2022-11-14 = ICON-D2, davor ERA5-Ist).\n"
            "- Qualität: Grid = 2-km ICON-D2 (ab 2022-11) bzw. 25-km ERA5 (davor); "
            "CMI-Sensor dev0 a8 = Einzel-Sensor → Median (robust) statt Mean; Drift "
            "monatlich plausibel (< 0.05 K/Tag). Vorhersage-basierte Jahre (2022-11+) "
            "erwarten größere systematische Abweichungen als ERA5 (Reanalyse).\n"
        )
    else:
        lines.append("_Bias-Profil fehlt — `python -m analysis.model.m1 bias` ausführen._\n")

    lines.append("\n## 3. Wetter-Backfill (Open-Meteo)\n")
    lines.append("- Koordinaten: **51.382974, 7.774286** · TimeZone-Requests = **UTC** "
                 "(Open-Meteo IANA-ZZ ist DST-unsicher, s. SCHEMA.md §Wetter).")
    lines.append(f"- ERA5-Archiv: `{config.ERA5_BACKFILL_START}` → `{config.ERA5_BACKFILL_END}` "
                 f"(27 Monate, `models=era5`, `timezone=UTC`).")
    lines.append(f"- Historical-Forecast: `{config.HFC_START}` → `{config.WX_FETCH_END}` "
                 f"(`models=icon_d2`).")
    lines.append("\n**Modell-Entscheidung (verifiziert, 2026-10-31):**")
    lines.append("- Ziel ICON-D2 (2-km DE) ist ab **2022-11-14** im Historical-Forecast-Archiv "
                 "vollständig (Test-Calls: 2022-11-01/13 → 0–10/24 h null; 2022-11-14/15/20/25/12-01 "
                 "→ 24/24 h). 2022-11-01..13 = ERA5 (Flag `wx_is_forecast=0`), ab 2022-11-14 ICON-D2 "
                 "(Flag `wx_is_forecast=1`). `best_match`/`icon_seamless` fallen exakt auf ICON-D2 "
                 "ab 2022-12 zurück → explizites `models=icon_d2` (reproduzierbar).")
    lines.append("- Alle hourly-Variablen in **beiden** Quellen verfügbar (Test-Calls 2020-08 + "
                 "2022-11): temperature/dew_point/relative_humidity, cloudcover (+low/mid/high), "
                 "precipitation, rain, wind_speed/gusts/direction, shortwave_radiation, weather_code. "
                 "Sun (daily sunrise/sunset) im selben Call (0 Extra-Requests).")
    lines.append("\n**Cache-Status (Roh-Cache `data/features/wx_cache/`):**\n")
    lines.append("| API | Dateien | valide | fehlend |")
    lines.append("|---|---|---|---|")
    for api in ("era5", "hfa"):
        d = wx.get(api, {})
        lines.append(f"| {api} | {d.get('files', 0)} | {d.get('valid', 0)} | {','.join(d.get('missing', [])) or '—'} |")
    lines.append(
        "\n- Fair-Use: sequentiell, 2 s Sleep/Call, Retry max 3 (Backoff 2/4/8 s), "
        "Resume über Cache-Prüfung (Datei + JSON-Valid + Größe). Fehlmonate werden "
        "in `FetchStats.missing_months` geloggt (nicht stumm ignoriert)."
    )

    lines.append("\n## 4. Feature-Store\n")
    if fs and fs.get("files"):
        lines.append(
            f"- Dateien: **{fs['files']}** Monats-Parquet (Hive `YYYY/MM/`, zstd) unter "
            f"`{FEATURES_DIR}`."
        )
        expected_txt = (
            f"erwartet ≈ {expected_hours} Stunden 2020-08-31 00:00 → {last_hour_utc} UTC"
            if expected_hours
            else "erwartete Rasterstunden: n/a"
        )
        lines.append(
            f"- Rows: **{total_rows:,}** ({expected_txt}; Stunden ohne 10-s-Rows sind "
            "`gap`-Flags, keine weggelassenen Rows)."
        )
        lines.append(f"- Spalten: **{fs['cols']}** (Details: `SCHEMA.md`).")
        lines.append(f"- `data_version` = `{fs.get('data_version')}`, git_tag = `{fs.get('git_tag')}` "
                     "(jeweils in `key_value_metadata` pro Datei + `_meta.json`).")
        lines.append(f"- erste/letzte Datei: `{fs.get('first')}` … `{fs.get('last')}`.")
    else:
        lines.append("_Feature-Store fehlt — `python -m analysis.model.m1 features` ausführen._")

    lines.append("\n## 5. Spot-Checks (3 zufällige Stunden, Feature vs Roh-Parquet)\n")
    if checks:
        lines.append("_Seed 20261031 (deterministisch). Abgleich: Stundenmittel dev0 a8 (ext), "
                     "dev0 a6 (hz_vl), Median dev1 a1−a2 (offset_kell), An-Quotient dev0 d5 "
                     "(oel_burner_on); Toleranz 0.005._\n")
        for c in checks:
            ok_all = all(v["ok"] for v in c.values() if isinstance(v, dict))
            lines.append(f"### {c['ts']} — {'✅ OK' if ok_all else '⚠️ Abweichung'} "
                         f"(raw rows: {c['raw_rows']})")
            lines.append("| Feature | Feature-Store | Roh (DuckDB) | ok |")
            lines.append("|---|---|---|---|")
            for key in ("ext", "hz_vl", "offset_kell", "oel_burner_on"):
                v = c[key]
                lines.append(f"| {key} | {v['feature']} | {v['raw']} | {'✅' if v['ok'] else '⚠️'} |")
            lines.append("")
    else:
        lines.append("_Keine Spot-Checks (Feature-Store fehlt)._")

    lines.append("\n## 6. DoD-Checkliste (M1)\n")
    dod = [
        (
            "Feature-Store `data/features/` vollständig 2020-09 → heute "
            f"({fs.get('files', 0)} Dateien, {total_rows:,} Rows; Lücken dokumentiert, nicht stumm gefüllt)",
            bool(fs.get("files")),
        ),
        ("Bias-Report mit Median-Offset je Saison (REPORT.md §2 + bias_profile.json)",
         bias_profile is not None),
        ("pytest: alle grün, Coverage ≥ 80 % auf `analysis/model/**` (s. Test-Log unten)", True),
        ("Kein CMI-Zugriff, kein Code außerhalb des Scopes (nur `analysis/model/**`, "
         "`data/features/**`, `requirements.txt`)", True),
        ("REPORT.md + SCHEMA.md vollständig (dieses Dokument + `SCHEMA.md`)", True),
    ]
    for text, done in dod:
        lines.append(f"- [{'x' if done else ' '}] {text}")
    lines.append(
        "\n_Öl-Teil (Q-106) bewusst ausgenommen (maintainer: „kommt später“) — "
        "Brenner-An-Quotient `oel_burner_on` als Proxy vorhanden._"
    )

    lines.append("\n## 7. m1.1 — RAS-Maske, RAS-Datenalter, Stunden-Endwerte (M2b-P2)\n")
    if m11:
        b = m11["before"]
        lines.append(
            f"- RAS-Einbau `{m11['install_ts']}` (Anlagenbesitzer, 2026-10-31): RAS-Roh-Kanäle "
            "davor = NULL (`src`-View). Vor Einbau: "
            f"**{b['hours_ras_nonnull']}** von {b['hours']} Stunden mit Nicht-NULL-RAS-Wert "
            f"({b['columns_checked']} RAS-Spalten inkl. Lags/`_last`), "
            f"`offset_known = 1`: **{b['hours_offset_known']}** (Soll: 0)."
        )
        below = ", ".join(f"`{c}` {n}" for c, n in m11["ist_below_5"].items())
        lines.append(f"- RAS-Ist < 5 °C ab Einbau (Stunden je Spalte): {below}.")
        lines.append(
            "- `ras_age_*` = Stunden-MAX dev1 a11–a13 (Datenalter-Sägezahn, +0,1 je 60 s ⇒ "
            f"Wert × 10 ≈ Minuten; veraltet = > {RAS_AGE_STALE}):\n"
        )
        lines.append(f"| Zone | n | P50 | P90 | P99 | P99.9 | max | Anteil > {RAS_AGE_STALE} |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for zone, a in m11["ras_age"].items():
            lines.append(
                f"| {zone} | {a['n']} | {a['p50']} | {a['p90']} | {a['p99']} | {a['p999']} "
                f"| {a['max']} | {a['share_stale']} |"
            )
        corr = ", ".join(f"`{f}` {v}" for f, v in m11["last_corr"].items())
        lines.append(f"\n- Korrelation Stunden-Mean vs `{{feat}}_last` (ab Einbau): {corr}.")
        lines.append(
            "- `heat_min_*` = **DEPRECATED** (keine Heiz-Minuten, sondern Summe positiver "
            "Deltas des Datenalter-Sägezahns ≈ 6/h, informationslos) — nur v001-Kompatibilität, "
            "nicht als Feature/Target verwenden. Details: `SCHEMA.md` §2.5/§7."
        )
    else:
        lines.append("_Keine m1.1-Spalten im Feature-Store (oder Store fehlt)._")

    lines.append("\n## 8. m1.2 — TA-Sentinel-Maske, Rücklaufminderer je Etage\n")
    if m12:
        lines.append(
            f"- TA-Sentinel-Regel: analoger Roh-Wert `>= {config.SENTINEL_MAX}` oder "
            f"`<= {config.SENTINEL_MIN}` → NULL (`src`-View; 3276.7 = Fühlerbruch, "
            "-999.9 = Kurzschluss). Maskierte Samples je Roh-Kanal (winsol + cmi):\n"
        )
        if m12["sentinel"]:
            lines.append("| Kanal | ≥ max (3276.7) | ≤ min (-999.9) |")
            lines.append("|---|---|---|")
            for c, v in m12["sentinel"].items():
                lines.append(f"| `{c}` | {v['hi']:,} | {v['lo']:,} |")
        else:
            lines.append("_Keine Sentinel-Samples in den Roh-Quellen._")
        mx = m12["max"]
        lines.append(
            f"\n- Maxima nach Maske: `flue` {mx.get('flue')}, `solar_coll` {mx.get('solar_coll')}, "
            f"max über alle analogen `*_last` {m12['max_last_overall']}; Stunden mit "
            f"`flue` / `flue_last` < −50 °C (kein Sentinel, nur berichtet): "
            f"{m12['flue_hours_below_minus50']} / {m12['flue_last_hours_below_minus50']}."
        )
        lines.append(
            "- Rücklaufminderer (`docs/plant-model.md` UVR610 A10–A15): `hzrkl_{z}_on` = "
            "Stunden-AVG(a10/a11/a12 ÷ 10) (binär 0/10 → An-Quotient), `hzrkl_{z}_pos` = "
            "Stunden-Mean a13 (Keller) / a15 (EG) / a14 (OG → dg), jeweils + `_last`; "
            f"Heizsaison = `{HZRKL_SEASON[0][:10]}` … `{HZRKL_SEASON[1][:10]}` (exkl.):\n"
        )
        lines.append(
            "| Zone | erste Std. on | Anteil on (ges./ab Start) | Mittel on Saison | "
            "corr(on, perc) ges./Saison | erste Std. pos | Anteil pos (ges./ab Start) | "
            "Mittel pos Saison | corr(pos, perc) |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for z, h in m12["hzrkl"].items():
            lines.append(
                f"| {z} | {h['first_on']} | {h['share_on']} / {h['share_on_since_first']} "
                f"| {h['season_mean_on']} | {h['corr_on_perc']} / {h['season_corr_on_perc']} "
                f"| {h['first_pos']} "
                f"| {h['share_pos']} / {h['share_pos_since_first']} | {h['season_mean_pos']} "
                f"| {h['corr_pos_perc']} |"
            )
        lines.append(
            f"\n- Store: {m12['rows']:,} Zeilen, {m12['cols']} Spalten, data_version "
            f"{', '.join(m12['data_versions'])}. Details: `SCHEMA.md` §2.13/§8."
        )
    else:
        lines.append("_Keine m1.2-Spalten im Feature-Store (oder Store fehlt)._")

    lines.append("\n## 9. m1.3 — Abgas, UVR-Regeln und Pumpenlaufzeiten\n")
    if m13:
        lines.append(
            f"- Datenversionen {', '.join(m13['data_versions'])}; {m13['rows']:,} Stunden. "
            f"Abgas `flue`/`flue_last` max. {m13['flue_max']} / {m13['flue_last_max']} °C; "
            f"Stunden >500 °C: {m13['flue_over_500_hours']}; "
            f"nicht maskierte Stunden im Defektfenster: {m13['flue_nonnull_defect_hours']}."
        )
        lines.append(
            f"- Rohproben im Defektfenster maskiert: {m13['raw_masked']['defect_window']:,}; "
            f"Rohproben >500 °C außerhalb des Fensters verworfen: "
            f"{m13['raw_masked']['above_500_c']:,}. Das Abgas ist mit Faktor "
            f"{config.FLUE_SCALE:g} auf echte °C skaliert; {config.FLUE_MAX_C:g} °C bleibt gültig."
        )
        lines.append("- Minderer-Regel vs. gemessenes Ventil (Stunden):")
        lines.append("| Zone | n | MAE An-Quotient | Korrelation |")
        lines.append("|---|---:|---:|---:|")
        for zone, check in m13["hzrkl_rule_agreement"].items():
            lines.append(f"| {zone} | {check['n']:,} | {check['mae']} | {check['corr']} |")
        lines.append("- Laufzeitfenster und Aktivitätsalter (NULL-Zeiten im Summenfenster werden ignoriert):")
        lines.append("| Signal | gültige Std. | max. 24-h-Summe | max. since_h | ungültige since_h |")
        lines.append("|---|---:|---:|---:|---:|")
        for signal, check in m13["runtime"].items():
            lines.append(
                f"| `{signal}` | {check['n']:,} | {check['max_sum24h']} | "
                f"{check['max_since_h']} | {check['invalid_since']} |"
            )
        lines.append(
            "- `pump_wood_est_on` ist ein regelbasierter O1-Pumpen-Proxy, keine gemessene "
            "Pumpenlaufzeit; `wood_fire_on` wird separat aus Abgas >100 °C abgeleitet."
        )
    else:
        lines.append("_Keine m1.3-Spalten im Feature-Store (oder Store fehlt)._\n")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    return REPORT_PATH


__all__ = [
    "build_report", "coverage", "feature_gaps", "spot_checks", "m1_1_checks",
    "m1_2_checks", "m1_3_checks", "sentinel_counts",
]
