// Loop-Views (S4N-2): 5 Loop-Ansichten (A–E) + „Gesamt“ (Pill-Selektor).
//
// Pro Loop-View (ui-architecture.md §3.1):
//   * Live-Status: Inline-SVG (loop-svg.js, P&ID-Symbole S4N-0) mit
//     Voll-Animation (Flow/Pumpen/Ventile/Kessel) aus state-map.js
//   * 24h-Chart: alle relevanten Serien des Loops in EINEM Chart (Q-12),
//     Daten via loop-history.js (/api/v1/history)
//   * Status-Chips (Pumpen/Ventile) + setzbare Adressen (Read-only, S4N-2b
//     bringt das Steuerungs-Overlay)
//   * Default-Pill = E Heizung (Q-11); Demo-Mode: Gateway down -> synthetische
//     Werte + Badge „Demo-Daten“ (ui-architecture.md §4)

import { apiGet } from "./api.js";
import {
  LOOPS,
  LOOP_ORDER,
  DEFAULT_LOOP,
  GESAMT,
  mapState,
  demoState,
  withDemoJsonFields,
} from "./state-map.js";
import { BUILDERS } from "./loop-svg.js";
import { fetchLoopSeries } from "./loop-history.js";

const ANIM_KEY = "cmi-gateway.animation";
const SVG_NS = "http://www.w3.org/2000/svg";

// ---------------------------------------------------------------------------
// P&ID-Symbol-Decls (S4N-0): ui/svg/pid-symbols.svg -> Inline-<defs>
// ---------------------------------------------------------------------------
// Die Loop-Views referenzieren die Symbole per <use href="#pid-...">. Damit
// das im DOKUMENT-Context löst (und per CSS färb-/animierbar bleibt), wird
// der <defs>-Block (inkl. <style>) der Symbol-Bibliothek einmalig in ein
// unsichtbares Inline-SVG injiziert. Externe-Datei-<use> wäre kein
// Document-Context (kein CSS, keine State-Classes).
let symbolsReady = null;

function ensureSymbols() {
  if (symbolsReady) return symbolsReady;
  symbolsReady = (async () => {
    const res = await fetch("/svg/pid-symbols.svg");
    if (!res.ok) throw new Error(`P&ID-Symbole nicht ladbar (HTTP ${res.status})`);
    const text = await res.text();
    const doc = new DOMParser().parseFromString(text, "image/svg+xml");
    if (doc.querySelector("parsererror")) {
      throw new Error("P&ID-Symbole: SVG-Parsefehler");
    }
    const host = document.createElementNS(SVG_NS, "svg");
    host.id = "pid-defs";
    host.setAttribute("width", "0");
    host.setAttribute("height", "0");
    host.setAttribute("aria-hidden", "true");
    const style = doc.querySelector("style");
    if (style) host.appendChild(style.cloneNode(true));
    const defs = doc.querySelector("defs");
    if (defs) {
      for (const child of Array.from(defs.children)) {
        host.appendChild(document.importNode(child, true));
      }
    }
    document.body.appendChild(host);
  })();
  return symbolsReady;
}

// Status-Chip-Labels (key -> Anzeige)
const STATUS_LABELS = {
  o1: "O1 · Holz-Pumpe",
  o5: "O5 · Ölbrenner (RISK)",
  o6: "O6 · Öl→P (WW-Modus)",
  o2: "O2 · Lade-Pumpe WW",
  o7: "O7 · Zirkulationspumpe",
  o8: "O8 · Heizkreis-Pumpe",
  o9: "O9 · HzRl-Ventil",
  sv: "SV · Solarpumpe",
  svMode: "SV · Solarpumpe-Modus",
  abgas: "Abgas AN/AUS",
  a16ss: "a16 · Strömungsschalter",
  mischer: "Mischer O3/O4 (C1-intern)",
};

/** Modul-State (singleton-Controller). */
const S = {
  token: null,
  on401: null,
  active: DEFAULT_LOOP,
  model: null,
  demo: false,
  lastState: null,
  chart: null,
  chartBusy: false,
  animOn: true,
};

function $(sel, root = document) {
  return root.querySelector(sel);
}

// ---------------------------------------------------------------------------
// Pills (Loop-Selektor)
// ---------------------------------------------------------------------------
export function initPills() {
  const host = $("#loop-pills");
  host.replaceChildren();
  for (const key of [...LOOP_ORDER, GESAMT]) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "loop-pill";
    btn.dataset.loop = key;
    btn.setAttribute("role", "tab");
    btn.textContent = key === GESAMT ? "Gesamt" : `${key} · ${LOOPS[key].name}`;
    btn.addEventListener("click", () => switchLoop(key));
    host.append(btn);
  }
  markActivePill();

  // Animation-Schalter (ui-architecture §1.5, Default: an)
  try {
    const stored = localStorage.getItem(ANIM_KEY);
    S.animOn = stored == null ? true : stored === "1";
  } catch {
    S.animOn = true;
  }
  const anim = $("#loop-anim-btn");
  syncAnimUI();
  anim.addEventListener("click", () => {
    S.animOn = !S.animOn;
    try {
      localStorage.setItem(ANIM_KEY, S.animOn ? "1" : "0");
    } catch {
      /* Private Mode */
    }
    syncAnimUI();
  });
}

function syncAnimUI() {
  const btn = $("#loop-anim-btn");
  if (!btn) return;
  document.body.classList.toggle("no-anim", !S.animOn);
  btn.textContent = S.animOn ? "Animation: an" : "Animation: aus";
  btn.setAttribute("aria-pressed", S.animOn ? "true" : "false");
}

function markActivePill() {
  for (const p of document.querySelectorAll(".loop-pill")) {
    const on = p.dataset.loop === S.active;
    p.classList.toggle("active", on);
    p.setAttribute("aria-selected", on ? "true" : "false");
  }
}

/** Loop-Ansicht wechseln (Pill-Klick). */
export function switchLoop(key) {
  if (key === S.active && $("#loop-svg-host").childElementCount) return;
  S.active = key;
  markActivePill();
  renderLoopFrame();
  applyModel();
  if (key !== GESAMT) {
    renderLoopChart();
  } else {
    destroyChart();
  }
}

/** Loop-Rahmen (Titel, Beschreibung, SVG, Status-Chips, setzbar) neu bauen. */
function renderLoopFrame() {
  const key = S.active;
  const isGesamt = key === GESAMT;
  const loop = isGesamt ? null : LOOPS[key];

  $("#loop-title").textContent = isGesamt ? "Gesamtdarstellung (Doku)" : loop.title;
  $("#loop-desc").textContent = isGesamt
    ? "Statische Referenz: wie hängt das alles zusammen. Live-Readouts + Pan/Zoom: S4N-3."
    : loop.desc;

  // SVG (Cache: einmal pro Loop bauen, danach nur class-/Wert-Updates)
  const host = $("#loop-svg-host");
  const cached = host.dataset.loop;
  if (cached !== key || !host.childElementCount) {
    host.replaceChildren(BUILDERS[key]());
    host.dataset.loop = key;
  }

  // Status-Chips
  const statusHost = $("#loop-status");
  statusHost.replaceChildren();
  if (!isGesamt) {
    for (const k of loop.statuses) {
      const chip = document.createElement("span");
      chip.className = "chip chip-muted";
      chip.dataset.status = k;
      chip.textContent = STATUS_LABELS[k] || k;
      statusHost.append(chip);
    }
  }

  // Setzbare Adressen (Read-only; Steuerungs-Overlay = S4N-2b)
  const setHost = $("#loop-settable");
  setHost.replaceChildren();
  if (!isGesamt && loop.settable.length) {
    const head = document.createElement("span");
    head.className = "settable-title";
    head.textContent = "Setzbar (Read-only)";
    setHost.append(head);
    for (const s of loop.settable) {
      const item = document.createElement("span");
      item.className = "settable-item";
      item.dataset.setkey = s.key;
      item.title = "S4N-2b / S4-2: Steuerungs-Overlay mit Bestätigungs-Dialog (Whitelist-Write, Read-back)";
      const dot = document.createElement("span");
      dot.className = "ov-addr";
      item.append(dot);
      const label = document.createElement("span");
      label.className = "settable-label";
      label.textContent = `${s.id} · ${s.label}`;
      const addr = document.createElement("code");
      addr.textContent = s.addr;
      const st = document.createElement("span");
      st.className = "settable-state chip-muted";
      st.dataset.setstate = s.key;
      st.textContent = "–";
      item.append(label, addr, st);
      setHost.append(item);
    }
  }

  // Chart-Bereich ein-/ausblenden
  $("#loop-chart-area").classList.toggle("hidden", isGesamt);
}

// ---------------------------------------------------------------------------
// State-Apply: SVG + Chips gegen das Loop-Modell aktualisieren
// ---------------------------------------------------------------------------
export function applyModel() {
  const m = S.model;
  const key = S.active;
  if (!m || !$("#loop-title")) return; // Loop-Views noch nicht initialisiert

  // Demo-Badge
  $("#loop-demo-badge").classList.toggle("hidden", !S.demo);

  // Modus-Badge (R-X4)
  const mode = $("#loop-mode-badge");
  mode.textContent = m.modus ? `Modus ${m.modus}` : "Modus –";
  mode.classList.toggle("hidden", !m.modus);

  if (key === GESAMT) return;

  const loop = LOOPS[key];
  const svg = $("#loop-svg-host svg");
  if (svg) {
    applyFlow(svg, m.flow[key] || new Set());
    applyReadouts(svg, m, loop);
  }

  // Status-Chips
  for (const k of loop.statuses) {
    const chip = $(`[data-status="${k}"]`);
    if (!chip) continue;
    const sw = m.switches[k];
    if (!sw) {
      chip.className = "chip chip-warn";
      chip.textContent = `${STATUS_LABELS[k] || k} · –`;
      continue;
    }
    chip.className = `chip ${sw.on ? "chip-ok" : "chip-muted"}`;
    chip.textContent = `${STATUS_LABELS[k] || k} · ${sw.text || (sw.on ? "an" : "aus")}`;
  }

  // setzbare Items: aktueller State
  for (const s of loop.settable) {
    const st = $(`[data-setstate="${s.key}"]`);
    if (!st) continue;
    const sw = m.switches[s.key];
    st.textContent = sw ? `State: ${sw.text || "–"}` : "State: –";
  }
}

/** Fluss-Tokens auf .flow/.pump-imp/.valve-state/.boiler anwenden. */
function applyFlow(svg, active) {
  for (const n of svg.querySelectorAll("[data-flow]")) {
    const on = active.has(n.dataset.flow);
    n.classList.toggle("on", on);
    n.classList.toggle("off", !on);
  }
}

/** Readout-Werte (PT-Boxes) + Strömungsschalter aktualisieren. */
function applyReadouts(svg, m, loop) {
  for (const n of svg.querySelectorAll("[data-readout]")) {
    const key = n.dataset.readout;
    const valEl = n.querySelector(".ro-val");
    if (!valEl) continue;
    if (key === "a16ss") {
      const sw = m.switches.a16ss;
      valEl.textContent = sw ? sw.text : "–";
      n.classList.toggle("on", !!(sw && sw.on));
      continue;
    }
    const r = m.readouts[key];
    const meta = READOUT_META_OF(r);
    valEl.textContent = r && r.value != null ? fmtVal(r.value) : "–";
    n.classList.toggle("has-value", !!(r && r.value != null));
    n.classList.remove("demo-ghost");
    void meta;
  }
}

function READOUT_META_OF(r) {
  return r; // Einheit kommt immer aus °C (heute alle Temperatur-Slots)
}

function fmtVal(v) {
  return Number(v).toLocaleString("de-DE", { maximumFractionDigits: 1 });
}

// ---------------------------------------------------------------------------
// 24h-Chart (loop-history.js + Chart.js)
// ---------------------------------------------------------------------------
let lastChartKey = null;
let lastChartTs = 0; // ms, letzter erfolgreicher Chart-Fetch
const CHART_MAX_AGE_MS = 5 * 60 * 1000; // 24h-Daten: max. alle 5 min neu laden

// Fenster-Fallback: 24 h ist die Default-View (Q-12); wenn im Fenster KEINE
// Daten sind (z. B. WinSol-Logging pausiert / Live-Polling jung), wird die
// Ansicht automatisch breiter, bis Daten sichtbar sind (max. 365 d).
const WINDOW_LADDER = [
  { hours: 24, interval: 900 },
  { hours: 168, interval: 3600 },
  { hours: 720, interval: 21600 },
  { hours: 8760, interval: 86400 },
];

export async function renderLoopChart(force = false) {
  if (!S.token || S.active === GESAMT) return;
  const loop = LOOPS[S.active];
  const fresh =
    !force && lastChartKey === S.active && S.chart &&
    Date.now() - lastChartTs < CHART_MAX_AGE_MS;
  if (S.chartBusy || fresh) return;
  S.chartBusy = true;
  const loading = $("#loop-chart-loading");
  const errEl = $("#loop-chart-error");
  errEl.classList.add("hidden");
  loading.classList.remove("hidden");

  try {
    let res = null;
    let hardError = null;
    for (const win of WINDOW_LADDER) {
      res = await fetchLoopSeries(S.token, loop.series, win);
      if (res.error && !res.datasets.length) {
        hardError = res.error; // API-Fehler: nicht breiter weiterfragen
        break;
      }
      const total = res.datasets.reduce((n, d) => n + d.data.length, 0);
      if (total > 0) break; // Daten im Fenster -> fertig
    }
    if (hardError) {
      showChartError(hardError);
      return;
    }
    if (!res.datasets.some((d) => d.data.length)) {
      showChartError("Keine Daten im Zeitraum (24 h … 365 d) — Kanäle ohne Messwerte.");
      return;
    }
    buildLoopChart(res.datasets, res.startMs, res.endMs, res.ms, res.hours);
  } catch (e) {
    if (e.status === 401 && S.on401) S.on401();
    else showChartError(`Historie nicht erreichbar: ${e.detail || e.message}`);
  } finally {
    loading.classList.add("hidden");
    S.chartBusy = false;
  }
}

function showChartError(msg) {
  const errEl = $("#loop-chart-error");
  errEl.textContent = msg;
  errEl.classList.remove("hidden");
}

function buildLoopChart(datasets, startMs, endMs, fetchMs, hours = 24) {
  const canvas = $("#loop-canvas");
  const ctx = canvas.getContext("2d");
  destroyChart();

  const ds = datasets.map((d) => ({
    label: d.label,
    data: d.data,
    borderColor: d.color,
    backgroundColor: d.color + "20",
    borderWidth: 1.8,
    pointRadius: 0,
    pointHoverRadius: 4,
    tension: d.digital ? 0 : 0.15,
    stepped: d.digital ? "end" : false, // digitale Kanäle: Sprunglinien
    spanGaps: true,
  }));

  const span = endMs - startMs;
  S.chart = new Chart(ctx, {
    type: "line",
    data: { datasets: ds },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 250 },
      interaction: { mode: "nearest", axis: "x", intersect: false },
      plugins: {
        legend: { labels: { color: "#e6edf7", font: { size: 11 } } },
        tooltip: {
          callbacks: {
            title: (items) => (items.length ? `Stand ${shortTs(items[0].raw.raw || "")}` : ""),
            label: (item) => ` ${item.dataset.label}: ${item.parsed.y}`,
          },
        },
        zoom: {
          pan: { enabled: true, mode: "x" },
          zoom: { wheel: { enabled: true }, pinch: { enabled: true } },
          limits: { x: { min: startMs, max: endMs } },
        },
      },
      scales: {
        x: {
          type: "linear",
          min: startMs,
          max: endMs,
          ticks: {
            color: "#8fa0bd",
            maxTicksLimit: 8,
            callback: (v) => {
              const d = new Date(v);
              const p = (n) => String(n).padStart(2, "0");
              return span >= 36 * 3600000
                ? `${p(d.getDate())}.${p(d.getMonth() + 1)}`
                : `${p(d.getHours())}:${p(d.getMinutes())}`;
            },
          },
          grid: { color: "rgba(36, 50, 80, 0.5)" },
        },
        y: {
          ticks: { color: "#8fa0bd", maxTicksLimit: 6 },
          grid: { color: "rgba(36, 50, 80, 0.5)" },
        },
      },
    },
  });
  lastChartKey = S.active;
  lastChartTs = Date.now();

  const total = ds.reduce((n, d) => n + d.data.length, 0);
  const winLabel = hours === 24 ? "24 h" : `${Math.round(hours / 24)} d`;
  $("#loop-chart-hint").textContent =
    `${total.toLocaleString("de-DE")} Punkte · Fenster ${winLabel}${
      hours > 24 ? " (24 h leer → Fenster automatisch erweitert)" : ""
    } · ${Math.round(fetchMs)} ms · Zoom: Mausrad / Ziehen · ` +
    "WinSol-Logging: letzte Messung kann zurückliegen (Kanal leer = keine Daten im Zeitraum)";
}

function destroyChart() {
  if (S.chart) {
    S.chart.destroy();
    S.chart = null;
    lastChartKey = null;
  }
}

function shortTs(ts) {
  return ts.slice(5, 16);
}

// ---------------------------------------------------------------------------
// Refresh-Hook (aus app.js): State/Health -> Loop-Modell + Views
// ---------------------------------------------------------------------------
/**
 * @param {object|null} state erfolgreiches /api/v1/state (null bei Fehler)
 * @param {object} health /api/v1/health-Block
 * @param {string} token aktuelles Read-Token
 * @param {function} on401 Token-Callback
 */
export function loopsOnState(state, health, token, on401) {
  S.token = token;
  S.on401 = on401;

  // Demo-Mode (ui-architecture.md §4): Gateway/State nicht erreichbar (null)
  // ODER CMI-Reachability=false (health.stale / status down) -> synthetische
  // plausible Werte + Badge "Demo-Daten" (wie S4-1-Dashboard-Status-Badging).
  const cmiDown = !!(
    health &&
    (health.stale === true ||
      health.status === "down" ||
      (health.cmi && health.cmi.stale === true))
  );
  if (state && !cmiDown) {
    S.lastState = state;
    S.model = mapState(state);
    S.demo = false;
  } else {
    S.model = mapState(withDemoJsonFields(demoState(Date.now())), { demo: true });
    S.demo = true;
  }

  applyModel();

  // Chart-Refresh: nur im Live-Betrieb (nicht in Demo, nicht ohne Token)
  // und wenn die View bereits gebaut ist (initLoopViews -> renderLoopFrame).
  if (!S.demo && S.token && S.active !== GESAMT && $("#loop-canvas")) {
    renderLoopChart();
  }
}

/** Erste Initialisierung (nach initPills + erstem State-Call). */
export async function initLoopViews(token, on401) {
  S.token = token;
  S.on401 = on401;
  try {
    await ensureSymbols();
  } catch (e) {
    console.error("P&ID-Symbol-Decls fehlen:", e);
    const host = $("#loop-svg-host");
    host.replaceChildren(Object.assign(document.createElement("p"), {
      className: "chart-error",
      textContent: "P&ID-Symbol-Bibliothek nicht ladbar (/svg/pid-symbols.svg).",
    }));
    return;
  }
  renderLoopFrame();
  // State kann bereits vor dem Symbol-Fetch angekommen sein -> sofort anwenden
  applyModel();
  if (S.active !== GESAMT) renderLoopChart();
}
