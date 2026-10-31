"""make-soccer: one soccer Short from the explainer library, optionally tied to a trend package.

    uv run make-soccer --list
    uv run make-soccer --explainer offside
    uv run make-soccer --package episodes/soccer/2026-w00-trends/<idea>.json

Output lands in episodes/soccer/. Nothing is uploaded: QC passing makes a Short eligible, and publishing stays a
human decision.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..core import Config, Episode
from ..stages import tts
from . import qc, render, script
from .explainers import EXPLAINERS, for_text


@dataclass(frozen=True)
class SoccerConfig(Config):
    """The NFL configuration with soccer's own episodes folder, so history and output never mix."""

    @property
    def episodes_dir(self) -> Path:
        return super().episodes_dir / "soccer"


def pick(package: dict) -> str | None:
    idea, cluster = package["idea"], package.get("cluster") or {}
    posts = " ".join(p.get("text", "") for p in cluster.get("top_posts") or [])
    return for_text(" ".join((idea["working_title"], " ".join(cluster.get("topics") or []), posts)))


def make(cfg: Config, explainer: str, package: dict | None, force: set[str]) -> bool:
    now = datetime.now(timezone.utc)
    week = now.isocalendar().week
    ep = Episode(now.year if now.month >= 7 else now.year - 1, week, "short", cfg, force=force, slug=explainer)
    ep.dir.mkdir(parents=True, exist_ok=True)
    ep.log(f"{ep.id}")
    script.run(ep, explainer, package)
    tts.run(ep)
    render.run(ep)
    passed = qc.run(ep)
    if passed:
        qc.record_history(ep)
    return passed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="make-soccer", description=__doc__.split("\n")[0])
    ap.add_argument("--explainer", choices=sorted(EXPLAINERS))
    ap.add_argument("--package", type=Path, help="research package from `trends --sport soccer select`")
    ap.add_argument("--force", default="", help="comma-separated stages to rerun, or 'all'")
    ap.add_argument("--list", action="store_true", help="list the explainers")
    args = ap.parse_args(argv)
    if args.list:
        for k, e in EXPLAINERS.items():
            print(f"{k:<10} {e['title']}")
        return 0
    package = json.loads(args.package.read_text()) if args.package else None
    explainer = args.explainer or (pick(package) if package else None)
    if not explainer:
        print("no explainer fits: pass --explainer, or add one to studio/soccer/explainers.py")
        return 2
    cfg = Config.load()
    ok = make(SoccerConfig(cfg.raw, cfg.path), explainer, package, {f for f in args.force.split(",") if f})
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
