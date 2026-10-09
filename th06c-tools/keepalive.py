#!/usr/bin/env python3
"""Keep th06c lives and bombs topped up.

A death decrements lives and refills bombs from cfg_bombs (RVA 0x4AA6C and
0x4AA8B), so a single write does not last. This reapplies the target values to the
live counters whenever the game has changed them.

It only writes when a value has actually drifted from the target, so it stays
idle while nothing is happening. Values are written through gamestate, so they are
bounds-checked.

The module base is re-resolved on every tick, because ASLR moves it when the game
restarts. If the game is not running the watcher waits and reattaches rather than
exiting.

  keepalive.py                      # 8 lives and 8 bombs, every 3 seconds
  keepalive.py --interval 0.5       # tighter, safer against a quick double death
  keepalive.py --bombs 0            # leave bombs alone
  keepalive.py --once               # apply once and exit
"""
import os
import argparse
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gamestate as G   # noqa: E402
import memedit as M     # noqa: E402
import modbase as MB    # noqa: E402
import th06             # noqa: E402


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def attach():
    """Return (pid, handle, base), or None when the game is not running."""
    if not th06.pids_named(th06.PROC_NAME):
        return None
    try:
        pid, h = M.open_game(write=True)
    except SystemExit:
        return None
    _name, base, _size = MB.find_module(pid, "th06c")
    return pid, h, base


def tick(h, base, targets):
    """Write any target that has drifted. Returns {field: (old, new)}."""
    changed = {}
    for field, want in targets.items():
        if want is None:
            continue
        have = G.read_field(h, base, field)
        if have is None or have == want:
            continue
        G.write_field(h, base, field, want)
        changed[field] = (have, want)
    return changed


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=float, default=3.0,
                    help="seconds between checks (default 3)")
    ap.add_argument("--lives", type=int, default=8, help="target lives (default 8)")
    ap.add_argument("--bombs", type=int, default=8, help="target bombs (default 8)")
    ap.add_argument("--once", action="store_true", help="apply once and exit")
    args = ap.parse_args()

    targets = {"lives": args.lives, "bombs": args.bombs}
    log("target %s every %gs; Ctrl-C or stop the task to end"
        % (targets, args.interval))

    pid = None
    while True:
        try:
            conn = attach()
            if conn is None:
                if pid is not None:
                    log("game exited; waiting for it to come back")
                    pid = None
                time.sleep(args.interval)
                continue

            new_pid, h, base = conn
            if new_pid != pid:
                pid = new_pid
                log("attached pid %d base %016x; %s"
                    % (pid, base,
                       " ".join("%s=%s" % (f, G.read_field(h, base, f))
                                for f in targets)))

            changed = tick(h, base, targets)
            for field, (old, new) in changed.items():
                log("%s %s -> %s" % (field, old, new))

            if args.once:
                log("done")
                return
        except OSError as exc:
            # WriteProcessMemory refused, usually because the game died mid-write.
            log("write failed (%s); reattaching" % exc)
            pid = None
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
