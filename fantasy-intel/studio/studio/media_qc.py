"""QC for editorial_media re-edits: is every picture the right person, team and moment, cleared, well cropped,
readable, varied, and true to the numbers?

Plan checks (before rendering, from storyboard.json):
  identity         a player photo shows the person the narration is about (gsis ids match)
  team / season    current uniform for current analysis; an old photo carries a FILE PHOTO label
  context          no generic injury imagery as a player's injury; stock under a line about a player is labelled
                   ILLUSTRATIVE; a teammate photo is that teammate
  license / review every file cleared for monetised use; new media approved by a person before publishing
  fallbacks        headshot fallbacks and missing-media slates are counted (a slate blocks publishing)
  crop             full-bleed crops upscale at most media_scenes.MAX_UPSCALE; the face stays inside the frame
  captions         the caption zone never covers the player's face
  variety          distinct pictures, no long run of one kind, no shot held too long
  duplication      the same photo and framing never repeat; no photo carries the video alone
  numbers          every overlaid number is a claim's display string, spoken in that shot
Output checks (after rendering): 1080x1920, the configured fps, duration matches the narration, audio present,
no black frames.

`preview_pass`: nothing blocks an experimental render. `publish_pass`: also every file reviewed and no slate.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

from . import media_library as ML
from . import media_scenes as MS

MAX_RUN = 3                 # consecutive shots of one kind
MAX_HOLD = 4.6              # seconds one shot may hold
MAX_USES = 3                # times one photo may appear


def _c(check, ok, detail, severity="block"):
    return {"check": check, "ok": bool(ok), "severity": severity, "detail": detail}


def check_plan(sb: dict, lib: ML.Library, ep, publish: bool = False) -> dict:
    shots = sb["shots"]
    out = []
    claims = ep.claims
    headshots_on = sb.get("headshot_fallback_enabled", False)
    renderer = MS.ShotRenderer(lib, ep.ctx, ep.scenes, "neutral", ep.season)
    for s in shots:
        if not s.get("asset"):
            continue
        e = lib.get(s["asset"])
        if not lib.available(e):
            out.append(_c("availability", False, f"shot {s['index']}: {e.id} is not in the cache (or its hash changed)"))
        use = {"subject_id": s.get("subject_id") if e.type in ML.PLAYER_PHOTOS else None,
               "subject_team": s.get("subject_team"), "intent": s.get("intent"), "labels": s.get("labels", []),
               "names_player": s.get("names_player"),
               "injury_claim": any(claims.get(c, {}).get("source", {}).get("kind", "").startswith("injury")
                                   for c in s.get("claims", []))}
        for p in ML.check_use(e, use, season=ep.season, cfg_headshots=headshots_on, publish=publish):
            out.append(_c(p["check"], False, f"shot {s['index']}: {p['message']} ({e.id})", p["severity"]))
        need = ML.labels_for(e, ep.season)
        if any(lab not in s.get("labels", []) for lab in need):
            out.append(_c("labels", False, f"shot {s['index']}: {e.id} must carry {need}"))
        # crop quality
        _, (sw, sh), win, full = renderer.geometry(s)
        up = MS.H / (win[3] - win[1])
        if full and up > MS.MAX_UPSCALE:
            out.append(_c("crop", False, f"shot {s['index']}: upscale x{up:.2f}"))
        f = e.subject.get("face")
        if full and f:
            fx0, fy0, fx1, fy1 = MS.to_frame(win, sw, sh, f)
            inside = fx0 >= -5 and fy0 >= -5 and fx1 <= MS.W + 5 and fy1 <= MS.H + 5
            out.append(_c("crop_face", inside, f"shot {s['index']}: face {'inside' if inside else 'cut off by'} the crop",
                          "warn"))
            cap_a, cap_b = (1380, 1500) if s["caption_zone"] == "low" else (355, 475)
            mid = MS.motion_window(win, sw, sh, s["crop"]["motion"], 0.5)
            fy0m, fy1m = MS.to_frame(mid, sw, sh, f)[1::2]
            clear = fy1m < cap_a or fy0m > cap_b
            out.append(_c("caption_face", clear, f"shot {s['index']}: caption ({s['caption_zone']}) "
                          f"{'clear of' if clear else 'over'} the face", "warn"))
    renderer.close()
    # fallbacks
    fb = [s["index"] for s in shots if s.get("asset") and lib.get(s["asset"]).type == "headshot_fallback"]
    miss = [s["index"] for s in shots if s["kind"] == "missing"]
    out.append(_c("fallbacks", not fb, f"headshot fallback in shots {fb}" if fb else "no fallbacks", "warn"))
    out.append(_c("missing_media", not miss, f"MEDIA NEEDED slates in shots {miss}" if miss else "no slates",
                  "block" if publish else "warn"))
    # variety and duplication
    kinds = [s["kind"] for s in shots]
    run, best = 1, 1
    for a, b in zip(kinds, kinds[1:]):
        run = run + 1 if a == b else 1
        best = max(best, run)
    out.append(_c("variety_run", best <= MAX_RUN, f"longest run of one kind: {best} shots", "warn"))
    hold = max(s["dur"] for s in shots)
    out.append(_c("variety_hold", hold <= MAX_HOLD, f"longest shot {hold:.1f} s", "warn"))
    assets = [s["asset"] for s in shots if s.get("asset")]
    photos_available = len({e.id for e in lib.entries.values()})
    out.append(_c("variety_assets", len(set(assets)) >= min(3, len(assets), photos_available),
                  f"{len(set(assets))} distinct pictures in {len(assets)} picture shots", "warn"))
    uses = {a: assets.count(a) for a in set(assets)}
    over = {a: n for a, n in uses.items() if n > MAX_USES}
    out.append(_c("duplication_uses", not over, f"over-used: {over}" if over else "no photo over-used", "warn"))
    pairs = [(s["asset"], s["crop"]["framing"]) for s in shots if s.get("asset")]
    rep = sorted({p for p in pairs if pairs.count(p) > 1})
    out.append(_c("duplication_framing", not rep, f"same photo and framing repeated: {rep}" if rep else "no repeats",
                  "warn"))
    # numbers
    for s in shots:
        cid = s.get("stat_claim")
        if s.get("overlay") == "stat" and cid:
            toks = [t for t in ep.tokens[s["segment"]] if t["claim"] == cid]
            disp = claims.get(cid, {}).get("display", "")
            spoken = " ".join(t["text"] for t in toks).rstrip(".,?!")
            ok = bool(toks) and disp and spoken.replace(" ", "") == disp.replace(" ", "")
            out.append(_c("numbers", ok, f"shot {s['index']}: overlay '{spoken}' vs claim {cid} '{disp}'"))
    blocks = [c for c in out if not c["ok"] and c["severity"] == "block"]
    return {"publish_mode": publish, "pass": not blocks, "checks": out}


def probe(path: Path) -> dict:
    raw = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(raw)


def check_output(out_dir: Path, ep) -> list[dict]:
    final = Path(out_dir) / "final.mp4"
    if not final.exists():
        return [_c("output", False, "final.mp4 missing")]
    p = probe(final)
    v = next((s for s in p["streams"] if s["codec_type"] == "video"), {})
    a = next((s for s in p["streams"] if s["codec_type"] == "audio"), None)
    num, den = (int(x) for x in v.get("r_frame_rate", "0/1").split("/"))
    fps = num / max(den, 1)
    dur = float(p["format"]["duration"])
    res = [_c("output_size", (v.get("width"), v.get("height")) == (1080, 1920), f"{v.get('width')}x{v.get('height')}"),
           _c("output_fps", abs(fps - ep.cfg["video"]["fps"]) < 0.01, f"{fps:.2f} fps"),
           _c("output_duration", abs(dur - ep.total) < 0.25, f"{dur:.2f} s video vs {ep.total:.2f} s narration"),
           _c("output_audio", a is not None, "audio stream present" if a else "no audio")]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(final), "-vf", "fps=2,scale=108:192", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    g = np.frombuffer(raw, np.uint8).reshape(-1, 192, 108)
    black = [round(i / 2, 1) for i, f in enumerate(g) if f.mean() < 8]
    res.append(_c("output_black_frames", not black, f"black frames at {black}" if black else "no black frames"))
    return res


def run(out_dir: Path, sb: dict, lib: ML.Library, ep) -> dict:
    preview = check_plan(sb, lib, ep, publish=False)
    publish = check_plan(sb, lib, ep, publish=True)
    output = check_output(out_dir, ep)
    ob = [c for c in output if not c["ok"] and c["severity"] == "block"]
    return {"preview_pass": preview["pass"] and not ob, "publish_pass": publish["pass"] and not ob,
            "checks": preview["checks"] + output,
            "publish_blocks": [c for c in publish["checks"] if not c["ok"] and c["severity"] == "block"]}


def markdown(qc: dict) -> str:
    lines = [f"# Media QC", "", f"- experimental render: **{'PASS' if qc['preview_pass'] else 'FAIL'}**",
             f"- publishable: **{'yes' if qc['publish_pass'] else 'NO'}**", "", "| check | ok | severity | detail |",
             "|---|---|---|---|"]
    for c in qc["checks"]:
        lines.append(f"| {c['check']} | {'yes' if c['ok'] else 'NO'} | {c['severity']} | {c['detail']} |")
    if qc["publish_blocks"]:
        lines += ["", "## Blocks publishing", ""] + [f"- {c['check']}: {c['detail']}" for c in qc["publish_blocks"]]
    return "\n".join(lines) + "\n"
