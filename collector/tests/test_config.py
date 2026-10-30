from pathlib import Path

import pytest
import yaml

from serverroom_monitor.config import (
    ConfigError,
    TimeWindow,
    format_duration,
    load_config,
    parse_config,
    parse_duration,
)

from .conftest import REPO

EXAMPLE = REPO / "config" / "config.example.yaml"


def test_example_config_is_valid_and_matches_spec_defaults() -> None:
    cfg = load_config(EXAMPLE, env={})
    assert cfg.sensor.model == "sht4x"
    assert cfg.sensor.i2c_address == 0x44
    assert cfg.sensor.poll_interval == 30
    t = cfg.thresholds
    assert (t.temperature_warning_high, t.temperature_critical_high) == (27, 30)
    assert (t.humidity_warning_low, t.humidity_critical_low) == (25, 20)
    assert (t.humidity_warning_high, t.humidity_critical_high) == (60, 70)
    assert t.stale_after == 180
    assert t.pending_period == 120
    assert cfg.influxdb.retention_raw == 30 * 86400
    assert cfg.influxdb.retention_downsampled == 730 * 86400
    assert cfg.validation.temperature_min == -20
    assert cfg.validation.temperature_max == 80


def test_empty_config_uses_defaults() -> None:
    cfg = parse_config(None, env={})
    assert cfg.location == "server-room"
    assert cfg.display.mode == "none"
    assert cfg.grafana.lan_access is False


def test_secrets_come_from_environment() -> None:
    cfg = parse_config(
        {"heartbeat": {"url": "https://example.com/a"}},
        env={"INFLUX_WRITE_TOKEN": "secret", "HEARTBEAT_URL": "https://hc-ping.com/x"},
    )
    assert cfg.influxdb.token == "secret"
    assert cfg.heartbeat.url == "https://hc-ping.com/x"
    assert "secret" not in repr(cfg)


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        (30, 30),
        (1.5, 1.5),
        ("30s", 30),
        ("2m", 120),
        ("1h", 3600),
        ("7d", 604800),
        ("250ms", 0.25),
        (" 45 ", 45),
    ],
)
def test_parse_duration(value: object, seconds: float) -> None:
    assert parse_duration(value, "k") == seconds


@pytest.mark.parametrize("value", ["2x", "abc", True, None, "-5s", [1]])
def test_parse_duration_invalid(value: object) -> None:
    with pytest.raises(ConfigError, match="k"):
        parse_duration(value, "k")


def test_format_duration() -> None:
    assert format_duration(120) == "2m"
    assert format_duration(3600) == "1h"
    assert format_duration(90) == "90s"
    assert format_duration(0) == "0s"


def test_hex_address_as_string_and_int() -> None:
    assert parse_config({"sensor": {"i2c_address": "0x45"}}, env={}).sensor.i2c_address == 0x45
    data = yaml.safe_load("sensor: {model: bme280, i2c_address: 0x77}")
    assert parse_config(data, env={}).sensor.i2c_address == 0x77


def test_bme280_default_address() -> None:
    assert parse_config({"sensor": {"model": "bme280"}}, env={}).sensor.i2c_address == 0x76


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"sensor": {"model": "dht22"}}, "sensor.model"),
        ({"sensor": {"model": "bme280", "i2c_address": 0x44}}, "not valid for bme280"),
        ({"sensor": {"poll_interval": "1s"}}, "sensor.poll_interval"),
        ({"display": {"mode": "lcd"}}, "display.mode"),
        ({"display": {"hdmi": {"rotation": 45}}}, "rotation"),
        ({"thresholds": {"temperature": {"warning_high": 31}}}, "warning_high must be below"),
        ({"thresholds": {"humidity": {"warning_low": 15}}}, "critical_low < warning_low"),
        ({"thresholds": {"stale_after": "1m"}, "sensor": {"poll_interval": "45s"}}, "stale_after"),
        ({"notifications": {"telegram": {"enabled": True, "chat_id": "abc"}}}, "chat_id"),
        ({"notifications": {"email": {"enabled": True}}}, "addresses"),
        ({"notifications": {"webhook": {"type": "pagerduty"}}}, "webhook.type"),
        ({"influxdb": {"url": "localhost:8086"}}, "influxdb.url"),
        ({"firewall": {"lan_subnets": ["10.0.0.0/33"]}}, "lan_subnets"),
        ({"location": "bad/name"}, "location"),
        ({"sensr": {}}, "unknown keys: sensr"),
        ({"sensor": {"intervall": 5}}, "unknown keys: sensor.intervall"),
        ({"thresholds": {"temperature": {"warn": 1}}}, "thresholds.temperature.warn"),
        ({"sensor": {"read_retries": "three"}}, "read_retries"),
        ({"grafana": {"lan_access": "yes"}}, "lan_access"),
        ([1, 2], "expected a mapping"),
    ],
)
def test_invalid_values_name_the_key(data: object, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(data, env={})


def test_load_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.yaml", env={})
    bad = tmp_path / "bad.yaml"
    bad.write_text("sensor: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(bad, env={})


def test_time_window() -> None:
    w = TimeWindow.parse("22:00-07:00", "k")
    assert w.contains(23 * 60)
    assert w.contains(3 * 60)
    assert not w.contains(12 * 60)
    day = TimeWindow.parse("08:30-17:00", "k")
    assert day.contains(8 * 60 + 30)
    assert not day.contains(17 * 60)
    with pytest.raises(ConfigError):
        TimeWindow.parse("25:00-07:00", "k")
