import importlib.util
import struct
from pathlib import Path

from .conftest import REPO


def load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("blank", REPO / "kiosk" / "make-blank-cursor.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_blank_cursor_is_valid_xcursor(tmp_path: Path) -> None:
    mod = load()
    data = mod.blank_cursor(24)
    magic, header, _version, ntoc = struct.unpack_from("<4sIII", data)
    assert (magic, header, ntoc) == (b"Xcur", 16, 1)
    _type, _subtype, pos = struct.unpack_from("<III", data, 16)
    fields = struct.unpack_from("<IIIIIIIII", data, pos)
    assert fields[3:5] == (1, 1)  # 1x1 image
    assert struct.unpack_from("<I", data, pos + 36)[0] >> 24 == 0  # fully transparent
    theme = mod.install(tmp_path)
    assert (theme / "cursors" / "left_ptr").read_bytes() == data
    assert (theme / "index.theme").exists()
