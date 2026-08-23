"""Sending input, through the kernel's uinput device.

ydotool writes events as though a physical keyboard and mouse had produced
them, so they reach native Wayland windows. X tools cannot: without an X
server in the path there is nothing for them to inject into."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from functools import cache
from pathlib import Path

from .keycodes import chord
from .shell import DesktopError, run, session_env, which

YDOTOOL_SOCKET = os.environ.get(
    "YDOTOOL_SOCKET", f"/run/user/{os.getuid()}/.ydotool_socket"
)


# --------------------------------------------------------------------------
# input


def ensure_input_daemon() -> None:
    """Start ydotoold if it is not already listening.

    Started here rather than left to the user because a missing daemon is the
    single most likely reason for this to appear broken, and the symptom -
    every action silently doing nothing - points nowhere near the cause.
    """
    if Path(YDOTOOL_SOCKET).exists():
        return
    if not shutil.which("ydotoold"):
        raise DesktopError(
            "ydotoold is not installed; without it no input can be sent on Wayland"
        )
    subprocess.Popen(
        ["ydotoold", f"--socket-path={YDOTOOL_SOCKET}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(50):
        if Path(YDOTOOL_SOCKET).exists():
            return
        time.sleep(0.1)
    raise DesktopError(
        "ydotoold did not create its socket; check that /dev/uinput is writable"
    )


def _ydotool(args: list[str]) -> None:
    ensure_input_daemon()
    env = {**session_env(), "YDOTOOL_SOCKET": YDOTOOL_SOCKET}
    done = subprocess.run(
        ["ydotool", *args], capture_output=True, text=True, env=env, check=False
    )
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip() or f"exit {done.returncode}"
        raise DesktopError(f"ydotool {args[0]} failed: {detail}")


def move_mouse(x: int, y: int) -> None:
    """Put the pointer at an absolute position on the desktop.

    Done as a slam into the top-left corner followed by a relative move,
    rather than with ydotool's own `--absolute`. That mode maps onto the
    virtual device's coordinate space and is affected by pointer
    acceleration - ydotool's own help says as much - so on a multi-monitor
    desktop it lands somewhere near the target rather than on it.

    A large negative relative move is clamped by the compositor at the
    corner, which gives a known origin no matter where the pointer started.
    From there the move is exact.
    """
    scale_x, scale_y = _pointer_scale()
    _home_pointer()
    if x or y:
        _ydotool(
            ["mousemove", "-x", str(round(x / scale_x)), "-y", str(round(y / scale_y))]
        )


def _home_pointer() -> None:
    """Slam the pointer into the top-left corner.

    A large negative relative move is clamped by the compositor at the corner,
    which gives a known origin no matter where the pointer started.
    """
    _ydotool(["mousemove", "-x", "-20000", "-y", "-20000"])


def _read_pointer() -> tuple[int, int]:
    reported = run([which("kdotool"), "getmouselocation", "--shell"])
    values = dict(
        line.split("=", 1) for line in reported.strip().splitlines() if "=" in line
    )
    return int(values.get("X", 0)), int(values.get("Y", 0))


@cache
def _pointer_scale() -> tuple[float, float]:
    """How far the pointer really travels per unit asked for.

    Pointer acceleration means a relative move of 1000 does not move the
    pointer 1000 pixels - on this desktop it moves about twice that. The
    factor depends on the user's acceleration settings, so it is measured
    once rather than guessed, by moving a known distance and reading back
    where the pointer actually ended up.

    Falls back to 1.0 if the position cannot be read, which leaves behaviour
    no worse than not calibrating at all.
    """
    probe_x, probe_y = 500, 300
    try:
        _home_pointer()
        _ydotool(["mousemove", "-x", str(probe_x), "-y", str(probe_y)])
        landed_x, landed_y = _read_pointer()
    except DesktopError:
        return 1.0, 1.0
    # A zero reading means the position could not be read, not that the
    # pointer did not move; scaling by it would divide by zero.
    scale_x = landed_x / probe_x if landed_x else 1.0
    scale_y = landed_y / probe_y if landed_y else 1.0
    return scale_x or 1.0, scale_y or 1.0


_BUTTONS = {"left": 0x00, "right": 0x01, "middle": 0x02}


def click(button: str = "left", count: int = 1) -> None:
    code = _BUTTONS.get(button.lower())
    if code is None:
        raise DesktopError(f"unknown button {button!r}; use left, right or middle")
    # 0x40 is press, 0x80 release; together they are one click.
    for _ in range(max(1, count)):
        _ydotool(["click", f"0x{0x40 | code:02X}", f"0x{0x80 | code:02X}"])


def drag(from_x: int, from_y: int, to_x: int, to_y: int, button: str = "left") -> None:
    code = _BUTTONS.get(button.lower())
    if code is None:
        raise DesktopError(f"unknown button {button!r}; use left, right or middle")
    move_mouse(from_x, from_y)
    _ydotool(["click", f"0x{0x40 | code:02X}"])
    # Moved in steps rather than one jump: a drag that teleports is often read
    # as a click by anything watching for motion, and drag-and-drop targets in
    # particular need to see the pointer arrive.
    steps = 12
    for step in range(1, steps + 1):
        move_mouse(
            from_x + (to_x - from_x) * step // steps,
            from_y + (to_y - from_y) * step // steps,
        )
        time.sleep(0.02)
    _ydotool(["click", f"0x{0x80 | code:02X}"])


def scroll(amount: int) -> None:
    """Positive scrolls down, negative up - the direction a wheel turns."""
    _ydotool(["mousemove", "--wheel", "-x", "0", "-y", str(int(amount))])


def type_text(
    text: str,
    method: str = "auto",
    layout: str | None = None,
    delay_ms: int = 12,
) -> None:
    """Enter text.

    `method` picks how:

    - `auto` pastes when a clipboard tool is available, otherwise types.
    - `paste` puts the text on the clipboard and presses ctrl+v. Layout-proof
      and fast, but some fields refuse a paste, and an application watching for
      key events sees none.
    - `keystrokes` sends real key events. Subject to the keyboard layout, so
      the text is rewritten first for `layout` - which defaults to whatever
      `XKB_DEFAULT_LAYOUT` says this session uses.

    The previous clipboard contents are restored after a paste. A tool that
    silently ate the clipboard would be a small betrayal every time it ran.
    """
    method = (method or "auto").strip().lower()
    if method not in ("auto", "paste", "keystrokes"):
        raise DesktopError(
            f"unknown method {method!r}; use auto, paste or keystrokes"
        )

    if method == "keystrokes":
        _type_keystrokes(text, layout, delay_ms)
        return

    try:
        clipboard = which("wl-copy")
    except DesktopError:
        if method == "paste":
            raise DesktopError(
                "pasting needs wl-clipboard installed; use method='keystrokes'"
            ) from None
        _type_keystrokes(text, layout, delay_ms)
        return

    previous = None
    try:
        previous = run([which("wl-paste"), "--no-newline"], timeout=5.0)
    except DesktopError:
        # An empty clipboard makes wl-paste exit non-zero. Nothing to restore.
        pass

    subprocess.run(
        [clipboard, "--", text], check=False, env=session_env(), capture_output=True
    )
    press("ctrl+v")
    if previous:
        time.sleep(0.2)
        subprocess.run(
            [clipboard, "--", previous],
            check=False,
            env=session_env(),
            capture_output=True,
        )


def _type_keystrokes(text: str, layout: str | None, delay_ms: int) -> None:
    """Send real key events, rewritten for the keyboard layout in use."""
    from . import layouts

    remapped = layouts.to_us_positions(text, layout)
    _ydotool(["type", "--key-delay", str(int(delay_ms)), "--", remapped])


def press(combination: str) -> None:
    """Press a chord such as "ctrl+shift+k" and let it go again."""
    codes = chord(combination)
    downs = [f"{code}:1" for code in codes]
    ups = [f"{code}:0" for code in reversed(codes)]
    _ydotool(["key", *downs, *ups])
