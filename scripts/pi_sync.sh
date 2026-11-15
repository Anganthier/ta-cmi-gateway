#!/usr/bin/env bash
# ============================================================================
# CMI Pi-Sync (S5-Raspi) — Bidirektionaler Daten-Spiegel Pi (<pi-host>) <->
# Pop!_OS (dieser Host). Läuft NUR auf Pop!_OS (ab R5 im Cron; R1: nur
# Erstellung + Dry-Run-Test). KEINE Container-, Build- oder CMI-Operationen.
#
# Datenfluss (Pi-Volume-Root = /var/lib/docker/volumes/cmi_data/_data):
#   Pi -> lokal (vom Gateway-Poller/Changer am Pi geschrieben):
#     cmi_log_pq/ : rsync -avz --delete  (frozen Monatsdateien + laufender
#                   Monat; --delete = lokale Kopie spiegelt exakt, Pi ist die
#                   Quelle, lokale Kopie = Backup)
#     audit/      : rsync -avz (OHNE --delete: ein abbruchiger/fehlerhafter
#                   Pi-Zustand darf lokal nichts löschen)
#   lokal -> Pi (NUR Wetter-Roh-Cache, offline M1-Quellen für Fallback-Runs):
#     features/wx_cache/ : rsync -avz (OHNE --delete: Pi-Cache darf lokal
#                   nie etwas löschen)
#
#   data/features (Store) wird NICHT gespiegelt: der M1-Build (Pi-Merge-Loop)
#   ist der einzige Writer und baut aus cmi_log_pq (oben Pi->lokal).
#   data/winsol_pq wird NICHT gespiegelt: WinSol endet absichtlich (10-27);
#   Bulk-Import = R3.
#
# Fehlende Verzeichnisse:
#   * Pi-Seite fehlt (z.B. frisches cmi_data vor R3: _data leer): Probe
#     (ssh test -d) schlägt fehl -> Transfer wird übersprungen + geloggt
#     (kein Fehler; R3 besiedelt das Volume).
#   * Lokale Ziele: mkdir -p (data/ ist git-ignored).
#
# flock -n /tmp/cmi-pi-sync.lock: kein Überlappung (Cron + manuell).
# Logs: data/pi_sync.log (git-ignored data/). Exit-Codes:
#   0 = OK (inkl. übersprungener fehlender Pi-Verzeichnisse)
#   1 = rsync-Transportfehler (SSH/Netz)
#   2 = fehlende lokale Präreqs
#
# Gate: `bash -n scripts/pi_sync.sh`; Dry-Run: `scripts/pi_sync.sh --dry-run`
# ============================================================================
set -u

DRY=0
if [ "${1:-}" = "--dry-run" ]; then DRY=1; fi

# BUGFIX (2026-11-09, Q-125-Folge): ${DRY:+--dry-run} expandierte IMMER
# (DRY=0 ist "gesetzt und nicht leer") -> Remote-rsync lief als No-Op,
# waehrend der lokale --delete-Mirror LOKALE DATA WIPT (08:10/08:15/09:15).
# Korrektes Conditional-Flag:
if [ "$DRY" -eq 1 ]; then DRYOPT="--dry-run"; else DRYOPT=""; fi

PI_HOST="<user>@<pi-host>"
SSH_KEY="$HOME/.ssh/cmi_pi"
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=10"
PI_VROOT="/var/lib/docker/volumes/cmi_data/_data"
LOCAL_REPO="<repo-root>"
LOCAL_TMP="/tmp/cmi-pi-sync"
LOCK="/tmp/cmi-pi-sync.lock"
LOG="$LOCAL_REPO/data/pi_sync.log"

ts() { date '+%F %T'; }
log() { printf '[%s] %s\n' "$(ts)" "$1" | tee -a "$LOG"; }
pi_ssh() { ssh -i "$SSH_KEY" $SSH_OPTS "$PI_HOST" "$@"; }

# --- Präreqs ------------------------------------------------------------------
command -v rsync >/dev/null 2>&1 || { log "FEHLER: rsync nicht gefunden"; exit 2; }
command -v flock >/dev/null 2>&1 || { log "FEHLER: flock nicht gefunden"; exit 2; }
[ -f "$SSH_KEY" ] || { log "FEHLER: SSH-Key fehlt: $SSH_KEY"; exit 2; }
[ -d "$LOCAL_REPO/data" ] || { log "FEHLER: lokales data/ fehlt: $LOCAL_REPO/data"; exit 2; }
mkdir -p "$(dirname "$LOG")"

# --- flock: kein Überlappung (Cron + manuell); -n = sofort Abbruch, kein Warten
exec 9>"$LOCK"
if ! flock -n 9; then
    log "ÜBERSPRUNGEN: anderer Pi-Sync läuft (Lock $LOCK)"
    exit 0
fi

# --- Key-Auth (BatchMode: kein interaktiver Prompt, kein Passwort-Fallback) ---
pi_ssh true 2>>"$LOG" || { log "FEHLER: Pi unerreichbar / Key-Auth schlägt fehl ($PI_HOST)"; exit 1; }
log "Pi erreichbar (Key-Auth), Start Pi-Sync$( [ "$DRY" -eq 1 ] && echo " [DRY-RUN]" )"

# --- Pi->lokal Transfer-Root (statisch frisch je Run) --------------------------
rm -rf "$LOCAL_TMP"
mkdir -p "$LOCAL_TMP/src/cmi_log_pq" "$LOCAL_TMP/src/audit"

# 1) cmi_log_pq (Pi ist Quelle; --delete spiegelt exakt; leere Gegenseite ok)
if pi_ssh "test -d $PI_VROOT/cmi_log_pq"; then
    if ! rsync -avz --delete $DRYOPT \
            -e "ssh -i $SSH_KEY -o BatchMode=yes -o ConnectTimeout=10" \
            "$PI_HOST:$PI_VROOT/cmi_log_pq/" "$LOCAL_TMP/src/cmi_log_pq/" >>"$LOG" 2>&1; then
        log "FEHLER: rsync cmi_log_pq (Pi->lokal) fehlgeschlagen (Details in $LOG)"; exit 1
    fi
else
    log "ÜBERSPRUNGEN: Pi cmi_log_pq fehlt noch ($PI_VROOT/cmi_log_pq) — R3 besiedelt cmi_data"
fi

# 2) audit (Pi ist Quelle; OHNE --delete; leere Gegenseite ok)
if pi_ssh "test -d $PI_VROOT/audit"; then
    if ! rsync -avz $DRYOPT \
            -e "ssh -i $SSH_KEY -o BatchMode=yes -o ConnectTimeout=10" \
            "$PI_HOST:$PI_VROOT/audit/" "$LOCAL_TMP/src/audit/" >>"$LOG" 2>&1; then
        log "FEHLER: rsync audit (Pi->lokal) fehlgeschlagen (Details in $LOG)"; exit 1
    fi
else
    log "ÜBERSPRUNGEN: Pi audit fehlt noch ($PI_VROOT/audit)"
fi

if [ "$DRY" -eq 1 ]; then
    log "DRY-RUN beendet (keine lokalen Ziele geschrieben, kein lokal->Pi-Transfer)"
    exit 0
fi

# --- Lokale Ziele (nur bei echtem Run) -----------------------------------------
mkdir -p "$LOCAL_REPO/data/cmi_log_pq" "$LOCAL_REPO/data/audit"
# SAFETY-Guard (2026-11-09): leere Spiegel-Quelle NIEMALS mit --delete auf
# den lokalen Store anwenden (leer-Quellen-Wipe-Abbau, Bugfix-Datum oben).
if [ ! -d "$LOCAL_TMP/src/cmi_log_pq" ] || [ -z "$(ls -A "$LOCAL_TMP/src/cmi_log_pq" 2>/dev/null)" ]; then
    log "FEHLER: Spiegel-Quelle leer ($LOCAL_TMP/src/cmi_log_pq) -- lokaler Store NICHT angefasst (kein --delete mit leerer Quelle)"; exit 1
fi
# --delete spiegelt den Pi-Zustand exakt (lokale cmi_log_pq-Kopie = Backup).
if ! rsync -avz --delete "$LOCAL_TMP/src/cmi_log_pq/" "$LOCAL_REPO/data/cmi_log_pq/" >>"$LOG" 2>&1; then
    log "FEHLER: lokaler rsync cmi_log_pq fehlgeschlagen"; exit 1
fi
if ! rsync -avz "$LOCAL_TMP/src/audit/" "$LOCAL_REPO/data/audit/" >>"$LOG" 2>&1; then
    log "FEHLER: lokaler rsync audit fehlgeschlagen"; exit 1; fi

# --- Lokal->Pi (nur wx_cache; fehlende Pi-Seite tolerieren, R3 erstellt sie) ---
if pi_ssh "test -d $PI_VROOT/features"; then
    if ! rsync -avz \
            -e "ssh -i $SSH_KEY -o BatchMode=yes -o ConnectTimeout=10" \
            "$LOCAL_REPO/data/features/wx_cache/" "$PI_HOST:$PI_VROOT/features/wx_cache/" >>"$LOG" 2>&1; then
        log "FEHLER: rsync wx_cache (lokal->Pi) fehlgeschlagen (Details in $LOG)"; exit 1
    fi
else
    log "ÜBERSPRUNGEN: Pi features/ fehlt noch ($PI_VROOT/features) — R3 besiedelt cmi_data"
fi

log "Pi-Sync OK: cmi_log_pq+audit (Pi->lokal), wx_cache (lokal->Pi)"
exit 0
