# CMI Gateway image (python:3.12-slim + uv)
FROM python:3.12-slim AS base

# Non-root user
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --shell /sbin/nologin app

# libgomp1: lightgbm laedt die native lib_lightgbm.so, die OpenMP (libgomp)
# braucht - python:3.12-slim haengt sie nicht mit (S4N-UI-KI, L2 im Image).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/cmi

# Install uv (fast, reproducible) and the app (no dev extras)
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir uv \
 && uv pip install --system --no-cache . \
 && pip uninstall --yes uv

# Schematic layouts must be present for coordinate parsing
COPY assets ./assets

# L2-Engine im Image (S4N-UI-KI): analysis/model/{l2,m2b,m1,m2} + M1-Bias-Profil.
# Die Engine wird IN-IMAGE von /api/v1/recommend gerufen; WORKDIR /srv/cmi ist
# dabei auf sys.path (uvicorn-Import-Root), deshalb importieren
# `analysis.model.*` direkt. analysis/model/l2/runs/ (Wetter-Raw-Cache) liegt
# im Image-Layer: nur Read-Write-Ziel für den LIVE-Forecast-Cache (rohe
# API-Antwort, <100 kB/Tag, git-ignoriert hostseitig).
COPY analysis ./analysis

# Web-UI (S4-1/S4-3 + S4N-UI-KI): static ui/ (index.html, css, js, vendored Chart.js)
COPY ui ./ui

# File caches (changer cache)
RUN mkdir -p /data && chown app:app /data
VOLUME ["/data"]

# /srv/cmi/data muss im Bild existieren (app:app):
# - history.db liegt dort (history_db_path-Default = REPO_ROOT/data),
# - data/winsol_pq wird read-only bind-gehangen. Fehlt das Verzeichnis im
#   Bild, prae-creiert Docker die Mount-Punkt-Eltern als root:root und der
#   app-User koennte keine history.db mehr anlegen (startup-Crash).
RUN mkdir -p /srv/cmi/data && chown app:app /srv/cmi/data
ENV CMI_DATA_DIR=/data

# L1-Artefakt-Root (S4N-UI-KI): ./models wird read-only bind-gehangen auf
# /srv/cmi/models (default l1_model_dir = REPO_ROOT/models/cmi-brain/v003
# = /srv/cmi/models/cmi-brain/v003). /srv/cmi/models muss im Bild existieren,
# damit der ro-Mount nicht root:root-prae-creiert wird (wie /srv/cmi/data).
# L1-Store: /data/features (ro-Mount innerhalb des rw /data-Binds, tieferer
# Mount gewinnt) = Default l1_store_dir.
RUN mkdir -p /srv/cmi/models && chown app:app /srv/cmi/models

RUN chown -R app:app /srv/cmi
USER app

EXPOSE 30001

# Single worker: the CMI serial queue + rate limiter are process-wide.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "30001", "--workers", "1"]
