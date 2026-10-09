#!/usr/bin/env python3
"""Compact text view of game state, for handing to a decision model.

The point is to give a model the *observations* and none of the *judgments*.
Positions and motion go in; safe cells, recommended directions, reachability and
time-to-impact stay out. The test for anything added here is: could a player see
it on screen? A player sees bullets and perceives their motion. A player does
not see a heat map of safety.

Layout: a fixed header, then one character per grid cell.

  @  you, your cell is clear for the whole horizon
  +  you, and a bullet reaches your cell within the horizon
  !  you, and a bullet or laser is in your cell now
  o  a bullet is here now
  #  a laser
  X  enemy that kills on touch
  x  a bullet arrives within SOON frames
  ,  a bullet arrives within the horizon
  e  enemy that does not kill on touch
  .  clear for the whole horizon

Precedence, most urgent first:  ! + o # X x @ , e .

Your own cell is the one that matters most, so it gets its own three symbols
rather than being hidden by whatever is arriving there. The header always names
your cell, so @, + and ! all tell you where you are; what they add is how soon
your cell stops being safe.

Coordinates are game units, y-down, origin at the playfield's top-left. The
playfield is 384x448 units and the grid is 32x36, so a cell is 12 x 12.44 units.

  view.py --mock NAME      render one synthetic scenario
  view.py --list           list scenarios
  view.py --all            render every scenario
  view.py --png            also write a PNG of each scenario
  view.py                  read the live game (needs it running)
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FIELD_W, FIELD_H = 384.0, 448.0
GRID_W, GRID_H = 32, 36
HORIZON = 15        # frames of lookahead; should match the commitment window
SOON = 5            # frames under which a bullet counts as imminent

ORDER = "!+o#Xx@,e"        # highest priority first; '.' is the default
PRIORITY = {s: len(ORDER) - i for i, s in enumerate(ORDER)}


def cell_of(x, y, cw=None, ch=None):
    cw = cw or FIELD_W / GRID_W
    ch = ch or FIELD_H / GRID_H
    return int(x / cw), int(y / ch)


def render(state, grid=(GRID_W, GRID_H), horizon=HORIZON, soon=SOON):
    """Render a state dict to the text view. Pure function, no I/O."""
    gw, gh = grid
    cw, ch = FIELD_W / gw, FIELD_H / gh
    cells = {}

    def put(cx, cy, sym):
        if not (0 <= cx < gw and 0 <= cy < gh):
            return
        cur = cells.get((cx, cy))
        if cur is None or PRIORITY[sym] > PRIORITY[cur]:
            cells[(cx, cy)] = sym

    player = state["player"]
    px, py = player["pos"]
    pcx, pcy = cell_of(px, py, cw, ch)

    # Lasers first so bullets and enemies can outrank them.
    for L in state.get("lasers", []):
        lx, ly = L["pos"]
        ang = math.radians(L.get("angle", 0.0))
        for i in range(int(L.get("length", 200.0)) + 1):
            put(*cell_of(lx + math.cos(ang) * i, ly + math.sin(ang) * i, cw, ch),
                "#")

    for b in state.get("bullets", []):
        bx, by = b["pos"]
        vx, vy = b.get("vel", (0.0, 0.0))
        for k in range(horizon + 1):
            cx, cy = cell_of(bx + vx * k, by + vy * k, cw, ch)
            if not (0 <= cx < gw and 0 <= cy < gh):
                continue
            if k == 0:
                put(cx, cy, "o")
            elif k <= soon:
                put(cx, cy, "x")
            else:
                put(cx, cy, ",")

    for e in state.get("enemies", []):
        ex, ey = e["pos"]
        put(*cell_of(ex, ey, cw, ch), "X" if e.get("fatal") else "e")

    # Your own cell, drawn last and by direct assignment: whatever was predicted
    # to arrive there is folded into the symbol instead of being hidden.
    here = cells.get((pcx, pcy))
    if here in ("o", "#"):
        cells[(pcx, pcy)] = "!"
    elif here in ("x", ","):
        cells[(pcx, pcy)] = "+"
    else:
        cells[(pcx, pcy)] = "@"

    bullets = state.get("bullets", [])
    enemies = state.get("enemies", [])
    lasers = state.get("lasers", [])
    fatal = sum(1 for e in enemies if e.get("fatal"))
    homing = sum(1 for b in bullets if b.get("homing"))

    head = [
        "you cell(%d,%d)/%dx%d  %.1fu/cell  speed %.1fu/f  state %s"
        % (pcx, pcy, gw, gh, cw, player.get("speed", 0.0),
           {0: "ALIVE", 1: "SPAWNING", 2: "DEAD", 3: "INVULNERABLE"}.get(
               player.get("state", 0), "?")),
        "horizon %df  bullets %d  homing %d  lasers %d  enemies %d fatal %d"
        % (horizon, len(bullets), homing, len(lasers), len(enemies), fatal),
        "lives %s  bombs %s  power %s"
        % (state.get("lives", "?"), state.get("bombs", "?"),
           state.get("power", "?")),
    ]

    ruler = "".join(str(c // 10 % 10) for c in range(gw))
    ruler2 = "".join(str(c % 10) for c in range(gw))
    body = ["      cols 0..%d go left to right, rows 0..%d go top to bottom"
            % (gw - 1, gh - 1),
            "      " + ruler, "      " + ruler2]
    for y in range(gh):
        body.append("%4d  %s" % (y, "".join(cells.get((x, y), ".") for x in range(gw))))

    return "\n".join(head) + "\n\n" + "\n".join(body)


# ------------------------------------------------------------------ scenarios
# Synthetic frames, built to have unambiguous answers so the model's reply can
# be judged by inspection. Player sits near the bottom centre at (192, 400).

PX, PY = 192.0, 400.0


def _player(**kw):
    d = {"pos": (PX, PY), "state": 0, "speed": 4.0}
    d.update(kw)
    return d


def _bullets(pts, vel=(0.0, 0.0), **kw):
    return [dict(pos=(x, y), vel=vel, **kw) for x, y in pts]


def _cluster(cx, cy, n=5, step=11.0, vel=(0.0, 0.0), **kw):
    half = (n - 1) / 2.0
    return _bullets([(cx, cy + (i - half) * step) for i in range(n)], vel, **kw)


def _base(**kw):
    s = {"player": _player(), "bullets": [], "enemies": [], "lasers": [],
         "lives": 3, "bombs": 2, "power": 64}
    s.update(kw)
    return s


SCENARIOS = {
    # Nothing near. Staying put should win.
    "open": _base(enemies=[dict(pos=(192.0, 60.0), fatal=False)]),

    # A cluster 3.5 cells to the left, closing at 4u/f -> reaches you in ~11f.
    # Sensible: right, or up/down out of its line. Not left, not stay.
    "cluster_left_closing": _base(
        bullets=_cluster(150.0, 400.0, vel=(4.0, 0.0))),

    # Same geometry, mirrored. The answer should mirror.
    "cluster_right_closing": _base(
        bullets=_cluster(234.0, 400.0, vel=(-4.0, 0.0))),

    # Same geometry as cluster_left_closing but the bullets are receding.
    # Sensible: stay. This is the counterfactual pair for motion.
    "cluster_left_receding": _base(
        bullets=_cluster(150.0, 400.0, vel=(-4.0, 0.0))),

    # A descending wall with one gap, left of the player and reachable in time.
    # The wall reaches the player's row in 10 frames, so staying under the solid
    # part is fatal and the gap must actually be reached.
    "wall_gap_left": _base(
        bullets=[dict(pos=(float(x), 340.0), vel=(0.0, 6.0))
                 for x in range(0, 384, 12) if not 120 <= x <= 180]),

    # Wall with a gap on the right instead.
    "wall_gap_right": _base(
        bullets=[dict(pos=(float(x), 340.0), vel=(0.0, 6.0))
                 for x in range(0, 384, 12) if not 204 <= x <= 264]),

    # A vertical laser one cell to the player's left, running the full height.
    "laser_left": _base(lasers=[dict(pos=(168.0, 0.0), angle=90.0,
                                     length=448.0, width=10.0)]),

    # A vertical stream falling onto the player, far enough that the escape is
    # real: the nearest bullet is 60u up and closes at 5u/f, so it lands in ~12
    # frames while the player can cross a whole cell in 3. Left or right escapes;
    # staying, or moving along the column, does not.
    "column_on_player": _base(
        bullets=[dict(pos=(192.0, float(y)), vel=(0.0, 5.0))
                 for y in range(100, 341, 24)]),
}


def main():
    argv = sys.argv[1:]
    if "--list" in argv:
        for name in SCENARIOS:
            print(name)
        return

    want_png = "--png" in argv
    if "--all" in argv:
        names = list(SCENARIOS)
    elif "--mock" in argv:
        names = [argv[argv.index("--mock") + 1]]
    else:
        print("live read not wired up yet; use --mock NAME, --all, or --list")
        return

    for name in names:
        state = SCENARIOS[name]
        text = render(state)
        print("=== %s ===" % name)
        print(text)
        if want_png:
            import pngout
            path = "view_%s.png" % name
            render_png(state, path)
            print("   wrote %s" % path)
        print()


# ------------------------------------------------------------- other formats
# The grid is one encoding of the same facts. These render the identical state
# differently, to find out whether the grid is the problem or the task is.

# Ordered clockwise on screen, where y grows downward.
DIRS = [("right", 0.0), ("down-right", 45.0), ("down", 90.0),
        ("down-left", 135.0), ("left", 180.0), ("up-left", -135.0),
        ("up", -90.0), ("up-right", -45.0)]


def _header(state, horizon=HORIZON):
    p = state["player"]
    return [
        "you (%.0f,%.0f)  speed %.1fu/f  horizon %df  state %s"
        % (p["pos"][0], p["pos"][1], p.get("speed", 0.0), horizon,
           {0: "ALIVE", 1: "SPAWNING", 2: "DEAD", 3: "INVULNERABLE"}.get(
               p.get("state", 0), "?")),
        "lives %s  bombs %s  power %s"
        % (state.get("lives", "?"), state.get("bombs", "?"),
           state.get("power", "?")),
    ]


def render_rays(state, horizon=HORIZON):
    """One row per direction: how many bullets, how far, how fast closing.

    Everything here is an observation. Closing speed is the bullet's velocity
    projected onto the line to you, so it is positive when the gap is shrinking.
    Deciding which direction to take is still the model's job.
    """
    px, py = state["player"]["pos"]
    buckets = {name: [] for name, _ in DIRS}
    here = 0
    for b in state.get("bullets", []):
        bx, by = b["pos"]
        dx, dy = bx - px, by - py
        dist = math.hypot(dx, dy)
        if dist < 1e-6:
            here += 1
            continue
        ang = math.degrees(math.atan2(dy, dx))
        name = DIRS[int((ang + 22.5) // 45) % 8][0]
        vx, vy = b.get("vel", (0.0, 0.0))
        closing = -(vx * dx + vy * dy) / dist
        buckets[name].append((dist, closing))

    out = _header(state, horizon)
    out.append("field edges from you: left %.0fu  right %.0fu  up %.0fu  down %.0fu"
               % (px, FIELD_W - px, py, FIELD_H - py))
    if here:
        out.append("IN YOUR CELL NOW: %d bullet(s)" % here)
    out.append("")
    out.append("%-10s %5s %10s %12s" % ("direction", "n", "nearest", "closing"))
    for name, _ in DIRS:
        hits = buckets[name]
        if not hits:
            out.append("%-10s %5d %10s %12s" % (name, 0, "--", "--"))
            continue
        dist, closing = min(hits)
        out.append("%-10s %5d %9.0fu %11.1f u/f"
                   % (name, len(hits), dist, closing))
    if state.get("lasers"):
        out.append("lasers: %d" % len(state["lasers"]))
    for e in state.get("enemies", []):
        dx, dy = e["pos"][0] - px, e["pos"][1] - py
        out.append("enemy %s %.0fu away at (%+.0f,%+.0f)%s"
                   % ("FATAL" if e.get("fatal") else "harmless",
                      math.hypot(dx, dy), dx, dy,
                      "" if e.get("fatal") else " (touch does not kill)"))
    return "\n".join(out)


def render_crop(state, half=7, horizon=HORIZON):
    """The same grid, windowed on the player so you are always dead centre."""
    gw, gh = GRID_W, GRID_H
    cw, ch = FIELD_W / gw, FIELD_H / gh
    cells = {}

    def put(cx, cy, sym):
        cur = cells.get((cx, cy))
        if cur is None or PRIORITY[sym] > PRIORITY[cur]:
            cells[(cx, cy)] = sym

    for b in state.get("bullets", []):
        bx, by = b["pos"]
        vx, vy = b.get("vel", (0.0, 0.0))
        for k in range(horizon + 1):
            cx, cy = cell_of(bx + vx * k, by + vy * k, cw, ch)
            put(cx, cy, "o" if k == 0 else ("x" if k <= SOON else ","))
    for L in state.get("lasers", []):
        lx, ly = L["pos"]
        ang = math.radians(L.get("angle", 0.0))
        for i in range(int(L.get("length", 200.0)) + 1):
            put(*cell_of(lx + math.cos(ang) * i, ly + math.sin(ang) * i, cw, ch),
                "#")
    for e in state.get("enemies", []):
        put(*cell_of(*e["pos"], cw, ch), "X" if e.get("fatal") else "e")

    px, py = state["player"]["pos"]
    pcx, pcy = cell_of(px, py, cw, ch)
    here = cells.get((pcx, pcy))
    cells[(pcx, pcy)] = ("!" if here in ("o", "#")
                         else "+" if here in ("x", ",") else "@")

    w = half * 2 + 1
    out = _header(state, horizon)
    out.append("window %dx%d cells, you at centre; each cell %.0fu" % (w, w, cw))
    out.append("legend: @ you  ! bullet on you  + bullet reaches you soon")
    out.append("        o bullet now  x bullet <=%df  , bullet <=%df  # laser"
               % (SOON, horizon))
    out.append("        X enemy fatal on touch  e enemy harmless  . clear")
    out.append("")
    for dy in range(-half, half + 1):
        row = []
        for dx in range(-half, half + 1):
            cx, cy = pcx + dx, pcy + dy
            if not (0 <= cx < gw and 0 <= cy < gh):
                row.append("#")          # field edge
            else:
                row.append(cells.get((cx, cy), "."))
        out.append("   " + "".join(row))
    return "\n".join(out)


FORMATS = {"grid": lambda s: render(s), "rays": render_rays,
           "crop": render_crop}


def render_as(state, fmt="grid"):
    if fmt not in FORMATS:
        raise SystemExit("unknown format %r; have %s"
                         % (fmt, ", ".join(sorted(FORMATS))))
    return FORMATS[fmt](state)


# ------------------------------------------------------------------- picture
BG = b"\x14\x16\x1a"        # BGR, dark
GRID = b"\x22\x24\x28"      # cell boundaries, barely visible
GREEN = b"\x00\xff\x00"     # bullet
BLUE = b"\xff\x80\x00"      # laser
RED = b"\x30\x30\xff"       # enemy, fatal on touch
CYAN = b"\xff\xc0\x40"      # enemy, harmless
WHITE = b"\xff\xff\xff"     # player
EDGE = b"\x50\x50\x50"      # field border
BULLET = b"\x00\xff\x00"    # bullet, bright
TRAIL = b"\x00\x50\x00"     # where a bullet will be, far
TRAIL_NEAR = b"\x00\x90\x00"  # where a bullet will be, within SOON
LASER = b"\xff\x80\x00"


def render_png(state, path, scale=2, with_grid=True):
    """Draw the raw state as a picture, so a human can check the text view."""
    w, h = int(FIELD_W), int(FIELD_H)
    cw, ch = FIELD_W / GRID_W, FIELD_H / GRID_H
    rows = [bytearray(BG * w) for _ in range(h)]

    if with_grid:
        for cx in range(GRID_W + 1):
            x = min(int(cx * cw), w - 1)
            for y in range(h):
                rows[y][x * 3:x * 3 + 3] = GRID
        for cy in range(GRID_H + 1):
            y = min(int(cy * ch), h - 1)
            for x in range(w):
                rows[y][x * 3:x * 3 + 3] = GRID

    def dot(x, y, color, r=1):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                nx, ny = int(x) + dx, int(y) + dy
                if 0 <= nx < w and 0 <= ny < h:
                    rows[ny][nx * 3:nx * 3 + 3] = color

    for L in state.get("lasers", []):
        lx, ly = L["pos"]
        ang = math.radians(L.get("angle", 0.0))
        half = L.get("width", 10.0) / 2.0
        for i in range(int(L.get("length", 200.0)) + 1):
            bx, by = lx + math.cos(ang) * i, ly + math.sin(ang) * i
            for off in range(-int(half), int(half) + 1):
                dot(bx + math.cos(ang + math.pi / 2) * off,
                    by + math.sin(ang + math.pi / 2) * off, BLUE, 0)

    for b in state.get("bullets", []):
        dot(b["pos"][0], b["pos"][1], GREEN, 2)

    for e in state.get("enemies", []):
        dot(e["pos"][0], e["pos"][1], RED if e.get("fatal") else CYAN, 3)

    px, py = state["player"]["pos"]
    for dy in range(-4, 5):
        for dx in range(-4, 5):
            if abs(dx) + abs(dy) <= 4:
                dot(px + dx, py + dy, WHITE, 0)

    if scale > 1:
        import pngout
        rows = pngout.upscale(rows, scale)
    import pngout
    pngout.write_png(path, rows)
    return path


def render_model_png(state, path, scale=2, horizon=HORIZON):
    """Whole-field picture aimed at a vision model, not at a human debugger.

    Differences from render_png: bullets are large, each bullet carries a faint
    trail of where it will be over the next `horizon` frames, and the player is a
    big white disc. No grid lines -- at this scale they are noise, and the grid
    was the worst text encoding we tested.
    """
    import pngout
    w, h = int(FIELD_W), int(FIELD_H)
    rows = [bytearray(BG * w) for _ in range(h)]

    def dot(x, y, color, r):
        x, y = int(x), int(y)
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if dx * dx + dy * dy > r * r:
                    continue
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    rows[ny][nx * 3:nx * 3 + 3] = color

    # field border, so the model can see the edges it must not cross
    for x in range(w):
        for y in (0, 1, h - 2, h - 1):
            rows[y][x * 3:x * 3 + 3] = EDGE
    for y in range(h):
        for x in (0, 1, w - 2, w - 1):
            rows[y][x * 3:x * 3 + 3] = EDGE

    for b in state.get("bullets", []):
        bx, by = b["pos"]
        vx, vy = b.get("vel", (0.0, 0.0))
        for k in range(1, horizon + 1):
            dot(bx + vx * k, by + vy * k, TRAIL if k > SOON else TRAIL_NEAR, 1)
        dot(bx, by, BULLET, 5)

    for L in state.get("lasers", []):
        lx, ly = L["pos"]
        ang = math.radians(L.get("angle", 0.0))
        half = int(L.get("width", 10.0) / 2.0)
        for i in range(int(L.get("length", 200.0)) + 1):
            for off in range(-half, half + 1):
                dot(lx + math.cos(ang) * i + math.cos(ang + math.pi / 2) * off,
                    ly + math.sin(ang) * i + math.sin(ang + math.pi / 2) * off,
                    LASER, 0)

    for e in state.get("enemies", []):
        dot(e["pos"][0], e["pos"][1], RED if e.get("fatal") else CYAN, 7)

    px, py = state["player"]["pos"]
    dot(px, py, (0, 0, 0), 9)          # dark ring, so white reads as white
    dot(px, py, WHITE, 7)

    if scale > 1:
        rows = pngout.upscale(rows, scale)
    pngout.write_png(path, rows)
    return path


if __name__ == "__main__":
    main()
