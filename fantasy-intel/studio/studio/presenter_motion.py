"""Natural-motion library for the AI host: short idle clips of the SAME portrait, generated once.

Runs in its own environment (diffusers + Wan2.2-TI2V-5B, Apache-2.0; see docs/presenter/models.md), never
imported by the studio:

    ~/models/wan/.venv/bin/python studio/presenter_motion.py --portrait assets/presenter/host.png \\
        --model ~/models/wan/Wan2.2-TI2V-5B-Diffusers --out ~/models/presenter/motion/library --clips 3

Each clip starts from the canonical portrait (image-to-video), so identity stays anchored to it. The prompts
differ slightly (a listening hold, a slight nod, a small head turn) and the seeds differ, so the presenter
layer can draw varied but consistent behaviour from the library instead of one loop. The mouth stays closed:
MuseTalk drives the mouth afterwards. Slow on Apple Silicon (about 21 s per denoising step at 720p), which is
fine for clips generated once and reused for every video.

Memory: on 48 GB the transformer, text encoder and the video decoder don't fit together, so every clip is
denoised to latents first (saved to disk), then the big models are freed and the VAE decodes tile by tile.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

# Pass 1 prompts ("He blinks naturally") gave long, sleepy eye closures and drifting gaze in 2 of 3 clips (see the
# eye audit in presenter.py). Pass 2 asks for eye contact throughout and one quick blink. Each clip's JSON
# sidecar records the exact prompt it was made with.
BASE = ("A man in a dark crewneck sweater in a TV sports studio looks straight into the camera lens the whole "
        "time, eyes open and focused on the viewer, with a calm, attentive, friendly expression, his mouth closed. "
        "{action} He blinks once, quickly. He breathes gently so his shoulders move a little. Static camera, "
        "realistic, natural soft studio lighting, sharp focus on the face.")
ACTIONS = [
    "He makes very small, natural head movements while keeping eye contact.",
    "He gives one slight, slow nod while keeping eye contact.",
    "He tilts his head very slightly to one side and back while keeping eye contact.",
]
NEGATIVE = ("talking, mouth opening, speaking, teeth, closed eyes, eyes closing, sleepy, drowsy, squinting, looking "
            "away, looking to the side, averted gaze, exaggerated motion, fast motion, camera movement, zoom, pan, "
            "distorted face, morphing, identity change, blurry, low quality, hands, fingers")
SIZE = (704, 1280)                         # Wan2.2-TI2V-5B's portrait 720p


def portrait_frame(path: Path):
    from PIL import Image
    img = Image.open(path).convert("RGB")
    w, h = SIZE
    k = h / img.height
    img = img.resize((round(img.width * k), h), Image.LANCZOS)
    x0 = (img.width - w) // 2
    return img.crop((x0, 0, x0 + w, h))


def decode_latents(pipe, latents):
    import torch
    vae = pipe.vae
    vae.to("mps")
    lat = latents.to("mps", vae.dtype)
    mean = torch.tensor(vae.config.latents_mean).view(1, vae.config.z_dim, 1, 1, 1).to(lat.device, lat.dtype)
    std = 1.0 / torch.tensor(vae.config.latents_std).view(1, vae.config.z_dim, 1, 1, 1).to(lat.device, lat.dtype)
    with torch.no_grad():
        video = vae.decode(lat / std + mean, return_dict=False)[0]
    return pipe.video_processor.postprocess_video(video, output_type="pil")[0]


def decode_file(model: Path, lat_path: Path) -> None:
    """Decode one clip's saved latents in a process that holds only the VAE."""
    import torch
    from diffusers import AutoencoderKLWan
    from diffusers.utils import export_to_video
    from diffusers.video_processor import VideoProcessor
    t0 = time.time()
    vae = AutoencoderKLWan.from_pretrained(model, subfolder="vae", torch_dtype=torch.float32).to("mps")
    vae.enable_tiling()
    # The tiled decoder keeps every decoded tile on the GPU until the end; at 81 frames x 720p that sits at
    # the edge of 48 GB and fails intermittently. Move each decoded piece to the CPU and release the cache.
    decoder_forward = vae.decoder.forward

    def to_cpu(*args, **kwargs):
        out = decoder_forward(*args, **kwargs).to("cpu")
        torch.mps.empty_cache()
        return out

    vae.decoder.forward = to_cpu
    lat = torch.load(lat_path).to("mps", torch.float32)
    mean = torch.tensor(vae.config.latents_mean).view(1, vae.config.z_dim, 1, 1, 1).to(lat.device, lat.dtype)
    std = 1.0 / torch.tensor(vae.config.latents_std).view(1, vae.config.z_dim, 1, 1, 1).to(lat.device, lat.dtype)
    with torch.no_grad():
        video = vae.decode(lat / std + mean, return_dict=False)[0]
    out = Path(str(lat_path).replace(".latents.pt", ".mp4"))
    export_to_video(VideoProcessor(vae_scale_factor=16).postprocess_video(video, output_type="pil")[0], str(out), fps=24)
    print(f"wrote {out} in {time.time() - t0:.0f}s", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--portrait", type=Path, required=True)
    ap.add_argument("--model", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--clips", type=int, default=3)
    ap.add_argument("--first", type=int, default=1, help="number of the first clip (idle_NN); keeps older clips")
    ap.add_argument("--frames", type=int, default=81)          # 4k+1 frames; 81 at 24 fps is about 3.4 s
    ap.add_argument("--steps", type=int, default=30)
    ap.add_argument("--latents-only", action="store_true",
                    help="stop after saving latents; decode each in a fresh process with --decode (more reliable "
                         "on 48 GB: the offloaded models otherwise keep holding memory)")
    ap.add_argument("--decode", type=Path, help="decode one saved .latents.pt to its .mp4 (VAE only in memory)")
    a = ap.parse_args()
    if a.decode:
        decode_file(a.model, a.decode)
        return
    import torch
    from diffusers import AutoencoderKLWan, WanImageToVideoPipeline
    from diffusers.utils import export_to_video
    a.out.mkdir(parents=True, exist_ok=True)
    vae = AutoencoderKLWan.from_pretrained(a.model, subfolder="vae", torch_dtype=torch.float32)
    pipe = WanImageToVideoPipeline.from_pretrained(a.model, vae=vae, torch_dtype=torch.bfloat16)
    pipe.enable_model_cpu_offload(device="mps")
    pipe.vae.enable_tiling()
    import gc
    image = portrait_frame(a.portrait)
    todo = []
    for i in range(a.first - 1, a.first - 1 + a.clips):   # 1. denoise every missing clip to latents (saved)
        name = a.out / f"idle_{i + 1:02d}.mp4"
        lat_path = name.with_suffix(".latents.pt")
        if name.exists():
            continue
        if not lat_path.exists():
            t0 = time.time()
            prompt = BASE.format(action=ACTIONS[i % len(ACTIONS)])
            lat = pipe(image=image, prompt=prompt, negative_prompt=NEGATIVE, height=SIZE[1], width=SIZE[0],
                       num_frames=a.frames, guidance_scale=5.0, num_inference_steps=a.steps, output_type="latent",
                       generator=torch.Generator("cpu").manual_seed(100 + i)).frames
            torch.save(lat.cpu(), lat_path)
            name.with_suffix(".json").write_text(json.dumps({
                "model": "Wan-AI/Wan2.2-TI2V-5B-Diffusers", "license": "Apache-2.0", "portrait": str(a.portrait),
                "prompt": prompt, "negative": NEGATIVE, "seed": 100 + i, "frames": a.frames, "steps": a.steps,
                "denoise_seconds": round(time.time() - t0)}, indent=1))
            print(f"latents {lat_path.name} in {time.time() - t0:.0f}s", flush=True)
        todo.append((name, lat_path))
    if a.latents_only:
        return
    pipe.transformer = None                        # 2. free the big models before decoding
    pipe.text_encoder = None
    gc.collect()
    torch.mps.empty_cache()
    for name, lat_path in todo:                    # 3. decode tile by tile
        t0 = time.time()
        export_to_video(decode_latents(pipe, torch.load(lat_path)), str(name), fps=24)
        print(f"wrote {name} in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
