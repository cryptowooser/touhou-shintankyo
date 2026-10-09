#!/usr/bin/env python3
"""Read everything that can kill the player out of game state, and overlay it.

There are exactly two fatal sources, and they are different code paths.

  Bullets graze and then kill, via Player::CheckGraze (RVA 0x4C3E0) and
  Player::CalcKillBoxCollision (RVA 0x4C690). Only bullets graze.

  Enemies can only kill -- there is no graze credit for touching one.
  EnemyManager calls Player::CalcKillBoxCollision directly.

Player::CalcKillBoxCollision is what calls Player::Die().

  g_BulletManager  RVA 0x955410
    bullets[0]       +0x008   stride 0x5D0, 640 slots
    bulletCount      +0xE8808
    Bullet.pos       +0x030
    Bullet.state     +0x044   0 inactive, 1 fired, 2..4 spawning, 5 despawning
    Bullet.isGrazed  +0x5C8

  g_EnemyManager   RVA 0xA7E1E0
    enemyCount       +0x000
    enemies[0]       +0x008   stride 0xFD0, 257 slots
    Enemy.position   +0x0A8
    Enemy.flags1     +0x0B4   bit 7 isSlotOccupied
    Enemy.flags2     +0x0B5   b0 isInteractable, b1 isCollidable,
                              b2 hasBeenInBounds, b3 isBoss, b4 isDamageable
    Enemy.flags3     +0x0B6   b3 isInvisible
    Enemy.life       +0x21C
    Enemy.hitbox     +0x2A4   raw; the touch test uses this / 1.5f

An enemy kills on touch exactly when

    isSlotOccupied && isInteractable && isCollidable && hasBeenInBounds
        && !isBoss && !isInvisible

so bosses are drawn distinctly -- they shoot at you but will not kill you by
contact.

The player is read as the anchor everything else is judged against:

  g_Player         RVA 0xBB89F0
    hitboxTopLeft    +0x410   the 2.5x2.5 hitbox, not the sprite
    hitboxBottomRight +0x41C
    playerState      +0x73B8  0 alive

Coordinates are game units, y increasing downward, 0 at the top of the field.
The playfield is 384x448 units and occupies the window rect (856,48)-(2008,1392),
which is 1152x1344 px, i.e. 3 px per unit. The overlay is subsampled 6x to
192x224, so one overlay pixel is 2 game units.

The y convention was not assumed. It was settled by scoring how much each
candidate pixel position deviates from the local background: y-down averaged
28.9 against y-up's 6.6 over the same 13 on-field enemies, and the player's own
hitbox independently lands on the player sprite. Both panels are still drawn, so
a regression would be visible rather than silent.

  entities.py                 one frame, dump + overlay
  entities.py --dump          text only, no screen capture
  entities.py --watch N       N frames, 0.25s apart
  entities.py --catch S       poll until bullets are in flight, then capture
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memedit as M      # noqa: E402
import modbase           # noqa: E402
import pngout            # noqa: E402
import th06              # noqa: E402

BM_RVA = 0x955410
BULLETS_OFF = 0x008
BULLET_STRIDE = 0x5D0
BULLET_SLOTS = 640
BULLET_COUNT_OFF = 0xE8808
B_OFF_POS = 0x030
B_OFF_STATE = 0x044
B_OFF_GRAZE_SIZE = 0x5A4
B_OFF_GRAZED = 0x5C8

EM_RVA = 0xA7E1E0
ENEMY_COUNT_OFF = 0x000
ENEMIES_OFF = 0x008
ENEMY_STRIDE = 0xFD0
ENEMY_SLOTS = 257
E_OFF_POS = 0x0A8
E_OFF_FLAGS1 = 0x0B4
E_OFF_FLAGS2 = 0x0B5
E_OFF_FLAGS3 = 0x0B6
E_OFF_LIFE = 0x21C
E_OFF_HITBOX = 0x2A4

PLAYER_RVA = 0xBB89F0
P_OFF_HITBOX_TL = 0x410
P_OFF_HITBOX_BR = 0x41C
P_OFF_STATE = 0x73B8

# Playfield: 384x448 units at 3 px/unit, origin (856,48) in the window.
PLAYFIELD = (856, 48)
PX_PER_UNIT = 3.0
FIELD_W, FIELD_H = 384.0, 448.0
SCALE = 6                       # overlay subsample
TW = int(FIELD_W * PX_PER_UNIT / SCALE)     # 192
TH = int(FIELD_H * PX_PER_UNIT / SCALE)     # 224

STATE_NAME = {0: "inactive", 1: "fired", 2: "spawn-fast",
              3: "spawn-normal", 4: "spawn-slow", 5: "despawning"}

# Rows are 24bpp BGR, so these literals are B, G, R in that order.
GREEN = b"\x00\xff\x00"     # bullet
RED = b"\x30\x30\xff"       # enemy that kills on touch
CYAN = b"\xff\xc0\x40"      # enemy that does not (boss / non-collidable)
WHITE = b"\xff\xff\xff"     # the player's hitbox centre


# ---------------------------------------------------------------- reading

def read_bullets(h, base):
    bm = base + BM_RVA
    n, = struct.unpack("<i", M.read_mem(h, bm + BULLET_COUNT_OFF, 4))
    out = []
    for i in range(BULLET_SLOTS):
        a = bm + BULLETS_OFF + i * BULLET_STRIDE
        st, = struct.unpack("<H", M.read_mem(h, a + B_OFF_STATE, 2))
        if st == 0:
            continue
        px, py, pz = struct.unpack("<3f", M.read_mem(h, a + B_OFF_POS, 12))
        gz, = struct.unpack("<B", M.read_mem(h, a + B_OFF_GRAZED, 1))
        sx, sy, _ = struct.unpack("<3f", M.read_mem(h, a + B_OFF_GRAZE_SIZE, 12))
        out.append({"idx": i, "state": st, "pos": (px, py, pz),
                    "grazed": gz, "size": (sx, sy), "addr": a})
    return n, out


def read_enemies(h, base):
    em = base + EM_RVA
    n, = struct.unpack("<i", M.read_mem(h, em + ENEMY_COUNT_OFF, 4))
    out = []
    for i in range(ENEMY_SLOTS):
        a = em + ENEMIES_OFF + i * ENEMY_STRIDE
        f1, f2, f3 = struct.unpack("<3B", M.read_mem(h, a + E_OFF_FLAGS1, 3))
        if not (f1 & 0x80):          # isSlotOccupied
            continue
        px, py, pz = struct.unpack("<3f", M.read_mem(h, a + E_OFF_POS, 12))
        life, = struct.unpack("<i", M.read_mem(h, a + E_OFF_LIFE, 4))
        hx, hy, hz = struct.unpack("<3f", M.read_mem(h, a + E_OFF_HITBOX, 12))
        e = {"idx": i, "pos": (px, py, pz), "life": life, "hitbox": (hx, hy, hz),
             "addr": a,
             "interactable": (f2 >> 0) & 1, "collidable": (f2 >> 1) & 1,
             "in_bounds": (f2 >> 2) & 1, "boss": (f2 >> 3) & 1,
             "damageable": (f2 >> 4) & 1, "invisible": (f3 >> 3) & 1,
             "death_mode": (f2 >> 5) & 7, "flags": (f1, f2, f3)}
        e["fatal"] = bool(e["interactable"] and e["collidable"]
                          and e["in_bounds"] and not e["boss"]
                          and not e["invisible"])
        out.append(e)
    return n, out


def read_player(h, base):
    """The player's hitbox centre -- the point everything else is measured to."""
    p = base + PLAYER_RVA
    state, = struct.unpack("<B", M.read_mem(h, p + P_OFF_STATE, 1))
    tl = struct.unpack("<3f", M.read_mem(h, p + P_OFF_HITBOX_TL, 12))
    br = struct.unpack("<3f", M.read_mem(h, p + P_OFF_HITBOX_BR, 12))
    return {"addr": p, "state": state,
            "pos": ((tl[0] + br[0]) / 2.0, (tl[1] + br[1]) / 2.0,
                    (tl[2] + br[2]) / 2.0),
            "hitbox": (br[0] - tl[0], br[1] - tl[1])}


def dump(nb, bullets, ne, enemies, player):
    if player:
        print("player hitbox=(%.2f,%.2f) size=(%.1fx%.1f) state=%d%s"
              % (player["pos"][0], player["pos"][1], player["hitbox"][0],
                 player["hitbox"][1], player["state"],
                 "" if player["state"] == 0 else "  (NOT ALIVE)"))
    print("bulletCount=%d  active=%d   |   enemyCount=%d  occupied=%d  fatal=%d"
          % (nb, len(bullets), ne, len(enemies),
             sum(1 for e in enemies if e["fatal"])))
    for b in bullets[:10]:
        print("  B[%3d] %-12s pos=(%8.2f,%8.2f) grazeSize=(%.1f,%.1f) grazed=%d"
              % (b["idx"], STATE_NAME.get(b["state"], "?"),
                 b["pos"][0], b["pos"][1], b["size"][0], b["size"][1],
                 b["grazed"]))
    if len(bullets) > 10:
        print("  ... %d more bullets" % (len(bullets) - 10))
    for e in enemies[:10]:
        print("  E[%3d] pos=(%8.2f,%8.2f) life=%-6d hitbox=(%.0f,%.0f) "
              "%s%s%s%s%s  %s"
              % (e["idx"], e["pos"][0], e["pos"][1], e["life"],
                 e["hitbox"][0], e["hitbox"][1],
                 "I" if e["interactable"] else "-",
                 "C" if e["collidable"] else "-",
                 "B" if e["in_bounds"] else "-",
                 "S" if e["boss"] else "-",
                 "V" if e["invisible"] else "-",
                 "FATAL ON TOUCH" if e["fatal"] else "harmless on touch"))
    if len(enemies) > 10:
        print("  ... %d more enemies" % (len(enemies) - 10))


# ---------------------------------------------------------------- overlay

def crop(hwnd):
    _w, _h, raw, stride, _u, _d = th06.capture(hwnd)
    x0, y0 = PLAYFIELD
    rows = []
    for y in range(TH):
        row = bytearray(TW * 3)
        base = (y0 + y * SCALE) * stride
        for x in range(TW):
            o = base + (x0 + x * SCALE) * 3
            row[x * 3:x * 3 + 3] = raw[o:o + 3]
        rows.append(row)
    return rows


def to_px(pos, ydown):
    """Game units -> overlay pixel. y is not assumed; both ways are offered."""
    x, y, _z = pos
    px = int(x * PX_PER_UNIT / SCALE)
    py = (int(y * PX_PER_UNIT / SCALE) if ydown
          else int((FIELD_H - y) * PX_PER_UNIT / SCALE))
    return px, py


def dot(rows, px, py, color, size=1):
    for dy in range(-size, size + 1):
        for dx in range(-size, size + 1):
            nx, ny = px + dx, py + dy
            if 0 <= nx < TW and 0 <= ny < TH:
                rows[ny][nx * 3:nx * 3 + 3] = color


def mark(rows, bullets, enemies, ydown, player=None):
    out = [bytearray(r) for r in rows]
    for b in bullets:
        px, py = to_px(b["pos"], ydown)
        dot(out, px, py, GREEN, 1)
    for e in enemies:
        px, py = to_px(e["pos"], ydown)
        dot(out, px, py, RED if e["fatal"] else CYAN, 2)
    if player:
        px, py = to_px(player["pos"], ydown)
        dot(out, px, py, WHITE, 1)
    return out


def write_bmp(path, tw, th, rows):
    rowsize = (tw * 3 + 3) & ~3
    pad = b"\0" * (rowsize - tw * 3)
    data = b"".join(bytes(r) + pad for r in reversed(rows))
    hdr = (struct.pack("<2sIHHI", b"BM", 14 + 40 + len(data), 0, 0, 54)
           + struct.pack("<IiiHHIIiiII", 40, tw, th, 1, 24, 0, len(data),
                         2835, 2835, 0, 0))
    open(path, "wb").write(hdr + data)


def main():
    argv = sys.argv[1:]
    watch = int(argv[argv.index("--watch") + 1]) if "--watch" in argv else 1
    text_only = "--dump" in argv
    catch = float(argv[argv.index("--catch") + 1]) if "--catch" in argv else None

    pid, h = modbase.open_game()
    _name, base, _size = modbase.find_module(pid, "th06c")
    hwnd = None if text_only else th06.find_game()[0]

    if catch is not None:
        # Poll for a frame with bullets actually in flight. A paused game or a
        # dialogue has bulletCount == 0, so there is nothing to align against.
        bm = base + BM_RVA
        t0 = time.time()
        best = 0
        while time.time() - t0 < catch:
            n, = struct.unpack("<i", M.read_mem(h, bm + BULLET_COUNT_OFF, 4))
            best = max(best, n)
            if n > 0:
                break
            time.sleep(0.02)
        if best == 0:
            print("no bullets in %.0fs (bulletCount stayed 0) -- game paused,"
                  " in dialogue, or between waves" % catch)
            return

    for f in range(watch):
        nb, bullets = read_bullets(h, base)
        ne, enemies = read_enemies(h, base)
        player = read_player(h, base)
        print("--- frame %d ---" % f)
        dump(nb, bullets, ne, enemies, player)
        if hwnd and (bullets or enemies) and f == watch - 1:
            rows = crop(hwnd)
            panels = [rows,
                      mark(rows, bullets, enemies, True, player),
                      mark(rows, bullets, enemies, False, player)]
            gap = 6
            joined = [(b"\x20\x20\x20" * gap).join(bytes(p[y]) for p in panels)
                      for y in range(TH)]
            pngout.write_png("entities.png", joined)
            pngout.write_png("entities_3x.png", pngout.upscale(joined, 3))
            print("   wrote entities.png (screen | y-down | y-up)"
                  "   green=bullet  red=enemy fatal on touch  cyan=enemy harmless"
                  "  white=player")
        if f != watch - 1:
            time.sleep(0.25)


if __name__ == "__main__":
    main()
