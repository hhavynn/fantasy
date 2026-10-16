"""Premium and broadcast designs for the editorial format scenes (studio/format_scenes.py).

The same football ideas as the original renderer, drawn with the component library in vector.py:

  mystery      mirror (two games side by side, snaps equal, targets not), which (him between the two games),
               rival (a returning teammate, head to head on a shared axis), zoom_out (his whole season as
               columns: the chart widens from the two games everyone saw), volume_balls (the team's targets
               with his share inside), verdict (steady against swinging, then the call)
  opportunity  giant_number (the share, huge on frame 0, over one square per carry), carry_grid (the carries
               as squares: his now, or the old split with the starter), handoff, report (the injury report as
               a dated card), window (the call while the starter sits; the only split seen with him back),
               fork (the call both ways, each with its share on a 0-100% track)

Premium never stamps: a call is a sentence in the title type. Broadcast puts it on a slab. Every number is a
claim's display string (c.d) logged with that claim as its source; counts of squares are the claims' values.
"""

from __future__ import annotations

import math

from .. import format_scenes as FS
from .. import headshots
from .. import motion as M
from ..stages.formats import POS_WORD
from ..stages.story import last as last_name
from . import kit as K
from .kit import W, clamp, mix, pal
from .vector import SCENES, _on, cutout, portrait, sentence, title_block


# ----------------------------------------------------------------------------------------- helpers

def _x(L):
    return L.mx, W - L.mr


def _n(c, cid) -> int:
    return int(round(c.claims[cid]["value"])) if cid else 0


def _shown(L, text: str, role: str, c=None) -> str:
    """The text as L.text will draw it (case rules), for measuring before drawing."""
    t = L.case(text, c) if role in ("title", "kicker", "label", "body") else text
    return t.upper() if role == "kicker" or (L.broadcast and role == "label") else t


def inline(F, L, parts, x: float, y: float, anchor: str = "l", alpha: float = 1.0, gap: int = 16, c=None):
    """Texts of different sizes on one baseline. parts: (text, key, color, src, role). y is the tallest cap top."""
    caps = [L.ty(k)[1] * 0.727 for _, k, *_ in parts]
    widths = [K.measure(_shown(L, tx, role, c), *L.ty(k)[:2], L.ty(k)[2]) for tx, k, _, _, role in parts]
    total = sum(widths) + gap * (len(parts) - 1)
    cx = x - (total / 2 if anchor == "c" else total if anchor == "r" else 0)
    top = max(caps)
    for (tx, key, color, src, role), w, cap in zip(parts, widths, caps):
        L.text(F, tx, cx, y + top - cap, key, color, alpha=alpha, src=src, role=role, c=c)
        cx += w + gap
    return total


def other_portrait(c, espn_id, jersey, name: str, width: int):
    """A teammate's picture: his headshot when headshots are on, otherwise the jersey graphic."""
    shot = headshots.fetch(espn_id) if c.headshots_on and espn_id else None
    if shot:
        return cutout(str(shot), width)
    return M.jersey(c.team, str(jersey or ""), last_name(name), width)


def figure(F, L, c, img, cx: float, bottom: float, w: float, h: float, alpha: float = 1.0, dim: float = 0.0):
    """A portrait standing on a team plate (premium) or a slanted team slab (broadcast), bottoms aligned."""
    p, s = L.team(c)
    x0, x1, y0 = cx - w / 2, cx + w / 2, bottom - h
    if L.broadcast:
        k = L.skew * h * 0.5
        F.poly([(x0 + k, y0), (x1 + k, y0), (x1 - k, bottom), (x0 - k, bottom)], mix(p, L.bg, dim * 0.7), alpha * 0.95)
        F.poly([(x0 + k - 18, y0), (x0 + k, y0), (x0 - k, bottom), (x0 - k - 18, bottom)], s, alpha * (1 - dim * 0.6))
    else:
        plate = mix(p, L.bg, 0.55) if K.rel_lum(p) > 0.02 else mix((60, 70, 86), L.bg, 0.4)
        F.rect((x0, y0, x1, bottom), mix(plate, L.bg, dim * 0.6), radius=L.tk.radius.panel, alpha=alpha)
    F.layer(img, cx - img.width / 2, bottom - img.height, alpha=alpha * (1 - 0.6 * dim))


def identity(F, L, c, t: float, y: int = 300, slide: bool = True) -> int:
    """Who this is, at the top: the name and his position and team. Returns the block's bottom."""
    k = L.e(t, 0.0) if slide else 1.0
    off = (1 - k) * 24
    x0, x1 = _x(L)
    p, _ = L.team(c)
    sub = f"{c.p['position']} · {c.d(c.p['team_name'])}"
    if L.broadcast:
        name = c.p["name"].upper()
        nw = min(K.measure(name, *L.ty("number_m")[:2], L.ty("number_m")[2]) + 110, x1 - x0)
        L.panel_box(F, (x0 - (1 - k) * 300, y, x0 + nw - (1 - k) * 300, y + 96), p, alpha=k, outline=False)
        L.text(F, name, x0 + 42 + (1 - k) * 40, y + 18, "number_m", _on(p), alpha=k ** 2, role="title", display=True,
               max_width=nw - 84)
        L.text(F, sub, x0, y + 122, "label", L.muted, alpha=k, role="label", src=c.p["team_name"], c=c)
        return y + 165
    F.rect((x0, y + 4 + off, x0 + 8, y + 136 + off), p if K.rel_lum(p) > 0.03 else L.muted, alpha=k)
    L.text(F, c.p["name"], x0 + 32, y + off, "title", L.ink, alpha=k, role="title", c=c, max_width=x1 - x0 - 32)
    L.text(F, sub, x0 + 32, y + 100 + off, "label", L.muted, alpha=k, role="label", src=c.p["team_name"], c=c)
    return y + 145


def card(F, L, box, alpha: float = 1.0, team=None, fill=None):
    """A content card. Broadcast: a slab with a gentle slant (tall slabs would lean too far) and its colour bar
    standing just outside the left edge, so it never runs under the type."""
    x0, y0, x1, y1 = box
    if not L.broadcast:
        L.panel_box(F, box, fill or L.panel, alpha)
        return
    k = min(L.skew * (y1 - y0), 34)
    F.poly([(x0 + k, y0), (x1, y0), (x1 - k, y1), (x0, y1)], fill or pal(L.tk, "slab_dark"), alpha * 0.96)
    if team is not None:
        F.poly([(x0 + k - 26, y0), (x0 + k - 12, y0), (x0 - 12, y1), (x0 - 26, y1)], team, alpha)


def _cell(F, L, box, color, ghost: bool, k: float):
    x0, y0, x1, y1 = box
    if L.broadcast:
        y0 = y1 - (y1 - y0) * 0.72                 # tiles, not posts: a lone slanted post reads as "!"
        sk = L.skew * (y1 - y0) * 0.5
        pts = [(x0 + sk, y0), (x1 + sk, y0), (x1 - sk, y1), (x0 - sk, y1)]
        if ghost:
            F.line(pts + [pts[0]], color, 3, alpha=k)
        else:
            F.poly(pts, color, k)
    elif ghost:
        F.rect(box, None, color, 3, L.tk.radius.bar, alpha=k)
    else:
        F.rect(box, color, radius=L.tk.radius.bar, alpha=k)


def units(F, L, x0: float, y0: float, per: int, cell: float, gap: float, groups, t: float) -> float:
    """One square per unit (a carry, a target), filled in order. groups: (count, color, ghost, at, span).
    Returns the bottom of the grid."""
    i = 0
    for count, color, ghost, at, span in groups:
        for j in range(max(0, count)):
            r, col = divmod(i, per)
            x, y = x0 + col * (cell + gap), y0 + r * (cell + gap)
            k = L.e(t, at + span * j / max(count, 1), 0.25)
            if k > 0:
                _cell(F, L, (x, y, x + cell, y + cell), color, ghost, k)
            i += 1
    return y0 + math.ceil(i / per) * (cell + gap) - gap


def grid_fit(total: int, width: float) -> tuple[int, float, float]:
    """(per row, cell, gap) for `total` squares across `width`."""
    per = 10 if total <= 50 else 14
    gap = 12 if per == 10 else 8
    cell = min(72, (width - gap * (per - 1)) / per)
    return per, cell, gap


def chip(F, L, text: str, x: float, y: float, fill, key: str = "label", anchor: str = "l", alpha: float = 1.0,
         src=None, role: str = "label", c=None) -> tuple[float, float]:
    """A label on a filled chip (rounded in premium, slanted in broadcast). (x, y) is the chip's top left (or
    centre top with anchor c). Returns the chip's (width, height)."""
    spec = L.ty(key)
    tw = K.measure(_shown(L, text, role, c), *spec[:2], spec[2])
    cap = spec[1] * 0.727
    w, h = tw + 48, cap + 36
    x0 = x - (w / 2 if anchor == "c" else w if anchor == "r" else 0)
    if L.broadcast:
        L.panel_box(F, (x0, y, x0 + w, y + h), fill, alpha=alpha, outline=False)
    else:
        F.rect((x0, y, x0 + w, y + h), fill, radius=int(h / 2), alpha=alpha)
    L.text(F, text, x0 + 24, y + 18, key, _on(fill), alpha=alpha, src=src, role=role, c=c)
    return w, h


def equal_mark(F, L, cx: float, cy: float, color, k: float, crossed: bool = False):
    """An equals sign drawn as two bars (crossed through for 'not equal')."""
    if k <= 0:
        return
    w = 64 * (0.6 + 0.4 * k)
    for dy in (-16, 10):
        F.rect((cx - w / 2, cy + dy, cx + w / 2, cy + dy + 10), color, radius=3, alpha=k)
    if crossed:
        F.line((cx + 22, cy - 40, cx - 22, cy + 46), color, 9, alpha=k)


def plural(n: int, word: str) -> str:
    return word if n == 1 else word + "s"


# ------------------------------------------------------------------------------------------ mystery

def scene_mirror(F, L, c, t, dur, spec):
    """Two games side by side: the snaps land as equals, then the targets land as anything but."""
    a, b = FS._games(c, spec["games"])
    stage = spec.get("stage", "collide")
    settled = stage == "targets"
    tt = 9 if settled else t
    identity(F, L, c, tt, slide=not settled)
    x0, x1 = _x(L)
    cx = (x0 + x1) / 2
    cols = ((x0 + (x1 - x0) * 0.25, a), (x0 + (x1 - x0) * 0.75, b))
    k = L.e(tt, 0.05)
    for x, g in cols:
        if L.broadcast:
            p, _ = L.team(c)
            w = K.measure(c.d(g["week_claim"]).upper(), *L.ty("kicker")[:2], L.ty("kicker")[2]) + 56
            L.panel_box(F, (x - w / 2, 506, x + w / 2, 564), p, alpha=k, outline=False)
            L.text(F, c.d(g["week_claim"]), x, 518, "kicker", _on(p), anchor="c", alpha=k, role="kicker",
                   src=g["week_claim"])
        else:
            L.text(F, c.d(g["week_claim"]), x, 520, "kicker", L.muted, anchor="c", alpha=k, role="kicker",
                   src=g["week_claim"])
    if not L.broadcast:
        F.line((x0, 570, x1, 570), L.line, 2, alpha=k)
    # row 1: the snaps, equal
    L.text(F, "Snaps", cx, 606, "kicker", L.muted, anchor="c", alpha=k, role="kicker")
    for i, (x, g) in enumerate(cols):
        kn = L.e(tt, 0.05 + 0.08 * i, 0.4)
        L.text(F, c.d(g["snaps"]), x, 664 + (1 - kn) * 30, "number_l", L.ink, anchor="c", alpha=K.clamp(kn * 1.5),
               src=g["snaps"], role="number", tabular=True, display=True)
        L.text(F, f"{c.d(g['snap_share'])} of snaps", x, 812, "number_s", L.muted, anchor="c",
               alpha=K.clamp(kn * 1.5), src=g["snap_share"], role="number")
    equal_mark(F, L, cx, 728, L.up, L.e(tt, 0.33, 0.3))
    # row 2: the targets, unequal. "?" while the snaps are said, each number as it is said.
    kq = L.e(tt, 0.9 if stage == "collide" else 0.1, 0.3)
    if not L.broadcast:
        F.line((x0, 916, x1, 916), L.line, 2, alpha=kq)
    L.text(F, "Targets", cx, 952, "kicker", L.muted, anchor="c", alpha=kq, role="kicker")
    big = max(_n(c, a["targets"]), _n(c, b["targets"]))
    for x, g in cols:
        at = c.at(claim=g["targets"], default=0.6) if stage != "targets" else -9
        kt = L.e(t, at - 0.08, 0.35) if stage != "collide" else 0.0
        if kt < 0.5:
            L.text(F, "?", x, 1010, "number_l", L.faint, anchor="c", alpha=kq * (1 - kt * 2), role="label")
        if kt <= 0:
            continue
        n = _n(c, g["targets"])
        col = L.ink if n == big else L.down
        L.text(F, c.d(g["targets"]), x, 1010 + (1 - kt) * 30, "number_l", col, anchor="c", alpha=K.clamp(kt * 1.6),
               src=g["targets"], role="number", tabular=True, display=True)
        per = max(8, big)
        gap = 10
        cell = min(34, ((x1 - x0) / 2 - 60 - gap * (per - 1)) / per)
        ux = x - (per * (cell + gap) - gap) / 2          # left-aligned rows on one scale: a bar of squares
        units(F, L, ux, 1176, per, cell, gap, [(n, col, False, at, 0.3)], t)
    if stage != "collide":
        at2 = c.at(claim=b["targets"], default=0.9) if stage != "targets" else -9
        equal_mark(F, L, cx, 1072, L.down, L.e(t, at2 + 0.05, 0.3), crossed=True)


def scene_which(F, L, c, t, dur, spec):
    """The question, on him: his picture between the two games that disagree."""
    a, b = FS._games(c, spec["games"])
    x0, x1 = _x(L)
    cx = (x0 + x1) / 2
    k = L.e(t + 0.15, 0.0)
    img = portrait(c, 470 if not L.broadcast else 500)
    figure(F, L, c, img, cx, 860, 520, 400, alpha=K.clamp(k * 1.5))
    L.text(F, c.p["name"], cx, 896, "title", L.ink, anchor="c", alpha=k, role="title", c=c, display=True,
           max_width=x1 - x0)
    L.text(F, f"{c.p['position']} · {c.d(c.p['team_name'])}", cx, 1000, "label", L.muted, anchor="c", alpha=k,
           role="label", src=c.p["team_name"], c=c)
    big = max(_n(c, a["targets"]), _n(c, b["targets"]))
    for i, (g, bx0, bx1) in enumerate(((a, x0, cx - 12), (b, cx + 12, x1))):
        kc = L.e(t, 0.1 + 0.08 * i)
        off = (1 - kc) * (-60 if i == 0 else 60)
        box = (bx0 + off, 1070, bx1 + off, 1290)
        n = _n(c, g["targets"])
        col = L.ink if n == big else L.down
        card(F, L, box, alpha=kc, team=L.team(c)[0])
        mid = (box[0] + box[2]) / 2
        L.text(F, c.d(g["week_claim"]), mid, 1100, "kicker", L.muted, anchor="c", alpha=kc, role="kicker",
               src=g["week_claim"])
        inline(F, L, [(c.d(g["targets"]), "number_m", col, g["targets"], "number"),
                      (plural(n, "target"), "label", L.muted, None, "label")], mid, 1160, anchor="c", alpha=kc, c=c)
    kq = L.e(t, c.at(word="real", default=dur * 0.6) - 0.1, 0.4)
    if kq > 0:
        q = "Which one is real?"
        if L.broadcast:
            w = K.measure(q.upper(), *L.ty("title")[:2], L.ty("title")[2]) + 90
            L.panel_box(F, (x0, 318, min(x1, x0 + w), 430), L.hi, alpha=kq, outline=False)
            L.text(F, q, x0 + 44, 342, "title", _on(L.hi), alpha=kq, role="title", display=True, max_width=x1 - x0 - 80)
        else:
            L.text(F, q, x0, 330 + (1 - kq) * 24, "title", L.hi, alpha=kq, role="title", c=c, max_width=x1 - x0)


def _bars(F, L, cx, y, mine, theirs, top, half, k, c_mine, c_theirs):
    """A head-to-head bar pair on one axis: zero at the centre line, his to the left, the other's to the right."""
    F.rect((cx - 2, y - 14, cx + 2, y + 38), L.muted, alpha=k)
    lw = (half - 10) * mine / top * k
    rw = (half - 10) * theirs / top * k
    if lw > 1:
        F.rect((cx - 8 - lw, y, cx - 8, y + 24), c_mine, radius=L.tk.radius.bar, alpha=k)
    if rw > 1:
        F.rect((cx + 8, y, cx + 8 + rw, y + 24), c_theirs, radius=L.tk.radius.bar, alpha=k)


def scene_rival(F, L, c, t, dur, spec):
    """A same-position teammate back in the lineup, face to face with him: snaps, then targets."""
    sp = c.p["details"]["spread"]
    r = next(x for x in sp["returned"] if x["gsis_id"] == spec["rival"])
    b = c.p["details"]["games"][-1]
    x0, x1 = _x(L)
    cx = (x0 + x1) / 2
    xl, xr = x0 + (x1 - x0) * 0.25, x0 + (x1 - x0) * 0.75
    title_block(F, L, c, t, c.d(sp["week_claim"]), f"The other {POS_WORD.get(c.p['position'], 'player')}",
                kick_src=sp["week_claim"])
    ka = L.e(t, 0.05)
    figure(F, L, c, portrait(c, 300), xl, 790, 330, 270, alpha=ka)
    figure(F, L, c, other_portrait(c, r.get("espn_id"), None, r["name"], 300), xr, 790, 330, 270, alpha=ka)
    for x, nm in ((xl, c.p["name"]), (xr, r["name"])):
        L.text(F, last_name(nm), x, 814, "number_s", L.ink, anchor="c", alpha=ka, role="title", c=c,
               max_width=(x1 - x0) / 2 - 24)
    kb = L.e(t, c.at(word="back", default=0.8) - 0.05, 0.35)
    if kb > 0:
        chip(F, L, "Back", xr + 150, 524, L.hi, anchor="r", alpha=kb, c=c)
        L.text(F, f"Missed {c.d(r['missed'])}", xr, 890, "label", L.hi, anchor="c", alpha=kb, src=r["missed"],
               role="label", c=c, max_width=(x1 - x0) / 2 - 16)
    rows = [("Snaps", b["snaps"], r["snaps"], c.at(word="outsnap", default=1.6)),
            ("Targets", b["targets"], r["targets"], c.at(claim=r["targets"], default=2.4))]
    for i, (label, mine, theirs, at) in enumerate(rows):
        y = 966 + i * 196
        k = L.e(t, at - 0.12, 0.4)
        if k <= 0:
            continue
        vm, vt = c.claims[mine]["value"], c.claims[theirs]["value"]
        L.text(F, label, cx, y, "kicker", L.muted, anchor="c", alpha=k, role="kicker")
        win = vt > vm
        L.text(F, c.d(mine), xl, y + 40, "number_m", L.muted if win else L.ink, anchor="c", alpha=K.clamp(k * 1.6),
               src=mine, role="number", tabular=True, display=True)
        L.text(F, c.d(theirs), xr, y + 40, "number_m", L.hi if win else L.ink, anchor="c", alpha=K.clamp(k * 1.6),
               src=theirs, role="number", tabular=True, display=True)
        _bars(F, L, cx, y + 126, vm, vt, max(vm, vt, 1), (x1 - x0) / 2, k, L.neutral if win else L.ink,
              L.hi if win else L.neutral)


def _season(c):
    return [g for g in c.p["details"]["season"] if g.get("targets")]


def scene_zoom_out(F, L, c, t, dur, spec):
    """His whole season as columns on a zero-based axis. On "zoom" the chart widens from the two games
    everyone saw to every game; the verdict marks the outlier and the snaps that never moved."""
    season = _season(c)
    spike = spec.get("spike")
    stage = spec.get("stage")
    zoom = stage == "zoom"
    st = t if zoom else 9.0
    x0, x1 = _x(L)
    n = len(season)
    recent = [i for i, g in enumerate(season) if g["week"] >= (spike or season[-1]["week"])] or [n - 1]
    z = L.e(t, c.at(word="zoom", default=0.3), 0.9, "move") if zoom else 1.0
    kt = L.e(t, c.at(word="chase", default=dur * 0.8) - 0.1, 0.4) if not zoom else 0.0
    if kt < 1:
        k0 = L.e(st, 0.0)
        a = (1 - kt) * k0
        L.text(F, "His whole season" if not zoom or z > 0.5 else "The two games everyone saw", x0, 320, "kicker",
               L.hi if L.broadcast else L.muted, alpha=a, role="kicker")
        L.text(F, "Targets per game", x0, 372, "title", L.ink, alpha=a, role="title", c=c, display=True,
               max_width=x1 - x0)
    if kt > 0:
        line = sentence(spec.get("stamp", "")) if not L.broadcast else spec.get("stamp", "")
        if L.broadcast:
            w = min(K.measure(line.upper(), *L.ty("title")[:2], L.ty("title")[2]) + 90, x1 - x0)
            L.panel_box(F, (x0, 318, x0 + w, 430), L.hi, alpha=kt, outline=False)
            L.text(F, line, x0 + 44, 342, "title", _on(L.hi), alpha=kt, role="title", display=True, max_width=w - 80)
        else:
            L.text(F, line, x0, 330 + (1 - kt) * 24, "title", L.hi, alpha=kt, role="title", c=c, max_width=x1 - x0)
    vals = [c.claims[g["targets"]]["value"] for g in season]
    top = K.nice_max(max(vals + [1]), 2 if max(vals) <= 10 else 5)
    by0, by1 = 660, 1030
    if L.tk.chart.gridlines and not L.broadcast:
        v = 2 if top <= 10 else 5
        while v <= top + 1e-9:
            y = by1 - (by1 - by0) * v / top
            F.line((x0, y, x1, y), L.grid, 2)
            v += 2 if top <= 10 else 5
    F.line((x0, by1, x1, by1), L.line if not L.broadcast else L.ink, 3)
    pitch_end = (x1 - x0) / n
    pitch_start = (x1 - x0) / len(recent)
    snap_y = 1180
    show_snaps = all(g.get("snaps") for g in season)
    if show_snaps:
        L.text(F, "Snaps", x0, 1128, "kicker", L.muted, alpha=L.e(st, 0.2), role="kicker")
    xs = {}
    for i, g in enumerate(season):
        end = x0 + pitch_end * (i + 0.5)
        start = x0 + pitch_start * (recent.index(i) + 0.5) if i in recent else x0 - pitch_end * (recent[0] - i - 0.5)
        cx = start + (end - start) * z
        bw = (pitch_start + (pitch_end - pitch_start) * z) * (1 - L.tk.chart.bar_gap)
        bw = min(bw, 150)
        xs[g["week"]] = cx
        at = 0.0 if i in recent else c.at(claim=g["targets"], default=0.6 + 0.3 * i)
        k = L.e(st, at - 0.1, 0.42)
        if k <= 0 or cx - bw / 2 < x0 - 2:
            continue
        v = c.claims[g["targets"]]["value"]
        h = (by1 - by0) * v / top * k
        is_spike = g["week"] == spike and not zoom
        color = L.hi if is_spike or (i in recent and zoom) else L.neutral
        if L.broadcast:
            sk = min(10, h * 0.05)
            F.poly([(cx - bw / 2 + sk, by1 - h), (cx + bw / 2 + sk, by1 - h), (cx + bw / 2, by1), (cx - bw / 2, by1)],
                   color)
        else:
            L.bar(F, (cx - bw / 2, by1 - h, cx + bw / 2, by1), color)
        key = "number_m" if is_spike else "number_s"
        vy = by1 - h - int(L.ty(key)[1] * 0.73) - 24
        L.text(F, c.d(g["targets"]), cx, vy, key, L.hi if is_spike else L.ink, anchor="c", alpha=K.clamp(k * 1.6),
               src=g["targets"], role="number", tabular=True)
        L.text(F, f"Wk {g['week']}", cx, by1 + 22, "label", L.muted, anchor="c", alpha=K.clamp(k * 2), role="axis",
               src="week", c=c)
        if show_snaps:
            L.text(F, c.d(g["snaps"]), cx, snap_y, "number_s", L.ink, anchor="c", alpha=K.clamp(k * 2),
                   src=g["snaps"], role="number", tabular=True)
    if zoom or not spike:
        return
    ko = L.e(t, c.at(word="outlier", default=0.6) - 0.05, 0.35)
    if ko > 0 and spike in xs:
        v = c.claims[next(g for g in season if g["week"] == spike)["targets"]]["value"]
        ytop = by1 - (by1 - by0) * v / top - int(L.ty("number_m")[1] * 0.73) - 24
        lab = "Outlier"
        xo = min(max(xs[spike], x0 + 110), x1 - 110)
        chip(F, L, lab, xo, ytop - 84, L.hi, anchor="c", alpha=ko, c=c)
    if spec.get("locked") and show_snaps:
        kl = L.e(t, c.at(word="locked", default=1.5) - 0.05, 0.4)
        weeks = [g["week"] for g in season if g["week"] >= spike - 1]
        if kl > 0 and weeks:
            bx0 = xs[weeks[0]] - pitch_end * 0.45
            bx1 = xs[weeks[-1]] + pitch_end * 0.45
            bx1 = bx0 + (bx1 - bx0) * kl
            F.rect((bx0, snap_y - 24, bx1, snap_y + 64), None, L.up, 4, L.tk.radius.panel, alpha=kl)
            L.text(F, "Locked in", (bx0 + xs[weeks[-1]] + pitch_end * 0.45) / 2, snap_y + 88, "label", L.up,
                   anchor="c", alpha=kl, role="label", c=c)


def scene_volume_balls(F, L, c, t, dur, spec):
    """The team's targets in the two games, his inside each column: the volume that left, to scale."""
    a, b = FS._games(c, spec["games"])
    team = c.d(c.p["team_name"])
    title_block(F, L, c, t, "The team's volume", f"{team} targets, game by game")
    x0, x1 = _x(L)
    vals = [c.claims[g["team_targets"]]["value"] for g in (a, b)]
    top = K.nice_max(max(vals + [1]), 10)
    by0, by1 = 580, 1060
    if L.tk.chart.gridlines and not L.broadcast:
        for v in range(10, int(top) + 1, 10):
            y = by1 - (by1 - by0) * v / top
            F.line((x0, y, x1, y), L.grid, 2)
    F.line((x0, by1, x1, by1), L.line if not L.broadcast else L.ink, 3)
    pitch = (x1 - x0) / 2
    bw = pitch * 0.42
    for i, (g, start) in enumerate(((a, 0.15), (b, 0.55))):
        k = L.e(t, start - 0.1, 0.45)
        if k <= 0:
            continue
        cx = x0 + pitch * (i + 0.5)
        tv, mv = c.claims[g["team_targets"]]["value"], c.claims[g["targets"]]["value"]
        h = (by1 - by0) * tv / top * k
        hm = (by1 - by0) * mv / top * k
        L.bar(F, (cx - bw / 2, by1 - h, cx + bw / 2, by1), L.neutral)
        L.bar(F, (cx - bw / 2, by1 - hm, cx + bw / 2, by1), L.hi)
        L.text(F, c.d(g["team_targets"]), cx, by1 - h - int(L.ty("number_m")[1] * 0.73) - 24, "number_m", L.ink,
               anchor="c", alpha=K.clamp(k * 1.6), src=g["team_targets"], role="number", tabular=True)
        L.text(F, c.d(g["week_claim"]), cx, by1 + 22, "label", L.muted, anchor="c", alpha=k, role="axis",
               src=g["week_claim"], c=c)
        inline(F, L, [(c.d(g["targets"]), "number_s", L.hi, g["targets"], "number"),
                      (f"to {last_name(c.p['name'])}", "label", L.muted, None, "label")],
               cx, by1 + 80, anchor="c", alpha=K.clamp(k * 1.6), gap=12, c=c)
    kl = L.e(t, 1.0, 0.4)
    F.rect((x0, 1222, x0 + 34, 1256), L.hi, radius=L.tk.radius.bar, alpha=kl)
    L.text(F, f"{last_name(c.p['name'])}'s targets", x0 + 54, 1224, "label", L.muted, alpha=kl, role="label", c=c)


def scene_verdict(F, L, c, t, dur, spec):
    """What is steady and what is not, game against game; then the call."""
    a, b = FS._games(c, spec["games"])
    call = spec.get("stage") == "call"
    tb = 9 if call else t
    lift = L.e(t, 0.0, 0.45, "move") if call else 0.0
    x0, x1 = _x(L)
    L.text(F, "The verdict", x0, 320, "kicker", L.hi if L.broadcast else L.muted, role="kicker")
    rows = [("Snaps", "snaps", "Steady", L.up, "budge"), ("Targets", "targets", "Swinging", L.down, None)]
    for i, (label, field, word, col, cue) in enumerate(rows):
        y = 400 + i * 250 - lift * (40 + i * 70)
        k = L.e(tb, 0.05 + i * 0.25)
        box = (x0, y, x1, y + 210)
        card(F, L, box, alpha=k, team=col)
        if not L.broadcast:
            F.rect((x0, y, x0 + 8, y + 210), col, alpha=k)
        L.text(F, label, x0 + 48, y + 34, "kicker", L.muted, alpha=k, role="kicker")
        xa = x0 + 48
        for g in (a, b):
            w = inline(F, L, [(c.d(g[field]), "number_m", L.ink, g[field], "number"),
                              (c.d(g["week_claim"]), "label", L.muted, g["week_claim"], "label")],
                       xa, y + 96, alpha=k, gap=14, c=c)
            xa += w + 56
        at = c.at(word=cue, default=dur * 0.6) if cue else 0.6
        ks = L.e(tb, at - 0.05, 0.35)
        chip(F, L, word, x1 - 40, y + 34, col, anchor="r", alpha=ks, c=c)
    if call:
        for i, line in enumerate(spec.get("stamp", [])):
            k = L.e(t, 0.35 + i * 0.5, 0.4)
            key = "display" if i == 0 else "title"
            text = sentence(line, c) if not L.broadcast else line
            L.text(F, text, x0, 880 + i * 170 + (1 - k) * 24, key, L.ink if i == 0 else L.hi, alpha=k, role="title",
                   c=c, display=True, max_width=x1 - x0, src="script:stamp")


# -------------------------------------------------------------------------------------- opportunity

def _car(c):
    return c.p["details"]["carries"]


def scene_giant_number(F, L, c, t, dur, spec):
    """The workload as one huge number on frame 0, what it measures, then one square per carry."""
    car = _car(c)
    x0, x1 = _x(L)
    reveal = spec.get("reveal_player", True)
    y = 360
    if reveal:
        y = identity(F, L, c, t + 0.25) + 40
    k = L.e(t + 0.3, 0.0)                          # already settling on frame 0
    data = L.data(c)
    if L.broadcast:
        L.panel_box(F, (x0, y - 20, x1, y + 290), pal(L.tk, "slab_dark"), alpha=k, team=L.team(c)[0])
    L.text(F, c.d(car["own_recent_share"]), x0 + (60 if L.broadcast else 0), y + (1 - k) * 30, "number_xl", data,
           alpha=K.clamp(k * 1.5), src=car["own_recent_share"], role="number", tabular=True, display=True)
    team = c.d(c.p["team_name"])
    L.text(F, f"of {team} running back carries", x0 + (60 if L.broadcast else 0), y + int(L.ty("number_xl")[1] * 1.03), "label", L.ink,
           alpha=K.clamp(k * 1.5), role="label", c=c, max_width=x1 - x0 - 80)
    yk = y + 400
    kc = L.e(t, 0.3)
    L.text(F, c.d(car["recent_weeks"]), x0, yk, "kicker", L.muted, alpha=kc, role="kicker", src=car["recent_weeks"])
    inline(F, L, [(c.d(car["own_recent"]), "number_s", data, car["own_recent"], "number"),
                  ("of", "label", L.muted, None, "label"),
                  (c.d(car["team_recent"]), "number_s", L.ink, car["team_recent"], "number"),
                  ("carries", "label", L.muted, None, "label")], x1, yk - 10, anchor="r", alpha=kc, gap=12, c=c)
    total, mine = _n(c, car["team_recent"]), _n(c, car["own_recent"])
    per, cell, gap = grid_fit(total, x1 - x0)
    gy = yk + 76
    end = max(1.2, c.at(word="carry", default=dur * 0.8))
    bottom = units(F, L, x0, gy, per, cell, gap, [(mine, data, False, 0.5, end - 0.6),
                                                  (total - mine, L.faint, True, end, 0.2)], t)
    kl = L.e(t, 0.8)
    L.text(F, "One square = one running back carry", x0, bottom + 32, "label", L.faint if not L.broadcast else L.muted,
           alpha=kl, role="label", c=c, max_width=x1 - x0)


def scene_carry_grid(F, L, c, t, dur, spec):
    """The carries as squares: his share now (one square per carry), or the old split with the starter."""
    car = _car(c)
    tm = c.p["teammate"]
    x0, x1 = _x(L)
    data = L.data(c)
    who, tml = last_name(c.p["name"]), last_name(tm["name"])
    if spec.get("stage") != "before":
        total, mine = _n(c, car["team_recent"]), _n(c, car["own_recent"])
        y = 330
        if spec.get("identify"):
            y = identity(F, L, c, t) + 34
        L.text(F, f"Running back carries · {c.d(car['recent_weeks'])}", x0, y, "kicker", L.muted,
               alpha=L.e(t, 0.0), role="kicker", src=car["recent_weeks"])
        per, cell, gap = grid_fit(total, x1 - x0)
        bottom = units(F, L, x0, y + 62, per, cell, gap, [(mine, data, False, 0.1, 1.0),
                                                          (total - mine, L.faint, True, 1.2, 0.2)], t)
        kc = L.e(t, c.at(claim=car["own_recent"], default=1.0) - 0.05, 0.4)
        if kc > 0:
            inline(F, L, [(c.d(car["own_recent"]), "number_l", data, car["own_recent"], "number"),
                          ("of", "label", L.muted, None, "label"),
                          (c.d(car["team_recent"]), "number_m", L.ink, car["team_recent"], "number")],
                   x0, bottom + 56 + (1 - kc) * 24, alpha=K.clamp(kc * 1.6), gap=20, c=c)
            L.text(F, f"Filled: {who}. Outlined: other backs.", x0, bottom + 210, "label", L.muted,
                   alpha=K.clamp(kc * 1.6), role="label", c=c, max_width=x1 - x0)
        return
    # the old split, with the starter: his carries in the neutral colour, the backup's in the data colour
    total, his, mine = _n(c, car["team_base"]), _n(c, car["tm_base"]), _n(c, car["own_base"])
    L.text(F, f"{c.d(car['base_weeks'])} · with {tml}", x0, 320, "kicker", L.muted, alpha=L.e(t, 0.0),
           role="kicker", src=car["base_weeks"], c=c)
    L.text(F, "Running back carries", x0, 372, "title", L.ink, alpha=L.e(t, 0.0), role="title", c=c, display=True,
           max_width=x1 - x0)
    per, cell, gap = grid_fit(total, x1 - x0)
    g0 = max(0.9, c.at(claim=car["own_base_share"], default=1.2) - 0.45)
    his_col = mix(L.neutral, L.muted, 0.3)
    bottom = units(F, L, x0, 510, per, cell, gap, [(his, his_col, False, 0.05, 0.8),
                                                   (mine, data, False, g0, 0.4),
                                                   (total - his - mine, L.faint, True, 1.6, 0.2)], t)
    cards = ((tm, other_portrait(c, tm.get("espn_id"), tm.get("jersey"), tm["name"], 170), car["tm_base_share"],
              car["tm_base"], his_col, L.ink, 0.5),
             (c.p, portrait(c, 170), car["own_base_share"], car["own_base"], data, data,
              c.at(claim=car["own_base_share"], default=1.2)))
    cx = (x0 + x1) / 2
    y = bottom + 80
    for i, (pl, img, share, count, swatch, col, at) in enumerate(cards):
        k = L.e(t, at - 0.1, 0.4)
        if k <= 0:
            continue
        bx = x0 if i == 0 else cx + 16
        bw = cx - 16 - bx if i == 0 else x1 - bx
        figure(F, L, c, img, bx + 85, y + 200, 170, 170, alpha=k)
        tx = bx + 194
        L.text(F, c.d(share), tx, y + 10 + (1 - k) * 20, "number_m", col, alpha=K.clamp(k * 1.6), src=share,
               role="number", tabular=True, display=True, max_width=bw - 194)
        F.rect((tx, y + 106, tx + 26, y + 132), swatch, radius=L.tk.radius.bar, alpha=k)
        L.text(F, last_name(pl["name"]), tx + 40, y + 104, "label", L.ink, alpha=k, role="label", c=c,
               max_width=bw - 234)
        L.text(F, f"{c.d(count)} carries", tx, y + 164, "label", L.muted, alpha=k, src=count, role="label",
               c=c, max_width=bw - 194)


def scene_handoff(F, L, c, t, dur, spec):
    """The starter ruled out, his carries moving across to the backup."""
    tm = c.p["teammate"]
    x0, x1 = _x(L)
    xl, xr = x0 + (x1 - x0) * 0.25, x0 + (x1 - x0) * 0.75
    title_block(F, L, c, t, "The handoff", f"{last_name(tm['name'])} out, {last_name(c.p['name'])} in")
    out_at = c.at(word="out", default=dur * 0.5)
    ko = L.e(t, out_at - 0.1, 0.35)
    figure(F, L, c, other_portrait(c, tm.get("espn_id"), tm.get("jersey"), tm["name"], 320), xl, 860, 360, 300,
           dim=ko)
    figure(F, L, c, portrait(c, 320), xr, 860, 360, 300)
    for x, nm in ((xl, tm["name"]), (xr, c.p["name"])):
        L.text(F, last_name(nm), x, 884, "number_s", L.ink, anchor="c", role="title", c=c,
               max_width=(x1 - x0) / 2 - 24)
    if ko > 0:
        chip(F, L, "Out", xl, 560, L.down, anchor="c", alpha=ko, c=c)
    if tm.get("reason"):
        kr = L.e(t, c.at(claim=tm["reason"], default=0.3) - 0.1, 0.35)
        reason = c.d(tm["reason"]).removeprefix("a ").removeprefix("an ")
        L.text(F, reason[:1].upper() + reason[1:], xl, 964, "label", L.down, anchor="c", alpha=kr, role="label",
               src=tm["reason"], c=c, max_width=(x1 - x0) / 2 - 16)
        L.text(F, f"Missed {c.d(tm['missed'])}", xl, 1030, "label", L.muted, anchor="c", alpha=kr, role="label",
               src=tm["missed"], c=c, max_width=(x1 - x0) / 2 - 16)
    ka = L.e(t, out_at + 0.05, 0.6, "move")
    if ka > 0:
        y = 1150
        ax0, ax1 = xl + 40, xl + 40 + (xr - xl - 80) * ka
        F.line((ax0, y, ax1, y), L.data(c), 8)
        F.poly([(ax1, y - 20), (ax1 + 30, y), (ax1, y + 20)], L.data(c))
        L.text(F, "His carries went here", x1, 1200, "label", L.data(c), anchor="r", alpha=L.e(t, out_at + 0.4),
               role="label", c=c, max_width=x1 - x0 - 120)


STATUS = {"Did Not Participate In Practice": "Did not practice", "Limited Participation in Practice":
          "Limited practice", "Full Participation in Practice": "Full practice"}


def _status_word(c, tm):
    code, week, detail = c.claims[tm["status"]]["value"]
    word = {"ir": "Injured reserve", "none": "Not on the report"}.get(code) or (
        detail.capitalize() if code == "designation" else STATUS.get(detail, detail))
    return code, week, word


def scene_report(F, L, c, t, dur, spec):
    """The injury report as a dated card: who, what he missed and why, and his practice status as of a date."""
    tm = c.p["teammate"]
    code, week, word = _status_word(c, tm)
    x0, x1 = _x(L)
    k = L.e(t, 0.0)
    off = (1 - k) * 60
    top = 330 + off
    n_rows = 3 if tm.get("reason") else 2
    bot = top + 290 + 150 * n_rows + (170 if "as_of" in c.p["details"] else 20)
    card(F, L, (x0, top, x1, bot), alpha=k, team=L.down)
    head = "Practice report" if code == "practice" else "Injury report"
    L.text(F, f"{head} · Week {week}", x0 + 48, top + 40, "kicker", L.hi if L.broadcast else L.muted, alpha=k,
           role="kicker", src=f"{tm['status']}:value")
    L.text(F, tm["name"], x0 + 48, top + 100, "title", L.ink, alpha=k, role="title", c=c, display=True,
           max_width=x1 - x0 - 96)
    L.text(F, f"{c.p['position']} · {c.d(c.p['team_name'])}", x0 + 48, top + 200, "label", L.muted, alpha=k,
           role="label", src=c.p["team_name"], c=c)
    rows = [("Missed", c.d(tm["missed"]), tm["missed"], c.at(claim=tm["missed"], default=0.3), L.ink)]
    if tm.get("reason"):
        reason = c.d(tm["reason"]).removeprefix("a ").removeprefix("an ")
        rows.append(("Injury", reason[:1].upper() + reason[1:], tm["reason"],
                     c.at(claim=tm["reason"], default=0.8), L.ink))
    brief = c.p["details"].get("status_brief")
    rows.append(("Status" if code != "practice" else f"Week {week} practice", word, f"{tm['status']}:value",
                 c.at(claim=brief or tm["status"], default=1.0), L.down if code != "none" else L.up))
    y = top + 290
    for label, value, src, at, col in rows:
        kr = L.e(t, at - 0.2, 0.35)
        F.line((x0 + 48, y - 26, x1 - 48, y - 26), L.line, 2, alpha=k)
        L.text(F, label, x0 + 48, y, "kicker", L.muted, alpha=kr, role="kicker", src=src if "Week" in label else None)
        if col is not L.ink and L.broadcast:
            chip(F, L, value, x0 + 48, y + 46, col, key="number_s", alpha=kr, role="label", c=c)
        else:
            L.text(F, value, x0 + 48, y + 52, "number_s", col, alpha=kr, role="label", src=src, c=c,
                   max_width=x1 - x0 - 96)
        y += 150
    if "as_of" in c.p["details"]:
        ka = L.e(t, c.at(claim=c.p["details"]["as_of"], default=0.4) - 0.05, 0.35)
        F.line((x0 + 48, y - 26, x1 - 48, y - 26), L.line, 2, alpha=k)
        L.text(F, f"As of {c.d(c.p['details']['as_of'])}", x0 + 48, y, "label", L.hi, alpha=ka, role="label",
               src=c.p["details"]["as_of"], c=c)
        L.text(F, "Check the final status before kickoff", x0 + 48, y + 56, "label", L.muted, alpha=ka,
               role="label", c=c, max_width=x1 - x0 - 96)


def scene_window(F, L, c, t, dur, spec):
    """While the starter sits: the call. When he is back: the only split seen with him, on a 0-100% bar."""
    tm = c.p["teammate"]
    x0, x1 = _x(L)
    cx = (x0 + x1) / 2
    who, tml = last_name(c.p["name"]), last_name(tm["name"])
    nxt = c.p["details"].get("next_game")
    if spec.get("stage") != "closing":
        k = L.e(t, 0.0)
        L.text(F, f"While {tml} sits", x0, 330, "display", L.ink, alpha=k, role="title", c=c, display=True,
               max_width=x1 - x0)
        figure(F, L, c, portrait(c, 460), cx, 1010, 520, 440, alpha=K.clamp(k * 1.5))
        ks = L.e(t, c.at(word="starter", default=dur * 0.6) - 0.05, 0.35)
        chip(F, L, f"Start {who}", cx, 1050, L.up, key="number_m", anchor="c", alpha=ks, role="title", c=c)
        if nxt:
            L.text(F, f"Next: {c.d(nxt['venue'])} {c.d(nxt['opponent'])}", cx, 1220, "label", L.muted, anchor="c",
                   alpha=L.e(t, 0.5), role="label", src=nxt["opponent"], c=c)
        return
    car = _car(c)
    k = L.e(t, 0.0)
    title_block(F, L, c, t, "The only split we've seen with him", f"When {tml} is back")
    his, mine = c.claims[car["tm_base_share"]]["value"], c.claims[car["own_base_share"]]["value"]
    kb = L.e(t, 0.25, 0.6, "move")
    y = 860
    figure(F, L, c, other_portrait(c, tm.get("espn_id"), tm.get("jersey"), tm["name"], 190), x0 + 110, y - 44, 200,
           170, alpha=k)
    figure(F, L, c, portrait(c, 190), x1 - 110, y - 44, 200, 170, alpha=k)
    F.rect((x0, y, x1, y + 70), L.neutral if not L.broadcast else pal(L.tk, "grid"), radius=L.tk.radius.bar)
    split = x0 + (x1 - x0) * his * kb
    F.rect((x0, y, split, y + 70), L.muted, radius=L.tk.radius.bar)
    F.rect((split, y, split + (x1 - x0) * mine * kb, y + 70), L.data(c), radius=L.tk.radius.bar)
    L.text(F, "0%", x0, y + 96, "label", L.faint, alpha=k, role="axis", src="axis")
    L.text(F, "100%", x1, y + 96, "label", L.faint, anchor="r", alpha=k, role="axis", src="axis")
    inline(F, L, [(tml, "label", L.muted, None, "label"),
                  (c.d(car["tm_base_share"]), "number_m", L.ink, car["tm_base_share"], "number")],
           x0, y + 180, alpha=kb, c=c)
    inline(F, L, [(who, "label", L.muted, None, "label"),
                  (c.d(car["own_base_share"]), "number_m", L.data(c), car["own_base_share"], "number")],
           x1, y + 180, anchor="r", alpha=kb, c=c)
    L.text(F, f"Running back carries · {c.d(car['base_weeks'])}", x0, y + 300, "kicker", L.muted, alpha=kb,
           role="kicker", src=car["base_weeks"])


def scene_fork(F, L, c, t, dur, spec):
    """The call both ways: if the starter sits, and if he is back, each with its share on a 0-100% track."""
    tm = c.p["teammate"]
    car = _car(c)
    nxt = c.p["details"].get("next_game")
    x0, x1 = _x(L)
    who, tml = last_name(c.p["name"]), last_name(tm["name"])
    k0 = L.e(t, 0.0)
    wk = c.facts["episode"]["week"]
    head = f"{c.d(wk)} · {c.d(nxt['venue'])} {c.d(nxt['opponent'])}" if nxt else c.d(wk)
    L.text(F, head, x0, 320, "kicker", L.hi if L.broadcast else L.muted, alpha=k0, role="kicker", src=wk)
    L.text(F, "The call, both ways", x0, 372, "title", L.ink, alpha=k0, role="title", c=c, display=True,
           max_width=x1 - x0)
    branches = [
        (f"If {tml} sits", f"Start {who}", L.up, car["own_recent_share"], car["recent_weeks"],
         c.at(word="sits", default=0.4), 520),
        (f"If {tml} is back", "Expect the old split", L.muted, car["own_base_share"], car["base_weeks"],
         c.at(word="back", default=dur * 0.6), 900),
    ]
    nx = x0 + 14
    F.d.ellipse((nx - 12, 476 - 12, nx + 12, 476 + 12), fill=L.ink + (int(255 * k0),))
    for head_, call, col, share, weeks, at, y in branches:
        k = L.e(t, at - 0.15, 0.4)
        if k <= 0:
            continue
        F.line((nx, 476, nx, 476 + (y + 160 - 476) * k), col, 5)
        F.line((nx, y + 160, nx + 34 * k, y + 160), col, 5)
        bx0 = x0 + 60
        off = (1 - k) * 60
        card(F, L, (bx0 + off, y, x1 + off, y + 330), alpha=k, team=col)
        L.text(F, head_, bx0 + 44 + off, y + 32, "kicker", L.muted, alpha=k, role="kicker", c=c)
        L.text(F, call, bx0 + 44 + off, y + 82, "title" if not L.broadcast else "number_m", col if col is not L.muted
               else L.ink, alpha=k, role="title", c=c, display=True, max_width=x1 - bx0 - 88)
        tx0, tx1, ty = bx0 + 44 + off, x1 - 44 + off, y + 210
        v = c.claims[share]["value"]
        F.rect((tx0, ty, tx1, ty + 20), L.neutral if not L.broadcast else pal(L.tk, "grid"), radius=L.tk.radius.bar)
        F.rect((tx0, ty, tx0 + (tx1 - tx0) * v * k, ty + 20), L.data(c) if col is L.up else L.muted,
               radius=L.tk.radius.bar, alpha=k)
        inline(F, L, [(c.d(share), "number_s", L.ink, share, "number"),
                      (f"of the carries · {c.d(weeks)}", "label", L.muted, weeks, "label")],
               tx0, ty + 46, alpha=k, gap=14, c=c)


FORMAT = {"mirror": scene_mirror, "which": scene_which, "rival": scene_rival, "zoom_out": scene_zoom_out,
          "volume_balls": scene_volume_balls, "verdict": scene_verdict, "giant_number": scene_giant_number,
          "carry_grid": scene_carry_grid, "handoff": scene_handoff, "report": scene_report, "window": scene_window,
          "fork": scene_fork}
SCENES.update(FORMAT)
