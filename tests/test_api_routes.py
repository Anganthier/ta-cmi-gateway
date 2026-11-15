"""API route tests (TestClient, auth 401/200, state/params/health).

The CMI side is completely faked via ``httpx.MockTransport``: no network.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.cmi.client import CMIClient
from app.cmi.json_api import parse_json_api
from app.cmi.schematic_parser import DEFAULT_SCHEMATIC_DIR, parse_schematic_file
from app.main import Gateway, create_app
from app.state import StateStore
from tests.conftest import (
    CHANGER_EXPECTED,
    DISCOVERY,
    SETTABLE_ADDRESSES,
    make_api_fixture,
    make_fake_transport,
)

READ = {"Authorization": "Bearer read-tok"}
WRITE = {"Authorization": "Bearer write-tok"}


def _seeded_store() -> StateStore:
    store = StateStore(cmi_url="http://cmi.test")
    for page in range(1, 6):
        html = (DISCOVERY / f"live_{page}.cgi").read_text(encoding="utf-8")
        store.update_schematic(parse_schematic_file(DEFAULT_SCHEMATIC_DIR, page, html), ts=1000.0)
    store.update_api(
        parse_json_api(make_api_fixture(32, "I,O,D,La,Ld"), node=32, params="I,O,D,La,Ld"),
        ts=1001.0,
    )
    return store


@pytest.fixture()
def app_client(settings, fake_transport):
    """App with the fake CMI transport + a pre-seeded state store."""
    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)
    client = CMIClient(
        base_url=settings.base_url,
        user=settings.cmi_user,
        password=settings.cmi_pass,
        transport=fake_transport,
    )
    gateway = Gateway(settings, client=client, store=_seeded_store())
    app = create_app(settings)
    app.state.gateway = gateway
    with TestClient(app) as tc:
        yield tc
    # TestClient exit already ran shutdown -> aclose()
    _ = client


def test_root_page():
    """The UI (ui/index.html) is served statically at / -- no token needed."""
    settings = Settings_for_test()
    app = create_app(settings)
    with TestClient(app) as tc:
        r = tc.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        # App-shell markers (S4-1)
        assert "cmi-gateway-ui" in r.text
        assert "token-input" in r.text
        assert "data-v=\"aussen\"" in r.text
        assert "vendor/chart.umd.js" in r.text
        assert "/docs" in r.text


def Settings_for_test():
    import tempfile
    from pathlib import Path

    from app.config import Settings

    tmp = Path(tempfile.mkdtemp())
    return Settings(
        cmi_base_url="http://cmi.test",
        cmi_poll_sec=3600,
        cmi_api_interval_sec=0.05,
        cmi_changer_ttl_sec=360,
        cmi_data_dir=tmp / "data",
        cmi_schematic_dir=DISCOVERY.parent / "assets" / "schematics",
        history_db_path=tmp / "data" / "history.db",
        gateway_read_token="read-tok",
        gateway_write_token="write-tok",
    )


def test_docs_and_openapi_open(app_client):
    assert app_client.get("/docs").status_code == 200
    spec = app_client.get("/openapi.json").json()
    assert "/api/v1/state" in spec["paths"]
    assert "/api/v1/params" in spec["paths"]
    assert "/api/v1/health" in spec["paths"]


@pytest.mark.parametrize(
    "headers",
    [None, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic admin:admin"}],
)
def test_state_requires_token(app_client, headers):
    r = app_client.get("/api/v1/state", headers=headers)
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate", "").startswith("Bearer")


@pytest.mark.parametrize("headers", [READ, WRITE])
def test_state_returns_full_document(app_client, headers):
    r = app_client.get("/api/v1/state", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["cmi"] == "http://cmi.test"
    assert set(map(int, body["schematics"].keys())) >= {1, 2, 3, 4, 5}
    assert body["health"]["schematic_ok"] is True
    assert body["health"]["api_ok"] is True
    assert body["health"]["stale"] is False
    ids = [f["id"] for f in body["fields"]]
    assert len(ids) == len(set(ids))
    schematic_fields = [f for f in body["fields"] if f["source"] == "schematic"]
    api_fields = [f for f in body["fields"] if f["source"] == "json_api"]
    assert len(schematic_fields) >= 20
    assert len(api_fields) >= 20
    # RAS-state from the recorded node-32 La/Ld is present
    ras = [f for f in api_fields if f["ras_state"]]
    assert ras


def test_params_requires_token(app_client):
    assert app_client.get("/api/v1/params").status_code == 401


def test_params_lazy_resolves_all_nine_addresses(app_client):
    """S1-2 DoD: every settable address is resolved with change address + options.

    Uses all 9 addresses (the conftest ``SETTABLE_ADDRESSES`` map) rather than
    a subset.  Per-address expected values live in ``CHANGER_EXPECTED``:
    three come from the recorded ``discovery/changer*.html`` fixtures, the
    other six are synthesized by :func:`tests.conftest._synth_changer_html`
    following the documented pattern (switches -> ``01910<pos>0A1``, x2 ->
    ``20000040911202``).
    """
    r = app_client.get("/api/v1/params", headers=READ)
    assert r.status_code == 200
    body = r.json()
    by_address = {p["address"]: p for p in body}

    # All 9 known settable addresses are present.
    assert set(by_address) >= set(SETTABLE_ADDRESSES), (
        f"missing addresses: {set(SETTABLE_ADDRESSES) - set(by_address)}"
    )

    # For each address assert the resolved change address, option count, and
    # the first option label.
    for address, is_x2 in SETTABLE_ADDRESSES.items():
        entry = by_address[address]
        expected = CHANGER_EXPECTED[address]
        assert entry["x2"] is is_x2, f"{address}: x2 mismatch"
        assert entry["error"] is None, f"{address}: unexpected error {entry['error']!r}"
        assert entry["change_address"] == expected["change_address"], (
            f"{address}: change_address {entry['change_address']!r} != {expected['change_address']!r}"
        )
        assert len(entry["options"]) == expected["n_options"], (
            f"{address}: {len(entry['options'])} options, expected {expected['n_options']}"
        )
        assert entry["options"][0]["label"] == expected["first_option"], (
            f"{address}: first option {entry['options'][0]['label']!r} != {expected['first_option']!r}"
        )

    # Spot-check the three recorded fixtures in detail.
    mode = by_address["101301D0180"]
    assert mode["change_address"] == "019001301D"
    assert mode["option_type"] == "select"
    assert [o["value"] for o in mode["options"]] == ["1", "2", "3", "4", "5", "6", "7"]
    assert mode["current_raw"] == "NORMAL"
    assert mode["labels"] == ["Betriebsmodus"]
    solar = by_address["10040912091"]
    assert solar["x2"] is True
    assert solar["change_address"] == "20000040911202"
    assert {o["value"]: o["label"] for o in solar["options"]} == {
        "2": "Auto",
        "0": "Hand/AUS",
        "1": "Hand/EIN",
    }


def test_params_cached_second_call_no_new_resolution(app_client):
    r1 = app_client.get("/api/v1/params", headers=READ).json()
    r2 = app_client.get("/api/v1/params", headers=READ).json()
    assert [p["change_address"] for p in r1] == [p["change_address"] for p in r2]


@pytest.mark.asyncio
async def test_params_per_address_error_not_500(settings):
    """S1 defect 5a: a failing changer resolution yields a per-address error,
    not a 500, while the other addresses still resolve."""
    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)
    # Make exactly one address unresolvable.
    failing = "10520A10180"
    transport = make_fake_transport(fail_changer={failing})
    client = CMIClient(
        base_url=settings.base_url,
        user=settings.cmi_user,
        password=settings.cmi_pass,
        transport=transport,
    )
    gateway = Gateway(settings, client=client, store=_seeded_store())
    app = create_app(settings)
    app.state.gateway = gateway
    with TestClient(app) as tc:
        r = tc.get("/api/v1/params", headers=READ)
        assert r.status_code == 200, "a per-address failure must not 500 the route"
        by_address = {p["address"]: p for p in r.json()}
        failed = by_address[failing]
        assert failed["error"], "the failing address must carry an error string"
        assert failed["change_address"] is None
        assert failed["options"] == []
        # A sibling address still resolves cleanly.
        sibling = by_address["10820A10180"]
        assert sibling["error"] is None
        assert sibling["change_address"] == "01910820A1"
    await client.aclose()


def test_health_empty_html_vs_real_error(settings, tmp_path):
    """S1 defect 5b: distinguish the 'empty HTML' ping path from a real error.

    * empty: the schematic CGI answers 200 with an empty body -> the health
      ping records the error text ``"empty"``.
    * real error: the schematic CGI answers 500 -> the ping records an
      ``"error: ..."`` text.
    Both downgrade the CMI to 503 (with the API route already down), but the
    reported ``last_error`` differs.
    """

    def build_app(transport, store):
        app = create_app(settings)
        app.state.gateway = Gateway(settings, client=_client(transport), store=store)
        return app

    # --- empty HTML --------------------------------------------------------
    empty_transport = httpx.MockTransport(
        lambda r: httpx.Response(200, text="")
    )
    store = StateStore(cmi_url=settings.base_url)
    store.update_api_failed(1, "api down", ts=1.0)  # api route already down
    app = build_app(empty_transport, store)
    with TestClient(app) as tc:
        r = tc.get("/api/v1/health")
        assert r.status_code == 503
        body = r.json()
        assert body["cmi"]["stale"] is True
        assert body["cmi"]["schematic"]["ok"] is False
        # The empty-HTML path reports the literal "empty" marker, not "error:".
        assert body["cmi"]["schematic"]["last_error"] == "empty"

    # --- real error (500) --------------------------------------------------
    err_transport = httpx.MockTransport(
        lambda r: httpx.Response(500, text="boom")
    )
    store2 = StateStore(cmi_url=settings.base_url)
    store2.update_api_failed(1, "api down", ts=1.0)
    app2 = build_app(err_transport, store2)
    with TestClient(app2) as tc:
        r = tc.get("/api/v1/health")
        assert r.status_code == 503
        last_error = r.json()["cmi"]["schematic"]["last_error"]
        assert last_error is not None
        assert last_error.startswith("error:"), (
            f"a real transport error must be reported as 'error: ...', got {last_error!r}"
        )
        assert "500" in last_error


def _client(transport):
    return CMIClient(
        base_url="http://cmi.test", user="admin", password="admin", transport=transport
    )


def test_health_without_token(app_client):
    r = app_client.get("/api/v1/health")
    assert r.status_code in (200, 503)
    body = r.json()
    assert body["gateway"]["status"] == "running"
    assert "schematic" in body["cmi"]
    assert "json_api" in body["cmi"]


def test_health_openapi_docs_require_no_token(app_client):
    # /docs and /openapi.json are reachable without a bearer token
    assert app_client.get("/docs").status_code == 200
    assert app_client.get("/openapi.json").status_code == 200


def test_state_auth_disabled_when_no_tokens(tmp_path):
    from app.config import Settings

    settings = Settings(
        cmi_base_url="http://cmi.test",
        cmi_poll_sec=3600,
        cmi_data_dir=tmp_path / "data",
        cmi_schematic_dir=DISCOVERY.parent / "assets" / "schematics",
        history_db_path=tmp_path / "data" / "history.db",
        gateway_read_token="",
        gateway_write_token="",
    )
    app = create_app(settings)
    app.state.gateway = Gateway(
        settings, client=CMIClient("http://cmi.test", "admin", "admin"),
        store=_seeded_store(),
    )
    with TestClient(app) as tc:
        assert tc.get("/api/v1/state").status_code == 200


def test_health_down_when_cmi_unreachable(tmp_path):
    """503 + stale when the CMI does not answer at all."""
    from app.config import Settings

    settings = Settings(
        cmi_base_url="http://127.0.0.1:9",  # closed port -> instant connection refused
        cmi_poll_sec=3600,
        cmi_api_interval_sec=0.05,
        cmi_data_dir=tmp_path / "data",
        cmi_schematic_dir=DISCOVERY.parent / "assets" / "schematics",
        history_db_path=tmp_path / "data" / "history.db",
        gateway_read_token="read-tok",
        gateway_write_token="write-tok",
    )
    store = StateStore(cmi_url=settings.base_url)
    store.update_schematic_failed("unreachable", ts=1.0)
    store.update_api_failed(1, "unreachable", ts=1.0)
    app = create_app(settings)
    app.state.gateway = Gateway(
        settings,
        client=CMIClient(settings.base_url, "admin", "admin", timeout_sec=2.0),
        store=store,
    )
    with TestClient(app) as tc:
        r = tc.get("/api/v1/health")
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "down"
        assert body["cmi"]["stale"] is True
        assert body["cmi"]["schematic"]["ok"] is False


def test_state_empty_store_still_ok(app_client):
    # A fresh, unseeded store must still return 200 with empty sections.
    from app.main import create_app as _create_app

    settings = app_client.app.state.settings
    app = _create_app(settings)
    app.state.gateway = Gateway(settings, client=CMIClient(settings.base_url, "a", "b"))
    with TestClient(app) as tc:
        r = tc.get("/api/v1/state", headers=READ)
        assert r.status_code == 200
        body = r.json()
        assert body["fields"] == []
        assert body["health"]["stale"] is True


# ---------------------------------------------------------------------------
# /api/v1/history (S2-1)
# ---------------------------------------------------------------------------
def _seed_history(settings) -> None:
    """Insert a deterministic, multi-timestamp history set into the gateway DB."""
    from app.db import get_history_db

    db = get_history_db(settings.history_db_path)
    db.upsert(
        [
            ("2026-01-01 00:00:00", "schematic", 209, "A", 10.0, None, "schematic"),
            ("2026-01-01 00:10:00", "schematic", 209, "A", 20.0, None, "schematic"),
            ("2026-01-01 00:20:00", "schematic", 209, "A", 30.0, None, "schematic"),
            ("2026-01-01 00:00:00", "schematic", 210, "A", 50.0, None, "schematic"),
            ("2026-01-01 00:10:00", "schematic", 210, "A", 60.0, None, "schematic"),
            ("2026-01-01 00:00:00", "json:32", 1, "A", 100.0, None, "json"),
        ]
    )


def test_history_requires_token(app_client):
    assert app_client.get("/api/v1/history").status_code == 401


def test_history_single_channel_points(app_client, settings):
    _seed_history(settings)
    r = app_client.get(
        "/api/v1/history", headers=READ,
        params={"device": "schematic", "channel": "209",
                "start": "2026-01-01", "end": "2026-01-02"},
    )
    assert r.status_code == 200
    j = r.json()
    assert j["device"] == "schematic" and j["channel"] == 209
    assert [p["value"] for p in j["points"]] == [10.0, 20.0, 30.0]
    assert j["count"] == 3


def test_history_multi_channel_dict(app_client, settings):
    _seed_history(settings)
    r = app_client.get(
        "/api/v1/history", headers=READ,
        params={"device": "schematic", "start": "2026-01-01", "end": "2026-01-02"},
    )
    j = r.json()
    assert "schematic:209" in j and "schematic:210" in j
    # device filter must exclude the seeded json:32 series
    assert "json:32:1" not in j
    assert j["_meta"]["count"] == 5


def test_history_date_range_filtering(app_client, settings):
    _seed_history(settings)
    r = app_client.get(
        "/api/v1/history", headers=READ,
        params={"device": "schematic", "channel": "209",
                "start": "2026-01-01 00:05:00", "end": "2026-01-01 00:15:00"},
    )
    j = r.json()
    # only the 00:10 point falls in [00:05, 00:15]
    assert [p["value"] for p in j["points"]] == [20.0]


def test_history_agg_min_max_avg(app_client, settings):
    _seed_history(settings)
    for agg, want in (("min", 10.0), ("max", 30.0), ("avg", 20.0)):
        r = app_client.get(
            "/api/v1/history", headers=READ,
            params={"device": "schematic", "channel": "209",
                    "start": "2026-01-01", "end": "2026-01-02", "agg": agg},
        )
        assert r.status_code == 200, (agg, r.text)
        j = r.json()
        assert j["count"] == 1 and j["points"][0]["value"] == want


def test_history_invalid_agg_400(app_client):
    r = app_client.get("/api/v1/history", headers=READ, params={"agg": "median"})
    assert r.status_code == 400


def test_history_bad_date_400(app_client):
    r = app_client.get("/api/v1/history", headers=READ, params={"start": "01.01.2026"})
    assert r.status_code == 400


def test_history_interval_bucketing(app_client, settings):
    _seed_history(settings)
    r = app_client.get(
        "/api/v1/history", headers=READ,
        params={"device": "schematic", "channel": "209",
                "start": "2026-01-01", "end": "2026-01-02",
                "agg": "avg", "interval": "900"},
    )
    j = r.json()
    # 900s (15 min) buckets: [00:00-00:15) -> avg(10, 20) = 15.0
    #                       [00:15-00:30) -> avg(30)     = 30.0
    assert [p["value"] for p in j["points"]] == [15.0, 30.0]
    assert j["interval"] == 900


def test_history_channel_repeatable_and_comma(app_client, settings):
    _seed_history(settings)
    # repeatable (?channel=209&channel=210) -- a list of tuples preserves both
    r = app_client.get(
        "/api/v1/history", headers=READ,
        params=[("channel", "209"), ("channel", "210"),
                ("start", "2026-01-01"), ("end", "2026-01-02")],
    )
    assert r.status_code == 200
    j = r.json()
    assert "schematic:209" in j and "schematic:210" in j
    # comma-separated single value
    r2 = app_client.get(
        "/api/v1/history", headers=READ,
        params={"channel": "209,210", "start": "2026-01-01", "end": "2026-01-02"},
    )
    assert set(k for k in r2.json() if not k.startswith("_")) == {"schematic:209", "schematic:210"}


def test_history_limit_cap(app_client, settings):
    from app.db import get_history_db

    db = get_history_db(settings.history_db_path)
    # one channel, 50 distinct samples
    for i in range(50):
        db.upsert(
            [(f"2026-02-01 00:{i:02d}:00", "schematic", 1000, "A", float(i), None, "schematic")]
        )
    r = app_client.get(
        "/api/v1/history", headers=READ,
        params={"channel": "1000", "start": "2026-02-01", "end": "2026-02-02", "limit": "5"},
    )
    j = r.json()
    # single series -> flat response; limit caps the per-series points
    assert j["count"] == 5
    # ...and the largest sample (last by ts) must be present
    assert j["points"][-1]["value"] == 49.0


def test_history_metric_filter(app_client, settings):
    _seed_history(settings)
    r = app_client.get(
        "/api/v1/history", headers=READ,
        params={"device": "json:32", "metric": "json",
                "start": "2026-01-01", "end": "2026-01-02"},
    )
    j = r.json()
    assert j["device"] == "json:32" and j["channel"] == 1


def test_history_empty_range(app_client):
    r = app_client.get(
        "/api/v1/history", headers=READ,
        params={"start": "2001-01-01", "end": "2001-01-02"},
    )
    assert r.status_code == 200
    assert r.json() == {}


def test_health_reports_history(app_client, settings):
    _seed_history(settings)
    r = app_client.get("/api/v1/health")
    assert r.status_code in (200, 503)
    h = r.json().get("history")
    assert h is not None
    assert h["rows"] >= 1
    assert h["last_insert_ts"] is not None
    assert h["error"] is None


# ---------------------------------------------------------------------------
# /api/v1/history: ad filter (S4-4) -- A/D types must not mix in one series
# ---------------------------------------------------------------------------
def _seed_history_ad(settings) -> None:
    """A schematic series with a DIGITAL TWIN of channel 209 at the same ts.

    Without an ``ad`` filter the long store unions both rows into one series
    (the exact defect S4-4 fixes for the UI's WinSol default view).
    """
    from app.db import get_history_db

    db = get_history_db(settings.history_db_path)
    db.upsert(
        [
            ("2026-03-01 00:00:00", "schematic", 209, "A", 21.0, "°C", "schematic"),
            ("2026-03-01 00:00:00", "schematic", 209, "D", 1.0, None, "schematic"),
            ("2026-03-01 00:10:00", "schematic", 209, "A", 22.0, "°C", "schematic"),
            ("2026-03-01 00:10:00", "schematic", 209, "D", 0.0, None, "schematic"),
        ]
    )


def test_history_ad_filter_analog(app_client, settings):
    _seed_history_ad(settings)
    r = app_client.get(
        "/api/v1/history", headers=READ,
        params={"device": "schematic", "channel": "209", "ad": "A",
                "start": "2026-03-01", "end": "2026-03-02"},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    # ONLY the analog points: the digital twins (1.0 / 0.0) must be excluded.
    assert j["device"] == "schematic" and j["channel"] == 209
    assert [p["value"] for p in j["points"]] == [21.0, 22.0]
    assert j["count"] == 2


def test_history_ad_filter_digital(app_client, settings):
    _seed_history_ad(settings)
    r = app_client.get(
        "/api/v1/history", headers=READ,
        params={"device": "schematic", "channel": "209", "ad": "D",
                "start": "2026-03-01", "end": "2026-03-02"},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    # ONLY the digital points: the analog temperatures must be excluded.
    assert [p["value"] for p in j["points"]] == [1.0, 0.0]


def test_history_ad_omitted_is_union(app_client, settings):
    _seed_history_ad(settings)
    r = app_client.get(
        "/api/v1/history", headers=READ,
        params={"device": "schematic", "channel": "209",
                "start": "2026-03-01", "end": "2026-03-02"},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    # No ad param -> both A and D rows in one series (original contract).
    assert j["count"] == 4
    assert {p["value"] for p in j["points"]} == {21.0, 22.0, 1.0, 0.0}


def test_history_ad_invalid_400(app_client, settings):
    _seed_history_ad(settings)
    r = app_client.get(
        "/api/v1/history", headers=READ,
        params={"device": "schematic", "channel": "209", "ad": "X",
                "start": "2026-03-01", "end": "2026-03-02"},
    )
    assert r.status_code == 400
    assert "ad" in r.json()["detail"]
