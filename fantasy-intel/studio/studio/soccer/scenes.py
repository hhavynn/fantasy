"""Soccer scenes, drawn entirely from code: a plain pitch, anonymous dots, cards and type.

No photos, crests, kits or footage ever enter this module, and the content gate (``soccer/qc.py``) holds
scripts to the scene types listed in ``SCENES``. Motion is keyed to the narration through ``Ctx.at``.
"""

from __future__ import annotations

import re
from functools import lru_cache

from PIL import Image, ImageDraw

from .. import motion as M
from . import toons

GRASS = (20, 62, 44)
GRASS_DARK = (15, 50, 36)
LINE = (214, 232, 222)
TEAM = {"a": (106, 174, 255), "d": (255, 92, 92), "k": (255, 205, 64)}
CARD = {"yellow": (255, 214, 10), "red": (232, 44, 44)}
PITCH_BOX = (70, 260, 1010, 1320)


class Ctx:
    def __init__(self, tokens: list[dict] | None = None, accent=M.GOLD):
        self.tokens, self.accent = tokens or [], accent

    def at(self, word: str | None, default: float = 0.0) -> float:
        for tok in self.tokens:
            if word and re.sub(r"[^a-z]", "", tok["text"].lower()).startswith(word.lower()):
                return tok["start"]
        return default


@lru_cache(maxsize=1)
def backdrop() -> Image.Image:
    img = Image.new("RGB", (M.W, M.H), GRASS_DARK)
    d = ImageDraw.Draw(img)
    for y in range(0, M.H, 320):
        d.rectangle((0, y, M.W, y + 160), fill=GRASS)
    return img


@lru_cache(maxsize=1)
def pitch_layer() -> Image.Image:
    """The attacking half: goal at the top, halfway line at the bottom."""
    x0, y0, x1, y1 = PITCH_BOX
    img = backdrop().copy()
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(PITCH_BOX, 18, fill=GRASS)
    bands = 6
    for i in range(0, bands, 2):
        top = y0 + (y1 - y0) * i / bands
        d.rectangle((x0 + 2, top, x1 - 2, top + (y1 - y0) / bands), fill=GRASS_DARK)
    w, h = x1 - x0, y1 - y0
    d.rectangle((x0 + 24, y0 + 20, x1 - 24, y1 - 2), outline=LINE, width=5)
    d.rectangle((x0 + w * 0.2, y0 + 20, x1 - w * 0.2, y0 + h * 0.3), outline=LINE, width=5)
    d.rectangle((x0 + w * 0.37, y0 + 20, x1 - w * 0.37, y0 + h * 0.11), outline=LINE, width=5)
    d.arc((x0 + w * 0.35, y1 - w * 0.15, x1 - w * 0.35, y1 + w * 0.15), 180, 360, fill=LINE, width=5)
    d.rectangle((x0 + w * 0.42, y0 + 6, x1 - w * 0.42, y0 + 20), outline=LINE, width=5)
    return img


def place(x: float, y: float) -> tuple[float, float]:
    x0, y0, x1, y1 = PITCH_BOX
    return x0 + 24 + x * (x1 - x0 - 48), y0 + 20 + y * (y1 - y0 - 22)


def dot(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, color, ring=None) -> None:
    if ring:
        d.ellipse((cx - r - 12, cy - r - 12, cx + r + 12, cy + r + 12), outline=ring, width=6)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color, outline=(8, 12, 12), width=4)


def verdict(img: Image.Image, t: float, at: float, text: str, color) -> None:
    k = M.back_out(M.progress(t, at, 0.28))
    if k > 0:
        M.put_text(img, text, M.CENTER_X, 1190, size=104, fill=color, scale=max(k, 0.01), stroke=8)


def scene_pitch(img, t, dur, spec, c: Ctx):
    img.paste(pitch_layer())
    d = ImageDraw.Draw(img, "RGBA")
    if spec.get("line") is not None:
        k = M.ease_out(M.progress(t, c.at(spec.get("line_at"), 0.1), 0.35))
        _, y = place(0, spec["line"])
        x0, _ = place(0, 0)
        x1, _ = place(1, 0)
        d.line((x0, y, x0 + (x1 - x0) * k, y), fill=(*M.GOLD, 230), width=7)
    mark = spec.get("mark") or {}
    for p in spec["players"]:
        x, y = place(p["x"], p["y"])
        lit = p["id"] == mark.get("id") and t >= c.at(mark.get("at"))
        dot(d, x, y, 36, TEAM[p["team"]], ring=M.GOLD if lit else None)
    bx, by = spec["ball"]
    move = spec.get("ball_to")
    if move:
        k = M.ease_in_out(M.progress(t, c.at(move["at"], 0.3), 0.55))
        bx, by = M.lerp(bx, move["to"][0], k), M.lerp(by, move["to"][1], k)
    x, y = place(bx, by)
    dot(d, x + 34, y - 30, 16, (248, 248, 244))
    if spec.get("verdict"):
        v = spec["verdict"]
        verdict(img, t, c.at(v["at"], dur * 0.5), v["text"], M.UP if v["good"] else M.GOLD)


def scene_title(img, t, dur, spec, c: Ctx):
    img.paste(backdrop())
    k = M.back_out(M.progress(t, 0.0, 0.3))
    M.put_text(img, spec["sub"], M.CENTER_X, 620, size=54, fill=c.accent, alpha=M.clamp(t * 5))
    size = M.fit_size(spec["text"], "black", 230, 860)
    M.put_text(img, spec["text"], M.CENTER_X, 800, size=size, scale=max(k, 0.01))
    scene_ball(img, t)


def scene_ball(img, t):
    d = ImageDraw.Draw(img, "RGBA")
    x = M.CENTER_X - 300 + 600 * M.ease_in_out(M.clamp((t % 2.4) / 2.4))
    dot(d, x, 1040, 30, (248, 248, 244))


def scene_columns(img, t, dur, spec, c: Ctx):
    img.paste(backdrop())
    for head, items, at, x, color in ((spec["left"], spec["left_items"], spec["left_at"], 300, M.UP),
                                      (spec["right"], spec["right_items"], spec["right_at"], 780, M.DOWN)):
        start = c.at(at, 0.2)
        M.put_text(img, head, x, 480, size=74, fill=color, alpha=M.clamp((t - start) * 6))
        for i, item in enumerate(items):
            k = M.back_out(M.progress(t, start + i * 0.18, 0.25))
            if k > 0:
                size = M.fit_size(item, "black", 72, 420)
                M.put_text(img, item, x, 650 + i * 140, size=size, scale=max(k, 0.01))


def scene_stack(img, t, dur, spec, c: Ctx):
    img.paste(backdrop())
    M.put_text(img, spec["head"], M.CENTER_X, 380, size=70, fill=c.accent, alpha=M.clamp(t * 5))
    n = len(spec["items"])
    for i, (item, at) in enumerate(zip(spec["items"], spec["at"])):
        start = c.at(at, dur * (i + 1) / (n + 1))
        k = M.back_out(M.progress(t, start, 0.25))
        if k > 0:
            size = M.fit_size(item, "black", 92, 860)
            M.put_text(img, item, M.CENTER_X, 560 + i * 170, size=size, scale=max(k, 0.01))


def scene_card(img, t, dur, spec, c: Ctx):
    img.paste(backdrop())
    colors = spec["colors"]
    gap = min(760 / len(colors), 300)
    ats = spec.get("at") or [None] * len(colors)
    d = ImageDraw.Draw(img, "RGBA")
    for i, (name, at) in enumerate(zip(colors, ats)):
        start = c.at(at, 0.1 + i * 0.2)
        k = M.back_out(M.progress(t, start, 0.25))
        if k <= 0:
            continue
        cx = M.CENTER_X + (i - (len(colors) - 1) / 2) * gap
        w, h = 210 * k, 310 * k
        d.rounded_rectangle((cx - w / 2, 760 - h / 2, cx + w / 2, 760 + h / 2), 22, fill=CARD[name])
    start = c.at(spec.get("label_at"), dur * 0.45)
    k = M.back_out(M.progress(t, start, 0.25))
    if k > 0:
        size = M.fit_size(spec["label"], "black", 110, 860)
        M.put_text(img, spec["label"], M.CENTER_X, 1120, size=size, fill=c.accent, scale=max(k, 0.01))


def scene_stamp(img, t, dur, spec, c: Ctx):
    img.paste(backdrop())
    k = M.back_out(M.progress(t, c.at(spec.get("at"), 0.2), 0.3))
    if k > 0:
        size = M.fit_size(spec["text"], "black", 150, 860)
        M.put_text(img, spec["text"], M.CENTER_X, 800, size=size, fill=c.accent, scale=max(k, 0.01))


def scene_cta(img, t, dur, spec, c: Ctx):
    img.paste(backdrop())
    k = M.back_out(M.progress(t, 0.0, 0.3))
    M.put_text(img, spec["text"], M.CENTER_X, 740, size=170, fill=c.accent, scale=max(k, 0.01))
    M.put_text(img, spec["sub"], M.CENTER_X, 900, size=60, alpha=M.clamp(t * 4))


SCENES = {"pitch": scene_pitch, "title": scene_title, "columns": scene_columns, "stack": scene_stack,
          "card": scene_card, "stamp": scene_stamp, "cta": scene_cta, "toon": toons.scene_toon,
          "split": toons.scene_split, "fact": toons.scene_fact}


def draw(spec: dict, t: float, dur: float, c: Ctx) -> Image.Image:
    img = Image.new("RGB", (M.W, M.H), M.DARK)
    SCENES[spec["type"]](img, t, dur, spec, c)
    if spec.get("badge"):
        toons.badge(img, spec["badge"])
    return img
