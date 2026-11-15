"""Exception hierarchy for the CMI gateway."""

from __future__ import annotations


class CMIGatewayError(Exception):
    """Base class for all gateway errors."""


class CMIConnectionError(CMIGatewayError):
    """The CMI was not reachable (network error / timeout)."""


class CMIAuthError(CMIGatewayError):
    """The CMI rejected the credentials (HTTP 401/403)."""


class CMISchematicError(CMIGatewayError):
    """A schematic page could not be fetched or parsed."""

    def __init__(self, message: str, page: int | None = None, http_status: int | None = None):
        super().__init__(message)
        self.page = page
        self.http_status = http_status


class CMIJsonApiError(CMIGatewayError):
    """The JSON-API returned a non-OK status code or unparseable JSON."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class CMIRateLimitedError(CMIJsonApiError):
    """The JSON-API answered with status code 4 (TOO MANY REQUESTS)."""

    def __init__(
        self, message: str = "CMI JSON-API rate limit exceeded (status 4)", *, status_code: int = 4
    ):
        super().__init__(message, status_code=status_code)


class CMIChangerError(CMIGatewayError):
    """A changer display address could not be resolved."""

    def __init__(self, message: str, address: str | None = None):
        super().__init__(message)
        self.address = address


class CMISafetyError(CMIGatewayError):
    """A write was refused by the safety rules of :mod:`app.cmi.write`.

    Raised when a write is rejected because of (a) the RISK-address block
    (oil burner), (b) an address outside the writable whitelist, (c) a
    current value that cannot be read unambiguously, (d) a ``changeto``
    value outside the allowed option list / numeric range, or (e) observed
    state drift after a no-op write (strict mode).
    """

    def __init__(self, message: str, address: str | None = None):
        super().__init__(message)
        self.address = address


class CmiLogError(CMIGatewayError):
    """The CMI internal log (/LOG) could not be fetched or decoded.

    Raised when a ``/LOG/*`` body is structurally invalid (no record grid,
    bad magic) so the log poller can latch the failure on /health instead of
    crashing.
    """

    def __init__(self, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status
