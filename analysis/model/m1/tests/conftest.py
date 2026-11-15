"""Pytest-Setup: stellt den Repo-Root auf ``sys.path`` (Tests importieren
``analysis.model.m1``). Keine echten Netzwerk-Calls in dieser Suite.
"""
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
