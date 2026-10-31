"""trends: social trend intelligence for the video generator.

    uv run trends run                      # collect -> rank -> ideas (the usual command)
    uv run trends collect [--only reddit,bluesky] [--force] [--optional tiktok]
    uv run trends health                   # per-source status, cooldowns, last errors
    uv run trends rank                     # re-cluster and re-score the last 72 h
    uv run trends list [--player dart] [--team NYG] [--min-score 40]
    uv run trends show 1                   # one trend: score breakdown, posts, analytics, angles
    uv run trends ideas [--top 8]          # generate + save ideas from the latest ranking
    uv run trends idea <idea_id>           # full research package for one idea
    uv run trends select <idea_id>         # hand it to the studio (writes the package, prints the command)
    uv run trends export --out bundle.json # versioned JSON contract bundle (for soccer / other repos)
    uv run trends schema                   # the contract's JSON Schema
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from social_intel import ideas as ideas_mod
from social_intel.collectors import REGISTRY
from social_intel.contract import CONTRACT_VERSION, iso, json_schema, utcnow
from social_intel.core.store import Store
from social_intel.engine import DEFAULT_COLLECTORS, Engine

FANTASY_INTEL = Path(__file__).resolve().parents[2]
DATA = Path(os.getenv("SOCIAL_DATA_DIR") or FANTASY_INTEL / "data" / "social")
STUDIO_EPISODES = Path(os.getenv("STUDIO_EPISODES") or FANTASY_INTEL / "studio" / "episodes")


def make_adapter(sport: str, now: datetime | None = None):
    if sport == "nfl":
        from social_intel.sports.football.adapter import FootballAdapter
        return FootballAdapter(entity_cache=DATA / "entities_nfl.json", now=now)
    if sport == "soccer":
        from social_intel.sports.soccer.adapter import SoccerAdapter
        return SoccerAdapter()
    raise SystemExit(f"unknown sport {sport!r}")


def sport_paths(sport: str) -> tuple[Path, Path]:
    """(database, studio history). source_state is keyed by collector, so a second sport needs its own
    database or one sport's cursors and cooldowns would suppress the other's collection."""
    if sport == "nfl":
        return DATA / "social.duckdb", STUDIO_EPISODES / "history.jsonl"
    return DATA / sport / "social.duckdb", STUDIO_EPISODES / sport / "history.jsonl"


def make_engine(args) -> Engine:
    db, history = sport_paths(args.sport)
    store = Store(Path(args.db) if args.db else db)
    return Engine(store, make_adapter(args.sport), history_path=history)


# ---------------------------------------------------------------- printing helpers

def _age(ts: str | None) -> str:
    if not ts:
        return "?"
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    h = (utcnow() - dt).total_seconds() / 3600
    return f"{h:.0f}h" if h >= 1 else f"{h * 60:.0f}m"


def print_ranking(rows: list[dict[str, Any]], limit: int = 20) -> None:
    print(f"{'#':>3} {'score':>5}  {'platforms':<28} {'voices':>6}  {'last':>5}  trend")
    for i, c in enumerate(rows[:limit], 1):
        s = c["score"]
        flags = f"  [{', '.join(s['flags'])}]" if s["flags"] else ""
        print(f"{i:>3} {s['total']:>5.1f}  {','.join(c['platforms'])[:28]:<28} {c['independent_sources']:>6}  "
              f"{_age(c['last_seen']):>5}  {c['label']}{flags}")


def filter_rows(rows, player=None, team=None, min_score=0.0):
    out = []
    for c in rows:
        if c["score"]["total"] < min_score:
            continue
        if team and team.upper() not in (c["entities"].get("teams") or []):
            continue
        if player:
            names = " ".join((c.get("entity_names") or {}).values()).lower()
            if player.lower() not in names and player.lower() not in c["label"].lower():
                continue
        out.append(c)
    return out


def resolve_cluster(rows, ref: str) -> dict[str, Any] | None:
    if ref.isdigit() and 1 <= int(ref) <= len(rows):
        return rows[int(ref) - 1]
    return next((c for c in rows if c["cluster_id"].startswith(ref)), None)


def print_cluster(c: dict[str, Any], related: list[dict[str, Any]]) -> None:
    s = c["score"]
    print(f"\n{c['label']}  —  score {s['total']}  ({c['cluster_id']})")
    print(f"  {s['explanation']}")
    print("\n  Score components (value x weight):")
    for comp in s["components"]:
        bar = "#" * round(comp["value"] * 10)
        print(f"    {comp['name']:<22} {comp['value']:.2f} x {comp['weight']:.2f}  {bar:<10} {comp['detail']}")
    print(f"\n  Platforms: {', '.join(c['platforms'])} | independent voices: {c['independent_sources']} | "
          f"topics: {', '.join(c['topics'][:6])}")
    print("\n  Original posts:")
    for tp in c["top_posts"]:
        eng = ", ".join(f"{k} {v:,}" for k, v in tp["engagement"].items())
        rate = f", {tp['comment_rate_per_h']:.0f} comments/h" if tp.get("comment_rate_per_h") else ""
        print(f"    [{tp['platform']}] {tp['text'][:110]!r}")
        print(f"        {tp['url']}  ({tp.get('community') or ''}{'; ' + eng if eng else ''}{rate}; {_age(tp.get('created_at'))} ago)")
    st = c.get("stance") or {}
    if st:
        print(f"\n  Stance: {st.get('positive', 0)} leaning positive, {st.get('negative', 0)} negative "
              f"(balance {st.get('balance', 0)})")
        for cl in st.get("claims", [])[:4]:
            print(f"    - \"{cl['text'][:140]}\"")
    enr = c.get("enrichment") or {}
    for p in enr.get("players", []):
        print(f"\n  Analytics: {p['name']} ({p.get('position')}, {p.get('team')}) — data cut {p.get('data_cut')}")
        for stt in p.get("stats", [])[:12]:
            print(f"    {stt['label']:<52} {stt['value']!s:>10}   [{stt['source']}]")
        if p.get("injury"):
            i = p["injury"]
            print(f"    injury: {i['report_status']} ({i.get('injury')}), practice {i.get('practice_status')} — {i['source']}")
    for v in enr.get("verdicts", []):
        print(f"\n  Crowd vs data on {v['player']}: crowd {v['social']}, usage {v['data']} -> data {v['relation']} the take")
    if related:
        print("\n  Competing video angles:")
        for i in related:
            print(f"    {i['idea_id']}  [{i['format']}] {i['working_title']}  (confidence {i['research_confidence']})")
            print(f"        hook: {i['hooks'][0]}")


def format_labels(eng: Engine) -> dict[str, str]:
    return getattr(eng.adapter, "format_labels", lambda: ideas_mod.FORMAT_LABEL)()


def print_idea(i: dict[str, Any], labels: dict[str, str]) -> None:
    print(f"\n{i['working_title']}   [{labels.get(i['format'], i['format'])}]")
    print(f"  id {i['idea_id']} | trend score {i['trend_score']} | research confidence {i['research_confidence']} | "
          f"{i['duration_s']} s | relevant {i['relevance_window']}" + (f" | status {i['status']}" if i.get("status") else ""))
    print("\n  Hooks (first 1-3 seconds):")
    for h in i["hooks"]:
        print(f"    - {h}")
    print(f"\n  Why it's trending: {i['why_trending']}")
    print(f"  Angle: {i['editorial_angle']}")
    if i["key_stats"]:
        print("\n  Key stats (every number sourced):")
        for s in i["key_stats"]:
            print(f"    {s['label']:<55} {s['value']!s:>10}   [{s['source']}]")
    for title, rows in (("For", i["supporting_args"]), ("Against", i["opposing_args"])):
        if rows:
            print(f"\n  {title}:")
            for r in rows:
                print(f"    - {r}")
    print(f"\n  Graphics: {'; '.join(i['graphics']) or '-'}")
    print(f"  Memes / SFX: {'; '.join(i['memes'])}")
    print(f"  Comment bait: {i['engagement_question']}")
    print("  Sources:")
    for u in i["source_links"]:
        print(f"    {u}")
    for n in i.get("notes", []):
        print(f"  note: {n}")


# ---------------------------------------------------------------- commands

def cmd_collect(eng: Engine, args) -> int:
    names = args.only.split(",") if args.only else list(DEFAULT_COLLECTORS)
    if args.optional:
        names += args.optional.split(",")
    results = eng.collect(names, force=args.force)
    for r in results:
        detail = f"{r.posts} posts ({r.new_posts} new), {r.requests} req" if r.status == "ok" else r.error
        print(f"  {r.collector:<16} {r.status:<8} {detail}")
    ok = sum(r.status == "ok" for r in results)
    print(f"{ok}/{len(results)} sources ok")
    return 0 if ok or all(r.status == "skipped" for r in results) else 1


def cmd_health(eng: Engine, args) -> int:
    rows = {h["collector"]: h for h in eng.store.health()}
    print(f"{'source':<16} {'last ok':<17} {'fails':>5} {'7d ok':>6} {'posts 24h':>9}  status")
    for name, cls in sorted(REGISTRY.items()):
        h = rows.get(name)
        if not h:
            ok, why = cls().available()
            print(f"{name:<16} {'never':<17} {'':>5} {'':>6} {'':>9}  {'optional, ' if cls.optional else ''}"
                  f"{'not run yet' if ok else why}")
            continue
        last = h["last_success"].astimezone(timezone.utc).strftime("%m-%d %H:%MZ") if h["last_success"] else "never"
        cool = f" cooling until {h['cooldown_until'].astimezone(timezone.utc):%H:%MZ}" if h["cooldown_until"] and h["cooldown_until"] > utcnow() else ""
        err = f" — {h['last_error'][:70]}" if h["last_status"] != "ok" and h["last_error"] else ""
        rate = f"{h['success_rate_7d']:.0%}" if h["success_rate_7d"] is not None else ""
        print(f"{name:<16} {last:<17} {h['consecutive_failures']:>5} {rate:>6} {h['posts_24h'] or 0:>9}  "
              f"{h['last_status']}{cool}{err}")
    return 0


def cmd_rank(eng: Engine, args) -> int:
    rows = eng.rank(since_hours=args.hours)
    print(f"{len(rows)} clusters from the last {args.hours:.0f} h")
    print_ranking(rows, args.limit)
    return 0


def cmd_list(eng: Engine, args) -> int:
    rows = filter_rows(eng.store.latest_ranking(eng.sport), args.player, args.team, args.min_score)
    if args.sort == "fresh":
        rows.sort(key=lambda c: c["last_seen"] or "", reverse=True)
    print_ranking(rows, args.limit)
    return 0


def cmd_show(eng: Engine, args) -> int:
    rows = eng.store.latest_ranking(eng.sport)
    c = resolve_cluster(rows, args.ref)
    if not c:
        print("no such trend; run `trends list`")
        return 2
    related = [i for i in eng.store.ideas(eng.sport) if i["cluster_id"] == c["cluster_id"]]
    if not related:
        related = [i.to_dict() for i in ideas_mod.generate([c], eng.adapter, top=1)]
    print_cluster(c, related)
    return 0


def _hook_history(eng: Engine) -> Counter[str]:
    return Counter()  # rotation state is per batch; previous batches only matter once ideas are sent


def cmd_ideas(eng: Engine, args) -> int:
    rows = eng.store.latest_ranking(eng.sport)
    if not rows:
        print("no ranking yet; run `trends run`")
        return 2
    batch = ideas_mod.generate(rows, eng.adapter, top=args.top, min_score=args.min_score)
    dicts = [i.to_dict() for i in batch]
    fps = {i.idea_id: f"{next((c.get('topic_id') for c in rows if c['cluster_id'] == i.cluster_id), i.cluster_id)}|{i.format}"
           for i in batch}
    eng.store.save_ideas(dicts, fps)
    for d in dicts:
        if args.full:
            print_idea(d, format_labels(eng))
        else:
            print(f"{d['idea_id']}  {d['trend_score']:>5.1f}  conf {d['research_confidence']:.2f}  "
                  f"[{d['format']}] {d['working_title']}\n            hook: {d['hooks'][0]}")
    print(f"\n{len(dicts)} ideas from {len({d['cluster_id'] for d in dicts})} trends. "
          f"`trends idea <id>` for the full package, `trends select <id>` to send one to the studio.")
    return 0


def cmd_idea(eng: Engine, args) -> int:
    i = eng.store.get_idea(args.idea_id)
    if not i:
        print("no such idea")
        return 2
    print_idea(i, format_labels(eng))
    return 0


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")  # the studio's slug rule (facts.pick_stories)


def studio_subjects(idea: dict[str, Any], cluster: dict[str, Any] | None) -> tuple[list[dict[str, Any]], str]:
    """Players the studio could build this idea's Short about, best first, and why the first one. An
    injury idea about a player who is out is told through the replacement (the studio's fill-in story),
    not through the player who isn't playing."""
    enr = (cluster or {}).get("enrichment") or {}
    players = [p for p in enr.get("players") or [] if p.get("name")]
    if not players:
        return [], "no player analytics"
    lead = players[0]
    if idea.get("format") == "injury_ripple" and cluster and \
            ideas_mod.availability(lead, enr.get("upcoming_week"), enr.get("status_cues")) == "out":
        who, _, _ = ideas_mod.replacement(cluster)
        repl = next((p for p in players if p["name"] == who), None)
        if repl is None and who:  # a teammate from our usage data the posts never named
            mate = next((m for m in lead.get("teammates") or [] if m.get("name") == who), None)
            if mate:
                repl = {"name": mate["name"], "gsis_id": mate.get("gsis_id"), "team": lead.get("team"),
                        "position": lead.get("position")}
        if repl:
            return [repl] + [p for p in players if p is not repl], f"{lead['name']} is out: the story is {who}"
    return players, "the idea's lead player"


def research_package(eng: Engine, idea: dict[str, Any]) -> dict[str, Any]:
    rows = eng.store.latest_ranking(eng.sport)
    cluster = next((c for c in rows if c["cluster_id"] == idea["cluster_id"]), None)
    week = (cluster or {}).get("enrichment", {}).get("upcoming_week")
    subjects, why = studio_subjects(idea, cluster)
    first = subjects[0] if subjects else {}
    slug = _slug(first.get("name", "")) if first else ""
    direction = first.get("data_direction")
    handoff = {
        "week": week,
        "player_slug": slug or None,
        "gsis_id": first.get("gsis_id"),
        "role": {"up": "riser", "down": "faller"}.get(direction),
        "subject_reason": why,
        # tried in order when the first player is not a usage story the studio can make this week
        "alternates": [{"slug": _slug(p["name"]), "gsis_id": p.get("gsis_id")} for p in subjects[1:]],
        "command": (f"uv run make-episode --week {week} --trend {{package}}" if slug and week else None),
        "note": ("The studio builds the Short from its own usage data and QC; this package adds the social "
                 "context to review.md. If none of the players qualifies as a usage story this week the studio "
                 "says so, and the package serves as a manual script brief."),
    }
    return {"contract_version": CONTRACT_VERSION, "generated_at": iso(utcnow()), "idea": idea,
            "cluster": cluster, "studio_handoff": handoff}


def cmd_select(eng: Engine, args) -> int:
    i = eng.store.get_idea(args.idea_id)
    if not i:
        print("no such idea")
        return 2
    pkg = research_package(eng, i)
    week = pkg["studio_handoff"]["week"] or 0
    season = eng.adapter.season
    out_dir = STUDIO_EPISODES / f"{season}-w{week:02d}-trends"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{i['idea_id']}.json"
    cmd = pkg["studio_handoff"]["command"]
    if cmd:
        pkg["studio_handoff"]["command"] = cmd.format(package=path)
    path.write_text(json.dumps(pkg, indent=2, default=str))
    eng.store.set_idea_status(i["idea_id"], "selected")
    print(f"research package: {path}")
    if pkg["studio_handoff"]["command"]:
        print(f"next (from fantasy-intel/studio): {pkg['studio_handoff']['command']}")
    else:
        print("no lead player with analytics: use the package as a manual brief")
    return 0


def cmd_export(eng: Engine, args) -> int:
    rows = eng.store.latest_ranking(eng.sport)
    bundle = {
        "contract_version": CONTRACT_VERSION,
        "sport": eng.sport,
        "generated_at": iso(utcnow()),
        "data_label": "fixture" if args.fixture else "live",
        "source_health": json.loads(json.dumps(eng.store.health(), default=str)),
        "clusters": rows[: args.limit],
        "ideas": eng.store.ideas(eng.sport)[: args.limit * 3],
    }
    for i in bundle["ideas"]:
        i.pop("status", None)
    text = json.dumps(bundle, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out} ({len(rows[:args.limit])} clusters, {len(bundle['ideas'])} ideas)")
    else:
        print(text)
    return 0


def cmd_run(eng: Engine, args) -> int:
    print("collect:")
    cmd_collect(eng, args)
    print("\nrank:")
    rows = eng.rank(since_hours=args.hours)
    print_ranking(rows, 15)
    print("\nideas:")
    args.full = False
    return cmd_ideas(eng, args)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="trends", description=__doc__.split("\n")[0])
    ap.add_argument("--sport", default="nfl")
    ap.add_argument("--db", help="social database path (default data/social/social.duckdb)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def collect_args(p):
        p.add_argument("--only", help="comma-separated collectors")
        p.add_argument("--optional", help="also run these optional collectors (tiktok, instagram)")
        p.add_argument("--force", action="store_true", help="ignore refresh intervals and cooldowns")

    p = sub.add_parser("collect"); collect_args(p)
    sub.add_parser("health")
    p = sub.add_parser("rank"); p.add_argument("--hours", type=float, default=72); p.add_argument("--limit", type=int, default=25)
    p = sub.add_parser("list")
    p.add_argument("--player"); p.add_argument("--team"); p.add_argument("--min-score", type=float, default=0)
    p.add_argument("--sort", choices=["score", "fresh"], default="score"); p.add_argument("--limit", type=int, default=25)
    p = sub.add_parser("show"); p.add_argument("ref", help="rank number from `list` or cluster id prefix")
    p = sub.add_parser("ideas"); p.add_argument("--top", type=int, default=8); p.add_argument("--full", action="store_true")
    p.add_argument("--min-score", type=float, default=0)
    p = sub.add_parser("idea"); p.add_argument("idea_id")
    p = sub.add_parser("select"); p.add_argument("idea_id")
    p = sub.add_parser("export"); p.add_argument("--out"); p.add_argument("--limit", type=int, default=25)
    p.add_argument("--fixture", action="store_true", help="label the bundle as fixture data")
    sub.add_parser("schema")
    p = sub.add_parser("run"); collect_args(p)
    p.add_argument("--hours", type=float, default=72); p.add_argument("--top", type=int, default=8)
    p.add_argument("--min-score", type=float, default=0)
    args = ap.parse_args(argv)
    if args.cmd == "schema":
        print(json.dumps(json_schema(), indent=2))
        return 0
    eng = make_engine(args)
    try:
        return {"collect": cmd_collect, "health": cmd_health, "rank": cmd_rank, "list": cmd_list, "show": cmd_show,
                "ideas": cmd_ideas, "idea": cmd_idea, "select": cmd_select, "export": cmd_export,
                "run": cmd_run}[args.cmd](eng, args)
    finally:
        eng.store.close()


if __name__ == "__main__":
    sys.exit(main())
