# L2-UI-Panel + S3-1-Write-Route (S4N-UI-KI)

Die **KI-Einstellungen** (logische „Seite 6“) der Gateway-Web-UI: alle
L2-v0.1-Empfehlungen aus `GET /api/v1/recommend` als Zone-Karten (Tier A:
RAS-Offset je Zone) + Tier-B-Advisorys (Heizkurve/WW/Zirkulation) mit
Stale-/Datenstand-Kopf und Auto-Refresh. „Anwenden“ führt in den **S3-1
Two-Phase-Write** (`GET`/`POST /api/v1/change`) — Whitelist + Confirmation-Hash
+ Read-back + Audit. Alle v0.1-Empfehlungen sind **Advisory**
(`executability = pending-cmi-write-discovery`), d. h. per CMI (noch) **nicht**
setzbar: RAS-Offsets/Heizkurven = TA-Designer/v2 (Q-107/108). „Anwenden“ ist
deshalb nur für CMI-setzbare Whitelist-Schalter aktiv (Future-Proofing).

Design-Kanon: `docs/ai-model-design.md` §3.2 (L2-Output + Guardrails) +
`docs/cmi-write-pfad.md` (Write-Mechanik + Safety).

> **Scope:** CMI-seitige `6.cgi` ist **nicht** dieser Track (CMI-Konfig;
> `6–8.cgi` = 404, live verifiziert 2026-11-02). Das Panel ist eine
> additive Gateway-UI-Seite; die CMI-Seiten 1–5 + ihre Assets bleiben
> unverändert.

---

## 1. Endpunkte

### `GET /api/v1/recommend` (Read-Token)

Liefert **exakt die L2-v0.1-JSON-Struktur** + `generated_at`:

```json
{
  "ts": "2026-11-02 07:00",
  "model_version": "cmi-brain-v003+l2-v001",
  "horizon_h": 12,
  "stale": false,
  "stale_reasons": [],
  "empfehlungen": [
    {"parameter": "RAS-Offset Keller", "zone": "kell", "kanal": "dev1 a1",
     "aktuell": "+3.0 K", "vorgeschlagen": "+1.5 K", "wirkung_prognose": "...",
     "begruendung_de": "...", "konfidenz": 0.82,
     "executability": "pending-cmi-write-discovery",
     "cmi_hint": "Q-107 (RAS-Offset-Write)", "tier": "A", "changed": true},
    {"parameter": "WW-Ladefenster", "zone": null, "aktuell": "...",
     "vorgeschlagen": "...", "konfidenz": 0.5, "tier": "B", "...": "..."}
  ],
  "kosten": {"comfort": 0.1, "energy": 0.3},
  "guardrail_hits": [],
  "konfidenz": 0.8,
  "szenarien": [],
  "kontext": {"datenstand": "...", "wx_quelle": "...", "wx_run": "...", "stale": false},
  "generated_at": "2026-11-02T07:12:04+00:00"
}
```

* **Fresh** → HTTP 200 (das `stale`-Flag ist *Daten*, kein Fehler).
* **Stale** (Metrik-Lücke > 1 h ODER Wetter-Run > 2 h alt) → HTTP 200 mit
  `stale=true` (Design-Regel R5: Stale-Fallback, „beibehalten“, nie 5xx).
* **Engine-Fehler** (Store fehlt/defekt, Artefakt kaputt, Crash) → HTTP 503 +
  JSON-Error `{"error": ..., "stale": false}` (kein nackter Crash-500).
* **`?force=true`** → Zwangs-Neulauf (UI „Jetzt aktualisieren“; 15-s-Burst-Sperre).

**Caching / Single-Flight** (`app/recommendation.py` `RecommendationService`):
Der Engine-Run wird **pro voller Berliner Stunde** gelaufen und in-memory
gecacht (Cache-Key = `berlin_hour_key()` = `YYYY-MM-DD HH:00`, TTL =
`RECOMMEND_CACHE_MIN`, Default 5 min). Treffer im TTL = kein Neulauf; ein
Fensterwechsel/`force` = ein frischer Run. Konkurrente Aufrufe derselben Stunde
teilen sich einen in-flight-Run (kein Doppel-Run). Ein fehlgeschlagener Run
vergifte den Cache **nicht** (nächster Aufruf retryt). Jeder frische Run lädt
das LIVE-Wetter (Open-Meteo ICON-D2) + L1-v003-Rollouts.

### `GET /api/v1/change?adr=<adr>&to=<wert>` (Read-Token) — **Preview**

Zweiphasen-Phase 1: changeadr + Optionen + aktueller Wert auflösen (nur
Read-only-CMI-Calls), `to` gegen die Live-Optionsliste/Min-Max validieren,
RISK/Whitelist prüfen (403), und einen signierten **Confirmation-Hash**
ausgeben. **Kein CMI-Write.** Response:

```json
{
  "adr": "10820A10180", "label": "AUTO", "current": {"value": "3", "label": "AUTO"},
  "target": {"value": "2", "label": "EIN"}, "changeadr": "01910820A1",
  "option_values": ["1","2","3"], "option_labels": ["AUS","EIN","AUTO"],
  "option_type": "select", "risk": false,
  "confirmation_hash": "<64 hex>", "expires_in_sec": 900, "issued_at": "2026-11-02 07:12:04+01:00"
}
```

### `POST /api/v1/change` (Write-Token) — **Execution**

Body = exakt 3 Felder: `{"adr", "to", "confirmation_hash"}` (der Browser hält
den Write-Token NICHT im Hash; der Hash ist HMAC-gekeyed). Auth = Bearer
Write-Token (leer = Route deaktiviert → 503; missing/wrong/Read-Token → 403).
Hash wird gegen das **aktuelle 15-Min-Fenster** geprüft (Fensterwechsel → 403
„abgelaufen“; idempotent im Fenster). RISK wird erneut geprüft, Vorher-Wert
für Audit-`from` gelesen, dann `CMIWriteClient.write()` mit Read-back.

Response (200):

```json
{"applied": true, "adr": "10820A10180", "from": "3", "to": "2",
 "readback": {"value": "2", "label": "EIN"}, "audit_id": "2026-11-02T06:12:04+00:00:10820A10180:2",
 "duration_sec": 1.2}
```

**Jede** Change-Attempt (Success **UND** Fehler) wird in die Audit-JSONL
( `settings.audit_jsonl_path`, Default `data/audit/change.jsonl` ) angehängt:

```json
{"ts": "...", "adr": "10820A10180", "label": "EIN", "from": "3", "to": "2",
 "user": "write-token", "readback": "2", "applied": true, "http_status": 200}
```

* **Read-back-Mismatch** → `applied=false` + HTTP **502** (Audit wird trotzdem
  geschrieben; `strict=False` im Write-Client, damit Mismatch sichtbar +
  auditiert bleibt und kein harter 403-`CMISafetyError` wird).
* **CMI down / Auth** beim Write → 503 + Audit-Zeile (`applied=null`).
* **RISK / nicht-Whitelist** → immer 403 (beide Phasen, vor jedem CMI-Call).

---

## 2. Docker / Mounts (rw-Nesting)

Die L2-Engine läuft **im Gateway-Image** (`COPY analysis`, `lightgbm`-Dep).
Feature-Store + Modell-Artefakt werden **read-only** bind-gehangen:

| Host-Pfad | Container-Pfad | Modus | App-Pfad (Default) |
|---|---|---|---|
| `./data` | `/data` | rw | (History-DB, Caches, Audit) |
| `./data/features` | `/data/features` | **ro** | `L1_STORE_DIR` → `/data/features` |
| `./models` | `/srv/cmi/models` | **ro** | `L1_MODEL_DIR` → `/srv/cmi/models/cmi-brain/v003` |

**rw-Nesting:** Der tiefer ro-Mount `./data/features:/data/features` liegt
*innerhalb* des rw `./data:/data`-Binds — der **tiefere** Mount gewinnt, d. h.
`/data` bleibt schreibbar (history.db, cmi_log_pq, audit), aber
`/data/features` ist strikt read-only (exakt wie das bestehende
`./data/winsol_pq:/data/winsol_pq:ro`). `/srv/cmi/models` ist ein ro-Mount in
einem **prae-creierten** Verzeichnis (Image: `mkdir -p /srv/cmi/models &&
chown app:app`), damit Docker es nicht root:root anlegt.

`settings.audit_jsonl_path` Default = `REPO_ROOT/data/audit/change.jsonl`;
im Container liegt `data/` auf dem rw-Bind → Audit landet unter
`/data/audit/change.jsonl` (hostseitig `./data/audit/`).

### Rebuild + Mount-Check (Deploy-Befehle)

```bash
# Image neu bauen (COPY analysis + lightgbm + ro-Mount-Points) + up
docker compose up -d --build cmi-gateway

# Mount-Check (host: data/features + models/cmi-brain/v003 müssen existieren):
docker compose exec cmi-gateway test -r /data/features            # -> 0
docker compose exec cmi-gateway test -r /srv/cmi/models/cmi-brain/v003  # -> 0
docker compose exec cmi-gateway test -w /data/audit               # rw-Bind (Audit)

# L2-Run + Route im Container live testen:
docker compose exec -e GATEWAY_READ_TOKEN=... cmi-gateway \
  curl -s -H "Authorization: Bearer $GATEWAY_READ_TOKEN" \
  http://127.0.0.1:30001/api/v1/recommend | python -m json.tool | head
docker compose exec cmi-gateway test -w /data/audit               # Audit-Pfad rw
tail -n 3 "$(docker volume inspect ...)"  # oder hostseitig: tail -n3 data/audit/change.jsonl
```

> **Host-Voraussetzung:** `data/features` (m1.3) + `models/cmi-brain/v003`
> müssen vor dem Deploy existieren (Orchestrator stellt das sicher).
> Fehlt der ro-gehangte Store/Artefakt-Content → `GET /api/v1/recommend`
> antwortet sauber mit **503 + JSON** (kein Crash).

---

## 3. UI-Panel „KI“ (Tab `data-page="ki"`)

* **Kopf:** `model_version` + Datenstand + Wetter-Run + `generated_at`
  (`fmtAge`); **Stale-Badge** (rot) bei `stale=true`; Button „Jetzt
  aktualisieren“ (`?force=true`, 15-s-Burst-Sperre).
* **Zone-Karten (Tier A):** je Zone (Keller/EG/OG): Offset
  **Ist → vorgeschlagen** (grüner Pfeil bei `changed`), Begründung DE,
  Prognose, Konfidenz-Chip, **executability**-Badge („setzbar per CMI“ vs.
  „Advisory (TA-Designer/v2)“) + `cmi_hint`.
* **Tier-B-Advisorys:** Heizkurve/VL-Soll, WW-Ladefenster, Zirkulation —
  Begründung + Konfidenz + Hinweis.
* **Fehler-/Stale-State sichtbar:** 503/Engine-Fehler → roter Fehler-Kasten mit
  „erneut versuchen“; `stale=true` → gelber Stale-Badge + `stale_reasons`.
* **Auto-Refresh:** alle **5 min**, **nur wenn der KI-Tab sichtbar ist**
  (`document.visibilityState` + Tab-aktiv) → sonst kein `/api/v1/recommend`.
  Der Server liefert ohnehin max. 1 Run/Stunde + TTL (respektiert).
* **„Anwenden“ (nur bei CMI-setzbarer Empfehlung):** Two-Phase —
  1) `GET /api/v1/change` (Read-Token, Confirmation-Hash), 2) **Dialog**
  (vor/nachher + **optionales Write-Token** `GATEWAY_WRITE_TOKEN`, separate
  localStorage-Key `cmi-gateway.write-token`), 3) `POST /api/v1/change`
  (Bearer Write-Token, 3 Felder), 4) Status `applied` + Read-back
  (502 → Fehleranzeige „Wert NICHT bestätigt“). 10-s-Doppelklick-Sperre.
  Nach Erfolg sofort `?force=true` (frischer Ist-Zustand).

CMI-Seiten 1–5 + `ui/svg`, `ui/js/{state-map,loops,loop-*}.js` bleiben
**unverändert**. Erlaubte UI-Änderungen: `ui/index.html` (Tab + `#page-ki` +
Apply-Dialog + `ki.js`-Load), `ui/css/app.css` (`.ki-*`), neu `ui/js/ki.js`.

---

## 4. Safety (S3-1)

* **NUR** `app/cmi/write.py` (`CMIWriteClient`) darf CMI-Write senden
  (Whitelist: `change.cgi` + 2 Read-back-Change-Endpunkte). Die Route baut
  auf `write()` auf (Read-back + `CMISafetyError`).
* **Whitelist** = die 9 setzbaren Adressen (`docs/plant-model.json`
  `settable.elements`); **RISK** `10520A10180` (Ölbrenner) = NIE
  schreibbar (beide Phasen, 403).
* **Confirmation-Hash** = `HMAC-SHA256(key=write_token,
  msg=adr \x00 to \x00 int(epoch//900))` (Design: `ts-fenster-15min`).
  Der Browser kennt den Write-Token NICHT; der Hash ist 15 min gültig
  (idempotent im Fenster).
* **Read-back** bestätigt jeden Write; Mismatch → `applied=false` + 502 +
  Audit (sichtbar, nicht „stumm“). **Kein Auto-Write** (v1).

## 5. Tests / Gates

* `tests/test_recommend_route.py` (17): fresh/stale/503, Cache (2nd-Call < TTL
  = kein 2. Run), TTL-Expire, force, Single-Flight, No-Poison-Retry,
  Token-401s, Engine-Holder (Import/Load/Caching), `_run_sync`, cancel-safe
  `aclose`, `berlin_hour_key`.
* `tests/test_change_route.py` (26): 100 % gemockt (`httpx.MockTransport` +
  `readback_delay=0` + injizierbare `now_fn`). Preview (kein CMI-Write),
  Hash falsch/abgelaufen = 403, RISK = 403, Write-ohne-Token = 403,
  Write-Token-unconfigured = 503, Success → Audit + Read-back, Mismatch →
  `applied=false` + 502 + Audit, `validate_target` (select/numeric/bit/leer),
  Helpers, `aclose`.
* `tests/test_ui_static.py` (14): KI-Tab/Panel/Dialog + `ki.js`-Marker,
  DOM-Selektor-Check, `node --check`.
* **Gate:** `pytest tests/ analysis/model/l2 analysis/model/m2b -q` grün
  (476 passed); `app/change_route.py` + `app/recommendation.py` je **99 %**
  Coverage, `app/` total **93 %**; `ruff check` clean auf allen neuen/
  geänderten Dateien.

## 6. Offene Punkte

* **v0.1-empfehlungen sind Advisory** — „Anwenden“ ist per CMI noch inaktiv
  (RAS-Offsets/Heizkurven = Q-107/108, TA-Designer/v2). Erst wenn die Engine
  eine Empfehlung mit `executability = setzbar-per-cmi` + `adresse` + `to`
  ausgeben kann, wird der Apply-Button aktiv (Future-Proofing steht).
* **CMI-`6.cgi`-Konfig-Track** (falls je gewünscht) = separater Track, nicht
  dieses Panel.
* **Live-Write** im UI-Lauf ist verboten (Grenze: No-Op ist bewiesen; echte
  Changes = HITL je Change, s. `Plan.md` §S4N-UI-KI).
