"""WinSol Parquet on-demand history tests (S2-Parquet-Pivot).

Parquet is the *canonical* store for WinSol history; the gateway serves it ON
DEMAND for ``winsol:<N>`` devices instead of copying it into the long store.
These tests build tiny in-test Parquet fixtures (pyarrow, a few rows x the wide
schema, in the Hive ``YYYY/MM/YYYY-MM.parquet`` layout) under ``tmp_path`` so the
column mapping, range/aggregation semantics, index-free partition pruning, the
merged ``/api/v1/history`` response and auth are all exercised with no network
and without touching the real ``data/winsol_pq`` store.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.winsol_wide import (
    WinsolInputError,
    WinsolParquet,
    WinsolStoreError,
)

READ = {"Authorization": "Bearer read-tok"}

#: Wide columns present in the fixtures (subset of the real 70).  The real store
#: has dev0 analog 1..16 + digital 2..9, dev1 analog 1..16 + digital 1..13,
#: dev2 analog 1..16 + digital 1; these are enough to exercise the mapping.
FIXTURE_COLS = (
    "dev0_analog_01",
    "dev0_analog_16",
    "dev2_analog_16",
    "dev1_analog_01",
    "dev1_digital_13",
    "dev1_digital_01",
)


def _schema() -> pa.Schema:
    return pa.schema([("ts", pa.string()), *( (c, pa.float64()) for c in FIXTURE_COLS )])


def _write_month(dst: Path, month: str, rows: list[dict]) -> Path:
    """Write one month's Parquet file at ``dst/YYYY/MM/YYYY-MM.parquet``."""
    year, mm = month[:4], month[5:7]
    d = dst / year / mm
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{month}.parquet"
    data = {c: [r.get(c) for r in rows] for c in ("ts", *FIXTURE_COLS)}
    pq.write_table(pa.table(data, schema=_schema()), out, compression="zstd")
    return out


def _make_parquet(tmp_path: Path) -> Path:
    """Two-month fixture (recent dates so they survive the 400-day retention
    window that runs on poller startup).

    * 2026-01: 36 rows, ts ``2026-01-01 00:00:00`` .. ``00:35:00`` (i = 0..35)
      - dev0_analog_01   = i
      - dev0_analog_16   = 1 (flag)
      - dev2_analog_16   = i * 2.5
      - dev1_analog_01   = 10 + i
      - dev1_digital_13  = 1 if i % 2 == 0 else 0
      - dev1_digital_01  = 0
    * 2026-02: 1 row, ts ``2026-02-05 00:00:00``, dev0_analog_01 = 999.0
      (used to prove month-partition pruning excludes other months)
    """
    dst = tmp_path / "winsol_pq"
    jan = []
    for i in range(36):
        jan.append(
            {
                "ts": f"2026-01-01 00:{i:02d}:00",
                "dev0_analog_01": float(i),
                "dev0_analog_16": 1.0,
                "dev2_analog_16": float(i * 2.5),
                "dev1_analog_01": float(10 + i),
                "dev1_digital_13": float(1 if i % 2 == 0 else 0),
                "dev1_digital_01": 0.0,
            }
        )
    _write_month(dst, "2026-01", jan)
    _write_month(dst, "2026-02", [{"ts": "2026-02-05 00:00:00", "dev0_analog_01": 999.0}])
    return dst


@pytest.fixture()
def pq_dir(tmp_path: Path) -> Path:
    return _make_parquet(tmp_path)


# ---------------------------------------------------------------------------
# Column mapping
# ---------------------------------------------------------------------------
def test_resolve_column_mapping(pq_dir):
    w = WinsolParquet(pq_dir)
    try:
        assert w.resolve_columns("winsol:0", 1, "A") == ["dev0_analog_01"]
        assert w.resolve_columns("winsol:2", 16, "A") == ["dev2_analog_16"]
        assert w.resolve_columns("winsol:1", 13, "D") == ["dev1_digital_13"]
        # ad=None unions A and D for the channel (ch1 has both on winsol:1)
        assert w.resolve_columns("winsol:1", 1, None) == ["dev1_analog_01", "dev1_digital_01"]
    finally:
        w.close()


def test_available_channels(pq_dir):
    w = WinsolParquet(pq_dir)
    try:
        assert w.available_channels("winsol:0", "A") == {1, 16}
        assert w.available_channels("winsol:1", "D") == {1, 13}
        assert w.available_channels("winsol:2", "A") == {16}
        assert w.available_channels("winsol:1", None) == {1, 13}  # union
    finally:
        w.close()


def test_bad_device_and_channel(pq_dir, tmp_path):
    w = WinsolParquet(pq_dir)
    try:
        with pytest.raises(WinsolInputError):
            w.resolve_columns("schematic", 1, "A")  # not a winsol device
        with pytest.raises(WinsolInputError):
            w.resolve_columns("winsol:7", 1, "A")  # N out of 0..2
        with pytest.raises(WinsolInputError):
            w.resolve_columns("winsol:0", 99, "A")  # channel out of range
        with pytest.raises(WinsolInputError):
            w.resolve_columns("winsol:0", 1, "X")  # bad ad
        with pytest.raises(WinsolInputError):
            w.query("winsol:0", 1, "A", "2026-01-01", "2026-01-02", agg="median")
    finally:
        w.close()
    # missing dir -> store error
    w2 = WinsolParquet(tmp_path / "absent")
    try:
        with pytest.raises(WinsolStoreError):
            w2.query("winsol:0", 1, "A", "2026-01-01", "2026-01-02")
    finally:
        w2.close()


def test_bad_channel_error_lists_available(pq_dir):
    w = WinsolParquet(pq_dir)
    try:
        with pytest.raises(WinsolInputError, match=r"available channels"):
            w.resolve_columns("winsol:0", 99, "A")
    finally:
        w.close()


# ---------------------------------------------------------------------------
# Range + aggregation + interval
# ---------------------------------------------------------------------------
def test_last_no_interval_returns_recent_samples(pq_dir):
    w = WinsolParquet(pq_dir)
    try:
        pts = w.query(
            "winsol:0", 1, "A", "2026-01-01 00:00:00", "2026-01-01 00:35:00",
            agg="last", limit=5,
        )
        assert [p["value"] for p in pts] == [31.0, 32.0, 33.0, 34.0, 35.0]
        assert pts[-1]["ts"] == "2026-01-01 00:35:00"
    finally:
        w.close()


def test_range_filtering(pq_dir):
    w = WinsolParquet(pq_dir)
    try:
        pts = w.query(
            "winsol:0", 1, "A", "2026-01-01 00:05:00", "2026-01-01 00:15:00", agg="last"
        )
        assert [p["value"] for p in pts] == list(range(5, 16))
    finally:
        w.close()


def test_agg_min_max_avg(pq_dir):
    w = WinsolParquet(pq_dir)
    try:
        for agg, want in (("min", 0.0), ("max", 35.0), ("avg", 17.5)):
            pts = w.query(
                "winsol:0", 1, "A", "2026-01-01 00:00:00", "2026-01-01 00:35:00", agg=agg
            )
            assert len(pts) == 1, agg
            assert pts[0]["value"] == want, agg
            assert pts[0]["ts"] == "2026-01-01 00:35:00"
    finally:
        w.close()


def test_agg_min_max_avg_interval_buckets(pq_dir):
    w = WinsolParquet(pq_dir)
    try:
        # 900s (15 min) buckets over [00:00, 00:35]:
        #   [00:00,00:15): i=0..14 -> avg 7.0
        #   [00:15,00:30): i=15..29 -> avg 22.0
        #   [00:30,00:45): i=30..35 -> avg 32.5
        pts = w.query(
            "winsol:0", 1, "A", "2026-01-01 00:00:00", "2026-01-01 00:35:00",
            agg="avg", interval=900,
        )
        assert [p["value"] for p in pts] == [7.0, 22.0, 32.5]
    finally:
        w.close()


def test_agg_last_interval_buckets(pq_dir):
    w = WinsolParquet(pq_dir)
    try:
        # 900s buckets, last-per-bucket: i=14, i=29, i=35 -> 14, 29, 35
        pts = w.query(
            "winsol:0", 1, "A", "2026-01-01 00:00:00", "2026-01-01 00:35:00",
            agg="last", interval=900,
        )
        assert [p["value"] for p in pts] == [14.0, 29.0, 35.0]
    finally:
        w.close()


def test_union_analog_digital_ad_none(pq_dir):
    # winsol:1 ch1 has analog (10..45) AND digital (all 0).  ad=None unions them.
    w = WinsolParquet(pq_dir)
    try:
        mx = w.query("winsol:1", 1, None, "2026-01-01 00:00:00", "2026-01-01 00:35:00", agg="max")
        assert mx[0]["value"] == 45.0
        mn = w.query("winsol:1", 1, None, "2026-01-01 00:00:00", "2026-01-01 00:35:00", agg="min")
        assert mn[0]["value"] == 0.0  # the digital column contributes 0s
    finally:
        w.close()


def test_ad_single(pq_dir):
    w = WinsolParquet(pq_dir)
    try:
        # ad='A' takes only the analog column (10..45), so min is 10 not 0.
        mn = w.query("winsol:1", 1, "A", "2026-01-01 00:00:00", "2026-01-01 00:35:00", agg="min")
        assert mn[0]["value"] == 10.0
    finally:
        w.close()


# ---------------------------------------------------------------------------
# Partition pruning
# ---------------------------------------------------------------------------
def test_month_partition_pruning(pq_dir):
    """A January range query must only return January rows.

    Note: the bare YYYY/MM directory names do NOT create hive columns, so
    DuckDB opens both month files; the ``ts`` filter prunes at row-group
    level instead (fast, results verified).  Behavioural assertion here: the
    February row (dev0_analog_01=999) must never leak into a January range.
    """
    w = WinsolParquet(pq_dir)
    try:
        pts = w.query(
            "winsol:0", 1, "A", "2026-01-01 00:00:00", "2026-01-31 23:59:59", agg="last"
        )
        assert len(pts) == 36
        assert pts[-1]["value"] == 35.0  # NOT 999.0 from the 2026-02 file
        agg = w.query(
            "winsol:0", 1, "A", "2026-01-01 00:00:00", "2026-01-31 23:59:59", agg="max"
        )
        assert agg[0]["value"] == 35.0
    finally:
        w.close()


# ---------------------------------------------------------------------------
# Store availability / health helpers
# ---------------------------------------------------------------------------
def test_is_available_and_months_range(pq_dir):
    w = WinsolParquet(pq_dir)
    try:
        assert w.is_available() is True
        assert w.months_range() == ["2026-01", "2026-02"]
    finally:
        w.close()


def test_missing_dir_is_unavailable(tmp_path):
    w = WinsolParquet(tmp_path / "does_not_exist")
    try:
        assert w.is_available() is False
        assert w.months_range() is None
        # available_channels degrades to empty (no crash, no store error)
        assert w.available_channels("winsol:0", "A") == set()
    finally:
        w.close()


def test_empty_dir_is_unavailable(tmp_path):
    (tmp_path / "empty").mkdir()
    w = WinsolParquet(tmp_path / "empty")
    try:
        assert w.is_available() is False
    finally:
        w.close()


# ---------------------------------------------------------------------------
# /api/v1/history: wide (Parquet) + long store merged, winsol on demand
# ---------------------------------------------------------------------------
def _seed_long(settings):
    from app.db import get_history_db

    db = get_history_db(settings.history_db_path)
    db.init_schema()
    db.upsert(
        [
            ("2026-01-01 00:00:00", "schematic", 209, "A", 10.0, None, "schematic"),
            ("2026-01-01 00:10:00", "schematic", 209, "A", 20.0, None, "schematic"),
            ("2026-01-01 00:20:00", "schematic", 209, "A", 30.0, None, "schematic"),
        ]
    )


def _app_with_winsol(settings, parquet_dir: Path):
    from app.cmi.client import CMIClient
    from app.main import Gateway, create_app
    from tests.conftest import make_fake_transport

    settings.winsol_parquet_dir = parquet_dir
    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)
    client = CMIClient(
        base_url=settings.base_url,
        user=settings.cmi_user,
        password=settings.cmi_pass,
        transport=make_fake_transport(),
    )
    gateway = Gateway(settings, client=client)
    app = create_app(settings)
    app.state.gateway = gateway
    return app


def test_history_winsol_parquet_plus_schematic_long_multi(settings, tmp_path):
    pq = _make_parquet(tmp_path)
    app = _app_with_winsol(settings, pq)
    _seed_long(settings)
    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        r = tc.get(
            "/api/v1/history",
            headers=READ,
            params={
                "channel": "1,209",
                "start": "2026-01-01",
                "end": "2026-01-02",
            },
        )
        assert r.status_code == 200, r.text
        j = r.json()
        # winsol served from the Parquet store.  ch1 exists on dev0 (analog only)
        # and dev1 (analog + digital, so ad=None unions both -> 72 points).
        assert j["winsol:0:1"]["count"] == 36
        assert j["winsol:0:1"]["points"][0]["value"] == 0.0
        assert j["winsol:1:1"]["count"] == 72
        # ... and schematic from the long store, in ONE multi-series response.
        assert [p["value"] for p in j["schematic:209"]["points"]] == [10.0, 20.0, 30.0]
        assert "_meta" in j
        assert j["_meta"]["count"] == 36 + 72 + 3


def test_history_winsol_single_series_flat_shape(settings, tmp_path):
    pq = _make_parquet(tmp_path)
    app = _app_with_winsol(settings, pq)
    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        r = tc.get(
            "/api/v1/history",
            headers=READ,
            params={
                "device": "winsol:0",
                "channel": "1",
                "start": "2026-01-01 00:05:00",
                "end": "2026-01-01 00:15:00",
                "agg": "last",
            },
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["device"] == "winsol:0" and j["channel"] == 1
        assert [p["value"] for p in j["points"]] == list(range(5, 16))


def test_history_winsol_bad_channel_returns_empty_200(settings, tmp_path):
    pq = _make_parquet(tmp_path)
    app = _app_with_winsol(settings, pq)
    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        r = tc.get(
            "/api/v1/history",
            headers=READ,
            params={
                "device": "winsol:0",
                "channel": "99",  # not a fixture column -> dropped, empty
                "start": "2026-01-01",
                "end": "2026-01-02",
            },
        )
        assert r.status_code == 200
        assert r.json() == {}


def test_history_missing_parquet_no_winsol_but_long_ok(settings, tmp_path):
    # Parquet dir absent -> no winsol series, but the long store still answers.
    _seed_long(settings)
    app = _app_with_winsol(settings, tmp_path / "absent_pq")
    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        r = tc.get(
            "/api/v1/history",
            headers=READ,
            params={"channel": "209", "start": "2026-01-01", "end": "2026-01-02"},
        )
        assert r.status_code == 200, r.text
        j = r.json()
        # Single series (no winsol store) -> flat shape
        assert j["device"] == "schematic" and j["channel"] == 209
        assert [p["value"] for p in j["points"]] == [10.0, 20.0, 30.0]


def test_history_winsol_requires_token(settings, tmp_path):
    pq = _make_parquet(tmp_path)
    app = _app_with_winsol(settings, pq)
    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        r = tc.get(
            "/api/v1/history",
            params={"device": "winsol:0", "channel": "1",
                    "start": "2026-01-01", "end": "2026-01-02"},
        )
        assert r.status_code == 401
        assert r.headers.get("WWW-Authenticate", "").startswith("Bearer")


def test_health_reports_winsol_block(settings, tmp_path):
    pq = _make_parquet(tmp_path)
    app = _app_with_winsol(settings, pq)
    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        r = tc.get("/api/v1/health")
        assert r.status_code in (200, 503)
        w = r.json().get("winsol")
        assert w is not None
        assert w["available"] is True
        assert w["months"] == ["2026-01", "2026-02"]


# ---------------------------------------------------------------------------
# /api/v1/history: ad filter (S4-4) -- A/D types must not mix in one series
# ---------------------------------------------------------------------------
def _winsol_range():
    return {"start": "2026-01-01", "end": "2026-01-02"}


def test_history_winsol_ad_analog_only(settings, tmp_path):
    """ad=A must return ONLY dev1_analog_01 -- the digital twin (all 0s) gone."""
    pq = _make_parquet(tmp_path)
    app = _app_with_winsol(settings, pq)
    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        r = tc.get(
            "/api/v1/history", headers=READ,
            params={"device": "winsol:1", "channel": "1", "ad": "A",
                    **_winsol_range()},
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["device"] == "winsol:1" and j["channel"] == 1
        assert j["count"] == 36
        assert [p["value"] for p in j["points"]] == [10.0 + i for i in range(36)]


def test_history_winsol_ad_digital_only(settings, tmp_path):
    """ad=D must return ONLY dev1_digital_01 (0/1) -- no analog temperatures."""
    pq = _make_parquet(tmp_path)
    app = _app_with_winsol(settings, pq)
    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        r = tc.get(
            "/api/v1/history", headers=READ,
            params={"device": "winsol:1", "channel": "1", "ad": "D",
                    **_winsol_range()},
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["count"] == 36
        assert all(p["value"] in (0.0, 1.0) for p in j["points"])


def test_history_winsol_ad_omitted_is_union(settings, tmp_path):
    """No ad param -> both A and D rows in one series (original contract)."""
    pq = _make_parquet(tmp_path)
    app = _app_with_winsol(settings, pq)
    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        r = tc.get(
            "/api/v1/history", headers=READ,
            params={"device": "winsol:1", "channel": "1", **_winsol_range()},
        )
        assert r.status_code == 200, r.text
        j = r.json()
        # 36 analog + 36 digital rows per the union contract.
        assert j["count"] == 72


def test_history_winsol_ad_invalid_400(settings, tmp_path):
    pq = _make_parquet(tmp_path)
    app = _app_with_winsol(settings, pq)
    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        r = tc.get(
            "/api/v1/history", headers=READ,
            params={"device": "winsol:1", "channel": "1", "ad": "X",
                    **_winsol_range()},
        )
        assert r.status_code == 400
        assert "ad" in r.json()["detail"]


def test_history_winsol_ad_a_on_digital_only_channel_is_empty(settings, tmp_path):
    """winsol:1 ch13 exists only as digital; ad=A must yield no series (200 {}).

    (In the real store this is ch2 on winsol:0: digital d2..d9 only.)
    """
    pq = _make_parquet(tmp_path)
    app = _app_with_winsol(settings, pq)
    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        r = tc.get(
            "/api/v1/history", headers=READ,
            params={"device": "winsol:1", "channel": "13", "ad": "A",
                    **_winsol_range()},
        )
        assert r.status_code == 200
        assert r.json() == {}
