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
from mcp.types import ImageContent, TextContent
from pydantic import Field

import linux_gui as desktop
from linux_gui import DesktopError, Window

server = MCPServer(
    name="linux-gui",
    instructions=(
        "Drive the local KDE/Wayland desktop. `find_window` to see what is "
        "open, then `interact` to do things to it - clicking, dragging, "
        "typing, shortcuts and scrolling are all one call, and they happen in "
        "that order, so a single call can click a field and type into it. "
        "Actions return a screenshot of what they affected; pass "
        "screenshot=false to skip it when you do not need to look. Prefer "
        "window-relative coordinates so a moved window does not send a click "
        "somewhere unintended."
    ),
)

SHOTS = Path(os.environ.get("LINUX_GUI_MCP_SHOTS", Path(tempfile.gettempdir()) / "linux-gui-mcp"))


#: How far a pointer may land from where it was asked before it is worth
#: mentioning, in pixels.
#:
#: A pixel or two is rounding. Anything past that and the click may have gone
#: to a different control than the caller meant, which is worth knowing about
#: even though the click itself still happened.
POINTER_DRIFT_LIMIT = 3


def _notes(notes: list[str]) -> list:
    """Anything the caller should know, ahead of the picture."""
    return [TextContent(type="text", text=note) for note in notes]


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
            "Call find_window for ids that are still valid."
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
def find_window(
    pattern: Annotated[str, Field(description="Regex against title and class. '.' matches everything")] = ".",
) -> list[dict]:
    """Every open window matching a pattern: id, title, class, pid, geometry.

    The default matches everything, so this doubles as "what is open?".
    """
    return [window.as_dict() for window in desktop.search_windows(pattern)]


@server.tool()
def active_window() -> dict:
    """Which window has focus."""
    return desktop.active_window().as_dict()


@server.tool()
def screenshot(
    window_id: Annotated[str | None, Field(description="Omit for the whole screen")] = None,
    settle_ms: Annotated[int, Field(description="Wait before capturing, in ms")] = 0,
) -> list:
    """Look at the screen, or at one window."""
    desktop.settle(settle_ms)
    return _picture(_resolve(window_id), "shot")


@server.tool()
def interact(
    window_id: Annotated[str | None, Field(description="Window to act on; coordinates become relative to it. Omit to act on the whole screen, which is the fallback when a window will not respond")] = None,
    x: Annotated[int | None, Field(description="X, relative to the window when one is given")] = None,
    y: Annotated[int | None, Field(description="Y, relative to the window when one is given")] = None,
    to_x: Annotated[int | None, Field(description="Drag to this X. Needs x and y as the start")] = None,
    to_y: Annotated[int | None, Field(description="Drag to this Y")] = None,
    button: Annotated[str, Field(description="left, right or middle")] = "left",
    click_count: Annotated[int, Field(description="0 moves without clicking, 1 clicks, 2 double-clicks")] = 0,
    paste_text: Annotated[str | None, Field(description="Text to paste. Layout-proof; some fields refuse a paste")] = None,
    type_text: Annotated[str | None, Field(description="Text to send as real keystrokes, rewritten for the layout")] = None,
    keys: Annotated[str | None, Field(description="A chord such as 'ctrl+s' or 'alt+f4'")] = None,
    layout: Annotated[str | None, Field(description="Keyboard layout for type_text: us, de, fr. Defaults to the session's")] = None,
    scroll_amount: Annotated[int, Field(description="Wheel delta; positive scrolls down")] = 0,
    focus_first: Annotated[bool, Field(description="Raise the window before acting")] = True,
    settle_ms: Annotated[int, Field(description="Wait before the screenshot, in ms")] = 400,
    screenshot: Annotated[bool, Field(description="Return a picture; costs about 0.2s")] = True,
    timeout: Annotated[float, Field(description="Give up after this long, in seconds")] = 30.0,
) -> list:
    """Do things to a window - or to the screen, when no window is named.

    Everything is optional and they happen in that order, so one call can click
    a field and then type into it - which is the usual shape of a UI step and
    would otherwise be two round trips, each paying for a screenshot.

    With `window_id` and no action at all, this just focuses the window and
    shows it.

    **Without `window_id` this acts on the whole screen**: coordinates are
    screen coordinates, nothing is focused first, and the picture is of the
    whole desktop. That is the fallback when a window will not cooperate -
    an application that ignores input aimed at it, a window whose id has gone
    stale, a popup or menu the compositor does not report as a window at all.
    Take a full-screen shot, read the position off it, and click there.

    Pasting and typing are different on purpose. `paste_text` goes through the
    clipboard, which no keyboard layout can garble and which is fast; but some
    fields refuse a paste, and an application watching for key events sees
    none. `type_text` sends real keystrokes, rewritten for the layout, because
    key codes name positions on a keyboard rather than letters.
    """
    notes: list[str] = []
    window = _resolve(window_id)
    if window is not None and focus_first:
        desktop.focus_window(window.id)
        desktop.settle(150)
        # Re-read: focusing can move or resize a window, and coordinates taken
        # from stale geometry land somewhere else entirely.
        window = desktop.window_info(window.id)

    if x is not None and y is not None:
        start = _point(window, x, y)
        if to_x is not None and to_y is not None:
            end = _point(window, to_x, to_y)
            desktop.drag(start[0], start[1], end[0], end[1], button)
        else:
            landed = desktop.move_mouse(start[0], start[1])
            drift = (landed[0] - start[0], landed[1] - start[1])
            if max(abs(drift[0]), abs(drift[1])) > POINTER_DRIFT_LIMIT:
                # Reported rather than raised: the click has value even from
                # slightly the wrong place, and an agent that can see it
                # missed can look and try again. Silence here is what made a
                # mis-scaled pointer look like an application ignoring input.
                notes.append(
                    f"pointer asked for {start[0]},{start[1]} but landed at "
                    f"{landed[0]},{landed[1]} ({drift[0]:+d},{drift[1]:+d}); "
                    "the click went to the second of those"
                )
            if click_count > 0:
                desktop.click(button, click_count)
    elif to_x is not None or to_y is not None:
        raise DesktopError("a drag needs x and y as its starting point")

    if scroll_amount:
        desktop.scroll(scroll_amount)
    if paste_text is not None:
        desktop.type_text(paste_text, method="paste", layout=layout, timeout=timeout)
    if type_text is not None:
        desktop.type_text(type_text, method="keystrokes", layout=layout, timeout=timeout)
    if keys is not None:
        desktop.press(keys)

    desktop.settle(settle_ms)
    return _notes(notes) + _picture(window, "interact", screenshot)


@server.tool()
def move_window(
    window_id: Annotated[str, Field(description="Window to move or resize")],
    x: Annotated[int | None, Field(description="New left edge; omit to leave where it is")] = None,
    y: Annotated[int | None, Field(description="New top edge; omit to leave where it is")] = None,
    width: Annotated[int | None, Field(description="New width; omit to keep the current one")] = None,
    height: Annotated[int | None, Field(description="New height; omit to keep the current one")] = None,
    settle_ms: Annotated[int, Field(description="Wait before the screenshot, in ms")] = 250,
    screenshot: Annotated[bool, Field(description="Return a picture of the result")] = True,
) -> list:
    """Move a window, resize it, or both.

    Each part is optional, so this can move without resizing or the other way
    round. Useful for making a window big enough that what you need is on
    screen at all, rather than scrolling to it.

    The geometry is read back after the compositor has applied it, and
    reported: a window may refuse a size, or round it to a step, and a caller
    that assumed otherwise would then be computing coordinates against a
    shape the window is not.
    """
    window = _resolve(window_id)
    if window is None:
        raise DesktopError("move_window needs a window id")

    result = desktop.move_window(window_id, x, y, width, height)

    asked = {"x": x, "y": y, "width": width, "height": height}
    got = {"x": result.x, "y": result.y, "width": result.width, "height": result.height}
    refused = [
        f"{name} {value} -> {got[name]}"
        for name, value in asked.items()
        if value is not None and got[name] != value
    ]

    notes = [
        f"window is now {got['width']}x{got['height']} at {got['x']},{got['y']}"
    ]
    if refused:
        notes.append("the compositor adjusted: " + ", ".join(refused))

    desktop.settle(settle_ms)
    return _notes(notes) + _picture(result, "move", screenshot)


@server.tool()
def input_settings() -> dict:
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
    }


@server.tool()
def wait_for_window(
    pattern: Annotated[str, Field(description="Regex against title and class")],
    timeout_s: Annotated[float, Field(description="Give up after this long")] = 15.0,
    settle_ms: Annotated[int, Field(description="Let it finish drawing before looking")] = 800,
    screenshot: Annotated[bool, Field(description="Return a picture")] = True,
) -> list:
    """Wait until a window appears, then show it.

    The settle matters more here than anywhere: a window exists as soon as it
    is mapped, which is well before it has drawn anything, so capturing the
    instant it appears reliably yields an empty rectangle.
    """
    window = desktop.wait_for_window(pattern, timeout_s)
    desktop.settle(settle_ms)
    window = desktop.window_info(window.id)
    return [window.as_dict(), *_picture(window, "waited", screenshot)]


@server.tool()
def wait_for_process(
    pattern: Annotated[str, Field(description="Matched against the full command line")],
    timeout_s: Annotated[float, Field(description="Give up after this long")] = 15.0,
    settle_ms: Annotated[int, Field(description="Wait before reporting, in ms")] = 800,
) -> dict:
    """Wait until a process exists. Returns its pid and any windows it owns.

    Windows are reported too because the usual reason for waiting on a process
    is to then drive it, and a process running with no window yet is the state
    where driving it fails confusingly.
    """
    pid = desktop.wait_for_process(pattern, timeout_s)
    desktop.settle(settle_ms)
    windows = [w.as_dict() for w in desktop.search_windows(".") if w.pid == pid]
    return {"pid": pid, "windows": windows}


@server.tool()
def run_app(
    executable: Annotated[str, Field(description="Program to run")],
    args: Annotated[list[str] | None, Field(description="Arguments")] = None,
    env: Annotated[dict[str, str] | None, Field(description="Extra environment variables")] = None,
    cwd: Annotated[str | None, Field(description="Working directory")] = None,
    focus_if_running: Annotated[bool, Field(description="Focus an existing window instead of starting another")] = True,
    restart_if_running: Annotated[bool, Field(description="Ask an existing instance to quit first")] = False,
    wait_for_window_s: Annotated[float, Field(description="Wait this long for its window; 0 to skip")] = 20.0,
    settle_ms: Annotated[int, Field(description="Let it finish drawing before looking")] = 800,
    screenshot: Annotated[bool, Field(description="Return a picture")] = True,
) -> list:
    """Start a graphical program in the desktop session and show its window.

    Runs detached, so it outlives this call. The session environment is passed
    explicitly rather than inherited: a program started without
    WAYLAND_DISPLAY does not fail loudly, it simply never appears.
    """
    # From the executable, never from the arguments. Taking the last argument
    # meant `run_app("kwrite", ["notes.md"])` looked for a window class called
    # "notes.md" - so the already-running check silently never matched, and
    # with restart_if_running it could have matched something else entirely.
    target_name = Path(executable).name
    running = [
        w for w in desktop.search_windows(".")
        if (w.cls or "").lower() == target_name.lower()
    ]

    if running and restart_if_running:
        for window in running:
            if window.pid:
                # SIGTERM, not SIGKILL: an application being closed should get
                # the chance to save.
                with contextlib.suppress(Exception):
                    os.kill(window.pid, signal.SIGTERM)
        time.sleep(0.3)
    elif running and focus_if_running:
        window = running[0]
        desktop.focus_window(window.id)
        desktop.settle(settle_ms)
        window = desktop.window_info(window.id)
        return [
            {"pid": window.pid, "window": window.as_dict(), "focused_existing": True},
            *_picture(window, "focused", screenshot),
        ]

    pid = desktop.spawn([executable, *(args or [])], cwd=cwd, env=env)
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
    wait_for_window_s: Annotated[float, Field(description="Wait this long for the terminal")] = 20.0,
    settle_ms: Annotated[int, Field(description="Let the program draw before looking")] = 1500,
    screenshot: Annotated[bool, Field(description="Return a picture")] = True,
) -> list:
    """Run a command in a real terminal window, and show it.

    For anything that needs a terminal to exist at all - a TUI, an interactive
    prompt, a program that refuses to start without a tty. The window is held
    open after the command finishes so its last output is still readable.
    """
    pid = desktop.spawn(
        [
            "konsole", "--separate", "--hold",
            *(["--workdir", cwd] if cwd else []),
            "-e", "bash", "-lc", command,
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
def list_tray_items() -> list[dict]:
    """Everything in the system tray, with its id, title, status and owner.

    Read over D-Bus rather than from pixels. The tray is a row of identical
    little squares, so recognising one by sight is guesswork; every item here
    says what it is.
    """
    return [item.as_dict() for item in desktop.tray.items()]


@server.tool()
def tray_action(
    pattern: Annotated[str, Field(description="Matched against the item id and title")],
    action: Annotated[str, Field(description="activate (left), secondary (middle), context (right), or scroll")] = "activate",
    scroll_amount: Annotated[int, Field(description="For action='scroll': positive up, negative down")] = 0,
    settle_ms: Annotated[int, Field(description="Let the menu or window appear, in ms")] = 700,
    screenshot: Annotated[bool, Field(description="Return a picture")] = True,
) -> list:
    """Click or scroll a tray item, and show what appeared.

    Sent as the item's own D-Bus method rather than as a click at its screen
    position: only the panel knows where an item sits, so an item that moved
    would take the click somewhere else entirely.
    """
    item = desktop.tray.find(pattern)
    if action == "scroll":
        desktop.tray.scroll(item, scroll_amount)
    else:
        desktop.tray.act(item, action)
    desktop.settle(settle_ms)
    return [item.as_dict(), *_picture(None, "tray", screenshot)]


@server.tool()
def get_process_info(
    target: Annotated[str, Field(description="A pid, or a pattern matched against the command line")],
    include_env: Annotated[bool, Field(description="Include the environment, with credentials blanked")] = True,
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
