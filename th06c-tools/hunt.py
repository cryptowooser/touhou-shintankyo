#!/usr/bin/env python3
"""Hunt for entity pools by diffing a live game.

With the player stationary, almost everything that changes between two
snapshots is bullets, enemies, animation phases and timers. Keeping only
plausible coordinate values, clustering by address, then histogramming the gaps
between movers separates a pool -- one dominant stride, many records -- from
scattered animation noise.

It prints a float dump for the busiest clusters so the record layout can be read
off directly, and it reports the changed-page count so a frozen game is obvious.

Read-only.

  hunt.py [interval] [--vmin A] [--vmax B] [--top N] [--dump N]
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
    delay = float(positional[0]) if positional else 0.3
    vmin = float(argval("--vmin", "0.01"))
    vmax = float(argval("--vmax", "1200.0"))
    top = int(argval("--top", "10"))
    ndump = int(argval("--dump", "3"))

    pid, h = M.open_game()
    th06.focus(th06.find_game()[0])
    time.sleep(0.5)

    a = snapshot(h)
    time.sleep(delay)
    b = snapshot(h)

    movers = []
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
            end = min(len(pa), len(pb)) - 4
            for o in range(0, end, 4):
                va = struct.unpack_from("<f", pa, o)[0]
                vb = struct.unpack_from("<f", pb, o)[0]
                if not (va == va and vb == vb):
                    continue
                if abs(va - vb) < 0.01:
                    continue
                if not (vmin <= abs(va) <= vmax and vmin <= abs(vb) <= vmax):
                    continue
                movers.append((base + poff + o, va, vb))

    print("changed pages: %d" % changed_pages)
    print("plausible-value movers: %d" % len(movers))
    if changed_pages == 0:
        print("nothing changed at all - the game is paused or not running")

    buckets = {}
    for addr, va, vb in movers:
        buckets.setdefault(addr >> 16, []).append((addr, va, vb))

    ranked = sorted(buckets.items(), key=lambda kv: -len(kv[1]))[:top]
    print("\nclusters (bucket, movers, span, dominant gaps):")
    for bucket, items in ranked:
        addrs = sorted(i[0] for i in items)
        gaps = {}
        for p, q in zip(addrs, addrs[1:]):
            d = q - p
            if 0x8 <= d <= 0x1000:
                gaps[d] = gaps.get(d, 0) + 1
        g = sorted(gaps.items(), key=lambda kv: -kv[1])[:5]
        print("  %012x0000  n=%-5d span=%#-7x gaps: %s"
              % (bucket, len(items), addrs[-1] - addrs[0],
                 ", ".join("%#x x%d" % t for t in g) or "-"))

    for bucket, items in ranked[:ndump]:
        items.sort()
        start = items[0][0] & ~0xF
        d = M.read_mem(h, start, 0x120)
        if not d:
            continue
        print("\n  dump %016x (%d movers), floats 4 per row:" % (start, len(items)))
        for i in range(0, 0x120, 16):
            v = [struct.unpack_from("<f", d, i + k)[0] for k in (0, 4, 8, 12)]
            print("    +%03x  %s" % (i, "  ".join("%13g" % x for x in v)))


if __name__ == "__main__":
    main()
