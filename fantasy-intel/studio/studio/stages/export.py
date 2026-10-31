"""Upload bundles: after QC, one folder per app for a Short, ready to post by hand from a phone or a desktop.

    <episode>/upload/{tiktok,reels,shorts}/   video.mp4, cover.png, caption.txt, post.json (shorts adds title.txt, tags.txt)
    <episode>/upload/README.md                a tick list for each app, disclosure step included
    <episode>/upload/manifest.json            which render went in, its hash, the posting deadline
    <season>-wNN-short/UPLOAD.md              the week's Shorts in posting order

Only a Short whose QC passed gets a bundle, and the video must still be the file QC checked. Nothing here reaches a
network: every upload is a human decision. Limits are data, in config.toml [export], with the date they were checked.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

from ..core import Config, Episode, read_json, sha256_file, utcnow, write_json
from ..numbers import TEAM_NAMES

SIZE = (1080, 1920)
PLATFORMS = ("tiktok", "reels", "shorts")
APP = {"tiktok": "TikTok", "reels": "Instagram Reels", "shorts": "YouTube Shorts"}
# Where each app keeps its own AI disclosure. The voice is synthetic, so every Short takes this step.
DISCLOSURE = {"tiktok": "Tap Next, then More options, and turn on AI-generated content.",
              "reels": "While creating the post, turn on Add AI label (look under More options if it isn't shown).",
              "shorts": "In YouTube Studio, open the video's details and set AI use to Yes."}
TICKS = {"tiktok": ["Open the TikTok app and upload `video.mp4` from this folder.",
                    "Paste `caption.txt` into the caption.",
                    "Choose the cover: use `cover.png`, or the frame closest to it."],
         "reels": ["Open Instagram, start a new Reel and pick `video.mp4` from this folder.",
                   "Paste `caption.txt` into the caption.",
                   "Choose the cover: use `cover.png`."],
         "shorts": ["Upload `video.mp4` from this folder and keep it Private for now.",
                    "Paste `title.txt` into the title and `caption.txt` into the description.",
                    "Paste `tags.txt` into tags and set Made for kids to No.",
                    "Set the cover where YouTube offers it: use `cover.png`, or the closest frame."]}
LAST_TICK = {"tiktok": "Watch the preview, then post.", "reels": "Watch the preview, then share.",
             "shorts": "Watch it in Studio, then set it to Public."}


class Blocked(RuntimeError):
    """This Short may not be bundled."""


def _probe(video: Path) -> tuple[int, int, float]:
    raw = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height:format=duration", "-of", "json", str(video)],
                         capture_output=True, text=True, check=True).stdout
    info = json.loads(raw)
    return info["streams"][0]["width"], info["streams"][0]["height"], float(info["format"]["duration"])


def _style(ep: Episode) -> str:
    """The graphic style this Short was rendered in, chosen the way cli.render_style chooses it."""
    style = (ep.cfg.raw.get("graphics") or {}).get("style", "original")
    if style == "auto":
        from .. import graphics
        script = read_json(ep.path("script.json"))
        style = graphics.choose(ep.cfg, script["structure"]["angle"], [s["type"] for s in script["shots"]])
    return style


def source(ep: Episode) -> tuple[str, Path]:
    """The render to post: the styled one when it passed its own QC, otherwise final.mp4."""
    style = _style(ep)
    styled, styled_qc = ep.path(f"styles/{style}/final.mp4"), ep.path(f"styles/{style}/qc.json")
    if style != "original" and styled.exists() and styled_qc.exists():
        rec = read_json(styled_qc)
        if rec["passed"] and rec["files"].get("final.mp4") == sha256_file(styled):
            return f"styles/{style}/final.mp4", styled
    return "final.mp4", ep.path("final.mp4")


def check(ep: Episode, qc: dict, meta: dict, video: Path, duration_limit: float) -> None:
    if not qc["passed"]:
        raise Blocked(f"{ep.id}: QC did not pass")
    if video == ep.path("final.mp4") and qc["files"].get("final.mp4") != sha256_file(video):
        raise Blocked(f"{ep.id}: final.mp4 changed after QC; rerun make-episode")
    if meta["privacy_status"] != "private":
        raise Blocked(f"{ep.id}: privacy_status must stay private, it is {meta['privacy_status']!r}")
    if meta.get("contains_synthetic_media") is not True:
        raise Blocked(f"{ep.id}: metadata must declare synthetic media")
    width, height, duration = _probe(video)
    if (width, height) != SIZE:
        raise Blocked(f"{ep.id}: the video is {width}x{height}, it must be 1080x1920")
    if duration > duration_limit:
        raise Blocked(f"{ep.id}: {duration:.0f}s is over the {duration_limit:.0f}s limit")


def parts(description: str) -> tuple[list[str], list[str]]:
    """The description's story lines, then the lines no caption may lose: any AI parody disclosure, the AI-voice
    disclosure and the data credit (stages/metadata.py writes them all)."""
    lines = description.splitlines()
    note = [x for x in lines if x.startswith("AI parody")]
    story = [x for x in description.split("\n\n")[0].splitlines() if x not in note]
    disclosure = next((x for x in lines if x.startswith("Narrated with an AI voice")), None)
    credit = next((x for x in lines if x.startswith("Data: nflverse")), None)
    if not disclosure:
        raise Blocked("the description has no AI-voice disclosure")
    if not credit:
        raise Blocked("the description has no data credit")
    return story, [*note, disclosure, credit]


def hashtags(meta: dict, hero: dict, limit: int) -> list[str]:
    """The series' own tags, then the player and his team; #shorts belongs to YouTube only."""
    base = [t for t in re.findall(r"#\w+", meta["description"].rsplit("\n\n", 1)[-1]) if t != "#shorts"]
    slug = lambda s: "#" + re.sub(r"[^a-z0-9]", "", s.lower())      # noqa: E731
    named = [slug(hero["name"])] if hero.get("name") else []
    team = [slug(TEAM_NAMES[hero["team"]])] if hero.get("team") in TEAM_NAMES else []
    return list(dict.fromkeys(named + team + base))[:limit]


def caption(title: str, story: list[str], fixed: list[str], tags: list[str], limit: int) -> str:
    """Title and story, the disclosure and the credit, then hashtags. A long story loses its last lines; the
    disclosure and the credit are never cut."""
    keep = list(story)
    while True:
        text = "\n\n".join(["\n".join([title, *keep]), "\n".join(fixed), " ".join(tags)])
        if len(text) <= limit:
            return text
        if not keep:
            raise Blocked(f"the disclosure and credit alone are over the {limit} character limit")
        keep.pop()


def _link(src: Path, dst: Path) -> None:
    """A hard link where the disk allows it, so a bundle does not store the video three more times."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _cover(ep: Episode, video: Path, out: Path) -> int:
    """The end of the first beat, where its scene has settled. Returns the frame's time in milliseconds."""
    segments = read_json(ep.path("audio/timings.json"))["segments"]
    _, _, duration = _probe(video)
    at = min(max(segments[0]["duration"] - 0.3, 0.5), max(duration - 0.1, 0.0))
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.3f}", "-i", str(video),
                    "-frames:v", "1", "-update", "1", str(out)], check=True)
    with Image.open(out) as im:
        if im.size != SIZE:
            raise Blocked(f"{ep.id}: the cover is {im.size}, it must be 1080x1920")
    return round(at * 1000)


def post_by(story: dict, qc: dict) -> str | None:
    """When the Short stops being true or timely: its own deadline, or the kickoff a live injury status expires at."""
    if story.get("post_by"):
        return story["post_by"]
    gate = next((g for g in qc.get("gates", []) if g["name"] == "timeliness"), {})
    return gate.get("details", {}).get("valid_until")


def readme(ep: Episode, meta: dict, deadline: str | None) -> str:
    what = "An AI voice" + (" and AI-generated video" if meta.get("ai_parody") else "") + " made this Short."
    lines = [f"# Upload: {meta['title']}", "",
             f"`{ep.id}`. This bundle does not publish anything: each post below is yours to make.",
             f"Post by {deadline}." if deadline else "No posting deadline.", "",
             f"{what} Take the AI step in every app.", ""]
    for platform in PLATFORMS:
        lines += [f"## {APP[platform]} (`{platform}/`)", ""]
        steps = [*TICKS[platform], f"**AI disclosure:** {DISCLOSURE[platform]}", LAST_TICK[platform]]
        lines += [f"- [ ] {s}" for s in steps] + [""]
    return "\n".join(lines)


def _write(ep: Episode, tmp: Path, platform: str, video: Path, cover: Path, text: str, record: dict) -> None:
    d = tmp / platform
    d.mkdir(parents=True)
    _link(video, d / "video.mp4")
    _link(cover, d / "cover.png")
    (d / "caption.txt").write_text(text + "\n")
    write_json(d / "post.json", record)


def run(ep: Episode) -> bool:
    """Write the bundle, replacing any earlier one. False for a variant (an experiment, never posted)."""
    story = read_json(ep.path("story.json"))
    if story.get("variant_of"):
        ep.log("  upload: variant, no bundle")
        return False
    limits = ep.cfg["export"]
    qc, meta = read_json(ep.path("qc.json")), read_json(ep.path("metadata.json"))
    rel, video = source(ep)
    check(ep, qc, meta, video, limits["shorts"]["max_seconds"])
    if not 0 < len(meta["title"]) <= limits["shorts"]["max_title"]:
        raise Blocked(f"{ep.id}: title is {len(meta['title'])} characters")
    if len(meta["description"].encode()) > limits["shorts"]["max_description"]:
        raise Blocked(f"{ep.id}: the description is over YouTube's limit")
    story_lines, fixed = parts(meta["description"])
    hero = read_json(ep.path("facts.json"))["players"][0]
    tmp = ep.path("upload.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    try:
        cover_ms = _cover(ep, video, tmp / "cover.png")
        deadline = post_by(story, qc)
        for platform in PLATFORMS:
            lim = limits[platform]
            tags = hashtags(meta, hero, lim["max_hashtags"]) if platform != "shorts" else ["#shorts"]
            text = (meta["description"] if platform == "shorts" else
                    caption(meta["title"], story_lines[:lim.get("story_lines")], fixed, tags, lim["max_caption"]))
            _write(ep, tmp, platform, video, tmp / "cover.png", text, {
                "platform": platform, "episode_id": ep.id, "title": meta["title"], "caption": text, "hashtags": tags,
                "video": "video.mp4", "cover": "cover.png", "cover_frame_ms": cover_ms, "post_by": deadline,
                "disclosure": {"ai_voice": True, "ai_visuals": bool(meta.get("ai_parody")),
                               "steps": DISCLOSURE[platform]}})
        (tmp / "shorts/title.txt").write_text(meta["title"] + "\n")
        (tmp / "shorts/tags.txt").write_text(", ".join(meta.get("tags", [])) + "\n")
        (tmp / "cover.png").unlink()
        (tmp / "README.md").write_text(readme(ep, meta, deadline))
        write_json(tmp / "manifest.json", {
            "episode_id": ep.id, "title": meta["title"], "source": rel, "sha256": sha256_file(video),
            "order": story.get("order", 0), "post_by": deadline, "platforms": list(PLATFORMS), "published": False,
            "exported_at": utcnow()})
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    shutil.rmtree(ep.path("upload"), ignore_errors=True)
    tmp.rename(ep.path("upload"))
    ep.log(f"  upload: {ep.path('upload')} ({rel})")
    return True


def week_list(cfg: Config, season: int, week: int) -> Path | None:
    """UPLOAD.md: every bundled Short of the week in posting order, with one tick per app."""
    prefix = f"{season}-w{week:02d}-short"
    bundles = [read_json(p) for p in cfg.episodes_dir.glob(f"{prefix}-*/upload/manifest.json")]
    if not bundles:
        return None
    bundles.sort(key=lambda m: (m["order"], m["episode_id"]))
    lines = [f"# Week {week} uploads", "", "Post in this order. Each Short's `upload/README.md` has the steps.", ""]
    for m in bundles:
        lines += [f"## {m['title']}", "", f"`{m['episode_id']}/upload/`"
                  + (f", post by {m['post_by']}" if m.get("post_by") else ""), ""]
        lines += [f"- [ ] {APP[p]}" for p in m["platforms"]] + [""]
    out = cfg.episodes_dir / prefix / "UPLOAD.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    return out


def bundle(ep: Episode) -> bool:
    """The make-episode step: a bundle that cannot be written is reported, never a crash after a good render."""
    try:
        return run(ep)
    except Blocked as exc:
        ep.log(f"  upload: no bundle, {exc}")
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="export-bundles", description="Rebuild the week's upload bundles from passed QC")
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--season", type=int, help="defaults to config.toml season")
    args = ap.parse_args(argv)
    cfg = Config.load()
    season = args.season or cfg.season
    prefix = f"{season}-w{args.week:02d}-short-"
    blocked = 0
    for d in sorted(cfg.episodes_dir.glob(f"{prefix}*")):
        if not (d / "qc.json").exists() or not (d / "story.json").exists():
            continue
        try:
            run(Episode(season, args.week, "short", cfg, slug=d.name.removeprefix(prefix)))
        except Blocked as exc:
            print(f"blocked: {exc}", file=sys.stderr)
            blocked += 1
    out = week_list(cfg, season, args.week)
    print(f"week list: {out}" if out else "no bundles this week")
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
