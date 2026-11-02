"""Tests for scripts/lib.sh (secrets handling and config access from bash)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import REPO

LIB = REPO / "scripts" / "lib.sh"


def bash(script: str, tmp_path: Path, **extra_env: str) -> subprocess.CompletedProcess[str]:
    app = tmp_path / "app"
    (app / "venv" / "bin").mkdir(parents=True, exist_ok=True)
    py = app / "venv" / "bin" / "python"
    if not py.exists():
        py.symlink_to(sys.executable)
    env = {
        **os.environ,
        "APP_DIR": str(app),
        "ETC_DIR": str(tmp_path / "etc"),
        "PYTHONPATH": str(REPO / "collector"),
        **extra_env,
    }
    (tmp_path / "etc").mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail; . {LIB}; chown() {{ :; }}; {script}"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


TRICKY = [
    "https://hooks.slack.com/services/T0/B0/xyz?a=1&b=2",
    'pa ss$word`x` \\back"slash',
    "123456:ABC-def_GHI",
    "a=b=c",
]


@pytest.mark.parametrize("value", TRICKY)
def test_secret_roundtrip(tmp_path: Path, value: str) -> None:
    result = bash(
        'secret_set WEBHOOK_URL "$V"; secret_set OTHER keep; secret_set WEBHOOK_URL "$V"; '
        "secret_get WEBHOOK_URL; echo; secret_get OTHER",
        tmp_path,
        V=value,
    )
    assert result.returncode == 0, result.stderr
    first, second = result.stdout.split("\n", 1)
    assert first == value
    assert second.strip() == "keep"
    secrets = tmp_path / "etc" / "secrets.env"
    assert secrets.read_text().count("WEBHOOK_URL=") == 1  # replaced, not duplicated
    assert secrets.stat().st_mode & 0o777 == 0o600


def test_single_quote_rejected(tmp_path: Path) -> None:
    r = bash('secret_set SMTP_PASSWORD "it\'s"', tmp_path)
    assert r.returncode != 0
    assert "single quote" in r.stderr


def test_service_envs_split_secrets(tmp_path: Path) -> None:
    r = bash(
        "secret_set INFLUX_WRITE_TOKEN w; secret_set INFLUX_READ_TOKEN r; "
        "secret_set GRAFANA_ADMIN_PASSWORD g; secret_set TELEGRAM_BOT_TOKEN t; "
        'install() { mkdir -p "${@: -1}"; }; write_service_envs',
        tmp_path,
    )
    assert r.returncode == 0, r.stderr
    collector = (tmp_path / "etc" / "env" / "collector.env").read_text()
    grafana = (tmp_path / "etc" / "env" / "grafana.env").read_text()
    assert "INFLUX_WRITE_TOKEN='w'" in collector
    assert "GRAFANA" not in collector
    assert "TELEGRAM" not in collector
    assert "GF_SECURITY_ADMIN_PASSWORD='g'" in grafana
    assert "INFLUX_READ_TOKEN='r'" in grafana
    assert "WRITE" not in grafana


def test_cfg_reads_validated_values(tmp_path: Path) -> None:
    cfg = tmp_path / "etc" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("display: {mode: oled}\nfirewall: {lan_subnets: [10.0.0.0/24, 10.1.0.0/24]}\n")
    r = bash(
        "cfg display.mode; cfg grafana.lan_access; cfg firewall.lan_subnets; "
        "cfg sensor.i2c_address; cfg influxdb.retention_raw",
        tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.split("\n")[:5] == ["oled", "0", "10.0.0.0/24 10.1.0.0/24", "68", "2592000"]


MARKER = "# serverroom-monitor: DHT sensor (managed, do not edit)"


def test_dht_overlay_sync(tmp_path: Path) -> None:
    boot = tmp_path / "config.txt"
    boot.write_text("dtparam=audio=on\n[cm4]\notg_mode=1\n[all]\n")
    cfg = tmp_path / "etc" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    env = {"BOOT_CONFIG_PATH": str(boot), "REBOOT_FLAG": str(tmp_path / "reboot")}

    def sync(sensor: str) -> str:
        cfg.write_text(f"sensor: {sensor}\n")
        r = bash("sync_dht_overlay", tmp_path, **env)
        assert r.returncode == 0, r.stderr
        return boot.read_text()

    text = sync("{model: dht11, gpio: 4}")
    assert text.endswith(f"[all]\n{MARKER}\ndtoverlay=dht11,gpiopin=4\n")
    assert (tmp_path / "reboot").exists()

    (tmp_path / "reboot").unlink()
    assert sync("{model: dht11, gpio: 4}") == text  # idempotent
    assert not (tmp_path / "reboot").exists()

    text = sync("{model: dht22, gpio: 17}")
    assert text.count(MARKER) == 1
    assert "gpiopin=17" in text
    assert "gpiopin=4" not in text

    text = sync("{model: sht4x}")
    assert MARKER not in text
    assert "dtoverlay=dht11" not in text
    assert text.startswith("dtparam=audio=on\n[cm4]\notg_mode=1\n[all]\n")
