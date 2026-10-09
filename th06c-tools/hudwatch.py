#!/usr/bin/env python3
"""Pin a HUD counter to its memory address by watching it change.

Reads the on-screen count (lives or bombs) from the HUD, records every memory
address holding that value, then waits for the count to change and keeps only
the addresses that followed it. Repeating the round narrows the set to one.

Nothing is injected into the game: the change is made by the person playing, so
this works even when key injection does not reach the game.

Two filters make the result trustworthy:

* stability — a candidate must hold the HUD value on several consecutive
  samples. Without this, a value that oscillates on its own (a timer, say) can
  coincidentally match "3, then 2, then 1" and be reported as the counter.
* width — the counter may not be a 4-byte int, so several widths are tried and
  the widest unambiguous hit is preferred.

  hudwatch.py --field bombs [--rounds 2] [--widths 4,2,1] [--timeout 240]
  hudwatch.py --field bombs --verify --write 4
  hudwatch.py --field bombs --await-start 3    wait for a usable value first

--verify settles the answer by experiment instead of elimination: it writes a
probe value to each surviving candidate in turn and keeps the one whose write
the HUD actually reflects. Narrowing alone can fail if the counter is not a
plain aligned int, or if it was not steady when the baseline was taken.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hud           # noqa: E402
import memedit as M  # noqa: E402
import th06          # noqa: E402


def argval(flag, default=None):
    if flag in sys.argv:
        return sys.argv[sys.argv.index(flag) + 1]
    return default


def hud_count(hwnd, field):
    return hud.count(hud.scan(hwnd)[field])


def stable_hud(hwnd, field, settle=0.45):
    """Read the HUD count twice; return it only if it did not move."""
    a = hud_count(hwnd, field)
    time.sleep(settle)
    b = hud_count(hwnd, field)
    return b if a == b else None


def wait_change(hwnd, field, current, timeout):
    """Block until the HUD count changes and settles; return the new value."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = hud_count(hwnd, field)
        if v != current:
            time.sleep(0.4)                 # let the change finish
            if hud_count(hwnd, field) == v:
                return v
            current = hud_count(hwnd, field)
        time.sleep(0.2)
    return None


def scan_candidates(h, regions, value, width):
    pat = M.pack_int(value, width)
    out = []
    for base, size, _prot, _typ in regions:
        data = M.read_mem(h, base, size)
        if not data:
            continue
        i = data.find(pat)
        while i >= 0:
            if i % width == 0:
                out.append(base + i)
            i = data.find(pat, i + 1)
    return out


def filter_stable(h, addrs, width, value, samples=3, gap=0.12):
    """Keep only addresses that hold `value` on every sample."""
    alive = list(addrs)
    for i in range(samples):
        alive = [a for a in alive if M.read_int(h, a, width) == value]
        if not alive:
            break
        if i < samples - 1:
            time.sleep(gap)
    return alive


def verify_writes(hwnd, field, ordered, base_value, probe_delta=3,
                  hold=2.0, cap=None):
    """Write a probe value to each candidate; the HUD names the real one.

    Two details matter, and getting either wrong produces a false negative:

    * the probe is re-written continuously from a second thread, and the HUD is
      sampled *while* that is happening. Writing once and sampling afterwards
      misses any value the game recomputes every frame — the sample lands after
      the game has already restored it.
    * the probe goes *down* (base-1), not up. A display that caps its icon
      count would render an upward probe as the cap, which looks like a miss.
    """
    _, hw = M.open_game(write=True)
    probe = base_value - 1 if base_value >= 1 else base_value + 1
    if cap is not None:
        ordered = ordered[:cap]
    hits = []
    for addr, width in ordered:
        orig = M.read_int(hw, addr, width)
        if orig is None:
            continue
        pbytes = M.pack_int(probe, width)
        stop = [False]

        def writer():
            while not stop[0]:
                M.write_mem(hw, addr, pbytes)

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        seen = {}
        t0 = time.time()
        while time.time() - t0 < hold:
            n = hud_count(hwnd, field)
            seen[n] = seen.get(n, 0) + 1
        stop[0] = True
        t.join(timeout=1.0)
        M.write_mem(hw, addr, M.pack_int(orig, width))   # always restore

        hit = probe in seen
        print("  %s%016x width %d: held %d, HUD %s seen as %s%s"
              % ("HIT " if hit else "    ", addr, width, probe, field,
                 dict(sorted(seen.items())),
                 "  <-- this is it" if hit else ""), flush=True)
        if hit:
            hits.append((addr, width))
    return hits


def main():
    field = argval("--field", "bombs")
    if field not in ("lives", "bombs"):
        raise SystemExit("--field must be lives or bombs")
    rounds = int(argval("--rounds", "2"))
    timeout = float(argval("--timeout", "240"))
    widths = [int(w) for w in argval("--widths", "4,2,1").split(",")]
    write_val = argval("--write")

    hwnd = th06.find_game()[0]
    pid, h = M.open_game()
    regions = M.rw_regions(h)

    value = stable_hud(hwnd, field)
    if value is None:
        raise SystemExit("HUD %s is not stable — is the game running?" % field)
    await_min = int(argval("--await-start", "0"))
    while value < await_min:
        print("waiting for HUD %s >= %d (currently %d)..."
              % (field, await_min, value), flush=True)
        time.sleep(1.0)
        value = stable_hud(hwnd, field)
        if value is None:
            value = 0
    print("HUD %s = %d  (%d regions)" % (field, value, len(regions)), flush=True)
    if value == 0:
        print("warning: count is 0, it cannot decrease further")

    results = {}
    for width in widths:
        cands = scan_candidates(h, regions, value, width)
        stable = filter_stable(h, cands, width, value)
        results[width] = stable
        print("width %d: %d raw, %d stable candidate(s) hold %d"
              % (width, len(cands), len(stable), value), flush=True)

    cur = value
    for rnd in range(1, rounds + 1):
        print("\nround %d: waiting for HUD %s to change from %d "
              "(change it now, up to %.0fs)..."
              % (rnd, field, cur, timeout), flush=True)
        new = wait_change(hwnd, field, cur, timeout)
        if new is None:
            print("timed out with no HUD change")
            break
        print("HUD %s: %d -> %d" % (field, cur, new), flush=True)
        for width in widths:
            pre = [a for a in results[width]
                   if M.read_int(h, a, width) == new]
            results[width] = filter_stable(h, pre, width, new)
            print("  width %d: %d survivor(s)" % (width, len(results[width])),
                  flush=True)
        cur = new
        if all(not results[w] for w in widths):
            print("  nothing survived at any width")
            break

    print("\n=== survivors ===")
    ordered = []
    seen = set()
    for width in widths:                     # widest first wins a tie
        for a in results[width]:
            if a in seen:
                continue
            seen.add(a)
            ordered.append((a, width))
    for a, width in ordered:
        print("  width %d  %016x = %s" % (width, a, M.read_int(h, a, width)))

    if "--verify" in sys.argv:
        hits = []
        for width in widths:                 # widest first, stop at first hit
            group = [(a, width) for a in results[width]]
            if not group:
                continue
            print("\n=== write-test: width %d (%d candidate(s)) ==="
                  % (width, len(group)), flush=True)
            hits = verify_writes(hwnd, field, group, value,
                                 cap=None if width >= 2 else 100)
            if hits:
                break
        if hits:
            print("confirmed: %s" % ", ".join("%016x/width %d" % t
                                               for t in hits))
            ordered = hits
        else:
            print("no candidate changed the HUD — counter not among them")
            return

    if write_val is None:
        return
    if not ordered:
        print("\nnot writing: no survivors")
        return
    addr, width = ordered[0]
    want = int(write_val, 0)
    _, hw = M.open_game(write=True)
    ok = M.write_mem(hw, addr, M.pack_int(want, width))
    print("\nwrote %d (width %d) to %016x -> %s (readback %s)"
          % (want, width, addr, "ok" if ok else "FAILED",
             M.read_int(hw, addr, width)))
    for i in range(3):
        time.sleep(0.4)
        print("  +%.1fs  mem=%s  hud_%s=%d"
              % (0.4 * (i + 1), M.read_int(hw, addr, width), field,
                 hud_count(hwnd, field)))


if __name__ == "__main__":
    main()
