"""Scenes for the editorial formats (studio/stages/formats.py). Each one shows a football idea, not a chart.

  mystery (case-file look: near-black slate, hard cuts, a still camera that snaps in on each reveal)
    mirror        the contradiction: two games' snaps collide as equals, then their targets land as unequal
    volume_balls  the team's targets as footballs, game against game, with empty slots for the volume that left
    rival         a same-position teammate back in the lineup, face to face with him: snaps and targets
    verdict       what is steady and what is not, stamped; then the call

  opportunity (alert look: team-colour field, whip pans, a drifting camera)
    giant_number  the workload as one huge number on frame 0, with who and what it measures
    carry_grid    the carries as footballs: his share now, or the old split with the starter
    handoff       the starter ruled out, his carries flying across to the backup
    report        the injury report as a dated sheet with the practice status stamped on it
    window        the call while the starter sits, then the only split seen with him back

Every number drawn is a claim's display string (c.d), so the screen and the narration cannot disagree.
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import headshots
from . import motion as M
from .scenes import football

TEAL = (64, 214, 200)
RED = (255, 84, 84)
PAPER = (242, 238, 228)
PAPER_INK = (24, 26, 30)
SILVER = (196, 204, 214)
BRAND = "FANTASY INTELLIGENCE"


# ------------------------------------------------------------------------------------------ backdrops

@lru_cache(maxsize=4)
def casefile_backdrop(tint: tuple) -> Image.Image:
    """Slate, a faint ruled grid, a cold top light and a hard vignette: an evidence table."""
    yy, xx = np.mgrid[0:M.H, 0:M.W].astype(np.float32)
    r = np.sqrt(((xx - M.W * 0.5) / M.W) ** 2 + ((yy - M.H * 0.3) / M.H) ** 2)
    light = np.clip(1 - r * 1.8, 0, 1) ** 1.6
    base = np.array((12, 15, 20), np.float32)[None, None] + light[..., None] * np.array(tint, np.float32) * 0.16
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")
    d = ImageDraw.Draw(img, "RGBA")
    for y in range(0, M.H, 64):
        d.line((0, y, M.W, y), fill=(255, 255, 255, 7))
    d.line((48, 0, 48, M.H), fill=RED + (40,), width=2)          # a margin rule, like a case notebook
    return img


@lru_cache(maxsize=4)
def alert_backdrop(primary: tuple, secondary: tuple) -> Image.Image:
    """Team colour, deep at the edges, with a bright diagonal band: urgent but clean."""
    yy, xx = np.mgrid[0:M.H, 0:M.W].astype(np.float32)
    p = np.array(M.vivid(primary, primary), np.float32)
    k = np.clip(1 - np.abs(xx / M.W - 0.5) * 1.1, 0, 1) * np.clip(1 - np.abs(yy / M.H - 0.42) * 1.2, 0, 1)
    base = p[None, None] * (0.22 + 0.45 * k[..., None])
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")


def background(kind: str, c, t: float) -> Image.Image:
    if kind == "casefile":
        return casefile_backdrop(TEAL).copy()
    img = alert_backdrop(c.primary, c.secondary).copy()
    d = ImageDraw.Draw(img, "RGBA")
    x = ((t * 260) % (M.W + 900)) - 450                 # one slow light band sweeping across
    d.polygon([(x, 0), (x + 220, 0), (x - 380, M.H), (x - 600, M.H)], fill=(255, 255, 255, 14))
    return img


# ------------------------------------------------------------------------------------------ helpers

def face(c, espn_id, team: str, number, last_name: str, width: int) -> Image.Image:
    shot = headshots.fetch(espn_id) if c.headshots_on and espn_id else None
    return M.player_art(team, str(number or ""), last_name, shot, width)


def exhibit(img, letter: str, title: str, t: float, y: int = 300):
    k = M.ease_out(M.progress(t, 0, 0.25))
    tag = f"EXHIBIT {letter}"
    f = M.font("black", 30)
    w = f.getlength(tag) + 40
    lay = Image.new("RGBA", (int(w), 56), (0, 0, 0, 0))
    ImageDraw.Draw(lay).rounded_rectangle((1, 1, w - 2, 54), radius=8, outline=RED, width=3)
    ImageDraw.Draw(lay).text((w / 2, 28), tag, font=f, fill=RED, anchor="mm")
    M.paste(img, lay, M.SAFE_LEFT + 30, y, 1.0, k, anchor="l")
    M.put_text(img, title, M.SAFE_LEFT, y + 82, size=M.fit_size(title, "black", 64, 860), alpha=k, anchor="l")


def identity(img, c, t: float, y: int = 300, slide: bool = True):
    """Who this is, small and constant at the top: face, name, position and team."""
    k = M.ease_out(M.progress(t, 0, 0.25)) if slide else 1.0
    av = c.art(150)
    M.paste(img, av, M.SAFE_LEFT + 70 - (1 - k) * 200, y, 1.0, k)
    name = c.p["name"].upper()
    M.put_text(img, name, M.SAFE_LEFT + 160 - (1 - k) * 120, y - 22, size=M.fit_size(name, "black", 56, 700),
               alpha=k, anchor="l")
    sub = f"{c.p['position']} · {c.d(c.p['team_name']).upper()}"
    M.put_text(img, sub, M.SAFE_LEFT + 160 - (1 - k) * 120, y + 34, size=30, fill=M.MUTED, alpha=k, anchor="l")


def brand(img, t: float, dur: float, y: int = 1268):
    k = M.ease_out(M.progress(t, max(0.0, dur - 1.6), 0.5))
    if k <= 0:
        return
    f = M.font("bold", 26)
    spaced = " ".join(BRAND)
    M.put_text(img, spaced, M.CENTER_X, y, weight="bold", size=26, fill=M.MUTED, alpha=0.8 * k)
    d = ImageDraw.Draw(img, "RGBA")
    half = f.getlength(spaced) / 2 + 30
    d.line((M.CENTER_X - half - 60, y, M.CENTER_X - half, y), fill=M.MUTED + (int(150 * k),), width=2)
    d.line((M.CENTER_X + half, y, M.CENTER_X + half + 60, y), fill=M.MUTED + (int(150 * k),), width=2)


def stamp(img, text: str, cx, cy, color, k: float, size: int = 110, angle: float = -6):
    if k <= 0:
        return
    lay = M.text_layer(text, "black", M.fit_size(text, "black", size, 820), color, stroke=0, shadow=False)
    pad = 26
    box = Image.new("RGBA", (lay.width + pad * 2, lay.height + pad), (0, 0, 0, 0))
    ImageDraw.Draw(box).rounded_rectangle((4, 4, box.width - 5, box.height - 5), radius=14, outline=color, width=8)
    box.alpha_composite(lay, (pad, pad // 2))
    M.paste(img, box.rotate(angle, expand=True, resample=Image.BICUBIC), cx, cy, 1.5 - 0.5 * k, M.clamp(k * 2.5))


def ball_grid(img, n: int, x0: float, y0: float, per_row: int, step: float, size: int, t: float, start: float,
              span: float, highlight: int = 0, ghosts: int = 0, ghost_start: float = 99, tint=None):
    """n footballs popping in order (over `span` seconds from `start`), the first `highlight` tinted gold,
    then `ghosts` dashed outlines for what is missing."""
    ball = football(size)
    ghost = football(size, ghost=True)
    for i in range(n + ghosts):
        r, col = divmod(i, per_row)
        x, y = x0 + col * step, y0 + r * step * 0.8
        if i < n:
            k = M.back_out(M.progress(t, start + span * i / max(n, 1), 0.18))
            if k <= 0:
                continue
            lay = _tinted(size, M.GOLD) if i < highlight else (ball if tint is None else _tinted(size, tint))
            M.paste(img, lay, x, y, 0.4 + 0.6 * k, M.clamp(k * 2))
        else:
            k = M.ease_out(M.progress(t, ghost_start + 0.04 * (i - n), 0.2))
            if k > 0:
                M.paste(img, ghost, x, y, 1.0, k)


@lru_cache(maxsize=8)
def _tinted(size: int, tint: tuple) -> Image.Image:
    b = football(size).copy()
    arr = np.asarray(b).astype(np.float32)
    lum = arr[..., :3].mean(-1, keepdims=True) / 255
    arr[..., :3] = np.array(tint, np.float32)[None, None] * (0.55 + 0.45 * lum)
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


def _n(c, cid) -> int:
    return int(round(c.claims[cid]["value"])) if cid else 0


def _games(c, weeks):
    by_week = {g["week"]: g for g in c.p["details"]["games"]}
    return [by_week[w] for w in weeks]


# ------------------------------------------------------------------------------------------ mystery

def scene_mirror(img, t, dur, spec, c):
    a, b = _games(c, spec["games"])
    stage = spec.get("stage", "collide")
    settled = stage == "targets"
    identity(img, c, t if not settled else 9, slide=not settled)
    xl, xr = M.CENTER_X - 245, M.CENTER_X + 245
    # Row 1: the snaps, equal
    tl = 9 if settled else t
    kl = M.ease_out(M.progress(tl, 0.05, 0.3))
    for x, g, side in ((xl, a, -1), (xr, b, 1)):
        M.put_text(img, c.d(g["week_claim"]).upper(), x, 510, size=42, fill=M.MUTED, alpha=kl)
        sx = x + side * (1 - kl) * 700
        M.put_text(img, c.d(g["snaps"]), sx, 660, size=220, fill=M.INK, alpha=M.clamp(kl * 2))
        M.put_text(img, f"SNAPS · {c.d(g['snap_share'])}", x, 805, size=36, fill=TEAL, alpha=kl)
    land = M.back_out(M.progress(tl, 0.33, 0.25))
    if land > 0:
        M.put_text(img, "=", M.CENTER_X, 650, size=150, fill=TEAL, scale=0.5 + 0.5 * land, alpha=M.clamp(land * 2))
        ring = M.clamp((tl - 0.33) / 0.4)
        if 0 < ring < 1:
            d = ImageDraw.Draw(img, "RGBA")
            rr = 60 + 400 * ring
            d.ellipse((M.CENTER_X - rr, 660 - rr, M.CENTER_X + rr, 660 + rr), outline=TEAL + (int(160 * (1 - ring)),),
                      width=6)
    # Row 2: the targets, unequal. Teased as "?" while the snaps line plays, then filled as they are said.
    kq = M.ease_out(M.progress(tl, 0.9, 0.3))
    M.put_text(img, "TARGETS", M.CENTER_X, 930, size=40, fill=RED, alpha=kq)
    if stage == "collide":
        pulse = 1 + 0.06 * math.sin(t * 7)
        for x in (xl, xr):
            M.put_text(img, "?", x, 1080, size=200, fill=(70, 78, 92), scale=pulse, alpha=kq)
        return
    for x, g in ((xl, a), (xr, b)):
        at = c.at(claim=g["targets"], default=0.6)
        k = M.back_out(M.progress(t, at - 0.08, 0.28))
        if k <= 0:
            if stage == "both":          # one-line hooks: the question marks hold until each number is said
                M.put_text(img, "?", x, 1080, size=200, fill=(70, 78, 92), scale=1 + 0.06 * math.sin(t * 7), alpha=kq)
            continue
        big = _n(c, g["targets"]) >= _n(c, b["targets"]) and _n(c, g["targets"]) >= _n(c, a["targets"])
        col = M.INK if big else RED
        M.put_text(img, c.d(g["targets"]), x, 1060, size=190, fill=col, scale=0.5 + 0.5 * k, alpha=M.clamp(k * 2))
        n = _n(c, g["targets"])
        ball_grid(img, n, x - 1.5 * 74, 1200, 4, 74, 60, t, at, 0.35)
    at2 = c.at(claim=b["targets"], default=0.9)
    kn = M.back_out(M.progress(t, at2 + 0.05, 0.25))
    if kn > 0:
        M.put_text(img, "≠", M.CENTER_X, 1050, size=150, fill=RED, scale=0.5 + 0.5 * kn, alpha=M.clamp(kn * 2))


def scene_volume_balls(img, t, dur, spec, c):
    a, b = _games(c, spec["games"])
    team = c.d(c.p["team_name"]).upper()
    exhibit(img, spec.get("exhibit", "A"), f"{team} TARGETS, GAME BY GAME", t)
    na, nb = _n(c, a["team_targets"]), _n(c, b["team_targets"])
    per, step, size = 10, 84, 64
    x0 = M.SAFE_LEFT + 60
    for row, (g, n, y, start) in enumerate(((a, na, 600, 0.15), (b, nb, 960, 0.55))):
        k = M.ease_out(M.progress(t, start - 0.1, 0.25))
        M.put_text(img, c.d(g["week_claim"]).upper(), M.SAFE_LEFT, y - 80, size=38, fill=M.MUTED, alpha=k, anchor="l")
        M.put_text(img, c.d(g["team_targets"]), M.SAFE_RIGHT - 60, y - 84, size=84, fill=M.INK if row == 0 else RED,
                   alpha=k)
        ghosts = max(0, na - nb) if row == 1 else 0
        ball_grid(img, n, x0, y, per, step, size, t, start, 0.55, highlight=_n(c, g["targets"]), ghosts=ghosts,
                  ghost_start=start + 0.65)
    kk = M.ease_out(M.progress(t, 1.4, 0.3))
    if kk > 0 and na > nb:
        lay = M.text_layer(f"−{na - nb}", "black", 90, RED, stroke=6)
        M.paste(img, lay, M.SAFE_RIGHT - 70, 1150, 0.6 + 0.4 * kk, M.clamp(kk * 2))
    last = c.p["name"].split()[-1] if c.p["name"].split()[-1] not in ("II", "III", "Jr.", "Sr.") else c.p["name"].split()[-2]
    M.put_text(img, f"GOLD = {last.upper()}'S TARGETS", M.SAFE_LEFT, 1268, size=34, fill=M.GOLD,
               alpha=M.clamp((t - 1.0) * 3), anchor="l")


def scene_rival(img, t, dur, spec, c):
    sp = c.p["details"]["spread"]
    r = next(x for x in sp["returned"] if x["gsis_id"] == spec["rival"])
    b = c.p["details"]["games"][-1]
    exhibit(img, spec.get("exhibit", "B"), f"{c.d(sp['week_claim']).upper()}: THE OTHER {c.p['position']}", t)
    xl, xr = M.CENTER_X - 220, M.CENTER_X + 220
    ka = M.ease_out(M.progress(t, 0.05, 0.3))
    me = c.art(300)
    other = face(c, r.get("espn_id"), c.team, "", r["name"].split()[-1], 300)
    M.paste(img, me, xl - (1 - ka) * 500, 600, 1.0, ka)
    M.paste(img, other, xr + (1 - ka) * 500, 600, 1.0, ka)
    nm = lambda s: s.split()[-1] if s.split()[-1] not in ("II", "III", "Jr.", "Sr.") else s.split()[-2]
    M.put_text(img, nm(c.p["name"]).upper(), xl, 800, size=48, alpha=ka)
    M.put_text(img, nm(r["name"]).upper(), xr, 800, size=48, alpha=ka)
    kb = M.back_out(M.progress(t, c.at(word="back", default=0.8) - 0.05, 0.25))
    if kb > 0:
        tag = f"BACK · MISSED {c.d(r['missed']).upper()}"
        lay = M.text_layer(tag, "black", M.fit_size(tag, "black", 36, 520), M.DARK, shadow=False)
        pill = Image.new("RGBA", (lay.width + 10, lay.height), (0, 0, 0, 0))
        ImageDraw.Draw(pill).rounded_rectangle((0, 8, pill.width - 1, pill.height - 8), radius=22, fill=M.GOLD)
        pill.alpha_composite(lay, (5, 0))
        M.paste(img, pill, min(xr, M.SAFE_RIGHT - pill.width / 2), 445, 0.6 + 0.4 * kb, M.clamp(kb * 2))
    rows = [("SNAPS", b["snaps"], r["snaps"], c.at(word="more", default=1.6)),
            ("TARGETS", b["targets"], r["targets"], c.at(claim=r["targets"], default=2.4))]
    for i, (label, mine, theirs, at) in enumerate(rows):
        y = 960 + i * 180
        k = M.back_out(M.progress(t, at - 0.12, 0.28))
        if k <= 0:
            continue
        M.put_text(img, label, M.CENTER_X, y - 70, size=32, fill=M.MUTED, alpha=M.clamp(k * 2))
        win = _n(c, theirs) > _n(c, mine)
        M.put_text(img, c.d(mine), xl, y + 10, size=120, fill=M.MUTED if win else M.INK, scale=0.6 + 0.4 * k,
                   alpha=M.clamp(k * 2))
        M.put_text(img, c.d(theirs), xr, y + 10, size=120, fill=M.GOLD if win else M.INK, scale=0.6 + 0.4 * k,
                   alpha=M.clamp(k * 2))
        M.put_text(img, "vs", M.CENTER_X, y + 20, weight="bold", size=40, fill=M.MUTED, alpha=M.clamp(k * 2))


def scene_verdict(img, t, dur, spec, c):
    a, b = _games(c, spec["games"])
    call = spec.get("stage") == "call"
    tb = 9 if call else t
    lift = M.ease_in_out(M.progress(t, 0.0, 0.45)) if call else 0.0
    M.put_text(img, "THE VERDICT", M.SAFE_LEFT, 300 - 30 * lift, size=44, fill=M.MUTED, alpha=1 - 0.4 * lift,
               anchor="l")
    rows = [("SNAPS", f"{c.d(a['snaps'])} · {c.d(b['snaps'])}", "STEADY", TEAL, "budge"),
            ("TARGETS", f"{c.d(a['targets'])} · {c.d(b['targets'])}", "SWINGING", RED, None)]
    for i, (label, vals, word, col, cue) in enumerate(rows):
        y = 470 + i * 230 - lift * (90 + i * 60)
        s = 1 - 0.25 * lift
        k = M.ease_out(M.progress(tb, 0.05 + i * 0.25, 0.3))
        card = Image.new("RGBA", (880, 190), (0, 0, 0, 0))
        cd = ImageDraw.Draw(card)
        cd.rounded_rectangle((0, 0, 879, 189), radius=26, fill=(20, 26, 34, 235), outline=col + (120,), width=3)
        cd.text((40, 60), label, font=M.font("black", 40), fill=M.MUTED, anchor="lm")
        cd.text((40, 128), vals, font=M.font("black", 76), fill=M.INK, anchor="lm")
        M.paste(img, card, M.CENTER_X, y + (1 - k) * 60, s, k)
        # the swing is already known (stamped as the board lands); "steady" lands on "didn't budge"
        at = c.at(word=cue, default=dur * 0.6) if cue else 0.6
        ks = M.back_out(M.progress(tb, at - 0.05, 0.25))
        stamp(img, word, M.CENTER_X + 250 * s, y + 10, col, ks, size=64, angle=-8)
    if call:
        lines = spec.get("stamp", [])
        for i, line in enumerate(lines):
            k = M.back_out(M.progress(t, 0.35 + i * 0.5, 0.3))
            if k <= 0:
                continue
            size = M.fit_size(line, "black", 130 if i == 0 else 84, 860)
            M.put_text(img, line, M.CENTER_X, 900 + i * 150, size=size, fill=M.INK if i == 0 else M.GOLD,
                       scale=1.4 - 0.4 * k, alpha=M.clamp(k * 2.5))
        brand(img, t, dur)


# -------------------------------------------------------------------------------------- opportunity

def _tm_face(c, width):
    tm = c.p["teammate"]
    return face(c, tm.get("espn_id"), c.team, tm.get("jersey"), tm["name"].split()[-1], width)


def scene_giant_number(img, t, dur, spec, c):
    car = c.p["details"]["carries"]
    k = M.back_out(M.progress(t, 0.0, 0.22), 1.2)
    shake = math.sin(t * 90) * 14 * max(0.0, 1 - t / 0.35) if t < 0.35 else 0
    M.put_text(img, c.d(car["own_recent_share"]), M.CENTER_X + shake, 680, size=380, fill=M.INK, scale=1.6 - 0.6 * k,
               alpha=M.clamp(k * 3), stroke=0)
    kn = M.ease_out(M.progress(t, 0.15, 0.3))
    name = c.p["name"].upper()
    M.put_text(img, name, M.SAFE_LEFT, 300, size=M.fit_size(name, "black", 72, 860), alpha=kn, anchor="l")
    team = c.d(c.p["team_name"]).upper()
    M.put_text(img, f"OF THE {team}' RUNNING BACK CARRIES", M.CENTER_X, 905,
               size=M.fit_size(f"OF THE {team}' RUNNING BACK CARRIES", "black", 50, 860), alpha=kn)
    kc = M.ease_out(M.progress(t, 0.3, 0.3))
    chip = f"{c.d(car['recent_weeks']).upper()} · {c.d(car['own_recent'])} OF {c.d(car['team_recent'])}"
    lay = M.text_layer(chip, "black", 38, M.DARK, shadow=False)
    pill = Image.new("RGBA", (lay.width + 20, lay.height), (0, 0, 0, 0))
    ImageDraw.Draw(pill).rounded_rectangle((0, 12, pill.width - 1, pill.height - 12), radius=28, fill=M.GOLD)
    pill.alpha_composite(lay, (10, 0))
    M.paste(img, pill, M.CENTER_X, 1000, 1.0, kc)
    kh = M.ease_out(M.progress(t, 0.35, 0.35))
    M.paste(img, c.art(170), M.SAFE_RIGHT - 80 + (1 - kh) * 300, 290, 1.0, kh)
    # the carries themselves, filling while the line is said: a preview of the grid that follows
    total, mine = _n(c, car["team_recent"]), _n(c, car["own_recent"])
    per, step, size = 10, 74, 58
    x0 = M.CENTER_X - (per - 1) * step / 2
    end = max(1.2, c.at(word="carry", default=dur * 0.8))
    ball_grid(img, mine, x0, 1095, per, step, size, t, 0.6, end - 0.6, ghosts=total - mine, ghost_start=end)


def scene_carry_grid(img, t, dur, spec, c):
    car = c.p["details"]["carries"]
    tm = c.p["teammate"]
    before = spec.get("stage") == "before"
    last = lambda s: s.split()[-1].upper()
    if not before:
        total, mine = _n(c, car["team_recent"]), _n(c, car["own_recent"])
        label_y = 300
        if spec.get("identify"):         # opening the video: say who before anything else
            identity(img, c, t)
            label_y = 440
        M.put_text(img, f"RUNNING BACK CARRIES · {c.d(car['recent_weeks']).upper()}", M.SAFE_LEFT, label_y,
                   size=40, fill=M.MUTED, alpha=M.clamp(t * 5), anchor="l")
        per, step, size = 8, 104, 84
        x0 = M.CENTER_X - (per - 1) * step / 2
        gy = 470 if not spec.get("identify") else 580
        ball_grid(img, mine, x0, gy, per, step, size, t, 0.1, 1.1)
        rest = total - mine
        ghost = football(size, ghost=True)
        for i in range(rest):
            r, col = divmod(mine + i, per)
            k = M.ease_out(M.progress(t, 1.25 + 0.1 * i, 0.2))
            M.paste(img, ghost, x0 + col * step, gy + r * step * 0.8, 1.0, k)
        kc = M.back_out(M.progress(t, c.at(claim=car["own_recent"], default=1.0) - 0.05, 0.25))
        if kc > 0:
            M.put_text(img, c.d(car["own_recent"]), M.CENTER_X - 150, 1110, size=190, fill=M.GOLD, scale=0.6 + 0.4 * kc,
                       alpha=M.clamp(kc * 2))
            M.put_text(img, f"OF {c.d(car['team_recent'])}", M.CENTER_X + 150, 1125, size=90, fill=M.INK,
                       alpha=M.clamp(kc * 2))
            M.put_text(img, f"{last(c.p['name'])} = FULL FOOTBALL · OTHER BACKS = OUTLINE", M.CENTER_X, 1255,
                       size=26, fill=M.MUTED, alpha=M.clamp(kc * 2))
        return
    # the old split, with the starter: his carries in silver, the backup's in gold
    total, his, mine = _n(c, car["team_base"]), _n(c, car["tm_base"]), _n(c, car["own_base"])
    M.put_text(img, f"{c.d(car['base_weeks']).upper()} · WITH {last(tm['name'])}", M.SAFE_LEFT, 300, size=40,
               fill=M.MUTED, alpha=M.clamp(t * 5), anchor="l")
    per, step, size = 10, 84, 64
    x0 = M.CENTER_X - (per - 1) * step / 2
    ball_grid(img, his, x0, 430, per, step, size, t, 0.05, 0.8, tint=SILVER, highlight=0)
    gold = _tinted(size, M.GOLD)
    g0 = max(0.9, c.at(claim=car["own_base"], default=1.2) - 0.45)
    for i in range(mine):
        r, col = divmod(his + i, per)
        k = M.back_out(M.progress(t, g0 + 0.03 * i, 0.18))
        if k > 0:
            M.paste(img, gold, x0 + col * step, 430 + r * step * 0.8, 0.4 + 0.6 * k, M.clamp(k * 2))
    other = total - his - mine
    for i in range(max(0, other)):
        r, col = divmod(his + mine + i, per)
        M.paste(img, football(size, ghost=True), x0 + col * step, 430 + r * step * 0.8, 1.0,
                M.clamp((t - 1.6) * 4))
    for x, art, n, col, cid in ((M.CENTER_X - 220, _tm_face(c, 190), his, SILVER, car["tm_base"]),
                                (M.CENTER_X + 220, c.art(190), mine, M.GOLD, car["own_base"])):
        k = M.back_out(M.progress(t, c.at(claim=cid, default=1.2) - 0.1, 0.25))
        if k <= 0:
            continue
        M.paste(img, art, x - 110, 1130, 1.0, M.clamp(k * 2))
        M.put_text(img, c.d(cid), x + 60, 1130, size=120, fill=col, scale=0.6 + 0.4 * k, alpha=M.clamp(k * 2))
        nm = (tm["name"] if col == SILVER else c.p["name"]).split()[-1].upper()
        M.put_text(img, nm, x - 110, 1245, size=32, fill=col, alpha=M.clamp(k * 2))


def scene_handoff(img, t, dur, spec, c):
    tm = c.p["teammate"]
    xl, xr = M.CENTER_X - 230, M.CENTER_X + 230
    M.paste(img, _tm_face(c, 330), xl, 560, 1.0, 1.0)
    M.paste(img, c.art(330), xr, 560, 1.0, 1.0)
    out_at = c.at(word="out", default=dur * 0.5)
    ko = M.back_out(M.progress(t, out_at - 0.1, 0.25))
    if ko > 0:
        d = ImageDraw.Draw(img, "RGBA")
        d.ellipse((xl - 160, 420, xl + 160, 740), fill=(0, 0, 0, int(120 * M.clamp(ko))))
        stamp(img, "OUT", xl, 580, RED, ko, size=110, angle=-12)
    kr = M.ease_out(M.progress(t, c.at(claim=tm.get("reason"), default=0.3) - 0.1, 0.3)) if tm.get("reason") else 0
    if kr > 0:
        reason = c.d(tm["reason"]).upper().removeprefix("A ").removeprefix("AN ")
        lay = M.text_layer(reason, "black", M.fit_size(reason, "black", 40, 400), M.INK, shadow=False)
        pill = Image.new("RGBA", (lay.width + 16, lay.height), (0, 0, 0, 0))
        ImageDraw.Draw(pill).rounded_rectangle((0, 14, pill.width - 1, pill.height - 14), radius=26, fill=RED)
        pill.alpha_composite(lay, (8, 0))
        M.paste(img, pill, xl, 800, 1.0, kr)
        M.put_text(img, f"MISSED {c.d(tm['missed']).upper()}", xl, 870, size=32, fill=M.INK, alpha=kr)
    # his carries arc across to the backup once he is ruled out
    ball = football(70)
    for i in range(14):
        ft = M.clamp((t - out_at - 0.05 - i * 0.07) / 0.55)
        if ft <= 0 or ft >= 1:
            continue
        x = xl + (xr - xl) * M.ease_in_out(ft)
        y = 560 - math.sin(math.pi * ft) * 260 + (i % 3) * 18
        M.paste(img, ball.rotate((i * 40 + ft * 300) % 360, expand=True), x, y, 1.0, M.clamp(1.4 - ft))
    kc = M.ease_out(M.progress(t, out_at + 0.4, 0.3))
    M.put_text(img, "HIS CARRIES WENT HERE", xr, 800, size=34, fill=M.GOLD, alpha=kc)


STATUS_WORDS = {"Did Not Participate In Practice": "DID NOT PRACTICE", "Limited Participation in Practice":
                "LIMITED PRACTICE", "Full Participation in Practice": "FULL PRACTICE"}


def scene_report(img, t, dur, spec, c):
    tm = c.p["teammate"]
    code, week, detail = c.claims[tm["status"]]["value"]
    k = M.ease_out(M.progress(t, 0.0, 0.35))
    W_, H_ = 860, 760
    sheet = Image.new("RGBA", (W_, H_), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sheet)
    sd.rounded_rectangle((0, 0, W_ - 1, H_ - 1), radius=18, fill=PAPER + (255,))
    sd.rectangle((0, 0, W_, 110), fill=(36, 40, 48))
    sd.text((40, 55), f"OFFICIAL INJURY REPORT · WEEK {week}", font=M.font("black", 40), fill=PAPER, anchor="lm")
    sd.text((40, 190), tm["name"].upper(), font=M.font("black", 72), fill=PAPER_INK, anchor="lm")
    sd.text((40, 262), f"RB · {c.d(c.p['team_name']).upper()}", font=M.font("bold", 36), fill=(90, 94, 102), anchor="lm")
    for y in (330, 450, 570):
        sd.line((40, y, W_ - 40, y), fill=(200, 196, 186), width=2)
    sd.text((40, 390), "PRACTICE STATUS" if code == "practice" else "STATUS", font=M.font("bold", 32),
            fill=(90, 94, 102), anchor="lm")
    asof = c.d(c.p["details"]["as_of"]).upper() if "as_of" in c.p["details"] else ""
    sd.text((40, 650), f"DATA AS OF {asof} · CHECK FINAL STATUS BEFORE KICKOFF", font=M.font("bold", 26),
            fill=(90, 94, 102), anchor="lm")
    if "as_of" in c.p["details"]:
        kd = M.ease_out(M.progress(t, c.at(claim=c.p["details"]["as_of"], default=0.4) - 0.05, 0.3))
        if kd > 0:
            f = M.font("bold", 26)
            x1 = 40 + f.getlength(f"DATA AS OF {asof}")
            sd.rectangle((36, 672, 36 + (x1 - 32) * kd, 680), fill=M.GOLD)
    sheet = sheet.rotate(-2.5, expand=True, resample=Image.BICUBIC)
    M.paste(img, sheet, M.CENTER_X, 720 + (1 - k) * 900, 1.0, 1.0)
    word = {"ir": "INJURED RESERVE", "none": "NOT ON REPORT"}.get(code) or (
        detail.upper() if code == "designation" else STATUS_WORDS.get(detail, detail.upper()))
    ks = M.back_out(M.progress(t, c.at(claim=tm["status"], default=1.0) + 0.1, 0.25))
    stamp(img, word, M.CENTER_X, 830, RED if code != "none" else M.UP, ks, size=88, angle=-7)


def scene_window(img, t, dur, spec, c):
    tm = c.p["teammate"]
    closing = spec.get("stage") == "closing"
    nxt = c.p["details"].get("next_game")
    last = lambda s: s.split()[-1].upper()
    if not closing:
        k = M.ease_out(M.progress(t, 0.0, 0.3))
        M.put_text(img, f"WHILE {last(tm['name'])} SITS", M.CENTER_X, 330, size=96, fill=M.INK, alpha=k)
        # an open window: two shutters swung wide, the starter's replacement framed in it
        open_k = M.ease_out(M.progress(t, 0.15, 0.5))
        d = ImageDraw.Draw(img, "RGBA")
        fx0, fx1, fy0, fy1 = M.CENTER_X - 260, M.CENTER_X + 260, 450, 1020
        d.rounded_rectangle((fx0, fy0, fx1, fy1), radius=20, outline=M.INK + (220,), width=10)
        sw = (fx1 - fx0) / 2 * (1 - 0.82 * open_k)
        d.rectangle((fx0, fy0, fx0 + sw, fy1), fill=(255, 255, 255, 36), outline=M.INK + (120,), width=4)
        d.rectangle((fx1 - sw, fy0, fx1, fy1), fill=(255, 255, 255, 36), outline=M.INK + (120,), width=4)
        M.paste(img, c.art(420), M.CENTER_X, 750, 0.9 + 0.1 * open_k, open_k)
        ks = M.back_out(M.progress(t, c.at(word="starter", default=dur * 0.6) - 0.05, 0.25))
        stamp(img, f"START {last(c.p['name'])}", M.CENTER_X, 1110, M.UP, ks, size=92, angle=-4)
        if nxt:
            kn = M.ease_out(M.progress(t, 0.5, 0.3))
            chip = f"NEXT: {c.d(nxt['venue']).upper()} {c.d(nxt['opponent']).upper()}"
            M.put_text(img, chip, M.CENTER_X, 1255, size=42, fill=M.GOLD, alpha=kn)
        return
    k = M.ease_out(M.progress(t, 0.0, 0.3))
    M.put_text(img, f"WHEN {last(tm['name'])} IS BACK", M.CENTER_X, 330, size=86, fill=M.INK, alpha=k)
    M.put_text(img, "THE ONLY SPLIT WE'VE SEEN WITH HIM", M.CENTER_X, 420, size=34, fill=M.MUTED, alpha=k)
    # the split as two faces and one bar: his share vs the backup's, from the weeks they played together
    car = c.p["details"]["carries"]
    his, mine = c.claims[car["tm_base_share"]]["value"], c.claims[car["own_base_share"]]["value"]
    kb = M.ease_out(M.progress(t, 0.25, 0.6))
    x0, x1, y = M.SAFE_LEFT + 20, M.SAFE_RIGHT - 20, 820
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle((x0, y - 50, x1, y + 50), radius=50, fill=(255, 255, 255, 30))
    split = x0 + (x1 - x0) * his * kb
    d.rounded_rectangle((x0, y - 50, split, y + 50), radius=50, fill=SILVER)
    d.rounded_rectangle((split, y - 50, x0 + (x1 - x0) * (his + mine) * kb, y + 50), radius=50, fill=M.GOLD)
    M.paste(img, _tm_face(c, 230), M.SAFE_LEFT + 120, 600, 1.0, k)
    M.paste(img, c.art(230), M.SAFE_RIGHT - 120, 600, 1.0, k)
    M.put_text(img, f"{last(tm['name'])} {c.d(car['tm_base_share'])}", M.SAFE_LEFT + 20, 960, size=64, fill=SILVER,
               alpha=kb, anchor="l")
    M.put_text(img, f"{last(c.p['name'])} {c.d(car['own_base_share'])}", M.SAFE_RIGHT - 190, 960, size=64,
               fill=M.GOLD, alpha=kb)
    M.put_text(img, f"OF RUNNING BACK CARRIES · {c.d(car['base_weeks']).upper()}", M.CENTER_X, 1050, size=32,
               fill=M.MUTED, alpha=kb)
    brand(img, t, dur)


FORMAT_SCENES = {
    "mirror": ("casefile", scene_mirror), "volume_balls": ("casefile", scene_volume_balls),
    "rival": ("casefile", scene_rival), "verdict": ("casefile", scene_verdict),
    "giant_number": ("alert", scene_giant_number), "carry_grid": ("alert", scene_carry_grid),
    "handoff": ("alert", scene_handoff), "report": ("alert", scene_report), "window": ("alert", scene_window),
}


def punches(scene: dict, c, dur: float) -> list[float]:
    kind = scene["type"]
    if kind == "mirror":
        a, b = _games(c, scene["games"])
        return ([0.33] if scene.get("stage") != "targets" else []) + \
            ([c.at(claim=b["targets"], default=99)] if scene.get("stage") != "collide" else [])
    if kind == "rival":
        return [c.at(word="more", default=99)]
    if kind == "verdict":
        return [c.at(word="budge", default=99)] if scene.get("stage") != "call" else [0.4]
    if kind == "giant_number":
        return [0.0]
    if kind == "handoff":
        return [c.at(word="out", default=99)]
    if kind == "report":
        return [c.at(claim=c.p["teammate"]["status"], default=99) + 0.1]
    if kind == "window":
        return [c.at(word="starter", default=99)] if scene.get("stage") != "closing" else []
    return []


def sounds(scene: dict, start: float, dur: float, c) -> list[tuple[float, str, float]]:
    kind = scene["type"]
    ev = []
    if kind == "mirror":
        a, b = _games(c, scene["games"])
        if scene.get("stage") != "targets":
            ev.append((start + 0.3, "impact", -2))
        if scene.get("stage") != "collide":
            for g in (a, b):
                at = c.at(claim=g["targets"], default=99)
                ev += [(start + at + 0.04 * i, "tick", -9) for i in range(min(8, _n(c, g["targets"])))]
            ev.append((start + c.at(claim=b["targets"], default=99) + 0.05, "stamp", -5))
    if kind == "volume_balls":
        ev += [(start + 0.15 + 0.06 * i, "tick", -12) for i in range(10)]
        ev.append((start + 1.4, "stamp", -6))
    if kind == "rival":
        ev.append((start + c.at(word="back", default=99) - 0.05, "stamp", -5))
        ev.append((start + c.at(word="more", default=99) - 0.1, "impact", -4))
    if kind == "verdict":
        if scene.get("stage") == "call":
            ev += [(start + 0.35, "stamp", -3), (start + 0.85, "stamp", -5)]
        else:
            ev.append((start + c.at(word="budge", default=99) - 0.05, "stamp", -4))
    if kind == "giant_number":
        ev.append((start, "impact", 0))
    if kind == "carry_grid":
        ev += [(start + 0.1 + 0.08 * i, "tick", -12) for i in range(12)]
    if kind == "handoff":
        ev.append((start + c.at(word="out", default=99) - 0.1, "stamp", -2))
    if kind == "report":
        ev.append((start + c.at(claim=c.p["teammate"]["status"], default=99) + 0.1, "stamp", -3))
    if kind == "window" and scene.get("stage") != "closing":
        ev.append((start + c.at(word="starter", default=99) - 0.05, "stamp", -3))
    return [e for e in ev if e[0] < start + dur + 0.5]
