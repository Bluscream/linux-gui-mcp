"""Running the programs this package leans on.

Every module here works by calling something else - ydotool, kdotool,
spectacle, qdbus - so the awkward parts of doing that live in one place: a
missing binary, a hung call, a non-zero exit with the reason on stderr.
"""

from __future__ import annotations

import shutil
import subprocess
from functools import cache


class DesktopError(RuntimeError):
    """Something the desktop refused, phrased for whoever asked.

    One exception type rather than several, because every caller does the same
    thing with it: report it and stop. A hierarchy would suggest a choice that
    nothing here actually makes.
    """


@cache
def session_env() -> dict[str, str]:
    """The environment a program needs to find the desktop session.

    Filled in rather than inherited, because this server is started by an MCP
    client and may inherit almost nothing - no DBUS_SESSION_BUS_ADDRESS, no
    WAYLAND_DISPLAY. Every helper here talks to the session over one of those,
    and without them they do not fail helpfully: kdotool tries to autolaunch
    its own D-Bus daemon and dies complaining about X11.

    Defaults rather than guesses: these are where a single-seat systemd
    session puts them, which is the case this runs in.
    """
    import os

    env = dict(os.environ)
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env["XDG_RUNTIME_DIR"] = runtime
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    env.setdefault("DISPLAY", ":0")
    return env


def run(argv: list[str], timeout: float = 20.0) -> str:
    """Run a program and return its output, or raise with the reason."""
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=session_env(),
        )
    except FileNotFoundError as missing:
        raise DesktopError(f"{argv[0]} is not installed") from missing
    except subprocess.TimeoutExpired as slow:
        raise DesktopError(f"{argv[0]} did not finish within {timeout}s") from slow
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip() or f"exit {done.returncode}"
        raise DesktopError(f"{argv[0]} failed: {detail}")
    return done.stdout


@cache
def which(*names: str) -> str:
    """The first of these that exists, or a message naming all of them.

    Cached because it is asked on every call and the answer cannot change
    within a run - a binary appearing mid-session is not a case worth paying
    for a filesystem lookup on every keystroke.
    """
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    raise DesktopError(f"none of {', '.join(names)} is installed")
