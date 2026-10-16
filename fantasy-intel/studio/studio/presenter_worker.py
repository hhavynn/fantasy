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
    series, missing, pts, eyes, gaze = [], 0, [], [], []
    with mp.solutions.face_mesh.FaceMesh(static_image_mode=False, max_num_faces=1, refine_landmarks=True) as fm:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            res = fm.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            if not res.multi_face_landmarks:
                series.append(None)
                pts.append(None)
                eyes.append(None)
                gaze.append(None)
                missing += 1
                continue
            p = res.multi_face_landmarks[0].landmark
            face_h = abs(p[152].y - p[10].y) or 1.0
            series.append(round(abs(p[14].y - p[13].y) / face_h, 5))
            # eye openness: lid gap over eye width, both eyes averaged (blink and slow-close detection)
            eyes.append(round(sum(abs(p[e["lower"]].y - p[e["upper"]].y) / (abs(p[e["outer"]].x - p[e["inner"]].x) or 1)
                                  for e in EYES.values()) / 2, 4))
            # gaze: iris centre (refined landmarks 468 / 473) across each eye in image x, 0 = the eye's image-left
            # corner, 1 = its image-right corner; both eyes averaged, so a sideways look moves it, a squint doesn't
            def across(iris, e):
                lo, hi = sorted((p[e["inner"]].x, p[e["outer"]].x))
                return (p[iris].x - lo) / ((hi - lo) or 1)
            gaze.append(round((across(468, EYES["left"]) + across(473, EYES["right"])) / 2, 4))
            h, w = frame.shape[:2]
            pts.append([[round(p[i].x * w, 2), round(p[i].y * h, 2)] for i in STABLE_POINTS])
    out.write_text(json.dumps({"open": series, "frames_without_face": missing, "points": pts,
                               "point_ids": STABLE_POINTS, "eyes": eyes,
                               "gaze": gaze}))


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


# eye corners, nose tip, chin, brow peaks, mouth corners: for jitter, motion and identity-geometry checks
STABLE_POINTS = [33, 133, 362, 263, 1, 152, 105, 334, 61, 291, 13, 14]
LIPS_OUTER = [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 409, 270, 269, 267, 0, 37, 39, 40, 185]


def restore_detail(gen, ori, box, mesh, strength: float = 0.9, sigma: float = 1.8):
    """Give the regenerated lower face back the skin detail of the same base frame.

    MuseTalk rebuilds the lower face at 256 px through a VAE and scales it up, which leaves cheeks and chin
    far smoother than the untouched upper face. The base frame's high-frequency detail (pores, stubble) is
    moved onto the generated geometry with optical flow (so it follows the jaw), and added only where the
    generated frame has lost detail; the lips and the mouth interior are excluded, so nothing of the old,
    closed mouth can ghost over the new one. Arrays are BGR uint8; returns BGR uint8."""
    import cv2
    import numpy as np
    x0, y0, x1, y1 = box
    h, w = gen.shape[:2]
    mid = y0 + (y1 - y0) // 3
    rx0, ry0, rx1, ry1 = max(0, x0 - 20), max(0, mid), min(w, x1 + 20), min(h, y1 + 40)
    g = gen[ry0:ry1, rx0:rx1].astype(np.float32)
    o = ori[ry0:ry1, rx0:rx1].astype(np.float32)
    g_low = cv2.GaussianBlur(g, (0, 0), sigma)
    o_low = cv2.GaussianBlur(o, (0, 0), sigma)
    o_high = o - o_low
    # flow from the generated geometry to the base frame, on the smooth images both share
    dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    gg = cv2.cvtColor(np.clip(g_low, 0, 255).astype(np.uint8), cv2.COLOR_BGR2GRAY)
    og = cv2.cvtColor(np.clip(o_low, 0, 255).astype(np.uint8), cv2.COLOR_BGR2GRAY)
    flow = dis.calc(gg, og, None)
    yy, xx = np.mgrid[0:g.shape[0], 0:g.shape[1]].astype(np.float32)
    mx, my = xx + flow[..., 0], yy + flow[..., 1]
    o_high_w = cv2.remap(o_high, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    o_low_w = cv2.remap(o_low, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    # only where detail was lost: local high-pass energy of the generated frame vs the base frame's
    e_gen = cv2.GaussianBlur((g - g_low) ** 2, (0, 0), 6).mean(-1)
    e_ori = cv2.GaussianBlur(o_high_w ** 2, (0, 0), 6).mean(-1) + 1e-3
    deficit = np.clip(1 - e_gen / e_ori, 0, 1)
    # trust the warp only where the smooth images agree after warping
    agree = np.clip(1 - np.abs(g_low - o_low_w).mean(-1) / 18.0, 0, 1)
    weight = deficit * agree
    if mesh is not None:                                  # keep lips and mouth interior as generated
        lips = np.zeros(g.shape[:2], np.uint8)
        pts = np.array([[(mesh[i][0] - rx0), (mesh[i][1] - ry0)] for i in LIPS_OUTER], np.int32)
        cv2.fillPoly(lips, [pts], 255)
        lips = cv2.dilate(lips, np.ones((15, 15), np.uint8))
        weight *= 1 - cv2.GaussianBlur(lips.astype(np.float32) / 255, (0, 0), 6)
    edge = np.zeros(g.shape[:2], np.float32)              # fade out at the region border
    edge[8:-8, 8:-8] = 1
    weight *= cv2.GaussianBlur(edge, (0, 0), 6)
    out = gen.copy()
    out[ry0:ry1, rx0:rx1] = np.clip(g + strength * weight[..., None] * o_high_w, 0, 255).astype(np.uint8)
    return out


def lipsync_hq(a) -> None:
    """MuseTalk 1.5 with our own loop around its model and blending: the output frame rate equals the
    video's (no 25->30 fps resampling), the paste-back uses a sharper resize, skin detail is restored
    (restore_detail), and frames are written losslessly (FFV1) instead of MuseTalk's lossy re-encode."""
    t0 = time.time()
    upstream = Path(a.upstream).resolve()
    out = Path(a.out).resolve()
    work = Path(a.work).resolve()
    a.audio, a.frames, a.report = str(Path(a.audio).resolve()), str(Path(a.frames).resolve()), str(Path(a.report).resolve())
    work.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    os.environ.setdefault("GLOG_minloglevel", "2")
    os.chdir(upstream)
    sys.path.insert(0, str(upstream))
    import glob
    import pickle
    import subprocess
    import cv2
    import numpy as np
    import torch
    import mediapipe as mp
    from transformers import WhisperModel
    from musetalk.utils.audio_processor import AudioProcessor
    from musetalk.utils.blending import get_image
    from musetalk.utils.face_parsing import FaceParsing
    from musetalk.utils.preprocessing import coord_placeholder, get_landmark_and_bbox, read_imgs
    from musetalk.utils.utils import datagen, load_all_model

    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    with torch.no_grad():
        vae, unet, pe = load_all_model(unet_model_path="./models/musetalkV15/unet.pth", vae_type="sd-vae",
                                       unet_config="./models/musetalkV15/musetalk.json", device=device)
        pe, vae.vae, unet.model = pe.to(device), vae.vae.to(device), unet.model.to(device)
        timesteps = torch.tensor([0], device=device)
        ap = AudioProcessor(feature_extractor_path="./models/whisper")
        whisper = WhisperModel.from_pretrained("./models/whisper").to(device=device, dtype=unet.model.dtype).eval()
        fp = FaceParsing(left_cheek_width=90, right_cheek_width=90)
        imgs = sorted(glob.glob(os.path.join(a.frames, "*.png")))
        coords_path = work / "coords.pkl"
        if coords_path.exists():
            coords, frames = pickle.loads(coords_path.read_bytes()), read_imgs(imgs)
        else:
            coords, frames = get_landmark_and_bbox(imgs, 0)
            coords_path.write_bytes(pickle.dumps(coords))
        feats, length = ap.get_audio_feature(a.audio)
        chunks = ap.get_whisper_chunk(feats, device, unet.model.dtype, whisper, length, fps=a.fps,
                                      audio_padding_length_left=2, audio_padding_length_right=2)
        margin = 10

        def face_crop(frame, box):
            x1, y1, x2, y2 = box
            y2 = min(y2 + margin, frame.shape[0])
            return cv2.resize(frame[y1:y2, x1:x2], (256, 256), interpolation=cv2.INTER_LANCZOS4)

        # Optional appearance reference: MuseTalk pairs a lower-half-masked target with a reference image of
        # the same person (in training, another frame of the same video, often with the mouth open). A fixed
        # teeth-visible reference gives it real teeth to copy instead of inventing them from closed lips.
        ref_latent = None
        if a.ref_image:
            rc, rf = get_landmark_and_bbox([str(Path(a.ref_image).resolve())], 0)
            if rc[0] == coord_placeholder:
                raise SystemExit("no face found in the reference image")
            ref_latent = vae.encode_latents(vae.preprocess_img(face_crop(rf[0], rc[0]), half_mask=False))
        latents, boxes, keep = [], [], []
        for box, frame in zip(coords, frames):
            if box == coord_placeholder:
                continue
            x1, y1, x2, y2 = box
            y2 = min(y2 + margin, frame.shape[0])
            crop = face_crop(frame, box)
            if ref_latent is None:
                latents.append(vae.get_latents_for_unet(crop))
            else:
                masked = vae.encode_latents(vae.preprocess_img(crop, half_mask=True))
                latents.append(torch.cat([masked, ref_latent], dim=1))
            boxes.append((x1, y1, x2, y2))
            keep.append(frame)
        cyc = lambda xs: xs + xs[::-1]
        latents, boxes, keep = cyc(latents), cyc(boxes), cyc(keep)
        results = []
        for wb, lb in datagen(whisper_chunks=chunks, vae_encode_latents=latents, batch_size=a.batch_size,
                              delay_frame=0, device=device):
            feat = pe(wb.to(device))
            pred = unet.model(lb.to(device=device, dtype=unet.model.dtype), timesteps, encoder_hidden_states=feat).sample
            results.extend(vae.decode_latents(pred))
    h, w = keep[0].shape[:2]
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
                             "-r", str(a.fps), "-i", "-", "-c:v", "ffv1", "-level", "3", str(out)],
                            stdin=subprocess.PIPE)
    mesh_model = mp.solutions.face_mesh.FaceMesh(static_image_mode=False, max_num_faces=1, refine_landmarks=True)
    for i, res in enumerate(results):
        x1, y1, x2, y2 = boxes[i % len(boxes)]
        ori = keep[i % len(keep)]
        res = cv2.resize(res.astype(np.uint8), (x2 - x1, y2 - y1), interpolation=cv2.INTER_LANCZOS4)
        frame = get_image(ori.copy(), res, [x1, y1, x2, y2], mode=a.parsing_mode, fp=fp)
        if a.detail > 0:
            lm = mesh_model.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).multi_face_landmarks
            mesh = [(p.x * w, p.y * h) for p in lm[0].landmark] if lm else None
            frame = restore_detail(frame, ori, (x1, y1, x2, y2), mesh, a.detail, a.detail_sigma)
        proc.stdin.write(np.ascontiguousarray(frame).tobytes())
    proc.stdin.close()
    proc.wait()
    loaded = [m for m in BANNED_MODULES if m in sys.modules]
    ok = out.exists() and out.stat().st_size > 10_000 and not loaded and len(results) > 0
    Path(a.report).write_text(json.dumps({
        "ok": ok, "seconds": round(time.time() - t0, 1), "output": str(out) if ok else None, "frames": len(results),
        "fps": a.fps, "detail": a.detail, "detail_sigma": a.detail_sigma, "parsing_mode": a.parsing_mode,
        "ref_image": a.ref_image, "banned_modules_loaded": loaded,
        "engine": "MuseTalk 1.5 hq loop (lossless, native fps, detail restore)", "device": str(device)}, indent=1))
    if not ok:
        raise SystemExit("MuseTalk hq produced no usable output" + (f"; banned modules loaded: {loaded}" if loaded else ""))


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
    hq = sub.add_parser("lipsync_hq")
    for k in ("upstream", "frames", "audio", "out", "report", "work"):
        hq.add_argument(f"--{k}", required=True)
    hq.add_argument("--fps", type=int, default=30)
    hq.add_argument("--batch_size", type=int, default=8)
    hq.add_argument("--detail", type=float, default=0.9)
    hq.add_argument("--detail_sigma", type=float, default=1.8)
    hq.add_argument("--parsing_mode", default="jaw")
    hq.add_argument("--ref_image", default="")
    a = ap.parse_args()
    if a.cmd == "landmarks":
        landmarks(a.image, a.out)
    elif a.cmd == "mouth":
        mouth(a.video, a.out)
    elif a.cmd == "lipsync_hq":
        lipsync_hq(a)
    else:
        lipsync(a)


if __name__ == "__main__":
    main()
