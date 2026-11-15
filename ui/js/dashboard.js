// Dashboard (S4-1): CMI-Status, Betriebsmodus, kuratierte Kernwerte.
//
// Kuratierung laeuft gegen die LIVE-Labels von /api/v1/state
// (proximity-Labels + curated LABEL_MAP, s. app/cmi/labels.py).
// Fehlende Werte degradieren sauber (dash "--").

/** Dashboard-Slots: Keyword-Matching auf State-Field-Labels. */
const DASH_KEYS = [
  { key: "aussen", re: /außen|aussen/i },
  { key: "speicher-oben", re: /^speicher oben/i },
  { key: "speicher-mitte", re: /^speicher mitte/i },
  { key: "speicher-unten", re: /^speicher unten/i },
  { key: "warmwasser", re: /^warmwasser$/i },
  { key: "vl", re: /heizkreis (vorlauf|vl)|^vl$/i },
  { key: "keller", re: /keller (raum)?temp|^keller$/i },
  { key: "eg", re: /erdgeschoss (raum)?temp|^erdgeschoss$|^eg$|^raumtemperatur$/i },
  { key: "dg", re: /dachgeschoss (raum)?temp|^dachgeschoss$/i },
];

const MODE_RE = /betriebsmodus/i;

function $(sel, root = document) {
  return root.querySelector(sel);
}

/** "YYYY-MM-DD HH:MM" (lokale Zeit) aus Unix-Epoch. */
function fmtTs(epoch) {
  if (!epoch) return "–";
  const d = new Date(epoch * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/**
 * Alle kuratierten Kernwerte aus einem State-Dokument ermitteln.
 * @param {object} doc /api/v1/state-Dokument
 * @returns {object} {modus, values: {key: {value, unit}}, found: Set<key>}
 */
export function extractDashboard(doc) {
  const values = {};
  const found = new Set();

  for (const field of doc.fields || []) {
    if (field.value == null) continue;
    const label = field.label || "";
    for (const slot of DASH_KEYS) {
      if (!found.has(slot.key) && slot.re.test(label)) {
        values[slot.key] = { value: field.value, unit: field.unit || "" };
        found.add(slot.key);
      }
    }
  }

  // Betriebsmodus: mode_select-Position mit Label "Betriebsmodus" (S{page}.{pos}).
  let modus = null;
  for (const key of Object.keys(doc.schematics || {})) {
    const page = doc.schematics[key];
    if (!page || !Array.isArray(page.positions)) continue;
    for (const pos of page.positions) {
      if (pos.pos_type === "mode_select" && pos.label && MODE_RE.test(pos.label)) {
        modus = pos.text || null;
        break;
      }
    }
    if (modus) break;
  }

  return { modus, values, found };
}

/** Dashboard-HTML gegen einen State-Dokument + Health-Block aktualisieren. */
export function renderDashboard(doc, health, winsolStatus) {
  const { modus, values } = extractDashboard(doc);

  // -- Kernwerte -------------------------------------------------------
  for (const slot of DASH_KEYS) {
    const el = $(`[data-v="${slot.key}"]`);
    const unitEl = $(`[data-u="${slot.key}"]`);
    if (!el) continue;
    const v = values[slot.key];
    if (v) {
      el.textContent = Number(v.value).toLocaleString("de-DE", {
        maximumFractionDigits: 1,
      });
      if (unitEl) unitEl.textContent = v.unit || "";
    } else {
      el.textContent = "–";
      if (unitEl) unitEl.textContent = "";
    }
  }

  // -- Betriebsmodus ---------------------------------------------------
  const modeEl = $("#dash-modus");
  if (modeEl) modeEl.textContent = modus || "–";

  // -- Status-Karte ----------------------------------------------------
  const detail = $("#status-detail");
  if (detail) {
    const cmi = health.cmi || {};
    const schematic = cmi.schematic || {};
    const jsonApi = cmi.json_api || {};
    const reachable = cmi.stale === false || schematic.ok || jsonApi.ok;
    const rows = [];
    rows.push(
      `<div class="row"><span class="dot ${reachable ? "" : "down"}"></span>
       <span>CMI ${reachable ? "erreichbar" : "nicht erreichbar"} ·
       <code>${esc(cmi.url || "–")}</code></span></div>`
    );
    rows.push(
      `<div class="row"><span class="dot ${schematic.ok ? "" : "down"}"></span>
       <span>Schema-Route ${schematic.ok ? "OK" : "Fehler"} ·
       JSON-API ${jsonApi.ok ? "OK" : "Fehler"}</span></div>`
    );
    const winsol = winsolStatus || {};
    const wOk = winsol.available === true;
    rows.push(
      `<div class="row"><span class="dot ${wOk ? "" : "down"}"></span>
       <span>WinSol-Store ${wOk ? "verfügbar" : "nicht verfügbar"}` +
        (wOk && Array.isArray(winsol.months)
          ? ` (${winsol.months[0]} … ${winsol.months[1]})`
          : "") +
        ` · <a href="/api/v1/health" target="_blank" rel="noopener">/api/v1/health</a></span></div>`
    );
    if (cmi.stale) {
      rows.push('<span class="stale-badge">stale – CMI liefert kein neues Daten</span>');
    }
    detail.innerHTML = rows.join("");
  }

  // -- Topbar-Chips ----------------------------------------------------
  setChip("cmi", health, (health) => {
    if (health.cmi && health.cmi.stale === false) return ["chip-ok", "CMI ok"];
    if (health.status === "down") return ["chip-err", "CMI down"];
    return ["chip-warn", "CMI stale"];
  });
  setChip("winsol", winsolStatus || {}, (w) =>
    w.available
      ? ["chip-ok", "WinSol ok"]
      : ["chip-warn", "WinSol fehlt"]
  );

  // -- letzte Aktualisierung -------------------------------------------
  const lu = $("#last-updated");
  if (lu) lu.textContent = `Stand ${fmtTs(doc.timestamp)}`;
}

function setChip(name, payload, fn) {
  const el = $(`[data-chip="${name}"]`);
  if (!el) return;
  const [cls, text] = fn(payload);
  el.className = `chip ${cls}`;
  el.textContent = text;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));
}
