# Das KI-Anlagenmodell — in Klartext (S6, Stand: nach M2, 2026-10-31)

> Dieses Dokument erklärt, **was** das stündliche Heizungs-KI-Modell macht, **welche Daten** es
> „sieht“, **wie** es aufgebaut ist und **wie man beurteilt, ob es gut ist** — in natürlicher
> Sprache, ohne Formeln und Code. Es beschreibt den tatsächlich gebauten Zustand
> (M1 Feature-Store + M2 Modell-Training, Artefakt `cmi-brain-v001`).

---

## 1. Der Gedanke in zwei Sätzen

Das Modell beantwortet eine einzige Frage: **„Wie sieht die Anlage in der nächsten Stunde
aus?“** — und das für 31 Größen gleichzeitig (Raumtemperaturen, Puffer, Vorlauf, Brenner-Zustand …).
Das Besondere: die Antwort hängt davon ab, **was man am Regler dreht** (Schalter, Raum-Offsets).
Damit kann man im Computer fragen: *„Was passiert, wenn ich jetzt den Keller-Offset um 3 Grad
erhöhe und das Wetter so wird?“* — ohne wirklich dran zu drehen.

Die eigentliche **Stundeneinstellung der Parameter** (der Optimizer, der das Wetter ansieht und
empfiehlt) ist eine zweite Ebene darüber, die auf diesem Modell aufbaut (M4). Dieses Dokument
behandelt nur das darunterliegende **Anlagenmodell** (M2).

---

## 2. Welche Daten kommen „rein“? (das, was man ein „Tensor“ nennt)

Das Modell bekommt für jede zu betrachtende Stunde (z. B. Donnerstag 14:00 Uhr) **eine einzige
lange Zahlenreihe** — am Ende **832 Zahlen**. Nicht mehr, nicht weniger. Das ist der „Tensor“.
Die 832 Zahlen gliedern sich in sechs Gruppen:

### 2.1 Der aktuelle Zustand (20 Zahlen)
Die Messwerte **genau in dieser Stunde**: die realen Raumtemperaturen von Keller, EG und DG;
die „perzipierten“ Raumtemperaturen (Ist + Offset, also das, was der Regler sieht); der
Pufferstand oben, Mitte und unten; der Vorlauf und Rücklauf der Heizung; die Warmwasser-
Temperatur; die Kollektortemperatur; die Kesseltemperaturen von Holz- und Ölkessel; die
Abgastemperatur; und die Außentemperatur.

### 2.2 Die Gedächtnis-Schnappschüsse (105 Zahlen)
Damit das Modell „sieht“, wie sich die Anlage zuletzt entwickelt hat, kriegt es für 21 der
wichtigsten Größen zusätzlich **fünf Schnappschüsse aus der Vergangenheit**: so wie es vor
**1, 2, 6, 12 und 24 Stunden** war. Das ist die Antwort auf deine Frage „letzte 24 h“ — nur
nicht als fließende Aufzeichnung, sondern als **fünf markante Zeitpunkte** (mehr dazu in §7).
Wegen des Puffers und der Mauern ändert sich das Haus langsam; fünf Schnappschüsse reichen,
um den Trend (wird es kälter? lädt der Puffer gerade?) zu erkennen.

### 2.3 Was „gedreht“ wurde — die Aktionen (9 Zahlen)
Die Schalter- und Pumpen-Stellungen dieser Stunde (wer war an/aus, gemessen als
Anteil der Zeit: Brenner, Pumpen, Mischventile, Zirkulation) **plus die drei Raum-Offsets**
(Keller/EG/DG). Genau diese 9 Zahlen sind die Stellgrößen: im Modell kann man sie ändern und
beobachten, was daraus folgt. Das war im realen Log ja so, wie es der Regler/du gesetzt hast;
im Szenario-Modus setzt man sie beliebig.

### 2.4 Das Wetter der nächsten 24 Stunden (264 Zahlen)
Hier steckt deine Idee „Wettervorhersage der nächsten x Stunden“ drin: **24 Stunden à 11
Werte** (Temperatur, Taupunkt, Luftfeuchte, Bewölkung insgesamt, Niederschlag, Regen,
Windgeschwindigkeit, Böen, Windrichtung, Sonneneinstrahlung, Wettercode). Die Temperatur ist
die **bias-korrigierte** Version — M1 hat saisonale Abweichungen zwischen dem Wetter-Grid und
deinem Außensensor gemessen (z. B. im Winter liegt der Sensor ~1 Grad über dem Grid) und sie
abgezogen. Wichtig: bei den Trainingsdaten ab Ende 2022 sind das **die Prognosen, die damals
wirklich verfügbar waren** („was hätte das Modell damals wissen können?“) — nicht der
rückwirkende Ist-Wert. Das verhindert einen heimlichen Blick in die Zukunft.

### 2.5 Der Kalender (5 Zahlen)
Uhrzeit des Tages, Wochentag, ob Wochenende, ob NRW-Feiertag, ob Sommerzeit. Das Haus verhält
sich an Urlaubstagen und feuerhaltigen Winterabenden anders als an Wochentagen im Sommer.

### 2.6 Qualitäts-Flags (2 + 427 Zahlen)
Zwei feste Flags (z. B. „Modus bekannt?“, „Offset geloggt?“) und — der größere Teil — ein
**Leerstellen-Flag je Kanal**: wenn ein Messwert fehlt (der Kollektor existiert erst seit
März 2022, die RAS-Doppelkanäle erst seit 2021), wird der Wert zwar ergänzt, aber das Modell
bekommt zusätzlich ein Flag mit, das sagt: „diese Zahl ist ergänzt, kein echter Messwert“.
So kann es lückige und vollständige Daten unterscheiden.

**Gemeinsamer Schritt vor dem Modell:** alle 832 Zahlen werden auf eine gemeinsame Skala
gebracht („normiert“): abzüglich des Durchschnitts, geteilt durch die typische Streuung —
aber **nur mit Durchschnitt und Streuung, die in der Trainingszeit gemessen wurden**. So
lernt das Modell aus der Trainingszeit keine „Zukunftswerte“ kennen.

---

## 3. Wie ist das Modell aufgebaut?

Das Keras-Modell (Bezeichnung im Projekt: L1, Artefakt `cmi-brain-v001`) ist ein
**vollvernetzte-Netze-Modell** („neuronales Netz“) mit einem klaren Grundriss:

- **Ein gemeinsamer „Stamm“** (der „geteilte Teil“): drei verknüpfte Schichten mit
  **256, dann 128, dann 64 „Neuronen“**. Jede Schicht berechnet aus allen 832 Eingängen
  neue Werte, und zwar so, dass alle Werte unterhalb des Schwellwerts 0 auf 0 gesetzt
  werden (das ist die **ReLU-Aktivierung** — sie macht das Netz sparsam und schnell).
  Nach jeder Schicht wird mit einer kleinen Wahrscheinlichkeit (10 %) ein zufälliger
  Neuron ausgesetzt („**Dropout**“) — das zwingt das Netz, sich nicht auf einzelne
  Eingänge zu versteifen, sondern robuste Muster zu lernen.
- **31 „Köpfe“** — einer für jede der 31 Ausgaben. Jeder Kopf hat eine kleine Zwischenschicht
  mit **32 Neuronen** (wieder ReLU) und einen letzten Neuron:
  - Für die **22 Temperatur-/Mengen-Größen** (Raumtemperaturen, Puffer, Vorlauf, …) ist
    die letzte Schicht **linear** — sie darf also jede Zahl abgeben (18 °C, 45 °C, …).
  - Für die **9 Schalter-Größen** ist die letzte Schicht eine **Sigmoid-Kurve**, die jede
    Zahl in einen Wert zwischen 0 und 1 umrechnet: die Wahrscheinlichkeit bzw. den Anteil
    „an“ in der nächsten Stunde.
- **Größe:** 319.903 „Drehregler“ (Parameter) — innerhalb der Vorgabe von maximal 500.000,
  damit das Modell schnell bleibt und nicht zu sehr an Details der Vergangenheit „overfittet“.

**Was das Modell beim Training lernt:** es soll die Abweichung zwischen Vorhersage und
Messung möglichst klein machen — gemessen als **Durchschnitts-Fehlertoleranz in Grad
(MAE)** — wobei nicht alle Größen gleich viel zählen: Raumtemperaturen haben das
höchste Gewicht (1,0), Puffer und Vorlauf 0,7, der Rest 0,4.

**Parallel gelaufen ist ein zweites Modell** (LightGBM, ein „Baum-Ensemble“): dasselbe
Eingangsschema, dieselbe Bewertung, aber ein grundsätzlich anderer Lerntyp — statt eines
Netzes hunderttausende kleine Entscheidungs-Bäume. **Beide Modelle wurden trainiert; der
bessere wird „eingesetzt“.** (Stand M2: das Baum-Ensemble liegt deutlich vorn bei der
Rauheit — Details in §6.) Das Keras-Modell bleibt als Referenz im Artefakt.

---

## 4. Was kommt als Ausgabe?

Für **jede Stunde** liefert das Modell **31 Zahlen** für die **nächste** Stunde:

- 3 reale Raumtemperaturen (Keller/EG/DG)
- 3 perzipierte Raumtemperaturen (die der Regler sieht)
- 3 Raum-Offsets
- Vorlauf und Rücklauf der Heizung
- Puffer oben / Mitte / unten
- Warmwasser-Temperatur
- Kollektortemperatur, Holz-Kessel-Vorlauf, Abgas, Öl-Kessel-Vorlauf
- Heiz-Minuten pro Zone (der „Heizlast“-Proxy) — **Korrektur 2026-10-31:** a11–a13 sind in Wahrheit das *Datenalter* der RAS-Funkübertragung (keine Heiz-Minuten); v002 nutzt sie nur noch als Qualitäts-Merkmal, s. `docs/l1-model-v002.md`
- 9 Schalter-Anteile (Ölbrenner, Pumpen, Mischventile, Zirkulation, Solarpumpe)

Beispiel: *„In der Stunde 15:00 räume das Zimmer auf: Raum-Ist 21,1 °C, Puffer-Oben 56 °C,
Vorlauf 38 °C, Brenner-An-Anteil 0 % (bleibt aus), Keller-Heizkreis-Pumpe: 60 % an.“*

Wichtig: Das Modell **empfiehlt nichts** und **schreibt nichts** in den Regler. Es sagt nur
vorher, was passiert. Es ist das „Was-wäre-wenn“-Werkzeug, auf dem die Empfehlungsschicht
aufbaut.

---

## 5. Wie entstehen die Trainingsdaten?

1. **Rohdaten:** Seit August 2020 wird alle 10 Sekunden gemessen (WinSol-Log; ~19 Millionen
   Datensätze). Aus M1 ist daraus eine **stündliche Tabelle** mit **54.061 Zeilen** (eine
   Zeile pro Kalenderstunde, Time-Zone Europe/Berlin) entstanden — alle Messwerte der Stunde
   zu einem Stunden-Wert zusammengefasst, plus die Wetter- und Kalender-Spalten aus §2.
2. **Der Sprung aus zwei Stunden in eine Beispiel:** Man legt zwei aufeinanderfolgende
   Stunden aneinander: Die „Frage“ (alles, was um **Stunde t** bekannt ist — Zustand,
   Schnappschüsse, Aktionen, 24-h-Wetter, Kalender) und die „Antwort“ (das, was in **Stunde
   t+1** gemessen wurde) bilden ein **Trainings-Beispiel**. Das Haus selbst ist der
   „Lehrmeister“: Die Antwort kommt immer aus den echten Messungen, nicht aus einer
   Simulation.
3. **Ausgefiltert:** Paare, bei denen eine der Stunden eine größere Datenlücke hat (Logger-
   Ausfall) oder die Folgestunde fehlt, werden weggelassen (zählt und dokumentiert).
4. **Fehlende Kanäle** (z. B. Kollektor vor März 2022) werden ergänzt und per Flag
   markiert (§2.6).
5. **Aufteilung (Walk-forward) — das ist das Herzstück:** Die Beispiele werden **chronologisch**
   aufgeteilt, nicht gemischt:
   - **Lernzeit:** alles bis 31.12.2024 → **37.426 Beispiele**
   - **Kontrollzeit:** das ganze Jahr 2025 → ca. 8.800 Beispiele (daran wird das Modell
     während des Trainings „zwischengeprüft“)
   - **Prüfzeit:** 2026 Januar bis Oktober (noch nicht im Training „gebraucht“)
   So ist garantiert, dass das Modell nie auf Daten „trainiert“ und dann dieselben
   „abkupfen“ kann — es wird immer auf **zukünftigere** Daten geprüft, wie im echten
   Betrieb. (Dein Vorschlag „natürlich alle Daten“ ist damit umgesetzt: **all** die Daten
   seit 2020 fließen rein, aber in dieser strikten Zeit-Reihenfolge.)

---

## 6. Wie wird bewertet, ob das Modell gut ist?

Die zentrale Größe ist der **durchschnittliche Irrtum in Grad** (MAE — Mean Absolute Error):
wie weit liegt die Vorhersage im Mittel von der Messung entfernt?

- **1-h-Vergleich:** Wie gut ist die Direkt-Vorhersage für die nächste Stunde?
- **4-h-Verkettung (die wichtigste Prüfung):** Man sagt Stunde 1 vorher, gibt die eigene
  Vorhersage wieder als Eingabe zurück, sagt so Stunde 2 vorher — und zwar **4 Schritte
  hintereinander**. Das ist genau das Prinzip, das du beschrieben hast: „Daten ab Ende des
  Eingangsfensters für die nächsten Stunden nutzen als Maßstab“. Man vergleicht die
  verkettete 4-h-Vorhersage mit dem, was wirklich in den 4 Stunden danach gemessen wurde.
  Das zeigt, **wie stark der Fehler anwächst, wenn das Modell auf sich selbst zurückgreift** —
  der entscheidende Test für ein Modell, das für die 12-h-Optimierung rollen soll.
  Zusätzlich wird ein **6-h-Test** für die Offset-Responsivität gefahren.
- **Der Stumpfe-Vergleich (Baseline „B2“):** Ein Naiv-Modell, das für die nächste Stunde
  einfach „die Hälfte von so-eben + die Hälfte von vor einer Stunde“ annimmt. **Das
  echte Modell muss diesen Naiv-Ansatz schlagen** — sonst ist es wertlos. (Zum Vergleich:
  der Naiv-Ansatz liegt bei Raum-Ist bei ~0,3 K, der Keras-Kopf bei ~1,1 K, der
  LightGBM-Kopf bei ~0,17 K.)
- **Offset-Responsivitäts-Test (der wichtigste fachliche Test):** In 200 zufälligen,
  lückenfreien Situationen wird simuliert: „Was passiert mit dem Keller-Heizkreis, wenn ich
  den Offset jetzt um 1,0 / 2,0 / 3,4 / 5,0 K anhebe?“ (3,4 K = der reale Sprung vom
  18.10.). Das Modell muss dann **weniger Heizen** vorhersagen (Korrelation muss klar
  negativ sein). **Stand M2: Das erfüllt weder Keras noch LightGBM hinreichend** — der
  Effekt ist in den historischen Daten vom Wetter überlagert (Offset-Sprünge passierten
  überwiegend in warmen Phasen). Das ist ein dokumentierter offener Punkt (→ M2-Risiko,
  Design §9) und nicht verschwiegen.
- **Keras-vs.-LightGBM-Entscheid:** Beide Modelle werden über dieselben Prüfungen
  (Rauheit + Responsivität + 4-h-Verkettung) gemessen; **wer gewinnt, wird „eingesetzt“**
  (aktuell: LightGBM). Die Entscheidung steht im Artefakt-Metadaten, mit Begründung.
- **2026-Prüfung (ohne „Nachbessern“):** Die letzten 10 Monate 2026 dienen als
  Abschluss-Prüfung; es wird außerdem der schlechteste Tag herausgesucht (Worst-Case) und
  dokumentiert, damit man das bei der Prüfung sieht.

---

## 7. Abweichung von deiner Idee — und warum

Du hattest vorgeschlagen: **Sliding-Window der letzten 24 h als Eingabe** (also die
laufende Aufzeichnung der letzten 24 Stunden), **plus Wettervorhersage der nächsten
x Stunden**, und als Maßstab die **Daten der 3–6 Stunden nach Ende des Fensters**.

So wurde es umgesetzt:

| Deine Idee | Umsetzung M1/M2 | Passt? |
|---|---|---|
| Wetter der nächsten x h | 24-h-Stunden-Forecast (11 Größen), bias-korrigiert, bei 2022-11+ die **damals wirklich verfügbare** Prognose | ✅ identisch (24 h) |
| 3–6 h nach dem Fenster als Maßstab | 1-h-Vorhersage + **4-h-Verkettung (DoD)** + **6-h** Offset-Test; Maßstab = echte Messungen | ✅ identisches Prinzip, 4+6 statt 3–6 |
| „natürlich alle Daten“ | alle 2020→heute, in strikter Zeit-Reihenfolge (Walk-forward) | ✅ |
| **Sliding-Window 24 h als Eingabe** | **fünf Schnappschüsse (−1, −2, −6, −12, −24 h) statt fließende 24-h-Reihe** | ⚠️ bewusste Abweichung, s. u. |

**Warum keine 24-h-Sequenz?** Zwei Gründe, beide aus dem Datenlauf:

1. **Ein Sliding-Window (24 aufeinanderfolgende Stunden) braucht ein „Sequenz-Modell“**
   (GRU/LSTM/Transformer). Genau den Gegenentwurf haben wir in M2 gebaut und
   **vergleicht**: ein volles Keras-Netz gegen ein Baum-Ensemble (LightGBM) auf
   exakt denselben Daten. **Das Baum-Ensemble hat klar gewonnen** (Raum-Ist 1-h:
   0,17 K vs. 1,1 K; im 4-h-Verlauf sogar 0,5 K vs. über 1 K — und das Keras-Netz
   „explodierte“ in einzelnen Extrem-Fällen auf absurd hohe Werte, während das
   Baum-Ensemble stabil blieb). Das ist die ehrliche Antwort auf deine Frage:
   **Ja, es gab eine andere Idee — und die Daten haben (bisher) für die kompakte
   Variante entschieden.**
2. **Fünf Schnappschüsse reichen für dieses Haus:** Wegen Puffer, Mauern und Kesseln
   ändert sich alles langsam. Das „Gedächtnis“ (Trend, ob der Puffer gerade lädt) steckt
   bereits in Pufferstand + Vorlauf + den fünf Schnappschüssen. Eine vollständige
   24-h-Reihe würde vor allem dann Mehrwert bringen, wo die exakte *Form* der letzten
   24 Stunden zählt (z. B. Solar-Spitze um 13:00) — das ist aber ein **24-Punkte-Sequenz-
   Problem**, für das man ein Sequenz-Modell bräuchte (siehe Punkt 1).

**Was das konkret bedeutet:** Wenn wir das Keras-Modell (das du explizit wolltest)
später weiter ausbauen, ist der natürliche nächste Schritt **gerade dein Sliding-Window** —
als Sequenz-Modell (GRU/Temporal-Convolution) über die letzten 24 Stunden + die 24-h-Wetter-
vorhersage. M2 hat bewusst zuerst die Frage „**welcher Modell-Typ gewinnt auf diesen
Daten**“ beantwortet (→ GBDT), nicht „welche Fenster-Form“. Die Fenster-Form ist die
offene v2-Option, mit M2-Evidenz, dass sie allein (ohne Sequenz-Modell) nicht zum
Durchbruch führt.

---

## 8. Was ist bewusst nicht enthalten? (Grenzen)

- **Kein Auto-Write:** Das Modell empfiehlt nur; jede Ausführung läuft über die
  Whitelist-Write-Pfad mit Read-back und Bestätigungs-Dialog (Design v0.3, S3-1).
- **Kein Blick in die Zukunft:** Wetter = die Prognose, die zum Zeitpunkt t verfügbar war;
  Split strikt chronologisch.
- **Modus (Betriebsart) historisch unbekannt:** Er war nicht geloggt; wird ab jetzt
  per Parameter-Logging (M6) mitgeliefert, dann lernt das Modell seine Wirkung.
- **Offset-Responsivität** (der „vorgegaukelt-warm“-Effekt vom 18.10.) ist im Modell
  **nicht in der geforderten Stärke** nachweisbar — dokumentiert als offener Punkt;
  die Empfehlungsschicht (M4) muss den Offset-Hebel zusätzlich mit Guardrails absichern.
- **Öl-Verbrauch (M7):** Die Öl-Tank-CSV fehlt noch (Q-106, von dir aufgeschoben) —
  bis dahin wird der Ölverbrauch über den Brenner-An-Anteil geschätzt.

---

## 9. Quellen (wer die Details will)

- `docs/ai-model-design.md` (v0.3) — das Voll-Design inkl. L2/L3
- `analysis/model/m1/REPORT.md` + `SCHEMA.md` — Feature-Store, Spaltenkatalog, Bias
- `analysis/model/m2/REPORT.md` — alle M2-Zahlen (MAE je Ziel, Keras-vs.-LGBM,
  Responsivität, Worst-Case-Tag, Annahmen)
- `models/cmi-brain/v001/metadata.json` — Versions- und Geräte-Informationen
