"""Unit tests for the schematic parser (all 5 recorded pages)."""

from __future__ import annotations

import pytest

from app.cmi.exceptions import CMISchematicError
from app.cmi.schematic_parser import (
    DEFAULT_SCHEMATIC_DIR,
    SCHEMATIC_PAGES,
    parse_schematic_file,
    parse_schematic_html,
    parse_schematic_layout,
)
from tests.conftest import DISCOVERY


def _page(page: int):
    html = (DISCOVERY / f"live_{page}.cgi").read_text(encoding="utf-8")
    return parse_schematic_file(DEFAULT_SCHEMATIC_DIR, page, html)


def test_page1_all_positions_parsed():
    result = _page(1)
    assert result.page == 1
    assert result.title == "Start"
    assert len(result.positions) == 34
    by_index = {p.index: p for p in result.positions}
    # clock / date
    assert by_index[0].pos_type == "time"
    assert by_index[0].text == "18:34:55"
    assert by_index[0].value is None
    assert by_index[1].pos_type == "date"
    assert by_index[1].text == "26.10.2026"
    # labels (static text)
    assert by_index[9].pos_type == "sensor_label"
    assert by_index[9].text == "Betriebsmodus"
    # settable: operating mode select
    assert by_index[10].pos_type == "mode_select"
    assert by_index[10].address == "101301D0180"
    assert by_index[10].text == "NORMAL"
    assert not by_index[10].x2
    # settable: x2 switch
    assert by_index[13].pos_type == "switch"
    assert by_index[13].address == "10040912091"
    assert by_index[13].x2 is True
    assert by_index[13].text == "Auto"
    # navigation hotspots
    for idx in range(27, 34):
        assert by_index[idx].pos_type == "navigation", idx
        assert by_index[idx].text == ""
    # coordinates merged from 1.html
    assert by_index[10].top == 56.0
    assert by_index[10].left == 288.0


def test_page2_temperatures_and_digital_states():
    result = _page(2)
    assert len(result.positions) == 29
    by_index = {p.index: p for p in result.positions}
    # German decimal comma
    assert by_index[9].pos_type == "sensor_value"
    assert by_index[9].value == 75.0
    assert by_index[9].unit == "°C"
    assert by_index[10].value == 74.4
    assert by_index[11].value == 52.8
    assert by_index[12].value == 34.3
    # digital states
    assert by_index[13].pos_type == "sensor_value"
    assert by_index[13].value is None
    assert by_index[13].text == "AUS"
    # settable addresses
    assert by_index[15].address == "10620A10180"
    assert by_index[16].address == "10220A10180"
    assert by_index[20].address == "10720A10180"


def test_page3_mixed_decimals_and_voltages():
    result = _page(3)
    assert len(result.positions) == 43
    by_index = {p.index: p for p in result.positions}
    # comma vs dot decimals must both parse
    assert by_index[9].value == 11.3
    assert by_index[10].value == 19.9
    assert by_index[16].value == 48.7
    # volts
    assert by_index[19].value == 0.0
    assert by_index[19].unit == "V"
    assert by_index[20].value == 10.0
    assert by_index[20].unit == "V"
    # RL/VL are labels, not values
    assert by_index[22].pos_type == "sensor_label"
    assert by_index[22].text == "RL"
    # bottom labels
    assert by_index[27].text == "Raumtemperatur"
    assert by_index[29].text == "Soll VL Temp"
    assert by_index[30].text == "Aussen Temp."
    # settable on page 3
    assert by_index[32].address == "101301D0180"
    assert by_index[33].address == "10820A10180"


def test_page4_solar_off_state_and_x2():
    result = _page(4)
    assert len(result.positions) == 23
    by_index = {p.index: p for p in result.positions}
    assert by_index[13].pos_type == "sensor_value"
    assert by_index[13].text == "OFF"
    assert by_index[13].value is None
    assert by_index[14].pos_type == "switch"
    assert by_index[14].address == "10040912091"
    assert by_index[14].x2 is True
    # dot decimals
    assert by_index[9].value == 11.2
    assert by_index[10].value == 75.1


def test_page5_kamin():
    result = _page(5)
    assert len(result.positions) == 26
    by_index = {p.index: p for p in result.positions}
    assert by_index[15].text == "EIN"
    assert by_index[15].pos_type == "sensor_value"
    assert by_index[16].address == "10120A10180"
    assert by_index[16].text == "AUTO/EIN"
    assert by_index[18].value == 67.7
    assert by_index[18].unit == "°C"
    # "Min 50C" must NOT be parsed as a value
    assert by_index[14].pos_type == "sensor_label"
    assert by_index[14].value is None


def test_unknown_page_raises():
    with pytest.raises(CMISchematicError):
        parse_schematic_html("<div id='pos0'>x</div>", page=99)


def test_parse_schematic_layout():
    layout = (DEFAULT_SCHEMATIC_DIR / "1.html").read_text(encoding="utf-8")
    coords = parse_schematic_layout(layout)
    assert 0 in coords and 27 in coords
    assert coords[0].top == 13.0
    assert coords[0].left == 1408.0
    assert coords[27].top == 67.0
    assert coords[27].left == 16.0
    # hotspots carry width/height too, but we only need top/left
    assert coords[9].top == 19.0
    assert coords[9].left == 286.0


def test_all_schematic_value_blocks_have_human_label():
    """S1-1 DoD: every value block carries a human label, not S{page}.{pos}."""
    from app.state import StateStore

    store = StateStore(cmi_url="http://cmi.test")
    for page in range(1, 6):
        html = (DISCOVERY / f"live_{page}.cgi").read_text(encoding="utf-8")
        store.update_schematic(parse_schematic_file(DEFAULT_SCHEMATIC_DIR, page, html),
                               ts=1000.0)
    doc = store.snapshot()
    schematic_fields = [f for f in doc.fields if f.source == "schematic"]
    assert len(schematic_fields) == 49, (
        f"expected 49 value blocks, got {len(schematic_fields)}"
    )
    # The state layer falls back to a bare "S{page}.{pos}" id only when no human
    # label was resolved.  Detect exactly that pattern (NOT startswith("S"),
    # which would also match real labels like "Speicher Oben" / "Solarpumpe").
    import re

    bare = [
        f for f in schematic_fields if re.fullmatch(r"S\d+\.\d+", f.label)
    ]
    assert not bare, (
        f"value blocks still carry bare S-ids: {[(f.id, f.label) for f in bare]}"
    )
    # spot-check against the live fixture text / WinSol channel names
    by_ref = {f.reference: f.label for f in schematic_fields}
    assert by_ref["page=1;pos=10"] == "Betriebsmodus"
    assert by_ref["page=2;pos=9"] == "Speicher Oben"
    assert by_ref["page=5;pos=12"] == "Abgas"
    assert by_ref["page=4;pos=9"] == "Kollektor"
    assert by_ref["page=5;pos=18"] == "Holzkessel Vorlauf"
    assert by_ref["page=3;pos=32"] == "Betriebsmodus"
    # parser-level: every VALUE pos has pos.label set
    for page in range(1, 6):
        result = _page(page)
        for pos in result.positions:
            if pos.pos_type in ("sensor_value", "mode_select", "switch"):
                assert pos.label, f"p{page} pos{pos.index} ({pos.text!r}) has no label"


def test_all_pages_have_coordinates():
    for page in SCHEMATIC_PAGES:
        result = _page(page)
        missing = [p.index for p in result.positions if p.top is None]
        # every pos present in the layout file gets coordinates
        layout = (DEFAULT_SCHEMATIC_DIR / f"{page}.html").read_text(encoding="utf-8")
        assert parse_schematic_layout(layout)
        assert not missing, f"page {page} missing coords for {missing}"


def test_classify_hotspot():
    from app.cmi.schematic_parser import classify_hotspot

    assert classify_hotspot(' class="durchsichtig" onClick="reload();"', "\n")
    assert not classify_hotspot(' class="changex2" adresse="1"', "Auto")
    assert not classify_hotspot("", "text")
