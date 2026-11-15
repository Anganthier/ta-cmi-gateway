"""M1-Pipeline-CLI.

Aufruf (aus dem Repo-Root):
    python -m analysis.model.m1 weather   # Open-Meteo-Backfill (Fair-Use)
    python -m analysis.model.m1 bias      # Bias-Profil (dev0 a8 vs Grid)
    python -m analysis.model.m1 features  # Feature-Store (Default = inkrementell)
    python -m analysis.model.m1 features --full    # kompletter Rebuild (Wartung/Audit)
    python -m analysis.model.m1 features --verify  # Audit: in-memory-Referenz vs Store
    python -m analysis.model.m1 all       # weather → bias → features
    python -m analysis.model.m1 report    # REPORT.md aktualisieren

Optionen:
    --force        Cache überspringen (nur weather)
    --fetch-end YMD  Wetter-Enddatum (Default: config.WX_FETCH_END)
    --quiet        weniger Log

features-only (M1-Columnar, 2026-11):
    --full             columnar Full-Build (vektorierte 692-Spalten-Assembly
                       statt finale SQL-Query; RAM-Budget 500 MB via
                       config.con; monolithisch nur noch als Test-Orakel)
    --no-progress      Fallback-(d) „kein Fortschritt“ → SKIP + Exit 0 statt
                       Full-Run (Pi-HH:15-Default: kein 21-GB-Vollscan zwischen
                       den :15-Läufen)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from . import backfill, bias, config, features, report, resample

# --- --verify (Audit-Modus, M1-Inkremental) -----------------------------------
#: Offenes Fenster (h) am Ende der Full-Referenz, in dem der Store-Stand
#: hinter der live wachsenden Roh-Quelle (cmi_log_pq, 5-min-Poller)
#: liegen KANN: Zeilen dort sind KEIN Drift (Hinweis "Store-Refresh
#: ausstehend"), nicht Abweichung. 2 h > Poller-Zyklus (5 min) + Build-
#: Dauer (~5 s inkrementell) mit großem Sicherheitsfaktor.
VERIFY_OPEN_WINDOW_H = 2
#: Relative Toleranz für Float-Spalten bei geschlossenen Stunden:
#: inkrementell (kleine Monats-Rohdatei) vs. full (19-Mio-Zeilen-Glob)
#: kann in der Summenfolge der letzten Float64-ULP-Bits abweichen
#: (DuckDBs parallele Aggregation; ~1e-13 relativ gemessen). 1e-9 liegt
#: 4 Größenordnungen darüber: für ML-Feature-Verbrauch irrelevant, aber
#: fängt echte Abweichungen (Fehler in Seeds/Windows/Join-Logik) klar ab.
VERIFY_FLOAT_REL_TOL = 1e-9


def _git_tag() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        tag = out.stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        return f"{tag}{'-dirty' if dirty else ''}"
    except Exception:  # noqa: BLE001 - kein git → Fallback
        return "nogit"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _cfg(args: argparse.Namespace) -> config.BuildConfig:
    return config.BuildConfig(fetch_end=args.fetch_end)


def _log(args: argparse.Namespace, *a) -> None:
    if not getattr(args, "quiet", False):
        print(*a)


def cmd_weather(args: argparse.Namespace) -> int:
    log = print if not args.quiet else lambda *_a: None
    stats = backfill.run_backfill(cfg=_cfg(args), force=args.force, log=log)
    return 0 if not stats.failures else 2


def cmd_bias(args: argparse.Namespace) -> int:
    con = config.con()
    cfg = _cfg(args)
    bias.prepare_con(con, cfg)
    result = bias.compute_bias(con, cfg)
    p = bias.save_profile(result, cfg.bias_profile)
    print(f"[bias] Profil → {p}")
    for s, prof in result.as_profile().items():
        print(
            f"   {s}: median={prof['offset_k']:+.3f} K drift={prof['drift_k_per_day']:+.5f} K/Tag "
            f"(n={prof['n_hours']}/{prof['season_hours']})"
        )
    print(
        f"[bias] Gesamt: {result.n_hours_total} gültige Stunden, Grid-Coverage={result.grid_coverage}"
    )
    con.close()
    return 0


# --- M1-Inkremental ---------------------------------------------------------------


def _raw_max_ts_from_stats(root: Path) -> datetime | None:
    """Max-Roh-ts aller Hive-Monate (Parquet-Row-Group-Statistiken, kein Daten-Scan)."""
    best: str | None = None
    for p in sorted(root.glob("*/*/*.parquet")):
        try:
            pf = pq.ParquetFile(p)
        except Exception:  # noqa: BLE001 - kaputtes Parquet → ignorieren
            continue
        md = pf.metadata
        if md.num_rows == 0:
            continue
        idx = pf.schema_arrow.get_field_index("ts")
        for rg in range(md.num_row_groups):
            st = md.row_group(rg).column(idx).statistics
            if st is None:
                continue
            try:
                mx = st.max
            except Exception:  # noqa: BLE001
                continue
            if mx is None:
                continue
            v = str(mx.decode() if isinstance(mx, bytes) else mx)
            if best is None or v > best:
                best = v
    return datetime.fromisoformat(best) if best else None


def _current_month(args: argparse.Namespace) -> tuple[str, datetime | None]:
    """Aktueller Datenmonat ('YYYY-MM') + Max-Roh-ts (None, wenn gar keine Rohdaten)."""
    cfg = _cfg(args)
    raw_max = None
    for root in (cfg.winsol_pq, cfg.cmi_log_pq):
        ts = _raw_max_ts_from_stats(root)
        if ts is not None and (raw_max is None or ts > raw_max):
            raw_max = ts
    if raw_max is None:
        return "", None
    last_hour = features.last_full_hour_wall(raw_max)
    return f"{last_hour.year:04d}-{last_hour.month:02d}", raw_max


def _store_meta_data_version(cfg: config.BuildConfig, month: str) -> str | None:
    """``data_version`` aus ``_meta.json`` (None, wenn Datei fehlt)."""
    p = cfg.features_dir / "_meta.json"
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f).get("data_version")
    except (json.JSONDecodeError, OSError):
        return None


def _write_store_meta(
    cfg: config.BuildConfig,
    coverage: dict,
    bias_profile: dict,
    wx_stats: dict,
    git_tag: str,
    generated_at: str,
) -> None:
    """``_meta.json`` schreiben (atomar, wie ``features.store_meta``)."""
    meta = {
        "data_version": features.DATA_VERSION,
        "git_tag": git_tag,
        "generated_at": generated_at,
        "features_dir": str(cfg.features_dir),
        "coverage": coverage,
        "bias_profile": bias_profile,
        "wx_cache": wx_stats,
        "schema_doc": "analysis/model/m1/SCHEMA.md",
    }
    p = cfg.features_dir / "_meta.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.part")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)  # atomic rename within the same directory


def _source_coverage_from_stats(root: Path, name: str) -> dict:
    """Quellen-Coverage JEINER Hive-Quelle aus Parquet-Metadaten/Statistiken.

    KEIN Daten-Scan (``report.coverage``-19,2-Mio-Row-Scan vermeiden):
    ``n_rows`` aus der File-Metadaten-Summe, ``first``/``last`` (Roh-ts) aus
    den Row-Group-``ts``-Statistiken (min/max), ``missing`` aus dem
    Parquet-Schema (``resample.SOURCE_COLUMNS``-Abgleich). Struktur ≡
    ``resample.check_source_schema`` (``name``/``n_cols``/``missing``/
    ``n_rows``/``first``/``last``).
    """
    out: dict = {"name": name, "n_cols": 0, "missing": [], "n_rows": 0, "first": None, "last": None}
    files = sorted(root.glob("*/*/*.parquet"))
    if not files:
        return out
    n_rows = 0
    cols: set[str] = set()
    first: str | None = None
    last: str | None = None
    for p in files:
        try:
            pf = pq.ParquetFile(p)
        except Exception:  # noqa: BLE001 - kaputtes Parquet → ignorieren
            continue
        md = pf.metadata
        n_rows += md.num_rows
        cols.update(pf.schema_arrow.names)
        idx = pf.schema_arrow.get_field_index("ts")
        for rg in range(md.num_row_groups):
            st = md.row_group(rg).column(idx).statistics
            if st is None:
                continue
            try:
                mn, mx = st.min, st.max
            except Exception:  # noqa: BLE001
                continue
            if mn is not None:
                v = str(mn.decode() if isinstance(mn, bytes) else mn)
                if first is None or v < first:
                    first = v
            if mx is not None:
                v = str(mx.decode() if isinstance(mx, bytes) else mx)
                if last is None or v > last:
                    last = v
    out.update(
        n_cols=len(cols),
        missing=[c for c in resample.SOURCE_COLUMNS if c not in cols],
        n_rows=n_rows,
        first=first,
        last=last,
    )
    return out


def _coverage_for_full(cfg: config.BuildConfig, parts: list[Path]) -> dict:
    """Coverage-Block für ``_meta.json`` (Full-Build, M1-Columnar).

    Ersetzt ``report.coverage`` (19,2-Mio-Row-Scan der Roh-Quellen) durch
    Parquet-Metadaten/Statistiken (``_source_coverage_from_stats``, kein
    Daten-Scan) + die frisch geschriebenen Store-Dateien (Metadata-Only).
    Struktur ≡ ``report.coverage`` (``sources``/``feature_store``/``gaps``),
    damit ``_meta.json``-Verbraucher unverändert funktionieren.
    """
    out: dict = {"sources": {}, "feature_store": {}, "gaps": []}
    for name, root in (("winsol_pq", cfg.winsol_pq), ("cmi_log_pq", cfg.cmi_log_pq)):
        out["sources"][name] = _source_coverage_from_stats(root, name)
    if parts:
        meta0 = pq.read_metadata(parts[0])
        kv = meta0.metadata or {}
        out["feature_store"] = {
            "files": len(parts),
            "first": str(parts[0]),
            "last": str(parts[-1]),
            "rows": sum(pq.ParquetFile(p).metadata.num_rows for p in parts),
            "cols": len(pq.read_schema(parts[0])),
            "data_version": (kv.get(b"data_version") or b"").decode(),
            "git_tag": (kv.get(b"git_tag") or b"").decode(),
        }
    else:
        out["feature_store"] = {"files": 0}
    return out


def _coverage_for_month(con, cfg: config.BuildConfig, cur_month: str, prev_month: str) -> dict:
    """Coverage-Block für ``_meta.json`` ohne 76-Monate-Roh-Scan.

    Quellen: max-ts über Parquet-Statistiken (alle Hive-Monate, <1 s);
    Feature-Store: nur die Monatsdateien des Vormonats + des aktuellen Monats
    (die einzigen, die der inkrementelle Build anfasst).
    """
    sources: dict = {}
    for name, root in (("winsol_pq", cfg.winsol_pq), ("cmi_log_pq", cfg.cmi_log_pq)):
        files = sorted(root.glob("*/*/*.parquet"))
        if not files:
            continue
        try:
            sources[name] = resample.check_source_schema(con, str(root / "*/*/*.parquet"), name)
        except Exception as exc:  # noqa: BLE001 - read-only-Quellen sollten immer lesbar sein
            sources[name] = {"error": str(exc)[:120]}
            continue
    fs_files = [
        features.month_file_path(cfg.features_dir, prev_month),
        features.month_file_path(cfg.features_dir, cur_month),
    ]
    fs_files = [p for p in fs_files if p.exists()]
    if fs_files:
        meta0 = pq.read_metadata(fs_files[0])
        kv = meta0.metadata or {}
        sources_meta = {
            "files": len(fs_files),
            "first": str(fs_files[0]),
            "last": str(fs_files[-1]),
            "rows": sum(pq.ParquetFile(p).metadata.num_rows for p in fs_files),
            "cols": len(pq.read_schema(fs_files[0])),
            "data_version": (kv.get(b"data_version") or b"").decode(),
            "git_tag": (kv.get(b"git_tag") or b"").decode(),
        }
    else:
        sources_meta = {"files": 0}
    return {"sources": sources, "feature_store": sources_meta, "incremental": True}


def cmd_features(args: argparse.Namespace) -> int:
    """Feature-Store bauen. Default = inkrementell (aktueller Monat); ``--full``
    = kompletter Rebuild (columnar, M1-Columnar); ``--verify`` = Audit
    (columnare in-memory-Referenz vs. Store); ``--no-progress`` =
    Fallback-(d)-SKIP.

    Inkrementeller Build: aktueller Monat aus den Roh-Quellen + 48 Kontext-
    Stunden des Vormonats (aus der Feature-Store-Datei) + ``hzrkl_rule``-Seeds;
    Write = nur die Monatsdatei des aktuellen Monats (atomar). Fallback →
    columnar ``--full`` (Log-Zeile, Exit 0) bei fehlenden Rohdaten (a),
    ``data_version``-Mismatch (c), fehlender Vor-Monat-Datei / Kontext <48 h
    (b) — echte Defekt-Situationen. Fallback (d) „kein Fortschritt“ (Store bis
    zur letzten vollen Stunde, Normalzustand zwischen den :15-Läufen) →
    ``--no-progress``: SKIP + Exit 0 (kein 21-GB-Full-Scan, R4'); ohne Flag:
    columnar Full (lokal unveränderte Semantik).
    """
    cfg = _cfg(args)
    profile = bias.load_profile(cfg.bias_profile)
    git_tag = _git_tag()
    generated_at = _now_iso()

    if getattr(args, "verify", False):
        return _features_verify(args, cfg, profile, git_tag, generated_at)

    full = getattr(args, "full", False)
    no_progress = getattr(args, "no_progress", False)
    con = config.con()
    try:
        if full:
            _log(
                args,
                f"[features] FULL-Build (alle Monate, columnar, {features.DATA_VERSION})",
            )
            parts = features.store_features(
                con,
                cfg,
                profile,
                git_tag,
                generated_at,
                log=lambda m: _log(args, m),
            )
            total = sum(_count_rows(p) for p in parts)
            meta = features.store_meta(
                cfg,
                _coverage_for_full(cfg, parts),
                profile or {},
                backfill.cache_summary(cfg.wx_cache_dir),
                git_tag,
                generated_at,
            )
            _log(
                args,
                f"[features] FULL: {len(parts)} Monatsdateien, {total} Zeilen → {cfg.features_dir}",
            )
            _log(args, f"[features] Meta → {meta}")
            _log(args, f"[features] data_version={features.DATA_VERSION} git_tag={git_tag}")
            return 0

        # --- Inkrementell (Default) ---
        cur_month, raw_max = _current_month(args)
        if raw_max is None:
            _log(args, "[features] FALLBACK→full: keine Rohdaten in winsol_pq/cmi_log_pq")
            return _features_full(args, cfg, profile, git_tag, generated_at, log=True)

        if _store_meta_data_version(cfg, cur_month) not in (None, features.DATA_VERSION):
            _log(
                args,
                f"[features] FALLBACK→full: _meta.json data_version "
                f"!= {features.DATA_VERSION} (Store-Referenz unzuverlässig)",
            )
            return _features_full(args, cfg, profile, git_tag, generated_at, log=True)

        prev_month = features.prev_month_str(cur_month)
        prev_store = features.month_file_path(cfg.features_dir, prev_month)

        # Fallback (d): aktueller Monat ohne neue Rohdaten → kein Fortschritt.
        # Default (lokal): wie heute → columnar Full (sicherer Referenz-Sync).
        # ``--no-progress`` (Pi-HH:15-Default): SKIP + Exit 0, KEIN Full-Run —
        # zwischen den :15-Läufen ist „Store bis zur letzten vollen Stunde" der
        # NORMALZUSTAND; der frühere Auto-Full-Trigger war der Auslöser des
        # Pi-Host-OOM (21-GB-Scan neben ~2,5 GB Baseline, kein Swapfile, R4').
        cur_store = features.month_file_path(cfg.features_dir, cur_month)
        last_hour_now = features.last_full_hour_wall(raw_max)
        # Beide Seiten als naive Berlin-Wallclock vergleichen (store_max_ts liefert
        # naive Wallclock; last_full_hour_wall liefert aware BERLIN → .replace).
        last_hour_now_naive = last_hour_now.replace(tzinfo=None)
        if cur_store.exists():
            store_last = features.store_max_ts(cur_store)
            if store_last is not None and store_last >= last_hour_now_naive:
                if no_progress:
                    _log(
                        args,
                        f"[features] SKIP: kein Fortschritt (aktueller Monat {cur_month}: "
                        f"Store bis {store_last}, letzte volle Stunde {last_hour_now_naive}); "
                        f"--no-progress → kein Full-Run",
                    )
                    return 0
                _log(
                    args,
                    f"[features] FALLBACK→full: aktueller Monat {cur_month} ohne neue "
                    f"Rohdaten (Store bis {store_last}, letzte volle Stunde {last_hour_now_naive})",
                )
                return _features_full(args, cfg, profile, git_tag, generated_at, log=True)

        ctx = features.load_context(prev_month, prev_store, data_version=features.DATA_VERSION)
        if ctx is None:
            _log(
                args,
                f"[features] FALLBACK→full: Kontext des Vormonats {prev_month} nicht "
                f"verwendbar (Datei fehlt: {not prev_store.exists()}, "
                f"mindestens {features.CONTEXT_HOURS} Zeilen + data_version={features.DATA_VERSION} nötig)",
            )
            return _features_full(args, cfg, profile, git_tag, generated_at, log=True)

        # --- Columnar (M1-Columnar): Phase A–B (hourly_agg) + WX + Sun in
        # con, Phase E (692-Spalten-Assembly) vektorisiert auf PyArrow.
        # Write = nur die Monatsdatei des aktuellen Monats (Kontext-Monat
        # wird übersprungen, Vormonat bleibt anfassfrei). ---
        parts = features.assemble_features_incremental_columnar(
            con,
            cfg,
            profile,
            git_tag,
            generated_at,
            ctx,
            cur_month,
            raw_max,
            out_dir=cfg.features_dir,
        )
        path = parts[0]
        n_rows = pq.ParquetFile(path).metadata.num_rows
        _write_store_meta(
            cfg,
            _coverage_for_month(con, cfg, cur_month, prev_month),
            profile or {},
            backfill.cache_summary(cfg.wx_cache_dir),
            git_tag,
            generated_at,
        )
        _log(
            args,
            f"[features] INKREMENTELL: {cur_month} neu ({n_rows} Zeilen, "
            f"Kontext {features.CONTEXT_HOURS} h aus {prev_month}, "
            f"Seeds={ {z: ctx.seeds[z] for z in config.HZRKL_ZONES} })",
        )
        _log(args, f"[features] → {path} (alle übrigen Monatsdateien anfassfrei)")
        return 0
    finally:
        con.close()


def _features_full(args, cfg, profile, git_tag, generated_at, *, log: bool = False) -> int:
    """Deterministischer columnar Full-Build (auch als Fallback, Exit 0)."""
    con = config.con()
    try:
        if log:
            _log(args, "[features] FULL-Build (Fallback, columnar)")
        parts = features.store_features(
            con,
            cfg,
            profile,
            git_tag,
            generated_at,
            log=(lambda m: _log(args, m)) if log else None,
        )
        total = sum(_count_rows(p) for p in parts)
        meta = features.store_meta(
            cfg,
            _coverage_for_full(cfg, parts),
            profile or {},
            backfill.cache_summary(cfg.wx_cache_dir),
            git_tag,
            generated_at,
        )
        if log:
            _log(
                args,
                f"[features] FULL: {len(parts)} Monatsdateien, {total} Zeilen → {cfg.features_dir}",
            )
            _log(args, f"[features] Meta → {meta}")
        return 0
    finally:
        con.close()


def _features_verify(args, cfg, profile, git_tag, generated_at) -> int:
    """``--verify`` (Audit): **columnare** Monats-Referenz in-memory (NIEMALS
    nach ``data/features`` schreiben), Diff der Monatsdatei des aktuellen
    Datenmonats gegen den Live-Store.

    M1-Columnar: die Referenz ist jetzt der columnare Monats-Run
    (``columnar_month_reference``: 48 Kontext-Stunden des Vormonats + aktueller
    Monat, vektorisierte 692-Spalten-Assembly; RAM ≈ Monatsgröße + kleine
    wx/hourly_agg-Schichten) statt des monolithischen Full-Build (Peak ~21 GB
    ohne memory_limit), datenidentisch (Kriterium wie M1-Inkremental/DoD-1).

    Exit 0 = identisch; Exit 3 = Abweichung (Drift); Exit 2 = unverifizierbar
    (kein Store / kein aktueller Monat / Vormonat-Kontext unbrauchbar).
    """
    cur_month, raw_max = _current_month(args)
    if raw_max is None:
        _log(args, "[verify] keine Rohdaten → unverifizierbar (exit 2)")
        return 2
    store_path = features.month_file_path(cfg.features_dir, cur_month)
    if not store_path.exists():
        _log(args, f"[verify] Store-Datei {store_path} fehlt → unverifizierbar (exit 2)")
        return 2
    prev_month = features.prev_month_str(cur_month)
    ctx = features.load_context(
        prev_month,
        features.month_file_path(cfg.features_dir, prev_month),
        data_version=features.DATA_VERSION,
    )
    if ctx is None:
        _log(
            args,
            f"[verify] Kontext des Vormonats {prev_month} nicht verwendbar "
            f"(mindestens {features.CONTEXT_HOURS} h + "
            f"data_version={features.DATA_VERSION} nötig) → unverifizierbar (exit 2)",
        )
        return 2
    con = config.con()
    try:
        cur_table = features.columnar_month_reference(
            con, cfg, profile, git_tag, generated_at, cur_month, raw_max, ctx
        )
        store_tbl = pq.read_table(store_path)
        problems, notes = _diff_month_tables(con, store_tbl, cur_table, month=cur_month)
        for note in notes:
            _log(args, f"[verify] Hinweis: {note}")
        if not problems:
            _log(
                args,
                f"[verify] OK: {cur_month} stimmt mit der in-memory-Referenz überein "
                f"({cur_table.num_rows} Zeilen × {cur_table.num_columns} Spalten; "
                f"diskret bit-exakt, Float rel-Tol {VERIFY_FLOAT_REL_TOL}, "
                f"offenes Fenster {VERIFY_OPEN_WINDOW_H} h ignoriert)",
            )
            return 0
        _log(args, f"[verify] DRIFT: {cur_month} weicht von der Referenz ab:")
        for d in problems[:10]:
            _log(args, f"   {d}")
        if len(problems) > 10:
            _log(args, f"   … ({len(problems) - 10} weitere)")
        return 3
    finally:
        con.close()


def _diff_month_tables(con, store_tbl, ref_tbl, *, month: str) -> tuple[list[str], list[str]]:
    """Zellen-/Zeilen-Diff zweier Monats-Tabellen (ein SQL-Pass pro Kategorie).

    Ignoriert die Lauf-Flags ``git_tag``/``generated_at`` (Build-spezifisch).

    **Audit-Grundlagen (M1-Inkremental, DoD 1 korrigiert):**

    * ``--verify`` rechnet die Full-Referenz **neu** (in-memory) und diffet gegen
      die im Store gespeicherte Monatsdatei. Die Roh-Quelle ``cmi_log_pq`` wächst
      aber live (5-min-Poller) → die letzten ``VERIFY_OPEN_WINDOW_H`` Stunden der
      Referenz sind „offen" (Store-Stand kann älter sein). Diese Zeilen sind
      **kein** Drift: nur als Hinweis (Store braucht ggf. Refresh).
    * Geschlossene Stunden (``ts < ref_max - open_window``) werden gecheckt:
      **diskrete Spalten** (ints, bools, strings, Timestamps, NULL-Pattern)
      = bit-exakt (``IS NOT DISTINCT FROM``); **Float-Spalten** = relative
      Toleranz ``VERIFY_FLOAT_REL_TOL`` (DuckDBs parallele Aggregation ist
      auf gleicher *Roh-Datei* bit-determinisch, aber inkrementell (kleine
      Monatsdatei) vs. full (19-Mio-Zeilen-Glob) kann in der Summenfolge der
      letzten ULP-Bits abweichen — intrinsisch, für ML irrelevant:
      LightGBM/Prognosen sind gegen ~1e-13 invariant).
    * only-ref-Zeilen **außerhalb** des offenen Fensters = Store vermisst
      geschlossene Stunden → Drift. only-store-Zeilen = immer Drift
      (Zeile im Store, die die Full-Referenz nicht mehr enthält).

    Returns ``(problems, notes)``: ``problems`` nicht leer → Drift (Exit 3);
    ``notes`` = erwartete/neutralen Befunde (z. B. offenes Fenster).
    """
    out: list[str] = []
    notes: list[str] = []
    if set(store_tbl.column_names) != set(ref_tbl.column_names):
        out.append(
            "schema: only-store="
            f"{sorted(set(store_tbl.column_names) - set(ref_tbl.column_names))[:8]} "
            f"only-ref={sorted(set(ref_tbl.column_names) - set(store_tbl.column_names))[:8]}"
        )
        return out, notes
    cols = [c for c in store_tbl.column_names if c not in ("git_tag", "generated_at")]
    con.register("v_store", store_tbl)
    con.register("v_ref", ref_tbl)
    ref_max = con.execute("SELECT max(ts) FROM v_ref").fetchone()[0]
    if ref_max is None:
        return ["leere Referenz"], notes
    cutoff = ref_max - timedelta(hours=VERIFY_OPEN_WINDOW_H)
    cutoff_lit = f"TIMESTAMP '{cutoff.isoformat()}'"
    ref_cut_lit = f"TIMESTAMP '{ref_max.isoformat()}'"

    # --- Zeilen-Abdeckung (FULL-JOIN auf ts) ---
    row = con.execute(
        f"""
        SELECT count(*) FILTER (WHERE r.ts IS NULL) AS only_store,
               count(*) FILTER (WHERE s.ts IS NULL AND r.ts < {cutoff_lit}) AS only_ref_closed,
               count(*) FILTER (WHERE s.ts IS NULL AND r.ts >= {cutoff_lit}) AS only_ref_open
        FROM v_store s
        FULL OUTER JOIN v_ref r ON s.ts = r.ts
        """
    ).fetchone()
    n_only_store, n_only_ref_closed, n_only_ref_open = (int(row[0]), int(row[1]), int(row[2]))
    if n_only_store:
        out.append(
            f"ts-Abdeckung: only-store={n_only_store} (Store-Zeilen fehlen in der Referenz → Drift)"
        )
    if n_only_ref_closed:
        out.append(
            f"ts-Abdeckung: only-ref-geschlossen={n_only_ref_closed} (Store vermisst geschlossene Stunden → Drift)"
        )
    if n_only_ref_open:
        notes.append(
            f"ts-Abdeckung: only-ref-offen={n_only_ref_open} (Zeilen im offener Fenster "
            f"{cutoff.isoformat()[:16]}–{ref_cut_lit} — erwartet, Store-Refresh ausstehend)"
        )

    # --- Zellen-Diff NUR geschlossene Stunden (ts < cutoff) ---
    # R4'-RAM: 690 parallele ``count(*) FILTER``-Aggregate explodieren den
    # per-Thread-Partial-Aggregate-Status in DuckDB (500 MB-Limit bei 20+
    # Threads → OOM, gemessen). Daher in Blöcken à 200 Spalten (Semantik
    # identisch, nur mehr Pässe; Monatsgröße → schnell).
    def _col_expr(c: str) -> str:
        return (
            f"count(*) FILTER (WHERE NOT (s.{c} IS NOT DISTINCT FROM r.{c}) AND s.ts < {cutoff_lit}) AS n_{c}"
            if not pa.types.is_floating(ref_tbl.column(c).type)
            else f"count(*) FILTER (WHERE ((s.{c} IS NULL) <> (r.{c} IS NULL)) "
            f"OR (s.{c} IS NOT NULL AND r.{c} IS NOT NULL "
            f"AND abs(s.{c} - r.{c}) > {VERIFY_FLOAT_REL_TOL} * greatest(1.0, abs(r.{c}))) "
            f"AND s.ts < {cutoff_lit}) AS n_{c}"
        )

    diffs: dict[str, int] = {}
    for lo in range(0, len(cols), 200):
        block = cols[lo : lo + 200]
        row = con.execute(
            f"""
            SELECT {", ".join(_col_expr(c) for c in block)}
            FROM v_store s
            JOIN v_ref r ON s.ts = r.ts
            WHERE s.ts < {cutoff_lit}
            """
        ).fetchone()
        for c, n in zip(block, row, strict=True):
            if int(n):
                diffs[c] = int(n)
    for c, n in diffs.items():
        is_float = pa.types.is_floating(ref_tbl.column(c).type)
        where = (
            (
                f"((s.{c} IS NULL) <> (r.{c} IS NULL)) "
                f"OR (s.{c} IS NOT NULL AND r.{c} IS NOT NULL "
                f"AND abs(s.{c} - r.{c}) > {VERIFY_FLOAT_REL_TOL} * greatest(1.0, abs(r.{c})))"
            )
            if is_float
            else f"NOT (s.{c} IS NOT DISTINCT FROM r.{c})"
        )
        sv = con.execute(
            f"""
            SELECT s.ts AS ts, s.{c} AS s, r.{c} AS r
            FROM v_store s JOIN v_ref r ON s.ts = r.ts
            WHERE {where} AND s.ts < {cutoff_lit}
            ORDER BY s.ts LIMIT 3
            """
        ).fetchall()
        out.append(
            f"{c}: {n} Zeile(n) weichen ab (geschlossene Stunden, "
            f"{'rel-Tol ' + str(VERIFY_FLOAT_REL_TOL) if is_float else 'bit-exakt'}), z. B. "
            + ", ".join(f"ts={row_[0]} store={row_[1]!r} ref={row_[2]!r}" for row_ in sv)
        )
    return out, notes


def _count_rows(p: Path) -> int:
    return pq.ParquetFile(p).metadata.num_rows


def cmd_all(args: argparse.Namespace) -> int:
    rc = cmd_weather(args)
    if rc != 0:
        print("[all] weather fehlgeschlagen (Fehlmonate) → Abbruch", file=sys.stderr)
        return rc
    rc = cmd_bias(args)
    if rc != 0:
        return rc
    rc = cmd_features(args)
    if rc != 0:
        return rc
    cmd_report(args)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    out = report.build_report()
    print(f"[report] {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="m1", description="M1-Pipeline (Wetter + Feature-Store)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in (
        ("weather", cmd_weather),
        ("bias", cmd_bias),
        ("features", cmd_features),
        ("report", cmd_report),
        ("all", cmd_all),
    ):
        p = sub.add_parser(name)
        p.add_argument("--force", action="store_true", help="Cache überspringen")
        p.add_argument(
            "--fetch-end", default=config.WX_FETCH_END, help="Wetter-Enddatum YYYY-MM-DD"
        )
        p.add_argument("--quiet", action="store_true")
        p.set_defaults(fn=fn)
    # M1-Inkremental: features-only-Flags
    feat_p = sub.choices["features"]
    feat_p.add_argument(
        "--full",
        action="store_true",
        help="kompletter Rebuild aller Monate, columnar (Default = inkrementell)",
    )
    feat_p.add_argument(
        "--no-progress",
        dest="no_progress",
        action="store_true",
        help="Fallback-(d) 'kein Fortschritt' → SKIP + Exit 0 statt "
        "Full-Run (Pi-HH:15-Default; s. R4')",
    )
    feat_p.add_argument(
        "--verify",
        action="store_true",
        help="Audit: columnare in-memory-Referenz (aktueller Monat) vs. Store "
        "(schreibt NIE, Exit≠0 bei Drift)",
    )
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
