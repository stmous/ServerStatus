"""SSD1306 128x64 OLED status screen.

Layout (128x64)::

    +------------------------------+
    |23.4°C               [ OK ]   |   big temperature + alert badge
    |RH 41.2%   DP 9.6°C           |
    |IP 192.168.1.20               |   alternates with the Tailscale IP
    |Last 14:03:22                 |   or "SENSOR ERROR" / "NO DATA"
    +------------------------------+

OLEDs burn in when they show the same pixels for months. Two mitigations:
the whole frame shifts by up to 2 px each update, and a configurable night
window dims or blanks the panel.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from serverroom_monitor.config import OledConfig
from serverroom_monitor.models import AlertLevel, Reading

log = logging.getLogger(__name__)

_FONT_DIRS = (Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/TTF"))
_SHIFTS = ((0, 0), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))

Font = ImageFont.FreeTypeFont | ImageFont.ImageFont


def _font(name: str, size: int) -> Font:
    for d in _FONT_DIRS:
        path = d / name
        if path.exists():
            return ImageFont.truetype(str(path), size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


@dataclass(frozen=True)
class ScreenState:
    reading: Reading | None
    level: AlertLevel
    sensor_ok: bool
    ip: str | None
    now: float


class OledRenderer:
    """Pure rendering (no hardware), so the layout is unit-testable."""

    def __init__(self, width: int = 128, height: int = 64) -> None:
        self.width = width
        self.height = height
        self._big = _font("DejaVuSans-Bold.ttf", 20)
        self._mid = _font("DejaVuSans.ttf", 11)
        self._small = _font("DejaVuSans.ttf", 10)
        self._badge = _font("DejaVuSans-Bold.ttf", 10)

    def render(self, s: ScreenState, shift: tuple[int, int] = (0, 0)) -> Image.Image:
        img = Image.new("1", (self.width, self.height), 0)
        draw = ImageDraw.Draw(img)
        dx, dy = shift
        x0 = 2 + dx
        r = s.reading

        temp = f"{r.temperature:.1f}°C" if r else "--.-°C"
        draw.text((x0, 0 + dy), temp, font=self._big, fill=1)

        # Alert badge: inverted box, top right.
        label = s.level.value
        bbox = draw.textbbox((0, 0), label, font=self._badge)
        bw, bh = bbox[2] - bbox[0] + 6, bbox[3] - bbox[1] + 4
        bx = self.width - bw - 2 + dx
        by = 3 + dy
        if s.level in (AlertLevel.WARNING, AlertLevel.CRITICAL):
            draw.rectangle((bx, by, bx + bw, by + bh), fill=1)
            draw.text((bx + 3, by + 2 - bbox[1]), label, font=self._badge, fill=0)
        else:
            draw.rectangle((bx, by, bx + bw, by + bh), outline=1)
            draw.text((bx + 3, by + 2 - bbox[1]), label, font=self._badge, fill=1)

        if r:
            dp = f"{r.dew_point:.1f}°" if r.dew_point is not None else "--"
            line2 = f"RH {r.humidity:.1f}%  DP {dp}"
        else:
            line2 = "RH --.-%"
        draw.text((x0, 25 + dy), line2, font=self._mid, fill=1)
        draw.text((x0, 39 + dy), f"IP {s.ip or 'no network'}", font=self._small, fill=1)

        if not s.sensor_ok:
            line4 = "SENSOR ERROR"
        elif r is None:
            line4 = "NO DATA YET"
        else:
            age = s.now - r.timestamp
            stamp = time.strftime("%H:%M:%S", time.localtime(r.timestamp))
            line4 = f"Last {stamp}" + (f" ({int(age // 60)}m ago)" if age >= 120 else "")
        draw.text((x0, 51 + dy), line4, font=self._small, fill=1)
        return img


class OledDisplay:
    """Drives the physical SSD1306. All hardware errors are contained here:
    a dead OLED must never stop data collection."""

    def __init__(self, cfg: OledConfig) -> None:
        self.cfg = cfg
        self.renderer = OledRenderer(cfg.width, cfg.height)
        self._dev: Any = None
        self._frame = 0
        self._failures = 0
        self._dimmed: bool | None = None

    def _open(self) -> Any:
        import adafruit_ssd1306  # noqa: PLC0415

        from serverroom_monitor.sensors.adafruit import shared_i2c  # noqa: PLC0415

        dev = adafruit_ssd1306.SSD1306_I2C(
            self.cfg.width, self.cfg.height, shared_i2c(), addr=self.cfg.i2c_address
        )
        dev.rotation = self.cfg.rotate
        dev.contrast(self.cfg.contrast)
        self._dimmed = None
        return dev

    def _night(self, now: float) -> bool:
        if self.cfg.night is None:
            return False
        lt = time.localtime(now)
        return self.cfg.night.contains(lt.tm_hour * 60 + lt.tm_min)

    def update(self, state: ScreenState) -> None:
        try:
            if self._dev is None:
                self._dev = self._open()
            night = self._night(state.now)
            if night and self.cfg.night_mode == "off":
                if self._dimmed is not True:
                    self._dev.poweroff()
                    self._dimmed = True
                return
            if self._dimmed is not night:
                self._dev.poweron()
                self._dev.contrast(1 if night else self.cfg.contrast)
                self._dimmed = night
            shift = _SHIFTS[self._frame % len(_SHIFTS)] if self.cfg.pixel_shift else (0, 0)
            self._frame += 1
            self._dev.image(self.renderer.render(state, shift))
            self._dev.show()
            if self._failures:
                log.info("OLED recovered after %d failure(s)", self._failures)
            self._failures = 0
        except Exception as exc:
            self._dev = None
            self._failures += 1
            if self._failures in (1, 10) or self._failures % 100 == 0:
                log.warning("OLED update failed (%d in a row): %s", self._failures, exc)

    def close(self) -> None:
        if self._dev is not None:
            try:
                self._dev.fill(0)
                self._dev.show()
            except Exception:
                log.debug("OLED clear on shutdown failed", exc_info=True)
