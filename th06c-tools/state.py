#!/usr/bin/env python3
"""Live game state for the controller: one bulk read per frame, with velocities.

entities.py reads the same structs one field at a time, which is the right shape
for a one-off dump and the wrong shape for a 60 Hz loop -- 640 bullet slots at
four reads each is 2,500 ReadProcessMemory calls per frame. Here the whole
bullet array is one read and the whole enemy array is another, and the parsing
happens in Python on the returned buffer.

Velocities are the awkward part. entities.py maps position and state but no
velocity field, and the view and oracle both need one: the view draws each
bullet's future path and the oracle extrapolates it. Until a velocity offset is
found, velocity is derived by differencing the same slot's position across
frames. Slot index is stable for the life of a bullet, so that is a reliable
match. The cost is that a bullet which has just appeared has no history and gets
no velocity for a few frames.

  state.py --dump              one frame, text
  state.py --watch 120         timing and bullet count over 120 frames
  state.py --find-velocity     scan the bullet struct for its velocity vector
"""
import math
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memedit as M       # noqa: E402
import modbase as MB      # noqa: E402
import th06               # noqa: E402

# ---------------------------------------------------------------- addresses
BM_RVA = 0x955410
BULLETS_OFF = 0x008
BULLET_STRIDE = 0x5D0
BULLET_SLOTS = 640
B_OFF_POS = 0x030
B_OFF_STATE = 0x044
B_OFF_GRAZE_SIZE = 0x5A4
# bulletCount sits immediately after the last slot, so one read covers both.
BULLET_BYTES = BULLETS_OFF + BULLET_SLOTS * BULLET_STRIDE + 4

EM_RVA = 0xA7E1E0
ENEMIES_OFF = 0x008
ENEMY_STRIDE = 0xFD0
ENEMY_SLOTS = 257
E_OFF_POS = 0x0A8
E_OFF_FLAGS1 = 0x0B4
E_OFF_FLAGS2 = 0x0B5
E_OFF_FLAGS3 = 0x0B6
E_OFF_HITBOX = 0x2A4
ENEMY_BYTES = ENEMIES_OFF + ENEMY_SLOTS * ENEMY_STRIDE + 4

PLAYER_RVA = 0xBB89F0
P_OFF_HITBOX_TL = 0x410
P_OFF_HITBOX_BR = 0x41C
P_OFF_STATE = 0x73B8

LIVES_RVA, BOMBS_RVA, POWER_RVA = 0x3EE804, 0x3EE805, 0x3ED034

FIELD_W, FIELD_H = 384.0, 448.0
FPS = 60.0

# Bullet states. Only these are actually in flight; 2..4 are still spawning and
# 5 is despawning, so extrapolating any of them would be extrapolating a bullet
# that is not yet (or no longer) moving on its flight path.
STATE_NAMES = {0: "inactive", 1: "fired", 2: "spawn-fast", 3: "spawn-normal",
               4: "spawn-slow", 5: "despawning"}
LIVE_STATES = (1,)

# The kill hitbox is not the graze size; entities.py notes the enemy touch test
# divides its raw hitbox by 1.5. Bullet graze size runs 8..64 units across the
# shot types, so half of it is a large overestimate of what kills. This is the
# radius used for the trail and for oracle scoring, and it is deliberately a
# guess until it is measured.
BULLET_KILL_RADIUS = 4.0
PLAYER_KILL_RADIUS = 2.0


class Reader:
    """Bulk state reads from a running th06c."""

    def __init__(self, vel_off=None, min_age=0.05):
        self.pid, self.h = MB.open_game()
        _name, self.base, _size = MB.find_module(self.pid, "th06c")
        if not self.base:
            raise SystemExit("th06c module not found in pid %d" % self.pid)
        self.vel_off = vel_off
        self.min_age = min_age
        self._old = {}
        self._old_t = 0.0
        self._cur = {}
        self.stats = {"reads": 0, "last_ms": 0.0}

    # ------------------------------------------------------------ bulk reads
    def _bullets_raw(self):
        buf = M.read_mem(self.h, self.base + BM_RVA + BULLETS_OFF,
                         BULLET_SLOTS * BULLET_STRIDE + 4)
        if buf is None:
            return None
        return buf

    def _enemies_raw(self):
        buf = M.read_mem(self.h, self.base + EM_RVA + ENEMIES_OFF,
                         ENEMY_SLOTS * ENEMY_STRIDE + 4)
        return buf

    def read_bullets(self, now=None):
        buf = self._bullets_raw()
        if buf is None:
            return []
        out = []
        pos_fmt = struct.Struct("<3f")
        for i in range(BULLET_SLOTS):
            a = i * BULLET_STRIDE
            st, = struct.unpack_from("<H", buf, a + B_OFF_STATE)
            if st == 0:
                continue
            x, y, z = pos_fmt.unpack_from(buf, a + B_OFF_POS)
            sx, sy, _sz = pos_fmt.unpack_from(buf, a + B_OFF_GRAZE_SIZE)
            b = {"idx": i, "state": st, "pos": (x, y, z), "size": (sx, sy),
                 "live": st in LIVE_STATES}
            if self.vel_off is not None:
                vx, vy, vz = pos_fmt.unpack_from(buf, a + self.vel_off)
                b["vel"] = (vx, vy, vz)
            out.append(b)
        return out

    def read_enemies(self):
        buf = self._enemies_raw()
        if buf is None:
            return []
        out = []
        pos_fmt = struct.Struct("<3f")
        for i in range(ENEMY_SLOTS):
            a = i * ENEMY_STRIDE
            f1, f2, f3 = struct.unpack_from("<3B", buf, a + E_OFF_FLAGS1)
            if not (f1 & 0x80):
                continue
            x, y, z = pos_fmt.unpack_from(buf, a + E_OFF_POS)
            hx, hy, _hz = pos_fmt.unpack_from(buf, a + E_OFF_HITBOX)
            interactable = (f2 >> 0) & 1
            collidable = (f2 >> 1) & 1
            in_bounds = (f2 >> 2) & 1
            boss = (f2 >> 3) & 1
            invisible = (f3 >> 3) & 1
            out.append({
                "idx": i, "pos": (x, y, z), "hitbox": (hx, hy),
                "fatal": bool(interactable and collidable and in_bounds
                              and not boss and not invisible)})
        return out

    def read_player(self):
        p = self.base + PLAYER_RVA
        raw = M.read_mem(self.h, p + P_OFF_STATE, 1)
        state = raw[0] if raw else 0xFF
        raw = M.read_mem(self.h, p + P_OFF_HITBOX_TL, 24)
        if raw is None:
            return None
        tl = struct.unpack_from("<3f", raw, 0)
        br = struct.unpack_from("<3f", raw, 12)
        return {"state": state,
                "pos": ((tl[0] + br[0]) / 2.0, (tl[1] + br[1]) / 2.0,
                        (tl[2] + br[2]) / 2.0),
                "hitbox": (br[0] - tl[0], br[1] - tl[1])}

    def read_hud(self):
        out = {}
        for name, rva, size in (("lives", LIVES_RVA, 1), ("bombs", BOMBS_RVA, 1),
                                ("power", POWER_RVA, 2)):
            raw = M.read_mem(self.h, self.base + rva, size)
            out[name] = int.from_bytes(raw, "little") if raw else None
        return out

    # ------------------------------------------------------------ velocities
    def _velocity(self, idx, x, y, now):
        old = self._old.get(idx)
        if old is None:
            return None
        dt = now - self._old_t
        if dt <= 0:
            return None
        frames = dt * FPS
        if frames < 1.0:
            return None
        return ((x - old[0]) / frames, (y - old[1]) / frames)

    def _rotate(self, now, cur):
        # `_old` is refreshed only once it is at least min_age stale, so the
        # baseline spans several frames and one frame of jitter does not turn
        # into a velocity error.
        if now - self._old_t >= self.min_age:
            self._old = self._cur
            self._old_t = now
        self._cur = cur

    # --------------------------------------------------------------- snapshot
    def snapshot(self, speed=4.0, hud=None):
        """One frame in the shape view.py renders and oracle.py scores."""
        t0 = time.perf_counter()
        now = time.perf_counter()
        player = self.read_player()
        bullets = self.read_bullets(now)
        enemies = self.read_enemies()

        cur = {}
        out_bullets = []
        for b in bullets:
            x, y, _z = b["pos"]
            cur[b["idx"]] = (x, y)
            if "vel" in b:
                vx, vy = b["vel"][0], b["vel"][1]
            else:
                v = self._velocity(b["idx"], x, y, now)
                vx, vy = v if v else (0.0, 0.0)
            out_bullets.append({"idx": b["idx"], "pos": (x, y), "vel": (vx, vy),
                                "state": b["state"], "size": b["size"],
                                "live": b["live"]})
        self._rotate(now, cur)

        state = {
            "player": {"pos": (player["pos"][0], player["pos"][1]) if player
                       else (FIELD_W / 2, FIELD_H / 2),
                       "state": player["state"] if player else 0xFF,
                       "speed": speed},
            "bullets": out_bullets,
            "enemies": [{"pos": (e["pos"][0], e["pos"][1]), "fatal": e["fatal"]}
                        for e in enemies],
            "lasers": [],
        }
        if hud:
            state.update({k: v for k, v in hud.items() if v is not None})
        self.stats["reads"] += 1
        self.stats["last_ms"] = (time.perf_counter() - t0) * 1000
        return state


# ---------------------------------------------------------------- diagnostics
def find_velocity(reader, tries=400, gap=0.05):
    """Find the bullet struct's velocity vector by matching true motion.

    Position is known, so a bullet's real velocity can be measured directly by
    differencing. Then the struct is scanned for three consecutive floats that
    equal it. Only bullets that are unambiguously alone and in flight are used,
    because the match has to be exact.
    """
    import view
    for _ in range(tries):
        before = {b["idx"]: b["pos"] for b in reader.read_bullets() if b["live"]}
        t0 = time.perf_counter()
        time.sleep(gap)
        after = {b["idx"]: b["pos"] for b in reader.read_bullets() if b["live"]}
        dt = time.perf_counter() - t0
        frames = dt * FPS
        hits = {}
        for idx, p1 in after.items():
            p0 = before.get(idx)
            if p0 is None:
                continue
            vx = (p1[0] - p0[0]) / frames
            vy = (p1[1] - p0[1]) / frames
            if abs(vx) < 0.3 and abs(vy) < 0.3:
                continue
            addr = reader.base + BM_RVA + BULLETS_OFF + idx * BULLET_STRIDE
            raw = M.read_mem(reader.h, addr, BULLET_STRIDE)
            if raw is None:
                continue
            for off in range(0, BULLET_STRIDE - 12, 4):
                if off in (B_OFF_POS,):
                    continue
                ax, ay, az = struct.unpack_from("<3f", raw, off)
                if (abs(ax - vx) < 0.05 and abs(ay - vy) < 0.05
                        and abs(az) < 0.05):
                    hits.setdefault(off, []).append(idx)
        if hits:
            best = sorted(hits.items(), key=lambda kv: -len(kv[1]))
            print("candidate velocity offsets in the bullet struct:")
            for off, idxs in best[:8]:
                print("   +0x%03X  matched on %d bullets  (slots %s)"
                      % (off, len(idxs), idxs[:6]))
            return best[0][0]
        time.sleep(0.2)
    print("no velocity offset found -- is the game running with bullets in flight?")
    return None


def calibrate_speed(reader, hold_s=0.4):
    """Measure the player's real speed by holding each direction.

    The view tells the model the player moves 4.0 units per frame, and that
    number was a placeholder nobody measured. It matters: the model is asked to
    decide whether it can clear an incoming line before it arrives, and that
    judgement is a ratio of the player's speed to the bullet's. If the view
    understates the speed the model plays too cautiously; if it overstates it,
    the model takes lines it cannot clear.

    This moves the player, so it can get killed. Run it somewhere harmless.
    """
    import th06
    wins = th06.game_windows()
    if not wins:
        print("no th06c window; the game pauses on focus loss and nothing would move")
        return None
    hwnd = max(wins, key=lambda w: w[3])[0]
    th06.focus(hwnd)
    time.sleep(0.3)
    print("holding each direction for %.1fs -- the player is moving and can die\n"
          % hold_s)
    results = {}
    for key in ("left", "right", "up", "down"):
        p0 = reader.read_player()
        if p0 is None:
            print("   %-6s could not read the player" % key)
            continue
        th06._send(th06.VK[key], False)
        t0 = time.perf_counter()
        deltas = []
        prev = p0
        while time.perf_counter() - t0 < hold_s:
            time.sleep(1.0 / FPS)
            cur = reader.read_player()
            if cur is None:
                break
            deltas.append((abs(cur["pos"][0] - prev["pos"][0]),
                           abs(cur["pos"][1] - prev["pos"][1])))
            prev = cur
        th06._send(th06.VK[key], True)
        elapsed = time.perf_counter() - t0
        p1 = prev
        dx = p1["pos"][0] - p0["pos"][0]
        dy = p1["pos"][1] - p0["pos"][1]
        dist = math.hypot(dx, dy)
        frames = elapsed * FPS
        med = 0.0
        if deltas:
            axis = [d[0] if key in ("left", "right") else d[1] for d in deltas]
            axis.sort()
            med = axis[len(axis) // 2]
        results[key] = dist / frames if frames else 0.0
        print("   %-6s moved %6.1f units in %5.2f s (%4.0f frames) -> %5.2f u/f"
              "   median step %5.2f"
              % (key, dist, elapsed, frames, results[key], med))
        time.sleep(0.1)
    vals = [v for v in results.values() if v > 0.1]
    if not vals:
        print("\nnothing moved -- is the game paused, or the window not focused?")
        return None
    vals.sort()
    speed = vals[len(vals) // 2]
    print("\nplayer speed ~= %.2f units/frame   (pass --speed %.2f)" % (speed, speed))
    return speed


def main():
    argv = sys.argv[1:]
    reader = Reader()
    if "--find-velocity" in argv:
        find_velocity(reader)
        return
    if "--calibrate-speed" in argv:
        calibrate_speed(reader)
        return

    watch = int(argv[argv.index("--watch") + 1]) if "--watch" in argv else 1
    hud = reader.read_hud()
    print("pid=%d base=%#x  hud=%s" % (reader.pid, reader.base, hud))
    times = []
    for f in range(watch):
        state = reader.snapshot()
        times.append(reader.stats["last_ms"])
        if watch == 1 or f % max(1, watch // 10) == 0 or f == watch - 1:
            live = [b for b in state["bullets"] if b["live"]]
            moving = [b for b in state["bullets"]
                      if abs(b["vel"][0]) + abs(b["vel"][1]) > 0.3]
            print("frame %4d  %5.2f ms  player=(%.1f,%.1f) state=%d  "
                  "bullets=%d live=%d moving=%d  enemies=%d fatal=%d"
                  % (f, reader.stats["last_ms"],
                     state["player"]["pos"][0], state["player"]["pos"][1],
                     state["player"]["state"], len(state["bullets"]), len(live),
                     len(moving), len(state["enemies"]),
                     sum(1 for e in state["enemies"] if e["fatal"])))
        if f != watch - 1:
            time.sleep(1.0 / FPS)
    times.sort()
    print("read: median %.2f ms  max %.2f ms  (%d frames)"
          % (times[len(times) // 2], times[-1], len(times)))
    if watch > 1:
        state = reader.snapshot()
        print("\n--- crop view as the model sees it ---")
        import view
        print(view.render_as(state, "crop"))


if __name__ == "__main__":
    main()
