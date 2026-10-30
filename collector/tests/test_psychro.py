import itertools
import math

import pytest

from serverroom_monitor.psychro import dew_point


@pytest.mark.parametrize(
    ("temp", "rh", "expected"),
    [
        # Reference values (Magnus formula / NOAA calculator), within 0.1 degC.
        (20.0, 50.0, 9.26),
        (25.0, 60.0, 16.69),
        (22.0, 40.0, 7.79),
        (30.0, 80.0, 26.17),
        (0.0, 100.0, 0.0),
        (-10.0, 50.0, -18.4),
    ],
)
def test_dew_point_reference_values(temp: float, rh: float, expected: float) -> None:
    dp = dew_point(temp, rh)
    assert dp is not None
    assert dp == pytest.approx(expected, abs=0.15)


def test_dew_point_equals_temperature_at_saturation() -> None:
    for t in (-5.0, 10.0, 23.5, 40.0):
        assert dew_point(t, 100.0) == pytest.approx(t, abs=1e-9)


def test_dew_point_never_exceeds_temperature() -> None:
    for t in range(-20, 61, 5):
        for rh in range(1, 101, 3):
            dp = dew_point(float(t), float(rh))
            assert dp is not None
            assert dp <= t + 1e-9


def test_dew_point_monotonic_in_humidity() -> None:
    values = [dew_point(24.0, float(rh)) for rh in range(5, 101, 5)]
    assert all(a is not None and b is not None and a < b for a, b in itertools.pairwise(values))


def test_dew_point_undefined_for_zero_humidity() -> None:
    assert dew_point(22.0, 0.0) is None
    assert dew_point(22.0, -1.0) is None
    assert dew_point(math.nan, 50.0) is None


def test_dew_point_clamps_supersaturation() -> None:
    assert dew_point(20.0, 100.4) == pytest.approx(20.0)
