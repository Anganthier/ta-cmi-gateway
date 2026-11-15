// Loop-SVG (S4N-2): Inline-SVG-Views der 5 Loops (A–E).
//
// Basis: P&ID-Symbol-Bibliothek ui/svg/pid-symbols.svg (S4N-0, <use href="#pid-...">)
// + Fluss-Tokens aus state-map.js (mapState().flow). Animation (Q-13 Voll):
//   * Wasser-Flow  = gestrichelte Overlay-Leitung mit animiertem stroke-dashoffset
//   * Pumpen-Rotation = Impeller-Blätter pro Pumpe, rotieren bei aktivem Fluss
//   * Ventile      = Öffnungs-Indikator (Pfeil = offen/aktiv, Balken = zu)
//   * Kessel-aktiv = Glühen (class .on)
//
// Struktur: jede Loop-View ist eine <svg viewBox="0 0 1000 H">. Aktiv/Inaktiv
// pro Fluss-Token: <g class="flow" data-flow="tok"> (Leitung), <g class="pump-imp"
// data-flow="tok"> (Pumpe), <g class="valve-state" data-flow="tok"> (Ventil),
// <g class="boiler" data-flow="tok"> (Kessel). CSS (.loop-svg ...) regelt
// Farbe + Animation je .on/.off (ui/css/app.css).
//
// Geometrie-Regeln (S4N-2-QA): alle Leitungsendpunkte treffen exakt auf
// Symbol-Kanten (Tank: x=±30, y=±60; Zone-Linien y=±20; Kessel: Rect-Kante),
// Labels stehen mindestens 8 px von Leitungen/Symbolen ab.

import { SVG_NS } from "./state-map.js";

const NS = SVG_NS;

function g(cls, attrs = {}) {
  const n = document.createElementNS(NS, "g");
  if (cls) n.setAttribute("class", cls);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  return n;
}

function el(tag, attrs = {}, text) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  if (text != null) n.textContent = text;
  return n;
}

/**
 * Fluss-Kanal: Basis-Leitung (breit, gedimmt) + Overlay (gestrichelt, fließend).
 * data-flow = Token aus mapState().flow; dir = fwd | rev.
 */
function flowPipe(d, color, flowToken, dir = "fwd", width = 5) {
  const grp = g("flow", { "data-flow": flowToken });
  grp.append(el("path", { d, class: "pipe-base", stroke: color, "stroke-width": width }));
  const ov = el("path", {
    d,
    class: "pipe-flow",
    stroke: color,
    "stroke-width": 2.2,
    "data-dir": dir,
  });
  grp.append(ov);
  return grp;
}

/** Gerader 2-Punkt-Kanal (H/V). */
function hv(x1, y1, x2, y2, color, flowToken, dir = "fwd") {
  return flowPipe(`M${x1},${y1} L${x2},${y2}`, color, flowToken, dir);
}

/** Pumpe: P&ID-Symbol + rotierender Impeller + Label. */
function pump(cx, cy, label, flowToken, labelAbove = false) {
  const grp = g("pump-wrap");
  grp.append(el("use", { href: "#pid-pump-cent", x: cx, y: cy, class: "sym" }));
  // Impeller: 3 Blätter im Pumpenkreis (Zentrum = Pumpen-Symbol-Mitte).
  // Rotation läuft per CSS (.pump-imp.on .imp-blades { animation: spin; transform-box: fill-box })
  const imp = g("pump-imp", { "data-flow": flowToken });
  const blades = el("g", { class: "imp-blades" });
  blades.append(
    el("path", { d: `M${cx},${cy - 7} A7,7 0 0 1 ${cx + 6.06},${cy - 3.5} L${cx + 2.4},${cy - 1.4} Z` }),
    el("path", { d: `M${cx + 6.06},${cy + 3.5} A7,7 0 0 1 ${cx - 6.06},${cy + 3.5} L${cx - 2.4},${cy + 1.4} Z` }),
    el("path", { d: `M${cx - 6.06},${cy - 3.5} A7,7 0 0 1 ${cx},${cy - 7} L${cx - 2.4},${cy - 1.4} Z` })
  );
  imp.append(blades);
  grp.append(imp);
  if (label) grp.append(labelEl(cx, labelAbove ? cy - 22 : cy + 30, label));
  return grp;
}

function labelEl(x, y, text, sub = "") {
  const t = el("text", { x, y, class: "lbl" }, text);
  if (sub) t.append(el("tspan", { class: "lbl-sub", dx: 5 }, sub));
  return t;
}

/** Ventil: P&ID-Symbol + Öffnungs-Indikator (Pfeil = offen, Balken = zu). */
function valve(cx, cy, kind, label, flowToken, sub = "") {
  const grp = g("valve-wrap");
  const symId =
    kind === "3way" ? "#pid-valve-3way" : kind === "globe" ? "#pid-valve-globe" : "#pid-valve-2way";
  grp.append(el("use", { href: symId, x: cx, y: cy, class: "sym" }));
  grp.append(valveState(cx, cy, flowToken));
  if (label) grp.append(labelEl(cx, cy + 34, label, sub));
  return grp;
}

/** Öffnungs-Indikator über dem Ventil-Stiel (grüner Pfeil / grauer Balken). */
export function valveState(cx, cy, flowToken, colorCls = "v-open") {
  const ind = g("valve-state", { "data-flow": flowToken });
  ind.append(el("path", {
    d: `M${cx - 6},${cy - 28} L${cx + 6},${cy - 28} L${cx},${cy - 36} Z`,
    class: colorCls,
  }));
  ind.append(el("line", { x1: cx - 6, y1: cy - 31, x2: cx + 6, y2: cy - 31, class: "v-closed" }));
  return ind;
}

/** Messwert-Readout (PT-Symbol + Wert; Wert wird per data-readout aktualisiert). */
function readout(cx, cy, key, label, side = "right") {
  const grp = g("readout", { "data-readout": key });
  const sx = side === "left" ? -1 : 1;
  const anchor = side === "left" ? "end" : "start";
  grp.append(el("use", { href: "#pid-pt", x: cx, y: cy, class: "sym" }));
  const tx = cx + sx * 16;
  grp.append(el("text", { x: tx, y: cy - 2, class: "ro-val", "text-anchor": anchor }, "–"));
  grp.append(el("text", { x: tx, y: cy + 14, class: "ro-lbl", "text-anchor": anchor }, label));
  return grp;
}

/** Kleines Führungs-Label (Zone-Bezeichnung etc.). */
function probe(cx, cy, label, anchor = "middle") {
  return el("text", { x: cx, y: cy, class: "probe-lbl", "text-anchor": anchor }, label);
}

/** Junction-Dot: markiert eine T-Verbindung (verbunden, NICHT nur gekreuzt). */
function junc(x, y, color = "#4da3ff") {
  return el("circle", { cx: x, cy: y, r: 3.2, class: "junc", fill: color });
}

/** Junction-Punkt (T-Verbindung): kleiner fester Kreis am Aufpunkt, damit
 *  „verbunden vs. kreuzt“ eindeutig ist. */
function junction(x, y, color) {
  return el("circle", { cx: x, cy: y, r: 4, class: "junction-dot", fill: color });
}

function svgFrame(viewBox, loopId) {
  return el("svg", {
    viewBox,
    class: "loop-svg",
    role: "img",
    "aria-label": `Hydraulischer Loop ${loopId}`,
  });
}

// ---------------------------------------------------------------------------
// Loop A · Holz: H1 (O1) <-> P (VL ganz oben / RL ganz unten), Abgas a10
//   Tank (700,235): Zonen-Linien y=215/255, Kanten y=175..295
// ---------------------------------------------------------------------------
export function buildLoopA() {
  const svg = svgFrame("0 0 1000 470", "A");
  const VL = "#f59e0b", RL = "#4da3ff", GAS = "#94a3b8";

  // Puffer (rechts); Zonen-Labels linksbündlich ab Tank-Kante (x=730)
  svg.append(el("use", { href: "#pid-tank-buffer", x: 700, y: 235, class: "sym dim" }));
  svg.append(probe(740, 196, "P ganz oben (VL H1)", "start"));
  svg.append(probe(740, 236, "P-Mitte", "start"));
  svg.append(probe(740, 288, "P-Unten", "start"));
  svg.append(probe(560, 352, "P ganz unten (RL H1)"));

  // H1 Holzofen (links): Rect-Kanten y=205..255, x=142..198
  const h1 = g("boiler", { "data-flow": "h1" });
  h1.append(el("use", { href: "#pid-boiler-solid", x: 170, y: 235, class: "sym" }));
  h1.append(labelEl(170, 178, "H1 · Holzofen"));
  svg.append(h1);

  // Vorlauf: H1 (oben rechts) -[O1]-> P ganz oben
  svg.append(flowPipe("M198,205 L198,190 L700,190 L700,175", VL, "a-vl"));
  svg.append(pump(400, 190, "O1", "a-vl"));
  // Rücklauf: P ganz unten -> H1 (unten)
  svg.append(flowPipe("M700,292 L700,330 L198,330 L198,255", RL, "a-rl", "rev"));

  // Abgas: H1 (oben) -> Schornstein E1
  svg.append(el("use", { href: "#pid-chimney", x: 330, y: 120, class: "sym dim" }));
  svg.append(flowPipe("M170,205 L170,120 L322,120", GAS, "abgas"));
  svg.append(probe(400, 112, "E1 · Schornstein"));

  // Fühler
  svg.append(readout(232, 236, "a9", "a9 Wassertank"));
  svg.append(readout(250, 96, "a10", "a10 Abgas", "left"));
  svg.append(readout(748, 322, "pUnten", "a3 P-Unten"));

  return svg;
}

// ---------------------------------------------------------------------------
// Loop B · Öl: H2 (O5) — HEIZ: P-Mitte -> H2 -> Mischer(E) · WW: O6 -> P ganz oben
//   Tank (150,235): Kanten y=175..295, Zone-Linien y=215/255
//   H2 Ölkessel (430,235): Rect y=209..261, x=402..458
// ---------------------------------------------------------------------------
export function buildLoopB() {
  const svg = svgFrame("0 0 1000 470", "B");
  const VL = "#fb923c", RL = "#4da3ff", WW = "#38bdf8";

  // Puffer (links)
  svg.append(el("use", { href: "#pid-tank-buffer", x: 150, y: 235, class: "sym dim" }));
  svg.append(probe(190, 196, "P ganz oben (WW)"));
  svg.append(probe(190, 236, "P-Mitte"));
  svg.append(probe(190, 288, "P-Unten"));

  // H2 Ölkessel (Mitte)
  const h2 = g("boiler", { "data-flow": "h2" });
  h2.append(el("use", { href: "#pid-boiler-oil", x: 430, y: 235, class: "sym" }));
  h2.append(labelEl(430, 176, "H2 · Ölkessel", "O5 ⚠ RISK"));
  svg.append(h2);

  // HEIZ: P-Mitte -> H2
  svg.append(hv(180, 235, 402, 235, RL, "b-rl"));
  // HEIZ: H2-VL -> Mischer (E)
  svg.append(flowPipe("M430,209 L430,150 L780,150 L780,320", VL, "b-vl"));
  svg.append(el("use", { href: "#pid-valve-3way", x: 780, y: 340, class: "sym dim" }));
  svg.append(labelEl(780, 384, "Mischer (E)", "O3/O4 C1-intern"));
  // WW-Modus: H2 -[O6]-> P ganz oben
  svg.append(flowPipe("M402,222 L300,222 L300,120 L150,120 L150,190", WW, "b-ww"));
  svg.append(pump(300, 120, "O6", "b-ww"));
  svg.append(probe(225, 106, "WW-Modus: H2-VL → P ganz oben"));

  // Fühler
  svg.append(readout(600, 150, "a5", "a5 Öl-VL"));
  svg.append(readout(150, 330, "pMitte", "a2 P-Mitte", "left"));
  svg.append(readout(860, 210, "aussen", "a8 Außen", "left"));

  return svg;
}

// ---------------------------------------------------------------------------
// Loop C · Solar: Kollektor H3 -[SV]-> Spirale in P-Unten
//   Tank (250,250): Kanten y=190..310, Zone-Linien y=230/270
//   Kollektor (800,100): Rect y=86..114, x=766..834
// ---------------------------------------------------------------------------
export function buildLoopC() {
  const svg = svgFrame("0 0 1000 470", "C");
  const VL = "#fbbf24", RL = "#4da3ff";

  // Kollektor (Dach, oben rechts)
  svg.append(el("use", { href: "#pid-collector-solar", x: 800, y: 100, class: "sym dim" }));
  svg.append(labelEl(800, 62, "H3 · Kollektor (Dach)"));

  // Puffer (links)
  svg.append(el("use", { href: "#pid-tank-buffer", x: 250, y: 250, class: "sym dim" }));
  svg.append(probe(292, 212, "P-Oben"));
  svg.append(probe(292, 262, "P-Mitte"));
  svg.append(probe(292, 302, "P-Unten (Spirale)"));

  // Spirale in P-Unten (Rohrbund-HE), kurzer Zuleiter von Tank-Boden
  svg.append(el("line", {
    x1: 250, y1: 310, x2: 250, y2: 322,
    class: "pipe-base", stroke: RL, "stroke-width": 4, opacity: 0.5,
  }));
  svg.append(el("use", { href: "#pid-heatex-shell", x: 250, y: 330, class: "sym dim" }));

  // Vorlauf: Kollektor (unten) -[SV]-> Spirale
  svg.append(flowPipe("M800,114 L800,330 L273,330", VL, "c-vl"));
  svg.append(pump(800, 230, "SV", "c-vl"));
  // Rücklauf: Spirale (links) -> Kollektor (links)
  svg.append(flowPipe("M220,330 L180,330 L180,100 L759,100", RL, "c-rl"));

  // Fühler
  svg.append(readout(872, 130, "kollektor", "a16 Kollektor", "left"));
  svg.append(readout(340, 350, "pUnten", "a3 P-Unten"));

  return svg;
}

// ---------------------------------------------------------------------------
// Loop D · WW: P-Oben -[O2]-> Waermetauscher -> WW (a4) · Zirk [O7] -> Zapfstelle
//   Tank (150,235): Kanten y=175..295
//   Platten-HE (480,200): Kanten x=445..515, y=186..214
// ---------------------------------------------------------------------------
export function buildLoopD() {
  const svg = svgFrame("0 0 1000 470", "D");
  const WW = "#38bdf8";

  // Puffer (links)
  svg.append(el("use", { href: "#pid-tank-buffer", x: 150, y: 235, class: "sym dim" }));
  svg.append(probe(190, 196, "P-Oben (WW-Quelle)"));
  svg.append(probe(190, 236, "P-Mitte"));
  svg.append(probe(190, 288, "P-Unten"));

  // Waermetauscher (Mitte): P-Oben -> Kaltwasser -> WW
  svg.append(el("use", { href: "#pid-heatex-plate", x: 480, y: 200, class: "sym dim" }));
  svg.append(labelEl(480, 158, "W · WW-Waermetauscher"));
  svg.append(flowPipe("M180,200 L445,200", WW, "d-lade"));
  svg.append(pump(320, 200, "O2", "d-lade", true));

  // WW-Vorlauf nach unten -> Zapfstelle (fest 50 °C)
  svg.append(el("line", {
    x1: 480, y1: 214, x2: 480, y2: 323,
    class: "pipe-base", stroke: WW, "stroke-width": 4, opacity: 0.5,
  }));
  svg.append(el("use", { href: "#pid-tap", x: 480, y: 330, class: "sym dim" }));
  svg.append(labelEl(480, 366, "WW → Zapfstellen (fest 50 °C)"));

  // Zirkulation: WW -[O7]-> Zapfstelle -> WW
  svg.append(flowPipe("M515,200 L650,200 L650,330 L487,330", WW, "d-zirk"));
  svg.append(pump(650, 265, "O7", "d-zirk"));
  svg.append(probe(700, 188, "Zirk-Ring (O7)", "start"));

  // Fühler + Strömungsschalter (a16, R-D3)
  svg.append(readout(560, 178, "a4", "a4 WW", "left"));
  svg.append(readout(250, 158, "pOben", "a1 P-Oben"));
  const ss = g("readout", { "data-readout": "a16ss" });
  ss.append(el("use", { href: "#pid-flow-switch", x: 560, y: 330, class: "sym" }));
  ss.append(el("text", { x: 578, y: 322, class: "ro-val" }, "–"));
  ss.append(el("text", { x: 578, y: 352, class: "ro-lbl" }, "a16 Strömungsschalter"));
  svg.append(ss);

  return svg;
}

// ---------------------------------------------------------------------------
// Loop E · Heizung: P-Mitte -> H2 (O5) -> Mischer (O3/O4) -> O8 -> VL (a6)
//                  -> R1/R2/R3 (Einrohr + RM) -> RL (a7) -> O9
//   H2 mit 3 Anschlüssen (Kanon R-B1): (1) P-Mitte oben (e-src),
//   (2) O9-(a) links (o9a), (3) VL -> Mischer rechts (e-vl).
//   O9-Entscheidung: EIN = (b) -> P „unten“, AUS = (a) -> H2, AUTO = offen.
//   Tank (90,150): Kanten x=60..120, y=90..210 · H2 (90,330): x=62..118,y=304..356
//   Mischer (240,180) · O8 (340,180) · R1/R2/R3: y=320/420/520 · O9 (740,590)
// ---------------------------------------------------------------------------
export function buildLoopE() {
  const svg = svgFrame("0 0 1000 640", "E");
  const VL = "#f59e0b", RL = "#4da3ff";

  // Puffer (links oben): P-Mitte speist H2 (e-src), H2 speist den Mischer
  svg.append(el("use", { href: "#pid-tank-buffer", x: 90, y: 150, class: "sym dim" }));
  svg.append(readout(150, 120, "pMitte", "a2 P-Mitte", "right"));

  // H2 Ölkessel (links unten): 3 Anschlüsse — P-Mitte oben (e-src),
  // O9-(a) links (o9a), VL -> Mischer rechts (e-vl).
  const h2 = g("boiler", { "data-flow": "h2" });
  h2.append(el("use", { href: "#pid-boiler-oil", x: 90, y: 330, class: "sym" }));
  h2.append(labelEl(90, 384, "H2 · Ölkessel", "O5 ⚠ RISK"));
  svg.append(h2);
  svg.append(readout(230, 300, "a5", "a5 Öl-VL"));

  // Zulauf (R-B1): P-Mitte (rechts) -> H2-Oben (x=104, rechts des
  // Abgas-Stumpfs x<=94) — physikalisch WARMER Vorlauf (e-src, VL-Orange),
  // dann H2-Rechts -> Mischer (links, y=180)
  svg.append(flowPipe("M120,150 L120,304 L104,304", VL, "e-src"));
  svg.append(flowPipe("M118,330 L190,330 L190,180 L220,180", VL, "e-vl"));

  // Mischer (3-Wege, C1-intern)
  svg.append(valve(240, 180, "3way", "Mischer", null, "O3/O4 C1-intern"));

  // O8 (nach Mischer) -> VL (a6), Stamm zu den 3 Kreis-Ästen (x=560)
  svg.append(flowPipe("M260,180 L320,180", VL, "e-vl"));
  svg.append(pump(340, 180, "O8", "o8", true));
  svg.append(flowPipe("M360,180 L560,180 L560,520", VL, "e-vl"));
  svg.append(readout(470, 155, "a6", "a6 VL"));

  // Drei Einrohr-Kreise (R1 Keller / R2 EG / R3 DG), je RAS-Fühler + RM.
  // rl/rm = Strang-Rücklauf-Temp (°C) + Minderer-Stellung (0–10 V) pro Kreis.
  const rooms = [
    { y: 320, key: "rasK", nm: "R1 · Keller", zn: "Keller", rl: "rlK", rm: "rmK" },
    { y: 420, key: "rasEg", nm: "R2 · EG", zn: "EG", rl: "rlEg", rm: "rmEg" },
    { y: 520, key: "rasDg", nm: "R3 · DG", zn: "DG", rl: "rlDg", rm: "rmDg" },
  ];
  for (const r of rooms) {
    // VL-Ast (T vom Stamm) + Heizkörper
    svg.append(flowPipe(`M560,${r.y} L690,${r.y}`, VL, "e-rooms"));
    svg.append(junc(560, r.y, VL));
    svg.append(el("use", { href: "#pid-radiator", x: 720, y: r.y, class: "sym dim" }));
    // RAS-Fühler (dev1 ch2/5/8) rechts-oberhalb des Heizkörpers
    svg.append(readout(720, r.y - 40, r.key, r.nm, "right"));
    // RL-Ast mit Rücklaufminderer -> zentrale Sammlung (x=950)
    svg.append(flowPipe(`M750,${r.y} L840,${r.y} L840,${r.y + 34} L860,${r.y + 34}`, RL, "e-rl"));
    svg.append(el("use", { href: "#pid-throttle", x: 880, y: r.y + 34, class: "sym dim" }));
    // RM-Stellung (0–10 V) + Strang-Rücklauf (°C) — Readouts am Minderer,
    // links im freien Band zwischen Heizkörper (x<=742) und RL-Abzweig (x=840).
    // Probe-Label "RM 0–10 V" entfällt: die Readouts ersetzen es.
    svg.append(readout(830, r.y + 16, r.rm, `RM ${r.zn}`, "left"));
    svg.append(readout(830, r.y + 44, r.rl, `RL ${r.zn}`, "left"));
    svg.append(hv(900, r.y + 34, 950, r.y + 34, RL, "e-rl"));
    svg.append(junc(950, r.y + 34, RL));
  }

  // zentrale RL (a7) -> O9 (Entscheidungs-Zone unten, eigene Lanes):
  // vertikale Sammelleitung x=950 von y=354 (Keller-Junction) bis y=590,
  // bindet alle 3 Einrohr-Zweige (Keller/EG/DG) an die zentrale RL an
  svg.append(flowPipe("M950,354 L950,590 L760,590", RL, "e-rl", "fwd", 6));
  svg.append(readout(900, 470, "a7", "a7 RL", "left"));

  // O9 (Kippstiel-Ventil): zentrale RL kommt von rechts, Ausgang geht links
  svg.append(valve(740, 590, "globe", "O9 · HzRl-Entscheidung", "o9b"));
  svg.append(junc(700, 590, RL));
  // (b) -> P „unten“ (P-Linkskante y=190): Lane y=580, Aufstieg x=40
  svg.append(flowPipe("M700,590 L700,580 L40,580 L40,190 L60,190", RL, "o9b", "fwd", 3.5));
  svg.append(probe(300, 574, "(b) → P „unten“"));
  // (a) -> H2 (H2-Linkskante y=330): Lane y=600, Aufstieg x=52
  svg.append(flowPipe("M700,590 L700,600 L52,600 L52,330 L62,330", RL, "o9a", "fwd", 3.5));
  svg.append(probe(300, 616, "(a) → H2"));

  return svg;
}

// ---------------------------------------------------------------------------
// „Gesamt“: statische Referenz (S4N-3: V6.3-SVG mit Pan/Zoom + Live-Readouts)
// ---------------------------------------------------------------------------
export function buildGesamt() {
  const svg = svgFrame("0 0 1000 640", "G");
  svg.append(el("rect", { x: 8, y: 8, width: 984, height: 624, class: "gesamt-bg" }));
  svg.append(el("text", { x: 500, y: 280, class: "gesamt-title" }, "Gesamtdarstellung: folgt in S4N-3"));
  svg.append(el("text", { x: 500, y: 320, class: "gesamt-sub" },
    "Statische Doku (hydraulic-schematic.svg V6.3): Pan/Zoom, kein Live/Animation (Q-15 offen)."));
  svg.append(el("text", { x: 500, y: 350, class: "gesamt-sub" },
    "Topologie: docs/hydraulic-topology.md (FINAL) · docs/hydraulic-loops-rules.md"));
  return svg;
}

export const BUILDERS = {
  A: buildLoopA,
  B: buildLoopB,
  C: buildLoopC,
  D: buildLoopD,
  E: buildLoopE,
  G: buildGesamt,
};
