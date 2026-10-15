"""Stage 9: QC gates. Any failure blocks upload; qc.json records every gate with its evidence.

Gates: data claims, script integrity, duration, audio (silence, clipping), video (black, frozen), A/V length,
subtitle overflow, originality vs the episode history, licenses, upload metadata, and pipeline integrity
(every stage built from the current version of its inputs).
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from dataclasses import asdict, dataclass, field
from importlib import metadata as importlib_metadata
from pathlib import Path

import numpy as np
import soundfile as sf
from PIL import Image

from ..core import Episode, read_json, sha256_path, utcnow, write_json
from ..numbers import render
from .assemble import CAPTION_MAX_LINES, CAPTION_MAX_WIDTH, caption_font, caption_lines
from .facts import evaluate_claim
from .script import BANNED, DIGIT, NUMBER_WORDS, PLACEHOLDER, fill

VERSION = 1

# Model weights may only come from these repos, under these licenses (plan: license audit).
ALLOWED_MODELS = {"unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF": "Apache-2.0", "hexgrad/Kokoro-82M": "Apache-2.0"}
# Never in a monetized pipeline: non-commercial weights (F5-TTS, XTTS/Coqui, AudioCraft/MusicGen) or code
# (Wav2Lip). Matched against installed distribution names.
BANNED_PACKAGES = {"f5-tts", "tts", "coqui-tts", "coqui-tts-trainer", "audiocraft", "musicgen", "wav2lip", "xtts"}
BANNED_MODEL_WORDS = ("f5-tts", "f5tts", "xtts", "coqui", "audiocraft", "musicgen", "wav2lip")

SILENCE_DB, SILENCE_MAX_S = -45, 1.5
CLIP_SAMPLE = 0.999                  # narration WAV sample peak at or above this is clipping
FINAL_TRUE_PEAK_MAX_DB = -0.1
BLACK_MIN_S, FREEZE_MIN_S = 0.5, 6.0
AV_TOLERANCE_S = 0.25
SIMILARITY_MAX = 0.25                # 5-gram Jaccard vs any earlier episode
PLAYER_OVERLAP_MAX = 0.6             # share of this episode's players featured in one earlier episode


@dataclass
class Gate:
    name: str
    passed: bool
    details: dict = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)


def _gate(name: str, failures: list[str], **details) -> Gate:
    return Gate(name, not failures, details, failures[:50])


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError(f"{cmd[0]} failed: {proc.stderr[-1500:]}")
    return proc.stdout + proc.stderr


def probe(path: Path) -> dict:
    return json.loads(_run(["ffprobe", "-v", "error", "-show_entries",
                            "stream=codec_type,codec_name,width,height,r_frame_rate,duration:format=duration",
                            "-of", "json", str(path)]))


# --------------------------------------------------------------------------------------------- gates

def gate_claims(snapshot: dict, facts: dict, script: dict) -> Gate:
    """Every claim re-derives from the snapshot to the same value, display and spoken strings, and the
    files chain together (snapshot -> facts -> script)."""
    failures = []
    if facts["snapshot_sha256"] != snapshot["content_sha256"]:
        failures.append("facts.json was built from a different snapshot")
    fact_claims = {c["id"]: c for c in facts["claims"]}
    for c in script["claims"]:
        src = fact_claims.get(c["id"])
        if src is None or src["source"] != c["source"]:
            failures.append(f"{c['id']}: not the claim facts.json defines")
            continue
        try:
            value = evaluate_claim(snapshot, c["source"])
        except Exception as exc:          # a source that no longer resolves is a mismatch
            failures.append(f"{c['id']} ({c['label']}): cannot re-derive: {exc}")
            continue
        same = (math.isclose(value, c["value"], rel_tol=0, abs_tol=1e-9)
                if isinstance(value, float) and isinstance(c["value"], (int, float)) else value == c["value"])
        if not same:
            failures.append(f"{c['id']} ({c['label']}): data says {value!r}, script says {c['value']!r}")
            continue
        display, spoken = render(c["format"], value)
        if (display, spoken) != (c["display"], c["spoken"]):
            failures.append(f"{c['id']} ({c['label']}): reads {c['display']!r}/{c['spoken']!r}, "
                            f"data renders {display!r}/{spoken!r}")
    return _gate("data_claims", failures, claims_checked=len(script["claims"]))


def gate_narration(script: dict) -> Gate:
    """Narration and titles: every number comes from a placeholder, the rendered texts match their
    templates, no banned topic, and every used placeholder is a recorded claim."""
    claims = {c["id"]: c for c in script["claims"]}
    failures = []
    texts = [(s["id"], s["text_template"], s) for s in script["narration_segments"]]
    texts += [(f"title{i + 1}", t, None) for i, t in enumerate(script["title_templates"])]
    for where, template, seg in texts:
        used = set(PLACEHOLDER.findall(template))
        if used - set(claims):
            failures.append(f"{where}: placeholders without a claim: {sorted(used - set(claims))}")
            continue
        bare = PLACEHOLDER.sub(" ", template)
        if DIGIT.search(bare):
            failures.append(f"{where}: a number typed outside any claim: {bare!r}")
        words = sorted({m.group(1).lower() for m in NUMBER_WORDS.finditer(bare)})
        if words:
            failures.append(f"{where}: number words outside any claim: {words}")
        banned = sorted({m.group(0).lower() for m in BANNED.finditer(bare)})
        if banned:
            failures.append(f"{where}: banned phrases {banned}")
        if seg is not None:
            if seg["text_display"] != fill(template, claims, "display"):
                failures.append(f"{where}: text_display does not match its template and claims")
            if seg["text_spoken"] != fill(template, claims, "spoken"):
                failures.append(f"{where}: text_spoken (what TTS read) does not match its template and claims")
            if DIGIT.search(seg["text_spoken"]):
                failures.append(f"{where}: digits in the spoken text")
    for i, (title, template) in enumerate(zip(script["title_candidates"], script["title_templates"])):
        if title != fill(template, claims, "display"):
            failures.append(f"title{i + 1}: does not match its template")
    return _gate("narration_numbers", failures, segments=len(script["narration_segments"]))


def gate_duration(video: Path, lo: float, hi: float) -> Gate:
    d = float(probe(video)["format"]["duration"])
    return _gate("duration", [] if lo <= d <= hi else [f"{d:.1f}s is outside {lo:.0f}-{hi:.0f}s"],
                 seconds=round(d, 2), bounds=[lo, hi])


def gate_audio(video: Path, wavs: list[Path]) -> Gate:
    failures = []
    out = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-vn",
                "-af", f"silencedetect=n={SILENCE_DB}dB:d={SILENCE_MAX_S},ebur128=peak=true", "-f", "null", "-"])
    silences = []
    for m in re.finditer(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", out):
        end, dur = float(m.group(1)), float(m.group(2))
        silences.append({"start": round(end - dur, 2), "seconds": dur})
    # A trailing silence is reported as a start with no end.
    tail = re.findall(r"silence_start: ([\d.]+)", out)
    if len(tail) > len(silences):
        silences.append({"start": float(tail[-1]), "seconds": None})
    failures += [f"silence of {s['seconds'] or 'to the end'}s at {s['start']}s" for s in silences]
    summary = out[out.rfind("Summary:"):] if "Summary:" in out else ""
    loud = re.search(r"I:\s+(-?[\d.]+) LUFS", summary)
    peak = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", summary)
    true_peak = float(peak.group(1)) if peak and peak.group(1) != "-inf" else None
    if true_peak is not None and true_peak >= FINAL_TRUE_PEAK_MAX_DB:
        failures.append(f"final true peak {true_peak} dBFS (clipping)")
    clipped = []
    for wav in wavs:
        data, _ = sf.read(wav, dtype="float32")
        if data.size == 0:
            failures.append(f"{wav.name}: empty audio")
            continue
        peak_sample = float(np.max(np.abs(data)))
        if peak_sample >= CLIP_SAMPLE:
            clipped.append(wav.name)
    failures += [f"{name}: narration clipped" for name in clipped]
    return _gate("audio", failures, integrated_lufs=float(loud.group(1)) if loud else None,
                 true_peak_dbfs=true_peak, silences=silences, wavs_checked=len(wavs))


def gate_video(video: Path) -> Gate:
    out = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-an",
                "-vf", f"blackdetect=d={BLACK_MIN_S}:pix_th=0.10,freezedetect=n=0.001:d={FREEZE_MIN_S}",
                "-f", "null", "-"])
    blacks = [{"start": float(a), "end": float(b)} for a, b in
              re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", out)]
    freezes = [float(x) for x in re.findall(r"lavfi\.freezedetect\.freeze_start: ([\d.]+)", out)]
    failures = [f"black frames {b['start']}-{b['end']}s" for b in blacks]
    failures += [f"frozen frames from {f}s" for f in freezes]
    return _gate("video", failures, black=blacks, frozen_starts=freezes)


def gate_av_sync(video: Path) -> Gate:
    streams = probe(video)["streams"]
    v = [float(s["duration"]) for s in streams if s["codec_type"] == "video"]
    a = [float(s["duration"]) for s in streams if s["codec_type"] == "audio"]
    if not v or not a:
        return _gate("av_length", ["missing a video or audio stream"])
    diff = abs(v[0] - a[0])
    return _gate("av_length", [] if diff <= AV_TOLERANCE_S else [f"video {v[0]:.2f}s vs audio {a[0]:.2f}s"],
                 video_s=v[0], audio_s=a[0], tolerance_s=AV_TOLERANCE_S)


def gate_subtitles(captions: dict) -> Gate:
    failures = []
    font = caption_font()
    for i, cue in enumerate(captions["cues"]):
        lines = caption_lines(cue["text"])
        if len(lines) > CAPTION_MAX_LINES:
            failures.append(f"cue {i + 1}: {len(lines)} lines: {cue['text'][:60]!r}")
        for line in lines:
            if font.getlength(line) > CAPTION_MAX_WIDTH:
                failures.append(f"cue {i + 1}: line wider than the caption area: {line[:60]!r}")
        if cue["end"] <= cue["start"]:
            failures.append(f"cue {i + 1}: zero or negative duration")
    return _gate("subtitles", failures, cues=len(captions["cues"]), max_lines=CAPTION_MAX_LINES)


def _shingles(texts: list[str], n: int = 5) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9%']+", " ".join(texts).lower())
    return {tuple(words[i:i + n]) for i in range(max(len(words) - n + 1, 0))}


def gate_originality(episode_id: str, narration: list[str], players: list[str], history: list[dict]) -> Gate:
    """Narration must not be a near copy of any earlier episode, and must not recycle its cast."""
    mine = _shingles(narration)
    scores, failures = [], []
    for e in history:
        if e["episode_id"] == episode_id:
            continue
        theirs = _shingles(e["narration"][:-1])     # the fixed CTA line is boilerplate
        jac = len(mine & theirs) / len(mine | theirs) if mine | theirs else 0.0
        overlap = len(set(players) & set(e["players"])) / len(players) if players else 0.0
        scores.append({"episode_id": e["episode_id"], "jaccard_5gram": round(jac, 3), "player_overlap": round(overlap, 2)})
        if jac > SIMILARITY_MAX:
            failures.append(f"narration {jac:.0%} similar to {e['episode_id']} (max {SIMILARITY_MAX:.0%})")
        if overlap > PLAYER_OVERLAP_MAX:
            failures.append(f"{overlap:.0%} of players already featured in {e['episode_id']}")
    return _gate("originality", failures, compared=scores)


def installed_distributions() -> set[str]:
    return {(d.metadata["Name"] or "").lower().replace("_", "-") for d in importlib_metadata.distributions()}


def gate_licenses(cfg, installed: set[str]) -> Gate:
    failures = [f"banned package installed: {p}" for p in sorted(installed & BANNED_PACKAGES)]
    for section in ("llm", "tts"):
        repo = cfg[section].get("model_repo") or cfg[section].get("repo")
        lic = cfg[section].get("model_license") or cfg[section].get("license")
        if ALLOWED_MODELS.get(repo) != lic:
            failures.append(f"{section}: model {repo!r} ({lic}) is not on the commercial allowlist")
        blob = json.dumps(cfg[section]).lower()
        failures += [f"{section}: banned model reference {w!r}" for w in BANNED_MODEL_WORDS if w in blob]
    if cfg["tts"].get("engine") != "kokoro":
        failures.append("tts engine is not Kokoro; OpenVoice needs a recorded consent check first")
    return _gate("licenses", failures, allowed_models=ALLOWED_MODELS)


def gate_metadata(meta: dict, thumbnail: Path) -> Gate:
    failures = []
    if meta["privacy_status"] != "private":
        failures.append(f"privacy_status is {meta['privacy_status']!r}, must be 'private'")
    if meta["self_declared_made_for_kids"] is not False:
        failures.append("made_for_kids must be false")
    if meta["contains_synthetic_media"] is not True:
        failures.append("contains_synthetic_media must be true (AI voice)")
    if "Narrated with an AI voice." not in meta["description"]:
        failures.append("description lacks the AI-voice disclosure")
    if "nflverse" not in meta["description"]:
        failures.append("description lacks the data attribution")
    if not 0 < len(meta["title"]) <= 100 or "<" in meta["title"] or ">" in meta["title"]:
        failures.append(f"title invalid for YouTube: {meta['title']!r}")
    if len(meta["description"].encode()) > 5000:
        failures.append("description over 5000 bytes")
    if sum(len(t) for t in meta["tags"]) > 450:
        failures.append("tags over YouTube's 500-character limit")
    with Image.open(thumbnail) as im:
        if im.size != (1280, 720):
            failures.append(f"thumbnail is {im.size}, expected 1280x720")
    if thumbnail.stat().st_size > 2 * 1024 * 1024:
        failures.append("thumbnail over 2 MB")
    chapters = meta.get("chapters", [])
    if len(chapters) < 3 or chapters[0]["start"] != 0:
        failures.append("chapters must start at 0:00 and number at least 3")
    return _gate("metadata", failures, title=meta["title"])


def gate_integrity(ep: Episode) -> Gate:
    """Each stage's recorded inputs equal the files on disk now: no stale audio, stills or video."""
    failures = []
    for name in ("facts", "script", "tts", "charts", "assemble", "thumbnail", "metadata"):
        path = ep.path(f".stages/{name}.json")
        if not path.exists():
            failures.append(f"{name}: no stage record")
            continue
        rec = read_json(path)
        for rel, digest in {**rec["inputs"], **rec["outputs"]}.items():
            if not ep.path(rel).exists() or sha256_path(ep.path(rel)) != digest:
                failures.append(f"{name}: {rel} changed after the stage ran; rerun make-episode")
    return _gate("pipeline_integrity", failures)


# ---------------------------------------------------------------------------------------------- run

def run_gates(ep: Episode) -> list[Gate]:
    snapshot, facts, script = (read_json(ep.path(f)) for f in ("snapshot.json", "facts.json", "script.json"))
    video = ep.path("final.mp4")
    dur = ep.cfg["duration"]
    lo, hi = (dur["long_min"], dur["long_max"]) if ep.fmt == "long" else (dur["short_min"], dur["short_max"])
    history_path = ep.cfg.episodes_dir / "history.jsonl"
    history = [json.loads(l) for l in history_path.read_text().splitlines() if l.strip()] \
        if history_path.exists() else []
    narration = [s["text_display"] for s in script["narration_segments"] if s["section"] != "cta"]
    return [
        gate_claims(snapshot, facts, script),
        gate_narration(script),
        gate_duration(video, lo, hi),
        gate_audio(video, sorted(ep.path("audio").glob("*.wav"))),
        gate_video(video),
        gate_av_sync(video),
        gate_subtitles(read_json(ep.path("captions.json"))),
        gate_originality(ep.id, narration, [p["gsis_id"] for p in facts["players"]], history),
        gate_licenses(ep.cfg, installed_distributions()),
        gate_metadata(read_json(ep.path("metadata.json")), ep.path("thumbnail.png")),
        gate_integrity(ep),
    ]


def run(ep: Episode) -> bool:
    gates = run_gates(ep)
    passed = all(g.passed for g in gates)
    checked = ["final.mp4", "thumbnail.png", "metadata.json", "captions.srt", "script.json"]
    write_json(ep.path("qc.json"), {
        "episode_id": ep.id, "passed": passed, "checked_at": utcnow(), "qc_version": VERSION,
        "files": {f: sha256_path(ep.path(f)) for f in checked},
        "gates": [asdict(g) for g in gates],
    })
    for g in gates:
        ep.log(f"  qc {'PASS' if g.passed else 'FAIL'} {g.name}" + ("" if g.passed else f": {g.failures[:3]}"))
    ep.log(f"  qc: {'PASSED' if passed else 'FAILED, upload blocked'}")
    return passed
