// Historie-Tab: Serie-Labels (Q-129 / UI-HistFix) -- A/D-aware.
//
// Root-Cause der falschen Labels: die Multi-Serie-Response-Keys von
// /api/v1/history sind `device:channel` (KEINE ad-Dimension -- API-
// Kontrakt unveraenderlich). Die alte Logik loeste das Label NUR aus
// der Kanal-Nummer: ein Kanal mit analogem UND digitalem Zwilling
// bekam immer das analoge Label, auch bei D-Auswahl (z. B. winsol:0
// ch 8/9 (D) -> "Temperatur Aussen" / "Holzkessel Vorlauf"). Die
// A/D-Information der Checkbox-Auswahl ({channel, ad}) muss stattdessen
// verwendet werden.
//
// Nicht-Winsol-Serien (json:<node> / schematic) und Auswahl ohne ad
// fallen auf die originale channelLabel()-Logik zurueck (hierher aus
// history.js umgezuegt, Q-129; Funktionskoerper unveraendert).

import { WINSOL_LABELS } from "./winsol-labels.js";

/**
 * Chart-Serie-Label (Q-129) aus der Kanal-Auswahl -- A/D-aware.
 *
 * @param {string} device Gerat-Id, z. B. "winsol:0" / "json:32" / "schematic"
 * @param {{channel:number, ad?:string}} item Ausgewaehlter Kanal
 * @param {Array<{channel:number, ad?:string}>} items ALLE aktuell
 *     ausgewaehlten Items dieses Query-Laufs (erkent die Mixed-Auswahl)
 * @param {object|null} doc State-Dokument (Fallback-Labels json:/schematic)
 * @returns {string} Label fuer Chart-Legende + Tooltip
 */
export function seriesLabelForSelection(device, item, items, doc) {
  const ch = item.channel;
  if (device.startsWith("winsol:")) {
    // Mixed-Auswahl (A+D gleichzeitig gecheckt): der Query-Run OHNE
    // ad-Filter liefert die A+D-Union pro Kanal (ein Response-Key pro
    // Kanal-Nummer) -- KEIN A- oder D-Label ist korrekt. Entscheidung
    // (Q-129): neutrales Label "ch N (A+D)" statt eines Tippes; die
    // max-3-Begrenzung macht das selten, es darf aber nicht crashen.
    if (new Set(items.map((i) => i.ad)).size > 1) {
      return `ch ${ch} (A+D)`;
    }
    if (item.ad === "A" || item.ad === "D") {
      const labels = WINSOL_LABELS[device] || {};
      const curated = (labels[item.ad === "A" ? "analog" : "digital"] || {})[ch];
      // Kanal nicht kuratiert (kein Setup.xml-Label) -> neutrales
      // Fallback-Label (z. B. "ch 12 (A)").
      return curated ? `${curated} (${item.ad})` : `ch ${ch} (${item.ad})`;
    }
  }
  // Keine A/D-Semantik (json:<node> / schematic) oder kein ad in der
  // Auswahl: exakt die ALT-Logik von channelLabel (State-Fields,
  // sonst "Kanal N").
  return channelLabel(device, `${device}:${ch}`, doc);
}

/**
 * Kanal-Label fuer eine Serie ("device:channel"): WinSol-Label aus
 * Setup.xml (+ A/D-Markierung, da der API-Key nur device:channel tragt),
 * sonst Live-Label aus dem State.
 *
 * Q-129 (UI-HistFix): aus history.js hierher umgezogen (Logik NICHT
 * dupliziert, sondern exakt uebernommen); dient jetzt als Fallback-Pfad
 * von seriesLabelForSelection().
 */
export function channelLabel(device, key, doc) {
  const chStr = key.split(":").pop();
  const ch = Number(chStr);
  if (device.startsWith("winsol:")) {
    const labels = WINSOL_LABELS[device] || {};
    const a = (labels.analog || {})[ch];
    const d = (labels.digital || {})[ch];
    if (a && !d) return `${a} (A)`;
    if (d && !a) return `${d} (D)`;
    if (a) return a; // A/D gemischt (union, nur ohne ad-Filter)
    return `Kanal ${ch}`;
  }
  for (const field of (doc && doc.fields) || []) {
    if (device === "schematic" && field.source === "schematic") {
      const m = /page=(\d+);pos=(\d+)/.exec(field.reference || "");
      if (m && Number(m[1]) * 100 + Number(m[2]) === ch) {
        return `${field.label} (S${m[1]}.${m[2]})`;
      }
    }
    if (field.source === "json_api") {
      const m = /node=(\d+);section=[^;]+;channel=(\d+)/.exec(
        field.reference || ""
      );
      if (m && `json:${m[1]}` === device && Number(m[2]) === ch) {
        return field.label;
      }
    }
  }
  return `Kanal ${ch}`;
}
