"""Taking pictures of the screen.

A window is captured by focusing it and asking KDE for the active window,
rather than by cropping a full screenshot to the window's geometry. Cropping
looked tidier and was wrong: with more than one output the captured image and
the compositor do not share an origin, so the crop lands somewhere else.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import windows
from .shell import DesktopError, run, which
from .windows import Window, focus_window

HELPER = Path(__file__).resolve().parent.parent / "capture-helper/target/release/kwin-capture"


def _kwin_capture(path: Path, window: Window | None) -> bool:
    """Ask KWin for the pixels directly. True if it obliged.

    Much faster than spectacle, which starts a whole Qt application each time:
    a window comes back in about 40ms against 560ms. KWin only answers
    processes it recognises, and it recognises them by executable - which is
    why this is a separate binary with its own desktop file rather than
    something this module does itself.
    """
    if not HELPER.is_file():
        return False
    try:
        if window is not None:
            focus_window(window.id)
            settle(120)
            run([str(HELPER), "window", str(path)], timeout=20.0)
        else:
            run([str(HELPER), "screen", str(path)], timeout=20.0)
    except DesktopError:
        # Not authorised, not built, or KWin said no. Falling back is better
        # than failing: spectacle is slower but always allowed.
        return False
    return path.is_file() and path.stat().st_size > 0


def screenshot(path: Path, window: Window | None = None) -> Path:
    """Capture the whole screen, or one window.

    A window is captured by focusing it and asking for the active window,
    rather than by cropping a full screenshot to its geometry. Cropping looked
    tidier and was wrong: with more than one output the captured image and the
    compositor do not share an origin, so the crop lands somewhere else.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    if window is not None and not windows.window_exists(window.id):
        raise DesktopError(f"window {window.id!r} closed before it could be captured")

    if _kwin_capture(path, window):
        return path

    if window is not None:
        focus_window(window.id)
        settle(250)
        run([which("spectacle"), "-b", "-n", "-a", "-o", str(path)], timeout=30.0)
    else:
        run([which("spectacle"), "-b", "-n", "-f", "-o", str(path)], timeout=30.0)

    for _ in range(40):
        if path.exists() and path.stat().st_size > 0:
            break
        time.sleep(0.1)
    else:
        raise DesktopError("spectacle produced no image")
    return path


def settle(milliseconds: int) -> None:
    """Give the interface time to react before looking at it.

    Every action takes one of these. Input is delivered the instant it is
    sent, but nothing on screen has moved yet, so a screenshot taken straight
    afterwards shows the state before the click rather than after it - which
    looks exactly like the click having done nothing.
    """
    if milliseconds > 0:
        time.sleep(milliseconds / 1000.0)
