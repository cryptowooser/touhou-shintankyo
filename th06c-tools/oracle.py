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

_S = math.sqrt(0.5)
MOVES = [("left", (-1, 0)), ("right", (1, 0)), ("up", (0, -1)), ("down", (0, 1)),
         ("up-left", (-_S, -_S)), ("up-right", (_S, -_S)),
         ("down-left", (-_S, _S)), ("down-right", (_S, _S)), ("stay put", (0, 0))]


def _unit(dx, dy):
    n = math.hypot(dx, dy)
    return (0.0, 0.0) if n == 0 else (dx / n, dy / n)


def _laser_hit(lx, ly, ang_deg, length, cx, cy, pad):
    """Distance from (cx,cy) to the laser segment, compared against pad."""
    a = math.radians(ang_deg)
    ux, uy = math.cos(a), math.sin(a)
    t = (cx - lx) * ux + (cy - ly) * uy
    if t < 0:
        t = 0.0
    elif t > length:
        t = length
    return math.hypot(cx - (lx + ux * t), cy - (ly + uy * t)) < pad


def survivable(state, horizon=view.HORIZON):
    """Map move name -> bool, for one state. True means no collision in the window."""
    px, py = state["player"]["pos"]
    speed = state["player"].get("speed", 4.0)
    bullets = state.get("bullets", [])
    lasers = state.get("lasers", [])
    fatal = [e for e in state.get("enemies", []) if e.get("fatal")]

    result = {}
    for name, (dx, dy) in MOVES:
        ux, uy = _unit(dx, dy)
        ok = True
        for t in range(1, horizon + 1):
            cx = min(max(px + ux * speed * t, 0.0), view.FIELD_W)
            cy = min(max(py + uy * speed * t, 0.0), view.FIELD_H)

            for b in bullets:
                bx = b["pos"][0] + b.get("vel", (0.0, 0.0))[0] * t
                by = b["pos"][1] + b.get("vel", (0.0, 0.0))[1] * t
                if math.hypot(bx - cx, by - cy) < PLAYER_RADIUS + BULLET_RADIUS:
                    ok = False
                    break
            if not ok:
                break

            for L in lasers:
                if _laser_hit(L["pos"][0], L["pos"][1], L.get("angle", 0.0),
                              L.get("length", 200.0), cx, cy,
                              PLAYER_RADIUS + L.get("width", 10.0) / 2.0):
                    ok = False
                    break
            if not ok:
                break

            for e in fatal:
                if math.hypot(e["pos"][0] - cx, e["pos"][1] - cy) < \
                        PLAYER_RADIUS + ENEMY_RADIUS:
                    ok = False
                    break
            if not ok:
                break
        result[name] = ok
    return result


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
