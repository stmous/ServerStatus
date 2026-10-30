#!/usr/bin/env bash
# Regenerate the reference Grafana provisioning committed under grafana/
# from config/config.example.yaml. CI fails if these files are out of date.
# On the Pi the real files are generated from /etc/serverroom-monitor/config.yaml
# by scripts/apply-config.sh; these committed copies are for review/diffing.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=${PYTHON:-python3}
PYTHONPATH=collector "$PYTHON" -m serverroom_monitor -c config/config.example.yaml \
    render-grafana --out grafana --deploy-root /etc/serverroom-monitor/generated >/dev/null
rm -f grafana/grafana-settings.env grafana/kiosk.env
echo "grafana/ regenerated"
