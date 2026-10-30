#!/usr/bin/env bash
# Configure (or remove) the local HDMI/DSI kiosk for the given display mode.
# Called by apply-config.sh; idempotent.
#
#   hdmi  -> Chromium kiosk via cage (Lite) or the desktop autostart (Desktop)
#   oled  -> kiosk disabled (the collector drives the OLED)
#   none  -> kiosk disabled
set -Eeuo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/lib.sh
. "$HERE/lib.sh"
require_root

MODE=${1:?usage: setup-kiosk.sh hdmi|oled|none}
LIGHTDM_CONF=/etc/lightdm/lightdm.conf
AUTOSTART=/home/$KIOSK_USER/.config/autostart/serverroom-kiosk.desktop

disable_kiosk() {
    systemctl disable --now serverroom-kiosk.service >/dev/null 2>&1 || true
    systemctl disable --now serverroom-backlight.timer >/dev/null 2>&1 || true
    rm -f "$AUTOSTART"
    if [[ -f $LIGHTDM_CONF ]] && grep -q "^autologin-user=$KIOSK_USER$" "$LIGHTDM_CONF"; then
        sed -i "s/^autologin-user=$KIOSK_USER$/#autologin-user=/" "$LIGHTDM_CONF"
    fi
}

if [[ $MODE != hdmi ]]; then
    disable_kiosk
    ok "kiosk disabled"
    exit 0
fi

# Desktop image = lightdm present. "auto" picks the desktop there, cage on Lite.
SESSION=$(cfg display.hdmi.session)
if [[ $SESSION == auto ]]; then
    if dpkg-query -W -f='${Status}' lightdm 2>/dev/null | grep -q "ok installed"; then
        SESSION=desktop
    else
        SESSION=cage
    fi
fi

# --- packages ---------------------------------------------------------------------------
# Raspberry Pi OS renamed chromium-browser to chromium in 2025; accept either.
if ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null; then
    info "installing chromium"
    apt-get install -y --no-install-recommends chromium >/dev/null 2>&1 ||
        apt-get install -y --no-install-recommends chromium-browser >/dev/null ||
        die "could not install Chromium"
fi
pkgs=(curl)
if [[ $SESSION == cage ]]; then
    pkgs+=(cage wlr-randr swayidle)
else
    pkgs+=(unclutter)
fi
missing=()
for p in "${pkgs[@]}"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "ok installed" || missing+=("$p")
done
if ((${#missing[@]})); then
    info "installing ${missing[*]}"
    apt-get install -y --no-install-recommends "${missing[@]}" >/dev/null || die "package installation failed"
fi

# --- kiosk user (unprivileged, no password, no sudo) ----------------------------------------
if ! id "$KIOSK_USER" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash --comment "Server room kiosk" "$KIOSK_USER"
    passwd -l "$KIOSK_USER" >/dev/null
fi
for g in video render input audio; do
    getent group "$g" >/dev/null && usermod -aG "$g" "$KIOSK_USER"
done

# Invisible cursor theme (Wayland has no unclutter).
python3 "$APP_DIR/kiosk/make-blank-cursor.py" /usr/share/icons >/dev/null

if [[ $SESSION == cage ]]; then
    rm -f "$AUTOSTART"
    systemctl set-default graphical.target >/dev/null
    systemctl enable serverroom-kiosk.service >/dev/null 2>&1
    systemctl restart serverroom-kiosk.service
    ok "kiosk: cage on tty1 (serverroom-kiosk.service)"
else
    systemctl disable --now serverroom-kiosk.service >/dev/null 2>&1 || true
    install -d -o "$KIOSK_USER" -g "$KIOSK_USER" "/home/$KIOSK_USER/.config" \
        "/home/$KIOSK_USER/.config/autostart"
    install -m 644 -o "$KIOSK_USER" -g "$KIOSK_USER" "$APP_DIR/kiosk/serverroom-kiosk.desktop" "$AUTOSTART"
    # Graphical boot with autologin as the kiosk user.
    systemctl set-default graphical.target >/dev/null
    if [[ -f $LIGHTDM_CONF ]]; then
        if grep -qE '^#?autologin-user=' "$LIGHTDM_CONF"; then
            sed -i -E "s/^#?autologin-user=.*/autologin-user=$KIOSK_USER/" "$LIGHTDM_CONF"
        else
            sed -i "/^\[Seat:\*\]/a autologin-user=$KIOSK_USER" "$LIGHTDM_CONF"
        fi
    fi
    # Desktop screen blanking is managed by raspi-config (0 = enable, 1 = disable).
    if command -v raspi-config >/dev/null; then
        if [[ $(cfg display.hdmi.screen_blanking) == 1 ]]; then
            raspi-config nonint do_blanking 0 || true
        else
            raspi-config nonint do_blanking 1 || true
        fi
    fi
    ok "kiosk: desktop autostart for user '$KIOSK_USER' (takes effect after reboot)"
fi

systemctl enable --now serverroom-backlight.timer >/dev/null 2>&1 || true
