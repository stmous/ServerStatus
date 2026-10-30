"""The collector main loop."""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Protocol

from serverroom_monitor import lineprotocol
from serverroom_monitor.buffer import PointBuffer
from serverroom_monitor.config import Config
from serverroom_monitor.display.oled import ScreenState
from serverroom_monitor.heartbeat import Heartbeat
from serverroom_monitor.influx import RejectedWriteError, RetryableWriteError
from serverroom_monitor.levels import classify
from serverroom_monitor.models import RawReading, Reading
from serverroom_monitor.netinfo import TailscaleIP, primary_ipv4
from serverroom_monitor.psychro import dew_point
from serverroom_monitor.sdnotify import SystemdNotifier
from serverroom_monitor.sensors import SensorDriver, SensorError
from serverroom_monitor.validation import ReadingValidator, Verdict

log = logging.getLogger(__name__)

MEASUREMENT = "environment"
STATUS_MEASUREMENT = "collector_status"
# Refuse to write data stamped before this (2025-01-01): the Pi 4 has no RTC,
# so before NTP sync the clock may be wildly wrong.
MIN_SANE_EPOCH = 1_735_689_600
TIMESYNC_DIR = Path("/run/systemd/timesync")


class Writer(Protocol):
    def write(self, lines: Sequence[str]) -> None: ...


class Display(Protocol):
    def update(self, state: ScreenState) -> None: ...
    def close(self) -> None: ...


@dataclass
class CycleResult:
    raw: RawReading | None
    reading: Reading | None
    verdict: Verdict | None
    sensor_ok: bool
    persisted: str  # "written", "buffered", "rejected", "skipped"


class Collector:
    def __init__(
        self,
        cfg: Config,
        driver: SensorDriver,
        writer: Writer,
        buffer: PointBuffer | None,
        *,
        heartbeat: Heartbeat | None = None,
        notifier: SystemdNotifier | None = None,
        display: Display | None = None,
        clock: Callable[[], float] = time.time,
        stop: threading.Event | None = None,
    ) -> None:
        self.cfg = cfg
        self.driver = driver
        self.writer = writer
        self.buffer = buffer
        self.heartbeat = heartbeat
        self.notifier = notifier or SystemdNotifier(environ={})
        self.display = display
        self.clock = clock
        self.stop = stop or threading.Event()
        self.validator = ReadingValidator(cfg.validation)

        self.consecutive_failures = 0
        self.read_errors_total = 0
        self.discarded_total = 0
        self.last_reading: Reading | None = None
        self._influx_ok: bool | None = None
        self._tailscale = TailscaleIP()
        self._cycle = 0

    # -- sensor ---------------------------------------------------------------

    def _read_with_retry(self) -> RawReading | None:
        s = self.cfg.sensor
        for attempt in range(s.read_retries + 1):
            try:
                return self.driver.read()
            except SensorError as exc:
                self.read_errors_total += 1
                if attempt < s.read_retries:
                    delay = s.retry_backoff * (2**attempt)
                    log.debug(
                        "sensor read failed (attempt %d), retrying in %.1fs: %s",
                        attempt + 1,
                        delay,
                        exc,
                    )
                    self.notifier.watchdog()
                    if self.stop.wait(delay):
                        return None
                else:
                    log.warning(
                        "sensor read failed after %d attempt(s): %s",
                        attempt + 1,
                        exc,
                        extra={"fields": {"event": "sensor_error"}},
                    )
        return None

    # -- persistence ----------------------------------------------------------

    def _set_influx_ok(self, ok: bool, detail: str = "") -> None:
        if ok and self._influx_ok is False:
            log.info("InfluxDB reachable again")
        elif not ok and self._influx_ok is not False:
            log.warning("InfluxDB write failed, buffering locally: %s", detail)
        self._influx_ok = ok

    def persist(self, lines: list[str]) -> str:
        buf = self.buffer
        if buf is None:
            try:
                self.writer.write(lines)
            except (RetryableWriteError, RejectedWriteError) as exc:
                log.error("write failed and no buffer configured: %s", exc)
                return "rejected"
            return "written"

        if len(buf) == 0:
            try:
                self.writer.write(lines)
            except RetryableWriteError as exc:
                self._set_influx_ok(False, str(exc))
                buf.append(lines)
                return "buffered"
            except RejectedWriteError as exc:
                log.error("InfluxDB rejected data, dropping: %s", exc)
                return "rejected"
            self._set_influx_ok(True)
            return "written"

        # Backlog exists: queue behind it so points stay in order, then drain.
        buf.append(lines)
        b = self.cfg.buffer
        flushed = buf.flush(self.writer.write, b.flush_batch, b.flush_time_budget)
        self._set_influx_ok(flushed > 0 or len(buf) == 0, "still unreachable")
        return "buffered"

    # -- one cycle ------------------------------------------------------------

    def run_cycle(self) -> CycleResult:
        self._cycle += 1
        self.notifier.watchdog()
        raw = self._read_with_retry()
        now = self.clock()
        reading: Reading | None = None
        verdict: Verdict | None = None

        if raw is None:
            self.consecutive_failures += 1
        else:
            result = self.validator.validate(raw, now)
            verdict = result.verdict
            if result.accepted:
                self.consecutive_failures = 0
                reading = Reading(
                    timestamp=now,
                    temperature=round(raw.temperature, 2),
                    humidity=round(raw.humidity, 2),
                    dew_point=_round(dew_point(raw.temperature, raw.humidity)),
                )
                self.last_reading = reading
                if verdict is Verdict.REBASELINED:
                    log.warning("accepted step change as new baseline: %s", result.detail)
            else:
                self.discarded_total += 1
                log.warning(
                    "discarded reading: %s",
                    result.detail,
                    extra={"fields": {"event": "discarded", "reason": verdict.value}},
                )
                # A sensor that only ever returns garbage is a failing sensor.
                if verdict is Verdict.OUT_OF_BOUNDS:
                    self.consecutive_failures += 1
                else:
                    self.consecutive_failures = 0

        if (
            self.consecutive_failures
            and self.consecutive_failures % self.cfg.sensor.reinit_after == 0
        ):
            log.info("re-initialising sensor after %d failed cycle(s)", self.consecutive_failures)
            self.driver.reset()

        sensor_ok = self.consecutive_failures < self.cfg.sensor.failure_threshold
        persisted = self._write_points(reading, sensor_ok, now)

        if reading is not None:
            log.info(
                "reading %.2fC %.1f%%RH dew point %s",
                reading.temperature,
                reading.humidity,
                f"{reading.dew_point:.2f}C" if reading.dew_point is not None else "n/a",
                extra={
                    "fields": {
                        "event": "reading",
                        "temperature": reading.temperature,
                        "humidity": reading.humidity,
                        "dew_point": reading.dew_point,
                        "persisted": persisted,
                    }
                },
            )

        self._ping_heartbeat(raw, sensor_ok)
        self._update_display(sensor_ok, now)
        self._update_status(reading, sensor_ok)
        return CycleResult(raw, reading, verdict, sensor_ok, persisted)

    def _write_points(self, reading: Reading | None, sensor_ok: bool, now: float) -> str:
        if now < MIN_SANE_EPOCH:
            log.error("system clock is not set (%s); not writing data", time.ctime(now))
            return "skipped"
        ts = int(now)
        tags = {"location": self.cfg.location, "sensor": self.driver.model}
        lines: list[str] = []
        if reading is not None:
            lines.append(
                lineprotocol.encode(
                    MEASUREMENT,
                    tags,
                    {
                        "temperature": reading.temperature,
                        "humidity": reading.humidity,
                        "dew_point": reading.dew_point,
                    },
                    ts,
                )
            )
        lines.append(
            lineprotocol.encode(
                STATUS_MEASUREMENT,
                tags,
                {
                    "sensor_ok": int(sensor_ok),
                    "consecutive_failures": self.consecutive_failures,
                    "read_errors_total": self.read_errors_total,
                    "discarded_total": self.discarded_total,
                    "buffered_points": len(self.buffer) if self.buffer is not None else 0,
                },
                ts,
            )
        )
        return self.persist(lines)

    def _ping_heartbeat(self, raw: RawReading | None, sensor_ok: bool) -> None:
        if self.heartbeat is None or not self.heartbeat.cfg.enabled:
            return
        if raw is not None and sensor_ok:
            self.heartbeat.success()
        elif not sensor_ok:
            self.heartbeat.failure(
                f"sensor failing: {self.consecutive_failures} consecutive failed cycle(s)"
            )

    def _update_display(self, sensor_ok: bool, now: float) -> None:
        if self.display is None:
            return
        ip = primary_ipv4()
        # Alternate between the LAN and Tailscale address every other cycle.
        if self._cycle % 2 == 0 and (ts_ip := self._tailscale.get()):
            ip = f"{ts_ip} (ts)"
        self.display.update(
            ScreenState(
                reading=self.last_reading,
                level=classify(self.last_reading, self.cfg.thresholds),
                sensor_ok=sensor_ok,
                ip=ip,
                now=now,
            )
        )

    def _update_status(self, reading: Reading | None, sensor_ok: bool) -> None:
        if not sensor_ok:
            status = f"SENSOR ERROR ({self.consecutive_failures} failed cycles)"
        elif reading is not None:
            status = f"{reading.temperature:.1f}C {reading.humidity:.1f}%RH"
        else:
            status = "reading discarded"
        if self.buffer is not None and len(self.buffer):
            status += f"; {len(self.buffer)} point(s) buffered"
        self.notifier.status(status)

    # -- main loop ------------------------------------------------------------

    def wait_for_clock(self, timeout: float = 300.0) -> None:
        """Wait (bounded) for NTP sync so data isn't stamped with a stale time."""
        if not TIMESYNC_DIR.exists():
            return  # timesyncd not in use (dev machine, or chrony); trust the clock
        deadline = time.monotonic() + timeout
        logged = False
        while not (TIMESYNC_DIR / "synchronized").exists():
            if time.monotonic() >= deadline:
                log.warning("clock not NTP-synchronised after %.0fs; continuing", timeout)
                return
            if not logged:
                log.info("waiting for NTP time synchronisation")
                self.notifier.status("waiting for NTP time sync")
                logged = True
            self.notifier.watchdog()
            if self.stop.wait(2.0):
                return

    def run_forever(self) -> None:
        interval = self.cfg.sensor.poll_interval
        wd = self.notifier.watchdog_interval
        tick = min(5.0, wd / 3) if wd else 5.0
        self.notifier.ready("starting")
        self.wait_for_clock()
        log.info(
            "collector running: model=%s location=%s interval=%.0fs",
            self.driver.model,
            self.cfg.location,
            interval,
        )
        next_due = time.monotonic()
        while not self.stop.is_set():
            try:
                self.run_cycle()
            except Exception:
                # Never let an unexpected bug kill the loop silently; systemd's
                # watchdog still catches genuine hangs.
                log.exception("unexpected error in collection cycle")
            next_due += interval
            now = time.monotonic()
            next_due = max(next_due, now)
            while not self.stop.is_set():
                remaining = next_due - time.monotonic()
                if remaining <= 0:
                    break
                self.notifier.watchdog()
                self.stop.wait(min(tick, remaining))
        self.notifier.stopping()
        log.info("collector stopping")

    def close(self) -> None:
        for closer in (self.driver.close, getattr(self.display, "close", None)):
            if closer is None:
                continue
            try:
                closer()
            except Exception:
                log.debug("error during shutdown", exc_info=True)
        if self.buffer is not None:
            self.buffer.close()


def install_signal_handlers(stop: threading.Event) -> None:
    def handler(signum: int, _frame: FrameType | None) -> None:
        log.info("received %s, shutting down", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)
