"""Unit tests for the changer resolver (fixture HTML incl. x2 variant)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.cmi.changer_resolver import ChangerResolver, parse_changer_html
from app.cmi.client import CMIClient
from app.cmi.exceptions import CMIChangerError
from app.cmi.queue import CMIGuard
from tests.conftest import DISCOVERY, make_fake_transport


def _client(tmp_path: Path, transport: httpx.MockTransport | None = None) -> CMIClient:
    return CMIClient(
        base_url="http://cmi.test",
        user="admin",
        password="admin",
        transport=transport,
    )


def test_parse_mode_select_fixture():
    html = (DISCOVERY / "changer_101301D0180.html").read_text(encoding="utf-8")
    info = parse_changer_html(html, display_address="101301D0180", x2=False)
    assert info.change_address == "019001301D"
    assert info.option_type == "select"
    assert [o.value for o in info.options] == ["1", "2", "3", "4", "5", "6", "7"]
    labels = {o.value: o.label for o in info.options}
    assert labels == {
        "1": "STANDBY",
        "2": "ZEIT/AUTO",
        "3": "NORMAL",
        "4": "ABGESENKT",
        "5": "PARTY",
        "6": "URLAUB",
        "7": "FEIERTAG",
    }
    assert info.current_value == "3"
    assert info.options[2].selected is True
    assert info.settable_values == ["1", "2", "3", "4", "5", "6", "7"]


def test_parse_switch_fixture():
    html = (DISCOVERY / "changer_10820A10180.html").read_text(encoding="utf-8")
    info = parse_changer_html(html, display_address="10820A10180", x2=False)
    assert info.change_address == "01910820A1"
    assert info.option_type == "select"
    assert [o.value for o in info.options] == ["1", "2", "3"]
    assert {o.value: o.label for o in info.options} == {"1": "AUS", "2": "EIN", "3": "AUTO"}
    assert info.current_value == "3"


def test_parse_x2_fixture():
    html = (DISCOVERY / "changerx2_10040912091.html").read_text(encoding="utf-8")
    info = parse_changer_html(html, display_address="10040912091", x2=True)
    assert info.x2 is True
    assert info.change_address == "20000040911202"
    assert info.option_type == "select"
    assert {o.value: o.label for o in info.options} == {
        "2": "Auto",
        "0": "Hand/AUS",
        "1": "Hand/EIN",
    }
    assert info.current_value == "2"


def test_parse_numeric_changer():
    html = (
        '<input type="hidden" value="0190000000" name="changeadr" id="changeadr">'
        '<input type="text" name="changeto" id="changeto" min="5" max="95" value="45">'
    )
    info = parse_changer_html(html, display_address="ADDR", x2=False)
    assert info.option_type == "numeric"
    assert info.change_address == "0190000000"
    assert info.min_value == "5"
    assert info.max_value == "95"


# ---------------------------------------------------------------------------
# v0.6-A' numeric read-back (UVR610 Minderer-Sollspannungen, Discovery 18.11)
# ---------------------------------------------------------------------------
def test_parse_numeric_readback_value_attribute():
    """The CMI's own live value of a numeric dialog = the ``value`` attribute
    of the ``<input type="number">`` (real EG changer HTML, page 6, x2)."""
    html = (
        '<input type="hidden" value="2000014414D10D" name="changeadrx2" id="changeadr">'
        "2.00 - 10.00V<br>"
        '<input type="number" name="changetox2" min="2.00" max="10.00" '
        'value="2.00" id="changeto" class="schanger" step="0.01">'
    )
    info = parse_changer_html(html, display_address="10144142091", x2=True)
    assert info.option_type == "numeric"
    assert info.change_address == "2000014414D10D"
    assert info.current_value == "2.00"
    assert info.min_value == "2.00"
    assert info.max_value == "10.00"
    assert info.settable_values == ["2.00", "10.00"]


def test_parse_numeric_readback_name_changeto_nonx2():
    """Non-x2 variant: ``name="changeto"`` (no sadrx2) is recognized too."""
    html = (
        '<input type="hidden" value="2000004414D10D" name="changeadr" id="changeadr">'
        '<input type="number" name="changeto" min="2.00" max="10.00" value="8.25">'
    )
    info = parse_changer_html(html, display_address="10044142091", x2=False)
    assert info.option_type == "numeric"
    assert info.current_value == "8.25"


def test_parse_numeric_readback_without_value_stays_none():
    """Numeric dialog without a ``value`` attribute -> current_value stays
    None (no invented value); min/max are still parsed."""
    html = (
        '<input type="hidden" value="2000024414D10D" name="changeadrx2" id="changeadr">'
        '<input type="number" name="changetox2" min="2.00" max="10.00" id="changeto">'
    )
    info = parse_changer_html(html, display_address="10244142091", x2=True)
    assert info.option_type == "numeric"
    assert info.current_value is None
    assert info.min_value == "2.00"
    assert info.max_value == "10.00"
    assert info.settable_values == ["2.00", "10.00"]


def test_settable_values_numeric_bounds_or_empty():
    from app.cmi.changer_resolver import ChangerInfo

    bounds = ChangerInfo(
        display_address="10144142091",
        x2=True,
        option_type="numeric",
        min_value="2.00",
        max_value="10.00",
    )
    assert bounds.settable_values == ["2.00", "10.00"]
    no_bounds = ChangerInfo(display_address="10144142091", x2=True, option_type="numeric")
    assert no_bounds.settable_values == []


def test_parse_time_changer():
    html = (
        '<input type="hidden" value="0190000000" name="changeadr" id="changeadr">'
        '<input type="text" name="changetotimeh" id="changetotimeh" value="20">'
        '<input type="text" name="changetotimem" id="changetotimem" value="30">'
    )
    info = parse_changer_html(html, display_address="ADDR", x2=False)
    assert info.option_type == "time"


def test_parse_date_changer():
    html = (
        '<input type="hidden" value="0190000000" name="changeadr" id="changeadr">'
        '<input type="text" name="changetodatey" id="changetodatey" value="2026">'
        '<input type="text" name="changetodatem" id="changetodatem" value="10">'
        '<input type="text" name="changetodated" id="changetodated" value="27">'
    )
    info = parse_changer_html(html, display_address="ADDR", x2=False)
    assert info.option_type == "date"


def test_parse_without_hidden_input_raises():
    html = "<select><option>1</option></select>"
    with pytest.raises(CMIChangerError, match="no hidden changeadr"):
        parse_changer_html(html, display_address="X", x2=False)


@pytest.mark.asyncio
async def test_resolver_fetches_and_caches_in_memory(tmp_path):
    transport = make_fake_transport()
    client = _client(tmp_path, transport)
    guard = CMIGuard(api_min_interval_sec=0.01)
    resolver = ChangerResolver(client, guard, ttl_sec=3600, cache_dir=tmp_path / "cache")

    info1 = await resolver.resolve("101301D0180")
    info2 = await resolver.resolve("101301D0180")
    assert info1.change_address == "019001301D"
    assert info2 is info1 or info2.change_address == info1.change_address
    assert info1.source == "cmi"

    # file cache written
    cache_files = list((tmp_path / "cache").glob("*.json"))
    assert len(cache_files) == 1
    assert "change_address" in json.loads(cache_files[0].read_text())

    await client.aclose()


@pytest.mark.asyncio
async def test_resolver_ttl_expiry(tmp_path):
    transport = make_fake_transport()
    client = _client(tmp_path, transport)
    guard = CMIGuard(api_min_interval_sec=0.01)
    fake_time = {"t": 1000.0}

    def clock() -> float:
        return fake_time["t"]

    resolver = ChangerResolver(
        client, guard, ttl_sec=100, cache_dir=None, clock=clock
    )
    i1 = await resolver.resolve("10820A10180")
    fake_time["t"] += 50
    i2 = await resolver.resolve("10820A10180")  # still fresh
    assert i2 is i1 or i2.resolved_at == i1.resolved_at
    fake_time["t"] += 200  # TTL passed
    i3 = await resolver.resolve("10820A10180")
    assert i3.resolved_at > i1.resolved_at
    await client.aclose()


@pytest.mark.asyncio
async def test_resolver_refresh_flag(tmp_path):
    transport = make_fake_transport()
    client = _client(tmp_path, transport)
    guard = CMIGuard(api_min_interval_sec=0.01)
    resolver = ChangerResolver(client, guard, ttl_sec=3600, cache_dir=None)
    i1 = await resolver.resolve("10820A10180")
    i2 = await resolver.resolve("10820A10180", refresh=True)
    assert i2.resolved_at >= i1.resolved_at
    await client.aclose()


@pytest.mark.asyncio
async def test_resolver_concurrent_dedup(tmp_path):
    transport = make_fake_transport()
    client = _client(tmp_path, transport)
    guard = CMIGuard(api_min_interval_sec=0.01)
    resolver = ChangerResolver(client, guard, ttl_sec=3600, cache_dir=None)
    import asyncio

    results = await asyncio.gather(
        resolver.resolve("101301D0180"),
        resolver.resolve("101301D0180"),
        resolver.resolve("101301D0180"),
    )
    assert all(r.change_address == "019001301D" for r in results)
    await client.aclose()


@pytest.mark.asyncio
async def test_resolve_many(tmp_path):
    transport = make_fake_transport()
    client = _client(tmp_path, transport)
    guard = CMIGuard(api_min_interval_sec=0.01)
    resolver = ChangerResolver(client, guard, ttl_sec=3600, cache_dir=tmp_path / "cache")
    results = await resolver.resolve_many(
        {"101301D0180": False, "10820A10180": False, "10040912091": True}
    )
    assert set(results) == {"101301D0180", "10820A10180", "10040912091"}
    assert results["10040912091"].x2 is True
    assert results["101301D0180"].change_address == "019001301D"
    await client.aclose()


@pytest.mark.asyncio
async def test_resolver_file_cache_read(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    from app.cmi.changer_resolver import ChangerInfo

    cached = ChangerInfo(
        display_address="10820A10180",
        x2=False,
        change_address="01910820A1",
        option_type="select",
        options=[],
        resolved_at=5000.0,
        source="cache",
    )
    (cache_dir / "10820A10180.json").write_text(json.dumps(cached.model_dump()))

    transport = make_fake_transport()
    client = _client(tmp_path, transport)
    guard = CMIGuard(api_min_interval_sec=0.01)
    fake_time = {"t": 5100.0}
    resolver = ChangerResolver(
        client, guard, ttl_sec=3600, cache_dir=cache_dir, clock=lambda: fake_time["t"]
    )
    info = await resolver.resolve("10820A10180")
    assert info.source == "cache"
    assert info.change_address == "01910820A1"
    await client.aclose()
