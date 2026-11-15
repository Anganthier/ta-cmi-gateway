// P&ID-Symbol-Loader (S4N-2, Teil 1): ui/svg/pid-symbols.svg -> Inline-<defs>.
//
// Die Symbol-Bibliothek (S4N-0) liegt als standalone-SVG in ui/svg/pid-symbols.svg.
// Alle Views (Loop-Views A–E, S4N-2; V-G Gesamtdarstellung, S4N-3) beziehen ihre
// Symbole via <use href="#pid-..."> aus EINEM in der DOM eingebundenen
// <svg width="0" height="0" aria-hidden>-Block. loadPidsDefs() injiziert den
// <defs>-Inhalt (alle <g id="pid-*"> + <style>) in so einen Block unter
// containerEl — genau EINMAL (Flag window.__pidsLoaded).
//
// ACHTUNG URL: ui/ ist beim Gateway unter "/ gemountet (app/main.py:
// app.mount("/", StaticFiles(directory=ui_dir))), daher ist die Browser-URL
// /svg/pid-symbols.svg und NICHT /ui/svg/pid-symbols.svg (404, live geprüft).
//
// Farbprofil (Farbsprache, ui-architecture.md §1.7): die Symbole sind
// currentColor-basiert; der Aufrufer faerbt ueber die CSS-Klassen .pid-vl/
// .pid-rl/.pid-ww/.pid-gas/.pid-ctl. pidColorClass() liefert die Klassennamen;
// die Hex-Werte liegen (ab Teil 2) in app.css — PID_COLORS ist die Referenz
// der konstanten Werte für JS-Logik (z. B. Chart-Serien).

const PIDS_URL = "/svg/pid-symbols.svg";
const SVG_NS = "http://www.w3.org/2000/svg";
const HOST_CLASS = "pids-defs";

/** Laufende Lade-Aktion (Dedup: zwei Views im selben Tick rufen parallel). */
let pending = null;

/**
 * Laedt die P&ID-Symbol-Bibliothek und injiziert <defs> (+ <style>) in einen
 * unsichtbaren Inline-SVG unterhalb von containerEl.
 *
 * Idempotent: laeuft nur, wenn window.__pidsLoaded nicht gesetzt ist;
 * Folgeberufe liefern den bestehenden Host (im Container oder im Dokument).
 *
 * @param {HTMLElement} containerEl Host-Element, unter dem der SVG-Block
 *        erzeugt wird (z. B. der Loop-Canvas-Host).
 * @returns {Promise<SVGSVGElement|null>} der (unsichtbare) Host-SVG, null
 *        nur wenn das Flag gesetzt ist, aber kein Host-Block im DOM existiert.
 * @throws {Error} wenn der Fetch fehlschlaegt oder kein <defs> mit
 *        pid-*-Symbolen im Dokument gefunden wird.
 */
export async function loadPidsDefs(containerEl) {
  if (window.__pidsLoaded) {
    return (
      containerEl.querySelector(`:scope > svg.${HOST_CLASS}`) ||
      document.querySelector(`svg.${HOST_CLASS}`)
    );
  }
  if (pending) return pending;

  pending = doLoad(containerEl).finally(() => {
    pending = null;
  });
  return pending;
}

async function doLoad(containerEl) {
  const res = await fetch(PIDS_URL, {
    headers: { Accept: "image/svg+xml" },
  });
  if (!res.ok) {
    throw new Error(`pid-symbols.svg: HTTP ${res.status}`);
  }
  const text = await res.text();
  const doc = new DOMParser().parseFromString(text, "image/svg+xml");
  if (doc.querySelector("parsererror")) {
    throw new Error("pid-symbols.svg: keine gültige SVG-Datei");
  }
  const defs = doc.querySelector("defs");
  if (!defs || !defs.querySelector('[id^="pid-"]')) {
    throw new Error("pid-symbols.svg: kein <defs> mit pid-*-Symbolen gefunden");
  }

  // Unsichtbarer Traeger-Block: width/height 0 + aria-hidden, kein
  // Layout-Platzhalter. viewBox wird NICHT gesetzt (reine <use>-Referenzen).
  const host = document.createElementNS(SVG_NS, "svg");
  host.setAttribute("width", "0");
  host.setAttribute("height", "0");
  host.setAttribute("aria-hidden", "true");
  host.setAttribute("focusable", "false");
  host.classList.add(HOST_CLASS);

  // <style> zuerst: die .pid/.pid-t-Klassen gelten dokumentweit fuer alle
  // Instanzen (Styling ueber currentColor, s. Symbol-Doku im SVG).
  const style = doc.querySelector("style");
  if (style) {
    host.appendChild(document.importNode(style, true));
  }
  host.appendChild(document.importNode(defs, true));
  containerEl.appendChild(host);

  window.__pidsLoaded = true;
  return host;
}

/**
 * CSS-Farbprofil-Klasse je P&ID-Medium (Farbsprache ui-architecture.md §1.7):
 *   VL amber · RL blau · WW cyan · Abgas grau · Steuer violett.
 *
 * @param {"VL"|"RL"|"WW"|"Abgas"|"Steuer"} kind
 * @returns {string|null} CSS-Klassename oder null bei unbekanntem Medium.
 */
export function pidColorClass(kind) {
  const MAP = {
    VL: "pid-vl",
    RL: "pid-rl",
    WW: "pid-ww",
    Abgas: "pid-gas",
    Steuer: "pid-ctl",
  };
  return MAP[kind] != null ? MAP[kind] : null;
}

/**
 * Referenz-Farben des P&ID-Farbprofils (Symbol-Doku pid-symbols.svg):
 * .pid-vl{#f59e0b} .pid-rl{#4da3ff} .pid-ww{#38bdf8} .pid-gas{#94a3b8}
 * .pid-ctl{#a78bfa}. Ab Teil 2 traegt app.css das Profil; hier nur die
 * konstanten Hex-Werte fuer JS-Logik.
 */
export const PID_COLORS = {
  vl: "#f59e0b",
  rl: "#4da3ff",
  ww: "#38bdf8",
  gas: "#94a3b8",
  ctl: "#a78bfa",
};
