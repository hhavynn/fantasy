"""Research: adaptive established workload / injury-aware role (ifp-aw, research only; see
docs/research/adaptive_workload.md). Offline, read-only on both databases; prints aggregates.

    cd backend
    uv run python ../scripts/adaptive_workload_research.py run     # roles -> development -> diagnostic -> decision
    uv run python ../scripts/adaptive_workload_research.py audit   # game-class information audit
    uv run python ../scripts/adaptive_workload_research.py cases   # Barkley 2026 + data-selected contrasts

Inputs as in challenger_research.py (play-by-play exits from `weekly_learning.py sync-exits` in
--exits-dir/pbp_exits, ifp-1.0.0 selection.json in --ifp-dir). Outputs go to
<ifp-dir>/research/adaptive_workload/ (git-ignored). Never writes the ifp-2 config or any ledger.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import challenger_research as cr  # noqa: E402

import duckdb  # noqa: E402
import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.analytics import adaptive_workload as aw  # noqa: E402
from app.analytics import challenger as ch  # noqa: E402
from app.analytics.forecast_backtest import (_arr, build_store, completed_weeks, frozen_penalties,  # noqa: E402
                                             metrics, walk_forward as ifp_walk_forward)
from app.analytics.forecast_features import POSITIONS, classify_games, features_for_week  # noqa: E402
from app.config import get_settings  # noqa: E402


def _sources(args):
    return cr.sources(SimpleNamespace(live_db=args.live_db, history_db=args.history_db, season=args.season,
                                      out=args.exits_dir))


def _out(args) -> Path:
    p = args.ifp_dir / "research" / "adaptive_workload"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _frame_hash(df: pl.DataFrame | None) -> str | None:
    """Order-independent content hash of a source frame."""
    if df is None:
        return None
    return hashlib.sha256(np.sort(df.hash_rows(seed=0).to_numpy()).tobytes()).hexdigest()[:16]


def input_fingerprint(args, src, scoring_fingerprint: str, weeks) -> str:
    """Everything the cached store depends on: source data, weeks, scoring, role configs and the
    code that builds it. Any change rebuilds the cache."""
    code = [Path(m.__file__).read_bytes() for m in (aw, ch, sys.modules["app.analytics.forecast_features"],
                                                     sys.modules["app.analytics.forecast_backtest"])]
    parts = {"season": args.season, "weeks": weeks, "scoring": scoring_fingerprint, "version": aw.AW_VERSION,
             "configs": {k: asdict(c) for k, c in aw.CONFIGS.items()}, "polars": pl.__version__,
             "code": hashlib.sha256(b"".join(code)).hexdigest()[:16],
             **{n: _frame_hash(getattr(src, n)) for n in ("panel", "games", "injuries", "exits", "rosters")}}
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def full_store(args, src, scoring_fingerprint: str) -> pl.DataFrame:
    """ifp-2 feature store + role features of every RoleConfig + evaluation columns. Cached with an
    input fingerprint; reused only when every input is unchanged (else rebuilt)."""
    path, meta = _out(args) / "store_aw.parquet", _out(args) / "store_aw.meta.json"
    weeks = completed_weeks(src, list(range(ch.TRAIN_START, args.season + 1)))
    fp = input_fingerprint(args, src, scoring_fingerprint, weeks)
    if path.exists() and meta.exists() and not args.rebuild and json.loads(meta.read_text()).get("fingerprint") == fp:
        return pl.read_parquet(path)
    store = ch.add_last_game(build_store(src, weeks), src.panel)
    for suffix, cfg in aw.CONFIGS.items():
        roles = aw.build_role_store(src, store, cfg, suffix)
        store = store.join(roles, on=["key", "gsis_id"], how="left")
    store = aw.attach_evaluation_columns(store, src)
    store.write_parquet(path)
    meta.write_text(json.dumps({"fingerprint": fp, "rows": store.height, "weeks": len(weeks)}))
    return store


def _cand_run(payload):
    store_path, weeks, cand, lams, ifp2_path = payload
    store = pl.read_parquet(store_path)
    cfg = ch.ChallengerConfig.load(Path(ifp2_path))
    feats = {p: aw.candidate_features(cand, p, cfg) for p in POSITIONS}
    return cand.name, aw.walk_forward(store, weeks, cand.name, feats, lams,
                                      {p: aw.candidate_half_life(p, cfg) for p in POSITIONS})


def _baselines(store, weeks, sel, cfg) -> pl.DataFrame:
    wf = ifp_walk_forward(store, weeks, frozen_penalties(sel), ch.TRAIN_START)
    c = pl.concat([wf.filter(pl.col("position") == pos).select(
        "key", "gsis_id", pl.col(f"pred_C_{sel['positions'][pos]['lambda']['C']:g}").alias("pred_C"))
        for pos in POSITIONS])
    return c.join(ch.challenger_predictions(store, weeks, cfg), on=["key", "gsis_id"], how="left")


def _predict_candidates(args, store_path, weeks, lams_by_cand) -> dict[str, pl.DataFrame]:
    payloads = [(str(store_path), weeks, c, lams_by_cand[c.name], str(ch.CONFIG_PATH)) for c in aw.CANDIDATES]
    # spawn, not fork: forking a process that already ran polars' thread pool can deadlock the children
    with ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        return dict(pool.map(_cand_run, payloads))


def _check_qb_identical(df: pl.DataFrame, models: dict) -> None:
    """Every candidate keeps ifp-2's QB model: identical QB predictions or the comparison is invalid."""
    qb = df.filter(pl.col("position") == "QB")
    for n in models:
        if n.startswith("AW"):
            d = (qb[f"pred_{n}"] - qb["pred_ifp2"]).abs().max()
            assert d is None or d < 1e-9, f"{n}: QB predictions differ from ifp-2 by {d}"


def cmd_run(args) -> dict:
    t0 = time.time()
    src, scoring = _sources(args)
    store = full_store(args, src, scoring.fingerprint)
    t_store = round(time.time() - t0, 1)
    store_path = _out(args) / "store_aw.parquet"
    cfg = ch.ChallengerConfig.load()
    sel = json.loads((args.ifp_dir / "selection.json").read_text())
    if sel.get("scoring_fingerprint") != scoring.fingerprint:
        raise SystemExit("selection.json (frozen ifp-1.0.0 penalties) was made under different league scoring")
    qb = {"QB": [cfg.positions["QB"]["lam"]]}
    grid = {**qb, **{p: list(aw.LAMBDA_GRID) for p in ("RB", "WR", "TE")}}

    # ---- development: penalty per candidate/position, then candidate
    dev_weeks = ch.window_weeks(store, aw.DEVELOPMENT)
    preds = _predict_candidates(args, store_path, dev_weeks, {c.name: grid for c in aw.CANDIDATES})
    dev = store.filter(pl.col("key").is_in([s * 100 + w for s, w in dev_weeks]))
    dev = dev.join(_baselines(store, dev_weeks, sel, cfg), on=["key", "gsis_id"], how="left")
    best = {}
    for name, p in preds.items():
        dev = dev.join(p, on=["key", "gsis_id"], how="left")
        best[name] = {}
        for pos in POSITIONS:
            cand_cols = [f"pred_{name}@{lam:g}" for lam in grid[pos]]
            g = dev.filter((pl.col("position") == pos) & pl.all_horizontal([pl.col(c).is_not_null() for c in cand_cols]))
            scores = {lam: metrics(_arr(g, "label"), _arr(g, f"pred_{name}@{lam:g}"))["mae"] for lam in grid[pos]}
            best[name][pos] = min(scores, key=lambda lam: (scores[lam], -lam))
    dev = dev.with_columns([pl.coalesce([pl.when(pl.col("position") == pos).then(pl.col(f"pred_{n}@{lam:g}"))
                                         for pos, lam in by.items()]).alias(f"pred_{n}") for n, by in best.items()])
    models = {"C": "pred_C", "ifp2": "pred_ifp2", **{c.name: f"pred_{c.name}" for c in aw.CANDIDATES}}
    _check_qb_identical(dev, models)
    dev_rep = aw.compare(dev, models)
    selectable = [c.name for c in aw.CANDIDATES if c.selectable]
    chosen = min(selectable, key=lambda n: dev_rep["ALL"]["metrics"][n]["mae"])

    # ---- diagnostic windows (already inspected seasons): frozen development penalties
    diag_weeks = {w: ch.window_weeks(store, seasons) for w, seasons in zip(("2023-2024", "2025-2026"), aw.DIAGNOSTIC)}
    all_diag = sorted(diag_weeks["2023-2024"] + diag_weeks["2025-2026"])
    frozen = {n: {p: [lam] for p, lam in by.items()} for n, by in best.items()}
    dpreds = _predict_candidates(args, store_path, all_diag, frozen)
    diag = store.filter(pl.col("key").is_in([s * 100 + w for s, w in all_diag]))
    diag = diag.join(_baselines(store, all_diag, sel, cfg), on=["key", "gsis_id"], how="left")
    for name, p in dpreds.items():
        diag = diag.join(p.rename({c: f"_{c}" for c in p.columns if c.startswith("pred_")}), on=["key", "gsis_id"],
                         how="left")
        diag = diag.with_columns(pl.coalesce([pl.when(pl.col("position") == pos).then(
            pl.col(f"_pred_{name}@{lam:g}")) for pos, lam in best[name].items()]).alias(f"pred_{name}"))
    diag = diag.drop([c for c in diag.columns if c.startswith("_pred_")])
    diag.select("key", "season", "week", "gsis_id", "position", "label", *[f"pred_{n}" for n in models]).write_parquet(
        _out(args) / "diagnostic_preds.parquet")
    dev.select("key", "season", "week", "gsis_id", "position", "label", *[f"pred_{n}" for n in models]).write_parquet(
        _out(args) / "development_preds.parquet")
    _check_qb_identical(diag, models)
    windows = {"2023-2024": diag.filter(pl.col("season").is_in(list(aw.DIAGNOSTIC[0]))),
               "2025-2026": diag.filter(pl.col("season").is_in(list(aw.DIAGNOSTIC[1])))}
    diag_rep = {w: aw.compare(d, models) for w, d in windows.items()}
    diag_rep["pooled"] = aw.compare(diag, models)
    decisions = {n: aw.decision(dev_rep, diag_rep["pooled"], n) for n in models if n not in ("C", "ifp2")}
    research_cfg = aw.research_config(next(c for c in aw.CANDIDATES if c.name == chosen), best[chosen], cfg,
                                      status=decisions[chosen]["outcome"])
    report = {"schema": "adaptive-workload-report/v1", "version": aw.AW_VERSION,
              "created_at": datetime.now(timezone.utc).isoformat(), "scoring_fingerprint": scoring.fingerprint,
              "ifp2_config_hash": cfg.hash(), "rules": aw.AW_DECISION_RULES,
              "development": {"seasons": aw.DEVELOPMENT, "weeks": len(dev_weeks), "best_lambda": best,
                              "report": dev_rep, "selected": chosen},
              "diagnostic": diag_rep, "decisions": decisions, "selected_decision": decisions[chosen],
              "research_config": research_cfg,
              "runtime_seconds": {"store_and_roles": t_store, "total": round(time.time() - t0, 1)}}
    (_out(args) / "report.json").write_text(json.dumps(ch.round_floats(report), indent=1, default=str))
    (_out(args) / f"research_config_{aw.AW_VERSION}.json").write_text(json.dumps(research_cfg, indent=1, default=str))
    return {"selected": chosen, "decision": decisions[chosen], "best_lambda": best,
            "dev_mae": {n: dev_rep["ALL"]["metrics"][n]["mae"] for n in models},
            "diag_mae": {w: {n: r["ALL"]["metrics"][n]["mae"] for n in models} for w, r in diag_rep.items()},
            "runtime_seconds": report["runtime_seconds"]}


def cmd_audit(args) -> dict:
    """Information content of each game class: next appearance usage relative to the pregame
    established workload (outcome side, every report up to the end of the data)."""
    src, scoring = _sources(args)
    store = full_store(args, src, scoring.fingerprint)
    exits = src.exits
    final = aw.final_regular_weeks(src.games)
    c = aw.information_classes(classify_games(src.panel, src, 9999, 1), exits, final)
    c = c.filter(pl.col("season") >= ch.TRAIN_START).sort("gsis_id", "season", "week").with_columns(
        (pl.col("season").shift(-1).over("gsis_id") == pl.col("season")).alias("_same"),
        (pl.col("week").shift(-1).over("gsis_id") == pl.col("week") + 1).alias("_next_week"),
        pl.col("xfpl").shift(-1).over("gsis_id").alias("_nx"),
        pl.col("xfpl").shift(-2).over("gsis_id").alias("_nx2"),
        (pl.col("season").shift(-2).over("gsis_id") == pl.col("season")).alias("_same2"))
    # pregame established workload of the game itself (its own forecast row) and of the next season
    est = store.select("season", "week", "gsis_id", "xfpl_eb", "aw_eb")
    c = c.join(est, on=["season", "week", "gsis_id"], how="left").filter(
        pl.col("position").is_in(["RB", "WR", "TE"]) & (pl.col("ref_snap") >= 0.4) & (pl.col("xfpl_eb") >= 4))
    nx = pl.when(pl.col("_same")).then(pl.col("_nx"))
    rat = nx / pl.col("xfpl_eb")
    rat2 = pl.when(pl.col("_same2")).then((pl.col("_nx") + pl.col("_nx2")) / 2) / pl.col("xfpl_eb")
    tab = c.group_by("position", "info_class").agg(
        pl.len().alias("n"), pl.col("_next_week").fill_null(False).mean().round(3).alias("appeared_next_week"),
        (pl.col("xfpl") / pl.col("xfpl_eb")).median().round(3).alias("game_usage_vs_established"),
        rat.median().round(3).alias("next_usage_vs_established"),
        (rat < 0.6).mean().round(3).alias("next_below_60pct"),
        (rat >= 0.9).mean().round(3).alias("next_back_to_90pct"),
        rat2.median().round(3).alias("next2_usage_vs_established"),
        # next appearance in any season (the only "next" game after a final-week drop is next season's)
        (pl.col("_nx") / pl.col("xfpl_eb")).median().round(3).alias("next_any_season_usage_vs_established"),
        (pl.col("_nx") / pl.col("xfpl_eb") >= 0.9).mean().round(3).alias("next_any_season_back_to_90pct"),
    ).sort("position", "info_class")
    week18 = c.filter(pl.col("info_class") == "week18_low").height
    out = {"rows": tab.to_dicts(),
           "definition": "players with reference snap share >= 40% and pregame xfpl_eb >= 4; usage = expected league "
                         "points; classes use all reports up to the end of the data (outcome side)",
           "week18_low_games": week18,
           "injury_report_timestamp_limitation": "2025-2026 nflverse injury rows have no date_modified: the "
                                                 "pre-kickoff filter cannot be applied to them (retrospective risk)"}
    (_out(args) / "audit.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def _forecast_rows(src, store, season, week, gsis_ids) -> pl.DataFrame:
    """Pregame feature rows for players who have not played (season, week) yet (case studies)."""
    rows = []
    for gid in gsis_ids:
        last = src.panel.filter((pl.col("gsis_id") == gid) & (pl.col("season") * 100 + pl.col("week") < season * 100 + week)
                                ).sort("season", "week").tail(1)
        g = src.games.filter((pl.col("season") == season) & (pl.col("week") == week) & (pl.col("team") == last["team"][0]))
        rows.append(pl.DataFrame({"gsis_id": [gid], "position": [last["position"][0]], "team": [last["team"][0]],
                                  "opponent": [g["opponent"][0]], "is_home": [g["is_home"][0]]}))
    targets = pl.concat(rows)
    f = features_for_week(src, season, week, targets).with_columns(
        (pl.col("season") * 100 + pl.col("week")).alias("key"))
    for suffix, cfg in aw.CONFIGS.items():
        r = aw.role_features_for_week(src, season, week, targets, cfg)
        f = f.join(r.rename({c: f"{c}{suffix}" for c in aw.AW_COLUMNS}) if suffix else r, on="gsis_id", how="left")
    return f


def _fit_predict(store, rows, k, features, lam, half_life):
    out = []
    for r in rows.iter_rows(named=True):
        pos = r["position"]
        m = ch.fit_linear(ch.train_rows(store, k, pos), pos, features[pos], lam[pos],
                          half_life_weeks=half_life[pos], target_key=k)
        out.append(float(m.predict(rows.filter(pl.col("gsis_id") == r["gsis_id"]))[0]))
    return out


def cmd_cases(args) -> dict:
    src, scoring = _sources(args)
    store = full_store(args, src, scoring.fingerprint)
    rep = json.loads((_out(args) / "report.json").read_text())
    best, chosen = rep["development"]["best_lambda"], rep["development"]["selected"]
    cfg = ch.ChallengerConfig.load()
    live = duckdb.connect(str(args.live_db), read_only=True)
    names = dict(live.execute("SELECT gsis_id, display_name FROM nfl_players").fetchall())
    live.close()
    ids = {v: k for k, v in names.items()}
    out = {}
    # ---- Saquon Barkley, 2026 week 6 (pregame; weeks 1-5 history)
    gid = ids.get(args.case_player)
    if gid:
        season, week = args.case_season, args.case_week
        k = season * 100 + week
        hist = src.panel.filter((pl.col("gsis_id") == gid) & (pl.col("season") == season) & (pl.col("week") < week))
        cls = aw.information_classes(classify_games(src.panel.filter(pl.col("season") >= season - 1), src, season, week),
                                     None if src.exits is None else src.exits.filter(pl.col("season") * 100 + pl.col("week") < k),
                                     aw.final_regular_weeks(src.games)).filter(
            (pl.col("gsis_id") == gid) & (pl.col("season") == season))
        inj = (src.injuries.filter((pl.col("gsis_id") == gid) & (pl.col("season") == season) & (pl.col("week") <= week))
               .select("week", "report_status", "practice_status", "injury") if src.injuries is not None else None)
        rows = _forecast_rows(src, store, season, week, [gid])
        feats = {n: {p: aw.candidate_features(c, p, cfg) for p in POSITIONS} for n, c in
                 [(c.name, c) for c in aw.CANDIDATES]}
        preds = {"ifp2": _fit_predict(store, rows, k, {p: cfg.features(p) for p in POSITIONS},
                                      {p: cfg.positions[p]["lam"] for p in POSITIONS},
                                      {p: cfg.positions[p].get("half_life_weeks") for p in POSITIONS})[0]}
        for n in feats:
            preds[n] = _fit_predict(store, rows, k, feats[n], best[n],
                                    {p: aw.candidate_half_life(p, cfg) for p in POSITIONS})[0]
        r = rows.row(0, named=True)
        out["barkley"] = {
            "player": args.case_player, "forecast": f"{season} week {week} (pregame, conditional on playing)",
            "weeks": hist.join(cls.select("week", "info_class", "f", "ref_snap"), on="week", how="left").select(
                "week", "carries", "targets", "offense_pct", "ref_snap", "xfpl", "info_class", "f",
                "league_points").sort("week").with_columns(pl.col(pl.Float64).round(3)).to_dicts(),
            "injury_reports": None if inj is None else inj.sort("week").to_dicts(),
            "raw_recent": {c: r.get(c) for c in ("carries_pg", "targets_pg", "xfp_recent", "snap_pct", "snap_trend")},
            "ifp2_inputs": {c: r.get(c) for c in ("h_carries_pg", "h_xfpl_pg", "prev_xfpl_pg", "cur_h_games", "xfpl_eb",
                                                  "inj_exits_recent", "last_injury_exit")},
            "experimental": {c: r.get(c) for c in ("aw_eb", "aw_role", "aw_r", "aw_streak", "aw_info", "aw_prev_role",
                                                   "aw_eb_x", "aw_role_n")},
            "projection_if_plays": {n: round(v, 2) for n, v in preds.items()}}
    # ---- data-selected contrasts from the diagnostic replays (no player-specific rules)
    d = pl.read_parquet(_out(args) / "diagnostic_preds.parquet").join(
        store.select("key", "gsis_id", "xfpl_eb", "aw_eb", "aw_role", "aw_streak", "aw_last_class", "carries_pg",
                     "h_carries_pg", "targets_pg", "h_targets_pg", "inj_exits_recent", "_target_xfpl", "_next_xfpl",
                     "_target_exit"), on=["key", "gsis_id"])
    d = d.with_columns(pl.col("gsis_id").replace_strict(names, default=None).alias("name"),
                       (pl.col("pred_AW_B") - pl.col("pred_ifp2")).alias("aw_minus_ifp2"))   # the adaptive blend
    show = ["name", "season", "week", "position", "carries_pg", "h_carries_pg", "targets_pg", "h_targets_pg",
            "inj_exits_recent", "aw_last_class", "xfpl_eb", "aw_eb", "aw_role", "aw_streak", "_target_xfpl",
            "_next_xfpl", "pred_C", "pred_ifp2", *[f"pred_{c.name}" for c in aw.CANDIDATES], "label"]
    shown = ["C", "ifp2", *[c.name for c in aw.CANDIDATES]]
    sel = {
        "lost_starting_role": (pl.col("aw_streak") <= -2) & (pl.col("xfpl_eb") >= 10)
                              & (pl.col("_target_xfpl") < 0.6 * pl.col("xfpl_eb")) & ~pl.col("_target_exit"),
        "breakout": (pl.col("aw_streak") >= 2) & (pl.col("_target_xfpl") > 1.4 * pl.col("xfpl_eb"))
                    & (pl.col("xfpl_eb") >= 5),
        "ambiguous_rest_or_injury": pl.col("aw_last_class").is_in(["week18_low", "injury_exit_report", "listed_low"])
                                    & (pl.col("_target_xfpl") >= 0.9 * pl.col("xfpl_eb")) & (pl.col("xfpl_eb") >= 8),
    }
    for name, flt in sel.items():
        g = d.filter(flt)
        a = g["label"].to_numpy()
        out[name] = {"n": g.height, **{f"mae_{m}": metrics(a, _arr(g, f"pred_{m}"))["mae"] for m in shown},
                     **{f"bias_{m}": metrics(a, _arr(g, f"pred_{m}"))["bias"] for m in shown},
                     "examples": g.sort(pl.col("aw_minus_ifp2").abs(), descending=True).head(args.limit).select(show)
                     .with_columns(pl.col(pl.Float64).round(2)).to_dicts()}
    (_out(args) / "cases.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "audit", "cases"])
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"),
                    help="directory containing pbp_exits/ (weekly_learning.py sync-exits)")
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--rebuild", action="store_true", help="rebuild the cached feature + role store")
    ap.add_argument("--workers", type=int, default=max(1, min(4, (os.cpu_count() or 2) - 1)))
    ap.add_argument("--limit", type=int, default=4)
    ap.add_argument("--case-player", default="Saquon Barkley")
    ap.add_argument("--case-season", type=int, default=2026)
    ap.add_argument("--case-week", type=int, default=6)
    args = ap.parse_args(argv)
    result = {"run": cmd_run, "audit": cmd_audit, "cases": cmd_cases}[args.command](args)
    print(json.dumps(result, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
