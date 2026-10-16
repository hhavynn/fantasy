"""Scenes for the shows in studio/shows.py, drawn with the component library (premium and broadcast).

  red_flag  flag_open (the warning, the player revealed with it or a beat later), flag_chart (ONE chart that
            evolves through the video: his target share game by game against his own baseline; then his snaps
            beneath it; then the low-volume week marked; then the line where the flag comes down)
  heist     heist_alert (the absence, as an alert), heist_meter (his share of what was left, filling a 0-100%
            meter; then the catch, his snaps), heist_split (the old split, the absent player's slice), heist_claim
            (his card dropping into a roster slot)
  myth      myth_card (the assumption, as a statement), myth_tile (one huge reality figure per beat, the earlier
            ones kept as small tiles above), myth_rule (the rule, two lines)

No scene invents a number: every figure is a claim display (c.d) or a snapshot value through numbers.render,
logged with its source. Axes start at zero; a meter is 0-100%.
"""

from __future__ import annotations

from ..numbers import render
from . import kit as K
from .kit import W, clamp, mix, pal
from .vector import SCENES, _on, portrait
from .vector_formats import _x, card, chip, figure, identity, inline, other_portrait


def _flag(F, x, y, size, color, k=1.0):
    """A flag: a pole and a pennant, drawn as shapes."""
    if k <= 0:
        return
    F.rect((x, y, x + size * 0.07, y + size), color, radius=2, alpha=k)
    F.poly([(x + size * 0.07, y), (x + size * 0.8, y + size * 0.2), (x + size * 0.07, y + size * 0.42)], color, k)


def _metric(c, column):
    return next((m for m in c.p["metrics"] if m["column"] == column), None)


# --------------------------------------------------------------------------------------------- red flag

def scene_flag_open(F, L, c, t, dur, spec):
    x0, x1 = _x(L)
    k = L.e(t + 0.2, 0.0)
    _flag(F, x0, 330, 92, L.down, k)
    L.text(F, "Red flag report", x0 + 92, 352, "title", L.down, alpha=k, role="title", display=True,
           max_width=x1 - x0 - 92)
    if spec.get("reveal"):                       # on frame 0 the player is already there: no empty opening
        lead = 0.3 if spec.get("stage") != "reveal" else 0.0
        kr = L.e(t + lead, 0.0)
        identity(F, L, c, t + lead, y=480)
        figure(F, L, c, portrait(c, 520), W / 2 + 120, 1250, 560, 600, alpha=kr)
        return
    # the problem before the player: his snaps, and a silhouette where the name will go
    sn = spec["claims"][0] if spec.get("claims") else None
    if sn:
        inline(F, L, [(c.d(sn), "display", L.ink, sn, "number"), ("of the snaps", "label", L.muted, None, "label")],
               x0, 520, alpha=k, c=c)
    card(F, L, (x0, 760, x1, 1240), alpha=0.9 * k)
    L.text(F, "?", (x0 + x1) / 2, 900, "display", L.faint, anchor="c", alpha=k, role="title")
    L.text(F, "Who is it?", (x0 + x1) / 2, 1110, "label", L.muted, anchor="c", alpha=k, role="label")


def scene_flag_chart(F, L, c, t, dur, spec):
    """His target share game by game against his own baseline, one chart evolving through four stages."""
    x0, x1 = _x(L)
    m = _metric(c, spec.get("metric", "target_share"))
    stage = spec.get("stage", "signal")
    weeks = [s["week"] for s in m["series"]]
    vals = [s["value"] for s in m["series"]]
    base, rec = c.claims[m["baseline"]]["value"], c.claims[m["recent"]]["value"]
    _flag(F, x0, 318, 40, L.down)
    L.text(F, f"Red flag · {c.p['name']}", x0 + 52, 322, "kicker", L.down, role="kicker", c=c)
    title = {"signal": "Target share, game by game", "role": "The snaps held. The targets didn't.",
             "context": "One week came in light", "call": "Where the flag comes down"}[stage]
    L.text(F, title, x0, 380, "title", L.ink, alpha=L.e(t, 0.0), role="title", c=c, display=True, max_width=x1 - x0)
    top = K.nice_max(max([v for v in vals if v is not None] + [base]), 0.1)
    cx0, cx1, cy0, cy1 = x0 + 90, x1 - 70, 580, 1060     # inset so end labels stay inside the safe zone
    if stage == "role":
        cy1 = 900
    xs = [cx0 + (cx1 - cx0) * i / max(len(weeks) - 1, 1) for i in range(len(weeks))]
    Y = lambda v: cy1 - (cy1 - cy0) * (v or 0) / top                      # noqa: E731
    F.line((cx0 - 30, cy1, cx1 + 30, cy1), L.line, 3)
    L.text(F, "0%", x0, cy1 - 14, "label", L.faint, role="axis", src="axis")
    # his own baseline, dashed, with its claim
    yb = Y(base)
    hi = stage == "call"
    F.dashed(cx0 - 30, cx1 + 30, yb, L.hi if hi else L.muted, 4 if hi else 3)
    lab = f"{K.weeks_label(K.claim_weeks(c, m['baseline']))}: {c.d(m['baseline'])}"
    L.text(F, ("Flag clears near " + c.d(m["baseline"])) if hi else lab, cx1 + 30, yb - 56, "label",
           L.hi if hi else L.muted, anchor="r", role="label", src=m["baseline"], c=c)
    if hi:
        _flag(F, cx1 + 6, yb - 112, 44, L.hi)
    recent = set(c.p["recent_weeks"])
    low = spec.get("claims", [None])[-2] if stage == "context" else None
    for i, (w, v) in enumerate(zip(weeks, vals)):
        kk = L.e(t, 0.05 + i * 0.12, 0.45) if stage == "signal" else 1.0
        if kk <= 0 or v is None:
            continue
        if i:
            pv = vals[i - 1]
            if pv is not None:
                col = L.down if w in recent else L.ink
                xe = xs[i - 1] + (xs[i] - xs[i - 1]) * kk
                ye = Y(pv) + (Y(v) - Y(pv)) * kk
                F.line((xs[i - 1], Y(pv), xe, ye), col, 7)
        if kk >= 0.99:
            col = L.down if w in recent else L.ink
            F.d.ellipse((xs[i] - 12, Y(v) - 12, xs[i] + 12, Y(v) + 12), fill=col)
            L.text(F, render("pct", v)[0], xs[i], Y(v) - 62, "number_s", col, anchor="c", role="number",
                   src="snapshot:series", tabular=True)
        L.text(F, f"Wk {w}", xs[i], cy1 + 22, "label", L.muted, anchor="c", role="axis", src="week", alpha=kk)
    if stage == "signal":
        kr = L.e(t, 0.9, 0.5)
        if kr > 0:
            chip(F, L, f"{K.weeks_label(K.claim_weeks(c, m['recent']))}: {c.d(m['recent'])}", x0, 1160, L.down,
                 key="number_s", alpha=kr, src=m["recent"], role="number", c=c)
    if stage == "role":                          # his snaps beneath, game by game: the role is intact
        sn = _metric(c, "offense_pct")
        L.text(F, "Snap share", x0, 1000, "kicker", L.muted, role="kicker")
        for i, s in enumerate(sn["series"]):
            kk = L.e(t, 0.1 + i * 0.08, 0.4)
            if s["value"] is None:
                continue
            card(F, L, (xs[i] - 70, 1050, xs[i] + 70, 1160), alpha=kk)
            L.text(F, render("pct", s["value"])[0], xs[i], 1084, "number_s", L.ink, anchor="c", alpha=kk,
                   role="number", src="snapshot:series", tabular=True)
        chip(F, L, f"Now {c.d(sn['recent'])} of the snaps", x0, 1200, L.neutral, key="number_s", alpha=L.e(t, 0.6),
             src=sn["recent"],
             role="number", c=c)
    if stage == "context" and low:
        i = len(weeks) - 1
        F.rect((xs[i] - 60, cy0 - 20, xs[i] + 60, cy1), L.hi, radius=L.tk.radius.bar, alpha=0.10)
        team, week = spec["claims"][-2], spec["claims"][-1]
        inline(F, L, [(c.d(team), "number_m", L.hi, team, "number"),
                      (f"{c.d(c.p['team_name'])} targets in {c.d(week)}", "label", L.muted, week, "label")],
               x0, 1160, alpha=L.e(t, 0.15), c=c)
    if stage == "call":
        n = spec["claims"][-1]
        L.text(F, f"{c.d(n)} games: a warning, not a verdict", x0, 1170, "label", L.muted, alpha=L.e(t, 0.2),
               role="label", src=n, c=c)


# ------------------------------------------------------------------------------------------------ heist

def scene_heist_alert(F, L, c, t, dur, spec):
    x0, x1 = _x(L)
    tm = c.p["teammate"]
    k = L.e(t + 0.15, 0.0)
    pulse = 1 + 0.04 * max(0.0, 1 - abs(t - 0.25) * 6)
    chip(F, L, "Opportunity alert", x0, 330, L.up, key="kicker", alpha=k, role="kicker")
    L.text(F, f"{tm['name']}", x0, 440, "display", L.ink, alpha=k, role="title", c=c, display=True,
           max_width=x1 - x0, scale=pulse)
    img = other_portrait(c, tm.get("espn_id"), tm.get("jersey"), tm["name"], 460)
    figure(F, L, c, img, W / 2, 1180, 520, 560, alpha=k, dim=0.55)
    kc = L.e(t, 0.3, 0.35)
    if kc > 0:
        chip(F, L, c.d(tm["status"]).capitalize(), W / 2, 1200, L.down, key="number_s", anchor="c", alpha=kc,
             src=tm["status"], role="number", c=c)


def scene_heist_meter(F, L, c, t, dur, spec):
    """His share of what the teammate left, filling a 0-100% meter; or, for the catch, his snaps."""
    x0, x1 = _x(L)
    stage = spec.get("stage", "role")
    tm = c.p["teammate"]
    m = _metric(c, tm["column"]) if stage == "role" else _metric(c, "offense_pct")
    y = identity(F, L, c, t, y=310, slide=stage == "role")
    base, rec = c.claims[m["baseline"]]["value"], c.claims[m["recent"]]["value"]
    k = L.e(t, 0.15, 0.8, "move")
    what = (f"of the {c.d(c.p['team_name'])}' running back carries" if m["column"] == "backfield_rush_share"
            else f"of his team's {m['label'].replace(' share', 's')}" if m["column"] != "offense_pct" else "of the snaps")
    color = L.up if stage == "role" else pal(L.tk, "caution") if hasattr(L.tk.palette, "caution") else L.hi
    nb = L.text(F, c.d(m["recent"]), x0, y + 60, "number_xl", color, alpha=L.e(t, 0.1), role="number", src=m["recent"],
                tabular=True, display=True)
    L.text(F, what, x0, nb[3] + 16, "label", L.muted, alpha=L.e(t, 0.1), role="label", c=c, max_width=x1 - x0)
    my = y + 380
    F.rect((x0, my, x1, my + 64), L.neutral, radius=L.tk.radius.bar)
    if stage == "role":
        F.rect((x0, my, x0 + (x1 - x0) * (base + (rec - base) * k), my + 64), color, radius=L.tk.radius.bar)
        xb = x0 + (x1 - x0) * base
        F.rect((xb - 3, my - 22, xb + 3, my + 86), L.ink, alpha=0.8)
        txt = f"Before: {c.d(m['baseline'])}"
        half = K.measure(txt.upper() if L.broadcast else txt, *L.ty("label")[:2], L.ty("label")[2]) / 2
        L.text(F, txt, min(max(xb, x0 + half), x1 - half), my + 110, "label", L.muted, anchor="c", role="label",
               src=m["baseline"], c=c)
    else:                                         # the catch: the part of the snaps he is not on the field for
        F.rect((x0, my, x0 + (x1 - x0) * rec * k, my + 64), color, radius=L.tk.radius.bar)
        L.text(F, "A big role, not an every-down one", x0, my + 150, "label", color, alpha=L.e(t, 0.5),
               role="label", c=c)
    L.text(F, "0%", x0, my + 76, "label", L.faint, role="axis", src="axis", alpha=0.0 if stage == "role" else 1.0)
    L.text(F, "100%", x1, my + 76, "label", L.faint, anchor="r", role="axis", src="axis",
           alpha=0.0 if stage == "role" else 1.0)


def scene_heist_split(F, L, c, t, dur, spec):
    """The old split: the absent player's slice, and his own, on one 0-100% bar; then his share now."""
    x0, x1 = _x(L)
    tm = c.p["teammate"]
    m = _metric(c, tm["column"])
    L.text(F, "Before he got hurt", x0, 330, "kicker", L.muted, role="kicker")
    L.text(F, f"{c.d(c.p['team_name'])} running back carries" if m["column"] == "backfield_rush_share"
           else f"{c.d(c.p['team_name'])} {m['label']}", x0, 388, "title", L.ink, role="title", c=c, display=True,
           max_width=x1 - x0)
    tshare = c.claims[tm["role"]]["value"]
    mine0, mine1 = c.claims[m["baseline"]]["value"], c.claims[m["recent"]]["value"]
    k1 = L.e(t, 0.1, 0.5)
    w = x1 - x0
    rows = [(620, f"{tm['name']}", tshare, L.muted, tm["role"]), (820, c.p["name"], mine0, L.ink, m["baseline"])]
    for y, who, v, col, cid in rows:
        L.text(F, who, x0, y, "label", L.ink, role="label", c=c, alpha=k1)
        F.rect((x0, y + 60, x1, y + 120), L.neutral, radius=L.tk.radius.bar, alpha=k1)
        F.rect((x0, y + 60, x0 + w * v * k1, y + 120), col, radius=L.tk.radius.bar, alpha=k1)
        L.text(F, c.d(cid), x1, y - 6, "number_s", col if col != L.muted else L.ink, anchor="r", role="number",
               src=cid, alpha=k1, tabular=True)
    k2 = L.e(t, 0.7, 0.6, "move")
    if k2 > 0:
        L.text(F, "Now", x0, 1040, "kicker", L.up, role="kicker", alpha=k2)
        F.rect((x0, 1100, x1, 1160), L.neutral, radius=L.tk.radius.bar, alpha=k2)
        F.rect((x0, 1100, x0 + w * (mine0 + (mine1 - mine0) * k2), 1160), L.up, radius=L.tk.radius.bar, alpha=k2)
        L.text(F, f"{c.p['name']}: {c.d(m['recent'])}", x1, 1034, "number_s", L.up, anchor="r", role="number",
               src=m["recent"], alpha=k2, c=c)


def scene_heist_claim(F, L, c, t, dur, spec):
    """His card dropping into a roster slot: the claim."""
    x0, x1 = _x(L)
    tm = c.p["teammate"]
    m = _metric(c, tm["column"])
    L.text(F, "Waiver claim", x0, 330, "kicker", L.up, role="kicker")
    L.text(F, "Your bench", x0, 388, "title", L.ink, role="title", display=True)
    slots = 4
    for i in range(slots):
        y = 560 + i * 150
        card(F, L, (x0, y, x1, y + 124), alpha=0.6)
        if i < slots - 1:
            F.rect((x0 + 40, y + 52, x0 + 300, y + 72), L.neutral, radius=6, alpha=0.6)
    drop = L.e(t, 0.15, 0.55)
    y = 560 + (slots - 1) * 150 - (1 - drop) * 380
    card(F, L, (x0, y, x1, y + 124), alpha=drop, team=L.team(c)[0], fill=L.panel)
    if not L.broadcast:
        F.rect((x0, y, x0 + 8, y + 124), L.up, alpha=drop)
    L.text(F, c.p["name"], x0 + 40, y + 24, "number_s", L.ink, role="label", alpha=drop, c=c, max_width=x1 - x0 - 300)
    L.text(F, f"{c.p['position']} · {c.d(c.p['team_name'])}", x0 + 40, y + 80, "label", L.muted, role="label",
           alpha=drop, src=c.p["team_name"], c=c)
    L.text(F, c.d(m["recent"]), x1 - 40, y + 30, "number_m", L.up, anchor="r", role="number", src=m["recent"],
           alpha=drop, tabular=True)
    kc = L.e(t, 0.75, 0.35)
    if kc > 0:
        chip(F, L, f"{tm['name'].split()[-1]}: {c.d(tm['status'])}", x0, 1200, L.down, alpha=kc, src=tm["status"],
             role="label", c=c)


# ------------------------------------------------------------------------------------------------- myth

def scene_myth_card(F, L, c, t, dur, spec):
    x0, x1 = _x(L)
    k = L.e(t + 0.2, 0.0)
    chip(F, L, "Myth", x0, 330, pal(L.tk, "caution") if hasattr(L.tk.palette, "caution") else L.hi, key="kicker",
         alpha=k, role="kicker")
    L.text(F, "More snaps", x0, 450, "display", L.ink, alpha=k, role="title", display=True, max_width=x1 - x0)
    L.text(F, "= more targets", x0, 450 + L.ty("display")[1] * 1.05, "display", L.muted, alpha=L.e(t, 0.25),
           role="title", display=True, max_width=x1 - x0)
    figure(F, L, c, portrait(c, 460), W / 2 + 150, 1250, 500, 520, alpha=L.e(t, 0.35))
    L.text(F, c.p["name"], x0, 1180, "label", L.ink, role="label", alpha=L.e(t, 0.35), c=c)


def scene_myth_tile(F, L, c, t, dur, spec):
    """Reality, one huge figure per beat; the earlier figures kept as tiles above."""
    x0, x1 = _x(L)
    order = ["snaps", "volume", "share"]
    stage = spec.get("stage", "snaps")
    sn, lead = _metric(c, "offense_pct"), c.p["metrics"][0]
    chip(F, L, "Reality", x0, 330, L.up if stage != "share" else L.down, key="kicker", role="kicker")
    done = order[:order.index(stage)]
    tw = (x1 - x0 - 24) / 2
    for i, st in enumerate(done):                # the evidence so far, small
        tx = x0 + i * (tw + 24)
        card(F, L, (tx, 430, tx + tw, 560))
        if st == "snaps":
            inline(F, L, [(c.d(sn["recent"]), "number_s", L.ink, sn["recent"], "number"),
                          ("of snaps", "label", L.muted, None, "label")], tx + 28, 470, c=c)
        else:
            L.text(F, "Team passing more", tx + 28, 474, "label", L.ink, role="label", max_width=tw - 56)
    k = L.e(t, 0.0, 0.35)
    y = 700
    if stage == "snaps":
        nb = L.text(F, c.d(sn["recent"]), x0, y, "number_xl", L.ink, alpha=k, role="number", src=sn["recent"],
                    tabular=True, display=True)
        L.text(F, "of the snaps", x0, nb[3] + 16, "label", L.muted, alpha=k, role="label")
        chip(F, L, f"Up from {c.d(sn['baseline'])}", x0, nb[3] + 96, L.neutral, key="number_s", alpha=L.e(t, 0.3),
             src=sn["baseline"],
             role="number", c=c)
    elif stage == "volume":
        team_c = [x for x in spec["claims"] if c.claims[x]["source"].get("kind") == "team_targets"]
        week_c = [x for x in spec["claims"] if c.claims[x]["source"].get("kind") == "week_value"]
        col_w = (x1 - x0 - 40) / max(len(team_c), 1)
        for i, (tc, wc) in enumerate(zip(team_c, week_c or [None] * len(team_c))):
            kx = L.e(t, 0.1 + i * 0.18, 0.35)
            cx = x0 + col_w * i
            L.text(F, c.d(tc), cx, y, "number_xl", L.ink, alpha=kx, role="number", src=tc, tabular=True)
            if wc:
                L.text(F, c.d(wc), cx, y + 250, "label", L.muted, alpha=kx, role="label", src=wc, c=c)
        L.text(F, f"{c.d(c.p['team_name'])} targets", x0, y + 320, "label", L.muted, alpha=k, role="label", c=c)
    else:
        big = "number_xl"
        while big != "number_m" and K.measure(c.d(lead["baseline"]) + c.d(lead["recent"]) + " to ",
                                              *L.ty(big)[:2]) > (x1 - x0) * 0.8:
            big = {"number_xl": "number_l", "number_l": "number_m"}[big]
        small = {"number_xl": "number_l", "number_l": "number_m", "number_m": "number_s"}[big]
        inline(F, L, [(c.d(lead["baseline"]), small, L.muted, lead["baseline"], "number"),
                      ("to", "label", L.muted, None, "label"),
                      (c.d(lead["recent"]), big, L.down, lead["recent"], "number")], x0, y, alpha=k, gap=28)
        L.text(F, "his target share", x0, y + L.ty(big)[1] * 1.05, "label", L.muted, alpha=k, role="label")


def scene_myth_rule(F, L, c, t, dur, spec):
    x0, x1 = _x(L)
    L.text(F, "The rule", x0, 330, "kicker", L.hi, role="kicker")
    k1, k2 = L.e(t, 0.0), L.e(t, 0.45)
    L.text(F, "Snaps get you", x0, 470, "title", L.muted, alpha=k1, role="title", display=True, max_width=x1 - x0)
    L.text(F, "on the field.", x0, 470 + 100, "title", L.muted, alpha=k1, role="title", display=True, max_width=x1 - x0)
    L.text(F, "Targets get you", x0, 760, "display", L.ink, alpha=k2, role="title", display=True, max_width=x1 - x0)
    L.text(F, "in the lineup.", x0, 760 + L.ty("display")[1] * 1.05, "display", L.hi, alpha=k2, role="title",
           display=True, max_width=x1 - x0)
    L.text(F, f"{c.p['name']}: don't start him on snaps alone.", x0, 1180, "label", L.ink, alpha=L.e(t, 0.9),
           role="label", c=c, max_width=x1 - x0)


SHOWS = {"flag_open": scene_flag_open, "flag_chart": scene_flag_chart, "heist_alert": scene_heist_alert,
         "heist_meter": scene_heist_meter, "heist_split": scene_heist_split, "heist_claim": scene_heist_claim,
         "myth_card": scene_myth_card, "myth_tile": scene_myth_tile, "myth_rule": scene_myth_rule}
SCENES.update(SHOWS)
