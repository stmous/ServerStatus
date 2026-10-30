"""Flux query builders shared by the dashboard and the alert rules.

Queries must not contain ``$``: Grafana expands environment variables in
provisioning files, so a literal dollar sign would be mangled.
"""

from __future__ import annotations

import json

from serverroom_monitor.config import Config

DATASOURCE_UID = "serverroom-influx"
DATASOURCE = {"type": "influxdb", "uid": DATASOURCE_UID}
# Ranges longer than this read the 5-minute downsampled bucket instead of raw.
DOWNSAMPLED_ABOVE_NS = 48 * 3600 * 1_000_000_000


def q(value: str) -> str:
    """Quote a Flux string literal."""
    return json.dumps(value)


def _filter(cfg: Config, measurement: str, field: str) -> str:
    return (
        f"  |> filter(fn: (r) => r._measurement == {q(measurement)} and "
        f"r._field == {q(field)} and r.location == {q(cfg.location)})\n"
    )


def last_value(
    cfg: Config, field: str, lookback: str = "-15m", measurement: str = "environment"
) -> str:
    """Latest value; empty (-> "No data") when nothing arrived within lookback."""
    return (
        f"from(bucket: {q(cfg.influxdb.bucket)})\n"
        f"  |> range(start: {lookback})\n" + _filter(cfg, measurement, field) + "  |> last()\n"
        '  |> keep(columns: ["_time", "_value"])'
    )


def series_for_range(cfg: Config, field: str) -> str:
    """Time series for the dashboard's time range, raw or downsampled as appropriate."""
    return (
        f"span = uint(v: v.timeRangeStop) - uint(v: v.timeRangeStart)\n"
        f"useDownsampled = span > uint(v: {DOWNSAMPLED_ABOVE_NS})\n"
        f"src = if useDownsampled then {q(cfg.influxdb.bucket_downsampled)} "
        f"else {q(cfg.influxdb.bucket)}\n"
        "from(bucket: src)\n"
        "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        + _filter(cfg, "environment", field)
        + '  |> filter(fn: (r) => not exists r.agg or r.agg == "mean")\n'
        "  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)\n"
        '  |> keep(columns: ["_time", "_value"])'
    )


def window_stat(cfg: Config, field: str, fn: str, start: str = "-24h") -> str:
    assert fn in ("min", "max", "mean")  # noqa: S101 - programming error guard
    return (
        f"from(bucket: {q(cfg.influxdb.bucket)})\n"
        f"  |> range(start: {start})\n" + _filter(cfg, "environment", field) + f"  |> {fn}()\n"
        '  |> keep(columns: ["_value"])'
    )


def age_seconds(cfg: Config, lookback: str = "-7d") -> str:
    """Seconds since the last environment point."""
    return (
        f"from(bucket: {q(cfg.influxdb.bucket)})\n"
        f"  |> range(start: {lookback})\n"
        + _filter(cfg, "environment", "temperature")
        + "  |> last()\n"
        "  |> map(fn: (r) => ({_time: r._time, location: r.location, "
        "_value: float(v: uint(v: now()) - uint(v: r._time)) / 1000000000.0}))"
    )


def alert_series(cfg: Config, measurement: str, field: str) -> str:
    """Rule query: values in the rule's relative time range, labelled by location."""
    return (
        f"from(bucket: {q(cfg.influxdb.bucket)})\n"
        "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        + _filter(cfg, measurement, field)
        + '  |> keep(columns: ["_time", "_value", "location"])'
    )
