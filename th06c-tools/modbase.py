#!/usr/bin/env python3
"""Report the live base address of th06c.exe and its PE section layout.

Maps a runtime address to a section + RVA, which is what a static patch needs.

  modbase.py                  base, size, sections
  modbase.py ADDR [ADDR ...]  locate runtime addresses within the image
"""
import os
import ctypes
import ctypes.wintypes as wt
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import th06  # noqa: E402

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class MODULEENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD),
                ("th32ModuleID", wt.DWORD),
                ("th32ProcessID", wt.DWORD),
                ("GlblcntUsage", wt.DWORD),
                ("ProccntUsage", wt.DWORD),
                ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wt.DWORD),
                ("hModule", ctypes.c_void_p),
                ("szModule", ctypes.c_char * 256),
                ("szExePath", ctypes.c_char * 260)]


def open_game():
    pids = th06.pids_named(th06.PROC_NAME)
    if not pids:
        raise SystemExit("%s is not running" % th06.PROC_NAME)
    pid = pids[0]
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        raise SystemExit("OpenProcess failed: %d" % ctypes.get_last_error())
    return pid, h


def find_module(pid, prefix):
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE
                                        | TH32CS_SNAPMODULE32, pid)
    if snap == INVALID_HANDLE_VALUE:
        raise SystemExit("CreateToolhelp32Snapshot failed: %d"
                         % ctypes.get_last_error())
    me = MODULEENTRY32()
    me.dwSize = ctypes.sizeof(me)
    ok = k32.Module32First(snap, ctypes.byref(me))
    while ok:
        name = me.szModule.decode("mbcs", "replace")
        if name.lower().startswith(prefix):
            base = me.modBaseAddr or 0
            k32.CloseHandle(snap)
            return name, base, me.modBaseSize
        ok = k32.Module32Next(snap, ctypes.byref(me))
    k32.CloseHandle(snap)
    return None, 0, 0


def read_mem(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size,
                                 ctypes.byref(n)):
        return None
    return buf.raw[:n.value]


def sections(h, base):
    """Parse the PE section table out of the live image."""
    hdr = read_mem(h, base, 0x40)
    if not hdr or hdr[:2] != b"MZ":
        return []
    e_lfanew = struct.unpack_from("<I", hdr, 0x3C)[0]
    nt = read_mem(h, base + e_lfanew, 0x18)
    if not nt or nt[:4] != b"PE\0\0":
        return []
    nsec = struct.unpack_from("<H", nt, 6)[0]
    optsize = struct.unpack_from("<H", nt, 0x14)[0]
    sec = read_mem(h, base + e_lfanew + 0x18 + optsize, 40 * nsec)
    out = []
    for i in range(nsec):
        off = 40 * i
        name = sec[off:off + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", sec, off + 8)
        chars = struct.unpack_from("<I", sec, off + 36)[0]
        out.append((name, va, vsize, rawsize, rawptr, chars))
    return out


def main():
    pid, h = open_game()
    name, base, size = find_module(pid, "th06c")
    if not base:
        raise SystemExit("th06c module not found")
    print("pid=%d  %s  base=%016x  size=%d (%.1f MiB)"
          % (pid, name, base, size, size / 1048576))
    secs = sections(h, base)
    print("\n%-9s %-10s %-10s %-10s %-9s %s"
          % ("section", "RVA", "vsize", "rawsize", "rawptr", "chars"))
    for sname, va, vsize, rawsize, rawptr, chars in secs:
        flags = []
        if chars & 0x80000000:
            flags.append("W")
        if chars & 0x40000000:
            flags.append("R")
        if chars & 0x20000000:
            flags.append("X")
        print("%-9s %08x   %08x   %08x   %08x  %s"
              % (sname, va, vsize, rawsize, rawptr, "".join(flags)))

    for arg in sys.argv[1:]:
        addr = int(arg, 0)
        if not (base <= addr < base + size):
            print("\n%016x  outside image" % addr)
            continue
        rva = addr - base
        hit = None
        for sname, va, vsize, rawsize, rawptr, chars in secs:
            if va <= rva < va + max(vsize, rawsize):
                hit = (sname, va, rawptr, vsize)
                break
        if hit:
            sname, va, rawptr, vsize = hit
            print("\n%016x  RVA=%08x  section=%s+%x  (file offset %08x)"
                  % (addr, rva, sname, rva - va, rawptr + (rva - va)))
        else:
            print("\n%016x  RVA=%08x  not in any section" % (addr, rva))


if __name__ == "__main__":
    main()
