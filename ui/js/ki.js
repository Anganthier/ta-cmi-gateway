// CMI Gateway UI -- KI-Panel (S4N-UI-KI, logische "Seite 6").
//
// Rendert die L2-Empfehlungen (v0.5: cmi-brain-v003+l2-v005) aus
// GET /api/v1/recommend (Read-Token, Ergebnis-Cache >= 5 min, Stale = HTTP 200
// mit stale=true):
//   v0.5 neu:  (1) ΔT-Diagnose-Block (#ki-diagnostics, aus rm_diagnostics:
//                Konditions-Header + je Zone ΔT/Lag/Flow-Klasse),
//              (2) Minderer-Block (#ki-minderer, parameter-
//                "Minderer-Feinjustierung …", Sollspannung als Ist-Vorgabe +
//                empfohlene Vorgabe (linear geschätzt) — v0.5-engine-rework,
//                kein Gate ≥ 2 Kreise mehr),
//              (3) O8-Statuszeile (#ki-pump, parameter-"Pumpe Heizkreis O8 …";
//                Status statt Empfehlung, advisory-manuell, KEIN
//                Anwenden-Button, Design-Entscheidung 2026-11-09 +
//                engine-rework),
//              (4) Heizkurven-Card überarbeitet (parameter-"Heizkurve …",
//                Klartext + einklappbare Kalibrierungs-Details,
//                Kalibrierungs-Fallback-Hinweis).
//   Zone-Karten (Tier A, RAS-Offset, #ki-zones): v0.5-engine-rework mit
//   prominentem Raum-Beleg (Feld raum_text, defensiv) + de-emphasized
//   Offset-Text. Generische Tier-B-Advisorys (WW/Zirkulation, #ki-advisory)
//   + Apply-Flow unverändert.
// "Anwenden" existiert NUR, wenn die Empfehlung per CMI-Whitelist setzbar ist
// (executability + adresse + to); alle v0.1/v0.5-Empfehlungen sind Advisory
// (pending) -> hier ist "Anwenden" nur für v2-fähige Empfehlungen aktiv
// (Future-Proofing, DoD-Vertrag). v0.5-Buckets (Minderer/O8/Heizkurve)
// bekommen KEINE Apply-Buttons.
// Defensive Rendering: alle v0.5-Felder können fehlen (legacy Payloads) ->
// Block ausblenden, nie Fehler.
//
// Apply-Flow (S3-1 Two-Phase):
//   1) GET  /api/v1/change?adr=<adr>&to=<to>  (Read-Token) -> Confirmation-Hash
//   2) Dialog: vor/nachher + optionales Write-Token (GATEWAY_WRITE_TOKEN)
//   3) POST /api/v1/change {adr, to, confirmation_hash} (Bearer Write-Token)
//   4) Status: applied + Read-back (502 = Read-back-Mismatch -> Fehleranzeige)
//
// Auto-Refresh: alle 5 min, WENN der Tab sichtbar ist (document.visibilityState
// + Tab-aktiv) -> sonst wird KEIN /api/v1/recommend aufgerufen (Speicherung
// des Caches respektiert: Server liefert ohnehin max. 1 Run/h + TTL).

import { apiGet, getStoredToken, storeToken } from "./api.js";

const WRITE_TOKEN_KEY = "cmi-gateway.write-token";
const KI_REFRESH_MS = 5 * 60 * 1000; // 5 min (Design: Ergebnis-Cache >= 5 min)
const FORCE_COOLDOWN_MS = 15_000; // Anti-Burst: force=true max. alle 15 s
const APPLY_COOLDOWN_MS = 10_000; // Apply: Doppelklick-Sperre

// executability-Werte, die als "per CMI setzbar" gelten (v0.1 liefert
// "pending-cmi-write-discovery" = NICHT setzbar -> keine Anwenden-Karte).
const EXECUTABLE = new Set([
  "setzbar-per-cmi",
  "cmi-settable",
  "whitelist",
]);

// v0.5: ΔT-Ziel-Toleranz (± K um Ziel-Band; ok-grün innerhalb, sonst warn).
// Fallback für Payloads ohne delta_t_target_c (legacy).
const KI_DT_OK_TOL_K = 0.5;

// v0.5-engine-rework (2026-11-12): O8-Statuszeile — kompakter Status-Text
// (manuelle Stufung/Druck-Soll am Aggregat), Fallback für Legacy-Payloads
// ohne aktuelles cmi_hint. Die Stufung-Extraktion entfällt: die O8-
// Empfehlung ist advisory-manuell ohne Stufung-Mix mehr.
const O8_STATUS_FALLBACK =
  "Stufe/Druck-Soll nur am Aggregat (manuell, nicht CMI-schreibbar).";

// v0.5: Zonen-Slots im ΔT-Diagnose-Block (Kanonen-Reihenfolge Keller/EG/DG;
// payload.per_zone kann je Zone fehlen -> Block-Platzhalter).
const KI_RM_ZONES = ["kell", "eg", "dg"];

let kiTimer = null;
let lastFetch = 0; // epoch ms
let lastForce = 0;
let lastApply = 0;
let activePayload = null; // letztes /api/v1/recommend-JSON

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function $(sel, root = document) {
  return root.querySelector(sel);
}

function getWriteToken() {
  try {
    return localStorage.getItem(WRITE_TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

function setWriteToken(value) {
  try {
    if (value) {
      localStorage.setItem(WRITE_TOKEN_KEY, value);
    } else {
      localStorage.removeItem(WRITE_TOKEN_KEY);
    }
  } catch {
    /* localStorage nicht verfuegbar (Private Mode o. a.) */
  }
}

function esc(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

function fmtPct(conf) {
  const n = Number(conf);
  return Number.isFinite(n) ? `${Math.round(n * 100)} %` : "–";
}

function fmtAge(tsIso) {
  if (!tsIso) return "";
  const t = Date.parse(tsIso);
  if (!Number.isFinite(t)) return "";
  const sec = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (sec < 90) return `vor ${sec} s`;
  const min = Math.floor(sec / 60);
  return min < 90 ? `vor ${min} min` : `vor ${Math.floor(min / 60)} h`;
}

function isTabActive() {
  const active = document.querySelector('.tab[data-page="ki"]');
  return !!active && active.classList.contains("active");
}

function isKiVisible() {
  return isTabActive() && document.visibilityState === "visible";
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
function executabilityBadge(entry) {
  const exe = entry.executability || "";
  const settable = EXECUTABLE.has(exe) || !!entry.adresse;
  const label = settable
    ? "setzbar per CMI"
    : "Advisory (TA-Designer/v2)";
  const cls = settable ? "chip chip-ok" : "chip chip-warn";
  return `<span class="${cls} ki-exec-badge">${esc(label)}</span>`;
}

function zoneCard(entry, payload) {
  const changed = !!entry.changed;
  const cls = changed ? "ki-card ki-card-changed" : "ki-card";
  const adr = entry.adresse || "";
  const to = entry.to_value || "";
  const canApply = adr && to && EXECUTABLE.has(entry.executability || "");
  const applyBtn = canApply
    ? `<button class="btn-primary ki-apply-btn" type="button"
         data-adr="${esc(adr)}" data-to="${esc(to)}"
         data-parameter="${esc(entry.parameter || "")}">Anwenden</button>`
    : "";
  const hint = entry.cmi_hint
    ? `<p class="ki-hint small muted">${esc(entry.cmi_hint)}</p>`
    : "";
  // v0.5-engine-rework (2026-11-12): maintainer-Beleg-Format prominent — neues
  // Feld raum_text ("Raum EG: 21,4 °C (Ist) vs 21,8 °C (Soll; Abw. -0,4 K)").
  // Defensiv: fehlt im (alt-)Payload → Fallback auf die state-Präfix-Zeile
  // in der Begründung (Ist … (Soll …, Abw. …)).
  const raumText =
    entry.raum_text ||
    (String(entry.begruendung_de || "").match(
      /^Ist [\d.,]+ °C \(Soll [\d.,]+ °C, Abw\..*?\)/
    ) || [""])[0];
  const raumHtml =
    raumText
      ? `<p class="ki-zone-raum">${esc(raumText)}</p>
         <p class="ki-zone-raum-hint small muted">
           Raum-Soll bei Bedarf direkt im UVR/TA-Designer setzen (Offset intern).
         </p>`
      : "";
  return `
    <div class="${cls}">
      <div class="ki-card-head">
        <span class="ki-zone">${esc(entry.zone || "–")}</span>
        ${executabilityBadge(entry)}
      </div>
      ${raumHtml}
      <div class="ki-values">
        <span class="ki-value-akt">Offset: ${esc(entry.aktuell || "–")}</span>
        <span class="ki-value-arrow">${changed ? "→" : "="}</span>
        <span class="ki-value-nach">${changed ? esc(entry.vorgeschlagen || "–") : "beibehalten"}</span>
      </div>
      <p class="ki-reason small">${esc(entry.begruendung_de || "")}</p>
      ${entry.wirkung_prognose ? `<p class="ki-effect small muted">Prognose: ${esc(entry.wirkung_prognose)}</p>` : ""}
      <div class="ki-card-foot">
        <span class="chip chip-muted">Konfidenz ${fmtPct(entry.konfidenz)}</span>
        ${hint}
      </div>
      ${applyBtn}
    </div>`;
}

function advisoryCard(entry) {
  const changed = !/beibehalten|keine Empfehlung|keine Änderung/i.test(
    String(entry.vorgeschlagen || "")
  );
  return `
    <div class="ki-card ${changed ? "ki-card-advisory-active" : ""}">
      <div class="ki-card-head">
        <span class="ki-zone">${esc(entry.parameter || "Advisory")}</span>
        ${executabilityBadge(entry)}
      </div>
      <p class="ki-reason small">${esc(entry.begruendung_de || "")}</p>
      <div class="ki-card-foot">
        <span class="chip chip-muted">Konfidenz ${fmtPct(entry.konfidenz)}</span>
        ${entry.cmi_hint ? `<span class="small muted">${esc(entry.cmi_hint)}</span>` : ""}
      </div>
    </div>`;
}

// v0.5: Routing-Regel (Kanon) — parameter-Prefix -> Render-Bucket.
// Reihenfolge ist significant (keine Prefix-Überlappungen, aber zentral
// gehalten): Minderer/Pumpe/Heizkurve vor dem generischen Zone/Advisory-
// Rest. RAS-Offset (oder tier "A" ohne Prefix-Treffer) -> Zone-Karten.
function routeRec(rec) {
  const p = String(rec?.parameter || "");
  if (p.startsWith("Minderer-Feinjustierung")) return "minderer";
  if (p.startsWith("Pumpe Heizkreis O8")) return "pump";
  if (p.startsWith("Heizkurve")) return "heating";
  if (p.startsWith("RAS-Offset") || rec?.tier === "A") return "zones";
  return "advisory"; // WW-Ladefenster, Zirkulation, sonst generisches Tier B
}

// v0.5: Block-Skeleton (Header + Container) ein- oder ausblenden.
// Fehlende Daten (legacy Payload ohne Block) -> Block ausblenden, nie Fehler.
function renderBlockSection(el, headHtml, bodyHtml) {
  if (!el) return;
  if (bodyHtml) {
    el.innerHTML = `${headHtml}\n${bodyHtml}`;
    el.classList.remove("hidden");
  } else {
    el.innerHTML = "";
    el.classList.add("hidden");
  }
}

function fmtK(v, digits = 1) {
  const n = Number(v);
  return Number.isFinite(n) ? `${n.toFixed(digits)} K` : "–";
}

function fmtV(v) {
  const n = Number(v);
  return Number.isFinite(n) ? `${n.toFixed(1)} V` : "–";
}

// v0.5: ΔT-Diagnose-Block (Ticket D): Konditions-Header + je Zone eine Zeile
// (Ist-ΔT → Ziel-ΔT + Lag-Transparenz + Flow-Klasse + Daten-Hinweise).
function dtDiagnosticsHtml(payload) {
  const rm = payload?.rm_diagnostics;
  if (!rm || typeof rm !== "object") return "";
  const cond = rm.condition || {};
  const active = !!rm.active && cond.active !== false;
  const perZone = rm.per_zone || {};
  const target = Number(rm.delta_t_target_c);

  let headerHtml;
  if (!active) {
    headerHtml =
      `<div class="ki-dt-head ki-dt-head-inactive">` +
      `<span class="ki-dt-cond chip chip-muted">Kein Heizbetrieb</span>` +
      `<span class="small muted">ΔT-Diagnose inaktiv (keine Zone braucht Wärme).</span>` +
      `</div>`;
    return headerHtml;
  }
  const reasons = Array.isArray(cond.reasons)
    ? cond.reasons.filter(Boolean).slice(0, 3)
    : [];
  const reasonHtml = reasons
    .map((r) => `<li class="small">${esc(r)}</li>`)
    .join("");
  headerHtml =
    `<div class="ki-dt-head">` +
    `<span class="ki-dt-cond chip chip-ok">≥1 Kreis braucht Wärme: JA</span>` +
    `<span class="small muted">ΔT-Ziel ${fmtK(target)}</span>` +
    (reasons.length ? `<ul class="ki-dt-reasons">${reasonHtml}</ul>` : "") +
    `</div>`;

  const tol = Number.isFinite(target) ? target - KI_DT_OK_TOL_K : 9.5;
  const rows = KI_RM_ZONES.map((z) => {
    const zc = perZone[z] || {};
    const dt = zc.delta_t_c;
    const inBand =
      Number.isFinite(Number(dt)) && Number(dt) <= target + KI_DT_OK_TOL_K && Number(dt) >= tol;
    const flowCls = zc.flow_class === "offen" ? "chip-ok" : zc.flow_class === "zu" ? "chip-muted" : "chip-warn";
    const lagNote =
      zc.lag_source === "estimate"
        ? ""
        : ` · <span class="muted">Default-Lag, wenig Daten</span>`;
    const sampleNote =
      Number.isFinite(Number(zc.n_raw_samples)) && Number(zc.n_raw_samples) > 0 && Number(zc.n_raw_samples) < 360
        ? ` · <span class="muted">Roh-Samples: ${Number(zc.n_raw_samples)}</span>`
        : "";
    return `
      <div class="ki-dt-row">
        <span class="ki-dt-zone">${esc(z)}</span>
        <span class="ki-dt-dt ${inBand ? "ki-dt-ok" : "ki-dt-warn"}">
          ΔT ${fmtK(dt)} → Ziel ${fmtK(zc.delta_t_target_c ?? target)}
        </span>
        <span class="ki-dt-lag small muted">
          Lag ${zc.lag_s != null ? esc(String(zc.lag_s)) : "–"} s
          (Konf. ${fmtPct(zc.lag_confidence)}, ${esc(zc.lag_source || "unknown")})${lagNote}
        </span>
        <span class="chip ${flowCls} ki-dt-flow">${esc(zc.flow_class || "–")}</span>
        <span class="small muted">${sampleNote}</span>
      </div>`;
  }).join("");
  return headerHtml + `<div class="ki-dt-rows">${rows}</div>`;
}

// v0.5-engine-rework (2026-11-12): Minderer-Block (Ticket A). Karten aus
// `empfehlungen` mit parameter.startsWith("Minderer-Feinjustierung").
// KEINE Apply-Buttons. Das alte Gate "≥ 2 aktive Kreise" ist in der Engine
// ENTFERNT (die O8 regelt den Durchfluss; auch 1 aktiver Kreis kann
// drosseln) → kein Gate-Hinweis mehr. "Ist-Vorgabe" = Sollspannung a17/18/19
// (statischer Fixwert, 30-min-Median aus rm_diagnostics; Default 10,0 V) —
// lesbar aus `aktuell` ("Sollspannung 10.0 V (statische Vorgabe; …)").
function mindererCardHtml(entry) {
  const zone = entry.zone || "–";
  const cmiHint = entry.cmi_hint
    ? `<p class="ki-hint small muted">${esc(entry.cmi_hint)}</p>`
    : "";
  return `
    <div class="ki-card">
      <div class="ki-card-head">
        <span class="ki-zone">${esc(zone)}</span>
        <span class="chip chip-warn ki-exec-badge">Minderer-Feinjustierung (Advisory)</span>
      </div>
      <div class="ki-minderer-v">
        <span class="ki-minderer-v-label">Ist-Vorgabe (Sollspannung)</span>
        <span class="ki-minderer-v-target">${esc(entry.aktuell || "–")}</span>
      </div>
      <p class="ki-minderer-rec small">
        <span class="small muted">Empfohlene Vorgabe:</span> ${esc(entry.vorgeschlagen || "–")}
      </p>
      ${entry.wirkung_prognose ? `<p class="ki-effect small muted">Prognose: ${esc(entry.wirkung_prognose)}</p>` : ""}
      <p class="ki-reason small">${esc(entry.begruendung_de || "")}</p>
      <p class="ki-minderer-gate small muted">
        <span class="chip chip-warn">Advisory</span>
        <span>ΔT &lt; Ziel → Sollspannung runter (linear geschätzt; V→ΔT-Kennlinie
          pro Strang folgt aus Messung, v0.6).</span>
      </p>
      <div class="ki-card-foot">
        <span class="chip chip-muted">Konfidenz ${fmtPct(entry.konfidenz)}</span>
        ${cmiHint}
      </div>
    </div>`;
}

// v0.5-engine-rework (2026-11-12): O8-Statuszeile (Ticket B): Status statt
// Empfehlung. Die O8 schaltet selbstständig an/aus nach Anforderung (Modus
// AUTO), Stufe/Druck-Soll ist manuell am Aggregat — es gibt nichts
// Dynamisches zu empfehlen (advisory-manuell, kein to_value). KEIN
// Anwenden-Button (Design-Entscheidung 2026-11-09 + engine-rework).
// ``o8`` = {adresse, optionen, aktueller_modus}; fehlendes ``o8``-Objekt
// (legacy Payload) -> defensive Fallback-Texte, nie Fehler.
function pumpCardHtml(entry) {
  const o8 = entry.o8 || {};
  const opts = o8.optionen || {};
  const modus = o8.aktueller_modus || "";
  const optChips = Object.entries(opts)
    .sort(([a], [b]) => Number(a) - Number(b))
    .map(
      ([val, name]) =>
        `<span class="chip ${String(modus) === String(name) ? "chip-ok" : "chip-muted"}">${esc(name)} (${esc(val)})</span>`
    )
    .join(" ");
  // Kompakte Statuszeile: "Pumpe AUTO (an/aus nach Anforderung) ·
  // Stufe/Druck manuell am Aggregat → nichts zu tun". Modus fehlt
  // (kein Discovery-Stand) -> "Pumpe: Modus unbekannt (…)".
  const statusMain = modus
    ? `Pumpe ${esc(modus)} (an/aus nach Anforderung)`
    : "Pumpe: Modus unbekannt (kein Discovery-Stand; an/aus nach Anforderung)";
  const statusLine = `${statusMain} · Stufe/Druck manuell am Aggregat → nichts zu tun`;
  const hintSrc = entry.cmi_hint || O8_STATUS_FALLBACK;
  return `
    <div class="ki-card ki-card-pump">
      <div class="ki-card-head">
        <span class="ki-zone">Pumpe Heizkreis O8</span>
        <span class="chip chip-warn ki-exec-badge">Advisory (manuell am Aggregat)</span>
      </div>
      <p class="ki-pump-status">${statusLine}</p>
      <div class="ki-pump-opts">
        <span class="small muted">Adresse <code>${esc(o8.adresse || "–")}</code></span>
        ${optChips}
      </div>
      <p class="ki-hint small muted">${esc(hintSrc)}</p>
      <div class="ki-card-foot">
        <span class="chip chip-muted">Konfidenz ${fmtPct(entry.konfidenz)}</span>
        <span class="small muted">${esc(entry.aktuell || "")}</span>
      </div>
    </div>`;
}

// v0.5-engine-rework (2026-11-12): Heizkurven-Card (Ticket C). Klartext in
// `aktuell`/`vorgeschlagen` (Ist-VL bei Außen-T vs Standard-Kurve, "VL ~x K
// zu hoch/niedrig", "ein Raster absenken → ~0,5–1 K weniger VL-Soll …");
// Fachjargon (Raster-Kalibrierung, n, Heizstunden-Only, Band-Abweichung)
// im optionalen Feld `detail` (= `begruendung_de`) → einklappbar via
// <details>/<summary>. Ruhezustand-/Legacy-Payloads haben kein `detail` →
// Begründung bleibt wie bisher inline (klein/muted, kein <details>).
function heatingCardHtml(entry) {
  const changed = !/beibehalten|keine Empfehlung|keine Änderung/i.test(
    String(entry.vorgeschlagen || "")
  );
  const noCal = /Kalibrierung nicht verfügbar/i.test(String(entry.begruendung_de || ""));
  const detailHtml = entry.detail
    ? `<details class="ki-heating-detail">
         <summary class="small muted">Details (Kalibrierung/Raster)</summary>
         <p class="ki-reason small muted">${esc(entry.detail)}</p>
       </details>`
    : `<p class="ki-reason small">${esc(entry.begruendung_de || "")}</p>`;
  return `
    <div class="ki-card ki-card-heating ${changed ? "ki-card-advisory-active" : ""}">
      <div class="ki-card-head">
        <span class="ki-zone">Heizkurve / VL-Soll</span>
        ${executabilityBadge(entry)}
      </div>
      <p class="ki-heating-akt">${esc(entry.aktuell || "–")}</p>
      <p class="ki-heating-target ${changed ? "" : "muted"}">
        <span class="small muted">Empfehlung:</span> ${esc(entry.vorgeschlagen || "–")}
      </p>
      ${detailHtml}
      ${
        noCal
          ? `<p class="ki-hint small"><span class="chip chip-warn">Fallback</span>
             <span class="muted">Kalibrierung nicht verfügbar (v0.4-Heuristik aktiv).</span></p>`
          : ""
      }
      <div class="ki-card-foot">
        <span class="chip chip-muted">Konfidenz ${fmtPct(entry.konfidenz)}</span>
        ${entry.cmi_hint ? `<span class="small muted">${esc(entry.cmi_hint)}</span>` : ""}
      </div>
    </div>`;
}

function renderPayload(payload) {
  activePayload = payload;
  const content = $("#ki-content");
  const loading = $("#ki-loading");
  const error = $("#ki-error");
  const staleBadge = $("#ki-stale-badge");
  const ctx = $("#ki-ctx");
  if (!content) return;

  // Kopf: Stale + Datenstand + Wetter-Run + generated_at
  if (payload.stale) {
    staleBadge.classList.remove("hidden");
    staleBadge.textContent = payload.stale_reasons?.length
      ? `stale: ${payload.stale_reasons.join(", ")}`
      : "stale";
  } else {
    staleBadge.classList.add("hidden");
  }
  const k = payload.kontext || {};
  const ctxBits = [];
  if (k.datenstand) ctxBits.push(`Datenstand ${k.datenstand}`);
  if (k.wx_quelle) ctxBits.push(`Wetter ${k.wx_quelle}${k.wx_run ? ` (Run ${k.wx_run})` : ""}`);
  if (payload.model_version) ctxBits.push(`Modell ${payload.model_version}`);
  if (payload.generated_at) ctxBits.push(`generiert ${fmtAge(payload.generated_at)}`);
  if (ctx) ctx.textContent = ctxBits.join(" · ");

  // Empfehlungen routen (v0.5-Kanon): Minderer / Pumpe O8 / Heizkurve /
  // RAS-Offset-Zonen / generisches Tier B (WW, Zirkulation).
  const recs = Array.isArray(payload.empfehlungen) ? payload.empfehlungen : [];
  const buckets = { zones: [], advisory: [], minderer: [], pump: [], heating: [] };
  for (const r of recs) {
    if (!r) continue;
    const bucket = routeRec(r);
    if (bucket in buckets) buckets[bucket].push(r);
  }

  // 1) ΔT-Diagnose (v0.5 Ticket D) — Block ausblenden, wenn kein rm_diagnostics
  renderBlockSection(
    $("#ki-diagnostics"),
    `<h3 class="ki-h">ΔT-Diagnose — VL↔RL je Etage (Minderer-Öffnung als Fluss-Proxy)</h3>`,
    dtDiagnosticsHtml(payload)
  );

  // 2) Minderer-Feinjustierung (v0.5 engine-rework: Sollspannung)
  renderBlockSection(
    $("#ki-minderer"),
    `<h3 class="ki-h">Minderer-Feinjustierung (Sollspannung, Advisory)</h3>`,
    buckets.minderer.length
      ? `<div class="ki-grid">${buckets.minderer.map((r) => mindererCardHtml(r)).join("")}</div>`
      : ""
  );

  // 3) O8-Pumpe (v0.5 engine-rework: Statuszeile statt Empfehlung)
  renderBlockSection(
    $("#ki-pump"),
    `<h3 class="ki-h">Pumpe Heizkreis O8 (Status)</h3>`,
    buckets.pump.length
      ? `<div class="ki-pump-grid">${buckets.pump.slice(0, 1).map((r) => pumpCardHtml(r)).join("")}</div>`
      : ""
  );

  // 4) Tier A: RAS-Offset je Zone (unverändert)
  const zonesEl = $("#ki-zones");
  zonesEl.innerHTML = buckets.zones.length
    ? buckets.zones.map((r) => zoneCard(r, payload)).join("")
    : `<p class="muted small">Keine Zone-Empfehlungen (beibehalten).</p>`;

  // 5) Tier B: Heizkurve (überarbeitet) + generische Advisorys (WW/Zirkulation)
  const advEl = $("#ki-advisory");
  const advisoryHtml = [
    ...buckets.heating.map((r) => heatingCardHtml(r)),
    ...buckets.advisory.map((r) => advisoryCard(r)),
  ].join("");
  advEl.innerHTML = advisoryHtml
    ? advisoryHtml
    : `<p class="muted small">Keine Tier-B-Advisorys.</p>`;

  // Anwenden-Buttons binden (delegiert)
  bindApplyButtons();

  loading.classList.add("hidden");
  content.classList.remove("hidden");
  error.classList.add("hidden");
}

function showError(message, kind = "err") {
  const error = $("#ki-error");
  const content = $("#ki-content");
  const loading = $("#ki-loading");
  if (error) {
    error.textContent = message;
    error.classList.toggle("chip-err", kind === "err");
    error.classList.remove("hidden");
  }
  if (content) content.classList.add("hidden");
  if (loading) loading.classList.add("hidden");
}

// ---------------------------------------------------------------------------
// Data
// ---------------------------------------------------------------------------
async function fetchRecommend(token, { force = false, silent = false } = {}) {
  if (!token) {
    const overlay = $("#token-overlay");
    if (overlay) overlay.classList.remove("hidden");
    return null;
  }
  if (!silent) {
    const loading = $("#ki-loading");
    const content = $("#ki-content");
    const error = $("#ki-error");
    if (error) error.classList.add("hidden");
    if (content) content.classList.add("hidden");
    if (loading) loading.classList.remove("hidden");
  }
  const path = force
    ? "/api/v1/recommend?force=true"
    : "/api/v1/recommend";
  try {
    const payload = await apiGet(path, token);
    lastFetch = Date.now();
    renderPayload(payload);
    return payload;
  } catch (e) {
    if (e.status === 401) {
      const overlay = $("#token-overlay");
      if (overlay) overlay.classList.remove("hidden");
    } else if (e.status === 503) {
      showError(
        `Empfehlungen derzeit nicht verfügbar (Engine/CMI-Problem): ${e.detail || "HTTP 503"} — erneut versuchen:`,
        "err"
      );
    } else {
      showError(
        `Empfehlungen nicht abrufbar (HTTP ${e.status}): ${e.detail || e.message}`,
        "err"
      );
    }
    return null;
  }
}

// ---------------------------------------------------------------------------
// Auto-Refresh (5 min, nur wenn KI-Tab sichtbar)
// ---------------------------------------------------------------------------
function scheduleKiRefresh() {
  if (kiTimer) clearTimeout(kiTimer);
  kiTimer = setTimeout(tickKiRefresh, KI_REFRESH_MS);
}

function tickKiRefresh() {
  const token = getStoredToken();
  if (!token) {
    scheduleKiRefresh();
    return;
  }
  if (isKiVisible()) {
    fetchRecommend(token, { silent: true }).then(() => scheduleKiRefresh());
  } else {
    scheduleKiRefresh();
  }
}

function bindRefresh() {
  const btn = $("#ki-refresh-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const now = Date.now();
    if (now - lastForce < FORCE_COOLDOWN_MS) {
      const remain = Math.ceil((FORCE_COOLDOWN_MS - (now - lastForce)) / 1000);
      showError(
        `Zwangs-Neulauf wird max. alle 15 s ausgelöst — noch ${remain} s warten.`,
        "warn"
      );
      return;
    }
    lastForce = now;
    btn.disabled = true;
    btn.textContent = "läuft …";
    try {
      await fetchRecommend(getStoredToken(), { force: true });
    } finally {
      btn.disabled = false;
      btn.textContent = "Jetzt aktualisieren";
    }
  });

  // Beim Tab-Wechsel: sichtbarer KI-Tab + fälliger Fetch -> sofort (silent).
  const observer = new MutationObserver(() => {
    const token = getStoredToken();
    if (!token) return;
    if (isKiVisible() && Date.now() - lastFetch >= KI_REFRESH_MS) {
      fetchRecommend(token, { silent: true });
    }
  });
  const tabs = document.getElementById("tabs");
  if (tabs) observer.observe(tabs, { subtree: true, childList: true, characterData: true });
}

// ---------------------------------------------------------------------------
// Apply-Flow (S3-1 Two-Phase: Preview -> Dialog -> POST -> Read-back)
// ---------------------------------------------------------------------------
function applyError(message) {
  const el = $("#ki-apply-error");
  if (el) {
    el.textContent = message || "";
    el.classList.toggle("hidden", !message);
  }
}

function openApplyDialog({ parameter, adr, to, current, targetLabel }) {
  const overlay = $("#ki-apply-overlay");
  const what = $("#ki-apply-what");
  const params = $("#ki-apply-params");
  const input = $("#ki-apply-writetoken");
  const ok = $("#ki-apply-ok");
  if (!overlay) return;
  applyError("");
  if (ok) ok.classList.add("hidden");
  overlay.dataset.adr = adr;
  overlay.dataset.to = to;
  if (what) {
    what.textContent = `${parameter || "Empfehlung"}: ${adr} auf ${to} setzen`;
  }
  if (params) {
    const bits = [];
    bits.push(`<span class="chip chip-muted">Adresse <code>${esc(adr)}</code></span>`);
    if (current) bits.push(`<span class="chip chip-muted">Ist: ${esc(current)}</span>`);
    if (targetLabel) bits.push(`<span class="chip chip-ok">Ziel: ${esc(targetLabel)}</span>`);
    bits.push(`<span class="chip chip-warn">Write nötig (S3-1, HITL)</span>`);
    params.innerHTML = bits.join(" ");
  }
  if (input) input.value = getWriteToken();
  overlay.classList.remove("hidden");
  if (input) input.focus();
}

function closeApplyDialog() {
  const overlay = $("#ki-apply-overlay");
  if (overlay) overlay.classList.add("hidden");
}

async function runApply(adr, to, writeToken, { silent = false } = {}) {
  const readToken = getStoredToken();
  if (!writeToken) {
    applyError("Bitte den Write-Token (GATEWAY_WRITE_TOKEN) eingeben.");
    return null;
  }
  if (!readToken) {
    applyError("Read-Token fehlt — bitte oben (Token-Button) neu anmelden.");
    return null;
  }
  const now = Date.now();
  if (now - lastApply < APPLY_COOLDOWN_MS) {
    applyError("Bitte 10 s warten zwischen zwei Write-Versuchen (Doppelklick-Sperre).");
    return null;
  }
  lastApply = now;

  try {
    // Phase 1: Preview (Read-Token, KEIN CMI-Write) -> Confirmation-Hash
    const qs = new URLSearchParams({ adr, to }).toString();
    const preview = await apiGet(`/api/v1/change?${qs}`, readToken);
    const hash = preview?.confirmation_hash;
    if (!hash) throw new Error("Preview ohne confirmation_hash");

    // Phase 2: POST (Bearer Write-Token, 3 Felder)
    const res = await fetch("/api/v1/change", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${writeToken}`,
      },
      body: JSON.stringify({ adr, to, confirmation_hash: hash }),
    });
    const body = await res.json().catch(() => ({}));
    if (res.status === 200 && body.applied) {
      if (!silent) {
        setWriteToken(writeToken); // für den nächsten Apply merken
        closeApplyDialog();
      }
      return { ok: true, body };
    }
    if (res.status === 502) {
      applyError(
        `Read-back-Mismatch: erwartet ${to}, gelesen ${body?.readback?.value ?? "unbekannt"} — Wert NICHT bestätigt (Audit: ${body?.audit_id || "?"}).`
      );
      return { ok: false, status: 502, body };
    }
    if (res.status === 403) {
      applyError(body?.detail || "Write abgelehnt (403) — Hash/Token/Fenster prüfen, neu previewsen.");
      return { ok: false, status: 403, body };
    }
    applyError(`Write fehlgeschlagen (HTTP ${res.status}): ${body?.detail || body?.error || ""}`);
    return { ok: false, status: res.status, body };
  } catch (e) {
    if (e.status === 401) {
      applyError("Token ungültig/fehlt (401) — Read- + Write-Token prüfen.");
    } else {
      applyError(`Write fehlgeschlagen: ${e.detail || e.message}`);
    }
    return { ok: false, body: null };
  }
}

function bindApplyButtons() {
  const buttons = document.querySelectorAll(".ki-apply-btn");
  buttons.forEach((btn) => {
    if (btn.dataset.bound === "1") return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => {
      const adr = btn.dataset.adr || "";
      const to = btn.dataset.to || "";
      const parameter = btn.dataset.parameter || "";
      const rec =
        activePayload?.empfehlungen?.find(
          (r) => r && (r.adresse || "") === adr && String(r.to_value ?? "") === String(to)
        ) || null;
      openApplyDialog({
        parameter,
        adr,
        to,
        current: rec?.aktuell,
        targetLabel: rec?.vorgeschlagen,
      });
    });
  });
}

function bindApplyDialog() {
  const overlay = $("#ki-apply-overlay");
  const form = $("#ki-apply-form");
  const cancel = $("#ki-apply-cancel");
  const input = $("#ki-apply-writetoken");
  const confirm = $("#ki-apply-confirm");
  if (!overlay || !form || !confirm) return;

  cancel?.addEventListener("click", closeApplyDialog);
  overlay.addEventListener("click", (ev) => {
    if (ev.target === overlay) closeApplyDialog();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !overlay.classList.contains("hidden")) {
      closeApplyDialog();
    }
  });

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const adr = (overlay.dataset.adr || "").trim();
    const to = (overlay.dataset.to || "").trim();
    if (!adr || !to) {
      applyError("Ziel-Adresse/Wert nicht ermittelt — bitte neu laden und erneut anwenden.");
      return;
    }
    const writeToken = (input?.value || "").trim();
    confirm.disabled = true;
    confirm.textContent = "schreibe …";
    const result = await runApply(adr, to, writeToken, { silent: true });
    confirm.disabled = false;
    confirm.textContent = "Schreiben (S3-1)";
    if (result?.ok) {
      applyError("");
      const read = result.body?.readback?.value;
      const okEl = $("#ki-apply-ok");
      if (okEl) {
        okEl.textContent = `Erfolg: ${adr} = ${read} (Read-back bestätigt, Audit ${result.body?.audit_id || ""}).`;
        okEl.classList.remove("hidden");
        setTimeout(closeApplyDialog, 2500);
      }
      // Empfehlung sofort neu laden (frischer Ist-Zustand).
      fetchRecommend(getStoredToken(), { force: true });
    }
  });
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
function initKIPanel() {
  bindRefresh();
  bindApplyDialog();
  const token = getStoredToken();
  if (token) {
    fetchRecommend(token, { silent: !isTabActive() });
  }
  scheduleKiRefresh();
}

window.addEventListener("DOMContentLoaded", initKIPanel);

export {
  initKIPanel,
  fetchRecommend,
  runApply,
  getWriteToken,
  setWriteToken,
  // v0.5-UI: Render-Helfer (export für Node-Smoke/Tests; UI nutzt sie intern).
  // renderPayload ist der zentrale Render-Einstieg — direkt exportiert, damit
  // die v0.5-Logik deterministisch (ohne Fetch/Microtask-Timing) testbar ist.
  renderPayload,
  routeRec,
  dtDiagnosticsHtml,
  mindererCardHtml,
  pumpCardHtml,
  heatingCardHtml,
};
