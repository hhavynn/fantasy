"""make-episode: one command, every stage, outputs in episodes/<episode_id>/.

    uv run make-episode --week 6 --format short          # this week's Shorts, one per story
    uv run make-episode --week 6 --format short --player braelon-allen
    uv run make-episode --week 6 --format long
    uv run make-episode --week 6 --format long --force script,tts
    uv run make-episode --week 6 --flame-praise           # the latest game's flame and praise Shorts
    uv run make-episode --week 6 --flame-praise --player bo-nix

Stages skip themselves when their inputs are unchanged, so a rerun only redoes what changed. QC gates run
last; the command exits non-zero, and upload stays blocked, if any gate fails. Each episode gets a
review.md for the human accuracy check.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time

from . import editorial
from .core import Config, Episode, read_json, sha256_file, write_json
from .stages import (assemble, charts, facts, flame, metadata, provenance, qc, render_motion, review, script,
                     snapshot, story, thumbnail, tts)

LONG = [("snapshot", snapshot), ("facts", facts), ("script", script), ("tts", tts), ("charts", charts),
        ("assemble", assemble), ("thumbnail", thumbnail), ("metadata", metadata)]
SHORT = [("facts", facts), ("script", story), ("tts", tts), ("render", render_motion),
         ("thumbnail", thumbnail), ("metadata", metadata)]


def finish(ep: Episode) -> bool:
    passed = qc.run(ep)
    provenance.run(ep)
    review.run(ep)
    return passed


def run_stages(ep: Episode, stages, hook: str | None = None) -> None:
    for name, stage in stages:
        t0 = time.time()
        if stage is story:
            stage.run(ep, hook=hook)
        else:
            stage.run(ep)
        ep.log(f"  [{name} {time.time() - t0:.1f}s]")


def editorial_pick(cfg: Config, snap: dict, season: int, week: int, recent: set[str], week_ep: Episode) -> list[dict]:
    """Score every qualifying story (one per team) on the editorial board and keep the best that pass, up to
    the weekly quota. The board, with every score's reason, is written to editorial.md for review."""
    candidates = facts.pick_stories(snap, 99, 99, recent)
    hist = editorial.history(cfg.episodes_dir, season, before_week=week)
    rows = []
    for s in candidates:
        f = facts.build_facts(snap, facts.ALL, set(), only=s["gsis_id"])
        if not f["players"]:
            continue
        claims = {c["id"]: c for c in f["claims"]}
        p = dict(f["players"][0], _episode_week=week)
        rows.append((s, editorial.score(p, claims, editorial.choose_format(p, claims), hist)))
    week_ep.path("editorial.md").write_text(editorial.board_markdown([r for _, r in rows]))
    quota = cfg["shorts"]["risers"] + cfg["shorts"]["fallers"]
    keep = [s for s, r in sorted(rows, key=lambda x: -x[1]["total"]) if r["verdict"] == "make"][:quota]
    return [{**s, "order": i} for i, s in enumerate(keep)]


def make_shorts(cfg: Config, season: int, week: int, force: set[str], only: str | None,
                hook: str | None = None, slug_suffix: str = "", presenter_mode: str = "off") -> int:
    week_ep = Episode(season, week, "short", cfg, force=force)
    week_ep.dir.mkdir(parents=True, exist_ok=True)
    snapshot.run(week_ep)
    snap = read_json(week_ep.path("snapshot.json"))
    recent = facts.recent_featured_players(cfg.episodes_dir / "history.jsonl",
                                           cfg["selection"]["recent_episode_window"], week, season, fmt="short")
    stories = editorial_pick(cfg, snap, season, week, recent, week_ep)
    write_json(week_ep.path("stories.json"), stories)
    if only:      # an explicitly requested player isn't limited by the weekly quota, only by the selection rules
        stories = facts.pick_stories(snap, 0, 0, recent, only=only)[:1]
        if not stories:
            print(f"no story for {only!r}; this week's: {[s['slug'] for s in read_json(week_ep.path('stories.json'))]}")
            return 2
    results = []
    for story in stories:
        ep = Episode(season, week, "short", cfg, force=force, slug=story["slug"] + slug_suffix)
        ep.dir.mkdir(parents=True, exist_ok=True)
        print(f"\nshort {ep.id}: {story['name']} ({story['role']})")
        src, dst = week_ep.path("snapshot.json"), ep.path("snapshot.json")
        if not dst.exists() or sha256_file(dst) != sha256_file(src):
            shutil.copyfile(src, dst)
        if slug_suffix:      # an A/B hook variant of the same story, not a new episode
            story = {**story, "variant_of": f"{season}-w{week:02d}-short-{story['slug']}"}
        if not ep.path("story.json").exists() or read_json(ep.path("story.json")) != story:
            write_json(ep.path("story.json"), story)
        run_stages(ep, SHORT, hook or cfg["shorts"].get("hook") or None)
        passed = finish(ep)
        if passed and presenter_mode != "off":       # optional AI host layer; never touches final.mp4
            from . import presenter
            presenter.present(ep, presenter_mode)
        results.append((ep, story, passed))
    index = ["# Shorts for review", "", f"Week {week}, {season}. Watch each, read its review.md, then publish by hand.", ""]
    for ep, story, passed in results:
        meta = read_json(ep.path("metadata.json"))
        index.append(f"- **{story['name']}** ({story['role']}): {meta['title']}. QC {'passed' if passed else 'FAILED'}. "
                     f"`{ep.id}/final.mp4`, `{ep.id}/review.md`")
    week_ep.path("review.md").write_text("\n".join(index) + "\n")
    failed = [ep.id for ep, _, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} Shorts passed QC. Index: {week_ep.path('review.md')}")
    return 1 if failed else 0


def make_flame_praise(cfg: Config, season: int, week: int, force: set[str], only: str | None,
                      hook: str | None = None) -> int:
    """The latest game's strongest flame (a historically bad usage or efficiency line) and praise (a monster one),
    chosen by stages/flame.py; the full candidate board, with every number behind each pick, goes to
    flame_praise.md in the week's folder."""
    week_ep = Episode(season, week, "short", cfg, force=force)
    week_ep.dir.mkdir(parents=True, exist_ok=True)
    snapshot.run(week_ep)
    snap = read_json(week_ep.path("snapshot.json"))
    picks = flame.pick(snap, only)
    week_ep.path("flame_praise.md").write_text(flame.board_markdown(snap, flame.candidates(snap), picks))
    if not picks:
        print(f"no flame or praise case{' for ' + repr(only) if only else ''}; see {week_ep.path('flame_praise.md')}")
        return 2
    results = []
    for c in picks:
        ep = Episode(season, week, "short", cfg, force=force, slug=c["slug"])
        ep.dir.mkdir(parents=True, exist_ok=True)
        print(f"\nshort {ep.id}: {c['name']} ({c['kind']}, {c['lead']['metric']})")
        src, dst = week_ep.path("snapshot.json"), ep.path("snapshot.json")
        if not dst.exists() or sha256_file(dst) != sha256_file(src):
            shutil.copyfile(src, dst)
        story_json = {"gsis_id": c["gsis_id"], "name": c["name"], "role": c["kind"], "team": c["team"],
                      "slug": c["slug"], "order": c["order"], "format": "flame_praise"}
        if not ep.path("story.json").exists() or read_json(ep.path("story.json")) != story_json:
            write_json(ep.path("story.json"), story_json)
        for name, run in (("facts", flame.facts_run), ("script", lambda e: flame.script_run(e, hook)),
                          ("tts", tts.run), ("render", render_motion.run), ("thumbnail", thumbnail.run),
                          ("metadata", metadata.run)):
            t0 = time.time()
            run(ep)
            ep.log(f"  [{name} {time.time() - t0:.1f}s]")
        results.append((ep, c, finish(ep)))
    failed = [ep.id for ep, _, ok in results if not ok]
    for ep, c, ok in results:
        print(f"{c['kind']}: {c['name']}: QC {'passed' if ok else 'FAILED'}. {ep.path('final.mp4')}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="make-episode", description=__doc__.split("\n")[0])
    ap.add_argument("--week", type=int, required=True, help="NFL week the episode previews (uses games before it)")
    ap.add_argument("--format", choices=["long", "short"], default="short")
    ap.add_argument("--season", type=int, help="defaults to config.toml season")
    ap.add_argument("--force", default="", help="comma-separated stages to rerun regardless of cache, or 'all'")
    ap.add_argument("--player", help="Shorts only: build just this story (its slug, e.g. braelon-allen)")
    ap.add_argument("--voice", help="Shorts only: narrate with this enrolled voice (overrides config "
                                    "shorts.voice_clone); written to <slug>-voice-<name>")
    ap.add_argument("--hook", help="Shorts only: force a hook family (e.g. where, talk, warning) for A/B variants; "
                                   "the variant is written to <slug>-hook-<name>")
    ap.add_argument("--flame-praise", action="store_true",
                    help="Shorts only: the latest game's flame (historically bad line) and praise (monster line) "
                         "Shorts instead of the usage stories; with --player, that player's strongest case")
    ap.add_argument("--presenter", choices=["off", "hook", "hybrid"],
                    help="Shorts only, EXPERIMENTAL: also write final_presenter_<mode>.mp4 with the AI host "
                         "(default: config.toml [presenter] mode, which is off)")
    args = ap.parse_args(argv)
    cfg = Config.load()
    presenter_mode = args.presenter or (cfg.raw.get("presenter") or {}).get("mode", "off")
    if args.voice:
        cfg.raw["shorts"] = {**cfg.raw["shorts"], "voice_clone": args.voice}
    season = args.season or cfg.season
    force = {s.strip() for s in args.force.split(",") if s.strip()}
    if args.flame_praise:
        return make_flame_praise(cfg, season, args.week, force, args.player, args.hook)
    if args.format == "short":
        suffix = (f"-hook-{args.hook}" if args.hook else "") + (f"-voice-{args.voice}" if args.voice else "")
        return make_shorts(cfg, season, args.week, force, args.player, args.hook, suffix, presenter_mode)
    ep = Episode(season, args.week, "long", cfg, force=force)
    ep.dir.mkdir(parents=True, exist_ok=True)
    print(f"episode {ep.id} -> {ep.dir}")
    run_stages(ep, LONG)
    if not finish(ep):
        print(f"QC FAILED: see {ep.path('qc.json')}. Upload is blocked.", file=sys.stderr)
        return 1
    print(f"QC passed. Review {ep.path('review.md')} and {ep.path('final.mp4')} before uploading.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
