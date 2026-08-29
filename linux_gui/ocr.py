"""OCR (Optical Character Recognition) for desktop automation.

Finds text within windows or full screen screenshots, returns bounding boxes,
centers, and confidence scores.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .windows import Window

from .capture import screenshot, settle
from .shell import DesktopError


@dataclass(frozen=True)
class OCRMatch:
    """A match found on screen via OCR."""

    text: str
    x: int
    y: int
    width: int
    height: int
    center_x: int
    center_y: int
    confidence: float

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "center_x": self.center_x,
            "center_y": self.center_y,
            "confidence": self.confidence,
        }


def _tesseract_bin() -> str:
    """Locate tesseract executable."""
    found = shutil.which("tesseract") or shutil.which("/var/home/linuxbrew/.linuxbrew/bin/tesseract")
    if not found:
        raise DesktopError(
            "tesseract is not installed; install it with 'brew install tesseract'"
        )
    return found


def extract_text_boxes(image_path: Path) -> list[OCRMatch]:
    """Run OCR on an image and return all detected text boxes with coordinates."""
    import pytesseract
    from PIL import Image

    tess_cmd = _tesseract_bin()
    pytesseract.pytesseract.tesseract_cmd = tess_cmd

    img = Image.open(image_path)
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

    matches: list[OCRMatch] = []
    n_boxes = len(data["text"])
    for i in range(n_boxes):
        text = data["text"][i].strip()
        conf = float(data["conf"][i])
        if not text or conf < 0:
            continue
        x = int(data["left"][i])
        y = int(data["top"][i])
        w = int(data["width"][i])
        h = int(data["height"][i])
        cx = x + (w // 2)
        cy = y + (h // 2)
        matches.append(
            OCRMatch(
                text=text,
                x=x,
                y=y,
                width=w,
                height=h,
                center_x=cx,
                center_y=cy,
                confidence=conf,
            )
        )
    return matches


def find_text_on_screen(
    pattern: str,
    window: Window | None = None,
    tries: int = 3,
    retry_delay_s: float = 0.5,
    exact_match: bool = False,
    case_sensitive: bool = False,
    match_index: int = -1,
) -> OCRMatch:
    """Find text on screen or inside a window, retrying if necessary.

    - pattern: substring, phrase (e.g. 'Type / for commands'), or regex.
    - match_index: which occurrence to pick if multiple match (default -1 = last occurrence on screen, e.g. bottom-most/newest).
    - Returns the match with its bounding box and center coordinates (relative to window if given).
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    regex = re.compile(pattern if not exact_match else f"^{re.escape(pattern)}$", flags)

    with tempfile.TemporaryDirectory() as tmpdir:
        shot_path = Path(tmpdir) / "ocr_shot.png"
        last_found_texts: list[str] = []

        for attempt in range(max(1, tries)):
            if attempt > 0:
                time.sleep(retry_delay_s)

            screenshot(shot_path, window)
            boxes = extract_text_boxes(shot_path)
            last_found_texts = [b.text for b in boxes]

            candidates: list[OCRMatch] = []

            # 1. Multi-word phrase search across contiguous word sequence
            phrase_words = pattern.strip().split()
            if len(phrase_words) > 1:
                for i in range(len(boxes) - len(phrase_words) + 1):
                    slice_boxes = boxes[i : i + len(phrase_words)]
                    # Check if boxes are roughly on the same line (y difference < 20px)
                    if max(b.y for b in slice_boxes) - min(b.y for b in slice_boxes) > 25:
                        continue
                    slice_text = " ".join(b.text for b in slice_boxes)
                    if regex.search(slice_text):
                        min_x = min(b.x for b in slice_boxes)
                        min_y = min(b.y for b in slice_boxes)
                        max_x = max(b.x + b.width for b in slice_boxes)
                        max_y = max(b.y + b.height for b in slice_boxes)
                        avg_conf = sum(b.confidence for b in slice_boxes) / len(slice_boxes)
                        candidates.append(
                            OCRMatch(
                                text=slice_text,
                                x=min_x,
                                y=min_y,
                                width=max_x - min_x,
                                height=max_y - min_y,
                                center_x=min_x + ((max_x - min_x) // 2),
                                center_y=min_y + ((max_y - min_y) // 2),
                                confidence=avg_conf,
                            )
                        )

            # 2. Single token matching if no multi-word candidate found
            if not candidates:
                for box in boxes:
                    if regex.search(box.text):
                        candidates.append(box)

            if candidates:
                # Sort candidates by top-to-bottom vertical position (y coordinate)
                candidates.sort(key=lambda m: (m.y, m.x))
                # Select requested occurrence (default -1 = bottom-most / latest occurrence)
                idx = match_index if match_index < len(candidates) else -1
                return candidates[idx]

        preview = ", ".join(repr(t) for t in last_found_texts[:10])
        raise DesktopError(
            f"OCR could not find text matching {pattern!r} after {tries} tries. "
            f"Detected text on screen included: [{preview}]"
        )
