from __future__ import annotations

import errno
from pathlib import Path

import pytest

from serverroom_monitor.config import ConfigError, parse_config
from serverroom_monitor.sensors import create_driver
from serverroom_monitor.sensors.base import SensorError
from serverroom_monitor.sensors.dht import MIN_INTERVAL, DhtDriver


def iio_device(root: Path, n: int, name: str, temp: str = "23000", hum: str = "41000") -> Path:
    dev = root / f"iio:device{n}"
    dev.mkdir(parents=True)
    (dev / "name").write_text(name + "\n")
    (dev / "in_temp_input").write_text(temp + "\n")
    (dev / "in_humidityrelative_input").write_text(hum + "\n")
    return dev


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def driver(root: Path, gpio: int = 4, clock: FakeClock | None = None) -> DhtDriver:
    c = clock or FakeClock()
    return DhtDriver("dht11", gpio, iio_root=root, clock=c, sleep=c.sleep)


def test_reads_milli_units(tmp_path: Path) -> None:
    iio_device(tmp_path, 0, "dht11@4", temp="23400", hum="41000")
    r = driver(tmp_path).read()
    assert (r.temperature, r.humidity) == (23.4, 41.0)


def test_picks_device_matching_gpio(tmp_path: Path) -> None:
    iio_device(tmp_path, 0, "some-adc")
    iio_device(tmp_path, 1, "dht11@17", temp="20000")
    iio_device(tmp_path, 2, "dht11@4", temp="25000")
    assert driver(tmp_path, gpio=17).read().temperature == 20.0
    assert driver(tmp_path, gpio=4).read().temperature == 25.0


def test_missing_overlay_explains_fix(tmp_path: Path) -> None:
    iio_device(tmp_path, 0, "some-adc")
    with pytest.raises(SensorError, match="dtoverlay=dht11,gpiopin=4"):
        driver(tmp_path).read()


def test_io_errors_become_sensor_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    iio_device(tmp_path, 0, "dht11@4")
    d = driver(tmp_path)

    def fail(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "in_temp_input":
            raise OSError(errno.EIO, "Input/output error")
        return "dht11@4"

    monkeypatch.setattr(Path, "read_text", fail)
    with pytest.raises(SensorError, match="Input/output error"):
        d.read()


def test_garbage_value(tmp_path: Path) -> None:
    iio_device(tmp_path, 0, "dht11@4", temp="abc")
    with pytest.raises(SensorError, match="unexpected value"):
        driver(tmp_path).read()


def test_attempts_are_spaced(tmp_path: Path) -> None:
    iio_device(tmp_path, 0, "dht11@4")
    clock = FakeClock()
    d = driver(tmp_path, clock=clock)
    d.read()
    clock.now += 0.5
    d.read()
    assert clock.slept == [pytest.approx(MIN_INTERVAL - 0.5)]
    clock.now += 30
    d.read()
    assert len(clock.slept) == 1  # no wait after a normal poll interval


def test_config_and_factory() -> None:
    cfg = parse_config({"sensor": {"model": "dht11", "gpio": 17}}, env={})
    assert cfg.sensor.gpio == 17
    d = create_driver(cfg.sensor)
    assert isinstance(d, DhtDriver)
    assert (d.model, d.gpio) == ("dht11", 17)
    assert parse_config({"sensor": {"model": "dht22"}}, env={}).sensor.gpio == 4


@pytest.mark.parametrize("gpio", [2, 3, 28, -1])
def test_gpio_out_of_range(gpio: int) -> None:
    with pytest.raises(ConfigError, match=r"sensor\.gpio"):
        parse_config({"sensor": {"model": "dht11", "gpio": gpio}}, env={})
