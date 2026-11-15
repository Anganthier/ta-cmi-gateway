# UI-Architektur: „Anlage"-Sektion — **Loop-zentrisch** (v2.1, FREIGEBEN 2026-10-29 R7)

> **Status: FREIGEBEN (maintainer, 2026-10-29 R7):** Q-10…Q-14 beantwortet; Q-15 offengehalten
> (V-G = die geplante `[ Gesamt ]`-Pill, existiert bisher nur als `docs/hydraulic-schematic.svg`).
> **Neuer globaler Grundsatz (maintainer R7): Standard-Symbole aus dem technischen Umfeld
> (P&ID / DIN EN ISO 10628), KEINE erfundenen Symbole.**
>
> **Auslöser (maintainer, R6):** „Die einzelnen Loops sind sehr wichtig — in der Anzeige der Loops
> will ich auch historische Werte über einen kurzen Zeitraum (24 h) sehen. D. h. die
> Darstellung der Loops wird hinter sehr wichtig. Die Gesamtdarstellung braucht **keine
> Livedaten** — sie dient eher als Dokumentation."
>
> **→ Design-Kern (v2): Loop-zentrische UI.** Jeder Loop (A–E) ist eine **eigene Ansicht**
> mit **Live-Status + 24h-Verlauf**. Die Gesamtdarstellung (V-G) ist **statische
> Referenz/Dokumentation** (zoombar, ohne Live-Daten/Animation).
>
> **Basis:** `docs/hydraulic-schematic.svg` V6.3 (IDs), `docs/hydraulic-loops-rules.md`
> (Kanon-Regelwerk R-A1 … R-X8), `/api/v1/state` (30 s) + `/api/v1/history`
> (device/channel, 24 h), `ui/js/winsol-labels.js` (Kanal-Mappings), aktuelle `ui/`
> (Dashboard + 5 CMI-PNG-Tab + Historie).

---

## 1. Prinzipien (kurz)

1. **1 Loop = 1 Ansicht = 1 Frage.** Die „Anlage"-Sektion zeigt immer **genau einen Loop**
   (oder die Gesamt-Referenz) — nie alle 5 gleichzeitig.
2. **Jeder Loop-View = Live-Status ODER 24h-Verlauf, beides auf einer Karte** (nicht zwei
   getrennte Seiten): oben kompakter Live-Zustand des Loops, darunter 24h-Chart der
   wichtigsten Sensoren des Loops.
3. **Progressive Disclosure:** Details (Setz-Adressen, alle Kanäle) per Klick/Zoom —
   Default = nur die 3–5 Kernwerte + 1 Chart.
4. **Gesamtdarstellung = Doku, kein Live:** statisches SVG (V6.3), Pan/Zoom, optional
   Readouts **ohne** Auto-Refresh (oder „aktualisieren" per Klick). Kein Flow-Animation.
5. **Animation (Freigabe R7: VOLLE Animation):** in den Loop-Views: Wasser-Flow
   (pipe-dashoffset), **Pumpen-Rotation** (O1/O2/O6/O7/O8/SV), **Ventile öffnen/schließen**
   (Mischer O3/O4, O9), Kessel-aktiv-State. Globaler Schalter „Animation an/aus"
   (Default: an). V-G (Gesamtdarstellung): **keine** Animation (Doku).
6. **Standard-Symbole (Freigabe R7, global):** P&ID-Symbole nach **DIN EN ISO 10628-1**
   (Process Flow Diagrams) + branchenübliche Symbole der Haustechnik (Solarthermie).
   **Keine erfundenen Symbole.** Symbol-Bibliothek einmalig bauen (`ui/svg/pid-symbols.svg`
   als `<defs>`-Set, Ticket S4N-0) und in ALLEN Views wiederverwenden:
   - Kreiselpumpe: P&ID-Kreiselpumpensymbol (Kreis + Laufrad-Säulen, Saug-/Druckseite)
   - 2-Wege-Ventil: P&ID-Ventil (doppelt-dreieckig, Stiel + Aktor); 3-Wege-Ventil (Mischer/O9)
   - Waermetauscher: P&ID-Rohrbund-/Platten-Waermetauscher (Kreis + Diagonale / Doppelkreis)
   - Pufferspeicher: Speichersymbol (vertikal, Zonen-Scheide, Füllstand)
   - Öl-/Holzkessel: Kesselsymbol mit Brenner-/Festbrennstoff-Zeichen (Flamme)
   - Solar-Kollektor: branchenübliches Kollektor-Panel-Symbol (Solarthermie-DIN)
   - PT-Fühler (a1…a16): T-Symbol (Kreis + T / Fühler-Linie)
   - Stromungsschalter (a16 dev0): Fluss-Schalter-Symbol (Kegel + Stab)
   - Heizkoerper: Standard-Radiatorensymbol
   - Rücklaufminderer (RM1/2/3): Drossel-/Ventilsymbol mit 0–10 V-Aktor-Kennung
   - Abgas/Schornstein: P&ID-Abgassymbol; Zapfstelle: Entnahmepunkt-Symbol
6. **Mobile (375 px):** Loop-View stapelt Live-Status → Chart; Gesamt = nur Pan/Zoom.
7. **Farbsprache konstant** (VL amber / RL blau / WW cyan / Abgas grau / C gestrichelt).

---

## 2. Navigations-Modell (v2)

```
Top-Nav (3 Tabs, unverändert):
  [Start (Dashboard)]   [Anlage ★]   [Historie]
                            │
                            │ Innerhalb „Anlage": Loop-Selektor (Pills, nicht weiter-Tabs):
                            │  ( A Holz ) ( B Öl ) ( C Solar ) ( D WW ) ( E Heizung ) [ Gesamt ]
                            │
                            │ Default-Pill: **E Heizung** (Freigabe Q-11 ✅)
                            │
                            │ Inhalt-Bereich:
                            │   • Pill A–E  → Loop-View: [Live-Status des Loops]
                            │                                          + [24h-Chart der Loop-Sensoren]
                            │                                          + [Steuer-Overlay, s. §5 S4N-2b]
                            │   • Pill „Gesamt" → V-G: statisches SVG V6.3 (Pan/Zoom, Doku)
```

- **Alt-Tab 2–5 (CMI-PNG) werden ENTFERNT** (Freigabe Q-10 ✅ „Die alten können weg").
- **Loop-Selektor = 6 Pills** (A·B·C·D·E + Gesamt) — keine kognitive Last, ein Klick.

---

## 3. Die Views (v2)

### 3.1 Loop-View (A–E) — **das Zentrum** (5 Variationen, gleiche Struktur)

Jede Loop-View folgt **einem Layout** (konsistent, lernbar):

```
┌──────────────────────────────────────────────────────────────┐
│  Loop E · Heizung            [Modus-Badge: Heizbetrieb]       │
├──────────────────────────────────────────────────────────────┤
│  ┌─ Live-Status (kompakt, ~5–8 Elemente) ──────────────────┐ │
│  │  H2 Öl ──► Mischer ─[O8]─► VL a6 ─► R1 R2 R3            │ │
│  │   (O5⚠)      (O3/O4)              │  RL a7 ◄─[RM]       │ │
│  │   Flow-Animation: aktiv=Farbe, inaktiv=grau             │ │
│  │   Live-Werte: VL a6 · RL a7 · RAS Keller/EG/DG          │ │
│  └─────────────────────────────────────────────────────────┘ │
├──────────────────────────────────────────────────────────────┤
│  24 h Verläufe (je Loop die relevanten Sensoren, max 3–4):    │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  [Chart.js]  VL a6 ── RL a7 ── RAS Keller/EG/DG        │ │
│  │  (Preset 24 h, Auto-Interval; Zoom-optional)           │ │
│  └────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

**Jeder Loop-View = Live-Block + 24h-Chart**, gleiche Höhe, eine Scroll-Karte.
Klick auf ein Live-Element → Detail-Tooltip (Wert + Adresse + „in Historie öffnen").

#### Loop → 24h-Kanäle (aus `winsol-labels.js` / `/api/v1/history`)

| Loop | Live-Status (Kern) | 24h-Chart-Serien (device:channel) |
|---|---|---|
| **A · Holz** | H1 (O1), P-oben/unten, Abgas | a9 Holz-Tank (winsol:0 ch9) · a10 Abgas (winsol:0 ch10) |
| **B · Öl** | H2 (O5⚠), VL a5, Modus | a5 Ölkessel-VL (winsol:0 ch5) · P-Mitte a2 (winsol:0 ch2) |
| **C · Solar** | H3 (SV, d1), Spirale P-unten, Kollektor a16 | a16 Kollektor (winsol:2 ch16) · P-Unten a3 (winsol:0 ch3) · [d1 Solarpumpe] |
| **D · WW** | WW-Box (a4), Waermetauscher, Zirk (O7) | a4 WW (winsol:0 ch4) · a16 WW-Zirk-SS (winsol:0 ch16) |
| **E · Heizung** | Mischer, O8, VL a6, 3 Kreise, O9 | a6 VL (winsol:0 ch6) · a7 RL (winsol:0 ch7) · RAS Keller/EG/DG (winsol:1 ch2/5/8) |
| **Gesamt** | — | (kein Live/Chart — statische Doku, s. 3.2) |

> **Seriengrenze: „maximal" (Freigabe Q-12 ✅)** — alle relevanten Kanäle des Loops in
> **einem Chart** (mehre Reihen, Chart.js-Legende pro Serie, eigene Linienfarben).
> Digitale Kanäle (d1, SS a16) als **Sprunglinien** (ad=D), analog als Kurven (ad=A).

### 3.2 V-G · Gesamtdarstellung — **statische Doku** (kein Live)

- SVG **V6.3 1:1**, **Pan/Zoom** (Maus/Pinch, „Anpassen"-Button).
- **Kein Auto-Refresh, keine Animation.** Readouts (19 Boxen) bleiben als
  **Platzhalter/Doku** (oder optional „einmal aktualisieren" per Klick — Q-15).
- Zweck: **Referenz/Dokumentation** („wie hängt das alles zusammen?"), z. B. als
  gedrucktes Sheet oder für Einweisungen.
- Mobile: nicht Default; „Gesamt"-Pill, Initial = „Anpassen".

---

## 4. Daten & State (aus bestehenden APIs, minimal neue Logik)

```
/api/v1/state (30 s)     ──►  state-map.js (NEU)
        · readouts: a1..a16, dev2.*  →  Loop-View Live-Status (Kernwerte + Modus + O-Pumpen)
        · loopStates: {A..E: {active, pumps[], valves[]}}  →  Animation (aktiv/inaktiv)
        · demo: CMI down → synth. Werte + Badge „Demo-Daten"

/api/v1/history (on-demand) ──►  loop-history.js (NEU, dünne Schale über history.js)
        · je Loop-View: 24 h, device:channel je 3.1 (Preset 24 h)
        · wiederverwendet: ui/js/history.js (Query/Chart.js/Zoom-Logik)
```
- **Kein neuer Poller** — nur die bestehenden Endpunkte; `loop-history.js` rendert
  je Loop die 24h-Serien und **recycelt** `history.js` (Query + Chart-Setup).
- **Modus→Aktivität** aus Kanon-Regelwerk R-X4 (konservativ: „Heizung an" = E aktiv,
  „WW-Modus" = O6-Pfad aktiv; Rest aus O/Pumpen-Status).
- **Kein CMI-Write** in dieser Phase (Steuerung = S4-2, Q-14).

---

## 5. Migration & Tickets (v2)

| **S4N-0** | **P&ID-Symbol-Bibliothek** (VORAUSSETZUNG, maintainer R7: Standard-Symbole): `ui/svg/pid-symbols.svg` als `<defs>`-Set nach **DIN EN ISO 10628-1** + Haustechnik-Übung (Pumpen, 2/3-Wege-Ventile, Waermetauscher, Speicher, Kessel, Kollektor, PT, Stromungsschalter, Radiator, Drossel, Abgas) + kurze Doku `docs/pid-symbols.md` (Symbol→Norm-Kennzeichen) | Alle ~12 Symbole als sauber gezeichnete SVG-Gruppen; Review maintainer: „sieht nach Standard-Symbol aus"; keine erfundenen Formen |
| **S4N-2** | **Loop-Views A–E**: je Loop = Live-Status (Inline-SVG aus S4N-0-Symbolen, Voll-Animation Q-13 ✅: Flow + Pumpen-Rotation + Ventile) + 24h-Chart (history.js-Recycle, **alle relevanten Serien in 1 Chart** Q-12 ✅) + Loop-Selektor (Pills, Default E) + Modus-Badge + Demo-Mode | 5 Loops korrekt, Voll-Animation, 24h-Chart pro Loop flüssig (<1 s), node --check + pytest ≥80 %, Screenshot Desktop+375 px |
| **S4N-3** | **V-G Gesamtdarstellung (Pill „Gesamt")**: SVG V6.3 mit S4N-0-Symbolen, Pan/Zoom (ViewBox), **ohne** Live/Animation (Doku); „einmal aktualisieren" je Q-15 | Pan/Zoom >50 fps, Mobile = Anpassen, kein Auto-Refresh |
| **S4N-4** | Navigation umstellen (3 Tabs + Pills), **Alt-Tab 2–5 (PNG) entfernen** (Q-10 ✅), Loop-Default E, Animation-Schalter | Tab/Pill-Wechsel <100 ms, PNG-Assets + schematics.js entfernt, Lighthouse ok |
| **S4N-2b** | (später, mit S4-2) **Steuerung-Overlay IN JEDEM LOOP** (Q-14 ✅, kein separater Tab): Modus-Select + Schalter + Bestätigungs-Dialog je Loop-View | = S4-2-DoD (E2E 2 freigegebene Changes) |

**Reihenfolge:** S4N-0 (Symbole) → S4N-2 (Loop-Views, Zentrum) → S4N-3 (V-G Doku) → S4N-4 (Nav + PNG-Entfernung).
S4N-0 ist die Voraussetzung für saubere Standard-Symbole in S4N-2/3.

---

## 6. Fragen (R7 2026-10-29) — **Q-10 … Q-14 BEANTWORTET, Q-15 offengehalten**

- **Q-10 ✅ (BEANTWORTET):** Alt-Tab 2–5 (CMI-PNG) **weg** („Die alten können weg").
- **Q-11 ✅ (BEANTWORTET):** Default-Pill = **E Heizung** („Ja").
- **Q-12 ✅ (BEANTWORTET):** **Maximal**, alle relevanten Kanäle in **einem** Chart
  („Ggf. auch mehrere Reihen in einen Graphen").
- **Q-13 ✅ (BEANTWORTET):** Animation = **Voll** (Flow + Pumpen-Rotation + Ventile).
- **Q-14 ✅ (BEANTWORTET):** Steuerung **in jedem Loop selber** (Overlay je Loop-View),
  **kein separater Tab**.
- **Q-15 ⬜ (offengehalten):** Gesamtdarstellung rein statisch oder „einmal aktualisieren"?
  (maintainer: „V-G, finde ich nicht?!?" → V-G = die geplante `[ Gesamt ]`-Pill aus S4N-3,
  existiert bisher nur als `docs/hydraulic-schematic.svg`, noch nicht in der UI.)