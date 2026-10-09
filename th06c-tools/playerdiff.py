#!/usr/bin/env python3
"""Isolate the player's coordinate with an out-and-back differential.

Holding Right then Left moves the player one way and back. Anything else that
changes during the same window -- camera matrices, animation phases, double
buffers, profiler counters -- does not reverse direction. So keeping only values
whose delta flips sign and roughly cancels leaves the player's coordinate.

This is the same technique that pinned the lives and bombs counters: apply a
known change and keep only what follows it.

Read-only apart from synthetic key input.

  playerdiff.py [hold_ms] [--min-delta X]
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
    hold_ms = int(positional[0]) if positional else 700
    min_delta = float(argval("--min-delta", "10.0"))
    max_delta = float(argval("--max-delta", "250.0"))
    vmax = float(argval("--vmax", "600.0"))

    pid, h = M.open_game()
    th06.focus(th06.find_game()[0])
    time.sleep(0.5)

    print("A: rest")
    a = snapshot(h)
    print("B: holding right %d ms" % hold_ms)
    th06.hold("right", hold_ms)
    time.sleep(0.3)
    b = snapshot(h)
    print("C: holding left %d ms" % hold_ms)
    th06.hold("left", hold_ms)
    time.sleep(0.3)
    c = snapshot(h)

    hits = []
    for base, da in a.items():
        db = b.get(base)
        dc = c.get(base)
        if db is None or dc is None or len(db) != len(da) or len(dc) != len(da):
            continue
        for poff in range(0, len(da), PAGE):
            pa = da[poff:poff + PAGE]
            pb = db[poff:poff + PAGE]
            pc = dc[poff:poff + PAGE]
            if pa == pb == pc:
                continue
            end = min(len(pa), len(pb), len(pc)) - 4
            for o in range(0, end, 4):
                va = struct.unpack_from("<f", pa, o)[0]
                vb = struct.unpack_from("<f", pb, o)[0]
                vc = struct.unpack_from("<f", pc, o)[0]
                if not all(v == v for v in (va, vb, vc)):
                    continue
                d1 = vb - va
                d2 = vc - vb
                if d1 * d2 >= 0:
                    continue
                if min(abs(d1), abs(d2)) < min_delta:
                    continue
                if max(abs(d1), abs(d2)) > max_delta:
                    continue
                if max(abs(va), abs(vb), abs(vc)) > vmax:
                    continue
                if abs(d1 + d2) > 0.35 * max(abs(d1), abs(d2)):
                    continue
                hits.append((base + poff + o, va, vb, vc, d1, d2))

    print("\nvalues that moved out and back by >%g: %d" % (min_delta, len(hits)))
    for addr, va, vb, vc, d1, d2 in hits[:40]:
        print("  %016x  %10.4f -> %-10.4f -> %-10.4f  (%+.2f / %+.2f)"
              % (addr, va, vb, vc, d1, d2))
    if not hits:
        print("  nothing reversed - the player did not move, or the bounds were "
              "hit")


if __name__ == "__main__":
    main()
