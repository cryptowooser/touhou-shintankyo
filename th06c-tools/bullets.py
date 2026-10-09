#!/usr/bin/env python3
"""Read enemy bullets straight out of the entity array and overlay them on screen.

Uses the addresses established statically and confirmed live:

  g_BulletManager  RVA 0x955410
  bullets[0]       +0x008   stride 0x5D0, 640 entries
  bulletCount      +0xE8808
  Bullet.pos       +0x030
  Bullet.state     +0x044   (0 inactive, 1 fired, 2..4 spawning, 5 despawning)
  Bullet.isGrazed  +0x5C8

The playfield is 384x448 units and the captured crop is 192x224 px, so one unit
is half a pixel. Which way y runs is not assumed -- both conventions are drawn
side by side so the right one can be read off.

  bullets.py            one frame
  bullets.py --watch N  N frames, printing the count each time
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memedit as M      # noqa: E402
import modbase           # noqa: E402
import th06              # noqa: E402

BM_RVA = 0x955410
BULLETS_OFF = 0x008
STRIDE = 0x5D0
COUNT = 640
BULLET_COUNT_OFF = 0xE8808
OFF_POS = 0x030
OFF_STATE = 0x044
OFF_GRAZE_SIZE = 0x5A4
OFF_GRAZED = 0x5C8

CROP = (856, 48)
SCALE = 6
TW, TH = 192, 224

STATE_NAME = {0: "inactive", 1: "fired", 2: "spawn-fast",
              3: "spawn-normal", 4: "spawn-slow", 5: "despawning"}


def read_bullets(h, base):
    bm = base + BM_RVA
    n = struct.unpack("<i", M.read_mem(h, bm + BULLET_COUNT_OFF, 4))[0]
    out = []
    for i in range(COUNT):
        a = bm + BULLETS_OFF + i * STRIDE
        st, = struct.unpack("<H", M.read_mem(h, a + OFF_STATE, 2))
        if st == 0:
            continue
        px, py, pz = struct.unpack("<3f", M.read_mem(h, a + OFF_POS, 12))
        gz, = struct.unpack("<B", M.read_mem(h, a + OFF_GRAZED, 1))
        sx, sy, _ = struct.unpack("<3f", M.read_mem(h, a + OFF_GRAZE_SIZE, 12))
        out.append({"idx": i, "state": st, "pos": (px, py, pz),
                    "grazed": gz, "size": (sx, sy), "addr": a})
    return n, out


def crop(hwnd):
    w, h, raw, stride, _u, _d = th06.capture(hwnd)
    x0, y0 = CROP
    rows = []
    for y in range(TH):
        row = bytearray(TW * 3)
        base = (y0 + y * SCALE) * stride
        for x in range(TW):
            o = base + (x0 + x * SCALE) * 3
            row[x * 3:x * 3 + 3] = raw[o:o + 3]
        rows.append(row)
    return rows


def write_bmp(path, tw, th, rows):
    rowsize = (tw * 3 + 3) & ~3
    pad = b"\0" * (rowsize - tw * 3)
    data = b"".join(bytes(r) + pad for r in reversed(rows))
    hdr = (struct.pack("<2sIHHI", b"BM", 14 + 40 + len(data), 0, 0, 54)
           + struct.pack("<IiiHHIIiiII", 40, tw, th, 1, 24, 0, len(data),
                         2835, 2835, 0, 0))
    open(path, "wb").write(hdr + data)


def mark(rows, bullets, ydown):
    out = [bytearray(r) for r in rows]
    for b in bullets:
        x, y, _z = b["pos"]
        px = int(x * 0.5)
        py = int(y * 0.5) if ydown else int((448.0 - y) * 0.5)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                nx, ny = px + dx, py + dy
                if 0 <= nx < TW and 0 <= ny < TH:
                    out[ny][nx * 3:nx * 3 + 3] = b"\x00\xff\x00"
    return out


def main():
    watch = 1
    if "--watch" in sys.argv:
        watch = int(sys.argv[sys.argv.index("--watch") + 1])
    pid, h = modbase.open_game()
    _name, base, _size = modbase.find_module(pid, "th06c")
    hwnd = th06.find_game()[0]

    for f in range(watch):
        n, bullets = read_bullets(h, base)
        print("frame %d: bulletCount=%d, active=%d" % (f, n, len(bullets)))
        if bullets and f == watch - 1:
            for b in bullets[:12]:
                print("   [%3d] %-12s pos=(%8.2f,%8.2f) size=(%.1f,%.1f) grazed=%d"
                      % (b["idx"], STATE_NAME.get(b["state"], "?"),
                         b["pos"][0], b["pos"][1], b["size"][0], b["size"][1],
                         b["grazed"]))
            rows = crop(hwnd)
            panels = [rows, mark(rows, bullets, True), mark(rows, bullets, False)]
            gap = 6
            joined = [(b"\x20\x20\x20" * gap).join(bytes(p[y]) for p in panels)
                      for y in range(TH)]
            write_bmp("bullets.bmp", TW * len(panels) + gap * (len(panels) - 1),
                      TH, joined)
            print("   wrote bullets.bmp (screen | y-down | y-up)")
        if f != watch - 1:
            time.sleep(0.25)


if __name__ == "__main__":
    main()
