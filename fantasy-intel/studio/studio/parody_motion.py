"""In-house motion for the AI parody show: one generated still, moved like a shot, at no cost and in seconds.

The still is split in two: the subject (a mask from Apple Vision, stages/frames.py) and a softened background. The
two drift at different speeds, which reads as depth, and a hit on a word bumps the subject. Without a mask the whole
still moves as one layer. Same inputs, same frames, so a render is reproducible.

Every scene carries the AI PARODY badge on every frame, inside the area the three apps leave clear. A beat may instead
hold a Wan clip (ai_hero, optional, parody_wan.py); once the clip runs out its last frame holds under the badge.
Every number drawn is a claim's display string, set in code: the generated pictures hold no text.
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from . import motion as M

OVERSCAN = 1.2                 # the still is larger than the frame, so a move never shows an edge
# start zoom, end zoom, horizontal travel, vertical travel (pixels, centred on the move's midpoint)
MOVES = {"push": (1.0, 1.1, 0, 0), "pull": (1.1, 1.0, 0, 0), "drift_left": (1.06, 1.06, 56, 0),
         "drift_right": (1.06, 1.06, -56, 0), "rise": (1.05, 1.08, 0, 56)}
BACK_RATE, FRONT_RATE = 0.5, 1.3        # how far each layer travels against the move: this difference is the depth
FRONT_ZOOM = 0.04                       # the subject also grows a little faster than its background
HIT_BUMP, HIT_DECAY = 0.07, 0.18
RED = (214, 36, 36)
BADGE_TEXT = "AI PARODY"
BADGE_Y = 262                           # below the apps' top bars, above every stat
CLIP_FPS = 30


@lru_cache(maxsize=1)
def _badge() -> Image.Image:
    f = M.font("black", 46)
    w = int(f.getlength(BADGE_TEXT)) + 52
    lay = Image.new("RGBA", (w, 78), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    d.rounded_rectangle((0, 0, w - 1, 77), radius=14, fill=RED + (255,))
    d.text((w / 2, 39), BADGE_TEXT, font=f, fill=(255, 255, 255, 255), anchor="mm")
    return lay


BADGE_BOX = (int(M.CENTER_X - _badge().width / 2), BADGE_Y - 39, int(M.CENTER_X + _badge().width / 2), BADGE_Y + 39)


def badge(img: Image.Image) -> None:
    lay = _badge()
    img.paste(lay, BADGE_BOX[:2], lay)


@lru_cache(maxsize=8)
def layers(path: str, mask: str | None) -> tuple[Image.Image, Image.Image | None]:
    src = Image.open(path).convert("RGB")
    k = max(M.W / src.width, M.H / src.height) * OVERSCAN
    base = src.resize((round(src.width * k), round(src.height * k)), Image.LANCZOS)
    if mask is None:
        return base, None
    front = base.copy()
    front.putalpha(Image.open(mask).convert("L").resize(base.size, Image.BILINEAR).filter(ImageFilter.GaussianBlur(3)))
    return base.filter(ImageFilter.GaussianBlur(5)), front


def _view(layer: Image.Image, zoom: float, dx: float, dy: float) -> Image.Image:
    w, h = M.W / zoom, M.H / zoom
    x, y = (layer.width - w) / 2 + dx, (layer.height - h) / 2 + dy
    return layer.resize((M.W, M.H), Image.BICUBIC, box=(x, y, x + w, y + h))


def frame(still, mask, t: float, dur: float, move: str, hit: float | None = None) -> Image.Image:
    z0, z1, travel_x, travel_y = MOVES[move]
    p = M.clamp(t / max(dur, 0.1))
    zoom = z0 + (z1 - z0) * p
    dx, dy = travel_x * (p - 0.5), travel_y * (p - 0.5)
    bump = HIT_BUMP * math.exp(-(t - hit) / HIT_DECAY) if hit is not None and t >= hit else 0.0
    back, front = layers(str(still), str(mask) if mask else None)
    if front is None:
        return _view(back, zoom * (1 + bump), dx, dy)
    out = _view(back, zoom, BACK_RATE * dx, BACK_RATE * dy)
    top = _view(front, zoom * (1 + FRONT_ZOOM * p + bump), FRONT_RATE * dx, FRONT_RATE * dy)
    out.paste(top, (0, 0), top)
    return out


def _stats(img: Image.Image, t: float, spec: dict, c) -> None:
    """The beat's numbers, each landing as it is said, on a dark band so they read over any picture."""
    items = spec.get("stats") or []
    times = [c.at(claim=s["claim"], default=0.0) for s in items]
    shown = [(s, at) for s, at in zip(items, times) if t >= at - 0.05]
    if not shown:
        return
    gap = 250
    top = 900 - (len(items) - 1) * gap / 2
    band = Image.new("L", (M.W, int(len(items) * gap + 60)), 120)
    img.paste((0, 0, 0), (0, int(top - gap / 2 - 30)), band)
    for i, (s, at) in enumerate(zip(items, times)):
        if t < at - 0.05:
            continue
        k = M.back_out(M.progress(t, at - 0.05, 0.22))
        y = top + i * gap
        M.put_text(img, c.d(s["claim"]), M.CENTER_X, y - 40, size=170, fill=M.GOLD, scale=0.6 + 0.4 * k,
                   alpha=M.clamp(k * 2), max_width=M.SAFE_RIGHT - M.SAFE_LEFT)
        M.put_text(img, s["label"], M.CENTER_X, y + 70, size=50, fill=M.INK, alpha=M.clamp(k * 2),
                   max_width=M.SAFE_RIGHT - M.SAFE_LEFT)


def _clip_frame(spec: dict, t: float, c) -> Image.Image:
    """Each beat has its own reader, because one reads forward only and the same clip can open two beats."""
    from .media_scenes import ClipReader
    key = (spec["art"], spec.get("_start", 0.0))
    if key not in c.clips:
        c.clips[key] = ClipReader(str(Path(c.episode_dir) / spec["clip"]))
    return c.clips[key].frame(int(t * CLIP_FPS))


def scene_ai(img: Image.Image, t: float, dur: float, spec: dict, c) -> None:
    if spec.get("clip"):
        picture = _clip_frame(spec, t, c)
    else:
        art = Path(c.episode_dir) / "art"
        mask = art / f"{spec['art']}.mask.png"
        hit = c.at(word=spec["hit_word"], default=None) if spec.get("hit_word") else None
        picture = frame(art / f"{spec['art']}.png", mask if mask.exists() else None, t, dur, spec["move"], hit)
    img.paste(picture)
    _stats(img, t, spec, c)
    badge(img)


SCENES = {"ai_still": scene_ai, "ai_hero": scene_ai}
