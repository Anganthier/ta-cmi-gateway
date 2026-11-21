"""S3-1 two-phase change route: ``GET`` preview + ``POST`` execution.

Safety-critical (``docs/cmi-write-pfad.md`` + ``docs/l2-ui-panel.md``).  The
route is built on :class:`app.cmi.write.CMIWriteClient` (the ONLY place the
gateway may issue CMI writes):

* ``GET /api/v1/change?adr=<adr>&to=<value>``  -- **Preview** (Read-Token):
  resolves changeadr + options + current value (CMI read-only calls only),
  validates ``to`` against the live option list, checks RISK/whitelist
  (403), and returns a signed **confirmation hash**
  (HMAC-SHA256: key = Write-Token, message = ``adr \x00 to \x00 15-min
  window-id``; the Write-Token is never transmitted to the client).  The
  Preview performs NO CMI write.
* ``POST /api/v1/change`` with body ``{adr, to, confirmation_hash}``
  (exactly these 3 fields -- the browser never holds the Write-Token) --
  **Execution** (Write-Token via Bearer; default "" = route disabled ->
  503).  The hash is verified against the CURRENT 15-minute window
  (``expires_in_sec`` = 900 as upper bound; it is valid until the 15-min
  slot in which it was issued rolls over -- idempotent inside that window:
  the same hash may be accepted several times, documented).  RISK is
  re-checked,
  the previous value is read for the audit ``from`` field, then
  :meth:`CMIWriteClient.write` runs the actual change with read-back
  verification.  Every attempt -- success AND failure -- is appended to the
  audit JSONL (``settings.audit_jsonl_path``).

Read-back mismatch -> ``applied=false`` + HTTP 502 (audit is written anyway).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.routes import require_bearer
from app.cmi.exceptions import CMIAuthError, CMIConnectionError, CMIGatewayError, CMISafetyError
from app.cmi.queue import CMISerialQueue
from app.cmi.write import (
    DEFAULT_READBACK_DELAY_SEC,
    RISK_ADDRESSES,
    WRITABLE_ADDRESSES,
    CMIWriteClient,
    Readback,
)
from app.config import Settings

__all__ = [
    "CHANGE_CONFIRM_TTL_SEC",
    "ChangeRequest",
    "ChangeService",
    "change_confirm_hash",
    "confirm_window",
    "require_write_bearer",
    "router",
]

logger = logging.getLogger("cmi.change")

#: Confirmation-hash validity window (15 min = design: ts-fenster-15min).
CHANGE_CONFIRM_TTL_SEC: int = 15 * 60

_TZ = ZoneInfo("Europe/Berlin")

router = APIRouter()


def create_change_app(settings: Settings) -> FastAPI:
    """Minimal app hosting ONLY the two-phase change route (tests / focus).

    The full gateway app (pollers, state, L2) is built by
    :func:`app.main.create_app`; this factory wires just ``settings`` +
    ``ChangeService`` on ``app.state`` so the change routes can be exercised
    without the running gateway.  Returns the FastAPI app.
    """
    app = FastAPI()
    app.state.settings = settings
    app.state.change = ChangeService(settings)
    app.include_router(router)
    return app


def confirm_window(now_epoch: float) -> int:
    """15-Minuten-Fenster-ID eines Zeitstempels: ``int(epoch // 900)`` (UTC).

    Der Hash bindet genau diese Fenster-ID (Design: ``ts-fenster-15min``).
    Ein Preview-Hash ist gültig, solange die Prüfung im GLEICHEN 15-min-Fenster
    wie die Erteilung liegt (idempotent innerhalb des Fensters, dokumentiert);
    nach dem Fensterwechsel ist er ungültig und muss neu previewsen werden.
    """
    return int(now_epoch // CHANGE_CONFIRM_TTL_SEC)


def change_confirm_hash(
    write_token: str,
    adr: str,
    to: str,
    now_epoch: float,
) -> str:
    """Confirmation-Hash: HMAC-SHA256 (server-seitig, Write-Token im Key).

    ``HMAC_SHA256(key=write_token, msg=adr \x00 to \x00 window(now))``
    (Design-Formel ``sha256(token + adr + to + ts-fenster-15min)`` als HMAC;
    der Client kennt den Write-Token NICHT -- der POST sendet nur den Hash,
    die Route validiert gegen den Server-Token) -- client-unberechenbar.
    """
    key = write_token.encode()
    msg = f"{adr}\x00{to}\x00{confirm_window(now_epoch)}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


class ChangeRequest(BaseModel):
    """POST /api/v1/change body (Two-Phase-Phase 2, exakt 3 Felder).

    ``confirmation_hash`` kommt aus dem GET-Preview.  Die Frische läuft
    server-seitig: der Hash bindet das 15-Minuten-Fenster der ERTEILUNG
    (``int(issued_epoch // 900)``) und wird beim POST gegen das
    AKTUELLE 15-Minuten-Fenster geprüft -- ein Wechsel des Fensters
    (max. ~15 min nach dem Preview) macht den Hash ungültig (403 "abgelaufen,
    neu previewsen").  Innerhalb desselben Fensters ist derselbe Hash
    idempotent (mehrere POSTs erlaubt -- dokumentiert).  Der Browser sendet
    NUR diese 3 Felder; der Write-Token bleibt server-seitig.
    """

    adr: str = Field(description="Display-Adresse (L1-Whitelist), z. B. 10820A10180")
    to: str = Field(description="Zielwert (value-Attribut der Option, nicht das Label)")
    confirmation_hash: str = Field(description="Hash aus dem GET-Preview")


class ChangeService:
    """Wires settings + CMI write client + audit JSONL for the change route.

    Created once per app instance (``app.state.change``); ``aclose()`` closes
    the CMI http client.  The write client uses its OWN serial queue
    (single-flight against the CMI), so writes can never interleave with the
    running gateway pollers.

    The write client runs with ``strict=False``: a read-back MISMATCH does
    not raise but is reported as ``applied=False`` -> the route answers 502
    and still writes the audit line (design: Mismatch = visible, audited, not
    a hard 403).  All OTHER safety gates (whitelist, RISK, value sanity) stay
    hard inside ``CMIWriteClient`` and raise before any CMI call.

    Testability: a ``transport`` (``httpx.AsyncBaseTransport``, e.g.
    ``MockTransport``) and/or a ``readback_delay_sec`` can be injected before
    the first client use; ``now_fn`` overrides the clock for the confirmation
    window (tests simulate window rollover without sleeping).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: Any | None = None,
        readback_delay_sec: float | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings
        self.queue = CMISerialQueue()
        self._client: CMIWriteClient | None = None
        self._transport = transport
        self._readback_delay_sec = readback_delay_sec
        self._now_fn = now_fn or time.time

    @property
    def write_token(self) -> str:
        """The configured write token (empty = write route disabled)."""
        return self.settings.gateway_write_token

    @property
    def enabled(self) -> bool:
        return bool(self.settings.gateway_write_token)

    def now_epoch(self) -> float:
        """Current epoch seconds (injectable clock; default ``time.time``)."""
        return self._now_fn()

    def _client_instance(self) -> CMIWriteClient:
        if self._client is None:
            self._client = CMIWriteClient(
                self.settings.base_url,
                self.settings.cmi_user,
                self.settings.cmi_pass,
                queue=self.queue,
                # strict=False: Read-back-Mismatch ist KEIN CMISafetyError,
                # sondern result.applied=False -> Route antwortet 502 +
                # Audit-Zeile (DoD: Mismatch -> applied=false + 502).
                strict=False,
                timeout_sec=self.settings.cmi_timeout_sec,
                readback_delay_sec=(
                    self._readback_delay_sec
                    if self._readback_delay_sec is not None
                    else DEFAULT_READBACK_DELAY_SEC
                ),
                transport=self._transport,
                # v0.6-A' role gate: die Rolle kommt aus den DIESER Route
                # gehörenden Settings (CMI_ROLE, Default dev) -- nicht aus
                # dem globalen Settings-Cache, damit ein injizierter Role
                # (z. B. prod in Tests) die Write-Gate-Entscheidung trifft.
                role=self.settings.cmi_role,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- validation ------------------------------------------------------------
    @staticmethod
    def check_access(adr: str) -> None:
        """Whitelist + RISK gate (both phases).  Raises 403 on violation."""
        if adr not in WRITABLE_ADDRESSES:
            raise HTTPException(
                status_code=403,
                detail=f"Adresse {adr!r} ist nicht in der L1-Write-Whitelist",
            )
        if adr in RISK_ADDRESSES:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Adresse {adr!r} ist RISK (Oelbrenner) und darf NIE geschrieben werden "
                    "(auch nicht als No-Op)"
                ),
            )

    @staticmethod
    def validate_target(info: Readback, to: str) -> None:
        """``to`` must be one of the address's own option values / in [min,max]."""
        value = str(to)
        if not value:
            raise HTTPException(status_code=422, detail="to: leerer Wert")
        if info.option_type == "select":
            allowed = list(info.option_values)
            if value not in allowed:
                raise HTTPException(
                    status_code=422,
                    detail=f"to {value!r} ist keine erlaubte Option (erlaubt: {allowed})",
                )
        elif info.option_type == "bit":
            if value not in ("0", "1"):
                raise HTTPException(status_code=422, detail=f"to {value!r} ist kein Bit-Wert (0/1)")
        elif info.option_type == "numeric":
            try:
                number = float(value.replace(",", "."))
            except ValueError:
                raise HTTPException(status_code=422, detail=f"to {value!r} ist keine Zahl") from None
            lo = _num(info.min_value)
            hi = _num(info.max_value)
            if (lo is not None and number < lo) or (hi is not None and number > hi):
                raise HTTPException(
                    status_code=422,
                    detail=f"to {value!r} außerhalb von [{info.min_value}, {info.max_value}]",
                )
        else:
            raise HTTPException(
                status_code=422,
                detail=f"option_type {info.option_type!r} ist nicht per CMI setzbar",
            )

    def verify_hash(self, adr: str, to: str, confirmation_hash: str) -> None:
        """Confirmation-Hash gegen das AKTUELLE 15-Min-Fenster prüfen (403).

        Der Hash (aus dem Preview) bindet das 15-Min-Fenster der Erteilung
        (``int(issued_epoch // 900)``).  Die Validierung läuft gegen das
        AKTUELLE 15-Min-Fenster (``now_epoch``):

        * selbes Fenster (max. ~15 min nach dem Preview): Hash passt, POST
          erlaubt -- innerhalb des Fensters ist derselbe Hash idempotent
          (mehrere POSTs möglich, dokumentiert);
        * Fenster gewechselt (oder adr/to/Token nicht passend): Hash passt
          NICHT -> 403 "abgelaufen/ungültig -- neu previewsen".  Die
          Abgrenzung Expired-vs-Invalid ist aus dem 3-Felder-Body
          (``{adr, to, confirmation_hash}``) nicht herleitbar und wird daher
          in EINEM 403 gemündet (DoD: Hash-Fehler = 403).

        Tests: ``now_fn``-Injektion schiebt die Uhr über einen
        Fenster-Grenze (``epoch // 900`` wechselt) -> Hash wird ungültig.
        """
        expected = change_confirm_hash(self.write_token, adr, to, self.now_epoch())
        if not hmac.compare_digest(expected, str(confirmation_hash).strip().lower()):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Confirmation-Hash ungültig oder abgelaufen "
                    "(15-Min-Fenster, adr/to/Token) -- neu previewsen"
                ),
            )

    # -- audit -----------------------------------------------------------------
    def audit(
        self,
        *,
        adr: str,
        label: str | None,
        from_value: str | None,
        to: str | None,
        readback: str | None,
        applied: bool | None,
        http_status: int,
    ) -> str:
        """Append one audit line (JSONL); returns the audit id (best-effort).

        A failed audit write is logged but NEVER masks the HTTP result.
        """
        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "adr": adr,
            "label": label,
            "from": from_value,
            "to": to,
            "user": "write-token",
            "readback": readback,
            "applied": applied,
            "http_status": http_status,
        }
        audit_id = f"{record['ts']}:{adr}:{(to or '')[:16]}"
        path: Path = self.settings.audit_jsonl_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.error("Audit-Write nach %s fehlgeschlagen: %s", path, exc)
        return audit_id


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def require_write_bearer(request: Request) -> None:
    """Bearer dependency for POST /api/v1/change (WRITE token only).

    * write token not configured (default "") -> 503 "nicht konfiguriert";
    * missing / invalid / READ token -> 403 (a read token must never open the
      write path -- S3-2 DoD: Read -> kein Write).
    """
    settings: Settings = request.app.state.settings
    write_token = settings.gateway_write_token
    if not write_token:
        raise HTTPException(
            status_code=503,
            detail="Write-Token nicht konfiguriert (GATEWAY_WRITE_TOKEN) -- Write-Route deaktiviert",
        )
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(
            status_code=403,
            detail="Write-Route erfordert Bearer-Write-Token",
            headers={"WWW-Authenticate": 'Bearer realm="cmi-gateway-write"'},
        )
    token = header.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, write_token):
        raise HTTPException(
            status_code=403,
            detail="Ungültiger Write-Token (Read-Token öffnet die Write-Route NIE)",
            headers={"WWW-Authenticate": 'Bearer realm="cmi-gateway-write"'},
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get(
    "/api/v1/change",
    summary="S3-1-Preview: Change-Plan + Confirmation-Hash (KEIN CMI-Write)",
    dependencies=[Depends(require_bearer)],
)
async def change_preview(
    request: Request,
    adr: Annotated[str, Query(description="Display-Adresse (L1-Whitelist)")],
    to: Annotated[str, Query(description="Zielwert (Option-Value, nicht Label)")],
) -> JSONResponse:
    """Two-Phase-Phase 1: resolve + validate + sign.  NO CMI write.

    Response::

        {"adr", "label", "current", "target", "changeadr", "option_values",
         "option_labels", "option_type", "risk", "confirmation_hash",
         "expires_in_sec", "issued_at"}

    403: nicht-Whitelist-Adresse / RISK-Adresse.
    422: ``to`` nicht in der Optionsliste / außerhalb [min,max].
    503: CMI nicht erreichbar (Resolver-Call fehlgeschlagen).
    """
    service: ChangeService = request.app.state.change
    ChangeService.check_access(adr)  # 403 before any CMI call
    try:
        info = await service._client_instance().read_current(adr)
    except CMISafetyError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (CMIConnectionError, CMIAuthError, CMIGatewayError, TimeoutError) as exc:
        raise HTTPException(
            status_code=503, detail=f"CMI für Preview nicht erreichbar: {exc}"
        ) from exc
    ChangeService.validate_target(info, to)

    issued_at = service.now_epoch()
    confirmation_hash = change_confirm_hash(service.write_token, adr, to, issued_at)
    labels = dict(zip(info.option_values, info.option_labels, strict=False))
    return JSONResponse(
        content={
            "adr": adr,
            "label": _label_for(adr, info),
            "current": {"value": info.current_value, "label": info.current_label},
            "target": {"value": to, "label": labels.get(to)},
            "changeadr": info.change_address,
            "option_values": list(info.option_values),
            "option_labels": list(info.option_labels),
            "option_type": info.option_type,
            "risk": adr in RISK_ADDRESSES,
            "confirmation_hash": confirmation_hash,
            "expires_in_sec": CHANGE_CONFIRM_TTL_SEC,
            "issued_at": datetime.fromtimestamp(issued_at, _TZ).isoformat(timespec="seconds"),
        }
    )


@router.post(
    "/api/v1/change",
    summary="S3-1-Execution: CMI-Write + Read-back + Audit (Two-Phase-Phase 2)",
    dependencies=[Depends(require_write_bearer)],
)
async def change_execute(request: Request, body: Annotated[ChangeRequest, Body()]) -> JSONResponse:
    """Two-Phase-Phase 2: verify hash, write, read-back, audit.

    Response::

        {"applied", "adr", "from", "to", "readback", "audit_id", "duration_sec"}

    403: Hash ungültig/abgelaufen, RISK/Whitelist-Verletzung.
    502: Read-back-Mismatch (``applied=false``, Audit trotzdem).
    503: CMI nicht erreichbar / Write-Token nicht konfiguriert (Auth).
    """
    service: ChangeService = request.app.state.change
    adr, to, confirmation_hash = body.adr, body.to, body.confirmation_hash

    service.check_access(adr)  # whitelist + RISK (403, auch vor Hash-Check)
    service.verify_hash(adr, to, confirmation_hash)

    client = service._client_instance()
    # Vorher-Wert für das Audit-Feld "from" (Read-only-Read; Fehlschlag
    # maskiert den Write NICHT -- then from=None).
    from_value: str | None = None
    try:
        before = await client.read_current(adr)
        from_value = before.current_value
    except Exception:  # noqa: BLE001 - audit-only, write proceeds regardless
        from_value = None
    try:
        result = await client.write(adr, to)
    except CMISafetyError as exc:
        audit_id = service.audit(
            adr=adr, label=None, from_value=from_value, to=to,
            readback=None, applied=False, http_status=403,
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (CMIConnectionError, CMIAuthError, CMIGatewayError, TimeoutError) as exc:
        audit_id = service.audit(
            adr=adr, label=None, from_value=from_value, to=to,
            readback=None, applied=None, http_status=503,
        )
        raise HTTPException(
            status_code=503, detail=f"CMI-Write fehlgeschlagen (CMI): {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - unexpected -> audited 500
        audit_id = service.audit(
            adr=adr, label=None, from_value=from_value, to=to,
            readback=None, applied=None, http_status=500,
        )
        raise HTTPException(status_code=500, detail=f"Unerwarteter Fehler beim CMI-Write: {exc}") from exc

    readback_value = result.after.current_value
    audit_id = service.audit(
        adr=adr,
        label=_label_for(adr, result.after),
        from_value=from_value,
        to=to,
        readback=readback_value,
        applied=result.applied,
        http_status=200 if result.applied else 502,
    )
    body_out = {
        "applied": result.applied,
        "adr": adr,
        "from": from_value,
        "to": to,
        "readback": {"value": readback_value, "label": result.after.current_label},
        "audit_id": audit_id,
        "duration_sec": result.duration_sec,
    }
    if not result.applied:
        return JSONResponse(
            status_code=502,
            content={
                **body_out,
                "error": (
                    f"Read-back-Mismatch: erwartet {to!r}, gelesen {readback_value!r} -- "
                    "Wert NICHT bestätigt (Audit-ID: " + audit_id + ")"
                ),
            },
        )
    return JSONResponse(status_code=200, content=body_out)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _num(token: str | None) -> float | None:
    if not token:
        return None
    try:
        return float(token.replace(",", "."))
    except ValueError:
        return None


def _label_for(adr: str, info: Readback) -> str:
    """Human label: option label of the current value, fallback: option list."""
    if info.current_label:
        return info.current_label
    for _value, label in zip(info.option_values, info.option_labels, strict=False):
        if label:
            return label
    return adr
