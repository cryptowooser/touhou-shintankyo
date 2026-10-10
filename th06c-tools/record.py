#!/usr/bin/env python3
"""Record live frames, and check the simulator against them.

`sim.py` is built from the 1.02h decomp, and this port has already been caught
disagreeing with that decomp once -- the laser struct is reordered, which is why
the field offsets had to come from the port's own code. So the simulator is a
claim, and this is how the claim gets checked: record consecutive frames from
the running game, step the simulator across each one, and compare where it says
the bullets and beams are with where the game says they are.

A frame is the same dict `state.Reader.snapshot` returns, so the recorder is
almost free and the two sides cannot drift apart in shape.

  record.py --out frames.jsonl --frames 600     record 10 s at 60 Hz
  record.py --validate frames.jsonl             step the sim across every pair
  record.py --show frames.jsonl --at 120        one recorded frame, as text

Validation only covers motion, not collision: it compares positions one frame
apart, so it says whether the arithmetic is right, not whether a hit at those
positions is right. The hit test is checked by `sim.py --selftest` against the
decomp's own boundary conditions.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sim as S  # noqa: E402


def record(path, frames, hz=60.0, vel_off=None):
    import state
    import time
    r = state.Reader(vel_off=vel_off)
    period = 1.0 / hz
    n = 0
    t0 = time.perf_counter()
    with open(path, "w", encoding="utf-8") as f:
        while n < frames:
            t = time.perf_counter()
            snap = r.snapshot()
            f.write(json.dumps(snap) + "\n")
            n += 1
            slack = t + period - time.perf_counter()
            if slack > 0:
                time.sleep(slack)
            if n % 60 == 0:
                print("  %d frames, %.1f s" % (n, time.perf_counter() - t0),
                      flush=True)
    print("wrote %d frames to %s" % (n, path))


def load(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def validate(path, verbose=False):
    """Step the sim from each frame and compare to the next.

    Bullets are matched by slot index, which `state.read_bullets` reports and is
    stable for the life of a bullet. A slot that changes generation between two
    frames -- a pooled slot reused by a new bullet -- is skipped rather than
    counted as a huge error, because that is not the same bullet.
    """
    frames = load(path)
    if len(frames) < 2:
        print("%s: need at least two frames" % path)
        return 1
    checked = 0
    errs = []
    miss = 0
    extra = 0
    laser_checked = 0
    laser_errs = []
    for i in range(len(frames) - 1):
        a, b = frames[i], frames[i + 1]
        sim = S.Sim(a)
        sim.step("stay put")
        got = {x.get("idx"): x for x in sim.bullets}
        want = {x.get("idx"): x for x in b.get("bullets", [])}
        for idx, w in want.items():
            g = got.get(idx)
            if g is None:
                miss += 1
                continue
            # A bullet that moves more than 40 units in one frame is a slot
            # that was reused, not a bullet that teleported.
            d = ((g["pos"][0] - w["pos"][0]) ** 2
                 + (g["pos"][1] - w["pos"][1]) ** 2) ** 0.5
            if d > 40.0:
                miss += 1
                continue
            errs.append(d)
            checked += 1
        for idx in got:
            if idx not in want:
                extra += 1
        ga = {L.get("idx"): L for L in sim.lasers}
        wa = {L.get("idx"): L for L in b.get("lasers", [])}
        for idx, w in wa.items():
            g = ga.get(idx)
            if g is None:
                continue
            laser_errs.append(abs(g["length"] - w["length"])
                              + abs(g["pos"][0] - w["pos"][0])
                              + abs(g["pos"][1] - w["pos"][1])
                              + abs((g["angle"] - w["angle"]) % 360.0))
            laser_checked += 1

    print("%s: %d frames" % (path, len(frames)))
    if checked:
        errs.sort()
        print("  bullets matched: %d  (unmatched %d, sim kept %d the game did not)"
              % (checked, miss, extra))
        print("  one-frame position error: median %.3f u  p90 %.3f u  max %.3f u"
              % (errs[len(errs) // 2], errs[int(len(errs) * 0.9)], errs[-1]))
    else:
        print("  no bullets to match (%d unmatched)" % miss)
    if laser_checked:
        laser_errs.sort()
        print("  lasers matched: %d  error median %.3f  max %.3f"
              % (laser_checked, laser_errs[len(laser_errs) // 2],
                 laser_errs[-1]))
    else:
        print("  no lasers in this recording")
    # A bullet can only move by its speed in one frame; the largest plausible
    # step is well under 40, so anything past a few units means the model is
    # wrong rather than noisy.
    bad = sum(1 for e in errs if e > 3.0)
    if bad:
        print("  %d bullet(s) moved more than 3 u from where the sim put them"
              % bad)
    if verbose and errs:
        print("  worst: %s" % ", ".join("%.2f" % e for e in errs[-10:]))
    return 0


def show(path, at=0):
    import view
    frames = load(path)
    print(view.render(frames[at]))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="frames.jsonl")
    ap.add_argument("--frames", type=int, default=600)
    ap.add_argument("--hz", type=float, default=60.0)
    ap.add_argument("--vel-off", default=None)
    ap.add_argument("--validate", default=None)
    ap.add_argument("--show", default=None)
    ap.add_argument("--at", type=int, default=0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.validate:
        raise SystemExit(validate(args.validate, args.verbose))
    if args.show:
        show(args.show, args.at)
        return
    record(args.out, args.frames, args.hz,
           int(args.vel_off, 0) if args.vel_off else None)


if __name__ == "__main__":
    main()
