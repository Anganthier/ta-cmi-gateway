"""CMI internal-logging (/LOG) decoder + index parser (S4N-LogPoll).

The CMI keeps its *own* binary logging buffer (independent of the WinSol
Logger / WinSol-LogX files).  Every 10 s the CMI appends one record of the
three CAN devices (UVR1611 Heizkreis, UVR1611 RAS, UVR610 Solar) into a
per-day file under ``/LOG/<JJJJ>/``.  The gateway polls this store (see
:mod:`app.main` log poller) and mirrors it into the canonical
``measurements`` history with ``source="cmi-log"``.

This module is the Rosetta-validated reader.  It was reverse-engineered and
cross-checked against the WinSol-LogX decoder (:mod:`analysis.winsol_logx`,
itself 100 % validated against the WinSol-CSV Rosetta stones) on the three
ground-truth days 2026-10-27 / 2026-10-28 / 2026-10-29
(``discovery/cmi-log/cmi_raw_*.log`` vs ``logx_*.log``) plus the 2026-10-30
toggle pair.  Layout, the 55-channel analog slot map and the 22-channel
digital map (dev0 d2-d9 from the CMI byte layout, dev1 d1-d13 as a
documented 0.0 fill, dev2 d1) are reproduced exactly; see
:mod:`docs.cmi-log-protocol` (Kap. 3.1/3.2, Rosetta-25 in Kap. 4.3).

Layout (record period ``P = 169`` bytes)::

    +0   ASCII magic line  ``8D312121\\r\\n``
         29 analog descriptor blocks  ``43 01 01 80 ...``  (80 B each)
         29 digital descriptor blocks ``43 02 01 80 ...``  (80 B each)
         17 device-label blocks       ``43 00 20 91 ...``  (80 B each)
         ---- header ends here ----
    records: P = 169 bytes each
        byte 106 = seconds (0..59)
        byte 107 = minutes (0..59)
        byte 108 = hours   (0..23)
        even byte 2N = one little-endian u16 "slot" N (value field)

The first record on the P-grid overlaps the last header label block, so its
dev1/dev2 value slots are garbage (the CMI paints the "Solarpumpe 1" label
over them).  The decoder therefore detects the header boundary and skips
every record that starts before it.  All remaining records carry a valid
``hh:mm:ss`` and are aligned 1:1 in wall-clock order.

Digital values ARE stored in this binary (S4N-LogParquet): the CMI keeps
*one byte per digital channel* in the u16 slots next to the analog map --
channel ``dN`` of a device lives in byte ``110 + N`` (slot ``55 + N``), the
value in bit 0, all other bits constant 0 (see :data:`_DIGITAL_MAP`,
Rosetta-validated against the WinSol-LogX digital fields, cross-checked on
the three ground-truth days 2026-10-27 / -28 / -29 and the 10-30 toggle
pair: 100 % value-identical AND toggle-consistent on every dev0 channel,
every day).  Digital rows carry ``ad="D"`` and ``unit=None``.

dev1 digital d1-d13 carries NO payload in the CMI record (full-record scan:
no unmapped byte correlates; unlike dev0 @ slots 56-59 and dev2 @ slot 48).
The LogX ground truth is constant 0 on 344 188 samples (the four recorded
LogX days), so the decoder emits dev1 digital rows as a **documented 0.0
fill** instead of NULL (S4N-LogFull, Q-112b: no NULL column may remain in
the store; see :mod:`docs.cmi-log-protocol` Kap. 3.2).
"""

from __future__ import annotations

import hashlib
import logging
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import httpx

from app.cmi.exceptions import CMIAuthError, CMIConnectionError, CmiLogError

logger = logging.getLogger("cmi.gateway.cmi-log")

#: The ASCII magic that opens every CMI /LOG data file.  **Format-agnostic**
#: (Q-125): the 8 hex digits are a per-file header CRC, NOT a constant --
#: four distinct values are observed in OLD (P=169) files alone
#: (``8D312121``/``86147834``/``A8024640``/``8204D130``) and the same set opens
#: P=181 files.  The decoder therefore matches ``8 hex digits + CRLF`` rather
#: than a hard-coded magic (the hard-coded check was fragile per se; the
#: P=169->P=181 format switch merely made it visible).
_MAGIC_RE: Final[re.Pattern[bytes]] = re.compile(rb"^[0-9A-Fa-f]{8}\r\n")
_MAGIC_LEN: Final[int] = 10

#: Record periods the CMI /LOG store is known to emit, oldest first.  P=169 is
#: the legacy layout (48 analog + 22 digital = 70 value channels); P=181 is the
#: layout introduced 2026-11-07 ~16:14 after the UVR-Programm-Upload (51 analog
#: + 22 digital = 73 value channels; the header channel-counter u16@10 reads
#: 51 vs 48).  P is auto-detected per file by the time-grid scan.
RECORD_PERIOD: Final[int] = 169  #: legacy period (kept for backward compat)
_KNOWN_PERIODS: Final[tuple[int, ...]] = (181, 169)  #: newest-first for detection

#: Offset (within a record) of the three time bytes, per period:
#: P=169 -> (ss, mm, hh) = (106, 107, 108); P=181 -> (3, 4, 5).
#: The P=181 record was restructured so the time bytes sit right after the
#: 3-byte sync prefix (``XX 0B 1A``), NOT at the old P=169 deep offsets.
_TIME_OFFSETS: Final[dict[int, tuple[int, int, int]]] = {
    169: (106, 107, 108),
    181: (3, 4, 5),
}
#: P=169 time-byte offsets (kept for the legacy callers / tests).
_TIME_SS: Final[int] = 106
_TIME_MM: Final[int] = 107
_TIME_HH: Final[int] = 108

#: One full day of 10 s samples.
_DAY_RECORDS: Final[int] = 8640

#: A record whose first ``_WINDOW_RECORDS`` time bytes form a monotonic
#: ``+9..+11 s`` sequence is accepted as the data start.
_WINDOW_RECORDS: Final[int] = 30

#: 48 logged (device, channel) -> (slot, signed, scale).  ``slot`` is the
#: little-endian u16 index (byte offset ``2 * slot`` within the record).
#: Scale = divisor of the raw value (phys = raw / scale).  See module doc +
#: ``docs/cmi-log-protocol.md`` (Kap. 3.1 + Rosetta-25 in Kap. 4.3) for the
#: slot derivation.  Every one of the canonical 48 analog channels is mapped,
#: so the wide store's analog columns are always filled (no NULL analog).
#:
#: **Signedness (Q-121, Signed-Bug):** the UVR loggers write the 48 analog
#: slots as *signed* i16.  Every temperature / raw / flag channel is therefore
#: decoded as ``signed=True`` (``struct "<h"``), exactly matching the
#: WinSol-LogX canon (``analysis.winsol_logx``: DEV0/DEV1 all signed,
#: ``_dev2_137`` a1-a9 + a13-a16 signed).  A negative analog value (e.g.
#: -0.1 °C collector, raw 0xFFFF) must yield ``-0.1``, NOT the unsigned
#: misread ``6553.5``.  The only channels that are NOT signed are the three
#: Minderer 0-10 V voltages dev2 a10-a12 (0-10 V, unsigned u16 /100, can never
#: be negative).  The structural regression guard is
#: ``tests/test_cmi_log.py::test_channel_map_matches_logx_canon``.
#:
#: The 12 channels added by S4N-LogFull (Q-112b) are the ones that were NULL
#: before; each is Rosetta-grounded:
#:   * dev2 a10-a12 (slots 34/36/38, /100, UNSIGNED) = Minderer Keller/EG/OG,
#:     0-10 V (raw 0 / 1000).  NOT temperatures.  a11/a12 proven by toggle +
#:     value agreement on 10-27/29 + the 10-30 toggle pair; a10 is constant 0
#:     on the ground-truth days (the 10 V state is real: winsol_pq shows dev2
#:     a10 = 10 V from 2026-01-01 to 2026-10-18).
#:   * dev0 a11-a15 (slots 72-76, /10): structurally between the mapped
#:     neighbors a10 @71 and a16 @77; the LogX GT is constant 0 and the CMI
#:     slots are constant 0 -> 0.0 fill by reading the real slot.
#:   * dev1 a10 (slot 9, /1) + dev1 a14-a16 (slots 13-15, /1): between the
#:     mapped neighbors; GT constant (a10 = -1.0 in the LogX canon, a14-a16 =
#:     0) and the CMI slots hold 0.0 -> 0.0 fill.  The a10 -1.0-vs-0.0
#:     divergence from the LogX canon is documented in Kap. 4.3.
_CHANNEL_MAP: Final[dict[tuple[int, str], tuple[int, bool, int]]] = {
    # dev 0 = UVR1611 Heizkreis (1/...).  All channels: signed i16, negative
    # values allowed (LogX-Kanon).
    (0, "a1"): (62, True, 10),
    (0, "a2"): (63, True, 10),
    (0, "a3"): (64, True, 10),
    (0, "a4"): (65, True, 10),
    (0, "a5"): (66, True, 10),
    (0, "a6"): (67, True, 10),
    (0, "a7"): (68, True, 10),
    (0, "a8"): (69, True, 10),
    (0, "a9"): (70, True, 10),
    (0, "a10"): (71, True, 10),
    # dev0 a11-a15: unassigned / "-----" in the WinSol canon, constant 0
    # (Rosetta-25: slots 72-76 constant 0 on every recorded day).
    (0, "a11"): (72, True, 10),
    (0, "a12"): (73, True, 10),
    (0, "a13"): (74, True, 10),
    (0, "a14"): (75, True, 10),
    (0, "a15"): (76, True, 10),
    # dev0 a16: flow switch (0/1).  LogX-Kanon Field("a16", 44, 2, True, 1,
    # "flag") -> signed=True (functionally 0/1-neutral), /1.
    (0, "a16"): (77, True, 1),
    # dev 1 = UVR1611 RAS (2/...).  All channels: signed i16, negative values
    # allowed (LogX-Kanon).  (a6 @5 and a9 @8 were already signed=True.)
    (1, "a1"): (0, True, 10),
    (1, "a2"): (1, True, 10),
    (1, "a3"): (2, True, 1),
    (1, "a4"): (3, True, 10),
    (1, "a5"): (4, True, 10),
    (1, "a6"): (5, True, 1),
    (1, "a7"): (6, True, 10),
    (1, "a8"): (7, True, 10),
    (1, "a9"): (8, True, 1),
    # dev1 a10: raw channel between a9 @8 and a11 @10 (Rosetta-25 slot 9).
    # LogX canon = constant -1.0 (0xFFFF); the CMI slot holds 0.0 -> documented
    # divergence, filled with the CMI value 0.0.
    (1, "a10"): (9, True, 1),
    (1, "a11"): (10, True, 10),
    (1, "a12"): (11, True, 10),
    (1, "a13"): (12, True, 10),
    # dev1 a14-a16: raw channels after a13 @12 (Rosetta-25 slots 13-15,
    # GT constant 0, CMI constant 0 -> 0.0 fill).
    (1, "a14"): (13, True, 1),
    (1, "a15"): (14, True, 1),
    (1, "a16"): (15, True, 1),
    # dev 2 = UVR610 Solar (32/...).  a1-a9 + a13-a16: signed i16, negative
    # values allowed (LogX-Kanon).
    (2, "a1"): (16, True, 10),
    (2, "a2"): (18, True, 10),
    (2, "a3"): (20, True, 10),
    (2, "a4"): (22, True, 10),
    (2, "a5"): (24, True, 10),
    (2, "a6"): (26, True, 10),
    (2, "a7"): (28, True, 10),
    (2, "a8"): (30, True, 10),
    (2, "a9"): (32, True, 10),
    # dev2 a10-a12 = Minderer Keller/EG/OG (Rücklaufminderer), 0-10 V:
    # UNSIGNED raw /100 (raw 0 = zu, 1000 = 10.0 V = offen).  These three
    # channels can never be negative, so they stay unsigned (signed=False) --
    # the only unsigned analog channels in the map.  Rosetta-25 slots
    # 34/36/38 (the odd slots 35/37 are constant 0).
    (2, "a10"): (34, False, 100),
    (2, "a11"): (36, False, 100),
    (2, "a12"): (38, False, 100),
    (2, "a13"): (40, True, 10),
    (2, "a14"): (42, True, 10),
    (2, "a15"): (44, True, 10),
    (2, "a16"): (46, True, 10),
}

#: P=181 analog channel map (Q-125 Round 2 + Round 5, introduced
#: 2026-11-07 ~16:14).  ``(dev, "aN") -> (byte_offset, signed, scale)``
#: where ``byte_offset`` is the ABSOLUTE byte offset within the 181-byte
#: record of the little-endian u16 value (NOT a slot index -- the P=181
#: record has a 1-byte interleave so value slots sit at ODD byte offsets,
#: unlike P=169 where slot N is at byte 2*N).
#:
#: Every entry is VALUE-verified against time-overlapping ground truth
#: (json:1 = dev0 UVR1611, json:32 = dev2 UVR610; 2784 records, 11-07
#: 16:15:59..23:59:59 + 3749 records, 11-08 00:00:09..29:25:10; window
#: ±120 s; dev1 RAS against the WinSol P=145 GT, Q-125 Round 5).  RMSE
#: thresholds: °C channels < 0.5 K, V channels < 0.05 V.
#:
#: **dev2 a1-a4 are NOT mapped in P=181 (Q-125 Round 5, elimination):**
#: the R2 dup-hack (a1-a4 ≡ a13-a16 @ 146/150/154/158) is semantically
#: WRONG -- the bytes 146/150/154/158 carry the Minderer valve temperatures
#: (json:32 ch13-ch16, value-verified), NOT the T.Speicher / Keller-RAS
#: values.  The physical Speicher temperatures are only logged by dev0
#: (a1-a3 @ 21-39, the UVR1611 master -- maintainer's q125-doc: "Alles diese
#: Werte sind hier nur per CAN BUS uebertragen ... die UVR1611 ist hier der
#: Master").  dev2 a1-a4 therefore have NO payload in the P=181 record and
#: are emitted as documented NULL (:data:`_CHANNEL_MAP_181_PENDING`).
#:
#: **Duplikat-Struktur:** the P=181 record stores certain physical values in
#: MULTIPLE slots.  The canonical mapping (verified by GT) is:
#:   * dev0 a1/a2/a3 are duplicated at off 21/23/25 AND off 98/102/106
#:     (the canonical region is 21-39; the 98-106 copies are in the dev2
#:     region and carry the same values).
#:   * dev2 a10/a11 (Minderer Keller/EG V) @ 134/138 == dev2 a17/a18
#:     (Sollspannung 1/2) @ 162/166 (constant 10.0/1.0 V on the recorded
#:     nights -- the setpoint equals the actual output).
_CHANNEL_MAP_181: Final[dict[tuple[int, str], tuple[int, bool, int]]] = {
    # --- dev0 UVR1611 Heizkreis (json:1 GT, RMSE 0.046-0.128, n=214) ---
    # ODD byte offsets 21-39, signed i16 /10.  The P181 record stores u16
    # values at ODD byte offsets (1-byte shift vs P169's even-aligned slots).
    (0, "a1"): (21, True, 10),  # T.SP oben      (json:1 ch1)
    (0, "a2"): (23, True, 10),  # T.SP mitte     (json:1 ch2)
    (0, "a3"): (25, True, 10),  # T.SP unten     (json:1 ch3)
    (0, "a4"): (27, True, 10),  # T.Warmwasser   (json:1 ch4)
    (0, "a5"): (29, True, 10),  # T.Oelkess VL   (json:1 ch5)
    (0, "a6"): (31, True, 10),  # T.Heizkr VL    (json:1 ch6)
    (0, "a7"): (33, True, 10),  # T.Heizkr RL    (json:1 ch7)
    (0, "a8"): (35, True, 10),  # Temp.Aussen    (json:1 ch8)
    (0, "a9"): (37, True, 10),  # T.Holzk VL     (json:1 ch9)
    (0, "a10"): (39, True, 10),  # Abgas          (json:1 ch10)
    # dev0 a11-a16 (Q-125 Round 3, 2026-11-07 ~16:14 v0.6-Lernstart): belegen
    # die dev0-Region direkt nach a10 @39 (ungerade Byte-Offs, signed i16).
    # json:1 liefert für ch11+ nur digital (kein JSON-GT) -> Beleg = Binary-
    # Pattern (Full-Tag) + Header-Label + Physik (Heizkurve):
    #   * a11 @41 = VL.Solltemp (Header-Block 23): full-day min 5.0 / max 49.8 °C,
    #     23 Distinktwerte -> Heizkurve-Verhalten (~47.7 °C bei Außen 7 °C am
    #     Tag, -> 5.0 °C nachts).  signed /10.
    #   * a12 @43 = T.raum.SOLL.eff (Header-Block 24): full-day exakt 2
    #     Distinktwerte {16.0, 20.0} °C = ABSENK 16 / NORMAL 20 (CMI-Seite 6
    #     pos4/pos5).  Kein anderes Slot-Byte zeigt exakt {16,20} -> decisive.
    #     signed /10.
    #   * a13 @45, a14 @47, a15 @49 = Header "-----" (unassigned): full-day
    #     konstant 0 (0-Fill wie P=169 a11-a15).  signed /10.
    #   * a16 @51 = Ström.Schalt5 (Header-Block 28): full-day Distinktwerte
    #     {0, 1} = Flow-Schalter-Flag (Semantik = P=169 dev0 a16 @slot 77).
    #     signed /1 (Flag, KEEIN °C -- siehe _TEMPERATURE_CHANNELS_181).
    # Kollision: byte 43/45/47/49/51 sind in der dev2-Region (114-170) NICHT
    # belegt (dev2 a1-a19 @114/118/.../170) -> kein dev2-Kanal zeigt auf diese
    # Bytes; dev0 a12 @43 ist der kanonische dev0-Region-Eintrag.
    (0, "a11"): (41, True, 10),  # VL.Solltemp    (Header blk23, Heizkurve)
    (0, "a12"): (43, True, 10),  # T.raum.SOLL.eff (Header blk24, {16,20})
    (0, "a13"): (45, True, 10),  # "-----" unassigned (0-Fill)
    (0, "a14"): (47, True, 10),  # "-----" unassigned (0-Fill)
    (0, "a15"): (49, True, 10),  # "-----" unassigned (0-Fill)
    (0, "a16"): (51, True, 1),  # Ström.Schalt5  (Header blk28, 0/1-Flag)
    # --- dev1 UVR1611 RAS (Q-125 Round 5, Rosetta vs WinSol P=145 GT) ---
    # EVEN byte offsets 66-96, signed i16.  Rosetta (Q-125 R5, both P=181
    # files): dev1 a1-a9 + a11-a13 = 100.00 % value-identical vs WinSol
    # P=145 (2784/2784 on 11-07 + 5755/5755 on 11-08, ±20 s alignment) with
    # the P=145 dev1 layout a1@46 .. a13@78.  dev0 a11/a12 (VL.Solltemp /
    # T.raum.SOLL.eff) do NOT collide -- they live in the dev0 region @41/43.
    #   a1/a2/a4/a5/a7/a8 = RAS perzipiert/ist temperatures (/10, °C).
    #   a3/a6/a9 = raw RAS counters (/1, no unit; P=145 GT carries the same
    #     signed raw values incl. negatives, e.g. -391 on 11-08 12:00:49).
    #   a11/a12/a13 = Datenalter per Etage (/10 minutes; P=169 dev1 a11-a13
    #     ras_age_kell/eg/dg reference, same scale).
    #   a10/a14/a15/a16 = "unused" (maintainer q125-doc): full-day constant 0 on
    #     both files -> 0-Fill, /1, no unit.
    (1, "a1"): (66, True, 10),  # ras_kell_perc   (P145 a1 @46, 100 %)
    (1, "a2"): (68, True, 10),  # ras_kell_ist    (P145 a2 @48, 100 %)
    (1, "a3"): (70, True, 1),  # raw Zähler      (P145 a3 @50, 100 %)
    (1, "a4"): (72, True, 10),  # ras_eg_perc     (P145 a4 @54, 100 %)
    (1, "a5"): (74, True, 10),  # ras_eg_ist      (P145 a5 @56, 100 %)
    (1, "a6"): (76, True, 1),  # raw Zähler      (P145 a6 @58, 100 %)
    (1, "a7"): (78, True, 10),  # ras_dg_perc     (P145 a7 @62, 100 %)
    (1, "a8"): (80, True, 10),  # ras_dg_ist      (P145 a8 @64, 100 %)
    (1, "a9"): (82, True, 1),  # raw Zähler      (P145 a9 @66, 100 %)
    (1, "a10"): (84, True, 1),  # unused (0-Fill, P145 has no a10)
    (1, "a11"): (86, True, 10),  # ras_age_kell    (P145 a11 @74, 100 %)
    (1, "a12"): (88, True, 10),  # ras_age_eg      (P145 a12 @76, 100 %)
    (1, "a13"): (90, True, 10),  # ras_age_dg      (P145 a13 @78, 100 %)
    (1, "a14"): (92, True, 1),  # unused (0-Fill)
    (1, "a15"): (94, True, 1),  # unused (0-Fill)
    (1, "a16"): (96, True, 1),  # unused (0-Fill)
    # --- dev2 UVR610 Solar (json:32 GT, ±60 s window) ---
    # a1-a4 = ELIMINATED in P=181 (CAN-only, UVR1611 master logs the
    # Speicher-Temperaturen; no dev2 payload byte -- see module map doc):
    # emitted as documented NULL, NOT the R2 dup-hack (a1-a4 ≡ a13-a16).
    # a5-a9 = Keller/DG Temp+RAS (json:32 ch5-9, 215/215 exact on 11-07).
    (2, "a5"): (114, True, 10),  # Keller Temp  (json:32 ch5, 215/215)
    (2, "a6"): (118, True, 10),  # EG RAS       (json:32 ch6, 215/215)
    (2, "a7"): (122, True, 10),  # EG Temp      (json:32 ch7, 214/215)
    (2, "a8"): (126, True, 10),  # DG RAS       (json:32 ch8, 215/215)
    (2, "a9"): (130, True, 10),  # DG Temp      (json:32 ch9, 215/215)
    # a10-a12 = Minderer Keller/EG/OG (0-10 V, unsigned /100).
    (2, "a10"): (134, False, 100),  # Keller HzRkl V2  (json:32 ch10)
    (2, "a11"): (138, False, 100),  # EG HzRkl V2      (json:32 ch11)
    (2, "a12"): (142, False, 100),  # OG HzRkl V2      (json:32 ch12)
    # a13-a16 = DUP of a1-a4 (same physical sensors, different JSON labels).
    (2, "a13"): (146, True, 10),  # Keller HzRkl V1  (= a1)
    (2, "a14"): (150, True, 10),  # OG HzRkl V1      (= a2)
    (2, "a15"): (154, True, 10),  # EG HzRkl V1      (= a3)
    (2, "a16"): (158, True, 10),  # T.Kollektor      (= a4)
    # a17-a19 = Sollspannung 1/2/3 (NEU in P=181, header blk74-76).
    (2, "a17"): (162, False, 100),  # Sollspannung 1   (json:32 ch17, 215/215)
    (2, "a18"): (166, False, 100),  # Sollspannung 2   (json:32 ch18, 215/215)
    (2, "a19"): (170, False, 100),  # Sollspannung 3   (json:32 ch19, 215/215)
}

#: P=181 digital channel map: ``(dev, "dN") -> (byte_offset, bit)``.
#: ``byte_offset`` is the ABSOLUTE byte position within the 181-byte record
#: (NOT a u16 slot index).  The digital value is ``bit`` of that byte.
#:
#: **Q-125 Round 5: the dev0 d2-d9 byte-per-channel layout is
#: Rosetta-verified against the WinSol P=145 GT** (the +8-B restructured
#: WinSol record that carries the SAME CAN digital channels as the CMI):
#: the P=145 dev0 digital mask is the u16 @ byte 44 (the P=137 mask @48
#: with the dev0/dev1 block shifted 4 B earlier; the P=137->P=145 bit
#: permutation is preserved, see ``DEV0_DIGIT_BITS`` in
#: :mod:`analysis.winsol_logx`).  Every channel matches the P=145 GT on
#: 100.00 % of the overlapping samples AND with identical toggle counts on
#: both P=181 files (11-07: 2784 records, 11-08: 5753 records, exact-ts
#: alignment):
#:   * dev0 d2: byte 6   (P145 mask bit 6; constant 0 on both files)
#:   * dev0 d3: byte 10  (Mischkreis Auf, P145 bit 5; 100 % + 28/104 toggles)
#:   * dev0 d4: byte 11  (Mischkreis Zu, P145 bit 4; 100 % + 22/32 toggles)
#:   * dev0 d5: byte 12  (Anforderung Brenner, P145 bit 3; 100 %)
#:   * dev0 d6: byte 13  (WW-Zirkulationspumpe, P145 bit 2; 100 % + 20/18
#:     toggles)
#:   * dev0 d7: byte 14  (Zirkulationspumpe, P145 bit 1; 100 % + 20/18
#:     toggles)
#:   * dev0 d8: byte 15  (Pumpe Heizkreis, P145 bit 0; 100 % + 3/2 toggles,
#:     intermittent)
#:   * dev0 d9: byte 16  (Ventil Heizungsrücklauf, P145 bit 15; 100 %,
#:     const-ON signature like P=169 byte119)
#:
#: **d8/d9 identity (⚠️ HITL open item, implement per Rosetta-golden):**
#: the P=169 Rosetta-golden (byte118 = d8 Pumpe Heizkreis, byte119 = d9
#: Ventil Heizungsrücklauf; P137 bit0/d8 vs bit15/d9) chains through the
#: P137->P145->P181 layout to P=181 byte15 = d8 / byte16 = d9, and the
#: signatures match (byte15 intermittent, byte16 const-ON).  maintainer's
#: q125-doc "Korrektur" claims byte16 = d8 (Pumpe) + d9 = Ventil
#: "verschoben" -- that is CONTRADICTED by the P169/P137 golden chain, so the
#: Rosetta-golden mapping is implemented and the contradiction is surfaced
#: to the orchestrator before deploy.
#:
#: **dev0 d1 (Pumpe Holzofen, byte 8, P145 mask bit 7) is VERIFIED (100 % +
#: toggle-consistent, both days) but NOT PERSISTED**: the 74-column store
#: schema has no ``dev0_digital_01`` column, so emitting it would break the
#: 73-row/record + 74-column gates.  It stays logged-but-unpersisted (a
#: store-schema change, out of Q-125-R5 scope).
#:
#: **dev2 d1 (Solarpumpe) = byte 174, bit 0** (Q-125 Round 5 CORRECTION: the
#: R2 mapping @ byte 10 was WRONG -- byte 10 is dev0 d3, Mischkreis Auf).
#: byte 174 bit0 = 100.00 % vs the WinSol P=145 dev2 digital byte @ 144
#: bit7 (2784/2784 on 11-07, 5753/5753 on 11-08) AND vs json:32 ch1 D
#: (182/182 on 11-07, 448/448 on 11-08; solar-ON events align).  It is
#: structurally the last byte of the dev2 region, right after dev2 a19 @170.
_DIGITAL_MAP_181: Final[dict[tuple[int, str], tuple[int, int]]] = {
    (0, "d2"): (6, 0),  # Pumpe WW zum Puffer (P145 mask bit 6, const 0)
    (0, "d3"): (10, 0),  # Mischkreis Auf    (P145 mask bit 5, 100 %)
    (0, "d4"): (11, 0),  # Mischkreis Zu     (P145 mask bit 4, 100 %)
    (0, "d5"): (12, 0),  # Anforderung Brenner (P145 mask bit 3, 100 %)
    (0, "d6"): (13, 0),  # WW-Zirkulationspumpe (P145 mask bit 2, 100 %)
    (0, "d7"): (14, 0),  # Zirkulationspumpe   (P145 mask bit 1, 100 %)
    (0, "d8"): (15, 0),  # Pumpe Heizkreis     (P145 mask bit 0, 100 %)
    (0, "d9"): (16, 0),  # Ventil Heizungsrücklauf (P145 mask bit 15, 100 %)
    (2, "d1"): (174, 0),  # Solarpumpe (P145 @144 bit7, 100 %; json:32 ch1 D)
}

#: P=181 channels whose slot has NO payload in the record (eliminated).
#: Emitted as documented NULL (never a guessed value):
#:
#: **dev2 a1-a4:** the R2 dup-hack (a1-a4 ≡ a13-a16 @ 146/150/154/158) is
#: eliminated (Q-125 Round 5).  The T.Speicher temperatures / Keller-RAS value
#: are logged by the UVR1611 master (dev0 a1-a3 @ 21-39 and the RAS region
#: dev1 a1-a16); the dev2 bytes @ 146/150/154/158 carry the Minderer valve
#: temperatures (dev2 a13-a16, json:32 ch13-ch16, value-verified).  maintainer
#: (q125-doc 2c): "Alles diese Werte sind hier nur per CAN BUS uebertragen ...
#: die UVR1611 ist hier der Master" -> dev2 a1-a4 have no own dev2 payload.
#:
#: (Q-125 Round 3/5 history: dev0 a11-a16 and dev1 a1-a16 / dev0 d2-d8 are
#: NO LONGER pending -- dev0 a11-a16 moved to :data:`_CHANNEL_MAP_181`
#: (a11@41 VL.Solltemp, a12@43 T.raum.SOLL.eff, a13-a15 0-Fill, a16@51
#: Ström.Schalt5); dev1 a1-a16 moved to :data:`_CHANNEL_MAP_181` @66-96 and
#: dev0 d2-d9 to :data:`_DIGITAL_MAP_181` (Rosetta R5).)
#:
#: dev1 d1-d13 stays the documented 0.0 fill (:data:`_DEV1_DIGITAL_FILL`),
#: exactly as P=169 (task: "dev1 digital d1-d13 = dokumentierter 0.0-Fill").
_CHANNEL_MAP_181_PENDING: Final[frozenset[tuple[int, str]]] = frozenset(
    {(2, f"a{n}") for n in range(1, 5)}  # dev2 a1-a4
)

#: Digital channel map (S4N-LogParquet, Rosetta-validated 2026-10-30;
#: extended by S4N-LogFull, Q-112b).  ``(dev, "dN") -> (slot, bit)``: the
#: digital value of channel N is the ``bit``-th bit of the little-endian u16
#: "slot" (byte offset ``2 * slot`` within the P = 169 record).
#:
#: **The CMI stores one byte per digital channel** -- not the WinSol-LogX
#: u16 mask.  For dev0 the channels occupy bytes 112..119 (slots 56..59),
#: channel ``dN`` in byte ``110 + N`` with the value in bit 0 (the high
#: bytes 111, 113, ... are constant 0; the u16 bit 8 is just the low bit of
#: the high byte -- that is why the map alternates bit 0 / bit 8 across the
#: slot pairs).  This is the CMI-side twin of the WinSol digital fields
#: (dev0 mask @ LogX offset 48, dev2 digital byte @ LogX offset 136 bit 7 =
#: Solarpumpe); both loggers read the same CAN digital channels, so the
#: values match sample-for-sample on the three ground-truth days (toggle
#: consistency included):
#:
#: * dev0 d2-d9: **100 % value-identical + 100 % toggle match** on 10-27
#:   (5962), 10-28 (8639) and 10-29 (7318) samples, every channel.
#: * dev2 d1 (Solarpumpe): 99.5-100 % value-identical -- the known dev2
#:   sampling phase (same class as dev2 a16, see WEAK_CHANNELS).
#: * dev1 digital d1-d13: **no payload byte exists in the CMI record** -- a
#:   full-record scan (all u16 slots across every recorded CMI file) found no
#:   unmapped slot whose content correlates with the LogX dev1 digital mask
#:   (unlike dev0 @ slots 56-59 and dev2 @ slot 48).  The LogX ground truth
#:   is constant 0 on 344 188 samples across all four recorded LogX days, so
#:   the decoder emits dev1 digital as a **documented 0.0 fill** (see
#:   :data:`_DEV1_DIGITAL_FILL` and the decoder loop, which special-cases it).
#:   No NULL, no guessing: the fill value equals the ground-truth value on
#:   100 % of recorded samples.
_DIGITAL_MAP: Final[dict[tuple[int, str], tuple[int, int]]] = {
    (0, "d2"): (56, 0),
    (0, "d3"): (56, 8),
    (0, "d4"): (57, 0),
    (0, "d5"): (57, 8),
    (0, "d6"): (58, 0),
    (0, "d7"): (58, 8),
    (0, "d8"): (59, 0),
    (0, "d9"): (59, 8),
    (2, "d1"): (48, 0),  # Solarpumpe
}

#: dev1 digital channels d1-d13: emitted as a constant 0.0 fill (S4N-LogFull,
#: Q-112b).  Grounding: (1) no CMI payload byte exists for dev1 digital
#: (full-record scan, see module doc + docs Kap. 3.2); (2) the WinSol-LogX
#: ground truth is constant 0 on every recorded sample (0 of 344 188 across
#: the four LogX days 2026-10-27 .. 10-30).  The store must carry no NULL
#: column, so the value 0.0 is filled explicitly and documented -- it is NOT
#: a decoded value.
_DEV1_DIGITAL_FILL: Final[frozenset[int]] = frozenset(range(1, 14))

#: Channels that carry a *temperature* (=> unit "°C").  Everything else (raw
#: counters, the a16 flow switch, RAS minute-counters, the Minderer 0-10 V
#: voltages dev2 a10-a12) has no reliable unit and is persisted with
#: ``unit=None``.  Grounded on the WinSol channel-label model
#: (``discovery/winsol/Setup.xml``) + the recorded La/Ld unit codes
#: (Unit "1" = °C, "46" = °C RAS), see json_api.UNIT_MAP.  dev2 a13-a15 =
#: Temperatur am Minderer (°C), Q-112; they are the *temperatures at* the
#: Minderer valves, distinct from the 0-10 V *positions* dev2 a10-a12.
_TEMPERATURE_CHANNELS: Final[frozenset[tuple[int, str]]] = frozenset(
    {
        (0, "a1"),
        (0, "a2"),
        (0, "a3"),
        (0, "a4"),
        (0, "a5"),
        (0, "a6"),
        (0, "a7"),
        (0, "a8"),
        (0, "a9"),
        (0, "a10"),
        (1, "a1"),
        (1, "a2"),
        (1, "a4"),
        (1, "a5"),
        (1, "a7"),
        (1, "a8"),
        (2, "a1"),
        (2, "a2"),
        (2, "a3"),
        (2, "a4"),
        (2, "a5"),
        (2, "a6"),
        (2, "a7"),
        (2, "a8"),
        (2, "a9"),
        (2, "a16"),
        (2, "a13"),
        (2, "a14"),
        (2, "a15"),  # Minderer-Temperatur (°C), Q-112
    }
)

#: P=181-specific temperature channels (=> unit "°C") -- Q-125 Round 3.
#: In P=181 the dev0 analog map is value-verified, so the channels that are
#: unassigned 0.0-fills in P=169 (dev0 a11/a12, kept ``unit=None`` in
#: :data:`_TEMPERATURE_CHANNELS` for the bit-identical P=169 regression) are
#: REAL setpoint temperatures here:
#:   * dev0 a11 = VL.Solltemp (°C, Heizkurve-Soll)
#:   * dev0 a12 = T.raum.SOLL.eff (°C, ABSENK 16 / NORMAL 20)
#: dev0 a16 (Ström.Schalt5, @51) is a 0/1 flow-switch flag, NOT a temperature
#: (same as P=169 dev0 a16).  The P=181 row keeps the P=169 classification for
#: every channel that is mapped in BOTH periods, so the P=181 unit set is
#: :data:`_TEMPERATURE_CHANNELS` + exactly these two dev0 analog channels.
_TEMPERATURE_CHANNELS_181: Final[frozenset[tuple[int, str]]] = _TEMPERATURE_CHANNELS | {
    (0, "a11"),
    (0, "a12"),
}

#: A data-file line: ``/LOG/<y>/data_<y>_<MM>_<DD>_<HH>_<MM>_<SS>.log <size>``.
#: The year appears both in the path and in the filename; the remaining fields
#: are month, day and the file's *creation* wall-clock (HH:MM:SS).  Note the
#: CMI writes the day-of-month slot with the letter ``TT`` in its own docs,
#: but it is a plain 2-digit day number.
_DATA_FILE_RE: Final[re.Pattern[str]] = re.compile(
    r"^/LOG/(?P<y>\d{4})/data_(?P<y2>\d{4})_(?P<mo>\d{2})_(?P<d>\d{2})"
    r"_(?P<h>\d{2})_(?P<mi>\d{2})_(?P<s>\d{2})\.log\s+(?P<size>\d+)\r?$"
)

#: One line of the ``/LOG/info<JJJJ>.log`` index (identical shape to a data
#: file line; the path-year and file-year are expected to agree).
_INDEX_ENTRY_RE: Final[re.Pattern[str]] = _DATA_FILE_RE


@dataclass(frozen=True)
class CmiLogFile:
    """One data file listed by the CMI /LOG index."""

    path: str  # e.g. ``/LOG/2026/data_2026_10_28_00_00_04.log``
    size: int  # byte size as reported by the index
    year: int
    month: int
    day: int

    @property
    def name(self) -> str:
        """Basename of the data file (the side-table key)."""
        return self.path.rsplit("/", 1)[-1]

    @property
    def date(self) -> str:
        """``YYYY-MM-DD`` (the logged day, from the filename)."""
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"


@dataclass
class CmiLogData:
    """The result of decoding one CMI /LOG data file.

    ``rows`` are ``(ts, device, channel, ad, value, unit, source)`` tuples, in
    the exact shape :meth:`app.db.HistoryDB.upsert` expects
    (``ts = "YYYY-MM-DD HH:MM:SS"``, ``device = "cmi-log:<dev>"``,
    ``channel`` = CMI channel number, ``ad = "A"`` analog / ``"D"`` digital,
    ``source = "cmi-log"``).  Per P=169 record: 48 analog rows (dev0 a1-a16,
    dev1 a1-a16, dev2 a1-a16) followed by the 22 digital rows in
    :data:`_DIGITAL_MAP` order (dev0 d2-d9, dev2 d1) plus the documented dev1
    d1-d13 0.0-fill (:data:`_DEV1_DIGITAL_FILL`) -> 70 rows per decoded
    record.  Per P=181 record: 51 analog rows (the value-verified dev0
    a1-a16 + dev1 a1-a16 + dev2 a5-a19 from :data:`_CHANNEL_MAP_181` plus the
    P=181-eliminated dev2 a1-a4 emitted as ``value=None``, see
    :data:`_CHANNEL_MAP_181_PENDING`) + 22 digital rows (the value-verified
    dev0 d2-d9 / dev2 d1 from :data:`_DIGITAL_MAP_181` and the documented
    dev1 d1-d13 0.0-fill) -> 73 rows per decoded record.  Of the 73, 69 carry
    a value (47 value-verified analog + 13 dev1-digital 0.0-fill + dev0
    d2-d9 + dev2 d1) and 4 are PENDING NULL (dev2 a1-a4).  Digital values are
    0/1 and carry ``unit=None``; dev1 digital is a constant 0.0 fill (no CMI
    payload; GT constant 0 -- see module doc).  ``value`` is ``float | None``:
    P=169 rows always carry a value, P=181 rows carry ``None`` for the
    eliminated dev2 a1-a4 channels.

    ``name`` / ``size`` / ``sha256`` carry the ingestion metadata of the
    fetched file (set by :func:`fetch_cmi_log_files`); they stay empty for a
    bare :func:`decode_cmi_log` of local bytes.
    """

    date: str  # "YYYY-MM-DD"
    n_records: int  # records present in the file (incl. skipped)
    n_decoded: int  # records actually decoded (clean ones)
    rows: list[tuple[str, str, int, str, float | None, str | None, str]] = field(
        default_factory=list
    )
    #: Basename of the data file (side-table key); set by the fetch layer.
    name: str = ""
    #: Byte size as reported by the index (verified on download).
    size: int = 0
    #: SHA-256 hex of the downloaded bytes.
    sha256: str = ""


# ---------------------------------------------------------------------------
# Index parsing
# ---------------------------------------------------------------------------
def parse_log_index(body: bytes | str) -> list[CmiLogFile]:
    """Parse the ``/LOG/info<JJJJ>.log`` index into :class:`CmiLogFile` items.

    The CMI index looks like::

        CMI\\r\\n
        1.47 1.06 2.00 Revision: 1803\\r\\n
        storekrit: 10\\r\\n
        \\0\\0\\0\\0\\r\\n
        \\r\\n
        /LOG/2026/data_2026_10_28_00_00_04.log 1466178\\r\\n
        /LOG/2026/data_2026_10_29_00_00_05.log 1242929\\r\\n

    Only the data-file lines matter; the header (magic / version /
    ``storekrit``) is ignored.  A blank or header-only index yields ``[]``.
    """
    text = body.decode("latin-1") if isinstance(body, (bytes, bytearray)) else body
    out: list[CmiLogFile] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or not line.startswith("/LOG/"):
            continue
        m = _INDEX_ENTRY_RE.match(line)
        if not m:
            continue
        out.append(
            CmiLogFile(
                path=line.rsplit(" ", 1)[0],  # the exact path as the CMI wrote it
                size=int(m["size"]),
                year=int(m["y"]),
                month=int(m["mo"]),
                day=int(m["d"]),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Data-file decoding
# ---------------------------------------------------------------------------
def _detect_data_start(buf: bytes, p: int) -> int:
    """Return the byte offset of the first record for period ``p``.

    P=169: the first record sits ON the P-grid (a multiple of 169 from byte 0)
    and is found by the classic time-grid scan (see the P=169 branch below) --
    this keeps the legacy detection BIT-IDENTICAL (regression guard).

    P=181: the record was restructured and its grid is OFFSET from byte 0
    (the verified data start is 6258 = 78x80-byte header blocks + 2 CRLF,
    which is NOT a multiple of 181).  The P-grid scan therefore finds nothing,
    so P=181 uses a LAYOUT-AGNOSTIC sync-scan (:func:`_detect_data_start_181`):
    locate the ``0B 1A`` sync tail (the constant last two bytes of the 3-byte
    record sync ``XX 0B 1A``; the leading sync byte varies 07/08 ...), confirm
    a CRLF-terminated +10 s ``hh:mm:ss`` window on the 181-stride, and take the
    first qualifying offset as the data start.  This does NOT hard-code 6258:
    it is header-size-agnostic (a different header would still be found).
    """
    if p == 181:
        return _detect_data_start_181(buf)
    _ss, _mm, hh_off = _TIME_OFFSETS[p]
    if len(buf) < p * 2 + hh_off:
        raise ValueError(
            f"CMI log too small for even one confirmation window ({len(buf)} < "
            f"{p * 2 + hh_off} B for P={p})"
        )
    for m in range(0, len(buf) - hh_off + 1, p):
        # Number of P-blocks from m onward whose hh byte (m + k*p + hh_off) is
        # in range (<= len-1).  When this is <2 we break BEFORE touching any
        # time byte, so no OOB access.
        available = (len(buf) - 1 - m - hh_off) // p + 1
        window = min(_WINDOW_RECORDS, available)
        if window < 2:
            break  # not enough blocks left from here
        if _valid_time_window(buf, m, window, p):
            return m
    raise ValueError(f"no data-start found for P={p}: no P-grid offset carries a valid time window")


#: P=181 record sync tail (the constant last two bytes of the 3-byte sync
#: ``XX 0B 1A``; the leading byte varies -- 07 in the 11-07 file, 08 in the
#: 11-08 file -- so only the tail is matched).  Also the record terminator is
#: the CRLF at record offsets +179/+180.
_P181_SYNC_TAIL: Final[bytes] = b"\x0b\x1a"
_P181_CRLF: Final[bytes] = b"\x0d\x0a"


def _valid_181_window(buf: bytes, start: int, p: int, window: int) -> bool:
    """True when ``window`` records at ``start`` on the ``p``-stride all carry
    the P=181 sync tail (``0B 1A`` at +1/+2), a CRLF terminator (at +p-2/+p-1),
    and form a valid ``hh:mm:ss`` sequence with +9..+11 s steps (time bytes at
    :data:`_TIME_OFFSETS` = 3/4/5).  This is the P=181 confirmation window that
    distinguishes real data from the descriptor/label header (which never
    carries the sync+CRLF+10-s grid)."""
    _ss, _mm, hh_off = _TIME_OFFSETS[p]
    prev: int | None = None
    steps = 0
    for k in range(window):
        b = start + k * p
        if b + p > len(buf):
            return False
        if buf[b + 1 : b + 3] != _P181_SYNC_TAIL:
            return False
        if buf[b + p - 2 : b + p] != _P181_CRLF:
            return False
        hh, mm, ss = buf[b + hh_off], buf[b + _mm], buf[b + _ss]
        if hh > 23 or mm > 59 or ss > 59:
            return False
        cur = hh * 3600 + mm * 60 + ss
        if prev is not None:
            if not 9 <= cur - prev <= 11:
                return False
            steps += 1
        prev = cur
    return steps >= max(1, window - 2)


def _detect_data_start_181(buf: bytes) -> int:
    """Layout-agnostic P=181 data-start discovery (Q-125 Round 2).

    Scan for the ``0B 1A`` sync tail (jumping via ``bytes.find`` so the scan is
    O(n) and header-size-agnostic) and return the first offset ``start`` (one
    byte before the tail) that carries a valid :func:`_valid_181_window` on the
    181-stride.  The header (descriptor/label blocks) never satisfies the
    sync+CRLF+10-s grid, so the first hit is the true data start (6258 on the
    2026-11-07/-08 files).  Raises :class:`ValueError` when no qualifying
    record start exists (e.g. a header-only fragment).
    """
    p = 181
    probe = _MAGIC_LEN  # never inside the 10-byte ASCII magic line
    while True:
        idx = buf.find(_P181_SYNC_TAIL, probe)
        if idx < 0:
            break
        start = idx - 1
        if start >= _MAGIC_LEN:
            available = (len(buf) - 1 - start - p) // p + 1
            window = min(_WINDOW_RECORDS, available)
            if window >= 2 and _valid_181_window(buf, start, p, window):
                return start
        probe = idx + 1
    raise ValueError(
        "no P=181 data-start found: no 0B1A+CRLF record grid carries a valid +10 s time window"
    )


def _valid_time_window(buf: bytes, start: int, window: int, p: int) -> bool:
    """True when ``window`` records at ``start`` on the ``p``-grid form a valid
    ``hh:mm:ss`` sequence with +9..+11 s steps (using the P-specific time
    byte offsets)."""
    _ss, _mm, hh_off = _TIME_OFFSETS[p]
    prev: int | None = None
    steps = 0
    for k in range(window):
        b = start + k * p + hh_off
        hh, mm, ss = buf[b], buf[b - 1], buf[b - 2]
        if hh > 23 or mm > 59 or ss > 59:
            return False
        cur = hh * 3600 + mm * 60 + ss
        if prev is not None:
            if not 9 <= cur - prev <= 11:
                return False
            steps += 1
        prev = cur
    # ``window - 2`` keeps the original tolerance for full days; for the short
    # (>=3) reboot windows every step is checked, which is all that exist.
    return steps >= max(1, window - 2)


def _detect_period(buf: bytes) -> int:
    """Auto-detect the record period ``P`` of a CMI /LOG data file (Q-125).

    The CMI /LOG store is known to emit P=169 (legacy, 70 value channels) or
    P=181 (introduced 2026-11-07 ~16:14, 73 value channels).  Detection is by
    the time-grid scan: the CORRECT period yields a valid +10 s ``hh:mm:ss``
    window on its own grid, while the wrong period does not (the time bytes sit
    at different offsets).  We try the newest period first (P=181) so a P=181
    file is never mis-detected as P=169; a P=169 file fails the P=181 scan and
    falls through to P=169.

    A header channel-counter cross-check is used as a fast hint when it is
    unambiguous: the u16LE at offset 10 (after the 8-hex magic + CRLF) is 48
    for P=169 and 51 for P=181.  When it matches a known period exactly we
    short-circuit; otherwise (or on a value we do not know) the time-grid scan
    is authoritative.  Raises :class:`ValueError` when no known period fits.
    """
    hint = _read_header_channel_counter(buf)
    if hint is not None and hint in _PERIOD_BY_CHANNEL_COUNT:
        return _PERIOD_BY_CHANNEL_COUNT[hint]
    for p in _KNOWN_PERIODS:
        try:
            _detect_data_start(buf, p)
        except ValueError:
            continue
        return p
    raise ValueError(
        "unknown CMI log record period: no known P "
        f"({list(_KNOWN_PERIODS)}) yields a valid time window"
    )


def _read_header_channel_counter(buf: bytes) -> int | None:
    """The header channel-counter u16LE at offset 10 (post magic), or ``None``.

    Q-125: the P=169 header carries 48 analog channels, P=181 carries 51
    (the +3 Minderer-Sollspannung).  This is an informational cross-check for
    :func:`_detect_period`; it is not the primary detection signal (the time
    grid is), because the counter semantics (count of enumerated analog
    descriptor blocks, not total analog slots) are not fully documented.
    Returns ``None`` when the value is outside the known set so the caller
    falls back to the time-grid scan.
    """
    try:
        v = struct.unpack_from("<H", buf, _MAGIC_LEN)[0]
    except struct.error:
        return None
    return v if v in _PERIOD_BY_CHANNEL_COUNT else None


#: Header channel-counter value -> record period (Q-125 cross-check).
_PERIOD_BY_CHANNEL_COUNT: Final[dict[int, int]] = {
    48: 169,
    51: 181,
}


def _detect_header_end(buf: bytes, data_start: int, p: int) -> int:
    """Return the byte just past the last 80-byte descriptor/label block.

    The header is a contiguous run of 80-byte ``0x43``-led blocks that begins
    a few bytes after the ASCII magic.  We locate the first block, then walk
    the contiguous run (it cannot extend past the first record on the P-grid)
    and return the end of the last block, so that any record overlapping the
    header is skipped.  For P=169 the last label block ends at 6014 while the
    record grid starts at 5915, so record 0 (5915..6083) is skipped; for P=181
    the header ends at 6254 and the grid starts at 6154, so record 0 is
    likewise skipped.
    """
    # Find the first descriptor block (a 0x43 byte) after the magic line.
    pstart = _MAGIC_LEN
    limit = data_start + p
    while pstart < limit and buf[pstart] != 0x43:
        pstart += 1
    end = pstart
    # Walk the contiguous 80-byte block run.
    while pstart + 80 <= limit and buf[pstart] == 0x43:
        end = pstart + 80
        pstart += 80
    # The header can never be shorter than the record grid start; clamp so we
    # at least skip the record that straddles the last block.
    return max(end, data_start)


def _date_from_filename(name: str) -> str | None:
    """``YYYY-MM-DD`` from a data-file name (``data_<y>_<MM>_<DD>_...log``).

    Accepts either a basename or a full ``/LOG/<y>/data_...`` path.
    """
    base = name.rsplit("/", 1)[-1]
    m = re.match(r"^data_(?P<y>\d{4})_(?P<mo>\d{2})_(?P<d>\d{2})_\d{2}_\d{2}_\d{2}\.log$", base)
    if not m:
        return None
    return f"{m['y']}-{m['mo']}-{m['d']}"


def decode_cmi_log(
    body: bytes, *, date: str | None = None, filename: str | None = None
) -> CmiLogData:
    """Decode one CMI /LOG data file into :class:`CmiLogData` rows.

    ``date`` is the ``YYYY-MM-DD`` logged day; it defaults to the day parsed
    from ``filename`` (the CMI encodes the day in the data-file name).  Only
    *clean* records (those that start after the header) are decoded; the
    record that overlaps the last header label block is skipped.

    Q-125: the record period is **auto-detected** (:func:`_detect_period`) and
    the magic check is format-agnostic (8 hex digits + CRLF, not a constant
    magic).  P=169 files decode exactly as before (70 rows/record,
    :data:`_CHANNEL_MAP`); P=181 files decode with :data:`_CHANNEL_MAP_181`
    (the value-verified dev0 a1-a16 + dev1 a1-a16 + dev2 a5-a19 slots) plus a
    documented NULL for the P=181 channels whose slot has no payload
    (:data:`_CHANNEL_MAP_181_PENDING` = dev2 a1-a4, eliminated in Q-125 Round
    5) and the P=181 digital bytes (73 rows/record: 51 analog + 22 digital,
    of which 69 carry a value and 4 are PENDING NULLs).
    """
    if len(body) < min(_KNOWN_PERIODS) * 2:
        raise ValueError(f"CMI log too small to contain records ({len(body)} B)")
    if not _MAGIC_RE.match(body):
        raise ValueError(f"bad CMI log magic {bytes(body[:8])!r} (want 8 hex digits + CRLF)")

    resolved_date = date or (filename and _date_from_filename(filename))
    if resolved_date is None:
        raise ValueError("cannot determine the logged day (pass date= or filename=)")

    p = _detect_period(body)
    try:
        data_start = _detect_data_start(body, p)
        header_end = _detect_header_end(body, data_start, p)
    except ValueError:
        # Zero-record tolerance (Q-125): a post-reboot fragment may carry only
        # the header (or the header plus a truncated record) and no valid
        # +10 s time window -- decode it as 0 records instead of raising.  The
        # poller then simply has nothing to persist for this file (the fetch
        # layer still marks it ingested, so it is not re-fetched forever).
        return CmiLogData(date=resolved_date, n_records=0, n_decoded=0, rows=[])
    n_records = (len(body) - data_start) // p

    channel_map = _CHANNEL_MAP if p == 169 else _CHANNEL_MAP_181
    # Q-125 Round 3: P=181 dev0 a11/a12 are real °C setpoints (VL.Solltemp /
    # T.raum.SOLL.eff) while P=169 keeps them unassigned (unit=None) -> the
    # unit set is period-specific (P=169 stays bit-identical).
    temp_channels = _TEMPERATURE_CHANNELS if p == 169 else _TEMPERATURE_CHANNELS_181
    _ss, _mm, hh_off = _TIME_OFFSETS[p]

    rows: list[tuple[str, str, int, str, float | None, str | None, str]] = []
    decoded = 0
    for k in range(n_records):
        rec_start = data_start + k * p
        if rec_start < header_end:
            continue  # record overlaps the header -> value slots are garbage
        b = rec_start + hh_off
        hh, mm, ss = body[b], body[b - 1], body[b - 2]
        if not (hh <= 23 and mm <= 59 and ss <= 59):
            continue  # corrupt/aborted record: drop, keep going
        ts = f"{resolved_date} {hh:02d}:{mm:02d}:{ss:02d}"
        for (dev, ch), (idx, signed, scale) in channel_map.items():
            # P=169: idx is the u16 slot index, byte offset = 2*idx (even).
            # P=181: idx is the ABSOLUTE byte offset of the u16 (the record was
            # restructured with a 1-byte shift so the verified value slots sit
            # at a MIX of even/odd byte offsets -- NOT a uniform shift of P=169;
            # see _CHANNEL_MAP_181 + docs Kap. 3.1).
            off = rec_start + (idx if p == 181 else 2 * idx)
            fmt = "<h" if signed else "<H"
            raw = struct.unpack_from(fmt, body, off)[0]
            value = raw / scale if scale != 1 else float(raw)
            unit = "°C" if (dev, ch) in temp_channels else None
            rows.append((ts, f"cmi-log:{dev}", int(ch[1:]), "A", value, unit, "cmi-log"))
        if p == 181:
            # P=181 digital bytes that ARE value-verified (dev0 d2-d9, dev2
            # d1 Solarpumpe @ byte 174): one byte per channel, value in
            # ``bit``.  The P=169 digital byte offsets (:data:`_DIGITAL_MAP`)
            # are WRONG on the restructured P=181 record and must not be
            # read.
            for (dev, ch), (byte_off, bit) in _DIGITAL_MAP_181.items():
                value = float((body[rec_start + byte_off] >> bit) & 1)
                rows.append((ts, f"cmi-log:{dev}", int(ch[1:]), "D", value, None, "cmi-log"))
            # P=181 channels whose slot has NO payload (eliminated, see
            # :data:`_CHANNEL_MAP_181_PENDING`): documented NULL, never a
            # guessed value.  ``ad`` is the UPPERCASE kind letter
            # ("A" = analog "aN", "D" = digital "dN") -- the downstream
            # wide-pivots (:mod:`app.main` / :mod:`analysis.cmi_log_backfill`)
            # key on exactly "A"/"D", so the lowercase channel letter must NOT
            # be used (a lowercase ad would mis-route an analog PENDING channel
            # into the digital wide column and abort the store write).
            for dev, ch in sorted(_CHANNEL_MAP_181_PENDING):
                ad = "A" if ch.startswith("a") else "D"
                rows.append((ts, f"cmi-log:{dev}", int(ch[1:]), ad, None, None, "cmi-log"))
        else:
            for (dev, ch), (slot, bit) in _DIGITAL_MAP.items():
                off = rec_start + 2 * slot
                value = float((struct.unpack_from("<H", body, off)[0] >> bit) & 1)
                rows.append((ts, f"cmi-log:{dev}", int(ch[1:]), "D", value, None, "cmi-log"))
        # dev1 digital d1-d13: documented 0.0 fill (S4N-LogFull, Q-112b), SAME
        # for P=181 as for P=169 (task spec).  The CMI record carries no
        # unique dev1 digital payload; the LogX ground truth is constant 0 ->
        # fill 0.0 explicitly, never NULL.
        for ch in sorted(_DEV1_DIGITAL_FILL):
            rows.append((ts, "cmi-log:1", ch, "D", 0.0, None, "cmi-log"))
        decoded += 1

    return CmiLogData(date=resolved_date, n_records=n_records, n_decoded=decoded, rows=rows)


# ---------------------------------------------------------------------------
# /LOG HTTP fetch layer (own client -- NOT the rate-limited CMIGuard path)
# ---------------------------------------------------------------------------
#: The single state-mutating /LOG call.  Read-only fetches (index + data) are
#: rate-unlimited; ``clear.log`` empties the CMI's storekrit buffer and must
#: only be issued *after* every index file has been ingested (last-data-first).
CLEAR_PATH: Final[str] = "/LOG/clear.log"


class CMILogClient:
    """Minimal async Basic-Auth client for the CMI ``/LOG/*`` store.

    Deliberately separate from :class:`app.cmi.client.CMIClient` /
    :class:`app.cmi.queue.CMIGuard`: ``/LOG`` is not rate-limited and must not
    be funneled through the 1/min JSON-API serial queue.  A
    ``transport`` override is accepted so tests can inject
    :class:`httpx.MockTransport`.
    """

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        timeout_sec: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = httpx.BasicAuth(username=user, password=password)
        self._timeout = httpx.Timeout(timeout_sec, connect=min(5.0, timeout_sec))
        kwargs: dict = {"base_url": self._base_url, "auth": self._auth, "timeout": self._timeout}
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> CMILogClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def get_bytes(self, path: str) -> bytes:
        """Fetch one ``/LOG/*`` resource as bytes, raising CMI errors."""
        try:
            response = await self._client.get(path)
        except httpx.TimeoutException as exc:
            raise CMIConnectionError(f"CMI /LOG timeout on {path}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise CMIConnectionError(f"CMI /LOG connection error on {path}: {exc}") from exc
        if response.status_code in (401, 403):
            raise CMIAuthError(f"CMI rejected /LOG credentials ({response.status_code}) for {path}")
        if response.status_code >= 400:
            raise CmiLogError(
                f"CMI returned HTTP {response.status_code} for {path}",
                http_status=response.status_code,
            )
        return response.content

    async def get_stream(self, path: str):
        """Start a streamed GET; the caller must ``await aclose()`` the response."""
        try:
            request = self._client.build_request("GET", path)
            return await self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise CMIConnectionError(f"CMI /LOG timeout on {path}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise CMIConnectionError(f"CMI /LOG connection error on {path}: {exc}") from exc


def _log_year(body: bytes | str) -> int | None:
    """The 4-digit year the index describes (first data-file line), or ``None``.

    The index body carries no explicit year header, but every data-file line
    embeds it (``/LOG/<y>/data_<y>_...log``), so the CMI's current logging year
    is read straight from the first listed file.
    """
    text = body.decode("latin-1") if isinstance(body, (bytes, bytearray)) else body
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("/LOG/"):
            continue
        m = re.match(r"^/LOG/(\d{4})/", line)
        if m:
            y = int(m.group(1))
            if 2000 <= y <= 2100:
                return y
    return None


async def fetch_log_index(client: CMILogClient, year: int) -> list[CmiLogFile]:
    """Fetch and parse the ``/LOG/info<JJJJ>.log`` index for ``year``."""
    body = await client.get_bytes(f"/LOG/info{year}.log")
    return parse_log_index(body)


async def download_data_file(
    client: CMILogClient,
    dest_dir: str | Path,
    file: CmiLogFile,
) -> tuple[bytes, str]:
    """Stream one data file to ``dest_dir/<name>.part`` + atomic rename.

    Returns ``(content, sha256_hex)``.  Streams in 64 KiB chunks so a full day
    (~1.5 MB) never sits entirely in memory twice; a SHA-256 is computed on the
    way so the side table can fingerprint the ingested bytes.  The temp file is
    renamed atomically onto ``dest_dir/<name>`` (same filesystem).
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / file.name
    tmp = dest_dir / f"{file.name}.part"
    hasher = hashlib.sha256()
    try:
        response = await client.get_stream(file.path)
        try:
            if response.status_code in (401, 403):
                raise CMIAuthError(
                    f"CMI rejected /LOG credentials ({response.status_code}) for {file.path}"
                )
            if response.status_code >= 400:
                raise CmiLogError(
                    f"CMI returned HTTP {response.status_code} for {file.path}",
                    http_status=response.status_code,
                )
            with open(tmp, "wb") as fh:
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    fh.write(chunk)
                    hasher.update(chunk)
        finally:
            await response.aclose()
        if tmp.stat().st_size != file.size:
            raise CmiLogError(
                f"size mismatch downloading {file.name}: got {tmp.stat().st_size}, index says {file.size}"
            )
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    # Atomic rename within the same directory.
    tmp.replace(final)
    return final.read_bytes(), hasher.hexdigest()


async def fetch_cmi_log_files(
    client: CMILogClient,
    dest_dir: str | Path,
    files: list[CmiLogFile],
    *,
    ingested: dict[str, int],
) -> tuple[list[CmiLogData], set[str]]:
    """Download + decode every data file in ``files`` that is new OR grown.

    ``files`` is the (already-parsed) index content, e.g. from
    :func:`fetch_log_index`.  ``ingested`` maps data-file basenames to the
    size the /LOG index reported when that file was last ingested (the
    ``cmi_log_ingested`` side table).  A file is fetched when

    * its name is not in ``ingested`` (new file), OR
    * ``ingested[name] != f.size`` (the index size changed since the last
      ingestion).

    Size-aware dedup instead of name-only (S4N-LogPoll-DedupFix): the CMI's
    *active block* is appended to WHILE it is listed in the index (batch
    flushes every ~10 s), so a name-only skip loses every record written
    after the first ingestion.  Proven 2026-10-30: ``data_2026_10_30_19_51_56.log``
    was ingested at the index size 54521 B (records until 20:39:36), grew to
    176539 B (records until 22:39:56) and was never re-fetched -- the 722
    missing records had to be backfilled manually.  ``f.size`` is the index
    size (from :func:`fetch_log_index`), NOT a measured download size; the
    download itself still verifies it in :func:`download_data_file` (full
    stream + size check + SHA-256 -- unchanged).  The Parquet store's
    ts-dedup (:meth:`app.cmi.cmi_log_pq.CmiLogParquet.write_month`) makes the
    re-ingestion idempotent: existing ts rows stay, new ts rows are appended.

    Returns ``(decoded, ingested_now)`` where ``decoded`` is a list of
    :class:`CmiLogData` (one per newly-ingested file, in index order) and
    ``ingested_now`` is the set of file basenames that now *are* ingested
    after this call (i.e. the pre-existing ``ingested`` names plus every file
    successfully decoded here).  The caller owns upsert +
    side-table bookkeeping + the conditional :data:`CLEAR_PATH` call.

    Best-effort per file: a download/decode failure for one file is recorded on
    its :class:`CmiLogData` slot as ``None`` and does not abort the rest, so a
    single corrupt/partial file never blocks the others.
    """
    new_files = [f for f in files if ingested.get(f.name) != f.size]
    decoded: list[CmiLogData] = []
    now = set(ingested)
    for f in new_files:
        try:
            content, sha = await download_data_file(client, dest_dir, f)
        except (CmiLogError, CMIConnectionError, CMIAuthError, OSError):
            logger.warning("cmi-log: skipping %s (fetch failed)", f.name)
            continue
        try:
            data = decode_cmi_log(content, filename=f.path)
        except ValueError as exc:
            logger.warning("cmi-log: cannot decode %s: %s", f.name, exc)
            continue
        # Attach the ingestion metadata (side-table key + fingerprint).
        data.name = f.name
        data.size = f.size
        data.sha256 = sha
        decoded.append(data)
        now.add(f.name)
    return decoded, now


__all__ = [
    "CmiLogFile",
    "CmiLogData",
    "CMILogClient",
    "CLEAR_PATH",
    "RECORD_PERIOD",
    "parse_log_index",
    "decode_cmi_log",
    "fetch_log_index",
    "download_data_file",
    "fetch_cmi_log_files",
]
