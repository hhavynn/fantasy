"""Shots for the editorial_media treatment: real photography full-bleed, graphics as interruptions or insets.

  photo     a catalog photo cropped 9:16 around its subject (face kept in the upper third, headroom kept), moving
            slowly (push, pull or drift), graded, with mild depth (the background softens away from the subject),
            a small credit and any required label (FILE PHOTO 2023, ILLUSTRATIVE, FALLBACK). A source too small to
            fill the frame becomes a sharp card over a blurred fill of itself instead of being upscaled to mush.
            Overlays: the spoken number slammed in the frame's empty zone, or a team result's score.
  broll     context B-roll the same way, always labelled ILLUSTRATIVE; `snap_zoom` punches into its subject on
            the joke's sound hit (the ruler's first inch on "smaller ruler").
  graphic   the beat's original scene (render_motion's renderer), drawn at the beat's own clock: an interruption
  inset     that graphic as a card over the player's blurred photo, placed away from his face
  presenter a pre-rendered host clip (only when one exists for the beat)
  missing   a labelled slate naming the media that is missing: storyboard and preview only, QC blocks publishing

Every frame reacts to the beat's sound cues (a small punch-in and flash on each hit; a hard stop freezes it).
Numbers drawn here are claim display strings. Zones are where things may go without covering the player:
the planner's subject box, mapped through the crop, decides where the caption and the overlays sit.
"""

from __future__ import annotations

import math
import subprocess
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from . import edit_kit as EK
from . import media_library as ML
from . import motion as M

W, H = M.W, M.H
ASPECT = W / H
MAX_UPSCALE = 2.6                      # beyond this a full-bleed crop is soft: use the card layout
ZONES = {"top": (250, 560), "mid": (700, 1120), "low": (1300, 1560)}


# ------------------------------------------------------------------------------------------- geometry

def crop_window(sw: int, sh: int, subject: dict, framing: str = "medium") -> tuple[tuple[float, float, float, float], bool]:
    """A 9:16 window (x0, y0, x1, y1 in source px) framing the subject, and whether it fits inside the source.

    wide: the subject fills ~60% of the frame height; medium: the subject box fills it; tight: the face fills
    ~22% (no face: the top half of the subject). The face sits at about a third of the frame height."""
    l, t, r, b = subject.get("box") or (0.2, 0.1, 0.8, 0.95)
    face = subject.get("face")
    bx0, by0, bx1, by1 = l * sw, t * sh, r * sw, b * sh
    bh = max(by1 - by0, 1)
    if framing == "tight" and face:
        fh = (face[3] - face[1]) * sh
        h = fh / 0.22
    elif framing == "tight":
        h = bh * 0.6
    elif framing == "wide":
        h = bh / 0.6
    else:
        h = bh * 1.06
    h = max(h, 360.0)
    w = h * ASPECT
    if face:
        cx = (face[0] + face[2]) / 2 * sw
        cy_face = (face[1] + face[3]) / 2 * sh
        y0 = cy_face - h * 0.32
    else:
        cx = (bx0 + bx1) / 2
        y0 = (by0 + by1) / 2 - h * 0.5 if framing != "tight" else by0 - h * 0.08
    # never wider/taller than the source: shrink to fit, keeping 9:16
    fits = True
    if w > sw:
        w = sw
        h = w / ASPECT
    if h > sh:
        h = sh
        w = h * ASPECT
        if w > sw:
            fits = False
    x0 = min(max(cx - w / 2, 0), max(sw - w, 0))
    y0 = min(max(y0, 0), max(sh - h, 0))
    av = subject.get("avoid")
    if av:                                           # keep a burned-in graphic (TV lower third) out of frame
        ax0, ay0, ax1, ay1 = av[0] * sw, av[1] * sh, av[2] * sw, av[3] * sh
        if y0 + h > ay0 and x0 < ax1 and x0 + w > ax0:
            top_face = face[1] * sh if face else by0
            if ay0 - h >= 0 and ay0 - h <= top_face:
                y0 = ay0 - h
            else:                                    # shrink until the window ends above it, face still in
                h2 = max(ay0 - max(0.0, top_face - 0.12 * h), 300.0)
                if h2 < h:
                    w2 = h2 * ASPECT
                    x0 = min(max(cx - w2 / 2, 0), max(sw - w2, 0))
                    y0, w, h = max(ay0 - h2, 0), w2, h2
    return (x0, y0, x0 + w, y0 + h), fits


def motion_window(win, sw, sh, motion: str, k: float, hit_k: float = 0.0, focus=None):
    """The window at progress k (0..1) for a camera move; hit_k (0..1) is the snap-zoom amount."""
    x0, y0, x1, y1 = win
    w, h = x1 - x0, y1 - y0
    e = M.ease_in_out(k)
    if motion == "push":
        s = 1 - 0.09 * e
    elif motion == "pull":
        s = 0.91 + 0.09 * e
    elif motion in ("drift", "drift_left"):
        s = 0.97 - 0.03 * e
    else:                                   # snap / still
        s = 1.0 - 0.02 * e
    if hit_k > 0:
        s *= 1 - 0.55 * M.ease_out(hit_k)
    nw, nh = w * s, h * s
    fx, fy = focus if focus else ((x0 + x1) / 2, y0 + h * 0.36)
    cx = x0 + w / 2 + (fx - (x0 + w / 2)) * (1 - s) * 1.6
    cy = y0 + h / 2 + (fy - (y0 + h / 2)) * (1 - s) * 1.6
    if motion == "drift":
        cx += w * 0.05 * (e - 0.5)
    if motion == "drift_left":
        cx -= w * 0.05 * (e - 0.5)
    nx0 = min(max(cx - nw / 2, 0), max(sw - nw, 0))
    ny0 = min(max(cy - nh / 2, 0), max(sh - nh, 0))
    return nx0, ny0, nx0 + nw, ny0 + nh


def to_frame(win, sw, sh, box) -> tuple[float, float, float, float]:
    """A source-fraction box mapped into frame pixels through a window."""
    x0, y0, x1, y1 = win
    sx, sy = W / (x1 - x0), H / (y1 - y0)
    return ((box[0] * sw - x0) * sx, (box[1] * sh - y0) * sy, (box[2] * sw - x0) * sx, (box[3] * sh - y0) * sy)


def caption_zone(subject_px, face_px=None) -> str:
    """'top' or 'low', whichever the player covers least (his face counts four times)."""
    def overlap(zone, box):
        if not box:
            return 0.0
        a, b = ZONES[zone]
        return max(0.0, min(b, box[3]) - max(a, box[1])) * max(0.0, min(W, box[2]) - max(0, box[0])) / (W * (b - a))
    score = {z: overlap(z, subject_px) + 4 * overlap(z, face_px) for z in ("low", "top")}
    return min(score, key=lambda z: (score[z], z != "low"))


def overlay_zone(cap: str, face_px=None) -> str:
    if face_px and ZONES["mid"][0] < (face_px[1] + face_px[3]) / 2 < ZONES["mid"][1]:
        return "top" if cap == "low" else "low"
    return "mid"


# --------------------------------------------------------------------------------------------- looks

def grade(img: Image.Image, look: str) -> Image.Image:
    if look == "punch":
        img = ImageEnhance.Contrast(ImageEnhance.Color(img).enhance(1.18)).enhance(1.12)
    elif look == "cold":
        img = ImageEnhance.Contrast(ImageEnhance.Color(img).enhance(0.72)).enhance(1.1)
        img = Image.blend(img, Image.new("RGB", img.size, (40, 60, 90)), 0.08)
    elif look == "muted":
        img = ImageEnhance.Contrast(ImageEnhance.Color(img).enhance(0.82)).enhance(1.04)
    else:
        img = ImageEnhance.Contrast(img).enhance(1.06)
    return img


@lru_cache(maxsize=1)
def _vignette() -> Image.Image:
    v = Image.new("L", (W, H), 0)
    ImageDraw.Draw(v).ellipse((-W * 0.35, -H * 0.18, W * 1.35, H * 1.18), fill=255)
    return v.filter(ImageFilter.GaussianBlur(220))


@lru_cache(maxsize=1)
def _shade() -> Image.Image:
    """A soft dark gradient at the top and bottom so labels and captions read on any photo."""
    a = np.zeros((H, 1), np.float32)
    y = np.arange(H)[:, None]
    a += np.clip((420 - y) / 420, 0, 1) * 110
    a += np.clip((y - (H - 760)) / 760, 0, 1) * 150
    return Image.fromarray(np.repeat(a, W, axis=1).astype(np.uint8), "L")


def card_box(sw: int, sh: int, subject: dict) -> tuple[float, float, float, float]:
    """A 4:5 portrait crop around the subject for the card layout (kept above any burned-in graphic)."""
    l, t, r, b = subject.get("box") or (0.1, 0.05, 0.9, 0.95)
    bottom = sh
    if subject.get("avoid"):
        bottom = min(sh, subject["avoid"][1] * sh)
    h = min(bottom, max((b - t) * sh * 1.1, 200))
    w = min(sw, h * 0.8)
    h = w / 0.8
    f = subject.get("face")
    cx = ((f[0] + f[2]) / 2 if f else (l + r) / 2) * sw
    cy = ((f[1] + f[3]) / 2 * sh + h * 0.18) if f else (t + b) / 2 * sh
    x0 = min(max(cx - w / 2, 0), sw - w)
    y0 = min(max(cy - h / 2, 0), max(bottom - h, 0))
    return x0, y0, x0 + w, y0 + h


def _cover(win, sw, sh):
    """Grow a window that is too wide for 9:16 into one the source can fill (centred on the same point)."""
    x0, y0, x1, y1 = win
    h = min(sh, y1 - y0)
    w = h * ASPECT
    if w > sw:
        w = sw
        h = w / ASPECT
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    nx = min(max(cx - w / 2, 0), sw - w)
    ny = min(max(cy - h / 2, 0), sh - h)
    return nx, ny, nx + w, ny + h


def finish(img: Image.Image, t: float) -> Image.Image:
    img = Image.composite(img, Image.blend(img, Image.new("RGB", (W, H), (0, 0, 0)), 0.45), _vignette())
    img = Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), img, _shade())
    return img


@lru_cache(maxsize=24)
def prepared(path: str, look: str, depth_box: tuple | None) -> Image.Image:
    """The graded source, with the background softened away from the subject (mild depth). Cached per shot."""
    im = Image.open(path).convert("RGB")
    im = grade(im, look)
    if depth_box:
        sw, sh = im.size
        blur = im.filter(ImageFilter.GaussianBlur(max(2, sw / 500)))
        mask = Image.new("L", im.size, 0)
        l, t, r, b = depth_box
        pad_x, pad_y = (r - l) * sw * 0.12, (b - t) * sh * 0.08
        ImageDraw.Draw(mask).rounded_rectangle((l * sw - pad_x, t * sh - pad_y, r * sw + pad_x, b * sh + pad_y),
                                               radius=int(min(sw, sh) * 0.08), fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(min(sw, sh) * 0.06))
        im = Image.composite(im, blur, mask)
    return im


@lru_cache(maxsize=8)
def _fill(path: str, look: str) -> Image.Image:
    im = prepared(path, look, None)
    sw, sh = im.size
    s = max(W / sw, H / sh)
    big = im.resize((int(sw * s) + 1, int(sh * s) + 1), Image.BILINEAR)
    x, y = (big.width - W) // 2, (big.height - H) // 2
    bg = big.crop((x, y, x + W, y + H)).filter(ImageFilter.GaussianBlur(38))
    return Image.blend(bg, Image.new("RGB", (W, H), (0, 0, 0)), 0.45)


# ------------------------------------------------------------------------------------------ labels/text

def label_chip(img: Image.Image, labels: list[str], credit: str, y: int = 238):
    d = ImageDraw.Draw(img, "RGBA")
    x = M.SAFE_LEFT
    for lab in labels:
        f = M.font("black", 28)
        w = f.getlength(lab) + 28
        col = (255, 196, 0, 235) if lab == "FALLBACK" else (255, 255, 255, 230)
        d.rounded_rectangle((x, y, x + w, y + 44), radius=8, fill=(0, 0, 0, 150), outline=col, width=2)
        d.text((x + 14, y + 22), lab, font=f, fill=col, anchor="lm")
        x += w + 12
    if credit:
        f = M.font("semibold", 22)
        d.text((M.SAFE_LEFT + 2, y + (58 if labels else 8)), "Photo: " + credit, font=f, fill=(235, 235, 235, 200),
               anchor="la", stroke_width=2, stroke_fill=(0, 0, 0, 160))


def caption(words: list[dict], active: int, accent, t_in: float, y_center: float) -> tuple[Image.Image, int]:
    """Minimal subtitles: one line when it fits, mixed case kept off (upper), a soft shadow, the spoken word lit."""
    f = M.font("black", 56)
    space = f.getlength(" ")
    texts = [w["text"].upper() for w in words]
    widths = [f.getlength(s) for s in texts]
    lines, cur, cw = [], [], 0.0
    for i, ww in enumerate(widths):
        if cur and cw + space + ww > 900:
            lines.append(cur)
            cur, cw = [], 0.0
        cur.append(i)
        cw += (space if len(cur) > 1 else 0) + ww
    if cur:
        lines.append(cur)
    lh = 66
    layer = Image.new("RGBA", (W, lh * len(lines) + 60), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    rise = (1 - M.ease_out(t_in * 7)) * 14
    for li, line in enumerate(lines):
        total = sum(widths[i] for i in line) + space * (len(line) - 1)
        x = M.CENTER_X - total / 2
        for i in line:
            col = M.INK
            if words[i].get("emph"):
                col = accent
            if i == active:
                col = M.GOLD if not words[i].get("emph") else accent
            d.text((x, 30 + li * lh + rise), texts[i], font=f, fill=col, anchor="la", stroke_width=7,
                   stroke_fill=(5, 6, 9))
            x += widths[i] + space
    shadow = layer.getchannel("A").filter(ImageFilter.GaussianBlur(10)).point(lambda v: int(v * 0.55))
    out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    out.paste(Image.new("RGBA", layer.size, (0, 0, 0, 255)), (0, 6), shadow)
    out.alpha_composite(layer)
    return out, int(y_center - out.height / 2)


def stat_overlay(img, number: str, word: str, zone: str, t: float, at: float, accent):
    cy = sum(ZONES[zone]) / 2
    k = EK.slam(img, number, M.CENTER_X, cy - 40, t, at, size=250, fill=M.INK, stroke=10)
    if k > 0 and word:
        M.put_text(img, word.upper(), M.CENTER_X, cy + 120, "black", 72, accent, alpha=M.clamp(k * 2), stroke=7)


def missing_slate(img: Image.Image, what: str, t: float):
    d = ImageDraw.Draw(img, "RGBA")
    img.paste((22, 22, 28), (0, 0, W, H))
    for x in range(-H, W, 60):                        # hazard stripes: nobody mistakes this for a finished shot
        d.line((x, 0, x + H, H), fill=(255, 196, 0, 18), width=22)
    M.put_text(img, "MEDIA NEEDED", M.CENTER_X, 760, "black", 96, (255, 196, 0))
    for i, line in enumerate(_wrap(what, 26)):
        M.put_text(img, line, M.CENTER_X, 900 + i * 70, "bold", 56, M.INK)
    M.put_text(img, "placeholder - not for publishing", M.CENTER_X, 1120, "semibold", 40, M.MUTED)


def _wrap(text: str, n: int) -> list[str]:
    out, cur = [], ""
    for w in text.split():
        if cur and len(cur) + 1 + len(w) > n:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return out + ([cur] if cur else [])


# ------------------------------------------------------------------------------------------- clips

class ClipReader:
    """Frames of a video clip, cover-scaled to 1080x1920, read forward through one ffmpeg pipe."""

    def __init__(self, path: str, start: float = 0.0, fps: int = 30):
        self.proc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", path, "-an", "-vf",
             f"fps={fps},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.last: Image.Image | None = None
        self.n = 0

    def frame(self, i: int) -> Image.Image:
        while self.n <= i:
            raw = self.proc.stdout.read(W * H * 3) if self.proc.stdout else b""
            if len(raw) < W * H * 3:
                break
            self.last = Image.frombytes("RGB", (W, H), raw)
            self.n += 1
        return (self.last or Image.new("RGB", (W, H), M.DARK)).copy()

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()


# ------------------------------------------------------------------------------------------- shots

class ShotRenderer:
    """Draws one storyboard shot at a time; graphics go through render_motion.draw_scene."""

    def __init__(self, lib: ML.Library, ctx, scenes_by_seg: dict, look: str, season: int, fps: int = 30):
        self.lib, self.c, self.scenes, self.look, self.season, self.fps = lib, ctx, scenes_by_seg, look, season, fps
        self.clips: dict[int, ClipReader] = {}

    def geometry(self, shot: dict):
        e = self.lib.get(shot["asset"])
        im = prepared(str(e.file), self.look, None)
        win, fits = crop_window(im.width, im.height, e.subject, shot["crop"]["framing"])
        up = H / (win[3] - win[1])
        return e, im.size, win, (fits and up <= MAX_UPSCALE)

    def zones(self, shot: dict) -> tuple[str, str]:
        """(caption zone, overlay zone) for a shot, from where the player sits in the frame at mid-shot."""
        if shot["kind"] == "inset" or (shot.get("asset") and self.lib.get(shot["asset"]).type == "headshot_fallback"):
            return "low", "top"
        if shot["kind"] not in ("photo", "broll") or not shot.get("asset"):
            return "low", "mid"
        e, (sw, sh), win, full = self.geometry(shot)
        if not full:
            return "low", "top"
        mid = motion_window(win, sw, sh, shot["crop"]["motion"], 0.5)
        sub = to_frame(mid, sw, sh, e.subject["box"])
        face = to_frame(mid, sw, sh, e.subject["face"]) if e.subject.get("face") else None
        cap = caption_zone(sub, face)
        return cap, overlay_zone(cap, face)

    def draw(self, shot: dict, t: float, seg_dur: float, tokens: list[dict], accent) -> Image.Image:
        """t: seconds into the beat (the beat's own clock, so cues and graphics stay in sync)."""
        from .stages import render_motion as RM
        self.c.tokens = tokens
        scene = self.scenes[shot["segment"]]
        kind = shot["kind"]
        k = M.clamp((t - shot["t0"]) / max(shot["t1"] - shot["t0"], 0.1))
        hits = EK.hits(self.c, scene)
        times = [h for h, sfx, _ in hits if sfx not in ("music_stop", "music_cut", "toggle_click")]
        stop = next((h for h, sfx, _ in hits if sfx == "music_stop"), None)
        if kind == "graphic":
            return RM.draw_scene(scene, t, seg_dur, self.c)
        if kind == "missing":
            img = Image.new("RGB", (W, H), M.DARK)
            missing_slate(img, shot.get("missing") or shot.get("wanted") or "media", t)
            return img
        if kind == "presenter" and shot.get("clip"):
            reader = self.clips.setdefault(shot["index"], ClipReader(shot["clip"], shot.get("clip_start", 0.0), self.fps))
            return reader.frame(int((t - shot["t0"]) * self.fps))
        e, (sw, sh), win, full = self.geometry(shot)
        if e.type == "headshot_fallback":              # a cutout on the team's backdrop, never a stretched square
            img = EK.backdrop("hype", e.team or self.c.team, t + shot.get("start", 0))
            from .stages.story import last
            cut = EK.cutout(str(e.file), e.team or self.c.team, e.jersey or "", last(e.name or ""), 1150,
                            rim=EK.team_rgb(e.team or self.c.team)[0])
            sc = 1.0 + 0.06 * M.ease_in_out(k)
            M.paste(img, cut, W / 2, H + 40 - cut.height * sc / 2, sc)
        elif e.is_video:
            reader = self.clips.setdefault(shot["index"], ClipReader(str(e.file), shot.get("clip_start", 0.0), self.fps))
            img = reader.frame(int((t - shot["t0"]) * self.fps))
        else:
            hit_k = 0.0
            if shot.get("effect") == "snap_zoom":
                at = next((h for h in times if shot["t0"] - 0.05 <= h <= shot["t1"]), shot["t0"] + (shot["t1"] - shot["t0"]) * 0.6)
                hit_k = M.clamp((t - at) / 0.16) if t >= at else 0.0
            focus = None
            if e.subject.get("face"):
                f = e.subject["face"]
                focus = ((f[0] + f[2]) / 2 * sw, (f[1] + f[3]) / 2 * sh)
            elif shot.get("effect") == "snap_zoom":
                b = e.subject["box"]
                focus = ((b[0] + b[2]) / 2 * sw, (b[1] + b[3]) / 2 * sh)
            src = prepared(str(e.file), self.look, tuple(e.subject["box"]) if e.type in ML.PLAYER_PHOTOS else None)
            if full or kind == "inset":              # an inset's photo sits behind a card: softness is fine there
                mw = motion_window(win, sw, sh, shot["crop"]["motion"], k, hit_k, focus)
                if not full:
                    mw = _cover(mw, sw, sh)
                img = src.resize((W, H), Image.BICUBIC if (mw[3] - mw[1]) < H else Image.BILINEAR, box=mw)
            else:                                     # card over a blurred fill of itself: sharp, not mushy
                img = _fill(str(e.file), self.look).copy()
                cb = card_box(sw, sh, e.subject)
                cw_, ch_ = cb[2] - cb[0], cb[3] - cb[1]
                s = min(940 / cw_, 1180 / ch_) * (1.0 + 0.04 * M.ease_in_out(k)) * (1 + 0.35 * M.ease_out(hit_k))
                card = src.resize((int(cw_ * s), int(ch_ * s)), Image.LANCZOS, box=cb)
                mask = Image.new("L", card.size, 0)
                ImageDraw.Draw(mask).rounded_rectangle((0, 0, card.width - 1, card.height - 1), radius=26, fill=255)
                sh_ = Image.new("L", (card.width + 80, card.height + 80), 0)
                sh_.paste(mask, (40, 40))
                img.paste((0, 0, 0), (W // 2 - card.width // 2 - 40 + 12, 900 - card.height // 2 - 40 + 22),
                          sh_.filter(ImageFilter.GaussianBlur(24)).point(lambda v: int(v * 0.6)))
                img.paste(card, (W // 2 - card.width // 2, 900 - card.height // 2), mask)
        img = finish(img, t)
        if kind == "inset":
            img = Image.blend(img.filter(ImageFilter.GaussianBlur(4)), Image.new("RGB", (W, H), (0, 0, 0)), 0.3)
            g = RM.draw_scene(scene, t, seg_dur, self.c)
            cardw, cardh = int(W * 0.58), int(H * 0.58)
            gc = g.resize((cardw, cardh), Image.BILINEAR)
            m = Image.new("L", gc.size, 0)
            ImageDraw.Draw(m).rounded_rectangle((0, 0, cardw - 1, cardh - 1), radius=30, fill=255)
            ki = M.ease_out(M.clamp((t - shot["t0"]) / 0.25))
            y = int(700 + (1 - ki) * 140)            # the card below his face: the face stays in the top third
            img.paste((0, 0, 0), (W // 2 - cardw // 2 + 10, y + 18),
                      m.filter(ImageFilter.GaussianBlur(20)).point(lambda v: int(v * 0.5 * ki)))
            img.paste(gc, (W // 2 - cardw // 2, y), m.point(lambda v: int(v * ki)))
        cap, ov = self.zones(shot)
        if shot.get("overlay") == "stat" and shot.get("stat_claim"):
            tok_i = next((i for i, tk in enumerate(tokens) if tk["claim"] == shot["stat_claim"]), None)
            if tok_i is not None:
                tok = tokens[tok_i]
                num = " ".join(tk["text"] for tk in tokens if tk["claim"] == shot["stat_claim"]).rstrip(".,?!")
                nxt = next((tk["text"].rstrip(".,?!") for tk in tokens[tok_i + 1:] if not tk["claim"]), "")
                stat_overlay(img, num, nxt if len(nxt) > 2 else "", ov, t, tok["start"], accent)
        if shot.get("overlay") == "score":
            nums = [tk for tk in tokens if tk["claim"] and tk["text"].rstrip(".,").isdigit()
                    and shot["t0"] - 0.05 <= tk["start"] < shot["t1"]]
            if len(nums) >= 2:
                EK.slam(img, f"{nums[0]['text'].rstrip('.,')}–{nums[1]['text'].rstrip('.,')}", M.CENTER_X,
                        sum(ZONES[ov]) / 2, t, nums[1]["start"], size=230, fill=M.INK, stroke=10)
        label_chip(img, shot.get("labels", []), shot.get("credit", ""))
        # the beat's sound hits land on the picture: a punch and a flash; a hard stop freezes it
        if stop is not None and t >= stop:
            img = EK.freeze(img, M.clamp((t - stop) / 0.12) * (0.0 if self.c.story == "praise" else 1.0))
        else:
            amt = 10 * EK.hit_strength(t, times, 0.16)
            img = EK.flash(EK.shake(img, amt, t), 0.35 * EK.hit_strength(t, times, 0.05))
        return img

    def close(self):
        for r in self.clips.values():
            r.close()
