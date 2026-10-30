#!/usr/bin/env bash
# Low-battery hook: shut the Pi down cleanly before the UPS runs flat.
#
# Call it from whatever watches your UPS:
#   * NUT (network UPS tools), in /etc/nut/upsmon.conf:
#         SHUTDOWNCMD "/opt/serverroom-monitor/system/ups/serverroom-ups-shutdown.sh"
#   * a UPS HAT with a "low battery" GPIO, via the example unit
#     serverroom-ups-gpio.service in this directory
#   * the HAT vendor's daemon (most have a "run this command" option)
#
# Anything executable in /etc/serverroom-monitor/hooks/low-battery.d/ runs
# first (e.g. to send a last notification); each gets 10 s.
set -uo pipefail

log() { logger -t serverroom-ups -p daemon.crit "$*"; echo "$*" >&2; }

log "UPS low battery: shutting down"
if [[ -d /etc/serverroom-monitor/hooks/low-battery.d ]]; then
    for hook in /etc/serverroom-monitor/hooks/low-battery.d/*; do
        [[ -x $hook ]] || continue
        log "running hook $hook"
        timeout 10 "$hook" || log "hook $hook failed"
    done
fi
# Stop the writers first so InfluxDB and Grafana close their files cleanly.
systemctl stop serverroom-monitor.service grafana-server.service influxdb.service || true
sync
systemctl poweroff
