#!/usr/bin/env python3
"""Read/write process memory for the th06c game process.

Subcommands:
  sizes [--private-only]         RW regions: count + total size
  find VALUE [--width N]         addresses holding VALUE
  diff --from A --to B           two-pass differential scan (see below)
       [--width N] [--key K] [--settle S]
  narrow --start V --key K       repeated-decrement scan (see below)
       [--steps N] [--width N] [--settle S]
  read ADDR [--width N] [--count C]
  write ADDR VALUE [--width N]
  refill ADDR VALUE --period S [--duration D]

`diff` reads every RW region, records every aligned address holding `--from`,
taps `--key` in the game window, waits `--settle` seconds, re-reads, and reports
the addresses that now hold `--to`. Used to pin a counter to its on-screen value
(e.g. bombs 3 -> 2 after pressing X).

Scanning covers every committed writable region regardless of type or
protection. PAGE_WRITECOPY matters here: Windows maps writable image pages
(most of th06c.exe's .data) as copy-on-write until they are first written, so
accepting only PAGE_READWRITE skips most of the game's own static state.
Pass --private-only to restrict to heap/stack when a scan is too slow.

`narrow` starts from every address holding `--start`, then taps `--key` `--steps`
times and keeps only the addresses whose value drops by exactly one each time.
A single decrement leaves too many coincidental hits; three in a row does not.
Used to pin a consumable counter (e.g. bombs 3 -> 2 -> 1 -> 0).

Addresses accept 0x-prefixed hex. Widths are 1, 2, 4 or 8 bytes, little-endian.
Writing is limited to RW regions.
"""
import os
import ctypes
import ctypes.wintypes as wt
import struct
import sys
import time
from array import array

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import th06  # noqa: E402

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000

RW_PROTECT = {0x04, 0x08, 0x40, 0x80}   # READWRITE, WRITECOPY, EXEC_* variants
MAX_ADDR = 0x7FFFFFFFFFFF

WIDTH_FMT = {1: "b", 2: "h", 4: "i", 8: "q"}


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_ulonglong),
                ("AllocationBase", ctypes.c_ulonglong),
                ("AllocationProtect", wt.DWORD),
                ("__a1", wt.DWORD),
                ("RegionSize", ctypes.c_ulonglong),
                ("State", wt.DWORD),
                ("Protect", wt.DWORD),
                ("Type", wt.DWORD),
                ("__a2", wt.DWORD)]


def open_game(write=False):
    pids = th06.pids_named(th06.PROC_NAME)
    if not pids:
        raise SystemExit("%s is not running" % th06.PROC_NAME)
    pid = pids[0]
    access = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
    if write:
        access |= PROCESS_VM_WRITE | PROCESS_VM_OPERATION
    h = k32.OpenProcess(access, False, pid)
    if not h:
        raise SystemExit("OpenProcess failed: %d" % ctypes.get_last_error())
    return pid, h


def regions(h, rw=False, private_only=False):
    out = []
    addr = 0
    mbi = MBI()
    while addr < MAX_ADDR:
        got = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                 ctypes.sizeof(mbi))
        if not got:
            break
        if mbi.State == MEM_COMMIT:
            if rw:
                if mbi.Protect in RW_PROTECT and (not private_only
                                                  or mbi.Type == MEM_PRIVATE):
                    out.append((mbi.BaseAddress, mbi.RegionSize, mbi.Protect,
                                mbi.Type))
            elif mbi.Protect in {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}:
                out.append((mbi.BaseAddress, mbi.RegionSize, mbi.Protect,
                            mbi.Type))
        addr = mbi.BaseAddress + mbi.RegionSize
        if mbi.RegionSize == 0:
            addr += 0x1000
    return out


def rw_regions(h):
    """RW regions, honouring --private-only from the command line."""
    return regions(h, rw=True, private_only="--private-only" in sys.argv)


def read_mem(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size,
                               ctypes.byref(n))
    if not ok:
        return None
    return buf.raw[:n.value]


def write_mem(h, addr, data):
    """WriteProcessMemory, raising on failure.

    This used to return a bool that callers ignored, which let a handle opened
    without PROCESS_VM_WRITE swallow every write silently. Failing loudly is the
    whole point.
    """
    n = ctypes.c_size_t(0)
    ok = k32.WriteProcessMemory(h, ctypes.c_void_p(addr), data, len(data),
                                ctypes.byref(n))
    if not ok:
        raise OSError(
            "WriteProcessMemory failed at %016x (%d bytes, err %d) - the process "
            "handle may lack PROCESS_VM_WRITE; open with open_game(write=True)"
            % (addr, len(data), ctypes.get_last_error()))
    return True


def read_int(h, addr, width):
    data = read_mem(h, addr, width)
    if data is None or len(data) < width:
        return None
    return struct.unpack("<" + WIDTH_FMT[width], data)[0]


def pack_int(value, width):
    return struct.pack("<" + WIDTH_FMT[width], value)


def hexdump(data, base):
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hx = " ".join("%02x" % b for b in chunk)
        tx = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print("%016x  %-47s  %s" % (base + off, hx, tx))


def argval(flag, default=None):
    if flag in sys.argv:
        return sys.argv[sys.argv.index(flag) + 1]
    return default


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cmd = sys.argv[1]
    width = int(argval("--width", "4"))

    if cmd == "sizes":
        pid, h = open_game()
        regs = rw_regions(h)
        total = sum(r[1] for r in regs)
        print("pid=%d  %d RW regions, %.1f MiB%s"
              % (pid, len(regs), total / 1048576,
                 "  (--private-only)" if "--private-only" in sys.argv else ""))
        by_type = {}
        for _b, size, _p, typ in regs:
            by_type[typ] = by_type.get(typ, 0) + size
        for typ, sz in sorted(by_type.items(), key=lambda kv: -kv[1]):
            print("  type 0x%08x: %.1f MiB" % (typ, sz / 1048576))
        regs.sort(key=lambda r: -r[1])
        for base, size, prot, typ in regs[:10]:
            print("  %016x %10d prot=0x%02x type=0x%08x"
                  % (base, size, prot, typ))
        return

    if cmd == "find":
        value = int(sys.argv[2], 0)
        pid, h = open_game()
        pat = pack_int(value, width)
        total = 0
        for base, size, prot, typ in rw_regions(h):
            data = read_mem(h, base, size)
            if not data:
                continue
            i = data.find(pat)
            while i >= 0:
                if i % width == 0:
                    print("  %016x" % (base + i))
                    total += 1
                i = data.find(pat, i + 1)
        print("%d hit(s)" % total)
        return

    if cmd == "diff":
        frm = int(argval("--from"), 0)
        to = int(argval("--to"), 0)
        key = argval("--key")
        settle = float(argval("--settle", "1.5"))
        pid, h = open_game()
        pat = pack_int(frm, width)

        cand = []
        n_before = 0
        for base, size, prot, typ in rw_regions(h):
            data = read_mem(h, base, size)
            if not data:
                continue
            offs = array("Q")
            i = data.find(pat)
            while i >= 0:
                if i % width == 0:
                    offs.append(i)
                i = data.find(pat, i + 1)
            if offs:
                cand.append((base, size, offs))
                n_before += len(offs)
        print("pass A: %d address(es) holding %d (width %d)"
              % (n_before, frm, width))

        if key:
            hwnd = th06.find_game()[0]
            th06.focus(hwnd)
            th06.tap(key)
            print("tapped %r, settling %.1fs" % (key, settle))
        else:
            print("no --key given, settling %.1fs" % settle)
        time.sleep(settle)

        survivors = []
        for base, size, offs in cand:
            data = read_mem(h, base, size)
            if not data:
                continue
            for off in offs:
                if off + width > len(data):
                    continue
                v = struct.unpack_from("<" + WIDTH_FMT[width], data, off)[0]
                if v == to:
                    survivors.append(base + off)
        print("pass B: %d address(es) now hold %d" % (len(survivors), to))
        for a in survivors[:200]:
            print("  %016x" % a)
        if len(survivors) > 200:
            print("  ... %d more" % (len(survivors) - 200))
        return

    if cmd == "narrow":
        start_v = int(argval("--start"), 0)
        key = argval("--key")
        steps = int(argval("--steps", "3"))
        settle = float(argval("--settle", "1.0"))
        pid, h = open_game()
        pat = pack_int(start_v, width)

        cand = []
        n0 = 0
        for base, size, prot, typ in rw_regions(h):
            data = read_mem(h, base, size)
            if not data:
                continue
            offs = array("Q")
            i = data.find(pat)
            while i >= 0:
                if i % width == 0:
                    offs.append(i)
                i = data.find(pat, i + 1)
            if offs:
                cand.append([base, size, offs])
                n0 += len(offs)
        print("step 0: %d address(es) hold %d (width %d)"
              % (n0, start_v, width))

        hwnd = th06.find_game()[0]
        for step in range(1, steps + 1):
            th06.focus(hwnd)
            th06.tap(key)
            time.sleep(settle)
            want = start_v - step
            kept = []
            total = 0
            for base, size, offs in cand:
                data = read_mem(h, base, size)
                if not data:
                    continue
                keep = array("Q")
                for off in offs:
                    if off + width > len(data):
                        continue
                    v = struct.unpack_from("<" + WIDTH_FMT[width], data,
                                           off)[0]
                    if v == want:
                        keep.append(off)
                if keep:
                    kept.append([base, size, keep])
                    total += len(keep)
            cand = kept
            print("step %d: tapped %r, %d address(es) now hold %d"
                  % (step, key, total, want))
            if not total:
                break

        print("survivors:")
        for base, size, offs in cand:
            for off in offs:
                print("  %016x" % (base + off))
        return

    if cmd == "read":
        addr = int(sys.argv[2], 0)
        count = int(argval("--count", "1"))
        pid, h = open_game()
        for i in range(count):
            a = addr + i * width
            print("  %016x = %s" % (a, read_int(h, a, width)))
        return

    if cmd == "write":
        addr = int(sys.argv[2], 0)
        value = int(sys.argv[3], 0)
        pid, h = open_game(write=True)
        ok = write_mem(h, addr, pack_int(value, width))
        print("write %d (width %d) to %016x -> %s" % (value, width, addr,
                                                      "ok" if ok else "FAILED"))
        print("  readback: %s" % read_int(h, addr, width))
        return

    if cmd == "refill":
        addr = int(sys.argv[2], 0)
        value = int(sys.argv[3], 0)
        period = float(argval("--period", "3"))
        duration = float(argval("--duration", "0"))
        pid, h = open_game(write=True)
        start = time.time()
        n = 0
        try:
            while True:
                ok = write_mem(h, addr, pack_int(value, width))
                n += 1
                print("[%6.1fs] wrote %d to %016x -> %s (readback %s)"
                      % (time.time() - start, value, addr,
                         "ok" if ok else "FAILED", read_int(h, addr, width)),
                      flush=True)
                if duration and time.time() - start >= duration:
                    break
                time.sleep(period)
        except KeyboardInterrupt:
            pass
        print("stopped after %d write(s)" % n)
        return

    raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
