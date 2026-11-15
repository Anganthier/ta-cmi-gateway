"""Bias-Korrektur: CMI-Außensensor (dev0 a8, Stundenmittel) vs Grid (Open-Meteo).

Methodik (s. REPORT.md/SCHEMA.md):
- Stündlich (Kalenderstunde Europe/Berlin): ``grid_h`` = Grid-T der Stunde
  (Roh-Grid, ERA5/ICON-D2), ``sensor_h`` = Stundenmittel dev0 a8 (``hourly_agg.ext``).
- Je Saison (DJF/MAM/JJA/SON):
  - ``offset_k`` = **Median**(sensor − grid) über alle gültigen Stunden der Saison.
  - ``drift_k_per_day`` = **OLS-Steigung** (sensor − grid) ~ Tag-in-Saison
    (Tag 0 = Saison-Start) in K/Tag.
- Ergebnis: ``analysis/model/m1/bias_profile.json`` (vom Feature-Builder gelesen;
  Feature-Store enthält **sowohl** Roh-Grid-T ``wx_t{k}_t`` **als auch**
  bias-korrigierte T ``wx_t{k}_t_bias``).

Kein Netzwerk-Zugriff: reines DuckDB/Python auf Roh-Quellen + Wetter-Roh-Cache.
``compute_bias`` erwartet eine Connection, auf der ``hour_grid`` + ``hourly_agg``
registriert/erzeugt sind (siehe ``prepare_con``).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from . import config, resample
from .features import build_hour_grid, last_full_hour_wall, load_wx_long


@dataclass
class BiasResult:
    """Saisonaler Bias-Profil (je Saison)."""

    djf: dict[str, float]
    mam: dict[str, float]
    jja: dict[str, float]
    son: dict[str, float]
    n_hours_total: int
    grid_coverage: dict[str, float]  # saison → Anteil gültiger Grid-Std (0..1)

    def as_profile(self) -> dict[str, dict[str, float]]:
        return {
            "DJF": self.djf,
            "MAM": self.mam,
            "JJA": self.jja,
            "SON": self.son,
        }


def _season_of(month: int) -> str:
    if month in (12, 1, 2):
        return "DJF"
    if month in (3, 4, 5):
        return "MAM"
    if month in (6, 7, 8):
        return "JJA"
    return "SON"


def _season_start(y: int, month: int) -> datetime:
    """Erster Tag (UTC-Mitternacht) der Saison von (y, month)."""
    if month in (12, 1, 2):
        yy = y - 1 if month == 12 else y
        mm = 12 if month == 12 else 1
        return datetime(yy, mm, 1, tzinfo=UTC)
    start_month = {3: 3, 4: 3, 5: 3, 6: 6, 7: 6, 8: 6, 9: 9, 10: 9, 11: 9}[month]
    return datetime(y, start_month, 1, tzinfo=UTC)


def _scalar(con: duckdb.DuckDBPyConnection, sql: str):
    """Skalares SELECT + komplettes Leeren des Resultats (duckdb 2.0.0: ein
    nicht geleertes Resultat blockiert ``register()`` → ``fetchall()`` drännt)."""
    r = con.execute(sql)
    row = r.fetchone()
    r.fetchall()
    return row


def prepare_con(con: duckdb.DuckDBPyConnection, cfg: config.BuildConfig) -> None:
    """Erzeugt auf ``con`` die Views ``hour_grid`` + ``hourly_agg`` (für Bias + Features)."""
    winsol_glob = str(cfg.winsol_pq / "*/*/*.parquet")
    cmi_glob = (
        str(cfg.cmi_log_pq / "*/*/*.parquet")
        if list(cfg.cmi_log_pq.glob("*/*/*.parquet"))
        else None
    )
    winsol_max = _scalar(con, f"SELECT max(cast(ts AS timestamp)) FROM read_parquet('{winsol_glob}')")[0]
    last_ts = winsol_max
    if cmi_glob:
        cmi_max = _scalar(con, f"SELECT max(cast(ts AS timestamp)) FROM read_parquet('{cmi_glob}')")[0]
        if cmi_max > last_ts:
            last_ts = cmi_max
    grid = build_hour_grid(
        cfg.first_hour, last_full_hour_wall(last_ts)
    )
    con.register("hour_grid", grid)
    con.execute(resample.build_hourly_sql(winsol_glob, cmi_glob))


def compute_bias(
    con: duckdb.DuckDBPyConnection,
    cfg: config.BuildConfig,
) -> BiasResult:
    """Berechnet das saisonale Bias-Profil (dev0 a8 vs Grid).

    Voraussetzung: ``prepare_con(con, cfg)`` (hour_grid + hourly_agg vorhanden).
    """
    era5_months = config.month_range(config.ERA5_BACKFILL_START, config.ERA5_BACKFILL_END)
    hfa_months = config.month_range(config.HFC_START[:7] + "01", cfg.fetch_end)
    wx = load_wx_long(cfg.wx_cache_dir, era5_months, hfa_months)
    con.register("wx_long", wx)

    con.execute(
        """
        CREATE OR REPLACE TABLE bias_pairs AS
        SELECT
            date(g.ts) AS ts_date,
            w.value AS grid_t,
            agg.ext AS sensor_t,
            (agg.ext - w.value) AS resid
        FROM hour_grid g
        LEFT JOIN (SELECT hour_utc, value FROM wx_long WHERE var = 'temperature_2m') w
          ON w.hour_utc = g.hour_utc
        LEFT JOIN hourly_agg agg
          ON agg.ts_bucket = strftime(g.ts, '%Y-%m-%d %H') || ':00'
        WHERE agg.ext IS NOT NULL AND w.value IS NOT NULL
          AND agg.ext BETWEEN -40 AND 60
          AND w.value BETWEEN -40 AND 60
        """
    )
    rows = con.execute("SELECT ts_date, resid FROM bias_pairs").fetchall()

    by_season: dict[str, list[tuple[datetime, float]]] = {s: [] for s in config.BIAS_SEASONS}
    for ts_date, resid in rows:
        by_season[_season_of(ts_date.month)].append((ts_date, float(resid)))

    grid_season_hours: dict[str, int] = dict(
        con.execute(
            """
            SELECT _s, count(*) FROM (
              SELECT CASE
                WHEN month(ts) IN (12, 1, 2) THEN 'DJF'
                WHEN month(ts) IN (3, 4, 5) THEN 'MAM'
                WHEN month(ts) IN (6, 7, 8) THEN 'JJA'
                ELSE 'SON' END AS _s
              FROM hour_grid)
            GROUP BY 1
            """
        ).fetchall()
    )

    def _profile(s: str) -> dict[str, float]:
        pts = by_season[s]
        n = int(grid_season_hours.get(s, 0))
        if not pts:
            return {"offset_k": 0.0, "drift_k_per_day": 0.0, "n_hours": 0, "season_hours": n}
        resid = sorted(r for _, r in pts)
        mid = len(resid) // 2
        median = resid[mid] if len(resid) % 2 else (resid[mid - 1] + resid[mid]) / 2.0
        # ``d`` ist duckdb-DATE (``datetime.date``) → kein ``.date()``.
        x = [
            (d - _season_start(d.year, d.month).date()).days
            for d, _ in pts
        ]
        y = [r for _, r in pts]
        n_pts = len(x)
        mx = sum(x) / n_pts
        my = sum(y) / n_pts
        denom = sum((xi - mx) ** 2 for xi in x)
        slope = (
            sum((xi - mx) * (yi - my) for xi, yi in zip(x, y, strict=True)) / denom
            if denom > 0
            else 0.0
        )
        return {
            "offset_k": round(median, 4),
            "drift_k_per_day": round(slope, 6),
            "n_hours": n_pts,
            "season_hours": n,
        }

    valid = {s: len(by_season[s]) for s in config.BIAS_SEASONS}
    return BiasResult(
        djf=_profile("DJF"),
        mam=_profile("MAM"),
        jja=_profile("JJA"),
        son=_profile("SON"),
        n_hours_total=sum(valid.values()),
        grid_coverage={
            s: round(valid[s] / max(int(grid_season_hours.get(s, 0)), 1), 4)
            for s in config.BIAS_SEASONS
        },
    )


def save_profile(result: BiasResult, path: Path) -> Path:
    """Schreibt das Bias-Profil als JSON (``bias_profile.json``)."""
    payload = {
        "generated_by": "cmi-m1 bias module",
        "method": "seasonal median offset + OLS drift (K/day); sensor = dev0 a8 hour-mean, grid = Open-Meteo T2m (ERA5/ICON-D2)",
        "timezone": "Europe/Berlin",
        "n_hours_total": result.n_hours_total,
        "grid_coverage": result.grid_coverage,
        "profile": result.as_profile(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return path


def load_profile(path: Path) -> dict | None:
    """Lädt das Bias-Profil (None, falls Datei fehlt/ungültig)."""
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        prof = data.get("profile")
        return prof if isinstance(prof, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


__all__ = [
    "BiasResult",
    "prepare_con",
    "compute_bias",
    "save_profile",
    "load_profile",
]
