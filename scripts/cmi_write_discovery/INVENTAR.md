# CMI Write-Pfad-Inventar (read-only Discovery, 2026-11-02)

Erzeugt: 2026-11-02T08:51:46+01:00 (live CMI `http://192.168.0.100`, read-only: `/schematic_files/1..5.cgi` + `/INCLUDE/changer[x2].cgi`).

| Adresse | Bezeichnung | Seiten | changeadr | option_type | Optionen/Min-Max | Aktueller Wert | risk | anwendbar |
|---|---|---|---|---|---|---|---|---|
| `10040912091` | Solarpumpe Modus (x2) | 1, 4 | `20000040911202` | select | 2=Auto, 0=Hand/AUS, 1=Hand/EIN | 2 (Auto) | nein | ja |
| `10120A10180` | Pumpe Holzofen | 1, 5 | `01910120A1` | select | 1=AUS, 2=EIN, 3=AUTO | 3 (AUTO) | nein | ja |
| `101301D0180` | Betriebsmodus (HEIZKR. Betriebsart) | 1, 3 | `019001301D` | select | 1=STANDBY, 2=ZEIT/AUTO, 3=NORMAL, 4=ABGESENKT, 5=PARTY, 6=URLAUB, 7=FEIERTAG | 3 (NORMAL) | nein | ja |
| `10220A10180` | WW vom Oel (Ladep.WW.5) | 1, 2 | `01910220A1` | select | 1=AUS, 2=EIN, 3=AUTO | 3 (AUTO) | nein | ja |
| `10520A10180` | Oelbrenner (Anfoerdung) | 1 | `01910520A1` | select | 1=AUS, 2=EIN, 3=AUTO | 3 (AUTO) | ja | nein |
| `10620A10180` | Ladepumpe WW | 1, 2 | `01910620A1` | select | 1=AUS, 2=EIN, 3=AUTO | 3 (AUTO) | nein | ja |
| `10720A10180` | Zirkulation | 1, 2 | `01910720A1` | select | 1=AUS, 2=EIN, 3=AUTO | 3 (AUTO) | nein | ja |
| `10820A10180` | Pumpe Heizkreis | 1, 3 | `01910820A1` | select | 1=AUS, 2=EIN, 3=AUTO | 3 (AUTO) | nein | ja |
| `10920A10180` | Ventil Heizungsruecklauf | 1 | `01910920A1` | select | 1=AUS, 2=EIN, 3=AUTO | 2 (EIN) | nein | ja |

## Nicht schreibbar (Details)

- `10520A10180` (Oelbrenner (Anfoerdung)): RISK: Oelbrenner -- nur inventarisieren, NIE schreiben


## Seite 6 „C.M.I. 6“ — neue Heizkreisregler-Parameter (statische Analyse 2026-11-07)

Quelle: `CMI-WEB.xml` (Seite_5/UID=6) + `CMI/6.cgi` (26 `loadChanger`-Elemente) + `CMI/config.tas` (36 unique Adressen), Nextcloud `house-heating/CMI-Web/CMI-WEB/`. Alle 26 Seiten-6-Adressen sind in `config.tas` enthalten; das INVENTAR deckt 100 % der 36 unique Adressen ab.

**Status: PENDING-LIVE** — Seite 6 wurde am 2026-11-07 noch NICHT auf den CMI geladen (Upload läuft / folgt). Die `changeadr`-Werte und die numerischen min/max-Grenzen sind daher noch per `changer.cgi?sadr=…` (read-only) zu verifizieren. `tdw_value` = Wert aus `20261107_owner.tdw` (Heizkreisregler, unverändert gegenüber 20211020).

| Adresse | CAN-Idx | Bezeichnung | tdw-Wert | risk |
|---|---|---|---|---|
| `10130290180` | 12329 | HEIZKR. Freigabe Heizkreis (Fg) | aktiv | **JA** (Ausschalten stoppt Heizkreis) |
| `101302E0180` | 12334 | HEIZKR. Freigabe Pumpe (FgP) | aktiv | **JA** (Ausschalten stoppt Heizkreispumpe) |
| `10130330180` | 12339 | HEIZKR. Freigabe Mischer (FgM) | aktiv | **JA** (Ausschalten stoppt Mischer) |
| `101301D0180` | 12317 | HEIZKR. Betriebsart Modus | 3 (NORMAL) | nein (live 2026-11-02, auch auf Seite 6) |
| `101300D0180` | 12301 | HEIZKR. T.raum ABSENK (Raumsoll) | 16 | nein |
| `101300C0180` | 12300 | HEIZKR. T.raum NORMAL (Raumsoll) | 20 | nein |
| `10130470180` | 12359 | HEIZKR. Vorhaltezeit (bez. −10 °C) | 30 | nein |
| `101300A0180` | 12298 | HEIZKR. Raumeinfluss (%) | 70 | nein |
| `101300B0180` | 12299 | HEIZKR. Einschaltüberhöhung | 0 | nein |
| `101300F0180` | 12303 | HEIZKR. VL bei +10 °C (Kurvenpunkt 1) | 45 | nein |
| `10130100180` | 12304 | HEIZKR. VL bei −20 °C / Steilheit (Kurvenpunkt 2) | 65 | nein |
| `10130110180` | 12305 | HEIZKR. Vorlauftemp. MAX | 70 | nein |
| `10130120180` | 12306 | HEIZKR. Vorlauftemp. MIN | 25 | **JA** (Heraufsetzen = Mindest-Heizleistung) |
| `10130460180` | 12358 | HEIZKR. Aussen-Schwelle Frostschutz | 5 | **JA** (Frosteinbruch-Risiko) |
| `10130450180` | 12357 | HEIZKR. Raumsoll Frostschutz | 5 | **JA** (Frosteinbruch-Risiko) |
| `10130050180` | 12293 | HEIZKR. Abschaltung Raumtemp (HyRTAb) | 1 | **JA** (Abschalt-Logik) |
| `10130060180` | 12294 | HEIZKR. Abschaltung VLSOLL<MIN (HyVLAb) | 20 | **JA** (Abschalt-Logik) |
| `10130070180` | 12295 | HEIZKR. Abschaltung Aussen (SchwATAb) | 19 | **JA** (Abschalt-Logik) |
| `10130160180` | 12310 | HEIZKR. Schwellwert AT Abschaltung (HyATAb) | 10 | **JA** (Abschalt-Logik) |
| `10130080180` | 12296 | HEIZKR. Abschaltung AT Absenk (SchwAbAb) | 15 | **JA** (Abschalt-Logik) |
| `10130180180` | 12312 | HEIZKR. Schwellwert AT Absenk (HyAbAb) | 10 | **JA** (Abschalt-Logik) |
| `101301C0180` | 12316 | HEIZKR. Abschaltung VL-IST>MAX (VLMaxAb) | 1 | **JA** (Abschalt-Logik) |
| `10120210180` | 8225 | Zeitprogramm Wochentagszuordnung | wt | nein |
| `10120310180` | 8241 | Zeitfenster 1 EIN (Min) | 36 (06:00) | nein |
| `10120410180` | 8257 | Zeitfenster 1 AUS (Min) | 135 (22:30) | nein |
| `10120510180` | 8273 | Zeitfenster 1 Solltemperatur | ? | nein |

### Unbekannte Adressen (nur in `config.tas`, NICHT auf Seite 6)

| Adresse | CAN-Idx | Status |
|---|---|---|
| `101301F0180` | 12319 | Bedeutung offen (in `owner.xml`/Seite 6 nicht vorhanden; Vermutung „Raum-Soll-Register“ **widerlegt** — T.raum Absenk/Norm = 12301/12300). **RISK=true, nie automatisieren** bis Live-Check die Bedeutung klärt. |
| `10130200180` | 12320 | wie 12319. |

### Risiko-Ranking für L2-Automatisierung (Seite 6)

- **Geringes Risiko (freie Spielräume, L2-Kandidaten):** `12300/01` (Raum-Soll ±0,5 K), `12303/04` (Kurve ±2 K), `12305` (VL-Max, nur senken), `12298` (Raumeinfluss).
- **RISK (hart blockiert, `writeable=false`):** alle 3 Freigaben (12329/12334/12339), VL-Min (12306), beide Frostschutz-Schwellen (12357/12358), alle 6 Abschalt-Schwellen (12293–12296, 12310, 12312, 12316), Ölbrenner (10520A10180, alt), 12319/12320 (unbekannt).

**Adresse-Format (verifiziert an `101301D0180`→12317 live + 26 Seiten-6-Adressen):** `1` + `AA` (Knoten: 1=UVR1611 dev 128, 0=UVR610/x2, 2=UVR16x2) + `BBBB` (CAN-Index, hex) + `CC` (Subindex) + `80`.
