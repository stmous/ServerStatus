import math

import pytest

from serverroom_monitor.config import ValidationConfig
from serverroom_monitor.models import RawReading
from serverroom_monitor.validation import ReadingValidator, Verdict


@pytest.fixture
def v() -> ReadingValidator:
    return ReadingValidator(
        ValidationConfig(
            max_temperature_jump=3.0, max_humidity_jump=10.0, jump_window=300, rebaseline_after=3
        )
    )


@pytest.mark.parametrize(
    "reading",
    [
        RawReading(-20.1, 50),
        RawReading(80.1, 50),
        RawReading(22, -0.1),
        RawReading(22, 100.1),
        RawReading(math.nan, 50),
        RawReading(22, math.inf),
    ],
)
def test_rejects_out_of_bounds(v: ReadingValidator, reading: RawReading) -> None:
    assert v.validate(reading, 0).verdict is Verdict.OUT_OF_BOUNDS


@pytest.mark.parametrize("reading", [RawReading(-20, 0), RawReading(80, 100), RawReading(22, 45)])
def test_accepts_bounds_inclusive(v: ReadingValidator, reading: RawReading) -> None:
    assert v.validate(reading, 0).accepted


def test_first_reading_always_accepted(v: ReadingValidator) -> None:
    assert v.validate(RawReading(35.0, 90.0), 0).verdict is Verdict.ACCEPTED


def test_small_changes_accepted(v: ReadingValidator) -> None:
    v.validate(RawReading(22.0, 45.0), 0)
    assert v.validate(RawReading(24.9, 54.9), 30).accepted
    assert v.validate(RawReading(22.0, 45.0), 60).accepted


def test_single_spike_discarded(v: ReadingValidator) -> None:
    v.validate(RawReading(22.0, 45.0), 0)
    assert v.validate(RawReading(47.0, 45.0), 30).verdict is Verdict.JUMP
    # Back to normal: accepted and the spike never became the baseline.
    assert v.validate(RawReading(22.1, 45.0), 60).verdict is Verdict.ACCEPTED
    assert v.last_accepted == RawReading(22.1, 45.0)


def test_humidity_jump_discarded(v: ReadingValidator) -> None:
    v.validate(RawReading(22.0, 45.0), 0)
    assert v.validate(RawReading(22.0, 60.0), 30).verdict is Verdict.JUMP


def test_genuine_step_change_rebaselines(v: ReadingValidator) -> None:
    v.validate(RawReading(22.0, 45.0), 0)
    assert v.validate(RawReading(28.0, 45.0), 30).verdict is Verdict.JUMP
    assert v.validate(RawReading(28.2, 45.0), 60).verdict is Verdict.JUMP
    result = v.validate(RawReading(28.4, 45.0), 90)
    assert result.verdict is Verdict.REBASELINED
    assert v.last_accepted == RawReading(28.4, 45.0)
    # And subsequent readings near the new level are plain accepts.
    assert v.validate(RawReading(28.5, 45.0), 120).verdict is Verdict.ACCEPTED


def test_inconsistent_spikes_never_rebaseline(v: ReadingValidator) -> None:
    v.validate(RawReading(22.0, 45.0), 0)
    t = 0
    for temp in (40.0, 60.0, 40.0, 60.0, 40.0, 60.0):
        t += 30
        assert v.validate(RawReading(temp, 45.0), t).verdict is Verdict.JUMP
    assert v.last_accepted == RawReading(22.0, 45.0)


def test_stale_baseline_ignored(v: ReadingValidator) -> None:
    v.validate(RawReading(22.0, 45.0), 0)
    # After an outage longer than jump_window, any plausible value is accepted.
    assert v.validate(RawReading(31.0, 70.0), 301).verdict is Verdict.ACCEPTED


def test_out_of_bounds_does_not_count_towards_rebaseline(v: ReadingValidator) -> None:
    v.validate(RawReading(22.0, 45.0), 0)
    v.validate(RawReading(28.0, 45.0), 30)
    v.validate(RawReading(120.0, 45.0), 60)  # garbage in between
    v.validate(RawReading(28.0, 45.0), 90)
    assert v.validate(RawReading(28.0, 45.0), 120).verdict is Verdict.REBASELINED


def test_rebaseline_after_one_accepts_immediately() -> None:
    v = ReadingValidator(ValidationConfig(rebaseline_after=1))
    v.validate(RawReading(22.0, 45.0), 0)
    assert v.validate(RawReading(30.0, 45.0), 30).verdict is Verdict.REBASELINED
