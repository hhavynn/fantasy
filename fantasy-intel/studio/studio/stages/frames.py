"""The AI parody show's start frames: one still per picture key, generated locally with Z-Image-Turbo through mflux,
plus a subject mask from Apple Vision. Free and on this Mac. art/manifest.json records each picture's model, license,
prompt, seed and hash. A prompt that could aim a picture at a person or a team stops the stage before anything runs.

A picture rejected in review (parody_qc.reject) takes a new seed; every other picture is left as it is.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

from ..core import Episode, read_json, run_stage, sha256_bytes, sha256_file, write_json
from .. import vision as V
from . import parody as P

VERSION = 1


def seed_for(ep: Episode, key: str, take: int) -> int:
    return int(sha256_bytes(f"{ep.id}:{key}:{take}".encode())[:8], 16) % 2**31


def generate(ep: Episode) -> Callable[[str, int, Path], None]:
    """mflux runs from its own install; the studio never imports it."""
    cfg = ep.cfg["parody"]
    width, height = cfg["still_size"]

    def make(text: str, seed: int, out: Path) -> None:
        subprocess.run([os.path.expanduser(cfg["mflux"]), "--prompt", text, "--width", str(width), "--height", str(height),
                        "--steps", str(cfg["still_steps"]), "--seed", str(seed), "--quantize", "8", "--output", str(out)],
                       check=True, capture_output=True)
    return make


def takes(ep: Episode) -> dict[str, int]:
    review = ep.path("clip_review.json")
    return read_json(review).get("takes", {}) if review.exists() else {}


def _run(ep: Episode, make: Callable[[str, int, Path], None]) -> None:
    script = read_json(ep.path("script.json"))
    names = [p["name"] for p in read_json(ep.path("facts.json"))["players"]]
    keys = sorted({s["art"] for s in script["shots"]})
    cfg, art, take = ep.cfg["parody"], ep.path("art"), takes(ep)
    art.mkdir(exist_ok=True)
    path = art / "manifest.json"
    manifest = read_json(path) if path.exists() else {}
    for key in keys:
        text = P.prompt(key)
        bad = P.check_prompt(text, names)
        if bad:
            raise RuntimeError(f"prompt for {key} refused: {bad}")
    for key in keys:
        text, seed = P.prompt(key), seed_for(ep, key, take.get(key, 0))
        out, mask = art / f"{key}.png", art / f"{key}.mask.png"
        e = manifest.get(key)
        if e and e["prompt"] == text and e["seed"] == seed and out.exists():
            continue
        out.unlink(missing_ok=True)            # mflux will not overwrite: it would write <key>_1.png beside the old one
        mask.unlink(missing_ok=True)
        make(text, seed, out)
        if not out.exists():
            raise RuntimeError(f"the generator wrote no picture for {key}")
        V.subject_mask(out, mask)
        manifest[key] = {"model": cfg["model_repo"], "license": cfg["model_license"], "prompt": text, "seed": seed,
                         "steps": cfg["still_steps"], "size": cfg["still_size"], "take": take.get(key, 0),
                         "sha256": sha256_file(out), "subject_mask": mask.exists()}
        ep.log(f"  frames: {key}")
    write_json(path, {k: manifest[k] for k in keys})


def run(ep: Episode, make: Callable[[str, int, Path], None] | None = None) -> None:
    params = {k: ep.cfg["parody"][k] for k in ("model_repo", "still_size", "still_steps")} | {"takes": takes(ep)}
    params["prompts"] = {s["art"]: P.prompt(s["art"]) for s in read_json(ep.path("script.json"))["shots"]}
    run_stage(ep, "frames", VERSION, params, ["script.json"], ["art"], lambda: _run(ep, make or generate(ep)))
