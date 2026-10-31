"""Build one soccer Short's script.json in the shape the shared TTS stage and narration gates read."""

from __future__ import annotations

from typing import Any

from ..core import Episode, read_json, run_stage, write_json
from ..stages.script import fill
from . import parody
from .explainers import CTA, EXPLAINERS

VERSION = 2


def _script(beats: list, claim_list: list[dict], title: str, sources: list[str], extra: dict) -> dict[str, Any]:
    claims = {c["id"]: c for c in claim_list}
    segments, shots = [], []
    for i, (template, scene, emphasis) in enumerate(beats):
        section = "cta" if i == len(beats) - 1 else "beat"
        segments.append({"id": f"s{i:02d}", "section": section, "text_template": template, "emphasis": emphasis,
                         "text_display": fill(template, claims, "display"),
                         "text_spoken": fill(template, claims, "spoken")})
        shots.append(scene)
    return {"claims": claim_list, "narration_segments": segments, "shots": shots, "title_templates": [title],
            "title_candidates": [title], "sources": sources, "made_for_kids": False, "synthetic_voice": True, **extra}


def build(explainer: str, package: dict[str, Any] | None = None) -> dict[str, Any]:
    e = EXPLAINERS[explainer]
    beats = list(e["beats"])
    subject = None
    if package:
        subject = parody.subject_of(package) if package.get("cluster") else None
    if subject:                                 # a trend picks the moment; the rule stays the same
        text, scene, emph = beats[0]
        beats[0] = (f"Everyone's arguing about {subject}. {text}", scene, emph)
    beats.append((CTA, {"type": "cta", "text": "FOLLOW", "sub": "EVERY RULE, EXPLAINED"}, []))
    return _script(beats, e["claims"], e["title"], e["sources"], {
        "format": "explainer", "explainer": explainer, "subject": subject,
        "structure": {"hook": e["hook"], "ending": e["ending"], "angle": "explainer", "format": "explainer",
                      "scenes": [b[1]["type"] for b in beats]}})


def build_parody(fmt: str, package: dict[str, Any], custom: list | None = None) -> dict[str, Any]:
    beats, claims, title = parody.beats_for(fmt, package, custom)
    sources = sorted({c["source"] for c in claims}) + package["idea"]["source_links"][:2]
    return _script(beats, claims, title, sources or ["Original fiction"], {
        "format": fmt, "explainer": None, "subject": parody.subject_of(package), "real_names": parody.real_names(package),
        "topics": package["cluster"].get("topics") or [],
        "structure": {"hook": fmt, "ending": "follow", "angle": fmt, "format": fmt,
                      "scenes": [b[1]["type"] for b in beats]}})


def run(ep: Episode, spec: dict[str, Any], package: dict[str, Any] | None) -> None:
    """``spec`` is {"explainer": name} or {"format": fmt, "custom": beats or None}."""
    def make() -> dict[str, Any]:
        if spec.get("explainer"):
            return build(spec["explainer"], package)
        return build_parody(spec["format"], package, spec.get("custom"))

    library = EXPLAINERS.get(spec.get("explainer")) or parody.TEMPLATES
    run_stage(ep, "script", VERSION, {"spec": spec, "package": package, "library": library},
              [], ["script.json"], lambda: write_json(ep.path("script.json"), make()))


def load(ep: Episode) -> dict[str, Any]:
    return read_json(ep.path("script.json"))
