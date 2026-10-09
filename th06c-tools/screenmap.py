#!/usr/bin/env python3
"""Map the playfield into two layers: small bright sprites (bullets) and large
bright structures (light beams, the enemy).

A single threshold plus connected components is not enough on its own: the
bullets and the light beams are both bright, but they differ in size. So the
mask is split by component area -- small ones become dots, large ones are drawn
as the filled shape they occupy. That keeps the beams in the map without
swallowing the bullets.

Read-only; PrintWindow means it does not steal focus.

Writes <out> (the map) and <out>.cmp.bmp (screen | map side by side).

  screenmap.py [--out map.bmp] [--crop L,T,W,H] [--lum 55] [--sat 20]
               [--big 60] [--scale 6] [--ascii]
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import th06  # noqa: E402

DEFAULT_CROP = (856, 48, 1152, 1344)


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
    out = argval("--out", "map.bmp")
    crop = tuple(int(v) for v in argval(
        "--crop", ",".join(str(v) for v in DEFAULT_CROP)).split(","))
    lum_t = int(argval("--lum", "55"))
    sat_t = int(argval("--sat", "20"))
    big_t = int(argval("--big", "60"))
    scale = int(argval("--scale", "6"))

    hwnd = th06.find_game()[0]
    w, h, raw, stride, used, dark = th06.capture(hwnd)
    x0, y0, cw, ch = crop
    TW, TH = cw // scale, ch // scale

    mask = bytearray(TW * TH)
    actual = [bytearray(b"\0" * (TW * 3)) for _ in range(TH)]
    for y in range(TH):
        base = (y0 + y * scale) * stride
        row = y * TW
        arow = actual[y]
        for x in range(TW):
            o = base + (x0 + x * scale) * 3
            b, g, r = raw[o], raw[o + 1], raw[o + 2]
            arow[x * 3:x * 3 + 3] = bytes((b, g, r))
            v = (r * 299 + g * 587 + b * 114) // 1000
            if v >= lum_t and max(r, g, b) - min(r, g, b) >= sat_t:
                mask[row + x] = 1

    # Connected components.
    seen = bytearray(TW * TH)
    comps = []
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
        comps.append(cells)

    small = [c for c in comps if len(c) < big_t]
    big = [c for c in comps if len(c) >= big_t]
    print("components: %d small (<%d), %d large (>=%d)"
          % (len(small), big_t, len(big), big_t))

    rows = [bytearray(b"\x10\x08\x04" * TW) for _ in range(TH)]
    for cells in big:
        for p in cells:
            o = (p % TW) * 3
            rows[p // TW][o:o + 3] = b"\x30\x90\xff"      # beams: orange-blue
    for cells in small:
        cx = int(sum(c % TW for c in cells) / len(cells))
        cy = int(sum(c // TW for c in cells) / len(cells))
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                nx, ny = cx + dx, cy + dy
                if 0 <= nx < TW and 0 <= ny < TH:
                    o = nx * 3
                    rows[ny][o:o + 3] = b"\x40\xff\xff"   # bullets: cyan

    write_bmp(out, TW, TH, [bytes(r) for r in rows])
    print("wrote %s (%dx%d): cyan dots = bullets, orange = large structures"
          % (out, TW, TH))

    gap = 6
    W = TW * 2 + gap
    cmp_rows = [bytes(actual[y]) + b"\x20\x20\x20" * gap + bytes(rows[y])
                for y in range(TH)]
    write_bmp(out + ".cmp.bmp", W, TH, cmp_rows)
    print("wrote %s.cmp.bmp (left = screen, right = map)" % out)

    print("\nlarge structures (playfield units x0,y0 - x1,y1, area):")
    for cells in sorted(big, key=len, reverse=True):
        xs = [c % TW for c in cells]
        ys = [c // TW for c in cells]
        print("  %6.1f %6.1f - %6.1f %6.1f  %d"
              % (min(xs) / TW * 384, min(ys) / TH * 448,
                 max(xs) / TW * 384, max(ys) / TH * 448, len(cells)))

    if "--ascii" in sys.argv:
        GW, GH = 48, 28
        grid = [[" "] * GW for _ in range(GH)]
        for cells in big:
            for p in cells:
                grid[int((p // TW) / TH * GH)][int((p % TW) / TW * GW)] = "#"
        for cells in small:
            cx = sum(c % TW for c in cells) / len(cells)
            cy = sum(c // TW for c in cells) / len(cells)
            grid[int(cy / TH * GH)][int(cx / TW * GW)] = "*"
        for r in grid:
            print("  |" + "".join(r) + "|")


if __name__ == "__main__":
    main()
