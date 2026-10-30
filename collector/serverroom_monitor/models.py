"""Plain data types shared across the collector."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True, slots=True)
class RawReading:
    """A reading exactly as returned by a sensor driver."""

    temperature: float  # degrees Celsius
    humidity: float  # percent relative humidity


@dataclass(frozen=True, slots=True)
class Reading:
    """A validated reading, enriched with derived values and a timestamp."""

    timestamp: float  # unix epoch seconds (wall clock)
    temperature: float
    humidity: float
    dew_point: float | None


class AlertLevel(Enum):
    """Local alert state, used by the OLED (Grafana evaluates the real alerts)."""

    OK = "OK"
    WARNING = "WARN"
    CRITICAL = "CRIT"
    UNKNOWN = "----"
