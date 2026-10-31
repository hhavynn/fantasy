"""Soccer render, thumbnail and metadata stages. Voice, music, sound effects and captions are the shared ones."""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
from PIL import Image

from .. import audio_design as A
from .. import motion as M
from ..core import Episode, read_json, run_stage, sha256_file, write_json
from ..stages.assemble import srt_time
from ..stages.metadata import AI_DISCLOSURE, AI_DISCLOSURE_CLONE
from ..stages.render_motion import CAPTION, LOUDNESS, TRANSITION, align, phrases
from . import scenes as S

VERSION = 1
HASHTAGS = "#shorts #football #soccer"
FICTION_NOTICE = ("PARODY: cartoon characters and invented events. Nothing shown here happened, and no line is a real "
                  "quote from anyone.")


def plan(ep: Episode) -> dict:
    script = read_json(ep.path("script.json"))
    timings = {s["id"]: s for s in read_json(ep.path("audio/timings.json"))["segments"]}
    claims = {c["id"]: c for c in script["claims"]}
    timeline, cues, voice, offset, events, sr = [], [], [], 0.0, [], None
    for i, (seg, shot) in enumerate(zip(script["narration_segments"], script["shots"])):
        tm = timings[seg["id"]]
        toks = align(seg, tm["words"], claims)
        timeline.append({"spec": shot, "start": offset, "dur": tm["duration"], "tokens": toks})
        for ph in phrases(toks):
            cues.append({"segment": seg["id"], "words": ph, "offset": offset, "start": offset + ph[0]["start"],
                         "text": " ".join(w["text"] for w in ph)})
        if i:
            events.append((offset - 0.12, "whoosh", -6))
        data, sr = sf.read(ep.path(f"audio/{seg['id']}.wav"), dtype="float32")
        voice.append(data)
        offset += tm["duration"]
    for i, cue in enumerate(cues):
        cue["end"] = cues[i + 1]["start"] if i + 1 < len(cues) else offset
    sh = ep.cfg["shorts"]
    mixed = A.mix(A.upsample(np.concatenate(voice), sr), events, sh.get("music", True), sh.get("music_db", -22),
                  sh.get("sfx_db", -12), "drive")
    ep.path("work").mkdir(parents=True, exist_ok=True)
    sf.write(ep.path("work/mix.wav"), mixed, A.SR, subtype="PCM_16")
    return {"timeline": timeline, "cues": cues, "total": offset}


def _run(ep: Episode) -> None:
    pl = plan(ep)
    timeline, cues, total = pl["timeline"], pl["cues"], pl["total"]
    fps = ep.cfg["video"]["fps"]
    frames = int(math.ceil(total * fps))
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{M.W}x{M.H}", "-r", str(fps), "-i", "-",
         "-i", str(ep.path("work/mix.wav")), "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-g", str(fps * 2),
         "-af", LOUDNESS, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
         "-frames:v", str(frames), "-movflags", "+faststart", str(ep.path("final.mp4"))],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    ci = 0
    for n in range(frames):
        t = n / fps
        i = max(j for j, sc in enumerate(timeline) if sc["start"] <= t + 1e-9)
        sc = timeline[i]
        tl = t - sc["start"]
        frame = S.draw(sc["spec"], tl, sc["dur"], S.Ctx(sc["tokens"]))
        if i and tl < TRANSITION:
            prev = timeline[i - 1]
            old = S.draw(prev["spec"], prev["dur"], prev["dur"], S.Ctx(prev["tokens"]))
            frame = Image.blend(old, frame, M.ease_out(tl / TRANSITION))
        while ci + 1 < len(cues) and cues[ci + 1]["start"] <= t:
            ci += 1
        cue = cues[ci]
        if cue["start"] <= t < cue["end"]:
            active = max((j for j, w in enumerate(cue["words"]) if cue["offset"] + w["start"] <= t), default=0)
            layer = M.caption_frame(cue["words"], active, M.GOLD, t - cue["start"])
            frame.paste(layer, (0, M.CAPTION_Y - 165), layer)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait():
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.read().decode()[-1500:]}")
    flat = [{"start": round(c["start"], 3), "end": round(c["end"], 3), "text": c["text"], "segment": c["segment"]}
            for c in cues]
    ep.path("captions.srt").write_text("\n".join(
        f"{i}\n{srt_time(c['start'])} --> {srt_time(c['end'])}\n{c['text']}\n" for i, c in enumerate(flat, 1)))
    write_json(ep.path("captions.json"), {**CAPTION, "cues": flat})
    ep.log(f"  render: final.mp4, {total:.1f}s, {frames} frames, {len(timeline)} scenes, {len(flat)} captions")


def thumbnail(ep: Episode) -> None:
    script = read_json(ep.path("script.json"))
    frame = S.draw(script["shots"][0], 2.0, 3.0, S.Ctx())
    fiction = script["format"] != "explainer"
    top = 80 if fiction else 280               # keeps a parody's label inside the thumbnail
    img = frame.resize((720, 1280)).crop((0, top, 720, top + 405)).resize((1280, 720))
    img.save(ep.path("thumbnail.png"))


def metadata(ep: Episode) -> None:
    script = read_json(ep.path("script.json"))
    clone = ep.cfg["shorts"].get("voice_clone")
    disclosure = AI_DISCLOSURE_CLONE if clone else AI_DISCLOSURE
    lines = [script["title_candidates"][0], "", disclosure, "Source: " + "; ".join(script["sources"]) + ".", "", HASHTAGS]
    fiction = script["format"] != "explainer"
    if fiction:
        lines[1:1] = [FICTION_NOTICE, ""]
    tag = script["explainer"] or script["format"].replace("_", " ")
    write_json(ep.path("metadata.json"), {
        "episode_id": ep.id, "title": script["title_candidates"][0], "title_candidates": script["title_candidates"],
        "description": "\n".join(lines), "tags": ["shorts", "football", "soccer", tag, "football parody" if fiction else "football rules"],
        "category_id": "17", "default_language": "en", "default_audio_language": "en",
        "privacy_status": "private", "self_declared_made_for_kids": script["made_for_kids"],
        "contains_synthetic_media": True if fiction else script["synthetic_voice"], "chapters": [],
        "altered_content": fiction,
        "files": {"video": "final.mp4", "thumbnail": "thumbnail.png", "captions": "captions.srt"},
    })


def _code() -> dict[str, str]:
    return {m.__name__: sha256_file(Path(m.__file__)) for m in (S, M, A)}


def run(ep: Episode) -> None:
    run_stage(ep, "render", VERSION, {"video": ep.cfg["video"], "shorts": ep.cfg["shorts"], "caption": CAPTION,
                                      "code": _code()},
              ["script.json", "audio"], ["final.mp4", "captions.srt", "captions.json"], lambda: _run(ep))
    run_stage(ep, "thumbnail", VERSION, {"code": _code()}, ["script.json"], ["thumbnail.png"], lambda: thumbnail(ep))
    run_stage(ep, "metadata", VERSION, {"voice_clone": ep.cfg["shorts"].get("voice_clone")},
              ["script.json"], ["metadata.json"], lambda: metadata(ep))
