#!/usr/bin/env python3
"""Frame-diff, then infer each cluster's record layout.

`framediff.py` reports where coordinates move but not what shape they form. This
groups the moving coordinates by 64 KiB bucket, and for each bucket works out the
record period by looking at the differences between consecutive moving offsets:
the true struct stride dominates once intra-record gaps are ignored.

A clean, large period with a handful of moving fields per record means a pool of
objects. A small period (0x1c, 0x70) repeated thousands of times means vertex or
quad geometry, which is drawn output rather than entity state.

Read-only. The game must be unpaused and moving.

  poolscan.py [delay] [--top N] [--min-hits N]
"""
import os
import struct
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memedit as M   # noqa: E402

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
    delay = float(positional[0]) if positional else 1.5
    top = int(argval("--top", "10"))
    min_hits = int(argval("--min-hits", "40"))

    pid, h = M.open_game()
    a = snapshot(h)
    print("snapshot A: %.1f MiB; waiting %.1fs (game must be moving)"
          % (sum(len(v) for v in a.values()) / 1048576, delay))
    time.sleep(delay)
    b = snapshot(h)

    buckets = {}
    for base, da in a.items():
        db = b.get(base)
        if db is None or len(db) != len(da):
            continue
        for poff in range(0, len(da), PAGE):
            pa = da[poff:poff + PAGE]
            pb = db[poff:poff + PAGE]
            if pa == pb:
                continue
            for o in range(0, min(len(pa), len(pb)) & ~3, 4):
                if pa[o:o + 4] == pb[o:o + 4]:
                    continue
                fa = struct.unpack_from("<f", pa, o)[0]
                fb = struct.unpack_from("<f", pb, o)[0]
                if fa != fa or fb != fb:
                    continue
                if not (0.5 <= abs(fa) <= 3000 and 0.5 <= abs(fb) <= 3000):
                    continue
                buckets.setdefault((base + poff) >> 16, []).append(
                    (base + poff + o, fa, fb))

    print("%d buckets with moving coordinates\n" % len(buckets))
    ranked = sorted(buckets.items(), key=lambda kv: -len(kv[1]))[:top]
    for bucket, hits in ranked:
        hits.sort()
        addrs = [x[0] for x in hits]
        # Candidate period: the most common gap above the intra-record noise.
        gaps = Counter()
        for p, q in zip(addrs, addrs[1:]):
            d = q - p
            if 0x10 <= d <= 0x800:
                gaps[d] += 1
        period = gaps.most_common(3)
        base_addr = bucket << 16
        print("bucket %012x  hits=%-5d  span=%#x" % (base_addr, len(hits),
                                                     addrs[-1] - addrs[0]))
        print("   top gaps: " + ", ".join("%#x x%d" % g for g in period))
        # Show the fields within one period for the most common stride.
        if period:
            stride = period[0][0]
            intra = Counter((x - addrs[0]) % stride for x in addrs[:200])
            print("   intra-record offsets (mod %#x): %s"
                  % (stride, ", ".join("%#x x%d" % kv for kv in
                                       intra.most_common(6))))
        for addr, fa, fb in hits[:3]:
            print("     %016x  %g -> %g" % (addr, fa, fb))
        print()


if __name__ == "__main__":
    main()
