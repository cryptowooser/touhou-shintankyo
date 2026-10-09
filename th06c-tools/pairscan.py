#!/usr/bin/env python3
"""Find pools of (x, y) coordinate pairs and infer their stride.

An entity position is two adjacent floats that both sit inside the playfield.
Scanning changed pages for such pairs, then histogramming the gaps between
consecutive pairs, separates the shapes:

  * 0x1C and 0x70 gaps mean quad vertices (4 vertices per sprite), i.e. drawn
    output;
  * one dominant large gap means a pool of objects -- a shot or enemy array.

Only changed pages are scanned, which keeps the address space small enough to
walk in Python and biases the result toward live data.

Read-only. Focuses the game, since a paused game changes nothing.

  pairscan.py [interval] [--lo X] [--hi X] [--top N]
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memedit as M   # noqa: E402
import th06           # noqa: E402

PAGE = 0x1000


def argval(flag, default):
    if flag in sys.argv:
        return sys.argv[sys.argv.index(flag) + 1]
    return default


def snapshot(h):
    snap = {}
    for base, size, _p, _t in M.regions(h, rw=True):
        d = M.read_mem(h, base, size)
        if d:
            snap[base] = d
    return snap


def main():
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    delay = float(positional[0]) if positional else 0.35
    lo = float(argval("--lo", "0.0"))
    hi = float(argval("--hi", "700.0"))
    top = int(argval("--top", "12"))

    pid, h = M.open_game()
    th06.focus(th06.find_game()[0])
    time.sleep(0.6)
    a = snapshot(h)
    time.sleep(delay)
    b = snapshot(h)

    pairs = []
    changed_pages = 0
    for base, da in a.items():
        db = b.get(base)
        if db is None or len(db) != len(da):
            continue
        for poff in range(0, len(da), PAGE):
            pa = da[poff:poff + PAGE]
            pb = db[poff:poff + PAGE]
            if pa == pb:
                continue
            changed_pages += 1
            end = min(len(pa), len(pb)) - 8
            for o in range(0, end, 4):
                xa, ya = struct.unpack_from("<2f", pa, o)
                xb, yb = struct.unpack_from("<2f", pb, o)
                if not all(v == v for v in (xa, ya, xb, yb)):
                    continue
                if not all(lo <= v <= hi for v in (xa, ya, xb, yb)):
                    continue
                if xa == xb and ya == yb:
                    continue
                pairs.append((base + poff + o, xa, ya, xb, yb))

    print("changed pages: %d" % changed_pages)
    print("playfield (x, y) pairs that changed: %d" % len(pairs))

    buckets = {}
    for addr, *_ in pairs:
        buckets.setdefault(addr >> 16, []).append(addr)

    print("\nclusters:")
    for bucket, addrs in sorted(buckets.items(), key=lambda kv: -len(kv[1]))[:top]:
        addrs.sort()
        gaps = {}
        for p, q in zip(addrs, addrs[1:]):
            d = q - p
            if 0x10 <= d <= 0x800:
                gaps[d] = gaps.get(d, 0) + 1
        g = sorted(gaps.items(), key=lambda kv: -kv[1])[:4]
        print("  %012x0000  n=%-5d span=%#-7x gaps: %s"
              % (bucket, len(addrs), addrs[-1] - addrs[0],
                 ", ".join("%#x x%d" % t for t in g) or "-"))
        for addr in addrs[:3]:
            for a2, xa, ya, xb, yb in pairs:
                if a2 == addr:
                    print("      %016x  (%g, %g) -> (%g, %g)"
                          % (addr, xa, ya, xb, yb))
                    break


if __name__ == "__main__":
    main()
