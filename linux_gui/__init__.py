"""Driving a KDE/Wayland desktop.

Split by what each part talks to rather than by what it does, because the
awkwardness is always in the thing being talked to: uinput for input, KWin for
windows, spectacle for pictures, D-Bus for the tray, /proc for processes. Each
module owns one of those conversations and the workarounds it needs.

Everything is re-exported here so a caller writes `linux_gui.click(...)`
without needing to know which of them answers.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import capture, input, launch, processes, tray, windows
from .capture import screenshot, settle
from .input import (
    click,
    drag,
    ensure_input_daemon,
    move_mouse,
    press,
    scroll,
    type_text,
)
from .launch import spawn, wait_for_window_of, windows_of
from .processes import Process
from .shell import DesktopError
from .tray import TrayItem
from .windows import (
    Window,
    active_window,
    focus_window,
    move_window,
    search_windows,
    wait_for_process,
    wait_for_window,
    window_exists,
    window_info,
)

__all__ = [
    "DesktopError",
    "Process",
    "ProcessReport",
    "TrayItem",
    "Window",
    "active_window",
    "capture",
    "click",
    "drag",
    "ensure_input_daemon",
    "focus_window",
    "input",
    "launch",
    "move_mouse",
    "move_window",
    "press",
    "process_report",
    "processes",
    "screenshot",
    "scroll",
    "search_windows",
    "settle",
    "spawn",
    "tray",
    "type_text",
    "wait_for_process",
    "wait_for_window",
    "wait_for_window_of",
    "window_exists",
    "window_info",
    "windows",
    "windows_of",
]


@dataclass(frozen=True)
class ProcessReport:
    """A process, and everything it put on the desktop.

    One answer rather than three calls, because the three are only useful
    together: a pid says a program is running, its windows say whether it got
    as far as showing anything, and its tray icons say how to reach it when it
    has no window at all. Asked separately, the caller has to know that a
    program with no window might still be running in the tray.
    """

    process: Process
    windows: list[Window]
    tray_items: list[TrayItem]

    def as_dict(self) -> dict:
        return {
            **self.process.as_dict(),
            "windows": [window.as_dict() for window in self.windows],
            "tray_items": [item.as_dict() for item in self.tray_items],
        }


def process_report(target: int | str, include_env: bool = True) -> ProcessReport:
    """Everything about a process and what it shows on screen.

    The windows are found by pid rather than by name, so a program with
    several windows reports all of them, and one whose title has changed is
    still recognised.
    """
    pid = processes.resolve(target)
    return ProcessReport(
        process=processes.read(pid, include_env=include_env),
        windows=launch.windows_of(pid),
        tray_items=tray.items_of(pid),
    )
