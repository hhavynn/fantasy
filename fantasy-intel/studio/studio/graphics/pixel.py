"""Pixel-art toolkit: an original 5x7 bitmap font, sprites drawn on the art grid, and a frame that logs text.

Everything is drawn at the logical art resolution (216x384) with hard edges, then scaled x5 with nearest
neighbour, so one art pixel is always a 5x5 block of video pixels. No anti-aliasing, no gradients, no
rotation, no fractional scaling: those are what make "AI pixel art" look wrong. Sprites are built from
simple shapes at native size and outlined with a 1 px dark line, the way hand-made sprites are.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw

from .kit import contrast, hexrgb, tokens

GLYPHS = {
    "A": ".###.|#...#|#...#|#####|#...#|#...#|#...#", "B": "####.|#...#|#...#|####.|#...#|#...#|####.",
    "C": ".###.|#...#|#....|#....|#....|#...#|.###.", "D": "####.|#...#|#...#|#...#|#...#|#...#|####.",
    "E": "#####|#....|#....|####.|#....|#....|#####", "F": "#####|#....|#....|####.|#....|#....|#....",
    "G": ".###.|#...#|#....|#.###|#...#|#...#|.####", "H": "#...#|#...#|#...#|#####|#...#|#...#|#...#",
    "I": "###|.#.|.#.|.#.|.#.|.#.|###", "J": "..###|...#.|...#.|...#.|#..#.|#..#.|.##..",
    "K": "#...#|#..#.|#.#..|##...|#.#..|#..#.|#...#", "L": "#....|#....|#....|#....|#....|#....|#####",
    "M": "#...#|##.##|#.#.#|#.#.#|#...#|#...#|#...#", "N": "#...#|#...#|##..#|#.#.#|#..##|#...#|#...#",
    "O": ".###.|#...#|#...#|#...#|#...#|#...#|.###.", "P": "####.|#...#|#...#|####.|#....|#....|#....",
    "Q": ".###.|#...#|#...#|#...#|#.#.#|#..#.|.##.#", "R": "####.|#...#|#...#|####.|#.#..|#..#.|#...#",
    "S": ".####|#....|#....|.###.|....#|....#|####.", "T": "#####|..#..|..#..|..#..|..#..|..#..|..#..",
    "U": "#...#|#...#|#...#|#...#|#...#|#...#|.###.", "V": "#...#|#...#|#...#|#...#|#...#|.#.#.|..#..",
    "W": "#...#|#...#|#...#|#.#.#|#.#.#|##.##|#...#", "X": "#...#|#...#|.#.#.|..#..|.#.#.|#...#|#...#",
    "Y": "#...#|#...#|.#.#.|..#..|..#..|..#..|..#..", "Z": "#####|....#|...#.|..#..|.#...|#....|#####",
    "0": ".###.|#...#|#..##|#.#.#|##..#|#...#|.###.", "1": "..#..|.##..|..#..|..#..|..#..|..#..|.###.",
    "2": ".###.|#...#|....#|...#.|..#..|.#...|#####", "3": "####.|....#|....#|.###.|....#|....#|####.",
    "4": "...#.|..##.|.#.#.|#..#.|#####|...#.|...#.", "5": "#####|#....|####.|....#|....#|#...#|.###.",
    "6": "..##.|.#...|#....|####.|#...#|#...#|.###.", "7": "#####|....#|...#.|..#..|.#...|.#...|.#...",
    "8": ".###.|#...#|#...#|.###.|#...#|#...#|.###.", "9": ".###.|#...#|#...#|.####|....#|...#.|.##..",
    "%": "##..#|##..#|...#.|..#..|.#...|#..##|#..##", ".": ".|.|.|.|.|.|#", ",": "..|..|..|..|..|.#|#.",
    ":": ".|.|#|.|.|#|.", "!": "#|#|#|#|#|.|#", "?": ".###.|#...#|....#|...#.|..#..|.....|..#..",
    "'": "#|#|.|.|.|.|.", "-": "...|...|...|###|...|...|...", "+": ".....|..#..|..#..|#####|..#..|..#..|.....",
    "/": "....#|....#|...#.|..#..|.#...|#....|#....", "(": ".#|#.|#.|#.|#.|#.|.#", ")": "#.|.#|.#|.#|.#|.#|#.",
    "→": ".....|..#..|...#.|#####|...#.|..#..|.....", "▶": "#...|##..|###.|####|###.|##..|#...",
    "·": ".|.|.|#|.|.|.", "#": ".#.#.|.#.#.|#####|.#.#.|#####|.#.#.|.#.#.", "=": ".....|.....|#####|.....|#####|.....|.....",
    "&": ".##..|#..#.|.##..|.#...|#.#.#|#..#.|.##.#", '"': "#.#|#.#|...|...|...|...|...",
    "≠": "....#|...#.|#####|..#..|#####|.#...|#....",
}
ALIASES = {"–": "-", "—": "-", "−": "-", "’": "'", "“": '"', "”": '"', ";": ":"}
SPACE = 3
GAP = 1
LINE = 9


@lru_cache(maxsize=None)
def glyph(ch: str) -> np.ndarray:
    ch = ALIASES.get(ch, ch).upper()
    rows = GLYPHS.get(ch, GLYPHS["?"]).split("|")
    return np.array([[c == "#" for c in r] for r in rows], bool)


def text_width(text: str, scale: int = 1) -> int:
    w = 0
    for ch in text:
        w += (SPACE if ch == " " else glyph(ch).shape[1]) + GAP
    return max(0, w - GAP) * scale


def supported(text: str) -> bool:
    return all(ch == " " or ALIASES.get(ch, ch).upper() in GLYPHS for ch in text)


@lru_cache(maxsize=1024)
def text_mask(text: str, scale: int = 1) -> Image.Image:
    """1-bit text at an integer scale (each font pixel becomes scale x scale art pixels)."""
    w = max(1, text_width(text, 1))
    m = np.zeros((7, w), bool)
    x = 0
    for ch in text:
        if ch == " ":
            x += SPACE + GAP
            continue
        g = glyph(ch)
        m[:, x:x + g.shape[1]] |= g
        x += g.shape[1] + GAP
    img = Image.fromarray((m * 255).astype(np.uint8), "L")
    return img.resize((w * scale, 7 * scale), Image.NEAREST) if scale > 1 else img


def wrap(text: str, max_px: int, scale: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        cand = f"{cur} {word}".strip()
        if cur and text_width(cand, scale) > max_px:
            lines.append(cur)
            cur = word
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


# ------------------------------------------------------------------------------------------ palette

def palette() -> dict[str, tuple[int, int, int]]:
    return {k: hexrgb(v) for k, v in vars(tokens("pixel").palette).items()}


def team_slots(primary, secondary) -> tuple[tuple, tuple]:
    """The two palette-swap slots for a team: its colours, nudged so they read on the night background."""
    P = palette()

    def lift(c):
        lum = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
        return c if lum > 45 else P["slate"]
    p = lift(primary)
    s = secondary if contrast(secondary, p) > 1.6 else P["white"]
    return p, s


# ------------------------------------------------------------------------------------------ sprites

def outline(spr: Image.Image, color) -> Image.Image:
    """A 1 px outline around every opaque pixel (4-neighbourhood), the hand-made sprite look."""
    a = np.asarray(spr.getchannel("A")) > 0
    edge = np.zeros_like(a)
    edge[1:, :] |= a[:-1, :]
    edge[:-1, :] |= a[1:, :]
    edge[:, 1:] |= a[:, :-1]
    edge[:, :-1] |= a[:, 1:]
    edge &= ~a
    out = np.asarray(spr).copy()
    out[edge] = (*color, 255)
    return Image.fromarray(out, "RGBA")


@lru_cache(maxsize=32)
def football(ghost: bool = False, tint: str | None = None) -> Image.Image:
    """A 13x7 football: leather, a white lace line with three stitches, a dark outline. `tint` swaps the
    leather for a palette colour ("gold" marks the story player's own, "silver" another player's)."""
    P = palette()
    body, edge = {"gold": (P["gold"], P["leather_dark"]), "silver": (P["mist"], P["steel"])}.get(
        tint, (P["leather"], P["leather_dark"]))
    rows = ["....#####....", "..##LLLLL##..", ".#LLLWLWLLLL#", "#LLLWWWWWLLL#", ".#LLLWLWLLLL#",
            "..##LLLLL##..", "....#####...."]
    img = Image.new("RGBA", (13, 7), (0, 0, 0, 0))
    px = img.load()
    for y, r in enumerate(rows):
        for x, c in enumerate(r):
            if ghost:
                if c == "#" and (x + y) % 2 == 0:
                    px[x, y] = (*P["mist"], 255)
            elif c == "#":
                px[x, y] = (*edge, 255)
            elif c == "L":
                px[x, y] = (*body, 255)
            elif c == "W":
                px[x, y] = (*P["white"], 255)
    return img


@lru_cache(maxsize=32)
def player(primary: tuple, secondary: tuple, number: str, h: int = 60) -> Image.Image:
    """An original front-facing player sprite in team colours with the roster number: helmet with a centre
    stripe, a dark visor behind a two-bar facemask, shoulder pads, the jersey and pants. Built from shapes at
    native resolution, then outlined; 40x60 art pixels at the default size."""
    P = palette()
    w = int(h * 0.68)
    u = h / 60
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = w // 2

    def R(x0, y0, x1, y1, c):
        d.rectangle((round(cx + x0 * u), round(y0 * u), round(cx + x1 * u) - 1, round(y1 * u) - 1), fill=(*c, 255))

    dark = tuple(max(0, int(v * 0.62)) for v in primary)
    light = tuple(min(255, int(v + (255 - v) * 0.35)) for v in primary)
    # legs and socks
    R(-9, 50, -2, 60, primary)
    R(2, 50, 9, 60, primary)
    R(-9, 57, -2, 60, P["night"])
    R(2, 57, 9, 60, P["night"])
    # pants with side stripes
    R(-10, 40, 10, 51, P["mist"])
    R(-10, 40, -8, 51, secondary)
    R(8, 40, 10, 51, secondary)
    R(-1, 46, 1, 51, P["steel"])
    # arms (a 1 px gap from the torso so the outline separates them), sleeve stripes, gloves
    for sgn in (-1, 1):
        x0, x1 = (-19, -14) if sgn < 0 else (14, 19)
        R(x0, 21, x1, 38, primary)
        R(x0, 26, x1, 28, secondary)
        R(x0, 38, x1, 42, secondary)
        R(x1 - 2, 21, x1, 38, dark) if sgn > 0 else None
    # torso, shading on the right, belt line
    R(-13, 22, 13, 41, primary)
    R(7, 22, 13, 41, dark)
    R(-13, 39, 13, 41, dark)
    # shoulder pads: a rounded yoke with a lit top edge
    d.ellipse((round(cx - 18 * u), round(15 * u), round(cx + 18 * u) - 1, round(27 * u) - 1), fill=(*primary, 255))
    R(-14, 16, 10, 17, light)
    R(9, 18, 17, 24, dark)
    # neck
    R(-4, 15, 4, 18, P["night"])
    # helmet: shell with a shaded back, centre stripe, visor, facemask, highlight
    d.ellipse((round(cx - 10 * u), 0, round(cx + 10 * u) - 1, round(19 * u) - 1), fill=(*primary, 255))
    R(5, 4, 9, 14, dark)
    R(-1, 0, 1, 9, secondary)
    R(-7, 9, 7, 16, P["night"])
    R(-8, 11, 8, 12, P["mist"])
    R(-8, 14, 8, 15, P["mist"])
    R(-5, 2, -2, 4, light)
    R(-7, 4, -5, 6, light)
    # roster number on the chest
    if number:
        m = text_mask(number[:2], 1)
        sc = 2 if h >= 90 else 1
        if sc > 1:
            m = m.resize((m.width * sc, m.height * sc), Image.NEAREST)
        num = Image.new("RGBA", m.size, (*P["white"], 255))
        num.putalpha(m)
        img.alpha_composite(num, (cx - m.width // 2, round(25 * u)))
    return outline(img, P["night"])


@lru_cache(maxsize=8)
def turf(w: int = 216, h: int = 384 * 2) -> Image.Image:
    """A tall strip of field: mowing stripes, chalk yard lines every 48 px, hash marks, no anti-aliasing."""
    P = palette()
    img = Image.new("RGB", (w, h), P["turf"])
    d = ImageDraw.Draw(img)
    for y in range(0, h, 48):
        d.rectangle((0, y, w - 1, y + 23), fill=P["turf_light"])
    for y in range(0, h, 48):
        d.rectangle((0, y, w - 1, y), fill=P["chalk"])
        for x in (60, 152):
            for k in (12, 24, 36):
                d.rectangle((x, y + k, x + 3, y + k), fill=P["chalk"])
    # darken the outer columns a step (no gradients: a hard step)
    d.rectangle((0, 0, 9, h - 1), fill=P["turf_dark"])
    d.rectangle((w - 10, 0, w - 1, h - 1), fill=P["turf_dark"])
    return img


# ------------------------------------------------------------------------------------------- frame

class PixelFrame:
    """Drawing on the art grid; text is logged in video pixels (x scale) for the visual QA."""

    def __init__(self, scale: int = 5, measure: bool = False, size=(216, 384)):
        self.scale = scale
        self.img = Image.new("RGB", size, palette()["night"])
        self.d = ImageDraw.Draw(self.img)
        self.log: list[dict] = []
        self.measure = measure

    def rect(self, box, fill):
        x0, y0, x1, y1 = (int(round(v)) for v in box)
        if x1 > x0 and y1 > y0:
            self.d.rectangle((x0, y0, x1 - 1, y1 - 1), fill=fill)

    def frame_box(self, box, fill, border, shadow=None):
        """A panel: fill, a 1 px border, and a hard 1 px drop shadow (no blur)."""
        x0, y0, x1, y1 = (int(round(v)) for v in box)
        if shadow:
            self.rect((x0 + 2, y0 + 2, x1 + 2, y1 + 2), shadow)
        self.rect((x0, y0, x1, y1), border)
        self.rect((x0 + 1, y0 + 1, x1 - 1, y1 - 1), fill)

    def sprite(self, spr: Image.Image, x: float, y: float):
        self.img.paste(spr, (int(round(x)), int(round(y))), spr)

    def text(self, text: str, x: float, y: float, scale: int, color, anchor: str = "l", src=None,
             role: str = "label", shadow=None, visible: bool = True):
        w = text_width(text, scale)
        x0 = int(round(x - (w // 2 if anchor == "c" else w if anchor == "r" else 0)))
        y0 = int(round(y))
        box = (x0, y0, x0 + w, y0 + 7 * scale)
        if not visible:
            return box
        m = text_mask(text, scale)
        entry = {"text": text, "box": [v * self.scale for v in box], "cap_px": 7 * scale * self.scale,
                 "fill": list(color), "alpha": 1.0, "src": src, "role": role}
        if self.measure:
            # contrast against what is under the letters (before they are drawn): the worst of 10/50/90 pct
            under = np.asarray(self.img.crop(box), np.float32)[np.asarray(m) > 0]
            if len(under):
                lum = under @ np.array([0.2126, 0.7152, 0.0722], np.float32)
                order = np.argsort(lum)
                entry["contrast"] = round(float(min(contrast(color, under[order[int(len(order) * q)]])
                                              for q in (0.1, 0.5, 0.9))), 2)
        if shadow:
            self.img.paste(Image.new("RGB", m.size, shadow), (x0 + scale, y0 + scale), m)
        self.img.paste(Image.new("RGB", m.size, color), (x0, y0), m)
        self.log.append(entry)
        return box

    def upscale(self) -> Image.Image:
        return self.img.resize((self.img.width * self.scale, self.img.height * self.scale), Image.NEAREST)
