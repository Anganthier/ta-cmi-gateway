"""Tests for the S3-1 two-phase change route (``app.change_route``).

100 % mocked like ``tests/test_write.py``: a stateful
:class:`httpx.MockTransport` (schematic + changer + ``change.cgi``) is
injected into ``ChangeService`` via the ``transport`` hook, ``readback_delay``
is 0 and the clock (``now_fn``) is injectable so a confirmation-window
rollover can be simulated without sleeping.  No network, no live CMI.

Coverage:

* auth: missing/wrong/READ token on POST = 403; write token unconfigured
  = 503; READ token never opens the write path;
* preview: no CMI write, current/target/options, 403 RISK / not-whitelisted,
  422 invalid ``to``, confirmation hash + ``expires_in_sec``;
* execution: valid preview-hash accepted, wrong hash = 403, window rollover
  = 403, RISK re-check = 403, success -> 200 + audit line, read-back
  mismatch -> ``applied=false`` + 502 + audit line, CMI down -> 503 + audit.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.change_route import CHANGE_CONFIRM_TTL_SEC, ChangeService, create_change_app
from app.config import Settings
from tests.conftest import SCHEMATIC_ASSETS

REPO_ROOT = Path(__file__).resolve().parent.parent

SWITCH_ADDR = "10820A10180"  # Pumpe Heizkreis (AUS/EIN/AUTO, changeadr 01910820A1)
RISK_ADDR = "10520A10180"  # Oelbrenner (RISK -- nie schreibbar)
NON_WHITELISTED = "999999D0000"


# ---------------------------------------------------------------------------
# Fake CMI (stateful MockTransport)
# ---------------------------------------------------------------------------
def _switch_changer(selected_value: str = "3") -> str:
    def opt(value: str, label: str) -> str:
        sel = ' selected="selected"' if value == selected_value else ""
        return f'<option value="{value}"{sel}>{label}</option>'

    opts = opt("1", "AUS") + opt("2", "EIN") + opt("3", "AUTO")
    return (
        '<input type="hidden" value="01910820A1" name="changeadr" id="changeadr">'
        f'<select name="changeto" class="schanger" id="changeto">{opts}</select>'
    )


class FakeCMIWrite:
    """Stateful MockTransport for the change route (schematic + changer + change).

    * ``change.cgi`` calls are recorded; after a write the served changer
      switches to ``post_value`` when the write is ``applied`` (else the
      previous value -- simulating a read-back mismatch).
    * ``fail_reads`` makes all reads raise (CMI down -> 503 paths).
    """

    def __init__(
        self,
        *,
        applied: bool = True,
        post_value: str = "2",
        pre_value: str = "3",
        fail_reads: bool = False,
    ) -> None:
        self.applied = applied
        self.post_value = post_value
        self.pre_value = pre_value
        self.fail_reads = fail_reads
        self.change_calls: list[dict[str, str]] = []
        self.paths: list[str] = []
        self._current = pre_value

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if self.fail_reads:
            return httpx.Response(503, text="cmi down (test)")

        if path.startswith("/schematic_files/"):
            text = "EIN" if self._current == "2" else ("AUS" if self._current == "1" else "AUTO/EIN")
            return httpx.Response(
                200,
                text=f'<div id="pos11"><a href="javascript:loadChanger(\'{SWITCH_ADDR}\');">{text}</a></div>',
            )

        if path in ("/INCLUDE/changer.cgi", "/INCLUDE/changerx2.cgi"):
            return httpx.Response(200, text=_switch_changer(self._current))

        if path == "/INCLUDE/change.cgi":
            params = dict(request.url.params)
            self.change_calls.append(params)
            if self.applied:
                self._current = self.post_value
            return httpx.Response(200, text="OK")

        return httpx.Response(404, text="unknown endpoint")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _settings(tmp_path: Path, *, write_token: str = "write-tok",
              read_token: str = "read-tok") -> Settings:
    return Settings(
        cmi_base_url="http://cmi.test",
        cmi_user="admin",
        cmi_pass="admin",
        cmi_poll_sec=3600,
        cmi_api_interval_sec=0.05,
        cmi_data_dir=tmp_path / "data",
        cmi_schematic_dir=SCHEMATIC_ASSETS,
        history_db_path=tmp_path / "data" / "history.db",
        cmi_log_parquet_dir=tmp_path / "data" / "cmi_log_pq",
        gateway_read_token=read_token,
        gateway_write_token=write_token,
        audit_jsonl_path=tmp_path / "audit" / "change.jsonl",
        l1_store_dir=tmp_path / "features",
        l1_model_dir=tmp_path / "models" / "v003",
    )


class FakeClock:
    """Injectable clock; tests advance it across the 15-min window."""

    def __init__(self, now: float = 900_000.0) -> None:  # inside window 1000
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    @property
    def window(self) -> int:
        return int(self.now // CHANGE_CONFIRM_TTL_SEC)


@contextlib.contextmanager
def _app_client(
    tmp_path: Path,
    fake: FakeCMIWrite,
    clock: FakeClock | None = None,
    *,
    write_token: str = "write-tok",
) -> Iterator[TestClient]:
    settings = _settings(tmp_path, write_token=write_token)
    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)

    app = create_change_app(settings)
    clock = clock or FakeClock()
    service: ChangeService = app.state.change
    service._transport = fake.transport()
    service._readback_delay_sec = 0.0
    service._now_fn = clock

    with TestClient(app) as client:
        client.service = service  # type: ignore[attr-defined]
        client.fake = fake  # type: ignore[attr-defined]
        client.clock = clock  # type: ignore[attr-defined]
        client.settings = settings  # type: ignore[attr-defined]
        yield client


@pytest.fixture()
def app_client(tmp_path: Path) -> Iterator[TestClient]:
    fake = FakeCMIWrite()
    with _app_client(tmp_path, fake) as client:
        yield client


def _audit_lines_for(settings: Settings) -> list[dict]:
    import json

    path: Path = settings.audit_jsonl_path
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _audit_lines(client: TestClient) -> list[dict]:
    return _audit_lines_for(client.settings)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Auth (POST)
# ---------------------------------------------------------------------------
def test_post_write_requires_write_token(app_client: TestClient):
    r = app_client.post("/api/v1/change", json={"adr": SWITCH_ADDR, "to": "2", "confirmation_hash": "x"})
    assert r.status_code == 403
    assert "Write-Token" in r.json()["detail"]


def test_post_read_token_rejected(app_client: TestClient):
    """The READ token must NEVER open the write path."""
    r = app_client.post(
        "/api/v1/change",
        headers={"Authorization": "Bearer read-tok"},
        json={"adr": SWITCH_ADDR, "to": "2", "confirmation_hash": "x"},
    )
    assert r.status_code == 403
    assert app_client.fake.change_calls == []


def test_post_wrong_token_rejected(app_client: TestClient):
    r = app_client.post(
        "/api/v1/change",
        headers={"Authorization": "Bearer nope"},
        json={"adr": SWITCH_ADDR, "to": "2", "confirmation_hash": "x"},
    )
    assert r.status_code == 403


def test_post_no_token_configured_503(tmp_path: Path):
    fake = FakeCMIWrite()
    with _app_client(tmp_path, fake, write_token="") as client:
        r = client.post(
            "/api/v1/change",
            headers={"Authorization": "Bearer anything"},
            json={"adr": SWITCH_ADDR, "to": "2", "confirmation_hash": "x"},
        )
        assert r.status_code == 503
        assert "nicht konfiguriert" in r.json()["detail"]
        assert fake.change_calls == []


# ---------------------------------------------------------------------------
# Preview (GET)
# ---------------------------------------------------------------------------
def test_preview_ok_no_cmi_write(app_client: TestClient):
    r = app_client.get(
        f"/api/v1/change?adr={SWITCH_ADDR}&to=2",
        headers={"Authorization": "Bearer read-tok"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["adr"] == SWITCH_ADDR
    assert body["changeadr"] == "01910820A1"
    assert body["current"]["value"] == "3"
    assert body["current"]["label"] == "AUTO"
    assert body["target"]["value"] == "2"
    assert body["target"]["label"] == "EIN"
    assert body["option_values"] == ["1", "2", "3"]
    assert body["option_labels"] == ["AUS", "EIN", "AUTO"]
    assert body["option_type"] == "select"
    assert body["risk"] is False
    assert body["expires_in_sec"] == CHANGE_CONFIRM_TTL_SEC == 900
    assert isinstance(body["confirmation_hash"], str) and len(body["confirmation_hash"]) == 64
    assert isinstance(body["issued_at"], str)
    # NO CMI write happened (preview is read-only):
    assert app_client.fake.change_calls == []


def test_preview_requires_read_token(app_client: TestClient):
    r = app_client.get(f"/api/v1/change?adr={SWITCH_ADDR}&to=2")
    assert r.status_code == 401


def test_preview_risk_address_403_no_cmi_call(app_client: TestClient):
    r = app_client.get(
        f"/api/v1/change?adr={RISK_ADDR}&to=2",
        headers={"Authorization": "Bearer read-tok"},
    )
    assert r.status_code == 403
    assert "RISK" in r.json()["detail"]
    assert app_client.fake.paths == []  # check_access BEFORE any CMI call


def test_preview_not_whitelisted_403(app_client: TestClient):
    r = app_client.get(
        f"/api/v1/change?adr={NON_WHITELISTED}&to=2",
        headers={"Authorization": "Bearer read-tok"},
    )
    assert r.status_code == 403
    assert app_client.fake.paths == []


def test_preview_invalid_to_422(app_client: TestClient):
    r = app_client.get(
        f"/api/v1/change?adr={SWITCH_ADDR}&to=42",
        headers={"Authorization": "Bearer read-tok"},
    )
    assert r.status_code == 422
    assert "keine erlaubte Option" in r.json()["detail"]
    assert app_client.fake.change_calls == []


def test_preview_cmi_down_503(tmp_path: Path):
    fake = FakeCMIWrite(fail_reads=True)
    with _app_client(tmp_path, fake) as client:
        r = client.get(
            f"/api/v1/change?adr={SWITCH_ADDR}&to=2",
            headers={"Authorization": "Bearer read-tok"},
        )
        assert r.status_code == 503
        assert "nicht erreichbar" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Execution (POST)
# ---------------------------------------------------------------------------
def test_post_success_audit_and_readback(app_client: TestClient):
    # Phase 1: preview (same clock -> same 15-min window).
    r = app_client.get(
        f"/api/v1/change?adr={SWITCH_ADDR}&to=2",
        headers={"Authorization": "Bearer read-tok"},
    )
    assert r.status_code == 200
    preview = r.json()

    # Phase 2: execute with the write token + the preview's hash (3 fields).
    r2 = app_client.post(
        "/api/v1/change",
        headers={"Authorization": "Bearer write-tok"},
        json={
            "adr": SWITCH_ADDR,
            "to": "2",
            "confirmation_hash": preview["confirmation_hash"],
        },
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["applied"] is True
    assert body["adr"] == SWITCH_ADDR
    assert body["from"] == "3"
    assert body["to"] == "2"
    assert body["readback"]["value"] == "2"
    assert body["readback"]["label"] == "EIN"
    assert body["audit_id"]
    assert len(app_client.fake.change_calls) == 1
    assert app_client.fake.change_calls[0] == {"changeadr": "01910820A1", "changeto": "2"}

    lines = _audit_lines(app_client)
    assert len(lines) == 1
    line = lines[0]
    assert line["adr"] == SWITCH_ADDR
    assert line["from"] == "3"
    assert line["to"] == "2"
    assert line["user"] == "write-token"
    assert line["readback"] == "2"
    assert line["applied"] is True
    assert line["http_status"] == 200
    assert line["ts"] and line["label"]


def test_post_wrong_hash_403_no_write(app_client: TestClient):
    r = app_client.post(
        "/api/v1/change",
        headers={"Authorization": "Bearer write-tok"},
        json={"adr": SWITCH_ADDR, "to": "2", "confirmation_hash": "0" * 64},
    )
    assert r.status_code == 403
    assert "Confirmation-Hash" in r.json()["detail"]
    assert app_client.fake.change_calls == []
    assert _audit_lines(app_client) == []


def test_post_hash_window_rollover_403(app_client: TestClient):
    """A preview hash becomes invalid once the 15-min window rolled over."""
    clock = app_client.clock
    r = app_client.get(
        f"/api/v1/change?adr={SWITCH_ADDR}&to=2",
        headers={"Authorization": "Bearer read-tok"},
    )
    assert r.status_code == 200
    preview = r.json()
    window_before = clock.window

    # Same window -> accepted.
    ok = app_client.post(
        "/api/v1/change",
        headers={"Authorization": "Bearer write-tok"},
        json={"adr": SWITCH_ADDR, "to": "2", "confirmation_hash": preview["confirmation_hash"]},
    )
    assert ok.status_code == 200

    # Roll the clock into the NEXT 15-min window -> the old hash is stale.
    clock.advance(CHANGE_CONFIRM_TTL_SEC + 1)
    assert clock.window == window_before + 1
    r2 = app_client.post(
        "/api/v1/change",
        headers={"Authorization": "Bearer write-tok"},
        json={"adr": SWITCH_ADDR, "to": "2", "confirmation_hash": preview["confirmation_hash"]},
    )
    assert r2.status_code == 403
    assert "abgelaufen" in r2.json()["detail"] or "ungültig" in r2.json()["detail"]


def test_post_hash_idempotent_within_window(app_client: TestClient):
    """The same preview hash may be re-used inside the 15-min window."""
    r = app_client.get(
        f"/api/v1/change?adr={SWITCH_ADDR}&to=2",
        headers={"Authorization": "Bearer read-tok"},
    )
    preview = r.json()
    for _ in range(2):
        resp = app_client.post(
            "/api/v1/change",
            headers={"Authorization": "Bearer write-tok"},
            json={
                "adr": SWITCH_ADDR,
                "to": "2",
                "confirmation_hash": preview["confirmation_hash"],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["applied"] is True
    assert len(app_client.fake.change_calls) == 2


def test_post_risk_rechecked_even_with_valid_hash(app_client: TestClient):
    """RISK is re-checked on POST (whitelist/RISK gate before the write)."""
    r = app_client.get(
        f"/api/v1/change?adr={RISK_ADDR}&to=3",
        headers={"Authorization": "Bearer read-tok"},
    )
    assert r.status_code == 403  # preview already blocks RISK
    # Even a crafted hash cannot get through:
    from app.change_route import change_confirm_hash

    crafted = change_confirm_hash("write-tok", RISK_ADDR, "3", app_client.clock())
    r2 = app_client.post(
        "/api/v1/change",
        headers={"Authorization": "Bearer write-tok"},
        json={"adr": RISK_ADDR, "to": "3", "confirmation_hash": crafted},
    )
    assert r2.status_code == 403
    assert app_client.fake.change_calls == []


def test_post_readback_mismatch_applied_false_502(tmp_path: Path):
    """Read-back mismatch -> applied=false + 502 + audit line (strict=False)."""
    fake = FakeCMIWrite(applied=False, post_value="9")  # CMI refuses / drifts
    with _app_client(tmp_path, fake) as client:
        r = client.get(
            f"/api/v1/change?adr={SWITCH_ADDR}&to=2",
            headers={"Authorization": "Bearer read-tok"},
        )
        preview = r.json()
        r2 = client.post(
            "/api/v1/change",
            headers={"Authorization": "Bearer write-tok"},
            json={"adr": SWITCH_ADDR, "to": "2", "confirmation_hash": preview["confirmation_hash"]},
        )
        assert r2.status_code == 502
        body = r2.json()
        assert body["applied"] is False
        assert body["readback"]["value"] != "2"
        assert "Read-back-Mismatch" in body["error"]
        assert body["audit_id"]
        lines = _audit_lines(client)
        assert len(lines) == 1
        assert lines[0]["applied"] is False
        assert lines[0]["http_status"] == 502


def test_post_cmi_down_503_audit(tmp_path: Path):
    """CMI down during the write -> 503 + audit line (applied=null)."""
    fake = FakeCMIWrite(fail_reads=True)
    with _app_client(tmp_path, fake) as client:
        # preview also fails (503) -- but the audit/503 path on POST is what
        # matters here: craft a hash for the current window and POST.
        from app.change_route import change_confirm_hash

        hash_ = change_confirm_hash("write-tok", SWITCH_ADDR, "2", client.clock())
        r = client.post(
            "/api/v1/change",
            headers={"Authorization": "Bearer write-tok"},
            json={"adr": SWITCH_ADDR, "to": "2", "confirmation_hash": hash_},
        )
        assert r.status_code == 503
        assert "CMI-Write fehlgeschlagen" in r.json()["detail"]
        lines = _audit_lines(client)
        assert len(lines) == 1
        assert lines[0]["applied"] is None
        assert lines[0]["http_status"] == 503
        assert lines[0]["to"] == "2"


def test_post_from_value_none_when_read_fails_but_write_ok(tmp_path: Path):
    """The 'before' read failure must not mask the write (from=null).

    Emulated with a transport that 503s the FIRST two read calls (the
    pre-write ``read_current`` = resolve + schematic) and then serves normal
    responses for the write + read-back.
    """
    call_state = {"reads": 0, "written": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/INCLUDE/change.cgi":
            call_state["written"] = True
            return httpx.Response(200, text="OK")
        if path.startswith("/schematic_files/") or path in (
            "/INCLUDE/changer.cgi",
            "/INCLUDE/changerx2.cgi",
        ):
            call_state["reads"] += 1
            # Call 1 = the PRE-write read_current (resolve) -> 503 (from=None);
            # all later reads serve the live state (3/AUTO pre-write, 2/EIN
            # after change.cgi has been called).
            if call_state["reads"] == 1:
                return httpx.Response(503, text="transient (test)")
            current = "2" if call_state["written"] else "3"
            if path.startswith("/schematic_files/"):
                text = "EIN" if current == "2" else "AUTO/EIN"
                return httpx.Response(
                    200,
                    text=(
                        f'<div id="pos11"><a href="javascript:loadChanger('
                        f"\'{SWITCH_ADDR}\');\">{text}</a></div>"
                    ),
                )
            return httpx.Response(200, text=_switch_changer(current))
        return httpx.Response(404, text="unknown")

    settings = _settings(tmp_path)
    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)
    app = create_change_app(settings)
    clock = FakeClock()
    service: ChangeService = app.state.change
    service._transport = httpx.MockTransport(handler)
    service._readback_delay_sec = 0.0
    service._now_fn = clock

    from app.change_route import change_confirm_hash

    with TestClient(app) as client:
        hash_ = change_confirm_hash("write-tok", SWITCH_ADDR, "2", clock())
        r = client.post(
            "/api/v1/change",
            headers={"Authorization": "Bearer write-tok"},
            json={"adr": SWITCH_ADDR, "to": "2", "confirmation_hash": hash_},
        )
        assert r.status_code == 200
        assert r.json()["from"] is None  # pre-read failed, write still ok
        assert r.json()["applied"] is True
        assert r.json()["to"] == "2"


# ---------------------------------------------------------------------------
# Confirm-window / hash unit tests
# ---------------------------------------------------------------------------
def test_confirm_window_and_hash_pure():
    from app.change_route import change_confirm_hash, confirm_window

    assert CHANGE_CONFIRM_TTL_SEC == 900
    # window is a pure function of the epoch
    assert confirm_window(0.0) == 0
    assert confirm_window(899.999) == 0
    assert confirm_window(900.0) == 1
    assert confirm_window(1_700_000_000.0) == int(1_700_000_000.0 // 900)

    # same window -> same hash; different window -> different hash
    h_a = change_confirm_hash("tok", SWITCH_ADDR, "2", 900_000.0)
    h_b = change_confirm_hash("tok", SWITCH_ADDR, "2", 900_800.0)  # same window
    h_c = change_confirm_hash("tok", SWITCH_ADDR, "2", 901_000.0)  # next window
    assert h_a == h_b
    assert h_a != h_c
    # different adr or to -> different hash
    assert h_a != change_confirm_hash("tok", SWITCH_ADDR, "3", 900_000.0)
    assert h_a != change_confirm_hash("tok", RISK_ADDR, "2", 900_000.0)
    # different key (write token) -> different hash
    assert h_a != change_confirm_hash("other", SWITCH_ADDR, "2", 900_000.0)


# ---------------------------------------------------------------------------
# validate_target / helpers / aclose (unit + focused)
# ---------------------------------------------------------------------------
def test_validate_target_numeric_and_bit(tmp_path: Path):
    from app.change_route import ChangeService
    from app.cmi.write import Readback

    # numeric: in-range ok, out-of-range 422, non-numeric 422
    num = Readback(
        display_address=SWITCH_ADDR, x2=False, change_address="x",
        option_type="numeric", min_value="0", max_value="10",
        current_value="5", current_label=None,
    )
    ChangeService.validate_target(num, "5")
    with pytest.raises(Exception) as e:
        ChangeService.validate_target(num, "15")
    assert e.value.status_code == 422
    with pytest.raises(Exception) as e2:
        ChangeService.validate_target(num, "abc")
    assert e2.value.status_code == 422
    # empty -> 422
    with pytest.raises(Exception) as e3:
        ChangeService.validate_target(num, "")
    assert e3.value.status_code == 422

    # bit: 0/1 ok, else 422
    bit = Readback(
        display_address=SWITCH_ADDR, x2=False, change_address="x",
        option_type="bit", current_value="1",
    )
    ChangeService.validate_target(bit, "0")
    with pytest.raises(Exception) as e4:
        ChangeService.validate_target(bit, "2")
    assert e4.value.status_code == 422

    # unsupported option_type -> 422
    weird = Readback(
        display_address=SWITCH_ADDR, x2=False, change_address="x",
        option_type="date", current_value=None,
    )
    with pytest.raises(Exception) as e5:
        ChangeService.validate_target(weird, "x")
    assert e5.value.status_code == 422


def test_num_and_label_for_helpers(tmp_path: Path):
    from app.change_route import _label_for, _num
    from app.cmi.write import Readback

    assert _num(None) is None
    assert _num("") is None
    assert _num("abc") is None
    assert _num("3,5") == 3.5
    assert _num("2") == 2.0

    # _label_for: current_label wins; else first option label; else adr
    with_label = Readback(display_address=SWITCH_ADDR, x2=False, change_address="x",
                          option_type="select", option_values=["1"], option_labels=["AUS"],
                          current_value="3", current_label="AUTO")
    assert _label_for(SWITCH_ADDR, with_label) == "AUTO"
    no_current = Readback(display_address=SWITCH_ADDR, x2=False, change_address="x",
                          option_type="select", option_values=["1", "2"],
                          option_labels=["", "EIN"], current_value=None, current_label=None)
    assert _label_for(SWITCH_ADDR, no_current) == "EIN"
    no_labels = Readback(display_address=SWITCH_ADDR, x2=False, change_address="x",
                         option_type="select", option_values=["1"], option_labels=[""],
                         current_value=None, current_label=None)
    assert _label_for(SWITCH_ADDR, no_labels) == SWITCH_ADDR


def test_service_aclose_closes_client(tmp_path: Path):
    import asyncio

    from app.change_route import ChangeService

    service = ChangeService(_settings(tmp_path))
    client = service._client_instance()
    assert service._client is client
    asyncio.run(service.aclose())
    assert service._client is None
    # aclose when there was never a client is a no-op
    empty = ChangeService(_settings(tmp_path))
    asyncio.run(empty.aclose())  # must not raise


def test_audit_oserror_is_not_masked(tmp_path: Path):
    """A failed audit write is logged but NEVER masks the HTTP result."""
    from app.change_route import ChangeService

    settings = _settings(tmp_path)
    service = ChangeService(settings)
    # Point the audit path at a location that cannot be written (a file).
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    service.settings.audit_jsonl_path = blocker / "sub" / "change.jsonl"
    # mkdir(parents=True) raises because 'blocker' exists as a FILE.
    audit_id = service.audit(
        adr=SWITCH_ADDR, label="Pumpe Heizkreis", from_value="3", to="2",
        readback="2", applied=True, http_status=200,
    )
    assert audit_id  # still returns an audit id
    assert not (blocker / "sub" / "change.jsonl").exists()


def test_service_enabled_and_write_token(tmp_path: Path):
    from app.change_route import ChangeService

    with_token = ChangeService(_settings(tmp_path, write_token="abc"))
    assert with_token.enabled is True
    assert with_token.write_token == "abc"
    without = ChangeService(_settings(tmp_path, write_token=""))
    assert without.enabled is False
    assert without.write_token == ""


# ---------------------------------------------------------------------------
# Write-exception handlers (safety + unexpected) and aclose-via-client
# ---------------------------------------------------------------------------
def test_post_write_safety_error_403_audit(tmp_path: Path):
    """A CMISafetyError raised by CMIWriteClient.write -> 403 + audit line."""
    import httpx

    from app.change_route import change_confirm_hash, create_change_app
    from app.cmi.exceptions import CMISafetyError

    settings = _settings(tmp_path)
    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)
    app = create_change_app(settings)
    clock = FakeClock()
    service: ChangeService = app.state.change
    service._transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=_switch_changer("2"))
        if not request.url.path.startswith("/schematic_files/")
        else httpx.Response(
            200,
            text=(
                f'<div id="pos11"><a href="javascript:loadChanger('
                f"\'{SWITCH_ADDR}\');\">EIN</a></div>"
            ),
        )
    )
    service._readback_delay_sec = 0.0
    service._now_fn = clock

    async def failing_write(display_address: str, value: str):
        raise CMISafetyError("safety gate tripped (test)", address=display_address)

    client = service._client_instance()
    client.write = failing_write  # type: ignore[method-assign]

    with TestClient(app) as tc:
        hash_ = change_confirm_hash("write-tok", SWITCH_ADDR, "2", clock())
        r = tc.post(
            "/api/v1/change",
            headers={"Authorization": "Bearer write-tok"},
            json={"adr": SWITCH_ADDR, "to": "2", "confirmation_hash": hash_},
        )
        assert r.status_code == 403
        assert "safety gate" in r.json()["detail"]
        lines = _audit_lines_for(settings)
        assert len(lines) == 1
        assert lines[0]["applied"] is False
        assert lines[0]["http_status"] == 403


def test_post_write_unexpected_error_500_audit(tmp_path: Path):
    """An unexpected (non-CMI) error in CMIWriteClient.write -> 500 + audit."""
    import httpx

    from app.change_route import change_confirm_hash, create_change_app

    settings = _settings(tmp_path)
    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)
    app = create_change_app(settings)
    clock = FakeClock()
    service: ChangeService = app.state.change
    service._transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=_switch_changer("2"))
        if not request.url.path.startswith("/schematic_files/")
        else httpx.Response(
            200,
            text=(
                f'<div id="pos11"><a href="javascript:loadChanger('
                f"\'{SWITCH_ADDR}\');\">EIN</a></div>"
            ),
        )
    )
    service._readback_delay_sec = 0.0
    service._now_fn = clock

    async def crashing_write(display_address: str, value: str):
        raise RuntimeError("unexpected write crash (test)")

    client = service._client_instance()
    client.write = crashing_write  # type: ignore[method-assign]

    with TestClient(app) as tc:
        hash_ = change_confirm_hash("write-tok", SWITCH_ADDR, "2", clock())
        r = tc.post(
            "/api/v1/change",
            headers={"Authorization": "Bearer write-tok"},
            json={"adr": SWITCH_ADDR, "to": "2", "confirmation_hash": hash_},
        )
        assert r.status_code == 500
        assert "unexpected" in r.json()["detail"].lower()
        lines = _audit_lines_for(settings)
        assert len(lines) == 1
        assert lines[0]["applied"] is None
        assert lines[0]["http_status"] == 500
