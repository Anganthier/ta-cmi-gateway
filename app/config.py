"""Gateway configuration (environment-driven, no secrets in code).

All secrets (CMI credentials, gateway tokens) live in ``.env`` (git-ignored).
See ``.env.example`` for the full list of variables.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Repository root (this file lives in ``app/``).
REPO_ROOT: Path = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime settings, loaded from the environment / ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- CMI connection -----------------------------------------------------
    #: Base URL of the CMI web server (no trailing slash).
    #: Example value — set the CMI's real LAN address via CMI_BASE_URL.
    cmi_base_url: str = Field(default="http://192.168.0.100")
    #: TA CMI expert (HTTP Basic) user. Stock default on many CMIs is
    #: "admin"; override via CMI_USER.
    cmi_user: str = Field(default="admin")
    #: TA CMI expert (HTTP Basic) password. Stock default on many CMIs is
    #: "admin"; override via CMI_PASS.
    cmi_pass: str = Field(default="admin")
    #: Per-request timeout in seconds for CMI HTTP calls.
    cmi_timeout_sec: float = Field(default=15.0)

    # --- Rate limiting / polling --------------------------------------------
    #: Minimum seconds between two ``/INCLUDE/api.cgi`` calls (hard limit is 1/min).
    cmi_api_interval_sec: float = Field(default=65.0)
    #: Schematic polling interval in seconds.
    cmi_poll_sec: float = Field(default=30.0)
    #: TTL (seconds) of the changer-resolver in-memory cache.
    cmi_changer_ttl_sec: float = Field(default=86400.0)

    # --- Data / caches -------------------------------------------------------
    #: Directory for file caches (git-ignored ``data/``).
    cmi_data_dir: Path = Field(default=REPO_ROOT / "data")
    #: Directory with the schematic layout files (``{1..5}.html``).
    cmi_schematic_dir: Path = Field(default=REPO_ROOT / "assets" / "schematics")

    # --- CMI internal logging (/LOG) poller (S4N-LogPoll) ----------------------
    #: How often (seconds) to poll the CMI's own binary logging store
    #: (``/LOG/info<JJJJ>.log`` index + ``/LOG/<JJJJ>/data_*.log``).  Default
    #: 300 s (5 min): the CMI appends one record every 10 s, so a 5-min poll
    #: picks up ~30 new records with plenty of head-room and keeps the
    #: storekrit:10 buffer from filling.
    cmi_log_poll_sec: int = Field(default=300)
    #: Whether the /LOG poller may issue the state-mutating
    #: ``GET /LOG/clear.log`` to release the CMI's storekrit buffer.
    #: Default ``False`` = the poller is strictly READ-ONLY and NEVER sends
    #: clear.log: a clear deletes ALL blocks in the CMI buffer INCLUDING the
    #: still-active block file that is not yet listed in the index, so a
    #: poller-issued clear destroys not-yet-ingested data (proven 2026-10-30:
    #: the 15:16:48 clear wiped the active 00:00:05-15:16:48 block, ~5400
    #: un-ingested records).  Buffer release is a manual
    #: WinSol-Fetch-with-Delete decision (user responsibility; see
    #: ``docs/cmi-log-protocol.md``).  ``True`` = the legacy last-data-first
    #: clear gate (clear only when every index file is already ingested) --
    #: experimental, only when the CMI buffer must be force-drained through
    #: the gateway.  Environment values "true"/"1" and "false"/"0" (any
    #: case) are accepted.
    cmi_log_clear: bool = Field(default=False)

    # --- History (S2-1) ------------------------------------------------------
    #: Persistent history DB path (git-ignored). Defaults to ``data/history.db``.
    history_db_path: Path = Field(default=REPO_ROOT / "data" / "history.db")
    #: How many days of measurements to retain (older rows are pruned).
    history_retention_days: float = Field(default=400.0)
    #: How often (seconds) the retention cleanup runs (opportunistic).
    history_retention_check_sec: float = Field(default=3600.0)
    #: Comma-separated live-poll sources that retention may prune.  DECISION
    #: (human-approved): only live-polling sources are pruned; historical
    #: sources (e.g. ``winsol``) are exempt and kept permanently.
    history_prune_sources: str = Field(default="schematic,json")

    @property
    def prune_source_set(self) -> frozenset[str]:
        """``history_prune_sources`` parsed into a set (blank entries dropped)."""
        return frozenset(part.strip() for part in self.history_prune_sources.split(",") if part.strip())

    # --- WinSol Parquet store (on-demand history) -----------------------------
    #: Directory of the WinSol *canonical* Parquet store
    #: (``data/winsol_pq/YYYY/MM/YYYY-MM.parquet``, one zstd file per month).
    #: The gateway serves WinSol history ON DEMAND from here for the
    #: ``winsol:<N>`` devices instead of copying it into the long store.
    #: A missing/empty dir degrades cleanly (no winsol series; startup never fails).
    winsol_parquet_dir: Path = Field(default=REPO_ROOT / "data" / "winsol_pq")

    # --- CMI internal-logging Parquet store (S4N-LogParquet) ------------------
    #: Directory of the cmi-log *live* Parquet store
    #: (``data/cmi_log_pq/YYYY/MM/YYYY-MM.parquet``, same 71-column wide schema
    #: as ``winsol_pq``).  The /LOG poller rewrites the in-progress month every
    #: 5 min atomically; finished months are frozen.  The history route merges
    #: this store into the logical ``winsol:<N>`` series (winsol_pq wins on
    #: ts collisions).  A missing/empty dir degrades cleanly.
    cmi_log_parquet_dir: Path = Field(default=REPO_ROOT / "data" / "cmi_log_pq")

    # --- L2 recommendation engine (S4N-UI-KI, in-image) ------------------------
    #: L1 feature store (Hive ``YYYY/MM/*.parquet``, m1.3).  Host default:
    #: ``data/features``; in the container the read-only bind mount
    #: ``./data/features:/data/features:ro`` (inside the rw ``./data:/data``
    #: bind -- the deeper mount wins).  Overridable via ``L1_STORE_DIR``.
    l1_store_dir: Path = Field(default=REPO_ROOT / "data" / "features")
    #: L1 model artifact directory (cmi-brain-v003; winner backend LightGBM).
    #: Host default: ``models/cmi-brain/v003``; in the container the read-only
    #: bind mount ``./models:/srv/cmi/models:ro``.  Overridable via
    #: ``L1_MODEL_DIR``.
    l1_model_dir: Path = Field(default=REPO_ROOT / "models" / "cmi-brain" / "v003")
    #: TTL (minutes) of the in-memory ``GET /api/v1/recommend`` result cache.
    #: The cache key is the current full hour; a hit inside the TTL returns the
    #: last engine run without re-running the L2 pipeline (design:
    #: engine runs <= every 5 min, default).
    recommend_cache_min: int = Field(default=5)

    # --- Minderer-Loop Phase 2 (v0.6-A') ---------------------------------------
    # Autonomer EG-Minderer-Regelkreis im Gateway (in-Gateway-Background-
    # Loop neben dem CMI-/LOG-Poller): Ziel-ΔT ≈ 10 K (L2-Bausteine,
    # Single Source analysis/model/l2), Envelope 2–7 V, max 1×/h via
    # MindererService-Rate-Limit, Ground-Truth-Verifikation per /LOG a18
    # (UVR-/LOG-Ground-Truth, nicht nur CMI-OK), Transient-Handling
    # (12:49-Fenster: Write wird vom CMI bestätigt, vom UVR zeitweise
    # ~17 min nicht angewendet).  NUR Zone EG (Phase 2).
    #
    # **Kill-Switch + Rolle (kanonisiert, maintainer):** der Loop schreibt
    # NIEMALS in die CMI außer (a) MINDERER_LOOP_ENABLED=1 AND
    # (b) CMI_ROLE=prod (Pi = Master, s. ``cmi_role``).  Default: aus.
    # Dev-Umgebung (cmi_role=dev) → 0 CMI-Verkehr (Gate VOR allem).
    #: Kill-Switch für den autonomen Minderer-Loop (Default AUS, fail-safe).
    #: Pydantic-Bool: "1"/"true"/"yes"/"on" (case-insens.) = an,
    #: "0"/"false"/"no"/"off" = aus.  WICHTIG: steht NIE in der .env
    #: (1:1-Spiegel-Kontrakt, s. ``cmi_role``) sondern in
    #: deploy/compose/compose-raspi.yml (Pi-Aktivierung).
    minderer_loop_enabled: bool = Field(default=False)
    #: Zyklus-Intervall in Minuten (Default 15; der Write bleibt max 1×/h
    #: via dem bestehenden Rate-Limit -- der Intervall-Sleep paces nur
    #: den Lese-/Verifikationszyklus).
    minderer_loop_interval_min: int = Field(default=15)
    #: Regelkreis-Zone (NUR ``eg`` erlaubt; anderes → Loop inaktiv +
    #: Status-Flag, kein Crash).  Phase 2 = EG-Minderer (a18).
    minderer_loop_zone: str = Field(default="eg")
    #: Envelope-Minimum (V).  Envelope-Clip für ``v_target`` (Default 2.0).
    minderer_loop_v_min: float = Field(default=2.0)
    #: Envelope-Maximum (V, Drossel-Regime).  Sättigungs-Flag ab ~10 V im
    #: Kurven-Fit (v0.6-A) macht > 7 V unzulässig; Envelope-Clip (Default 7.0).
    minderer_loop_v_max: float = Field(default=7.0)
    #: v2 (21.11): Maximalschritt je Zyklus (V, Default 0.75).  Der
    #: Verhältnis-Korrektur-Wert ``v_now · (ΔT_ist/ΔT_ziel)`` wird in beide
    #: Richtungen auf ± ``minderer_loop_max_step_v`` relativ zum
    #: CMI-Istwert geklemmt, DANN erst Envelope-Clip.  Bei max. 1×/h
    #: (Rate-Limit) = sehr träge (maintainer-Vorgabe „sehr träge“).
    minderer_loop_max_step_v: float = Field(default=0.75)
    #: Mindest-ΔV (V) für einen Write (Default 0.05).  OOB-Außen-T:
    #: verdoppelt (0.10) -- außerhalb des Fit-Kalibrierungsbands ist das
    #: V-Ziel unsicherer, größerer Deadband = weniger Fehltritte.
    minderer_loop_deadband_v: float = Field(default=0.05)
    #: Raum-T-Floor EG (°C): darunter = kein Drosseln (v_target < v_now
    #: blockiert; Anheben bleibt erlaubt -- Raum zu kalt → nie weiter
    #: drosseln).  Default 20.8.
    minderer_loop_room_floor_c: float = Field(default=20.8)
    #: Verifikations-/Resend-Prüfungen bis Give-up (Default 6, ~1.5 h bei
    #: 15-min-Takt, 4 Resends).  Give-up: ``transient_giveup=True`` +
    #: Audit ``minderer_verify ok=false`` + kein weiteres Resend (bis eine
    #: NEUE Entscheidung das Pending ersetzt).
    minderer_loop_verify_giveup: int = Field(default=6)

    # --- S3-1 write route (/api/v1/change) ------------------------------------
    #: Audit JSONL path for every attempted change (success AND failure):
    #: ``{ts, adr, label, from, to, user, readback, applied, http_status}``.
    #: Host default: ``data/audit/change.jsonl``; in the container
    #: ``/data/audit/change.jsonl`` (on the rw ``./data`` bind).
    audit_jsonl_path: Path = Field(default=REPO_ROOT / "data" / "audit" / "change.jsonl")

    # --- Gateway security ----------------------------------------------------
    #: CMI role gate for real CMI writes (v0.6-A', maintainer-Forderung 18.11:
    #: Pi ist Master; die lokale Dev-Umgebung darf NIEMALS in die CMI
    #: schreiben).  ``dev`` (Default) = jeder echte CMI-Write (``write()`` in
    #: :mod:`app.cmi.write`) raise :class:`~app.cmi.exceptions.CMISafetyError`
    #: VOR jeglichem HTTP-Verkehr; der wertneutrale No-op-Write (aktueller
    #: Wert zurueckgeschrieben, Strict-Drift-Check) bleibt in dev erlaubt
    #: (maintainer-Go: einmalig lokalen Pfad-Test).  ``prod`` = echte CMI-Writes
    #: freigegeben (Master am Pi).  Ungültige Werte (z. B. ``PROD``,
    #: ``prodX``) werden FAIL-SAFE wie ``dev`` behandelt -- ein Tippfehler
    #: darf niemals Writes freigeben.  Wichtig: `prod` gehoert NICHT in die
    #: `.env` (das Pi-`.env` ist 1:1-Spiegel der lokalen, pi_deploy_r1.sh),
    #: sondern in `deploy/compose/compose-raspi.yml` (Compose-`environment`
    #: ueberschreibt `env_file`).
    cmi_role: str = Field(default="dev")
    #: Bearer token accepted for read routes. Empty = auth disabled for reads.
    gateway_read_token: str = Field(default="")
    #: Bearer token accepted for the S3-1 write route (``POST /api/v1/change``).
    #: Empty (default) = the write route is DISABLED (503), never open:
    #: ``gateway_write_token`` must be configured explicitly to enable writes.
    #: The hash of the GET-preview is HMAC-keyed with THIS token (server-side),
    #: so a missing/invalid Bearer write token -> 403 even with a valid hash.
    #: The UI prompts for this token only on "Anwenden" (separate localStorage
    #: key); the confirmation hash alone NEVER grants write access.
    gateway_write_token: str = Field(default="")

    @property
    def base_url(self) -> str:
        """CMI base URL without trailing slash."""
        return self.cmi_base_url.rstrip("/")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached)."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache (used by tests that monkeypatch env vars)."""
    get_settings.cache_clear()
