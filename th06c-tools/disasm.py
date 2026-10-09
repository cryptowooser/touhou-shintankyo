#!/usr/bin/env python3
"""Minimal PE reader + capstone disassembler for th06c.exe.

Works from the executable on disk, so code can be inspected without the game
running and without writing anything to a live process. Addresses are printed as
RVAs (offset from the image base), which are stable across launches; add the
runtime base to get a live address.

  disasm.py 0x29900 0x300              # disassemble an RVA range
  disasm.py --refs 0x3ed7bc            # find RIP-relative refs to an RVA
  disasm.py --secs                     # list sections
"""
import os
import struct
import sys

import capstone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gamepath  # noqa: E402


def load_pe(path=None):
    d = open(path or gamepath.require_exe(), "rb").read()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    if d[pe:pe + 4] != b"PE\0\0":
        raise SystemExit("not a PE file")
    _machine, nsec = struct.unpack_from("<HH", d, pe + 4)
    optsize = struct.unpack_from("<H", d, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", d, opt)[0]
    if magic != 0x20B:
        raise SystemExit("expected PE32+ (x64), got %#x" % magic)
    imagebase = struct.unpack_from("<Q", d, opt + 24)[0]
    sizeimage = struct.unpack_from("<I", d, opt + 56)[0]
    secs = {}
    st = opt + optsize
    for i in range(nsec):
        o = st + i * 40
        name = d[o:o + 8].rstrip(b"\0").decode("latin1")
        vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", d, o + 8)
        secs[name] = (va, vsize, rawptr, rawsize)
    return d, imagebase, sizeimage, secs


def text_bytes(d, secs):
    va, vsize, rawptr, rawsize = secs[".text"]
    return d[rawptr:rawptr + rawsize], va


def ins_ops(ins, imagebase):
    """Instruction operand text, with RIP-relative targets annotated as RVAs."""
    s = ins.op_str
    notes = []
    for op in ins.operands:
        if op.type == capstone.x86.X86_OP_MEM and op.mem.base == capstone.x86.X86_REG_RIP:
            # capstone reports the raw disp32, so the target is end-of-insn + disp.
            # We disassemble at RVAs, so this value is already an RVA.
            tgt = ins.address + ins.size + op.mem.disp
            notes.append("-> RVA %08x" % tgt)
    if notes:
        s = s + "   ; " + ", ".join(notes)
    return s


def disasm(d, secs, imagebase, rva, length, show_bytes=False):
    code, tva = text_bytes(d, secs)
    start = rva - tva
    chunk = code[start:start + length]
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    n = 0
    for ins in md.disasm(chunk, rva):
        ops = ins_ops(ins, imagebase)
        line = "  %08x  %-24s %s %s" % (ins.address, ins.mnemonic, ops, "")
        if show_bytes:
            line = "  %08x  %-20s %-8s %s %s" % (
                ins.address, ins.bytes.hex(), ins.mnemonic, ops, "")
        print(line.rstrip())
        n += 1
    print("  (%d instruction(s))" % n)


def rip_disp_offset(ins):
    """Byte offset of an instruction's RIP-relative displacement field, or None.

    The displacement is not always the last field: `cmp byte [rip+disp], 8` has a
    trailing imm8. Subtract the immediate operands to find where the disp really
    starts.
    """
    imm = sum(op.size for op in ins.operands
              if op.type == capstone.x86.X86_OP_IMM)
    for op in ins.operands:
        if op.type == capstone.x86.X86_OP_MEM and op.mem.base == capstone.x86.X86_REG_RIP:
            return ins.address + ins.size - imm - 4
    return None


def verify_ref(code, tva, md, disp_off, target_rva):
    """True if some instruction really references target_rva with its disp field
    at disp_off.

    The naive test `disp_off + 4 + disp == target` assumes the displacement is the
    last field of the instruction, so it both invents references to target-1 and
    misses real ones. Trying every alignment and checking the true disp position
    resolves both.
    """
    for start in range(disp_off - 15, disp_off + 1):
        if start < tva:
            continue
        chunk = code[start - tva:disp_off - tva + 8]
        for ins in md.disasm(chunk, start):
            if ins.address + ins.size <= disp_off:
                continue
            off = rip_disp_offset(ins)
            if off != disp_off:
                continue
            for op in ins.operands:
                if (op.type == capstone.x86.X86_OP_MEM
                        and op.mem.base == capstone.x86.X86_REG_RIP
                        and ins.address + ins.size + op.mem.disp == target_rva):
                    return True
            break
    return False


def find_refs(d, secs, imagebase, target_rva, verify=True):
    """RIP-relative references to an RVA. Addresses here are RVAs throughout,
    so the comparison is against target_rva, not imagebase + target_rva."""
    code, tva = text_bytes(d, secs)
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    out = []
    for off in range(0, len(code) - 4):
        disp = struct.unpack_from("<i", code, off)[0]
        naive = tva + off + 4 + disp
        # A trailing immediate of up to 4 bytes shifts the naive result low.
        if not (target_rva - 4 <= naive <= target_rva):
            continue
        if verify and not verify_ref(code, tva, md, tva + off, target_rva):
            continue
        out.append(tva + off)
    return out


def pdata_funcs(d, secs):
    """Function boundaries from the .pdata exception directory (RUNTIME_FUNCTION)."""
    _va, _vsize, rawptr, rawsize = secs[".pdata"]
    out = []
    for off in range(0, rawsize - 11, 12):
        b, e, _u = struct.unpack_from("<III", d, rawptr + off)
        if b:
            out.append((b, e))
    return out


def func_at(funcs, rva):
    for b, e in funcs:
        if b <= rva < e:
            return b, e
    return None


def find_callers(d, secs, target_rva):
    """Direct E8 rel32 call sites targeting an RVA."""
    code, tva = text_bytes(d, secs)
    out = []
    for off in range(len(code) - 5):
        if code[off] == 0xE8:
            rel = struct.unpack_from("<i", code, off + 1)[0]
            if tva + off + 5 + rel == target_rva:
                out.append(tva + off)
    return out


def disasm_around(d, secs, imagebase, ref_rva, back=0x40, fwd=0x20, ctx=7):
    """Show the instruction that ends at ref_rva+4 (the end of a RIP-relative
    displacement field). The displacement offset alone does not tell us where the
    instruction starts, so try each candidate start and keep the first alignment
    in which some instruction ends exactly at ref_rva+4.
    """
    code, tva = text_bytes(d, secs)
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    best = None
    for start in range(ref_rva - back, ref_rva):
        if start < tva:
            continue
        chunk = code[start - tva:ref_rva - tva + fwd]
        insns = list(md.disasm(chunk, start))
        for i, ins in enumerate(insns):
            if ins.address + ins.size == ref_rva + 4:
                # prefer the alignment that yields the fewest undecoded gaps
                if best is None or len(insns) > len(best[0]):
                    best = (insns, i)
                break
    if not best:
        print("  (no alignment found for RVA %08x)" % ref_rva)
        return
    insns, i = best
    for ins in insns[max(0, i - ctx):i + 3]:
        mark = "  <== ref here" if ins.address + ins.size == ref_rva + 4 else ""
        print("  %08x  %-10s %s%s" % (ins.address, ins.mnemonic,
                                       ins_ops(ins, imagebase), mark))


def main():
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    d, imagebase, sizeimage, secs = load_pe()
    print("imagebase=%016x sizeimage=%x" % (imagebase, sizeimage))

    if args[0] == "--secs":
        for name, (va, vsize, rawptr, rawsize) in secs.items():
            print("  %-10s RVA %08x vsize %08x raw %08x+%x"
                  % (name, va, vsize, rawptr, rawsize))
        return

    if args[0] == "--ctx":
        for a in args[1:]:
            print("\nRVA %08x context:" % int(a, 0))
            disasm_around(d, secs, imagebase, int(a, 0))
        return

    if args[0] == "--func":
        funcs = pdata_funcs(d, secs)
        print("%d function(s) in .pdata" % len(funcs))
        for a in args[1:]:
            rva = int(a, 0)
            r = func_at(funcs, rva)
            if not r:
                print("  RVA %08x: no function found" % rva)
                continue
            b, e = r
            print("  RVA %08x is inside function RVA %08x..%08x (%d bytes)"
                  % (rva, b, e, e - b))
        return

    if args[0] == "--callers":
        for a in args[1:]:
            rva = int(a, 0)
            hits = find_callers(d, secs, rva)
            print("\nRVA %08x: %d caller(s)" % (rva, len(hits)))
            funcs = pdata_funcs(d, secs)
            for h in hits:
                r = func_at(funcs, h)
                where = ("in func RVA %08x..%08x" % r) if r else "(no func)"
                print("    call at RVA %08x  %s" % (h, where))
        return

    if args[0] == "--refs":
        for a in args[1:]:
            rva = int(a, 0)
            hits = find_refs(d, secs, imagebase, rva)
            print("\nRVA %08x: %d reference(s)" % (rva, len(hits)))
            for h in hits:
                print("    disp at RVA %08x" % h)
        return

    rva = int(args[0], 0)
    length = int(args[1], 0) if len(args) > 1 else 0x100
    show_bytes = "--bytes" in args
    disasm(d, secs, imagebase, rva, length, show_bytes)


if __name__ == "__main__":
    main()
