"""Stage 5 (Shorts): motion-designed 1080x1920 video with designed audio.

Each beat of the story is a scene with its own composition and background (turf, team colours, studio).
Animation is keyed to the narration: a number lands on screen as it is spoken, a stamp hits on the word it
illustrates, each name appears as it is said. Scenes push in slowly, change with a short push transition and a
whoosh, and captions show two-to-three-word phrases with the spoken word lit.

Audio: the narration beats (trimmed, tightly spaced), an original procedural music bed ducked under the
voice, and sound effects at transitions and reveals (studio/audio_design.py), loudness-normalised by FFmpeg.

Every number drawn comes from a claim display string or a snapshot value through numbers.render.
"""

from __future__ import annotations

import math
import re
import subprocess

import numpy as np
import soundfile as sf
from PIL import Image, ImageDraw, ImageFilter

from .. import audio_design as A
from .. import flame_scenes as FPS
from .. import format_scenes as FS
from .. import headshots
from .. import motion as M
from .. import scenes as S
from ..core import Episode, read_json, run_stage, write_json
from ..numbers import TEAM_NAMES, render
from .assemble import srt_time
from .script import PLACEHOLDER, fill
from .story import last

VERSION = 18
TRANSITION = 0.2
CAPTION = {"font": "Inter-Black.ttf", "font_size": M.CAPTION_FONT[1], "max_width_px": M.CAPTION_MAX_W,
           "max_lines": 2}
LOUDNESS = "loudnorm=I=-14:TP=-1.5:LRA=11"


# --------------------------------------------------------------------------------- word alignment

def align(seg: dict, words: list[dict], claims: dict) -> list[dict]:
    """Display tokens of one beat with start/end times, the claim each came from, and emphasis.

    Kokoro's word timestamps line up one-to-one with the spoken words when no multi-word pronunciation
    override is used; if the counts ever differ, times fall back to proportional positions."""
    spoken_tokens = [w for w in words if re.search(r"[A-Za-z0-9]", w["text"])]
    units = []
    for tt in seg["text_template"].split():
        m = PLACEHOLDER.search(tt)
        units.append({"display": fill(tt, claims, "display").split(), "n": len(fill(tt, claims, "spoken").split()),
                      "claim": m.group(1) if m else None})
    total = sum(u["n"] for u in units) or 1
    exact = len(spoken_tokens) == total
    emph = {e.lower() for e in seg.get("emphasis", [])}
    out, k = [], 0
    end_all = spoken_tokens[-1]["end"] if spoken_tokens else 0.0
    for u in units:
        if exact:
            span = spoken_tokens[k:k + u["n"]]
            start, end = span[0]["start"], span[-1]["end"]
        else:
            a, b = k / total, (k + u["n"]) / total
            start, end = a * end_all, b * end_all
        k += u["n"]
        n = len(u["display"])
        for i, d in enumerate(u["display"]):
            s0 = start + (end - start) * i / n
            out.append({"text": d, "start": round(s0, 3), "end": round(start + (end - start) * (i + 1) / n, 3),
                        "claim": u["claim"],
                        "emph": bool(re.search(r"\d", d)) or re.sub(r"[^a-z']", "", d.lower()) in emph})
    return out


def phrases(tokens: list[dict], max_words: int = 3) -> list[list[dict]]:
    """Caption cards of up to three words, breaking after punctuation, and never splitting one claim's words
    ("per the Week 6 injury report") or a "from X to Y" range across cards when they fit on one (up to five
    words)."""
    out, cur = [], []
    for i, tok in enumerate(tokens):
        cur.append(tok)
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        same_claim = nxt is not None and tok["claim"] and nxt["claim"] == tok["claim"] and len(cur) < 5
        # "23% down to 12%" stays on one card: alone, "THAT'S 23% DOWN" reads as a 23-point drop
        nxt2 = tokens[i + 2] if i + 2 < len(tokens) else None
        to_range = len(cur) < 5 and nxt is not None and (
            (nxt["text"].lower() == "to" and nxt2 is not None and nxt2["claim"])
            or (tok["text"].lower() == "to" and nxt["claim"]))
        if (len(cur) >= max_words and not (same_claim or to_range)) or re.search(r"[.,!?:;]$", tok["text"]):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    # A lone word ("field.", "part:") joins the card before it unless that card ends a sentence.
    merged: list[list[dict]] = []
    for ph in out:
        if merged and len(ph) == 1 and len(merged[-1]) <= 3 and not re.search(r"[.!?]$", merged[-1][-1]["text"]):
            merged[-1] = merged[-1] + ph
        else:
            merged.append(ph)
    return merged


# ------------------------------------------------------------------------------------------ scenes

class Ctx:
    def __init__(self, facts: dict, claims: dict, accent, story: str):
        self.facts, self.claims = facts, claims
        self.p = facts["players"][0]
        self.team = self.p["team"]
        self.accent = accent
        self.story = story
        self.primary, self.secondary = M.team_colors(self.team)
        self.primary_vivid = M.vivid(self.primary)
        self.tokens: list[dict] = []
        self.headshot = None
        self.headshots_on = False

    def art(self, width: int):
        """The player's picture: his headshot (when enabled and available) or the jersey graphic."""
        return M.player_art(self.team, str(self.p.get("jersey") or ""), last(self.p["name"]), self.headshot, width)

    def d(self, cid: str) -> str:
        return self.claims[cid]["display"]

    def at(self, claim: str | None = None, word: str | None = None, default: float = 0.0) -> float:
        for tok in self.tokens:
            if claim and tok["claim"] == claim:
                return tok["start"]
            if word and re.sub(r"[^a-z]", "", tok["text"].lower()).startswith(word.lower()):
                return tok["start"]
        return default


def chip(img, text, cx, cy, fill, fg=M.DARK, size=40, alpha=1.0, scale=1.0):
    f = M.font("black", size)
    w = f.getlength(text) + 56
    h = size + 34
    lay = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=fill)
    d.text((w / 2, h / 2), text, font=f, fill=fg, anchor="mm")
    M.paste(img, lay, cx, cy, scale, alpha)


def scene_hook(img, t, dur, spec, c: Ctx):
    """Frame 0 already shows the player and the question. On the turn of the hook (the second sentence,
    e.g. "So where..."), the headline punches and the jersey steps back: a second visual beat."""
    p = c.p
    lines = spec.get("headline") or [p["name"].upper()]
    turn = next((tok["start"] for i, tok in enumerate(c.tokens) if i > 0 and re.search(r"[.?!]$", c.tokens[i - 1]["text"])),
                dur * 0.5)
    punch = M.progress(t, turn - 0.05, 0.35)
    pulse = 1 + 0.12 * math.sin(math.pi * punch) if 0 < punch < 1 else 1.0
    for i, line in enumerate(lines):
        k = M.back_out(M.progress(t + 0.25, i * 0.18, 0.32))     # already landing on frame 0
        color = M.GOLD if i == len(lines) - 1 else M.INK
        size = M.fit_size(line, "black", 150 if i == len(lines) - 1 else 104, M.SAFE_RIGHT - M.SAFE_LEFT)
        grow = 1 + 0.18 * M.ease_out(punch) if i == len(lines) - 1 else 1.0
        M.put_text(img, line, M.CENTER_X, 330 + i * 140 + 30 * M.ease_out(punch) * i, size=size, fill=color,
                   scale=(0.6 + 0.4 * k) * grow * (pulse if i == len(lines) - 1 else 1), alpha=M.clamp(k * 1.4))
    j = c.art(660)
    k = M.back_out(M.progress(t + 0.15, 0.0, 0.45))
    back = M.ease_in_out(punch)
    M.paste(img, j, M.CENTER_X + 40 * back, 920 + (1 - k) * 380 + 90 * back + M.wobble(t, 4),
            (0.92 + 0.08 * k) * (1 - 0.18 * back), M.clamp(k * 2) * (1 - 0.35 * back))
    chip(img, f"{p['position']}  ·  {c.d(p['team_name']).upper()}", M.CENTER_X, 1330, M.INK, size=38,
         alpha=M.clamp((t - 0.35) * 4) * (1 - back))


def scene_big_compare(img, t, dur, spec, c: Ctx):
    m = c.p["metrics"][spec.get("metric", 0)]
    t_before = c.at(m["baseline"], default=0.2)
    t_after = c.at(m["recent"], default=dur * 0.5)
    M.put_text(img, m["label"].upper(), M.CENTER_X, 340, size=78, fill=M.INK, alpha=M.clamp(t * 6))
    kb = M.back_out(M.progress(t, t_before - 0.1, 0.3))
    ka = M.back_out(M.progress(t, t_after - 0.08, 0.32))
    dim = 1 - 0.45 * M.clamp((t - t_after) * 5)
    M.put_text(img, c.d(m["baseline"]), M.CENTER_X, 640 - 40 * M.clamp((t - t_after) * 5), size=250,
               fill=M.mix_color(M.INK, M.MUTED, 1 - dim), scale=(0.5 + 0.5 * kb) * (0.85 + 0.15 * dim),
               alpha=M.clamp(kb * 2))
    down = c.p["role"] == "faller"
    if t >= t_after - 0.2:
        d = ImageDraw.Draw(img)
        ka2 = M.ease_out(M.progress(t, t_after - 0.2, 0.25))
        M.arrow(d, M.CENTER_X, 860, 120 * ka2 + 1, c.accent, down)
    M.put_text(img, c.d(m["recent"]), M.CENTER_X, 1110, size=330, fill=c.accent, scale=0.4 + 0.6 * ka,
               alpha=M.clamp(ka * 2))
    chip(img, c.d(m["change"]), M.CENTER_X, 1330, c.accent, size=44, alpha=M.clamp((t - t_after - 0.25) * 4))


def scene_bar_timeline(img, t, dur, spec, c: Ctx):
    p = c.p
    m = p["metrics"][spec.get("metric", 0)]
    M.put_text(img, m["label"].upper(), M.CENTER_X, 320, size=96, fill=M.INK, alpha=M.clamp(t * 6), max_width=880)
    M.put_text(img, "GAME BY GAME", M.CENTER_X, 420, size=46, fill=M.MUTED, alpha=M.clamp(t * 6))
    series = m["series"]
    recent = {v["week"]: v["value"] for v in p.get("recent_values", [])}
    top = max([s["value"] or 0 for s in series] + [0.05])
    x0, x1, base, hmax = M.SAFE_LEFT + 20, M.SAFE_RIGHT - 20, 1220, 640
    n = len(series)
    gap = 28
    bw = (x1 - x0 - gap * (n - 1)) / n
    d = ImageDraw.Draw(img)
    for i, s in enumerate(series):
        grow = M.ease_out(M.progress(t, 0.05 + i * 0.07, 0.3))
        val = max(s["value"] or 0, 0)
        h = hmax * val / top * grow
        x = x0 + i * (bw + gap)
        hot = s["week"] in recent
        lit = hot and t >= c.at(recent[s["week"]], default=99) - 0.05
        col = c.accent if lit else ((90, 100, 116) if hot else (58, 66, 80))
        if h > 2:
            d.rounded_rectangle((x, base - h, x + bw, base), radius=14, fill=col)
        label = render("pct", s["value"])[0] if s["value"] is not None else "—"
        pop = M.back_out(M.progress(t, c.at(recent.get(s["week"]), default=99) - 0.05, 0.25)) if hot else 1.0
        size = 72 if lit else 46
        M.put_text(img, label, x + bw / 2, base - h - 50, size=size, fill=M.INK if not lit else c.accent,
                   alpha=M.clamp(grow * 1.5), scale=0.7 + 0.3 * pop if lit else 1.0)
        M.put_text(img, f"WK {s['week']}", x + bw / 2, base + 46, size=34, fill=M.MUTED, alpha=M.clamp(grow * 2))
    mean_b = c.claims[m["baseline"]]["value"]
    y = base - hmax * mean_b / top
    k = M.ease_out(M.progress(t, 0.6, 0.4))
    for xx in range(int(x0), int(x0 + (x1 - x0) * k), 34):
        d.line((xx, y, xx + 18, y), fill=M.INK, width=4)
    chip(img, f"AVERAGE BEFORE: {c.d(m['baseline'])}", M.CENTER_X, 1350, M.INK, size=40, alpha=k)


def _hbar(d, x, y, w, h, frac, color):
    d.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=(36, 42, 54))
    if frac > 0.002:
        d.rounded_rectangle((x, y, x + max(h, w * frac), y + h), radius=h // 2, fill=color)


def scene_twist(img, t, dur, spec, c: Ctx):
    """Two metrics, big: the one that barely moved, and the one that fell (or rose)."""
    p = c.p
    other = next((m for m in p["metrics"] if m["column"] == spec.get("snaps")), p["metrics"][-1])
    lead = p["metrics"][0]
    d = ImageDraw.Draw(img)
    rows = [(other, 330, M.INK), (lead, 940, c.accent)]
    for r, (m, y, col) in enumerate(rows):
        kr = M.ease_out(M.progress(t, 0.05 + r * 0.3, 0.35))
        if kr <= 0:
            continue
        M.put_text(img, m["label"].upper(), M.SAFE_LEFT, y, size=76, fill=col, alpha=kr, anchor="l")
        if m.get("weekly"):     # an up-and-down metric is shown game by game, as the narration says it
            pairs = [(f"WK {w}", cid, col) for w, cid in zip(p["recent_weeks"], m["weekly"])]
        else:
            pairs = [("BEFORE", m["baseline"], (120, 130, 146)), ("NOW", m["recent"], col)]
        for j, (lab, cid, bc) in enumerate(pairs):
            kj = kr * (M.ease_out(M.progress(t, c.at(claim=cid, default=0.0) - 0.1, 0.3)) if m.get("weekly") else 1)
            if kj <= 0:
                continue
            yy = y + 100 + j * 130
            M.put_text(img, lab, M.SAFE_LEFT, yy + 40, size=40, fill=M.MUTED, alpha=kj, anchor="l")
            _hbar(d, M.SAFE_LEFT + 200, yy + 10, 470 * kj, 64, c.claims[cid]["value"], bc)
            M.put_text(img, c.d(cid), M.SAFE_LEFT + 700, yy + 42, size=78, fill=M.INK, alpha=kj, anchor="l")
    if c.story == "vanishing_targets":
        ts = c.at(word="barely", default=dur * 0.6)
        ks = M.back_out(M.progress(t, ts - 0.05, 0.3))
        if ks > 0:
            stamp = M.text_layer("BARELY MOVED", "black", 100, M.GOLD, stroke=8)
            M.paste(img, stamp.rotate(5, expand=True, resample=Image.BICUBIC), M.CENTER_X, 770,
                    1.4 - 0.4 * ks, M.clamp(ks * 2))


def scene_spread(img, t, dur, spec, c: Ctx):
    gainers = [g for g in c.p.get("gainers", []) if g["gsis_id"] in spec.get("gainers", [])]
    gainers.sort(key=lambda g: spec["gainers"].index(g["gsis_id"]))
    M.put_text(img, "WHERE THEY WENT", M.CENTER_X, 330, size=96, fill=M.GOLD, alpha=M.clamp(t * 6))
    for i, g in enumerate(gainers):
        ta = c.at(word=last(g["name"]), default=0.3 + i * 0.5)
        k = M.back_out(M.progress(t, ta - 0.1, 0.3))
        if k <= 0:
            continue
        y = 520 + i * 250
        off = (1 - k) * 160
        box = Image.new("RGBA", (880, 210), (0, 0, 0, 0))
        bd = ImageDraw.Draw(box)
        bd.rounded_rectangle((0, 0, 879, 209), radius=28, fill=(16, 22, 30, 235), outline=(255, 255, 255, 40), width=3)
        name = g["name"].upper()
        bd.text((40, 70), name, font=M.font("black", M.fit_size(name, "black", 56, 820)), fill=M.INK, anchor="lm")
        bd.text((40, 150), g["position"], font=M.font("bold", 40), fill=M.MUTED, anchor="lm")
        bd.text((840, 150), f"{c.d(g['baseline'])} → {c.d(g['recent'])}", font=M.font("black", 64), fill=M.UP,
                anchor="rm")
        M.paste(img, box, M.CENTER_X + off, y + 105, 1.0, M.clamp(k * 2))


def scene_statement(img, t, dur, spec, c: Ctx):
    lines = spec.get("lines", [])
    j = c.art(520)
    M.paste(img, j, M.CENTER_X + 170, 1130 + M.wobble(t, 3), 1.0, 0.35)
    for i, line in enumerate(lines):
        k = M.back_out(M.progress(t, 0.05 + i * 0.16, 0.3))
        col = M.GOLD if (i == len(lines) - 1 or (len(lines) == 4 and i == 3)) else M.INK
        size = M.fit_size(line, "black", 150, M.SAFE_RIGHT - M.SAFE_LEFT)
        M.put_text(img, line, M.SAFE_LEFT, 400 + i * 165, size=size, fill=col, scale=0.7 + 0.3 * k,
                   alpha=M.clamp(k * 2), anchor="l")


def scene_decision(img, t, dur, spec, c: Ctx):
    M.put_text(img, "THE USAGE REPORT", M.SAFE_LEFT, 300, size=34, fill=M.MUTED, alpha=0.8, anchor="l")
    q = spec.get("question", ["?"])
    for i, line in enumerate(q):
        k = M.back_out(M.progress(t, i * 0.15, 0.3))
        size = M.fit_size(line, "black", 130 if i else 110, M.SAFE_RIGHT - M.SAFE_LEFT)
        M.put_text(img, line, M.CENTER_X, 560 + i * 160, size=size, fill=M.GOLD if i == len(q) - 1 else M.INK,
                   scale=0.6 + 0.4 * k, alpha=M.clamp(k * 2))
    options = spec.get("options", ["START", "SIT"])
    for i, (label, col) in enumerate(zip(options, (M.UP, M.DOWN))):
        k = M.back_out(M.progress(t, 0.35 + i * 0.12, 0.3))
        pulse = 1 + 0.03 * math.sin(t * 6 + i)
        box = Image.new("RGBA", (380, 150), (0, 0, 0, 0))
        bd = ImageDraw.Draw(box)
        bd.rounded_rectangle((4, 4, 375, 145), radius=36, fill=col + (40,), outline=col, width=8)
        bd.text((190, 75), label, font=M.font("black", M.fit_size(label, "black", 72, 320)), fill=M.INK, anchor="mm")
        M.paste(img, box, M.SAFE_LEFT + 200 + i * 470, 1010, (0.6 + 0.4 * k) * pulse, M.clamp(k * 2))


def scene_teammate_out(img, t, dur, spec, c: Ctx):
    p, tm = c.p, c.p["teammate"]
    m = p["metrics"][0]
    tc = t + 5 if spec.get("phase") == 2 else t
    M.put_text(img, "WHY THE ROLE OPENED UP", M.CENTER_X, 330, size=64, fill=M.GOLD, alpha=M.clamp(tc * 6),
               max_width=880)
    weeks = [s["week"] for s in m["series"]]
    missed = set(tm["missed_values"])
    tm_series = {x["week"]: x["value"] for x in tm["series"]}
    me = {s["week"]: s["value"] for s in m["series"]}
    n = len(weeks)
    gap = 16
    cw = (M.SAFE_RIGHT - M.SAFE_LEFT - gap * (n - 1)) / n
    for r, (name, series, is_tm) in enumerate(((tm["name"], tm_series, True), (p["name"], me, False))):
        y = 470 + r * 400
        kr = M.ease_out(M.progress(tc, r * 0.3, 0.3))
        M.put_text(img, name.upper(), M.SAFE_LEFT, y, size=M.fit_size(name.upper(), "black", 58, 860), fill=M.INK,
                   alpha=kr, anchor="l")
        label = tm["column"].replace("_", " ") if is_tm else m["label"]
        M.put_text(img, label.upper(), M.SAFE_LEFT, y + 62, size=32, fill=M.MUTED, alpha=kr, anchor="l")
        d = ImageDraw.Draw(img)
        for i, w in enumerate(weeks):
            k = M.back_out(M.progress(tc, 0.15 + r * 0.25 + i * 0.1, 0.25))
            if k <= 0:
                continue
            x = M.SAFE_LEFT + i * (cw + gap)
            box = (x, y + 110, x + cw, y + 270)
            if is_tm and w in missed:
                d.rounded_rectangle(box, radius=18, fill=M.DOWN)
                M.put_text(img, "OUT", x + cw / 2, y + 190, size=48, fill=M.DARK, scale=0.7 + 0.3 * k)
            else:
                val = series.get(w)
                hot = (not is_tm and w in p["recent_weeks"]) or (spec.get("phase") == 2 and is_tm and val is not None)
                fill = c.accent if hot else (30, 36, 46)
                d.rounded_rectangle(box, radius=18, fill=fill, outline=(255, 255, 255, 30), width=2)
                txt = render("pct", val)[0] if val is not None else "—"
                M.put_text(img, txt, x + cw / 2, y + 190, size=46, fill=M.DARK if hot else M.INK, scale=0.7 + 0.3 * k)
            M.put_text(img, f"WK {w}", x + cw / 2, y + 305, size=28, fill=M.MUTED, alpha=M.clamp(k))
    if spec.get("phase") == 2:
        chip(img, f"BEFORE: {c.d(tm['role'])}", M.CENTER_X, 1330, c.accent, size=48, alpha=M.clamp(t * 4))
    elif "reason" in tm:
        chip(img, f"OUT: {c.d(tm['reason']).upper()}", M.CENTER_X, 1330, M.DOWN, fg=M.INK, size=40,
             alpha=M.clamp((t - 0.6) * 4))


def scene_status_stamp(img, t, dur, spec, c: Ctx):
    tm = c.p["teammate"]
    code, week, detail = c.claims[tm["status"]]["value"]
    big = {"ir": "INJURED RESERVE", "none": "NOT ON THE REPORT"}.get(code) or (
        f"LISTED {detail.upper()}" if code == "designation" else
        {"Did Not Participate In Practice": "NOT PRACTICING", "Limited Participation in Practice": "LIMITED",
         "Full Participation in Practice": "PRACTICING FULLY"}.get(detail, detail.upper()))
    col = M.DOWN if ("NOT" in big or code in ("ir", "designation")) else M.UP
    M.put_text(img, f"{tm['name'].upper()} RIGHT NOW", M.CENTER_X, 400, size=60, fill=M.INK, alpha=M.clamp(t * 6),
               max_width=880)
    k = M.back_out(M.progress(t, 0.25, 0.3))
    words = big.split()
    halves = [" ".join(words[: max(1, len(words) // 2)]), " ".join(words[max(1, len(words) // 2):])]
    for i, line in enumerate(x for x in halves if x):
        lay = M.text_layer(line, "black", M.fit_size(line, "black", 150, 860), col, stroke=6)
        M.paste(img, lay.rotate(-5, expand=True, resample=Image.BICUBIC), M.CENTER_X, 700 + i * 170,
                1.5 - 0.5 * k, M.clamp(k * 2))
    src = "NFL roster status" if code == "ir" else "Official injury report"
    M.put_text(img, f"{src.upper()} · WEEK {week}", M.CENTER_X, 1080, size=36, fill=M.MUTED,
               alpha=M.clamp((t - 0.6) * 4))


SCENES = {"hook": ("field", scene_hook), "big_compare": ("team", scene_big_compare),
          "bar_timeline": ("studio", scene_bar_timeline), "twist": ("studio", scene_twist),
          "spread": ("field", scene_spread), "statement": ("team", scene_statement),
          "decision": ("studio", scene_decision), "teammate_out": ("studio", scene_teammate_out),
          "status_stamp": ("team", scene_status_stamp)}

SCENES.update(S.STORY_SCENES)
SCENES.update(FS.FORMAT_SCENES)
SCENES.update(FPS.SCENES)
MOOD = {"mystery": "tense", "opportunity": "bright"}

# Sound design per scene: (local time source, kind, gain dB)
def sound_events(scene: dict, start: float, dur: float, c: Ctx) -> list[tuple[float, str, float]]:
    kind = scene["type"]
    ev = []
    if kind == "hook":
        ev.append((start + 0.0, "impact", -2))
    if kind == "big_compare":
        m = c.p["metrics"][scene.get("metric", 0)]
        ev.append((start + c.at(m["recent"], default=dur * 0.5) - 0.05, "impact", 0))
    if kind == "bar_timeline":
        for i in range(len(c.p["metrics"][0]["series"])):
            ev.append((start + 0.05 + i * 0.07, "tick", -8))
        for v in c.p.get("recent_values", []):
            ev.append((start + c.at(v["value"], default=99) - 0.05, "tick", -2))
    if kind == "twist" and c.story == "vanishing_targets":
        ev.append((start + c.at(word="barely", default=dur * 0.6) - 0.05, "impact", -3))
    if kind == "spread":
        for g in c.p.get("gainers", []):
            ev.append((start + c.at(word=last(g["name"]), default=0.3) - 0.1, "tick", -3))
    if kind == "status_stamp":
        ev.append((start + 0.25, "impact", -2))
    if kind == "decision":
        ev.append((start - 0.4, "rise", -6))
    ev += S.sounds(scene, start, dur, c)
    ev += FS.sounds(scene, start, dur, c)
    ev += FPS.sounds(scene, start, dur, c)
    return [e for e in ev if e[0] < start + dur + 1]


# ------------------------------------------------------------------------------------------- render

def _camera(frame: Image.Image, k: float, punch: float = 0.0, mode: str = "push") -> Image.Image:
    """Slow push-in over a scene (up to 3.5%), plus a quick punch-in on a reveal that settles back.
    'still' holds the frame and only snaps in on reveals (the mystery); 'drift' pushes and slides sideways."""
    s = 1 + (0.0 if mode == "still" else 0.035 * k) + punch
    if s <= 1.001:
        return frame
    cw, ch = M.W / s, M.H / s
    x0, y0 = (M.W - cw) / 2, (M.H - ch) * 0.45
    if mode == "drift":
        x0 = (M.W - cw) * (0.5 - 0.35 * (k - 0.5))
    return frame.resize((M.W, M.H), Image.BILINEAR, box=(x0, y0, x0 + cw, y0 + ch))


def draw_scene(scene: dict, t: float, dur: float, c: Ctx) -> Image.Image:
    bg_kind, fn = SCENES[scene["type"]]
    if scene.get("fp"):                       # a flame / praise Short keeps its own look throughout
        img = FPS.background(FPS.backdrop_for(scene), c, t + scene.get("_start", 0))
    elif bg_kind in ("casefile", "alert"):
        img = FS.background(bg_kind, c, t + scene.get("_start", 0))
    else:
        img = M.background(bg_kind, c.team, c.accent, t + scene.get("_start", 0))
    fn(img, t, dur, scene, c)
    hits = S.punches(scene, c, dur) + FS.punches(scene, c, dur) + FPS.punches(scene, c, dur)
    strength = 0.06 if scene.get("camera") == "still" else 0.045
    punch = sum(strength * math.exp(-(t - tp) / 0.16) for tp in hits if t >= tp)
    return _camera(img, min(t / max(dur, 0.1), 1), punch, scene.get("camera", "push"))


def _run(ep: Episode) -> None:
    script = read_json(ep.path("script.json"))
    facts = read_json(ep.path("facts.json"))
    claims = {cl["id"]: cl for cl in facts["claims"]}
    timings = {s["id"]: s for s in read_json(ep.path("audio/timings.json"))["segments"]}
    fps = ep.cfg["video"]["fps"]
    story = script["structure"]["angle"]
    p = facts["players"][0]
    accent = FPS.ACCENT[story] if story in FPS.ACCENT else M.GOLD if story in ("fill_in", "mystery", "opportunity") else (M.DOWN if p["role"] == "faller" else M.UP)
    ctx = Ctx(facts, claims, accent, story)
    ctx.headshot = headshots.for_player(ep.cfg, p)
    ctx.headshots_on = headshots.enabled(ep.cfg)

    scenes, cues, voice, offset, events = [], [], [], 0.0, []
    sr = None
    for seg, shot in zip(script["narration_segments"], script["shots"]):
        tm = timings[seg["id"]]
        toks = align(seg, tm["words"], claims)
        scenes.append({**shot, "_start": offset, "_dur": tm["duration"], "_tokens": toks})
        for ph in phrases(toks):
            cues.append({"segment": seg["id"], "words": ph, "offset": offset, "start": offset + ph[0]["start"],
                         "text": " ".join(w["text"] for w in ph)})
        data, sr = sf.read(ep.path(f"audio/{seg['id']}.wav"), dtype="float32")
        voice.append(data)
        offset += tm["duration"]
    total = offset
    for i, cue in enumerate(cues):
        cue["end"] = cues[i + 1]["start"] if i + 1 < len(cues) else total
    for i, sc in enumerate(scenes):
        ctx.tokens = sc["_tokens"]
        tr = sc.get("transition", "push")
        # A transition sound only where the edit is a moment (format scenes opt in with "sfx"); a plain scene
        # change is silent, so the whoosh never becomes wallpaper.
        loud = sc.get("sfx", sc["type"] not in FS.FORMAT_SCENES)
        if i > 0 and loud and tr in ("push", "whip", "rewind"):
            events.append((sc["_start"] - 0.12, "whoosh", -6))
        elif i > 0 and loud and tr == "cut":
            events.append((sc["_start"] - 0.01, "cut", -7))
        events += sound_events(sc, sc["_start"], sc["_dur"], ctx)

    sh = ep.cfg["shorts"]
    if script["structure"].get("series"):       # the flame / praise series: its format's mood, silences, sounds
        from .. import series_audio as SA
        mixed = SA.mix(A.upsample(np.concatenate(voice), sr), events, script["structure"], scenes, sh)
    else:
        mixed = A.mix(A.upsample(np.concatenate(voice), sr), events, sh.get("music", True), sh.get("music_db", -22),
                      sh.get("sfx_db", -12), MOOD.get(story, FPS.MOOD.get(story, "drive")))
    work = ep.path("work")
    work.mkdir(parents=True, exist_ok=True)
    sf.write(work / "mix.wav", mixed, A.SR, subtype="PCM_16")

    frames = int(math.ceil(total * fps))
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{M.W}x{M.H}", "-r", str(fps), "-i", "-",
         "-i", str(work / "mix.wav"), "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-g", str(fps * 2),
         "-af", LOUDNESS, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
         "-frames:v", str(frames), "-movflags", "+faststart", str(ep.path("final.mp4"))],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    prev_last: Image.Image | None = None
    cur = -1
    ci = 0
    for n in range(frames):
        t = n / fps
        i = max(j for j, sc in enumerate(scenes) if sc["_start"] <= t + 1e-9)
        sc = scenes[i]
        if i != cur:
            if cur >= 0:
                ctx.tokens = scenes[cur]["_tokens"]
                prev_last = draw_scene(scenes[cur], scenes[cur]["_dur"], scenes[cur]["_dur"], ctx)
            cur = i
        ctx.tokens = sc["_tokens"]
        tl = t - sc["_start"]
        frame = draw_scene(sc, tl, sc["_dur"], ctx)
        tr = sc.get("transition", "push")
        if prev_last is not None and tr == "cut" and tl < 0.067:
            # A hard cut with a two-frame flash: the mystery's evidence slams onto the table.
            frame = Image.blend(frame, Image.new("RGB", frame.size, (255, 255, 255)), 0.3 * (1 - tl / 0.067))
        elif prev_last is not None and tr == "zoom" and tl < 0.2:
            # Punch in: the old frame rushes past the camera while the new one settles from slightly close.
            k = M.ease_out(tl / 0.2)
            s_old = 1 + 0.35 * k
            cw, ch = M.W / s_old, M.H / s_old
            old = prev_last.resize((M.W, M.H), Image.BILINEAR,
                                   box=((M.W - cw) / 2, (M.H - ch) / 2, (M.W + cw) / 2, (M.H + ch) / 2))
            frame = Image.blend(old, frame, M.clamp(k * 1.2))
        elif prev_last is not None and tr in ("whip", "rewind") and tl < TRANSITION:
            # A whip pan: the old scene leaves sideways and the new one arrives smeared, then sharp.
            k = M.ease_out(tl / TRANSITION)
            sign = 1 if tr == "whip" else -1
            canvas = Image.new("RGB", frame.size, M.DARK)
            canvas.paste(prev_last, (int(-sign * M.W * k), 0))
            canvas.paste(frame, (int(sign * M.W * (1 - k)), 0))
            if k < 0.85:
                canvas = canvas.filter(ImageFilter.BoxBlur(int(18 * (1 - k)) + 1)) if (1 - k) > 0.2 else canvas
            if tr == "rewind" and k < 0.9:
                d = ImageDraw.Draw(canvas, "RGBA")
                for yy in range(0, M.H, 12):
                    d.line((0, yy, M.W, yy), fill=(255, 255, 255, int(28 * (1 - k))))
            frame = canvas
        elif prev_last is not None and tr == "push" and tl < TRANSITION:
            # Push with a short motion blur: the incoming scene slides up over the outgoing one.
            k = M.ease_out(tl / TRANSITION)
            out = Image.blend(prev_last, Image.new("RGB", prev_last.size, M.DARK), 0.45 * k)
            off = (1 - k) * 300
            blur = None
            for j, o in enumerate((off, off + 40 * (1 - k), off + 80 * (1 - k))):
                shifted = Image.new("RGB", frame.size, M.DARK)
                shifted.paste(frame, (0, int(o)))
                blur = shifted if blur is None else Image.blend(blur, shifted, 1 / (j + 1))
            frame = Image.blend(out, blur, M.clamp(k * 1.3))
        while ci + 1 < len(cues) and cues[ci + 1]["start"] <= t:
            ci += 1
        cue = cues[ci]
        if cue["start"] <= t < cue["end"]:
            active = max((j for j, w in enumerate(cue["words"]) if cue["offset"] + w["start"] <= t), default=0)
            layer = M.caption_frame(cue["words"], active, accent, t - cue["start"])
            frame.paste(layer, (0, M.CAPTION_Y - 165), layer)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait():
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.read().decode()[-1500:]}")

    flat = [{"start": round(cq["start"], 3), "end": round(cq["end"], 3), "text": cq["text"],
             "segment": cq["segment"]} for cq in cues]
    ep.path("captions.srt").write_text("\n".join(
        f"{i}\n{srt_time(cq['start'])} --> {srt_time(cq['end'])}\n{cq['text']}\n" for i, cq in enumerate(flat, 1)))
    write_json(ep.path("captions.json"), {**CAPTION, "cues": flat})
    write_json(ep.path("work/timeline.json"), {"scenes": [{k: v for k, v in sc.items() if k != "_tokens"} for sc in scenes],
                                               "sound_events": events, "duration": total})
    ep.log(f"  render: final.mp4, {total:.1f}s, {frames} frames, {len(scenes)} scenes, {len(flat)} captions, "
           f"{len(events)} sound events")


def run(ep: Episode) -> None:
    shot = headshots.record(ep.cfg, read_json(ep.path("facts.json"))["players"][0])
    from pathlib import Path
    from ..core import sha256_file
    from .. import series_audio as SA, series_scenes as SS
    code = {m.__name__: sha256_file(Path(m.__file__)) for m in (FS, S, M, A, FPS, SS, SA)}
    run_stage(ep, "render", VERSION, {"video": ep.cfg["video"], "shorts": ep.cfg["shorts"], "caption": CAPTION,
                                      "code": code,
                                      "headshot": shot and shot["sha256"]},
              ["script.json", "facts.json", "audio", "story.json"], ["final.mp4", "captions.srt", "captions.json"],
              lambda: _run(ep))
