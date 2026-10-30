"""Fake sensor for running the whole stack without hardware (``--simulate``)."""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable

from serverroom_monitor.models import RawReading
from serverroom_monitor.sensors.base import SensorDriver, SensorError

SCENARIOS = ("normal", "overheat", "dry", "humid", "spikes", "flaky", "dead")


class SimulatedDriver(SensorDriver):
    """Generates plausible data, with scenarios to exercise every alert.

    * ``normal``   - 22-24 degC daily cycle, 40-50 %RH, small noise
    * ``overheat`` - temperature climbs 0.5 degC/min until 35 degC (AC failure)
    * ``dry``      - humidity falls 1 %RH/min to 15 %RH
    * ``humid``    - humidity rises 1 %RH/min to 80 %RH
    * ``spikes``   - normal, plus an implausible spike every 10th read
    * ``flaky``    - normal, but 40 % of reads raise an I2C-style error
    * ``dead``     - every read fails (sensor-error alert)

    ``speed`` makes the scenarios evolve faster (e.g. 10 = ten times faster).
    """

    model = "simulated"

    def __init__(
        self,
        scenario: str = "normal",
        seed: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        speed: float = 1.0,
    ) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}; choose from {', '.join(SCENARIOS)}")
        self.scenario = scenario
        self._rng = random.Random(seed)  # noqa: S311 - not used for security
        self._clock = clock
        self._start = clock()
        self._speed = speed
        self._count = 0

    def read(self) -> RawReading:
        self._count += 1
        elapsed_min = (self._clock() - self._start) * self._speed / 60.0
        if self.scenario == "dead":
            raise SensorError("simulated: [Errno 121] Remote I/O error")
        if self.scenario == "flaky" and self._rng.random() < 0.4:
            raise SensorError("simulated: [Errno 121] Remote I/O error")

        day = time.localtime().tm_hour / 24.0
        temperature = 23.0 + math.sin(2 * math.pi * day) * 1.0 + self._rng.gauss(0, 0.05)
        humidity = 45.0 + math.cos(2 * math.pi * day) * 4.0 + self._rng.gauss(0, 0.3)

        if self.scenario == "overheat":
            temperature = min(35.0, temperature + 0.5 * elapsed_min)
        elif self.scenario == "dry":
            humidity = max(15.0, humidity - 1.0 * elapsed_min)
        elif self.scenario == "humid":
            humidity = min(80.0, humidity + 1.0 * elapsed_min)
        elif self.scenario == "spikes" and self._count % 10 == 0:
            temperature += 25.0
        return RawReading(round(temperature, 2), round(min(max(humidity, 0.0), 100.0), 2))
