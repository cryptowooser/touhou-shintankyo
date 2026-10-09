#!/usr/bin/env python3
"""Histogram the game-object field offsets referenced by a function.

The main game object (`this`) arrives in rcx and is copied to rdi. Every field
access therefore appears as `[rdi + disp]` or `[rdi + idx*scale + disp]`. Two
kinds of field are interesting:

  * a plain `[rdi + disp]` holding a pointer that is then dereferenced -- the
    head of a linked list or an array;
  * a `[rdi + idx*scale + disp]` access -- an inline array, where `scale` gives
    the element size.

Listing the displacements a function uses shows the object's layout without any
guessing about what each field means.

  offsets.py [start_rva] [end_rva]
"""
import os
import sys

import capstone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disasm as D   # noqa: E402

DEFAULT_START = 0x2CCF0
DEFAULT_END = 0x364A4


def collect(rva_start, rva_end, reg="rdi"):
    d, imagebase, _sizeimage, secs = D.load_pe()
    code, tva = D.text_bytes(d, secs)
    chunk = code[rva_start - tva:rva_end - tva]
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    want = getattr(capstone.x86, "X86_REG_" + reg.upper())
    plain = {}
    indexed = {}
    for ins in md.disasm(chunk, rva_start):
        for op in ins.operands:
            if op.type != capstone.x86.X86_OP_MEM or op.mem.base != want:
                continue
            disp = op.mem.disp
            if op.mem.index == 0:
                plain.setdefault(disp, []).append((ins.address, ins.mnemonic))
            else:
                indexed.setdefault((disp, op.mem.scale), []).append(
                    (ins.address, ins.mnemonic))
    return plain, indexed


def main():
    a = int(sys.argv[1], 0) if len(sys.argv) > 1 else DEFAULT_START
    b = int(sys.argv[2], 0) if len(sys.argv) > 2 else DEFAULT_END
    plain, indexed = collect(a, b)
    print("plain [rdi + disp] accesses in %#x..%#x: %d distinct offsets"
          % (a, b, len(plain)))
    for disp, uses in sorted(plain.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        print("  +%#06x  x%-4d  %s" % (disp, len(uses),
                                       " ".join("%x" % u[0] for u in uses[:6])))
    print("\nindexed [rdi + idx*scale + disp] accesses: %d distinct"
          % len(indexed))
    for (disp, scale), uses in sorted(indexed.items(),
                                      key=lambda kv: (-len(kv[1]), kv[0])):
        print("  +%#06x  scale=%d  x%-4d  %s"
              % (disp, scale, len(uses), " ".join("%x" % u[0] for u in uses[:6])))


if __name__ == "__main__":
    main()
