"""Model registry dry runs (app/analytics/model_registry.py). Read-only: never promotes, activates or
rolls back a model, never writes the committed registry, artifacts or ledgers.

    cd backend
    uv run python ../scripts/model_registry.py status                      # production + every candidate
    uv run python ../scripts/model_registry.py evaluate --proposed new.json  # gate a proposed manifest
    uv run python ../scripts/model_registry.py plan-rollback --reason "..." --by NAME --out proposal.json
    uv run python ../scripts/model_registry.py pin-artifact --version ifp-1.0.0 --reason "..." --by NAME --out pinned.json
    uv run python ../scripts/model_registry.py rollback-dry-run           # what a rollback restores, per week

`status` checks the approved production model (and that the projection switch reads exactly it) and
reviews every other registered model against the promotion checks. `evaluate` judges a proposed
manifest (a future promotion) against the committed one. `plan-rollback` writes the manifest a
rollback would produce to a NEW file (write-once) for review; applying it is a reviewed code change.
`pin-artifact` writes the manifest that pins a model's artifact to the exact bytes production served (the
file's sha256 must equal the artifact_id the newest stored run of that version recorded under the live league
scoring and season, among runs whose rows verify against their content hash) to a NEW
file; copying it into model_registry.json is the reviewed change. `rollback-dry-run` reports what a rollback
of the current production model would restore, what is missing, and which run every consumer would see per
week before and after. The live database is only ever opened read-only.
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
from app.analytics.production_pin import APPROVED_ARTIFACT_SHA256 as CONSUMER_PIN
from app.analytics.production_pin import PINNED_MODEL_VERSION as CONSUMER_PINNED_VERSION
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


def _ro(db: Path):
    return duckdb.connect(str(db), read_only=True) if db.exists() else None


def _recorded_runs(db: Path, version: str, season: int, fp: str | None) -> list[dict]:
    """Pin evidence (model_registry.recorded_runs_for_pin), read from the live DB opened read-only."""
    con = _ro(db)
    if con is None:
        return []
    try:
        return reg.recorded_runs_for_pin(con, version, season, fp)
    finally:
        con.close()


def _rollback_dry_run(m: dict, args, fp: str | None) -> dict:
    con = _ro(args.live_db)
    try:
        weeks = set(args.weeks or [])
        if con is not None and not weeks:
            weeks = {r[0] for r in con.execute("SELECT DISTINCT week FROM independent_projection_runs WHERE season = ?",
                                               [args.season]).fetchall()}
            weeks |= {r[0] for r in con.execute("SELECT current_week FROM leagues WHERE season = ? "
                                                "ORDER BY synced_at DESC LIMIT 1", [args.season]).fetchall()}
        return reg.rollback_dry_run(m, model_root=args.models, live_scoring_fp=fp, con=con, season=args.season,
                                    weeks=weeks)
    finally:
        if con is not None:
            con.close()


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["status", "evaluate", "plan-rollback", "pin-artifact", "rollback-dry-run"])
    ap.add_argument("--manifest", type=Path, default=reg.MANIFEST_PATH, help="the committed registry")
    ap.add_argument("--proposed", type=Path, help="evaluate: the proposed manifest")
    ap.add_argument("--models", type=Path, default=_path("INDEPENDENT_MODEL_DIR", "data/models"))
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--reason", help="plan-rollback / pin-artifact: why")
    ap.add_argument("--by", help="plan-rollback / pin-artifact: who requests it")
    ap.add_argument("--version", help="pin-artifact: the registered model whose artifact to pin")
    ap.add_argument("--weeks", type=int, nargs="*", help="rollback-dry-run: weeks to simulate (default: every week "
                                                         "with a stored run, plus the current week)")
    ap.add_argument("--out", type=Path, help="write the result to this NEW file (never overwritten)")
    args = ap.parse_args(argv)
    try:
        m = reg.load_manifest(args.manifest)
        fp = _live_fingerprint(args.live_db, args.season)
        if args.command == "status":
            result = reg.dry_run_report(m, model_root=args.models, live_scoring_fp=fp, consumer_version=CONSUMER_VERSION,
                                        consumer_artifact_sha256=CONSUMER_PIN,
                                        consumer_pinned_version=CONSUMER_PINNED_VERSION)
        elif args.command == "rollback-dry-run":
            result = _rollback_dry_run(m, args, fp)
        elif args.command == "pin-artifact":
            if not (args.version and args.out):
                print("--version and --out are required (a proposal is written to a new file for review)",
                      file=sys.stderr)
                return 2
            new, result = reg.pin_artifact(m, args.version, model_root=args.models, live_scoring_fp=fp,
                                           recorded_runs=_recorded_runs(args.live_db, args.version, args.season, fp),
                                           by=args.by or "", at=datetime.now(timezone.utc), reason=args.reason or "")
            result = {**result, "proposal": str(reg.write_manifest(new, args.out))}
            args.out = None
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
    except (reg.RegistryError, duckdb.Error, OSError, ValueError, TypeError, AttributeError, KeyError) as err:
        print(str(err), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
