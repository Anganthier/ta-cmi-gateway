# TA CMI Gateway

A read-only FastAPI gateway in front of the TA **CMI** heating controller,
with a loop-centric Web-UI, a persistent history store, an L1/L2 "advisor"
model layer, and a token-gated two-phase write route.

The gateway polls the CMI (schematic pages, JSON-API, and the CMI-internal
`/LOG` logger), normalizes everything into a single live-state document,
serves it over a small REST API, and renders it in a vanilla-JS web
frontend with animated P&ID loop views.

> ⚠️ **Experimental software.** This project was developed for one specific
> TA CMI installation. Use it at your own risk; operation of the write
> route is entirely under your responsibility.

## Features

- **Read-only gateway** — async `httpx` client with a strict read-only
  allow-list, a serial in-flight queue, and a rate limiter that respects
  the CMI's `api.cgi` limits. No write path is reachable from the
  pollers.
- **Loop-centric Web-UI** — vanilla JS (ES modules, no build step):
  dashboard, five animated P&ID loop views (wood / oil / solar /
  domestic hot water / heating) with DIN-EN-ISO-10628 symbolics,
  live readouts, flow/pump animation, and a 24 h chart per loop.
  Includes an offline demo mode when the CMI is unreachable.
- **History store** — WinSol LogX history and the CMI-internal 10 s
  logger are kept as Hive-partitioned Parquet (`winsol_pq`, `cmi_log_pq`);
  `GET /api/v1/history` queries both plus a SQLite long store through
  DuckDB with filter push-down.
- **L1/L2 advisor** — `analysis/model/` holds an offline feature store
  (M1), an hourly prediction model (L1, LightGBM), and a recommendation
  engine (L2) that turns model output + live state into guarded,
  explainable suggestions. Served via `GET /api/v1/recommend`
  (cached, stale-while-revalidate).
- **Gated two-phase write** — `GET /api/v1/change` (preview +
  confirmation hash, no side effect) and `POST /api/v1/change`
  (write + read-back + JSONL audit) are separated, whitelist-restricted,
  and require a dedicated write token; the route is disabled (503) while
  the write token is empty.

## Stack

Python 3.12 · FastAPI · httpx (async) · Pydantic v2 · DuckDB + PyArrow ·
LightGBM · vanilla-JS UI (Chart.js vendored, no CDN).

## Architecture (schematic)

```
 [browser / external clients]
        |  Authorization: Bearer <token>
        v
  /api/v1/{state,params,history,recommend,health}   /  (static web UI)
        |
  StateStore (normalized live state) <--- Pollers (schematic pages,
        |                                  JSON-API, CMI /LOG logger)
        |        CMIGuard: serial queue + api.cgi rate limiter
        |        CmiLogClient: own read-only poller, Parquet sink
        v
  TA CMI on your LAN (read-only: /schematic_files/*.cgi,
                       /INCLUDE/api.cgi, /INCLUDE/changer*.cgi, /LOG/*)

  separate, isolated write route (token-gated, not part of polling):
  GET  /api/v1/change?adr=&to=   -> preview + confirmation hash
  POST /api/v1/change            -> CMI write + read-back + audit
```

- Schematic poller: pages `/schematic_files/{1..5}.cgi` every
  `CMI_POLL_SEC` (default 30 s).
- JSON-API poller: paced by the rate limiter (default minimum 65 s
  between `api.cgi` calls).
- CMI-`/LOG` poller: fetches new logger files every
  `CMI_LOG_POLL_SEC` (default 300 s) into the Parquet store; strictly
  read-only by default (`CMI_LOG_CLEAR=false`).
- All pollers survive failures: they keep the last values and flip the
  `stale` health flag; `/api/v1/health` returns 503 while the CMI is
  unreachable.

## Quickstart

Prerequisites: Docker + Docker Compose (or Python 3.12 for development),
and a CMI reachable from the host's LAN.

```bash
git clone <your-fork-of-ta-cmi-gateway> ta-cmi-gateway
cd ta-cmi-gateway
cp .env.example .env
# edit .env: set CMI_BASE_URL to the CMI's LAN address, CMI_USER/CMI_PASS
# to the CMI expert-mode credentials, and generate both gateway tokens
# (e.g. python3 -c "import secrets; print(secrets.token_urlsafe(32))")
docker compose up -d --build
```

The UI is served at `http://<host>:30001/`. Enter the read token when
prompted. Smoke test:

```bash
curl -s localhost:30001/api/v1/health | python3 -m json.tool
curl -s -H "Authorization: Bearer $GATEWAY_READ_TOKEN" \
     localhost:30001/api/v1/state | python3 -c \
     "import json,sys; d=json.load(sys.stdin); print(len(d['fields']), 'fields; stale:', d['health']['stale'])"
```

> **Note:** the L2 advisor (`/api/v1/recommend`) needs a feature store
> (`data/features`) and a trained L1 model (`models/cmi-brain/...`) to
> be present on the host (or in the mounted volumes). Without them the
> route degrades gracefully. See `docs/ai-model-design.md` and
> `analysis/model/` for the model pipeline.

## API

| Route                          | Auth                     | Description                                              |
|--------------------------------|--------------------------|----------------------------------------------------------|
| `GET /`                        | –                        | Web-UI (static, `index.html` at the root)                |
| `GET /docs`                    | –                        | OpenAPI documentation                                    |
| `GET /api/v1/health`           | –                        | Gateway + CMI reachability (200 / 503)                   |
| `GET /api/v1/state`            | Bearer (read)            | Normalized live state (schematics + JSON-API)            |
| `GET /api/v1/params`           | Bearer (read)            | Settable parameters with options + labels (cached)       |
| `GET /api/v1/history`          | Bearer (read)            | Time-series query (Parquet stores + long store, DuckDB)  |
| `GET /api/v1/recommend`        | Bearer (read)            | L2 recommendations (cached, stale-while-revalidate)      |
| `GET /api/v1/change?adr=&to=`  | Bearer (read)            | Change preview + confirmation hash (no side effect)      |
| `POST /api/v1/change`          | Bearer (write)           | Two-phase execution: write + read-back + JSONL audit     |

The write route additionally requires the write token and is **disabled
(503) while `GATEWAY_WRITE_TOKEN` is empty**; the confirmation hash
alone never grants write access.

## Configuration

All settings are environment-driven; see `.env.example` for the full,
annotated list. Highlights:

| Variable                   | Default                  | Purpose                                |
|----------------------------|--------------------------|----------------------------------------|
| `CMI_BASE_URL`             | `http://192.168.0.100`   | CMI web server (example value, adjust) |
| `CMI_USER` / `CMI_PASS`    | –                        | CMI expert-mode Basic-Auth             |
| `CMI_POLL_SEC`             | `30`                     | Schematic poll interval                |
| `CMI_API_INTERVAL_SEC`     | `65`                     | Minimum spacing of `api.cgi` calls     |
| `CMI_LOG_POLL_SEC`         | `300`                    | CMI `/LOG` poll interval               |
| `GATEWAY_READ_TOKEN`       | –                        | Bearer token for read routes           |
| `GATEWAY_WRITE_TOKEN`      | – (empty = write off)    | Bearer token for the write route       |
| `CMI_GIT_USER`             | `you`                    | Git-host owner of this repo (Pi scripts) |

## Repository layout

```
app/                # FastAPI gateway: state store, pollers, history, L2 route,
                    #   change route, CMI clients (schematic/JSON-API/LOG/changer/write)
analysis/model/     # M1 feature store, L1 (m2b) prediction, L2 recommendation engine
docs/               # plant model, hydraulic topology, UI architecture, model docs
deploy/             # compose files (host + Raspberry Pi) + merge-loop script
scripts/            # Pi deploy/sync helpers, write-path discovery inventory
tests/              # pytest suite (no network; recorded fixtures)
ui/                 # vanilla-JS web UI (ES modules, vendored Chart.js)
assets/             # schematic layout pages (readout coordinates)
```

Runtime data (`data/`: history DB, Parquet stores, feature store, audit
log; `models/`: L1 model artifacts) is git-ignored and lives on the host
or in Docker volumes.

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/ruff check .            # lint
.venv/bin/pytest                  # full suite (incl. UI static checks)
.venv/bin/pytest --cov=app        # coverage; CI gate: >= 80 % on app/
```

Tests run hermetically: the CMI is mocked via transport fixtures, no
network access and no CMI write is ever performed. UI modules are
syntax-checked with `node --check`.

## License

[AGPL-3.0](./LICENSE) — see the `LICENSE` file for the full text.
Copyright (C) 2026 Anganthier.

## Disclaimer

This software is provided **as is**, without warranty of any kind. It
interacts with real building-automation hardware; misconfiguration of
the (gated) write route can affect your heating system. You are solely
responsible for any changes made through it.
