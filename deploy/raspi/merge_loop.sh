#!/usr/bin/env bash
# ============================================================================
# M1-Feature-Build-Loop (S5-Raspi) — läuft im cmi-m1-merge-Container (CMD),
# User `app`, WORKDIR /srv/cmi (Image cmi-gateway:latest).
#
# Semantik = Pop!_OS-Cron (dort: `15 * * * *` + `0 3 * * 0`, crontab; am Pi
# ersetzt dieser Loop die crontab — der Merge-Container ist der einzige
# M1-Writer, der Feature-Store liegt im cmi_data-Volume):
#
#   * stündlich um HH:15 (TZ via env, Default Europe/Berlin) inkrementell:
#       python -m analysis.model.m1 features --no-progress
#       → Log /srv/cmi/data/m1_feature_build.log
#         (stdout+stderr des Runs + Zeitstempel- und Exit-Code-Zeilen)
#     (R4': ``--no-progress`` = bei „kein Fortschritt" [Store bis zur letzten
#      vollen Stunde = NORMALZUSTAND zwischen den :15-Läufen] SKIP + Exit 0
#      statt Auto-Full-Run; der Full-Scan (Peak ~21 GB, 4-GB-Pi ohne
#      Swapfile) war der OOM-Trigger. Echte Defekt-Fallbacks (a/b/c) laufen
#      unverändert als chunked Full (500-MB-RAM-Budget, pi-tauglich).)
#   * SONNTAG 03:00 (vor dem :15-Lauf desselben Tages) Audit-Sequenz:
#       1) python -m analysis.model.m1 features --full --verify
#          (schreibt NIEMALS nach data/features; Exit 0 = identisch,
#           Exit 3 = DRIFT, Exit 2 = unverifizierbar)
#           rc=3 → ALARM-Logzeile (kein externer Alert in v1)
#       2) python -m analysis.model.m1 features --full  (Referenz-Sync)
#       → Log /srv/cmi/data/m1_feature_audit.log
#     (R4': beide --full-Beine laufen chunked (24-h-Chunks, 500-MB-RAM-Budget)
#      → pi-tauglich; --verify referenziert jetzt den chunked Monats-Run statt
#      des monolithischen 21-GB-Full-Builds. Kommandozeile unverändert.)
#
# Zeitplaner (robust, ohne Busy-Wait, mit Clock-Re-Check):
#   * LAST_RUN = Epoch der zuletzt ausgeführten Zielzeit; jede nächste
#     Zielzeit ist STRIKT größer als max(now, LAST_RUN) -> kein Double-Run
#     desselben :15 / derselben Sunday-Audit, auch nach Clock-Sprüngen
#     (NTP vor/zurück) oder Container-Reboot (M1-Inkrementell ist
#     idempotent: eine verpasste Stunde holt die nächste auf).
#   * next_h15_after / next_sun_after rechnen die nächste Zielminute aus;
#     Ziel = das frühere der beiden (Sun 03:00 gewinnt, falls <= HH:15).
#   * Sleep bis zur Zielminute, aber in Chunks von max. 300 s; danach Uhr
#     neu checken (now2 >= Ziel? → ausführen; sonst neu berechnen + weiter
#     schlafen). Ein Einzel-Run (inkl. erwartetem Exit 3 von --verify) darf
#     den Loop niemals killen: set -u, KEIN set -e, if/else + Exit-Status-Log.
#
# M1-Build = offline-Run (Wetter-Cache); wx_cache ist im Container
# ro-gemountet -> ein Fallback-Refetch schlägt laut fehl (Fail-Laut).
# Kein CMI-Zugriff, kein Port; Persistenz = cmi_data-Volume (USB sda1).
#
# Gate: `bash -n deploy/raspi/merge_loop.sh`
# ============================================================================
set -u

TZ="${TZ:-Europe/Berlin}"
export TZ

WORKDIR="/srv/cmi"
DATA_DIR="/srv/cmi/data"
BUILD_LOG="${DATA_DIR}/m1_feature_build.log"
AUDIT_LOG="${DATA_DIR}/m1_feature_audit.log"
SLEEP_MAX=300  # Obergrenze pro Sleep-Chunk; danach Uhr neu checken

ts() { date '+%F %T %Z'; }

log() { # log <file> <msg>
    printf '[%s] %s\n' "$(ts)" "$2" >> "$1"
}

# Nächste HH:15:00, strikt größer als $1 (Epoch). (02:15 folgt auf 02:15:00
# selbst erst um 03:15 — so wird exakt-gesetzte Zielzeit nicht doppelt
# "fällig".)
next_h15_after() {
    local ref="$1" ymd hh c
    ymd=$(date -d "@$ref" +%F)
    hh=$(date -d "@$ref" +%H)
    c=$(date -d "$ymd ${hh}:15" +%s)
    [ "$c" -le "$ref" ] && c=$((c + 3600))
    echo "$c"
}

# Nächster Sonntag 03:00, strikt größer als $1 (Epoch). Sonntag vor 03:00 ->
# dieser Sonntag 03:00; sonst +7 Tage.
next_sun_after() {
    local ref="$1" dow ymd c
    dow=$(date -d "@$ref" +%u)
    ymd=$(date -d "@$ref" +%F)
    if [ "$dow" -eq 7 ]; then
        c=$(date -d "$ymd 03:00" +%s)
    else
        c=$(date -d "$ymd +$((7 - dow)) days 03:00" +%s)
    fi
    [ "$c" -le "$ref" ] && c=$((c + 604800))
    echo "$c"
}

run_incremental() {
    local rc=0
    log "$BUILD_LOG" "RUN  m1 features (inkrementell, HH:15, no-progress)"
    python -m analysis.model.m1 features --no-progress >> "$BUILD_LOG" 2>&1
    rc=$?
    if [ "$rc" -eq 0 ]; then
        log "$BUILD_LOG" "DONE m1 features rc=0"
    else
        log "$BUILD_LOG" "FAIL m1 features rc=$rc (Loop läuft weiter)"
    fi
}

run_audit() {
    local rc=0
    log "$AUDIT_LOG" "RUN  m1 features --full --verify (So 03:00-Audit)"
    python -m analysis.model.m1 features --full --verify >> "$AUDIT_LOG" 2>&1
    rc=$?
    case "$rc" in
        0) log "$AUDIT_LOG" "DONE verify rc=0 (Store = in-memory-Referenz)" ;;
        3) log "$AUDIT_LOG" "ALARM verify rc=3 = DRIFT: Feature-Store weicht von der in-memory-Referenz ab (Detail-Log oben; kein externer Alert in v1)" ;;
        2) log "$AUDIT_LOG" "WARN verify rc=2 = unverifizierbar (kein Store / kein aktueller Monat)" ;;
        *) log "$AUDIT_LOG" "FAIL verify rc=$rc (unerwarteter Exit-Code)" ;;
    esac
    log "$AUDIT_LOG" "RUN  m1 features --full (Referenz-Sync nach Audit)"
    python -m analysis.model.m1 features --full >> "$AUDIT_LOG" 2>&1
    rc=$?
    if [ "$rc" -eq 0 ]; then
        log "$AUDIT_LOG" "DONE full rc=0"
    else
        log "$AUDIT_LOG" "FAIL full rc=$rc (Loop läuft weiter)"
    fi
}

cd "$WORKDIR" || exit 1
mkdir -p "$DATA_DIR"
log "$BUILD_LOG" "LOOP-START cmi-m1-merge (pid $$, TZ $TZ)"

LAST_RUN=0
while :; do
    now=$(date +%s)
    ref=$now
    [ "$LAST_RUN" -gt "$now" ] && ref=$LAST_RUN
    t15=$(next_h15_after "$ref")
    tsun=$(next_sun_after "$ref")
    if [ "$tsun" -le "$t15" ]; then
        target=$tsun
        kind=sun
    else
        target=$t15
        kind=h15
    fi
    delta=$((target - now))
    [ "$delta" -lt 1 ] && delta=1      # garantiert Vorwaerts (kein Busy-Wait)
    [ "$delta" -gt "$SLEEP_MAX" ] && delta=$SLEEP_MAX
    sleep "$delta"
    # Uhr neu checken: erreicht (oder uberschritten)? -> ausfuhren, sonst
    # Ziel neu berechnen (z. B. nach gecamptem Sleep-Chunk).
    now2=$(date +%s)
    if [ "$now2" -ge "$target" ]; then
        if [ "$kind" = "sun" ]; then
            run_audit
        else
            run_incremental
        fi
        LAST_RUN=$target
    fi
done
