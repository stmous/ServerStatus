#!/usr/bin/env bash
# Start the cage Wayland compositor with the Chromium kiosk launcher inside.
# cage 0.1.4 (Bookworm) rotates with -r (90 degrees clockwise per flag);
# newer cage dropped -r, so the launcher falls back to wlr-randr there.
set -euo pipefail

args=(-d -s) # no client-side decorations; allow VT switching (Ctrl+Alt+F2)
rotation=${KIOSK_ROTATION:-0}
if [[ $rotation != 0 ]] && cage -h 2>&1 | grep -q -- '-r'; then
    for _ in $(seq 1 $((rotation / 90))); do args+=(-r); done
    export KIOSK_ROTATION_APPLIED=1
fi
exec cage "${args[@]}" -- /opt/serverroom-monitor/kiosk/serverroom-kiosk.sh
