"""Psychrometric helpers."""

from __future__ import annotations

import math

# Magnus-Tetens coefficients over water (Sonntag 1990), accurate to about
# +/-0.35 degC between -45 and +60 degC, which covers any server room.
_A = 17.62
_B = 243.12


def dew_point(temperature_c: float, humidity_pct: float) -> float | None:
    """Return the dew point in degC, or None when it is undefined (RH <= 0)."""
    if humidity_pct <= 0 or math.isnan(humidity_pct) or math.isnan(temperature_c):
        return None
    rh = min(humidity_pct, 100.0)
    gamma = math.log(rh / 100.0) + (_A * temperature_c) / (_B + temperature_c)
    return (_B * gamma) / (_A - gamma)
