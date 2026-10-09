#!/usr/bin/env python3
"""Capture the game window to a small BMP so it can be inspected directly.

Writing a downscaled BMP and reading it back as an image is far quicker than
inferring game state from memory. It is how the pause menu was identified: the
game was sitting in its pause overlay the whole time, so every differential
memory scan had been measuring menu-cursor animation rather than gameplay.

  peek.py [out.bmp] [width]
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import th06   # noqa: E402


def write_bmp(path, tw, th, rows):
    rowsize = (tw * 3 + 3) & ~3
    pad = b"\0" * (rowsize - tw * 3)
    data = b"".join(bytes(r) + pad for r in reversed(rows))
    hdr = (struct.pack("<2sIHHI", b"BM", 14 + 40 + len(data), 0, 0, 54)
           + struct.pack("<IiiHHIIiiII", 40, tw, th, 1, 24, 0, len(data),
                         2835, 2835, 0, 0))
    with open(path, "wb") as fh:
        fh.write(hdr + data)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "peek.bmp"
    tw = int(sys.argv[2]) if len(sys.argv) > 2 else 900

    hwnd = th06.find_game()[0]
    th06.focus(hwnd)
    time.sleep(0.5)
    w, h, raw, stride, _used, dark = th06.capture(hwnd)
    tw2, th, rows = th06.thumb(raw, w, h, stride, tw)
    write_bmp(out, tw2, th, rows)
    print("wrote %s (%dx%d, from %dx%d, dark_ratio=%.3f)"
          % (out, tw2, th, w, h, dark))


if __name__ == "__main__":
    main()
