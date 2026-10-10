#!/usr/bin/env python3
"""A per-frame dodging policy: arithmetic, no model, no API calls.

The controller's model decides 4-6 times a second and holds the direction
between decisions, so a decision is roughly 250 ms stale when it is applied and
stays in force for another 250 ms. That is not a per-frame policy and cannot
become one. This is the other thing: it reads the same state at 60 Hz and picks
a direction every frame, which is what a player's hands do.

It exists to separate two hypotheses that the model runs could not. If this
dodger survives where the model does not, the decision rate is the problem. If
it also dies, the problem is upstream of the policy -- the representation or the
oracle's model of it -- and no prompt or encoding will fix it.

It is not a coach and never enters a prompt. It is scored by the same oracle the
model is scored by, and that oracle is a known-approximate referee (straight
lines, no new spawns, fixed radii), so a run under the dodger measures agreement
with that approximation rather than true survival.

  dodger.py --selftest        scenario suite, prints each choice
  dodger.py --bench           time one decision on a dense board
"""
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oracle  # noqa: E402
import state  # noqa: E402
import view  # noqa: E402

# When two moves have the same clearance, prefer the earlier one here. Staying
# still when nothing is coming is correct play (you keep shooting and do not
# drift into a pattern), moving down is the safe default, and moving up is the
# last resort because it goes into the stream. This only breaks ties; it never
# overrides a real clearance difference.
PREFERENCE = ["stay put", "down", "left", "right",
              "down-left", "down-right", "up-left", "up-right", "up"]


class Dodger:
    """Greedy per-frame maximiser of the oracle's clearance.

    `stickiness` is hysteresis, in the same units as clearance. A move is kept
    while it is within this many units of the best one, so two near-equal moves
    do not alternate frame to frame and cancel the player's motion. It only
    delays a change that the oracle would make anyway; it never forces a move
    the oracle calls unsafe.
    """

    def __init__(self, horizon=view.HORIZON, stickiness=2.0):
        self.horizon = horizon
        self.stickiness = stickiness
        self.last = None
        self.clearance = {}

    def choose(self, state):
        cl = oracle.clearance(state, self.horizon)
        self.clearance = cl
        best = max(PREFERENCE, key=lambda m: cl[m])
        if (self.last in cl
                and cl[self.last] >= cl[best] - self.stickiness):
            choice = self.last
        else:
            choice = best
        self.last = choice
        return choice


# ------------------------------------------------------------------ selftest
def _base():
    return {"player": {"pos": (192.0, 400.0), "state": 0, "speed": 4.0},
            "bullets": [], "enemies": [], "lasers": [],
            "lives": 3, "bombs": 2, "power": 64}


def _bullet(x, y, vel=None):
    return {"idx": 0, "pos": (float(x), float(y)), "vel": vel, "state": 1,
            "live": True}


def selftest():
    """Assertions that need no game. Returns the number of failures."""
    fails = 0

    def check(name, cond, detail=""):
        nonlocal fails
        if cond:
            print("  ok    %s" % name)
        else:
            fails += 1
            print("  FAIL  %s  %s" % (name, detail))

    # --- velocity tracking -------------------------------------------------
    print("velocity tracker")
    tr = state.VelocityTracker(min_age=0.05)
    dt = 1.0 / state.FPS
    vels = []
    for f in range(12):
        out = tr.update(f * dt, [(7, 100.0 + 3.0 * f, 200.0 - 1.0 * f)])
        if 7 in out:
            vels.append(out[7])
    check("a 3 u/f bullet reads as 3 u/f, not 1.5x", bool(vels)
          and all(abs(vx - 3.0) < 0.01 and abs(vy + 1.0) < 0.01
                  for vx, vy in vels), repr(vels[:4]))

    tr = state.VelocityTracker(min_age=0.05)
    for f in range(6):
        tr.update(f * dt, [(3, 100.0 + 2.0 * f, 50.0)])
    tr.update(6 * dt, [])                       # slot 3 empty: bullet despawned
    out = tr.update(7 * dt, [(3, 300.0, 400.0)])  # slot reused by a new bullet
    check("a reused slot is not differenced across bullets", 3 not in out,
          repr(out))
    vals = []
    for f in range(8, 16):
        out = tr.update(f * dt, [(3, 300.0 + 5.0 * (f - 7), 400.0)])
        if 3 in out:
            vals.append(out[3])
    check("the new bullet then reads correctly", bool(vals)
          and all(abs(vx - 5.0) < 0.01 for vx, _ in vals), repr(vals[:4]))

    # --- the oracle fails closed on an unknown velocity --------------------
    print("oracle")
    st = _base()
    st["bullets"] = [_bullet(100, 400, vel=None)]     # 92u left, no history
    sv = oracle.survivable(st)
    check("unknown-velocity bullet is not called safe", not sv["left"],
          "left was safe")
    check("standing in its path is not called safe", not sv["stay put"],
          "stay put was safe")
    check("moving away from it is safe", sv["right"], "right was not safe")

    st2 = _base()
    st2["bullets"] = [_bullet(100, 400, vel=(0.0, 0.0))]
    sv2 = oracle.survivable(st2)
    check("the same bullet with a measured zero velocity is harmless",
          sv2["left"] and sv2["stay put"], "a stationary bullet 92u away was lethal")

    st3 = _base()
    st3["bullets"] = [_bullet(100, 400, vel=None)]
    st3["bullets"][0]["live"] = False
    check("a non-live bullet is ignored",
          all(oracle.survivable(st3).values()), "a spawning bullet was scored")

    # --- the dodger on the hand-built scenarios ---------------------------
    print("dodger on view scenarios")
    expect = {
        # scenario: directions the oracle calls fatal; the dodger must not take
        # one. An empty set means the board is loose, so only the invariant
        # that it never chooses a fatal move applies.
        "cluster_left_closing": {"left", "stay put"},
        "cluster_right_closing": {"right", "stay put"},
        "cluster_left_receding": set(),
        "wall_gap_left": {"right", "up", "up-right", "stay put"},
        "wall_gap_right": {"left", "up", "up-left", "stay put"},
        "laser_left": {"left", "down-left", "up-left"},
        "column_on_player": {"stay put", "up"},
    }
    for name, forbidden in expect.items():
        st = view.SCENARIOS[name]
        safe = oracle.safe_moves(st)
        d = Dodger()
        choice = d.choose(st)
        check("%-22s chose %-10s" % (name, choice),
              choice in safe and choice not in forbidden, "chose a fatal move")

    # --- the dodger's clearance grades moves that all kill ----------------
    # A bullet parked 20u above the player: moving up goes into it, everything
    # else moves away. The sign is not enough here -- a per-frame dodger has to
    # know which of several lethal moves is least bad -- so the ordering is the
    # thing being tested.
    st = _base()
    st["bullets"] = [_bullet(192, 380, vel=(0.0, 0.0))]
    cl = oracle.clearance(st)
    check("graded clearance orders a board with one lethal move",
          cl["down"] > cl["stay put"] > cl["up"],
          repr({k: round(v, 1) for k, v in cl.items()}))

    # --- closed loop: does re-deciding every frame resolve to an escape? ---
    # A column falls on the player, simulated frame by frame with the dodger's
    # choice applied to the player's position. The oracle scores a constant
    # 15-frame commitment, so this is the one place the per-frame loop is
    # checked against motion that is not the oracle's own arithmetic.
    st = _base()
    st["bullets"] = [{"idx": i, "pos": (192.0, 100.0 + 24 * i),
                      "vel": (0.0, 5.0), "state": 1, "live": True}
                     for i in range(10)]
    px, py = 192.0, 400.0
    d = Dodger()
    hit = False
    for _ in range(30):
        for b in st["bullets"]:
            b["pos"] = (b["pos"][0], b["pos"][1] + 5.0)
        st["player"]["pos"] = (px, py)
        choice = d.choose(st)
        ux, uy = dict(oracle.MOVES)[choice]
        px = min(max(px + ux * 4.0, 0.0), view.FIELD_W)
        py = min(max(py + uy * 4.0, 0.0), view.FIELD_H)
        for b in st["bullets"]:
            if math.hypot(b["pos"][0] - px, b["pos"][1] - py) < 8.0:
                hit = True
    check("closed loop escapes a falling column", not hit,
          "the player was hit at (%.0f, %.0f)" % (px, py))

    print()
    print("%d failure(s)" % fails)
    return fails


# --------------------------------------------------------------------- bench
def bench(n_bullets=150, iters=200):
    """Time one decision on a board dense enough to matter."""
    import random
    rng = random.Random(1)
    st = _base()
    bullets = []
    for i in range(n_bullets):
        # Concentrate them around the player, where a real stage's threats are.
        x = rng.uniform(60.0, 324.0)
        y = rng.uniform(200.0, 440.0)
        ang = rng.uniform(0.0, 2.0 * math.pi)
        sp = rng.uniform(1.0, 5.0)
        bullets.append({"idx": i, "pos": (x, y),
                        "vel": (math.cos(ang) * sp, math.sin(ang) * sp),
                        "state": 1, "live": True})
    st["bullets"] = bullets
    d = Dodger()
    d.choose(st)                       # warm up
    t0 = time.perf_counter()
    for _ in range(iters):
        d.choose(st)
    ms = (time.perf_counter() - t0) * 1000.0 / iters
    print("%d live bullets: %.2f ms per decision (%.0f Hz budget is 16.7 ms)"
          % (n_bullets, ms, 1000.0 / ms if ms else 0.0))
    return ms


def main():
    argv = sys.argv[1:]
    if "--bench" in argv:
        for count in (20, 60, 150, 300):
            bench(count)
        return
    if "--selftest" in argv:
        raise SystemExit(1 if selftest() else 0)
    # Default: show what it does on every scenario.
    for name in view.SCENARIOS:
        d = Dodger()
        choice = d.choose(view.SCENARIOS[name])
        cl = d.clearance
        print("%-22s -> %-10s  clearance %.1f  (best %.1f)"
              % (name, choice, cl[choice], max(cl.values())))


if __name__ == "__main__":
    main()
