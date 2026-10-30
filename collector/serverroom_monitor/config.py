"""Configuration loading and validation.

The single source of truth is ``/etc/serverroom-monitor/config.yaml``. Secrets
never live in that file: they come from environment variables, which systemd
loads from per-service env files generated from ``secrets.env``.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import yaml

DEFAULT_CONFIG_PATH = Path("/etc/serverroom-monitor/config.yaml")

SensorModel = Literal["sht4x", "sht31", "bme280", "simulated"]
DisplayMode = Literal["hdmi", "oled", "none"]
WebhookType = Literal["slack", "discord", "teams", "generic"]

SENSOR_ADDRESSES: dict[str, tuple[int, ...]] = {
    "sht4x": (0x44, 0x45, 0x46),
    "sht31": (0x44, 0x45),
    "bme280": (0x76, 0x77),
    "simulated": (),
}

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?\s*$")
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_LOCATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,62}$")


class ConfigError(ValueError):
    """Raised for any invalid configuration value. Messages name the key."""


def parse_duration(value: object, key: str) -> float:
    """Parse ``30``, ``"30s"``, ``"2m"``, ``"1.5h"`` into seconds."""
    if isinstance(value, bool):
        raise ConfigError(f"{key}: expected a duration, got {value!r}")
    if isinstance(value, int | float):
        seconds = float(value)
    elif isinstance(value, str) and (m := _DURATION_RE.match(value)):
        seconds = float(m.group(1)) * _DURATION_UNITS[m.group(2) or "s"]
    else:
        raise ConfigError(f"{key}: invalid duration {value!r} (use e.g. 30, '30s', '2m', '1h')")
    if seconds < 0:
        raise ConfigError(f"{key}: duration must not be negative")
    return seconds


def format_duration(seconds: float) -> str:
    """Render seconds the way Grafana/Prometheus expect (``2m``, ``90s``)."""
    s = round(seconds)
    for unit, size in (("h", 3600), ("m", 60)):
        if s >= size and s % size == 0:
            return f"{s // size}{unit}"
    return f"{s}s"


@dataclass(frozen=True)
class TimeWindow:
    """A daily window such as 22:00-07:00 (may wrap past midnight)."""

    start_minute: int
    end_minute: int

    @classmethod
    def parse(cls, value: str, key: str) -> TimeWindow:
        parts = value.split("-")
        if len(parts) != 2:
            raise ConfigError(f"{key}: expected 'HH:MM-HH:MM', got {value!r}")
        minutes = []
        for part in parts:
            m = _HHMM_RE.match(part.strip())
            if not m:
                raise ConfigError(f"{key}: invalid time {part!r} in {value!r}")
            minutes.append(int(m.group(1)) * 60 + int(m.group(2)))
        return cls(minutes[0], minutes[1])

    def contains(self, minute_of_day: int) -> bool:
        if self.start_minute == self.end_minute:
            return False
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute_of_day < self.end_minute
        return minute_of_day >= self.start_minute or minute_of_day < self.end_minute


@dataclass(frozen=True)
class SensorConfig:
    model: SensorModel = "sht4x"
    i2c_address: int = 0x44
    poll_interval: float = 30.0
    read_retries: int = 3
    retry_backoff: float = 0.5
    failure_threshold: int = 3
    reinit_after: int = 2


@dataclass(frozen=True)
class ValidationConfig:
    temperature_min: float = -20.0
    temperature_max: float = 80.0
    humidity_min: float = 0.0
    humidity_max: float = 100.0
    max_temperature_jump: float = 3.0
    max_humidity_jump: float = 10.0
    jump_window: float = 300.0
    rebaseline_after: int = 3


@dataclass(frozen=True)
class OledConfig:
    i2c_address: int = 0x3C
    width: int = 128
    height: int = 64
    rotate: int = 0
    contrast: int = 255
    pixel_shift: bool = True
    night: TimeWindow | None = None
    night_mode: Literal["dim", "off"] = "dim"


@dataclass(frozen=True)
class HdmiConfig:
    session: Literal["auto", "cage", "desktop"] = "auto"
    rotation: int = 0
    screen_blanking: bool = False
    blank_after: float = 0.0
    brightness: int = 100
    night: TimeWindow | None = None
    night_brightness: int = 20
    theme: Literal["dark", "light"] = "dark"


@dataclass(frozen=True)
class DisplayConfig:
    mode: DisplayMode = "none"
    oled: OledConfig = field(default_factory=OledConfig)
    hdmi: HdmiConfig = field(default_factory=HdmiConfig)


@dataclass(frozen=True)
class InfluxConfig:
    url: str = "http://127.0.0.1:8086"
    org: str = "serverroom"
    bucket: str = "serverroom_raw"
    bucket_downsampled: str = "serverroom_5m"
    retention_raw: float = 30 * 86400.0
    retention_downsampled: float = 730 * 86400.0
    timeout: float = 5.0
    token: str = field(default="", repr=False)


@dataclass(frozen=True)
class BufferConfig:
    path: Path = Path("/var/lib/serverroom-monitor/buffer.sqlite")
    max_points: int = 500_000
    flush_batch: int = 500
    flush_time_budget: float = 15.0


@dataclass(frozen=True)
class HeartbeatConfig:
    url: str = field(default="", repr=False)
    timeout: float = 5.0
    fail_suffix: str = "/fail"

    @property
    def enabled(self) -> bool:
        return bool(self.url)


@dataclass(frozen=True)
class Thresholds:
    temperature_warning_high: float = 27.0
    temperature_critical_high: float = 30.0
    humidity_warning_low: float = 25.0
    humidity_critical_low: float = 20.0
    humidity_warning_high: float = 60.0
    humidity_critical_high: float = 70.0
    stale_after: float = 180.0
    pending_period: float = 120.0


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool = False
    chat_id: str = ""


@dataclass(frozen=True)
class EmailConfig:
    enabled: bool = False
    addresses: tuple[str, ...] = ()
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    from_address: str = ""
    from_name: str = "Server Room Monitor"
    starttls: Literal["opportunistic", "mandatory", "none"] = "mandatory"


@dataclass(frozen=True)
class WebhookConfig:
    enabled: bool = False
    type: WebhookType = "generic"


@dataclass(frozen=True)
class DeadmanConfig:
    enabled: bool = False


@dataclass(frozen=True)
class NotificationsConfig:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    deadman: DeadmanConfig = field(default_factory=DeadmanConfig)
    group_wait: float = 30.0
    group_interval: float = 300.0
    repeat_interval_warning: float = 4 * 3600.0
    repeat_interval_critical: float = 3600.0

    @property
    def any_enabled(self) -> bool:
        return self.telegram.enabled or self.email.enabled or self.webhook.enabled


@dataclass(frozen=True)
class GrafanaConfig:
    url: str = "http://127.0.0.1:3000"
    lan_access: bool = False
    port: int = 3000
    tailscale_serve: bool = True


@dataclass(frozen=True)
class FirewallConfig:
    enabled: bool = True
    lan_subnets: tuple[str, ...] = ()  # empty = auto-detect at install time


@dataclass(frozen=True)
class BackupConfig:
    directory: Path = Path("/var/backups/serverroom-monitor")
    keep: int = 7


@dataclass(frozen=True)
class Config:
    location: str = "server-room"
    sensor: SensorConfig = field(default_factory=SensorConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    influxdb: InfluxConfig = field(default_factory=InfluxConfig)
    buffer: BufferConfig = field(default_factory=BufferConfig)
    heartbeat: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    thresholds: Thresholds = field(default_factory=Thresholds)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    grafana: GrafanaConfig = field(default_factory=GrafanaConfig)
    firewall: FirewallConfig = field(default_factory=FirewallConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)


# --------------------------------------------------------------------------
# Parsing helpers. Each takes the raw mapping for one section and the dotted
# key prefix so error messages point at the exact offending key.
# --------------------------------------------------------------------------


class _Section:
    def __init__(self, data: object, prefix: str) -> None:
        if data is None:
            data = {}
        if not isinstance(data, Mapping):
            raise ConfigError(f"{prefix}: expected a mapping, got {type(data).__name__}")
        self._data: Mapping[str, Any] = data
        self._prefix = prefix
        self._used: set[str] = set()

    def key(self, name: str) -> str:
        return f"{self._prefix}.{name}" if self._prefix else name

    def has(self, name: str) -> bool:
        return name in self._data and self._data[name] is not None

    def raw(self, name: str) -> object:
        self._used.add(name)
        return self._data.get(name)

    def section(self, name: str) -> _Section:
        return _Section(self.raw(name), self.key(name))

    def str_(self, name: str, default: str) -> str:
        v = self.raw(name)
        if v is None:
            return default
        if isinstance(v, bool) or not isinstance(v, str | int | float):
            raise ConfigError(f"{self.key(name)}: expected a string")
        return str(v)

    def bool_(self, name: str, default: bool) -> bool:
        v = self.raw(name)
        if v is None:
            return default
        if not isinstance(v, bool):
            raise ConfigError(f"{self.key(name)}: expected true or false, got {v!r}")
        return v

    def num(
        self,
        name: str,
        default: float,
        lo: float | None = None,
        hi: float | None = None,
    ) -> float:
        v = self.raw(name)
        if v is None:
            return default
        if isinstance(v, bool) or not isinstance(v, int | float):
            raise ConfigError(f"{self.key(name)}: expected a number, got {v!r}")
        value = float(v)
        self._check_range(name, value, lo, hi)
        return value

    def int_(self, name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
        v = self.raw(name)
        if v is None:
            return default
        if isinstance(v, str):
            try:
                v = int(v, 0)  # allow "0x44"
            except ValueError as exc:
                raise ConfigError(f"{self.key(name)}: expected an integer, got {v!r}") from exc
        if isinstance(v, bool) or not isinstance(v, int):
            raise ConfigError(f"{self.key(name)}: expected an integer, got {v!r}")
        self._check_range(name, v, lo, hi)
        return v

    def duration(
        self, name: str, default: float, lo: float | None = None, hi: float | None = None
    ) -> float:
        v = self.raw(name)
        if v is None:
            return default
        value = parse_duration(v, self.key(name))
        self._check_range(name, value, lo, hi)
        return value

    def choice(self, name: str, default: str, choices: tuple[str, ...]) -> str:
        v = self.str_(name, default)
        if v not in choices:
            raise ConfigError(f"{self.key(name)}: must be one of {', '.join(choices)}; got {v!r}")
        return v

    def window(self, name: str) -> TimeWindow | None:
        v = self.raw(name)
        if v in (None, "", False):
            return None
        if not isinstance(v, str):
            raise ConfigError(f"{self.key(name)}: expected 'HH:MM-HH:MM'")
        return TimeWindow.parse(v, self.key(name))

    def str_list(self, name: str) -> tuple[str, ...]:
        v = self.raw(name)
        if v is None:
            return ()
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list) or not all(isinstance(i, str) for i in v):
            raise ConfigError(f"{self.key(name)}: expected a list of strings")
        return tuple(i.strip() for i in cast("list[str]", v) if i.strip())

    def _check_range(self, name: str, value: float, lo: float | None, hi: float | None) -> None:
        if lo is not None and value < lo:
            raise ConfigError(f"{self.key(name)}: must be >= {lo}, got {value}")
        if hi is not None and value > hi:
            raise ConfigError(f"{self.key(name)}: must be <= {hi}, got {value}")

    def unknown_keys(self) -> list[str]:
        return [self.key(k) for k in self._data if k not in self._used]


def _parse_sensor(s: _Section) -> SensorConfig:
    model = cast(
        "SensorModel", s.choice("model", "sht4x", ("sht4x", "sht31", "bme280", "simulated"))
    )
    default_addr = {"sht4x": 0x44, "sht31": 0x44, "bme280": 0x76, "simulated": 0}[model]
    address = s.int_("i2c_address", default_addr, 0, 0x7F)
    allowed = SENSOR_ADDRESSES[model]
    if allowed and address not in allowed:
        valid = ", ".join(hex(a) for a in allowed)
        raise ConfigError(f"sensor.i2c_address: {hex(address)} is not valid for {model} ({valid})")
    return SensorConfig(
        model=model,
        i2c_address=address,
        poll_interval=s.duration("poll_interval", 30.0, 2.0, 3600.0),
        read_retries=s.int_("read_retries", 3, 0, 10),
        retry_backoff=s.duration("retry_backoff", 0.5, 0.0, 10.0),
        failure_threshold=s.int_("failure_threshold", 3, 1, 100),
        reinit_after=s.int_("reinit_after", 2, 1, 100),
    )


def _parse_validation(s: _Section) -> ValidationConfig:
    v = ValidationConfig(
        temperature_min=s.num("temperature_min", -20.0, -40.0, 125.0),
        temperature_max=s.num("temperature_max", 80.0, -40.0, 125.0),
        humidity_min=s.num("humidity_min", 0.0, 0.0, 100.0),
        humidity_max=s.num("humidity_max", 100.0, 0.0, 100.0),
        max_temperature_jump=s.num("max_temperature_jump", 3.0, 0.1, 100.0),
        max_humidity_jump=s.num("max_humidity_jump", 10.0, 0.1, 100.0),
        jump_window=s.duration("jump_window", 300.0, 0.0, 86400.0),
        rebaseline_after=s.int_("rebaseline_after", 3, 1, 100),
    )
    if v.temperature_min >= v.temperature_max:
        raise ConfigError("validation: temperature_min must be below temperature_max")
    if v.humidity_min >= v.humidity_max:
        raise ConfigError("validation: humidity_min must be below humidity_max")
    return v


def _parse_display(s: _Section) -> DisplayConfig:
    mode = cast("DisplayMode", s.choice("mode", "none", ("hdmi", "oled", "none")))
    o = s.section("oled")
    oled = OledConfig(
        i2c_address=o.int_("i2c_address", 0x3C, 0x3C, 0x3D),
        width=o.int_("width", 128, 64, 256),
        height=o.int_("height", 64, 32, 128),
        rotate=o.int_("rotate", 0, 0, 3),
        contrast=o.int_("contrast", 255, 0, 255),
        pixel_shift=o.bool_("pixel_shift", True),
        night=o.window("night"),
        night_mode=cast("Literal['dim', 'off']", o.choice("night_mode", "dim", ("dim", "off"))),
    )
    h = s.section("hdmi")
    rotation = h.int_("rotation", 0)
    if rotation not in (0, 90, 180, 270):
        raise ConfigError("display.hdmi.rotation: must be 0, 90, 180 or 270")
    hdmi = HdmiConfig(
        session=cast(
            "Literal['auto', 'cage', 'desktop']",
            h.choice("session", "auto", ("auto", "cage", "desktop")),
        ),
        rotation=rotation,
        screen_blanking=h.bool_("screen_blanking", False),
        blank_after=h.duration("blank_after", 0.0, 0.0, 86400.0),
        brightness=h.int_("brightness", 100, 0, 100),
        night=h.window("night"),
        night_brightness=h.int_("night_brightness", 20, 0, 100),
        theme=cast("Literal['dark', 'light']", h.choice("theme", "dark", ("dark", "light"))),
    )
    for sub in (o, h):
        if unknown := sub.unknown_keys():
            raise ConfigError(f"unknown keys: {', '.join(unknown)}")
    return DisplayConfig(mode=mode, oled=oled, hdmi=hdmi)


def _parse_influx(s: _Section, env: Mapping[str, str]) -> InfluxConfig:
    url = s.str_("url", "http://127.0.0.1:8086").rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ConfigError("influxdb.url: must start with http:// or https://")
    cfg = InfluxConfig(
        url=url,
        org=s.str_("org", "serverroom"),
        bucket=s.str_("bucket", "serverroom_raw"),
        bucket_downsampled=s.str_("bucket_downsampled", "serverroom_5m"),
        retention_raw=s.duration("retention_raw", 30 * 86400.0, 3600.0),
        retention_downsampled=s.duration("retention_downsampled", 730 * 86400.0, 3600.0),
        timeout=s.duration("timeout", 5.0, 0.5, 60.0),
        token=env.get("INFLUX_WRITE_TOKEN", ""),
    )
    if cfg.bucket == cfg.bucket_downsampled:
        raise ConfigError("influxdb: bucket and bucket_downsampled must differ")
    return cfg


def _parse_thresholds(s: _Section) -> Thresholds:
    t = s.section("temperature")
    h = s.section("humidity")
    th = Thresholds(
        temperature_warning_high=t.num("warning_high", 27.0, -20.0, 80.0),
        temperature_critical_high=t.num("critical_high", 30.0, -20.0, 80.0),
        humidity_warning_low=h.num("warning_low", 25.0, 0.0, 100.0),
        humidity_critical_low=h.num("critical_low", 20.0, 0.0, 100.0),
        humidity_warning_high=h.num("warning_high", 60.0, 0.0, 100.0),
        humidity_critical_high=h.num("critical_high", 70.0, 0.0, 100.0),
        stale_after=s.duration("stale_after", 180.0, 60.0, 86400.0),
        pending_period=s.duration("pending_period", 120.0, 0.0, 3600.0),
    )
    for sub in (t, h):
        if unknown := sub.unknown_keys():
            raise ConfigError(f"unknown keys: {', '.join(unknown)}")
    if th.temperature_warning_high >= th.temperature_critical_high:
        raise ConfigError("thresholds.temperature: warning_high must be below critical_high")
    if not (
        th.humidity_critical_low
        < th.humidity_warning_low
        < th.humidity_warning_high
        < th.humidity_critical_high
    ):
        raise ConfigError(
            "thresholds.humidity: need critical_low < warning_low < warning_high < critical_high"
        )
    return th


def _parse_notifications(s: _Section) -> NotificationsConfig:
    tg = s.section("telegram")
    telegram = TelegramConfig(enabled=tg.bool_("enabled", False), chat_id=tg.str_("chat_id", ""))
    if telegram.enabled and not re.fullmatch(r"-?\d+|@\w+", telegram.chat_id):
        raise ConfigError(
            "notifications.telegram.chat_id: must be a numeric chat ID (e.g. -1001234567890)"
        )
    em = s.section("email")
    email = EmailConfig(
        enabled=em.bool_("enabled", False),
        addresses=em.str_list("addresses"),
        smtp_host=em.str_("smtp_host", ""),
        smtp_port=em.int_("smtp_port", 587, 1, 65535),
        smtp_user=em.str_("smtp_user", ""),
        from_address=em.str_("from_address", ""),
        from_name=em.str_("from_name", "Server Room Monitor"),
        starttls=cast(
            "Literal['opportunistic', 'mandatory', 'none']",
            em.choice("starttls", "mandatory", ("opportunistic", "mandatory", "none")),
        ),
    )
    if email.enabled:
        if not email.addresses:
            raise ConfigError("notifications.email.addresses: at least one address is required")
        if not email.smtp_host or not email.from_address:
            raise ConfigError("notifications.email: smtp_host and from_address are required")
    wh = s.section("webhook")
    webhook = WebhookConfig(
        enabled=wh.bool_("enabled", False),
        type=cast(
            "WebhookType", wh.choice("type", "generic", ("slack", "discord", "teams", "generic"))
        ),
    )
    dm = s.section("deadman")
    deadman = DeadmanConfig(enabled=dm.bool_("enabled", False))
    for sub in (tg, em, wh, dm):
        if unknown := sub.unknown_keys():
            raise ConfigError(f"unknown keys: {', '.join(unknown)}")
    return NotificationsConfig(
        telegram=telegram,
        email=email,
        webhook=webhook,
        deadman=deadman,
        group_wait=s.duration("group_wait", 30.0, 0.0, 3600.0),
        group_interval=s.duration("group_interval", 300.0, 60.0, 86400.0),
        repeat_interval_warning=s.duration("repeat_interval_warning", 4 * 3600.0, 300.0),
        repeat_interval_critical=s.duration("repeat_interval_critical", 3600.0, 300.0),
    )


def _parse_firewall(s: _Section) -> FirewallConfig:
    subnets = s.str_list("lan_subnets")
    for net in subnets:
        try:
            ipaddress.ip_network(net, strict=False)
        except ValueError as exc:
            raise ConfigError(f"firewall.lan_subnets: invalid subnet {net!r}") from exc
    return FirewallConfig(enabled=s.bool_("enabled", True), lan_subnets=subnets)


def parse_config(data: object, env: Mapping[str, str] | None = None) -> Config:
    """Build a validated :class:`Config` from parsed YAML and the environment."""
    env = os.environ if env is None else env
    root = _Section(data, "")
    location = root.str_("location", "server-room").strip()
    if not _LOCATION_RE.match(location):
        raise ConfigError(
            "location: use 1-63 letters, digits, spaces, '.', '_' or '-' (starting alphanumeric)"
        )
    sec_sensor = root.section("sensor")
    sec_validation = root.section("validation")
    sec_display = root.section("display")
    sec_influx = root.section("influxdb")
    sec_thresholds = root.section("thresholds")
    sec_notifications = root.section("notifications")
    sensor = _parse_sensor(sec_sensor)
    validation = _parse_validation(sec_validation)
    display = _parse_display(sec_display)
    influx = _parse_influx(sec_influx, env)

    b = root.section("buffer")
    buffer = BufferConfig(
        path=Path(b.str_("path", str(BufferConfig.path))),
        max_points=b.int_("max_points", 500_000, 1000, 50_000_000),
        flush_batch=b.int_("flush_batch", 500, 1, 10_000),
        flush_time_budget=b.duration("flush_time_budget", 15.0, 1.0, 120.0),
    )
    hb = root.section("heartbeat")
    config_url = hb.str_("url", "")
    heartbeat = HeartbeatConfig(
        url=env.get("HEARTBEAT_URL") or config_url,
        timeout=hb.duration("timeout", 5.0, 1.0, 30.0),
        fail_suffix=hb.str_("fail_suffix", "/fail"),
    )
    if heartbeat.url and not heartbeat.url.startswith(("http://", "https://")):
        raise ConfigError("heartbeat.url: must start with http:// or https://")
    thresholds = _parse_thresholds(sec_thresholds)
    notifications = _parse_notifications(sec_notifications)
    g = root.section("grafana")
    grafana = GrafanaConfig(
        url=g.str_("url", "http://127.0.0.1:3000").rstrip("/"),
        lan_access=g.bool_("lan_access", False),
        port=g.int_("port", 3000, 1024, 65535),
        tailscale_serve=g.bool_("tailscale_serve", True),
    )
    sec_firewall = root.section("firewall")
    firewall = _parse_firewall(sec_firewall)
    bk = root.section("backup")
    backup = BackupConfig(
        directory=Path(bk.str_("directory", str(BackupConfig.directory))),
        keep=bk.int_("keep", 7, 1, 365),
    )
    sections = (
        sec_sensor,
        sec_validation,
        sec_display,
        sec_influx,
        sec_thresholds,
        sec_notifications,
        b,
        hb,
        g,
        bk,
        sec_firewall,
    )
    unknown = [k for sec in sections for k in sec.unknown_keys()]
    unknown += root.unknown_keys()
    if unknown:
        raise ConfigError(f"unknown keys: {', '.join(unknown)}")

    # The stale-data alert must not be able to fire between two healthy polls.
    if thresholds.stale_after < 2 * sensor.poll_interval:
        raise ConfigError("thresholds.stale_after: must be at least twice sensor.poll_interval")

    return Config(
        location=location,
        sensor=sensor,
        validation=validation,
        display=display,
        influxdb=influx,
        buffer=buffer,
        heartbeat=heartbeat,
        thresholds=thresholds,
        notifications=notifications,
        grafana=grafana,
        firewall=firewall,
        backup=backup,
    )


def load_config(path: Path = DEFAULT_CONFIG_PATH, env: Mapping[str, str] | None = None) -> Config:
    """Read and validate a YAML config file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    return parse_config(data, env)
