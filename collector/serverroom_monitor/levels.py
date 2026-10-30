"""Local threshold classification (for the OLED; Grafana owns real alerting)."""

from __future__ import annotations

from serverroom_monitor.config import Thresholds
from serverroom_monitor.models import AlertLevel, Reading


def classify(reading: Reading | None, t: Thresholds) -> AlertLevel:
    if reading is None:
        return AlertLevel.UNKNOWN
    temp, hum = reading.temperature, reading.humidity
    if (
        temp > t.temperature_critical_high
        or hum < t.humidity_critical_low
        or hum > t.humidity_critical_high
    ):
        return AlertLevel.CRITICAL
    if (
        temp > t.temperature_warning_high
        or hum < t.humidity_warning_low
        or hum > t.humidity_warning_high
    ):
        return AlertLevel.WARNING
    return AlertLevel.OK
