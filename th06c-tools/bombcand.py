#!/usr/bin/env python3
"""Test whether a set of candidate addresses holds the bomb counter.

Two hypotheses are being separated:

* the star row is re-read every frame, so writing the counter changes it at
  once; or
* the star row is cached and only re-rendered when the game itself changes the
  count, in which case an external write is invisible until the player bombs.

So: write a large value (8) to every candidate, watch for 8 stars, and if
nothing appears, ask for one bomb press. If the counter is in the set, the press
drops it to 7 and the game re-renders -> 7 stars. If it is not, the press takes
the real count down instead.

  bombcand.py LOGFILE
"""
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hud           # noqa: E402
import memedit as M  # noqa: E402
import th06          # noqa: E402

PROBE = 8


def main():
    log = sys.argv[1]
    txt = open(log, encoding="utf-8", errors="replace").read()
    block = txt.split("=== survivors ===")[1].split("=== write-test")[0]
    cands = sorted({int(m.group(2), 16)
                    for m in re.finditer(r"width (\d+)\s+([0-9a-f]{16})",
                                         block)})
    print("loaded %d candidate address(es)" % len(cands), flush=True)

    hwnd = th06.find_game()[0]
    _, hw = M.open_game(write=True)

    def stars():
        return hud.count(hud.scan(hwnd)["bombs"])

    before = stars()
    print("HUD bombs before: %d" % before, flush=True)

    orig = {}
    for a in cands:
        v = M.read_int(hw, a, 4)
        if v is not None:
            orig[a] = v
    print("saved %d original value(s)" % len(orig), flush=True)

    stop = [False]

    def writer():
        pb = M.pack_int(PROBE, 4)
        while not stop[0]:
            for a in cands:
                M.write_mem(hw, a, pb)

    t = threading.Thread(target=writer, daemon=True)
    t.start()

    # Phase 1: does the display follow an external write with no press at all?
    seen = {}
    t0 = time.time()
    while time.time() - t0 < 4.0:
        n = stars()
        seen[n] = seen.get(n, 0) + 1
    print("phase 1 (holding %d, no press): star counts seen %s"
          % (PROBE, dict(sorted(seen.items()))), flush=True)
    if PROBE in seen:
        print("RESULT: display follows the write directly -> counter is in the set",
              flush=True)
    else:
        print("phase 1 inconclusive; now PRESS THE BOMB KEY ONCE "
              "(watching for %d stars)" % (PROBE - 1), flush=True)

    # Phase 2: wait for the player to bomb, which forces a re-render.
    seen2 = {}
    t0 = time.time()
    while time.time() - t0 < 60.0:
        n = stars()
        seen2[n] = seen2.get(n, 0) + 1
        if n >= 4:
            break
    stop[0] = True
    t.join(timeout=2.0)
    print("phase 2 (after press): star counts seen %s"
          % dict(sorted(seen2.items())), flush=True)
    if any(n >= 4 for n in seen2):
        print("RESULT: %s stars appeared -> counter IS in the set, "
              "and the row is cached" % [n for n in seen2 if n >= 4],
              flush=True)
    else:
        print("RESULT: no high count -> counter is NOT in this set", flush=True)

    for a, v in orig.items():
        M.write_mem(hw, a, M.pack_int(v, 4))
    print("restored %d address(es); HUD bombs now %d" % (len(orig), stars()),
          flush=True)


if __name__ == "__main__":
    main()
