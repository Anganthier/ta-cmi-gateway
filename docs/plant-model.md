# TA example plant „Am Example-Street 1" — Anlagenstrukturmodell (Plant Structure Model)

> **Kanon-Dokument** für das CMI-Gateway-Projekt (`<repo-root>`).
> Maschinennlesbare Zwilling: [`plant-model.json`](./plant-model.json) (für UI + S6-Feature-Engineering).
> Extraktion: 2026-10-27 (Read-only-Subagent, kein CMI-Zugriff).
> Konfidenz-Skala: **hoch** = .tdw + WinSol-Setup.xml + (ggf.) CMI-WEB-2022 + live-Parquet-Timeline deckungsgleich · **mittel** = zwei Quellen deckungsgleich · **niedrig** = Widerspruch/reine Ableitung.

---

## 0. Quellen & Datenstempel (welche Datei für welchen Schluss)

| Datei | Ära | Rolle in dieser Analyse |
|---|---|---|
| `house-heating/20211020_owner.tdw` | 2021-10-20 | UVR1611-Hauptprogramm (CAN-Node 1), TA-Designer v1.0.14. **Menschenlesbare Struktur** (Cameloen-Zeichnung, `<uvr block>`: Eingaenge/Ausgaenge/CanDatLog/BDBListe/Netzwerk). |
| `house-heating/UVR610_32_20211029.tdw` | 2021-10-29 | UVR610-Programm (CAN-Node 32), Solar + RAS-Logger. |
| `house-heating/20220918_owner.dat` | **2022-09-18** | RAW-EEPROM-Image (D1.00) der UVR1611 = **aktuelles Programm-Image auf dem Gerät**. |
| `house-heating/UVR610_32_20220918.dat` | **2022-09-18** | RAW-EEPROM-Image (D1.00, FW T1.19) der UVR610. |
| `house-heating/owner_backup.tdw` | ~2014 | Alt-/Referenz-Variante (v1.0.0), BDB-Labels noch leer. |
| `house-heating/owner.xml` / `owner-backup_01042014.xml` | 2017-01 / 2014-04 | CAN-Touch-Layout; **bestätigt die a1..a10-Kanalbelegung der UVR1611** (`Bezeichnung="...T.SP.oben"` mit `can_index=8333 subindex=1..10`). |
| `house-heating/CMI/{1.cgi,1.html,1.png,config.tas}` | 2015–2021 | CMI-Startseite-Layout (alt, 1200×900) — posN-Koordinaten + `config.tas`-Adressliste. |
| `CMI_WebTest/CMI-WEB/CMI-WEB.xml` | **2022-09** | **Aktuelles CMI-Seiten-Layout (1600×1200) — authoritativ für die 9 Setzelemente (CAN-idx/sub)**. |
| `WinSol .../example-plant/Setup.xml` | 2023-10 | WinSol-Label-Modell aller 3 Logger-Geräte (`a1..a16`, `d1..d13` je Gerät). |
| `discovery/json-api-doc.txt` | 2025-10 | CMI JSON-API v8 Doku (Unit-Mapping, `La/Ld`, RAS-State 0..3). |
| `discovery/la_ld_node32.json`, `live_{1..5}.cgi` | 2026-10-26 | Live-Abgleich: Node-32 `La/Ld` + die 5 Schematic-Seiten. |
| `data/winsol_pq/**.parquet` | 2020–2026 | WinSol-Bestandsdaten — **historische Belegung / Inbetriebnahmen** (`count IS NOT NULL`). |

**Staleness-Kernaussage (kurz):** Die 2021er `.tdw`-Dateien sind die *menschenlesbare* Struktur; die 2022-09-18 `.dat`-Images sind das *aktuelle* Programm auf den Geräten. Die **Kanal-/BDB-/Setz-Belegung ist zwischen `.tdw` (2021) und `.dat` (2022-09-18) strukturell identisch** (BDB-Labels der UVR1611 1:1, UVR610-Labels 6→9). Einzige nachvollziehbare Differenz: **UVR610-Firmware T1.18 → T1.19** und **drei neue UVR610-BDB-Labels** (`Keller/EG/OG HzRkl Ventil`) im 2022er-Image. Details in §6.

---

## 1. Anlagenübersicht (Komponenten + Verschaltung)

```
                        [Aussen]──> a8
                                      │
   ┌──────────────────────────────────┴─────────────────────────────────────┐
   │                         UVR1611 (CAN-Node 1)  – Hauptsteuerung         │
   │  · heizt Speicher/WW/Heizkreis, Mischkreis, Oelbrenner-Grundregelung   │
   │  · Logging: dev0 (DS1 Heizung) + dev1 (DS2 RAS)                        │
   └────────────────────────────────────────────────────────────────────────┘
        │(CAN: auge 1..16, DS)          │(CAN: A10..A15 = RAS-Raumtemperaturen)
        │                                ▼
   ┌──────────────┐   ┌────────────┐  ┌───────────────────────────────────────┐
   │ Holzkessel   │──>│  Puffer-   │  │  UVR610 (CAN-Node 32)  – Solar+RAS     │
   │ (Festbrennst)│   │  speicher  │  │  · dev2 (DatLog): Speicher/Kollektor + │
   │ a9 Wassertank,│   │ Oben/Mitte │  │    RAS(Keller/EG/OG) + Ventil-Ergebn.  │
   │ Abgas        │   │ Unten      │  │  · O1 = Solarpumpe                     │
   └──────────────┘   │  + WW-Boiler│ └────────────────────────────────────────┘
          │           └────────────┘            │(a16 Kollektor, a1..a3 Speicher)
          │ Abgas (a10)        │                │
          ▼                    ▼                ▼
   [Abgas-Schorn-]   [Warmwasser]        [Kollektor → Solarpumpe → Speicher]
      stein            a4 WW, O2/O6/O7         (dev2 a16 T.Kollektor)
   (Seite 5 Kamin)    (Ladepumpe + Zirkulation)

   Heizkreis RL (Mischkreis) : a6 VL / a7 RL, O8 Pumpe HzKl, O3/O4 Misch Auf/Zu, O9 Ventil HzRL
   · 3 RAS-Räume: Keller / EG / OG  (dev1: Offset/Temp/Minuten · dev2 a4..a9: RAS+Temp)
   · O5 = Anfoerdung Oelbrenner
   Oelkessel: a5 Oelkessel-VL, O5 Brenner-Anforderung
```

**Komponentenliste** (IDs = `plant-model.json:components`):

| ID | Komponente | Typ | Gemessen (dev0) | Ausgänge (dev0 D) | tdw-Beleg |
|---|---|---|---|---|---|
| `kessel_holz` | Festbrennstoff-/Holzkessel | Boiler | a9 Holzkessel VL, a10 Abgas | O1 Pumpe Holzofen | Cameloon „Festbrennstoffkessel" `1632559195079233`; `ein nr=9/10`; BDB bdb6 „Holz CNT"; Seite 5 |
| `kessel_oel` | Ölkessel / Ölbrenner | Boiler | a5 Ölkessel VL, d5 Anfoerdung | O5 Anfoerdung Ölbrenner | `ein nr=5` (bgrp=1 bnr=1); BDB bdb5 „Oel CNT"; Seite 1 (rechter Kessel) |
| `speicher_puffer` | Pufferspeicher (900 L) + WW-Boiler | Speicher | a1 Oben, a2 Mitte, a3 Unten, a16 Stroemungsschalter | – | Cameloon „Speicher" `1632560038189520` (8 Wasser-Pins); `ein nr=1,2,3`; owner.xml `T.SP.oben/mitte/unten` idx8333 sub1/2/3. **Volumen 900 L (maintainer 2026-11-01)** |
| `warmwasser` | Warmwasser (WW-Boiler im Puffer) | WW | a4 Warmwasser | O2 Ladepumpe WW, O6 Ladep.WW.5 (WW vom Öl), O7 Zirkulation | `ein nr=4`; BDB bdb3 „Warmwasser", bdb7 „NL Lad WW", bdb11 „L-WW Cmp"; Seite 2 |
| `heizkreis_rl` | Heizkreis RL (Mischkreis, 3 RAS-Räume + RL-Ventil) | Heizkreis | a6 VL, a7 RL | O8 Pumpe HzKl, O3 Misch Auf, O4 Misch Zu, O9 Ventil HzRL | Cameloen „2/3 Wegeventil" + „Pumpe"; `ein nr=6/7`; BDB bdb9 „P-Heizkreis", bdb2 „Vent. HzRL"; Seiten 1/3 |
| `raum_keller` | Raum Keller (RAS) | Raum | (dev1 a2/d, dev2 a5) | – | UVR610 BDB „Keller RAS/Temp"; dev1 a2 |
| `raum_eg` | Raum Erdgeschoss (RAS) | Raum | (dev1 a5, dev2 a7) | – | UVR610 BDB „Erdgeschoss RAS/Temp"; dev1 a5 |
| `raum_dg` | Raum Dachgeschoss (RAS) | Raum | (dev1 a8, dev2 a9) | – | UVR610 BDB „Dachgeschoss RAS/Temp"; dev1 a8 |
| `solar_system` | Solarsystem (Kollektor, Solarpumpe, UVR610) | Solarthermie | dev2 a16 Kollektor, a1..a3 Speicher | dev2 d1 Solarpumpe | UVR610 `ein nr=1..4`, `aus nr=1`, DatLog; Seite 4 |
| `abgas_holz` | Abgas Holz (Schornstein) | Abgas | a10 Abgastemp | (Seite 5 pos15 digital) | `ein nr=10` (styp=1); BDB bdb4 „Abgas"; owner.xml „Abgas" idx8333 sub10 |
| `aussen` | Außen (Außensensor) | Sensor | a8 Außen | – | `ein nr=8`; owner.xml „Temp.Aussen" idx8333 sub8 |
| `uvr1611` | UVR1611 (Steuergerät, Node 1) | Controller | – | O1..O9 | `UVR1611`-Block (KontrollFunk/CanDatLog/BDBListe/Netzwerk Knot=1/Eingaenge/Ausgaenge) |
| `uvr610` | UVR610 (Steuergerät, Node 32) | Controller | dev2 (La/Ld) | Solarpumpe | `UVR610`-Block (GeraeteEinst/BDBListe/CAN Knot=32/DL/DatLog/Eingaenge/Ausgaenge/MBus) |

**Cameloen-Zeichnung (20211020_owner.tdw):** 4 physikalische Symbole im Plan — `Festbrennstoffkessel`, `Pumpe`, `Speicher`, `2/3 Wegeventil` — plus die UVR1611-/UVR610-Blöcke. Die Topologie (Pins/Wires) bestätigt: Holzkessel → (Pumpe) → Pufferspeicher → 2/3-Wegeventil (Mischkreis) → Heizkreis; Ölkessel → Puffer; Solar (UVR610, externe Übertragung) → Puffer; Abgas (Kamin) vom Holzkessel.

**Pufferspeicher-Energiebilanz (900 L, maintainer 2026-11-01 — relevant für L1-Targets `buff_mid`/`buff_bot`):** Die Puffer-Zonen ändern sich über Lade- und Entladevorgänge; entscheidend ist, *wer wie lange* pumpen lässt:
- **Laden:** Holzkessel (O1, d1 — **nicht geloggt**, Proxy über Abgas/Holz-VL), Ölkessel (O5, d5 `oel_burner_on`), Solar (dev2 d1 `pump_solar_on`).
- **Entladen:** Heizkreis (O8, d8 `pump_hz_on`), Zirkulation (O7 `pump_circ`, bedarfsabhängig; O6 `pump_ww_oil` ist an O7 gekoppelt und heizt den Zirkulationskreis über einen Wärmetauscher aus dem Puffer oben → fast identische Laufzeiten ≈ 19,8 %).
- **O2 (`pump_ww`, d2) lädt:** Öl→Puffer-oben-Ladepumpe (+5,4 K/h Puffer oben), sie entlädt nicht; Regel etwa Ölkessel-Vorlauf > Puffer oben + 24,6 K. Spaltennamen `pump_ww*` bleiben.
- **O5 Ölbrenner:** ca. 78 % des Vorhersagefehlers Puffer Mitte +4 h entfallen auf Stunden mit Ölbrenner-Aktivität (Holz-Stunden nur ≈ 2 % bzw. 13 %); der Startzeitpunkt ist aus Ist-Daten nur begrenzt vorhersagbar. Folgeschritt v004: UVR-Regeln (O5/O2/O9) als Szenario-Eingang.
- **Rücklaufweg (O9 `valve_hzrl_on`, d9):** steuert, wohin das Rücklaufwasser geht — direkt in die Ölheizung oder in den Puffer (Mischkreis). Für `buff_*` damit kausal relevant. **Bestätigt (m1.3):** O9 an = Heizungsrücklauf → Puffer, aus = Rücklauf → Ölkessel; Schaltbedingung Puffer Mitte > Heizkreis-Rücklauf + 5 K (`o9_switch_margin`); Wirkung bei laufender Heizkreispumpe: Puffer Mitte −0,73 K/h (O9 an) bzw. +0,32 K/h (O9 aus).
- **Datenlage (L1-relevant):** Alle gelogten Pumpen/Ventile stehen im Store nur als **stundenweise An-/Aus-Anteil** (`_on` = 0..1, `_last` = letzter Zustand) — das deckt „wer war an“ ab, aber **nicht die Pumpen-Geschwindigkeit** (jeweils anders, nicht geloggt). Die kumulierte Laufzeit ist **keine Datenlücke**: sie ergibt sich aus den stündlichen An-Quotienten (m1.3: `{sig}_sum{3,6,12,24}h`, `{sig}_since_h` für Ölbrenner, O1-Proxy, Holzbrand-Proxy, O2, O6, O7, Heizkreispumpe, Solarpumpe, O9). Echte Lücke bleibt nur O1 (Holzpumpe, dev0 d1 nicht geloggt) → Proxy `pump_wood_est_on`: wood_vl > 65 °C, wood_vl > buff_bot + 3 K, buff_bot < 90 °C. Die mittlere/untere Pufferzone ist deshalb der schwerste L1-Target (v002-DoD offen, `docs/l1-model-v002.md` §4.5). Laufzeitsummen, Abgas ×10 und Minderer-Regel sind in m1.3/v003 umgesetzt. Hinweis: „Fehler Puffer Mitte“ ist ein Vorhersagefehler des Modells, kein Messfehler (Fühler plausibel: 11,0–91,6 °C, 0 Sentinels, 598 Logging-Lücken-Stunden).

---

## 2. Steuergeräteeinheiten

### UVR1611 (CAN-Node 1) — Hauptsteuerung
- **Blocke im .tdw:** `KontrollFunk` (Display/Auto), `CanDatLog` (Logging: `DS1` = 16 A + 13 D, `DS2` = 16 A + 13 D), `BDBListe` (12 Events), `Netzwerk` (Knot=1, `NSB`-Send-Intervalle, `NWEA` CAN-Netz-Events), `Eingaenge` (11 `<ein>`), `Ausgaenge` (14 `<aus>`, O1..O9 + 2 analoge Ausgänge).
- **Eingaenge (`<ein nr>`)** — Analogeingangsbelegung der UVR1611 (→ dev0 a-Kanäle):
  - nr=1 (bnr=12) Speicher Oben · nr=2 (bnr=11) Speicher Mitte · nr=3 (bnr=10) Speicher Unten · nr=4 (bnr=8) Warmwasser · nr=5 (bgrp=1 bnr=1) Ölkessel-VL · nr=6 (bnr=6) Heizkreis VL · nr=7 (bnr=7) Heizkreis RL · nr=8 (bnr=15) Außen · nr=9 (bgrp=1 bnr=5) Holzkessel-VL · nr=10 (bgrp=5 bnr=4, styp=1) Abgas · nr=16 (bnr=2 bidx=5, typ=1) Stroemungsschalter.
- **Ausgaenge (`<aus nr>`)** — Digitalausgänge (→ dev0 d-Kanäle): O1 (bgrp=2) Pumpe Holzo. · O2 Ladep.WW. · O3 Mischkreis Auf (dig=3) · O4 Mischkreis Zu · O5 Anfoerdung Brenner · O6 (bnr=5 bidx=5) Ladep.WW.5/WW vom Öl · O7 (bnr=13 bidx=5, nl=10) Zirkulationspumpe · O8 Pumpe Heizkreis · O9 (bgrp=2) Ventil Heizungsruecklauf. O14..O16 inalt/dig=0 (inaktiv).
- **Logging:** `CanDatLog` `DS1` (qu=1/2) → **dev0** (Heizung); `DS2` (qu=7) → **dev1** (RAS). `Master=62`, `Timeout=60`.
- **BDBListe (12):** bdb1 „Pumpe Holzo." · bdb2 „Vent. HzRL" · bdb3 „Warmwasser" · bdb4 „Abgas" · bdb5 „Oel CNT" · bdb6 „Holz CNT" · bdb7 „NL Lad WW" · bdb8 „Urlaub" · bdb9 „P-Heizkreis" · bdb10 „RaumTemp" · bdb11 „L-WW Cmp" · bdb12 „TRICK-Net".
- **Beitragende Text-Blöcke (Raster):** „Keller", „erdgeschoss", „Dachgeschoss", „Definition der Ein- und Ausgänge", „Regelung der Zirkulation", „Statistik Werte", „Uebergabe an Externe CAN Geraete - UVR610", „Errechnen der minimalen Raumtempanforderung", „Urlaubsmodus Berechnung".

### UVR610 (CAN-Node 32) — Solar + RAS-Logger
- **Blocke im .tdw:** `GeraeteEinst` (FachCode/ExpCode 509050), `BDBListe` (2021er: 6 Labels „Keller/EG/OG RAS+Temp"; 2022er `.dat`: + „Keller/EG/OG HzRkl Ventil" = 9), `CAN` (Knot=32, `nwe A1..A3` lokale Speicher-PT1000, `nwe A10..A15 knot=1` RAS-Raumtemperaturen **vom UVR1611 über CAN**), `DL` (Datenlogger A1..A3), `DatLog` (A1..A16 + D1..D64, Interv=30 s), `Eingaenge` (nr=1..3 PT1000 Speicher, nr=4 PT1000 Kollektor skorr=-7), `Ausgaenge` (nr=1 = O1 Solarpumpe, awa/awe PWM-Parameter), `MBus` (inaktiv).
- **Rolle:** erzeugt die `dev2`-Logging-Spur (JSON-API Node-32 `La`=16 A + `Ld`=1 D). RAS-Raumtemperaturen (dev2 a4..a9) werden **nicht lokal gemessen**, sondern über CAN von Node 1 (UVR1611) gezogen.
- **Note zur Quelle „x2":** In `memory.md`/`Plan.md` steht „UVR610 Node 32 (x2, Solar HzRl, RAS-Logger)". Es ist **ein** UVR610 @ Node 32 mit Doppelrolle (Solar-Heizkreislauf + RAS-Logger), nicht zwingend zwei physische Geräte. (Setup.xml zeigt genau ein Dev auf Node 32.)

---

## 3. Kanal-Belegungsplan (SCHLÜSSELTABELLE)

Device-Namespace (WinSol-Long): `winsol:0` = dev0 · `winsol:1` = dev1 · `winsol:2` = dev2. JSON-API: `json:1` = UVR1611, `json:32` = UVR610.

### 3.1 dev0 = `winsol:0` — Heizung / UVR1611 DS1 (Node 1)
**Quelle:** UVR1611 `<uvr block=Eingaenge/Ausgaenge>` + `CanDatLog DS1` (qu=1/2). **Bestätigt durch:** WinSol-Setup.xml `DevBez="Heizung"` (1. Dev) + owner.xml (idx8333 sub1..10) + live `api.cgi` Node-1. **Datenseit:** Logger-Start **2020-08-30** (Parquet).

| Kanal | Bedeutung | Komponente | Einheit | tdw-/Setup-Beleg | Konfidenz |
|---|---|---|---|---|---|
| A1 | Speicher Oben | `speicher_puffer` | °C | `ein nr=1`; Setup a1 „Speicher Oben"; owner `T.SP.oben` sub1 | hoch |
| A2 | Speicher Mitte | `speicher_puffer` | °C | `ein nr=2`; Setup a2; owner `T.SP.mitte` sub2 | hoch |
| A3 | Speicher Unten | `speicher_puffer` | °C | `ein nr=3`; Setup a3; owner `T.SP.unten` sub3 | hoch |
| A4 | Warmwasser | `warmwasser` | °C | `ein nr=4`; Setup a4; owner `T.Warmwasser` sub4 | hoch |
| A5 | Oelkessel Vorlauf | `kessel_oel` | °C | `ein nr=5` (bgrp=1 bnr=1); Setup a5; owner `T.Oelkess.VL` sub5 | hoch |
| A6 | Heizkreis Vorlauf | `heizkreis_rl` | °C | `ein nr=6`; Setup a6; owner `T.Heizkr.VL` sub6 | hoch |
| A7 | Heizkreis Ruecklauf | `heizkreis_rl` | °C | `ein nr=7`; Setup a7; owner `T.Heizkr.RL` sub7 | hoch |
| A8 | Temperatur Aussen | `aussen` | °C | `ein nr=8`; Setup a8; owner `Temp.Aussen` sub8 | hoch |
| A9 | Holzkessel Vorlauf | `kessel_holz` | °C | `ein nr=9` (bgrp=1 bnr=5); Setup a9; owner `T.Holzk.VL` sub9 | hoch |
| A10 | Abgastemperatur Holz | `abgas_holz` | °C | `ein nr=10` (bgrp=5 bnr=4, styp=1); Setup a10; owner `Abgas` sub10. **Korrektur 2026-11-01 (maintainer): geloggt = 1/10 der echten Temperatur — echtes Abgas = Rohwert ×10** (×10 = Teiler-Bauteil, vom Besitzer bestätigt; kalt Roh ~3 = ~30 °C; live 01.11 Anzeige 23,2 °C = 232 °C). **Die extremen Rohwerte (Roh-Max 251 → „2.510 °C“) sind KEIN Beleg, sondern Fühlerfehler:** TA-Sentinels 3276,7/−999,9 ab 2021-12-15 und Kurzschlussfenster 2022-04-28 10:00 bis 2022-05-06 14:00. Realistisch (×10): Median ≈ 96 °C, P99 ≈ 360 °C, laut Besitzer max. ca. 330–360 °C; Gültigkeitsgrenze 500 °C (darüber = Fühlerfehler, außerhalb des Fensters nur 4 Rohproben); m1.3-Store: Stundenmittel max. 342,9 °C, `flue_last` max. 430 °C; Holzkessel-Vorlauf max. 97 °C, typisch ≤ ~80 °C. Gilt für winsol_pq + cmi_log_pq + CMI-Anzeige. Umgesetzt in m1.3: `flue` ×10, Fenster maskiert, > 500 °C → NULL | hoch |
| A11–A14 | (unused) | – | – | Setup a11..a14 „-----" | hoch |
| A15 | Test | – | °C | Setup a15 „Test" | niedrig |
| A16 | Stroemungsschalter | `speicher_puffer` | Aus/EIN | `ein nr=16` (bnr=2 bidx=5, typ=1); Setup a16 | mittel |
| D1 | Pumpe Holzofen | `kessel_holz` | – | `aus nr=1` (O1, bgrp=2); BDB bdb1; owner `Pumpe Holzo.` sub1. **Datenseitig NICHT geloggt** (LogX-Schema dev0 startet bei d2; cmi-log-protocol: nur dev0 d2–d9 + dev2 d1 im Record) → Holzpumpen-Laufzeit nicht im Datenbestand; Proxy `pump_wood_est_on` (wood_vl > 65 °C, wood_vl > buff_bot + 3 K, buff_bot < 90 °C) | hoch |
| D2 | Pumpe WW zum Puffer (Ladepumpe WW) | `warmwasser` | – | `aus nr=2` (O2); BDB bdb3/7; owner `Ladep.WW.` sub2 | hoch |
| D3 | Mischkreis Auf | `heizkreis_rl` | – | `aus nr=3` (O3, dig=3) | hoch |
| D4 | Mischkreis Zu | `heizkreis_rl` | – | `aus nr=4` (O4, dig=0) | hoch |
| D5 | Anfoerdung Oelbrenner | `kessel_oel` | – | `aus nr=5` (O5); BDB bdb5; owner `Anf.Brenner` sub5 | hoch |
| D6 | Ladep.WW.5 / WW vom Öl | `warmwasser` | – | `aus nr=6` (O6, bnr=5 bidx=5); owner `Ladep.WW.5` sub6 | hoch |
| D7 | Zirkulationspumpe | `warmwasser` | – | `aus nr=7` (O7, bnr=13 bidx=5, nl=10); owner `Pumpe-Zirku` sub7 | hoch |
| D8 | Pumpe Heizkreis | `heizkreis_rl` | – | `aus nr=8` (O8); owner `Pumpe-Hzkr` sub8 | hoch |
| D9 | Ventil Heizungsruecklauf | `heizkreis_rl` | – | `aus nr=9` (O9, bgrp=2); BDB bdb2; owner `Vent. HzRL` sub9 | hoch |
| D10–D13 | (unused) | – | – | Setup d10..d13 „-----" | hoch |

### 3.2 dev1 = `winsol:1` — RAS / UVR1611 DS2 (Node 1)
**Quelle:** UVR1611 `CanDatLog DS2` (qu=7). **Bestätigt durch:** WinSol-Setup.xml (2. Dev, `DevBez="Heizung"`, Knt=1 Ds=2). **Datenseit:** Logger-Start **2020-08-30** (Parquet) — **NICHT erst seit UVR610!**

> ⚠️ **Korrektur zur Aufgabenstellung:** dev1 wird von der **UVR1611 (Node 1)** geloggt, nicht von der UVR610. Es ist der *zweite* Logging-Block (DS2) der UVR1611. Die RAS-Raumtemperaturen stammen aus den RAS-Modulen/Kanälen, die die UVR1611 auf dem CAN-Bus sieht (BDB bdb10 „RaumTemp"). Die UVR610 (dev2) loggt dieselben RAS-Werte *zusätzlich*, gezogen über CAN von Node 1.

| Kanal | Bedeutung | Komponente | Einheit | Setup-Beleg | Konfidenz |
|---|---|---|---|---|---|
| A1 | Offset Keller | `raum_keller` | K | Setup a1 „Offset Keller" | mittel |
| A2 | Temp Keller | `raum_keller` | °C | Setup a2 „Temp Keller" | hoch |
| A3 | (ANALOG 3 / unused) | – | – | Setup a3 „ANALOG 3" | niedrig |
| A4 | Offset Erdgeschoss | `raum_eg` | K | Setup a4 | mittel |
| A5 | Temp Erdgeschoss | `raum_eg` | °C | Setup a5 | hoch |
| A6 | (ANALOG 6 / unused) | – | – | Setup a6 | niedrig |
| A7 | Offset Dachgeschoss | `raum_dg` | K | Setup a7 | mittel |
| A8 | Temp Dachgeschoss | `raum_dg` | °C | Setup a8 | hoch |
| A9 | (ANALOG 9 / unused) | – | – | Setup a9 | niedrig |
| A10 | (ANALOG 10 / unused) | – | – | Setup a10 | niedrig |
| A11 | Keller Minuten | `raum_keller` | Std | Setup a11 „Keller Minuten". **Korrektur 2026-10-31 (maintainer): Datenalter = Zeit seit letzter Funkübertragung des RAS-Elements (Sägezahn, +0,1 je Minute, Reset ≈ 11 min), keine Heiz-/Brennerminuten.** M1 `m1.1`: `ras_age_kell` (Stunden-MAX) | hoch |
| A12 | EG Minuten | `raum_eg` | Std | Setup a12 — Datenalter RAS EG (wie A11) → `ras_age_eg` | hoch |
| A13 | DG Minuten | `raum_dg` | Std | Setup a13 — Datenalter RAS DG (wie A11) → `ras_age_dg` | hoch |
| A14–A16 | (unused) | – | – | Setup a14..a16 | niedrig |
| D1–D13 | (DIGITAL n / unused) | – | – | Setup d1..d13 „DIGITAL 1..13" | niedrig |

**RAS-Regel-Semantik (maintainer 2026-11-01, Rosetta-geprüft am 31.10.2026 09:24:06,
Screenshot `docs/Screenshots/`):** Die RAS arbeitet zur Referenztemperatur **20 °C**
(Regelkreis: Soll = 20 °C Norm / Absenk 16 °C). `a1/a4/a7` („Offset …") zeigt die
**perzipierte Temperatur = Ist + Offset** (Setup-Label „Offset" ist irreführend —
Wert ist °C, kein K-Delta); `a2/a5/a8` = gemessene Ist. Beispiel (Screenshot, Keller):
Ist a2 = 20,5 °C, perzipiert a1 = 24,0 °C → Offset = +3,5 K → die Anlage zählt
den Keller um 3,5 K „zu warm" und senkt die **eigentliche Zieltemperatur auf dem
Ist auf 20 − 3,5 = 16,5 °C** (bei Absenk entsprechend). Der Offset ist damit die
Stellgröße je Etage (Q-110: Wirkung über den Rücklaufminderer der Etage).
Store-Rosetta 09:24:06 exakt: dev1 a1/a2 24,0/20,5 · a4/a5 20,2/22,1 · a7/a8 20,4/19,0 ·
dev0 a8 Außen 5,7 (Screenshot 2/AnaX = UVR1611 DS2 = `winsol:1`/dev1; 3/AnaX = UVR610 = `winsol:2`/dev2 —
**a10–a12 bedeuten auf dev1 und dev2 Unterschiedliches**: dev1 a10 unused, dev2 a10–a12 = Minderer).

### 3.3 dev2 = `winsol:2` — Solar + RAS / UVR610 (Node 32)
**Quelle:** UVR610 `DatLog` A1..A16 / D1. **Bestätigt durch:** WinSol-Setup.xml `DevBez="UVR610 - Solar HzRl"` (3. Dev, Knt=32) + `discovery/la_ld_node32.json` (Node-32 `La` 16 A + `Ld` 1 D). **Datenseit analog:** **2021-10-31** (Parquet) · **datenseit digital (d1 Solarpumpe): 2022-03-24** (Parquet, LogX P=137).

| Kanal | Bedeutung | Komponente | Einheit | UVR610-DatLog-/Setup-Beleg | Konfidenz |
|---|---|---|---|---|---|
| A1 | T.Speicher oben | `speicher_puffer` | °C | `ein nr=1` (PT1000 bnr=6); DatLog A1 (eanr=1); Setup „1: T.Speicher oben" | hoch |
| A2 | T.Speicher mitte | `speicher_puffer` | °C | `ein nr=2` (bnr=8); DatLog A2 (eanr=2) | hoch |
| A3 | T.Speicher unten | `speicher_puffer` | °C | `ein nr=3` (bnr=7); DatLog A3 (eanr=3) | hoch |
| A4 | Keller RAS | `raum_keller` | °C/RAS | CAN A10 (knot=1 ausg=10); Setup „10: Keller RAS 1" | hoch |
| A5 | Keller Temp | `raum_keller` | °C | CAN A11 (ausg=11); Setup „11: Keller Temp 1" | hoch |
| A6 | Erdgeschoss RAS | `raum_eg` | °C/RAS | CAN A12 (ausg=12); Setup „12: EG RAS 1" | hoch |
| A7 | Erdgeschoss Temp | `raum_eg` | °C | CAN A13 (ausg=13); Setup „13: EG Temp 1" | hoch |
| A8 | Dachgeschoss RAS | `raum_dg` | °C/RAS | CAN A14 (ausg=14); Setup „14: DG RAS 1" | hoch |
| A9 | Dachgeschoss Temp | `raum_dg` | °C | CAN A15 (ausg=15); Setup „15: DG Temp 1" | hoch |
| A10 | Keller HzRkl Ventil 2 Ergebnis | `raum_keller` | V (0–10) | DatLog A10 (qu=3 fid=1); Setup „2: Keller HzRkl Ventil 2 Ergeb“. **Semantik 2026-11-01 (maintainer): Rücklaufminderer Keller — 0 V = zu, 10 V = 100 % offen** (analog 0–10 V, Schritte dazwischen möglich; CMI-Anzeige „0,00 V“/„10,00 V“). Aktuell regelt die Anlage binär 0/10 V (Messbefund: 2025 77 % 0 / 23 % 10 / 0 % dazwischen) | hoch |
| A11 | EG HzRkl Ventil 2 Ergebnis | `raum_eg` | V (0–10) | DatLog A11 (qu=3 fid=3); Setup „4: EG HzRkl Ventil 2 Ergebnis“ — wie A10, Etage EG | hoch |
| A12 | OG HzRkl Ventil 2 Ergebnis | `raum_dg` | V (0–10) | DatLog A12 (qu=3 fid=5); Setup „6: OG HzRkl Ventil 2 Ergebnis“ — wie A10, Etage OG | hoch |
| A13–A15 | HzRkl Ventil 1 Keller/OG/EG | `raum_keller/og/eg` | **°C (Temperatur)** | Setup „1/2/3: … HzRkl Ventil 1“. **Klarstellung 2026-11-01 (Q-112 gelöst, JSON-API json:32 ch13–15 = Einheit °C, Werte ~18–61):** eine **Temperatur-Messung** am Minderer (Verd. Misch-/Rücklauf-Ort), **keine Ventilstellung**. Korreliert **positiv** mit Heizkreis-Vorlauf (+0,81 / +0,93 / +0,88) und Ventil 2 (a10, +0,61). Minderer-Regel: offen < 20,1 °C perzipiert, zu > 20,1 °C, bei Gleichstand halten (Korrelation 0,999); m1.3 leitet `hzrkl_{z}_rule` ab. Feature `hzrkl_*_pos` (m1.2) = diese Temperatur; Name „pos“ bleibt aus Kompatibilitätsgründen, Einheit ist °C. Live über JSON-API (`history.db`, json:32 ch10–12, seit 27.10.); `cmi_log_pq` enthält a10–a12 nicht (Decoder ordnet bewusst nur 36 Analogkanäle zu), ob die Roh-/LOG-Daten sie enthalten, ist **offen** (Q-112b nicht dringend) | hoch |
| A16 | T. Kollektor | `solar_system` | °C | `ein nr=4` (PT1000 bnr=1, skorr=-7); DatLog A16 (eanr=4); Setup „T. Kollektor" | hoch |
| D1 | Solarpumpe | `solar_system` | – | `aus nr=1` (O1); DatLog D1; Setup d1 „Solarpumpe". **Datenseit: 2022-03-24** | hoch |
| D7–D10 | (BDB „Ventil-HzRL 1/2/3", „Solarpumpe 1" — **nicht aktiv geloggt**) | – | – | Setup d7..d10, aber DatLog D2..D10 `eanr=0` (inaktiv) | niedrig |

> **Hinweis:** JSON-API Node 32 liefert praktisch nur 1 digitales Logging-Kanal (`Ld` d1 = Solarpumpe). Die Setup-Labels d7..d10 sind BDB-Namen, die im `DatLog` aber nicht aktiv geschaltet sind.

### 3.4 Doppelte RAS-Belegung (dev1 + dev2)
| Quelle | RAS-Kanäle | Gilt ab | Anmerkung |
|---|---|---|---|
| **dev1** (UVR1611 DS2) | Offset/Temp/Minuten Keller/EG/OG (a1,a2,a4,a5,a7,a8,a11,a12,a13) | **2020-08-30** (Kanäle geloggt; **RAS-Sensoren physisch erst ab 2021-10-20 verbaut** — Werte davor ungültig, maintainer 2026-10-31) | UVR1611-eigene RAS-Logspur (Korrektur: nicht UVR610). |
| **dev2** (UVR610) | RAS+Temp Keller/EG/OG (a4..a9) + Ventil-Ergebnisse (a10..a15) | **2021-10-31** | RAS-Werte über CAN von Node 1 gezogen. |
| **Parallele Phase** | beide | ab **2021-10-31** | Zwei unabhängige RAS-Messspuren. **Für S6-Features:** RAS-Raumtemperatur aus EINER Quelle wählen (Vorschlag: `dev2` ab 2021-10, davor `dev1`), um Double-Counting zu vermeiden. |

---

## 4. Setzbare Elemente (9 Adressen, L1-Whitelist)

Authoritative CAN-Adresse aus `CMI_WebTest/CMI-WEB/CMI-WEB.xml` (2022, 1600×1200); **Display-Adresse + Bezeichnung + aktueller Wert** abgeglichen gegen `discovery/live_1.cgi` + `assets/schematics/1.html` (Proximity-Regel = exakt die von `app/cmi/schematic_parser.py`).

| Display-Adresse | Bezeichnung | Komponente | CAN-Ref (2022) | Options | aktuell (live 2026-10-26) | riskant |
|---|---|---|---|---|---|---|
| `101301D0180` | Betriebsmodus (HEIZKR. Betriebsart) | `uvr1611` | idx12317 sub1 (node1) | STANDBY, ZEIT/AUTO, NORMAL, ABGESENKT, PARTY, URLAUB, FEIERTAG | NORMAL | – |
| `10040912091` (x2) | Solarpumpe Modus | `solar_system` | idx8337 sub0 (node32) | Hand/AUS, Hand/EIN, Auto | Auto | – |
| `10820A10180` | Pumpe Heizkreis | `heizkreis_rl` | idx8353 sub8 (Pumpe-Hzkr) | AUS, EIN, AUTO | AUTO/EIN | – |
| `10120A10180` | Pumpe Holzofen (Pumpe Holzo.) | `kessel_holz` | idx8353 sub1 (Pumpe Holzo.) | AUS, EIN, AUTO | AUTO/EIN | – |
| `10620A10180` | Ladepumpe WW (Ladep.WW.) | `warmwasser` | idx8353 sub2 (Ladep.WW.) | AUS, EIN, AUTO | AUTO/AUS | – |
| `10220A10180` | WW vom Öl (Ladep.WW.5) | `warmwasser` | idx8353 sub6 (Ladep.WW.5) | AUS, EIN, AUTO | AUTO/AUS | – |
| `10720A10180` | Zirkulation (Pumpe-Zirku5) | `warmwasser` | idx8353 sub7 (Pumpe-Zirku5) | AUS, EIN, AUTO | AUTO/AUS | – |
| `10920A10180` | Ventil Heizungsruecklauf (Vent. HzRL) | `heizkreis_rl` | idx8353 sub9 (Vent. HzRL) | AUS, EIN, AUTO | **HAND/EIN** | – |
| `10520A10180` | **Oelbrenner** (Anfoerdung Oelbrenner) | `kessel_oel` | idx8353 sub5 (Anf.Brenner) | AUS, EIN, AUTO | AUTO/AUS | **JA** |

**Risikohinweis (Oelbrenner):** `10520A10180` (Ölbrenner) ist der riskante Schalter (Ölverbrennung). S4-2 verlangt Bestätigungs-Dialog + S3-1 Whitelist-Read-back. ⚠️ **DISCREPANCY:** `live_1.cgi` (2026-10-26) zeigt Ölbrenner = **AUTO/AUS**; der einzige **HAND/EIN**-Schalter ist `10920A10180` = **Ventil HzRkl**. `memory.md`/`Plan.md` (2026-10-27) notierten dagegen „Ölbrenner aktuell HAND/EIN". → **Live-Read-back vor dem ersten Ölbrenner-Write** (kein CMI-Call in dieser Task). Siehe §7.

---

## 5. Live-Seiten (5 Schematics) & Setzbare pro Seite

| Seite | Bereich | Kernwerte | Setzbare Adressen |
|---|---|---|---|
| 1 | Start / Kamin | Betriebsmodus, alle Schalter, Speicher Oben/Mitte/Unten, Außen, Holzkessel-VL, Abgas | alle 9 |
| 2 | Warmwasser | Speicher Oben/Mitte/Unten, WW, Ladepumpe WW, Zirkulation, Ölladepumpe | 10620, 10220, 10720 |
| 3 | Raum / Heizkreis | 3 RAS-Raumtemperaturen (Keller/EG/OG), Oelkessel-VL, VL/RL, Außen, Soll-VL, Betriebsmodus, Pumpen-HzKl-Status | 101301, 10820 |
| 4 | Solar | Kollektor, Speicher Oben/Mitte/Unten, Solarpumpe, Solarpumpe-Modus | 10040912091 |
| 5 | Kamin | Speicher Oben/Mitte/Unten, Holzkessel-VL, Abgas AN/AUS, Pumpe Holzofen, Min 50C | 10120 |

**Suggested areas (Dashboard-Gruppierung)** → `plant-model.json:suggested_areas`.

---

## 6. Staleness-Prüfung: `.tdw` (2021) vs `.dat` (2022-09-18)

- **UVR1611:** BDB-Labels im `20211020_owner.tdw` (bdb1..bdb12) sind **identisch** mit den 2022-09-18 `.dat`-Labels (`Pumpe Holzo. / Vent. HzRL / Warmwasser / Abgas / Oel CNT / Holz CNT / NL Lad WW / Urlaub / P-Heizkreis / RaumTemp / L-WW Cmp / TRICK-Net`). → **Kanal-Belegung unverändert.**
- **UVR610:** 2021-10-29 `.dat` (FW T1.18, DATA_SIZE 0x6E0) hat **6** BDB-Labels; 2022-09-18 `.dat` (FW T1.19, DATA_SIZE 0x750) hat **9** BDB-Labels (+= `Keller/EG/OG HzRkl Ventil`). → **Drei neue BDB-Labels**, Belegung der vorhandenen Kanäle unverändert.
- **Schluss:** Das 2022-09-18-Image ist das aktuellste Programm; es **bestätigt** die 2021er-`.tdw`-Struktur. Die hier verwendete Kanal-Belegung (aus `.tdw` 2021) gilt daher auch für die 2022er-Ära. **Kein Widerspruch** zur WinSol-Setup.xml-Belegung.

---

## 7. Historische Belegung / Inbetriebnahmen (datengetreu aus Parquet)

> Konfidenz: **datengetreu** aus `data/winsol_pq/**.parquet` (`count(IS NOT NULL)` pro Device-Spalte) — **nicht geraten**.

| Meilenstein | Datum | Evidenz (Parquet) | Konfidenz |
|---|---|---|---|
| Logger-Start (dev0 **und** dev1, UVR1611 DS1+DS2) | **2020-08-30 12:36:09** | `dev0_analog_01` + `dev1_analog_01` + `dev1_digital_01` erste Nicht-NULL; 19.211.004 Werte durchgehend | hoch |
| UVR610 Inbetriebnahme (dev2 analog, Solar) | **2021-10-31 00:00:03** | `dev2_analog_01` erste Nicht-NULL; 15.559.299 Werte. Bestätigt HITL-Vermutung „UVR610 kam 2021". tdw 2021-10-29 + .dat 2021-10-29 passen dazu. | hoch |
| dev2 digital (Solarpumpe / D1) aktiv | **2022-03-24 14:50:25** | `dev2_digital_01` erste Nicht-NULL; 14.312.642 Werte. Stimmte mit LogX-Formatvariante P=137 (digital-Byte +136). UVR610-.dat 2022-03-24/25/09-18 = FW T1.19. | hoch |

**Jahresweise Fill (count analog_01):** 2020: dev1=1.066.776, dev2=0 · 2021: dev1=3.120.514, dev2=535.653 · 2022–2026: dev1≈dev2 (voll).

**Kernaussagen:**
1. **dev1 (RAS Keller/EG/OG) loggt seit dem Logger-Start 2020-08** — **NICHT erst seit der UVR610 (2021-10).** dev1 ist der UVR1611-eigene DS2-Block.
2. **dev2 (Solar/UVR610) startet 2021-10-31** — Inbetriebnahme der UVR610 Ende Oktober 2021.
3. **dev2-digital (Solarpumpe) erst ab 2022-03-24** — digitaler Logging-Kanal der UVR610.
4. **Ab 2021-10-31 liefern dev1 (UVR1611) und dev2 (UVR610) parallele RAS-Messungen** (dev2 zieht die RAS-Werte über CAN von Node 1). → S6: RAS-Quelle konsolidieren (§3.4).

---

## 8. Analyseinformationen (Vertrauenswürdigkeit, Ambiguitäten)

**Zuverlässig parsbar (hoch):**
- UVR1611/Eingaenge/Ausgaenge/BDB/CanDatLog/Netzwerk — vollständig und eindeutig (1:1-Belegung, durch owner.xml + Setup.xml + live cross-checked).
- UVR610 Eingaenge/Ausgaenge/DatLog/CAN — vollständig (PT1000-Speicher/Kollektor, O1 Solarpumpe, RAS-Übertragung von Node 1).
- Setzelemente (9 Adressen) — CAN-idx/sub aus CMI-WEB-2022, Bezeichnung+Wert aus live_1.cgi+Layout.

**Ambiguität / offene Punkte (mittel/niedrig):**
- **dev1-RAS-Physik:** Die RAS-Raumtemperaturen in dev1 (UVR1611 DS2) werden nicht über die 10 lokalen Analogeingänge der UVR1611 geführt (die sind Speicher/WW/Kessel/Außen/Abgas belegt), sondern stammen aus RAS-Modulen auf dem CAN-Bus (BDB bdb10 „RaumTemp"). Exakte Sensor-Physischplatzierung: aus `.tdw` nicht 1:1 ableitbar (nur logisch belegt). Konfidenz: mittel.
- **`<av>`-Bitfelder → DS-Logging-Layout:** Die `<av name=...>`-Definitionen (z. B. `Gr`/`Kl`, `Erg`, `Inv`, `Sch`) beschreiben das **interne Statistik-/Rückführungs-Wort-Layout**, nicht direkt die Dev-Kanäle. Die Dev-Kanal-Belegung wird stattdessen über `CanDatLog`/`DatLog` (`qu`, `eanr`, `fid`) + BDB-Labels + Setup.xml abgeleitet — das ist der belastbare Weg. Die `<av>`-Namen sind für die Kanalzuordnung nur indirekt/informativ.
- **Ölbrenner-Stand:** live (AUTO/AUS) vs. memory.md/Plan.md (HAND/EIN) → DISCREPANCY, Live-Read-back nötig (§4).
- **dev2 d7..d10:** Setup-Labels vorhanden, aber im `DatLog` nicht aktiv (`eanr=0`) → JSON-API liefert nur d1.
- **„x2" bei UVR610:** Quelle „UVR610 Node 32 (x2, ...)": ein Gerät mit Doppelrolle, nicht zwei physische Geräte (Setup.xml: 1 Dev @ Node 32).

**Korrekturen zum bestehenden Label-Modell (menschliches Review empfohlen):**
1. **dev1 ist UVR1611 (Node 1, DS2), nicht UVR610** → `app/cmi/labels.py` (Kommentar „WinSol dev-1 a2/a8 (Keller/DG)" bleibt korrekt, aber die Gerätezuordnung UVR610 ist falsch) + `memory.md` (Kanal-Belegungs-Anmerkung).
2. **Ölbrenner = `10520A10180`** (AUTO/AUS), **Ventil HzRL = `10920A10180`** (HAND/EIN). `memory.md`/`Plan.md` „Ölbrenner aktuell HAND/EIN" → korrigieren + Live-Read-back.
3. **Historische Belegung** (dev1 seit 2020-08, dev2 seit 2021-10-31, dev2-digital seit 2022-03-24) → in `memory.md` als Datenstempel ergänzen.
4. **Doppelte RAS-Belegung** (dev1 + dev2 ab 2021-10) → für S6-Feature-Engineering konsolidieren.

*(Ende des Plant-Structure-Modells.)*
