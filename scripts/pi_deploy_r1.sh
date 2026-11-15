#!/usr/bin/env bash
# ============================================================================
# CMI Pi-Deploy R1 (S5-Raspi) — einmaliges Basis-Setup auf dem Pi
# (<user>@<pi-host>, Key-Auth via ~/.ssh/cmi_pi, BatchMode). Ausführung
# auf Pop!_OS (dieser Host), IDEMPOTENT (mehrere Läufe ok; R2/R3 bauen
# darauf auf).
#
# Was am Ende stehen muss:
#   /var/lib/docker/cmi/{bin,repo}   (Slim-Host-Tree; /var/lib/docker = sda1/USB)
#   /var/lib/docker/cmi/bin/docker-compose  (v2.x linux-aarch64, 755, SHA-pinned)
#   /var/lib/docker/cmi/.env         (1:1-Inhaltsspiegel des lokalen .env, 600)
#   /var/lib/docker/cmi/repo         (git-Clone, Gitea, credential.helper=store)
#   /var/lib/docker/cmi/compose-raspi.yml  (aus deploy/compose/)
#   Docker Named-Volumes cmi_data + cmi_models (existieren)
#
# Security:
#   * .env-Werte werden NIE ausgegeben: Cat-Pipe `cat .env | ssh 'cat > ...'`,
#     remote umask 077 + chmod 600.
#   * Git-Passwort NIE ausgeben: es wird aus der lokalen ~/.git-credentials
#     (nur die Gitea-URL-Zeile, URL-Encoded) remote nach ~/.git-credentials
#     gespiegelt (umask 077 + 600).
#   * Binary NIE ins Repo, NIE in ein Host-PATH-Verzeichnis; Übertrag als
#     Roh-Byte-Stream `cat | ssh 'cat >'` (kein Base64).
#
# Keine Container-Operationen (kein Build, kein Start = R2/R3), kein CMI-Zugriff.
# Exit: 0 = Pi-Zustand valid, 1 = Deploy-Schritt fehlgeschlagen,
#       2 = fehlende lokale Präreqs.
#
# Gate: `bash -n scripts/pi_deploy_r1.sh`
# ============================================================================
set -u

NO_CLONE=0
for a in "$@"; do
    case "$a" in
        --no-clone) NO_CLONE=1 ;;
        -h|--help)
            echo "Usage: pi_deploy_r1.sh [binary-path] [--no-clone]"
            echo "  --no-clone: Schritt 5 (Repo-Clone) auslassen (nur wenn der"
            echo "    Commit NOCH nicht gepusht ist; danach erneut OHNE Flag laufen)"
            exit 0
            ;;
    esac
done

LOCAL_REPO="<repo-root>"
PIN_FILE="$LOCAL_REPO/deploy/compose/docker-compose-sha256"
COMPOSE_YML="$LOCAL_REPO/deploy/compose/compose-raspi.yml"
BINARY="${1:-/tmp/opencode/compose-pin/docker-compose-linux-aarch64}"
PI_HOST="<user>@<pi-host>"
SSH_KEY="$HOME/.ssh/cmi_pi"
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=10"
LOCK="/tmp/cmi-pi-deploy-r1.lock"
PI_TREE="/var/lib/docker/cmi"
LOG="$LOCAL_REPO/data/pi_deploy_r1.log"
# Gitea-Owner des ta-cmi-gateway-Repos (Default = "de" für maintainer).
# Vorrang: Umgebungs-Var CMI_GIT_USER > "CMI_GIT_USER=" in .env > Default.
if [ -z "${CMI_GIT_USER:-}" ] && [ -f "$LOCAL_REPO/.env" ]; then
    CMI_GIT_USER=$(grep -E '^[[:space:]]*CMI_GIT_USER=' "$LOCAL_REPO/.env" \
        | tail -n 1 | cut -d'=' -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]')
fi
GITEA_USER="${CMI_GIT_USER:-you}"

ts() { date '+%F %T'; }
log() { printf '[%s] %s\n' "$(ts)" "$1" | tee -a "$LOG"; }
pi_ssh() { ssh -i "$SSH_KEY" $SSH_OPTS "$PI_HOST" "$@"; }

# --- Präreqs (lokal) -----------------------------------------------------------
command -v sha256sum >/dev/null 2>&1 || { log "FEHLER: sha256sum fehlt (lokal)"; exit 2; }
[ -d "$LOCAL_REPO" ] || { log "FEHLER: Repo fehlt: $LOCAL_REPO"; exit 2; }
[ -f "$SSH_KEY" ] || { log "FEHLER: SSH-Key fehlt: $SSH_KEY"; exit 2; }
[ -f "$LOCAL_REPO/.env" ] || { log "FEHLER: lokales .env fehlt"; exit 2; }
[ -f "$PIN_FILE" ] || { log "FEHLER: Pin-Datei fehlt: $PIN_FILE"; exit 2; }
[ -f "$COMPOSE_YML" ] || { log "FEHLER: Compose-Datei fehlt: $COMPOSE_YML"; exit 2; }
[ -f "$BINARY" ] || {
    log "FEHLER: lokales Compose-Binary fehlt: $BINARY"
    log "  (Download-URL s. deploy/compose/docker-compose-release.md; Datei-Name muss 'docker-compose-linux-aarch64' sein)"
    exit 2
}
[ -r "$HOME/.git-credentials" ] || { log "FEHLER: ~/.git-credentials fehlt (lokal)"; exit 2; }
grep -q "<git-host>" "$HOME/.git-credentials" \
    || { log "FEHLER: keine Gitea-Zeile in ~/.git-credentials (lokal)"; exit 2; }
mkdir -p "$(dirname "$LOG")"

# --- flock ---------------------------------------------------------------------
exec 9>"$LOCK"
if ! flock -n 9; then
    log "FEHLER: anderes R1-Deploy läuft (Lock $LOCK)"; exit 1
fi

# --- SHA256-Pin-Prüfung des Binaries (im Binary-Verzeichnis: Dateiname muss    ---
# --- 'docker-compose-linux-aarch64' sein, wie in der Pin-Datei) ----------------
BIN_DIR=$(dirname "$BINARY"); BIN_NAME=$(basename "$BINARY")
if [ "$BIN_NAME" != "docker-compose-linux-aarch64" ]; then
    log "WARN: Binary-Dateiname $BIN_NAME != Pin-Name; Kopie in temporäres Verzeichnis"
    mkdir -p /tmp/opencode/compose-pin-check
    cp "$BINARY" /tmp/opencode/compose-pin-check/docker-compose-linux-aarch64
    BIN_DIR=/tmp/opencode/compose-pin-check
fi
if ! ( cd "$BIN_DIR" && sha256sum -c "$PIN_FILE" ); then
    log "FEHLER: SHA256-Prüfsumme weicht von der Pin-Datei ab (Abort)"; exit 1
fi
log "SHA256-OK: $(awk '{print $1}' "$PIN_FILE" | cut -c1-16)… (Pin: docker-compose-sha256)"

# --- Pi erreichbar (Key-Auth) ---------------------------------------------------
pi_ssh true || { log "FEHLER: Pi unerreichbar / Key-Auth schlägt fehl"; exit 1; }
log "Pi erreichbar (Key-Auth: $PI_HOST)"

# --- 1) Pi-Tree + Volumes (idempotent) ------------------------------------------
if ! pi_ssh "umask 077; mkdir -p $PI_TREE/bin $PI_TREE/repo; \
             docker volume create cmi_data; docker volume create cmi_models" 2>>"$LOG"; then
    log "FEHLER: Pi-Tree/Volumes-Anlage fehlgeschlagen"; exit 1
fi
log "OK: $PI_TREE/{bin,repo} + Named-Volumes cmi_data/cmi_models"

# --- 2) Compose-Binary (Roh-Byte-Stream; NICHT Base64, NICHT ins Repo) ---------
pi_ssh "umask 077; cat > $PI_TREE/bin/docker-compose; chmod 755 $PI_TREE/bin/docker-compose" < "$BIN_DIR/docker-compose-linux-aarch64" \
    || { log "FEHLER: Binary-Übertrag auf den Pi fehlgeschlagen"; exit 1; }
# Remote-Prüfsumme gegen die Pin (Binary bleibt NUR im Pi-Tree)
PI_SHA=$(pi_ssh "sha256sum $PI_TREE/bin/docker-compose" | awk '{print $1}')
PIN_SHA=$(awk '{print $1}' "$PIN_FILE")
if [ "$PI_SHA" != "$PIN_SHA" ]; then
    log "FEHLER: Remote-SHA256 weicht von der Pin ab (Abort)"; exit 1
fi
log "OK: Binary am Pi ($PI_TREE/bin/docker-compose), Remote-SHA256 = Pin"

# --- 3) Pi-.env (1:1-Inhaltsspiegel; Werte NIE drucken) -------------------------
if ! cat "$LOCAL_REPO/.env" | pi_ssh "umask 077; cat > $PI_TREE/.env; chmod 600 $PI_TREE/.env"; then
    log "FEHLER: .env-Übertrag auf den Pi fehlgeschlagen"; exit 1
fi
ENV_BYTES=$(pi_ssh "stat -c%s $PI_TREE/.env")
LOCAL_BYTES=$(stat -c%s "$LOCAL_REPO/.env")
if [ "$ENV_BYTES" != "$LOCAL_BYTES" ]; then
    log "FEHLER: .env-Größe weicht ab (lokal $LOCAL_BYTES vs. Pi $ENV_BYTES; Abort)"; exit 1
fi
log "OK: Pi-.env gespiegelt (Größe $ENV_BYTES Bytes, 0600; Inhalt redigiert, 1:1)"

# --- 4) Git-Passwort: nur die Gitea-Zeile aus ~/.git-credentials (lokal) nach   ---
# --- Pi ~/.git-credentials (credential.helper=store). Passwort NIE drucken. ----
if ! pi_ssh "umask 077; touch ~/.git-credentials; chmod 600 ~/.git-credentials; \
             git config --global credential.helper store"; then
    log "FEHLER: Git-Credential-Setup am Pi fehlgeschlagen"; exit 1
fi
# Idempotent: bestehende Gitea-Zeilen am Pi entfernen, dann NUR die lokale
# Gitea-Zeile(n) anhaengen -> exakt 1:1 (keine Duplikate, kein Verlust).
pi_ssh "umask 077; f=~/.git-credentials; touch \$f; grep -v '<git-host>' \$f > \$f.tmp || true; cat \$f.tmp > \$f; rm -f \$f.tmp; chmod 600 \$f" \
    || { log "FEHLER: Git-credentials (Gitea-Zeile) nicht auf den Pi übertragen"; exit 1; }
grep "<git-host>" "$HOME/.git-credentials" \
    | pi_ssh "cat >> ~/.git-credentials; chmod 600 ~/.git-credentials" \
    || { log "FEHLER: Git-credentials (Gitea-Zeile) nicht auf den Pi übertragen"; exit 1; }
# Gitea-Zeile muss am Pi exakt lokal vorhanden sein (keine Duplikate)
PI_GITEA_LINES=$(pi_ssh "grep -c '<git-host>' ~/.git-credentials")
LOCAL_GITEA_LINES=$(grep -c "<git-host>" "$HOME/.git-credentials")
if [ "$PI_GITEA_LINES" != "$LOCAL_GITEA_LINES" ]; then
    log "WARN: Gitea-Zeilen am Pi ($PI_GITEA_LINES) != lokal ($LOCAL_GITEA_LINES) (Duplikate? — Clone-Funktion unaffected)"
fi
log "OK: Pi-Git-Credentials eingerichtet (credential.helper=store, Zeilen redigiert)"

# --- 5) Repo-Clone (idempotent: exists → fetch + reset --hard origin/master) ----
REMOTE_BRANCH="master"
if [ "$NO_CLONE" -eq 1 ]; then
    log "SKIP: Repo-Clone (--no-clone; Commit noch nicht gepusht — nach dem Push erneut OHHNE Flag ausführen)"
    PI_HEAD="(nicht geklont)"
else
    CLONE_OUT=$(pi_ssh "set -e; \
        if [ ! -d $PI_TREE/repo/.git ]; then \
            git clone https://<git-host>/${GITEA_USER}/ta-cmi-gateway.git $PI_TREE/repo; \
        else \
            git -C $PI_TREE/repo fetch origin $REMOTE_BRANCH; \
            git -C $PI_TREE/repo reset --hard origin/$REMOTE_BRANCH; \
        fi; \
        git -C $PI_TREE/repo rev-parse HEAD" 2>&1) || {
        log "FEHLER: Repo-Clone/Update am Pi fehlgeschlagen:"
        # Nur nicht-sekrete Zeilen ausgeben (URL-Zeile in Gitea-Fehlermeldungen maskieren)
        printf '%s\n' "$CLONE_OUT" | sed -E 's#https://[^@/]+@#https://***@#g' | while IFS= read -r l; do log "  $l"; done
        exit 1
    }
    PI_HEAD=$(printf '%s\n' "$CLONE_OUT" | tail -n 1)
    LOCAL_HEAD=$(git -C "$LOCAL_REPO" rev-parse HEAD)
    log "OK: Repo am Pi (HEAD=$PI_HEAD)"
    if [ "$PI_HEAD" = "$LOCAL_HEAD" ]; then
        log "   HEAD == lokaler HEAD (Push-Status: in Sync)"
    else
        log "   HINWEIS: Pi-HEAD != lokaler HEAD ($LOCAL_HEAD) — Push ausstehend?"
    fi
fi

# --- 6) compose-raspi.yml spiegeln ----------------------------------------------
cat "$COMPOSE_YML" | pi_ssh "cat > $PI_TREE/compose-raspi.yml" \
    || { log "FEHLER: compose-raspi.yml nicht auf den Pi übertragen"; exit 1; }
log "OK: $PI_TREE/compose-raspi.yml gespiegelt"

# --- 7) Validierung am Pi --------------------------------------------------------
# 7a) Binary läuft + Compose-Config parst (config -q = nur Validierung, kein
#     Output → keine .env-Werte im Log)
if ! pi_ssh "$PI_TREE/bin/docker-compose version | head -1; \
            $PI_TREE/bin/docker-compose -f $PI_TREE/compose-raspi.yml config -q; \
            echo COMPOSE-CONFIG-OK" 2>>"$LOG"; then
    log "FEHLER: docker-compose config -q parst die Compose-Datei NICHT"; exit 1
fi
log "OK: docker-compose config -q (Compose-Datei parst sauber; Version-Head + COMPOSE-CONFIG-OK im Log)"
# 7b) sda1-Nachweis (df; findmnt/lsblk sind auf dem Pi nicht installiert)
DF_OUT=$(pi_ssh "df /var/lib/docker/volumes | tail -1")
echo "$DF_OUT" | grep -q sda1 \
    || { log "FEHLER: /var/lib/docker/volumes liegt NICHT auf sda1 (df: $DF_OUT)"; exit 1; }
log "OK: /var/lib/docker/volumes auf sda1 (df: $(awk '{print $1}' <<<"$DF_OUT"))"
# 7c) Volume-Pfade
pi_ssh "docker volume inspect cmi_data cmi_models" >>"$LOG" 2>&1 \
    || { log "FEHLER: docker volume inspect cmi_data/cmi_models fehlgeschlagen"; exit 1; }
log "OK: Volume-Inspection (Pfade im Log)"

log "R1-Deploy FERTIG: Pi-Zustand valid (Tree, Binary, .env, Clone, Compose, Volumes, sda1)"
exit 0
