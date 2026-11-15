"""Tests for the isolated CMI write client (:mod:`app.cmi.write`).

All CMI traffic goes through :class:`httpx.MockTransport` with the recorded
``discovery/`` fixtures or small inline HTML -- no network, no live CMI.

Coverage:

* no-op logic: current value == changeto, read-back comparison, strict
  drift rejection, x2 parameters;
* safety: RISK address never written, non-whitelisted addresses refused,
  no write when the current value is not unambiguously readable;
* ``changeto`` sanitisation: only option values / [min, max] numbers;
* endpoint whitelists: the write client may only touch ``change.cgi``; the
  read client may not;
* inventory helpers: address collection from the 5 live pages, inventory
  entry assembly (risk / not-readable / writeable), option formatting.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.cmi.changer_resolver import parse_changer_html
from app.cmi.exceptions import CMISafetyError
from app.cmi.write import (
    WRITABLE_ADDRESSES,
    AddressPresence,
    CMIWriteClient,
    Readback,
    _extract_current,
    _request_url,
    build_inventory_entry,
    collect_settable_addresses,
    format_options,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
#: Recorded CMI live pages. Public repo ships them under tests/fixtures; an
#: internal tree with discovery/ takes precedence when present.
_DISCOVERY_DIR = REPO_ROOT / "discovery"
DISCOVERY = _DISCOVERY_DIR if _DISCOVERY_DIR.is_dir() else REPO_ROOT / "tests" / "fixtures"
LIVE_PAGES = {p: (DISCOVERY / f"live_{p}.cgi").read_text(encoding="utf-8") for p in (1, 2, 3, 4, 5)}

MODE_ADDR = "101301D0180"
SWITCH_ADDR = "10820A10180"
RISK_ADDR = "10520A10180"
X2_ADDR = "10040912091"


# ---------------------------------------------------------------------------
# Mock CMI
# ---------------------------------------------------------------------------
class FakeCMI:
    """Stateful MockTransport serving schematic pages + changer + change.cgi.

    ``change.cgi`` calls are recorded; after a recorded call the changer
    response switches to ``post_write_html`` (simulating the CMI's new state)
    when set, otherwise it keeps serving the pre-write fixture.
    """

    def __init__(
        self,
        *,
        schematic_html: str = "",
        changer_html: str = "",
        post_write_html: str | None = None,
        x2: bool = False,
    ) -> None:
        self.schematic_html = schematic_html
        self.changer_html = changer_html
        self.post_write_html = post_write_html if post_write_html is not None else changer_html
        self.x2 = x2
        self.change_calls: list[dict[str, str]] = []
        self.paths: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        params = dict(request.url.params)

        if path.startswith("/schematic_files/"):
            return httpx.Response(200, text=self.schematic_html)

        if path in ("/INCLUDE/changer.cgi", "/INCLUDE/changerx2.cgi"):
            html = self.post_write_html if self.change_calls else self.changer_html
            return httpx.Response(200, text=html)

        if path == "/INCLUDE/change.cgi":
            self.change_calls.append(params)
            return httpx.Response(200, text="OK")

        return httpx.Response(404, text="unknown endpoint")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def _schematic_with_mode(text: str = "NORMAL") -> str:
    return f'<div id="pos10"><a href="javascript:loadChanger(\'{MODE_ADDR}\');">{text}</a></div>'


def _schematic_with_switch(text: str = "AUTO/EIN") -> str:
    return f'<div id="pos11"><a href="javascript:loadChanger(\'{SWITCH_ADDR}\');">{text}</a></div>'


def _schematic_with_risk(text: str = "AUTO/AUS") -> str:
    return f'<div id="pos23"><a href="javascript:loadChanger(\'{RISK_ADDR}\');">{text}</a></div>'


def _schematic_with_x2(text: str = "Auto") -> str:
    return f'<div id="pos13" class="changex2" adresse="{X2_ADDR}">{text}</div>'


def _mode_changer(selected_value: str = "3") -> str:
    opts = (
        '<option value="1">STANDBY</option>'
        f'<option value="2">ZEIT/AUTO</option>'
        f'<option value="3"{' selected="selected"' if selected_value == "3" else ""}>NORMAL</option>'
        f'<option value="4"{' selected="selected"' if selected_value == "4" else ""}>ABGESENKT</option>'
        '<option value="5">PARTY</option>'
        '<option value="6">URLAUB</option>'
        '<option value="7">FEIERTAG</option>'
    )
    return (
        '<input type="hidden" value="019001301D" name="changeadr" id="changeadr">'
        f'<select name="changeto" class="schanger" id="changeto">{opts}</select>'
    )


def _switch_changer(selected_value: str = "3") -> str:
    opts = (
        f'<option value="1"{' selected="selected"' if selected_value == "1" else ""}>AUS</option>'
        f'<option value="2"{' selected="selected"' if selected_value == "2" else ""}>EIN</option>'
        f'<option value="3"{' selected="selected"' if selected_value == "3" else ""}>AUTO</option>'
    )
    return (
        '<input type="hidden" value="01910820A1" name="changeadr" id="changeadr">'
        f'<select name="changeto" class="schanger" id="changeto">{opts}</select>'
    )


def _x2_changer(selected_value: str = "2") -> str:
    options = (("2", "Auto"), ("0", "Hand/AUS"), ("1", "Hand/EIN"))
    opts = "".join(
        f'<option{' selected="selected"' if value == selected_value else ""} value="{value}">{label}</option>'
        for value, label in options
    )
    return (
        '<input type="hidden" value="20000040911202" name="changeadrx2" id="changeadr">'
        f'<select id="changeto" class="schanger" name="changetox2">{opts}</select>'
    )


def _client(transport: httpx.MockTransport) -> CMIWriteClient:
    return CMIWriteClient(
        "http://cmi.test", "admin", "admin", transport=transport, readback_delay_sec=0.0
    )


# ---------------------------------------------------------------------------
# No-op write
# ---------------------------------------------------------------------------
async def test_noop_write_keeps_value_and_proves_path():
    """The full change.cgi path: before == changeto == after (mocked)."""
    fake = FakeCMI(schematic_html=_schematic_with_mode("NORMAL"), changer_html=_mode_changer("3"))
    async with _client(fake.transport()) as client:
        result = await client.noop_write(MODE_ADDR)

    assert fake.change_calls == [{"changeadr": "019001301D", "changeto": "3"}]
    assert result.before.current_value == "3"
    assert result.before.current_label == "NORMAL"
    assert result.changeto == "3"
    assert result.after.current_value == "3"
    assert result.value_changed is False
    assert result.label_changed is False
    assert result.noop_ok is True
    assert result.response_body == "OK"
    assert result.request == "GET /INCLUDE/change.cgi?changeadr=019001301D&changeto=3"
    # read-back endpoints were used, exactly one write endpoint call
    assert "/INCLUDE/change.cgi" in fake.paths
    assert fake.paths.count("/INCLUDE/change.cgi") == 1
    assert fake.paths.count("/INCLUDE/changer.cgi") == 2  # before + after


async def test_noop_write_x2_uses_x2_params():
    fake = FakeCMI(schematic_html=_schematic_with_x2("Auto"), changer_html=_x2_changer("2"))
    async with _client(fake.transport()) as client:
        result = await client.noop_write(X2_ADDR)

    assert fake.change_calls == [{"changeadrx2": "20000040911202", "changetox2": "2"}]
    assert result.noop_ok is True
    assert result.x2 is True


async def test_write_applies_new_value_with_readback():
    """A real value change (mocked): before=AUTO(3), write EIN(2), after=EIN(2)."""
    fake = FakeCMI(
        schematic_html=_schematic_with_switch("AUTO/EIN"),
        changer_html=_switch_changer("3"),
        post_write_html=_switch_changer("2"),
    )
    async with _client(fake.transport()) as client:
        result = await client.write(SWITCH_ADDR, "2")

    assert fake.change_calls == [{"changeadr": "01910820A1", "changeto": "2"}]
    assert result.applied is True
    assert result.after.current_value == "2"
    assert result.after.current_label == "EIN"


async def test_write_strict_rejects_readback_mismatch():
    """The CMI 'changes' something else -> strict mode raises CMISafetyError."""
    fake = FakeCMI(
        schematic_html=_schematic_with_switch("AUTO/EIN"),
        changer_html=_switch_changer("3"),
        post_write_html=_switch_changer("1"),  # CMI somehow shows AUS after writing AUTO
    )
    async with _client(fake.transport()) as client:
        with pytest.raises(CMISafetyError, match="read-back mismatch"):
            await client.write(SWITCH_ADDR, "3")
    assert fake.change_calls  # the write itself was attempted exactly once
    assert len(fake.change_calls) == 1


async def test_write_rejects_value_not_in_option_list():
    fake = FakeCMI(schematic_html=_schematic_with_mode("NORMAL"), changer_html=_mode_changer("3"))
    async with _client(fake.transport()) as client:
        with pytest.raises(CMISafetyError, match="not one of the allowed options"):
            await client.write(MODE_ADDR, "42")
    assert fake.change_calls == []  # no write happened


async def test_write_rejects_empty_value():
    fake = FakeCMI(schematic_html=_schematic_with_mode("NORMAL"), changer_html=_mode_changer("3"))
    async with _client(fake.transport()) as client:
        with pytest.raises(CMISafetyError, match="non-empty"):
            await client.write(MODE_ADDR, "")
    assert fake.change_calls == []


# ---------------------------------------------------------------------------
# Safety gates
# ---------------------------------------------------------------------------
async def test_risk_address_never_written_even_noop():
    fake = FakeCMI(
        schematic_html=_schematic_with_risk("AUTO/AUS"), changer_html=_switch_changer("3")
    )
    async with _client(fake.transport()) as client:
        with pytest.raises(CMISafetyError, match="RISK"):
            await client.noop_write(RISK_ADDR)
    assert fake.change_calls == []  # no write at all


async def test_risk_address_readable_for_inventory():
    """RISK addresses may still be resolved and read (inventory)."""
    fake = FakeCMI(
        schematic_html=_schematic_with_risk("AUTO/AUS"), changer_html=_switch_changer("3")
    )
    async with _client(fake.transport()) as client:
        readback = await client.read_current(RISK_ADDR)
    assert readback.current_value == "3"
    assert readback.current_label == "AUTO"
    assert fake.change_calls == []


async def test_non_whitelisted_address_refused():
    fake = FakeCMI(schematic_html="", changer_html=_mode_changer("3"))
    async with _client(fake.transport()) as client:
        with pytest.raises(CMISafetyError, match="whitelist"):
            await client.noop_write("999999D0000")
        with pytest.raises(CMISafetyError, match="whitelist"):
            await client.write("999999D0000", "3")
    assert fake.change_calls == []


async def test_no_write_when_current_value_not_readable():
    """Changer without a selected option + schematic text matching no option
    -> the current value is unreadable -> the write is refused (0 calls)."""
    changer_no_selected = (
        '<input type="hidden" value="01910820A1" name="changeadr" id="changeadr">'
        '<select name="changeto" class="schanger" id="changeto">'
        '<option value="1">AUS</option>'
        '<option value="2">EIN</option>'
        '<option value="3">AUTO</option>'
        "</select>"
    )
    fake = FakeCMI(
        schematic_html=_schematic_with_switch("SOMETHING"), changer_html=changer_no_selected
    )
    async with _client(fake.transport()) as client:
        readback = await client.read_current(SWITCH_ADDR)
        assert readback.current_value is None
        with pytest.raises(CMISafetyError, match="not unambiguously readable"):
            await client.noop_write(SWITCH_ADDR)
    assert fake.change_calls == []


# ---------------------------------------------------------------------------
# Endpoint whitelists
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "/INCLUDE/api.cgi",  # 1/min JSON-API: not a write-client endpoint
        "/INCLUDE/checkbuttondo.cgi",  # write confirmation: not allowed
        "/INCLUDE/whatever.cgi",  # unknown endpoint
        "/LOG/info2026.log",  # log endpoints: not a write-client endpoint
    ],
)
async def test_write_client_blocks_non_whitelisted_endpoints(path):
    """The write client's allow-list is exactly change.cgi + the two
    read-back changer endpoints (+ base-class schematic pages)."""
    fake = FakeCMI(schematic_html="", changer_html="")
    async with _client(fake.transport()) as client:
        with pytest.raises(PermissionError, match="not allowed"):
            await client._get_raw(path)
    assert "/INCLUDE/change.cgi" not in fake.paths


async def test_write_client_allows_write_and_readback_endpoints():
    fake = FakeCMI(schematic_html="", changer_html="")
    async with _client(fake.transport()) as client:
        await client._get_raw("/INCLUDE/change.cgi", {"changeadr": "1", "changeto": "1"})
        await client._get_raw("/INCLUDE/changer.cgi", {"sadr": "101301D0180"})
        await client._get_raw("/INCLUDE/changerx2.cgi", {"sadrx2": "10040912091"})
        await client.get_schematic(1)  # read-back schematic page (base-class rule)
    assert fake.paths == [
        "/INCLUDE/change.cgi",
        "/INCLUDE/changer.cgi",
        "/INCLUDE/changerx2.cgi",
        "/schematic_files/1.cgi",
    ]


async def test_read_only_client_still_blocks_change_cgi():
    """The plain read-only gateway client is unchanged: change.cgi is NOT
    in its allow-list (S1 stays read-only)."""
    from app.cmi.client import CMIClient

    fake = FakeCMI(schematic_html="", changer_html="")
    async with CMIClient(
        "http://cmi.test", "admin", "admin", transport=fake.transport()
    ) as read_client:
        with pytest.raises(PermissionError, match="not allowed"):
            await read_client._get_raw("/INCLUDE/change.cgi")
        await read_client.get_schematic(1)  # still allowed
    assert "/schematic_files/1.cgi" in fake.paths


# ---------------------------------------------------------------------------
# changeto sanitisation (numeric / bit / select)
# ---------------------------------------------------------------------------
async def test_write_rejects_numeric_out_of_range():
    numeric_changer = (
        '<input type="hidden" value="019001301D" name="changeadr" id="changeadr">'
        '<input type="text" id="changeto" name="changeto" value="21" '
        'min="5" max="30" style="width:80px;">'
    )
    # route the numeric address through the mode address slot is not
    # possible (whitelist + option list), so test the pure validation helper
    # via ChangerInfo instead.
    from app.cmi.changer_resolver import ChangerInfo

    info = ChangerInfo(
        display_address=MODE_ADDR,
        x2=False,
        change_address="019001301D",
        option_type="numeric",
        min_value="5",
        max_value="30",
    )
    with pytest.raises(CMISafetyError, match="outside"):
        CMIWriteClient._assert_value_allowed(info, "31")
    with pytest.raises(CMISafetyError, match="outside"):
        CMIWriteClient._assert_value_allowed(info, "4")
    with pytest.raises(CMISafetyError, match="not a number"):
        CMIWriteClient._assert_value_allowed(info, "abc")
    CMIWriteClient._assert_value_allowed(info, "21")  # in range: ok
    # (numeric changer HTML remains parsed; unused here)
    assert (
        parse_changer_html(numeric_changer, display_address=MODE_ADDR, x2=False).option_type
        == "numeric"
    )


async def test_write_rejects_bit_out_of_domain():
    from app.cmi.changer_resolver import ChangerInfo

    info = ChangerInfo(
        display_address=SWITCH_ADDR, x2=False, change_address="01910820A1", option_type="bit"
    )
    with pytest.raises(CMISafetyError, match="bit"):
        CMIWriteClient._assert_value_allowed(info, "2")
    CMIWriteClient._assert_value_allowed(info, "1")


async def test_write_rejects_unknown_option_type():
    from app.cmi.changer_resolver import ChangerInfo

    info = ChangerInfo(
        display_address=MODE_ADDR, x2=False, change_address="019001301D", option_type="date"
    )
    with pytest.raises(CMISafetyError, match="unsupported option type"):
        CMIWriteClient._assert_value_allowed(info, "2026-01-01")


async def test_noop_strict_rejects_state_drift():
    """Before=AUTO(3), the CMI shows ABGESENKT(4) after the no-op -> drift."""
    fake = FakeCMI(
        schematic_html=_schematic_with_mode("NORMAL"),
        changer_html=_mode_changer("3"),
        post_write_html=_mode_changer("4"),
    )
    async with _client(fake.transport()) as client:
        with pytest.raises(CMISafetyError, match="state drift"):
            await client.noop_write(MODE_ADDR)
    assert len(fake.change_calls) == 1


async def test_noop_rejects_when_after_readback_unreadable():
    """After the write the value can no longer be read -> state unclear."""
    fake = FakeCMI(
        schematic_html=_schematic_with_mode("NORMAL"),
        changer_html=_mode_changer("3"),
        # post-write: no selected option, and the schematic text no longer
        # matches any option label -> unreadable read-back
        post_write_html=(
            '<input type="hidden" value="019001301D" name="changeadr" id="changeadr">'
            '<select name="changeto" class="schanger" id="changeto">'
            '<option value="1">STANDBY</option>'
            '<option value="3">NORMAL</option>'
            "</select>"
        ),
    )
    fake.schematic_html = _schematic_with_mode("??")
    async with _client(fake.transport()) as client:
        with pytest.raises(CMISafetyError, match="not readable"):
            await client.noop_write(MODE_ADDR)
    assert len(fake.change_calls) == 1


# ---------------------------------------------------------------------------
# current-value extraction
# ---------------------------------------------------------------------------
def test_extract_current_select_selected_option():
    info = parse_changer_html(_mode_changer("3"), display_address=MODE_ADDR, x2=False)
    value, label, consistent = _extract_current(info, "NORMAL")
    assert (value, label, consistent) == ("3", "NORMAL", True)


def test_extract_current_falls_back_to_schematic_text_match():
    changer_no_selected = (
        '<input type="hidden" value="01910820A1" name="changeadr" id="changeadr">'
        '<select name="changeto" class="schanger" id="changeto">'
        '<option value="1">AUS</option>'
        '<option value="2">EIN</option>'
        '<option value="3">AUTO</option>'
        "</select>"
    )
    info = parse_changer_html(changer_no_selected, display_address=SWITCH_ADDR, x2=False)
    value, label, consistent = _extract_current(info, "EIN")
    assert value == "2" and label == "EIN"


def test_extract_current_ambiguous_text_is_unreadable():
    changer_no_selected = (
        '<input type="hidden" value="01910820A1" name="changeadr" id="changeadr">'
        '<select name="changeto" class="schanger" id="changeto">'
        '<option value="1">EIN</option>'
        '<option value="2">EIN</option>'
        "</select>"
    )
    info = parse_changer_html(changer_no_selected, display_address=SWITCH_ADDR, x2=False)
    assert _extract_current(info, "EIN")[0] is None  # two options share the label


def test_extract_current_bit_and_unknown_types():
    from app.cmi.changer_resolver import ChangerInfo

    bit = ChangerInfo(
        display_address=SWITCH_ADDR, x2=False, change_address="01910820A1", option_type="bit"
    )
    assert _extract_current(bit, "1") == ("1", "1", True)
    assert _extract_current(bit, "0") == ("0", "0", True)
    assert _extract_current(bit, "AUS") == (None, None, False)

    date = ChangerInfo(
        display_address=MODE_ADDR, x2=False, change_address="019001301D", option_type="date"
    )
    assert _extract_current(date, "01.01.2026") == (None, None, False)


async def test_write_retries_once_on_transport_error(monkeypatch):
    """First change.cgi call fails at the transport level (e.g. slot
    collision with the running gateway poller) -> one retry, then success."""
    import app.cmi.write as write_mod

    monkeypatch.setattr(write_mod, "_RETRY_DELAY_SEC", 0.0)
    state = {"change_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/INCLUDE/change.cgi":
            state["change_calls"] += 1
            if state["change_calls"] == 1:
                raise httpx.ConnectError("slot collision", request=request)
            return httpx.Response(200, text="OK")
        if path.startswith("/schematic_files/"):
            return httpx.Response(200, text=_schematic_with_mode("NORMAL"))
        if path == "/INCLUDE/changer.cgi":
            return httpx.Response(200, text=_mode_changer("3"))
        return httpx.Response(404, text="unknown endpoint")

    async with _client(httpx.MockTransport(handler)) as client:
        result = await client.noop_write(MODE_ADDR)
    assert state["change_calls"] == 2
    assert result.noop_ok is True


async def test_write_gives_up_after_second_transport_error(monkeypatch):
    """Both change.cgi attempts fail at the transport level -> the error
    surfaces after exactly two calls (no third attempt)."""
    import app.cmi.write as write_mod
    from app.cmi.exceptions import CMIConnectionError

    monkeypatch.setattr(write_mod, "_RETRY_DELAY_SEC", 0.0)
    state = {"change_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/INCLUDE/change.cgi":
            state["change_calls"] += 1
            raise httpx.ConnectError("always down", request=request)
        if request.url.path.startswith("/schematic_files/"):
            return httpx.Response(200, text=_schematic_with_mode("NORMAL"))
        if request.url.path == "/INCLUDE/changer.cgi":
            return httpx.Response(200, text=_mode_changer("3"))
        return httpx.Response(404, text="unknown endpoint")

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(CMIConnectionError, match="always down"):
            await client.noop_write(MODE_ADDR)
    assert state["change_calls"] == 2


async def test_schematic_text_missing_address_returns_none():
    """Address not present on its schematic page -> no schematic text
    (read-back still works via the changer's selected option, consistent=False)."""
    fake = FakeCMI(schematic_html="", changer_html=_mode_changer("3"))  # no pos block
    async with _client(fake.transport()) as client:
        readback = await client.read_current(MODE_ADDR)
    assert readback.schematic_text is None
    assert readback.current_value == "3"
    assert readback.consistent is False


async def test_write_no_retry_on_http_error():
    """HTTP 500 on change.cgi is a gateway error (not a transport error):
    it must NOT be retried, and surface as-is."""
    import app.cmi.exceptions as exc_mod

    state = {"change_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/INCLUDE/change.cgi":
            state["change_calls"] += 1
            return httpx.Response(500, text="server broke")
        if request.url.path.startswith("/schematic_files/"):
            return httpx.Response(200, text=_schematic_with_mode("NORMAL"))
        if request.url.path == "/INCLUDE/changer.cgi":
            return httpx.Response(200, text=_mode_changer("3"))
        return httpx.Response(404, text="unknown endpoint")

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(exc_mod.CMIGatewayError, match="500"):
            await client.noop_write(MODE_ADDR)
    assert state["change_calls"] == 1  # no retry on HTTP errors


async def test_schematic_text_unknown_page_returns_none(monkeypatch):
    """An address without a known schematic page yields no schematic text."""
    import app.cmi.write as write_mod

    monkeypatch.setattr(
        write_mod,
        "ADDRESS_PAGES",
        {k: v for k, v in write_mod.ADDRESS_PAGES.items() if k != MODE_ADDR},
    )
    fake = FakeCMI(schematic_html="", changer_html=_mode_changer("3"))
    async with _client(fake.transport()) as client:
        readback = await client.read_current(MODE_ADDR)
    assert readback.schematic_text is None
    assert readback.current_value == "3"


def test_extract_current_numeric_without_number_is_unreadable():
    from app.cmi.changer_resolver import ChangerInfo

    info = ChangerInfo(
        display_address=MODE_ADDR,
        x2=False,
        change_address="x",
        option_type="numeric",
        min_value="0",
        max_value="10",
    )
    assert _extract_current(info, "keine Zahl") == (None, None, False)


def test_extract_current_select_missing_option_values():
    """Options without values are ignored by the selected-option rule."""
    changer = (
        '<input type="hidden" value="01910820A1" name="changeadr" id="changeadr">'
        '<select name="changeto" class="schanger" id="changeto">'
        '<option selected="selected"></option>'
        '<option value="3">AUTO</option>'
        "</select>"
    )
    info = parse_changer_html(changer, display_address=SWITCH_ADDR, x2=False)
    # the selected option has no value -> unreadable (schematic text "AUTO"
    # would match, but the label must be non-empty; here it is)
    assert _extract_current(info, "AUTO") == ("3", "AUTO", True)


def test_extract_current_select_two_selected_options_uses_text_fallback():
    """A broken dialog (two selected options) degrades to the unique
    schematic-text match instead of inventing a value."""
    changer = (
        '<input type="hidden" value="01910820A1" name="changeadr" id="changeadr">'
        '<select name="changeto" class="schanger" id="changeto">'
        '<option value="1" selected="selected">AUS</option>'
        '<option value="2" selected="selected">EIN</option>'
        "</select>"
    )
    info = parse_changer_html(changer, display_address=SWITCH_ADDR, x2=False)
    # "EIN" matches exactly one option -> still readable
    assert _extract_current(info, "EIN") == ("2", "EIN", True)
    # "AUTO" matches no option -> unreadable
    assert _extract_current(info, "AUTO")[0] is None


def test_extract_current_numeric_within_range():
    numeric_changer = (
        '<input type="hidden" value="019001301D" name="changeadr" id="changeadr">'
        '<input type="text" id="changeto" name="changeto" value="21" min="5" max="30">'
    )
    info = parse_changer_html(numeric_changer, display_address=MODE_ADDR, x2=False)
    assert info.option_type == "numeric"
    assert _extract_current(info, "21,5 °C")[0] == "21,5"
    assert _extract_current(info, "42")[2] is False  # out of [5, 30]
    assert _extract_current(info, None) == (None, None, False)


# ---------------------------------------------------------------------------
# inventory helpers
# ---------------------------------------------------------------------------
def test_collect_settable_addresses_from_live_pages():
    found = collect_settable_addresses(LIVE_PAGES)
    assert set(found) == {
        "101301D0180",
        "10820A10180",
        "10120A10180",
        "10620A10180",
        "10220A10180",
        "10720A10180",
        "10520A10180",
        "10920A10180",
        "10040912091",
    }
    # mode address on pages 1 and 3; solar pump x2 on pages 1 and 4
    assert found["101301D0180"].pages == [1, 3]
    assert found["10040912091"].x2 is True
    assert found["10040912091"].pages == [1, 4]
    assert found["10040912091"].texts["1"] == "Auto"
    assert found["10120A10180"].pages == [1, 5]


def _presence(address: str, x2: bool = False):
    return AddressPresence(address=address, x2=x2, pages=[1], texts={"1": "x"})


def _readback(
    value: str | None = "3", label: str | None = "AUTO", change_address: str = "01910820A1"
) -> Readback:
    return Readback(
        display_address="10820A10180",
        x2=False,
        change_address=change_address,
        option_type="select",
        option_values=["1", "2", "3"],
        option_labels=["AUS", "EIN", "AUTO"],
        current_value=value,
        current_label=label,
        schematic_text="AUTO/EIN",
        consistent=value is not None,
    )


def test_inventory_entry_writeable_switch():
    entry = build_inventory_entry(_presence("10820A10180"), _readback(), label="Pumpe Heizkreis")
    assert entry.writeable is True
    assert entry.risk is False
    assert entry.change_address == "01910820A1"
    assert entry.current_value == "3"


def test_inventory_entry_risk_is_never_writeable():
    entry = build_inventory_entry(_presence(RISK_ADDR), _readback(), label="Oelbrenner")
    assert entry.risk is True
    assert entry.writeable is False
    assert "RISK" in (entry.note or "")


def test_inventory_entry_not_readable_is_not_writeable():
    entry = build_inventory_entry(
        _presence("10820A10180"), _readback(value=None, label=None), label="Pumpe Heizkreis"
    )
    assert entry.writeable is False
    assert "nicht eindeutig lesbar" in (entry.note or "")


def test_inventory_entry_unresolvable_changeadr_not_writeable():
    entry = build_inventory_entry(
        _presence("10820A10180"),
        _readback(change_address=""),
        label="Pumpe Heizkreis",
    )
    assert entry.writeable is False
    assert "nicht aufloesbar" in (entry.note or "")


def test_inventory_entry_non_whitelisted_not_writeable():
    entry = build_inventory_entry(
        AddressPresence(address="99999999999", x2=False, pages=[1], texts={"1": "x"}),
        _readback(),
    )
    assert "99999999999" not in WRITABLE_ADDRESSES
    assert entry.writeable is False
    assert "Whitelist" in (entry.note or "")


def test_format_options_select_and_numeric():
    rb = Readback(
        display_address=MODE_ADDR,
        x2=False,
        change_address="019001301D",
        option_type="select",
        option_values=["1", "2", "3"],
        option_labels=["STANDBY", "ZEIT/AUTO", "NORMAL"],
    )
    assert format_options(rb) == "1=STANDBY, 2=ZEIT/AUTO, 3=NORMAL"

    numeric = Readback(
        display_address=MODE_ADDR,
        x2=False,
        change_address="x",
        option_type="numeric",
        min_value="5",
        max_value="30",
    )
    assert format_options(numeric) == "[5, 30]"


def test_request_url_param_serialization():
    assert _request_url(params={"changeadr": "01910820A1", "changeto": "2"}) == (
        "GET /INCLUDE/change.cgi?changeadr=01910820A1&changeto=2"
    )
    assert _request_url(params={"changeadrx2": "20000040911202", "changetox2": "2"}) == (
        "GET /INCLUDE/change.cgi?changeadrx2=20000040911202&changetox2=2"
    )
