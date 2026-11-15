# UVR1611-Heizkreisregler — CMI-setzbare Parameter (Seite 6) & Zielbild „VL so niedrig wie möglich“

Stand: 2026-11-07 (statische Analyse der neuen CMI-Web-Dateien in Nextcloud; **Live-Check ausstehend** — Seite 6 noch nicht auf den CMI geladen).
Quellen: `house-heating/CMI-Web/CMI-WEB/CMI-WEB.xml` (Seite_5/UID=6), `CMI/6.cgi` (26 `loadChanger`), `CMI/config.tas` (36 unique Adressen), `20261107_owner.tdw` (Parameter-Werte), `20261107_owner.dat`, `UVR610_32_20261107.tdw`.
Inventar: `scripts/cmi_write_discovery/INVENTAR.json` (36 Einträge, 16 RISK) + `INVENTAR.md` (Tabelle).

## 1. Hintergrund

maintainer hat am 2026-11-07 (a) beide UVR-Programme neu kompiliert (UVR1611: Logik **unverändert**, nur TA-Designer 1.0.20-Re-Compile; UVR610: Minderer jetzt **statisch** über `Fixwerte`-Block, aktuell exakt die alten Werte → keine verändernde Wirkung diese Nacht), (b) das CMI-Web-Layout neu exportiert (TA-Designer 1.31, 6 Seiten, `Highest_ID=6`, neue `.dat`-Referenzen `dat_numb=2` UVR610 / `dat_numb=3` UVR1611) und (c) **Seite 6 „C.M.I. 6“ (1024×768)** hinzugefügt, auf der alle setzbaren Heizkreisregler-Parameter der UVR1611 liegen (26 `loadChanger`-Elemente).

Zielbild (L2 v0.5, maintainer-Beauftragung): **VL-Soll so niedrig wie möglich**, bei ΔT-Optimierung (EG heute ~4,1 K statt Ziel 10 K). Beobachtungsfenster O8-Pumpe (Drucksteuerung, ab 2026-11-07 15:35) + Minderer-statisch: Auswertung **2026-11-08 ~08:00** (steht in agentmemory `pending_items`).

## 2. Adresse-Format (verifiziert)

Display-Adresse = `1` + `AA` (CAN-Knoten) + `BBBB` (CAN-Index, hex) + `CC` (Subindex) + `80`.
Beleg: `101301D0180` → Knoten `1`, Index `301D` = 12317 (live 2026-11-02, No-Op-Write) + alle 26 Seiten-6-Adressen dekodieren konsistent auf die XML-Bezeichnungen.
Knoten: `01` = UVR1611 (dev_id 128), `00` = UVR610/x2 (dev_id 145), `02` = UVR16x2 (dev_id 128, Solarpumpe-Modus `00409120`).

## 3. Die 26 setzbaren Heizkreisregler-Parameter (Seite 6)

Aktuelle Werte aus `20261107_owner.tdw` (unverändert gegenüber 20211020):

| # | CMI-Adresse | CAN-Idx | Parameter | tdw-Wert | L2-Risiko |
|---|---|---|---|---|---|
| 1 | `10130290180` | 12329 | Fg Heizkreis (Freigabe) | aktiv | **RISK** (Ausschalten stoppt Heizkreis) |
| 2 | `101302E0180` | 12334 | Fg Pumpe (Freigabe) | aktiv | **RISK** (Ausschalten stoppt Heizkreispumpe) |
| 3 | `10130330180` | 12339 | Fg Mischer (Freigabe) | aktiv | **RISK** (Ausschalten stoppt Mischer) |
| 4 | `101301D0180` | 12317 | Betriebsart Modus | 3 (NORMAL) | ok (live 2026-11-02) |
| 5 | `101300D0180` | 12301 | **T.raum ABSENK** (Raumsoll) | **16 °C** | gering (±0,5 K) |
| 6 | `101300C0180` | 12300 | **T.raum NORMAL** (Raumsoll) | **20 °C** | gering (±0,5 K) |
| 7 | `10130470180` | 12359 | Vorhaltezeit (bez. −10 °C) | 30 | gering |
| 8 | `101300A0180` | 12298 | **Raumeinfluss (%)** | **70** | mittel (Kurvengeschmäck) |
| 9 | `101300B0180` | 12299 | Einschaltüberhöhung | 0 | gering |
| 10 | `101300F0180` | 12303 | **VL bei +10 °C** (Kurvenpunkt 1) | **45 °C** | gering (±2 K) |
| 11 | `10130100180` | 12304 | **VL bei −20 °C / Steilheit** (Kurvenpunkt 2) | **65 °C** | gering (±2 K) |
| 12 | `10130110180` | 12305 | **VL-Max** | **70 °C** | gering (nur senken) |
| 13 | `10130120180` | 12306 | VL-Min | 25 °C | **RISK** (Heraufsetzen = Mindest-Heizleistung) |
| 14 | `10130460180` | 12358 | Aussen-Schwelle Frostschutz | 5 °C | **RISK** (Frosteinbruch) |
| 15 | `10130450180` | 12357 | Raumsoll Frostschutz | 5 °C | **RISK** (Frosteinbruch) |
| 16 | `10130050180` | 12293 | Abschaltung Raumtemp (HyRTAb) | 1 | **RISK** (Abschalt-Logik) |
| 17 | `10130060180` | 12294 | Abschaltung VLSOLL<MIN (HyVLAb) | 20 | **RISK** (Abschalt-Logik) |
| 18 | `10130070180` | 12295 | Abschaltung Aussen (SchwATAb) | 19 °C | **RISK** (Abschalt-Logik) |
| 19 | `10130160180` | 12310 | Schwellwert AT Abschaltung (HyATAb) | 10 °C | **RISK** (Abschalt-Logik) |
| 20 | `10130080180` | 12296 | Abschaltung AT im Absenkbetrieb (SchwAbAb) | 15 °C | **RISK** (Abschalt-Logik) |
| 21 | `10130180180` | 12312 | Schwellwert AT Absenk (HyAbAb) | 10 °C | **RISK** (Abschalt-Logik) |
| 22 | `101301C0180` | 12316 | Abschaltung VL-IST>MAX (VLMaxAb) | 1 | **RISK** (Abschalt-Logik) |
| 23 | `10120210180` | 8225 | Zeitprogramm Wochentagszuordnung | wt | ok |
| 24 | `10120310180` | 8241 | Zeitfenster 1 EIN (Min) | 36 (06:00) | ok |
| 25 | `10120410180` | 8257 | Zeitfenster 1 AUS (Min) | 135 (22:30) | ok |
| 26 | `10120510180` | 8273 | Zeitfenster 1 Solltemperatur | ? | ok |

### Unbekannte Adressen (nur in `config.tas`, NICHT auf Seite 6)

| CMI-Adresse | CAN-Idx | Status |
|---|---|---|
| `101301F0180` | 12319 | Bedeutung offen. Vermutung „T.raum Absenk“ **widerlegt** (das ist 12301). In `owner.xml` (CAN-Touch) und Seite 6 nicht vorhanden → vermutlich alternative Raum-Soll-Register (z. B. pro Zeitfenster). **RISK=true, nie automatisieren** bis Live-Check (`changer.cgi?sadr=…`) die Bedeutung klärt. |
| `10130200180` | 12320 | wie 12319. |

## 4. L2-Zielbild: die 4 Hebel für „VL so niedrig wie möglich“

| Hebel | Parameter (Adresse) | Wirkung | Begrenzung für Automatisierung |
|---|---|---|---|
| 1 (stärkster) | T.raum Normal/Absenk (`12300`/`12301`) | Raum-Soll senken → Kurve verschiebt sich bei gleicher AT automatisch flacher | ±0,5 K-Schritte, min. 18 °C Normal / 14 °C Absenk (Komfort-Boden), RAS-Intent-Abgleich |
| 2 | Kurvenpunkte (`12303`/`12304`) | Kurve direkt verschieben, wirkt nur auf Grundregelung | ±2 K, beide Punkte gleichsinnig (Steilheit erhalten) |
| 3 | VL-Max (`12305`) | Deckel auf VL-Soll | nur senken, mind. +5 K über Max-Kurvensoll |
| 4 | Raumeinfluss (`12298`) | RAS-Einfluss auf Kurve modifizieren | 60–80 %, erst wenn RAS-Offsets nicht ausreichen |

**Kein AT-Offset nötig:** die frühere Idee (T.auss. + Offset) wird obsolet, da die Kurve/Raum-Soll direkt setzbar sind. `skorr` im `.tdw` bleibt 0 (wirkt auf alle AT-Funktionen inkl. Abschalt-/Frostschutz-Logik → Nebenwirkungen).

**L2 v0.6-Ausbau (Kurskorrektur):** ab dem 2026-11-07-Upload sind die UVR610-Minderer-Ventile **statisch** (`Fixwerte`: eanr 1/2/3 = 1000/100/0, Freigabe = binäre 20 °C-RAS-Regel, aktuell unverändert). Die m1.3-Annahme `hzrkl_{z}_rule` (binäre 20,1-°C-Proxy, Korrelation ≥0,999) gilt ab dieser Nacht nur noch als Proxy; die **Minderer-Vorgaben (Fixwerte) werden in v0.6 zur eigentlichen Aktionsgröße** (Lernstart: L2 empfiehlt neue Fixwerte statt binärer Regel).

## 5. UVR610-Änderung (2026-11-07, Kontext)

- Neuer `Fixwerte`-Block: `fix nr=1 fixw=1000`, `nr=2 fixw=100`, `nr=3 fixw=0`, `fgrana=24`.
- Minderer-Mathematik-Module beziehen `EVarA` jetzt `qu=4` (Fixwert eanr 1/2/3) statt `qu=0` (Regelwert).
- Die binären 20,0-°C-Vergleiche (ch10/12/14 vs. 200) bleiben als **Freigabe**.
- maintainer: Werte sind aktuell identisch mit dem bisherigen Verhalten → diese Nacht ohne Änderung.
- **Offen (bitte klären):** (a) Zuordnung fix-nr → Kreis (eanr 1/2/3 = Keller/EG/DG?), (b) `fgrana=24`-Skalierung (1000 = 10 V?), (c) `nr=2` max=30000 (andere Einheit?).

## 6. Post-Upload-Verifikation (read-only, HITL-gated, nach „Upload fertig“)

1. `/api/v1/state` (Gateway, umgeht 1/min-JSON-Limit): JSON-Node 1+32 → UVR1611-Labels unverändert, Minderer a10–a12 = Fixwerte, kein CAN-Fehler.
2. `changer.cgi?sadr=…` für die 26 Seiten-6-Adressen (nur Changer-Dialog-Auflösung, **kein** Write): bestätigt `changeadr`, option_type (numeric/select), min/max, aktuellen Wert → INVENTAR `verified: pending-live` → `live-2026-11-07`.
3. `changer.cgi?sadr=101301F0180` + `…200180`: Bedeutung von 12319/12320 klären (open question).
4. Danach: INVENTAR aktualisieren + dieses Doku-Kapitel abhaken; L2-Executability-Ticket (Q-108) kann von der parallelen Session auf `cmi-settable` (Klasse: Raumsoll/Kurve) umgestellt werden — **nach** Live-Verifikation, nicht vorher.

## 7. Safety (gilt für L2-Automatisierung)

- **Hart blockiert (INVENTAR `risk=true`, `writeable=false`):** 10520A10180 (Ölbrenner), 12329/12334/12339 (Freigaben), 12306 (VL-Min), 12357/12358 (Frostschutz), 12293–12296/12310/12312/12316 (Abschalt-Schwellen), 12319/12320 (unbekannt).
- **L2-Kandidaten (geringes Risiko):** 12300/12301 (Raum-Soll), 12303/12304 (Kurve), 12305 (VL-Max), 12298 (Raumeinfluss) — mit Wertebegrenzung (Kap. 4) und HITL je Change (S3-1-Pattern, wie O8-Modus).
- JSON-API bleibt 100 % read-only; Writes laufen ausschließlich über `change.cgi` (S3-1-Write-Pfad, Whitelist + RISK-Flag + Read-back).
