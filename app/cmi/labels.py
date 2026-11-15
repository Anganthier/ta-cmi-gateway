"""Positional labels for schematic values whose label is NOT a ``posN`` div.

Most value blocks have a CMI label div *above* them in the same column;
:mod:`app.cmi.schematic_parser` resolves those automatically by proximity
(see :func:`app.cmi.schematic_parser._nearest_label`).  A smaller set of
value blocks sits where the CMI paints the label into the PNG graphic (or
uses a bare ``RL``/``VL`` pipe marker), so those positions are labelled here
by hand.  Every entry is grounded, not invented:

* the recorded ``discovery/live_*.cgi`` fixtures (value magnitudes + positions),
* the WinSol channel-label model
  ``discovery/winsol/Setup.xml`` (the authoritative device channel names, e.g.
  a1=Speicher Oben, a2=Speicher Mitte, a3=Speicher Unten, a4=Warmwasser,
  a5=Oelkessel Vorlauf, a7=Heizkreis Ruecklauf, a9=Holzkessel Vorlauf,
  a10=Abgastemperatur Holz, a16=Stroemungsschalter),
* a live ``api.cgi`` cross-check (node 1 inputs = WinSol a1..a16, node 32
  I/O designations) taken on 2026-10-27, and
* the TA-Designer drawings ``/home/user/Development/CMI_WebTest/Tapps2/*.tdw``
  (component names: Speicher, Pumpe, 2/3-Wegeventil, Festbrennstoffkessel,
  "Abgas Holz AN/AUS").

Priority order (see :func:`app.cmi.schematic_parser._resolve_label`):
proximity (same page) -> this :data:`LABEL_MAP` -> generated ``S{page}.{pos}``
id.

Source legend for the report:
* ``proximity`` -- label came from a nearby CMI label div (auto, per page).
* ``labelmap``  -- label curated here, grounded as documented above.
"""

from __future__ import annotations

#: (page, pos) -> human-readable label (only the blocks that have no nearby
#: CMI label div; everything else is resolved by proximity in the parser).
LABEL_MAP: dict[tuple[int, int], str] = {
    # --- Page 2 (Warmwasser): storage tank + WW pumps (PNG-baked) -----------
    # WinSol node-1 a1..a4 + the two WW pumps + oil-charge pump.
    (2, 9): "Speicher Oben",
    (2, 10): "Speicher Mitte",
    (2, 11): "Speicher Unten",
    (2, 12): "Warmwasser",
    (2, 13): "Ladepumpe WW",
    (2, 14): "Zirkulationspumpe",
    (2, 19): "Oel-Ladepumpe",
    # --- Page 3 (Raumregelung): room temps + supply rails + pipe markers ----
    # Room temps are WinSol dev-1 a2/a8 (Keller/DG); the RL/VL pipe temps
    # (pos13..17) carry the CMI's own "RL"/"VL" markers via proximity.
    (3, 10): "Keller Raumtemperatur",
    (3, 12): "Dachgeschoss Raumtemperatur",
    # pos18 is the Oelkessel (oil boiler) supply -- WinSol node-1 a5
    # (live cross-check: p3.pos18 == node1.a5 "Oelkessel Vorlauf").
    (3, 18): "Oelkessel Vorlauf",
    # pos19/20/21 are the CMI supply-voltage readouts (0 V / 10 V / 0 V).
    (3, 19): "Versorgung 0 V",
    (3, 20): "Versorgung 10 V",
    (3, 21): "Versorgung 0 V",
    # pos35 is the heating-pump status shown on the room page (digital EIN).
    (3, 35): "Pumpe Heizkreis (Status)",
    # --- Page 4 (Solar): storage + collector + solar pump (PNG-baked) -------
    # WinSol UVR610 a1..a3 + a16 (Kollektor) + d1 (Solarpumpe).
    # Live cross-check: p4.pos09 == node-32 a16 "T.Kollektor 1".
    (4, 9): "Kollektor",
    (4, 10): "Speicher Oben",
    (4, 11): "Speicher Mitte",
    (4, 12): "Speicher Unten",
    (4, 13): "Solarpumpe",
    # --- Page 5 (Kamin): storage + wood boiler (PNG-baked) ------------------
    # WinSol node-1 a1..a3 + a9 (Holzkessel Vorlauf); the .tdw "Abgas Holz
    # AN/AUS" component sits at pos15 (digital EIN/AUS).
    (5, 9): "Speicher Oben",
    (5, 10): "Speicher Mitte",
    (5, 11): "Speicher Unten",
    (5, 15): "Abgas Holz AN/AUS",
    # pos18 is the wood-boiler supply -- WinSol node-1 a9 (live cross-check:
    # p5.pos18 == node1.a9 "Holzkessel Vorlauf").
    (5, 18): "Holzkessel Vorlauf",
}


def label_for(page: int, pos: int) -> str | None:
    """Return the curated label for a schematic position, if known."""
    return LABEL_MAP.get((page, pos))
