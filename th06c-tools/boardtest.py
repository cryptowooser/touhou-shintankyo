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
    text = view.render_as(st, "crop")
    ans = DE.read_text(text, DE.MOVE_QUESTION, OPTIONS)
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
    ap.add_argument("--control", action="store_true")
    ap.add_argument("--random", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--variants", type=int, default=len(VARIANTS))
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--model", default=DE.DEFAULT_MODEL)
    ap.add_argument("--json", default=None,
                    help="write the per-board rows here for later analysis")
    args = ap.parse_args()
    rng = random.Random(20261009)

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

    if args.random:
        rows = []
        while len(rows) < args.n:
            st, lethal = random_board(rng)
            if st is None:
                continue
            text, ans = ask(st)
            rows.append({"lethal": lethal, "choice": ans["choice"],
                         "ok": ans["choice"] not in lethal,
                         "confidence": ans["probabilities"][ans["letter"]]})
        n = len(rows)
        ok = sum(1 for r in rows if r["ok"])
        print("=" * 72)
        print("RANDOM boards: %d, each with 1-4 lethal moves" % n)
        print("=" * 72)
        print("  model chose a surviving move:  %d/%d  (%.0f%%)"
              % (ok, n, 100.0 * ok / n))
        print()
        print("  by how many moves were lethal:")
        for k in range(1, 5):
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
