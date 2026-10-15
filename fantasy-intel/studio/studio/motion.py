"""Motion-graphics toolkit for vertical videos: easing, original backgrounds, a jersey illustration, cached
text layers that scale smoothly, and short-form captions.

Everything is drawn from code: no photos, logos, footage or third-party artwork. Team colours are plain
colour values (not marks); the jersey is a generic shape with the player's name and roster number.
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .core import FONTS
from .visual import WEIGHTS

W, H = 1080, 1920
# Mobile safe zone: platform UI covers the top ~200 px, the bottom ~380 px and a right-hand rail.
SAFE_LEFT, SAFE_RIGHT, SAFE_TOP, SAFE_BOTTOM = 70, 950, 230, 1540
CENTER_X = (SAFE_LEFT + SAFE_RIGHT) / 2

INK = (240, 244, 248)
MUTED = (164, 174, 188)
DARK = (8, 10, 14)
UP = (90, 211, 148)
DOWN = (255, 92, 92)
GOLD = (255, 205, 64)

TEAM_COLORS = {
    "ARI": ("#97233F", "#FFB612"), "ATL": ("#A71930", "#101820"), "BAL": ("#241773", "#9E7C0C"),
    "BUF": ("#00338D", "#C60C30"), "CAR": ("#0085CA", "#101820"), "CHI": ("#0B162A", "#C83803"),
    "CIN": ("#FB4F14", "#101820"), "CLE": ("#311D00", "#FF3C00"), "DAL": ("#003594", "#869397"),
    "DEN": ("#FB4F14", "#002244"), "DET": ("#0076B6", "#B0B7BC"), "GB": ("#203731", "#FFB612"),
    "HOU": ("#03202F", "#A71930"), "IND": ("#002C5F", "#A2AAAD"), "JAX": ("#006778", "#D7A22A"),
    "KC": ("#E31837", "#FFB81C"), "LA": ("#003594", "#FFA300"), "LAC": ("#0080C6", "#FFC20E"),
    "LV": ("#000000", "#A5ACAF"), "MIA": ("#008E97", "#FC4C02"), "MIN": ("#4F2683", "#FFC62F"),
    "NE": ("#002244", "#C60C30"), "NO": ("#D3BC8D", "#101820"), "NYG": ("#0B2265", "#A71930"),
    "NYJ": ("#125740", "#FFFFFF"), "PHI": ("#004C54", "#A5ACAF"), "PIT": ("#FFB612", "#101820"),
    "SEA": ("#002244", "#69BE28"), "SF": ("#AA0000", "#B3995D"), "TB": ("#D50A0A", "#34302B"),
    "TEN": ("#0C2340", "#4B92DB"), "WAS": ("#5A1414", "#FFB612"),
}


def rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


def team_colors(team: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    p, s = TEAM_COLORS.get(team, ("#1E2A3A", "#6AAEFF"))
    return rgb(p), rgb(s)


def luminance(c) -> float:
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def vivid(c, fallback=GOLD):
    """A team colour bright enough to read on dark backgrounds, or the fallback."""
    return c if luminance(c) > 90 else fallback


# ------------------------------------------------------------------------------------------- easing

def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def ease_out(x: float) -> float:
    x = clamp(x)
    return 1 - (1 - x) ** 3


def ease_in_out(x: float) -> float:
    x = clamp(x)
    return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2


def back_out(x: float, s: float = 1.6) -> float:
    """Overshoot then settle: the 'slam' of a number landing."""
    x = clamp(x)
    return 1 + (s + 1) * (x - 1) ** 3 + s * (x - 1) ** 2


def progress(t: float, start: float, dur: float) -> float:
    return clamp((t - start) / dur) if dur > 0 else float(t >= start)


# -------------------------------------------------------------------------------------------- fonts

@lru_cache(maxsize=None)
def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / WEIGHTS[weight]), size)


@lru_cache(maxsize=1024)
def text_layer(text: str, weight: str, size: int, fill=INK, stroke: int = 0, stroke_fill=DARK,
               shadow: bool = True) -> Image.Image:
    """Text rendered once onto a tight RGBA layer (with a soft drop shadow), then reused every frame."""
    f = font(weight, size)
    l, t, r, b = f.getbbox(text, stroke_width=stroke)
    pad = 30
    layer = Image.new("RGBA", (r - l + 2 * pad, b - t + 2 * pad), (0, 0, 0, 0))
    if shadow:
        sh = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        ImageDraw.Draw(sh).text((pad - l + 6, pad - t + 10), text, font=f, fill=(0, 0, 0, 170),
                                stroke_width=stroke, stroke_fill=(0, 0, 0, 170))
        layer = Image.alpha_composite(layer, sh.filter(ImageFilter.GaussianBlur(10)))
    ImageDraw.Draw(layer).text((pad - l, pad - t), text, font=f, fill=fill, stroke_width=stroke,
                               stroke_fill=stroke_fill)
    return layer


def fit_size(text: str, weight: str, size: int, width: float) -> int:
    while size > 20 and font(weight, size).getlength(text) > width:
        size -= 4
    return size


def paste(base: Image.Image, layer: Image.Image, cx: float, cy: float, scale: float = 1.0,
          alpha: float = 1.0, anchor: str = "c") -> None:
    """Composite a layer centred (or left-aligned with anchor='l') at (cx, cy), scaled and faded."""
    if alpha <= 0.01 or scale <= 0.01:
        return
    lay = layer
    if abs(scale - 1) > 0.004:
        lay = layer.resize((max(1, int(layer.width * scale)), max(1, int(layer.height * scale))), Image.BILINEAR)
    if alpha < 0.999:
        a = lay.getchannel("A").point(lambda v: int(v * alpha))
        lay = lay.copy()
        lay.putalpha(a)
    x = cx - lay.width / 2 if anchor == "c" else cx - 30 * scale
    y = cy - lay.height / 2
    base.paste(lay, (int(x), int(y)), lay)


def put_text(base, text, cx, cy, weight="black", size=80, fill=INK, scale=1.0, alpha=1.0, anchor="c",
             stroke=0, max_width=None):
    if max_width:
        size = fit_size(text, weight, size, max_width)
    paste(base, text_layer(text, weight, size, fill, stroke), cx, cy, scale, alpha, anchor)


# -------------------------------------------------------------------------------------- backgrounds

@lru_cache(maxsize=8)
def field_strip(primary: tuple) -> Image.Image:
    """A tall, original turf illustration (yard lines, hash marks, faint yard numbers) that scrolls."""
    h = H * 2
    yy = np.linspace(0, 1, h)[:, None]
    base = np.stack([np.full((h, W), v) for v in (14, 54, 32)], -1).astype(np.float32)
    stripes = ((np.arange(h)[:, None] // 120) % 2 == 0) * 6.0
    base = base + stripes[..., None] * np.array([0.3, 1.0, 0.5])
    tint = np.array(primary, np.float32) * 0.18
    base = base * 0.85 + tint * (0.4 + 0.6 * yy[..., None])
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    marks = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(marks)
    for i, y in enumerate(range(0, h, 240)):
        d.rectangle((0, y, W, y + 6), fill=(255, 255, 255, 60))
        for hx in (300, 780):
            for k in range(1, 4):
                d.rectangle((hx, y + k * 60, hx + 34, y + k * 60 + 4), fill=(255, 255, 255, 45))
        num = ["10", "20", "30", "40", "50", "40", "30", "20"][i % 8]
        d.text((70, y + 40), num, font=font("black", 110), fill=(255, 255, 255, 22))
        d.text((W - 230, y + 40), num, font=font("black", 110), fill=(255, 255, 255, 22))
    img = Image.alpha_composite(img, marks).convert("RGB")
    # Darken toward the edges so foreground type always reads.
    vig = np.clip(1 - (np.abs(np.linspace(-1, 1, W))[None, :] ** 2) * 0.45, 0, 1)
    arr = (np.asarray(img, np.float32) * vig[..., None] * 0.8).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


@lru_cache(maxsize=8)
def team_backdrop(primary: tuple, secondary: tuple) -> Image.Image:
    """Diagonal team-colour gradient with end-zone style stripes."""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    k = (xx / W * 0.35 + yy / H * 0.65)
    p = np.array(primary, np.float32)
    dark = p * 0.25
    base = dark[None, None] * k[..., None] + p[None, None] * (1 - k[..., None]) * 0.9
    stripe = (((xx + yy * 0.6) // 90) % 2 == 0) * 10.0
    base = base + stripe[..., None]
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")
    d = ImageDraw.Draw(img, "RGBA")
    d.polygon([(0, 1500), (W, 1180), (W, 1260), (0, 1580)], fill=secondary + (90,))
    return img


@lru_cache(maxsize=8)
def studio_backdrop(accent: tuple) -> Image.Image:
    """Near-black set with a soft accent spotlight and a faint grid."""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    r = np.sqrt(((xx - W * 0.5) / W) ** 2 + ((yy - H * 0.38) / H) ** 2)
    glow = np.clip(1 - r * 2.2, 0, 1) ** 2
    base = np.array(DARK, np.float32)[None, None] + glow[..., None] * np.array(accent, np.float32) * 0.28
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")
    d = ImageDraw.Draw(img, "RGBA")
    for x in range(0, W, 90):
        d.line((x, 0, x, H), fill=(255, 255, 255, 9))
    for y in range(0, H, 90):
        d.line((0, y, W, y), fill=(255, 255, 255, 9))
    return img


def background(kind: str, team: str, accent: tuple, t: float) -> Image.Image:
    primary, secondary = team_colors(team)
    if kind == "field":
        strip = field_strip(primary)
        off = int((t * 60) % H)                      # a slow camera drift down the field
        return strip.crop((0, H - off, W, 2 * H - off))
    if kind == "team":
        return team_backdrop(primary, secondary).copy()
    return studio_backdrop(accent).copy()


# ------------------------------------------------------------------------------------------- jersey

@lru_cache(maxsize=16)
def jersey(team: str, number: str, last_name: str, width: int = 720) -> Image.Image:
    """A generic football jersey (no marks) in team colours with the player's name and number."""
    primary, secondary = team_colors(team)
    h = int(width * 1.08)
    img = Image.new("RGBA", (width, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = width
    body = [(0.30 * w, 0.02 * h), (0.70 * w, 0.02 * h), (0.98 * w, 0.14 * h), (1.0 * w, 0.42 * h),
            (0.82 * w, 0.46 * h), (0.82 * w, 0.98 * h), (0.18 * w, 0.98 * h), (0.18 * w, 0.46 * h),
            (0.0 * w, 0.42 * h), (0.02 * w, 0.14 * h)]
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon([(x + 10, y + 16) for x, y in body], fill=(0, 0, 0, 150))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(14)))
    d = ImageDraw.Draw(img)
    fill = primary if luminance(primary) > 25 else (40, 44, 52)
    d.polygon(body, fill=fill)
    trim = secondary if abs(luminance(secondary) - luminance(fill)) > 40 else (240, 240, 240)
    for k in (0.30, 0.355):                         # sleeve stripes
        d.line([(0.05 * w, k * h), (0.21 * w, (k + 0.035) * h)], fill=trim, width=int(w * 0.022))
        d.line([(0.95 * w, k * h), (0.79 * w, (k + 0.035) * h)], fill=trim, width=int(w * 0.022))
    d.polygon([(0.38 * w, 0.02 * h), (0.62 * w, 0.02 * h), (0.5 * w, 0.12 * h)], fill=DARK)   # collar
    d.line([(0.38 * w, 0.02 * h), (0.5 * w, 0.12 * h), (0.62 * w, 0.02 * h)], fill=trim, width=int(w * 0.018))
    name_f = font("black", int(w * 0.085))
    d.text((0.5 * w, 0.22 * h), last_name.upper(), font=name_f, fill=INK, anchor="mm")
    num_f = font("black", int(w * 0.42))
    d.text((0.5 * w, 0.6 * h), number, font=num_f, fill=INK, anchor="mm", stroke_width=int(w * 0.014),
           stroke_fill=trim)
    return img


# ----------------------------------------------------------------------------------------- captions

CAPTION_FONT = ("black", 78)
CAPTION_MAX_W = 860
CAPTION_Y = 1450


def caption_frame(words: list[dict], active: int, keyword_color, t_in: float) -> Image.Image:
    """One caption card: up to two lines of UPPERCASE words; the word being spoken pops slightly; numbers
    and emphasised words carry the story colour. `words` items: {text, emph}."""
    f = font(*CAPTION_FONT)
    layer = Image.new("RGBA", (W, 330), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    space = f.getlength(" ")
    lines, cur, cur_w = [], [], 0.0
    for i, w in enumerate(words):
        ww = f.getlength(w["text"].upper())
        if cur and cur_w + space + ww > CAPTION_MAX_W:
            lines.append(cur)
            cur, cur_w = [], 0.0
        cur.append((i, w, ww))
        cur_w += (space if len(cur) > 1 else 0) + ww
    if cur:
        lines.append(cur)
    lh = int(CAPTION_FONT[1] * 1.12)
    y0 = 165 - lh * len(lines) / 2 + (1 - ease_out(t_in * 6)) * 24
    for li, line in enumerate(lines):
        total = sum(ww for _, _, ww in line) + space * (len(line) - 1)
        x = CENTER_X - total / 2
        for i, w, ww in line:
            color = keyword_color if w["emph"] else INK
            if i == active:
                color = GOLD if not w["emph"] else keyword_color
            d.text((x, y0 + li * lh), w["text"].upper(), font=f, fill=color, anchor="la", stroke_width=9,
                   stroke_fill=(6, 8, 12))
            x += ww + space
    return layer


def lerp(a, b, k):
    return a + (b - a) * k


def mix_color(c1, c2, k):
    return tuple(int(lerp(a, b, clamp(k))) for a, b in zip(c1, c2))


def arrow(d: ImageDraw.ImageDraw, cx: float, cy: float, size: float, color, down: bool) -> None:
    s = size
    if down:
        pts = [(cx - s * 0.5, cy - s * 0.1), (cx + s * 0.5, cy - s * 0.1), (cx, cy + s * 0.55)]
        d.rectangle((cx - s * 0.18, cy - s * 0.6, cx + s * 0.18, cy - s * 0.08), fill=color)
    else:
        pts = [(cx - s * 0.5, cy + s * 0.1), (cx + s * 0.5, cy + s * 0.1), (cx, cy - s * 0.55)]
        d.rectangle((cx - s * 0.18, cy + s * 0.08, cx + s * 0.18, cy + s * 0.6), fill=color)
    d.polygon(pts, fill=color)


def wobble(t: float, amp: float = 1.0) -> float:
    """A tiny continuous drift so held frames never freeze."""
    return math.sin(t * 2.1) * amp
