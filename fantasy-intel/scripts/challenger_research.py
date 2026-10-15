"""ifp-2 challenger research CLI. Offline, read-only on both databases; prints aggregates.

    cd backend
    uv run python ../scripts/challenger_research.py store        # feature store (+ play-by-play exits) -> parquet
    uv run python ../scripts/challenger_research.py develop      # development window 2021-2022 only
    uv run python ../scripts/challenger_research.py confirm      # frozen challenger vs C on 2023-2024 (run once)
    uv run python ../scripts/challenger_research.py exploratory  # 2025 + 2026 (already-inspected seasons)
    uv run python ../scripts/challenger_research.py exits-audit  # injury-exit classes vs next-game usage

Inputs as in backtest_independent_projections.py, plus the play-by-play exit files written by
`weekly_learning.py sync-exits` (<model dir>/pbp_exits/<season>.parquet). Outputs go to
<model dir>/research/ (git-ignored).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
import json
import os
from pathlib import Path
import sys
import time

import _bootstrap
import duckdb
import polars as pl

from app.analytics import challenger as ch
from app.analytics.forecast_backtest import build_store, completed_weeks
from app.analytics.forecast_features import Sources, load_sources
from app.analytics.independent_projection_store import league_scoring
from app.config import REPO_ROOT, get_settings
from app.nfl.pbp_exits import load_dir


def _path(env: str, default: str) -> Path:
    raw = Path(os.getenv(env) or default)
    return raw if raw.is_absolute() else REPO_ROOT / raw


def sources(args) -> tuple[Sources, object]:
    live = duckdb.connect(str(args.live_db), read_only=True)
    hist = duckdb.connect(str(args.history_db), read_only=True) if args.history_db.exists() else None
    _, scoring = league_scoring(live, args.season)
    parts = [load_sources(c, scoring.require_supported()) for c in (hist, live) if c is not None]
    src = Sources.concat(parts).with_exits(load_dir(args.out / "pbp_exits"))
    return src, scoring


def cmd_store(args) -> dict:
    t = time.time()
    src, scoring = sources(args)
    weeks = completed_weeks(src, list(range(ch.TRAIN_START, args.season + 1)))
    store = ch.add_last_game(build_store(src, weeks), src.panel)
    path = args.out / "research" / "store.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_parquet(path)
    return {"rows": store.height, "weeks": len(weeks), "exits_seasons": None if src.exits is None else
            sorted(src.exits["season"].unique().to_list()), "scoring_fingerprint": scoring.fingerprint,
            "runtime_seconds": round(time.time() - t, 1), "path": str(path)}


def _load_store(args) -> pl.DataFrame:
    return pl.read_parquet(args.out / "research" / "store.parquet")


def _run(payload):
    store_path, weeks, spec = payload
    store = pl.read_parquet(store_path)
    preds = ch.walk_forward(store, weeks, [spec])
    return spec, preds.select("key", "season", "week", "gsis_id", "position",
                              *[c for c in preds.columns if c.startswith("pred_") and "@" in c or c == f"pred_{spec.name}"])


DEV_SPECS = [
    ch.Spec("C"),
    ch.Spec("C_X", "X"), ch.Spec("C_K", "K"), ch.Spec("C_H", "H"), ch.Spec("C_P", "P"), ch.Spec("C_E", "E"),
    ch.Spec("C_HPE", "HPE"), ch.Spec("C_XHPE", "XHPE"), ch.Spec("C_XKHPE", "XKHPE"),
    ch.Spec("O_XHPE", "XHPE", opportunity_first=True),
    ch.Spec("C_hub1.0", loss="huber", huber_c=1.0), ch.Spec("C_hub1.345", loss="huber", huber_c=1.345),
    ch.Spec("C_hub2.0", loss="huber", huber_c=2.0),
    ch.Spec("C_hl18", half_life_weeks=18), ch.Spec("C_hl36", half_life_weeks=36), ch.Spec("C_hl72", half_life_weeks=72),
    ch.Spec("C_E_k1", "E", usage_prior_games=1.0), ch.Spec("C_E_k6", "E", usage_prior_games=6.0),
    ch.Spec("C_E_k10", "E", usage_prior_games=10.0), ch.Spec("C_E_k0", "E", usage_prior_games=1e-9),
    # round 2 (added after round-1 development results; still development window only)
    ch.Spec("C_F", "F"), ch.Spec("C_U", "U"), ch.Spec("C_Q", "Q"), ch.Spec("C_FE", "FE"), ch.Spec("C_FQ", "FQ"),
    ch.Spec("C_FEQ", "FEQ"), ch.Spec("C_FUEQ", "FUEQ"), ch.Spec("C_FEQ_hl36", "FEQ", half_life_weeks=36),
    # round 3 (after confirmation; development window only, reported, cannot change the frozen config)
    ch.Spec("C_R", "R"), ch.Spec("C_RFEQ_hl36", "RFEQ", half_life_weeks=36),
]


def cmd_develop(args) -> dict:
    """Development window only: every candidate across the penalty grid, best penalty per position."""
    store = _load_store(args)
    weeks = ch.window_weeks(store, ch.WINDOWS["development"])
    names = set(args.only.split(",")) if args.only else None
    specs = [s for s in DEV_SPECS if names is None or s.name in names]
    t = time.time()
    path = str(args.out / "research" / "store.parquet")
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(_run, [(path, weeks, s) for s in specs]))
    base = store.filter(pl.col("key").is_in([s * 100 + w for s, w in weeks]))
    for _, preds in results:
        base = base.join(preds.drop("season", "week", "position"), on=["key", "gsis_id"], how="left")
    best = {}
    for spec, _ in results:
        best[spec.name] = {}
        for pos in ch.POSITIONS:
            g = base.filter(pl.col("position") == pos)
            cand = [c for c in base.columns if c.startswith(f"pred_{spec.name}@")]
            g = g.filter(pl.all_horizontal([pl.col(c).is_not_null() for c in cand]))
            a = ch._arr(g, "label")
            scores = {c: ch.metrics(a, ch._arr(g, c))["mae"] for c in cand}
            best[spec.name][pos] = min(scores, key=lambda c: (scores[c], -float(c.split("@")[1])))
    # one column per spec at its best development penalty
    cols = []
    for name, by_pos in best.items():
        expr = pl.lit(None, dtype=pl.Float64)
        for pos, c in by_pos.items():
            expr = pl.when(pl.col("position") == pos).then(pl.col(c)).otherwise(expr)
        cols.append(expr.alias(f"pred_{name}"))
    dev = base.with_columns(cols)
    dev.select("key", "season", "week", "gsis_id", "position", "label",
               *[f"pred_{n}" for n in best]).write_parquet(args.out / "research" / "dev_preds.parquet")
    rep = ch.compare(dev, {n: f"pred_{n}" for n in best}, baseline="C")
    report = {"window": ch.WINDOWS["development"], "weeks": len(weeks),
              "best_lambda": {n: {p: float(c.split("@")[1]) for p, c in bp.items()} for n, bp in best.items()},
              "report": rep, "runtime_seconds": round(time.time() - t, 1)}
    (args.out / "research" / f"develop{'_' + args.tag if args.tag else ''}.json").write_text(
        json.dumps(ch.round_floats(report), indent=2, default=str))
    return summarize(report)


FROZEN_CHOICE = {"QB": ("C", {"groups": ""}),
                 **{p: ("C_FEQ_hl36", {"groups": "FEQ", "half_life_weeks": 36.0}) for p in ("RB", "WR", "TE")}}


def cmd_freeze(args) -> dict:
    """Write the frozen challenger configuration from the DEVELOPMENT results only."""
    from app.analytics.forecast_features import USAGE_PRIOR_GAMES
    dev = json.loads((args.out / "research" / "develop_r2.json").read_text())
    preds = pl.read_parquet(args.out / "research" / "dev_preds.parquet")
    positions, intervals, evidence = {}, {}, {}
    for pos, (name, spec) in FROZEN_CHOICE.items():
        positions[pos] = {**spec, "lam": dev["best_lambda"][name][pos]}
        intervals[pos] = ch.interval_bands(preds.filter(pl.col("position") == pos), f"pred_{name}")
        r = dev["report"][pos]
        evidence[pos] = {"development_spec": name, "n": r["n"], "mae_C": r["metrics"]["C"]["mae"],
                         "mae_challenger": r["metrics"][name]["mae"],
                         "minus_C": r["minus_baseline_mae"].get(name)}
    cfg = ch.ChallengerConfig(
        version=ch.CHALLENGER_VERSION, positions=positions, intervals=intervals, usage_prior_games=USAGE_PRIOR_GAMES,
        selected_on=list(ch.WINDOWS["development"]), evidence=evidence,
        notes=["Selected on 2021-2022 only (walk-forward). QB keeps the ifp-1.0.0 context features: no addition "
               "improved QB on development.",
               "RB/WR/TE: context model C + game-class flags (F) + empirical-Bayes established workload (E) + "
               "previous-season expected points / team change (Q), training rows weighted with a 36-week half-life.",
               "Rejected on development: Huber loss (lower MAE by targeting the median: bias +0.47 to +0.63, worse "
               "RMSE and pair accuracy), touchdown/yardage efficiency split (X), extra team context (K), healthy "
               "usage averages (U), opportunity-first variant (O)."])
    path = Path(_bootstrap.BACKEND) / "app" / "analytics" / "challenger_config.json"
    path.write_text(json.dumps(ch.round_floats(asdict(cfg)), indent=2) + "\n")
    return {"path": str(path), "hash": ch.ChallengerConfig.load(path).hash(), "positions": positions}


def _window_compare(args, window: str) -> dict:
    """Frozen challenger vs frozen ifp-1.0.0 B/C and the trailing baselines on one window."""
    cfg = ch.ChallengerConfig.load()
    sel = json.loads((args.ifp_dir / "selection.json").read_text())
    store = _load_store(args)
    weeks = ch.window_weeks(store, ch.WINDOWS[window])
    t = time.time()
    # ifp-1.0.0 B and C at their frozen penalties, exactly as forecast_backtest.evaluate computes them
    from app.analytics.forecast_backtest import frozen_penalties, walk_forward as ifp_walk_forward
    wf = ifp_walk_forward(store, weeks, frozen_penalties(sel), ch.TRAIN_START)
    preds = pl.concat([wf.filter(pl.col("position") == pos).with_columns(
        pl.col(f"pred_{m}_{sel['positions'][pos]['lambda'][m]:g}").alias(f"pred_{m}") for m in ("B", "C"))
        for pos in ch.POSITIONS], how="diagonal_relaxed")
    preds = preds.join(ch.challenger_predictions(store, weeks, cfg), on=["key", "gsis_id"], how="left")
    dev = pl.read_parquet(args.out / "research" / "dev_preds.parquet")
    intervals = {"ifp2": cfg.intervals, "C": ch.bands_for(dev, "pred_C")}
    rep = ch.compare(preds, {"C": "pred_C", "B": "pred_B", "A1": "pred_A1", "A2": "pred_A2", "ifp2": "pred_ifp2"},
                     baseline="C", intervals=intervals)
    preds.select("key", "season", "week", "gsis_id", "position", "label", "pred_A1", "pred_A2", "pred_B", "pred_C",
                 "pred_ifp2").write_parquet(args.out / "research" / f"{window}_preds.parquet")
    report = {"window": window, "seasons": ch.WINDOWS[window], "weeks": len(weeks), "config_hash": cfg.hash(),
              "report": rep, "runtime_seconds": round(time.time() - t, 1),
              "label": "EXPLORATORY: these seasons were already inspected by ifp-1.0.0 work" if window == "exploratory"
              else "confirmation (run once with the frozen challenger)"}
    if window == "confirmation":
        report["rules"] = ch.CONFIRMATION_RULES
        report["decision"] = confirmation_decision(rep, "ifp2", "C")
    (args.out / "research" / f"{window}.json").write_text(json.dumps(ch.round_floats(report), indent=2, default=str))
    return {**summarize(report), "decision": report.get("decision")}


def confirmation_decision(rep: dict, cand: str, base: str) -> dict:
    a = rep["ALL"]
    d = a["minus_baseline_mae"][cand]
    m, b = a["metrics"][cand], a["metrics"][base]
    st = a["cohorts"]["starters_pool"]
    pa, pb = rep["pairs"]["all"][f"pred_{cand}"]["accuracy"], rep["pairs"]["all"][f"pred_{base}"]["accuracy"]
    checks = {
        "pooled_mae": d["diff"] < 0 and d["ci95"][1] < 0,
        "no_position_significantly_worse": all(rep[p]["minus_baseline_mae"][cand]["ci95"][0] <= 0 for p in ch.POSITIONS),
        "rmse_guard": m["rmse"] <= b["rmse"] * 1.005,
        "bias_guard": abs(m["bias"]) <= abs(b["bias"]) + 0.15,
        "starters_guard": st[cand] <= st[base] * 1.01,
        "pairs_guard": pa >= pb - 0.005,
    }
    return {"checks": checks, "outcome": "shadow" if all(checks.values()) else "rejected"}


def cmd_confirm(args) -> dict:
    return _window_compare(args, "confirmation")


def cmd_exploratory(args) -> dict:
    return _window_compare(args, "exploratory")


def cmd_exits_audit(args) -> dict:
    """How reliable is the game classification, and what does each class predict? Labels are
    classified with ALL information (outcome side, never features); 2020-2026 week 5."""
    from app.analytics.forecast_features import classify_games
    src, _ = sources(args)
    store = _load_store(args)
    classes = classify_games(src.panel, src, 2100, 1).filter(pl.col("season") >= ch.TRAIN_START)
    out: dict = {"pbp_exit_seasons": None if src.exits is None else sorted(src.exits["season"].unique().to_list())}
    pbp = (src.exits.select("season", "week", "gsis_id", "injured_in_game") if src.exits is not None else None)
    c = classes.join(pbp, on=["season", "week", "gsis_id"], how="left") if pbp is not None else classes
    out["class_counts"] = {pos: dict(sorted(Counter(g["gclass"].to_list()).items()))
                           for (pos,), g in c.group_by(["position"], maintain_order=True)}
    ex = c.filter(pl.col("gclass") == "injury_exit")
    out["injury_exit_evidence"] = {"games": ex.height,
                                   "with_pbp_injury_mention": int(ex["injured_in_game"].fill_null(0).sum()),
                                   "report_only": int((ex["injured_in_game"].fill_null(0) == 0).sum())}
    # agreement with the ifp-1.0.0-era label on the same forecast rows
    s_ = store.filter(pl.col("injury_exit_last").is_not_null())
    out["label_agreement_on_forecast_rows"] = {
        "old_injury_exit_last": int((s_["injury_exit_last"] == 1).sum()),
        "new_last_injury_exit": int((s_["last_injury_exit"] == 1).sum()),
        "both": int(((s_["injury_exit_last"] == 1) & (s_["last_injury_exit"] == 1)).sum())}
    # next appearance after each class (same season), relative to the player's reference
    nxt = c.sort("gsis_id", "season", "week").with_columns(
        pl.col("season").shift(-1).over("gsis_id").alias("_ns"), pl.col("week").shift(-1).over("gsis_id").alias("_nw"),
        pl.col("offense_pct").shift(-1).over("gsis_id").alias("_next_snap"),
        (pl.col("targets") + pl.col("carries")).shift(-1).over("gsis_id").alias("_next_opp"),
        (pl.col("targets") + pl.col("carries")).shift(1).rolling_mean(4, min_samples=2).over("gsis_id").alias("_ref_opp"))
    nxt = nxt.filter(pl.col("ref_snap") >= 0.4)
    same = pl.col("_ns") == pl.col("season")
    out["next_appearance_by_class"] = nxt.group_by("position", "gclass").agg(
        pl.len().alias("n"), (same & (pl.col("_nw") == pl.col("week") + 1)).mean().round(3).alias("appeared_next_week"),
        (pl.col("_next_snap") / pl.col("ref_snap")).filter(same).median().round(3).alias("next_snap_vs_ref"),
        (pl.col("_next_opp") / pl.col("_ref_opp")).filter(same & (pl.col("_ref_opp") > 2)).median().round(3)
        .alias("next_opps_vs_ref")).sort("position", "gclass").to_dicts()
    # how far injury exits pull raw last-5 usage below healthy-game usage (forecast rows)
    red = {}
    for pos, raw, h in (("RB", "carries_pg", "h_carries_pg"), ("WR", "targets_pg", "h_targets_pg"),
                        ("TE", "targets_pg", "h_targets_pg"), ("QB", "xfp_recent", "h_xfpl_pg")):
        for k in (1, 2):
            g = store.filter((pl.col("position") == pos) & (pl.col("inj_exits_recent") >= k) & (pl.col(h) > 0))
            red[f"{pos}_{raw}_exits>={k}"] = {"n": g.height, "median_raw_over_healthy":
                                              None if g.height == 0 else round(float((g[raw] / g[h]).median()), 3)}
    out["usage_reduction_from_exits"] = red
    # does a recent exit predict another exit (P(normal workload | appears))?
    lab = store.join(c.select("season", "week", "gsis_id", (pl.col("gclass") == "injury_exit").cast(pl.Int8)
                              .alias("_exit_now")), on=["season", "week", "gsis_id"], how="left")
    lab = lab.filter(pl.col("_exit_now").is_not_null())
    out["exit_rate_given_history"] = lab.group_by("position", pl.col("inj_exits_recent").clip(0, 2).alias("prior_exits")).agg(
        pl.len().alias("n"), pl.col("_exit_now").mean().round(4).alias("p_exit"),
        pl.col("label").filter(pl.col("_exit_now") == 1).mean().round(2).alias("pts_if_exit"),
        pl.col("label").filter(pl.col("_exit_now") == 0).mean().round(2).alias("pts_if_not")).sort(
        "position", "prior_exits").to_dicts()
    (args.out / "research" / "exits_audit.json").write_text(json.dumps(out, indent=2, default=str))
    return out


def cmd_cases(args) -> dict:
    """Player-level examples from the confirmation + exploratory replays (numbers only; the
    'why' is inferred from the classified evidence and labeled as such)."""
    store = _load_store(args)
    preds = pl.concat([pl.read_parquet(args.out / "research" / f"{w}_preds.parquet") for w in ("confirmation", "exploratory")])
    live = duckdb.connect(str(args.live_db), read_only=True)
    names = dict(live.execute("SELECT gsis_id, display_name FROM nfl_players").fetchall())
    cols = ["season", "week", "gsis_id", "position", "label", "pred_C", "pred_ifp2", "carries_pg", "h_carries_pg",
            "targets_pg", "h_targets_pg", "xfpl_eb", "inj_exits_recent", "last_pts", "last_td_pts", "last_opps",
            "pts_recent_raw", "games_recent"]
    j = preds.join(store.select("key", "gsis_id", *[c for c in cols if c not in preds.columns]), on=["key", "gsis_id"])
    j = j.with_columns(pl.col("gsis_id").replace_strict(names, default=None).alias("name"),
                       (pl.col("pred_ifp2") - pl.col("pred_C")).round(2).alias("ifp2_minus_C"))
    out = {}
    sel = {"established_rb_after_injury_exit": (pl.col("position") == "RB") & (pl.col("last_injury_exit") == 1)
                                               & (pl.col("h_carries_pg") >= 14),
           "low_opportunity_td_boom": (pl.col("last_td_pts") >= 12) & (pl.col("last_opps") <= 6)
                                      & (pl.col("games_recent") >= 3),
           "possible_role_cut": (pl.col("last_role_low") == 1) & (pl.col("h_xfpl_pg") >= 10)}
    for name, flt in sel.items():
        g = j.join(store.select("key", "gsis_id", "last_injury_exit", "last_role_low", "h_xfpl_pg"),
                   on=["key", "gsis_id"]).filter(flt)
        a = g["label"].to_numpy()
        out[name] = {"n": g.height, "mae_C": ch.metrics(a, ch._arr(g, "pred_C"))["mae"],
                     "mae_ifp2": ch.metrics(a, ch._arr(g, "pred_ifp2"))["mae"],
                     "bias_C": ch.metrics(a, ch._arr(g, "pred_C"))["bias"],
                     "bias_ifp2": ch.metrics(a, ch._arr(g, "pred_ifp2"))["bias"],
                     "examples": g.sort("ifp2_minus_C", descending=True).head(args.limit).select(
                         "name", "season", "week", "position", "carries_pg", "h_carries_pg", "targets_pg", "h_targets_pg",
                         "inj_exits_recent", "last_pts", "last_td_pts", "last_opps", "pred_C", "pred_ifp2", "label")
                     .with_columns(pl.col(pl.Float64).round(2)).to_dicts()}
    (args.out / "research" / "cases.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def summarize(report: dict) -> dict:
    rep = report["report"]
    out = {}
    for pos in [*ch.POSITIONS, "ALL"]:
        r = rep[pos]
        out[pos] = {"n": r["n"], "mae": {m: v["mae"] for m, v in r["metrics"].items()},
                    "diff_vs_C": {m: (v["diff"], v["ci95"]) for m, v in r["minus_baseline_mae"].items()}}
    out["pairs_accuracy"] = {m.replace("pred_", ""): v["accuracy"] for m, v in rep["pairs"]["all"].items()}
    return out


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["store", "develop", "freeze", "confirm", "exploratory", "exits-audit", "cases"])
    ap.add_argument("--limit", type=int, default=6)
    ap.add_argument("--ifp-dir", type=Path, default=_path("IFP_MODEL_DIR", "data/models"),
                    help="directory with the frozen ifp-1.0.0 selection.json (read-only)")
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=_path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--out", type=Path, default=_path("INDEPENDENT_MODEL_DIR", "data/models"))
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--only", help="comma-separated spec names (develop)")
    ap.add_argument("--tag", default="")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    args = ap.parse_args(argv)
    result = {"store": cmd_store, "develop": cmd_develop, "freeze": cmd_freeze, "confirm": cmd_confirm,
              "exploratory": cmd_exploratory, "exits-audit": cmd_exits_audit,
              "cases": cmd_cases}[args.command](args)
    print(json.dumps(result, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
