"""Visual QA for styled renders: measured from the draw log and the captured frames, not judged by eye.

Gates (each fails the styled video's QC):
  vqa_safe_zone       every text box inside the mobile safe zone (platform UI covers the rest)
  vqa_text_size       cap height at least 20 px for labels, 30 px for numbers and captions (a 1080-wide video is
                      shown about 2.7x smaller on a phone, so 20 px caps are about 7 px on glass)
  vqa_contrast        WCAG contrast of text against what is under it: 4.5:1 below 40 px caps, 3:1 above
  vqa_overlap         no two texts in a settled frame overlap
  vqa_caption_clear   no scene text reaches into the caption zone (it would be hidden behind the captions)
  vqa_numbers         every drawn number has a source, and a claim-sourced number shows that claim's display
  vqa_number_dwell    every claim number stays fully visible for at least 0.6 s
  vqa_pixel_grid      pixel art only: every 5x5 block of a captured frame is one colour (no smoothing)
Subjective review (does it look designed, is it interesting) is written up separately, never automated.
"""

from __future__ import annotations

import re

import numpy as np
from PIL import Image

from .kit import SAFE

MIN_CAP = {"number": 30, "caption": 30, "title": 30}
MIN_CAP_DEFAULT = 20
DWELL_MIN_S = 0.6
SLACK = 4


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("−", "-").replace("–", "-").upper()).strip()


def _overlap(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    return w * h / max(1, min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])))


def gates(vqa: dict, claims: dict, frames_dir) -> list[tuple[str, list[str], dict]]:
    out = []
    samples = vqa["samples"]
    settled = [s for s in samples if s["frame"].endswith("_97.png")]
    l, t, r, b = SAFE
    fail = []
    for s in samples:
        for e in s["log"]:
            x0, y0, x1, y1 = e["box"]
            if x0 < l - SLACK or y0 < t - SLACK or x1 > r + SLACK or y1 > b + SLACK:
                fail.append(f"{s['frame']}: {e['text'][:30]!r} at {e['box']}")
    out.append(("vqa_safe_zone", fail, {"safe": SAFE, "frames": len(samples)}))

    fail = []
    for s in samples:
        for e in s["log"]:
            need = MIN_CAP.get(e["role"], MIN_CAP_DEFAULT)
            if e["role"] != "source" and e["cap_px"] < need:
                fail.append(f"{s['frame']}: {e['text'][:30]!r} cap {e['cap_px']} px < {need}")
    out.append(("vqa_text_size", sorted(set(fail)), {"min_cap_px": MIN_CAP, "default": MIN_CAP_DEFAULT}))

    fail, worst = [], 99.0
    for s in samples:
        for e in s["log"]:
            if "contrast" not in e or e["role"] == "source":
                continue
            need = 3.0 if e["cap_px"] >= 40 else 4.5
            worst = min(worst, e["contrast"])
            if e["contrast"] < need:
                fail.append(f"{s['frame']}: {e['text'][:30]!r} contrast {e['contrast']} < {need}")
    out.append(("vqa_contrast", sorted(set(fail)), {"worst": worst}))

    fail = []
    for s in settled:
        es = [e for e in s["log"] if e["alpha"] > 0.95]
        for i in range(len(es)):
            for j in range(i + 1, len(es)):
                if _overlap(es[i]["box"], es[j]["box"]) > 0.12:
                    fail.append(f"{s['frame']}: {es[i]['text'][:24]!r} overlaps {es[j]['text'][:24]!r}")
    out.append(("vqa_overlap", fail, {"frames": len(settled)}))

    top = vqa.get("caption_top")
    fail = []
    if top:
        for s in samples:
            for e in s["log"]:
                if e["role"] != "caption" and e["alpha"] > 0.5 and e["box"][3] > top + SLACK:
                    fail.append(f"{s['frame']}: {e['text'][:30]!r} reaches y={e['box'][3]} (captions from {top})")
    out.append(("vqa_caption_clear", sorted(set(fail)), {"caption_top": top}))

    fail, n = [], 0
    for s in samples:
        for e in s["log"]:
            if e["role"] == "caption" or not re.search(r"\d", e["text"]):
                continue
            n += 1
            src = e["src"]
            if not src:
                fail.append(f"{s['frame']}: {e['text']!r} has no source")
            elif src in claims and _norm(claims[src]["display"]) not in _norm(e["text"]):
                fail.append(f"{s['frame']}: {e['text']!r} does not show claim {src} ({claims[src]['display']!r})")
    out.append(("vqa_numbers", sorted(set(fail)), {"numbers_checked": n}))

    fail = [f"{src} {v['text']!r} readable for {v['seconds']} s" for src, v in vqa["numbers_visible"].items()
            if src in claims and v["seconds"] < DWELL_MIN_S]
    out.append(("vqa_number_dwell", fail, {"min_seconds": DWELL_MIN_S, "numbers": len(vqa["numbers_visible"])}))

    if vqa["style"] == "pixel":
        fail = []
        for s in samples:
            a = np.asarray(Image.open(frames_dir / s["frame"]).convert("RGB")).astype(np.int16)
            h, w = (a.shape[0] // 5) * 5, (a.shape[1] // 5) * 5
            blocks = a[:h, :w].reshape(h // 5, 5, w // 5, 5, 3)
            uniform = (blocks.max(axis=(1, 3)) - blocks.min(axis=(1, 3))).max(axis=-1) == 0
            share = float(uniform.mean())
            if share < 0.999:
                fail.append(f"{s['frame']}: {100 * (1 - share):.2f}% of 5x5 blocks are not one colour")
        out.append(("vqa_pixel_grid", fail, {"block": 5}))
    return out
