"""The cartoon cast: original characters drawn from code, never likenesses of real people.

Every character is a fictional archetype with an unnatural colour, so nobody can mistake one for a person.
Invented dialogue belongs to these characters only. ``CAST`` is the closed list the parody gate enforces.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

from .. import motion as M

SKIN = {"boss": (64, 196, 180), "pundit": (240, 138, 70), "ref": (176, 142, 240), "fan": (120, 190, 90)}
COAT = {"boss": (34, 44, 66), "pundit": (120, 40, 60), "ref": (20, 20, 22), "fan": (70, 90, 170)}
GOLD_CUP = (245, 196, 60)
CAST = frozenset({"boss", "pundit", "ref", "fan", "trophy", "rumour"})


def _face(d, cx, cy, r, talk: float, sweat: bool) -> None:
    for sx in (-0.38, 0.38):
        ex, ey = cx + sx * r, cy - 0.15 * r
        d.ellipse((ex - 0.2 * r, ey - 0.24 * r, ex + 0.2 * r, ey + 0.24 * r), fill=(255, 255, 255))
        d.ellipse((ex - 0.08 * r, ey - 0.06 * r, ex + 0.1 * r, ey + 0.14 * r), fill=(10, 10, 14))
    open_h = 0.05 * r + 0.3 * r * talk
    d.ellipse((cx - 0.3 * r, cy + 0.34 * r - open_h / 2, cx + 0.3 * r, cy + 0.34 * r + open_h / 2), fill=(30, 14, 18))
    if sweat:
        d.ellipse((cx + r * 0.9, cy - r * 0.5, cx + r * 1.1, cy - r * 0.1), fill=(150, 210, 255))


def person(d, who: str, cx: float, cy: float, s: float, talk: float, sweat: bool) -> None:
    r = 100 * s
    d.rounded_rectangle((cx - 1.2 * r, cy + r * 0.8, cx + 1.2 * r, cy + 3.4 * r), int(36 * s), fill=COAT[who])
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=SKIN[who], outline=(8, 10, 12), width=max(int(5 * s), 2))
    _face(d, cx, cy, r, talk, sweat)
    if who == "boss":
        d.polygon([(cx - 0.4 * r, cy + r), (cx + 0.4 * r, cy + r), (cx, cy + 2.2 * r)], fill=(214, 60, 70))
    elif who == "pundit":
        d.polygon([(cx - 0.5 * r, cy + 0.9 * r), (cx, cy + 1.15 * r), (cx - 0.5 * r, cy + 1.4 * r)], fill=(245, 196, 60))
        d.polygon([(cx + 0.5 * r, cy + 0.9 * r), (cx, cy + 1.15 * r), (cx + 0.5 * r, cy + 1.4 * r)], fill=(245, 196, 60))
    elif who == "ref":
        d.ellipse((cx - 0.2 * r, cy + 1.2 * r, cx + 0.3 * r, cy + 1.7 * r), fill=(220, 220, 225))


def trophy(d, cx, cy, s, talk, sweat) -> None:
    r = 100 * s
    d.arc((cx - 1.5 * r, cy - 1.0 * r, cx - 0.5 * r, cy + 0.2 * r), 90, 270, fill=GOLD_CUP, width=int(16 * s))
    d.arc((cx + 0.5 * r, cy - 1.0 * r, cx + 1.5 * r, cy + 0.2 * r), 270, 90, fill=GOLD_CUP, width=int(16 * s))
    d.rounded_rectangle((cx - r, cy - 1.2 * r, cx + r, cy + 0.9 * r), int(50 * s), fill=GOLD_CUP)
    d.rectangle((cx - 0.25 * r, cy + 0.9 * r, cx + 0.25 * r, cy + 1.6 * r), fill=GOLD_CUP)
    d.rounded_rectangle((cx - 0.8 * r, cy + 1.5 * r, cx + 0.8 * r, cy + 1.95 * r), int(14 * s), fill=(120, 84, 30))
    _face(d, cx, cy - 0.3 * r, r * 0.8, talk, sweat)


def rumour(d, cx, cy, s, talk, sweat) -> None:
    r = 130 * s
    for i in range(8):
        a = i * math.tau / 8
        d.ellipse((cx + 0.7 * r * math.cos(a) - 0.5 * r, cy + 0.7 * r * math.sin(a) - 0.5 * r,
                   cx + 0.7 * r * math.cos(a) + 0.5 * r, cy + 0.7 * r * math.sin(a) + 0.5 * r), fill=(150, 90, 210))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(170, 110, 230))
    _face(d, cx, cy, r * 0.8, talk, sweat)


def draw_cast(img: Image.Image, cast: list[dict], t: float, speaker: str | None, speaking: bool) -> None:
    d = ImageDraw.Draw(img, "RGBA")
    for i, m in enumerate(cast):
        who = m["who"]
        talk = abs(math.sin(t * 11)) if speaking and who == speaker else 0.0
        bob = math.sin(t * 3 + i) * 7
        k = M.ease_out(M.progress(t, 0.05 * i, 0.3))
        cx = M.lerp(m["x"] + (m.get("from", 0)), m["x"], k)
        s = m.get("s", 1.0)
        fn = {"trophy": trophy, "rumour": rumour}.get(who)
        if fn:
            fn(d, cx, m.get("y", 980) + bob, s, talk, m.get("sweat", False))
        else:
            person(d, who, cx, m.get("y", 900) + bob, s, talk, m.get("sweat", False))


def bubble(img: Image.Image, text: str, cx: float, cy: float, k: float) -> None:
    if k <= 0:
        return
    size = M.fit_size(text, "black", 62, 760)
    w = M.font("black", size).getlength(text) + 80
    d = ImageDraw.Draw(img, "RGBA")
    box = (cx - w / 2, cy - 70, cx + w / 2, cy + 70)
    d.rounded_rectangle(box, 34, fill=(250, 250, 244), outline=(8, 10, 12), width=6)
    d.polygon([(cx - 30, cy + 66), (cx + 30, cy + 66), (cx, cy + 120)], fill=(250, 250, 244))
    M.put_text(img, text, cx, cy, size=size, fill=(14, 16, 20), scale=max(k, 0.01), stroke=0)


SETS = {"vault": (24, 26, 44), "press": (30, 30, 38), "wild": (38, 92, 60), "studio": (44, 30, 60)}


def stage(img: Image.Image, name: str) -> None:
    base = SETS[name]
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, M.W, M.H), fill=base)
    if name == "vault":
        d.ellipse((240, 330, 840, 930), outline=(90, 96, 130), width=28)
        d.line((540, 340, 540, 920), fill=(70, 76, 110), width=10)
        d.line((250, 630, 830, 630), fill=(70, 76, 110), width=10)
    elif name == "press":
        d.rectangle((0, 1180, M.W, 1340), fill=(70, 46, 34))
        for i in range(5):
            d.rectangle((120 + i * 180, 300, 260 + i * 180, 440), fill=(52, 54, 66))
    elif name == "wild":
        d.ellipse((-200, 980, 700, 1700), fill=(30, 74, 48))
        d.ellipse((400, 1020, 1300, 1760), fill=(26, 66, 44))
    else:
        d.rectangle((140, 300, 940, 760), fill=(66, 46, 96))


def scene_toon(img, t, dur, spec, c):
    stage(img, spec.get("set", "studio"))
    say = spec.get("say") or {}
    start = c.at(say.get("at"), 0.25) if say else 0.0
    speaking = bool(say) and t >= start
    draw_cast(img, spec.get("cast", []), t, say.get("who"), speaking)
    if say:
        bubble(img, say["text"], M.CENTER_X, spec.get("bubble_y", 460), M.back_out(M.progress(t, start, 0.22)))
    if spec.get("title"):
        size = M.fit_size(spec["title"], "black", 130, 860)
        M.put_text(img, spec["title"], M.CENTER_X, 400, size=size, fill=M.GOLD, scale=0.8 + 0.2 * M.back_out(M.progress(t, 0, 0.3)))


def scene_split(img, t, dur, spec, c):
    """Two universes, stacked: the real one on top, the invented one below."""
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, M.W, 790), fill=SETS["studio"])
    d.rectangle((0, 790, M.W, M.H), fill=SETS["wild"])
    for panel, y, label in ((spec["ours"], 300, "OUR UNIVERSE"), (spec["other"], 860, "THE OTHER ONE")):
        M.put_text(img, label, M.CENTER_X, y, size=48, fill=M.GOLD)
        M.put_text(img, panel["text"], M.CENTER_X, y + 74, size=M.fit_size(panel["text"], "black", 70, 860))
        draw_cast(img, [{**m, "y": y + 240, "s": 0.8} for m in panel["cast"]], t, None, False)


def scene_fact(img, t, dur, spec, c):
    stage(img, "studio")
    M.put_text(img, "BACK IN REALITY", M.CENTER_X, 480, size=62, fill=M.GOLD, alpha=M.clamp(t * 5))
    k = M.back_out(M.progress(t, c.at(spec.get("at"), 0.3), 0.3))
    if k > 0:
        M.put_text(img, spec["value"], M.CENTER_X, 720, size=M.fit_size(spec["value"], "black", 280, 860), scale=max(k, 0.01))
    M.put_text(img, spec["label"], M.CENTER_X, 930, size=M.fit_size(spec["label"], "black", 66, 860), alpha=M.clamp(t * 3))


def badge(img: Image.Image, text: str) -> None:
    """The label on every frame of a parody or what-if, inside the mobile safe zone."""
    w = M.font("black", 44).getlength(text) + 56
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle((M.CENTER_X - w / 2, 170, M.CENTER_X + w / 2, 244), 20, fill=(214, 40, 56, 235))
    M.put_text(img, text, M.CENTER_X, 207, size=44)
