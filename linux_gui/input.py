"""Sending input, through the kernel's uinput device.

ydotool writes events as though a physical keyboard and mouse had produced
them, so they reach native Wayland windows. X tools cannot: without an X
server in the path there is nothing for them to inject into."""

from __future__ import annotations

import contextlib
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


def _ydotool(args: list[str], timeout: float = 30.0) -> None:
    ensure_input_daemon()
    env = {**session_env(), "YDOTOOL_SOCKET": YDOTOOL_SOCKET}
    try:
        done = subprocess.run(
            ["ydotool", *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as expired:
        raise DesktopError(f"ydotool {args[0]} timed out after {timeout}s") from expired
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip() or f"exit {done.returncode}"
        raise DesktopError(f"ydotool {args[0]} failed: {detail}")


#: How far off a pointer move may land before it is corrected, in pixels.
#:
#: One pixel of slack: the compositor rounds, and chasing the last pixel
#: would cost a round trip to gain nothing a click can tell apart.
POINTER_TOLERANCE = 2

#: How many corrections to attempt before giving up and reporting where it is.
#:
#: Enough for the estimate to double its way back from a badly wrong start:
#: each attempt costs about ten milliseconds, and three was not enough to
#: climb out of a corner.
POINTER_ATTEMPTS = 8

#: The range of pixels-per-unit worth believing.
#:
#: Measured on this desktop the figure sits near 2.0 and drifts to about 2.3
#: over a long move, because acceleration is a function of speed. Anything
#: outside these bounds did not come from a clean measurement - the usual
#: cause is an axis that hit the edge of the screen, which reports less
#: movement than was delivered and so looks like a tiny scale. Dividing the
#: next correction by a tiny scale is what sent the pointer into the corner.
POINTER_SCALE_BOUNDS = (0.25, 8.0)


@cache
def _screen_extent() -> tuple[int, int]:
    """The largest coordinate the pointer can reach, per axis.

    Found by asking for far more movement than the screen has and reading
    where it stopped - the compositor clamps, so what comes back is the edge.
    That is the only thing here that needs to know the screen size, and no
    tool in this stack will report it directly.

    Cached: it costs a pointer move, and a desktop does not change size
    mid-session often enough to pay that on every click.
    """
    try:
        _ydotool(["mousemove", "-x", "20000", "-y", "20000"])
        time.sleep(POINTER_SETTLE)
        return _read_pointer()
    except DesktopError:
        # Unknown, so nothing is ever treated as clamped. That is the old
        # behaviour, which is wrong but no worse than it was.
        return (10**9, 10**9)


#: Time for the compositor to apply a pointer move before it is read back.
POINTER_SETTLE = 0.02


def move_mouse(x: int, y: int) -> tuple[int, int]:
    """Put the pointer at an absolute position, and confirm it got there.

    Returns where it actually landed, which is not always where it was sent:
    ydotool speaks in relative movements scaled by the user's pointer
    acceleration, so an absolute position is an estimate until it is read
    back.

    Done as a slam into the top-left corner followed by a relative move,
    rather than with ydotool's own `--absolute`. That mode maps onto the
    virtual device's coordinate space and is affected by acceleration -
    ydotool's own help says as much - so on a multi-monitor desktop it lands
    somewhere near the target rather than on it.

    The estimate is then corrected against a reading. Crucially the
    correction is scaled by what the move that just happened actually
    delivered, not by the stored factor: correcting with a wrong factor
    overshoots by the same proportion every time, which oscillates instead of
    converging. Measuring per move is also what makes a stale factor
    harmless - it used to fall back to 1.0 after one failed calibration and
    stay cached for the life of the server, sending every click to roughly
    double its intended offset with nothing raised.
    """
    extent = _screen_extent()
    scale_x, scale_y = _pointer_scale()
    units_x, units_y = _units(x, scale_x, extent[0]), _units(y, scale_y, extent[1])

    _home_pointer()
    if units_x or units_y:
        _ydotool(["mousemove", "-x", str(units_x), "-y", str(units_y)])
    time.sleep(POINTER_SETTLE)

    # The pointer started at the corner, so where it is now is what those
    # units delivered.
    origin = (0, 0)
    sent = (units_x, units_y)

    landed = (x, y)
    for _ in range(POINTER_ATTEMPTS):
        try:
            landed = _read_pointer()
        except DesktopError:
            # Position cannot be read at all; the open-loop estimate is the
            # best on offer, and saying so is the caller's business.
            return x, y

        error = (x - landed[0], y - landed[1])
        if abs(error[0]) <= POINTER_TOLERANCE and abs(error[1]) <= POINTER_TOLERANCE:
            return landed

        # An axis that ran into the edge of the screen travelled less than it
        # was told to, so a scale derived from it comes out far too small -
        # and dividing the next correction by that is what threw the pointer
        # into the corner. But refusing to learn from it is no better: the
        # estimate then never changes and the same overshoot repeats, which
        # is an oscillation from one corner to the other.
        #
        # Hitting the edge is itself the measurement. It says the move was
        # too long, so the pointer covers more ground per unit than assumed,
        # so the estimate goes up. Doubling reaches any true value in a few
        # steps from anywhere, which is what makes this recover rather than
        # merely fail differently.
        moved = (landed[0] - origin[0], landed[1] - origin[1])
        scale_x = (
            min(scale_x * 2, POINTER_SCALE_BOUNDS[1])
            if _clamped(landed[0], origin[0], extent[0])
            else _observed_scale(moved[0], sent[0], scale_x)
        )
        scale_y = (
            min(scale_y * 2, POINTER_SCALE_BOUNDS[1])
            if _clamped(landed[1], origin[1], extent[1])
            else _observed_scale(moved[1], sent[1], scale_y)
        )

        sent = (
            _units(error[0], scale_x, extent[0]),
            _units(error[1], scale_y, extent[1]),
        )
        if not sent[0] and not sent[1]:
            # Closer than one unit of the device can express.
            return landed

        origin = landed
        try:
            _ydotool(["mousemove", "-x", str(sent[0]), "-y", str(sent[1])])
            time.sleep(POINTER_SETTLE)
        except DesktopError:
            return landed

    return landed


def _units(pixels: int, scale: float, extent: int) -> int:
    """Device units for a distance in pixels, never more than one screen.

    The cap is what stops a single bad scale from being able to throw the
    pointer off the desktop: no correct move is ever longer than the screen,
    so one that would be is wrong by definition and clamping it keeps the
    loop somewhere it can still measure from.
    """
    asked = round(pixels / scale)
    limit = round(extent / POINTER_SCALE_BOUNDS[0])
    return max(-limit, min(limit, asked))


#: How close to an edge still counts as being against it, in pixels.
#:
#: The pointer does not rest at zero - the compositor stops it at 1 - so a
#: test for `<= 0` never fires and a move clamped against the left or top
#: edge was read as a clean one. That is not a detail: the scale learned from
#: it was wrong in the direction that makes the next move overshoot too.
POINTER_EDGE_MARGIN = 2


def _clamped(landed: int, origin: int, extent: int) -> bool:
    """Whether an axis stopped because it ran out of screen.

    Only counts when it also tried to move: resting against the edge is not
    the same as being stopped by it.
    """
    if landed == origin:
        return False
    return landed <= POINTER_EDGE_MARGIN or landed >= extent - POINTER_EDGE_MARGIN


def _observed_scale(moved: int, sent: int, fallback: float) -> float:
    """Pixels per unit, as the last move actually delivered them.

    Too small a movement says nothing useful - rounding dominates - so the
    previous figure is kept rather than replaced with noise.
    """
    if abs(sent) < 5 or moved == 0:
        return fallback
    observed = moved / sent
    low, high = POINTER_SCALE_BOUNDS
    if not low <= observed <= high:
        # Outside anything a real pointer does, so the measurement is not
        # about the pointer - a clamped axis or a missed reading.
        return fallback
    return observed


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


def read_pointer() -> tuple[int, int]:
    """Return the current absolute screen pointer coordinates (x, y)."""
    return _read_pointer()


def _pointer_scale() -> tuple[float, float]:
    """How far the pointer really travels per unit asked for.

    Pointer acceleration means a relative move of 1000 does not move the
    pointer 1000 pixels - on this desktop it moves about twice that. The
    factor depends on the user's acceleration settings, so it is measured
    once rather than guessed.

    Only ever a starting estimate: `move_mouse` reads the position back and
    corrects. A wrong answer here costs an extra correction, where it used to
    cost every click for the life of the process.

    A plausible measurement is remembered, because it was costing three
    round trips on every single move to re-derive a figure that does not
    change. An implausible one is not remembered - that is the failure this
    docstring has warned about since before it was true, where one bad
    calibration is cached and every click for the rest of the session goes
    to the wrong place.

    Deliberately not `@cache`, which is what it used to be: that remembers
    whatever came back, including the nonsense, and there is no way to clear
    it from a check that wants to see the recovery path work.
    """
    global _CACHED_SCALE
    if _CACHED_SCALE is not None:
        return _CACHED_SCALE

    probe_x, probe_y = 500, 300
    try:
        _home_pointer()
        time.sleep(POINTER_SETTLE)
        _ydotool(["mousemove", "-x", str(probe_x), "-y", str(probe_y)])
        time.sleep(POINTER_SETTLE)
        landed_x, landed_y = _read_pointer()
    except DesktopError:
        return 1.0, 1.0

    low, high = POINTER_SCALE_BOUNDS
    scale_x = landed_x / probe_x if landed_x else 1.0
    scale_y = landed_y / probe_y if landed_y else 1.0
    if low <= scale_x <= high and low <= scale_y <= high:
        _CACHED_SCALE = (scale_x, scale_y)
        return _CACHED_SCALE
    # Unbelievable, so hand back a neutral estimate and let the closed loop
    # find the truth rather than baking the nonsense in.
    return 1.0, 1.0


#: A believed pointer scale, once one has been measured.
_CACHED_SCALE: tuple[float, float] | None = None


_BUTTONS = {"left": 0x00, "right": 0x01, "middle": 0x02}


#: Pause between arriving somewhere and pressing there, in seconds.
#:
#: Moving the pointer into a different window makes the compositor send that
#: window a pointer-enter, and a press that arrives before the window has
#: taken it is delivered against the previous focus - so the click lands
#: somewhere else, or nowhere, with nothing to show it went wrong.
CLICK_SETTLE = 0.06

#: How long the button is held down, in seconds.
#:
#: A press and release in the same input batch is a zero-length click, and a
#: toolkit that decides what was clicked on its own frame - GPUI does - can
#: see both edges between two frames and act on neither. Holding for longer
#: than a frame at 60Hz puts the press and the release in different frames,
#: which is what a real click looks like.
CLICK_HOLD = 0.03


def click(button: str = "left", count: int = 1) -> None:
    code = _BUTTONS.get(button.lower())
    if code is None:
        raise DesktopError(f"unknown button {button!r}; use left, right or middle")
    time.sleep(CLICK_SETTLE)
    for index in range(max(1, count)):
        if index:
            time.sleep(CLICK_HOLD)
        # 0x40 is press, 0x80 release; separately, so the button is held.
        _ydotool(["click", f"0x{0x40 | code:02X}"])
        time.sleep(CLICK_HOLD)
        _ydotool(["click", f"0x{0x80 | code:02X}"])


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
    timeout: float = 30.0,
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
        raise DesktopError(f"unknown method {method!r}; use auto, paste or keystrokes")

    if method == "keystrokes":
        _type_keystrokes(text, layout, delay_ms, timeout=timeout)
        return

    try:
        clipboard = which("wl-copy")
    except DesktopError:
        if method == "paste":
            raise DesktopError(
                "pasting needs wl-clipboard installed; use method='keystrokes'"
            ) from None
        _type_keystrokes(text, layout, delay_ms, timeout=timeout)
        return

    try:
        previous = None
        # An empty clipboard makes wl-paste exit non-zero. Nothing to restore.
        with contextlib.suppress(Exception):
            previous = run(
                [which("wl-paste"), "--no-newline"], timeout=min(2.0, timeout)
            )

        # Detached rather than waited on: wl-copy stays running to serve the
        # clipboard until something else claims it, so waiting would hang.
        subprocess.Popen(
            [clipboard, "--", text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=session_env(),
            start_new_session=True,
        )
        _await_clipboard(text, timeout=min(2.0, timeout))
        press("ctrl+v", timeout=timeout)
        if previous:
            time.sleep(0.1)
            subprocess.Popen(
                [clipboard, "--", previous],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                env=session_env(),
                start_new_session=True,
            )
    except (DesktopError, OSError, subprocess.SubprocessError):
        _type_keystrokes(text, layout, delay_ms, timeout=timeout)


def _await_clipboard(wanted: str, timeout: float = 2.0) -> None:
    """Wait until the clipboard really holds this, rather than assuming.

    Becoming the clipboard owner is not instant, and a fixed sleep is a guess
    at how long it takes. Guess short and ctrl+v pastes whatever was there
    before - which looks like the text having been typed wrongly rather than
    like a race. Reading it back is the only way to know.

    Gives up quietly on timeout: pasting something is still better than
    refusing to type at all, and the caller will see the result.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            if run([which("wl-paste"), "--no-newline"], timeout=1.0) == wanted:
                return
        time.sleep(0.02)


def _type_keystrokes(
    text: str, layout: str | None, delay_ms: int, timeout: float = 30.0
) -> None:
    """Send real key events, rewritten for the keyboard layout in use."""
    from . import layouts

    remapped = layouts.to_us_positions(text, layout)
    _ydotool(
        ["type", "--key-delay", str(int(delay_ms)), "--", remapped], timeout=timeout
    )


def press(combination: str, timeout: float = 30.0) -> None:
    """Press a chord such as "ctrl+shift+k" and let it go again."""
    codes = chord(combination)
    downs = [f"{code}:1" for code in codes]
    ups = [f"{code}:0" for code in reversed(codes)]
    _ydotool(["key", *downs, *ups], timeout=timeout)
