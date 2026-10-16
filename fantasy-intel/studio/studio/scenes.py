"""Story components for Shorts: each one shows the thing the narrator is talking about.

  player_card   who it is and the conflict, on frame 0: name, jersey, and the premise number crashing
  target_balls  targets as footballs, one column per game, with "ghost" footballs for the volume that is gone
  split         GETTING THE BALL (target share) against ON THE FIELD (snap share as a strip of 20 plays)
  volume        the team's own targets per game, then his share
  lineup        a fantasy lineup with his slot flagged: the decision the viewer actually makes

All are generic: they read the player's facts and claims and key their motion to the spoken words
(Ctx.at). Numbers on screen are claim display strings or snapshot values through numbers.render.
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw

from . import motion as M
from .numbers import TEAM_NAMES, render

BALL = (122, 62, 29)
BALL_DARK = (78, 38, 16)


# ------------------------------------------------------------------------------------------ icons

@lru_cache(maxsize=64)
def football(w: int, ghost: bool = False) -> Image.Image:
    """An original football icon: a pointed lens shape with laces (or a dashed outline for a 'missing' one)."""
    h = int(w * 0.6)
    img = Image.new("RGBA", (w + 8, h + 8), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    a, b = w / 2, h / 2
    xs = np.linspace(-a, a, 60)
    top = [(4 + a + x, 4 + b - b * (1 - (x / a) ** 2) ** 0.75) for x in xs]
    bot = [(4 + a + x, 4 + b + b * (1 - (x / a) ** 2) ** 0.75) for x in xs[::-1]]
    pts = top + bot
    if ghost:
        for i in range(0, len(pts) - 1, 3):
            d.line([pts[i], pts[i + 1]], fill=(255, 255, 255, 90), width=max(2, w // 26))
        return img
    d.polygon(pts, fill=BALL, outline=BALL_DARK)
    d.line([(4 + a * 0.55, 4 + b * 0.55), (4 + a * 1.45, 4 + b * 0.55)], fill=(245, 245, 245), width=max(2, w // 22))
    for k in range(5):
        x = 4 + a * (0.65 + 0.175 * k)
        d.line([(x, 4 + b * 0.38), (x, 4 + b * 0.72)], fill=(245, 245, 245), width=max(2, w // 30))
    return img


def marker(d: ImageDraw.ImageDraw, cx, cy, r, color, filled: bool) -> None:
    if filled:
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    else:
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255, 60), width=3)


@lru_cache(maxsize=4)
def sweep_band(h: int) -> Image.Image:
    """A soft diagonal light band for a single glint across a card."""
    w = 220
    x = np.linspace(-1, 1, w)
    alpha = (np.exp(-(x * 2.2) ** 2) * 70).astype(np.uint8)
    band = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    band.putalpha(Image.fromarray(np.tile(alpha, (h, 1)), "L"))
    return band.rotate(-18, expand=True, resample=Image.BICUBIC)


def _card_layer(c, w: int, h: int) -> Image.Image:
    card = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    yy = np.linspace(0, 1, h)[:, None, None]
    p = np.array(c.primary, np.float32)
    grad = p * (1 - 0.65 * yy) + np.array((8, 10, 14), np.float32) * 0.65 * yy
    body = Image.fromarray(np.broadcast_to(grad, (h, w, 3)).astype(np.uint8), "RGB").convert("RGBA")
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=44, fill=255)
    card.paste(body, (0, 0), mask)
    ImageDraw.Draw(card).rounded_rectangle((2, 2, w - 3, h - 3), radius=44, outline=c.secondary + (200,), width=5)
    return card, mask


# ----------------------------------------------------------------------------------------- scenes

def scene_player_card(img, t, dur, spec, c):
    """Frame 0: the player and the stakes. The premise number crashes within half a second."""
    p = c.p
    m = p["metrics"][0]
    W, H = 900, 1020
    card, mask = _card_layer(c, W, H)
    cd = ImageDraw.Draw(card)
    first, last = p["name"].split()[0].upper(), " ".join(p["name"].split()[1:]).upper()
    cd.text((60, 70), first, font=M.font("bold", 56), fill=M.INK)
    cd.text((60, 130), last, font=M.font("black", M.fit_size(last, "black", 140, 780)), fill=M.INK)
    tag = f"{p['position']} · {c.d(p['team_name']).upper()}"
    cd.rounded_rectangle((60, 300, 60 + M.font("black", 36).getlength(tag) + 44, 360), radius=30, fill=M.INK)
    cd.text((82, 330), tag, font=M.font("black", 36), fill=M.DARK, anchor="lm")
    j = c.art(470)
    card.alpha_composite(j, (W - 500, 250))
    # premise strip
    crash = c.at(word=spec.get("crash_word") or "", default=0.45) if spec.get("crash_word") else 0.45
    k = M.back_out(M.progress(t, crash, 0.3))
    cd.rounded_rectangle((40, 760, W - 40, H - 40), radius=30, fill=(6, 8, 12, 220))
    cd.text((80, 800), m["label"].upper(), font=M.font("black", 44), fill=M.MUTED)
    before = c.d(m["baseline"])
    after = c.d(m["recent"])
    cd.text((80, 870), before, font=M.font("black", 96), fill=M.INK if k < 0.05 else M.MUTED)
    if k > 0:
        x = 80 + M.font("black", 96).getlength(before) + 40
        cd.text((x, 905), "→", font=M.font("black", 80), fill=M.INK, anchor="lm")
        big = M.text_layer(after, "black", 150, c.accent, stroke=0, shadow=False)
        s = 0.6 + 0.4 * k
        lay = big.resize((max(1, int(big.width * s)), max(1, int(big.height * s))), Image.BILINEAR)
        card.alpha_composite(lay, (int(x + 90), int(905 - lay.height / 2)))
    # one glint across the card, sparingly
    g = M.progress(t, 0.15, 0.6)
    if 0 < g < 1:
        band = sweep_band(H + 400)
        glint = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        glint.alpha_composite(band, (int(-300 + (W + 500) * g), -200))
        glint.putalpha(Image.fromarray(np.minimum(np.asarray(glint.getchannel("A")), np.asarray(mask)), "L"))
        card.alpha_composite(glint)
    whip = M.ease_out(M.progress(t, 0, 0.25))
    # On the turn of the hook ("So why did his targets dry up?") the card lifts and targets fall out of it.
    turn = next((tok["start"] for i, tok in enumerate(c.tokens) if i > 0 and c.tokens[i - 1]["text"][-1:] in ".?!"),
                dur * 0.5)
    lift = M.ease_in_out(M.progress(t, turn - 0.1, 0.4))
    M.paste(img, card, M.CENTER_X, 790 + (1 - whip) * 60 - 110 * lift, (1.10 - 0.10 * whip) * (1 - 0.08 * lift))
    if spec.get("story") == "vanishing_targets" and lift > 0:
        ball = football(96)
        for i in range(6):
            ft = M.clamp((t - turn - 0.05 - i * 0.09) / 0.9)
            if ft <= 0:
                continue
            x = 170 + i * 135 + math.sin(i * 1.7) * 30
            y = 1180 + ft ** 2 * 520 - 60 * math.sin(math.pi * ft) + 0 * i
            rot = ball.rotate((i * 47 + ft * 240) % 360, expand=True, resample=Image.BICUBIC)
            M.paste(img, rot, x, y, 1.0, M.clamp(1.6 - ft * 1.6))


def scene_target_balls(img, t, dur, spec, c):
    """Targets as footballs, game by game. After the drop-off, ghost footballs show the volume that is gone."""
    games = c.p["counts"]["games"]
    k_split = spec.get("split") or len(games)
    vals = [c.claims[g["value"]]["value"] for g in games]
    early = vals[:k_split]
    ghost_to = round(sum(early) / len(early)) if early else 0
    M.put_text(img, "TARGETS PER GAME", M.CENTER_X, 300, size=80, fill=M.INK, alpha=M.clamp(t * 6))
    n = len(games)
    col_w = (M.SAFE_RIGHT - M.SAFE_LEFT) / n
    ball_w = int(min(col_w * 0.78, 120))
    step = int(ball_w * 0.5)
    base = 1210
    for i, (g, v) in enumerate(zip(games, vals)):
        ta = c.at(g["value"], default=0.2 + i * 0.35)
        k = M.ease_out(M.progress(t, ta - 0.12, 0.3))
        if k <= 0:
            continue
        cx = M.SAFE_LEFT + col_w * (i + 0.5)
        late = i >= k_split
        if late:
            for j in range(v, max(ghost_to, v)):
                gb = football(ball_w, ghost=True)
                M.paste(img, gb, cx, base - j * step, 1.0, M.clamp(k))
        for j in range(v):
            drop = (1 - M.ease_out(M.progress(t, ta - 0.12 + j * 0.03, 0.25))) * 140
            M.paste(img, football(ball_w), cx, base - j * step - drop, 1.0, M.clamp(k * 2))
        M.put_text(img, str(c.d(g["value"])), cx, base - max(v, ghost_to if late else 0) * step - 70, size=72,
                   fill=c.accent if late else M.INK, alpha=M.clamp(k * 2))
        M.put_text(img, f"WK {g['week']}", cx, base + 70, size=34, fill=M.MUTED, alpha=M.clamp(k * 2))
    ts = c.at(word="since", default=99)
    kb = M.ease_out(M.progress(t, ts, 0.3))
    if kb > 0 and k_split < n:
        d = ImageDraw.Draw(img)
        x0 = M.SAFE_LEFT + col_w * k_split + 10
        x1 = M.SAFE_LEFT + col_w * n - 10
        y = base + 130
        d.line((x0, y, x0 + (x1 - x0) * kb, y), fill=c.accent, width=6)
        M.put_text(img, f"SINCE WK {games[k_split]['week']}", (x0 + x1) / 2, y + 50, size=38, fill=c.accent, alpha=kb)


def scene_split(img, t, dur, spec, c):
    """GETTING THE BALL vs ON THE FIELD: the share collapse against nearly unchanged playing time."""
    p = c.p
    ts_m = p["metrics"][0]
    sn = next((m for m in p["metrics"] if m["column"] == "offense_pct"), None)
    d = ImageDraw.Draw(img)
    twist = spec.get("role") == "twist"
    t_targets = 0.0 if twist else c.at(word="fewer", default=c.at(word="targets", default=0.6))
    t_snaps = c.at(sn["recent"], default=c.at(word="snaps", default=0.0)) if sn else 99
    # top: targets
    kt = M.ease_out(M.progress(t, t_targets - 0.1, 0.4))
    M.put_text(img, "GETTING THE BALL", M.SAFE_LEFT, 300, size=64, fill=c.accent, alpha=M.clamp(kt * 2 + 0.25),
               anchor="l")
    for j, (lab, cid, col) in enumerate((("BEFORE", ts_m["baseline"], (150, 160, 176)), ("NOW", ts_m["recent"], c.accent))):
        y = 400 + j * 120
        frac = c.claims[cid]["value"]
        grow = kt if j == 0 else kt * (1 - 0.0)
        M.put_text(img, lab, M.SAFE_LEFT, y + 32, size=36, fill=M.MUTED, alpha=M.clamp(kt * 2), anchor="l")
        d.rounded_rectangle((M.SAFE_LEFT + 170, y, M.SAFE_LEFT + 170 + 520, y + 64), radius=32, fill=(32, 38, 50))
        if grow > 0:
            w = max(64, 520 * frac / 0.4 * grow)          # 40% of targets = full bar
            d.rounded_rectangle((M.SAFE_LEFT + 170, y, M.SAFE_LEFT + 170 + min(w, 520), y + 64), radius=32, fill=col)
        M.put_text(img, c.d(cid), M.SAFE_LEFT + 740, y + 34, size=76, fill=M.INK, alpha=M.clamp(kt * 2), anchor="l")
    # bottom: playing time as 20 plays
    if sn:
        ks = M.ease_out(M.progress(t, t_snaps - 0.15, 0.5))
        M.put_text(img, "ON THE FIELD", M.SAFE_LEFT, 760, size=64, fill=M.INK, alpha=M.clamp(ks * 2 + 0.25), anchor="l")
        M.put_text(img, "EVERY DOT = 5% OF SNAPS", M.SAFE_LEFT, 830, size=30, fill=M.MUTED, alpha=M.clamp(ks * 2),
                   anchor="l")
        for j, (lab, cid) in enumerate((("BEFORE", sn["baseline"]), ("NOW", sn["recent"]))):
            y = 920 + j * 150
            filled = int(round(c.claims[cid]["value"] * 20))
            M.put_text(img, lab, M.SAFE_LEFT, y, size=36, fill=M.MUTED, alpha=M.clamp(ks * 2), anchor="l")
            for i in range(20):
                cx = M.SAFE_LEFT + 30 + (i % 10) * 60 + 170
                cy = y - 26 + (i // 10) * 60
                on = i < filled and ks * 20 > i
                marker(d, cx, cy, 22, M.INK if j == 0 else c.primary_vivid, on)
            M.put_text(img, c.d(cid), M.SAFE_LEFT + 800, y + 4, size=70, fill=M.INK, alpha=M.clamp(ks * 2), anchor="l")
        tsame = c.at(word=spec.get("stamp_word", "same"), default=99)
        kk = M.back_out(M.progress(t, tsame - 0.05, 0.3))
        if kk > 0:
            stamp = M.text_layer(spec.get("stamp", "ABOUT THE SAME"), "black", 84, M.GOLD, stroke=8)
            M.paste(img, stamp.rotate(4, expand=True, resample=Image.BICUBIC), M.CENTER_X, 1290, 1.4 - 0.4 * kk,
                    M.clamp(kk * 2))


def scene_presence(img, t, dur, spec, c):
    """Playing time, full screen: every dot is 5% of the snaps; the 'now' row fills as the number is said."""
    sn = next((m for m in c.p["metrics"] if m["column"] == "offense_pct"), None)
    d = ImageDraw.Draw(img)
    M.put_text(img, "ON THE FIELD", M.CENTER_X, 320, size=110, fill=M.INK, alpha=M.clamp(t * 6))
    M.put_text(img, "EVERY DOT = 5% OF HIS TEAM'S SNAPS", M.CENTER_X, 420, size=36, fill=M.MUTED, alpha=M.clamp(t * 6))
    for j, (lab, cid, col) in enumerate((("BEFORE", sn["baseline"], M.INK), ("NOW", sn["recent"], c.accent))):
        ta = c.at(cid, default=0.3 + j * 0.8)
        k = M.ease_out(M.progress(t, ta - 0.2, 0.6))
        y = 560 + j * 380
        M.put_text(img, lab, M.SAFE_LEFT, y, size=48, fill=M.MUTED, alpha=M.clamp(k * 3), anchor="l")
        M.put_text(img, c.d(cid), M.SAFE_LEFT + 260, y, size=96, fill=col, alpha=M.clamp(k * 3), anchor="l")
        filled = int(round(c.claims[cid]["value"] * 20))
        for i in range(20):
            cx = M.SAFE_LEFT + 45 + (i % 10) * 84
            cy = y + 110 + (i // 10) * 88
            marker(d, cx, cy, 32, col, i < filled and k * 20 > i)


def scene_volume(img, t, dur, spec, c):
    """The team's own targets per game (so a low-volume week is visible), then his share."""
    games = c.p["counts"]["games"]
    team = [g for g in games if g.get("team_targets")]
    M.put_text(img, f"{c.d(c.p['team_name']).upper()} TARGETS PER GAME", M.CENTER_X, 300, size=64, fill=M.INK,
               alpha=M.clamp(t * 6), max_width=880)
    vals = [c.claims[g["team_targets"]]["value"] for g in team]
    top = max(vals + [1])
    d = ImageDraw.Draw(img)
    n = len(team)
    gap = 26
    bw = (M.SAFE_RIGHT - M.SAFE_LEFT - gap * (n - 1)) / n
    base, hmax = 980, 520
    t_low = c.at(team[-1]["team_targets"], default=0.5)
    for i, (g, v) in enumerate(zip(team, vals)):
        k = M.ease_out(M.progress(t, 0.05 + i * 0.07, 0.3))
        h = hmax * v / top * k
        x = M.SAFE_LEFT + i * (bw + gap)
        lit = i == n - 1 and t >= t_low - 0.05
        d.rounded_rectangle((x, base - h, x + bw, base), radius=14, fill=M.GOLD if lit else (70, 80, 96))
        M.put_text(img, c.d(g["team_targets"]), x + bw / 2, base - h - 46, size=60 if lit else 46,
                   fill=M.GOLD if lit else M.INK, alpha=M.clamp(k * 2))
        M.put_text(img, f"WK {g['week']}", x + bw / 2, base + 44, size=32, fill=M.MUTED, alpha=M.clamp(k * 2))
    m = c.p["metrics"][0]
    t_share = c.at(m["baseline"], default=dur * 0.6)
    ks = M.back_out(M.progress(t, t_share - 0.1, 0.3))
    if ks > 0:
        panel = Image.new("RGBA", (880, 230), (0, 0, 0, 0))
        pd = ImageDraw.Draw(panel)
        pd.rounded_rectangle((0, 0, 879, 229), radius=34, fill=(8, 10, 14, 235), outline=c.accent + (255,), width=5)
        pd.text((40, 60), "BUT HIS SHARE STILL FELL", font=M.font("black", 40), fill=M.MUTED, anchor="lm")
        pd.text((40, 150), f"{c.d(m['baseline'])} → {c.d(m['recent'])}", font=M.font("black", 110), fill=c.accent,
                anchor="lm")
        M.paste(img, panel, M.CENTER_X, 1210, 0.7 + 0.3 * ks, M.clamp(ks * 2))


def scene_lineup(img, t, dur, spec, c):
    """A fantasy lineup with his slot flagged: the decision, and the one number to watch."""
    p = c.p
    m = p["metrics"][0]
    slots = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX"]
    mine = slots.index(p["position"]) if p["position"] in slots else 6
    week = c.d(c.facts["episode"]["week"])
    M.put_text(img, f"YOUR LINEUP · {week.upper()}", M.SAFE_LEFT, 290, size=50, fill=M.MUTED,
               alpha=M.clamp(t * 6), anchor="l")
    d = ImageDraw.Draw(img)
    sched = next((s for s in p["schedule"] if s["week"] == c.facts["week"]), None)
    y = 370
    for i, slot in enumerate(slots):
        k = M.ease_out(M.progress(t, 0.04 * i, 0.25))
        hot = i == mine
        h = 170 if hot else 92
        x = M.SAFE_LEFT + (1 - k) * 120
        d.rounded_rectangle((x, y, x + 860, y + h), radius=26, fill=(28, 34, 44) if not hot else (14, 18, 24),
                            outline=c.accent if hot else (50, 58, 72), width=6 if hot else 2)
        d.text((x + 34, y + h / 2), slot, font=M.font("black", 38), fill=M.MUTED if not hot else c.accent, anchor="lm")
        if not hot:
            d.rounded_rectangle((x + 170, y + h / 2 - 12, x + 520, y + h / 2 + 12), radius=12, fill=(44, 52, 66))
            y += h + 18
            continue
        name = p["name"].upper()
        d.text((x + 170, y + 46), name, font=M.font("black", M.fit_size(name, "black", 50, 640)), fill=M.INK, anchor="lm")
        opp = ""
        if sched and sched["opponent_value"] != "bye":
            opp = f" · {'VS' if sched.get('home') else 'AT'} {TEAM_NAMES[sched['opponent_value']].upper()}"
        d.text((x + 170, y + 92), f"{p['team']}{opp}", font=M.font("bold", 30), fill=M.MUTED, anchor="lm")
        pulse = 0.5 + 0.5 * math.sin(t * 5)
        badge = f"{m['label'].upper()}: {c.d(m['recent'])}"
        bw = M.font("black", 32).getlength(badge) + 44
        d.rounded_rectangle((x + 170, y + 116, x + 170 + bw, y + 158), radius=21,
                            fill=M.mix_color(c.accent, (255, 255, 255), 0.15 * pulse))
        d.text((x + 170 + bw / 2, y + 137), badge, font=M.font("black", 32), fill=M.DARK, anchor="mm")
        y += h + 18
    tw = c.at(word="watch", default=dur * 0.35 if spec.get("watch") else dur * 0.5)
    kw = M.back_out(M.progress(t, tw - 0.05, 0.3))
    if kw > 0:
        label = spec.get("watch") or f"WATCH: BACK TOWARD {c.d(m['baseline'])}?"
        panel = M.text_layer(label, "black", M.fit_size(label, "black", 64, 860), M.GOLD, stroke=6)
        M.paste(img, panel, M.CENTER_X, 1330, 0.7 + 0.3 * kw, M.clamp(kw * 2))


STORY_SCENES = {"presence": ("field", scene_presence), "player_card": ("field", scene_player_card), "target_balls": ("studio", scene_target_balls),
                "split": ("split", scene_split), "volume": ("team", scene_volume), "lineup": ("studio", scene_lineup)}


def punches(scene: dict, c, dur: float) -> list[float]:
    """Local times where the camera punches in slightly: the moments of revelation."""
    kind = scene["type"]
    if kind == "player_card":
        return [0.45]
    if kind == "target_balls":
        k = scene.get("split") or 0
        games = c.p["counts"]["games"]
        return [c.at(games[k]["value"], default=99)] if k < len(games) else []
    if kind == "split":
        return [c.at(word="same", default=99)]
    if kind == "volume":
        return [c.at(c.p["metrics"][0]["baseline"], default=99)]
    if kind == "lineup":
        return [c.at(word="watch", default=99)]
    return []


def sounds(scene: dict, start: float, dur: float, c) -> list[tuple[float, str, float]]:
    kind = scene["type"]
    ev = []
    if kind == "player_card":
        ev += [(start + 0.42, "impact", -1)]
    if kind == "target_balls":
        for g in c.p["counts"]["games"]:
            ev.append((start + c.at(g["value"], default=99) - 0.1, "tick", -6))
    if kind == "split":
        ev.append((start + c.at(word="same", default=99) - 0.05, "impact", -4))
    if kind == "volume":
        ev.append((start + c.at(c.p["metrics"][0]["baseline"], default=99) - 0.1, "impact", -4))
    if kind == "lineup":
        ev.append((start + c.at(word="watch", default=99) - 0.05, "tick", -2))
    return [e for e in ev if e[0] < start + dur + 0.5]
