"""Apple Vision on this Mac, through small Swift helpers compiled once: reading text, finding people, cutting out a
subject. On device and free. Each caller decides what a failure means; here a helper that cannot run raises."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .core import ASSETS

BIN = ASSETS / "media" / ".bin"            # git-ignored, shared with media_sources.py


def binary(name: str) -> Path:
    src = Path(__file__).with_name(f"{name}.swift")
    out = BIN / name
    if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
        BIN.mkdir(parents=True, exist_ok=True)
        subprocess.run(["swiftc", "-O", str(src), "-o", str(out)], check=True, capture_output=True)
    return out


def read_text(paths: list[Path]) -> dict[str, list[str]]:
    """Every line of text Vision finds in each image, keyed by its path."""
    raw = subprocess.run([str(binary("vision_text")), *map(str, paths)], capture_output=True, text=True, check=True)
    return json.loads(raw.stdout)


def people(path: Path) -> dict[str, list]:
    """Faces and human bodies in an image: boxes as left, top, right, bottom, confidence."""
    raw = subprocess.run([str(binary("vision_subject")), str(path)], capture_output=True, text=True, check=True)
    return json.loads(raw.stdout)


def subject_mask(path: Path, out: Path) -> bool:
    """Write the image's foreground subject as a mask. False when Vision finds none: the still moves as one layer."""
    proc = subprocess.run([str(binary("vision_mask")), str(path), str(out)], capture_output=True)
    if proc.returncode not in (0, 2):
        raise RuntimeError(f"vision_mask failed: {proc.stderr.decode()[-300:]}")
    return proc.returncode == 0
