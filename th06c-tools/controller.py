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

Stop it with Ctrl+C, or by creating the stop file (default controller.stop).
"""
import ctypes
import json
import os
import sys
import threading
import time

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


class Controller:
    def __init__(self, fmt="crop", speed=4.0, dry_run=False, log_path=None,
                 stop_file="controller.stop", stop_after=None, vel_off=None,
                 poll_hz=60.0, use_oracle=True, model=DE.DEFAULT_MODEL):
        self.fmt = fmt
        self.speed = speed
        self.dry_run = dry_run
        self.stop_file = stop_file
        self.stop_after = stop_after
        self.poll_dt = 1.0 / poll_hz
        self.use_oracle = use_oracle
        self.model = model

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
    def decide_once(self):
        with self._lock:
            snap = self.latest
            age = time.perf_counter() - self.latest_t
        if snap is None or age > MAX_SNAPSHOT_AGE:
            return None

        text = view.render_as(snap, self.fmt)
        t0 = time.perf_counter()
        ans = DE.read_text(text, DE.MOVE_QUESTION, DE.MOVE_OPTIONS,
                           model=self.model)
        wall = (time.perf_counter() - t0) * 1000

        row = {
            "t": time.time(),
            "n": self.decisions,
            "snapshot_age_ms": round(age * 1000, 1),
            "round_trip_ms": round(wall, 1),
            "choice": ans["choice"],
            "letter": ans["letter"],
            "confidence": round(ans["probabilities"][ans["letter"]], 4),
            "probabilities": {k: round(v, 4) for k, v in
                              ans["probabilities"].items()},
            "player": [round(snap["player"]["pos"][0], 1),
                       round(snap["player"]["pos"][1], 1)],
            "bullets": len(snap["bullets"]),
            "live_bullets": sum(1 for b in snap["bullets"] if b.get("live")),
            "enemies_fatal": sum(1 for e in snap["enemies"] if e["fatal"]),
        }
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
        print("pid=%d base=%#x  window=%s  format=%s  dry_run=%s"
              % (pid, self.reader.base, self.hwnd, self.fmt, self.dry_run))
        if self.dry_run:
            print("dry run: reading state and asking the DE, pressing no keys")
        print("stop with Ctrl+C or by creating %s\n" % self.stop_file)

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
        fmt=opt("--format", "crop"),
        speed=opt("--speed", 4.0, float),
        dry_run="--dry-run" in argv,
        log_path=opt("--log", None),
        stop_file=opt("--stop-file", "controller.stop"),
        stop_after=opt("--stop-after", None, int),
        vel_off=opt("--vel-off", None, lambda s: int(s, 0)),
        model=opt("--model", DE.DEFAULT_MODEL),
        use_oracle="--no-oracle" not in argv,
    )
    ctl.run()


if __name__ == "__main__":
    main()
