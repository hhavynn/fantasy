"""Model registry dry runs (app/analytics/model_registry.py). Read-only: never promotes, activates or
rolls back a model, never writes the committed registry, artifacts or ledgers.

    cd backend
    uv run python ../scripts/model_registry.py status                      # production + every candidate
    uv run python ../scripts/model_registry.py evaluate --proposed new.json  # gate a proposed manifest
    uv run python ../scripts/model_registry.py plan-rollback --reason "..." --by NAME --out proposal.json

`status` checks the approved production model (and that the projection switch reads exactly it) and
reviews every other registered model against the promotion checks. `evaluate` judges a proposed
manifest (a future promotion) against the committed one. `plan-rollback` writes the manifest a
rollback would produce to a NEW file (write-once) for review; applying it is a reviewed code change.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import _bootstrap  # noqa: F401
import duckdb

from app.analytics import model_registry as reg
from app.analytics.projection_source import MODEL_VERSION as CONSUMER_VERSION
from app.config import REPO_ROOT, get_settings


def _path(env: str, default: str) -> Path:
    raw = Path(os.getenv(env) or default)
    return raw if raw.is_absolute() else REPO_ROOT / raw


def _live_fingerprint(db: Path, season: int) -> str | None:
    if not db.exists():
        return None
    from app.analytics.independent_projection_store import league_scoring

    con = duckdb.connect(str(db), read_only=True)
    try:
        _, scoring = league_scoring(con, season)
    finally:
        con.close()
    return scoring.fingerprint if scoring.supported else None


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["status", "evaluate", "plan-rollback"])
    ap.add_argument("--manifest", type=Path, default=reg.MANIFEST_PATH, help="the committed registry")
    ap.add_argument("--proposed", type=Path, help="evaluate: the proposed manifest")
    ap.add_argument("--models", type=Path, default=_path("INDEPENDENT_MODEL_DIR", "data/models"))
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--reason", help="plan-rollback: why")
    ap.add_argument("--by", help="plan-rollback: who requests it")
    ap.add_argument("--out", type=Path, help="write the result to this NEW file (never overwritten)")
    args = ap.parse_args(argv)
    try:
        m = reg.load_manifest(args.manifest)
        fp = _live_fingerprint(args.live_db, args.season)
        if args.command == "status":
            result = reg.dry_run_report(m, model_root=args.models, live_scoring_fp=fp, consumer_version=CONSUMER_VERSION)
        elif args.command == "evaluate":
            if not args.proposed:
                print("--proposed is required", file=sys.stderr)
                return 2
            new = json.loads(args.proposed.read_text())
            result = reg.evaluate_promotion(m, new, model_root=args.models, live_scoring_fp=fp)
        else:
            new = reg.plan_rollback(m, reason=args.reason or "", by=args.by or "", at=datetime.now(timezone.utc),
                                    model_root=args.models, live_scoring_fp=fp)
            if not args.out:
                print("--out is required (a proposal is written to a new file for review)", file=sys.stderr)
                return 2
            result = {"proposal": str(reg.write_manifest(new, args.out)), "production": new["production"],
                      "note": "proposal only: nothing was rolled back"}
            args.out = None
        if args.out:
            reg.write_once(args.out, result)
    except (reg.RegistryError, OSError, ValueError, TypeError, AttributeError, KeyError) as err:
        print(str(err), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
