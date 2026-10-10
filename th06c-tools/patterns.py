#!/usr/bin/env python3
"""Attack patterns to run a policy against, with no game running.

Two of these are not invented. `captured_pair_vertical` and `captured_spread`
are the two attacks the live laser probe actually caught, rebuilt from the raw
slot dump: the same pivots, angles, widths, speeds and lengths. They are here so
the thing that killed the dodger under a laser can be replayed on demand, which
is the whole reason this file exists -- getting back to that attack in the game
took a full run.

The rest are the shapes a dodging policy has to handle: a beam that sweeps, a
gap it has to be inside when the beam arrives, a ring that closes, a wall that
descends, and a board that pushes the player into an edge and then cuts off the
exit. That last one is the failure the logged run showed 20 times out of 25.

  patterns.py --list
  patterns.py --show NAME       the opening frame, as text
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FIELD_W, FIELD_H = 384.0, 448.0


# ------------------------------------------------------------------- helpers
def player(x=192.0, y=400.0, speed=4.0):
    return {"pos": (float(x), float(y)), "state": 0, "speed": speed}


def state(pl=None, bullets=(), lasers=(), enemies=()):
    return {"player": pl or player(), "bullets": list(bullets),
            "lasers": list(lasers), "enemies": list(enemies),
            "lives": 3, "bombs": 2, "power": 64}


def bullet(x, y, vx, vy, half=6.0, live=True, state_=1):
    return {"pos": (float(x), float(y)), "vel": (float(vx), float(vy)),
            "half": half, "state": state_, "live": live}


def laser(origin, angle, width=10.0, speed=0.0, start_length=200.0,
          start_offset=0.0, state_=1, start_time=0, duration=10 ** 6,
          despawn_duration=30, hitbox_start=0, hitbox_end=10 ** 6):
    """A beam. `origin` is the pivot; the live part runs from startOffset out."""
    return {"origin": (float(origin[0]), float(origin[1])),
            "angle": float(angle), "width": float(width), "speed": float(speed),
            "startOffset": float(start_offset),
            "endOffset": float(start_offset + start_length),
            "startLength": float(start_length),
            "state": state_, "timer": 0, "startTime": start_time,
            "duration": duration, "despawnDuration": despawn_duration,
            "hitboxStartTime": hitbox_start, "hitboxEndDelay": hitbox_end,
            "inUse": True}


def ring(cx, cy, n, speed, phase=0.0, radius=0.0, half=6.0):
    out = []
    for i in range(n):
        a = phase + 2.0 * math.pi * i / n
        out.append(bullet(cx + math.cos(a) * radius, cy + math.sin(a) * radius,
                          math.cos(a) * speed, math.sin(a) * speed, half))
    return out


# ------------------------------------------------------------------ patterns
# Each entry is a function returning {"state": ..., "spawners": [...]}.
# A spawner is called as `spawner(sim, t)` once per frame before the collision
# test, which is where the game's own spawn code sits in the frame.

def _empty():
    return {"state": state()}


def _captured_pair_vertical():
    """The two wide beams the probe caught: x=48 and x=336, width 32, 90 deg.

    Dumped live as startOffset 0, endOffset 500, startLength 500, width 32,
    speed 4, state 2. Rebuilt in state 1 so the attack lasts long enough to
    dodge, which is what it was doing before it started despawning.

    The corridor between them is 64 < x < 320 -- 256 units wide, and the player
    moves 4 a frame, so this is about holding a lane, not threading a gap.

    They slide off the field in ~160 frames (startOffset reaches 640), so the
    pair is respawned; otherwise the pattern tests nothing after that.
    """
    def make():
        return [laser((48.0, 61.0), 90.0, width=32.0, speed=4.0,
                      start_length=500.0),
                laser((336.0, 61.0), 90.0, width=32.0, speed=4.0,
                      start_length=500.0)]

    def respawn(sim, t):
        if not sim.lasers:
            sim.lasers.extend(make())

    return {"state": state(lasers=make()), "spawners": [respawn]}


def _captured_spread():
    """The three-beam fan the probe caught, from one pivot at three angles.

    Live: origin (182.3, 70.9), angles 0.908 / 1.301 / 1.694 rad (52.0, 74.5,
    97.1 deg), width 6, speed 4, startOffset 32, startLength 192. Each beam was
    fully extended and sliding, and endOffset - startOffset was exactly 192.
    """
    def make():
        return [laser((182.3, 70.9), ang, width=6.0, speed=4.0,
                      start_length=192.0, start_offset=32.0)
                for ang in (52.0, 74.5, 97.1)]

    def respawn(sim, t):
        if not sim.lasers:
            sim.lasers.extend(make())

    return {"state": state(lasers=make()), "spawners": [respawn]}


def _laser_sweep():
    """One long beam pivoting from the top centre, a turn every ~7.5 s.

    Starts pointing right, so the opening frame is survivable and the player has
    most of a turn to be somewhere else when it comes round to vertical. A
    policy that reads the beam as static stands in front of it.
    """
    L = laser((192.0, 40.0), 0.0, width=8.0, speed=0.0, start_length=520.0)

    def spin(sim, t):
        L["angle"] = (0.0 + t * 0.8) % 360.0

    return {"state": state(lasers=[L]), "spawners": [spin]}


def _laser_gate():
    """A vertical beam pair whose gap slides across the field.

    The gap is 96 units wide and moves 3 a frame, slower than the player, so it
    is always reachable if the policy starts moving early enough. Standing still
    is fatal as soon as the gap moves off the player.
    """
    ls = [laser((0.0, 0.0), 90.0, width=140.0, speed=0.0, start_length=520.0),
          laser((0.0, 0.0), 90.0, width=140.0, speed=0.0, start_length=520.0)]

    def slide(sim, t):
        gap = 60.0 + ((t * 3.0) % (FIELD_W - 156.0))
        ls[0]["origin"] = (gap, 0.0)              # beam left of the gap
        ls[1]["origin"] = (gap + 156.0, 0.0)      # beam right of the gap
        for L in ls:
            L["pos"] = (L["origin"][0], 0.0)

    return {"state": state(lasers=ls), "spawners": [slide]}


def _laser_cross():
    """Four beams, one from each edge, meeting at the centre.

    Each beam is 140 wide, so the safe set is the four corner pockets. The
    beams slide inward, which shrinks the pockets; the policy has to commit to
    a corner before they arrive.
    """
    ls = [laser((192.0, -20.0), 90.0, width=140.0, speed=3.0, start_length=0.0,
                start_offset=0.0),
          laser((192.0, FIELD_H + 20.0), -90.0, width=140.0, speed=3.0,
                start_length=0.0),
          laser((-20.0, 224.0), 0.0, width=140.0, speed=3.0, start_length=0.0),
          laser((FIELD_W + 20.0, 224.0), 180.0, width=140.0, speed=3.0,
                start_length=0.0)]
    for L in ls:
        L["startLength"] = 300.0
    return {"state": state(lasers=ls)}


def _ring_16():
    """A 16-bullet ring every 40 frames from the top centre, aimed outward."""
    def fire(sim, t):
        if t % 40 == 0:
            sim.bullets.extend(ring(192.0, 80.0, 16, 2.4, phase=t * 0.05))

    return {"state": state(), "spawners": [fire]}


def _spiral():
    """A one-bullet-per-2-frames spiral from the top centre."""
    def fire(sim, t):
        if t % 2 == 0:
            a = t * 0.21
            sim.bullets.append(bullet(192.0, 80.0,
                                      math.cos(a) * 2.6, math.sin(a) * 2.6))

    return {"state": state(), "spawners": [fire]}


def _wall_gap():
    """A descending wall with one gap, re-aimed every 70 frames."""
    def fire(sim, t):
        if t % 70 == 0:
            gap = 60.0 + ((t * 7.0) % 264.0)
            for x in range(0, int(FIELD_W) + 1, 12):
                if gap <= x <= gap + 84:
                    continue
                sim.bullets.append(bullet(x, -10.0, 0.0, 3.0))

    return {"state": state(), "spawners": [fire]}


def _column():
    """A column falling onto the player's starting lane."""
    def fire(sim, t):
        if t % 18 == 0:
            sim.bullets.append(bullet(192.0, 100.0, 0.0, 5.0))

    return {"state": state(), "spawners": [fire]}


def _corner_trap():
    """The logged failure, rebuilt: pressure to the bottom edge, then a beam.

    The logged run died 20 times out of 25 at y=432 with the oracle reporting a
    safe move. The board that does it is a broad fan from above -- nothing
    lethal near the player, so the highest clearance is at the bottom edge --
    followed by a beam laid across that edge. A greedy maximiser of clearance
    walks into it.
    """
    ls = [laser((192.0, FIELD_H + 30.0), -90.0, width=90.0, speed=0.0,
                start_length=180.0, start_offset=0.0, state_=0,
                start_time=150, duration=10 ** 6, hitbox_start=170)]

    def fan(sim, t):
        if t % 12 == 0:
            a = math.pi / 2.0 + math.sin(t * 0.03) * 0.9
            for k in range(7):
                aa = a + (k - 3) * 0.16
                sim.bullets.append(bullet(192.0 + math.cos(aa) * 40.0, 90.0,
                                          math.cos(aa) * 2.2, math.sin(aa) * 2.2))

    return {"state": state(lasers=ls), "spawners": [fan]}


def _laser_then_bullets():
    """A beam sweep that switches off as a ring closes, so the escape changes."""
    L = laser((192.0, 40.0), 0.0, width=10.0, speed=0.0, start_length=520.0,
              duration=240)

    def spin(sim, t):
        if t < 240:
            L["angle"] = (0.0 + t * 1.2) % 360.0
        if t == 240:
            sim.bullets.extend(ring(192.0, 200.0, 24, 2.0))

    return {"state": state(lasers=[L]), "spawners": [spin]}


PATTERNS = {
    "empty": _empty,
    "captured_pair_vertical": _captured_pair_vertical,
    "captured_spread": _captured_spread,
    "laser_sweep": _laser_sweep,
    "laser_gate": _laser_gate,
    "laser_cross": _laser_cross,
    "laser_then_bullets": _laser_then_bullets,
    "ring_16": _ring_16,
    "spiral": _spiral,
    "wall_gap": _wall_gap,
    "column": _column,
    "corner_trap": _corner_trap,
}


def build(name):
    if name not in PATTERNS:
        raise SystemExit("unknown pattern %r; have %s"
                         % (name, ", ".join(sorted(PATTERNS))))
    spec = PATTERNS[name]()
    spec.setdefault("spawners", [])
    return spec


def main():
    argv = sys.argv[1:]
    if "--list" in argv or not argv:
        for name in PATTERNS:
            print(name)
        return
    if "--show" in argv:
        import view
        spec = build(argv[argv.index("--show") + 1])
        print(view.render(spec["state"]))


if __name__ == "__main__":
    main()
