"""What a process is, read from /proc.

Read directly rather than by shelling out to ps, because the interesting parts
- the argument vector and the environment - are NUL-separated lists that ps
flattens into a single string. Recovering them from that is guesswork the
moment an argument contains a space.
"""

from __future__ import annotations

import os
import pwd
import re
from dataclasses import dataclass, field
from pathlib import Path

from .shell import DesktopError, run

_PROC = Path("/proc")

# Names whose values are withheld. Matched loosely on purpose: the cost of
# withholding one harmless variable is nothing, and the cost of printing one
# secret is that it has to be rotated.
_SECRET_NAME = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|_PW$|_PASS$|APIKEY|API_KEY|CREDENTIAL"
    r"|AUTH|SESSION|COOKIE|PRIVATE|SIGNING|CERT|LICENSE)",
    re.IGNORECASE,
)
# Values that look like a credential whatever they are called, since plenty
# are not named helpfully.
_SECRET_VALUE = re.compile(
    r"^(gh[pousr]_|sk-|xox[baprs]-|ey[A-Za-z0-9_-]{16,}\.|dckr_pat_)"
)


def redact_env(env: dict[str, str]) -> dict[str, str]:
    """Blank the values that look like credentials.

    Applied by default rather than on request. An environment is one of the
    densest concentrations of secrets on a machine, and a tool that hands the
    whole thing to a language model is one leak away from a rotation.
    """
    redacted = {}
    for name, value in env.items():
        if _SECRET_NAME.search(name) or _SECRET_VALUE.match(value):
            redacted[name] = "[redacted]"
        else:
            redacted[name] = value
    return redacted


def _read_nul_list(path: Path) -> list[str]:
    try:
        raw = path.read_bytes()
    except (OSError, PermissionError):
        return []
    return [part for part in raw.decode("utf-8", "replace").split("\0") if part]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except (OSError, PermissionError):
        return ""


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int | None
    name: str
    exe: str
    cmdline: list[str]
    cwd: str
    user: str
    state: str
    threads: int
    rss_kb: int
    env: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "name": self.name,
            "exe": self.exe,
            "cmdline": self.cmdline,
            "cwd": self.cwd,
            "user": self.user,
            "state": self.state,
            "threads": self.threads,
            "rss_kb": self.rss_kb,
            "env": self.env,
        }


def exists(pid: int) -> bool:
    return (_PROC / str(pid)).is_dir()


def read(pid: int, include_env: bool = True) -> Process:
    """Everything readable about one process.

    Fields this user may not read come back empty rather than raising: a
    process owned by somebody else is a normal thing to ask about, and half an
    answer is more useful than an error.
    """
    root = _PROC / str(pid)
    if not root.is_dir():
        raise DesktopError(f"no process {pid} is running")

    status = {}
    for line in _read_text(root / "status").splitlines():
        key, _, value = line.partition(":")
        status[key.strip()] = value.strip()

    try:
        owner = pwd.getpwuid((root.stat()).st_uid).pw_name
    except (KeyError, OSError):
        owner = str(status.get("Uid", "").split("\t")[0] or "?")

    try:
        exe = os.readlink(root / "exe")
    except OSError:
        exe = ""
    try:
        cwd = os.readlink(root / "cwd")
    except OSError:
        cwd = ""

    env: dict[str, str] = {}
    if include_env:
        for entry in _read_nul_list(root / "environ"):
            name, _, value = entry.partition("=")
            env[name] = value
        env = redact_env(env)

    def _int(field_name: str) -> int:
        digits = re.sub(r"\D", "", status.get(field_name, "") or "")
        return int(digits) if digits else 0

    return Process(
        pid=pid,
        ppid=_int("PPid") or None,
        name=status.get("Name", ""),
        exe=exe,
        cmdline=_read_nul_list(root / "cmdline"),
        cwd=cwd,
        user=owner,
        state=status.get("State", ""),
        threads=_int("Threads"),
        rss_kb=_int("VmRSS"),
        env=env,
    )


def find(pattern: str) -> list[int]:
    """Pids whose command line matches, newest last."""
    try:
        found = run(["pgrep", "-f", pattern], timeout=10.0)
    except DesktopError:
        # pgrep exits non-zero when nothing matches, which is an answer rather
        # than a failure.
        return []
    return [int(line) for line in found.split() if line.isdigit()]


def resolve(target: int | str) -> int:
    """A pid from either a pid or a pattern.

    Refuses an ambiguous pattern rather than picking one: several matches is a
    question the caller has to answer, and guessing would report on whichever
    process happened to start first.
    """
    if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
        pid = int(target)
        if not exists(pid):
            raise DesktopError(f"no process {pid} is running")
        return pid

    matches = find(target)
    # Drop the search itself, which matches its own pattern.
    matches = [pid for pid in matches if pid != os.getpid()]
    if not matches:
        raise DesktopError(f"no process matching {target!r}")

    # A pattern matches anything whose command line mentions it, which for a
    # long-running program is usually its own children as well. When exactly
    # one process is *named* this, that is plainly the one meant, and refusing
    # would be pedantry rather than caution.
    named = [pid for pid in matches if read(pid, include_env=False).name == target]
    if len(named) == 1:
        return named[0]
    if len(matches) > 1:
        described = ", ".join(
            f"{pid} ({read(pid, include_env=False).name})" for pid in matches[:6]
        )
        raise DesktopError(
            f"{target!r} matches several processes: {described}. Pass a pid instead."
        )
    return matches[0]
