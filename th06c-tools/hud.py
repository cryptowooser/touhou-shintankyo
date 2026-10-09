#!/usr/bin/env python3
"""Count the star icons in the th06c HUD to read lives and bombs.

The HUD shows remaining lives as red stars on the "Player" row and remaining
bombs as green stars on the "Bomb" row. Counting icons by colour reads those
counters without inspecting the image by eye, which makes it usable as an
automated check around a memory edit.

  hud.py                       count stars in the default HUD region
  hud.py --region x,y,w,h      override the scanned region
  hud.py --shot out.bmp        also write the capture to a BMP
  hud.py --verbose             list each detected icon's x range

Importable: scan(hwnd) -> dict with lives/bombs/dark and the raw pixel lists.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import th06  # noqa: E402

# Right-hand stats panel of the 3440x1440 fullscreen window, covering only the
# "Player" (red stars, y=372-409) and "Bomb" (green stars, y=446-477) rows.
# Starting at x=2205 excludes the "Power" (red) and "Graze" (green) text labels.
DEFAULT_REGION = (2205, 350, 700, 145)


def px(raw, stride, x, y):
    o = y * stride + x * 3
    return raw[o + 2], raw[o + 1], raw[o]      # R, G, B


def is_red(r, g, b):
    return r > 120 and r - g > 50 and r - b > 50


def is_green(r, g, b):
    return g > 120 and g - r > 50 and g - b > 50


def bands(ys, gap=6):
    """Group y values into contiguous bands."""
    if not ys:
        return []
    ys = sorted(set(ys))
    out = []
    start = prev = ys[0]
    for y in ys[1:]:
        if y - prev > gap:
            out.append((start, prev))
            start = y
        prev = y
    out.append((start, prev))
    return out


def runs(xs, gap=4):
    """Group x values into contiguous runs; one run per icon."""
    if not xs:
        return []
    xs = sorted(set(xs))
    out = []
    start = prev = xs[0]
    for x in xs[1:]:
        if x - prev > gap:
            out.append((start, prev))
            start = x
        prev = x
    out.append((start, prev))
    return out


def icons(pts):
    """Return [(y0, y1, [x runs])] — one entry per detected row of icons."""
    out = []
    for by0, by1 in bands([y for _x, y in pts]):
        xs = [x for x, y in pts if by0 <= y <= by1]
        out.append((by0, by1, runs(xs)))
    return out


def count(groups):
    """Total number of icons across every detected row."""
    return sum(len(rr) for _y0, _y1, rr in groups)


def scan(hwnd, region=DEFAULT_REGION):
    w, h, raw, stride, used, dark = th06.capture(hwnd)
    x0, y0, rw, rh = region
    red, green = [], []
    for y in range(y0, min(y0 + rh, h)):
        for x in range(x0, min(x0 + rw, w)):
            r, g, b = px(raw, stride, x, y)
            if is_red(r, g, b):
                red.append((x, y))
            elif is_green(r, g, b):
                green.append((x, y))
    return {"w": w, "h": h, "used": used, "dark": dark, "raw": raw,
            "stride": stride, "lives": icons(red), "bombs": icons(green)}


def describe(label, groups):
    if not groups:
        return "%s: none" % label
    parts = []
    for by0, by1, rr in groups:
        parts.append("y=%d-%d n=%d x=%s"
                     % (by0, by1, len(rr),
                        ",".join("%d-%d" % t for t in rr)))
    return "%s: %s" % (label, "  ".join(parts))


def main():
    args = sys.argv[1:]
    region = DEFAULT_REGION
    if "--region" in args:
        region = tuple(int(v)
                       for v in args[args.index("--region") + 1].split(","))

    hwnd = th06.find_game()[0]
    r = scan(hwnd, region)
    if "--shot" in args:
        th06.write_bmp(args[args.index("--shot") + 1], r["w"], r["h"], r["raw"])

    print("window %dx%d via %s (dark=%.2f)  region %d,%d %dx%d"
          % (r["w"], r["h"], r["used"], r["dark"], region[0], region[1],
             region[2], region[3]))
    print("  " + describe("red  (lives)", r["lives"]))
    print("  " + describe("green(bombs)", r["bombs"]))


if __name__ == "__main__":
    main()
