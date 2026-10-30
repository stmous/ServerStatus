"""Durable local spool for points that could not be written to InfluxDB.

SQLite (stdlib) is used instead of a flat file because it survives power
loss mid-write without corrupting earlier data, and makes "delete exactly the
rows that were flushed" trivial. The spool is size-capped: when full, the
oldest points are dropped (and logged) so a long outage can never fill the disk.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from serverroom_monitor.influx import RejectedWriteError, RetryableWriteError

log = logging.getLogger(__name__)

WriteFn = Callable[[Sequence[str]], None]


class PointBuffer:
    def __init__(self, path: Path, max_points: int) -> None:
        self.path = path
        self.max_points = max_points
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS points "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, line TEXT NOT NULL)"
        )
        self._count = self._query_count()
        self.dropped_total = 0

    def _query_count(self) -> int:
        row = self._db.execute("SELECT COUNT(*) FROM points").fetchone()
        return int(row[0])

    def __len__(self) -> int:
        return self._count

    def append(self, lines: Sequence[str]) -> None:
        if not lines:
            return
        with self._db:
            self._db.execute("BEGIN")
            self._db.executemany("INSERT INTO points(line) VALUES (?)", [(line,) for line in lines])
        self._count += len(lines)
        overflow = self._count - self.max_points
        if overflow > 0:
            with self._db:
                self._db.execute("BEGIN")
                self._db.execute(
                    "DELETE FROM points WHERE id IN (SELECT id FROM points ORDER BY id LIMIT ?)",
                    (overflow,),
                )
            self._count -= overflow
            self.dropped_total += overflow
            log.warning("buffer full: dropped %d oldest point(s)", overflow)

    def peek(self, limit: int) -> list[tuple[int, str]]:
        rows = self._db.execute("SELECT id, line FROM points ORDER BY id LIMIT ?", (limit,))
        return [(int(i), str(line)) for i, line in rows]

    def delete_through(self, last_id: int) -> None:
        with self._db:
            self._db.execute("BEGIN")
            cur = self._db.execute("DELETE FROM points WHERE id <= ?", (last_id,))
        self._count -= cur.rowcount

    def flush(
        self,
        write: WriteFn,
        batch_size: int,
        time_budget: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> int:
        """Write buffered points oldest-first. Returns the number flushed.

        Stops at the first retryable error (server still down) or when the
        time budget runs out, so a big backlog can't starve the watchdog.
        Batches the server rejects as malformed are discarded.
        """
        deadline = clock() + time_budget
        flushed = 0
        while self._count and clock() < deadline:
            rows = self.peek(batch_size)
            if not rows:
                self._count = 0
                break
            try:
                write([line for _, line in rows])
            except RetryableWriteError:
                break
            except RejectedWriteError as exc:
                log.error(
                    "discarding %d buffered point(s) rejected by InfluxDB: %s", len(rows), exc
                )
            else:
                flushed += len(rows)
            self.delete_through(rows[-1][0])
        if flushed:
            log.info("flushed %d buffered point(s); %d remaining", flushed, self._count)
        return flushed

    def close(self) -> None:
        self._db.close()
