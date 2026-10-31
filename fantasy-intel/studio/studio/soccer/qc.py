"""Soccer QC: the shared technical gates, plus the account's content rules and claim sources."""

from __future__ import annotations

import json
from dataclasses import asdict

from ..core import Episode, read_json, sha256_path, utcnow, write_json
from ..stages import qc as Q
from .explainers import EXPLAINERS
from .scenes import SCENES

STAGES = ("script", "tts", "render", "thumbnail", "metadata")
ATTRIBUTION = "IFAB Laws of the Game"
BANNED_SCENE_KEYS = {"photo", "image", "headshot", "footage", "crest", "logo", "video"}


def gate_content(script: dict) -> Q.Gate:
    """Scenes are the code-drawn types only, so no photo, crest or footage can reach a frame."""
    failures = []
    for i, shot in enumerate(script["shots"]):
        if shot["type"] not in SCENES:
            failures.append(f"shot {i}: scene type {shot['type']!r} is not allowed")
        failures += [f"shot {i}: {k!r} is not allowed" for k in sorted(BANNED_SCENE_KEYS & set(shot))]
    if len(script["shots"]) != len(script["narration_segments"]):
        failures.append("every narration line needs exactly one scene")
    if not script["sources"]:
        failures.append("no source listed")
    return Q._gate("content_rules", failures, scenes=len(script["shots"]))


def gate_sources(script: dict) -> Q.Gate:
    """Every claim is the library's own, with a stated source: nothing can be altered on the way through."""
    defined = {c["id"]: c for c in EXPLAINERS[script["explainer"]]["claims"]}
    failures = [f"{c['id']}: not the claim the library defines" for c in script["claims"] if defined.get(c["id"]) != c]
    failures += [f"{c['id']}: no source" for c in script["claims"] if not c.get("source")]
    return Q._gate("claim_sources", failures, claims=len(script["claims"]))


def run(ep: Episode) -> bool:
    script = read_json(ep.path("script.json"))
    video = ep.path("final.mp4")
    dur = ep.cfg["duration"]
    history_path = ep.cfg.episodes_dir / "history.jsonl"
    history = [json.loads(line) for line in history_path.read_text().splitlines() if line.strip()] \
        if history_path.exists() else []
    narration = [s["text_display"] for s in script["narration_segments"] if s["section"] != "cta"]
    gates = [
        gate_sources(script), gate_content(script), Q.gate_narration(script),
        Q.gate_duration(video, dur["short_min"], dur["short_max"]),
        Q.gate_audio(video, sorted(ep.path("audio").glob("*.wav"))), Q.gate_video(video), Q.gate_av_sync(video),
        Q.gate_subtitles(read_json(ep.path("captions.json"))),
        Q.gate_originality(ep.id, narration, [script["explainer"]], history),
        Q.gate_licenses(ep.cfg, Q.installed_distributions()),
        Q.gate_metadata(read_json(ep.path("metadata.json")), ep.path("thumbnail.png"), ep.fmt, ATTRIBUTION),
        Q.gate_integrity(ep, STAGES),
    ]
    passed = all(g.passed for g in gates)
    checked = ["final.mp4", "thumbnail.png", "metadata.json", "captions.srt", "script.json"]
    write_json(ep.path("qc.json"), {"episode_id": ep.id, "passed": passed, "checked_at": utcnow(),
                                    "files": {f: sha256_path(ep.path(f)) for f in checked},
                                    "gates": [asdict(g) for g in gates]})
    for g in gates:
        ep.log(f"  qc {'PASS' if g.passed else 'FAIL'} {g.name}" + ("" if g.passed else f": {g.failures[:3]}"))
    ep.log(f"  qc: {'PASSED' if passed else 'FAILED, upload blocked'}")
    return passed


def record_history(ep: Episode) -> None:
    """Only a Short that passed QC counts against the next one's originality gate."""
    script = read_json(ep.path("script.json"))
    path = ep.cfg.episodes_dir / "history.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    rows = [r for r in rows if r["episode_id"] != ep.id]
    rows.append({"episode_id": ep.id, "season": ep.season, "week": ep.week, "format": ep.fmt, "order": 0,
                 "players": [script["explainer"]], "structure": script["structure"],
                 "narration": [s["text_display"] for s in script["narration_segments"]]})
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
