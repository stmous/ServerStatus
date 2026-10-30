#!/usr/bin/env bash
# Chromium kiosk for the Server Room dashboard.
#
# Works inside cage (Lite), the Raspberry Pi OS desktop (labwc, wayfire) and
# X11. Restarts Chromium whenever it exits or crashes, and once a day (long
# running Chromium sessions slowly leak memory on a 4 GB Pi).
#
# Settings come from /etc/serverroom-monitor/generated/kiosk.env (rendered from
# config.yaml) and kiosk-url.env (the shared-dashboard URL created by
# install.sh). Both are sourced here as well, so the desktop autostart entry
# needs no systemd.
set -uo pipefail

for f in /etc/serverroom-monitor/generated/kiosk.env /etc/serverroom-monitor/generated/kiosk-url.env; do
    # shellcheck disable=SC1090
    [[ -r $f ]] && set -a && . "$f" && set +a
done

GRAFANA=${KIOSK_GRAFANA_URL:-http://127.0.0.1:3000}
# Fallback to the normal dashboard in kiosk view if no shared link exists
# (that one needs a login, so the shared link is strongly preferred).
URL=${KIOSK_URL:-$GRAFANA/d/${KIOSK_DASHBOARD_UID:-serverroom}?kiosk&refresh=30s}
THEME=${KIOSK_THEME:-dark}
DAILY_RESTART=${KIOSK_DAILY_RESTART:-04:00}
PROFILE=${XDG_CONFIG_HOME:-$HOME/.config}/serverroom-kiosk

log() { printf 'serverroom-kiosk: %s\n' "$*" >&2; }

# --- display setup ------------------------------------------------------------
if [[ -n ${WAYLAND_DISPLAY:-} ]]; then
    if [[ ${KIOSK_ROTATION:-0} != 0 && -z ${KIOSK_ROTATION_APPLIED:-} ]] && command -v wlr-randr >/dev/null; then
        output=$(wlr-randr | awk '/^[^ ]/ {print $1; exit}')
        [[ -n $output ]] && wlr-randr --output "$output" --transform "${KIOSK_ROTATION}" || true
    fi
    if [[ ${KIOSK_SCREEN_BLANKING:-0} == 1 ]] && command -v swayidle >/dev/null && command -v wlopm >/dev/null; then
        swayidle -w timeout "${KIOSK_BLANK_AFTER:-600}" 'wlopm --off \*' resume 'wlopm --on \*' &
    fi
elif [[ -n ${DISPLAY:-} ]]; then
    if [[ ${KIOSK_SCREEN_BLANKING:-0} == 1 ]]; then
        xset s "${KIOSK_BLANK_AFTER:-600}" s blank +dpms || true
    else
        xset s off s noblank -dpms || true
    fi
    command -v unclutter >/dev/null && unclutter -idle 0.5 -root &
fi

# --- wait for Grafana -----------------------------------------------------------
for i in $(seq 1 120); do
    curl -fsS -m 3 "$GRAFANA/api/health" >/dev/null 2>&1 && break
    ((i == 1)) && log "waiting for Grafana at $GRAFANA"
    sleep 2
done

CHROMIUM=$(command -v chromium || command -v chromium-browser || true)
[[ -n $CHROMIUM ]] || { log "chromium is not installed"; exit 1; }

# shellcheck disable=SC2054  # commas inside --disable-features are intended
FLAGS=(
    --kiosk
    --noerrdialogs
    --disable-infobars
    --no-first-run
    --no-default-browser-check
    --disable-session-crashed-bubble
    --disable-translate
    --disable-features=Translate,TranslateUI,OverscrollHistoryNavigation,TouchpadOverscrollHistoryNavigation,MediaRouter
    --overscroll-history-navigation=0
    --disable-pinch
    --hide-scrollbars
    --check-for-update-interval=31536000
    --disable-component-update
    --password-store=basic
    --ozone-platform-hint=auto
    --disk-cache-size=33554432
    --user-data-dir="$PROFILE"
)
[[ $THEME == dark ]] && FLAGS+=(--force-dark-mode)

# Don't show "Chromium didn't shut down correctly" after a crash or power cut.
mark_clean_exit() {
    local prefs=$PROFILE/Default/Preferences
    [[ -f $prefs ]] && sed -i -e 's/"exited_cleanly":false/"exited_cleanly":true/' \
        -e 's/"exit_type":"[A-Za-z]*"/"exit_type":"Normal"/' "$prefs"
}

trap 'kill "${pid:-}" 2>/dev/null; exit 0' TERM INT

backoff=5
while true; do
    mark_clean_exit
    log "starting $CHROMIUM -> $URL"
    started=$(date +%s)
    "$CHROMIUM" "${FLAGS[@]}" "$URL" >/dev/null 2>&1 &
    pid=$!
    restarted_today=""
    while kill -0 "$pid" 2>/dev/null; do
        sleep 30
        now=$(date +%H:%M)
        if [[ -n $DAILY_RESTART && $now == "$DAILY_RESTART" && -z $restarted_today ]]; then
            log "daily restart"
            restarted_today=1
            kill "$pid" 2>/dev/null
        fi
    done
    wait "$pid"
    rc=$?
    # Quick repeated crashes back off (up to 60 s); a long healthy run resets.
    if (($(date +%s) - started > 300)); then backoff=5; else backoff=$((backoff * 2 > 60 ? 60 : backoff * 2)); fi
    log "chromium exited (status $rc); restarting in ${backoff}s"
    sleep "$backoff"
done
