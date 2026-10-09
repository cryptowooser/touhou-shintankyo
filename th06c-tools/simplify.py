#!/usr/bin/env python3
"""Capture the game window and write a simplified (downscaled) image.

Read-only, and it uses PrintWindow, so it never steals focus from the game.
Useful for eyeballing what the game actually shows right now without a 14 MB
BMP in the way.

  simplify.py [out.bmp] [--w 160] [--crop L,T,W,H] [--ascii]
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import th06  # noqa: E402


def write_bmp(path, tw, th, rows):
    rowsize = (tw * 3 + 3) & ~3
    pad = b"\0" * (rowsize - tw * 3)
    data = b"".join(bytes(r) + pad for r in reversed(rows))
    hdr = (struct.pack("<2sIHHI", b"BM", 14 + 40 + len(data), 0, 0, 54)
           + struct.pack("<IiiHHIIiiII", 40, tw, th, 1, 24, 0, len(data),
                         2835, 2835, 0, 0))
    with open(path, "wb") as fh:
        fh.write(hdr + data)


def argval(flag, default):
    if flag in sys.argv:
        return sys.argv[sys.argv.index(flag) + 1]
    return default


def main():
    out = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].endswith(".bmp") \
        else "simple.bmp"
    tw = int(argval("--w", "160"))
    crop = argval("--crop", None)
    want_ascii = "--ascii" in sys.argv

    hwnd = th06.find_game()[0]
    w, h, raw, stride, used, dark = th06.capture(hwnd)
    print("capture %dx%d via %s dark=%.3f" % (w, h, used, dark))

    x0, y0, cw, ch = 0, 0, w, h
    if crop:
        x0, y0, cw, ch = (int(v) for v in crop.split(","))

    # Nearest-neighbour downscale of the crop region.
    th = max(1, int(ch * tw / cw))
    rows = []
    for y in range(th):
        sy = y0 + min(ch - 1, y * ch // th)
        base = sy * stride
        row = bytearray()
        for x in range(tw):
            sx = x0 + min(cw - 1, x * cw // tw)
            o = base + sx * 3
            row += raw[o:o + 3]
        rows.append(bytes(row))
    write_bmp(out, tw, th, rows)
    print("wrote %s (%dx%d)" % (out, tw, th))

    if want_ascii:
        ramp = " .:-=+*#%@"
        for y in range(th):
            line = []
            for x in range(tw):
                b, g, r = rows[y][x * 3:x * 3 + 3]
                v = (r * 299 + g * 587 + b * 114) // 1000
                line.append(ramp[v * (len(ramp) - 1) // 255])
            print("|" + "".join(line) + "|")


if __name__ == "__main__":
    main()
