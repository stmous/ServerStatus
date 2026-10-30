"""Reading validation: physical bounds and jump filtering with re-baselining.

A naive "reject anything that differs from the last value by more than X"
filter has a nasty failure mode: after a long outage, a restart, or a *real*
fast change (AC failure, door left open) every new reading differs from the
stale baseline, so the filter rejects good data forever and the monitor goes
blind exactly when it matters. This implementation avoids that by:

1. only applying the jump check when the last accepted reading is recent
   (``jump_window``); older baselines are ignored;
2. tracking consecutive rejected readings and accepting them as the new
   baseline once ``rebaseline_after`` of them agree with *each other*.

So a single-sample glitch is dropped, but a genuine step change is accepted
after a few polls.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from serverroom_monitor.config import ValidationConfig
from serverroom_monitor.models import RawReading


class Verdict(Enum):
    ACCEPTED = "accepted"
    REBASELINED = "rebaselined"  # accepted as a new baseline after a confirmed step change
    OUT_OF_BOUNDS = "out_of_bounds"
    JUMP = "jump"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    verdict: Verdict
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.verdict in (Verdict.ACCEPTED, Verdict.REBASELINED)


@dataclass(frozen=True, slots=True)
class _Sample:
    timestamp: float
    reading: RawReading


class ReadingValidator:
    def __init__(self, cfg: ValidationConfig) -> None:
        self.cfg = cfg
        self._last: _Sample | None = None
        self._candidates: list[_Sample] = []

    @property
    def last_accepted(self) -> RawReading | None:
        return self._last.reading if self._last else None

    def check_bounds(self, r: RawReading) -> str | None:
        """Return a reason string when the reading is physically implausible."""
        c = self.cfg
        if not (math.isfinite(r.temperature) and math.isfinite(r.humidity)):
            return "non-finite value"
        if not c.temperature_min <= r.temperature <= c.temperature_max:
            return (
                f"temperature {r.temperature:.2f} outside "
                f"[{c.temperature_min:g}, {c.temperature_max:g}]"
            )
        if not c.humidity_min <= r.humidity <= c.humidity_max:
            return f"humidity {r.humidity:.2f} outside [{c.humidity_min:g}, {c.humidity_max:g}]"
        return None

    def _close(self, a: RawReading, b: RawReading) -> bool:
        return (
            abs(a.temperature - b.temperature) <= self.cfg.max_temperature_jump
            and abs(a.humidity - b.humidity) <= self.cfg.max_humidity_jump
        )

    def validate(self, r: RawReading, timestamp: float) -> ValidationResult:
        if reason := self.check_bounds(r):
            # Out-of-bounds values are garbage, not a step change: they never
            # count towards re-baselining.
            return ValidationResult(Verdict.OUT_OF_BOUNDS, reason)

        sample = _Sample(timestamp, r)
        last = self._last
        if last is None or timestamp - last.timestamp > self.cfg.jump_window:
            return self._accept(sample, Verdict.ACCEPTED)
        if self._close(last.reading, r):
            return self._accept(sample, Verdict.ACCEPTED)

        # Jump. Keep the run of mutually consistent candidates.
        if self._candidates and not self._close(self._candidates[-1].reading, r):
            self._candidates = []
        self._candidates.append(sample)
        detail = (
            f"jump from {last.reading.temperature:.2f}C/{last.reading.humidity:.1f}% "
            f"to {r.temperature:.2f}C/{r.humidity:.1f}%"
        )
        if len(self._candidates) >= self.cfg.rebaseline_after:
            return self._accept(sample, Verdict.REBASELINED, detail)
        return ValidationResult(
            Verdict.JUMP,
            f"{detail} ({len(self._candidates)}/{self.cfg.rebaseline_after} to re-baseline)",
        )

    def _accept(self, sample: _Sample, verdict: Verdict, detail: str = "") -> ValidationResult:
        self._last = sample
        self._candidates = []
        return ValidationResult(verdict, detail)
