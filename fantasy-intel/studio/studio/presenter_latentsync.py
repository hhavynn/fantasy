"""EXPERIMENT C: LatentSync 1.6 (512 px) lip sync on Apple Silicon, without InsightFace. Not used in production.

MuseTalk generates the face at 256 px, which can't draw clean upper teeth (see docs/presenter/README.md).
LatentSync 1.6 works at 512 px. Its code is Apache-2.0 and its weights OpenRAIL++, but as shipped its inference
uses InsightFace's non-commercial face models, CUDA and decord. `patch()` makes small, idempotent text patches
to a LatentSync checkout:
  - face detection: Mediapipe face mesh (Apache-2.0) supplies the three alignment points LatentSync takes
    from InsightFace (left eyebrow centre, right eyebrow centre, nose centre); no InsightFace code or weights
  - device: "mps" instead of hard-coded "cuda"
  - IO: no decord (OpenCV for video, librosa for audio); no forced 25 fps resampling, so the clip can run at
    the video's own frame rate

Run in the LatentSync environment:
    ~/models/LatentSync/.venv/bin/python -I studio/presenter_latentsync.py --repo ~/models/LatentSync \\
        --video base.mp4 --audio line.wav --out clip.mp4 [--fps 30 --steps 20 --guidance 1.5]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

FACE_DETECTOR = '''"""Patched for the fantasy-intel presenter: Mediapipe instead of InsightFace (non-commercial weights)."""
import numpy as np
import mediapipe as mp

LEFT_BROW = [70, 63, 105, 66, 107]
RIGHT_BROW = [336, 296, 334, 293, 300]
NOSE = [1, 2, 4, 5]


class FaceDetector:
    def __init__(self, device="mps"):
        self.mesh = mp.solutions.face_mesh.FaceMesh(static_image_mode=False, max_num_faces=1, refine_landmarks=True)

    def __call__(self, frame, threshold=0.5):
        h, w = frame.shape[:2]
        res = self.mesh.process(np.ascontiguousarray(frame))
        if not res.multi_face_landmarks:
            return None, None
        p = np.array([(q.x * w, q.y * h) for q in res.multi_face_landmarks[0].landmark])
        brows = [p[LEFT_BROW].mean(0), p[RIGHT_BROW].mean(0)]
        brows.sort(key=lambda v: v[0])              # template order: image-left point first
        pts3 = np.array([brows[0], brows[1], p[NOSE].mean(0)])
        x1, y1 = p.min(0)
        x2, y2 = p.max(0)
        return (int(x1), int(y1), int(x2), int(y2)), pts3
'''

PATCHES = [
    ("latentsync/utils/image_processor.py",
     '''        pt_left_eye = np.mean(landmark_2d_106[[43, 48, 49, 51, 50]], axis=0)  # left eyebrow center
        pt_right_eye = np.mean(landmark_2d_106[101:106], axis=0)  # right eyebrow center
        pt_nose = np.mean(landmark_2d_106[[74, 77, 83, 86]], axis=0)  # nose center

        landmarks3 = np.round([pt_left_eye, pt_right_eye, pt_nose])''',
     '''        landmarks3 = np.round(landmark_2d_106)  # patched: Mediapipe supplies the three alignment points'''),
    ("latentsync/utils/image_processor.py",
     '''        if device == "cpu":
            self.face_detector = None''',
     '''        if False:  # patched: Mediapipe runs on the CPU
            self.face_detector = None'''),
    ("latentsync/pipelines/lipsync_pipeline.py",
     '''ImageProcessor(height, device="cuda", mask_image=mask_image)''',
     '''ImageProcessor(height, device=str(device), mask_image=mask_image)'''),
    ("latentsync/pipelines/lipsync_pipeline.py",
     '''video_frames = read_video(video_path, use_decord=False)''',
     '''video_frames = read_video(video_path, change_fps=False, use_decord=False)'''),
    ("latentsync/utils/util.py",
     '''from decord import AudioReader, VideoReader''',
     '''try:
    from decord import AudioReader, VideoReader
except ImportError:  # patched: no decord on Apple Silicon
    AudioReader = VideoReader = None'''),
    ("latentsync/utils/util.py",
     '''    ar = AudioReader(audio_path, sample_rate=audio_sample_rate, mono=True)

    # To access the audio samples
    audio_samples = torch.from_numpy(ar[:].asnumpy())
    audio_samples = audio_samples.squeeze(0)''',
     '''    import librosa  # patched: librosa instead of decord
    audio_samples = torch.from_numpy(librosa.load(audio_path, sr=audio_sample_rate, mono=True)[0])'''),
]


def patch(repo: Path) -> list[str]:
    done = []
    fd = repo / "latentsync/utils/face_detector.py"
    if "Mediapipe instead of InsightFace" not in fd.read_text():
        fd.write_text(FACE_DETECTOR)
        done.append(str(fd))
    for rel, old, new in PATCHES:
        f = repo / rel
        s = f.read_text()
        if new in s:
            continue
        if old not in s:
            raise SystemExit(f"LatentSync source changed; patch target not found in {rel}")
        f.write_text(s.replace(old, new))
        done.append(rel)
    return done


def run(a) -> None:
    t0 = time.time()
    repo = Path(a.repo).expanduser().resolve()
    paths = {k: str(Path(getattr(a, k)).resolve()) for k in ("video", "audio", "out")}
    patch(repo)
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    os.chdir(repo)
    sys.path.insert(0, str(repo))
    import torch
    from diffusers import AutoencoderKL, DDIMScheduler
    from omegaconf import OmegaConf
    from latentsync.models.unet import UNet3DConditionModel
    from latentsync.pipelines.lipsync_pipeline import LipsyncPipeline
    from latentsync.whisper.audio2feature import Audio2Feature
    banned = [m for m in sys.modules if m.startswith("insightface")]
    if banned:
        raise SystemExit(f"InsightFace was imported: {banned}")
    config = OmegaConf.load("configs/unet/stage2_512.yaml")
    dtype = torch.float16
    scheduler = DDIMScheduler.from_pretrained("configs")
    audio_encoder = Audio2Feature(model_path="checkpoints/whisper/tiny.pt", device="mps",
                                  num_frames=config.data.num_frames, audio_feat_length=config.data.audio_feat_length)
    vae = AutoencoderKL.from_pretrained(a.vae, torch_dtype=dtype)
    vae.config.scaling_factor, vae.config.shift_factor = 0.18215, 0
    unet, _ = UNet3DConditionModel.from_pretrained(OmegaConf.to_container(config.model),
                                                   "checkpoints/latentsync_unet.pt", device="cpu")
    pipe = LipsyncPipeline(vae=vae, audio_encoder=audio_encoder, unet=unet.to(dtype=dtype), scheduler=scheduler).to("mps")
    torch.manual_seed(1247)
    pipe(video_path=paths["video"], audio_path=paths["audio"], video_out_path=paths["out"],
         num_frames=config.data.num_frames, num_inference_steps=a.steps, guidance_scale=a.guidance,
         weight_dtype=dtype, width=config.data.resolution, height=config.data.resolution,
         mask_image_path=config.data.mask_image_path, temp_dir=str(Path(paths["out"]).parent / "ls_temp"),
         video_fps=a.fps)
    loaded = [m for m in sys.modules if m.startswith("insightface")]
    Path(paths["out"] + ".json").write_text(json.dumps({
        "seconds": round(time.time() - t0, 1), "fps": a.fps, "steps": a.steps, "guidance": a.guidance,
        "engine": "LatentSync 1.6 (patched: Mediapipe, MPS, no decord)", "insightface_loaded": loaded}, indent=1))
    print(f"wrote {paths['out']} in {time.time() - t0:.0f}s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--vae", default=str(Path("~/models/musetalk-mac/upstream/models/sd-vae").expanduser()))
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--guidance", type=float, default=1.5)
    run(ap.parse_args())


if __name__ == "__main__":
    main()
