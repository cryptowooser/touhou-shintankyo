#!/usr/bin/env python3
"""Which moves survive the next N frames.

This is the critic, not a coach. It never goes into a prompt; it exists to score
decisions after the fact. Hand-written answer keys for a dodging test are easy to
get wrong -- "is moving toward receding bullets safe?" is exactly the kind of
marginal call a person reasons about badly -- so the scoring set comes from
simulation instead.

Approximations, all of which matter at longer horizons:

  * bullets are extrapolated in a straight line, so accelerating, curving and
    homing bullets (EX_ACCELERATION, EX_ANGLE_ADD, EX_ANGLE_PLAYER) are wrong
  * bullets that spawn during the window are invisible
  * bullets still playing their spawn or despawn animation cannot kill and
    are excluded; see view.live_bullets for the disassembly
  * collision is a circle test with fixed radii; real bullets vary from 4 to 32
    units and the real test is a box
  * lasers are treated as static segments and never as moving or fading

  oracle.py --scenarios
  oracle.py --scenario column_on_player --moves
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view  # noqa: E402

PLAYER_RADIUS = 2.0
BULLET_RADIUS = 6.0
ENEMY_RADIUS = 8.0

# The speed to assume for a bullet whose velocity has not been measured yet.
# `state` reports None until a bullet has been seen long enough to difference
# its position, which is the first few frames of its life. The old fallback was
# (0, 0), and the oracle extrapolates that as a bullet that never moves, so a
# freshly spawned bullet aimed at the player was scored harmless -- the failure
# direction that kills. 8 u/f is twice the player's speed and above every bullet
# speed observed so far; it is deliberately an over-estimate.
UNKNOWN_BULLET_SPEED = 8.0

_S = math.sqrt(0.5)
MOVES = [("left", (-1, 0)), ("right", (1, 0)), ("up", (0, -1)), ("down", (0, 1)),
         ("up-left", (-_S, -_S)), ("up-right", (_S, -_S)),
         ("down-left", (-_S, _S)), ("down-right", (_S, _S)), ("stay put", (0, 0))]


def _unit(dx, dy):
    n = math.hypot(dx, dy)
    return (0.0, 0.0) if n == 0 else (dx / n, dy / n)


def _laser_dist(lx, ly, ang_deg, length, cx, cy):
    """Distance from (cx,cy) to the laser segment."""
    a = math.radians(ang_deg)
    ux, uy = math.cos(a), math.sin(a)
    t = (cx - lx) * ux + (cy - ly) * uy
    if t < 0:
        t = 0.0
    elif t > length:
        t = length
    return math.hypot(cx - (lx + ux * t), cy - (ly + uy * t))


def _unknown_vel(px, py, bx, by, speed=UNKNOWN_BULLET_SPEED):
    """The velocity to assume for a bullet whose real one is not known.

    Assume the worst bounded case: aimed straight at the player, faster than any
    real bullet. That is a guess, but it is a guess in the safe direction -- it
    can mark a harmless move unsafe for the few frames a bullet is new, which
    costs a dodge, where the alternative marks a lethal move safe, which costs a
    life.
    """
    dx, dy = px - bx, py - by
    n = math.hypot(dx, dy)
    if n < 1e-6:
        return (0.0, 0.0)      # already on the player; position alone kills
    return (dx / n * speed, dy / n * speed)


def clearance(state, horizon=view.HORIZON):
    """Map move name -> the smallest clearance (distance minus radii) it keeps.

    Positive means no collision inside the window; larger is safer. The critic
    only needs the sign, but a per-frame dodger also needs an ordering among
    moves that all kill, so both are kept. This is the arithmetic `survivable`
    always did, no longer thrown away at the comparison.
    """
    px, py = state["player"]["pos"]
    speed = state["player"].get("speed", 4.0)
    lasers = state.get("lasers", [])
    fatal = [e for e in state.get("enemies", []) if e.get("fatal")]

    reach = PLAYER_RADIUS + BULLET_RADIUS
    limit = speed * horizon + reach
    bullets = []
    for b in view.live_bullets(state):
        bx, by = b["pos"]
        v = b.get("vel")
        if v is None:
            v = _unknown_vel(px, py, bx, by)
        # A bullet that cannot get within the player's reach of any point on
        # the player's path cannot matter, and most of the field is like that.
        if math.hypot(bx - px, by - py) > limit + math.hypot(v[0], v[1]) * horizon:
            continue
        bullets.append((bx, by, v[0], v[1]))

    result = {}
    for name, (dx, dy) in MOVES:
        ux, uy = _unit(dx, dy)
        worst = float("inf")
        for t in range(1, horizon + 1):
            cx = min(max(px + ux * speed * t, 0.0), view.FIELD_W)
            cy = min(max(py + uy * speed * t, 0.0), view.FIELD_H)

            for bx, by, vx, vy in bullets:
                d = math.hypot(bx + vx * t - cx, by + vy * t - cy) - reach
                if d < worst:
                    worst = d

            for L in lasers:
                pad = PLAYER_RADIUS + L.get("width", 10.0) / 2.0
                # A laser's far end advances at `speed` per frame, so at frame t
                # of the look-ahead it has reached `length + speed*t`. Treating
                # it as the static current segment is what let the dodger hold
                # still while a laser swept onto it.
                d = _laser_dist(L["pos"][0], L["pos"][1], L.get("angle", 0.0),
                                L.get("length", 200.0) + L.get("speed", 0.0) * t,
                                cx, cy) - pad
                if d < worst:
                    worst = d

            for e in fatal:
                d = (math.hypot(e["pos"][0] - cx, e["pos"][1] - cy)
                     - (PLAYER_RADIUS + ENEMY_RADIUS))
                if d < worst:
                    worst = d

        result[name] = worst
    return result


def survivable(state, horizon=view.HORIZON):
    """Map move name -> bool, for one state. True means no collision in the window."""
    return {m: c > 0 for m, c in clearance(state, horizon).items()}


def safe_moves(state, horizon=view.HORIZON):
    return {m for m, ok in survivable(state, horizon).items() if ok}


def main():
    argv = sys.argv[1:]
    names = (argv[argv.index("--scenario") + 1].split(",") if "--scenario" in argv
             else list(view.SCENARIOS))
    for name in names:
        state = view.SCENARIOS[name]
        sv = survivable(state)
        safe = sorted(m for m, ok in sv.items() if ok)
        dead = sorted(m for m, ok in sv.items() if not ok)
        print("%-22s safe: %s" % (name, ", ".join(safe) or "(none)"))
        print("%-22s dies: %s" % ("", ", ".join(dead) or "(none)"))


if __name__ == "__main__":
    main()
