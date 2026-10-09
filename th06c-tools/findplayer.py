#!/usr/bin/env python3
"""Locate a live player struct in th06c by read-only memory differencing.

STATUS: written on a false premise. Its docstring originally claimed the lives
and bombs globals are per-frame mirrors that overwrite any write. That was wrong.
The globals are authoritative and writable; the "revert" was a bug in the caller
(a process handle opened without PROCESS_VM_WRITE, so every write failed
silently). This tool found zero survivors for exactly that reason. Kept only
because the differencing technique is sound and may be useful later.

Use gamestate.py to read and write lives/bombs (RVA 0x3EE804 / 0x3EE805).

The original approach: the game copies lives/bombs between globals and player
records at RVA 0x30837/0x3084D and 0x4D67D, reading bytes +9 and +0xA of an
object reached through an array. Scanning for the byte pair (lives, bombs) at
offset +9 matches thousands of unrelated addresses, so this narrowed them
without writing to the game:

  1. snapshot every 8-byte-aligned address whose +9/+0xA bytes equal the current
     lives/bombs pair,
  2. wait for you to change one of them in-game (press a bomb),
  3. rescan and intersect.

  findplayer.py          # snapshot, wait for a change, intersect
  findplayer.py --scan   # just snapshot and report the count
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memedit as M   # noqa: E402
import modbase as MB  # noqa: E402

LIVES_RVA = 0x3EE804
BOMBS_RVA = 0x3EE805


def globals_now(h, base):
    return (M.read_int(h, base + LIVES_RVA, 1),
            M.read_int(h, base + BOMBS_RVA, 1))


def candidates(h, lives, bombs):
    """Every 8-byte-aligned address with (lives, bombs) at +9/+0xA."""
    pat = bytes([lives, bombs])
    out = []
    for a, sz, _prot, _typ in M.regions(h):
        buf = M.read_mem(h, a, sz)
        if not buf or len(buf) < 16:
            continue
        pos = 0
        while True:
            k = buf.find(pat, pos)
            if k < 0:
                break
            pos = k + 1
            obj = a + k - 9
            if obj >= a and obj % 8 == 0:
                out.append(obj)
    return out


def main():
    pid, h = M.open_game()
    _name, base, _size = MB.find_module(pid, "th06c")
    lives, bombs = globals_now(h, base)
    print("current globals: lives=%s bombs=%s" % (lives, bombs))

    first = candidates(h, lives, bombs)
    print("scan 1: %d candidate(s) with (%s,%s) at +9/+0xA"
          % (len(first), lives, bombs))
    if "--scan" in sys.argv:
        return

    print("\nNow change it in-game: press a bomb (or die). Watching the globals...")
    t0 = time.time()
    while time.time() - t0 < 300:
        now = globals_now(h, base)
        if now != (lives, bombs):
            break
        time.sleep(0.25)
    else:
        raise SystemExit("timed out waiting for a change")

    nl, nb = now
    print("changed: lives=%s bombs=%s (was %s,%s)" % (nl, nb, lives, bombs))
    second = set(candidates(h, nl, nb))
    keep = [a for a in first if a in second]
    print("scan 2: %d candidate(s); %d survived the intersection"
          % (len(second), len(keep)))
    for a in keep:
        raw = M.read_mem(h, a + 8, 8)
        print("   player object? %016x  +8..+15 = %s" % (a, raw.hex() if raw else "?"))


if __name__ == "__main__":
    main()
