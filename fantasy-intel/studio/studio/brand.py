"""Channel identity for Fantasy Intelligence, matching the Fantasy Intel website.

    uv run python -m studio.brand            # writes assets/brand/*.png

Everything comes from the site's design system (frontend/app/globals.css, Night theme): the "fi" monogram
from frontend/app/icon.svg in the accent blue, the FANTASY / INTEL two-colour wordmark in Barlow Condensed
SemiBold, IBM Plex Sans for supporting type, and the Night surface, line and chart-grid colours. The two
fonts are the basic-Latin subsets the site already ships (OFL-1.1, licences in assets/fonts). Drawn at 4x and
downsampled.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .core import ASSETS, FONTS
from .graphics import kit

OUT = ASSETS / "brand"
TAGLINE = "Usage, not box scores."
# Night theme tokens (frontend/app/globals.css)
BG = kit.hexrgb("#070b12")
SURFACE = kit.hexrgb("#0d131c")
LINE = kit.hexrgb("#222c3a")
GRID = kit.hexrgb("#1f2835")
INK = kit.hexrgb("#e9eef4")
MUTED = kit.hexrgb("#a3adba")
ACCENT = kit.hexrgb("#6aaeff")
ICON_BG = kit.hexrgb("#0a0d12")                   # icon.svg's own tile colour
SS = 4


def display(px: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / "BarlowCondensed-SemiBold-latin.woff2"), px)


def sans(px: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / "IBMPlexSans-SemiBold-latin.woff2"), px)


def _bezier(p0, p1, p2, p3, n=48):
    t = np.linspace(0, 1, n)[:, None]
    pts = ((1 - t) ** 3) * p0 + 3 * ((1 - t) ** 2) * t * p1 + 3 * (1 - t) * t * t * p2 + t ** 3 * p3
    return [tuple(p) for p in pts]


def _stroke(d: ImageDraw.ImageDraw, pts, width: float, fill):
    """A polyline with round caps and joins (SVG stroke-linecap/linejoin="round")."""
    r = width / 2
    d.line(pts, fill=fill, width=int(round(width)))
    for x, y in pts:
        d.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def monogram(size: int, color=ACCENT) -> Image.Image:
    """The site's "fi" mark (frontend/app/icon.svg, a 32-unit grid) on a transparent square."""
    S = size * SS
    k = S / 32
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    P = lambda x, y: np.array([x * k, y * k])                      # noqa: E731
    w = 3.6 * k
    # f: the stem M11.4 24.6 V13.4, then the hook c0-3.4 2-5 5.6-4.7
    stem = [tuple(P(11.4, 24.6)), tuple(P(11.4, 13.4))]
    hook = _bezier(P(11.4, 13.4), P(11.4, 10.0), P(13.4, 8.4), P(17.0, 8.7))
    _stroke(d, stem + hook[1:], w, color)
    _stroke(d, [tuple(P(8, 14.2)), tuple(P(16.2, 14.2))], w, color)             # crossbar
    _stroke(d, [tuple(P(22.4, 14.2)), tuple(P(22.4, 24.6))], w, color)          # i
    cx, cy, r = 22.4 * k, 8.9 * k, 2.3 * k
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)                     # the dot
    return img.resize((size, size), Image.LANCZOS)


def _backdrop(w: int, h: int, cx: float, cy: float, glow: float = 0.10) -> Image.Image:
    """Night background with one soft accent light, the way the site's hero surfaces read."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = np.sqrt(((xx - cx) / max(w, h)) ** 2 + ((yy - cy) / max(w, h)) ** 2)
    light = (np.clip(1 - r * 2.0, 0, 1) ** 2)[..., None]
    base = np.array(BG, np.float32)[None, None] * (1 - light * 0.5) + np.array(ACCENT, np.float32) * light * glow
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")


def profile(size: int = 800) -> Image.Image:
    """Profile picture: the monogram large on the Night background, inside the circle platforms crop to."""
    img = _backdrop(size, size, size * 0.5, size * 0.42, 0.14)
    m = monogram(int(size * 0.80))
    img.paste(m, ((size - m.width) // 2, (size - m.height) // 2 + int(size * 0.02)), m)
    return img


def _tracked(d, x, y, text, f, fill, track):
    for ch in text:
        d.text((x, y), ch, font=f, fill=fill)
        x += f.getlength(ch) + track
    return x


def _width(text, f, track):
    return sum(f.getlength(ch) + track for ch in text) - track


def banner(w: int = 2560, h: int = 1440) -> Image.Image:
    """YouTube banner: the monogram on its tile plus the two-colour wordmark, centred in the 1546x423 area
    every device shows; the site's chart grid runs across the full width for TVs and desktop."""
    img = _backdrop(w, h, w * 0.5, h * 0.5, 0.08)
    d = ImageDraw.Draw(img, "RGBA")
    for y in range(int(h / 2) - 560, int(h / 2) + 561, 80):
        d.line((0, y, w, y), fill=GRID + (150,), width=2)
    safe_w, safe_h = 1546, 423
    sx, sy = (w - safe_w) / 2, (h - safe_h) / 2
    tile = 250
    first, second = "FANTASY", "INTELLIGENCE"
    px = 176
    while True:                                          # the largest size that fits beside the tile
        f, track = display(px), px * 0.03                # .brand letter-spacing: .03em
        word_w = _width(first, f, track) + track * 3 + _width(second, f, track)
        if tile + 56 + word_w <= safe_w - 40:
            break
        px -= 4
    group = tile + 56 + word_w
    gx = sx + (safe_w - group) / 2
    ty = sy + (safe_h - tile) / 2
    d.rounded_rectangle((gx, ty, gx + tile, ty + tile), radius=int(tile * 7 / 32), fill=ICON_BG + (255,),
                        outline=LINE + (255,), width=3)
    m = monogram(tile)
    img.paste(m, (int(gx), int(ty)), m)
    tx = gx + tile + 56
    cap_top = f.getbbox("H")[1]
    wy = ty + 20 - cap_top
    x = _tracked(d, tx, wy, first, f, INK, track)
    _tracked(d, x + track * 3, wy, second, f, ACCENT, track)
    d.text((tx + 4, ty + tile - 58), TAGLINE.upper(), font=sans(40), fill=MUTED)
    assert tx + word_w < sx + safe_w, "wordmark wider than the banner's safe area"
    return img


def write() -> list:
    OUT.mkdir(parents=True, exist_ok=True)
    out = {"profile-800.png": profile(800), "banner-youtube-2560x1440.png": banner(), "mark-1024.png": monogram(1024)}
    files = []
    for name, im in out.items():
        im.save(OUT / name, optimize=True)
        files.append(OUT / name)
    return files


if __name__ == "__main__":
    for f in write():
        print(f)
