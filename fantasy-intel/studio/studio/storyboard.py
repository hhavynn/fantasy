"""Cheap previews: a story's script and storyboard as a given show, checked before anything is rendered.

    uv run python -m studio.storyboard <episode_dir> <show> <out_dir>

Copies the episode's snapshot and story into <out_dir>/<id>-show-<show>/, rebuilds facts and the script there
(deterministic, no audio, no language model), runs the factual gates (claims, narration) and the creative gate
against the channel history, and draws one keyframe per beat in the premium look (timings estimated from word
counts, so motion is approximate). Writes script.md, storyboard.png and gates.json. Nothing in the source
episode changes; a full render happens only after these gates pass.
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from PIL import Image, ImageDraw

from .core import Config, Episode, read_json, write_json
from .stages import facts as FA
from .stages import qc
from .stages import story as ST
from .stages.render_motion import Ctx, align

WPS = 2.9                              # Kokoro at this speed, words per second (spoken form)


def preview(src: Path, show: str | None, out: Path) -> Path:
    cfg = Config.load()
    base_id = src.name
    slug = base_id.split("-short-", 1)[1] + (f"-show-{show}" if show else "-preview")
    season, week = int(base_id[:4]), int(base_id.split("-w")[1][:2])
    cfg.raw["episodes_dir"] = str(out)
    ep = Episode(season, week, "short", cfg, slug=slug, log=lambda *_: None)
    if ep.dir.exists():
        shutil.rmtree(ep.dir)
    ep.dir.mkdir(parents=True)
    shutil.copyfile(src / "snapshot.json", ep.path("snapshot.json"))
    story = read_json(src / "story.json")
    write_json(ep.path("story.json"), {**story, "show": show, "variant_of": base_id} if show else story)
    FA.run(ep)
    ST.run(ep)
    script, facts = read_json(ep.path("script.json")), read_json(ep.path("facts.json"))
    snap = read_json(ep.path("snapshot.json"))
    hist_path = Path(__file__).resolve().parents[1] / "episodes" / "history.jsonl"
    history = [json.loads(l) for l in hist_path.read_text().splitlines() if l.strip()] if hist_path.exists() else []
    history = [h for h in history if h["format"] == "short" and h["episode_id"] != base_id]
    gates = [qc.gate_claims(snap, facts, script), qc.gate_narration(script), qc.gate_creative(ep, script, history)]
    write_json(ep.path("gates.json"), [asdict(g) for g in gates])
    _script_md(ep, script, gates)
    _storyboard(ep, script, facts)
    return ep.dir


def _script_md(ep: Episode, script: dict, gates) -> None:
    st = script["structure"]
    lines = [f"# {script['title_candidates'][0]}", "",
             f"Show **{st['family']}** · opening {st['hook_mechanism']} · ending {st['ending_mechanism']} · "
             f"visual {st['visual_device']}", f"Question: {st['question']}", ""]
    for seg, shot in zip(script["narration_segments"], script["shots"]):
        lines.append(f"- **{seg['section']}** ({shot['type']}{'/' + shot['stage'] if shot.get('stage') else ''}"
                     f", {shot.get('transition') or 'push'}): {seg['text_display']}")
    words = sum(len(s["text_spoken"].split()) for s in script["narration_segments"])
    lines += ["", f"{words} words, about {words / WPS:.0f} s.", "",
              "Gates: " + ", ".join(f"{g.name} {'pass' if g.passed else 'FAIL'}" for g in gates)]
    for g in gates:
        if not g.passed:
            lines += [f"  - {g.name}: {f}" for f in g.failures]
    ep.path("script.md").write_text("\n".join(lines) + "\n")


def _storyboard(ep: Episode, script: dict, facts: dict) -> None:
    from .graphics import vector
    claims = {c["id"]: c for c in facts["claims"]}
    c = Ctx(facts, claims, (255, 92, 92), script["structure"]["angle"])
    from . import headshots
    c.headshot = headshots.for_player(ep.cfg, facts["players"][0])
    c.headshots_on = headshots.enabled(ep.cfg)
    frames = []
    for seg, shot in zip(script["narration_segments"], script["shots"]):
        words = [{"text": w, "start": i / WPS, "end": (i + 1) / WPS} for i, w in enumerate(seg["text_spoken"].split())]
        dur = max(len(words) / WPS, 1.2)
        c.tokens = align(seg, words, claims)
        F = vector.draw("premium", {**shot, "_start": 0.0}, dur * 0.92, dur, c)
        vector.caption("premium", F.img, c.tokens[-3:], 2, 1.0, c)
        im = F.img.resize((360, 640), Image.LANCZOS)
        ImageDraw.Draw(im).text((10, 8), f"{seg['section']} · {shot['type']}", fill=(255, 255, 255))
        frames.append(im)
    sheet = Image.new("RGB", (360 * len(frames), 640), (0, 0, 0))
    for i, im in enumerate(frames):
        sheet.paste(im, (i * 360, 0))
    sheet.save(ep.path("storyboard.png"))


if __name__ == "__main__":
    src, show, out = Path(sys.argv[1]), (sys.argv[2] if sys.argv[2] != "none" else None), Path(sys.argv[3])
    print(preview(src, show, out))
