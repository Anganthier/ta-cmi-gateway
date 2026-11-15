"""Tests: Wetter-Backfill (Mocked-HTTP, Monats-Chunking, kein echtes Netzwerk).

Deckt:
- ``era5_month_range`` / ``hfa_month_range`` (Monats-Chunking inkl. Split);
- ``run_backfill`` mit Mock-``get_json``: alle Monate werden angefragt,
  Cache-Dateien entstehen, Stats zählen Requests/Cache-Skips korrekt;
- Fehlmonate (dauerhafter Fehler) werden dokumentiert (nicht stumm);
- Resume: zweiter Lauf → alle Monate cached (0 Requests);
- Retry: temporärer Fehler → Retries-Count;
- ``cache_summary`` (Übersicht over Cache-Verzeichnis).
"""
import calendar
import json

import pytest

from analysis.model.m1 import backfill, config
from analysis.model.m1.weather import FetchStats, cache_path, is_valid_cache


def _mock_get_json(url: str, state: dict | None = None, fail_month: str | None = None):
    """Mock: liefert einen validen Monats-Chunk basierend auf der URL."""
    if state is not None:
        state["calls"].append(url)
    if "start_date=" not in url:
        raise ValueError(f"unexpected url: {url}")
    start = url.split("start_date=")[1].split("&")[0]  # YYYY-MM-01
    ym = start[:7]
    year, month = int(ym[:4]), int(ym[5:7])
    if fail_month and ym == fail_month:
        raise RuntimeError(f"simulated failure for {ym}")
    last = calendar.monthrange(year, month)[1]
    times = [f"{year:04d}-{month:02d}-{d:02d}T{h:02d}:00"
             for d in range(1, last + 1) for h in range(24)]
    return {
        "latitude": 51.38,
        "longitude": 7.77,
        "hourly": {"time": times, "temperature_2m": [10.0] * len(times)},
        "daily": {"time": [f"{year:04d}-{month:02d}-{d:02d}" for d in range(1, last + 1)],
                  "sunrise": ["06:00"] * last, "sunset": ["18:00"] * last},
    }


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path / "wx_cache"


@pytest.fixture
def cfg(tmp_path, cache_dir) -> config.BuildConfig:
    return config.BuildConfig(wx_cache_dir=cache_dir, fetch_end="2026-10-31",
                              fair_use_sleep=0.0)


class TestMonthRanges:
    def test_era5_range(self):
        r = backfill.era5_month_range()
        assert r[0] == (2020, 8)
        assert r[-1] == (2022, 11)  # ERA5 trägt den Split-Monat 2022-11 (01..13)
        # 2020: Aug–Dez (5), 2021: 12, 2022: 11 → 28
        assert len(r) == 28
        assert r.count((2022, 11)) == 1

    def test_hfa_range(self):
        r = backfill.hfa_month_range("2026-10-31")
        assert r[0] == (2022, 11)  # HFC-Start-Monat (ICON-D2 ab 2022-11-14)
        assert r[-1] == (2026, 10)
        # 2022: 11,12 (2) + 2023–2025 (36) + 2026: Jan–Okt (10) = 48
        assert len(r) == 48

    def test_hfa_range_full_years(self):
        r = backfill.hfa_month_range("2023-05-31")
        assert r == [(2022, 11), (2022, 12), (2023, 1), (2023, 2), (2023, 3), (2023, 4), (2023, 5)]


class TestRunBackfill:
    def test_full_run_creates_cache(self, cfg, cache_dir):
        """Kompletter Backfill: ERA5 + HFA → alle Cache-Dateien vorhanden."""
        state = {"calls": []}
        stats = backfill.run_backfill(
            cfg=cfg, sleep_sec=0.0,
            url_for=lambda b, s, e, m, la, lo, hv, dv: f"mock?url=x&start_date={s}&end_date={e}",
            get_json=lambda url: _mock_get_json(url, state),
            log=lambda *a: None,
        )
        assert stats.failures == 0
        assert stats.missing_months == []
        # ERA5: 28 Dateien, HFA: 48 Dateien
        era5_files = list((cache_dir / "era5").glob("*.json"))
        hfa_files = list((cache_dir / "hfa").glob("*.json"))
        assert len(era5_files) == 28
        assert len(hfa_files) == 48
        # jede Datei valide
        assert all(is_valid_cache(f) for f in era5_files + hfa_files)
        # erste + letzte ERA5-Datei
        assert (cache_dir / "era5" / "2020-08.json").exists()
        assert (cache_dir / "era5" / "2022-11.json").exists()
        # erste + letzte HFA-Datei
        assert (cache_dir / "hfa" / "2022-11.json").exists()
        assert (cache_dir / "hfa" / "2026-10.json").exists()
        # URL-Bounds: Monats-Enddatum korrekt (31/30/29 Tage)
        urls = state["calls"]
        assert any("start_date=2020-08-01" in u and "end_date=2020-08-31" in u for u in urls)
        assert any("start_date=2022-11-01" in u and "end_date=2022-11-30" in u for u in urls)

    def test_resume_skips_cached(self, cfg, cache_dir):
        """Zweiter Lauf: alle Monate cached → 0 Requests, skipped_cached = alle."""
        backfill.run_backfill(cfg=cfg, sleep_sec=0.0,
                              get_json=lambda url: _mock_get_json(url),
                              log=lambda *a: None)
        n_total = len(backfill.era5_month_range()) + len(backfill.hfa_month_range(cfg.fetch_end))

        def no_calls(url):
            raise AssertionError("darf bei validem Cache nicht aufgerufen werden")

        stats2 = backfill.run_backfill(cfg=cfg, sleep_sec=0.0,
                                       get_json=no_calls,
                                       log=lambda *a: None)
        assert stats2.requests == 0
        assert stats2.skipped_cached == n_total
        assert stats2.failures == 0

    def test_missing_month_documented(self, cfg, cache_dir):
        """Dauerhafter Fehler bei 2021-05 → dokumentiert in missing_months."""
        stats = backfill.run_backfill(
            cfg=cfg, sleep_sec=0.0, force=True, backoff_base=0.0,
            get_json=lambda url: _mock_get_json(url, fail_month="2021-05"),
            log=lambda *a: None,
        )
        assert stats.failures >= 1
        assert any("2021-05" in m for m in stats.missing_months)
        # Cache-Datei für den Fehlmonat existiert nicht (Fehler → kein File)
        assert not is_valid_cache(cache_path(cache_dir, "era5", 2021, 5))
        # Andere Monate sind gecacht
        assert is_valid_cache(cache_path(cache_dir, "era5", 2021, 4))

    def test_retry_counts(self, cfg, cache_dir):
        """Temporärer Fehler (einmalig) → Retry, danach Erfolg."""
        state = {"n": 0, "calls": []}

        def get(url):
            state["calls"].append(url)
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("transient")
            return _mock_get_json(url)

        stats = backfill.run_backfill(cfg=cfg, sleep_sec=0.0, backoff_base=0.0,
                                      get_json=get, log=lambda *a: None,
                                      )
        # run_backfill hat kein backoff_base-Param → über fetch_month Default;
        # wir testen hier stattdessen die Count-Logik:
        assert stats.requests >= 1
        assert stats.retries >= 1
        assert stats.failures == 0

    def test_force_bypasses_cache(self, cfg, cache_dir):
        """--force: Cache wird ignoriert, neu geladen."""
        backfill.run_backfill(cfg=cfg, sleep_sec=0.0,
                              get_json=lambda url: _mock_get_json(url),
                              log=lambda *a: None)
        assert len(list((cache_dir / "era5").glob("*.json"))) > 0  # Cache befüllt
        state = {"n": 0}

        def get(url):
            state["n"] += 1
            return _mock_get_json(url)

        stats = backfill.run_backfill(cfg=cfg, force=True, sleep_sec=0.0,
                                      get_json=get, log=lambda *a: None)
        assert stats.requests > 0  # nichts wurde gecacht übersprungen
        assert stats.skipped_cached == 0

    def test_stats_as_dict(self):
        s = FetchStats(requests=1, retries=2, failures=3, skipped_cached=4,
                       missing_months=["2020-08 (era5: x)"])
        d = s.as_dict()
        assert d == {"requests": 1, "retries": 2, "failures": 3, "skipped_cached": 4,
                     "missing_months": ["2020-08 (era5: x)"]}


class TestCacheSummary:
    def test_cache_summary_empty(self, tmp_path):
        """Leeres (isoliertes) Cache-Verzeichnis → 0 Dateien."""
        s = backfill.cache_summary(cache_dir=tmp_path / "leer")
        assert set(s.keys()) == {"era5", "hfa"}
        assert s["era5"]["files"] == 0
        assert s["hfa"]["files"] == 0

    def test_cache_summary_mixed(self, tmp_path):
        """Mix: valide + invalide Datei → Summary trennt korrekt."""
        era5 = tmp_path / "wx_cache" / "era5"
        era5.mkdir(parents=True)
        # valide Datei (>= 200 Bytes + "hourly"-Key)
        p1 = era5 / "2020-08.json"
        p1.write_text(json.dumps({
            "hourly": {"time": [f"2020-08-0{d}T00:00" for d in range(1, 9)],
                        "temperature_2m": [1.0] * 8},
            "padding_for_min_size_200_bytes": "x" * 150,
        }), encoding="utf-8")
        # invalide (zu klein)
        p2 = era5 / "2020-09.json"
        p2.write_text("{}", encoding="utf-8")
        s = backfill.cache_summary(cache_dir=tmp_path / "wx_cache")
        assert s["era5"]["files"] == 2
        assert s["era5"]["valid"] == 1
        assert "2020-09.json" in s["era5"]["missing"]
