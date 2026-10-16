"""Research: shallow histogram gradient boosting vs ridge (ifp-hgb, research only; see
docs/research/hgb_challenger.md). Offline; needs the `research` dependency group (scikit-learn,
installed by a plain `uv sync`; Vercel's --no-dev never installs it).

    cd backend
    uv run python ../scripts/hgb_research.py develop    # 2021-22 selection -> write-once frozen config
    uv run python ../scripts/hgb_research.py predict --week W   # frozen config, pregame, write-once ledger
    uv run python ../scripts/hgb_research.py diagnose   # 2023-26 replay, importance, ablations, ensemble, cases

Outputs: <ifp-dir>/research/hgb/ (git-ignored). Never writes ifp-1/ifp-2 artifacts or the weekly ledger.
"""

from __future__ import annotations

import os

os.environ.setdefault("OMP_NUM_THREADS", "1")    # one thread per process; processes run in parallel

import argparse  # noqa: E402
from concurrent.futures import ProcessPoolExecutor  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import multiprocessing  # noqa: E402
from pathlib import Path  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import _bootstrap  # noqa: F401, E402

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import challenger_research as cr  # noqa: E402

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.analytics import challenger as ch  # noqa: E402
from app.analytics import hgb_research as hg  # noqa: E402
from app.analytics.forecast_backtest import (_arr, build_store, completed_weeks, frozen_penalties,  # noqa: E402
                                             metrics, pair_decisions, starters_pool_expr, week_bootstrap_mae_diff,
                                             walk_forward as ifp_walk_forward)
from app.analytics.forecast_features import POSITIONS  # noqa: E402
from app.config import get_settings  # noqa: E402


def _out(args) -> Path:
    p = args.ifp_dir / "research" / "hgb"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sources(args):
    return cr.sources(SimpleNamespace(live_db=args.live_db, history_db=args.history_db, season=args.season,
                                      out=args.exits_dir))


def _frame_hash(df: pl.DataFrame | None) -> str | None:
    """Order-independent content hash of a source frame."""
    if df is None:
        return None
    return hashlib.sha256(np.sort(df.hash_rows(seed=0).to_numpy()).tobytes()).hexdigest()[:16]


def input_fingerprint(args, src, scoring_fingerprint: str, weeks) -> str:
    """Everything the cached store depends on: source data, weeks, scoring and the code that builds
    it. Any change rebuilds the cache."""
    mods = ["app.analytics.challenger", "app.analytics.forecast_features", "app.analytics.forecast_backtest", "app.analytics.hgb_research"]
    code = b"".join(Path(sys.modules[m].__file__).read_bytes() for m in mods)
    parts = {"season": args.season, "weeks": weeks, "scoring": scoring_fingerprint, "polars": pl.__version__,
             "code": hashlib.sha256(code).hexdigest()[:16],
             **{n: _frame_hash(getattr(src, n)) for n in ("panel", "games", "injuries", "exits", "rosters")}}
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def full_store(args, src, scoring_fingerprint: str) -> pl.DataFrame:
    """Cached feature store, reused only when its recorded input fingerprint still matches."""
    path = _out(args) / "store.parquet"
    meta = path.with_suffix(".meta.json")
    weeks = completed_weeks(src, list(range(ch.TRAIN_START, args.season + 1)))
    fp = input_fingerprint(args, src, scoring_fingerprint, weeks)
    if path.exists() and meta.exists() and not args.rebuild and json.loads(meta.read_text()).get("fingerprint") == fp:
        return pl.read_parquet(path)
    store = ch.add_last_game(build_store(src, weeks), src.panel)
    store.write_parquet(path)
    meta.write_text(json.dumps({"fingerprint": fp, "rows": store.height, "weeks": len(weeks)}))
    return store


def _job(payload):
    store_path, weeks, pos, configs, permute = payload
    store = pl.read_parquet(store_path).filter(pl.col("position") == pos)
    cfg = ch.ChallengerConfig.load()
    feats = {p: hg.hgb_features(p, cfg) for p in POSITIONS}
    hl = {p: cfg.positions[p].get("half_life_weeks") for p in POSITIONS}
    t = time.time()
    preds = hg.walk_forward(store, weeks, feats, configs, hl, permute_groups=permute)
    return pos, preds, round(time.time() - t, 1)


def _run_jobs(args, store_path, weeks, per_pos_configs: dict[str, dict], permute=False):
    payloads = [(str(store_path), weeks, pos, {n: c}, permute) for pos, cs in per_pos_configs.items()
                for n, c in cs.items()]
    with ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        res = list(pool.map(_job, payloads))
    by_pos: dict[str, pl.DataFrame] = {}
    runtime: dict[str, float] = {}
    for (pos, preds, sec), (_, _, _, cfgs, _) in zip(res, payloads):
        name = next(iter(cfgs))
        runtime[f"{pos}:{name}"] = sec
        by_pos[pos] = preds if pos not in by_pos else by_pos[pos].join(preds, on=["key", "gsis_id"], how="full",
                                                                          coalesce=True)
    return pl.concat(list(by_pos.values()), how="diagonal_relaxed"), runtime


def _baselines(store, weeks, sel, cfg) -> pl.DataFrame:
    wf = ifp_walk_forward(store, weeks, frozen_penalties(sel), ch.TRAIN_START)
    c = pl.concat([wf.filter(pl.col("position") == pos).select(
        "key", "gsis_id", pl.col(f"pred_C_{sel['positions'][pos]['lambda']['C']:g}").alias("pred_C"))
        for pos in POSITIONS])
    return c.join(ch.challenger_predictions(store, weeks, cfg), on=["key", "gsis_id"], how="left")


def _write_once(path: Path, obj) -> bool:
    from app.analytics.weekly_learning import write_once
    return write_once(path, json.dumps(obj, indent=1, default=str))


# ----------------------------------------------------------------------------- develop + freeze


def cmd_develop(args) -> dict:
    t0 = time.time()
    src, scoring = _sources(args)
    store = full_store(args, src, scoring.fingerprint)
    store_path = _out(args) / "store.parquet"
    cfg = ch.ChallengerConfig.load()
    sel = json.loads((args.ifp_dir / "selection.json").read_text())
    if sel.get("scoring_fingerprint") != scoring.fingerprint:
        raise SystemExit("selection.json (frozen ifp-1.0.0 penalties) was made under different league scoring")
    weeks = ch.window_weeks(store, hg.DEVELOPMENT)
    preds, runtime = _run_jobs(args, store_path, weeks, {p: dict(hg.SEARCH_SPACE) for p in POSITIONS})
    dev = store.filter(pl.col("key").is_in([s * 100 + w for s, w in weeks])).join(
        _baselines(store, weeks, sel, cfg), on=["key", "gsis_id"], how="left").join(preds, on=["key", "gsis_id"],
                                                                                    how="left")
    names = list(hg.SEARCH_SPACE)
    chosen, grid = {}, {}
    for pos in POSITIONS:
        g = dev.filter((pl.col("position") == pos) & pl.all_horizontal([pl.col(f"pred_{n}").is_not_null()
                                                                         for n in names + ["ifp2"]]))
        a = _arr(g, "label")
        grid[pos] = {n: metrics(a, _arr(g, f"pred_{n}"))["mae"] for n in names}
        grid[pos]["ifp2"] = metrics(a, _arr(g, "pred_ifp2"))["mae"]
        chosen[pos] = min(names, key=lambda n: (grid[pos][n], n))
    dev = dev.with_columns(pl.coalesce([pl.when(pl.col("position") == p).then(pl.col(f"pred_{n}"))
                                        for p, n in chosen.items()]).alias("pred_hgb"))
    both = dev.filter(pl.col("pred_hgb").is_not_null() & pl.col("pred_ifp2").is_not_null())
    er, eh = _arr(both, "label") - _arr(both, "pred_ifp2"), _arr(both, "label") - _arr(both, "pred_hgb")
    corr = float(np.corrcoef(er, eh)[0, 1])
    ens = None
    if corr < hg.ENSEMBLE_MAX_ERROR_CORR:
        scores = {w: metrics(_arr(both, "label"), w * _arr(both, "pred_ifp2") + (1 - w) * _arr(both, "pred_hgb"))["mae"]
                  for w in hg.ENSEMBLE_WEIGHTS}
        ens = {"w_ridge": min(scores, key=lambda w: (scores[w], -w)), "dev_mae_by_weight": scores}
    frozen = {"version": hg.HGB_VERSION, "selected_on": hg.DEVELOPMENT, "search_space": hg.SEARCH_SPACE,
              "params": {p: hg.SEARCH_SPACE[n] for p, n in chosen.items()}, "chosen": chosen,
              "features": {p: hg.hgb_features(p, cfg) for p in POSITIONS},
              "half_life_weeks": {p: cfg.positions[p].get("half_life_weeks") for p in POSITIONS},
              "ensemble": ens, "development_error_correlation": round(corr, 4),
              "ifp2_config_hash": cfg.hash(), "scoring_fingerprint": scoring.fingerprint,
              "rules": hg.HGB_DECISION_RULES}
    frozen["config_hash"] = hg.config_hash({k: v for k, v in frozen.items() if k != "rules"})
    frozen["frozen_at"] = datetime.now(timezone.utc).isoformat()
    path = _out(args) / f"frozen_config_{hg.HGB_VERSION}.json"
    if not _write_once(path, frozen):
        prev = json.loads(path.read_text())
        if prev["config_hash"] != frozen["config_hash"]:
            raise SystemExit(f"{path} already frozen with a different configuration ({prev['config_hash']})")
    dev.select("key", "season", "week", "gsis_id", "position", "label", "pred_C", "pred_ifp2", "pred_hgb",
               *[f"pred_{n}" for n in names]).write_parquet(_out(args) / "development_preds.parquet")
    (_out(args) / "development_grid.json").write_text(json.dumps({"mae": grid, "runtime_seconds": runtime}, indent=1))
    return {"chosen": chosen, "grid": grid, "ensemble": ens, "error_corr": round(corr, 4),
            "config_hash": frozen["config_hash"], "runtime_seconds": round(time.time() - t0, 1)}


def _frozen(args) -> dict:
    return json.loads((_out(args) / f"frozen_config_{hg.HGB_VERSION}.json").read_text())


# ----------------------------------------------------------------------------- prospective ledger


def cmd_predict(args) -> dict:
    """Pregame forecasts with the frozen configuration for one future week, written once. Rows whose
    team has kicked off are excluded (never backdated); refuses if every game has started."""
    from app.analytics.participation import live_population
    from app.analytics.weekly_learning import store_hash, team_kickoffs, training_store
    from app.analytics.forecast_features import features_for_week

    now = datetime.now(timezone.utc)
    frozen = _frozen(args)
    cfg = ch.ChallengerConfig.load()
    src, scoring = _sources(args)
    hg.check_frozen(frozen, scoring.fingerprint, cfg.hash())
    season, week = args.season, args.week
    lpath = _out(args) / "ledger" / f"s{season}w{week:02d}-pregame.json"
    if lpath.exists():      # write-once: a rerun reports the frozen run instead of fitting a new artifact
        prev = json.loads(lpath.read_text())
        return {"ledger": str(lpath), "written": False, "created_at": prev["created_at"],
                "artifact_hash": prev["artifact_hash"], "rows": len(prev["rows"]),
                "forecast_rows": sum(r["hgb_points"] is not None for r in prev["rows"])}
    kick = team_kickoffs(src, season, week)
    open_teams = {t for t, ko in kick.items() if now < ko}
    if not open_teams:
        raise SystemExit(f"every week-{week} game has kicked off: no pregame forecast possible")
    store = training_store(src, season, week)
    cutoff = season * 100 + week
    art = hg.fit_artifact(store, cutoff, frozen["params"], cfg, now.isoformat(), scoring.fingerprint,
                          frozen["config_hash"], store_hash(store, sorted({f for fs in frozen["features"].values()
                                                                           for f in fs})))
    pop = live_population(src, season, week)
    feats = features_for_week(src, season, week, pop).filter(pl.col("team").is_in(sorted(open_teams)))
    pred = art.predict(feats)
    rows = [{"gsis_id": r["gsis_id"], "position": r["position"], "team": r["team"],
             "kickoff": kick[r["team"]].isoformat(), "hgb_points": None if not np.isfinite(p) else round(float(p), 3)}
            for r, p in zip(feats.select("gsis_id", "position", "team").iter_rows(named=True), pred)]
    ledger = {"schema": hg.LEDGER_SCHEMA, "version": hg.HGB_VERSION, "kind": "pregame", "season": season,
              "week": week, "created_at": now.isoformat(), "config_hash": frozen["config_hash"],
              "artifact_hash": art.content_hash, "training_cutoff_key": cutoff,
              "data_hash": art.data_hash, "scoring_fingerprint": scoring.fingerprint,
              "teams_excluded_already_started": sorted(set(kick) - open_teams), "rows": rows}
    apath = _out(args) / "artifacts" / f"{hg.HGB_VERSION}-s{season}w{week:02d}-{art.content_hash}.json"
    wrote_a = _write_once(apath, art.to_dict())
    wrote_l = _write_once(lpath, ledger)
    return {"ledger": str(lpath), "written": wrote_l, "artifact": str(apath), "artifact_written": wrote_a,
            "rows": len(rows), "forecast_rows": sum(r["hgb_points"] is not None for r in rows),
            "artifact_bytes": apath.stat().st_size, "excluded_teams": ledger["teams_excluded_already_started"]}


# ----------------------------------------------------------------------------- diagnose (2023-26)


def cohorts(df: pl.DataFrame) -> dict[str, pl.Expr]:
    base = ch.cohorts(df)
    out = {k: v for k, v in base.items() if k not in ("high_projection_ge15",)}
    out.update({
        "high_volume_starters": base["starters_pool"] & (pl.col("h_xfpl_pg").fill_null(0) >= 15),
        "injury_return": (pl.col("last_injury_exit") == 1) | ((pl.col("inj_exits_recent") >= 1)
                                                                & (pl.col("weeks_since_last") >= 2)),
        "breakout_last2_above_prior": (pl.col("xfpl_recent") > 1.4 * pl.col("xfpl_eb")) & (pl.col("xfpl_eb") >= 5),
        "early_season_wk1_4": pl.col("week") <= 4,
    })
    return out


def compare(p: pl.DataFrame, models: list[str], baseline: str = "ifp2", seed: int = 20261015) -> dict:
    lab = p.filter(pl.col("label").is_not_null())
    cov = {m: round(float(lab[f"pred_{m}"].is_not_null().mean()), 4) for m in models}
    p = hg.common_rows(lab, [f"pred_{m}" for m in models])
    out = {"population": {"labeled": lab.height, "common": p.height, "coverage": cov}}
    flt = cohorts(p)
    for pos in [*POSITIONS, "ALL"]:
        g = p if pos == "ALL" else p.filter(pl.col("position") == pos)
        a = _arr(g, "label")
        r = {"n": g.height, "metrics": {m: metrics(a, _arr(g, f"pred_{m}")) for m in models},
             "minus_baseline_mae": {m: week_bootstrap_mae_diff(g, f"pred_{m}", f"pred_{baseline}", 1000, seed)
                                    for m in models if m != baseline}}
        if pos == "ALL":
            r["cohorts"] = {}
            for c, e in flt.items():
                cg = g.filter(e.fill_null(False))
                r["cohorts"][c] = {"n": cg.height, **{m: metrics(_arr(cg, "label"), _arr(cg, f"pred_{m}")) for m in models}}
                if cg.height >= 30:
                    r["cohorts"][c]["minus_baseline_mae"] = {
                        m: week_bootstrap_mae_diff(cg, f"pred_{m}", f"pred_{baseline}", 1000, seed)
                        for m in models if m != baseline}
        out[pos] = r
    out["pairs"] = pair_decisions(p, [f"pred_{m}" for m in models])
    return out


def decision(dev: dict, diag: dict, cand: str, base: str = "ifp2") -> dict:
    d = dev["ALL"]["minus_baseline_mae"][cand]
    a = diag["ALL"]
    m, b = a["metrics"][cand], a["metrics"][base]
    sp = a["cohorts"]["starters_pool"]
    pa, pb = diag["pairs"]["all"][f"pred_{cand}"], diag["pairs"]["all"][f"pred_{base}"]
    ca, cb = diag["pairs"]["close_calls"][f"pred_{cand}"], diag["pairs"]["close_calls"][f"pred_{base}"]
    fails = {
        "pooled_mae_worse": a["minus_baseline_mae"][cand]["diff"] > 0,
        "position_significantly_worse": any(diag[p]["minus_baseline_mae"][cand]["ci95"][0] > 0 for p in POSITIONS),
        "rmse_worse": m["rmse"] > b["rmse"] * 1.005,
        "bias_worse": abs(m["bias"]) > abs(b["bias"]) + 0.15,
        "starters_pool_worse": sp[cand]["mae"] > sp[base]["mae"] * 1.01,
        "pairs_worse": pa["accuracy"] < pb["accuracy"] - 0.005,
        "close_call_regret_worse": bool(ca["mean_regret"] > cb["mean_regret"] * 1.02),
    }
    gain = d["ci95"] is not None and d["ci95"][1] < 0
    return {"candidate": cand, "development_gain": gain, "development_diff": d, "failures": fails,
            "outcome": "prospective_shadow" if gain and not any(fails.values()) else "retain_ridge"}


def cmd_diagnose(args) -> dict:
    t0 = time.time()
    src, scoring = _sources(args)
    store = full_store(args, src, scoring.fingerprint)
    store_path = _out(args) / "store.parquet"
    frozen = _frozen(args)
    cfg = ch.ChallengerConfig.load()
    hg.check_frozen(frozen, scoring.fingerprint, cfg.hash())   # the replay must be the frozen model
    sel = json.loads((args.ifp_dir / "selection.json").read_text())
    if sel.get("scoring_fingerprint") != scoring.fingerprint:
        raise SystemExit("selection.json (frozen ifp-1.0.0 penalties) was made under different league scoring")
    weeks = sorted(w for s in hg.DIAGNOSTIC for w in ch.window_weeks(store, s))
    per_pos = {p: {"hgb": frozen["params"][p], "additive": hg.ADDITIVE} for p in POSITIONS}
    preds, runtime = _run_jobs(args, store_path, weeks, {p: {"hgb": per_pos[p]["hgb"]} for p in POSITIONS},
                               permute=True)
    add, rt2 = _run_jobs(args, store_path, weeks, {p: {"additive": hg.ADDITIVE} for p in POSITIONS})
    runtime.update(rt2)
    diag = (store.filter(pl.col("key").is_in([s * 100 + w for s, w in weeks]))
            .join(_baselines(store, weeks, sel, cfg), on=["key", "gsis_id"], how="left")
            .join(preds, on=["key", "gsis_id"], how="left").join(add, on=["key", "gsis_id"], how="left"))
    models = ["C", "ifp2", "hgb", "additive"]
    if frozen.get("ensemble"):
        diag = diag.with_columns(hg.ensemble(pl.col("pred_ifp2"), pl.col("pred_hgb"),
                                             frozen["ensemble"]["w_ridge"]).alias("pred_ens"))
        models.append("ens")
    dev = pl.read_parquet(_out(args) / "development_preds.parquet").join(
        store.drop("label", "season", "week", "position"), on=["key", "gsis_id"], how="left").with_columns(
        pl.col("pred_C").alias("pred_C"))
    dev_models = ["C", "ifp2", "hgb"]
    if frozen.get("ensemble"):
        dev = dev.with_columns(hg.ensemble(pl.col("pred_ifp2"), pl.col("pred_hgb"),
                                           frozen["ensemble"]["w_ridge"]).alias("pred_ens"))
        dev_models.append("ens")
    dev_rep = compare(dev, dev_models)
    windows = {"2023-2024": diag.filter(pl.col("season").is_in(list(hg.DIAGNOSTIC[0]))),
               "2025-2026": diag.filter(pl.col("season").is_in(list(hg.DIAGNOSTIC[1])))}
    rep = {w: compare(d, models) for w, d in windows.items()}
    rep["pooled"] = compare(diag, models)
    decisions = {m: decision(dev_rep, rep["pooled"], m) for m in dev_models if m not in ("C", "ifp2")}
    # permutation importance (out-of-sample, 2023-26): MAE increase when a group is shuffled
    common = diag.filter(pl.col("pred_hgb").is_not_null() & pl.col("label").is_not_null())
    base_mae = metrics(_arr(common, "label"), _arr(common, "pred_hgb"))["mae"]
    importance = {}
    for pos in [*POSITIONS, "ALL"]:
        g = common if pos == "ALL" else common.filter(pl.col("position") == pos)
        importance[pos] = {}
        for grp in hg.FEATURE_GROUPS:
            col = f"pred_hgb__perm_{grp}"
            if col not in g.columns:
                continue
            # a position without this group (e.g. QB has no game-class flags) is unaffected by shuffling it:
            # its unpermuted prediction stands in, so every MAE here is on the same rows
            gg = g.with_columns(pl.coalesce(pl.col(col), pl.col("pred_hgb")).alias(col))
            importance[pos][grp] = round(metrics(_arr(gg, "label"), _arr(gg, col))["mae"]
                                         - metrics(_arr(gg, "label"), _arr(gg, "pred_hgb"))["mae"], 4)
    # error complementarity
    er, eh = _arr(common, "label") - _arr(common, "pred_ifp2"), _arr(common, "label") - _arr(common, "pred_hgb")
    keep = ["key", "season", "week", "gsis_id", "position", "label", *[f"pred_{m}" for m in models]]
    diag.select(keep).write_parquet(_out(args) / "diagnostic_preds.parquet")
    report = {"schema": "hgb-research-report/v1", "version": hg.HGB_VERSION, "created_at": datetime.now(timezone.utc)
              .isoformat(), "frozen_config_hash": frozen["config_hash"], "scoring_fingerprint": scoring.fingerprint,
              "development": dev_rep, "diagnostic": rep, "decisions": decisions,
              "permutation_importance_mae_increase": importance, "base_mae": base_mae,
              "error_correlation_2023_26": round(float(np.corrcoef(er, eh)[0, 1]), 4),
              "runtime_seconds": {"jobs": runtime, "total": round(time.time() - t0, 1)}}
    (_out(args) / "report.json").write_text(json.dumps(ch.round_floats(report), indent=1, default=str))
    return {"decisions": decisions, "mae": {w: {m: r["ALL"]["metrics"][m]["mae"] for m in models} for w, r in rep.items()},
            "dev_mae": {m: dev_rep["ALL"]["metrics"][m]["mae"] for m in dev_models}, "importance": importance["ALL"],
            "runtime_seconds": report["runtime_seconds"]["total"]}


def cmd_ablate(args) -> dict:
    """Refit the frozen configuration without each feature group on development weeks (explanatory;
    selection is already frozen)."""
    src, scoring = _sources(args)
    store = full_store(args, src, scoring.fingerprint)      # validates (or rebuilds) the cached store
    frozen = _frozen(args)
    hg.check_frozen(frozen, scoring.fingerprint, ch.ChallengerConfig.load().hash())
    weeks = ch.window_weeks(store, hg.DEVELOPMENT)
    payloads = []
    for grp in [None, *hg.FEATURE_GROUPS]:
        for pos in POSITIONS:
            payloads.append((str(_out(args) / "store.parquet"), weeks, pos, grp, frozen["params"][pos]))
    with ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        res = list(pool.map(_ablate_job, payloads))
    out: dict = {}
    for grp, pos, mae, n in res:
        out.setdefault(grp or "none_removed", {})[pos] = {"mae": mae, "n": n}
    full = out["none_removed"]
    delta = {g: {p: round(v[p]["mae"] - full[p]["mae"], 4) for p in v} for g, v in out.items() if g != "none_removed"}
    res_out = {"mae": out, "mae_increase_when_removed": delta}
    (_out(args) / "ablation.json").write_text(json.dumps(res_out, indent=1))
    return res_out


def _ablate_job(payload):
    store_path, weeks, pos, grp, params = payload
    store = pl.read_parquet(store_path).filter(pl.col("position") == pos)
    cfg = ch.ChallengerConfig.load()
    feats = {p: [f for f in hg.hgb_features(p, cfg) if grp is None or hg.group_of(f) != grp] for p in POSITIONS}
    hl = {p: cfg.positions[p].get("half_life_weeks") for p in POSITIONS}
    p = hg.walk_forward(store, weeks, feats, {"x": params}, hl).join(store.select("key", "gsis_id", "label"),
                                                                     on=["key", "gsis_id"])
    p = p.filter(pl.col("pred_x").is_not_null())
    return grp, pos, metrics(_arr(p, "label"), _arr(p, "pred_x"))["mae"], p.height


def cmd_cases(args) -> dict:
    import duckdb

    src, scoring = _sources(args)
    store = full_store(args, src, scoring.fingerprint)
    d = pl.read_parquet(_out(args) / "diagnostic_preds.parquet").join(
        store.select("key", "gsis_id", "carries_pg", "targets_pg", "snap_pct", "xfpl_eb", "prev_xfpl_pg",
                     "games_recent", "inj_exits_recent", "last_injury_exit", "last_role_low", "team_changed",
                     "pts_recent", "rush_share", "target_share", "team_pts_pg"), on=["key", "gsis_id"])
    con = duckdb.connect(str(args.live_db), read_only=True)
    names = dict(con.execute("SELECT gsis_id, display_name FROM nfl_players").fetchall())
    con.close()
    d = d.filter(pl.col("pred_hgb").is_not_null() & pl.col("pred_ifp2").is_not_null()).with_columns(
        pl.col("gsis_id").replace_strict(names, default=None).alias("name"),
        (pl.col("pred_hgb") - pl.col("pred_ifp2")).alias("hgb_minus_ridge"),
        ((pl.col("label") - pl.col("pred_ifp2")).abs() - (pl.col("label") - pl.col("pred_hgb")).abs()).alias("hgb_gain"))
    show = ["name", "season", "week", "position", "games_recent", "carries_pg", "targets_pg", "snap_pct", "xfpl_eb",
            "prev_xfpl_pg", "inj_exits_recent", "last_role_low", "team_changed", "pred_ifp2", "pred_hgb", "label"]
    big = d.filter(pl.col("hgb_minus_ridge").abs() >= 3)
    out = {"n_rows": d.height, "n_diff_ge_3": big.height,
           "share_hgb_closer_when_diff_ge_3": round(float((big["hgb_gain"] > 0).mean()), 4) if big.height else None,
           "mean_abs_diff": round(float(d["hgb_minus_ridge"].abs().mean()), 3),
           "hgb_better": big.sort("hgb_gain", descending=True).head(args.limit).select(show)
           .with_columns(pl.col(pl.Float64).round(2)).to_dicts(),
           "hgb_worse": big.sort("hgb_gain").head(args.limit).select(show)
           .with_columns(pl.col(pl.Float64).round(2)).to_dicts()}
    (_out(args) / "cases.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["develop", "predict", "diagnose", "ablate", "cases"])
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--week", type=int)
    ap.add_argument("--limit", type=int, default=4)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--workers", type=int, default=max(1, min(4, os.cpu_count() or 2)))
    args = ap.parse_args(argv)
    if args.command == "predict" and args.week is None:
        ap.error("predict needs --week")
    cmds = {"develop": cmd_develop, "predict": cmd_predict, "diagnose": cmd_diagnose, "ablate": cmd_ablate,
            "cases": cmd_cases}
    print(json.dumps(cmds[args.command](args), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
