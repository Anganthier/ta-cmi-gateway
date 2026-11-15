"""Unit tests for the StateStore (schematic + JSON-API merge, health flags)."""

from __future__ import annotations

from app.cmi.json_api import parse_json_api
from app.cmi.schematic_parser import DEFAULT_SCHEMATIC_DIR, parse_schematic_file
from app.state import (
    SOURCE_JSON_API,
    SOURCE_SCHEMATIC,
    CmiHealth,
    StateStore,
    _json_fields,
)
from tests.conftest import DISCOVERY, make_api_fixture


def _parse_page(page: int):
    html = (DISCOVERY / f"live_{page}.cgi").read_text(encoding="utf-8")
    return parse_schematic_file(DEFAULT_SCHEMATIC_DIR, page, html)


def test_initial_state_is_stale():
    store = StateStore()
    doc = store.snapshot()
    assert doc.health.stale is True
    assert doc.fields == []
    assert doc.schematics == {}


def test_update_schematic_updates_health_and_fields():
    store = StateStore(cmi_url="http://cmi.test")
    page1 = _parse_page(1)
    store.update_schematic(page1, ts=1000.0)
    doc = store.snapshot()
    assert doc.health.schematic_ok is True
    assert doc.health.schematic_time == 1000.0
    assert doc.health.stale is False  # schematic route ok
    assert 1 in doc.schematics
    assert doc.schematics[1].page == 1
    schematic_fields = doc.fields_by_source(SOURCE_SCHEMATIC)
    assert schematic_fields, "expected schematic fields"
    assert all(f.timestamp == 1000.0 for f in schematic_fields)
    # mode select field keeps its address
    mode_fields = [f for f in schematic_fields if f.address == "101301D0180"]
    assert mode_fields and mode_fields[0].raw == "NORMAL"
    assert mode_fields[0].value is None


def test_update_schematic_failed_keeps_last_values():
    store = StateStore()
    page1 = _parse_page(1)
    store.update_schematic(page1, ts=1000.0)
    store.update_schematic_failed("timeout", ts=2000.0)
    doc = store.snapshot()
    assert doc.health.schematic_ok is False
    assert doc.health.schematic_error == "timeout"
    # last values are kept
    assert 1 in doc.schematics
    assert doc.fields_by_source(SOURCE_SCHEMATIC)


def test_mark_schematic_ping_does_not_replace_values():
    store = StateStore()
    store.mark_schematic_ping(True, ts=1.0)
    assert store.health.schematic_ok is True
    assert store.snapshot().schematics == {}
    store.mark_schematic_ping(False, error="unreachable", ts=2.0)
    assert store.health.schematic_ok is False
    assert store.health.schematic_error == "unreachable"


def test_node1_empty_dbus_merge_no_phantom_entries():
    """Defect 5c: the real UVR1611 accepts ``D`` but returns an empty DL-Bus.

    The fixture variant ``node1_empty_d=True`` must merge without crashing and
    must not invent any DL-Bus state fields (no phantom entries).
    """
    payload = make_api_fixture(1, "I,O,D", node1_empty_d=True)
    # Sanity: the fixture really has no DL-Bus entries.
    assert payload["Data"].get("DL-Bus") == []

    result = parse_json_api(payload, node=1, params="I,O,D")
    assert result.ok
    store = StateStore(cmi_url="http://cmi.test")
    store.update_api(result, ts=200.0)
    doc = store.snapshot()

    api_fields = doc.fields_by_source(SOURCE_JSON_API)
    # No DL-Bus fields at all for node 1 with the empty fixture.
    dl_fields = [f for f in api_fields if "dl-bus" in f.id]
    assert dl_fields == [], f"phantom DL-Bus entries: {dl_fields}"
    # Inputs + Outputs are still present (4 + 4).
    assert len(api_fields) == 8, f"expected 8 fields (4 I + 4 O), got {len(api_fields)}"
    assert doc.health.api_ok is True


def test_update_api_merges_node_fields():
    store = StateStore()
    result = parse_json_api(make_api_fixture(32, "I,O,D,La,Ld"), node=32, params="I,O,D,La,Ld")
    store.update_api(result, ts=5000.0)
    doc = store.snapshot()
    api_fields = doc.fields_by_source(SOURCE_JSON_API)
    assert len(api_fields) == 4 + 4 + 2 + 16 + 1  # I + O + D + La + Ld
    by_id = {f.id: f for f in api_fields}
    assert by_id["api:32:logging analog:1"].label.startswith("Node 32")
    assert by_id["api:32:logging analog:4"].ras_state == "Zeit/Auto"
    assert by_id["api:32:inputs:1"].unit == "°C"
    assert all(f.timestamp == 5000.0 for f in api_fields)
    assert doc.health.api_ok is True


def test_update_api_failed():
    store = StateStore()
    store.update_api_failed(1, "can busy", ts=3.0)
    assert store.health.api_ok is False
    assert "node 1" in store.health.api_error


def test_full_merge_5_pages_plus_nodes():
    store = StateStore(cmi_url="http://cmi.test")
    for page in range(1, 6):
        store.update_schematic(_parse_page(page), ts=100.0)
    store.update_api(parse_json_api(make_api_fixture(1, "I,O,D"), node=1, params="I,O,D"), ts=101.0)
    store.update_api(
        parse_json_api(make_api_fixture(32, "I,O,D,La,Ld"), node=32, params="I,O,D,La,Ld"),
        ts=102.0,
    )
    doc = store.snapshot()
    assert set(doc.schematics) == {1, 2, 3, 4, 5}
    assert doc.health.schematic_ok and doc.health.api_ok
    assert not doc.health.stale
    ids = [f.id for f in doc.fields]
    assert len(ids) == len(set(ids)), "field ids must be unique"
    # all schematics contribute fields
    assert sum(1 for f in doc.fields if f.source == SOURCE_SCHEMATIC) >= 20
    assert sum(1 for f in doc.fields if f.source == SOURCE_JSON_API) >= 20


def test_snapshot_isolated_from_store_changes():
    store = StateStore()
    store.update_schematic(_parse_page(1), ts=1.0)
    doc = store.snapshot()
    n_before = len(store.snapshot().fields)
    # mutating the document must not affect the store
    doc.health.schematic_error = "mutated"
    doc.fields.clear()
    assert store.health.schematic_error is None
    assert len(store.snapshot().fields) == n_before


def test_json_fields_no_sections():
    result = parse_json_api(make_api_fixture(1, "Sg"), node=1, params="Sg")
    assert _json_fields(result) == []  # unknown section -> no fields


def test_cmi_health_stale_property():
    health = CmiHealth(schematic_ok=False, api_ok=False)
    assert health.stale is True
    health.api_ok = True
    assert health.stale is False
