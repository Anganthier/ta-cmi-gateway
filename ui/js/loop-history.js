// Loop-History (S4N-2): 24h-Serien je Loop gegen /api/v1/history.
//
// Dünne Schale über dem History-Endpunkt (ui-architecture.md §4): ein
// Loop-View ruft die 3–5 relevanten Kanäle ab (LOOPS[*].series aus
// state-map.js), getrennt je Gerät (device) und A/D-Typ (der Endpunkt
// nimmt pro Request genau einen ad-Filter). Antwort -> Chart.js-
// Datasets (ms-Xachse, analoge Kurven / digitale Sprunglinien).

import { apiGet } from "./api.js";

const HIST_LIMIT = 5000;
const LOOP_HOURS = 24;
const LOOP_INTERVAL_SEC = 900; // 15-min-Buckets für 24 h (≈ 96 Punkte/Serie, S4-3-Preset)

/** "YYYY-MM-DD HH:MM:SS" (lokal, naive Wandzeit) -> ms. */
export function tsToMs(ts) {
  return new Date(ts.replace(" ", "T")).getTime();
}

/** ms -> "YYYY-MM-DD HH:MM:SS" (lokal, naive Wandzeit). */
export function msToFull(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/**
 * Alle Serien eines Loops abfragen (ein API-Call je device + ad-Gruppe).
 *
 * @param {string} token Read-Token
 * @param {Array<{device:string,channel:number,ad?:string,label:string,color:string}>} specs
 * @param {object} [opts] {hours=24, interval=900}
 * @returns {Promise<{datasets: Array, error: string|null, ms: number, hours: number}>}
 */
export async function fetchLoopSeries(token, specs, opts = {}) {
  const hours = opts.hours || LOOP_HOURS;
  const intervalSec = opts.interval || LOOP_INTERVAL_SEC;
  const end = Date.now();
  const start = end - hours * 3600 * 1000;

  // specs nach device + ad gruppieren (ad nur für winsol:* relevant)
  const groups = new Map();
  for (const s of specs) {
    const ad = s.device.startsWith("winsol:") ? s.ad || "A" : undefined;
    const key = `${s.device}|${ad ?? "-"}`;
    if (!groups.has(key)) groups.set(key, { device: s.device, ad, specs: [] });
    groups.get(key).specs.push(s);
  }

  const out = [];
  let error = null;
  const t0 = performance.now();

  for (const { device, ad, specs: groupSpecs } of groups.values()) {
    const q = new URLSearchParams();
    q.set("device", device);
    q.set("channel", groupSpecs.map((s) => s.channel).join(","));
    if (ad) q.set("ad", ad);
    q.set("start", msToFull(start));
    q.set("end", msToFull(end));
    q.set("agg", "last");
    q.set("interval", String(intervalSec));
    q.set("limit", String(HIST_LIMIT));
    const url = `/api/v1/history?${q.toString()}`;

    let data;
    try {
      data = await apiGet(url, token);
    } catch (e) {
      if (e.status === 401) throw e; // Aufrufer zeigt Token-Overlay
      error = e.status === 400
        ? `Kanal für ${device} nicht verfügbar (${e.detail || "400"})`
        : e.status === 500
          ? `Store-Fehler bei ${device}: ${e.detail || "Historie nicht lesbar"}`
          : `Historie ${device}: HTTP ${e.status}`;
      continue;
    }

    // Response-Shape: {device,channel,points} (1 Serie) | {"dev:ch":{...},_meta} | {}
    const seriesMap = {};
    if (Array.isArray(data.points)) {
      seriesMap[`${data.device}:${data.channel}`] = data;
    } else {
      for (const [k, v] of Object.entries(data)) {
        if (k !== "_meta" && v && Array.isArray(v.points)) seriesMap[k] = v;
      }
    }

    for (const spec of groupSpecs) {
      const key = `${spec.device}:${spec.channel}`;
      const s = seriesMap[key];
      const pts = (s && s.points) || [];
      // Dedupe pro ts (nur relevant ohne ad-Filter): letzter Wert gewinnt.
      const byTs = new Map();
      for (const p of pts) byTs.set(p.ts, p);
      out.push({
        key,
        label: spec.label,
        color: spec.color,
        digital: !!spec.ad && spec.ad === "D",
        data: [...byTs.values()].map((p) => ({ x: tsToMs(p.ts), y: p.value, raw: p.ts })),
      });
    }
  }

  return {
    datasets: out,
    error,
    startMs: start,
    endMs: end,
    ms: performance.now() - t0,
    hours,
  };
}
