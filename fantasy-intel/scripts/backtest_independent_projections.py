"""Independent projection research CLI. Offline only; prints aggregates (no player rows).

    cd backend
    uv run python ../scripts/backtest_independent_projections.py audit
    uv run python ../scripts/backtest_independent_projections.py select      # validation seasons only
    uv run python ../scripts/backtest_independent_projections.py evaluate    # frozen selection -> held-out
    uv run python ../scripts/backtest_independent_projections.py fit         # final model artifact
    uv run python ../scripts/backtest_independent_projections.py participation-select    # validation only
    uv run python ../scripts/backtest_independent_projections.py participation-evaluate  # frozen -> held-out
    uv run python ../scripts/backtest_independent_projections.py participation-fit       # participation artifact
    uv run python ../scripts/backtest_independent_projections.py snap-ablation           # validation only
    uv run python ../scripts/backtest_independent_projections.py project --week 7   # appends one immutable run
    uv run python ../scripts/backtest_independent_projections.py project --week 7 --model ifp-1.0.0   # previous model

Inputs: the live DB (FANTASY_DB_PATH, opened read-only except by `project`) and the optional
history DB from scripts/sync_nfl_history.py (NFL_HISTORY_DB_PATH, default data/nfl_history.duckdb).
Outputs go to data/models/ (git-ignored): selection.json, heldout_report.json, artifact JSON, and for
the participation model participation_selection.json, participation_heldout_report.json and its artifact.
`evaluate` refuses a selection made on other seasons; `fit` refuses an evaluation of another selection;
the participation commands refuse a participation selection made with another conditional selection.

`project` runs the production model (projection_source.MODEL_VERSION, ifp-2.0.0) by default: the newest
weekly-learning artifact for the target week (scripts/weekly_learning.py sync-exits + train --week W,
--weekly-dir, default data/models/weekly) with its play-by-play injury evidence. `--model ifp-1.0.0`
runs the previous model's frozen artifact from --out.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import _bootstrap  # noqa: F401
import duckdb
import polars as pl

from app.analytics.forecast_backtest import (Protocol, build_store, completed_weeks, espn_pregame, evaluate,
                                             league_roster_pairs, select)
from app.analytics.forecast_features import Sources, load_sources
from app.analytics import participation as pm
from app.analytics.independent_projections import MODEL_VERSION, Artifact, fit_final
from app.analytics.league_scoring import reconcile_with_espn
from app.analytics.projection_source import MODEL_VERSION as CONSUMER_VERSION
from app.config import REPO_ROOT, get_settings


def _path(env: str, default: str) -> Path:
    raw = Path(os.getenv(env) or default)
    return raw if raw.is_absolute() else REPO_ROOT / raw


def _ro(path: Path | None):
    return duckdb.connect(str(path), read_only=True) if path is not None and path.exists() else None


def _scoring(live, season):
    from app.analytics.independent_projection_store import league_scoring
    _, scoring = league_scoring(live, season)
    return scoring


def _sources(live, hist, scoring) -> Sources:
    parts = [load_sources(c, scoring) for c in (hist, live) if c is not None]
    if not parts:
        raise SystemExit("No NFL data: provide --live-db and/or --history-db")
    return Sources.concat(parts)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str, allow_nan=False))


def cmd_audit(args, live, hist, scoring) -> dict:
    out: dict = {"scoring": scoring.describe()}
    if live is not None:
        out["scoring_reconciliation"] = reconcile_with_espn(live, args.season, scoring)
        out["espn_projection_history"] = [
            {"snapshot_date": str(d), "weeks": f"{a}-{b}", "rows": n} for d, a, b, n in live.execute(
                "SELECT snapshot_date, min(scoring_period), max(scoring_period), count(*) FROM espn_projection_history "
                "WHERE season = ? GROUP BY 1 ORDER BY 1", [args.season]).fetchall()]
        pregame = espn_pregame(live, args.season)
        done = live.execute("SELECT DISTINCT season, scoring_period FROM espn_player_weekly_points "
                            "WHERE season = ? AND actual_points IS NOT NULL", [args.season]).fetchall()
        done = {(int(s), int(w)) for s, w in done}
        out["espn_pregame_snapshot_player_weeks"] = pregame.height
        out["espn_pregame_with_completed_actual"] = sum(
            (s, w) in done for s, w in pregame.select("season", "week").iter_rows())
        out["id_mapping"] = {f"{m}/{c}": n for m, c, n in live.execute(
            "SELECT match_method, match_confidence, count(*) FROM player_id_map GROUP BY 1, 2 ORDER BY 1").fetchall()}
    if scoring.supported:
        src = _sources(live, hist, scoring)
        p = src.panel
        cov = p.group_by("season").agg(
            pl.len().alias("player_weeks"),
            pl.col("gsis_id").n_unique().alias("players"),
            *[(pl.col("position") == pos).sum().alias(pos) for pos in ("QB", "RB", "WR", "TE")],
            pl.col("league_points").is_not_null().mean().round(4).alias("label_coverage"),
            pl.col("xfp").is_not_null().mean().round(4).alias("xfp_coverage"),
            pl.col("offense_pct").is_not_null().mean().round(4).alias("snap_coverage"),
            pl.col("rz_targets").is_not_null().mean().round(4).alias("pbp_coverage"),
            pl.col("team_targets").is_not_null().mean().round(4).alias("team_week_coverage"),
            pl.col("week").max().alias("max_week")).sort("season")
        out["panel_by_season"] = cov.to_dicts()
        out["defense_weeks_by_season"] = src.defense.group_by("season").len().sort("season").to_dicts()
        out["schedule_games_by_season"] = (src.games.group_by("season").agg(
            (pl.len() / 2).alias("games"), pl.col("points_for").is_not_null().sum().truediv(2).alias("final"))
            .sort("season").to_dicts())
    return out


def cmd_select(args, live, hist, scoring, protocol) -> dict:
    src = _sources(live, hist, scoring.require_supported())
    seasons = list(range(protocol.train_start_season, max(protocol.validation_seasons) + 1))
    t = time.time()
    store = build_store(src, completed_weeks(src, seasons))
    sel = select(store, protocol)
    sel.update(scoring_fingerprint=scoring.fingerprint, created_at=datetime.now(timezone.utc).isoformat(),
               store_rows=store.height, runtime_seconds=round(time.time() - t, 1))
    _dump(args.out / "selection.json", sel)
    return sel


def cmd_evaluate(args, live, hist, scoring, protocol) -> dict:
    sel_path = args.out / "selection.json"
    sel = json.loads(sel_path.read_text())
    if sel.get("scoring_fingerprint") != scoring.fingerprint:
        raise SystemExit("selection.json was made under different league scoring")
    src = _sources(live, hist, scoring.require_supported())
    t = time.time()
    seasons = list(range(protocol.train_start_season, max(protocol.heldout_seasons) + 1))
    store = build_store(src, completed_weeks(src, seasons))
    espn = pl.concat([espn_pregame(live, s) for s in protocol.heldout_seasons]) if live is not None else None
    pairs = set().union(*(league_roster_pairs(live, s) for s in protocol.heldout_seasons)) if live is not None else None
    rep = evaluate(store, sel, protocol, espn=espn, roster_pairs=pairs)
    rep.update(selection_sha=_sha(sel_path), scoring_fingerprint=scoring.fingerprint,
               created_at=datetime.now(timezone.utc).isoformat(), runtime_seconds=round(time.time() - t, 1),
               data_source="real nflverse + league data" if not args.synthetic_label else "SYNTHETIC")
    _dump(args.out / "heldout_report.json", rep)
    return rep


def cmd_fit(args, live, hist, scoring, protocol) -> dict:
    sel_path, rep_path = args.out / "selection.json", args.out / "heldout_report.json"
    sel, rep = json.loads(sel_path.read_text()), json.loads(rep_path.read_text())
    if rep.get("selection_sha") != _sha(sel_path):
        raise SystemExit("heldout_report.json does not evaluate the current selection.json; run evaluate first")
    src = _sources(live, hist, scoring.require_supported())
    seasons = list(range(protocol.train_start_season, max(protocol.heldout_seasons) + 1))
    t = time.time()
    store = build_store(src, completed_weeks(src, seasons))
    art = fit_final(store, sel, rep, scoring.describe(), datetime.now(timezone.utc).isoformat(),
                    protocol.train_start_season)
    path = args.out / f"independent_projections_{MODEL_VERSION}.json"
    _dump(path, art.to_dict())
    return {"artifact": str(path), "bytes": path.stat().st_size, "runtime_seconds": round(time.time() - t, 1),
            "status": art.status, "trained_rows": {p: m.n_train for p, m in art.models.items()},
            "train_window": {p: m.train_window for p, m in art.models.items()}}


def _participation_stores(src, protocol, last_season):
    weeks = completed_weeks(src, list(range(protocol.train_start_season, last_season + 1)))
    return build_store(src, weeks), pm.build_store(src, weeks)


def cmd_participation_select(args, live, hist, scoring, protocol) -> dict:
    sel_path = args.out / "selection.json"
    ifp = json.loads(sel_path.read_text())
    if ifp.get("scoring_fingerprint") != scoring.fingerprint:
        raise SystemExit("selection.json was made under different league scoring")
    src = _sources(live, hist, scoring.require_supported())
    t = time.time()
    cond_store, store = _participation_stores(src, protocol, max(protocol.validation_seasons))
    sel = pm.select(store, cond_store, ifp, protocol)
    sel.update(ifp_selection_sha=_sha(sel_path), scoring_fingerprint=scoring.fingerprint,
               created_at=datetime.now(timezone.utc).isoformat(), store_rows=store.height,
               snap_drop_diagnostic_train_and_validation=pm.snap_drop_diagnostic(store),
               runtime_seconds=round(time.time() - t, 1))
    _dump(args.out / "participation_selection.json", sel)
    return sel


def cmd_participation_evaluate(args, live, hist, scoring, protocol) -> dict:
    sel_path, psel_path = args.out / "selection.json", args.out / "participation_selection.json"
    ifp, psel = json.loads(sel_path.read_text()), json.loads(psel_path.read_text())
    if psel.get("ifp_selection_sha") != _sha(sel_path):
        raise SystemExit("participation_selection.json was made with another selection.json; rerun participation-select")
    src = _sources(live, hist, scoring.require_supported())
    t = time.time()
    cond_store, store = _participation_stores(src, protocol, max(protocol.heldout_seasons))
    rep = pm.evaluate(store, cond_store, psel, ifp, protocol)
    rep.update(participation_selection_sha=_sha(psel_path), created_at=datetime.now(timezone.utc).isoformat(),
               runtime_seconds=round(time.time() - t, 1),
               data_source="real nflverse + league data" if not args.synthetic_label else "SYNTHETIC")
    _dump(args.out / "participation_heldout_report.json", rep)
    return rep


def cmd_participation_fit(args, live, hist, scoring, protocol) -> dict:
    psel_path, rep_path = args.out / "participation_selection.json", args.out / "participation_heldout_report.json"
    psel, rep = json.loads(psel_path.read_text()), json.loads(rep_path.read_text())
    if rep.get("participation_selection_sha") != _sha(psel_path):
        raise SystemExit("participation_heldout_report.json does not evaluate the current participation selection")
    src = _sources(live, hist, scoring.require_supported())
    t = time.time()
    _, store = _participation_stores(src, protocol, max(protocol.heldout_seasons))
    art = pm.fit_final(store, psel, rep, datetime.now(timezone.utc).isoformat(), protocol.train_start_season)
    path = args.out / f"participation_{pm.PARTICIPATION_VERSION}.json"
    _dump(path, art.to_dict())
    return {"artifact": str(path), "bytes": path.stat().st_size, "runtime_seconds": round(time.time() - t, 1),
            "status": art.status, "trained_rows": {p: m.n_train for p, m in art.models.items()},
            "converged": {p: m.converged for p, m in art.models.items()}}


def cmd_snap_ablation(args, live, hist, scoring, protocol) -> dict:
    from app.analytics.forecast_backtest import snap_split_ablation
    ifp = json.loads((args.out / "selection.json").read_text())
    src = _sources(live, hist, scoring.require_supported())
    weeks = completed_weeks(src, list(range(protocol.train_start_season, max(protocol.validation_seasons) + 1)))
    return snap_split_ablation(build_store(src, weeks), ifp, protocol)


def _project_artifact(args, scoring) -> tuple[Artifact, bytes, pl.DataFrame | None, Path | None]:
    """(artifact, its file bytes, play-by-play exits) for `project --model`. ifp-1.0.0: the frozen artifact in
    --out. ifp-2.0.0: the newest weekly-learning artifact for the target week, which weekly_learning.latest_artifact
    checks was trained under the league's scoring on games before the week, plus the exits it was trained with.
    The fourth value is that artifact's path (None for ifp-1.0.0)."""
    if args.model == MODEL_VERSION:
        data = (args.out / f"independent_projections_{MODEL_VERSION}.json").read_bytes()
        return Artifact.from_dict(json.loads(data)), data, None, None
    from app.analytics import weekly_learning as wl
    from app.nfl import pbp_exits
    try:
        art, _, path = wl.latest_artifact(args.weekly_dir, args.model, args.season, args.week, scoring.fingerprint)
    except wl.NotReady as err:
        raise ValueError(f"{err} (scripts/weekly_learning.py train --week {args.week})") from err
    if art.config["provenance"].get("retrospective"):
        raise ValueError(f"the newest {args.model} artifact for week {args.week} ({path.name}) is a retrospective "
                         "replay; train a pregame artifact (scripts/weekly_learning.py train --week "
                         f"{args.week}) before kickoff")
    exits = pbp_exits.load_dir(args.weekly_dir / "pbp_exits")
    if exits is None and art.config["provenance"].get("pbp_exits_through_week") is not None:
        raise ValueError(f"{path.name} was trained with play-by-play injury evidence, but {args.weekly_dir / 'pbp_exits'} "
                         "has none (scripts/weekly_learning.py sync-exits)")
    return art, path.read_bytes(), exits, path


def cmd_project(args, hist, scoring) -> dict:
    from app.analytics.independent_projection_store import build_live, write_run
    from app.db.connection import open_db

    art, art_bytes, exits, art_path = _project_artifact(args, scoring.require_supported())
    # Run provenance: sha256 of the artifact FILE bytes (the digest model_registry.json pins).
    art_sha = hashlib.sha256(art_bytes).hexdigest()
    ppath = args.out / f"participation_{pm.PARTICIPATION_VERSION}.json"
    part = pm.ParticipationArtifact.from_dict(json.loads(ppath.read_text())) if ppath.exists() else None
    with open_db(args.live_db) as con:
        src = _sources(con, hist, scoring).with_exits(exits)
        t = time.time()
        registered = None
        if args.register_artifact:       # ifp-2.0.0 only: approve the write-once store artifact this run uses
            if art_path is None:
                raise ValueError("--register-artifact applies to the weekly-trained model (ifp-2.0.0) only; ifp-1.0.0 "
                                 "is pinned in the registry and production_pin.py")
            from app.analytics import weekly_learning as wl
            registered = wl.register_saved_artifact(con, art, art_path, args.weekly_dir, args.register_artifact)
        meta, rows = build_live(con, src, art, args.season, args.week, participation=part, artifact_sha256=art_sha)
        stored = write_run(con, meta, rows)   # append-only: an identical retry returns the existing run
    by = {}
    for r in rows:
        by[r["coverage_status"]] = by.get(r["coverage_status"], 0) + 1
    return {"run_id": stored["run_id"], "created": stored["created"], "rows": stored["row_count"],
            "model_version": art.version, "artifact_id": art_sha, "artifact_registered": registered and
            {"sha256": registered[0], "created": registered[1], "source": args.register_artifact},
            "content_sha256": stored["content_sha256"], "coverage": by, "runtime_seconds": round(time.time() - t, 2),
            "participation": meta["participation_version"],
            "with_expected_points": sum(r["expected_points"] is not None for r in rows),
            "data_through": [meta["data_through_season"], meta["data_through_week"]]}


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["audit", "select", "evaluate", "fit", "project", "participation-select",
                                            "participation-evaluate", "participation-fit", "snap-ablation"])
    parser.add_argument("--live-db", type=Path, default=settings.db_path)
    parser.add_argument("--history-db", type=Path, default=_path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    parser.add_argument("--out", type=Path, default=_path("INDEPENDENT_MODEL_DIR", "data/models"))
    parser.add_argument("--season", type=int, default=settings.nfl_season, help="live season")
    parser.add_argument("--week", type=int, help="target NFL week (project)")
    parser.add_argument("--model", choices=[CONSUMER_VERSION, MODEL_VERSION], default=CONSUMER_VERSION,
                        help="model `project` runs (default: the production model the projection switch reads)")
    parser.add_argument("--weekly-dir", type=Path, default=_path("WEEKLY_MODEL_DIR", "data/models/weekly"),
                        help="weekly-learning output (ifp-2 artifacts and pbp_exits), as in weekly_learning.py --out")
    parser.add_argument("--register-artifact", choices=["learning_cycle", "manual_pin"],
                        help="project --model ifp-2.0.0: record the weekly-store artifact this run uses in the live "
                             "database's approved_artifacts (write-once); the consumer view serves only registered "
                             "ifp-2.0.0 artifacts. The learning cycle passes learning_cycle; a manual pin is manual_pin")
    parser.add_argument("--synthetic-label", action="store_true", help="mark reports as synthetic-data runs")
    args = parser.parse_args(argv)
    protocol = Protocol(heldout_seasons=(2025, args.season) if args.season > 2025 else (2025,))
    hist = _ro(args.history_db)
    live = _ro(args.live_db) if args.command != "project" else None
    try:
        scoring_con = live or (_ro(args.live_db) if args.command == "project" else None)
        if scoring_con is None:
            print("A live database with league scoring settings is required.", file=sys.stderr)
            return 2
        scoring = _scoring(scoring_con, args.season)
        if args.command == "project":
            scoring_con.close()
            if not args.week:
                print("--week is required for project", file=sys.stderr)
                return 2
            result = cmd_project(args, hist, scoring)
        elif args.command == "audit":
            result = cmd_audit(args, live, hist, scoring)
        else:
            result = {"select": cmd_select, "evaluate": cmd_evaluate, "fit": cmd_fit,
                      "participation-select": cmd_participation_select,
                      "participation-evaluate": cmd_participation_evaluate,
                      "participation-fit": cmd_participation_fit, "snap-ablation": cmd_snap_ablation}[args.command](
                args, live, hist, scoring, protocol)
    except (ValueError, FileNotFoundError) as err:
        print(str(err), file=sys.stderr)
        return 2
    finally:
        for c in (hist, live):
            if c is not None:
                c.close()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
