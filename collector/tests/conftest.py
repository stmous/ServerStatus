"""Shared fixtures: a scriptable mock sensor and a fake InfluxDB writer."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

from serverroom_monitor.config import Config, parse_config
from serverroom_monitor.influx import RejectedWriteError, RetryableWriteError
from serverroom_monitor.models import RawReading
from serverroom_monitor.sensors.base import SensorDriver, SensorError

REPO = Path(__file__).resolve().parents[2]


class MockSensor(SensorDriver):
    """Returns scripted readings; an Exception in the script is raised instead."""

    model = "mock"

    def __init__(self, script: Iterable[RawReading | Exception] = ()) -> None:
        self.script = list(script)
        self.default = RawReading(22.0, 45.0)
        self.reads = 0
        self.resets = 0

    def read(self) -> RawReading:
        self.reads += 1
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return self.default

    def reset(self) -> None:
        self.resets += 1


class FakeWriter:
    """Records writes; set ``fail`` to a WriteError instance to simulate outages."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.calls = 0
        self.fail: RetryableWriteError | RejectedWriteError | None = None

    def write(self, lines: Sequence[str]) -> None:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        self.lines.extend(lines)


def i2c_error() -> SensorError:
    return SensorError("[Errno 121] Remote I/O error")


@pytest.fixture
def mock_sensor() -> MockSensor:
    return MockSensor()


@pytest.fixture
def fake_writer() -> FakeWriter:
    return FakeWriter()


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return parse_config(
        {
            "location": "test-room",
            "sensor": {"model": "sht4x", "retry_backoff": 0, "read_retries": 2},
            "buffer": {"path": str(tmp_path / "buffer.sqlite"), "max_points": 1000},
        },
        env={"INFLUX_WRITE_TOKEN": "tok"},
    )
