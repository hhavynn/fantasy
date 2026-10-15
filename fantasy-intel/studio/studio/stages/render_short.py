"""Stage 5 (Shorts): 1080x1920 animated video, drawn frame by frame and piped straight into FFmpeg.

Each beat has its own card (hero stat, weekly bars that grow, support stats, playoff tiles, follow card);
elements ease in, then captions carry the motion. Captions are bold one-to-two-line chunks burned into the
frames. Layout keeps clear of the Shorts UI: nothing important above y=180, right of x=950 in the lower
half, or below y=1600.

Every number drawn is a claim display string or a snapshot value through numbers.render.
"""

from __future__ import annotations

import math
import re
import subprocess
from functools import lru_cache

import numpy as np
import soundfile as sf
from PIL import Image, ImageDraw, ImageFont

from .. import visual as V
from ..core import FONTS, Episode, read_json, run_stage, write_json
from ..numbers import TEAM_NAMES, render
from .assemble import LOUDNESS, _wrap_px, srt_time, time_chunks
from .facts import FAVORABLE_RANK, TOUGH_RANK
from .script import fill

VERSION = 5
W, H = 1080, 1920
LEFT, RIGHT = 80, 1000
CAPTION = {"font": "Inter-Black.ttf", "font_size": 64, "max_width_px": 860, "max_lines": 2, "center_y": 1450}


@lru_cache(maxsize=None)
def F(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / V.WEIGHTS[weight]), size)


def ease(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


def mix(c1: str, c2: str, a: float) -> tuple[int, int, int]:
    """Colour c2 faded in over c1 by a (0..1)."""
    a = min(max(a, 0.0), 1.0)
    p, q = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (c1, c2))
    return tuple(int(p[i] + (q[i] - p[i]) * a) for i in range(3))


class Frame:
    def __init__(self):
        self.img = Image.new("RGB", (W, H), V.BG)
        self.d = ImageDraw.Draw(self.img)

    def text(self, xy, s, weight, size, color, anchor="la", t=1.0, rise=30):
        a = ease(t)
        self.d.text((xy[0], xy[1] + (1 - a) * rise), s, font=F(weight, size), fill=mix(V.BG, color, a), anchor=anchor)

    def fit(self, s, weight, size, width):
        while size > 20 and self.d.textlength(s, font=F(weight, size)) > width:
            size -= 2
        return size

    def rect(self, box, fill, radius=18, outline=None, width=0, t=1.0):
        a = ease(t)
        self.d.rounded_rectangle(box, radius=radius, fill=mix(V.BG, fill, a) if fill else None,
                                 outline=mix(V.BG, outline, a) if outline else None, width=width)


def band(rank: int) -> str:
    return "favorable" if rank <= FAVORABLE_RANK else "tough" if rank >= TOUGH_RANK else "middle-of-the-pack"


class ShortCards:
    def __init__(self, ep: Episode):
        self.facts = read_json(ep.path("facts.json"))
        self.claims = {c["id"]: c for c in self.facts["claims"]}
        self.p = self.facts["players"][0]
        self.color = V.POSITIVE if self.p["role"] == "riser" else V.NEGATIVE
        self.week = self.d(self.facts["episode"]["week"])

    def d(self, cid):
        return self.claims[cid]["display"]

    def header(self, f: Frame, t: float, kicker: str):
        p = self.p
        f.text((LEFT, 200), f"{self.week.upper()} USAGE {kicker}", "bold", 34, self.color, t=t * 3)
        size = f.fit(p["name"], "black", 84, RIGHT - LEFT)
        f.text((LEFT, 250), p["name"], "black", size, V.INK, t=t * 3 - 0.2)
        f.text((LEFT, 260 + size + 8), f"{p['position']} · {self.d(p['team_name'])}", "semibold", 40,
               V.INK_MUTED, t=t * 3 - 0.4)

    # ------------------------------------------------------------------------------------- beats

    def hero_stat(self, f: Frame, t: float, shot: dict):
        """Fully drawn from the very first frame: player, protagonist stat and the change are readable before
        any narration, with sound off, and wherever YouTube grabs a preview frame."""
        m = self.p["metrics"][shot.get("metric", 0)]
        self.header(f, 1.0, "RISER" if self.p["role"] == "riser" else "FALLER")
        f.text((W / 2 - 40, 500), m["label"].upper(), "black", 56, self.color, "ma")
        f.text((W / 2 - 40, 620), f"EARLIER {self.d(self.p['baseline_games'])} GAMES", "bold", 32, V.INK_FAINT, "ma")
        f.text((W / 2 - 40, 665), self.d(m["baseline"]), "black", 150, V.INK_FAINT, "ma")
        f.text((W / 2 - 40, 880), f"LAST {self.d(self.p['recent_games'])} GAMES", "bold", 32, self.color, "ma")
        pop = ease(t * 2.5)                      # a small settle, never invisible
        size = int(230 * (0.94 + 0.06 * pop))
        f.text((W / 2 - 40, 925 + (230 - size) / 2), self.d(m["recent"]), "black", size, V.INK, "ma", rise=0)
        f.text((W / 2 - 40, 1200), self.d(m["change"]), "bold", 46, self.color, "ma")

    def takeaway_card(self, f: Frame, t: float, shot: dict):
        headline = {"expansion": ["THE ROLE", "GOT BIGGER"], "unresolved": ["DO THE TARGETS", "FOLLOW?"],
                    "takeover": ["DOES THE", "ROLE HOLD?"], "contraction": ["THE ROLE IS", "SHRINKING"]}[shot["angle"]]
        m = self.p["metrics"][0]
        self.header(f, 1.0, "RISER" if self.p["role"] == "riser" else "FALLER")
        for i, line in enumerate(headline):
            f.text((LEFT, 560 + i * 140), line, "black", f.fit(line, "black", 130, RIGHT - LEFT - 40),
                   self.color if i == 1 else V.INK, t=t * 6 + 0.2 - i * 0.3)
        recap = f"{m['label'].capitalize()}: {self.d(m['baseline'])} → {self.d(m['recent'])}"
        f.text((LEFT, 900), recap, "bold", 48, V.INK_MUTED, t=t * 5 - 0.6)
        f.text((LEFT, 970), f"Last {self.d(self.p['recent_games'])} games vs the {self.d(self.p['baseline_games'])} before",
               "regular", 36, V.INK_FAINT, t=t * 5 - 0.8)

    def usage_bars(self, f: Frame, t: float, shot: dict):
        p, m = self.p, self.p["metrics"][shot.get("metric", 0)]
        self.header(f, 1.0, "RISER" if p["role"] == "riser" else "FALLER")
        f.text((LEFT, 470), f"{m['label'].capitalize()} by week", "bold", 46, V.INK, t=t * 4)
        series = m["series"]
        recent = set(p["recent_weeks"])
        top = max([s["value"] or 0 for s in series] + [0.05])
        x0, x1, y_base, h_max = LEFT, 920, 1230, 560
        n = len(series)
        gap = 26
        bw = (x1 - x0 - gap * (n - 1)) / n
        for i, s in enumerate(series):
            grow = ease((t - 0.08 - i * 0.07) * 3.2)
            val = max(s["value"] or 0, 0)
            h = h_max * val / top * grow
            x = x0 + i * (bw + gap)
            col = self.color if s["week"] in recent else "#3a4656"
            if h > 1:
                f.d.rounded_rectangle((x, y_base - h, x + bw, y_base), radius=10, fill=col)
            label = render("pct", s["value"])[0] if s["value"] is not None else "—"
            f.text((x + bw / 2, y_base - h - 52), label, "bold", 36, V.INK, "ma", t=grow * 1.3 - 0.3, rise=0)
            f.text((x + bw / 2, y_base + 18), f"Wk {s['week']}", "semibold", 30, V.INK_MUTED, "ma", t=t * 4)
        f.d.line((x0, y_base, x1, y_base), fill=V.LINE, width=3)
        legend_t = t * 3 - 1.2
        f.text((LEFT, 1300), f"Before: {self.d(m['baseline'])}", "bold", 40, V.INK_FAINT, t=legend_t)
        f.text((520, 1300), f"Now: {self.d(m['recent'])}", "bold", 40, self.color, t=legend_t - 0.2)

    def support_card(self, f: Frame, t: float, shot: dict):
        p = self.p
        self.header(f, 1.0, "RISER" if p["role"] == "riser" else "FALLER")
        rows = []
        if len(p["metrics"]) > 1:
            m = p["metrics"][1]
            rows.append((m["label"].upper(), f"{self.d(m['baseline'])} → {self.d(m['recent'])}"))
        vol_label = "TARGETS PER GAME" if "target" in p["volume"]["label"] else "DESIGNED RUNS PER GAME"
        rows.append((f"{vol_label}, LAST {self.d(p['recent_games'])}", self.d(p["volume"]["recent"])))
        y = 470
        for i, (label, value) in enumerate(rows):
            rt = t * 3.5 - i * 0.5
            f.rect((LEFT, y, RIGHT - 60, y + 210), V.SURFACE, outline=V.LINE, width=2, t=rt)
            f.text((LEFT + 36, y + 34), label, "bold", 30, V.INK_FAINT, t=rt)
            f.text((LEFT + 36, y + 84), value, "black", f.fit(value, "black", 96, RIGHT - LEFT - 140), V.INK, t=rt)
            y += 240
        ctx = p["context"]
        if len(self.claims[ctx["qbs_recent"]]["value"]) < 2:
            return                       # the QB box only appears when the narration mentions a QB change
        label, value = "STARTING QBS IN THAT STRETCH", self.d(ctx["qbs_recent"])
        rt = t * 3.5 - len(rows) * 0.5
        f.rect((LEFT, y, RIGHT - 60, y + 210), V.SURFACE, outline=V.LINE, width=2, t=rt)
        f.text((LEFT + 36, y + 34), label, "bold", 30, V.INK_FAINT, t=rt)
        f.text((LEFT + 36, y + 90), value, "black", f.fit(value, "black", 64, RIGHT - LEFT - 140), V.INK, t=rt)

    def playoff_tiles(self, f: Frame, t: float, shot: dict):
        p = self.p
        self.header(f, 1.0, "RISER" if p["role"] == "riser" else "FALLER")
        f.text((LEFT, 470), "Fantasy playoff schedule", "bold", 46, V.INK, t=t * 4)
        f.text((LEFT, 530), "Defense rank so far this season (1st = most generous)", "regular", 30, V.INK_MUTED,
               t=t * 4 - 0.2)
        y = 600
        for i, s in enumerate(x for x in p["schedule"] if x["playoff"]):
            rt = t * 3.5 - 0.4 - i * 0.45
            f.rect((LEFT, y, RIGHT - 60, y + 210), V.SURFACE, outline=V.LINE, width=2, t=rt)
            f.text((LEFT + 36, y + 30), f"WEEK {s['week']}", "bold", 30, V.ACCENT, t=rt)
            if s["opponent_value"] == "bye":
                f.text((LEFT + 36, y + 90), "Bye", "black", 64, V.INK_FAINT, t=rt)
            else:
                prefix = "vs" if s.get("home") else "at"
                f.text((LEFT + 36, y + 90), f"{prefix} {TEAM_NAMES[s['opponent_value']]}", "black", 60, V.INK, t=rt)
                g = band(s["rank_value"])
                f.text((RIGHT - 96, y + 70), render("ordinal", s["rank_value"])[0], "black", 70, V.BAND_COLOR[g],
                       "ra", t=rt)
                f.text((RIGHT - 96, y + 150), g, "semibold", 28, V.BAND_COLOR[g], "ra", t=rt)
            y += 240

    def follow_card(self, f: Frame, t: float, shot: dict):
        m = self.p["metrics"][0]
        f.text((W / 2 - 40, 420), "THE USAGE REPORT", "black", 92, V.INK, "ma", t=t * 4)
        f.text((W / 2 - 40, 560), "Who gained a role. Who lost one.", "semibold", 44, V.INK_MUTED, "ma", t=t * 4 - 0.3)
        f.rect((LEFT + 40, 700, RIGHT - 100, 860), V.ACCENT, radius=24, t=t * 4 - 0.6)
        f.text((W / 2 - 40, 742), "Follow for every week", "black", 52, "#08111d", "ma", t=t * 4 - 0.6, rise=0)
        recap = f"{self.p['name']}: {self.d(m['baseline'])} → {self.d(m['recent'])} {m['label']}"
        f.text((W / 2 - 40, 960), recap, "semibold", f.fit(recap, "semibold", 38, RIGHT - LEFT - 40), V.INK_FAINT,
               "ma", t=t * 4 - 1.0)


# --------------------------------------------------------------------------------------- captions

def caption_chunks_short(template: str, claims: dict) -> list[dict]:
    """Caption cards that follow the speech: break at sentence ends and clause marks (, : ;), and split a
    clause too long for two lines into balanced halves rather than at an arbitrary word.

    Chunking works on DISPLAYED words: a single placeholder can expand to a long phrase ("two
    middle-of-the-pack matchups and one tough one") that must be splittable like any other text. Each displayed
    word carries its share of the spoken words, which is what the caption timing runs on."""
    font = F("black", CAPTION["font_size"])
    tokens: list[tuple[str, float]] = []
    for w in template.split():
        shown = fill(w, claims, "display").split()
        spoken = len(fill(w, claims, "spoken").split())
        tokens += [(d, spoken / len(shown)) for d in shown]

    def text(ts):
        return " ".join(d for d, _ in ts)

    def fits(ts) -> bool:
        return len(_wrap_px(text(ts), font, CAPTION["max_width_px"])) <= CAPTION["max_lines"] and len(ts) <= 9

    def split(ts):
        if fits(ts) or len(ts) == 1:
            return [ts]
        mid = len(ts) // 2
        return split(ts[:mid]) + split(ts[mid:])

    clauses, cur = [], []
    for tok in tokens:
        cur.append(tok)
        if re.search(r"[.!?:;,]['\"”’]?$", tok[0]):
            clauses.append(cur)
            cur = []
    if cur:
        clauses.append(cur)
    chunks: list[list[tuple[str, float]]] = []
    for c in clauses:
        if chunks and len(chunks[-1]) <= 3 and fits(chunks[-1] + c):   # "One more thing:" joins what follows
            chunks[-1] = chunks[-1] + c
        else:
            chunks.extend(split(c))
    # A one- or two-word leftover ("either.") never stands alone: it joins its neighbour when that fits.
    merged: list[list[tuple[str, float]]] = []
    for c in chunks:
        if merged and len(c) <= 2 and fits(merged[-1] + c):
            merged[-1] = merged[-1] + c
        else:
            merged.append(c)
    if len(merged) > 1 and len(merged[0]) <= 2 and fits(merged[0] + merged[1]):
        merged[:2] = [merged[0] + merged[1]]
    return [{"display": text(c), "spoken_words": sum(wt for _, wt in c)} for c in merged]


@lru_cache(maxsize=512)
def caption_layer(text: str) -> Image.Image:
    font = F("black", CAPTION["font_size"])
    lines = _wrap_px(text, font, CAPTION["max_width_px"])
    layer = Image.new("RGBA", (W, 260), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    lh = int(CAPTION["font_size"] * 1.18)
    y0 = 130 - lh * len(lines) / 2
    for i, line in enumerate(lines):
        d.text((W / 2 - 40, y0 + i * lh + lh / 2), line, font=font, fill=V.INK, anchor="mm",
               stroke_width=7, stroke_fill=(5, 7, 10))
    return layer


# ------------------------------------------------------------------------------------------- run

def _run(ep: Episode) -> None:
    script = read_json(ep.path("script.json"))
    timings = {s["id"]: s for s in read_json(ep.path("audio/timings.json"))["segments"]}
    cards = ShortCards(ep)
    claims = cards.claims
    fps = ep.cfg["video"]["fps"]
    tail = ep.cfg["tts"]["segment_tail_silence"]

    beats, cues, offset, audio = [], [], 0.0, []
    for seg, shot in zip(script["narration_segments"], script["shots"]):
        tm = timings[seg["id"]]
        chunks = caption_chunks_short(seg["text_template"], claims)
        time_chunks(chunks, tm["words"], tm["duration"], tail)
        beats.append({"start": offset, "end": offset + tm["duration"], "shot": shot})
        cues += [{"segment": seg["id"], "start": round(offset + c["start"], 3), "end": round(offset + c["end"], 3),
                  "text": c["display"]} for c in chunks]
        data, sr = sf.read(ep.path(f"audio/{seg['id']}.wav"), dtype="float32")
        audio.append(data)
        offset += tm["duration"]
    work = ep.path("work")
    work.mkdir(parents=True, exist_ok=True)
    sf.write(work / "narration.wav", np.concatenate(audio), sr, subtype="PCM_16")

    frames = int(math.ceil(offset * fps))
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
         "-i", str(work / "narration.wav"),
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
         "-g", str(fps * 2), "-af", LOUDNESS, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
         "-frames:v", str(frames), "-movflags", "+faststart", str(ep.path("final.mp4"))],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    ci = 0
    for n in range(frames):
        t = n / fps
        beat = next((b for b in beats if b["start"] <= t < b["end"]), beats[-1])
        f = Frame()
        getattr(cards, beat["shot"]["type"])(f, t - beat["start"], beat["shot"])
        while ci + 1 < len(cues) and cues[ci + 1]["start"] <= t:
            ci += 1
        cue = cues[ci]
        if cue["start"] <= t < cue["end"]:
            f.img.paste(caption_layer(cue["text"]), (0, CAPTION["center_y"] - 130), caption_layer(cue["text"]))
        proc.stdin.write(f.img.tobytes())
    proc.stdin.close()
    if proc.wait():
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.read().decode()[-1500:]}")

    ep.path("captions.srt").write_text("\n".join(
        f"{i}\n{srt_time(c['start'])} --> {srt_time(c['end'])}\n{c['text']}\n" for i, c in enumerate(cues, 1)))
    write_json(ep.path("captions.json"), {**CAPTION, "cues": cues})
    ep.log(f"  render: final.mp4, {offset:.1f}s, {frames} frames, {len(cues)} captions")


def run(ep: Episode) -> None:
    run_stage(ep, "render", VERSION, {"video": ep.cfg["video"], "caption": CAPTION},
              ["script.json", "facts.json", "audio"], ["final.mp4", "captions.srt", "captions.json"],
              lambda: _run(ep))
