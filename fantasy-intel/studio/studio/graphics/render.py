"""Render a Short in one of the graphic styles, next to (never instead of) the original final.mp4.

    styles/<style>/final.mp4       the video (same narration, music, sound effects, timing and captions)
    styles/<style>/captions.json   the cues as burned in, with the caption font, for the subtitle gate
    styles/<style>/frames/*.png    representative frames (each scene settled, plus mid-scene)
    styles/<style>/vqa.json        the draw log of those frames and how long every number stayed readable
    styles/<style>/render.json     timing and settings

The timeline, cues and audio come from render_motion.plan, so only the pictures differ between styles.
"""

from __future__ import annotations

import math
import subprocess
import time
from pathlib import Path

from PIL import Image

from .. import format_scenes as FS
from .. import scenes as S
from ..core import Episode, sha256_file, write_json
from ..stages import render_motion as RM
from ..stages.assemble import srt_time
from . import kit as K
from . import pixelart, vector

STYLES = ("premium", "pixel", "broadcast")
SAMPLES = (0.45, 0.97)          # where in each scene a representative frame is captured


class Unsupported(Exception):
    pass


def scene_types(style: str) -> set[str]:
    return set(pixelart.SCENES if style == "pixel" else vector.SCENES)


def unsupported(style: str, types) -> list[str]:
    return sorted(set(types) - scene_types(style))


def _draw(style: str, sc: dict, tl: float, c, measure: bool):
    """(RGB frame, draw log, art frame for pixel) for one scene at local time tl."""
    if style == "pixel":
        F = pixelart.draw(sc, tl, sc["_dur"], c, measure)
        return F, F.log
    F = vector.draw(style, sc, tl, sc["_dur"], c, measure)
    return F, F.log


def render(ep: Episode, style: str, out: Path) -> dict:
    t0 = time.time()
    out.mkdir(parents=True, exist_ok=True)
    pl = RM.plan(ep, work=out)
    ctx, scenes, cues, total, fps = (pl[k] for k in ("ctx", "scenes", "cues", "total", "fps"))
    missing = unsupported(style, [sc["type"] for sc in scenes])
    if missing:
        raise Unsupported(f"style {style!r} has no design for scene types {missing}")
    frames = int(math.ceil(total * fps))
    sample_at = {int(round((sc["_start"] + sc["_dur"] * f) * fps)): (i, f) for i, sc in enumerate(scenes)
                 for f in SAMPLES}
    (out / "frames").mkdir(exist_ok=True)
    tk = K.tokens(style)
    tdur = tk.motion.transition_dur
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{K.W}x{K.H}", "-r", str(fps), "-i", "-",
         "-i", str(pl["mix"]), "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-g", str(fps * 2),
         "-af", RM.LOUDNESS, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
         "-frames:v", str(frames), "-movflags", "+faststart", str(out / "final.mp4")],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    prev_last = None
    cur = -1
    ci = 0
    samples, visible = [], {}
    for n in range(frames):
        t = n / fps
        i = max(j for j, sc in enumerate(scenes) if sc["_start"] <= t + 1e-9)
        sc = scenes[i]
        if i != cur:
            if cur >= 0:
                ctx.tokens = scenes[cur]["_tokens"]
                pf, _ = _draw(style, scenes[cur], scenes[cur]["_dur"], ctx, False)
                prev_last = _finish(style, pf, scenes[cur], scenes[cur]["_dur"], ctx)
            cur = i
        ctx.tokens = sc["_tokens"]
        tl = t - sc["_start"]
        measure = n in sample_at
        F, log = _draw(style, sc, tl, ctx, measure)
        while ci + 1 < len(cues) and cues[ci + 1]["start"] <= t:
            ci += 1
        cue = cues[ci]
        cap = None
        if cue["start"] <= t < cue["end"]:
            active = max((j for j, w in enumerate(cue["words"]) if cue["offset"] + w["start"] <= t), default=0)
            cap = (cue["words"], active, t - cue["start"])
        if style == "pixel":
            frame = _finish(style, F, sc, tl, ctx)
            if prev_last is not None and tl < tdur:
                frame = pixelart.transition(prev_last, frame, tl / tdur)
            if cap:                    # the dialog box sits above the fade, so captions never dim
                CF = pixelart.X.PixelFrame(pixelart.S, measure)
                pixelart.caption(CF, cap[0], cap[1], ctx)
                box = tuple(v * pixelart.S for v in pixelart.CAPTION_BOX)
                frame.paste(CF.upscale().crop(box), box[:2])
                log = log + CF.log
        else:
            frame = _finish(style, F, sc, tl, ctx)
            if prev_last is not None and tl < tdur:
                frame = vector.transition(style, prev_last, frame, tl / tdur, ctx)
            if cap:
                log = log + vector.caption(style, frame, cap[0], cap[1], cap[2], ctx, measure)
        for e in log:
            if e["role"] == "number" and e["alpha"] >= 0.95:
                v = visible.setdefault(e["src"] or "?", {"text": e["text"], "frames": 0, "first": t})
                v["frames"] += 1
        if measure:
            si, f = sample_at[n]
            name = f"s{si + 1:02d}_{sc['type']}_{int(f * 100):02d}.png"
            frame.save(out / "frames" / name)
            samples.append({"frame": name, "t": round(t, 3), "scene": sc["type"], "log": log,
                            "pixel_art": style == "pixel"})
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait():
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.read().decode()[-1500:]}")
    _captions(style, cues, total, out)
    for v in visible.values():
        v["seconds"] = round(v.pop("frames") / fps, 2)
    caption_top = pixelart.CAPTION_BOX[1] * pixelart.S if style == "pixel" else vector.CAPTION_ZONE
    write_json(out / "vqa.json", {"style": style, "samples": samples, "numbers_visible": visible,
                                  "caption_top": caption_top})
    report = {"style": style, "seconds": round(time.time() - t0, 1), "frames": frames, "duration": round(total, 3),
              "render_fps": round(frames / max(time.time() - t0, 0.01), 1),
              "tokens": str(K.STYLES_DIR / f"{style}.toml"),
              "tokens_sha256": sha256_file(K.STYLES_DIR / f"{style}.toml")}
    write_json(out / "render.json", report)
    return report


def _finish(style: str, F, sc: dict, tl: float, c) -> Image.Image:
    """Camera (push and punch-ins for vector styles; a one-pixel shake for pixel art) on a drawn frame."""
    hits = S.punches(sc, c, sc["_dur"]) + FS.punches(sc, c, sc["_dur"])
    if style == "pixel":
        shake = any(0 <= tl - tp < 0.12 for tp in hits)
        img = F.upscale()
        if shake:
            sh = Image.new("RGB", img.size, (11, 14, 26))
            sh.paste(img, (pixelart.S * (1 if int(tl * 30) % 2 else -1), 0))
            img = sh
        return img
    tk = K.tokens(style)
    punch = sum(tk.motion.punch * math.exp(-(tl - tp) / 0.16) for tp in hits if tl >= tp)
    return vector.camera(style, F.img, min(tl / max(sc["_dur"], 0.1), 1), punch)


def _captions(style: str, cues: list[dict], total: float, out: Path) -> None:
    spec = pixelart.caption_spec() if style == "pixel" else vector.caption_spec(style)
    upper = spec.get("case") == "upper"
    flat = [{"start": round(cq["start"], 3), "end": round(cq["end"], 3),
             "text": cq["text"].upper() if upper else cq["text"], "segment": cq["segment"]} for cq in cues]
    (out / "captions.srt").write_text("\n".join(
        f"{i}\n{srt_time(cq['start'])} --> {srt_time(cq['end'])}\n{cq['text']}\n" for i, cq in enumerate(flat, 1)))
    write_json(out / "captions.json", {**spec, "cues": flat})


VERSION = 1


def run(ep: Episode, style: str) -> bool:
    """The cached render stage for a style: reruns only when the story, narration, audio, the renderer's code
    or the style's tokens change. Research, facts, script and narration are never redone for a style."""
    from ..core import run_stage, sha256_file as h
    from ..stages import qc
    from . import kit, pixel, vqa
    code = {m.__name__: h(Path(m.__file__)) for m in (vector, pixelart, pixel, kit, RM, S, FS, vqa)}
    out = ep.path(f"styles/{style}")
    rel = f"styles/{style}"
    run_stage(ep, f"render-{style}", VERSION,
              {"style": style, "tokens": h(K.STYLES_DIR / f"{style}.toml"), "code": code, "video": ep.cfg["video"],
               "shorts": ep.cfg["shorts"]},
              ["script.json", "facts.json", "audio", "story.json"],
              [f"{rel}/final.mp4", f"{rel}/captions.json", f"{rel}/vqa.json"],
              lambda: render(ep, style, out))
    return qc.run_style(ep, style)
