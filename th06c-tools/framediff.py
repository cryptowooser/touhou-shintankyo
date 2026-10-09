#!/usr/bin/env python3
"""Whole-memory frame differ, filtered to moving coordinates.

Entity positions change every frame, so reading every writable region twice and
diffing finds them without knowing any structure in advance. Two filters make
the result readable:

  * only 4-byte-aligned float32 values that look like coordinates in BOTH
    snapshots are kept, which discards timers, flags and animation indices;
  * the spacing between consecutive surviving addresses is histogrammed, and an
    entity pool shows up as one dominant difference (its struct stride).

Read-only against the game. The game must be running and unpaused while this
runs, or nothing will move.

  framediff.py [delay_seconds] [--min F] [--max F]
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memedit as M   # noqa: E402

PAGE = 0x1000


def snapshot(h):
    snap = {}
    for base, size, _prot, _typ in M.regions(h, rw=True):
        d = M.read_mem(h, base, size)
        if d:
            snap[base] = d
    return snap


def argval(flag, default):
    if flag in sys.argv:
        return sys.argv[sys.argv.index(flag) + 1]
    return default


def main():
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    delay = float(positional[0]) if positional else 2.0
    lo = float(argval("--min", "0.5"))
    hi = float(argval("--max", "3000"))

    pid, h = M.open_game()
    a = snapshot(h)
    total = sum(len(v) for v in a.values())
    print("snapshot A: %d regions, %.1f MiB" % (len(a), total / 1048576))
    print("waiting %.1fs -- the game must be unpaused and moving" % delay)
    time.sleep(delay)
    b = snapshot(h)

    changed_pages = 0
    hits = []           # (addr, floatA, floatB)
    page_owner = {}     # 64 KiB bucket -> count of float hits
    for base, da in a.items():
        db = b.get(base)
        if db is None or len(db) != len(da):
            continue
        n = len(da)
        for poff in range(0, n, PAGE):
            pa = da[poff:poff + PAGE]
            pb = db[poff:poff + PAGE]
            if pa == pb:
                continue
            changed_pages += 1
            end = min(len(pa), len(pb)) & ~3
            for o in range(0, end, 4):
                if pa[o:o + 4] == pb[o:o + 4]:
                    continue
                fa = struct.unpack_from("<f", pa, o)[0]
                fb = struct.unpack_from("<f", pb, o)[0]
                if fa != fa or fb != fb:
                    continue
                if lo <= abs(fa) <= hi and lo <= abs(fb) <= hi:
                    addr = base + poff + o
                    hits.append((addr, fa, fb))
                    bucket = (base + poff) >> 16
                    page_owner[bucket] = page_owner.get(bucket, 0) + 1

    print("changed pages: %d" % changed_pages)
    print("coordinate-like floats that moved: %d" % len(hits))
    print("\ntop 64 KiB buckets by hit count:")
    for bucket, cnt in sorted(page_owner.items(), key=lambda kv: -kv[1])[:12]:
        print("  %012x0000  %d" % (bucket, cnt))

    hits.sort()
    deltas = {}
    for (a1, _f1, _g1), (a2, _f2, _g2) in zip(hits, hits[1:]):
        d = a2 - a1
        if 0 < d <= 0x1000:
            deltas[d] = deltas.get(d, 0) + 1
    print("\nmost common spacings between hits (candidate struct strides):")
    for d, cnt in sorted(deltas.items(), key=lambda kv: -kv[1])[:12]:
        print("  %#06x  x%d" % (d, cnt))

    print("\nfirst 40 hits:")
    for addr, fa, fb in hits[:40]:
        print("  %016x  %g -> %g" % (addr, fa, fb))


if __name__ == "__main__":
    main()
