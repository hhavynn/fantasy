"""Stage 7: thumbnail.png (1280x720): the hero's big stat and name, plus his headshot when [shorts]
headshots is on (see studio/headshots.py). No team logos of our own."""

from __future__ import annotations

from PIL import Image

from .. import headshots
from .. import motion as M
from .. import visual as V
from ..core import Episode, read_json, run_stage

VERSION = 4
SIZE = (1280, 720)


def _run(ep: Episode) -> None:
    script = read_json(ep.path("script.json"))
    facts = read_json(ep.path("facts.json"))
    claims = {c["id"]: c for c in facts["claims"]}
    hero_shot = next((s for s in script["shots"] if s["type"] == "hero_stat"), {"metric": 0})
    p = next((p for p in facts["players"] if p["gsis_id"] == hero_shot.get("player")), facts["players"][0])
    m = p["metrics"][hero_shot.get("metric", 0)] if "metrics" in p else None
    up = p["role"] in ("riser", "praise")
    color = V.POSITIVE if up else V.NEGATIVE
    fmt = script.get("structure", {}).get("format")
    d = lambda cid: claims[cid]["display"]
    hook_shot = script["shots"][0]
    if fmt == "mystery":                  # the contradiction itself: same snaps, very different targets
        a, b = [g for g in p["details"]["games"] if g["week"] in hook_shot["games"]]
        kicker, stat, label = "SAME SNAPS. WHAT HAPPENED?", f"{d(a['targets'])} → {d(b['targets'])}", "TARGETS"
        color = V.NEGATIVE
    elif fmt == "opportunity":            # the workload, as counts that match the share
        car = p["details"]["carries"]
        kicker, stat, label = f"WITH {p['teammate']['name'].split()[-1].upper()} OUT", f"{d(car['own_recent'])} OF {d(car['team_recent'])}", "RB CARRIES"
        color = V.POSITIVE
    elif fmt in ("flame", "praise"):      # the stat line itself, and where it ranks
        from . import flame
        kicker, stat, label = flame.thumbnail_text(p, claims, script.get("structure"))
        color = V.NEGATIVE if fmt == "flame" else V.POSITIVE
    else:
        kicker = stat = label = None

    shot = headshots.for_player(ep.cfg, p, script.get("structure")) if ep.fmt == "short" else None
    text_w = (800 if shot else SIZE[0]) - 128
    c = V.Canvas(SIZE)
    c.rect((0, 0, 18, SIZE[1]), fill=color, radius=0)
    week = claims[facts["episode"]["week"]]["display"]
    c.text((64, 70), kicker or f"{week.upper()} USAGE {'RISER' if up else 'FALLER'}", "black", 40, color)
    stat = stat or f"{claims[m['baseline']]['display']} → {claims[m['recent']]['display']}"
    c.text((64, 300), stat, "black", c.fit_text(stat, "black", 210, text_w), V.INK, "lm")
    c.text((64, 450), (label or m["label"]).upper(), "black", 64, color, "lm")
    name = p["name"].upper()
    c.text((64, 600), name, "black", c.fit_text(name, "black", 80, text_w), V.INK, "lm")
    img = c.img.resize(SIZE, Image.LANCZOS)
    if shot:
        art = M.portrait(p["team"], str(p.get("jersey") or ""), str(shot), 470)
        img = img.convert("RGBA")
        img.alpha_composite(art, (SIZE[0] - art.width - 30, (SIZE[1] - art.height) // 2))
        img = img.convert("RGB")
    img.save(ep.path("thumbnail.png"), optimize=True)
    ep.log("  thumbnail: thumbnail.png")


def run(ep: Episode) -> None:
    run_stage(ep, "thumbnail", VERSION, {"headshots": headshots.enabled(ep.cfg)}, ["script.json", "facts.json"], ["thumbnail.png"], lambda: _run(ep))
