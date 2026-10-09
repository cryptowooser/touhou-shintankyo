#!/usr/bin/env python3
"""Freeze the game, read a pool, then find where those same positions live.

The bullet render pool gives positions but is still the graphics layer. The sim
copy of a bullet holds the same (x, y), so searching memory for a position read
out of the render pool should land on it.

That search only works on a frozen game: a live bullet moves several pixels per
frame, so by the time the scan reaches its address the value has changed. Esc
opens the pause menu with the cursor already on Resume, so the freeze is safe
and reversible. This script pauses and reads but deliberately does NOT resume --
resuming needs Z, and a mis-selected menu item would discard the run, so that
decision is left to the caller after looking at pause.bmp.

Read-only apart from the single Esc keystroke.

  freezeread.py [--pool ADDR] [--stride N] [--nrec N] [--esc/--no-esc]
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memedit as M   # noqa: E402
import th06           # noqa: E402

POOL = 0x1304a600000
STRIDE = 0x30
NREC = 1536


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
    pool = int(argval("--pool", hex(POOL)), 16)
    stride = int(argval("--stride", hex(STRIDE)), 16)
    nrec = int(argval("--nrec", str(NREC)))
    do_esc = "--no-esc" not in sys.argv

    hwnd = th06.find_game()[0]
    th06.focus(hwnd)
    time.sleep(0.4)

    if do_esc:
        th06.tap("esc")
        time.sleep(0.8)

    w, h, raw, sa, _u, _d = th06.capture(hwnd)
    time.sleep(0.25)
    _w2, _h2, rb, sb, _u2, _d2 = th06.capture(hwnd)
    diff = sum(1 for y in range(0, h, 2)
               if raw[y * sa:y * sa + w * 4] != rb[y * sb:y * sb + w * 4])
    print("liveness after Esc: %d differing rows -> %s"
          % (diff, "STILL LIVE (bad)" if diff else "FROZEN (good)"))

    tw, th, trows = th06.thumb(raw, w, h, sa, 900)
    write_bmp("pause.bmp", tw, th, trows)
    print("wrote pause.bmp -- check the menu cursor before resuming")

    pid, hh = M.open_game()
    d = M.read_mem(hh, pool, stride * nrec)
    if not d or len(d) < stride * nrec:
        print("pool unreadable")
        return

    recs = [struct.unpack_from("<3f", d, i * stride) for i in range(nrec)]

    # Group consecutive records that share a position; the render pool emits a
    # fixed number of vertices per bullet.
    runs = []
    i = 0
    while i < nrec:
        j = i + 1
        while (j < nrec and recs[j][0] == recs[i][0]
               and recs[j][1] == recs[i][1]):
            j += 1
        runs.append((j - i, recs[i][0], recs[i][1]))
        i = j

    hist = {}
    for n, _x, _y in runs:
        hist[n] = hist.get(n, 0) + 1
    print("\nrun-length histogram (records sharing one position): %s"
          % ", ".join("%d x%d" % (k, hist[k]) for k in sorted(hist)))

    bullets = [(x, y) for n, x, y in runs if n == 6 and (x, y) != (0.0, 0.0)]
    print("bullets (6-vertex runs, non-zero position): %d" % len(bullets))
    for x, y in bullets[:10]:
        print("    %12g %12g" % (x, y))

    if not bullets:
        return

    # Search every RW region for the exact 8-byte (x, y) pair. The render pool
    # itself contains these, so its own range is excluded from the results.
    needles = {}
    for x, y in bullets:
        needles.setdefault(struct.pack("<2f", x, y), 0)
    print("\nsearching %d distinct (x,y) needles across RW memory..."
          % len(needles))

    hits = []
    for base, size, _p, _t in M.regions(hh, rw=True):
        data = M.read_mem(hh, base, size)
        if not data:
            continue
        for nd in needles:
            start = 0
            while True:
                k = data.find(nd, start)
                if k < 0:
                    break
                hits.append((base + k, nd))
                start = k + 1

    pool_lo, pool_hi = pool, pool + stride * nrec
    outside = [a for a, _nd in hits if not (pool_lo <= a < pool_hi)]
    print("total hits: %d  (%d inside the render pool, %d outside)"
          % (len(hits), len(hits) - len(outside), len(outside)))

    if not outside:
        print("\nno sim copy found: nothing outside the render pool holds these "
              "positions as an adjacent float pair.")
        return

    buckets = {}
    for a in outside:
        buckets.setdefault(a >> 16, []).append(a)
    print("\nregions holding the same positions (outside the render pool):")
    for bucket, addrs in sorted(buckets.items(), key=lambda kv: -len(kv[1]))[:12]:
        addrs.sort()
        gaps = {}
        for p, q in zip(addrs, addrs[1:]):
            gaps[q - p] = gaps.get(q - p, 0) + 1
        g = sorted(gaps.items(), key=lambda kv: -kv[1])[:4]
        print("  %012x0000  n=%-4d first=%016x gaps: %s"
              % (bucket, len(addrs), addrs[0],
                 ", ".join("%#x x%d" % t for t in g) or "-"))

    best = max(buckets.items(), key=lambda kv: len(kv[1]))[1]
    best.sort()
    start = best[0] & ~0xF
    dd = M.read_mem(hh, start, 0x100)
    print("\ndump around %016x (busiest region), floats 4 per row:" % start)
    for i in range(0, 0x100, 16):
        v = [struct.unpack_from("<f", dd, i + k)[0] for k in (0, 4, 8, 12)]
        print("  +%03x  %s" % (i, "  ".join("%13g" % x for x in v)))


if __name__ == "__main__":
    main()
