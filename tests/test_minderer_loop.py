"""Tests für den autonomen Minderer-Loop Zone EG (``app.cmi.minderer_loop``).

v0.6-A' Phase 2 + **v2 (21.11): O8-Durchfluss-Gate + ΔT-Verhältnis-
Feedback.**  In-Gateway-Regelkreis für die EG-Minderer-Sollspannung
(a18) -- Ziel-ΔT ≈ 10 K, Envelope [2,7] V, max 1×/h (bestehendes
Rate-Limit), Ground-Truth-Verifikation per CMI-/LOG a18 (nicht nur
CMI-``OK``), Transient-Handling (12:49-Fenster: CMI bestätigt, UVR
wendet ~17 min nicht an).

v2-Steuerung (O8-ON = Regelzeit, NORMAL/ABSENK egal):

* O8-Gate: ON-Anteil im 30-min-Fenster des Raw-Window ≥ 0,5, sonst
  ``no-flow`` (0 CMI-Verkehr, nicht mal Read-back);
* Steuer-ΔT nur aus O8-AN-Samples (lag-korrigiert; ``dt ≤ 0`` →
  ``dt-invalid``);
* Verhältnis-Korrektur ``v_now · (ΔT_ist/ΔT_ziel)`` + Schritt-Klemme
  ± ``minderer_loop_max_step_v`` (Default 0,75 V) + Envelope-Clip;
* KEIN ``no-curve``-Kill mehr (Kurve = nur OOB-Band für Deadband ×2).

**100 % Fakes / Mocks -- 0 echte CMI-/LOG-Aufrufe:**

* ``FakeMindererService`` (Read-back + Write-Record + Rate-Limit-Simulation);
* ``FakeLogStore`` (cmi_log_pq.query-Interface für den Raum-T-Floor-Guard);
* ``FakeL2`` (injected ``l2_builder`` → state/diag/curve/raw_window;
  Fake-RawWindow mit ``pump``-Spalte: Zeitstempel-Grid + O8-Pattern);
* ``FakeLogClient`` (eigener Basic-Auth-Client für den Direktpfad
  /LOG a18; index + P=181-Synthetik-File via
  ``tests.test_cmi_log.make_log_file_181``, echtes
  ``parse_log_index`` + ``decode_cmi_log``).

UVR-Wartepause (:data:`UVR_APPLY_WAIT_S`, 120 s) wird per
``monkeypatch`` auf 0 s gekürzt.  Zeitbasis: echte ``time.time``
(Fake-/LOG-Files werden relativ zu ``now`` konstruiert).
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

import app.cmi.minderer_loop as ml
from app.cmi.exceptions import CMISafetyError
from app.cmi.minderer import MINDERER_ZONES
from app.cmi.minderer_loop import MindererL2Data, MindererLoop, ext_oob
from app.cmi.write import normalize_role
from app.config import Settings
from tests.test_cmi_log import _P181_DEFAULT_SLOTS, make_log_file_181

REPO_ROOT = Path(__file__).resolve().parent.parent
BERLIN_TZ = ml._BERLIN

#: EG-Fit (v0.6-A, data/l2/minderer_curve.json): ΔT = a/V + b·ext + c.
FIT = {
    "a": 21.3563,
    "b": -0.7815,
    "c": 8.2011,
    "ext_min": 4.0,
    "ext_max": 10.25,
    "usable": True,
    "n_points": 4,
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeMindererService:
    """MindererService-Fake: Read-back + Write-Record + Rate-Limit-Simulation.

    ``readback_value`` = aktueller CMI-Istwert.  Ein erfolgreicher Write
    setzt ihn auf den geschriebenen Wert (CMI bestätigt; das ist genau das
    12:49-Transient: CMI-Read-back zeigt den Befehlswert, während a18 am
    UVR (FakeLogClient) beim alten Wert kleben kann).  ``write_error`` wird
    erst RAUSGEWURFELN nachdem der Call gemeldet wurde (kein Wertwechsel).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        readback_value: str | None = "4.00",
        write_error: Exception | None = None,
        audit_path: Path,
    ) -> None:
        self.settings = settings
        self.readback_value = readback_value
        self.write_error = write_error
        self.audit_path = audit_path
        self.readback_calls: list[str] = []
        self.write_calls: list[dict[str, Any]] = []

    def address(self, zone: str) -> str:
        return MINDERER_ZONES[zone]

    def role(self) -> str:
        return normalize_role(self.settings.cmi_role)

    async def readback(self, zone: str) -> SimpleNamespace:
        self.readback_calls.append(zone)
        return SimpleNamespace(
            current_value=self.readback_value,
            min_value="2.00",
            max_value="10.00",
        )

    async def write(self, zone: str, volt: str | float, *, confirm: bool = True, source: str | None = None) -> SimpleNamespace:
        self.write_calls.append(
            {"zone": zone, "volt": str(volt), "confirm": confirm, "source": source}
        )
        if self.write_error is not None:
            raise self.write_error
        # CMI bestätigt den Write → Read-back zeigt fortan den Befehlswert.
        self.readback_value = f"{float(volt):.2f}"
        return SimpleNamespace(after=SimpleNamespace(current_value=self.readback_value))


class FakeLogStore:
    """cmi_log_pq.query-Fake (Raum-T-Floor-Guard)."""

    def __init__(self, points: list[dict] | None = None) -> None:
        self.points = points if points is not None else []
        self.calls: list[tuple] = []

    def query(
        self,
        device: str,
        channel: int,
        ad: str | None,
        start: str,
        end: str,
        *,
        agg: str = "last",
        interval: int | None = None,
        limit: int = 5,
    ) -> list[dict]:
        self.calls.append((device, channel, ad, start, end, agg, limit))
        return list(self.points)


_CURVE_DEFAULT: Any = object()  # Sentinel: „Default (FIT)“ vs explizites None


def _raw_window(
    n: int = 7200,
    *,
    pump_on: bool = True,
    pump_tail_n: int | None = None,
    end_offset_s: float = 10.0,
    vl: float = 45.0,
    rl_eg: float | None = 36.5,  # None → NaN-Reihe (keine gültige (VL,RL)-Paare)
    rl_gap: int = 3,
    rl_finite_head: int | None = None,
    pump_none: bool = False,
) -> SimpleNamespace:
    """Fake-RawWindow (``rm_diagnostics.RawWindow``-Attribute, SimpleNamespace).

    ``n`` Samples (10-s-Grid; Default n=7200 → 2 h = O8-Gate-Fenster
    ≥ 30 min) mit gemeinsamer ``datetime64[s]``-Zeitachse; ``pump`` =
    O8-Pattern (``pump_on`` = durchgehend 1; ``pump_tail_n`` = NUR die
    letzten N Samples 1 (davor 0); ``pump_none`` = NaN-Reihe; sonst 0);
    ``vl`` konstant; ``rl["eg"]`` = konstant ``rl_eg`` (lag-invariant)
    mit den ersten ``rl_gap`` Einträgen NaN (Lag-Shift-Verbrauch);
    ``rl_finite_head`` = eg-RL NUR in den ersten K Samples endlich
    (davor/dran NaN); alle übrigen Zonen = konstant (keine Drift).
    """
    t_end = np.datetime64(int(time.time() - end_offset_s), "s")
    ts = t_end - np.arange(n)[::-1] * np.timedelta64(10, "s")
    if pump_none:
        pump = np.full(n, np.nan)
    elif pump_tail_n is not None:
        pump = np.zeros(n)
        pump[n - pump_tail_n :] = 1.0
    else:
        pump = np.ones(n) if pump_on else np.zeros(n)
    vl_arr = np.full(n, vl)
    rl_arr = np.full(n, np.nan)
    if rl_eg is not None:
        if rl_finite_head is not None:
            rl_arr[:rl_finite_head] = rl_eg
        elif rl_gap < n:
            rl_arr[rl_gap:] = rl_eg
    return SimpleNamespace(
        ts=ts,
        vl=vl_arr,
        rl={"kell": vl_arr, "eg": rl_arr, "dg": vl_arr, "central": vl_arr},
        rm={},
        pump=pump,
        soll={},
        n=n,
    )


def _fake_l2(
    *,
    dt: float | None = 8.5,
    ext: float | None = 5.0,
    metric_gap_h: float = 0.0,
    curve: dict[str, Any] | None = _CURVE_DEFAULT,  # type: ignore[assignment]
    raw: Any = "default",
    pump_on: bool = True,
    raw_n: int = 7200,
    vl: float = 45.0,
    rl_eg_value: float | None = 36.5,
    rl_gap: int = 3,
    pump_none: bool = False,
    rl_eg_series: Any = None,
) -> MindererL2Data:
    """MindererL2Data-Instanz (FakeL2-Return; builder-injizierbar).

    ``curve``: Default (sentinel) = FIT (usable); explizit None =
    kein Artefakt; Dict = wie gegeben.

    v2-Fenster (``raw``): Default = Fake-RawWindow mit ``pump_on``
    (O8 durchgehend AN → Gate offen, ΔT_ist = vl − rl_eg_value);
    ``raw=None`` = kein Fenster; ``"off"`` = O8 durchgehend AUS;
    ``"short"`` = < 30 min Daten; ``"no-pair"`` = O8 NUR im letzten
    30-min-Fenster AN + eg-RL NUR davor endlich → ≥ 30 endliche
    Samples (PT1000-OK) aber < 10 gültige O8-ON-(VL,RL)-Paare →
    dt-unknown; ``"pump-none"`` = Pumpen-Spalle None;
    sonst = wie gegeben.  ``rl_eg_series``: Custom-Array für
    ``raw_window.rl["eg"]`` (PT1000-Drift-Guard-Tests; Default =
    Window-Standard).
    """
    state = SimpleNamespace(rows={"ext": ext}, metric_gap_h=metric_gap_h, ts=datetime.now(BERLIN_TZ))
    diag = SimpleNamespace(
        per_zone={
            "eg": {"delta_t_c": dt, "lag_s": 30, "minderer_soll_v": 10.0},
            "kell": {},
            "dg": {},
        },
        active=True,
    )
    if curve is _CURVE_DEFAULT:
        curve = {"zones": {"eg": dict(FIT)}}
    if raw == "default":
        raw = _raw_window(raw_n, pump_on=pump_on, vl=vl, rl_eg=rl_eg_value, rl_gap=rl_gap)
    elif raw == "off":
        raw = _raw_window(raw_n, pump_on=False, vl=vl, rl_eg=rl_eg_value, rl_gap=rl_gap)
    elif raw == "short":
        raw = _raw_window(180, pump_on=pump_on, vl=vl, rl_eg=rl_eg_value, rl_gap=rl_gap)
    elif raw == "no-pair":
        # O8 NUR im letzten 30-min-Fenster AN (Gate offen, frac = 1,0),
        # eg-RL NUR in der ersten Hälfte endlich (PT1000-Guard ≥ 30
        # endliche Samples OK) → im 30-min-Fenster 0 gültige O8-ON-
        # Paare → dt-unknown.
        raw = _raw_window(
            raw_n,
            pump_tail_n=180,
            vl=vl,
            rl_eg=rl_eg_value,
            rl_finite_head=raw_n // 2,
        )
    elif raw == "pump-none":
        raw = _raw_window(raw_n, pump_none=True, vl=vl, rl_eg=rl_eg_value, rl_gap=rl_gap)
    if rl_eg_series is not None and raw is not None:
        raw.rl["eg"] = rl_eg_series
    return MindererL2Data(
        state=state,
        diag=diag,
        curve=curve,
        raw_window=raw,
    )


class FakeLogClient:
    """CMILogClient-Fake für den Direktpfad /LOG a18 (index + P=181-Synthetik).

    ``a18_v`` = aktuelle a18-Sollspannung am UVR (V); ``end_epoch`` =
    ts des letzten Records (File-Ende).  Index + Datenfile werden pro
    ``get_bytes``-Aufruf (neu) konstruiert, so dass ein
    ``set_a18(...)`` zwischen Zyklen wirkt.
    """

    def __init__(self, a18_v: float | None, *, end_offset_s: float = 60.0, n_records: int = 10) -> None:
        self._a18_v = a18_v
        self._end_offset_s = end_offset_s
        self._n = n_records
        self.get_bytes_calls: list[str] = []
        self.aclose_calls = 0

    def set_a18(self, v: float | None) -> None:
        self._a18_v = v

    def _data_bytes(self) -> tuple[str, int, bytes]:
        now = time.time()
        end = int(now - self._end_offset_s)
        start = end - (self._n - 1) * 10
        dt = datetime.fromtimestamp(start, BERLIN_TZ)
        name = f"data_{dt.year}_{dt.month:02d}_{dt.day:02d}_00_00_00.log"
        slots = dict(_P181_DEFAULT_SLOTS)
        if self._a18_v is not None:
            slots[166] = int(round(self._a18_v * 100))  # dev2 a18 @166 (unsigned /100)
        _, _size, body = make_log_file_181(
            name, (dt.hour, dt.minute, dt.second), n_records=self._n, slots=slots
        )
        return f"/LOG/{dt.year}/{name}", len(body), body

    async def get_bytes(self, path: str) -> bytes:
        self.get_bytes_calls.append(path)
        if path.startswith("/LOG/info") and path.endswith(".log"):
            year = int(path.rsplit("/", 1)[-1].removeprefix("info").removesuffix(".log"))
            data_path, size, _body = self._data_bytes()
            if int(data_path.split("/")[2]) == year:
                return f"CMI\r\nstorekrit: 10\r\n{data_path} {size}\r\n".encode("latin-1")
            return b"CMI\r\nstorekrit: 10\r\n"
        data_path, _size, body = self._data_bytes()
        if path == data_path:
            return body
        raise AssertionError(f"unexpected /LOG path in test: {path}")

    async def aclose(self) -> None:
        self.aclose_calls += 1


def _loop(
    settings: Settings,
    *,
    service: FakeMindererService,
    log_store: FakeLogStore | None = None,
    l2: MindererL2Data | None = None,
    log_client: FakeLogClient | None = None,
) -> MindererLoop:
    def l2_builder(ts: datetime) -> MindererL2Data:
        assert l2 is not None
        return l2

    return MindererLoop(
        settings,
        service=service,
        log_store=log_store if log_store is not None else FakeLogStore(),
        l2_builder=l2_builder,
        now_fn=time.time,
        log_client_factory=lambda: log_client if log_client is not None else None,  # type: ignore[return-value]
    )


def _settings(tmp_path: Path, *, enabled: bool = True, role: str = "prod", **kwargs: Any) -> Settings:
    return Settings(
        cmi_base_url="http://cmi.test",
        cmi_user="admin",
        cmi_pass="admin",
        cmi_data_dir=tmp_path / "data",
        audit_jsonl_path=tmp_path / "audit" / "minderer.jsonl",
        cmi_role=role,
        l1_store_dir=tmp_path / "features",
        minderer_loop_enabled=enabled,
        **kwargs,
    )


def _audit_lines(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture()
def no_uvr_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """UVR-Erstcheck-Wartepause (120 s) auf 0 s kürzen."""
    monkeypatch.setattr(ml, "UVR_APPLY_WAIT_S", 0.0)


def _room_points(age_s: float, value: float = 21.5) -> list[dict]:
    ts = (datetime.now(BERLIN_TZ) - timedelta(seconds=age_s)).strftime("%Y-%m-%d %H:%M:%S")
    return [{"ts": ts, "value": value}]


def _force_dt_target(l2: MindererL2Data, dt_target: float = 10.0, *, vl: float = 45.0) -> None:
    """v2-Testhilfe: eg-RL so setzen, dass ΔT_ist = ``dt_target`` wird
    (konstante RL über das Fenster; O8-Gate + PT1000-Guard bleiben grün).

    In v2 hängt die Stellgröße an v_now: nach einem Write (v_now ändert
    sich) muss der Fake-ΔT-Input so gewährt werden, dass der Loop zum
    no-change-Regime konvergiert (v_target = v_now), sonst würde jeder
    Folgezyklus einen NEUEN (anderen) Target-Write anstoßen.  Das
    spiegelt die v1-Semantik (stabile Kurve → stabiles v_target),
    die der Transient-Test (Resend desselben Zielwerts) verifizieren
    soll.
    """
    win = l2.raw_window
    rl_arr = np.full(win.n, vl - dt_target)
    win.rl["eg"] = rl_arr


# ---------------------------------------------------------------------------
# 1./2./16. Gate: Kill-Switch / Rolle / Zonewahl → 0 CMI-Verkehr
# ---------------------------------------------------------------------------
async def test_gate_disabled_no_cmi_traffic(tmp_path: Path) -> None:
    """Default-Settings (kein MINDERER_LOOP_ENABLED) → inaktiv, 0 CMI-Verkehr."""
    settings = _settings(tmp_path, enabled=False)
    service = FakeMindererService(settings, audit_path=settings.audit_jsonl_path)
    loop = _loop(settings, service=service, log_client=FakeLogClient(4.0))
    await loop.cycle_once()
    assert service.readback_calls == []
    assert service.write_calls == []
    s = loop.status
    assert s.enabled is False
    assert s.last_kill_reason == "disabled"
    # Zyklus lief sauber (Gate abgelehnt, kein Fehler) → ok=True; der
    # Kill-Grund trägt die Semantik (separates Feld, s. Status-Doku).
    assert s.last_cycle_ok is True
    assert s.last_write is None


async def test_gate_role_dev_no_cmi_traffic(tmp_path: Path) -> None:
    """Rolle dev (enabled=True) → kein Verkehr, Status role=dev."""
    settings = _settings(tmp_path, enabled=True, role="dev")
    service = FakeMindererService(settings, audit_path=settings.audit_jsonl_path)
    loop = _loop(settings, service=service, log_client=FakeLogClient(4.0))
    await loop.cycle_once()
    assert service.readback_calls == []
    assert service.write_calls == []
    s = loop.status
    assert s.role == "dev"
    assert s.last_kill_reason == "role=dev"


async def test_gate_role_invalid_fails_safe(tmp_path: Path) -> None:
    """Ungültiger Role (PROD) → fail-safe dev → inaktiv."""
    settings = _settings(tmp_path, enabled=True, role="PROD")
    service = FakeMindererService(settings, audit_path=settings.audit_jsonl_path)
    loop = _loop(settings, service=service, log_client=FakeLogClient(4.0))
    await loop.cycle_once()
    assert service.write_calls == []
    assert loop.status.last_kill_reason == "role=dev"


async def test_zone_not_eg_inactive(tmp_path: Path) -> None:
    """minderer_loop_zone='dg' → inaktiv + Status-Flag (kein Verkehr, kein Crash)."""
    settings = _settings(tmp_path, enabled=True, role="prod", minderer_loop_zone="dg")
    service = FakeMindererService(settings, audit_path=settings.audit_jsonl_path)
    loop = _loop(settings, service=service, log_client=FakeLogClient(4.0))
    await loop.cycle_once()
    assert service.readback_calls == []
    assert service.write_calls == []
    assert loop.status.zone == "dg"
    assert loop.status.last_kill_reason.startswith("zone=dg")


# ---------------------------------------------------------------------------
# v2: O8-Durchfluss-Gate (fail-safe) + dt-unknown/invalid
# ---------------------------------------------------------------------------
async def test_o8_off_no_cmi_traffic(tmp_path: Path) -> None:
    """Task 1 (Fail-Safe-Nachweis): O8 OFF (< 50 % ON-Anteil in 30 min) →
    0 CMI-Verkehr (0 Read-backs, 0 Writes), Kill ``no-flow``,
    ``o8_on_frac`` im Decision-Block (berechneter Wert)."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, audit_path=settings.audit_jsonl_path)
    log_client = FakeLogClient(4.0)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(raw="off"),  # O8 durchgehend 0
        log_client=log_client,
    )
    await loop.cycle_once()
    assert service.readback_calls == []
    assert service.write_calls == []
    assert log_client.get_bytes_calls == []  # auch kein /LOG-Verkehr
    s = loop.status
    assert s.last_kill_reason == "no-flow"
    assert s.last_cycle_ok is True
    d = s.last_decision
    assert d["reason"] == "no-flow"
    assert d["o8_on_frac"] == 0.0
    assert d["written"] is False


async def test_o8_gate_missing_raw_window_failsafe(tmp_path: Path) -> None:
    """Raw-Window fehlt/zu kurz/unzureichend → O8-Gate zu (``no-flow``),
    0 CMI-Verkehr (fail-safe, VOR CMI-Read-back)."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(raw=None),  # kein Fenster
        log_client=FakeLogClient(4.0),
    )
    await loop.cycle_once()
    assert service.readback_calls == []
    assert service.write_calls == []
    assert loop.status.last_kill_reason == "no-flow"
    assert loop.status.last_decision["o8_on_frac"] is None


async def test_o8_gate_short_window_failsafe(tmp_path: Path) -> None:
    """Raw-Window < 30 min Daten → O8-Gate zu (``no-flow``), 0 CMI-Verkehr."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(raw="short"),  # 180 Samples = 30 min? → < 1800 s
        log_client=FakeLogClient(4.0),
    )
    await loop.cycle_once()
    assert service.readback_calls == []
    assert service.write_calls == []
    assert loop.status.last_kill_reason == "no-flow"


async def test_o8_gate_pump_column_missing_failsafe(tmp_path: Path) -> None:
    """Pumpen-Spalte (pump) None → O8-Gate zu (``no-flow``), 0 CMI-Verkehr."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(raw="pump-none"),
        log_client=FakeLogClient(4.0),
    )
    await loop.cycle_once()
    assert service.readback_calls == []
    assert service.write_calls == []
    assert loop.status.last_kill_reason == "no-flow"
    assert loop.status.last_decision["o8_on_frac"] is None


# ---------------------------------------------------------------------------
# 3./4. v2: Verhältnis-Korrektur (drosseln / öffnen) + Schritt-Klemme + Clip
# ---------------------------------------------------------------------------
async def test_v2_throttle_write_two_dp_source_loop(tmp_path: Path, no_uvr_wait: None) -> None:
    """v2 Drossel-Write: v_now=4,0 V, ΔT_ist=8,5 K → raw 3,40 V
    (4,0·0,85) → Schritt-Klemme greift NICHT (0,60 ≤ 0,75) →
    write('3.40', loop, no-confirm)."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)
    log_client = FakeLogClient(3.40)  # a18 folgt sofort (Erstcheck ok)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(ext=5.0),
        log_client=log_client,
    )
    await loop.cycle_once()
    assert service.write_calls == [
        {"zone": "eg", "volt": "3.40", "confirm": False, "source": "loop"}
    ]
    assert loop.status.last_kill_reason is None
    assert loop.status.last_cycle_ok is True
    d = loop.status.last_decision
    assert d["written"] is True and d["reason"] is None
    assert d["v_now"] == 4.0 and d["v_target"] == 3.40 and d["dt_k"] == 8.5
    assert d["o8_on_frac"] == 1.0
    assert loop.status.last_write["v"] == 3.40 and loop.status.last_write["resend"] is False


async def test_v2_open_direction_write(tmp_path: Path, no_uvr_wait: None) -> None:
    """v2 öffnen-Richtung: v_now=3,0 V, ΔT_ist=13 K → raw 3,9 V →
    Schritt-Klemme (±0,75) → 3,75 V → Write (beide Richtungen erlaubt)."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="3.00", audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(rl_eg_value=32.0),  # ΔT_ist = 45 − 32 = 13 K
        log_client=FakeLogClient(3.75),
    )
    await loop.cycle_once()
    assert service.write_calls == [
        {"zone": "eg", "volt": "3.75", "confirm": False, "source": "loop"}
    ]


async def test_v2_throttle_clamped_by_max_step(tmp_path: Path, no_uvr_wait: None) -> None:
    """Task-Zahl 2 (Live-Bezug 21.11): v_now=5,6 V, ΔT_ist=6,5 K →
    raw 3,64 V (5,6·0,65) → Schritt-Klemme ±0,75 → 4,85 V → Write
    (Klemmung IN der Envelope; Ziel bleibt 10 K, max. 0,75 V/h)."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="5.60", audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(rl_eg_value=38.5),  # ΔT_ist = 45 − 38,5 = 6,5 K
        log_client=FakeLogClient(4.85),
    )
    await loop.cycle_once()
    assert service.write_calls == [
        {"zone": "eg", "volt": "4.85", "confirm": False, "source": "loop"}
    ]
    assert loop.status.last_decision["v_target"] == 4.85


async def test_step_clamp_down_and_up(tmp_path: Path, no_uvr_wait: None) -> None:
    """Schritt-Klemme beide Richtungen: v_now=2,0, ΔT=15 K → raw 3,0 →
    2,75 (2,0+0,75); umgekehrt v_now=7,0, ΔT=5 K → raw 3,5 → 6,25 (7,0−0,75)."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="2.00", audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(rl_eg_value=30.0),  # ΔT_ist = 45 − 30 = 15 K
        log_client=FakeLogClient(2.75),
    )
    await loop.cycle_once()
    assert service.write_calls and service.write_calls[0]["volt"] == "2.75"

    # Umgekehrte Richtung (frischer Loop, eigener Service).
    service2 = FakeMindererService(settings, readback_value="7.00", audit_path=settings.audit_jsonl_path)
    loop2 = _loop(
        settings,
        service=service2,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(rl_eg_value=40.0),  # ΔT_ist = 45 − 40 = 5 K
        log_client=FakeLogClient(6.25),
    )
    await loop2.cycle_once()
    assert service2.write_calls and service2.write_calls[0]["volt"] == "6.25"


async def test_step_clamp_plus_envelope_clip(tmp_path: Path, no_uvr_wait: None) -> None:
    """Klemmen-Interaktion: v_now=2,0, ΔT=20 K → raw 4,0 → Schritt-Klemme
    2,75 (Envelope [2,0; 7,0] unverletzt) → Write 2,75.  Und Clip-Nähe:
    v_now=2,3, ΔT=0,4 K → raw 0,92 → Klemme 1,55 → Clip auf 2,0
    (Envelope-Min) → |2,0−2,3|=0,3 ≥ Deadband → Write 2,00."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="2.00", audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(rl_eg_value=25.0),  # ΔT_ist = 45 − 25 = 20 K
        log_client=FakeLogClient(2.75),
    )
    await loop.cycle_once()
    assert service.write_calls and service.write_calls[0]["volt"] == "2.75"

    # Und Clip-Nähe: v_now=2,3, ΔT=0,4 K → raw 0,92 → Klemme 1,55 →
    # Clip auf 2,0 (Envelope-Min) → |2,0−2,3|=0,3 ≥ Deadband → Write 2,00.
    service2 = FakeMindererService(settings, readback_value="2.30", audit_path=settings.audit_jsonl_path)
    loop2 = _loop(
        settings,
        service=service2,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(rl_eg_value=44.6),  # ΔT_ist = 45 − 44,6 = 0,4 K
        log_client=FakeLogClient(2.0),
    )
    await loop2.cycle_once()
    assert service2.write_calls and service2.write_calls[0]["volt"] == "2.00"


# ---------------------------------------------------------------------------
# 5./6./7. Guards: Deadband, Raum-T-Floor, Raum-T-Staleness
# ---------------------------------------------------------------------------
async def test_deadband_no_change(tmp_path: Path, no_uvr_wait: None) -> None:
    """v2 ΔT_ist = Ziel (10 K) → Verhältnis-Ziel = v_now · 1,0 = v_now →
    |Δ| = 0 < 0,05 → kein Write, Status no-change (Deadband).  v_now =
    4,0 V (Envelope-Mittpunkt, keine Klemmung/Clip-Interferenz)."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(ext=5.0, rl_eg_value=35.0),  # ΔT_ist = 45 − 35 = 10 K = Ziel
        log_client=FakeLogClient(4.0),
    )
    await loop.cycle_once()
    assert service.write_calls == []
    assert loop.status.last_kill_reason == "no-change"
    d = loop.status.last_decision
    assert d["written"] is False and d["reason"] == "no-change"
    assert d["v_now"] == 4.0 and d["v_target"] == 4.0 and d["dt_k"] == 10.0


async def test_room_floor_blocks_throttle(tmp_path: Path, no_uvr_wait: None) -> None:
    """Raum-T 20.5 < 20.8 AND v_target < v_now (Drossel) → blockiert (room-floor).

    v2: v_now=4,0, ΔT_ist=5 K → raw 2,0 → Schritt-Klemme 3,25 < 4,0 →
    Drossel-Richtung → Raum-Floor blockiert (kein weiteres Drosseln).
    """
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0, value=20.5)),
        l2=_fake_l2(rl_eg_value=40.0),  # ΔT_ist = 45 − 40 = 5 K
        log_client=FakeLogClient(3.25),
    )
    await loop.cycle_once()
    assert service.write_calls == []
    assert loop.status.last_kill_reason == "room-floor"
    d = loop.status.last_decision
    assert d["reason"] == "room-floor" and d["v_target"] == 3.25 and d["v_now"] == 4.0


async def test_room_floor_allows_open(tmp_path: Path, no_uvr_wait: None) -> None:
    """Raum-T 20.5 < 20.8, aber öffnen-Richtung (v_target > v_now) → Write OK.

    v2: v_now=3,0, ΔT_ist=13 K → raw 3,9 → Schritt-Klemme 3,75 > 3,0 →
    Anheben bleibt erlaubt (Raum zu kalt → Minderer öffnen).
    """
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="3.00", audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0, value=20.5)),
        l2=_fake_l2(rl_eg_value=32.0),  # ΔT_ist = 45 − 32 = 13 K
        log_client=FakeLogClient(3.75),
    )
    await loop.cycle_once()
    assert service.write_calls and service.write_calls[0]["volt"] == "3.75"
    assert loop.status.last_kill_reason is None


async def test_room_t_stale(tmp_path: Path, no_uvr_wait: None) -> None:
    """Letzter cmi_log_pq-ts > 30 min alt → kein Write, room-t-stale."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(3 * 3600.0)),
        l2=_fake_l2(ext=5.0),
        log_client=FakeLogClient(3.74),
    )
    await loop.cycle_once()
    assert service.write_calls == []
    assert loop.status.last_kill_reason == "room-t-stale"
    # Guard-Lesung tatsächlich über den injizierten Store (winsol:2 ch 7 A).
    assert loop.log_store.calls
    dev, ch, ad, *_ = loop.log_store.calls[0]
    assert (dev, ch, ad) == ("winsol:2", 7, "A")


async def test_room_t_empty_store_stale(tmp_path: Path, no_uvr_wait: None) -> None:
    """Leerer Store (keine Punkte) → room-t-stale."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore([]),
        l2=_fake_l2(ext=5.0),
        log_client=FakeLogClient(3.74),
    )
    await loop.cycle_once()
    assert loop.status.last_kill_reason == "room-t-stale"


# ---------------------------------------------------------------------------
# 8. PT1000-Drift-Guard
# ---------------------------------------------------------------------------
async def test_pt1000_drift_jump(tmp_path: Path, no_uvr_wait: None) -> None:
    """Springer > 5 K zwischen aufeinanderfolgenden Samples → sensor-drift.

    (O8-AN-Fenster; der Drift-Guard steht VOR der ΔT-Steuerberechnung,
    hier aber NACH dem O8-Gate → Gate muss offen sein.)
    """
    arr = np.full(7200, 40.0)
    arr[360] = 40.0 + 6.0  # +6 K-Sprung (volle Länge = ts-aliniert)
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(rl_eg_series=arr),  # O8-AN-Fenster, eg-RL mit 6 K-Sprung
        log_client=FakeLogClient(3.74),
    )
    await loop.cycle_once()
    assert service.write_calls == []
    assert loop.status.last_kill_reason == "sensor-drift"


async def test_pt1000_too_few_samples(tmp_path: Path, no_uvr_wait: None) -> None:
    """< 30 endliche Samples in raw_window.rl[eg] → sensor-drift."""
    arr = np.full(7200, np.nan)
    arr[:25] = 40.0  # nur 25 endliche Samples
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(rl_eg_series=arr),
        log_client=FakeLogClient(3.74),
    )
    await loop.cycle_once()
    assert loop.status.last_kill_reason == "sensor-drift"


async def test_pt1000_guard_missing_window(tmp_path: Path, no_uvr_wait: None) -> None:
    """Roh-Fenster fehlt (None) → O8-Gate vor dem Drift-Guard: no-flow
    (fail-safe, 0 CMI-Verkehr); der Drift-Guard kommt gar nicht dran."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(raw=None),
        log_client=FakeLogClient(3.74),
    )
    await loop.cycle_once()
    assert loop.status.last_kill_reason == "no-flow"


async def test_pt1000_guard_missing_zone_in_window(tmp_path: Path, no_uvr_wait: None) -> None:
    """Roh-Fenster OHNE eg-RL (``rl['eg']=None``) → sensor-drift
    (fail-safe), nach offenem O8-Gate."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)
    l2 = _fake_l2()
    l2.raw_window.rl["eg"] = None
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=l2,
        log_client=FakeLogClient(3.74),
    )
    await loop.cycle_once()
    assert loop.status.last_kill_reason == "sensor-drift"


# ---------------------------------------------------------------------------
# 9./10./15. Store-Staleness, no-curve, ext-OOB
# ---------------------------------------------------------------------------
async def test_store_stale(tmp_path: Path, no_uvr_wait: None) -> None:
    """metric_gap_h=2.5 > 1.5 → kein Write, store-stale."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(metric_gap_h=2.5),
        log_client=FakeLogClient(3.74),
    )
    await loop.cycle_once()
    assert service.write_calls == []
    assert loop.status.last_kill_reason == "store-stale"
    assert loop.status.last_decision["reason"] == "store-stale"


async def test_curve_missing_no_kill_write_allowed(tmp_path: Path, no_uvr_wait: None) -> None:
    """v2 Abweichung: Curve-Artefakt fehlt (None) → KEIN ``no-curve``-Kill
    mehr (Kurve = nur OOB-Band/Advisory); O8-ON + validem ΔT → Write
    erlaubt (Verhältnis-Korrektur + Schritt-Klemme)."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(curve=None),
        log_client=FakeLogClient(3.4),
    )
    await loop.cycle_once()
    assert service.write_calls and service.write_calls[0]["volt"] == "3.40"
    assert loop.status.last_kill_reason is None
    assert loop.status.last_decision["ext_oob"] is False


async def test_curve_zone_not_usable_no_kill_write_allowed(tmp_path: Path, no_uvr_wait: None) -> None:
    """v2 Abweichung: Zone-Entry usable=False (linear-Fallback) → KEIN
    ``no-curve``-Kill mehr; OOB-Band nicht lesbar → oob=False, Deadband
    bleibt 0,05 (nicht ×2) → Write erlaubt."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)
    bad_curve = {"zones": {"eg": {"usable": False, "a": None, "b": None, "c": None}}}
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(curve=bad_curve),
        log_client=FakeLogClient(3.4),
    )
    await loop.cycle_once()
    assert service.write_calls and service.write_calls[0]["volt"] == "3.40"
    assert loop.status.last_kill_reason is None
    assert loop.status.last_decision["ext_oob"] is False


async def test_dt_unknown_no_pairs(tmp_path: Path, no_uvr_wait: None) -> None:
    """O8-AN, aber < 10 gültige (VL,RL)-Paare im 30-min-Fenster →
    dt-unknown, kein Write (besteht, v2-Quelle = Pumpen-ON-Paare)."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(raw="no-pair"),
        log_client=FakeLogClient(3.4),
    )
    await loop.cycle_once()
    assert service.write_calls == []
    assert loop.status.last_kill_reason == "dt-unknown"
    d = loop.status.last_decision
    assert d["dt_k"] is None
    assert d["o8_on_frac"] is not None and d["o8_on_frac"] > 0.99


async def test_dt_invalid_negative(tmp_path: Path, no_uvr_wait: None) -> None:
    """ΔT ≤ 0 (RL ≥ VL, Sensor-Transient) → dt-invalid, kein Write (neu,
    v2).  v_now=4,0, ΔT=−1 K → (sonst: raw 4,4 → öffnen; hier: Kill)."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(rl_eg_value=46.0),  # ΔT_ist = 45 − 46 = −1 K
        log_client=FakeLogClient(4.0),
    )
    await loop.cycle_once()
    assert service.write_calls == []
    assert loop.status.last_kill_reason == "dt-invalid"
    d = loop.status.last_decision
    assert d["dt_k"] == -1.0 and d["o8_on_frac"] == 1.0


async def test_ext_unknown(tmp_path: Path, no_uvr_wait: None) -> None:
    """ext=NULL → ext-unknown, kein Write."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(ext=None),
        log_client=FakeLogClient(3.74),
    )
    await loop.cycle_once()
    assert service.write_calls == []
    assert loop.status.last_kill_reason == "ext-unknown"


async def test_ext_oob_write_allowed_flag_and_deadband_x2(tmp_path: Path, no_uvr_wait: None) -> None:
    """ext=20 (OOB) → ext_oob-Flag + Deadband 0.10 statt 0.05 (OOB-
    Deadband = breiter → no-change, in-Band → Write).

    Diskriminator: v_now=2,06, ΔT_ist=8 K → raw 1,648 → Klemme → Clip
    2,00 → |Δ|=0,06.  In-Band (Deadband 0,05): 0,06 < 0,05 = False →
    WIRD geschrieben.  OOB (0,10): 0,06 < 0,10 → no-change.
    """
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="2.06", audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(ext=20.0, rl_eg_value=37.0),  # ΔT_ist = 45 − 37 = 8 K, OOB
        log_client=FakeLogClient(2.0),
    )
    await loop.cycle_once()
    assert service.write_calls == []
    assert loop.status.last_kill_reason == "no-change"
    assert loop.status.last_decision["ext_oob"] is True
    # Referenz: ext_oob-Prüfung (Kurve-OOB-Band; v2: ohne Kurve oob=False)
    assert ext_oob(FIT, 20.0) is True
    assert ext_oob(FIT, 5.0) is False

    # Kontrolle: identisch, aber ext in-Band → Deadband 0,05 → Write 2,00.
    service2 = FakeMindererService(settings, readback_value="2.06", audit_path=settings.audit_jsonl_path)
    loop2 = _loop(
        settings,
        service=service2,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(ext=5.0, rl_eg_value=37.0),
        log_client=FakeLogClient(2.0),
    )
    await loop2.cycle_once()
    assert service2.write_calls and service2.write_calls[0]["volt"] == "2.00"
    assert loop2.status.last_decision["ext_oob"] is False


# ---------------------------------------------------------------------------
# 11. Rate-Limit
# ---------------------------------------------------------------------------
async def test_rate_limited_clean_exit(tmp_path: Path, no_uvr_wait: None) -> None:
    """CMISafetyError (Rate-Limit) beim Write → Zyklus endet sauber.

    Status rate-limited, kein Crash, KEIN zweiter Write-Versuch, kein
    Pending (Verifikation startet nur nach erfolgreichem Write).
    """
    settings = _settings(tmp_path)
    err = CMISafetyError("Rate-Limit 1×/h: letzter Minderer-Write vor 120 s")
    service = FakeMindererService(
        settings, readback_value="4.00", write_error=err, audit_path=settings.audit_jsonl_path
    )
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(ext=5.0),
        log_client=FakeLogClient(3.74),
    )
    await loop.cycle_once()  # darf nicht werfen
    assert len(service.write_calls) == 1  # genau EIN Versuch, kein Retry
    s = loop.status
    assert s.last_kill_reason == "rate-limited"
    # Rate-Limit = erwarteter Betriebszustand (kein Fehler) → ok=True,
    # Kill-Reason trägt die Semantik.
    assert s.last_cycle_ok is True
    assert s.last_decision["written"] is False
    assert s.last_decision["reason"] == "rate-limited"
    assert s.verification["state"] is None and s.verification["pending"] is False
    assert s.transient_giveup is False


# ---------------------------------------------------------------------------
# 12./13./14. Verifikation: ok / Transient+Resend / Give-up
# ---------------------------------------------------------------------------
async def test_verification_ok_and_audit(tmp_path: Path, no_uvr_wait: None) -> None:
    """Write + a18 folgt (im Fenster, ±0.05 V) → verified + Audit ok=true.

    v2: v_now=4,0, ΔT_ist=8,5 K → raw 3,40 → Klemme (0,60 ≤ 0,75) →
    Write 3,40 V.
    """
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)
    log_client = FakeLogClient(3.40)  # a18 zeigt 3.40, File-Ende ~60 s alt
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(ext=5.0),
        log_client=log_client,
    )
    await loop.cycle_once()
    s = loop.status
    v = s.verification
    assert v["state"] == "ok" and v["pending"] is False and v["last_a18"] == 3.40
    assert v["target_v"] is None and v["since_ts"] is None
    assert s.transient_giveup is False
    lines = _audit_lines(settings.audit_jsonl_path)
    assert len(lines) == 1
    rec = lines[0]
    assert rec["kind"] == "minderer_verify" and rec["ok"] is True
    assert rec["zone"] == "eg" and rec["address"] == "10144142091"
    assert rec["to"] == "3.40" and rec["a18"] == "3.4"
    assert rec["source"] == "loop" and rec["role"] == "prod"
    assert isinstance(rec["delta_s"], (int, float)) and rec["ts"]


async def test_transient_resend_then_ok(tmp_path: Path, no_uvr_wait: None) -> None:
    """Transient: 1. Zyklus Erstcheck fail (tries=1, KEIN Resend), 2. Zyklus
    Resend desselben Zielwerts (wertneutral → Rate-Limit-Exempt, tries=2),
    3. Zyklus a18 folgt (späte Anwendung) → ok.  tries zählt die PRÜFUNGEN
    (Task 2.1 P. 7: „1 Initial + 4 Resends“ bei 6 Prüfungen)."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)
    log_client = FakeLogClient(4.0)  # a18 bleibt bei altem 4.0 V (nicht angewendet)
    l2 = _fake_l2(ext=5.0)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=l2,
        log_client=log_client,
    )
    # Zyklus 1: Write 3.40 + Erstcheck fail → pending (tries=1, KEIN Resend).
    await loop.cycle_once()
    assert [w["volt"] for w in service.write_calls] == ["3.40"]
    assert loop.status.verification["state"] == "pending"
    assert loop.status.verification["tries"] == 1
    # v2: ΔT-Input am Soll (Regelkreis hat das Ziel erreicht) → Folge-
    # Entscheidungen sind no-change (sonst würde v2 wegen v_now-Abhängigkeit
    # ein NEUES Ziel pro Zyklus anstoßen; v1 war dank stabiler Kurve
    # immun).  Verifiziert wird hier die Transient-Verifikation, nicht
    # die Neuregelung.
    _force_dt_target(l2)
    # Zyklus 2: Resend desselben Zielwerts (tries=2).
    await loop.cycle_once()
    assert [w["volt"] for w in service.write_calls] == ["3.40", "3.40"]
    assert loop.status.last_write["resend"] is True
    assert loop.status.verification["tries"] == 2
    assert loop.status.verification["state"] == "pending"
    # Zyklus 3: a18 folgt (späte Anwendung) → ok.
    log_client.set_a18(3.40)
    await loop.cycle_once()
    assert len(service.write_calls) == 2  # kein weiterer Write
    v = loop.status.verification
    assert v["state"] == "ok" and v["pending"] is False and v["last_a18"] == 3.40
    lines = _audit_lines(settings.audit_jsonl_path)
    assert [line["kind"] for line in lines] == ["minderer_verify"]
    assert lines[0]["ok"] is True


async def test_giveup_after_n_checks(tmp_path: Path, no_uvr_wait: None) -> None:
    """a18 folgt nie → nach 6 Prüfungen: transient_giveup + Audit ok=false,
    kein weiteres Resend (Give-up-Prüfung zählt mit; 1 Initial + 4 Resends)."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)
    log_client = FakeLogClient(4.0)  # a18 bleibt 4.0 (nie angewendet)
    l2 = _fake_l2(ext=5.0)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=l2,
        log_client=log_client,
    )
    for i in range(1, 6):  # Zyklen 1–5
        await loop.cycle_once()
        s = loop.status
        if i == 1:
            assert s.verification["tries"] == 1 and s.verification["state"] == "pending"
            assert len(service.write_calls) == 1
            # v2: ΔT am Soll (Regelkreis hat das Ziel erreicht) → Folge-
            # Entscheidungen no-change, d.h. v_target = v_now = 3.40 =
            # Give-up-Wert (Task 2.1 P. 7: kein ewiges Transient-Looping).
            _force_dt_target(l2)
        else:
            assert s.verification["tries"] == i
            assert len(service.write_calls) == i  # Initial + (i−1) Resends
    # Zyklus 6: tries=6 ≥ 6 → Give-up (kein 6. Resend).
    await loop.cycle_once()
    s = loop.status
    assert s.transient_giveup is True
    assert s.verification["state"] == "giveup"
    assert s.verification["tries"] == 6
    assert len(service.write_calls) == 5
    lines = _audit_lines(settings.audit_jsonl_path)
    assert len(lines) == 1 and lines[0]["ok"] is False
    assert "transient" in (lines[0]["detail"] or "") and "6" in lines[0]["detail"]
    # Zyklus 7: weiter giveup, KEIN weiteres Resend + KEINE 2. Audit-Zeile
    # + KEIN neuer Write/Pending (v_target == Give-up-Wert, Task 2.1 P. 7).
    await loop.cycle_once()
    assert len(service.write_calls) == 5
    assert len(_audit_lines(settings.audit_jsonl_path)) == 1
    assert loop.status.transient_giveup is True
    assert loop.status.verification["state"] == "giveup"
    assert loop.status.last_kill_reason == "transient-giveup"
    assert loop.status.last_decision["written"] is False


# ---------------------------------------------------------------------------
# CMI-Read-back-Fehler + unvorhergesehene Fehler (Loop überlebt)
# ---------------------------------------------------------------------------
async def test_readback_unknown_value(tmp_path: Path, no_uvr_wait: None) -> None:
    """Read-back ohne lesbaren Wert (None) → cmi-readback, kein Write."""
    settings = _settings(tmp_path)
    service = FakeMindererService(
        settings, readback_value=None, audit_path=settings.audit_jsonl_path
    )
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(ext=5.0),
        log_client=FakeLogClient(3.74),
    )
    await loop.cycle_once()
    assert service.write_calls == []
    assert loop.status.last_kill_reason == "cmi-readback"


async def test_unexpected_error_kills_nothing(tmp_path: Path, no_uvr_wait: None) -> None:
    """Unvorhergesehener Fehler im Zyklus → Status.error, Task läuft weiter."""
    settings = _settings(tmp_path)
    service = FakeMindererService(settings, readback_value="4.00", audit_path=settings.audit_jsonl_path)

    def bad_l2(ts: datetime) -> MindererL2Data:
        raise RuntimeError("boom")

    loop = MindererLoop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2_builder=bad_l2,
        now_fn=time.time,
        log_client_factory=lambda: None,  # type: ignore[return-value]
    )
    await loop.cycle_once()
    assert loop.status.last_kill_reason == "error"
    assert "boom" in (loop.status.error or "")
    # Nächster Zyklus ist wieder sauber startbar (keine Exception).
    await loop.cycle_once()
    assert loop.status.last_kill_reason == "error"


# ---------------------------------------------------------------------------
# 17. Health-/Status-Shape (Gateway-Adapter)
# ---------------------------------------------------------------------------
def test_minderer_loop_status_shape(settings) -> None:
    """Gateway.minderer_loop_status() liefert alle dokumentierten Felder."""
    from app.main import Gateway

    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)
    gateway = Gateway(settings)
    service = FakeMindererService(settings, audit_path=settings.audit_jsonl_path)
    loop = _loop(
        settings,
        service=service,
        log_store=FakeLogStore(_room_points(30.0)),
        l2=_fake_l2(ext=5.0),
        log_client=FakeLogClient(4.0),
    )
    gateway.minderer_loop = loop
    asyncio.run(loop.cycle_once())  # Dummy-Zyklus (dev → inaktiv, 0 Verkehr)
    status = gateway.minderer_loop_status()
    for key in (
        "enabled",
        "role",
        "zone",
        "last_cycle_ts",
        "last_cycle_ok",
        "last_kill_reason",
        "last_decision",
        "last_write",
        "verification",
        "transient_giveup",
        "error",
    ):
        assert key in status, key
    for vkey in ("pending", "target_v", "since_ts", "tries", "last_a18", "last_check_ts", "state"):
        assert vkey in status["verification"], vkey
    assert status["enabled"] is False and status["role"] == "dev"
    assert status["last_kill_reason"] == "disabled"
    assert status["last_write"] is None


def test_health_route_includes_minderer_loop_block(settings, fake_transport) -> None:
    """/api/v1/health trägt den Block ``minderer_loop`` (analog cmi_log)."""

    from fastapi.testclient import TestClient

    from app.cmi.client import CMIClient
    from app.main import Gateway, create_app
    from app.state import StateStore

    settings.cmi_data_dir.mkdir(parents=True, exist_ok=True)
    client = CMIClient(
        base_url=settings.base_url,
        user=settings.cmi_user,
        password=settings.cmi_pass,
        transport=fake_transport,
    )
    gateway = Gateway(settings, client=client, store=StateStore(cmi_url=settings.base_url))
    app = create_app(settings)
    app.state.gateway = gateway
    with TestClient(app) as tc:
        r = tc.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert "minderer_loop" in body
    assert body["minderer_loop"]["zone"] == "eg"
    assert "verification" in body["minderer_loop"]
