#!/usr/bin/env python3
"""Named access to th06c game-state fields, derived by static analysis.

Every address is an RVA: an offset from the module base, which is stable across
launches. At runtime add the base from modbase.find_module(pid, "th06c").

Evidence for each field (see disasm.py for the tools used):

  lives     byte   RVA 0x3EE804
      The HUD star row loops `movsx eax, byte [0x3EE804]` drawing one star per
      unit (RVA 0x2973F). Stage init sets it from the config byte 0xBC28EC
      (RVA 0x91BA). The stage-clear bonus computes lives * 3,000,000
      (RVA 0x27335, `imul r9d, eax, 0x2dc6c0`). The result screen writes it back
      to 0xBC28EC (RVA 0x2431F).

  bombs     byte   RVA 0x3EE805
      Same star loop at RVA 0x297AF. Stage init sets it from config byte
      0xBC28ED (RVA 0x91C7). `inc byte [0x3EE805]` on bomb pickup (RVA 0x2B618).
      Result screen writes it back to 0xBC28ED (RVA 0x24312).

  score     dword  RVA 0x3ED7BC   read by the HUD score text (RVA 0x29990)
  score_bak dword  RVA 0x3ED7C0   zeroed alongside the score at stage init
  highscore dword  RVA 0x3ED7C8   read by the HUD high-score text (RVA 0x299C0)
  power     word   RVA 0x3ED034   HUD power bar draws up to 128 segments
                                  (RVA 0x297EE, `mov ebx, 0x80`)

Both counters are read with movsx, so they are signed bytes. Keep values in a
sane range (0..8): the HUD loops once per unit and draws one sprite each time.

  gamestate.py show                 # read every field
  gamestate.py set bombs 4          # write one field
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memedit as M   # noqa: E402
import modbase as MB  # noqa: E402

# name -> (rva, size in bytes, signed)
FIELDS = {
    "lives":     (0x3EE804, 1, True),
    "bombs":     (0x3EE805, 1, True),
    "score":     (0x3ED7BC, 4, False),
    "score_bak": (0x3ED7C0, 4, False),
    "highscore": (0x3ED7C8, 4, False),
    "power":     (0x3ED034, 2, False),
    "cfg_lives": (0xBC28EC, 1, False),
    "cfg_bombs": (0xBC28ED, 1, False),
    # Read by the respawn path at RVA 0x2CC00, so these are what a death restores
    # lives and bombs to. Confirmed: after setting the counters to 8 and dying,
    # they came back as 2/3, matching this pair (the 0x302 override at 0x2CC30 was
    # skipped because [0x3ED7D0] was 0 and [0x3ED7D8] was not 4).
    "start_lives": (0xBC28C4, 1, False),
    "start_bombs": (0xBC28C5, 1, False),
}

# Sensible bounds, so a typo cannot drive the HUD draw loop wild.
LIMITS = {"lives": (0, 8), "bombs": (0, 8),
          "start_lives": (0, 8), "start_bombs": (0, 8)}


def game():
    # write=True is required: a read-only handle makes every write fail, and
    # before write_mem raised on failure that happened silently.
    pid, h = M.open_game(write=True)
    _name, base, _size = MB.find_module(pid, "th06c")
    return h, base


def read_field(h, base, name):
    rva, size, signed = FIELDS[name]
    raw = M.read_mem(h, base + rva, size)
    if not raw:
        return None
    return int.from_bytes(raw, "little", signed=signed)


def write_field(h, base, name, value):
    rva, size, _signed = FIELDS[name]
    lo, hi = LIMITS.get(name, (None, None))
    if lo is not None and not (lo <= value <= hi):
        raise SystemExit("%s must be between %d and %d (got %d)"
                         % (name, lo, hi, value))
    M.write_mem(h, base + rva, value.to_bytes(size, "little"))


def main():
    args = sys.argv[1:]
    h, base = game()
    if not args or args[0] == "show":
        print("module base %016x" % base)
        for name in FIELDS:
            print("  %-10s RVA %08x = %s" % (name, FIELDS[name][0],
                                             read_field(h, base, name)))
        return
    if args[0] == "set" and len(args) == 3:
        name = args[1]
        if name not in FIELDS:
            raise SystemExit("unknown field %r" % name)
        old = read_field(h, base, name)
        write_field(h, base, name, int(args[2], 0))
        print("%s: %s -> %s" % (name, old, read_field(h, base, name)))
        return
    raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
