"""QC for the AI parody show, on top of every gate a series Short already passes.

  ai_label      "AI PARODY" read back from sampled frames of the final video, frame 0 included
  no_likeness   only generated shots; every prompt clean; no face or body found in any picture or clip
  clip_text     no text or numbers in generated footage: on-screen numbers come only from claims
  clip_review   a person approved every picture and clip, by hash (`parody-review`)
  provenance    model, license, prompt, seed and hash on record for each one, all from the allowed local models
  ai_metadata   the flag, the disclosure line and "AI Parody" in the title

A gate that cannot look (no Swift, no Vision) fails: not being able to check is not a pass.
"""

from __future__ import annotations

import argparse
import getpass
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from . import motion as M
from . import vision as V
from .core import Config, Episode, read_json, sha256_file, utcnow, write_json
from .parody_motion import BADGE_BOX, BADGE_TEXT
from .stages.parody import check_prompt
from .stages.qc import ALLOWED_MODELS, Gate, _gate
from .stages.showrunner import FORMATS

MIN_CONFIDENCE = 0.5
NOTE = FORMATS["ai_parody"]["note"]
FAILS = (OSError, subprocess.SubprocessError, RuntimeError, ValueError)


def stills(ep: Episode, script: dict) -> dict[str, Path]:
    return {k: ep.path(f"art/{k}.png") for k in sorted({s["art"] for s in script["shots"]})}


def clips(ep: Episode, script: dict) -> dict[str, Path]:
    return {s["art"]: ep.path(s["clip"]) for s in script["shots"] if s.get("clip")}


def _samples(video: Path, out: Path) -> list[Path]:
    """One frame a second, the first frame included."""
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-vf", "fps=1",
                    str(out / "f%04d.png")], check=True)
    return sorted(out.glob("*.png"))


def _pictures(ep: Episode, script: dict) -> list[Path]:
    """Every generated picture, and sampled frames of every clip."""
    out = list(stills(ep, script).values())
    for key, clip in clips(ep, script).items():
        out += _samples(clip, ep.path(f"work/parody_frames/{key}"))
    return out


def badge_crop(frame: Path) -> Path:
    """The badge's corner of a frame, enlarged. OCR on a whole busy frame can miss it, and this proves where it sits."""
    x0, y0, x1, y1 = BADGE_BOX
    out = frame.with_name(frame.stem + ".badge.png")
    with Image.open(frame) as im:
        k = im.width / M.W
        box = tuple(round(v * k) for v in (x0 - 40, y0 - 40, x1 + 40, y1 + 40))
        im.crop(box).resize((2 * (x1 - x0 + 80), 2 * (y1 - y0 + 80)), Image.LANCZOS).save(out)
    return out


def gate_ai_label(ep: Episode, script: dict, facts: dict) -> Gate:
    try:
        frames = [badge_crop(p) for p in _samples(ep.path("final.mp4"), ep.path("work/label_frames"))]
        read = V.read_text(frames)
    except FAILS as exc:
        return _gate("ai_label", [f"could not read the frames: {exc}"])
    badge = BADGE_TEXT.replace(" ", "")          # OCR may close up the gap between the words
    missing = [p.name for p in frames if not any(badge in t.upper().replace(" ", "") for t in read.get(str(p), []))]
    if not frames:
        missing = ["no frames"]
    return _gate("ai_label", [f"{BADGE_TEXT} not found in {m}" for m in missing], frames=len(frames))


def gate_no_likeness(ep: Episode, script: dict, facts: dict) -> Gate:
    failures = [f"{s.get('id', s['type'])} is not generated art" for s in script["shots"]
                if not s["type"].startswith("ai_")]
    names = [p["name"] for p in facts["players"]]
    manifest = ep.path("art/manifest.json")
    entries = read_json(manifest) if manifest.exists() else {}
    for key, e in entries.items():
        bad = check_prompt(e["prompt"], names)
        if bad:
            failures.append(f"{key}: prompt mentions {bad}")
    checked = 0
    try:
        for path in _pictures(ep, script):
            found = V.people(path)
            checked += 1
            for kind, word in (("faces", "face"), ("humans", "person")):
                if any(b[4] >= MIN_CONFIDENCE for b in found.get(kind, [])):
                    failures.append(f"{path.name}: a {word} was found")
    except FAILS as exc:
        failures.append(f"could not check the pictures: {exc}")
    return _gate("no_likeness", failures, pictures_checked=checked, prompts_checked=len(entries))


def is_text(line: str) -> bool:
    """Real writing, not marks: a football's laces and a vent's slats both read as strokes like '1111/'."""
    if re.fullmatch(r"[1lIi|/\\_=. -]+", line) or re.fullmatch(r"([A-Za-z])\1{2,}", line):
        return False
    return len(re.findall(r"[A-Za-z0-9]", line)) >= 2 or bool(re.search(r"\d", line))


def gate_clip_text(ep: Episode, script: dict, facts: dict) -> Gate:
    try:
        pictures = _pictures(ep, script)
        read = V.read_text(pictures)
    except FAILS as exc:
        return _gate("clip_text", [f"could not read the pictures: {exc}"])
    failures = [f"{Path(p).name}: {t!r}" for p, lines in read.items() for t in lines if is_text(t)]
    return _gate("clip_text", failures, pictures_checked=len(pictures))


def _review(ep: Episode) -> dict:
    path = ep.path("clip_review.json")
    return read_json(path) if path.exists() else {"approved": {}, "takes": {}}


def _assets(ep: Episode, script: dict) -> dict[str, Path]:
    return {**stills(ep, script), **{f"clip:{k}": v for k, v in clips(ep, script).items()}}


def gate_clip_review(ep: Episode, script: dict, facts: dict) -> Gate:
    approved = _review(ep)["approved"]
    failures = []
    for key, path in _assets(ep, script).items():
        rec = approved.get(key)
        if not rec:
            failures.append(f"{key}: not approved; look at review_sheet.png, then run parody-review")
        elif not path.exists() or rec["sha256"] != sha256_file(path):
            failures.append(f"{key}: changed after it was approved")
    return _gate("clip_review", failures, approved=len(approved))


def gate_provenance(ep: Episode, script: dict, facts: dict) -> Gate:
    manifest = ep.path("art/manifest.json")
    entries = read_json(manifest) if manifest.exists() else {}
    records = dict(entries)
    failures = []
    for key, clip in clips(ep, script).items():
        side = clip.with_suffix(".json")
        records[f"clip:{key}"] = read_json(side) if side.exists() else {}
        still = ep.path(f"art/{key}.png")
        if not still.exists() or records[f"clip:{key}"].get("still_sha256") != sha256_file(still):
            failures.append(f"clip:{key}: its still changed after the clip was made")
    for key in stills(ep, script):
        if key not in entries:
            failures.append(f"{key}: no provenance record")
    for key, e in records.items():
        for field in ("model", "license", "prompt", "seed"):
            if field not in e:
                failures.append(f"{key}: no {field} on record")
        if ALLOWED_MODELS.get(e.get("model")) != e.get("license"):
            failures.append(f"{key}: {e.get('model')!r} ({e.get('license')}) is not an allowed model")
    return _gate("ai_provenance", failures, records=len(records))


def gate_ai_metadata(meta: dict) -> Gate:
    failures = []
    if meta.get("ai_parody") is not True:
        failures.append("metadata does not flag ai_parody")
    if NOTE not in meta["description"]:
        failures.append("the description lacks the AI parody disclosure")
    if "AI Parody" not in meta["title"]:
        failures.append("the title does not say AI Parody")
    return _gate("ai_metadata", failures)


def gates(ep: Episode, script: dict, facts: dict) -> list[Gate]:
    return [gate_ai_label(ep, script, facts), gate_no_likeness(ep, script, facts), gate_clip_text(ep, script, facts),
            gate_clip_review(ep, script, facts), gate_provenance(ep, script, facts),
            gate_ai_metadata(read_json(ep.path("metadata.json")))]


# ------------------------------------------------------------------------------------------------- review

def sheet(ep: Episode) -> Path:
    """review_sheet.png: every picture and a middle frame of every clip, labelled, to judge in one look."""
    script = read_json(ep.path("script.json"))
    items = [(k, p) for k, p in stills(ep, script).items()]
    for key, clip in clips(ep, script).items():
        frames = _samples(clip, ep.path(f"work/parody_frames/{key}"))
        items += [(f"clip:{key} {i + 1}", f) for i, f in enumerate(frames)]
    tw, th, pad = 270, 480, 14
    out = Image.new("RGB", (pad + len(items) * (tw + pad), th + 2 * pad + 36), (16, 16, 20))
    d = ImageDraw.Draw(out)
    for i, (label, path) in enumerate(items):
        x = pad + i * (tw + pad)
        with Image.open(path) as im:
            out.paste(im.convert("RGB").resize((tw, th)), (x, pad))
        d.text((x, th + pad + 6), label, font=M.font("bold", 22), fill=(235, 235, 240))
    path = ep.path("review_sheet.png")
    out.save(path)
    return path


def approve(ep: Episode, by: str) -> list[str]:
    script = read_json(ep.path("script.json"))
    review = _review(ep)
    for key, path in _assets(ep, script).items():
        review["approved"][key] = {"sha256": sha256_file(path), "by": by, "at": utcnow()}
    write_json(ep.path("clip_review.json"), review)
    return sorted(review["approved"])


def reject(ep: Episode, key: str) -> None:
    """Send a picture back: its approval goes, and the frames stage makes it again with a new seed."""
    review = _review(ep)
    review["approved"].pop(key, None)
    review["takes"][key] = review["takes"].get(key, 0) + 1
    write_json(ep.path("clip_review.json"), review)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="parody-review", description="Approve or reject an AI parody Short's pictures")
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--slug", required=True, help="the Short's slug, e.g. parody-bo-nix")
    ap.add_argument("--approve", action="store_true", help="approve every current picture and clip")
    ap.add_argument("--reject", action="append", default=[], metavar="KEY", help="make this picture again")
    ap.add_argument("--season", type=int)
    args = ap.parse_args(argv)
    cfg = Config.load()
    ep = Episode(args.season or cfg.season, args.week, "short", cfg, slug=args.slug)
    for key in args.reject:
        reject(ep, key)
        print(f"rejected {key}; rerun make-episode --parody to make it again")
    if args.approve:
        print(f"approved: {approve(ep, getpass.getuser())}")
    if not args.approve and not args.reject:
        print(f"look at {sheet(ep)}, then rerun with --approve or --reject KEY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
