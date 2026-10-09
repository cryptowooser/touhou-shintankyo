#!/usr/bin/env python3
"""Locate the game executable on disk.

The static-analysis tools read `th06c.exe` directly instead of reading a
running process, so they need a filesystem path. This module is the only place
that decides which file that is, so the path is not duplicated across scripts.

Override it with the TH06C_DIR environment variable when the game is installed
somewhere else, or when working from a copy.
"""
import os

DEFAULT_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\th06c"
EXE_NAME = "th06c.exe"

GAME_DIR = os.environ.get("TH06C_DIR") or DEFAULT_DIR
EXE = os.path.join(GAME_DIR, EXE_NAME)


def require_exe():
    """Return the executable path, or exit with a message that says how to fix it."""
    if not os.path.isfile(EXE):
        raise SystemExit(
            "game executable not found:\n"
            "  %s\n"
            "Set TH06C_DIR to the folder that contains %s." % (EXE, EXE_NAME))
    return EXE
