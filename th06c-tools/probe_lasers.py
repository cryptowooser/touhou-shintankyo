#!/usr/bin/env python3
"""Dump the live laser array so its fields can be identified.

The array is `g_BulletManager + 0xE8818` (RVA `0xA3DC28`), 64 slots of 0x278
bytes, active byte at `+0x254` and state byte at `+0x270` (both from
`BulletManager_OnUpdate` at `0x0DB90`; the slot loop is at `0xE570`). Which
offsets hold `pos`, `angle`, `length` and `width` is not documented for this
port -- the 64-bit layout does not match the 1.02h decomp field order -- so this
prints the whole slot and leaves the reading to the eye:

  * an origin is two floats inside the field (x 0..384, y 0..448) that stay put
  * an angle is a float in radians (|v| <= pi) or degrees
  * a length grows over the laser's life
  * a width is small and constant

  probe_lasers.py --wait 300        poll up to 5 minutes for a laser
  probe_lasers.py --frames 60       dump 60 frames once one appears
  probe_lasers.py --slot 0          force slot 0 even if inactive
"""
import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memedit as M   # noqa: E402
import modbase as MB  # noqa: E402
import state as S     # noqa: E402

LASER0 = 0xA3DC28
STRIDE = 0x278
SLOTS = 64
ACTIVE = 0x254
STATE = 0x270

# Every 4-byte word in the slot is printed, so nothing is assumed about the
# layout. A laser's fields are then read off by how they behave over time.
WORDS = list(range(0x00, STRIDE - 3, 4))


def read_slot(buf, i):
    return buf[i * STRIDE:(i + 1) * STRIDE]


def dump(reader, slot, buf):
    raw = read_slot(buf, slot)
    words = []
    for off in WORDS:
        u, = struct.unpack_from("<I", raw, off)
        f, = struct.unpack_from("<f", raw, off)
        if u == 0:
            continue
        if abs(f) < 1e6 and f == f:
            words.append("+%03X=%.4g" % (off, f))
        else:
            words.append("+%03X=%d" % (off, u))
    print("  slot %2d active=%d state=%d: %s"
          % (slot, raw[ACTIVE], raw[STATE], "  ".join(words)), flush=True)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wait", type=float, default=300.0)
    ap.add_argument("--frames", type=int, default=40)
    ap.add_argument("--slot", type=int, default=None)
    args = ap.parse_args()

    r = S.Reader()
    print("pid=%d base=%#x  laser[0]=%#x" % (r.pid, r.base, r.base + LASER0))

    if args.slot is not None:
        buf = M.read_mem(r.h, r.base + LASER0, SLOTS * STRIDE)
        dump(r, args.slot, buf)
        return

    print("polling up to %.0fs for an active laser..." % args.wait, flush=True)
    t0 = time.time()
    while time.time() - t0 < args.wait:
        buf = M.read_mem(r.h, r.base + LASER0, SLOTS * STRIDE)
        active = [i for i in range(SLOTS) if buf[i * STRIDE + ACTIVE]]
        if active:
            print("active slots: %s -- dumping %d frames"
                  % (active, args.frames), flush=True)
            for f in range(args.frames):
                buf = M.read_mem(r.h, r.base + LASER0, SLOTS * STRIDE)
                still = [i for i in active if buf[i * STRIDE + ACTIVE]]
                if not still:
                    print("  (all lasers gone after %d frames)" % f)
                    return
                print("frame %d" % f, flush=True)
                interp = {L["idx"]: L for L in r.read_lasers()}
                for i in still:
                    dump(r, i, buf)
                    L = interp.get(i)
                    if L is not None:
                        print("       state.py reads: pos=(%.1f,%.1f) "
                              "angle=%.1f deg  len=%.1f  width=%.1f  state=%d"
                              % (L["pos"][0], L["pos"][1], L["angle"],
                                 L["length"], L["width"], L["state"]),
                              flush=True)
                time.sleep(1.0 / 60.0)
            return
        time.sleep(0.2)
    print("no laser appeared in %.0fs" % args.wait)


if __name__ == "__main__":
    main()
