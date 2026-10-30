import time
from pathlib import Path

from serverroom_monitor.config import parse_config
from serverroom_monitor.display.backlight import apply_backlight, target_percent


def make_device(root: Path, name: str = "10-0045", max_b: int = 255, cur: int = 255) -> Path:
    dev = root / name
    dev.mkdir(parents=True)
    (dev / "max_brightness").write_text(f"{max_b}\n")
    (dev / "brightness").write_text(f"{cur}\n")
    (dev / "bl_power").write_text("0\n")
    return dev


def at(hour: int, minute: int = 0) -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))


def cfg(**hdmi: object):  # type: ignore[no-untyped-def]
    return parse_config({"display": {"mode": "hdmi", "hdmi": hdmi}}, env={}).display


def test_day_and_night_levels(tmp_path: Path) -> None:
    dev = make_device(tmp_path)
    c = cfg(brightness=80, night="22:00-07:00", night_brightness=10)
    assert target_percent(c, at(12)) == 80
    assert target_percent(c, at(23)) == 10
    assert apply_backlight(c, tmp_path, now=at(12)) == 0
    assert (dev / "brightness").read_text().strip() == "204"
    apply_backlight(c, tmp_path, now=at(2))
    assert (dev / "brightness").read_text().strip() == "26"


def test_touch_display_2_scale_and_off(tmp_path: Path) -> None:
    dev = make_device(tmp_path, "11-0045", max_b=31, cur=31)
    apply_backlight(cfg(brightness=1), tmp_path, now=at(12))
    assert (dev / "brightness").read_text().strip() == "1"  # never rounds down to dark
    apply_backlight(cfg(brightness=0), tmp_path, now=at(12))
    assert (dev / "bl_power").read_text().strip() == "4"
    apply_backlight(cfg(brightness=50), tmp_path, now=at(12))
    assert (dev / "bl_power").read_text().strip() == "0"
    assert (dev / "brightness").read_text().strip() == "16"


def test_noop_without_device_or_other_mode(tmp_path: Path) -> None:
    assert apply_backlight(cfg(), tmp_path) == 0
    make_device(tmp_path)
    oled = parse_config({"display": {"mode": "oled"}}, env={}).display
    assert apply_backlight(oled, tmp_path) == 0
