"""Unit tests for the persistent history store (app/db.py).

All tests use a temp DB file and never touch the real ``data/history.db``.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app.cmi.json_api import parse_json_api
from app.cmi.schematic_parser import DEFAULT_SCHEMATIC_DIR, parse_schematic_file
from app.db import (
    DEFAULT_HISTORY_LIMIT,
    HistoryDB,
    HistoryQuery,
    now_iso,
    parse_ts,
)
from tests.conftest import DISCOVERY, make_api_fixture


@pytest.fixture()
def db(tmp_path: Path) -> HistoryDB:
    store = HistoryDB(tmp_path / "history.db")
    store.init_schema()
    yield store
    store.close()


def _ts(offset_days: float) -> str:
    from datetime import datetime

    return (datetime.now() + timedelta(days=offset_days)).strftime("%Y-%m-%d %H:%M:%S")


def test_schema_init_idempotent(db, tmp_path: Path):
    """Opening + init on the same file twice must not raise or duplicate."""
    db.init_schema()  # no-op second time
    db2 = HistoryDB(tmp_path / "history.db")
    db2.init_schema()  # re-open, still fine
    db2.close()
    # canonical columns exist exactly once
    con = db._ensure_conn()
    cols = [r[1] for r in con.execute("PRAGMA table_info(measurements)")]
    for c in ("ts", "device", "channel", "ad", "value", "unit", "source"):
        assert cols.count(c) == 1


def test_migrates_missing_column(tmp_path: Path):
    """A pre-existing table missing a canonical column gets it added."""
    import sqlite3

    path = tmp_path / "old.db"
    raw = sqlite3.connect(str(path))
    raw.execute("CREATE TABLE measurements (ts TEXT, device TEXT)")
    raw.commit()
    raw.close()
    db = HistoryDB(path)
    db.init_schema()  # should ADD the missing columns, not fail
    cols = [r[1] for r in db._ensure_conn().execute("PRAGMA table_info(measurements)")]
    assert "channel" in cols and "value" in cols
    db.close()


def test_upsert_inserts_and_dedups(db):
    row = (_ts(0), "schematic", 209, "A", 75.0, "°C", "schematic")
    assert db.upsert([row]) == 1
    # same composite key -> ignored
    assert db.upsert([row]) == 0
    # a different value at the same key is still ignored (PK-based)
    assert db.upsert([(_ts(0), "schematic", 209, "A", 76.0, "°C", "schematic")]) == 0
    assert db.stats()["rows"] == 1


def test_upsert_records_source(db):
    db.upsert([(_ts(0), "schematic", 209, "A", 1.0, None, "schematic")])
    db.upsert([(_ts(0), "json:32", 1, "A", 2.0, None, "json")], source_name="json:32")
    con = db._ensure_conn()
    names = {r[0] for r in con.execute("SELECT name FROM sources")}
    assert {"schematic", "json:32"} <= names


def test_stats_empty(db):
    stat = db.stats()
    assert stat["rows"] == 0 and stat["first_ts"] is None and stat["last_ts"] is None


def test_parse_ts_variants():
    assert parse_ts("2026-01-02") == "2026-01-02 00:00:00"
    assert parse_ts("2026-01-02 03:04:05") == "2026-01-02 03:04:05"
    assert parse_ts("2026-01-02T03:04:05") == "2026-01-02 03:04:05"
    with pytest.raises(ValueError):
        parse_ts("02.01.2026")


def _seed(db: HistoryDB) -> None:
    # Distinct timestamps per row (the PK dedups on ts+device+channel+ad, so the
    # same ts with a different value would be collapsed -- use distinct ts).
    db.upsert(
        [
            ("2026-01-01 00:00:00", "schematic", 209, "A", 10.0, None, "schematic"),
            ("2026-01-01 00:05:00", "schematic", 209, "A", 20.0, None, "schematic"),
            ("2026-01-01 00:00:00", "schematic", 210, "A", 30.0, None, "schematic"),
            ("2026-01-01 00:00:00", "json:32", 1, "A", 40.0, None, "json"),
        ]
    )


def test_query_single_channel_last(db):
    _seed(db)
    out = db.query_series(
        HistoryQuery(start="2025-12-31 00:00:00", end="2026-01-02 00:00:00",
                     device="schematic", channels=(209,))
    )
    assert out["device"] == "schematic" and out["channel"] == 209
    assert out["count"] == 2
    assert [p["value"] for p in out["points"]] == [10.0, 20.0]
    assert out["agg"] == "last"


def test_query_multi_channel(db):
    _seed(db)
    out = db.query_series(
        HistoryQuery(start="2025-12-31 00:00:00", end="2026-01-02 00:00:00",
                     device="schematic")
    )
    assert "schematic:209" in out and "schematic:210" in out
    assert out["_meta"]["count"] == 3


def test_query_agg_min_max_avg(db):
    _seed(db)
    for agg, want in (("min", 10.0), ("max", 20.0), ("avg", 15.0)):
        out = db.query_series(
            HistoryQuery(start="2025-12-31 00:00:00", end="2026-01-02 00:00:00",
                         device="schematic", channels=(209,), agg=agg)
        )
        assert out["count"] == 1 and out["points"][0]["value"] == want, agg


def test_query_interval_bucketing(db):
    db.upsert(
        [
            ("2026-01-01 00:00:00", "d", 1, "A", 1.0, None, "schematic"),
            ("2026-01-01 00:01:00", "d", 1, "A", 2.0, None, "schematic"),
            ("2026-01-01 00:05:00", "d", 1, "A", 9.0, None, "schematic"),
        ]
    )
    out = db.query_series(
        HistoryQuery(start="2025-12-31 00:00:00", end="2026-01-02 00:00:00",
                     device="d", channels=(1,), agg="avg", interval=300)
    )
    # 300s buckets: [00:00,05:00) -> avg(1,2)=1.5 ; [05:00,10:00) -> avg(9)=9.0
    assert [p["value"] for p in out["points"]] == [1.5, 9.0]


def test_query_interval_last_no_bindings_error(db):
    """Regression (S4N-2-Fix): ``agg='last'`` + ``interval`` used to fail with
    ``Incorrect number of bindings supplied`` (13 vs 11) because the outer
    combo clauses ``m.device=?``/``m.channel=?`` were never bound.  Expect
    exactly one point per bucket, holding the last sample inside it."""
    db.upsert(
        [
            ("2026-01-01 00:00:00", "d", 1, "A", 1.0, None, "json"),
            ("2026-01-01 00:01:00", "d", 1, "A", 1.5, None, "json"),  # same bucket
            ("2026-01-01 00:05:00", "d", 1, "A", 2.0, None, "json"),  # next bucket
        ]
    )
    out = db.query_series(
        HistoryQuery(start="2025-12-31 00:00:00", end="2026-01-02 00:00:00",
                     device="d", channels=(1,), agg="last", interval=300)
    )
    assert out["device"] == "d" and out["channel"] == 1
    assert out["count"] == 2
    assert [p["value"] for p in out["points"]] == [1.5, 2.0]
    # multi-channel + device filter: count is the sum over both combos
    db.upsert(
        [
            ("2026-01-01 00:00:30", "d", 2, "A", 3.0, None, "json"),
            ("2026-01-01 00:06:00", "d", 2, "A", 4.0, None, "json"),
        ]
    )
    out2 = db.query_series(
        HistoryQuery(start="2025-12-31 00:00:00", end="2026-01-02 00:00:00",
                     device="d", channels=(1, 2), agg="last", interval=300)
    )
    assert "d:1" in out2 and "d:2" in out2
    assert out2["_meta"]["count"] == 4
    assert [p["value"] for p in out2["d:2"]["points"]] == [3.0, 4.0]


def test_query_limit_cap(db):
    # 10 distinct samples on ONE channel; limit is applied per series.
    for i in range(10):
        db.upsert(
            [(f"2026-01-01 00:{i:02d}:00", "d", 1, "A", float(i), None, "schematic")]
        )
    out = db.query_series(
        HistoryQuery(start="2025-12-31 00:00:00", end="2026-01-02 00:00:00",
                     device="d", channels=(1,), agg="last", limit=3)
    )
    assert out["count"] == 3
    assert [p["value"] for p in out["points"]] == [7.0, 8.0, 9.0]


def test_query_rejects_bad_agg(db):
    with pytest.raises(ValueError):
        db.query_series(HistoryQuery(start=_ts(-1), end=_ts(1), agg="median"))


def test_retention_cleanup(db):
    db.upsert([
        (_ts(-1000), "d", 1, "A", 1.0, None, "schematic"),  # very old
        (_ts(0), "d", 2, "A", 2.0, None, "schematic"),      # recent
    ])
    deleted = db.retention_cleanup(days=100)
    assert deleted == 1
    assert db.stats()["rows"] == 1


def test_retention_prunes_live_sources_but_keeps_winsol(db):
    """DECISION (human-approved): retention prunes ONLY live-poll sources
    (default schematic/json); historical 'winsol' rows are exempt and must
    survive with the *same* old timestamp."""
    old = _ts(-1000)  # far outside any retention window
    db.upsert([
        (old, "d", 1, "A", 1.0, None, "schematic"),  # old live -> pruned
        (old, "d", 2, "A", 2.0, None, "json"),       # old live -> pruned
        (old, "winsol", 3, "A", 3.0, "degC", "winsol"),  # old historical -> KEPT
        (_ts(0), "d", 4, "A", 4.0, None, "schematic"),   # recent -> kept
    ])
    winsol_before = db._ensure_conn().execute(
        "SELECT COUNT(*) FROM measurements WHERE source='winsol'"
    ).fetchone()[0]
    deleted = db.retention_cleanup(days=100)
    assert deleted == 2  # only the two old live-poll rows
    con = db._ensure_conn()
    assert con.execute(
        "SELECT COUNT(*) FROM measurements WHERE source='winsol'"
    ).fetchone()[0] == winsol_before  # exempt row count unchanged
    # the winsol row is still queryable at its old timestamp
    left = {r[0] for r in con.execute("SELECT source FROM measurements")}
    assert left == {"schematic", "winsol"}


def test_retention_keeps_cmi_log(db):
    """The CMI /LOG mirror (source='cmi-log', namespaced devices cmi-log:<N>)
    is a historical source like 'winsol': it is NOT in HISTORY_PRUNE_SOURCES
    (default 'schematic,json') and must survive retention no matter how old
    its timestamp is."""
    old = _ts(-1000)  # far outside any retention window
    db.upsert([
        (old, "cmi-log:0", 1, "A", 45.0, "\u00b0C", "cmi-log"),   # old -> KEPT
        (old, "cmi-log:2", 6, "A", 30.0, "\u00b0C", "cmi-log"),   # old -> KEPT
        (old, "d", 1, "A", 1.0, None, "schematic"),               # old live -> pruned
        (_ts(0), "d", 2, "A", 2.0, None, "schematic"),            # recent -> kept
    ])
    deleted = db.retention_cleanup(days=100)  # default prune set
    assert deleted == 1
    con = db._ensure_conn()
    kept = con.execute(
        "SELECT device FROM measurements WHERE source='cmi-log'"
    ).fetchall()
    assert {r[0] for r in kept} == {"cmi-log:0", "cmi-log:2"}
    # and an explicit prune set without 'cmi-log' keeps it too
    assert db.retention_cleanup(days=100, prune_sources=frozenset({"schematic"})) == 0


def test_retention_keeps_winsol_with_namespaced_device(db):
    """The namespaced device 'winsol:<N>' must NOT affect retention: the
    exemption keys on source='winsol' (HISTORY_PRUNE_SOURCES), so an old row
    with device='winsol:0' (the authoritative mapping) is still kept."""
    old = _ts(-1000)
    db.upsert([
        (old, "winsol:0", 1, "A", 40.0, "degC", "winsol"),  # namespaced, old -> KEPT
        (old, "winsol:1", 1, "A", 50.0, "degC", "winsol"),  # namespaced, old -> KEPT
        (old, "schematic", 1, "A", 1.0, None, "schematic"),  # old live -> pruned
    ])
    winsol_before = db._ensure_conn().execute(
        "SELECT COUNT(*) FROM measurements WHERE source='winsol'"
    ).fetchone()[0]
    assert winsol_before == 2
    deleted = db.retention_cleanup(days=100)  # default prune set = schematic,json
    assert deleted == 1
    con = db._ensure_conn()
    kept_devices = {r[0] for r in con.execute(
        "SELECT device FROM measurements WHERE source='winsol'")}
    assert kept_devices == {"winsol:0", "winsol:1"}  # both namespaced devices kept


def test_retention_custom_prune_sources_respected(db):
    """HISTORY_PRUNE_SOURCES config is honoured: a source outside the set is
    never pruned, even when it is older than the window."""
    old = _ts(-1000)
    db.upsert([
        (old, "d", 1, "A", 1.0, None, "schematic"),
        (old, "d", 2, "A", 2.0, None, "json"),
    ])
    # custom set: only 'schematic' is pruned -> 'json' row must survive
    assert db.retention_cleanup(days=100, prune_sources=frozenset({"schematic"})) == 1
    con = db._ensure_conn()
    assert con.execute(
        "SELECT source FROM measurements"
    ).fetchone()[0] == "json"
    # empty prune set disables pruning entirely
    assert db.retention_cleanup(days=100, prune_sources=frozenset()) == 0
    assert con.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 1


def test_settings_prune_source_set_parsing():
    """The config property parses the comma-separated env value (and the
    documented default)."""
    from app.config import Settings

    # default (no explicit value) -> {"schematic", "json"}
    assert Settings().prune_source_set == frozenset({"schematic", "json"})
    assert Settings(history_prune_sources="schematic, json").prune_source_set == frozenset(
        {"schematic", "json"}
    )
    assert Settings(history_prune_sources="schematic").prune_source_set == frozenset(
        {"schematic"}
    )
    # blank entries are dropped; fully blank disables pruning
    assert Settings(history_prune_sources=",,").prune_source_set == frozenset()


def test_schematic_rows_builder():
    from app.db import schematic_rows

    p2 = parse_schematic_file(
        DEFAULT_SCHEMATIC_DIR, 2, (DISCOVERY / "live_2.cgi").read_text(encoding="utf-8")
    )
    rows = schematic_rows(p2, now_iso())
    assert rows
    assert all(isinstance(r[4], float) for r in rows)  # all values numeric
    for _ts, device, channel, ad, _value, _unit, source in rows:
        assert device == "schematic"
        assert source == "schematic"
        assert 200 <= channel < 300  # page 2 -> 2xx
        assert ad in ("A", "D")


def test_json_rows_builder():
    from app.db import json_rows

    res = parse_json_api(make_api_fixture(32, "I,O,D,La,Ld"), node=32, params="I,O,D,La,Ld")
    rows = json_rows(res, now_iso())
    assert rows
    assert all(isinstance(r[4], float) for r in rows)  # all values numeric
    for _ts, device, _channel, ad, _value, _unit, source in rows:
        assert device == "json:32" and source == "json"
        assert ad in ("A", "D")


def test_default_limit_constant():
    assert DEFAULT_HISTORY_LIMIT == 5000

