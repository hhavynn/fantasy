"""Scenes for the flame / praise series formats (stages/showrunner.py). Five looks, each its own composition,
motion and sound, chosen per story; none of them is a chart by default.

  deadpan    (roast_monologue) flat graphite, white type, a settings toggle, one number, long stillness
               dp_toggle  a phone settings row ("AIRPLANE MODE") switched on as the joke lands
               dp_stat    one number alone, the benchmark small beneath it, a bar for each
               dp_line    a typed line or two; a footnote of the claims behind it
               dp_end     a pause, then the punchline typed out; nothing moves after it
  court      (courtroom, a parody, labelled so on every frame) wood panelling, paper, stamps, a gavel
               court_docket / court_exhibit / court_verdict
  versus     (comparison) a top-down field measured in yards, or footballs counted against a whole offense
               cmp_field (stages league, him, season, safety) / cmp_tally (stages him, them, teams, end)
  cinematic  (main_character) letterboxed, team-coloured light, title cards, slams, a movie poster
               cine_title / cine_slam / cine_contrast / cine_poster
  meme       (fan_meltdown) pastel calm turning red hot, text that grows and shakes, a scream, a hard cut
               melt_calm / melt_stat / melt_scream

Every number drawn is a claim's display string (c.d). Backdrops stay above the QC black-frame threshold.
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import motion as M
from .format_scenes import PAPER, PAPER_INK, brand, stamp
from .scenes import football

GRAPHITE = (40, 40, 46)
WOOD_DARK, WOOD = (64, 38, 22), (104, 64, 36)
MANILA = (222, 190, 132)
COURT_RED = (190, 30, 36)
TURF, TURF2 = (34, 112, 58), (40, 124, 64)
TOGGLE_ON = (255, 159, 10)
STYLES = ("deadpan", "court", "versus", "cinematic", "meme")


def fill(c, template: str, values: dict) -> str:
    return template.format(**{k: c.d(v) for k, v in values.items()}) if values else template


def _short(name: str) -> str:
    parts = [p for p in name.split() if p not in ("Jr.", "Sr.", "II", "III", "IV")]
    return parts[-1]


def _typed(text: str, t: float, start: float, cps: float = 34) -> str:
    n = int(max(0.0, t - start) * cps)
    return text[:n]


# ------------------------------------------------------------------------------------------ backdrops

@lru_cache(maxsize=2)
def _graphite() -> Image.Image:
    yy, xx = np.mgrid[0:M.H, 0:M.W].astype(np.float32)
    r = np.sqrt(((xx - M.W / 2) / M.W) ** 2 + ((yy - M.H * 0.4) / M.H) ** 2)
    k = 1 - 0.35 * np.clip(r * 1.4, 0, 1)
    base = np.array(GRAPHITE, np.float32)[None, None] * (0.95 + 0.25 * k[..., None])
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")


@lru_cache(maxsize=2)
def _wood() -> Image.Image:
    img = Image.new("RGB", (M.W, M.H), WOOD_DARK)
    d = ImageDraw.Draw(img)
    rng = np.random.default_rng(3)
    for i, x in enumerate(range(0, M.W, 180)):                   # vertical panels, each its own tone
        tone = tuple(int(c * (0.85 + 0.25 * rng.random())) for c in WOOD)
        d.rectangle((x + 4, 0, x + 176, M.H), fill=tone)
        for _ in range(9):                                      # grain
            gx = x + 10 + rng.random() * 160
            d.line((gx, 0, gx + rng.random() * 30 - 15, M.H), fill=tuple(int(c * 0.86) for c in tone), width=2)
    return img.filter(ImageFilter.GaussianBlur(1.2))


@lru_cache(maxsize=2)
def _field() -> Image.Image:
    img = Image.new("RGB", (M.W, M.H), TURF)
    d = ImageDraw.Draw(img)
    for i, y in enumerate(range(0, M.H, 200)):
        d.rectangle((0, y, M.W, y + 100), fill=TURF2)
    return img


@lru_cache(maxsize=8)
def _cinema(primary: tuple) -> Image.Image:
    yy, xx = np.mgrid[0:M.H, 0:M.W].astype(np.float32)
    r = np.sqrt(((xx - M.W * 0.5) / M.W) ** 2 + ((yy - M.H * 0.35) / M.H) ** 2)
    glow = np.clip(1 - r * 1.3, 0, 1) ** 1.5
    p = np.array(M.vivid(primary, primary), np.float32)
    base = np.array((40, 38, 50), np.float32)[None, None] + glow[..., None] * p * 0.55
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")


def _meme_color(level: float) -> tuple:
    calm, hot = np.array((126, 176, 222)), np.array((236, 70, 46))
    k = M.clamp(level / 4)
    return tuple(int(v) for v in calm * (1 - k) + hot * k)


def background(style: str, c, t: float, scene: dict | None = None) -> Image.Image:
    if style == "deadpan":
        return _graphite().copy()
    if style == "court":
        return _wood().copy()
    if style == "versus":
        return _field().copy()
    if style == "cinematic":
        img = _cinema(c.primary).copy()
        d = ImageDraw.Draw(img, "RGBA")
        x = ((t * 140) % (M.W + 800)) - 400                     # one slow light sweep
        d.polygon([(x, 0), (x + 160, 0), (x - 300, M.H), (x - 460, M.H)], fill=(255, 255, 255, 12))
        d.rectangle((0, 0, M.W, 150), fill=(0, 0, 0, 255))      # letterbox, clear of the captions and the UI
        d.rectangle((0, M.H - 150, M.W, M.H), fill=(0, 0, 0, 255))
        return img
    level = (scene or {}).get("level", 0 if (scene or {}).get("type") == "melt_calm" else 5)
    img = Image.new("RGB", (M.W, M.H), _meme_color(level))
    if level >= 3:                                              # heat lines radiating from the centre
        d = ImageDraw.Draw(img, "RGBA")
        for i in range(24):
            a = i / 24 * 2 * math.pi + t * 0.6
            d.polygon([(M.W / 2, 760), (M.W / 2 + 1600 * math.cos(a), 760 + 1600 * math.sin(a)),
                       (M.W / 2 + 1600 * math.cos(a + 0.09), 760 + 1600 * math.sin(a + 0.09))],
                      fill=(255, 220, 120, 40))
    return img


# ------------------------------------------------------------------------------------------- deadpan

def _corner_id(img, c, t, y=250):
    """Small and constant: who this is, top left (deadpan never makes him the star)."""
    k = M.clamp(t * 5)
    M.paste(img, c.art(120), M.SAFE_LEFT + 60, y, 1.0, k)
    M.put_text(img, c.p["name"].upper(), M.SAFE_LEFT + 140, y - 16, size=40, alpha=k, anchor="l")
    M.put_text(img, f"VS {c.d(c.p['opponent']).upper()} · {c.d(c.p['week_claim']).upper()}", M.SAFE_LEFT + 140, y + 26,
               weight="bold", size=28, fill=M.MUTED, alpha=k, anchor="l")


def _plane(size: int, color) -> Image.Image:
    lay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    s = size
    d.polygon([(0.5 * s, 0.05 * s), (0.58 * s, 0.4 * s), (0.95 * s, 0.6 * s), (0.95 * s, 0.68 * s), (0.58 * s, 0.58 * s),
               (0.56 * s, 0.82 * s), (0.68 * s, 0.92 * s), (0.68 * s, 0.97 * s), (0.5 * s, 0.92 * s),
               (0.32 * s, 0.97 * s), (0.32 * s, 0.92 * s), (0.44 * s, 0.82 * s), (0.42 * s, 0.58 * s),
               (0.05 * s, 0.68 * s), (0.05 * s, 0.6 * s), (0.42 * s, 0.4 * s)], fill=color + (255,))
    return lay


def scene_dp_toggle(img, t, dur, spec, c):
    _corner_id(img, c, t)
    w, h = 880, 190
    card = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(card)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=36, fill=(28, 28, 32, 255))
    d.rounded_rectangle((34, 45, 134, 145), radius=24, fill=TOGGLE_ON + (255,))
    card.alpha_composite(_plane(70, (255, 255, 255)), (49, 60))
    label = spec.get("label", "AIRPLANE MODE")
    d.text((170, h / 2), label, font=M.font("bold", 54), fill=M.INK, anchor="lm")
    on = M.ease_in_out(M.progress(t, c.at(word="airplane", default=dur * 0.5) - 0.05, 0.22))
    tx0, ty0 = w - 210, 52
    track = tuple(int(a + (b - a) * on) for a, b in zip((80, 80, 88), (52, 199, 89)))
    d.rounded_rectangle((tx0, ty0, tx0 + 170, ty0 + 86), radius=43, fill=track + (255,))
    kx = tx0 + 43 + on * 84
    d.ellipse((kx - 37, ty0 + 6, kx + 37, ty0 + 80), fill=(250, 250, 250, 255))
    M.put_text(img, "PASSING GAME SETTINGS", M.SAFE_LEFT, 560, weight="bold", size=36, fill=M.MUTED,
               alpha=M.clamp(t * 4), anchor="l")
    M.paste(img, card, M.CENTER_X, 720, 1.0, M.clamp(t * 4))
    rows = ["DEEP BALLS", "DOWNFIELD THROWS"]
    for i, r in enumerate(rows):                               # greyed-out options under airplane mode
        y = 930 + i * 120
        k = M.clamp((t - 0.3 - i * 0.1) * 4)
        M.put_text(img, r, M.SAFE_LEFT + 40, y, weight="bold", size=40, fill=(110, 110, 118), alpha=k, anchor="l")
        if on > 0.5:
            M.put_text(img, "UNAVAILABLE", M.SAFE_RIGHT - 250, y, weight="bold", size=32, fill=(110, 110, 118),
                       alpha=k * on, anchor="l")


def scene_dp_stat(img, t, dur, spec, c):
    _corner_id(img, c, t)
    big, small = spec["big"], spec.get("small")
    k = M.ease_out(M.progress(t, c.at(claim=big, default=0.2) - 0.05, 0.2))
    M.put_text(img, c.d(big), M.CENTER_X, 640, size=300, fill=M.INK, alpha=k)
    M.put_text(img, spec.get("unit", ""), M.CENTER_X, 820, weight="bold", size=44, fill=M.MUTED, alpha=k)
    if small:
        ks = M.ease_out(M.progress(t, c.at(claim=small, default=dur * 0.6) - 0.05, 0.25))
        M.put_text(img, f"{spec.get('small_label', '')}: {c.d(small)}", M.CENTER_X, 960, weight="bold", size=44,
                   fill=M.INK, alpha=ks)
        if spec.get("unit", "").startswith("YARDS"):            # the two distances as bars, same scale
            vb, vs = c.claims[big]["value"], c.claims[small]["value"]
            top = max(vb, vs)
            d = ImageDraw.Draw(img)
            for i, (v, col, kk) in enumerate(((vb, M.INK, k), (vs, (120, 120, 130), ks))):
                w = 760 * v / top * kk
                if w > 4:
                    d.rounded_rectangle((M.SAFE_LEFT + 60, 1060 + i * 70, M.SAFE_LEFT + 60 + w, 1100 + i * 70),
                                        radius=20, fill=col)


def scene_dp_line(img, t, dur, spec, c):
    _corner_id(img, c, t)
    lines = [fill(c, ln, spec.get("fill", {})) for ln in spec.get("lines", [])]
    start = 0.1
    for i, line in enumerate(lines):
        shown = _typed(line, t, start)
        M.put_text(img, shown or " ", M.SAFE_LEFT, 640 + i * 120, size=M.fit_size(line, "black", 84, 860),
                   fill=M.INK if i == 0 else (230, 230, 236), anchor="l")
        start += len(line) / 34 + 0.15
    if spec.get("foot"):
        foot = fill(c, spec["foot"], spec.get("fill", {}))
        M.put_text(img, foot, M.SAFE_LEFT, 900, weight="bold", size=34, fill=M.MUTED, alpha=M.clamp((t - start) * 3),
                   anchor="l")


def scene_dp_end(img, t, dur, spec, c):
    """Silence first (the setup is still being said), then the punchline types out and nothing else moves."""
    _corner_id(img, c, t)
    cue = c.at(word=spec.get("cue", "the"), default=dur * 0.4)
    parts = [i for i, tok in enumerate(c.tokens) if i and tok["text"].endswith(".")]
    if parts:                                                   # the punchline starts after the first sentence
        cue = c.tokens[parts[0] + 1]["start"] if parts[0] + 1 < len(c.tokens) else cue
    start = cue - 0.05
    for i, line in enumerate(spec.get("lines", [])):
        shown = _typed(line, t, start, cps=30)
        M.put_text(img, shown or " ", M.SAFE_LEFT, 640 + i * 130, size=M.fit_size(line, "black", 96, 860), anchor="l")
        start += len(line) / 30 + 0.1
    brand(img, t, dur, y=1180)


# --------------------------------------------------------------------------------------------- court

def _parody(img, t):
    """On every courtroom frame: this is a parody, not a real proceeding."""
    f = M.font("black", 40)
    w = int(f.getlength("PARODY")) + 48
    badge = Image.new("RGBA", (w, 70), (0, 0, 0, 0))
    ImageDraw.Draw(badge).rounded_rectangle((0, 0, w - 1, 69), radius=14, fill=COURT_RED + (255,))
    ImageDraw.Draw(badge).text((w / 2, 35), "PARODY", font=f, fill=(255, 255, 255), anchor="mm")
    M.paste(img, badge.rotate(6, expand=True, resample=Image.BICUBIC), M.SAFE_RIGHT - 110, 250, 1.0, 1.0)


def _paper(w, h, color=PAPER) -> Image.Image:
    lay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=10, fill=color + (255,))
    for y in range(160, h - 40, 56):
        d.line((40, y, w - 40, y), fill=(150, 160, 190, 60), width=2)
    return lay


def _gavel(size, angle) -> Image.Image:
    lay = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    cx, cy = size, size
    d.rounded_rectangle((cx - 12, cy - 10, cx + 12, cy + size * 0.9), radius=8, fill=(120, 70, 36, 255))
    d.rounded_rectangle((cx - size * 0.45, cy - 60, cx + size * 0.45, cy + 10), radius=14, fill=(92, 52, 26, 255))
    d.rectangle((cx - size * 0.32, cy - 60, cx - size * 0.26, cy + 10), fill=(220, 180, 90, 255))
    d.rectangle((cx + size * 0.26, cy - 60, cx + size * 0.32, cy + 10), fill=(220, 180, 90, 255))
    return lay.rotate(angle, resample=Image.BICUBIC, center=(cx, cy + size * 0.9))


def scene_court_docket(img, t, dur, spec, c):
    k = M.ease_out(M.progress(t, 0.0, 0.3))
    doc = _paper(860, 1020)
    d = ImageDraw.Draw(doc)
    d.text((430, 80), "IN THE COURT OF FOOTBALL OPINION", font=M.font("bold", 34), fill=PAPER_INK, anchor="mm")
    d.line((60, 120, 800, 120), fill=PAPER_INK, width=3)
    d.text((430, 250), "THE NFL", font=M.font("black", 96), fill=PAPER_INK, anchor="mm")
    d.text((430, 350), "v.", font=M.font("bold", 64), fill=PAPER_INK, anchor="mm")
    name = c.p["name"].upper()
    d.text((430, 460), name, font=M.font("black", M.fit_size(name, "black", 110, 760)), fill=COURT_RED, anchor="mm")
    case = f"CASE NO. {c.d(c.p['week_claim']).upper()} · VS {c.d(c.p['opponent']).upper()}"
    d.text((430, 580), case, font=M.font("bold", M.fit_size(case, "bold", 38, 760)), fill=PAPER_INK, anchor="mm")
    doc.alpha_composite(c.art(300), (280, 640))
    M.paste(img, doc.rotate(-1.5, expand=True, resample=Image.BICUBIC), M.CENTER_X, 770 + (1 - k) * 200, 1.0, k)
    _parody(img, t)
    kg = M.ease_in_out(M.progress(t, 0.0, 0.25))
    M.paste(img, _gavel(150, 40 - 40 * kg), M.SAFE_RIGHT - 120, 1240, 1.0, 1.0)


def scene_court_exhibit(img, t, dur, spec, c):
    i = spec.get("index", 0)
    for j in range(i):                                           # earlier exhibits, filed behind
        tab = _paper(260, 90, MANILA)
        ImageDraw.Draw(tab).text((130, 45), f"EXHIBIT {'ABC'[j]}", font=M.font("black", 36), fill=PAPER_INK,
                                 anchor="mm")
        M.paste(img, tab, M.SAFE_LEFT + 160 + j * 290, 300, 1.0, 0.9)
    k = M.back_out(M.progress(t, 0.0, 0.3))
    card = _paper(860, 760, MANILA)
    d = ImageDraw.Draw(card)
    d.text((430, 90), f"EXHIBIT {spec['letter']}", font=M.font("black", 72), fill=PAPER_INK, anchor="mm")
    d.line((60, 150, 800, 150), fill=PAPER_INK, width=3)
    big = c.d(spec["big"])
    kb = M.ease_out(M.progress(t, c.at(claim=spec["big"], default=0.4) - 0.1, 0.25))
    lay = M.text_layer(big, "black", M.fit_size(big, "black", 260, 760), PAPER_INK, shadow=False).copy()
    lay.putalpha(lay.getchannel("A").point(lambda v: int(v * kb)))
    card.alpha_composite(lay, (int(430 - lay.width / 2), int(380 - lay.height / 2)))
    label = spec["label"]
    d.text((430, 600), label, font=M.font("bold", M.fit_size(label, "bold", 48, 760)), fill=PAPER_INK, anchor="mm")
    M.paste(img, card.rotate((-1) ** i * 2, expand=True, resample=Image.BICUBIC), M.CENTER_X, 830, 0.85 + 0.15 * k,
            M.clamp(k * 2))
    ks = M.back_out(M.progress(t, c.at(claim=spec["big"], default=0.4) + 0.25, 0.25))
    stamp(img, "ENTERED INTO EVIDENCE", M.CENTER_X + 60, 1180, COURT_RED, ks, size=56, angle=-7)
    _parody(img, t)


def scene_court_verdict(img, t, dur, spec, c):
    doc = _paper(860, 900)
    d = ImageDraw.Draw(doc)
    d.text((430, 90), "VERDICT", font=M.font("black", 90), fill=PAPER_INK, anchor="mm")
    d.line((60, 160, 800, 160), fill=PAPER_INK, width=3)
    name = c.p["name"].upper()
    d.text((430, 240), f"THE NFL v. {name}", font=M.font("bold", M.fit_size(f'THE NFL v. {name}', 'bold', 46, 760)),
           fill=PAPER_INK, anchor="mm")
    M.paste(img, doc, M.CENTER_X, 800, 1.0, M.clamp(t * 5))
    cue = c.at(word="guilty", default=dur * 0.5)
    ks = M.back_out(M.progress(t, cue - 0.05, 0.25))
    stamp(img, spec.get("stamp", "GUILTY"), M.CENTER_X, 760, COURT_RED, ks, size=200, angle=-8)
    kc = M.clamp((t - cue - 0.3) * 3)
    for i, line in enumerate(_wrap(spec.get("charge", ""), 22)):
        M.put_text(img, line, M.CENTER_X, 980 + i * 70, size=M.fit_size(line, "black", 56, 760), fill=PAPER_INK,
                   alpha=kc)
    swing = M.progress(t, cue - 0.25, 0.2)
    ang = 40 - 55 * M.ease_in_out(swing) + (15 * M.ease_out(M.progress(t, cue - 0.05, 0.2)) if swing >= 1 else 0)
    M.paste(img, _gavel(170, ang), M.SAFE_RIGHT - 110, 1250, 1.0, 1.0)
    _parody(img, t)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return lines + ([cur] if cur else [])


# ------------------------------------------------------------------------------------------- versus

def scene_cmp_field(img, t, dur, spec, c):
    """A top-down field: the line of scrimmage low, yard lines every yard, each average throw landing in yards."""
    stage = spec.get("stage", "league")
    los, ppy = 1250, 52                                          # pixels per yard
    d = ImageDraw.Draw(img, "RGBA")
    for yd in range(0, 21):
        y = los - yd * ppy
        d.line((M.SAFE_LEFT, y, M.SAFE_RIGHT, y), fill=(255, 255, 255, 120 if yd % 5 == 0 else 45),
               width=4 if yd % 5 == 0 else 2)
    d.line((M.SAFE_LEFT, los, M.SAFE_RIGHT, los), fill=(70, 140, 255, 255), width=8)
    M.put_text(img, "LINE OF SCRIMMAGE", M.CENTER_X, los + 50, weight="bold", size=32, fill=M.INK)
    M.paste(img, c.art(150), M.SAFE_LEFT + 85, los - 85, 1.0, 1.0)      # the passer, at the line
    order = ["tease", "league", "him", "season", "safety"]
    at = order.index(stage)
    if stage == "tease":                                          # a tiny ruler for a tiny throw
        k = M.back_out(M.progress(t, c.at(word="smaller", default=dur * 0.6) - 0.1, 0.3))
        rx, ry = M.CENTER_X - 120, los - 260
        d.rounded_rectangle((rx, ry, rx + 240 * k + 1, ry + 60), radius=6, fill=(250, 214, 90, 255))
        for i in range(0, int(240 * k), 20):
            d.line((rx + i, ry, rx + i, ry + (30 if i % 60 == 0 else 16)), fill=(40, 30, 10, 255), width=3)
        M.put_text(img, f"MEASURING {c.p['name'].upper()}'S PASSES", M.CENTER_X, 300,
                   size=M.fit_size(f"MEASURING {c.p['name'].upper()}'S PASSES", "black", 54, 880), stroke=4)
        return
    throws = [("NFL AVERAGE", spec["league"], (255, 255, 255), 420)]
    if at >= 2:
        throws.append((_short(c.p["name"]).upper(), spec["x"], (255, 140, 40), 740))
    for label, cid, col, x in throws:
        yds = c.claims[cid]["value"]
        animate = stage == "him" or (stage == "league" and label == "NFL AVERAGE")
        start = c.at(claim=cid, default=0.2) - (0.6 if animate else 99)
        k = M.ease_in_out(M.progress(t, start, 0.6))
        y = los - yds * ppy * k
        for yy in range(int(y), los, 18):                        # the flight, dashed
            d.line((x, yy, x, min(los, yy + 9)), fill=col + (200,), width=5)
        M.paste(img, football(70).rotate(90, expand=True), x, y, 1.0, 1.0)
        if k >= 0.99:
            M.put_text(img, f"{label}: {c.d(cid)} YDS", x, y - 70, size=M.fit_size(f"{label}: {c.d(cid)} YDS", "black",
                                                                                   44, 420), fill=col, stroke=3)
    if stage == "season":
        ks = M.ease_out(M.progress(t, c.at(word="nobody", default=dur * 0.6) - 0.1, 0.3))
        M.put_text(img, "SHORTEST AVERAGE THROW IN THE NFL", M.CENTER_X, 300,
                   size=M.fit_size("SHORTEST AVERAGE THROW IN THE NFL", "black", 52, 860), fill=(255, 210, 90),
                   alpha=ks, stroke=4)
        sub = (f"THIS SEASON: {c.d(spec['season'])} YDS · MIN. {c.d(spec['min'])} ATTEMPTS" if spec.get("season")
               else "THIS SEASON")
        M.put_text(img, sub, M.CENTER_X, 365, weight="bold", size=M.fit_size(sub, "bold", 34, 860), alpha=ks)
    if stage == "safety":                                        # the deep safety, 20 yards off, waiting
        y = los - 19 * ppy
        k = M.ease_out(M.progress(t, c.at(word="safety", default=dur * 0.5) - 0.2, 0.3))
        d.ellipse((M.CENTER_X + 170 - 34, y - 34, M.CENTER_X + 170 + 34, y + 34), fill=(30, 30, 40, int(255 * k)))
        M.put_text(img, "S", M.CENTER_X + 170, y, size=44, fill=M.INK, alpha=k)
        M.put_text(img, "z z z", M.CENTER_X + 280, y - 50 - 10 * math.sin(t * 3), weight="bold", size=48, alpha=k)
        M.put_text(img, "THE DEEP SAFETY", M.CENTER_X + 170, y + 70, weight="bold", size=34, alpha=k)
    title = {"league": "HOW FAR THE AVERAGE NFL PASS GOES", "him": f"{c.p['name'].upper()} VS THE "
             f"{c.d(c.p['opponent']).upper()}", "season": "", "safety": ""}[stage]
    if title:
        M.put_text(img, title, M.CENTER_X, 300, size=M.fit_size(title, "black", 54, 880), stroke=4)


def scene_cmp_tally(img, t, dur, spec, c):
    """His targets as footballs against a whole offense's."""
    stage = spec.get("stage", "him")
    him, them = spec["him"], spec["them"]
    cols = [(_short(c.p["name"]).upper(), him, (255, 205, 64), M.SAFE_LEFT + 20)]
    if stage in ("them", "end"):
        cols.append((f"ENTIRE {c.d(c.p['opponent']).upper()} OFFENSE", them, (230, 236, 244), M.CENTER_X + 30))
    for label, cid, col, x0 in cols:
        n = int(c.claims[cid]["value"])
        start = c.at(claim=cid, default=0.2) - 0.3 if (stage == "him") == (cid == him) else -5
        M.put_text(img, label, x0, 330, size=M.fit_size(label, "black", 50, 420), fill=col, anchor="l", stroke=3)
        for i in range(n):
            r, k_ = divmod(i, 4)
            k = M.back_out(M.progress(t, start + i * 0.035, 0.18))
            if k > 0:
                M.paste(img, football(84), x0 + 60 + k_ * 100, 430 + r * 64, 0.5 + 0.5 * k, M.clamp(k * 2))
        if t > start + n * 0.035:
            M.put_text(img, c.d(cid), x0 + 200, 1050 if n <= 24 else 1200, size=150, fill=col, stroke=6)
    if stage == "end" and c.claims[him]["value"] > c.claims[them]["value"]:
        ks = M.back_out(M.progress(t, 0.3, 0.3))
        stamp(img, "ONE RECEIVER > ONE OFFENSE", M.CENTER_X, 1230, (255, 205, 64), ks, size=64, angle=-4)


# --------------------------------------------------------------------------------------------- cinematic

def _title(img, text, y, k, size=64, col=M.INK):
    M.put_text(img, text, M.CENTER_X, y + (1 - k) * 20, weight="bold", size=M.fit_size(text, "bold", size, 880),
               fill=col, alpha=k)


def scene_cine_title(img, t, dur, spec, c):
    M.paste(img, c.art(760), M.CENTER_X, 860, 1.0 + 0.04 * t / max(dur, 1), 0.18)
    k1 = M.ease_out(M.progress(t, 0.0, 0.5))
    _title(img, c.d(c.p["week_claim"]).upper(), 520, k1, 60, M.MUTED)
    k2 = M.ease_out(M.progress(t, c.at(claim=c.p["opponent"], default=0.8) - 0.3, 0.5))
    _title(img, f"{c.d(c.p['team_name']).upper()}  ·  {c.d(c.p['opponent']).upper()}", 610, k2, 70)
    k3 = M.ease_out(M.progress(t, c.at(word="one", default=dur * 0.55) - 0.1, 0.6))
    _title(img, "ONE MAN DECIDED", 1080, k3, 76, (255, 214, 120))
    _title(img, "EVERYONE ELSE WAS OPTIONAL", 1170, k3 * M.clamp((t - c.at(word="optional", default=dur) + 0.4) * 3),
           76, (255, 214, 120))


def scene_cine_slam(img, t, dur, spec, c):
    slams, words = spec["slams"], spec["words"]
    times = [c.at(claim=cid, default=0.3 + i * 0.8) for i, cid in enumerate(slams)]
    M.paste(img, c.art(520), M.SAFE_RIGHT - 170, 1180, 1.0, 0.85)
    for i, (cid, word, at) in enumerate(zip(slams, words, times)):
        k = M.back_out(M.progress(t, at - 0.06, 0.22))
        if k <= 0:
            continue
        current = i == max(j for j, tt in enumerate(times) if t >= tt - 0.06)
        y = 360 + i * 190
        M.put_text(img, c.d(cid), M.SAFE_LEFT, y, size=170 if current else 120, fill=M.INK if current else M.MUTED,
                   scale=0.6 + 0.4 * k, alpha=M.clamp(k * 2), anchor="l", stroke=4)
        M.put_text(img, word, M.SAFE_LEFT + (330 if current else 250), y + 20, weight="bold", size=60 if current else 46,
                   fill=(255, 214, 120) if current else M.MUTED, alpha=M.clamp(k * 2), anchor="l")
    flash = max((math.exp(-(t - at) / 0.05) for at in times if t >= at - 0.02), default=0)
    if flash > 0.02:
        overlay = Image.new("RGB", img.size, (255, 255, 255))
        img.paste(Image.blend(img, overlay, 0.35 * flash))


def scene_cine_contrast(img, t, dur, spec, c):
    them = spec.get("them")
    if them:
        k = M.ease_out(M.progress(t, c.at(claim=them, default=0.5) - 0.2, 0.4))
        M.put_text(img, f"THE ENTIRE {c.d(c.p['opponent']).upper()} OFFENSE", M.CENTER_X, 420, weight="bold", size=50,
                   alpha=M.clamp(t * 5))
        M.put_text(img, c.d(them), M.CENTER_X, 600, size=220, fill=M.MUTED, alpha=k)
        M.put_text(img, "TARGETS", M.CENTER_X, 740, weight="bold", size=48, fill=M.MUTED, alpha=k)
    k2 = M.ease_out(M.progress(t, c.at(word="no", default=c.at(word="that", default=dur * 0.55)) - 0.1, 0.4))
    pos = c.p["position"]
    lines = ([f"MOST TARGETS IN ANY OF", f"{c.d(spec['games'])} {pos} GAMES THIS SEASON"] if spec.get("rank1")
             else [f"{c.d(spec['rank']).upper()}-MOST OF {c.d(spec['games'])}", f"{pos} GAMES THIS SEASON"])
    for i, line in enumerate(lines):
        M.put_text(img, line, M.CENTER_X, 960 + i * 90, size=M.fit_size(line, "black", 70, 880), fill=(255, 214, 120),
                   alpha=k2, stroke=3)


def scene_cine_poster(img, t, dur, spec, c):
    k = M.ease_out(M.progress(t, 0.0, 0.8))
    M.paste(img, c.art(620), M.CENTER_X, 640, 1.08 - 0.08 * k, k)
    name = c.p["name"].upper()
    M.put_text(img, name, M.CENTER_X, 1060, size=M.fit_size(name, "black", 130, 900), alpha=k, stroke=4)
    kt = M.ease_out(M.progress(t, c.at(word=_short(c.p["name"]).lower(), default=dur * 0.6) - 0.1, 0.5))
    M.put_text(img, spec.get("tagline", ""), M.CENTER_X, 1170, weight="bold",
               size=M.fit_size(spec.get("tagline", ""), "bold", 50, 880), fill=(255, 214, 120), alpha=kt)
    credits = "  ·  ".join(f"{c.d(cid)} {ab}" for cid, ab in spec.get("credits", []))
    M.put_text(img, credits, M.CENTER_X, 1260, weight="bold", size=M.fit_size(credits, "bold", 40, 860), fill=M.MUTED,
               alpha=kt)
    M.put_text(img, "A FOOTBALL STORY", M.CENTER_X, 260, weight="bold", size=34, fill=M.MUTED, alpha=k)


# -------------------------------------------------------------------------------------------------- meme

def _shake(t, amp):
    return amp * math.sin(t * 61), amp * math.cos(t * 47)


def scene_melt_calm(img, t, dur, spec, c):
    M.paste(img, c.art(360), M.CENTER_X, 1060, 1.0, M.clamp(t * 4))
    for i, line in enumerate(["OKAY.", "EVERYBODY RELAX."]):
        k = M.clamp((t - i * 0.5) * 4)
        M.put_text(img, line, M.CENTER_X, 520 + i * 110, size=72 if i else 96, fill=(255, 255, 255), alpha=k, stroke=5)
    kc = M.clamp((t - c.at(word="calmly", default=dur * 0.7)) * 4)
    M.put_text(img, "(CALMLY)", M.CENTER_X, 780, weight="bold", size=48, fill=(30, 40, 60), alpha=kc)


def scene_melt_stat(img, t, dur, spec, c):
    lvl = spec.get("level", 1)
    dx, dy = _shake(t, 3 * lvl)
    k = M.back_out(M.progress(t, c.at(claim=spec["big"], default=0.05) - 0.08, 0.2))
    size = 230 + 50 * lvl
    M.put_text(img, c.d(spec["big"]), M.CENTER_X + dx, 720 + dy, size=size, fill=(255, 255, 255), scale=0.5 + 0.5 * k,
               alpha=M.clamp(k * 2), stroke=10)
    word = spec.get("word", "")
    M.put_text(img, word, M.CENTER_X - dx, 950 - dy, size=M.fit_size(word, "black", 70 + 10 * lvl, 900),
               fill=(255, 245, 160) if lvl >= 3 else (255, 255, 255), alpha=M.clamp(k * 2), stroke=6)
    M.paste(img, c.art(260), M.SAFE_LEFT + 120 + dx, 1240 + dy, 1.0, 0.9)


def scene_melt_scream(img, t, dur, spec, c):
    dx, dy = _shake(t, 18)
    k = M.back_out(M.progress(t, 0.0, 0.18))
    for i, line in enumerate(["WE ARE", "NOT CALM"]):
        M.put_text(img, line, M.CENTER_X + dx * (1 if i else -1), 600 + i * 230 + dy,
                   size=M.fit_size("NOT CALM", "black", 230, 860), fill=(255, 245, 160),
                   scale=0.7 + 0.3 * k, stroke=12)
    kn = M.clamp((t - c.at(word="never", default=dur * 0.5)) * 4)
    M.put_text(img, "WE WILL NEVER BE CALM AGAIN", M.CENTER_X, 1120, size=60, fill=(255, 255, 255), alpha=kn, stroke=5)
    M.paste(img, c.art(300), M.CENTER_X + dx, 1300 + dy, 1.0, 1.0)


SCENES = {"dp_toggle": scene_dp_toggle, "dp_stat": scene_dp_stat, "dp_line": scene_dp_line, "dp_end": scene_dp_end,
          "court_docket": scene_court_docket, "court_exhibit": scene_court_exhibit,
          "court_verdict": scene_court_verdict, "cmp_field": scene_cmp_field, "cmp_tally": scene_cmp_tally,
          "cine_title": scene_cine_title, "cine_slam": scene_cine_slam, "cine_contrast": scene_cine_contrast,
          "cine_poster": scene_cine_poster, "melt_calm": scene_melt_calm, "melt_stat": scene_melt_stat,
          "melt_scream": scene_melt_scream}


def punches(scene: dict, c, dur: float) -> list[float]:
    kind = scene["type"]
    if kind == "court_verdict":
        return [c.at(word="guilty", default=dur * 0.5)]
    if kind == "cine_slam":
        return [c.at(claim=cid, default=99) for cid in scene["slams"]]
    if kind == "melt_stat":
        return [c.at(claim=scene["big"], default=0.05)]
    if kind == "melt_scream":
        return [0.0]
    if kind == "cine_poster":
        return [0.05]
    return []                                                   # deadpan, court paper, the field: a still camera


def sounds(scene: dict, start: float, dur: float, c) -> list[tuple[float, str, float]]:
    kind, ev = scene["type"], []
    cue = scene.get("sfx_cue")
    if cue and kind not in ("court_verdict", "dp_end"):
        ev.append((start, cue, -4 if cue != "boom" else -2))
    if kind == "dp_toggle":
        ev.append((start + c.at(word="airplane", default=dur * 0.5), "tick", -6))
    if kind in ("dp_line", "dp_end"):
        for i in range(6):
            ev.append((start + 0.12 + i * 0.09 + (c.at(word="the", default=0) if kind == "dp_end" else 0), "type", -12))
    if kind == "dp_end":
        ev.append((start - 0.05, "scratch", -6))
    if kind == "court_exhibit":
        ev.append((start + c.at(claim=scene["big"], default=0.4) + 0.25, "stamp", -4))
    if kind == "court_verdict":
        at = start + c.at(word="guilty", default=dur * 0.5)
        ev += [(at - 0.05, "gavel", -1), (at + 0.05, "stamp", -5)]
    if kind == "cmp_field":
        for cid in (scene["league"], scene["x"]):
            ev.append((start + c.at(claim=cid, default=99) - 0.6, "whoosh", -10))
    if kind == "cmp_tally":
        ev.append((start + 0.2, "tick", -10))
        if scene.get("stage") == "end":
            ev.append((start + 0.3, "stamp", -4))
    if kind == "cine_slam":
        for cid in scene["slams"]:
            ev.append((start + c.at(claim=cid, default=99) - 0.04, "boom", -4))
    if kind == "cine_contrast":
        ev.append((start + 0.05, "whoosh", -10))
    if kind == "melt_stat":
        lvl = scene.get("level", 1)
        ev.append((start + c.at(claim=scene["big"], default=0.05) - 0.04, "impact", -8 + 2 * lvl))
    if kind == "melt_scream":
        ev.append((start, "swell", -2))
        ev.append((start, "boom", -1))
    return [e for e in ev if e[0] < start + dur + 0.5]
