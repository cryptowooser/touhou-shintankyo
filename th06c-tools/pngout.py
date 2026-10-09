#!/usr/bin/env python3
"""Minimal PNG writer, so overlays can be viewed without Pillow installed.

Everything here is 24bpp BGR rows, matching what th06.capture() returns.
"""
import struct
import zlib


def write_png(path, rows):
    """rows: list of bytearray/bytes, each len == width*3, BGR, top row first."""
    h = len(rows)
    w = len(rows[0]) // 3
    raw = bytearray()
    for r in rows:
        raw.append(0)                      # filter type 0 (None)
        for x in range(w):
            o = x * 3
            raw += bytes((r[o + 2], r[o + 1], r[o]))   # BGR -> RGB

    def chunk(tag, data):
        c = tag + data
        return (struct.pack(">I", len(data)) + c
                + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    open(path, "wb").write(png)
    return w, h


def upscale(rows, k):
    out = []
    for r in rows:
        row = bytearray()
        for x in range(len(r) // 3):
            row += bytes(r[x * 3:x * 3 + 3]) * k
        for _ in range(k):
            out.append(bytearray(row))
    return out
