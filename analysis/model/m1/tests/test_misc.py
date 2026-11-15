"""Tests: kleine Einzelfunktionen (calendar_utils-Fallbacks, lags-Edge, config).

Bedeckt die Rand-Zweige:
- ``calendar_utils.sun_features`` (polarexrem-Fallback set ≤ rise → -1),
- ``calendar_utils.first_full_hour`` / ``day_is_dst``,
- ``lags.lag_index`` (Rand),
- ``config.col_for`` / ``dev_col`` / ``analog_raw_col`` (Kanal-Maps),
- m1.1-Konstanten (DATA_VERSION, RAS_INSTALL_TS, RAS_RAW_COLUMNS, LAST_VALUE_FEATURES),
- m1.2-Konstanten (SENTINEL_MAX/MIN, DEV2_HZRKL_ON, hzrkl-Kanal-Mapping a13/a14/a15),
- ``config.con()`` (frische In-Memory-Verbindung).
"""
from datetime import UTC, date, datetime

import pytest

from analysis.model.m1 import calendar_utils as cu
from analysis.model.m1 import config, lags
from analysis.model.m1.calendar_utils import BERLIN


class TestCalendarEdge:
    def test_sun_features_normal(self):
        f = cu.sun_features(date(2024, 6, 21))
        assert f["sunrise_min"] > 0
        assert f["sunset_min"] > f["sunrise_min"]
        assert f["daylight_min"] == f["sunset_min"] - f["sunrise_min"]

    def test_sun_features_invalid(self, monkeypatch):
        """Sunrise > Sunset (simuliertes Extrem) → -1-Fallback."""
        st = cu.SunTimes(date(2024, 6, 1),
                         datetime(2024, 6, 1, 20, 0, tzinfo=BERLIN),
                         datetime(2024, 6, 1, 6, 0, tzinfo=BERLIN))
        monkeypatch.setattr(cu, "sun_times", lambda d: st)
        f = cu.sun_features(date(2024, 6, 1))
        assert f == {"sunrise_min": -1, "sunset_min": -1, "daylight_min": -1}

    def test_sun_features_none(self, monkeypatch):
        st = cu.SunTimes(date(2024, 6, 1), None, None)
        monkeypatch.setattr(cu, "sun_times", lambda d: st)
        f = cu.sun_features(date(2024, 6, 1))
        assert f["sunrise_min"] == -1

    def test_noaa_sun_none(self, monkeypatch):
        """noaa_sun_utc liefert None (Polar/extrem) → sun_times → None."""
        monkeypatch.setattr(cu, "noaa_sun_utc", lambda d, lat, lon: None)
        st = cu.sun_times(date(2024, 6, 1))
        assert st.sunrise is None and st.sunset is None

    def test_is_dst_aware_and_naive(self):
        # aware UTC → Berlin (Sommerzeit)
        assert cu.is_dst(datetime(2024, 6, 15, 12, 0, tzinfo=UTC)) is True
        assert cu.is_dst(datetime(2024, 1, 15, 12, 0, tzinfo=UTC)) is False
        # naive → implizit Berlin
        assert cu.is_dst(datetime(2024, 6, 15, 12, 0)) is True
        assert cu.is_dst(datetime(2024, 1, 15, 12, 0)) is False

    def test_day_is_dst_transitions(self):
        assert cu.day_is_dst(date(2024, 3, 30)) is False  # noch Winterzeit
        assert cu.day_is_dst(date(2024, 3, 31)) is True   # umgestellt (12:00)
        assert cu.day_is_dst(date(2024, 10, 26)) is True
        assert cu.day_is_dst(date(2024, 10, 27)) is False

    def test_first_full_hour(self):
        assert cu.first_full_hour(date(2020, 8, 31)) == datetime(2020, 8, 31, tzinfo=BERLIN)
        assert cu.first_full_hour(date(2024, 12, 25)).hour == 0


class TestLagsEdge:
    def test_lag_index(self):
        # n_hours=10, i=9 (letzter), k=3 → idx = 10-1-3 = 6
        assert lags.lag_index(10, 3) == 6
        # k=0 → letzter Index
        assert lags.lag_index(5, 0) == 4

    def test_lag_index_negative(self):
        # n=2, k=5 → 2-1-5 = -4 (kein Lag vorhanden)
        assert lags.lag_index(2, 5) == -4

    def test_cross_check_empty(self):
        assert lags.cross_check_raster([]) is True

    def test_cross_check_single(self):
        h = [datetime(2024, 1, 1, tzinfo=BERLIN)]
        assert lags.cross_check_raster(h) is True

    def test_first_full_hour_index_empty(self):
        assert lags.first_full_hour_index([], datetime(2024, 1, 1, tzinfo=BERLIN)) == -1


class TestConfig:
    def test_dev_col(self):
        assert config.dev_col(0, "analog", 8) == "dev0_analog_08"
        assert config.dev_col(2, "digital", 1) == "dev2_digital_01"
        assert config.dev_col(1, "analog", 13) == "dev1_analog_13"

    def test_col_for_named(self):
        assert config.col_for(0, "analog", 8) == "ext"
        assert config.col_for(0, "digital", 5) == "oel_burner"
        assert config.col_for(1, "analog", 1) == "ras_kell_perc"
        assert config.col_for(2, "analog", 16) == "solar_coll"

    def test_col_for_unnamed(self):
        assert config.col_for(0, "analog", 11) is None
        assert config.col_for(1, "digital", 5) is None
        assert config.col_for(3, "analog", 1) is None  # ungültiges Device

    def test_con_fresh(self):
        c = config.con()
        try:
            assert c.execute("SELECT 1").fetchone()[0] == 1
        finally:
            c.close()


class TestConfigM11:
    def test_data_version_and_install_ts(self):
        assert config.DATA_VERSION == "m1.3"
        assert config.RAS_INSTALL_TS == "2021-10-20 17:00:00"
        # gleiches Format wie Roh-ts (String-Vergleich in der src-View)
        assert datetime.strptime(config.RAS_INSTALL_TS, "%Y-%m-%d %H:%M:%S")

    def test_ras_raw_columns(self):
        assert config.RAS_RAW_COLUMNS == [
            "dev1_analog_01", "dev1_analog_02", "dev1_analog_04", "dev1_analog_05",
            "dev1_analog_07", "dev1_analog_08", "dev1_analog_11", "dev1_analog_12",
            "dev1_analog_13",
            "dev2_analog_04", "dev2_analog_05", "dev2_analog_06", "dev2_analog_07",
            "dev2_analog_08", "dev2_analog_09",
        ]

    def test_ras_age_mapping_and_deprecated_heat_min(self):
        assert config.col_for(1, "analog", 11) == "ras_age_kell"
        assert config.col_for(1, "analog", 13) == "ras_age_dg"
        assert [f for _ch, f in config.HEAT_MIN_DEPRECATED] == [
            "heat_min_kell", "heat_min_eg", "heat_min_dg"]

    def test_analog_raw_col(self):
        assert config.analog_raw_col("ext") == "dev0_analog_08"
        assert config.analog_raw_col("ras_dg_ist") == "dev1_analog_08"
        assert config.analog_raw_col("solar_coll") == "dev2_analog_16"
        assert [config.analog_raw_col(f) for f in config.LAST_VALUE_FEATURES]
        with pytest.raises(KeyError):
            config.analog_raw_col("pump_hz")


class TestConfigM12:
    def test_sentinel_constants(self):
        assert config.SENTINEL_MAX == 3276.0
        assert config.SENTINEL_MIN == -999.0
        assert config.SENTINEL_MAX <= 3276.7  # TA-Fühlerbruch-Kennung
        assert config.SENTINEL_MIN >= -999.9  # TA-Kurzschluss-Kennung

    def test_hzrkl_mapping(self):
        assert config.DEV2_HZRKL_ON == {10: "hzrkl_kell", 11: "hzrkl_eg", 12: "hzrkl_dg"}
        assert config.HZRKL_ON_SCALE == 10.0
        assert config.HZRKL_ZONES == ["kell", "eg", "dg"]
        # Reihenfolge laut UVR610-Setup: a13 Keller, a14 OG (→ dg), a15 EG
        assert config.col_for(2, "analog", 13) == "hzrkl_kell_pos"
        assert config.col_for(2, "analog", 14) == "hzrkl_dg_pos"
        assert config.col_for(2, "analog", 15) == "hzrkl_eg_pos"
        assert config.analog_raw_col("hzrkl_dg_pos") == "dev2_analog_14"
        assert config.analog_raw_col("hzrkl_eg_pos") == "dev2_analog_15"
        # a10–a12 nicht als Roh-AVG unter dem Basisnamen (nur _on/_last)
        assert config.col_for(2, "analog", 10) is None
        assert len(config.LAST_VALUE_FEATURES) == 20
        assert config.LAST_VALUE_FEATURES[-3:] == ["hzrkl_kell_pos", "hzrkl_eg_pos", "hzrkl_dg_pos"]
