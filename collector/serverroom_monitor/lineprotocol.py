"""Minimal InfluxDB line-protocol encoder (no client library needed)."""

from __future__ import annotations

import math
from collections.abc import Mapping

FieldValue = float | int | bool | str


def _escape_key(value: str) -> str:
    """Escape a tag key, tag value or field key."""
    return value.replace("\\", "\\\\").replace(",", r"\,").replace("=", r"\=").replace(" ", r"\ ")


def _escape_measurement(value: str) -> str:
    return value.replace("\\", "\\\\").replace(",", r"\,").replace(" ", r"\ ")


def _format_field(value: FieldValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("line protocol cannot encode NaN or infinity")
        return repr(value)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def encode(
    measurement: str,
    tags: Mapping[str, str],
    fields: Mapping[str, FieldValue | None],
    timestamp_s: int,
) -> str:
    """Encode one point with second precision. ``None`` fields are omitted."""
    present = {k: v for k, v in fields.items() if v is not None}
    if not present:
        raise ValueError("a point needs at least one field")
    tag_str = "".join(
        f",{_escape_key(k)}={_escape_key(v)}" for k, v in sorted(tags.items()) if v != ""
    )
    field_str = ",".join(f"{_escape_key(k)}={_format_field(v)}" for k, v in present.items())
    return f"{_escape_measurement(measurement)}{tag_str} {field_str} {timestamp_s}"
