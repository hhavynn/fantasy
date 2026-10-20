"""Edit-style scenes for the flame / praise series: the player big, the joke physical, every hit felt.

Three looks, each its own visual language (studio/edit_kit.py has the primitives):

  comparison (vx_)  a confrontation: the ruler gag, two throw lanes (the league's throw against his), the stat that
                    ruins it with the player crashing in, the scoreboard and the safety dismissed, then a freeze
  courtroom (cx_)   an escalating parody trial: gavel slam, polaroid evidence, a conspiracy board with red string,
                    an evidence avalanche, a verdict that cracks the screen and drops jail bars, then a freeze
  hype (hx_)        a player-first edit: giant bust over a giant number, a VS split against a whole offense, close-up
                    stat slams that re-frame him each time, one glowing dot among hundreds, a poster

Every scene reacts to its beat's sound cues (edit_kit.hits): shake, flash, bursts and punch-ins land on the same
frame as the sound, and a `music_stop` cue freezes the picture. Every number drawn is a claim's display string.
Scenes declare `hero` (share of the frame height the player fills) for the visual audit.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from . import edit_kit as K
from . import motion as M
from .format_scenes import stamp

RED = (230, 40, 40)
GOLD = M.GOLD
CREAM = (246, 238, 222)
CORK = (176, 132, 84)
TURF = (38, 120, 62)


def _short(name: str) -> str:
    return [w for w in name.split() if w not in ("Jr.", "Sr.", "II", "III", "IV")][-1]


def _finish(img, t, dur, spec, c, praise: bool = False, hold: bool = False):
    """The shared reaction layer: on each sound hit the frame shakes and (for impacts) flashes; after a hard stop it
    freezes (roast: colour drains; praise: a white flash and hold)."""
    hs = K.hits(c, spec)
    times = [h for h, sfx, _ in hs if sfx not in ("music_stop", "music_cut", "toggle_click")]
    stop = next((h for h, sfx, _ in hs if sfx == "music_stop"), None)
    if stop is not None and t >= stop:
        k = M.clamp((t - stop) / 0.12)
        if hold:                                                       # the picture holds, in colour, still
            return
        frozen = K.freeze(img, k) if not praise else K.flash(img, 0.5 * math.exp(-(t - stop) / 0.08))
        img.paste(frozen)
        return
    amt = 14 * K.hit_strength(t, times, 0.16)
    fl = 0.45 * K.hit_strength(t, times, 0.05)
    out = K.flash(K.shake(img, amt, t), fl)
    img.paste(out)


def _hit_time(c, spec, sfx_class: str | None = None, default: float = 99.0) -> float:
    hs = [h for h, sfx, _ in K.hits(c, spec) if sfx not in ("music_stop", "music_cut")]
    return hs[0] if hs else default


def _stop_time(c, spec) -> float | None:
    return next((h for h, sfx, _ in K.hits(c, spec) if sfx == "music_stop"), None)


# ===================================================================================== comparison (vx_)

def _ruler(length: float, height: float = 110) -> Image.Image:
    """A wooden ruler, ticks only (no printed digits)."""
    L = max(30, int(length))
    lay = Image.new("RGBA", (L + 20, int(height) + 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    d.rounded_rectangle((10, 10, L + 10, height + 10), radius=10, fill=(240, 200, 90, 255), outline=(90, 60, 20, 255),
                        width=5)
    step = max(6, L / 48)
    i = 0
    x = 18.0
    while x < L + 4:
        long = i % 4 == 0
        d.line((x, 12, x, 12 + height * (0.45 if long else 0.25)), fill=(70, 45, 15, 255), width=4 if long else 2)
        x += step
        i += 1
    return lay


def _bo(img, c, height, x, y, crop=(0.1, 0.0, 0.9, 1.0), scale=1.0, angle=0.0, mirror=False):
    """The player, bottom-anchored (his flat edge stays off frame)."""
    pr, _ = K.team_rgb(c.team)
    lay = K.player(c, height, rim=pr, crop=crop, mirror=mirror)
    if angle:
        lay = lay.rotate(angle, expand=True, resample=Image.BICUBIC)
    M.paste(img, lay, x, y if y is not None else M.H - lay.height * scale / 2 + 70, scale, 1.0)


def scene_vx_hook(img, t, dur, spec, c):
    """Frame 0: the player big, a giant ruler measuring him and the question. On 'smaller' the camera snaps to a
    close-up and the ruler shrinks to a stub; the sound hit lands on the stub."""
    p = c.p
    pr, _ = K.team_rgb(c.team)
    img.paste(K.backdrop("versus", c.team, t))
    K.outlined(img, _short(p["name"]).upper(), M.CENTER_X, 900, 330, pr, 0.55, 6)
    hit = _hit_time(c, spec, default=dur * 0.8)
    small_at = c.at(word="smaller", default=hit - 0.4)
    close = t >= small_at - 0.05
    push = 1 + 0.05 * M.clamp(t / max(small_at, 0.1))
    if not close:                                                    # wide: him, the ruler, the question
        _bo(img, c, 1000, M.CENTER_X - 90, None, scale=push)
        k1 = K.ease_spring(M.progress(t + 0.25, 0, 0.2), 1.3)
        M.put_text(img, "MEASURING", M.SAFE_LEFT, 300, size=96, fill=K.WHITE, alpha=M.clamp(k1 * 2), anchor="l",
                   stroke=8, scale=0.8 + 0.2 * k1)
        who = f"{_short(p['name']).upper()}'S PASSES"
        M.put_text(img, who, M.SAFE_LEFT, 410, size=M.fit_size(who, "black", 110, 900), fill=GOLD,
                   alpha=M.clamp(k1 * 2), anchor="l", stroke=8)
        kr = K.ease_spring(M.progress(t, 0.35, 0.3), 1.4)              # the ruler swings in across him
        rl = _ruler(940).rotate(-8 + 14 * (1 - kr), expand=True, resample=Image.BICUBIC)
        M.paste(img, rl, M.CENTER_X + (1 - kr) * 900, 700, 1.0, 1.0)
        if t > 1.0:
            K.sticker(img, "?", M.SAFE_RIGHT - 40, 300, M.progress(t, 1.0, 0.2), size=110, angle=10)
    else:                                                            # close-up, and the tiny ruler
        _bo(img, c, 1500, M.CENTER_X + 40, None, crop=(0.2, 0.0, 0.8, 0.72), scale=1 + 0.03 * (t - small_at))
        shrink = K.ease_spring(M.progress(t, small_at, max(0.15, hit - small_at)), 1.5)
        rl = _ruler(940 - 860 * shrink).rotate(-8, expand=True, resample=Image.BICUBIC)
        M.paste(img, rl, M.CENTER_X - 160, 420, 1.0, 1.0)
        K.sticker(img, "SMALLER RULER", M.CENTER_X, 260, M.progress(t, small_at, 0.2), angle=-5, size=64)
        K.burst(img, M.CENTER_X - 160, 420, K.hit_strength(t, [hit], 0.25), GOLD, r0=90)
    _finish(img, t, dur, spec, c)


def _lane(img, y, label, yards_cid, flight_k, c, ppy=72, x0=200, drop=False, art=None):
    """One throw lane: a turf strip measured in yards, a passer at the line, the ball's arc to its landing."""
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle((40, y - 130, M.W - 40, y + 130), radius=26, fill=TURF + (255,), outline=(255, 255, 255, 90),
                        width=4)
    for yd in range(0, 11):
        x = x0 + yd * ppy
        d.line((x, y - 120, x, y + 120), fill=(255, 255, 255, 140 if yd % 5 == 0 else 60), width=4 if yd % 5 == 0 else 2)
    d.line((x0, y - 120, x0, y + 120), fill=(70, 140, 255, 255), width=8)
    M.put_text(img, label, 70, y - 170, size=46, fill=K.WHITE, anchor="l", stroke=6)
    if art is not None:
        M.paste(img, art, x0 - 95, y + 10, 1.0, 1.0)
    yds = c.claims[yards_cid]["value"]
    k = M.clamp(flight_k)
    x = x0 + yds * ppy * k
    by = y - 10 - math.sin(math.pi * k) * (150 if not drop else 50)
    if drop and k >= 1:
        by = y + 40                                                    # it just plops
    from .scenes import football
    M.paste(img, football(80).rotate(-20 + 40 * k, expand=True), x, by, 1.0, 1.0)
    return x, k


def scene_vx_lanes(img, t, dur, spec, c):
    """The confrontation: the average NFL throw sails; his barely leaves the hand. Same scale, so the gap is real.
    He watches from below, and reacts when his ball plops."""
    p = c.p
    img.paste(K.backdrop("roast", c.team, t))
    t_lg = c.at(claim=spec["league"], default=0.6)
    t_x = c.at(claim=spec["x"], default=dur * 0.7)
    qb = Image.new("RGBA", (110, 160), (0, 0, 0, 0))
    dq = ImageDraw.Draw(qb)
    dq.ellipse((32, 5, 78, 51), fill=(200, 205, 215, 255))
    dq.rounded_rectangle((22, 51, 88, 155), radius=20, fill=(200, 205, 215, 255))
    xl, kl = _lane(img, 440, "NFL AVERAGE", spec["league"], M.progress(t, t_lg - 0.55, 0.55), c, art=qb)
    if kl >= 1:
        K.slam(img, f"{c.d(spec['league'])} YDS", min(xl, M.W - 200), 300, t, t_lg, 92)
    xb, kb = _lane(img, 800, f"{_short(p['name']).upper()} VS THE {c.d(p['opponent']).upper()}", spec["x"],
                   M.progress(t, t_x - 0.45, 0.45), c, drop=True)
    # him, big, under his own lane: neutral while the league throw flies, then the reaction on his number
    react = M.clamp((t - t_x) / 0.2)
    _bo(img, c, 860, M.W - 250, None, scale=1.0 + 0.08 * K.ease_spring(react, 1.6),
        crop=(0.1, 0.0, 0.9, 1.0) if react <= 0 else (0.16, 0.0, 0.84, 0.86))
    if kb >= 1:
        K.slam(img, f"{c.d(spec['x'])} YDS", 270, 1010, t, t_x, 150, fill=(255, 120, 60), angle=-6)
        K.sticker(img, "THAT'S IT?", 270, 1170, M.progress(t, t_x + 0.25, 0.2), angle=-7, size=64)
        K.burst(img, xb, 840, K.hit_strength(t, [t_x], 0.22), (255, 120, 60), r0=60)
    _finish(img, t, dur, spec, c)


def scene_vx_air(img, t, dur, spec, c):
    """His passing yards as one bar, and how little of it flew. He looks on from below, then crashes in on the
    reaction."""
    air, yds = c.claims[spec["air"]]["value"], c.claims[spec["yds"]]["value"]
    img.paste(K.backdrop("roast", c.team, t))
    t_yds = c.at(claim=spec["yds"], default=0.4)
    t_air = c.at(claim=spec["air"], default=dur * 0.6)
    hit = _hit_time(c, spec, default=dur * 0.8)
    kp = K.ease_spring(M.progress(t, hit - 0.06, 0.22), 1.6)
    if kp <= 0:                                                      # watching, slowly pushed in
        _bo(img, c, 760, M.SAFE_RIGHT - 170, None, scale=1 + 0.04 * t / max(dur, 0.1), mirror=True)
    K.slam(img, c.d(spec["yds"]), 330, 400, t, min(0.0, t_yds - 0.1), 300, fill=K.WHITE)
    M.put_text(img, "PASSING YARDS", 330, 560, size=52, fill=(200, 200, 210), alpha=M.clamp(t * 5), stroke=4)
    x0, x1, y = 70, M.W - 70, 700
    d = ImageDraw.Draw(img, "RGBA")
    kb = M.ease_in_out(M.progress(t, 0.1, 0.4))
    if kb > 0:
        d.rounded_rectangle((x0, y, x0 + (x1 - x0) * kb, y + 120), radius=22, fill=(110, 112, 122, 255))
        ka = M.ease_out(M.progress(t, t_air - 0.05, 0.25))
        if ka > 0:
            w = max(24, (x1 - x0) * air / max(yds, 1))
            d.rounded_rectangle((x0, y, x0 + w * ka, y + 120), radius=22, fill=(255, 110, 40, 255))
            M.put_text(img, "AFTER THE CATCH", x0 + w + 30, y + 60, size=44, fill=K.WHITE, anchor="l",
                       alpha=ka, stroke=4)
            K.slam(img, c.d(spec["air"]), x0 + 110, y + 230, t, t_air, 210, fill=(255, 120, 60))
            M.put_text(img, "IN THE AIR", x0 + 250, y + 240, size=58, fill=(255, 140, 80), anchor="l", alpha=ka,
                       stroke=5)
            kc = M.ease_out(M.progress(t, t_air + 0.1, 0.25))
            if kc > 0:                                                  # the red circle, drawn on
                cx, cy, r = x0 + 110, y + 230, 150
                d.arc((cx - r, cy - r * 0.8, cx + r, cy + r * 0.8), -90, -90 + 360 * kc, fill=RED + (255,), width=12)
    if kp > 0:                                                       # he crashes in on the reaction
        _bo(img, c, 1100, M.CENTER_X + 140 + (1 - kp) * 700, None, crop=(0.16, 0.0, 0.84, 0.8), angle=-8)
        K.sticker(img, "AIR? WHAT AIR?", 330, 1100, M.progress(t, hit + 0.1, 0.2), color=K.WHITE, angle=-5, size=64)
    _finish(img, t, dur, spec, c)


def scene_vx_season(img, t, dur, spec, c):
    """The season fact as a leaderboard he tops for the wrong reason."""
    img.paste(K.backdrop("versus", c.team, t))
    k = K.ease_spring(M.progress(t, 0.05, 0.25), 1.3)
    M.put_text(img, "SHORTEST AVERAGE THROW", M.CENTER_X, 320, size=74, fill=GOLD, stroke=6, alpha=M.clamp(k * 2))
    M.put_text(img, f"THIS SEASON · MIN. {c.d(spec['min'])} ATTEMPTS", M.CENTER_X, 400, size=40, fill=K.WHITE,
               stroke=4, alpha=M.clamp(k * 2))
    _bo(img, c, 900, M.CENTER_X, None, scale=0.9 + 0.1 * k)
    t_no = c.at(word="nobody", default=dur * 0.5)
    K.slam(img, "#1", M.CENTER_X + 300, 640, t, t_no, 260, fill=GOLD)
    K.slam(img, f"{c.d(spec['season'])} YDS", M.CENTER_X - 220, 660, t, t_no + 0.1, 120)
    _finish(img, t, dur, spec, c)


def _safety(img, x, y, scale=1.0, alpha=255):
    d = ImageDraw.Draw(img, "RGBA")
    s = scale
    col = (20, 34, 74, alpha)
    d.ellipse((x - 28 * s, y - 120 * s, x + 28 * s, y - 64 * s), fill=col)
    d.rounded_rectangle((x - 40 * s, y - 64 * s, x + 40 * s, y + 40 * s), radius=int(16 * s), fill=col)
    d.rectangle((x - 34 * s, y + 40 * s, x - 10 * s, y + 100 * s), fill=col)
    d.rectangle((x + 10 * s, y + 40 * s, x + 34 * s, y + 100 * s), fill=col)
    d.ellipse((x + 50 * s, y - 40 * s, x + 90 * s, y), fill=(240, 240, 240, alpha))          # a coffee mug


def scene_vx_scoreboard(img, t, dur, spec, c):
    """'Won anyway' on a scoreboard he stands under; then the empty deep field from his side: his ball at his feet,
    the deep safety far away, bored, who gets thrown out on the hit."""
    p, g = c.p, c.p["game"]
    t_tell = c.at(word="tell", default=dur * 0.45)
    hit = _hit_time(c, spec, default=dur * 0.9)
    if t < t_tell - 0.02:
        img.paste(K.backdrop("versus", c.team, t))
        _bo(img, c, 860, M.CENTER_X, None, scale=1 + 0.04 * t)
        d = ImageDraw.Draw(img, "RGBA")
        k = K.ease_spring(M.progress(t + 0.1, 0, 0.25), 1.3)
        bx0, by0, bx1, by1 = 80, 260, M.W - 80, 760
        d.rounded_rectangle((bx0, by0, bx1, by1), radius=30, fill=(10, 10, 14, 245), outline=(255, 255, 255, 120), width=6)
        tl = [(c.d(p["team_name"]).upper(), g["points"]), (c.d(p["opponent"]).upper(), g["opp_points"])]
        if g["result_value"] != "won":
            tl = tl[::-1]
        for i, (name, pts) in enumerate(tl):
            y = 390 + i * 240
            M.put_text(img, name, bx0 + 60, y, size=M.fit_size(name, "black", 92, 520), fill=K.WHITE, anchor="l",
                       alpha=M.clamp(k * 2))
            K.slam(img, c.d(pts), bx1 - 150, y, t, c.at(claim=pts, default=0.3 + 0.3 * i) - 0.05, 150,
                   fill=(255, 210, 60))
        K.sticker(img, f"{c.d(g['result']).upper()} ANYWAY", M.CENTER_X, 830,
                  M.progress(t, c.at(word="anyway", default=0.6), 0.2), angle=-4, size=64)
        _finish(img, t, dur, spec, c)
        return
    # the empty deep field, its one lonely resident, and him at the line with his ball
    img.paste(Image.new("RGB", (M.W, M.H), TURF))
    d = ImageDraw.Draw(img, "RGBA")
    for i, y in enumerate(range(-200, M.H, 160)):
        if i % 2:
            d.rectangle((0, y, M.W, y + 160), fill=(44, 132, 70, 255))
        d.line((0, y, M.W, y), fill=(255, 255, 255, 90), width=4)
    k_out = M.clamp((t - hit) / 0.22)
    sx = M.CENTER_X + 120 + (k_out ** 2) * 900
    zoom = 1 + 0.25 * M.clamp((t - t_tell) / max(0.1, hit - t_tell))   # the camera creeps toward the safety
    _safety(img, sx, 470, 2.0 * zoom)
    if k_out <= 0:
        M.put_text(img, "z z z", sx + 150, 250 - 10 * math.sin(t * 3), size=70, fill=K.WHITE, stroke=5)
        M.put_text(img, "THE DEEP SAFETY", sx, 760, size=52, fill=K.WHITE, stroke=6)
    else:
        K.speed_lines(img, t, alpha=110)
        K.burst(img, M.CENTER_X + 120, 470, K.hit_strength(t, [hit], 0.25), K.WHITE, r0=120)
    _bo(img, c, 720, 250, None, mirror=True)
    from .scenes import football
    M.paste(img, football(90), 520, 1250, 1.0, 1.0)                     # his throw, still at his feet
    stop = _stop_time(c, spec)
    _finish(img, t, dur, spec, c)
    if stop is not None and t >= stop:
        K.sticker(img, "SAFETY: DISMISSED", M.CENTER_X, 900, M.progress(t, stop + 0.05, 0.2), color=(255, 70, 70),
                  fg=K.WHITE, size=72, angle=-5)


# ====================================================================================== courtroom (cx_)

def _gavel(size, angle):
    lay = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    cx, cy = size, size
    d.rounded_rectangle((cx - 16, cy - 10, cx + 16, cy + size * 0.95), radius=10, fill=(120, 70, 36, 255))
    d.rounded_rectangle((cx - size * 0.5, cy - 80, cx + size * 0.5, cy + 12), radius=18, fill=(86, 48, 22, 255),
                        outline=(40, 20, 8, 255), width=6)
    for sx in (-0.36, 0.32):
        d.rectangle((cx + size * sx, cy - 80, cx + size * (sx + 0.06), cy + 12), fill=(220, 180, 90, 255))
    return lay.rotate(angle, resample=Image.BICUBIC, center=(cx, cy + size * 0.95))


def _parody(img):
    f = M.font("black", 38)
    w = int(f.getlength("PARODY")) + 44
    badge = Image.new("RGBA", (w, 64), (0, 0, 0, 0))
    ImageDraw.Draw(badge).rounded_rectangle((0, 0, w - 1, 63), radius=12, fill=RED + (255,))
    ImageDraw.Draw(badge).text((w / 2, 32), "PARODY", font=f, fill=K.WHITE, anchor="mm")
    M.paste(img, badge.rotate(6, expand=True, resample=Image.BICUBIC), M.SAFE_RIGHT - 100, 250, 1.0, 1.0)


def _bench(img):
    d = ImageDraw.Draw(img, "RGBA")
    d.rectangle((0, 330, M.W, 470), fill=(58, 28, 16, 255))
    d.rectangle((0, 330, M.W, 344), fill=(140, 90, 50, 255))
    d.rounded_rectangle((M.CENTER_X - 190, 360, M.CENTER_X + 190, 440), radius=10, fill=(214, 178, 96, 255))
    ImageDraw.Draw(img).text((M.CENTER_X, 400), "THE NFL", font=M.font("black", 52), fill=(40, 24, 10), anchor="mm")


def scene_cx_order(img, t, dur, spec, c):
    """The defendant in the dock, big; the gavel slams on 'Order!'."""
    p = c.p
    pr, _ = K.team_rgb(c.team)
    img.paste(K.backdrop("court", c.team, t))
    _bench(img)
    lay = K.player(c, 1000, rim=pr, crop=(0.1, 0.0, 0.9, 1.0))
    M.paste(img, lay, M.CENTER_X, M.H - lay.height / 2 + 40, 1.0 + 0.03 * t, 1.0)
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle((M.CENTER_X - 400, 1560, M.CENTER_X + 400, 1960), radius=16, fill=(70, 34, 18, 255),
                        outline=(30, 14, 6, 255), width=8)                       # the dock, below his chin
    d.rounded_rectangle((M.CENTER_X - 230, 1600, M.CENTER_X + 230, 1660), radius=8, fill=(214, 178, 96, 255))
    ImageDraw.Draw(img).text((M.CENTER_X, 1630), "DEFENDANT", font=M.font("black", 40), fill=(40, 24, 10), anchor="mm")
    hit = _hit_time(c, spec, default=0.05)
    swing = M.clamp((t - (hit - 0.12)) / 0.12)
    M.paste(img, _gavel(230, 50 - 70 * swing), M.SAFE_RIGHT - 120, 380, 1.0, 1.0)
    K.burst(img, M.SAFE_RIGHT - 160, 420, K.hit_strength(t, [hit], 0.28), (255, 230, 160), r0=70)
    t_vs = c.at(word="nfl", default=0.6)
    K.slam(img, f"THE NFL v. {p['name'].upper()}", M.CENTER_X, 560, t, t_vs, 84, fill=K.WHITE, angle=-3)
    K.slam(img, "ORDER!", M.CENTER_X - 120, 250, t, hit, 120, fill=(255, 220, 120), angle=-6)
    _parody(img)
    _finish(img, t, dur, spec, c)


def _polaroid(c, width, angle, crop=(0.1, 0.0, 0.9, 1.0)):
    inner = K.player(c, int(width * 0.95), outline=0, crop=crop)
    lay = Image.new("RGBA", (width + 60, int(width * 1.18) + 60), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    d.rectangle((30, 30, width + 30, int(width * 1.18) + 30), fill=CREAM + (255,))
    d.rectangle((60, 60, width, int(width * 0.95)), fill=(40, 46, 60, 255))
    inner = inner.resize((width - 60, int(inner.height * (width - 60) / inner.width)))
    lay.alpha_composite(inner.crop((0, 0, width - 60, int(width * 0.95) - 60)), (60, 60))
    return lay.rotate(angle, expand=True, resample=Image.BICUBIC)


def scene_cx_exhibit(img, t, dur, spec, c):
    """Each exhibit is a different piece of evidence, escalating: a mugshot polaroid, a conspiracy board with red
    string, then an avalanche of files."""
    p = c.p
    i = spec.get("index", 0)
    img.paste(K.backdrop("court", c.team, t))
    big_t = c.at(claim=spec["big"], default=0.35)
    hit = _hit_time(c, spec, default=99)
    d = ImageDraw.Draw(img, "RGBA")
    K.slam(img, f"EXHIBIT {spec['letter']}", M.CENTER_X, 270, t, -0.2, 80, fill=(255, 220, 120), angle=-2)
    if i == 0:                                                     # the mugshot, flash, a number scrawled in red
        M.paste(img, _polaroid(c, 640, -5), M.CENTER_X - 40, 820, 1.0, 1.0)
        img.paste(K.flash(img, 0.85 * K.hit_strength(t, [big_t], 0.07)))      # the evidence camera
        K.slam(img, c.d(spec["big"]), M.CENTER_X + 280, 1150, t, big_t, 330, fill=RED, stroke=12, stroke_fill=K.WHITE,
               angle=-10)
        K.slam(img, spec["label"], M.CENTER_X + 120, 1330, t, big_t + 0.12, 96, fill=RED, stroke=10,
               stroke_fill=K.WHITE, angle=-6)
    elif i == 1:                                                   # the conspiracy board
        d.rounded_rectangle((50, 380, M.W - 50, 1290), radius=18, fill=CORK + (255,), outline=(90, 60, 30, 255), width=10)
        rng = np.random.default_rng(4)
        for _ in range(400):                                       # cork texture
            x, y = 60 + rng.random() * (M.W - 120), 390 + rng.random() * 890
            d.ellipse((x, y, x + 4, y + 4), fill=(120, 84, 46, 120))
        pins = [(300, 640), (800, 560), (760, 1060), (300, 1120)]
        M.paste(img, _polaroid(c, 330, 4), *pins[0], 1.0, 1.0)
        if "note" in spec:                                          # the builder says what the second note reads
            second = [f"{pre}{c.d(cid).upper() if cid else ''}{suf}" for pre, cid, suf in spec["note"]]
        elif "rank" in spec:                                        # e.g. 3RD WORST / 412 QB GAMES
            second = [c.d(spec["games"]) + " QB GAMES", (c.d(spec["rank"]).upper() + " " + spec["worst"])
                      if spec["rank_value"] > 1 else "THE " + spec["worst"]]
        else:                                                       # nothing verified to pin: a blank note
            second = ["", ""]
        lines = [(pins[1], [c.d(spec["big"]), _wrap(spec["label"], 14)[0]]), (pins[2], second)]
        for (x, y), txt in lines:
            nd = Image.new("RGBA", (420, 300), (0, 0, 0, 0))
            ndd = ImageDraw.Draw(nd)
            ndd.rectangle((10, 10, 410, 290), fill=(255, 238, 140, 255))
            for j, line in enumerate(txt):
                ndd.text((210, 110 + j * 90), line, font=M.font("black", M.fit_size(line, "black", 66, 370)),
                         fill=(30, 30, 30), anchor="mm")
            M.paste(img, nd.rotate(-4 + 8 * (x > 500), expand=True, resample=Image.BICUBIC), x, y, 1.0, 1.0)
        ks = M.clamp((t - 0.2) / max(0.3, big_t))                  # the red string runs pin to pin as it is said
        path = pins + [pins[0]]
        total = len(path) - 1
        for j in range(total):
            seg = M.clamp(ks * total - j)
            if seg <= 0:
                break
            (xa, ya), (xb, yb) = path[j], path[j + 1]
            d.line((xa, ya, xa + (xb - xa) * seg, ya + (yb - ya) * seg), fill=RED + (255,), width=7)
        for (x, y) in pins:
            d.ellipse((x - 16, y - 16, x + 16, y + 16), fill=(220, 30, 30, 255))
        kc = M.ease_out(M.progress(t, hit - 0.02, 0.25))
        if kc > 0:                                                  # the circle on the damning note
            cx, cy = pins[2]
            d.arc((cx - 250, cy - 190, cx + 250, cy + 190), -90, -90 + 360 * kc, fill=RED + (255,), width=14)
    else:                                                           # the avalanche, burying him
        pr, _ = K.team_rgb(c.team)
        lay = K.player(c, 900, rim=pr, crop=(0.1, 0.0, 0.9, 1.0))
        M.paste(img, lay, M.CENTER_X, M.H - lay.height / 2 + 60 + 260 * M.clamp(t / max(0.5, dur)), 1.0, 1.0)
        rng = np.random.default_rng(9)
        n = 4 + int(30 * M.clamp(t / max(0.5, dur * 0.6)))
        for j in range(n):
            x = 120 + rng.random() * (M.W - 240)
            fall = M.clamp((t - j * 0.035) / 0.25)
            y = 380 + rng.random() * 1200
            ang = rng.random() * 50 - 25
            f = Image.new("RGBA", (300, 210), (0, 0, 0, 0))
            ImageDraw.Draw(f).rounded_rectangle((5, 20, 295, 205), radius=10, fill=(214, 182, 122, 255),
                                                outline=(120, 90, 50, 255), width=4)
            ImageDraw.Draw(f).rounded_rectangle((5, 5, 120, 40), radius=6, fill=(214, 182, 122, 255))
            M.paste(img, f.rotate(ang, expand=True), x, y - (1 - fall) * 900, 1.0, 1.0)
        K.slam(img, c.d(spec["big"]), M.CENTER_X, 760, t, big_t, 420, fill=K.WHITE)
        K.slam(img, spec["label"], M.CENTER_X, 1010, t, big_t + 0.1, 84, fill=(255, 220, 120))
        if hit < 99:
            K.sticker(img, "AT HOME", M.CENTER_X + 160, 1190, M.progress(t, hit - 0.05, 0.2), color=RED, fg=K.WHITE,
                      size=80, angle=-8)
            K.burst(img, M.CENTER_X, 760, K.hit_strength(t, [hit], 0.3), (255, 220, 120), r0=250)
    _parody(img)
    _finish(img, t, dur, spec, c)


def scene_cx_verdict(img, t, dur, spec, c):
    """The gavel winds up through the pause, then the verdict cracks the screen and the bars drop."""
    p = c.p
    pr, _ = K.team_rgb(c.team)
    img.paste(K.backdrop("court", c.team, t))
    _bench(img)
    hit = _hit_time(c, spec, default=dur * 0.4)
    lay = K.player(c, 860, rim=pr, crop=(0.1, 0.0, 0.9, 1.0))
    M.paste(img, lay, M.CENTER_X, 1720 - lay.height / 2 + 100, 1 + 0.05 * M.clamp(t / max(hit, 0.1)), 1.0)
    kb = M.clamp((t - hit) / 0.16)                                  # jail bars slam down
    if kb > 0:
        d = ImageDraw.Draw(img, "RGBA")
        drop = (1 - K.ease_spring(kb, 1.2)) * 900
        for x in range(90, M.W, 130):
            d.rounded_rectangle((x - 14, 470 - drop, x + 14, 1700 - drop), radius=12, fill=(70, 74, 84, 255),
                                outline=(20, 20, 24, 255), width=4)
        d.rectangle((40, 470 - drop, M.W - 40, 510 - drop), fill=(60, 64, 74, 255))
    wind = M.clamp(t / max(0.1, hit - 0.1))                         # the wind-up, then the smash
    ang = 30 + 40 * wind if t < hit - 0.1 else 70 - 110 * M.clamp((t - (hit - 0.1)) / 0.1)
    M.paste(img, _gavel(320, ang), M.CENTER_X + 260, 520, 1.0 + 0.1 * wind, 1.0)
    if t >= hit:
        d = ImageDraw.Draw(img, "RGBA")
        rng = np.random.default_rng(3)                              # the screen cracks from the impact
        for j in range(14):
            a = j / 14 * 2 * math.pi + rng.random() * 0.3
            x, y = M.CENTER_X + 160, 600
            for _ in range(5):
                nx = x + math.cos(a) * (60 + rng.random() * 120)
                ny = y + math.sin(a) * (60 + rng.random() * 120)
                d.line((x, y, nx, ny), fill=(255, 255, 255, 200), width=4)
                x, y, a = nx, ny, a + rng.random() * 0.6 - 0.3
        stamp(img, "GUILTY", M.CENTER_X, 800, RED, K.ease_spring(M.clamp((t - hit) / 0.2), 1.4), size=230, angle=-9)
        kc = M.clamp((t - hit - 0.25) * 4)
        for j, line in enumerate(_wrap(spec.get("charge", ""), 20)):
            M.put_text(img, line, M.CENTER_X, 1000 + j * 80, size=M.fit_size(line, "black", 66, 940), fill=K.WHITE,
                       stroke=6, alpha=kc)
    _parody(img)
    stop = _stop_time(c, spec)
    _finish(img, t, dur, spec, c, hold=True)
    if stop is not None and t >= stop + 0.35:
        K.sticker(img, "CASE CLOSED", M.CENTER_X, 1230, M.progress(t, stop + 0.35, 0.2), color=K.WHITE, size=64,
                  angle=4)


def _wrap(text: str, width: int) -> list[str]:
    out, cur = [], ""
    for w in text.split():
        if len(cur) + len(w) + 1 > width and cur:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return out + ([cur] if cur else [])


# ============================================================================================ hype (hx_)

def scene_hx_open(img, t, dur, spec, c):
    """Frame 0: him, huge, over a giant number; the bell lands on the number. On his name the camera snaps to a
    close-up and the opponent slams in: a second shot inside three seconds."""
    p = c.p
    pr, sc = K.team_rgb(c.team)
    img.paste(K.backdrop("hype", c.team, t, accent=sc))
    big = spec["slams"][0]
    hit = _hit_time(c, spec, default=0.45)
    glow = K.hit_strength(t, [hit], 0.4)
    turn = c.at(word="against", default=dur * 0.55) - 0.25
    word = spec["words"][0]
    if t < turn:
        K.outlined(img, c.d(big), M.CENTER_X, 720, 1000, sc, 0.85, 14)
        lay = K.player(c, 1080, outline=16, rim=sc, crop=(0.08, 0.0, 0.92, 1.0))
        M.paste(img, lay, M.CENTER_X, M.H - lay.height / 2 + 30, 1.0 + 0.025 * t + 0.05 * glow, 1.0)
        K.slam(img, f"{c.d(big)} {word}", M.CENTER_X, 330, t, -0.3, 120, fill=K.WHITE, stroke=10)
        K.burst(img, M.CENTER_X, 700, glow, sc, rays=30, r0=420)
    else:
        K.speed_lines(img, t, sc, 60)
        lay = K.player(c, 1500, outline=16, rim=sc, crop=(0.2, 0.0, 0.8, 0.72), mirror=True)
        M.paste(img, lay, M.CENTER_X - 60, M.H - lay.height / 2 + 30, 1.0 + 0.03 * (t - turn), 1.0)
        M.put_text(img, f"{c.d(big)} {word}", M.CENTER_X, 260, size=70, fill=K.WHITE, stroke=7)
        K.slam(img, f"VS {c.d(p['opponent']).upper()}", M.CENTER_X, 400, t, turn + 0.25, 150, fill=sc, angle=-4)
    _finish(img, t, dur, spec, c, praise=True)


def _silhouette(d, x, y, s, color):
    d.ellipse((x - 18 * s, y - 70 * s, x + 18 * s, y - 34 * s), fill=color)
    d.rounded_rectangle((x - 26 * s, y - 34 * s, x + 26 * s, y + 40 * s), radius=int(10 * s), fill=color)


def scene_hx_vs(img, t, dur, spec, c):
    """VS: him against a whole offense, split on a diagonal."""
    p = c.p
    pr, sc = K.team_rgb(c.team)
    from .numbers import TEAM_NAMES
    opp_code = next((k for k, v in TEAM_NAMES.items() if v == c.d(p["opponent"])), c.team)
    op, _ = M.team_colors(opp_code)                                  # their panel: the real colour, darkened
    _, os_ = K.team_rgb(opp_code)
    img.paste(K.backdrop("hype", c.team, t, accent=sc))
    over = Image.new("RGBA", (M.W, M.H), (0, 0, 0, 0))
    dd = ImageDraw.Draw(over)
    slide = 1 - K.ease_spring(M.progress(t + 0.1, 0, 0.25), 1.2)
    dd.polygon([(M.W * 0.62 + slide * 600, 0), (M.W, 0), (M.W, M.H), (M.W * 0.30 + slide * 600, M.H)],
               fill=tuple(int(v * 0.55) for v in op) + (255,))
    img.paste(Image.alpha_composite(img.convert("RGBA"), over).convert("RGB"))
    lay = K.player(c, 860, rim=sc, crop=(0.08, 0.0, 0.92, 1.0))
    M.paste(img, lay, M.CENTER_X - 210 - slide * 500, 1900 - lay.height / 2 + 40, 1.0, 1.0)
    big = spec["him"]
    M.put_text(img, c.d(big), 260, 560, size=300, fill=K.WHITE, stroke=12, alpha=M.clamp((t + 0.2) * 4))
    M.put_text(img, "TARGETS", 260, 730, size=60, fill=sc, stroke=5)
    d = ImageDraw.Draw(img, "RGBA")
    them_t = c.at(claim=spec["them"], default=dur * 0.7)
    for j in range(11):                                              # the whole offense, tiny
        r, k = divmod(j, 4)
        _silhouette(d, 700 + k * 90 + (r % 2) * 40, 900 + r * 140, 1.1, os_ + (255,))
    M.put_text(img, f"THE ENTIRE {c.d(p['opponent']).upper()}", 840, 1320, size=40, fill=K.WHITE, stroke=4)
    M.put_text(img, "OFFENSE", 840, 1370, size=40, fill=K.WHITE, stroke=4)
    K.slam(img, c.d(spec["them"]), 850, 620, t, them_t, 210, fill=os_)
    kv = K.ease_spring(M.progress(t + 0.15, 0, 0.25), 1.6)
    M.put_text(img, "VS", M.CENTER_X + 60, 1000, size=150, fill=GOLD, stroke=10, scale=0.5 + 0.5 * kv,
               alpha=M.clamp(kv * 2))
    K.burst(img, 850, 620, K.hit_strength(t, [them_t], 0.25), K.WHITE, r0=110)
    _finish(img, t, dur, spec, c, praise=True)


CLOSE_UPS = [((0.24, 0.0, 0.76, 0.62), False, "close"), ((0.06, 0.0, 0.94, 1.0), True, "bust"),
             ((0.16, 0.0, 0.84, 0.82), False, "mid")]


def scene_hx_slams(img, t, dur, spec, c):
    """Each number re-frames him: a close-up, a mirrored bust, a mid shot. A flash and a slam on every one."""
    pr, sc = K.team_rgb(c.team)
    slams, words = spec["slams"], spec["words"]
    times = [c.at(claim=cid, default=0.2 + i * 0.6) for i, cid in enumerate(slams)]
    i = max([j for j, tt in enumerate(times) if t >= tt - 0.05], default=0)
    img.paste(K.backdrop("hype", c.team, t + i, accent=sc))
    crop, mirror, kind = CLOSE_UPS[i % len(CLOSE_UPS)]
    h = {"close": 1200, "bust": 900, "mid": 1050}[kind]
    lay = K.player(c, h, rim=sc, crop=crop, mirror=mirror)
    x = {"close": M.CENTER_X + 170, "bust": M.CENTER_X + 200, "mid": M.CENTER_X}[kind]
    M.paste(img, lay, x, 1960 - lay.height / 2 + 40, 1.0 + 0.04 * (t - times[i]), 1.0)
    K.speed_lines(img, t, sc, 60)
    nx = {"close": 300, "bust": 300, "mid": M.CENTER_X}[kind]
    K.slam(img, c.d(slams[i]), nx, 520, t, times[i], 320, fill=K.WHITE)
    K.slam(img, words[i], nx, 720, t, times[i] + 0.05, 96, fill=sc)
    img.paste(K.flash(img, 0.6 * K.hit_strength(t, times, 0.06)))
    _finish(img, t, dur, spec, c, praise=True)


def scene_hx_rank(img, t, dur, spec, c):
    """Hundreds of games as dots; one burns gold; it opens into him."""
    from .stages.showrunner import POS_WORD
    pr, sc = K.team_rgb(c.team)
    img.paste(K.backdrop("hype", c.team, t, accent=sc))
    n = int(c.claims[spec["games"]]["value"])
    cols = 20
    d = ImageDraw.Draw(img, "RGBA")
    sweep = M.clamp(t / max(0.5, dur * 0.35))
    gx, gy = 0, 0
    for j in range(n):
        r, k = divmod(j, cols)
        x, y = 90 + k * 47, 420 + r * 46
        lit = j / n < sweep
        d.ellipse((x - 13, y - 13, x + 13, y + 13), fill=(255, 255, 255, 140 if lit else 40))
        if j == n // 2 + 7:
            gx, gy = x, y
    hit = _hit_time(c, spec, default=dur * 0.85)
    t_open = min(c.at(word="nobody", default=dur * 0.5), hit) - 0.1     # the dot opens as the claim is made
    open_k = K.ease_spring(M.progress(t, t_open, 0.45), 1.2)
    pulse = 1 + 0.25 * math.sin(t * 8)
    r = 22 * pulse + open_k * 470
    cx, cy = gx + (M.CENTER_X - gx) * open_k, gy + (900 - gy) * open_k
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=GOLD + (255,))
    if open_k > 0.05:                                                 # one cached face, scaled with the circle
        face = K.player(c, 1100, outline=0, crop=(0.2, 0.0, 0.8, 0.75))
        s_ = (2 * r * 1.05) / face.width
        f2 = face.resize((max(1, int(face.width * s_)), max(1, int(face.height * s_))), Image.BILINEAR)
        mask = Image.new("L", f2.size, 0)
        ImageDraw.Draw(mask).ellipse((f2.width / 2 - r, f2.height / 2 - r, f2.width / 2 + r, f2.height / 2 + r),
                                     fill=255)
        f2.putalpha(ImageChops.multiply(f2.getchannel("A"), mask))
        M.paste(img, f2, cx, cy, 1.0, M.clamp(open_k * 2))
    pos = POS_WORD.get(c.p["position"], "player").upper()
    swap = t_open + 0.2                                              # the headline is replaced by the verdict, up top
    if t < swap - 0.02:
        M.put_text(img, f"{c.d(spec['games'])} {pos} GAMES THIS SEASON", M.CENTER_X, 300, size=58, fill=K.WHITE,
                   stroke=5, max_width=960)
    sup = spec.get("sup", "MOST")                                    # a monster line's lead stat: HIGHEST, MOST
    rank1 = ("NOBODY'S SEEN MORE" if sup == "MOST" and not spec.get("tied")
             else f"{'TIED FOR ' if spec.get('tied') else ''}THE {sup}")
    K.slam(img, rank1 if spec.get("rank1") else f"{c.d(spec['rank']).upper()}-{sup}",
           M.CENTER_X, 300, t, swap, 96, fill=GOLD, stroke=9)
    K.burst(img, cx, cy, K.hit_strength(t, [hit], 0.3), GOLD, rays=28, r0=r + 20)
    _finish(img, t, dur, spec, c, praise=True)


def scene_hx_poster(img, t, dur, spec, c):
    """The poster: him, the tagline, the final score small."""
    p, g = c.p, c.p["game"]
    pr, sc = K.team_rgb(c.team)
    img.paste(K.backdrop("hype", c.team, t, accent=sc))
    K.outlined(img, _short(p["name"]).upper(), M.CENTER_X, 640, 330, sc, 0.7, 8)
    lay = K.player(c, 1060, rim=sc, crop=(0.08, 0.0, 0.92, 1.0))
    M.paste(img, lay, M.CENTER_X, 1960 - lay.height / 2 + 30, 1.0 + 0.03 * t, 1.0)
    k = K.ease_spring(M.progress(t, c.at(word=_short(p["name"]).lower(), default=dur * 0.5) - 0.1, 0.25), 1.3)
    M.put_text(img, spec.get("tagline", ""), M.CENTER_X, 330, size=M.fit_size(spec.get("tagline", ""), "black", 86, 960),
               fill=K.WHITE, stroke=8, alpha=M.clamp(k * 2), scale=0.8 + 0.2 * k)
    score = f"{c.d(p['opponent']).upper()} {c.d(g['opp_points'])} · {c.d(p['team_name']).upper()} {c.d(g['points'])}"
    M.put_text(img, score, M.CENTER_X, 440, size=46, fill=sc, stroke=5, alpha=M.clamp(t * 4))
    credits = "  ·  ".join(f"{c.d(cid)} {ab}" for cid, ab in spec.get("credits", []))
    M.put_text(img, credits, M.CENTER_X, 520, size=M.fit_size(credits, "bold", 40, 960), weight="bold", fill=K.WHITE,
               stroke=4, alpha=M.clamp(t * 4))
    _finish(img, t, dur, spec, c, praise=True)


# Share of the frame height the player fills, time-weighted over the scene (for the visual audit's hero_share).
HERO = {"vx_hook": 0.55, "vx_lanes": 0.4, "vx_air": 0.4, "vx_season": 0.45, "vx_scoreboard": 0.37,
        "cx_order": 0.45, "cx_exhibit": 0.18, "cx_verdict": 0.43,
        "hx_open": 0.55, "hx_vs": 0.44, "hx_slams": 0.55, "hx_rank": 0.15, "hx_poster": 0.54}

SCENES = {"vx_hook": scene_vx_hook, "vx_lanes": scene_vx_lanes, "vx_air": scene_vx_air, "vx_season": scene_vx_season,
          "vx_scoreboard": scene_vx_scoreboard, "cx_order": scene_cx_order, "cx_exhibit": scene_cx_exhibit,
          "cx_verdict": scene_cx_verdict, "hx_open": scene_hx_open, "hx_vs": scene_hx_vs, "hx_slams": scene_hx_slams,
          "hx_rank": scene_hx_rank, "hx_poster": scene_hx_poster}
