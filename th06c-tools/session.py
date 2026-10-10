#!/usr/bin/env python3
"""One unattended measurement session.

Waits for th06c to appear, waits until the board is a live stage with bullets in
flight, runs the velocity-offset finder once (it is read-only, and it has never
been run with bullets on screen), then runs the two logged dodger runs back to
back: the 60 Hz policy and the identical policy throttled to the model's rate.

Nothing here presses a key until a controller run starts, and the controller
focuses the game window itself. The session log is written next to the run logs.

  session.py                      60 Hz for 60s, then 4 Hz for 60s
  session.py --seconds 90         longer window
  session.py --skip-finder        go straight to the dodger runs

Stop it with Ctrl-C, or stop the background task.
"""
import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state as S   # noqa: E402
import th06         # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def wait_for_game(timeout):
    """Return a Reader once th06c is running, or None on timeout."""
    t0 = time.time()
    noted = False
    while time.time() - t0 < timeout:
        if th06.pids_named(th06.PROC_NAME):
            try:
                return S.Reader()
            except SystemExit:
                pass                        # process is up, module not yet
        if not noted:
            noted = True
            log("waiting for %s to start..." % th06.PROC_NAME)
        time.sleep(1.0)
    return None


def wait_for_stage(reader, timeout):
    """Wait until the board is moving and has live bullets.

    A menu is a static board with no bullets; a stage is a moving board with
    bullets. Requiring both avoids running the finder against a menu, where it
    would spend its whole budget finding nothing.
    """
    t0 = time.time()
    last = None
    moving = 0
    while time.time() - t0 < timeout:
        try:
            snap = reader.snapshot()
        except Exception as e:                       # noqa: BLE001
            log("read failed while waiting for a stage: %s" % e)
            time.sleep(1.0)
            continue
        live = sum(1 for b in snap["bullets"] if b["live"])
        sig = (len(snap["bullets"]),
               round(sum(b["pos"][0] + b["pos"][1]
                         for b in snap["bullets"]), 1))
        moving = moving + 1 if sig != last else 0
        last = sig
        if live and moving >= 3:
            log("stage is live: %d live bullets, player state %d"
                % (live, snap["player"]["state"]))
            return True
        time.sleep(0.5)
    return False


def run(cmd, label):
    log("--- %s ---" % label)
    log(" ".join(os.path.basename(c) for c in cmd))
    t0 = time.time()
    rc = subprocess.call(cmd, cwd=HERE)
    log("--- %s finished rc=%d in %.0fs ---" % (label, rc, time.time() - t0))
    return rc


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=60.0,
                    help="alive-time window for each dodger run (default 60)")
    ap.add_argument("--wait-game", type=float, default=900.0,
                    help="seconds to wait for th06c to start (default 900)")
    ap.add_argument("--wait-stage", type=float, default=600.0,
                    help="seconds to wait for a live stage (default 600)")
    ap.add_argument("--skip-finder", action="store_true",
                    help="skip state.py --find-velocity")
    args = ap.parse_args()

    log("session start; waiting up to %.0fs for the game" % args.wait_game)
    reader = wait_for_game(args.wait_game)
    if reader is None:
        log("no th06c after %.0fs -- giving up" % args.wait_game)
        return 1

    log("game is running (pid %d, base %#x)" % (reader.pid, reader.base))
    log("waiting up to %.0fs for a live stage (get into one now)"
        % args.wait_stage)
    if not wait_for_stage(reader, args.wait_stage):
        log("no live stage after %.0fs -- giving up" % args.wait_stage)
        return 1

    if not args.skip_finder:
        # Read-only: it differences bullet positions and scans the struct for a
        # matching vector. It has never been run with bullets in flight.
        run([PY, "state.py", "--find-velocity"], "velocity-offset finder")

    frames = int(round(args.seconds * 60))
    run([PY, "controller.py", "--dodger",
         "--log", "dodge60.jsonl",
         "--stop-after", str(frames),
         "--wait-live", "120"], "dodger at 60 Hz")

    run([PY, "controller.py", "--dodger", "--dodger-hz", "4",
         "--log", "dodge4.jsonl",
         "--stop-after", str(int(round(args.seconds * 4))),
         "--wait-live", "120"], "dodger at 4 Hz")

    log("session done. score with: python score_run.py dodge60.jsonl dodge4.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
