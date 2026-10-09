#!/usr/bin/env python3
"""Find candidate game-state objects in .data by their field signature.

The update function at 0x2CCF0 receives `this` in rcx and reads two fields very
close together:

  +0x11608  dword   state index, dispatched through a 0x15-entry jump table
  +0x11610  qword   pointer to the player array

Whichever object owns those offsets is the stage object. Rather than guess its
address, read all of writable .data once and test every aligned candidate for
that signature, then follow the pointer chain one level to confirm it is real.

Read-only: no writes to the game.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memedit as M   # noqa: E402
import modbase as MB  # noqa: E402

STATE_OFF = 0x11608
PLAYERS_OFF = 0x11610
TAIL = PLAYERS_OFF + 8


def is_ptr(v):
    # Anything in the user-mode heap or image range, excluding low garbage.
    return v > 0x10000 and v % 8 == 0 and v < 0x00007FFFFFFFFFFF


def main():
    pid, h = M.open_game()
    _name, base, _size = MB.find_module(pid, "th06c")
    secs = MB.sections(h, base)
    data_sec = [s for s in secs if s[0] == ".data"]
    if not data_sec:
        raise SystemExit(".data not found")
    _n, va, vsize, _rawsize, _rawptr, _ch = data_sec[0]
    blob = M.read_mem(h, base + va, vsize)
    if not blob:
        raise SystemExit("cannot read .data")
    print("read .data %#x bytes at %016x" % (len(blob), base + va))

    limit = len(blob) - TAIL
    cands = []
    for off in range(0, limit, 8):
        state = struct.unpack_from("<i", blob, off + STATE_OFF)[0]
        if not (0 <= state <= 0x14):
            continue
        p = struct.unpack_from("<Q", blob, off + PLAYERS_OFF)[0]
        if not is_ptr(p):
            continue
        cands.append((base + va + off, state, p))

    print("%d candidate(s) with 0<=state<=0x14 and a pointer at +0x11610"
          % len(cands))
    for addr, state, p in cands:
        note = ""
        hdr = M.read_mem(h, p, 0x40)
        if hdr:
            fx = struct.unpack_from("<f", hdr, 0x2C)[0]
            rec = struct.unpack_from("<Q", hdr, 0x38)[0]
            note = "  arr+0x2c(float)=%r  arr+0x38=%016x" % (fx, rec)
            if is_ptr(rec):
                rb = M.read_mem(h, rec, 0x20)
                if rb:
                    note += "  rec bytes: " + rb[:12].hex()
        else:
            note = "  <players unreadable>"
        print("  obj %016x (RVA %08x) state=%d players=%016x%s"
              % (addr, addr - base, state, p, note))


if __name__ == "__main__":
    main()
