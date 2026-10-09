#!/usr/bin/env python3
"""Catch the game live, snapshot memory, and pull the bullet vertices out of it.

The paused game cannot validate the pool: the vertex array is rebuilt every
frame, so while paused it holds whatever the pause overlay drew while the
framebuffer still shows the last gameplay frame. Everything therefore has to
happen in one short live window.

Timing matters. Bullets move, so the screen capture and the memory read have to
land at the same instant or the positions will not line up. The order here is:

  1. snapshot every writable region          (A)
  2. wait --interval, snapshot again         (B)
  3. capture the playfield from the framebuffer, immediately after B
  4. diff A against B for floats that moved  -> candidate pools
  5. in each candidate region, find stride-0x30 vertices of the form
     [x,y,z,pad][0,-1,color,pad][u,v,pad,pad] -- read out of snapshot B, so
     they are from the same moment as the frame
  6. score each candidate by how many of its positions land on a bright pixel
     in that frame, and draw the screen next to the best few

Output: liverun.txt (numbers) and liverun_cmp.bmp (screen | regions).

  liverun.py [--wait 600] [--interval 0.35] [--top 16] [--minrows 5]
              [--now] [--bucket 0x1304a640000]

--now skips the wait for live frames and --bucket forces one region into the
report; both exist to smoke-test the pipeline against a paused game.
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memedit as M   # noqa: E402
import th06           # noqa: E402

CROP = (856, 48, 1152, 1344)
SCALE = 6
TW, TH = 192, 224
SIG = b"\x00\x00\x80\xbf"          # -1.0f, the vertex normal's z
LOG = []


def log(msg):
    print(msg, flush=True)
    LOG.append(msg)


def argval(flag, default):
    if flag in sys.argv:
        return sys.argv[sys.argv.index(flag) + 1]
    return default


def rows_differ(hwnd, gap):
    """Differing sampled rows between two frames `gap` apart."""
    w, h, ra, sa, _u, _d = th06.capture(hwnd)
    time.sleep(gap)
    w2, h2, rb, sb, _u2, _d2 = th06.capture(hwnd)
    if h != h2 or w != w2:
        return 10 ** 6
    n = 0
    for y in range(0, h, 2):
        oa, ob = y * sa, y * sb
        if ra[oa:oa + w * 4] != rb[ob:ob + w2 * 4]:
            n += 1
    return n


def crop(hwnd):
    """Downscaled playfield rows, 3 bytes per pixel."""
    w, h, raw, stride, _u, _d = th06.capture(hwnd)
    x0, y0, _cw, _ch = CROP
    out = []
    for y in range(TH):
        row = bytearray(TW * 3)
        base = (y0 + y * SCALE) * stride
        for x in range(TW):
            o = base + (x0 + x * SCALE) * 3
            row[x * 3:x * 3 + 3] = raw[o:o + 3]
        out.append(row)
    return out


def snapshot(h):
    snap = {}
    for base, size, _p, _t in M.regions(h, rw=True):
        d = M.read_mem(h, base, size)
        if d:
            snap[base] = d
    return snap


def movers(a, b, vmin=0.5, vmax=500.0):
    out = []
    for base, da in a.items():
        db = b.get(base)
        if db is None or len(db) != len(da):
            continue
        for poff in range(0, len(da), 0x1000):
            pa = da[poff:poff + 0x1000]
            pb = db[poff:poff + 0x1000]
            if pa == pb:
                continue
            for o in range(0, min(len(pa), len(pb)) - 4, 4):
                va = struct.unpack_from("<f", pa, o)[0]
                vb = struct.unpack_from("<f", pb, o)[0]
                if not (va == va and vb == vb):
                    continue
                if abs(va - vb) < 0.01:
                    continue
                if vmin <= abs(va) <= vmax and vmin <= abs(vb) <= vmax:
                    out.append((base + poff + o, va, vb))
    return out


def vertices(d, base):
    """Stride-0x30 sprite vertices with positions in playfield range."""
    out = []
    i = d.find(SIG)
    while i >= 0:
        rec = i - 0x14
        if rec >= 0 and rec % 4 == 0 and rec + 0x30 <= len(d):
            f3, f4 = struct.unpack_from("<2f", d, rec + 0x0C)
            x, y = struct.unpack_from("<2f", d, rec)
            if (f3 == 0 and f4 == 0 and x == x and y == y
                    and -192 <= x <= 192 and -224 <= y <= 224
                    and (abs(x) > 1 or abs(y) > 1)):
                out.append((base + rec, x, y))
        i = d.find(SIG, i + 1)
    return out


def bright_at(frame, x, y, radius=4, lum=90):
    """Is there a bright pixel near this playfield position in the crop?"""
    px = int((x + 192) / 384 * TW)
    py = int((y + 224) / 448 * TH)
    for dy in range(-radius, radius + 1):
        ny = py + dy
        if not 0 <= ny < TH:
            continue
        row = frame[ny]
        for dx in range(-radius, radius + 1):
            nx = px + dx
            if not 0 <= nx < TW:
                continue
            o = nx * 3
            b, g, r = row[o], row[o + 1], row[o + 2]
            if (r * 299 + g * 587 + b * 114) // 1000 >= lum:
                return True
    return False


def write_bmp(path, tw, th, rows):
    rowsize = (tw * 3 + 3) & ~3
    pad = b"\0" * (rowsize - tw * 3)
    data = b"".join(bytes(r) + pad for r in reversed(rows))
    hdr = (struct.pack("<2sIHHI", b"BM", 14 + 40 + len(data), 0, 0, 54)
           + struct.pack("<IiiHHIIiiII", 40, tw, th, 1, 24, 0, len(data),
                         2835, 2835, 0, 0))
    open(path, "wb").write(hdr + data)


def panel(pos, bg=(0x10, 0x08, 0x04)):
    rows = [bytearray(bytes(bg) * TW) for _ in range(TH)]
    for x, y in pos:
        px = int((x + 192) / 384 * TW)
        py = int((y + 224) / 448 * TH)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                nx, ny = px + dx, py + dy
                if 0 <= nx < TW and 0 <= ny < TH:
                    o = nx * 3
                    rows[ny][o:o + 3] = b"\x40\xff\xff"
    return rows


def main():
    wait = float(argval("--wait", "600"))
    interval = float(argval("--interval", "0.35"))
    top = int(argval("--top", "16"))
    minrows = int(argval("--minrows", "5"))
    npanel = int(argval("--panels", "6"))
    forced = argval("--bucket", None)

    hwnd = th06.find_game()[0]
    if "--now" in sys.argv:
        log("game window %#x; --now, skipping the live wait" % hwnd)
    else:
        log("game window %#x; waiting up to %gs for live frames" % (hwnd, wait))
        log("(focus is not taken -- switch to the game and resume it)")

    t0 = time.time()
    while "--now" not in sys.argv:
        n = rows_differ(hwnd, 0.3)
        if n >= minrows:
            log("LIVE after %.1fs (%d differing rows)" % (time.time() - t0, n))
            break
        if time.time() - t0 > wait:
            log("gave up after %gs: still paused (%d rows)" % (wait, n))
            open("liverun.txt", "w").write("\n".join(LOG) + "\n")
            return
        if int(time.time() - t0) % 15 < 1:
            log("  still paused (%.0fs, %d rows)" % (time.time() - t0, n))

    pid, h = M.open_game()
    ta = time.time()
    a = snapshot(h)
    time.sleep(interval)
    b = snapshot(h)
    log("snapshots: %d regions, %.2fs apart" % (len(a), time.time() - ta))

    # The frame goes here, right after B, so screen and memory agree in time.
    frame = crop(hwnd)
    log("frame captured immediately after snapshot B")

    after = rows_differ(hwnd, 0.2)
    log("liveness after capture: %d rows%s"
        % (after, "" if after >= minrows else "  <-- SUSPECT, game may have paused"))

    mv = movers(a, b)
    log("movers (0.5 <= |v| <= 500): %d" % len(mv))

    buckets = {}
    for addr, va, vb in mv:
        buckets.setdefault(addr >> 16, []).append((addr, va, vb))
    ranked = sorted(buckets.items(), key=lambda kv: -len(kv[1]))[:top]
    if forced:
        fb = int(forced, 0)
        ranked = [(fb >> 16, buckets.get(fb >> 16, []))]

    log("\ntop mover clusters:")
    for bucket, items in ranked[:npanel]:
        if not items:
            log("  %012x0000  (forced, no movers)" % bucket)
            continue
        addrs = sorted(i[0] for i in items)
        gaps = {}
        for p, q in zip(addrs, addrs[1:]):
            d = q - p
            if 0x8 <= d <= 0x1000:
                gaps[d] = gaps.get(d, 0) + 1
        g = sorted(gaps.items(), key=lambda kv: -kv[1])[:4]
        log("  %012x0000  n=%-5d span=%#-8x gaps: %s"
            % (bucket, len(items), addrs[-1] - addrs[0],
               ", ".join("%#x x%d" % t for t in g) or "-"))

    # Vertices come out of snapshot B, not a fresh read, so they match the frame.
    log("\nvertex extraction (from snapshot B) and framebuffer agreement:")
    cands = []
    for bucket, _items in ranked:
        base = bucket << 16
        host, data = None, None
        for rbase, d in b.items():
            if rbase <= base < rbase + len(d):
                host, data = rbase, d
                break
        if data is None:
            log("  %012x0000  not inside any snapshotted region" % bucket)
            continue
        off = base - host
        vs = vertices(data[off:off + 0x10000], base)
        if not vs:
            log("  %012x0000  0 vertices" % bucket)
            continue
        pos = sorted({(round(x, 1), round(y, 1)) for _a, x, y in vs})
        hit = sum(1 for x, y in pos if bright_at(frame, x, y))
        cands.append((hit / len(pos), bucket, len(vs), len(pos), pos))
        log("  %012x0000  %3d vertices, %3d positions, %3d on bright pixels (%.0f%%)"
            % (bucket, len(vs), len(pos), hit, 100.0 * hit / len(pos)))

    cands.sort(reverse=True)
    if cands:
        log("\nbest agreement:")
        for frac, bucket, nv, npos, _pos in cands[:5]:
            log("  %012x0000  %.0f%% of %d positions on a bright pixel"
                % (bucket, 100 * frac, npos))

    panels = [frame] + [panel(c[4]) for c in cands[:npanel - 1]]
    if not cands:                       # forced bucket with no movers
        for bucket, _ in ranked[:1]:
            base = bucket << 16
            d = M.read_mem(h, base, 0x10000)
            if d:
                pos = sorted({(round(x, 1), round(y, 1))
                              for _a, x, y in vertices(d, base)})
                panels.append(panel(pos))

    gap = 6
    rows = []
    for y in range(TH):
        parts = [bytes(p[y]) for p in panels]
        rows.append((b"\x20\x20\x20" * gap).join(parts))
    write_bmp("liverun_cmp.bmp", TW * len(panels) + gap * (len(panels) - 1),
              TH, rows)
    log("\nwrote liverun_cmp.bmp: screen | top %d candidates by agreement"
        % (len(panels) - 1))

    with open("liverun.txt", "w") as f:
        f.write("\n".join(LOG) + "\n")
        f.write("\n=== movers (%d) ===\n" % len(mv))
        for addr, va, vb in sorted(mv):
            f.write("%016x  %g -> %g\n" % (addr, va, vb))
    log("wrote liverun.txt")


if __name__ == "__main__":
    main()
