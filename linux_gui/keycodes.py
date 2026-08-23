"""Key names to Linux keycodes.

ydotool speaks raw kernel keycodes, because there is no way for it to know
which of the world's keyboard layouts is in front of it. That is the right
call for a tool at that level and a poor one for anybody writing automation,
so this does the translation once.

The table is read from the kernel header when it is installed, which keeps it
correct across kernel versions rather than frozen at whatever was true when
this was written. The small fallback covers a machine without kernel headers.
"""

from __future__ import annotations

import re
from pathlib import Path

_HEADER = Path("/usr/include/linux/input-event-codes.h")

# Enough to type and to press the usual shortcuts on a machine with no kernel
# headers installed. Not a full table on purpose - a half-remembered long one
# is worse than a short one that says when it does not know a key.
_FALLBACK = {
    "esc": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9,
    "9": 10, "0": 11, "minus": 12, "equal": 13, "backspace": 14, "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23,
    "o": 24, "p": 25, "enter": 28, "ctrl": 29, "a": 30, "s": 31, "d": 32,
    "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38, "shift": 42,
    "backslash": 43, "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49,
    "m": 50, "comma": 51, "dot": 52, "slash": 53, "alt": 56, "space": 57,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64, "f7": 65,
    "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88, "home": 102,
    "up": 103, "pageup": 104, "left": 105, "right": 106, "end": 107,
    "down": 108, "pagedown": 109, "insert": 110, "delete": 111,
    "super": 125, "meta": 125,
}

# Spellings people actually type, mapped onto the kernel's names.
_ALIASES = {
    "ctrl": "leftctrl", "control": "leftctrl", "shift": "leftshift",
    "alt": "leftalt", "super": "leftmeta", "meta": "leftmeta",
    "win": "leftmeta", "cmd": "leftmeta", "return": "enter",
    "escape": "esc", "pgup": "pageup", "pgdn": "pagedown", "del": "delete",
    "period": "dot", "plus": "equal", "caps": "capslock",
}


def _from_header() -> dict[str, int]:
    if not _HEADER.is_file():
        return {}
    table: dict[str, int] = {}
    pattern = re.compile(r"^#define\s+KEY_([A-Z0-9_]+)\s+(\d+)")
    for line in _HEADER.read_text(errors="ignore").splitlines():
        found = pattern.match(line)
        if found:
            table.setdefault(found.group(1).lower(), int(found.group(2)))
    return table


_TABLE = _from_header() or dict(_FALLBACK)


def keycode(name: str) -> int:
    """The kernel keycode for a key name.

    Raises rather than guessing. A shortcut that silently pressed the wrong key
    would be worse than one that refused: the caller can see a name it does not
    know, but it cannot see a keystroke that went somewhere unintended.
    """
    key = name.strip().lower()
    key = _ALIASES.get(key, key)
    if key in _TABLE:
        return _TABLE[key]
    # Single characters that the header spells differently.
    if len(key) == 1 and key.isalnum() and key in _TABLE:
        return _TABLE[key]
    known = ", ".join(sorted(k for k in _TABLE if len(k) <= 5)[:12])
    raise ValueError(f"unknown key {name!r}; names look like: {known}, ...")


def chord(combination: str) -> list[int]:
    """Split "ctrl+shift+k" into the codes to press, in order.

    Released in reverse by the caller, which is what a keyboard does and what
    applications expect: a modifier released before the key it modifies reads
    as two separate presses.
    """
    parts = [part for part in combination.replace(" ", "").split("+") if part]
    if not parts:
        raise ValueError("empty key combination")
    return [keycode(part) for part in parts]
