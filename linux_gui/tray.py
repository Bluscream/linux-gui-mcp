"""The system tray, over D-Bus.

Read and driven through `StatusNotifierItem` rather than by clicking pixels.
The tray is a row of identical little squares, so recognising one by sight is
guesswork - and only the panel knows where each sits, so a positional click
goes somewhere else the moment the tray reorders.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache

from .shell import DesktopError, run, which

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_INTERFACE = "org.kde.StatusNotifierItem"

# What an item can be asked to do, in the words a caller would use.
ACTIONS = {
    "activate": "Activate",
    "secondary": "SecondaryActivate",
    "context": "ContextMenu",
}


def _qdbus(args: list[str]) -> str:
    return run([which("qdbus", "qdbus6", "qdbus-qt6"), *args]).strip()


@lru_cache(maxsize=1)
def _connection_owners() -> dict[str, int]:
    """Which process owns each D-Bus connection.

    Tray items are registered under a connection's unique name, which says
    nothing about who they belong to. This is the only link between an item on
    the panel and a process, and it is what lets a process report its own tray
    icons rather than the caller matching them up by name and hoping.
    """
    owners: dict[str, int] = {}
    try:
        listing = run([which("busctl"), "--user", "list", "--no-pager"], timeout=15.0)
    except DesktopError:
        return owners
    for line in listing.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            owners[parts[0]] = int(parts[1])
    return owners


@dataclass(frozen=True)
class TrayItem:
    service: str
    path: str
    id: str
    title: str
    status: str
    icon: str
    category: str
    pid: int | None

    @property
    def address(self) -> str:
        return f"{self.service}{self.path}"

    @property
    def actions(self) -> list[str]:
        """What this item can be asked to do.

        Every item accepts all three; the names are listed so a caller does
        not have to know the D-Bus spelling of a middle click.
        """
        return sorted(ACTIONS)

    def as_dict(self) -> dict:
        return {
            "address": self.address,
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "icon": self.icon,
            "category": self.category,
            "pid": self.pid,
            "actions": self.actions,
        }


_WANTED = ("Id", "Title", "Status", "IconName", "Category")


def _properties(service: str, path: str) -> dict[str, str]:
    """Read every property this cares about in one call.

    `busctl get-property` takes several names at once and answers in order,
    which turns five round trips per item into one. Read individually they
    cost about 50ms an item; across a full tray that was most of a second for
    information that fits on one line.

    Not `GetAll`, which would also drag back the icon pixmap - several
    thousand bytes of image per item, none of it wanted here.
    """
    try:
        answered = run(
            [
                which("busctl"),
                "--user",
                "--json=short",
                "get-property",
                service,
                path,
                ITEM_INTERFACE,
                *_WANTED,
            ],
            timeout=10.0,
        )
    except DesktopError:
        # Items differ in which properties they publish, and one that refuses
        # the batch is reported as itself rather than not at all.
        return {}

    values: dict[str, str] = {}
    for name, line in zip(_WANTED, answered.strip().splitlines(), strict=False):
        try:
            values[name] = str(json.loads(line).get("data", ""))
        except json.JSONDecodeError:
            values[name] = ""
    return values


def items() -> list[TrayItem]:
    """Everything in the tray, with its identity and owning process."""
    raw = _qdbus(
        [WATCHER, "/StatusNotifierWatcher", f"{WATCHER}.RegisteredStatusNotifierItems"]
    )
    owners = _connection_owners()
    found = []
    for entry in raw.splitlines():
        entry = entry.strip()
        if not entry:
            continue
        # "service/object/path" - a bus name never contains a slash, so the
        # first one starts the object path.
        service, _, path = entry.partition("/")
        path = f"/{path}"
        properties = _properties(service, path)
        found.append(
            TrayItem(
                service=service,
                path=path,
                id=properties.get("Id", ""),
                title=properties.get("Title", ""),
                status=properties.get("Status", ""),
                icon=properties.get("IconName", ""),
                category=properties.get("Category", ""),
                pid=owners.get(service),
            )
        )
    return found


def items_of(pid: int) -> list[TrayItem]:
    """The tray icons belonging to one process."""
    return [item for item in items() if item.pid == pid]


def find(pattern: str) -> TrayItem:
    """One item, by id, title or address.

    Refuses an ambiguous match rather than taking the first: two matches is a
    question the caller has to answer, and guessing would eventually activate
    the wrong application.
    """
    wanted = re.compile(pattern, re.IGNORECASE)
    present = items()
    matches = [
        item
        for item in present
        if wanted.search(item.id or "")
        or wanted.search(item.title or "")
        or wanted.search(item.address)
    ]
    if not matches:
        known = ", ".join(item.id or item.address for item in present) or "none"
        raise DesktopError(f"no tray item matching {pattern!r}. Present: {known}")
    if len(matches) > 1:
        names = ", ".join(item.id or item.address for item in matches)
        raise DesktopError(f"{pattern!r} matches several tray items: {names}")
    return matches[0]


def act(item: TrayItem, action: str = "activate", x: int = 0, y: int = 0) -> None:
    """Activate an item, or open its menu."""
    method = ACTIONS.get(action.lower())
    if method is None:
        raise DesktopError(
            f"unknown tray action {action!r}; use {', '.join(sorted(ACTIONS))}"
        )
    _qdbus(
        [
            item.service,
            item.path,
            f"{ITEM_INTERFACE}.{method}",
            str(int(x)),
            str(int(y)),
        ]
    )


def scroll(item: TrayItem, delta: int, orientation: str = "vertical") -> None:
    """Scroll on an item - volume applets use this."""
    if orientation not in ("vertical", "horizontal"):
        raise DesktopError("orientation must be vertical or horizontal")
    _qdbus(
        [
            item.service,
            item.path,
            f"{ITEM_INTERFACE}.Scroll",
            str(int(delta)),
            orientation,
        ]
    )
