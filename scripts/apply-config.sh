#!/usr/bin/env bash
# Apply /etc/serverroom-monitor/config.yaml and secrets.env to the running
# system. Run this after every config or secrets change:
#
#     sudo /opt/serverroom-monitor/scripts/apply-config.sh
#
# It validates the config, regenerates the Grafana provisioning (dashboard
# thresholds, alert rules, contact points), the per-service secret files,
# firewall rules, Tailscale serve and the kiosk, then restarts what changed.
# Idempotent. install.sh runs it as its final configuration step.
set -Eeuo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/lib.sh
. "$HERE/lib.sh"

require_root
[[ -x $SRM ]] || die "$SRM not found; run install.sh first"
[[ -f $CONFIG ]] || die "$CONFIG not found; run install.sh first"

step "Validating configuration"
(load_secrets && "$SRM" -c "$CONFIG" check-config) || die "fix $CONFIG and re-run"

MODE=$(cfg display.mode)

# --- secrets -> per-service env files ---------------------------------------------
step "Writing per-service secret files"
missing=()
n_enabled() { [[ $(cfg "notifications.$1.enabled") == 1 ]]; }
n_enabled telegram && [[ -z $(secret_get TELEGRAM_BOT_TOKEN) ]] && missing+=(TELEGRAM_BOT_TOKEN)
n_enabled webhook && [[ -z $(secret_get WEBHOOK_URL) ]] && missing+=(WEBHOOK_URL)
n_enabled deadman && [[ -z $(secret_get DEADMAN_URL) ]] && missing+=(DEADMAN_URL)
n_enabled email && [[ -n $(cfg notifications.email.smtp_user) && -z $(secret_get SMTP_PASSWORD) ]] &&
    missing+=(SMTP_PASSWORD)
((${#missing[@]} == 0)) || warn "enabled channels need secrets that are empty in $SECRETS: ${missing[*]}"
write_service_envs
ok "env/collector.env, env/grafana.env, env/telegram-bot.env"

# --- sensor -------------------------------------------------------------------------
# Early on purpose: it must not depend on Grafana or anything else below.
step "Sensor overlay"
sync_dht_overlay
[[ $(cfg sensor.model) == dht* ]] || ok "not needed for $(cfg sensor.model)"

# --- Grafana provisioning -----------------------------------------------------------
step "Rendering Grafana provisioning"
ROOT_URL=""
TS_NAME=$(tailscale_dns_name || true)
if [[ $(cfg grafana.tailscale_serve) == 1 && -n $TS_NAME ]]; then
    ROOT_URL="https://$TS_NAME/"
fi
install -d -m 755 "$GEN_DIR"
"$SRM" -c "$CONFIG" render-grafana --out "$GEN_DIR" ${ROOT_URL:+--root-url "$ROOT_URL"} >/dev/null
chmod -R a+rX "$GEN_DIR"
ok "generated in $GEN_DIR${ROOT_URL:+ (root URL $ROOT_URL)}"

install -D -m 644 "$APP_DIR/systemd/grafana-server.service.d/serverroom.conf" \
    /etc/systemd/system/grafana-server.service.d/serverroom.conf
install -D -m 644 "$APP_DIR/systemd/influxdb.service.d/serverroom.conf" \
    /etc/systemd/system/influxdb.service.d/serverroom.conf
for unit in serverroom-monitor.service serverroom-kiosk.service serverroom-backlight.service \
    serverroom-backlight.timer serverroom-backup.service serverroom-backup.timer \
    serverroom-telegram-bot.service; do
    install -m 644 "$APP_DIR/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload

step "Restarting Grafana"
GRAFANA_OK=1
RESTARTED_AT=$(date '+%Y-%m-%d %H:%M:%S')
systemctl enable grafana-server.service >/dev/null 2>&1
systemctl restart grafana-server.service || true
info "waiting for Grafana (the first start on a Pi can take a few minutes)"
if grafana_wait; then
    PASSWORD_OK=1
    grafana_sync_admin_password || PASSWORD_OK=0
    # Fail loudly if provisioning was rejected (e.g. a broken template). Only
    # look at this start: older lines may be from before a fix.
    if journalctl -u grafana-server --since "$RESTARTED_AT" --no-pager 2>/dev/null |
        grep -E 'level=(error|eror)' | grep -iE 'provision|alerting' | grep -v plugin; then
        warn "Grafana reported provisioning errors (above)"
    fi
    if ((PASSWORD_OK)); then
        grafana_sync_public_dashboard "$MODE"
    else
        warn "skipping the kiosk's shared-dashboard check (can't log in to Grafana)"
    fi
    ok "Grafana running"
else
    # Keep going: the sensor, firewall, display and collector don't need Grafana.
    GRAFANA_OK=0
    warn "Grafana did not come up. Last log lines:"
    journalctl -u grafana-server -n 25 --no-pager 2>&1 | sed 's/^/      /' >&2 || true
fi

# --- Tailscale serve ------------------------------------------------------------------
if command -v tailscale >/dev/null && ((GRAFANA_OK)); then
    step "Tailscale"
    if [[ -z $TS_NAME ]]; then
        warn "Tailscale is installed but not logged in. Run: sudo tailscale up --ssh=false"
        warn "then re-run this script to enable HTTPS access to Grafana."
    elif [[ $(cfg grafana.tailscale_serve) == 1 ]]; then
        if tailscale serve --bg --https=443 "http://127.0.0.1:$(cfg grafana.port)" >/dev/null 2>&1; then
            ok "Grafana on your tailnet: https://$TS_NAME/"
        else
            warn "'tailscale serve' failed. Enable HTTPS certificates for your tailnet"
            warn "(admin console > DNS > HTTPS Certificates), then re-run this script."
        fi
    else
        tailscale serve --https=443 off >/dev/null 2>&1 || true
        ok "tailscale serve disabled (grafana.tailscale_serve: false)"
    fi
fi

# --- firewall ---------------------------------------------------------------------------
if [[ $(cfg firewall.enabled) == 1 ]]; then
    step "Firewall (ufw)"
    LAN=$(cfg firewall.lan_subnets)
    [[ -n $LAN ]] || LAN=$(detect_lan_subnets)
    if [[ -z $LAN ]]; then
        warn "could not detect the LAN subnet; set firewall.lan_subnets. Firewall NOT changed."
    else
        args=(--lan "$LAN" --grafana-port "$(cfg grafana.port)")
        [[ $(cfg grafana.lan_access) == 1 ]] && args+=(--grafana-lan)
        "$APP_DIR/system/firewall.sh" "${args[@]}" >/dev/null
        ok "SSH allowed from $LAN and tailscale0$([[ $(cfg grafana.lan_access) == 1 ]] && echo "; Grafana from $LAN")"
    fi
fi

# --- local display ------------------------------------------------------------------------
step "Display: $MODE"
"$APP_DIR/scripts/setup-kiosk.sh" "$MODE"

# --- collector ------------------------------------------------------------------------------
step "Restarting collector"
systemctl enable serverroom-monitor.service >/dev/null 2>&1
systemctl restart serverroom-monitor.service
systemctl enable --now serverroom-backup.timer >/dev/null 2>&1
sleep 3
if systemctl is-active --quiet serverroom-monitor.service; then
    ok "serverroom-monitor is running"
else
    warn "serverroom-monitor is not running: journalctl -u serverroom-monitor -n 30"
fi

# --- Telegram bot ---------------------------------------------------------------------------
step "Telegram bot (/status)"
if [[ $(cfg notifications.telegram.enabled) == 1 && $(cfg notifications.telegram.commands) == 1 ]]; then
    systemctl enable serverroom-telegram-bot.service >/dev/null 2>&1
    systemctl restart serverroom-telegram-bot.service
    sleep 2
    if systemctl is-active --quiet serverroom-telegram-bot.service; then
        ok "send /status to your bot"
    else
        warn "bot not running: journalctl -u serverroom-telegram-bot -n 30"
    fi
else
    systemctl disable --now serverroom-telegram-bot.service >/dev/null 2>&1 || true
    ok "disabled (needs notifications.telegram.enabled and .commands)"
fi

if ((!GRAFANA_OK)); then
    die "configuration applied except Grafana (see the log lines above); fix it and re-run"
fi
printf '\n%sConfiguration applied.%s\n' "$C_GRN" "$C_OFF"
