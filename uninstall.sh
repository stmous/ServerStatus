#!/usr/bin/env bash
# Remove serverroom-monitor. By default keeps your data and configuration:
#   /etc/serverroom-monitor        (config.yaml, secrets.env)
#   InfluxDB and Grafana packages and their data
#   /var/backups/serverroom-monitor
#
# Options:
#   --purge   also remove the config/secrets, the local buffer, InfluxDB and
#             Grafana (packages AND data), and the apt repositories
#   --yes     don't ask for confirmation
set -Eeuo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/lib.sh
. "$HERE/scripts/lib.sh"
require_root

PURGE=0
YES=0
for arg in "$@"; do
    case $arg in
    --purge) PURGE=1 ;;
    --yes) YES=1 ;;
    *) die "unknown option $arg" ;;
    esac
done

if ((!YES)); then
    if ((PURGE)); then
        echo "This removes serverroom-monitor, InfluxDB, Grafana AND ALL RECORDED DATA."
    else
        echo "This removes serverroom-monitor (config, InfluxDB and Grafana data are kept)."
    fi
    read -r -p "Continue? [y/N] " ans
    [[ $ans =~ ^[Yy] ]] || exit 1
fi

step "Stopping services"
for unit in serverroom-monitor.service serverroom-kiosk.service serverroom-backlight.timer \
    serverroom-backlight.service serverroom-backup.timer serverroom-backup.service \
    serverroom-telegram-bot.service; do
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$unit"
done
rm -rf /etc/systemd/system/grafana-server.service.d/serverroom.conf \
    /etc/systemd/system/influxdb.service.d/serverroom.conf
systemctl daemon-reload

step "Removing kiosk"
rm -f "/home/$KIOSK_USER/.config/autostart/serverroom-kiosk.desktop"
if [[ -f /etc/lightdm/lightdm.conf ]]; then
    sed -i "s/^autologin-user=$KIOSK_USER$/#autologin-user=/" /etc/lightdm/lightdm.conf
fi
id "$KIOSK_USER" >/dev/null 2>&1 && userdel -r "$KIOSK_USER" 2>/dev/null || true
rm -rf /usr/share/icons/serverroom-blank

step "Removing firewall rules and tailscale serve"
if command -v ufw >/dev/null; then
    mapfile -t ours < <(ufw status numbered 2>/dev/null | grep -F "# serverroom-monitor" |
        sed -E 's/^\[ *([0-9]+)\].*/\1/' | sort -rn)
    for n in "${ours[@]}"; do ufw --force delete "$n" >/dev/null; done
    warn "ufw is still enabled; make sure SSH remains allowed (sudo ufw status)"
fi
command -v tailscale >/dev/null && tailscale serve --https=443 off >/dev/null 2>&1 || true

step "Removing system configuration"
rm -f /etc/systemd/journald.conf.d/serverroom.conf \
    /etc/systemd/system.conf.d/serverroom-watchdog.conf \
    /etc/apt/apt.conf.d/51serverroom-unattended-upgrades
systemctl daemon-reexec

step "Removing application"
rm -rf "$APP_DIR"
id "$SVC_USER" >/dev/null 2>&1 && userdel "$SVC_USER" 2>/dev/null || true

if ((PURGE)); then
    step "Purging data, InfluxDB and Grafana"
    systemctl disable --now grafana-server influxdb >/dev/null 2>&1 || true
    apt-get purge -y grafana influxdb2 influxdb2-cli >/dev/null 2>&1 || true
    rm -rf /var/lib/influxdb /var/lib/grafana /etc/influxdb /etc/grafana \
        /var/lib/serverroom-monitor "$ETC_DIR"
    rm -f /etc/apt/sources.list.d/influxdata.list /etc/apt/sources.list.d/grafana.list \
        /etc/apt/keyrings/influxdata-archive.gpg /etc/apt/keyrings/grafana.gpg
    info "Tailscale was left installed (sudo apt purge tailscale to remove it)"
else
    info "kept: $ETC_DIR, InfluxDB/Grafana and their data, /var/lib/serverroom-monitor"
fi
ok "uninstalled"
