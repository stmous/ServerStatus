from __future__ import annotations

from typing import Any

import pytest
import requests

from serverroom_monitor.config import Config, parse_config
from serverroom_monitor.telegram_bot import (
    HELP,
    Snapshot,
    StatusBot,
    TelegramError,
    format_status,
    parse_csv,
    parse_time,
)

NOW = 1_800_000_000.0
STATS = {
    "temperature_min": 21.04,
    "temperature_max": 24.5,
    "temperature_mean": 22.8,
    "humidity_min": 38.0,
    "humidity_max": 45.2,
    "humidity_mean": 41.0,
}


@pytest.fixture
def cfg() -> Config:
    return parse_config(
        {
            "location": "dc-1",
            "notifications": {"telegram": {"enabled": True, "chat_id": "-100123"}},
        },
        env={},
    )


def snap(t: float = 23.4, h: float = 41.0, age: float = 20, ok: bool = True) -> Snapshot:
    return Snapshot(t, h, 9.6, NOW - age, ok, STATS)


def test_format_ok(cfg: Config) -> None:
    text = format_status(cfg, snap(), NOW)
    assert text.startswith("🟢 OK - dc-1")
    assert "🌡 23.4 °C   💧 41 %RH   dew point 9.6 °C" in text
    assert "Last reading 20 s ago" in text
    assert "Sensor: OK" in text
    assert "24 h: 21.0-24.5 °C (avg 22.8) · 38-45 %RH (avg 41)" in text


@pytest.mark.parametrize(
    ("t", "h", "label"),
    [
        (27.5, 41, "🟠 WARNING"),
        (30.5, 41, "🔴 CRITICAL"),
        (22, 19, "🔴 CRITICAL"),
        (22, 61, "🟠 WARNING"),
    ],
)
def test_format_levels(cfg: Config, t: float, h: float, label: str) -> None:
    assert format_status(cfg, snap(t, h), NOW).startswith(label)


def test_format_stale_and_sensor_error(cfg: Config) -> None:
    text = format_status(cfg, snap(age=600, ok=False), NOW)
    assert text.startswith("⚪ NO DATA")
    assert "⚠️ Last reading 10 min ago" in text
    assert "Sensor: ⚠️ ERROR" in text


def test_format_nothing(cfg: Config) -> None:
    text = format_status(cfg, Snapshot(), NOW)
    assert text.startswith("⚪ NO DATA")
    assert "No readings in the last 7 days" in text


def test_parse_csv_multiple_tables() -> None:
    text = (
        ",result,table,_field,_value\r\n,_result,0,temperature,23.4\r\n\r\n"
        ",result,table,_field,_value\r\n,_result,1,humidity,41\r\n\r\n"
    )
    rows = parse_csv(text)
    assert [(r["_field"], r["_value"]) for r in rows] == [
        ("temperature", "23.4"),
        ("humidity", "41"),
    ]


def test_parse_time_nanoseconds() -> None:
    assert parse_time("2027-01-15T08:00:00Z") == parse_time("2027-01-15T08:00:00.000000123Z")


class FakeReader:
    def __init__(self, result: Snapshot | Exception) -> None:
        self.result = result

    def snapshot(self, location: str) -> Snapshot:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeTelegram:
    """Stands in for requests.Session against the Bot API."""

    def __init__(self, updates: list[dict[str, Any]]) -> None:
        self.updates = updates
        self.sent: list[dict[str, Any]] = []
        self.calls: list[str] = []

    def post(self, url: str, json: dict[str, Any], timeout: float) -> Any:
        method = url.rsplit("/", 1)[1]
        self.calls.append(method)
        result: Any = True
        if method == "getUpdates":
            result, self.updates = self.updates, []
        elif method == "sendMessage":
            self.sent.append(json)

        class Resp:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"ok": True, "result": result}

        return Resp()


def message(text: str, chat_id: int = -100123, age: float = 1, uid: int = 1) -> dict[str, Any]:
    return {
        "update_id": uid,
        "message": {"chat": {"id": chat_id, "title": "Ops"}, "text": text, "date": NOW - age},
    }


def make_bot(cfg: Config, tg: FakeTelegram, reader: FakeReader | None = None) -> StatusBot:
    return StatusBot(
        cfg,
        "123:SECRET",
        reader or FakeReader(snap()),  # type: ignore[arg-type]
        session=tg,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


def test_answers_status_in_authorised_chat_only(cfg: Config) -> None:
    tg = FakeTelegram(
        [
            message("/status", uid=10),
            message("/status", chat_id=999, uid=11),  # stranger
            message("/status@my_bot", uid=12),
            message("/status", age=600, uid=13),  # queued while offline
            message("hello", uid=14),
        ]
    )
    bot = make_bot(cfg, tg)
    bot.poll_once()
    assert [m["chat_id"] for m in tg.sent] == [-100123, -100123, -100123]
    assert tg.sent[0]["text"].startswith("🟢 OK - dc-1")
    assert tg.sent[1]["text"].startswith("🟢 OK")
    assert tg.sent[2]["text"] == HELP
    bot.poll_once()  # next poll acknowledges everything up to update 14
    assert bot._offset == 15


def test_influx_failure_is_reported_to_chat(cfg: Config) -> None:
    tg = FakeTelegram([message("/status")])
    make_bot(cfg, tg, FakeReader(RuntimeError("InfluxDB query failed: HTTP 401"))).poll_once()
    assert tg.sent[0]["text"] == "⚠️ Could not read InfluxDB: InfluxDB query failed: HTTP 401"


def test_username_chat_id() -> None:
    cfg = parse_config(
        {"notifications": {"telegram": {"enabled": True, "chat_id": "@ops_channel"}}}, env={}
    )
    bot = make_bot(cfg, FakeTelegram([]))
    assert bot.authorized({"id": 5, "username": "ops_channel"})
    assert not bot.authorized({"id": 5, "username": "other"})


def test_token_never_in_errors(cfg: Config) -> None:
    class Broken:
        def post(self, url: str, **kwargs: Any) -> Any:
            raise requests.ConnectionError(f"Max retries exceeded with url: {url}")

    bot = StatusBot(cfg, "123:SECRET", FakeReader(snap()), session=Broken())  # type: ignore[arg-type]
    with pytest.raises(TelegramError) as info:
        bot.api("getUpdates")
    assert "SECRET" not in str(info.value)
    assert "<token>" in str(info.value)


def test_commands_option_defaults_on() -> None:
    cfg = parse_config({"notifications": {"telegram": {"enabled": True, "chat_id": "1"}}}, env={})
    assert cfg.notifications.telegram.commands is True
