#!/usr/bin/env python3
"""Find x64 RIP-relative references to an address inside th06c.exe's .text.

Every memory operand in x64 code is RIP-relative, so a reference to a global
encodes a 32-bit displacement as the last field of the instruction:

    target = (address_of_displacement + 4) + disp32

That holds whatever the instruction length is, so scanning every byte offset for
a displacement that resolves to the target finds real references without needing
a disassembler. Used to locate the code that reads a known global (e.g. the
score) so the globals read near it can be examined.

  findrefs.py 0x7ff711fbd7bc [0x...]
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memedit as M   # noqa: E402
import modbase as MB  # noqa: E402


def refs_in_text(h, base, va, vsize, target):
    tbase = base + va
    data = M.read_mem(h, tbase, vsize)
    if not data:
        return []
    hits = []
    for off in range(0, len(data) - 4):
        d = struct.unpack_from("<i", data, off)[0]
        if tbase + off + 4 + d == target:
            hits.append(tbase + off)
    return hits


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    targets = [int(a, 0) for a in sys.argv[1:]]
    pid, h = M.open_game()
    name, base, size = MB.find_module(pid, "th06c")
    secs = MB.sections(h, base)
    text = [s for s in secs if s[0] == ".text"]
    if not text:
        raise SystemExit(".text not found")
    _n, va, vsize, _rawsize, _rawptr, _ch = text[0]
    print("th06c.exe base=%016x  .text RVA %x vsize %x" % (base, va, vsize))

    for target in targets:
        # Accept either an absolute address or a small RVA; the latter is what
        # disasm.py prints, and forgetting to add the base silently matches
        # nothing instead of failing loudly.
        if target < base:
            target += base
        hits = refs_in_text(h, base, va, vsize, target)
        print("\n%016x (RVA %08x): %d reference(s)"
              % (target, target - base, len(hits)))
        for a in hits:
            print("  instr disp at %016x  RVA %08x" % (a, a - base))


if __name__ == "__main__":
    main()
