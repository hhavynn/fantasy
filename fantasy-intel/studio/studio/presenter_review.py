"""A/B review montages for presenter variants: the same frames, side by side.

    uv run python -m studio.presenter_review <out_dir> <start>-<end>[,<start>-<end>...] LABEL=video.mp4 ...

Writes, for up to four variants of the same episode (same audio and timing):
  phone_grid.mp4   all variants 2x2 at phone size, full length, with the shared audio
  face_zoom.mp4    the host windows only, each variant's face cropped and enlarged 2x, 2x2
Labels are drawn with Pillow (this FFmpeg build has no drawtext).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from . import motion as M

FACE_CROP = (300, 820, 720, 1320)          # where the host's face sits in the composite (presenter layout)


def _label(text: str, w: int, path: Path) -> None:
    img = Image.new("RGBA", (w, 70), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, min(w - 8, 30 + int(M.font("black", 36).getlength(text))), 62), radius=14,
                        fill=(0, 0, 0, 190))
    d.text((20, 35), text, font=M.font("black", 36), fill=M.GOLD, anchor="lm")
    img.save(path)


def _grid(inputs: list[str], labels: list[Path], cell: tuple[int, int], pre: str, out: Path, audio: bool,
          trims: list[tuple[float, float]] | None = None) -> None:
    n = len(inputs)
    w, h = cell
    args = ["ffmpeg", "-v", "error", "-y"]
    for i in inputs:
        args += ["-i", i]
    for lab in labels:
        args += ["-i", str(lab)]
    parts = []
    for i in range(n):
        chain = f"[{i}:v]"
        if trims:
            segs = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in trims)
            chain += f"select='{segs}',setpts=N/30/TB,"
        parts.append(f"{chain}{pre}scale={w}:{h}[v{i}]")
        parts.append(f"[v{i}][{n + i}:v]overlay=0:0[l{i}]")
    while n < 4:                                      # pad to a 2x2 grid
        parts.append(f"color=c=black:s={w}x{h}:d=1[l{n}]")
        n += 1
    parts.append(f"[l0][l1][l2][l3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0[g]")
    args += ["-filter_complex", ";".join(parts), "-map", "[g]"]
    if audio and not trims:
        args += ["-map", "0:a", "-c:a", "aac"]
    args += ["-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30", "-shortest", str(out)]
    subprocess.run(args, check=True)


def build(out_dir: Path, variants: list[tuple[str, str]], windows: list[tuple[float, float]]) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    labels = []
    for i, (name, _) in enumerate(variants):
        p = tmp / f"label{i}.png"
        _label(name, 540, p)
        labels.append(p)
    vids = [v for _, v in variants]
    phone = out_dir / "phone_grid.mp4"
    _grid(vids, labels, (540, 960), "", phone, audio=True)
    zoom = out_dir / "face_zoom.mp4"
    x0, y0, x1, y1 = FACE_CROP
    _grid(vids, labels, (x1 - x0, y1 - y0), f"crop={x1 - x0}:{y1 - y0}:{x0}:{y0},", zoom, audio=False, trims=windows)
    return [phone, zoom]


def main(argv: list[str] | None = None) -> int:
    a = argv if argv is not None else sys.argv[1:]
    out = Path(a[0])
    wins = [tuple(float(x) for x in w.split("-")) for w in a[1].split(",")]
    variants = [tuple(v.split("=", 1)) for v in a[2:]]
    for p in build(out, variants, wins):
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
