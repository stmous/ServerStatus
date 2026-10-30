from __future__ import annotations

from PIL import Image

from serverroom_monitor.config import Thresholds
from serverroom_monitor.display.oled import OledRenderer, ScreenState
from serverroom_monitor.levels import classify
from serverroom_monitor.models import AlertLevel, Reading


def reading(t: float, h: float) -> Reading:
    return Reading(timestamp=1_800_000_000.0, temperature=t, humidity=h, dew_point=9.6)


def test_classify_levels() -> None:
    th = Thresholds()
    assert classify(None, th) is AlertLevel.UNKNOWN
    assert classify(reading(22, 45), th) is AlertLevel.OK
    assert classify(reading(27.5, 45), th) is AlertLevel.WARNING
    assert classify(reading(30.5, 45), th) is AlertLevel.CRITICAL
    assert classify(reading(22, 24), th) is AlertLevel.WARNING
    assert classify(reading(22, 19), th) is AlertLevel.CRITICAL
    assert classify(reading(22, 61), th) is AlertLevel.WARNING
    assert classify(reading(22, 71), th) is AlertLevel.CRITICAL
    # Exactly at the threshold is not yet a breach (rules use "above").
    assert classify(reading(27.0, 60.0), th) is AlertLevel.OK


def _lit(img: Image.Image) -> int:
    return sum(img.histogram()[1:])


def test_render_produces_correct_size_and_content() -> None:
    r = OledRenderer()
    state = ScreenState(reading(23.4, 41.2), AlertLevel.OK, True, "192.168.1.20", 1_800_000_030.0)
    img = r.render(state)
    assert img.size == (128, 64)
    assert img.mode == "1"
    assert _lit(img) > 200


def test_render_variants_do_not_crash_and_differ() -> None:
    r = OledRenderer()
    ok = r.render(ScreenState(reading(23.4, 41.2), AlertLevel.OK, True, "10.0.0.2", 1.8e9))
    crit = r.render(ScreenState(reading(31.0, 41.2), AlertLevel.CRITICAL, True, "10.0.0.2", 1.8e9))
    err = r.render(ScreenState(None, AlertLevel.UNKNOWN, False, None, 1.8e9))
    assert ok.tobytes() != crit.tobytes()
    assert _lit(crit) > _lit(ok)  # inverted badge
    assert _lit(err) > 0


def test_pixel_shift_moves_image() -> None:
    r = OledRenderer()
    s = ScreenState(reading(23.4, 41.2), AlertLevel.OK, True, "10.0.0.2", 1.8e9)
    assert r.render(s, (0, 0)).tobytes() != r.render(s, (1, 1)).tobytes()
