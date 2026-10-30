"""Backlight control for DSI touch displays (official 7" v1 and Touch Display 2).

Run every few minutes by ``serverroom-backlight.timer`` (as root): sets the
day brightness, or the night brightness inside the configured night window.
HDMI monitors have no sysfs backlight; then this is a no-op.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from serverroom_monitor.config import DisplayConfig

log = logging.getLogger(__name__)

SYSFS = Path("/sys/class/backlight")


def target_percent(cfg: DisplayConfig, now: float) -> int:
    h = cfg.hdmi
    if h.night is not None:
        lt = time.localtime(now)
        if h.night.contains(lt.tm_hour * 60 + lt.tm_min):
            return h.night_brightness
    return h.brightness


def apply_backlight(cfg: DisplayConfig, sysfs: Path = SYSFS, now: float | None = None) -> int:
    """Set every backlight device to the target level. Returns a process exit code."""
    if cfg.mode != "hdmi":
        return 0
    devices = sorted(p for p in sysfs.glob("*") if (p / "max_brightness").exists())
    if not devices:
        log.info("no backlight device found (HDMI monitor?); nothing to do")
        return 0
    pct = target_percent(cfg, time.time() if now is None else now)
    rc = 0
    for dev in devices:
        try:
            max_b = int((dev / "max_brightness").read_text().strip())
            value = round(max_b * pct / 100)
            # Never fully dark via brightness=0 unless asked for 0 %.
            if pct > 0:
                value = max(value, 1)
            current = (dev / "brightness").read_text().strip()
            if current != str(value):
                (dev / "brightness").write_text(f"{value}\n")
                log.info("backlight %s: %s -> %d (%d%%)", dev.name, current, value, pct)
            bl_power = dev / "bl_power"
            if bl_power.exists():  # 0 = on, 4 = off (FB_BLANK_POWERDOWN)
                want = "4" if pct == 0 else "0"
                if bl_power.read_text().strip() != want:
                    bl_power.write_text(f"{want}\n")
        except (OSError, ValueError) as exc:
            log.error("cannot set backlight %s: %s", dev.name, exc)
            rc = 1
    return rc
