"""Scenes for the flame / praise Shorts (stages/flame.py). One game, shown against everything it is measured by.

  flame  (ember look: charcoal, a red-orange glow from below, embers drifting up)
  praise (spotlight look: deep navy, gold beams sweeping from above, a few sparks)

    fp_hook     the stat line on frame 0: the number huge, what it measures, on what volume, against whom
    fp_strip    every qualifying game at his position this season as a dot, stacked; his dot found at the end
    fp_own      his own season game by game, this game lit, his earlier average as a dashed line
    fp_receipt  the rest of the stat line as a receipt (flame) or a stat sheet (praise), stamped

The ending is the shared `decision` scene. Every number drawn is a claim's display string (c.d); the strip's dots
are drawn without numbers.
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import motion as M
from . import edit_scenes as ES
from . import parody_motion as PM
from . import series_scenes as SS
from .format_scenes import PAPER, PAPER_INK, brand, stamp
from .stages import flame as FL

FLAME = (255, 104, 38)
EMBER = (232, 54, 36)
PRAISE = M.GOLD
ACCENT = {"flame": FLAME, "praise": PRAISE}
BACKDROPS = ("ember", "spotlight")
MOOD = {"flame": "tense", "praise": "bright"}


# ------------------------------------------------------------------------------------------ backdrops

@lru_cache(maxsize=2)
def ember_backdrop() -> Image.Image:
    yy, xx = np.mgrid[0:M.H, 0:M.W].astype(np.float32)
    r = np.sqrt(((xx - M.W * 0.5) / M.W) ** 2 + ((yy - M.H * 1.05) / M.H) ** 2)
    glow = np.clip(1 - r * 1.25, 0, 1) ** 1.8
    base = np.array((14, 10, 10), np.float32)[None, None] + glow[..., None] * np.array((210, 60, 22), np.float32)
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")


@lru_cache(maxsize=2)
def spotlight_backdrop() -> Image.Image:
    yy, xx = np.mgrid[0:M.H, 0:M.W].astype(np.float32)
    r = np.sqrt(((xx - M.W * 0.5) / M.W) ** 2 + ((yy + M.H * 0.05) / M.H) ** 2)
    glow = np.clip(1 - r * 1.1, 0, 1) ** 2
    base = np.array((8, 12, 26), np.float32)[None, None] + glow[..., None] * np.array((120, 96, 30), np.float32)
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")


def _particles(n: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).random((n, 4))


EMBERS = _particles(46, 11)
SPARKS = _particles(26, 5)


def background(kind: str, c, t: float) -> Image.Image:
    if kind == "none":                                      # the scene paints the whole frame itself
        return Image.new("RGB", (M.W, M.H))
    style, _, level = kind.partition(":")
    if style in SS.STYLES:                                  # the series formats' own looks (series_scenes.py)
        return SS.background(style, c, t, {"level": int(level)} if level else None)
    if kind == "ember":
        img = ember_backdrop().copy()
        d = ImageDraw.Draw(img, "RGBA")
        for x0, phase, speed, size in EMBERS:          # embers rise and sway, then loop
            y = M.H - ((t * (60 + 90 * speed) + phase * M.H) % (M.H + 80))
            x = x0 * M.W + 30 * math.sin(t * (0.8 + speed) + phase * 6)
            r = 2 + 4 * size
            a = int(90 + 140 * (y / M.H))
            d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 140 + int(60 * size), 50, max(0, min(255, a))))
        return img
    img = spotlight_backdrop().copy()
    lay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    for i, base_angle in enumerate((-0.42, 0.0, 0.42)):  # three beams sweeping slowly from above
        a = base_angle + 0.12 * math.sin(t * 0.7 + i * 2.1)
        top = (M.W / 2 + (i - 1) * 260, -40)
        far = 2600
        spread = 0.11
        pts = [top, (top[0] + far * math.sin(a - spread), far * math.cos(a - spread)),
               (top[0] + far * math.sin(a + spread), far * math.cos(a + spread))]
        d.polygon(pts, fill=(255, 220, 120, 22))
    img = Image.alpha_composite(img.convert("RGBA"), lay.filter(ImageFilter.GaussianBlur(18))).convert("RGB")
    d = ImageDraw.Draw(img, "RGBA")
    for x0, y0, phase, size in SPARKS:                    # sparks twinkle in place
        tw = 0.5 + 0.5 * math.sin(t * 3 + phase * 9)
        x, y, r = x0 * M.W, 200 + y0 * 1300, 1 + 3 * size * tw
        d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 236, 170, int(200 * tw)))
    return img


# -------------------------------------------------------------------------------------------- icons

def flame_icon(size: int, color=FLAME) -> Image.Image:
    """A drawn flame: an outer tongue and a bright core (no emoji fonts needed)."""
    lay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)

    def tongue(scale, fill):
        cx, by, w, h = size / 2, size * 0.96, size * 0.42 * scale, size * 0.9 * scale
        pts = []
        for i in range(41):
            u = i / 40
            ang = math.pi * u
            x = cx - w * math.cos(ang) * (1 - 0.35 * u)
            y = by - (h * 0.45) * math.sin(ang)
            pts.append((x, y))
        pts += [(cx + w * 0.55, by - h * 0.55), (cx + w * 0.15, by - h * 0.7), (cx + w * 0.05, by - h)]
        pts += [(cx - w * 0.25, by - h * 0.62), (cx - w * 0.6, by - h * 0.5)]
        d.polygon(pts, fill=fill)
    tongue(1.0, color + (255,))
    tongue(0.62, (255, 200, 70, 255))
    tongue(0.32, (255, 248, 210, 255))
    return lay


def crown_icon(size: int, color=PRAISE) -> Image.Image:
    lay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    s = size
    pts = [(0.08 * s, 0.82 * s), (0.08 * s, 0.32 * s), (0.3 * s, 0.56 * s), (0.5 * s, 0.18 * s),
           (0.7 * s, 0.56 * s), (0.92 * s, 0.32 * s), (0.92 * s, 0.82 * s)]
    d.polygon(pts, fill=color + (255,))
    for x, y in ((0.08, 0.3), (0.5, 0.16), (0.92, 0.3)):
        d.ellipse(((x - 0.06) * s, (y - 0.06) * s, (x + 0.06) * s, (y + 0.06) * s), fill=(255, 245, 210, 255))
    d.rectangle((0.08 * s, 0.84 * s, 0.92 * s, 0.94 * s), fill=color + (255,))
    return lay


def _flame(c) -> bool:
    return c.p["role"] == "flame"


def _accent(c):
    return ACCENT[c.p["role"]]


def _short_week(text: str) -> str:
    return text.upper().replace("WEEK ", "WK ")


# ------------------------------------------------------------------------------------------- scenes

def scene_fp_hook(img, t, dur, spec, c):
    """Frame 0 already reads: the badge, the number, what it measures and on what volume."""
    p, lead = c.p, c.p["lead"]
    col = _accent(c)
    tt = t + 0.25                                          # already landing on frame 0
    kb = M.back_out(M.progress(tt, 0.0, 0.3))
    icon = flame_icon(130) if _flame(c) else crown_icon(120)
    flick = 1 + (0.04 * math.sin(t * 14) if _flame(c) else 0.02 * math.sin(t * 4))
    M.paste(img, icon, M.CENTER_X - 210, 300, (0.6 + 0.4 * kb) * flick, M.clamp(kb * 2))
    M.put_text(img, spec.get("stamp", ""), M.CENTER_X + 60, 300, size=96, fill=col, scale=0.7 + 0.3 * kb,
               alpha=M.clamp(kb * 2), stroke=4)
    kn = M.back_out(M.progress(tt, 0.1, 0.35))
    pulse = 1 + 0.025 * math.sin(t * 5)
    value = c.d(lead["value"])
    M.put_text(img, value, M.CENTER_X, 590, size=M.fit_size(value, "black", 330, 860), fill=col,
               scale=(0.5 + 0.5 * kn) * pulse, alpha=M.clamp(kn * 2), stroke=6)
    M.put_text(img, lead["screen"], M.CENTER_X, 790, size=M.fit_size(lead["screen"], "black", 70, 860),
               fill=M.INK, alpha=M.clamp(kn * 2))
    name = p["name"].upper()
    M.put_text(img, name, M.CENTER_X, 880, size=M.fit_size(name, "black", 60, 860), fill=col,
               alpha=M.clamp(kn * 2), stroke=3)
    vol = p["volume"]
    bits = [f"ON {c.d(vol['value'])} {vol['screen']}"]
    if p.get("snaps"):
        bits.append(f"{c.d(p['snaps'])} OF SNAPS")
    bits.append(f"VS {c.d(p['opponent']).upper()}")
    bits.append(_short_week(c.d(p["week_claim"])))
    sub = " · ".join(bits)
    M.put_text(img, sub, M.CENTER_X, 955, size=M.fit_size(sub, "bold", 44, 880), weight="bold", fill=M.MUTED,
               alpha=M.clamp((tt - 0.1) * 4))
    ka = M.back_out(M.progress(tt, 0.2, 0.45))
    M.paste(img, c.art(320), M.CENTER_X, 1150 + (1 - ka) * 200 + M.wobble(t, 3), 1.0, M.clamp(ka * 2))


def _bins(values: list[float], n_bins: int = 28):
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    idx = [min(n_bins - 1, int((v - lo) / span * n_bins)) for v in values]
    return lo, span, idx


def scene_fp_strip(img, t, dur, spec, c):
    """Every qualifying game at his position this season, one dot each, stacked by value; his found at the end."""
    p, lead = c.p, c.p["lead"]
    col = _accent(c)
    flame = _flame(c)
    pos = {"QB": "QB", "WR": "WR", "TE": "TE", "RB": "RB"}[p["position"]]
    title = f"{c.d(lead['games'])} {pos} GAMES THIS SEASON"
    M.put_text(img, title, M.CENTER_X, 320, size=M.fit_size(title, "black", 70, 880), alpha=M.clamp(t * 6))
    qual = c.d(p["qualifier"])
    unit = {"QB": "ATTEMPTS", "RB": "OF SNAPS"}.get(p["position"], "OF SNAPS")
    sub = f"EACH DOT IS ONE GAME · {qual}+ {unit}"
    M.put_text(img, sub, M.CENTER_X, 395, size=M.fit_size(sub, "bold", 36, 880), weight="bold", fill=M.MUTED,
               alpha=M.clamp(t * 5))
    values = lead["dist"]
    mine = c.claims[lead["value"]]["value"]
    lo, span, idx = _bins(values + [mine])
    my_bin = idx[-1]
    idx = idx[:-1]
    x0, x1, base = M.SAFE_LEFT + 30, M.SAFE_RIGHT - 30, 1010
    n_bins = 28
    step = (x1 - x0) / n_bins
    tallest = max(idx.count(b) for b in set(idx))
    dy = min(step * 0.84 + 3, (base - 480) / max(tallest, 1))     # stacks always fit under the title
    r = min(11, step * 0.42, (dy - 2) / 2)
    heights: dict[int, int] = {}
    d = ImageDraw.Draw(img, "RGBA")
    reveal = c.at(claim=lead["rank"], default=dur * 0.45)
    found = M.ease_out(M.progress(t, reveal - 0.15, 0.35))
    # the extreme end of the strip, where his game sits, warms up as it is found
    edge_x = x0 + (my_bin + (1 if flame else 0)) * step
    zone = (x0 - 20, 460, edge_x, base + 20) if flame else (edge_x, 460, x1 + 20, base + 20)
    d.rounded_rectangle(zone, radius=20, fill=col + (int(46 * found),))
    my_slot = None
    for v, b in sorted(zip(values, idx), key=lambda z: z[0]):
        h = heights.get(b, 0)
        heights[b] = h + 1
        is_me = my_slot is None and b == my_bin and abs(v - mine) < 1e-9
        if is_me:
            my_slot = (b, h)
        delay = 0.05 + (b / n_bins) * 0.6 + h * 0.012
        k = M.ease_out(M.progress(t, delay, 0.25))
        if k <= 0:
            continue
        cx = x0 + (b + 0.5) * step
        cy = base - h * dy - r - (1 - k) * 120
        fill = (col if is_me and found > 0 else (150, 158, 172)) + (int(255 * k),)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)
    d.line((x0, base + 4, x1, base + 4), fill=(255, 255, 255, 90), width=3)
    M.put_text(img, "← LESS", x0, base + 50, size=34, weight="bold", fill=M.MUTED, anchor="l", alpha=M.clamp(t * 4))
    M.put_text(img, "MORE →", x1 - 150, base + 50, size=34, weight="bold", fill=M.MUTED, anchor="l",
               alpha=M.clamp(t * 4))
    if my_slot and found > 0:
        b, h = my_slot
        cx = x0 + (b + 0.5) * step
        cy = base - h * dy - r
        ring = r + 10 + 26 * (1 - found)
        d.ellipse((cx - ring, cy - ring, cx + ring, cy + ring), outline=col + (255,), width=6)
        tip_y = 560
        d.line((cx, cy - ring, cx, tip_y + 70), fill=col + (int(255 * found),), width=5)
        tag = f"{p['name'].split()[-1].upper()}: {c.d(lead['value'])}"
        tx = min(max(cx, M.SAFE_LEFT + 230), M.SAFE_RIGHT - 230)
        M.put_text(img, tag, tx, tip_y + 30, size=M.fit_size(tag, "black", 60, 460), fill=col,
                   scale=0.7 + 0.3 * found, alpha=found, stroke=3)
    rank = lead["rank_value"]
    word = ("TIED FOR THE " if lead["tied"] else "THE ") + lead["sup"].upper() if rank == 1 \
        else f"{c.d(lead['rank']).upper()}-{lead['sup'].upper()}"
    big = f"{word} OF {c.d(lead['games'])}"
    kc = M.back_out(M.progress(t, reveal + 0.1, 0.3))
    M.put_text(img, big, M.CENTER_X, 1200, size=M.fit_size(big, "black", 92, 880), fill=col,
               scale=0.6 + 0.4 * kc, alpha=M.clamp(kc * 2), stroke=4)
    if FL.week_best(lead):
        kw = M.ease_out(M.progress(t, c.at(word="anyone", default=dur * 0.75) - 0.1, 0.3))
        wk = f"AND THE {lead['sup'].upper()} OF ANY {pos} IN {c.d(p['week_claim']).upper()}"
        M.put_text(img, wk, M.CENTER_X, 1310, size=M.fit_size(wk, "bold", 44, 880), weight="bold", fill=M.INK,
                   alpha=kw)


def scene_fp_own(img, t, dur, spec, c):
    """His season game by game: earlier games grey, this one lit, his earlier average dashed across."""
    lead = c.p["lead"]
    col = _accent(c)
    M.put_text(img, lead["screen"], M.CENTER_X, 320, size=M.fit_size(lead["screen"], "black", 84, 880),
               alpha=M.clamp(t * 6))
    M.put_text(img, "HIS SEASON, GAME BY GAME", M.CENTER_X, 405, size=42, weight="bold", fill=M.MUTED,
               alpha=M.clamp(t * 6))
    series = lead["series"]
    vals = [c.claims[s["value"]]["value"] for s in series]
    own = c.claims[lead["own"]]["value"]
    top, bot = max(vals + [own, 0]), min(vals + [own, 0])
    span = (top - bot) or 1.0
    x0, x1, y_top, y_bot = M.SAFE_LEFT + 20, M.SAFE_RIGHT - 20, 660, 1180
    zero = y_top + (top - 0) / span * (y_bot - y_top)
    n = len(series)
    gap = 26
    bw = (x1 - x0 - gap * (n - 1)) / n
    d = ImageDraw.Draw(img, "RGBA")
    for i, (s, v) in enumerate(zip(series, vals)):
        last = i == n - 1
        at = c.at(claim=s["value"], default=0.1 + i * 0.12) if last else 0.08 + i * 0.1
        grow = M.ease_out(M.progress(t, at - 0.1 if last else at, 0.3))
        if grow <= 0:
            continue
        y = zero - (v / span) * (y_bot - y_top) * grow
        x = x0 + i * (bw + gap)
        fill = col if last else (70, 78, 92)
        box = (x, min(y, zero), x + bw, max(y, zero))
        if box[3] - box[1] > 2:
            d.rounded_rectangle(box, radius=12, fill=fill)
        label = c.d(s["value"])
        ly = (y - 44) if v >= 0 else (y + 44)
        M.put_text(img, label, x + bw / 2, ly, size=(64 if last else 42), fill=col if last else M.INK,
                   alpha=M.clamp(grow * 1.5), scale=M.back_out(grow) if last else 1.0)
        M.put_text(img, _short_week(c.d(s["week_claim"])), x + bw / 2, y_bot + 60, size=32, weight="bold",
                   fill=M.MUTED, alpha=M.clamp(grow * 2))
    d.line((x0, zero, x1, zero), fill=(255, 255, 255, 60), width=2)
    ko = M.ease_out(M.progress(t, c.at(claim=lead["own"], default=dur * 0.3) - 0.15, 0.4))
    yo = zero - (own / span) * (y_bot - y_top)
    for xx in range(int(x0), int(x0 + (x1 - x0) * ko), 34):
        d.line((xx, yo, xx + 18, yo), fill=M.INK + (230,), width=4)
    chip = f"BEFORE: {c.d(lead['own'])}" + (" A GAME" if lead["per_game"] else "")
    M.put_text(img, chip, M.CENTER_X, 1320, size=M.fit_size(chip, "black", 56, 880), fill=M.INK, alpha=ko)
    if lead["season_extreme"]:
        kx = M.back_out(M.progress(t, c.at(word="new", default=dur * 0.8) - 0.1, 0.3))
        stamp(img, "SEASON LOW" if _flame(c) else "SEASON HIGH", M.CENTER_X, 520, col, kx, size=64, angle=-4)


def scene_fp_receipt(img, t, dur, spec, c):
    """The rest of the line, itemised: a till receipt for a flame, a stat sheet for a praise; then the stamp."""
    p = c.p
    lead = p["lead"]
    flame = _flame(c)
    col = _accent(c)
    w, h = 800, 980
    paper = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pd = ImageDraw.Draw(paper)
    body = PAPER if flame else (20, 24, 36)
    ink = PAPER_INK if flame else M.INK
    teeth = 24
    poly = [(0, 0), (w, 0), (w, h - teeth)]
    for i in range(int(w / teeth), -1, -1):                       # a torn, zig-zag bottom edge
        poly.append((i * teeth, h - (teeth if i % 2 else 0)))
    pd.polygon(poly, fill=body + (255,))
    if not flame:
        pd.rectangle((0, 0, w - 1, h - teeth - 1), outline=PRAISE + (255,), width=6)
    f_head, f_line, f_small = M.font("black", 54), M.font("bold", 44), M.font("bold", 30)
    pd.text((w / 2, 70), "STAT RECEIPT" if flame else "STAT SHEET", font=f_head, fill=ink, anchor="mm")
    sub = f"{p['name'].upper()} · VS {c.d(p['opponent']).upper()} · {_short_week(c.d(p['week_claim']))}"
    pd.text((w / 2, 130), sub, font=M.font("bold", M.fit_size(sub, "bold", 30, w - 60)), fill=ink, anchor="mm")
    pd.line((40, 175, w - 40, 175), fill=ink + (140,), width=3)
    same = c.claims[p["volume"]["value"]]["source"]["metric"] == lead["metric"]     # 24 targets, said once
    rows = [] if same else [(p["volume"]["screen"], p["volume"]["value"], None)]
    if p.get("snaps"):
        rows.append(("SNAP SHARE", p["snaps"], None))
    rows.append((lead["screen"], lead["value"], lead))
    rows += [(r["screen"], r["value"], r) for r in p["receipts"]]
    y = 250
    for label, cid, case in rows:
        at = c.at(claim=cid, default=0.15 if case is None or case is lead else dur * 0.5)
        if case is not None and case is not lead and t < at - 0.12:
            y += 120
            continue
        val = c.d(cid)
        lab = M.font("bold", M.fit_size(label, "bold", 40, w - 330))
        pd.text((40, y), label, font=lab, fill=ink, anchor="lm")
        pd.text((w - 40, y), val, font=f_line, fill=ink, anchor="rm")
        lw = lab.getlength(label)
        vw = f_line.getlength(val)
        for xx in range(int(60 + lw), int(w - 60 - vw), 18):        # leader dots
            pd.ellipse((xx, y + 8, xx + 5, y + 13), fill=ink + (150,))
        if case is not None:
            r_txt = ("THE " + case["sup"].upper()) if case["rank_value"] == 1 else \
                f"{c.d(case['rank']).upper()}-{case['sup'].upper()}"
            pd.text((w - 40, y + 48), f"{r_txt} OF {p['position']} GAMES", font=f_small,
                    fill=(EMBER if flame else PRAISE) + (255,), anchor="rm")
        y += 120
    pd.line((40, y - 30, w - 40, y - 30), fill=ink + (140,), width=3)
    pd.text((w / 2, h - 90), "* ALL FIGURES FROM THIS ONE GAME *", font=f_small, fill=ink + (170,), anchor="mm")
    k = M.ease_out(M.progress(t, 0.0, 0.35))
    paper = paper.rotate(-2.5 if flame else 0, expand=True, resample=Image.BICUBIC)
    M.paste(img, paper, M.CENTER_X, 860 - (1 - k) * 300, 1.0, M.clamp(k * 2))
    ks = M.back_out(M.progress(t, dur * 0.72, 0.3))
    stamp(img, "ROASTED" if flame else "CERTIFIED", M.CENTER_X + 120, 1180, EMBER if flame else PRAISE, ks,
          size=120, angle=-12)
    brand(img, t, dur, y=1390)


SCENES = {"fp_hook": (None, scene_fp_hook), "fp_strip": (None, scene_fp_strip), "fp_own": (None, scene_fp_own),
          "fp_receipt": (None, scene_fp_receipt)}
SCENES.update({k: (None, fn) for k, fn in SS.SCENES.items()})
SCENES.update({k: (None, fn) for k, fn in ES.SCENES.items()})   # edit-style scenes draw their own backdrop
SCENES.update({k: (None, fn) for k, fn in PM.SCENES.items()})   # so does the AI parody show (parody_motion.py)


def backdrop_for(scene: dict) -> str:
    if scene["type"] in ES.SCENES or scene["type"] in PM.SCENES:
        return "none"
    style = scene.get("style")
    if style in SS.STYLES:
        level = scene.get("level", 0 if scene["type"] == "melt_calm" else 5) if style == "meme" else None
        return style if level is None else f"{style}:{level}"
    return "ember" if scene.get("fp") == "flame" else "spotlight"


def punches(scene: dict, c, dur: float) -> list[float]:
    kind = scene["type"]
    if kind in SS.SCENES:
        return SS.punches(scene, c, dur)
    if kind == "fp_hook":
        return [0.0]
    if kind == "fp_strip":
        return [c.at(claim=c.p["lead"]["rank"], default=dur * 0.45) + 0.1]
    if kind == "fp_own":
        return [c.at(claim=c.p["lead"]["series"][-1]["value"], default=99)]
    if kind == "fp_receipt":
        return [dur * 0.72]
    return []


def sounds(scene: dict, start: float, dur: float, c) -> list[tuple[float, str, float]]:
    kind = scene["type"]
    if kind in SS.SCENES or kind in ES.SCENES or kind in PM.SCENES:
        return SS.sounds(scene, start, dur, c)
    ev = []
    if kind == "fp_hook":
        ev.append((start, "impact", -1))
    if kind == "fp_strip":
        ev.append((start + 0.05, "rise", -12))
        ev.append((start + c.at(claim=c.p["lead"]["rank"], default=dur * 0.45) + 0.05, "stamp", -4))
    if kind == "fp_own":
        ev.append((start + c.at(claim=c.p["lead"]["series"][-1]["value"], default=99) - 0.05, "impact", -5))
    if kind == "fp_receipt":
        for tok in c.tokens:
            if tok["claim"]:
                ev.append((start + tok["start"] - 0.03, "tick", -8))
        ev.append((start + dur * 0.72, "stamp", -2))
    return [e for e in ev if e[0] < start + dur + 0.5]
