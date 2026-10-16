"""Style B: retro pixel-art football. Same stories, same numbers, drawn on a 216x384 art grid.

The look borrows from 16-bit sports games without copying any: a scrolling turf field, a score-bug HUD,
menu windows with hard drop shadows, segmented meters (each segment is 5%, so the meters are to scale),
footballs stacked one per target, a player sprite in the team's colours with his number, and a dialog box for
captions. Motion is stepped (sprites on twos at 15 fps), panels open in steps, impacts shake the screen by one
art pixel, and scenes change with a stepped palette fade. Nothing is scaled by a fraction or rotated.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image

from .. import format_scenes as FS
from .. import motion as M
from ..numbers import TEAM_NAMES, render
from ..stages.story import last as last_name
from . import kit as K
from . import pixel as X

AW, AH, S = 216, 384, 5
L0, R0, T0, B0 = 14, 190, 46, 308          # art-grid safe zone: the video safe zone (70, 230, 950, 1540) / 5
CX = (L0 + R0) / 2
CAPTION_BOX = (L0 - 4, 266, R0 + 5, 307)        # the dialog box, art pixels (x0, y0, x1, y1)
FPS_ART = 15


def P():
    return X.palette()


def step(t: float) -> float:
    """Time quantised to the art frame rate: sprites move on twos."""
    return math.floor(t * FPS_ART) / FPS_ART


def appear(t: float, at: float) -> bool:
    return t >= at


def opened(t: float, at: float, steps: int = 4, dur: float = 0.2) -> float:
    """A menu window opening in whole steps (0..1)."""
    if t < at:
        return 0.0
    return min(steps, math.floor((t - at) / dur * steps) + 1) / steps


class C:
    """Colours for this episode: the palette plus the team's two swap slots."""

    def __init__(self, c):
        pal = P()
        self.__dict__.update(pal)
        self.team, self.team2 = X.team_slots(*M.team_colors(c.team))
        self.data = pal["red"] if c.p.get("role") == "faller" else pal["green"]


def weeks(c, cid) -> str:
    return K.weeks_label(K.claim_weeks(c, cid)).upper().replace("–", "-")


# ----------------------------------------------------------------------------------------- chrome

def background(F: X.PixelFrame, t: float):
    strip = X.turf()
    off = int(step(t) * 12) % 48                       # the field scrolls a whole pixel at a time
    F.img.paste(strip.crop((0, 48 - off, AW, 48 - off + AH)), (0, 0))
    # darken the upper and lower thirds a step so panels and captions sit on calm ground
    F.rect((0, 0, AW, 44), P()["night"])
    F.rect((0, 312, AW, AH), P()["night"])


SHOW = {"casefile": "CASE FILE", "alert": "OPPORTUNITY"}      # the format scenes' own show names


def hud(F, c, col: C, scene: dict | None = None):
    F.frame_box((L0, T0, R0, T0 + 16), col.night, col.steel)
    kind = FS.FORMAT_SCENES.get((scene or {}).get("type"), ("usage",))[0]
    F.text(SHOW.get(kind, "USAGE REPORT"), L0 + 5, T0 + 5, 1, col.white, role="kicker")
    wk = c.d(c.facts["episode"]["week"]).upper()
    w = X.text_width(wk, 1) + 10
    tw = X.text_width(c.team, 1) + 10
    F.rect((R0 - w - tw - 2, T0 + 13, R0 - w - 2, T0 + 15), col.team)
    F.rect((R0 - w - 1, T0 + 13, R0 - 1, T0 + 15), col.team2)
    F.text(c.team, R0 - w - tw / 2 - 2, T0 + 4, 1, col.white, anchor="c", role="kicker")
    F.text(wk, R0 - w / 2 - 1, T0 + 4, 1, col.gold, anchor="c", role="kicker", src=c.facts["episode"]["week"])


def _on(bg):
    return P()["white"] if K.contrast(P()["white"], bg) >= K.contrast(P()["night"], bg) else P()["night"]


def title(F, text: str, y: int, col, scale: int = 2, color=None):
    """A title on a dark bar, like a 16-bit menu header (type on turf alone doesn't read)."""
    lines = X.wrap(text.upper(), R0 - L0 - 8, scale)
    lh = 7 * scale + 4
    F.rect((L0, y - 3, R0, y + len(lines) * lh), col.night)
    F.rect((L0, y + len(lines) * lh, R0, y + len(lines) * lh + 1), col.team)
    for i, line in enumerate(lines):
        F.text(line, CX, y + i * lh, scale, color or col.gold, anchor="c", role="title")
    return y + len(lines) * lh + 2


def meter(F, x0, y, value: float, color, col, segs: int = 20, seg_w: int = 7, h: int = 8, shown: float = 1.0):
    """A segmented meter on a 0-100% scale: every segment is 5%."""
    on = int(round(value * segs))
    lit = min(on, int(math.floor(shown * segs + 1e-9)))
    for i in range(segs):
        x = x0 + i * (seg_w + 1)
        F.rect((x, y, x + seg_w, y + h), color if i < lit else col.slate)
    return x0 + segs * (seg_w + 1)


# ------------------------------------------------------------------------------------------ scenes

def scene_player_card(F, c, t, dur, spec, col):
    p = c.p
    m = p["metrics"][0]
    y = title(F, "PLAYER FILE", 72, col)
    k = opened(t + 0.2, 0.0)
    y0, y1 = y + 2, 262
    F.frame_box((L0, y0, R0, y0 + (y1 - y0) * k), col.navy, col.steel, shadow=col.night)
    if k < 1:
        return
    # the sprite on a patch of turf
    F.rect((L0 + 6, y0 + 92, L0 + 76, y0 + 100), col.turf)
    F.rect((L0 + 6, y0 + 92, L0 + 76, y0 + 92), col.chalk)
    bob = 1 if (int(t * FPS_ART) // 8) % 2 else 0           # an idle bob, two frames up, two down
    spr = X.player(col.team, col.team2, str(p.get("jersey") or ""), 84)
    F.sprite(spr, L0 + 41 - spr.width // 2, y0 + 96 - spr.height - bob)
    first, last = p["name"].split()[0].upper(), " ".join(p["name"].split()[1:]).upper()
    xr = L0 + 84
    F.text(first, xr, y0 + 12, 1, col.mist, role="label")
    room = R0 - xr - 6
    sc = 3 if X.text_width(last, 3) <= room else 2
    name_lines = [last] if X.text_width(last, sc) <= room else X.wrap(last, room, 2)
    for i, line in enumerate(name_lines):
        F.text(line, xr, y0 + 24 + i * 16, sc, col.white, role="title", shadow=col.night)
    yb = y0 + 24 + (len(name_lines) - 1) * 16 + 7 * sc + 8
    F.text(f"{p['position']} · {c.d(p['team_name']).upper()}", xr, yb, 1, col.mist, role="label", src=p["team_name"])
    if p.get("jersey"):
        F.text(f"#{p['jersey']}", xr, yb + 14, 2, col.gold, role="label", src="roster")
    # the stat block: a meter that drains as the new number lands
    crash = c.at(word=spec.get("crash_word") or "", default=0.45) if spec.get("crash_word") else 0.45
    sy = y0 + 100
    F.rect((L0 + 6, sy, R0 - 6, sy + 1), col.steel)
    F.text(m["label"].upper(), L0 + 8, sy + 6, 1, col.mist, role="label")
    b, r = c.claims[m["baseline"]]["value"], c.claims[m["recent"]]["value"]
    drained = appear(t, crash)
    drain = min(1.0, max(0.0, (step(t) - crash) / 0.4)) if drained else 0.0
    shown_val = b - (b - r) * drain
    meter(F, L0 + 8, sy + 17, shown_val if drained else b, col.data if drained else col.gold, col)
    F.text(c.d(m["baseline"]), L0 + 8, sy + 29, 3, col.mist if drained else col.white, role="number",
           src=m["baseline"])
    F.text(weeks(c, m["baseline"]), L0 + 8, sy + 54, 1, col.mist, role="axis", src="claim-weeks")
    if drained:
        xa = L0 + 8 + X.text_width(c.d(m["baseline"]), 3) + 8
        F.text("→", xa, sy + 29, 3, col.mist, role="label")
        xn = xa + X.text_width("→", 3) + 8
        F.text(c.d(m["recent"]), xn, sy + 29, 3, col.data, role="number", src=m["recent"], shadow=col.night)
        F.text(weeks(c, m["recent"]), xn, sy + 54, 1, col.mist, role="axis", src="claim-weeks")
    turn = next((tok["start"] for i, tok in enumerate(c.tokens) if i > 0 and c.tokens[i - 1]["text"][-1:] in ".?!"),
                dur * 0.5)
    if appear(t, turn) and m.get("change"):
        txt = c.d(m["change"]).upper().replace("−", "-")
        w = X.text_width(txt, 1) + 8
        F.rect((R0 - 8 - w, sy + 4, R0 - 8, sy + 14), col.data)
        F.text(txt, R0 - 8 - w / 2, sy + 6, 1, _on(col.data), anchor="c", role="number", src=m["change"])


def scene_target_balls(F, c, t, dur, spec, col):
    games = c.p["counts"]["games"]
    k_split = spec.get("split") or len(games)
    vals = [c.claims[g["value"]]["value"] for g in games]
    y = title(F, "TARGETS PER GAME", 72, col)
    F.frame_box((L0, y + 2, R0, 262), col.navy, col.steel, shadow=col.night)      # a scoreboard screen, not turf
    early = vals[:k_split]
    ghost_to = round(sum(early) / len(early)) if early else 0
    n = len(games)
    pitch = (R0 - L0) / n
    base = 222
    stepy = 8
    for i, (g, v) in enumerate(zip(games, vals)):
        ta = c.at(g["value"], default=0.2 + i * 0.35) - 0.12
        if not appear(t, ta):
            continue
        cx = L0 + pitch * (i + 0.5)
        late = i >= k_split
        if late:
            for j in range(v, max(ghost_to, v)):
                F.sprite(X.football(True), cx - 6, base - 7 - j * stepy)
        for j in range(v):
            drop = max(0, int((ta + 0.03 * j + 0.2 - step(t)) * 120))
            F.sprite(X.football(), cx - 6, base - 7 - j * stepy - drop)
        top = max(v, ghost_to if late else 0)
        F.text(c.d(g["value"]), cx, base - 7 - top * stepy - 18, 2, col.data if late else col.white, anchor="c",
               role="number", src=g["value"], shadow=col.night)
        F.text(f"WK {g['week']}", cx, base + 6, 1, col.white, anchor="c", role="axis", src="week", shadow=col.night)
    if k_split < n and appear(t, c.at(word="since", default=99)):
        x0 = int(L0 + pitch * k_split + 3)
        x1 = int(L0 + pitch * n - 3)
        F.rect((x0, base + 17, x1, base + 19), col.data)
        F.text(f"SINCE WK {games[k_split]['week']}", (x0 + x1) / 2, base + 24, 1, col.white, anchor="c", role="label",
               src="week", shadow=col.night)


def scene_split(F, c, t, dur, spec, col):
    p = c.p
    ts_m = p["metrics"][0]
    sn = next((m for m in p["metrics"] if m["column"] == "offense_pct"), None)
    twist = spec.get("role") == "twist"
    title(F, "BEFORE VS NOW", 72, col)
    blocks = [("GETTING THE BALL", ts_m, col.data, 0.0 if twist else c.at(word="fewer", default=0.4))]
    if sn:
        blocks.append(("ON THE FIELD", sn, col.white, c.at(sn["recent"], default=c.at(word="snaps", default=0.6))))
    y = 92
    for label, m, color, at in blocks:
        k = opened(t, at - 0.1)
        F.frame_box((L0, y, R0, y + 54 * k), col.navy, col.steel, shadow=col.night)
        if k >= 1:
            F.text(label, L0 + 6, y + 5, 1, color, role="label")
            for j, (lab, cid, cc) in enumerate((("BEFORE", m["baseline"], col.mist), ("NOW", m["recent"], color))):
                yy = y + 18 + j * 18
                F.text(lab, L0 + 6, yy + 1, 1, col.mist, role="label")
                end = meter(F, L0 + 40, yy, c.claims[cid]["value"], cc, col, seg_w=3, shown=min(1, (step(t) - at) / 0.5))
                F.text(c.d(cid), end + 4, yy - 3, 2, cc, role="number", src=cid)
        y += 60
    if sn and appear(t, c.at(word=spec.get("stamp_word", "same"), default=99) - 0.05):
        lines = X.wrap(spec.get("stamp", "ABOUT THE SAME"), R0 - L0 - 12, 2)
        w = max(X.text_width(line, 2) for line in lines) + 12
        F.frame_box((CX - w / 2, y + 6, CX + w / 2, y + 12 + 18 * len(lines)), col.gold, col.night, shadow=col.night)
        for i, line in enumerate(lines):
            F.text(line, CX, y + 11 + 18 * i, 2, col.night, anchor="c", role="label")


def scene_volume(F, c, t, dur, spec, col):
    games = c.p["counts"]["games"]
    team = [g for g in games if g.get("team_targets")]
    vals = [c.claims[g["team_targets"]]["value"] for g in team]
    y = title(F, f"{c.d(c.p['team_name'])} TARGETS PER GAME", 70, col)
    F.frame_box((L0, y + 2, R0, 262), col.navy, col.steel, shadow=col.night)
    top = K.nice_max(max(vals + [1]), 10)
    n = len(team)
    pitch = (R0 - L0) / n
    base, hmax = 196, 196 - y - 30
    t_low = c.at(team[-1]["team_targets"], default=0.5)
    m = c.p["metrics"][0]
    t_share = c.at(m["baseline"], default=dur * 0.6)
    for i, (g, v) in enumerate(zip(team, vals)):
        if not appear(t, 0.05 + i * 0.07):
            continue
        grow = min(1.0, (step(t) - 0.05 - i * 0.07) / 0.3)
        h = int(round(hmax * v / top * grow))
        cx = L0 + pitch * (i + 0.5)
        lit = i == n - 1 and t >= t_low - 0.05
        x0, x1 = int(cx - pitch * 0.32), int(cx + pitch * 0.32)
        F.rect((x0, base - h, x1, base), col.gold if lit else col.steel)
        for yy in range(base - 4, base - h, -4):                     # brick courses
            F.rect((x0, yy, x1, yy + 1), col.navy if not lit else col.leather)
        F.text(c.d(g["team_targets"]), cx, base - h - (18 if lit else 11), 1 if not lit else 2, col.gold if lit else col.white,
               anchor="c", role="number", src=g["team_targets"])
        F.text(f"WK {g['week']}", cx, base + 4, 1, col.white, anchor="c", role="axis", src="week", shadow=col.night)
    k = opened(t, t_share - 0.1)
    if k > 0:
        F.frame_box((L0, 210, R0, 210 + 48 * k), col.night, col.data, shadow=col.night)
        if k >= 1:
            F.text("BUT HIS SHARE STILL FELL", CX, 216, 1, col.mist, anchor="c", role="label")
            txt = [c.d(m["baseline"]), "→", c.d(m["recent"])]
            w = sum(X.text_width(s, 3) for s in txt) + 16
            x = CX - w / 2
            for s, cc, src in zip(txt, (col.mist, col.mist, col.data), (m["baseline"], None, m["recent"])):
                F.text(s, x, 228, 3, cc, role="number" if src else "label", src=src)
                x += X.text_width(s, 3) + 8


def scene_lineup(F, c, t, dur, spec, col):
    p = c.p
    m = p["metrics"][0]
    title(F, "YOUR LINEUP", 72, col, scale=2)
    slots = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX"]
    mine = slots.index(p["position"]) if p["position"] in slots else 6
    sched = next((s for s in p["schedule"] if s["week"] == c.facts["week"]), None)
    k = opened(t, 0.0)
    F.frame_box((L0, 92, R0, 92 + 118 * k), col.navy, col.steel, shadow=col.night)
    if k < 1:
        return
    y = 98
    for i, slot in enumerate(slots):
        hot = i == mine
        if hot:
            F.rect((L0 + 3, y - 2, R0 - 3, y + 30), col.slate)
            if (int(t * 4) % 2) == 0:
                F.text("▶", L0 + 5, y + 4, 2, col.gold, role="label")
            F.text(slot, L0 + 18, y + 4, 2, col.gold, role="label")
            last = last_name(p["name"]).upper()
            F.text(last, L0 + 66, y + 1, 2, col.white, role="label")
            opp = ""
            if sched and sched["opponent_value"] != "bye":
                opp = f" {'VS' if sched.get('home') else 'AT'} {TEAM_NAMES[sched['opponent_value']].upper()}"
            F.text(f"{p['team']}{opp}", L0 + 66, y + 19, 1, col.mist, role="label", src="schedule")
            y += 34
        else:
            F.text(slot, L0 + 18, y + 2, 1, col.mist, role="label")
            F.rect((L0 + 66, y + 4, L0 + 140, y + 5), col.slate)
            y += 13
    if appear(t, c.at(word="watch", default=dur * 0.35) - 0.05):
        y = 218
        lines = X.wrap(f"WATCH: BACK TOWARD {c.d(m['baseline'])}?", R0 - L0 - 8, 2)
        F.frame_box((L0, y - 4, R0, y + len(lines) * 18), col.night, col.gold)
        for i, line in enumerate(lines):
            F.text(line, CX, y + i * 18, 2, col.gold, anchor="c", role="label", src=m["baseline"])


def scene_decision(F, c, t, dur, spec, col):
    q = " ".join(spec.get("question", ["?"]))
    lines = X.wrap(q.upper(), R0 - L0 - 12, 3)
    y = 84
    F.frame_box((L0, y - 8, R0, y + len(lines) * 26 + 2), col.night, col.team, shadow=col.night)
    for i, line in enumerate(lines):
        F.text(line, CX, y, 3, col.gold if i == len(lines) - 1 else col.white, anchor="c", role="title")
        y += 26
    k = opened(t, 0.3)
    y += 10
    F.frame_box((L0 + 10, y, R0 - 10, y + 56 * k), col.navy, col.white, shadow=col.night)
    if k >= 1:
        for i, (label, cc) in enumerate(zip(spec.get("options", ["START", "SIT"]), (col.green, col.red))):
            yy = y + 10 + i * 22
            F.text(label, L0 + 34, yy, 2, cc, role="label")
            sel = (int(t * 2) % 2) == i                       # the cursor hops between the two choices
            if sel:
                F.text("▶", L0 + 20, yy, 2, col.white, role="label")


def scene_presence(F, c, t, dur, spec, col):
    sn = next((m for m in c.p["metrics"] if m["column"] == "offense_pct"), None)
    y = title(F, "ON THE FIELD", 72, col)
    F.rect((L0, y - 2, R0, y + 10), col.night)
    F.text("EVERY BLOCK = 5% OF SNAPS", CX, y, 1, col.mist, anchor="c", role="label", src="legend")
    for j, (lab, cid, cc) in enumerate((("BEFORE", sn["baseline"], col.white), ("NOW", sn["recent"], col.data))):
        ta = c.at(cid, default=0.3 + j * 0.8)
        y = 112 + j * 76
        k = opened(t, ta - 0.2)
        F.frame_box((L0, y, R0, y + 66 * k), col.navy, col.steel, shadow=col.night)
        if k < 1:
            continue
        F.text(f"{lab} · {weeks(c, cid)}", L0 + 6, y + 6, 1, col.mist, role="label", src="claim-weeks")
        F.text(c.d(cid), R0 - 6, y + 6, 3, cc, anchor="r", role="number", src=cid)
        filled = int(round(c.claims[cid]["value"] * 20))
        for i in range(20):
            x = L0 + 8 + (i % 10) * 17
            yy = y + 32 + (i // 10) * 16
            F.rect((x, yy, x + 14, yy + 12), cc if i < filled and (step(t) - ta) * 40 > i else col.slate)


def scene_teammate_out(F, c, t, dur, spec, col):
    p, tm = c.p, c.p["teammate"]
    m = p["metrics"][0]
    tc = t + 5 if spec.get("phase") == 2 else t
    y0 = title(F, "WHY THE ROLE OPENED UP", 70, col)
    F.frame_box((L0, y0 + 2, R0, 249), col.navy, col.steel, shadow=col.night)
    weeks_ = [s["week"] for s in m["series"]]
    missed = set(tm["missed_values"])
    tm_series = {x["week"]: x["value"] for x in tm["series"]}
    me = {s["week"]: s["value"] for s in m["series"]}
    n = len(weeks_)
    cw = (R0 - L0 - 8 - 4 * (n - 1)) // n
    for r, (name, series, is_tm) in enumerate(((tm["name"], tm_series, True), (p["name"], me, False))):
        y = y0 + 8 + r * 68
        F.text(last_name(name).upper(), L0 + 6, y, 2, col.white, role="label")
        for i, w in enumerate(weeks_):
            if not appear(tc, 0.15 + r * 0.25 + i * 0.1):
                continue
            x = L0 + 4 + i * (cw + 4)
            box = (x, y + 20, x + cw, y + 48)
            if is_tm and w in missed:
                F.frame_box(box, col.red, col.night)
                F.text("OUT", x + cw / 2, y + 30, 1, _on(col.red), anchor="c", role="label")
            else:
                val = series.get(w)
                hot = (not is_tm and w in p["recent_weeks"]) or (spec.get("phase") == 2 and is_tm and val is not None)
                F.frame_box(box, col.data if hot else col.navy, col.steel)
                F.text(render("pct", val)[0] if val is not None else "-", x + cw / 2, y + 30, 1,
                       _on(col.data) if hot else col.white, anchor="c", role="number", src="snapshot:series")
            if r == 1:
                F.text(f"WK {w}", x + cw / 2, y + 54, 1, col.mist, anchor="c", role="axis", src="week")
    note = None
    if spec.get("phase") == 2:
        note = (f"BEFORE: {c.d(tm['role']).upper()}", col.gold, tm["role"])
    elif "reason" in tm and appear(t, 0.6):
        note = (f"OUT: {c.d(tm['reason']).upper()}", col.red, tm["reason"])
    if note:
        lines = X.wrap(note[0], R0 - L0 - 8, 1)
        F.frame_box((L0, 252, R0, 258 + 9 * len(lines)), col.night, note[1])
        for i, line in enumerate(lines):
            F.text(line, CX, 255 + 9 * i, 1, note[1], anchor="c", role="label", src=note[2])


def scene_status_stamp(F, c, t, dur, spec, col):
    tm = c.p["teammate"]
    code, week, detail = c.claims[tm["status"]]["value"]
    big = {"ir": "INJURED RESERVE", "none": "NOT ON THE REPORT"}.get(code) or (
        f"LISTED {detail.upper()}" if code == "designation" else
        {"Did Not Participate In Practice": "NOT PRACTICING", "Limited Participation in Practice": "LIMITED",
         "Full Participation in Practice": "PRACTICING FULLY"}.get(detail, detail.upper()))
    cc = col.red if ("NOT" in big or code in ("ir", "designation")) else col.green
    title(F, f"{tm['name']} RIGHT NOW", 72, col)
    k = opened(t, 0.25)
    F.frame_box((L0, 120, R0, 120 + 96 * k), col.night, cc, shadow=col.night)
    if k >= 1:
        sc = 3 if len(X.wrap(big, R0 - L0 - 12, 3)) <= 2 else 2
        for i, line in enumerate(X.wrap(big, R0 - L0 - 12, sc)):
            F.text(line, CX, 132 + i * (7 * sc + 5), sc, cc, anchor="c", role="title")
        src = "NFL ROSTER STATUS" if code == "ir" else "INJURY REPORT"
        F.text(f"{src} · WEEK {week}", CX, 202, 1, col.mist, anchor="c", role="label", src=f"{tm['status']}:value")


SCENES = {"player_card": scene_player_card, "target_balls": scene_target_balls, "split": scene_split,
          "volume": scene_volume, "lineup": scene_lineup, "decision": scene_decision, "presence": scene_presence,
          "teammate_out": scene_teammate_out, "status_stamp": scene_status_stamp}


# ------------------------------------------------------------------------------------------ render

def draw(scene: dict, t: float, dur: float, c, measure: bool = False) -> X.PixelFrame:
    F = X.PixelFrame(S, measure)
    col = C(c)
    background(F, t + scene.get("_start", 0))
    hud(F, c, col, scene)
    SCENES[scene["type"]](F, c, t, dur, scene, col)
    return F


def caption(F: X.PixelFrame, words: list[dict], active: int, c) -> None:
    """A dialog box: two lines of the pixel font at x2, the spoken word in gold."""
    col = C(c)
    texts = [w["text"].upper() for w in words]
    lines, cur = [], []
    for i, w in enumerate(texts):
        if cur and X.text_width(" ".join(texts[j] for j in cur + [i]), 2) > R0 - L0 - 4:
            lines.append(cur)
            cur = []
        cur.append(i)
    if cur:
        lines.append(cur)
    if len(lines) > 2:                             # page the box: the lines holding the spoken word
        page = next(k for k in range(0, len(lines), 2) if active in sum(lines[k:k + 2], []))
        more = page + 2 < len(lines)
        lines = lines[page:page + 2]
    else:
        more = False
    F.frame_box(CAPTION_BOX[:2] + (CAPTION_BOX[2] - 1, CAPTION_BOX[3] - 1), col.night, col.white)
    F.rect((L0 - 2, 268, R0 + 2, 269), col.slate)
    if more:
        F.text("▶", R0 - 6, 298, 1, col.gold, role="caption")
    y = 286 - (len(lines) * 18 - 4) // 2
    for line in lines:
        total = X.text_width(" ".join(texts[i] for i in line), 2)
        x = CX - total / 2
        for i in line:
            color = col.gold if i == active else (col.data if words[i]["emph"] else col.white)
            F.text(texts[i], x, y, 2, color, role="caption")
            x += X.text_width(texts[i] + " ", 2) + 2
        y += 18


def transition(prev: Image.Image, cur: Image.Image, k: float) -> Image.Image:
    """A stepped palette fade, the 16-bit way: the old screen steps down to night in four steps, the new one
    steps up in four. Every step is a whole frame of one brightness, so the art grid is never broken."""
    night = np.array(P()["night"], np.float32)
    if k < 0.5:
        img, level = prev, 1 - math.floor(k * 2 * 4 + 1e-9) / 4          # 1, .75, .5, .25
    else:
        img, level = cur, math.floor((k - 0.5) * 2 * 4 + 1e-9) / 4 + 0.25  # .25, .5, .75, 1
    if level >= 1:
        return img
    a = np.asarray(img, np.float32)
    return Image.fromarray((night + (a - night) * level).astype(np.uint8), "RGB")


def caption_spec() -> dict:
    # two lines on screen; a longer card pages (two pages at most for the 2-5 word cues the timeline makes)
    return {"font": "pixel5x7", "font_size": 2, "max_width_px": (R0 - L0 - 4), "max_lines": 4, "case": "upper",
            "lines_on_screen": 2}


from . import pixel_formats  # noqa: E402,F401  (registers the format scenes in SCENES)
