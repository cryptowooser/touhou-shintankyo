#!/usr/bin/env python3
"""Ask the model about constructed boards whose answer is already known.

Playing runs mix three questions together: does the model read the board, does it
dodge well, and how lucky was the geometry. A 500-decision run yields about 178
usable frames, which is not enough to separate them -- the last run put the model
81% against `always down` at 83%, three frames apart, which settles nothing.

This constructs the boards instead. Ground truth comes from `oracle.py`, so the
answer key is simulated rather than hand-written, and because the boards are
synthetic there is no game, no deaths and no waiting. The same question can be
asked hundreds of times in a minute.

The sharpest mode is `--block`. One bullet is parked directly in the path of a
single direction, close enough to be hit inside the horizon but far enough that
the two diagonals beside it clear by a wide margin. The oracle then reports
exactly one lethal move, and the question "does the model avoid the blocked
direction" has an unambiguous answer. `--control` runs the identical boards with
the blocker removed, which measures how often the model picks that direction when
nothing is there -- the base rate that a reader has to beat.

  boardtest.py --block                 5 variants x 8 directions
  boardtest.py --block --variants 3
  boardtest.py --control                the same boards, no blocker
  boardtest.py --random --n 60          realistic boards, 1-4 lethal moves
  boardtest.py --show                   print one rendered board per direction
"""
import argparse
import collections
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import de_client as DE  # noqa: E402
import oracle  # noqa: E402
import view  # noqa: E402

_S = math.sqrt(0.5)
DIRS = [("left", (-1.0, 0.0)), ("right", (1.0, 0.0)),
        ("up", (0.0, -1.0)), ("down", (0.0, 1.0)),
        ("up-left", (-_S, -_S)), ("up-right", (_S, -_S)),
        ("down-left", (-_S, _S)), ("down-right", (_S, _S))]
OPTIONS = DE.move_options(stay_put=False)
MENU = [d for _, d in OPTIONS]

# Which view encoding to send. Set from --format, so a comparison across
# encodings uses the same boards with the encoding as the only variable.
FMT = "crop"

# The question. The model reliably moves toward the most distinctive thing in
# the evidence -- a bullet it cannot even be hit by pulls it 98% of the time --
# so asking it to find the threat may work where asking it to avoid the threat
# does not. --danger swaps in the inverted question.
MOVE_QUESTION = DE.MOVE_QUESTION
DANGER_QUESTION = ("Which direction is most dangerous for the player to move "
                   "in?")
BINARY_QUESTION = ("If the player moves %s, will a bullet hit them within the "
                   "next 15 frames?")
BINARY_OPTIONS = [("A", "yes"), ("B", "no")]
CRITERION = MOVE_QUESTION

# Player positions and blocker distances. Every position is at least 60 units
# from each edge, which is the full 15-frame reach at 4.0 u/f, so no move is
# clipped by the field boundary and the geometry stays the only variable.
# Distances are all well past the 11.3 units where a diagonal would start to
# clip the blocker: at 24 units the two neighbouring diagonals clear it by 17.
VARIANTS = [
    (24.0, (192.0, 224.0)),
    (16.0, (192.0, 224.0)),
    (30.0, (150.0, 200.0)),
    (20.0, (240.0, 260.0)),
    (26.0, (120.0, 300.0)),
]


def make_variants(n, rng):
    """The fixed five, then as many generated ones as asked for. Every player
    position keeps a 60-unit margin so no move is clipped by the field edge.
    Distances stay in 14-34: above the 11.3 where a diagonal would start to clip
    the column, and below the 60 the player can cover in the horizon."""
    out = list(VARIANTS)
    while len(out) < n:
        out.append((rng.uniform(14.0, 34.0),
                    (rng.uniform(64.0, view.FIELD_W - 64.0),
                     rng.uniform(64.0, view.FIELD_H - 64.0))))
    return out[:n]


def state(player, bullets, enemies=(), speed=4.0):
    """A snapshot shaped like state.py's, which is what the view and oracle want."""
    return {
        "player": {"pos": player, "state": 0, "speed": speed},
        "bullets": [{"idx": i, "pos": b[0], "vel": b[1], "state": 1,
                     "size": (8.0, 8.0), "live": True}
                    for i, b in enumerate(bullets)],
        "enemies": [{"idx": i, "pos": e, "hitbox": (8.0, 8.0), "fatal": True}
                    for i, e in enumerate(enemies)],
        "lasers": [],
        "lives": 3, "bombs": 3, "power": 64,
    }


def column(direction, player, dists):
    """Bullets strung out along one direction, like an aimed stream.

    Every one of them lies on the path of `direction`, so `direction` stays the
    only lethal move: the diagonals beside it diverge at 45 degrees and clear a
    bullet at distance r by 0.707r, which is more than the 8-unit collision
    radius for every r used here. That keeps the answer key unambiguous while
    the board still looks like a board rather than one dot on an empty grid.
    """
    ux, uy = dict(DIRS)[direction]
    return [((player[0] + ux * r, player[1] + uy * r), (0.0, 0.0)) for r in dists]


def block_board(direction, dist, player, rng):
    return state(player, column(direction, player, (dist, dist + 16.0, dist + 32.0)))


def control_board(direction, dist, player, rng):
    """The same board with the stream removed, to measure the base rate at which
    the model picks that direction when nothing is there."""
    return state(player, [])


def single_block_board(direction, dist, player, rng=None):
    """One stationary bullet, to match approach_board's bullet count.

    The column version differs from the approaching board in three ways at once
    -- bullet count, trail length and whether the bullet is moving -- so this
    removes the first. The other two are inherent to the comparison.
    """
    ux, uy = dict(DIRS)[direction]
    return state(player, [((player[0] + ux * dist, player[1] + uy * dist),
                           (0.0, 0.0))])


def clutter_board(direction, dist, player, rng, n_clutter=18):
    """One lethal bullet plus a field full of harmless ones.

    The single-threat boards are the only ones where a constant policy cannot
    win, but they have one bullet on screen and real frames have twenty. Every
    extra bullet is parked beyond the player's reach -- 4 u/f over 15 frames is
    60u, so anything past 100u cannot be arrived at -- which keeps the lethal
    direction unique while making the board as busy as a stage.
    """
    ux, uy = dict(DIRS)[direction]
    bullets = [((player[0] + ux * dist, player[1] + uy * dist), (0.0, 0.0))]
    for _ in range(600):
        if len(bullets) > n_clutter:
            break
        ang = rng.uniform(0, 2 * math.pi)
        r = rng.uniform(100.0, 250.0)
        bx, by = player[0] + math.cos(ang) * r, player[1] + math.sin(ang) * r
        if 8.0 <= bx < view.FIELD_W - 8.0 and 8.0 <= by < view.FIELD_H - 8.0:
            bullets.append(((bx, by), (0.0, 0.0)))
    return state(player, bullets)


def approach_board(direction, dist, player, rng=None, speed=4.0):
    """A bullet aimed straight at the player along `direction`.

    Same blocked direction as `block_board`, different marker. Because it is
    closing on the player's current position inside the horizon the crop marks
    it `,` (reaches you within 15f) or `x` (within 5f), where a stationary
    column is only ever `o` (bullet now). If the model reads the markers, this
    board should be avoided far more often than the stationary one.

    With dist=40 and speed=4.0 it arrives at frame 10, so moving in
    `direction` meets it and staying put is hit, but the perpendicular moves
    are 40 units clear by then. Offered lethal should still be exactly one.
    """
    ux, uy = dict(DIRS)[direction]
    return state(player, [((player[0] + ux * dist, player[1] + uy * dist),
                           (-ux * speed, -uy * speed))])


def harmless_board(direction, dist, player, rng=None, far=70.0):
    """The same single `o` mark at the same bearing, but too far to reach.

    Nothing on this board is lethal: 70 units is beyond the 60 the player can
    cover in the horizon. So the mark is identical to single_block_board's and
    only the threat is gone. If the model's answer does not change, the mark is
    driving the choice and the danger never entered into it.
    """
    ux, uy = dict(DIRS)[direction]
    return state(player, [((player[0] + ux * far, player[1] + uy * far),
                           (0.0, 0.0))])


def halfstep_board(direction, dist, player, rng=None):
    """One stationary bullet at the same range, rotated 22.5 degrees off the axis.

    Same distance and same lone `o` as single_block_board, but sitting between
    two moves, so every path clears it: the closest approach is r*sin(22.5) =
    0.383r, which is past the 8-unit collision radius for any r above 21. This
    removes the distance confound from harmless_board, where the safe mark had
    to sit 70 units out.
    """
    idx = [d for d, _ in DIRS].index(direction)
    ux, uy = DIRS[idx][1]
    a = math.radians(22.5)
    rx = ux * math.cos(a) - uy * math.sin(a)
    ry = ux * math.sin(a) + uy * math.cos(a)
    return state(player, [((player[0] + rx * dist, player[1] + ry * dist),
                           (0.0, 0.0))])


def random_board(rng, lo=1, hi=4, n_bullets=14):
    """A board drawn from a plausible distribution, kept only if it is
    discriminating but winnable."""
    for _ in range(400):
        player = (rng.uniform(80.0, view.FIELD_W - 80.0),
                  rng.uniform(80.0, view.FIELD_H - 80.0))
        bullets = []
        for _ in range(n_bullets):
            ang = rng.uniform(0, 2 * math.pi)
            dist = rng.uniform(20.0, 110.0)
            x = player[0] + math.cos(ang) * dist
            y = player[1] + math.sin(ang) * dist
            if not (4.0 < x < view.FIELD_W - 4.0 and 4.0 < y < view.FIELD_H - 4.0):
                continue
            speed = rng.uniform(1.0, 4.0)
            bullets.append(((x, y), (math.cos(ang) * speed, math.sin(ang) * speed)))
        if not bullets:
            continue
        st = state(player, bullets)
        safe = oracle.safe_moves(st)
        lethal = [m for m in MENU if m not in safe]
        if lo <= len(lethal) <= hi:
            return st, lethal
    return None, None


def ask(st):
    text = view.render_as(st, FMT)
    ans = DE.read_text(text, CRITERION, OPTIONS)
    return text, ans


def report_block(rows, label, control=False):
    """rows: (direction, blocked_is_lethal, choice, avoided, ok)."""
    print("=" * 72)
    print(label)
    print("=" * 72)
    n = len(rows)
    if not n:
        print("  no boards")
        return
    bad = [r for r in rows if not r["ground_truth_exact"]]
    if bad:
        print("  %d/%d boards were DISCARDED: the oracle did not report the"
              % (len(bad), n))
        print("  expected lethal set, so the answer key was not unambiguous.")
        rows = [r for r in rows if r["ground_truth_exact"]]
        n = len(rows)
        if not n:
            return
    if control:
        # There is nothing to avoid here; the point is the base rate.
        print("  %d boards with no lethal move at all" % n)
        print()
        print("  choices: %s"
              % ", ".join("%s %d (%.0f%%)" % (k, v, 100.0 * v / n)
                          for k, v in collections.Counter(
                              r["choice"] for r in rows).most_common()))
        conf = sorted(r["confidence"] for r in rows)
        print("  confidence: median %.2f" % conf[len(conf) // 2])
        print()
        print("  This is the prior: what the model says when the board carries no")
        print("  information. Compare it against the choices on the blocked boards.")
        return
    avoided = sum(1 for r in rows if r["avoided"])
    print("  %d boards, each with exactly one lethal move" % n)
    print()
    print("  model avoided the lethal direction:  %d/%d  (%.0f%%)"
          % (avoided, n, 100.0 * avoided / n))
    print()
    print("  by direction:")
    for d, _ in DIRS:
        sub = [r for r in rows if r["direction"] == d]
        if sub:
            a = sum(1 for r in sub if r["avoided"])
            picks = collections.Counter(r["choice"] for r in sub)
            print("     %-11s avoided %d/%d   picked: %s"
                  % (d, a, len(sub),
                     ", ".join("%s %d" % (k, v) for k, v in picks.most_common())))
    print()
    print("  choices overall: %s"
          % ", ".join("%s %d" % (k, v)
                      for k, v in collections.Counter(
                          r["choice"] for r in rows).most_common()))
    conf_ok = sorted(r["confidence"] for r in rows if r["avoided"])
    conf_bad = sorted(r["confidence"] for r in rows if not r["avoided"])
    print()
    print("  confidence when it avoided:      median %.2f"
          % conf_ok[len(conf_ok) // 2])
    if conf_bad:
        print("  confidence when it did not:      median %.2f"
              % conf_bad[len(conf_bad) // 2])

    # Cardinals against diagonals. On the blocked set these behave so
    # differently that reporting one number hides the whole finding.
    card = [r for r in rows if r["direction"] in ("left", "right", "up", "down")]
    diag = [r for r in rows if r["direction"] not in ("left", "right", "up", "down")]
    if card and diag:
        ca = sum(1 for r in card if r["avoided"])
        da = sum(1 for r in diag if r["avoided"])
        print()
        print("  cardinals: %3d/%d  (%.0f%%)   diagonals: %3d/%d  (%.0f%%)"
              % (ca, len(card), 100.0 * ca / len(card),
                 da, len(diag), 100.0 * da / len(diag)))

    # The baseline that decides whether any of the above means anything. Every
    # board here has exactly one lethal move, so a policy of always naming one
    # direction avoids it on every board except the ones that direction blocks.
    # The first version of this tool reported 88% on the approaching set without
    # it, and `always left` scores exactly the same 77/88 -- the whole number was
    # the prior. Same discipline as score_run.py, for the same reason.
    consts = sorted(((m, sum(1 for r in rows if m not in r["lethal"]))
                     for m in MENU), key=lambda kv: -kv[1])
    print()
    print("  constant-policy baselines (these ignore the board entirely):")
    for m, c in consts:
        print("     always %-11s %4d/%d  (%.0f%%)"
              % (m, c, n, 100.0 * c / n))
    best_m, best_c = consts[0]
    print()
    print("  best constant: `always %s` at %d/%d (%.0f%%)"
          % (best_m, best_c, n, 100.0 * best_c / n))
    if avoided <= best_c:
        print("  The model did NOT beat a policy that ignores the board.")
    else:
        print("  The model beat it by %d/%d. Check that the margin is not just"
              % (avoided - best_c, n))
        print("  the direction counts: the blocked direction is uniform here, but")
        print("  the model's prior is not.")


def dump(rows, path):
    """Keep the raw rows so the analysis can be redone without re-asking."""
    if not path:
        return
    import json
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1)
    print("  rows written to %s" % path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--block", action="store_true")
    ap.add_argument("--marker", action="store_true")
    ap.add_argument("--harmless", action="store_true")
    ap.add_argument("--halfstep", action="store_true")
    ap.add_argument("--danger", action="store_true",
                    help="ask which direction is most dangerous instead")
    ap.add_argument("--danger-random", action="store_true",
                    help="danger question on busy boards; the real test of it")
    ap.add_argument("--binary", action="store_true",
                    help="one direction at a time, yes/no; removes selection")
    ap.add_argument("--binary-single", action="store_true",
                    help="binary ranking on single-threat boards, where a "
                         "constant policy cannot win")
    ap.add_argument("--clutter", type=int, default=0,
                    help="with --binary-single, add this many out-of-reach "
                         "bullets so the board is as busy as a real frame")
    ap.add_argument("--binary-hard", action="store_true",
                    help="binary ranking on boards where most directions kill")
    ap.add_argument("--random-hard", action="store_true",
                    help="the eight-way question on the same hard boards, to "
                         "separate the framing from the boards")
    ap.add_argument("--control", action="store_true")
    ap.add_argument("--random", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--variants", type=int, default=len(VARIANTS))
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--model", default=DE.DEFAULT_MODEL)
    ap.add_argument("--format", default="crop", choices=sorted(view.FORMATS),
                    help="which view encoding to send")
    ap.add_argument("--json", default=None,
                    help="write the per-board rows here for later analysis")
    args = ap.parse_args()
    rng = random.Random(20261009)
    global FMT, CRITERION
    FMT = args.format
    if args.danger or args.danger_random:
        CRITERION = DANGER_QUESTION
    if args.block or args.control or args.marker or args.harmless \
            or args.halfstep or args.random or args.danger \
            or args.danger_random or args.binary or args.binary_single \
            or args.binary_hard or args.random_hard:
        print("view encoding: %s   question: %s" % (FMT, CRITERION))

    if args.show:
        for d, _ in DIRS:
            st = block_board(d, 24.0, (192.0, 224.0), rng)
            safe = sorted(oracle.safe_moves(st))
            lethal = [m for m in MENU if m not in safe]
            print("--- blocked: %s   oracle lethal: %s" % (d, lethal))
            print(view.render_as(st, "crop"))
            print()
        return

    if args.harmless:
        variants = make_variants(args.variants, rng)
        rows = []
        for d, _ in DIRS:
            for dist, player in variants:
                st = harmless_board(d, dist, player)
                safe = oracle.safe_moves(st)
                lethal = [m for m in MENU if m not in safe]
                text, ans = ask(st)
                rows.append({
                    "direction": d, "dist": dist, "player": player,
                    "lethal": lethal, "ground_truth_exact": (lethal == []),
                    "choice": ans["choice"],
                    "avoided": ans["choice"] not in lethal,
                    "confidence": ans["probabilities"][ans["letter"]],
                })
        n = len(rows)
        bad = sum(1 for r in rows if not r["ground_truth_exact"])
        print("=" * 72)
        print("HARMLESS: one mark at the same bearing, out of reach")
        print("=" * 72)
        print("  %d boards; %d had something lethal (should be 0)" % (n, bad))
        toward = sum(1 for r in rows if r["choice"] == r["direction"])
        print()
        print("  model moved toward the mark:  %d/%d  (%.0f%%)"
              % (toward, n, 100.0 * toward / n))
        print()
        print("  by direction (compare against the lethal single-bullet run):")
        for d, _ in DIRS:
            sub = [r for r in rows if r["direction"] == d]
            t = sum(1 for r in sub if r["choice"] == d)
            picks = collections.Counter(r["choice"] for r in sub)
            print("     %-11s toward %2d/%2d   picked: %s"
                  % (d, t, len(sub),
                     ", ".join("%s %d" % (k, v) for k, v in picks.most_common())))
        print()
        print("  choices overall: %s"
              % ", ".join("%s %d" % (k, v)
                          for k, v in collections.Counter(
                              r["choice"] for r in rows).most_common()))
        if args.json:
            dump(rows, args.json)
        return

    if args.danger:
        # Inverted scoring: the model is asked to name the danger, so success is
        # picking a direction the oracle says would kill.
        variants = make_variants(args.variants, rng)
        rows = []
        for d, _ in DIRS:
            for dist, player in variants:
                st = single_block_board(d, dist, player)
                safe = oracle.safe_moves(st)
                lethal = [m for m in MENU if m not in safe]
                text, ans = ask(st)
                rows.append({
                    "direction": d, "dist": dist, "player": player,
                    "lethal": lethal, "ground_truth_exact": (lethal == [d]),
                    "choice": ans["choice"],
                    "avoided": ans["choice"] not in lethal,
                    "named_danger": ans["choice"] in lethal,
                    "confidence": ans["probabilities"][ans["letter"]],
                })
        rows = [r for r in rows if r["ground_truth_exact"]]
        n = len(rows)
        named = sum(1 for r in rows if r["named_danger"])
        print("=" * 72)
        print("INVERTED: asked which direction is MOST DANGEROUS")
        print("=" * 72)
        print("  %d boards, exactly one lethal move each" % n)
        print()
        print("  model named the lethal direction:  %d/%d  (%.0f%%)"
              % (named, n, 100.0 * named / n))
        print("  naming one at random would give:   %.1f/%d  (%.0f%%)"
              % (n / 8.0, n, 12.5))
        print()
        conf = sorted(r["confidence"] for r in rows if r["named_danger"])
        if conf:
            print("  confidence when it named it: median %.2f"
                  % conf[len(conf) // 2])
        if named / float(n) > 0.5:
            print()
            print("  That is well above chance, which is the whole point: the")
            print("  model finds the distinctive item reliably. Inverting the")
            print("  question to match that bias is a real way to use it.")
        if args.json:
            dump(rows, args.json)
        return

    if args.halfstep:
        # Fixed 24 units, the same range as the lethal boards, and every variant
        # is at least 21 so the 0.383r clearance holds.
        variants = [v for v in make_variants(args.variants, rng) if v[0] >= 24.0]
        rows = []
        for d, _ in DIRS:
            for _dist, player in variants:
                st = halfstep_board(d, 24.0, player)
                safe = oracle.safe_moves(st)
                lethal = [m for m in MENU if m not in safe]
                text, ans = ask(st)
                rows.append({
                    "direction": d, "dist": 24.0, "player": player,
                    "lethal": lethal, "ground_truth_exact": (lethal == []),
                    "choice": ans["choice"],
                    "avoided": ans["choice"] not in lethal,
                    "confidence": ans["probabilities"][ans["letter"]],
                })
        n = len(rows)
        bad = sum(1 for r in rows if not r["ground_truth_exact"])
        print("=" * 72)
        print("HALFSTEP: one mark at 24u, rotated 22.5 degrees off every move")
        print("=" * 72)
        print("  %d boards; %d had something lethal (should be 0)" % (n, bad))
        toward = sum(1 for r in rows if r["choice"] == r["direction"])
        print()
        print("  model moved toward the mark's bearing: %d/%d (%.0f%%)"
              % (toward, n, 100.0 * toward / n))
        print()
        for lab, keep in (("mark above", ("up", "up-left", "up-right")),
                          ("mark below", ("down", "down-left", "down-right"))):
            sub = [r for r in rows if r["direction"] in keep]
            t = sum(1 for r in sub if r["choice"] == r["direction"])
            print("  %-11s toward %3d/%3d (%.0f%%)"
                  % (lab, t, len(sub), 100.0 * t / len(sub)))
        print()
        print("  choices overall: %s"
              % ", ".join("%s %d" % (k, v)
                          for k, v in collections.Counter(
                              r["choice"] for r in rows).most_common()))
        if args.json:
            dump(rows, args.json)
        return

    if args.marker:
        variants = make_variants(args.variants, rng)
        for kind, maker in (("column, stationary", block_board),
                            ("single, stationary", single_block_board),
                            ("single, approaching", approach_board)):
            rows = []
            for d, _ in DIRS:
                for dist, player in variants:
                    st = maker(d, dist, player, rng)
                    safe = oracle.safe_moves(st)
                    lethal = [m for m in MENU if m not in safe]
                    text, ans = ask(st)
                    rows.append({
                        "direction": d, "dist": dist, "player": player,
                        "lethal": lethal, "ground_truth_exact": (lethal == [d]),
                        "choice": ans["choice"],
                        "avoided": ans["choice"] not in lethal,
                        "confidence": ans["probabilities"][ans["letter"]],
                    })
            report_block(rows, "MARKER TEST: %s bullet blocking one direction"
                         % kind)
            if args.json:
                stem = args.json[:-5] if args.json.endswith(".json") else args.json
                # "single, stationary" -> "single-stationary", so the file is
                # usable on a command line without quoting.
                slug = kind.replace(", ", "-").replace(" ", "-")
                dump(rows, "%s-%s.json" % (stem, slug))
            print()
        return

    if args.block or args.control:
        variants = make_variants(args.variants, rng)
        rows = []
        for d, _ in DIRS:
            for dist, player in variants:
                if args.control:
                    st = control_board(d, dist, player, rng)
                else:
                    st = block_board(d, dist, player, rng)
                safe = oracle.safe_moves(st)
                lethal = [m for m in MENU if m not in safe]
                exact = (lethal == [d]) if args.block else (lethal == [])
                text, ans = ask(st)
                rows.append({
                    "direction": d, "dist": dist, "player": player,
                    "lethal": lethal, "ground_truth_exact": exact,
                    "choice": ans["choice"],
                    "avoided": ans["choice"] not in lethal,
                    "confidence": ans["probabilities"][ans["letter"]],
                    "probs": ans["probabilities"],
                })
        label = ("CONTROL: the same boards with the blocker removed"
                 if args.control else
                 "BLOCKED: a column of bullets parked in one direction's path")
        report_block(rows, label, control=args.control)
        dump(rows, args.json)
        if not args.control:
            print()
            base = collections.Counter(r["choice"] for r in rows)
            print("  A reader should avoid the blocked direction every time.")
            print("  Run --control for the rate it picks those directions when")
            print("  nothing is blocking them.")
        return

    if args.danger_random:
        # The single-threat version of this question is answered by the one
        # populated row. Real boards populate every row, so the heuristic has
        # nothing to grab and this is where it either survives or does not.
        rows = []
        while len(rows) < args.n:
            st, lethal = random_board(rng)
            if st is None:
                continue
            text, ans = ask(st)
            rows.append({"lethal": lethal, "choice": ans["choice"],
                         "named_danger": ans["choice"] in lethal,
                         "chance": len(lethal) / 8.0,
                         "confidence": ans["probabilities"][ans["letter"]]})
        n = len(rows)
        named = sum(1 for r in rows if r["named_danger"])
        chance = 100.0 * sum(r["chance"] for r in rows) / n
        print("=" * 72)
        print("INVERTED on BUSY boards: which direction is most dangerous?")
        print("=" * 72)
        print("  %d boards, 1-4 lethal moves each, every rays row populated" % n)
        print()
        print("  model named a lethal direction:  %d/%d  (%.0f%%)"
              % (named, n, 100.0 * named / n))
        print("  picking at random would give:    %.0f%%" % chance)
        print()
        if named / float(n) > chance / 100.0 + 0.15:
            print("  Above chance -- some signal survives on busy boards.")
        else:
            print("  At chance. The single-threat result was the populated")
            print("  row, and on a busy board there is no such row: the")
            print("  inverted question buys nothing where it would matter.")
        if args.json:
            dump(rows, args.json)
        return

    if args.binary_hard:
        # Random boards where most directions kill. A constant policy wins at
        # most ~40% here, so unlike the 1-4 lethal set this cannot be passed by
        # holding one direction, and unlike the clutter set the threats are in
        # reach and separated by timing rather than by an order of magnitude.
        boards = []
        while len(boards) < args.n:
            st, lethal = random_board(rng, lo=4, hi=8, n_bullets=20)
            if st is None:
                continue
            boards.append((st, lethal))
        rows = []
        for i, (st, lethal) in enumerate(boards):
            text = view.render_as(st, FMT)
            for d in MENU:
                ans = DE.read_text(text, BINARY_QUESTION % d, BINARY_OPTIONS)
                rows.append({"board": i, "direction": d, "truth": d in lethal,
                             "said_yes": ans["choice"] == "yes",
                             "confidence": ans["probabilities"][ans["letter"]]})
        nb = len(boards)

        def py(x):
            return x["confidence"] if x["said_yes"] else 1.0 - x["confidence"]

        low = sum(1 for i in range(nb)
                  if not min([r for r in rows if r["board"] == i], key=py)["truth"])
        best = max(sum(1 for i in range(nb) if not [r for r in rows
                   if r["board"] == i and r["direction"] == m][0]["truth"])
                   for m in MENU)
        avg = sum(len(l) for _, l in boards) / float(nb)
        print("=" * 72)
        print("BINARY RANKING on HARD boards (most directions kill)")
        print("=" * 72)
        print("  %d boards, %.1f lethal directions each on average" % (nb, avg))
        print("  %d boards x 8 questions = %d calls" % (nb, len(rows)))
        print()
        print("  move where the model says yes least:  %d/%d  (%.0f%%)"
              % (low, nb, 100.0 * low / nb))
        print("  best constant policy:                 %d/%d  (%.0f%%)"
              % (best, nb, 100.0 * best / nb))
        print()
        if low > best:
            print("  It beats every constant where most moves are fatal. This")
            print("  is the case that matters, because a stage is dense.")
        elif low == best:
            print("  It ties the best constant on the case that matters. The")
            print("  single-threat win does not carry into density.")
        else:
            print("  It loses to a constant policy on the case that matters.")
        if args.json:
            dump(rows, args.json)
        return

    if args.binary_single:
        # The random-board version of this is saturated: with 1-4 lethal moves
        # out of 8, a fixed direction survives ~95% and no policy can be told
        # apart from it. Here the lethal direction is varied across all eight,
        # so the best constant tops out at 7/8 and skill has somewhere to show.
        boards = []
        for d, _ in DIRS:
            for dist, player in make_variants(args.variants, rng):
                if args.clutter:
                    st = clutter_board(d, dist, player, rng, args.clutter)
                else:
                    st = single_block_board(d, dist, player)
                lethal = [m for m in MENU if m not in oracle.safe_moves(st)]
                if lethal != [d]:
                    continue
                boards.append((st, lethal))
        rows = []
        for i, (st, lethal) in enumerate(boards):
            text = view.render_as(st, FMT)
            for d in MENU:
                ans = DE.read_text(text, BINARY_QUESTION % d, BINARY_OPTIONS)
                rows.append({"board": i, "direction": d, "truth": d in lethal,
                             "said_yes": ans["choice"] == "yes",
                             "confidence": ans["probabilities"][ans["letter"]]})
        nb = len(boards)
        low = sum(1 for i in range(nb)
                  if not min([r for r in rows if r["board"] == i],
                             key=lambda x: x["confidence"] if x["said_yes"]
                             else 1.0 - x["confidence"])["truth"])
        best = max(sum(1 for i in range(nb)
                       if not [r for r in rows
                               if r["board"] == i and r["direction"] == m][0]["truth"])
                   for m in MENU)
        print("=" * 72)
        print("BINARY RANKING on single-threat boards")
        print("=" * 72)
        print("  %d boards x 8 questions = %d calls" % (nb, len(rows)))
        if args.clutter:
            print("  each board carries %d extra out-of-reach bullets"
                  % args.clutter)
        print()
        print("  move where the model says yes least:  %d/%d  (%.0f%%)"
              % (low, nb, 100.0 * low / nb))
        print("  best constant policy:                 %d/%d  (%.0f%%)"
              % (best, nb, 100.0 * best / nb))
        print()
        if low > best:
            print("  It beats every constant policy, which nothing in the")
            print("  eight-way framing managed. The signal is real and usable,")
            print("  at the cost of eight questions per decision.")
        elif low == best:
            print("  It ties the best constant. No usable edge: the ranking")
            print("  is no better than picking a fixed direction and holding.")
        else:
            print("  It loses to a constant policy. The discrimination seen")
            print("  in the pooled yes-rate does not survive being asked to")
            print("  actually choose.")
        if args.json:
            dump(rows, args.json)
        return

    if args.binary:
        # One direction per question, two options, neither salient. This takes
        # the selection bias out entirely: if the model cannot do the
        # simulation, there is nothing left for it to get right by accident.
        rows = []
        boards = 0
        while boards < args.n:
            st, lethal = random_board(rng)
            if st is None:
                continue
            boards += 1
            text = view.render_as(st, FMT)
            for d in MENU:
                ans = DE.read_text(text, BINARY_QUESTION % d, BINARY_OPTIONS)
                rows.append({"direction": d, "truth": d in lethal,
                             "said_yes": ans["choice"] == "yes",
                             "confidence": ans["probabilities"][ans["letter"]]})
        n = len(rows)
        acc = sum(1 for r in rows if r["said_yes"] == r["truth"])
        yes = sum(1 for r in rows if r["said_yes"])
        hit = [r for r in rows if r["truth"]]
        tp = sum(1 for r in hit if r["said_yes"])
        print("=" * 72)
        print("ONE DIRECTION AT A TIME: will moving <d> be fatal?")
        print("=" * 72)
        print("  %d questions over %d boards, 2 options each"
              % (n, boards))
        print()
        print("  accuracy:            %d/%d  (%.0f%%)"
              % (acc, n, 100.0 * acc / n))
        print("  answering 'no' always:      %.0f%%  <- the bar to beat"
              % (100.0 * (n - len(hit)) / n))
        print()
        print("  of %d fatal directions it said yes to %d  (%.0f%%)"
              % (len(hit), tp, 100.0 * tp / max(len(hit), 1)))
        print("  of %d safe directions it said yes to %d  (%.0f%%)"
              % (n - len(hit), yes - tp, 100.0 * (yes - tp) / max(n - len(hit), 1)))
        print()
        tpr = 100.0 * tp / max(len(hit), 1)
        fpr = 100.0 * (yes - tp) / max(n - len(hit), 1)
        gap = tpr - fpr
        by_dir = {}
        for r in rows:
            y, t = by_dir.setdefault(r["direction"], [0, 0])
            by_dir[r["direction"]] = [y + (1 if r["said_yes"] else 0), t + 1]
        print("  yes-rate by direction name (a prior over names, not the board):")
        for d, (y, t) in sorted(by_dir.items(), key=lambda kv: -kv[1][0]):
            print("      %-11s %2d/%2d  %3.0f%%" % (d, y, t, 100.0 * y / t))
        print()
        if tpr < 5.0 and fpr < 5.0:
            print("  It essentially never says yes (%.0f%% vs %.0f%%). The high"
                  % (tpr, fpr))
            print("  accuracy is the majority answer, not a judgement.")
        elif gap < 8.0:
            print("  Its yes-rate does not separate fatal from safe moves")
            print("  (gap %+.0f points): no usable discrimination." % gap)
        elif tpr < 50.0:
            print("  It does separate them (%+.0f points, %.1fx the rate), so some"
                  % (gap, tpr / max(fpr, 0.01)))
            print("  board signal survives the binary framing -- but it misses")
            print("  %.0f%% of fatal moves, so it is not usable as a filter on"
                  % (100.0 - tpr))
            print("  its own. Compare the yes-rate by name above before")
            print("  crediting the board: much of the split may be the name.")
        else:
            print("  It separates them (%+.0f points) and catches most fatal"
                  % gap)
            print("  moves. Usable, if 8 questions per decision is affordable.")
        if args.json:
            dump(rows, args.json)
        return

    if args.random or args.random_hard:
        lo, hi, nb_ = (4, 8, 20) if args.random_hard else (1, 4, 14)
        rows = []
        while len(rows) < args.n:
            st, lethal = random_board(rng, lo=lo, hi=hi, n_bullets=nb_)
            if st is None:
                continue
            text, ans = ask(st)
            rows.append({"lethal": lethal, "choice": ans["choice"],
                         "ok": ans["choice"] not in lethal,
                         "confidence": ans["probabilities"][ans["letter"]]})
        n = len(rows)
        ok = sum(1 for r in rows if r["ok"])
        print("=" * 72)
        if args.random_hard:
            print("HARD random boards: %d, %d-%d lethal moves of 8" % (n, lo, hi))
        else:
            print("RANDOM boards: %d, each with 1-4 lethal moves" % n)
        print("=" * 72)
        print("  model chose a surviving move:  %d/%d  (%.0f%%)"
              % (ok, n, 100.0 * ok / n))
        best = max(sum(1 for r in rows if m not in r["lethal"]) for m in MENU)
        print("  best constant policy:         %d/%d  (%.0f%%)"
              % (best, n, 100.0 * best / n))
        print()
        print("  by how many moves were lethal:")
        for k in range(lo, hi + 1):
            sub = [r for r in rows if len(r["lethal"]) == k]
            if sub:
                o = sum(1 for r in sub if r["ok"])
                exp = sum(1.0 - len(r["lethal"]) / len(MENU) for r in sub)
                print("     %d lethal: %3d boards, model %3d (%.0f%%), "
                      "random %.1f (%.0f%%)"
                      % (k, len(sub), o, 100.0 * o / len(sub),
                         exp, 100.0 * exp / len(sub)))
        exp = sum(1.0 - len(r["lethal"]) / len(MENU) for r in rows)
        print()
        print("  random would get: %.1f/%d (%.0f%%)" % (exp, n, 100.0 * exp / n))
        if ok > exp:
            print("  The model beat random by %.1f/%d." % (ok - exp, n))
        else:
            print("  The model did not beat random.")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
