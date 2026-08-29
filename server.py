"""Driving a Linux desktop over MCP.

Consolidated high-leverage toolset for KDE/Wayland desktop automation.
Every action returns structured JSON metadata + optional screenshots.
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
        "Drive the local KDE/Wayland desktop with a consolidated toolset:\n"
        "- `find_window`: find windows, get focused window, or wait for windows.\n"
        "- `interact`: focus, move/resize, OCR text search, click/drag, type/paste, shortcuts.\n"
        "- `screenshot`: capture screen or window.\n"
        "- `run_app`: launch GUI applications or terminal commands.\n"
        "- `tray`: list system tray items or perform actions on them.\n"
        "- `get_process_info`: inspect process details, windows, and tray items."
    ),
)

SHOTS = Path(
    os.environ.get("LINUX_GUI_MCP_SHOTS", Path(tempfile.gettempdir()) / "linux-gui-mcp")
)

POINTER_DRIFT_LIMIT = 3


def _check_interact_blocked(tool_name: str) -> None:
    """Check BLOCK_INTERACT dynamically on every non-readonly call."""
    val = os.environ.get("BLOCK_INTERACT", "").strip().lower()
    if val in ("1", "true", "yes", "on", "enabled"):
        raise DesktopError(
            f"Tool '{tool_name}' is blocked by BLOCK_INTERACT environment variable"
        )


def _notes(notes: list[str]) -> list:
    """Encapsulate notes as standard MCP TextContent."""
    return [TextContent(type="text", text=note) for note in notes]


def _picture(window: Window | None, tag: str, wanted: bool = True) -> list:
    """Return standard MCP ImageContent screenshot unless declined."""
    if not wanted:
        return []
    path = desktop.screenshot(SHOTS / f"{tag}.png", window)
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return [ImageContent(type="image", data=data, mimeType="image/png")]


def _resolve(window_id: str | None) -> Window | None:
    """Resolve window by ID or title/class pattern."""
    if window_id is None:
        return None
    if desktop.window_exists(window_id):
        return desktop.window_info(window_id)

    matches = desktop.search_windows(window_id)
    if matches:
        return matches[0]

    known = desktop.search_windows(".")
    listed = ", ".join(f"{w.cls}:{w.title[:28]!r}" for w in known[:8]) or "none"
    raise DesktopError(
        f"no window matching {window_id!r} is open. Currently open: {listed}. "
        "Call find_window for ids that are still valid."
    )


def _point(window: Window | None, x: int, y: int) -> tuple[int, int]:
    """Calculate absolute screen coordinates relative to window if given."""
    if window is None:
        return x, y
    return window.x + x, window.y + y


# ==============================================================================
# Consolidated MCP Tools
# ==============================================================================


@server.tool()
def find_window(
    pattern: Annotated[
        str, Field(description="Regex against title and class. '.' matches everything")
    ] = ".",
    focused_only: Annotated[
        bool, Field(description="If true, returns only the currently focused active window")
    ] = False,
    wait_timeout_s: Annotated[
        float,
        Field(
            description="If > 0, waits up to this many seconds for a matching window to appear"
        ),
    ] = 0.0,
    settle_ms: Annotated[
        int, Field(description="Wait before reporting/capturing when waiting, in ms")
    ] = 800,
    screenshot: Annotated[
        bool, Field(description="Include a screenshot of the matched/active window")
    ] = False,
) -> list:
    """Find windows, inspect the active focused window, or wait for a window to appear.

    Consolidates `find_window`, `active_window`, and `wait_for_window`.
    """
    if focused_only:
        active = desktop.active_window()
        return [active.as_dict(), *_picture(active, "active", screenshot)]

    if wait_timeout_s > 0:
        window = desktop.wait_for_window(pattern, wait_timeout_s)
        desktop.settle(settle_ms)
        window = desktop.window_info(window.id)
        return [window.as_dict(), *_picture(window, "waited", screenshot)]

    windows = desktop.search_windows(pattern)
    res = [w.as_dict() for w in windows]
    pic = _picture(windows[0] if windows else None, "found", screenshot)
    return [res, *pic] if screenshot else res


@server.tool()
def screenshot(
    window_id: Annotated[
        str | None,
        Field(description="Window ID, title, or class pattern. Omit for full desktop screen"),
    ] = None,
    settle_ms: Annotated[int, Field(description="Wait before capturing, in ms")] = 0,
) -> list:
    """Capture a screenshot of the entire desktop or a specific window."""
    desktop.settle(settle_ms)
    return _picture(_resolve(window_id), "shot")


@server.tool()
def interact(
    window_id: Annotated[
        str | None,
        Field(
            description="Window ID, title, or class pattern to act on. Coordinates/OCR become relative to it. Omit to act on full screen"
        ),
    ] = None,
    text: Annotated[
        str | None,
        Field(
            description="Text or regex to locate on screen/window via OCR and target its center (overrides x/y)"
        ),
    ] = None,
    ocr_tries: Annotated[
        int,
        Field(
            description="Number of OCR attempts to locate the text before giving up (useful for UI elements that take a moment to render)"
        ),
    ] = 3,
    ocr_retry_delay_s: Annotated[
        float, Field(description="Seconds to wait between OCR search attempts")
    ] = 0.5,
    x: Annotated[
        int | None, Field(description="X coordinate, relative to window (or screen)")
    ] = None,
    y: Annotated[
        int | None, Field(description="Y coordinate, relative to window (or screen)")
    ] = None,
    to_x: Annotated[
        int | None, Field(description="Drag to this X. Requires x and y as start")
    ] = None,
    to_y: Annotated[int | None, Field(description="Drag to this Y")] = None,
    button: Annotated[str, Field(description="Mouse button: left, right or middle")] = "left",
    click_count: Annotated[
        int, Field(description="0 moves without clicking, 1 clicks, 2 double-clicks")
    ] = 0,
    move_window_x: Annotated[
        int | None, Field(description="Move window to new left edge X")
    ] = None,
    move_window_y: Annotated[
        int | None, Field(description="Move window to new top edge Y")
    ] = None,
    resize_width: Annotated[
        int | None, Field(description="Resize window to new width")
    ] = None,
    resize_height: Annotated[
        int | None, Field(description="Resize window to new height")
    ] = None,
    paste_text: Annotated[
        str | None,
        Field(description="Text to paste via clipboard (fast and layout-proof)"),
    ] = None,
    type_text: Annotated[
        str | None,
        Field(description="Text to send as keystrokes rewritten for layout"),
    ] = None,
    keys: Annotated[
        str | None, Field(description="Keyboard shortcut chord, e.g. 'ctrl+v' or 'alt+f4'")
    ] = None,
    layout: Annotated[
        str | None,
        Field(
            description="Keyboard layout for type_text: us, de, fr. Defaults to session layout"
        ),
    ] = None,
    scroll_amount: Annotated[
        int, Field(description="Wheel delta; positive scrolls down, negative scrolls up")
    ] = 0,
    focus_first: Annotated[
        bool, Field(description="Raise and focus window before acting")
    ] = True,
    settle_ms: Annotated[
        int, Field(description="Wait before final screenshot, in ms")
    ] = 400,
    screenshot: Annotated[
        bool, Field(description="Return a screenshot of what was affected")
    ] = True,
    timeout: Annotated[
        float, Field(description="Timeout for input commands, in seconds")
    ] = 30.0,
) -> list:
    """All-in-one GUI interaction: window management, OCR targeting, mouse, keyboard.

    Execution order in a single call:
    1. Focus/Raise window
    2. Move/Resize window (if requested)
    3. Read pointer position (reported in metadata)
    4. OCR text targeting & center resolution (if `text` provided)
    5. Mouse move / drag / click
    6. Scroll
    7. Paste text / Type text / Shortcut keys
    8. Read updated pointer position
    9. Settle & capture screenshot
    """
    _check_interact_blocked("interact")
    notes: list[str] = []

    try:
        before_abs = desktop.read_pointer()
    except Exception:
        before_abs = None

    window = _resolve(window_id)
    if window is not None and focus_first:
        desktop.focus_window(window.id)
        desktop.settle(150)
        window = desktop.window_info(window.id)

    # Window resizing / moving if requested
    if window is not None and any(
        v is not None
        for v in (move_window_x, move_window_y, resize_width, resize_height)
    ):
        window = desktop.move_window(
            window.id,
            move_window_x,
            move_window_y,
            resize_width,
            resize_height,
        )
        notes.append(
            f"window resized/moved: {window.width}x{window.height} at ({window.x}, {window.y})"
        )

    if before_abs is not None:
        if window is not None:
            before_rel = (before_abs[0] - window.x, before_abs[1] - window.y)
            notes.append(
                f"pointer before: absolute ({before_abs[0]}, {before_abs[1]}), "
                f"window-relative ({before_rel[0]}, {before_rel[1]})"
            )
        else:
            notes.append(f"pointer before: absolute ({before_abs[0]}, {before_abs[1]})")

    # OCR text location
    if text is not None:
        ocr_match = desktop.find_text_on_screen(
            text,
            window=window,
            tries=ocr_tries,
            retry_delay_s=ocr_retry_delay_s,
        )
        x = ocr_match.center_x
        y = ocr_match.center_y
        notes.append(
            f"OCR found text {text!r} (matched: {ocr_match.text!r}, conf: {ocr_match.confidence:.0f}%) "
            f"at center ({x}, {y})"
        )
        if (
            click_count == 0
            and type_text is None
            and paste_text is None
            and keys is None
            and to_x is None
        ):
            click_count = 1

    if x is not None and y is not None:
        start = _point(window, x, y)
        if to_x is not None and to_y is not None:
            end = _point(window, to_x, to_y)
            desktop.drag(start[0], start[1], end[0], end[1], button)
        else:
            landed = desktop.move_mouse(start[0], start[1])
            drift = (landed[0] - start[0], landed[1] - start[1])
            if max(abs(drift[0]), abs(drift[1])) > POINTER_DRIFT_LIMIT:
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
        desktop.type_text(
            type_text, method="keystrokes", layout=layout, timeout=timeout
        )
    if keys is not None:
        desktop.press(keys)

    try:
        after_abs = desktop.read_pointer()
        if window is not None:
            after_rel = (after_abs[0] - window.x, after_abs[1] - window.y)
            notes.append(
                f"pointer after: absolute ({after_abs[0]}, {after_abs[1]}), "
                f"window-relative ({after_rel[0]}, {after_rel[1]})"
            )
        else:
            notes.append(f"pointer after: absolute ({after_abs[0]}, {after_abs[1]})")
    except Exception:
        pass

    desktop.settle(settle_ms)
    return _notes(notes) + _picture(window, "interact", screenshot)


@server.tool()
def run_app(
    executable_or_command: Annotated[
        str, Field(description="Application executable (e.g. 'kwrite') or command line")
    ],
    args: Annotated[
        list[str] | None, Field(description="Arguments for executable")
    ] = None,
    terminal: Annotated[
        bool,
        Field(
            description="If true, executes the command in an interactive terminal window (konsole)"
        ),
    ] = False,
    cwd: Annotated[str | None, Field(description="Working directory")] = None,
    env: Annotated[
        dict[str, str] | None, Field(description="Extra environment variables")
    ] = None,
    focus_if_running: Annotated[
        bool, Field(description="Focus existing window instead of starting another")
    ] = True,
    restart_if_running: Annotated[
        bool, Field(description="Ask existing instance to quit first")
    ] = False,
    wait_for_window_s: Annotated[
        float, Field(description="Wait this long for window; 0 to skip")
    ] = 20.0,
    settle_ms: Annotated[
        int, Field(description="Let the window draw before looking")
    ] = 800,
    screenshot: Annotated[bool, Field(description="Return a picture")] = True,
) -> list:
    """Launch GUI applications or commands in a terminal window.

    Consolidates `run_app` and `run_in_terminal`.
    """
    _check_interact_blocked("run_app")

    if terminal:
        pid = desktop.spawn(
            [
                "konsole",
                "--separate",
                "--hold",
                *(["--workdir", cwd] if cwd else []),
                "-e",
                "bash",
                "-lc",
                executable_or_command,
            ],
            cwd,
        )
        if wait_for_window_s <= 0:
            return [{"pid": pid}]
        window = desktop.wait_for_window_of(pid, wait_for_window_s)
        desktop.settle(settle_ms)
        window = desktop.window_info(window.id)
        return [
            {"pid": pid, "window": window.as_dict()},
            *_picture(window, "terminal", screenshot),
        ]

    target_name = Path(executable_or_command).name
    running = [
        w
        for w in desktop.search_windows(".")
        if (w.cls or "").lower() == target_name.lower()
    ]

    if running and restart_if_running:
        for window in running:
            if window.pid:
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

    pid = desktop.spawn([executable_or_command, *(args or [])], cwd=cwd, env=env)
    if wait_for_window_s <= 0:
        return [{"pid": pid, "windows": []}]
    window = desktop.wait_for_window_of(pid, wait_for_window_s, target_name=target_name)
    desktop.settle(settle_ms)
    window = desktop.window_info(window.id)
    return [
        {"pid": pid, "window": window.as_dict()},
        *_picture(window, "launched", screenshot),
    ]


@server.tool()
def tray(
    pattern: Annotated[
        str | None,
        Field(
            description="Matched against item id and title. Omit or set action='list' to enumerate tray items"
        ),
    ] = None,
    action: Annotated[
        str,
        Field(
            description="list, activate (left click), secondary (middle click), context (right click), or scroll"
        ),
    ] = "list",
    scroll_amount: Annotated[
        int, Field(description="For action='scroll': positive up, negative down")
    ] = 0,
    settle_ms: Annotated[
        int, Field(description="Let menu or window appear, in ms")
    ] = 700,
    screenshot: Annotated[
        bool, Field(description="Return picture after tray action")
    ] = True,
) -> list:
    """Inspect system tray items or interact with them over D-Bus.

    Consolidates `list_tray_items` and `tray_action`.
    """
    if pattern is None or action == "list":
        return [item.as_dict() for item in desktop.tray.items()]

    _check_interact_blocked("tray")
    item = desktop.tray.find(pattern)
    if action == "scroll":
        desktop.tray.scroll(item, scroll_amount)
    else:
        desktop.tray.act(item, action)
    desktop.settle(settle_ms)
    return [item.as_dict(), *_picture(None, "tray", screenshot)]


@server.tool()
def get_process_info(
    target: Annotated[
        str, Field(description="A PID or regex matched against full command line")
    ],
    wait_timeout_s: Annotated[
        float, Field(description="If > 0, waits up to this many seconds for process to exist")
    ] = 0.0,
    include_env: Annotated[
        bool, Field(description="Include environment (credentials blanked)")
    ] = True,
) -> dict:
    """Get complete info about a process: PID, args, environment, windows, and tray items.

    Consolidates `get_process_info` and `wait_for_process`.
    """
    if wait_timeout_s > 0:
        pid = desktop.wait_for_process(target, wait_timeout_s)
        desktop.settle(200)
        target = str(pid)

    return desktop.process_report(target, include_env=include_env).as_dict()


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
