# M1-Inkremental — Feature-Store-Rebuild auf „aktueller + letzter Monat"

Status: **✅ ABGESCHLOSSEN + DEPLOYED** (2026-11-02, Subagent python-pro + Orchestrator-Abnahme; maintainer-Review „Y“).
Commit `ce8a2fb` + Anker `agent-last-state` + Cron-Umschalt (stündlich inkrementell + wöchentlich Audit).
`data_version` bleibt `m1.3`.

**Effekt (gemessen, reale Daten):** stündlicher Build **~4,2 s statt ~86 s** (≈20×),
**1 Datei-Write/h statt 76** (Raspi/ARM-relevant: CPU + schreibende Disk-IO).

## Architektur (kurz)

- **Kontext:** `load_context()` liest die letzten **48 Zeilen** der Vor-Monat-Store-Datei
  (volles Feature-Store-Schema, `hourly_agg`-98-Column-Auswahl + `ts_bucket`) →
  `ContextBundle(table, seeds, month)`. Gültig nur bei: exaktem stündlichen Raster,
  Ende = letztes Monats-Stundende (23:00), `data_version`-Match, ≥48 Zeilen —
  sonst `None` → Fallback `--full`.
- **Seeds:** je Zone `hzrkl_{kell,eg,dg}_rule_last` der letzten Kontext-Zeile =
  gehaltener UVR-Regelzustand am Vormonat-Ende (Events können >48 h zurückliegen
  → nicht aus dem Fenster ableitbar).
- **Window-Brücke:** `resample.build_hourly_sql(..., rule_seed=…)` seedet
  `hzrkl_{z}_rule` (Forward-Fill `LAST_VALUE(event IGNORE NULLS)` über
  `COALESCE(…, seed)`). 48 Kontext-Zeilen + frischer Monat laufen gemeinsam
  durch die **kanonische** `features_final`-Query-Form
  (`final_features_view_sql` + `create_final_views` + `register_wx_and_sun`,
  gemeinsam für Full und inkrementell) → LAG/SUM/SINCE-Fenster sehen exakt
  den benötigten Vormonat-Kontext. Kontext-Zeilen bleiben **draußen** im Output.
- **Write:** atomar `.parquet.part` + `os.replace`, **nur** die Monatsdatei des
  aktuellen Monats; alle übrigen Monatsdateien byte-beharrt. `_meta.json` atomar
  aktualisiert (ohne 76-Monate-Scan).

## CLI (`python -m analysis.model.m1 features …`)

| Befehl | Verhalten |
|---|---|
| (Default) | **inkrementell**: aktueller Monat aus `cmi_log_pq`/`winsol_pq`-Monatsdatei + Kontext + Seeds |
| `--full` | kompletter Rebuild (alle Monate; Wartung/Audit; deterministisch auf frozen Eingabe) |
| `--verify` | **Audit**: Full-Build in-memory (schreibt NIEMALS), Diff gegen Live-Store → Exit 0 ok / 3 Drift / 2 unverifizierbar |

**Fallback → `--full`** (Log-Zeile, Exit 0) bei: (a) keine Rohdaten,
(c) `_meta.json`-`data_version` ≠ `m1.3`, (b) fehlende Vor-Monat-Datei /
Kontext <48 h / Raster-Defekt / Version-Mismatch in `load_context`,
(d) aktueller Monat ohne neue Rohdaten (Store bereits bis letzte volle Stunde
→ kein Fortschritt).

## `--verify`-Semantik (korrigiertes DoD-1)

**Strikte Bit-Identität inkrementell≡full ist nicht erreichbar** (messbar):
DuckDBs parallele Float-Aggregation weicht zwischen 19-Mio-Zeilen-Glob (full)
und Monats-Rohdatei (inkrementell) in der Summenfolge der letzten ULP-Bits ab
(gemessen rel. ~1e-13; auf gleicher *Roh-Datei* bit-determinisch, 4/4 Runs +
2 Prozesse). Der Full-Build auf **live wandernder** `cmi_log_pq` (5-min-Poller)
ist selbst zwischen zwei Läufen ≠ bit-identisch.

Ehrlicher, ML-korrekturstandard (LightGBM gegen ~1e-13 invariant):
- **diskret/NULL-Spalten:** bit-exakt (geschlossene Stunden),
- **Float-Spalten:** relative Toleranz `VERIFY_FLOAT_REL_TOL=1e-9`
  (4 Größenordnungen über dem Messdrift),
- **offenes Fenster** `VERIFY_OPEN_WINDOW_H=2 h` am Ende der Referenz:
  Hinweis „Store-Refresh ausstehend", **kein** Drift,
- Zeilen im Store, die die Referenz nicht enthält (`only-store`), oder
  geschlossene `only-ref`-Stunden = Drift (Exit 3).

## Gates (2026-11-02, finale Abnahme)

- **Tests:** `pytest analysis/model/m1 analysis/model/l2 analysis/model/m2b` →
  **322 passed** (inkl. 22 neu `tests/test_incremental.py`:
  inkrementell≡full geschlossene Stunden (diskret bit-exakt / Float rel ≤1e-9),
  rule-Seed-Carry-over über Monatsgrenze, Seed-NULL-Pfad, `load_context`-Edge-Cases
  (Datei fehlt, <48 h, Version-Mismatch, Raster-Defekt), CLI Default/`--full`/
  `--verify`/Drift-Exit-3/offenes-Fenster-Hinweis, alle 4 Fallback-Ästze).
- **ruff:** `ruff check analysis/model/m1/` → All checks passed.
- **Coverage neue Pfade:** `build.py` 84 %, `features.py` 88 %, Total 86 % (DoD ≥80 ✓).
- **0 `.parquet.part`-Reste** nach jedem Lauf.
- **Befangene Subagent-Bugs (Orchestrator gefixt):**
  (i) `--verify`-Display-Bug (Diff-Beispiel-Query wählte 2 Spalten, Format las 3
  → IndexError, maskierte echten Befund);
  (ii) **naive/aware-DateTime-Vergleich** im Fallback-(d)
  (`store_max_ts` naive vs. `last_full_hour_wall` aware BERLIN → TypeError,
  live noch nicht getriggert).

## Live-Nachweis (host-seitig, reale Daten, 2026-11-02 16:45–16:53)

1. **Inkrementeller Lauf 1:** 4,15 s, 41 Zeilen, Kontext 48 h aus 2026-10,
   Seeds `{kell: 0.0, eg: 0.0, dg: 1.0}`, nur `2026/11/2026-11.parquet`
   geschrieben, 0 `.part`-Reste.
2. **Lauf 2 (hintereinander):** korrekt **Fallback (d)** → deterministischer
   Full-Rebuild (85 s, 76 Dateien, 54.113 Zeilen) — no-progress-Guard live
   demonstriert; Endzustand = sauberer `--full`-Referenzzustand.
3. **`GET /api/v1/recommend?force=true`** (Read-Token): `stale=false`,
   `datenstand=2026-11-02 16:00:00` (= letzte volle Stunde), 4 Empfehlungen,
   `model_version=cmi-brain-v003+l2-v001`.

## Offene Punkte

- Cron: stündlich `:15` inkrementell (Default) + So 03:00 `--full --verify`
  (Drift-Alarm: Exit 3 = Alarm) + `--full` (Referenz-Sync) — installiert
  2026-11-02 (maintainer-Go „Y“), `flock -n /tmp/cmi-m1-features.lock`,
  Logs `data/m1_feature_build.log` / `data/m1_feature_audit.log`.
  Nächster stündlicher Lauf: `:15`; nächster Audit-So: 03:00.
- L2 v0.2 (dynamische executability), echte S3-1-Write je Schalter (HITL),
  M1 m1.4/v004 (Minderer `_on` ab 10-27), Q-111 Plan-B-RAS (separate Tracks).
