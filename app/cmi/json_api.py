"""Client + models for the CMI JSON-API (``/INCLUDE/api.cgi``, API version 8).

The JSON-API is strictly **read-only** and rate-limited to one request per
minute (status code 4 = TOO MANY REQUESTS).  See ``discovery/json-api-doc.txt``
for the full protocol reference.

Endpoint::

    GET /INCLUDE/api.cgi?jsonnode=<nr>&jsonparam=<I,O,D,Sg,Sd,St,Ss,Na,Nd,M,AM,AK,La,Ld>
                        [&jsondesignation=1]

The response is always JSON::

    {"Header": {"Version":8, "Device":"91", "Timestamp":...},
     "Data": {"Logging Analog": [...], "Logging Digital": [...]},
     "Status": "OK", "Status code": 0}
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from enum import IntEnum

from pydantic import BaseModel, Field

from app.cmi.client import CMIClient
from app.cmi.exceptions import CMIJsonApiError, CMIRateLimitedError
from app.cmi.queue import CMIGuard

logger = logging.getLogger("cmi.json_api")


class JsonApiStatus(IntEnum):
    """JSON-API status codes (json-api-doc.txt, section 6.3)."""

    OK = 0
    NODE_ERROR = 1
    FAIL = 2
    SYNTAX_ERROR = 3
    TOO_MANY_REQUESTS = 4
    DEVICE_NOT_SUPPORTED = 5
    TOO_FEW_ARGUMENTS = 6
    CAN_BUSY = 7

    @property
    def description(self) -> str:
        return {
            JsonApiStatus.OK: "OK",
            JsonApiStatus.NODE_ERROR: "Node not available",
            JsonApiStatus.FAIL: "Failure during the CAN-request / parameter not available",
            JsonApiStatus.SYNTAX_ERROR: "Error in the request string",
            JsonApiStatus.TOO_MANY_REQUESTS: "Only one request per minute is permitted",
            JsonApiStatus.DEVICE_NOT_SUPPORTED: "Device not supported",
            JsonApiStatus.TOO_FEW_ARGUMENTS: "jsonnode or jsonparam not set",
            JsonApiStatus.CAN_BUSY: "CAN bus is busy",
        }.get(self, f"Any other error ({int(self)})")


# ---------------------------------------------------------------------------
# Unit table
#
# Derived from json-api-doc.txt (API v8, section 6.2.4.2).  NOTE: the PDF text
# extraction is column-misaligned, so the table below was cross-validated
# against the *recorded live responses* (discovery/la_ld_node32.json), which
# prove: Unit "1" = degC, "13" = V, "43" = Aus/EIN, "46" = RAS degC.
# Units with no live confirmation are marked in the doc string.
# ---------------------------------------------------------------------------
UNIT_MAP: dict[str, str] = {
    "1": "°C",
    "2": "W/m²",
    "3": "l/h",
    "4": "Sek",
    "5": "Min",
    "6": "l/Imp",
    "7": "K",
    "8": "%",
    "10": "kW",
    "11": "kWh",
    "12": "MWh",
    "13": "V",
    "14": "mA",
    "15": "Std",
    "16": "Tage",
    "17": "Imp",
    "18": "kΩ",
    "19": "l",
    "20": "km/h",
    "21": "Hz",
    "22": "l/min",
    "23": "bar",
    "24": "km",
    "25": "m",
    "26": "mm",
    "27": "m³",
    "28": "l/d",
    "29": "m/s",
    "30": "m³/min",
    "31": "m³/h",
    "32": "m³/d",
    "40": "mm/min",
    "41": "mm/h",
    "42": "mm/d",
    "43": "Aus/EIN",
    "44": "NEIN/JA",
    "46": "°C (RAS)",
    "50": "€",
    "51": "$",
    "52": "g/m³",
    "53": "°",
    "54": "°",
    "56": "Sek",
    "57": "%",
    "58": "Uhr",
    "59": "A",
    "60": "mbar",
    "63": "Pa",
    "65": "ppm",
    "66": "W",
    "67": "t",
    "68": "kg",
    "69": "g",
    "70": "cm",
    "71": "K",
    "72": "lx",
    "73": "Bq/m³",
    "74": "ct/kWh",
    "75": "OFFEN/GESCHLOSSEN",
}

#: RAS-state meaning (only present when Unit == "46").
RAS_STATES: dict[str, str] = {
    "0": "Zeit/Auto",
    "1": "Standard",
    "2": "Setback",
    "3": "Standby/Frostschutz",
}


class JsonValue(BaseModel):
    """One analog/digital value as delivered by the JSON-API."""

    value: float
    unit: str | None = None
    unit_symbol: str | None = None
    ras: str | None = None  # RAS-state 0..3, only when unit == "46"
    ras_state: str | None = None
    state: float | None = None  # digital state of an analog output


class JsonNodeValue(BaseModel):
    """One entry of a JSON-API data array (Inputs, Logging Analog, ...)."""

    number: int
    ad: str = ""  # "A" analog / "D" digital
    value: JsonValue
    designation: str | None = None

    @property
    def id(self) -> str:
        """Stable short identifier, e.g. ``A1`` / ``D3``."""
        return f"{self.ad}{self.number}"


class JsonApiHeader(BaseModel):
    """JSON-API response header."""

    version: int | None = None
    device: str | None = None
    timestamp: int | None = None

    @property
    def device_name(self) -> str | None:
        if self.device is None:
            return None
        try:
            return _DEVICE_NAMES.get(int(self.device, 16), f"0x{self.device}")
        except ValueError:
            return self.device

    @property
    def timestamp_dt(self) -> datetime | None:
        if self.timestamp is None:
            return None
        return datetime.fromtimestamp(self.timestamp, tz=UTC)


_DEVICE_NAMES: dict[int, str] = {
    0x7F: "CoE",
    0x80: "UVR1611",
    0x87: "UVR16x2",
    0x88: "RSM610",
    0x8B: "CMI",
    0x8C: "CAN-EZ2",
    0x8D: "CAN-MTx2",
    0x8E: "CAN-BC2",
    0x8F: "UVR65",
    0x91: "UVR610",
    0x92: "UVR67",
}


class JsonApiResult(BaseModel):
    """Fully parsed JSON-API response for one node/parameter set."""

    node: int
    params: str
    header: JsonApiHeader | None = None
    values: dict[str, list[JsonNodeValue]] = Field(default_factory=dict)
    status: str | None = None
    status_code: int = 0

    @property
    def ok(self) -> bool:
        return self.status_code == JsonApiStatus.OK

    @property
    def error(self) -> str | None:
        if self.ok:
            return None
        try:
            return f"{JsonApiStatus(self.status_code).name}: {JsonApiStatus(self.status_code).description}"
        except ValueError:
            return f"UNKNOWN ERROR ({self.status_code})"


def parse_json_api(payload: dict, *, node: int, params: str) -> JsonApiResult:
    """Parse a decoded JSON-API response dict into :class:`JsonApiResult`.

    Raises:
        CMIRateLimitedError: status code 4 (TOO MANY REQUESTS).
        CMIJsonApiError: any other non-OK status code.
    """
    raw_status_code = payload.get("Status code")
    status_code = int(raw_status_code) if raw_status_code is not None else 0
    status_text = payload.get("Status")

    if status_code != 0:
        message = f"JSON-API node {node} ({params}) failed: {status_text} (code {status_code})"
        if status_code == JsonApiStatus.TOO_MANY_REQUESTS:
            raise CMIRateLimitedError(message, status_code=status_code)
        raise CMIJsonApiError(message, status_code=status_code)

    header_raw = payload.get("Header") or {}
    header = JsonApiHeader(
        version=header_raw.get("Version"),
        device=header_raw.get("Device"),
        timestamp=header_raw.get("Timestamp"),
    )

    data = payload.get("Data") or {}
    values: dict[str, list[JsonNodeValue]] = {}
    for section_name, entries in data.items():
        if not isinstance(entries, list):
            continue
        parsed: list[JsonNodeValue] = []
        for entry in entries:
            value_raw = entry.get("Value") or {}
            unit = value_raw.get("Unit")
            ras_raw = value_raw.get("RAS")
            value = JsonValue(
                value=float(value_raw.get("Value", 0.0)),
                unit=unit,
                unit_symbol=UNIT_MAP.get(unit, unit) if unit is not None else None,
                ras=str(ras_raw) if ras_raw is not None else None,
                ras_state=RAS_STATES.get(str(ras_raw)) if ras_raw is not None else None,
                state=value_raw.get("State"),
            )
            parsed.append(
                JsonNodeValue(
                    number=int(entry.get("Number", 0)),
                    ad=str(entry.get("AD", "")),
                    value=value,
                    designation=entry.get("Designation"),
                )
            )
        if parsed:
            values[section_name] = parsed

    return JsonApiResult(
        node=node,
        params=params,
        header=header,
        values=values,
        status=status_text,
        status_code=status_code,
    )


class JsonApiClient:
    """High-level JSON-API client bound to a :class:`CMIGuard`.

    Every call goes through the 1/min rate limiter **and** the serial queue,
    so concurrent callers (pollers, on-demand fetches) can never violate the
    CMI rate limit or run two CMI requests in parallel.
    """

    def __init__(self, client: CMIClient, guard: CMIGuard) -> None:
        self._client = client
        self._guard = guard

    async def fetch(self, node: int, params: str, *, designation: bool = False) -> JsonApiResult:
        """Fetch one node/parameter combination (rate-limited + serial).

        Args:
            node: CAN node number (1 = UVR1611, 32 = UVR610).
            params: Comma-separated ``jsonparam`` string, e.g. ``"I,O,D"``.
            designation: Add ``jsondesignation=1`` (x2 devices only).

        Returns:
            The parsed :class:`JsonApiResult` (status OK guaranteed,
            otherwise an exception is raised).
        """
        query: dict[str, str] = {"jsonnode": str(node), "jsonparam": params}
        if designation:
            query["jsondesignation"] = "1"

        raw = await self._guard.run_api(lambda: self._client.get_api(query))
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CMIJsonApiError(f"JSON-API returned invalid JSON: {exc}") from exc
        return parse_json_api(payload, node=node, params=params)
