"""Optional overnight clips for the AI parody show: local Wan 2.2 (Apache-2.0) animates a Short's hook and punchline
stills. Off unless asked for: a Short is complete without it, and a clip that fails simply leaves the still in place.

    uv run parody-wan --week 8                      # every parody Short that week, one clip at a time
    uv run make-episode --week 8 --parody --hero    # swap finished clips in

Two halves, because Wan lives in its own environment and the studio never imports it. `main` runs in the studio and
queues the work; `--denoise` and `--decode` run in the Wan environment, each clip in fresh processes (on 48 GB the
models don't fit beside the video decoder). Latents are saved between the two, so an interrupted night resumes.

Measured on this Mac (2026-10-31): 480x832, 73 frames (3 s), 20 steps, about 9 min a clip with nothing else running
and 15.5 min under memory pressure. Close heavy apps first.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

NEGATIVE = ("people, person, face, hands, text, letters, numbers, logo, watermark, blurry, low quality, distorted, "
            "morphing, flicker")
MOTION = " Slow cinematic push-in, gentle natural movement, comedic stillness."


def denoise(job: dict) -> None:
    import torch
    from diffusers import AutoencoderKLWan, WanImageToVideoPipeline
    from PIL import Image
    t0 = time.time()
    w, h = job["size"]
    img = Image.open(job["still"]).convert("RGB")
    k = max(w / img.width, h / img.height)
    img = img.resize((round(img.width * k), round(img.height * k)), Image.LANCZOS)
    x, y = (img.width - w) // 2, (img.height - h) // 2
    img = img.crop((x, y, x + w, y + h))
    vae = AutoencoderKLWan.from_pretrained(job["model_path"], subfolder="vae", torch_dtype=torch.float32)
    pipe = WanImageToVideoPipeline.from_pretrained(job["model_path"], vae=vae, torch_dtype=torch.bfloat16)
    pipe.enable_model_cpu_offload(device="mps")
    out = pipe(image=img, prompt=job["prompt"], negative_prompt=NEGATIVE, height=h, width=w, num_frames=job["frames"],
               guidance_scale=5.0, num_inference_steps=job["steps"], output_type="latent",
               generator=torch.Generator("cpu").manual_seed(job["seed"])).frames
    torch.save(out.cpu(), job["latents"])
    Path(job["sidecar"]).write_text(json.dumps({
        "model": job["model"], "license": job["license"], "prompt": job["prompt"], "negative": NEGATIVE,
        "seed": job["seed"], "frames": job["frames"], "steps": job["steps"], "size": job["size"],
        "still_sha256": job["still_sha256"], "denoise_seconds": round(time.time() - t0)}, indent=1))


def decode(job: dict) -> None:
    import torch
    from diffusers import AutoencoderKLWan
    from diffusers.utils import export_to_video
    from diffusers.video_processor import VideoProcessor
    t0 = time.time()
    vae = AutoencoderKLWan.from_pretrained(job["model_path"], subfolder="vae", torch_dtype=torch.float32).to("mps")
    vae.enable_tiling()
    forward = vae.decoder.forward

    def to_cpu(*args, **kwargs):                  # each decoded tile leaves the GPU at once, or 48 GB runs out
        out = forward(*args, **kwargs).to("cpu")
        torch.mps.empty_cache()
        return out

    vae.decoder.forward = to_cpu
    lat = torch.load(job["latents"]).to("mps", torch.float32)
    mean = torch.tensor(vae.config.latents_mean).view(1, vae.config.z_dim, 1, 1, 1).to(lat.device, lat.dtype)
    std = 1.0 / torch.tensor(vae.config.latents_std).view(1, vae.config.z_dim, 1, 1, 1).to(lat.device, lat.dtype)
    with torch.no_grad():
        video = vae.decode(lat / std + mean, return_dict=False)[0]
    frames = VideoProcessor(vae_scale_factor=16).postprocess_video(video, output_type="pil")[0]
    export_to_video(frames, job["out"], fps=24)
    side = Path(job["sidecar"])
    side.write_text(json.dumps({**json.loads(side.read_text()), "decode_seconds": round(time.time() - t0)}, indent=1))
    os.unlink(job["latents"])


# ---------------------------------------------------------------------------------- the studio's half

def hero_keys(script: dict, count: int) -> list[str]:
    """The pictures worth a clip: the hook's and the punchline's, each once."""
    order = [script["shots"][0]["art"], script["shots"][-1]["art"]]
    return list(dict.fromkeys(order))[:count]


def jobs(cfg, season: int, week: int) -> list[dict]:
    from .core import read_json, sha256_file
    from .stages import parody as P
    c = cfg["parody"]
    out = []
    for d in sorted(cfg.episodes_dir.glob(f"{season}-w{week:02d}-short-parody-*")):
        if not (d / "script.json").exists():
            continue
        art = read_json(d / "art/manifest.json")
        for key in hero_keys(read_json(d / "script.json"), c["hero_clips"]):
            clip = d / "clips" / f"{key}.mp4"
            if clip.exists():
                continue
            out.append({"episode": d.name, "key": key, "still": str(d / f"art/{key}.png"),
                        "still_sha256": sha256_file(d / f"art/{key}.png"), "prompt": P.prompt(key) + MOTION,
                        "seed": art[key]["seed"], "size": c["wan_size"], "frames": c["wan_frames"],
                        "steps": c["wan_steps"], "model": c["wan_repo"], "license": c["wan_license"],
                        "model_path": os.path.expanduser(c["wan_model"]), "out": str(clip),
                        "latents": str(clip.with_suffix(".latents.pt")), "sidecar": str(clip.with_suffix(".json"))})
    return out


def worker(cfg, mode: str, job: dict) -> list[str]:
    return [os.path.expanduser(cfg["parody"]["wan_python"]), str(Path(__file__).resolve()), f"--{mode}", json.dumps(job)]


def run_job(cfg, job: dict, run=subprocess.run) -> bool:
    Path(job["out"]).parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTORCH_ENABLE_MPS_FALLBACK": "1"}
    steps = (["decode"] if Path(job["latents"]).exists() else ["denoise", "decode"])
    for mode in steps:
        if run(worker(cfg, mode, job), env=env).returncode:
            print(f"  {job['episode']} {job['key']}: {mode} failed; the still stays in the Short", file=sys.stderr)
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="parody-wan", description=__doc__.split("\n")[0])
    ap.add_argument("--week", type=int)
    ap.add_argument("--season", type=int)
    ap.add_argument("--denoise", help=argparse.SUPPRESS)
    ap.add_argument("--decode", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.denoise:
        denoise(json.loads(args.denoise))
        return 0
    if args.decode:
        decode(json.loads(args.decode))
        return 0
    from .core import Config
    cfg = Config.load()
    todo = jobs(cfg, args.season or cfg.season, args.week)
    print(f"{len(todo)} clips to make, one at a time")
    failed = 0
    for i, job in enumerate(todo, 1):
        t0 = time.time()
        ok = run_job(cfg, job)
        failed += not ok
        print(f"  {i}/{len(todo)} {job['episode']} {job['key']}: {'done' if ok else 'FAILED'} in {time.time() - t0:.0f}s")
    print("Now run make-episode --parody --hero, look at review_sheet.png, then parody-review.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
