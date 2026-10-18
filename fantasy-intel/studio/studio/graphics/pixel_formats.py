"""Pixel-art designs for the editorial format scenes (studio/format_scenes.py), on the 216x384 art grid.

The same football ideas as the original renderer, as a 16-bit game would draw them: footballs stacked or laid
out one per target or carry (gold for the story player's own), menu windows and score panels, sprites in team
colours, 5%-per-segment meters for shares, a dialog-style call, and stepped motion (whole art pixels, on twos).
Every number is a claim's display string logged with that claim as its source.
"""

from __future__ import annotations

from .. import format_scenes as FS
from ..stages.formats import POS_WORD
from ..stages.story import last as last_name
from . import pixel as X
from .pixelart import CX, L0, R0, SCENES, _on, appear, meter, opened, step, title


def _n(c, cid) -> int:
    return int(round(c.claims[cid]["value"])) if cid else 0


def last(name: str) -> str:
    return last_name(name).upper()


def balls(F, x0: int, y0: int, per: int, groups, t: float, pitch: int = 14, row: int = 9) -> int:
    """Footballs laid out one per unit, in order: groups of (count, tint or "ghost", at, span). A ball appears
    (stepped) when its turn comes. Returns the bottom of the layout."""
    i = 0
    for count, kind, at, span in groups:
        for j in range(max(0, count)):
            r, col = divmod(i, per)
            if appear(step(t), at + span * j / max(count, 1)):
                spr = X.football(True) if kind == "ghost" else X.football(tint=kind)
                F.sprite(spr, x0 + col * pitch, y0 + r * row)
            i += 1
    return y0 + -(-i // per) * row - 2


def badge(F, text: str, x: float, y: int, fill, col, scale: int = 1, anchor: str = "c", src=None, role="label"):
    """A small label on a filled tag with a hard shadow."""
    w = X.text_width(text, scale) + 6
    h = 7 * scale + 4
    x0 = int(x - (w / 2 if anchor == "c" else w if anchor == "r" else 0))
    F.frame_box((x0, y, x0 + w, y + h), fill, col.night, shadow=col.night)
    F.text(text, x0 + 3, y + 2, scale, _on(fill), role=role, src=src)
    return w


def bar(F, x0: int, y0: int, x1: int, col):
    """A dark strip behind a line of text on the turf."""
    F.rect((x0, y0, x1, y0 + 11), col.night)


def stack(F, x: float, base: int, n: int, tint, t: float, at: float, ghost_to: int = 0):
    """One football per target, stacked upward from `base`, dropping in a ball at a time."""
    for j in range(n, max(n, ghost_to)):
        F.sprite(X.football(True), x - 6, base - 7 - j * 8)
    for j in range(n):
        drop = max(0, int((at + 0.03 * j + 0.2 - step(t)) * 120))
        F.sprite(X.football(tint=tint), x - 6, base - 7 - j * 8 - drop)


# ------------------------------------------------------------------------------------------ mystery

def scene_mirror(F, c, t, dur, spec, col):
    a, b = FS._games(c, spec["games"])
    stage = spec.get("stage", "collide")
    tt = 9 if stage == "targets" else t
    y = title(F, c.p["name"], 70, col)
    halves = ((L0, CX - 2, a), (CX + 2, R0, b))
    k = opened(tt, 0.0)
    for x0, x1, _ in halves:
        F.frame_box((x0, y + 4, x1, y + 4 + (258 - y - 4) * k), col.navy, col.steel, shadow=col.night)
    if k < 1:
        return
    big = max(_n(c, a["targets"]), _n(c, b["targets"]))
    for x0, x1, g in halves:
        m = (x0 + x1) / 2
        F.text(c.d(g["week_claim"]).upper(), m, y + 9, 1, col.gold, anchor="c", role="kicker", src=g["week_claim"])
        F.text("SNAPS", m, y + 21, 1, col.mist, anchor="c", role="label")
        if appear(tt, 0.05):
            F.text(c.d(g["snaps"]), m, y + 32, 4, col.white, anchor="c", role="number", src=g["snaps"])
            F.text(f"{c.d(g['snap_share'])} SNAPS", m, y + 64, 1, col.mist, anchor="c", role="number",
                   src=g["snap_share"])
        F.rect((x0 + 4, y + 76, x1 - 4, y + 77), col.slate)
        if stage != "collide" or appear(tt, 0.9):
            F.text("TARGETS", m, y + 81, 1, col.mist, anchor="c", role="label")
        at = c.at(claim=g["targets"], default=0.6) if stage != "targets" else -9
        said = stage != "collide" and appear(t, at - 0.08)
        if not said:
            if stage != "collide" or appear(tt, 0.9):
                F.text("?", m, y + 92, 4, col.mist, anchor="c", role="label")
            continue
        n = _n(c, g["targets"])
        F.text(c.d(g["targets"]), m, y + 92, 4, col.white if n == big else col.red, anchor="c", role="number",
               src=g["targets"])
        per = 5
        balls(F, int(m - (per * 14 - 1) / 2), y + 126, per, [(n, None, at, 0.3)], t)
    if appear(tt, 0.33):
        badge(F, "=", CX + 1, y + 40, col.gold, col, scale=2)
    if stage != "collide" and appear(t, (c.at(claim=b["targets"], default=0.9) if stage != "targets" else -9) + 0.05):
        badge(F, "≠", CX + 1, y + 100, col.red, col, scale=2)


def scene_which(F, c, t, dur, spec, col):
    a, b = FS._games(c, spec["games"])
    real = appear(t, c.at(word="real", default=dur * 0.6) - 0.1)
    y = title(F, "WHICH ONE IS REAL?" if real else c.p["name"], 70, col, color=col.gold if real else None)
    spr = X.player(col.team, col.team2, str(c.p.get("jersey") or ""), 84)
    bob = 1 if (int(t * 15) // 8) % 2 else 0
    sy = y + 4
    F.rect((CX - 36, sy + 85, CX + 36, sy + 88), col.night)
    F.sprite(spr, CX - spr.width // 2, sy + 87 - spr.height - bob)
    if real:
        badge(F, "?", CX + 48, sy + 6, col.gold, col, scale=4)
        bar(F, L0, sy + 91, R0, col)
        F.text(c.p["name"].upper(), CX, sy + 93, 1, col.white, anchor="c", role="label")
    big = max(_n(c, a["targets"]), _n(c, b["targets"]))
    k = opened(t, 0.1)
    for x0, x1, g in ((L0, CX - 2, a), (CX + 2, R0, b)):
        F.frame_box((x0, 214, x1, 214 + 44 * k), col.navy, col.steel, shadow=col.night)
        if k < 1:
            continue
        m = (x0 + x1) / 2
        n = _n(c, g["targets"])
        F.text(c.d(g["week_claim"]).upper(), m, 219, 1, col.mist, anchor="c", role="kicker", src=g["week_claim"])
        F.text(c.d(g["targets"]), m, 230, 2, col.white if n == big else col.red, anchor="c", role="number",
               src=g["targets"])
        F.text("TARGET" if n == 1 else "TARGETS", m, 247, 1, col.mist, anchor="c", role="label")


def scene_rival(F, c, t, dur, spec, col):
    sp = c.p["details"]["spread"]
    r = next(x for x in sp["returned"] if x["gsis_id"] == spec["rival"])
    b = c.p["details"]["games"][-1]
    y = title(F, f"THE OTHER {POS_WORD.get(c.p['position'], 'PLAYER').upper()}", 70, col)
    xl, xr = CX - 44, CX + 44
    me = X.player(col.team, col.team2, str(c.p.get("jersey") or ""), 60)
    other = X.player(col.team, col.team2, "", 60)
    base = y + 66
    for x, spr in ((xl, me), (xr, other)):
        F.rect((x - 24, base - 2, x + 24, base + 1), col.night)
        F.sprite(spr, x - spr.width // 2, base - spr.height)
    bar(F, L0, base + 4, R0, col)
    F.text(last(c.p["name"]), xl, base + 6, 1, col.white, anchor="c", role="label")
    F.text(last(r["name"]), xr, base + 6, 1, col.white, anchor="c", role="label")
    if appear(t, c.at(word="back", default=0.8) - 0.05):
        badge(F, "BACK", xr + 22, base - 64, col.gold, col)
        bar(F, L0, base + 15, R0, col)
        F.text(f"{last(r['name'])} MISSED {c.d(r['missed']).upper()}", CX, base + 17, 1, col.gold, anchor="c",
               role="label", src=r["missed"])
    rows = [("SNAPS", b["snaps"], r["snaps"], c.at(word="outsnap", default=1.6)),
            ("TARGETS", b["targets"], r["targets"], c.at(claim=r["targets"], default=2.4))]
    top = base + 30
    k = opened(t, rows[0][3] - 0.2)
    F.frame_box((L0, top, R0, top + (258 - top) * k), col.navy, col.steel, shadow=col.night)
    if k < 1:
        return
    F.text(c.d(sp["week_claim"]).upper(), CX, top + 4, 1, col.gold, anchor="c", role="kicker", src=sp["week_claim"])
    for i, (label, mine, theirs, at) in enumerate(rows):
        yy = top + 15 + i * 23
        F.text(label, CX, yy + 4, 1, col.mist, anchor="c", role="label")
        if not appear(t, at - 0.12):
            continue
        win = c.claims[theirs]["value"] > c.claims[mine]["value"]
        F.text(c.d(mine), xl, yy, 2, col.mist if win else col.white, anchor="c", role="number", src=mine)
        F.text(c.d(theirs), xr, yy, 2, col.gold if win else col.white, anchor="c", role="number", src=theirs)


def _season(c):
    return [g for g in c.p["details"]["season"] if g.get("targets")]


def scene_zoom_out(F, c, t, dur, spec, col):
    season = _season(c)
    spike = spec.get("spike")
    zoom = spec.get("stage") == "zoom"
    st = t if zoom else 9.0
    stamp = not zoom and appear(t, c.at(word="chase", default=dur * 0.8) - 0.1)
    y = title(F, spec.get("stamp", "") if stamp else "HIS WHOLE SEASON: TARGETS", 70, col,
              color=col.gold if stamp else None)
    F.frame_box((L0, y + 2, R0, 258), col.navy, col.steel, shadow=col.night)
    n = len(season)
    recent = [i for i, g in enumerate(season) if g["week"] >= (spike or season[-1]["week"])] or [n - 1]
    gx0, gx1 = L0 + 34, R0 - 2
    z = 1.0
    if zoom:
        z0 = c.at(word="zoom", default=0.3)
        z = min(1.0, max(0.0, (step(t) - z0) / 0.6))
        z = z * z * (3 - 2 * z)
    pe, ps = (gx1 - gx0) / n, (gx1 - gx0) / len(recent)
    base = 212
    snap_y = base + 18
    show_snaps = all(g.get("snaps") for g in season)
    F.text("WK", L0 + 4, base + 5, 1, col.mist, role="label")
    if show_snaps:
        F.text("SNAPS", L0 + 3, snap_y, 1, col.mist, role="label")
    xs = {}
    for i, g in enumerate(season):
        end = gx0 + pe * (i + 0.5)
        start = gx0 + ps * (recent.index(i) + 0.5) if i in recent else gx0 - pe * (recent[0] - i - 0.5)
        x = int(round(start + (end - start) * z))
        xs[g["week"]] = x
        at = 0.0 if i in recent else c.at(claim=g["targets"], default=0.6 + 0.3 * i)
        if not appear(st, at - 0.1) or x - 7 < gx0 - 4:
            continue
        v = _n(c, g["targets"])
        is_spike = g["week"] == spike
        stack(F, x, base, v, "gold" if is_spike or (zoom and i in recent) else None, st, at - 0.1)
        if appear(step(st), at + 0.1 + 0.03 * v):         # the count once its footballs have landed
            F.text(c.d(g["targets"]), x, base - 7 - v * 8 - 16, 2, col.gold if is_spike else col.white, anchor="c",
                   role="number", src=g["targets"], shadow=col.night)
        F.text(str(g["week"]), x, base + 5, 1, col.white, anchor="c", role="axis", src="week")
        if show_snaps:
            F.text(c.d(g["snaps"]), x, snap_y, 1, col.white, anchor="c", role="number", src=g["snaps"])
    if zoom or not spike:
        return
    if appear(t, c.at(word="outlier", default=0.6) - 0.05) and spike in xs:
        v = _n(c, next(g for g in season if g["week"] == spike)["targets"])
        badge(F, "OUTLIER", min(max(xs[spike], L0 + 26), R0 - 26), base - 7 - v * 8 - 30, col.gold, col)
    if spec.get("locked") and show_snaps and appear(t, c.at(word="locked", default=1.5) - 0.05):
        weeks = [g["week"] for g in season if g["week"] >= spike - 1]
        x0, x1 = int(xs[weeks[0]] - pe / 2 + 1), int(xs[weeks[-1]] + pe / 2 - 1)
        F.rect((x0, snap_y - 3, x1, snap_y - 2), col.green)
        F.rect((x0, snap_y + 8, x1, snap_y + 9), col.green)
        F.rect((x0, snap_y - 3, x0 + 1, snap_y + 9), col.green)
        F.rect((x1 - 1, snap_y - 3, x1, snap_y + 9), col.green)
        F.text("LOCKED IN", (x0 + x1) / 2, snap_y + 13, 1, col.green, anchor="c", role="label")


def scene_volume_balls(F, c, t, dur, spec, col):
    a, b = FS._games(c, spec["games"])
    y = title(F, f"{c.d(c.p['team_name'])} TARGETS, GAME BY GAME", 70, col)
    F.frame_box((L0, y + 2, R0, 258), col.navy, col.steel, shadow=col.night)
    yy = y + 8
    for g, start in ((a, 0.15), (b, 0.55)):
        if not appear(t, start - 0.1):
            continue
        F.text(c.d(g["week_claim"]).upper(), L0 + 6, yy, 1, col.mist, role="kicker", src=g["week_claim"])
        F.text(c.d(g["team_targets"]), R0 - 6, yy - 2, 2, col.white, anchor="r", role="number", src=g["team_targets"])
        mine, total = _n(c, g["targets"]), _n(c, g["team_targets"])
        yy = balls(F, L0 + 6, yy + 16, 12, [(mine, "gold", start, 0.2), (total - mine, None, start + 0.2, 0.35)], t)
        yy += 10
    F.text(f"GOLD = {last(c.p['name'])}'S TARGETS", CX, 246, 1, col.gold, anchor="c", role="label")


def scene_verdict(F, c, t, dur, spec, col):
    a, b = FS._games(c, spec["games"])
    call = spec.get("stage") == "call"
    tb = 9 if call else t
    y = title(F, "THE VERDICT", 70, col)
    rows = [("SNAPS", "snaps", "STEADY", col.green, "budge"), ("TARGETS", "targets", "SWINGING", col.red, None)]
    for i, (label, field, word, cc, cue) in enumerate(rows):
        yy = y + 6 + i * 52
        k = opened(tb, 0.05 + i * 0.25)
        F.frame_box((L0, yy, R0, yy + 46 * k), col.navy, cc, shadow=col.night)
        if k < 1:
            continue
        F.text(label, L0 + 6, yy + 5, 1, col.mist, role="label")
        for j, g in enumerate((a, b)):
            x = L0 + 30 + j * 60
            F.text(c.d(g[field]), x, yy + 16, 2, col.white, anchor="c", role="number", src=g[field])
            F.text(c.d(g["week_claim"]).upper(), x, yy + 34, 1, col.mist, anchor="c", role="axis",
                   src=g["week_claim"])
        if appear(tb, (c.at(word=cue, default=dur * 0.6) if cue else 0.6) - 0.05):
            badge(F, word, R0 - 6, yy + 16, cc, col, anchor="r")
    if call:
        yy = y + 114
        for i, line in enumerate(spec.get("stamp", [])):
            if not appear(t, 0.35 + i * 0.5):
                continue
            sc = 3 if i == 0 else 2
            lines = X.wrap(line.upper(), R0 - L0 - 8, sc)
            F.frame_box((L0, yy - 4, R0, yy + len(lines) * (7 * sc + 5) - 1), col.night, col.gold if i else col.white,
                        shadow=col.night)
            for ln in lines:
                F.text(ln, CX, yy, sc, col.white if i == 0 else col.gold, anchor="c", role="title", src="script:stamp")
                yy += 7 * sc + 5
            yy += 6


# -------------------------------------------------------------------------------------- opportunity

def _car(c):
    return c.p["details"]["carries"]


def identity(F, c, col, t: float, y: int = 66) -> int:
    """A player file strip: his sprite, name, position and team. Returns its bottom."""
    k = opened(t, 0.0)
    F.frame_box((L0, y, R0, y + 64 * k), col.navy, col.steel, shadow=col.night)
    if k < 1:
        return y + 64
    F.rect((L0 + 3, y + 58, L0 + 52, y + 61), col.turf)
    spr = X.player(col.team, col.team2, str(c.p.get("jersey") or ""), 60)
    F.sprite(spr, L0 + 28 - spr.width // 2, y + 2)
    name = last(c.p["name"])
    first = c.p["name"].split()[0].upper()
    F.text(first, L0 + 58, y + 10, 1, col.mist, role="label")
    F.text(name, L0 + 58, y + 22, 2 if X.text_width(name, 2) <= R0 - L0 - 64 else 1, col.white, role="title")
    F.text(f"{c.p['position']} · {c.d(c.p['team_name']).upper()}", L0 + 58, y + 42, 1, col.mist, role="label",
           src=c.p["team_name"])
    return y + 64


def scene_giant_number(F, c, t, dur, spec, col):
    car = _car(c)
    y = 68
    reveal = spec.get("reveal_player", True)
    if reveal:
        y = identity(F, c, col, t + 0.25) + 4
    sc = 5 if reveal else 7                       # with the player file above, the number steps down a size
    h = 7 * sc + 34
    F.frame_box((L0, y, R0, y + h + 6), col.night, col.team, shadow=col.night)
    F.text(c.d(car["own_recent_share"]), CX, y + 6, sc, col.data, anchor="c", role="number",
           src=car["own_recent_share"])
    F.text(f"OF {c.d(c.p['team_name']).upper()} RB CARRIES", CX, y + 7 * sc + 12, 1, col.white, anchor="c",
           role="label")
    F.text(c.d(car["recent_weeks"]).upper(), L0 + 6, y + h - 6, 1, col.mist, role="kicker", src=car["recent_weeks"])
    xr = R0 - 6
    for txt, cc, src in ((c.d(car["team_recent"]), col.white, car["team_recent"]), ("OF", col.mist, None),
                         (c.d(car["own_recent"]), col.gold, car["own_recent"])):
        F.text(txt, xr, y + h - 6, 1, cc, anchor="r", role="number" if src else "label", src=src)
        xr -= X.text_width(txt, 1) + 4
    total, mine = _n(c, car["team_recent"]), _n(c, car["own_recent"])
    gy = y + h + 12
    end = max(1.2, c.at(word="carry", default=dur * 0.8))
    per, pitch, row = (12, 14, 9) if not reveal else (13, 13, 8)
    rows = -(-total // per)
    F.frame_box((L0, gy, R0, min(258, gy + 6 + rows * row + 20)), col.navy, col.steel, shadow=col.night)
    bottom = balls(F, L0 + 3 + (172 - per * pitch) // 2, gy + 6, per,
                   [(mine, "gold", 0.5, end - 0.6), (total - mine, "ghost", end, 0.2)], t, pitch=pitch, row=row)
    F.text("EACH BALL = ONE CARRY", CX, min(bottom + 6, 249), 1, col.mist, anchor="c", role="label")


def scene_carry_grid(F, c, t, dur, spec, col):
    car = _car(c)
    tm = c.p["teammate"]
    who, tml = last(c.p["name"]), last(tm["name"])
    if spec.get("stage") != "before":
        total, mine = _n(c, car["team_recent"]), _n(c, car["own_recent"])
        y = identity(F, c, col, t) + 4 if spec.get("identify") else title(F, "THE BACKFIELD", 70, col)
        F.frame_box((L0, y, R0, 258), col.navy, col.steel, shadow=col.night)
        F.text(f"RB CARRIES · {c.d(car['recent_weeks']).upper()}", L0 + 6, y + 6, 1, col.mist, role="kicker",
               src=car["recent_weeks"])
        bottom = balls(F, L0 + 5, y + 18, 12, [(mine, "gold", 0.1, 1.0), (total - mine, "ghost", 1.2, 0.2)], t)
        if appear(t, c.at(claim=car["own_recent"], default=1.0) - 0.05):
            yy = bottom + 8
            x = L0 + 6
            for txt, sc, cc, src in ((c.d(car["own_recent"]), 4, col.gold, car["own_recent"]),
                                     ("OF", 2, col.mist, None), (c.d(car["team_recent"]), 4, col.white,
                                                                 car["team_recent"])):
                F.text(txt, x, yy + (14 if sc == 2 else 0), sc, cc, role="number" if src else "label", src=src)
                x += X.text_width(txt, sc) + 6
            F.text(f"GOLD = {who}", L0 + 6, yy + 34, 1, col.gold, role="label")
            F.text("OUTLINE = OTHER BACKS", L0 + 6, yy + 44, 1, col.mist, role="label")
        return
    total, his, mine = _n(c, car["team_base"]), _n(c, car["tm_base"]), _n(c, car["own_base"])
    y = title(F, f"WITH {tml} PLAYING", 70, col)
    F.frame_box((L0, y + 2, R0, 258), col.navy, col.steel, shadow=col.night)
    F.text(f"RB CARRIES · {c.d(car['base_weeks']).upper()}", L0 + 6, y + 7, 1, col.mist, role="kicker",
           src=car["base_weeks"])
    g0 = max(0.9, c.at(claim=car["own_base_share"], default=1.2) - 0.45)
    bottom = balls(F, L0 + 5, y + 18, 12, [(his, None, 0.05, 0.8), (mine, "gold", g0, 0.4),
                                         (total - his - mine, "ghost", 1.6, 0.2)], t)
    rows = ((tm, car["tm_base_share"], car["tm_base"], col.white, None, str(tm.get("jersey") or ""), 0.5),
            (c.p, car["own_base_share"], car["own_base"], col.gold, "gold", str(c.p.get("jersey") or ""),
             c.at(claim=car["own_base_share"], default=1.2)))
    yy = bottom + 6
    for pl, share, count, cc, tint, num, at in rows:
        if not appear(t, at - 0.1):
            yy += 34
            continue
        F.sprite(X.football(tint=tint), L0 + 5, yy + 2)
        F.text(last(pl["name"]), L0 + 22, yy + 2, 1, cc, role="label")
        F.text(f"{c.d(count)} CARRIES", R0 - 6, yy + 2, 1, col.mist, anchor="r", role="label", src=count)
        end = meter(F, L0 + 6, yy + 13, c.claims[share]["value"], cc, col, seg_w=6, h=7,
                    shown=min(1.0, (step(t) - at) / 0.5))
        F.text(c.d(share), end + 3, yy + 13, 1, cc, role="number", src=share)
        yy += 34
    F.text("BLOCK = 5% OF RB CARRIES", CX, min(yy, 249), 1, col.mist, anchor="c", role="label", src="legend")


def scene_handoff(F, c, t, dur, spec, col):
    tm = c.p["teammate"]
    out_at = c.at(word="out", default=dur * 0.5)
    out = appear(t, out_at - 0.1)
    y = title(F, f"{last(tm['name'])} OUT, {last(c.p['name'])} IN", 70, col)
    xl, xr = CX - 46, CX + 46
    base = y + 84
    tm_spr = X.player(col.slate, col.steel, str(tm.get("jersey") or ""), 72) if out else \
        X.player(col.team, col.team2, str(tm.get("jersey") or ""), 72)
    me = X.player(col.team, col.team2, str(c.p.get("jersey") or ""), 72)
    for x, spr in ((xl, tm_spr), (xr, me)):
        F.rect((x - 28, base - 2, x + 28, base + 1), col.night)
        F.sprite(spr, x - spr.width // 2, base - spr.height)
    bar(F, L0, base + 4, R0, col)
    F.text(last(tm["name"]), xl, base + 6, 1, col.mist if out else col.white, anchor="c", role="label")
    F.text(last(c.p["name"]), xr, base + 6, 1, col.white, anchor="c", role="label")
    if out:
        badge(F, "OUT", xl, base - 50, col.red, col, scale=2)
    # his carries arc across, a whole art pixel at a time
    for i in range(8):
        ft = (step(t) - out_at - 0.05 - i * 0.08) / 0.5
        if 0 < ft < 1:
            x = xl + (xr - xl) * ft
            yy = base - 40 - int(4 * ft * (1 - ft) * 40) + (i % 3) * 3
            F.sprite(X.football(), int(x) - 6, yy)
    lines = []
    if tm.get("reason"):
        reason = c.d(tm["reason"]).upper().removeprefix("A ").removeprefix("AN ")
        lines.append((f"{last(tm['name'])}: {reason}", col.red, tm["reason"],
                      c.at(claim=tm["reason"], default=0.3) - 0.1))
    lines.append((f"MISSED {c.d(tm['missed']).upper()}", col.white, tm["missed"], c.at(claim=tm["missed"],
                                                                                         default=0.3) - 0.1))
    lines.append(("HIS CARRIES WENT HERE ▶", col.gold, None, out_at + 0.4))
    rows = [(ln, cc, src, at) for text, cc, src, at in lines for ln in X.wrap(text, R0 - L0 - 8, 1)]
    yy = base + 18
    k = opened(t, 0.2)
    F.frame_box((L0, yy, R0, yy + (8 + 10 * len(rows)) * k), col.night, col.white, shadow=col.night)
    if k < 1:
        return
    for ln, cc, src, at in rows:
        if appear(t, at):
            F.text(ln, CX, yy + 5, 1, cc, anchor="c", role="label", src=src)
        yy += 10


STATUS = {"Did Not Participate In Practice": "DID NOT PRACTICE", "Limited Participation in Practice":
          "LIMITED PRACTICE", "Full Participation in Practice": "FULL PRACTICE"}


def scene_report(F, c, t, dur, spec, col):
    tm = c.p["teammate"]
    code, week, detail = c.claims[tm["status"]]["value"]
    word = {"ir": "INJURED RESERVE", "none": "NOT ON THE REPORT"}.get(code) or (
        detail.upper() if code == "designation" else STATUS.get(detail, detail.upper()))
    head = "PRACTICE REPORT" if code == "practice" else "INJURY REPORT"
    y = 66
    F.frame_box((L0, y, R0, y + 14), col.night, col.red)
    F.text(f"{head} · WEEK {week}", CX, y + 4, 1, col.white, anchor="c", role="kicker", src=f"{tm['status']}:value")
    k = opened(t, 0.0)
    F.frame_box((L0, y + 16, R0, 258 * k + (y + 16) * (1 - k)), col.white, col.steel, shadow=col.night)
    if k < 1:
        return
    ink = col.night
    F.text(tm["name"].upper(), L0 + 6, y + 24, 2, ink, role="title")
    F.text(f"{c.p['position']} · {c.d(c.p['team_name']).upper()}", L0 + 6, y + 42, 1, col.steel, role="label",
           src=c.p["team_name"])
    rows = [("MISSED", c.d(tm["missed"]).upper(), tm["missed"], c.at(claim=tm["missed"], default=0.3))]
    if tm.get("reason"):
        rows.append(("INJURY", c.d(tm["reason"]).upper().removeprefix("A ").removeprefix("AN "), tm["reason"],
                     c.at(claim=tm["reason"], default=0.8)))
    yy = y + 56
    for label, value, src, at in rows:
        F.rect((L0 + 4, yy - 3, R0 - 4, yy - 2), col.mist)
        F.text(label, L0 + 6, yy + 2, 1, col.steel, role="label")
        if appear(t, at - 0.2):
            for i, ln in enumerate(X.wrap(value, R0 - L0 - 12, 1)):
                F.text(ln, L0 + 6, yy + 12 + 10 * i, 1, ink, role="label", src=src)
        yy += 26 + 10 * (len(X.wrap(value, R0 - L0 - 12, 1)) - 1)
    F.rect((L0 + 4, yy - 3, R0 - 4, yy - 2), col.mist)
    F.text(f"WEEK {week} PRACTICE" if code == "practice" else "STATUS", L0 + 6, yy + 2, 1, col.steel, role="label",
           src=f"{tm['status']}:value")
    brief = c.p["details"].get("status_brief")
    if appear(t, c.at(claim=brief or tm["status"], default=1.0) + 0.1):
        lines = X.wrap(word, R0 - L0 - 16, 2)
        F.frame_box((L0 + 6, yy + 12, R0 - 6, yy + 16 + 18 * len(lines)), col.red if code != "none" else col.green,
                    col.night)
        for i, ln in enumerate(lines):
            F.text(ln, CX, yy + 15 + 18 * i, 2, col.night, anchor="c", role="title")
    if "as_of" in c.p["details"] and appear(t, c.at(claim=c.p["details"]["as_of"], default=0.4) - 0.05):
        F.rect((L0 + 1, 239, R0 - 1, 257), col.night)
        F.text(f"AS OF {c.d(c.p['details']['as_of']).upper()}", L0 + 6, 241, 1, col.gold, role="label",
               src=c.p["details"]["as_of"])
        F.text("CHECK BEFORE KICKOFF", L0 + 6, 250, 1, col.mist, role="label")


def scene_window(F, c, t, dur, spec, col):
    tm = c.p["teammate"]
    who, tml = last(c.p["name"]), last(tm["name"])
    nxt = c.p["details"].get("next_game")
    if spec.get("stage") != "closing":
        y = title(F, f"WHILE {tml} SITS", 70, col)
        x0, x1, y0, y1 = CX - 44, CX + 44, y + 8, y + 118
        F.frame_box((x0 - 3, y0 - 3, x1 + 3, y1 + 3), col.night, col.chalk, shadow=col.night)
        op = opened(t, 0.15, steps=4, dur=0.5)
        spr = X.player(col.team, col.team2, str(c.p.get("jersey") or ""), 96)
        F.sprite(spr, CX - spr.width // 2, y1 - spr.height)
        sw = int((x1 - x0) / 2 * (1 - 0.8 * op))
        for sx0, sx1 in ((x0, x0 + sw), (x1 - sw, x1)):
            F.frame_box((sx0, y0, sx1, y1), col.slate, col.steel)
        if appear(t, c.at(word="starter", default=dur * 0.6) - 0.05):
            w = X.text_width(f"START {who}", 2) + 12
            F.frame_box((CX - w / 2, y1 + 10, CX + w / 2, y1 + 30), col.green, col.night, shadow=col.night)
            F.text(f"START {who}", CX, y1 + 14, 2, col.night, anchor="c", role="title")
        if nxt and appear(t, 0.5):
            bar(F, L0, 244, R0, col)
            F.text(f"NEXT: {c.d(nxt['venue']).upper()} {c.d(nxt['opponent']).upper()}", CX, 246, 1, col.gold,
                   anchor="c", role="label", src=nxt["opponent"])
        return
    car = _car(c)
    y = title(F, f"WHEN {tml} IS BACK", 70, col)
    F.frame_box((L0, y + 2, R0, 258), col.navy, col.steel, shadow=col.night)
    F.text("THE ONLY SPLIT WITH HIM", CX, y + 8, 1, col.mist, anchor="c", role="label")
    yy = y + 24
    for name, share, cc in ((tml, car["tm_base_share"], col.white), (who, car["own_base_share"], col.gold)):
        F.text(name, L0 + 6, yy, 2, cc, role="label")
        F.text(c.d(share), R0 - 6, yy, 2, cc, anchor="r", role="number", src=share)
        meter(F, L0 + 6, yy + 18, c.claims[share]["value"], cc, col, seg_w=7, h=8,
              shown=min(1.0, (step(t) - 0.25) / 0.6))
        yy += 40
    F.text("BLOCK = 5% OF RB CARRIES", CX, yy, 1, col.mist, anchor="c", role="label", src="legend")
    F.text(c.d(car["base_weeks"]).upper(), CX, yy + 12, 1, col.mist, anchor="c", role="kicker",
           src=car["base_weeks"])


def scene_fork(F, c, t, dur, spec, col):
    tm = c.p["teammate"]
    car = _car(c)
    nxt = c.p["details"].get("next_game")
    who, tml = last(c.p["name"]), last(tm["name"])
    wk = c.facts["episode"]["week"]
    head = f"{c.d(wk)} · {c.d(nxt['venue'])} {c.d(nxt['opponent'])}" if nxt else c.d(wk)
    y = title(F, "THE CALL", 70, col)
    bar(F, L0, y + 2, R0, col)
    F.text(head.upper(), CX, y + 4, 1, col.gold, anchor="c", role="kicker", src=wk)
    branches = [(f"IF {tml} SITS", f"START {who}", col.green, car["own_recent_share"], car["recent_weeks"],
                 c.at(word="sits", default=0.4)),
                (f"IF {tml} IS BACK", "THE OLD SPLIT", col.white, car["own_base_share"], car["base_weeks"],
                 c.at(word="back", default=dur * 0.6))]
    yy = y + 18
    hgt = (258 - yy - 6) // 2
    for head_, call, cc, share, weeks, at in branches:
        k = opened(t, at - 0.15)
        F.frame_box((L0, yy, R0, yy + hgt * k), col.navy, cc, shadow=col.night)
        if k >= 1:
            F.text(head_, L0 + 6, yy + 5, 1, col.mist, role="label")
            if (int(t * 2) % 2) == 0 or cc is not col.green:
                F.text("▶", L0 + 6, yy + 17, 2, cc, role="label")
            F.text(call, L0 + 20, yy + 17, 2, cc, role="title")
            end = meter(F, L0 + 6, yy + 36, c.claims[share]["value"], col.gold if cc is col.green else col.mist, col,
                        seg_w=6, h=7, shown=min(1.0, (step(t) - at) / 0.5))
            F.text(c.d(share), end + 3, yy + 36, 1, col.white, role="number", src=share)
            F.text(f"OF RB CARRIES · {c.d(weeks).upper()}", L0 + 6, yy + 48, 1, col.mist, role="label", src=weeks)
        yy += hgt + 6


FORMAT = {"mirror": scene_mirror, "which": scene_which, "rival": scene_rival, "zoom_out": scene_zoom_out,
          "volume_balls": scene_volume_balls, "verdict": scene_verdict, "giant_number": scene_giant_number,
          "carry_grid": scene_carry_grid, "handoff": scene_handoff, "report": scene_report, "window": scene_window,
          "fork": scene_fork}
SCENES.update(FORMAT)
