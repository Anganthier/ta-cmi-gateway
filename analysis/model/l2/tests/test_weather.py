"""Wetter: LIVE-Forecast (Slice/Parse/Cache/Bias) ohne Netzwerk."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from analysis.model.l2 import weather
from analysis.model.l2.tests.conftest import make_api_response, make_wx

# M1-Wetter-Join-Semantik: ``wx_t{k}`` = Grid-Stunde ``ts_utc + k h`` (naive
# UTC-Arithmetik, keine AT TIME ZONE). Fenster 11:00 Berlin (10:00 UTC) →
# 10:00–09:00 UTC, d. h. Stunden 10 … 23 des Tages + Stunde 0 des Folgetags.
# (Die API liefert nur 0:00–23:00 pro Tag → exakt 24 h ab ts_utc sind
# 11:00–23:00 + Folgetag 0:00–8:00, s. ``test_slice_window_day1``.)


def _ts() -> datetime:
    return weather.berlin_hour("2026-11-01 11:00")


# --- Berlin-Stunden / Window ---


def test_berlin_hour_naive_and_minutes() -> None:
    """Naive String → volle Berliner Stunde (Minute abgerundet)."""
    b = weather.berlin_hour("2026-11-01 11:37")
    assert b.hour == 11 and b.minute == 0
    assert weather.berlin_hour("2026-11-01 11:00").tzinfo is not None


def test_window_utc_24h() -> None:
    """Fenster = [ts_utc .. ts_utc+23] (M1-Join: Grid-Stunde + k h, UTC)."""
    hours = weather.window_utc(_ts())
    assert len(hours) == 24
    assert hours[0] == datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
    assert hours[-1] == datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def test_window_dst_berlin() -> None:
    """Sommerzeit: 01:30 UTC = 03:30 Berlin (CEST) → Stunde 3."""
    h = weather.utc_to_berlin_hour(datetime(2026, 7, 1, 1, 30, tzinfo=UTC))
    assert h == 3
    h_winter = weather.utc_to_berlin_hour(datetime(2026, 1, 1, 1, 30, tzinfo=UTC))
    assert h_winter == 2


def test_window_days_two_days() -> None:
    """API-Tagesfenster (start_date/end_date) für [ts_utc .. ts_utc+23]."""
    # 11:00 Berlin = 10:00 UTC → 10:00–09:00 UTC raut über Mitternacht:
    assert weather.window_days(weather.berlin_hour("2026-11-01 11:00")) == (
        "2026-11-01", "2026-11-02")
    # 20:00 Berlin = 19:00 UTC → 19:00–18:00 UTC raut ebenfalls über Mitternacht:
    assert weather.window_days(weather.berlin_hour("2026-11-01 20:00")) == (
        "2026-11-01", "2026-11-02")


# --- Slice (Tages-Fenster → exakt 24 h) ---


def test_slice_window_day1() -> None:
    """Fenster 10:00–09:00 UTC (11:00–23:00 + Folgetag 0:00–08:00) aus 2-Tage-API-Antwort."""
    data = make_api_response(_ts(), n=96)
    sliced = weather.slice_window(data, _ts())
    assert len(sliced["hourly"]["time"]) == 24
    assert sliced["hourly"]["time"][0] == "2026-11-01T10:00"
    assert sliced["hourly"]["time"][23] == "2026-11-02T09:00"
    assert len(sliced["hourly"]["temperature_2m"]) == 24


def test_slice_window_crosses_midnight() -> None:
    """ts 20:00 Berlin = 19:00 UTC → Fenster 19:00–18:00 UTC (über Mitternacht)."""
    ts = weather.berlin_hour("2026-11-01 20:00")  # 19:00 UTC
    data = make_api_response(ts, n=96)
    sliced = weather.slice_window(data, ts)
    assert len(sliced["hourly"]["time"]) == 24
    assert sliced["hourly"]["time"][0] == "2026-11-01T19:00"
    assert sliced["hourly"]["time"][-1] == "2026-11-02T18:00"


def test_slice_window_missing_hour_raises() -> None:
    """Fehlende Stunde im API-Fenster → ValueError (nicht still)."""
    ts = weather.berlin_hour("2026-11-01 11:00")
    data = make_api_response(ts, n=96)
    del data["hourly"]["time"][20]
    for var in list(data["hourly"]):
        if var != "time":
            del data["hourly"][var][20]
    import pytest

    with pytest.raises(ValueError, match="nicht ab"):
        weather.slice_window(data, ts)


def test_window_complete_false_on_bad_range() -> None:
    data = make_api_response(_ts(), n=96)
    data["hourly"]["time"] = data["hourly"]["time"][:23]
    for var in data["hourly"]:
        if var != "time":
            data["hourly"][var] = data["hourly"][var][:23]
    assert weather.window_complete(data, _ts()) is False


# --- Run / Parse ---


def test_parse_response_run_and_age() -> None:
    data = weather.slice_window(make_api_response(_ts(), n=96), _ts())
    now = datetime(2026, 11, 1, 11, 30, tzinfo=UTC)
    wx = weather.parse_response(data, _ts(), bias_profile=None,
                                fetched_at=now - timedelta(hours=0.5), now=now)
    assert wx.wx_age_h == 0.5
    assert wx.wx_run == "2026-11-01 11:00 UTC"  # Run-Proxy = Request-Zeit
    assert wx.utc_hour[0] == datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


def test_parse_response_missing_var_raises() -> None:
    import pytest

    data = weather.slice_window(make_api_response(_ts(), n=96), _ts())
    del data["hourly"]["weather_code"]
    with pytest.raises(ValueError, match="wmo|weather_code"):
        weather.parse_response(data, _ts())


# --- Bias-Korrektur (M1-Profil) ---


def test_bias_correct_son() -> None:
    """SON-Profil: Offset 0,9836 K + Drift (Tag 11 des 1.11. → 10 Tage)."""
    profile = {
        "profile": {
            "SON": {"offset_k": 1.0, "drift_k_per_day": 0.1},
            "DJF": {"offset_k": 1.1, "drift_k_per_day": 0.0},
        }
    }
    out = weather.bias_correct(_ts(), [0.0, None, 5.0], profile)
    days = (datetime(2026, 11, 1).date() - datetime(2026, 9, 1).date()).days
    assert out[0] == 1.0 + 0.1 * days
    assert out[1] is None
    assert out[2] == 5.0 + 1.0 + 0.1 * days


def test_bias_correct_none_profile_passthrough() -> None:
    out = weather.bias_correct(_ts(), [1.0, 2.0], None)
    assert out == [1.0, 2.0]


def test_load_bias_profile_missing() -> None:
    assert weather.load_bias_profile(Path("/nonexistent/bias.json")) is None


# --- Cache + fetch (Test-Doppel, kein HTTP) ---


def test_fetch_live_fetches_and_caches(tmp_path: Path) -> None:
    calls: list[str] = []

    def get_json(url: str) -> dict:
        calls.append(url)
        return make_api_response(_ts(), n=96)

    wx = weather.fetch_live("2026-11-01 11:00", runs_dir=tmp_path, get_json=get_json, retries=0,
                            clock=lambda: datetime(2026, 11, 1, 11, 5, tzinfo=UTC))
    assert wx is not None and len(wx.utc_hour) == 24
    p = weather.cache_path(_ts(), tmp_path)
    assert p.exists() and p.name == "wx_live_2026-11-01.json"
    assert calls and "models=icon_d2" in calls[0] and "timezone=UTC" in calls[0]
    # Zweiter Aufruf: Cache-Treffer (kein zweiter HTTP-Call).
    wx2 = weather.fetch_live("2026-11-01 11:00", runs_dir=tmp_path, get_json=get_json, retries=0,
                             clock=lambda: datetime(2026, 11, 1, 11, 5, tzinfo=UTC))
    assert wx2 is not None and len(calls) == 1


def test_fetch_live_fallback_on_error(tmp_path: Path) -> None:
    def get_json(url: str) -> dict:
        raise ConnectionError("kein Netz")

    wx = weather.fetch_live("2026-11-01 11:00", runs_dir=tmp_path, get_json=get_json, retries=0,
                            backoff_base=0.0, clock=lambda: datetime(2026, 11, 1, 11, 5, tzinfo=UTC))
    assert wx is None  # → Engine: Fallback + stale


def test_fetch_live_invalid_response_not_cached(tmp_path: Path) -> None:
    def get_json(url: str) -> dict:
        return make_api_response(_ts(), n=96) | {"hourly": {"time": make_api_response(_ts())["hourly"]["time"][:10]}}

    wx = weather.fetch_live("2026-11-01 11:00", runs_dir=tmp_path, get_json=get_json, retries=0,
                            backoff_base=0.0, clock=lambda: datetime(2026, 11, 1, 11, 5, tzinfo=UTC))
    assert wx is None
    assert not weather.cache_path(_ts(), tmp_path).exists()


def test_fetch_live_stale_cache_hit_age(tmp_path: Path) -> None:
    """Cache-Datei mit altem MTIME → Run-Proxy = MTIME → Alter > 2 h (Stale-Auslöser)."""
    p = weather.cache_path(_ts(), tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(make_api_response(_ts(), n=96)))
    old = datetime(2026, 11, 1, 7, 0, tzinfo=UTC).timestamp()
    os.utime(p, (old, old))
    now = datetime(2026, 11, 1, 11, 30, tzinfo=UTC)
    wx = weather.fetch_live("2026-11-01 11:00", runs_dir=tmp_path,
                            clock=lambda: now)
    assert wx is not None
    assert wx.wx_age_h > 4.0


# --- Aggregat-Helfer (Frost/Solar) ---


def test_wx_min_solar_frost() -> None:
    wx = make_wx("2026-11-01 11:00", t_values=[5.0, -1.0, 0.0] + [5.0] * 21, swr=200.0)
    assert weather.wx_min_12h(wx) == -1.0
    assert weather.wx_solar_12h(wx) == 200.0 * 12
    assert weather.wx_frost_night(wx) is True
    wx_mild = make_wx("2026-11-01 11:00", t_values=[5.0] * 24, swr=0.0)
    assert weather.wx_frost_night(wx_mild) is False
    assert weather.wx_solar_12h(wx_mild) == 0.0


def test_wx_as_dict_serializable() -> None:
    d = weather.wx_as_dict(make_wx("2026-11-01 11:00"))
    json.dumps(d)  # muss serialisierbar sein (kontext-Blöcke)
    assert d["wx_quelle"] == "Open-Meteo ICON-D2 (live)"
    assert len(d["window_utc"]) == 2
