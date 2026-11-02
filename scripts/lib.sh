# shellcheck shell=bash
# Shared helpers for install.sh, apply-config.sh and uninstall.sh.
# Sourced, not executed.
# shellcheck disable=SC2034  # globals are used by the scripts that source this

APP_DIR=${APP_DIR:-/opt/serverroom-monitor}
ETC_DIR=${ETC_DIR:-/etc/serverroom-monitor}
CONFIG=${CONFIG:-$ETC_DIR/config.yaml}
SECRETS=${SECRETS:-$ETC_DIR/secrets.env}
GEN_DIR=$ETC_DIR/generated
ENV_DIR=$ETC_DIR/env
VENV=$APP_DIR/venv
PY=$VENV/bin/python
SRM=$VENV/bin/serverroom-monitor
SVC_USER=serverroom
KIOSK_USER=kiosk
GRAFANA_INTERNAL=http://127.0.0.1:3000

if [[ -t 1 ]]; then
    C_BOLD=$'\e[1m' C_RED=$'\e[31m' C_YEL=$'\e[33m' C_GRN=$'\e[32m' C_OFF=$'\e[0m'
else
    C_BOLD="" C_RED="" C_YEL="" C_GRN="" C_OFF=""
fi

step() { printf '\n%s==> %s%s\n' "$C_BOLD" "$*" "$C_OFF"; }
info() { printf '    %s\n' "$*"; }
ok() { printf '    %s✔%s %s\n' "$C_GRN" "$C_OFF" "$*"; }
warn() { printf '    %s!%s %s\n' "$C_YEL" "$C_OFF" "$*" >&2; }
die() {
    printf '%sERROR:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2
    exit 1
}

require_root() { [[ $EUID -eq 0 ]] || die "run as root (sudo $0)"; }

# cfg KEY - print a value from the validated config (defaults applied).
# Dotted path into the Config dataclass, e.g. cfg display.mode
# Booleans print as 1/0, lists as space-separated words.
cfg() {
    "$PY" - "$CONFIG" "$1" <<'PY'
import sys
from pathlib import Path
from serverroom_monitor.config import load_config
value = load_config(Path(sys.argv[1]), env={})
for part in sys.argv[2].split("."):
    value = getattr(value, part)
if isinstance(value, bool):
    print(1 if value else 0)
elif isinstance(value, (tuple, list)):
    print(" ".join(str(v) for v in value))
elif isinstance(value, float) and value.is_integer():
    print(int(value))
else:
    print("" if value is None else value)
PY
}

# --- secrets.env ------------------------------------------------------------

load_secrets() {
    [[ -f $SECRETS ]] || return 0
    set -a
    # shellcheck disable=SC1090
    . "$SECRETS"
    set +a
}

# secret_get KEY - value from secrets.env (empty if unset)
secret_get() {
    (
        load_secrets
        printf '%s' "${!1:-}"
    )
}

# secret_set KEY VALUE - add or replace KEY in secrets.env, single-quoted so
# URLs with & or ? survive both bash sourcing and systemd's EnvironmentFile.
secret_set() {
    local key=$1 value=$2 quoted tmp
    # Single quotes mean the same thing to bash and to systemd, as long as the
    # value itself contains none.
    [[ $value != *"'"* ]] || die "$key must not contain a single quote (')"
    quoted="'$value'"
    tmp=$(mktemp "$ETC_DIR/.secrets.XXXXXX")
    chmod 600 "$tmp"
    if [[ -f $SECRETS ]] && grep -q "^${key}=" "$SECRETS"; then
        KEY=$key VAL=$quoted awk -F= 'BEGIN{k=ENVIRON["KEY"]; v=ENVIRON["VAL"]}
            $1==k {print k "=" v; next} {print}' "$SECRETS" >"$tmp"
    else
        { [[ -f $SECRETS ]] && cat "$SECRETS"; printf '%s=%s\n' "$key" "$quoted"; } >"$tmp"
    fi
    mv "$tmp" "$SECRETS"
    chmod 600 "$SECRETS"
    chown root:root "$SECRETS"
}

random_secret() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "${1:-32}"; }

# write_service_envs - split secrets.env into per-service env files so each
# service only sees the secrets it needs (least privilege).
write_service_envs() {
    install -d -m 700 -o root -g root "$ENV_DIR"
    (
        load_secrets
        umask 077
        {
            echo "# Generated from secrets.env by serverroom-monitor. Do not edit."
            printf "INFLUX_WRITE_TOKEN='%s'\n" "${INFLUX_WRITE_TOKEN:-}"
            [[ -n ${HEARTBEAT_URL:-} ]] && printf "HEARTBEAT_URL='%s'\n" "$HEARTBEAT_URL"
        } >"$ENV_DIR/collector.env"
        {
            echo "# Generated from secrets.env by serverroom-monitor. Do not edit."
            printf "GF_SECURITY_ADMIN_PASSWORD='%s'\n" "${GRAFANA_ADMIN_PASSWORD:-}"
            printf "GF_SMTP_PASSWORD='%s'\n" "${SMTP_PASSWORD:-}"
            printf "INFLUX_READ_TOKEN='%s'\n" "${INFLUX_READ_TOKEN:-}"
            printf "TELEGRAM_BOT_TOKEN='%s'\n" "${TELEGRAM_BOT_TOKEN:-}"
            printf "WEBHOOK_URL='%s'\n" "${WEBHOOK_URL:-}"
            printf "DEADMAN_URL='%s'\n" "${DEADMAN_URL:-}"
        } >"$ENV_DIR/grafana.env"
    )
    chmod 600 "$ENV_DIR"/*.env
}

# --- Grafana API --------------------------------------------------------------

grafana_wait() {
    for _ in $(seq 1 150); do # up to ~5 minutes
        curl -fsS -m 3 "$GRAFANA_INTERNAL/api/health" >/dev/null 2>&1 && return 0
        sleep 2
    done
    return 1
}

# Credentials go to curl on stdin (-K -) so they never appear in `ps`.
_curl_admin() {
    local pw
    pw=$(secret_get GRAFANA_ADMIN_PASSWORD)
    printf 'user = "admin:%s"\n' "${pw//\"/\\\"}" | curl -K - "$@"
}

grafana_api() { # METHOD PATH [JSON]
    local method=$1 path=$2 data=${3:-}
    _curl_admin -sS -m 30 -X "$method" -H 'Content-Type: application/json' \
        ${data:+--data "$data"} "$GRAFANA_INTERNAL$path"
}

# Ensure the stored admin password works (GF_SECURITY_ADMIN_PASSWORD only
# applies when Grafana creates its database, so re-runs must reset it).
grafana_sync_admin_password() {
    local code
    code=$(_curl_admin -s -o /dev/null -w '%{http_code}' "$GRAFANA_INTERNAL/api/user")
    [[ $code == 200 ]] && return 0
    info "resetting Grafana admin password to the one in secrets.env"
    secret_get GRAFANA_ADMIN_PASSWORD |
        (cd /usr/share/grafana && runuser -u grafana -- grafana cli --homepath /usr/share/grafana \
            --config /etc/grafana/grafana.ini admin reset-admin-password --password-from-stdin >/dev/null)
}

# The kiosk opens a read-only shared ("public") dashboard link so it needs no
# login. It only exists while display.mode is hdmi.
grafana_sync_public_dashboard() {
    local mode=$1 existing token uid
    existing=$(grafana_api GET /api/dashboards/uid/serverroom/public-dashboards || true)
    token=$(jq -r '.accessToken // empty' <<<"$existing" 2>/dev/null || true)
    uid=$(jq -r '.uid // empty' <<<"$existing" 2>/dev/null || true)
    if [[ $mode == hdmi ]]; then
        if [[ -z $token ]]; then
            existing=$(grafana_api POST /api/dashboards/uid/serverroom/public-dashboards \
                '{"isEnabled":true,"share":"public","timeSelectionEnabled":false,"annotationsEnabled":false}')
            token=$(jq -r '.accessToken // empty' <<<"$existing")
        fi
        if [[ -z $token ]]; then
            warn "could not create the shared dashboard link: $existing"
            warn "the kiosk will fall back to the login-protected dashboard"
            rm -f "$GEN_DIR/kiosk-url.env"
            return 0
        fi
        printf "KIOSK_URL='%s/public-dashboards/%s'\n" "$GRAFANA_INTERNAL" "$token" >"$GEN_DIR/kiosk-url.env"
        chmod 644 "$GEN_DIR/kiosk-url.env"
        ok "kiosk uses shared dashboard link"
    else
        rm -f "$GEN_DIR/kiosk-url.env"
        if [[ -n $uid ]]; then
            grafana_api DELETE "/api/dashboards/uid/serverroom/public-dashboards/$uid" >/dev/null
            ok "removed shared dashboard link (display mode is not hdmi)"
        fi
    fi
}

# --- Tailscale --------------------------------------------------------------

tailscale_dns_name() {
    command -v tailscale >/dev/null || return 0
    tailscale status --json 2>/dev/null | jq -r '.Self.DNSName // empty' | sed 's/\.$//'
}

# --- boot config (config.txt) --------------------------------------------------

boot_config() {
    if [[ -n ${BOOT_CONFIG_PATH:-} ]]; then
        echo "$BOOT_CONFIG_PATH"
    elif [[ -f /boot/firmware/config.txt ]]; then
        echo /boot/firmware/config.txt
    elif [[ -f /boot/config.txt ]]; then
        echo /boot/config.txt
    fi
}

DHT_MARKER="# serverroom-monitor: DHT sensor (managed, do not edit)"
REBOOT_FLAG=${REBOOT_FLAG:-/run/reboot-required}

# Remove the managed marker line and the line after it, in place.
_remove_dht_block() {
    local tmp
    tmp=$(mktemp)
    awk -v m="$DHT_MARKER" 'skip {skip = 0; next} $0 == m {skip = 1; next} {print}' "$1" >"$tmp"
    cat "$tmp" >"$1" # keep the original file's owner and mode
    rm -f "$tmp"
}

# DHT11/DHT22 sensors are read by the kernel's dht11 driver, enabled with a
# device-tree overlay. Keep exactly one managed overlay line in config.txt in
# sync with sensor.model / sensor.gpio. Changes need a reboot; the overlay is
# also loaded at runtime when possible so a first install can work at once.
sync_dht_overlay() {
    local boot model gpio line current
    boot=$(boot_config)
    model=$(cfg sensor.model)
    gpio=$(cfg sensor.gpio)
    if [[ -z $boot ]]; then
        [[ $model == dht* ]] && warn "config.txt not found: add 'dtoverlay=dht11,gpiopin=$gpio' yourself"
        return 0
    fi
    current=$(grep -A1 -xF "$DHT_MARKER" "$boot" | sed -n 2p || true)
    if [[ $model == dht11 || $model == dht22 ]]; then
        line="dtoverlay=dht11,gpiopin=$gpio"
        [[ $current == "$line" ]] && return 0
        _remove_dht_block "$boot"
        # [all] so the line is never swallowed by an earlier [pi4]/[cm4] filter.
        printf '\n[all]\n%s\n%s\n' "$DHT_MARKER" "$line" >>"$boot"
        touch "$REBOOT_FLAG"
        ok "added '$line' to $boot (reboot required)"
        if command -v dtoverlay >/dev/null && [[ -z $current ]]; then
            if dtoverlay dht11 "gpiopin=$gpio" 2>/dev/null; then
                ok "DHT overlay loaded at runtime"
            fi
        fi
    elif [[ -n $current ]]; then
        _remove_dht_block "$boot"
        touch "$REBOOT_FLAG"
        ok "removed DHT overlay from $boot (reboot required)"
    fi
}

# --- network ------------------------------------------------------------------

detect_lan_subnets() {
    local dev
    dev=$(ip -4 route show default | awk '{for (i=1;i<=NF;i++) if ($i=="dev") {print $(i+1); exit}}')
    [[ -n $dev ]] || return 0
    ip -4 route show dev "$dev" scope link | awk '$1 ~ /\// {print $1}' | sort -u | tr '\n' ' '
}
