#!/usr/bin/env python3
"""Drive th06c with the DE: read state, render it, ask, hold the arrow key.

Two threads, because the two jobs have different clocks.

  sampler   the main thread. Reads game state at 60 Hz so velocities stay
            meaningful, and applies whatever direction is currently decided.
            This must never block, or the player stops moving.

  decider   one worker. Takes the freshest snapshot, renders it, asks the DE,
            and sets the direction. One round trip is 130-300 ms, so this runs
            at roughly 3-7 decisions per second while the game runs at 60.

The consequence is worth stating plainly: a decision is made from state that is
already ~1 round trip old by the time it is applied, and then held until the
next one lands. At 200 ms that is 12 game frames of stale state and 12 frames of
commitment. This is not a per-frame policy and cannot become one.

Every decision is appended to a JSONL log, including the oracle's view of which
moves would have survived. That is what makes the run scoreable afterwards
without having to reproduce it.

  controller.py --dry-run                 read and decide, press nothing
  controller.py                           live
  controller.py --format crop --log run.jsonl
  controller.py --stop-after 60           stop after 60 decisions
  controller.py --no-stay-put             drop `stay put`, so it must move
  controller.py --binary                  one yes/no question per direction

The `--binary` mode asks a separate question for each direction -- "if the
player moves here, will a bullet hit them" -- and takes the direction least
likely to be hit. It is slower in requests (one per direction) but they are
asked concurrently, so a decision still costs about one round trip. It exists
because the single eight-way question is answered by whatever is most
distinctive in the evidence, which is always the threat, and that scores at
chance on a dense board. It defaults to the `rays` encoding, which is the one
that carries the distances the comparison needs.

Stop it with Ctrl+C, or by creating the stop file (default controller.stop).
"""
import ctypes
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import de_client as DE    # noqa: E402
import oracle             # noqa: E402
import state as S         # noqa: E402
import th06               # noqa: E402
import view               # noqa: E402

user32 = ctypes.windll.user32

DIR_KEYS = {
    "left": ("left",), "right": ("right",), "up": ("up",), "down": ("down",),
    "up-left": ("up", "left"), "up-right": ("up", "right"),
    "down-left": ("down", "left"), "down-right": ("down", "right"),
    "stay put": (),
}

# A decision this stale is worse than holding the last one, so the decider
# refuses to act on it.
MAX_SNAPSHOT_AGE = 0.5

# Bumped whenever the evidence or the oracle's inputs change, because a log from
# a different version is not comparable with a new one.
#   1: implicit. The oracle collided against every allocated bullet, including
#      the spawning and despawning ones that cannot kill -- see view.live_bullets
#      for the disassembly. On one 500-decision run that affected 55% of the
#      discriminating frames, so its numbers cannot be compared to a fixed run.
#   2: the oracle and the view see live bullets only.
#   3: the binary mode, which asks one yes/no question per direction instead of
#      one eight-way question. Rows carry `mode` and `p_yes`.
LOG_VERSION = 3

# The binary mode. One yes/no question per direction, and the move is whichever
# direction the model says is least likely to be hit. This exists because the
# single eight-way question is answered by the most distinctive thing in the
# evidence, which is always the threat: on 100 dense boards it scores 48%, which
# is exactly chance and below a constant policy, while the same model asked one
# direction at a time scores 85% against a best constant of 53%. See WORKLOG.md.
#
# `rays` is the encoding that works here, and only here: a "would I hit it"
# question needs the exact distances to compare against the 60u the player
# covers in the horizon, and `crop` quantises those into three glyph buckets.
BINARY_OPTIONS = [("A", "yes"), ("B", "no")]
YES_LETTER = next(l for l, d in BINARY_OPTIONS if d == "yes")
BINARY_QUESTION = ("If the player moves %s, will a bullet hit them within the "
                   "next 15 frames?")
STAY_QUESTION = ("If the player stays where they are, will a bullet hit them "
                 "within the next 15 frames?")

# Consecutive decisions over an unchanged board before the run gives up. At the
# observed 5-7 decisions/s this is roughly 7-9 seconds, which is longer than any
# legitimate static window (a death, a dialogue) and far shorter than the ~100s a
# full run would waste.
FROZEN_LIMIT = 45


def board_fingerprint(snap):
    """A cheap signature of the parts of the board that must change on their own.

    A paused game returns identical memory on every read, and th06c pauses
    itself whenever its window loses focus. Against a frozen board the model is
    asked the same question over and over, every answer is meaningless, and a
    full run is spent learning nothing. Bullet positions are the reliable tell:
    in a live stage they change on essentially every frame.
    """
    bullets = snap.get("bullets", [])
    return (
        round(snap["player"]["pos"][0], 1),
        round(snap["player"]["pos"][1], 1),
        len(bullets),
        round(sum(b["pos"][0] + b["pos"][1] for b in bullets), 1),
        len(snap.get("enemies", [])),
    )


class Controller:
    def __init__(self, fmt="crop", speed=4.0, dry_run=False, log_path=None,
                 stop_file="controller.stop", stop_after=None, vel_off=None,
                 poll_hz=60.0, use_oracle=True, model=DE.DEFAULT_MODEL,
                 options=None, binary=False):
        self.fmt = fmt
        self.binary = binary
        self.speed = speed
        self.dry_run = dry_run
        self.stop_file = stop_file
        self.stop_after = stop_after
        self.poll_dt = 1.0 / poll_hz
        self.use_oracle = use_oracle
        self.model = model
        self.options = list(options) if options else list(DE.MOVE_OPTIONS)

        self.reader = S.Reader(vel_off=vel_off)
        self.hwnd = None
        self.hud = None
        self.latest = None
        self.latest_t = 0.0
        self.desired = ()
        self.pressed = set()
        self.running = True
        self.decisions = 0
        self.errors = 0
        self.deaths = 0
        self._was_alive = True
        self._last_fp = None
        self._frozen = 0
        self._lock = threading.Lock()

        self.log = open(log_path, "a", encoding="utf-8") if log_path else None

    # ------------------------------------------------------------------ input
    def _set_keys(self, keys):
        keys = set(keys)
        if keys == self.pressed:
            return
        if not self.dry_run:
            for k in self.pressed - keys:
                th06._send(th06.VK[k], True)
            for k in keys - self.pressed:
                th06._send(th06.VK[k], False)
        self.pressed = keys

    def release_all(self):
        self._set_keys(())

    # -------------------------------------------------------------- foreground
    def focused(self):
        if self.hwnd is None:
            return True
        return user32.GetForegroundWindow() == self.hwnd

    # --------------------------------------------------------------- sampling
    def sample_loop(self):
        next_t = time.perf_counter()
        tick = 0
        while self.running:
            tick += 1
            if tick % 15 == 0 and self.stop_file \
                    and os.path.exists(self.stop_file):
                print("stop file %s appeared" % self.stop_file)
                self.running = False
                break
            try:
                # The view the model was validated on carries lives, bombs and
                # power, so keep them populated rather than sending "?". They
                # change only on death or pickup, so once a second is plenty.
                if self.hud is None or tick % 60 == 0:
                    self.hud = self.reader.read_hud()
                snap = self.reader.snapshot(speed=self.speed, hud=self.hud)
            except Exception as e:                       # noqa: BLE001
                print("state read failed: %s: %s" % (type(e).__name__, e))
                self.running = False
                break
            with self._lock:
                self.latest = snap
                self.latest_t = time.perf_counter()

            alive = snap["player"]["state"] == 0
            if self._was_alive and not alive:
                self.deaths += 1
                print("  player state %d (not alive) -- death %d"
                      % (snap["player"]["state"], self.deaths))
            self._was_alive = alive

            if not self.focused():
                self._set_keys(())            # th06c pauses on focus loss
            else:
                self._set_keys(self.desired)

            next_t += self.poll_dt
            slack = next_t - time.perf_counter()
            if slack > 0:
                time.sleep(slack)
            else:
                next_t = time.perf_counter()

    # --------------------------------------------------------------- deciding
    def decide_binary(self, text):
        """One yes/no question per direction, all in flight at once.

        Eight sequential round trips at 130-300 ms each would hold the player on
        one direction for well over a second. The questions are independent of
        each other, so they go out together and a decision costs about one round
        trip rather than eight. Returns the safest direction and its P(hit).
        """
        items = [(d, BINARY_QUESTION % d) for _, d in self.options
                 if d != "stay put"]
        if any(d == "stay put" for _, d in self.options):
            items.append(("stay put", STAY_QUESTION))
        p_yes = {}
        with ThreadPoolExecutor(max_workers=len(items)) as pool:
            futs = {pool.submit(DE.read_text, text, q, BINARY_OPTIONS,
                                model=self.model): d for d, q in items}
            for fut in as_completed(futs):
                ans = fut.result()
                p_yes[futs[fut]] = ans["probabilities"].get(YES_LETTER, 0.0)
        # Least likely to be hit. Ties keep the menu's own order.
        order = [d for _, d in self.options]
        choice = min(items, key=lambda it: (p_yes[it[0]],
                                            order.index(it[0])))[0]
        return choice, p_yes

    def decide_once(self):
        with self._lock:
            snap = self.latest
            age = time.perf_counter() - self.latest_t
        if snap is None or age > MAX_SNAPSHOT_AGE:
            return None

        # Stop rather than spend an API call on a board that is not moving.
        fp = board_fingerprint(snap)
        if fp == self._last_fp:
            self._frozen += 1
        else:
            self._frozen = 0
        self._last_fp = fp
        if self._frozen >= FROZEN_LIMIT:
            print("\n  board unchanged across %d decisions -- the game is "
                  "paused or its window lost focus, so nothing pressed is "
                  "reaching it. Stopping." % self._frozen)
            self.running = False
            return None

        text = view.render_as(snap, self.fmt)
        t0 = time.perf_counter()
        if self.binary:
            choice, p_yes = self.decide_binary(text)
            ans = {"choice": choice, "letter": "",
                   "probabilities": {}}
            conf = 1.0 - p_yes[choice]
        else:
            p_yes = None
            ans = DE.read_text(text, DE.MOVE_QUESTION, self.options,
                               model=self.model)
            conf = ans["probabilities"][ans["letter"]]
        wall = (time.perf_counter() - t0) * 1000

        row = {
            "t": time.time(),
            "n": self.decisions,
            "log_version": LOG_VERSION,
            "mode": "binary" if self.binary else "eight-way",
            "snapshot_age_ms": round(age * 1000, 1),
            "round_trip_ms": round(wall, 1),
            "choice": ans["choice"],
            "letter": ans["letter"],
            "confidence": round(conf, 4),
            "probabilities": {k: round(v, 4) for k, v in
                              ans["probabilities"].items()},
            "player": [round(snap["player"]["pos"][0], 1),
                       round(snap["player"]["pos"][1], 1)],
            "player_state": snap["player"]["state"],
            "bullets": len(snap["bullets"]),
            "live_bullets": sum(1 for b in snap["bullets"] if b.get("live")),
            "enemies_fatal": sum(1 for e in snap["enemies"] if e["fatal"]),
            # Record the menu that was offered, so a run stays scoreable
            # against the right baselines after the menu changes.
            "options": [d for _, d in self.options],
        }
        if p_yes is not None:
            # Every direction's P(hit), so a run can be rescored against a
            # different rank rule without being replayed.
            row["p_yes"] = {k: round(v, 4) for k, v in p_yes.items()}
        if self.use_oracle:
            try:
                safe = sorted(oracle.safe_moves(snap))
            except Exception:                            # noqa: BLE001
                safe = None
            row["oracle_safe"] = safe
            row["oracle_ok"] = (ans["choice"] in safe) if safe is not None else None

        self.desired = DIR_KEYS.get(ans["choice"], ())
        self.decisions += 1
        if self.log:
            self.log.write(json.dumps(row) + "\n")
            self.log.flush()
        return row

    def decide_loop(self):
        while self.running:
            try:
                row = self.decide_once()
            except Exception as e:                       # noqa: BLE001
                self.errors += 1
                print("  decision failed (%d): %s: %s"
                      % (self.errors, type(e).__name__, e))
                time.sleep(0.5)
                continue
            if row is None:
                time.sleep(0.05)
                continue
            ok = row.get("oracle_ok")
            mark = "" if ok is None else ("  ok" if ok else "  XX")
            print("  #%-4d %-11s %.2f  %4.0f ms  bullets=%-3d%s"
                  % (row["n"], row["choice"], row["confidence"],
                     row["round_trip_ms"], row["live_bullets"], mark))
            if self.stop_after and self.decisions >= self.stop_after:
                self.running = False

    # -------------------------------------------------------------------- run
    def run(self):
        pid, h = self.reader.pid, self.reader.h
        wins = th06.game_windows()
        if wins:
            # The game window, not a splash or dialog: largest by area.
            self.hwnd = max(wins, key=lambda w: w[3])[0]
            th06.focus(self.hwnd)
        else:
            print("warning: no th06c window found; keys will not reach the game")
        print("pid=%d base=%#x  window=%s  format=%s  mode=%s  dry_run=%s"
              % (pid, self.reader.base, self.hwnd, self.fmt,
                 "binary" if self.binary else "eight-way", self.dry_run))
        if self.dry_run:
            print("dry run: reading state and asking the DE, pressing no keys")
        print("stop with Ctrl+C or by creating %s\n" % self.stop_file)

        # Fail fast when the game is not actually running. A paused window --
        # and th06c pauses itself on focus loss -- answers every read
        # identically, so a full run against one is 500 decisions spent asking
        # the same frozen question. Two reads 0.4s apart is enough to tell.
        try:
            a = board_fingerprint(self.reader.snapshot(speed=self.speed))
            time.sleep(0.4)
            b = board_fingerprint(self.reader.snapshot(speed=self.speed))
        except Exception as e:                           # noqa: BLE001
            print("refusing to start: could not read the board: %s: %s"
                  % (type(e).__name__, e))
            raise SystemExit(1)
        if a == b:
            print("refusing to start: the board is identical across two reads "
                  "0.4s apart, so the game is paused or its window is not "
                  "focused. Nothing pressed would reach it.")
            print("unpause the game, then run again.")
            raise SystemExit(1)

        worker = threading.Thread(target=self.decide_loop, daemon=True)
        worker.start()
        try:
            self.sample_loop()
        except KeyboardInterrupt:
            print("\ninterrupted")
        finally:
            self.running = False
            worker.join(timeout=2.0)
            self.release_all()
            if self.log:
                self.log.close()

        print("\n%d decisions, %d errors, %d deaths"
              % (self.decisions, self.errors, self.deaths))
        if self.reader.stats["reads"]:
            print("state read: last %.2f ms over %d frames"
                  % (self.reader.stats["last_ms"], self.reader.stats["reads"]))


def main():
    argv = sys.argv[1:]

    def opt(flag, default=None, cast=str):
        if flag in argv:
            return cast(argv[argv.index(flag) + 1])
        return default

    ctl = Controller(
        fmt=opt("--format", "rays" if "--binary" in argv else "crop"),
        speed=opt("--speed", 4.0, float),
        dry_run="--dry-run" in argv,
        log_path=opt("--log", None),
        stop_file=opt("--stop-file", "controller.stop"),
        stop_after=opt("--stop-after", None, int),
        vel_off=opt("--vel-off", None, lambda s: int(s, 0)),
        model=opt("--model", DE.DEFAULT_MODEL),
        use_oracle="--no-oracle" not in argv,
        options=DE.move_options(stay_put="--no-stay-put" not in argv),
        binary="--binary" in argv,
    )
    ctl.run()


if __name__ == "__main__":
    main()
