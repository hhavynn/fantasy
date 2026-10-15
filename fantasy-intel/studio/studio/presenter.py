"""Optional AI presenter layer, applied on top of a finished Short. EXPERIMENTAL; `off` is the default.

The presenter is visual only. It reads an episode that already rendered and passed QC (final.mp4, the Kokoro
segment WAVs, word timings, the scene timeline) and writes a separate final_presenter_<mode>.mp4. final.mp4,
the script, the narration, every claim and the audio track are untouched: the variant's audio is a stream
copy of the original, and the presenter QC checks it bit for bit.

  off     nothing happens (the current pipeline, unchanged)
  hook    the host delivers the opening line, then the graphics take over
  hybrid  the host delivers the opening line and the closing verdict; the analysis stays in graphics

While the host is on screen, the scene the narration refers to keeps playing in a card above him, so no stat or
player visual disappears; captions are redrawn from the same word timings, below his face.

Steps (each cached by the hash of its inputs):
  1. host portrait: assets/presenter/host.png, a fictional person generated locally with Z-Image-Turbo
     (Apache-2.0), see assets/presenter/host.json. The same image for every video keeps him consistent.
  2. base clip: the portrait with slow procedural head motion and breathing (25 fps frames); synthetic
     blinks were tried and switched off
  3. lip sync: MuseTalk 1.5 (MIT) on PyTorch MPS in its own environment (studio/presenter_worker.py),
     driven by the existing segment WAV of each window
  4. composite: host shot with the scene card, captions and an "AI HOST" tag, cut into final.mp4's frames
  5. presenter QC; on any failure the variant is withheld and final.mp4 stays the publishable file
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from PIL import Image, ImageDraw, ImageFilter

from . import motion as M
from .core import STUDIO_ROOT, Config, Episode, read_json, sha256_bytes, sha256_file, utcnow, write_json

MODES = ("off", "hook", "hybrid")
VERSION = 1
FPS_HOST = 25
WORKER = Path(__file__).with_name("presenter_worker.py")

# Layout of the host shot (1080x1920): the scene card on top, his face below it, captions under his chin.
CARD_BOX = (0, 200, 1080, 1300)          # the part of the original frame shown in the card (no captions)
CARD_SCALE = 0.56
CARD_TOP = 240
FACE_TOP = 880                           # where the top of his face lands
FACE_WIDTH = 330
CAPTION_TOP = M.CAPTION_Y - 165          # the caption layer's top edge
CROSSFADE = 4                            # frames of dissolve at each cut in and out
WARMUP = 3                               # MuseTalk's first frames (inside the dissolve), not judged by QC
BLINK = [0.35, 0.8, 1.0, 0.75, 0.3]      # eye closure per frame at 25 fps (~0.2 s)
# Synthetic blinks are OFF: warping the lid looked wrong in motion (owner review). On 2-6 s clips no blink beats
# a broken one; a real idle clip with natural blinks is the proper fix.
BLINKS_AT: list[float] = []

LICENSES = {
    "portrait_model": "Tongyi-MAI/Z-Image-Turbo (Apache-2.0), run locally with mflux (MIT)",
    "lipsync": "MuseTalk 1.5 code and weights (MIT), via musetalk-mac (MIT), PyTorch MPS",
    "components": "sd-vae-ft-mse (MIT), whisper-tiny (Apache-2.0), BiSeNet face parsing (MIT), "
                  "Mediapipe face mesh (Apache-2.0)",
    "not_used": "Wav2Lip (banned; the worker proves MuseTalk's training-only Wav2Lip-derived module never loads), "
                "LivePortrait/InsightFace (non-commercial), Kling/Replicate/ElevenLabs (paid)",
}


class PresenterError(RuntimeError):
    pass


def settings(cfg: Config) -> dict:
    p = dict(getattr(cfg, "raw", {}).get("presenter") or {})
    return {"mode": p.get("mode", "off"), "engine": Path(p.get("engine", "~/models/musetalk-mac")).expanduser(),
            "cache": Path(p.get("cache", "~/models/presenter")).expanduser(),
            "portrait": STUDIO_ROOT / p.get("portrait", "assets/presenter/host.png")}


# ------------------------------------------------------------------------------------------- windows

def windows(ep: Episode, mode: str) -> list[dict]:
    """The stretches the host delivers: the hook line, and in hybrid also the closing verdict."""
    scenes = read_json(ep.path("work/timeline.json"))["scenes"]
    picks = [scenes[0]] + ([scenes[-1]] if mode == "hybrid" and len(scenes) > 2 else [])
    return [{"segment": s["segment"], "start": s["_start"], "end": s["_start"] + s["_dur"],
             "audio": str(ep.path(f"audio/{s['segment']}.wav")), "scene": s["type"]} for s in picks]


# ------------------------------------------------------------------------------------------- worker

def _worker(st: dict, args: list[str]) -> None:
    py = st["engine"] / ".venv" / "bin" / "python"
    if not py.exists():
        raise PresenterError(f"MuseTalk environment not found at {st['engine']} (see docs/presenter/README.md)")
    # -I: isolated, so studio/numbers.py next to the worker can't shadow the standard library
    proc = subprocess.run([str(py), "-I", str(WORKER), *args], capture_output=True, text=True)
    if proc.returncode:
        raise PresenterError(f"presenter worker failed: {(proc.stderr or proc.stdout)[-1200:]}")


def landmarks(st: dict) -> dict:
    out = st["cache"] / f"host_{sha256_file(st['portrait'])[:12]}_landmarks.json"
    if not out.exists():
        _worker(st, ["landmarks", "--image", str(st["portrait"]), "--out", str(out)])
    return json.loads(out.read_text())


# ----------------------------------------------------------------------------------------- base clip

def blink(img: Image.Image, eyes: dict, c: float) -> Image.Image:
    """Close the eyes by fraction c: lid skin from just above each eye slides down over it, feathered."""
    if c <= 0.01:
        return img
    arr = np.asarray(img).astype(np.float32)
    out = arr.copy()
    for eye in eyes.values():
        (ux, uy), (lx, ly) = eye["upper"], eye["lower"]
        (ox, oy), (ix, iy) = eye["outer"], eye["inner"]
        cx, w = (ox + ix) / 2, abs(ox - ix) * 1.35
        h = max(ly - uy, 4.0)
        top, bot = int(uy - h * 1.6), int(ly + h * 0.6)
        x0, x1 = int(cx - w / 2), int(cx + w / 2)
        rows = np.arange(top, bot)
        lid = uy + c * (ly - uy + h * 0.25)              # where the lid edge is now
        src = np.where(rows < lid, rows - (lid - uy), rows)
        src = np.clip(src, 0, arr.shape[0] - 1).astype(int)
        patch = arr[src][:, x0:x1]
        yy, xx = np.mgrid[top:bot, x0:x1]
        mask = np.clip(1 - (((xx - cx) / (w / 2)) ** 2 + ((yy - (uy + ly) / 2) / (h * 1.5)) ** 2), 0, 1) ** 0.5
        lash = np.exp(-((yy - lid) / 1.6) ** 2) * (np.abs(xx - cx) < w * 0.42) * 0.35 * c
        blended = arr[top:bot, x0:x1] * (1 - mask[..., None]) + patch * mask[..., None]
        out[top:bot, x0:x1] = blended * (1 - lash[..., None])
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def base_frames(st: dict, lm: dict, seconds: float = 6.0) -> Path:
    """The idle clip MuseTalk animates: slow sway, breathing scale, two blinks. MuseTalk plays it forward then
    backward, so the motion loops smoothly."""
    key = sha256_bytes(json.dumps([sha256_file(st["portrait"]), seconds, BLINK, BLINKS_AT, VERSION]).encode())[:12]
    out = st["cache"] / f"base_{key}"
    if (out / "done").exists():
        return out
    out.mkdir(parents=True, exist_ok=True)
    src = Image.open(st["portrait"]).convert("RGB")
    W, H = src.size
    fx0, fy0, fx1, fy1 = lm["face_box"]
    fcx, fcy = (fx0 + fx1) / 2, (fy0 + fy1) / 2
    blinks = [int(s_ * FPS_HOST) for s_ in BLINKS_AT]
    for i in range(int(seconds * FPS_HOST)):
        t = i / FPS_HOST
        ang = math.radians(0.55 * math.sin(2 * math.pi * t / 6.1) + 0.25 * math.sin(2 * math.pi * t / 2.7))
        s = 1 + 0.006 * math.sin(2 * math.pi * t / 3.9)
        dx = 3.0 * math.sin(2 * math.pi * t / 5.3) + 1.2 * math.sin(2 * math.pi * t / 2.1)
        dy = 2.0 * math.sin(2 * math.pi * t / 4.4)
        c = next((BLINK[i - b] for b in blinks if 0 <= i - b < len(BLINK)), 0.0)
        frame = blink(src, lm["eyes"], c) if c else src
        # inverse affine: rotate and scale about the face centre, then shift
        a_, b_ = math.cos(ang) / s, math.sin(ang) / s
        d_, e_ = -math.sin(ang) / s, math.cos(ang) / s
        c_ = fcx - a_ * fcx - b_ * fcy - dx
        f_ = fcy - d_ * fcx - e_ * fcy - dy
        frame.transform((W, H), Image.AFFINE, (a_, b_, c_, d_, e_, f_), resample=Image.BICUBIC).save(
            out / f"{i:08d}.png")
    (out / "done").write_text(utcnow())
    return out


# ------------------------------------------------------------------------------------------- lip sync

def lipsync(st: dict, frames: Path, audio: Path) -> tuple[Path, dict]:
    key = sha256_bytes(json.dumps([frames.name, sha256_file(audio), VERSION]).encode())[:16]
    out = st["cache"] / "clips" / f"{key}.mp4"
    report = out.with_suffix(".json")
    if out.exists() and report.exists():
        return out, {**json.loads(report.read_text()), "cached": True}
    out.parent.mkdir(parents=True, exist_ok=True)
    _worker(st, ["lipsync", "--upstream", str(st["engine"] / "upstream"), "--frames", str(frames),
                 "--audio", str(audio), "--out", str(out), "--report", str(report),
                 "--work", str(st["cache"] / "musetalk_work" / frames.name)])
    return out, json.loads(report.read_text())


def read_frames(video: Path, size: tuple[int, int] | None = None) -> np.ndarray:
    w, h = size or _probe_size(video)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3)


def _probe_size(video: Path) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", str(video)], capture_output=True, text=True, check=True).stdout
    w, h = out.strip().split(",")
    return int(w), int(h)


def _frame_count(video: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0", "-show_entries",
                          "stream=nb_read_packets", "-of", "csv=p=0", str(video)], capture_output=True, text=True)
    return int(out.stdout.strip())


# ------------------------------------------------------------------------------------------ composite

def host_placement(lm: dict) -> tuple[float, float, float]:
    """Scale and offset that put his face FACE_WIDTH wide, centred, with its top at FACE_TOP."""
    fx0, fy0, fx1, fy1 = lm["face_box"]
    s = FACE_WIDTH / (fx1 - fx0)
    return s, M.CENTER_X - s * (fx0 + fx1) / 2, FACE_TOP - s * fy0


def _fade_layer() -> Image.Image:
    alpha = np.linspace(0, 1, 700)[:, None] ** 1.4 * 170
    a = Image.fromarray(np.repeat(alpha, M.W, 1).astype(np.uint8), "L")
    layer = Image.new("RGBA", (M.W, 700), (6, 8, 12, 0))
    layer.putalpha(a)
    return layer


FADE = None
PLATES: dict = {}


def plate(host: Image.Image, place) -> Image.Image:
    """His own studio background, enlarged to fill the frame and blurred: the wall behind him continues past
    the edges of his shot, so it reads as one camera frame rather than a box pasted on the video."""
    key = (host.size, place)
    if key not in PLATES:
        s, ox, oy = place
        k = max(M.W / host.width, M.H / host.height) * 1.08
        big = host.resize((int(host.width * k), int(host.height * k)), Image.BILINEAR)
        cx = (M.W / 2 - ox) / s * k                      # keep his face roughly where it sits in the shot
        x0 = int(min(max(cx - M.W / 2, 0), big.width - M.W))
        y0 = int(max(min(big.height - M.H, (oy * -1) / s * k), 0))
        crop = big.crop((x0, y0, x0 + M.W, y0 + M.H)).filter(ImageFilter.GaussianBlur(28))
        PLATES[key] = Image.eval(crop, lambda v: int(v * 0.8)).convert("RGBA")
    return PLATES[key]


def compose(original: Image.Image, host: Image.Image, place, caption: Image.Image | None) -> Image.Image:
    global FADE
    s, ox, oy = place
    canvas = plate(host, place).copy()
    hs = host.resize((int(host.width * s), int(host.height * s)), Image.LANCZOS).convert("RGBA")
    edge = Image.new("L", hs.size, 0)
    ImageDraw.Draw(edge).rectangle((90, 90, hs.width - 90, hs.height), fill=255)
    hs.putalpha(edge.filter(ImageFilter.GaussianBlur(70)))      # feather his frame into the plate
    canvas.alpha_composite(hs, (int(ox), int(oy)))
    FADE = FADE or _fade_layer()
    canvas.alpha_composite(FADE, (0, M.H - FADE.height))        # captions read over his shirt
    # the scene the narration is about, still playing, in a card above him
    crop = original.crop(CARD_BOX)
    cw, ch = int(crop.width * CARD_SCALE), int(crop.height * CARD_SCALE)
    crop = crop.resize((cw, ch), Image.LANCZOS)
    mask = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, cw - 1, ch - 1), radius=26, fill=255)
    cx = int(M.CENTER_X - cw / 2)
    shadow = Image.new("RGBA", (cw + 60, ch + 60), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((30, 40, cw + 30, ch + 40), radius=30, fill=(0, 0, 0, 160))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)), (cx - 30, CARD_TOP - 30))
    canvas.paste(crop, (cx, CARD_TOP), mask)
    ImageDraw.Draw(canvas).rounded_rectangle((cx, CARD_TOP, cx + cw - 1, CARD_TOP + ch - 1), radius=26,
                                             outline=(255, 255, 255, 70), width=3)
    tag = M.text_layer("AI HOST", "black", 26, M.INK, shadow=False)
    pill = Image.new("RGBA", (tag.width + 6, tag.height - 10), (0, 0, 0, 0))
    ImageDraw.Draw(pill).rounded_rectangle((0, 8, pill.width - 1, pill.height - 8), radius=20, fill=(0, 0, 0, 150))
    pill.alpha_composite(tag, (3, -5))
    canvas.alpha_composite(pill, (M.SAFE_LEFT + 10, CAPTION_TOP - pill.height - 6))
    if caption is not None:
        canvas.alpha_composite(caption, (0, CAPTION_TOP))
    return canvas.convert("RGB")


def caption_cues(ep: Episode) -> tuple[list[dict], tuple]:
    """The caption cues the renderer drew (same alignment and phrasing code), to redraw over the host."""
    from .stages.render_motion import align, phrases
    script = read_json(ep.path("script.json"))
    facts = read_json(ep.path("facts.json"))
    claims = {c["id"]: c for c in facts["claims"]}
    timings = {s["id"]: s for s in read_json(ep.path("audio/timings.json"))["segments"]}
    cues, offset = [], 0.0
    for seg in script["narration_segments"]:
        tm = timings[seg["id"]]
        for ph in phrases(align(seg, tm["words"], claims)):
            cues.append({"words": ph, "offset": offset, "start": offset + ph[0]["start"]})
        offset += tm["duration"]
    for i, cue in enumerate(cues):
        cue["end"] = cues[i + 1]["start"] if i + 1 < len(cues) else offset
    story = script["structure"]["angle"]
    role = facts["players"][0]["role"]
    accent = M.GOLD if story in ("fill_in", "mystery", "opportunity") else (M.DOWN if role == "faller" else M.UP)
    return cues, accent


def caption_at(cues: list[dict], t: float, accent) -> Image.Image | None:
    cue = next((c for c in cues if c["start"] <= t < c["end"]), None)
    if cue is None:
        return None
    active = max((j for j, w in enumerate(cue["words"]) if cue["offset"] + w["start"] <= t), default=0)
    return M.caption_frame(cue["words"], active, accent, t - cue["start"])


def _composite(ep: Episode, clips: list[dict], place, out: Path) -> None:
    fps = ep.cfg["video"]["fps"]
    original = read_frames(ep.path("final.mp4"), (M.W, M.H))
    cues, accent = caption_cues(ep)
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{M.W}x{M.H}", "-r", str(fps), "-i", "-", "-i", str(ep.path("final.mp4")),
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
         "-g", str(fps * 2), "-c:a", "copy", "-movflags", "+faststart", str(out)], stdin=subprocess.PIPE)
    for n, arr in enumerate(original):
        t = n / fps
        frame = arr
        for c in clips:
            first, last = int(math.ceil(c["start"] * fps)), int(math.ceil(c["end"] * fps)) - 1
            if first <= n <= last:
                k = min(int((t - c["start"]) * FPS_HOST), len(c["frames"]) - 1)
                comp = np.asarray(compose(Image.fromarray(arr), Image.fromarray(c["frames"][k]), place,
                                          caption_at(cues, t, accent)))
                tail = last - n if last < len(original) - 1 else CROSSFADE   # no fade-out when he closes the video
                a = M.clamp((min(n - first, tail) + 1) / CROSSFADE)
                frame = comp if a >= 1 else (comp * a + arr * (1 - a)).astype(np.uint8)
                break
        proc.stdin.write(np.ascontiguousarray(frame).tobytes())
    proc.stdin.close()
    if proc.wait():
        raise PresenterError("ffmpeg failed while writing the presenter variant")


# ------------------------------------------------------------------------------------------------ QC

def mouth_series(st: dict, clip_path: Path) -> dict:
    out = clip_path.with_suffix(".mouth.json")
    if not out.exists():
        _worker(st, ["mouth", "--video", str(clip_path), "--out", str(out)])
    return json.loads(out.read_text())


def clip_qc(clip: np.ndarray, audio: Path, lm: dict, frames_expected: int, mouth_open: dict) -> dict:
    """Checks on the lip-synced clip itself (host resolution, 25 fps)."""
    n = len(clip)
    out = {"exists": {"pass": n >= frames_expected * 0.95, "frames": n, "expected": frames_expected}}
    flat = clip.reshape(n, -1)
    means, stds = flat.mean(1), flat.std(1)
    out["not_corrupt"] = {"pass": bool((means > 12).all() and (means < 243).all() and (stds > 8).all()),
                          "mean_range": [round(float(means.min()), 1), round(float(means.max()), 1)]}
    mo = lm["mouth"]
    my0, my1 = int(mo["upper"][1]) - 25, int(mo["lower"][1]) + 30
    mx0, mx1 = int(mo["left"][0]) - 10, int(mo["right"][0]) + 10
    mouth = clip[:, my0:my1, mx0:mx1].astype(np.float32).mean(-1)
    motion = np.abs(np.diff(mouth, axis=0)).mean((1, 2))
    out["mouth_moves"] = {"pass": bool(motion.mean() > 0.8), "mean_motion": round(float(motion.mean()), 2)}
    # Lip-sync sanity, not SyncNet. The mouth opening is measured from Mediapipe landmarks on every generated
    # frame (a pixel-darkness proxy proved unusable); it must track loudness, and be clearly more open while
    # he speaks than in the pauses. That catches the catastrophic failures: a mouth flapping through silence,
    # or a frozen mouth during speech.
    series = [v for v in mouth_open["open"]][:n]
    if not series or sum(v is None for v in series) > 0.05 * len(series):
        out["face_tracked"] = {"pass": False, "frames_without_face": mouth_open.get("frames_without_face")}
        return out
    out["face_tracked"] = {"pass": True, "frames_without_face": mouth_open.get("frames_without_face", 0)}
    dark = np.array(series + [series[-1]] * (n - len(series)), np.float32)
    a, sr = sf.read(audio, dtype="float32")
    a = a if a.ndim == 1 else a.mean(1)
    hop = sr / FPS_HOST
    env = np.array([np.sqrt(np.mean(a[int(i * hop):int((i + 1) * hop)] ** 2) + 1e-12) for i in range(n)])
    voiced, silent = env > 0.25 * env.max(), env < 0.05 * env.max()
    # MuseTalk's first frames are a warm-up (audio padding); they sit inside the cross-fade, so skip them
    voiced[:WARMUP], silent[:WARMUP] = False, False
    corr = max(np.corrcoef(np.roll(dark, k), env)[0, 1] for k in range(-3, 4))
    if voiced.sum() >= 3 and silent.sum() >= 3:
        contrast = float(dark[voiced].mean() - dark[silent].mean())
        ok, how = contrast > 0.01 and corr > 0.15, "open while speaking vs closed in pauses, and tracks loudness"
    else:                                   # no pause in the clip: fall back to the smoothed correlation
        contrast, ok, how = None, corr > 0.15, "correlation with loudness (no pause in the clip)"
    out["lipsync_tracks_audio"] = {"pass": bool(ok), "open_contrast": None if contrast is None else round(contrast, 3),
                                   "best_corr": round(float(corr), 3), "test": how,
                                   "note": "landmark mouth opening vs loudness; a sanity gate, not a SyncNet score"}
    fx0, fy0, fx1, fy1 = (int(v) for v in lm["face_box"])
    lum = clip[:, fy0:fy1, fx0:fx1].astype(np.float32).mean((1, 2, 3))
    jump = np.abs(np.diff(lum))
    out["no_flicker"] = {"pass": bool(jump.max() < 6), "max_luma_jump": round(float(jump.max()), 2)}
    return out


def layout_qc(lm: dict) -> dict:
    s, ox, oy = host_placement(lm)
    fx0, fy0, fx1, fy1 = lm["face_box"]
    box = (fx0 * s + ox, fy0 * s + oy, fx1 * s + ox, fy1 * s + oy)
    card_bottom = CARD_TOP + (CARD_BOX[3] - CARD_BOX[1]) * CARD_SCALE
    return {"face_in_frame": {"pass": bool(box[0] > M.SAFE_LEFT and box[2] < M.SAFE_RIGHT
                                           and box[1] > card_bottom - 20 and box[3] < CAPTION_TOP + 10),
                              "face_box": [round(v) for v in box], "card_bottom": round(card_bottom),
                              "caption_top": CAPTION_TOP},
            "no_caption_collision": {"pass": bool(card_bottom < CAPTION_TOP), "card_bottom": round(card_bottom)}}


def _audio_sha(video: Path) -> str:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-map", "0:a", "-c", "copy", "-f", "adts", "-"],
                         capture_output=True, check=True).stdout
    return sha256_bytes(raw)


# ---------------------------------------------------------------------------------------------- apply

def apply(ep: Episode, mode: str) -> Path | None:
    """Make final_presenter_<mode>.mp4, or return None (reason logged and recorded) if anything fails."""
    if mode == "off":
        return None
    if mode not in MODES:
        raise ValueError(f"presenter mode must be one of {MODES}")
    st = settings(ep.cfg)
    st["cache"].mkdir(parents=True, exist_ok=True)
    out = ep.path(f"final_presenter_{mode}.mp4")
    rec = {"episode_id": ep.id, "mode": mode, "version": VERSION, "experimental": True, "started_at": utcnow(),
           "licenses": LICENSES, "timings": {}}
    t0 = time.time()
    try:
        if not st["portrait"].exists():
            raise PresenterError(f"host portrait missing: {st['portrait']}")
        lm = landmarks(st)
        rec["portrait_sha256"] = sha256_file(st["portrait"])
        frames = base_frames(st, lm)
        rec["timings"]["base_clip_s"] = round(time.time() - t0, 1)
        clips, gates = [], {"layout": layout_qc(lm)}
        for w in windows(ep, mode):
            t1 = time.time()
            path, report = lipsync(st, frames, Path(w["audio"]))
            clip = read_frames(path)
            dur = sf.info(w["audio"]).duration
            g = clip_qc(clip, Path(w["audio"]), lm, int(dur * FPS_HOST) - 1, mouth_series(st, path))
            g["no_banned_modules"] = {"pass": not report.get("banned_modules_loaded"),
                                      "loaded": report.get("banned_modules_loaded")}
            gates[w["segment"]] = g
            rec["timings"][f"lipsync_{w['segment']}_s"] = round(time.time() - t1, 1)
            rec["timings"][f"lipsync_{w['segment']}_engine_s"] = report.get("seconds")
            clips.append({**w, "frames": clip, "audio_s": round(dur, 3), "cached": report.get("cached", False)})
        t2 = time.time()
        _composite(ep, clips, host_placement(lm), out)
        rec["timings"]["composite_s"] = round(time.time() - t2, 1)
        o_frames, v_frames = _frame_count(ep.path("final.mp4")), _frame_count(out)
        gates["invariants"] = {
            "audio_identical": {"pass": _audio_sha(out) == _audio_sha(ep.path("final.mp4")),
                                "note": "narration and mix are a stream copy of final.mp4"},
            "timing_identical": {"pass": o_frames == v_frames, "frames": [o_frames, v_frames]},
            "resolution": {"pass": _probe_size(out) == (M.W, M.H), "size": list(_probe_size(out))},
        }
        rec["windows"] = [{k: v for k, v in c.items() if k != "frames"} for c in clips]
        rec["gates"] = gates
        failed = [f"{g}.{k}" for g, checks in gates.items() for k, v in checks.items() if not v["pass"]]
        rec["passed"], rec["failures"] = not failed, failed
        if failed:
            rejected = out.with_name(out.stem + ".rejected.mp4")
            shutil.move(out, rejected)
            rec["rejected_file"] = rejected.name
            ep.log(f"  presenter {mode}: QC failed {failed}; kept final.mp4, variant saved as {rejected.name}")
            return None
        _metadata(ep, mode)
        ep.log(f"  presenter {mode}: {out.name} ({time.time() - t0:.0f}s), QC passed")
        return out
    except Exception as e:                       # fail safe: the original video path is never affected
        rec["passed"], rec["error"] = False, f"{type(e).__name__}: {e}"
        ep.log(f"  presenter {mode}: skipped ({rec['error'][:200]}); final.mp4 unchanged")
        return None
    finally:
        rec["finished_at"] = utcnow()
        rec["timings"]["total_s"] = round(time.time() - t0, 1)
        write_json(ep.path(f"presenter_{mode}.json"), rec)


def _metadata(ep: Episode, mode: str) -> None:
    meta = read_json(ep.path("metadata.json"))
    meta["description"] = meta["description"].rstrip() + "\nThe on-screen host is an AI-generated character, not a real person."
    meta["containsSyntheticMedia"] = True
    meta["presenter"] = {"mode": mode, "ai_generated_host": True}
    write_json(ep.path(f"metadata_presenter_{mode}.json"), meta)


# ------------------------------------------------------------------------------------------------ CLI

def parse_episode(episode_id: str, cfg: Config) -> Episode:
    season, wk, fmt, *slug = episode_id.split("-", 3)
    return Episode(int(season), int(wk.lstrip("w")), fmt, cfg, slug=slug[0] if slug else None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="make-presenter", description="Add the AI host to finished Shorts.")
    ap.add_argument("episodes", nargs="+", help="episode ids, e.g. 2026-w06-short-braelon-allen")
    ap.add_argument("--mode", choices=MODES[1:], default="hook")
    a = ap.parse_args(argv)
    cfg = Config.load()
    ok = True
    for eid in a.episodes:
        ep = parse_episode(eid, cfg)
        if not ep.path("final.mp4").exists():
            print(f"{eid}: no final.mp4; render it first")
            ok = False
            continue
        ok &= apply(ep, a.mode) is not None
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
