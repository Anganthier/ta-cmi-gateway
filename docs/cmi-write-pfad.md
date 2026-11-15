# CMI Write-Pfad — Discovery, Inventar & No-Op-Nachweis (2026-11-02)

**Status:** ✅ Discovery abgeschlossen (Inventar + EIN No-Op-Write + Read-back + 6-Seiten-Befund).
**Scope:** Read-only-Inventar + minimaler write-Kapazitäts-Nachweis. `app/main.py` (laufende Gateway-App) bleibt READ-ONLY; der No-Op lief per CLI-Script (`scripts/cmi_write_discovery.py`), nicht über die App.

---

## 1. Write-Mechanik (verifiziert)

Protokoll-Grundlage: CMI-eigenes Frontend `discovery/scheme.js` + live Verifikation am 2026-11-02.

1. **Auflösen (read-only):** `GET /INCLUDE/changer.cgi?sadr=<displayAdr>` (x2: `GET /INCLUDE/changerx2.cgi?sadrx2=<displayAdr>`) → HTML-Fragment mit
   * Hidden-Input `id="changeadr"` (bzw. `name="changeadrx2"` + `id="changeadr"` bei x2) = **change-Adresse**,
   * `<select id="changeto">` mit den erlaubten Optionen; die **`selected`-Option = aktueller Wert** (kanonisch, direkt aus dem CMI).
2. **Write (GET, nicht POST):**
   * x1: `GET /INCLUDE/change.cgi?changeadr=<changeAdr>&changeto=<value>`
   * x2: `GET /INCLUDE/change.cgi?changeadrx2=<changeAdr>&changetox2=<value>`
   * `changeto` ist das **value-Attribut** des Options (`"1"`/`"2"`/`"3"` …), nicht das Label.
   * Erfolg: HTTP 200, Body `OK` (live gemessen: `OK\r`).
3. **Read-back:** Schematic-Seite erneut auflesen (`/schematic_files/N.cgi`, unlimitiert) +/oder Changer-Dialog erneut holen (`selected`-Option). **Kein JSON-API-Call nötig** (1/min-Limit bleibt unangetastet).

**Isolierung:** `app/cmi/write.py` (`CMIWriteClient`, Subklasse von `CMIClient`) mit eigener Endpoint-Whitelist: `change.cgi` + die beiden Read-back-Change-Endpunkte + Schematic-Seiten (Read-only-Regel der Basisklasse). Der Read-only-Gateway-Client (`CMIClient`, `ALLOWED_READ_PATHS`) ist unverändert und blockt `change.cgi` weiterhin (Regressionstest `test_read_only_client_still_blocks_change_cgi`). Alle CMI-Requests (Read + Write) laufen über eine Serial-Queue (Single-Flight) und kollidieren damit nie mit dem laufenden Gateway-Poller.

## 2. Inventar: alle setzbaren Adressen (live, 2026-11-02 08:51 MESZ)

Quelle: `scripts/cmi_write_discovery/INVENTAR.md` / `.json` (live: Schematic 1–5 + Changer-Resolution).

| Adresse | Bezeichnung | Seiten | changeadr | option_type | Optionen / Min-Max | Aktueller Wert | risk | anwendbar |
|---|---|---|---|---|---|---|---|---|
| `10040912091` | Solarpumpe Modus (x2) | 1, 4 | `20000040911202` | select | 2=Auto, 0=Hand/AUS, 1=Hand/EIN | 2 (Auto) | nein | ja |
| `10120A10180` | Pumpe Holzofen | 1, 5 | `01910120A1` | select | 1=AUS, 2=EIN, 3=AUTO | 3 (AUTO) | nein | ja |
| `101301D0180` | Betriebsmodus (HEIZKR. Betriebsart) | 1, 3 | `019001301D` | select | 1=STANDBY, 2=ZEIT/AUTO, 3=NORMAL, 4=ABGESENKT, 5=PARTY, 6=URLAUB, 7=FEIERTAG | 3 (NORMAL) | nein | ja |
| `10220A10180` | WW vom Öl (Ladep.WW.5) | 1, 2 | `01910220A1` | select | 1=AUS, 2=EIN, 3=AUTO | 3 (AUTO) | nein | ja |
| `10520A10180` | Ölbrenner (Anforderung) | 1 | `01910520A1` | select | 1=AUS, 2=EIN, 3=AUTO | 3 (AUTO) | **ja** | **nein** |
| `10620A10180` | Ladepumpe WW | 1, 2 | `01910620A1` | select | 1=AUS, 2=EIN, 3=AUTO | 3 (AUTO) | nein | ja |
| `10720A10180` | Zirkulation | 1, 2 | `01910720A1` | select | 1=AUS, 2=EIN, 3=AUTO | 3 (AUTO) | nein | ja |
| `10820A10180` | Pumpe Heizkreis | 1, 3 | `01910820A1` | select | 1=AUS, 2=EIN, 3=AUTO | 3 (AUTO) | nein | ja |
| `10920A10180` | Ventil Heizungsrücklauf | 1 | `01910920A1` | select | 1=AUS, 2=EIN, 3=AUTO | 2 (EIN) | nein | ja |

**Befunde:**

* **Alle 9 Adressen sind live auflösbar** (changeadr + Optionen + aktueller Wert eindeutig lesbar). Damit sind die 6 zuvor nur per dokumentiertem Muster abgeleiteten changeadr-Werte (`01910{pos}20A1`) **live bestätigt** — keine Adressen „nicht setzbar“ im Inventar-Sinn.
* `docs/plant-model.json` (unverändert) stimmt exakt überein: gleiche 9 Adressen, gleiche Optionen, gleiche aktuellen Werte (inkl. Korrektur vom 2026-10-27: Ölbrenner = `10520A10180` = **AUTO/AUS**, einziger HAND/EIN = `10920A10180`).
* **Nicht über die Schematic-Seiten setzbar (also keine `loadChanger`/`changex2`-Bindung):** alle numerischen Parameter (z. B. Vorlaufsoll `idx12360 sub1` = read-only im CMI-Layout), RAS-Offsets, Heizkurven-Parameter. Diese fallen unter Q-107/Q-108 (v2/TA-Designer) und sind mit dem `change.cgi`-Pfadt **nicht** erreichbar.
* `changeto`-Sanität ist implementiert: nur Werte aus der eigenen Optionsliste (select) bzw. `[min,max]` (numeric) bzw. `{0,1}` (bit); sonst `CMISafetyError` **vor** dem Write.

## 3. No-Op-Write (live, exakt EIN) — Ergebnis

Ziel: `101301D0180` (Betriebsmodus, rein logischer Schalter, keine Aktoratorik), aktueller Wert `3` = NORMAL (laut plant-model.json und Read-back bestätigt).

| Schritt | Wert / Befund |
|---|---|
| 1. Read-back VORHER | `value=3`, `label=NORMAL`, Schematic-Text `NORMAL`, consistent=true |
| 2. No-Op-Write | `GET /INCLUDE/change.cgi?changeadr=019001301D&changeto=3` → **HTTP 200, Body `OK\r`** (3,6 s Gesamtdauer inkl. beiden Read-backs) |
| 3. Read-back NACHHER | `value=3`, `label=NORMAL`, Schematic-Text `NORMAL`, consistent=true |
| Vergleich | **vorher == nachher** (`value_changed=false`, `label_changed=false`, `noop_ok=true`) |
| Post-Mortem | Betriebsmodus-Pos auf Schematic-Seite 1 weiterhin `NORMAL`; Gateway `/api/v1/health` ok (schematic+json_api, stale=false) |

**Pfad-Beweis:** Auth (Basic), Changer-Resolution (changeadr), `change.cgi`-Call (GET mit `changeadr`+`changeto`), Rückmeldung des CMI (`OK`), Read-back (Schematic + Changer-Dialog) — alle Stufen funktionieren live. Das CMI akzeptiert das Zurückschreiben des aktuellen Werts ohne Fehlermeldung („Wert unverändert“ wurde nicht abgelehnt, der No-Op ging durch). Vollständiges Protokoll: `scripts/cmi_write_discovery/NOOP.json`.

**Keine Betriebsänderung:** nur `101301D0180` wurde angesprochen, und zwar ausschließlich mit dem exakt aktuellen Wert. Keine Schalter umgeschaltet, kein Ölbrenner, kein Moduswechsel.

## 4. 6-Seiten-Befund (read-only)

`GET /schematic_files/6.cgi` / `7.cgi` / `8.cgi` (live 2026-11-02 08:53 MESZ): **alle HTTP 404** („Error 404 - Not Found“ der CMI-Server-Error-Seite; bereits 2026-11-02 im S4N-UI-KI-Block vorverifiziert und jetzt mit Statuscode protokolliert: `scripts/cmi_write_discovery/PROBE6.json`).

* Eine 6. Schematic-Seite existiert also **nicht** und wurde **nicht** angelegt (nur beobachtet).
* Es wurde kein CMI-Konfig-Endpunkt aufgerufen, der eine neue Seite anlegen könnte.
* Konsequenz: Die logische „Seite 6“ (KI-Einstellungen) bleibt ein **Gateway-UI-Panel** (S4N-UI-KI), kein CMI-Seiten-Upload.

## 5. Safety — was wurde NICHT angefasst

* **RISK-Adresse `10520A10180` (Ölbrenner):** nur inventarisiert (read-only Changer-Resolution + Schematic), **nie** geschrieben — die Schutzlogik ist hart (`CMISafetyError` bei jedem Write-Versuch, inkl. No-Op; Regressionstest `test_risk_address_never_written_even_noop`).
* Keine der anderen 8 Adressen wurde geschrieben; **gesamt genau EIN `change.cgi`-Call** (der No-Op auf `101301D0180` mit `changeto=3` = aktueller Wert).
* Kein CMI-/LOG/clear, kein schematic-clear, kein Poller-Stop.
* Keine UI-/App-Route geändert: `app/main.py`, `app/api/*`, `ui/*` unverändert (laufende App bleibt read-only; No-Op lief per CLI).
* `analysis/model/*`, `data/features*`, `models/`, `docs/plant-model.md/.json`, `docs/cmi-log-protocol.md`, `app/cmi/cmi_log*.py` (Q-112b-Lauf): unverändert.
* Kein git-Commit.

## 6. Implementierung

* **`app/cmi/write.py`** (neu): `CMIWriteClient` (Subklasse `CMIClient`, eigene Whitelist `change.cgi` + Read-back-Endpunkte, `_assert_allowed`-Override strikter als die Read-only-Basis), `resolve()`, `read_current()` (Changer-`selected` + Schematic-Text, Konsistenzflag), `noop_write()` (vorher → write mit exakt aktuellem Wert → nachher, Strict-Mode: Drift ⇒ `CMISafetyError`), `write()` (S3-1-Fundament: Wert-Validierung gegen Optionsliste/Min-Max, Read-back-Verifikation), `CMISafetyError`-Basisregeln (Whitelist, RISK, nicht-lesbar, changeto-Sanität), Retries: 1× bei Transport-Fehlern (Slot-Kollision mit Poller), keine Retries bei HTTP-Fehlern.
* **`app/cmi/exceptions.py`**: `CMISafetyError` (neue Exception).
* **`scripts/cmi_write_discovery.py`** (neue CLI): `inventory` / `noop` / `probe6`; Artefakte `scripts/cmi_write_discovery/{INVENTAR.md,INVENTAR.json,NOOP.json,PROBE6.json}`.
* **`tests/test_write.py`** (neu, 42 Tests, 100 % gemockt via `httpx.MockTransport` + `discovery/`-Fixtures): No-Op-Logik (vorher==nachher, Pfad-Beweis, x2-Parameter), Strict-Drift-/Unlesbar-Abbruch, RISK-/Whitelist-/Option-Sanierung-Abwehr, Endpunkt-Whitelists (Write-Client strikt, Read-only-Client blockiert `change.cgi` weiterhin), Inventar-Parser (9 Adressen über die 5 Live-Seiten, Seiten-Mapping), Retry-Semantik (1× Retry bei Transport-Fehlern, kein Retry bei HTTP-Fehlern, Aufgeben nach 2. Fehler).

## 7. Offene Punkte

* **Q-107 (v2, RAS-Offset per CMI-Write):** RAS-Offsets sind **nicht** unter den 9 Schematic-Adressen (kein `loadChanger`-Bind) → über `change.cgi` nicht setzbar; bleibt v2/TA-Designer-Track (ggf. eigener CMI-Parameter-Endpunkt, zu untersuchen).
* **Q-108 (Heizkurven-Parameter, CAN-Adressen):** Stand 2026-11-02: nicht im Schematic-Write-Pfad (Seiten 1–5). **Update 2026-11-07:** maintainer hat CMI-Web-**Seite 6 „C.M.I. 6“** hinzugefügt (26 `loadChanger`-Elemente = alle setzbaren Heizkreisregler-Parameter der UVR1611, inkl. Kurvenpunkte 12303/12304, Raum-Soll 12300/12301, VL-Max 12305). → Q-108 ist **nach Upload von Seite 6 + config.tas prinzipiell gelöst** (Pfad: `change.cgi`), Live-Verifikation (`changer.cgi`-Resolution + `state`-Snapshot) ausstehend. Details: `docs/uvr-heizkreisregler-parameter-cmi.md` + `scripts/cmi_write_discovery/INVENTAR.md` (Kap. „Seite 6“; 36 Einträge, 16 RISK).
* **S3-1 (`/api/v1/change`-Route):** Fundament steht (`CMIWriteClient.write()` + Sanität + Read-back + `CMISafetyError`); offene Punkte vor Integration: Gateway-Audit-Log (JSONL), `GATEWAY_WRITE_TOKEN`-Auth, Bestätigungs-Dialog-Vertrag, HITL-Freigabe pro Change.
* **S4N-UI-KI `executability`-Daten:** jetzt verfügbar — alle 9 Whitelist-Schalter = „setzbar per CMI“ (Inventar §2); RAS-Offsets/Heizkurven = „TA-Designer/v2“.
* **Ölbrenner-Live-Stand (F9):** laut Inventar `AUTO` (Schematic `AUTO/AUS`) — konsistent mit plant-model; kein geänderter Zustand.
