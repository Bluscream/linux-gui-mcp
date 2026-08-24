"""Driving a Linux desktop over MCP.

Every action that changes something returns a picture of what it changed. That
is the whole design: an agent cannot see a screen, so an action whose only
answer is "ok" leaves it guessing whether a click landed on the button or on
the empty space beside it. Returning the screenshot makes the next decision
based on what happened rather than on what was supposed to happen.

Every action also takes `settle_ms`. Input arrives the instant it is sent, but
nothing has redrawn yet - a screenshot taken immediately shows the state
before the action, which reads exactly like the action having failed.

Built for KDE on Wayland, where input has to go through the kernel's uinput
device because no X tool can reach a native Wayland window.
"""

from __future__ import annotations

import base64
import contextlib
import os
import signal
import tempfile
import time
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent
from pydantic import Field

import linux_gui as desktop
from linux_gui import DesktopError, Window

server = MCPServer(
    name="linux-gui",
    instructions=(
        "Drive the local KDE/Wayland desktop: find and focus windows, click, "
        "drag, type, press shortcuts, and see the result. Actions return a "
        "screenshot of what they affected. Prefer window-relative coordinates "
        "so a moved window does not send a click somewhere unintended."
    ),
)

SHOTS = Path(os.environ.get("LINUX_GUI_MCP_SHOTS", Path(tempfile.gettempdir()) / "linux-gui-mcp"))


def _picture(window: Window | None, tag: str, wanted: bool = True) -> list:
    """A screenshot of what just happened, unless the caller declined one.

    Optional because it is the expensive part: capturing costs about a second,
    while everything else here is a few tens of milliseconds. A sequence of
    actions that only needs to see the end result should not pay for a picture
    of each step.
    """
    if not wanted:
        return []
    path = desktop.screenshot(SHOTS / f"{tag}.png", window)
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return [ImageContent(type="image", data=data, mimeType="image/png")]


def _resolve(window_id: str | None) -> Window | None:
    """Look a window up, or say plainly that it is not there.

    Checked before every action rather than letting the underlying tool fail:
    a window that closed is the ordinary case, and `kdotool` reports it with a
    message that names neither the window nor what to do instead. An agent
    reading that cannot tell a closed window from a broken tool.
    """
    if window_id is None:
        return None
    if not desktop.window_exists(window_id):
        known = desktop.search_windows(".")
        listed = ", ".join(
            f"{w.cls}:{w.title[:28]!r}" for w in known[:8]
        ) or "none"
        raise DesktopError(
            f"no window {window_id!r} is open. Currently open: {listed}. "
            "Call find_window or list_windows for ids that are still valid."
        )
    return desktop.window_info(window_id)


def _point(window: Window | None, x: int, y: int) -> tuple[int, int]:
    """Turn coordinates into screen ones.

    Relative to the window when one is named. A tool that only spoke screen
    coordinates would send every click to the wrong place the moment somebody
    moved the window, and would do it silently.
    """
    if window is None:
        return x, y
    return window.x + x, window.y + y


@server.tool()
def list_windows(
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> list[dict]:
    """Every window on the desktop, with its id, title, class, pid and geometry."""
    return [window.as_dict() for window in desktop.search_windows(".")]


@server.tool()
def find_window(
    pattern: Annotated[str, Field(description="Regular expression against title and class")],
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> list[dict]:
    """Find windows whose title or class matches a pattern."""
    return [window.as_dict() for window in desktop.search_windows(pattern)]


@server.tool()
def focus_window(
    window_id: Annotated[str, Field(description="Window id from find_window")],
    settle_ms: Annotated[int, Field(description="Wait before looking, in ms")] = 300,
    screenshot: Annotated[bool, Field(description="Return a picture; costs ~1s")] = True,
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> list:
    """Bring a window to the front and show it."""
    desktop.focus_window(window_id)
    desktop.settle(settle_ms)
    window = desktop.window_info(window_id)
    return [window.as_dict(), *_picture(window, "focus", screenshot)]


@server.tool()
def screenshot(
    window_id: Annotated[str | None, Field(description="Omit for the whole screen")] = None,
    settle_ms: Annotated[int, Field(description="Wait before capturing")] = 0,
    screenshot: Annotated[bool, Field(description="Return a picture; costs ~1s")] = True,
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> list:
    """Look at the screen, or at one window."""
    desktop.settle(settle_ms)
    return _picture(_resolve(window_id), "shot", screenshot)


@server.tool()
def click(
    x: Annotated[int, Field(description="X, relative to the window if one is named")],
    y: Annotated[int, Field(description="Y, relative to the window if one is named")],
    window_id: Annotated[str | None, Field(description="Click inside this window")] = None,
    button: Annotated[str, Field(description="left, right or middle")] = "left",
    count: Annotated[int, Field(description="2 for a double click")] = 1,
    focus_first: Annotated[bool, Field(description="Raise the window before clicking")] = True,
    settle_ms: Annotated[int, Field(description="Wait before looking, in ms")] = 400,
    screenshot: Annotated[bool, Field(description="Return a picture; costs ~1s")] = True,
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> list:
    """Click, and show what happened."""
    window = _resolve(window_id)
    if window is not None and focus_first:
        desktop.focus_window(window.id)
        desktop.settle(150)
        # Re-read: focusing can move or resize a window, and a click placed
        # from stale geometry lands somewhere else entirely.
        window = desktop.window_info(window.id)
    screen_x, screen_y = _point(window, x, y)
    desktop.move_mouse(screen_x, screen_y)
    desktop.click(button, count)
    desktop.settle(settle_ms)
    return _picture(window, "click", screenshot)


@server.tool()
def drag(
    from_x: int,
    from_y: int,
    to_x: int,
    to_y: int,
    window_id: Annotated[str | None, Field(description="Coordinates inside this window")] = None,
    button: Annotated[str, Field(description="left, right or middle")] = "left",
    settle_ms: Annotated[int, Field(description="Wait before looking, in ms")] = 500,
    screenshot: Annotated[bool, Field(description="Return a picture; costs ~1s")] = True,
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> list:
    """Press, move and release - a drag, or a drag and drop."""
    window = _resolve(window_id)
    start = _point(window, from_x, from_y)
    end = _point(window, to_x, to_y)
    desktop.drag(start[0], start[1], end[0], end[1], button)
    desktop.settle(settle_ms)
    return _picture(window, "drag", screenshot)


@server.tool()
def type_text(
    text: str,
    window_id: Annotated[str | None, Field(description="Focus this window first")] = None,
    method: Annotated[
        str,
        Field(
            description=(
                "auto (paste if possible), paste (layout-proof, but some fields "
                "refuse it), or keystrokes (real key events, subject to layout)"
            )
        ),
    ] = "auto",
    layout: Annotated[
        str | None,
        Field(
            description=(
                "Keyboard layout for keystrokes: us, de, fr. Defaults to the "
                "session's own layout. Ignored when pasting."
            )
        ),
    ] = None,
    settle_ms: Annotated[int, Field(description="Wait before looking, in ms")] = 300,
    screenshot: Annotated[bool, Field(description="Return a picture; costs ~1s")] = True,
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> list:
    """Type into whatever has focus.

    Pastes by default, which no keyboard layout can garble. Use
    `method="keystrokes"` when something needs real key events - the text is
    rewritten for the layout first, since key codes name positions on a
    keyboard rather than letters.
    """
    window = _resolve(window_id)
    if window is not None:
        desktop.focus_window(window.id)
        desktop.settle(150)
    desktop.type_text(text, method=method, layout=layout, timeout=timeout)
    desktop.settle(settle_ms)
    return _picture(window, "type", screenshot)


@server.tool()
def input_settings(
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> dict:
    """How text will be entered, and what the alternatives are.

    Worth asking before typing into something fussy: it reports which layout
    this session uses, whether pasting is available, and which layouts the
    keystroke path knows how to rewrite for.
    """
    from linux_gui import layouts
    from linux_gui.shell import DesktopError as _Error
    from linux_gui.shell import which

    try:
        which("wl-copy")
        can_paste = True
    except _Error:
        can_paste = False

    return {
        "session_layout": layouts.session_layout(),
        "known_layouts": layouts.known_layouts(),
        "can_paste": can_paste,
        "default_method": "paste" if can_paste else "keystrokes",
        "methods": ["auto", "paste", "keystrokes"],
    }


@server.tool()
def press_keys(
    keys: Annotated[str, Field(description='A chord such as "ctrl+s" or "alt+f4"')],
    window_id: Annotated[str | None, Field(description="Focus this window first")] = None,
    settle_ms: Annotated[int, Field(description="Wait before looking, in ms")] = 300,
    screenshot: Annotated[bool, Field(description="Return a picture; costs ~1s")] = True,
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> list:
    """Press a keyboard shortcut."""
    window = _resolve(window_id)
    if window is not None:
        desktop.focus_window(window.id)
        desktop.settle(150)
    desktop.press(keys)
    desktop.settle(settle_ms)
    return _picture(window, "keys", screenshot)


@server.tool()
def interact(
    window_id: Annotated[str | None, Field(description="Window id from find_window or list_windows")] = None,
    x: Annotated[int | None, Field(description="X coordinate, relative to window if specified")] = None,
    y: Annotated[int | None, Field(description="Y coordinate, relative to window if specified")] = None,
    to_x: Annotated[int | None, Field(description="End X coordinate for drag action")] = None,
    to_y: Annotated[int | None, Field(description="End Y coordinate for drag action")] = None,
    button: Annotated[str, Field(description="Mouse button for click or drag: left, right, middle")] = "left",
    click_count: Annotated[int, Field(description="Click count (e.g. 1 for single click, 2 for double click)")] = 0,
    paste_text: Annotated[str | None, Field(description="Text to paste directly into focus/target field")] = None,
    type_text: Annotated[str | None, Field(description="Text to send as simulated keystrokes")] = None,
    keys: Annotated[str | None, Field(description="Key chord shortcut to press (e.g. 'ctrl+s', 'enter', 'tab')")] = None,
    layout: Annotated[str | None, Field(description="Keyboard layout for type_text: us, de, fr")] = None,
    scroll_amount: Annotated[int, Field(description="Scroll wheel delta: positive scrolls down, negative up")] = 0,
    focus_first: Annotated[bool, Field(description="Raise and focus window before performing interactions")] = True,
    settle_ms: Annotated[int, Field(description="Wait after interactions before returning screenshot, in ms")] = 400,
    screenshot: Annotated[bool, Field(description="Return a picture of the resulting window state")] = True,
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> list:
    """Perform one or multiple combined GUI actions (click, drag, type, paste, key shortcut, scroll)."""
    window = _resolve(window_id)

    if window is not None and focus_first:
        desktop.focus_window(window.id)
        desktop.settle(150)
        window = desktop.window_info(window.id)

    # 1. Pointer positioning, click, or drag
    if x is not None and y is not None:
        screen_x, screen_y = _point(window, x, y)
        if to_x is not None and to_y is not None:
            end_x, end_y = _point(window, to_x, to_y)
            desktop.drag(screen_x, screen_y, end_x, end_y, button)
        else:
            desktop.move_mouse(screen_x, screen_y)
            if click_count > 0:
                desktop.click(button, click_count)

    # 2. Scroll wheel
    if scroll_amount != 0:
        desktop.scroll(scroll_amount)

    # 3. Paste text
    if paste_text is not None:
        desktop.type_text(paste_text, method="paste", layout=layout, timeout=timeout)

    # 4. Type text as keystrokes
    if type_text is not None:
        desktop.type_text(type_text, method="keystrokes", layout=layout, timeout=timeout)

    # 5. Press keyboard shortcut / chord
    if keys is not None:
        desktop.press(keys)

    desktop.settle(settle_ms)
    return _picture(window, "interact", screenshot)


@server.tool()
def scroll(
    amount: Annotated[int, Field(description="Positive scrolls down, negative up")],
    x: Annotated[int | None, Field(description="Point here first")] = None,
    y: int | None = None,
    window_id: str | None = None,
    settle_ms: int = 300,
    screenshot: Annotated[bool, Field(description="Return a picture; costs ~1s")] = True,
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> list:
    """Scroll the wheel."""
    window = _resolve(window_id)
    if x is not None and y is not None:
        screen_x, screen_y = _point(window, x, y)
        desktop.move_mouse(screen_x, screen_y)
    desktop.scroll(amount)
    desktop.settle(settle_ms)
    return _picture(window, "scroll", screenshot)


@server.tool()
def wait_for_window(
    pattern: Annotated[str, Field(description="Regular expression against title and class")],
    timeout_s: Annotated[float, Field(description="Give up after this long")] = 15.0,
    settle_ms: Annotated[int, Field(description="Let it finish drawing before looking")] = 800,
    screenshot: Annotated[bool, Field(description="Return a picture; costs ~1s")] = True,
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> list:
    """Wait until a window appears, then show it.

    The settle matters more here than anywhere: a window exists as soon as it
    is mapped, which is well before it has drawn anything. Screenshotting the
    instant it appears reliably captures an empty rectangle.
    """
    window = desktop.wait_for_window(pattern, timeout_s)
    desktop.settle(settle_ms)
    window = desktop.window_info(window.id)
    return [window.as_dict(), *_picture(window, "waited", screenshot)]


@server.tool()
def wait_for_process(
    pattern: Annotated[str, Field(description="Matched against the full command line")],
    timeout_s: float = 15.0,
    settle_ms: int = 800,
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> dict:
    """Wait until a process exists. Returns its pid, and its windows if it has any.

    Windows are reported too because the usual reason for waiting on a process
    is to then drive it, and a process that is running with no window yet is
    the state where driving it fails confusingly.
    """
    pid = desktop.wait_for_process(pattern, timeout_s)
    desktop.settle(settle_ms)
    windows = [w.as_dict() for w in desktop.search_windows(".") if w.pid == pid]
    return {"pid": pid, "windows": windows}


@server.tool()
def active_window(
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> dict:
    """Which window has focus."""
    return desktop.active_window().as_dict()


@server.tool()
def run_app(
    executable: Annotated[str, Field(description="Program binary or script to run")],
    args: Annotated[list[str] | None, Field(description="Arguments list for the executable")] = None,
    env: Annotated[dict[str, str] | None, Field(description="Additional environment variables")] = None,
    cwd: Annotated[str | None, Field(description="Working directory")] = None,
    focus_if_running: Annotated[
        bool, Field(description="If a matching window is already running, focus and return it instead of spawning a new instance")
    ] = True,
    restart_if_running: Annotated[
        bool, Field(description="If a matching window/process is already running, terminate it before spawning a fresh instance")
    ] = False,
    wait_for_window_s: Annotated[float, Field(description="Wait this long for its window; 0 to skip")] = 20.0,
    screenshot: Annotated[bool, Field(description="Return a picture; costs ~1s")] = True,
    settle_ms: Annotated[int, Field(description="Let it finish drawing before looking")] = 800,
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> list:
    """Start a graphical program in the desktop session and show its window.

    Runs detached, so it outlives this call. Environment variables passed in `env`
    are merged into the desktop session environment.
    """
    full_cmd = [executable, *(args or [])]
    target_name = Path(full_cmd[-1] if full_cmd else executable).name

    # Check for existing matching windows if requested (matching app class strictly to avoid killing IDE)
    existing_windows = [
        w for w in desktop.search_windows(".")
        if (w.cls or "").lower() == target_name.lower()
        and "antigravity" not in (w.cls or "").lower()
    ]

    if existing_windows:
        if restart_if_running:
            for w in existing_windows:
                with contextlib.suppress(Exception):
                    os.kill(w.pid, signal.SIGTERM)
            time.sleep(0.3)
        elif focus_if_running:
            window = existing_windows[0]
            desktop.focus_window(window.id)
            desktop.settle(settle_ms)
            window = desktop.window_info(window.id)
            return [{"pid": window.pid, "window": window.as_dict(), "focused_existing": True}, *_picture(window, "focused", screenshot)]

    pid = desktop.spawn(full_cmd, cwd=cwd, env=env)
    if wait_for_window_s <= 0:
        return [{"pid": pid, "windows": []}]
    window = desktop.wait_for_window_of(pid, wait_for_window_s, target_name=target_name)
    desktop.settle(settle_ms)
    window = desktop.window_info(window.id)
    return [{"pid": pid, "window": window.as_dict()}, *_picture(window, "launched", screenshot)]



@server.tool()
def run_in_terminal(
    command: Annotated[str, Field(description="Shell command line to run")],
    cwd: Annotated[str | None, Field(description="Working directory")] = None,
    wait_for_window_s: float = 20.0,
    settle_ms: Annotated[int, Field(description="Let the program draw before looking")] = 1500,
    screenshot: Annotated[bool, Field(description="Return a picture; costs ~1s")] = True,
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> list:
    """Run a command in a real terminal window, and show it.

    For anything that needs a terminal to exist at all - a TUI, an interactive
    prompt, a program that refuses to start without a tty. The window is held
    open after the command finishes so its last output is still readable
    rather than vanishing with the window.
    """
    pid = desktop.spawn(
        [
            "konsole",
            "--separate",
            "--hold",
            *(["--workdir", cwd] if cwd else []),
            "-e",
            "bash",
            "-lc",
            command,
        ],
        cwd,
    )
    if wait_for_window_s <= 0:
        return [{"pid": pid}]
    window = desktop.wait_for_window_of(pid, wait_for_window_s)
    desktop.settle(settle_ms)
    window = desktop.window_info(window.id)
    return [{"pid": pid, "window": window.as_dict()}, *_picture(window, "terminal", screenshot)]


@server.tool()
def list_tray_items(
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> list[dict]:
    """Everything in the system tray, with its id, title and status.

    Read over D-Bus rather than from pixels. The tray is a row of identical
    little squares, so recognising one by sight is guesswork; every item here
    says what it is.
    """
    return [item.as_dict() for item in desktop.tray.items()]


@server.tool()
def click_tray_item(
    pattern: Annotated[str, Field(description="Matched against the item id and title")],
    action: Annotated[str, Field(description="activate (left), secondary (middle) or context (right)")] = "activate",
    settle_ms: Annotated[int, Field(description="Let the menu or window appear")] = 700,
    screenshot: Annotated[bool, Field(description="Return a picture; costs ~1s")] = True,
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> list:
    """Click a tray item and show what appeared.

    Sent as the item's own D-Bus method rather than as a click at its screen
    position: only the panel knows where an item sits, and an item that has
    moved would otherwise take the click somewhere else entirely.
    """
    item = desktop.tray.find(pattern)
    desktop.tray.act(item, action)
    desktop.settle(settle_ms)
    return [item.as_dict(), *_picture(None, "tray", screenshot)]


@server.tool()
def scroll_tray_item(
    pattern: Annotated[str, Field(description="Matched against the item id and title")],
    delta: Annotated[int, Field(description="Positive up, negative down")],
    orientation: Annotated[str, Field(description="vertical or horizontal")] = "vertical",
    settle_ms: int = 500,
    screenshot: Annotated[bool, Field(description="Return a picture; costs ~1s")] = True,
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> list:
    """Scroll on a tray item - volume applets use this."""
    item = desktop.tray.find(pattern)
    desktop.tray.scroll(item, delta, orientation)
    desktop.settle(settle_ms)
    return [item.as_dict(), *_picture(None, "tray-scroll", screenshot)]


@server.tool()
def find_and_click(
    text: Annotated[str, Field(description="Text or label to search for and click")],
    window_id: Annotated[str | None, Field(description="Target window ID")] = None,
    settle_ms: Annotated[int, Field(description="Wait after click in ms")] = 400,
    screenshot: Annotated[bool, Field(description="Return picture result")] = True,
    timeout: Annotated[float, Field(description="Timeout in seconds")] = 30.0,
) -> list:
    """Click a control by its label. Not available on this desktop.

    Locating a control by its text needs the accessibility tree, and on KDE
    the Qt applications do not publish one: the AT-SPI bus here lists only
    tray helpers and GTK programs, so `kwrite` is invisible to it even while
    its window is on screen.

    This refuses rather than guessing. It previously chose coordinates from a
    table of keywords and clicked them - which meant a caller asking for a
    button got a click at a fixed point that had nothing to do with where the
    button was, plus a screenshot that made it look as though something had
    happened.
    """
    del window_id, settle_ms, screenshot, timeout
    raise DesktopError(
        f"cannot locate {text!r} by its label: KDE's Qt applications do not "
        "publish an accessibility tree, so there is nothing to search. "
        "Take a screenshot, read the position off it, and call click(x, y, "
        "window_id) instead. To change this, Qt accessibility would have to be "
        "enabled session-wide (QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1) and every "
        "application restarted."
    )


@server.tool()
def assert_window_title(
    expected: Annotated[str, Field(description="Expected window title substring")],
    window_id: Annotated[str | None, Field(description="Target window ID; defaults to active window")] = None,
    timeout: Annotated[float, Field(description="Timeout in seconds")] = 30.0,
) -> dict:
    """Assert that a window title contains the expected substring."""
    window = _resolve(window_id) if window_id else desktop.active_window()
    actual = window.title or ""
    matched = expected.lower() in actual.lower()
    return {
        "asserted": expected,
        "actual": actual,
        "matched": matched,
        "window": window.as_dict(),
    }


@server.tool()
def get_process_info(
    target: Annotated[str, Field(description="A pid, or a pattern matched against the command line")],
    include_env: Annotated[bool, Field(description="Include the environment, with credentials blanked")] = True,
    timeout: Annotated[float, Field(description="Timeout for operation execution in seconds")] = 30.0,
) -> dict:
    """Everything about a process and what it put on the desktop.

    Its identity and arguments, its windows with titles, classes and
    rectangles, and the tray icons it owns with the actions each accepts.

    One answer rather than three calls, because the three are only useful
    together: a pid says a program is running, its windows say whether it got
    as far as showing anything, and its tray icons say how to reach it when it
    has no window at all.

    Environment values that look like credentials are blanked. An environment
    is one of the densest concentrations of secrets on a machine, and handing
    one whole to a language model is a rotation waiting to happen.
    """
    return desktop.process_report(target, include_env=include_env).as_dict()


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()

