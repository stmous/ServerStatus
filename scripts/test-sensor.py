#!/usr/bin/env python3
"""One-shot sensor check for verifying the wiring.

    sudo /opt/serverroom-monitor/scripts/test-sensor.py            # uses config.yaml
    sudo /opt/serverroom-monitor/scripts/test-sensor.py --model sht4x --address 0x44
    sudo /opt/serverroom-monitor/scripts/test-sensor.py --model dht11 --gpio 4   # KY-015
    sudo /opt/serverroom-monitor/scripts/test-sensor.py --count 10

Does not need InfluxDB or the collector service (stop the service first if
you see I2C "resource busy" errors: sudo systemctl stop serverroom-monitor).
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import subprocess
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
VENV_PY = APP / "venv" / "bin" / "python"

# Re-exec inside the application venv when run with the system python.
if Path(sys.prefix).resolve() != (APP / "venv").resolve() and VENV_PY.exists():
    os.execv(str(VENV_PY), [str(VENV_PY), __file__, *sys.argv[1:]])  # noqa: S606
sys.path.insert(0, str(APP / "collector"))

from serverroom_monitor.config import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    SensorConfig,
    load_config,
)
from serverroom_monitor.psychro import dew_point  # noqa: E402
from serverroom_monitor.sensors import SensorError, create_driver  # noqa: E402

WIRING = """\
Expected wiring (sensor -> Raspberry Pi header):
  VIN -> pin 1 (3.3 V)     GND -> pin 6
  SDA -> pin 3 (GPIO2)     SCL -> pin 5 (GPIO3)"""

DHT_WIRING = """\
Expected wiring for a DHT11/KY-015 (module pin -> Raspberry Pi header):
  S (signal) -> pin 7 (GPIO4, or the GPIO set in sensor.gpio)
  + / VCC    -> pin 1 (3.3 V)  -- not 5 V: the module's pull-up would put 5 V on the GPIO
  - / GND    -> pin 9
DHT reads fail now and then (EIO/timeout); a few failures out of 5 are normal."""

IIO = Path("/sys/bus/iio/devices")


def dht_status() -> str:
    devices = [
        f"  {d.name}: {(d / 'name').read_text().strip()}"
        for d in sorted(IIO.glob("iio:device*"))
        if (d / "name").exists()
    ]
    return "\n".join(devices) or "  (no IIO devices: the dht11 overlay is not active)"


def i2cdetect() -> str:
    try:
        out = subprocess.run(
            ["i2cdetect", "-y", "1"], capture_output=True, text=True, timeout=10, check=False
        )
    except FileNotFoundError:
        return "(i2cdetect not installed: sudo apt install i2c-tools)"
    return out.stdout or out.stderr


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG_PATH)
    ap.add_argument("--model", choices=("sht4x", "sht31", "bme280", "dht11", "dht22"))
    ap.add_argument("--address", type=lambda v: int(v, 0))
    ap.add_argument("--gpio", type=int, help="BCM GPIO of a DHT sensor (default 4)")
    ap.add_argument("--count", type=int, default=3)
    args = ap.parse_args()

    if args.model:
        default_addr = {"sht4x": 0x44, "sht31": 0x44, "bme280": 0x76}.get(args.model, 0)
        sensor_cfg = SensorConfig(
            model=args.model, i2c_address=args.address or default_addr, gpio=args.gpio or 4
        )
    else:
        sensor_cfg = load_config(args.config, env={}).sensor
        if args.address or args.gpio:
            sensor_cfg = dataclasses.replace(
                sensor_cfg,
                i2c_address=args.address or sensor_cfg.i2c_address,
                gpio=args.gpio or sensor_cfg.gpio,
            )

    is_dht = sensor_cfg.model in ("dht11", "dht22")
    wiring = DHT_WIRING if is_dht else WIRING
    if is_dht:
        print(f"Sensor: {sensor_cfg.model} on GPIO{sensor_cfg.gpio} (kernel dht11 driver)\n")
        print("IIO devices:")
        print(dht_status() + "\n")
    else:
        print(f"Sensor: {sensor_cfg.model} at 0x{sensor_cfg.i2c_address:02x}\n")
        if not Path("/dev/i2c-1").exists():
            print("/dev/i2c-1 does not exist: I2C is disabled.")
            print("Enable it with: sudo raspi-config nonint do_i2c 0 && sudo reboot")
            return 1
        print("I2C bus 1 scan:")
        print(i2cdetect())

    driver = create_driver(sensor_cfg)
    ok = 0
    for i in range(args.count):
        if i:
            time.sleep(2)
        try:
            r = driver.read()
        except SensorError as exc:
            print(f"  read {i + 1}: FAILED - {exc}")
            continue
        ok += 1
        dp = dew_point(r.temperature, r.humidity)
        dp_text = f"{dp:.2f} °C" if dp is not None else "n/a"
        print(
            f"  read {i + 1}: {r.temperature:6.2f} °C  {r.humidity:6.2f} %RH  dew point {dp_text}"
        )
    driver.close()

    if ok == 0:
        print(f"\nNo successful reads.\n{wiring}")
        if not is_dht:
            print("If the address is missing from the scan above, check wiring and power;")
            print("if a different address shows up, set sensor.i2c_address in config.yaml.")
        return 1
    print(f"\n{ok}/{args.count} reads OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
