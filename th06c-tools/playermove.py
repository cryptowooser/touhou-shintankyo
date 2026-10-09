#!/usr/bin/env python3
"""Locate the player's position by driving the game and diffing.

Holding a direction key moves the player along one axis only, so its position
changes in x while y stays put. That is a far tighter signature than "a float
that changed": timers, velocities and animation counters do not move a
coordinate pair by a large amount along one axis while leaving the other fixed.

It doubles as a liveness test. If the game is paused (th06c pauses on focus
loss) the player does not move and this reports nothing, which distinguishes
"paused" from "the scan missed it".

Read-only apart from synthetic key input.

  playermove.py [key] [hold_ms]
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memedit as M   # noqa: E402
import th06           # noqa: E402

PAGE = 0x1000


def snapshot(h):
    snap = {}
    for base, size, _p, _t in M.regions(h, rw=True):
        d = M.read_mem(h, base, size)
        if d:
            snap[base] = d
    return snap


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else "right"
    hold_ms = int(sys.argv[2]) if len(sys.argv) > 2 else 700

    pid, h = M.open_game()
    th06.focus(th06.find_game()[0])
    time.sleep(0.5)
    print("holding %r for %d ms" % (key, hold_ms))

    a = snapshot(h)
    th06.hold(key, hold_ms)
    time.sleep(0.25)
    b = snapshot(h)

    hits = []
    for base, da in a.items():
        db = b.get(base)
        if db is None or len(db) != len(da):
            continue
        for poff in range(0, len(da), PAGE):
            pa = da[poff:poff + PAGE]
            pb = db[poff:poff + PAGE]
            if pa == pb:
                continue
            end = min(len(pa), len(pb)) - 8
            for o in range(0, end, 4):
                xa, ya = struct.unpack_from("<2f", pa, o)
                xb, yb = struct.unpack_from("<2f", pb, o)
                if not all(v == v for v in (xa, ya, xb, yb)):
                    continue
                if abs(yb - ya) > 0.01:
                    continue
                if abs(xb - xa) < 5.0:
                    continue
                if not (-200 <= xa <= 1000 and -200 <= xb <= 1000):
                    continue
                if not (-200 <= ya <= 1000):
                    continue
                hits.append((base + poff + o, xa, ya, xb, yb))

    print("coordinate pairs that moved along x only by >5: %d" % len(hits))
    for addr, xa, ya, xb, yb in hits[:40]:
        print("  %016x  x %10.4f -> %-10.4f  y %-10.4f  dx=%+.2f"
              % (addr, xa, xb, ya, xb - xa))
    if not hits:
        print("  nothing moved - the game is probably paused, or %r is bound "
              "differently" % key)


if __name__ == "__main__":
    main()
