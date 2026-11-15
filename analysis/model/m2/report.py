"""M2 — REPORT.md-Generierung (DoD-Checkliste mit exakten Zahlen).

Die Numbers kommen aus dem Build-Run (``results``-Dict, s.
:mod:`m2.build`). Der Report ist **vollständig deterministisch** aus
``results`` (keine Datei-IO außer Schreiben von ``REPORT.md``).
"""
from __future__ import annotations

from pathlib import Path

from . import config


def _fmt(x: float | None, nd: int = 3) -> str:
    if x is None:
        return "n/a"
    try:
        if x != x:  # NaN
            return "n/a"
    except TypeError:
        return str(x)
    return f"{x:.{nd}f}"


def _group_mae(maes: dict[str, float], cols: tuple[str, ...] | list[str]) -> float:
    """Mittel-MAE über die Spalten (nur vorhandene)."""
    vals = [maes[c] for c in cols if c in maes and maes[c] is not None]
    return float(sum(vals) / len(vals)) if vals else float("nan")


def _device_label(devices: list[str] | None) -> str:
    """Device-Label aus dem Report-Context (nicht hardcodiert):
    ``"Device: GPU:0"`` (bzw. mehrere) oder ``"Device: CPU"``."""
    ds = [d for d in (devices or []) if d]
    return "Device: " + (", ".join(ds) if ds else "CPU")


def _run_tag(devices: list[str] | None) -> str:
    """Run-Tag für den Frameworks-Zeilenkopf: GPU-Run vs. CPU-Run."""
    ds = [d for d in (devices or []) if d]
    return "(GPU-Run, deterministischer Seed + TF_CUBLAS_DETERMINISTIC=1)" if ds else "(CPU-Run, deterministisch)"


def render_report(r: dict[str, object]) -> str:
    """Rendert REPORT.md (vollständig aus ``r``)."""
    L: list[str] = []
    a = L.append
    stats: dict = r["stats"]
    split_info: dict = r["split_info"]
    maes_k4 = r["maes_keras_valid_4h"]
    maes_b4 = r["maes_b2_valid_4h"]
    maes_l4 = r["maes_lgbm_valid_4h"]
    resp_k: dict = r["responsivity_keras"]
    resp_l: dict = r["responsivity_lgbm"]
    test_report: dict = r["test_2026"]
    arch: dict = r["architecture"]
    decision: dict = r["decision"]

    a("# M2 — REPORT: L1 Keras-Anlagen-Modell (1-h-Step, action-conditional)")
    a("")
    a(f"Stand: {r['generated_at']} · Seed **{config.SEED}** · data_version **{config.DATA_VERSION}** · "
      f"git-Rev **{r.get('git_rev', 'n/a')}**")
    a("")
    devices = r.get("devices") or []
    a(f"Frameworks: TF **{r['tf_version']}** / Keras **{r['keras_version']}** / LightGBM **{r['lgbm_version']}** / "
      f"NumPy **{r['numpy_version']}** · {_device_label(devices)} · "
      f"LGBM-Device: **{r.get('lgbm_device', 'cpu')}** {_run_tag(devices)}.")
    a("")
    a("---")
    a("## 1. Daten & Übergangs-Tabelle")
    a("")
    a("| Größe | Wert |")
    a("|---|---|")
    a(f"| Rows Feature-Store (gesamt) | {stats['n_rows_total']:,} |")
    a(f"| Rows mit `gap_full=1` (defekte/gap-Rasterstunden) | {stats['n_rows_gap_full']:,} |")
    a(f"| Drops: aktuelle Row `gap_full=1` | {stats.get('n_drops_gap_current', 0):,} |")
    a(f"| Drops: fehlende Next-Row (Raster-Ende) | {stats['n_drops_missing_next']:,} |")
    a(f"| Drops: Next-Row (t+1) `gap_full=1` | {stats['n_drops_gap_full_next']:,} |")
    a(f"| **Transitions gesamt** | **{stats['n_transitions']:,}** |")
    a(f"| Train ({split_info['train_start']} → {split_info['train_end']}) | {stats['split_sizes'].get('train', 0):,} |")
    a(f"| Valid ({split_info['valid_start']} → {split_info['valid_end']}) | {stats['split_sizes'].get('valid', 0):,} |")
    a(f"| Test ({split_info['test_start']} → {split_info['test_end']}) | {stats['split_sizes'].get('test', 0):,} |")
    a(f"| Imputierte NULL-Zellen (F/B-Fill + Median-Fallback) | {stats['n_imputed_cells']:,} |")
    a("")
    a("**Spalten-Entscheidungen:**")
    a(f"- Targets: {len(config.ANALOG_TARGETS)} analog + {len(config.DIGITAL_TARGETS)} digital "
      "(An-Quotient, Ziel-Bucket).")
    a("- **Kollektor** = `solar_coll` (dev2 a16, M1-SCHEMA §2.7). Vor 2022-03-24 NULL → "
      "Forward-/Median-Imputation + NULL-Flag-Feature `_tgt_null` (Modell sieht „Kollektor inaktiv“).")
    a("- **Wetter-T**: bias-korrigierte `wx_t{k}_t_bias` (M1 Bias-Profil dev0 a8 vs Grid, "
      "saisonal median + Drift) — Begründung: Sensor-Referenz, Grid-Bias raus; z-score "
      "normalisiert ebenso wie die Roh-T (reproduzierbar, framework-neutral).")
    a(f"- Inputs: {len(config.FEATURE_INPUTS)} Feature-Spalten + NULL-Flags "
      f"(Train: {r['n_flag_cols']} Flags). Digital-Actions = 6 Schalter "
      "(d5/d8/d_solar = Ergebnisse der aktuellen Stunde → **kein** Aktion-Input, "
      "nur Target/Kontext). Offsets (Keller/EG/DG) = Stellgrößen-Input "
      "(historisch = von der RAS-Logik gesetzt).")
    a("- Flags `mode_known` (konstant 0) / `offset_known` als informativ übernommen "
      "(dokumentiert: konstant/informativ, kein Lern-Signal historisch).")
    a("")
    a("---")
    a("## 2. Modell")
    a("")
    a("### 2.1 Architektur (Keras-L1)")
    a("")
    a(f"- Geteilter Stamm: Dense {config.HIDDEN[0]} → {config.HIDDEN[1]} → {config.HIDDEN[2]} "
      f"(ReLU, Dropout {config.DROPOUT}) + **einer Head pro Target** (Dense {config.HEAD_HIDDEN} → 1).")
    a("- Digital-Targets (9): Sigmoid + MAE auf Wahrscheinlichkeit (Initial-Design; "
      "BCE-Alternative dokumentiert als Annahme).")
    a(f"- **Parameter-Count: {arch['n_params']:,}** (Vorgabe ≤ {config.MAX_PARAMS:,} → "
      f"{'OK' if arch['n_params'] <= config.MAX_PARAMS else 'VERLETZT'}).")
    a("- Loss: gewichtetes MAE (Initial-Gewichte, dokumentiert): RAS ist/perc 1,0 · "
      "offset 0,8 · hz_vl/hz_rl/buff 0,7 · heat_min 0,5 · ww/oil/wood/flue/collector/digital 0,4.")
    a(f"- Training: Batch {config.BATCH_SIZE}, LR {config.LEARNING_RATE}, "
      f"Early-Stopping (Patience {config.PATIENCE}, Best-Epoch-Weights), "
      f"Ende Epoch {r.get('keras_last_epoch', 'n/a')}/{config.EPOCHS_DEFAULT} · "
      f"{_device_label(devices)}.")
    a("")
    a("### 2.2 Trainings-Kurve (Valid-Loss, gewichtete Summe der Head-MAEs)")
    a("")
    hist = r.get("keras_valid_history", [])
    if hist:
        a("| Epoch | Val-Loss (Keras, gewichtete Summe) |")
        a("|---|---|")
        step = max(1, len(hist) // 20)
        for i, h in enumerate(hist):
            if i % step == 0 or i == len(hist) - 1:
                a(f"| {i + 1} | {_fmt(h)} |")
        a(f"| **best** | **{_fmt(r.get('keras_valid_loss_best'))}** |")
    else:
        a("(keine History; Best-epoch-Loss s. u.)")
    a("")
    a("### 2.3 Keras vs LightGBM (Valid 2025, 4-h-Rollout)")
    a("")
    a("| Target-Gruppe | Keras MAE+4h | B2 MAE+4h | LGBM MAE+4h | Gewinner |")
    a("|---|---|---|---|---|")
    groups = [
        ("RAS-Ist Keller", "ras_kell_ist",),
        ("RAS-Ist EG", "ras_eg_ist",),
        ("RAS-Ist DG", "ras_dg_ist",),
        ("Puffer Oben", "buff_top",),
        ("Puffer Mitte", "buff_mid",),
        ("Puffer Unten", "buff_bot",),
        ("hz_vl", "hz_vl",),
        ("heat_min_kell", "heat_min_kell",),
    ]
    for label, col in groups:
        kv = _fmt(maes_k4.get(col))
        bv = _fmt(maes_b4.get(col))
        lv = _fmt(maes_l4.get(col))
        cands = [
            ("Keras", maes_k4.get(col)),
            ("B2", maes_b4.get(col)),
            ("LGBM", maes_l4.get(col)),
        ]
        cands = [(n, v) for n, v in cands if v is not None]
        winner = min(cands, key=lambda x: x[1])[0] if cands else "n/a"
        a(f"| {label} | {kv} | {bv} | {lv} | **{winner}** |")
    a("")
    a("**Offset-Responsivität (Valid 2025, 200 Fälle × 4 Δ, 6-h-Rollout):**")
    a("")
    a("| Größe | Keras | LightGBM |")
    a("|---|---|---|")
    a(f"| Pearson(Δoffset, ΔH) | {_fmt(resp_k.get('pearson_dH'), 3)} | {_fmt(resp_l.get('pearson_dH'), 3)} |")
    a(f"| Pearson(Δoffset, ΔD) | {_fmt(resp_k.get('pearson_dD'), 3)} | {_fmt(resp_l.get('pearson_dD'), 3)} |")
    a(f"| Anteil ΔH<0 bei +3,4 K | {_fmt(resp_k.get('frac_dH_neg_34'), 4)} | {_fmt(resp_l.get('frac_dH_neg_34'), 4)} |")
    a(f"| ΔH-Mittel +3,4 K | {_fmt(resp_k.get('mean_dH_34'))} | {_fmt(resp_l.get('mean_dH_34'))} |")
    a("")
    a("### 2.4 Decision (Q-101): model_kind")
    a("")
    reason = str(decision.get("reason", "s. DoD-Checkliste"))
    a(f"**Gewonnen: `{decision['model_kind']}`** — Begründung: {reason}.")
    a("(Beide Artefakte werden gebaut und versioniert; deployment-fähig = model_kind.)")
    a("")
    a("---")
    a("## 3. DoD-Checkliste (Valid 2025)")
    a("")
    a("DoD wird auf **beiden** Modellfamilien ausgewiesen; das Gesamtergebnis "
      "bezieht sich auf das gewinnende Modell (``model_kind``).")
    a("")
    a("**DoD 1 — MAE +4 h RAS-Ist je Zone < B2 UND < 1,0 K:**")
    k4, l4 = maes_k4, maes_l4
    dod1_k = dod1_l = []
    for col in config.RAS_IST_TARGETS:
        b = maes_b4.get(col)
        k = k4.get(col)
        lg = l4.get(col)
        ok_k = k is not None and b is not None and k < b and k < 1.0
        ok_l = lg is not None and b is not None and lg < b and lg < 1.0
        dod1_k.append(ok_k)
        dod1_l.append(ok_l)
        a(f"- `{col}`: B2 {_fmt(b)} K · Keras {_fmt(k)} K ({'✅' if ok_k else '❌'}) · LGBM {_fmt(lg)} K ({'✅' if ok_l else '❌'})")
    a("**DoD 2 — MAE +4 h Puffer < 1,5 K je buff:**")
    dod2_k = dod2_l = []
    for col in config.BUFF_TARGETS:
        k = k4.get(col)
        lg = l4.get(col)
        ok_k = k is not None and k < 1.5
        ok_l = lg is not None and lg < 1.5
        dod2_k.append(ok_k)
        dod2_l.append(ok_l)
        a(f"- `{col}`: Keras {_fmt(k)} K ({'✅' if ok_k else '❌'}) · LGBM {_fmt(lg)} K ({'✅' if ok_l else '❌'})")
    a("**DoD 3 — Offset-Responsivität (Kern, 18.10-Muster): Pearson(Δoffset,ΔH) ≤ −0,6 UND ΔH<0 bei +3,4 K ≥ 75 %:**")
    resp_dec = resp_l if decision["model_kind"] == "lightgbm" else resp_k
    lbl_dec = "LGBM" if decision["model_kind"] == "lightgbm" else "Keras"
    ok3a = resp_dec.get("pearson_dH") is not None and resp_dec["pearson_dH"] <= -0.6
    ok3b = (resp_dec.get("frac_dH_neg_34") or 0) >= 0.75
    a(f"- Keras: Pearson(Δoffset,ΔH) = {_fmt(resp_k.get('pearson_dH'))} · Anteil ΔH<0 bei +3,4 K = {_fmt(resp_k.get('frac_dH_neg_34'), 4)}")
    a(f"- LGBM:  Pearson(Δoffset,ΔH) = {_fmt(resp_l.get('pearson_dH'))} · Anteil ΔH<0 bei +3,4 K = {_fmt(resp_l.get('frac_dH_neg_34'), 4)}")
    a(f"- **{lbl_dec}** (model_kind): Pearson = {_fmt(resp_dec.get('pearson_dH'))} → {'✅' if ok3a else '❌'} (≤ −0,6) · "
      f"Anteil ΔH<0 bei +3,4 K = {_fmt(resp_dec.get('frac_dH_neg_34'), 4)} → {'✅' if ok3b else '❌'} (≥ 0,75)")
    a(f"- ΔH-Mittel Keras: +1,0 K = {_fmt(resp_k.get('mean_dH_10'))} · +3,4 K = {_fmt(resp_k.get('mean_dH_34'))} · +5,0 K = {_fmt(resp_k.get('mean_dH_50'))} · "
      f"ΔD +3,4 K = {_fmt(resp_k.get('mean_dD_34'))} (negativ = Pumpe weniger an)")
    dod_w = dod1_l + dod2_l + [ok3a, ok3b] if decision["model_kind"] == "lightgbm" else dod1_k + dod2_k + [ok3a, ok3b]
    a("")
    a(f"**Gesamt ({lbl_dec} = model_kind): " + ("✅ DoD erfüllt" if all(dod_w) else "⚠️ DoD NICHT vollständig erfüllt — s. Probleme/Annahmen") + "**")
    a("")
    a("---")
    a("## 4. Test-2026-Report (final, kein Tuning)")
    a("")
    a("| Target-Gruppe | Keras MAE+4h (Test) | B2 MAE+4h (Test) | LGBM MAE+4h (Test) |")
    a("|---|---|---|---|")
    for label, col in [
        ("RAS-Ist Keller", "ras_kell_ist"), ("RAS-Ist EG", "ras_eg_ist"), ("RAS-Ist DG", "ras_dg_ist"),
        ("Puffer Oben", "buff_top"), ("Puffer Mitte", "buff_mid"), ("Puffer Unten", "buff_bot"),
        ("hz_vl", "hz_vl"), ("heat_min_kell", "heat_min_kell"),
    ]:
        a(f"| {label} | {_fmt(test_report.get('keras', {}).get(col))} | "
          f"{_fmt(test_report.get('b2', {}).get(col))} | {_fmt(test_report.get('lgbm', {}).get(col))} |")
    wc = test_report.get("worst_day")
    if wc:
        a("")
        a(f"**Worst-Case-Tag (max. Tages-MAE RAS-Ist, Keras): {wc['date']}** — "
          f"MAE Kell {_fmt(wc.get('ras_kell_ist'))} K · EG {_fmt(wc.get('ras_eg_ist'))} K · "
          f"DG {_fmt(wc.get('ras_dg_ist'))} K (maintainer-Review-Punkt).")
    a("")
    a("---")
    a("## 5. Offset-Responsivitäts-Details (Methode & Interpretation)")
    a("")
    a("**Methode:** 200 zufällige, lückenfreie Valid-Zustände (Seed 20261031, keine "
      "NULL-Features/-Targets, ≥ 6 h Raster-Abstand zum Ende) × Keller-Offset-Steps "
      "Δ ∈ {+1,0; +2,0; +3,4; +5,0 K}. Pro Zustand 6-h-Rollout mit (a) Offset-Halt "
      "und (b) Offset+Δ (alle übrigen Aktionen/Offsets = t0-Werte → reiner "
      "Offset-Kontrastfakt). ΔH = Σ heat_min_kell(0–6 h) im Δ-Fall minus Hold; "
      "ΔD = Mittel pump_hz_on(d8) Differenz. Wetter-Fenster im Rollout konstant.")
    a("")
    pk = resp_k
    a(f"**Zahlen (Keras):** Pearson(Δoffset, ΔH) = **{_fmt(pk.get('pearson_dH'))}** · "
      f"Pearson(Δoffset, ΔD) = {_fmt(pk.get('pearson_dD'))} · "
      f"Anteil ΔH<0 bei +3,4 K = **{_fmt(pk.get('frac_dH_neg_34'), 4)}** · "
      f"ΔH-Mittel +3,4 K = {_fmt(pk.get('mean_dH_34'))}.")
    a(f"**Zahlen (LGBM):** Pearson(Δoffset, ΔH) = **{_fmt(resp_l.get('pearson_dH'))}** · "
      f"Anteil ΔH<0 bei +3,4 K = **{_fmt(resp_l.get('frac_dH_neg_34'), 4)}** · "
      f"ΔH-Mittel +3,4 K = {_fmt(resp_l.get('mean_dH_34'))}.")
    a("")
    a("**Interpretation (reproduziert das 18.10-Muster?):** "
      + r.get("responsivity_interpretation", "(siehe DoD-Checkliste)."))
    a("")
    a("---")
    a("## 6. Probleme / Annahmen")
    a("")
    for p in r.get("assumptions", []):
        a(f"- {p}")
    a("")
    a("---")
    a("## 7. Reproduktion")
    a("")
    a("```bash")
    a("python -m analysis.model.m2 build   # Train → Valid → Test → Responsivität → Artefakt → Report")
    a("```")
    a(f"Artefakt: `{config.rel_to_repo(config.ARTIFACT_DIR)}` (git-Rev {r.get('git_rev', 'n/a')}, "
      f"Seed {config.SEED}).")
    a("")
    return "\n".join(L) + "\n"


def write_report(results: dict[str, object], path: Path | None = None) -> Path:
    p = Path(path) if path is not None else config.REPORT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_report(results))
    return p
