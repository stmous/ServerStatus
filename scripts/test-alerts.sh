#!/usr/bin/env bash
# Send a test notification to every enabled channel.
#
#   sudo /opt/serverroom-monitor/scripts/test-alerts.sh            # through Grafana
#   sudo /opt/serverroom-monitor/scripts/test-alerts.sh --direct   # bypass Grafana
#
# The default path uses Grafana's own contact-point test, i.e. exactly what a
# real alert uses. --direct talks to Telegram/SMTP/the webhook itself, which
# helps tell a bad token apart from a Grafana problem.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/lib.sh
. "$HERE/lib.sh"
require_root
load_secrets
exec "$SRM" -c "$CONFIG" test-alerts "$@"
