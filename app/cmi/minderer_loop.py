"""Autonomer Minderer-Regelkreis Zone EG (v0.6-A' Phase 2 + v2, Gateway-intern).

Background-Loop neben dem CMI-/LOG-Poller (:mod:`app.main`
``_minderer_loop_poller``): regelt die EG-Minderer-Sollspannung (a18,
UVR610) autonom gegen das Ziel ``delta_t ≈ 10 K`` (L2-Entscheidungs-
Bausteine aus ``analysis.model.l2``), mit Envelope
``[minderer_loop_v_min, minderer_loop_v_max]`` (Default 2–7 V,
Drossel-Regime; Sättigungs-Flag ab ~10 V macht > 7 V unzulässig),
max. 1×/h via dem bestehenden Rate-Limit in
:class:`~app.cmi.minderer.MindererService` und **Ground-Truth-
Verifikation per CMI-/LOG a18** (nicht nur CMI-``OK``: Transient-
Fenster, 12:49-Beleg -- CMI bestätigt den Write, das UVR wendet ihn
zeitweise ~17 min nicht an).

**v2 (21.11, maintainer-Entscheidung): echter Regelkreis mit O8-
Durchfluss-Gate.**  „NORMAL/ABSENK bestimmt nur VL-Soll + ob die
Pumpe läuft. **Solange die Heizpumpe läuft, muss das ΔT passen --
Betriebsmodus ist egal.** O8-AUS-Zeiten (stehendes Wasser) darf die
Regelung NICHT betrachten.“

1. **O8-Durchfluss-Gate (fail-safe, ZUERST im Zyklus):** O8-ON-Anteil
   (``pump > 0,5``) im letzten 30 min des Roh-Fensters ≥ 0,5 → Gate
   offen; sonst KEIN CMI-Verkehr (nicht mal Read-back), Kill
   ``no-flow`` (kein nächtliches Drift-Jagen: das ΔT stehenden Wassers
   ist kein Durchfluss-ΔT).
2. **Steuer-ΔT nur aus O8-AN-Samples** (letztes 30-min-Fenster,
   lag-korrigiert nach dem
   ``rm_diagnostics._dt_raw_lag_corrected``-Muster: VL(t) vs
   RL(t+lag); ``lag_s`` aus der L2-Diagnose, Fallback
   ``l2_config.RM_DEFAULT_LAG_S``); < 10 gültige (VL,RL)-Paare →
   ``dt-unknown``, ``ΔT ≤ 0`` (Sensor-Transient) → ``dt-invalid``,
   kein Write.
3. **Stellgröße = Verhältnis-Korrektur + Schritt-Klemme** (ersetzt
   die v1-Envelope-Formel als Steuergröße): Drossel-Regime
   ``ΔT ≈ k/V`` → ``v_target = v_now · (ΔT_ist / ΔT_ziel)`` (Ziel
   10 K aus ``l2_config.RM_DELTA_T_TARGET_C``), Schritt-Klemme auf
   ± ``minderer_loop_max_step_v`` (Default 0,75 V, beide Richtungen →
   max. 0,75 V/h beim 1×/h-Rate-Limit = „sehr träge“), dann
   Envelope-Clip ``[2,0; 7,0] V`` + 2 Dezimalen.

**Abgrenzung V→ΔT-Kurve (Änderung gegenüber v1):** die Kurve
(``analysis.model.l2.minderer_curve``) wird NICHT mehr für die
Stellgröße verwendet -- sie bleibt Seed/Advisory für die L2-UI.
Folge: fehlende oder ``usable=False``-Kurve → **kein** ``no-curve``-
Kill mehr (Write erlaubt bei O8-ON + validem ΔT); nur ihr
Fit-Band (``ext_oob``) dient weiter der Deadband-Verdopplung (×2).

**Sicherheits-Grundprinzip (kanonisiert, maintainer):** Dev-Umgebung
schreibt NIEMALS in die CMI.  Der Loop entscheidet nur wenn

* (a) Kill-Switch ``settings.minderer_loop_enabled`` an (Default **False**)
  UND
* (b) ``normalize_role(settings.cmi_role) == "prod"`` (Pi = Master;
  Compose-``environment``, nie ``.env``).

Fail-safe-Default: aus (0 CMI-Verkehr bei Default-Settings).  Alle
Guards sind fail-safe = kein Write: Store-Staleness, O8-
Durchfluss-Gate (``no-flow``), ``dt``-Unknown/Invalid (``ΔT ≤ 0``),
PT1000-Drift, Raum-T-Staleness, Raum-T-Floor (nie weiter drosseln,
wenn der Raum unter ``minderer_loop_room_floor_c`` liegt -- Anheben
bleibt erlaubt), Deadband (OOB-Außen-T: verdoppelt).

Raum-T-Kanal-Entscheidung (Task 1.3, Beleg): ``winsol:2`` **Kanal 7**
(dev2 a7, „EG Temp“; Setup.xml-UI-Kanon ``ui/js/winsol-labels.js``:
Kanal 6 = „Erdgeschoss RAS 1“ = RAS-Offset/Stellgröße, Kanal 7 =
„Erdgeschoss Temp 1“).  Live-Beleg 18.11 (cmi_log_pq, 3-Tage-Fenster,
stunden-Ø): a7 = 20.9–22.3 °C (Tagesgang, 15–26-°C-Plausibilitätsband
Treffer) vs a6 = 19.0–20.4 °C (Nacht 20.0 > Tag 19.5 °C --
Stellgröße, kein Raum-Sensor).  Primär a7, wie gefordert.

Alle zyklus-internen Abhängigkeiten sind injizierbar
(``service`` / ``log_store`` / ``l2_builder`` / ``now_fn`` /
``log_client_factory``) → 100 % testbar mit Fakes (0 echte
CMI-/LOG-Aufrufe in den Tests).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import numpy as np

from analysis.model.l2 import config as l2_config
from analysis.model.l2 import minderer_curve, recommend
from analysis.model.l2 import rm_diagnostics as rm_diag
from app.cmi.cmi_log import CMILogClient, decode_cmi_log, fetch_log_index
from app.cmi.exceptions import (
    CMIAuthError,
    CMIConnectionError,
    CMIGatewayError,
    CmiLogError,
    CMISafetyError,
)
from app.cmi.minderer import MindererService, audit_append
from app.cmi.write import normalize_role
from app.config import Settings

logger = logging.getLogger("cmi.minderer-loop")

#: Zone des Regelkreises (Phase 2: NUR EG; kell/dg aus).
ZONE: Final[str] = "eg"

#: Minderer-EG-Sollspannung am UVR = dev2 a18 (json:32 ch18,
#: ``_CHANNEL_MAP_181``; rohe Adresse ``2000014414D10D`` = CMI
#: 10144142091).  /LOG-Zeilen: ``dev == "cmi-log:2"``,
#: ``str(channel) == "18"``.
LOG_DEVICE: Final[str] = "cmi-log:2"
LOG_A18_CHANNEL: Final[str] = "18"

#: Raum-T EG (Floor-Guard): ``cmi_log_pq.query("winsol:2", 7, "A", ...)``
#: (Kanal-Entscheidung + Beleg: Modul-Docstring oben).
ROOM_T_DEVICE: Final[str] = "winsol:2"
ROOM_T_CHANNEL: Final[int] = 7

#: PT1000-Drift-Guard: Strang-RL EG = dev2 a15 (PT1000 „HzRk Ventil 1“,
#: ``l2.config.RM_RAW_STRANG_RL["eg"]``) im 10-s-Roh-Fenster.
PT1000_MIN_SAMPLES: Final[int] = 30
PT1000_MAX_JUMP_K: Final[float] = 5.0

#: Frisch-Guards.
STORE_STALE_MAX_H: Final[float] = 1.5
ROOM_T_MAX_AGE_MIN: Final[float] = 30.0

#: Verifikation (UVR-Lag ~15 s + /LOG-Flush-Rand ~10-s-Batches).
UVR_APPLY_WAIT_S: Final[float] = 120.0
A18_WINDOW_S: Final[float] = 300.0
A18_TOL_V: Final[float] = 0.05

#: v2: O8-Durchfluss-Gate + Steuer-ΔT (letztes 30 min-Fenster des
#: Roh-Fensters; O8 = ``dev0_digital_08``, 0/1, in RawWindow als
#: ``pump``).  ON-Anteil ≥ 0,5 = Durchfluss (Gate offen); < 10
#: gültige (VL,RL)-Paare = ``dt-unknown`` (fail-safe).
O8_GATE_WINDOW_S: Final[float] = 1800.0
O8_ON_THRESHOLD: Final[float] = 0.5
DT_MIN_PAIRS: Final[int] = 10

_BERLIN: Final[ZoneInfo] = ZoneInfo(l2_config.TIMEZONE)


# ---------------------------------------------------------------------------
# L2-Lesen (Single Source: analysis/model/l2 -- hier nur LADEN)
# ---------------------------------------------------------------------------
@dataclass
class MindererL2Data:
    """Ein Lesezyklus der L2-Entscheidungs-Bausteine (blocking → Thread)."""

    state: Any  # recommend.State (``.rows``, ``.metric_gap_h``, ``.ts``)
    diag: Any  # rm_diag.RmDiagnostics (``.per_zone`` direkt, nicht to_dict())
    curve: dict[str, Any] | None  # Kurven-Artefakt (None = fehlt/ungültig)
    #: 10-s-Roh-Fenster (``raw.rl["eg"]`` = PT1000-Drift-Guard).
    raw_window: Any = None


def load_l2_data(ts: datetime, *, features_dir: str | Path) -> MindererL2Data:
    """State + ΔT-Diagnose + Kurven-Artefakt + Roh-Fenster laden.

    Blocking (DuckDB/Datei) → der Loop ruft es per :func:`asyncio.to_thread`
    auf.  ``diag`` ist das Objekt direkt (``.per_zone`` bleibt auch bei
    ``active=False`` gefüllt -- im Unterschied zu ``to_dict()``).
    """
    state = recommend.load_state(ts, features_dir=features_dir)
    lag_art = recommend.load_lag_estimate(features_dir=features_dir)
    raw = recommend.load_raw_window(features_dir=features_dir, ts=state.ts)
    diag = rm_diag.compute_diagnostics(state.rows, lag_art, raw)
    curve = recommend.load_minderer_curve(features_dir=features_dir)
    return MindererL2Data(state=state, diag=diag, curve=curve, raw_window=raw)


def _finite(v: Any) -> bool:
    return v is not None and isinstance(v, (int, float, np.floating)) and math.isfinite(float(v))


def ext_oob(z_fit: dict[str, Any], ext_c: float) -> bool:
    """Außen-T außerhalb des Fit-Kalibrierungsbands (OOB-Flag, kein Stopp)."""
    lo = z_fit.get("ext_min")
    hi = z_fit.get("ext_max")
    if not _finite(lo) or not _finite(hi):
        return False
    return ext_c < float(lo) or ext_c > float(hi)


def _o8_on_frac(win: Any) -> float | None:
    """v2: O8-ON-Anteil im letzten 30 min-Fenster des Roh-Fensters.

    ``None`` = unberechenbar (Fenster fehlt, < 30 min Daten oder
    Pumpen-Spalte fehlt/leer).  Basis: ``win.pump`` (``dev0_digital_08``,
    0/1) + ``win.ts``; ON = ``pump > 0,5``.  Fail-safe-Verbrauch im
    O8-Gate: nur ``frac ≥ O8_ON_THRESHOLD`` öffnet das Gate.
    """
    ts = getattr(win, "ts", None)
    pump = getattr(win, "pump", None)
    if ts is None or ts.size < 2 or pump is None or pump.size == 0:
        return None
    ts = np.asarray(ts)
    span = (ts[-1] - ts[0]) / np.timedelta64(1, "s")
    if span < O8_GATE_WINDOW_S:
        return None
    cutoff = ts[-1] - np.timedelta64(int(O8_GATE_WINDOW_S), "s")
    m = ts >= cutoff
    p = np.asarray(pump, dtype=np.float64)[m]
    p = p[np.isfinite(p)]
    if p.size == 0:
        return None
    return float(np.mean(p > O8_ON_THRESHOLD))


def _dt_pump_on(win: Any, zone: str, lag_s: int) -> float | None:
    """v2: Steuer-ΔT (Zone) = Ø ``VL(t) − RL_zone(t+lag)`` über O8-AN-
    Samples im letzten 30 min-Fenster (lag-korrigiert).

    Lag-Korrektur nach dem ``rm_diagnostics._dt_raw_lag_corrected``-
    Muster (VL führt, RL folgt: ``idx + k``, ``k = round(lag/dt_s)``):
    VL(t) wird mit dem RL(t+lag) desselben Durchfluss-Samples
    verglichen.  ``None`` bei < :data:`DT_MIN_PAIRS` gültigen
    (VL,RL)-Paaren (→ ``dt-unknown``).
    """
    ts = getattr(win, "ts", None)
    vl = getattr(win, "vl", None)
    pump = getattr(win, "pump", None)
    if ts is None or ts.size < 2 or vl is None or pump is None:
        return None
    ts = np.asarray(ts)
    span = (ts[-1] - ts[0]) / np.timedelta64(1, "s")
    if span < O8_GATE_WINDOW_S:
        return None
    rl = (getattr(win, "rl", None) or {}).get(zone)
    if rl is None:
        return None
    vl = np.asarray(vl, dtype=np.float64)
    rl = np.asarray(rl, dtype=np.float64)
    pump = np.asarray(pump, dtype=np.float64)
    if min(vl.size, rl.size, pump.size, ts.size) < DT_MIN_PAIRS:
        return None
    cutoff = ts[-1] - np.timedelta64(int(O8_GATE_WINDOW_S), "s")
    m = ts >= cutoff
    m &= (pump > O8_ON_THRESHOLD) & np.isfinite(vl) & np.isfinite(rl)
    # Abtastperiode: ``RawWindow.dt_s`` (Median-Abstand), Fallback = Median
    # der ts-Differenzen (Fakes ohne ``dt_s``-Property).
    dt_s = getattr(win, "dt_s", None)
    if not (_finite(dt_s) and float(dt_s) > 0):
        diffs = (np.diff(ts) / np.timedelta64(1, "s")).astype("float64")
        diffs = diffs[diffs > 0]
        dt_s = float(np.median(diffs)) if diffs.size else 10.0
    k = int(round(lag_s / dt_s))
    idx = np.where(m)[0]
    idx = idx[idx + k < rl.size]
    if idx.size < DT_MIN_PAIRS:
        return None
    pairs = vl[idx] - rl[idx + k]
    pairs = pairs[np.isfinite(pairs)]
    if pairs.size < DT_MIN_PAIRS:
        return None
    return float(np.mean(pairs))


def _ts_parseable(ts: str) -> bool:
    try:
        datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return False
    return True


def _ts_to_epoch(ts: str | None) -> float | None:
    """Naive-Berlin-ts → Epoch (die /LOG-Rows sind naive Berlin)."""
    if not ts or not _ts_parseable(ts):
        return None
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_BERLIN).timestamp()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
@dataclass
class _Pending:
    """Verifikations-Pending (letzter Write, noch nicht a18-verifiziert).

    ``tries`` = Anzahl der a18-PRÜFUNGEN (1 = Erstcheck im Write-Zyklus,
    +1 pro Folgezyklus-Check; ab Prüfung 2 fällt pro fehlgeschlagener
    Prüfung ein Resend desselben Zielwerts an -- wertneutral →
    Rate-Limit-Exempt).  Give-up nach ``minderer_loop_verify_giveup``
    PRÜFUNGEN (Default 6: 1 Erst- + 5 Folge-Prüfungen ≈ 1.5 h bei
    15-min-Takt, 4 Resends dazwischen -- s. Task 2.1 Punkt 7).
    ``giveup_audited``: die Give-up-Audit-Zeile (ok=false) wird nur
    einmal geschrieben.
    """

    v: float
    ts: float
    tries: int = 0
    giveup_audited: bool = False


def _default_verification() -> dict[str, Any]:
    return {
        "pending": False,
        "target_v": None,
        "since_ts": None,
        "tries": 0,
        "last_a18": None,
        "last_check_ts": None,
        "state": None,
    }


@dataclass
class MindererLoopStatus:
    """In-Memory-Status (``minderer_loop_status()`` für /health).

    Feldliste (dokumentiert, Health-Block ``minderer_loop``)::

        {"enabled": bool, "role": str, "zone": "eg",
         "last_cycle_ts": float|None, "last_cycle_ok": bool,
         "last_kill_reason": str|None
             ("disabled"|"role=dev"|"zone=<z>"|"store-stale"
              |"no-flow"|"dt-unknown"|"dt-invalid"|"ext-unknown"
              |"room-t-stale"|"sensor-drift"
              |"room-floor"|"no-change"|"rate-limited"|"cmi-readback"
              |"cmi-error"|"safety"|"transient-giveup"|"error"),
         "last_decision": {"dt_k", "ext_c", "v_now", "v_target",
                           "written": bool, "reason": str|None,
                           "ext_oob": bool,
                            "o8_on_frac": float|None},
         "last_write": {"ts", "v", "cmi_readback", "audit_ok",
                        "resend": bool}|None,
         "verification": {"pending": bool, "target_v", "since_ts",
                          "tries", "last_a18", "last_check_ts",
                          "state": "ok|pending|giveup"|None},
         "transient_giveup": bool, "error": str|None}
    """

    enabled: bool
    role: str
    zone: str
    last_cycle_ts: float | None = None
    last_cycle_ok: bool = False
    last_kill_reason: str | None = None
    last_decision: dict[str, Any] | None = None
    last_write: dict[str, Any] | None = None
    verification: dict[str, Any] = field(default_factory=_default_verification)
    transient_giveup: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "role": self.role,
            "zone": self.zone,
            "last_cycle_ts": self.last_cycle_ts,
            "last_cycle_ok": self.last_cycle_ok,
            "last_kill_reason": self.last_kill_reason,
            "last_decision": self.last_decision,
            "last_write": self.last_write,
            "verification": self.verification,
            "transient_giveup": self.transient_giveup,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------
class MindererLoop:
    """Ein Regelkreis-Zyklus: Gate → L2-Lesen → Guards → Write → a18-Verifikation.

    ``cycle_once()`` enthält die komplette Zyklus-Logik (``app/main.py``
    ist nur der dünne Adapter: ``cycle_once()`` + Intervall-Sleep).
    Jeder Exception-Blitz im Zyklus wird gefangen (``error``/
    ``kill_reason`` im Status), der Task läuft weiter.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        service: MindererService | None = None,
        log_store: Any | None = None,
        l2_builder: Callable[[datetime], MindererL2Data] | None = None,
        now_fn: Callable[[], float] | None = None,
        log_client_factory: Callable[[], CMILogClient] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.service = service if service is not None else MindererService(settings)
        #: cmi_log_pq-Store (``gateway.cmi_log_pq``) für den Raum-T-Floor-Guard;
        #: Fakes in Tests (0 echte Store-Lesungen).
        self.log_store = log_store
        self._l2_builder = l2_builder or self._l2_default_builder
        self._now = now_fn or time.time
        self._log_client_factory = log_client_factory or self._log_client_default
        self._log_client: CMILogClient | None = None
        self._pending: _Pending | None = None
        #: Give-up-Wert (Task 2.1 P. 7): solange v_target diesem Wert
        #: gleicht, wird KEIN neuer Pending gesetzt (kein ewiges
        #: Transient-Looping gegen ein UVR, das den Wert ignoriert).
        self._given_up_v: float | None = None
        self.status = MindererLoopStatus(
            enabled=False,
            role=normalize_role(settings.cmi_role),
            zone=settings.minderer_loop_zone,
        )

    # -- Lifecycle ----------------------------------------------------------
    def _log_client_default(self) -> CMILogClient:
        """Lazy CMILogClient (direkter /LOG-Zugriff; Test-Override-Punkt)."""
        return CMILogClient(
            self.settings.base_url,
            self.settings.cmi_user,
            self.settings.cmi_pass,
            timeout_sec=self.settings.cmi_timeout_sec,
        )

    async def aclose(self) -> None:
        """Den (ggf. angelegten) /LOG-Client schließen."""
        if self._log_client is not None:
            await self._log_client.aclose()
            self._log_client = None

    # -- Zyklus -------------------------------------------------------------
    async def cycle_once(self) -> None:
        """Ein Regelkreis-Zyklus (Gate → Guards → Write → Verifikation)."""
        s = self.status
        s.error = None
        s.last_kill_reason = None
        s.enabled = bool(self.settings.minderer_loop_enabled)
        s.role = normalize_role(self.settings.cmi_role)
        s.zone = self.settings.minderer_loop_zone

        # 1) Gate-Check (fail-safe, VOR jeglichem CMI-Verkehr).
        if not s.enabled:
            s.last_kill_reason = "disabled"
            self._cycle_done()
            return
        if s.role != "prod":
            s.last_kill_reason = f"role={s.role}"
            self._cycle_done()
            return
        if s.zone != ZONE:
            s.last_kill_reason = f"zone={s.zone} (nur {ZONE!r} erlaubt)"
            self._cycle_done()
            return

        try:
            # 2) Verifikation zuerst (Transient-Handling: a18 folgt ggf.
            #    spät; Resend desselben Zielwerts = wertneutral =
            #    Rate-Limit-Exempt).
            if self._pending is not None:
                await self._process_pending(allow_resend=True)
            # 3) Neue Entscheidung (nur wenn kein Pending anstand).
            if self._pending is None:
                await self._new_decision()
        except CMISafetyError as exc:
            s.error = f"safety: {exc}"
            s.last_kill_reason = "safety"
            logger.warning("minderer-loop: %s", exc)
        except CMIGatewayError as exc:
            s.error = f"cmi: {exc}"
            s.last_kill_reason = "cmi-error"
            logger.warning("minderer-loop: CMI-Fehler: %s", exc)
        except Exception as exc:  # noqa: BLE001 - ein Zyklus-Blitz töten den Loop nie
            s.error = f"unexpected: {exc}"
            s.last_kill_reason = "error"
            logger.exception("minderer-loop: unexpected error")
        self._cycle_done()

    def _cycle_done(self) -> None:
        s = self.status
        s.last_cycle_ts = self._now()
        s.last_cycle_ok = s.error is None

    # -- a18-Ground-Truth (direkt /LOG, NICHT der 5-min-cmi_log_pq-Store) ----
    def _get_log_client(self) -> CMILogClient:
        if self._log_client is None:
            self._log_client = self._log_client_factory()
        return self._log_client

    async def _read_log_a18(self) -> tuple[float | None, float | None, float | None]:
        """Letztes a18 aus dem aktiven /LOG-File:
        ``(value_v|None, ts_a18_epoch|None, data_max_ts_epoch|None)``.

        ``data_max_ts`` = höchster Zeilen-ts im File (Fenster-Basis;
        max(ts) hinkt ggf. Minuten hinterher).  ``(None,)*3`` = leeres
        /LOG (noch kein aktives File).  Transport-/Auth-/Decode-Fehler
        werfen (→ Verifikations-Fehler-Handling im Caller).
        """
        client = self._get_log_client()
        year = datetime.now(_BERLIN).year
        try:
            files = await fetch_log_index(client, year)
        except CMIAuthError:
            raise
        except (CmiLogError, CMIConnectionError) as exc:
            if getattr(exc, "http_status", None) != 404:
                raise
            files = []
        if not files:
            try:
                files = await fetch_log_index(client, year - 1)
            except CMIAuthError:
                raise
            except (CmiLogError, CMIConnectionError):
                files = []
        if not files:
            return None, None, None
        last = files[-1]
        data = decode_cmi_log(await client.get_bytes(last.path), filename=last.path)
        rows_a18 = [r for r in data.rows if r[1] == LOG_DEVICE and str(r[2]) == LOG_A18_CHANNEL]
        rows_ts = [r for r in data.rows if _ts_parseable(r[0])]
        if not rows_a18 or not rows_ts:
            return None, None, None
        last_a18 = max(rows_a18, key=lambda r: r[0])
        ts_max_row = max(rows_ts, key=lambda r: r[0])
        value = last_a18[4]
        return (
            float(value) if _finite(value) else None,
            _ts_to_epoch(last_a18[0]),
            _ts_to_epoch(ts_max_row[0]),
        )

    def _audit_verify(
        self,
        ok: bool,
        *,
        to: float,
        a18: float | None,
        delta_s: float,
        detail: str | None = None,
    ) -> None:
        """Audit-Zeile ``kind=minderer_verify`` (ok + zone/address/to/a18/delta_s/ts)."""
        audit_append(
            {
                "kind": "minderer_verify",
                "zone": self.settings.minderer_loop_zone,
                "address": self.service.address(self.settings.minderer_loop_zone),
                "from": None,
                "to": f"{to:.2f}",
                "a18": f"{a18:.1f}" if a18 is not None else None,
                "delta_s": round(delta_s, 0),
                "role": self.service.role(),
                "source": "loop",
                "ok": ok,
                "detail": detail,
            },
            path=self.service.audit_path,
        )

    async def _check_a18(self, *, allow_resend: bool) -> str:
        """Ein a18-Check (Erst- oder Wieder-Check): lesen + Pending-Status pflegen.

        Timing: ~120 s Warten (UVR-Lag ~15 s + /LOG-Flush-Rand) vor dem
        /LOG-Lesen.  Returns ``"ok"`` (angewendet, Pending klar) |
        ``"pending"`` (noch nicht angewendet; ab Prüfung 2 bei
        ``allow_resend`` wurde derselbe Zielwert re-gesendet --
        wertneutral → Rate-Limit-Exempt) | ``"giveup"`` (``tries ≥
        verify_giveup``: Transient-Give-up, Audit ``ok=false``).

        ``tries`` zählt die PRÜFUNGEN (Erstcheck = 1); der Resend gehört
        zum FOLLOW-up-Check (Task 2.1 Punkt 7: „1 Initial + 4 Resends“
        bei 6 Prüfungen).
        """
        pending = self._pending
        assert pending is not None
        s = self.status
        v = f"{pending.v:.2f}"
        await asyncio.sleep(UVR_APPLY_WAIT_S)
        # Fenster: letztes ts ≥ pending.ts − 5 min (max(ts)-Basis, s.o.).
        read_ok = True
        read_err: str | None = None
        try:
            value, ts_a18, ts_max = await self._read_log_a18()
        except Exception as exc:  # noqa: BLE001 - a18-Fehler killen den Zyklus nicht
            read_ok = False
            read_err = str(exc)
            value = ts_a18 = ts_max = None
            logger.warning("minderer-loop: a18-Read fehlgeschlagen: %s", exc)
        s.verification["last_a18"] = value
        s.verification["last_check_ts"] = self._now()
        if (
            read_ok
            and value is not None
            and ts_a18 is not None
            and ts_max is not None
            and ts_max >= pending.ts - A18_WINDOW_S
            and abs(value - pending.v) <= A18_TOL_V
        ):
            s.transient_giveup = False
            self._given_up_v = None
            s.verification.update(
                pending=False,
                target_v=None,
                since_ts=None,
                tries=pending.tries,
                state="ok",
            )
            self._pending = None
            self._audit_verify(True, to=pending.v, a18=value, delta_s=self._now() - pending.ts)
            logger.info("minderer-loop: a18 verifiziert bei %s V (Prüfung %d)", v, pending.tries)
            return "ok"
        # Nicht (noch) am UVR (Transient, 12:49-Fenster).
        pending.tries += 1
        giveup_n = self.settings.minderer_loop_verify_giveup
        if pending.tries >= giveup_n:
            s.transient_giveup = True
            s.verification.update(
                pending=False,
                target_v=pending.v,
                since_ts=pending.ts,
                tries=pending.tries,
                state="giveup",
            )
            if not pending.giveup_audited:
                pending.giveup_audited = True
                detail = (
                    f"transient: UVR hat a18 nach {pending.tries} Prüfungen nicht "
                    f"auf {v} V gebracht (12:49-Fenster-Analog)"
                    if read_ok
                    else f"a18 nicht lesbar nach {pending.tries} Prüfungen: {read_err}"
                )
                self._audit_verify(
                    False, to=pending.v, a18=value, delta_s=self._now() - pending.ts, detail=detail
                )
            logger.error(
                "minderer-loop: Transient-Give-up nach %d Prüfungen (a18 ≠ %s V)",
                pending.tries,
                v,
            )
            self._pending = None
            self._given_up_v = pending.v
            return "giveup"
        s.verification.update(
            pending=True,
            target_v=pending.v,
            since_ts=pending.ts,
            tries=pending.tries,
            state="pending",
        )
        if not allow_resend:
            return "pending"
        # Resend desselben Zielwerts (CMI-Read-back zeigt pending.v →
        # wertneutral → Rate-Limit ausgenommen).
        try:
            await self.service.write(
                self.settings.minderer_loop_zone,
                v,
                confirm=False,
                source="loop",
            )
            s.last_write = {
                "ts": self._now(),
                "v": pending.v,
                "cmi_readback": v,
                "audit_ok": True,
                "resend": True,
            }
            logger.info(
                "minderer-loop: Resend %s V (Prüfung %d/%d)", v, pending.tries, giveup_n
            )
        except CMISafetyError as exc:
            s.last_kill_reason = "rate-limited"
            s.error = f"resend rate-limited: {exc}"
            logger.warning("minderer-loop: Resend rate-limitiert: %s", exc)
        except CMIGatewayError as exc:
            s.last_kill_reason = "cmi-error"
            s.error = f"resend: {exc}"
            logger.warning("minderer-loop: Resend fehlgeschlagen: %s", exc)
        return "pending"

    async def _process_pending(self, *, allow_resend: bool) -> None:
        """Zyklus-Start: das offene Pending abarbeiten (s. :meth:`_check_a18`)."""
        await self._check_a18(allow_resend=allow_resend)

    # -- Neue Entscheidung ---------------------------------------------------
    def _l2_default_builder(self, ts: datetime) -> MindererL2Data:
        return load_l2_data(ts, features_dir=self.settings.l1_store_dir)

    async def _new_decision(self) -> None:
        """L2-Lesen (Thread) + O8-Gate + Guards + Verhältnis-Korrektur + Write.

        v2-Abfolge: store-stale → **O8-Durchfluss-Gate (fail-safe, VOR
        jeglichem CMI-Verkehr)** → CMI-Read-back → Raum-T-/Frisch-Guards
        → **PT1000-Drift-Guard (VOR der Steuer-ΔT: der Drift-Kanal eg-RL
        ist in v2 die ΔT-Quelle)** → Steuer-ΔT (O8-AN-Samples, 30 min,
        lag-korrigiert; < 10 Paare → ``dt-unknown``, ΔT ≤ 0 →
        ``dt-invalid``) → Verhältnis-Korrektur + Schritt-Klemme +
        Envelope-Clip → Guards (Transient-Give-up/Floor/Deadband) →
        Write → a18-Verifikation.
        """
        s = self.status
        now_dt = datetime.now(_BERLIN)
        # Blocking-Sections (DuckDB) per Thread; CMI-HTTP im Event-Loop.
        l2d = await asyncio.to_thread(self._l2_builder, now_dt)
        state, diag = l2d.state, l2d.diag

        if state.metric_gap_h > STORE_STALE_MAX_H:
            s.last_kill_reason = "store-stale"
            s.last_decision = self._decision(None, None, None, None, False, "store-stale")
            return

        # v2: O8-Durchfluss-Gate (fail-safe, ZUERST -- VOR CMI-Read-back):
        # O8-AUS-Zeit (stehendes Wasser) → 0 CMI-Verkehr (nicht mal
        # Read-back); nächtliches Drift-Jagen unmöglich (stehendes-Wasser-
        # ΔT ist kein Durchfluss-ΔT).  maintainer-Kanon: NORMAL/ABSENK ist
        # egal -- O8-ON = Regelzeit.
        o8_on_frac = _o8_on_frac(l2d.raw_window)
        if o8_on_frac is None or o8_on_frac < O8_ON_THRESHOLD:
            s.last_kill_reason = "no-flow"
            s.last_decision = self._decision(
                None, None, None, None, False, "no-flow", o8=o8_on_frac
            )
            logger.info(
                "minderer-loop: O8-Gate zu (ON-Anteil %s im 30-min-Fenster) → kein CMI-Verkehr",
                f"{o8_on_frac:.3f}" if o8_on_frac is not None else "n/b",
            )
            return

        # CMI-Read-back (Ist-Vorgabe, 2 Dezimalen).
        try:
            readback = await self.service.readback(self.settings.minderer_loop_zone)
        except CMIGatewayError as exc:
            s.last_kill_reason = "cmi-readback"
            s.error = f"readback: {exc}"
            s.last_decision = self._decision(
                None, None, None, None, False, "cmi-readback", o8=o8_on_frac
            )
            logger.warning("minderer-loop: Read-back fehlgeschlagen: %s", exc)
            return
        v_now = readback.current_value
        if not (isinstance(v_now, str) and _finite(float(v_now))):
            s.last_kill_reason = "cmi-readback"
            s.last_decision = self._decision(
                None, None, None, None, False, "cmi-readback", o8=o8_on_frac
            )
            return
        v_now_f = float(v_now)

        # Raum-T EG (cmi_log_pq, Frisch-Guard 30 min) → Floor-Guard-Basis.
        room_t, _room_ts = await self._read_room_t()
        if room_t is None:
            s.last_kill_reason = "room-t-stale"
            s.last_decision = self._decision(
                None, None, v_now_f, None, False, "room-t-stale", o8=o8_on_frac
            )
            return

        # PT1000-Drift-Guard (Roh-Fenster dev2 a15).  In v2 VOR der
        # Steuer-ΔT-Berechnung: der Drift-Kanal (eg-RL) IST in v2 die
        # ΔT-Quelle -- ein kaputter Sensor darf nie in die Regelung
        # fließen (fail-safe: drift-Kill vor dt-Kill).
        drift = self._pt1000_guard(l2d.raw_window)
        if drift is not None:
            s.last_kill_reason = "sensor-drift"
            s.last_decision = self._decision(
                None, None, None, None, False, "sensor-drift", o8=o8_on_frac
            )
            return

        # v2: Steuer-ΔT nur aus O8-AN-Samples (30-min-Fenster,
        # lag-korrigiert; ersetzt die v1-``delta_t_c``-Lesequelle).
        zdiag = diag.per_zone.get(ZONE) or {}
        lag_s = zdiag.get("lag_s")
        if not _finite(lag_s):
            lag_s = l2_config.RM_DEFAULT_LAG_S
        dt = _dt_pump_on(l2d.raw_window, ZONE, int(lag_s))
        if not _finite(dt):
            s.last_kill_reason = "dt-unknown"
            s.last_decision = self._decision(
                None, None, None, None, False, "dt-unknown", o8=o8_on_frac
            )
            return
        if float(dt) <= 0.0:
            s.last_kill_reason = "dt-invalid"
            s.last_decision = self._decision(
                dt, None, None, None, False, "dt-invalid", o8=o8_on_frac
            )
            logger.info(
                "minderer-loop: ΔT %.2f K ≤ 0 (RL ≥ VL, Sensor-Transient) → kein Write",
                float(dt),
            )
            return

        # v2: OOB-Band NUR aus dem (ggf. fehlenden/``usable=False``-)
        # Kurven-Fit-Band für die Deadband-Verdopplung -- v2: KEIN
        # ``no-curve``-Kill mehr (Kurve = Seed/Advisory, keine Steuergröße).
        z_fit = minderer_curve.zone_entry(l2d.curve, ZONE)

        ext = state.rows.get("ext")
        if not _finite(ext):
            s.last_kill_reason = "ext-unknown"
            s.last_decision = self._decision(
                dt, None, v_now_f, None, False, "ext-unknown", o8=o8_on_frac
            )
            return
        ext_f = float(ext)
        oob = ext_oob(z_fit, ext_f) if z_fit is not None else False
        deadband = (
            self.settings.minderer_loop_deadband_v * 2.0
            if oob
            else self.settings.minderer_loop_deadband_v
        )
        # v2: Verhältnis-Korrektur + Schritt-Klemme (ersetzt die
        # v1-Envelope-Formel als Stellgröße; Envelope-Clip + 2 Dezimalen
        # wie bisher).
        v_target = self._v_target(v_now_f, float(dt))

        # Task 2.1 P. 7: nach Transient-Give-up erst wieder neuer
        # Pending/Write, wenn v_target ≠ gegeben-aufener Wert (kein
        # ewiges Transient-Looping gegen ein UVR, das den Wert ignoriert;
        # steht VOR den übrigen Guards = spezifischster Kill-Grund).
        if self._given_up_v is not None and abs(v_target - self._given_up_v) < 0.005:
            s.last_kill_reason = "transient-giveup"
            s.last_decision = self._decision(
                dt, ext_f, v_now_f, v_target, False, "transient-giveup", oob=oob, o8=o8_on_frac
            )
            logger.info(
                "minderer-loop: v_target %s V = Give-up-Wert → kein neuer Pending",
                f"{v_target:.2f}",
            )
            return

        # Guards (alle fail-safe = kein Write; Reihenfolge nach Task 2.1:
        # Floor vor Deadband -- im Deadband-Fall wäre ohnehin kein Write).
        if room_t < self.settings.minderer_loop_room_floor_c and v_target < v_now_f:
            s.last_kill_reason = "room-floor"
            s.last_decision = self._decision(
                dt, ext_f, v_now_f, v_target, False, "room-floor", oob=oob, o8=o8_on_frac
            )
            return
        if abs(v_target - v_now_f) < deadband:
            s.last_kill_reason = "no-change"
            s.last_decision = self._decision(
                dt, ext_f, v_now_f, v_target, False, "no-change", oob=oob, o8=o8_on_frac
            )
            return

        # Alle Guards grün: Write (Rate-Limit läuft im service.write;
        # CMISafetyError → rate-limited, Zyklusende, KEIN Retry).
        target_2dp = f"{v_target:.2f}"
        try:
            await self.service.write(
                self.settings.minderer_loop_zone,
                target_2dp,
                confirm=False,
                source="loop",
            )
        except CMISafetyError as exc:
            s.last_kill_reason = "rate-limited"
            s.last_decision = self._decision(
                dt, ext_f, v_now_f, v_target, False, "rate-limited", o8=o8_on_frac
            )
            logger.info("minderer-loop: Rate-Limit (kein zweiter Versuch): %s", exc)
            return
        self._pending = _Pending(v=v_target, ts=self._now(), tries=0)
        s.transient_giveup = False
        s.last_write = {
            "ts": self._now(),
            "v": v_target,
            "cmi_readback": target_2dp,
            "audit_ok": True,
            "resend": False,
        }
        s.verification.update(
            pending=True,
            target_v=v_target,
            since_ts=self._pending.ts,
            tries=0,
            state="pending",
        )
        s.last_decision = self._decision(
            dt, ext_f, v_now_f, v_target, True, None, oob=oob, o8=o8_on_frac
        )
        logger.info(
            "minderer-loop: Write %s V (dt=%.2f K, ext=%.1f °C, v_now=%s, oob=%s, o8=%.2f)",
            target_2dp,
            float(dt),
            ext_f,
            v_now,
            oob,
            o8_on_frac,
        )
        # Erstcheck im selben Zyklus: ~120 s warten, dann a18 (kein
        # Resend -- der gehört dem NÄCHSTEN Zyklus, s. Task 2.1 Punkt 7).
        await self._check_a18(allow_resend=False)

    def _v_target(self, v_now: float, dt_ist: float) -> float:
        """v2-Stellgröße: Verhältnis-Korrektur + Schritt-Klemme + Envelope-Clip.

        Drossel-Regime ``ΔT ≈ k/V`` → ``v_target = v_now · (ΔT_ist /
        ΔT_ziel)`` (Ziel ``l2_config.RM_DELTA_T_TARGET_C`` = 10 K): ΔT zu
        groß → v_now senken (drosseln), ΔT zu klein → v_now erhöhen
        (öffnen).  Schritt-Klemme (fail-safe, beide Richtungen): maximal
        ``minderer_loop_max_step_v`` (Default 0,75 V) pro Zyklus → bei
        max. 1×/h (Rate-Limit) sehr träge (maintainer-Vorgabe).  Danach
        Envelope-Clip ``[v_min, v_max]`` (2,0–7,0 V) + 2 Dezimalen wie
        in v1.  Die v1-Envelope-Formel (Kurven-Inverse) ist NICHT mehr
        die Steuergröße (Kurve = Seed/Advisory, s. Modul-Doku).
        """
        tgt = l2_config.RM_DELTA_T_TARGET_C
        v_raw = v_now * (dt_ist / tgt)
        step = float(self.settings.minderer_loop_max_step_v)
        v = min(max(v_raw, v_now - step), v_now + step)
        v = min(max(v, float(self.settings.minderer_loop_v_min)), float(self.settings.minderer_loop_v_max))
        return round(v, 2)

    async def _read_room_t(self) -> tuple[float | None, float | None]:
        """Letzter Raum-T-EG-Wert aus ``cmi_log_pq`` (Kanal 7, „EG Temp“).

        Returns ``(value_c|None, ts_epoch|None)``; ``None`` = kein Wert
        oder letzter ts > 30 min alt → Guard ``room-t-stale``.
        """
        now = self._now()
        end = datetime.fromtimestamp(now, _BERLIN).strftime("%Y-%m-%d %H:%M:%S")
        start = datetime.fromtimestamp(now - ROOM_T_MAX_AGE_MIN * 60.0, _BERLIN).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        points = await asyncio.to_thread(
            self.log_store.query,
            ROOM_T_DEVICE,
            ROOM_T_CHANNEL,
            "A",
            start,
            end,
            agg="last",
            limit=5,
        )
        if not points:
            return None, None
        last = points[-1]
        value = last.get("value")
        ts = _ts_to_epoch(last.get("ts"))
        if not _finite(value):
            return None, None
        if ts is None or (now - ts) > ROOM_T_MAX_AGE_MIN * 60.0:
            return None, None
        return float(value), ts

    def _pt1000_guard(self, raw: Any) -> str | None:
        """PT1000-Drift-Guard auf ``raw.rl['eg']`` (dev2 a15).

        Fail-safe (kein Write): Roh-Fenster fehlt, < 30 endliche Samples
        ODER max-|Differenz| zwischen aufeinanderfolgenden endlichen
        Samples > 5 K → ``"sensor-drift"``; sonst ``None``.
        """
        if raw is None:
            return "sensor-drift"
        arr = (getattr(raw, "rl", None) or {}).get(ZONE)
        if arr is None:
            return "sensor-drift"
        fin = [float(v) for v in np.asarray(arr, dtype=np.float64) if np.isfinite(v)]
        if len(fin) < PT1000_MIN_SAMPLES:
            return "sensor-drift"
        for prev, cur in zip(fin, fin[1:], strict=False):
            if abs(cur - prev) > PT1000_MAX_JUMP_K:
                return "sensor-drift"
        return None

    @staticmethod
    def _decision(
        dt: Any,
        ext_c: Any,
        v_now: Any,
        v_target: Any,
        written: bool,
        reason: str | None,
        oob: bool = False,
        o8: float | None = None,
    ) -> dict[str, Any]:
        return {
            "dt_k": round(float(dt), 3) if _finite(dt) else None,
            "ext_c": round(float(ext_c), 2) if _finite(ext_c) else None,
            "v_now": v_now,
            "v_target": v_target,
            "written": written,
            "reason": reason,
            "ext_oob": oob,
            "o8_on_frac": round(float(o8), 4) if _finite(o8) else None,
        }


__all__ = [
    "A18_TOL_V",
    "DT_MIN_PAIRS",
    "LOG_A18_CHANNEL",
    "MindererL2Data",
    "MindererLoop",
    "MindererLoopStatus",
    "O8_GATE_WINDOW_S",
    "O8_ON_THRESHOLD",
    "ROOM_T_CHANNEL",
    "ROOM_T_DEVICE",
    "UVR_APPLY_WAIT_S",
    "ZONE",
    "ext_oob",
    "load_l2_data",
]
