"""Styles A (premium analytics) and C (broadcast): one component library, two looks.

Components (player header, column chart, slope chart, before/after meter, comparison track, lineup strip,
option buttons, source line, captions) take a Look, which carries a style's design tokens and the few things
that differ structurally: panel shape (rounded card vs. slanted slab), type case and italics, and the
persistent chrome (a quiet header for premium; a score bug and lower third for broadcast).

Scenes are compositions of components keyed to the narration (Ctx.at), and every number comes from a claim
(c.d) or a snapshot value through numbers.render; each is logged with its source for the visual QA.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .. import format_scenes as FS
from .. import motion as M
from ..numbers import TEAM_NAMES, render
from ..stages.story import last as last_name
from . import kit as K
from .kit import H, SAFE, W, Frame, clamp, mix, pal


# ------------------------------------------------------------------------------------------- look

class Look:
    def __init__(self, name: str):
        self.name = name
        self.tk = K.tokens(name)
        tk = self.tk
        self.bg, self.ink, self.muted, self.faint = pal(tk, "bg"), pal(tk, "ink"), pal(tk, "ink_muted"), pal(tk, "ink_faint")
        self.panel, self.line, self.grid = pal(tk, "panel"), pal(tk, "panel_line"), pal(tk, "grid")
        self.up, self.down, self.hi, self.neutral = pal(tk, "up"), pal(tk, "down"), pal(tk, "highlight"), pal(tk, "neutral_bar")
        self.mx = tk.space.margin_x                  # left margin; the right one clears the platform's side rail
        self.mr = W - SAFE[2] + tk.space.margin_right
        self.mr_width = W - self.mx - self.mr
        self.top, self.bottom = tk.space.content_top, tk.space.content_bottom
        self.broadcast = name == "broadcast"
        self.shear = getattr(tk.type, "italic", 0.0)
        self.skew = getattr(tk.radius, "skew", 0.0)

    def ty(self, key: str):
        return getattr(self.tk.type, key)

    def case(self, text: str, c=None) -> str:
        if self.tk.type.case == "upper":
            return text.upper()
        return sentence(text, c)

    def e(self, t, start, dur=None, kind="enter"):
        return K.ease(self.tk, kind, t, start, dur)

    def data(self, c):
        """The data colour of the story: down for a faller, up otherwise."""
        return self.down if c.p.get("role") == "faller" else self.up

    def team(self, c):
        """The team's colours, the primary darkened just enough (if at all) that type on it reaches 4.5:1."""
        p, s = M.team_colors(c.team)
        return readable(p), s

    # panels -----------------------------------------------------------------------------------
    def panel_box(self, F: Frame, box, fill=None, alpha=1.0, outline=True, team=None):
        x0, y0, x1, y1 = box
        if self.broadcast:
            k = self.skew * (y1 - y0)
            F.poly([(x0 + k, y0), (x1, y0), (x1 - k, y1), (x0, y1)], fill or self.panel, alpha)
            if team is not None:
                F.poly([(x0 + k, y0), (x0 + k + 14, y0), (x0 + 14, y1), (x0, y1)], team, alpha)
        else:
            F.rect(box, fill or self.panel, self.line if outline else None, 2 if outline else 0,
                   self.tk.radius.panel, alpha)

    def bar(self, F: Frame, box, fill, alpha=1.0):
        F.rect(box, fill, radius=self.tk.radius.bar, alpha=alpha)

    def text(self, F, text, x, y, key, fill, anchor="l", alpha=1.0, scale=1.0, src=None, role="label",
             max_width=None, c=None, display=False, tabular=False):
        t = self.case(text, c) if role in ("title", "kicker", "label", "body") else text
        if role == "kicker" or (self.broadcast and role in ("label",)):
            t = t.upper()
        sh = self.shear if (display and self.broadcast) else 0.0
        if max_width and sh:
            max_width -= sh * self.ty(key)[1] * 0.75           # the lean of italic type needs room on the right
        return F.text(t, x, y, self.ty(key), fill, anchor, alpha, scale, src, role, sh, tabular, max_width)


def readable(col) -> tuple:
    """`col`, or the nearest darker shade on which white or near-black type has at least 4.5:1 contrast."""
    out = tuple(col)
    for k in range(0, 21):
        out = mix(col, (10, 15, 28), k * 0.05)
        if max(K.contrast((255, 255, 255), out), K.contrast((10, 15, 28), out)) >= 4.5:
            break
    return out


STYLES = {"premium": None, "broadcast": None}


def look(name: str) -> Look:
    if STYLES.get(name) is None:
        STYLES[name] = Look(name)
    return STYLES[name]


def sentence(text: str, c=None) -> str:
    """Sentence case for text written in capitals, with names, teams and positions kept capitalised."""
    if text != text.upper():
        return text
    keep = {"nfl": "NFL", "qb": "QB", "rb": "RB", "wr": "WR", "te": "TE", "flex": "FLEX", "week": "Week",
            "wk": "Wk", "wks": "Wks"}
    if c is not None:
        names = [c.p["name"]] + [g["name"] for g in c.p.get("gainers") or []]
        if c.p.get("teammate"):
            names.append(c.p["teammate"]["name"])
        for n in names:
            for part in n.split():
                keep[part.lower().strip(".")] = part.strip(".")
        for v in TEAM_NAMES.values():
            keep[v.lower()] = v
    out = []
    for i, w in enumerate(text.lower().split(" ")):
        core = re.sub(r"[^a-z'.]", "", w).strip(".")
        if core in keep:
            w = w.replace(core, keep[core], 1)
        elif i == 0 or (out and out[-1].endswith((".", "?", "!"))):
            w = w[:1].upper() + w[1:]
        out.append(w)
    return " ".join(out)


# -------------------------------------------------------------------------------------- backgrounds

@lru_cache(maxsize=16)
def backdrop(name: str, team: str) -> Image.Image:
    L = look(name)
    yy = np.linspace(0, 1, H)[:, None, None]
    top, bot = np.array(pal(L.tk, "bg_top"), np.float32), np.array(L.bg, np.float32)
    base = top * (1 - yy) + bot * yy
    base = np.broadcast_to(base, (H, W, 3)).copy()
    if L.broadcast:
        p, s = M.team_colors(team)
        xx = np.linspace(0, 1, W)[None, :]
        # two broad diagonal team-colour bands, faint, and a fine horizontal scan texture
        band = ((xx * 0.8 + np.linspace(0, 1, H)[:, None] * 0.45) % 0.62 < 0.2).astype(np.float32)
        base += band[..., None] * np.array(p, np.float32) * 0.12
        base[::4] *= 0.97
    else:
        # one soft key light from the upper left: depth without a glow
        yv, xv = np.mgrid[0:H, 0:W].astype(np.float32)
        r = np.sqrt(((xv - W * 0.2) / W) ** 2 + ((yv - H * 0.18) / H) ** 2)
        base += (np.clip(1 - r * 1.6, 0, 1) ** 2)[..., None] * 9
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")
    if L.broadcast:
        d = ImageDraw.Draw(img, "RGBA")
        for y in range(380, H, 210):              # yard-line hints, very faint
            d.line((0, y, W, y), fill=(255, 255, 255, 10), width=3)
    return img


# --------------------------------------------------------------------------------------- headshots

@lru_cache(maxsize=16)
def cutout(path: str, width: int, fade: float = 0.22) -> Image.Image:
    """The player's cut-out photo scaled to `width`, fading out over the bottom `fade` of its height."""
    src = Image.open(path).convert("RGBA")
    h = int(src.height * width / src.width)
    im = src.resize((width, h), Image.LANCZOS)
    a = np.asarray(im.getchannel("A"), np.float32)
    ramp = np.clip((1 - np.linspace(0, 1, h)) / fade, 0, 1)[:, None]
    im.putalpha(Image.fromarray((a * ramp).astype(np.uint8), "L"))
    return im


def portrait(c, width: int) -> Image.Image:
    if c.headshot:
        return cutout(str(c.headshot), width)
    j = M.jersey(c.team, str(c.p.get("jersey") or ""), c.p["name"].split()[-1], width)
    return j


# ----------------------------------------------------------------------------------------- chrome

def chrome(F: Frame, L: Look, c, t: float, scene: dict, dur: float):
    """Persistent identity: premium, a quiet header line; broadcast, a score bug and a lower third."""
    week = c.d(c.facts["episode"]["week"])
    show = SHOW.get(FS.FORMAT_SCENES.get(scene["type"], ("usage",))[0], SHOW["usage"])
    if not L.broadcast:
        y = 248
        L.text(F, show[0], L.mx, y, "kicker", L.muted, role="kicker")
        L.text(F, week, W - L.mr, y, "kicker", L.muted, anchor="r", role="kicker", src=c.facts["episode"]["week"])
        F.line((L.mx, y + 40, W - L.mr, y + 40), L.line, 2)
        return
    p, s = L.team(c)
    # score bug, top left
    y0, y1 = 240, 292
    F.poly([(L.mx, y0), (L.mx + 330, y0), (L.mx + 330 - 11, y1), (L.mx - 11, y1)], pal(L.tk, "slab_dark"))
    F.poly([(L.mx + 330, y0), (L.mx + 470, y0), (L.mx + 470 - 11, y1), (L.mx + 330 - 11, y1)], p)
    F.text(show[1], L.mx + 22, y0 + 13, ["black", 30, 0.06], L.ink, role="kicker")
    F.text(week.upper(), L.mx + 400 - 6, y0 + 13, ["black", 30, 0.04], _on(p), anchor="c", role="kicker",
           src=c.facts["episode"]["week"])
    if scene["type"] not in LOWER_THIRD:
        return
    # lower third: name on a team slab, position and team on the secondary colour
    k = L.e(t, 0.0, 0.3)
    y = 1316
    name = c.p["name"].upper()
    sub = f"{c.p['position']} · {c.d(c.p['team_name']).upper()}"
    sw = K.measure(sub, "bold", 30, 0.04) + 40
    npx = 50                                     # a long name steps down so the strip stays inside the safe zone
    while npx > 40 and K.measure(name, "black", npx, -0.01) + 70 + 8 + sw + 16 > W - L.mr - L.mx:
        npx -= 2
    nw = K.measure(name, "black", npx, -0.01) + 70
    x0 = L.mx - (1 - k) * 500
    F.poly([(x0 + 16, y), (x0 + nw + 16, y), (x0 + nw, y + 74), (x0, y + 74)], p, alpha=0.97)
    F.text(name, x0 + 32, y + 16 + (50 - npx) * 0.36, ["black", npx, -0.01], _on(p), shear=L.shear, role="label",
           alpha=k)
    xs = x0 + nw + 8
    F.poly([(xs + 16, y + 12), (xs + sw + 16, y + 12), (xs + sw, y + 74), (xs, y + 74)], s if K.contrast(s, p) > 1.5 else L.ink)
    F.text(sub, xs + 24, y + 31, ["bold", 30, 0.04], _on(s if K.contrast(s, p) > 1.5 else L.ink), role="label", alpha=k)


LOWER_THIRD = {"target_balls", "split", "presence", "teammate_out", "status_stamp", "zoom_out", "volume_balls"}
# The show's name in the chrome, by format: the usage report, or the format scenes' own (format_scenes.py).
SHOW = {"usage": ("THE USAGE REPORT", "USAGE REPORT"), "casefile": ("THE CASE FILE", "CASE FILE"),
        "alert": ("OPPORTUNITY WATCH", "OPPORTUNITY")}


def _on(bg) -> tuple:
    """Ink for text on a coloured slab: whichever of white or near-black reads better."""
    return (255, 255, 255) if K.contrast((255, 255, 255), bg) >= K.contrast((10, 15, 28), bg) else (10, 15, 28)


def source_line(F: Frame, L: Look, c, alpha: float, y: int = 1318):
    weeks = sorted({s["week"] for m in c.p["metrics"] for s in m["series"]})
    txt = f"Data: nflverse play-by-play and snap counts, {K.weeks_label(weeks)}"
    F.text(txt if not L.broadcast else txt.upper(), L.mx, y, ["regular" if not L.broadcast else "bold", 26, 0.02],
           L.faint if not L.broadcast else L.muted, alpha=alpha, role="source", src="data:weeks")


def title_block(F: Frame, L: Look, c, t: float, kicker: str | None, title: str, y: int = 320, kick_src=None):
    k = L.e(t, 0.0)
    off = (1 - k) * 24
    if kicker:
        L.text(F, kicker, L.mx, y + off, "kicker", L.hi if L.broadcast else L.muted, alpha=k, role="kicker", c=c,
               src=kick_src)
        y += 52
    L.text(F, title, L.mx, y + off, "title", L.ink, alpha=k, role="title", c=c, display=True,
           max_width=L.mr_width)


# ------------------------------------------------------------------------------------------ charts

def column_chart(F: Frame, L: Look, c, t: float, cols: list[dict], box, top: float, step: float,
                 dim: float = 0.0):
    """Columns on a zero-based axis. cols: {label, value, display, src, color, at (reveal time), lit}.
    Values are drawn above their columns; the axis has `step` gridlines labelled faintly."""
    x0, y0, x1, y1 = box
    n = len(cols)
    pitch = (x1 - x0) / n
    bw = pitch * (1 - L.tk.chart.bar_gap)
    a_dim = 1 - 0.5 * dim
    if L.tk.chart.gridlines and not L.broadcast:
        v = step
        while v <= top + 1e-9:
            y = y1 - (y1 - y0) * v / top
            F.line((x0, y, x1, y), L.grid, 2, alpha=a_dim)
            v += step
    F.line((x0, y1, x1, y1), L.line if not L.broadcast else L.ink, 3, alpha=a_dim)
    for i, col in enumerate(cols):
        k = L.e(t, col["at"], 0.42)
        if k <= 0:
            continue
        val = max(col["value"] or 0, 0)
        h = (y1 - y0) * val / top * k
        cx = x0 + pitch * (i + 0.5)
        color = col["color"]
        if L.broadcast:
            sk = min(10, h * 0.05)
            F.poly([(cx - bw / 2 + sk, y1 - h), (cx + bw / 2 + sk, y1 - h), (cx + bw / 2, y1), (cx - bw / 2, y1)],
                   color, alpha=a_dim)
        else:
            L.bar(F, (cx - bw / 2, y1 - h, cx + bw / 2, y1), color, alpha=a_dim)
        key = "number_m" if col.get("lit") else "number_s"
        vy = y1 - h - int(L.ty(key)[1] * 0.73) - 26
        L.text(F, col["display"], cx, vy, key, col.get("text", L.ink), anchor="c", alpha=K.clamp(k * 1.6) * a_dim,
               src=col["src"], role="number", tabular=True)
        L.text(F, col["label"], cx, y1 + 22, "label", L.muted, anchor="c",
               alpha=K.clamp(k * 2) * a_dim, src=col.get("label_src", "week"), role="axis")


def track(F: Frame, L: Look, x0, x1, y, top: float, marks: list[dict], t: float, h: int = 18):
    """A horizontal 0..top track with filled values or markers. marks: {value, color, kind fill|marker, at,
    label, src}."""
    F.rect((x0, y, x1, y + h), L.neutral if not L.broadcast else pal(L.tk, "grid"), radius=L.tk.radius.bar)
    for m in marks:
        k = L.e(t, m["at"], 0.6)
        if k <= 0:
            continue
        xv = x0 + (x1 - x0) * clamp(m["value"] / top) * (k if m.get("kind", "fill") == "fill" else 1)
        if m.get("kind", "fill") == "fill":
            F.rect((x0, y, xv, y + h), m["color"], radius=L.tk.radius.bar)
        else:
            F.rect((xv - 3, y - 18, xv + 3, y + h + 18), m["color"], alpha=k)
        if m.get("label"):
            half = K.measure(m["label"], *L.ty("number_s")[:2]) / 2
            above = m.get("kind", "fill") != "fill"
            ly = y - 30 - int(L.ty("number_s")[1] * 0.73) - 24 if above else y + h + 30
            L.text(F, m["label"], min(max(xv, x0 + half), x1 - half), ly, "number_s", m["color"], anchor="c",
                   alpha=k, src=m.get("src"), role="number")


# ------------------------------------------------------------------------------------------ scenes

def _turn(c, dur):
    return next((tok["start"] for i, tok in enumerate(c.tokens) if i > 0 and c.tokens[i - 1]["text"][-1:] in ".?!"),
                dur * 0.5)


def scene_player_card(F, L, c, t, dur, spec):
    p = c.p
    m = p["metrics"][0]
    k0 = L.e(t + 0.25, 0.0)                      # already settling on frame 0
    first, last = p["name"].split()[0], " ".join(p["name"].split()[1:])
    prim, sec = L.team(c)
    # portrait on a flat team-colour plate (premium) or a slanted team slab (broadcast)
    if L.broadcast:
        F.poly([(470, 330), (1080, 330), (1080, 930), (330, 930)], prim, alpha=0.95)
        F.poly([(450, 330), (480, 330), (340, 930), (310, 930)], sec, alpha=0.9)
    else:
        plate = mix(prim, L.bg, 0.55) if K.rel_lum(prim) > 0.02 else mix((60, 70, 86), L.bg, 0.4)
        F.rect((560, 380, W - L.mr + 40, 900), plate, radius=L.tk.radius.panel)
    img = portrait(c, 560 if L.broadcast else 500)
    lift = L.e(t, _turn(c, dur) - 0.1, 0.5, "move")
    F.layer(img, (W - img.width + 40 if L.broadcast else 515), 930 - img.height + 10 * (1 - k0), alpha=K.clamp(k0 * 1.5))
    # name and identity
    y = 360
    if L.broadcast:
        L.text(F, first.upper(), L.mx, y, "title", L.ink, alpha=k0, role="title", display=True)
        L.text(F, last.upper(), L.mx, y + 92, "display", L.ink, alpha=k0, role="title", display=True,
               max_width=520)
    else:
        L.text(F, first, L.mx, y, "title", L.muted, alpha=k0, role="title", c=c)
        L.text(F, last, L.mx, y + 86, "display", L.ink, alpha=k0, role="title", max_width=440, c=c)
    F.rect((L.mx, y + 250, L.mx + 56, y + 258), prim if K.rel_lum(prim) > 0.03 else L.muted, alpha=k0)
    L.text(F, f"{p['position']} · {c.d(p['team_name'])}", L.mx, y + 282, "label", L.muted, alpha=k0, role="label",
           src=p["team_name"], c=c)
    # the premise: before -> now, landing as it is said
    crash = c.at(word=spec.get("crash_word") or "", default=0.45) if spec.get("crash_word") else 0.45
    kb = L.e(t + 0.25, 0.1)
    ka = L.e(t, crash)
    y = 980
    if L.broadcast:
        L.panel_box(F, (L.mx, y - 30, W - L.mr, y + 300), pal(L.tk, "slab_dark"), 0.96, team=prim)
    else:
        F.line((L.mx, y - 20, W - L.mr, y - 20), L.line, 2)
    L.text(F, m["label"].upper(), L.mx + (40 if L.broadcast else 0), y, "kicker", L.muted, role="kicker",
           alpha=kb)
    xb, xa = L.mx + (40 if L.broadcast else 0), 560
    L.text(F, c.d(m["baseline"]), xb, y + 56, "number_l", mix(L.ink, L.muted, ka), alpha=kb, src=m["baseline"],
           role="number", tabular=True, display=True, max_width=xa - 140 - xb)
    L.text(F, K.weeks_label(K.claim_weeks(c, m["baseline"])), xb, y + 220, "label", L.faint if not L.broadcast else L.muted,
           alpha=kb, role="axis", src="claim-weeks")
    if ka > 0:
        F.line((xa - 120, y + 120, xa - 50, y + 120), L.muted, 4, alpha=ka)
        F.poly([(xa - 50, y + 108), (xa - 30, y + 120), (xa - 50, y + 132)], L.muted, alpha=ka)
        L.text(F, c.d(m["recent"]), xa, y + 56 + (1 - ka) * 30, "number_l", L.data(c), alpha=K.clamp(ka * 1.4),
               src=m["recent"], role="number", tabular=True, display=True,
               max_width=W - L.mr - xa)
        L.text(F, K.weeks_label(K.claim_weeks(c, m["recent"])), xa, y + 220, "label",
               L.faint if not L.broadcast else L.muted, alpha=ka, role="axis", src="claim-weeks")
    kc = L.e(t, _turn(c, dur) - 0.05, 0.4)
    if kc > 0 and m.get("change"):
        txt = c.d(m["change"])
        wch = K.measure(txt, *L.ty("number_s")[:2]) + 40
        x1 = W - L.mr - (40 if L.broadcast else 0)
        L.panel_box(F, (x1 - wch, y - 14, x1, y + 46), L.data(c), alpha=kc, outline=False)
        L.text(F, txt, x1 - wch / 2, y, "number_s", _on(L.data(c)), anchor="c", alpha=kc, src=m["change"],
               role="number")


def scene_target_balls(F, L, c, t, dur, spec):
    games = c.p["counts"]["games"]
    k_split = spec.get("split") or len(games)
    vals = [c.claims[g["value"]]["value"] for g in games]
    title_block(F, L, c, t, f"{c.p['name']} · targets", "Targets per game")
    top = K.nice_max(max(vals + [1]), 2 if max(vals) <= 10 else 5)
    box = (L.mx + 20, 560, W - L.mr - 20, 1150)
    data = L.data(c)
    cols = []
    for i, (g, v) in enumerate(zip(games, vals)):
        late = i >= k_split
        at = c.at(g["value"], default=0.2 + i * 0.35) - 0.12
        cols.append({"label": f"Wk {g['week']}", "value": v, "display": c.d(g["value"]), "src": g["value"],
                     "color": data if late else L.neutral, "at": at, "lit": late, "text": data if late else L.ink})
    column_chart(F, L, c, t, cols, box, top, 2 if top <= 10 else 5)
    early = vals[:k_split]
    if early and k_split < len(games):
        pace = sum(early) / len(early)
        y = box[3] - (box[3] - box[1]) * pace / top
        kp = L.e(t, c.at(games[k_split]["value"], default=1.0) - 0.1, 0.5)
        pitch = (box[2] - box[0]) / len(games)
        F.dashed(box[0], box[0] + (box[2] - box[0]) * kp, y, L.muted, 3, alpha=0.9)
        L.text(F, f"{K.weeks_label([g['week'] for g in games[:k_split]])} pace", box[2], y - 44, "label",
               L.muted, anchor="r", alpha=kp, role="axis", src="derived:claims-mean", c=c)
        ks = L.e(t, c.at(word="since", default=99), 0.4)
        if ks > 0:
            xs, xe = box[0] + pitch * k_split + 8, box[2] - 8
            yb = box[3] + 92
            F.line((xs, yb, xs + (xe - xs) * ks, yb), data, 5)
            L.text(F, f"Since Wk {games[k_split]['week']}", (xs + xe) / 2, yb + 22, "label", data, anchor="c",
                   alpha=ks, role="label", src="week", c=c)


def scene_split(F, L, c, t, dur, spec):
    """A slope chart: target share against snap share, before and now, on one shared 0-based axis."""
    p = c.p
    ts_m = p["metrics"][0]
    sn = next((m for m in p["metrics"] if m["column"] == "offense_pct"), None)
    twist = spec.get("role") == "twist"
    title_block(F, L, c, t, "Before vs now", "Same field time. Fewer targets." if spec.get("stamp_word", "same") == "same"
                else "On the field more. Fewer targets.")
    rows = [(ts_m, L.data(c), 0.0 if twist else c.at(word="fewer", default=0.4))]
    if sn:
        rows.append((sn, L.ink, c.at(sn["recent"], default=c.at(word="snaps", default=0.6))))
    vals = [c.claims[m[k]]["value"] for m, _, _ in rows for k in ("baseline", "recent")]
    top = K.nice_max(max(vals), 0.2)
    lab_w = max(K.measure(L.case(m["label"], c).upper() if L.broadcast else L.case(m["label"], c), *L.ty("label")[:2],
                          L.ty("label")[2]) for m, _, _ in rows)
    xl, xr = L.mx + 150, W - L.mr - max(lab_w, 170) - 40
    y0, y1 = 600, 1160
    for v in np.arange(0, top + 1e-9, 0.2):
        y = y1 - (y1 - y0) * v / top
        F.line((xl - 40, y, xr + 40, y), L.grid if not L.broadcast else pal(L.tk, "grid"), 2)
    L.text(F, "0%", xl - 60, y1 - 16, "label", L.faint, anchor="r", role="axis", src="axis")
    for x, cid in ((xl, ts_m["baseline"]), (xr, ts_m["recent"])):
        L.text(F, K.weeks_label(K.claim_weeks(c, cid)), x, y0 - 70, "label", L.muted, anchor="c", role="axis",
               src="claim-weeks")
    for m, color, at in rows:
        k = L.e(t, at - 0.1, 0.7, "move")
        if k <= 0:
            continue
        b, r = c.claims[m["baseline"]]["value"], c.claims[m["recent"]]["value"]
        yb, yr = y1 - (y1 - y0) * b / top, y1 - (y1 - y0) * r / top
        xe, ye = xl + (xr - xl) * k, yb + (yr - yb) * k
        F.line((xl, yb, xe, ye), color, 8 if not L.broadcast else 10)
        F.d.ellipse((xl - 12, yb - 12, xl + 12, yb + 12), fill=color)
        L.text(F, c.d(m["baseline"]), xl - 30, yb - 70, "number_s", color, anchor="r", alpha=K.clamp(k * 3),
               src=m["baseline"], role="number")
        if k > 0.95:
            F.d.ellipse((xr - 12, yr - 12, xr + 12, yr + 12), fill=color)
            nb = L.text(F, c.d(m["recent"]), xr + 30, yr - 30, "number_m", color, alpha=L.e(t, at + 0.5, 0.3),
                        src=m["recent"], role="number", tabular=True)
            L.text(F, m["label"], xr + 30, nb[3] + 8, "label", L.muted,
                   alpha=L.e(t, at + 0.5, 0.3), role="label", c=c)
    snap_y = None
    if sn:
        b, r = c.claims[sn["baseline"]]["value"], c.claims[sn["recent"]]["value"]
        snap_y = min(y1 - (y1 - y0) * b / top, y1 - (y1 - y0) * r / top)
        ts = c.at(word=spec.get("stamp_word", "same"), default=99)
        ks = L.e(t, ts - 0.05, 0.4)
        if ks > 0:
            txt = sentence(spec.get("stamp", "ABOUT THE SAME")) if not L.broadcast else spec.get("stamp", "ABOUT THE SAME")
            wv = K.measure(txt if not L.broadcast else txt.upper(), *L.ty("label")[:2]) + 48
            yb = snap_y - 100 if snap_y - 100 >= y0 - 20 else snap_y + 40   # beside the line it describes
            xm = min((xl + xr) / 2, xr - 24 - wv / 2)       # never reaching the end labels
            L.panel_box(F, (xm - wv / 2, yb, xm + wv / 2, yb + 64), L.hi, alpha=ks, outline=False)
            L.text(F, txt, xm, yb + 14, "label", _on(L.hi), anchor="c", alpha=ks, role="label")


def scene_volume(F, L, c, t, dur, spec):
    games = c.p["counts"]["games"]
    team = [g for g in games if g.get("team_targets")]
    vals = [c.claims[g["team_targets"]]["value"] for g in team]
    title_block(F, L, c, t, "The team's volume", f"{c.d(c.p['team_name'])} targets per game", kick_src=None)
    m = c.p["metrics"][0]
    t_share = c.at(m["baseline"], default=dur * 0.6)
    dim = L.e(t, t_share - 0.2, 0.5, "move")
    t_low = c.at(team[-1]["team_targets"], default=0.5)
    cols = []
    for i, (g, v) in enumerate(zip(team, vals)):
        lit = i == len(team) - 1 and t >= t_low - 0.05
        cols.append({"label": f"Wk {g['week']}", "value": v, "display": c.d(g["team_targets"]),
                     "src": g["team_targets"], "color": L.hi if lit else L.neutral, "at": 0.05 + i * 0.06,
                     "lit": lit, "text": L.hi if lit else L.ink})
    top = K.nice_max(max(vals + [1]), 10)
    column_chart(F, L, c, t, cols, (L.mx + 20, 560, W - L.mr - 20, 1000), top, 10, dim=dim)
    ks = L.e(t, t_share - 0.1, 0.5)
    if ks > 0:
        y = 1090 - (1 - ks) * 40
        L.panel_box(F, (L.mx, y, W - L.mr, y + 250), pal(L.tk, "slab_dark") if L.broadcast else L.panel, alpha=ks,
                    team=L.team(c)[0] if L.broadcast else None)
        L.text(F, f"But his {m['label']} still fell", L.mx + 44, y + 34, "kicker", L.muted, alpha=ks, role="kicker")
        L.text(F, c.d(m["baseline"]), L.mx + 44, y + 92, "number_l", L.muted, alpha=ks, src=m["baseline"],
               role="number", tabular=True, display=True)
        bx = L.mx + 44 + K.measure(c.d(m["baseline"]), *L.ty("number_l")[:2]) + 40
        F.line((bx, y + 150, bx + 60, y + 150), L.muted, 5, alpha=ks)
        F.poly([(bx + 60, y + 136), (bx + 82, y + 150), (bx + 60, y + 164)], L.muted, alpha=ks)
        L.text(F, c.d(m["recent"]), bx + 110, y + 92, "number_l", L.data(c), alpha=ks, src=m["recent"],
               role="number", tabular=True, display=True,
               max_width=W - L.mr - 44 - bx - 110)


def scene_lineup(F, L, c, t, dur, spec):
    """The decision in context: his slot in a lineup strip, the matchup, then the one number to watch on a track
    from zero, with where it is now and where it was."""
    p = c.p
    m = p["metrics"][0]
    week = c.d(c.facts["episode"]["week"])
    title_block(F, L, c, t, f"Your lineup · {week}", "The one number to watch", kick_src=c.facts["episode"]["week"])
    slots = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX"]
    mine = slots.index(p["position"]) if p["position"] in slots else 6
    sched = next((s for s in p["schedule"] if s["week"] == c.facts["week"]), None)
    x0, x1 = L.mx, W - L.mr
    gap = 10
    cw = (x1 - x0 - gap * 6) / 7
    for i, slot in enumerate(slots):
        k = L.e(t, 0.03 * i, 0.4)
        x = x0 + i * (cw + gap)
        hot = i == mine
        L.panel_box(F, (x, 540, x + cw, 610), L.data(c) if hot else L.panel, alpha=k, outline=not hot)
        L.text(F, slot, x + cw / 2, 562, "label", _on(L.data(c)) if hot else L.muted, anchor="c", alpha=k,
               role="label")
    k = L.e(t, 0.25)
    L.panel_box(F, (x0, 640, x1, 840), pal(L.tk, "slab_dark") if L.broadcast else L.panel, alpha=k,
                team=L.team(c)[0] if L.broadcast else None)
    if not L.broadcast:
        F.rect((x0, 640, x0 + 8, 840), L.data(c), alpha=k)
    L.text(F, p["name"], x0 + 48, 676, "number_m", L.ink, alpha=k, role="title", c=c, max_width=x1 - x0 - 96,
           display=True)
    opp = ""
    if sched and sched["opponent_value"] != "bye":
        opp = f" · {'vs' if sched.get('home') else 'at'} {TEAM_NAMES[sched['opponent_value']]}"
    L.text(F, f"{p['position']} · {c.d(p['team_name'])}{opp} · {week}", x0 + 48, 780, "label", L.muted, alpha=k,
           role="label", src="schedule", c=c, max_width=x1 - x0 - 96)
    tw = c.at(word="watch", default=dur * 0.35)
    kw = L.e(t, tw - 0.05, 0.5)
    if kw > 0:
        yb = 900
        L.text(F, "What to watch", x0, yb, "kicker", L.muted, alpha=kw, role="kicker")
        L.text(F, f"{m['label'].capitalize()} back toward {c.d(m['baseline'])}?", x0, yb + 52, "title"
               if not L.broadcast else "number_m", L.hi, alpha=kw, role="title", src=m["baseline"], c=c,
               max_width=x1 - x0, display=True)
        vals = [c.claims[m["recent"]]["value"], c.claims[m["baseline"]]["value"]]
        top = K.nice_max(max(vals), 0.1)
        track(F, L, x0, x1, yb + 250, top, [
            {"value": vals[0], "color": L.data(c), "kind": "fill", "at": tw, "label": f"Now {c.d(m['recent'])}",
             "src": m["recent"]},
            {"value": vals[1], "color": L.hi, "kind": "marker", "at": tw + 0.25,
             "label": f"{K.weeks_label(K.claim_weeks(c, m['baseline']))}: {c.d(m['baseline'])}", "src": m["baseline"]}],
            t, h=28)


def scene_decision(F, L, c, t, dur, spec):
    q = spec.get("question", ["?"])
    text = " ".join(q)
    k = L.e(t, 0.0)
    y = 480
    key = "display"
    lines = _wrap(L.case(text, c), L.ty(key), L.mr_width)
    lh = L.ty(key)[1] * 1.08
    y = min(y, CAPTION_ZONE - int(len(lines) * lh + 80 + 120 + 24 + 44))    # keep the block clear of the captions
    for i, line in enumerate(lines):
        L.text(F, line, L.mx, y + i * (L.ty(key)[1] * 1.08), key,
               L.hi if i == len(lines) - 1 else L.ink, alpha=L.e(t, 0.06 * i), role="title", display=True)
    yo = y + len(lines) * L.ty(key)[1] * 1.08 + 80
    for i, (label, col) in enumerate(zip(spec.get("options", ["START", "SIT"]), (L.up, L.down))):
        ko = L.e(t, 0.3 + i * 0.1)
        x = L.mx + i * 440
        L.panel_box(F, (x, yo, x + 410, yo + 120), col if L.broadcast else L.panel, alpha=ko, outline=not L.broadcast)
        if not L.broadcast:
            F.rect((x, yo, x + 410, yo + 120), None, col, 4, L.tk.radius.panel, alpha=ko)
        L.text(F, label, x + 205, yo + 40, "number_s", _on(col) if L.broadcast else L.ink, anchor="c", alpha=ko,
               role="label", c=c)
    source_line(F, L, c, k, y=int(yo + 144))


def scene_presence(F, L, c, t, dur, spec):
    sn = next((m for m in c.p["metrics"] if m["column"] == "offense_pct"), None)
    title_block(F, L, c, t, "Playing time", "Share of the team's snaps")
    for j, (lab, cid, col) in enumerate((("Before", sn["baseline"], L.muted), ("Now", sn["recent"], L.data(c)))):
        ta = c.at(cid, default=0.3 + j * 0.8)
        k = L.e(t, ta - 0.2, 0.7)
        y = 620 + j * 300
        L.text(F, f"{lab} · {K.weeks_label(K.claim_weeks(c, cid))}", L.mx, y, "label", L.muted, alpha=K.clamp(k * 3),
               role="label", src="claim-weeks", c=c)
        L.text(F, c.d(cid), W - L.mr, y - 20, "number_m", col, anchor="r", alpha=K.clamp(k * 3), src=cid,
               role="number", tabular=True)
        track(F, L, L.mx, W - L.mr, y + 90, 1.0, [{"value": c.claims[cid]["value"], "color": col, "at": ta - 0.2}], t,
              h=36)
        L.text(F, "0%", L.mx, y + 150, "label", L.faint, alpha=k, role="axis", src="axis")
        L.text(F, "100%", W - L.mr, y + 150, "label", L.faint, anchor="r", alpha=k, role="axis", src="axis")


def scene_teammate_out(F, L, c, t, dur, spec):
    p, tm = c.p, c.p["teammate"]
    m = p["metrics"][0]
    tc = t + 5 if spec.get("phase") == 2 else t
    title_block(F, L, c, tc, "Why the role opened up", f"{tm['name']} missed time", )
    weeks = [s["week"] for s in m["series"]]
    missed = set(tm["missed_values"])
    tm_series = {x["week"]: x["value"] for x in tm["series"]}
    me = {s["week"]: s["value"] for s in m["series"]}
    n = len(weeks)
    gap = 14
    x0 = L.mx + 210
    cw = (W - L.mr - x0 - gap * (n - 1)) / n
    for r, (name, series, is_tm) in enumerate(((tm["name"], tm_series, True), (p["name"], me, False))):
        y = 620 + r * 290
        kr = L.e(tc, r * 0.3)
        L.text(F, last_name(name), L.mx, y + 30, "label", L.ink, alpha=kr, role="label", c=c, max_width=200)
        L.text(F, (tm["column"] if is_tm else m["column"]).replace("_", " ").replace("offense pct", "snap share"),
               x0, y - 52, "kicker", L.muted, alpha=kr, role="kicker", c=c)
        for i, w in enumerate(weeks):
            k = L.e(tc, 0.15 + r * 0.25 + i * 0.08, 0.4)
            if k <= 0:
                continue
            x = x0 + i * (cw + gap)
            box = (x, y, x + cw, y + 150)
            if is_tm and w in missed:
                F.rect(box, L.down, radius=L.tk.radius.bar, alpha=k)
                L.text(F, "Out", x + cw / 2, y + 56, "label", _on(L.down), anchor="c", alpha=k, role="label")
            else:
                val = series.get(w)
                hot = (not is_tm and w in p["recent_weeks"]) or (spec.get("phase") == 2 and is_tm and val is not None)
                F.rect(box, L.data(c) if hot else L.panel, None if hot else L.line, 0 if hot else 2,
                       L.tk.radius.bar, alpha=k)
                L.text(F, render("pct", val)[0] if val is not None else "—", x + cw / 2, y + 52, "number_s",
                       _on(L.data(c)) if hot else L.ink, anchor="c", alpha=k, src="snapshot:series", role="number",
                       max_width=cw - 16)
            if r == 1:
                L.text(F, f"Wk {w}", x + cw / 2, y + 176, "label", L.muted, anchor="c", alpha=k, role="axis", src="week")
    kc = L.e(t, 0.6)
    if spec.get("phase") == 2:
        L.text(F, f"His role before: {c.d(tm['role'])}", L.mx, 1170, "label", L.hi, alpha=L.e(t, 0.0), role="label",
               src=tm["role"], c=c)
    elif "reason" in tm:
        L.text(F, f"Out: {c.d(tm['reason'])}", L.mx, 1170, "label", L.down, alpha=kc, role="label", src=tm["reason"], c=c)


def scene_status_stamp(F, L, c, t, dur, spec):
    tm = c.p["teammate"]
    code, week, detail = c.claims[tm["status"]]["value"]
    big = {"ir": "Injured reserve", "none": "Not on the report"}.get(code) or (
        f"Listed {detail.lower()}" if code == "designation" else
        {"Did Not Participate In Practice": "Not practicing", "Limited Participation in Practice": "Limited",
         "Full Participation in Practice": "Practicing fully"}.get(detail, detail))
    col = L.down if ("not" in big.lower() or code in ("ir", "designation")) else L.up
    title_block(F, L, c, t, "Right now", f"{tm['name']}")
    k = L.e(t, 0.25)
    L.panel_box(F, (L.mx, 640, W - L.mr, 900), pal(L.tk, "slab_dark") if L.broadcast else L.panel, alpha=k,
                team=col if L.broadcast else None)
    if not L.broadcast:
        F.rect((L.mx, 640, L.mx + 8, 900), col, alpha=k)
    L.text(F, big, L.mx + 56, 720, "title", col, alpha=k, role="title", display=True, max_width=L.mr_width - 100)
    src = "NFL roster status" if code == "ir" else "Official injury report"
    L.text(F, f"{src} · Week {week}", L.mx + 56, 820, "label", L.muted, alpha=k, role="label",
           src=f"{tm['status']}:value", c=c)


SCENES = {"player_card": scene_player_card, "target_balls": scene_target_balls, "split": scene_split,
          "volume": scene_volume, "lineup": scene_lineup, "decision": scene_decision, "presence": scene_presence,
          "teammate_out": scene_teammate_out, "status_stamp": scene_status_stamp}


def _wrap(text: str, spec, width: float) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        cand = f"{cur} {w}".strip()
        if cur and K.measure(cand, spec[0], spec[1], spec[2]) > width:
            lines.append(cur)
            cur = w
        else:
            cur = cand
    return lines + ([cur] if cur else [])


# ------------------------------------------------------------------------------------------ render

def draw(name: str, scene: dict, t: float, dur: float, c, measure: bool = False) -> Frame:
    L = look(name)
    img = backdrop(name, c.team).copy()
    F = Frame(img, measure)
    chrome(F, L, c, t, scene, dur)
    SCENES[scene["type"]](F, L, c, t, dur, scene)
    return F


def camera(name: str, img: Image.Image, k: float, punch: float = 0.0) -> Image.Image:
    L = look(name)
    s = 1 + L.tk.motion.camera_push * k + punch
    if s <= 1.0005:
        return img
    cw, ch = W / s, H / s
    x0, y0 = (W - cw) / 2, (H - ch) * 0.45
    return img.resize((W, H), Image.BILINEAR, box=(x0, y0, x0 + cw, y0 + ch))


def transition(name: str, prev: Image.Image, cur: Image.Image, k: float, c) -> Image.Image:
    L = look(name)
    if not L.broadcast:
        e = K.bezier(*L.tk.motion.enter)(k)
        shifted = Image.new("RGB", cur.size, L.bg)
        shifted.paste(cur, (0, int((1 - e) * 48)))
        return Image.blend(prev, shifted, K.clamp(e * 1.15))
    # a team-colour slab sweeps across on a slant; the new scene is behind its leading edge
    e = K.bezier(*L.tk.motion.move)(k)
    p, s = L.team(c)
    sk = 0.35 * H
    edge = -sk - 200 + (W + sk + 400) * e
    mask = Image.new("L", cur.size, 0)
    ImageDraw.Draw(mask).polygon([(-10, 0), (edge + sk, 0), (edge, H), (-10, H)], fill=255)
    out = prev.copy()
    out.paste(cur, (0, 0), mask)
    d = ImageDraw.Draw(out)
    d.polygon([(edge + sk - 170, 0), (edge + sk, 0), (edge, H), (edge - 170, H)], fill=p)
    d.polygon([(edge + sk, 0), (edge + sk + 26, 0), (edge + 26, H), (edge, H)], fill=s)
    return out


CAPTION_Y = 1420
CAPTION_ZONE = 1396            # scene text stays above this line; the captions own the band below it
CAPTION_BOTTOM = 1535          # the last caption line's letters end above this


def caption(name: str, img: Image.Image, words: list[dict], active: int, t_in: float, c, measure=False) -> list[dict]:
    """Burned-in captions. Premium: as-spoken case on a soft dark band, the spoken word in the highlight colour.
    Broadcast: capitals on white slabs, the spoken word on a team-colour block."""
    L = look(name)
    tk = L.tk.caption
    spec = [tk.weight, tk.size, 0.0]
    F = Frame(img, measure)
    text_words = [w["text"].upper() if tk.case == "upper" else w["text"] for w in words]
    lines = _wrap_words(text_words, spec, CAPTION_MAX_W)
    lh = int(tk.size * 1.3)
    # centred on the caption line for one or two lines; a rare third line grows upward, never into the UI below
    y0 = min(CAPTION_Y + (2 - len(lines)) * lh / 2, CAPTION_BOTTOM - tk.size - (len(lines) - 1) * lh)
    y0 += (1 - K.bezier(*L.tk.motion.enter)(K.clamp(t_in * 5))) * 14
    if tk.band:
        band = _band(tk.band)
        img.paste(band, (0, CAPTION_Y - 70), band)
    idx = 0
    for li, line in enumerate(lines):
        widths = [K.measure(w, spec[0], spec[1]) for w in line]
        sp = K.measure(" ", spec[0], spec[1])
        total = sum(widths) + sp * (len(line) - 1)
        x = CAPTION_CX - total / 2
        y = y0 + li * lh
        if L.broadcast:
            k = 0.18 * tk.size
            F.poly([(x - 26 + k, y - 14), (x + total + 26 + k, y - 14), (x + total + 26 - k, y + tk.size * 0.73 + 18),
                    (x - 26 - k, y + tk.size * 0.73 + 18)], (255, 255, 255))
        for w, ww in zip(line, widths):
            is_act = idx == active
            emph = words[idx]["emph"]
            if L.broadcast:
                p, _ = L.team(c)
                if is_act:
                    F.rect((x - 8, y - 10, x + ww + 8, y + tk.size * 0.73 + 14), p)
                fill = _on(p) if is_act else (L.down if emph else K.hexrgb(tk.color))
            else:
                fill = K.hexrgb(tk.active) if is_act else (L.data(c) if emph else K.hexrgb(tk.color))
            F.text(w, x, y, spec, fill, role="caption")
            x += ww + sp
            idx += 1
    return F.log


CAPTION_MAX_W = 840
CAPTION_CX = (SAFE[0] + SAFE[2]) / 2          # the safe zone's centre (the right rail is UI)


def _wrap_words(words, spec, width):
    lines, cur = [], []
    for w in words:
        cand = cur + [w]
        if cur and K.measure(" ".join(cand), spec[0], spec[1]) > width:
            lines.append(cur)
            cur = [w]
        else:
            cur = cand
    return lines + ([cur] if cur else [])


@lru_cache(maxsize=4)
def _band(opacity: float) -> Image.Image:
    h = 300
    a = (np.clip(np.sin(np.linspace(0, math.pi, h)) * 1.4, 0, 1) * 255 * opacity).astype(np.uint8)
    band = Image.new("RGBA", (W, h), (6, 8, 11, 0))
    band.putalpha(Image.fromarray(np.tile(a[:, None], (1, W)), "L"))
    return band


def caption_spec(name: str) -> dict:
    tk = look(name).tk.caption
    from .. import visual
    return {"font": visual.WEIGHTS[tk.weight], "font_size": tk.size, "max_width_px": CAPTION_MAX_W, "max_lines": 2,
            "case": tk.case}


from . import vector_formats  # noqa: E402,F401  (registers the format scenes in SCENES)
