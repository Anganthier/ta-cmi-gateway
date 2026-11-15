"""L2 v0.2 — dynamische Executability-Markierung (discovery-basiert, Advisory).

Ersetzt die statische v0.1-Markierung ``pending-cmi-write-discovery`` (alle
Empfehlungen identisch) durch eine **je-Empfehlung** Markierung aus dem
CMI-Write-Pfad-Discovery (``scripts/cmi_write_discovery/INVENTAR.json``,
9 Adressen mit ``risk``/``writeable``/``option_values``).

Kuratiertes Parameter→Klassen-Mapping (:data:`~.config.ADVISORY_MAP`):

========================  ==============================  =========
L2-Parameter              Executability (v0.2)            Q-Bezug
========================  ==============================  =========
RAS-Offset (kell/eg/dg)   ``advisory-uvr``                Q-107
Heizkurve/VL-Soll         ``advisory-ta-designer``        Q-108
WW-Ladefenster            ``advisory-zeitprogramm``       —
Zirkulation               ``advisory-uvr``                —
========================  ==============================  =========

**RISK-Schutz:** ist ein Parameter einem RISK-Schalter (Ölbrenner
``10520A10180``) zugeordnet, trägt er ``risk-blocked`` (niemals setzbar) —
abgeleitet aus dem Discovery-``risk``-Flag, nie aus einer hartkodierten
Adresse im Ergebnis-Pfad.

**Fail-Closed:** fehlt das Discovery-Artefakt oder ist es ungültig →
``load_discovery()`` gibt ``None`` zurück und ``executability_for`` fällt
auf die Advisory-Labels des Mappings zurück (``EXECUTABILITY_PENDING``
nur bei unbekannten Parametern) — **nie** ``cmi-settable``, nie ein
RISK-Schalter.

**Safety (bewusst):** L2 emittiert **weiterhin KEIN** ``adresse``/
``to_value`` → UI-„Anwenden“ bleibt inaktiv (UI-``EXECUTABLE``-Gate
untätig); die Markierung ist Advisory-Metadaten, kein Write-Pfad.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config

_UNLOADED = object()  # Sentinel: Default = frisch laden; explizit None = kein Discovery


@dataclass(frozen=True)
class DiscoveryEntry:
    """Eine inventarisierte CMI-Schalter-Adresse (INVENTAR ``entries[]``)."""

    address: str
    label: str
    risk: bool
    writeable: bool
    option_values: tuple[str, ...] = ()
    current_value: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DiscoveryEntry:
        """Validiert einen ``entries[]``-Dict (Fehler → ValueError)."""
        if not isinstance(d, dict):
            raise ValueError(f"Discovery-Eintrag ist kein Objekt: {d!r}")
        addr = d.get("address")
        if not isinstance(addr, str) or not addr:
            raise ValueError("Discovery-Eintrag: 'address' fehlt (oder leer)")
        risk = d.get("risk")
        writeable = d.get("writeable")
        if not isinstance(risk, bool) or not isinstance(writeable, bool):
            raise ValueError(
                f"Discovery-Eintrag {addr}: 'risk'/'writeable' müssen boolean sein"
            )
        opts = d.get("option_values") or []
        if not isinstance(opts, (list, tuple)) or any(
            not isinstance(o, (str, int, float)) for o in opts
        ):
            raise ValueError(f"Discovery-Eintrag {addr}: 'option_values' ungültig")
        return cls(
            address=addr,
            label=str(d.get("label") or ""),
            risk=risk,
            writeable=writeable,
            option_values=tuple(str(o) for o in opts),
            current_value=d.get("current_value"),
        )


@dataclass
class Discovery:
    """Ladbares Discovery-Artefakt (INVENTAR.json, read-only)."""

    path: Path
    entries: tuple[DiscoveryEntry, ...] = ()
    risk_entries: tuple[DiscoveryEntry, ...] = field(default_factory=tuple)

    @classmethod
    def parse(cls, raw: dict[str, Any], path: Path) -> Discovery:
        """INVENTAR-JSON → :class:`Discovery` (strukturfremd → ValueError)."""
        entries_raw = raw.get("entries")
        if not isinstance(entries_raw, list) or not entries_raw:
            raise ValueError("INVENTAR: 'entries' fehlt oder ist leer")
        entries = tuple(DiscoveryEntry.from_dict(e) for e in entries_raw)
        return cls(
            path=path,
            entries=entries,
            risk_entries=tuple(e for e in entries if e.risk),
        )


def inventar_path() -> Path:
    """Discovery-Pfad: Env ``CMI_WRITE_INVENTAR`` → Default-Repo-Pfad."""
    env = os.environ.get("CMI_WRITE_INVENTAR")
    if env and env.strip():
        return Path(env).expanduser()
    return config.INVENTAR_DEFAULT_PATH


def load_discovery(path: Path | str | None = None) -> Discovery | None:
    """INVENTAR.json laden (read-only, kein Netzwerk).

    Pfad-Reihenfolge: explizites ``path`` → Env ``CMI_WRITE_INVENTAR`` →
    Default-Repo-Pfad (``scripts/cmi_write_discovery/INVENTAR.json``).
    Fehlend/ungültig → ``None`` (**Fail-Closed**): die Engine emittiert dann
    die kuratierten Advisory-Labels, aber **nie** ``cmi-settable``.
    """
    p = Path(path).expanduser() if path else inventar_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("INVENTAR-Root ist kein JSON-Objekt")
        return Discovery.parse(raw, p)
    except (OSError, ValueError, json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None


# Kuratiertes Parameter → Discovery-Entry (Klassen-Matching über den
# inventarisierten Label). L2 empfiehlt bewusst KEINEN dieser Schalter
# (maintainer-Option 2); die Zuordnung dient dem RISK-Schutz + dem Discovery-
# Bezug in ``cmi_hint`` (z. B. „CMI-Schalter 'Zirkulation' (10720A10180)
# cmi-setzbar, L2 empfiehlt ihn bewusst nicht“). ``oel_burner_on`` (exkludiert,
# nie empfohlen) → Ölbrenner (RISK-Schutz: risk=true → risk-blocked).
_PARAMETER_CLASS_LABELS: dict[str, tuple[str, ...]] = {
    "ww_window": ("WW vom Oel",),
    "circulation": ("Zirkulation",),
    "oel_burner_on": ("Oelbrenner",),
    # v0.5 Ticket B: O8 „Pumpe Heizkreis“ (10820A10180, select AUS/EIN/AUTO) —
    # einziger cmi-setzbarer L2-Parameter (S3-1, HITL je Change). writeable +
    # non-RISK im Discovery → ``cmi-settable`` (aus dem Mapping); risk →
    # risk-blocked; Discovery fehlt → Fail-Closed → advisory (nie setzbar).
    "pump_o8": ("Pumpe Heizkreis",),
}


def _entry_for_parameter(
    parameter: str, discovery: Discovery | None
) -> DiscoveryEntry | None:
    """Discovery-Eintrag für einen L2-Parameter (klassenbasiert, nie
    adressbasiert). Nicht zugeordnet (z. B. RAS-Offset/Heizkurve — nicht
    im Schematic-Write-Pfad) → ``None``."""
    if discovery is None:
        return None
    needles = _PARAMETER_CLASS_LABELS.get(parameter, ())
    for e in discovery.entries:
        if any(n.lower() in e.label.lower() for n in needles):
            return e
    return None


def executability_for(
    parameter: str,
    zone: str | None = None,
    discovery: Discovery | None | object = _UNLOADED,
) -> tuple[str, str]:
    """Je-Empfehlung-Executability + ``cmi_hint`` (deterministisch).

    ``parameter``: L2-Parameter-Name (``offset_kell``/``offset_eg``/
    ``offset_dg``/``vl_curve``/``ww_window``/``circulation``).
    ``zone``: Zonen-Name bei RAS-Offsets (nur für den Hint).
    ``discovery``: Discovery-Artefakt. Default (sentinel) = frisch laden
    (Env→Default-Pfad). **Explizit** ``None`` = „kein Discovery“
    (Fail-Closed-Simulation; es wird NICHT nachgeladen) — die kuratierten
    Advisory-Labels gelten, aber nie ``cmi-settable``.

    Rückgabe ``(executability, cmi_hint)``:

    - Parameter einem RISK-Schalter zugeordnet → ``risk-blocked``
      (abgeleitet aus dem Discovery-``risk``-Flag; nie setzbar).
    - Kuratierter Mapping-Treffer → Advisory-Label + Discovery-Bezug im
      Hint (Schalter-Auswahl: ``cmi-settable``-Status; Nicht-Schalter:
      Advisory-Klasse ohne Schematic-Write-Pfad).
    - Unbekannt → Fail-Closed-Fallback ``EXECUTABILITY_PENDING``.

    **Nie** ein UI-``EXECUTABLE``-Wert, kein ``adresse``/``to_value``
    (UI-„Anwenden“ bleibt inaktiv).
    """
    if discovery is _UNLOADED:
        disc: Discovery | None = load_discovery()
    else:
        disc = discovery if isinstance(discovery, Discovery) else None

    # --- RISK-Schutz (aus Discovery 'risk'-Flag, nie aus Adresse im Ergebnis) ---
    ent = _entry_for_parameter(parameter, disc)
    if ent is not None and ent.risk:
        return (
            config.EXECUTABILITY_RISK_BLOCKED,
            f"RISK-Schutz: Parameter {parameter!r} ist RISK-Schalter "
            f"„{ent.label or ent.address}“ (Discovery risk=true) → "
            "niemals setzbar (kein L2-Empfehlungszugriff)",
        )

    mapped = config.ADVISORY_MAP.get(parameter)
    if mapped is None:
        # Fail-Closed-Fallback (unbekannt): Advisory-Haltung, nie cmi-settable.
        return (
            config.EXECUTABILITY_PENDING,
            config.CMI_HINTS.get(parameter, "CMI-Bezug: offen (kein kuratiertes Mapping)"),
        )

    label, q_ref = mapped
    if (
        label == config.EXECUTABILITY_CMI_SETTABLE
        and parameter in config.CMI_SETTABLE_PARAMS
        and disc is None
    ):
        # Fail-Closed (v0.5 Ticket B): ``cmi-settable`` NUR mit
        # Discovery-Verifizierung (writeable + non-RISK). Kein/ungültiges
        # Discovery → Advisory (nie cmi-settable), S3-1 nur nach Re-Check.
        return (
            config.EXECUTABILITY_PENDING,
            f"{q_ref}; Fail-Closed: kein gültiges Discovery (INVENTAR.json "
            "fehlend/ungültig) → O8-Modus erst nach Re-Verifizierung "
            "(writeable + non-RISK) per S3-1, Advisory",
        )
    if ent is not None:
        if label == config.EXECUTABILITY_CMI_SETTABLE:
            # v0.5 Ticket B: O8-Modus = der (einzige) empfohlene CMI-Schalter
            # (S3-1, HITL je Change) — KEIN „bewusst nicht empfohlen“.
            hint = (
                f"{q_ref}; CMI-Schalter „{ent.label or ent.address}“ "
                f"({ent.address}, {ent.current_value or '—'}) cmi-setzbar "
                "(Discovery writeable=true) — S3-1-Write-Pfad, HITL je Change "
                "(L2 v0.5; kein Auto-Write, kein Adresse/To-Value im L2-JSON)"
            )
        else:
            hint = (
                f"{q_ref}; CMI-Schalter „{ent.label or ent.address}“ "
                f"({ent.address}, {ent.current_value or '—'}) "
                + ("cmi-setzbar (Discovery writeable=true), L2 empfiehlt ihn bewusst nicht"
                   if ent.writeable
                   else "NICHT cmi-setzbar (Discovery writeable=false)")
            )
    elif disc is not None:
        hint = (
            f"{q_ref}; Advisory — nicht im CMI-Schematic-Write-Pfad "
            f"(Discovery {disc.path.name})"
        )
    else:
        hint = f"{q_ref}; Advisory — kein Discovery-Bezug (INVENTAR.json fehlend/ungültig, Fail-Closed)"
    return label, hint
