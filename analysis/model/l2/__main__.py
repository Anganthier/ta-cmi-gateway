"""CLI: ``python -m analysis.model.l2 recommend`` (L2 v0.2 Empfehlungse-Engine).

Beispiele::

    python -m analysis.model.l2 recommend                      # jetzt (Europe/Berlin)
    python -m analysis.model.l2 recommend --ts "2026-11-01 18:00"
    python -m analysis.model.l2 recommend --json               # nur JSON (Stdout)
    python -m analysis.model.l2 recommend --no-wx              # Fallback (Store-Wetter)

Optionen: ``--no-save`` (keine JSON/REPORT-Datei), ``--runs-dir`` (Ziel für
JSON/REPORT), ``--features-dir``/``--model`` (READ-ONLY-Quellen überschreiben),
``--wx`` (eigener Live-Forecast-Pfad? nein: ``--no-wx`` nur; sonst fetch).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import config, weather
from .recommend import recommend, save_json, write_report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="l2",
        description="L2 v0.2 Empfehlungse-Engine (Advisory, offline; L1 v003 + LIVE-Forecast)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    rc = sub.add_parser("recommend", help="Empfehlung für eine volle Berliner Stunde")
    rc.add_argument("--ts", default=None, help="'YYYY-MM-DD HH:MM' (Default: jetzt, volle Stunde)")
    rc.add_argument("--json", action="store_true", help="JSON nach Stdout statt DE-Zusammenfassung")
    rc.add_argument("--no-save", action="store_true", help="keine JSON/REPORT-Datei schreiben")
    rc.add_argument("--no-wx", action="store_true", help="LIVE-Forecast überspringen (Store-Wetter + stale)")
    rc.add_argument("--runs-dir", type=Path, default=None, help="Ziel-Verzeichnis für JSON/REPORT")
    rc.add_argument("--features-dir", type=Path, default=None, help="Feature-Store (Default data/features)")
    rc.add_argument("--model", type=Path, default=None, help="L1-Artefakt (Default models/cmi-brain/v003)")
    el = sub.add_parser(
        "estimate-lags",
        help="v0.5 Ticket D: pro-Etage-Lag-Schätzung (10-s-Rohdaten, Kreuzkorrelation) "
        "→ data/l2/rm_lag_estimate.json (einmalig, caches-fähig)",
    )
    el.add_argument("--data-dir", type=Path, default=None, help="Daten-Root (Default data/)")
    el.add_argument("--window-days", type=int, default=30, help="Fenster in Tagen (Default 30)")
    el.add_argument("--now", default=None, help="'YYYY-MM-DD HH:MM:SS' (Default: jetzt Berlin)")
    el.add_argument("--out", type=Path, default=None, help="Ziel-JSON (Default data/l2/rm_lag_estimate.json)")
    cv = sub.add_parser(
        "calibrate-vl",
        help="v0.5 Ticket C: VL-Soll-Kalibrierung (Heizstunden-Ø je Außen-T-Band "
        "3 K vs UVR-Standard-Heizkurve) → data/l2/vl_kalibrierung.json "
        "(einmalig, caches-fähig; Rebuild-Befehl für das Kalibrierungs-Artefakt)",
    )
    cv.add_argument("--features-dir", type=Path, default=None,
                    help="Feature-Store (Default data/features)")
    cv.add_argument("--window-days", type=int, default=None,
                    help="Heizsaison-Fenster in Tagen (Default: VL_KALIB_WINDOW_DAYS = 365)")
    cv.add_argument("--now", default=None, help="'YYYY-MM-DD HH:MM:SS' (Default: jetzt Berlin)")
    cv.add_argument("--out", type=Path, default=None,
                    help="Ziel-JSON (Default data/l2/vl_kalibrierung.json)")
    cvm = sub.add_parser(
        "calibrate-vl-min",
        help="v0.6-B: L2-Heizkurve v2 — VL-Minimum je Außen-T-Band (3 K), bei "
        "dem die Räume am Soll blieben (ab Lernstart 2026-11-07 16:14, "
        "Heizstunden-Only) → data/l2/vl_minimum.json (einmalig, caches-fähig; "
        "Rebuild-Befehl für das VL-Minimum-Artefakt)",
    )
    cvm.add_argument("--features-dir", type=Path, default=None,
                     help="Feature-Store (Default data/features)")
    cvm.add_argument("--now", default=None,
                     help="'YYYY-MM-DD HH:MM:SS' (Default: jetzt Berlin)")
    cvm.add_argument("--out", type=Path, default=None,
                     help="Ziel-JSON (Default data/l2/vl_minimum.json)")
    a = ap.parse_args(argv)

    # --- v0.5 Ticket C: VL-Kalibrierung (einmalig, caches-fähig) ---
    if a.cmd == "calibrate-vl":
        from . import vl_calibration as vl_cal

        art = vl_cal.calibrate_vl(
            features_dir=a.features_dir,
            window_days=a.window_days,
            now=a.now,
            out_path=a.out,
        )
        out_path = a.out or vl_cal.vl_calibration_path()
        print("L2 v0.5 · VL-Soll-Kalibrierung (Heizstunden-Ø je Außen-T-Band 3 K)")
        print(f"  window: {art.get('window_days')} d ab {art.get('window_since')} · "
              f"{art.get('n_heizstunden')} Heizstunden (pump_hz_on ≥ 0.5)")
        for b in sorted(art.get("bands") or {}, key=int):
            bb = art["bands"][b]
            usable = "usable" if bb.get("usable") else "zu wenig n"
            print(
                f"  Band {bb.get('band_lo'):>4}…{bb.get('band_hi'):>4} °C: VL-Ø "
                f"{bb.get('vl_mean')} °C (n={bb.get('n')}, {usable}) · Kurve-Soll "
                f"{bb.get('curve_vl_soll')} °C · dev {bb.get('dev_k'):+} K"
            )
        print(f"  out: {out_path}")
        print("  Rebuild-Befehl: python -m analysis.model.l2 calibrate-vl "
              "(pro recommend() nur Cache-Read)")
        return 0

    # --- v0.6-B: VL-Minimum (einmalig, caches-fähig) ---
    if a.cmd == "calibrate-vl-min":
        from . import vl_minimum as vl_min

        art = vl_min.calibrate_vl_min(
            features_dir=a.features_dir,
            now=a.now,
            out_path=a.out,
        )
        out_path = a.out or vl_min.vl_minimum_path()
        print("L2 v0.6-B · VL-Minimum je Außen-T-Band 3 K (Räume am Soll, "
              f"ab Lernstart {config.VL_MIN_LEARN_START})")
        print(f"  {art.get('n_heizstunden')} Heizstunden (pump_hz_on ≥ "
              f"{config.VL_KALIB_HEAT_EPS}, ab Lernstart) · "
              f"Quantil {art.get('quantile')} · Am-Soll-Toleranz "
              f"−{art.get('am_soll_tol_k')} K · min_n {art.get('min_n_per_band')}")
        for b in sorted(art.get("bands") or {}, key=int):
            bb = art["bands"][b]
            usable = "usable" if bb.get("usable") else "nicht kalibriert"
            vl_min_txt = f"{bb.get('vl_min_soll')} °C" if bb.get("vl_min_soll") is not None else "—"
            print(
                f"  Band {bb.get('band_lo'):>4}…{bb.get('band_hi'):>4} °C: VL-Minimum "
                f"{vl_min_txt} (n_am_soll={bb.get('n_am_soll')}/{bb.get('n_heizstunden')}, "
                f"{usable})"
            )
        print(f"  out: {out_path}")
        print("  Rebuild-Befehl: python -m analysis.model.l2 calibrate-vl-min "
              "(pro recommend() nur Cache-Read)")
        return 0

    # --- v0.5 Ticket D: Lag-Schätzung (einmalig, caches-fähig) ---
    if a.cmd == "estimate-lags":
        from . import rm_diagnostics as rm_diag

        art = rm_diag.estimate_lags(
            data_dir=a.data_dir,
            window_days=a.window_days,
            now=a.now,
            out_path=a.out,
        )
        out_path = a.out or rm_diag.lag_estimate_path(a.data_dir)
        zones = art.get("zones") or {}
        print("L2 v0.5 · Lag-Schätzung (10-s-Rohdaten, Kreuzkorrelation)")
        print(f"  window: {art.get('window_days')} d · {art.get('n_samples_total')} Samples")
        print(f"  method: {art.get('method')}")
        for z in list(config.ZONES) + ["central"]:
            zz = zones.get(z) or {}
            print(
                f"  {z:7s}: lag {zz.get('lag_s')} s · corr {zz.get('corr')} · "
                f"n {zz.get('n')} · conf {zz.get('confidence')} · {zz.get('source')}"
            )
        print(f"  out: {out_path}")
        return 0

    from .recommend import FETCH

    ts = a.ts or datetime.now(UTC).astimezone(weather.BERLIN).strftime("%Y-%m-%d %H:00")
    rec = recommend(
        ts,
        features_dir=a.features_dir,
        artifact_dir=a.model,
        runs_dir=a.runs_dir,
        wx=None if a.no_wx else FETCH,
    )
    json_out: Path | None = None
    if not a.no_save:
        json_out = save_json(rec, rec.ts, runs_dir=a.runs_dir)
        write_report(rec, json_out)
    if a.json:
        print(json.dumps(rec.to_json(), indent=2, ensure_ascii=False))
        return 0
    # DE-Zusammenfassung (kompakt; Details in REPORT.md + JSON).
    k = rec.to_json()["kontext"]
    print(f"L2 v0.2 · {rec.ts} · {rec.model_version} · Horizont {rec.horizon_h} h")
    print(f"  stale: {rec.stale}" + (f" — {'; '.join(rec.stale_reasons)}" if rec.stale_reasons else ""))
    print(
        f"  Datenstand: {k['datenstand']} ({k['data_version']}, Zustand {k['state_ts']}) · "
        f"Wetter: {k['wx_quelle']}" + (f", Run {k['wx_run']}" if k.get("wx_run") else "")
        + (", Alter " + str(k.get("wx_age_h")) + " h" if k.get("wx_age_h") is not None else "")
    )
    for e in rec.empfehlungen:
        mark = "➜" if e.get("changed") else "●"
        print(f"  {mark} {e['parameter']}: {e['aktuell']} → {e['vorgeschlagen']} "
              f"(Konfidenz {e['konfidenz']}, {e['executability']})")
        print(f"      {e['begruendung_de'][:160]}")
    if rec.guardrail_hits:
        print("  Guardrail-Hits:")
        for h in rec.guardrail_hits:
            print(f"    - {h}")
    if json_out is not None:
        print(f"  JSON: {json_out}")
        print(f"  REPORT: {config.REPORT_PATH if a.runs_dir is None else a.runs_dir / 'REPORT.md'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
