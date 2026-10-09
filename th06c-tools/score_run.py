#!/usr/bin/env python3
"""Score a controller run log. Reports what the log can and cannot support.

The thing this is built to avoid: quoting an accuracy number from frames that
prove nothing. On loose frames every move survives, so any policy scores 100%
and the model's answer carries no information. So every number here is split
into frames where the answer could be wrong and frames where it could not.

The comparison that matters is not the model against random -- random is a weak
opponent. It is the model against the best constant policy on the same frames,
because a constant policy ignores the board entirely. If `always down` matches
the model, the model has shown nothing. An earlier run looked like 8/8 until
that baseline was computed and every "do not move up" policy also scored 8/8.

  score_run.py                       newest run-*.jsonl
  score_run.py run-20261009-214246.jsonl
  score_run.py a.jsonl b.jsonl       score several, in order
"""
import collections
import glob
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import de_client as DE  # noqa: E402

ALL_MOVES = [d for _, d in DE.MOVE_OPTIONS]


def load(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def offered(row):
    """The menu this decision was offered. Older logs predate the field."""
    return row.get("options") or ALL_MOVES


def deaths(rows):
    """A death is a transition out of playerState 0, counted once per death.

    State 0 is ALIVE and 1/2/3 are all not-alive, so a single death shows up as
    a run of non-zero frames. Only the leading edge counts.
    """
    n, prev = 0, 0
    for r in rows:
        s = r.get("player_state")
        if s is None:
            return None
        if prev == 0 and s != 0:
            n += 1
        prev = s
    return n


def score(path, rng):
    rows = load(path)
    if not rows:
        print("%s: empty" % path)
        return
    safe_rows = [r for r in rows if r.get("oracle_safe")]
    print("=" * 72)
    print(os.path.basename(path))
    print("=" * 72)
    print("  %d decisions" % len(rows))
    if not safe_rows:
        print("  no oracle data; nothing to score")
        return

    opts = offered(safe_rows[0])
    print("  menu offered: %s" % ", ".join(opts))
    if len(opts) != len(ALL_MOVES):
        print("  (reduced menu, so constant-policy baselines are over these only)")

    ver = safe_rows[0].get("log_version")
    if ver is None or ver < 2:
        print()
        print("  WARNING: this log predates the live-bullet fix (version %s)."
              % (ver if ver is not None else "unversioned"))
        print("  Its oracle verdicts scored spawning and despawning bullets as")
        print("  lethal. On one run that affected 55% of the discriminating")
        print("  frames, so the numbers below are not comparable with a fixed run.")

    # Context for reading the rest: how much of the board could not kill.
    extra = sum(r.get("bullets", 0) - r.get("live_bullets", 0) for r in rows)
    if extra:
        frames = sum(1 for r in rows
                     if r.get("bullets", 0) != r.get("live_bullets", 0))
        print("  non-live bullets present: %d across %d frames" % (extra, frames))
        print("     (excluded from the evidence and the oracle on version 2+)")

    dur = rows[-1]["t"] - rows[0]["t"]
    rt = sorted(r["round_trip_ms"] for r in rows)
    age = sorted(r["snapshot_age_ms"] for r in rows)
    if dur > 0:
        print("  %.1f s -> %.2f decisions/s" % (dur, len(rows) / dur))
    print("  round trip  median %.0f ms  p90 %.0f ms  max %.0f ms"
          % (rt[len(rt) // 2], rt[int(len(rt) * 0.9)], rt[-1]))
    print("  snapshot age at decision  median %.0f ms" % age[len(age) // 2])
    d = deaths(rows)
    print("  deaths: %s" % ("%d" % d if d is not None
                            else "not logged (pre-dates player_state)"))

    print()
    print("  choice distribution:")
    for k, v in collections.Counter(r["choice"] for r in rows).most_common():
        print("     %-11s %4d  (%.0f%%)" % (k, v, 100.0 * v / len(rows)))

    # Split by whether the answer could have been wrong.
    def all_safe(r):
        return all(m in r["oracle_safe"] for m in opts)

    trivial = [r for r in safe_rows if all_safe(r)]
    constrained = [r for r in safe_rows if not all_safe(r)]
    winnable = [r for r in constrained
                if any(m in r["oracle_safe"] for m in opts)]
    doomed = [r for r in constrained
              if not any(m in r["oracle_safe"] for m in opts)]

    print()
    print("  frames where every offered move was safe:  %4d  (%.0f%%)"
          % (len(trivial), 100.0 * len(trivial) / len(safe_rows)))
    print("  frames with at least one lethal move:      %4d" % len(constrained))
    print("     of those, some move survived:           %4d" % len(winnable))
    print("     of those, every offered move died:     %4d" % len(doomed))

    if not winnable:
        print()
        print("  No discriminating frames, so the run proves nothing about")
        print("  whether the model reads the board. Run longer, or force the")
        print("  player to move so the geometry stops repeating.")
        return

    print()
    print("  --- on the %d discriminating frames ---" % len(winnable))
    ok = sum(1 for r in winnable if r["oracle_ok"])
    print("  model chose a surviving move:  %d/%d  (%.0f%%)"
          % (ok, len(winnable), 100.0 * ok / len(winnable)))

    # Which directions were lethal. If one direction dominates, the safe set is
    # skewed by where the player is standing rather than by the pattern.
    lethal = collections.Counter()
    for r in winnable:
        for m in opts:
            if m not in r["oracle_safe"]:
                lethal[m] += 1
    print("  lethal-move counts: %s"
          % ", ".join("%s %d" % (m, n) for m, n in lethal.most_common()))

    print()
    print("  constant-policy baselines on the same frames:")
    best = None
    for m in opts:
        b = sum(1 for r in winnable if m in r["oracle_safe"])
        print("     always %-11s %4d/%d  (%.0f%%)"
              % (m, b, len(winnable), 100.0 * b / len(winnable)))
        if best is None or b > best[1]:
            best = (m, b)
    trials = 2000
    tot = sum(sum(1 for r in winnable if rng.choice(opts) in r["oracle_safe"])
              for _ in range(trials))
    print("     random                  %4.1f/%d  (%.0f%%)"
          % (tot / trials, len(winnable), 100.0 * tot / trials / len(winnable)))

    print()
    print("  best constant policy: `always %s` at %d/%d (%.0f%%)"
          % (best[0], best[1], len(winnable), 100.0 * best[1] / len(winnable)))
    if ok <= best[1]:
        print("  The model did NOT beat a policy that ignores the board.")
    else:
        print("  The model beat it by %d/%d. Worth checking whether the gap"
              % (ok - best[1], len(winnable)))
        print("  survives a longer run before believing it.")

    # The best constant is only informative where it is wrong. On the frames it
    # gets right, matching it proves nothing.
    wrong = [r for r in winnable if best[0] not in r["oracle_safe"]]
    if wrong:
        wok = sum(1 for r in wrong if r["oracle_ok"])
        print()
        print("  --- on the %d frames where `always %s` dies ---"
              % (len(wrong), best[0]))
        print("  model chose a survivor:  %d/%d  (%.0f%%)"
              % (wok, len(wrong), 100.0 * wok / len(wrong)))
        for m in opts:
            b = sum(1 for r in wrong if m in r["oracle_safe"])
            if b:
                print("     always %-11s %4d/%d  (%.0f%%)"
                      % (m, b, len(wrong), 100.0 * b / len(wrong)))

    # The cleanest board-reading test the log supports: exactly one lethal move
    # among the offered menu. Reading the board should avoid it almost every
    # time. Ignoring the board hits it about 1/N of the time, same as random.
    one = [r for r in winnable
           if sum(1 for m in opts if m not in r["oracle_safe"]) == 1]
    if one:
        hit = [r for r in one if not r["oracle_ok"]]
        exp = len(one) / float(len(opts))
        print()
        print("  --- exactly one lethal move among the %d offered ---" % len(opts))
        print("  model picked the lethal move:  %d/%d  (%.0f%%)"
              % (len(hit), len(one), 100.0 * len(hit) / len(one)))
        print("  picking at random would:       %.1f/%d  (%.0f%%)"
              % (exp, len(one), 100.0 / len(opts)))
        if len(hit) > exp:
            print("  ...which is worse than random. Moving blindly would have")
            print("  avoided it more often, so on these frames the board is not")
            print("  being read at all.")
        else:
            print("  ...at or better than random, so the board is being read to")
            print("  some degree. This is the number to watch.")


def main():
    argv = sys.argv[1:]
    paths = [a for a in argv if not a.startswith("-")]
    if not paths:
        found = sorted(glob.glob("run-*.jsonl"))
        if not found:
            raise SystemExit("no run-*.jsonl here; pass a path")
        paths = [found[-1]]
    rng = random.Random(0)
    for p in paths:
        score(p, rng)
        print()


if __name__ == "__main__":
    main()
