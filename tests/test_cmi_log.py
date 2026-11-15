"""Tests for the CMI /LOG decoder + poller (S4N-LogPoll).

* **Rosetta**: the decoder is cross-checked against the WinSol-LogX ground
  truth (:mod:`analysis.winsol_logx`) on the three recorded days
  (``discovery/cmi-log/cmi_raw_*.log`` vs ``logx_*.log``): record-0 skip,
  10-s ts grid and per-channel value agreement.
* **Index parsing**: the real ``info2026.log`` + a synthetic multi-file index.
* **Poller DoD (a)-(d)**: driven against ``httpx.MockTransport`` only --
  never the real CMI, no live clear.log.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import struct
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from app.cmi.cmi_log import (
    RECORD_PERIOD,
    CMILogClient,
    CmiLogFile,
    decode_cmi_log,
    download_data_file,
    fetch_cmi_log_files,
    fetch_log_index,
    parse_log_index,
)
from app.cmi.exceptions import CMIAuthError, CmiLogError
from app.db import now_iso
from app.main import Gateway
from tests.conftest import DISCOVERY

CMILog_DIR = DISCOVERY / "cmi-log"
YEAR = 2026

#: Channels whose CMI-vs-LogX raw counts deviate by 1-2 counts on a
#: sub-second sampling-phase (fast counters / minute counters / collector
#: temps / Minderer 0-10 V valves).  Not mapping errors -- documented in
#: docs/cmi-log-protocol.md.  dev2 a10-a12 (Minderer Keller/EG/OG) join the
#: dev2 a16 sampling-phase class (S4N-LogFull): their 1-2 mismatches per day
#: are sub-second phase offsets, never mapping errors.
WEAK_CHANNELS: frozenset[tuple[int, str]] = frozenset(
    {
        (1, "a4"),
        (1, "a6"),
        (1, "a9"),
        (1, "a11"),
        (1, "a12"),
        (1, "a13"),
        (2, "a1"),
        (2, "a2"),
        (2, "a3"),
        (2, "a13"),
        (2, "a14"),
        (2, "a15"),
        (2, "a16"),
        (2, "a10"),
        (2, "a11"),
        (2, "a12"),  # Minderer 0-10 V, sampling phase
    }
)

#: Channels that are deliberately NOT value-identical to the LogX canon
#: (S4N-LogFull): the CMI record holds a constant 0 in these structurally
#: mapped slots while the LogX canon holds a different constant.  They are
#: filled with the CMI value (documented decision, no NULL) and proven by
#: :func:`test_constant_fill_channels_read_their_slots` instead of the
#: value-identity thresholds in :func:`test_rosetta_full_alignment`:
#:   * dev1 a10: LogX canon = constant -1.0 (0xFFFF), CMI slot 9 = 0.0
#:   * dev1 a14-a16: LogX canon = 0, CMI slots 13-15 = 0.0 (match, but kept
#:     out of the threshold set for symmetry: their proof is slot-constant,
#:     not value-identity over a varying signal)
DIVERGENT_CHANNELS: frozenset[tuple[int, str]] = frozenset(
    {(1, "a10"), (1, "a14"), (1, "a15"), (1, "a16")}
)

#: Measured ground-truth expectations per day (record 0 is the
#: header-overlap record that MUST be skipped).
DAYS: dict[str, dict] = {
    "2026-10-27": dict(
        records=5963,
        decoded=5962,
        first="2026-10-27 07:26:24",
        last="2026-10-27 23:59:54",
        skipped="2026-10-27 07:26:14",
    ),
    "2026-10-28": dict(
        records=8640,
        decoded=8639,
        first="2026-10-28 00:00:14",
        last="2026-10-28 23:59:55",
        skipped="2026-10-28 00:00:04",
    ),
    "2026-10-29": dict(
        records=7319,
        decoded=7318,
        first="2026-10-29 00:00:15",
        last="2026-10-29 20:19:46",
        skipped="2026-10-29 00:00:05",
    ),
}

_LOGX_CACHE: dict[str, dict[tuple[str, int, str], float]] = {}


def _require_capture(*names: str) -> None:
    """Full-day CMI-/LOG captures (``cmi_raw_*.log`` / ``logx_*.log``) are > 1 MB
    each and are only bundled in the internal dev tree. Skip hermetically when
    a capture is not bundled (public repo) instead of failing."""
    for name in names:
        if not (CMILog_DIR / name).is_file():
            pytest.skip(f"CMI-/LOG capture {name!r} not bundled (public repo)")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _logx_map(day: str) -> dict[tuple[str, int, str], float]:
    """WinSol-LogX ground truth: {(ts, dev, 'aN'): physical value}."""
    if day in _LOGX_CACHE:
        return _LOGX_CACHE[day]
    from analysis.winsol_logx import decode_record, find_sessions

    buf = (CMILog_DIR / f"logx_{day}.log").read_bytes()
    out: dict[tuple[str, int, str], float] = {}
    for sess in find_sessions(buf):
        p = sess.variant.period
        for k in range(sess.n_records):
            rec = decode_record(buf[sess.start + p * k : sess.start + p * (k + 1)], sess.variant)
            ts = f"{day} {rec.hh:02d}:{rec.mm:02d}:{rec.ss:02d}"
            for dev, vals in rec.values.items():
                for ch, v in vals.items():
                    out[(ts, dev, ch)] = float(v)
    _LOGX_CACHE[day] = out
    return out


def _record_bytes(hh: int, mm: int, ss: int) -> bytes:
    """One synthetic P=169 record: valid time + known analog AND digital slots.

    Digital (S4N-LogParquet byte-per-channel): d2/d3/d4/d5/d6/d8/d9 = 1,
    d7 = 0, dev2 d1 (Solarpumpe) = 1.
    """
    rec = bytearray(RECORD_PERIOD)
    struct.pack_into("<H", rec, 2 * 0, 2000)  # dev1 a1 -> 20.0
    struct.pack_into("<H", rec, 2 * 62, 553)  # dev0 a1 -> 55.3
    struct.pack_into("<H", rec, 2 * 77, 1)  # dev0 a16 -> 1
    struct.pack_into("<H", rec, 2 * 48, 1)  # dev2 d1 -> 1
    struct.pack_into("<H", rec, 2 * 56, 1)  # dev0 d2 -> 1 (slot 56 bit 0)
    struct.pack_into("<H", rec, 2 * 57, 1)  # dev0 d4 -> 1 (slot 57 bit 0)
    struct.pack_into("<H", rec, 2 * 58, 1)  # dev0 d6 -> 1 (slot 58 bit 0, d7 stays 0)
    struct.pack_into("<H", rec, 2 * 59, 1)  # dev0 d8 -> 1 (slot 59 bit 0)
    rec[113] = 1  # dev0 d3 -> 1 (slot 56 bit 8)
    rec[115] = 1  # dev0 d5 -> 1 (slot 57 bit 8)
    rec[119] = 1  # dev0 d9 -> 1 (slot 59 bit 8)
    rec[108] = hh
    rec[107] = mm
    rec[106] = ss
    return bytes(rec)


def _record_bytes_slots(hh: int, mm: int, ss: int, slots: dict[int, int]) -> bytes:
    """One synthetic P=169 record: valid time + explicit analog slots.

    ``slots`` maps a slot index to the raw u16 bytes to place at offset
    ``2 * slot``.  Used by the Q-121 signed round-trip test to encode negative
    i16 values (e.g. 0xFFFF = -1) into specific analog slots.
    """
    rec = bytearray(RECORD_PERIOD)
    for slot, raw in slots.items():
        struct.pack_into("<H", rec, 2 * slot, raw)
    rec[108] = hh
    rec[107] = mm
    rec[106] = ss
    return bytes(rec)


def _advance(hh: int, mm: int, ss: int) -> tuple[int, int, int]:
    total = hh * 3600 + mm * 60 + ss + 10
    return total // 3600, (total % 3600) // 60, total % 60


def make_log_file(
    name: str, start: tuple[int, int, int], n_records: int = 40, *, year: int = YEAR
) -> tuple[str, int, bytes]:
    """Build a minimal valid CMI /LOG data file.

    Magic + one 0x43 header block + ``n_records`` P-grid records.  Like the
    real files, record 0 straddles the header boundary and must be skipped
    by the decoder -> ``n_records - 1`` decoded records.
    """
    path = f"/LOG/{year}/{name}"
    parts = [b"8D312121\r\n", b"\x43" * (RECORD_PERIOD - 10)]
    hh, mm, ss = start
    for _ in range(n_records):
        parts.append(_record_bytes(hh, mm, ss))
        hh, mm, ss = _advance(hh, mm, ss)
    body = b"".join(parts)
    return path, len(body), body


class LogServer:
    """Mutable fake /LOG server state + :class:`httpx.MockTransport` factory."""

    def __init__(
        self,
        files: list[tuple[str, int, bytes]],
        *,
        index_status: int = 200,
        index_404_years: frozenset[int] = frozenset(),
        data_fail: dict[str, int] | None = None,
        data_timeout: set[str] | None = None,
        clear_status: int = 200,
        clear_body: bytes = b"OK",
        infoh_status: int = 0,
    ) -> None:
        self.files: dict[str, tuple[int, bytes]] = {p: (s, b) for p, s, b in files}
        self.index_status = index_status
        self.index_404_years = index_404_years
        self.data_fail = data_fail or {}
        self.data_timeout = data_timeout or set()
        self.clear_status = clear_status
        self.clear_body = clear_body
        #: Failure status for the /LOG/infoh.log flush-read (S4N-LogFlush),
        #: e.g. 500; 0 = the flush-read succeeds.
        self.infoh_status = infoh_status
        self.calls: dict = {"index": 0, "infoh": 0, "data": [], "clear": 0}
        #: Every /LOG request path in arrival order (S4N-LogFlush: the
        #: flush-read must precede the index read -- assertable in tests).
        self.request_paths: list[str] = []

    def transport(self) -> httpx.MockTransport:
        calls = self.calls

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            self.request_paths.append(path)
            # S4N-LogFlush: the RAM-buffer flush-read is NOT the year index --
            # intercept it BEFORE the /LOG/info* index rule so calls["index"]
            # keeps counting index reads only (infoh.log is counted in
            # calls["infoh"] instead).
            if path == "/LOG/infoh.log":
                calls["infoh"] += 1
                if self.infoh_status:
                    return httpx.Response(self.infoh_status, text="flush-read error")
                return httpx.Response(200, text="CMI\r\nstorekrit: 10\r\n")
            if path.startswith("/LOG/info"):
                calls["index"] += 1
                m = re.search(r"info(\d{4})", path)
                year = int(m.group(1)) if m else 0
                if year in self.index_404_years or self.index_status != 200:
                    status = 404 if year in self.index_404_years else self.index_status
                    return httpx.Response(status, text="no index")
                # only list files that belong to THIS year's index
                text = "CMI\r\nstorekrit: 10\r\n" + "".join(
                    f"{p} {size}\r\n"
                    for p, (size, _body) in self.files.items()
                    if p.startswith(f"/LOG/{year}/")
                )
                return httpx.Response(200, text=text)
            if path == "/LOG/clear.log":
                calls["clear"] += 1
                return httpx.Response(self.clear_status, content=self.clear_body)
            if path in self.data_timeout:
                raise httpx.ConnectTimeout("synthetic /LOG timeout")
            if path in self.data_fail:
                calls["data"].append(path)
                return httpx.Response(self.data_fail[path], text="error")
            if path in self.files:
                calls["data"].append(path)
                return httpx.Response(200, content=self.files[path][1])
            return httpx.Response(404, text="missing")

        return httpx.MockTransport(handler)


def _log_gateway(settings, server: LogServer) -> Gateway:
    """A Gateway whose /LOG client runs against the fake server."""
    client = CMILogClient(
        settings.base_url,
        settings.cmi_user,
        settings.cmi_pass,
        transport=server.transport(),
    )
    return Gateway(settings, cmi_log=client)


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


# ---------------------------------------------------------------------------
# Index parsing
# ---------------------------------------------------------------------------
def test_parse_real_index_info2026():
    """The recorded info2026.log parses into the single listed data file."""
    body = (CMILog_DIR / "info2026.log").read_bytes()
    files = parse_log_index(body)
    assert len(files) == 1
    f = files[0]
    assert f.path == "/LOG/2026/data_2026_10_29_20_20_16.log"
    assert f.size == 6863
    assert f.name == "data_2026_10_29_20_20_16.log"
    assert f.date == "2026-10-29"
    assert (f.year, f.month, f.day) == (2026, 10, 29)


def test_parse_synthetic_index_ignores_junk():
    lines = (
        "CMI\r\n"
        "1.47 1.06 2.00 Revision: 1803\r\n"
        "storekrit: 10\r\n"
        "\0\0\0\0\r\n"
        "\r\n"
        "/LOG/2026/data_2026_10_27_00_00_04.log 1013765\r\n"
        "/LOG/2026/data_2026_10_28_00_00_04.log 1466178\r\n"
        "garbage line that is not a data file\r\n"
        "/LOG/2026/data_2026_10_29_00_00_05.log 1242929\r\n"
    )
    files = parse_log_index(lines)
    assert [f.name for f in files] == [
        "data_2026_10_27_00_00_04.log",
        "data_2026_10_28_00_00_04.log",
        "data_2026_10_29_00_00_05.log",
    ]
    assert [f.size for f in files] == [1013765, 1466178, 1242929]


def test_parse_empty_index():
    assert parse_log_index(b"CMI\r\n\r\n") == []
    assert parse_log_index(b"") == []


# ---------------------------------------------------------------------------
# Decoder: error paths
# ---------------------------------------------------------------------------
def test_decode_too_small():
    with pytest.raises(ValueError, match="too small"):
        decode_cmi_log(b"8D312121\r\n" + b"\x43" * 100, date="2026-10-28")


def test_decode_bad_magic():
    with pytest.raises(ValueError, match="bad CMI log magic"):
        decode_cmi_log(b"NOPE-MAGIC" + b"\x43" * 1000, date="2026-10-28")


def test_decode_missing_date():
    _require_capture("cmi_raw_2026-10-29.log")
    body = (CMILog_DIR / "cmi_raw_2026-10-29.log").read_bytes()
    with pytest.raises(ValueError, match="cannot determine the logged day"):
        decode_cmi_log(body)


def test_date_from_filename_basename_and_path():
    from app.cmi.cmi_log import _date_from_filename

    assert _date_from_filename("data_2026_10_28_00_00_04.log") == "2026-10-28"
    assert _date_from_filename("/LOG/2026/data_2026_10_28_00_00_04.log") == "2026-10-28"
    assert _date_from_filename("other.log") is None


# ---------------------------------------------------------------------------
# Decoder: Rosetta vs WinSol-LogX ground truth (3 days)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("day", sorted(DAYS))
def test_rosetta_full_alignment(day: str):
    """Decoder record 0-skip + 10-s ts grid + value agreement vs LogX."""
    _require_capture(f"cmi_raw_{day}.log", f"logx_{day}.log")
    exp = DAYS[day]
    raw = (CMILog_DIR / f"cmi_raw_{day}.log").read_bytes()
    filename = f"/LOG/{YEAR}/data_{day.replace('-', '_')}_00_00_00.log"
    data = decode_cmi_log(raw, filename=filename)

    assert data.date == day
    assert data.n_records == exp["records"]
    assert data.n_decoded == exp["decoded"]  # header-overlap record skipped
    assert exp["skipped"] not in {r[0] for r in data.rows[::70]}
    assert len(data.rows) == exp["decoded"] * 70
    assert data.rows[0][0] == exp["first"]
    assert data.rows[-1][0] == exp["last"]

    # 10-s sampling: the per-record ts sequence is monotonic, 10 s typical,
    # with rare sub-second/one-step drift (observed: mostly 10 s, a handful of
    # 8/11/12 s).  No step may exceed one full record period + drift.
    record_ts = [r[0] for r in data.rows[::70]]
    deltas = [
        (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
        for a, b in zip(record_ts, record_ts[1:], strict=False)
    ]
    assert all(8 <= d <= 12 for d in deltas)
    assert sorted(deltas)[len(deltas) // 2] == 10.0  # median = 10 s

    # Row shape: source fixed, ad A/D, device namespace cmi-log:<dev>.
    for _ts, dev, _ch, ad, value, unit, source in data.rows:
        assert source == "cmi-log"
        assert ad in ("A", "D")
        assert dev in ("cmi-log:0", "cmi-log:1", "cmi-log:2")
        if ad == "D":
            assert value in (0.0, 1.0)
            assert unit is None

    # Rosetta value comparison vs the LogX decoder (analog; digital has its
    # own test below: test_rosetta_digital).
    lx = _logx_map(day)
    cmi_map: dict[tuple[str, int, str], float] = {}
    for ts, devstr, ch, ad, val, _unit, _src in data.rows:
        if ad != "A":
            continue
        cmi_map[(ts, int(devstr.rsplit(":", 1)[1]), f"a{ch}")] = val
    # every CMI timestamp exists in the LogX day
    assert {k[0] for k in cmi_map} <= {k[0] for k in lx}

    strong_match = strong_cmp = 0
    weak_match = weak_cmp = 0
    for (ts, dev, ch), val in cmi_map.items():
        if (ts, dev, ch) not in lx:
            continue
        if (dev, ch) in DIVERGENT_CHANNELS:
            continue  # dokumentierte CMI-0.0-vs-LogX-Kanon-Divergenz (S4N-LogFull)
        equal = abs(lx[(ts, dev, ch)] - val) < 1e-9
        if (dev, ch) in WEAK_CHANNELS:
            weak_cmp += 1
            weak_match += equal
        else:
            strong_cmp += 1
            strong_match += equal
    # dev0 (Heizkreis) must be 100 % exact on all three days
    for (ts, dev, ch), val in cmi_map.items():
        if dev == 0 and (ts, dev, ch) in lx:
            assert abs(lx[(ts, dev, ch)] - val) < 1e-9, f"dev0 {ch} @ {ts}"
    assert strong_match / strong_cmp >= 0.995
    assert weak_match / weak_cmp >= 0.85  # documented sampling-phase exceptions


@pytest.mark.parametrize("day", ["2026-10-29"])
def test_decode_deterministic(day: str):
    """Same file -> same rows (the decoder must be deterministic)."""
    _require_capture(f"cmi_raw_{day}.log")
    raw = (CMILog_DIR / f"cmi_raw_{day}.log").read_bytes()
    a = decode_cmi_log(raw, date=day)
    b = decode_cmi_log(raw, date=day)
    assert a.rows == b.rows
    assert a.n_records == b.n_records


# ---------------------------------------------------------------------------
# Decoder: post-reboot fragments (tiny data files, S4N-LogPoll)
# ---------------------------------------------------------------------------
def test_decode_reboot_fragment_two_records():
    """A 2-record post-reboot file decodes instead of raising.

    Regression (2026-10-30): the 30-record confirmation window rejected the
    real ``data_2026_10_29_20_19_56.log`` fragment (6356 B, 2 records after a
    CMI reboot).  The window is now capped per candidate to the blocks that
    actually carry time bytes (floor 2).  The decoder still skips the record
    that straddles the header block -> 1 decoded record.
    """
    _path, _size, body = make_log_file("data_2026_10_29_20_19_56.log", (20, 19, 56), n_records=2)
    data = decode_cmi_log(body, filename="data_2026_10_29_20_19_56.log")
    assert data.date == "2026-10-29"
    assert data.n_records == 2
    assert data.n_decoded == 1
    assert len(data.rows) == 70
    assert data.rows[0][0] == "2026-10-29 20:20:06"


def test_decode_reboot_fragment_five_records():
    """A 5-record reboot fragment decodes every record after the straddler."""
    _path, _size, body = make_log_file("data_2026_10_29_20_20_16.log", (20, 20, 16), n_records=5)
    data = decode_cmi_log(body, filename="data_2026_10_29_20_20_16.log")
    assert data.n_records == 5
    assert data.n_decoded == 4
    assert len(data.rows) == 280
    record_ts = [r[0][11:] for r in data.rows[::70]]
    assert record_ts == ["20:20:26", "20:20:36", "20:20:46", "20:20:56"]


def test_unit_assignment():
    """Only temperature channels carry unit='C'; counters/flow/Minderer-V
    stay None; digital rows never carry a unit."""
    _, _, body = make_log_file("data_2026_10_28_00_00_04.log", (0, 0, 4))
    data = decode_cmi_log(body, filename="data_2026_10_28_00_00_04.log")
    by_key = {(dev, ch, ad): unit for _, dev, ch, ad, v, unit, _s in data.rows}
    assert by_key[("cmi-log:0", 6, "A")] == "\u00b0C"  # dev0 a6: T
    assert by_key[("cmi-log:0", 16, "A")] is None  # dev0 a16: flow switch
    assert by_key[("cmi-log:1", 3, "A")] is None  # dev1 a3: raw counter
    assert by_key[("cmi-log:1", 6, "A")] is None  # dev1 a6: raw (signed)
    assert by_key[("cmi-log:1", 13, "A")] is None  # dev1 a13: minute counter
    assert by_key[("cmi-log:2", 16, "A")] == "\u00b0C"  # dev2 a16: collector temp
    # S4N-LogFull new channels:
    assert by_key[("cmi-log:2", 13, "A")] == "\u00b0C"  # dev2 a13: Minderer-T °C (Q-112)
    assert by_key[("cmi-log:2", 14, "A")] == "\u00b0C"  # dev2 a14: Minderer-T °C
    assert by_key[("cmi-log:2", 15, "A")] == "\u00b0C"  # dev2 a15: Minderer-T °C
    assert by_key[("cmi-log:2", 10, "A")] is None  # dev2 a10: Minderer 0-10 V
    assert by_key[("cmi-log:2", 11, "A")] is None  # dev2 a11: Minderer 0-10 V
    assert by_key[("cmi-log:2", 12, "A")] is None  # dev2 a12: Minderer 0-10 V
    assert by_key[("cmi-log:0", 11, "A")] is None  # dev0 a11: unassigned
    assert by_key[("cmi-log:1", 10, "A")] is None  # dev1 a10: raw (NOT temp)
    assert by_key[("cmi-log:1", 14, "A")] is None  # dev1 a14: raw
    assert by_key[("cmi-log:1", 15, "A")] is None  # dev1 a15: raw
    assert by_key[("cmi-log:1", 16, "A")] is None  # dev1 a16: raw
    assert by_key[("cmi-log:0", 6, "D")] is None  # dev0 d6: digital, no unit
    assert by_key[("cmi-log:2", 1, "D")] is None  # dev2 d1: digital, no unit
    # dev1 digital IS emitted now (S4N-LogFull: documented 0.0 fill, no payload
    # byte in the CMI record; GT constant 0).
    assert by_key[("cmi-log:1", 1, "D")] is None
    assert by_key[("cmi-log:1", 13, "D")] is None
    assert {
        ch for (dev, ch, ad), unit in by_key.items() if dev == "cmi-log:1" and ad == "D"
    } == set(range(1, 14))


def test_synthetic_digital_decode():
    """The synthetic records carry known digital bits (S4N-LogParquet bitmap)
    plus the documented dev1 d1-d13 0.0 fill (S4N-LogFull)."""
    _, _, body = make_log_file("data_2026_10_28_00_00_04.log", (0, 0, 4))
    data = decode_cmi_log(body, filename="data_2026_10_28_00_00_04.log")
    digital = {(dev, ch): val for _ts, dev, ch, ad, val, _u, _s in data.rows if ad == "D"}
    assert digital == {
        ("cmi-log:0", 2): 1.0,
        ("cmi-log:0", 3): 1.0,
        ("cmi-log:0", 4): 1.0,
        ("cmi-log:0", 5): 1.0,
        ("cmi-log:0", 6): 1.0,
        ("cmi-log:0", 7): 0.0,
        ("cmi-log:0", 8): 1.0,
        ("cmi-log:0", 9): 1.0,
        ("cmi-log:2", 1): 1.0,  # Solarpumpe
        **{("cmi-log:1", n): 0.0 for n in range(1, 14)},  # dev1 d1-d13: 0.0 fill
    }


_LOGX_DIGITAL_CACHE: dict[str, dict[str, tuple[dict[int, int], dict[int, int]]]] = {}


def _logx_digital_map(day: str) -> dict[str, tuple[dict[int, int], dict[int, int]]]:
    """WinSol-LogX digital ground truth: {ts: (dev0 {dN: 0/1}, dev2 {d1: 0/1})}.

    Built by decoding every LogX record of the day (the ``_logx_map`` analog
    cache does not carry the digital fields).
    """
    if day in _LOGX_DIGITAL_CACHE:
        return _LOGX_DIGITAL_CACHE[day]
    from analysis.winsol_logx import decode_record, find_sessions

    buf = (CMILog_DIR / f"logx_{day}.log").read_bytes()
    out: dict[str, tuple[dict[int, int], dict[int, int]]] = {}
    for sess in find_sessions(buf):
        p = sess.variant.period
        for k in range(sess.n_records):
            rec = decode_record(buf[sess.start + p * k : sess.start + p * (k + 1)], sess.variant)
            ts = f"{day} {rec.hh:02d}:{rec.mm:02d}:{rec.ss:02d}"
            dev0: dict[int, int] = {
                int(nm[1:]): int(v) for nm, v in rec.values.get(0, {}).items() if nm.startswith("d")
            }
            dev2: dict[int, int] = {
                int(nm[1:]): int(v) for nm, v in rec.values.get(2, {}).items() if nm.startswith("d")
            }
            out[ts] = (dev0, dev2)
    _LOGX_DIGITAL_CACHE[day] = out
    return out


@pytest.mark.parametrize("day", sorted(DAYS))
def test_rosetta_digital(day: str):
    """Digital Rosetta vs WinSol-LogX ground truth.

    * dev0 d2-d9 (byte-per-channel layout): >= 99.9 % value-identical on
      every GT day AND toggle-consistent (every CMI-side toggle matches the
      LogX transition at the same wall-clock second).
    * dev1 digital: emitted as a documented 0.0 fill (S4N-LogFull, Q-112b;
      the CMI record carries no dev1 digital payload and the GT is constant
      0 on 344 188 samples) -> every emitted dev1 digital value == 0.0.
    * dev2 d1 (Solarpumpe): >= 99 % value-identical (the known dev2
      sampling phase, same class as dev2 a16).
    """
    _require_capture(f"cmi_raw_{day}.log", f"logx_{day}.log")
    raw = (CMILog_DIR / f"cmi_raw_{day}.log").read_bytes()
    filename = f"/LOG/{YEAR}/data_{day.replace('-', '_')}_00_00_00.log"
    data = decode_cmi_log(raw, filename=filename)
    gt = _logx_digital_map(day)

    by_ts: dict[str, dict[int, dict[int, float]]] = {}
    for ts, devstr, ch, ad, val, _u, _s in data.rows:
        if ad != "D":
            continue
        by_ts.setdefault(ts, {}).setdefault(int(devstr.rsplit(":", 1)[1]), {})[ch] = val

    # dev1 digital IS emitted now (S4N-LogFull: documented 0.0 fill; the CMI
    # record carries no dev1 digital payload, and the LogX GT is constant 0).
    for ts, devs in by_ts.items():
        assert set(devs.get(1, ())) == set(range(1, 14)), ts
        assert all(v == 0.0 for v in devs[1].values()), f"dev1 digital != 0 at {ts}"
    # dev0 carries exactly d2-d9, dev2 exactly d1.
    for ts, devs in by_ts.items():
        assert set(devs.get(0, ())) == set(range(2, 10)), ts
        assert set(devs.get(2, ())) == {1}, ts

    verified: dict[int, list[int]] = {n: [0, 0] for n in range(2, 10)}
    dev2 = [0, 0]  # dev2 d1: match / compared
    dev0_toggles_m = dev0_toggles_c = 0  # dev0 toggle consistency (verified)
    prev_cmi_dev0: dict[int, int] | None = None
    prev_ts: str | None = None
    for ts in sorted(by_ts):
        if ts not in gt:
            continue
        gt_dev0, gt_dev2 = gt[ts]
        cmi_dev0 = {n: int(v) for n, v in by_ts[ts].get(0, {}).items()}
        cmi_dev2 = {n: int(v) for n, v in by_ts[ts].get(2, {}).items()}
        for n in range(2, 10):
            verified[n][0] += cmi_dev0[n] == gt_dev0[n]
            verified[n][1] += 1
        if 1 in cmi_dev2 and 1 in gt_dev2:
            dev2[0] += cmi_dev2[1] == gt_dev2[1]
            dev2[1] += 1
        # Toggle consistency: a CMI-side transition 0<->1 between prev_ts and
        # ts must be a LogX-side transition at the same place (both loggers
        # sample the same CAN channel; the byte-per-channel layout is
        # validated on the toggles, not only on the constant baseline).
        if prev_cmi_dev0 is not None and prev_ts in gt:
            pgt_dev0 = gt[prev_ts][0]
            for n in range(2, 10):
                if cmi_dev0.get(n) != prev_cmi_dev0.get(n):
                    dev0_toggles_c += 1
                    dev0_toggles_m += int(gt_dev0[n] != pgt_dev0[n])
        prev_cmi_dev0, prev_ts = cmi_dev0, ts
    # Verified dev0 channels: d2-d9 >= 99.9 % value-identical (per channel).
    for n in range(2, 10):
        m, c = verified[n]
        assert c == data.n_decoded
        assert m / c >= 0.999, f"dev0 d{n} only {m}/{c} identical"
    # Every dev0 CMI toggle must be a LogX toggle (no-toggle days: 0/0 -> pass).
    assert dev0_toggles_m == dev0_toggles_c, (
        f"dev0 toggle mismatch {dev0_toggles_m}/{dev0_toggles_c}"
    )
    # dev2 d1 (Solarpumpe): >= 99 % value-identical (sampling phase -- a weak
    # channel, same class as dev2 a16; its toggles carry a 1-sample phase
    # offset and are NOT part of the strict toggle-consistency check).
    assert dev2[1] == data.n_decoded
    assert dev2[0] / dev2[1] >= 0.99, f"dev2 d1 {dev2[0]}/{dev2[1]}"


# ---------------------------------------------------------------------------
# Digital toggle proof (2026-10-30, CMI-nativ vs WinSol-LogX, gleiche Records)
# ---------------------------------------------------------------------------
_TOGGLE_DAY = "2026-10-30"
_TOGGLE_CMI = CMILog_DIR / f"cmi_raw_{_TOGGLE_DAY}_toggle.log"
_TOGGLE_LX = CMILog_DIR / f"logx_{_TOGGLE_DAY}_toggle.log"


def _logx_digital_map_path(path: str | Path) -> dict[str, tuple[dict[int, int], dict[int, int]]]:
    """WinSol-LogX digital ground truth from an explicit file path."""
    from analysis.winsol_logx import decode_record, find_sessions

    buf = Path(path).read_bytes()
    out: dict[str, tuple[dict[int, int], dict[int, int]]] = {}
    for sess in find_sessions(buf):
        p = sess.variant.period
        for k in range(sess.n_records):
            rec = decode_record(buf[sess.start + p * k : sess.start + p * (k + 1)], sess.variant)
            ts = f"{_TOGGLE_DAY} {rec.hh:02d}:{rec.mm:02d}:{rec.ss:02d}"
            dev0: dict[int, int] = {
                int(nm[1:]): int(v) for nm, v in rec.values.get(0, {}).items() if nm.startswith("d")
            }
            dev2: dict[int, int] = {
                int(nm[1:]): int(v) for nm, v in rec.values.get(2, {}).items() if nm.startswith("d")
            }
            out[ts] = (dev0, dev2)
    return out


def test_digital_toggle_proof():
    """Digital Rosetta WITH real toggles (HITL-Toggle-Test 2026-10-30).

    Beide Logger haben denselben 10-s-Samplestrom aufgezeichnet:

    * CMI-nativ  ``cmi_raw_2026-10-30_toggle.log`` (P=169, abgeholt OHNE
      ``clear.log`` -- dadurch konnte WinSol dieselben Records abholen).
    * WinSol-LogX ``logx_2026-10-30_toggle.log`` (P=137, WinSol "Loeschen"
      deaktiviert, daher anhaender Tages-File 15:16:56-20:39:36).

    Manuell getoggelt wurden dev0 d2 (WW-Pumpe Puffer), d5 (Anforderung
    Brenner), d6 (WW-Zirkulation), d7 (Zirkulationspumpe), d8 (Pumpe
    Heizkreis), d9 (Ventil Heizungsruecklauf) sowie dev2 d1 (Solarpumpe).
    d3 (Mischkreis Auf) und d4 (Mischkreis Zu) waren nicht manuell
    schaltbar -- d4 togglete im GT dennoch (Mischkreis-Automatik) und dient
    dadurch als zweiter Toggle-Nachweis; d3 blieb konstant.

    * dev0 d2-d9: 100 % werteidentisch INKLUSIVE der Toggle-Transitionen
      (d2/d4/d5/d6/d7/d8/d9 schalteten im Fixture, d3 blieb konstant).
    * dev2 d1: >= 99 % (bekannte dev2-Sampling-Phase; der eindeutige
      Byte96-Bit0-Tie-Breaker gegen Byte32-Bit5 steht in
      docs/cmi-log-protocol.md Kap. 3.2).
    * dev1 digital: emittiert als 0.0-Fill (S4N-LogFull: kein CMI-Payload,
      GT konstant 0) -- alle emittierten Werte == 0.0.
    """
    if not _TOGGLE_CMI.is_file() or not _TOGGLE_LX.is_file():
        pytest.skip("toggle-test fixtures missing (discovery/cmi-log/*_toggle.log)")

    raw = _TOGGLE_CMI.read_bytes()
    data = decode_cmi_log(raw, filename="data_2026_10_30_19_51_56.log")
    gt = _logx_digital_map_path(_TOGGLE_LX)

    cmi_by_ts: dict[str, dict[int, dict[int, float]]] = {}
    for ts, devstr, ch, ad, val, _u, _s in data.rows:
        if ad != "D":
            continue
        cmi_by_ts.setdefault(ts, {}).setdefault(int(devstr.rsplit(":", 1)[1]), {})[ch] = val

    common = sorted(set(cmi_by_ts) & set(gt))
    assert len(common) >= 200, f"only {len(common)} overlapping samples"

    # dev1 digital is emitted now (S4N-LogFull: documented 0.0 fill, no CMI
    # payload byte; GT constant 0) -> all values must be exactly 0.0.
    for ts, devs in cmi_by_ts.items():
        assert set(devs.get(1, ())) == set(range(1, 14)), ts
        assert all(v == 0.0 for v in devs[1].values()), f"dev1 digital != 0 at {ts}"
    # dev0 carries exactly d2-d9, dev2 exactly d1.
    for ts, devs in cmi_by_ts.items():
        assert set(devs.get(0, ())) == set(range(2, 10)), ts
        assert set(devs.get(2, ())) == {1}, ts

    # dev0 d2-d9: 100 % value-identical, toggles included.
    for n in range(2, 10):
        for ts in common:
            assert int(cmi_by_ts[ts][0][n]) == gt[ts][0].get(n, 0), (ts, n)

    # The toggles really exist in the GT (no constant-channel trap):
    # d2/d4/d5/d6/d7/d8/d9 toggled; d3 (not switchable) stayed constant.
    gt_toggles = {n: 0 for n in range(2, 10)}
    for i in range(1, len(common)):
        for n in range(2, 10):
            if gt[common[i]][0].get(n, 0) != gt[common[i - 1]][0].get(n, 0):
                gt_toggles[n] += 1
    for n in (2, 4, 5, 6, 7, 8, 9):
        assert gt_toggles[n] >= 1, f"dev0 d{n} never toggled in toggle fixture"
    assert gt_toggles[3] == 0, "dev0 d3 was not switchable and must stay constant"

    # dev2 d1 (Solarpumpe): >= 99 % value-identical (sampling phase).
    dev2_m = sum(1 for ts in common if int(cmi_by_ts[ts][2][1]) == gt[ts][1].get(1, 0))
    assert dev2_m / len(common) >= 0.99, f"dev2 d1 {dev2_m}/{len(common)}"


# ---------------------------------------------------------------------------
# S4N-LogFull (Q-112b): die 25 neuen Kanäle + 70-Rows-Regression
# ---------------------------------------------------------------------------
def test_seventy_rows_per_record():
    """S4N-LogFull: jeder dekodierter Record emittiert exakt 70 Rows
    (48 analog + 22 digital) -- die volle Kanon-Abdeckung des wide Schemas."""
    _, _, body = make_log_file("data_2026_10_28_00_00_04.log", (0, 0, 4))
    data = decode_cmi_log(body, filename="data_2026_10_28_00_00_04.log")
    assert len(data.rows) == data.n_decoded * 70
    # pro ts exakt eine Row je Wertkanal (70), kein Kanal doppelt
    from collections import Counter

    per_ts = Counter(r[0] for r in data.rows)
    assert all(n == 70 for n in per_ts.values())


def test_minderer_v_scaling_and_units():
    """dev2 a10-a12 = Minderer Keller/EG/OG, 0-10 V (raw/100, KEIN °C):
    slot 34/36/38 mit Wertebereich {0.0, 10.0} -- 10 V = 10.0 (nicht 1000)."""
    _require_capture("cmi_raw_2026-10-27.log")
    raw = (CMILog_DIR / "cmi_raw_2026-10-27.log").read_bytes()
    data = decode_cmi_log(raw, filename="/LOG/2026/data_2026_10_27_07_26_14.log")
    minder: dict[int, set[float]] = {10: set(), 11: set(), 12: set()}
    units: dict[int, set[str | None]] = {10: set(), 11: set(), 12: set()}
    for _ts, dev, ch, ad, val, unit, _s in data.rows:
        if dev != "cmi-log:2" or ad != "A" or ch not in (10, 11, 12):
            continue
        minder[ch].add(val)
        units[ch].add(unit)
    # 10-27: EG (a11) togglet 0/10 (5013× 10 V), Keller/OG konstant 0 V
    assert minder[11] == {0.0, 10.0}, f"dev2 a11 values {minder[11]}"
    assert minder[10] == {0.0}, f"dev2 a10 values {minder[10]}"
    assert minder[12] == {0.0}, f"dev2 a12 values {minder[12]}"
    # Skala ÷100: 10.0 V statt 1000 / 1000.0
    assert all(v <= 10.0 for vals in minder.values() for v in vals)
    # KEIN °C (das wäre die dev2 a13-a15-Klassifikation)
    assert all(units[ch] == {None} for ch in (10, 11, 12))


def test_constant_fill_channels_read_their_slots():
    """dev0 a11-a15 (slots 72-76), dev1 a10 (slot 9), dev1 a14-a16
    (slots 13-15): konstanter GT (0 bzw. -1 Kanon bei a10) + konstanter
    CMI-Slot (0) -> 0.0-Fill über den echten Slot (kein Hardcode-0, der
    Slot wird tatsächlich gelesen).  Beleg: die Header-Overlapp-Record-0
    trägt in den dev1-Slots 9/13 die Label-Bytes (z. B. 17152 = „Solar…")
    und wird vom Decoder verworfen; alle dekodierten Records sind 0."""
    for day, name in (
        ("2026-10-27", "data_2026_10_27_07_26_14.log"),
        ("2026-10-28", "data_2026_10_28_00_00_04.log"),
        ("2026-10-29", "data_2026_10_29_00_00_05.log"),
    ):
        _require_capture(f"cmi_raw_{day}.log")
        raw = (CMILog_DIR / f"cmi_raw_{day}.log").read_bytes()
        data = decode_cmi_log(raw, filename=f"/LOG/2026/{name}")
        vals: dict[tuple[int, int], set] = {}
        for _ts, dev, ch, ad, val, _u, _s in data.rows:
            if ad != "A":
                continue
            d = int(dev.rsplit(":", 1)[1])
            key = (d, ch)
            vals.setdefault(key, set()).add(val)
        # alle konstanten Fill-Kanäle: exakt {0.0} über den ganzen Tag
        for key in (
            (0, 11),
            (0, 12),
            (0, 13),
            (0, 14),
            (0, 15),
            (1, 10),
            (1, 14),
            (1, 15),
            (1, 16),
        ):
            assert key in vals, f"{day} dev{key[0]} a{key[1]} nicht emittiert"
            assert vals[key] == {0.0}, f"{day} dev{key[0]} a{key[1]} = {vals[key]}"


def test_dev1_digital_fill_is_emitted_zero():
    """dev1 d1-d13: kein CMI-Payload (Slot-Scan negativ) + GT konstant 0
    -> 0.0-Fill wird in JEDEM dekodierten Record emittiert (70-Rows,
    keine NULL im Store; kein Raten -- der Wert = GT auf allen Samples)."""
    _require_capture("cmi_raw_2026-10-28.log")
    raw = (CMILog_DIR / "cmi_raw_2026-10-28.log").read_bytes()
    data = decode_cmi_log(raw, filename="/LOG/2026/data_2026_10_28_00_00_04.log")
    dev1d = [
        (ch, val) for _ts, dev, ch, ad, val, _u, _s in data.rows if dev == "cmi-log:1" and ad == "D"
    ]
    assert len(dev1d) == data.n_decoded * 13
    assert {ch for ch, _ in dev1d} == set(range(1, 14))
    assert all(val == 0.0 for _ch, val in dev1d)


def test_legacy_45_channels_bit_identical_regression():
    """Die vor S4N-LogFull existierenden 45 Kanäle bleiben WERT-mäßig
    bit-identisch (Value-Regression gegen die mit dem alten Decoder
    aufgenommenen Referenzwerte; die einzige erlaubte Delta-Änderung ist
    die dev2 a13-a15-Einheit °C gemäß Q-112)."""
    _require_capture("cmi_raw_2026-10-28.log")
    # Referenz: cmi_raw_2026-10-28.log, erste dekoderte Record (00:00:14)
    # und letzte (23:59:55), aufgenommen mit dem Pre-S4N-LogFull-Decoder
    # (git 8de6aaa) am 2026-11-01.
    ref_first = {
        ("cmi-log:0", 1, "A"): 66.1,
        ("cmi-log:0", 10, "A"): 19.4,
        ("cmi-log:0", 16, "A"): 0.0,
        ("cmi-log:0", 2, "D"): 0.0,
        ("cmi-log:0", 8, "D"): 0.0,
        ("cmi-log:0", 9, "D"): 1.0,
        ("cmi-log:1", 1, "A"): 24.7,
        ("cmi-log:1", 2, "A"): 21.2,
        ("cmi-log:1", 6, "A"): 527.0,
        ("cmi-log:1", 9, "A"): -340.0,
        ("cmi-log:1", 11, "A"): 1.0,
        ("cmi-log:1", 13, "A"): 0.5,
        ("cmi-log:2", 1, "A"): 66.1,
        ("cmi-log:2", 13, "A"): 24.9,
        ("cmi-log:2", 16, "A"): 10.5,
        ("cmi-log:2", 1, "D"): 0.0,
    }
    ref_last = {
        ("cmi-log:0", 1, "A"): 76.8,
        ("cmi-log:0", 10, "A"): 17.2,
        ("cmi-log:0", 16, "A"): 0.0,
        ("cmi-log:0", 2, "D"): 0.0,
        ("cmi-log:0", 8, "D"): 0.0,
        ("cmi-log:0", 9, "D"): 1.0,
        ("cmi-log:1", 1, "A"): 24.3,
        ("cmi-log:1", 2, "A"): 20.8,
        ("cmi-log:1", 6, "A"): -459.0,
        ("cmi-log:1", 9, "A"): -170.0,
        ("cmi-log:1", 11, "A"): 0.6,
        ("cmi-log:1", 13, "A"): 0.7,
        ("cmi-log:2", 1, "A"): 76.6,
        ("cmi-log:2", 13, "A"): 21.5,
        ("cmi-log:2", 16, "A"): 4.0,
        ("cmi-log:2", 1, "D"): 0.0,
    }
    raw = (CMILog_DIR / "cmi_raw_2026-10-28.log").read_bytes()
    data = decode_cmi_log(raw, filename="/LOG/2026/data_2026_10_28_00_00_04.log")
    assert data.rows[0][0] == "2026-10-28 00:00:14"
    assert data.rows[-1][0] == "2026-10-28 23:59:55"
    for (dev, ch, ad), val in ref_first.items():
        row = next(r for r in data.rows[:70] if r[1] == dev and r[2] == ch and r[3] == ad)
        assert row[4] == val, f"{dev} {ad}{ch:02d} @ first: {row[4]} != {val}"
    for (dev, ch, ad), val in ref_last.items():
        row = next(r for r in data.rows[-70:] if r[1] == dev and r[2] == ch and r[3] == ad)
        assert row[4] == val, f"{dev} {ad}{ch:02d} @ last: {row[4]} != {val}"


def test_rosetta_new_slots_vs_logx_gt():
    """Rosetta-Proof der neuen Minderer-Slots (S4N-LogFull): dev2 a11
    (slot 36 ÷100) und dev2 a12 (slot 38 ÷100) stimmen auf den Tagen mit
    variablem GT zu >= 99.5 % mit dem LogX-Ground-Truth überein (die 1-2
    Abweichungen pro Tag = dokumentierte dev2-Sampling-Phase); dev2 a10
    (slot 34 ÷100) ist auf allen GT-Tagen konstant 0 (der 10-V-Zustand ist
    real -- winsol_pq-Beleg in scripts/rosetta_full.py)."""
    from analysis.winsol_logx import decode_record, find_sessions

    _require_capture(
        "cmi_raw_2026-10-27.log", "logx_2026-10-27.log",
        "cmi_raw_2026-10-29.log", "logx_2026-10-29.log",
        "cmi_raw_2026-10-30_toggle.log", "logx_2026-10-30_toggle.log",
    )
    for day, suffix in (("2026-10-27", ""), ("2026-10-29", ""), ("2026-10-30", "_toggle")):
        raw = (CMILog_DIR / f"cmi_raw_{day}{suffix}.log").read_bytes()
        data = decode_cmi_log(raw, filename=f"/LOG/2026/data_{day.replace('-', '_')}_19_51_56.log")
        cmi: dict[str, dict[int, float]] = {}
        for ts, dev, ch, ad, val, _u, _s in data.rows:
            if dev == "cmi-log:2" and ad == "A" and ch in (10, 11, 12):
                cmi.setdefault(ts, {})[ch] = val
        buf = (CMILog_DIR / f"logx_{day}{suffix}.log").read_bytes()
        matches = {ch: 0 for ch in (10, 11, 12)}
        compared = {ch: 0 for ch in (10, 11, 12)}
        for sess in find_sessions(buf):
            p = sess.variant.period
            for k in range(sess.n_records):
                rec = decode_record(
                    buf[sess.start + p * k : sess.start + p * (k + 1)], sess.variant
                )
                if not rec.valid_clock:
                    continue
                ts = f"{day} {rec.hh:02d}:{rec.mm:02d}:{rec.ss:02d}"
                if ts not in cmi:
                    continue
                for ch in (10, 11, 12):
                    gt = rec.values.get(2, {}).get(f"a{ch}")
                    if gt is None:
                        continue
                    compared[ch] += 1
                    matches[ch] += cmi[ts][ch] == round(float(gt) * 100) / 100
        # alle drei Minderer-Kanäle: über den gemeinsamen ts >= 99.5 % matchen
        # (konstante GT-Tage: 100 %; variable GT-Tage: Sampling-Phase-Abweichung
        # von 1-2 Samples pro Tag)
        for ch in (10, 11, 12):
            assert compared[ch] >= 100, f"dev2 a{ch} {day}: zu wenige Common-ts"
            assert matches[ch] / compared[ch] >= 0.995, (
                f"dev2 a{ch} {day} only {matches[ch]}/{compared[ch]}"
            )


def test_synthetic_body_roundtrip():
    """The synthetic builder is a valid CMI log: record 0 skipped, values ok."""
    path, size, body = make_log_file("data_2026_10_28_00_00_10.log", (0, 0, 10))
    data = decode_cmi_log(body, filename=path)
    assert data.n_records == 40
    assert data.n_decoded == 39  # record 0 straddles the header block
    assert data.rows[0][0] == "2026-10-28 00:00:20"
    assert data.rows[0][4] == 55.3  # dev0 a1 slot-62 value


# ---------------------------------------------------------------------------
# Q-121 (Signed-Bug): signed i16 decoding + LogX-Kanon-Parität
# ---------------------------------------------------------------------------
def test_signed_analog_roundtrip_negative_values():
    """Q-121 Signed-Bug regression: a negative analog value encoded as i16
    (two's complement u16 bytes) must decode to the SIGNED physical value.

    Grounding: ``data_2026_11_05_00_00_01.log`` dev2 a16 @ 23:10:19 carries
    raw 0xFFFF = signed i16 -1 -> -0.1 °C (NOT the unsigned misread 6553.5).

    Synthetic P=169 file (header + 3 records; record 0 straddles the header
    and is skipped -> 2 decoded):
      * slot 46 (dev2 a16, /10) = 0xFFFF -> -0.1 °C
      * slot 69 (dev0 a8, /10, Außen) = 0xFFF8 -> -8 /10 = -0.8 °C
      * slot 44 (dev2 a15, /10) = 0xFFFE -> -0.2 °C
      * slot 16 (dev2 a1, /10)  = 0x0001 -> +0.1 °C (positive stays positive)
      * slot 34 (dev2 a10, /100, UNSIGNED Minderer 0-10 V) = 0xFFFF -> 655.35
        (stays unsigned: a 0-10 V voltage is never negative)
      * slot 77 (dev0 a16, /1, flag) = 1 -> 1.0
    """
    # Three records on the P-grid; record 0 straddles the header block and is
    # skipped -> 2 decoded records.
    body = b"8D312121\r\n" + b"\x43" * (RECORD_PERIOD - 10)
    for hh, mm, ss in ((23, 10, 9), (23, 10, 19), (23, 10, 29)):
        body += _record_bytes_slots(
            hh,
            mm,
            ss,
            {
                46: 0xFFFF,  # dev2 a16 -> -0.1
                69: 0xFFF8,  # dev0 a8  -> -0.8
                44: 0xFFFE,  # dev2 a15 -> -0.2
                16: 0x0001,  # dev2 a1  -> +0.1
                34: 0xFFFF,  # dev2 a10 -> 655.35 (unsigned Minderer 0-10 V)
                77: 1,  # dev0 a16 -> 1.0 (flag)
            },
        )
    data = decode_cmi_log(body, filename="data_2026_11_05_00_00_01.log")
    assert data.n_records == 3
    assert data.n_decoded == 2  # record 0 straddles the header block

    # Both decoded records carry the same slots -> same values.
    decoded_ts = [r[0] for r in data.rows[::70]]
    assert decoded_ts == ["2026-11-05 23:10:19", "2026-11-05 23:10:29"]
    for ts in decoded_ts:
        vals = {(r[1], r[2], r[3]): r[4] for r in data.rows if r[0] == ts}
        assert len(vals) == 70  # full 70-channel record
        assert vals[("cmi-log:2", 16, "A")] == -0.1  # -1 /10, NOT 6553.5
        assert vals[("cmi-log:0", 8, "A")] == -0.8  # -8 /10 (Außen)
        assert vals[("cmi-log:2", 15, "A")] == -0.2  # -2 /10
        assert vals[("cmi-log:2", 1, "A")] == 0.1  # +1 /10 (positive)
        assert vals[("cmi-log:2", 10, "A")] == 655.35  # 0xFFFF unsigned /100
        assert vals[("cmi-log:0", 16, "A")] == 1.0  # flag /1


# ---------------------------------------------------------------------------
# Q-125 (P=181): the restructured record format introduced 2026-11-07 ~16:14
#
# CORRECTED frame (Round 2, verified byte-for-byte on the recorded 11-07/-08
# files + json:1/json:32 Rosetta):
#   * record = 181 B; bytes 0-2 = 3-byte sync prefix ``XX 0B 1A`` (the leading
#     byte varies 07/08, the tail is always ``0B 1A``)
#   * bytes 3/4/5 = ss / mm / hh  (time RIGHT after the sync prefix, NOT the
#     old P=169 deep offsets 106/107/108)
#   * bytes 6-178 = u16 value slots at a MIX of even/odd byte offsets (NOT a
#     uniform shift of P=169)
#   * bytes 179/180 = CRLF record terminator
#   * data_start = 6258 on the 11-07/-08 files (= header end + 2 CRLF), which
#     is NOT a multiple of 181 -> discovery is layout-agnostic (sync-scan, see
#     :func:`app.cmi.cmi_log._detect_data_start_181`), never hard-coded.
# The P=181 map stores ABSOLUTE byte offsets; the decoder reads a slot at
# ``rec_start + off``.
# ---------------------------------------------------------------------------
#: The P=181 record period (introduced 2026-11-07 ~16:14).
P181_PERIOD = 181
#: The P=181 sync-tail bytes (constant last two of the 3-byte sync prefix).
P181_SYNC = b"\x0b\x1a"
#: The P=181 record terminator.
P181_CRLF = b"\r\n"


def _record_bytes_181(
    hh: int,
    mm: int,
    ss: int,
    slots: dict[int, int] | None = None,
    dbytes: dict[int, int] | None = None,
    sync: bytes = b"\x07\x0b\x1a",
) -> bytes:
    """One synthetic P=181 record in the CORRECTED frame.

    * sync prefix ``XX 0B 1A`` at bytes 0-2 (leading byte varies; default 07)
    * time bytes ``ss @3`` / ``mm @4`` / ``hh @5``
    * CRLF terminator at bytes 179/180
    * ``slots`` maps ABSOLUTE byte offset -> raw i16/u16 (packed little-
      endian); the P=181 decoder reads a slot at ``rec_start + off``
    * ``dbytes`` maps absolute byte offset -> the raw digital byte (bit 0 =
      the value for the P=181 digital channels, e.g. dev0 d9 @ byte 16,
      dev2 d1 @ byte 174 -- Q-125 Round 5)
    """
    rec = bytearray(P181_PERIOD)
    rec[0:3] = sync
    rec[3] = ss
    rec[4] = mm
    rec[5] = hh
    rec[179:181] = P181_CRLF
    for off, raw in (slots or {}).items():
        struct.pack_into("<H", rec, off, raw & 0xFFFF)
    for off, val in (dbytes or {}).items():
        rec[off] = val & 0xFF
    return bytes(rec)


#: Default P=181 value slots used by the synthetic builders (byte offset ->
#: raw u16): dev0 a1 @21 = 200 (20.0 °C signed /10), dev0 a6 @31 = 380
#: (38.0 °C signed /10, VL), dev0 a8 @35 = 75 (7.5 °C signed /10, Außen),
#: dev2 a17 @162 = 1000 (10.0 V unsigned /100, Sollspannung 1).  Q-125 Round 3
#: adds the dev0 a11-a16 slots: a11 @41 = 477 (47.7 °C VL.Solltemp), a12 @43 =
#: 200 (20.0 °C T.raum.SOLL.eff), a16 @51 = 1 (Ström.Schalt5 0/1-Flag); a13-a15
#: @45/47/49 stay 0 (the "-----" 0-Fill).  Q-125 Round 5 adds the dev1 RAS
#: slots @66-96: a1/a2/a4/a5/a7/a8 @66/68/72/74/78/80 = 200 (20.0 °C /10),
#: a3/a6/a9 @70/76/82 = raw counters /1 (0), a11/a12/a13 @86/88/90 = 61
#: (6.1 min /10), a10/a14-a16 @84/92/94/96 = 0 (unused 0-Fill).
_P181_DEFAULT_SLOTS: dict[int, int] = {
    21: 200,
    31: 380,
    35: 75,
    162: 1000,
    41: 477,
    43: 200,
    51: 1,
    66: 200,
    68: 200,
    72: 200,
    74: 200,
    78: 200,
    80: 200,
    86: 61,
    88: 61,
    90: 61,
}
#: Default P=181 digital bytes (byte offset -> raw byte, bit 0 = value):
#: dev0 d2-d9 @6/10/11/12/13/14/15/16, dev2 d1 (Solarpumpe) @174 (Q-125 Round 5
#: correction: the R2 byte-10 mapping was dev0 d3, Mischkreis Auf).  All
#: default to 0; the round-trip test overrides individual bytes.
_P181_DEFAULT_DBYTES: dict[int, int] = {
    6: 0,
    10: 0,
    11: 0,
    12: 0,
    13: 0,
    14: 0,
    15: 0,
    16: 0,
    174: 0,
}


def make_log_file_181(
    name: str,
    start: tuple[int, int, int],
    n_records: int = 40,
    *,
    year: int = YEAR,
    channel_counter: int = 51,
    slots: dict[int, int] | None = None,
    dbytes: dict[int, int] | None = None,
    header_bytes: int = 320,
) -> tuple[str, int, bytes]:
    """Build a minimal valid P=181 CMI /LOG data file (corrected frame).

    Magic + header channel-counter u16LE (= 51 for P=181) + ``header_bytes``
    of 0x43 header + ``n_records`` P=181 records.  Because the P=181 data
    start is NOT a multiple of 181, the decoder's layout-agnostic sync-scan
    (:func:`app.cmi.cmi_log._detect_data_start_181`) locates the first record
    from its sync tail + CRLF + 10-s time grid -- so record 0 (the first
    real record) is DECODED (not skipped as in the P=169 header-overlap).
    ``slots`` / ``dbytes`` are written at absolute byte offsets into every
    record (defaults: :data:`_P181_DEFAULT_SLOTS` / :data:`_P181_DEFAULT_DBYTES`).
    """
    path = f"/LOG/{year}/{name}"
    slots = slots if slots is not None else _P181_DEFAULT_SLOTS
    dbytes = dbytes if dbytes is not None else _P181_DEFAULT_DBYTES
    parts = [
        b"8D312121\r\n",
        struct.pack("<H", channel_counter),
        b"\x43" * header_bytes,
    ]
    hh, mm, ss = start
    for _ in range(n_records):
        parts.append(_record_bytes_181(hh, mm, ss, slots, dbytes))
        hh, mm, ss = _advance(hh, mm, ss)
    body = b"".join(parts)
    return path, len(body), body


def test_p181_magic_lenience_all_four_magics():
    """Q-125: the magic check is format-agnostic (8 hex digits + CRLF).  All
    four observed magics -- shared by P=169 AND P=181 files -- must pass, and a
    non-hex opener must still fail."""
    from app.cmi.cmi_log import _MAGIC_RE

    for magic in (b"8D312121", b"86147834", b"A8024640", b"8204D130"):
        assert _MAGIC_RE.match(magic + b"\r\n" + b"\x00" * 400), magic
    for bad in (b"NOPE-MAG", b"8D3121  ", b"\x00" * 8):
        assert not _MAGIC_RE.match(bad + b"\r\n" + b"\x00" * 400), bad


def test_p181_synthetic_roundtrip_73_rows():
    """Q-125 synthetic round-trip (corrected frame + R5 maps): a P=181 file
    decodes to 73 rows/record, the P=181 time grid is honored (ss@3/mm@4/hh@5),
    the value-verified slots read at their ABSOLUTE byte offsets (dev0 a1 @21
    signed /10, dev0 a11 @41 / a12 @43 / a16 @51 from Q-125 Round 3, dev1 RAS
    a1-a16 @66-96 from Q-125 Round 5, dev2 a17 @162 unsigned /100), and the
    P=181-pending channels (dev2 a1-a4, eliminated) are emitted as documented
    NULL while the value-verified P=181 digital bytes (dev0 d2-d9, dev2 d1
    @byte 174) carry a value and the dev1 digital stays the documented 0.0
    fill."""
    dbytes = {6: 0, 10: 0, 11: 0, 12: 0, 13: 0, 14: 0, 15: 1, 16: 1, 174: 1}
    path, size, body = make_log_file_181(
        "data_2026_11_07_16_15_59.log",
        (23, 10, 0),
        n_records=31,
        dbytes=dbytes,
    )
    data = decode_cmi_log(body, filename=path)
    # The layout-agnostic sync-scan finds the first record; record 0 is
    # DECODED (no P=169-style header-overlap skip on the corrected frame).
    assert data.n_records == 31
    assert data.n_decoded == 31
    assert data.rows[0][0] == "2026-11-07 23:10:00"
    # 73 rows per P=181 record (51 analog + 22 digital).
    assert len(data.rows) == data.n_decoded * 73

    # Spot-check the FIRST decoded record: value-verified slots + NULL pending.
    first_ts = data.rows[0][0]
    vals = {(r[1], r[2], r[3]): r[4] for r in data.rows if r[0] == first_ts}
    assert len(vals) == 73
    # Value-verified analog (corrected byte offsets).
    assert vals[("cmi-log:0", 1, "A")] == 20.0  # dev0 a1 @21 signed /10
    assert vals[("cmi-log:0", 6, "A")] == 38.0  # dev0 a6 @31 signed /10 (VL)
    assert vals[("cmi-log:0", 8, "A")] == 7.5  # dev0 a8 @35 signed /10 (Außen)
    assert vals[("cmi-log:0", 11, "A")] == 47.7  # dev0 a11 @41 VL.Solltemp (Q-125 R3)
    assert vals[("cmi-log:0", 12, "A")] == 20.0  # dev0 a12 @43 T.raum.SOLL.eff
    assert vals[("cmi-log:0", 13, "A")] == 0.0  # dev0 a13 @45 "-----" 0-Fill
    assert vals[("cmi-log:0", 16, "A")] == 1.0  # dev0 a16 @51 Ström.Schalt5
    assert vals[("cmi-log:2", 17, "A")] == 10.0  # dev2 a17 @162 unsigned /100
    # dev1 RAS @66-96 (Q-125 Round 5): temperatures /10, counters /1.
    assert vals[("cmi-log:1", 1, "A")] == 20.0  # a1 @66 ras_kell_perc
    assert vals[("cmi-log:1", 2, "A")] == 20.0  # a2 @68 ras_kell_ist
    assert vals[("cmi-log:1", 3, "A")] == 0.0  # a3 @70 raw Zähler /1
    assert vals[("cmi-log:1", 5, "A")] == 20.0  # a5 @74 ras_eg_ist
    assert vals[("cmi-log:1", 8, "A")] == 20.0  # a8 @80 ras_dg_ist
    assert vals[("cmi-log:1", 11, "A")] == 6.1  # a11 @86 ras_age_kell /10
    assert vals[("cmi-log:1", 13, "A")] == 6.1  # a13 @90 ras_age_dg /10
    assert vals[("cmi-log:1", 14, "A")] == 0.0  # a14 @92 unused 0-Fill
    # P=181-eliminated analog -> documented NULL (dev2 a1-a4 only, after R5).
    for ch in range(1, 5):
        assert vals[("cmi-log:2", ch, "A")] is None
    # Value-verified P=181 digital -> valued (NOT NULL).
    assert vals[("cmi-log:0", 2, "D")] == 0.0  # dev0 d2 @byte6 (const 0)
    assert vals[("cmi-log:0", 8, "D")] == 1.0  # dev0 d8 @byte15 (Pumpe Hz)
    assert vals[("cmi-log:0", 9, "D")] == 1.0  # dev0 d9 @byte16 (Ventil HzRL)
    assert vals[("cmi-log:2", 1, "D")] == 1.0  # dev2 d1 @byte174 (Solarpumpe)
    # dev1 digital d1-d13 stays the documented 0.0 fill (unchanged P=169).
    for ch in range(1, 14):
        assert vals[("cmi-log:1", ch, "D")] == 0.0


def test_p181_detect_period():
    """Q-125 P-detection: a P=181 file is detected as P=181, a P=169 file as
    P=169 (both directions).  The header channel-counter hint (51/48) and the
    time-grid scan agree."""
    from app.cmi.cmi_log import _detect_period

    _, _, body181 = make_log_file_181("data_2026_11_07_16_15_59.log", (23, 10, 0), n_records=31)
    assert _detect_period(body181) == 181

    # A P=169 file must NOT be mis-detected as P=181.
    path, size, body169 = make_log_file("data_2026_10_28_00_00_04.log", (0, 0, 4), n_records=31)
    assert _detect_period(body169) == 169


def test_p181_pending_channels_all_null():
    """Q-125 (Round 5): the P=181-pending channels are now ONLY dev2 a1-a4
    (eliminated, no dev2 payload byte -- the R2 dup-hack is removed).  They
    are emitted as None across ALL decoded records -- never a guessed value.
    The PENDING set must carry the UPPERCASE ad letter so the wide-pivot
    routes it to the correct analog column.  dev1 a1-a16 (Q-125 R5, @66-96)
    and dev0 d2-d9 (Q-125 R5, @6/10-16) are NO LONGER pending: they are
    value-verified in :data:`_CHANNEL_MAP_181` / :data:`_DIGITAL_MAP_181`."""
    from app.cmi.cmi_log import (
        _CHANNEL_MAP_181,
        _CHANNEL_MAP_181_PENDING,
        _DIGITAL_MAP_181,
    )

    path, size, body = make_log_file_181("data_2026_11_07_16_15_59.log", (23, 10, 0), n_records=31)
    data = decode_cmi_log(body, filename=path)
    pending_keys = {
        (
            f"cmi-log:{dev}",
            int(ch[1:]),
            "A" if ch.startswith("a") else "D",
        )
        for (dev, ch) in _CHANNEL_MAP_181_PENDING
    }
    # Sanity: the PENDING set is exactly dev2 a1-a4 (4 channels, R5).
    assert len(pending_keys) == 4
    # R5 invariant: dev1 a1-a16 left PENDING and are now mapped; dev0 a11-a16
    # left PENDING in R3 and are mapped; dev0 d2-d9 mapped in R5; dev2 a5-a19
    # mapped; dev2 a1-a4 the ONLY remaining pending.
    assert {(1, f"a{n}") for n in range(1, 17)}.isdisjoint(_CHANNEL_MAP_181_PENDING)
    assert {(1, f"a{n}") for n in range(1, 17)} <= set(_CHANNEL_MAP_181)
    assert {(0, f"d{n}") for n in range(2, 10)}.isdisjoint(_CHANNEL_MAP_181_PENDING)
    assert {(0, f"d{n}") for n in range(2, 10)} <= set(_DIGITAL_MAP_181)
    assert {(2, f"a{n}") for n in range(1, 5)} == _CHANNEL_MAP_181_PENDING
    n_checked = 0
    for r in data.rows:
        key = (r[1], r[2], r[3])
        if key in pending_keys:
            assert r[4] is None, f"pending channel {key} not None: {r[4]!r}"
            n_checked += 1
    assert n_checked == data.n_decoded * len(pending_keys)


def test_p181_real_file_decodes_73_rows():
    """Q-125 Rosetta (corrected frame + R5 maps): the recorded P=181 primary
    file ``data/cmi_log/data_2026_11_07_16_15_59.log`` (510162 B = 6258 +
    2784x181) decodes ALL 2784 records to 73 rows/record with the
    time-verified dev0/dev1/dev2 values and the P=181-pending channels NULL.

    Grounded on the byte-verified record 0 @ 16:15:59 + the full-night
    Rosetta vs json:1/json:32/WinSol-P=145 (2784 records, ±60 s window):
      * dev0 a6 (VL) = 37.9 °C (json:1 ch6 ~38.5 @ 16:16:33)
      * dev0 a8 (Außen) = 7.4 °C (json:1 ch8 ~7.5)
      * dev0 a11 (VL.Solltemp, Q-125 R3 @byte 41) = 47.7 °C (rec0)
      * dev0 a12 (T.raum.SOLL.eff, Q-125 R3 @byte 43) = 20.0 °C (rec0)
      * dev1 a1 (ras_kell_perc, Q-125 R5 @byte 66) = 20.0 °C (rec0, P=145 GT)
      * dev1 a11 (ras_age_kell, Q-125 R5 @byte 86) = 6.1 min (rec0)
      * dev0 d5 (Anforderung Brenner, Q-125 R5 @byte 12) = 1.0 (rec0)
      * dev0 d8 (Pumpe Heizkreis, Q-125 R5 @byte 15) = 1.0 (rec0)
      * dev0 d9 (Ventil Heizungsrücklauf, Q-125 R5 @byte 16) = 1.0 (rec0)
      * dev2 a17 (Sollspannung 1) = 10.0 V, dev2 a18 = 1.0 V, dev2 a19 = 0.0 V
      * dev2 d1 (Solarpumpe, Q-125 R5 @byte 174) = 0.0 (json:32 ch1 D)
      * dev2 a1 (T.Speicher oben) = None (eliminated, R5), dev2 a13 = 29.1
    The P=181-pending channels (dev2 a1-a4) are NULL; dev1 digital is the
    documented 0.0 fill.  Skipped when the file is not present (data/ is
    .gitignore'd) so the test is host-agnostic.
    """
    from tests.conftest import REPO_ROOT

    fp = REPO_ROOT / "data" / "cmi_log" / "data_2026_11_07_16_15_59.log"
    if not fp.is_file():
        pytest.skip("P=181 primary file not present (data/ is .gitignore'd)")

    raw = fp.read_bytes()
    data = decode_cmi_log(raw, filename=fp.name)
    # All 2784 records decode; first record @ 16:15:59 (record 0 is now
    # DECODED, not skipped as in the old off-by-104 B frame).
    assert data.n_records == 2784
    assert data.n_decoded == 2784
    assert len(data.rows) == data.n_decoded * 73
    assert data.rows[0][0] == "2026-11-07 16:15:59"

    # Per-record value checks on the first decoded record.
    first_ts = data.rows[0][0]
    vals = {(r[1], r[2], r[3]): r[4] for r in data.rows if r[0] == first_ts}
    assert vals[("cmi-log:0", 6, "A")] == 37.9  # VL (json:1 ch6)
    assert vals[("cmi-log:0", 8, "A")] == 7.4  # Außen (json:1 ch8)
    assert abs(vals[("cmi-log:0", 11, "A")] - 47.7) < 0.1  # VL.Solltemp (Q-125 R3)
    assert abs(vals[("cmi-log:0", 12, "A")] - 20.0) < 0.1  # T.raum.SOLL.eff
    assert vals[("cmi-log:0", 13, "A")] == 0.0  # "-----" 0-Fill
    assert vals[("cmi-log:0", 16, "A")] in (0.0, 1.0)  # Ström.Schalt5 0/1-Flag
    # dev1 RAS @66-96 (Q-125 R5, WinSol P=145 GT).
    assert abs(vals[("cmi-log:1", 1, "A")] - 20.0) < 0.05  # ras_kell_perc @66
    assert abs(vals[("cmi-log:1", 8, "A")] - 20.0) < 0.05  # ras_dg_ist @80
    assert abs(vals[("cmi-log:1", 11, "A")] - 6.1) < 0.05  # ras_age_kell @86
    # dev0 digital @6/10-16 (Q-125 R5, WinSol P=145 mask @44 GT).
    assert vals[("cmi-log:0", 5, "D")] == 1.0  # d5 Anforderung Brenner @12
    assert vals[("cmi-log:0", 8, "D")] == 1.0  # d8 Pumpe Heizkreis @15
    assert vals[("cmi-log:0", 9, "D")] == 1.0  # d9 Ventil Heizungsrücklauf @16
    assert vals[("cmi-log:2", 1, "D")] == 0.0  # dev2 d1 Solarpumpe @byte174 (json:32 ch1 D)
    assert vals[("cmi-log:2", 1, "A")] is None  # dev2 a1 ELIMINATED (R5, no payload)
    assert abs(vals[("cmi-log:2", 13, "A")] - 29.1) < 0.05  # Minderer Keller Temp (a13 @146)
    assert vals[("cmi-log:2", 17, "A")] == 10.0  # Sollspannung 1
    assert vals[("cmi-log:2", 18, "A")] == 1.0  # Sollspannung 2
    assert vals[("cmi-log:2", 19, "A")] == 0.0  # Sollspannung 3
    # P=181-pending analog is NULL (dev2 a1-a4 only, after Round 5).
    assert vals[("cmi-log:2", 1, "A")] is None
    assert vals[("cmi-log:2", 4, "A")] is None
    # dev0 d2-d9 are all VALUE-VERIFIED now (R5): not NULL.
    for ch in range(2, 10):
        assert vals[("cmi-log:0", ch, "D")] in (0.0, 1.0)


def test_p181_dev0_a11_a16_full_day_grounding():
    """Q-125 Round 3: the six newly-mapped dev0 channels are verified on the
    FULL-DAY 11-07 file (2784 records, no JSON GT available -- json:1 logs
    ch11+ only as digital), so the proof is the binary pattern itself:
      * a11 (VL.Solltemp, @41): full-day min 5.0 / max 49.8 °C, 23
        distinct values, variance >> 5 K^2 -> Heizkurve behavior (~47.7 °C
        bei Außen 7 °C am Tag, -> 5.0 °C nachts).  Range [0, 70].
      * a12 (T.raum.SOLL.eff, @43): EXACTLY {16.0, 20.0} °C = ABSENK 16 /
        NORMAL 20 -- no other slot byte shows exactly {16, 20}.
      * a13-a15 (@45/47/49): full-day constant 0.0 ("-----" unassigned).
      * a16 (Ström.Schalt5, @51): full-day values in {0, 1} (flow-switch
        flag, semantics identical to P=169 dev0 a16 @slot 77).
    Skipped when the primary file is not present (data/ is .gitignore'd).
    """
    from tests.conftest import REPO_ROOT

    fp = REPO_ROOT / "data" / "cmi_log" / "data_2026_11_07_16_15_59.log"
    if not fp.is_file():
        pytest.skip("P=181 primary file not present (data/ is .gitignore'd)")

    data = decode_cmi_log(fp.read_bytes(), filename=fp.name)
    assert data.n_decoded == 2784

    series: dict[int, list[float]] = {ch: [] for ch in (11, 12, 13, 14, 15, 16)}
    for r in data.rows:
        if r[1] == "cmi-log:0" and r[3] == "A" and r[2] in series:
            assert r[4] is not None, f"dev0 a{r[2]} emitted NULL"
            series[r[2]].append(r[4])

    a11, a12 = series[11], series[12]
    # (c) a11 full-day ⊂ [0, 70] with variance > 5 K^2 (Heizkurve-Beleg).
    assert all(0.0 <= v <= 70.0 for v in a11)
    mean = sum(a11) / len(a11)
    variance = sum((v - mean) ** 2 for v in a11) / len(a11)
    assert variance > 5.0
    assert min(a11) == 5.0 and max(a11) == 49.8
    assert len(set(a11)) == 23
    # (b) a12 full-day ∈ {16.0, 20.0} ONLY.
    assert set(a12) == {16.0, 20.0}
    # (d) a13-a15 = 0.0 full-day; a16 ∈ {0, 1} full-day.
    for ch in (13, 14, 15):
        assert set(series[ch]) == {0.0}, f"dev0 a{ch} not constant 0: {set(series[ch])}"
    assert set(series[16]) <= {0.0, 1.0}
    # rec0/2 spot (task DoD): a11 = 47.7, a12 = 20.0 on the first records.
    ts0, ts2 = data.rows[0][0], data.rows[73 * 2][0]
    for ts in (ts0, ts2):
        vals = {(r[1], r[2], r[3]): r[4] for r in data.rows if r[0] == ts}
        assert abs(vals[("cmi-log:0", 11, "A")] - 47.7) < 0.1
        assert abs(vals[("cmi-log:0", 12, "A")] - 20.0) < 0.1


def test_p181_unit_assignment_round3():
    """Q-125 Round 3 + 5: P=181 dev0 a11/a12 carry unit °C (real setpoint
    temperatures), dev0 a13-a16 carry no unit; R5 adds dev1 a1/a2/a4/a5/
    a7/a8 as °C (RAS temperatures, already in the shared
    :data:`_TEMPERATURE_CHANNELS`) while dev1 a3/a6/a9 (raw counters),
    a10/a14-a16 (unused) and a11-a13 (minute counters) carry no unit.
    P=169 is UNCHANGED (dev0 a11/a12 stay unit=None there -- the shared
    :data:`_TEMPERATURE_CHANNELS` must NOT gain them)."""
    from app.cmi.cmi_log import _TEMPERATURE_CHANNELS, _TEMPERATURE_CHANNELS_181

    assert (0, "a11") not in _TEMPERATURE_CHANNELS
    assert (0, "a12") not in _TEMPERATURE_CHANNELS
    assert (0, "a11") in _TEMPERATURE_CHANNELS_181
    assert (0, "a12") in _TEMPERATURE_CHANNELS_181
    assert (0, "a16") not in _TEMPERATURE_CHANNELS_181  # flow-switch flag

    path, size, body = make_log_file_181("data_2026_11_07_16_15_59.log", (23, 10, 0), n_records=31)
    data = decode_cmi_log(body, filename=path)
    units = {(r[1], r[2], r[3]): r[5] for r in data.rows if r[0] == data.rows[0][0]}
    assert units[("cmi-log:0", 11, "A")] == "\u00b0C"
    assert units[("cmi-log:0", 12, "A")] == "\u00b0C"
    for ch in (13, 14, 15, 16):
        assert units[("cmi-log:0", ch, "A")] is None
    # R5: dev1 RAS temperatures carry °C (inherited P=169 classification);
    # the counters / unused / minute channels carry no unit.
    for ch in (1, 2, 4, 5, 7, 8):
        assert units[("cmi-log:1", ch, "A")] == "\u00b0C"
    for ch in (3, 6, 9, 10, 11, 12, 13, 14, 15, 16):
        assert units[("cmi-log:1", ch, "A")] is None


def test_p181_dev0_a11_a12_store_fill(tmp_path: Path) -> None:
    """Q-125 Round 3 + 5: the store's dev0_analog_11/12 columns (which exist
    since the 74-column schema, were NULL up to the 16:14 switch) are filled
    by P=181 rows; R5 additionally fills dev1_analog_01-16 (RAS) +
    dev0_digital_02-09 + dev2_digital_01, while the eliminated dev2_analog_01
    stays NULL for those rows.  Writes a real P=181 decode into a scratch
    store and checks the wide columns directly."""
    import duckdb

    from app.cmi.cmi_log_pq import CmiLogParquet
    from app.main import Gateway
    from tests.conftest import REPO_ROOT

    fp = REPO_ROOT / "data" / "cmi_log" / "data_2026_11_07_16_15_59.log"
    if not fp.is_file():
        pytest.skip("P=181 primary file not present (data/ is .gitignore'd)")

    data = decode_cmi_log(fp.read_bytes(), filename=fp.name)
    # Wide pivot exactly as the poller does (staticmethod, no Gateway state).
    records = Gateway._cmi_log_wide_records(data)
    assert records, "no wide records from the P=181 decode"

    store = CmiLogParquet(tmp_path / "pq")
    try:
        added = store.write_month("2026-11", records)
        assert added == data.n_decoded
        month = tmp_path / "pq" / "2026" / "11" / "2026-11.parquet"
        con = duckdb.connect()
        try:
            n, a11, a12, a15, a16, dev1a1, dev2a1, d02, d09, d2_1 = con.execute(
                f"""
                SELECT COUNT(*),
                       COUNT(dev0_analog_11), COUNT(dev0_analog_12),
                       COUNT(dev0_analog_15), COUNT(dev0_analog_16),
                       COUNT(dev1_analog_01), COUNT(dev2_analog_01),
                       COUNT(dev0_digital_02), COUNT(dev0_digital_09),
                       COUNT(dev2_digital_01)
                FROM read_parquet('{month}')
                """
            ).fetchone()
        finally:
            con.close()
        assert n == data.n_decoded
        # dev0 a11/a12 (VL.Solltemp / T.raum.SOLL.eff) filled from 16:14 on.
        assert a11 == n and a12 == n
        # a15/a16 (0-Fill / flow flag) also filled now (were NULL under R2).
        assert a15 == n and a16 == n
        # R5: dev1 a1-a16 (RAS) filled; dev2 a1-a4 eliminated -> NULL.
        assert dev1a1 == n
        assert dev2a1 == 0
        # R5: dev0 digital d2-d9 + dev2 d1 filled.
        assert d02 == n and d09 == n and d2_1 == n
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Q-125 Round 5: Rosetta tests (dev1 RAS, dev0 digital, dev2 d1 @174, dev2
# a1-a4 elimination) against the WinSol P=145 ground truth.
# ---------------------------------------------------------------------------
#: WinSol-LogX ground-truth directory (outside the repo; the tests SKIP when
#: absent -- host-agnostic).  P=145 session: 11-07 from ~16:15:59 (the
#: format switch day, UVR-Upload), 11-08 the whole day.
_WINSOL_LOGX_2026 = Path(
    "/home/user/Documents/Technische-Alternative/Winsol/Infosol/example-plant/LogX/2026"
)
#: WinSol P=145 session separator marker (LogX session delimiter).
_WINSOL_SESSION_MARKER = b">>>>"
#: P=145 dev1 RAS offsets (byte -> channel): the +8-B restructured layout
#: (dev0/dev1 block shifted 4 B earlier vs P=137).
_P145_DEV1_OFFS: dict[int, int] = {
    1: 46,
    2: 48,
    3: 50,
    4: 54,
    5: 56,
    6: 58,
    7: 62,
    8: 64,
    9: 66,
    11: 74,
    12: 76,
    13: 78,
}
#: P=145 dev1 channels that are raw RAS counters (/1, not /10).
_P145_DEV1_RAW: frozenset[int] = frozenset({3, 6, 9})
#: P=145 dev0 digital mask bit permutation (identical to the P=137 canon,
#: :data:`analysis.winsol_logx.DEV0_DIGIT_BITS`): channel -> mask bit.
_P145_DEV0_DIG_BITS: dict[int, int] = {
    8: 0,
    7: 1,
    6: 2,
    5: 3,
    4: 4,
    3: 5,
    2: 6,
    9: 15,
}
#: CMI P=181 dev0 digital byte-per-channel (Q-125 Round 5): channel -> byte.
_P181_DEV0_DIG_BYTES: dict[int, int] = {
    2: 6,
    3: 10,
    4: 11,
    5: 12,
    6: 13,
    7: 14,
    8: 15,
    9: 16,
}


def _p145_sessions(buf: bytes) -> list[tuple[int, int]]:
    """All (start, end) P=145 sessions in a WinSol-LogX day file."""
    marks: list[int] = []
    m = buf.find(_WINSOL_SESSION_MARKER)
    while m >= 0:
        marks.append(m + 4)
        m = buf.find(_WINSOL_SESSION_MARKER, m + 4)
    ends = marks[1:] + [len(buf)]
    if not marks:
        return []
    return [(s, e) for s, e in zip(marks, ends, strict=True) if e - s >= 500]


def _p145_records(buf: bytes, start: int, end: int) -> list[tuple[int, int]]:
    """P=145 records (ts-seconds, record-offset) in [start, end)."""
    p = 145
    out: list[tuple[int, int]] = []
    for k in range((end - start) // p):
        i = start + p * k
        if i + p > end:
            break
        hh, mm, ss = buf[i + 1], buf[i + 2], buf[i + 3]
        if hh <= 23 and mm <= 59 and ss <= 59:
            out.append((hh * 3600 + mm * 60 + ss, i))
    return out


def _p181_records(fp: Path) -> list[tuple[int, int]]:
    """CMI P=181 records (ts-seconds, record-offset) via the sync-grid."""
    from app.cmi.cmi_log import _detect_data_start

    buf = fp.read_bytes()
    start = _detect_data_start(buf, 181)
    out: list[tuple[int, int]] = []
    for k in range((len(buf) - start) // 181):
        i = start + 181 * k
        hh, mm, ss = buf[i + 5], buf[i + 4], buf[i + 3]
        if hh <= 23 and mm <= 59 and ss <= 59:
            out.append((hh * 3600 + mm * 60 + ss, i))
    return out


def _rossetta_pair_counts(
    gbuf: bytes,
    grecs: list[tuple[int, int]],
    g_off: int,
    g_bit: int,
    g_is_u16: bool,
    cbuf: bytes,
    crecs: list[tuple[int, int]],
    c_off: int,
    c_bit: int,
) -> tuple[int, int, int, int]:
    """(match, compared, cmi_toggles, gt_toggles) at exact common ts.

    The GT side reads a bit of the record byte at ``g_off`` -- or, when
    ``g_is_u16``, of the little-endian u16 at ``g_off`` (the WinSol P=145
    dev0 digital mask @ byte 44: the value is the u16, so bit 15 for d9
    sits in byte 45).  The CMI side reads bit ``c_bit`` of the record byte
    at ``c_off`` (P=181 byte-per-channel, bit 0)."""
    gt: dict[int, int] = {}
    for t, i in grecs:
        raw = struct.unpack_from("<H", gbuf, i + g_off)[0] if g_is_u16 else gbuf[i + g_off]
        gt[t] = (raw >> g_bit) & 1
    shared = sorted(set(gt) & {t for t, _ in crecs})
    cmi_by_ts = {t: (cbuf[i + c_off] >> c_bit) & 1 for t, i in crecs}
    match = cmp = 0
    cmi_tg = gt_tg = 0
    prev_c: int | None = None
    prev_g: int | None = None
    for t in shared:
        c = cmi_by_ts[t]
        g = gt[t]
        if prev_c is not None and c != prev_c:
            cmi_tg += 1
        if prev_g is not None and g != prev_g:
            gt_tg += 1
        prev_c, prev_g = c, g
        cmp += 1
        match += c == g
    return match, cmp, cmi_tg, gt_tg


def test_p181_rossetta_dev1_ras_vs_p145_gt():
    """Q-125 Round 5: dev1 RAS a1-a9/a11-a13 @66-90 (even) are 100 %
    value-identical vs the WinSol P=145 GT (both P=181 files, exact-ts
    alignment, >= 500 samples per day), and a10/a14-a16 stay 0-Fill.
    Skipped when the WinSol LogX files are absent (host-agnostic)."""
    import struct

    from app.cmi.cmi_log import _CHANNEL_MAP_181
    from tests.conftest import REPO_ROOT

    days = [
        ("2026-11-07", "data_2026_11_07_16_15_59.log", -1),  # last P145 session
        ("2026-11-08", "data_2026_11_08_00_00_09.log", 0),  # first P145 session
    ]
    for day, cmi_name, sess_idx in days:
        ws_fp = _WINSOL_LOGX_2026 / f"D{day}.log"
        cmi_fp = REPO_ROOT / "data" / "cmi_log" / cmi_name
        if not (ws_fp.is_file() and cmi_fp.is_file()):
            pytest.skip("WinSol P=145 GT / CMI P=181 file not present")
        buf = ws_fp.read_bytes()
        cbuf = cmi_fp.read_bytes()
        sessions = _p145_sessions(buf)
        start, end = sessions[sess_idx]
        recs = _p145_records(buf, start, end)
        crecs = _p181_records(cmi_fp)
        assert len(recs) >= 500 and len(crecs) >= 500
        for ch in (1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13):
            c_off = _CHANNEL_MAP_181[(1, f"a{ch}")][0]
            scale = 1 if ch in _P145_DEV1_RAW else 10
            match = cmp = 0
            gt_by_ts = {t: i for t, i in recs}
            for t, i in crecs:
                gi = gt_by_ts.get(t)
                if gi is None or gi + _P145_DEV1_OFFS[ch] + 2 > end:
                    continue
                cmp += 1
                g = struct.unpack_from("<h", buf, gi + _P145_DEV1_OFFS[ch])[0] / scale
                c = struct.unpack_from("<h", cbuf, i + c_off)[0] / scale
                match += g == c
            assert cmp >= 500, f"dev1 a{ch}: {cmp} aligned samples (< 500)"
            assert match / cmp >= 0.98, f"dev1 a{ch}: {match}/{cmp}"
        # a10/a14-a16: 0-Fill on the CMI side (both files, full-day).
        for ch in (10, 14, 15, 16):
            c_off = _CHANNEL_MAP_181[(1, f"a{ch}")][0]
            for _t, i in crecs:
                assert struct.unpack_from("<h", cbuf, i + c_off)[0] == 0, f"a{ch}"


def test_p181_rossetta_dev0_digital_vs_p145_mask():
    """Q-125 Round 5: dev0 digital d2-d9 (byte-per-channel, bit0) match the
    WinSol P=145 dev0 mask @ byte 44 on 100 % of the aligned samples with
    IDENTICAL toggle counts, both P=181 files (>= 500 samples/day).  This is
    the d8/d9 Rosetta-golden: d8 @ byte 15, d9 @ byte 16.  Skipped when the
    WinSol LogX files are absent (host-agnostic)."""
    from tests.conftest import REPO_ROOT

    days = [
        ("2026-11-07", "data_2026_11_07_16_15_59.log", -1),
        ("2026-11-08", "data_2026_11_08_00_00_09.log", 0),
    ]
    for day, cmi_name, sess_idx in days:
        ws_fp = _WINSOL_LOGX_2026 / f"D{day}.log"
        cmi_fp = REPO_ROOT / "data" / "cmi_log" / cmi_name
        if not (ws_fp.is_file() and cmi_fp.is_file()):
            pytest.skip("WinSol P=145 GT / CMI P=181 file not present")
        buf = ws_fp.read_bytes()
        cbuf = cmi_fp.read_bytes()
        sessions = _p145_sessions(buf)
        start, end = sessions[sess_idx]
        recs = _p145_records(buf, start, end)
        crecs = _p181_records(cmi_fp)
        for ch, c_off in _P181_DEV0_DIG_BYTES.items():
            match, cmp, cmi_tg, gt_tg = _rossetta_pair_counts(
                buf,
                recs,
                44,
                _P145_DEV0_DIG_BITS[ch],
                True,
                cbuf,
                crecs,
                c_off,
                0,
            )
            assert cmp >= 500, f"dev0 d{ch}: {cmp} aligned samples (< 500)"
            assert match / cmp >= 0.98, f"dev0 d{ch}: {match}/{cmp}"
            assert cmi_tg == gt_tg, (
                f"dev0 d{ch}: toggle-count mismatch (CMI {cmi_tg} vs GT {gt_tg})"
            )


def test_p181_rossetta_dev2_d1_byte174_vs_p145():
    """Q-125 Round 5 CORRECTION: dev2 d1 (Solarpumpe) is @ byte 174 bit0, NOT
    the R2 byte 10.  Proof vs the WinSol P=145 dev2 digital byte @ 144 bit7
    (both P=181 files, exact ts, >= 500 samples/day) AND the P=145 dev0 mask
    @ byte 44 does NOT sit at byte 10 (that is dev0 d3, Mischkreis Auf).
    Skipped when the WinSol LogX files are absent (host-agnostic)."""
    from tests.conftest import REPO_ROOT

    days = [
        ("2026-11-07", "data_2026_11_07_16_15_59.log", -1),
        ("2026-11-08", "data_2026_11_08_00_00_09.log", 0),
    ]
    for day, cmi_name, sess_idx in days:
        ws_fp = _WINSOL_LOGX_2026 / f"D{day}.log"
        cmi_fp = REPO_ROOT / "data" / "cmi_log" / cmi_name
        if not (ws_fp.is_file() and cmi_fp.is_file()):
            pytest.skip("WinSol P=145 GT / CMI P=181 file not present")
        buf = ws_fp.read_bytes()
        cbuf = cmi_fp.read_bytes()
        sessions = _p145_sessions(buf)
        start, end = sessions[sess_idx]
        recs = _p145_records(buf, start, end)
        crecs = _p181_records(cmi_fp)
        # byte174 bit0 = P145 @144 bit7 (Solarpumpe), 100 % on both days.
        match, cmp, _ctg, _gtg = _rossetta_pair_counts(
            buf, recs, 144, 7, False, cbuf, crecs, 174, 0
        )
        assert cmp >= 500, f"dev2 d1: {cmp} aligned samples (< 500)"
        assert match / cmp >= 0.98, f"dev2 d1: {match}/{cmp}"
        # byte10 bit0 is dev0 d3 (Mischkreis Auf, P145 mask bit 5), NOT the
        # solar pump: it must match P145 @44 bit5 instead.
        match_d3, cmp_d3, _c, _g = _rossetta_pair_counts(buf, recs, 44, 5, True, cbuf, crecs, 10, 0)
        assert cmp_d3 >= 500
        assert match_d3 / cmp_d3 >= 0.98, (
            f"byte10 is dev0 d3: {match_d3}/{cmp_d3} vs P145 mask bit5"
        )


def test_p181_dev2_a1_a4_eliminated_null():
    """Q-125 Round 5: dev2 a1-a4 (T.Speicher oben/mitte/unten + Keller RAS)
    have NO dev2 payload in the P=181 record (CAN-only, UVR1611 master logs
    the Speicher-Temperaturen at dev0 a1-a3; maintainer q125-doc 2c).  The R2
    dup-hack (a1-a4 ≡ a13-a16 @146/150/154/158) is removed: the bytes
    @146/150/154/158 carry the Minderer valve temperatures (dev2 a13-a16,
    value-verified).  Assert: (a) PENDING set = dev2 a1-a4 only, (b) the map
    no longer maps dev2 a1-a4, (c) dev2 a13-a16 keep their @146-158 slots."""
    from app.cmi.cmi_log import _CHANNEL_MAP_181, _CHANNEL_MAP_181_PENDING

    assert frozenset({(2, "a1"), (2, "a2"), (2, "a3"), (2, "a4")}) == _CHANNEL_MAP_181_PENDING
    for ch in range(1, 5):
        assert (2, f"a{ch}") not in _CHANNEL_MAP_181
    # a13-a16 keep the verified Minderer-Temp slots.
    assert _CHANNEL_MAP_181[(2, "a13")] == (146, True, 10)
    assert _CHANNEL_MAP_181[(2, "a14")] == (150, True, 10)
    assert _CHANNEL_MAP_181[(2, "a15")] == (154, True, 10)
    assert _CHANNEL_MAP_181[(2, "a16")] == (158, True, 10)

    path, size, body = make_log_file_181("data_2026_11_07_16_15_59.log", (23, 10, 0), n_records=5)
    data = decode_cmi_log(body, filename=path)
    for r in data.rows:
        if r[1] == "cmi-log:2" and r[3] == "A" and 1 <= r[2] <= 4:
            assert r[4] is None, f"dev2 a{r[2]} must be documented NULL"
            assert r[5] is None
            assert r[6] == "cmi-log"
    """Q-125: a P=181 transition fragment that carries only the header (or the
    header + a truncated record) and NO valid +10 s time window decodes to 0
    records instead of raising (the poller still marks it ingested)."""
    from tests.conftest import REPO_ROOT

    # Header-only fragment (the recorded 16_15_19 fragment decodes to 0 recs).
    fp = REPO_ROOT / "data" / "cmi_log" / "data_2026_11_07_16_15_19.log"
    if fp.is_file():
        data = decode_cmi_log(fp.read_bytes(), filename=fp.name)
        assert data.n_decoded == 0
        assert data.rows == []
    else:
        # Synthetic header-only P=181 body: magic + counter + 0x43 header, no
        # sync tail / no time-window -> 0 records, no raise.
        body = b"8D312121\r\n" + struct.pack("<H", 51) + b"\x43" * 800
        data = decode_cmi_log(body, filename="data_2026_11_07_16_15_19.log")
        assert data.n_decoded == 0
        assert data.rows == []


def test_p181_data_start_layout_agnostic():
    """Q-125: the P=181 data-start is found by the layout-agnostic sync-scan,
    NOT by a hard-coded 6258.  Varying the header size (and thus the data-start
    offset) must still locate the first record; the discovered start is NOT a
    multiple of 181 in general."""
    from app.cmi.cmi_log import _detect_data_start

    for header in (160, 320, 640, 1200):
        path, size, body = make_log_file_181(
            "data_2026_11_07_16_15_59.log",
            (23, 10, 0),
            n_records=31,
            header_bytes=header,
        )
        ds = _detect_data_start(body, 181)
        expected = 10 + 2 + header
        assert ds == expected, f"header={header}: data_start {ds} != {expected}"
        data = decode_cmi_log(body, filename=path)
        assert data.n_decoded == 31


def _p181_window_two_records(hh: int = 23, mm: int = 10, ss: int = 0) -> bytes:
    """Two valid P=181 records back-to-back (23:10:00, 23:10:10) for window
    probes: record 0 is fully valid, record 1 is valid so the +10 s step holds
    when record 0 is intact."""
    return _record_bytes_181(hh, mm, ss, {}, {}) + _record_bytes_181(hh, mm, ss + 10, {}, {})


def test_p181_valid_window_rejects_corrupt_record():
    """Q-125: `_valid_181_window` returns False when ANY record in the window
    carries a bad sync tail, a missing CRLF terminator, an out-of-range time
    byte, or a non-+10 s step.  These are the rejection paths that keep the
    P=181 data-start detection from locking onto garbage."""
    from app.cmi.cmi_log import _valid_181_window

    p = 181
    good = _p181_window_two_records()
    assert _valid_181_window(good, 0, p, 2) is True

    # (1) Bad sync tail on record 0 (byte 1/2 not 0B 1A).
    buf = bytearray(good)
    buf[1:3] = b"\x00\x00"
    assert _valid_181_window(bytes(buf), 0, p, 2) is False

    # (2) Missing CRLF terminator on record 0 (bytes 179/180).
    buf = bytearray(good)
    buf[179:181] = b"\x00\x00"
    assert _valid_181_window(bytes(buf), 0, p, 2) is False

    # (3) Out-of-range time byte on record 0 (ss > 59 -> byte 3 = 0x40 = 64).
    buf = bytearray(good)
    buf[3] = 64  # 0x40 -> ss = 64 > 59
    assert _valid_181_window(bytes(buf), 0, p, 2) is False

    # (4) Bad step on record 1 (23:10:30 instead of 23:10:10 -> +30 s, not +10 s).
    buf = bytearray(good)
    buf[181 + 3] = 30  # record 1 ss = 30 -> step = 30 s > 11
    assert _valid_181_window(bytes(buf), 0, p, 2) is False

    # (5) Trailing record extends past the buffer end (b + p > len(buf)).
    assert _valid_181_window(good, 0, p, 3) is False


def test_p181_no_data_start_raises():
    """Q-125: `_detect_data_start_181` raises ValueError when NO sync+CRLF+
    10 s grid exists anywhere in the body (header-only fragment).  This is the
    direct-detection path; `decode_cmi_log` wraps it in the zero-record
    tolerance (no raise, 0 records)."""
    from app.cmi.cmi_log import _detect_data_start_181

    # Header-only P=181 body (magic + counter + 0x43 header, no records):
    # no sync tail -> the find loop terminates without a hit -> ValueError.
    body = b"8D312121\r\n" + struct.pack("<H", 51) + b"\x43" * 800
    with pytest.raises(ValueError, match="no P=181 data-start"):
        _detect_data_start_181(body)


def test_p181_scan_skips_stray_sync_tail_in_header():
    """Q-125: a stray ``0B 1A`` byte pair in the header (e.g. a corrupted
    descriptor block) must NOT lock the P=181 data-start.  The scan tries it,
    the +10 s window fails (no CRLF grid on the 181-stride from there), the
    scan CONTINUES, and the real data start is found at the first record."""
    from app.cmi.cmi_log import _detect_data_start_181

    path, size, body = make_log_file_181(
        "data_2026_11_07_16_15_59.log",
        (23, 10, 0),
        n_records=31,
        header_bytes=320,
    )
    # Inject a stray 0B 1A at an early header offset (inside the 0x43 header,
    # before any real record).  It is the FIRST sync-tail hit, but it does not
    # carry a valid +10 s grid on the 181-stride -> the scan must skip it.
    data = bytearray(body)
    data[50:52] = P181_SYNC
    body = bytes(data)
    # Real data start = 10 (magic) + 2 (counter) + 320 (header) = 332.
    assert _detect_data_start_181(body) == 332


def test_channel_map_matches_logx_canon():
    """Q-121 regression guard: the CMI ``_CHANNEL_MAP`` must agree with the
    WinSol-LogX canon on the SIGNEDNESS of every analog/flag/raw channel.

    For all 48 (dev, ch) pairs with a LogX field of kind in
    ``('analog', 'raw', 'flag')`` the ``signed`` flag must be identical; a
    divergence means a negative analog value would be misread (the exact
    bug class of the dev2 a16 collector 0xFFFF = -0.1 °C case).

    Scale may differ ONLY where documented:
      * dev0 a16: CMI /1 flag vs LogX /1 flag -> same scale here too, but the
        CMI-side flag fill (0/1) is a documented structural choice;
      * dev2 a10-a12: CMI unsigned u16 /100 (0-10 V Minderer) vs LogX canon
        ``Field(..., 4, False, 100)`` u32/100 -- unsigned is REQUIRED (a
        0-10 V voltage can never be negative).
    Any OTHER signed/scale divergence is a bug and fails the test.
    """
    from analysis.winsol_logx import DEV0, DEV1, _dev2_137
    from app.cmi.cmi_log import _CHANNEL_MAP

    canon = {
        0: DEV0,
        1: DEV1,
        2: _dev2_137(),  # current P=137 variant
    }

    # Channels where a scale difference is documented (see module docstring).
    # signed must still match for ALL of these; scale is checked leniently.
    scale_exempt = {(0, "a16")}
    # dev2 a10-a12: intentionally unsigned on BOTH sides (0-10 V), u16 vs u32.
    unsigned_minderer = {(2, "a10"), (2, "a11"), (2, "a12")}

    compared = 0
    for dev in (0, 1, 2):
        for ch in range(1, 17):
            key = (dev, f"a{ch}")
            assert key in _CHANNEL_MAP, f"{key} missing from _CHANNEL_MAP"
            _slot, cmi_signed, cmi_scale = _CHANNEL_MAP[key]
            canon_fields = {f.ch: f for f in canon[dev]}
            assert f"a{ch}" in canon_fields, f"canon missing {key}"
            cf = canon_fields[f"a{ch}"]
            if cf.kind not in ("analog", "raw", "flag", "unknown"):
                continue
            compared += 1
            # --- signed parity (the Q-121 regression protection) ---
            if key in unsigned_minderer:
                # Minderer 0-10 V: unsigned on BOTH the CMI and the LogX side.
                assert cmi_signed is False, f"{key}: CMI signed=True (Minderer 0-10 V)"
                assert cf.signed is False, f"{key}: LogX canon signed=True"
            else:
                assert cmi_signed == cf.signed, (
                    f"Q-121 SIGNEDNESS DIVERGENCE {key}: CMI signed="
                    f"{cmi_signed} vs LogX canon signed={cf.signed} "
                    f"(kind={cf.kind}, scale CMI={cmi_scale}/LogX={cf.scale})"
                )
            # --- scale parity (lenient where documented) ---
            if key in scale_exempt:
                continue
            assert cmi_scale == cf.scale, (
                f"Q-121 SCALE DIVERGENCE {key}: CMI /{cmi_scale} vs LogX "
                f"/{cf.scale} (document it in _CHANNEL_MAP + this test)"
            )
    assert compared == 48, f"compared {compared} channels, expected 48"


# ---------------------------------------------------------------------------
# Download + fetch layer (MockTransport, no network)
# ---------------------------------------------------------------------------
async def test_download_success(tmp_path):
    path, size, body = make_log_file("data_2026_10_28_00_00_04.log", (0, 0, 4))
    srv = LogServer([(path, size, body)])
    client = CMILogClient("http://cmi.test", "u", "p", transport=srv.transport())
    f = CmiLogFile(path=path, size=size, year=YEAR, month=10, day=28)
    content, sha = await download_data_file(client, tmp_path, f)
    await client.aclose()
    assert content == body
    assert sha == hashlib.sha256(body).hexdigest()
    assert (tmp_path / f.name).read_bytes() == body
    assert not (tmp_path / f"{f.name}.part").exists()
    assert srv.calls["data"] == [path]


async def test_download_size_mismatch(tmp_path):
    path, size, body = make_log_file("data_2026_10_28_00_00_04.log", (0, 0, 4))
    srv = LogServer([(path, size, body)])
    client = CMILogClient("http://cmi.test", "u", "p", transport=srv.transport())
    f = CmiLogFile(path=path, size=size + 1, year=YEAR, month=10, day=28)
    with pytest.raises(CmiLogError, match="size mismatch"):
        await download_data_file(client, tmp_path, f)
    await client.aclose()
    assert not (tmp_path / f"{f.name}.part").exists()  # temp cleaned up


async def test_fetch_skips_already_ingested(tmp_path):
    p1, s1, b1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    p2, s2, b2 = make_log_file("data_2026_10_28_00_00_04.log", (0, 0, 4))
    srv = LogServer([(p1, s1, b1), (p2, s2, b2)])
    client = CMILogClient("http://cmi.test", "u", "p", transport=srv.transport())
    files = await fetch_log_index(client, YEAR)
    # Name+SIZE dedup (S4N-LogPoll-DedupFix): p1 was ingested at exactly the
    # index size -> skipped; p2 is new (name absent) -> fetched.
    decoded, now = await fetch_cmi_log_files(
        client, tmp_path, files, ingested={"data_2026_10_27_00_00_04.log": s1}
    )
    await client.aclose()
    assert [d.name for d in decoded] == ["data_2026_10_28_00_00_04.log"]
    assert srv.calls["data"] == [p2]  # only the new file was downloaded
    assert now == {"data_2026_10_27_00_00_04.log", "data_2026_10_28_00_00_04.log"}
    assert decoded[0].size == s2
    assert decoded[0].sha256 == hashlib.sha256(b2).hexdigest()


async def test_fetch_refetches_grown_file(tmp_path):
    """Same name + LARGER index size -> the file is re-fetched
    (S4N-LogPoll-DedupFix: the active block keeps growing while it is listed
    in the index; the stored size no longer matches)."""
    p1, s1, b1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    p2, s2, b2 = make_log_file("data_2026_10_28_00_00_04.log", (0, 0, 4))
    srv = LogServer([(p1, s1, b1), (p2, s2, b2)])
    client = CMILogClient("http://cmi.test", "u", "p", transport=srv.transport())
    files = await fetch_log_index(client, YEAR)
    decoded, now = await fetch_cmi_log_files(
        client,
        tmp_path,
        files,
        ingested={
            "data_2026_10_27_00_00_04.log": s1 - 1,  # stored size is stale
            "data_2026_10_28_00_00_04.log": s2,
        },  # exact match -> skip
    )
    await client.aclose()
    assert [d.name for d in decoded] == ["data_2026_10_27_00_00_04.log"]
    assert srv.calls["data"] == [p1]  # ONLY the grown file was re-downloaded
    assert decoded[0].size == s1
    assert now == {"data_2026_10_27_00_00_04.log", "data_2026_10_28_00_00_04.log"}


async def test_fetch_grown_file_index_smaller_than_stored(tmp_path):
    """Same name + SMALLER index size -> re-fetched as well.

    Should never happen in practice (the active block only grows while
    listed; a new day's file has a new name), but the decision is defined
    consistently with the grown case: ANY size difference (stored != index)
    means "re-fetch" -- only an exact match skips.  This keeps the rule a
    single, cheap equality check and makes the gate's ``meta[name] == f.size``
    condition the exact dual of the selection rule.
    """
    p1, s1, b1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    srv = LogServer([(p1, s1, b1)])
    client = CMILogClient("http://cmi.test", "u", "p", transport=srv.transport())
    files = await fetch_log_index(client, YEAR)
    decoded, _now = await fetch_cmi_log_files(
        client,
        tmp_path,
        files,
        ingested={"data_2026_10_27_00_00_04.log": s1 + 100},  # stored is bigger
    )
    await client.aclose()
    assert [d.name for d in decoded] == ["data_2026_10_27_00_00_04.log"]
    assert srv.calls["data"] == [p1]
    """Same name + SMALLER index size -> re-fetched as well.

    Should never happen in practice (the active block only grows while
    listed; a new day's file has a new name), but the decision is defined
    consistently with the grown case: ANY size difference (stored != index)
    means "re-fetch" -- only an exact match skips.  This keeps the rule a
    single, cheap equality check and makes the gate's ``meta[name] == f.size``
    condition the exact dual of the selection rule.
    """
    p1, s1, b1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    srv = LogServer([(p1, s1, b1)])
    client = CMILogClient("http://cmi.test", "u", "p", transport=srv.transport())
    files = await fetch_log_index(client, YEAR)
    decoded, _now = await fetch_cmi_log_files(
        client,
        tmp_path,
        files,
        ingested={"data_2026_10_27_00_00_04.log": s1 + 100},  # stored is bigger
    )
    await client.aclose()
    assert [d.name for d in decoded] == ["data_2026_10_27_00_00_04.log"]
    assert srv.calls["data"] == [p1]


async def test_fetch_bad_magic_is_skipped_not_fatal(tmp_path):
    """A corrupt data file (bad magic) must not abort the other files."""
    p1, s1, b1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    bad_name = "data_2026_10_28_00_00_04.log"
    bad = b"XXXX" + b"\x43" * (RECORD_PERIOD * 40)
    srv = LogServer([(p1, s1, b1), (f"/LOG/{YEAR}/{bad_name}", len(bad), bad)])
    client = CMILogClient("http://cmi.test", "u", "p", transport=srv.transport())
    files = await fetch_log_index(client, YEAR)
    decoded, now = await fetch_cmi_log_files(client, tmp_path, files, ingested={})
    await client.aclose()
    assert [d.name for d in decoded] == ["data_2026_10_27_00_00_04.log"]
    assert bad_name not in now  # undecodable file is NOT marked ingested


# ---------------------------------------------------------------------------
# CMILogClient: auth / status mapping
# ---------------------------------------------------------------------------
async def test_client_get_bytes_500():
    srv = LogServer([])
    client = CMILogClient("http://cmi.test", "u", "p", transport=srv.transport())
    with pytest.raises(CmiLogError) as exc:
        await client.get_bytes("/LOG/does-not-exist.log")
    await client.aclose()
    assert exc.value.http_status == 404


async def test_client_get_bytes_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    client = CMILogClient("http://cmi.test", "u", "p", transport=httpx.MockTransport(handler))
    with pytest.raises(CMIAuthError):
        await client.get_bytes("/LOG/info2026.log")
    await client.aclose()


async def test_client_sends_basic_auth():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="CMI\r\n")

    client = CMILogClient(
        "http://cmi.test", "cmiuser", "cmipass", transport=httpx.MockTransport(handler)
    )
    await client.get_bytes("/LOG/info2026.log")
    await client.aclose()
    assert seen[0].headers["authorization"].startswith("Basic ")
    assert seen[0].url.path == "/LOG/info2026.log"


# ---------------------------------------------------------------------------
# Poller DoD (a)-(d)
# ---------------------------------------------------------------------------
async def test_dod_a_only_new_file_ingested_then_cleared(settings):
    """(a) Index with 2 files, 1 new -> download/decode/upsert ONLY the new
    file; since the old one was already ingested, the buffer is cleared.
    (S4N-LogParquet A: the legacy clear gate now requires CMI_LOG_CLEAR=True;
    the read-only default is covered in tests/test_pollers.py.)"""
    f1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    f2 = make_log_file("data_2026_10_28_00_00_04.log", (0, 0, 4))
    srv = LogServer([f1, f2])
    settings.cmi_log_clear = True
    gw = _log_gateway(settings, srv)
    try:
        gw.history.cmi_log_mark(f1[0].rsplit("/", 1)[-1], f1[1], "old-sha", now_iso())
        await gw._cmi_log_cycle()
        assert srv.calls["data"] == [f2[0]]  # only the new file was fetched
        assert srv.calls["clear"] == 1  # all files ingested -> clear ok
        assert set(gw.history.cmi_log_ingested_names()) == {
            f1[0].rsplit("/", 1)[-1],
            f2[0].rsplit("/", 1)[-1],
        }
        size, sha, _ts = gw.history.cmi_log_get(f2[0].rsplit("/", 1)[-1])
        assert size == f2[1]
        assert sha == hashlib.sha256(f2[2]).hexdigest()
        # S4N-LogParquet: the new file's rows live in the cmi-log Parquet
        # store (month files), NOT in history.db.
        pq_files = sorted(
            p.name
            for p in (settings.cmi_log_parquet_dir / str(YEAR)).rglob("*.parquet")
            if p.is_file()
        )
        assert pq_files == ["2026-10.parquet"]
        import pyarrow.parquet as pq

        table = pq.read_table(settings.cmi_log_parquet_dir / str(YEAR) / "10" / "2026-10.parquet")
        assert len(table) == 39  # only the new file's records (record 0 skipped)
        assert "source" not in table.schema.names  # wide schema, not long rows
        assert (
            gw.history._ensure_conn()
            .execute("SELECT COUNT(*) FROM measurements WHERE source='cmi-log'")
            .fetchone()[0]
            == 0
        )
        assert gw._cmi_log_error is None
    finally:
        await gw.aclose()


async def test_dod_b_no_clear_when_not_all_ingested(settings):
    """(b) One file fails to download -> NOT all index files are ingested ->
    no clear (last-data-first)."""
    f1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    f2 = make_log_file("data_2026_10_28_00_00_04.log", (0, 0, 4))
    srv = LogServer([f1, f2], data_fail={f2[0]: 500})
    settings.cmi_log_clear = True
    gw = _log_gateway(settings, srv)
    try:
        await gw._cmi_log_cycle()
        assert srv.calls["clear"] == 0
        assert gw.history.cmi_log_ingested_names() == {f1[0].rsplit("/", 1)[-1]}
        con = gw.history._ensure_conn()
        assert (
            con.execute("SELECT COUNT(*) FROM measurements WHERE source='cmi-log'").fetchone()[0]
            == 0
        )
        assert gw._cmi_log_error is None  # per-file failure is swallowed
    finally:
        await gw.aclose()


async def test_dod_c_clear_failure_latches_and_retries(settings):
    """(c) Clear error -> poller survives, health latches, next cycle retries
    the clear and clears the flag on success."""
    f1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    srv = LogServer([f1], clear_status=500, clear_body=b"boom")
    settings.cmi_log_poll_sec = 0.2  # fast retry loop for the test
    settings.cmi_log_clear = True
    gw = _log_gateway(settings, srv)
    try:
        gw.history.cmi_log_mark(f1[0].rsplit("/", 1)[-1], f1[1], "sha", now_iso())
        task = asyncio.create_task(gw._cmi_log_poller())
        try:
            await _wait_for(lambda: gw._cmi_log_error is not None, timeout=3.0)
            assert "500" in gw._cmi_log_error
            assert not task.done(), "poller must survive the clear failure"
            assert srv.calls["clear"] == 1
            # next cycle: clear succeeds -> flag cleared, retry counted
            srv.clear_status = 200
            srv.clear_body = b"OK"
            await _wait_for(
                lambda: gw._cmi_log_error is None and srv.calls["clear"] >= 2,
                timeout=3.0,
            )
            assert gw.cmi_log_status()["last_ingested_files"] == 0
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        await gw.aclose()


async def test_dod_d_download_500_no_clear(settings):
    """(d) 500 on every download -> no clear, nothing ingested, poller lives."""
    f1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    f2 = make_log_file("data_2026_10_28_00_00_04.log", (0, 0, 4))
    srv = LogServer([f1, f2], data_fail={f1[0]: 500, f2[0]: 500})
    settings.cmi_log_clear = True
    gw = _log_gateway(settings, srv)
    try:
        await gw._cmi_log_cycle()
        assert srv.calls["clear"] == 0
        assert gw.history.cmi_log_ingested_names() == set()
        assert gw._cmi_log_error is None
    finally:
        await gw.aclose()


async def test_dod_d_timeout_no_clear(settings):
    """(d) Timeouts on the downloads behave like the 500 case."""
    f1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    srv = LogServer([f1], data_timeout={f1[0]})
    settings.cmi_log_clear = True
    gw = _log_gateway(settings, srv)
    try:
        await gw._cmi_log_cycle()
        assert srv.calls["clear"] == 0
        assert gw.history.cmi_log_ingested_names() == set()
        assert gw._cmi_log_error is None
    finally:
        await gw.aclose()


async def test_index_404_is_a_noop(settings):
    """A missing year index (e.g. New Year) is not an error: no latch, no clear."""
    srv = LogServer([], index_status=404)
    gw = _log_gateway(settings, srv)
    try:
        await gw._cmi_log_cycle()
        assert gw._cmi_log_error is None
        assert srv.calls["clear"] == 0
    finally:
        await gw.aclose()


async def test_year_boundary_fallback_ingests_without_clear(settings):
    """Year change: the current-year index does not exist yet (404), but the
    storekrit buffer still holds the previous year's data file.  The cycle
    must fall back to the previous-year index, ingest the remaining file,
    but NOT issue clear.log (buffer state across the boundary is
    unverifiable)."""
    import datetime as _dt

    prev_year = _dt.datetime.now().year - 1
    f_old = make_log_file(f"data_{prev_year}_12_31_23_50_00.log", (23, 50, 0), year=prev_year)
    srv = LogServer(
        [f_old],
        index_404_years=frozenset({_dt.datetime.now().year}),
    )
    gw = _log_gateway(settings, srv)
    try:
        await gw._cmi_log_cycle()
        assert srv.calls["clear"] == 0  # no clear across the year boundary
        assert gw.history.cmi_log_ingested_names() == {f_old[0].rsplit("/", 1)[-1]}
        assert gw._cmi_log_error is None
        assert (
            gw.history._ensure_conn()
            .execute("SELECT COUNT(*) FROM measurements WHERE source='cmi-log'")
            .fetchone()[0]
            == 0
        )
        # S4N-LogParquet: the ingested rows live in the cmi-log Parquet store.
        import pyarrow.parquet as pq

        table = pq.read_table(
            settings.cmi_log_parquet_dir / str(prev_year) / "12" / f"{prev_year}-12.parquet"
        )
        assert len(table) == 39
    finally:
        await gw.aclose()


async def test_clear_must_answer_ok(settings):
    """A clear that does not answer 'OK' is treated as a failure (latched).
    (Legacy gate: CMI_LOG_CLEAR=True.)"""
    f1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    srv = LogServer([f1], clear_body=b"NO")
    settings.cmi_log_clear = True
    gw = _log_gateway(settings, srv)
    try:
        gw.history.cmi_log_mark(f1[0].rsplit("/", 1)[-1], f1[1], "sha", now_iso())
        await gw._cmi_log_cycle()
        assert gw._cmi_log_error and "clear.log" in gw._cmi_log_error
        assert srv.calls["clear"] == 1
    finally:
        await gw.aclose()


async def test_cmi_log_status_block(settings):
    """/health block shape: error None + last poll ts + ingest count after a
    successful cycle; error latched after a failing one; S4N-LogParquet adds
    the cmi-log store block."""
    f1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    srv = LogServer([f1])
    gw = _log_gateway(settings, srv)
    try:
        st = gw.cmi_log_status()
        assert st["error"] is None
        assert st["last_poll_ts"] is None
        assert st["last_ingested_files"] == 0
        assert st["store"]["available"] is False
        assert st["store"]["months"] is None
        await gw._cmi_log_cycle()
        st = gw.cmi_log_status()
        assert st["error"] is None
        assert st["last_poll_ts"] is not None
        assert st["last_ingested_files"] == 1
        assert st["store"]["available"] is True
        assert st["store"]["months"] == ["2026-10"]
    finally:
        await gw.aclose()


async def test_cmi_log_parquet_write_error_latches(settings, monkeypatch):
    """S4N-LogParquet: a failed Parquet month rewrite must latch the cmi-log
    error flag, NOT mark the file ingested (the clear gate stays closed, the
    retry is safe) and leave the poller alive. (Legacy-gate semantics:
    CMI_LOG_CLEAR=True.)"""
    f1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    srv = LogServer([f1])
    settings.cmi_log_clear = True
    gw = _log_gateway(settings, srv)
    try:

        def boom_write_month(month: str, records):
            raise OSError("simulated: cmi_log_pq dir read-only (disk full)")

        monkeypatch.setattr(gw.cmi_log_pq, "write_month", boom_write_month)
        await gw._cmi_log_cycle()
        assert gw._cmi_log_error is not None and "cmi-log" in gw._cmi_log_error
        # file is NOT ingested -> next cycle retries it; clear stays closed.
        assert gw.history.cmi_log_ingested_names() == set()
        assert srv.calls["clear"] == 0
    finally:
        await gw.aclose()


# ---------------------------------------------------------------------------
# Q-125 R4: decode blind-spot latch (total decode failure must be visible
# on /health -- a format change where EVERY new file is undecodable used to
# look like a successful cycle: error=None, last_poll_ts=now, frozen store)
# ---------------------------------------------------------------------------
async def test_decode_blindspot_total_failure_latches(settings):
    """Q-125 R4: index with 1 new file whose content is undecodable
    (bad magic -> the per-file ValueError swallow in fetch_cmi_log_files)
    -> the cycle must NOT report health: error latched with a
    format-change hint, last_poll_ts stays stale, nothing ingested,
    side table empty (the file is retried next cycle)."""
    bad_name = "data_2026_11_07_16_14_00.log"
    bad = b"XXXX" + b"\x43" * (RECORD_PERIOD * 40)
    srv = LogServer([(f"/LOG/{YEAR}/{bad_name}", len(bad), bad)])
    gw = _log_gateway(settings, srv)
    try:
        await gw._cmi_log_cycle()
        assert gw._cmi_log_error is not None
        assert "0 decoded" in gw._cmi_log_error
        assert "format change" in gw._cmi_log_error
        assert bad_name in gw._cmi_log_error
        st = gw.cmi_log_status()
        assert st["error"] is gw._cmi_log_error
        # Health-fix semantics: a blind-spot cycle does NOT refresh
        # last_poll_ts (the store is frozen; the ts must NOT claim a
        # successful poll).
        assert st["last_poll_ts"] is None
        assert st["last_ingested_files"] == 0
        # The undecodable file is NOT marked ingested -> next cycle retries.
        assert gw.history.cmi_log_ingested_names() == set()
        assert srv.calls["clear"] == 0
    finally:
        await gw.aclose()


async def test_decode_blindspot_recovery_clears_latch(settings):
    """Q-125 R4: the latch clears itself: once a cycle decodes >=1 file
    (decoder fixed, e.g. R2/R3 landed) the error goes back to None, the
    last_poll_ts refresh resumes and the valid file is ingested -- while
    the still-undecodable file keeps being retried per-file."""
    bad_name = "data_2026_11_07_16_14_00.log"
    bad = b"XXXX" + b"\x43" * (RECORD_PERIOD * 40)
    good_name = "data_2026_11_08_00_00_04.log"
    p_good, s_good, b_good = make_log_file(good_name, (0, 0, 4))
    srv = LogServer([(f"/LOG/{YEAR}/{bad_name}", len(bad), bad)])
    gw = _log_gateway(settings, srv)
    try:
        # Cycle 1: total decode failure -> blind-spot latch.
        await gw._cmi_log_cycle()
        assert gw._cmi_log_error is not None and "0 decoded" in gw._cmi_log_error
        assert gw.cmi_log_status()["last_poll_ts"] is None

        # Cycle 2: a VALID file arrives (the corrupt one stays in the index
        # and still fails per-file) -> >=1 decoded -> latch cleared.
        srv.files[p_good] = (s_good, b_good)
        await gw._cmi_log_cycle()
        assert gw._cmi_log_error is None
        st = gw.cmi_log_status()
        assert st["last_poll_ts"] is not None
        assert st["last_ingested_files"] == 1
        assert gw.history.cmi_log_ingested_names() == {good_name}
        import pyarrow.parquet as pq

        table = pq.read_table(
            settings.cmi_log_parquet_dir / str(YEAR) / "11" / f"{YEAR}-11.parquet"
        )
        assert len(table) == 39  # the valid file's records (record 0 skipped)
    finally:
        await gw.aclose()


async def test_decode_blindspot_partial_failure_no_latch(settings):
    """Q-125 R4 regression guard: a PARTIAL decode failure (1 of 2 new files
    corrupt) must NOT latch -- today's per-file swallow behaviour is kept
    (twin of test_dod_b, which covers the fetch-failure side)."""
    f1 = make_log_file("data_2026_10_27_00_00_04.log", (0, 0, 4))
    bad_name = "data_2026_10_28_00_00_04.log"
    bad = b"XXXX" + b"\x43" * (RECORD_PERIOD * 40)
    srv = LogServer([f1, (f"/LOG/{YEAR}/{bad_name}", len(bad), bad)])
    gw = _log_gateway(settings, srv)
    try:
        await gw._cmi_log_cycle()
        assert gw._cmi_log_error is None  # partial failure: swallow, no latch
        assert gw._cmi_log_no_decode is False
        st = gw.cmi_log_status()
        assert st["last_poll_ts"] is not None
        assert st["last_ingested_files"] == 1
        assert gw.history.cmi_log_ingested_names() == {f1[0].rsplit("/", 1)[-1]}
    finally:
        await gw.aclose()
