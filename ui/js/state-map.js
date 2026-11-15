// State-Map (S4N-2): /api/v1/state -> Loop-View-Modell.
//
// ZWEI Ebenen in einer Datei (S4N-2 Teil 1 + Teil 2):
//
//  TEIL 1 — Daten-Schicht (dieses Ticket):
//   * fetchState(token)  = apiGet('/api/v1/state') mit {ok,data,demo,reason}
//                          (demo bei 5xx/401/Unreachable, analog dashboard.js)
//   * buildLoopStates(stateDoc) = {A..E: {values, pumps, ras?, active}, meta}
//                          — Werte + O-Status + Aktiv-Logik (konservativ R-X4)
//   * demoState()        = synthetischer State-Doc (plausible Werte, stale=true)
//
//  TEIL 2 — Rendering-Vertrag (WIP loops.js/loop-svg.js, unverändert erhalten):
//   * LOOPS/LOOP_ORDER/DEFAULT_LOOP/READOUT_META (24h-Serien + Setz-Adressen)
//   * mapState(doc,{demo}) = Schematic-primär/JSON-Fallback -> readouts,
//                          switches, flow-Tokens (data-flow-Attribute)
//   * withDemoJsonFields(doc, nowMs) = JSON-only-Demo-Felder (a16-SS, O3/O4)
//
// Basis: docs/hydraulic-loops-rules.md (Kanon R-A1…R-X8), ui-architecture.md §4,
// app/state.py (StateDocument: fields[] {id,label,value,unit,raw,source,
// reference,address,ras_state,timestamp} + health {schematic_ok,api_ok,stale}).
// device-Zuordnung: dev0/node 1 = Heizung (UVR1611), dev1 = RAS-Raeume,
// dev2/node 32 = Solar (UVR610) — s. winsol-labels.js.
//
// WICHTIG (Feld-Befund aus dem Live-State 2026-10-29, fuer S4N-2-Teil 2):
//   * O-Pumpen/Ventile sind im State als SCHEMATIC-FELD mit address-Attribut
//     (CAN-L1-Setzadresse) + raw-Text lesbar ("AUTO/AUS", "HAND/EIN", ...):
//       O1 10120A10180 (p1:15) · O5 10520A10180 (p1:23) · O6 10220A10180 (p1:18)
//       O2 10620A10180 (p1:17/p2:15) · O7 10720A10180 (p1:21) ·
//       O8 10820A10180 (p1:11) · O9 10920A10180 (p1:25) ·
//       SV-Modus 10040912091 (p4:14, raw "Auto" = Betriebsart, NICHT Zustand)
//     Lookup ueber ADDRESS (primär) statt Label-Text: labels sind duplikat
//     ("Ladepumpe WW" 3x, "Zirkulation" 2x auf den Schematic-Seiten).
//   * JSON-Outputs (node 1, section Outputs ch1..13) = SELBEN Signale als 0/1
//     (O1=ch1, O2=ch2, O5=ch5, O6=ch6, O7=ch7, O8=ch8, O9=ch9) — Fallback.
//     ACHTUNG: Inputs und Outputs teilen sich die Kanal-Nummern (beide 1..16),
//     der Index MUSS section-aware sein ("node;section;channel").
//   * SV-Pumpen-Zustand: node 32 Outputs ch1 (0/1) + Schematic p4:13 ("Solarpumpe",
//     raw "ON"/"OFF"). p4:14 ist der Modus-Select ("Auto"), kein Zustand.
//   * Mischer O3/O4: node 1 Outputs ch3/ch4 (0/1), C1-intern (R-E2).
//   * Zirk-Stromungsschalter: node 1 INPUTS ch16 (binär, R-D3 — a16, kein Output).
//   * RAS (dev1 a2/a5/a8): existiert im State NICHT unter node 1 Inputs ch2/5/8
//     (das sind P-Mitte/Öl-VL/RL)! Raum-Temperaturen = Schematic p3:10/11/12
//     ("Keller/Raum/Dachgeschoss Raumtemperatur"). RAS-Zustand je Zone:
//     node 32 "Logging Analog" ch4/6/8 ("Keller/EG/DG RAS 1") mit ras_state
//     (Zeit/Auto | Standard | Setback | Standby/Frostschutz; R-X4 konservativ:
//     aktiv = NICHT Standby/Frostschutz). dev2-Kollektor (a16): node 32
//     "Logging Analog" ch16 ("T.Kollektor 1").

import { apiGet } from "./api.js";

// Serie-Labels: kuratierte WinSol-Labels aus Setup.xml (winsol-labels.js),
// ui-architecture.md §3.1 ("Label je Serie aus winsol-labels.js").
import { WINSOL_LABELS } from "./winsol-labels.js";

function winLabel(device, channel, ad) {
  const L = WINSOL_LABELS[device] || {};
  const map = ad === "D" ? L.digital || {} : L.analog || {};
  return map[channel] != null ? String(map[channel]) : `Kanal ${channel}`;
}

// ---------------------------------------------------------------------------
// Loop-Definitionen (A–E) + Gesamt  (TEIL 2: 24h-Serien + Setz-Adressen)
// ---------------------------------------------------------------------------
export const LOOPS = {
  A: {
    letter: "A",
    name: "Holz",
    title: "Loop A · Holzofen",
    desc: "Eigener Kreis: H1 ↔ Puffer P (VL ganz oben / RL ganz unten), Pumpe O1, Abgas a10.",
    series: [
      { device: "winsol:0", channel: 9, ad: "A", label: `a9 · ${winLabel("winsol:0", 9, "A")}`, color: "#f59e0b" },
      { device: "winsol:0", channel: 10, ad: "A", label: `a10 · ${winLabel("winsol:0", 10, "A")}`, color: "#94a3b8" },
    ],
    readouts: ["a9", "a10", "pUnten"],
    statuses: ["o1", "abgas"],
    settable: [{ id: "O1", label: "Pumpe Holzofen", addr: "10120A10180", key: "o1" }],
  },
  B: {
    letter: "B",
    name: "Öl",
    title: "Loop B · Ölheizung H2",
    desc: "H2 zwischen Heizkreisen und Puffer: HEIZ-Modus → Mischer (E), WW-Modus → O6 → P ganz oben.",
    series: [
      { device: "winsol:0", channel: 5, ad: "A", label: `a5 · ${winLabel("winsol:0", 5, "A")}`, color: "#fb923c" },
      { device: "winsol:0", channel: 2, ad: "A", label: `a2 · ${winLabel("winsol:0", 2, "A")}`, color: "#4da3ff" },
    ],
    readouts: ["a5", "pMitte", "aussen"],
    statuses: ["o5", "o6"],
    settable: [
      { id: "O5", label: "Ölbrenner (RISK)", addr: "10520A10180", key: "o5" },
      { id: "O6", label: "Ladepumpe WW vom Öl", addr: "10220A10180", key: "o6" },
    ],
  },
  C: {
    letter: "C",
    name: "Solar",
    title: "Loop C · Solar",
    desc: "Kollektor H3 → Solarpumpe SV → Spirale in P-Unten. Kein separates Solar-Behältnis.",
    series: [
      { device: "winsol:2", channel: 16, ad: "A", label: `a16 · ${winLabel("winsol:2", 16, "A")}`, color: "#fbbf24" },
      { device: "winsol:0", channel: 3, ad: "A", label: `a3 · ${winLabel("winsol:0", 3, "A")}`, color: "#4da3ff" },
      { device: "winsol:2", channel: 1, ad: "D", label: `d1 · ${winLabel("winsol:2", 1, "D")}`, color: "#a78bfa" },
    ],
    readouts: ["kollektor", "pUnten"],
    statuses: ["sv", "svMode"],
    settable: [{ id: "SV", label: "Solarpumpe-Modus", addr: "10040912091 (x2, Node 32)", key: "svMode" }],
  },
  D: {
    letter: "D",
    name: "WW",
    title: "Loop D · Warmwasser",
    desc: "WW aus P-Oben über Waermetauscher, Zirkulation über O7 fest 50 °C, Überwachung über Strömungsschalter a16.",
    series: [
      { device: "winsol:0", channel: 4, ad: "A", label: `a4 · ${winLabel("winsol:0", 4, "A")}`, color: "#38bdf8" },
      { device: "winsol:0", channel: 16, ad: "A", label: `a16 · ${winLabel("winsol:0", 16, "A")}`, color: "#a78bfa" },
    ],
    readouts: ["a4", "pOben"],
    statuses: ["o2", "o7", "a16ss"],
    settable: [
      { id: "O2", label: "Ladepumpe WW", addr: "10620A10180", key: "o2" },
      { id: "O7", label: "Zirkulationspumpe", addr: "10720A10180", key: "o7" },
    ],
  },
  E: {
    letter: "E",
    name: "Heizung",
    title: "Loop E · Heizung",
    desc: "P-Mitte → H2 (O5) → Mischer (O3/O4) → O8 → VL (a6) → R1 Keller / R2 EG / R3 DG (Einrohr) → RM1/2/3 → RL (a7) → O9 (→ H2 oder P „unten“). H2 mit 3 Anschlüssen: P-Mitte-Zulauf (oben), O9-(a) (links), VL→Mischer (rechts).",
    series: [
      { device: "winsol:0", channel: 6, ad: "A", label: `a6 · ${winLabel("winsol:0", 6, "A")}`, color: "#f59e0b" },
      { device: "winsol:0", channel: 7, ad: "A", label: `a7 · ${winLabel("winsol:0", 7, "A")}`, color: "#4da3ff" },
      { device: "winsol:1", channel: 2, ad: "A", label: `RAS · ${winLabel("winsol:1", 2, "A")}`, color: "#34d399" },
      { device: "winsol:1", channel: 5, ad: "A", label: `RAS · ${winLabel("winsol:1", 5, "A")}`, color: "#fb7185" },
      { device: "winsol:1", channel: 8, ad: "A", label: `RAS · ${winLabel("winsol:1", 8, "A")}`, color: "#a78bfa" },
    ],
    readouts: ["pMitte", "a5", "a6", "a7", "rasK", "rasEg", "rasDg", "rlK", "rlEg", "rlDg", "rmK", "rmEg", "rmDg"],
    statuses: ["o8", "o9", "mischer"],
    settable: [
      { id: "O8", label: "Pumpe Heizkreis", addr: "10820A10180", key: "o8" },
      { id: "O9", label: "Ventil Heiz-RL (Rücklauf-Entscheidung)", addr: "10920A10180", key: "o9" },
    ],
  },
};

export const LOOP_ORDER = ["A", "B", "C", "D", "E"];
export const DEFAULT_LOOP = "E"; // Freigabe Q-11
export const GESAMT = "G"; // statische Doku (S4N-3 verfeinert)

// Zentrale Messwert-Labels (Einheit + Kurzlabel für Chips/Readouts).
export const READOUT_META = {
  a1: { label: "P-Oben", unit: "°C" },
  pOben: { label: "P-Oben", unit: "°C" },
  pMitte: { label: "P-Mitte", unit: "°C" },
  pUnten: { label: "P-Unten", unit: "°C" },
  a4: { label: "WW", unit: "°C" },
  a5: { label: "Öl-VL", unit: "°C" },
  a6: { label: "VL", unit: "°C" },
  a7: { label: "RL", unit: "°C" },
  aussen: { label: "Außen", unit: "°C" },
  a9: { label: "Holzkessel a9", unit: "°C" },
  a10: { label: "Abgas a10", unit: "°C" },
  kollektor: { label: "Kollektor", unit: "°C" },
  // Raum-Ist-Temperatur (node32 „Temp"-Kanäle, KEIN RAS-Fühler-Perzipiert-Wert)
  rasK: { label: "Keller (Raum)", unit: "°C" },
  rasEg: { label: "EG (Raum)", unit: "°C" },
  rasDg: { label: "DG (Raum)", unit: "°C" },
  // Strang-Rücklauf-Temp pro Kreis (node32 „HzRkl Ventil 1" / Schematic p3:13-15)
  rlK: { label: "RL Keller", unit: "°C" },
  rlEg: { label: "RL EG", unit: "°C" },
  rlDg: { label: "RL DG", unit: "°C" },
  // Minderer-Stellung pro Kreis (node32 „HzRkl Ventil 2 Ergebnis", 0–10 V)
  rmK: { label: "RM Keller", unit: "V" },
  rmEg: { label: "RM EG", unit: "V" },
  rmDg: { label: "RM DG", unit: "V" },
};

// ---------------------------------------------------------------------------
// TEIL 1: Kanal- & Adress-Definitionen (Kanon: docs/hydraulic-loops-rules.md)
// ---------------------------------------------------------------------------

/** O-Pumpen/Ventile: CAN-L1-Setzadresse, Schematic-Fallback, JSON-Output-Fallback. */
const O_FIELD = {
  O1: { addr: "10120A10180", page: 1, pos: 15, out: 1 },
  O2: { addr: "10620A10180", page: 1, pos: 17, out: 2 },
  O5: { addr: "10520A10180", page: 1, pos: 23, out: 5 },
  O6: { addr: "10220A10180", page: 1, pos: 18, out: 6 },
  O7: { addr: "10720A10180", page: 1, pos: 21, out: 7 },
  O8: { addr: "10820A10180", page: 1, pos: 11, out: 8 },
  O9: { addr: "10920A10180", page: 1, pos: 25, out: 9 },
  SV: { addr: "10040912091", page: 4, pos: 14, out: 1, node: 32 },
};

/** Loop-Kanäle: [device, channel, ad] (ad nur zur Semantik, Auflösung unten). */
const LOOP_CHANNELS = {
  A: [
    ["dev0", 9, "A"], // a9 Holzkessel-Tank (Wasserjacket)
    ["dev0", 10, "A"], // a10 Abgas
  ],
  B: [
    ["dev0", 5, "A"], // a5 Ölkessel-VL
    ["dev0", 2, "A"], // a2 P-Mitte
  ],
  C: [
    ["dev2", 16, "A"], // a16 Kollektor (node 32 Logging Analog ch16)
    ["dev0", 3, "A"], // a3 P-Unten
    ["dev2", 1, "D"], // d1 Solarpumpe (binär, node 32 Outputs ch1)
  ],
  D: [
    ["dev0", 4, "A"], // a4 WW
    ["dev0", 16, "D"], // a16 Zirk-Stromungsschalter (binär, node 1 Inputs ch16)
  ],
  E: [
    ["dev0", 6, "A"], // a6 VL
    ["dev0", 7, "A"], // a7 RL
    ["dev1", 2, "A"], // RAS Keller (Schematic p3:10)
    ["dev1", 5, "A"], // RAS EG (Schematic p3:11)
    ["dev1", 8, "A"], // RAS DG (Schematic p3:12)
  ],
};

/** Aktive Antriebe je Loop (null-Status -> UI zeigt „?“). O9 = Ventil, O5 = Brenner. */
const LOOP_DRIVES = {
  A: ["O1"],
  B: ["O5", "O6"],
  C: ["SV"],
  D: ["O2", "O7"],
  E: ["O8"],
};

// RAS-Zonen: Raum-Temp (dev1) als Schematic-Position + node 32 Logging-Analog-
// Channel mit ras_state.
const RAS_ZONES = {
  keller: { dev: "dev1", ch: 2, sch: "3;10", la: 4 },
  eg: { dev: "dev1", ch: 5, sch: "3;11", la: 6 },
  dg: { dev: "dev1", ch: 8, sch: "3;12", la: 8 },
};

// ---------------------------------------------------------------------------
// TEIL 1: Indexe + Leser
// ---------------------------------------------------------------------------

/** JSON-Index "node;section;channel" -> Feld (section-aware: Inputs und
 *  Outputs teilen sich die Kanal-Nummern). */
function jsonIndex(doc) {
  const idx = new Map();
  for (const f of fieldsOf(doc)) {
    if (f.source !== "json_api" || !f.reference) continue;
    const m = /node=(\d+);section=([^;]+);channel=(\d+)/.exec(f.reference);
    if (m) idx.set(`${m[1]};${m[2]};${m[3]}`, f);
  }
  return idx;
}

/** Schematic-Index "page;pos" -> Feld. */
function schIndex(doc) {
  const idx = new Map();
  for (const f of fieldsOf(doc)) {
    if (f.source !== "schematic" || !f.reference) continue;
    const m = /page=(\d+);pos=(\d+)/.exec(f.reference);
    if (m) idx.set(`${m[1]};${m[2]}`, f);
  }
  return idx;
}

function fieldsOf(doc) {
  return (doc && Array.isArray(doc.fields) && doc.fields) || [];
}

/** Schematic-Raw-Text -> an/aus (konservativ: AUTO/AUS = aus). */
function textOn(text) {
  const t = String(text == null ? "" : text).toUpperCase();
  if (t.trim() === "") return false;
  if (/AUS|OFF/.test(t)) return false;
  return /EIN|AN\b|ON\b/.test(t);
}

/**
 * Auflösung eines Loop-Kanals (device:channel) aus dem State-Doc.
 * @returns {{value: (number|null), unit: string}}
 */
function resolveChannel(doc, idxJ, idxS, dev, ch, ad) {
  // RAS (dev1): im Live-State nur als Schematic-Feld p3:10/11/12 vorhanden.
  if (dev === "dev1") {
    const zone = Object.values(RAS_ZONES).find((z) => z.ch === ch);
    if (zone) {
      const f = idxS.get(zone.sch);
      return {
        value: f && f.value != null ? f.value : null,
        unit: f && f.unit ? f.unit : "°C",
      };
    }
  }
  // Kollektor (dev2 a16): node 32 Logging Analog ch16 ("T.Kollektor 1").
  if (dev === "dev2" && ch === 16) {
    const f = idxJ.get("32;Logging Analog;16");
    return {
      value: f && f.value != null ? f.value : null,
      unit: f && f.unit ? f.unit : "°C",
    };
  }
  // Zirk-Stromungsschalter (dev0 a16): node 1 INPUTS ch16 (binär).
  if (dev === "dev0" && ch === 16) {
    const f = idxJ.get("1;Inputs;16");
    return {
      value: f && f.value != null ? f.value : null,
      unit: f && f.unit ? f.unit : "Aus/EIN",
    };
  }
  const node = dev === "dev2" ? 32 : 1;
  const section = ad === "D" ? "Outputs" : "Inputs";
  const f = idxJ.get(`${node};${section};${ch}`);
  return {
    value: f && f.value != null ? f.value : null,
    unit: f && f.unit ? f.unit : ad === "D" ? "Aus/EIN" : "°C",
  };
}

/** O-Status: Schematic-Text (address-gelot) primär, JSON-Output-Fallback. */
function oStatus(doc, idxJ, idxS, key) {
  const spec = O_FIELD[key];
  if (!spec) return null;

  // SV: JSON-Output (node 32 Outputs ch1) ist der Pumpen-Zustand; p4:14 ist
  // der Modus-Select ("Auto") und taugt NICHT als an/aus.
  if (spec.node) {
    const jf = idxJ.get("32;Outputs;1");
    if (jf && jf.value != null) {
      return { key, on: jf.value === 1, text: jf.value === 1 ? "ON" : "OFF", source: "json" };
    }
    const f = idxS.get("4;13");
    if (f && typeof f.raw === "string" && f.raw.trim() !== "") {
      return { key, on: textOn(f.raw), text: f.raw, source: "schematic" };
    }
    return null;
  }

  let f = null;
  for (const g of fieldsOf(doc)) {
    if (g.source === "schematic" && g.address === spec.addr) {
      f = g;
      break;
    }
  }
  if (!f) f = idxS.get(`${spec.page};${spec.pos}`) || null;
  if (f && typeof f.raw === "string" && f.raw.trim() !== "") {
    return { key, on: textOn(f.raw), text: f.raw, source: "schematic" };
  }
  const jf = idxJ.get(`1;Outputs;${spec.out}`);
  if (jf && jf.value != null) {
    return { key, on: jf.value === 1, text: jf.value === 1 ? "EIN" : "AUS", source: "json" };
  }
  return null; // -> UI zeigt „?“
}

/** RAS-Zustand (E): Raum-Temp (dev1) + Zonen-Regler-Status (node 32). */
function rasStatus(doc, idxJ, idxS, zoneKey) {
  const z = RAS_ZONES[zoneKey];
  const v = resolveChannel(doc, idxJ, idxS, z.dev, z.ch, "A");
  const s = idxJ.get(`32;Logging Analog;${z.la}`);
  const rasState = s && s.ras_state != null ? String(s.ras_state) : null;
  // Konservativ (R-X4): aktiv = NICHT Standby/Frostschutz; unbekannt -> null.
  const active = rasState == null ? null : rasState !== "Standby/Frostschutz";
  return { value: v.value, unit: v.unit, rasState, active };
}

/** Modus (M, R-X4): Schematic-Feld mit CAN-L1-Adresse 101301D0180. */
function modeOf(doc, idxS) {
  for (const g of fieldsOf(doc)) {
    if (
      g.source === "schematic" &&
      g.address === "101301D0180" &&
      typeof g.raw === "string" &&
      g.raw.trim() !== ""
    ) {
      return g.raw;
    }
  }
  const sp = idxS.get("1;10") || idxS.get("3;32");
  return sp && typeof sp.raw === "string" && sp.raw.trim() !== "" ? sp.raw : null;
}

// ---------------------------------------------------------------------------
// TEIL 1: fetchState — /api/v1/state mit Demo-Fallback
// ---------------------------------------------------------------------------

/**
 * Ruft /api/v1/state ab (apiGet-Wrapper).
 *
 * @param {string} token Read-Token (Bearer)
 * @returns {Promise<{ok: boolean, data: object|null, demo: boolean, reason: string|null}>}
 *   ok=true + data (State-Doc, evtl. stale = CMI down, Letztwerte behalten)
 *   bei 200; ok=false + demo=true + reason bei 5xx/401/Unreachable -> UI nutzt
 *   demoState() und zeigt Badge „Demo-Daten“ (ui-architecture.md §4).
 */
export async function fetchState(token) {
  try {
    const data = await apiGet("/api/v1/state", token);
    if (data && Array.isArray(data.fields)) {
      return { ok: true, data, demo: false, reason: null };
    }
    return { ok: false, data: null, demo: true, reason: "State-Doc ohne fields[]" };
  } catch (err) {
    const status = err && Number.isFinite(err.status) ? err.status : 0;
    let reason = "Gateway nicht erreichbar (network error)";
    if (status === 401) reason = "401: Token ungültig/fehlt";
    else if (status >= 500) reason = `Gateway-Fehler (HTTP ${status})`;
    else if (status > 0) reason = `HTTP ${status}`;
    else if (err && err.message) reason = err.message;
    return { ok: false, data: null, demo: true, reason };
  }
}

// ---------------------------------------------------------------------------
// TEIL 1: buildLoopStates — State-Doc -> {A..E, meta}
// ---------------------------------------------------------------------------

/**
 * Baut das Loop-Zustandsmodell aus dem (live oder Demo) State-Doc.
 *
 * @param {object} stateDoc State-Doc (fields[] + health, s. app/state.py).
 * @returns {{A: object, B: object, C: object, D: object, E: object,
 *            meta: {mode: string|null, modeLabel: string|null, cmiOk: boolean}}}
 *
 * Je Loop:
 *   values: device:channel -> { value, unit, label } (value=null wenn fehlt)
 *   pumps:  O-Status je zugehörigem Antrieb { key, on, text, source } (null -> „?“)
 *   active: >=1 Antrieb an ODER (E) RAS aktiv (konservativ, R-X4)
 *   ras:    nur E — Keller/EG/DG mit value + rasState + active-Flag
 */
export function buildLoopStates(stateDoc) {
  const doc = stateDoc || {};
  const idxJ = jsonIndex(doc);
  const idxS = schIndex(doc);

  const mode = modeOf(doc, idxS);
  const meta = {
    mode,
    modeLabel: mode != null ? `Modus: ${mode}` : null,
    cmiOk: !!(doc.health && doc.health.stale === false),
  };

  const drives = {};
  for (const key of Object.keys(O_FIELD)) drives[key] = oStatus(doc, idxJ, idxS, key);
  const dOn = (k) => !!(drives[k] && drives[k].on);

  const loops = {};
  for (const letter of Object.keys(LOOP_CHANNELS)) {
    const values = {};
    for (const [dev, ch, ad] of LOOP_CHANNELS[letter]) {
      const key = `${dev}:${ch}`;
      const r = resolveChannel(doc, idxJ, idxS, dev, ch, ad);
      values[key] = {
        value: r.value,
        unit: r.unit,
        label: `${ad === "D" ? "d" : "a"}${ch} · ${dev}`,
      };
    }

    const ras =
      letter === "E"
        ? {
            Keller: rasStatus(doc, idxJ, idxS, "keller"),
            EG: rasStatus(doc, idxJ, idxS, "eg"),
            DG: rasStatus(doc, idxJ, idxS, "dg"),
          }
        : null;

    const pumps = {};
    for (const key of LOOP_DRIVES[letter]) pumps[key] = drives[key];

    const driveActive = LOOP_DRIVES[letter].some((k) => dOn(k));
    const rasActive =
      !!ras &&
      (ras.Keller.active === true || ras.EG.active === true || ras.DG.active === true);
    const active = letter === "E" ? driveActive || rasActive : driveActive;

    loops[letter] = {
      letter,
      values,
      pumps,
      ras,
      active,
      activeReason: driveActive ? "Antrieb an" : rasActive ? "RAS aktiv" : "inaktiv",
    };
  }

  return { ...loops, meta };
}

// ---------------------------------------------------------------------------
// TEIL 1: demoState — plausible synthetische Werte (CMI down -> „Demo-Daten“)
// ---------------------------------------------------------------------------

/**
 * Synthetischer State-Doc (Form wie /api/v1/state, fields[] + schematics{})
 * fuer den Demo-Modus. health.stale=true => buildLoopStates() liefert
 * cmiOk=false; die Werte liegen im plausiblen Bereich: P 45–55 °C,
 * VL 40–55 je Modus, RAS 19–22, Kollektor 30–60, WW 48–52, Abgas 120–180.
 *
 * Szenario „Heizbetrieb NORMAL“: O8 + O7 + O1 an (O1 folgt der
 * WIP-Demo-Logik: Heizsaison an), O9 an (Ruecklauf zu H2, konservativ),
 * Solar SV an bei Sonne (Kollektor > P-Unten), O5/O6/O2 aus.
 *
 * Das Schematic-Block (pages 1–5) ist NICHT TEIL der Task-Spezifikation,
 * sondern wird von der WIP-Rendering-Schicht (mapState, loops.js) als
 * Demo-Basis gebraucht und bleibt daher unveraenderlich dabei.
 *
 * @param {number} [nowMs] Zeitstempel (Default: jetzt)
 * @returns {object} State-Doc-Form (fields[] + health + schematics).
 */
export function demoState(nowMs = Date.now()) {
  const d = new Date(nowMs);
  const h = d.getHours() + d.getMinutes() / 60;
  // Tageskurve: 0 bei 6 Uhr, 1 bei 14 Uhr, 0 bei 22 Uhr
  const sun = Math.max(0, Math.sin(((h - 6) / 14) * Math.PI));
  const t = nowMs / 1000;
  const w = (a, ph) => a * Math.sin(t / 5400 + ph); // langsame Schwankung
  const on8 = true; // Heizsaison: Heizkreispumpe laeuft im Demo
  const svOn = sun > 0.5;

  const num = (x) => Math.round(x * 10) / 10;
  const p = (page, index, pos_type, text, value, unit, label, address) => ({
    page, index, pos_type, text, value, unit, label, address, x2: false, top: null, left: null,
  });

  const nowSec = nowMs / 1000;
  const f = (id, label, value, unit, raw, source, reference, address, ras_state) => ({
    id,
    label,
    value,
    unit,
    raw,
    source,
    reference,
    address: address || null,
    ras_state: ras_state || null,
    timestamp: nowSec,
  });

  return {
    cmi: "demo",
    timestamp: nowMs / 1000,
    health: {
      schematic_ok: false, api_ok: false,
      schematic_time: null, api_time: null, schematic_error: "demo", api_error: "demo",
      stale: true, // -> buildLoopStates().meta.cmiOk = false (Demo-Daten)
    },
    fields: [
      // Modus (M, R-X4)
      f("demo:mod", "Betriebsmodus", null, null, "NORMAL", "schematic", "page=1;pos=10", "101301D0180"),
      // O-Antriebe (Schematic-Text, address-gelot wie im Live-State)
      f("demo:o1", "Pumpe Holzofen", null, null, on8 ? "AUTO/EIN" : "AUTO/AUS", "schematic", "page=1;pos=15", "10120A10180"),
      f("demo:o5", "Oelbrenner", null, null, "AUTO/AUS", "schematic", "page=1;pos=23", "10520A10180"),
      f("demo:o6", "WW vom Oel", null, null, "AUTO/AUS", "schematic", "page=1;pos=18", "10220A10180"),
      f("demo:o2", "Ladepumpe WW", null, null, "AUTO/AUS", "schematic", "page=1;pos=17", "10620A10180"),
      f("demo:o7", "Zirkulation", null, null, "AUTO/EIN", "schematic", "page=1;pos=21", "10720A10180"),
      f("demo:o8", "Pumpe Heizkreis", null, null, "AUTO/EIN", "schematic", "page=1;pos=11", "10820A10180"),
      f("demo:o9", "Ventil HzRkl", null, null, "HAND/EIN", "schematic", "page=1;pos=25", "10920A10180"),
      f("demo:sv-mode", "Solarpumpe Modus", null, null, "Auto", "schematic", "page=4;pos=14", "10040912091"),
      f("demo:sv-st", "Solarpumpe", null, null, svOn ? "ON" : "OFF", "schematic", "page=4;pos=13"),
      // node 1 Inputs (analog, dev0) — Task-Bereiche
      f("demo:a1", "P-Oben", num(52.4), "°C", "52.4", "json_api", "node=1;section=Inputs;channel=1"),
      f("demo:a2", "P-Mitte", num(50.8), "°C", "50.8", "json_api", "node=1;section=Inputs;channel=2"),
      f("demo:a3", "P-Unten", num(48.9), "°C", "48.9", "json_api", "node=1;section=Inputs;channel=3"),
      f("demo:a4", "WW", num(50.2), "°C", "50.2", "json_api", "node=1;section=Inputs;channel=4"),
      f("demo:a5", "Oelkessel-VL", num(46.5), "°C", "46.5", "json_api", "node=1;section=Inputs;channel=5"),
      f("demo:a6", "VL", num(48.3), "°C", "48.3", "json_api", "node=1;section=Inputs;channel=6"),
      f("demo:a7", "RL zentral", num(41.6), "°C", "41.6", "json_api", "node=1;section=Inputs;channel=7"),
      f("demo:a8", "Aussen", num(8.4), "°C", "8.4", "json_api", "node=1;section=Inputs;channel=8"),
      f("demo:a9", "Holzkessel-Tank", num(45.2), "°C", "45.2", "json_api", "node=1;section=Inputs;channel=9"),
      f("demo:a10", "Abgas", num(152), "°C", "152", "json_api", "node=1;section=Inputs;channel=10"),
      f("demo:ss16", "Stroemungsschalter", 1, "Aus/EIN", "1", "json_api", "node=1;section=Inputs;channel=16"),
      // node 1 Outputs (digital, O1..O9 + Mischer O3/O4)
      f("demo:out1", "Pumpe Holzofen", 0, "0", "0", "json_api", "node=1;section=Outputs;channel=1"),
      f("demo:out2", "Ladepumpe WW", 0, "0", "0", "json_api", "node=1;section=Outputs;channel=2"),
      f("demo:out3", "Mischkreis Auf", 0, "0", "0", "json_api", "node=1;section=Outputs;channel=3"),
      f("demo:out4", "Mischkreis Zu", 1, "0", "1", "json_api", "node=1;section=Outputs;channel=4"),
      f("demo:out5", "Brenner", 0, "0", "0", "json_api", "node=1;section=Outputs;channel=5"),
      f("demo:out6", "WW vom Oel", 0, "0", "0", "json_api", "node=1;section=Outputs;channel=6"),
      f("demo:out7", "Zirkulation", 1, "0", "1", "json_api", "node=1;section=Outputs;channel=7"),
      f("demo:out8", "Pumpe Heizkreis", 1, "0", "1", "json_api", "node=1;section=Outputs;channel=8"),
      f("demo:out9", "Ventil HzRkl", 1, "0", "1", "json_api", "node=1;section=Outputs;channel=9"),
      // RAS (E): Raum-Temp dev1 (Schematic p3:10/11/12) + Zonen-Regler (node 32)
      f("demo:ras-k", "Keller Raumtemperatur", num(20.3), "°C", "20.3", "schematic", "page=3;pos=10"),
      f("demo:ras-eg", "Raumtemperatur", num(21.1), "°C", "21.1", "schematic", "page=3;pos=11"),
      f("demo:ras-dg", "Dachgeschoss Raumtemperatur", num(20.7), "°C", "20.7", "schematic", "page=3;pos=12"),
      f("demo:ras-st-k", "Keller RAS 1", num(20.3), "°C (RAS)", "20.3", "json_api", "node=32;section=Logging Analog;channel=4", null, "Zeit/Auto"),
      f("demo:ras-st-eg", "Erdgeschoss RAS 1", num(21.1), "°C (RAS)", "21.1", "json_api", "node=32;section=Logging Analog;channel=6", null, "Zeit/Auto"),
      f("demo:ras-st-dg", "Dachgeschoss RAS 1", num(20.7), "°C (RAS)", "20.7", "json_api", "node=32;section=Logging Analog;channel=8", null, "Zeit/Auto"),
      // node 32 (dev2): Kollektor a16 + Solarpumpe d1
      f("demo:kollektor", "T.Kollektor 1", num(48.5), "°C", "48.5", "json_api", "node=32;section=Logging Analog;channel=16"),
      f("demo:solar-d1", "Solarpumpe 1", svOn ? 1 : 0, "Aus/EIN", svOn ? "1" : "0", "json_api", "node=32;section=Outputs;channel=1"),
      f("demo:solar-dlg1", "Solarpumpe 1", svOn ? 1 : 0, "Aus/EIN", svOn ? "1" : "0", "json_api", "node=32;section=Logging Digital;channel=1"),
    ],
    params_resolved: 0,
    schematics: {
      "1": {
        page: 1,
        positions: [
          p(1, 10, "mode_select", "NORMAL", null, null, "Betriebsmodus", "101301D0180"),
          p(1, 11, "switch", on8 ? "AUTO/EIN" : "AUTO/AUS", null, null, "Pumpe Heizkreis", "10820A10180"),
          p(1, 13, "switch", "Auto", null, null, "Solarpumpe Modus", "10040912091"),
          p(1, 15, "switch", on8 ? "AUTO/EIN" : "AUTO/AUS", null, null, "Pumpe Holzofen", "10120A10180"),
          p(1, 17, "switch", "AUTO/AUS", null, null, "Ladepumpe WW", "10620A10180"),
          p(1, 18, "switch", "AUTO/AUS", null, null, "WW vom Oel", "10220A10180"),
          p(1, 21, "switch", "AUTO/EIN", null, null, "Zirkulation", "10720A10180"),
          p(1, 23, "switch", "AUTO/AUS", null, null, "Oelbrenner", "10520A10180"),
          p(1, 25, "switch", "HAND/EIN", null, null, "Ventil HzRkl", "10920A10180"),
        ],
      },
      "2": {
        page: 2,
        positions: [
          p(2, 9, "sensor_value", null, num(58 + 9 * sun + w(1.5, 0)), "°C", "Speicher Oben"),
          p(2, 10, "sensor_value", null, num(57 + 8 * sun + w(1.2, 1)), "°C", "Speicher Mitte"),
          p(2, 11, "sensor_value", null, num(55 + 7 * sun + w(1.1, 2)), "°C", "Speicher Unten"),
          p(2, 12, "sensor_value", null, num(49 + w(1.4, 3)), "°C", "Warmwasser"),
        ],
      },
      "3": {
        page: 3,
        positions: [
          p(3, 9, "sensor_value", null, num(7 + 6 * sun + w(0.8, 4)), "°C", "Aussen Temp."),
          p(3, 10, "sensor_value", null, num(19.4 + w(0.5, 5)), "°C", "Keller Raumtemperatur"),
          p(3, 11, "sensor_value", null, num(20.6 + w(0.5, 6)), "°C", "Raumtemperatur"),
          p(3, 12, "sensor_value", null, num(20.1 + w(0.5, 7)), "°C", "Dachgeschoss Raumtemperatur"),
          p(3, 16, "sensor_value", null, num(36.5 + 3 * (on8 ? 1 : 0) + w(1.0, 8)), "°C", "RL"),
          p(3, 17, "sensor_value", null, num(44.5 + 3.5 * sun + w(1.2, 9)), "°C", "VL"),
          p(3, 18, "sensor_value", null, num(66 + 4 * sun + w(1.0, 10)), "°C", "Oelkessel Vorlauf"),
        ],
      },
      "4": {
        page: 4,
        positions: [
          p(4, 9, "sensor_value", null, num(13 + 32 * sun + w(1.5, 11)), "°C", "Kollektor"),
          p(4, 13, "sensor_value", svOn ? "ON" : "OFF", null, null, "Solarpumpe"),
          p(4, 14, "switch", "Auto", null, null, "Solarpumpe Modus", "10040912091"),
        ],
      },
      "5": {
        page: 5,
        positions: [
          p(5, 12, "sensor_value", null, num(sun > 0.3 ? 58 + 12 * sun : 18), "°C", "Abgas"),
          p(5, 15, "sensor_value", sun > 0.3 ? "AN" : "AUS", null, null, "Abgas Holz AN/AUS"),
          p(5, 18, "sensor_value", null, num(sun > 0.3 ? 68 + 6 * sun : 34), "°C", "Holzkessel Vorlauf"),
        ],
      },
    },
  };
}

// ---------------------------------------------------------------------------
// TEIL 2: State-Leser (Schematic-primär, JSON-API-Fallback) — unveraenderter
// WIP-Vertrag fuer loops.js/loop-svg.js
// ---------------------------------------------------------------------------

const SVG_NS = "http://www.w3.org/2000/svg";

function schPos(doc, page, pos) {
  const pg = doc && doc.schematics && doc.schematics[page];
  if (!pg || !Array.isArray(pg.positions)) return null;
  return pg.positions.find((p) => p.index === pos) || null;
}

function buildJsonIndex(doc) {
  const idx = new Map();
  for (const f of (doc && doc.fields) || []) {
    if (f.source !== "json_api" || !f.reference) continue;
    const m = /node=(\d+);section=([^;]+);channel=(\d+)/.exec(f.reference);
    if (!m) continue;
    idx.set(`${m[1]}|${m[2]}|${m[3]}`, f);
  }
  return idx;
}

function jsonVal(doc, idx, node, section, ch) {
  const f = idx.get(`${node}|${section}|${ch}`);
  return f ? f.value : null;
}

/** Analogwert: Schematic-Position (page,pos) primär, sonst JSON-API-Kanal.
 *  sch=null = json-only (kein Schematic, z. B. Minderer-V). preferJson=true:
 *  JSON-API-Quelle VOR dem Schematic (Schematic-Label fehlerhaft, z. B.
 *  Keller/DG-Vertauschung bei den Raum-Temperaturen). */
function analogValue(doc, idx, sch, json, preferJson = false) {
  if (preferJson && json) {
    const v = jsonVal(doc, idx, json.node, json.section, json.ch);
    if (v != null) return v;
  }
  const sp = sch ? schPos(doc, sch.page, sch.pos) : null;
  if (sp && sp.value != null) return sp.value;
  if (json) {
    const v = jsonVal(doc, idx, json.node, json.section, json.ch);
    if (v != null) return v;
  }
  return null;
}

/** Schalter-Status: Schematic-Text primär, sonst JSON-Output (0/1). */
function switchValue(doc, idx, sch, json) {
  const sp = schPos(doc, sch.page, sch.pos);
  if (sp && sp.text) {
    return { text: sp.text, on: parseOn(sp.text), source: "schematic" };
  }
  if (json) {
    const v = jsonVal(doc, idx, json.node, json.section, json.ch);
    if (v != null) {
      return { text: v === 1 ? "EIN" : "AUS", on: v === 1, source: "json" };
    }
  }
  return null;
}

function parseOn(text) {
  const t = String(text || "").toUpperCase();
  if (/AUS|OFF/.test(t)) return false;
  return /EIN|\bON\b|AN$/.test(t);
}

// ---------------------------------------------------------------------------
// TEIL 2: mapState — /api/v1/state -> Loop-Modell (Schematic primär)
// ---------------------------------------------------------------------------
export function mapState(doc, { demo = false } = {}) {
  const idx = buildJsonIndex(doc);

  // -- Schalter/Pumpen ------------------------------------------------------
  const sw = {
    o1: switchValue(doc, idx, { page: 1, pos: 15 }, { node: 1, section: "Outputs", ch: 1 }),
    o5: switchValue(doc, idx, { page: 1, pos: 23 }, { node: 1, section: "Outputs", ch: 5 }),
    o6: switchValue(doc, idx, { page: 1, pos: 18 }, null),
    o2: switchValue(doc, idx, { page: 1, pos: 17 }, { node: 1, section: "Outputs", ch: 2 }),
    o7: switchValue(doc, idx, { page: 1, pos: 21 }, { node: 1, section: "Outputs", ch: 7 }),
    o8: switchValue(doc, idx, { page: 1, pos: 11 }, { node: 1, section: "Outputs", ch: 8 }),
    o9: switchValue(doc, idx, { page: 1, pos: 25 }, { node: 1, section: "Outputs", ch: 9 }),
    sv: switchValue(doc, idx, { page: 4, pos: 13 }, { node: 32, section: "Outputs", ch: 1 }),
    svMode: switchValue(doc, idx, { page: 4, pos: 14 }, { node: 32, section: "Outputs", ch: 1 }),
    abgas: switchValue(doc, idx, { page: 5, pos: 15 }, null),
  };

  // Mischer O3/O4 (C1-intern, R-E2): lesbar ueber dev0 d3/d4 = JSON Outputs 3/4.
  const mixAuf = jsonVal(doc, idx, 1, "Outputs", 3);
  const mixZu = jsonVal(doc, idx, 1, "Outputs", 4);
  sw.mischer = {
    text: mixAuf != null && mixZu != null ? `Auf ${mixAuf === 1 ? "an" : "aus"} / Zu ${mixZu === 1 ? "an" : "aus"}` : "–",
    on: mixAuf === 1 || mixZu === 1,
    source: "json",
  };

  // Stroemungsschalter a16 (Zirk-Überwachung, R-D3): nur JSON-API (dev0 in16).
  const ssVal = jsonVal(doc, idx, 1, "Inputs", 16);
  sw.a16ss = {
    text: ssVal == null ? "–" : ssVal === 1 ? "Zirk an" : "Zirk aus",
    on: ssVal === 1,
    source: "json",
  };

  // -- analoge Readouts -----------------------------------------------------
  const readouts = {};
  const specs = {
    pOben: { sch: { page: 2, pos: 9 }, json: { node: 1, section: "Inputs", ch: 1 } },
    pMitte: { sch: { page: 2, pos: 10 }, json: { node: 1, section: "Inputs", ch: 2 } },
    pUnten: { sch: { page: 2, pos: 11 }, json: { node: 1, section: "Inputs", ch: 3 } },
    a4: { sch: { page: 2, pos: 12 }, json: { node: 1, section: "Inputs", ch: 4 } },
    a5: { sch: { page: 3, pos: 18 }, json: { node: 1, section: "Inputs", ch: 5 } },
    a6: { sch: { page: 3, pos: 17 }, json: { node: 1, section: "Inputs", ch: 6 } },
    a7: { sch: { page: 3, pos: 16 }, json: { node: 1, section: "Inputs", ch: 7 } },
    aussen: { sch: { page: 3, pos: 9 }, json: { node: 1, section: "Inputs", ch: 8 } },
    a9: { sch: { page: 5, pos: 18 }, json: { node: 1, section: "Inputs", ch: 9 } },
    a10: { sch: { page: 5, pos: 12 }, json: { node: 1, section: "Inputs", ch: 10 } },
    kollektor: { sch: { page: 4, pos: 9 }, json: { node: 32, section: "Inputs", ch: 4 } },
    // Raum-Ist-Temperatur: node32 „Temp"-Kanäle sind autoritativ — das
    // Schematic-Label Keller/DG ist vertauscht (p3:10/12), daher json primär.
    rasK: { sch: { page: 3, pos: 10 }, json: { node: 32, section: "Logging Analog", ch: 5 }, preferJson: true },
    rasEg: { sch: { page: 3, pos: 11 }, json: { node: 32, section: "Logging Analog", ch: 7 }, preferJson: true },
    rasDg: { sch: { page: 3, pos: 12 }, json: { node: 32, section: "Logging Analog", ch: 9 }, preferJson: true },
    // Strang-Rücklauf-Temp pro Kreis (node32 „HzRkl Ventil 1" / p3:13-15)
    rlK: { sch: { page: 3, pos: 13 }, json: { node: 32, section: "Logging Analog", ch: 13 } },
    rlDg: { sch: { page: 3, pos: 14 }, json: { node: 32, section: "Logging Analog", ch: 14 } },
    rlEg: { sch: { page: 3, pos: 15 }, json: { node: 32, section: "Logging Analog", ch: 15 } },
    // Minderer-Stellung pro Kreis (node32 „HzRkl Ventil 2 Ergebnis", 0–10 V):
    // KEIN Schematic vorhanden -> json-only (sch=null), Einheit V.
    rmK: { json: { node: 32, section: "Logging Analog", ch: 10 }, unit: "V" },
    rmEg: { json: { node: 32, section: "Logging Analog", ch: 11 }, unit: "V" },
    rmDg: { json: { node: 32, section: "Logging Analog", ch: 12 }, unit: "V" },
  };
  for (const [key, spec] of Object.entries(specs)) {
    readouts[key] = {
      value: analogValue(doc, idx, spec.sch, spec.json, spec.preferJson),
      unit: spec.unit || "°C",
    };
  }
  // Aliase fuer die READOUT_META-Slots
  readouts.a1 = readouts.pOben;

  // -- Betriebsmodus (M, R-X4) ---------------------------------------------
  let modus = null;
  const sp = schPos(doc, 1, 10);
  if (sp && sp.text) modus = sp.text;

  // -- Fluss-Entscheidungen (konservativ, s. Kopfkommentar) -----------------
  // O9-Ruecklauf-Entscheidung (HITL 2026-10-29): EIN = (b) -> P „unten“,
  // AUS = (a) -> H2, AUTO = C1-entschieden (kein Pfad sichtbar).
  const o9Raw = sw.o9 ? String(sw.o9.text || "").toUpperCase() : "";
  const o9Mode =
    o9Raw.includes("EIN") ? "toP" : o9Raw.trim() === "AUS" ? "toH2" : null;
  const on = (s) => !!(s && s.on);
  const flow = {
    A: new Set(on(sw.o1) ? ["a-vl", "a-rl", "o1"] : []),
    B: new Set(),
    C: new Set(on(sw.sv) ? ["c-vl", "c-rl", "sv"] : []),
    D: new Set(),
    E: new Set(),
  };
  // Abgas (H1 aktiv): digitaler Abgas-Schalter ODER Holzkessel-T > 45 °C
  const abgasOn = on(sw.abgas) || (readouts.a9.value != null && readouts.a9.value > 45);
  if (abgasOn) flow.A.add("abgas");
  // Loop B: HEIZ-Modus nur, wenn E laeuft UND O9 zu H2 gerichtet ist
  // (R-B1: P-Mitte -> H2 -> Mischer). WW-Modus-Pfad bei O6 an.
  if (on(sw.o8) && o9Mode === "toH2") flow.B.add("b-rl");
  if (on(sw.o8) && o9Mode === "toH2") flow.B.add("b-vl");
  if (on(sw.o6)) flow.B.add("b-ww");
  if (on(sw.o5) || flow.B.has("b-vl")) flow.B.add("h2");
  if (on(sw.o2)) flow.D.add("d-lade");
  if (on(sw.o7)) flow.D.add("d-zirk");
  if (on(sw.o8)) flow.E.add("e-src");
  if (on(sw.o8)) flow.E.add("e-vl");
  if (on(sw.o8)) flow.E.add("e-rooms");
  if (on(sw.o8)) flow.E.add("e-rl");
  if (on(sw.o8)) flow.E.add("o8");
  if (o9Mode === "toH2") flow.E.add("o9a");
  if (o9Mode === "toP") flow.E.add("o9b");
  if (on(sw.o5) || flow.B.has("b-vl")) flow.E.add("h2");
  if (abgasOn || on(sw.o1)) flow.A.add("h1");

  return {
    demo: !!demo,
    modus,
    switches: sw,
    readouts,
    flow,
  };
}

// Demo-Feld fuer die JSON-only Messstellen (Stroemungsschalter a16, Mischer
// d3/d4): demoState() traegt das vollstaendige fields-Array; diese Funktion
// (WIP-Vertrag, loops.js) ueberschreibt fields bewusst auf die 3 JSON-only
// Zaehner, damit mapState() die Schematic-Primärlogik uebt.
export function withDemoJsonFields(doc, nowMs) {
  const d = new Date(nowMs);
  const h = d.getHours() + d.getMinutes() / 60;
  const sun = Math.max(0, Math.sin(((h - 6) / 14) * Math.PI));
  const t = nowMs / 1000;
  const w = (a, ph) => a * Math.sin(t / 5400 + ph);
  doc.fields = [
    { id: "demo:in16", label: "Stroemungsschalter", value: Math.abs(w(0.6, 1)) > 0.4 ? 1 : 0, unit: "Aus/EIN", raw: "demo", source: "json_api", reference: "node=1;section=Inputs;channel=16" },
    { id: "demo:out3", label: "Mischkreis Auf", value: 0, unit: "0", raw: "demo", source: "json_api", reference: "node=1;section=Outputs;channel=3" },
    { id: "demo:out4", label: "Mischkreis Zu", value: 1, unit: "0", raw: "demo", source: "json_api", reference: "node=1;section=Outputs;channel=4" },
  ];
  void sun;
  return doc;
}

export { SVG_NS, parseOn };
