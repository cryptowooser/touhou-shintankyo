#!/usr/bin/env python3
"""Wait until the game is genuinely running, then scan it immediately.

Why this exists: the game auto-pauses and shows its pause menu whenever it
loses focus, and the terminal always has focus while these tools run. Two
earlier scans therefore measured a frozen game and reported "no pools" -- the
pause menu was on screen the whole time.

So the order is inverted. This script does not touch focus at all; it polls the
window with PrintWindow, which reads the frame without stealing focus. The
moment consecutive frames differ the game is live, and the scan starts at once.
Liveness is re-checked after the snapshots so a game that paused mid-scan is
reported rather than silently trusted.

Run it in the background, then bring the game to the front and resume it.

  livescan.py [--wait SEC] [--interval 0.3] [--vmin 0.5] [--vmax 500]
              [--top 12] [--dump 4] [--poll 0.3]
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


def live(hwnd, gap):
    """Return the number of differing rows between two frames `gap` apart."""
    _w, h, ra, sa, _u, _d = th06.capture(hwnd)
    time.sleep(gap)
    _w2, h2, rb, sb, _u2, _d2 = th06.capture(hwnd)
    if h != h2:
        return 1
    rows = 0
    for y in range(0, h, 2):
        oa, ob = y * sa, y * sb
        if ra[oa:oa + _w * 4] != rb[ob:ob + _w2 * 4]:
            rows += 1
    return rows


def snapshot(h):
    snap = {}
    for base, size, _p, _t in M.regions(h, rw=True):
        d = M.read_mem(h, base, size)
        if d:
            snap[base] = d
    return snap


def main():
    wait = float(argval("--wait", "600"))
    interval = float(argval("--interval", "0.3"))
    vmin = float(argval("--vmin", "0.5"))
    vmax = float(argval("--vmax", "500.0"))
    top = int(argval("--top", "12"))
    ndump = int(argval("--dump", "4"))
    poll = float(argval("--poll", "0.3"))

    hwnd = th06.find_game()[0]
    print("game window %#x; waiting up to %gs for it to go live" % (hwnd, wait),
          flush=True)
    print("(no focus is taken -- bring the game forward and resume it yourself)",
          flush=True)

    t0 = time.time()
    while True:
        rows = live(hwnd, poll)
        if rows:
            print("LIVE after %.1fs (%d differing rows)" % (time.time() - t0, rows),
                  flush=True)
            break
        if time.time() - t0 > wait:
            print("gave up after %gs: frames identical, game still paused" % wait,
                  flush=True)
            return
        if int(time.time() - t0) % 15 < 1:
            print("  still paused (%.0fs elapsed)" % (time.time() - t0),
                  flush=True)

    pid, h = M.open_game()

    a = snapshot(h)
    time.sleep(interval)
    b = snapshot(h)

    after = live(hwnd, 0.2)
    if after:
        print("liveness re-check after snapshots: LIVE (%d rows)" % after, flush=True)
    else:
        print("WARNING: frames identical after the snapshots -- the game paused "
              "mid-scan, results below are suspect", flush=True)

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

    print("\nchanged pages: %d" % changed_pages)
    print("movers passing %.3g <= |v| <= %.3g: %d" % (vmin, vmax, len(movers)))

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
        d = M.read_mem(h, start, 0x140)
        if not d:
            continue
        print("\n  dump %016x (%d movers), floats 4 per row:" % (start, len(items)))
        for i in range(0, 0x140, 16):
            v = [struct.unpack_from("<f", d, i + k)[0] for k in (0, 4, 8, 12)]
            print("    +%03x  %s" % (i, "  ".join("%13g" % x for x in v)))


if __name__ == "__main__":
    main()
