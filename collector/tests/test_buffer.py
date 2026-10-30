from collections.abc import Sequence
from pathlib import Path

from serverroom_monitor.buffer import PointBuffer
from serverroom_monitor.influx import RejectedWriteError, RetryableWriteError

from .conftest import FakeWriter


def lines(n: int, start: int = 0) -> list[str]:
    return [f"m v={i}i {i}" for i in range(start, start + n)]


def test_append_and_count_persist_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "buf.sqlite"
    buf = PointBuffer(path, max_points=100)
    buf.append(lines(5))
    assert len(buf) == 5
    buf.close()
    reopened = PointBuffer(path, max_points=100)
    assert len(reopened) == 5
    assert [line for _, line in reopened.peek(10)] == lines(5)


def test_flush_writes_in_order_and_empties(tmp_path: Path) -> None:
    buf = PointBuffer(tmp_path / "b.sqlite", 1000)
    buf.append(lines(25))
    writer = FakeWriter()
    assert buf.flush(writer.write, batch_size=10, time_budget=10) == 25
    assert writer.lines == lines(25)
    assert writer.calls == 3
    assert len(buf) == 0


def test_flush_stops_on_retryable_error_and_keeps_data(tmp_path: Path) -> None:
    buf = PointBuffer(tmp_path / "b.sqlite", 1000)
    buf.append(lines(10))
    writer = FakeWriter()
    writer.fail = RetryableWriteError("down")
    assert buf.flush(writer.write, batch_size=4, time_budget=10) == 0
    assert len(buf) == 10
    assert writer.calls == 1  # gave up immediately, no hammering


def test_flush_partial_outage(tmp_path: Path) -> None:
    buf = PointBuffer(tmp_path / "b.sqlite", 1000)
    buf.append(lines(10))
    calls = 0

    def flaky(batch: Sequence[str]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RetryableWriteError("blip")

    assert buf.flush(flaky, batch_size=4, time_budget=10) == 4
    assert len(buf) == 6
    assert [line for _, line in buf.peek(1)] == lines(1, start=4)


def test_rejected_batches_are_discarded(tmp_path: Path) -> None:
    buf = PointBuffer(tmp_path / "b.sqlite", 1000)
    buf.append(lines(8))
    seen: list[list[str]] = []

    def picky(batch: Sequence[str]) -> None:
        seen.append(list(batch))
        if len(seen) == 1:
            raise RejectedWriteError("bad line")

    assert buf.flush(picky, batch_size=4, time_budget=10) == 4
    assert len(buf) == 0
    assert len(seen) == 2


def test_capacity_drops_oldest(tmp_path: Path) -> None:
    buf = PointBuffer(tmp_path / "b.sqlite", max_points=10)
    buf.append(lines(8))
    buf.append(lines(5, start=8))
    assert len(buf) == 10
    assert buf.dropped_total == 3
    assert [line for _, line in buf.peek(1)] == lines(1, start=3)


def test_time_budget_limits_flush(tmp_path: Path) -> None:
    buf = PointBuffer(tmp_path / "b.sqlite", 1000)
    buf.append(lines(100))
    ticks = iter(range(1000))
    writer = FakeWriter()
    flushed = buf.flush(
        writer.write, batch_size=10, time_budget=3, clock=lambda: float(next(ticks))
    )
    assert 0 < flushed < 100
    assert len(buf) == 100 - flushed
