#!/usr/bin/env python3
"""Screen-space extraction: find bright sprites in the playfield and draw them.

This is the framebuffer route from the worklog: capture the window, crop the
playfield, threshold bright saturated pixels, take connected components, and
render the blobs as a simplified map. It reads no game memory, so it works while
paused and survives patches.

Read-only; PrintWindow means it does not steal focus.

Writes two images and a side-by-side comparison:
  <out>            the detected blobs (cyan dots on black)
  <out>.actual.bmp the same crop, downscaled, for eyeballing
  <out>.cmp.bmp    the two next to each other

  blobs.py [--out blobs.bmp] [--crop L,T,W,H] [--lum 140] [--sat 40]
           [--minarea 4] [--maxarea 400] [--ascii]
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import th06  # noqa: E402

# The game frame is 640x480 scaled 3x and centred in the 3440x1440 window; the
# playfield is the 384x448 box at (32,16) in that frame.
DEFAULT_CROP = (856, 48, 1152, 1344)
SCALE = 6  # full-res pixels per working pixel
TW, TH = 192, 224


def argval(flag, default):
    if flag in sys.argv:
        return sys.argv[sys.argv.index(flag) + 1]
    return default


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
    out = argval("--out", "blobs.bmp")
    crop = tuple(int(v) for v in argval(
        "--crop", ",".join(str(v) for v in DEFAULT_CROP)).split(","))
    lum_t = int(argval("--lum", "140"))
    sat_t = int(argval("--sat", "40"))
    minarea = int(argval("--minarea", "4"))
    maxarea = int(argval("--maxarea", "400"))

    hwnd = th06.find_game()[0]
    w, h, raw, stride, used, dark = th06.capture(hwnd)
    print("capture %dx%d via %s dark=%.3f" % (w, h, used, dark))

    x0, y0, cw, ch = crop

    mask = bytearray(TW * TH)
    actual = [bytearray(b"\0" * (TW * 3)) for _ in range(TH)]
    for y in range(TH):
        base = (y0 + y * SCALE) * stride
        row = y * TW
        arow = actual[y]
        for x in range(TW):
            o = base + (x0 + x * SCALE) * 3
            b, g, r = raw[o], raw[o + 1], raw[o + 2]
            arow[x * 3:x * 3 + 3] = bytes((b, g, r))
            v = (r * 299 + g * 587 + b * 114) // 1000
            if v < lum_t:
                continue
            if max(r, g, b) - min(r, g, b) < sat_t:
                continue
            mask[row + x] = 1

    # Connected components (4-neighbour, iterative flood fill).
    seen = bytearray(TW * TH)
    blobs = []
    for i in range(TW * TH):
        if not mask[i] or seen[i]:
            continue
        stack = [i]
        seen[i] = 1
        cells = []
        while stack:
            p = stack.pop()
            cells.append(p)
            px, py = p % TW, p // TW
            for nx, ny in ((px - 1, py), (px + 1, py), (px, py - 1),
                           (px, py + 1)):
                if 0 <= nx < TW and 0 <= ny < TH:
                    q = ny * TW + nx
                    if mask[q] and not seen[q]:
                        seen[q] = 1
                        stack.append(q)
        if minarea <= len(cells) <= maxarea:
            cx = sum(c % TW for c in cells) / len(cells)
            cy = sum(c // TW for c in cells) / len(cells)
            blobs.append((cx, cy, len(cells)))

    print("working grid %dx%d, %d blob(s) after area filter [%d,%d]"
          % (TW, TH, len(blobs), minarea, maxarea))

    blob_rows = [bytearray(b"\x10\x08\x04" * TW) for _ in range(TH)]
    for cx, cy, _a in blobs:
        ix, iy = int(cx), int(cy)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                nx, ny = ix + dx, iy + dy
                if 0 <= nx < TW and 0 <= ny < TH:
                    o = nx * 3
                    blob_rows[ny][o:o + 3] = b"\x40\xff\xff"

    write_bmp(out, TW, TH, [bytes(r) for r in blob_rows])
    write_bmp(out + ".actual.bmp", TW, TH, [bytes(r) for r in actual])
    print("wrote %s and %s.actual.bmp" % (out, out))

    gap = 6
    W = TW * 2 + gap
    cmp_rows = []
    for y in range(TH):
        cmp_rows.append(bytes(actual[y]) + b"\x20\x20\x20" * gap
                        + bytes(blob_rows[y]))
    write_bmp(out + ".cmp.bmp", W, TH, cmp_rows)
    print("wrote %s.cmp.bmp (left = screen, right = detected blobs)" % out)

    # Playfield coordinates: x 0..384 (left to right), y 0..448 (top to bottom).
    print("\nblob centroids (playfield units x, y, area):")
    for cx, cy, a in blobs:
        print("  %6.1f %6.1f  %d" % (cx / TW * 384, cy / TH * 448, a))

    if "--ascii" in sys.argv:
        GW, GH = 48, 28
        grid = [[" "] * GW for _ in range(GH)]
        for cx, cy, _a in blobs:
            gx, gy = int(cx / TW * GW), int(cy / TH * GH)
            if 0 <= gx < GW and 0 <= gy < GH:
                grid[gy][gx] = "*"
        for r in grid:
            print("  |" + "".join(r) + "|")


if __name__ == "__main__":
    main()
