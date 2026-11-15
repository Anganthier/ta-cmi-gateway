"""CLI: ``python -m analysis.model.m2b {train,predict,report}``."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="m2b", description="L1-Modell v003 (Delta, direkte Horizonte, Store m1.3)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    tr = sub.add_parser("train", help="Komplett-Training + Bewertung + Artefakt + REPORT.md")
    tr.add_argument("--features-dir", type=Path, default=None)
    tr.add_argument("--out", type=Path, default=None, help="Artefakt-Ordner (Default models/cmi-brain/v003)")
    tr.add_argument("--report", type=Path, default=None)
    tr.add_argument("--threads", type=int, default=config.LGBM_THREADS_DEFAULT)
    tr.add_argument("--no-keras", action="store_true")
    tr.add_argument("--no-refit", action="store_true")
    tr.add_argument("--no-monotone", action="store_true")
    tr.add_argument("--targets", default=None, help="Kommaliste (Default: alle)")
    tr.add_argument("--horizons", default=None, help="Kommaliste (Default: 1,4,6,12)")
    tr.add_argument("--num-boost", type=int, default=config.LGBM_NUM_BOOST)
    tr.add_argument("--keras-epochs", type=int, default=config.KERAS_EPOCHS)
    tr.add_argument("--resp-cases", type=int, default=config.RESP_N_CASES)
    tr.add_argument("--no-version-check", action="store_true")
    tr.add_argument("--train-start", default=config.TRAIN_START,
                    help=f"Trainingsbeginn (Default {config.TRAIN_START}; 'none' = alle Daten)")

    pr = sub.add_parser("predict", help="Vorhersage ab einer Stunde aus dem Feature-Store")
    pr.add_argument("--ts", required=True, help="'YYYY-MM-DD HH:00'")
    pr.add_argument("--model", type=Path, default=None,
                    help="Artefakt-Ordner (Default models/cmi-brain/v003; Rollback v002 mit "
                         "--features-dir data/features_m1.2)")
    pr.add_argument("--backend", choices=("lgbm", "keras"), default=None)
    pr.add_argument("--features-dir", type=Path, default=None)
    pr.add_argument("--no-version-check", action="store_true",
                    help="Versionssperre Store ↔ Artefakt (data_version) aus")
    for z in config.ZONES:
        pr.add_argument(f"--offset-{z}", type=float, default=None, help=f"Szenario: Offset {z} um ΔK")
    pr.add_argument("--json", action="store_true")

    rp = sub.add_parser("report", help="REPORT.md aus summary.json neu schreiben")
    rp.add_argument("--model", type=Path, default=None)
    rp.add_argument("--out", type=Path, default=None)

    a = ap.parse_args(argv)
    if a.cmd == "train":
        from .pipeline import run_train

        kw = {}
        if a.targets:
            kw["targets"] = tuple(x.strip() for x in a.targets.split(","))
        if a.horizons:
            kw["horizons"] = tuple(int(x) for x in a.horizons.split(","))
        run_train(
            features_dir=a.features_dir, out_dir=a.out, report_path=a.report, threads=a.threads,
            with_keras=not a.no_keras, with_refit=not a.no_refit, with_monotone=not a.no_monotone,
            num_boost=a.num_boost, keras_epochs=a.keras_epochs, resp_cases=a.resp_cases,
            check_version=not a.no_version_check,
            train_start=None if str(a.train_start).lower() == "none" else a.train_start, **kw,
        )
        return 0
    if a.cmd == "predict":
        from .predict import L1Predictor, format_table

        p = L1Predictor.load(a.model, backend=a.backend)
        od = {z: getattr(a, f"offset_{z}") for z in config.ZONES if getattr(a, f"offset_{z}") is not None}
        res = p.predict_at(a.ts, features_dir=a.features_dir, offset_delta=od or None,
                           check_version=not a.no_version_check)
        if a.json:
            print(json.dumps({str(k): v for k, v in res.items()}, indent=1))
        else:
            print(f"Modell {p.meta.get('artifact')} · Daten {p.data_version} · Backend {p.backend} · ab {a.ts}"
                  + (f" · Szenario {od}" if od else ""))
            print(format_table(res))
        return 0
    if a.cmd == "report":
        from .report import write_report

        d = a.model or config.ARTIFACT_DIR
        s = json.loads((Path(d) / "summary.json").read_text())
        print(write_report(s, a.out or config.REPORT_PATH))
        return 0
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
