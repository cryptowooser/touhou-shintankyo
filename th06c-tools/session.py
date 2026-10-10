#!/usr/bin/env python3
"""One unattended dodger session against the live game.

Waits for th06c, waits for a stage with bullets in flight, then runs the dodger
at 60 Hz for a fixed wall-clock window and stops it cleanly.

The velocity offset is already known -- 0x008, confirmed against the game -- so
the finder does not run unless --find-velocity asks for it. It is read-only, but
it costs a minute of the window and it re-derives something already settled.

The window is wall-clock rather than a decision count on purpose. --stop-after
counts decisions, and the dodger only makes one while the player is alive, so
on a run with deaths a decision budget takes an unbounded amount of real time
to expire. The stop file is used instead.

Lives and bombs are not managed here. Run the keepalive separately or the run
ends after three deaths.

  session.py --seconds 180          three minutes of dodging
  session.py --seconds 60 --dry-run  read and decide, press nothing
  session.py --find-velocity        re-run the read-only offset finder first

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

# Confirmed against the live game by differencing bullet positions and matching
# the vector back into the struct. state.py --find-velocity re-derives it.
VEL_OFF = "0x008"


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


def wait_for_stage(reader, timeout, need=5):
    """Wait until the board is moving and has live bullets.

    A menu is a static board with no bullets; a stage is a moving board with
    bullets. Requiring both, plus a few live bullets, avoids starting on a menu
    -- where the dodger would press directions into the UI -- or on the title
    screen's demo reel.

    `need` is deliberately low. Lunatic throws far more, but a stage's first
    second can be sparse, and the cost of starting slightly early is one wasted
    second, while the cost of waiting is a shorter run.
    """
    t0 = time.time()
    last = None
    moving = 0
    best = 0
    while time.time() - t0 < timeout:
        try:
            snap = reader.snapshot()
        except Exception as e:                       # noqa: BLE001
            log("read failed while waiting for a stage: %s" % e)
            time.sleep(1.0)
            continue
        live = sum(1 for b in snap["bullets"] if b["live"])
        best = max(best, live)
        sig = (len(snap["bullets"]),
               round(sum(b["pos"][0] + b["pos"][1]
                         for b in snap["bullets"]), 1))
        moving = moving + 1 if sig != last else 0
        last = sig
        if live >= need and moving >= 3:
            log("stage is live: %d live bullets, %d lasers, player state %d, "
                "lives %s"
                % (live, len(snap.get("lasers", [])),
                   snap["player"]["state"], snap.get("lives")))
            return True
        time.sleep(0.5)
    log("no live stage after %.0fs (most live bullets seen: %d)"
        % (timeout, best))
    return False


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=180.0,
                    help="wall-clock length of the dodger run (default 180)")
    ap.add_argument("--wait-game", type=float, default=1800.0,
                    help="seconds to wait for th06c to start (default 1800)")
    ap.add_argument("--wait-stage", type=float, default=900.0,
                    help="seconds to wait for a live stage (default 900)")
    ap.add_argument("--log", default=None,
                    help="log path (default lunatic-<HHMMSS>.jsonl)")
    ap.add_argument("--vel-off", default=VEL_OFF,
                    help="velocity offset for the reader (default %s)" % VEL_OFF)
    ap.add_argument("--find-velocity", action="store_true",
                    help="re-run the read-only offset finder first")
    ap.add_argument("--no-shoot", action="store_true",
                    help="do not hold Z (slower stage progress, fewer threats)")
    ap.add_argument("--dry-run", action="store_true",
                    help="decide and log, but press no keys")
    args = ap.parse_args()

    log("session start; waiting up to %.0fs for the game" % args.wait_game)
    reader = wait_for_game(args.wait_game)
    if reader is None:
        log("no th06c after %.0fs -- giving up" % args.wait_game)
        return 1

    log("game is running (pid %d, base %#x)" % (reader.pid, reader.base))
    log("waiting up to %.0fs for a live stage -- be in a stage now"
        % args.wait_stage)
    if not wait_for_stage(reader, args.wait_stage):
        log("giving up")
        return 1

    if args.find_velocity:
        # Read-only: it differences bullet positions and scans the struct for a
        # matching vector.
        log("--- velocity-offset finder ---")
        subprocess.call([PY, "state.py", "--find-velocity"], cwd=HERE)

    logfile = args.log or time.strftime("lunatic-%H%M%S.jsonl")
    stopfile = os.path.join(HERE, "session.stop")
    if os.path.exists(stopfile):
        os.remove(stopfile)

    cmd = [PY, "controller.py", "--dodger",
           "--vel-off", args.vel_off,
           "--dodger-horizon", "30",
           "--log", logfile,
           "--stop-file", stopfile,
           "--wait-live", "120"]
    if not args.no_shoot:
        cmd.append("--shoot")
    if args.dry_run:
        cmd.append("--dry-run")

    log("--- dodger, %.0fs wall clock -> %s ---" % (args.seconds, logfile))
    log(" ".join(os.path.basename(c) for c in cmd))
    t0 = time.time()
    proc = subprocess.Popen(cmd, cwd=HERE)

    # The controller watches for this file every 15 ticks and then releases the
    # keys in its finally block, so this is a clean stop rather than a kill.
    try:
        while proc.poll() is None and time.time() - t0 < args.seconds:
            time.sleep(1.0)
        if proc.poll() is None:
            log("window elapsed; asking the controller to stop")
            open(stopfile, "w").close()
            for _ in range(30):
                if proc.poll() is not None:
                    break
                time.sleep(0.5)
        if proc.poll() is None:
            log("controller did not stop on the stop file; terminating")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    except KeyboardInterrupt:
        log("interrupted; stopping the controller")
        open(stopfile, "w").close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        if os.path.exists(stopfile):
            os.remove(stopfile)

    log("--- dodger finished rc=%s after %.0fs ---"
        % (proc.returncode, time.time() - t0))

    path = logfile if os.path.isabs(logfile) else os.path.join(HERE, logfile)
    if os.path.exists(path) and os.path.getsize(path):
        log("--- score ---")
        subprocess.call([PY, "score_run.py", path], cwd=HERE)
    else:
        log("no rows were written to %s" % path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
