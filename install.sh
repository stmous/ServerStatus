#!/usr/bin/env bash
# Server Room Monitor installer for Raspberry Pi OS Bookworm (64-bit).
#
#   git clone <repo> && cd <repo> && sudo ./install.sh
#
# Idempotent: re-run it any time (after a git pull, or to repair an install).
# Existing config.yaml and secrets.env are never overwritten.
#
# Options:
#   --non-interactive    never prompt; fail if a required secret is missing
#   --skip-sensor-check  don't fail when the sensor is not found on I2C
#   --force              continue on an unsupported OS / architecture / model
set -Eeuo pipefail

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/lib.sh
. "$REPO_DIR/scripts/lib.sh"

INTERACTIVE=1
SKIP_SENSOR=0
FORCE=0
for arg in "$@"; do
    case $arg in
    --non-interactive) INTERACTIVE=0 ;;
    --skip-sensor-check) SKIP_SENSOR=1 ;;
    --force) FORCE=1 ;;
    -h | --help)
        sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    *) die "unknown option: $arg (see --help)" ;;
    esac
done
[[ -t 0 ]] || INTERACTIVE=0

require_root
LOG=/var/log/serverroom-monitor-install.log
exec > >(tee -a "$LOG") 2>&1
echo "=== serverroom-monitor install $(date -Is) ==="
trap 'die "install failed at line $LINENO (see $LOG)"' ERR

export DEBIAN_FRONTEND=noninteractive
BOOT_CONFIG=/boot/firmware/config.txt
[[ -f $BOOT_CONFIG ]] || BOOT_CONFIG=/boot/config.txt
INFLUX_KEY_FPR=24C975CBA61A024EE1B631787C3D57159FC2F927

# ============================================================================
step "Checking platform"
# ============================================================================
# shellcheck disable=SC1091
. /etc/os-release
ARCH=$(uname -m)
MODEL=$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo "unknown")
info "OS: ${PRETTY_NAME:-unknown}  arch: $ARCH  model: $MODEL"
if [[ ${VERSION_CODENAME:-} != bookworm ]]; then
    ((FORCE)) || die "Raspberry Pi OS Bookworm is required (found ${VERSION_CODENAME:-?}); --force to try anyway"
    warn "untested OS release ${VERSION_CODENAME:-?}"
fi
if [[ $ARCH != aarch64 ]]; then
    # InfluxDB 2.x is only built for 64-bit ARM.
    ((FORCE)) || die "64-bit Raspberry Pi OS (aarch64) is required; found $ARCH"
    warn "unsupported architecture $ARCH"
fi
if [[ $MODEL != *"Raspberry Pi 4"* ]]; then
    ((FORCE)) || die "designed for a Raspberry Pi 4 (found: $MODEL); --force to continue"
    warn "untested hardware: $MODEL"
fi
ROOT_DEV=$(findmnt -no SOURCE /)
case $ROOT_DEV in
/dev/mmcblk*) warn "root filesystem is on the SD card ($ROOT_DEV); an SSD is more durable" ;;
*) info "root filesystem: $ROOT_DEV" ;;
esac
curl -fsS -m 10 -o /dev/null https://apt.grafana.com/gpg.key || die "no internet access (needed for packages)"
ok "platform OK"

# ============================================================================
step "Installing base packages"
# ============================================================================
apt-get update -q
apt-get install -y -q --no-install-recommends \
    python3 python3-venv python3-systemd python3-lgpio i2c-tools \
    curl gnupg ca-certificates jq sqlite3 rsync openssl \
    ufw unattended-upgrades apt-listchanges fonts-dejavu-core systemd-timesyncd >/dev/null
ok "base packages"

# ============================================================================
step "Installing application to $APP_DIR"
# ============================================================================
install -d -m 755 "$APP_DIR"
if [[ $REPO_DIR != "$APP_DIR" ]]; then
    rsync -a --delete --exclude .git --exclude venv --exclude '__pycache__' \
        --exclude '.*_cache' "$REPO_DIR/" "$APP_DIR/"
fi
chown -R root:root "$APP_DIR"
# --system-site-packages: lets Blinka use the distro's lgpio and python3-systemd
# (as recommended by Adafruit for Bookworm). Pinned deps still come from the venv.
[[ -x $PY ]] || python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install -q --disable-pip-version-check -r "$APP_DIR/requirements.txt"
"$VENV/bin/pip" install -q --disable-pip-version-check --no-deps --force-reinstall "$APP_DIR"
ok "collector $("$SRM" --version)"

# ============================================================================
step "Configuration"
# ============================================================================
install -d -m 755 "$ETC_DIR"
if [[ ! -f $CONFIG ]]; then
    install -m 644 "$APP_DIR/config/config.example.yaml" "$CONFIG"
    ok "created $CONFIG from the example"
    if ((INTERACTIVE)); then
        echo
        read -r -p "    Edit the config now (location, sensor, display, notifications)? [Y/n] " ans
        if [[ ${ans:-y} =~ ^[Yy] ]]; then
            "${EDITOR:-nano}" "$CONFIG"
        fi
    else
        warn "using example defaults; edit $CONFIG and run scripts/apply-config.sh"
    fi
fi
until "$SRM" -c "$CONFIG" check-config >/dev/null 2>&1; do
    "$SRM" -c "$CONFIG" check-config || true
    ((INTERACTIVE)) || die "invalid $CONFIG"
    read -r -p "    Fix it now in an editor? [Y/n] " ans
    [[ ${ans:-y} =~ ^[Yy] ]] || die "invalid $CONFIG"
    "${EDITOR:-nano}" "$CONFIG"
done
ok "config valid"

# ============================================================================
step "Secrets ($SECRETS)"
# ============================================================================
if [[ ! -f $SECRETS ]]; then
    install -m 600 -o root -g root "$APP_DIR/config/secrets.env.example" "$SECRETS"
fi
chmod 600 "$SECRETS"
chown root:root "$SECRETS"
for key in GRAFANA_ADMIN_PASSWORD INFLUX_ADMIN_PASSWORD INFLUX_OPERATOR_TOKEN; do
    if [[ -z $(secret_get "$key") ]]; then
        len=32
        [[ $key == INFLUX_OPERATOR_TOKEN ]] && len=64
        secret_set "$key" "$(random_secret "$len")"
        info "generated $key"
    fi
done

prompt_secret() { # KEY "description"
    local key=$1 what=$2 value=""
    [[ -n $(secret_get "$key") ]] && return 0
    if ((INTERACTIVE)); then
        read -r -s -p "    $what ($key): " value
        echo
    fi
    [[ -n $value ]] || die "$key is required for $what; add it to $SECRETS and re-run"
    secret_set "$key" "$value"
}
[[ $(cfg notifications.telegram.enabled) == 1 ]] && prompt_secret TELEGRAM_BOT_TOKEN "Telegram bot token"
[[ $(cfg notifications.webhook.enabled) == 1 ]] && prompt_secret WEBHOOK_URL "Webhook URL"
[[ $(cfg notifications.deadman.enabled) == 1 ]] && prompt_secret DEADMAN_URL "Deadman ping URL"
if [[ $(cfg notifications.email.enabled) == 1 && -n $(cfg notifications.email.smtp_user) ]]; then
    prompt_secret SMTP_PASSWORD "SMTP password"
fi
if [[ -z $(secret_get HEARTBEAT_URL) && -z $(cfg heartbeat.url) && $INTERACTIVE == 1 ]]; then
    read -r -p "    Heartbeat URL, e.g. https://hc-ping.com/<uuid> (Enter to skip): " hb
    [[ -n $hb ]] && secret_set HEARTBEAT_URL "$hb"
fi
ok "secrets complete (mode 600, root only)"

# ============================================================================
step "System: I2C, watchdog, time sync, journald, unattended upgrades"
# ============================================================================
if command -v raspi-config >/dev/null; then
    raspi-config nonint do_i2c 0
fi
modprobe i2c-dev 2>/dev/null || true
grep -q '^i2c-dev' /etc/modules 2>/dev/null || echo i2c-dev >>/etc/modules
ok "I2C enabled"

if [[ -f $BOOT_CONFIG ]] && ! grep -qE '^dtparam=watchdog=on' "$BOOT_CONFIG"; then
    printf '\n[all]\n# serverroom-monitor: hardware watchdog\ndtparam=watchdog=on\n' >>"$BOOT_CONFIG"
    REBOOT_NEEDED=1
fi
install -D -m 644 "$APP_DIR/system/watchdog.conf" /etc/systemd/system.conf.d/serverroom-watchdog.conf
ok "hardware watchdog (systemd RuntimeWatchdogSec=15s)"

timedatectl set-ntp true
systemctl enable --now systemd-timesyncd.service >/dev/null 2>&1 || true
ok "NTP via systemd-timesyncd"

install -D -m 644 "$APP_DIR/system/journald.conf" /etc/systemd/journald.conf.d/serverroom.conf
install -d -m 2755 -g systemd-journal /var/log/journal
systemctl restart systemd-journald
ok "journald: persistent, capped at 200 MB"

install -m 644 "$APP_DIR/system/unattended-upgrades.conf" /etc/apt/apt.conf.d/51serverroom-unattended-upgrades
install -m 644 "$APP_DIR/system/auto-upgrades.conf" /etc/apt/apt.conf.d/20auto-upgrades
systemctl enable --now unattended-upgrades.service >/dev/null 2>&1 || true
ok "unattended security upgrades (Grafana/InfluxDB excluded)"
systemctl daemon-reexec # apply the watchdog setting to PID 1

# ============================================================================
step "Service user"
# ============================================================================
if ! id "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/serverroom-monitor --no-create-home \
        --shell /usr/sbin/nologin --comment "Server room monitor" "$SVC_USER"
fi
for g in i2c gpio; do
    getent group "$g" >/dev/null && usermod -aG "$g" "$SVC_USER"
done
ok "user $SVC_USER ($(id -nG "$SVC_USER" | tr ' ' ','))"

# ============================================================================
step "InfluxDB 2.x"
# ============================================================================
install -d -m 755 /etc/apt/keyrings
if [[ ! -f /etc/apt/keyrings/influxdata-archive.gpg ]]; then
    tmp=$(mktemp)
    curl -fsSL -o "$tmp" https://repos.influxdata.com/influxdata-archive.key
    gpg --show-keys --with-fingerprint --with-colons "$tmp" 2>/dev/null |
        grep -q "^fpr:\+$INFLUX_KEY_FPR:$" || die "InfluxData signing key fingerprint mismatch"
    gpg --dearmor <"$tmp" >/etc/apt/keyrings/influxdata-archive.gpg
    rm -f "$tmp"
fi
echo "deb [signed-by=/etc/apt/keyrings/influxdata-archive.gpg] https://repos.influxdata.com/debian stable main" \
    >/etc/apt/sources.list.d/influxdata.list
if [[ ! -f /etc/apt/keyrings/grafana.gpg ]]; then
    curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor >/etc/apt/keyrings/grafana.gpg
fi
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" \
    >/etc/apt/sources.list.d/grafana.list
apt-get update -q
apt-get install -y -q influxdb2 influxdb2-cli >/dev/null

# Listen on localhost only and never phone home.
INFLUX_TOML=/etc/influxdb/config.toml
touch "$INFLUX_TOML"
toml_set() { # key value(raw TOML)
    if grep -qE "^$1 *=" "$INFLUX_TOML"; then
        sed -i -E "s|^$1 *=.*|$1 = $2|" "$INFLUX_TOML"
    else
        echo "$1 = $2" >>"$INFLUX_TOML"
    fi
}
toml_set http-bind-address '"127.0.0.1:8086"'
toml_set reporting-disabled true
install -D -m 644 "$APP_DIR/systemd/influxdb.service.d/serverroom.conf" \
    /etc/systemd/system/influxdb.service.d/serverroom.conf
systemctl daemon-reload
systemctl enable influxdb.service >/dev/null 2>&1
systemctl restart influxdb.service

ret_raw=$(cfg influxdb.retention_raw)
ret_ds=$(cfg influxdb.retention_downsampled)
tokens=$(
    load_secrets
    CURRENT_WRITE_TOKEN=${INFLUX_WRITE_TOKEN:-} CURRENT_READ_TOKEN=${INFLUX_READ_TOKEN:-} \
        INFLUX_HOST=$(cfg influxdb.url) INFLUX_ORG=$(cfg influxdb.org) \
        INFLUX_BUCKET=$(cfg influxdb.bucket) INFLUX_BUCKET_DS=$(cfg influxdb.bucket_downsampled) \
        RETENTION_RAW_S=${ret_raw%.*} RETENTION_DS_S=${ret_ds%.*} \
        "$APP_DIR/influxdb/setup.sh"
)
while IFS='=' read -r key value; do
    [[ $key == INFLUX_WRITE_TOKEN || $key == INFLUX_READ_TOKEN ]] && secret_set "$key" "$value"
done <<<"$tokens"
ok "InfluxDB: org, buckets (raw $((${ret_raw%.*} / 86400))d, 5m $((${ret_ds%.*} / 86400))d), downsampling task, tokens"

# ============================================================================
step "Grafana"
# ============================================================================
apt-get install -y -q grafana >/dev/null
ok "grafana $(dpkg-query -W -f='${Version}' grafana)"

# ============================================================================
step "Tailscale"
# ============================================================================
if ! command -v tailscale >/dev/null; then
    curl -fsSL "https://pkgs.tailscale.com/stable/debian/${VERSION_CODENAME:-bookworm}.noarmor.gpg" \
        -o /usr/share/keyrings/tailscale-archive-keyring.gpg
    curl -fsSL "https://pkgs.tailscale.com/stable/debian/${VERSION_CODENAME:-bookworm}.tailscale-keyring.list" \
        -o /etc/apt/sources.list.d/tailscale.list
    apt-get update -q
    apt-get install -y -q tailscale >/dev/null
fi
systemctl enable --now tailscaled.service >/dev/null 2>&1
if [[ -z $(tailscale_dns_name) ]]; then
    if [[ -n $(secret_get TAILSCALE_AUTHKEY) ]]; then
        secret_get TAILSCALE_AUTHKEY >/run/serverroom-ts-key
        chmod 600 /run/serverroom-ts-key
        tailscale up --auth-key "file:/run/serverroom-ts-key" --hostname "$(hostname)" || warn "tailscale up failed"
        rm -f /run/serverroom-ts-key
    else
        warn "Tailscale is not logged in yet. After install run:  sudo tailscale up"
        warn "then:  sudo $APP_DIR/scripts/apply-config.sh   (enables HTTPS access)"
    fi
fi
[[ -n $(tailscale_dns_name) ]] && ok "tailnet name: $(tailscale_dns_name)"

# ============================================================================
step "Applying configuration (Grafana, firewall, display, collector)"
# ============================================================================
"$APP_DIR/scripts/apply-config.sh"

# ============================================================================
step "Checking the sensor"
# ============================================================================
# One-shot read as the service user. DHT sensors fail individual reads
# fairly often, so take the first good one of a few attempts.
check_reading() {
    local out good
    out=$(runuser -u "$SVC_USER" -- "$SRM" -c "$CONFIG" read --count "$1" 2>/dev/null || true)
    good=$(grep -m1 '"ok": true' <<<"$out" || true)
    if [[ -n $good ]]; then
        ok "reading: $(jq -r '"\(.temperature) °C, \(.humidity) %RH, dew point \(.dew_point) °C"' <<<"$good")"
    else
        warn "the sensor read failed: $(tail -1 <<<"$out")"
        ((SKIP_SENSOR)) || die "sensor read failed (see README > Troubleshooting); --skip-sensor-check to ignore"
    fi
}

SENSOR_MODEL=$(cfg sensor.model)
if [[ $SENSOR_MODEL == simulated ]]; then
    warn "sensor.model is 'simulated' - no hardware check"
elif [[ $SENSOR_MODEL == dht11 || $SENSOR_MODEL == dht22 ]]; then
    GPIO=$(cfg sensor.gpio)
    info "$SENSOR_MODEL on GPIO$GPIO via the kernel dht11 driver"
    if grep -qs '^dht11' /sys/bus/iio/devices/iio:device*/name; then
        ok "DHT device present in /sys/bus/iio/devices"
        check_reading 3
    else
        warn "the dht11 overlay is not active yet. Reboot, then check with:"
        warn "  sudo $APP_DIR/scripts/test-sensor.py"
        REBOOT_NEEDED=1
    fi
else
    ADDR=$(printf '%02x' "$(cfg sensor.i2c_address)")
    info "i2cdetect on bus 1 (expecting $SENSOR_MODEL at 0x$ADDR):"
    i2cdetect -y 1 | sed 's/^/      /' || true
    # i2cdetect prints found devices as their hex address (or UU if a kernel
    # driver has claimed them).
    if i2cdetect -y 1 2>/dev/null | awk 'NR>1 {for (i=2;i<=NF;i++) print $i}' | grep -qx "$ADDR"; then
        ok "device found at 0x$ADDR"
        check_reading 1
    else
        warn "nothing at 0x$ADDR. Check wiring: VIN->pin 1 (3.3V), GND->pin 6, SDA->pin 3, SCL->pin 5"
        ((SKIP_SENSOR)) || die "sensor not detected (re-run with --skip-sensor-check to finish anyway)"
    fi
fi
if [[ $SENSOR_MODEL != simulated ]]; then
    if [[ $(cfg display.mode) == oled ]]; then
        OADDR=$(printf '%02x' "$(cfg display.oled.i2c_address)")
        if i2cdetect -y 1 2>/dev/null | awk 'NR>1 {for (i=2;i<=NF;i++) print $i}' | grep -qx "$OADDR"; then
            ok "OLED found at 0x$OADDR"
        else
            warn "OLED not found at 0x$OADDR (display.mode is oled)"
        fi
    fi
fi

# ============================================================================
step "Done"
# ============================================================================
TS=$(tailscale_dns_name || true)
cat <<EOF

    Dashboard (on the Pi):   $GRAFANA_INTERNAL/d/serverroom
    Dashboard (tailnet):     ${TS:+https://$TS/}${TS:-run 'sudo tailscale up', then apply-config.sh}
    Grafana login:           admin / password in $SECRETS (GRAFANA_ADMIN_PASSWORD)
    Collector logs:          journalctl -u serverroom-monitor -f
    Test notifications:      sudo $APP_DIR/scripts/test-alerts.sh
    After config changes:    sudo $APP_DIR/scripts/apply-config.sh

EOF
if [[ ${REBOOT_NEEDED:-0} == 1 || -f /run/reboot-required || $(cfg display.mode) == hdmi ]]; then
    warn "Reboot to activate the hardware watchdog / sensor overlay / kiosk:  sudo reboot"
fi
trap - ERR
