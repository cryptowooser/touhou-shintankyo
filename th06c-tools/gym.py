#!/usr/bin/env python3
"""A Gym-style environment over `sim.py`, so a policy can be driven or trained.

This is the "TouhouGym" part: `reset()` returns an observation, `step(action)`
returns the next one, and the reward is survival. It exists so a policy can be
measured against a named attack thousands of times without the game, and so the
observation can be changed and re-measured in seconds.

Two observation encodings, because they answer different questions:

  features  a flat float vector: the player's position, its distance to each
            wall, and the nearest `k` threats as (distance, angle, closing
            speed, radius, is-laser). This is the one to hand a learned policy.
  grid      the same facts as a small occupancy grid, mostly for debugging.

The action space is the nine moves the game actually has -- eight directions
plus staying put -- not a continuous vector. The player moves at a fixed speed
in one of eight directions, so a continuous action would have to be quantised
back anyway, and the quantisation is where the interesting decisions live.

  gym.py --patterns            every pattern, dodger vs random, side by side
  gym.py --pattern NAME        one pattern, with a survival histogram
  gym.py --obs NAME            print the feature vector for one frame
  gym.py --selftest
"""
import argparse
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patterns  # noqa: E402
import sim as S  # noqa: E402

ACTION_NAMES = [name for name, _ in S.MOVES]
N_ACTIONS = len(ACTION_NAMES)


def observe(sim, k=8):
    """Flat feature vector for the current frame.

    Deliberately the same information the oracle has, in a shape a network can
    take: no verdicts, no safe/unsafe labels, nothing the player could not see.
    A learned policy trained on these can be compared with the oracle directly.
    """
    p = sim.state["player"]
    px, py = p["pos"]
    out = [px / S.FIELD_W, py / S.FIELD_H,
           px / S.FIELD_W, (S.FIELD_W - px) / S.FIELD_W,
           py / S.FIELD_H, (S.FIELD_H - py) / S.FIELD_H]

    threats = []
    for b in sim.bullets:
        if not b.get("live", b.get("state", 1) == 1):
            continue
        bx, by = b["pos"]
        vx, vy = b.get("vel") or (0.0, 0.0)
        dx, dy = bx - px, by - py
        d = math.hypot(dx, dy)
        closing = -(vx * dx + vy * dy) / d if d > 1e-6 else 0.0
        threats.append((d, math.atan2(dy, dx) / math.pi, closing / 10.0,
                        b.get("half", S.DEFAULT_BULLET_HALF) / 32.0, 0.0))
    for L in sim.lasers:
        lx, ly = L["pos"]
        dx, dy = lx - px, ly - py
        d = math.hypot(dx, dy)
        threats.append((d, math.atan2(dy, dx) / math.pi, 0.0,
                        L["width"] / 64.0, 1.0))
    threats.sort(key=lambda t: t[0])
    for t in threats[:k]:
        out.extend(t)
    out.extend([0.0] * (5 * (k - min(k, len(threats)))))
    return out


OBS_DIM = 6 + 5 * 8


class TouhouEnv:
    """`reset()` / `step(action)` / `render()`, no external dependency."""

    def __init__(self, pattern="column", frames=900, k=8):
        self.pattern = pattern
        self.frames = frames
        self.k = k
        self.spec = None
        self.sim = None
        self.t = 0

    def reset(self):
        self.spec = patterns.build(self.pattern)
        self.sim = S.Sim(self.spec["state"], spawners=self.spec.get("spawners", ()))
        self.t = 0
        return observe(self.sim, self.k)

    def step(self, action):
        if isinstance(action, str):
            move = action
        else:
            move = ACTION_NAMES[action % N_ACTIONS]
        dead, info = self.sim.step(move)
        self.t += 1
        done = dead or self.t >= self.frames
        reward = -1.0 if dead else 0.0
        return observe(self.sim, self.k), reward, done, {
            "t": self.t, "dead": dead, "hit": info.get("hit")}

    def render(self):
        return self.sim.render()

    def close(self):
        pass


# ------------------------------------------------------------------ policies
def random_policy(rng=None):
    rng = rng or random.Random(0)
    return lambda st: ACTION_NAMES[rng.randrange(N_ACTIONS)]


def stay_policy():
    return lambda st: "stay put"


def dodger_policy(horizon=15):
    import dodger
    d = dodger.Dodger(horizon=horizon)
    return lambda st: d.choose(st)


# --------------------------------------------------------------------- tests
def trial(pattern, policy, frames=900, runs=1):
    survivals = []
    for _ in range(runs):
        env = TouhouEnv(pattern, frames=frames)
        env.reset()
        alive = frames
        for _ in range(frames):
            _, _, done, info = env.step(policy(env.sim.state))
            if done:
                alive = info["t"]
                break
        survivals.append(alive)
    return survivals


def selftest():
    fails = 0

    def check(name, cond, detail=""):
        nonlocal fails
        if cond:
            print("  ok    %s" % name)
        else:
            fails += 1
            print("  FAIL  %s  %s" % (name, detail))

    env = TouhouEnv("empty", frames=100)
    obs = env.reset()
    check("the observation has a fixed length", len(obs) == OBS_DIM,
          "%d vs %d" % (len(obs), OBS_DIM))
    check("an empty board survives", trial("empty", stay_policy(), 200) == [200])

    env = TouhouEnv("column", frames=200)
    env.reset()
    o, r, done, info = env.step("stay put")
    check("the step returns the four-tuple", len(o) == OBS_DIM and r == 0.0
          and done is False and info["t"] == 1, repr((len(o), r, done, info)))

    # The column falls on the start lane, so standing still must die and the
    # dodger must not.
    check("standing still dies under the column",
          trial("column", stay_policy(), 300)[0] < 300)
    check("the dodger survives the column",
          trial("column", dodger_policy(), 300)[0] == 300)

    # A policy that always picks the same action must be reproducible, which is
    # what makes the harness usable for comparing policies at all.
    a = trial("spiral", dodger_policy(), 300, runs=1)
    b = trial("spiral", dodger_policy(), 300, runs=1)
    check("runs are deterministic", a == b, "%s vs %s" % (a, b))

    print()
    print("%d failure(s)" % fails)
    return fails


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pattern", default=None)
    ap.add_argument("--patterns", action="store_true")
    ap.add_argument("--frames", type=int, default=900)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--obs", default=None)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        raise SystemExit(1 if selftest() else 0)
    if args.obs:
        env = TouhouEnv(args.obs)
        obs = env.reset()
        print("obs dim %d for %s:" % (len(obs), args.obs))
        for i in range(0, len(obs), 5):
            print("  " + "  ".join("%7.3f" % v for v in obs[i:i + 5]))
        return

    names = ([args.pattern] if args.pattern
             else list(patterns.PATTERNS))
    print("%-22s %10s %10s %10s" % ("pattern", "dodger", "random", "stay"))
    for name in names:
        d = trial(name, dodger_policy(), args.frames, 1)[0]
        r = sum(trial(name, random_policy(random.Random(1)), args.frames,
                      args.runs)) / args.runs
        s = trial(name, stay_policy(), args.frames, 1)[0]
        print("%-22s %10d %10.1f %10d" % (name, d, r, s))


if __name__ == "__main__":
    main()
