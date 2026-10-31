"""Build one soccer Short's script.json in the shape the shared TTS stage and narration gates read."""

from __future__ import annotations

from typing import Any

from ..core import Episode, read_json, run_stage, write_json
from ..stages.script import fill
from .explainers import CTA, EXPLAINERS

VERSION = 1


def build(explainer: str, package: dict[str, Any] | None = None) -> dict[str, Any]:
    e = EXPLAINERS[explainer]
    claims = {c["id"]: c for c in e["claims"]}
    beats = list(e["beats"])
    subject = None
    if package:
        subject = package["cluster"]["label"].split(":")[0] if package.get("cluster") else None
    if subject:                                 # a trend picks the moment; the rule stays the same
        text, scene, emph = beats[0]
        beats[0] = (f"Everyone's arguing about {subject}. {text}", scene, emph)
    beats.append((CTA, {"type": "cta", "text": "FOLLOW", "sub": "EVERY RULE, EXPLAINED"}, []))
    segments, shots = [], []
    for i, (template, scene, emphasis) in enumerate(beats):
        section = "cta" if i == len(beats) - 1 else "beat"
        segments.append({"id": f"s{i:02d}", "section": section, "text_template": template, "emphasis": emphasis,
                         "text_display": fill(template, claims, "display"),
                         "text_spoken": fill(template, claims, "spoken")})
        shots.append(scene)
    return {
        "format": "explainer", "explainer": explainer, "claims": e["claims"], "narration_segments": segments,
        "shots": shots, "title_templates": [e["title"]], "title_candidates": [e["title"]], "sources": e["sources"],
        "structure": {"hook": e["hook"], "ending": e["ending"], "angle": "explainer", "format": "explainer",
                      "scenes": [s["type"] for s in shots]},
        "subject": subject, "made_for_kids": False, "synthetic_voice": True,
    }


def run(ep: Episode, explainer: str, package: dict[str, Any] | None) -> None:
    run_stage(ep, "script", VERSION, {"explainer": explainer, "package": package, "library": EXPLAINERS[explainer]},
              [], ["script.json"], lambda: write_json(ep.path("script.json"), build(explainer, package)))


def load(ep: Episode) -> dict[str, Any]:
    return read_json(ep.path("script.json"))
