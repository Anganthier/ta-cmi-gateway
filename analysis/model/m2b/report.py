"""M2b — REPORT.md aus der Lauf-Zusammenfassung (``summary.json``).

v003: Vergleich mit dem Vorgänger-Artefakt (v002, :func:`load_reference`). Die
Referenzwerte werden beim Training in ``summary.json:reference_v002`` abgelegt, damit
``python -m analysis.model.m2b report`` ohne v002-Ordner reproduzierbar bleibt.
flue-Metriken von v002 sind ×10 umgerechnet (m1.2 = Rohwert/10, m1.3 = °C).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config

# v001-Referenzwerte (analysis/model/m2/REPORT.md + Diagnose-Session 2026-10-31,
# MAE +4 h, Valid 2025 / Test 2026; v001 = 1-h-Modell × 4-h-Rollout).
V001_REF: dict[str, tuple[str, str]] = {
    "ras_kell_ist": ("0.421", "0.717"),
    "ras_eg_ist": ("0.194", "0.444"),
    "ras_dg_ist": ("0.270", "0.545"),
    "buff_top": ("2.27", "2.08"),
    "buff_mid": ("2.05", "3.38"),
}
_REF_KEYS = ("mae", "rmse", "mae_b0", "rmse_b0")


def reference_from_summary(s: dict, scale: dict[str, float] | None = None) -> dict:
    """Kompakte Referenz (Metriken je Target@h + Responsivität) aus einer v002-``summary.json``.

    ``scale`` = Faktor je Target (Default ``config.V002_SCALE``: flue ×10).
    """
    scale = config.V002_SCALE if scale is None else scale
    m = s.get("metrics", {})
    out: dict = {"artifact": s.get("artifact"), "data_version": s.get("data_version"),
                 "scale": dict(scale), "metrics": {}, "responsivity": {}}
    for k, r in m.get("lgbm", {}).items():
        f = float(scale.get(k.split("@")[0], 1.0))
        row = {sp: {q: (r[sp][q] * f if r[sp].get(q) is not None else None)
                    for q in _REF_KEYS if q in r[sp]} for sp in ("valid", "test") if sp in r}
        rt = m.get("lgbm_refit", {}).get(k, {}).get("test")
        if rt:
            row["refit_test"] = {q: (rt[q] * f if rt.get(q) is not None else None) for q in _REF_KEYS if q in rt}
        out["metrics"][k] = row
    for mod, rz in s.get("responsivity", {}).items():
        out["responsivity"][mod] = {
            z: {"primary": r.get("primary"), "pearson_primary": r.get("pearson_primary"),
                "frac_neg": r.get(f"frac_neg_primary_at_{config.RESP_TARGET_DELTA}K"),
                "dod_passed": r.get("dod_passed")}
            for z, r in rz.items() if isinstance(r, dict)
        }
    return out


def load_reference(art_dir: Path | str | None) -> dict | None:
    """Referenz aus ``{art_dir}/summary.json`` (fehlt der Ordner → ``None``)."""
    if art_dir is None:
        return None
    p = Path(art_dir) / "summary.json"
    if not p.exists():
        return None
    return reference_from_summary(json.loads(p.read_text()))


def _f(v, nd: int = 3) -> str:
    if v is None:
        return "–"
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _ok(b) -> str:
    return "✅" if b else "❌"


def _pct(a, b) -> str:
    """Relative Änderung a gegenüber b in % (negativ = besser)."""
    try:
        return f"{100.0 * (float(a) / float(b) - 1.0):+.1f} %"
    except (TypeError, ValueError, ZeroDivisionError):
        return "–"


def _metric(s: dict, t: str) -> str:
    return "rmse" if s.get("target_kind", {}).get(t) == "digital" else "mae"


def render(summary: dict) -> str:
    s = summary
    m = s["metrics"]
    win = s["winner"]
    ref = s.get("reference_v002") or {}
    rm = ref.get("metrics", {})
    ver = str(s["artifact"]).rsplit("-", 1)[-1]
    L: list[str] = []
    L.append(f"# M2b-Report — L1-Modell {ver} (`{s['artifact']}`)\n")
    L.append(f"Erzeugt {s['created_at']} · git `{s['git_rev']}` · Daten `{s['data_version']}` · "
             f"Features `{s['derive_version']}` ({s['n_features']}, davon {s.get('n_raw_inputs', '–')} Rohspalten, "
             f"{s.get('n_new_features', '–')} neu ggb. v002) · Seed {s['seed']}\n")
    L.append(f"Doku/Entscheidungen: `docs/l1-model-{ver}.md` · Artefakt: `models/cmi-brain/{ver}/` · "
             f"Referenz: `{ref.get('artifact', '–')}` (Daten `{ref.get('data_version', '–')}`, "
             f"Skalierung {ref.get('scale', {})})\n")
    L.append("## 1. Ergebnis in einem Satz\n")
    L.append(f"Gewinner nach Q-101-Regel: **{win}** (mittlerer Skill vs. Persistence B0 auf Valid: "
             + ", ".join(f"{k} {_f(v)}" for k, v in s["mean_skill_valid"].items()) + "). "
             f"Deploy-LightGBM: `{s['deploy_lgbm']}`.\n")
    L.append("## 2. DoD (Valid 2025, +4 h)\n")
    L.append("| Modell | RAS-Ist < B0 und < 1,0 K | Puffer < 1,5 K | keine Explosion (max. abs. Vorhersage) | Responsivität |")
    L.append("|---|---|---|---|---|")
    for mod, d in s["dod"].items():
        r = s["responsivity"].get(mod, {})
        boom = ", ".join(d.get("exploded", [])[:4])
        L.append(f"| {mod} | {_ok(d['ras_ok'])} | {_ok(d['buff_ok'])} | {_ok(d['no_explosion'])} "
                 f"({_f(d['max_abs_pred'], 1)}{'; ' + boom if boom else ''}) "
                 f"| {_ok(r.get('dod_passed')) if r else '–'} |")
    L.append("")
    h = config.DOD_HORIZON
    L.append(f"## 3. Vergleich +{h} h: {ver} vs. v002 vs. B0 (Valid 2025 / Test 2026)\n")
    L.append(f"{ver}/v002 = Bewertungsmodell (Train bis 2024, Early-Stopping Valid); Refit = Deploy-Modell "
             "(Train+Valid, nur Test out-of-sample). B0 = Persistence auf den v003-Zeilen. "
             "Δ = Änderung v003 ggb. v002 (negativ = besser). v002-flue ×10.\n")
    L.append(f"| Target | Metrik | {ver} | {ver}-Refit (Test) | v002 | v002-Refit (Test) | B0 | Δ Valid | Δ Test (Refit) | v001 (Rollout) |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for t in s["targets"]:
        k = f"{t}@{h}"
        if k not in m.get("lgbm", {}):
            continue
        q = _metric(s, t)
        lg, rf, rr = m["lgbm"][k], m.get("lgbm_refit", {}).get(k, {}), rm.get(k, {})
        v1 = V001_REF.get(t, ("–", "–"))
        L.append(
            f"| {t} | {q.upper()} | {_f(lg.get('valid', {}).get(q))} / {_f(lg.get('test', {}).get(q))} "
            f"| {_f(rf.get('test', {}).get(q))} "
            f"| {_f(rr.get('valid', {}).get(q))} / {_f(rr.get('test', {}).get(q))} "
            f"| {_f(rr.get('refit_test', {}).get(q))} "
            f"| {_f(lg.get('valid', {}).get(q + '_b0'))} / {_f(lg.get('test', {}).get(q + '_b0'))} "
            f"| {_pct(lg.get('valid', {}).get(q), rr.get('valid', {}).get(q))} "
            f"| {_pct(rf.get('test', {}).get(q), rr.get('refit_test', {}).get(q))} "
            f"| {v1[0]} / {v1[1]} |")
    L.append("")
    hs = s["horizons"]
    L.append(f"## 4. Alle Horizonte kompakt ({ver} · v002 · B0; Valid | Test)\n")
    L.append("| Target | Metrik | " + " | ".join(f"+{x} h Valid | +{x} h Test" for x in hs) + " |")
    L.append("|---|---|" + "---|---|" * len(hs))
    for t in s["targets"]:
        q = _metric(s, t)
        cells = []
        for x in hs:
            r, rr = m["lgbm"].get(f"{t}@{x}", {}), rm.get(f"{t}@{x}", {})
            for sp in ("valid", "test"):
                cells.append(f"{_f(r.get(sp, {}).get(q))} · {_f(rr.get(sp, {}).get(q))} · "
                             f"{_f(r.get(sp, {}).get(q + '_b0'))}" if r else "–")
        L.append(f"| {t} | {q.upper()} | " + " | ".join(cells) + " |")
    L.append("")
    L.append(f"## 5. {ver} LightGBM: alle Targets × Horizonte (Valid / Test, Skill vs. B0 Valid)\n")
    L.append("| Target | Metrik | " + " | ".join(f"+{x} h" for x in hs) + " |")
    L.append("|---|---|" + "---|" * len(hs))
    for t in s["targets"]:
        dig = s.get("target_kind", {}).get(t) == "digital"
        key, sk = ("rmse", "skill_b0_rmse") if dig else ("mae", "skill_b0")
        cells = []
        for x in hs:
            r = m["lgbm"].get(f"{t}@{x}", {})
            if not r:
                cells.append("–")
                continue
            cells.append(f"{_f(r.get('valid', {}).get(key))} / {_f(r.get('test', {}).get(key))} "
                         f"(S {_f(r.get('valid', {}).get(sk), 2)})")
        L.append(f"| {t} | {key.upper()} | " + " | ".join(cells) + " |")
    L.append("\nS = Skill = 1 − Fehler/Fehler_B0 (> 0 besser als Persistence). Analog: MAE in K; "
             "Digital (An-Quotient 0..1): RMSE (Brier-artig, s. `evaluate.py`).\n")
    if "keras" in m:
        L.append(f"### 5b. Keras v2 (Referenz), +{h} h, Valid / Test\n")
        L.append("| Target | Metrik | Keras v2 | LightGBM | B0 |")
        L.append("|---|---|---|---|---|")
        for t in s["targets"]:
            k = f"{t}@{h}"
            if k not in m["keras"]:
                continue
            key = _metric(s, t)
            ke, lg = m["keras"][k], m["lgbm"].get(k, {})
            L.append(f"| {t} | {key.upper()} | {_f(ke.get('valid', {}).get(key))} / {_f(ke.get('test', {}).get(key))} | "
                     f"{_f(lg.get('valid', {}).get(key))} / {_f(lg.get('test', {}).get(key))} | "
                     f"{_f(lg.get('valid', {}).get(key + '_b0'))} / {_f(lg.get('test', {}).get(key + '_b0'))} |")
        L.append("")
    L.append("## 6. Offset-Responsivität v2 (je Etage, Heizsaison-Fälle)\n")
    L.append("Eingriff: Offset + perzipierte T der Etage um Δ ∈ {1; 2; 3,4; 5} K (v003 zusätzlich Minderer-Regel "
             "`hzrkl_{z}_rule` neu aus perzipiert_last + Δ). Wirkung = mittlere Änderung der Vorhersage über "
             "+1/+4/+6 h. DoD-Größe = Rücklaufminderer der Etage (`hzrkl_{z}_on`), "
             f"DoD-Zone = **{config.RESP_ZONE}** (Q-110). Werte: Pearson(Δ, Wirkung) / Anteil Wirkung < 0 bei +3,4 K.\n")
    fk = f"frac_neg_at_{config.RESP_TARGET_DELTA}K"
    eff_cols = ("hzrkl_on", "hzrkl_pos", "pump_hz_on", "ras_ist")
    L.append("| Modell (Fälle) | Zone | Ventil an (DoD) | Minderer-Temp. (`_pos`, °C) | Pumpe d8 | RAS-Ist +6 h | bestanden | v002 (DoD-Größe) |")
    L.append("|---|---|---|---|---|---|---|---|")
    rresp = ref.get("responsivity", {})
    for mod, rz in s["responsivity"].items():
        for z, r in rz.items():
            if not isinstance(r, dict):
                continue
            cells = []
            for e in eff_cols:
                name = e.replace("hzrkl", f"hzrkl_{z}") if e.startswith("hzrkl") else e
                pc, fr = r.get("pearson", {}).get(name), r.get(fk, {}).get(name)
                cells.append("–" if pc is None and fr is None else f"{_f(pc, 2)} / {_f(fr, 2)}")
            dz = " (DoD)" if z == rz.get("dod_zone") else ""
            v2 = rresp.get(mod, {}).get(z, {})
            v2c = f"{_f(v2.get('pearson_primary'), 2)} / {_f(v2.get('frac_neg'), 2)}" if v2 else "–"
            L.append(f"| {mod} ({rz.get('split', 'valid')}) | {z}{dz} | " + " | ".join(cells)
                     + f" | {_ok(r.get('dod_passed'))} | {v2c} |")
    L.append("")
    L.append("## 7. Monotonie-Varianten (LightGBM, Valid-Fehler frei vs. monoton)\n")
    L.append("| Target@h | frei | monoton | gewählt |")
    L.append("|---|---|---|---|")
    for k, v in s["lgbm_variants"].items():
        if v.get("valid_mae_mono") is not None:
            L.append(f"| {k} | {_f(v['valid_mae_plain'], 4)} | {_f(v['valid_mae_mono'], 4)} | {'monoton' if v['monotone'] else 'frei'} |")
    L.append("")
    L.append(f"## 8. Feature-Importance (+{h} h, Gain-Anteil, Bewertungsmodell)\n")
    L.append("### 8a. Neue v003-Merkmale (Rohspalten m1.3 + Ableitungen m2b-f3)\n")
    imp_new = s.get("importance_new", {})
    for t in config.BUFF_TARGETS:
        v = imp_new.get(f"{t}@{h}")
        if v:
            L.append(f"- **`{t}@{h}`** — Anteil neue Merkmale gesamt {_f(v['share_total'], 3)}: "
                     + ", ".join(f"{n} {g}" for n, g in v["top"]))
    L.append("")
    if imp_new:
        L.append("| Target@h | Anteil neue Merkmale | Top 3 neu |")
        L.append("|---|---|---|")
        for k, v in imp_new.items():
            L.append(f"| {k} | {_f(v['share_total'], 3)} | " + ", ".join(f"{n} {g}" for n, g in v["top"][:3]) + " |")
        L.append("")
    L.append("### 8b. Top 8 gesamt\n")
    for k, imp in s.get("importance", {}).items():
        L.append(f"- `{k}`: " + ", ".join(f"{n} {g}" for n, g in imp))
    L.append("")
    L.append("## 9. Lauf-Metadaten\n")
    L.append(f"- Stunden: {s['n_hours']} · Splits: {s['split_rows']} · Nicht-1-h-Rasterschritte (DST): {s['n_nonhourly_steps']}")
    L.append(f"- Laufzeiten: {s['timings']}")
    its = [v.get("best_iter") for v in s["lgbm_variants"].values() if v.get("best_iter") is not None]
    n_cap = sum(1 for v in s["lgbm_variants"].values()
                if v.get("best_iter") is not None and v["best_iter"] >= s["config"].get("lgbm_num_boost", 0))
    if its:
        L.append(f"- LightGBM-Runden: max {max(its)} · am Rundenlimit {s['config'].get('lgbm_num_boost')}: {n_cap} Modelle")
    L.append(f"- Konfiguration: {json.dumps(s['config'], ensure_ascii=False)}")
    return "\n".join(L) + "\n"


def write_report(summary: dict, path: Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render(summary))
    return p
