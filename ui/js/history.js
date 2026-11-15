// Historie-Charts (S4-3): Chart.js + chartjs-plugin-zoom (vendored).
//
// Ein /api/v1/history-Call pro Chart-Render (limit 5000, multi-Kanal via
// ?channel=2,5,8). Zeitachse: ts ist naive lokale Wandzeit
// ("YYYY-MM-DD HH:MM:SS") -> manuelles ms-Parsing, kein Date-Adapter.

import { apiGet } from "./api.js";
import { WINSOL_LABELS } from "./winsol-labels.js";
// Q-129 (UI-HistFix): A/D-aware Serie-Labels aus der Checkbox-Auswahl
// (Response-Key "device:channel" traegt KEINE ad-Dimension mehr).
import { channelLabel, seriesLabelForSelection } from "./history-labels.js";

const HIST_LIMIT = 5000;
const MAX_SERIES = 3;

// Auto-Intervall-Recommendation pro Zeitraum (Bucket-Größe in Sekunden).
const RANGE_PRESETS = [
  { hours: 24, interval: 900 },
  { hours: 168, interval: 3600 },
  { hours: 720, interval: 21600 },
  { hours: 8760, interval: 86400 },
];

// Default-View: winsol:1 (RAS-Raume), Kanale 2/5/8 (Temp Keller/EG/DG),
// 24 h, nur analog (ad=A). Hinweis: die drei Raumtemperaturen liegen in
// Setup.xml unter a2/a5/a8 (= Kanale 2/5/8), nicht unter a1/a2/a3.
// ad=A ist Pflicht, weil /api/v1/history ohne ad-Filter den digitalen
// Zwilling dev1_digital_NN (0/1) in dieselbe Serie unionen wuerde.
const DEFAULT_DEVICE = "winsol:1";
const DEFAULT_CHANNELS = {
  "winsol:1": [
    { channel: 2, ad: "A" },
    { channel: 5, ad: "A" },
    { channel: 8, ad: "A" },
  ],
};

function $(sel, root = document) {
  return root.querySelector(sel);
}

/** "YYYY-MM-DD HH:MM:SS" (lokal, naive Wandzeit) -> ms. */
function tsToMs(ts) {
  // ES: "YYYY-MM-DDTHH:MM:SS" ohne Zeitzone = lokale Zeit.
  return new Date(ts.replace(" ", "T")).getTime();
}

/** "YYYY-MM-DD" (lokal) aus ms. */
function msToDay(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function axisFmt(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  const span = Math.abs(window.__histSpanMs || 0);
  if (span >= 3600 * 24 * 3) {
    return `${p(d.getDate())}.${p(d.getMonth() + 1)}.${String(d.getFullYear()).slice(2)}`;
  }
  return `${p(d.getDate())}.${p(d.getMonth() + 1)} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function shortTs(ts) {
  return ts.slice(5, 16); // "MM-DD HH:MM"
}

/**
 * Kanal-Liste pro Gerat bauen.
 *
 * WinSol-Geräte bieten pro Kanalnummer bis zu ZWEI Einträge an --
 * analog "ch N <label> (A)" und digital "ch N <label> (D)". Der Grund:
 * /api/v1/history?ad=A|D liest nur EINEN A/D-Typ pro Request, und ohne
 * ad-Filter uniont die API beide Typen in eine Serie (der digitale Zwilling
 * 0/1 wuerde die Temperaturkurve zerreißen). json:/schematic-Geräte haben
 * nur EINEN Eintrag pro Kanal (keine ad-Semantik dort relevant).
 *
 * @param {string} device Gerat-Id
 * @param {object|null} doc State-Dokument (fuer json:/schematic-Labels)
 * @returns {Array<{channel:number,label:string,ad?:string}>}
 */
export function channelsForDevice(device, doc) {
  // WinSol: kuratierte Labels aus Setup.xml (winsol-labels.js).
  // S4N-LogParquet: die cmi-log-Historie ist hiermit merged (ein
  // winsol:<dev>-Device deckt Archiv + CMI-internen Logger); eigene
  // cmi-log:<dev>-Kanal-Liste gibt es nicht mehr.
  if (device.startsWith("winsol:")) {
    const labels = WINSOL_LABELS[device] || {};
    const out = [];
    for (const [n, lbl] of Object.entries(labels.analog || {})) {
      out.push({ channel: Number(n), label: lbl, ad: "A" });
    }
    for (const [n, lbl] of Object.entries(labels.digital || {})) {
      out.push({ channel: Number(n), label: lbl, ad: "D" });
    }
    // ch-Nummer aufsteigend, dann A vor D (gleiche Nummer).
    return out.sort((a, b) => a.channel - b.channel || a.ad.localeCompare(b.ad));
  }

  // json:<node> / schematic: Kanale aus den Live-State-Fields (Quelle:
  // /api/v1/state). schematic-Kanale sind page*100+pos (deterministisch).
  const out = [];
  const seen = new Set();
  for (const field of (doc && doc.fields) || []) {
    let channel = null;
    let sourceDevice = null;
    if (field.source === "json_api") {
      const m = /node=(\d+);section=[^;]+;channel=(\d+)/.exec(
        field.reference || ""
      );
      if (m) {
        sourceDevice = `json:${m[1]}`;
        channel = Number(m[2]);
      }
    } else if (field.source === "schematic") {
      const m = /page=(\d+);pos=(\d+)/.exec(field.reference || "");
      if (m) {
        sourceDevice = "schematic";
        channel = Number(m[1]) * 100 + Number(m[2]);
      }
    }
    if (sourceDevice !== device || channel == null || seen.has(channel)) {
      continue;
    }
    seen.add(channel);
    out.push({ channel, label: field.label || "" });
    if (out.length >= 24) break; // Liste begrenzen
  }
  return out.sort((a, b) => a.channel - b.channel);
}

/**
 * Standard-Kanäle pro Gerät (Liste von {channel, ad}).
 * winsol:1 -> die drei RAS-Raumtemperaturen, analog (A).
 */
function defaultChannels(device) {
  const defs = DEFAULT_CHANNELS[device] || [];
  return defs.map((c) => ({ ...c }));
}

/** Checkbox-Value: "A:2" / "D:2" (winsol) bzw. "209" (json/schematic). */
function cbValue(item) {
  return item.ad ? `${item.ad}:${item.channel}` : String(item.channel);
}

/** Checkbox-Value zurueck in {channel, ad}. */
function parseCbValue(v) {
  const i = v.indexOf(":");
  if (i < 0) return { channel: Number(v), ad: undefined };
  return { channel: Number(v.slice(i + 1)), ad: v.slice(0, i) };
}

/** Kanal-Auswahl-UI fuer das ausgewaehlte Gerat aufbauen. */
function renderChannelList(device, doc, selected) {
  const list = $("#hist-channels");
  list.replaceChildren();
  const channels = channelsForDevice(device, doc);
  if (!channels.length) {
    list.append(
      Object.assign(document.createElement("span"), {
        className: "muted small",
        textContent:
          device.startsWith("winsol:")
            ? "Keine Kanal-Labels verfuegbar."
            : "Keine Kanale im Live-State (noch nicht geladen?)",
      })
    );
    return;
  }
  for (const ch of channels) {
    const label = document.createElement("label");
    label.className = "channel-item";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = cbValue(ch);
    cb.checked = selected.some(
      (s) => s.channel === ch.channel && s.ad === ch.ad
    );
    label.append(cb);
    const num = document.createElement("span");
    num.className = "ch-num";
    num.textContent = `ch ${ch.channel}${ch.ad ? ` (${ch.ad})` : ""}`;
    label.append(num);
    if (ch.label) {
      const l = document.createElement("span");
      l.textContent = ch.label;
      label.append(l);
    }
    list.appendChild(label);
  }
  // max. 3 Serien pro Chart (mehr ist beim 5000-Punkte-Limit unlesbar):
  // ueberfluessige Anhaeksel sofort wieder enthaekeln. Einmalig binden --
  // #hist-channels ist ein persistentes Element und wird bei jedem
  // 30-s-Refresh neu gefuellt, ein neues addEventListener pro Runde
  // wuerde die Listener sammeln.
  if (!list.dataset.maxEnforced) {
    list.dataset.maxEnforced = "1";
    list.addEventListener("change", () => {
      const checked = [...list.querySelectorAll("input:checked")];
      for (const cb of checked.slice(MAX_SERIES)) cb.checked = false;
    });
  }
}

/** Aktuell gecheckte Kanael-Items als [{channel, ad}] (sortiert). */
function selectedChannelSet() {
  return [...document.querySelectorAll("#hist-channels input:checked")]
    .map((i) => parseCbValue(i.value))
    .sort((a, b) => a.channel - b.channel || String(a.ad).localeCompare(String(b.ad)));
}

/** "YYYY-MM-DD HH:MM:SS" (lokal) aus ms. */
function msToFull(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/**
 * Aktuelle Start/Ende (vollstaendige Zeitstempel, locale Wandzeit).
 * Presets: [now - N h, now]; Custom: [Datum 00:00:00, Datum 23:59:59].
 */
function rangeBounds() {
  const sel = $("#hist-range").value;
  const now = Date.now();
  if (sel === "custom") {
    const s = $("#hist-start").value;
    const e = $("#hist-end").value;
    return {
      start: (s || msToDay(now - 86400000)) + " 00:00:00",
      end: (e || msToDay(now)) + " 23:59:59",
    };
  }
  const hours = Number(sel);
  return {
    start: msToFull(now - hours * 3600 * 1000),
    end: msToFull(now),
  };
}

/**
 * Chart rendern (ein /api/v1/history-Call).
 * @param {string} token Read-Token
 * @param {object} lastState letztes /api/v1/state (fuer Kanal-Labels)
 * @param {object} chartRef {chart: Chart|null}
 */
export async function runHistoryQuery(token, lastState, chartRef, on401) {
  const device = $("#hist-device").value;
  const items = selectedChannelSet();
  const agg = $("#hist-agg").value;
  const interval = Number($("#hist-interval").value) || 900;
  const { start, end } = rangeBounds();

  const loading = $("#chart-loading");
  const errEl = $("#chart-error");
  errEl.classList.add("hidden");
  loading.classList.remove("hidden");

  const q = new URLSearchParams();
  q.set("device", device);
  if (items.length) q.set("channel", items.map((i) => i.channel).join(","));
  // ad nur bei Geraeten mit A/D-Trennung (Winsol inkl. gemerged cmi-log,
  // S4N-LogParquet). Gemischte Auswahl (soll durch die max-3-Begrenzung
  // nicht passieren) laesst den Filter weg -> Union wie im Basiskontrakt.
  const ads = items.map((i) => i.ad).filter(Boolean);
  const adDevice = device.startsWith("winsol:");
  if (ads.length && new Set(ads).size === 1 && adDevice) {
    q.set("ad", ads[0]);
  }
  q.set("start", start);
  q.set("end", end);
  q.set("agg", agg);
  q.set("interval", String(interval));
  q.set("limit", String(HIST_LIMIT));
  const url = `/api/v1/history?${q.toString()}`;

  const t0 = performance.now();
  let data;
  try {
    data = await apiGet(url, token);
  } catch (e) {
    loading.classList.add("hidden");
    if (e.status === 401) {
      on401();
      return;
    }
    const msg =
      e.status === 400
        ? `Kanal für Gerät nicht verfügbar (${e.detail || "400"})`
        : e.status === 500
          ? `Store-Fehler (500): ${e.detail || "Historie nicht lesbar"}`
          : `Fehler HTTP ${e.status}`;
    errEl.textContent = msg;
    errEl.classList.remove("hidden");
    return;
  }
  loading.classList.add("hidden");

  // Response-Shapes: {device,channel,points,...} | {"device:channel":{...},_meta} | {}
  let seriesMap = {};
  if (Array.isArray(data.points)) {
    seriesMap[`${data.device}:${data.channel}`] = data;
  } else {
    for (const [k, v] of Object.entries(data)) {
      if (k !== "_meta" && v && Array.isArray(v.points)) seriesMap[k] = v;
    }
  }

  // Span fuer die Achsenformatierung (Preset-Standardspannweite)
  const sel = $("#hist-range").value;
  window.__histSpanMs =
    sel === "custom" ? 3 * 86400000 : Number(sel) * 3600000;

  const datasets = [];
  for (const [key, s] of Object.entries(seriesMap)) {
    // Q-129: Label aus der AUSWAHL loesen (Response-Key nur fuer die
    // Punkte-Zuordnung, A/D-Information aus items erhalten bleibt).
    // Restserie ohne Match in items (soll mit channel-Filter nicht
    // vorkommen; ad-None-Union liefert pro Kanal max. 1 Serie) ->
    // channelLabel-Fallback wie vor Q-129.
    const chStr = key.split(":").pop();
    const matched = items.find((i) => i.channel === Number(chStr));
    const label = matched
      ? seriesLabelForSelection(device, matched, items, lastState)
      : channelLabel(device, key, lastState);
    // Dedupe pro ts (nur relevant fuer ad=None-Union; mit dem ad-Filter
    // liefert jede Serie genau einen Wert pro ts). Letzter Wert gewinnt.
    const byTs = new Map();
    for (const p of s.points) byTs.set(p.ts, p);
    datasets.push({
      label,
      data: [...byTs.values()].map((p) => ({
        x: tsToMs(p.ts),
        y: p.value,
        raw: p.ts,
      })),
      borderColor: nextColor(datasets.length),
      backgroundColor: nextColor(datasets.length, 0.12),
      borderWidth: 1.6,
      pointRadius: 0,
      pointHoverRadius: 4,
      tension: 0.15,
    });
  }

  if (!datasets.length) {
    errEl.textContent = "Keine Daten für diese Auswahl (Kanal/Zeitraum).";
    errEl.classList.remove("hidden");
    return;
  }

  const msMin = tsToMs(start);
  const msMax = tsToMs(end) + (86400000 * 0.5);
  destroyChart(chartRef);
  const canvas = $("#history-canvas");
  const chart = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 300 },
      interaction: { mode: "nearest", axis: "x", intersect: false },
      plugins: {
        legend: {
          labels: { color: "#e6edf7", font: { size: 12 } },
        },
        tooltip: {
          callbacks: {
            title: (items) =>
              items.length ? `Stand ${shortTs(items[0].raw.raw || "")}` : "",
            label: (item) =>
              ` ${item.dataset.label}: ${item.parsed.y} `,
          },
        },
        zoom: {
          pan: { enabled: true, mode: "x" },
          zoom: {
            wheel: { enabled: true },
            pinch: { enabled: true },
            drag: {
              enabled: true,
              backgroundColor: "rgba(77, 163, 255, 0.18)",
              borderColor: "#4da3ff",
              borderWidth: 1,
            },
          },
          limits: { x: { min: msMin - 3600000, max: msMax } },
        },
      },
      scales: {
        x: {
          type: "linear",
          min: msMin,
          max: msMax,
          ticks: {
            color: "#8fa0bd",
            callback: (v) => axisFmt(v),
            maxTicksLimit: 8,
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
  chartRef.chart = chart;

  const hint = $("#hist-hint");
  const total = datasets.reduce(
    (n, d) => n + d.data.length,
    0
  );
  hint.textContent = `${total.toLocaleString("de-DE")} Punkte in ${Math.round(
    performance.now() - t0
  )} ms · WinSol: 10-Sekunden-Abtastung (2020–2026) · Zoom: Ziehen oder Mausrad · Reset über Button`;
}

function destroyChart(chartRef) {
  if (chartRef.chart) {
    chartRef.chart.destroy();
    chartRef.chart = null;
  }
}

function resetZoom(chartRef) {
  if (chartRef.chart) chartRef.chart.resetZoom();
}

function nextColor(i, alpha = 1) {
  const palette = [
    [77, 163, 255],
    [52, 211, 153],
    [251, 191, 36],
    [167, 139, 250],
    [248, 113, 113],
  ];
  const [r, g, b] = palette[i % palette.length];
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export {
  DEFAULT_DEVICE,
  defaultChannels,
  destroyChart,
  resetZoom,
  renderChannelList,
  selectedChannelSet,
  RANGE_PRESETS,
};
