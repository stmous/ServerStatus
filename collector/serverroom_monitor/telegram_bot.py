"""Telegram bot that answers /status with the current server room readings.

Long polling (``getUpdates``): the Pi only makes outbound HTTPS requests to
Telegram, so no port is opened and the firewall stays closed. Data comes
straight from InfluxDB with Grafana's read-only token, so /status keeps
working when Grafana is down and shows plainly when data is stale.

Only the chat configured in ``notifications.telegram.chat_id`` gets answers;
anyone can find a bot by name, so everything else is ignored (and logged).
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from serverroom_monitor.config import Config, InfluxConfig
from serverroom_monitor.grafana.flux import q
from serverroom_monitor.levels import classify
from serverroom_monitor.models import AlertLevel, Reading

log = logging.getLogger(__name__)

API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
POLL_TIMEOUT = 50  # seconds Telegram holds a getUpdates request open
MAX_MESSAGE_AGE = 120  # ignore commands queued while the bot was down

HELP = (
    "Server room monitor commands:\n"
    "/status - current temperature, humidity and alert level\n"
    "/help - this message"
)

LEVEL_ICON = {
    AlertLevel.OK: "🟢 OK",
    AlertLevel.WARNING: "🟠 WARNING",
    AlertLevel.CRITICAL: "🔴 CRITICAL",
    AlertLevel.UNKNOWN: "⚪ NO DATA",
}


class TelegramError(Exception):
    pass


@dataclass
class Snapshot:
    """What /status reports; every field may be missing."""

    temperature: float | None = None
    humidity: float | None = None
    dew_point: float | None = None
    time: float | None = None  # epoch seconds of the last reading
    sensor_ok: bool | None = None
    stats: dict[str, float] | None = None  # e.g. {"temperature_min": 21.0, ...}


# --------------------------------------------------------------------------
# InfluxDB
# --------------------------------------------------------------------------

_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_time(value: str) -> float:
    """RFC 3339 with up to nanoseconds (Python 3.11 accepts at most 6 digits)."""
    return datetime.fromisoformat(_FRACTION.sub(r"\1", value).replace("Z", "+00:00")).timestamp()


def parse_csv(text: str) -> list[dict[str, str]]:
    """Parse InfluxDB annotated-less CSV (several tables, each with a header)."""
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for row in csv.reader(io.StringIO(text)):
        if not row or not any(cell.strip() for cell in row):
            header = None  # a blank line separates tables
            continue
        if header is None:
            header = row
            continue
        rows.append(dict(zip(header, row, strict=False)))
    return rows


class InfluxReader:
    def __init__(
        self, cfg: InfluxConfig, token: str, session: requests.Session | None = None
    ) -> None:
        self.cfg = cfg
        self._session = session or requests.Session()
        self._headers = {
            "Authorization": f"Token {token}",
            "Accept": "application/csv",
            "Content-Type": "application/json",
        }

    def query(self, flux: str) -> list[dict[str, str]]:
        resp = self._session.post(
            f"{self.cfg.url}/api/v2/query",
            params={"org": self.cfg.org},
            headers=self._headers,
            json={
                "query": flux,
                "type": "flux",
                "dialect": {"header": True, "annotations": [], "delimiter": ","},
            },
            timeout=self.cfg.timeout + 10,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"InfluxDB query failed: HTTP {resp.status_code} {resp.text[:200]}")
        return parse_csv(resp.text)

    def snapshot(self, location: str) -> Snapshot:
        bucket = q(self.cfg.bucket)
        loc = q(location)
        snap = Snapshot()
        for row in self.query(
            f"from(bucket: {bucket}) |> range(start: -7d)"
            f' |> filter(fn: (r) => r._measurement == "environment" and r.location == {loc})'
            " |> last()"
        ):
            value = float(row["_value"])
            if row["_field"] == "temperature":
                snap.temperature = value
                snap.time = parse_time(row["_time"])
            elif row["_field"] == "humidity":
                snap.humidity = value
            elif row["_field"] == "dew_point":
                snap.dew_point = value
        for row in self.query(
            f"from(bucket: {bucket}) |> range(start: -15m)"
            f' |> filter(fn: (r) => r._measurement == "collector_status" and r.location == {loc}'
            ' and r._field == "sensor_ok") |> last()'
        ):
            snap.sensor_ok = float(row["_value"]) >= 1
        stats: dict[str, float] = {}
        for fn in ("min", "max", "mean"):
            for row in self.query(
                f"from(bucket: {bucket}) |> range(start: -24h)"
                f' |> filter(fn: (r) => r._measurement == "environment" and r.location == {loc}'
                ' and (r._field == "temperature" or r._field == "humidity"))'
                f' |> {fn}() |> keep(columns: ["_field", "_value"])'
            ):
                stats[f"{row['_field']}_{fn}"] = float(row["_value"])
        snap.stats = stats or None
        return snap


# --------------------------------------------------------------------------
# Formatting (pure, unit-tested)
# --------------------------------------------------------------------------


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 90 * 60:
        return f"{seconds // 60} min ago"
    if seconds < 48 * 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min ago"
    return f"{seconds // 86400} days ago"


def format_status(cfg: Config, snap: Snapshot, now: float) -> str:
    stale = snap.time is None or now - snap.time > cfg.thresholds.stale_after
    if snap.temperature is None or snap.humidity is None or snap.time is None:
        level = AlertLevel.UNKNOWN
    else:
        reading = Reading(snap.time, snap.temperature, snap.humidity, snap.dew_point)
        level = AlertLevel.UNKNOWN if stale else classify(reading, cfg.thresholds)

    lines = [f"{LEVEL_ICON[level]} - {cfg.location}"]
    if snap.temperature is not None and snap.humidity is not None:
        dp = f"   dew point {snap.dew_point:.1f} °C" if snap.dew_point is not None else ""
        lines.append(f"🌡 {snap.temperature:.1f} °C   💧 {snap.humidity:.0f} %RH{dp}")
    if snap.time is None:
        lines.append("⚠️ No readings in the last 7 days")
    else:
        stamp = time.strftime("%H:%M:%S", time.localtime(snap.time))
        warn = "⚠️ " if stale else ""
        lines.append(f"{warn}Last reading {_ago(now - snap.time)} ({stamp})")
    if snap.sensor_ok is not None:
        lines.append("Sensor: OK" if snap.sensor_ok else "Sensor: ⚠️ ERROR (reads failing)")
    s = snap.stats or {}
    if "temperature_min" in s and "humidity_min" in s:
        lines.append(
            f"24 h: {s['temperature_min']:.1f}-{s['temperature_max']:.1f} °C "
            f"(avg {s['temperature_mean']:.1f}) · "
            f"{s['humidity_min']:.0f}-{s['humidity_max']:.0f} %RH "
            f"(avg {s['humidity_mean']:.0f})"
        )
    t = cfg.thresholds
    lines.append(
        f"Limits: warn > {t.temperature_warning_high:g} °C, "
        f"crit > {t.temperature_critical_high:g} °C; "
        f"RH {t.humidity_warning_low:g}-{t.humidity_warning_high:g} %"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Bot loop
# --------------------------------------------------------------------------


class StatusBot:
    def __init__(
        self,
        cfg: Config,
        token: str,
        reader: InfluxReader,
        session: requests.Session | None = None,
        clock: Any = time.time,
        stop: threading.Event | None = None,
    ) -> None:
        self.cfg = cfg
        self._token = token
        self._reader = reader
        self._session = session or requests.Session()
        self._clock = clock
        self.stop = stop or threading.Event()
        self._offset: int | None = None
        self._failing = False

    def _clean(self, text: str) -> str:
        """Never let the bot token reach the logs (it is part of every API URL)."""
        return text.replace(self._token, "<token>") if self._token else text

    def api(self, method: str, http_timeout: float = 20, **params: Any) -> Any:
        try:
            resp = self._session.post(
                f"{API_BASE}/bot{self._token}/{method}", json=params, timeout=http_timeout
            )
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise TelegramError(self._clean(str(exc))) from exc
        if not data.get("ok"):
            raise TelegramError(str(data.get("description") or f"HTTP {resp.status_code}"))
        return data.get("result")

    def authorized(self, chat: dict[str, Any]) -> bool:
        wanted = self.cfg.notifications.telegram.chat_id
        return str(chat.get("id")) == wanted or (
            wanted.startswith("@") and f"@{chat.get('username', '')}" == wanted
        )

    def reply_for(self, text: str) -> str:
        command = text.strip().split()[0].split("@")[0].lower() if text.strip() else ""
        if command in ("/status", "status", "/s"):
            try:
                snap = self._reader.snapshot(self.cfg.location)
            except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
                log.warning("status query failed: %s", exc)
                return f"⚠️ Could not read InfluxDB: {exc}"
            return format_status(self.cfg, snap, self._clock())
        return HELP

    def handle(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        text = message.get("text") or ""
        if not chat or not text:
            return
        if not self.authorized(chat):
            log.info(
                "ignoring message from unauthorised chat %s (%s)",
                chat.get("id"),
                chat.get("title") or chat.get("username") or chat.get("first_name"),
            )
            return
        if self._clock() - float(message.get("date", 0)) > MAX_MESSAGE_AGE:
            return  # queued while the bot was offline; don't answer stale requests
        self.api("sendMessage", chat_id=chat["id"], text=self.reply_for(text))

    def poll_once(self) -> None:
        params: dict[str, Any] = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message"]}
        if self._offset is not None:
            params["offset"] = self._offset
        updates = self.api("getUpdates", http_timeout=POLL_TIMEOUT + 15, **params)
        for update in updates or []:
            self._offset = int(update["update_id"]) + 1
            try:
                self.handle(update)
            except TelegramError as exc:
                log.warning("could not answer: %s", exc)

    def run_forever(self) -> None:
        try:
            self.api(
                "setMyCommands",
                commands=[
                    {"command": "status", "description": "Current temperature and humidity"},
                    {"command": "help", "description": "Show available commands"},
                ],
            )
        except TelegramError as exc:
            log.warning("could not register bot commands: %s", exc)
        log.info(
            "Telegram bot running (answering chat %s)", self.cfg.notifications.telegram.chat_id
        )
        backoff = 5.0
        while not self.stop.is_set():
            try:
                self.poll_once()
            except TelegramError as exc:
                if not self._failing:
                    log.warning("Telegram unreachable, retrying: %s", exc)
                self._failing = True
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 300)
                continue
            if self._failing:
                log.info("Telegram reachable again")
            self._failing = False
            backoff = 5.0
        log.info("Telegram bot stopping")
