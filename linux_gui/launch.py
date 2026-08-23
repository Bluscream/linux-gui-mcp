"""Starting programs in the desktop session."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from . import windows
from .shell import DesktopError, session_env
from .windows import Window


def spawn(
    command: list[str],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """Start a program in the desktop session and let go of it.

    Detached on purpose: the point is a program that outlives this call, so
    that a client which stops asking does not take the window with it.
    """
    if not command:
        raise DesktopError("no command given")
    if cwd is not None and not Path(cwd).is_dir():
        raise DesktopError(f"working directory {cwd!r} does not exist")
    if shutil.which(command[0]) is None and not Path(command[0]).exists():
        raise DesktopError(f"{command[0]!r} is not on PATH and is not a file")
    child = subprocess.Popen(
        command,
        cwd=cwd,
        env={**session_env(), **(env or {})},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return child.pid


def windows_of(pid: int) -> list[Window]:
    """Every window belonging to a process."""
    return [window for window in windows.search_windows(".") if window.pid == pid]


def wait_for_window_of(pid: int, timeout: float = 20.0, poll: float = 0.3) -> Window:
    """Wait for a process to put a window on screen.

    A process existing and a process having a window are different facts, and
    the gap between them is where driving it fails confusingly.
    """
    deadline = time.monotonic() + timeout
    while True:
        windows = windows_of(pid)
        if windows:
            return windows[0]
        if not Path(f"/proc/{pid}").exists():
            raise DesktopError(
                f"process {pid} exited before it showed a window - "
                "it probably failed to start; run it in a terminal to see why"
            )
        if time.monotonic() >= deadline:
            raise DesktopError(f"process {pid} showed no window within {timeout}s")
        time.sleep(poll)
