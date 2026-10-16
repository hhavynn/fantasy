"""The editorial_media treatment: an existing Short re-edited with real photography, B-roll and its own graphics.

Optional and experimental. It takes a finished episode (its script, claims, word timings and designed mix: the
same voice, the same jokes, the same numbers) and writes a NEW folder beside it; the original is only read.

  1. storyboard   shot_planner picks the media for every clause; storyboard.json / storyboard.md and a preview
                  sheet (storyboard.png: one real frame per shot with its narration, media, reason, crop,
                  transition and overlay) are written first. --preview stops here: review before rendering.
  2. render       media_scenes draws each shot; captions sit in whichever zone the player covers least; the
                  original mix is reused, so every sound still lands on its word
  3. checks       media_qc (identity, team and season, context, licences and review, crops, captions, variety,
                  duplication, numbers, output) and the visual audit; media_provenance.json lists every file
                  used with its licence and attribution

Usage (from fantasy-intel/studio):
  python -m studio.stages.editorial_media --source episodes/2026-w06-short-praise-puka-nacua-visual --preview
  python -m studio.stages.editorial_media --source <episode dir> [--treatment player_hype] [--out <dir>]
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .. import flame_scenes as FPS
from .. import headshots
from .. import media_library as ML
from .. import media_qc as QC
from .. import media_scenes as MS
from .. import motion as M
from .. import shot_planner as SP
from ..core import Config, read_json, sha256_file, write_json
from . import render_motion as RM

VERSION = 1
COPY = ["script.json", "facts.json", "story.json", "brief.json", "audio", "work/mix.wav", "work/timeline.json"]
TRANS = {"cut": 0.0, "none": 0.0, "flash": 0.1, "dissolve": 0.18, "whip": 0.16}
CAP_Y = {"low": 1430, "top": 405}


def prepare(src: Path, out: Path) -> dict:
    """Copy what the re-edit needs (never move, never write into the source)."""
    src, out = Path(src).resolve(), Path(out).resolve()
    if src == out:
        raise ValueError("the re-edit must go to a new folder")
    out.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for rel in COPY:
        s = src / rel
        if not s.exists():
            continue
        d = out / ("source_timeline.json" if rel == "work/timeline.json" else rel)
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_dir():
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copyfile(s, d)
            hashes[rel] = sha256_file(s)
    meta = {"source": str(src), "source_sha256": hashes, "treatment_version": VERSION}
    write_json(out / "source.json", meta)
    return meta


class Episode:
    """Everything the planner and renderer need from a prepared folder."""

    def __init__(self, d: Path, cfg: Config):
        self.dir, self.cfg = Path(d), cfg
        self.script = read_json(self.dir / "script.json")
        self.facts = read_json(self.dir / "facts.json")
        self.claims = {c["id"]: c for c in self.facts["claims"]}
        tm = {s["id"]: s for s in read_json(self.dir / "audio/timings.json")["segments"]}
        self.story = self.script["structure"]["angle"]
        p = self.facts["players"][0]
        self.accent = FPS.ACCENT.get(self.story) or (M.GOLD if self.story in ("fill_in", "mystery", "opportunity")
                                                     else (M.DOWN if p["role"] == "faller" else M.UP))
        self.ctx = RM.Ctx(self.facts, self.claims, self.accent, self.story)
        self.ctx.headshot = headshots.for_player(cfg, p)
        self.ctx.headshots_on = headshots.enabled(cfg)
        self.tokens, self.durations, self.scenes, self.offsets = {}, {}, {}, {}
        off = 0.0
        for seg, shot in zip(self.script["narration_segments"], self.script["shots"]):
            toks = RM.align(seg, tm[seg["id"]]["words"], self.claims)
            self.tokens[seg["id"]] = toks
            self.durations[seg["id"]] = tm[seg["id"]]["duration"]
            self.scenes[seg["id"]] = {**shot, "_start": off, "_dur": tm[seg["id"]]["duration"], "_tokens": toks}
            self.offsets[seg["id"]] = off
            off += tm[seg["id"]]["duration"]
        self.total = off
        self.season = self.facts.get("season") or cfg.season
        self.id = self.script.get("episode_id") or self.dir.name


def storyboard(ep: Episode, lib: ML.Library, treatment: str, recent: list[str] | None = None) -> dict:
    plan = SP.plan(ep.script, ep.facts, ep.claims, ep.tokens, ep.durations, lib, treatment, season=ep.season,
                   cfg_headshots=headshots.enabled(ep.cfg), episode_id=ep.id, recent_signatures=recent)
    renderer = MS.ShotRenderer(lib, ep.ctx, ep.scenes, SP.TREATMENTS[treatment]["grade"], ep.season)
    for s in plan.shots:
        s["caption_zone"], s["overlay_zone"] = renderer.zones(s)
        if s.get("asset"):
            e, _, win, full = renderer.geometry(s)
            s["crop"]["window"] = [round(v) for v in win]
            s["crop"]["layout"] = "full_bleed" if full else "card_over_blur"
            s["crop"]["upscale"] = round(MS.H / (win[3] - win[1]), 2)
    sb = {"episode": ep.id, "treatment": treatment, "seed": plan.seed, "signature": plan.signature,
          "duration": round(ep.total, 2), "shots": plan.shots, "gaps": plan.gaps,
          "headshot_fallback_enabled": headshots.enabled(ep.cfg)}
    return sb


def storyboard_markdown(sb: dict, lib: ML.Library) -> str:
    out = [f"# Storyboard: {sb['episode']} ({sb['treatment']})", "",
           f"{len(sb['shots'])} shots over {sb['duration']} s. Plan seed {sb['seed']}, shape {sb['signature']}. "
           "Review this (and storyboard.png) before rendering; new media stays `pending` until a person approves it.",
           "", "| # | time | narration | media | why | crop / motion | transition | overlay | caption |",
           "|---|---|---|---|---|---|---|---|---|"]
    for s in sb["shots"]:
        media = s["kind"]
        if s.get("asset"):
            e = lib.get(s["asset"])
            media = f"{s['kind']}: `{e.id}` ({e.type}, {e.license}, review {e.review.get('status')})"
            if s.get("labels"):
                media += " · " + ", ".join(s["labels"])
        if s.get("scene_type"):
            media += f" · graphic `{s['scene_type']}`" + (" (inset)" if s["kind"] == "inset" else "")
        if s["kind"] == "missing":
            media = f"**MISSING**: {s.get('missing')}"
        crop = ""
        if s.get("crop"):
            c = s["crop"]
            crop = f"{c['framing']}, {c['motion']}, {c.get('layout', '')} x{c.get('upscale', '')}"
        out.append(f"| {s['index']} | {s['start']:.2f}–{s['end']:.2f} | {s['narration']} | {media} | {s['why']} | "
                   f"{crop} | {s['transition']} | {s.get('overlay') or s.get('effect') or ''} | {s['caption_zone']} |")
    out += ["", "## Media gaps (wanted vs used)", ""]
    if sb["gaps"]:
        out += [f"- shot {g['shot']} ({g['segment']}) \"{g['narration']}\": wanted **{g['wanted']}**, used {g['used']}"
                for g in sb["gaps"]]
    else:
        out.append("- none")
    used = [lib.get(s["asset"]) for s in sb["shots"] if s.get("asset")]
    out += ["", "## Credits", ""] + [f"- {line}" for line in ML.attribution_lines(used)]
    return "\n".join(out) + "\n"


def frame_at(ep: Episode, renderer: MS.ShotRenderer, shot: dict, t_global: float) -> Image.Image:
    seg = shot["segment"]
    t = t_global - ep.offsets[seg]
    return renderer.draw(shot, t, ep.durations[seg], ep.tokens[seg], ep.accent)


def storyboard_sheet(ep: Episode, sb: dict, lib: ML.Library, path: Path) -> Path:
    """One real frame per shot (at 70% through it, after its overlay lands) with the plan's notes beside it."""
    renderer = MS.ShotRenderer(lib, ep.ctx, ep.scenes, SP.TREATMENTS[sb["treatment"]]["grade"], ep.season)
    tw, th, nw = 243, 432, 420
    cols = 2
    rows = math.ceil(len(sb["shots"]) / cols)
    sheet = Image.new("RGB", (cols * (tw + nw + 30) + 20, rows * (th + 24) + 90), (16, 17, 21))
    d = ImageDraw.Draw(sheet)
    d.text((20, 22), f"{sb['episode']} · {sb['treatment']} · {len(sb['shots'])} shots · preview frames",
           font=M.font("black", 30), fill=(240, 240, 240))
    for i, s in enumerate(sb["shots"]):
        t = s["start"] + (s["end"] - s["start"]) * 0.7
        fr = frame_at(ep, renderer, s, t)
        x = 20 + (i % cols) * (tw + nw + 30)
        y = 80 + (i // cols) * (th + 24)
        sheet.paste(fr.resize((tw, th), Image.LANCZOS), (x, y))
        lines = [f"#{s['index']}  {s['start']:.1f}-{s['end']:.1f}s  {s['kind'].upper()}",
                 f"\"{s['narration']}\""]
        if s.get("asset"):
            e = lib.get(s["asset"])
            lines.append(f"{e.id} [{e.type}] {e.license} review:{e.review.get('status')}")
            lines.append(f"crop {s['crop']['framing']}/{s['crop']['motion']} {s['crop'].get('layout')}")
        if s.get("scene_type"):
            lines.append(f"graphic: {s['scene_type']}")
        lines += [f"why: {s['why']}", f"in: {s['transition']}  overlay: {s.get('overlay') or s.get('effect') or '-'}",
                  f"labels: {', '.join(s.get('labels', [])) or '-'}  caption: {s['caption_zone']}"]
        if s.get("wanted"):
            lines.append(f"GAP: wanted {s['wanted']}")
        yy = y + 4
        for ln in lines:
            for part in MS._wrap(ln, 46):
                d.text((x + tw + 14, yy), part, font=M.font("semibold", 17),
                       fill=(255, 196, 0) if ln.startswith("GAP") else (225, 225, 230))
                yy += 22
    renderer.close()
    sheet.save(path, quality=88) if path.suffix == ".jpg" else sheet.save(path)
    return path


def _transition(prev: Image.Image, cur: Image.Image, kind: str, k: float) -> Image.Image:
    if kind == "flash":
        return Image.blend(cur, Image.new("RGB", cur.size, (255, 255, 255)), 0.55 * (1 - k))
    if kind == "dissolve":
        return Image.blend(prev, cur, M.ease_in_out(k))
    if kind == "whip":
        e = M.ease_out(k)
        canvas = Image.new("RGB", cur.size, M.DARK)
        canvas.paste(prev, (int(-M.W * e), 0))
        canvas.paste(cur, (int(M.W * (1 - e)), 0))
        return canvas.filter(ImageFilter.BoxBlur(int(16 * (1 - e)) + 1)) if e < 0.8 else canvas
    return cur


def render(ep: Episode, sb: dict, lib: ML.Library, out: Path) -> dict:
    fps = ep.cfg["video"]["fps"]
    shots = sb["shots"]
    renderer = MS.ShotRenderer(lib, ep.ctx, ep.scenes, SP.TREATMENTS[sb["treatment"]]["grade"], ep.season, fps)
    cues = []
    for seg in ep.script["narration_segments"]:
        off = ep.offsets[seg["id"]]
        for ph in RM.phrases(ep.tokens[seg["id"]]):
            cues.append({"segment": seg["id"], "words": ph, "offset": off, "start": off + ph[0]["start"],
                         "text": " ".join(w["text"] for w in ph)})
    for i, cq in enumerate(cues):        # a caption ends at the next one or at its own beat's end, whichever is first
        seg_end = ep.offsets[cq["segment"]] + ep.durations[cq["segment"]]
        cq["end"] = min(cues[i + 1]["start"] if i + 1 < len(cues) else ep.total, seg_end)
    frames = int(math.ceil(ep.total * fps))
    final = out / "final.mp4"
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{M.W}x{M.H}", "-r", str(fps), "-i", "-", "-i", str(out / "work/mix.wav"), "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-g", str(fps * 2),
         "-af", RM.LOUDNESS, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-frames:v", str(frames),
         "-movflags", "+faststart", str(final)], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    cur, prev_last, ci = -1, None, 0
    cap_boxes = []
    for n in range(frames):
        t = n / fps
        i = max(j for j, s in enumerate(shots) if s["start"] <= t + 1e-9)
        s = shots[i]
        if i != cur:
            if cur >= 0:
                prev_last = frame_at(ep, renderer, shots[cur], max(shots[cur]["end"] - 1 / fps, shots[cur]["start"]))
            cur = i
        frame = frame_at(ep, renderer, s, t)
        tl = t - s["start"]
        tr = s.get("transition", "cut")
        if prev_last is not None and tl < TRANS.get(tr, 0.0):
            frame = _transition(prev_last, frame, tr, tl / TRANS[tr])
        while ci + 1 < len(cues) and cues[ci + 1]["start"] <= t:
            ci += 1
        cq = cues[ci]
        slammed = s.get("overlay") == "stat" and any(w["claim"] == s.get("stat_claim") for w in cq["words"])
        if cq["start"] <= t < cq["end"] and not slammed:     # the number is already on screen, big: no subtitle
            active = max((j for j, w in enumerate(cq["words"]) if cq["offset"] + w["start"] <= t), default=0)
            layer, y = MS.caption(cq["words"], active, ep.accent, t - cq["start"], CAP_Y[s["caption_zone"]])
            frame.paste(layer, (0, y), layer)
            if n % fps == 0:
                cap_boxes.append({"t": round(t, 2), "shot": s["index"], "y0": y + 24, "y1": y + layer.height - 24})
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    renderer.close()
    if proc.wait():
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.read().decode()[-1500:]}")
    flat = [{"start": round(c["start"], 3), "end": round(c["end"], 3), "text": c["text"], "segment": c["segment"]}
            for c in cues]
    (out / "captions.srt").write_text("\n".join(f"{i}\n{RM.srt_time(c['start'])} --> {RM.srt_time(c['end'])}\n{c['text']}\n"
                                                for i, c in enumerate(flat, 1)))
    write_json(out / "captions.json", {"cues": flat, "boxes": cap_boxes})
    src_tl = out / "source_timeline.json"
    events = read_json(src_tl)["sound_events"] if src_tl.exists() else []
    tl_scenes = []
    for s in shots:
        from ..edit_scenes import HERO
        from ..visual_audit import LEGACY_HERO
        hero = HERO.get(s.get("scene_type"), LEGACY_HERO.get(s.get("scene_type"), 0.0)) if s.get("scene_type") else 0.0
        if s["kind"] == "inset":
            hero *= 0.58 * 0.58                     # the graphic is a card at 58% scale
        if s.get("asset") and s["kind"] in ("photo", "broll"):
            e = lib.get(s["asset"])
            if e.type in ML.PLAYER_PHOTOS:
                hero = round(min(1.0, (e.subject["box"][3] - e.subject["box"][1]) * e.height
                                 / max(1, s["crop"]["window"][3] - s["crop"]["window"][1])), 2)
        tl_scenes.append({"type": f"em_{s['kind']}", "_start": s["start"], "_dur": s["dur"], "hero": hero,
                          "asset": s.get("asset"), "scene_type": s.get("scene_type")})
    write_json(out / "work/timeline.json", {"scenes": tl_scenes, "sound_events": events, "duration": ep.total})
    return {"frames": frames, "duration": ep.total, "captions": len(flat)}


def provenance(sb: dict, lib: ML.Library, cfg_headshots: bool) -> dict:
    used = {}
    for s in sb["shots"]:
        if s.get("asset"):
            e = lib.get(s["asset"])
            used.setdefault(e.id, {"id": e.id, "type": e.type, "who": e.name, "team": e.team, "season": e.season,
                                   "license": e.license, "license_url": ML.LICENSES[e.license]["url"],
                                   "attribution": e.attribution, "source": e.source.get("page") or e.source.get("url"),
                                   "sha256": e.sha256, "review": e.review.get("status"), "shots": []})["shots"].append(s["index"])
    return {"assets": list(used.values()), "credits": ML.attribution_lines([lib.get(i) for i in used]),
            "espn_headshot_used": any(lib.get(i).license == "espn_override" for i in used),
            "headshots_switch": cfg_headshots}


def make(source: Path, out: Path, cfg: Config, treatment: str | None = None, preview: bool = False,
         recent: list[str] | None = None, lib: ML.Library | None = None) -> dict:
    prepare(source, out)
    ep = Episode(out, cfg)
    lib = lib or ML.Library.load()
    treatment = treatment or SP.choose_treatment(ep.story)
    sb = storyboard(ep, lib, treatment, recent)
    write_json(out / "storyboard.json", sb)
    (out / "storyboard.md").write_text(storyboard_markdown(sb, lib))
    storyboard_sheet(ep, sb, lib, out / "storyboard.png")
    result = {"storyboard": str(out / "storyboard.md"), "shots": len(sb["shots"]), "gaps": len(sb["gaps"])}
    pre = QC.check_plan(sb, lib, ep, publish=False)
    write_json(out / "media_qc_plan.json", pre)
    if preview:
        return {**result, "plan_blocks": [c for c in pre["checks"] if not c["ok"] and c["severity"] == "block"]}
    if any(not c["ok"] and c["severity"] == "block" for c in pre["checks"]):
        raise RuntimeError(f"storyboard blocked: see {out / 'media_qc_plan.json'}")
    result.update(render(ep, sb, lib, out))
    write_json(out / "media_provenance.json", provenance(sb, lib, headshots.enabled(cfg)))
    qc = QC.run(out, sb, lib, ep)
    write_json(out / "media_qc.json", qc)
    (out / "media_qc.md").write_text(QC.markdown(qc))
    from .. import visual_audit as VA
    write_json(out / "visual_audit.json", VA.measure(out))
    VA.contact_sheet(out / "final.mp4", out / "contact_sheet.jpg", 12, f"{ep.id} · editorial_media · {treatment}")
    return {**result, "qc_preview_pass": qc["preview_pass"], "qc_publish_pass": qc["publish_pass"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="editorial_media", description=__doc__.split("\n")[0])
    ap.add_argument("--source", required=True, help="a finished episode folder (only read)")
    ap.add_argument("--out", help="new folder (default: <source>-editorial beside it)")
    ap.add_argument("--treatment", choices=sorted(SP.TREATMENTS), help="default: chosen from the story angle")
    ap.add_argument("--preview", action="store_true", help="storyboard and preview sheet only, no render")
    a = ap.parse_args(argv)
    src = Path(a.source)
    out = Path(a.out) if a.out else src.with_name(src.name + "-editorial")
    print(json.dumps(make(src, out, Config.load(), a.treatment, a.preview), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
