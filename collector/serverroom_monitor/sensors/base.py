"""Sensor driver interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from serverroom_monitor.models import RawReading


class SensorError(Exception):
    """A read failed in a way that may succeed on retry (I2C NACK, CRC, ...)."""


class SensorDriver(ABC):
    """One driver per supported sensor model.

    Drivers are opened lazily so that a sensor that is missing at boot does
    not crash the service: ``read()`` raises :class:`SensorError` instead and
    the collector keeps retrying (and reporting the error).
    """

    #: Value written to the ``sensor`` tag in InfluxDB.
    model: str = "unknown"

    @abstractmethod
    def read(self) -> RawReading:
        """Return one reading or raise :class:`SensorError`."""

    def reset(self) -> None:  # noqa: B027 - optional hook with a no-op default
        """Drop any open handles so the next ``read()`` re-initialises the bus."""

    def close(self) -> None:
        self.reset()
