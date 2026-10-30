"""The "Server Room" dashboard, generated so thresholds track config.yaml.

Layout targets the official 7" touch display (800x480) first: the top
10 grid rows (current values + 24 h graphs) fit on one screen. Below the fold
are 24 h statistics and collector health. On a phone (< 769 px wide) Grafana
stacks the panels in the same order, so the current values come first.
"""

from __future__ import annotations

from typing import Any

from serverroom_monitor.config import Config
from serverroom_monitor.grafana import flux
from serverroom_monitor.grafana.flux import DATASOURCE

DASHBOARD_UID = "serverroom"

GREEN, ORANGE, RED, BLUE = "green", "orange", "red", "blue"
# Grafana's built-in "humidity" unit renders as "%H"; "%RH" is the usual notation.
HUMIDITY_UNIT = "suffix: %RH"

Json = dict[str, Any]


def _steps(*pairs: tuple[str, float | None]) -> Json:
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in pairs]}


def temperature_thresholds(cfg: Config, base: str = GREEN) -> Json:
    t = cfg.thresholds
    return _steps(
        (base, None), (ORANGE, t.temperature_warning_high), (RED, t.temperature_critical_high)
    )


def humidity_thresholds(cfg: Config, ok: str = GREEN) -> Json:
    t = cfg.thresholds
    return _steps(
        (RED, None),
        (ORANGE, t.humidity_critical_low),
        (ok, t.humidity_warning_low),
        (ORANGE, t.humidity_warning_high),
        (RED, t.humidity_critical_high),
    )


def _target(ref: str, query: str, hide: bool = False) -> Json:
    return {"refId": ref, "datasource": DATASOURCE, "query": query, "hide": hide}


def _stat(
    pid: int,
    title: str,
    grid: tuple[int, int, int, int],
    targets: list[Json],
    unit: str,
    thresholds: Json,
    decimals: int | None = 1,
    graph: bool = False,
    description: str = "",
    mappings: list[Json] | None = None,
    overrides: list[Json] | None = None,
    text_mode: str = "value",
) -> Json:
    x, y, w, h = grid
    return {
        "id": pid,
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": decimals,
                "color": {"mode": "thresholds"},
                "thresholds": thresholds,
                "mappings": mappings or [],
                "noValue": "No data",
            },
            "overrides": overrides or [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "background",
            "graphMode": "area" if graph else "none",
            "justifyMode": "center",
            "orientation": "auto",
            "textMode": text_mode,
            "wideLayout": True,
            "showPercentChange": False,
        },
    }


def _timeseries(
    pid: int,
    title: str,
    grid: tuple[int, int, int, int],
    query: str,
    unit: str,
    color: str,
    thresholds: Json,
    soft_min: float,
    soft_max: float,
    name: str,
) -> Json:
    x, y, w, h = grid
    return {
        "id": pid,
        "type": "timeseries",
        "title": title,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [_target("A", query)],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": 1,
                "displayName": name,
                "color": {"mode": "fixed", "fixedColor": color},
                "thresholds": thresholds,
                "custom": {
                    "drawStyle": "line",
                    "lineWidth": 2,
                    "fillOpacity": 10,
                    "gradientMode": "opacity",
                    "showPoints": "never",
                    "spanNulls": 300_000,
                    "axisSoftMin": soft_min,
                    "axisSoftMax": soft_max,
                    # Shade the warning/critical bands behind the line.
                    "thresholdsStyle": {"mode": "line+area"},
                },
            },
            "overrides": [],
        },
        "options": {
            "legend": {"showLegend": False, "displayMode": "hidden", "placement": "bottom"},
            "tooltip": {"mode": "single", "sort": "none"},
        },
    }


def _by_ref(ref: str, props: list[Json]) -> Json:
    return {"matcher": {"id": "byFrameRefID", "options": ref}, "properties": props}


def build_dashboard(cfg: Config) -> Json:
    t = cfg.thresholds
    stale = t.stale_after
    poll = cfg.sensor.poll_interval
    temp_band = temperature_thresholds(cfg, base="transparent")
    hum_band = humidity_thresholds(cfg, ok="transparent")

    panels: list[Json] = [
        # --- first screen (800x480): rows 0-9 ------------------------------
        _stat(
            1,
            "Temperature",
            (0, 0, 6, 4),
            [_target("A", flux.last_value(cfg, "temperature"))],
            "celsius",
            temperature_thresholds(cfg),
            description=(
                f"Warning above {t.temperature_warning_high:g} °C, "
                f"critical above {t.temperature_critical_high:g} °C"
            ),
        ),
        _stat(
            2,
            "Humidity",
            (6, 0, 6, 4),
            [_target("A", flux.last_value(cfg, "humidity"))],
            HUMIDITY_UNIT,
            humidity_thresholds(cfg),
            description=(
                f"OK between {t.humidity_warning_low:g} and {t.humidity_warning_high:g} %RH; "
                f"critical below {t.humidity_critical_low:g} or above {t.humidity_critical_high:g}"
            ),
        ),
        _stat(
            3,
            "Dew point",
            (12, 0, 6, 4),
            [_target("A", flux.last_value(cfg, "dew_point"))],
            "celsius",
            _steps((BLUE, None)),
            description="Temperature at which condensation would form",
        ),
        _stat(
            4,
            "Last reading",
            (18, 0, 6, 4),
            [_target("A", flux.age_seconds(cfg))],
            "s",
            _steps((GREEN, None), (ORANGE, max(2 * poll + 5, 60)), (RED, stale)),
            decimals=0,
            description=f"Time since the last reading. Alert after {stale:g} s.",
        ),
        _timeseries(
            5,
            "Temperature",
            (0, 4, 12, 6),
            flux.series_for_range(cfg, "temperature"),
            "celsius",
            "#F2CC0C",
            temp_band,
            18,
            t.temperature_critical_high + 2,
            "Temperature",
        ),
        _timeseries(
            6,
            "Humidity",
            (12, 4, 12, 6),
            flux.series_for_range(cfg, "humidity"),
            HUMIDITY_UNIT,
            "#5794F2",
            hum_band,
            t.humidity_critical_low - 5,
            t.humidity_critical_high + 5,
            "Humidity",
        ),
        # --- below the fold ------------------------------------------------
        _stat(
            7,
            "Last 24 hours",
            (0, 10, 24, 4),
            [
                _target("TMIN", flux.window_stat(cfg, "temperature", "min")),
                _target("TAVG", flux.window_stat(cfg, "temperature", "mean")),
                _target("TMAX", flux.window_stat(cfg, "temperature", "max")),
                _target("HMIN", flux.window_stat(cfg, "humidity", "min")),
                _target("HAVG", flux.window_stat(cfg, "humidity", "mean")),
                _target("HMAX", flux.window_stat(cfg, "humidity", "max")),
            ],
            "celsius",
            temperature_thresholds(cfg),
            text_mode="value_and_name",
            overrides=[
                _by_ref("TMIN", [{"id": "displayName", "value": "Temp min"}]),
                _by_ref("TAVG", [{"id": "displayName", "value": "Temp avg"}]),
                _by_ref("TMAX", [{"id": "displayName", "value": "Temp max"}]),
                *[
                    _by_ref(
                        ref,
                        [
                            {"id": "displayName", "value": label},
                            {"id": "unit", "value": HUMIDITY_UNIT},
                            {"id": "thresholds", "value": humidity_thresholds(cfg)},
                        ],
                    )
                    for ref, label in (
                        ("HMIN", "RH min"),
                        ("HAVG", "RH avg"),
                        ("HMAX", "RH max"),
                    )
                ],
            ],
        ),
        _stat(
            8,
            "Sensor",
            (0, 14, 6, 4),
            [
                _target(
                    "A",
                    flux.last_value(cfg, "sensor_ok", "-15m", measurement="collector_status"),
                )
            ],
            "none",
            _steps((RED, None), (GREEN, 1)),
            decimals=0,
            mappings=[
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "ERROR", "color": RED},
                        "1": {"text": "OK", "color": GREEN},
                    },
                }
            ],
            description="Collector-reported sensor health (read errors after retries)",
        ),
        _stat(
            9,
            "Buffered points",
            (6, 14, 6, 4),
            [
                _target(
                    "A",
                    flux.last_value(cfg, "buffered_points", "-15m", measurement="collector_status"),
                )
            ],
            "none",
            _steps((GREEN, None), (ORANGE, 1)),
            decimals=0,
            description="Points waiting in the local spool (non-zero after an InfluxDB outage)",
        ),
        _stat(
            10,
            "Read errors",
            (12, 14, 6, 4),
            [
                _target(
                    "A",
                    flux.last_value(
                        cfg, "read_errors_total", "-15m", measurement="collector_status"
                    ),
                )
            ],
            "none",
            _steps((GREEN, None), (ORANGE, 10)),
            decimals=0,
            description="I2C read errors since the collector started (retries included)",
        ),
        _stat(
            11,
            "Discarded readings",
            (18, 14, 6, 4),
            [
                _target(
                    "A",
                    flux.last_value(cfg, "discarded_total", "-15m", measurement="collector_status"),
                )
            ],
            "none",
            _steps((GREEN, None), (ORANGE, 5)),
            decimals=0,
            description="Readings rejected by validation (out of bounds or spikes) since start",
        ),
        {
            "id": 12,
            "type": "alertlist",
            "title": "Active alerts",
            "gridPos": {"x": 0, "y": 18, "w": 24, "h": 6},
            "options": {
                "viewMode": "list",
                "groupMode": "default",
                "maxItems": 20,
                "sortOrder": 1,
                "dashboardAlerts": False,
                "alertName": "",
                "alertInstanceLabelFilter": "",
                "stateFilter": {"firing": True, "pending": True, "normal": False},
                "folder": None,
            },
        },
    ]
    return {
        "uid": DASHBOARD_UID,
        "title": "Server Room",
        "description": f"Environmental monitoring for {cfg.location}",
        "tags": ["serverroom"],
        "timezone": "browser",
        "editable": False,
        "graphTooltip": 1,
        "refresh": "30s",
        "time": {"from": "now-24h", "to": "now"},
        "timepicker": {"refresh_intervals": ["30s", "1m", "5m", "15m"]},
        "fiscalYearStartMonth": 0,
        "liveNow": False,
        "schemaVersion": 39,
        "version": 1,
        "links": [],
        "annotations": {"list": []},
        "templating": {"list": []},
        "panels": panels,
    }
