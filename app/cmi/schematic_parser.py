"""Parser for CMI schematic ``posN`` value pages.

The CMI serves five schematic value pages from ``/schematic_files/{1..5}.cgi``.
Each page is a fragment of ``<div id="posN" ...>content</div>`` blocks that the
CMI web UI overlays on top of a static PNG (``assets/schematics/{N}.png``).
The *position* of every ``posN`` is defined in the matching layout file
``assets/schematics/{N}.html`` (CSS ``top``/``left`` in a 1600x1200 space).

This module turns that HTML into typed pydantic models:

* :func:`parse_schematic_html` -- parse one page's value HTML.
* :func:`load_schematic`        -- value HTML + layout coordinates.
* :class:`SchematicPos`, :class:`SchematicPage` -- the result models.

Classification of a ``posN`` block (in priority order):

1. settable field (``loadChanger('ADDR')`` link or ``class="changex2"
   adresse="ADDR"``) -> ``mode_select`` (operating-mode address) or ``switch``
2. transparent hotspot (``class="durchsigmoid"``, empty content) -> ``navigation``
3. text matches ``HH:MM:SS`` -> ``time``
4. text matches ``DD.MM.YYYY`` -> ``date``
5. text is a digital state word (EIN/AUS/ON/OFF/...) -> ``sensor_value``
6. text parses as a number (with optional unit) -> ``sensor_value``
7. otherwise -> ``sensor_label``
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from app.cmi import labels as cmi_labels
from app.cmi.exceptions import CMISchematicError

logger = logging.getLogger("cmi.schematic")

#: pos types that represent a live value (numeric, digital, select, switch).
VALUE_TYPES = frozenset({"sensor_value", "mode_select", "switch"})

#: Proximity association: a label div within this horizontal distance (px) of a
#: value's column is a candidate.
_LABEL_COL_TOL_PX = 80.0
#: ...and within this vertical distance (px) *above* the value. A label far
#: above the value (e.g. a section header on the other side of the page) is not
#: its label, so the search window is bounded.
_LABEL_MAX_DY_PX = 130.0

#: The five schematic pages and their human titles (live-poll pages; the
#: gateway's poller + UI page selector are built from THIS dict only).
SCHEMATIC_PAGES: dict[int, str] = {
    1: "Start",
    2: "Warmwasser",
    3: "Raumregelung",
    4: "Solar",
    5: "Kamin",
}

#: Page 6 (UVR610) is NOT live-polled (the gateway polls pages 1-5 only) but
#: IS a legal read-back page: the three Minderer-Sollspannungen (v0.6-A',
#: Discovery 18.11) are bound there.  ``parse_schematic_html`` accepts it.
_EXTRA_PAGES: frozenset[int] = frozenset({6})

#: Default schematic asset directory (relative to the repo root).
DEFAULT_SCHEMATIC_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "schematics"

#: Operating-mode display address (7-option select) -- classified as mode_select.
MODE_ADDRESS = "101301D0180"

#: Digital-state words that denote a boolean-like value, not a label.
DIGITAL_STATES = frozenset({"EIN", "AUS", "ON", "OFF", "JA", "NEIN", "AUTO", "HAND"})

#: posN block: id, attributes, inner content (dot-all).
_POS_RE = re.compile(r'<div\s+id="pos(\d+)"([^>]*)>(.*?)</div>', re.DOTALL)
_LOAD_CHANGER_RE = re.compile(r"loadChanger\('([^']+)'\)")
_ATTR_RE = re.compile(r'([a-zA-Z-]+)\s*=\s*"([^"]*)"')
_TAG_RE = re.compile(r"<[^>]+>")

_TIME_RE = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}\s*$")
_DATE_RE = re.compile(r"^\s*\d{1,2}\.\d{1,2}\.\d{4}\s*$")

#: Units, longest-match first so e.g. "m³/h" wins over "m" / "h".
_UNIT_TOKENS = (
    "m³/h", "m³/d", "m³/min", "m³", "l/min", "l/h", "l/Imp", "ct/kWh", "Bq/m³",
    "g/m³", "km/h", "kWh", "kW", "mbar", "kΩ", "mA", "mm/min", "mm/h", "mm/d",
    "°C", "Sek", "Min", "Std", "Tage", "Imp", "bar", "Hz", "km", "mm", "lx",
    "m/s", "Aus/EIN", "NEIN/JA", "OFFEN/GESCHLOSSEN", "%", "K", "V", "A", "W",
    "t", "kg", "g", "cm", "l", "m",
)
_UNIT_RE = re.compile("|".join(re.escape(u) for u in _UNIT_TOKENS))

#: Number: optional sign, integer part, optional decimal (dot or German comma).
_NUMBER_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


@dataclass(frozen=True)
class PosCoordinates:
    """Pixel coordinates of a posN within the 1600x1200 schematic space."""

    top: float
    left: float


class SchematicPos(BaseModel):
    """One parsed ``posN`` value block."""

    page: int
    index: int
    raw: str
    text: str
    value: float | None = None
    unit: str | None = None
    pos_type: str
    address: str | None = None
    x2: bool = False
    top: float | None = None
    left: float | None = None
    #: Human-readable label for a value block, resolved from the nearest
    #: label div (same schematic page, above the value, same column).
    #: ``None`` when no nearby label div exists.
    label: str | None = None

    @property
    def coordinates(self) -> PosCoordinates | None:
        if self.top is None or self.left is None:
            return None
        return PosCoordinates(top=self.top, left=self.left)

    @property
    def settable(self) -> bool:
        return self.address is not None


class SchematicPage(BaseModel):
    """All parsed positions of one schematic page."""

    page: int
    title: str
    positions: list[SchematicPos] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------
def _clean_text(content: str) -> str:
    """Strip HTML tags and collapse whitespace to a single trimmed string."""
    text = _TAG_RE.sub(" ", content)
    return " ".join(text.split())


def _extract_address(attrs: str, content: str) -> tuple[str | None, bool]:
    """Return (address, is_x2) from div attributes or a loadChanger link."""
    attr_map = dict(_ATTR_RE.findall(attrs))
    if "changex2" in (attr_map.get("class") or "").split():
        return attr_map.get("adresse"), True
    load = _LOAD_CHANGER_RE.search(content)
    if load:
        return load.group(1), False
    return None, False


def _parse_value_unit(text: str) -> tuple[float | None, str | None]:
    """Parse a numeric value and infer its unit from the text.

    Handles both German decimal comma (``75,0``) and dot decimal (``19.9``).
    Text with remaining letters after removing the number+unit (e.g.
    ``"Min 50C"``) is NOT a measurement -> value None.
    """
    unit_match = _UNIT_RE.search(text)
    unit = unit_match.group(0) if unit_match else None
    number_match = _NUMBER_RE.search(text)
    if not number_match:
        return None, None  # a unit without a number is meaningless
    # Stray letters around the number? -> not a plain measurement.
    remainder = text.replace(number_match.group(0), "").replace(unit or "", "").strip()
    if any(ch.isalpha() for ch in remainder):
        return None, None  # e.g. "Min 50C" -> label, not a measurement
    token = number_match.group(0)
    if "," in token and "." in token:
        token = token.replace(".", "").replace(",", ".")  # thousands dot + comma
    elif "," in token:
        token = token.replace(",", ".")
    try:
        value = float(token)
    except ValueError:
        return None, unit
    return value, unit


def _classify(text: str, has_address: bool, address: str | None, value: float | None) -> tuple[str, bool]:
    """Return (pos_type, is_digital_value) for a cleaned text block.

    ``value`` is the already-parsed number (None when the text is not a plain
    measurement).  ``is_digital_value`` is True when the text is a boolean-like
    state word (kept as sensor_value even though it is not numeric).
    """
    if has_address:
        return ("mode_select" if address == MODE_ADDRESS else "switch"), False
    if _TIME_RE.match(text):
        return "time", False
    if _DATE_RE.match(text):
        return "date", False
    if text.upper() in DIGITAL_STATES:
        return "sensor_value", True
    if value is not None:
        return "sensor_value", False
    return "sensor_label", False


# ---------------------------------------------------------------------------
# Layout (coordinates)
# ---------------------------------------------------------------------------
def parse_schematic_layout(layout_html: str) -> dict[int, PosCoordinates]:
    """Parse a layout HTML (``assets/schematics/N.html``) into coordinates."""
    coords: dict[int, PosCoordinates] = {}
    for match in re.finditer(r"#pos(\d+)\{([^}]*)\}", layout_html):
        index = int(match.group(1))
        body = match.group(2)
        top = re.search(r"top:\s*(-?\d+(?:\.\d+)?)px", body)
        left = re.search(r"left:\s*(-?\d+(?:\.\d+)?)px", body)
        if top and left:
            coords[index] = PosCoordinates(top=float(top.group(1)), left=float(left.group(1)))
    return coords


def load_schematic_layout(schematic_dir: Path, page: int) -> dict[int, PosCoordinates]:
    """Load and parse the layout file for ``page``. Missing file -> empty dict."""
    layout = schematic_dir / f"{page}.html"
    if not layout.is_file():
        logger.warning("Schematic layout not found: %s", layout)
        return {}
    return parse_schematic_layout(layout.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Label association
# ---------------------------------------------------------------------------
def _nearest_label(value: SchematicPos, positions: list[SchematicPos]) -> str | None:
    """Return the text of the nearest label div for a value block.

    Rule (verified against the recorded ``discovery/live_*.cgi`` + layout CSS):
    a label lives in its own ``posN`` div in the same horizontal column
    (``left`` within :data:`_LABEL_COL_TOL_PX`).  In practice the label sits
    *above* the value, or on the same row to the left.  So:

    1. among label divs at or above the value (vertical gap
       :data:`_LABEL_MAX_DY_PX` at most), pick the one immediately above
       (smallest gap) -- the intended label for a column of stacked values;
    2. else, among label divs on the same row / slightly below
       (gap >= -30 px), pick the closest one.

    Returns ``None`` when no such label exists (the caller then falls back to
    the curated :data:`app.cmi.labels.LABEL_MAP`).
    """
    if value.top is None or value.left is None:
        return None
    labels = [
        p for p in positions
        if p.pos_type == "sensor_label" and p.text and p.top is not None and p.left is not None
    ]
    same_column = [p for p in labels if abs(p.left - value.left) <= _LABEL_COL_TOL_PX]
    above = [p for p in same_column if 0 <= (value.top - p.top) <= _LABEL_MAX_DY_PX]
    if above:
        return min(above, key=lambda p: value.top - p.top).text
    same_row = [p for p in same_column if -30.0 <= (value.top - p.top) < 0.0]
    if same_row:
        return min(same_row, key=lambda p: abs(value.top - p.top)).text
    return None


def _resolve_label(pos: SchematicPos, positions: list[SchematicPos]) -> str | None:
    """Resolve the display label for a value block.

    Order (per the S1-1 DoD):
    1. proximity to the nearest label div on the same page (primary),
    2. fall back to the curated :data:`app.cmi.labels.LABEL_MAP` (verified
       across pages / discovery fixtures),
    3. otherwise ``None`` -> the state layer emits a generated ``S{page}.{pos}``
       id (should not happen for the 5 shipped pages once LABEL_MAP is complete).
    """
    prox = _nearest_label(pos, positions)
    if prox:
        return prox
    return cmi_labels.label_for(pos.page, pos.index)


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------
def parse_schematic_html(html: str, page: int, *, title: str | None = None,
                         layout_html: str | None = None) -> SchematicPage:
    """Parse one schematic value page into a :class:`SchematicPage`."""
    if page not in SCHEMATIC_PAGES and page not in _EXTRA_PAGES and not title:
        raise CMISchematicError(f"Unknown schematic page {page}", page=page)

    coords = parse_schematic_layout(layout_html) if layout_html is not None else {}

    positions: list[SchematicPos] = []
    for match in _POS_RE.finditer(html):
        index = int(match.group(1))
        attrs = match.group(2)
        content = match.group(3)
        text = _clean_text(content)
        address, is_x2 = _extract_address(attrs, content)

        if classify_hotspot(attrs, content):
            pos_type, is_digital = "navigation", False
            value = unit = None
        else:
            value, unit = _parse_value_unit(text)
            pos_type, is_digital = _classify(
                text, has_address=address is not None, address=address, value=value
            )
            if pos_type in ("time", "date"):
                value, unit = None, None
            elif pos_type == "sensor_value" and is_digital:
                value = None  # digital states are not numeric

        coord = coords.get(index)
        positions.append(
            SchematicPos(
                page=page,
                index=index,
                raw=match.group(0),
                text=text,
                value=value,
                unit=unit,
                pos_type=pos_type,
                address=address,
                x2=is_x2,
                top=coord.top if coord else None,
                left=coord.left if coord else None,
            )
        )

    # Second pass: resolve labels for value blocks from the full position set
    # (proximity needs all label divs of the page).
    for pos in positions:
        if pos.pos_type in VALUE_TYPES:
            pos.label = _resolve_label(pos, positions)

    return SchematicPage(page=page, title=title or SCHEMATIC_PAGES.get(page, f"Seite {page}"),
                         positions=positions)


def parse_schematic_html_with_classified(
    html: str, page: int, *, title: str | None = None, layout_html: str | None = None
) -> SchematicPage:
    """Alias kept for readability (same as :func:`parse_schematic_html`)."""
    return parse_schematic_html(html, page, title=title, layout_html=layout_html)


def load_schematic(schematic_dir: Path, page: int, value_html: str) -> SchematicPage:
    """Parse fetched value HTML for ``page`` and merge disk layout coordinates.

    Args:
        schematic_dir: Directory containing the ``{page}.html`` layouts.
        page: Schematic page number (1..5).
        value_html: The value HTML fetched from the CMI (``{page}.cgi``).
    """
    layout_path = schematic_dir / f"{page}.html"
    layout_html = layout_path.read_text(encoding="utf-8") if layout_path.is_file() else None
    return parse_schematic_html(value_html, page, layout_html=layout_html)


def parse_schematic_file(schematic_dir: Path, page: int, value_html: str) -> SchematicPage:
    """Parse a recorded value fixture (``live_{page}.cgi``) plus the layout.

    Identical to :func:`load_schematic`; the name documents the fixture use.
    """
    return load_schematic(schematic_dir, page, value_html)


def classify_hotspot(attrs: str, content: str) -> bool:
    """True when a posN is a transparent navigation hotspot."""
    attr_map = dict(_ATTR_RE.findall(attrs))
    cls = (attr_map.get("class") or "").split()
    return "durchsichtig" in cls and not _clean_text(content).strip()
