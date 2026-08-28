#!/usr/bin/env python3
"""Check that the pointer lands where it is sent.

Not a unit test: it drives the real pointer on the real desktop, because the
thing being checked is the interaction between ydotool, libinput's
acceleration curve and the compositor's clamping - none of which a fake would
reproduce, and all three of which have been wrong here at some point.

Run it after touching anything in `move_mouse`:

    python3 check_pointer.py

It moves the pointer around for a few seconds and puts it back.
"""

from __future__ import annotations

import random
import sys
import time

import linux_gui.input as pointer
from linux_gui.input import (
    POINTER_SCALE_BOUNDS,
    POINTER_TOLERANCE,
    _read_pointer,
    _screen_extent,
    move_mouse,
)


def accuracy(extent: tuple[int, int]) -> list[str]:
    """Every corner, every edge, and a spread of interior points."""
    width, height = extent
    targets = [
        (5, 5),
        (width - 5, 5),
        (5, height - 5),
        (width - 5, height - 5),
        (width // 2, 5),
        (5, height // 2),
        (width // 2, height // 2),
    ]
    random.seed(7)
    targets += [
        (random.randint(5, width - 5), random.randint(5, height - 5)) for _ in range(15)
    ]

    failures = []
    for target in targets:
        landed = move_mouse(*target)
        off = max(abs(landed[0] - target[0]), abs(landed[1] - target[1]))
        if off > POINTER_TOLERANCE:
            failures.append(f"asked {target}, landed {landed}, off by {off}px")
    return failures


def recovery(extent: tuple[int, int]) -> list[str]:
    """The loop must climb out of a wrong scale, not oscillate on it.

    This is the regression: a scale far too small threw the pointer into the
    corner, every reading after it was clamped there, and the correction
    never recovered. Every value here reproduced that.
    """
    low, high = POINTER_SCALE_BOUNDS
    target = (extent[0] // 3, extent[1] // 3)

    failures = []
    for poison in (low, 0.5, 1.0, 4.0, high):
        pointer._CACHED_SCALE = (poison, poison)
        landed = move_mouse(*target)
        off = max(abs(landed[0] - target[0]), abs(landed[1] - target[1]))
        if off > POINTER_TOLERANCE:
            failures.append(f"scale {poison}: landed {landed}, off by {off}px")
    pointer._CACHED_SCALE = None
    return failures


def edges_are_detected(extent: tuple[int, int]) -> list[str]:
    """The pointer rests at 1, not 0, so `<= 0` never fires.

    A move clamped against the left or top edge read as a clean one, and the
    scale learned from it was wrong in the direction that overshoots.
    """
    failures = []
    if not pointer._clamped(1, 500, extent[0]):
        failures.append("resting at x=1 after moving is not being seen as clamped")
    if not pointer._clamped(extent[0], 500, extent[0]):
        failures.append("resting at the right edge is not being seen as clamped")
    if pointer._clamped(500, 500, extent[0]):
        failures.append("not having moved is being reported as clamped")
    if pointer._clamped(extent[0] // 2, 500, extent[0]):
        failures.append("a point in open screen is being reported as clamped")
    return failures


def main() -> int:
    start = _read_pointer()
    extent = _screen_extent()
    print(f"screen extent {extent}, tolerance {POINTER_TOLERANCE}px")

    checks = {
        "edge detection": lambda: edges_are_detected(extent),
        "accuracy": lambda: accuracy(extent),
        "recovery from a wrong scale": lambda: recovery(extent),
    }

    bad = 0
    for name, check in checks.items():
        began = time.time()
        failures = check()
        took = time.time() - began
        if failures:
            bad += len(failures)
            print(f"FAIL {name} ({took:.1f}s)")
            for line in failures:
                print(f"       {line}")
        else:
            print(f"ok   {name} ({took:.1f}s)")

    move_mouse(*start)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
