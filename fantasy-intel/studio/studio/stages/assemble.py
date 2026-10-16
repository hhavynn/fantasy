"""Stage 5b-6: per-segment clips (slow zoom on the still + burned-in captions + narration), then final.mp4.

Captions are rendered by Pillow (this Homebrew FFmpeg has no libass/freetype) as transparent PNGs and
overlaid by FFmpeg, so they are burned in. Caption text is the display form (88%), timed against Kokoro's
spoken-word timestamps. captions.srt is written alongside for upload as a separate caption track.
"""

from __future__ import annotations

import re
import subprocess

from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .. import visual as V
from ..core import FONTS, Episode, read_json, run_stage, write_json
from .script import PLACEHOLDER, fill

VERSION = 2
CAPTION_SIZE = 46
CAPTION_MAX_WIDTH = 1500
CAPTION_MAX_LINES = 2
LOUDNESS = "loudnorm=I=-14:TP=-1.5:LRA=11"


def ffmpeg(*args: str) -> None:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-2000:]}")


# ------------------------------------------------------------------------------------------ captions

@lru_cache(maxsize=None)
def caption_font() -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / "Inter-SemiBold.ttf"), CAPTION_SIZE)


def caption_lines(text: str) -> list[str]:
    """The lines a caption wraps to at burn-in size (shared with the subtitle-overflow QC gate)."""
    return _wrap_px(text, caption_font(), CAPTION_MAX_WIDTH)


def _fits(text: str) -> bool:
    return len(caption_lines(text)) <= CAPTION_MAX_LINES


def _wrap_px(text: str, font, max_px: float) -> list[str]:
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if cur and draw.textlength(trial, font=font) > max_px:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def caption_chunks(template: str, claims: dict) -> list[dict]:
    """Split a segment into caption cards of at most two lines, preferring sentence ends.
    Placeholders contain no spaces, so splitting on whitespace never breaks one."""
    words, chunks, cur = template.split(), [], []
    for w in words:
        trial = cur + [w]
        if cur and not _fits(fill(" ".join(trial), claims, "display")):
            chunks.append(cur)
            trial = [w]
        cur = trial
        if re.search(r"[.!?]['\"”’]?$", w) and len(fill(" ".join(cur), claims, "display")) >= 24:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    out = []
    for ch in chunks:
        text = " ".join(ch)
        out.append({"display": fill(text, claims, "display"),
                    "spoken_words": len(fill(text, claims, "spoken").split())})
    return out


def time_chunks(chunks: list[dict], words: list[dict], duration: float, tail: float) -> None:
    """Map each chunk onto Kokoro's word timeline by its share of the spoken words."""
    tokens = [w for w in words if re.search(r"[A-Za-z0-9]", w["text"])]
    total = sum(c["spoken_words"] for c in chunks) or 1
    speech_end = max(duration - tail, 0.1)
    k = 0
    for c in chunks:
        if tokens:
            i0 = min(int(round(k / total * len(tokens))), len(tokens) - 1)
            c["start"] = tokens[i0]["start"]
        else:
            c["start"] = speech_end * k / total
        k += c["spoken_words"]
    chunks[0]["start"] = 0.0
    for a, b in zip(chunks, chunks[1:]):
        a["end"] = b["start"]
    chunks[-1]["end"] = speech_end


def render_caption(text: str, size: tuple[int, int], path) -> None:
    w, h = size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = caption_font()
    lines = caption_lines(text)
    line_h = int(CAPTION_SIZE * 1.3)
    block_h = line_h * len(lines)
    bottom = h - (112 if h < w else 420)     # landscape: the band below charts.CONTENT_BOTTOM
    widths = [d.textlength(line, font=font) for line in lines]
    pad = 26
    box = (w / 2 - max(widths) / 2 - pad, bottom - block_h - pad * 0.6, w / 2 + max(widths) / 2 + pad, bottom + pad * 0.6)
    d.rounded_rectangle(box, radius=16, fill=(8, 11, 16, 205))
    for i, line in enumerate(lines):
        d.text((w / 2, bottom - block_h + i * line_h + line_h / 2), line, font=font, fill=V.INK, anchor="mm")
    img.save(path)


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# -------------------------------------------------------------------------------------------- video

def segment_clip(ep: Episode, seg: dict, timing: dict, chunks: list[dict], index: int) -> None:
    w, h = ep.size
    fps = ep.cfg["video"]["fps"]
    work = ep.path(f"work/{seg['id']}")
    work.mkdir(parents=True, exist_ok=True)
    blank = work / "blank.png"
    Image.new("RGBA", (w, h), (0, 0, 0, 0)).save(blank)
    lines = ["ffconcat version 1.0"]
    for i, c in enumerate(chunks):
        png = work / f"cap_{i:03d}.png"
        render_caption(c["display"], (w, h), png)
        lines += [f"file '{png.name}'", f"duration {max(c['end'] - c['start'], 0.04):.3f}"]
    tail = max(timing["duration"] - chunks[-1]["end"], 0.04)
    lines += [f"file '{blank.name}'", f"duration {tail:.3f}", f"file '{blank.name}'"]
    (work / "captions.ffconcat").write_text("\n".join(lines) + "\n")

    duration = timing["duration"]
    frames = max(int(round(duration * fps)), 1)
    zoom_in = index % 2 == 0          # alternate slow zoom-in / zoom-out so consecutive shots differ
    z = f"1+0.05*on/{frames}" if zoom_in else f"1.05-0.05*on/{frames}"
    graph = (f"[0:v]zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={w}x{h}:fps={fps},"
             f"setsar=1,format=yuv420p[bg];[1:v]fps={fps},format=rgba[cap];"
             f"[bg][cap]overlay=0:0:eof_action=pass:format=auto,format=yuv420p[v]")
    ffmpeg("-loop", "1", "-framerate", str(fps), "-i", str(ep.path(f"shots/{seg['shot']}.png")),
           "-f", "concat", "-safe", "0", "-i", str(work / "captions.ffconcat"),
           "-i", str(ep.path(f"audio/{seg['id']}.wav")),
           "-filter_complex", graph, "-map", "[v]", "-map", "2:a",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-r", str(fps), "-g", str(fps * 2),
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
           "-frames:v", str(frames), "-t", f"{duration:.3f}", str(ep.path(f"clips/{seg['id']}.mp4")))


def _run(ep: Episode) -> None:
    script = read_json(ep.path("script.json"))
    timings = {t["id"]: t for t in read_json(ep.path("audio/timings.json"))["segments"]}
    claims = {c["id"]: c for c in read_json(ep.path("facts.json"))["claims"]}
    tail = ep.cfg["tts"]["segment_tail_silence"]
    ep.path("clips").mkdir(parents=True, exist_ok=True)
    srt, offset, cues = [], 0.0, []
    for i, seg in enumerate(script["narration_segments"]):
        timing = timings[seg["id"]]
        chunks = caption_chunks(seg["text_template"], claims)
        time_chunks(chunks, timing["words"], timing["duration"], tail)
        segment_clip(ep, seg, timing, chunks, i)
        for c in chunks:
            cues.append({"segment": seg["id"], "start": round(offset + c["start"], 3),
                         "end": round(offset + c["end"], 3), "text": c["display"]})
        offset += timing["duration"]
        ep.log(f"    clip {seg['id']} ({timing['duration']:.1f}s)")
    for n, c in enumerate(cues, start=1):
        srt.append(f"{n}\n{srt_time(c['start'])} --> {srt_time(c['end'])}\n{c['text']}\n")
    ep.path("captions.srt").write_text("\n".join(srt))
    write_json(ep.path("captions.json"), {"font": "Inter-SemiBold.ttf", "max_width_px": CAPTION_MAX_WIDTH,
                                          "max_lines": CAPTION_MAX_LINES, "font_size": CAPTION_SIZE, "cues": cues})

    concat = ep.path("work/clips.ffconcat")
    concat.write_text("ffconcat version 1.0\n" + "".join(
        f"file '{ep.path('clips') / (s['id'] + '.mp4')}'\n" for s in script["narration_segments"]))
    joined = ep.path("work/joined.mp4")
    ffmpeg("-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(joined))
    ffmpeg("-i", str(joined), "-c:v", "copy", "-af", LOUDNESS, "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
           "-movflags", "+faststart", str(ep.path("final.mp4")))
    ep.log(f"  assemble: final.mp4, {offset:.1f}s, {len(cues)} captions")


def run(ep: Episode) -> None:
    run_stage(ep, "assemble", VERSION, {"video": ep.cfg["video"], "size": ep.size},
              ["script.json", "facts.json", "audio", "shots"], ["final.mp4", "captions.srt", "captions.json"],
              lambda: _run(ep))


__all__ = ["run", "caption_chunks", "time_chunks", "PLACEHOLDER"]
