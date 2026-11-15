# S6-AI-Design — KI-Modell für stündliche Heizungssteuerung (Draft v0.3)

Status: **ENTWURF v0.3 — Horizont-Korrektur + RAS-Offset-Semantik nach maintainer-Feedback (2026-10-31, zweite Runde)**.
Reines Planungs-/Design-Dokument, **kein Code**. Parallel-Task: andere Session arbeitet an UI/App.
v0.3 ersetzt v0.2 (v0.2-Fehler: 1-h-Entscheidungs-Horizont für ein trüges Haus zu knapp; RAS-Offset-Kanäle dev1 a1/a4/a7 als „Soll" fehlinterpretiert — sie sind **Perzipiert-Temperatur = Ist + Offset**, und der **Offset selbst ist die Stellgröße**).
Quellen: `docs/plant-model.md`/`.json`, `docs/hydraulic-topology.md`, `docs/hydraulic-loops-rules.md`, `data/winsol_pq/`, `data/cmi_log_pq/`, `CMI_WebTest/CMI-WEB/CMI-WEB.xml`, TA-Manual UVR1611 (`ta.co.at/fileadmin/Downloads/Betriebsanleitungen/.../Manual_UVR1611_A4.03-2.pdf`).

---

## 1. Problemstellung (geklärte Faktenlage 2026-10-31, Stand nach 2. Feedback-Runde)

> **Ein Keras-Modell, trainiert auf der Metrik-DB, das jede Stunde die Parameter der Heizungssteuerung so empfiehlt, dass aktuelle Situation + aktuelles Wetter + 24-h-Vorhersage optimal berücksichtigt sind. Entscheidungs-Horizont: 6–12 h (Haus ist thermisch träge).**

**Zentrale Befunde (Daten-Abgleich 2026-10-31, 2 Runden):**

| # | Befund | Evidenz | Konsequenz |
|---|---|---|---|
| 1 | **dev1 a1/a4/a7 = Perzipiert-Raumtemperatur (Ist + Offset), nicht Soll.** Der Regler „vorgegaukelt“ der Regelung eine andere Temperatur → schaltet den Heizkreis ab, wenn perzipiert > Soll-RAS (fest, vom RAS-Modul gesetzt). | **18.10-15:30-Ereignis (winsol_pq/2026-10, Keller):** 15:00–15:33 a2(Ist)≈20,5–21,3, a1(perzipiert)≈20,0 (Offset −0,9 K), VL fällt 42→34. **15:34–15:43 Offset-Step auf +3,4 K → a1 springt 21,2→24,0**, a2 bleibt 20,5. VL steigt 34→52. **16:05 d8 (Pumpe Heizkreis Keller) 1→0** (Heizkreis Keller aus) — die Regelung „sieht“ 24–25 °C = zu warm. 16:32 d8 kurz zurück (Raschel/Schaltdämpfung), 17:45 erneut 0, abends d8≈0 (min/h Keller fällt 0,6→0,1–0,5). | a1/a4/a7 = **Kontext** für L1 (worauf die CMI-Regelung reagiert) + **Offset Δ(a1−a2) je Zone = Stellgröße / Entscheidungs-Variable für L2** (v1: read-only + Advisory; setzbar = Q-107). plant-model-Label „Offset [K]“ = korrektes Label, meine v0.2-Interpretation „Soll“ war falsch. |
| 2 | **RAS-Doppelt: dev1 (UVR1611, Node 1) = volle 10-s-Auflösung = kanonisch.** dev2 (UVR610, Node 32) erhält RAS-Werte nur alle 5 min per CAN-Transfer (geringere Auflösung). | maintainer-Statement 2026-10-31; Daten: dev1 a2/a5/a8 ≡ dev2 a5/a7/a9 (Werte deckungsgleich, nur Takt unterscheidet sich). | **Training nur auf dev1** (RAS). dev2 = Konsistenz-Check (Divergenz-Alarm > 1 K = CAN-Transfer-Problem). |
| 3 | **Haus ist thermisch träge → 1-h-Horizont zu knapp.** Vorlauf-/Puffer-/Raum-Trajektorien entwickeln sich über Stunden; eine 1-h-Entscheidung optimiert den falschen Kausalhorizont. | maintainer-Statement 2026-10-31 („Das Haus ist der träge. 1 h als Zukunftszeitraum ist hier zu knapp — mach daraus eher 6 bis 12 h“). | **Entscheidungs-Horizont H = 6–12 h** (v1-Default **H = 12 h**): L2 optimiert Kosten über den 12-h-Horizont (6-h-Zwischencheck), Wetter-Fenster = 24 h (Lade-Strategie: Puffer für die kalte Nacht vorgefillen). L1 bleibt 1-h-Step (physikalisch korrekt), Rollouts laufen H-Steps. |
| 4 | **Vorlaufsolltemperatur (CAN 12360 sub1) ist erst anpassbar, wenn ein neues UVR1611-Programm geladen wird (später).** Aktuell: Heizkurve (Grundregelung: a8 Außen-T → Heizkurve → VL-Soll, R-X5) wird per **Parameter** (Heizkurven-Neigung/-Absatz) eingestellt — alternativ **statischer VL-Wert**. | maintainer-Statement 2026-10-31 („Kann anpassbar gemacht werden, dann muss ich aber ein neues Programm in die UVR1611 hochladen, später. Aktuell stelle ich die Heizkurve über Parameter ein, aber ich kann auch einen statischen Wert vorgeben. Siehe Doku Heizungsregler UVR1611“); `docs/hydraulic-loops-rules.md` R-X5; TA-Manual UVR1611 (Modul HEIZKREISREGELUNG: T.vorl.Soll; Eingangsvariable = Außentemperatur + Heizkurve). | **v1-Aktionsraum:** RAS-Offsets (advisory), 9 Whitelist-Schalter, **Heizkurven-Parameter** (Neigung/Absatz, falls CAN-setzbar — Prüf-Task Q-108) + statischer-Modus-Advisory. **Vorlaufsoll = v2** (nach UVR1611-Programmupload) → dann stärkster Hebel. |
| 5 | RAS-Ist-Temperatur doppelt geloggt, Heizlast-Proxy vorhanden. | dev1 a2/a5/a8 ≡ dev2 a5/a7/a9 (s. #2). ~~dev1 a11–a13 = Heiz-Minuten je Zone~~ **Korrektur 2026-10-31 (maintainer): a11–a13 = Sägezahn „Zeit seit letzter Datenübertragung der RAS-Elemente“ (Datenalter, Reset ≈ 11 min), keine Heiz-Minuten.** RAS-Sensoren erst ab Okt. 2021 verbaut. | Konsolidierung auf dev1; a11–a13 → Feature `ras_age_*` (Datenqualität/Stale-Maske), **kein Target** (M1 `m1.1`, M2b). |
| 6 | Betriebsmodus + Vorlaufsoll sind NICHT in der Historie. | weder im Log (nur Digital-States) noch im Feature-Store. | Parameter-Logging ab jetzt (Q-102, **BEANTWORTET: Ja — so machen**): Schematic-Poller 30–60 s read-only → Betriebsmodus + (neu) **RAS-Offset-Kanäle dev1 a1/a4/a7** + Heizkurven-Parameter in `history.db`/Feature-Store. |

**Grenzen (verbindlich):** Modell **empfiehlt nur**; Ausführung über S3-1 (Whitelist + Read-back + Audit + Bestätigungs-Dialog). **Kein Auto-Write in v1.** LLM (DGX-SGLang) bleibt Interface-/Erklärungsschicht.

---

## 2. Architektur — die Kernidee (v0.3)

### 2.1 Die drei Kern-Korrekturen gegenüber v0.2

1. **Horizont 1 h → 6–12 h (Default 12 h).** Der Optimizer bewertet jede Kandidatenaktion über 12 h (Wetter-Fenster 24 h). Grund: träge Haus — Puffer, Vorlauf, RAS reagieren mit Stunden-Verzögerung. Eine 1-h-Optimierung „heizt kurz vor Kälte“ statt „lädt den Puffer, während es noch mild ist“.
2. **RAS-Offset wird zur Stellgröße.** a1/a4/a7 sind Perzipiert-Temp (Ist + Offset). Der **Offset** (Δ = a1 − a2 je Zone) ist das, was der Nutzer/Regler „dreht“: höheres Offset → Regelung sieht wärmer → Heizkreis fährt ab (Nachweis 18.10-15:30). Damit ist der Offset **exakt die Art von Parameter, die das KI-Modell stündlich an das Wetter anpassen soll** (z. B. Keller-Offset am Abend senken, weil Keller-RL-Trägheit + Puffer reicht; DG-Offset bei Frost-Nacht erhöhen, weil DG-Abkühlung am stärksten).
3. **dev1 = kanonische RAS-Quelle** (volle Auflösung); dev2 nur als 5-min-Transfer → Konsistenz-Check.

### 2.2 Drei Schichten

```
┌────────────────────────────────────────────────────────────────────────┐
│ L3 (v2)  PREFERENZLERNEN: Loggt maintainer-Akzeptanz/-Anpassung der        │
│         Empfehlungen → Keras-Präferenzmodell verfeinert die            │
│         L2-Action-Rangfolge + lernt Offset-Wirkung (erst ab ~3        │
│         Monaten Parameter-Logging).                                   │
└────────────────────────────────────────────────────────────────────────┘
              ▲ (empfohlen / akzeptiert / angepasst + Timestamp + Ergebnis)
┌────────────────────────────────────────────────────────────────────────┐
│ L2  EMPFEHLUNGSENGINE (Optimizer, stündlich, deterministisch, kein ML) │
│  Jede Stunde (Minute 05):                                             │
│   1. Zustand S_t lesen (Metrik-DB + live)                              │
│   2. 24-h-Vorhersage W_(t→t+24) laden (Open-Meteo-Cache)              │
│   3. Kandidaten-Aktionsraum generieren (v1):                          │
│        • 9 Whitelist-Schalter (Modus ×7, Ölbrenner ×3, Solarpumpe,     │
│          WW-Ladepumpe, Zirkulation, Mischkreis-… — Rest AUTO)         │
│        • RAS-Offsets je Zone: {0, ±0,5, ±1,0, ±2,0 K} um Ist-Offset   │
│          (3 Zonen → 5^3=125 Offset-Kombis; gekappt auf plausible      │
│          ~20 per Zone-Raum + No-change)                               │
│        • Heizkurven-Parameter (falls CAN-setzbar, Q-108):             │
│          Neigung/Absatz ±1 Schritt (v1-Advisory, v2-Set)              │
│      → ~150–300 Kandidaten (Pruning: Doms-Schalter + Offset-Sprünge   │
│      nur wenn Last-Signal es erfordert)                               │
│   4. JEDE Aktion H = 12 h durch L1 rollen (6-h-Zwischencheck)         │
│   5. Kostenfunktion scoren: Komfort (perzipiert vs RAS-Soll-Band)     │
│      + Energie + Änderungs-Strafe + Risiko-Gates                     │
│   6. Beste Aktion → Empfehlung + Begründung + Konfidenz +             │
│      Szenario-Antworten („Holz jetzt → ~X L Öl in 48 h gespart“,      │
│      „Solar lädt 13:10–16:40 → Puffer +Y K um 22:00“)                 │
└────────────────────────────────────────────────────────────────────────┘
              ▲ (12-h-Rollouts je Kandidat ≈ 150–300 × 12 Forward-Steps)
┌────────────────────────────────────────────────────────────────────────┐
│ L1  ANLAGEN-MODELL (Keras, trainiert, 1-h-Step, action-conditional)   │
│  Lernt die DYNAMIK der Anlage aus 6 Jahren Messung:                   │
│    S_(t+1) = f( S_t, A_t, W_(t→t+1), Kalender_t )                    │
│  Targets (alle 1-h-ahead, alle in der Metrik-DB):                     │
│    RAS: perzipiert + Ist + Offset je Zone (dev1 a1/a2, a4/a5, a7/a8)  │
│    Heizkreis: Vorlauf/Rücklauf (dev0 a6/a7) · Puffer: ob/mit/unten   │
│    (dev0 a1/a2/a3) · WW (dev0 a4) · Kollektor (dev2 a16)             │
│    Holzkessel-VL/Abgas (dev0 a9/a10) · Ölkessel-VL (dev0 a5)         │
│    + resultierende Digital-States (Pumpen/Brenner/Mischkreis d1–d9)   │
│  Inputs: Zustand (alle Targets + Lags 1/2/6/12/24 h), **Aktion A_t**  │
│    (Digital-States + **RAS-Offset je Zone** + Heizkurven-Parameter),  │
│    Wetter 0→+24 h (Open-Meteo), Kalender (Stunde/Woche/Feiertag NRW/ │
│    DST/Sonnenauf- und -untergang).                                   │
│  Trainingsdaten: ~55.000 Stundenzustände 2020-08 → heute.            │
│    Offset je Zone = geloggt (a1−a2). Modus = historisch unbekannt    │
│    (Flag `mode_known=0`) → Modus-Effekt kommt mit L3-Logging dazu.   │
└────────────────────────────────────────────────────────────────────────┘
```

**Warum diese Form (und warum v0.1/v0.2 daneben lagen):**
- **v0.1-Fehler** (Klassifikation vergangener Parameter-States) = Imitation der bestehenden CMI-Automatik, keine Wetter-Optimierung + keine Szenario-Antworten möglich.
- **v0.2-Fehler**: (a) 1-h-Horizont ignorierte die thermische Trägheit (Haus = großer Speicher: Puffer, Mauern, RAS-Trägheit) → Optimizer hätte kurzfristig „nachgeheizt“ statt Puffer-strategisch zu laden; (b) a1/a4/a7 als „Soll“ behandelt → der eigentliche, manuell gesteuerte Hebel (Offset) stand im Modell als Ziel statt als Stellgröße.
- **v0.3**: L1 kennt die **Physik** (was passiert mit RAS/Puffer/Vorlauf, wenn der Offset um +2 K springt, wenn der Brenner an/ausschaltet, wenn das Wetter so ist). L2 ist die Stelle, an der „jeden Tag an Wetter+Situation+Vorhersage anpassen“ korrekt passiert — über **6–12 h**. Ergebnis: **erklärbar** (Begründung pro Empfehlung), **sicher** (Constraints hart), **stabil** (kein Policy-Net-Driften).
- **Keras-Treu**: L1 (+ später L3) sind die Keras-Modelle; L2 deterministisch → jede Empfehlung erklärungsfähig.

### 2.3 Die Kostenfunktion (definiert „optimal“)

```
H = 12 h (v1-Default, maintainer-einstellbar 6–12)
Soll_zoned = RAS-Modul-Soll (vom RAS-Gerät; nicht aus a1 abgeleitet —
             a1 ist Perzipiert = Ist + Offset)

Cost(A, t) =  w_komfort · Σ_Zonen Σ_{h=0..H}  max(0, |T_perc_zoned(t+h) − Soll_zoned(t+h)| − 0,5 K)²
                                            (Komfort: Band-Überschreitung, kein quadr. Anreiz im Band)
            + w_energie · ( Öl-Liter(A, H) + λ_holz · Holzbedarf(A, H) )
            + w_change  · Σ_Parameter  1[A_t ≠ A_(t−1)] · (Offset-Änderungen: 0,5·|ΔK|)
            + w_risiko  · Risikostrafen (Öl-Brenner-EIN bei Konfidenz < 0,85,
                            Puffer > 70 °C + Lade-Anforderung, Kollektor < 25 °C + Solar-HAND, …)
```
- Gewichte (v1-Default, maintainer-einstellbar): w_komfort=1,0 · w_energie=0,3 (kalibriert über Öl-Verbrauchs-Modell) · w_change=0,5 · w_risiko=10 (hart).
- **Komfort-Band ±0,5 K** um RAS-Soll zählt als voll erfüllt (kein Anreiz, 20,4 auf 20,9 zu treiben).
- **Energie**: Öl-Liter = Brenner-An-Quotient(A) × Liter/h-Kurve (aus Tank-CSV + dev0 d5, M1; **blockiert bis Q-106-CSV maschinenlesbar** — bis dahin: Brenner-An-Quotient als Proxy mit Konfidenz-Band). Holz: kein Zähler → Szenario-Logik („Holz-Ofen-VL > X °C seit Y min → Holzkessel trägt Last, Ölbrenner aus → geschätzte Ersparnis“).
- **No-change-by-default**: Parameter nur ändern, wenn Cost-Reduktion > Schwellwert (≈ 0,5 K·h Komfort oder ≈ 0,2 L Öl/12 h) → minimiert Falsch-Alarme und „KI-futtert-um“.

---

## 3. Ziele sauber beschrieben (Target-Spezifikation v0.3)

### 3.1 L1 — Anlagen-Modell (Keras-Regression, 1-h-Step)

**Targets (je 1-h-ahead, alle in der Metrik-DB):**

| Target | Quelle (Label) | Einheit | Rolle |
|---|---|---|---|
| **T_perzipiert Keller / EG / DG** | dev1 **a1 / a4 / a7** | °C | **Primär-Komfort-Signal** (darauf reagiert die Regelung) |
| T_ist Keller / EG / DG | dev1 **a2 / a5 / a8** (kanonisch; dev2 a5/a7/a9 = 5-min-Check) | °C | Physikalisch (Trägheit, Abkühlrate) |
| **Offset Keller / EG / DG** | dev1 **a1−a2 / a4−a5 / a7−a8** | K | **Stellgröße-Feedback** (was wurde gerade gedreht, wirkt wie?) |
| T_Vorlauf / Rücklauf Heizkreis | dev0 a6 / a7 | °C | Grundregelung-Resultat (Heizkurve) |
| T_Puffer Oben / Mitte / Unten | dev0 a1 / a2 / a3 | °C | Speicher-Ladezustand (12-h-Strategie!) |
| T_WW | dev0 a4 | °C | Nebenziel |
| T_Kollektor | dev2 a16 | °C | Solar-Ladefähigkeit |
| T_Holzkessel-VL / Abgas | dev0 a9 / a10 | °C | Holz-Szenario |
| T_Ölkessel-VL | dev0 a5 | °C | Öl-Ladezustand |
| Digital-Resultate (Brenner/Pumpen/Mischkreis/Ventil) | dev0 d1–d9, dev2 d1 | 0/1 | Resultierende Zustände |
| ~~Heiz-Minuten je Zone~~ → **RAS-Datenalter** `ras_age_*` (nur Input/Stale-Maske, **kein Target**, Korrektur 2026-10-31) | dev1 a11 / a12 / a13 | Zähler (0,1/min) | Aktualität der RAS-Werte |

**Inputs:**
- **Zustand** = alle obigen Targets + Lags 1/2/6/12/24 h (thermische Trägheit = Lags sind erstklassig).
- **Aktion A_t** (das, was „gedreht“ wurde — aus Log + Parameter-Logging):
  - Digital-States (Pumpen/Brenner/Mischkreis-Schalter, dev0 d1–d9, dev2 d1) — historisch geloggt,
  - **RAS-Offset je Zone** (Δ = a1−a2, a4−a5, a7−a8) — historisch geloggt (18.10-Beleg: Offset-Steps sind diskret, 0,1–0,5 K-Quantisierung sichtbar),
  - **Heizkurven-Parameter** (Neigung/Absatz, falls CAN-loggbar — Q-108; sonst: VL-Soll-Proxy aus a6+Außen-T),
  - **Betriebsmodus** — `mode_known`-Flag (historisch 0; ab Parameter-Logging 1).
- **Wetter 0→+24 h** (Open-Meteo: T, Taupunkt, Wolken total/low/mid/high, Regen mm + Wahrscheinlichkeit, Wind + Böen + Richtung, Luftfeuchte, Strahlung, WMO-Code, Frost-Flags).
- **Kalender**: Stunde / Wochentag / Feiertag NRW / DST / Sonnenauf- und -untergang / Ferien-Proxy.

**Loss:** MAE je Target, gewichtet (perzipiert + Ist RAS 1,0 · Puffer + Vorlauf 0,7 · Rest 0,4).
> **Nachtrag v002 (2026-10-31, M2b, `docs/l1-model-v002.md`):** L1 wird als **direkte Modelle je Horizont h ∈ {1, 4, 6, 12}** auf **Delta-Targets** `y(t+h) − y(t)` trainiert (Digital: Level), Aktion der Stunde t gilt über h als gehalten (= L2-Fragestellung). Offsets sind in v002 reine Aktion (kein Target). Harte DoD-Baseline ist **B0 Persistence** (`y(t+h) = y(t)`), B2 nur informativ. Responsivitäts-Test v2: Offset **und** perzipiert gemeinsam verschoben, Wirkung über d8-An-Quotient (`pump_hz_on`), nur Heizsaison-Zustände.
**Erfolg (v1, hart):**
- MAE +4 h RAS-Ist < Naive-Baseline (Persistence + gleiche Zeit gestern) **und** < 1,0 K. **(v002: harte Baseline = reine Persistence B0, s. Nachtrag.)**
- MAE +4 h Puffer < 1,5 K.
- **Offset-Responsivität** (neuer, kritischer Test): simulierter Offset-Step von +0 → +3,4 K (18.10-Muster) muss im Modell zu einer messbaren Senkung des resultierenden d8-An-Quotienten innerhalb von ≤ 6 h führen (Korrelation ≥ 0,6 auf Valid, Vorzeichen: Offset↑ → d8-An-Quotient↓).
- Szenario-Validierung: 5 Hand-Pick-Szenarien mit maintainer (z. B. „Regenwoche im März 2025: stimmt die Puffer-Trajektorie über 12 h?“).

### 3.2 L2 — Empfehlung (Output je Stunde, JSON)

```json
{
  "ts": "2026-10-31T21:05",
  "model_version": "cmi-brain-v003",
  "horizon_h": 12,
  "empfehlungen": [
    {
      "parameter": "RAS-Offset Keller",
      "kanal": "dev1 a1 (Perzipiert) / Offset Δ = a1−a2",
      "aktuell_K": 3.4,
      "empfohlen_K": 1.5,
      "konfidenz": 0.82,
      "ausfuehrung": "advisory_v1 (setzbar erst nach UVR1611-Programmupload, Q-107)",
      "begruendung_de": "T_out min 0–6 Uhr −2 °C, aber Puffer 58 °C + Keller-RL-Trägheit: Keller-Offset 3,4→1,5 K hält perzipiert im Band (±0,5 K), spart ~0,9 L Öl in 12 h, max. Abw. 0,3 K."
    },
    {
      "parameter": "Betriebsmodus",
      "adresse": "101301D0180",
      "aktuell": "NORMAL",
      "empfohlen": "ABGESENKT",
      "konfidenz": 0.74,
      "begruendung_de": "… (nur wenn Cost-Reduktion > Schwellwert, s. No-change-by-default)"
    }
  ],
  "szenarien": [
    {"frage": "Holz jetzt legen?", "antwort": "Ja: prognostiziert 2,1 h Brenner-Ersatz in 24 h → ~1,4 L Öl gespart; Abgas-T im Modell 280–320 °C (OK)."},
    {"frage": "Solar", "antwort": "Solar lädt 13:10–16:40, ~3,2 kWh in Puffer → Puffer +4 K um 22:00 (Strahlung 410 Wh/m², Wolken 20 %)."}
  ],
  "kontext": {"datenstand": "2026-10-31T21:00", "wx_quelle": "Open-Meteo ICON-D2, run 18:00", "stale": false}
}
```

**Regeln (v1, hart):**
1. **No-change-by-default** (Threshold: ≈ 0,5 K·h Komfort oder ≈ 0,2 L Öl/12 h).
2. **Ölbrenner-EIN**: nur mit Konfidenz ≥ 0,85 + Begründung + Alternativ-Vorschlag („warten: Puffer reicht bis HH:MM“); sonst Hinweis, keine Empfehlung.
3. **Offset-Empfehlungen (v1 = Advisory)**: nur als Vorschlag (Ausführung = RAS-Gerät/UVR1611, nicht per CMI). Setzbarkeit per CMI = Q-107 (blockiert auf UVR1611-Programmupload). Guardrail: Offset-Empfehlung nie > ±2 K pro Stunde (Raschel-Prävention), nie in die falsche Richtung (wenn Puffer < 40 °C und Frost: Offset nicht senken).
4. **Plausibilitäts-Gates** (vor Ausgabe): Puffer > 70 °C → keine Lade-Empfehlung; Kollektor < 25 °C → keine Solar-Hand-EIN; Modus-Wechsel nur zu ganzen Stunden; Offset-Sprünge in 0,5-K-Schritten (Quantisierung der RAS).
5. **Stale-Guard**: Metrik-Lücke > 1 h ODER Wetter-Run > 2 h alt → `stale=true`, nur „beibehalten“.
6. **Quellenangabe-DoD**: jedes Output muss `model_version` + Datenstand + Wetter-Run enthalten.

### 3.3 L3 (v2) — Präferenz-/Policy-Lernen

- Log: jede Empfehlung + the maintainer's Aktion (akzeptiert/angepasst/abgelehnt) + Ergebnis (Komfort/Energie rückwirkend über 12 h).
- Keras-Modell: (Zustand, Wetter, Kandidaten) → Präferenz-Rang (pairwise / Bradley-Terry) → verfeinert L2-Gewichte + lernt **Offset-Wirkung** (welcher Offset-Sprung in welcher Situation führte zu welchem Komfort-/Energie-Outcome).
- Zusätzlich: **Modus-Rekonstruktion** — aus Parameter-Logging + Offset-Wechsel wird der historische Modus (NORMAL/ABGESENKT/…) für 2025–26 rückwirkend plausibilisiert und ins L1-Training übernommen (`mode_known=1`-Flag).

---

## 4. Daten & Quellen (Datengeber)

### 4.1 Metrik-DB (vorhanden)

| Store | Zeitraum | Auflösung | Rolle |
|---|---|---|---|
| `data/winsol_pq/` | 2020-08-30 → 2026-10-27 | 10 s, 19,2 Mio. Records | L1-Training (Übergänge) + Backtest |
| `data/cmi_log_pq/` | 2026-10-27 → live (Poller 5 min) | 10 s | L1-Update + Live-Zustand |
| `data/history.db` + `/api/v1/state` | live | 30–60 s | Live-Zustand für L2 (Betriebsmodus + **RAS-Offsets** ab Parameter-Logging) |
| **Parameter-Logging (neu, read-only)** | ab Go (Q-102 ✅) | 30–60 s | **Betriebsmodus + RAS-Offset-Kanäle dev1 a1/a4/a7 + Heizkurven-Parameter** → L3 + L1-Modus-Flag |

Resampling 10 s → 1 h (Bucket = Kalenderstunde Europe/Berlin): Temperaturen/Offsets = mean (Offset = median der Δ), Digital = An-Quotient + letzte Stufe, ~~Heiz-Minuten = Summe~~ RAS-Datenalter a11–a13 = Stunden-MAX (`m1.1`), zusätzlich `*_last` = letzter 10-s-Wert der Stunde; Lücken: Forward-Fill ≤ 30 min + `gap`-Flag; CMI-Log-Ausfall (Q-97) = Lücke, kein Interpolieren > 1 h.
**Feature-Store:** `data/features/` (Parquet, Hive `YYYY/MM/`, zstd, ~200 Spalten/Zeile, < 50 MB) inkl. `data_version`, `gap_flags`, `mode_known`, `offset_known` (dev1 a1 verfügbar: ab 2020-08-30, kanonisch).
**dev2 = Konsistenz-Check nur** (RAS 5-min-Transfer; Divergenz > 1 K = CAN-Problem, Flag + Alarm).

### 4.2 Wetter (Datengeber, Auslese 2026-10-31)

Koordinaten: **51.382974, 7.774286** (Q-98 ✅) · Bundesland **NRW** (Q-99 ✅).

| Quelle | Endpunkt | Verwendung |
|---|---|---|
| **Open-Meteo Forecast** (DWD **ICON-D2**, 2 km, 3-h-Update, +16 d) | `api.open-meteo.com/v1/forecast` | **Live-24-h-Vorhersage** für L2 (stündlich 1 Call: T, Taupunkt, Wolken t/l/m/h, Regen + Prob., Wind + Böen + Richtung, Luftfeuchte, Strahlung, WMO, Frost-Flag; kostenlos non-commercial, Fair-Use) |
| **Open-Meteo Historical Forecast** (archivierte Runs „wie am Tag t ausgegeben“, ICON-D2 ab **2022-11**) | `historical-forecast-api.open-meteo.com` | **L1-Training 2022-11→heute ohne Look-Ahead-Bias** (Feature = damals verfügbare Prognose) |
| **Open-Meteo Historical Weather** (ERA5/IFS ab 1940/2017) | `archive-api.open-meteo.com` | Backfill **2020-08→2022-11** (als „Ist“, Feature `wx_is_forecast=0`) |
| **CMI-Außensensor** dev0 a8 | Metrik-DB | **Bias-Korrektur** Grid↔Sensor (quartalsweise neu: medianer Offset + Drift); Plausibilitäts-Check |
| **DWD Open Data** (optional) | `opendata.dwd.de` | nächste Station (Radar/Station) für Regen-Kalibrierung, v2 |
| **Öl-Tank-CSV** (maintainer, Q-106 — **aufgeschoben, Erinnerung offen**) | lokal (Pfad unbekannt, CSV erst maschinenlesbar machen) | **Verbrauchs-Modell** Liter/h aus Tankmenge + Brenner-Runningtime (dev0 d5) → w_energie-Kalibrierung (blockiert M1-Teil) |
| **Kalender NRW** | `python-holidays` (NRW) + DST | Feiertage (FEIERTAG-Modus-Kontext), Ferien-Proxy |

### 4.3 Was historisch fehlt (offen, dokumentiert)

- **Betriebsmodus**: nicht loggiert → L1-Flag `mode_known=0` (2020–heute) + Parameter-Logging ab jetzt (Q-102 ✅). Historisch: Modus-Effekt ≈ Offset-Effekt (Modus-Wechsel zieht Offset mit) → Teil der Unsicherheit, konservativ gewichtet.
- **Vorlaufsolltemperatur**: nicht als Log-Kanal gefunden; **erst anpassbar nach UVR1611-Programmupload (später, Q-107)**. Aktuell: Heizkurve per Parameter (Neigung/Absatz) oder statischer VL-Wert → L2-Kontext bis dahin = RAS-Soll + Puffer-Verhalten + VL-Proxy (a6 + a8).
- **Ölstand/Holzvorrat**: keine Sensoren → keine Versprechungen > 72 h; Szenario-Antworten nur mit Konfidenz-Band.
- **Q-106 (Öl-CSV)**: maintainer = „kommt später, ich muss die maschinenlesbar machen“ → **Erinnerung offen** (CIR-Log); M1-Öl-Teilverfahren blockiert, Rest (Wetter-Backfill + Feature-Store + RAS-Offset-Features) kann starten.

---

## 5. Training & Validierung

- **Walk-forward (verbindlich):** Train ≤ 2024-12-31 · Valid 2025 (volles Kalenderjahr) · Test 2026-01→10 (nur finaler Bericht, kein Tuning). 2020-08→12 = Kaltstart-Check.
- **Monatliches Re-Learning** im Betrieb: Train ≤ T−3 Monate, Valid T−2…T−1, Inferenz T. Shadow-Phase 1 Woche (nur Loggen) → A/B → Promotion (`models/cmi-brain/active`), Rollback = 1 Datei-Tausch.
- **Baselines (Ehrlichkeits-Gate):**
  - **B0 „keine Heizung“**: Aktionen = konstant AUTO, Puffer-Last → L1-Only-Vergleich (reines Systemmodell).
  - **B1 „so weiter“**: heutiges CMI-Verhalten (Digital-States + Offset-Sprünge aus der Historie wiedergeben) — L2 muss das über den 12-h-Horizont schlagen, sonst hat die KI keinen Mehrwert.
  - **B2 Naive-Predictor** (Persistence + gleiche Zeit gestern) für L1-MAE-Vergleich. **v002 (2026-11-01):** B2 ist schwächer als reine Persistence (RAS-Keller +4 h 0,460 K vs. 0,196 K) → L1-DoD gegen **B0-Persistence** `ŷ(t+h) = y(t)` (Pflicht), B2 + B0-last nur informativ (`analysis/model/m2b/evaluate.py`). Achtung Namenskollision: dieses „B0“ (L1-Persistence) ≠ obiges B0 „keine Heizung“ (L2-Backtest).
- **Metriken:**
  - L1: MAE je Target + **Offset-Responsivität** (§3.1, Vorzeichen + Korrelation) + Szenario-Validierung (5 Hand-Picks mit maintainer).
  - L2: Backtest 2025+2026 (12-h-Rollouts): Comfort-Band-Erreichungsrate (±0,5 K perzipiert vs RAS-Soll, % Stunden), Öl-Äquivalent (geschätzte Liter/12 h), Change-Rate (empfohlene Wechsels je Woche — Ziel < 5, weniger = ruhiger), False-Alarm-Report (jede Empfehlung, die im Backtest „falsch“ war → manuelle Prüfung).
- **Determinismus:** fester Seed, deterministischer CPU-Inferenz-Modus, `data_version` + git-Rev + Keras/TF-Version in `metadata.json` je Artefakt.

---

## 6. Serving & Betrieb

- **Takt:** L2 stündlich (Minute 05), < 500 ms (150–300 Kandidaten × 12 Steps eines ≤ 500 k-Parameter-Netzes, CPU-Only; Kandidaten-Pruning + Vectorised-Rollout).
- **Modell-Service-Container** `cmi-model` (eigener leichtgewichtiger Container, Docker-Compose, interner Port; keine CMI-Verbindung — nur Feature-Store + Wetter-Cache + Empfehlungsjournal).
- **Endpunkte:** `POST /predict` (L1-Rollout für beliebige Szenario-Fragen — z. B. „Puffer bei Solar-Aus ab 14:00 über 12 h?“), `POST /recommend` (L2-Stundenvorschlag, §3.2-Format), `GET /model/status` (Version, Datenstand, Stale-Flags).
- **LLM-Anbindung (v2, S6-5):** OpenAI-kompatible Tools `cmi_forecast` (→ /predict) + `cmi_recommend` (→ /recommend) für DGX-SGLang; Antwort **muss** `model_version` + Datenstand + Wetter-Run enthalten (Quellenangabe-DoD).
- **Empfehlungsjournal (kritisch für L3):** jede Empfehlung + späteres Outcome (Komfort/Energie über 12 h) + maintainer-Entscheidung → `data/recommendations/YYYY-MM.parquet` (Trainingsdaten für L3 + Transparenz: „Was hat die KI wann empfohlen, und was war’s wert?“).

---

## 7. Sicherheit & Guardrails (Sicherheits-Frame, unverändert + neu)

1. **Kein Auto-Write** (v1): Empfehlungen → UI/LLM/Mensch; Ausführung nur über S3-1 (Whitelist, Read-back, Audit, Bestätigungs-Dialog).
2. **Risikoparameter Ölbrenner** (P2): Konfidenz-Gate 0,85 + Begründung + Alternativ-Option.
3. **No-change-by-default** (Threshold-basiert) → minimiert Falsch-Alarme.
4. **Offset-Guardrails (neu, v0.3):** Offset-Empfehlungen v1 = **Advisory** (Ausführung am RAS-Gerät/UVR1611, nicht per CMI); Maximal-Δ 2 K/h; 0,5-K-Quantisierung; nie entgegen der Puffer-/Frost-Lage (Puffer < 40 °C + Frost-Nacht → Offset nicht senken).
5. **Harte Plausibilitäts-Gates** (§3.2 Regel 4) — Violation = Empfehlung wird verworfen + geloggt (nie umgangen).
6. **Stale-Guard** (§3.2 Regel 5).
7. **Scope-Trennung zur Parallel-Session:** dieser Track = `analysis/model/**`, `data/features/**`, `data/recommendations/**`, `models/**`, `docs/ai-model-design.md`; UI/App-Code = andere Session (Empfehlungs-UI-Panel kommt in M6, abgestimmt).

---

## 8. Meilensteine (Tickets mit DoD)

> Reihenfolge strikt: Daten → L1 → Baselines/Backtest-Harness → L2 → Guardrails/Shadow → Integration. Jeder Ticket = 1 Subagent-Run (python-pro) + Orchestrator-Verifikation. Build & Coverage-Gate (≥ 80 %) + deterministischer Seed gelten. **M1 startet ohne Öl-Teil (Q-106 blockiert).**

| Ticket | Inhalt | DoD (hart) |
|---|---|---|
| **M1 — Wetter-Backfill + Feature-Store** (Öl-Teil ausgenommen, Q-106) | Open-Meteo-Backfill (ERA5 2020-08→2022-11, Historical-Forecast 2022-11→heute, Koordinaten 51.382974/7.774286), Bias-Korrektur dev0 a8↔Grid, Feature-Store `data/features/` (1-h-Buckets, Lags 1/2/6/12/24 h, **RAS-Offset je Zone (Δ a1−a2, a4−a5, a7−a8)** + Perzipiert/Ist, Digital-Actions, Heiz-Minuten a11–a13, `mode_known`, `offset_known`) | Parquet vollständig (Lücken nur dokumentiert), Bias-Report (Median-Offset je Saison), Schema-Tests, pytest ≥ 80 %, kein CMI-Zugriff |
| **M2 — L1 Anlagen-Modell (Keras)** | §3.1: Input-Branches (State+Lags / **Aktion (Digital + Offset + Heizkurve)** / Wetter-24h-Sequenz / Kalender), Multi-Output-Regression, Walk-forward, Baselines B0/B2, **Offset-Responsivitäts-Test** (18.10-Muster: +3,4 K → d8-An-Quotient↓ in ≤ 6 h) | MAE +4 h RAS-Ist < B2 UND < 1,0 K; MAE +4 h Puffer < 1,5 K; Offset-Responsivität (Korrelation ≥ 0,6, Vorzeichen korrekt); deterministischer Seed; `models/cmi-brain/v001/`; pytest ≥ 80 % |
| **M3 — Backtest-Harness + Baseline „so weiter“ (B1)** | L1 + B1-Backtest 2025+2026 **über 12-h-Horizont** (Comfort-Band perzipiert vs RAS-Soll, Öl-Äquivalent, Change-Rate), Eval-CLI deterministisch, Report | B1-Metriken reproduzierbar (Seed), Report mit allen Metriken + False-Alarm-Kandidaten |
| **M4 — L2 Empfehlungsengine (Optimizer)** | §3.2: Aktionsraum (Whitelist-Schalter + **Offset je Zone 0,5-K-Schritte, gekappt** + Heizkurven-Parameter (advisory)), 12-h-Rollouts (Pruning), Kostenfunktion + Gewichte (maintainer-tunbar), No-change-by-default, Offset-Guardrails, Plausibilitäts-Gates, Stale-Guard, `/predict` + `/recommend` im `cmi-model`-Container | API-Tests (alle Gates inkl. Violation-Fälle), Latenz < 500 ms, Backtest: Comfort-Band ≥ B1 UND Öl-Äquivalent ≤ B1 + 5 %, Change-Rate < 5/Woche; Quellenangabe in jedem Output |
| **M5 — Shadow-Phase + Empfehlungsjournal** | L2 live (Shadow: loggen nur, keine UI), 1 Woche, Journal `data/recommendations/`, A/B-Report vs. B1 (12-h-Horizont) | 1 Woche Log ohne Gate-Violation (0), Staleness-Alarm-Funktion getestet, A/B-Report (Komfort/Energie/Change), maintainer-Review-Datei |
| **M6 — Integration** | UI-Empfehlungs-Panel (in Loop-Views: „KI-Vorschlag“-Badge + Begründung + Szenario-Antworten), LLM-Tools `cmi_forecast`/`cmi_recommend` (S6-5), DGX-Live-Test, Retrain-Cron + Promotion-/Rollback-Runbook, **Parameter-Logging (Betriebsmodus + RAS-Offset-Kanäle + Heizkurven-Parameter) im Gateway-Poller (Q-102 ✅, read-only, 30–60 s)** | UI rendert /recommend (Desktop + 375 px), DGX-Tool-Call live (Quellenangabe im Output), Retrain-Run dokumentiert + Rollback getestet, Modus- + Offset-Logging in Feature-Store sichtbar |
| **M7 (neu, v0.3) — Öl-Verbrauchs-Modell** (nach Q-106) | maintainer macht Tank-CSV maschinenlesbar → M7: Liter/h-Kurve aus Tank-Events + dev0 d5 Brenner-Runningtime (6 Jahre), Konfidenzband, monatliche Re-Kalibrierung | Öl-Liter/h-Kurve + Band, Backtest-Übereinstimmung Tank-Abnahme vs Modell ≥ 80 %, w_energie kalibriert |

**Nicht in diesem Track:** Auto-Write (S3-3/S3-4 = v2/v3, später), Modbus (S2-3), **Vorlaufsoll-Schreib-Freischaltung (Q-107 → UVR1611-Programmupload → v2)**, RAS-Offset-CMI-Write (Q-107 → v2), UI außerhalb des Empfehlungs-Panels (andere Session).

---

## 9. Risiken & offene Fragen

### Offene Fragen (HITL → maintainer)

| ID | Frage | Status |
|---|---|---|
| Q-98 | Koordinaten | **BEANTWORTET: 51.382974, 7.774286** |
| Q-99 | Bundesland | **BEANTWORTET: NRW** |
| Q-100 | RAS-Offset/Sollwerte | **BEANTWORTET (2. Runde, 2026-10-31):** a1/a4/a7 = **Perzipiert-Temperatur = Ist + Offset** (nicht Soll!). Offset = Stellgröße → **RAS-Offset je Zone ist jetzt L2-Entscheidungs-Variable** (v1 = Advisory, setzbar = Q-107). |
| Q-101 | Keras vs. GBDT | **BEANTWORTET (2. Runde):** „Ansonsten könnte das so passen“ → Keras-L1 + LightGBM-Pflicht-Vergleich, deployt der Gewinner (MAE + Responsivität). |
| Q-102 | Parameter-Logging | **BEANTWORTET: „Ja, mach das so.“** → Schematic-Poller 30–60 s read-only: **Betriebsmodus + RAS-Offset-Kanäle (dev1 a1/a4/a7) + Heizkurven-Parameter** in history.db/Feature-Store (M6). |
| Q-103 | Energie-Metrik | **RICHTUNG KLAR:** Öl = Tank-CSV (Q-106, **aufgeschoben**) + Brenner-Runningtime (dev0 d5) → M7. Holz = Szenario-Antworten ohne Zähler. Bis Q-106: Brenner-An-Quotient als Proxy (Konfidenz-Band). |
| Q-104 | Vorlaufsolltemperatur | **BEANTWORTET (2. Runde):** erst anpassbar **nach UVR1611-Programmupload (später)** → **v2, Ticket M-„Vorlaufsoll“** (neue Whitelist-Adresse + Read-back + Gate). Aktuell: **Heizkurve per Parameter** (Neigung/Absatz) **oder statischer VL-Wert** → L2-Advisory v1. |
| Q-105 | Semantik dev1 a1/a4/a7 | **BEANTWORTET (2. Runde):** = **Perzipiert-Raumtemperatur (Ist + Offset)**, Regelung reagiert darauf → Heizkreis-Abfahrt (18.10-15:30-Beleg im Parquet: Offset −0,9 → +3,4 K, VL 34→52, d8 16:05 1→0). Nicht „Soll“. plant-model-Label „Offset [K]“ = korrekt (K-Offset, nicht absolute Ziel-T). |
| Q-106 | Öl-CSV | **AUF GESCHOBBEN (maintainer: „kommt später, ich muss die maschinenlesbar machen“)** → **Erinnerung offen** (CIR-Log). Blockiert: M7 (Öl-Verbrauchs-Modell). Nicht blockiert: M1 (Rest), M2–M6. |
| Q-107 (neu) | **RAS-Offset per CMI setzbar?** (dev1 a1/a4/a7 / Offset-Kanäle): nach UVR1611-Programmupload = CMI-Write-Adresse (CAN 12xxx?) + Read-back + Gate (v2, M-„Offset-Write“). Bis dahin: Advisory. |
| Q-108 (neu) | **Heizkurven-Parameter (Neigung/Absatz) — CAN-Adressen + setzbar/ausslesbar via CMI?** (read-only-Prüfung; falls ja: L2-Advisory v1 + Parameter-Logging ab M6). Doku: TA UVR1611 Manual, Modul HEIZKREISREGELUNG. |

### Risiken

| Risiko | Auswirkung | Mitigation |
|---|---|---|
| **CMI-Log-Ausfall (Q-97)** | Live-Zustand lückig | Stale-Guard, Feature-Store gap-Flags, CMI-Prüfung (HITL) |
| **Modus 2020–26 nicht loggiert** | L1-Modus-Effekt unscharf | `mode_known`-Flag + Offset als Proxy (Modus-Wechsel zieht Offset mit) + L3-Logging ab jetzt |
| **Aktionen = Digital-States + Offset-Sprünge (Regel-Folgen, keine menschlichen Entscheidungen)** | L1 lernt „CMI-Logik“, nicht „Was wäre wenn“ | Genau dafür da: L1 soll die Anlagedynamik (thermisch) lernen, die CMI-Logik ist Teil der Realität; Szenario-Validierung mit maintainer (5 Hand-Picks) als Gate |
| **Haus-Trägheit → 12-h-Rollouts mit Modellfehler-Kumulierung** | L2-Entscheidung auf unsichere Trajektorien | 6-h-Zwischencheck (Kostensumme: 0–6 h + 6–12 h, zweite Hälfte mit Unsicht-Band), Stale-Guard, Backtest-Gate (Comfort ≥ B1) |
| **Offset-Responsivität (Kernaussage v0.3) nicht im Modell reproduzierbar** | v0.3-Architektur bricht | M2-DoD: Offset-Responsivitäts-Test (18.10-Muster) als hartes Gate; Alternativ: explizites Offset-Feature-Branching (Offset als direkte Aktion-Eingabe, nicht nur Zustand) |
| **Öl-Verbrauchs-Modell aus Tank-CSV (spärlich: wenige Tanks/Jahr)** | w_energie unscharf | Konfidenzband + monatliche Re-Kalibrierung; Öl-Ersparnis-Aussagen immer mit Band; bis M7: Brenner-An-Quotient-Proxy |
| **Klasse-Ungleichheit / No-change-Dominanz** | L2 empfiehlt kaum etwas | Threshold-Kalibrierung im Backtest (Change-Rate-Ziel < 5/Woche, aber Comfort ≥ B1) |
| **Wetter-Grid ≠ Sensor (ERA5 25 km 2020–22)** | System-Error im frühen Training | Bias-Korrektur + `wx_is_forecast`-Flag (frühe Jahre reduziert gewichtet) |
| **Overfitting der 6-jährigen Historie auf ein Haus** | schlechte Generalisierung (Saison/Anomalie) | Walk-forward + Monats-Retrain + Shadow-Phase + Drift-Alarm (Valid-MAE-Trend) |
| **Parallel-Session (UI/App)** | Datei-Kollisionen | Strikte Scope-Trennung (§7.7) |
| **Open-Meteo-Lizenz (non-commercial)** | ok privat; kommerziell → Lizenzwechsel | Doku-Vermerk |
| **dev2-RAS (5-min-Transfer) weicht von dev1 ab** | Konsistenz-Problem (CAN-Transfer) | dev2 = nur Konsistenz-Check (Divergenz > 1 K → Flag + Alarm), Training nur dev1 |

---

## 10. Quellen & Referenzen

- **Open-Meteo** (Forecast / Historical Forecast / Historical Weather API, ICON-D2 2 km DE, kostenlos non-commercial): `open-meteo.com/en/docs`, `…/historical-forecast-api`, `…/historical-weather-api`
- **DWD Open Data** (optional, v2): `opendata.dwd.de`
- **TA UVR1611 Manual** (Heizkurve / HEIZKREISREGELUNG / T.vorl.Soll): `ta.co.at/fileadmin/Downloads/Betriebsanleitungen/00_Auslauftypen/UVR1611/Manual_UVR1611_A4.03-2.pdf`
- Projekt-intern: `docs/plant-model.md`/`.json` (Kanal-Belegung, 9 Setz-Adressen, dev1/dev2-Doppelbelegung), `docs/hydraulic-topology.md` + `docs/hydraulic-loops-rules.md` (R-A1…R-X8 → Plausibilitäts-Gates, **R-X5: a8 Außen-T → Heizkurve → VL-Soll**), `docs/winsol-logx.md` + `docs/cmi-log-protocol.md` (Formate/Rosetta), `CMI_WebTest/CMI-WEB/CMI-WEB.xml` (Setzbarkeits-Nachweis CAN 12360 sub1 Vorlaufsoll), `data/winsol_pq/` + `data/cmi_log_pq/` (Trainings-Quellen), Plan.md S6 (Grundgerüst + Sicherheits-Frame).
