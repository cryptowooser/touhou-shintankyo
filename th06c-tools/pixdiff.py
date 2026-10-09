#!/usr/bin/env python3
"""Prove whether the game is actually moving on screen, and where.

Memory scans have twice failed to find moving bullet coordinates. Before
trusting any of that, establish the ground truth: capture several frames and
compare them. If the playfield pixels are identical between frames the game is
frozen and every differential scan measured nothing. If they differ, the motion
exists and the failure is in the scan filters, not in the game.

Prints a per-pair differing-row count, then a coarse character map of where the
changes are, so bullet movement is visible directly.

Read-only.

  pixdiff.py [interval] [--frames N]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import th06   # noqa: E402


def argval(flag, default):
    if flag in sys.argv:
        return sys.argv[sys.argv.index(flag) + 1]
    return default


def main():
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    delay = float(positional[0]) if positional else 0.3
    nframes = int(argval("--frames", "4"))

    hwnd = th06.find_game()[0]
    th06.focus(hwnd)
    time.sleep(0.5)

    frames = []
    for _ in range(nframes):
        w, h, raw, stride, _used, dark = th06.capture(hwnd)
        frames.append((w, h, raw, stride))
        time.sleep(delay)

    w, h = frames[0][0], frames[0][1]
    print("captured %d frames, %dx%d, %gs apart" % (nframes, w, h, delay))

    for i in range(nframes - 1):
        _w, _h, ra, sa = frames[i]
        _w2, _h2, rb, sb = frames[i + 1]
        rows = 0
        for y in range(h):
            oa, ob = y * sa, y * sb
            if ra[oa:oa + w * 4] != rb[ob:ob + w * 4]:
                rows += 1
        print("  frame %d -> %d: %d of %d rows differ" % (i, i + 1, rows, h))

    # Coarse map of the first pair, subsampled 8 px in each direction.
    _w, _h, ra, sa = frames[0]
    _w2, _h2, rb, sb = frames[1]
    gw, gh = 48, 24
    cw, ch = w // gw, h // gh
    print("\nchange map for frame 0 -> 1  ('#' heavy, '+' some, '.' little):")
    for gy in range(gh):
        row = []
        for gx in range(gw):
            d = 0
            for y in range(gy * ch, min(h, (gy + 1) * ch), 8):
                oa, ob = y * sa, y * sb
                for x in range(gx * cw, min(w, (gx + 1) * cw), 8):
                    p = x * 4
                    if ra[oa + p:oa + p + 4] != rb[ob + p:ob + p + 4]:
                        d += 1
            row.append("#" if d > 40 else ("+" if d > 8 else ("." if d else " ")))
        print("  |" + "".join(row) + "|")


if __name__ == "__main__":
    main()
