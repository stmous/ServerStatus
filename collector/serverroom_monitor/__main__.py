"""Command-line entry point.

    serverroom-monitor run [--simulate [SCENARIO]] [--once] [--dry-run]
    serverroom-monitor read [--simulate [SCENARIO]] [--count N]
    serverroom-monitor check-config
    serverroom-monitor render-grafana --out DIR
    serverroom-monitor backlight
    serverroom-monitor test-alerts [--direct]

``run`` is the default when no subcommand is given.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from serverroom_monitor import __version__
from serverroom_monitor.config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config
from serverroom_monitor.logging_setup import setup_logging
from serverroom_monitor.sensors.simulated import SCENARIOS

log = logging.getLogger("serverroom_monitor")

SUBCOMMANDS = (
    "run",
    "read",
    "check-config",
    "render-grafana",
    "backlight",
    "test-alerts",
    "telegram-bot",
)


class _PrintWriter:
    """--dry-run writer: print line protocol instead of sending it."""

    def write(self, lines: Sequence[str]) -> None:
        for line in lines:
            print(line, flush=True)


def _add_simulate(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--sim-speed",
        type=float,
        default=1.0,
        metavar="FACTOR",
        help="make simulated scenarios evolve FACTOR times faster (e.g. 10)",
    )
    p.add_argument(
        "--simulate",
        nargs="?",
        const="normal",
        choices=SCENARIOS,
        metavar="SCENARIO",
        help=f"use fake sensor data instead of I2C ({', '.join(SCENARIOS)}; default: normal)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="serverroom-monitor", description=__doc__.split("\n")[0])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-c", "--config", type=Path, default=DEFAULT_CONFIG_PATH, help="path to config.yaml"
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the collector loop (default)")
    _add_simulate(run)
    run.add_argument("--once", action="store_true", help="run a single cycle and exit")
    run.add_argument("--dry-run", action="store_true", help="print points instead of writing them")
    run.add_argument("--interval", type=float, help="override sensor.poll_interval (seconds)")

    read = sub.add_parser("read", help="one-shot sensor read (no database)")
    _add_simulate(read)
    read.add_argument("--count", type=int, default=1, help="number of reads, 2 s apart")

    sub.add_parser("check-config", help="validate the config file and exit")

    render = sub.add_parser("render-grafana", help="generate Grafana provisioning from config")
    render.add_argument("--out", type=Path, required=True, help="output directory")
    render.add_argument("--root-url", default=None, help="public URL (e.g. Tailscale HTTPS)")
    render.add_argument(
        "--deploy-root",
        type=Path,
        default=None,
        help="path the files will live at (default: --out)",
    )

    sub.add_parser("backlight", help="apply the configured display backlight level now")

    alerts = sub.add_parser("test-alerts", help="send a test notification to every channel")
    alerts.add_argument(
        "--direct",
        action="store_true",
        help="talk to Telegram/SMTP/webhook directly instead of through Grafana",
    )
    alerts.add_argument("--grafana-url", default=None, help="override grafana.url")

    sub.add_parser("telegram-bot", help="answer /status in the configured Telegram chat")
    return parser


def _load(path: Path) -> Config:
    try:
        return load_config(path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def cmd_run(args: argparse.Namespace) -> int:
    import dataclasses  # noqa: PLC0415

    from serverroom_monitor.buffer import PointBuffer  # noqa: PLC0415
    from serverroom_monitor.heartbeat import Heartbeat  # noqa: PLC0415
    from serverroom_monitor.influx import InfluxWriter  # noqa: PLC0415
    from serverroom_monitor.sdnotify import SystemdNotifier  # noqa: PLC0415
    from serverroom_monitor.sensors import create_driver  # noqa: PLC0415
    from serverroom_monitor.service import (  # noqa: PLC0415
        Collector,
        Display,
        Writer,
        install_signal_handlers,
    )

    cfg = _load(args.config)
    if args.interval:
        cfg = dataclasses.replace(
            cfg, sensor=dataclasses.replace(cfg.sensor, poll_interval=args.interval)
        )
    driver = create_driver(cfg.sensor, simulate=args.simulate, sim_speed=args.sim_speed)

    writer: Writer
    buffer: PointBuffer | None = None
    if args.dry_run:
        writer = _PrintWriter()
    else:
        if not cfg.influxdb.token:
            log.error("INFLUX_WRITE_TOKEN is not set (see /etc/serverroom-monitor/secrets.env)")
            return 2
        writer = InfluxWriter(cfg.influxdb)
        buffer = PointBuffer(cfg.buffer.path, cfg.buffer.max_points)

    display: Display | None = None
    if cfg.display.mode == "oled" and not args.simulate:
        from serverroom_monitor.display.oled import OledDisplay  # noqa: PLC0415

        display = OledDisplay(cfg.display.oled)

    stop = threading.Event()
    collector = Collector(
        cfg,
        driver,
        writer,
        buffer,
        heartbeat=Heartbeat(cfg.heartbeat) if not args.dry_run else None,
        notifier=SystemdNotifier(),
        display=display,
        stop=stop,
    )
    try:
        if args.once:
            result = collector.run_cycle()
            if result.reading:
                print(json.dumps(_reading_dict(result.reading)))
            return 0 if result.reading else 1
        install_signal_handlers(stop)
        collector.run_forever()
        return 0
    finally:
        collector.close()


def _reading_dict(r: object) -> dict[str, object]:
    import dataclasses  # noqa: PLC0415

    assert dataclasses.is_dataclass(r) and not isinstance(r, type)  # noqa: S101
    return dataclasses.asdict(r)


def cmd_read(args: argparse.Namespace) -> int:
    from serverroom_monitor.psychro import dew_point  # noqa: PLC0415
    from serverroom_monitor.sensors import SensorError, create_driver  # noqa: PLC0415
    from serverroom_monitor.validation import ReadingValidator  # noqa: PLC0415

    cfg = _load(args.config)
    driver = create_driver(cfg.sensor, simulate=args.simulate, sim_speed=args.sim_speed)
    validator = ReadingValidator(cfg.validation)
    failures = 0
    for i in range(args.count):
        if i:
            time.sleep(2)
        try:
            raw = driver.read()
        except SensorError as exc:
            failures += 1
            print(json.dumps({"ok": False, "error": str(exc)}))
            continue
        dp = dew_point(raw.temperature, raw.humidity)
        print(
            json.dumps(
                {
                    "ok": True,
                    "sensor": driver.model,
                    "temperature": round(raw.temperature, 2),
                    "humidity": round(raw.humidity, 2),
                    "dew_point": None if dp is None else round(dp, 2),
                    "bounds_error": validator.check_bounds(raw),
                }
            )
        )
    driver.close()
    return 1 if failures == args.count else 0


def cmd_check_config(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    n = cfg.notifications
    channels = [
        name
        for name, on in (
            ("telegram", n.telegram.enabled),
            ("email", n.email.enabled),
            ("webhook", n.webhook.enabled),
        )
        if on
    ]
    print(f"{args.config}: OK")
    print(f"  location:   {cfg.location}")
    s = cfg.sensor
    where = f"GPIO{s.gpio}" if s.model in ("dht11", "dht22") else f"0x{s.i2c_address:02x}"
    print(f"  sensor:     {s.model} @ {where}, every {s.poll_interval:g}s")
    print(f"  display:    {cfg.display.mode}")
    print(f"  channels:   {', '.join(channels) or 'NONE (alerts will not be delivered!)'}")
    print(f"  heartbeat:  {'configured' if cfg.heartbeat.enabled else 'not configured'}")
    print(f"  influx tok: {'set' if cfg.influxdb.token else 'not set in environment'}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    from serverroom_monitor.grafana.render import render_all  # noqa: PLC0415

    cfg = _load(args.config)
    for path in render_all(cfg, args.out, root_url=args.root_url, deploy_root=args.deploy_root):
        print(path)
    return 0


def cmd_backlight(args: argparse.Namespace) -> int:
    from serverroom_monitor.display.backlight import apply_backlight  # noqa: PLC0415

    cfg = _load(args.config)
    return apply_backlight(cfg.display)


def cmd_test_alerts(args: argparse.Namespace) -> int:
    from serverroom_monitor.alerttest import run_test_alerts  # noqa: PLC0415

    cfg = _load(args.config)
    return run_test_alerts(cfg, direct=args.direct, grafana_url=args.grafana_url)


def cmd_telegram_bot(args: argparse.Namespace) -> int:
    import os  # noqa: PLC0415
    import signal  # noqa: PLC0415
    from types import FrameType  # noqa: PLC0415

    from serverroom_monitor.telegram_bot import InfluxReader, StatusBot  # noqa: PLC0415

    cfg = _load(args.config)
    tg = cfg.notifications.telegram
    if not (tg.enabled and tg.commands):
        log.error("notifications.telegram.enabled and .commands must both be true")
        return 2
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    read_token = os.environ.get("INFLUX_READ_TOKEN", "")
    if not token or not read_token:
        log.error("TELEGRAM_BOT_TOKEN and INFLUX_READ_TOKEN must be set")
        return 2
    stop = threading.Event()

    # The bot holds no state, so exit at once instead of waiting for the
    # open long-poll request (up to ~50 s) to return.
    def _exit(signum: int, _frame: FrameType | None) -> None:
        log.info("received %s, shutting down", signal.Signals(signum).name)
        stop.set()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _exit)
    signal.signal(signal.SIGINT, _exit)
    StatusBot(cfg, token, InfluxReader(cfg.influxdb, read_token), stop=stop).run_forever()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Default to "run" so `serverroom-monitor --simulate` works as expected.
    if not any(a in SUBCOMMANDS for a in argv) and not {"-h", "--help", "--version"} & set(argv):
        # Insert after global options (-c PATH / --log-level LEVEL).
        i = 0
        while i < len(argv) and argv[i] in ("-c", "--config", "--log-level"):
            i += 2
        argv.insert(i, "run")
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    handlers = {
        "run": cmd_run,
        "read": cmd_read,
        "check-config": cmd_check_config,
        "render-grafana": cmd_render,
        "backlight": cmd_backlight,
        "test-alerts": cmd_test_alerts,
        "telegram-bot": cmd_telegram_bot,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
