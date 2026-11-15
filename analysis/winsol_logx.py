"""WinSol LogX Binary-Decoder (Technische Alternative / Infosol, CMI-Logger).

Reverse-engineered 2026-10-27 (S2-2), validiert gegen WinSol-CSV-Exporte
(UTF-32LE, Tab-separiert) als Rosetta-Stein. Doku: docs/winsol-logx.md.

Kernfakten
----------
* Datei-Magic: ``TA_WINSOL_LOG_FILE`` (16 B), dann Header (groesse variantenabhaengig).
* Datenstart: ``find(b'>>>>') + 4``. Erstes Daten-Byte ist immer ``0x44`` ('D'-Marker).
* Pro 10 s eine Record (8640/Tag bei vollem Tag). Zeiteinzel: hh/mm/ss explizit in
  +1/+2/+3 — KEIN Delta-Decoding. Fehlende Samples => Tages-Datum aus Dateiname,
  hh/mm/ss aus Record (Import darf nicht fest an 8640 Records pro Datei hängen).
* Analogwerte: u16 LE (bzw. u32 LE bei P=137, dev2 a10..a12), pro Kanal Skalierung.
* dev0 digital @+48: u16 mit **permutierter** Bitzuordnung (nur D2..D9 belegt).

Varianten (Record-Perioden P), dev0/dev1-Block ist in allen identisch:
======  ======  ===========  =====================================================
P       AnzDev dev2-Block    Bemerkung
98      2       -            2021 (kurz), dev0+dev1
104     2       -            2021 (kurz), dev1-Block 3 u16 laenger
106     3       4 x u16      2021, dev2 = UVR610 mit 4 Kanälen
116     2       -            2020, dev0+dev1 (dev1 meist 0)
118     3       9 x u16      2022 (früh), dev2 a1..a9 (+2 B Padd)
122     3       12 x u16     2022, dev2 a1..a12 (a10..a12 u16 x100)
128     3       15 x u16     2022, dev2 a1..a15 (a10..a12 u16 x100)
131     3       16 u16 +1 B  2022(03/31)..2026(02/16), **CSV-validiert 100 %**
137     3       9 u16 +3 u32 + a16/dig, dev2 a10..a12 auf u32 erweitert (+6 B)
======  ======  ===========  =====================================================

Eine Datei kann mehrere '>>>>'-Sessions enthalten (Logger-Neustart mitten im
Tag); die Sessions dürfen unterschiedliche Perioden haben
(z. B. D2022-03-07.log = P=122 + P=128). find_sessions() teilt auf.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

MAGIC = b"TA_WINSOL_LOG_FILE"
MARKER = b">>>>"
D_MARKER = 0x44  # 'D', erstes Byte jeder Record

#: bekannte Record-Perioden (aus Survey 2020..2026)
KNOWN_PERIODS = (98, 104, 106, 116, 118, 122, 128, 131, 137)


# ---------------------------------------------------------------------------
# Feld-Spezifikation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Field:
    """Ein Record-Feld. ``off`` = Byte-Offset innerhalb der Record (Basis = 'D')."""

    ch: str          # 'a1'..'a16', 'dig'
    off: int
    width: int       # 1, 2 oder 4 Bytes
    signed: bool
    scale: int       # physikalischer Wert = raw / scale
    kind: str = "analog"  # analog | flag | digital | raw | unknown
    label: str = ""


def _a(i: int, off: int, scale: int, label: str = "", kind: str = "analog") -> Field:
    return Field(f"a{i}", off, 2, True, scale, kind, label)


#: dev0 = UVR1611 Heizkreis (1/...), identisch in ALLEN Varianten
DEV0: tuple[Field, ...] = (
    _a(1, 4, 10, "Speicher Oben"),
    _a(2, 6, 10, "Speicher Mitte"),
    _a(3, 8, 10, "Speicher Unten"),
    _a(4, 10, 10, "Warmwasser"),
    _a(5, 12, 10, "Oelkessel Vorlauf"),
    _a(6, 14, 10, "Heizkreis Vorlauf"),
    _a(7, 16, 10, "Heizkreis Ruecklauf"),
    _a(8, 18, 10, "Temperatur Aussen"),
    _a(9, 20, 10, "Holzkessel Vorlauf"),
    _a(10, 22, 10, "Abgastemperatur Holz"),
    _a(11, 24, 10, "----- (unbelegt)"),
    _a(12, 26, 10, "----- (unbelegt)"),
    _a(13, 28, 10, "----- (unbelegt)"),
    _a(14, 30, 10, "----- (unbelegt)"),
    _a(15, 32, 10, "Test (beobachtet: 0)"),
    Field("a16", 44, 2, True, 1, "flag", "Stroemungsschalter (0/1)"),
    Field("dig", 48, 2, False, 1, "digital", "dev0 digital-Maske (permutiert)"),
)

#: dev1 = UVR1611 RAS Raumtemperaturen (2/...), identisch in ALLEN Varianten
DEV1: tuple[Field, ...] = (
    _a(1, 50, 10, "Offset Keller"),
    _a(2, 52, 10, "Temp Keller"),
    _a(3, 54, 1, "ANALOG 3 (raw)", "raw"),
    _a(4, 58, 10, "Offset Erdgeschoss"),
    _a(5, 60, 10, "Temp Erdgeschoss"),
    _a(6, 62, 1, "ANALOG 6 (raw)", "raw"),
    _a(7, 66, 10, "Offset Dachgeschoss"),
    _a(8, 68, 10, "Temp Dachgeschoss"),
    _a(9, 70, 1, "ANALOG 9 (raw)", "raw"),
    _a(10, 72, 1, "ANALOG 10 (raw, beobachtet: 0)", "raw"),
    _a(11, 78, 10, "Keller Minuten"),
    _a(12, 80, 10, "EG Minuten"),
    _a(13, 82, 10, "DG Minuten"),
    _a(14, 84, 1, "ANALOG 14 (raw, beobachtet: 0)", "raw"),
    _a(15, 86, 1, "ANALOG 15 (raw, beobachtet: 0)", "raw"),
    _a(16, 88, 1, "ANALOG 16 (raw, beobachtet: 0)", "raw"),
    Field("dig", 90, 2, False, 1, "digital", "dev1 digital-Maske (plain, beobachtet: 0)"),
)


def _dev2_106() -> tuple[Field, ...]:
    return (
        _a(1, 98, 10, "T.Speicher oben"),
        _a(2, 100, 10, "T.Speicher mitte"),
        _a(3, 102, 10, "T.Speicher unten"),
        Field("a4", 104, 2, True, 1, "unknown", "beobachtet: Konstante 2"),
    )


def _dev2_118() -> tuple[Field, ...]:
    labels = {1: "T.Speicher oben", 2: "T.Speicher mitte", 3: "T.Speicher unten",
              4: "Keller RAS", 5: "Keller Temp", 6: "EG RAS", 7: "EG Temp",
              8: "DG RAS", 9: "DG Temp"}
    return tuple(_a(i, 98 + 2 * (i - 1), 10, labels[i]) for i in range(1, 10))


def _dev2_128() -> tuple[Field, ...]:
    f = list(_dev2_118())
    f += (
        Field("a10", 116, 2, False, 100, "analog", "Keller HzRkl Ventil 2 Ergebnis"),
        Field("a11", 118, 2, False, 100, "analog", "EG HzRkl Ventil 2 Ergebnis"),
        Field("a12", 120, 2, False, 100, "analog", "OG HzRkl Ventil 2 Ergebnis"),
        _a(13, 122, 10, "Keller HzRkl Ventil 1"),
        _a(14, 124, 10, "OG HzRkl Ventil 1"),
        _a(15, 126, 10, "EG HzRkl Ventil 1"),
    )
    return tuple(f)


def _dev2_122() -> tuple[Field, ...]:
    """P=122 = P=131 ohne dev2 a13..a16 und ohne digital-Byte (9 B weniger)."""
    f = list(_dev2_118())
    f += (
        Field("a10", 116, 2, False, 100, "analog", "Keller HzRkl Ventil 2 Ergebnis"),
        Field("a11", 118, 2, False, 100, "analog", "EG HzRkl Ventil 2 Ergebnis"),
        Field("a12", 120, 2, False, 100, "analog", "OG HzRkl Ventil 2 Ergebnis"),
    )
    return tuple(f)


def _dev2_131() -> tuple[Field, ...]:
    f = list(_dev2_128())
    f += (
        _a(16, 128, 10, "T. Kollektor (signed, negativ ok)"),
        Field("dig", 130, 1, False, 1, "digital", "dev2 digital-Byte (beobachtet: 0)"),
    )
    return tuple(f)


def _dev2_137() -> tuple[Field, ...]:
    """P=137: dev2 a10..a12 als u32 (Kopfbereitschaft f. groessere Werte), +6 Bytes."""
    return (
        _a(1, 98, 10, "T.Speicher oben"),
        _a(2, 100, 10, "T.Speicher mitte"),
        _a(3, 102, 10, "T.Speicher unten"),
        _a(4, 104, 10, "Keller RAS"),
        _a(5, 106, 10, "Keller Temp"),
        _a(6, 108, 10, "EG RAS"),
        _a(7, 110, 10, "EG Temp"),
        _a(8, 112, 10, "DG RAS"),
        _a(9, 114, 10, "DG Temp"),
        Field("a10", 116, 4, False, 100, "analog", "Keller HzRkl Ventil 2 Ergebnis (u32)"),
        Field("a11", 120, 4, False, 100, "analog", "EG HzRkl Ventil 2 Ergebnis (u32)"),
        Field("a12", 124, 4, False, 100, "analog", "OG HzRkl Ventil 2 Ergebnis (u32)"),
        _a(13, 128, 10, "Keller HzRkl Ventil 1"),
        _a(14, 130, 10, "OG HzRkl Ventil 1"),
        _a(15, 132, 10, "EG HzRkl Ventil 1"),
        _a(16, 134, 10, "T. Kollektor (signed, negativ ok)"),
        Field("dig", 136, 1, False, 1, "digital", "dev2 digital-Byte (0x80 = Solarpumpe)"),
    )


_DEV2_BY_PERIOD = {
    106: _dev2_106(),
    118: _dev2_118(),
    122: _dev2_122(),
    128: _dev2_128(),
    131: _dev2_131(),
    137: _dev2_137(),
}

#: dev0 digital: Kanal-Nummer -> Bit (permutiert, aus CSV-Abgegleiche linear gelöst)
DEV0_DIGIT_BITS = {8: 0, 7: 1, 6: 2, 5: 3, 4: 4, 3: 5, 2: 6, 9: 15}

DEV0_DIG_LABELS = {
    1: "Pumpe Holzofen",
    2: "Pumpe Warmwasser zum Puffer",
    3: "Mischkreis Auf",
    4: "Mischkreis Zu",
    5: "Anforderung Brenner",
    6: "Pumpe WW Zirkulation",
    7: "Zirkulationspumpe",
    8: "Pumpe Heizkreis",
    9: "Ventil Heizungsruecklauf",
}


def decode_dev0_digital(mask: int) -> dict[str, int]:
    """Permutierte u16-Maske -> {dN: 0/1} (nur belegte Kanäle D2..D9)."""
    return {f"d{i}": int((mask >> b) & 1) for i, b in DEV0_DIGIT_BITS.items()}


def decode_dev1_digital(mask: int) -> dict[str, int]:
    """dev1 digital (13 Kanäle), plain Bit-Order angenommen (beobachtet: immer 0)."""
    return {f"d{i}": int((mask >> (i - 1)) & 1) for i in range(1, 14)}


def decode_dev2_digital(byte: int) -> dict[str, int]:
    """dev2 digital-Byte; Bit 7 = Solarpumpe (P=137: 0x80 = an)."""
    return {"d1": int(byte & 0x80 != 0)}


# ---------------------------------------------------------------------------
# Variante / Erkennung
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Variant:
    period: int
    anzdev: int
    data_offset: int
    dev2: tuple[Field, ...] = ()

    def fields_for_dev(self, dev: int) -> tuple[Field, ...]:
        """0-basiertes dev (0=Heizung, 1=RAS, 2=Solar)."""
        if dev == 0:
            return DEV0
        if dev == 1:
            return DEV1
        if dev == 2:
            return self.dev2
        return ()


def _marker_score(buf: bytes, off: int, p: int, end: int, n: int = 2000) -> float:
    """Anteil der ersten `n` Record-Starts in [off, end), die den 'D'-Marker +
    plausibles hh/mm/ss tragen."""
    n = min(n, (end - off) // p)
    if n <= 0:
        return 0.0
    hits = 0
    for k in range(n):
        i = off + p * k
        if buf[i] == D_MARKER and buf[i + 1] <= 23 and buf[i + 2] <= 59 and buf[i + 3] <= 59:
            hits += 1
    return hits / n


def _divisors_in(total: int, lo: int = 90, hi: int = 250) -> list[int]:
    out: set[int] = set()
    i = 1
    while i * i <= total:
        if total % i == 0:
            if lo <= i <= hi:
                out.add(i)
            j = total // i
            if lo <= j <= hi:
                out.add(j)
        i += 1
    return sorted(out)


def _detect_period_in_range(buf: bytes, start: int, end: int) -> int | None:
    """P fuer ein Segment [start, end). Abgerissene letzte Record (Tail < P) wird
    toleriert — gezaehlt werden nur komplette Records.

    Alias-Abwehr: P' = k·P (k ≥ 2) ist ebenfalls Teiler der Groesse und trifft
    jeden k-ten Record (Marker-Score ~1.0, oft sogar hoeher). Die echte Periode
    ist daher der KLEINSTE Kandidat mit Score ≥ 0.9.
    """
    total = end - start
    cands = sorted(set(_divisors_in(total)) | {p for p in KNOWN_PERIODS if 2 * p <= total})
    best: tuple[float, int, int] | None = None
    for p in cands:
        s = _marker_score(buf, start, p, end)
        if s >= 0.9:
            key = (s, 0 if p in KNOWN_PERIODS else 1)
            if best is None or key > (best[0], best[1]):
                best = (s, key[1], p)
    if best is None:
        return None
    # kleinste Periode, die (praktisch) den besten Score erreicht
    threshold = best[0] - 0.002
    for p in cands:
        if _marker_score(buf, start, p, end) >= threshold:
            return p
    return best[2]


@dataclass(frozen=True)
class Session:
    """Ein (evtl. durch Neustart/Update abgetrennter) Block mit einer Periode."""

    variant: Variant
    start: int
    end: int

    @property
    def n_records(self) -> int:
        return (self.end - self.start) // self.variant.period


#: Minimum Anzahl ganzer Records, die nach einem '>>>>'-Mark liegen muss, damit
#: es als echte Session-Grenze (Neustart/Update) gelten duerft. In Record-Daten
#: vorkommende zufaellige `3e 3e 3e 3e`-Sequenzen erzeugen sonst 1-Record-"Sessions"
#: mit Marker-Score 1.0. Beobachtete echte Sessions: >= 69 Records.
MIN_SESSION_RECORDS = 32


def find_sessions(buf: bytes) -> list[Session]:
    """Teilt die Datei in Sessions auf (mehrere '>>>>'-Header moeglich, wenn der
    Logger/WinSol waechstens neugestartet wurde).

    Ein Mark ist eine echte Session-Grenze nur, wenn der folgende Block bis zum
    naechsten Mark (bzw. Dateiende) mind. MIN_SESSION_RECORDS komplette Records
    liefert. Zufallige `>>>>`-Sequenzen in Daten erzeugen 1-Record-"Sessions"
    und werden verworfen.
    """
    markers: list[int] = []
    m = buf.find(MARKER)
    while m >= 0:
        markers.append(m + 4)
        m = buf.find(MARKER, m + 4)
    if not markers:
        raise ValueError("kein '>>>>'-Datenmark im LogX-File (kein LogX-Format?)")
    anzdev = buf[0x19]
    if not 1 <= anzdev <= 3:
        raise ValueError(f"ungueltiger AnzDev-Wert @{0x19}: {anzdev}")
    ends = markers[1:] + [len(buf)]
    sessions: list[Session] = []
    for idx, (start, end) in enumerate(zip(markers, ends, strict=False)):
        p = _detect_period_in_range(buf, start, end)
        if p is None:
            continue
        n_full = (end - start) // p
        if idx > 0 and n_full < MIN_SESSION_RECORDS:
            continue  # zu klein: zufaelliges >>>> in den Daten
        variant = Variant(
            period=p, anzdev=anzdev, data_offset=start, dev2=_DEV2_BY_PERIOD.get(p, ())
        )
        sessions.append(Session(variant, start, end))
    if not sessions:
        raise ValueError("keine eindeutige Record-Periode (Datenstart-/Marker-Score < 0.9)")
    return sessions


def detect_variant(buf: bytes) -> Variant:
    """Variant des dominanten (groessen) Session-Blocks."""
    return max(find_sessions(buf), key=lambda s: s.n_records).variant


# ---------------------------------------------------------------------------
# Dekodierung
# ---------------------------------------------------------------------------
@dataclass
class Record:
    hh: int
    mm: int
    ss: int
    day: str | None = None
    #: {dev_index (0-basiert): {feld_name: Wert}}  — analoge Werte skaliert (raw/scale)
    values: dict[int, dict[str, float | int]] = field(default_factory=dict)

    @property
    def clock(self) -> tuple[int, int, int]:
        return self.hh, self.mm, self.ss

    @property
    def valid_clock(self) -> bool:
        """True, wenn hh/mm/ss im gueltigen Bereich sind (abgerissene/korrumpierte
        Records koennen z. B. 60:60:60 enthalten)."""
        return 0 <= self.hh <= 23 and 0 <= self.mm <= 59 and 0 <= self.ss <= 59

    def ts_str(self) -> str:
        day = self.day or "????-??-??"
        return f"{day} {self.hh:02d}:{self.mm:02d}:{self.ss:02d}"


def _read_field(buf: bytes, base: int, f: Field) -> int:
    if f.width == 1:
        return buf[base + f.off]
    if f.width == 2:
        return struct.unpack_from("<h" if f.signed else "<H", buf, base + f.off)[0]
    if f.width == 4:
        return struct.unpack_from("<i" if f.signed else "<I", buf, base + f.off)[0]
    raise ValueError(f"ununterstuetzte width {f.width}")


def decode_record(rec: bytes, variant: Variant, day: str | None = None) -> Record:
    """Eine Record (laenge = variant.period) in einen :class:`Record` umwandeln."""
    if len(rec) < variant.period:
        raise ValueError("Record zu kurz")
    r = Record(hh=rec[1], mm=rec[2], ss=rec[3], day=day)
    for dev in range(variant.anzdev):
        fields = variant.fields_for_dev(dev)
        vals: dict[str, float | int] = {}
        for f in fields:
            raw = _read_field(rec, 0, f)
            if f.kind == "digital":
                if f.ch == "dig" and dev == 0:
                    vals.update(decode_dev0_digital(raw))
                elif f.ch == "dig" and dev == 1:
                    vals.update(decode_dev1_digital(raw))
                elif f.ch == "dig" and dev == 2:
                    vals.update(decode_dev2_digital(raw))
                else:
                    vals[f.ch] = raw
            elif f.kind == "flag":
                vals[f.ch] = int(raw)
            else:
                vals[f.ch] = raw / f.scale if f.scale != 1 else int(raw)
        r.values[dev] = vals
    return r


def iter_records(
    buf: bytes,
    variant: Variant | None = None,
    day: str | None = None,
    limit: int | None = None,
) -> Iterator[Record]:
    """Yielded Records aus rohem Puff (alle Sessions); unvollstaendiger
    Tail einer Session wird verworfen."""
    if variant is not None:
        sessions = [Session(variant, variant.data_offset, len(buf))]
    else:
        sessions = find_sessions(buf)
    yielded = 0
    for sess in sessions:
        p = sess.variant.period
        for k in range(sess.n_records):
            if limit is not None and yielded >= limit:
                return
            yield decode_record(
                buf[sess.start + p * k : sess.start + p * (k + 1)], sess.variant, day
            )
            yielded += 1


def day_from_filename(path: str | Path) -> str | None:
    name = Path(path).name
    if len(name) == 15 and name[0] in "DAE" and name[1:4].isdigit():
        return name[1:11]
    return None


# ---------------------------------------------------------------------------
# CSV (Rosetta-Stein / Fallback-Quellformat)
# ---------------------------------------------------------------------------
def read_csv(path: str | Path, day: str | None = None) -> tuple[list[str], list[list[str]]]:
    """WinSol-CSV: UTF-32LE (BOM FF FE 00 00), Tab-separiert, 93 Spalten.

    Ein CSV-File kann mehrere Tage enthalten (WinSol exportiert den gewaehlten
    Zeitraum). ``day`` (z. B. ``"2023-01-26"``) filtert auf genau diesen Tag.
    """
    raw = Path(path).read_bytes()
    if raw[:4] == b"\xff\xfe\x00\x00":
        text = raw.decode("utf-32-le")
    else:
        text = raw.decode("utf-32-le", errors="replace")
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        return [], []
    hdr = lines[0].lstrip("\ufeff").split("\t")
    rows: list[list[str]] = []
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0].strip()[:4].isdigit():
            continue
        if day is not None and parts[0].strip() != day:
            continue
        rows.append(parts)
    return hdr, rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WinSol LogX Dekodierer (Read-only)")
    ap.add_argument("file", help="LogX-Datei (.log)")
    ap.add_argument("-n", "--limit", type=int, default=10, help="Anzahl Records (Default 10)")
    ap.add_argument("--all", action="store_true", help="alle Records ausgeben")
    ap.add_argument("--json", action="store_true", help="JSON pro Record (Default: Tabelle)")
    args = ap.parse_args(argv)

    buf = Path(args.file).read_bytes()
    v = detect_variant(buf)
    day = day_from_filename(args.file)
    print(
        f"# {args.file}: P={v.period} AnzDev={v.anzdev} data_offset={v.data_offset} "
        f"records={(len(buf) - v.data_offset) // v.period} day={day}",
        file=sys.stderr,
    )
    limit = None if args.all else args.limit
    for rec in iter_records(buf, v, day, limit):
        if args.json:
            out = {"ts": rec.ts_str(), "dev": {str(d): val for d, val in rec.values.items()}}
            print(json.dumps(out, ensure_ascii=False))
        else:
            print(rec.ts_str())
            for d, val in rec.values.items():
                shown = {k: val[k] for k in ("a1", "a2", "a4", "a6", "a8", "a16", "d1", "d5") if k in val}
                print(f"  dev{d + 1}: {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
