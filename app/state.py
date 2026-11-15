"""Normalized live-state document (StateStore).

Merges the two live data sources into one document:

* the five schematic value pages (fast, ~0.7 s, polled every ``CMI_POLL_SEC``)
* the JSON-API node snapshots (rate-limited to 1/min; node 1 and node 32)

The result is served by ``GET /api/v1/state``.  When the CMI is unreachable,
the store keeps the last known values and raises a *stale* flag instead of
losing data.

Thread/async safety: the store is only touched from the gateway event loop
(FastAPI handlers + poller tasks).  All mutations are synchronous (no awaits
inside), so a snapshot can never observe a half-applied update.
"""

from __future__ import annotations

import logging
import time

from pydantic import BaseModel, Field, computed_field

from app.cmi import labels
from app.cmi.json_api import JsonApiResult
from app.cmi.schematic_parser import SchematicPage, SchematicPos

logger = logging.getLogger("cmi.state")

#: Sources.
SOURCE_SCHEMATIC = "schematic"
SOURCE_JSON_API = "json_api"

#: Data sections inside "Data" that carry measurable values.
_VALUE_SECTIONS = ("Inputs", "Outputs", "DL-Bus", "Logging Analog", "Logging Digital")


class StateField(BaseModel):
    """One normalized live value in the state document."""

    id: str
    label: str
    value: float | None = None
    unit: str | None = None
    raw: str
    source: str
    reference: str
    address: str | None = None
    ras_state: str | None = None
    timestamp: float = 0.0


class CmiHealth(BaseModel):
    """Health flags for both data routes."""

    schematic_ok: bool
    schematic_time: float | None = None
    schematic_error: str | None = None
    api_ok: bool
    api_time: float | None = None
    api_error: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stale(self) -> bool:
        """True when neither data route has delivered data successfully."""
        return not (self.schematic_ok or self.api_ok)


class StateDocument(BaseModel):
    """The full normalized live state served by ``/api/v1/state``."""

    cmi: str = ""
    timestamp: float
    health: CmiHealth
    schematics: dict[int, SchematicPage] = Field(default_factory=dict)
    fields: list[StateField] = Field(default_factory=list)
    params_resolved: int = 0

    def fields_by_source(self, source: str) -> list[StateField]:
        return [f for f in self.fields if f.source == source]


# ---------------------------------------------------------------------------
# Field builders
# ---------------------------------------------------------------------------
def _schematic_field(page: SchematicPage, pos: SchematicPos) -> StateField | None:
    """Build a state field from a schematic position (value-ish blocks only)."""
    if pos.pos_type not in ("sensor_value", "mode_select", "switch"):
        return None
    # Label resolution order: proximity (pos.label, set by the parser) -> curated
    # LABEL_MAP (fallback) -> generated id.
    name = pos.label or labels.label_for(page.page, pos.index)
    if not name:
        if pos.pos_type == "sensor_value":
            name = f"S{page.page}.{pos.index}"
        else:
            name = f"{pos.text or 'Parameter'} ({page.page}.{pos.index})"
    value = pos.value
    if pos.pos_type in ("mode_select", "switch"):
        value = None  # digital/select states stay in `raw` (text), not numeric
    return StateField(
        id=f"sch:{page.page}:{pos.index}",
        label=name,
        value=value,
        unit=pos.unit,
        raw=pos.text,
        source=SOURCE_SCHEMATIC,
        reference=f"page={page.page};pos={pos.index}",
        address=pos.address,
    )


def _json_fields(result: JsonApiResult) -> list[StateField]:
    """Flatten one JSON-API result into state fields."""
    fields: list[StateField] = []
    node_device = result.header.device_name if result.header else None
    for section in _VALUE_SECTIONS:
        for node_value in result.values.get(section, []):
            v = node_value.value
            label = node_value.designation or f"Node {result.node} {section} {node_value.number}"
            reference = f"node={result.node};section={section};channel={node_value.number}"
            if node_device:
                reference += f";device={node_device}"
            fields.append(
                StateField(
                    id=f"api:{result.node}:{section.lower()}:{node_value.number}",
                    label=label,
                    value=v.value,
                    unit=v.unit_symbol,
                    raw=f"{v.value:g}",
                    source=SOURCE_JSON_API,
                    reference=reference,
                    ras_state=v.ras_state,
                )
            )
    return fields


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class StateStore:
    """In-memory normalized live state (single event loop, see module doc)."""

    def __init__(self, *, cmi_url: str = "") -> None:
        self._cmi_url = cmi_url
        self._schematics: dict[int, SchematicPage] = {}
        self._schematic_fields: list[StateField] = []
        self._api_results: dict[int, JsonApiResult] = {}
        self._api_fields: dict[int, list[StateField]] = {}
        self._last_merge_ts: float = 0.0
        self._params_resolved: int = 0
        self.health = CmiHealth(schematic_ok=False, api_ok=False)

    # -- updates ------------------------------------------------------------
    def update_schematic(self, page: SchematicPage, *, ts: float | None = None) -> None:
        """Store a freshly parsed schematic page + its derived fields."""
        ts = time.time() if ts is None else ts
        self._schematics[page.page] = page
        self._schematic_fields = [
            field
            for stored in self._schematics.values()
            for field in _stamp(_fields_of(stored), ts)
        ]
        self._last_merge_ts = ts
        self.health.schematic_ok = True
        self.health.schematic_time = ts
        self.health.schematic_error = None

    def update_schematic_failed(self, error: str, *, ts: float | None = None) -> None:
        """Mark the schematic route as broken (last values remain -> stale)."""
        ts = time.time() if ts is None else ts
        self.health.schematic_ok = False
        self.health.schematic_time = ts
        self.health.schematic_error = error

    def mark_schematic_ping(self, ok: bool, *, error: str | None = None, ts: float | None = None) -> None:
        """Update reachability from the (throttled) health ping only.

        Does not replace stored schematic values; only the health flags.
        """
        ts = time.time() if ts is None else ts
        self.health.schematic_ok = ok
        self.health.schematic_time = ts
        self.health.schematic_error = None if ok else error

    def update_api(self, result: JsonApiResult, *, ts: float | None = None) -> None:
        """Store a fresh JSON-API node snapshot + its derived fields."""
        ts = time.time() if ts is None else ts
        self._api_results[result.node] = result
        self._api_fields[result.node] = _stamp(_json_fields(result), ts)
        self.health.api_ok = True
        self.health.api_time = ts
        self.health.api_error = None

    def update_api_failed(self, node: int, error: str, *, ts: float | None = None) -> None:
        """Mark the JSON-API route as broken (last values remain -> stale)."""
        ts = time.time() if ts is None else ts
        self.health.api_ok = False
        self.health.api_time = ts
        self.health.api_error = f"node {node}: {error}"

    def set_params_resolved(self, count: int) -> None:
        self._params_resolved = count

    # -- read ----------------------------------------------------------------
    def snapshot(self) -> StateDocument:
        """Return a consistent point-in-time snapshot of the state."""
        fields = list(self._schematic_fields) + [f for fl in self._api_fields.values() for f in fl]
        return StateDocument(
            cmi=self._cmi_url,
            timestamp=self._last_merge_ts,
            health=self.health.model_copy(deep=True),
            schematics={p: page.model_copy(deep=True) for p, page in self._schematics.items()},
            fields=fields,
            params_resolved=self._params_resolved,
        )

    @property
    def api_results(self) -> dict[int, JsonApiResult]:
        """Raw JSON-API snapshots (for tests/observability)."""
        return dict(self._api_results)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _fields_of(page: SchematicPage) -> list[StateField]:
    fields: list[StateField] = []
    for pos in page.positions:
        field = _schematic_field(page, pos)
        if field is not None:
            fields.append(field)
    return fields


def _stamp(fields: list[StateField], ts: float) -> list[StateField]:
    for field in fields:
        field.timestamp = ts
    return fields
