// CMI Gateway UI -- Einstieg (S4-1 + S4-3 + S4N-2).
//
// Vanilla-JS-Modul: Token-Handling, Auto-Refresh (30 s, ?refresh= 5..300),
// Dashboard (Tab "Start"), 5 Loop-Views + Gesamt (Tab "Anlage", S4N-2),
// Historie-Charts (Tab "Historie").

import { apiGet, getStoredToken, storeToken } from "./api.js";
import { renderDashboard } from "./dashboard.js";
import {
  initPills,
  initLoopViews,
  loopsOnState,
} from "./loops.js";
import {
  defaultChannels,
  destroyChart,
  resetZoom,
  renderChannelList,
  runHistoryQuery,
  selectedChannelSet,
} from "./history.js";

// S1 TV-Modus: `?tv=1` (1/true/yes) wird vom Inline-Head-Script (index.html)
// BEVOR die Module laden auf <html class="tv"> gesetzt (Anti-Flash +
// ?token=->localStorage-Vorablage, Key "cmi-gateway.read-token" aus api.js).
// Hier nur Konsequenzen ziehen: Read-only (ki-Tab ausgeblendet, ki->anlage)
// + Fernbedienung-D-pad-Navigation + Initial-Fokus auf dem aktiven Tab.
function isTvMode() {
  return document.documentElement.classList.contains("tv");
}

const REFRESH_PARAM = "refresh";
const REFRESH_MIN = 5;
const REFRESH_MAX = 300;
const REFRESH_DEFAULT = 30;
const PAGES = ["start", "anlage", "ki", "history"];

let token = getStoredToken();
let refreshMs = currentRefreshMs();
let stateTimer = null;
let lastState = null;
let channelsRenderedOnce = false;
const chartRef = { chart: null };

function $(sel, root = document) {
  return root.querySelector(sel);
}

function currentRefreshMs() {
  const params = new URLSearchParams(window.location.search);
  // ACHTUNG: Number(null) === 0 -- ohne params.has-Pruefung wuerde ein
  // fehlender ?refresh=-Param auf den 5-s-Minimum geclampt statt auf
  // den 30-s-Standard.
  const raw = params.has(REFRESH_PARAM)
    ? Number(params.get(REFRESH_PARAM))
    : Number.NaN;
  if (!Number.isFinite(raw)) return REFRESH_DEFAULT * 1000;
  return Math.min(Math.max(raw, REFRESH_MIN), REFRESH_MAX) * 1000;
}

// ---------------------------------------------------------------------------
// Token-Handling
// ---------------------------------------------------------------------------
function showTokenOverlay() {
  const overlay = $("#token-overlay");
  const input = $("#token-input");
  // Vorbefuellen NUR, wenn das Feld leer ist -- ein eingegebener (noch
  // nicht submitteter) Wert darf nie zurueckgesetzt werden. Das
  // touched-Flag (bindTokenUI) markiert Aufruf/Bearbeitung des Feldes;
  // leer bedeutet unangetastet, dann ist das Vorbefuellen sicher.
  if (!input.value) {
    input.value = token || "";
  }
  overlay.classList.remove("hidden");
  input.focus();
  if (!input.value) {
    input.select();
  }
}

function setTokenError(message) {
  const errEl = $("#token-error");
  if (!errEl) return;
  errEl.textContent = message || "";
  errEl.classList.toggle("hidden", !message);
}

function hideTokenOverlay() {
  $("#token-overlay").classList.add("hidden");
}

function bindTokenUI() {
  const input = $("#token-input");
  // Touch-Flag: sobald der Nutzer das Feld bearbeitet/aufsucht, kein
  // stilles Vorbefuellen mehr (siehe showTokenOverlay).
  const markTouched = () => {
    input.dataset.touched = "1";
  };
  input.addEventListener("focus", markTouched);
  input.addEventListener("input", markTouched);

  $("#token-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    token = input.value.trim();
    if (!token) {
      // Sichtbare Fehlermeldung statt stilles Return.
      setTokenError("Bitte Token eingeben (Feld ist leer).");
      input.focus();
      return;
    }
    storeToken(token);
    setTokenError(""); // Fehlermeldung ausblenden (erfolgreicher Login-Pfad)
    hideTokenOverlay();
    refreshNow();
  });
  $("#token-cancel").addEventListener("click", hideTokenOverlay);
  $("#token-btn").addEventListener("click", showTokenOverlay);
}

function handle401() {
  // Token ungültig -> neu abfragen (S4-1) + Fehlermeldung IM Overlay.
  showTokenOverlay();
  setTokenError(
    "Token ungültig — bitte den Gateway-Read-Token aus .env (GATEWAY_READ_TOKEN) neu eingeben."
  );
}

// ---------------------------------------------------------------------------
// Data-Loop
// ---------------------------------------------------------------------------
async function refreshNow() {
  try {
    await runRefresh();
  } finally {
    // Auto-Refresh muss auf JEDEM Completion-Pfad neu gearmt werden
    // (Success, 401, HTTP-Fehler, Netzwerk-Fehler, fehlendes Token).
    // scheduleRefresh() macht selbst clearTimeout -> doppeltes Arming
    // ist ausgeschlossen (Defect-Report: Loop starb nach init()+1 Tick).
    // Nach Cancel ohne Token fuehrt der naechste Tick automatisch wieder
    // zum Token-Overlay (runRefresh -> showTokenOverlay).
    scheduleRefresh();
  }
}

async function runRefresh() {
  if (!token) {
    showTokenOverlay();
    return;
  }
  let state = null;
  let health = null;
  let winsol = {};

  try {
    state = await apiGet("/api/v1/state", token);
  } catch (e) {
    if (e.status === 401) {
      handle401();
      return;
    }
    markStateError(e);
    // state bleibt null -> Loop-Views fallen in Demo-Mode (Badge "Demo-Daten")
  }

  // /api/v1/health antwortet 503, wenn das CMI down ist -- der Koerper ist
  // dann trotzdem vollstaendig (gateway/winsol-Blocks). 503 ist also ein
  // VALIDER Health-Report: rohes fetch + JSON-Parse statt apiGet (die
  // woerfe bei 503 -> winsol available=true wuerde nie angezeigt).
  try {
    const hres = await fetch("/api/v1/health");
    if (hres.ok || hres.status === 503) {
      health = await hres.json();
    }
  } catch {
    health = null;
  }
  if (health) winsol = health.winsol || {};

  if (state) {
    lastState = state;
    renderDashboard(state, health || { cmi: {} }, winsol);
  }

  // S4N-2: Loop-Views (Live-Status + Animation + 24h-Chart-Refresh).
  // state == null (HTTP-/Netzwerkfehler) -> Demo-Daten + Badge.
  loopsOnState(state, health, token, handle401);

  // Erster Render: Standard-Kanale (z. B. winsol:1 -> 2/5/8) aktiv; danach
  // die (ggf. geaenderte) Auswahl des Nutzers beibehalten.
  const device = $("#hist-device").value;
  const selection = selectedChannelSet();
  const effective = !channelsRenderedOnce
    ? defaultChannels(device)
    : selection.length
      ? selection
      : defaultChannels(device);
  channelsRenderedOnce = true;
  renderChannelList(device, state, effective);
  refreshInfo();
}

function markStateError(e) {
  const detail = $("#status-detail");
  const chip = $('[data-chip="cmi"]');
  if (e && e.status !== undefined) {
    // HTTP-Fehlerpfad (Gateway antwortet, State z. B. 5xx).
    if (detail) {
      detail.innerHTML = `<span class="stale-badge">State nicht erreichbar (HTTP ${e.status})</span>`;
    }
    if (chip) {
      chip.className = "chip chip-err";
      chip.textContent = "CMI down";
    }
  } else {
    // Netzwerkfehler (fetch wirft, kein HTTP-Status): das GATEWAY ist
    // nicht erreichbar -- kein CMI-Status.
    if (detail) {
      detail.innerHTML =
        '<span class="stale-badge">Gateway nicht erreichbar (Netzwerk)</span>';
    }
    if (chip) {
      chip.className = "chip chip-err";
      chip.textContent = "Gateway down";
    }
  }
}

function refreshInfo() {
  const el = $("#refresh-info");
  if (el) el.textContent = `Auto-Refresh ${Math.round(refreshMs / 1000)} s`;
}

function scheduleRefresh() {
  if (stateTimer) clearTimeout(stateTimer);
  stateTimer = setTimeout(refreshNow, refreshMs);
}

// ---------------------------------------------------------------------------
// Tabs (S4N-2): start | anlage | history
// ---------------------------------------------------------------------------
function bindTabs() {
  const tabs = [...document.querySelectorAll(".tab")];
  const pages = [...document.querySelectorAll(".page")];
  // S1 TV-Modus: Roving-Fokus per D-pad (Fernbedienung) ueber die
  // sichtbaren Tabs. Sichtbarkeit = computed display != none, damit der
  // per CSS ausgeblendete ki-Tab (Read-only) uebersprungen wird, auch
  // wenn CSS nicht geladen sein sollte.
  // ArrowLeft/ArrowRight: Tab wechseln (am Rand umschalten),
  // Enter/Space: nativer Button-Trigger (keine Extra-Logik).
  // ArrowUp/ArrowDown: nur preventDefault (kein Page-Scroll), ausser ein
  // Input-Element hat Fokus (z. B. Token-Overlay).
  const isVisible = (el) => getComputedStyle(el).display !== "none";
  if (isTvMode()) {
    document.addEventListener("keydown", (ev) => {
      const visible = tabs.filter(isVisible);
      const i = visible.findIndex((t) => t === document.activeElement);
      if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
        ev.preventDefault();
        const dir = ev.key === "ArrowRight" ? 1 : -1;
        const next = i >= 0 ? i + dir : dir === 1 ? 0 : visible.length - 1;
        const target = visible[(next + visible.length) % visible.length];
        if (target) target.focus();
      } else if (ev.key === "ArrowUp" || ev.key === "ArrowDown") {
        const tag = (document.activeElement && document.activeElement.tagName) || "";
        if (tag !== "INPUT" && tag !== "TEXTAREA") ev.preventDefault();
      }
    });
  }
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const page = tab.dataset.page;
      tabs.forEach((t) => t.classList.toggle("active", t === tab));
      pages.forEach((p) =>
        p.classList.toggle("active", p.dataset.page === page)
      );
      if (page === "history" && !chartRef.chart) {
        queryHistory();
      }
    });
  });
}

// ---------------------------------------------------------------------------
// Historie (S4-3)
// ---------------------------------------------------------------------------
async function queryHistory() {
  await runHistoryQuery(token, lastState, chartRef, handle401);
}

function bindHistory() {
  const deviceSel = $("#hist-device");
  const rangeSel = $("#hist-range");

  deviceSel.addEventListener("change", () => {
    // Gerat-Wechsel: Standard-Kanale neu anbieten (winsol:1 -> 2/5/8,
    // uebrige: leer) und Kanal-Liste aus dem letzten State rendern.
    const chs = defaultChannels(deviceSel.value);
    if (lastState) {
      renderChannelList(deviceSel.value, lastState, chs);
    }
  });

  rangeSel.addEventListener("change", () => {
    const custom = rangeSel.value === "custom";
    $("#hist-start-wrap").hidden = !custom;
    $("#hist-end-wrap").hidden = !custom;
    if (!custom) {
      // Auto-Intervall-Vorschlag pro Zeitraum anwenden.
      const presets = { 24: 900, 168: 3600, 720: 21600, 8760: 86400 };
      $("#hist-interval").value = String(presets[rangeSel.value] || 900);
    }
  });

  $("#hist-query").addEventListener("click", queryHistory);
  $("#hist-zoom-reset").addEventListener("click", () => resetZoom(chartRef));
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
function init() {
  bindTokenUI();
  bindTabs();
  bindHistory();
  initPills();

  const params = new URLSearchParams(window.location.search);
  let requested = params.get("page");
  // S1 TV-Modus: Read-only -- die KI-Seite (Write-Einstieg) ist im TV-Modus
  // nicht erreichbar; ?page=ki faellt auf die Default-Ansicht "anlage"
  // zurueck (kein Hash-State in der UI; Seiten-State = ?page= + .active).
  if (isTvMode() && requested === "ki") requested = "anlage";
  const initialPage = PAGES.includes(requested) ? requested : "anlage";
  const tab = document.querySelector(`.tab[data-page="${initialPage}"]`);
  if (tab && !tab.classList.contains("active")) tab.click();
  // S1 TV-Modus: aktiven Tab sofort fokussieren, damit die Fernbedienung
  // (D-pad/OK) ohne vorherige Interaktion funktioniert.
  if (isTvMode() && tab) tab.focus();

  // S4N-2: P&ID-Symbol-Decls (fetch) + Default-Loop E (Q-11) bauen.
  // Asynchron; der erste State-Refresh rendert die Loop-Views unabhängig
  // (applyModel nach initLoopViews-Start).
  initLoopViews(token, handle401);

  if (!token) {
    showTokenOverlay();
  }
  refreshNow();
  scheduleRefresh();
}

window.addEventListener("DOMContentLoaded", init);

// Sauberes Chart-Cleanup beim Verlassen
window.addEventListener("beforeunload", () => {
  if (stateTimer) clearTimeout(stateTimer);
  destroyChart(chartRef);
});

export { refreshNow, init, chartRef };
