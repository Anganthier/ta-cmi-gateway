"""Tests: Open-Meteo-Client (Mocked-HTTP, keine echten Network-Calls).

Deckt: URL-Bau (UTC, Modelle, Variablen, Sun-Daily), Cache-Resume/Validität,
Retry mit Backoff (max 3), Fehlmonat-Handling (nicht stumm), Split-Flags
(wx_is_forecast / wx_source), Monats-Chunking (ERA5 vs HFA).
"""
import json
from datetime import UTC, datetime

from analysis.model.m1 import weather
from analysis.model.m1.config import WX_HOURLY_VARS


def _mock_data(year=2022, month=11):
    return {
        "latitude": 51.38,
        "longitude": 7.77,
        "hourly_units": {"time": "iso8601"},
        "hourly": {
            "time": [f"2022-11-01T{i:02d}:00" for i in range(24)],
            "temperature_2m": [float(i) for i in range(24)],
        },
        "daily": {"time": ["2022-11-01"], "sunrise": ["2022-11-01T07:00"], "sunset": ["2022-11-01T17:00"]},
        "utc_offset_seconds": 0,
    }


class TestBuildUrl:
    def test_url_era5_utc(self):
        url = weather.build_url(
            "https://archive-api.open-meteo.com/v1/archive",
            "2020-08-01",
            "2020-08-31",
            "era5",
            51.382974,
            7.774286,
            ["temperature_2m"],
        )
        assert url.startswith("https://archive-api.open-meteo.com/v1/archive?")
        assert "start_date=2020-08-01" in url
        assert "end_date=2020-08-31" in url
        assert "models=era5" in url
        assert "timezone=UTC" in url
        assert "hourly=temperature_2m" in url
        assert "daily" not in url

    def test_url_with_daily_sun(self):
        url = weather.build_url(
            "https://archive-api.open-meteo.com/v1/archive",
            "2020-08-01",
            "2020-08-31",
            "era5",
            51.38,
            7.77,
            ["temperature_2m"],
            daily_vars=["sunrise", "sunset"],
        )
        assert "daily=sunrise%2Csunset" in url

    def test_url_full_var_set(self):
        url = weather.build_url(
            "https://x", "2020-08-01", "2020-08-31", "era5", 1.0, 2.0, WX_HOURLY_VARS
        )
        for var in WX_HOURLY_VARS:
            assert var in url


class TestCache:
    def test_cache_path_layout(self, tmp_path):
        p = weather.cache_path(tmp_path, "era5", 2020, 8)
        assert p == tmp_path / "era5" / "2020-08.json"

    def test_valid_cache_roundtrip(self, tmp_path):
        p = weather.cache_path(tmp_path, "hfa", 2022, 11)
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(_mock_data()), encoding="utf-8")
        assert weather.is_valid_cache(p) is True
        with p.open() as f:
            data = json.load(f)
        assert data["hourly"]["time"][0] == "2022-11-01T00:00"

    def test_invalid_cache_tiny(self, tmp_path):
        p = weather.cache_path(tmp_path, "hfa", 2022, 11)
        p.parent.mkdir(parents=True)
        p.write_text("{}", encoding="utf-8")  # zu klein + kein hourly
        assert weather.is_valid_cache(p) is False

    def test_invalid_cache_corrupt(self, tmp_path):
        p = weather.cache_path(tmp_path, "hfa", 2022, 11)
        p.parent.mkdir(parents=True)
        p.write_text("{not json", encoding="utf-8")
        assert weather.is_valid_cache(p) is False

    def test_missing_cache(self, tmp_path):
        assert weather.is_valid_cache(tmp_path / "hfa" / "2022-11.json") is False


class TestFetchMonth:
    def test_success_writes_cache(self, tmp_path):
        calls = []

        def get(url):
            calls.append(url)
            return _mock_data()

        stats = weather.FetchStats()
        data = weather.fetch_month(
            base="https://archive-api.open-meteo.com/v1/archive",
            api_name="era5",
            year=2020,
            month=8,
            model="era5",
            cache_dir=tmp_path,
            stats=stats,
            sleep_sec=0,
            get_json=get,
        )
        assert data is not None
        assert data["hourly"]["time"][0] == "2022-11-01T00:00"
        assert stats.requests == 1
        assert weather.cache_path(tmp_path, "era5", 2020, 8).exists()

    def test_cached_skip_no_request(self, tmp_path):
        p = weather.cache_path(tmp_path, "era5", 2020, 8)
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(_mock_data()), encoding="utf-8")
        stats = weather.FetchStats()

        def get(url):
            raise AssertionError("darf bei validem Cache nicht aufgerufen werden")

        data = weather.fetch_month(
            base="b",
            api_name="era5",
            year=2020,
            month=8,
            model="era5",
            cache_dir=tmp_path,
            stats=stats,
            get_json=get,
        )
        assert data is not None
        assert stats.skipped_cached == 1
        assert stats.requests == 0

    def test_retry_then_success(self, tmp_path):
        state = {"n": 0}

        def get(url):
            state["n"] += 1
            if state["n"] < 3:
                raise RuntimeError("boom")
            return _mock_data()

        stats = weather.FetchStats()
        data = weather.fetch_month(
            base="b",
            api_name="era5",
            year=2020,
            month=8,
            model="era5",
            cache_dir=tmp_path,
            stats=stats,
            sleep_sec=0,
            backoff_base=0.0,
            get_json=get,
        )
        assert data is not None
        assert stats.retries == 2
        assert stats.failures == 0

    def test_failure_max_retries_documents_missing(self, tmp_path):
        def get(url):
            raise RuntimeError("permanent")

        stats = weather.FetchStats()
        data = weather.fetch_month(
            base="b",
            api_name="hfa",
            year=2022,
            month=11,
            model="icon_d2",
            cache_dir=tmp_path,
            stats=stats,
            sleep_sec=0,
            backoff_base=0.0,
            get_json=get,
        )
        assert data is None
        assert stats.failures == 1
        assert len(stats.missing_months) == 1
        assert "2022-11" in stats.missing_months[0]  # dokumentiert, nicht stumm
        assert stats.requests == 4  # 1 + 3 Retries

    def test_error_response_retried(self, tmp_path):
        state = {"n": 0}

        def get(url):
            state["n"] += 1
            if state["n"] == 1:
                return {"error": True, "reason": "bad"}
            return _mock_data()

        stats = weather.FetchStats()
        data = weather.fetch_month(
            base="b",
            api_name="era5",
            year=2020,
            month=8,
            model="era5",
            cache_dir=tmp_path,
            stats=stats,
            sleep_sec=0,
            backoff_base=0.0,
            get_json=get,
        )
        assert data is not None
        assert stats.retries == 1


class TestMonthsAndFlags:
    def test_month_range(self):
        from analysis.model.m1.config import month_range

        assert month_range("2020-08-01", "2020-10-31") == [(2020, 8), (2020, 9), (2020, 10)]
        assert month_range("2020-08-01", "2022-11-13")[-1] == (2022, 11)
        assert month_range("2022-11-01", "2022-11-14") == [(2022, 11)]

    def test_utc_hour_parse(self):
        assert weather.utc_hour("2022-11-01T00:00") == datetime(2022, 11, 1, tzinfo=UTC)

    def test_hours_list(self):
        data = _mock_data()
        hours = weather.utc_hours_list(data)
        assert len(hours) == 24
        assert hours[0].tzinfo is not None

    def test_utc_to_berlin_minute(self):
        # 2024-06-21 00:00 UTC = 02:00 CEST → 120 min
        h = datetime(2024, 6, 21, 0, 0, tzinfo=UTC)
        assert weather.utc_to_berlin_minute(h) == 120
        # 2020-01-15 00:00 UTC = 01:00 CET → 60 min
        assert weather.utc_to_berlin_minute(datetime(2020, 1, 15, 0, 0, tzinfo=UTC)) == 60

    def test_wx_is_forecast_split(self):
        assert weather.wx_is_forecast(datetime(2022, 11, 13, 23, 0, tzinfo=UTC)) == 0
        assert weather.wx_is_forecast(datetime(2022, 11, 14, 0, 0, tzinfo=UTC)) == 1
        assert weather.wx_is_forecast(datetime(2026, 1, 1, tzinfo=UTC)) == 1
        assert weather.wx_source(datetime(2020, 8, 1, tzinfo=UTC)) == "era5"
        assert weather.wx_source(datetime(2023, 1, 1, tzinfo=UTC)) == "icon_d2"

    def test_hours_in_month(self):
        assert weather.hours_in_month(_mock_data()) == 24
        assert weather.hours_in_month(None) == 0
        assert weather.hours_in_month({}) == 0
