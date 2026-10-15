"""Weekly learning CLI for the independent projection engine (champion ifp-1.0.0 refit + shadow
challenger ifp-2.0.0). Never writes the live database or the production model files.

    cd backend
    uv run python ../scripts/sync_nfl.py                                   # 1. nflverse (existing; Tuesday)
    uv run python ../scripts/weekly_learning.py sync-exits                 # 2. play-by-play injury evidence
    uv run python ../scripts/weekly_learning.py status  --week 7           # 3. readiness (read-only)
    uv run python ../scripts/weekly_learning.py train   --week 7           # 4. versioned artifacts
    uv run python ../scripts/weekly_learning.py predict --week 7           # 5. frozen pregame forecasts
    uv run python ../scripts/weekly_learning.py evaluate                   # 6. prospective accuracy
    uv run python ../scripts/weekly_learning.py cycle   --week 7           # 2-6 in order, stops on any error

Inputs: the live DB (FANTASY_DB_PATH, read-only here), the history DB (NFL_HISTORY_DB_PATH), and
the production ifp-1.0.0 files in --ifp-dir (selection.json, heldout_report.json and the
participation artifact; read-only). Outputs: --out (default data/models/weekly, git-ignored):
pbp_exits/<season>.parquet, artifacts/<version>/s<season>w<week>-<id>.json, ledger/s<season>w<week>/<run>.json,
reports/evaluate-<timestamp>.json.

`train`/`predict` refuse when an earlier game is not final, a completed week lacks box scores,
snaps or xFP, the league scoring differs from an artifact's, or week W has already kicked off
(use --retrospective for a clearly labeled replay that never counts as pregame evidence).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

import _bootstrap  # noqa: F401
import duckdb
import polars as pl

from app.analytics import challenger as ch
from app.analytics import weekly_learning as wl
from app.analytics.forecast_features import Sources, load_sources
from app.analytics.independent_projection_store import league_scoring
from app.config import REPO_ROOT, get_settings
from app.nfl import pbp_exits


def _path(env: str, default: str) -> Path:
    raw = Path(os.getenv(env) or default)
    return raw if raw.is_absolute() else REPO_ROOT / raw


def _ro(path: Path):
    return duckdb.connect(str(path), read_only=True) if path is not None and Path(path).exists() else None


def _now(args) -> datetime:
    return datetime.fromisoformat(args.now).astimezone(timezone.utc) if args.now else datetime.now(timezone.utc)


def _context(args):
    live = _ro(args.live_db)
    if live is None:
        raise wl.NotReady(f"live database {args.live_db} not found")
    hist = _ro(args.history_db)
    _, scoring = league_scoring(live, args.season)
    scoring.require_supported()
    parts = [load_sources(c, scoring) for c in (hist, live) if c is not None]
    src = Sources.concat(parts).with_exits(pbp_exits.load_dir(args.out / "pbp_exits"))
    return live, hist, scoring, src


def cmd_sync_exits(args, **_) -> dict:
    live = _ro(args.live_db)
    hist = _ro(args.history_db)
    out = []
    if args.history_seasons:
        first, last = (int(x) for x in args.history_seasons.split("-"))
        seasons = list(range(first, last + 1))
    else:
        seasons = [args.season]
    for season in seasons:
        con = live if season == args.season else hist
        if con is None:
            raise wl.NotReady(f"no database holds {season} rosters")
        ros = pl.from_arrow(con.execute("SELECT season, week, team, jersey_number, gsis_id, position, last_name "
                                        "FROM nfl_rosters_weekly WHERE season = ?", [season]).arrow())
        done = con.execute("SELECT max(week) FROM nfl_schedules WHERE season = ? AND game_type = 'REG' AND week NOT IN "
                           "(SELECT week FROM nfl_schedules WHERE season = ? AND game_type = 'REG' AND home_score IS NULL)",
                           [season, season]).fetchone()[0]
        out.append(pbp_exits.sync_season(season, ros, args.out / "pbp_exits", max_week=done))
    return {"seasons": out}


def cmd_status(args, src, **_) -> dict:
    return wl.readiness(src, args.season, args.week, _now(args))


def _require_ready(args, src) -> dict:
    rep = wl.readiness(src, args.season, args.week, _now(args))
    if not rep["ok"]:
        raise wl.NotReady("; ".join(rep["errors"]))
    if not rep["pregame"] and not args.retrospective:
        raise wl.NotReady(f"week {args.week} kicked off at {rep['first_kickoff']}: a model trained or a forecast made "
                          "now is not pregame evidence (use --retrospective for a labeled replay)")
    return rep


def cmd_train(args, src, scoring, **_) -> dict:
    _require_ready(args, src)
    sel_path, rep_path = args.ifp_dir / "selection.json", args.ifp_dir / "heldout_report.json"
    sel, rep = json.loads(sel_path.read_text()), json.loads(rep_path.read_text())
    import hashlib
    if rep.get("selection_sha") != hashlib.sha256(sel_path.read_bytes()).hexdigest()[:16]:
        raise wl.NotReady("heldout_report.json does not evaluate the current selection.json")
    if sel.get("scoring_fingerprint") != scoring.fingerprint:
        raise wl.NotReady("ifp-1.0.0 selection was made under different league scoring")
    t = time.time()
    champ, chal = wl.train(src, args.season, args.week, ifp_selection=sel, ifp_report=rep, cfg=ch.ChallengerConfig.load(),
                           scoring_desc=scoring.describe(), created_at=_now(args).isoformat(),
                           retrospective=args.retrospective)
    res = {}
    for art in (champ, chal):
        aid, path, created = wl.save_artifact(art, args.out)
        res[art.version] = {"id": aid, "path": str(path), "new": created, **{
            k: art.config["provenance"][k] for k in ("latest_included_week", "training_seasons", "rows", "data_hash",
                                                     "config_hash", "retrospective")}}
    res["runtime_seconds"] = round(time.time() - t, 1)
    return res


def cmd_predict(args, live, src, scoring, **_) -> dict:
    rep = _require_ready(args, src)
    from app.analytics.participation import PARTICIPATION_VERSION, ParticipationArtifact
    champ, cid, cpath = wl.latest_artifact(args.out, wl.MODEL_VERSION, args.season, args.week, scoring.fingerprint)
    chal, xid, xpath = wl.latest_artifact(args.out, ch.CHALLENGER_VERSION, args.season, args.week, scoring.fingerprint)
    for art in (champ, chal):
        if bool(art.config["provenance"]["retrospective"]) != bool(args.retrospective):
            raise wl.NotReady(f"{art.version} artifact retrospective={art.config['provenance']['retrospective']} does "
                              "not match this run; retrain for the right kind")
    ppath = args.ifp_dir / f"participation_{PARTICIPATION_VERSION}.json"
    # A retrospective replay gets no P(appears): the participation artifact was fitted on later weeks.
    part = ParticipationArtifact.from_dict(json.loads(ppath.read_text())) \
        if ppath.exists() and not args.retrospective else None
    t = time.time()
    run = wl.predict(src, champ, chal, args.season, args.week, _now(args), con=live, participation=part,
                     kind="retrospective" if args.retrospective else "pregame", readiness_report=rep,
                     artifact_ids={"champion": {"version": champ.version, "id": cid, "path": str(cpath)},
                                   "challenger": {"version": chal.version, "id": xid, "path": str(xpath)}})
    path, created = wl.write_run(run, args.out)
    return {"run_id": run["run_id"], "path": str(path), "new": created, "rows": len(run["rows"]),
            "pregame_rows": sum(r["pregame"] for r in run["rows"]),
            "with_challenger": sum(r["challenger"]["points"] is not None for r in run["rows"]),
            "runtime_seconds": round(time.time() - t, 2)}


def cmd_evaluate(args, src, **_) -> dict:
    runs = wl.load_ledger(args.out)
    rep = {"pregame": wl.evaluate(runs, src, "pregame"),
           "retrospective_not_evidence": wl.evaluate(runs, src, "retrospective")}
    d = args.out / "reports"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"evaluate-{_now(args):%Y%m%dT%H%M%S}.json"
    path.write_text(json.dumps(rep, indent=1, default=str))
    p = rep["pregame"]
    return {"report": str(path), "pregame_weeks": p.get("weeks"),
            "preliminary_weeks_not_evidence": (p.get("preliminary") or {}).get("weeks", []),
            "outcomes": p.get("outcomes"),
            "by_position": {pos: {"n": r["n"], "mae": {m: v["mae"] for m, v in r["metrics"].items()},
                                  "diff": r.get("challenger_minus_champion_mae")}
                            for pos, r in p.get("positions", {}).items()},
            "shadow_status": {pos: {"status": s["status"], "lifecycle": s["lifecycle"],
                                    "criteria": [f"{c['name']}: {c['result']} (observed {c['observed']}, required "
                                                 f"{c['required']})" for c in s["criteria"]]}
                              for pos, s in (p.get("shadow_status") or {}).items()},
            "promotion_blockers": p.get("promotion_blockers")}


def cmd_cycle(args, **ctx) -> dict:
    out = {"sync_exits": cmd_sync_exits(args)}
    live, _, scoring, src = _context(args)
    ctx = {"live": live, "src": src, "scoring": scoring}
    out["status"] = cmd_status(args, **ctx)
    out["train"] = cmd_train(args, **ctx)
    out["predict"] = cmd_predict(args, **ctx)
    out["evaluate"] = cmd_evaluate(args, **ctx)
    return out


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["sync-exits", "status", "train", "predict", "evaluate", "cycle"])
    ap.add_argument("--week", type=int)
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--history-seasons", help="sync-exits only, e.g. 2019-2025 (history database rosters)")
    ap.add_argument("--retrospective", action="store_true",
                    help="replay a week that already started; stored as retrospective, never counted as pregame")
    ap.add_argument("--now", help="ISO timestamp to use as the current time (testing and audits)")
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=_path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--ifp-dir", type=Path, default=_path("INDEPENDENT_MODEL_DIR", "data/models"))
    ap.add_argument("--out", type=Path, default=_path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    args = ap.parse_args(argv)
    if args.command in ("status", "train", "predict", "cycle") and not args.week:
        print("--week is required", file=sys.stderr)
        return 2
    try:
        if args.command == "sync-exits":
            result = cmd_sync_exits(args)
        elif args.command == "cycle":
            result = cmd_cycle(args)
        else:
            live, _, scoring, src = _context(args)
            result = {"status": cmd_status, "train": cmd_train, "predict": cmd_predict, "evaluate": cmd_evaluate}[
                args.command](args, live=live, src=src, scoring=scoring)
    except (wl.NotReady, FileNotFoundError, ValueError) as err:
        print(f"NOT READY: {err}", file=sys.stderr)
        return 3
    print(json.dumps(result, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
