"""DHT11 / DHT22 (AM2302) via the Linux kernel's IIO driver.

Covers cheap single-wire modules such as the KY-015 (a DHT11 on a small
board). The protocol is timing-critical (microsecond pulses), which a Python
process can't do reliably on Linux. The kernel's ``dht11`` driver does it in
interrupt context instead, enabled with the device-tree overlay

    dtoverlay=dht11,gpiopin=4

(install.sh adds this). Readings then appear in sysfs as milli-degrees and
milli-percent::

    /sys/bus/iio/devices/iio:deviceN/name                       "dht11@4"
    /sys/bus/iio/devices/iio:deviceN/in_temp_input              "23000"
    /sys/bus/iio/devices/iio:deviceN/in_humidityrelative_input  "41000"

Individual reads fail fairly often (EIO on a checksum error, ETIMEDOUT when
the sensor doesn't answer); the collector's retry logic handles that. The
sensor needs at least ~1 s between measurements, so this driver spaces its
attempts 2.1 s apart. The kernel caches a good measurement for 2 s, so the
temperature and humidity files read back-to-back come from the same one.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from serverroom_monitor.models import RawReading
from serverroom_monitor.sensors.base import SensorDriver, SensorError

IIO_ROOT = Path("/sys/bus/iio/devices")
MIN_INTERVAL = 2.1  # seconds between measurement attempts


class DhtDriver(SensorDriver):
    def __init__(
        self,
        model: str,
        gpio: int,
        iio_root: Path = IIO_ROOT,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self.gpio = gpio
        self._root = iio_root
        self._clock = clock
        self._sleep = sleep
        self._device: Path | None = None
        self._last_attempt: float | None = None

    def find_device(self) -> Path:
        """Find the IIO device created by the dht11 overlay for our GPIO."""
        candidates = []
        for dev in sorted(self._root.glob("iio:device*")):
            try:
                name = (dev / "name").read_text().strip()
            except OSError:
                continue
            if name.startswith("dht11"):
                candidates.append((name, dev))
        if not candidates:
            raise SensorError(
                f"no DHT IIO device found in {self._root}: is "
                f"'dtoverlay=dht11,gpiopin={self.gpio}' in /boot/firmware/config.txt "
                "(and has the Pi been rebooted)?"
            )
        # The overlay names the node "dht11@<gpio>"; prefer the matching one.
        for name, dev in candidates:
            if name == f"dht11@{self.gpio}":
                return dev
        return candidates[0][1]

    def _read_milli(self, dev: Path, attribute: str) -> float:
        try:
            return int((dev / attribute).read_text().strip()) / 1000.0
        except OSError as exc:
            # EIO: checksum/framing error; ETIMEDOUT: no response. Both are
            # normal now and then for a DHT sensor.
            raise SensorError(f"{self.model} on GPIO{self.gpio}: {exc.strerror or exc}") from exc
        except ValueError as exc:
            raise SensorError(f"{self.model}: unexpected value in {attribute}") from exc

    def read(self) -> RawReading:
        if self._last_attempt is not None:
            wait = MIN_INTERVAL - (self._clock() - self._last_attempt)
            if wait > 0:
                self._sleep(wait)
        self._last_attempt = self._clock()
        if self._device is None:
            self._device = self.find_device()
        temperature = self._read_milli(self._device, "in_temp_input")
        humidity = self._read_milli(self._device, "in_humidityrelative_input")
        return RawReading(temperature=temperature, humidity=min(max(humidity, 0.0), 100.0))

    def reset(self) -> None:
        self._device = None
