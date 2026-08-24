"""Making keystrokes come out right on a non-US keyboard.

ydotool sends raw kernel keycodes, which name *positions* on a keyboard rather
than letters. The position that types `y` on a US layout types `z` on a German
one, so asking ydotool to type "typed" on this desktop produces "tzped".

Pasting avoids the problem entirely and is the default. This exists for the
cases where pasting is wrong - a field that rejects a paste, an application
that only reacts to real key events - and for those, the text is rewritten so
that pressing US positions produces the characters that were asked for.

The tables are deliberately partial. They cover the letter swaps and the
punctuation that actually differs, and a character with no known mapping is
sent unchanged rather than dropped: wrong is recoverable, missing is not.
"""

from __future__ import annotations

import os
from functools import cache

# Each entry maps the character wanted to the character whose *US position*
# produces it on that layout.
_LAYOUTS: dict[str, dict[str, str]] = {
    "us": {},
    # German. The famous y/z swap, plus the punctuation that moves with it.
    "de": {
        "y": "z",
        "z": "y",
        "Y": "Z",
        "Z": "Y",
        "-": "/",
        "_": "?",
        "/": "&",
        "?": "_",
        ";": ",",
        ":": ">",
        "+": "]",
        "*": "}",
        "'": "\\",
        '"': "|",
        "#": "\\",
        "<": "<",
        ">": ">",
    },
    # French AZERTY. The letter rows move, which is more than a swap.
    "fr": {
        "a": "q",
        "q": "a",
        "A": "Q",
        "Q": "A",
        "z": "w",
        "w": "z",
        "Z": "W",
        "W": "Z",
        "m": ";",
        "M": ":",
        ",": "m",
        ";": ",",
        ":": ".",
        "!": "/",
    },
}

# Layouts that are the same as US for anything this remaps.
_ALIASES = {"gb": "us", "uk": "us", "en": "us", "at": "de", "ch": "de"}


def known_layouts() -> list[str]:
    return sorted(_LAYOUTS)


@cache
def session_layout() -> str:
    """Which keyboard layout this session uses.

    Asked of the system rather than read from the environment. This server is
    started by an MCP client with almost nothing set, so `XKB_DEFAULT_LAYOUT`
    is usually absent - and its absence is indistinguishable from a US
    keyboard, which means text is typed unremapped and comes out mangled with
    no hint as to why.

    `localectl` is the authority and is always present on a systemd machine;
    `setxkbmap` is the fallback. Anything unrecognised is treated as US, which
    is what the underlying tool assumes anyway.
    """
    from .shell import DesktopError, run, which

    candidates = [os.environ.get("XKB_DEFAULT_LAYOUT", "")]
    for argv, marker in (
        (["localectl", "status"], "layout:"),
        (["setxkbmap", "-query"], "layout:"),
    ):
        try:
            reported = run([which(argv[0]), *argv[1:]], timeout=5.0)
        except (DesktopError, Exception):
            continue
        for line in reported.splitlines():
            if marker in line.lower():
                candidates.append(line.split(":", 1)[1])
                break

    for raw in candidates:
        name = (raw or "").split(",")[0].strip().lower()
        name = _ALIASES.get(name, name)
        if name in _LAYOUTS:
            return name
    return "us"


def to_us_positions(text: str, layout: str | None = None) -> str:
    """Rewrite text so US key positions produce it on the given layout.

    A character with no mapping is passed through untouched. That is right for
    every character the layouts agree on, which is most of them.
    """
    name = (layout or session_layout()).strip().lower()
    name = _ALIASES.get(name, name)
    table = _LAYOUTS.get(name)
    if not table:
        return text
    return "".join(table.get(character, character) for character in text)
