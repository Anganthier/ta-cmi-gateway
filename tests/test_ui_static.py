"""Web-UI tests (S4-1/S4-3/S4N-2): static mount, UI shell, assets, 404s.

No network: the static files come straight from the repository (``ui/`` +
``assets/``).  Auth tests stay in test_api_routes.py; here we verify that the
UI path needs no token and that the API routes keep their priority.

S4N-2 (Loop-Views): die 5 CMI-PNG-Seiten sind entfernt (Q-10); stattdessen
3 Tabs (Start/Anlage/Historie) + Loop-Pills (A–E + Gesamt, Default E, Q-11),
P&ID-Symbol-Bibliothek als /svg/pid-symbols.svg (S4N-0).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import SCHEMATIC_ASSETS

REPO_ROOT = Path(__file__).resolve().parent.parent
UI_ROOT = REPO_ROOT / "ui"


@pytest.fixture()
def ui_client():
    """App with a throwaway data dir (no real DB, no token needed for statics).

    The CMI side is never touched: static routes do no polling, and we do not
    enter the TestClient context manager (that would start the pollers).
    """
    settings = Settings(
        cmi_base_url="http://cmi.test",
        cmi_data_dir=Path("/tmp/cmi-ui-test-unused"),
        cmi_schematic_dir=SCHEMATIC_ASSETS,
        history_db_path=Path("/tmp/cmi-ui-test-unused/history.db"),
        gateway_read_token="read-tok",
        gateway_write_token="write-tok",
    )
    app = create_app(settings)
    # TestClient without context manager: no startup/shutdown, no pollers.
    return TestClient(app)


# ---------------------------------------------------------------------------
# ui/ files exist and are non-empty
# ---------------------------------------------------------------------------
def test_ui_files_exist_and_nonempty():
    expected = {
        "index.html": ("cmi-gateway-ui", "token-input", "vendor/chart.umd.js"),
        "css/app.css": (
            "loop-pill",
            "ki-grid",
            "ki-apply-card",
            # L2-v0.5-UI: Styles für ΔT-Diagnose / Minderer / O8 / Heizkurve
            "ki-dt-dt",
            "ki-minderer-v-target",
            "ki-card-pump",
            # L2-v0.5-engine-rework-UI: O8-Statuszeile + Heizkurve-Details +
            # RAS-Raum-Beleg + Minderer-Empfohlene-Vorgabe-Zeile
            "ki-pump-status",
            "ki-heating-detail",
            "ki-zone-raum",
            "ki-minderer-rec",
        ),
        "js/app.js": ("renderDashboard", "initPills", "initLoopViews", '"ki"'),
        "js/api.js": ("apiGet",),
        "js/dashboard.js": ("DASH_KEYS",),
        "js/history.js": ("runHistoryQuery",),
        "js/loops.js": ("ensureSymbols", "switchLoop"),
        "js/loop-svg.js": ("buildLoopA", "buildLoopE"),
        "js/loop-history.js": ("fetchLoopSeries",),
        "js/state-map.js": ("mapState", "demoState", "LOOP_ORDER"),
        "js/winsol-labels.js": ("WINSOL_LABELS",),
        # S4N-UI-KI: KI-Panel (L2-Empfehlungen + S3-1-Apply-Flow)
        "js/ki.js": (
            "initKIPanel",
            "fetchRecommend",
            "runApply",
            "ki-apply-overlay",
            # L2-v0.5-UI: neue Render-Blocks (ΔT-Diagnose, Minderer, O8-Pumpe,
            # Heizkurven-Card) + Routing-Regel + defensive-Rendering-Helfer
            "ki-diagnostics",
            "ki-minderer",
            "ki-pump",
            "routeRec",
            "rm_diagnostics",
            "Minderer-Feinjustierung",
            "Pumpe Heizkreis O8",
            "Heizkurve",
            "ki-heating",
        ),
        "svg/pid-symbols.svg": ("pid-pump-cent", "pid-tank-buffer"),
        "vendor/chart.umd.js": ("Chart.js",),
        "vendor/chartjs-plugin-zoom.min.js": ("chartjs-plugin-zoom",),
        "vendor/hammer.min.js": ("Hammer",),
    }
    for rel, markers in expected.items():
        p = UI_ROOT / rel
        assert p.is_file(), f"missing ui file: {rel}"
        assert p.stat().st_size > 0, f"empty ui file: {rel}"
        text = p.read_text(encoding="utf-8", errors="replace")
        for marker in markers:
            assert marker in text, f"marker {marker!r} not in ui/{rel}"


def test_index_html_shell_markers():
    html = (UI_ROOT / "index.html").read_text(encoding="utf-8")
    # token handling (S4-1)
    assert 'id="token-overlay"' in html
    assert 'type="password"' in html
    assert 'id="token-input"' in html
    # dashboard (S4-1, Tab "Start")
    assert 'id="dashboard"' in html
    assert 'data-card="aussen"' in html
    assert 'id="dash-modus"' in html
    # S4N-2: 3 Tabs (Start/Anlage/Historie) + Loop-Views (Q-10: PNG-Seiten weg)
    assert 'data-page="start"' in html
    assert 'data-page="anlage"' in html
    assert 'data-page="history"' in html
    assert 'id="loop-pills"' in html
    assert 'id="loop-canvas"' in html
    assert 'id="loop-demo-badge"' in html
    assert 'id="loop-anim-btn"' in html
    # S4N-UI-KI: KI-Tab (nach "Anlage ★", vor "Historie") + Panel + Apply-Dialog
    assert 'data-page="ki"' in html
    assert 'id="page-ki"' in html
    assert 'id="ki-zones"' in html
    assert 'id="ki-advisory"' in html
    # L2-v0.5-UI: neue Container im KI-Tab (ΔT-Diagnose, Minderer, O8-Pumpe)
    assert 'id="ki-diagnostics"' in html
    assert 'id="ki-minderer"' in html
    assert 'id="ki-pump"' in html
    assert 'id="ki-stale-badge"' in html
    assert 'id="ki-refresh-btn"' in html
    assert 'id="ki-apply-overlay"' in html
    assert 'id="ki-apply-confirm"' in html
    assert 'src="/js/ki.js"' in html
    # Default-Pill E (Q-11) + Alt-Tab 2-5 sind NICHT mehr da
    assert 'data-page="2"' not in html
    assert "assets/schematics/1.png" not in html
    # history (S4-3)
    assert 'id="history-canvas"' in html
    assert "vendor/chart.umd.js" in html
    assert "vendor/chartjs-plugin-zoom.min.js" in html
    assert "vendor/hammer.min.js" in html


def test_loop_definitions_complete():
    """S4N-2: alle 5 Loops + Gesamt definiert, je mit 24h-Serien (Q-12)."""
    text = (UI_ROOT / "js" / "state-map.js").read_text(encoding="utf-8")
    for letter in ("A", "B", "C", "D", "E"):
        assert f'{letter}: {{"' in text or f"{letter}: {{" in text, f"Loop {letter} fehlt"
    # Serien je Loop (ui-architecture.md §3.1); Labels aus winsol-labels.js
    # (winLabel()) abgeleitet: a9 (ch9) + a10 (ch10) bei winsol:0.
    assert 'winLabel("winsol:0", 9, "A")' in text, "a9-Serie fehlt"
    assert 'winLabel("winsol:0", 10, "A")' in text, "a10-Serie fehlt"
    assert "winsol:1" in text  # RAS-Serien für Loop E
    assert "DEFAULT_LOOP = \"E\"" in text  # Q-11
    # Fluss-Tokens (mapState)
    for tok in (
        "a-vl", "b-vl", "b-ww", "c-vl", "d-lade", "d-zirk",
        "e-vl", "e-src", "o9a", "o9b",
    ):
        assert tok in text, f"Fluss-Token {tok!r} fehlt"


def test_loop_e_readout_q123_fixes():
    """Q-123 P2/P4: Loop E Readouts — Keller/DG-Korrektur (preferJson, Temp-Kanäle)
    + pro-Kreis Minderer-Stellung (V) und Strang-Rücklauf (°C).

    P2: Schematic-Label Keller/DG ist vertauscht (CMI-Gerät, read-only) → die
    Raum-Ist-Temperaturen nehmen die node32 „Temp“-Kanäle (ch5/7/9) via
    ``preferJson``; der RAS-Perzipiert-Wert (ch4) wird NICHT für rasK verwendet.
    P4: rlK/rlEg/rlDg (°C) + rmK/rmEg/rmDg (json-only, Einheit „V“) in specs
    und in der Loop-E-readouts-Liste.
    """
    text = (UI_ROOT / "js" / "state-map.js").read_text(encoding="utf-8")
    e = text[text.index('letter: "E"') : text.index("export const LOOP_ORDER")]
    # P4: neue Readout-Keys in der Loop-E-Liste
    for k in ("rlK", "rlEg", "rlDg", "rmK", "rmEg", "rmDg"):
        assert f'"{k}"' in e, f"P4: Readout {k} in Loop-E-readouts fehlt"
    # P2: preferJson + korrekte node32-„Temp“-Kanäle (Keller=ch5, EG=ch7, DG=ch9)
    assert 'rasK: { sch: { page: 3, pos: 10 }, json: { node: 32, section: "Logging Analog", ch: 5 }, preferJson: true }' in text, "P2: rasK ≠ node32 Temp-Kanal ch5 (preferJson)"
    assert 'rasEg: { sch: { page: 3, pos: 11 }, json: { node: 32, section: "Logging Analog", ch: 7 }, preferJson: true }' in text, "P2: rasEg ≠ node32 Temp-Kanal ch7 (preferJson)"
    assert 'rasDg: { sch: { page: 3, pos: 12 }, json: { node: 32, section: "Logging Analog", ch: 9 }, preferJson: true }' in text, "P2: rasDg ≠ node32 Temp-Kanal ch9 (preferJson)"
    # P4: Minderer-Stellung json-only (kein sch) + Einheit V
    for ch, k in ((10, "rmK"), (11, "rmEg"), (12, "rmDg")):
        assert f'"{k}"' in text and f'ch: {ch}' in text, f"P4: {k} (node32 ch{ch}) fehlt"
    assert text.count('unit: "V"') >= 3, "P4: Minderer-Readouts ohne Einheit V"
    # analogValue: preferJson-Flag + null-safe sch (rm*-specs haben kein sch)
    assert "function analogValue(doc, idx, sch, json, preferJson = false)" in text, "analogValue ohne preferJson-Parameter"
    assert "const sp = sch ? schPos(doc, sch.page, sch.pos) : null;" in text, "analogValue nicht null-safe für json-only-Specs"


def test_loop_e_h2_topology_regression():
    """S4N-2-Fix-B: H2 (Ölkessel) sitzt im E-Vorlauf (P-Mitte -> H2 -> Mischer).

    Regressionsschutz für die kanon-validierte Topologie (R-B1): H2 bekommt
    3 Anschlüsse (P-Mitte oben, O9-(a) links, VL->Mischer rechts) und der
    alte direkte P-Mitte->Mischer-Pfad ohne H2 ist entfernt.
    """
    text = (UI_ROOT / "js" / "loop-svg.js").read_text(encoding="utf-8")
    e = text[text.index("export function buildLoopE") :
             text.index("export function buildGesamt")]
    # P-Mitte -> H2-Oben (e-src, VL-Orange: physikalisch WARMER Vorlauf, Q-123 P1)
    assert "M120,150 L120,304" in e, "e-src: P-Mitte -> H2-Oben fehlt"
    # Q-123 P1: Zulaufstrecke ist VL-Orange (nicht mehr RL-Blau)
    assert 'flowPipe("M120,150 L120,304 L104,304", VL, "e-src")' in e, "P-Mitte->H2 Zulauf nicht VL-Orange"
    # Q-123 P4: pro-Kreis Minderer (rm) + Strang-Rücklauf (rl) Readouts + zentrale
    # RL-Pipe verbindet alle 3 Einrohr-Zweige (Keller/EG/DG)
    for k in ("rmK", "rmEg", "rmDg", "rlK", "rlEg", "rlDg"):
        assert f'"{k}"' in e, f"P4: Readout-Key {k} in buildLoopE fehlt"
    assert "M950,354 L950,590 L760,590" in e, "P4: zentrale RL-Pipe bindet nicht alle 3 Zweige"
    # H2-Rechts -> Mischer (e-vl)
    assert "M118,330 L190,330" in e, "e-vl: H2-Rechts -> Mischer fehlt"
    # alter direkter P-Mitte -> Mischer-Pfad (ohne H2) muss weg
    assert "L220,150 L220,180" not in e, "alter direkter P-Mitte->Mischer-Pfad noch da"
    # O9-(a) -> H2-Links erhalten
    assert "M700,590 L700,600 L52,600 L52,330 L62,330" in e, "o9a: O9-(a) -> H2 fehlt"
    # H2 in E nicht mehr gedimmt
    assert 'href: "#pid-boiler-oil", x: 90, y: 330, class: "sym dim"' not in e
    assert 'href: "#pid-boiler-oil", x: 90, y: 330, class: "sym"' in e


def test_winsol_labels_curation():
    """winsol-labels.js: labels come from Setup.xml <Bezeichnungen>."""
    text = (UI_ROOT / "js" / "winsol-labels.js").read_text(encoding="utf-8")
    # device names (DevBez)
    assert "winsol:0" in text and "winsol:1" in text and "winsol:2" in text
    # spot labels (a1/a4 dev0, a2/a5/a8 dev1 RAS room temps, a16 dev2)
    assert "Speicher Oben" in text
    assert "Warmwasser" in text
    assert "Temp Keller" in text
    assert "Temp Erdgeschoss" in text
    assert "Temp Dachgeschoss" in text
    assert "T. Kollektor" in text
    assert "Solarpumpe" in text


def test_history_labels_ad_aware():
    """Q-129 (UI-HistFix): Historie-Tab-Labels sind A/D-aware.

    Root-Cause: die Multi-Serie-Response-Keys von /api/v1/history sind
    ``device:channel`` (KEINE ad-Dimension) -- das alte
    ``channelLabel(device, key, doc)`` loeste aus der Kanal-Nummer,
    also bei A+D-Zwilling immer das analoge Label (D-Auswahl ch 8/9
    dev0 -> "Temperatur Aussen" / "Holzkessel Vorlauf"). Fix: die
    Dataset-Labels kommen aus der Checkbox-Auswahl ``{channel, ad}``
    (``seriesLabelForSelection`` im neuen Modul
    ``ui/js/history-labels.js``); die unveraenderte Alt-Logik
    ``channelLabel`` dient dort als Fallback (json:/schematic), wird
    nicht dupliziert und erzeugt keinen Import-Kreislauf.
    """
    labels_js = (UI_ROOT / "js" / "history-labels.js").read_text(encoding="utf-8")
    history_js = (UI_ROOT / "js" / "history.js").read_text(encoding="utf-8")
    # Neues Modul: A/D-aware Funktion + kuratierte Labels + Fallback
    for marker in (
        "export function seriesLabelForSelection(device, item, items, doc)",
        "export function channelLabel(device, key, doc)",
        'from "./winsol-labels.js"',
        "(A+D)",  # dokumentiertes Mixed-Label (A+D-Union, kein ad-Filter)
    ):
        assert marker in labels_js, f"marker {marker!r} fehlt in ui/js/history-labels.js"
    # history.js: importiert das Modul und loest die Dataset-Labels aus
    # der Auswahl (Response-Key nur noch fuer die Punkte-Zuordnung)
    for marker in (
        'from "./history-labels.js"',
        "seriesLabelForSelection(device, matched, items, lastState)",
    ):
        assert marker in history_js, f"marker {marker!r} fehlt in ui/js/history.js"
    # Regressionsschutz: das alte LABEL-Zuordnungs-Statement (direkt aus
    # dem Response-Key) ist weg; channelLabel taucht in history.js nur noch
    # als Fallback fuer Restserien ohne Auswahl-Match auf.
    assert "const label = channelLabel(device, key, lastState);" not in history_js, (
        "history.js loest Labels weiterhin (unbedingt) aus dem Response-Key"
    )
    assert "function channelLabel(" not in history_js, (
        "channelLogik-Koerper in history.js dupliziert (wohnt in history-labels.js)"
    )


def test_pid_symbols_serveable_and_wellformed():
    """S4N-0: Symbol-Bibliothek wird als statische Datei ausgeliefert."""
    p = UI_ROOT / "svg" / "pid-symbols.svg"
    text = p.read_text(encoding="utf-8")
    assert text.startswith("<svg")
    for sym in (
        "pid-pump-cent",
        "pid-valve-2way",
        "pid-valve-3way",
        "pid-heatex-shell",
        "pid-heatex-plate",
        "pid-tank-buffer",
        "pid-boiler-oil",
        "pid-boiler-solid",
        "pid-collector-solar",
        "pid-pt",
        "pid-flow-switch",
        "pid-radiator",
        "pid-throttle",
        "pid-chimney",
        "pid-tap",
    ):
        assert f'id="{sym}"' in text, f"P&ID-Symbol {sym} fehlt"


# ---------------------------------------------------------------------------
# static routes
# ---------------------------------------------------------------------------
def test_ui_index_served_at_root_no_token(ui_client):
    r = ui_client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "cmi-gateway-ui" in r.text
    assert "token-input" in r.text


def test_ui_static_files_served(ui_client):
    for path in (
        "/css/app.css",
        "/js/app.js",
        "/js/loops.js",
        "/js/loop-svg.js",
        "/js/loop-history.js",
        "/js/state-map.js",
        "/js/winsol-labels.js",
        "/js/ki.js",
        "/svg/pid-symbols.svg",
        "/vendor/chart.umd.js",
        "/vendor/hammer.min.js",
        "/vendor/chartjs-plugin-zoom.min.js",
    ):
        r = ui_client.get(path)
        assert r.status_code == 200, path
        assert len(r.content) > 100


def test_png_assets_still_served_by_assets_mount(ui_client):
    """assets/ bleibt gemountet (S4N-3 V-G-Referenz + docs); die UI
    referenziert die PNGs aber nicht mehr (Q-10)."""
    for page in range(1, 6):
        r = ui_client.get(f"/assets/schematics/{page}.png")
        assert r.status_code == 200, page
        assert r.headers["content-type"] == "image/png"
        assert len(r.content) > 1000
    html = ui_client.get("/assets/schematics/1.html")
    assert html.status_code == 200


def test_unknown_ui_path_404s(ui_client):
    assert ui_client.get("/nope.css").status_code == 404
    assert ui_client.get("/assets/schematics/99.png").status_code == 404


def test_ki_js_selectors_exist_in_index_html():
    """S4N-UI-KI: jeder #id-Selektor / getElementById in ui/js/ki.js muss in
    ui/index.html existieren (minimaler DOM-Check ohne Test-Framework)."""
    import re

    ki_js = (UI_ROOT / "js" / "ki.js").read_text(encoding="utf-8")
    html = (UI_ROOT / "index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([\w-]+)"', html))
    # $("#...") und getElementById("...") Selektoren
    selectors = set(re.findall(r'\$\("#([\w-]+)"\)', ki_js))
    selectors |= set(re.findall(r'getElementById\("([\w-]+)"\)', ki_js))
    missing = selectors - ids
    assert not missing, f"ki.js referenziert fehlende id(s): {sorted(missing)}"
    # Kern-Elemente des KI-Panels
    for required in ("ki-zones", "ki-advisory", "ki-stale-badge", "ki-apply-overlay"):
        assert required in ids, f"KI-Panel-Element #{required} fehlt in index.html"


def test_ki_js_v005_routing_kanon():
    """L2-v0.5-UI: Routing-Kanon in ki.js — Parameter-Präfixe steuern die
    Buckets (Minderer/O8/Heizkurve vor RAS-Offset/Zone-Karten); O8- und
    Minderer-Karten erhalten KEIN Anwenden-Button (Design-Entscheidung
    2026-11-09), das v0.4-Apply-Flow bleibt für Zone-Karten (Tier A).

    v0.5-engine-rework-UI (2026-11-12): O8 = Statuszeile (advisory-manuell,
    kein to_value/executability_mix), Minderer = Sollspannung als
    Ist-Vorgabe (Gate ≥ 2 Kreise in der Engine entfernt), RAS-Zone-Karten
    mit prominentem Raum-Beleg (raum_text), Heizkurve mit einklappbaren
    Details (detail-Feld)."""
    ki_js = (UI_ROOT / "js" / "ki.js").read_text(encoding="utf-8")
    # Routing-Kanon: Präfix-Tests in der richtigen Reihenfolge (Minderer/O8/
    # Heizkurve vor dem RAS-Offset/Fallback)
    pos_m = ki_js.index('p.startsWith("Minderer-Feinjustierung")')
    pos_p = ki_js.index('p.startsWith("Pumpe Heizkreis O8")')
    pos_h = ki_js.index('p.startsWith("Heizkurve")')
    pos_r = ki_js.index('p.startsWith("RAS-Offset")')
    assert pos_m < pos_p < pos_h < pos_r, "Routing-Reihenfolge verletzt"
    # O8: Statuszeile (advisory-manuell), kein Anwenden-Button, kein
    # Arrow-Modus→Empfehlung; statischer Fallback für fehlendes cmi_hint
    assert "Advisory (manuell am Aggregat)" in ki_js
    assert "O8_STATUS_FALLBACK" in ki_js
    assert "Stufe/Druck manuell am Aggregat" in ki_js
    # v0.5-Statuszeile ohne Stufung-Extraktion (executability_mix entfällt)
    assert "executability_mix" not in ki_js
    # Minderer: Sollspannung als Ist-Vorgabe (engine-rework), Badge bleibt;
    # das alte Gate "≥ 2 aktive Kreise" ist entfernt (kein Gate-Hinweis)
    assert "Minderer-Feinjustierung (Advisory)" in ki_js
    assert "Ist-Vorgabe (Sollspannung)" in ki_js
    assert "Nur 1 aktiver Kreis" not in ki_js
    # RAS-Zone: maintainer-Beleg-Format prominent (raum_text) + Handlungs-Hint
    assert "ki-zone-raum" in ki_js
    assert "Raum-Soll bei Bedarf direkt im UVR/TA-Designer setzen (Offset intern)" in ki_js
    # Heizkurven-Card: Kalibrierungs-Fallback-Hinweis + einklappbare Details
    assert "Kalibrierung nicht verfügbar (v0.4-Heuristik aktiv)" in ki_js
    assert "ki-heating-detail" in ki_js
    # defensive: fehlende rm_diagnostics -> Block bleibt hidden
    assert "renderBlockSection" in ki_js
    # v0.4-Apply-Flow bleibt intakt (nur Zone-Karten bekommen Buttons)
    assert "ki-apply-btn" in ki_js
    assert "bindApplyButtons" in ki_js


def test_ki_js_node_syntax_check():
    """node --check (falls Node.js vorhanden): Syntax-Check für ki.js + app.js."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available in this environment")
    for rel in ("js/ki.js", "js/app.js"):
        proc = subprocess.run(
            [node, "--check", str(UI_ROOT / rel)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"node --check {rel} failed: {proc.stderr}"


def test_api_routes_keep_priority_over_static(ui_client):
    """Unknown API routes must 404 from the router, not fall through to ui/."""
    r = ui_client.get("/api/v1/does-not-exist")
    assert r.status_code == 404
    assert "cmi-gateway-ui" not in r.text


def test_auth_still_enforced_on_api(ui_client):
    r = ui_client.get("/api/v1/state")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").startswith("Bearer")
