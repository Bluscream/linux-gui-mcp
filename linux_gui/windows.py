"""Finding and moving windows, through KWin's own scripting interface.

Nothing else can enumerate Wayland windows: there is no X server to ask, and
the compositor is the only thing that knows what is on screen."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .shell import DesktopError, run, which


@dataclass(frozen=True)
class Window:
    id: str
    title: str
    cls: str
    pid: int | None
    x: int
    y: int
    width: int
    height: int

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "class": self.cls,
            "pid": self.pid,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


def _kdotool(args: list[str]) -> str:
    return run([which("kdotool"), *args]).strip()


_POSITION = re.compile(r"Position:\s*([-\d.]+),([-\d.]+)")
_GEOMETRY = re.compile(r"Geometry:\s*([\d.]+)x([\d.]+)")


def window_exists(window_id: str) -> bool:
    """Whether the compositor still knows this window.

    Judged on whether the geometry parses, not on the exit code: kdotool
    answers a made-up window id with a success and a line that says nothing,
    so trusting the status would report every closed window as open.
    """
    if not window_id or not window_id.strip():
        return False
    try:
        raw = _kdotool(["getwindowgeometry", window_id])
    except DesktopError:
        return False
    return bool(_POSITION.search(raw) and _GEOMETRY.search(raw))


def window_info(window_id: str) -> Window:
    if not window_id or not window_id.strip():
        raise DesktopError("no window id given")
    try:
        raw = _kdotool(["getwindowgeometry", window_id])
    except DesktopError as gone:
        # Named rather than passed through: kdotool's own message for a window
        # that has closed says nothing about which window, and a closed window
        # is the common case rather than an exotic one.
        raise DesktopError(
            f"no window {window_id!r} - it may have closed; "
            "call list_windows or find_window for current ids"
        ) from gone
    position = _POSITION.search(raw)
    geometry = _GEOMETRY.search(raw)
    if not position or not geometry:
        raise DesktopError(f"could not read the geometry of {window_id}")

    def _int(value: str) -> int:
        return int(round(float(value)))

    try:
        pid: int | None = int(_kdotool(["getwindowpid", window_id]))
    except (DesktopError, ValueError):
        pid = None
    try:
        title = _kdotool(["getwindowname", window_id])
    except DesktopError:
        title = ""
    try:
        cls = _kdotool(["getwindowclassname", window_id])
    except DesktopError:
        cls = ""
    return Window(
        id=window_id,
        title=title,
        cls=cls,
        pid=pid,
        x=_int(position.group(1)),
        y=_int(position.group(2)),
        width=_int(geometry.group(1)),
        height=_int(geometry.group(2)),
    )


def search_windows(pattern: str) -> list[Window]:
    found = _kdotool(["search", pattern])
    windows = []
    for line in found.splitlines():
        line = line.strip()
        if line:
            try:
                windows.append(window_info(line))
            except DesktopError:
                # A window that vanished between the search and the query is
                # not an error; it is a window that closed.
                continue
    return windows


def active_window() -> Window:
    return window_info(_kdotool(["getactivewindow"]))


def focus_window(window_id: str) -> None:
    _kdotool(["windowactivate", window_id])


def wait_for_window(pattern: str, timeout: float = 15.0, poll: float = 0.25) -> Window:
    deadline = time.monotonic() + timeout
    while True:
        matches = search_windows(pattern)
        if matches:
            return matches[0]
        if time.monotonic() >= deadline:
            raise DesktopError(f"no window matching {pattern!r} after {timeout}s")
        time.sleep(poll)


def wait_for_process(pattern: str, timeout: float = 15.0, poll: float = 0.25) -> int:
    """Wait until a process matching this exists, and return its pid.

    The search itself lives in `processes`, which is the module that knows
    what a process is. Imported here rather than at the top because that
    module reads windows back for its own reports, and the two would otherwise
    import each other at load time.
    """
    from . import processes

    deadline = time.monotonic() + timeout
    while True:
        found = processes.find(pattern)
        if found:
            return found[0]
        if time.monotonic() >= deadline:
            raise DesktopError(f"no process matching {pattern!r} after {timeout}s")
        time.sleep(poll)
