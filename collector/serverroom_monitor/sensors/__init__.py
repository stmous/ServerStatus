"""Sensor abstraction layer: one driver per supported model."""

from __future__ import annotations

from serverroom_monitor.config import SensorConfig
from serverroom_monitor.sensors.base import SensorDriver, SensorError

__all__ = ["SensorDriver", "SensorError", "create_driver"]


def create_driver(
    cfg: SensorConfig, simulate: str | None = None, sim_speed: float = 1.0
) -> SensorDriver:
    """Return the driver for ``cfg.model`` (or a simulator when requested)."""
    if simulate is not None or cfg.model == "simulated":
        from serverroom_monitor.sensors.simulated import SimulatedDriver  # noqa: PLC0415

        return SimulatedDriver(scenario=simulate or "normal", speed=sim_speed)

    from serverroom_monitor.sensors import adafruit  # noqa: PLC0415

    drivers: dict[str, type[adafruit._AdafruitDriver]] = {
        "sht4x": adafruit.SHT4xDriver,
        "sht31": adafruit.SHT31Driver,
        "bme280": adafruit.BME280Driver,
    }
    return drivers[cfg.model](cfg.i2c_address)
