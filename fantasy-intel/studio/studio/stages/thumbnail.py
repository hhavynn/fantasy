"""Stage 7: thumbnail.png (1280x720): the hero's big stat and name as text. No photos, no logos."""

from __future__ import annotations

from PIL import Image

from .. import visual as V
from ..core import Episode, read_json, run_stage

VERSION = 1
SIZE = (1280, 720)


def _run(ep: Episode) -> None:
    script = read_json(ep.path("script.json"))
    facts = read_json(ep.path("facts.json"))
    claims = {c["id"]: c for c in facts["claims"]}
    hero_shot = next((s for s in script["shots"] if s["type"] == "hero_stat"), {"metric": 0})
    p = next((p for p in facts["players"] if p["gsis_id"] == hero_shot.get("player")), facts["players"][0])
    m = p["metrics"][hero_shot.get("metric", 0)]
    up = p["role"] == "riser"
    color = V.POSITIVE if up else V.NEGATIVE

    c = V.Canvas(SIZE)
    c.rect((0, 0, 18, SIZE[1]), fill=color, radius=0)
    week = claims[facts["episode"]["week"]]["display"]
    c.text((64, 70), f"{week.upper()} USAGE {'RISER' if up else 'FALLER'}", "black", 40, color)
    stat = f"{claims[m['baseline']]['display']} → {claims[m['recent']]['display']}"
    c.text((64, 300), stat, "black", c.fit_text(stat, "black", 210, SIZE[0] - 128), V.INK, "lm")
    c.text((64, 450), m["label"].upper(), "black", 64, color, "lm")
    name = p["name"].upper()
    c.text((64, 600), name, "black", c.fit_text(name, "black", 80, SIZE[0] - 128), V.INK, "lm")
    img = c.img.resize(SIZE, Image.LANCZOS)
    img.save(ep.path("thumbnail.png"), optimize=True)
    ep.log("  thumbnail: thumbnail.png")


def run(ep: Episode) -> None:
    run_stage(ep, "thumbnail", VERSION, {}, ["script.json", "facts.json"], ["thumbnail.png"], lambda: _run(ep))
