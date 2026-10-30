#!/usr/bin/env python3
"""Install an invisible Xcursor theme ("serverroom-blank").

Wayland has no `unclutter`; the portable way to hide the pointer on a touch
kiosk (cage, labwc, wayfire) is a cursor theme whose images are a single
transparent pixel, selected with XCURSOR_THEME. Standard library only.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

NAMES = [
    "default",
    "left_ptr",
    "arrow",
    "top_left_arrow",
    "pointer",
    "hand",
    "hand1",
    "hand2",
    "pointing_hand",
    "text",
    "xterm",
    "ibeam",
    "vertical-text",
    "crosshair",
    "cross",
    "tcross",
    "watch",
    "wait",
    "progress",
    "left_ptr_watch",
    "help",
    "question_arrow",
    "move",
    "fleur",
    "all-scroll",
    "grab",
    "grabbing",
    "openhand",
    "closedhand",
    "dnd-move",
    "not-allowed",
    "no-drop",
    "forbidden",
    "circle",
    "copy",
    "alias",
    "context-menu",
    "cell",
    "col-resize",
    "row-resize",
    "sb_h_double_arrow",
    "sb_v_double_arrow",
    "e-resize",
    "w-resize",
    "n-resize",
    "s-resize",
    "ne-resize",
    "nw-resize",
    "se-resize",
    "sw-resize",
    "ew-resize",
    "ns-resize",
    "nesw-resize",
    "nwse-resize",
    "size_hor",
    "size_ver",
    "size_bdiag",
    "size_fdiag",
    "zoom-in",
    "zoom-out",
    "right_ptr",
    "center_ptr",
]

IMAGE_TYPE = 0xFFFD0002


def blank_cursor(size: int = 24) -> bytes:
    """A valid Xcursor file containing one fully transparent 1x1 image."""
    header = struct.pack("<4sIII", b"Xcur", 16, 0x10000, 1)
    toc = struct.pack("<III", IMAGE_TYPE, size, 16 + 12)
    image = struct.pack("<IIIIIIIII", 36, IMAGE_TYPE, size, 1, 1, 1, 0, 0, 0)
    pixels = struct.pack("<I", 0)  # ARGB, alpha 0
    return header + toc + image + pixels


def install(root: Path) -> Path:
    theme = root / "serverroom-blank"
    cursors = theme / "cursors"
    cursors.mkdir(parents=True, exist_ok=True)
    (theme / "index.theme").write_text(
        "[Icon Theme]\nName=serverroom-blank\nComment=Invisible cursor for kiosks\n"
    )
    data = blank_cursor()
    for name in NAMES:
        (cursors / name).write_bytes(data)
    return theme


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/usr/share/icons")
    print(install(target))
