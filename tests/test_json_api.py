"""Unit tests for the JSON-API client (OK + all error statuses)."""

from __future__ import annotations

import httpx
import pytest

from app.cmi.client import CMIClient
from app.cmi.exceptions import CMIJsonApiError, CMIRateLimitedError
from app.cmi.json_api import (
    RAS_STATES,
    UNIT_MAP,
    JsonApiClient,
    JsonApiStatus,
    parse_json_api,
)
from app.cmi.queue import CMIGuard
from tests.conftest import make_api_fixture, make_fake_transport


def _client(transport: httpx.MockTransport) -> CMIClient:
    return CMIClient(
        base_url="http://cmi.test",
        user="admin",
        password="admin",
        transport=transport,
    )


def test_parse_ok_node1():
    payload = make_api_fixture(1, "I,O,D")
    result = parse_json_api(payload, node=1, params="I,O,D")
    assert result.ok
    assert result.status_code == 0
    assert result.header.version == 8
    assert result.header.device_name == "UVR16x2"  # device 0x87
    assert len(result.values["Inputs"]) == 4
    first = result.values["Inputs"][0]
    assert first.number == 1
    assert first.ad == "A"
    assert first.value.value == 92.0
    assert first.value.unit_symbol == "°C"
    assert result.values["Outputs"][0].value.unit_symbol == "Aus/EIN"
    assert result.values["DL-Bus"][1].value.value == 1


def test_parse_ok_node32_recorded_lald():
    payload = make_api_fixture(32, "I,O,D,La,Ld")
    result = parse_json_api(payload, node=32, params="I,O,D,La,Ld")
    assert result.ok
    assert result.header.device_name == "UVR610"  # device 0x91
    la = result.values["Logging Analog"]
    assert len(la) == 16
    # unit mappings confirmed by the recorded live response
    assert la[0].value.unit_symbol == "°C"
    assert la[9].value.unit_symbol == "V"
    # RAS entries: unit 46 -> RAS-state
    ras_entry = la[3]
    assert ras_entry.value.unit_symbol == "°C (RAS)"
    assert ras_entry.value.ras == "0"
    assert ras_entry.value.ras_state == RAS_STATES["0"]
    ld = result.values["Logging Digital"]
    assert len(ld) == 1
    assert ld[0].value.unit_symbol == "Aus/EIN"


def test_parse_designation():
    payload = {
        "Header": {"Version": 8, "Device": "91", "Timestamp": 1},
        "Data": {
            "Inputs": [
                {"Designation": "T.Raum", "Number": 1, "AD": "A",
                 "Value": {"Value": 23.5, "Unit": "1"}},
                {"Designation": "T.Außen", "Number": 2, "AD": "A",
                 "Value": {"Value": 10.2, "Unit": "1"}},
            ]
        },
        "Status": "OK",
        "Status code": 0,
    }
    result = parse_json_api(payload, node=32, params="I")
    assert result.values["Inputs"][0].designation == "T.Raum"


@pytest.mark.parametrize(
    ("code", "exc_type"),
    [
        (1, CMIJsonApiError),
        (2, CMIJsonApiError),
        (3, CMIJsonApiError),
        (5, CMIJsonApiError),
        (6, CMIJsonApiError),
        (7, CMIJsonApiError),
        (99, CMIJsonApiError),
    ],
)
def test_parse_error_status_codes(code, exc_type):
    payload = {"Header": {}, "Data": {}, "Status": "FAIL", "Status code": code}
    with pytest.raises(exc_type) as excinfo:
        parse_json_api(payload, node=1, params="I,O,D")
    assert excinfo.value.status_code == code


def test_parse_rate_limited_raises_cmirate_limited():
    payload = {"Header": {}, "Data": {}, "Status": "TOO MANY REQUESTS", "Status code": 4}
    with pytest.raises(CMIRateLimitedError) as excinfo:
        parse_json_api(payload, node=1, params="I,O,D")
    assert excinfo.value.status_code == 4


@pytest.mark.asyncio
async def test_client_fetch_ok(fake_transport):
    client = _client(fake_transport)
    guard = CMIGuard(api_min_interval_sec=0.01)
    api = JsonApiClient(client, guard)
    result = await api.fetch(32, "La,Ld")
    assert result.ok
    assert len(result.values["Logging Analog"]) == 16
    await client.aclose()


@pytest.mark.asyncio
async def test_client_fetch_rate_limited(fake_transport):

    transport = make_fake_transport(api_status=4)
    client = _client(transport)
    guard = CMIGuard(api_min_interval_sec=0.01)
    api = JsonApiClient(client, guard)
    with pytest.raises(CMIRateLimitedError):
        await api.fetch(1, "I,O,D")
    await client.aclose()


@pytest.mark.asyncio
async def test_client_fetch_invalid_json():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, text="not-json")
    )
    client = _client(transport)
    guard = CMIGuard(api_min_interval_sec=0.01)
    api = JsonApiClient(client, guard)
    with pytest.raises(CMIJsonApiError, match="invalid JSON"):
        await api.fetch(1, "I,O,D")
    await client.aclose()


def test_status_code_description():
    assert "minute" in JsonApiStatus.TOO_MANY_REQUESTS.description
    assert JsonApiStatus.OK.name == "OK"


def test_unit_map_has_documented_ids():
    assert UNIT_MAP["43"] == "Aus/EIN"
    assert UNIT_MAP["46"] == "°C (RAS)"
    assert UNIT_MAP["13"] == "V"
    assert UNIT_MAP["1"] == "°C"
