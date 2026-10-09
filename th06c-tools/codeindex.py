#!/usr/bin/env python3
"""Index th06c.exe's .text once, then answer cross-reference questions fast.

Linear-sweeping .text from the start goes wrong the moment one byte is data.
Instead this walks the 4,514 function ranges from the .pdata exception
directory, so every function is disassembled from its true entry point.

Builds (and pickles) three tables:

  refs   RIP-relative memory references, keyed by target RVA
  disps  non-RIP memory operands, keyed by displacement value
  calls  direct call/jmp targets, keyed by target RVA

  codeindex.py --build
  codeindex.py --ref 0x3ED010
  codeindex.py --disp 0x17F4
  codeindex.py --calls 0x2B8560
  codeindex.py --funcs                 # functions ranked by float-op density
"""
import os
import pickle
import struct
import sys

import capstone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from disasm import load_pe, pdata_funcs, text_bytes   # noqa: E402

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codeindex.pkl")

FLOAT_OPS = {
    "addss", "subss", "mulss", "divss", "sqrtss", "comiss", "ucomiss",
    "addps", "subps", "mulps", "divps", "sqrtps", "cmpps", "andps", "andnps",
    "minss", "maxss", "rcpss", "rsqrtss", "cvtsi2ss", "cvtss2sd", "cvttss2si",
    "addsd", "subsd", "mulsd", "divsd", "comisd", "ucomisd", "sqrtsd",
}


def build():
    d, imagebase, sizeimage, secs = load_pe()
    code, tva = text_bytes(d, secs)
    funcs = pdata_funcs(d, secs)
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True

    refs = {}      # target_rva -> [(func_rva, insn_rva, text)]
    disps = {}     # disp -> [(func_rva, insn_rva, text)]
    calls = {}     # target_rva -> [(func_rva, insn_rva, text)]
    fops = {}      # func_rva -> [float_op_count, insn_count, size]
    bad = 0

    for begin, end in funcs:
        if end <= begin or begin < tva or end > tva + len(code):
            bad += 1
            continue
        chunk = code[begin - tva:end - tva]
        n = 0
        nf = 0
        for ins in md.disasm(chunk, begin):
            n += 1
            mnem = ins.mnemonic
            if mnem in FLOAT_OPS:
                nf += 1
            text = "%s %s" % (mnem, ins.op_str)
            for op in ins.operands:
                if op.type == capstone.x86.X86_OP_MEM:
                    if op.mem.base == capstone.x86.X86_REG_RIP:
                        tgt = ins.address + ins.size + op.mem.disp
                        refs.setdefault(tgt, []).append((begin, ins.address, text))
                    else:
                        disps.setdefault(op.mem.disp, []).append(
                            (begin, ins.address, text))
                elif op.type == capstone.x86.X86_OP_IMM:
                    if mnem in ("call", "jmp") and op.imm > 0:
                        calls.setdefault(op.imm, []).append(
                            (begin, ins.address, text))
        fops[begin] = (nf, n, end - begin)

    blob = {"refs": refs, "disps": disps, "calls": calls, "fops": fops,
            "funcs": funcs, "imagebase": imagebase, "tva": tva,
            "textlen": len(code)}
    pickle.dump(blob, open(CACHE, "wb"), protocol=4)
    print("functions: %d (%d skipped)" % (len(funcs), bad))
    print("  distinct RIP ref targets: %d" % len(refs))
    print("  distinct disp values:     %d" % len(disps))
    print("  distinct call targets:    %d" % len(calls))
    print("  cache -> %s (%.1f MB)" % (CACHE, os.path.getsize(CACHE) / 1e6))


def load():
    if not os.path.exists(CACHE):
        raise SystemExit("no cache; run: codeindex.py --build")
    return pickle.load(open(CACHE, "rb"))


def func_of(blob, rva):
    for b, e in blob["funcs"]:
        if b <= rva < e:
            return b, e
    return None


def show(rows, limit=40, label=""):
    if not rows:
        print("  (none)")
        return
    print("  %d hit(s)%s" % (len(rows), (" -- " + label) if label else ""))
    for f, a, t in sorted(rows)[:limit]:
        print("    func %08x  insn %08x  %s" % (f, a, t))
    if len(rows) > limit:
        print("    ... %d more" % (len(rows) - limit))


def main():
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    if args[0] == "--build":
        build()
        return
    blob = load()
    if args[0] == "--ref":
        for a in args[1:]:
            rva = int(a, 0)
            print("\nRIP-relative refs to RVA %08x" % rva)
            show(blob["refs"].get(rva, []))
    elif args[0] == "--disp":
        for a in args[1:]:
            v = int(a, 0)
            print("\nmemory operands with disp %#x" % v)
            show(blob["disps"].get(v, []))
    elif args[0] == "--calls":
        for a in args[1:]:
            rva = int(a, 0)
            print("\ndirect calls to RVA %08x" % rva)
            show(blob["calls"].get(rva, []))
    elif args[0] == "--funcs":
        rows = [(nf, n, sz, f) for f, (nf, n, sz) in blob["fops"].items()]
        rows.sort(reverse=True)
        print("functions by float-op count:")
        for nf, n, sz, f in rows[:30]:
            print("    func %08x  %5d float / %6d insn  (%d bytes)"
                  % (f, nf, n, sz))
    elif args[0] == "--stat":
        n = len(blob["fops"])
        tot = sum(v[1] for v in blob["fops"].values())
        print("%d functions, %d instructions total" % (n, tot))
        print("imagebase %016x  .text RVA %08x len %x"
              % (blob["imagebase"], blob["tva"], blob["textlen"]))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
