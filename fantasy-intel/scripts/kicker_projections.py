"""Kicker projection engine (kfp-1.1.0): reconcile scoring, evaluate, fit the artifact, project a week.

    cd backend
    uv run python ../scripts/kicker_projections.py reconcile                       # league kicker scoring vs ESPN
    uv run python ../scripts/kicker_projections.py evaluate                        # validation selection + test report
    uv run python ../scripts/kicker_projections.py fit [--register]                # artifact for the live season
    uv run python ../scripts/kicker_projections.py register                        # approve an existing artifact file
    uv run python ../scripts/kicker_projections.py project [--week W] [--through-week T] [--no-weather]

Data: the history DB (NFL_HISTORY_DB_PATH, finished seasons) and the live DB (FANTASY_DB_PATH). Both need the
kicker opportunity tables (scripts/sync_kicking_pbp.py backfills them; the regular syncs keep them current).
Outputs: <out>/kfp_evaluation.json, <out>/kfp-1.1.0_<season>.json (artifact; <out> defaults to data/models/kicker,
git-ignored). Only `project` and `fit --register` write to the live database: `project` appends one write-once
kicker run per target week (an identical rerun is a no-op), `--register` approves the artifact file's sha256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401

import numpy as np
import polars as pl

from app.analytics import kicker_backtest as kb
from app.analytics import kicker_model as km
from app.analytics.kicker_scoring import league_items, reconcile_with_espn, resolve_kicker_scoring
from app.config import REPO_ROOT, get_settings
from app.db.connection import connect, open_db


def _path(env: str, default: str) -> Path:
    raw = Path(os.getenv(env) or default)
    return raw if raw.is_absolute() else REPO_ROOT / raw


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items() if not str(k).startswith("_")}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, float):
        return None if (o != o or o in (float("inf"), float("-inf"))) else o
    if isinstance(o, (np.floating,)):
        return _clean(float(o))
    if isinstance(o, (np.integer,)):
        return int(o)
    return o


def _load(args):
    live = connect(args.live_db, read_only=True)
    hist = connect(args.history_db, read_only=True) if args.history_db.exists() else None
    items = league_items(live, args.season)
    scoring = resolve_kicker_scoring(items)
    frames = km.merge_frames(*(f for f in (km.load_frames(hist) if hist else None, km.load_frames(live)) if f))
    return live, hist, items, scoring, frames


def espn_frame(con, season: int) -> pl.DataFrame:
    """ESPN kicker projections, labeled by how they were recorded: a snapshot taken before the week's first kickoff
    is 'espn_pregame_snapshot'; the per-week stored projection (synced at an unverified time) is
    'espn_stored_projection_unverified_timing'. Never an input to the model."""
    from app.analytics.production_forecasts import first_kickoffs

    rows = []
    stored = con.execute("""SELECT m.gsis_id, e.scoring_period AS week, e.projected_points FROM espn_player_weekly_points e
        JOIN fantasy_players f ON f.espn_player_id = e.espn_player_id AND f.position = 'K'
        JOIN player_id_map m ON m.espn_player_id = e.espn_player_id AND m.match_confidence = 'high'
        WHERE e.season = ? AND e.projected_points IS NOT NULL""", [season]).fetchall()
    rows += [{"gsis_id": g, "season": season, "week": int(w), "espn_points": float(p),
              "espn_label": "espn_stored_projection_unverified_timing"} for g, w, p in stored]
    first = first_kickoffs(con, season)
    snaps = con.execute("""SELECT m.gsis_id, h.scoring_period AS week, h.projected_points, h.synced_at
        FROM espn_projection_history h JOIN fantasy_players f ON f.espn_player_id = h.espn_player_id AND f.position = 'K'
        JOIN player_id_map m ON m.espn_player_id = h.espn_player_id AND m.match_confidence = 'high'
        WHERE h.season = ? AND h.projected_points IS NOT NULL ORDER BY h.synced_at""", [season]).fetchall()
    latest: dict = {}
    for g, w, p, t in snaps:
        ko = first.get(int(w))
        if ko is not None and t is not None and t.replace(tzinfo=timezone.utc) < ko:
            latest[(g, int(w))] = float(p)
    rows += [{"gsis_id": g, "season": season, "week": w, "espn_points": p, "espn_label": "espn_pregame_snapshot"}
             for (g, w), p in latest.items()]
    return pl.DataFrame(rows, schema={"gsis_id": pl.String, "season": pl.Int32, "week": pl.Int32,
                                      "espn_points": pl.Float64, "espn_label": pl.String})


def cmd_reconcile(args) -> dict:
    live, _, items, scoring, _ = _load(args)
    out = {"scoring": scoring.describe(), "reconciliation": reconcile_with_espn(live, args.season, scoring, items)}
    alt = resolve_kicker_scoring(items, blocked_counts_as_miss=False)
    out["alternative_blocked_not_a_miss"] = {k: v for k, v in reconcile_with_espn(live, args.season, alt, items).items()
                                            if k != "mismatches"}
    return out


def _sample_sizes(frames) -> dict:
    k = frames["kicks"]
    t = frames["teams"]
    out = {}
    for s in sorted(t["season"].unique().to_list()):
        ks, ts = k.filter(pl.col("season") == s), t.filter(pl.col("season") == s)
        out[str(s)] = {"team_games": ts.height, "weeks": ts["week"].n_unique(),
                       "fg_tries": int((ks["kick_type"] == "FG").sum()), "pat_tries": int((ks["kick_type"] == "PAT").sum()),
                       "kickers": ks["kicker_player_id"].n_unique()}
    return out


def cmd_evaluate(args) -> dict:
    live, hist, items, scoring, frames = _load(args)
    scoring.require_supported()
    rep: dict = {"model_version": km.MODEL_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(),
                 "config": kb.cfg_dict(kb.PRODUCTION),
                 "scoring": scoring.describe(), "protocol": {"burn_in": 2019, "validation": list(kb.VALIDATION),
                                                             "test": list(kb.TEST), "prospective": args.season,
                                                             "test_role": "retrospective, report-only (inspected when "
                                                                          "kfp-1.1.0 was chosen)",
                                                             "held_out": f"prospective {args.season}"},
                 "sample_sizes": _sample_sizes(frames)}
    rep["reconciliation"] = {k: v for k, v in reconcile_with_espn(live, args.season, scoring, items).items()}
    base = kb.PRODUCTION
    data = kb.build_dataset(frames, scoring, base)
    # --- selection on validation only
    tc = kb.tune_conversion(data, base)
    to = kb.tune_opportunity(frames, scoring, base)
    rep["selection"] = {"conversion": {"best": tc["best"], "league_curve_only_logloss": tc["league_curve_only_logloss"],
                                       "best_by_groups": {",".join(g) or "none": min((r for r in tc["grid"] if r["groups"] == list(g)),
                                                                                     key=lambda r: r["logloss"])
                                                          for g in ((), ("stadium",), ("stadium", "weather"))}},
                        "opportunity": {"best": to["best"], "top5": to["grid"][:5]}}
    variants = {}
    no_line = tuple(g for g in base.groups if g != "line")
    for name, (g, fg) in {"production (fg: line; pat: off+def+stadium+line)": (base.groups, base.fg_groups),
                          "kfp-1.0.0: line removed (fg: league rate; pat: off+def+stadium)": (no_line, ()),
                          "fg uses the same groups as pat": (base.groups, None),
                          "fg: stadium only (line removed from fg)": (base.groups, ("stadium",)),
                          "+script": (base.groups + ("script",), base.fg_groups)}.items():
        c = base.with_groups(g, fg)
        sf = kb.season_frames(data, c, range(kb.FIRST_ROW_SEASON, max(kb.VALIDATION) + 1))
        p = pl.concat([kb.evaluate_season(data, c, scoring, s, sf) for s in kb.VALIDATION])
        y = p["label"].to_numpy()
        variants[name] = {**kb._metrics(p["pred_struct"].to_numpy(), y),
                          "blend_mae": {str(w): round(float(np.mean(np.abs(y - (w * p["pred_struct"].to_numpy()
                                                                                    + (1 - w) * p["pred_direct"].to_numpy())))), 4)
                                        for w in (1.0, 0.75, 0.5, 0.0)}}
    rep["selection"]["variants_validation"] = variants
    rep["selection"]["frozen_production_config"] = kb.cfg_dict(base)
    drift = []
    if tc["best"]["tau2"] != base.tau2 or tc["best"]["decay"] != base.decay:
        drift.append(f"conversion grid now prefers tau2={tc['best']['tau2']} decay={tc['best']['decay']}")
    if (to["best"]["team_k"], to["best"]["prior_k"], to["best"]["ridge"]) != (base.team_k, base.prior_k, base.ridge_lambda):
        drift.append(f"opportunity grid now prefers {to['best']}")
    rep["selection"]["drift_from_frozen"] = drift
    # --- ablations (validation and test, reported side by side; nothing is chosen on test)
    rep["ablations_validation"] = kb.run_ablations(data, base, scoring, kb.VALIDATION)
    rep["ablations_test"] = kb.run_ablations(data, base, scoring, kb.TEST)
    # --- intervals from validation residuals, coverage measured on test
    val = kb.full_report(data, base, scoring, kb.VALIDATION, "validation")
    vp = val.pop("_predictions")
    bands = km.residual_bands(vp["pred"].to_numpy(), vp["label"].to_numpy())
    # 2023-25 was inspected when kfp-1.1.0 was chosen: a retrospective, report-only split. The untouched check is the
    # prospective season below.
    test = kb.full_report(data, base, scoring, kb.TEST, "test 2023-25 (retrospective, report-only)")
    tp = test.pop("_predictions")
    test["interval80_coverage"] = _coverage(bands, tp)
    rep["validation"], rep["test"] = val, test
    rep["intervals_from_validation"] = bands
    # --- prospective: the live season so far
    espn = espn_frame(live, args.season)
    pro = kb.full_report(data, base, scoring, [args.season], f"prospective {args.season}", espn=espn)
    pp = pro.pop("_predictions")
    pro["interval80_coverage"] = _coverage(bands, pp)
    rep["prospective"] = pro
    rep["league_median_points"] = float(np.median(np.concatenate([vp["label"].to_numpy(), tp["label"].to_numpy()])))
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "kfp_evaluation.json"
    path.write_text(json.dumps(_clean(rep), indent=2, sort_keys=True, default=str))
    pl.concat([vp.with_columns(pl.lit("validation").alias("split")), tp.with_columns(pl.lit("test").alias("split")),
               pp.with_columns(pl.lit("prospective").alias("split"))], how="diagonal_relaxed").select(
        "split", "season", "week", "team", "opponent", "kicker_player_id", "label", "pred", "pred_struct", "pred_direct",
        "pred_A1", "pred_A2", "pred_A3", "pred_lam_fg", "pred_lam_pat", "fg_att", "pat_att", "roof_class", "obs_wind",
        "obs_temp").write_csv(args.out / "kfp_evaluation_predictions.csv")
    return {"report": str(path), "test_common_support": test["common_support"],
            "prospective": {k: pro.get(k) for k in ("all_rows_model", "espn")}}


def _coverage(bands: dict, preds: pl.DataFrame) -> dict:
    lo, hi, y = [], [], preds["label"].to_numpy()
    inside = n = 0
    for p, a in zip(preds["pred"].to_numpy(), y):
        iv = km.interval_for(bands, float(p))
        if iv is None:
            continue
        n += 1
        inside += iv[0] <= a <= iv[1]
    return {"n": n, "empirical": round(inside / n, 4) if n else None, "nominal": bands.get("nominal_coverage")}


def report_is_stale(ev: dict, cfg) -> bool:
    """True when an evaluation report was made for another model version, or records a different frozen config
    (reports made before the config was recorded are judged by version alone)."""
    if ev.get("model_version") != km.MODEL_VERSION:
        return True
    recorded = ev.get("config")
    return recorded is not None and _clean(recorded) != _clean(kb.cfg_dict(cfg))


def artifact_evaluation(args, cfg) -> dict:
    """The evaluation summary a new artifact embeds, from <out>/kfp_evaluation.json. A report made for another model
    version or configuration (e.g. the first cycle after an upgrade) is re-derived first, so an artifact never carries
    another model's metrics."""
    ev_path = args.out / "kfp_evaluation.json"
    if ev_path.exists() and report_is_stale(json.loads(ev_path.read_text()), cfg):
        cmd_evaluate(args)
    if not ev_path.exists():
        return {}
    ev = json.loads(ev_path.read_text())
    if report_is_stale(ev, cfg):
        raise SystemExit(f"{ev_path} still describes {ev.get('model_version')!r} after re-evaluating; not fitting")
    return {"test": {k: ev["test"].get(k) for k in ("common_support", "all_rows_model", "interval80_coverage",
                                                     "fg_make_logloss")},
            "prospective": {k: ev["prospective"].get(k) for k in ("all_rows_model", "espn")},
            "ablations_test": {k: {"mae": v["mae"], "per_season_mae": v["per_season_mae"]}
                               for k, v in ev["ablations_test"].items()},
            "league_median_points": ev.get("league_median_points"), "report_generated_at": ev.get("generated_at")}


def cmd_fit(args) -> dict:
    live, hist, items, scoring, frames = _load(args)
    scoring.require_supported()
    cfg = kb.PRODUCTION
    season = args.season
    data = kb.build_dataset(frames, scoring, cfg)
    rows = data["rows"].filter(pl.col("completed") & (pl.col("season") >= kb.FIRST_ROW_SEASON) & (pl.col("season") < season))
    sf = kb.season_frames(data, cfg, range(kb.FIRST_ROW_SEASON, season))
    train = pl.concat([sf[s][0] for s in range(kb.FIRST_ROW_SEASON, season)], how="diagonal_relaxed").filter(pl.col("completed"))
    comps = kb.fit_components(train, cfg)
    conv = km.fit_conversion(data["conv"].filter(pl.col("season") < season), cfg)
    # intervals: out-of-sample residuals of every season the walk-forward can predict (2021 .. season-1)
    oos = pl.concat([kb.evaluate_season(data, cfg, scoring, s, sf) for s in range(kb.FIRST_ROW_SEASON + 1, season)])
    bands = km.residual_bands(oos["pred"].to_numpy(), oos["label"].to_numpy())
    evaluation = artifact_evaluation(args, cfg)
    hcons = [c for c in (connect(args.history_db, read_only=True) if args.history_db.exists() else None, live) if c]
    part = kb.participation_table(hcons, frames, scoring)
    status = {"K": {"status": "research_validated_historical", "start_sit_ready": False,
                    "reasons": ["Historical walk-forward validation only (see evaluation); not yet validated "
                                "prospectively or against genuine ESPN pregame projections."]}}
    created = datetime.now(timezone.utc).isoformat()
    art = km.KickerArtifact(version=km.MODEL_VERSION, created_at=created, scoring_fingerprint=scoring.fingerprint,
                            scoring=scoring.describe(), config={"kconfig": kb.cfg_dict(cfg), "train_seasons":
                                                               [kb.FIRST_ROW_SEASON, season - 1]},
                            lambda_fg=comps["lam_fg"], lambda_pat=comps["lam_pat"], conversion=conv,
                            direct=comps["direct"], intervals=bands, participation=part, status=status,
                            evaluation=evaluation, train_window=[kb.FIRST_ROW_SEASON, season - 1],
                            data_cutoff_key=(season - 1) * 100 + 99)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{km.MODEL_VERSION}_{season}.json"
    text = json.dumps(_clean(art.to_dict()), sort_keys=True, indent=1)
    if path.exists() and path.read_text() != text and not args.overwrite:
        raise SystemExit(f"{path} exists with different content; artifacts are write-once (pass --overwrite only for "
                         "an artifact that was never registered or used)")
    path.write_text(text)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    out = {"artifact": str(path), "sha256": sha, "train_rows": train.height, "rows_total": rows.height,
           "participation": part}
    if args.register:
        from app.analytics.kicker_store import register_artifact
        live.close()
        with open_db(args.live_db) as con:
            out["registered"] = register_artifact(con, model_version=km.MODEL_VERSION, artifact_sha256=sha,
                                                  target_season=season, training_cutoff_key=art.data_cutoff_key,
                                                  artifact_created_at=created)
    return out


def cmd_register(args) -> dict:
    """Approve an existing artifact file (the season's, or --artifact) by its sha256. Idempotent: approving an
    already-approved artifact creates nothing. For an artifact fitted without --register, a failed registration,
    or a database restored separately from the model directory; refitting would write a different file."""
    from app.analytics.kicker_store import register_artifact
    path = args.artifact or args.out / f"{km.MODEL_VERSION}_{args.season}.json"
    if not path.exists():
        raise SystemExit(f"{path} does not exist; run `fit --register` first")
    art = json.loads(path.read_text())
    if art.get("version") != km.MODEL_VERSION:
        raise SystemExit(f"{path} is a {art.get('version')!r} artifact, not {km.MODEL_VERSION}")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    with open_db(args.live_db) as con:
        created = register_artifact(con, model_version=km.MODEL_VERSION, artifact_sha256=sha, target_season=args.season,
                                    training_cutoff_key=art.get("data_cutoff_key"),
                                    artifact_created_at=art.get("created_at"))
    return {"artifact": str(path), "sha256": sha, "registered": created}


def next_week(con, season: int) -> int | None:
    from app.analytics.production_forecasts import first_kickoffs
    now = datetime.now(timezone.utc)
    upcoming = [w for w, ko in first_kickoffs(con, season).items() if ko > now]
    return min(upcoming) if upcoming else None


def cmd_project(args) -> dict:
    from app.analytics.kicker_live import build_run, load_artifact
    from app.analytics.kicker_store import write_run

    path = args.artifact or args.out / f"{km.MODEL_VERSION}_{args.season}.json"
    art = load_artifact(path)
    sha = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    live, hist, items, scoring, frames = _load(args)
    week = args.week or next_week(live, args.season)
    if week is None:
        return {"status": "no upcoming week"}
    weeks = list(range(week, (args.through_week or week) + 1))
    live.close()
    results = []
    # one write connection: weather snapshots are appended as they are fetched, then each week's run
    with open_db(args.live_db) as con:
        for target in weeks:
            meta, rows = build_run(con, frames, art, args.season, target, artifact_sha256=sha,
                                   projected_from_week=None if target == week else week,
                                   fetch_weather=not args.no_weather)
            res = kb_write(con, meta, rows, write_run)
            res["coverage"] = {s: sum(r["coverage_status"] == s for r in rows) for s in sorted({r["coverage_status"] for r in rows})}
            res["week"] = target
            results.append(res)
    return {"artifact": str(path), "artifact_sha256": sha, "runs": results}


def kb_write(con, meta, rows, write_run):
    return write_run(con, meta, rows)


def main(argv=None) -> int:
    s = get_settings()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("reconcile", "evaluate", "fit", "register", "project"))
    ap.add_argument("--season", type=int, default=s.nfl_season)
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--through-week", type=int, default=None)
    ap.add_argument("--live-db", type=Path, default=s.db_path)
    ap.add_argument("--history-db", type=Path, default=_path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--out", type=Path, default=_path("KICKER_MODEL_DIR", "data/models/kicker"))
    ap.add_argument("--artifact", type=Path, default=None)
    ap.add_argument("--register", action="store_true", help="fit: approve the artifact in the live database")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--no-weather", action="store_true", help="project: use cached forecasts only, never fetch")
    args = ap.parse_args(argv)
    fn = {"reconcile": cmd_reconcile, "evaluate": cmd_evaluate, "fit": cmd_fit, "register": cmd_register,
          "project": cmd_project}[args.command]
    print(json.dumps(_clean(fn(args)), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
