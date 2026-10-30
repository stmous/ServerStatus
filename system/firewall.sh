#!/usr/bin/env bash
# Configure ufw for serverroom-monitor. Idempotent; safe to re-run.
#
#   incoming: deny by default
#   SSH (22/tcp):        from the LAN subnets and the tailscale0 interface
#   HTTPS (443/tcp):     on tailscale0 only (tailscale serve -> Grafana)
#   Grafana (3000/tcp):  from the LAN only if grafana.lan_access is true
#   Tailscale (41641/udp): direct WireGuard connections (optional but faster)
#
# Nothing is opened to the internet: the Pi sits behind your router's NAT and
# no port forwarding is needed for Tailscale.
#
# Usage: firewall.sh --lan "192.168.1.0/24 10.0.0.0/24" [--grafana-lan] [--grafana-port 3000]
set -euo pipefail

LAN=""
GRAFANA_LAN=0
GRAFANA_PORT=3000
while [[ $# -gt 0 ]]; do
    case $1 in
    --lan) LAN=$2; shift 2 ;;
    --grafana-lan) GRAFANA_LAN=1; shift ;;
    --grafana-port) GRAFANA_PORT=$2; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
    esac
done
[[ -n $LAN ]] || { echo "firewall.sh: --lan is required" >&2; exit 2; }

command -v ufw >/dev/null || { echo "ufw is not installed" >&2; exit 1; }

COMMENT="serverroom-monitor"

# Start from a clean slate for *our* rules only (identified by comment), so
# re-runs don't accumulate duplicates and other rules you added survive.
mapfile -t ours < <(ufw status numbered 2>/dev/null | grep -F "# $COMMENT" | sed -E 's/^\[ *([0-9]+)\].*/\1/' | sort -rn)
for n in "${ours[@]}"; do
    yes | ufw delete "$n" >/dev/null
done

# Order matters: allow SSH *before* enabling, or an SSH session gets cut off.
for net in $LAN; do
    ufw limit from "$net" to any port 22 proto tcp comment "$COMMENT" >/dev/null
    if [[ $GRAFANA_LAN == 1 ]]; then
        ufw allow from "$net" to any port "$GRAFANA_PORT" proto tcp comment "$COMMENT" >/dev/null
    fi
done
ufw allow in on tailscale0 to any port 22 proto tcp comment "$COMMENT" >/dev/null
ufw allow in on tailscale0 to any port 443 proto tcp comment "$COMMENT" >/dev/null
ufw allow 41641/udp comment "$COMMENT" >/dev/null

ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw logging low >/dev/null
ufw --force enable >/dev/null
ufw status verbose
