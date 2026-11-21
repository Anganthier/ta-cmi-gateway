"""Async HTTP client for the CMI web server (read-only operations).

All credentials come from the environment (``CMI_BASE_URL`` / ``CMI_USER`` /
``CMI_PASS``).  Only read-only endpoints are exposed here:

* ``/schematic_files/N.cgi``  -- live schematic values (~0.7 s, unlimited)
* ``/INCLUDE/api.cgi``        -- JSON node data (rate limited to 1/min, status 4)
* ``/INCLUDE/changer.cgi`` / ``/INCLUDE/changerx2.cgi`` -- display address
  resolution (read-only)

Write endpoints (``change.cgi``) are deliberately **not** part of this client
(Sprint S1 is strictly read-only).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Final

import httpx

from app.cmi.exceptions import CMIAuthError, CMIConnectionError, CMIGatewayError
from app.config import Settings, get_settings

logger = logging.getLogger("cmi.client")

#: Read-only endpoints this client may use (enforced by :meth:`CMIClient._assert_allowed`).
ALLOWED_READ_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/INCLUDE/api.cgi",
        "/INCLUDE/changer.cgi",
        "/INCLUDE/changerx2.cgi",
    }
)

#: Schematic value pages are read-only and rate-unlimited; allow only well-formed
#: ``/schematic_files/<page>.cgi`` (rejects traversal / arbitrary files).
_SCHEMATIC_PATH_RE: Final[re.Pattern[str]] = re.compile(r"^/schematic_files/\d+\.cgi$")

#: The CMI's own internal logging store (S4N-LogPoll).  ``/LOG/*`` is
#: read-only, rate-unlimited and **not** part of the 1/min JSON-API queue: it
#: serves the day-index (``/LOG/info<JJJJ>.log``), the binary data files
#: (``/LOG/<JJJJ>/data_*.log``) and the buffer-clear (``/LOG/clear.log``).
#: ``clear.log`` is the only state-mutating call (it empties the storekrit
#: buffer) but it is *read-fetch-then-clear* -- the gateway only clears after
#: it has ingested every listed file, so it is treated as a read for this
#: allow-list (see :mod:`app.cmi.cmi_log` + ``docs/cmi-log-protocol.md``).
_LOG_PATH_RE: Final[re.Pattern[str]] = re.compile(r"^/LOG/.+$")


class CMIClient:
    """Reusable async HTTP client for read-only CMI endpoints.

    Connection reuse is provided by the underlying :class:`httpx.AsyncClient`
    (keep-alive).  The client is safe for concurrent use.
    """

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        timeout_sec: float = 15.0,
        allowed_paths: frozenset[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Create the client.

        ``headers`` optionally adds client-level default headers to every
        request (e.g. the browser signature the write client sends); the
        default ``None`` leaves the wire behaviour exactly as before.
        """
        self._base_url = base_url.rstrip("/")
        self._auth = httpx.BasicAuth(username=user, password=password)
        self._timeout = httpx.Timeout(timeout_sec, connect=min(5.0, timeout_sec))
        self._allowed_paths: frozenset[str] = allowed_paths if allowed_paths is not None else ALLOWED_READ_PATHS
        client_kwargs: dict = {
            "base_url": self._base_url,
            "auth": self._auth,
            "timeout": self._timeout,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        if headers is not None:
            client_kwargs["headers"] = dict(headers)
        self._client = httpx.AsyncClient(**client_kwargs)

    # -- lifecycle ------------------------------------------------------------
    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> CMIClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- requests -------------------------------------------------------------
    async def get_schematic(self, page: int) -> str:
        """Fetch a schematic value page (``/schematic_files/{page}.cgi``).

        The schematic CGI is read-only and rate-unlimited on the CMI.
        """
        return await self._get_raw(f"/schematic_files/{page}.cgi")

    async def get_api(self, params: dict[str, str]) -> str:
        """Fetch ``/INCLUDE/api.cgi`` with the given query parameters.

        The query string is built **without percent-encoding**: the CMI CGI
        rejects ``%2C``-encoded commas in ``jsonparam`` with SYNTAX ERROR
        (code 3) -- verified live 2026-10-27.  The parameter values used by
        the gateway contain no characters that need escaping.

        .. note::
            The CMI allows at most one api.cgi request per minute.  Callers
            must go through :class:`~app.cmi.queue.ApiRateLimiter` so that the
            minimum interval is enforced.
        """
        raw_query = "&".join(f"{k}={v}" for k, v in params.items())
        return await self._get_raw(f"/INCLUDE/api.cgi?{raw_query}")

    async def get_changer(self, display_address: str, *, x2: bool = False) -> str:
        """Fetch a changer page for a display address.

        * ``x2=False`` -> ``/INCLUDE/changer.cgi?sadr=<addr>``
        * ``x2=True``  -> ``/INCLUDE/changerx2.cgi?sadrx2=<addr>``
        """
        if x2:
            return await self._get("/INCLUDE/changerx2.cgi", {"sadrx2": display_address})
        return await self._get("/INCLUDE/changer.cgi", {"sadr": display_address})

    # -- internals ------------------------------------------------------------
    def _assert_allowed(self, path: str) -> None:
        """Raise :class:`PermissionError` unless ``path`` is a read-only endpoint.

        This is the *single* enforcement point: every request funnels through
        :meth:`_get_raw`, so the allow-list applies regardless of which public
        method (``get_schematic`` / ``get_api`` / ``get_changer`` / ``_get``)
        was used to build the URL.  ``change.cgi`` and any other write or
        unknown endpoint is rejected before a connection is opened.
        """
        path = path.split("?", 1)[0]  # ignore query strings for allow-listing
        if path in self._allowed_paths or _SCHEMATIC_PATH_RE.match(path) or _LOG_PATH_RE.match(path):
            return
        raise PermissionError(f"write/non-read endpoint not allowed in S1: {path!r}")

    async def _get(self, path: str, params: dict[str, str]) -> str:
        self._assert_allowed(path)
        return await self._get_raw(path, params)

    async def _get_raw(self, path: str, params: dict[str, str] | None = None) -> str:
        """Perform a GET, translating transport/auth errors into CMIGatewayError.

        Enforces the read-only allow-list (:meth:`_assert_allowed`) on every
        request, no matter which public method built the URL.
        """
        self._assert_allowed(path)
        try:
            response = await self._client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise CMIConnectionError(f"CMI timeout on {path}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise CMIConnectionError(f"CMI connection error on {path}: {exc}") from exc

        if response.status_code in (401, 403):
            raise CMIAuthError(
                f"CMI rejected credentials for {path} (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            raise CMIGatewayError(
                f"CMI returned HTTP {response.status_code} for {path}: "
                f"{response.text[:200]!r}"
            )
        logger.debug("GET %s?%s -> %d", path, params, response.status_code)
        return response.text


__all__ = ["CMIClient", "ALLOWED_READ_PATHS", "build_client"]


def build_client(settings: Settings | None = None) -> CMIClient:
    """Create a :class:`CMIClient` from settings."""
    cfg = settings or get_settings()
    return CMIClient(
        base_url=cfg.base_url,
        user=cfg.cmi_user,
        password=cfg.cmi_pass,
        timeout_sec=cfg.cmi_timeout_sec,
    )
