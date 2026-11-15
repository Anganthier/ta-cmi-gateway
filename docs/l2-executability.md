# L2 v0.2 — Dynamische Executability-Markierung (2026-11-02)

**Status:** Implementiert + Gates grün (Subagent-Lauf); Deploy = separater
Orchestrator-HITL-Schritt (siehe §4). **Kein Commit** (Orchestrator).

L2 v0.2 ersetzt die statische v0.1-Markierung
`executability="pending-cmi-write-discovery"` (alle Empfehlungen identisch)
durch eine **discovery-basierte, je-Empfehlung** Markierung aus dem
CMI-Write-Pfad-Discovery (`scripts/cmi_write_discovery/INVENTAR.json`:
9 Adressen mit `risk`/`writeable`/`option_values`/`current_value`).
Die 4 Empfehlungstypen bleiben (maintainer-Option 2: **keine neuen
Schalter-Empfehlungen**); `data_version`/L1-Pinning unverändert (v003);
`model_version` → **`cmi-brain-v003+l2-v002`**.

**Safety (bewusst, unverändert):** L2 emittiert **weiterhin KEIN**
`adresse`/`to_value` → UI-„Anwenden“ bleibt inaktiv (UI-`EXECUTABLE`-Gate
in `ui/js/ki.js` kennt keine `advisory-*`-Labels). Die Markierung ist
Advisory-Metadaten, kein CMI-Write-Pfad; Safety-Oberfläche unverändert.

## 1) Mapping-Tabelle (4 Typen → advisory-Label + Q-Bezug)

Kuratiertes Mapping: `analysis/model/l2/config.py::ADVISORY_MAP`
(Parameter → (Label, Q-Bezug)). Discovery-Bezug (Schalter-Auswahl) wird
per Klassen-Matching über den inventarisierten `label` aufgelöst —
nie über hartkodierte Adressen im Ergebnis-Pfad.

| L2-Parameter | Recommendation-`parameter` | `executability` (v0.2) | Q-Bezug / Hinweis | Discovery-Bezug |
|---|---|---|---|---|
| `offset_kell` | RAS-Offset kell | `advisory-uvr` | Q-107 (RAS-Offset-Write, UVR1611-Programmupload/TA-Designer) | nicht in den 9 Schematic-Adressen → „nicht im CMI-Schematic-Write-Pfad“ |
| `offset_eg` | RAS-Offset eg | `advisory-uvr` | Q-107 (dito) | dito |
| `offset_dg` | RAS-Offset dg | `advisory-uvr` | Q-107 (dito) | dito |
| `vl_curve` | Heizkurve/VL-Soll | `advisory-ta-designer` | Q-108 (Heizkurven-Parameter, TA-Designer) | nicht in den 9 Schematic-Adressen → „nicht im CMI-Schematic-Write-Pfad“ |
| `ww_window` | WW-Ladefenster | `advisory-zeitprogramm` | Zeitprogramm-Parameter (WW-Ladefenster) | CMI-Schalter „WW vom Oel“ (`10220A10180`, writeable=true) → **cmi-setzbar**, L2 empfiehlt ihn bewusst nicht |
| `circulation` | Zirkulation | `advisory-uvr` | UVR-Programmier-Aufgabe (kein Live-Parameter) | CMI-Schalter „Zirkulation“ (`10720A10180`, writeable=true) → cmi-setzbar, L2 empfiehlt ihn bewusst nicht |
| `oel_burner_on` (exkludiert, nie empfohlen) | — | `risk-blocked` | Ölbrenner (RISK-Schutz) | Discovery-Eintrag `10520A10180` `risk=true` → nie setzbar (abgeleitet aus `risk`-Flag, nie aus Adresse im Ergebnis) |

Alle `advisory-*`-Labels ∉ UI-`EXECUTABLE` (`setzbar-per-cmi`,
`cmi-settable`, `whitelist`) → UI rendert Badge „Advisory (TA-Designer/v2)“
und **kein** „Anwenden“-Button (Test:
`test_ui_executable_gate_false_for_all_advisory_labels`).

## 2) Fail-Closed-Verhalten

- **`load_discovery(path=None)`**: Pfad-Reihenfolge
  Env `CMI_WRITE_INVENTAR` → Default
  `scripts/cmi_write_discovery/INVENTAR.json` (Repo-Pfad,
  `config.INVENTAR_DEFAULT_PATH`). **Fehlend/ungültig** (fehlende Datei,
  kaputtes JSON, `entries` leer/fehlend, Eintrag ohne `address`,
  `risk`/`writeable` nicht boolean, `option_values` ungültig) → `None`.
  Nur Datei-Lesung, **0 Live-Netz** (Discovery-Fixtur im Test).
- **`executability_for(parameter, zone, discovery=None)`**:
  - Discovery-`None` (fehlend/ungültig) → kuratierte **Advisory-Labels**
    aus `ADVISORY_MAP` ohne Discovery-Bezug (Hinweis: „INVENTAR.json
    fehlend/ungültig, Fail-Closed“) — **nie** `cmi-settable`.
  - Unbekannter Parameter (kein kuratiertes Mapping) → Fallback
    `EXECUTABILITY_PENDING` (`pending-cmi-write-discovery`, bleibt in
    `config.py` als Fail-Closed-Fallback; ebenfalls ∉ UI-`EXECUTABLE`).
  - RISK-Schutz: Parameter ↔ Discovery-Eintrag mit `risk=true` →
    `risk-blocked` (Ölbrenner; nie setzbar) — vor dem Mapping-Check.
- Discovery wird **einmal pro `recommend()`-Lauf** geladen
  (`executability.load_discovery()` am Beginn) und allen
  Empfehlungspfaden (Tier A Zone-Advice, Tier B Advisories, Stale-Pfad)
  injiziert — deterministisch, kein erneutes Laden pro Empfehlung.
- **Stale-Pfad** ebenfalls discovery-markiert (keine Modell-Aktionen,
  aber korrektes `executability`-Badge).

## 3) Geänderte/neue Dateien (Arbeitsbereich)

| Datei | Änderung |
|---|---|
| `analysis/model/l2/executability.py` | **NEU**: `Discovery`/`DiscoveryEntry` (validiert), `load_discovery()` (Env→Default, Fail-Closed→`None`), `inventar_path()`, `executability_for()` (Mapping + RISK-Schutz + Discovery-Bezug im `cmi_hint`), Sentinel-Default (frisch laden) vs. explizit `None` (kein Discovery, kein Nachladen) |
| `analysis/model/l2/config.py` | `L2_VERSION="l2-v002"`; `INVENTAR_DEFAULT_PATH`; neue `EXECUTABILITY_*`-Konstanten (`RISK_BLOCKED`, `CMI_SETTABLE`, `ADVISORY_UVR`, `ADVISORY_TA_DESIGNER`, `ADVISORY_ZEITPROGRAMM`); `ADVISORY_MAP`; `EXECUTABILITY_PENDING` bleibt als Fail-Closed-Fallback; `CMI_HINTS` bleibt (v0.1-kompatible Basis-Hints, inkl. exkludierter Parameter) |
| `analysis/model/l2/recommend.py` | 5 Emissionsstellen (`_zone_advice`, 3× `tier_b_advisories`, Stale-Pfad) → Resolver `executability.executability_for(...)` via Helper `_exec(...)`; Discovery einmal pro `recommend()`-Lauf geladen; REPORT-„Executability-Map“-Sektion je-Empfehlung statt „alle pending“; Modul-Doku v0.2 |
| `analysis/model/l2/__init__.py` | Export `Discovery`, `executability_for`, `load_discovery`; Doku v0.2 |
| `analysis/model/l2/__main__.py` | Versions-Label „L2 v0.2“ (Kosmetik) |
| `analysis/model/l2/tests/test_executability.py` | **NEU**, 17 Test-Funktionen / 28 Test-Fälle (s. §5) |
| `analysis/model/l2/tests/conftest.py` | Neue Fixture `store_full`/`st_full` (triggert alle 4 Empfehlungstypen: Puffer 55 °C + Zirkulation 15 h/24 h) |
| `analysis/model/l2/tests/test_engine.py` | `test_executability_all_pending` (v0.1) → `test_executability_per_type_advisory` (v0.2: je-Typ-Wert + `cmi_hint` + Discovery-Bezug) |
| `analysis/model/l2/tests/test_cli.py` | DE-Summary-Assertion „L2 v0.1“ → „L2 v0.2“ |
| `Plan.md` | Status-Zeile §L2 v0.2: „offen“ → „in Arbeit“ |

**Anfassfrei (einhaltend):** `docker-compose.yml`, `Dockerfile`, `app/*`,
`ui/*`, `data/*`, `models/*`, `analysis/model/{m1,m2b}/*`,
`scripts/cmi_write_discovery/*` (nur read-only gelesen),
`docs/plant-model*`. `tests/test_recommend_route.py` (App-Seite)
untouched — die Route mockt die Engine; der Mock-Payload
(`pending-cmi-write-discovery`) ist v0.1-Struktur-Fixtur und bricht
nicht.

## 4) Deployment-Bedarf Gateway-Container (DOKUMENTIERT — nicht ausgeführt)

L2-Code läuft **im Gateway-Container** (`COPY analysis ./analysis`) →
v0.2-Code-Änderung braucht einen **Docker-Rebuild + Container-Start**
(separater HITL-Schritt, wie S4N-UI-KI). Das Discovery-Artefakt liegt
**host-seitig, NICHT im Image** → der Container braucht:

1. **ro-Mount** (docker-compose `volumes:`):
   ```yaml
   - ./scripts/cmi_write_discovery:/srv/cmi/scripts/cmi_write_discovery:ro
   ```
2. **Env** (docker-compose `environment:` bzw. compose-Template):
   ```yaml
   CMI_WRITE_INVENTAR: /srv/cmi/scripts/cmi_write_discovery/INVENTAR.json
   ```
   (Ohne Env: Default-Repo-Pfad `analysis/model/l2/../../scripts/...`
   existiert im Image nicht → `load_discovery()` → `None` →
   Fail-Closed: advisory-Labels ohne Discovery-Bezug, Engine läuft
   unverändert weiter, nie `cmi-settable`.)

Host-seitig (Subagent/CLI `python -m analysis.model.l2`) lauffähig ohne
zusätzliche Konfiguration (Default-Repo-Pfad vorhanden).

**Deploy-DoD (Orchestrator):** Container healthy;
`GET /api/v1/recommend` (Read-Token) → `model_version=…+l2-v002` +
advisory-`executability` (nicht `pending`); `stale`-Semantik
unverändert; UI-„KI“-Panel rendert Advisory-Badges, **kein**
„Anwenden“-Button (kein `adresse`/`to`); **0 CMI-Write**.

## 5) Gates (Subagent-Lauf, alle erfüllt)

| Gate | Ergebnis |
|---|---|
| `pytest analysis/model/m1 analysis/model/l2 analysis/model/m2b -q` | **grün** (v0.1-Baseline 322 + 28 neue L2-Tests = 350; 0 failed) |
| `ruff check analysis/model/l2/` | **clean** |
| Coverage `executability.py` | **98 %** (DoD ≥ 80 %) |
| 0 Live-Netz in Tests | Discovery = tmp-/Repo-Datei-Fixtur; Open-Meteo gemockt (`WxLive`-Fixtur, wie v0.1) |
| kein pandas | nur `json`/`pathlib`/`dataclasses` (neue Module) |
| Smoke (host, realer Store + realer INVENTAR) | `L2 v0.2 · cmi-brain-v003+l2-v002 · advisory-uvr` (je Zone); `CMI_WRITE_INVENTAR=/tmp/nicht-da.json` → Fail-Closed, advisory-Labels ohne Discovery-Bezug, kein Crash |

Neue Tests (`test_executability.py`, 17 Test-Funktionen / 28 Test-Fälle): je-Typ-Wert + `cmi_hint`
(6 parametrized + WW-Schalter-Bezug + Q-107-nicht-Schematic),
Fail-Closed missing/invalid JSON (root/Eintrags-Validierung, 7
parametrized), Fail-Closed Ende-zu-Ende (fresh + stale, Env-Override,
nie pending/cmi-settable), RISK-blocked-Pfad (fixture-Discovery,
`risk`-Flag-abgeleitet), Determinismus (2 Läufe → identische
Markierung + JSON), `model_version=l2-v002`, kein `adresse`/`to_value`
(JSON-Wege + UI-EXECUTABLE-Gate false), Pfad-Auflösung Env/Default,
Real-INVENTAR-Abgleich (9 Adressen, genau 1 RISK = `10520A10180`).

## 6) Offene Punkte / Grenzen

- **Kein Commit** (Orchestrator); `CIR-Log.md`-Änderung im Repo gehört
  zum parallelen M1-Inkremental-Lauf (nicht dieser Task).
- **Deploy** (Docker-Rebuild + ro-Mount + Env + Live-Smoke) =
  Orchestrator-HITL (s. §4) — danach L2-v0.2 live im Gateway.
- RAS-Offsets/Heizkurven bleiben Advisory (Q-107/108: UVR-Upload/
  TA-Designer-Track, v2) — erst mit echtem Write-Pfad + HITL je Change
  (S3-1) wäre `cmi-settable` + UI-Apply denkbar; DoD „Anwenden nur bei
  setzbar“ bleibt erfüllt.
- `EXECUTABILITY_PENDING` bleibt im Vokabular (Fail-Closed-Fallback für
  unbekannte Parameter + v0.1-Backward-Compat, z. B.
  `tests/test_recommend_route.py`-Mock-Fixtur).
- Cron unverändert (M1-Inkremental läuft weiter; L2 liest nur den
  Store). `data_version` unverändert (m1.3 / L1 v003).
