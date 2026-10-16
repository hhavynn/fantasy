"""make-episode: one command, every stage, outputs in episodes/<episode_id>/.

    uv run make-episode --week 6 --format short          # this week's Shorts, one per story
    uv run make-episode --week 6 --format short --player braelon-allen
    uv run make-episode --week 6 --format long
    uv run make-episode --week 6 --format long --force script,tts
    uv run make-episode --week 6 --flame-praise           # the latest game's flame and praise Shorts
    uv run make-episode --week 6 --flame-praise --player bo-nix
    uv run make-episode --week 6 --flame-praise --player bo-nix --treatment courtroom --storyboard
    uv run make-episode --week 6 --format short --style premium   # also render a graphic style (pixel, broadcast, auto)

Stages skip themselves when their inputs are unchanged, so a rerun only redoes what changed. QC gates run
last; the command exits non-zero, and upload stays blocked, if any gate fails. Each episode gets a
review.md for the human accuracy check.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time

from . import editorial, shows
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


def editorial_media(ep: Episode, cfg: Config) -> None:
    """Optional, experimental: the Short re-edited with real photography (stages/editorial_media.py), written to
    <slug>-editorial beside it. final.mp4 and everything else in the episode are only read."""
    mode = (cfg.raw.get("editorial_media") or {}).get("mode", "off")
    if mode == "off":
        return
    from .stages import editorial_media as em
    out = ep.dir.with_name(ep.dir.name + "-editorial")
    print(f"  editorial_media ({mode}): {em.make(ep.dir, out, cfg, preview=mode == 'preview')}")


def render_style(ep: Episode, style: str) -> tuple[str, bool] | None:
    """Render a graphic style next to final.mp4; (style rendered, its QC result), or None when none was."""
    from . import graphics
    from .graphics import render as GR
    if style == "auto":
        types = [s["type"] for s in read_json(ep.path("script.json"))["shots"]]
        style = graphics.choose(ep.cfg, read_json(ep.path("script.json"))["structure"]["angle"], types)
        ep.log(f"  style: auto chose {style}")
        if style == "original":
            return None
    try:
        ok = GR.run(ep, style)
    except GR.Unsupported as e:
        ep.log(f"  style {style}: skipped, {e}")
        return None
    ep.log(f"  style {style}: QC {'passed' if ok else 'FAILED'}, {ep.path(f'styles/{style}/final.mp4')}")
    return style, ok


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
    rows, show_rows = [], []
    for s in candidates:
        f = facts.build_facts(snap, facts.ALL, set(), only=s["gsis_id"])
        if not f["players"]:
            continue
        claims = {c["id"]: c for c in f["claims"]}
        p = dict(f["players"][0], _episode_week=week)
        cs = shows.cases(p, claims)
        best = max(cs, key=lambda k: cs[k]["fit"]) if cs else None
        fmt = {"detective": "mystery", "contingency": "opportunity"}.get(best, best) if best else \
            editorial.choose_format(p, claims)
        row = editorial.score(p, claims, fmt, hist)
        rows.append((s, row))
        show_rows.append({"slug": s["slug"], "player": p["name"], "total": row["total"], "cases": cs})
    # one show per story, each show once a week; a story that would repeat a stronger one is deferred
    plan = {d["slug"]: d for d in shows.plan_batch(show_rows, hist)}
    week_ep.path("shows.md").write_text(shows.board_markdown(list(plan.values())))
    write_json(week_ep.path("shows.json"), list(plan.values()))
    for s, row in rows:
        d = plan.get(s["slug"])
        if d and d["verdict"] == "defer":
            row["verdict"], row["reject_reasons"] = "reject", row["reject_reasons"] + d["reasons"][-1:]
        if d and d["show"]:                     # the planned show is the one made (the formats included)
            s["show"] = d["show"]
    week_ep.path("editorial.md").write_text(editorial.board_markdown([r for _, r in rows]))
    quota = cfg["shorts"]["risers"] + cfg["shorts"]["fallers"]
    keep = [s for s, r in sorted(rows, key=lambda x: -x[1]["total"]) if r["verdict"] == "make"][:quota]
    return [{**s, "order": i} for i, s in enumerate(keep)]


def make_shorts(cfg: Config, season: int, week: int, force: set[str], only: str | None,
                hook: str | None = None, slug_suffix: str = "", presenter_mode: str = "off",
                style: str = "original", show: str | None = None) -> int:
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
        planned = {d["slug"]: d["show"] for d in read_json(week_ep.path("shows.json"))}
        if stories and planned.get(stories[0]["slug"]):
            stories[0]["show"] = planned[stories[0]["slug"]]
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
        if show:             # an experiment: this story as a given show
            story = {**story, "show": show}
        if not ep.path("story.json").exists() or read_json(ep.path("story.json")) != story:
            write_json(ep.path("story.json"), story)
        run_stages(ep, SHORT, hook or cfg["shorts"].get("hook") or None)
        passed = finish(ep)
        styled = render_style(ep, style) if style != "original" else None   # next to final.mp4, never over it
        if passed and presenter_mode != "off":       # optional AI host layer; never touches final.mp4
            from . import presenter
            presenter.present(ep, presenter_mode)
        if passed:
            editorial_media(ep, cfg)
        results.append((ep, story, passed, styled))
    index = ["# Shorts for review", "", f"Week {week}, {season}. Watch each, read its review.md, then publish by hand.", ""]
    for ep, story, passed, styled in results:
        meta = read_json(ep.path("metadata.json"))
        line = (f"- **{story['name']}** ({story['role']}): {meta['title']}. QC {'passed' if passed else 'FAILED'}. "
                f"`{ep.id}/final.mp4`, `{ep.id}/review.md`")
        if styled:
            line += (f". Styled ({styled[0]}): QC {'passed' if styled[1] else 'FAILED'}, "
                     f"`{ep.id}/styles/{styled[0]}/final.mp4`")
        index.append(line)
    week_ep.path("review.md").write_text("\n".join(index) + "\n")
    failed = [ep.id for ep, _, ok, _ in results if not ok]
    styled_failed = [ep.id for ep, _, _, st in results if st and not st[1]]
    if styled_failed:
        print(f"styled render QC failed (original final.mp4 unaffected): {styled_failed}")
    print(f"\n{len(results) - len(failed)}/{len(results)} Shorts passed QC. Index: {week_ep.path('review.md')}")
    return 1 if failed else 0


def make_flame_praise(cfg: Config, season: int, week: int, force: set[str], only: str | None,
                      hook: str | None = None, treatment: str | None = None, storyboard: bool = False,
                      variant: str | None = None, compare_mix: bool = False, frozen_snapshot: bool = False) -> int:
    """The latest game's strongest flame (a historically bad usage or efficiency line) and praise (a monster one),
    chosen by stages/flame.py and told by stages/showrunner.py in the format that fits the story; the candidate
    board goes to flame_praise.md in the week's folder. --storyboard stops after the brief and script and writes
    storyboard.png (no narration, no render). --variant writes <slug>-<variant> as an experiment beside the
    original, kept out of the channel history."""
    week_ep = Episode(season, week, "short", cfg, force=force)
    week_ep.dir.mkdir(parents=True, exist_ok=True)
    if not (frozen_snapshot and week_ep.path("snapshot.json").exists()):
        snapshot.run(week_ep)
    else:                       # re-make an earlier week's Shorts from its archived data, not today's database
        print(f"  snapshot: frozen ({week_ep.path('snapshot.json')})")
    snap = read_json(week_ep.path("snapshot.json"))
    picks = flame.pick(snap, only)
    week_ep.path("flame_praise.md").write_text(flame.board_markdown(snap, flame.candidates(snap), picks))
    if not picks:
        print(f"no flame or praise case{' for ' + repr(only) if only else ''}; see {week_ep.path('flame_praise.md')}")
        return 2
    results, recent = [], []
    for c in picks:
        slug = c["slug"] + (f"-{variant}" if variant else "")
        ep = Episode(season, week, "short", cfg, force=force, slug=slug)
        ep.dir.mkdir(parents=True, exist_ok=True)
        print(f"\nshort {ep.id}: {c['name']} ({c['kind']}, {c['lead']['metric']})")
        src, dst = week_ep.path("snapshot.json"), ep.path("snapshot.json")
        if not dst.exists() or sha256_file(dst) != sha256_file(src):
            shutil.copyfile(src, dst)
        story_json = {"gsis_id": c["gsis_id"], "name": c["name"], "role": c["kind"], "team": c["team"],
                      "slug": slug, "order": c["order"], "format": "flame_praise", "treatment": treatment,
                      "recent_series": recent}
        if variant:                                # an experiment beside the original, not a new episode
            story_json["variant_of"] = f"{season}-w{week:02d}-short-{c['slug']}"
        if not ep.path("story.json").exists() or read_json(ep.path("story.json")) != story_json:
            write_json(ep.path("story.json"), story_json)
        stages = [("facts", flame.facts_run), ("script", lambda e: flame.script_run(e, hook))]
        if not storyboard:
            stages += [("tts", tts.run), ("render", render_motion.run), ("thumbnail", thumbnail.run),
                       ("metadata", metadata.run)]
        try:
            for name, run in stages:
                t0 = time.time()
                run(ep)
                ep.log(f"  [{name} {time.time() - t0:.1f}s]")
        except flame.NotEntertaining as exc:
            print(f"  skipped: fails the entertainment test ({exc}); see {ep.path('brief.md')}")
            continue
        if read_json(ep.path("brief.json")).get("series"):
            recent.append(read_json(ep.path("script.json"))["structure"])
        if storyboard:
            from .stages import showrunner
            print(f"  storyboard: {showrunner.storyboard(ep)}")
            continue
        if compare_mix and read_json(ep.path("script.json"))["structure"].get("series"):
            from . import series_audio          # before QC, so the pacing gate can hear each effect in the mix
            print(f"  comparison mix without meme effects: {series_audio.write_nosfx(ep)}")
        results.append((ep, c, finish(ep)))
        if results[-1][2]:
            editorial_media(ep, cfg)
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
    ap.add_argument("--treatment", help="flame/praise only: force a format (roast_monologue, courtroom, comparison, "
                                       "main_character, fan_meltdown; classic = the first template)")
    ap.add_argument("--variant", help="flame/praise only: write <slug>-<variant> beside the original, out of history")
    ap.add_argument("--storyboard", action="store_true",
                    help="flame/praise only: brief, script and storyboard.png, no narration or render")
    ap.add_argument("--frozen-snapshot", action="store_true",
                    help="flame/praise only: reuse the week's archived snapshot.json instead of rebuilding it")
    ap.add_argument("--compare-mix", action="store_true",
                    help="flame/praise only: also write final_nosfx.mp4 (same picture, no meme effects)")
    ap.add_argument("--editorial-media", choices=["off", "preview", "render"],
                    help="Shorts only, EXPERIMENTAL: also write <slug>-editorial, the Short re-edited with real "
                         "photography (preview = storyboard only). Default: config.toml [editorial_media] mode")
    ap.add_argument("--show", choices=["red_flag", "heist", "myth"],
                    help="Shorts only, for creative experiments: tell --player's story as this show, written to "
                         "<slug>-show-<name> (a variant: production history is not rewritten)")
    ap.add_argument("--presenter", choices=["off", "hook", "hybrid"],
                    help="Shorts only, EXPERIMENTAL: also write final_presenter_<mode>.mp4 with the AI host "
                         "(default: config.toml [presenter] mode, which is off)")
    from .graphics import CHOICES
    ap.add_argument("--style", choices=CHOICES,
                    help="Shorts only: also render this graphic style to styles/<style>/final.mp4 (default: "
                         "config.toml [graphics] style; 'original' renders only the classic final.mp4)")
    args = ap.parse_args(argv)
    cfg = Config.load()
    if args.editorial_media:
        cfg.raw["editorial_media"] = {**(cfg.raw.get("editorial_media") or {}), "mode": args.editorial_media}
    presenter_mode = args.presenter or (cfg.raw.get("presenter") or {}).get("mode", "off")
    if args.voice:
        cfg.raw["shorts"] = {**cfg.raw["shorts"], "voice_clone": args.voice}
    season = args.season or cfg.season
    force = {s.strip() for s in args.force.split(",") if s.strip()}
    if args.flame_praise:
        return make_flame_praise(cfg, season, args.week, force, args.player, args.hook, args.treatment,
                                 args.storyboard, args.variant, args.compare_mix, args.frozen_snapshot)
    if args.format == "short":
        suffix = ((f"-hook-{args.hook}" if args.hook else "") + (f"-voice-{args.voice}" if args.voice else "")
                  + (f"-show-{args.show}" if args.show else ""))
        style = args.style or (cfg.raw.get("graphics") or {}).get("style", "original")
        return make_shorts(cfg, season, args.week, force, args.player, args.hook, suffix, presenter_mode, style,
                           args.show)
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
