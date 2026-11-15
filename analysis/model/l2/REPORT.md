# L2 v0.1 — Empfehlung (Advisory, offline)

- **Zeitpunkt:** 2026-11-01 12:00 (Europe/Berlin)
- **model_version:** `cmi-brain-v003+l2-v001` · Horizont 12 h (6-h-Zwischencheck)
- **stale:** `False`
- **Datenstand:** 2026-11-01 11:00:00 (data_version m1.3, Zustand 2026-11-01 11:00:00, Metrik-Lücke 1.0 h)
- **Wetter:** Open-Meteo ICON-D2 (live), Run 2026-11-01 18:40 UTC, Alter 0.04 h
- **Konfidenz (Basis):** 0.125

## Top-3-Empfehlungen je Zone (Tier A)

### RAS-Offset kell — ➜ ändern

- aktuell: **+3.5 K** → vorgeschlagen: **+2.5 K**
- Konfidenz: 0.125 · Executability: `pending-cmi-write-discovery` (Q-107 (RAS-Offset-Write, pending UVR1611-Programmupload))
- Begründung: Perzipiert 25.3 °C (Soll 20 °C, Abw. 5.3 K); Offset +3.5 → +2.5 K reduziert die 12-h-Band-Überschreitung (Komfort 297.8 → 192.2, Energie-Proxy 0.10 → 0.10, Gesamtkosten 297.84 → 192.43). Außer-T-Prognose 12 h: Minimum +7.6 °C (ICON-D2, Run 2026-11-01 18:40 UTC). Tier A (stündlich, primär); Ausführung am RAS-Gerät/UVR1611, Advisory.

### RAS-Offset eg — ● beibehalten

- aktuell: **-1.8 K** → vorgeschlagen: **-1.8 K**
- Konfidenz: 0.125 · Executability: `pending-cmi-write-discovery` (Q-107 (RAS-Offset-Write, pending UVR1611-Programmupload))
- Begründung: Perzipiert 20.1 °C (Soll 20 °C, Abw. 0.1 K); keine kostengünstigere Offset-Änderung gefunden (Basis-Komfort 297.8, No-change-Schwelle 0.15). Außer-T-Prognose 12 h: Minimum +7.6 °C (ICON-D2, Run 2026-11-01 18:40 UTC).

### RAS-Offset dg — ● beibehalten

- aktuell: **+1.4 K** → vorgeschlagen: **+1.4 K**
- Konfidenz: 0.125 · Executability: `pending-cmi-write-discovery` (Q-107 (RAS-Offset-Write, pending UVR1611-Programmupload))
- Begründung: Perzipiert 20.5 °C (Soll 20 °C, Abw. 0.5 K); keine kostengünstigere Offset-Änderung gefunden (Basis-Komfort 297.8, No-change-Schwelle 0.15). Außer-T-Prognose 12 h: Minimum +7.6 °C (ICON-D2, Run 2026-11-01 18:40 UTC).

## Tages-Advisory (Tier B, regelnbasiert)

- **Heizkurve/VL-Soll:** Heizkurven-Absatz −1 Raster (≈ −0,5–1 K VL-Soll) (Konfidenz 0.5, `TA-Designer-Parameter (Heizkurven-Absatz), v2`)

## Guardrail-Hits (hart, vor Ausgabe)

- solar: Kollektor 20 °C < 25 °C → keine Solar-Hand-EIN

## Kosten-Abbildung (Top-Kandidaten)

| Kandidat | Komfort | Energie | Change | Total |
|---|---|---|---|---|
| kell:-1.0+eg:+0.0+dg:+0.0 | 192.2246 | 0.1029 | 0.5 | 192.426 |
| kell:-0.5+eg:-0.5+dg:-0.5 | 240.5197 | 0.1397 | 0.75 | 240.8146 |
| kell:-0.5+eg:-0.5+dg:+0.0 | 241.1174 | 0.1273 | 0.5 | 241.331 |
| kell:-0.5+eg:+0.0+dg:-0.5 | 241.3275 | 0.1231 | 0.5 | 241.539 |
| kell:-0.5+eg:+0.0+dg:+0.0 | 241.8702 | 0.1019 | 0.25 | 241.9961 |
| kell:-0.5+eg:+0.5+dg:-0.5 | 242.4989 | 0.1095 | 0.75 | 242.7787 |
| kell:-0.5+eg:+0.5+dg:+0.0 | 243.1066 | 0.0697 | 0.5 | 243.2915 |
| kell:-0.5+eg:-0.5+dg:+0.5 | 244.783 | 0.1242 | 0.75 | 245.0701 |
| kell:-0.5+eg:+0.0+dg:+0.5 | 245.5246 | 0.0991 | 0.5 | 245.7241 |
| kell:-0.5+eg:+0.5+dg:+0.5 | 246.7201 | 0.0676 | 0.75 | 246.9789 |

- Kandidaten: 30 nach Pruning (von 343)

## Executability-Map (v0.1: alle pending)

- Alle Empfehlungen: `pending-cmi-write-discovery` — CMI-Write-Pfad wird separat abgeglichen (Orchestrator).
  - `offset_kell`: Q-107 (RAS-Offset-Write, pending UVR1611-Programmupload)
  - `offset_eg`: Q-107 (RAS-Offset-Write, pending UVR1611-Programmupload)
  - `offset_dg`: Q-107 (RAS-Offset-Write, pending UVR1611-Programmupload)
  - `vl_curve`: TA-Designer-Parameter (Heizkurven-Absatz), v2
  - `ww_window`: Zeitprogramm-Parameter (WW-Ladefenster)
  - `circulation`: UVR-Programmier-Aufgabe (nach Nutzungsverhalten statt Puffer-geführt; kein Live-Parameter)

## Quellen

- JSON: `<repo-root>/analysis/model/l2/runs/recommend_2026-11-01_1200.json`
- L1: `<repo-root>/models/cmi-brain/v003` (cmi-brain-v003, READ-ONLY)
- Feature-Store: `<repo-root>/data/features` (m1.3, READ-ONLY)
- Wetter: Open-Meteo ICON-D2 LIVE (non-commercial) + M1-Bias-Profil (dev0 a8)

