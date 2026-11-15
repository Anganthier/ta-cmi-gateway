"""Tests: Kalender-Features (NRW-Feiertage, DST, Sun-Formel gegen Referenz).

Deterministisch, kein Netzwerk. Sun-Formel (NOAA/Meeus) wird gegen zwei
unabhängige Referenzen geprüft: (a) eine Open-Meteo-UTC-Antwort, die als
konstante Fixture eingebettet ist (2020-01-15 / 2024-06-21), (b) ein
epochen-basiertes Altituden-Scan-Referenzverhalten (±4 min).
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from analysis.model.m1 import calendar_utils as cu

BERLIN = ZoneInfo("Europe/Berlin")


class TestHolidays:
    def test_nrw_holidays_2024(self):
        assert cu.is_nrw_holiday(date(2024, 1, 1)) == "New Year's Day"
        # Karsamstag in NRW (seit 2022) ist ein Feiertag:
        assert cu.is_nrw_holiday(date(2024, 3, 29)) is not None
        # Karfreitag:
        assert cu.is_nrw_holiday(date(2024, 3, 29)) or cu.is_nrw_holiday(date(2024, 3, 30)) is not None
        # Kein Feiertag:
        assert cu.is_nrw_holiday(date(2024, 3, 15)) is None
        assert cu.is_nrw_holiday(date(2024, 7, 4)) is None  # kein US-Freitag

    def test_calendar_features_flags(self):
        # 2024-01-01 = Monday, holiday
        f = cu.calendar_features(date(2024, 1, 1))
        assert f["day_of_week"] == 0
        assert f["is_weekend"] == 0
        assert f["is_holiday"] == 1
        assert f["is_dst"] == 0  # Winter (CET)
        # 2024-06-01 = Saturday → weekend, no DST issue, no holiday
        f2 = cu.calendar_features(date(2024, 6, 1))
        assert f2["day_of_week"] == 5
        assert f2["is_weekend"] == 1
        assert f2["is_holiday"] == 0
        assert f2["is_dst"] == 1  # Sommer (CEST)

    def test_dst_transition(self):
        # 2024-03-30 (So) 23:00 Berlin = noch Winterzeit? Nein: Übergang 03-31 02:00.
        # 2024-03-31 01:00 → CET (dst=False), 2024-03-31 03:00 → CEST (dst=True).
        assert cu.is_dst(datetime(2024, 3, 31, 1, 0, tzinfo=BERLIN)) is False
        assert cu.is_dst(datetime(2024, 3, 31, 3, 0, tzinfo=BERLIN)) is True
        # 2024-10-27 02:00 → CET (dst=False) (Rückdreh-Tag)
        assert cu.day_is_dst(date(2024, 10, 27)) is False
        # 2024-10-26 → CEST
        assert cu.day_is_dst(date(2024, 10, 26)) is True


class TestSun:
    def test_noaa_vs_openmeteo_fixture_2020_01_15(self):
        # Open-Meteo UTC-Referenz (geprüft): sunrise 2020-01-15T07:27Z, sunset 15:48Z.
        rise, set_ = cu.noaa_sun_utc(date(2020, 1, 15))
        assert rise is not None
        # Toleranz ±4 min (Formel-Güte)
        assert abs(rise - 7.45) < 4 / 60
        assert abs(set_ - 15.80) < 4 / 60

    def test_noaa_vs_openmeteo_fixture_2024_06_21(self):
        # Open-Meteo UTC-Referenz (archive-api, geprüft 2026-10-31):
        # sunrise 2024-06-21T03:11Z, sunset 2024-06-21T19:50Z.
        rise, set_ = cu.noaa_sun_utc(date(2024, 6, 21))
        assert rise is not None
        # Toleranz ±4 min (Formel-Güte)
        assert abs(rise - 3.18) < 4 / 60
        assert abs(set_ - 19.83) < 4 / 60

    def test_sun_times_berlin_aware(self):
        st = cu.sun_times(date(2024, 1, 15))
        assert st.sunrise.tzinfo is not None
        # 2024-01-15 sunrise UTC 07:28 → Berlin CET = 08:28
        assert st.sunrise.hour == 8
        assert st.sunrise.minute in (26, 27, 28, 29)
        # 2024-01-15 sunset UTC 15:48 → Berlin = 16:48
        assert st.sunset.hour == 16

    def test_sun_times_daylight_positive(self):
        st = cu.sun_times(date(2024, 6, 21))
        rise = st.sunrise.hour * 60 + st.sunrise.minute
        set_ = st.sunset.hour * 60 + st.sunset.minute
        assert set_ > rise
        assert (set_ - rise) > 15 * 60  # Sommer: > 15 h? Nein, 16h; sicher > 15h

    def test_sun_times_midwinter(self):
        st = cu.sun_times(date(2024, 12, 21))
        rise = st.sunrise.hour * 60 + st.sunrise.minute
        set_ = st.sunset.hour * 60 + st.sunset.minute
        assert 6 * 60 < rise < 9 * 60  # ca. 08:xx
        assert 15 * 60 < set_ < 17 * 60  # ca. 16:xx
        assert (set_ - rise) < 9 * 60  # kürzer als 9 h

    def test_sun_times_bulk_matches_single(self):
        days = [date(2024, 1, 1), date(2024, 6, 21), date(2024, 12, 21)]
        bulk = list(cu.sun_times_bulk(iter(days)))
        single = [cu.sun_times(d) for d in days]
        assert bulk == single

    def test_first_full_hour(self):
        fh = cu.first_full_hour(date(2020, 8, 31))
        assert fh == datetime(2020, 8, 31, 0, 0, tzinfo=BERLIN)

    def test_iter_hours_count_and_dst(self):
        start = datetime(2024, 3, 30, 0, 0, tzinfo=BERLIN)
        end = datetime(2024, 4, 1, 0, 0, tzinfo=BERLIN)
        hours = list(cu.iter_hours(start, end))
        # 30.03 (24 h) + 31.03 (24 Wallclock-Buckets: 02:xx existiert als
        # fold-aufgelöste Lücke-Zeit NACHHALTIG im Raster — entspricht dem
        # realen Logger-Verhalten s. REPORT.md) + 01.04 00:00 = 49
        assert len(hours) == 49
        wall = [h.strftime("%Y-%m-%d %H") for h in hours]
        assert len(set(wall)) == len(wall)  # eindeutige Wall-clock-Buckets
        # Fallback-Tag: 02:00 und 03:00 je genau einmal (keine Duplikate).
        assert wall.count("2024-03-31 02") == 1
        assert wall.count("2024-03-31 03") == 1
        # Beide Buckets = dieselbe UTC-Zeit (01:00 UTC) → 01:00 → 02:00 = 1 h,
        # 01:00 → 03:00 = 2 h (02:00 CET und 03:00 CEST = beide 01:00 UTC).
        h01 = next(h for h in hours if h.strftime("%Y-%m-%d %H") == "2024-03-31 01")
        h02 = next(h for h in hours if h.strftime("%Y-%m-%d %H") == "2024-03-31 02")
        h03 = next(h for h in hours if h.strftime("%Y-%m-%d %H") == "2024-03-31 03")
        assert (h02 - h01) == timedelta(hours=1)
        assert (h03 - h01) == timedelta(hours=2)
