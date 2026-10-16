"""Visual-engagement audit for a rendered Short: does the picture keep changing in ways that mean something?

Measured from final.mp4 (sampled every 0.2 s at phone size), with the caption band masked out so subtitles changing
word by word never count as visual variety:

  first_frame_fill      share of the content area that is not flat on frame 0 (16 px tiles with real detail)
  first_reveal_s        first moment the composition changes meaningfully from frame 0
  compositions          distinct compositions (frames clustered by their blurred 18x32 colour layout)
  longest_hold_s        the longest stretch that stays one composition
  dominant_share        the share of the video taken by the most common composition
  empty_share           mean share of flat tiles in the content area (big empty backgrounds)
  hero_share            time-weighted share of the frame height the player occupies (declared by each scene's
                        `hero`; a legacy scene without one is counted from its known headshot size)
  sync                  for each sound cue, the picture's frame-to-frame change at the hit against the video's typical
                        change: a ratio of 3+ means the picture reacts with the sound
  repeat_vs             share of this video's frames that look like some frame of another video (near-duplicates)

These are editorial heuristics for review, not retention predictions. Decorative motion barely moves them: a
composition only changes when the blurred layout of the frame changes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 270, 480                     # phone-size samples
CAPTION = (1300 / 1920, 1620 / 1920)  # the caption band (fractions of height), masked out
CONTENT = (200 / 1920, 1300 / 1920)   # where the story is drawn
STEP = 0.2
NEW_COMP = 0.085                     # mean abs layout difference (0-1) that counts as a new composition
FLAT = 6.0                           # a 16 px tile with luminance std under this is flat

# headshot width used by legacy scene types (px of 1080), for hero_share of renders made before scenes declared it
LEGACY_HERO = {"cmp_field": 150 / 1920 * 0.75, "cmp_tally": 0.0, "dp_toggle": 120 * 0.73 / 1920,
               "dp_stat": 120 * 0.73 / 1920, "dp_line": 120 * 0.73 / 1920, "dp_end": 120 * 0.73 / 1920,
               "court_docket": 300 * 0.73 / 1920, "court_exhibit": 0.0, "court_verdict": 0.0,
               "cine_slam": 420 * 0.73 / 1920, "cine_contrast": 0.0, "cine_poster": 620 * 0.73 / 1920,
               "cine_title": 760 * 0.73 / 1920}


def frames(video: Path, step: float = STEP) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vf", f"fps={1 / step},scale={W}:{H}",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, H, W, 3)


def _masked(f: np.ndarray) -> np.ndarray:
    a, b = int(CAPTION[0] * H), int(CAPTION[1] * H)
    g = f.astype(np.float32).copy()
    g[a:b] = 0
    return g


def signature(f: np.ndarray) -> np.ndarray:
    """The frame's coarse colour layout (18x32, captions masked): what a viewer reads as 'the shot'."""
    img = Image.fromarray(_masked(f).astype(np.uint8)).resize((18, 32), Image.BOX)
    return np.asarray(img, np.float32) / 255


def fill(f: np.ndarray) -> float:
    a, b = int(CONTENT[0] * H), int(CONTENT[1] * H)
    lum = f[a:b].astype(np.float32).mean(2)
    t = 8
    hh, ww = lum.shape[0] // t * t, lum.shape[1] // t * t
    tiles = lum[:hh, :ww].reshape(hh // t, t, ww // t, t).std(axis=(1, 3))
    return float((tiles > FLAT).mean())


def measure(ep_dir: Path, other: Path | None = None) -> dict:
    ep_dir = Path(ep_dir)
    fr = frames(ep_dir / "final.mp4")
    sigs = [signature(f) for f in fr]
    dur = len(fr) * STEP
    # greedy clustering of layouts
    centers, labels = [], []
    for s in sigs:
        d = [float(np.abs(s - c).mean()) for c in centers]
        if d and min(d) < NEW_COMP:
            labels.append(int(np.argmin(d)))
        else:
            centers.append(s)
            labels.append(len(centers) - 1)
    runs, cur, n = [], labels[0], 0
    for lab in labels:
        if lab == cur:
            n += 1
        else:
            runs.append(n)
            cur, n = lab, 1
    runs.append(n)
    counts = np.bincount(labels)
    first_reveal = next((i * STEP for i, s in enumerate(sigs) if float(np.abs(s - sigs[0]).mean()) >= NEW_COMP), None)
    fills = [fill(f) for f in fr]
    out = {"episode": ep_dir.name, "duration_s": round(dur, 1), "first_frame_fill": round(fills[0], 2),
           "first_reveal_s": round(first_reveal, 2) if first_reveal is not None else None,
           "compositions": len(centers), "longest_hold_s": round(max(runs) * STEP, 1),
           "dominant_share": round(float(counts.max() / len(labels)), 2),
           "empty_share": round(1 - float(np.mean(fills)), 2), "hero_share": hero_share(ep_dir),
           "sync": sync(ep_dir)}
    if other:
        out["repeat_vs"] = {Path(other).name: repeat(sigs, [signature(f) for f in frames(Path(other) / "final.mp4")])}
    return out


def hero_share(ep_dir: Path) -> float | None:
    tl = ep_dir / "work/timeline.json"
    if not tl.exists():
        return None
    from .edit_scenes import HERO
    scenes = json.loads(tl.read_text())["scenes"]
    total = sum(s["_dur"] for s in scenes) or 1
    return round(sum(s.get("hero", HERO.get(s["type"], LEGACY_HERO.get(s["type"], 0.0))) * s["_dur"] for s in scenes) / total, 2)


def sync(ep_dir: Path) -> dict:
    """The picture's change at each sound hit (30 fps), against the median change of the whole video."""
    tl = ep_dir / "work/timeline.json"
    if not tl.exists():
        return {}
    from .series_audio import parse_meme
    from . import sfx_palette as P
    hits = []
    for at, kind, _ in json.loads(tl.read_text())["sound_events"]:
        m = parse_meme(kind)
        if m and m[0] not in ("music_cut", "music_stop") and m[0] in P.PALETTE:
            spec = P.PALETTE[m[0]]
            hits.append((round(at + (spec["attack"]), 2), m[0]))
    if not hits:
        return {}
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(ep_dir / "final.mp4"), "-vf", "scale=135:240",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    g = np.frombuffer(raw, np.uint8).reshape(-1, 240, 135).astype(np.float32)
    a, b = int(CAPTION[0] * 240), int(CAPTION[1] * 240)
    g[:, a:b] = 0
    diff = np.abs(np.diff(g, axis=0)).mean(axis=(1, 2))
    base = float(np.median(diff)) + 0.5
    out = {}
    for t, sid in hits:
        i = int(t * 30)
        win = diff[max(0, i - 3):i + 4]
        out[f"{sid}@{t}"] = round(float(win.max()) / base, 1) if len(win) else None
    return out


def repeat(a: list, b: list) -> float:
    """Share of a's frames whose layout matches some frame of b."""
    B = np.stack(b)
    return round(float(np.mean([np.abs(B - s).mean(axis=(1, 2, 3)).min() < NEW_COMP for s in a])), 2)


def contact_sheet(video: Path, out: Path, n: int = 12, title: str = "") -> Path:
    """n frames evenly across the timeline at phone size, timestamped, captions included (as a viewer sees it)."""
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(video)], capture_output=True, text=True).stdout)
    tw, th, pad, head = 216, 384, 10, 44 if title else 0
    cols = 6
    rows = (n + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (tw + pad) + pad, rows * (th + 30 + pad) + pad + head), (18, 18, 22))
    d = ImageDraw.Draw(sheet)
    from .core import FONTS
    font = ImageFont.truetype(str(FONTS / "Inter-Bold.ttf"), 20) if (FONTS / "Inter-Bold.ttf").exists() else None
    if title:
        d.text((pad, 10), title, fill=(240, 240, 240), font=ImageFont.truetype(str(FONTS / "Inter-Black.ttf"), 26)
               if (FONTS / "Inter-Black.ttf").exists() else None)
    for i in range(n):
        t = dur * (i + 0.5) / n
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                              "-vf", f"scale={tw}:{th}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, check=True).stdout
        im = Image.frombytes("RGB", (tw, th), raw)
        x, y = pad + (i % cols) * (tw + pad), head + pad + (i // cols) * (th + 30 + pad)
        sheet.paste(im, (x, y))
        d.text((x, y + th + 4), f"{t:.1f}s", fill=(200, 200, 200), font=font)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=85)
    return out
