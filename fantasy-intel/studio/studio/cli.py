"""make-episode: one command, every stage, outputs in episodes/<episode_id>/.

    uv run make-episode --week 6 --format long
    uv run make-episode --week 6 --format long --force script,tts

Stages skip themselves when their inputs are unchanged, so a rerun only redoes what changed.
"""

from __future__ import annotations

import argparse
import sys
import time

from .core import Config, Episode
from .stages import assemble, charts, facts, metadata, script, snapshot, thumbnail, tts

STAGES = [("snapshot", snapshot), ("facts", facts), ("script", script), ("tts", tts), ("charts", charts),
          ("assemble", assemble), ("thumbnail", thumbnail), ("metadata", metadata)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="make-episode", description=__doc__.split("\n")[0])
    ap.add_argument("--week", type=int, required=True, help="NFL week the episode previews (uses games before it)")
    ap.add_argument("--format", choices=["long", "short"], default="long")
    ap.add_argument("--season", type=int, help="defaults to config.toml season")
    ap.add_argument("--force", default="", help="comma-separated stages to rerun regardless of cache, or 'all'")
    ap.add_argument("--until", choices=[name for name, _ in STAGES], help="stop after this stage")
    args = ap.parse_args(argv)
    if args.format == "short":
        ap.error("the Shorts format arrives in Milestone 5")
    cfg = Config.load()
    ep = Episode(args.season or cfg.season, args.week, args.format, cfg,
                 force={s.strip() for s in args.force.split(",") if s.strip()})
    ep.dir.mkdir(parents=True, exist_ok=True)
    print(f"episode {ep.id} -> {ep.dir}")
    for name, stage in STAGES:
        t0 = time.time()
        stage.run(ep)
        print(f"  [{name} {time.time() - t0:.1f}s]")
        if name == args.until:
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
