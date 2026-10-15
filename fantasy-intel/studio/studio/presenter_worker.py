"""MuseTalk 1.5 worker for the presenter layer. Runs in the MuseTalk environment, never imported by the studio.

    <musetalk>/.venv/bin/python presenter_worker.py landmarks --image host.png --out host_landmarks.json
    <musetalk>/.venv/bin/python presenter_worker.py lipsync --upstream <musetalk>/upstream --frames DIR \\
        --audio clip.wav --out clip.mp4 --report clip.json [--fps 25]

`lipsync` runs MuseTalk's own inference script (MIT code and weights: musetalkV15 UNet, sd-vae-ft-mse, whisper-tiny,
BiSeNet face parsing; Mediapipe face mesh for landmarks) on a directory of base-clip frames, driven by our Kokoro
audio. MuseTalk swallows its own exceptions, so success is judged by the output file, not the exit code. The
report records the time taken and proves the Wav2Lip-derived module MuseTalk ships for training
(musetalk/data/audio.py) was never loaded: Wav2Lip is banned for this channel.
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
import tempfile
import time
from pathlib import Path

BANNED_MODULES = ("musetalk.data.audio", "musetalk.data.dataset")
EYES = {"left": {"upper": 159, "lower": 145, "outer": 33, "inner": 133},
        "right": {"upper": 386, "lower": 374, "outer": 263, "inner": 362}}
MOUTH = {"upper": 13, "lower": 14, "left": 61, "right": 291}


def landmarks(image: Path, out: Path) -> None:
    import cv2
    import mediapipe as mp
    img = cv2.imread(str(image))
    h, w = img.shape[:2]
    with mp.solutions.face_mesh.FaceMesh(static_image_mode=True, max_num_faces=2, refine_landmarks=True) as fm:
        res = fm.process(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    faces = res.multi_face_landmarks or []
    if len(faces) != 1:
        raise SystemExit(f"expected exactly one face, found {len(faces)}")
    pts = [(p.x * w, p.y * h) for p in faces[0].landmark]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    data = {"size": [w, h], "face_box": [min(xs), min(ys), max(xs), max(ys)],
            "eyes": {k: {n: pts[i] for n, i in v.items()} for k, v in EYES.items()},
            "mouth": {n: pts[i] for n, i in MOUTH.items()}}
    out.write_text(json.dumps(data, indent=1))


def mouth(video: Path, out: Path) -> None:
    """Per-frame inner-lip gap (landmarks 13/14) over face height, from Mediapipe's face mesh: how open the
    mouth really is in each generated frame, for the presenter QC."""
    import cv2
    import mediapipe as mp
    cap = cv2.VideoCapture(str(video))
    series, missing = [], 0
    with mp.solutions.face_mesh.FaceMesh(static_image_mode=False, max_num_faces=1, refine_landmarks=True) as fm:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            res = fm.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            if not res.multi_face_landmarks:
                series.append(None)
                missing += 1
                continue
            p = res.multi_face_landmarks[0].landmark
            face_h = abs(p[152].y - p[10].y) or 1.0
            series.append(round(abs(p[14].y - p[13].y) / face_h, 5))
    out.write_text(json.dumps({"open": series, "frames_without_face": missing}))


def lipsync(a) -> None:
    t0 = time.time()
    upstream = Path(a.upstream).resolve()
    out = Path(a.out).resolve()
    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    os.environ.setdefault("GLOG_minloglevel", "2")
    cfg = Path(tempfile.mkdtemp()) / "task.yaml"
    cfg.write_text(f'task_0:\n  video_path: "{Path(a.frames).resolve()}"\n  audio_path: "{Path(a.audio).resolve()}"\n'
                   f'  result_name: "{out.name}"\n')
    os.chdir(upstream)
    sys.path.insert(0, str(upstream))
    sys.argv = ["inference", "--inference_config", str(cfg), "--result_dir", str(work),
                "--unet_model_path", "./models/musetalkV15/unet.pth",
                "--unet_config", "./models/musetalkV15/musetalk.json", "--whisper_dir", "./models/whisper",
                "--version", "v15", "--batch_size", str(a.batch_size), "--fps", str(a.fps),
                "--use_saved_coord", "--saved_coord"]
    runpy.run_module("scripts.inference", run_name="__main__")
    produced = work / "v15" / out.name
    loaded = [m for m in BANNED_MODULES if m in sys.modules]
    ok = produced.exists() and produced.stat().st_size > 10_000 and not loaded
    if ok:
        produced.replace(out)
    Path(a.report).write_text(json.dumps({
        "ok": ok, "seconds": round(time.time() - t0, 1), "output": str(out) if ok else None,
        "banned_modules_loaded": loaded, "engine": "MuseTalk 1.5 (musetalk-mac, PyTorch MPS)",
        "device": "mps"}, indent=1))
    if not ok:
        raise SystemExit("MuseTalk produced no usable output" + (f"; banned modules loaded: {loaded}" if loaded else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    mo = sub.add_parser("mouth")
    mo.add_argument("--video", type=Path, required=True)
    mo.add_argument("--out", type=Path, required=True)
    lm = sub.add_parser("landmarks")
    lm.add_argument("--image", type=Path, required=True)
    lm.add_argument("--out", type=Path, required=True)
    ls = sub.add_parser("lipsync")
    for k in ("upstream", "frames", "audio", "out", "report", "work"):
        ls.add_argument(f"--{k}", required=True)
    ls.add_argument("--fps", type=int, default=25)
    ls.add_argument("--batch_size", type=int, default=8)
    a = ap.parse_args()
    if a.cmd == "landmarks":
        landmarks(a.image, a.out)
    elif a.cmd == "mouth":
        mouth(a.video, a.out)
    else:
        lipsync(a)


if __name__ == "__main__":
    main()
