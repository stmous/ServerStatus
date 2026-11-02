"""Drivers backed by Adafruit CircuitPython libraries (via Blinka).

The hardware imports happen inside :meth:`_open` so the rest of the package
(and the tests) work on machines without I2C or Blinka.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from typing import Any

from serverroom_monitor.models import RawReading
from serverroom_monitor.sensors.base import SensorDriver, SensorError

log = logging.getLogger(__name__)

# Blinka >= 10 checks its "platform dependencies" on `import board` and, on a
# terminal, interactively offers to pip-install PWM/NeoPixel backends we never
# use. The one it really needs on a Pi 4 (gpiod 2.x) is pinned in
# requirements.txt, so skip the check: it must never prompt or block a service.
os.environ.setdefault("BLINKA_SKIP_DEPENDENCY_CHECK", "1")

# One shared bus object per process: the OLED and the sensor sit on the same
# bus, and Blinka's busio.I2C locking is per object.
_bus_lock = threading.Lock()
_bus: Any = None


def shared_i2c() -> Any:
    """Return the process-wide ``busio.I2C`` instance (created on first use)."""
    global _bus  # noqa: PLW0603 - deliberate process-wide singleton
    with _bus_lock:
        if _bus is None:
            import board  # noqa: PLC0415 - hardware import is intentionally lazy
            import busio  # noqa: PLC0415

            _bus = busio.I2C(board.SCL, board.SDA)
        return _bus


def reset_shared_i2c() -> None:
    global _bus  # noqa: PLW0603
    with _bus_lock:
        if _bus is not None:
            try:
                _bus.deinit()
            except Exception:
                log.debug("ignoring error while closing I2C bus", exc_info=True)
            _bus = None


class _AdafruitDriver(SensorDriver):
    def __init__(self, address: int) -> None:
        self.address = address
        self._dev: Any = None

    def _open(self) -> Any:
        raise NotImplementedError

    def _measure(self, dev: Any) -> tuple[float, float]:
        raise NotImplementedError

    def read(self) -> RawReading:
        try:
            if self._dev is None:
                self._dev = self._open()
                log.info("opened %s at 0x%02x", self.model, self.address)
            temperature, humidity = self._measure(self._dev)
        except (OSError, RuntimeError, ValueError) as exc:
            # OSError: NACK / remote I/O error (errno 121); RuntimeError and
            # ValueError: CRC mismatch or "no sensor found" from the libraries.
            self._dev = None
            raise SensorError(f"{self.model}@0x{self.address:02x}: {exc}") from exc
        if not (math.isfinite(temperature) and math.isfinite(humidity)):
            raise SensorError(f"{self.model}: non-finite value {temperature!r}/{humidity!r}")
        return RawReading(temperature=float(temperature), humidity=float(humidity))

    def reset(self) -> None:
        self._dev = None
        reset_shared_i2c()


class SHT4xDriver(_AdafruitDriver):
    """Sensirion SHT40/41/45 (0x44 default, 0x45/0x46 variants)."""

    model = "sht4x"

    def _open(self) -> Any:
        import adafruit_sht4x  # noqa: PLC0415

        dev = adafruit_sht4x.SHT4x(shared_i2c(), address=self.address)
        dev.mode = adafruit_sht4x.Mode.NOHEAT_HIGHPRECISION
        return dev

    def _measure(self, dev: Any) -> tuple[float, float]:
        t, h = dev.measurements
        # The SHT4x can report slightly outside 0..100 %RH by design; the
        # datasheet says to clamp.
        return float(t), min(max(float(h), 0.0), 100.0)


class SHT31Driver(_AdafruitDriver):
    """Sensirion SHT30/31/35 (0x44 or 0x45)."""

    model = "sht31"

    def _open(self) -> Any:
        import adafruit_sht31d  # noqa: PLC0415

        dev = adafruit_sht31d.SHT31D(shared_i2c(), address=self.address)
        dev.heater = False
        return dev

    def _measure(self, dev: Any) -> tuple[float, float]:
        return float(dev.temperature), float(dev.relative_humidity)


class BME280Driver(_AdafruitDriver):
    """Bosch BME280 (0x76 or 0x77). Note: BMP280 has no humidity sensor."""

    model = "bme280"

    def _open(self) -> Any:
        from adafruit_bme280 import basic  # noqa: PLC0415

        return basic.Adafruit_BME280_I2C(shared_i2c(), address=self.address)

    def _measure(self, dev: Any) -> tuple[float, float]:
        return float(dev.temperature), float(dev.relative_humidity)
