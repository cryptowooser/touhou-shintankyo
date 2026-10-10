#!/usr/bin/env python3
"""A forward model of EoSD's threat layer: bullets, lasers, enemies, player.

Why this exists. The live game is a bad iteration loop for a dodging policy.
Getting to a specific attack means playing to it, the state you want lasts a few
seconds, and every experiment costs a run. Worse, the thing being debugged is
the oracle's *model* of the game, and the only way to find a model error by
playing is to die of it.

So this is the other half: step the same state dict the reader produces, using
the game's own arithmetic rather than the oracle's approximation, and a policy
can be run against any attack as often as you like, deterministically.

Fidelity. Every rule here is taken from the 1.02h decomp and, where it was
observable, checked against the port:

  * the player's hitbox is a 2.5x2.5 box -- `hitboxSize = (1.25, 1.25)`, from
    `Player::AddedCallback` at Player.cpp:107. The oracle uses a radius of 2.0,
    which is close but is a circle, and the difference is the whole point: this
    file is allowed to disagree with the oracle.
  * bullet collision is an AABB test, `Player::CalcKillBoxCollision`, not a
    circle test with a fixed radius. Real bullets run from 4 to 32 units.
  * a laser is a box in its own frame: `x` in [startOffset, endOffset], `y` in
    [-width/2, width/2]. Both ends advance by `speed` per frame, which is what
    `state.read_lasers` measures and what the oracle approximates with a
    straight extension.
  * only a bullet in state 1 can kill; states 2-4 are the spawn animation and 5
    is the despawn. See `view.live_bullets` for the disassembly.

Not modelled, and each is a real gap: accelerating, curving and homing bullets
(EX_ACCELERATION, EX_ANGLE_ADD, EX_ANGLE_PLAYER) all fly straight here; enemies
do not move or fire; a bullet's kill box is taken from the state rather than
looked up per bullet type; and nothing spawns unless a pattern asks it to.

  sim.py --patterns                 run the dodger against every pattern
  sim.py --pattern NAME --frames N  run one, with a trace
  sim.py --selftest                 the assertions that need no game
"""
import argparse
import copy
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FIELD_W, FIELD_H = 384.0, 448.0
FPS = 60.0
PLAYER_SPEED = 4.0

# GameManager.cpp:290 -- playerMovementAreaTopLeftPos (8, 16) with size
# (368, 416), applied by Player::OnUpdate at Player.cpp:803. The player's centre
# cannot leave this box, and it is smaller than the playfield by 8 units on the
# left and right and 16 on the top and bottom. Clamping to the playfield instead
# hands the player 16 units of escape at the bottom edge that the game does not
# have, which is exactly where the logged run kept dying.
MOVE_L, MOVE_T = 8.0, 16.0
MOVE_R, MOVE_B = 376.0, 432.0

# Player::AddedCallback, Player.cpp:107.
PLAYER_HALF = 1.25

# A bullet with no size of its own. Real ones are set per bullet type and run
# from 4 to 32 units across; 6.0 is the oracle's fixed radius, so a pattern that
# does not say otherwise is scored the same way by both.
DEFAULT_BULLET_HALF = 6.0
ENEMY_HALF = 8.0

# Bullets are despawned once they are this far outside the field, so a bullet
# that has left the screen cannot come back and hit anything.
DESPAWN_MARGIN = 32.0

LASER_DESPAWN_OFFSET = 640.0      # `startOffset >= 640` retires the slot


def _unit(dx, dy):
    n = math.hypot(dx, dy)
    return (0.0, 0.0) if n == 0 else (dx / n, dy / n)


class Sim:
    """One board, stepped one frame at a time.

    `state` is the same dict shape `state.Reader.snapshot` returns, so a frame
    captured from the live game can be handed straight to `Sim` and played
    forward. `spawners` are called as `spawner(self, t)` after the move and
    before the collision test, which is where the game's own spawn code runs.
    """

    def __init__(self, state, speed=PLAYER_SPEED, spawners=()):
        self.state = copy.deepcopy(state)
        self.speed = speed
        self.spawners = list(spawners)
        self.t = 0
        self.dead = False
        self.death_t = None
        self.bullets = self.state.setdefault("bullets", [])
        self.lasers = self.state.setdefault("lasers", [])
        self.enemies = self.state.setdefault("enemies", [])
        for b in self.bullets:
            b.setdefault("state", 1)
            b.setdefault("live", b["state"] == 1)
            if b.get("vel") is None:
                b["vel"] = (0.0, 0.0)
        for i, L in enumerate(self.lasers):
            _laser_defaults(L, i)

    # ------------------------------------------------------------------ step
    def step(self, move):
        """Apply one frame of `move`. Returns (dead, info)."""
        p = self.state["player"]
        if self.dead:
            return True, {"t": self.t}
        if p.get("state", 0) != 0:                 # respawning/invulnerable
            self.t += 1
            return False, {"t": self.t}

        ux, uy = dict(MOVES)[move] if isinstance(move, str) else move
        px, py = p["pos"]
        px = min(max(px + ux * self.speed, MOVE_L), MOVE_R)
        py = min(max(py + uy * self.speed, MOVE_T), MOVE_B)
        p["pos"] = (px, py)

        for s in self.spawners:
            s(self, self.t)
        _advance_bullets(self)
        _advance_lasers(self)

        hit = _collides(self, px, py)
        if hit:
            self.dead = True
            self.death_t = self.t
            p["state"] = 2                          # DEAD
        self.t += 1
        return self.dead, {"t": self.t, "hit": hit}

    # -------------------------------------------------------------- plotting
    def render(self):
        import view
        return view.render(self.state)


# The eight directions plus staying put, in the order the oracle prefers ties.
_S = math.sqrt(0.5)
MOVES = [("left", (-1.0, 0.0)), ("right", (1.0, 0.0)),
         ("up", (0.0, -1.0)), ("down", (0.0, 1.0)),
         ("up-left", (-_S, -_S)), ("up-right", (_S, -_S)),
         ("down-left", (-_S, _S)), ("down-right", (_S, _S)),
         ("stay put", (0.0, 0.0))]


# --------------------------------------------------------------------- lasers
def _laser_defaults(L, idx=0):
    """Fill in the fields the live reader does not report.

    `read_lasers` gives what the oracle needs to score a frame: where the live
    part of the beam is now. The state machine that produced it also needs the
    timers, which are not read, so a pattern states them and a frame captured
    mid-attack gets a laser that is already in state 1 and stays there until it
    leaves the screen.
    """
    L.setdefault("idx", idx)
    L.setdefault("angle", 0.0)
    L.setdefault("length", max(0.0, L.get("endOffset", 200.0)
                               - L.get("startOffset", 0.0)))
    L.setdefault("width", 10.0)
    L.setdefault("speed", 0.0)
    L.setdefault("state", 1)
    L.setdefault("timer", 0)
    # endOffset/startOffset are the authoritative pair when present; `length`
    # is the reader's derived view of them.
    L.setdefault("startOffset", 0.0)
    L.setdefault("endOffset", L["startOffset"] + L["length"])
    L.setdefault("startLength", L["endOffset"] - L["startOffset"])
    L.setdefault("startTime", 0)
    L.setdefault("duration", 10 ** 6)
    L.setdefault("despawnDuration", 30)
    L.setdefault("hitboxStartTime", 0)
    L.setdefault("hitboxEndDelay", 10 ** 6)
    L.setdefault("inUse", True)
    # A pattern states the pivot and the two offsets; `pos` is the derived
    # segment start that the oracle reads, so compute it when it is missing.
    if "origin" in L and "pos" not in L:
        a = math.radians(L["angle"])
        L["pos"] = (L["origin"][0] + math.cos(a) * L["startOffset"],
                    L["origin"][1] + math.sin(a) * L["startOffset"])
    return L


def _advance_lasers(sim):
    alive = []
    for L in sim.lasers:
        if not L.get("inUse", True):
            continue
        # Both ends advance at `speed`; the near end is dragged along so the
        # visible length never exceeds startLength. BulletManager::OnUpdate.
        L["endOffset"] += L["speed"]
        if L["startLength"] < L["endOffset"] - L["startOffset"]:
            L["startOffset"] = L["endOffset"] - L["startLength"]
        if L["startOffset"] < 0.0:
            L["startOffset"] = 0.0

        st = L["state"]
        if st == 0 and L["timer"] >= L["startTime"]:
            L["timer"] = 0
            L["state"] = 1
        elif st == 1 and L["timer"] >= L["duration"]:
            L["timer"] = 0
            L["state"] = 2
            if L["despawnDuration"] == 0:
                L["inUse"] = False
        elif st == 2 and L["timer"] >= L["despawnDuration"]:
            L["inUse"] = False

        if L["startOffset"] >= LASER_DESPAWN_OFFSET:
            L["inUse"] = False
        L["timer"] += 1
        if L["inUse"]:
            # Keep the derived view the oracle reads in step with the machine.
            L["length"] = max(0.0, L["endOffset"] - L["startOffset"])
            L["pos"] = _laser_segment_start(L)
            alive.append(L)
    sim.lasers[:] = alive


def _laser_segment_start(L):
    """Where the live part of the beam starts, which is what the oracle scores.

    `read_lasers` folds `startOffset` into the origin the same way, so a frame
    from the game and a frame from here are read identically.
    """
    ox, oy = L.get("origin", L.get("pos", (0.0, 0.0)))
    a = math.radians(L["angle"])
    return (ox + math.cos(a) * L["startOffset"],
            oy + math.sin(a) * L["startOffset"])


def _laser_origin(L):
    """The beam's pivot. Stored separately because `pos` is the segment start."""
    if "origin" in L:
        return L["origin"]
    a = math.radians(L["angle"])
    x, y = L["pos"]
    return (x - math.cos(a) * L.get("startOffset", 0.0),
            y - math.sin(a) * L.get("startOffset", 0.0))


# -------------------------------------------------------------------- bullets
def _advance_bullets(sim):
    keep = []
    for b in sim.bullets:
        vx, vy = b.get("vel") or (0.0, 0.0)
        x, y = b["pos"][0] + vx, b["pos"][1] + vy
        b["pos"] = (x, y)
        if (-DESPAWN_MARGIN <= x <= FIELD_W + DESPAWN_MARGIN
                and -DESPAWN_MARGIN <= y <= FIELD_H + DESPAWN_MARGIN):
            keep.append(b)
    sim.bullets[:] = keep


# ----------------------------------------------------------------- collision
def _box_hit(ax, ay, ah, bx, by, bh):
    """AABB overlap of two boxes given centre and half-extent."""
    return not (ax + ah < bx - bh or ax - ah > bx + bh
                or ay + ah < by - bh or ay - ah > by + bh)


def _laser_hit(L, px, py):
    """`Player::CalcLaserHitbox`: the player box against the beam's own box.

    The player is moved into the beam's frame (relative to the pivot, rotated by
    -angle) and then it is a plain AABB test: `x` along the beam from
    `startOffset` to `endOffset`, `y` across it by `width`.
    """
    a = math.radians(L["angle"])
    c, s = math.cos(-a), math.sin(-a)
    ox, oy = _laser_origin(L)
    dx, dy = px - ox, py - oy
    rx = dx * c - dy * s
    ry = dx * s + dy * c
    half_w = L["width"] / 2.0
    return not (rx + PLAYER_HALF < L["startOffset"]
                or rx - PLAYER_HALF > L["endOffset"]
                or ry + PLAYER_HALF < -half_w
                or ry - PLAYER_HALF > half_w)


def _collides(sim, px, py):
    for b in sim.bullets:
        if not b.get("live", b.get("state", 1) == 1):
            continue
        half = b.get("half", DEFAULT_BULLET_HALF)
        if _box_hit(px, py, PLAYER_HALF, b["pos"][0], b["pos"][1], half):
            return "bullet"
    for L in sim.lasers:
        st = L["state"]
        # State 0 is lethal only from hitboxStartTime, 1 always, 2 for
        # hitboxEndDelay frames. Same switch as BulletManager::OnUpdate.
        if st == 0 and L["timer"] < L["hitboxStartTime"]:
            continue
        if st == 2 and L["timer"] >= L["hitboxEndDelay"]:
            continue
        if _laser_hit(L, px, py):
            return "laser"
    for e in sim.enemies:
        if not e.get("fatal"):
            continue
        if _box_hit(px, py, PLAYER_HALF, e["pos"][0], e["pos"][1], ENEMY_HALF):
            return "enemy"
    return None


# ----------------------------------------------------------------------- run
def run(state, policy, frames=600, spawners=(), speed=PLAYER_SPEED,
        stop_on_death=True, trace=None):
    """Play `policy(state) -> move` for `frames`, closed loop.

    Returns a dict with the outcome. `trace` is an optional list the per-frame
    rows are appended to, for inspecting a death.
    """
    sim = Sim(state, speed=speed, spawners=spawners)
    deaths = 0
    for _ in range(frames):
        move = policy(sim.state)
        dead, info = sim.step(move)
        if trace is not None:
            p = sim.state["player"]
            trace.append({"t": sim.t, "move": move, "pos": p["pos"],
                          "bullets": len(sim.bullets), "lasers": len(sim.lasers),
                          "hit": info.get("hit")})
        if dead:
            deaths += 1
            if stop_on_death:
                break
    return {"deaths": deaths, "frames": sim.t, "survived": not sim.dead,
            "death_t": sim.death_t, "sim": sim}


def dodger_policy(horizon=15):
    """The real dodger, so a pattern measures the shipped policy."""
    import dodger
    d = dodger.Dodger(horizon=horizon)
    return lambda st: d.choose(st)


# ------------------------------------------------------------------ selftest
def selftest():
    fails = 0

    def check(name, cond, detail=""):
        nonlocal fails
        if cond:
            print("  ok    %s" % name)
        else:
            fails += 1
            print("  FAIL  %s  %s" % (name, detail))

    def base():
        return {"player": {"pos": (192.0, 400.0), "state": 0, "speed": 4.0},
                "bullets": [], "enemies": [], "lasers": []}

    # --- the player box is 2.5 wide, not a radius-2 circle ------------------
    # A bullet centred 3.0 to the right of the player overlaps a 2.5 box only if
    # the bullet has any width at all; a radius-6 circle at distance 3 overlaps
    # far more. The exact boundary is what the oracle gets wrong, so pin it.
    st = base()
    st["bullets"] = [{"pos": (192.0 + 1.25 + 6.0 - 0.01, 400.0),
                      "vel": (0.0, 0.0), "state": 1, "live": True,
                      "half": 6.0}]
    sim = Sim(st)
    dead, _ = sim.step("stay put")
    check("a bullet just touching the player box kills", dead)

    st = base()
    st["bullets"] = [{"pos": (192.0 + 1.25 + 6.0 + 0.5, 400.0),
                      "vel": (0.0, 0.0), "state": 1, "live": True,
                      "half": 6.0}]
    sim = Sim(st)
    dead, _ = sim.step("stay put")
    check("a bullet just clear of it does not", not dead)

    # --- only state 1 kills -------------------------------------------------
    st = base()
    st["bullets"] = [{"pos": (192.0, 400.0), "vel": (0.0, 0.0),
                      "state": 3, "live": False}]
    sim = Sim(st)
    dead, _ = sim.step("stay put")
    check("a spawning bullet on the player does not kill", not dead)

    # --- a laser is a box, and its ends advance -----------------------------
    # Length 500 so the beam actually spans the player's row; width 20 is the
    # full beam width, so half is 10.
    st = base()
    st["lasers"] = [{"pos": (192.0, 0.0), "origin": (192.0, 0.0), "angle": 90.0,
                     "length": 500.0, "startOffset": 0.0, "endOffset": 500.0,
                     "startLength": 500.0, "width": 20.0, "speed": 0.0,
                     "state": 1, "timer": 0, "duration": 10 ** 6,
                     "despawnDuration": 30, "hitboxEndDelay": 10 ** 6}]
    sim = Sim(st)
    dead, _ = sim.step("stay put")
    check("a vertical beam through the player kills", dead)

    # The player box reaches x=193.25, so a beam whose left edge is at 196 is
    # 2.75 clear. 192+14 with width 20 is that beam.
    st = base()
    st["lasers"] = [{"pos": (206.0, 0.0), "origin": (206.0, 0.0),
                     "angle": 90.0, "length": 500.0, "startOffset": 0.0,
                     "endOffset": 500.0, "startLength": 500.0, "width": 20.0,
                     "speed": 0.0, "state": 1, "timer": 0, "duration": 10 ** 6,
                     "despawnDuration": 30, "hitboxEndDelay": 10 ** 6}]
    sim = Sim(st)
    dead, _ = sim.step("stay put")
    check("a beam clear of the player box does not", not dead)

    # A short beam that grows into the player: safe now, lethal later. This is
    # the case the oracle's static reading got wrong.
    st = base()
    st["lasers"] = [{"pos": (192.0, 0.0), "origin": (192.0, 0.0), "angle": 90.0,
                     "length": 0.0, "startOffset": 0.0, "endOffset": 0.0,
                     "startLength": 500.0, "width": 20.0, "speed": 40.0,
                     "state": 1, "timer": 0, "duration": 10 ** 6,
                     "despawnDuration": 30, "hitboxEndDelay": 10 ** 6}]
    sim = Sim(st)
    hit_t = None
    for _ in range(20):
        dead, _ = sim.step("stay put")
        if dead:
            hit_t = sim.t
            break
    check("a growing beam reaches a stationary player", hit_t is not None,
          "never hit in 20 frames at 40 u/f from 400u away")

    # --- a laser's near end is dragged along, so length is bounded ----------
    L = _laser_defaults({"pos": (0.0, 0.0), "origin": (0.0, 0.0), "angle": 0.0,
                         "startOffset": 0.0, "endOffset": 0.0,
                         "startLength": 50.0, "speed": 10.0, "width": 5.0,
                         "state": 1})
    sim = Sim(base())
    sim.lasers = [L]
    for _ in range(20):
        _advance_lasers(sim)
    check("a beam never grows past startLength",
          abs((L["endOffset"] - L["startOffset"]) - 50.0) < 1e-6,
          "%s" % (L["endOffset"] - L["startOffset"]))
    check("the near end is dragged along", L["startOffset"] > 0.0,
          "startOffset=%s" % L["startOffset"])

    # --- the player is clamped to the movement area, not the playfield -----
    st = base()
    st["player"]["pos"] = (FIELD_W - 1.0, FIELD_H - 1.0)
    sim = Sim(st)
    for _ in range(10):
        sim.step("down-right")
    check("the player stops at the movement area, not the field edge",
          sim.state["player"]["pos"] == (MOVE_R, MOVE_B),
          repr(sim.state["player"]["pos"]))
    check("and that box is smaller than the playfield",
          MOVE_R < FIELD_W and MOVE_B < FIELD_H,
          "%s %s" % (MOVE_R, MOVE_B))

    # --- off-screen bullets despawn ----------------------------------------
    st = base()
    st["bullets"] = [{"pos": (192.0, 10.0), "vel": (0.0, -20.0),
                      "state": 1, "live": True, "half": 6.0}]
    sim = Sim(st)
    for _ in range(5):
        sim.step("stay put")
    check("a bullet that leaves the field is dropped", not sim.bullets,
          "%d left" % len(sim.bullets))

    # --- a deterministic closed loop ---------------------------------------
    st = base()
    st["bullets"] = [{"pos": (192.0, 100.0 + 24 * i), "vel": (0.0, 5.0),
                      "state": 1, "live": True, "half": 6.0}
                     for i in range(10)]
    import dodger
    out = run(st, dodger_policy(), frames=40)
    check("the dodger escapes a falling column here too", out["survived"],
          "died at frame %s" % out["death_t"])

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
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        raise SystemExit(1 if selftest() else 0)

    import patterns
    if args.patterns or args.pattern is None:
        names = list(patterns.PATTERNS) if not args.pattern else [args.pattern]
    else:
        names = [args.pattern]

    for name in names:
        spec = patterns.build(name)
        trace = [] if args.trace else None
        out = run(spec["state"], dodger_policy(), frames=args.frames,
                  spawners=spec.get("spawners", ()), trace=trace)
        print("%-22s %s  %4d frames  bullets %-4d lasers %-3d %s"
              % (name, "SURVIVED" if out["survived"] else "DIED    ",
                 out["frames"], len(out["sim"].bullets),
                 len(out["sim"].lasers),
                 "" if out["survived"] else "at frame %s" % out["death_t"]))
        if args.trace and not out["survived"]:
            for row in trace[-12:]:
                print("      t=%-4d move=%-11s pos=(%6.1f,%5.1f) b=%-4d L=%-2d %s"
                      % (row["t"], row["move"], row["pos"][0], row["pos"][1],
                         row["bullets"], row["lasers"], row["hit"] or ""))


if __name__ == "__main__":
    main()
