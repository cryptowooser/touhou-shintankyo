#!/usr/bin/env python3
"""Find all code that indexes an inline array inside the game object.

`offsets.py` shows the update function only touching field +0 of each element of
the 0x110-byte array at +0x94EA. The fields that matter -- positions -- are read
by other functions. This scans all of .text for memory operands whose
displacement falls inside the array, then folds each displacement modulo the
stride to recover which intra-element offsets are actually used.

Linear disassembly can desync, so treat a single hit as a hint and confirm with
`disasm.py --ctx` before relying on it.

  arrayrefs.py 0x94ea 0x110 0x1162a
"""
import os
import sys

import capstone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disasm as D   # noqa: E402


def main():
    base = int(sys.argv[1], 0)
    stride = int(sys.argv[2], 0)
    end = int(sys.argv[3], 0)
    d, _ib, _si, secs = D.load_pe()
    code, tva = D.text_bytes(d, secs)
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True

    by_intra = {}
    regs = {}
    n = 0
    # Linear disassembly stops dead at the first byte it cannot decode, which
    # silently truncates the scan (4891 instructions instead of ~570k). Walk the
    # section in chunks and step over undecodable bytes instead.
    off = 0
    while off < len(code):
        chunk = code[off:off + 0x400]
        insns = list(md.disasm(chunk, tva + off))
        if not insns:
            off += 1
            continue
        for ins in insns:
            n += 1
            for op in ins.operands:
                if op.type != capstone.x86.X86_OP_MEM:
                    continue
                if op.mem.base == capstone.x86.X86_REG_RIP:
                    continue
                disp = op.mem.disp
                if not (base <= disp < end):
                    continue
                r = (disp - base) % stride
                k = (disp - base) // stride
                by_intra.setdefault(r, []).append((ins.address, k, ins))
                regs[r] = ins.reg_name(op.mem.base) if op.mem.base else "?"
        last = insns[-1]
        off = last.address + last.size - tva

    print("scanned %d instructions in .text; %d distinct intra-element offsets used"
          % (n, len(by_intra)))
    for r, hits in sorted(by_intra.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        ks = sorted({h[1] for h in hits})
        sample = hits[0][2]
        print("  +%#04x  x%-4d elements %d..%d (n=%d)  base=%s  e.g. %x: %s %s"
              % (r, len(hits), ks[0], ks[-1], len(ks), regs[r],
                 sample.address, sample.mnemonic, sample.op_str))


if __name__ == "__main__":
    main()
