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
  controller.py --wait-live 120           wait up to 2 min for the game to run
  controller.py --dodger                  per-frame arithmetic, no model calls
  controller.py --dodger --dodger-hz 4    the same policy at the model's rate

The `--dodger` mode replaces the model with `dodger.py`, which re-picks a
direction from the oracle every frame. It costs no API calls and exists to
separate the decision rate from the policy: if the arithmetic dodger survives
where the model does not, the rate was the problem; if it also dies, the
representation or the oracle is. `--dodger-hz` throttles it, so the identical
policy can be run at the model's ~4 decisions/s as a control.

The `--binary` mode asks a separate question for each direction -- "if the
player moves here, will a bullet hit them" -- and takes the direction least
likely to be hit. It is slower in requests (one per direction) but they are
asked concurrently, so a decision still costs about one round trip. It exists
because the single eight-way question is answered by whatever is most
distinctive in the evidence, which is always the threat, and that scores at
chance on a dense board. It defaults to the `rays` encoding, which is the one
that carries the distances the comparison needs.

th06c pauses whenever it loses focus and stays paused until it is resumed by
hand, so the controller waits for the board to start moving rather than
refusing. Alt-tabbing to start a run is the normal case, not an error.

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
import dodger as D        # noqa: E402
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
#   4: unknown bullet velocities are no longer reported as (0, 0). The oracle
#      now assumes a new bullet is aimed at the player, so a freshly spawned
#      threat is no longer scored harmless, and the differenced velocity no
#      longer carries the one-frame timestamp skew or the reused-slot match.
#      Also adds `mode: dodger`, which carries `clearance` instead of `p_yes`.
LOG_VERSION = 4

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
                 options=None, binary=False, wait_live=60.0, use_dodger=False,
                 dodger_hz=60.0, dodger_horizon=view.HORIZON,
                 dodger_stickiness=2.0):
        self.fmt = fmt
        self.binary = binary
        self.wait_live = wait_live
        self.speed = speed
        self.dry_run = dry_run
        self.stop_file = stop_file
        self.stop_after = stop_after
        self.poll_dt = 1.0 / poll_hz
        self.use_oracle = use_oracle
        self.model = model
        self.options = list(options) if options else list(DE.MOVE_OPTIONS)

        self.dodger = (D.Dodger(horizon=dodger_horizon,
                                stickiness=dodger_stickiness)
                       if use_dodger else None)
        # The dodger is evaluated on the sampler's clock, so throttle it to the
        # requested rate. --dodger-hz 4 runs the identical policy at the model's
        # decision rate, which is the control for "is it the rate or the
        # policy".
        self.dodger_every = max(1, int(round(poll_hz / max(1.0, dodger_hz))))

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

            if (self.dodger is not None and alive and self.focused()
                    and tick % self.dodger_every == 0):
                t0 = time.perf_counter()
                choice = self.dodger.choose(snap)
                self.desired = DIR_KEYS[choice]
                self._log_dodger(snap, choice,
                                 (time.perf_counter() - t0) * 1000.0)
                if self.stop_after and self.decisions >= self.stop_after:
                    self.running = False

            if not self.focused() or not alive:
                # th06c pauses on focus loss, and input does nothing while the
                # player is respawning -- but a direction held through a respawn
                # starts the player moving the instant they reappear, which is
                # how a run dies immediately after every death. Release instead.
                self._set_keys(())
            else:
                self._set_keys(self.desired)

            next_t += self.poll_dt
            slack = next_t - time.perf_counter()
            if slack > 0:
                time.sleep(slack)
            else:
                next_t = time.perf_counter()

    # --------------------------------------------------------------- deciding
    def _log_dodger(self, snap, choice, compute_ms):
        """One row per frame the dodger ran, in the shape score_run expects.

        `oracle_ok` here is not evidence about the dodger -- it picks by that
        same oracle, so on any frame with a safe move it is safe by
        construction. What the row is for is the `clearance` map at the frame a
        death happens, which is the only place the oracle's model error shows.
        """
        self.decisions += 1
        if not self.log:
            return
        cl = self.dodger.clearance
        safe = sorted(m for m, c in cl.items() if c > 0)
        row = {
            "t": time.time(),
            "n": self.decisions,
            "log_version": LOG_VERSION,
            "mode": "dodger",
            "snapshot_age_ms": 0.0,
            "round_trip_ms": round(compute_ms, 2),
            "choice": choice,
            "letter": "",
            "confidence": 1.0,
            "probabilities": {},
            "player": [round(snap["player"]["pos"][0], 1),
                       round(snap["player"]["pos"][1], 1)],
            "player_state": snap["player"]["state"],
            "bullets": len(snap["bullets"]),
            "live_bullets": sum(1 for b in snap["bullets"] if b.get("live")),
            "enemies_fatal": sum(1 for e in snap["enemies"] if e["fatal"]),
            "options": list(D.PREFERENCE),
            "oracle_safe": safe,
            "oracle_ok": choice in safe,
            "clearance": {m: round(c, 2) for m, c in cl.items()},
        }
        self.log.write(json.dumps(row) + "\n")
        self.log.flush()

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
    def _wait_for_live(self, deadline):
        """Wait until the board moves, or the deadline passes.

        A paused game answers every read identically, so this is the same test
        the old hard refusal used -- run in a loop so that resuming the game a
        second or two after starting the controller still works.
        """
        a = board_fingerprint(self.reader.snapshot(speed=self.speed))
        last_note = 0.0
        while time.perf_counter() < deadline:
            time.sleep(0.4)
            b = board_fingerprint(self.reader.snapshot(speed=self.speed))
            if a != b:
                return True
            a = b
            now = time.perf_counter()
            if now - last_note > 10.0:
                last_note = now
                print("  ... still frozen, %.0fs left"
                      % max(0.0, deadline - now))
        return False

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
                 "dodger" if self.dodger is not None
                 else "binary" if self.binary else "eight-way", self.dry_run))
        if self.dry_run:
            print("dry run: reading state and %s, pressing no keys"
                  % ("running the dodger" if self.dodger is not None
                     else "asking the DE"))
        print("stop with Ctrl+C or by creating %s\n" % self.stop_file)

        # Fail fast when the game is not actually running -- but wait for it
        # rather than refusing on the spot, because th06c pauses whenever it
        # loses focus and stays paused until it is resumed by hand. The usual
        # sequence is that the player alt-tabs away to start the run and the
        # game pauses on the way, so a hard refusal at that instant would make
        # the tool refuse every time it is most needed. Wait for the board to
        # move instead, and only give up if it never does.
        print("waiting up to %ds for the board to move (resume the game if it "
              "is paused)..." % self.wait_live)
        if not self._wait_for_live(time.perf_counter() + self.wait_live):
            print("refusing to start: the board was identical across every read "
                  "for %ds, so the game is paused or its window is not "
                  "focused. Nothing pressed would reach it." % self.wait_live)
            print("resume the game (th06c stays paused once it loses focus), "
                  "then run again.")
            raise SystemExit(1)
        print("board is moving; starting.")

        worker = None
        if self.dodger is not None:
            print("dodger mode: arithmetic policy every %d frame(s), no model "
                  "calls" % self.dodger_every)
        else:
            worker = threading.Thread(target=self.decide_loop, daemon=True)
            worker.start()
        try:
            self.sample_loop()
        except KeyboardInterrupt:
            print("\ninterrupted")
        finally:
            self.running = False
            if worker is not None:
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
        wait_live=opt("--wait-live", 60.0, float),
        use_dodger="--dodger" in argv,
        dodger_hz=opt("--dodger-hz", 60.0, float),
        dodger_horizon=opt("--dodger-horizon", view.HORIZON, int),
        dodger_stickiness=opt("--dodger-stickiness", 2.0, float),
    )
    ctl.run()


if __name__ == "__main__":
    main()
