#!/usr/bin/env bash
# Back up config + secrets, the Grafana database and InfluxDB.
#
#   sudo /opt/serverroom-monitor/scripts/backup.sh [DEST_DIR]
#
# Runs daily from serverroom-backup.timer. Writes one tarball per run
# (serverroom-backup-YYYYmmdd-HHMMSS.tar.gz, mode 600 - it contains secrets)
# and keeps the newest `backup.keep` of them. Copy them off the Pi!
#
# Restore: see README > "Backup and restore".
set -Eeuo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/lib.sh
. "$HERE/lib.sh"
require_root

DEST=${1:-$(cfg backup.directory)}
KEEP=$(cfg backup.keep)
STAMP=$(date +%Y%m%d-%H%M%S)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
install -d -m 700 "$DEST"

echo "backing up to $DEST"

# 1. Configuration and secrets
tar -C / -cf "$WORK/config.tar" etc/serverroom-monitor

# 2. Grafana: online-consistent copy of the SQLite DB (alert state, users,
#    shared-dashboard link). Dashboards/rules are provisioned from config anyway.
if [[ -f /var/lib/grafana/grafana.db ]]; then
    sqlite3 /var/lib/grafana/grafana.db ".backup '$WORK/grafana.db'"
fi

# 3. InfluxDB: native backup (needs the operator token).
if command -v influx >/dev/null; then
    INFLUX_TOKEN=$(secret_get INFLUX_OPERATOR_TOKEN) INFLUX_HOST=$(cfg influxdb.url) \
        INFLUX_CONFIGS_PATH=$WORK/influx-configs \
        influx backup "$WORK/influxdb" >/dev/null
fi

# 4. Any points still waiting in the collector's local buffer.
if [[ -f /var/lib/serverroom-monitor/buffer.sqlite ]]; then
    sqlite3 /var/lib/serverroom-monitor/buffer.sqlite ".backup '$WORK/buffer.sqlite'"
fi

OUT="$DEST/serverroom-backup-$STAMP.tar.gz"
(umask 077 && tar -C "$WORK" --exclude influx-configs -czf "$OUT" .)
echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"

# Rotate (file names are our own fixed pattern, so ls is safe here)
# shellcheck disable=SC2012
mapfile -t old < <(ls -1t "$DEST"/serverroom-backup-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)))
for f in "${old[@]}"; do
    rm -f -- "$f"
    echo "removed old backup $(basename "$f")"
done
