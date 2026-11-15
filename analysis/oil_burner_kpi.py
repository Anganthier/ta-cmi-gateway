"""Oil-burner KPI: effective burn time vs. tank-gauge oil consumption.

Task OEL-KPI-1 (2026-11): correlate the oil burner's *effective burn time*
derived from the 10 s timeseries (demand flag + supply-temperature gate) with
the *measured* oil consumption from the tank gauge (Zollstock) log.

Data sources (all read-only):

* ``data/winsol_pq/YYYY/MM/YYYY-MM.parquet``  (2020-08-30 .. 2026-10-27)
* ``data/cmi_log_pq/YYYY/MM/YYYY-MM.parquet`` (2026-10-27 .. today, same
  dev0 channel names, same 10 s grid)
* ``tmp/tank_zollstock_2015_2026.csv``        (tank-gauge measurements;
  parsed for the reference history block - the KPI segment canon itself
  is the explicit anchor set, see ``TANK_VALID_FROM`` below)

Method summary (full documentation: ``docs/oel-kpi-brenndauer.md``):

* A 10 s sample counts as "burning" IFF ``dev0_digital_05 = 1`` (oil-burner
  demand, UVR O5) AND ``dev0_analog_05 >= T_IGN`` (oil boiler supply
  temperature, default 30 deg C).  ``T_IGN`` separates ignition failure /
  pre-ignition ramp (kettle side still cold) from genuine combustion.
  Sensitivity check required by the task spec: T_IGN in {28, 30, 35} deg C.
* Failure-day rule (two tiers; the spec's 4 h/day threshold is kept for the
  strict tier, the candidate floor is data-supported):  a calendar day with
  >= MIN_COLD_DEMAND_HOURS (4 h, the task spec's value) cold-demand hours
  ``(d5=1 AND a5 < 30)`` AND daily ``a5`` max < MAX_A5_FAILURE_DEG is a
  STRICT failure day and is EXCLUDED from effective burn time.  The 4 h is
  reachable in the data: on the known failure days the cold demand runs
  3.1-24.0 h/day (near-continuous unmet demand), while the strongest
  non-failure day stays at 2.43 h (28.11.2025; days above that carry
  a5 max > 78 deg C and are excluded by the temperature cap).  Days with >=
  COLD_DEMAND_CANDIDATE_HOURS (2.5 h, calibrated into the 2.43/3.12 h gap)
  and
  an a5 max up to MAX_A5_INTERMITTENT_DEG are INTERMITTENT days ("manchmal
  hat es geklappt"): listed inside the failure window but NOT excluded
  (their successful ignition samples did burn).  maintainer-Override
  (2026-11-10, authoritative; default ACTIVE, disable via
  ``--no-oct2025-override``): the 5 "strict" days 13.-17.10.2025 are
  re-classified as intermittent (``overridden=True``) per maintainer's
  testimony - successful restarts every day/night.  The Oct-2025 window
  then carries 0 strict days -> listed with a note, NOTHING excluded
  (those days had 0 warm samples -> zero numeric effect).  Jan-2026
  stays the only total failure.  All values are documented in code,
  report and docs.
* New-burner era (01.02.2026 -> window end, ``--era-start``): a
  replacement burner (same brand/model) was installed in Feb 2026.  The
  module reports the era's burn hours (total + per month + O6), the
  strict-day sanity count (0 expected) and three oil scenarios based on
  the S3 anchor segment: ``s3_rate`` (S3 rate over the era span),
  ``sub_split`` (S3a rate over 01.02 -> 21.08 + the S3b sub-segment
  litres) and the clean mini segment S3b 21.08.26 -> 09.11.26.  Both
  full-era scenarios carry the +/-300 L gauge-uncertainty band
  (conservative).
* Tank model (Q-127 correction, 2026-11-10, authoritative): the KPI
  segment canon starts at ``TANK_VALID_FROM`` (01.06.2023).  Before that
  the 2021/2022 deliveries were NOT recorded, so every gauge-sequence
  segment starting before 2023-06 is listed for reference but marked
  ``usable=False`` and enters NO KPI (no seasons, no trend, no monthly
  interpolation).  The usable segments are the anchor segments built from
  the full-tank deliveries (4500 L each) and the cm-precision
  intermediate readings: S1 01.06.23 -> 31.10.24 (2945 L, exact),
  S2 31.10.24 -> 23.10.25 (2947 L, exact), S3 23.10.25 -> 09.11.26
  (2670 L = 4500 - 1830, gauge +/-300 L) and the S3 sub-segments
  S3a/S3b (intermediate reading 2070 L on 21.08.26).  S1+S2 form the
  OLD-BURNER baseline L/h.  The gauge-sequence segments of the raw CSV
  rows are still parsed and listed ("Historie, nicht verwertbar"), and
  the old A1 variants are gone: A1/A2 are RESOLVED (entry offset of the
  2945 L delivery quantity to the 19.10.25 row; unlogged 31.10.24
  delivery).
* v0.6-D KPI (2026-11-12, maintainer requirement): the compass metric is
  **litres per heating degree day (HDD-15), corrected for the wood
  share**.  The wood boiler's activity (``dev0_analog_09`` > 65 deg C,
  the existing WOOD_ACTIVE_DEG proxy) VARIES season to season (Okt-2026
  record: 13 wood-active days vs. 4 in Okt-2025 and 8 in Okt-2021), so
  a raw L/HDD-15 value is not comparable across periods without making
  the wood share explicit.  For every KPI period - anchor segments
  S1/S2/S3/S3b (mini), heating seasons and calendar months - the module
  now reports in parallel: (a) raw ``L/hdd15``, (b) the oil-heating
  share ``oel_heiz_share = oel_heiz_h / (oel_heiz_h + wood_h)`` with
  ``oel_heiz_h`` = effective burn hours EXCLUDING O6 (WW) hours, and
  (c) the corrected ``L/hdd15_korr = L / (hdd15 * oel_heiz_share)`` =
  "litres per heating degree day the oil burner actually had to supply".
  The correction is an HOURS-PROXY (wood boiler supply temperature as
  activity signal), NOT a wood energy balance - that limitation is
  documented in every output.  The gauge-uncertainty band
  (+/-TANK_GAUGE_UNCERTAINTY_L on the segment litres) is carried onto
  the KPI; gauge uncertainty of the WOOD SHARE itself is NOT modelled.
  Wood-primary days (wood_h > oel_heiz_h, the oil burner barely ran
  while the wood boiler carried the heating) are counted as context in
  every period row.  Month litres are a per-day interpolation from the
  top-level anchor segments (flagged via ``estimated_days``; months with
  fewer than MIN_MONTH_HDD15 = 5 HDD-15 are skipped as degenerate
  summer/shoulder noise); sub-segments S3a/S3b never enter the KPI
  additively (Q-127).

Outputs (written under ``--out``, default ``tmp/oel_kpi``):

* ``oel_kpi.json``          -- machine-readable KPIs, failure days/windows,
                               segments, monthly oil interpolation, trend fit
* ``burn_hours_monthly.csv`` -- per month: raw/effective burn hours, failure
                               days, ext avg, HDD-15, wood hours, O6 hours
* ``report.md``             -- human-readable German report (for maintainer)

Usage::

    python -m analysis.oil_burner_kpi --out tmp/oel_kpi \
        [--start 2020-09-01] [--end 2026-11-10]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Channel canon (verified by orchestrator recon 2026-11 - do NOT re-derive)
# ---------------------------------------------------------------------------
COL_DEMAND = "dev0_digital_05"  # O5 "Anforderung Oelbrenner" (0/1), ON/OFF burner
COL_SUPPLY = "dev0_analog_05"  # oil boiler supply temperature (deg C)
COL_EXT = "dev0_analog_08"  # outdoor temperature (deg C)
COL_WOOD = "dev0_analog_09"  # wood boiler supply temperature (deg C)
COL_WW = "dev0_digital_06"  # O6 "Ladep.WW.5" - domestic hot water via oil

#: Ignition/recognise threshold in deg C (data-supported, see docs).
DEFAULT_T_IGN_DEG = 30.0
#: Sensitivity thresholds required by the task spec.
SENSITIVITY_T_IGN: tuple[float, ...] = (28.0, 30.0, 35.0)
#: Failure day: minimum cold-demand hours (d5=1 AND a5<30) per calendar day
#: - the task spec's 4 h value (reachable: failure days 3.6-24.0 h cold).
MIN_COLD_DEMAND_HOURS = 4.0
#: Cold-demand floor for a candidate failure day (window membership).
#: 2.5 h is calibrated into the data gap: the strongest non-failure day
#: (28.11.2025, a5 max 69) has 2.43 h cold demand; the weakest failure
#: candidate day (19.10.2025) has 3.12 h.
COLD_DEMAND_CANDIDATE_HOURS = 2.5
#: Failure day: daily a5 max must stay below this (deg C) for a STRICT
#: failure day (excluded from burn time) - the spec's criterion.
MAX_A5_FAILURE_DEG = 40.0
#: Upper a5 max for an INTERMITTENT failure day (window day, NOT excluded -
#: the burner managed ignition that day, "manchmal hat es geklappt").
MAX_A5_INTERMITTENT_DEG = 78.0
#: Wood-active proxy threshold (deg C).
WOOD_ACTIVE_DEG = 65.0
#: HDD base temperature (deg C).
HDD_BASE_DEG = 15.0
#: Heating season runs Oct 09 ... Apr 30 (task spec).
SEASON_START_MONTH = 10
SEASON_START_DAY = 9
SEASON_END_MONTH = 4
SEASON_END_DAY = 30
#: Summer months for the "WW (O6) im Sommer" context.
SUMMER_MONTHS: tuple[int, ...] = (5, 6, 7, 8, 9)
#: Frost hint: outdoor-avg threshold for "cold day without any oil demand".
FROST_HINT_EXT_DEG = 5.0
#: Samples per full day on the 10 s grid (completeness check).
SAMPLES_PER_DAY = 8640
#: Parquet sources (winsol = canonical 2020-08 .. 2026-10, cmi_log = the
#: continuation; both have identical dev0 column names).
DEFAULT_PARQUET_GLOBS: tuple[str, ...] = (
    "data/winsol_pq/*/*/*.parquet",
    "data/cmi_log_pq/*/*/*.parquet",
)
#: ------------------------------------------------------------------------
#: Tank model (Q-127 correction, 2026-11-10, maintainer - authoritative):
#: the KPI segment canon is an explicit anchor set, NOT the raw CSV gauge
#: sequence.  The 2021/2022 deliveries were never recorded, so nothing
#: before TANK_VALID_FROM is usable for any KPI.  A full delivery always
#: tops the tank to TANK_FULL_L (4500 L - the full sensor triggers a few
#: cm below the 4800 L physical max, so every full fill reads 4500 L /
#: 1500 mm).  The CSV's ``fehlmeng`` column is formula noise
#: (4800 - litres on every row) and is never used, like
#: ``ltr_pro_tag_owner``.
# ------------------------------------------------------------------------
#: Full tank volume in litres (every full delivery reaches exactly this).
TANK_FULL_L = 4500.0
#: KPI segment canon starts here (oldest exact anchor: full delivery
#: 01.06.2023, 3500 L).  Segments starting before this date are reference
#: only (usable=False, no KPI).
TANK_VALID_FROM = "2023-06-01"
#: Full deliveries of the KPI window: (delivery date, quantity in litres).
#: 27.01.20 and 16.11.20 predate TANK_VALID_FROM and are kept for
#: documentation only.  After each delivery the tank holds TANK_FULL_L.
TANK_FULL_DELIVERIES: tuple[tuple[str, float], ...] = (
    ("2020-01-27", 0.0),  # before KPI window - documentary only
    ("2020-11-16", 0.0),  # before KPI window - documentary only
    ("2023-06-01", 3500.0),
    ("2024-10-31", 2945.0),
    ("2025-10-23", 2947.0),
)
#: cm-precision intermediate readings inside S3: (reading date, litres).
TANK_INTERMEDIATE_READINGS: tuple[tuple[str, float], ...] = (
    ("2026-08-21", 2070.0),
    ("2026-11-09", 1830.0),
)
#: Gauge uncertainty in litres for cm-precision readings (S3 end point and
#: intermediate readings); S1/S2 (full-to-full) are exact.
TANK_GAUGE_UNCERTAINTY_L = 300.0
#: maintainer-Override (2026-11-10, authoritative): the 5 "strict" days of the
#: Oct-2025 failure window are NOT total failures - per maintainer, every
#: day/night had one or two successful restarts.  They stay in the window
#: but are NOT excluded from burn time (and had 0 warm samples anyway ->
#: zero numeric effect).
OCT_2025_OVERRIDE_DAYS: frozenset[str] = frozenset(
    {"2025-10-13", "2025-10-14", "2025-10-15", "2025-10-16", "2025-10-17"}
)
#: New-burner era start (replacement burner installed 01.02.2026, same
#: brand/model; data check: 0 strict failure days since then).
ERA_START_DEFAULT = "2026-02-01"
#: KPI anchor segments (the only segments that feed any KPI):
#: (label, start, end, litres, exact, note).  S1/S2 are full-to-full and
#: exact; S3 runs from the 4500 L anchor to the cm-precision reading
#: 09.11.26 (1830 L) and is the only segment that carries the +/-300 L
#: gauge uncertainty.  S3a/S3b are the S3 sub-segments over the
#: intermediate reading 2070 L on 21.08.26 (consistency: 2430 + 240 =
#: 2670 = S3 total).
KPI_ANCHOR_SEGMENTS: tuple[tuple[str, str, str, float, bool, str], ...] = (
    (
        "S1",
        "2023-06-01",
        "2024-10-31",
        2945.0,
        True,
        "Voll->Voll (4500 L Anker), EXAKT; alter Brenner",
    ),
    (
        "S2",
        "2024-10-31",
        "2025-10-23",
        2947.0,
        True,
        "Voll->Voll (4500 L Anker), EXAKT; alter Brenner",
    ),
    (
        "S3",
        "2025-10-23",
        "2026-11-09",
        2670.0,
        False,
        "grob: Start = 4500 L exakt (Voll), Ende = 1830 L Zollstock (cm-Präzision, ±300 L)",
    ),
    (
        "S3a",
        "2025-10-23",
        "2026-08-21",
        2430.0,
        False,
        "S3-Sub-Segment (4500 - 2070), cm-Präzision, ±300 L",
    ),
    (
        "S3b",
        "2026-08-21",
        "2026-11-09",
        240.0,
        False,
        "S3-Sub-Segment / sauberes Mini-Segment (2070 - 1830), cm-Präzision, ±300 L",
    ),
)
#: Old-burner baseline = S1 + S2 (the two exact full-to-full segments of
#: the original burner, before the 01.02.2026 replacement).
OLD_BURNER_SEGMENT_LABELS: tuple[str, ...] = ("S1", "S2")
#: S3 sub-segment used as the "clean mini segment" of the era block.
MINI_SEGMENT_START, MINI_SEGMENT_END = "2026-08-21", "2026-11-09"
#: S3a end / S3b start (intermediate reading date) - split point for the
#: era "sub_split" scenario.
S3_SPLIT_DATE = "2026-08-21"


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------
def parse_date(s: str) -> date:
    """Parse an ISO ``YYYY-MM-DD`` string into a :class:`datetime.date`."""
    return date.fromisoformat(s)


def iso(d: date) -> str:
    """Format a :class:`datetime.date` as ISO ``YYYY-MM-DD``."""
    return d.isoformat()


def days_in_month(year: int, month: int) -> int:
    """Number of calendar days of the given month."""
    if month == 12:
        return (date(year + 1, 1, 1) - date(year, 12, 1)).days
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def month_range(year: int, month: int) -> tuple[date, date]:
    """Inclusive (first, last) calendar days of the given month."""
    return date(year, month, 1), date(year, month, days_in_month(year, month))


def month_span_days(start: date, end: date) -> int:
    """Inclusive number of days between two dates."""
    return (end - start).days + 1


def day_range(start: date, end: date) -> list[date]:
    """Inclusive list of days between start and end."""
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def season_id_for(d: date) -> tuple[int, int] | None:
    """Heating-season key (start_year, end_year) containing calendar day
    ``d``; None for off-season days (May .. September).

    Seasons run Oct 09 .. Apr 30 of the following year.
    """
    if d.month >= SEASON_START_MONTH:
        return d.year, d.year + 1
    if d.month <= SEASON_END_MONTH:
        return d.year - 1, d.year
    return None


def summer_season_for(d: date) -> tuple[int, int] | None:
    """Season that *follows* a summer day (May .. September -> (y, y+1)).

    Summer oil hot-water (O6) consumption is attributed to the upcoming
    heating season as context.
    """
    if d.month in SUMMER_MONTHS:
        return d.year, d.year + 1
    return None


def season_bounds(start_year: int) -> tuple[date, date]:
    """Inclusive season dates of the season starting in ``start_year``."""
    return date(start_year, SEASON_START_MONTH, SEASON_START_DAY), date(
        start_year + 1, SEASON_END_MONTH, SEASON_END_DAY
    )


def season_label(key: tuple[int, int]) -> str:
    """'2020/21' style label of a season key."""
    return f"{key[0]}/{str(key[1])[2:]}"


def clamp_to_window(a: date, b: date, start: date, end: date) -> tuple[date, date] | None:
    """Intersect [a, b] with the data window; None when no overlap."""
    lo = max(a, start)
    hi = min(b, end)
    if lo > hi:
        return None
    return lo, hi


def round1(x: float | None) -> float | None:
    """Round to 1 decimal (None-safe)."""
    return None if x is None else round(x, 1)


def round2(x: float | None) -> float | None:
    """Round to 2 decimals (None-safe)."""
    return None if x is None else round(x, 2)


def round3(x: float | None) -> float | None:
    """Round to 3 decimals (None-safe)."""
    return None if x is None else round(x, 3)


def pct_dev(value: float, base: float) -> float:
    """``value`` as percent deviation from ``base`` (0 = equal)."""
    if base == 0:
        return 0.0
    return round((value - base) / base * 100.0, 2)


def daily_hdd15(ext_avg: float) -> float:
    """Heating degree days (15 deg C base) for one daily mean temperature."""
    return max(0.0, HDD_BASE_DEG - ext_avg)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class FailureDay:
    """One failure-relevant day (burner demanded but (mostly) not igniting).

    ``kind`` = ``"strict"``: cold demand >= MIN_COLD_DEMAND_HOURS and daily
    a5 max < MAX_A5_FAILURE_DEG -> day is EXCLUDED from effective burn time.
    ``kind`` = ``"intermittent"``: same cold demand but the burner managed
    some ignition that day (a5 max up to MAX_A5_INTERMITTENT_DEG) -> reported
    inside the failure window, NOT excluded (its successful samples did burn).
    """

    day: str
    kind: str  #: "strict" | "intermittent"
    d5_hours: float  #: total demand hours (raw)
    cold_hours: float  #: cold-demand hours (d5=1 AND a5 < 30)
    a5_max: float
    a5_avg: float
    ext_avg: float
    overridden: bool = False  #: maintainer-Override (Okt-2025): not excluded


@dataclass(slots=True)
class FailureWindow:
    """A run of failure-relevant days.  The window spans all candidate days
    (strict + intermittent) with at most one gap day between two candidates
    (a day below the candidate floor can sit inside the window, e.g. one
    lucky ignition day with little cold demand).  A window needs at least
    one strict day to count as a real failure.  Excluded from burn time:
    the strict days only."""

    start: str
    end: str
    days: list[str] = field(default_factory=list)
    candidate_days: int = 0
    strict_days: int = 0
    intermittent_days: int = 0
    gap_days: int = 0
    total_cold_hours: float = 0.0
    total_d5_hours: float = 0.0
    excluded_cold_hours: float = 0.0  #: cold demand of the strict days
    ext_range: tuple[float, float] = (0.0, 0.0)
    overridden_days: int = 0
    note: str = ""  #: e.g. maintainer-Override windows without strict days


@dataclass(slots=True)
class TankMeasurement:
    """One tank-gauge row of ``tmp/tank_zollstock_2015_2026.csv``."""

    day: str
    height_mm: int
    litres: float
    delivery: bool
    delivery_qty: float | None  #: menge_l, only set for delivery rows


@dataclass(slots=True)
class TankSegment:
    """One consumption segment between two consecutive gauge readings.

    With the Q-127 tank model, ``KPI_ANCHOR_SEGMENTS`` (S1/S2/S3/S3a/S3b)
    are the ONLY segments feeding any KPI; the gauge-sequence segments of
    the raw CSV are reference history (pre-2023-06: ``usable=False``).
    """

    label: str = ""  #: anchor label S1/S2/S3/S3a/S3b ("" = gauge sequence)
    start: str = ""
    end: str = ""
    litres: float = 0.0  #: consumption in L (negative = volume INCREASE)
    days: int = 0
    usable: bool = False
    #: True for S1/S2/S3 (feed seasons/monthly/baseline); False for the S3
    #: sub-segments S3a/S3b (they overlap S3 and must NOT be attributed
    #: additively anywhere).
    top_level: bool = True
    note: str = "ok"
    ref_litres: float | None = None  #: litres per CSV reference block
    ref_days: int | None = None
    in_window: bool = False
    partial: bool = False  #: segment starts before the data window
    hours_eff: float | None = None
    hours_raw: float | None = None
    o6_hours: float | None = None  #: O6 (WW) hours inside the segment span
    l_per_h_eff: float | None = None  #: L per effective burn hour (primary)
    l_per_h_naive: float | None = None  #: naive L/h, only for partial segments
    corrected: bool = False  #: litres taken from the A4 balance, not raw gauge
    gauge: bool = False  #: cm-precision (non full-to-full) litres
    uncertainty_l: float = 0.0  #: gauge uncertainty in litres (+/-)
    monthly_estimated: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class DayAgg:
    """One day of daily aggregates (hours already converted from 10 s sample
    counts: 360 samples = 1 h).

    v0.6-D fields: ``oel_heiz_hours`` = effective burn hours EXCLUDING O6
    (WW) hours, ``l_hdd15`` = the daily HDD-15 value (also the value used
    for period sums - see :func:`daily_hdd15`), ``oel_heiz_share`` = the
    oil-heating hour share of the day (None when oil-heating and wood
    hours are both zero) and ``wood_primary`` = the wood-boiler was the
    primary heat source of the day (wood_h > oel_heiz_h, the oil burner
    barely ran).
    """

    day: str
    n_samples: int
    raw_hours: float
    eff_hours: float
    cold30_hours: float
    a5_max: float
    a5_avg: float
    ext_avg: float
    wood_hours: float
    o6_hours: float
    oel_heiz_hours: float = 0.0  #: effective h minus O6 (WW) h, floored at 0
    l_hdd15: float = 0.0  #: daily HDD-15 (deg C)
    oel_heiz_share: float | None = None  #: oel_heiz_h / (oel_heiz_h + wood_h)
    wood_primary: bool = False  #: wood carried the day (wood_h > oel_heiz_h)
    is_failure: bool = False


@dataclass(slots=True)
class OldBurnerBaseline:
    """Old-burner baseline L/h over the exact S1 + S2 segments (KPI anchor
    model, Q-127): litres, effective burn hours, L/h and O6 (WW) hours."""

    litres: float
    eff_hours: float
    l_per_h: float
    o6_hours: float
    o6_share_pct: float | None = None


@dataclass(slots=True)
class BurnerEra:
    """New-burner era block (01.02.2026 -> window end): burn hours, context
    and oil scenarios on the S3 anchor segment: ``s3_rate`` (S3 rate over
    the era span), ``sub_split`` (S3a rate over the era part until the S3
    split date + the S3b sub-segment litres), and the clean mini segment
    S3b 21.08.26 -> 09.11.26.  Both full-era scenarios carry the +/-300 L
    gauge-uncertainty band (conservative)."""

    start: str
    end: str
    eff_hours: float
    raw_hours: float
    o6_hours: float
    ext_avg: float
    strict_days: int  #: sanity check: must be 0 since 01.02.2026
    months: list[dict[str, object]] = field(default_factory=list)
    s3_rate: dict[str, object] = field(default_factory=dict)
    sub_split: dict[str, object] = field(default_factory=dict)
    mini_segment: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# DuckDB queries
# ---------------------------------------------------------------------------
def source_table_sql(globs: tuple[str, ...]) -> str:
    """SQL projecting the five needed channels from all Parquet sources."""
    patterns = ",".join(f"'{g}'" for g in globs)
    return (
        f"SELECT ts, {COL_DEMAND} AS d5, {COL_SUPPLY} AS a5, {COL_EXT} AS a8, "
        f"{COL_WOOD} AS a9, {COL_WW} AS o6 "
        f"FROM read_parquet([{patterns}])"
    )


def daily_sql(globs: tuple[str, ...], start: str, end: str, t_ign: float) -> str:
    """Daily aggregates: burn hours, cold demand, a5 stats, context sums."""
    return f"""
    WITH src AS ({source_table_sql(globs)})
    SELECT substr(ts, 1, 10) AS day,
           COUNT(*) AS n_samples,
           SUM(CASE WHEN d5 = 1 THEN 1 ELSE 0 END) AS raw_samples,
           SUM(CASE WHEN d5 = 1 AND a5 >= {t_ign} THEN 1 ELSE 0 END) AS eff_samples,
           SUM(CASE WHEN d5 = 1 AND a5 < 30.0 THEN 1 ELSE 0 END) AS cold30_samples,
           MAX(a5) AS a5_max,
           SUM(a5) / NULLIF(SUM(CASE WHEN a5 IS NOT NULL THEN 1 ELSE 0 END), 0) AS a5_avg,
           SUM(a8) / NULLIF(SUM(CASE WHEN a8 IS NOT NULL THEN 1 ELSE 0 END), 0) AS ext_avg,
           SUM(CASE WHEN a9 > {WOOD_ACTIVE_DEG} THEN 1 ELSE 0 END) AS wood_samples,
           SUM(CASE WHEN o6 = 1 THEN 1 ELSE 0 END) AS o6_samples,
           SUM(CASE WHEN d5 = 1 AND a5 >= {t_ign} AND o6 = 1 THEN 1 ELSE 0 END) AS o6_eff_samples
    FROM src
    WHERE ts >= '{start} 00:00:00' AND ts <= '{end} 23:59:59'
    GROUP BY 1
    ORDER BY 1
    """


def sensitivity_sql(globs: tuple[str, ...], start: str, end: str) -> str:
    """Total effective sample counts for every sensitivity threshold
    (one column per threshold, in SENSITIVITY_T_IGN order)."""
    terms = ", ".join(
        f"SUM(CASE WHEN d5 = 1 AND a5 >= {t} THEN 1 ELSE 0 END)" for t in SENSITIVITY_T_IGN
    )
    return f"""
    WITH src AS ({source_table_sql(globs)})
    SELECT {terms}
    FROM src
    WHERE ts >= '{start} 00:00:00' AND ts <= '{end} 23:59:59'
    """


def window_stats_sql(globs: tuple[str, ...], start: str, end: str, t_ign: float) -> str:
    """Total raw/effective samples and observed day count of the window."""
    return f"""
    WITH src AS ({source_table_sql(globs)})
    SELECT COUNT(*) AS total_samples,
           SUM(CASE WHEN d5 = 1 THEN 1 ELSE 0 END) AS raw_samples,
           SUM(CASE WHEN d5 = 1 AND a5 >= {t_ign} THEN 1 ELSE 0 END) AS eff_samples,
           COUNT(DISTINCT substr(ts, 1, 10)) AS days
    FROM src
    WHERE ts >= '{start} 00:00:00' AND ts <= '{end} 23:59:59'
    """


def sample_completeness_sql(globs: tuple[str, ...], start: str, end: str) -> str:
    """Per-month sample counts for the 8640/day completeness check."""
    return f"""
    WITH src AS ({source_table_sql(globs)})
    SELECT substr(ts, 1, 7) AS mon, COUNT(*) AS n
    FROM src
    WHERE ts >= '{start} 00:00:00' AND ts <= '{end} 23:59:59'
    GROUP BY 1 ORDER BY 1
    """


def frost_hint_sql(globs: tuple[str, ...], start: str, end: str) -> str:
    """Days with zero oil demand although very cold - wood-only context hint
    (NOT a failure: the spec distinguishes "kein Oel-Heizbedarf" from
    "Brenner defekt")."""
    return f"""
    WITH src AS ({source_table_sql(globs)})
    SELECT substr(ts, 1, 10) AS day, MAX(a5) AS a5_max,
           SUM(a8) / NULLIF(SUM(CASE WHEN a8 IS NOT NULL THEN 1 ELSE 0 END), 0) AS ext_avg
    FROM src
    WHERE ts >= '{start} 00:00:00' AND ts <= '{end} 23:59:59'
    GROUP BY 1
    HAVING SUM(CASE WHEN d5 = 1 THEN 1 ELSE 0 END) = 0
       AND MAX(a5) < {MAX_A5_FAILURE_DEG}
       AND SUM(a8) / NULLIF(SUM(CASE WHEN a8 IS NOT NULL THEN 1 ELSE 0 END), 0) < {FROST_HINT_EXT_DEG}
    ORDER BY 1
    """


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def make_connection() -> duckdb.DuckDBPyConnection:
    """Create a dedicated in-memory DuckDB connection."""
    return duckdb.connect(":memory:")


def load_daily(
    con: duckdb.DuckDBPyConnection,
    globs: tuple[str, ...],
    start: str,
    end: str,
    t_ign: float,
) -> list[DayAgg]:
    """Run the daily aggregation and convert sample counts to hours
    (10 s grid: SAMPLES_PER_DAY = 8640 -> 360 samples per hour)."""
    rows = con.execute(daily_sql(globs, start, end, t_ign)).fetchall()
    out: list[DayAgg] = []
    for day, n, raw, eff, cold30, a5_max, a5_avg, ext_avg, wood, o6, o6_eff in rows:
        per_hour = SAMPLES_PER_DAY / 24.0
        ext = float(ext_avg) if ext_avg is not None else 0.0
        wood_h = wood / per_hour
        o6_h = o6 / per_hour
        eff_h = eff / per_hour
        o6_eff_h = o6_eff / per_hour
        # v0.6-D: oil-heating hours = effective hours EXCLUDING the O6 (WW)
        # subset that is simultaneously effective (d5=1 AND a5>=T_ign AND
        # o6=1); o6_eff_h <= eff_h by construction.
        oel_heiz = max(0.0, eff_h - o6_eff_h)
        share = oel_heiz / (oel_heiz + wood_h) if (oel_heiz + wood_h) > 0 else None
        out.append(
            DayAgg(
                day=str(day),
                n_samples=int(n),
                raw_hours=raw / per_hour,
                eff_hours=eff_h,
                cold30_hours=cold30 / per_hour,
                a5_max=float(a5_max) if a5_max is not None else 0.0,
                a5_avg=float(a5_avg) if a5_avg is not None else 0.0,
                ext_avg=ext,
                wood_hours=wood_h,
                o6_hours=o6_h,
                oel_heiz_hours=oel_heiz,
                l_hdd15=daily_hdd15(ext),
                oel_heiz_share=share,
                wood_primary=wood_h > oel_heiz,
            )
        )
    return out


def load_sensitivity(
    con: duckdb.DuckDBPyConnection,
    globs: tuple[str, ...],
    start: str,
    end: str,
) -> dict[float, float]:
    """Effective hours per sensitivity threshold (10 s grid)."""
    row = con.execute(sensitivity_sql(globs, start, end)).fetchone()
    per_hour = SAMPLES_PER_DAY / 24.0
    return {t: float(n) / per_hour for t, n in zip(SENSITIVITY_T_IGN, row, strict=False)}


def load_window_stats(
    con: duckdb.DuckDBPyConnection,
    globs: tuple[str, ...],
    start: str,
    end: str,
    t_ign: float,
) -> dict[str, object]:
    """Window-level totals (samples, raw/effective hours, observed days)."""
    total, raw, eff, days = con.execute(window_stats_sql(globs, start, end, t_ign)).fetchone()
    per_hour = SAMPLES_PER_DAY / 24.0
    return {
        "total_samples": int(total),
        "raw_hours": raw / per_hour,
        "eff_hours": eff / per_hour,
        "observed_days": int(days),
    }


def load_completeness(
    con: duckdb.DuckDBPyConnection,
    globs: tuple[str, ...],
    start: str,
    end: str,
) -> dict[str, dict[str, object]]:
    """Per-month sample counts + full-month flag (completeness check)."""
    rows = con.execute(sample_completeness_sql(globs, start, end)).fetchall()
    win_start, win_end = parse_date(start), parse_date(end)
    out: dict[str, dict[str, object]] = {}
    for mon, n in rows:
        y, m = int(mon[:4]), int(mon[5:7])
        m_start, m_end = month_range(y, m)
        out[mon] = {
            "samples": int(n),
            "full_month": m_start >= win_start and m_end <= win_end,
        }
    return out


def load_frost_hints(
    con: duckdb.DuckDBPyConnection,
    globs: tuple[str, ...],
    start: str,
    end: str,
) -> list[dict[str, object]]:
    """Days with zero oil demand on very cold days (context, not failure)."""
    rows = con.execute(frost_hint_sql(globs, start, end)).fetchall()
    return [
        {"day": str(day), "a5_max": round1(float(a5max)), "ext_avg": round1(float(ext))}
        for day, a5max, ext in rows
    ]


# ---------------------------------------------------------------------------
# Tank gauge CSV
# ---------------------------------------------------------------------------
#: Tank-gauge CSV columns (fixed order; the file has NO header row - the
#: column names only appear inside the leading ``#`` comment block).  Rows
#: with a missing trailing field carry 7 instead of 8 columns; the first six
#: are always present and are the only ones this analysis uses.
TANK_CSV_COLUMNS: tuple[str, ...] = (
    "datum",
    "hoehe_mm",
    "ltr",
    "fehlmeng",
    "getankt",
    "menge_l",
)


def read_tank_csv(path: Path) -> list[TankMeasurement]:
    """Read the tank-gauge CSV (``;`` separated, ``#`` comment lines, no
    header row - the fixed column order :data:`TANK_CSV_COLUMNS` is used)."""
    out: list[TankMeasurement] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(";")
            if len(parts) < len(TANK_CSV_COLUMNS):
                raise ValueError(f"tank CSV: unexpected row: {line!r}")
            rec = dict(zip(TANK_CSV_COLUMNS, parts, strict=False))
            out.append(
                TankMeasurement(
                    day=rec["datum"],
                    height_mm=int(rec["hoehe_mm"]),
                    litres=float(rec["ltr"]),
                    delivery=rec.get("getankt", "").strip().upper() == "WAHR",
                    delivery_qty=(
                        float(rec["menge_l"]) if rec.get("menge_l", "").strip() else None
                    ),
                )
            )
    return out


def build_anchor_segments(start: str, end: str) -> list[TankSegment]:
    """Build the KPI anchor segments S1/S2/S3/S3a/S3b (Q-127 tank model).

    These are the ONLY segments that feed any KPI.  S1/S2 are exact
    full-to-full (4500 L anchors); S3 and its sub-segments S3a/S3b carry
    the cm-precision gauge uncertainty (``gauge=True``,
    ``uncertainty_l=TANK_GAUGE_UNCERTAINTY_L``).  ``days`` is the exclusive
    day difference (31.10.24 -> 23.10.25 = 357 d), consistent with the
    segment L/d rates used for season attribution and monthly
    interpolation.
    """
    segments: list[TankSegment] = []
    for label, s_day, e_day, litres, exact, note in KPI_ANCHOR_SEGMENTS:
        seg = TankSegment(
            label=label,
            start=s_day,
            end=e_day,
            litres=litres,
            days=(parse_date(e_day) - parse_date(s_day)).days,
            usable=True,
            note=note,
            gauge=not exact,
            uncertainty_l=0.0 if exact else TANK_GAUGE_UNCERTAINTY_L,
            top_level=label not in ("S3a", "S3b"),
        )
        if s_day < start:
            seg.partial = True
        seg.in_window = not (e_day < start or s_day > end)
        segments.append(seg)
    return segments


#: Gauge-sequence segment notes that survive the Q-127 model as
#: documentation (the segments themselves are NOT usable for any KPI).
HISTORY_SEGMENT_NOTES: dict[tuple[str, str], str] = {
    ("2025-10-19", "2025-10-23"): (
        "HISTORIE (nicht verwertbar); ANOMALIE A1 GELÖST: die 2945-L-Menge "
        "dieser Zeile gehört zur Lieferung 31.10.24 (Eintrags-Versatz) - "
        "keine Überfüllung; Fehleintrag/Rest, nur gelistet"
    ),
    ("2024-10-07", "2025-10-19"): (
        "HISTORIE (nicht verwertbar); ANOMALIE A2 GELÖST: „0 Verbrauch im "
        "Okt“ = ungeloggte 31.10.24-Lieferung (2945 L, Menge in der "
        "19.10.25-Zeile eingetragen) - selbe Ursache wie A1"
    ),
    ("2022-11-19", "2022-12-02"): (
        "HISTORIE (nicht verwertbar); A4-Kontext: 29 L/d = 4-10x über Normal "
        "(02.12.22-Bilanz, vor dem KPI-Fenster)"
    ),
}


def build_segments(
    meas: list[TankMeasurement], start: str, end: str
) -> tuple[list[TankSegment], dict[str, object]]:
    """Build the full segment list of the Q-127 tank model.

    Part 1: the KPI anchor segments (S1/S2/S3/S3a/S3b) - the only segments
    feeding any KPI.  Part 2: the raw gauge-sequence segments of the CSV
    rows, listed as reference history only ("Historie, nicht verwertbar").
    Every gauge-sequence segment starting before :data:`TANK_VALID_FROM`
    is ``usable=False`` with an explicit note (2021/2022 deliveries were
    never recorded); segments at/after the anchor start that fall outside
    the anchor canon (e.g. the odd 02.04.24 row) are flagged "außerhalb
    des Anker-Kanons".  The tank metadata dict carries the RESOLVED
    anomaly documentation (A1/A2) instead of the old open A1 variants.
    """
    segments = build_anchor_segments(start, end)
    for a, b in zip(meas, meas[1:], strict=False):
        seg = TankSegment(
            start=a.day,
            end=b.day,
            litres=round(a.litres - b.litres, 1),
            days=(parse_date(b.day) - parse_date(a.day)).days,
            usable=False,
        )
        key = (seg.start, seg.end)
        if key in HISTORY_SEGMENT_NOTES:
            seg.note = HISTORY_SEGMENT_NOTES[key]
        elif a.day < TANK_VALID_FROM:
            seg.note = (
                "HISTORIE (nicht verwertbar): ungelöste Lieferungen 2021/22 "
                "(maintainer, Q-127); vor TANK_VALID_FROM = 01.06.2023"
            )
        else:
            seg.note = (
                "HISTORIE (nicht verwertbar): außerhalb des Anker-Kanons "
                "(Zwischen-/Fehlmess-Zeile; ersetzt durch S1/S2/S3-Anker)"
            )
        if seg.litres < 0:
            seg.note = f"{seg.note} (NEGATIV: Zunahme ohne Lieferung -> Fehlmess-Kandidat)"
        if a.day < start:
            seg.partial = True
        seg.in_window = not (b.day < start or a.day > end)
        segments.append(seg)
    anomalies: dict[str, object] = {
        "a1": {
            "status": "gelöst (Q-127, maintainer 2026-11-10)",
            "explanation": (
                "Eintrags-Versatz: die 2945-L-Menge in der CSV-Zeile 19.10.25 "
                "gehört zur Lieferung 31.10.24 (Voll getankt -> 4500 L). "
                "KEINE Überfüllung - die 19.10.25-Zeile ist ein Fehleintrag/"
                "Rest und wird gelistet, aber NICHT verwertet. Die alten "
                "A1-Varianten A/B entfallen."
            ),
        },
        "a2": {
            "status": "gelöst (Q-127, maintainer 2026-11-10)",
            "explanation": (
                "„0 Verbrauch im Okt 2024“ (Referenz-Segment 07.10.24 -> "
                "19.10.25): ungeloggte 31.10.24-Lieferung (2945 L, Menge in "
                "die 19.10.25-Zeile eingetragen) - selbe Ursache wie A1."
            ),
        },
    }
    return segments, {"anomalies": anomalies, "tank_valid_from": TANK_VALID_FROM}


def enrich_segments(
    segments: list[TankSegment],
    daily: list[DayAgg],
    start: date,
    end: date,
) -> None:
    """Attach burn hours and L/h to segments in place.

    Partial segments (start before the data window): primary value is the
    pro-rata L/h (``litres x window_days/total_days`` over in-window burn
    hours); the naive L/h (full litres over in-window hours) is kept
    additionally and is BIAS-flagged (overestimate, early consumption
    missing).

    KPI anchor segments (labeled, Q-127) follow the spec window convention:
    the start day (delivery/measurement day) is EXCLUDED, the end day is
    INCLUDED (S2 31.10.24 -> 23.10.25 counts 01.11.24 .. 23.10.25, so the
    boundary days of adjacent segments are not double-counted).
    """
    daily_by_day = {d.day: d for d in daily}
    for seg in segments:
        lo, hi = parse_date(seg.start), parse_date(seg.end)
        if seg.label:
            lo += timedelta(days=1)  # anchor segments: exclusive start day
        overlap = clamp_to_window(lo, hi, start, end)
        if overlap is None:
            continue
        o_lo, o_hi = overlap
        hours_eff = 0.0
        hours_raw = 0.0
        o6_hours = 0.0
        for day in day_range(o_lo, o_hi):
            d = daily_by_day.get(day.isoformat())
            if d is not None:
                hours_eff += d.eff_hours
                hours_raw += d.raw_hours
                o6_hours += d.o6_hours
        seg.hours_eff = round2(hours_eff)
        seg.hours_raw = round2(hours_raw)
        seg.o6_hours = round2(o6_hours)
        if seg.partial and hours_eff > 0:
            total_days = month_span_days(lo, hi)
            overlap_days = month_span_days(o_lo, o_hi)
            seg.l_per_h_naive = round3(seg.litres / hours_eff)
        if hours_eff > 0 and seg.litres > 0 and seg.usable:
            if seg.partial:
                total_days = month_span_days(lo, hi)
                overlap_days = month_span_days(o_lo, o_hi)
                seg.l_per_h_eff = round3(seg.litres * (overlap_days / total_days) / hours_eff)
            else:
                seg.l_per_h_eff = round3(seg.litres / hours_eff)
        # monthly oil estimate for calendar months fully contained inside the
        # segment (segment [start, end] covers the whole month): rate x month
        # days, flagged "geschätzt" per task spec.  Sub-segments (S3a/S3b)
        # are excluded: they overlap S3 and would be attributed additively
        # (only S1/S2/S3 feed the monthly interpolation, Q-127).
        if seg.litres > 0 and seg.usable and seg.top_level:
            rate = seg.litres / max(seg.days, 1)
            y, m = parse_date(seg.start).year, parse_date(seg.start).month
            while date(y, m, 1) <= hi:
                m_start, m_end = month_range(y, m)
                if m_start >= lo and m_end <= hi:
                    seg.monthly_estimated.append(
                        {
                            "month": m_start.isoformat()[:7],
                            "litres": round(rate * days_in_month(y, m), 1),
                            "rate_l_per_day": round(rate, 2),
                            "estimated": True,
                        }
                    )
                if m == 12:
                    y, m = y + 1, 1
                else:
                    m += 1


# ---------------------------------------------------------------------------
# Burn-time and failure detection
# ---------------------------------------------------------------------------
def detect_failure_days(
    daily: list[DayAgg], apply_oct2025_override: bool = True
) -> list[FailureDay]:
    """Apply the two-tier failure-day rule (cold demand + a5 caps).

    * **strict** (excluded from effective burn time): cold demand >=
      MIN_COLD_DEMAND_HOURS and a5 max < MAX_A5_FAILURE_DEG (the spec's
      criterion).
    * **intermittent** (window member, NOT excluded): cold demand >=
      COLD_DEMAND_CANDIDATE_HOURS and a5 max <= MAX_A5_INTERMITTENT_DEG -
      the burner managed ignition that day ("manchmal hat es geklappt").

    maintainer-Override (2026-11-10, active unless
    ``apply_oct2025_override=False``): a strict day contained in
    :data:`OCT_2025_OVERRIDE_DAYS` is re-classified as *intermittent*
    (``overridden=True``) and therefore NOT excluded - per maintainer's
    testimony the Oct-2025 window had successful restarts every
    day/night, so it is not a total failure.
    """
    out: list[FailureDay] = []
    for d in daily:
        if d.cold30_hours < COLD_DEMAND_CANDIDATE_HOURS:
            continue
        if d.a5_max > MAX_A5_INTERMITTENT_DEG:
            continue
        is_strict = d.a5_max < MAX_A5_FAILURE_DEG and d.cold30_hours >= MIN_COLD_DEMAND_HOURS
        overridden = bool(apply_oct2025_override and d.day in OCT_2025_OVERRIDE_DAYS)
        kind = "intermittent" if (overridden or not is_strict) else "strict"
        out.append(
            FailureDay(
                day=d.day,
                kind=kind,
                d5_hours=round2(d.raw_hours) or 0.0,
                cold_hours=round2(d.cold30_hours) or 0.0,
                a5_max=round1(d.a5_max) or 0.0,
                a5_avg=round1(d.a5_avg) or 0.0,
                ext_avg=round1(d.ext_avg) or 0.0,
                overridden=overridden,
            )
        )
    return out


def mark_failure_days(daily: list[DayAgg], failures: list[FailureDay]) -> None:
    """Set ``is_failure`` on strict failure days (excluded from burn time)."""
    strict = {f.day for f in failures if f.kind == "strict"}
    for d in daily:
        d.is_failure = d.day in strict


def group_failure_windows(failures: list[FailureDay]) -> list[FailureWindow]:
    """Group candidate failure days (strict + intermittent) into windows.

    A window spans candidate days with at most one gap day between two
    candidates and counts as a real failure when it contains at least one
    strict day.  A window whose days are all maintainer-overridden (Okt-2025)
    carries no strict days and is listed with ``note`` set (nothing is
    excluded from the burn time).  Intermittent-only clusters without any
    strict or overridden day are dropped.  Days below the candidate floor
    can sit inside the window (e.g. one lucky ignition) - they bridge the
    window but are not themselves failure days.
    """
    if not failures:
        return []
    by_day = {f.day: f for f in failures}
    days = [parse_date(f.day) for f in failures]
    windows: list[list[date]] = [[days[0]]]
    for d in days[1:]:
        if (d - windows[-1][-1]).days <= 2:
            windows[-1].append(d)
        else:
            windows.append([d])
    out: list[FailureWindow] = []
    for w in windows:
        in_win = [by_day[iso(d)] for d in w]
        strict = [f for f in in_win if f.kind == "strict"]
        overridden = [f for f in in_win if f.overridden]
        if not strict and not overridden:
            # intermittent-only cluster: not a real failure and not an
            # override window -> drop it (unchanged behaviour)
            continue
        span = max(w) - min(w) + timedelta(days=1)
        gap = span.days - len(w)
        note = ""
        if not strict:
            note = (
                "kein strikter Tag (maintainer-Override Okt-2025, 2026-11-10): "
                "Fenster gelistet, nichts exkludiert"
            )
        out.append(
            FailureWindow(
                start=iso(w[0]),
                end=iso(w[-1]),
                days=[f.day for f in in_win],
                candidate_days=len(w),
                strict_days=len(strict),
                intermittent_days=len(w) - len(strict),
                gap_days=gap,
                total_cold_hours=round2(sum(f.cold_hours for f in in_win)) or 0.0,
                total_d5_hours=round2(sum(f.d5_hours for f in in_win)) or 0.0,
                excluded_cold_hours=round2(sum(f.cold_hours for f in strict)) or 0.0,
                ext_range=(
                    round1(min(f.ext_avg for f in in_win)) or 0.0,
                    round1(max(f.ext_avg for f in in_win)) or 0.0,
                ),
                overridden_days=len(overridden),
                note=note,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Aggregations over daily aggregates
# ---------------------------------------------------------------------------
def sum_hours(daily: list[DayAgg]) -> tuple[float, float]:
    """Total (raw, effective) hours; effective excludes failure days."""
    raw = sum(d.raw_hours for d in daily)
    eff = sum(d.eff_hours for d in daily if not d.is_failure)
    return raw, eff


def group_by_month(daily: list[DayAgg]) -> dict[str, dict[str, float]]:
    """Per calendar month: raw/effective hours + context columns (CSV data)."""
    out: dict[str, dict[str, float]] = {}
    for d in daily:
        mon = d.day[:7]
        rec = out.setdefault(
            mon,
            {
                "month": mon,
                "raw_h": 0.0,
                "eff_h": 0.0,
                "ausfall_tage": 0.0,
                "ext_sum": 0.0,
                "ext_days": 0.0,
                "hdd15": 0.0,
                "holzstunden": 0.0,
                "o6stunden": 0.0,
            },
        )
        rec["raw_h"] += d.raw_hours
        if d.is_failure:
            rec["ausfall_tage"] += 1
        else:
            rec["eff_h"] += d.eff_hours
        rec["ext_sum"] += d.ext_avg
        rec["ext_days"] += 1
        rec["hdd15"] += daily_hdd15(d.ext_avg)
        rec["holzstunden"] += d.wood_hours
        rec["o6stunden"] += d.o6_hours
    for rec in out.values():
        rec["ext_avg"] = round1(rec.pop("ext_sum") / max(rec.pop("ext_days"), 1))
        rec["hdd15"] = round1(rec["hdd15"])
        rec["raw_h"] = round2(rec["raw_h"])
        rec["eff_h"] = round2(rec["eff_h"])
        rec["holzstunden"] = round2(rec["holzstunden"])
        rec["o6stunden"] = round2(rec["o6stunden"])
    return out


def group_by_year(daily: list[DayAgg]) -> dict[str, dict[str, float]]:
    """Per calendar year: raw/effective hours."""
    out: dict[str, dict[str, float]] = {}
    for d in daily:
        rec = out.setdefault(d.day[:4], {"raw_h": 0.0, "eff_h": 0.0})
        rec["raw_h"] += d.raw_hours
        if not d.is_failure:
            rec["eff_h"] += d.eff_hours
    for rec in out.values():
        rec["raw_h"] = round2(rec["raw_h"])
        rec["eff_h"] = round2(rec["eff_h"])
    return out


def _blank_season_rec() -> dict[str, float]:
    """Zeroed season accumulator (see :func:`group_by_season`)."""
    return {
        "raw_h": 0.0,
        "eff_h": 0.0,
        "ext_sum": 0.0,
        "ext_days": 0.0,
        "hdd15": 0.0,
        "holz_h": 0.0,
        "o6_summer_h": 0.0,
    }


def group_by_season(daily: list[DayAgg]) -> dict[tuple[int, int], dict[str, float]]:
    """Per heating season: hours + context (ext mean, HDD-15, wood hours,
    O6 hours in the following summer).  Off-season days are not part of any
    season, except summer O6 hours (context, attributed to the season that
    follows the summer)."""
    out: dict[tuple[int, int], dict[str, float]] = {}
    for d in daily:
        day = parse_date(d.day)
        key = season_id_for(day)
        if key is not None:
            rec = out.setdefault(key, _blank_season_rec())
            rec["raw_h"] += d.raw_hours
            if not d.is_failure:
                rec["eff_h"] += d.eff_hours
            rec["ext_sum"] += d.ext_avg
            rec["ext_days"] += 1
            rec["hdd15"] += daily_hdd15(d.ext_avg)
            rec["holz_h"] += d.wood_hours
        s_key = summer_season_for(day)
        if s_key is not None:
            out.setdefault(s_key, _blank_season_rec())["o6_summer_h"] += d.o6_hours
    for rec in out.values():
        rec["ext_avg"] = round1(rec.pop("ext_sum") / max(rec.pop("ext_days"), 1))
        rec["hdd15"] = round2(rec["hdd15"])
        rec["raw_h"] = round2(rec["raw_h"])
        rec["eff_h"] = round2(rec["eff_h"])
        rec["holz_h"] = round2(rec["holz_h"])
        rec["o6_summer_h"] = round2(rec.pop("o6_summer_h"))
    return out


# ---------------------------------------------------------------------------
# Trend fit
# ---------------------------------------------------------------------------
def trend_fit(points: list[tuple[float, float]]) -> dict[str, object]:
    """Least-squares linear fit ``y = intercept + slope*x``.

    ``points`` are (x, y) pairs (x = season start date in days since a fixed
    epoch, y = L/h of the season).  Returns the slope in L/h per year, R^2,
    n and honest caveat metadata.
    """
    if len(points) < 2:
        return {"n": len(points), "slope_l_per_h_per_year": None, "r2": None}
    x = np.array([p[0] for p in points], dtype=float)
    y = np.array([p[1] for p in points], dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    pred = slope * x + intercept
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2: float | None = 1.0 - ss_res / ss_tot if ss_tot > 0 else None
    return {
        "n": int(len(points)),
        "slope_l_per_h_per_year": round3(float(slope) * 365.25),
        "intercept": round3(float(intercept)),
        "r2": r2,
    }


def build_trend(season_rows: list[dict[str, object]]) -> dict[str, object]:
    """Season L/h trend fits: PRIMARY = full seasons only, plus a secondary
    fit that also includes the running partial season.

    The primary fit is the KPI trend: only non-partial season rows
    (``is_partial=False``, i.e. tank coverage >= 200 days) participate.
    The partial season of the running year (e.g. 2026/27 with a handful of
    days and pro-rata interpolation) is not comparable to full seasons and
    would dominate the slope - it is reported separately as
    ``including_partial`` with an explanatory note.
    """
    epoch = date(2020, 9, 1).toordinal()

    def to_points(rows: list[dict[str, object]]) -> list[tuple[float, float]]:
        pts: list[tuple[float, float]] = []
        for r in rows:
            sy = int(r["key"][0])
            x = date(sy, SEASON_START_MONTH, SEASON_START_DAY).toordinal() - epoch
            pts.append((x, float(r["l_per_h"])))
        return pts

    full_rows = [r for r in season_rows if not r["is_partial"]]
    return {
        "primary": trend_fit(to_points(full_rows)),
        "including_partial": trend_fit(to_points(season_rows)),
        "note": (
            "primary = nur Voll-Saisons (Tank-Deckung >= 200 Tage); "
            "including_partial = zusätzlich die laufende Teil-Saison "
            "(nicht vergleichbar: pro-rata-Interpolation + kurzer Zeitraum)"
        ),
    }


def season_trend_rows(
    segments: list[TankSegment],
    season_hours: dict[tuple[int, int], dict[str, float]],
) -> list[dict[str, object]]:
    """Aggregate segment litres by heating season -> one L/h row per season.

    Only usable positive TOP-LEVEL segments count (S1/S2/S3); the S3
    sub-segments S3a/S3b overlap S3 and must NOT be attributed additively
    (they would count S3's consumption up to three times).  Segment
    consumption is spread uniformly over its days, so each season receives
    its day-share.  Summer (off-season) days do not belong to any season
    and are not attributed.
    """
    litres_by_season: dict[tuple[int, int], float] = {}
    covered: dict[tuple[int, int], int] = {}
    for seg in segments:
        if not seg.usable or not seg.top_level or seg.litres <= 0:
            continue
        rate = seg.litres / max(seg.days, 1)
        for day in day_range(parse_date(seg.start), parse_date(seg.end)):
            key = season_id_for(day)
            if key is None:
                continue
            litres_by_season[key] = litres_by_season.get(key, 0.0) + rate
            covered[key] = covered.get(key, 0) + 1
    rows: list[dict[str, object]] = []
    for key in sorted(litres_by_season):
        sh = season_hours.get(key)
        if not sh or sh["eff_h"] <= 0:
            continue
        lit = litres_by_season[key]
        lph = lit / sh["eff_h"]
        days = covered.get(key, 0)
        # Wood share is only meaningful up to 100 % of the oil effective
        # hours: when holz_h EXCEEDS eff_h, wood was the primary heat
        # source in the period (e.g. a record wood month while the oil
        # burner barely ran) and the share is not meaningful - report it
        # as None + explanatory note instead of a value > 100 %.
        if sh["holz_h"] > sh["eff_h"]:
            holz_share: float | None = None
            holz_note = "Holz-h > Öl-h (Holz primäre Wärmequelle im Zeitraum)"
        else:
            holz_share = round(100.0 * sh["holz_h"] / sh["eff_h"], 1)
            holz_note = ""
        rows.append(
            {
                "season": season_label(key),
                "key": list(key),
                "litres_season": round2(lit),
                "days_covered": days,
                "is_partial": days < 200,
                "eff_h": sh["eff_h"],
                "raw_h": sh["raw_h"],
                "l_per_h": round3(lph),
                "ext_avg": sh["ext_avg"],
                "hdd15": sh["hdd15"],
                "holz_h": sh["holz_h"],
                "holz_share_pct": holz_share,
                "holz_note": holz_note,
                "o6_summer_h": sh["o6_summer_h"],
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Old-burner baseline + new-burner era (01.02.2026 ->)
# ---------------------------------------------------------------------------
def compute_old_burner_baseline(
    segments: list[TankSegment], daily: list[DayAgg]
) -> OldBurnerBaseline | None:
    """L/h of the ORIGINAL burner over the exact S1 + S2 anchor segments.

    Uses the effective burn hours inside each segment span with the spec
    window convention (start day exclusive, end day inclusive - the delivery
    day itself is not part of the consumption span, so the boundary days of
    adjacent segments are not double-counted).  O6 (WW) hours are reported
    for the O6-share analysis.  Returns None when the anchor segments are
    missing.
    """
    by_label = {s.label: s for s in segments if s.label}
    parts = [by_label.get(lbl) for lbl in OLD_BURNER_SEGMENT_LABELS]
    if any(p is None for p in parts):
        return None
    eff = 0.0
    o6 = 0.0
    litres = 0.0
    for seg in parts:
        day_lo = parse_date(seg.start) + timedelta(days=1)
        for d in daily:
            if day_lo.isoformat() <= d.day <= seg.end:
                eff += d.eff_hours
                o6 += d.o6_hours
        litres += seg.litres
    if eff <= 0:
        return None
    return OldBurnerBaseline(
        litres=round2(litres),
        eff_hours=round2(eff),
        l_per_h=round3(litres / eff),
        o6_hours=round2(o6),
        o6_share_pct=round(100.0 * o6 / eff, 1),
    )


def _gauge_band(litres: float, hours: float) -> list[float]:
    """Conservative +/-TANK_GAUGE_UNCERTAINTY_L band on a L/h value.

    Lower bound is floored at 0 (consumption cannot be negative)."""
    lo = max(0.0, litres - TANK_GAUGE_UNCERTAINTY_L) / hours
    hi = (litres + TANK_GAUGE_UNCERTAINTY_L) / hours
    return [round2(lo), round2(hi)]


def _era_s3_rate(
    s3: TankSegment, era_start: date, era_end: date, eff_hours: float
) -> dict[str, object]:
    """Scenario 1: S3 segment rate (L/d) over the exclusive era span
    (``era_end - era_start`` days, e.g. 01.02.26 -> 09.11.26 = 281 d)."""
    rate = s3.litres / max(s3.days, 1)
    oil_days = (era_end - era_start).days
    litres = rate * oil_days
    return {
        "rate_l_per_day": round(rate, 4),
        "oil_days": oil_days,
        "litres": round2(litres),
        "l_per_h": round3(litres / eff_hours) if eff_hours > 0 else 0.0,
        "uncertainty": _gauge_band(litres, eff_hours) if eff_hours > 0 else [0.0, 0.0],
    }


def _era_sub_split(
    s3a: TankSegment | None,
    s3b: TankSegment | None,
    era_start: date,
    eff_hours: float,
) -> dict[str, object]:
    """Scenario 2: sub-split over the S3 intermediate reading 21.08.26 -
    S3a rate over the era part until the split date PLUS the S3b
    sub-segment litres (240 L, exact sub-segment value)."""
    if s3a is None or s3b is None or eff_hours <= 0:
        return {}
    split_days = (parse_date(S3_SPLIT_DATE) - era_start).days
    rate_a = s3a.litres / max(s3a.days, 1)
    litres = rate_a * split_days + s3b.litres
    return {
        "s3a_rate_l_per_day": round(rate_a, 4),
        "split_days": split_days,
        "s3b_litres": s3b.litres,
        "litres": round2(litres),
        "l_per_h": round3(litres / eff_hours),
        "uncertainty": _gauge_band(litres, eff_hours),
    }


def _era_minisegment(
    segments: list[TankSegment],
    daily: list[DayAgg],
    start_key: tuple[str, str],
) -> dict[str, object]:
    """The clean mini segment (21.08.26 -> 09.11.26, anchor S3b) with its
    L/h based on the effective hours *inside the segment* and the
    gauge-precision uncertainty band: up to +/-300 L on the segment span
    (gauge uncertainty +/-150-300 L per reading, applied to the segment,
    conservative; band floored at 0)."""
    seg = next((s for s in segments if (s.start, s.end) == start_key), None)
    if seg is None:
        return {}
    eff_hours = sum(d.eff_hours for d in daily if seg.start <= d.day <= seg.end)
    if eff_hours <= 0:
        return {}
    return {
        "start": seg.start,
        "end": seg.end,
        "litres": seg.litres,
        "days": seg.days,
        "eff_hours": round2(eff_hours),
        "l_per_h": round3(seg.litres / eff_hours),
        "uncertainty": _gauge_band(seg.litres, eff_hours),
    }


def compute_burner_era(
    daily: list[DayAgg],
    segments: list[TankSegment],
    era_start: str,
    end: str,
    failures: list[FailureDay] | None = None,
) -> BurnerEra | None:
    """Compute the new-burner era block (``era_start`` -> era end).

    Era end = min(window end, S3 end 09.11.26): the S3 anchor segment is the
    oil basis of the era, so the era stops at the last S3 reading even when
    the data window runs further (spec 2026-11-10: era = 01.02.26 ->
    09.11.26, 281 oil days).  Returns None when the era does not overlap the
    data window (no era days in ``daily``).  Includes burn hours (total +
    per month), O6 hours, outdoor average, the strict-day sanity count (0
    expected since 01.02.2026) and the three oil scenarios based on the S3
    anchor segment: ``s3_rate`` (S3 rate over the era span), ``sub_split``
    (S3a rate over 01.02 -> 21.08 + S3b litres) and the clean mini segment
    S3b 21.08.26 -> 09.11.26; both full-era scenarios carry the +/-300 L
    gauge-uncertainty band.
    """
    era_s, era_e = parse_date(era_start), parse_date(end)
    by_label = {s.label: s for s in segments if s.label}
    s3 = by_label.get("S3")
    s3a = by_label.get("S3a")
    s3b = by_label.get("S3b")
    era_end = era_e
    if s3 is not None:
        era_end = min(era_end, parse_date(s3.end))
    era_days = [d for d in daily if era_start <= d.day <= iso(era_end)]
    if not era_days:
        return None
    eff_hours = sum(d.eff_hours for d in era_days)
    raw_hours = sum(d.raw_hours for d in era_days)
    o6_hours = sum(d.o6_hours for d in era_days)
    ext_avg = sum(d.ext_avg for d in era_days) / len(era_days)
    era_set = {d.day for d in era_days}
    strict_days = 0
    if failures is not None:
        strict_days = sum(1 for f in failures if f.kind == "strict" and f.day in era_set)
    months: list[dict[str, object]] = []
    by_mon: dict[str, list[DayAgg]] = {}
    for d in era_days:
        by_mon.setdefault(d.day[:7], []).append(d)
    for mon in sorted(by_mon):
        ds = by_mon[mon]
        months.append(
            {
                "month": mon,
                "eff_hours": round2(sum(d.eff_hours for d in ds)),
                "o6_hours": round2(sum(d.o6_hours for d in ds)),
                "ext_avg": round1(sum(d.ext_avg for d in ds) / len(ds)),
            }
        )
    s3_rate: dict[str, object] = (
        _era_s3_rate(s3, era_s, era_end, eff_hours) if s3 is not None else {}
    )
    sub_split = _era_sub_split(s3a, s3b, era_s, eff_hours)
    return BurnerEra(
        start=era_start,
        end=iso(era_end),
        eff_hours=round2(eff_hours),
        raw_hours=round2(raw_hours),
        o6_hours=round2(o6_hours),
        ext_avg=round1(ext_avg),
        strict_days=strict_days,
        months=months,
        s3_rate=s3_rate,
        sub_split=sub_split,
        mini_segment=_era_minisegment(segments, daily, (MINI_SEGMENT_START, MINI_SEGMENT_END)),
    )


# ---------------------------------------------------------------------------
# v0.6-D: L/Heizgradtag (HDD-15), um Holz-Anteil korrigiert (Kompass-Metrik)
# ---------------------------------------------------------------------------
#: The mini (S3b) anchor label reported in the v0.6-D segment block.
MINI_SEGMENT_LABEL = "S3b"
#: Months with fewer than this many HDD-15 are not listed in the v0.6-D
#: month block: with near-zero heating demand the L/HDD ratio degenerates
#: (any WW/standby litres look like hundreds of L/HDD) and is not a
#: meaningful oil-efficiency statement.  The threshold cuts only the
#: summer/shoulder noise (2025-08: 0.1 HDD, 2026-06: 0.5 HDD); the coldest
#: summer month of the KPI window stays above it (2024-09: 30.5 HDD).
MIN_MONTH_HDD15 = 5.0


def _kpi_hours_range(daily: list[DayAgg], lo: date, hi: date) -> tuple[float, float]:
    """(oel_heiz_hours, hdd15) of days [lo, hi] (inclusive), both from the
    v0.6-D day fields (O6 already excluded from oel_heiz_hours)."""
    lo_s, hi_s = lo.isoformat(), hi.isoformat()
    days = [d for d in daily if lo_s <= d.day <= hi_s]
    return (
        sum(d.oel_heiz_hours for d in days),
        sum(d.l_hdd15 for d in days),
    )


def _kpi_wood_range(daily: list[DayAgg], lo: date, hi: date) -> tuple[float, int]:
    """(wood_hours, wood_primary_days) of days [lo, hi] (inclusive)."""
    lo_s, hi_s = lo.isoformat(), hi.isoformat()
    days = [d for d in daily if lo_s <= d.day <= hi_s]
    return sum(d.wood_hours for d in days), sum(1 for d in days if d.wood_primary)


def _kpi_gauge_band_hdd(
    litres: float, hours: float, uncertainty_l: float, oel_share: float | None
) -> list[float]:
    """Conservative +/-uncertainty_l band on a CORRECTED L/hdd15 value.

    Mirrors :func:`_gauge_band` but in HDD units and carries the wood-share
    correction through (divide by ``oel_share``; the share's own gauge
    uncertainty is NOT modelled).  Lower bound floored at 0.
    """
    if hours <= 0:
        return [0.0, 0.0]
    lo = max(0.0, litres - uncertainty_l) / hours
    hi = (litres + uncertainty_l) / hours
    if oel_share is not None and oel_share > 0:
        lo, hi = lo / oel_share, hi / oel_share
    return [round3(lo), round3(hi)]


def segment_kpi_rows(
    segments: list[TankSegment], daily: list[DayAgg]
) -> list[dict[str, object]]:
    """v0.6-D L/Heizgradtag (HDD-15) je KPI-Anker-Segment (S1/S2/S3/S3b-Mini).

    Only usable labelled segments with positive litres and an effective
    hour base > 0 are reported.  S3a is excluded (sub-segment overlapping
    S3 - Q-127); S3b is the clean mini segment.  Span convention: start
    day EXCLUSIVE, end day INCLUSIVE (Q-127 window convention, the same
    basis as the segment L/h).  The wood correction is the hours proxy
    (no energy balance), see the module docstring v0.6-D block.
    """
    out: list[dict[str, object]] = []
    for seg in segments:
        if not seg.label or not seg.usable or seg.litres <= 0 or seg.label == "S3a":
            continue
        lo = parse_date(seg.start) + timedelta(days=1)  # exclusive start day
        hi = parse_date(seg.end)
        oel_heiz, hdd = _kpi_hours_range(daily, lo, hi)
        wood_h, wood_days = _kpi_wood_range(daily, lo, hi)
        if oel_heiz <= 0 or hdd <= 0:
            continue
        oel_share = oel_heiz / (oel_heiz + wood_h) if (oel_heiz + wood_h) > 0 else None
        holz_share = wood_h / (oel_heiz + wood_h) if (oel_heiz + wood_h) > 0 else None
        kpi_raw = seg.litres / hdd
        # corrected = litres / (hdd15 * (1 - holz_share)) = lit / (hdd * oel_share)
        kpi_korr = seg.litres / (hdd * oel_share) if oel_share and oel_share > 0 else None
        out.append(
            {
                "label": seg.label,
                "mini": seg.label == MINI_SEGMENT_LABEL,
                "span": f"{seg.start}→{seg.end}",
                "days": seg.days,
                "litres": seg.litres,
                "gauge": seg.gauge,
                "uncertainty_l": seg.uncertainty_l,
                "geschätzt": seg.gauge,
                "oel_heiz_h": round2(oel_heiz),
                "wood_h": round2(wood_h),
                "wood_primary_days": wood_days,
                "hdd15": round2(hdd),
                "oel_heiz_share": round3(oel_share) if oel_share is not None else None,
                "holz_share_pct": round(100.0 * holz_share, 1) if holz_share is not None else None,
                "l_hdd15": round3(kpi_raw),
                "l_hdd15_korr": round3(kpi_korr) if kpi_korr is not None else None,
                "l_hdd15_korr_band": _kpi_gauge_band_hdd(
                    seg.litres, hdd, seg.uncertainty_l, oel_share
                )
                if seg.uncertainty_l > 0
                else None,
            }
        )
    return out


def season_kpi_rows(daily: list[DayAgg], segments: list[TankSegment]) -> list[dict[str, object]]:
    """v0.6-D L/Heizgradtag (HDD-15) je Heizsaison.

    Seasons with no usable tank coverage are skipped (Q-127: pre-2023-06
    is unusable).  Season litres = per-day rate of the covering top-level
    anchor segments (same day-share basis as :func:`season_trend_rows`).
    The season's HDD-15 and wood/oil-heating hours come from the v0.6-D
    day fields over the season span (Oct 09 .. Apr 30; a partial running
    season covers the observed days only and is flagged
    ``is_partial=True`` - comparable in spirit to the L/h season rows).
    """
    # per-season litres from top-level usable segments (day-share basis);
    # gauge_by_season tracks whether ANY contributing segment is gauge
    # (S3 = cm-precision +/-300 L) so the KPI row can carry the flag.
    litres_by_season: dict[tuple[int, int], float] = {}
    covered: dict[tuple[int, int], int] = {}
    gauge_by_season: dict[tuple[int, int], bool] = {}
    for seg in segments:
        if not seg.usable or not seg.top_level or seg.litres <= 0:
            continue
        rate = seg.litres / max(seg.days, 1)
        for day in day_range(parse_date(seg.start), parse_date(seg.end)):
            key = season_id_for(day)
            if key is None:
                continue
            litres_by_season[key] = litres_by_season.get(key, 0.0) + rate
            covered[key] = covered.get(key, 0) + 1
            if seg.gauge:
                gauge_by_season[key] = True
    out: list[dict[str, object]] = []
    for key in sorted(litres_by_season):
        s_lo, s_hi = season_bounds(key[0])
        oel_heiz, hdd = _kpi_hours_range(daily, s_lo, s_hi)
        wood_h, wood_days = _kpi_wood_range(daily, s_lo, s_hi)
        if hdd <= 0 or oel_heiz <= 0:
            continue
        lit = litres_by_season[key]
        days_covered = covered.get(key, 0)
        is_partial = days_covered < 200  # same flag basis as season_trend_rows
        oel_share = oel_heiz / (oel_heiz + wood_h) if (oel_heiz + wood_h) > 0 else None
        holz_share = wood_h / (oel_heiz + wood_h) if (oel_heiz + wood_h) > 0 else None
        out.append(
            {
                "season": season_label(key),
                "key": list(key),
                "litres_season": round2(lit),
                "days_covered": days_covered,
                "is_partial": is_partial,
                "oel_heiz_h": round2(oel_heiz),
                "wood_h": round2(wood_h),
                "wood_primary_days": wood_days,
                "hdd15": round2(hdd),
                "oel_heiz_share": round3(oel_share) if oel_share is not None else None,
                "holz_share_pct": round(100.0 * holz_share, 1) if holz_share is not None else None,
                "l_hdd15": round3(lit / hdd),
                "l_hdd15_korr": round3(lit / (hdd * oel_share))
                if oel_share
                and oel_share > 0
                else None,
                "gauge": gauge_by_season.get(key, False),
            }
        )
    return out


def month_kpi_rows(daily: list[DayAgg], segments: list[TankSegment]) -> list[dict[str, object]]:
    """v0.6-D L/Heizgradtag (HDD-15) je Kalendermonat.

    Month litres = **per-day interpolation** from the top-level usable
    anchor segments: for every calendar day the rates of all segments
    whose consumption span covers the day are summed (span convention:
    start day EXCLUSIVE, end day INCLUSIVE - the same basis as the
    segment L/h and the Q-127 boundary rule).  This keeps boundary
    months honest (e.g. 06/2023: only 30 of 31 days sit inside S1;
    10/2025: 23 days S2 + 8 days S3), unlike the older rate x full-month
    days estimate which would miss S3 days in 09/2026.  Days without any
    usable segment coverage (pre-2023-06, after the S3 end, or before the
    window) are flagged ``estimated=True`` (partial month estimate).
    Months with no usable coverage at all are not listed, and months with
    fewer than :data:`MIN_MONTH_HDD15` HDD-15 are skipped as degenerate
    (summer/shoulder noise - at 0.1 HDD any standby/WW litres look like
    hundreds of L/HDD, so the ratio is not an oil-efficiency statement).
    The wood share uses the same full-period definition as the segment and
    season blocks (``holz_share = wood_h / (wood_h + oel_heiz_h)``); the
    count of wood-primary days is reported as context.
    """
    by_month: dict[str, list[DayAgg]] = {}
    for d in daily:
        by_month.setdefault(d.day[:7], []).append(d)
    segs = [
        s
        for s in segments
        if s.top_level and s.usable and s.litres > 0
    ]
    out: list[dict[str, object]] = []
    for mon in sorted(by_month):
        days = by_month[mon]
        hdd = sum(d.l_hdd15 for d in days)
        # degenerate summer/shoulder months: with (nearly) no heating demand
        # the L/HDD ratio blows up to meaningless values (e.g. 2025-08 with
        # 0.1 HDD -> ~1800 L/HDD).  Below MIN_MONTH_HDD15 the month is
        # skipped (not an oil-efficiency statement).
        if hdd < MIN_MONTH_HDD15:
            continue
        lit = 0.0
        sources: set[str] = set()
        n_estimated = 0
        for day in days:
            covered = 0.0
            for seg in segs:
                if seg.start < day.day <= seg.end:
                    covered += seg.litres / max(seg.days, 1)
                    sources.add(seg.label)
            lit += covered
            if covered == 0.0:
                n_estimated += 1
        if lit <= 0 or not sources:
            continue
        # full-period hours (same definition as segment/season blocks):
        # oel_heiz_h EXCLUDES O6 (WW) hours by construction (day field).
        oel_h = sum(d.oel_heiz_hours for d in days)
        wood_h = sum(d.wood_hours for d in days)
        wood_primary_days = sum(1 for d in days if d.wood_primary)
        denom = oel_h + wood_h
        # wood share (0..1) - same definition everywhere; when wood carried
        # the heating (Okt-2026) the share approaches 1 and the corrected
        # KPI diverges, which is the intended "visibility" signal.
        holz_share = round3(wood_h / denom) if denom > 0 else None
        oel_share = round3(oel_h / denom) if denom > 0 else None
        kpi_raw = lit / hdd
        # corrected = litres / (hdd15 * (1 - holz_share)) = lit / (hdd * oel_share)
        kpi_korr = lit / (hdd * oel_share) if oel_share and oel_share > 0 else None
        out.append(
            {
                "month": mon,
                "litres": round2(lit),
                "estimated_days": n_estimated,
                "month_days": len(days),
                "estimated": n_estimated > 0,
                "sources": sorted(sources),
                "hdd15": round2(hdd),
                "oel_heiz_h": round2(oel_h),
                "wood_h": round2(wood_h),
                "wood_primary_days": wood_primary_days,
                "holz_share_pct": round(100.0 * holz_share, 1) if holz_share is not None else None,
                "oel_heiz_share": oel_share,
                "l_hdd15": round3(kpi_raw),
                "l_hdd15_korr": round3(kpi_korr) if kpi_korr is not None else None,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
MONTHLY_CSV_FIELDS: tuple[tuple[str, str], ...] = (
    ("month", "month"),
    ("roh_h", "raw_h"),
    ("effektive_h", "eff_h"),
    ("ausfall_tage", "ausfall_tage"),
    ("ext_avg", "ext_avg"),
    ("hdd15", "hdd15"),
    ("holzstunden", "holzstunden"),
    ("o6stunden", "o6stunden"),
)


def write_monthly_csv(path: Path, monthly: dict[str, dict[str, object]]) -> None:
    """Write ``burn_hours_monthly.csv`` (semicolons, ISO month keys)."""
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(";".join(h for h, _ in MONTHLY_CSV_FIELDS) + "\n")
        for mon in sorted(monthly):
            rec = monthly[mon]
            fh.write(";".join(str(rec.get(k, "")) for _, k in MONTHLY_CSV_FIELDS) + "\n")


def collect_monthly_estimates(segments: list[TankSegment]) -> list[dict[str, object]]:
    """Merge monthly oil estimates (segment rate x month days) over segments.

    Only top-level segments (S1/S2/S3) contribute: the S3 sub-segments
    overlap S3 and must not be attributed additively (Q-127)."""
    seen: dict[str, dict[str, object]] = {}
    for seg in segments:
        if not seg.top_level:
            continue
        for est in seg.monthly_estimated:
            mon = str(est["month"])
            rec = seen.setdefault(mon, {"month": mon, "litres": 0.0, "sources": []})
            rec["litres"] = round(float(rec["litres"]) + float(est["litres"]), 1)  # type: ignore[operator]
            rec["sources"].append(f"{seg.start}->{seg.end}")
    return [seen[mon] for mon in sorted(seen)]


def build_json_payload(
    start: str,
    end: str,
    t_ign: float,
    failures: list[FailureDay],
    windows: list[FailureWindow],
    segments: list[TankSegment],
    measurements: list[TankMeasurement],
    monthly: dict[str, dict[str, object]],
    yearly: dict[str, dict[str, float]],
    seasons: dict[tuple[int, int], dict[str, float]],
    season_rows: list[dict[str, object]],
    trend: dict[str, object],
    sensitivity: dict[float, float],
    window_stats: dict[str, object],
    completeness: dict[str, dict[str, object]],
    frost_hints: list[dict[str, object]],
    tank: dict[str, object],
    total_raw: float,
    total_eff: float,
    removed_raw: float,
    removed_eff: float,
    burner_era: BurnerEra | None = None,
    old_burner_baseline: OldBurnerBaseline | None = None,
    v06_kpi: dict[str, object] | None = None,
) -> dict[str, object]:
    """Assemble the machine-readable ``oel_kpi.json`` payload."""
    return {
        "meta": {
            "task": "OEL-KPI-1",
            "window": {"start": start, "end": end},
            "t_ign_deg": t_ign,
            "failure_rule": {
                "candidate_cold_hours_min": COLD_DEMAND_CANDIDATE_HOURS,
                "strict_cold_hours_min": MIN_COLD_DEMAND_HOURS,
                "a5_max_strict_deg": MAX_A5_FAILURE_DEG,
                "a5_max_intermittent_deg": MAX_A5_INTERMITTENT_DEG,
                "note": (
                    "2 Stufen: strikt = >= "
                    f"{MIN_COLD_DEMAND_HOURS:.0f} h Kalt-Anforderung (d5=1, "
                    "a5<30) UND a5-Max < 40 -> exkludiert aus der "
                    "Brenndauer; die Spec-Schwelle 4 h/Tag ist erreicht "
                    "(Ausfall-Tage: 3.1-24.0 h Kalt-Anforderung, "
                    "Normaltage <= 2.4 h). Intermittent (Kandidat >= "
                    f"{COLD_DEMAND_CANDIDATE_HOURS:.1f} h, a5-Max <= 78, "
                    "mal geglueckt) = Fenster-Tag, NICHT exkludiert. Siehe "
                    "docs/oel-kpi-brenndauer.md."
                ),
            },
            "sources": {
                "parquet_globs": list(DEFAULT_PARQUET_GLOBS),
                "tank_csv": "tmp/tank_zollstock_2015_2026.csv",
            },
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "totals": {
            "raw_hours": round2(total_raw),
            "effective_hours": round2(total_eff),
            "failure_days_candidate": len(failures),
            "failure_days_strict": sum(1 for f in failures if f.kind == "strict"),
            "failure_windows": len(windows),
            "failure_days_raw_hours": round2(removed_raw),
            "failure_days_effective_hours": round2(removed_eff),
        },
        "burn_hours": {
            "monthly": {m: monthly[m] for m in sorted(monthly)},
            "yearly": {y: yearly[y] for y in sorted(yearly)},
            "seasonly": {season_label(k): v for k, v in sorted(seasons.items())},
        },
        "failures": {
            "days": [asdict(f) for f in failures],
            "windows": [asdict(w) for w in windows],
            "frost_hints_no_demand": frost_hints,
        },
        "tank_measurements": [asdict(m) for m in measurements],
        "segments": [asdict(s) for s in segments],
        "old_burner_baseline": asdict(old_burner_baseline)
        if old_burner_baseline is not None
        else None,
        "oil": {
            "monthly_estimated": collect_monthly_estimates(segments),
            "anomalies": tank.get("anomalies"),
            "tank_valid_from": tank.get("tank_valid_from"),
        },
        "sensitivity_t_ign": {
            f"T_{int(t)}_deg": {
                "effective_hours": round2(h),
                "dev_vs_default_pct": pct_dev(h, sensitivity.get(t_ign, total_eff)),
            }
            for t, h in sorted(sensitivity.items())
        },
        "season_lph": season_rows,
        "trend": trend,
        "v06_kpi": v06_kpi,
        "window_stats": window_stats,
        "completeness": completeness,
        "burner_era": asdict(burner_era) if burner_era is not None else None,
    }


def write_json(path: Path, payload: dict[str, object]) -> None:
    """Write the machine-readable JSON payload."""
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
        fh.write("\n")


# ---------------------------------------------------------------------------
# Report (German, for maintainer)
# ---------------------------------------------------------------------------
def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Markdown table helper."""
    out = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    for r in rows:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def fmt_h(x: float | None) -> str:
    """Format hours (1 decimal, German thousands/decimal separators)."""
    if x is None:
        return "n/a"
    s = f"{x:,.1f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_num(x: float | None) -> str:
    """Format a plain number (1 decimal, German separators)."""
    if x is None:
        return "n/a"
    s = f"{x:,.1f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_l(x: float | None) -> str:
    """Format litres (1 decimal, German separators)."""
    return fmt_num(x)


def fmt_lph(x: float | None) -> str:
    """Format L/h (2 decimals, German decimal separator)."""
    return "n/a" if x is None else f"{x:.2f}".replace(".", ",")


def fmt_lhdd(x: float | None) -> str:
    """Format L/HDD-15 (2 decimals, German decimal separator)."""
    return "n/a" if x is None else f"{x:.2f}".replace(".", ",")


def build_report(
    start: str,
    end: str,
    t_ign: float,
    daily: list[DayAgg],
    failures: list[FailureDay],
    windows: list[FailureWindow],
    segments: list[TankSegment],
    monthly: dict[str, dict[str, object]],
    yearly: dict[str, dict[str, float]],
    seasons: dict[tuple[int, int], dict[str, float]],
    season_rows: list[dict[str, object]],
    trend: dict[str, object],
    sensitivity: dict[float, float],
    window_stats: dict[str, object],
    completeness: dict[str, dict[str, object]],
    frost_hints: list[dict[str, object]],
    total_raw: float,
    total_eff: float,
    removed_raw: float,
    removed_eff: float,
    burner_era: BurnerEra | None = None,
    old_burner_baseline: OldBurnerBaseline | None = None,
    v06_kpi: dict[str, object] | None = None,
) -> str:
    """Build the human-readable German report."""
    base_eff = sensitivity.get(t_ign, total_eff)
    p: list[str] = []
    p.append("# ÖL-KPI-1: Brenndauer × Ölverbrauch-Korrelation\n")
    p.append(
        f"*Fenster: {start} → {end} · T_ign = {t_ign:.0f} °C · "
        f"generiert: {time.strftime('%Y-%m-%d %H:%M:%S')}*\n"
    )

    p.append("## 1. Methodik (Kurzfassung)\n")
    p.append(
        "- **Brennstunde (Proxy):** 10-s-Sample zählt als „gebrannt“ IFF "
        "`dev0_digital_05 = 1` (Öl-Brenner-Anforderung O5) **und** "
        "`dev0_analog_05 ≥ 30 °C` (Öl-Vorlauf warm = Kessel brennt). "
        "Kalt-Start (a5 < 30 °C) und Nachglühen (d5 = 0, a5 warm) zählen nicht."
    )
    p.append(
        f"- **Ausfall-Tag (2 Stufen):** „Kalt-Anforderung“ = d5=1 und a5 < 30 °C. "
        f"**Strikt** (≥ {MIN_COLD_DEMAND_HOURS:.0f} h/Tag Kalt-Anforderung, die "
        f"Aufgaben-Schwelle, + a5-Max < {MAX_A5_FAILURE_DEG:.0f} °C): ganz aus der "
        f"Brenndauer entfernt. Die 4 h sind im Daten erreicht: Ausfall-Tage zeigen "
        f"3,1–24,0 h Kalt-Anforderung/Tag (nahezu durchgehende, nicht bediente "
        f"Anforderung), der stärkste Nicht-Ausfall-Tag bleibt bei 2,4 h "
        f"(28.11.2025). **Kandidat** ab "
        f"{COLD_DEMAND_CANDIDATE_HOURS:.1f} h (in die 2,4/3,1-h-Lücke kalibriert) "
        f"mit a5-Max ≤ {MAX_A5_INTERMITTENT_DEG:.0f} °C ist **intermittierend** "
        f"(„manchmal hat es geglückt“ → im Fenster gelistet, NICHT exkludiert: "
        f"geglückte Samples zählen). Beleg: docs/oel-kpi-brenndauer.md, § "
        f"Ausfall-Regel."
    )
    p.append(
        "- **Öl-Verbrauch (Q-127 Tank-Modell, 2026-11-10):** KPI-Kanon = "
        "explizite Anker-Segmente ab 01.06.2023 (Voll getankt = 4500 L): "
        "S1 01.06.23→31.10.24 = 2945 L exakt, S2 31.10.24→23.10.25 = 2947 L "
        "exakt, S3 23.10.25→09.11.26 = 2670 L (cm-Präzision, ±300 L) mit "
        "Sub-Segmenten S3a (→21.08.26 = 2430 L) / S3b (21.08.26→09.11.26 = "
        "240 L). Alles **vor 01.06.2023** (ungelöste 2021/22-Lieferungen) = "
        "NUR Referenz-Historie, in KEINEN KPI eingerechnet (keine Saisons, "
        "kein Trend, keine Monats-Interpolation). S1+S2 = Baseline des alten "
        "Brenners. Beleg: docs/oel-kpi-brenndauer.md § Tank-Segmente."
    )

    p.append("\n## 2. Kernzahlen\n")
    p.append(
        f"**Effektive Brenndauer gesamt: {fmt_h(total_eff)} h** "
        f"(roh d5=1: {fmt_h(total_raw)} h; Ausfall-Korrektur: −{fmt_h(removed_eff)} h "
        f"warm auf {sum(w.strict_days for w in windows)} strikten Ausfall-Tagen "
        f"({fmt_h(removed_raw)} h roh d5 an diesen Tagen))."
    )
    p.append("")
    p.append("### Effektiv-Brennstunden je Jahr")
    rows = [[y, fmt_h(yearly[y]["eff_h"]), fmt_h(yearly[y]["raw_h"])] for y in sorted(yearly)]
    p.append(_md_table(["Jahr", "effektiv h", "roh h"], rows))
    if old_burner_baseline is not None:
        ob = old_burner_baseline
        p.append(
            f"\n**Alter Brenner (S1+S2, 01.06.2023→23.10.2025): "
            f"{fmt_l(ob.litres)} L / {fmt_h(ob.eff_hours)} h = "
            f"**{fmt_lph(ob.l_per_h)} L/h — Baseline** (O6-Öl {fmt_h(ob.o6_hours)} h = "
            f"{fmt_num(ob.o6_share_pct)} % der Effektivzeit). Alle Segmente davor "
            f"(Saisons 2020/21–2022/23) entfallen aus den KPIs: ungelöste "
            f"2021/22-Lieferungen (Q-127) — der Trend-Fit läuft daher nur über "
            f"die 4 Saisons mit Tank-Deckung ab 06/2023."
        )

    p.append("\n### Effektiv-Brennstunden je Heizsaison (09.10.→30.04.)")
    rows = [
        [
            season_label(k),
            fmt_h(seasons[k]["eff_h"]),
            fmt_num(seasons[k]["hdd15"]),
            fmt_num(seasons[k]["ext_avg"]),
        ]
        for k in sorted(seasons)
    ]
    p.append(_md_table(["Saison", "effektiv h", "HDD-15", "Außen-Ø °C"], rows))

    p.append("\n## 3. Ausfall-Fenster (maintainer-Check!)\n")
    p.append(
        f"**{len(windows)} Ausfall-Fenster** mit {sum(w.strict_days for w in windows)} "
        f"„strikt“-Ausfall-Tagen (exkludiert) + {sum(w.intermittent_days for w in windows)} "
        f"„intermittierenden“ Kandidat-Tagen (Zündung geglückt, nicht exkludiert). "
        f"Regel: Kandidat ab {COLD_DEMAND_CANDIDATE_HOURS:.2f} h Kalt-Anforderung "
        f"(d5=1, a5<30 °C); strikt = ≥ {MIN_COLD_DEMAND_HOURS:.1f} h + a5-Max < "
        f"{MAX_A5_FAILURE_DEG:.0f} °C; intermittierend = a5-Max ≤ "
        f"{MAX_A5_INTERMITTENT_DEG:.0f} °C („manchmal hat es geglückt“):"
    )
    p.append("")
    rows = [
        [
            w.start,
            w.end,
            str(w.candidate_days),
            f"{w.strict_days} strikt",
            f"{w.intermittent_days} intermitt.",
            fmt_h(w.total_cold_hours),
            fmt_num(w.ext_range[0]),
            "–",
            fmt_num(w.ext_range[1]),
        ]
        for w in windows
    ]
    p.append(
        _md_table(
            [
                "Beginn",
                "Ende",
                "Kandidaten-Tage",
                "strikt",
                "intermitt.",
                "Kalt-h ges.",
                "Außen-Ø min °C",
                "",
                "Außen-Ø max °C",
            ],
            rows,
        )
    )
    for w in windows:
        if w.note:
            p.append(
                f"\n⚠ **{w.start}→{w.end} (maintainer-Override 2026-11-10):** {w.note} — "
                f"nach maintainer-Beleg tägliche erfolgreiche Neustarts („jeden Tag/Nacht "
                f"mindestens 1–2 Neustarts“); die Daten zeigen an den "
                f"{w.overridden_days} übergeleiteten Tagen a5-Max ≤ 22,2 °C, "
                f"daher **nichts exkludiert** (und 0 warme Samples = null Auswirkung "
                f"auf die eff. Brenndauer)."
            )
    p.append("")
    if failures:
        p.append("### Einzelne Ausfall-Tage (strikte Tage = exkludiert aus Brenndauer)")
        rows = [
            [
                f.day,
                f.kind,
                fmt_h(f.d5_hours),
                fmt_h(f.cold_hours),
                fmt_num(f.a5_max),
                fmt_num(f.a5_avg),
                fmt_num(f.ext_avg),
            ]
            for f in failures
        ]
        p.append(
            _md_table(
                ["Tag", "Typ", "d5-h (roh)", "Kalt-h", "a5-Max °C", "a5-Ø °C", "Außen-Ø °C"], rows
            )
        )
    else:
        p.append("_Keine Ausfall-Tage erkannt._")

    p.append("\n## 4. Öl-Verbrauch je Segment (L/eff. Brennstunde)\n")
    p.append(
        "**KPI-Anker-Segmente (Q-127, die einzigen verwertbaren Segmente — ab 01.06.2023):**\n"
    )
    rows: list[list[str]] = []
    for s in segments:
        if not s.label or not s.in_window:
            continue
        flag = "EXAKT (Voll→Voll)" if not s.gauge else f"grob (±{s.uncertainty_l:.0f} L)"
        band = ""
        if s.gauge and s.l_per_h_eff is not None and (s.hours_eff or 0) > 0:
            lo = max(0.0, s.litres - s.uncertainty_l) / (s.hours_eff or 1)
            hi = (s.litres + s.uncertainty_l) / (s.hours_eff or 1)
            band = f" [Band {fmt_lph(lo)}–{fmt_lph(hi)}]"
        o6 = fmt_h(s.o6_hours) if s.o6_hours is not None else "n/a"
        rows.append(
            [
                f"{s.label}: {s.start}→{s.end}",
                str(s.days),
                fmt_l(s.litres),
                fmt_h(s.hours_eff),
                f"**{fmt_lph(s.l_per_h_eff)}**{band}" if s.l_per_h_eff is not None else "n/a",
                o6,
                flag,
            ]
        )
    p.append(
        _md_table(
            ["Segment (Anker)", "Tage", "Liter", "eff. Brennh.", "L/h", "O6-h", "Präzision"],
            rows,
        )
    )
    p.append("")
    hist = [s for s in segments if not s.label and s.in_window]
    p.append(
        "**Historie (nicht verwertbar, Q-127):** Alle Zollstock-Messreihen-"
        "Segmente vor 01.06.2023 sind NUR Referenz — die 2021/22-Lieferungen "
        "wurden nicht notiert, daher kein KPI (keine Saisons, kein Trend, "
        "keine Monats-Interpolation). Die CSV-`fehlmeng`-Spalte ist "
        "Formel-Noise (4800 − ltr, auf allen Zeilen) und wird — wie "
        "`ltr_pro_tag_owner` — nicht verwertet.\n"
    )
    rows = [[f"{s.start}→{s.end}", str(s.days), fmt_l(s.litres), s.note] for s in hist]
    if rows:
        p.append(_md_table(["Segment (Historie)", "Tage", "Liter (roh)", "Notiz"], rows))
        p.append("")
    p.append(
        "**Anomalien GELÖST (Q-127, maintainer 2026-11-10):** "
        "**A1** = Eintrags-Versatz — die 2945-L-Menge in der CSV-Zeile "
        "19.10.25 gehört zur Lieferung **31.10.24** (Voll getankt → 4500 L); "
        "es gab **keine** Überfüllung, die 19.10.25-Zeile ist ein "
        "Fehleintrag/Rest (gelistet, NICHT verwertet). Die alten "
        "A1-Varianten A/B entfallen. "
        "**A2** („0 Verbrauch im Okt 2024“) = ungeloggte 31.10.24-Lieferung "
        "(selbe Ursache). **A4 / Zunahme-Segmente** liegen vor 06/2023 = "
        "außerhalb des KPI-Fensters (nicht verwertbar)."
    )

    p.append("\n### Öl-Verbrauch je Kalendermonat (Interpolation, „geschätzt“-Flag)\n")
    p.append(
        "_Monats-Öl = Segment-Ø-L/d × Tage des Monats, nur für Monate, die **voll** in einem "
        "Anker-Segment (S1/S2/S3, ab 01.06.2023) liegen; Monate über Segment-Grenzen/Lieferungen "
        "sind nicht gelistet. Vor 06/2023: keine Interpolation (Q-127). "
        "L/h (Monat) = geschätzte Liter / eff. Brennstunden des Monats._\n"
    )
    est_by_mon: dict[str, dict[str, object]] = {}
    for s in segments:
        for est in s.monthly_estimated:
            mon = str(est["month"])
            rec = est_by_mon.setdefault(mon, {"litres": 0.0, "sources": []})
            rec["litres"] = round(float(rec["litres"]) + float(est["litres"]), 1)
            rec["sources"].append(f"{s.start}→{s.end}")
    rows = []
    for mon in sorted(est_by_mon):
        rec = est_by_mon[mon]
        lph = None
        m_eff = monthly.get(mon)
        if m_eff and m_eff.get("eff_h") and float(m_eff["eff_h"]) > 0:
            lph = float(rec["litres"]) / float(m_eff["eff_h"])
        rows.append(
            [
                mon,
                fmt_l(float(rec["litres"])),
                "geschätzt",
                fmt_lph(lph),
                "; ".join(str(x) for x in rec["sources"]),
            ]
        )
    p.append(
        _md_table(["Monat", "Öl L (geschätzt)", "Status", "L/eff. h (Monat)", "Segment"], rows)
    )

    p.append("\n## 5. Trend: L/h je Heizsaison\n")
    rows = [
        [
            str(r["season"]),
            fmt_l(float(r["litres_season"])),
            fmt_h(float(r["eff_h"])),
            fmt_lph(float(r["l_per_h"])),
            fmt_num(float(r["ext_avg"])),
            fmt_num(float(r["hdd15"])),
            f"{float(r['holz_share_pct']):.0f} %"
            if r["holz_share_pct"] is not None
            else "— (Holz dominiert)",
        ]
        for r in season_rows
    ]
    p.append(
        _md_table(
            [
                "Saison",
                "Öl L (Saison, pro-rata)",
                "eff. h",
                "L/h",
                "Außen-Ø °C",
                "HDD-15",
                "Holz-Anteil",
            ],
            rows,
        )
    )
    for r in season_rows:
        if r["holz_note"]:
            p.append(
                f"- _{r['season']}: {r['holz_note']} — Holz-h {fmt_h(float(r['holz_h']))} "
                f"> Öl-Effektivzeit {fmt_h(float(r['eff_h']))} → Anteil nicht cappable, "
                f"als „—“ gelistet._"
            )
    p.append("")
    trend_primary = dict(trend.get("primary") or {})
    trend_full = dict(trend.get("including_partial") or {})
    if trend_primary.get("slope_l_per_h_per_year") is not None:
        slope = float(trend_primary["slope_l_per_h_per_year"])
        direction = "steigt" if slope > 0 else "fällt"
        p.append(
            f"**Trend-Fit (n = {trend_primary['n']} Voll-Saisons, Primary): "
            f"{slope:+.3f} L/h pro Jahr ({direction}, R² = {fmt_num(trend_primary['r2'])}).**"
        )
    else:
        p.append("**Trend-Fit (Primary):** nicht möglich (zu wenige Voll-Saisons mit L/h).")
    if trend_full.get("slope_l_per_h_per_year") is not None:
        p.append(
            f"Inkl. Teil-Saison (n = {trend_full['n']}): "
            f"{float(trend_full['slope_l_per_h_per_year']):+.3f} "
            f"L/h pro Jahr (R² = {fmt_num(trend_full['r2'])}) — dominiert von der nicht "
            f"vergleichbaren Teil-Saison (pro-rata-Interpolation + kurzer Zeitraum), "
            f"**kein Beleg** für Verhaltensänderung."
        )
    else:
        p.append("Inkl. Teil-Saison: nicht möglich (zu wenige Saisons mit L/h).")
    p.append(
        "\n_Honest-Nachweis: Primary = nur Voll-Saisons (Tank-Deckung >= 200 Tage, "
        "Q-127: nur Segmente ab 06/2023 verwertbar); Saisons unterscheiden sich in "
        "Außen-Temperatur, HDD-15, Holz-Anteil und WW-Nutzung (O6-Stunden im Sommer "
        "= Kontext). Höheres L/h in kalten Saisons / mit hohem WW-Anteil ist Physik, "
        "kein Drift. Ein sauberer Drift-Beleg würde eine HDD-normierte "
        "Vergleichsgröße brauchen (siehe Limitationen). Die laufende "
        "**Teil-Saison** ist kein Voll-Saison-Wert und wird aus dem Primary-Fit "
        "ausgeschlossen._\n"
    )

    p.append("\n## 6. Kontext je Monat\n")
    rows = [
        [
            m,
            fmt_h(float(monthly[m]["eff_h"])),
            fmt_num(monthly[m].get("ext_avg")),
            fmt_num(monthly[m].get("hdd15")),
            fmt_h(float(monthly[m]["holzstunden"])),
            fmt_h(float(monthly[m]["o6stunden"])),
            str(int(monthly[m]["ausfall_tage"])),
        ]
        for m in sorted(monthly)
    ]
    p.append(
        _md_table(
            [
                "Monat",
                "eff. h",
                "Außen-Ø °C",
                "HDD-15",
                "Holz-h (a9>65 °C)",
                "O6-h (WW-Öl)",
                "Ausfall-Tage",
            ],
            rows,
        )
    )

    p.append("\n## 7. Datenqualität & Limitationen\n")
    incomplete = [
        f"{mon} ({int(v['samples']) / SAMPLES_PER_DAY:,.0f} d)"
        for mon, v in sorted(completeness.items())
        if not v.get("full_month", True)
    ]
    p.append(
        f"- Zeitfenster: {start} → {end}, beobachtet {window_stats['observed_days']} Tage, "
        f"{int(window_stats['total_samples']):,} Samples. Teilmonate (inaktiv/unvollständig): "
        + (", ".join(incomplete) if incomplete else "keine")
        + "."
    )
    p.append(
        "- **2015 → 2020-08: keine Brennerdaten** (Timeseries startet 2020-08-30) → "
        "Korrelation erst ab 2020-09 möglich; Tank-Segmente davor sind unverwertbar für L/h."
    )
    p.append(
        "- **Tank-Modell (Q-127):** ±150–300 L Zollstock-Genauigkeit gilt NUR "
        "für S3-Ende (1830 L) und die Zwischen-Ablesungen 2070 L/1830 L "
        "(cm-Präzision); **S1/S2 sind exakt** (Voll→Voll, 4500-L-Anker). Die "
        "CSV-`fehlmeng`-Spalte ist Formel-Noise (4800 − ltr) und nicht "
        "verwertet. Alles vor 01.06.2023 = Referenz-Historie (ungelöste "
        "2021/22-Lieferungen), kein KPI."
    )
    p.append(
        "- **WW im d5 enthalten:** O5 = ODER(AnfHeiz, AnfWW) → Öl-Warmwasser-Zyklen zählen "
        "als Brenndauer (physikalisch korrekt: der Brenner läuft). Sommer-O6-Stunden sind "
        "deshalb Kontext, keine Exklusion."
    )
    p.append(
        "- **ON/OFF-Physik (F3):** Ölbrenner ohne Lastmodulation → kurze Zyklen; "
        "Rampen < 10 s / < T_ign sind vernachlässigbar."
    )
    p.append(
        "- **Sensitivität T_ign:** "
        + ", ".join(
            f"T={int(t)} °C: {fmt_h(h)} h ({pct_dev(h, base_eff):+} % vs. Default)"
            for t, h in sorted(sensitivity.items())
            if abs(t - t_ign) > 1e-6
        )
        + " (Brennstunden-Abschwankung = Vertrauensbereich der T_ign-Wahl)."
    )
    if frost_hints:
        p.append(
            "- **Frost-Tage ohne Öl-Anforderung** (a5-Max < 40 °C, Außen-Ø < "
            f"{FROST_HINT_EXT_DEG:.0f} °C, d5 nie an) = kein Öl-Heizbedarf (Holz?), **kein** "
            f"Ausfall: {', '.join(str(f['day']) for f in frost_hints)}."
        )
    p.append(
        "- **Ausfall-Regel (2 Stufen):** ≥ 4 h/Tag Kalt-Anforderung (Spec) + a5-Max < 40 °C "
        "= strikt (exkludiert); Kandidat ab 2,5 h (in die 2,4/3,1-h-Lücke zwischen "
        "Normal- und Ausfall-Tagen kalibriert) + a5-Max ≤ 78 °C = intermittierend "
        "(Fenster, nicht exkludiert). Beleg: docs/oel-kpi-brenndauer.md § "
        "Ausfall-Regel. Die Jan-2026- und Okt-2025-Fenster bleiben vollständig "
        "detektiert (T_ign = 30 °C robust: strikte Tage mit a5-Max 14–31 °C)."
    )

    p.append(
        f"\n## 8. Neubrenner-Era ({burner_era.start} → {burner_era.end})\n"
        if burner_era is not None
        else "\n## 8. Neubrenner-Era (01.02.2026 → …)\n"
    )
    if burner_era is None:
        p.append(
            "_Era nicht im Datenfenster (Era-Start nach Fenster-Ende oder keine "
            "Era-Tage in den Daten)._ "
            "Neuer Brenner (gleiche Marke/Modell) ab 01.02.2026 — Block fällt "
            "bei --end < 01.02.2026 bzw. fehlenden Era-Tagen weg."
        )
    else:
        p.append(
            f"Kompletter Ersatz-Brenner (gleiche Marke/Modell) ab 01.02.2026. "
            f"Effektive Brenndauer in der Era: **{fmt_h(burner_era.eff_hours)} h** "
            f"(roh {fmt_h(burner_era.raw_hours)} h), O6 (WW-Öl) {fmt_h(burner_era.o6_hours)} h, "
            f"Außen-Ø {fmt_num(burner_era.ext_avg)} °C. Sanity-Check: "
            f"{burner_era.strict_days} strikte Ausfall-Tage seit 01.02.2026 "
            f"({'erwartet 0 ✓' if burner_era.strict_days == 0 else '⚠ unerwartet — Daten prüfen'})."
        )
        p.append("")
        p.append("### Effektiv-Brennstunden je Monat (Era)")
        rows = [
            [
                m["month"],
                fmt_h(float(m["eff_hours"])),
                fmt_h(float(m["o6_hours"])),
                fmt_num(float(m["ext_avg"])),
            ]
            for m in burner_era.months
        ]
        p.append(_md_table(["Monat", "eff. h", "O6-h (WW-Öl)", "Außen-Ø °C"], rows))
        p.append("")
        p.append("### Öl-Verbrauch in der Era (drei Szenarien, Basis S3-Anker)")
        s3r = burner_era.s3_rate or {}
        sub = burner_era.sub_split or {}
        mini = burner_era.mini_segment or {}
        ob_ref = (
            f" (Baseline alter Brenner: {fmt_lph(old_burner_baseline.l_per_h)} L/h)"
            if old_burner_baseline is not None
            else ""
        )
        if s3r:
            band = s3r.get("uncertainty") or [None, None]
            p.append(
                f"1. **S3-Ø-Rate (grob):** {fmt_num(float(s3r.get('rate_l_per_day', 0.0)))} L/d "
                f"× {int(s3r.get('oil_days', 0))} d = "
                f"{fmt_l(float(s3r.get('litres', 0)))} L / {fmt_h(burner_era.eff_hours)} h = "
                f"**{fmt_lph(float(s3r.get('l_per_h', 0.0)))} L/h** "
                f"(±300 L Band: **{fmt_lph(float(band[0]))}–{fmt_lph(float(band[1]))} L/h**)."
            )
        if sub:
            band = sub.get("uncertainty") or [None, None]
            p.append(
                f"2. **Sub-Split über S3a/S3b:** S3a-Rate "
                f"{fmt_num(float(sub.get('s3a_rate_l_per_day', 0.0)))} L/d × "
                f"{int(sub.get('split_days', 0))} d (01.02.→21.08.) + S3b "
                f"{float(sub.get('s3b_litres', 0)):.0f} L = "
                f"{fmt_l(float(sub.get('litres', 0)))} L / {fmt_h(burner_era.eff_hours)} h = "
                f"**{fmt_lph(float(sub.get('l_per_h', 0.0)))} L/h** "
                f"(±300 L Band: **{fmt_lph(float(band[0]))}–{fmt_lph(float(band[1]))} L/h**)."
            )
        if mini:
            band = mini.get("uncertainty") or [None, None]
            mini_eff = float(mini.get("eff_hours", 0))
            alt_hi = (float(mini.get("litres", 0)) + 150.0) / mini_eff if mini_eff > 0 else 0.0
            p.append(
                f"3. **Sauberes Mini-Segment S3b {mini.get('start', 'n/a')} → "
                f"{mini.get('end', 'n/a')} (Zwischen-Ablesungen 2070 L → 1830 L):** "
                f"{fmt_l(float(mini.get('litres', 0)))} L / "
                f"{fmt_h(mini_eff)} h = "
                f"**{fmt_lph(float(mini.get('l_per_h', 0.0)))} L/h**. Unsicherheit "
                f"aus der Tank-Messgenauigkeit (±150–300 L pro Messung → "
                f"Segment-Band bis ±300 L, konservativ; typisch ±150 L → obere "
                f"Grenze {fmt_lph(alt_hi)} L/h): "
                f"**{fmt_lph(float(band[0]))}–{fmt_lph(float(band[1]))} L/h**."
            )
        mini_o6 = (
            sum(
                d.o6_hours
                for d in daily
                if mini.get("start")
                and mini.get("end")
                and f"{mini.get('start')}" <= d.day <= f"{mini.get('end')}"
            )
            if mini
            else 0.0
        )
        o6_lo = min(float(m["o6_hours"]) for m in burner_era.months)
        o6_hi = max(float(m["o6_hours"]) for m in burner_era.months)
        era_o6_share = (
            round(100.0 * burner_era.o6_hours / burner_era.eff_hours, 1)
            if burner_era.eff_hours > 0
            else 0.0
        )
        base_o6_share = old_burner_baseline.o6_share_pct if old_burner_baseline is not None else 0.0
        p.append(
            "\n**Einordnung (ehrlich):** "
            f"{fmt_lph(float(sub.get('l_per_h', 0.0)))}–"
            f"{fmt_lph(float(s3r.get('l_per_h', 0.0)))} L/h (Mini-Segment "
            f"{fmt_lph(float(mini.get('l_per_h', 0.0)))} L/h{ob_ref}) liegt **ca. 45–55 % über** der alten "
            "Baseline (S1+S2). **Primärer Erklärungskandidat: O6-Anteil in der "
            f"Effektivzeit — in der Era brennt der Ölbrenner zu {fmt_num(era_o6_share)} % "
            f"für Öl-Warmwasser (O6 {fmt_h(burner_era.o6_hours)} h / "
            f"{fmt_h(burner_era.eff_hours)} h; im alten Brenner: {fmt_num(base_o6_share)} %), "
            f"O6 {fmt_num(o6_lo)}–{fmt_num(o6_hi)} h/Monat, im Mini-Segment "
            f"{fmt_num(mini_o6)} h O6. Weitere Verdachtsmischung: mildere Witterung "
            f"(Außen-Ø Era {fmt_num(burner_era.ext_avg)} °C) und die grobe S3-Anker-"
            "Messung (±300 L). **Interpretation als „Brenner verbraucht mehr“ erst "
            "mit v0.6 (L/Heizgradtag, um Holz korrigiert)** — die explizite "
            "v0.6-Folge-Aufgabe — **erledigt mit v0.6-D: § v0.6 (unten)** "
            "(L/Heizgradtag, um Holz-Anteil korrigiert, Okt-2026-Beispiel)."
        )
    p.append("")

    p.extend(_build_v06_section(v06_kpi, daily, segments))
    return "\n".join(p)


def _build_v06_section(
    v06_kpi: dict[str, object] | None,
    daily: list[DayAgg],
    segments: list[TankSegment],
) -> list[str]:
    """Report section v0.6: L/Heizgradtag (HDD-15), um Holz-Anteil korrigiert."""
    p: list[str] = []
    p.append("\n## v0.6: L/Heizgradtag (HDD-15), um Holz-Anteil korrigiert\n")
    p.append(
        "**Kompass-Metrik (v0.6-D, maintainer 2026-11-09/12):** Der Öl-Verbrauch je "
        "Heizgradtag ist erst dann zwischen Saisons/Monaten vergleichbar, wenn "
        "der **Holz-Anteil der Wärmelieferung** sichtbar/korrigiert ist — der "
        "Holzkessel (a9 > 65 °C, Aktivitäts-Proxy) lieferte z. B. im Okt-2026 "
        "**176,8 h auf 13 Tagen** (Rekord, davor 0–5 aktive Tage/Monat) und "
        "übernahm einen großen Teil der Heizung. **Drei Werte immer "
        "nebeneinander:** (a) **roh** `L/HDD-15 = Liter / HDD-15` (HDD aus "
        "demselben Zeitraum, bestehende Tageslogik), (b) **Holz-Kontext** = "
        "Öl-Heizstunden-Anteil `oel_heiz_share = oel_heiz_h / "
        "(oel_heiz_h + wood_h)` mit **oel_heiz_h = eff. Brennstunden EXKL. O6 "
        "(WW-Stunden abgezogen)** und (c) **korrigiert** `L/HDD-15_korr = "
        "L / (HDD-15 × oel_heiz_share)` = „Liter pro Heizgradtag, den der "
        "Ölbrenner eigentlich selbst liefern musste“ (bei 50/50-Teilung also "
        "≈ doppelte Roh-KPI).\n"
    )
    p.append(
        "_**Limitation (ehrlich):** die Korrektur ist ein **Stunden-Proxy** "
        "(Holzkessel-Vorlauf a9 als Aktivitätssignal), **keine Wärme-Energie-"
        "bilanz** — ein „Holz-Wärmenäquivalent abziehen“ ist aus diesen Daten "
        "nicht verteidigbar. Der ±300-L-Band des S3/Tankmodells gilt auf die "
        "KPIs (S3 + Mini-Segment „geschätzt“-Flag); die Unsicherheit des "
        "**Holz-Shares selbst** (VL-Proxy, kein Wärmemessung) wird nicht "
        "modelliert._\n"
    )
    v06 = dict(v06_kpi or {})
    seg_rows = list(v06.get("segments") or [])
    p.append("### v0.6-A: L/Heizgradtag je Anker-Segment (Spanne: Start exkl., Ende inkl.)")
    if seg_rows:
        rows = []
        for r in seg_rows:
            band = ""
            if r.get("l_hdd15_korr_band"):
                lo, hi = r["l_hdd15_korr_band"]
                band = f" [{fmt_lhdd(float(lo))}–{fmt_lhdd(float(hi))}]"
            rows.append(
                [
                    str(r["label"]),
                    str(r["span"]),
                    fmt_l(float(r["litres"])),
                    "exakt" if not r.get("gauge") else f"grob (±{float(r.get('uncertainty_l', 0.0)):.0f} L, geschätzt)",
                    fmt_num(float(r["hdd15"])),
                    str(r.get("wood_primary_days", 0)),
                    fmt_h(float(r["oel_heiz_h"])),
                    fmt_h(float(r["wood_h"])),
                    fmt_num(r.get("holz_share_pct")),
                    f"**{fmt_lhdd(float(r['l_hdd15']))}**",
                    f"**{fmt_lhdd(r.get('l_hdd15_korr'))}**{band}",
                ]
            )
        p.append(
            _md_table(
                [
                    "Segment",
                    "Spanne",
                    "Liter",
                    "Präzision",
                    "HDD-15",
                    "Holz-prim. Tage",
                    "Öl-Heiz-h",
                    "Holz-h",
                    "Holz-Share %",
                    "L/HDD roh",
                    "L/HDD korr.",
                ],
                rows,
            )
        )
    else:
        p.append("_Keine Segmente im Fenster (keine verwertbare Basis)._\n")
    p.append("")
    seas = list(v06.get("seasons") or [])
    p.append("### v0.6-B: L/Heizgradtag je Heizsaison (09.10. → 30.04., Q-127: nur Saisons mit Tank-Deckung)")
    if seas:
        rows = []
        for r in seas:
            flag = " *(Teil-Saison, geschätzt)*" if r.get("is_partial") else ""
            flag += " *(S3-Teil: geschätzt, ±300 L)*" if r.get("gauge") else ""
            rows.append(
                [
                    f"{r['season']}{flag}",
                    fmt_l(float(r["litres_season"])),
                    fmt_num(float(r["hdd15"])),
                    str(r.get("wood_primary_days", 0)),
                    fmt_h(float(r["oel_heiz_h"])),
                    fmt_h(float(r["wood_h"])),
                    fmt_num(r.get("holz_share_pct")),
                    f"**{fmt_lhdd(float(r['l_hdd15']))}**",
                    f"**{fmt_lhdd(r.get('l_hdd15_korr'))}**",
                ]
            )
        p.append(
            _md_table(
                [
                    "Saison",
                    "Öl L (pro-rata)",
                    "HDD-15",
                    "Holz-prim. Tage",
                    "Öl-Heiz-h",
                    "Holz-h",
                    "Holz-Share %",
                    "L/HDD roh",
                    "L/HDD korr.",
                ],
                rows,
            )
        )
        p.append(
            "_n = Saisons = 4 (Q-127: Tank-Deckung ab 06/2023). Teil-Saison "
            "2026/27 = laufend (Okt→Nov 2026), pro-rata-Interpoliert — kein "
            "Voll-Saison-Wert. „Holz-prim. Tage“ = Tage mit Holz-h > Öl-Heiz-h "
            "(im Monat/Segment gesamt, nicht nur 13-tägige Aktivität)._ \n"
        )
    else:
        p.append("_Keine Saisons mit Tank-Deckung im Fenster._\n")
    p.append("")
    mons = list(v06.get("months") or [])
    if mons:
        p.append("### v0.6-C: L/Heizgradtag je Monat (Monats-Öl = Segment-Rate × Tage MIT verwertbarer Segment-Deckung)")
        p.append(
            "_Monats-Öl = **per-Tag-Interpolation** aus den Top-Level-Anker-"
            "Segmenten (S1/S2/S3, Q-127): je Kalendertag wird die Rate "
            "(L/d) aller Segmente, deren Verbrauchsspanne den Tag abdeckt "
            "(Start exkl./Ende inkl.), summiert — dadurch sind auch "
            "Grenz-Monate korrekt (z. B. 06/2023 nur 30 v. 31 Tagen in "
            f"S1, 10/2025 = 23 d S2 + 8 d S3; Monate **ohne** verwertbare "
            f"Segment-Deckung (vor 06/2023, nach dem S3-Ende) und Monate mit "
            f"< {MIN_MONTH_HDD15:.0f} HDD-15 (Sommer-/Schulter-Noise: bei "
            f"0,1 HDD würde jede Standby-Literzahl einen Unsinn-Wert von "
            f"Hunderten L/HDD ergeben) sind nicht gelistet). "
            f"„geschätzt“-Tage = Tage "
            "des Monats ohne Segment-Deckung (nur in der Deckungsspalte "
            "sichtbar; Liter beziehen sich auf die abgedeckten Tage). Der "
            "**Holz-Share** = wood_h / (wood_h + oel_heiz_h) über den "
            "gesamten Monat (0–100 %); „Holz-prim. Tage“ (Holz-h > "
            "Öl-Heiz-h) sind Kontext — in Okt-2026 (13 Tage) übernimmt Holz "
            "die Heizung, der Share → 76 % und die korrigierte KPI zeigt "
            "den hohen Öl-Lastanteil pro Heizgradtag._\n"
        )
        rows = []
        for r in mons:
            est = r.get("estimated_days") or 0
            mdays = r.get("month_days") or 0
            status = f"geschätzt ({est}/{mdays} d ohne Deckung)" if est else "voll abgedeckt"
            rows.append(
                [
                    str(r["month"]),
                    fmt_l(float(r["litres"])),
                    status,
                    fmt_num(float(r["hdd15"])),
                    str(r.get("wood_primary_days", 0)),
                    fmt_num(r.get("holz_share_pct")),
                    f"**{fmt_lhdd(float(r['l_hdd15']))}**",
                    f"**{fmt_lhdd(r.get('l_hdd15_korr'))}**",
                ]
            )
        p.append(
            _md_table(
                [
                    "Monat",
                    "Öl L (Interpolation)",
                    "Segment-Deckung",
                    "HDD-15",
                    "Holz-prim. Tage",
                    "Holz-Share %",
                    "L/HDD roh",
                    "L/HDD korr.",
                ],
                rows,
            )
        )
    p.append("")
    p.extend(_v06_october_example(daily, segments))
    return p


def _v06_october_example(daily: list[DayAgg], segments: list[TankSegment]) -> list[str]:
    """Okt-2026-Beispiel (Rekord-Holz-Monat) mit Vorjahres-Vergleich.

    Month litres use the same per-day segment attribution as
    :func:`month_kpi_rows` (Q-127: only top-level usable segments).
    """
    p: list[str] = []
    p.append("### v0.6-E: Beispiel Okt-2026 (Rekord-Holz-Monat) vs. Vorjahre\n")
    by_month: dict[str, list[DayAgg]] = {}
    for d in daily:
        by_month.setdefault(d.day[:7], []).append(d)
    segs = [
        s for s in segments if s.top_level and s.usable and s.litres > 0
    ]
    rows: list[str] = []
    for mon in ("2026-10", "2025-10", "2021-10"):
        days = by_month.get(mon, [])
        if not days:
            continue
        hdd = sum(d.l_hdd15 for d in days)
        ext_avg = sum(d.ext_avg for d in days) / len(days)
        wood_days = sum(1 for d in days if d.wood_hours > 0)
        wood_primary = sum(1 for d in days if d.wood_primary)
        wood_h = sum(d.wood_hours for d in days)
        oel_h = sum(d.oel_heiz_hours for d in days)
        lit = 0.0
        for day in days:
            for seg in segs:
                if seg.start < day.day <= seg.end:
                    lit += seg.litres / max(seg.days, 1)
        lit = round2(lit) if lit > 0 else None
        share = 100.0 * wood_h / (oel_h + wood_h) if (oel_h + wood_h) > 0 else None
        # round3 like month_kpi_rows so the example matches the month table
        oel_share = round3(oel_h / (oel_h + wood_h)) if (oel_h + wood_h) > 0 else None
        kpi_raw = lit / hdd if lit and hdd > 0 else None
        kpi_korr = (
            lit / (hdd * oel_share) if (lit and hdd > 0 and oel_share) else None
        )
        if lit is None:
            lit_s = "— (kein Tank-Anker-Segment, Q-127: kein KPI)"
        else:
            lit_s = f"{fmt_l(lit)} L (Interpolation aus Anker-Segment-Raten)"
        share_s = f"{share:.1f} %" if share is not None else "n/a"
        rows.append(
            f"- **{mon}:** {len(days)} Tage, Außen-Ø {ext_avg:.1f} °C, "
            f"**HDD-15 = {hdd:.0f}** · Holz-aktive Tage **{wood_days}** "
            f"(Holz-primär: {wood_primary}), Holz-h {wood_h:.1f} h · "
            f"Öl-Heiz-h {oel_h:.1f} h · Holz-Share {share_s} · "
            f"Öl {lit_s} → **L/HDD-15 roh {fmt_lhdd(kpi_raw)}** / "
            f"**korrigiert {fmt_lhdd(kpi_korr)}**"
        )
    if rows:
        p.extend(rows)
        p.append(
            "\n**Lesart (Okt 2026 vs. Vorjahre):** Okt-2026 hatte **weniger** "
            "HDD (milder) UND einen **Rekord-Holz-Anteil** (13 Holz-primäre "
            "Tage, 176,8 Holz-h; davor 4 bzw. 8 Holz-aktive Tage/Monat). "
            "Ohne Korrektur wäre die Roh-KPI (L/HDD) für Okt-2026 verzerrt "
            "tief, weil ein großer Teil der Heizlast vom Holzkessel "
            "übernommen wurde. Der **korrigierte** Wert (÷ Öl-Heiz-Anteil, "
            "d. h. × 1/(1 − Holz-Share)) zeigt den eigentlichen "
            "„Öl-Leistungsdruck“ pro Heizgradtag: bei Okt-2026 deutlich "
            "höher als 2025/2021, obwohl die Roh-Literzahl wegen milderer "
            "Witterung und Holz-Ersatz niedriger liegt. "
            "2021-10 liegt vor dem KPI-Fenster (Q-127: kein Anker-Segment) "
            "→ Kontext nur, kein KPI.\n"
        )
    else:
        p.append("_Keine Oktober-Monate im Datenfenster._\n")
    return p


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        prog="analysis.oil_burner_kpi",
        description="Oil-burner KPI: effective burn time vs. tank-gauge oil consumption.",
    )
    parser.add_argument(
        "--out",
        default="tmp/oel_kpi",
        help="output directory for oel_kpi.json / burn_hours_monthly.csv / report.md",
    )
    parser.add_argument("--start", default="2020-09-01", help="window start (ISO date)")
    parser.add_argument("--end", default="2026-11-10", help="window end (ISO date, inclusive)")
    parser.add_argument(
        "--t-ign",
        type=float,
        default=DEFAULT_T_IGN_DEG,
        help=f"ignition threshold in deg C (default {DEFAULT_T_IGN_DEG})",
    )
    parser.add_argument(
        "--tank-csv",
        default="tmp/tank_zollstock_2015_2026.csv",
        help="path to the tank-gauge CSV",
    )
    parser.add_argument(
        "--no-oct2025-override",
        action="store_true",
        default=False,
        help=(
            "disable the maintainer-Override for the Oct-2025 window (default: override "
            "ACTIVE - the 5 days 13.-17.10.2025 stay in the window but are NOT "
            "excluded from the burn time)"
        ),
    )
    parser.add_argument(
        "--era-start",
        default=ERA_START_DEFAULT,
        help=f"new-burner era start, ISO date (default {ERA_START_DEFAULT})",
    )
    return parser.parse_args(argv)


def run(
    start: str,
    end: str,
    t_ign: float,
    out_dir: Path,
    tank_csv: Path,
    apply_oct2025_override: bool = True,
    era_start: str = ERA_START_DEFAULT,
) -> dict[str, object]:
    """Execute the full analysis and write all artifacts.

    Returns the JSON payload (also written to ``out_dir/oel_kpi.json``).
    """
    t0 = time.time()
    con = make_connection()
    try:
        daily = load_daily(con, DEFAULT_PARQUET_GLOBS, start, end, t_ign)
        if not daily:
            raise RuntimeError("no daily data in window - check globs/start/end")
        window_stats = load_window_stats(con, DEFAULT_PARQUET_GLOBS, start, end, t_ign)
        sensitivity = load_sensitivity(con, DEFAULT_PARQUET_GLOBS, start, end)
        completeness = load_completeness(con, DEFAULT_PARQUET_GLOBS, start, end)
        frost_hints = load_frost_hints(con, DEFAULT_PARQUET_GLOBS, start, end)
    finally:
        con.close()

    daily.sort(key=lambda d: d.day)
    failures = detect_failure_days(daily, apply_oct2025_override=apply_oct2025_override)
    mark_failure_days(daily, failures)
    windows = group_failure_windows(failures)

    total_raw, total_eff = sum_hours(daily)
    removed_raw = sum(d.raw_hours for d in daily if d.is_failure)
    removed_eff = sum(d.eff_hours for d in daily if d.is_failure)
    monthly = group_by_month(daily)
    yearly = group_by_year(daily)
    seasons = group_by_season(daily)

    measurements = read_tank_csv(tank_csv)
    segments, tank = build_segments(measurements, start, end)
    enrich_segments(segments, daily, parse_date(start), parse_date(end))

    season_rows = season_trend_rows(segments, seasons)
    trend = build_trend(season_rows)

    era = compute_burner_era(daily, segments, era_start, end, failures=failures)
    baseline = compute_old_burner_baseline(segments, daily)

    # v0.6-D: L/Heizgradtag (HDD-15) roh + holz-korrigiert, je Segment /
    # Saison / Monat (Stunden-Proxy, keine Energiebilanz - see module docs)
    v06 = {
        "method": {
            "kpi": "L / HDD-15 (HDD-15 = heating degree days, 15 deg C base)",
            "correction": (
                "oel_heiz_share = oel_heiz_h / (oel_heiz_h + wood_h) with "
                "oel_heiz_h = effective burn hours EXCLUDING O6 (WW) hours; "
                "L/hdd15_korr = L / (hdd15 * oel_heiz_share). Hours-proxy, "
                "NOT a wood energy balance."
            ),
            "wood_active_proxy_deg": WOOD_ACTIVE_DEG,
            "gauge_band_on_kpi": "TANK_GAUGE_UNCERTAINTY_L on segment litres",
        },
        "segments": segment_kpi_rows(segments, daily),
        "seasons": season_kpi_rows(daily, segments),
        "months": month_kpi_rows(daily, segments),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    write_monthly_csv(out_dir / "burn_hours_monthly.csv", monthly)

    payload = build_json_payload(
        start=start,
        end=end,
        t_ign=t_ign,
        failures=failures,
        windows=windows,
        segments=segments,
        measurements=measurements,
        monthly=monthly,
        yearly=yearly,
        seasons=seasons,
        season_rows=season_rows,
        trend=trend,
        sensitivity=sensitivity,
        window_stats=window_stats,
        completeness=completeness,
        frost_hints=frost_hints,
        tank=tank,
        total_raw=total_raw,
        total_eff=total_eff,
        removed_raw=removed_raw,
        removed_eff=removed_eff,
        burner_era=era,
        old_burner_baseline=baseline,
        v06_kpi=v06,
    )
    write_json(out_dir / "oel_kpi.json", payload)
    report = build_report(
        start=start,
        end=end,
        t_ign=t_ign,
        daily=daily,
        failures=failures,
        windows=windows,
        segments=segments,
        monthly=monthly,
        yearly=yearly,
        seasons=seasons,
        season_rows=season_rows,
        trend=trend,
        sensitivity=sensitivity,
        window_stats=window_stats,
        completeness=completeness,
        frost_hints=frost_hints,
        total_raw=total_raw,
        total_eff=total_eff,
        removed_raw=removed_raw,
        removed_eff=removed_eff,
        burner_era=era,
        old_burner_baseline=baseline,
        v06_kpi=v06,
    )
    (out_dir / "report.md").write_text(report, encoding="utf-8")

    print(
        f"oel_kpi: window {start}..{end}, days={window_stats['observed_days']}, "
        f"eff_h={total_eff:.1f}, failure_windows={len(windows)} "
        f"(strict={sum(w.strict_days for w in windows)}, "
        f"candidate={sum(w.candidate_days for w in windows)}), "
        f"runtime={time.time() - t0:.1f}s",
        file=sys.stderr,
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)
    out_dir = Path(args.out)
    tank = Path(args.tank_csv)
    if not tank.is_absolute():
        tank = REPO_ROOT / tank
    if not tank.exists():
        print(f"error: tank CSV not found: {tank}", file=sys.stderr)
        return 2
    payload = run(
        args.start,
        args.end,
        args.t_ign,
        out_dir,
        tank,
        apply_oct2025_override=not args.no_oct2025_override,
        era_start=args.era_start,
    )
    totals = payload["totals"]
    print(
        f"oel_kpi: wrote {out_dir}/oel_kpi.json, burn_hours_monthly.csv, report.md; "
        f"effective_hours={totals['effective_hours']}, raw_hours={totals['raw_hours']}, "
        f"failure_windows={totals['failure_windows']} "
        f"(strict_days={totals['failure_days_strict']}, "
        f"candidate_days={totals['failure_days_candidate']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
