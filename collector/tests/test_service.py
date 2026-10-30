from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from serverroom_monitor.buffer import PointBuffer
from serverroom_monitor.config import Config, HeartbeatConfig
from serverroom_monitor.heartbeat import Heartbeat
from serverroom_monitor.influx import RejectedWriteError, RetryableWriteError
from serverroom_monitor.models import RawReading
from serverroom_monitor.sdnotify import SystemdNotifier
from serverroom_monitor.service import MIN_SANE_EPOCH, Collector
from serverroom_monitor.validation import Verdict

from .conftest import FakeWriter, MockSensor, i2c_error

T0 = float(MIN_SANE_EPOCH + 1_000_000)


class Clock:
    def __init__(self, start: float = T0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class RecordingHeartbeat(Heartbeat):
    def __init__(self) -> None:
        super().__init__(HeartbeatConfig(url="https://hc.example/abc"))
        self.events: list[str] = []

    def _ping(self, url: str, message: str) -> bool:
        self.events.append(url)
        return True


def make(
    cfg: Config,
    sensor: MockSensor,
    writer: FakeWriter,
    *,
    buffer: bool = True,
    clock: Clock | None = None,
    heartbeat: Heartbeat | None = None,
) -> Collector:
    buf = PointBuffer(cfg.buffer.path, cfg.buffer.max_points) if buffer else None
    return Collector(cfg, sensor, writer, buf, heartbeat=heartbeat, clock=clock or Clock())


def test_successful_cycle_writes_environment_and_status(
    cfg: Config, mock_sensor: MockSensor, fake_writer: FakeWriter
) -> None:
    c = make(cfg, mock_sensor, fake_writer)
    result = c.run_cycle()
    assert result.reading is not None
    assert result.reading.dew_point == pytest.approx(9.53, abs=0.02)
    assert result.persisted == "written"
    env, status = fake_writer.lines
    assert env.startswith(
        "environment,location=test-room,sensor=mock temperature=22.0,humidity=45.0,dew_point="
    )
    assert env.endswith(f" {int(T0)}")
    assert "sensor_ok=1i" in status
    assert status.startswith("collector_status,location=test-room,sensor=mock ")


def test_retries_then_succeeds(cfg: Config, fake_writer: FakeWriter) -> None:
    sensor = MockSensor([i2c_error(), i2c_error(), RawReading(23.0, 40.0)])
    c = make(cfg, sensor, fake_writer)
    result = c.run_cycle()
    assert result.reading is not None
    assert result.reading.temperature == 23.0
    assert sensor.reads == 3
    assert c.read_errors_total == 2
    assert c.consecutive_failures == 0


def test_persistent_failure_reports_sensor_error_and_reinits(
    cfg: Config, fake_writer: FakeWriter
) -> None:
    # read_retries=2 -> 3 attempts per cycle; failure_threshold=3; reinit_after=2
    sensor = MockSensor([i2c_error()] * 9)
    c = make(cfg, sensor, fake_writer)
    results = [c.run_cycle() for _ in range(3)]
    assert [r.sensor_ok for r in results] == [True, True, False]
    assert all(r.reading is None for r in results)
    assert sensor.reads == 9
    assert sensor.resets == 1  # after the 2nd failed cycle
    # Status point is still written each cycle, so the alert can fire...
    assert sum("sensor_ok=0i" in line for line in fake_writer.lines) == 1
    assert not any(line.startswith("environment") for line in fake_writer.lines)
    # ...and resolves once the sensor recovers.
    assert c.run_cycle().sensor_ok
    assert fake_writer.lines[-1].count("sensor_ok=1i") == 1


def test_out_of_bounds_counts_as_failure(cfg: Config, fake_writer: FakeWriter) -> None:
    sensor = MockSensor([RawReading(130.0, 50.0)] * 3)
    c = make(cfg, sensor, fake_writer)
    results = [c.run_cycle() for _ in range(3)]
    assert [r.verdict for r in results] == [Verdict.OUT_OF_BOUNDS] * 3
    assert results[-1].sensor_ok is False
    assert c.discarded_total == 3


def test_spike_is_discarded_but_not_a_failure(cfg: Config, fake_writer: FakeWriter) -> None:
    clock = Clock()
    sensor = MockSensor([RawReading(22.0, 45.0), RawReading(40.0, 45.0), RawReading(22.1, 45.0)])
    c = make(cfg, sensor, fake_writer, clock=clock)
    c.run_cycle()
    clock.now += 30
    spike = c.run_cycle()
    assert spike.verdict is Verdict.JUMP
    assert spike.reading is None
    assert spike.sensor_ok
    assert c.consecutive_failures == 0
    clock.now += 30
    assert c.run_cycle().reading is not None
    assert sum(line.startswith("environment") for line in fake_writer.lines) == 2


def test_buffers_while_influx_down_and_flushes_in_order(
    cfg: Config, fake_writer: FakeWriter
) -> None:
    clock = Clock()
    sensor = MockSensor()
    c = make(cfg, sensor, fake_writer, clock=clock)
    fake_writer.fail = RetryableWriteError("connection refused")
    for _ in range(5):
        assert c.run_cycle().persisted == "buffered"
        clock.now += 30
    assert c.buffer is not None
    assert len(c.buffer) == 10  # environment + status per cycle
    assert fake_writer.lines == []

    fake_writer.fail = None
    c.run_cycle()
    assert len(c.buffer) == 0
    assert len(fake_writer.lines) == 12
    timestamps = [int(line.rsplit(" ", 1)[1]) for line in fake_writer.lines]
    assert timestamps == sorted(timestamps)
    # Once drained, writes go direct again (no buffer round-trip).
    clock.now += 30
    assert c.run_cycle().persisted == "written"


def test_buffer_survives_restart(cfg: Config, fake_writer: FakeWriter) -> None:
    fake_writer.fail = RetryableWriteError("down")
    c = make(cfg, MockSensor(), fake_writer)
    c.run_cycle()
    c.close()
    fake_writer.fail = None
    c2 = make(cfg, MockSensor(), fake_writer)
    c2.run_cycle()
    assert len(fake_writer.lines) == 4


def test_rejected_write_is_dropped_not_buffered(cfg: Config, fake_writer: FakeWriter) -> None:
    fake_writer.fail = RejectedWriteError("bad")
    c = make(cfg, MockSensor(), fake_writer)
    assert c.run_cycle().persisted == "rejected"
    assert c.buffer is not None
    assert len(c.buffer) == 0


def test_unsynchronised_clock_skips_writes(cfg: Config, fake_writer: FakeWriter) -> None:
    c = make(cfg, MockSensor(), fake_writer, clock=Clock(1_600_000_000.0))
    assert c.run_cycle().persisted == "skipped"
    assert fake_writer.lines == []


def test_heartbeat_success_and_failure(cfg: Config, fake_writer: FakeWriter) -> None:
    hb = RecordingHeartbeat()
    sensor = MockSensor([RawReading(22.0, 45.0)] + [i2c_error()] * 9)
    c = make(cfg, sensor, fake_writer, heartbeat=hb)
    for _ in range(4):
        c.run_cycle()
    # success, (silence while still under threshold) x2, then /fail
    assert hb.events == ["https://hc.example/abc", "https://hc.example/abc/fail"]


def test_heartbeat_disabled_without_url() -> None:
    hb = Heartbeat(HeartbeatConfig(url=""))
    assert hb.success() is False
    assert hb.failure() is False


def test_run_forever_stops_on_event_and_notifies(
    cfg: Config, fake_writer: FakeWriter, tmp_path: Path
) -> None:
    sock_path = tmp_path / "notify.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(sock_path))
    server.settimeout(2)
    notifier = SystemdNotifier(
        environ={"NOTIFY_SOCKET": str(sock_path), "WATCHDOG_USEC": "3000000"}
    )
    assert notifier.watchdog_interval == 3.0
    stop = threading.Event()
    c = Collector(cfg, MockSensor(), fake_writer, None, notifier=notifier, stop=stop)
    thread = threading.Thread(target=c.run_forever)
    thread.start()
    messages = []
    try:
        while len(messages) < 4:
            messages.append(server.recv(1024).decode())
    finally:
        stop.set()
        thread.join(timeout=5)
        server.close()
    assert not thread.is_alive()
    assert messages[0].startswith("READY=1")
    assert "WATCHDOG=1" in messages
    assert any(m.startswith("STATUS=22.0C") for m in messages)


def test_sdnotify_disabled_without_socket() -> None:
    n = SystemdNotifier(environ={})
    assert not n.enabled
    n.ready()
    n.watchdog()  # no-op, must not raise
