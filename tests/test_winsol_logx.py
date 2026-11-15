"""WinSol-LogX-Tests — Bruecke zu analysis/test_decode.py (S2-2).

Die eigentlichen Tests liegen neben dem Dekodierer (analysis/test_decode.py),
denn sie noetigen die Discovery-Fixtures aus discovery/winsol/. Diese Datei
macht sie fuer das Repo-Pytest (testpaths = tests/) sichtbar.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def _load() -> None:
    p = _REPO / "analysis" / "test_decode.py"
    spec = importlib.util.spec_from_file_location("cmi_winsol_test_decode", p)
    assert spec and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    globals().update({n: f for n, f in vars(mod).items() if n.startswith("test_")})


_load()
