"""Tests for the Minderer write path + CMI_ROLE gate (``app.cmi.minderer``).

v0.6-A' Phase-1-Baustein: Minderer-Sollspannungen (UVR610, Seite 6, x2
numeric) per CMI ``change.cgi`` + Dev/Prod-Role-Gate (Default ``dev`` =
kein echter CMI-Write, No-op bleibt erlaubt; ``prod`` = Pi = Master).

100 % mocked (``httpx.MockTransport``, s. ``tests/test_write.py``):
**0 echte CMI-Aufrufe** -- die Seite-6-Read-back wird mit mocktem
``6.cgi``-HTML + dem realen Dialog-HTML (Discovery 18.11) getestet.

Semantik (Task v006ap, 2.6 + 3.4):

* ``CMIWriteClient.write()`` (und damit jeder echte Minderer-Write) wirft
  in ``dev`` :class:`CMISafetyError` VOR jeglichem HTTP (client-level:
  MockTransport sieht 0 Requests; s. auch ``tests/test_write.py``);
* der Minderer-Service führt vor dem Write ein read-only Read-back durch
  (changerx2 + schematic 6) -- das ist erlaubt (Dev darf LSEN, nie
  SCHREIBEN); der Write-Endpunkt (``change.cgi``) bleibt in dev unberührt;
* ``noop_write`` (wertneutral) ist ohne Role-Gate (dev erlaubt).

Coverage:

* Role-Gate dev/invalid (kein Write), prod (Write läuft durch, x2-Params,
  2-Dezimal-Form, Read-back konsistent);
* Minderer-Service: Zone-Mapping, Volt-Normierung + Range-Guard,
  Read-back (Seite 6), Rate-Limit 1×/h (Audit-JSONL in tmp-Datei,
  wertneutral ausgenommen, pro Adresse), No-op (dev erlaubt) + Audit;
* Confirm/TTY: y/N-Abbruch, kein-TTY-Abbruch;
* CLI: Argument-Handling ohne Netz (Zone/Volt-Validierung exit 1,
  ``write`` ohne TTY exit 2, main()-Pfade über injizierte Settings/Service).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import app.cmi.minderer as minderer_mod
from app.cmi.exceptions import CMISafetyError
from app.cmi.minderer import (
    MINDERER_ZONES,
    MindererAbortError,
    MindererService,
    address_for_zone,
    audit_append,
    build_write_client,
    normalize_volt,
)
from app.config import Settings, reset_settings_cache
from tests.conftest import SCHEMATIC_ASSETS
from tests.test_write import _minderer_changer

REPO_ROOT = Path(__file__).resolve().parent.parent

EG = "10144142091"
KELL = "10044142091"
EG_ROHE = "2000014414D10D"


def _settings(tmp_path: Path, *, role: str = "dev", audit_name: str = "minderer.jsonl") -> Settings:
    """Test settings (fake CMI, tmp-Audit-Pfad); ``role`` setzt CMI_ROLE."""
    return Settings(
        cmi_base_url="http://cmi.test",
        cmi_user="admin",
        cmi_pass="admin",
        cmi_poll_sec=3600,
        cmi_api_interval_sec=0.05,
        cmi_data_dir=tmp_path / "data",
        cmi_schematic_dir=SCHEMATIC_ASSETS,
        audit_jsonl_path=tmp_path / "audit" / audit_name,
        cmi_role=role,
    )


def _client_for(settings: Settings, fake: FakeMindererCMI) -> object:
    """Write client mit Fake-Transport + null Read-back-Delay."""
    return build_write_client(settings, transport=fake.transport(), readback_delay_sec=0.0)


def _service(settings: Settings, fake: FakeMindererCMI, *, now_fn=None, source: str = "cli") -> MindererService:
    return MindererService(
        settings,
        client=_client_for(settings, fake),
        audit_path=settings.audit_jsonl_path,
        now_fn=now_fn,
        source=source,
    )


def _audit_lines(settings: Settings) -> list[dict]:
    path: Path = settings.audit_jsonl_path
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fake CMI (stateful MockTransport, Minderer-Adressen, Seite 6)
# ---------------------------------------------------------------------------
class FakeMindererCMI:
    """Stateful MockTransport für die Minderer-Adressen (Seite 6).

    Liefert den realen CMI-Dialog (``<input type="number">``-Read-back,
    Discovery 18.11) + Seite-6-Schematic; ``change.cgi`` wird aufgezeichnet
    und setzt den Changer auf ``post_value``.
    """

    def __init__(self, addr: str, *, pre_value: str = "2.00", post_value: str | None = None) -> None:
        self.addr = addr
        self.pre_value = pre_value
        # Default: the value is STABLE after a write (no drift). Only an
        # explicitly different post_value simulates CMI drift.
        self.post_value = post_value if post_value is not None else pre_value
        self.change_calls: list[dict[str, str]] = []
        self.paths: list[str] = []
        self._written = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        params = dict(request.url.params)

        if path.startswith("/schematic_files/"):
            value = self.post_value if self._written else self.pre_value
            return httpx.Response(
                200,
                text=f'<div id="pos1" class="changex2" adresse="{self.addr}">{value} V</div>',
            )

        if path in ("/INCLUDE/changer.cgi", "/INCLUDE/changerx2.cgi"):
            value = self.post_value if self._written else self.pre_value
            return httpx.Response(200, text=_minderer_changer(self.addr, value))

        if path == "/INCLUDE/change.cgi":
            self.change_calls.append(params)
            self._written = True
            return httpx.Response(200, text="OK")

        return httpx.Response(404, text="unknown endpoint")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


# ---------------------------------------------------------------------------
# Zone-Mapping + Volt-Normierung
# ---------------------------------------------------------------------------
def test_zone_mapping_all_three():
    assert MINDERER_ZONES == {
        "kell": "10044142091",
        "eg": "10144142091",
        "dg": "10244142091",
    }
    assert address_for_zone("kell") == "10044142091"
    assert address_for_zone("eg") == "10144142091"
    assert address_for_zone("dg") == "10244142091"


def test_address_for_zone_unknown_raises_value_error():
    for zone in ("xx", "", "keller", "EG"):  # case-sensitive, nur die 3 Zonen
        with pytest.raises(ValueError, match="unknown Minderer-Zone"):
            address_for_zone(zone)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2.5", "2.50"),
        ("2", "2.00"),
        (2, "2.00"),
        ("2.500", "2.50"),
        ("10", "10.00"),
        ("2.00", "2.00"),
        ("9,99", "9.99"),  # Komma = Dezimaltrenner
    ],
)
def test_normalize_volt_forms(raw, expected):
    assert normalize_volt(raw) == expected


@pytest.mark.parametrize("raw", ["1.99", "10.01", "0", "1", "100", "abc", ""])
def test_normalize_volt_range_guard(raw):
    with pytest.raises(CMISafetyError, match="außerhalb|keine Zahl"):
        normalize_volt(raw)


def test_normalize_volt_bounds_are_inclusive():
    assert normalize_volt("2.0") == "2.00"
    assert normalize_volt("10.0") == "10.00"


# ---------------------------------------------------------------------------
# Read-back (Seite 6, mockt)
# ---------------------------------------------------------------------------
async def test_minderer_readback_page6_numeric(tmp_path: Path):
    settings = _settings(tmp_path)
    fake = FakeMindererCMI(EG, pre_value="2.50")
    service = _service(settings, fake)
    rb = await service.readback("eg")

    assert "/schematic_files/6.cgi" in fake.paths  # ADDRESS_PAGES -> 6
    assert "/INCLUDE/changerx2.cgi" in fake.paths
    assert rb.display_address == EG
    assert rb.x2 is True
    assert rb.change_address == EG_ROHE
    assert rb.option_type == "numeric"
    assert rb.current_value == "2.50"
    assert rb.min_value == "2.00"
    assert rb.max_value == "10.00"
    assert rb.consistent is True
    assert fake.change_calls == []  # read-only
    await service.client.aclose()


async def test_minderer_readback_unknown_zone_value_error(tmp_path: Path):
    settings = _settings(tmp_path)
    fake = FakeMindererCMI(EG)
    service = _service(settings, fake)
    with pytest.raises(ValueError, match="unknown Minderer-Zone"):
        await service.readback("xx")
    assert fake.paths == []
    await service.client.aclose()


# ---------------------------------------------------------------------------
# Role-Gate: dev (Default) blockt echte Writes (kein change.cgi)
# ---------------------------------------------------------------------------
async def test_write_dev_default_blocks_real_write(tmp_path: Path):
    """Kern-DoD: Default-Settings (CMI_ROLE unset -> dev) -> CMISafetyError;
    der WRITE-Endpunkt wird NIE berührt (kein change.cgi, keine
    change-Calls). Das Read-back (read-only) vor dem Write ist erlaubt --
    Dev darf LSEN, nie SCHREIBEN (maintainer-Forderung 18.11)."""
    settings = _settings(tmp_path)
    assert settings.cmi_role == "dev"
    fake = FakeMindererCMI(EG)
    service = _service(settings, fake)
    with pytest.raises(CMISafetyError, match="CMI_ROLE=dev"):
        await service.write("eg", "2.5", confirm=False)
    assert fake.change_calls == []
    assert "/INCLUDE/change.cgi" not in fake.paths
    assert _audit_lines(settings) == []  # dev: "HIER wird NIX weiter gemeldet"
    await service.client.aclose()


async def test_write_dev_blocks_even_value_neutral_via_write(tmp_path: Path):
    """Dev blockt JEDEN write() (auch wertneutral per write()); der
    freigegebene No-op-Pfad ist noop() ohne Role-Gate."""
    settings = _settings(tmp_path)
    fake = FakeMindererCMI(EG, pre_value="2.00")
    service = _service(settings, fake)
    with pytest.raises(CMISafetyError, match="CMI_ROLE=dev"):
        await service.write("eg", "2.0", confirm=False)
    assert fake.change_calls == []
    await service.client.aclose()


@pytest.mark.parametrize("env_role", ["PROD", "prodX", " Prod ", "DEV"])
async def test_write_invalid_role_fails_safe_to_dev(tmp_path: Path, monkeypatch, env_role):
    """CMI_ROLE=PROD/prodX/... -> dev-Semantik: kein Write, kein change.cgi
    (ein Tippfehler darf niemals Writes freigeben)."""
    monkeypatch.setenv("CMI_ROLE", env_role)
    reset_settings_cache()
    settings = _settings(tmp_path, role=env_role)
    fake = FakeMindererCMI(EG)
    service = MindererService(settings, client=_client_for(settings, fake))
    assert service.role() == "dev"

    try:
        with pytest.raises(CMISafetyError, match="CMI_ROLE=dev"):
            await service.write("eg", "2.5", confirm=False)
        assert fake.change_calls == []
        assert "/INCLUDE/change.cgi" not in fake.paths
    finally:
        reset_settings_cache()
    await service.client.aclose()


# ---------------------------------------------------------------------------
# Role-Gate: prod erlaubt echte Writes (mockt)
# ---------------------------------------------------------------------------
async def test_write_prod_applies_value_x2_params_and_readback(tmp_path: Path, monkeypatch):
    """CMI_ROLE=prod: der Write läuft durch -- x2-Parameter, 2-Dezimal-Form
    (2.5 -> "2.50"), Read-back konsistent, Audit-Zeile."""
    monkeypatch.setenv("CMI_ROLE", "prod")
    reset_settings_cache()
    settings = _settings(tmp_path, role="prod")
    fake = FakeMindererCMI(EG, pre_value="2.00", post_value="2.50")
    service = MindererService(settings, client=_client_for(settings, fake))
    assert service.role() == "prod"

    try:
        result = await service.write("eg", "2.5", confirm=False)
        assert fake.change_calls == [{"changeadrx2": EG_ROHE, "changetox2": "2.50"}]
        assert result.applied is True
        assert result.x2 is True
        assert result.after.current_value == "2.50"
        assert result.request.endswith(f"changeadrx2={EG_ROHE}&changetox2=2.50")
        lines = _audit_lines(settings)
        assert len(lines) == 1
        line = lines[0]
        assert line["kind"] == "minderer_write"
        assert line["zone"] == "eg"
        assert line["address"] == EG
        assert line["from"] == "2.00"
        assert line["to"] == "2.50"
        assert line["role"] == "prod"
        assert line["source"] == "cli"
        assert line["ok"] is True
        assert line["detail"] is None
        assert line["ts"]
    finally:
        reset_settings_cache()
    await service.client.aclose()


async def test_write_prod_readback_mismatch_strict_raises_and_audits(
    tmp_path: Path, monkeypatch
):
    """prod + CMI-Drift (post-Write zeigt 9.99 statt 2.50) -> strict
    CMISafetyError; Audit-Zeile ok=False (Write ausgeführt, Wert NICHT
    bestätigt)."""
    monkeypatch.setenv("CMI_ROLE", "prod")
    reset_settings_cache()
    settings = _settings(tmp_path, role="prod")
    fake = FakeMindererCMI(EG, pre_value="2.00", post_value="9.99")
    service = MindererService(settings, client=_client_for(settings, fake))
    try:
        with pytest.raises(CMISafetyError, match="read-back mismatch"):
            await service.write("eg", "2.5", confirm=False)
        assert len(fake.change_calls) == 1
        lines = _audit_lines(settings)
        assert len(lines) == 1
        assert lines[0]["ok"] is False
        assert "read-back mismatch" in (lines[0]["detail"] or "")
    finally:
        reset_settings_cache()
    await service.client.aclose()


# ---------------------------------------------------------------------------
# No-op (wertneutral, dev-erlaubt) + Audit
# ---------------------------------------------------------------------------
async def test_noop_dev_allowed_writes_back_current_value(tmp_path: Path):
    """dev + No-op: erlaubt (maintainer-Go), exakt der aktuelle Wert wird
    zurueckgeschrieben (x2), Strict-Drift-Check, Audit-Zeile."""
    settings = _settings(tmp_path)
    fake = FakeMindererCMI(EG, pre_value="2.00")
    service = _service(settings, fake)
    result = await service.noop("eg")

    assert fake.change_calls == [{"changeadrx2": EG_ROHE, "changetox2": "2.00"}]
    assert result.noop_ok is True
    assert result.value_changed is False
    assert result.before.current_value == "2.00"
    lines = _audit_lines(settings)
    assert len(lines) == 1
    line = lines[0]
    assert line["kind"] == "minderer_noop"
    assert line["zone"] == "eg"
    assert line["address"] == EG
    assert line["from"] == "2.00"
    assert line["to"] == "2.00"
    assert line["role"] == "dev"
    assert line["ok"] is True
    assert line["detail"] is None
    await service.client.aclose()


async def test_noop_strict_drift_audited_not_ok(tmp_path: Path):
    """No-op + CMI-Drift (2.00 -> 2.01 nach dem Write): strict raises;
    Audit-Zeile ok=False mit Drift-Detail."""
    settings = _settings(tmp_path)
    fake = FakeMindererCMI(EG, pre_value="2.00", post_value="2.01")
    service = _service(settings, fake)
    with pytest.raises(CMISafetyError, match="state drift"):
        await service.noop("eg")
    assert len(fake.change_calls) == 1
    lines = _audit_lines(settings)
    assert len(lines) == 1
    assert lines[0]["kind"] == "minderer_noop"
    assert lines[0]["ok"] is False
    assert "drift" in (lines[0]["detail"] or "").lower()
    await service.client.aclose()


async def test_noop_unknown_zone_value_error(tmp_path: Path):
    settings = _settings(tmp_path)
    fake = FakeMindererCMI(EG)
    service = _service(settings, fake)
    with pytest.raises(ValueError, match="unknown Minderer-Zone"):
        await service.noop("xx")
    assert fake.paths == []
    await service.client.aclose()


# ---------------------------------------------------------------------------
# Rate-Limit 1×/h (Audit-JSONL in tmp-Datei)
# ---------------------------------------------------------------------------
async def test_rate_limit_second_real_write_within_hour_blocked(tmp_path: Path, monkeypatch):
    """1. echter Write ok; 2. echter Write < 3600 s an gleicher Adresse ->
    CMISafetyError (Rate-Limit); KEIN zweiter Write-Versuch."""
    monkeypatch.setenv("CMI_ROLE", "prod")
    reset_settings_cache()
    settings = _settings(tmp_path, role="prod")
    clock = {"t": time.time()}
    fake = FakeMindererCMI(EG, pre_value="2.00", post_value="2.50")
    service = MindererService(
        settings, client=_client_for(settings, fake), now_fn=lambda: clock["t"]
    )
    try:
        await service.write("eg", "2.5", confirm=False)
        assert len(fake.change_calls) == 1

        clock["t"] += 600  # 10 min später
        with pytest.raises(CMISafetyError, match="Rate-Limit 1×/h"):
            await service.write("eg", "3.0", confirm=False)
        assert len(fake.change_calls) == 1  # kein zweiter Write
        # nur die erste Zeile (der geblockte Versuch läuft vor client.write)
        assert len(_audit_lines(settings)) == 1
    finally:
        reset_settings_cache()
    await service.client.aclose()


async def test_rate_limit_value_neutral_write_is_exempt(tmp_path: Path, monkeypatch):
    """No-op (wertneutral) zählt NICHT ins Rate-Limit: nach einem No-op ist
    sofort ein echter Write möglich (prod)."""
    monkeypatch.setenv("CMI_ROLE", "prod")
    reset_settings_cache()
    settings = _settings(tmp_path, role="prod")
    fake = FakeMindererCMI(EG, pre_value="2.00", post_value="2.00")  # wertstabil
    service = MindererService(settings, client=_client_for(settings, fake))
    try:
        await service.noop("eg")  # wertneutral -> zählt nicht
        fake._written = False  # CMI-Zustand zurücksetzen
        fake.post_value = "2.50"  # erst jetzt: echter Change für den write()
        result = await service.write("eg", "2.5", confirm=False)
        assert result.applied is True
        assert len(fake.change_calls) == 2
        assert [line["kind"] for line in _audit_lines(settings)] == ["minderer_noop", "minderer_write"]
    finally:
        reset_settings_cache()
    await service.client.aclose()


async def test_rate_limit_allows_after_one_hour(tmp_path: Path, monkeypatch):
    """Nach > 3600 s ist der nächste echte Write wieder erlaubt."""
    monkeypatch.setenv("CMI_ROLE", "prod")
    reset_settings_cache()
    settings = _settings(tmp_path, role="prod")
    clock = {"t": time.time()}
    fake = FakeMindererCMI(EG, pre_value="2.00", post_value="2.50")
    service = MindererService(
        settings, client=_client_for(settings, fake), now_fn=lambda: clock["t"]
    )
    try:
        await service.write("eg", "2.5", confirm=False)
        clock["t"] += 3700  # > 1 h
        fake._written = False  # CMI-Zustand: der neue Wert ist jetzt Ist
        fake.pre_value = "2.50"
        fake.post_value = "3.00"
        result = await service.write("eg", "3.0", confirm=False)
        assert result.applied is True
        assert len(fake.change_calls) == 2
    finally:
        reset_settings_cache()
    await service.client.aclose()


async def test_rate_limit_other_address_unaffected(tmp_path: Path, monkeypatch):
    """Rate-Limit ist pro ADRESSE: ein Write an EG blockiert Keller nicht."""
    monkeypatch.setenv("CMI_ROLE", "prod")
    reset_settings_cache()
    settings = _settings(tmp_path, role="prod")
    clock = {"t": time.time()}
    fake_eg = FakeMindererCMI(EG, pre_value="2.00", post_value="2.50")
    fake_kell = FakeMindererCMI(KELL, pre_value="2.00", post_value="2.50")
    service_eg = MindererService(
        settings, client=_client_for(settings, fake_eg), now_fn=lambda: clock["t"]
    )
    service_kell = MindererService(
        settings, client=_client_for(settings, fake_kell), now_fn=lambda: clock["t"]
    )
    try:
        await service_eg.write("eg", "2.5", confirm=False)
        result = await service_kell.write("kell", "2.5", confirm=False)
        assert result.applied is True
        assert len(fake_kell.change_calls) == 1
    finally:
        reset_settings_cache()
    await service_eg.client.aclose()
    await service_kell.client.aclose()


def test_last_write_epoch_parsing_and_skips(tmp_path: Path):
    """_last_write_epoch: nur minder er_write + gleiche Adresse + gültiges
    ts; No-ops, fremde Adressen und korrupte Zeilen werden übersprungen."""
    settings = _settings(tmp_path)
    fake = FakeMindererCMI(EG)
    service = MindererService(settings, client=_client_for(settings, fake))
    base = datetime.now(UTC).isoformat(timespec="seconds")

    audit_append(
        {"kind": "minderer_noop", "zone": "eg", "address": EG, "from": "2.00",
         "to": "2.00", "role": "dev", "source": "cli", "ok": True, "detail": None},
        path=settings.audit_jsonl_path,
    )
    assert service._last_write_epoch(EG) is None  # No-op zählt nicht

    audit_append(
        {"kind": "minderer_write", "zone": "kell", "address": KELL,
         "from": "2.00", "to": "2.50", "role": "prod", "source": "cli",
         "ok": True, "detail": None, "ts": base},
        path=settings.audit_jsonl_path,
    )
    assert service._last_write_epoch(EG) is None  # fremde Adresse zählt nicht

    settings.audit_jsonl_path.open("a", encoding="utf-8").write("not-json\n")
    assert service._last_write_epoch(EG) is None  # korrupte Zeile übersprungen

    record = {
        "ts": base, "kind": "minderer_write", "zone": "eg", "address": EG,
        "from": "2.00", "to": "2.50", "role": "prod", "source": "cli",
        "ok": True, "detail": None,
    }
    settings.audit_jsonl_path.open("a", encoding="utf-8").write(json.dumps(record) + "\n")
    assert service._last_write_epoch(EG) == datetime.fromisoformat(base).timestamp()
    _run(service.client.aclose())


def test_last_write_epoch_missing_file_is_none(tmp_path: Path):
    settings = _settings(tmp_path)
    fake = FakeMindererCMI(EG)
    service = MindererService(settings, client=_client_for(settings, fake))
    assert service._last_write_epoch(EG) is None
    _run(service.client.aclose())


# ---------------------------------------------------------------------------
# Confirm / TTY (Modul-API-Level, kein CMI-Write)
# ---------------------------------------------------------------------------
class _FakeTTY:
    def isatty(self) -> bool:
        return True


class _NoTTY:
    def isatty(self) -> bool:
        return False


async def test_write_confirm_requires_tty(tmp_path: Path, monkeypatch):
    """confirm=True + stdin ohne TTY -> CMISafetyError (kein --force,
    kein CMI-Write)."""
    monkeypatch.setenv("CMI_ROLE", "prod")
    reset_settings_cache()
    settings = _settings(tmp_path, role="prod")
    fake = FakeMindererCMI(EG)
    service = MindererService(settings, client=_client_for(settings, fake))
    monkeypatch.setattr(sys, "stdin", _NoTTY())
    try:
        with pytest.raises(MindererAbortError, match="interaktiver Confirm"):
            await service.write("eg", "2.5", confirm=True)
        assert fake.change_calls == []
    finally:
        reset_settings_cache()
    await service.client.aclose()


async def test_write_confirm_yes_proceeds(tmp_path: Path, monkeypatch, capsys):
    """confirm=True + TTY + 'y' -> der Write läuft (mockt prod)."""
    monkeypatch.setenv("CMI_ROLE", "prod")
    reset_settings_cache()
    settings = _settings(tmp_path, role="prod")
    fake = FakeMindererCMI(EG, pre_value="2.00", post_value="2.50")
    service = MindererService(settings, client=_client_for(settings, fake))
    monkeypatch.setattr(sys, "stdin", _FakeTTY())
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    try:
        result = await service.write("eg", "2.5", confirm=True)
        assert result.applied is True
        assert fake.change_calls == [{"changeadrx2": EG_ROHE, "changetox2": "2.50"}]
        out = capsys.readouterr().out
        assert "Rolle:            prod" in out
        assert "echt (!= Ist)" in out
        assert "y/N" in out
    finally:
        reset_settings_cache()
    await service.client.aclose()


async def test_write_confirm_enter_aborts(tmp_path: Path, monkeypatch, capsys):
    """confirm=True + TTY + Enter (leer) = N -> Abbruch: kein Write, kein
    Audit, klare Meldung (CLI mappt das auf exit 0)."""
    monkeypatch.setenv("CMI_ROLE", "prod")
    reset_settings_cache()
    settings = _settings(tmp_path, role="prod")
    fake = FakeMindererCMI(EG, pre_value="2.00")
    service = MindererService(settings, client=_client_for(settings, fake))
    monkeypatch.setattr(sys, "stdin", _FakeTTY())
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    try:
        with pytest.raises(MindererAbortError, match="abgebrochen"):
            await service.write("eg", "2.5", confirm=True)
        assert fake.change_calls == []
        assert _audit_lines(settings) == []
        out = capsys.readouterr().out
        assert "Aktueller Wert:   '2.00'" in out
    finally:
        reset_settings_cache()
    await service.client.aclose()


# ---------------------------------------------------------------------------
# CLI (Subprocess, offline: keine CMI-Traffic möglich; Argument-Pfade)
# ---------------------------------------------------------------------------
def _cli_env(tmp_path: Path, role: str = "dev") -> dict:
    import os

    env = dict(os.environ)
    env["CMI_ROLE"] = role
    env["AUDIT_JSONL_PATH"] = str(tmp_path / "audit" / "minderer.jsonl")
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return env


def _run_cli(*args: str, tmp_path: Path, role: str = "dev", input_text: str | None = None):
    """``python -m app.cmi.minderer`` in einem Subprocess (offline)."""
    return subprocess.run(
        [sys.executable, "-m", "app.cmi.minderer", *args],
        capture_output=True,
        text=True,
        env=_cli_env(tmp_path, role),
        input=input_text,
        timeout=120,
    )


def test_cli_status_unknown_zone_exit_1(tmp_path: Path):
    """Zone-Validierung OHNE Netz: exit 1 + klare Meldung."""
    proc = _run_cli("status", "xx", tmp_path=tmp_path)
    assert proc.returncode == 1
    assert "unknown Minderer-Zone" in (proc.stderr + proc.stdout)


def test_cli_write_invalid_volt_exit_1(tmp_path: Path):
    """Volt-Validierung (Range-Guard) OHNE Netz: exit 1."""
    proc = _run_cli("write", "eg", "1.5", tmp_path=tmp_path)
    assert proc.returncode == 1
    assert "außerhalb" in (proc.stderr + proc.stdout)


def test_cli_write_without_tty_exit_2(tmp_path: Path):
    """``write`` ohne TTY (stdin gepiped) -> exit 2 + Hinweis, KEIN
    CMI-Verkehr (keine Audit-Zeile; der Confirm-Check läuft vor dem
    Read-back)."""
    proc = _run_cli("write", "eg", "2.5", tmp_path=tmp_path, input_text="")
    assert proc.returncode == 2
    assert "interaktiver Confirm" in proc.stderr
    audit = tmp_path / "audit" / "minderer.jsonl"
    assert not audit.exists()


def test_cli_missing_subcommand_or_zone_exits_nonzero(tmp_path: Path):
    """argparse: fehlendes Subkommando / fehlende Zone -> exit 2;
    in beiden Fällen keine CMI-Interaktion."""
    proc = subprocess.run(
        [sys.executable, "-m", "app.cmi.minderer"],
        capture_output=True,
        text=True,
        env=_cli_env(tmp_path),
        timeout=120,
    )
    assert proc.returncode == 2  # argparse: subcommand required
    proc2 = _run_cli("status", tmp_path=tmp_path)
    assert proc2.returncode == 2  # argparse: zone required


def test_cli_role_defaults_to_dev(tmp_path: Path):
    """CMI_ROLE unset im CLI-Env -> Settings-Default dev (kein echter
    Write möglich): die Rolle wird von der CLI sichtbar gemacht (dev)."""
    import os

    env = dict(os.environ)
    env.pop("CMI_ROLE", None)
    env["AUDIT_JSONL_PATH"] = str(tmp_path / "audit" / "minderer.jsonl")
    proc = subprocess.run(
        [sys.executable, "-c", "from app.config import get_settings; print(get_settings().cmi_role)"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "dev"


# --- main() (Settings-/Service-Fabrik injiziert; Fake-Transport = Mock) ---
def _patched_main(tmp_path: Path, role: str, args: list[str], monkeypatch,
                  confirm_answer: str = "y", post_value: str = "2.00"):
    """``main()`` mit injizierten Settings + MindererService (Fake-Transport
    -> 0 echte CMI-Aufrufe). CMI_ROLE wird zusätzlich in die ENV gesetzt +
    Cache-Reset. ``post_value`` default "2.00" = wertstabil (No-op/status);
    für echte Writes mit ``post_value="2.50"`` übergeben."""
    monkeypatch.setenv("CMI_ROLE", role)
    reset_settings_cache()
    settings = _settings(tmp_path, role=role)
    fake = FakeMindererCMI(EG, pre_value="2.00", post_value=post_value)
    service = MindererService(settings, client=_client_for(settings, fake))
    monkeypatch.setattr(minderer_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(minderer_mod, "MindererService", lambda s: service)
    monkeypatch.setattr(sys, "stdin", _FakeTTY())
    monkeypatch.setattr("builtins.input", lambda *a, **k: confirm_answer)
    rc = minderer_mod.main(args)
    reset_settings_cache()
    return rc, service, settings


def test_main_write_tty_yes_exit_0_and_audit(tmp_path: Path, monkeypatch, capsys):
    """main(): write mit TTY + 'y' (prod) -> exit 0 + Audit-Zeile +
    Read-back-Ausgabe."""
    rc, service, settings = _patched_main(
        tmp_path, "prod", ["write", "eg", "2.5"], monkeypatch, "y", post_value="2.50"
    )
    assert rc == 0
    assert len(_audit_lines(settings)) == 1
    out = capsys.readouterr().out
    assert "Minderer-Write ausgefuehrt" in out
    assert "Read-back '2.50'" in out
    _run(service.client.aclose())


def test_main_write_tty_no_exit_0_no_audit(tmp_path: Path, monkeypatch, capsys):
    """main(): write mit TTY + Enter (N) -> exit 0 (Abbruch), kein Write,
    kein Audit."""
    rc, service, settings = _patched_main(
        tmp_path, "prod", ["write", "eg", "2.5"], monkeypatch, ""
    )
    assert rc == 0
    assert _audit_lines(settings) == []
    _run(service.client.aclose())


def test_main_status_exit_0_prints_role_and_value(tmp_path: Path, monkeypatch, capsys):
    """main(): status -> exit 0; Rolle (dev Default) + Wert + Adressen
    werden gedruckt."""
    rc, service, _ = _patched_main(tmp_path, "dev", ["status", "eg"], monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Rolle:            dev" in out
    assert "2.00" in out
    assert EG in out
    _run(service.client.aclose())


def test_main_status_unknown_zone_exit_1(tmp_path: Path, monkeypatch):
    """main(): status mit unbekannter Zone -> exit 1 (ValueError-Mapping)."""
    rc, service, _ = _patched_main(tmp_path, "dev", ["status", "xx"], monkeypatch)
    assert rc == 1
    _run(service.client.aclose())


def test_main_noop_exit_0_and_audit(tmp_path: Path, monkeypatch, capsys):
    """main(): noop (dev) -> exit 0 + Audit-Zeile (wertneutraler No-op)."""
    rc, service, settings = _patched_main(tmp_path, "dev", ["noop", "eg"], monkeypatch)
    assert rc == 0
    lines = _audit_lines(settings)
    assert len(lines) == 1
    assert lines[0]["kind"] == "minderer_noop"
    assert lines[0]["role"] == "dev"
    assert "No-op bestaetigt" in capsys.readouterr().out
    _run(service.client.aclose())


def test_main_write_dev_role_gate_exit_1(tmp_path: Path, monkeypatch, capsys):
    """main(): write in DEV -> Role-Gate: exit 1 + dev-Meldung, KEIN Write
    (confirm 'y' wird trotzdem gefragt; dann das Gate, kein change.cgi)."""
    rc, service, settings = _patched_main(
        tmp_path, "dev", ["write", "eg", "2.5"], monkeypatch, "y"
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "CMI_ROLE=dev" in err
    assert _audit_lines(settings) == []
    _run(service.client.aclose())


def test_main_write_no_tty_exit_2(tmp_path: Path, monkeypatch, capsys):
    """main(): write ohne TTY (in-Prozess) -> exit 2 + Hinweis, kein CMI."""
    monkeypatch.setenv("CMI_ROLE", "prod")
    reset_settings_cache()
    settings = _settings(tmp_path, role="prod")
    fake = FakeMindererCMI(EG, pre_value="2.00")
    service = MindererService(settings, client=_client_for(settings, fake))
    monkeypatch.setattr(minderer_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(minderer_mod, "MindererService", lambda s: service)
    monkeypatch.setattr(sys, "stdin", _NoTTY())
    try:
        rc = minderer_mod.main(["write", "eg", "2.5"])
        assert rc == 2
        assert "interaktiver Confirm" in capsys.readouterr().err
    finally:
        reset_settings_cache()
    _run(service.client.aclose())


def test_main_status_cmi_down_exit_1(tmp_path: Path, monkeypatch, capsys):
    """main(): status bei CMI-Down (CMIConnectionError) -> exit 1 +
    CMI-Fehler-Meldung."""
    from app.cmi.exceptions import CMIConnectionError

    monkeypatch.setenv("CMI_ROLE", "dev")
    reset_settings_cache()
    settings = _settings(tmp_path)
    service = MindererService(settings, client=object())

    async def _down(_zone: str):
        raise CMIConnectionError("CMI down (test)")

    monkeypatch.setattr(minderer_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(minderer_mod, "MindererService", lambda s: service)
    monkeypatch.setattr(service, "readback", _down)
    try:
        rc = minderer_mod.main(["status", "eg"])
        assert rc == 1
        assert "CMI-Fehler" in capsys.readouterr().err
    finally:
        reset_settings_cache()


# ---------------------------------------------------------------------------
# audit_append-Helper
# ---------------------------------------------------------------------------
def test_audit_append_creates_parent_and_appends(tmp_path: Path):
    target = tmp_path / "deep" / "audit" / "minderer.jsonl"
    audit_append(
        {"kind": "minderer_write", "zone": "eg", "address": EG, "from": "2.00",
         "to": "2.50", "role": "prod", "source": "cli", "ok": True, "detail": None},
        path=target,
    )
    lines = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    assert lines[0]["kind"] == "minderer_write"
    assert "+0" in lines[0]["ts"]  # Europe/Berlin aware (±01/±02)
    audit_append(
        {"kind": "minderer_noop", "zone": "eg", "address": EG, "from": "2.50",
         "to": "2.50", "role": "dev", "source": "route", "ok": True, "detail": None},
        path=target,
    )
    lines = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[1]["source"] == "route"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_settings():
    """CMI_ROLE-Env-Änderungen dürfen nicht zwischen Tests durchrutschen."""
    reset_settings_cache()
    yield
    reset_settings_cache()
