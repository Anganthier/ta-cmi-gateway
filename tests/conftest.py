"""Shared test fixtures: fake CMI transport, settings, recorded payloads.

No network access in tests: every CMI call goes through
:class:`httpx.MockTransport` which serves the recorded discovery fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
#: Recorded CMI fixtures. The public repo ships the small, sanitized captures
#: under ``tests/fixtures``; an internal checkout with the full ``discovery/``
#: tree still takes precedence when present.
DISCOVERY = REPO_ROOT / "discovery" if (REPO_ROOT / "discovery").is_dir() else REPO_ROOT / "tests" / "fixtures"
SCHEMATIC_ASSETS = REPO_ROOT / "assets" / "schematics"

#: The 9 known settable display addresses (Discovery 2026-10-26).
SETTABLE_ADDRESSES: dict[str, bool] = {
    "101301D0180": False,  # Betriebsmodus (7 options)
    "10820A10180": False,  # Pumpe Heizkreis
    "10220A10180": False,  # Zirkulation
    "10620A10180": False,  # Ladepumpe WW
    "10720A10180": False,  # WW vom Oel
    "10520A10180": False,  # Oelbrenner
    "10920A10180": False,  # Ventil Heizkreis
    "10120A10180": False,  # Pumpe Holzofen
    "10040912091": True,  # Solarpumpe Modus (x2)
}

#: Recorded changer responses (tests/fixtures/changer_*.html).
CHANGER_FIXTURES: dict[str, str] = {
    "101301D0180": (DISCOVERY / "changer_101301D0180.html").read_text(encoding="utf-8"),
    "10820A10180": (DISCOVERY / "changer_10820A10180.html").read_text(encoding="utf-8"),
    "10040912091": (DISCOVERY / "changerx2_10040912091.html").read_text(encoding="utf-8"),
}

#: Documented per-address expectations for all 9 settable addresses (S1-2 DoD).
#:
#: * mode select ``101301D0180`` -> changeadr ``019001301D`` (7 options).
#: * switches ``10<pos>20A10180`` -> changeadr ``01910<pos>0A1`` (AUS/EIN/AUTO).
#:   Verified live for ``10820A10180`` -> ``01910820A1``; the other six
#:   addresses follow the same documented pattern.
#: * x2 ``10040912091`` -> changeadrx2 ``20000040911202`` (recorded fixture).
#:
#: ``first_option`` is the label of the first <option> in document order.
CHANGER_EXPECTED: dict[str, dict] = {
    "101301D0180": {"change_address": "019001301D", "n_options": 7, "first_option": "STANDBY"},
    "10820A10180": {"change_address": "01910820A1", "n_options": 3, "first_option": "AUS"},
    "10220A10180": {"change_address": "01910220A1", "n_options": 3, "first_option": "AUS"},
    "10620A10180": {"change_address": "01910620A1", "n_options": 3, "first_option": "AUS"},
    "10720A10180": {"change_address": "01910720A1", "n_options": 3, "first_option": "AUS"},
    "10520A10180": {"change_address": "01910520A1", "n_options": 3, "first_option": "AUS"},
    "10920A10180": {"change_address": "01910920A1", "n_options": 3, "first_option": "AUS"},
    "10120A10180": {"change_address": "01910120A1", "n_options": 3, "first_option": "AUS"},
    "10040912091": {"change_address": "20000040911202", "n_options": 3, "first_option": "Auto"},
}


def _synth_changer_html(address: str, *, x2: bool = False) -> str:
    """Build a plausible select-changer response for an address whose recorded
    fixture is not bundled in ``discovery/``.

    Follows the documented pattern: switches -> changeadr ``01910<pos>0A1``
    with options AUS/EIN/AUTO; the single x2 address -> changeadrx2
    ``20000040911202`` with options Auto/Hand-AUS/Hand-EIN.
    """
    if x2:
        change = "20000040911202"
        hidden = f'<input type="hidden" value="{change}" name="changeadrx2" id="changeadr">'
        select = '<select id="changeto" class="schanger" name="changetox2">'
        options = (
            '<option value="2" selected="selected">Auto</option>'
            '<option value="0">Hand/AUS</option>'
            '<option value="1">Hand/EIN</option>'
        )
    else:
        change = "01910" + address[2:4] + "0A1"
        hidden = f'<input type="hidden" value="{change}" name="changeadr" id="changeadr">'
        select = '<select name="changeto" class="schanger" id="changeto">'
        options = (
            '<option value="1">AUS</option>'
            '<option value="2">EIN</option>'
            '<option value="3" selected="selected">AUTO</option>'
        )
    return f"{hidden}{select}{options}</select>"


def make_api_fixture(node: int, params: str, *, node1_empty_d: bool = False) -> dict:
    """Build a JSON-API fixture payload for the given node/params.

    Node 32 reuses the *recorded* La/Ld response (discovery/la_ld_node32.json)
    plus I/O/D sections modeled on the json-api-doc examples.

    Args:
        node: CAN node number (1 = UVR1611, 32 = UVR610).
        params: The ``jsonparam`` string, e.g. ``"I,O,D,La,Ld"``.
        node1_empty_d: When True and ``node == 1``, the DL-Bus section is
            returned **empty** -- matching the real UVR1611, which has no
            DL-Bus channels even though it accepts the ``D`` parameter
            (verified live 2026-10-27: Inputs=16, Outputs=13, DL-Bus empty).
    """
    timestamp = 1793042604
    header = {"Version": 8, "Device": "87" if node == 1 else "91", "Timestamp": timestamp}
    data: dict = {}

    if "I" in _param_names(params):
        data["Inputs"] = [
            {"Number": i + 1, "AD": "A", "Value": {"Value": v, "Unit": "1"}}
            for i, v in enumerate(
                [92.0, 71.2, 22.7, 11.3] if node == 1 else [30.4, 47.9, 37.1, 11.1]
            )
        ]
    if "O" in _param_names(params):
        data["Outputs"] = [
            {"Number": i + 1, "AD": "D", "Value": {"Value": (1 if i % 2 == 0 else 0), "Unit": "43"}}
            for i in range(4)
        ]
    if "D" in _param_names(params):
        if node == 1 and node1_empty_d:
            # Real UVR1611: the D parameter is accepted but yields no DL-Bus.
            data["DL-Bus"] = []
        else:
            data["DL-Bus"] = [
                {"Number": 1, "AD": "D", "Value": {"Value": 0, "Unit": "43"}},
                {"Number": 2, "AD": "D", "Value": {"Value": 1, "Unit": "43"}},
            ]
    if node == 32 and ("La" in _param_names(params) or "Ld" in _param_names(params)):
        recorded = json.loads((DISCOVERY / "la_ld_node32.json").read_text(encoding="utf-8"))
        if "La" in _param_names(params):
            data["Logging Analog"] = recorded["Data"]["Logging Analog"]
        if "Ld" in _param_names(params):
            data["Logging Digital"] = recorded["Data"]["Logging Digital"]

    return {"Header": header, "Data": data, "Status": "OK", "Status code": 0}


def _param_names(params: str) -> set[str]:
    return {p.strip() for p in params.split(",") if p.strip()}


def make_fake_transport(
    *,
    api_status: int | None = None,
    fail_changer: set[str] | None = None,
    node1_empty_d: bool = False,
) -> httpx.MockTransport:
    """MockTransport serving all read-only CMI endpoints from fixtures.

    Args:
        api_status: When set, ``/INCLUDE/api.cgi`` always returns this status
            code (used to exercise the rate-limit / error paths).
        fail_changer: Display addresses whose changer response should be
            unparseable (no hidden ``changeadr`` input) so that
            :class:`ChangerResolver` raises :class:`CMIChangerError` for them.
        node1_empty_d: Serve node 1 with an empty DL-Bus section (real UVR1611
            behaviour) instead of two synthetic DL-Bus entries.
    """

    fail_changer = fail_changer or set()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)

        if path.startswith("/schematic_files/"):
            try:
                page = int(path.rsplit("/", 1)[-1].split(".")[0])
            except ValueError:
                return _cmi_404()
            fixture = DISCOVERY / f"live_{page}.cgi"
            if not fixture.is_file():
                return _cmi_404()
            return httpx.Response(200, text=fixture.read_text(encoding="utf-8"))

        if path == "/INCLUDE/api.cgi":
            node = int(params.get("jsonnode", "0"))
            jsonparam = params.get("jsonparam", "")
            if api_status is not None:
                return httpx.Response(
                    200,
                    text=json.dumps(
                        {
                            "Header": {"Version": 8, "Device": "87", "Timestamp": 1},
                            "Data": {},
                            "Status": "TOO MANY REQUESTS"
                            if api_status == 4
                            else f"ERROR {api_status}",
                            "Status code": api_status,
                        }
                    ),
                )
            return httpx.Response(
                200,
                content=json.dumps(
                    make_api_fixture(node, jsonparam, node1_empty_d=node1_empty_d)
                ).encode("utf-8"),
            )

        if path == "/INCLUDE/changer.cgi" or path == "/INCLUDE/changerx2.cgi":
            x2 = path.endswith("changerx2.cgi")
            adr = params.get("sadrx2" if x2 else "sadr", "")
            if adr in fail_changer:
                # No hidden changeadr input -> ChangerResolver raises
                # CMIChangerError ("no hidden changeadr input ...").
                return httpx.Response(
                    200,
                    text='<select name="changeto" class="schanger" id="changeto">'
                    "<option value=\"1\">?</option></select>",
                )
            if adr in CHANGER_FIXTURES:
                return httpx.Response(200, text=CHANGER_FIXTURES[adr])
            # Synthesize a documented-pattern select changer for the other
            # settable addresses (see CHANGER_EXPECTED).
            return httpx.Response(200, text=_synth_changer_html(adr, x2=x2))

        if path.startswith("/INCLUDE/change") or path == "/INCLUDE/checkbuttondo.cgi":
            return httpx.Response(403, text="write endpoints are blocked in tests")

        return httpx.Response(404, text="unknown endpoint")

    return httpx.MockTransport(handler)


def _cmi_404() -> httpx.Response:
    return httpx.Response(
        404,
        text="<head><title>CMI Server Error</title></head>"
        "<body><h2>Error 404 - Not Found</h2></body>",
    )


@pytest.fixture()
def fake_transport() -> httpx.MockTransport:
    return make_fake_transport()


@pytest.fixture(autouse=True)
def _reset_history_db_cache():
    """Close/drop cached history DB connections between tests.

    Each test uses its own temp DB path; this prevents a stale connection to a
    previous temp dir (and any leaked file handle) leaking across tests.
    """
    from app.db import reset_db_cache

    yield
    reset_db_cache()


@pytest.fixture()
def settings(tmp_path: Path):
    """Test settings pointing at the fake transport (never the real CMI)."""
    from app.config import Settings

    return Settings(
        cmi_base_url="http://cmi.test",
        cmi_user="admin",
        cmi_pass="admin",
        cmi_poll_sec=3600,  # pollers do not loop in tests
        cmi_api_interval_sec=0.05,
        cmi_changer_ttl_sec=60,
        cmi_data_dir=tmp_path / "data",
        cmi_schematic_dir=SCHEMATIC_ASSETS,
        history_db_path=tmp_path / "data" / "history.db",
        cmi_log_parquet_dir=tmp_path / "data" / "cmi_log_pq",
        gateway_read_token="read-tok",
        gateway_write_token="write-tok",
    )
