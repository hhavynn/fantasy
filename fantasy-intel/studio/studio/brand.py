"""Channel identity for Fantasy Intelligence: the mark, the profile picture and the YouTube banner.

    uv run python -m studio.brand            # writes assets/brand/*.png

The mark is a football drawn as a lens with three rising bars for laces: the ball and the usage trend in one
shape. It is drawn from code in the premium style's palette and Inter (OFL), at 4x and downsampled, so it is
original and reproducible. No team marks, no NFL marks.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .core import ASSETS
from .graphics import kit

OUT = ASSETS / "brand"
NAME = "FANTASY INTELLIGENCE"
TAGLINE = "Usage, not box scores."
BG = kit.hexrgb("#0C0F13")
PANEL = kit.hexrgb("#151B22")
GOLD = kit.hexrgb("#F3C350")
INK = kit.hexrgb("#F1F3F6")
MUTED = kit.hexrgb("#98A2AF")
SS = 4                                                # supersampling


def _lens(cx: float, cy: float, w: float, h: float, n: int = 160) -> list[tuple[float, float]]:
    """A football outline: two arcs meeting in points, a little fuller than a plain ellipse."""
    a, b = w / 2, h / 2
    xs = np.linspace(-a, a, n)
    top = [(cx + x, cy - b * (1 - (x / a) ** 2) ** 0.72) for x in xs]
    bot = [(cx + x, cy + b * (1 - (x / a) ** 2) ** 0.72) for x in xs[::-1]]
    return top + bot


def mark(size: int, ball=GOLD, bars=INK) -> Image.Image:
    """The mark on a transparent square: a tilted football outline with three rising bars for laces."""
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy, w, h = S / 2, S / 2, S * 0.92, S * 0.56
    stroke = S * 0.075
    outer = _lens(cx, cy, w, h)
    inner = _lens(cx, cy, w - stroke * 2.6, h - stroke * 2)
    d.polygon(outer, fill=ball)
    d.polygon(inner, fill=(0, 0, 0, 0))
    # three bars rising left to right, the lace line along their base
    bw, gap = S * 0.085, S * 0.05
    base = cy + h * 0.17
    heights = [h * 0.20, h * 0.32, h * 0.46]
    x0 = cx - (3 * bw + 2 * gap) / 2
    for i, bh in enumerate(heights):
        x = x0 + i * (bw + gap)
        d.rounded_rectangle((x, base - bh, x + bw, base), radius=bw * 0.18, fill=bars)
    d.rounded_rectangle((x0 - gap * 0.9, base + S * 0.02, x0 + 3 * bw + 2 * gap + gap * 0.9, base + S * 0.02 + stroke * 0.55),
                        radius=stroke * 0.27, fill=bars)
    img = img.rotate(-24, resample=Image.BICUBIC, center=(cx, cy))
    return img.resize((size, size), Image.LANCZOS)


def _glow_bg(w: int, h: int, cx: float, cy: float) -> Image.Image:
    """The premium set: near-black with one soft key light, no texture."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = np.sqrt(((xx - cx) / max(w, h)) ** 2 + ((yy - cy) / max(w, h)) ** 2)
    base = np.array(BG, np.float32)[None, None] + (np.clip(1 - r * 2.2, 0, 1) ** 2)[..., None] * 18
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")


def profile(size: int = 800) -> Image.Image:
    """Profile picture: the mark alone, large, inside the circle every platform crops to."""
    img = _glow_bg(size, size, size * 0.42, size * 0.38)
    m = mark(int(size * 0.74))
    img.paste(m, ((size - m.width) // 2, (size - m.height) // 2), m)
    return img


def _tracked(d: ImageDraw.ImageDraw, xy, text: str, f, fill, track: float):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=f, fill=fill)
        x += f.getlength(ch) + track


def _tracked_width(text: str, f, track: float) -> float:
    return sum(f.getlength(ch) + track for ch in text) - track


def banner(w: int = 2560, h: int = 1440) -> Image.Image:
    """YouTube banner. Everything that matters sits in the 1546x423 centre that shows on every device."""
    img = _glow_bg(w, h, w * 0.5, h * 0.5)
    d = ImageDraw.Draw(img, "RGBA")
    # faint chart grid across the full width (what TVs show), quiet enough to sit behind the wordmark
    for y in range(int(h * 0.5) - 420, int(h * 0.5) + 421, 70):
        d.line((0, y, w, y), fill=(255, 255, 255, 7), width=2)
    safe_w, safe_h = 1546, 423
    sx, sy = (w - safe_w) / 2, (h - safe_h) / 2
    m = mark(300)
    f = kit.font("black", 96)
    track = 96 * 0.04
    words = NAME.split()
    group = m.width + 40 + max(_tracked_width(wd, f, track) for wd in words)
    gx = sx + (safe_w - group) / 2                       # the mark and wordmark, centred as one group
    img.paste(m, (int(gx), int(sy + (safe_h - m.height) / 2)), m)
    tx = gx + m.width + 40
    _tracked(d, (tx, sy + 92), words[0], f, INK, track)
    _tracked(d, (tx, sy + 92 + 104), words[1], f, GOLD, track)
    d.text((tx + 4, sy + 92 + 104 + 128), TAGLINE.upper(), font=kit.font("semibold", 34), fill=MUTED)
    # keep the wordmark inside the safe area
    assert tx + max(_tracked_width(wd, f, track) for wd in words) < sx + safe_w, "wordmark wider than the safe area"
    return img


def write() -> list:
    OUT.mkdir(parents=True, exist_ok=True)
    files = []
    p = OUT / "profile-800.png"
    profile(800).save(p, optimize=True)
    files.append(p)
    p = OUT / "banner-youtube-2560x1440.png"
    banner().save(p, optimize=True)
    files.append(p)
    p = OUT / "mark-1024.png"
    mark(1024).save(p, optimize=True)
    files.append(p)
    return files


if __name__ == "__main__":
    for f in write():
        print(f)
