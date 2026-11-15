"""L2 recommendation route: ``GET /api/v1/recommend`` (S4N-UI-KI).

Read-token protected (same :func:`app.api.routes.require_bearer` as the other
read routes).  The payload is exactly the L2 v0.1 JSON structure
(``docs/ai-model-design.md`` §3.2) plus ``generated_at``:

* **Fresh**: HTTP 200 + the engine's JSON (``stale`` may be true inside --
  that is the engine's own stale state, design §3.2 rule R5: stale is data,
  not an error).
* **Engine failure** (store missing/unreadable, broken artifact, crash):
  HTTP 503 + JSON error body ``{"error": ..., "stale": false}`` (no bare
  crash-500, no HTML error page).

Caching + engine wiring live in :class:`app.recommendation.RecommendationService`
(one run per full hour, in-memory TTL >= 5 min, ``force`` = fresh run).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.recommendation import RecommendationError, RecommendationService

__all__ = ["router"]

router = APIRouter()


def _read_dep(request: Request) -> str:
    """Read-token dependency (deferred import: no circular with routes.py)."""
    from app.api.routes import require_bearer

    return require_bearer(request)


@router.get(
    "/api/v1/recommend",
    summary="L2-Empfehlungen (frischer Run, Ergebnis-Cache, Stale=200)",
    dependencies=[Depends(_read_dep)],
)
async def recommend_route(
    request: Request,
    force: Annotated[
        bool,
        Query(description="true = Zwangs-Neulauf (UI 'Jetzt aktualisieren')"),
    ] = False,
) -> JSONResponse:
    """Frische L2-v0.1-Empfehlungen (Feature-Store + LIVE-Wetter + v003-Rollout).

    Response = exakt die L2-JSON-Struktur + ``generated_at``:
    ``{ts, model_version, horizon_h, stale, stale_reasons, empfehlungen[],
    kosten, guardrail_hits, konfidenz, szenarien, kontext, generated_at}``.
    """
    service: RecommendationService = request.app.state.recommendation
    try:
        payload = await service.get(force=force)
    except RecommendationError as exc:
        return JSONResponse(status_code=503, content={"error": str(exc), "stale": False})
    return JSONResponse(status_code=200, content=payload)
