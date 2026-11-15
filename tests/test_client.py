"""Unit tests for the CMI HTTP client (error mapping, auth, allow-list)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.cmi.client import CMIClient
from app.cmi.exceptions import CMIAuthError, CMIConnectionError, CMIGatewayError


def _client(transport: httpx.MockTransport, **kwargs) -> CMIClient:
    return CMIClient(
        base_url="http://cmi.test",
        user="admin",
        password="admin",
        transport=transport,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_get_schematic_ok():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text="<div id='pos0'>1</div>"))
    async with _client(transport) as client:
        html = await client.get_schematic(1)
        assert "pos0" in html


@pytest.mark.asyncio
async def test_get_schematic_404_raises():
    transport = httpx.MockTransport(lambda r: httpx.Response(404, text="not found"))
    client = _client(transport)
    with pytest.raises(CMIGatewayError, match="404"):
        await client.get_schematic(99)
    await client.aclose()


@pytest.mark.asyncio
async def test_401_raises_auth_error():
    transport = httpx.MockTransport(lambda r: httpx.Response(401, text="nope"))
    client = _client(transport)
    with pytest.raises(CMIAuthError, match="401"):
        await client.get_schematic(1)
    await client.aclose()


@pytest.mark.asyncio
async def test_500_raises_gateway_error():
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="server broke"))
    client = _client(transport)
    with pytest.raises(CMIGatewayError, match="500"):
        await client.get_schematic(1)
    await client.aclose()


@pytest.mark.asyncio
async def test_connection_error_mapping():
    class RefusingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

    client = _client(RefusingTransport())
    with pytest.raises(CMIConnectionError, match="connection error"):
        await client.get_schematic(1)
    await client.aclose()


@pytest.mark.asyncio
async def test_write_endpoint_blocked():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=""))
    client = _client(transport)
    with pytest.raises(PermissionError, match="not allowed"):
        # change.cgi is not in the allow-list (S1 is read-only)
        await client._get("/INCLUDE/change.cgi", {"changeadr": "1", "changeto": "1"})
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/INCLUDE/change.cgi",          # write endpoint
        "/INCLUDE/checkbuttondo.cgi",   # write confirmation
        "/schematic_files/..%2Fpasswd", # traversal (non-digit page)
        "/INCLUDE/whatever.cgi",        # unknown endpoint
    ],
)
async def test_allow_list_enforced_at_raw_choke_point(path):
    """Defect 3: the allow-list must be enforced at the single choke point
    (:meth:`CMIClient._get_raw`), so a forbidden path raises
    :class:`PermissionError` no matter which public entry point built the URL.

    ``get_schematic`` and ``get_api`` call ``_get_raw`` directly (previously
    bypassing the ``_get`` check); now every request is gated here.
    """
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
    client = _client(transport)
    with pytest.raises(PermissionError, match="not allowed"):
        await client._get_raw(path)
    await client.aclose()


@pytest.mark.asyncio
async def test_schematic_and_api_paths_still_allowed():
    """The legitimate read-only paths (schematic pages + api.cgi) are NOT
    blocked by the allow-list -- only write/unknown endpoints are."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, text="ok")

    client = _client(httpx.MockTransport(handler))
    await client.get_schematic(1)          # /schematic_files/1.cgi
    await client.get_api({"jsonnode": "1", "jsonparam": "I"})  # /INCLUDE/api.cgi?...
    await client._get_raw("/INCLUDE/changer.cgi?x=1")          # query-stripped
    await client.aclose()
    assert "/schematic_files/1.cgi" in seen
    assert "/INCLUDE/api.cgi" in [p.split("?")[0] for p in seen]
    assert "/INCLUDE/changer.cgi" in [p.split("?")[0] for p in seen]


@pytest.mark.asyncio
async def test_basic_auth_header_sent():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, text="ok")

    client = _client(httpx.MockTransport(handler))
    await client.get_schematic(1)
    assert captured["auth"].startswith("Basic ")
    await client.aclose()


@pytest.mark.asyncio
async def test_api_query_uses_literal_commas():
    """The CMI CGI rejects %2C-encoded commas in jsonparam (SYNTAX ERROR 3)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.raw_path.decode()
        return httpx.Response(200, text=json.dumps({"Header": {}, "Data": {}, "Status": "OK", "Status code": 0}))

    client = _client(httpx.MockTransport(handler))
    await client.get_api({"jsonnode": "32", "jsonparam": "I,O,D,La,Ld"})
    assert "jsonparam=I%2CO%2CD" not in seen["path"]
    assert "jsonparam=I,O,D,La,Ld" in seen["path"]
    await client.aclose()


def test_build_client_from_settings(settings):
    from app.cmi.client import build_client

    client = build_client(settings)
    assert client._base_url == settings.base_url
    assert "Basic" in client._auth._auth_header
