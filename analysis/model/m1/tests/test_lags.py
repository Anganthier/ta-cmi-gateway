"""Tests: Lag-Berechnung (Monats-/Zeitzonen-Grenzen, DST, Randfälle).

Lags arbeiten auf dem Stunden-Raster (Index-Schritte), nie auf Wall-clock-
Arithmetic → DST-sicher. Unit-Tests ohne Netzwerk.
"""
from datetime import datetime

import pytest

from analysis.model.m1 import lags
from analysis.model.m1.calendar_utils import BERLIN, iter_hours


def _hours(day1, day2=None):
    start = datetime(*day1, tzinfo=BERLIN)
    end = datetime(*day2, tzinfo=BERLIN) if day2 else start
    return list(iter_hours(start, end))


class TestLagArrays:
    def test_basic_lag1(self):
        vals = [0.0, 1.0, 2.0, 3.0, 4.0]
        out = lags.build_lag_arrays(vals)
        assert len(out) == 5  # LAG_HOURS = 1,2,6,12,24
        assert out[0] == [None, 0.0, 1.0, 2.0, 3.0]

    def test_lag_sizes(self):
        vals = list(range(40))
        out = lags.build_lag_arrays(vals)
        for k, arr in zip(lags.LAG_HOURS, out, strict=True):
            assert len(arr) == len(vals)
            assert all(v is None for v in arr[:k])
            # arr[k+i] = values[i] → nach dem Shift: arr[k:][i] == i
            assert all(v == i for i, v in enumerate(arr[k:]))

    def test_lag24_needs_24(self):
        vals = [float(i) for i in range(24)]
        out = lags.build_lag_arrays(vals)
        lag24 = out[lags.LAG_HOURS.index(24)]
        assert all(v is None for v in lag24)
        vals2 = [float(i) for i in range(25)]
        out2 = lags.build_lag_arrays(vals2)
        assert out2[lags.LAG_HOURS.index(24)][24] == 0.0

    def test_lag_with_none_values(self):
        vals = [1.0, None, 3.0, 4.0]
        out = lags.build_lag_arrays(vals)
        assert out[0][1] == 1.0
        assert out[0][2] is None  # None propagiert (Index-Semantik)
        assert out[0][3] == 3.0

    def test_lag_series_mismatch_raises(self):
        hours = _hours((2024, 1, 1, 0), (2024, 1, 1, 2))
        with pytest.raises(ValueError):
            lags.lag_series(hours, [1.0, 2.0], 1)

    def test_lag_series_values(self):
        hours = _hours((2024, 1, 1, 0), (2024, 1, 1, 3))
        vals = [10.0, 11.0, 12.0, 13.0]
        out = lags.lag_series(hours, vals, 2)
        assert out == [None, None, 10.0, 11.0]


class TestLagDST:
    def test_lag_across_dst_spring(self):
        """Lag über den DST-Übergang (2024-03-31): 03:00 = Nachfolger von 01:00."""
        hours = _hours((2024, 3, 31, 0), (2024, 3, 31, 23))
        vals = [float(i) for i in range(len(hours))]
        out = lags.lag_series(hours, vals, 1)
        # 00:00 → None; 01:00 → 0.0; 03:00 (nach 01:00) → Wert von 01:00 = 2.0
        h03 = hours.index(datetime(2024, 3, 31, 3, 0, tzinfo=BERLIN))
        assert out[h03] == vals[h03 - 1] == 2.0

    def test_lag_across_dst_autumn(self):
        """Lag über den DST-Rückfall (2024-10-27): 01:00 CET = Nachfolger von 01:00 CEST."""
        hours = _hours((2024, 10, 26, 22), (2024, 10, 28, 2))
        vals = [float(i) for i in range(len(hours))]
        out = lags.lag_series(hours, vals, 1)
        for i in range(1, len(hours)):
            assert out[i] == vals[i - 1]  # immer exakt ein Raster-Schritt

    def test_lag_across_year_boundary(self):
        hours = _hours((2024, 12, 31, 22), (2025, 1, 1, 2))
        vals = [float(i) for i in range(len(hours))]
        out = lags.lag_series(hours, vals, 1)
        for i in range(1, len(hours)):
            assert out[i] == vals[i - 1]
        assert out[0] is None

    def test_lag_across_month_boundary(self):
        hours = _hours((2024, 2, 28, 22), (2024, 3, 1, 2))
        vals = [float(i) for i in range(len(hours))]
        out = lags.lag_series(hours, vals, 2)
        for i in range(2, len(hours)):
            assert out[i] == vals[i - 2]

    def test_lag_24h_over_dst_is_one_raster_step(self):
        """24-h-Lag über den Spring-DST = 24 Raster-Schritte (kein UTC-Drift)."""
        hours = _hours((2024, 3, 30, 0), (2024, 4, 1, 23))
        vals = [float(i) for i in range(len(hours))]
        out = lags.lag_series(hours, vals, 24)
        # Index 24 = 2024-03-31 01:00 (Raster), Wert = vals[0]
        assert out[24] == vals[0]


class TestCrossCheck:
    def test_cross_check_raster_ok(self):
        hours = _hours((2024, 3, 30, 0), (2024, 4, 2, 0))
        assert lags.cross_check_raster(hours) is True

    def test_cross_check_raster_gap(self):
        hours = _hours((2024, 1, 1, 0), (2024, 1, 1, 3))
        hours = hours[0:2] + hours[3:]  # 02:00 fehlt
        assert lags.cross_check_raster(hours) is False

    def test_first_full_hour_index(self):
        hours = _hours((2024, 1, 1, 0), (2024, 1, 1, 3))
        assert lags.first_full_hour_index(hours, hours[0]) == 0
        assert lags.first_full_hour_index(hours, hours[2]) == 2
        assert lags.first_full_hour_index(hours, hours[0] - __import__("datetime").timedelta(hours=1)) == -1
