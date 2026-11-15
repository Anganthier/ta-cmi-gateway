"""CMI /LOG Parquet store tests (S4N-LogParquet).

* **Write**: the live-month rewrite -- exact 71-column schema (like
  ``winsol_pq``), ts de-duplication (existing rows win), atomic
  ``.part`` + rename, finished months byte-stable, cross-month rejects.
* **Read**: the ``CmiLogParquet`` interface mirrors ``WinsolParquet``
  (column resolution, range/aggregation, clean degradation on a missing dir).
* **API union**: ``/api/v1/history?device=winsol:<N>`` merges the read-only
  WinSol archive with the live cmi-log store per (device, channel, ad) --
  ts-union, the archive wins on timestamp collisions.

No network; all Parquet fixtures are tiny in-test files under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.cmi.cmi_log_pq import (
    WIDE_COLUMNS,
    CmiLogParquet,
    CmiLogPqInputError,
    CmiLogPqStoreError,
)

READ = {"Authorization": "Bearer read-tok"}

REAL_WINSOL = Path(__file__).resolve().parent.parent / "data" / "winsol_pq" / "2026" / "10" / "2026-10.parquet"
_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
def _require_cmi_log_captures() -> None:
    """Backfill-Tests benötigen die echten CMI-/LOG-Roh-Dateien (``data/cmi_log``)
    und die LogX-Gap-Quelle (``discovery/cmi-log/logx_2026-10-30_toggle.log``) --
    beide sind > 1 MB / Dev-Repo-artefakte und im Public-Repo nicht gebündelt."""
    raw_dir = _REPO_ROOT / "data" / "cmi_log"
    gap = _REPO_ROOT / "discovery" / "cmi-log" / "logx_2026-10-30_toggle.log"
    if not raw_dir.is_dir() or not gap.is_file():
        pytest.skip("CMI-/LOG-Raw-Captures nicht gebündelt (public repo)")


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
def _write_cmi_month(store_dir: Path, month: str, records: list[tuple[str, dict]]) -> None:
    CmiLogParquet(store_dir).write_month(month, records)


def _read_month(store_dir: Path, month: str):
    p = store_dir / month[:4] / month[5:7] / f"{month}.parquet"
    return pq.read_table(p)


def _write_winsol_month(dst: Path, month: str, rows: list[dict]) -> None:
    """Winsol archive fixture (subset of the 71 wide columns)."""
    cols = ("ts", "dev1_analog_02", "dev1_digital_02", "dev0_analog_01", "dev0_digital_02")
    d = dst / month[:4] / month[5:7]
    d.mkdir(parents=True, exist_ok=True)
    data = {c: [r.get(c) for r in rows] for c in cols}
    schema = pa.schema([("ts", pa.string()), *((c, pa.float64()) for c in cols[1:])])
    pq.write_table(pa.table(data, schema=schema), d / f"{month}.parquet", compression="zstd")


def _app_with_both_stores(settings, winsol_dir: Path, cmi_dir: Path):
    from app.cmi.client import CMIClient
    from app.main import Gateway, create_app
    from tests.conftest import make_fake_transport

    settings.winsol_parquet_dir = winsol_dir
    settings.cmi_log_parquet_dir = cmi_dir
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


# ---------------------------------------------------------------------------
# Write: schema
# ---------------------------------------------------------------------------
def test_write_month_schema_is_the_canonical_74_columns(tmp_path):
    """Q-125: the store schema grew 71 -> 74 columns (+dev2_analog_17/18/19)."""
    store = CmiLogParquet(tmp_path / "pq")
    try:
        store.write_month(
            "2026-10",
            [
                ("2026-10-27 07:26:24", {"dev1_analog_02": 21.5, "dev0_digital_02": 1.0}),
                ("2026-10-27 07:26:34", {"dev2_digital_01": 0.0}),
            ],
        )
    finally:
        store.close()
    table = _read_month(tmp_path / "pq", "2026-10")
    assert len(WIDE_COLUMNS) == 74
    assert table.schema.names == list(WIDE_COLUMNS)
    assert all(str(f.type) == "double" for f in table.schema if f.name != "ts")
    assert str(table.schema.field("ts").type) == "string"
    if REAL_WINSOL.is_file():
        ref = pq.read_schema(REAL_WINSOL)
        # The 74-column cmi schema = the 71-column winsol schema with the three
        # Q-125 dev2 analog channels inserted directly after dev2_analog_16.
        anchor = ref.names.index("dev2_analog_16")
        expected = [
            *ref.names[: anchor + 1],
            "dev2_analog_17",
            "dev2_analog_18",
            "dev2_analog_19",
            *ref.names[anchor + 1:],
        ]
        assert table.schema.names == expected, "cmi 74-col schema = winsol 71 + dev2 a17/18/19"
        # every non-ts value column is double (incl. the three new ones).
        assert all(
            str(table.schema.field(n).type) == "double"
            for n in ("dev2_analog_17", "dev2_analog_18", "dev2_analog_19")
        )


def test_write_month_migrates_legacy_71_col_file(tmp_path):
    """Q-125: a pre-Q-125 71-column month file (missing dev2 a17/18/19) is
    transparently migrated to the canonical 74-column schema on the write path
    (the three new columns read as NULL; existing rows are preserved verbatim).
    Any OTHER schema is still a store error."""
    store = CmiLogParquet(tmp_path / "pq")
    dest = tmp_path / "pq" / "2026" / "10" / "2026-10.parquet"
    dest.parent.mkdir(parents=True)
    legacy_cols = [c for c in WIDE_COLUMNS if c not in
                   ("dev2_analog_17", "dev2_analog_18", "dev2_analog_19")]
    legacy_schema = pa.schema([("ts", pa.string()),
                               *((c, pa.float64()) for c in legacy_cols[1:])])
    legacy_data = {c: [None, None] for c in legacy_cols[1:]}
    legacy_data["dev0_analog_01"] = [10.0, 11.0]
    legacy_data = {"ts": ["2026-10-01 00:00:00", "2026-10-01 00:00:10"],
                   **legacy_data}
    pq.write_table(
        pa.table(legacy_data, schema=legacy_schema),
        dest,
    )
    try:
        added = store.write_month(
            "2026-10",
            [("2026-10-01 00:00:20", {"dev0_analog_01": 12.0, "dev2_analog_17": 10.0})],
        )
    finally:
        store.close()
    assert added == 1
    table = _read_month(tmp_path / "pq", "2026-10")
    assert table.schema.names == list(WIDE_COLUMNS)  # migrated to 74 columns
    assert table.column("ts").to_pylist() == [
        "2026-10-01 00:00:00",
        "2026-10-01 00:00:10",
        "2026-10-01 00:00:20",
    ]
    # P=169 legacy rows: the three new columns are NULL.
    assert table.column("dev2_analog_17").to_pylist()[:2] == [None, None]
    # the migrated P=169 rows keep their values verbatim.
    assert table.column("dev0_analog_01").to_pylist() == [10.0, 11.0, 12.0]
    # the P=181 new row populates dev2 a17.
    assert table.column("dev2_analog_17").to_pylist()[2] == 10.0


def test_write_month_p169_records_null_new_cols(tmp_path):
    """Q-125: P=169 records carry no dev2 a17-a19 payload -> those three wide
    columns read NULL for P=169 rows; P=181 rows populate them."""
    store = CmiLogParquet(tmp_path / "pq")
    try:
        store.write_month(
            "2026-11",
            [
                # P=169 record: only the 70 legacy channels, no dev2 a17-19.
                ("2026-11-07 00:00:10", {"dev0_analog_01": 5.0, "dev1_analog_01": 20.0}),
                # P=181 record: additionally the three new dev2 analog channels.
                (
                    "2026-11-07 16:16:09",
                    {"dev0_analog_01": 6.0, "dev1_analog_01": 21.0,
                     "dev2_analog_17": 10.0, "dev2_analog_18": 1.0, "dev2_analog_19": 0.0},
                ),
            ],
        )
    finally:
        store.close()
    table = _read_month(tmp_path / "pq", "2026-11")
    ts = table.column("ts").to_pylist()
    a17 = table.column("dev2_analog_17").to_pylist()
    a18 = table.column("dev2_analog_18").to_pylist()
    a19 = table.column("dev2_analog_19").to_pylist()
    assert ts == ["2026-11-07 00:00:10", "2026-11-07 16:16:09"]
    # P=169 row: the three Q-125 columns are NULL.
    assert (a17[0], a18[0], a19[0]) == (None, None, None)
    # P=181 row: the three Q-125 columns are populated.
    assert (a17[1], a18[1], a19[1]) == (10.0, 1.0, 0.0)


def test_write_month_rows_and_nulls(tmp_path):
    store = CmiLogParquet(tmp_path / "pq")
    try:
        added = store.write_month(
            "2026-10",
            [
                ("2026-10-27 07:26:24", {"dev1_analog_02": 21.5, "dev0_digital_02": 1.0}),
                ("2026-10-27 07:26:34", {"dev2_digital_01": 0.0}),
            ],
        )
    finally:
        store.close()
    assert added == 2
    table = _read_month(tmp_path / "pq", "2026-10")
    ts = table.column("ts").to_pylist()
    assert ts == ["2026-10-27 07:26:24", "2026-10-27 07:26:34"]
    a2 = table.column("dev1_analog_02").to_pylist()
    assert a2[0] == 21.5 and a2[1] is None
    d2 = table.column("dev0_digital_02").to_pylist()
    assert d2[0] == 1.0 and d2[1] is None
    dd1 = table.column("dev2_digital_01").to_pylist()
    assert dd1[0] is None and dd1[1] == 0.0


# ---------------------------------------------------------------------------
# Write: rewrite / de-dup / atomicity / stability
# ---------------------------------------------------------------------------
def test_write_month_rewrite_dedup_existing_wins(tmp_path):
    """The poller rewrites the in-progress month every 5 min: a ts already in
    the store is never overwritten (existing row wins), new ts are appended."""
    store = CmiLogParquet(tmp_path / "pq")
    try:
        store.write_month("2026-10", [("2026-10-27 07:26:24", {"dev1_analog_02": 21.5})])
        added = store.write_month(
            "2026-10",
            [
                ("2026-10-27 07:26:24", {"dev1_analog_02": 99.9}),  # dup: keep 21.5
                ("2026-10-27 07:26:34", {"dev1_analog_02": 22.0}),  # new
            ],
        )
    finally:
        store.close()
    assert added == 1
    table = _read_month(tmp_path / "pq", "2026-10")
    assert len(table) == 2
    assert table.column("dev1_analog_02").to_pylist() == [21.5, 22.0]
    # atomic write: no temp file left behind
    assert not list((tmp_path / "pq" / "2026" / "10").glob("*.part"))


def test_write_month_finished_month_is_byte_stable(tmp_path):
    """A rewrite of 2026-10 must not touch the finished 2026-09 file (SHA)."""
    store = CmiLogParquet(tmp_path / "pq")
    try:
        store.write_month("2026-09", [("2026-09-30 23:59:50", {"dev0_analog_01": 1.0})])
        p9 = tmp_path / "pq" / "2026" / "09" / "2026-09.parquet"
        sha_before = hashlib.sha256(p9.read_bytes()).hexdigest()
        store.write_month("2026-10", [("2026-10-01 00:00:00", {"dev0_analog_01": 2.0})])
    finally:
        store.close()
    p9 = tmp_path / "pq" / "2026" / "09" / "2026-09.parquet"
    assert hashlib.sha256(p9.read_bytes()).hexdigest() == sha_before


def test_write_month_rejects_foreign_month(tmp_path):
    store = CmiLogParquet(tmp_path / "pq")
    try:
        with pytest.raises(CmiLogPqStoreError, match="outside month"):
            store.write_month("2026-10", [("2026-11-01 00:00:00", {"dev0_analog_01": 1.0})])
        with pytest.raises(ValueError, match="YYYY-MM"):
            store.write_month("2026-100", [("2026-10-01 00:00:00", {})])
    finally:
        store.close()
    assert not store.is_available()


def test_write_month_empty_is_noop(tmp_path):
    store = CmiLogParquet(tmp_path / "pq")
    try:
        assert store.write_month("2026-10", []) == 0
        assert store.is_available() is False
    finally:
        store.close()


def test_write_month_rejects_corrupt_existing_file(tmp_path):
    """A month file without the canonical schema is a store error (never a
    silent overwrite)."""
    store = CmiLogParquet(tmp_path / "pq")
    try:
        d = tmp_path / "pq" / "2026" / "10"
        d.mkdir(parents=True)
        pq.write_table(pa.table({"ts": pa.array(["x"], pa.string())}),
                       d / "2026-10.parquet")
        with pytest.raises(CmiLogPqStoreError, match="canonical"):
            store.write_month("2026-10", [("2026-10-01 00:00:00", {"dev0_analog_01": 1.0})])
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Read: interface mirror of WinsolParquet
# ---------------------------------------------------------------------------
def test_cmi_pq_column_resolution_and_channels(tmp_path):
    store = CmiLogParquet(tmp_path / "pq")
    try:
        store.write_month(
            "2026-10",
            [
                ("2026-10-27 07:26:24", {"dev1_analog_02": 21.5, "dev1_digital_02": 1.0}),
                ("2026-10-27 07:26:34", {"dev0_digital_05": 0.0, "dev2_digital_01": 1.0}),
            ],
        )
        # Data-driven: only the actually logged channels exist.
        assert store.available_channels("winsol:1", "A") == {2}
        assert store.available_channels("winsol:1", "D") == {2}
        assert store.available_channels("winsol:1", None) == {2}
        assert store.available_channels("winsol:0", "D") == {5}
        assert store.available_channels("winsol:2", "D") == {1}
        assert store.available_channels("winsol:0", "A") == set()  # no analog ch1 data
        assert store.resolve_columns("winsol:1", 2, "A") == ["dev1_analog_02"]
        assert store.resolve_columns("winsol:1", 2, None) == [
            "dev1_analog_02",
            "dev1_digital_02",
        ]
        with pytest.raises(CmiLogPqInputError):
            store.resolve_columns("cmi-log:1", 2, "A")  # wrong tag
        with pytest.raises(CmiLogPqInputError):
            store.resolve_columns("winsol:7", 2, "A")
        with pytest.raises(CmiLogPqInputError, match="available channels"):
            store.resolve_columns("winsol:0", 1, "A")  # dev0 ch1 not logged
        # invalid agg
        with pytest.raises(CmiLogPqInputError):
            store.query("winsol:1", 2, "A", "2026-10-01", "2026-10-02", agg="median")
    finally:
        store.close()


def test_cmi_pq_query_last_min_interval(tmp_path):
    store = CmiLogParquet(tmp_path / "pq")
    try:
        store.write_month(
            "2026-10",
            [
                (f"2026-10-27 07:{i:02d}:00", {"dev1_analog_02": float(i)})
                for i in range(10)
            ]
            + [
                (f"2026-10-27 07:{i:02d}:05", {"dev1_digital_02": float(i % 2)})
                for i in range(10)
            ],
        )
        last = store.query(
            "winsol:1", 2, "A", "2026-10-27 07:00:00", "2026-10-27 07:09:00",
            agg="last", limit=3,
        )
        assert [p["value"] for p in last] == [7.0, 8.0, 9.0]
        # ad=None unions analog (0..9 @ :00) and digital (0/1 @ :05)
        mx = store.query(
            "winsol:1", 2, None, "2026-10-27 07:00:00", "2026-10-27 07:09:00", agg="max"
        )
        assert mx[0]["value"] == 9.0
        avg = store.query(
            "winsol:1", 2, "A", "2026-10-27 07:00:00", "2026-10-27 07:09:00",
            agg="avg", interval=300,
        )
        assert [p["value"] for p in avg] == [2.0, 7.0]
        lastb = store.query(
            "winsol:1", 2, "A", "2026-10-27 07:00:00", "2026-10-27 07:09:00",
            agg="last", interval=300,
        )
        assert [p["value"] for p in lastb] == [4.0, 9.0]
    finally:
        store.close()


def test_cmi_pq_missing_dir_degrades(tmp_path):
    store = CmiLogParquet(tmp_path / "absent")
    try:
        assert store.is_available() is False
        assert store.months_range() is None
        assert store.available_channels("winsol:1", "A") == set()
        with pytest.raises(CmiLogPqStoreError):
            store.resolve_columns("winsol:1", 1, "A")
        with pytest.raises(CmiLogPqStoreError):
            store.query("winsol:1", 1, "A", "2026-10-01", "2026-10-02")
    finally:
        store.close()


def test_cmi_pq_months_range(tmp_path):
    store = CmiLogParquet(tmp_path / "pq")
    try:
        store.write_month("2026-09", [("2026-09-01 00:00:00", {"dev0_analog_01": 1.0})])
        store.write_month("2026-10", [("2026-10-01 00:00:00", {"dev0_analog_01": 1.0})])
        assert store.months_range() == ["2026-09", "2026-10"]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# /api/v1/history: the two-store union (winsol archive + cmi-log live store)
# ---------------------------------------------------------------------------
def test_history_union_merges_both_stores_winsol_wins_on_collision(settings, tmp_path):
    """ts T present in BOTH stores -> the winsol_pq value wins; the cmi-log
    row right after the archive gap (07:25:54 -> 07:26:24) extends the
    series without duplicates."""
    winsol_dir = tmp_path / "winsol_pq"
    cmi_dir = tmp_path / "cmi_log_pq"
    _write_winsol_month(winsol_dir, "2026-10", [
        {"ts": "2026-10-27 07:25:34", "dev1_analog_02": 1.0},
        {"ts": "2026-10-27 07:25:54", "dev1_analog_02": 5.0},
    ])
    _write_cmi_month(cmi_dir, "2026-10", [
        ("2026-10-27 07:25:54", {"dev1_analog_02": 9.9}),   # collision: winsol wins
        ("2026-10-27 07:26:24", {"dev1_analog_02": 6.0}),   # gap continuation
    ])
    app = _app_with_both_stores(settings, winsol_dir, cmi_dir)
    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        r = tc.get(
            "/api/v1/history", headers=READ,
            params={
                "device": "winsol:1", "channel": "2", "ad": "A",
                "start": "2026-10-27", "end": "2026-10-28",
                "agg": "last",
            },
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["device"] == "winsol:1" and j["channel"] == 2
        assert j["count"] == 3
        pts = j["points"]
        assert [p["ts"] for p in pts] == [
            "2026-10-27 07:25:34", "2026-10-27 07:25:54", "2026-10-27 07:26:24"
        ]
        assert [p["value"] for p in pts] == [1.0, 5.0, 6.0]  # 5.0 = archive win


def test_history_channel_only_in_cmi_store_is_served(settings, tmp_path):
    """A channel that exists ONLY in cmi_log_pq (e.g. dev0 d2..d9 digital or
    dev2 d1) must still be queryable via the winsol:<N> device."""
    winsol_dir = tmp_path / "winsol_pq"
    cmi_dir = tmp_path / "cmi_log_pq"
    _write_winsol_month(winsol_dir, "2026-10", [
        {"ts": "2026-10-27 07:25:54", "dev1_analog_02": 5.0},
    ])
    _write_cmi_month(cmi_dir, "2026-10", [
        ("2026-10-27 07:26:24", {"dev0_digital_02": 1.0, "dev2_digital_01": 1.0}),
    ])
    app = _app_with_both_stores(settings, winsol_dir, cmi_dir)
    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        r = tc.get(
            "/api/v1/history", headers=READ,
            params={"device": "winsol:0", "channel": "2", "ad": "D",
                    "start": "2026-10-27", "end": "2026-10-28"},
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["count"] == 1 and j["points"][0]["value"] == 1.0
        # ... and with no channel filter at all, both cmi-only channels come
        # back under their respective devices.
        r2 = tc.get(
            "/api/v1/history", headers=READ,
            params={"start": "2026-10-27", "end": "2026-10-28"},
        )
        assert r2.status_code == 200, r2.text
        j2 = r2.json()
        assert "winsol:0:2" in j2  # from cmi store digital only
        assert "winsol:2:1" in j2
        assert "winsol:1:2" in j2  # archive analog row


def test_history_cmi_store_absent_still_serves_archive(settings, tmp_path):
    """cmi_log_pq empty -> the winsol archive alone answers (clean degradation
    of the union; the request never fails because the live store is missing)."""
    winsol_dir = tmp_path / "winsol_pq"
    _write_winsol_month(winsol_dir, "2026-10", [
        {"ts": "2026-10-27 07:25:54", "dev1_analog_02": 5.0},
    ])
    app = _app_with_both_stores(settings, winsol_dir, tmp_path / "no_cmi")
    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        r = tc.get(
            "/api/v1/history", headers=READ,
            params={"device": "winsol:1", "channel": "2", "ad": "A",
                    "start": "2026-10-27", "end": "2026-10-28"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["count"] == 1


def test_history_ad_d_filter_only_cmi_digital(settings, tmp_path):
    """ad=D on a channel whose cmi-log side carries digital only -> no analog
    leak, cmi points served."""
    winsol_dir = tmp_path / "winsol_pq"
    cmi_dir = tmp_path / "cmi_log_pq"
    _write_winsol_month(winsol_dir, "2026-10", [
        {"ts": "2026-10-27 07:25:54", "dev1_analog_02": 5.0},
    ])
    _write_cmi_month(cmi_dir, "2026-10", [
        ("2026-10-27 07:26:24", {"dev1_digital_02": 1.0}),
    ])
    app = _app_with_both_stores(settings, winsol_dir, cmi_dir)
    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        r = tc.get(
            "/api/v1/history", headers=READ,
            params={"device": "winsol:1", "channel": "2", "ad": "D",
                    "start": "2026-10-27", "end": "2026-10-28"},
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["count"] == 1
        assert j["points"] == [{"ts": "2026-10-27 07:26:24", "value": 1.0}]


# ---------------------------------------------------------------------------
# Backfill cleanup (DB side): cmi-log rows out, side table stays
# ---------------------------------------------------------------------------
def test_db_cmi_log_cleanup(tmp_path):
    from app.db import HistoryDB

    db = HistoryDB(tmp_path / "h.db")
    db.init_schema()
    db.upsert(
        [
            ("2026-10-27 07:26:24", "cmi-log:0", 6, "A", 55.0, "\u00b0C", "cmi-log"),
            ("2026-10-27 07:26:24", "cmi-log:2", 1, "A", 40.0, "\u00b0C", "cmi-log"),
            ("2026-10-27 07:26:24", "schematic", 101, "A", 20.0, None, "schematic"),
        ]
    )
    db.cmi_log_mark("data_2026_10_27_07_26_14.log", 1013765, "abc", "2026-10-30 00:00:00")
    assert db.cmi_log_cleanup() == 2
    # idempotent
    assert db.cmi_log_cleanup() == 0
    con = db._ensure_conn()
    assert con.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 1
    assert con.execute(
        "SELECT COUNT(*) FROM measurements WHERE source='cmi-log'"
    ).fetchone()[0] == 0
    # side table stays (poller dedup + clear gate)
    assert db.cmi_log_ingested_names() == {"data_2026_10_27_07_26_14.log"}
    db.close()


# ---------------------------------------------------------------------------
# Backfill script: --dry-run (decode + report, no writes)
# ---------------------------------------------------------------------------
def test_backfill_dry_run_reports_expected_rows(settings, tmp_path, monkeypatch, capsys):
    """analysis/cmi_log_backfill.py --dry-run: decode all CMI files, report the
    wide row counts, write NOTHING (no Parquet, no DB).  S4N-LogFull: the
    decoder now emits 70 channels/record, so the per-file row counts are
    n_decoded * 70 and the LogX-Gap-Rows fill all 70 value columns."""
    _require_cmi_log_captures()
    import analysis.cmi_log_backfill as bf

    monkeypatch.setenv("HISTORY_DB_PATH", str(tmp_path / "unused.db"))
    monkeypatch.setenv("CMI_LOG_PARQUET_DIR", str(tmp_path / "unused_pq"))
    monkeypatch.setenv("CMI_DATA_DIR", str(tmp_path))
    assert bf.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    # the three GT days (analog+digital wide rows = n_decoded each) + the two
    # fragment ts ranges (the CMI file NAMEs carry underscores, not colons)
    # + S4N-LogFull: the 10-31 full day (8592) and the full-coverage markers
    for marker in ("5962", "8639", "7318", "1313", "8592", "20:20:06",
                   "23:59:56", "15:16:56", "70 Kanäle besetzt",
                   "gap-only"):
        assert marker in out, f"expected {marker!r} in dry-run report"
    # alle 10 CMI-Quelldateien dekodiert (S4N-LogFull: +10-31, +11-01)
    for name in ("data_2026_10_27_07_26_14.log", "data_2026_10_28_00_00_04.log",
                 "data_2026_10_29_00_00_05.log", "data_2026_10_29_20_19_56.log",
                 "data_2026_10_29_20_20_16.log", "data_2026_10_29_20_21_06.log",
                 "data_2026_10_30_19_40_56.log", "data_2026_10_30_19_51_56.log",
                 "data_2026_10_31_00_00_06.log", "data_2026_11_01_00_00_09.log"):
        assert name in out, f"expected {name!r} in dry-run report"
    assert not (tmp_path / "unused_pq").exists()
    assert not (tmp_path / "unused.db").exists()


def test_backfill_full_70_channel_coverage(settings, tmp_path, monkeypatch, capsys):
    """Q-125 DoD (Stellenwert): der Backfill deckt alle 73 Kanäle ab (70
    P=169-Kanäle + 3 Q-125 dev2 a17-a19).  Die CMI-Quelle (P=169 + P=181) füllt
    exakt diese 73 Kanäle (P=169: 70 gefüllt + 3 NULL; P=181: 73, davon die
    PENDING dev1/dev2/digital-Spalten NULL).  Die LogX-Gap-Quelle (P=137) trägt
    dieselben 70 P=169-Kanäle, aber KEINE dev2 a17-a19 -- ihre Wide-Rows füllen
    daher genau die 70 P=169-Spalten (die drei Q-125-Spalten bleiben NULL,
    da LogX sie nicht kennt)."""
    _require_cmi_log_captures()
    import analysis.cmi_log_backfill as bf

    monkeypatch.setenv("HISTORY_DB_PATH", str(tmp_path / "unused.db"))
    monkeypatch.setenv("CMI_LOG_PARQUET_DIR", str(tmp_path / "unused_pq"))
    monkeypatch.setenv("CMI_DATA_DIR", str(tmp_path))
    # die 73 Kanäle sind exakt das Kanon-Schema (WIDE_COLUMNS minus ts)
    from app.cmi.cmi_log_pq import WIDE_COLUMNS

    expected = {
        (
            int(c.split("_")[0][3:]),
            "A" if c.split("_")[1] == "analog" else "D",
            int(c.rsplit("_", 1)[1]),
        )
        for c in WIDE_COLUMNS[1:]
    }
    assert expected == set(bf._CMI_LOG_CHANNELS), "CMI-Abdeckung != 73 Kanon-Kanäle"
    assert len(bf._CMI_LOG_CHANNELS) == 73
    # der LogX-Pivot füllt jede der 70 P=169-Spalten (LogX P=137 trägt
    # dieselben 70 Kanäle, aber keine dev2 a17-a19 -> die 3 Q-125-Spalten
    # bleiben in den LogX-Wide-Rows NULL).
    from winsol_logx import decode_record, find_sessions

    p169_cols = set(WIDE_COLUMNS[1:]) - set(bf._NEW3_COLUMNS)
    buf = (bf.REPO / bf.LOGX_GAP_REL).read_bytes()
    records = [
        decode_record(
            buf[sess.start + p * k: sess.start + p * (k + 1)], sess.variant,
            bf.LOGX_GAP_DAY,
        )
        for sess in find_sessions(buf)
        for p in [sess.variant.period]
        for k in range(sess.n_records)
    ]
    rows = bf._logx_wide_rows(records)
    assert rows, "LogX-Gap-Quelle lieferte keine Rows"
    filled = set()
    for _ts, row in rows:
        filled |= set(row)
    assert filled == p169_cols, (
        f"LogX-Gap-Rows füllen nicht exakt die 70 P=169-Spalten: "
        f"fehlend={sorted(p169_cols - filled)} "
        f"zusätzlich={sorted(filled - p169_cols)}"
    )
    # dev2 a17-a19 (Q-125) müssen NICHT von der LogX-Quelle gefüllt sein.
    assert not (filled & set(bf._NEW3_COLUMNS))
    assert not (tmp_path / "unused_pq").exists()  # Dry-Run: kein Write
