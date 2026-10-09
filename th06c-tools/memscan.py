#!/usr/bin/env python3
"""Read-only process memory inspection for the th06c game process.

Subcommands:
  regions                 list committed readable regions
  find INT [INT ...]      locate 32-bit little-endian values
  find64 INT              locate 64-bit little-endian values
  dump ADDR LEN           hex+ascii dump
  read ADDR TYPE          read one value (i8 i16 i32 i64 f32 f64)

All addresses accept 0x-prefixed hex. Read-only: the process is opened with
PROCESS_VM_READ only, never written to.
"""
import os
import ctypes
import ctypes.wintypes as wt
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import th06  # noqa: E402  (reuse pids_named / PROC_NAME)

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000

READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}

MAX_ADDR = 0x7FFFFFFFFFFF


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


def open_game():
    pids = th06.pids_named(th06.PROC_NAME)
    if not pids:
        raise SystemExit("%s is not running" % th06.PROC_NAME)
    pid = pids[0]
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        raise SystemExit("OpenProcess failed: %d" % ctypes.get_last_error())
    return pid, h


def regions(h):
    out = []
    addr = 0
    mbi = MBI()
    while addr < MAX_ADDR:
        got = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                 ctypes.sizeof(mbi))
        if not got:
            break
        if mbi.State == MEM_COMMIT and mbi.Protect in READABLE:
            out.append((mbi.BaseAddress, mbi.RegionSize, mbi.Protect,
                        mbi.Type))
        addr = mbi.BaseAddress + mbi.RegionSize
        if mbi.RegionSize == 0:
            addr += 0x1000
    return out


def read_mem(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size,
                               ctypes.byref(n))
    if not ok:
        return None
    return buf.raw[:n.value]


def scan_values(h, targets, width=4, limit=200):
    """Find addresses holding any of `targets` as little-endian `width` ints."""
    pats = {struct.pack("<i" if width == 4 else "<q", t): t for t in targets}
    found = []
    for base, size, prot, typ in regions(h):
        data = read_mem(h, base, size)
        if not data:
            continue
        for pat, val in pats.items():
            start = 0
            while True:
                i = data.find(pat, start)
                if i < 0:
                    break
                found.append((base + i, val))
                start = i + 1
                if len(found) >= limit:
                    return found
    return found


def hexdump(data, base):
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hx = " ".join("%02x" % b for b in chunk)
        tx = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print("%016x  %-47s  %s" % (base + off, hx, tx))


FMT = {"i8": "<b", "i16": "<h", "i32": "<i", "i64": "<q",
       "f32": "<f", "f64": "<d"}


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cmd = sys.argv[1]
    pid, h = open_game()

    if cmd == "regions":
        regs = regions(h)
        total = sum(r[1] for r in regs)
        print("pid=%d  %d readable regions, %.1f MiB" % (pid, len(regs),
                                                         total / 1048576))
        for base, size, prot, typ in regs:
            print("  %016x  %10d  prot=0x%02x type=0x%08x"
                  % (base, size, prot, typ))
    elif cmd == "find":
        targets = [int(a, 0) for a in sys.argv[2:]]
        hits = scan_values(h, targets, 4)
        print("%d hit(s)" % len(hits))
        for a, v in hits:
            print("  %016x = %d" % (a, v))
    elif cmd == "find64":
        targets = [int(a, 0) for a in sys.argv[2:]]
        hits = scan_values(h, targets, 8)
        print("%d hit(s)" % len(hits))
        for a, v in hits:
            print("  %016x = %d" % (a, v))
    elif cmd == "dump":
        addr = int(sys.argv[2], 0)
        ln = int(sys.argv[3], 0)
        data = read_mem(h, addr, ln)
        if data is None:
            raise SystemExit("read failed at %x" % addr)
        hexdump(data, addr)
    elif cmd == "read":
        addr = int(sys.argv[2], 0)
        fmt = sys.argv[3]
        sz = struct.calcsize(FMT[fmt])
        data = read_mem(h, addr, sz)
        if data is None:
            raise SystemExit("read failed at %x" % addr)
        print(struct.unpack(FMT[fmt], data)[0])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
