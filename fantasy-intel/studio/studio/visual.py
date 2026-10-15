"""Shared look: the dashboard's dark palette (frontend/app/globals.css) and bundled Inter (OFL-1.1).

No logos, photos or game footage anywhere: every pixel is text, shapes or charts drawn from our data.
"""

from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .core import FONTS

BG = "#0a0d12"
SURFACE = "#11151b"
SURFACE_RAISED = "#171c24"
LINE = "#252d38"
INK = "#e9eef4"
INK_MUTED = "#a3adba"
INK_FAINT = "#8a95a5"
ACCENT = "#6aaeff"
POSITIVE = "#5ad394"
NEGATIVE = "#ff8173"
CAUTION = "#f2bd52"

WEIGHTS = {"regular": "Inter-Regular.ttf", "semibold": "Inter-SemiBold.ttf", "bold": "Inter-Bold.ttf",
           "black": "Inter-Black.ttf"}
BAND_COLOR = {"favorable": POSITIVE, "tough": NEGATIVE, "middle-of-the-pack": INK_FAINT}

# Everything is laid out on a 1920x1080 grid and rendered at SCALE for a clean slow zoom.
SCALE = 2


@lru_cache(maxsize=None)
def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / WEIGHTS[weight]), size * SCALE)


class Canvas:
    """A 1920x1080 (or 1080x1920) layout in logical pixels, drawn at SCALE."""

    def __init__(self, size: tuple[int, int], bg: str = BG):
        self.w, self.h = size
        self.img = Image.new("RGB", (self.w * SCALE, self.h * SCALE), bg)
        self.d = ImageDraw.Draw(self.img)

    def text(self, xy, text: str, weight: str = "regular", size: int = 40, fill: str = INK, anchor: str = "la"):
        self.d.text((xy[0] * SCALE, xy[1] * SCALE), text, font=font(weight, size), fill=fill, anchor=anchor)

    def text_width(self, text: str, weight: str, size: int) -> float:
        return self.d.textlength(text, font=font(weight, size)) / SCALE

    def rect(self, box, fill: str | None = None, outline: str | None = None, width: int = 2, radius: int = 14):
        self.d.rounded_rectangle([v * SCALE for v in box], radius=radius * SCALE, fill=fill, outline=outline,
                                 width=width * SCALE)

    def line(self, xy, fill: str = LINE, width: int = 2):
        self.d.line([v * SCALE for v in xy], fill=fill, width=width * SCALE)

    def fit_text(self, text: str, weight: str, size: int, max_width: float) -> int:
        while size > 12 and self.text_width(text, weight, size) > max_width:
            size -= 2
        return size

    def footer(self, left: str, right: str = ""):
        self.line((80, self.h - 84, self.w - 80, self.h - 84))
        self.text((80, self.h - 52), left, "semibold", 26, INK_FAINT, "lm")
        if right:
            self.text((self.w - 80, self.h - 52), right, "regular", 24, INK_FAINT, "rm")

    def save(self, path) -> None:
        self.img.save(path, optimize=True)


def wrap(text: str, max_chars: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > max_chars:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return lines
