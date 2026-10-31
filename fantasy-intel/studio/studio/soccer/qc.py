"""Soccer QC: the shared technical gates, plus the account's content rules and claim sources."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict

from ..core import Episode, read_json, sha256_path, utcnow, write_json
from ..stages import qc as Q
from .explainers import EXPLAINERS
from .parody import LABELS, real_stat, stat_claim
from .render import FICTION_NOTICE
from .scenes import SCENES
from .toons import CAST

STAGES = ("script", "tts", "render", "thumbnail", "metadata")
ATTRIBUTION = "Source:"
NAME_PARTICLES = {"de", "del", "der", "di", "da", "dos", "das", "do", "van", "von", "la", "le", "el", "al", "bin", "ibn",
                  "jr", "st"}
BLOCKED_TOPICS = {"injury", "serious"}     # an injury or a real harm is never the joke
SPEECH = r"(?:says?|said|told|tells|admits?|admitted|claims?|claimed|insists?|insisted)"
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


def gate_sources(script: dict, package: dict | None = None) -> Q.Gate:
    """Every claim is the library's own or the package's sourced stat: nothing can be altered on the way through."""
    if script["format"] == "explainer":
        defined = {c["id"]: c for c in EXPLAINERS[script["explainer"]]["claims"]}
    else:
        stat = real_stat(package) if package else None
        defined = {"c001": stat_claim(stat)} if stat else {}
    failures = [f"{c['id']}: not the claim the source defines" for c in script["claims"] if defined.get(c["id"]) != c]
    failures += [f"{c['id']}: no source" for c in script["claims"] if not c.get("source")]
    return Q._gate("claim_sources", failures, claims=len(script["claims"]))


def fold(text: str) -> str:
    """Lower-case without accents, so "MBAPPE" and "Mbappé" are the same name to the gate."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def _names(script: dict) -> list[str]:
    return sorted({w for name in script["real_names"] for w in re.split(r"[\s-]+", fold(name))
                   if w and w not in NAME_PARTICLES})


def gate_parody(script: dict, meta: dict) -> Q.Gate:
    """The parody rules: the closed cast, labelled on every scene and aloud, no invented line from a real person."""
    failures = []
    label = LABELS[script["format"]]
    names = _names(script)
    blocked = sorted(BLOCKED_TOPICS & set(script["topics"][:2]))     # most common first: what the story is about
    if blocked:
        failures.append(f"no fiction format for this story: {blocked}")
    first = script["narration_segments"][0]["text_spoken"].lower()
    if "none of it happened" not in first or not ("parody" in first or "made up" in first):
        failures.append("the opening line must say out loud that this is invented")
    for i, shot in enumerate(script["shots"]):
        if shot.get("badge") != label:
            failures.append(f"shot {i}: missing the {label!r} label")
        members = list(shot.get("cast", []))
        for side in ("ours", "other"):
            members += (shot.get(side) or {}).get("cast", [])
        failures += [f"shot {i}: {m['who']!r} is not in the cast" for m in members if m["who"] not in CAST]
        said = fold((shot.get("say") or {}).get("text", ""))
        failures += [f"shot {i}: a real name in invented dialogue: {n}" for n in names
                     if re.search(rf"\b{re.escape(n)}\b", said)]
    for seg in script["narration_segments"]:
        for n in names:
            if re.search(rf"\b{re.escape(n)}\b(?:\W+\w+){{0,4}}\W+{SPEECH}\b|\b{SPEECH}\b(?:\W+\w+){{0,2}}\W+{re.escape(n)}\b",
                         fold(seg["text_display"])):
                failures.append(f"{seg['id']}: reads as a statement by {n}")
    if FICTION_NOTICE not in meta["description"]:
        failures.append("description lacks the parody notice")
    if not meta.get("altered_content") or meta["contains_synthetic_media"] is not True:
        failures.append("altered-content disclosure not set")
    return Q._gate("parody_rules", failures, label=label)


def run(ep: Episode, package: dict | None = None) -> bool:
    script = read_json(ep.path("script.json"))
    video = ep.path("final.mp4")
    dur = ep.cfg["duration"]
    history_path = ep.cfg.episodes_dir / "history.jsonl"
    history = [json.loads(line) for line in history_path.read_text().splitlines() if line.strip()] \
        if history_path.exists() else []
    narration = [s["text_display"] for s in script["narration_segments"] if s["section"] != "cta"]
    meta = read_json(ep.path("metadata.json"))
    cast_id = script["explainer"] or f"{script['format']}:{script['subject']}"
    gates = [
        gate_sources(script, package), gate_content(script), Q.gate_narration(script),
        *([gate_parody(script, meta)] if script["format"] != "explainer" else []),
        Q.gate_duration(video, dur["short_min"], dur["short_max"]),
        Q.gate_audio(video, sorted(ep.path("audio").glob("*.wav"))), Q.gate_video(video), Q.gate_av_sync(video),
        Q.gate_subtitles(read_json(ep.path("captions.json"))),
        Q.gate_originality(ep.id, narration, [cast_id], history),
        Q.gate_licenses(ep.cfg, Q.installed_distributions()),
        Q.gate_metadata(meta, ep.path("thumbnail.png"), ep.fmt, ATTRIBUTION),
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
                 "players": [script["explainer"] or f"{script['format']}:{script['subject']}"],
                 "structure": script["structure"],
                 "narration": [s["text_display"] for s in script["narration_segments"]]})
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
