#!/usr/bin/env python3
"""Walk th06c's static task list and identify each node's owner object.

The task registration at 0x36E60 builds a 0x40-byte node, fills it with three
function pointers, and links it into a static sentinel at 0x3EC5F0 using
+0x20 (prev) and +0x28 (next). The node also receives a pointer at +0x38, which
is the candidate for the `this` passed to the update function at 0x2CCF0 -- the
object that holds the player records at +0x11610.

This reads the list read-only and reports, per node, the function RVAs and the
+0x38 pointer. Read-only: no writes, safe to run against a live game.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memedit as M   # noqa: E402
import modbase as MB  # noqa: E402

SENTINEL_RVA = 0x3EC5F0
UPDATE_FN_RVA = 0x2CCF0
NODE_SIZE = 0x40
MAX_NODES = 64


def q(h, a):
    d = M.read_mem(h, a, 8)
    return struct.unpack("<Q", d)[0] if d and len(d) == 8 else None


def main():
    pid, h = M.open_game()
    _name, base, _size = MB.find_module(pid, "th06c")
    sent = base + SENTINEL_RVA
    raw = M.read_mem(h, sent, NODE_SIZE)
    if not raw:
        raise SystemExit("cannot read sentinel at %016x" % sent)
    print("sentinel %016x (RVA %08x)" % (sent, SENTINEL_RVA))
    for off in range(0, NODE_SIZE, 8):
        print("  +%02x  %016x" % (off, struct.unpack_from("<Q", raw, off)[0]))

    # The sentinel's links are at the same offsets as a node's.
    node = q(h, sent + 0x28)
    seen = set()
    n = 0
    print("\nwalking next (+0x28) from sentinel+0x28:")
    while node and node not in seen and n < MAX_NODES:
        seen.add(node)
        n += 1
        buf = M.read_mem(h, node, NODE_SIZE)
        if not buf:
            print("  %016x  <unreadable>" % node)
            break
        words = struct.unpack("<8Q", buf)
        fns = [words[1], words[2], words[3]]
        fns_rva = ["%08x" % (f - base) if base <= f < base + 0x1000000 else "-" for f in fns]
        owner = words[7]
        mark = ""
        if words[1] == base + UPDATE_FN_RVA:
            mark = "  <== update fn 0x2ccf0"
        print("  node %016x  type=%d  fn=%s owner(+0x38)=%016x%s"
              % (node, struct.unpack_from("<H", buf, 0)[0], "/".join(fns_rva), owner, mark))
        node = words[5]

    # For the update node, chase the object fields that identify the game state.
    node = q(h, sent + 0x28)
    seen = set()
    while node and node not in seen:
        seen.add(node)
        buf = M.read_mem(h, node, NODE_SIZE)
        if not buf:
            break
        words = struct.unpack("<8Q", buf)
        if words[1] == base + UPDATE_FN_RVA:
            owner = words[7]
            print("\nupdate node owner = %016x" % owner)
            if owner:
                st = M.read_mem(h, owner + 0x11600, 0x20)
                if st:
                    print("  [owner+0x11608] state   = %d"
                          % struct.unpack_from("<i", st, 8)[0])
                    parr = struct.unpack_from("<Q", st, 0x10)[0]
                    print("  [owner+0x11610] players = %016x" % parr)
                    if parr:
                        rec = q(h, parr + 0x38)
                        print("  [players+0x38] record   = %016x" % rec)
                        if rec:
                            rb = M.read_mem(h, rec, 0x20)
                            if rb:
                                print("    record bytes: lives(+9)=%d bombs(+0xA)=%d"
                                      % (rb[9], rb[0xA]))
                                print("    record head:  " + " ".join("%02x" % x for x in rb[:16]))
            break
        node = words[5]


if __name__ == "__main__":
    main()
