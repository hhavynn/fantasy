"""Style D (paper) primitives: the sheet, the player sticker, the pen and the page turn.

The paper style draws the premium scenes on a dark sheet and adds an editorial data-video grammar on top: a
marker loop around the number being said, underlines and short handwritten notes, a sticker cut-out of the
player, and pages that slide away to uncover the next one. Every primitive is deterministic (seeded from its
geometry), so a rerender draws the same strokes and the render cache stays honest.
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import kit as K
from .kit import H, W

SS = 2                  # pen strokes are drawn at 2x and downsampled, so they are anti-aliased


# ------------------------------------------------------------------------------------------- sheet

def grain(base: Image.Image, tk) -> Image.Image:
    """`base` as a sheet of paper: fine seeded grain, faint fibres and a soft vignette."""
    rng = np.random.default_rng(1907)
    a = np.asarray(base, np.float32).copy()
    n = Image.fromarray(np.clip(rng.normal(128, 40, (H, W)), 0, 255).astype(np.uint8), "L")
    n = np.asarray(n.filter(ImageFilter.GaussianBlur(0.6)), np.float32) - 128     # tooth, not sensor noise
    a += (n / (n.std() + 1e-6) * tk.paper.grain)[..., None]
    fib = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(fib)
    for _ in range(tk.paper.fibres):
        x, y, ang, ln = rng.uniform(0, W), rng.uniform(0, H), rng.uniform(0, math.pi), rng.uniform(20, 90)
        pts = [(x + math.cos(ang) * ln * s + rng.normal(0, 2), y + math.sin(ang) * ln * s + rng.normal(0, 2))
               for s in np.linspace(-0.5, 0.5, 5)]
        d.line(pts, fill=int(rng.uniform(40, 110)), width=1)
    a += (np.asarray(fib.filter(ImageFilter.GaussianBlur(0.7)), np.float32) / 255 * 10)[..., None]
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    r = np.sqrt(((xx - W / 2) / (W * 0.75)) ** 2 + ((yy - H * 0.45) / (H * 0.7)) ** 2)
    a *= (1 - tk.paper.vignette * np.clip(r - 0.35, 0, 1) ** 1.5)[..., None]
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")


@lru_cache(maxsize=8)
def rays(color: tuple, size: int, n: int = 16, alpha: float = 0.32) -> Image.Image:
    """A sunburst in `color` that fades out from its centre (the backdrop of the player sticker)."""
    lay = Image.new("RGBA", (size, size), tuple(color) + (255,))
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    c, R = size / 2, size * 0.75
    for i in range(n):
        a0 = 2 * math.pi * i / n
        a1 = a0 + math.pi / n
        d.polygon([(c, c), (c + R * math.cos(a0), c + R * math.sin(a0)), (c + R * math.cos(a1), c + R * math.sin(a1))],
                  fill=255)
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    fade = np.clip(1 - np.hypot(xx - c, yy - c) / (size / 2), 0, 1) ** 0.8 * alpha
    lay.putalpha(Image.fromarray((np.asarray(mask, np.float32) * fade).astype(np.uint8), "L"))
    return lay


# ----------------------------------------------------------------------------------------- sticker

def make_sticker(im: Image.Image, border: int, tilt: float) -> Image.Image:
    """`im` (RGBA cut-out) with a white outline grown `border` px around its alpha, a soft drop shadow and a
    tilt of `tilt` degrees."""
    pad = border * 3
    canvas = Image.new("RGBA", (im.width + 2 * pad, im.height + 2 * pad), (0, 0, 0, 0))
    canvas.paste(im, (pad, pad), im)
    a = canvas.getchannel("A")
    # blur, then a hard threshold: the alpha grown by about `border` px with rounded corners
    grown = a.filter(ImageFilter.GaussianBlur(border / 2)).point(lambda v: 255 if v > 8 else 0)
    grown = grown.filter(ImageFilter.GaussianBlur(0.8))
    shadow = grown.filter(ImageFilter.GaussianBlur(10)).point(lambda v: int(v * 0.5))
    out = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    out.paste((0, 0, 0, 255), (6, 12), shadow)
    out.paste((250, 248, 242, 255), (0, 0), grown)
    out.alpha_composite(canvas)
    return out.rotate(tilt, resample=Image.BICUBIC, expand=True)


@lru_cache(maxsize=16)
def sticker(path: str, width: int, border: int, tilt: float) -> Image.Image:
    src = Image.open(path).convert("RGBA")
    im = src.resize((width, int(src.height * width / src.width)), Image.LANCZOS)
    return make_sticker(im, border, tilt)


# --------------------------------------------------------------------------------------------- pen

def _seed(*vals) -> int:
    return abs(hash(tuple(int(round(v)) for v in vals))) % (2 ** 31)


def _stroke(F, pts: list[tuple[float, float]], color, width: int, head: float = 0.0):
    """Draw a polyline (frame coordinates) as an anti-aliased pen stroke; `head` > 0 adds an arrowhead of that
    length at the last point."""
    if len(pts) < 2:
        return
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    m = width * 2 + head + 4
    x0, y0 = int(min(xs) - m), int(min(ys) - m)
    x1, y1 = int(max(xs) + m), int(max(ys) + m)
    lay = Image.new("L", ((x1 - x0) * SS, (y1 - y0) * SS), 0)
    d = ImageDraw.Draw(lay)
    sp = [((x - x0) * SS, (y - y0) * SS) for x, y in pts]
    d.line(sp, fill=255, width=width * SS, joint="curve")
    r = width * SS / 2
    for x, y in (sp[0], sp[-1]):                      # round pen ends
        d.ellipse((x - r, y - r, x + r, y + r), fill=255)
    if head:
        (ax, ay), (bx, by) = sp[-2], sp[-1]
        ang = math.atan2(by - ay, bx - ax)
        for s in (-1, 1):
            hx = bx - head * SS * math.cos(ang + s * 0.5)
            hy = by - head * SS * math.sin(ang + s * 0.5)
            d.line((bx, by, hx, hy), fill=255, width=width * SS)
            d.ellipse((hx - r, hy - r, hx + r, hy + r), fill=255)
    mask = lay.resize((x1 - x0, y1 - y0), Image.LANCZOS)
    F.img.paste(tuple(color[:3]), (x0, y0), mask)


def _partial(pts, k: float):
    """The first k (0..1) of a polyline, ending on an interpolated point."""
    if k >= 1:
        return pts
    f = k * (len(pts) - 1)
    i = int(f)
    a, b = pts[i], pts[min(i + 1, len(pts) - 1)]
    u = f - i
    return pts[:i + 1] + [(a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u)]


def loop_points(box, pad=(28, 20)) -> list[tuple[float, float]]:
    """A hand-drawn loop around `box`: starts upper left like a right-handed pen, goes once round and a bit
    further, slightly wider on the second pass, a little lopsided and tilted."""
    x0, y0, x1, y1 = box
    rng = np.random.default_rng(_seed(*box))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rx, ry = (x1 - x0) / 2 + pad[0], (y1 - y0) / 2 + pad[1]
    start = -2.3 + rng.uniform(-0.2, 0.2)
    sweep = 2 * math.pi + 0.6
    tilt = math.radians(rng.uniform(-5, -2))
    w = rng.uniform(0, 2 * math.pi, 3)
    pts = []
    for i in range(73):
        s = i / 72
        a = start + sweep * s
        grow = 1 + 0.07 * s + 0.025 * math.sin(3 * a + w[0])
        x = rx * grow * math.cos(a)
        y = ry * grow * math.sin(a) + ry * 0.06 * math.sin(2 * a + w[1])
        pts.append((cx + x * math.cos(tilt) - y * math.sin(tilt), cy + x * math.sin(tilt) + y * math.cos(tilt)))
    return pts


def circle(F, box, k: float, color, width: int):
    """A marker loop drawing itself around `box` as k goes 0 to 1."""
    if k > 0:
        _stroke(F, _partial(loop_points(box), k), color, width)


def underline(F, x0: float, x1: float, y: float, k: float, color, width: int):
    """A quick pen underline from x0 to x1, slightly rising and bowed."""
    if k <= 0:
        return
    rng = np.random.default_rng(_seed(x0, x1, y))
    lift, bow = rng.uniform(4, 10), rng.uniform(3, 7)
    pts = [(x0 - 6 + (x1 - x0 + 18) * s, y - lift * s + bow * math.sin(math.pi * s)) for s in np.linspace(0, 1, 32)]
    _stroke(F, _partial(pts, k), color, width)


def arrow(F, a, b, k: float, color, width: int, bend: float = 0.25):
    """A curved pen arrow from point a to point b, bowed by `bend` of its length; the head lands at the end."""
    if k <= 0:
        return
    (ax, ay), (bx, by) = a, b
    mx, my = (ax + bx) / 2, (ay + by) / 2
    nx, ny = -(by - ay) * bend, (bx - ax) * bend
    pts = [((1 - s) ** 2 * ax + 2 * (1 - s) * s * (mx + nx) + s * s * bx,
            (1 - s) ** 2 * ay + 2 * (1 - s) * s * (my + ny) + s * s * by) for s in np.linspace(0, 1, 28)]
    _stroke(F, _partial(pts, k), color, width, head=22 if k >= 0.98 else 0)


# -------------------------------------------------------------------------------------------- page

def page(prev: Image.Image, cur: Image.Image, k: float) -> Image.Image:
    """The previous page slides off down and to the left with a slight turn and a shadow, uncovering the new
    page beneath it (which sits in that shadow until it is clear). k=0 is the previous frame, k=1 the new one."""
    if k <= 0:
        return prev.copy()
    if k >= 1:
        return cur.copy()
    e = K.bezier(0.45, 0.0, 0.7, 0.5)(k)              # pulled away: slow off the mark, then gone
    out = Image.blend(Image.new("RGB", cur.size, (0, 0, 0)), cur, 1 - 0.35 * (1 - e))
    angle = 5.0 * e
    sheet = prev.convert("RGBA").rotate(angle, resample=Image.BICUBIC, expand=True)
    cx, cy = W / 2 - 0.3 * W * e, H / 2 + 1.1 * H * e
    px, py = int(cx - sheet.width / 2), int(cy - sheet.height / 2)
    sh = _shadow(angle, sheet.size)
    out.paste((0, 0, 0), (px + 10, py + 26), sh)
    out.paste(sheet, (px, py), sheet)
    return out


def _shadow(angle: float, size) -> Image.Image:
    """The page's soft shadow (built small and scaled up: a blur at full size per frame is too slow)."""
    s = 8
    m = Image.new("L", (W // s, H // s), 150).rotate(angle, resample=Image.BICUBIC, expand=True)
    m = m.filter(ImageFilter.GaussianBlur(3))
    return m.resize(size, Image.BILINEAR)
