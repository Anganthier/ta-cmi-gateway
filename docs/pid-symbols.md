# P&ID-Symbol-Bibliothek — CMI-UI (Hydraulik-Loops)

> **Zweck:** Wiederverwendbare Standard-Symbole für die 5 animierten Loop-Views
> (A=Holz, B=Öl, C=Solar, D=WW, E=Heizung). Basis: **DIN EN ISO 10628-1**
> (P&ID-Symbolik) + branchenübliche Haustechnik-Symbole. **Keine erfundenen/
> simplifizierten Symbole** — jedes Symbol muss auf einen Blick erkennbar sein.

**File:** `ui/svg/pid-symbols.svg` — `<defs>`-Katalog, 18 Symbole als
`<g id="pid-...">`, jeweils lokal um `(0,0)` zentriert.

## Verwendung

```html
<svg ...>
  <use href="#pid-pump-cent" x="400" y="200"/>
  <use href="#pid-valve-3way" x="600" y="200" class="pid-ctl"/>
</svg>
```

- **Farbprofil per CSS** (Symbole sind `currentColor`-basiert, `stroke-width` 1.5–2, `fill:none`):

  | CSS-Klasse | Farbe | Bedeutung |
  |---|---|---|
  | `.pid-vl` | `#f59e0b` (amber) | VL / Vorlauf (heiß) |
  | `.pid-rl` | `#4da3ff` (blau) | RL / Rücklauf (kühl) |
  | `.pid-ww` | `#38bdf8` (cyan) | WW / Warmwasser |
  | `.pid-gas` | `#94a3b8` (grau) | Abgas |
  | `.pid-ctl` | `#a78bfa` (lila) | Steuer / Regelung |

- **Anschlusspunkte:** `±x` = Prozess-Leitung, `−y` = oben (Stiel/Flue/Probe/
  Abgas). Symbole haben **keine** eigenen Labels — Labels setzt der Aufrufer.
  Technische Kennziffern (`Q`, `T`, `F`, `0–10 V`) sind P&ID-üblich und Teil
  des Symbols.
- **Rendering-Prüfung:** `<g id="pid-demo">` am Ende des SVG zeigt alle 18
  Symbole per `<use>` (6×3-Raster + Farbdemo).

## Symbol-Katalog

| ID | Symbol | P&ID-Norm / Technik-Name | Wo in der Anlage |
|---|---|---|---|
| `pid-pump-cent` | Kreiselpumpe (Kreis + Saug-/Druckseite + Impeller-Pfeil + „Q") | ISO 10628-1 Pumpsymbol (Kreis mit Pfeil), Kennziffer Q | O1 (A), O2/O7 (D), O6 (B/D), O8 (E), SV (C) |
| `pid-valve-2way` | 2-Wege-Ventil (Sattel/Schmetterling + Stiel + Stielett) | ISO 10628-1 A/Z- und Mischventil, 2-Wege | O9 (E → H2/P) |
| `pid-valve-3way` | 3-Wege-Ventil (horizontaler Sattel + senkrechter Schenkel) | ISO 10628-1 3-Wege-Ventil (Mischer) | O3/O4 Mischkreis (E) |
| `pid-valve-actuator` | Ventil mit Aktor (Kasten, „0–10 V"-Kennz.) | ISO 10628-1 Betätigungselement über Ventil | generisch: 0–10 V-angesteuerte Ventile |
| `pid-valve-globe` | Kugelhahn/Spindelventil (Sattel + Handhebel/Kippstiel) | ISO 10628-1 Absperren (Handbetätigung) | O9-Entscheidung (absperrende Variante) |
| `pid-heatex-shell` | Rohrbund-/Rohrwürfel-Waermetauscher (zwei Kreise mit Diagonale) | ISO 10628-1 Rohrbund-HE (zwei Konuspaare + Diagonalen) | P-Spirale Solar (C → P unten), P → WW |
| `pid-heatex-plate` | Platten-Waermetauscher (zwei S-Stränge über Trennlinie) | ISO 10628-1 Platten-HE | WW-Waermetauscher (P oben → KW), ggf. weitere HE-Stellen |
| `pid-tank-buffer` | Pufferspeicher (Behälter, Zonen-Scheiden, Füllstand gestrichelt) | Haustechnik-Standard: Speicher mit Zonen | P (zentral, 3 Zonen + ganz oben/unten) |
| `pid-boiler-oil` | Ölkessel mit Brenner (Kessel + Brennereinheit + Öl-Zufuhr) | Haustechnik-Standard: Kessel + Flammrohr-Brenner | H2 (Ölheizung, Oelbrenner R1, O5) |
| `pid-boiler-solid` | Festbrennstoff-/Holzkessel (Kessel + Feuerungsraum + Holz) | Haustechnik-Standard: Holzofen mit Wassertank | H1 (Holzofen, O1) |
| `pid-collector-solar` | Solar-Kollektor (Rohr-Kollektor mit Schlauchspirale) | Haustechnik-Standard: Rohr-Kollektor | H3 (Dach, SV, a16) |
| `pid-pt` | PT-Fühler (Messkreis „T" + Mess-Element/Pin) | ISO 10628-1 Temperaturmessung (T) | a1–a10, a16 (alle Loops, E2) |
| `pid-flow-switch` | Stromungsschalter (Kreis „F" + Schwimmer-Punkt) | P&ID-Fluss-Schalter (F), binäre Auswertung | a16 WW-Zirkulation (D) |
| `pid-radiator` | Heizkoerper (Rippen-Box, horizontaler Anschluss) | Haustechnik-Standard-Radiator | R1/R2/R3 (E, Keller/EG/DG) |
| `pid-throttle` | Drossel-/Rücklaufminderer (Sattel + Kegel + 0–10 V-Aktor) | P&ID-Drossel/Ventil mit Kegel + Durchflusskennlinie | RM1/RM2/RM3 (E, je Einrohr-Kreis) |
| `pid-chimney` | Schornstein/Abgas (Kamin + Pfeil nach oben) | P&ID-Abgasführung (Aufwärts-Kennzeichnung) | E1 (Abgas H1, a10) |
| `pid-tap` | Zapfstelle/Entnahmepunkt (Kreis + nach aussen nach unten) | P&ID-Entnahmepunkt (Probe/Abzapfen) | WW-Zapfstellen (D) |
| `pid-mixing` | Mischer-Knoten (P&ID-Verbindungspunkt + Wirbel-Pfeil) | P&ID-Kreuzung mit Verbindungspunkt | zentrale RL-Mischung vor O9 (E) |

## Design-Entscheidungen (Kurzfassung)

- **P&ID-Standard-Formen statt geometrischer Vereinfachung:** Sattel-
  (Schmetterlings-)Körper für Ventile nach ISO 10628-1, Kreis mit Impeller-
  Pfeil + „Q" für Kreiselpumpen, zwei Kreise mit Diagonalen für Rohrbund-HE.
- **`currentColor` + `fill:none`** (Stroke 1.5–2): UI färbt Symbole pro
  Loop-View ein (VL amber / RL blau / WW cyan / Abgas grau / Steuer lila),
  wie die bestehende Legende in `docs/hydraulic-schematic.svg`.
- **Technische Kennziffern bleiben im Symbol** (`Q`, `T`, `F`, `0–10 V`),
  da sie P&ID-üblich sind; alle Labels (z. B. „O8", „a7") setzt der Aufrufer.
- **Wiederholung des `<use href="#sym-pump" x=… y=…>`-Musters** aus
  `docs/hydraulic-schematic.svg` — gleiche Konvention: `x`/`y` = Zentrum.

## Quellen

- **DIN EN ISO 10628-1** — P&ID (Plant Layout and Piping/Instrumentation
  Diagrams), Symbolik für Pumpen, Ventile, Waermetauscher, Messkreise.
- **Branchenübliche Haustechnik-Symbole** — Pufferspeicher, Kessel (Öl/Holz),
  Solar-Rohr-Kollektor, Heizkoerper, Schornstein, Rücklaufminderer.
- Referenz-Pattern: `docs/hydraulic-schematic.svg` (`<defs>`/`<use>`-Katalog).
