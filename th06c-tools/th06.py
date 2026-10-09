#!/usr/bin/env python3
"""Minimal Win32 helper to inspect and drive the th06c game window.

Subcommands:
  info                 list candidate windows + geometry + focus state
  focus                bring the game window to the foreground
  shot OUT.bmp         capture the game window to a 24bpp BMP
  key KEY [KEY ...]    send keystrokes (press+release) to the game window
  hold KEY MS          hold KEY down for MS milliseconds
"""
import ctypes
import ctypes.wintypes as wt
import struct
import sys
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

# Per-monitor DPI aware so reported rects are real pixels, not scaled ones.
try:
    ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

PROC_NAME = "th06c.exe"


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


# ---------------------------------------------------------------- windows

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def all_windows():
    out = []

    def cb(hwnd, _):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        out.append((hwnd, pid.value, buf.value))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


TH32CS_SNAPPROCESS = 0x2


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(wt.ULONG)),
                ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_char * 260)]


def pids_named(name):
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return []
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
    out = []
    try:
        ok = k.Process32First(snap, ctypes.byref(pe))
        while ok:
            if pe.szExeFile.decode("ascii", "replace").lower() == name.lower():
                out.append(pe.th32ProcessID)
            ok = k.Process32Next(snap, ctypes.byref(pe))
    finally:
        k.CloseHandle(snap)
    return out


def game_windows():
    """All visible top-level windows owned by the game process."""
    pids = set(pids_named(PROC_NAME))
    out = []
    for hwnd, pid, title in all_windows():
        if pid not in pids or not user32.IsWindowVisible(hwnd):
            continue
        r = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        out.append((hwnd, pid, title, (r.right - r.left) * (r.bottom - r.top), r))
    return out


def find_game():
    """The game's main window: the largest visible one it owns."""
    wins = [w for w in game_windows() if w[3] > 10000]
    if not wins:
        raise SystemExit("no visible %s window found (game not running?)"
                         % PROC_NAME)
    hwnd, pid, title, area, r = max(wins, key=lambda w: w[3])
    return hwnd, pid, title, r


def win_info(hwnd):
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    fg = user32.GetForegroundWindow()
    style = user32.GetWindowLongW(hwnd, -16)
    exstyle = user32.GetWindowLongW(hwnd, -20)
    return {
        "hwnd": hwnd,
        "rect": (r.left, r.top, r.right, r.bottom),
        "size": (r.right - r.left, r.bottom - r.top),
        "visible": bool(user32.IsWindowVisible(hwnd)),
        "minimized": bool(user32.IsIconic(hwnd)),
        "foreground": hwnd == fg,
        "style": hex(style & 0xFFFFFFFF),
        "exstyle": hex(exstyle & 0xFFFFFFFF),
    }


def focus(hwnd):
    """Bring hwnd to the foreground.

    SetForegroundWindow alone is refused when the caller does not own the
    foreground, so attach to the current foreground thread first; that is what
    makes the call succeed.
    """
    if user32.GetForegroundWindow() == hwnd:
        return True
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fg = user32.GetForegroundWindow()
    tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tid_me = k32.GetCurrentThreadId()
    attached = False
    if tid_fg and tid_fg != tid_me:
        attached = bool(user32.AttachThreadInput(tid_me, tid_fg, True))
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(tid_me, tid_fg, False)
    time.sleep(0.25)
    return user32.GetForegroundWindow() == hwnd


# ---------------------------------------------------------------- capture


PW_RENDERFULLCONTENT = 0x00000002


def capture(hwnd, method="auto"):
    """Return (w, h, raw, stride, used, dark_ratio); raw is top-down 24bpp BGR."""
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)

    # PrintWindow(PW_RENDERFULLCONTENT) asks the window to render itself, so
    # the result does not depend on what is stacked on top of it.
    used = None
    if method in ("auto", "print"):
        if user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT):
            used = "print"
    if used is None and method in ("auto", "blt"):
        gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, r.left, r.top, 0x00CC0020)
        used = "blt"

    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h  # top-down
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 24
    bi.bmiHeader.biCompression = 0  # BI_RGB
    stride = (w * 3 + 3) & ~3
    buf = ctypes.create_string_buffer(stride * h)
    gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bi), 0)

    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(0, hdc_screen)

    raw = buf.raw
    # Cheap sanity check: a uniformly black frame means the capture failed.
    sample = raw[::997]
    dark = sum(1 for b in sample if b < 8) / max(1, len(sample))
    return w, h, raw, stride, used, dark


def write_bmp(path, w, h, raw):
    with open(path, "wb") as f:
        f.write(b"BM" + struct.pack("<IHHI", 14 + 40 + len(raw), 0, 0, 14 + 40))
        f.write(struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 24, 0, len(raw),
                            2835, 2835, 0, 0))
        f.write(raw)


def grab(hwnd, path, method="auto"):
    w, h, raw, stride, used, dark = capture(hwnd, method)
    write_bmp(path, w, h, raw)
    return w, h, used, dark


def thumb(raw, w, h, stride, tw):
    """Nearest-neighbour downscale to width tw; returns (tw, th, rows)."""
    th = max(1, int(h * tw / w))
    rows = []
    for y in range(th):
        base = min(h - 1, y * h // th) * stride
        row = bytearray()
        for x in range(tw):
            o = base + min(w - 1, x * w // tw) * 3
            row += raw[o:o + 3]
        rows.append(bytes(row))
    return tw, th, rows


def contact(path, hwnd, frames=12, interval=1.5, cols=4, tw=320):
    """Capture `frames` stills and tile them into one contact sheet."""
    shots = []
    for i in range(frames):
        w, h, raw, stride, used, dark = capture(hwnd)
        tw_, th_, rows = thumb(raw, w, h, stride, tw)
        shots.append((tw_, th_, rows))
        print("frame %2d: %dx%d dark=%.3f via %s" % (i, w, h, dark, used),
              flush=True)
        if i != frames - 1:
            time.sleep(interval)
    cw = max(s[0] for s in shots)
    ch = max(s[1] for s in shots)
    nrows = (len(shots) + cols - 1) // cols
    gap = 4
    W = cols * cw + (cols + 1) * gap
    H = nrows * ch + (nrows + 1) * gap
    stride = (W * 3 + 3) & ~3
    canvas = bytearray(b"\x30" * (stride * H))
    for i, (sw, sh, srows) in enumerate(shots):
        cx = gap + (i % cols) * (cw + gap)
        cy = gap + (i // cols) * (ch + gap)
        for y in range(sh):
            dst = (cy + y) * stride + cx * 3
            canvas[dst:dst + sw * 3] = srows[y]
    write_bmp(path, W, H, bytes(canvas))
    return W, H


# ---------------------------------------------------------------- input

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x0002, 0x0008
MAPVK_VK_TO_VSC = 0

VK = {
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "z": 0x5A, "x": 0x58, "c": 0x43, "shift": 0x10, "ctrl": 0x11,
    "enter": 0x0D, "esc": 0x1B, "space": 0x20, "tab": 0x09,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34, "5": 0x35,
}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD),
                ("wParamH", wt.WORD)]


class INPUT(ctypes.Structure):
    # The union must include MOUSEINPUT: it is the largest member (32 bytes on
    # x64), which makes sizeof(INPUT) 40. Sizing it from KEYBDINPUT alone gives
    # 32 and SendInput then fails with ERROR_INVALID_PARAMETER (87).
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT),
                    ("hi", HARDWAREINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


def _send(vk, up):
    scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=vk, wScan=scan,
                        dwFlags=KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0))
    return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def tap(key, ms=60):
    vk = VK[key]
    _send(vk, False)
    time.sleep(ms / 1000.0)
    _send(vk, True)


def hold(key, ms):
    vk = VK[key]
    _send(vk, False)
    time.sleep(ms / 1000.0)
    _send(vk, True)


def selftest():
    """Check whether SendInput from this process is delivered at all.

    Creates a window in this process, focuses it, injects a key and reports
    whether WM_KEYDOWN comes back. This isolates the injection machinery from
    anything the game does.
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hinst = k32.GetModuleHandleW(None)
    seen = []

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, ctypes.c_uint,
                                 ctypes.c_size_t, ctypes.c_ssize_t)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                    ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                    ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]

    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint,
                                      ctypes.c_size_t, ctypes.c_ssize_t]

    def wndproc(hwnd, msg, wp, lp):
        if msg == 0x0100:  # WM_KEYDOWN
            seen.append(wp)
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    print("sizeof(INPUT) = %d (expect 40 on x64)" % ctypes.sizeof(INPUT))

    cls = "JouzuInputSelfTest"
    wc = WNDCLASSW()
    wc.lpfnWndProc = WNDPROC(wndproc)
    wc.hInstance = hinst
    wc.lpszClassName = cls
    if not user32.RegisterClassW(ctypes.byref(wc)):
        err = ctypes.get_last_error()
        if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS
            raise SystemExit("RegisterClassW failed: %d" % err)

    hwnd = user32.CreateWindowExW(0, cls, "input selftest", 0x80000000,
                                  40, 40, 260, 120, None, None, hinst, None)
    if not hwnd:
        raise SystemExit("CreateWindowExW failed: %d" % ctypes.get_last_error())
    user32.ShowWindow(hwnd, 5)
    ok = focus(hwnd)
    fg = user32.GetForegroundWindow()
    print("our hwnd=%s foreground=%s focus_ok=%s"
          % (hex(hwnd), hex(fg), ok))

    rc = _send(VK["z"], False)
    print("SendInput keydown -> %d (err %d)" % (rc, ctypes.get_last_error()))
    _send(VK["z"], True)

    msg = wt.MSG()
    deadline = time.time() + 1.5
    while time.time() < deadline:
        while user32.PeekMessageW(ctypes.byref(msg), hwnd, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.02)

    print("WM_KEYDOWN vk seen: %s" % [hex(v) for v in seen])
    print("RESULT: %s" % ("SendInput IS delivered" if 0x5A in seen
                          else "SendInput NOT delivered"))
    user32.DestroyWindow(hwnd)


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cmd = sys.argv[1]

    if cmd == "selftest":
        selftest()
        return

    if cmd == "info":
        wins = game_windows()
        if not wins:
            print("no %s process/window found" % PROC_NAME)
            return
        for hwnd, pid, title, area, _ in wins:
            i = win_info(hwnd)
            print("pid=%d hwnd=%s title=%r size=%s rect=%s fg=%s vis=%s "
                  "min=%s style=%s exstyle=%s"
                  % (pid, hex(hwnd), title, i["size"], i["rect"],
                     i["foreground"], i["visible"], i["minimized"],
                     i["style"], i["exstyle"]))
        return

    hwnd, pid, title, _ = find_game()

    if cmd == "focus":
        print("focused:", focus(hwnd))
    elif cmd == "shot":
        out = sys.argv[2] if len(sys.argv) > 2 else "shot.bmp"
        method = sys.argv[3] if len(sys.argv) > 3 else "auto"
        w, h, used, dark = grab(hwnd, out, method)
        print("wrote %s (%dx%d) via %s, black_ratio=%.3f"
              % (out, w, h, used, dark))
    elif cmd == "key":
        if not focus(hwnd):
            print("warning: could not take foreground", file=sys.stderr)
        for k in sys.argv[2:]:
            tap(k)
            print("tapped", k)
    elif cmd == "contact":
        out = sys.argv[2] if len(sys.argv) > 2 else "contact.bmp"
        frames = int(sys.argv[3]) if len(sys.argv) > 3 else 12
        interval = float(sys.argv[4]) if len(sys.argv) > 4 else 1.5
        W, H = contact(out, hwnd, frames, interval)
        print("wrote %s (%dx%d)" % (out, W, H))
    elif cmd == "hold":
        if not focus(hwnd):
            print("warning: could not take foreground", file=sys.stderr)
        hold(sys.argv[2], int(sys.argv[3]))
        print("held", sys.argv[2], sys.argv[3], "ms")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
