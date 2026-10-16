"""Visual primitives for the flame / praise edits: the player big, the joke physical, the hits felt.

Built on Pillow, cached where a layer repeats, so a frame stays a few tens of milliseconds:

  cutout        the player's transparent headshot as a big sticker: thick outline, drop shadow, team-colour rim
  backdrop      layered team backgrounds: two-tone diagonal, halftone dots, light rays, speed lines, film grain
  slam          a number or word that lands with overshoot, a white flash and a shake (keyed to a time)
  burst         comic action lines radiating from an impact point, for ~0.3 s
  sticker       a meme sticker (bold text in a tilted box) that pops in
  shake/punch   the whole frame jolts on a hit (crop-and-shift, no wrapped edges)
  freeze        the hard stop: desaturated, vignetted, grain-locked frame
  hits(c, spec) the times in this beat when something should land: its sound cues' anchors, so picture and sound
                hit together

Every number drawn is still a claim's display string, passed in by the scene.
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

from . import motion as M

W, H = M.W, M.H
WHITE, BLACK = (255, 255, 255), (12, 12, 16)


def ease_spring(x: float, overshoot: float = 1.7) -> float:
    """0 -> 1 with an overshoot that settles: things land with weight."""
    x = M.clamp(x)
    return 1 + (overshoot + 1) * (x - 1) ** 3 + overshoot * (x - 1) ** 2


# ------------------------------------------------------------------------------------------ the player

@lru_cache(maxsize=16)
def _headshot(path: str | None, team: str, number: str, last: str) -> Image.Image:
    if path:
        im = Image.open(path).convert("RGBA")
        bbox = im.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
        return im.crop(bbox) if bbox else im
    return M.jersey(team, number, last, 600)                    # no photo: the jersey graphic, as before


@lru_cache(maxsize=48)
def cutout(path: str | None, team: str, number: str, last: str, height: int, outline: int = 14,
           rim: tuple | None = None, mirror: bool = False, crop: tuple | None = None) -> Image.Image:
    """The player as a big sticker. `crop` (fractions l, t, r, b) frames a close-up; `rim` adds a team-colour edge
    glow behind the white outline."""
    src = _headshot(path, team, number, last)
    if crop:
        w, h = src.size
        src = src.crop((int(crop[0] * w), int(crop[1] * h), int(crop[2] * w), int(crop[3] * h)))
    if mirror:
        src = ImageOps.mirror(src)
    scale = height / src.height
    big = src.resize((max(1, int(src.width * scale)), height), Image.LANCZOS)
    big = big.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))   # crisp after upscaling
    pad = outline * 3 + 40
    canvas = Image.new("RGBA", (big.width + 2 * pad, big.height + 2 * pad), (0, 0, 0, 0))
    alpha = Image.new("L", canvas.size, 0)
    alpha.paste(big.getchannel("A"), (pad, pad))
    if rim:
        glow = alpha.filter(ImageFilter.MaxFilter(outline * 2 + 1)).filter(ImageFilter.GaussianBlur(outline * 1.6))
        canvas.paste(Image.new("RGBA", canvas.size, rim + (255,)), (0, 0), glow.point(lambda v: int(v * 0.9)))
    shadow = alpha.filter(ImageFilter.MaxFilter(outline * 2 + 1)).filter(ImageFilter.GaussianBlur(18))
    canvas.paste(Image.new("RGBA", canvas.size, (0, 0, 0, 255)),
                 (16, 22), shadow.point(lambda v: int(v * 0.55)))
    edge = alpha.point(lambda v: 255 if v > 90 else 0).filter(ImageFilter.MaxFilter(outline * 2 + 1))
    canvas.paste(Image.new("RGBA", canvas.size, WHITE + (255,)), (0, 0), edge)
    canvas.alpha_composite(big, (pad, pad))
    return canvas


def player(c, height: int, **kw) -> Image.Image:
    from .stages.story import last
    return cutout(str(c.headshot) if c.headshot else None, c.team, str(c.p.get("jersey") or ""), last(c.p["name"]),
                  height, **kw)


def team_rgb(team: str) -> tuple[tuple, tuple]:
    p, s = M.team_colors(team)
    return M.vivid(p, p), M.vivid(s, s)


# ------------------------------------------------------------------------------------------ backdrops

@lru_cache(maxsize=8)
def _halftone(color: tuple, spacing: int = 26, alpha: int = 60) -> Image.Image:
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    for yi, y in enumerate(range(0, H + spacing, spacing)):
        for x in range(-spacing, W + spacing, spacing):
            xx = x + (spacing // 2 if yi % 2 else 0)
            r = 3 + 6 * (y / H)                                     # dots grow toward the bottom
            d.ellipse((xx - r, y - r, xx + r, y + r), fill=color + (alpha,))
    return lay


@lru_cache(maxsize=4)
def _grain(seed: int) -> Image.Image:
    rng = np.random.default_rng(seed)
    n = (rng.standard_normal((H // 2, W // 2)) * 22 + 128).clip(0, 255).astype(np.uint8)
    return Image.fromarray(n, "L").resize((W, H), Image.NEAREST)


@lru_cache(maxsize=16)
def _two_tone(a: tuple, b: tuple, angle: float) -> Image.Image:
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    k = (xx * math.cos(angle) + yy * math.sin(angle)) / (W * abs(math.cos(angle)) + H * abs(math.sin(angle)))
    k = np.clip((k - 0.35) * 3.0, 0, 1)[..., None]
    base = np.array(a, np.float32) * (1 - k) + np.array(b, np.float32) * k
    r = np.sqrt(((xx - W / 2) / W) ** 2 + ((yy - H * 0.42) / H) ** 2)[..., None]
    base *= np.clip(1.15 - r * 0.9, 0.35, 1.1)                       # vignette toward the edges
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")


def backdrop(kind: str, team: str, t: float, accent: tuple | None = None) -> Image.Image:
    """hype: team two-tone + rotating light rays + halftone; roast: dark, desaturated, red-tinted; court: deep wood
    red with spotlight; versus: hard diagonal split. All carry a little film grain (moving) so the frame breathes."""
    p, s = team_rgb(team)
    if kind == "hype":
        img = _two_tone(tuple(int(v * 0.55) for v in p), tuple(int(v * 0.35) for v in s), 1.1).copy()
        q = 4                                                       # rays drawn and softened at 1/4 size: ~1 ms
        rays = Image.new("RGBA", (W // q, H // q), (0, 0, 0, 0))
        d = ImageDraw.Draw(rays)
        cx, cy = W / 2 / q, H * 0.45 / q
        for i in range(18):
            a = i / 18 * 2 * math.pi + t * 0.25
            d.polygon([(cx, cy), (cx + 600 * math.cos(a), cy + 600 * math.sin(a)),
                       (cx + 600 * math.cos(a + 0.12), cy + 600 * math.sin(a + 0.12))],
                      fill=(accent or M.GOLD) + (26,))
        rays = rays.filter(ImageFilter.GaussianBlur(1.5)).resize((W, H), Image.BILINEAR)
        img = Image.alpha_composite(img.convert("RGBA"), rays)
        img.alpha_composite(_halftone(accent or M.GOLD, 28, 34))
    elif kind == "roast":
        img = _two_tone((46, 30, 34), (18, 16, 22), 0.9).convert("RGBA")
        img.alpha_composite(_halftone((255, 80, 60), 30, 22))
    elif kind == "court":
        img = _two_tone((92, 36, 28), (34, 16, 14), 1.35).convert("RGBA")
        d = ImageDraw.Draw(img, "RGBA")
        for x in range(0, W, 120):                                  # wood panelling
            d.rectangle((x, 0, x + 4, H), fill=(0, 0, 0, 40))
        spot = Image.new("L", (W, H), 0)
        ImageDraw.Draw(spot).ellipse((W * 0.1, H * 0.12, W * 0.9, H * 0.72), fill=150)
        img.paste(Image.new("RGBA", (W, H), (255, 220, 170, 255)), (0, 0), spot.filter(ImageFilter.GaussianBlur(120))
                  .point(lambda v: int(v * 0.35)))
    else:                                                           # versus
        img = _two_tone(tuple(int(v * 0.6) for v in p), (24, 26, 34), 2.0).convert("RGBA")
        img.alpha_composite(_halftone((255, 255, 255), 30, 16))
    g = ImageChops.offset(_grain(1), int(t * 37) % W, int(t * 53) % H)
    img = Image.composite(Image.new("RGBA", (W, H), (255, 255, 255, 255)), img, g.point(lambda v: max(0, v - 150)))
    return img.convert("RGB")


def speed_lines(img: Image.Image, t: float, color=WHITE, alpha: int = 70, n: int = 26, horizontal: bool = True):
    d = ImageDraw.Draw(img, "RGBA")
    rng = np.random.default_rng(int(t * 30) // 2)
    for _ in range(n):
        y = rng.random() * H
        x = rng.random() * W
        ln = 120 + rng.random() * 380
        if horizontal:
            d.line((x, y, x + ln, y), fill=color + (alpha,), width=int(2 + rng.random() * 5))
        else:
            d.line((x, y, x, y + ln), fill=color + (alpha,), width=int(2 + rng.random() * 5))


# ------------------------------------------------------------------------------------------ hits

def hits(c, spec: dict) -> list[float]:
    """Times (s, in this beat) when something lands: the beat's sound-cue anchors (word / number timings), so the
    picture reacts on the same frame as the sound. Silences (the hard stop) included: that's when it freezes."""
    from .series_scenes import anchor_time
    out = []
    for q in spec.get("cues", []):
        if q.get("sfx"):
            a = anchor_time(q, c.tokens)
            if a is not None:
                out.append((a, q["sfx"], q["event"]))
    return out


def hit_strength(t: float, times: list[float], decay: float = 0.18) -> float:
    return max((math.exp(-(t - h) / decay) for h in times if t >= h), default=0.0)


def shake(img: Image.Image, amount: float, t: float) -> Image.Image:
    """Jolt the frame by up to `amount` px without showing edges: crop a slightly zoomed window and shift it."""
    if amount < 0.5:
        return img
    dx, dy = amount * math.sin(t * 91), amount * math.cos(t * 67)
    m = amount + 4
    box = (m + dx, m + dy, W - m + dx, H - m + dy)
    return img.resize((W, H), Image.BILINEAR, box=box)


def flash(img: Image.Image, k: float, color=WHITE) -> Image.Image:
    if k < 0.02:
        return img
    return Image.blend(img, Image.new("RGB", img.size, color), min(0.75, k))


def burst(img: Image.Image, cx: float, cy: float, k: float, color=WHITE, rays: int = 22, r0: float = 160):
    """Comic action lines out of an impact point; k from 1 (the hit) down to 0."""
    if k <= 0.02:
        return
    d = ImageDraw.Draw(img, "RGBA")
    for i in range(rays):
        a = i / rays * 2 * math.pi + (i % 3) * 0.05
        r1 = r0 + (1 - k) * 220
        r2 = r1 + 140 + 160 * (i % 2)
        w = int(4 + 10 * k)
        d.line((cx + r1 * math.cos(a), cy + r1 * math.sin(a), cx + r2 * math.cos(a), cy + r2 * math.sin(a)),
               fill=color + (int(255 * k),), width=w)


def slam(img, text: str, cx, cy, t: float, at: float, size: int = 260, fill=WHITE, stroke: int = 10,
         stroke_fill=BLACK, angle: float = 0, weight: str = "black"):
    """A number/word that lands: big -> overshoot -> settle in 0.18 s; nothing before `at`."""
    if t < at - 0.02:
        return 0.0
    k = M.clamp((t - at) / 0.18)
    s = 1.7 - 0.7 * ease_spring(k, 1.4)
    lay = M.text_layer(text, weight, M.fit_size(text, weight, size, 980), fill, stroke, stroke_fill)
    if angle:
        lay = lay.rotate(angle, expand=True, resample=Image.BICUBIC)
    M.paste(img, lay, cx, cy, s, M.clamp(k * 3))
    return k


def sticker(img, text: str, cx, cy, k: float, color=(255, 214, 0), fg=BLACK, size: int = 54, angle: float = -6):
    """A meme sticker: bold text in a tilted, outlined box that pops in."""
    if k <= 0:
        return
    f = M.font("black", size)
    w = int(f.getlength(text)) + 50
    h = size + 40
    lay = Image.new("RGBA", (w + 16, h + 16), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    d.rounded_rectangle((8, 8, w + 8, h + 8), radius=14, fill=color + (255,), outline=BLACK + (255,), width=6)
    d.text((w / 2 + 8, h / 2 + 8), text, font=f, fill=fg, anchor="mm")
    lay = lay.rotate(angle, expand=True, resample=Image.BICUBIC)
    M.paste(img, lay, cx, cy, 0.4 + 0.6 * ease_spring(k, 1.8), M.clamp(k * 3))


def freeze(img: Image.Image, k: float) -> Image.Image:
    """The hard stop's look: colour drains, edges darken. k 0..1."""
    if k <= 0:
        return img
    grey = ImageOps.grayscale(img).convert("RGB")
    out = Image.blend(img, grey, 0.8 * k)
    vig = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vig).ellipse((-W * 0.25, -H * 0.1, W * 1.25, H * 1.1), fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(160))
    return Image.composite(out, Image.blend(out, Image.new("RGB", (W, H), BLACK), 0.6 * k), vig)


def outlined(img, text: str, cx, cy, size: int, color, alpha: float = 1.0, width: int = 6):
    """Hollow outlined type (for giant numbers behind the player)."""
    f = M.font("black", size)
    lay = Image.new("RGBA", (int(f.getlength(text)) + 60, size + 80), (0, 0, 0, 0))
    ImageDraw.Draw(lay).text((30, 20), text, font=f, fill=(0, 0, 0, 0), stroke_width=width,
                             stroke_fill=color + (int(255 * alpha),))
    M.paste(img, lay, cx, cy, 1.0, 1.0)
