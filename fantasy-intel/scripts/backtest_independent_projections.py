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
    uv run python ../scripts/backtest_independent_projections.py project --week 7 --model ifp-2.1.0   # previous model
    uv run python ../scripts/backtest_independent_projections.py project --week 10 --artifact-week 7   # week 10 from week 7's artifact
    uv run python ../scripts/backtest_independent_projections.py project --week 7 --through-week 18   # rest of the season
    uv run python ../scripts/backtest_independent_projections.py reproject   # week in progress, final injury reports

`project` and `reproject` apply the reviewed availability news (app/analytics/availability_news.json: reported
multi-week absences and reinstatements no synced source carries yet; docs/independent_projection_engine.md,
"Availability news"); `--no-news` leaves it out.

Inputs: the live DB (FANTASY_DB_PATH, opened read-only except by `project`) and the optional
history DB from scripts/sync_nfl_history.py (NFL_HISTORY_DB_PATH, default data/nfl_history.duckdb).
Outputs go to data/models/ (git-ignored): selection.json, heldout_report.json, artifact JSON, and for
the participation model participation_selection.json, participation_heldout_report.json and its artifact.
`evaluate` refuses a selection made on other seasons; `fit` refuses an evaluation of another selection;
the participation commands refuse a participation selection made with another conditional selection.

`project` runs the production model (projection_source.MODEL_VERSION, ifp-2.2.0) by default: the newest
weekly-learning artifact for the target week (scripts/weekly_learning.py sync-exits + train --week W,
--weekly-dir, default data/models/weekly) with its play-by-play injury evidence. `--model ifp-2.1.0` runs the
previous (fallback) model's weekly artifact the same way; `--model ifp-1.0.0` the retired model's frozen artifact
from --out.

Rest-of-season projections (docs/independent_projection_engine.md): `--week T --artifact-week W` projects the later
week T with week W's artifact (features as of W, T's opponent; projected_from_week = W, forecast_horizon = T - W);
`--week W --through-week L` does that for every week W..L in one process (one source load, one artifact
registration; a failure on a later week is reported in the output's rest_of_season list and does not stop the others;
a failure on week W itself is an error). Both apply to the weekly-trained models (ifp-2.2.0, ifp-2.1.0, ifp-2.0.0) only. An ifp-2.2.0 artifact
also reads <weekly-dir>/depth_raw (weekly_learning.py sync-depth) and the context tables of both databases.

`reproject` (docs/independent_projection_engine.md, "Final-designation re-projection"; learning_cycle.py reproject runs
it after sync_nfl.py): a NEW run of the week in progress (or --week) made with the artifact FILE of that week's newest
verified production-model run, so the late-week injury reports reach expected_points. Teams whose kickoff has passed
keep their base-run rows (carried, flagged); nothing is written when no re-projected row changed. Exit 2 when it is
refused.
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
import polars as pl

from app.analytics.forecast_backtest import (Protocol, build_store, completed_weeks, espn_pregame, evaluate,
                                             league_roster_pairs, select)
from app.analytics.forecast_features import Sources, load_sources
from app.analytics import participation as pm
from app.analytics.independent_projections import MODEL_VERSION, Artifact, fit_final
from app.analytics.league_scoring import reconcile_with_espn
from app.analytics.projection_source import FALLBACK_VERSION, RETIRED_WEEKLY_VERSION
from app.analytics.projection_source import MODEL_VERSION as CONSUMER_VERSION
from app.config import REPO_ROOT, get_settings
from app.db.connection import connect as db_connect


def _path(env: str, default: str) -> Path:
    raw = Path(os.getenv(env) or default)
    return raw if raw.is_absolute() else REPO_ROOT / raw


def _ro(path: Path | None):
    return db_connect(path, read_only=True) if path is not None and path.exists() else None


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


def _artifact_week(args) -> int | None:
    """The week whose artifact makes the run: --artifact-week, else None (the target week's own artifact)."""
    return getattr(args, "artifact_week", None)


def _project_artifact(args, scoring) -> tuple[Artifact, bytes, pl.DataFrame | None, Path | None]:
    """(artifact, its file bytes, play-by-play exits) for `project --model`. ifp-1.0.0: the frozen artifact in
    --out. ifp-2.2.0 / ifp-2.1.0 / ifp-2.0.0: the newest weekly-learning artifact for the target week, which weekly_learning.latest_artifact
    checks was trained under the league's scoring on games before the week, plus the exits it was trained with.
    The fourth value is that artifact's path (None for ifp-1.0.0)."""
    if args.model == MODEL_VERSION:
        data = (args.out / f"independent_projections_{MODEL_VERSION}.json").read_bytes()
        return Artifact.from_dict(json.loads(data)), data, None, None
    from app.analytics import weekly_learning as wl
    from app.nfl import pbp_exits
    try:
        art, _, path = wl.latest_artifact(args.weekly_dir, args.model, args.season, args.week, scoring.fingerprint,
                                          artifact_week=_artifact_week(args))
    except wl.NotReady as err:
        raise ValueError(f"{err} (scripts/weekly_learning.py train --week {_artifact_week(args) or args.week})") from err
    if art.config["provenance"].get("retrospective"):
        raise ValueError(f"the newest {args.model} artifact for week {_artifact_week(args) or args.week} ({path.name}) "
                         "is a retrospective replay; train a pregame artifact (scripts/weekly_learning.py train --week "
                         f"{_artifact_week(args) or args.week}) before kickoff")
    exits = pbp_exits.load_dir(args.weekly_dir / "pbp_exits")
    if exits is None and art.config["provenance"].get("pbp_exits_through_week") is not None:
        raise ValueError(f"{path.name} was trained with play-by-play injury evidence, but {args.weekly_dir / 'pbp_exits'} "
                         "has none (scripts/weekly_learning.py sync-exits)")
    return art, path.read_bytes(), exits, path


def _context_inputs(args, art, con, hist):
    """ifp-2.2.0 artifacts read the context tables of both databases and the depth snapshots in
    <weekly-dir>/depth_raw (weekly_learning.py sync-depth); None for every other version."""
    from app.analytics import context_model as cm
    if not cm.is_context_artifact(art):
        return None
    return cm.load_inputs([hist, con], args.weekly_dir / "depth_raw", args.season)


def _summary(meta: dict, rows: list[dict], stored: dict, art, art_sha: str, t0: float) -> dict:
    by = {}
    for r in rows:
        by[r["coverage_status"]] = by.get(r["coverage_status"], 0) + 1
    return {"run_id": stored["run_id"], "created": stored["created"], "rows": stored["row_count"],
            "week": meta["week"], "forecast_horizon": meta["forecast_horizon"],
            "projected_from_week": meta["projected_from_week"],
            "model_version": art.version, "artifact_id": art_sha,
            "content_sha256": stored["content_sha256"], "coverage": by, "runtime_seconds": round(time.time() - t0, 2),
            "participation": meta["participation_version"],
            "with_expected_points": sum(r["expected_points"] is not None for r in rows),
            "news_overridden": sorted(r["gsis_id"] for r in rows
                                      if any(str(f).startswith("availability news overridden") for f in r["flags"])),
            "data_through": [meta["data_through_season"], meta["data_through_week"]]}


def cmd_project(args, hist, scoring) -> dict:
    from app.analytics import weekly_learning as wl
    from app.analytics.independent_projection_store import build_live, write_run
    from app.db.connection import open_db

    through = getattr(args, "through_week", None)
    art_week = _artifact_week(args)
    rest_of_season = through is not None or art_week is not None
    if rest_of_season and args.model == MODEL_VERSION:
        raise ValueError("--artifact-week / --through-week apply to the weekly-trained models (ifp-2.2.0, ifp-2.1.0, ifp-2.0.0) only")
    if through is not None:
        if art_week is not None:
            raise ValueError("--through-week projects weeks --week .. --through-week from the artifact of --week; "
                             "do not combine it with --artifact-week")
        if through < args.week:
            raise ValueError(f"--through-week {through} is before --week {args.week}")
        weeks, from_week = list(range(args.week, through + 1)), args.week
        target0 = args.week
    else:
        if art_week is not None and art_week > args.week:
            raise ValueError(f"--artifact-week {art_week} is after the target week {args.week}")
        weeks, from_week = [args.week], (art_week if art_week is not None else None)
        target0 = args.week
    art, art_bytes, exits, art_path = _project_artifact(args, scoring.require_supported())
    news = _news(args)
    # Run provenance: sha256 of the artifact FILE bytes (the digest model_registry.json pins).
    art_sha = hashlib.sha256(art_bytes).hexdigest()
    ppath = args.out / f"participation_{pm.PARTICIPATION_VERSION}.json"
    part = pm.ParticipationArtifact.from_dict(json.loads(ppath.read_text())) if ppath.exists() else None
    # Phase 1, read-only: build every target week's rows. Nothing is written and the DuckDB write lock (which makes the
    # local API answer 503) is not held while the (19 - W) weeks are computed.
    t = time.time()
    built, ros = [], []
    with open_db(args.live_db, read_only=True) as con:
        src = _sources(con, hist, scoring).with_exits(exits)
        ctx_inputs = _context_inputs(args, art, con, hist)
        for target in weeks:
            t0 = time.time()
            try:
                if rest_of_season:     # the same artifact, whatever the horizon: its cutoff must precede the target
                    wl.check_artifact(art, args.season, target, scoring.fingerprint)
                captured: dict = {}     # the rows' full model inputs, stored beside the run (#225 attribution)
                meta, rows = build_live(con, src, art, args.season, target, participation=part,
                                        artifact_sha256=art_sha, projected_from_week=from_week,
                                        context_inputs=ctx_inputs, news=news, inputs_out=captured)
                built.append((target, meta, rows, t0, captured))
            except Exception as err:      # noqa: BLE001 - a later week's failure is reported, the others still run
                if target == target0:
                    raise
                ros.append({"week": target, "forecast_horizon": target - from_week, "error": f"{type(err).__name__}: {err}"})
                print(f"WARNING: rest-of-season projection for week {target} failed: {err}", file=sys.stderr)
    # Phase 2, read-write, opened once: register the artifact, then one write_run (one transaction) per week.
    results = []
    with open_db(args.live_db) as con:
        registered = backfill = None
        if args.register_artifact:       # weekly models only: approve the write-once store artifact this run uses
            if art_path is None:
                raise ValueError("--register-artifact applies to the weekly-trained models (ifp-2.2.0, ifp-2.1.0, ifp-2.0.0) only; "
                                 "ifp-1.0.0 is pinned in the registry and production_pin.py")
            # FIRST every other artifact already in the write-once store (the first run after the upgrade covers the
            # runs those artifacts made; files that are not the store's own are skipped), THEN this run's artifact:
            # the first registration starts enforcement, so it must not precede the backfill.
            try:
                backfill = wl.backfill_store_artifacts(con, args.weekly_dir, art.version, scoring.fingerprint,
                                                       skip=(art_path,))
            except Exception as err:      # noqa: BLE001 - reported, never swallowed
                backfill = {"error": f"{type(err).__name__}: {err}"}
                print(f"WARNING: backfill of the weekly artifact store failed: {backfill['error']}", file=sys.stderr)
                if con.execute("SELECT count(*) FROM approved_artifacts WHERE model_version = ?",
                               [art.version]).fetchone()[0] == 0:
                    raise ValueError("the store backfill failed and no artifact is registered yet, so the current "
                                     "artifact is not registered alone (that would start enforcement with the older "
                                     f"store artifacts unregistered): {backfill['error']}") from err
            registered = wl.register_saved_artifact(con, art, art_path, args.weekly_dir, args.register_artifact)
        for target, meta, rows, t0, captured in built:
            try:
                stored = write_run(con, meta, rows, inputs=captured)   # append-only: an identical retry returns the existing run
                results.append(_summary(meta, rows, stored, art, art_sha, t0))
            except Exception as err:      # noqa: BLE001
                if target == target0:
                    raise
                ros.append({"week": target, "forecast_horizon": target - from_week, "error": f"{type(err).__name__}: {err}"})
                print(f"WARNING: rest-of-season projection for week {target} failed: {err}", file=sys.stderr)
    first = results[0]
    prov = art.config.get("provenance") or {}
    out = {**first, "artifact_trained_after_first_kickoff": bool(prov.get("trained_after_first_kickoff")),
           "artifact_registered": registered and
           {"sha256": registered[0], "created": registered[1], "source": args.register_artifact},
           "artifact_backfill": backfill, "runtime_seconds": round(time.time() - t, 2)}
    if through is not None:
        out["rest_of_season"] = [{k: r[k] for k in ("week", "forecast_horizon", "run_id", "created", "rows", "coverage",
                                                   "with_expected_points")} for r in results[1:]] + ros
        out["rest_of_season"].sort(key=lambda r: r["week"])
        out["rest_of_season_failed"] = [r["week"] for r in ros]
    return out


def _news(args) -> list:
    """The reviewed availability-news entries (app/analytics/availability_news.json) unless --no-news."""
    from app.analytics import availability_news as an

    return [] if getattr(args, "no_news", False) else an.load()


def _artifact_by_sha(weekly_dir: Path, version: str, season: int, week: int, sha: str) -> tuple[Artifact, bytes, Path]:
    """The week's write-once store artifact whose FILE bytes hash to `sha` (a stored run's artifact_id)."""
    from app.analytics import weekly_learning as wl
    for f in sorted((weekly_dir / "artifacts" / version).glob(f"s{season}w{week:02d}-*.json")):
        data = f.read_bytes()
        if hashlib.sha256(data).hexdigest() == sha:
            return wl.load_artifact(f), data, f
    raise ValueError(f"no {version} artifact file with sha256 {sha[:12]} for {season} week {week} in "
                     f"{weekly_dir / 'artifacts' / version}: the base run's artifact must be re-used, never replaced")


def cmd_reproject(args, hist, scoring, now: datetime | None = None) -> dict:
    """Final-designation re-projection (app/analytics/final_reprojection.py): a NEW run of the week in progress with
    the artifact of its newest verified run, after the late-week injury reports. Teams already kicked off keep their
    base-run rows (carried, flagged). Phase 1 read-only, phase 2 one write_run. Production model only."""
    from app.analytics import final_reprojection as fr
    from app.analytics import weekly_learning as wl
    from app.db.connection import open_db
    from app.nfl import pbp_exits

    if args.model != CONSUMER_VERSION:
        raise ValueError(f"reproject applies to the production model ({CONSUMER_VERSION}) only")
    t = time.time()
    now = now or datetime.now(timezone.utc)
    scoring = scoring.require_supported()
    with open_db(args.live_db, read_only=True) as con:
        week = args.week or fr.in_progress_week(con, args.season, now)
        if week is None:
            raise fr.ReprojectRefused(f"no {args.season} regular-season kickoff is ahead of {now.isoformat()}")
        base = fr.base_run(con, args.season, week, args.model, scoring.fingerprint)
        sha = fr.check_base(base, args.season, week, args.model)
        art, data, path = _artifact_by_sha(args.weekly_dir, args.model, args.season, week, sha)
        wl.check_artifact(art, args.season, week, scoring.fingerprint)
        exits = pbp_exits.load_dir(args.weekly_dir / "pbp_exits")
        if exits is None and art.config["provenance"].get("pbp_exits_through_week") is not None:
            raise ValueError(f"{path.name} was trained with play-by-play injury evidence, but "
                             f"{args.weekly_dir / 'pbp_exits'} has none (scripts/weekly_learning.py sync-exits)")
        # the participation model the base run recorded (not whatever version is current); build refuses when
        # it is missing, so a re-projection never silently drops p_play / expected_points
        recorded = base["run"].get("participation_version")
        ppath = args.out / f"participation_{recorded}.json" if recorded else None
        part = (pm.ParticipationArtifact.from_dict(json.loads(ppath.read_text()))
                if ppath is not None and ppath.exists() else None)
        src = _sources(con, hist, scoring).with_exits(exits)
        built = fr.build(con, src, art, args.season, week, base=base, participation=part,
                         artifact_sha256=hashlib.sha256(data).hexdigest(),
                         context_inputs=_context_inputs(args, art, con, hist), news=_news(args))
    if built["status"] == fr.WRITTEN:
        with open_db(args.live_db) as con:
            stored = fr.store(con, built)
    else:
        stored = fr.store(None, built)
    keys = ("base_run_id", "base_run_created_at", "kicked_off_teams", "unknown_kickoff_teams",
            "missing_from_schedule_teams", "reprojected", "carried",
            "dropped_post_kickoff", "changes", "injury_freshness")
    return {"schema": fr.SCHEMA, "season": args.season, "week": week, "model_version": art.version,
            "artifact_id": sha, "artifact_file": path.name, **stored, **{k: built[k] for k in keys},
            "runtime_seconds": round(time.time() - t, 2)}


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["audit", "select", "evaluate", "fit", "project", "reproject",
                                            "participation-select",
                                            "participation-evaluate", "participation-fit", "snap-ablation"])
    parser.add_argument("--live-db", type=Path, default=settings.db_path)
    parser.add_argument("--history-db", type=Path, default=_path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    parser.add_argument("--out", type=Path, default=_path("INDEPENDENT_MODEL_DIR", "data/models"))
    parser.add_argument("--season", type=int, default=settings.nfl_season, help="live season")
    parser.add_argument("--week", type=int, help="target NFL week (project; reproject: default the week in progress)")
    parser.add_argument("--artifact-week", type=int, dest="artifact_week",
                        help="project --week T with the weekly artifact of this EARLIER week W (rest-of-season "
                             "projection: features as of W, T's opponent; forecast_horizon = T - W)")
    parser.add_argument("--through-week", type=int, dest="through_week",
                        help="project --week W and every later week up to this one (the last regular-season week) "
                             "from W's artifact, in one process")
    parser.add_argument("--model", choices=[CONSUMER_VERSION, FALLBACK_VERSION, RETIRED_WEEKLY_VERSION, MODEL_VERSION], default=CONSUMER_VERSION,
                        help="model `project` runs (default: the production model the projection switch reads)")
    parser.add_argument("--weekly-dir", type=Path, default=_path("WEEKLY_MODEL_DIR", "data/models/weekly"),
                        help="weekly-learning output (ifp-2 artifacts and pbp_exits), as in weekly_learning.py --out")
    parser.add_argument("--register-artifact", choices=["learning_cycle", "manual_pin"],
                        help="weekly models (ifp-2.2.0, ifp-2.1.0, ifp-2.0.0): record the weekly-store artifact this run uses in "
                             "the live database's approved_artifacts (write-once); the consumer view serves only "
                             "registered artifacts of its version once one is registered. The learning cycle passes "
                             "learning_cycle; a manual pin is manual_pin")
    parser.add_argument("--no-news", action="store_true", dest="no_news",
                        help="project / reproject without the reviewed availability news "
                             "(app/analytics/availability_news.json)")
    parser.add_argument("--synthetic-label", action="store_true", help="mark reports as synthetic-data runs")
    args = parser.parse_args(argv)
    protocol = Protocol(heldout_seasons=(2025, args.season) if args.season > 2025 else (2025,))
    hist = _ro(args.history_db)
    writes = args.command in ("project", "reproject")
    live = _ro(args.live_db) if not writes else None
    try:
        scoring_con = live or (_ro(args.live_db) if writes else None)
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
        elif args.command == "reproject":
            scoring_con.close()
            result = cmd_reproject(args, hist, scoring)
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
