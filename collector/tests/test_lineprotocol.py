import math

import pytest

from serverroom_monitor.lineprotocol import encode


def test_basic_point() -> None:
    line = encode(
        "environment",
        {"location": "rack-a", "sensor": "sht4x"},
        {"temperature": 22.5, "humidity": 41.0},
        1700000000,
    )
    assert (
        line == "environment,location=rack-a,sensor=sht4x temperature=22.5,humidity=41.0 1700000000"
    )


def test_escaping() -> None:
    line = encode("my measurement", {"location": "Room 1,a=b"}, {"msg": 'say "hi"'}, 1)
    assert line == r'my\ measurement,location=Room\ 1\,a\=b msg="say \"hi\"" 1'


def test_integers_booleans_and_none() -> None:
    line = encode("s", {}, {"ok": 1, "flag": True, "dp": None}, 5)
    assert line == "s ok=1i,flag=true 5"


def test_rejects_empty_and_nan() -> None:
    with pytest.raises(ValueError):
        encode("s", {}, {"a": None}, 1)
    with pytest.raises(ValueError):
        encode("s", {}, {"a": math.nan}, 1)


def test_empty_tag_values_are_dropped() -> None:
    assert encode("s", {"a": "", "b": "x"}, {"v": 1.5}, 1) == "s,b=x v=1.5 1"
