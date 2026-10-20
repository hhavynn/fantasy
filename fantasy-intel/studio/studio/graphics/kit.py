"""Drawing kit shared by the vector styles: design tokens, cubic-bezier easing, tracked type and a draw log.

Every piece of text goes through Frame.text, which records what was drawn, where, how big, in what colour,
and which claim it came from. The visual QA (studio/graphics/vqa.py) reads that log instead of guessing from
pixels: text outside the safe zone, text too small for a phone, low contrast, overlapping labels and numbers
without a source are all measured, not eyeballed.
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ..core import ASSETS, FONTS
from ..visual import WEIGHTS

W, H = 1080, 1920
SAFE = (70, 230, 950, 1540)        # left, top, right, bottom: platform UI covers the rest (studio/motion.py)
STYLES_DIR = ASSETS / "styles"


# ------------------------------------------------------------------------------------------- tokens

def _ns(d):
    return SimpleNamespace(**{k: _ns(v) if isinstance(v, dict) else v for k, v in d.items()})


@lru_cache(maxsize=None)
def tokens(name: str) -> SimpleNamespace:
    """A style's design tokens from assets/styles/<name>.toml."""
    return _ns(tomllib.loads((STYLES_DIR / f"{name}.toml").read_text()))


def hexrgb(h: str) -> tuple[int, int, int]:
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def pal(tk, key: str) -> tuple[int, int, int]:
    return hexrgb(getattr(tk.palette, key))


# ------------------------------------------------------------------------------------------- easing

@lru_cache(maxsize=None)
def bezier(x1: float, y1: float, x2: float, y2: float):
    """CSS-style cubic-bezier easing: f(x) for x in 0..1 (Newton steps on x, then y)."""
    def coord(t, a, b):
        return 3 * a * t * (1 - t) ** 2 + 3 * b * t * t * (1 - t) + t ** 3

    def f(x: float) -> float:
        if x <= 0:
            return 0.0
        if x >= 1:
            return 1.0
        t = x
        for _ in range(8):
            dx = coord(t, x1, x2) - x
            d = 3 * x1 * (1 - t) ** 2 + 6 * (x2 - x1) * t * (1 - t) + 3 * (1 - x2) * t * t
            if abs(d) < 1e-6:
                break
            t = min(1.0, max(0.0, t - dx / d))
        return coord(t, y1, y2)
    return f


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def prog(t: float, start: float, dur: float) -> float:
    return clamp((t - start) / dur) if dur > 0 else float(t >= start)


def ease(tk, kind: str, t: float, start: float, dur: float | None = None) -> float:
    """Eased progress of an animation that starts at `start` with the style's curve and duration."""
    return bezier(*getattr(tk.motion, kind))(prog(t, start, tk.motion.enter_dur if dur is None else dur))


def lerp(a, b, k):
    return a + (b - a) * k


def mix(c1, c2, k):
    return tuple(int(round(lerp(a, b, clamp(k)))) for a, b in zip(c1, c2))


# --------------------------------------------------------------------------------------------- type

# Type families a style can name in its tokens. Inter (regular..black) is the studio's own; Barlow Condensed and
# IBM Plex Sans are the Fantasy Intel website's display and text faces (the basic-Latin subsets the site ships,
# OFL-1.1). Plex and Caveat are variable fonts: the number is the weight axis.
FONT_FILES = {**{w: (f, None) for w, f in WEIGHTS.items()},
              "barlow": ("BarlowCondensed-SemiBold-latin.woff2", None),
              "plex": ("IBMPlexSans-SemiBold-latin.woff2", 400),
              "plex-medium": ("IBMPlexSans-SemiBold-latin.woff2", 500),
              "plex-semibold": ("IBMPlexSans-SemiBold-latin.woff2", 600),
              "plex-bold": ("IBMPlexSans-SemiBold-latin.woff2", 700),
              "hand": ("Caveat-wght.ttf", 700)}          # Caveat (OFL-1.1): the paper style's pen notes


@lru_cache(maxsize=None)
def font(weight: str, px: int) -> ImageFont.FreeTypeFont:
    file, axis = FONT_FILES[weight]
    f = ImageFont.truetype(str(FONTS / file), px)
    if axis is not None:
        f.set_variation_by_axes([axis])
    return f


def measure(text: str, weight: str, px: int, track: float = 0.0, tabular: bool = False) -> float:
    f = font(weight, px)
    if not track and not tabular:
        return f.getlength(text)
    dw = max(f.getlength(d) for d in "0123456789") if tabular else 0
    return sum((dw if tabular and ch.isdigit() else f.getlength(ch)) + track * px for ch in text) - track * px


def fit(text: str, weight: str, px: int, width: float, track: float = 0.0, floor: int = 24,
        tabular: bool = False) -> int:
    while px > floor and measure(text, weight, px, track, tabular) > width:
        px -= 2
    return px


@lru_cache(maxsize=2048)
def text_layer(text: str, weight: str, px: int, fill: tuple, track: float = 0.0, shear: float = 0.0,
               tabular: bool = False) -> Image.Image:
    """Text on a tight RGBA layer with cap-height-based vertical metrics, optionally tracked and sheared.
    The layer's box runs from the cap top to the baseline (descenders hang below), so aligning layers aligns
    cap heights, the way a designer aligns type."""
    f = font(weight, px)
    asc = f.getbbox("H")[1]                  # cap top offset from the draw origin
    cap = f.getbbox("H")[3] - asc
    desc = int(px * 0.28)
    w = int(measure(text, weight, px, track, tabular)) + 4
    pl = int(shear * desc) + 1 if shear else 0          # room for the descenders, which lean left
    pad = int(shear * cap) + 2 if shear else 0
    lay = Image.new("RGBA", (w + pl + pad, cap + desc), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    if not track and not tabular:
        d.text((pl, -asc), text, font=f, fill=fill)
    else:
        dw = max(f.getlength(d_) for d_ in "0123456789") if tabular else 0
        x = float(pl)
        for ch in text:
            cw = dw if tabular and ch.isdigit() else f.getlength(ch)
            off = (cw - f.getlength(ch)) / 2 if tabular and ch.isdigit() else 0
            d.text((x + off, -asc), ch, font=f, fill=fill)
            x += cw + track * px
    if shear:
        # Shear about the baseline so the letters lean right (broadcast italics).
        lay = lay.transform(lay.size, Image.AFFINE, (1, shear, -shear * cap, 0, 1, 0), resample=Image.BICUBIC)
    lay.info["cap"] = cap
    return lay


# -------------------------------------------------------------------------------------------- frame

def rel_lum(c) -> float:
    def ch(v):
        v = v / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(x) for x in c[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(c1, c2) -> float:
    a, b = sorted((rel_lum(c1), rel_lum(c2)), reverse=True)
    return (a + 0.05) / (b + 0.05)


class Frame:
    """One 1080x1920 frame plus the log of every text it carries."""

    def __init__(self, img: Image.Image, measure: bool = False):
        self.img = img
        self.d = ImageDraw.Draw(img, "RGBA")
        self.log: list[dict] = []
        self.measure = measure

    # shapes -----------------------------------------------------------------------------------
    def rect(self, box, fill=None, outline=None, width: int = 0, radius: int = 0, alpha: float = 1.0):
        if alpha <= 0.01:
            return
        f = None if fill is None else tuple(fill[:3]) + (int(255 * alpha * (fill[3] / 255 if len(fill) > 3 else 1)),)
        o = None if outline is None else tuple(outline[:3]) + (int(255 * alpha),)
        x0, y0, x1, y1 = box
        if x1 - x0 < 1 or y1 - y0 < 1:
            return
        self.d.rounded_rectangle((x0, y0, x1, y1), radius=min(radius, (y1 - y0) / 2, (x1 - x0) / 2), fill=f,
                                 outline=o, width=width)

    def poly(self, pts, fill, alpha: float = 1.0):
        if alpha > 0.01:
            self.d.polygon(pts, fill=tuple(fill[:3]) + (int(255 * alpha),))

    def line(self, pts, fill, width: int = 2, alpha: float = 1.0):
        if alpha > 0.01:
            self.d.line(pts, fill=tuple(fill[:3]) + (int(255 * alpha),), width=width)

    def dashed(self, x0, x1, y, fill, width=3, dash=14, gap=10, alpha=1.0):
        x = x0
        while x < x1:
            self.line((x, y, min(x + dash, x1), y), fill, width, alpha)
            x += dash + gap

    def layer(self, lay: Image.Image, x: float, y: float, alpha: float = 1.0):
        if alpha <= 0.01:
            return
        if alpha < 0.999:
            lay = lay.copy()
            lay.putalpha(lay.getchannel("A").point(lambda v: int(v * alpha)))
        self.img.paste(lay, (int(round(x)), int(round(y))), lay)

    # text -------------------------------------------------------------------------------------
    def text(self, text: str, x: float, y: float, spec, fill, anchor: str = "l", alpha: float = 1.0,
             scale: float = 1.0, src=None, role: str = "label", shear: float = 0.0, tabular: bool = False,
             max_width: float | None = None) -> tuple[float, float, float, float]:
        """Draw `text` with a type spec [weight, px, tracking]. (x, y) is the cap-top; anchor l|c|r is
        horizontal. Returns the drawn box. `src` names the claim (or data field) a number came from."""
        weight, px, track = spec
        if max_width:
            px = fit(text, weight, px, max_width, track, tabular=tabular)
        lay = text_layer(text, weight, px, tuple(fill[:3]), track, shear, tabular)
        if abs(scale - 1) > 0.003:
            lay = lay.resize((max(1, int(lay.width * scale)), max(1, int(lay.height * scale))), Image.BICUBIC)
        w, h = lay.size
        x0 = x - (w / 2 if anchor == "c" else w if anchor == "r" else 0)
        box = (x0, y, x0 + w, y + h)
        entry = {"text": text, "box": [round(v) for v in box], "cap_px": round(px * scale * 0.727),
                 "fill": list(fill[:3]), "alpha": round(alpha, 3), "src": src, "role": role}
        if self.measure and alpha > 0.95:
            entry["contrast"] = self._contrast(box, fill, lay)
        self.layer(lay, x0, y, alpha)
        if alpha > 0.02:
            self.log.append(entry)
        return box

    def _contrast(self, box, fill, lay) -> float:
        """Contrast against what is under the letters themselves (sampled before they are drawn)."""
        x0, y0 = int(round(box[0])), int(round(box[1]))
        region = np.asarray(self.img.crop((x0, y0, x0 + lay.width, y0 + lay.height)).convert("RGB"), np.float32)
        a = region[np.asarray(lay.getchannel("A")) > 128]
        if not len(a):
            return 21.0
        lum = a @ np.array([0.2126, 0.7152, 0.0722], np.float32)
        # the worst-case background: the background pixel closest in brightness to the text colour
        target = 0.2126 * fill[0] + 0.7152 * fill[1] + 0.0722 * fill[2]
        cand = [a[np.argsort(lum)[int(len(lum) * q)]] for q in (0.1, 0.5, 0.9)]
        worst = min(cand, key=lambda c: abs(0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2] - target))
        return round(float(contrast(fill, worst)), 2)


# -------------------------------------------------------------------------------------------- data

def weeks_label(weeks) -> str:
    """'Wk 4', 'Wks 1–3' or 'Wks 2, 5' from a claim's source weeks."""
    weeks = sorted(set(weeks))
    if len(weeks) == 1:
        return f"Wk {weeks[0]}"
    if weeks == list(range(weeks[0], weeks[-1] + 1)):
        return f"Wks {weeks[0]}–{weeks[-1]}"
    return "Wks " + ", ".join(str(w) for w in weeks)


def claim_weeks(c, cid: str) -> list[int]:
    src = c.claims[cid].get("source", {})
    return src.get("weeks") or ([src["week"]] if "week" in src else [])


def nice_max(v: float, step: float) -> float:
    """The axis top: the smallest multiple of `step` at or above v (never distorts: the axis starts at 0)."""
    import math
    return max(step, math.ceil(v / step - 1e-9) * step)
