"""Research: two-target opportunity + efficiency projections (ifp-oe, research only; see
docs/research/opportunity_efficiency.md). Offline, read-only on both databases; prints aggregates.

    cd backend
    uv run python ../scripts/opportunity_efficiency_research.py run

Inputs as in challenger_research.py (play-by-play exits in --exits-dir/pbp_exits, the ifp-1.0.0
selection.json in --ifp-dir). Outputs go to <ifp-dir>/research/opportunity_efficiency/ (git-ignored).
Never writes the ifp-2 config, a production artifact or any ledger.
"""

from __future__ import annotations

import argparse
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

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.analytics import challenger as ch  # noqa: E402
from app.analytics import opportunity_efficiency as oe  # noqa: E402
from app.analytics.forecast_backtest import (_arr, build_store, completed_weeks, frozen_penalties,  # noqa: E402
                                             metrics, walk_forward as ifp_walk_forward)
from app.analytics.forecast_features import POSITIONS  # noqa: E402
from app.config import get_settings  # noqa: E402


def _out(args) -> Path:
    p = args.ifp_dir / "research" / "opportunity_efficiency"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _frame_hash(df: pl.DataFrame | None) -> str | None:
    """Order-independent content hash of a source frame."""
    if df is None:
        return None
    return hashlib.sha256(np.sort(df.hash_rows(seed=0).to_numpy()).tobytes()).hexdigest()[:16]


def code_hash() -> str:
    """Hash of every module that can shape a cached result: all of app/ and every script (this one
    and the store builders it imports). Deliberately broader than today's import graph, so a change
    to any helper the store is built with invalidates the cache."""
    root = Path(ch.__file__).resolve().parents[3]
    files = sorted((root / "backend" / "app").rglob("*.py")) + sorted((root / "scripts").glob("*.py"))
    h = hashlib.sha256()
    for f in files:
        h.update(str(f.relative_to(root)).encode() + b"\0" + f.read_bytes())
    return h.hexdigest()[:16]


def input_fingerprint(args, src, scoring_fingerprint: str, weeks) -> str:
    """Everything the cached store depends on: source data, weeks, scoring and the code that builds
    it. Any change rebuilds the cache."""
    parts = {"season": args.season, "weeks": weeks, "scoring": scoring_fingerprint, "polars": pl.__version__,
             "code": code_hash(),
             **{n: _frame_hash(getattr(src, n)) for n in ("panel", "games", "injuries", "exits", "rosters", "defense")}}
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def full_store(args, src, scoring_fingerprint: str) -> pl.DataFrame:
    """Cached feature store, reused only when its recorded input fingerprint still matches."""
    path = _out(args) / "store_oe.parquet"
    meta = path.with_suffix(".meta.json")
    weeks = completed_weeks(src, list(range(ch.TRAIN_START, args.season + 1)))
    fp = input_fingerprint(args, src, scoring_fingerprint, weeks)
    if path.exists() and meta.exists() and not args.rebuild and json.loads(meta.read_text()).get("fingerprint") == fp:
        return pl.read_parquet(path)
    store = oe.attach_targets(ch.add_last_game(build_store(src, weeks), src.panel), src.panel)
    store.write_parquet(path)
    meta.write_text(json.dumps({"fingerprint": fp, "rows": store.height, "weeks": len(weeks)}))
    return store


def _job(payload):
    store_path, weeks, tag, comp, lams = payload
    store = pl.read_parquet(store_path)
    cfg = ch.ChallengerConfig.load()
    cand = next(c for c in oe.CANDIDATES if oe.tag_of(c) == tag)
    feats = {p: oe.component_features(cand, p, cfg)[comp] for p in POSITIONS}
    hl = {p: oe.half_life(p, cfg) for p in POSITIONS}
    return (tag, comp), oe.walk_forward(store, weeks, tag, feats, comp, lams, hl)


def _components(args, store_path, weeks, grids: dict[tuple[str, str], dict[str, tuple]]) -> dict:
    payloads = [(str(store_path), weeks, tag, comp, lams) for (tag, comp), lams in grids.items()]
    # spawn, not fork: forking a process that already ran polars' thread pool can deadlock the children
    with ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        return dict(pool.map(_job, payloads))


def _baselines(store, weeks, sel, cfg) -> pl.DataFrame:
    wf = ifp_walk_forward(store, weeks, frozen_penalties(sel), ch.TRAIN_START)
    c = pl.concat([wf.filter(pl.col("position") == pos).select(
        "key", "gsis_id", pl.col(f"pred_C_{sel['positions'][pos]['lambda']['C']:g}").alias("pred_C"))
        for pos in POSITIONS])
    return c.join(ch.challenger_predictions(store, weeks, cfg), on=["key", "gsis_id"], how="left")


def _frame(store, weeks, comps, sel, cfg) -> pl.DataFrame:
    df = store.filter(pl.col("key").is_in([s * 100 + w for s, w in weeks]))
    df = df.join(_baselines(store, weeks, sel, cfg), on=["key", "gsis_id"], how="left")
    for p in comps.values():
        df = df.join(p, on=["key", "gsis_id"], how="left")
    return df


def _assemble(df: pl.DataFrame, best: dict) -> pl.DataFrame:
    cols = []
    for cand in oe.CANDIDATES:
        tag = oe.tag_of(cand)
        cols.append(pl.coalesce([pl.when(pl.col("position") == pos).then(oe.combine(df, tag, lo, le))
                                 for pos, (lo, le) in best[cand.name].items()]).alias(f"pred_{cand.name}"))
        for comp, i in (("opp", 0), ("eff", 1)):
            cols.append(pl.coalesce([pl.when(pl.col("position") == pos).then(pl.col(f"pred_{tag}_{comp}@{lams[i]:g}"))
                                     for pos, lams in best[cand.name].items()]).alias(f"{cand.name}_{comp}"))
    return df.with_columns(cols)


def _position_baseline(store: pl.DataFrame, weeks, cfg) -> pl.DataFrame:
    """Efficiency shrunk all the way to the position baseline: recency-weighted mean t_eff of the
    training rows (same rows, half-life and cutoff as the efficiency component)."""
    out = []
    for season, week in weeks:
        k = season * 100 + week
        for pos in POSITIONS:
            tr = oe.component_train_rows(store, k, pos, "eff")
            if tr.height == 0:
                continue
            w = ch.recency_weights(tr["key"].to_numpy(), k, oe.half_life(pos, cfg))
            out.append({"key": k, "position": pos, "eff_pos_baseline": float(np.average(tr["t_eff"].to_numpy(), weights=w))})
    return pl.DataFrame(out)


def shrinkage_study(df: pl.DataFrame, store: pl.DataFrame, weeks, cfg, opp_col: str) -> dict:
    """Where should efficiency shrink? Efficiency-target MAE (rows with target xFP) and summed-points
    MAE (all rows with an opportunity forecast) for each shrinkage target."""
    d = df.join(_position_baseline(store, weeks, cfg), on=["key", "position"], how="left").with_columns(
        pl.lit(0.0).alias("eff_zero"),
        (pl.col("yd_over_exp").fill_null(0) + pl.col("td_over_exp").fill_null(0)).alias("eff_player_hist"),
    ).with_columns((pl.col("eff_pos_baseline") + pl.col("eff_player_hist")).alias("eff_pos_plus_player"))
    names = ("eff_zero", "eff_pos_baseline", "eff_player_hist", "eff_pos_plus_player", "OE_D_eff", "OE_E_eff")
    # one population for every shrinkage target: rows with a known efficiency target, an opportunity
    # forecast and a value from every compared method (the ridge components abstain without history)
    shared = d.filter(pl.col("t_eff").is_not_null() & pl.col("label").is_not_null() & pl.col(opp_col).is_not_null()
                      & pl.all_horizontal([pl.col(n).is_not_null() for n in names]))
    out = {"n": shared.height}
    for name in names:
        out[name] = {"eff_mae": metrics(_arr(shared, "t_eff"), _arr(shared, name))["mae"],
                     "eff_bias": metrics(_arr(shared, "t_eff"), _arr(shared, name))["bias"],
                     "points_mae_with_selected_opportunity": metrics(
                         _arr(shared, "label"), np.maximum(_arr(shared, opp_col) + _arr(shared, name), 0.0))["mae"]}
    return out


def cmd_run(args) -> dict:
    t0 = time.time()
    src, scoring = cr.sources(SimpleNamespace(live_db=args.live_db, history_db=args.history_db, season=args.season,
                                              out=args.exits_dir))
    store = full_store(args, src, scoring.fingerprint)
    store_path = _out(args) / "store_oe.parquet"
    cfg = ch.ChallengerConfig.load()
    sel = json.loads((args.ifp_dir / "selection.json").read_text())
    if sel.get("scoring_fingerprint") != scoring.fingerprint:
        raise SystemExit("selection.json (frozen ifp-1.0.0 penalties) was made under different league scoring")

    # ---- development: every penalty of every component, then the best pair per candidate/position
    dev_weeks = ch.window_weeks(store, oe.DEVELOPMENT)
    grids = {}
    for cand in oe.CANDIDATES:
        for comp, g in (("opp", cand.lam_grid_opp), ("eff", cand.lam_grid_eff)):
            cur = grids.setdefault((oe.tag_of(cand), comp), {p: () for p in POSITIONS})
            for p in POSITIONS:
                cur[p] = tuple(sorted(set(cur[p]) | set(g)))
    dev = _frame(store, dev_weeks, _components(args, store_path, dev_weeks, grids), sel, cfg)
    best = {}
    for cand in oe.CANDIDATES:
        tag, best[cand.name] = oe.tag_of(cand), {}
        for pos in POSITIONS:
            g = dev.filter(pl.col("position") == pos)
            pairs = oe.lambda_pairs(cand)
            P = g.select("label", *[oe.combine(g, tag, *pr).alias(f"_p{i}") for i, pr in enumerate(pairs)]).drop_nulls()
            scores = {pr: metrics(P["label"].to_numpy(), P[f"_p{i}"].to_numpy())["mae"] for i, pr in enumerate(pairs)}
            best[cand.name][pos] = min(scores, key=lambda pr: (scores[pr], -pr[1], -pr[0]))
    dev = _assemble(dev, best)
    models = {"C": "pred_C", "ifp2": "pred_ifp2", **{c.name: f"pred_{c.name}" for c in oe.CANDIDATES}}
    dev_rep = oe.compare(dev, models)
    chosen = min((c.name for c in oe.CANDIDATES), key=lambda n: dev_rep["ALL"]["metrics"][n]["mae"])

    # ---- diagnostic windows (already inspected): frozen development penalties
    diag_weeks = sorted(w for s in oe.DIAGNOSTIC for w in ch.window_weeks(store, s))
    dgrids = {}
    for cand in oe.CANDIDATES:
        for comp, i in (("opp", 0), ("eff", 1)):
            cur = dgrids.setdefault((oe.tag_of(cand), comp), {p: () for p in POSITIONS})
            for p in POSITIONS:
                cur[p] = tuple(sorted(set(cur[p]) | {best[cand.name][p][i]}))
    diag = _assemble(_frame(store, diag_weeks, _components(args, store_path, diag_weeks, dgrids), sel, cfg), best)
    windows = {"2023-2024": diag.filter(pl.col("season").is_in(list(oe.DIAGNOSTIC[0]))),
               "2025-2026": diag.filter(pl.col("season").is_in(list(oe.DIAGNOSTIC[1])))}
    diag_rep = {w: oe.compare(d, models) for w, d in windows.items()}
    diag_rep["pooled"] = oe.compare(diag, models)
    decisions = {c.name: oe.decision(dev_rep, diag_rep["pooled"], c.name) for c in oe.CANDIDATES}

    validity = {}
    for name, frame in (("development", dev), ("diagnostic_pooled", diag)):
        validity[name] = {c.name: oe.component_validity(frame, f"{c.name}_opp", f"{c.name}_eff", "pred_ifp2")
                          for c in oe.CANDIDATES}
    shrink = shrinkage_study(dev, store, dev_weeks, cfg, f"{chosen}_opp")
    keep = ["key", "season", "week", "gsis_id", "position", "label", "t_opp", "t_eff",
            *[f"pred_{n}" for n in models], *[f"{c.name}_{k}" for c in oe.CANDIDATES for k in ("opp", "eff")]]
    dev.select(keep).write_parquet(_out(args) / "development_preds.parquet")
    diag.select(keep).write_parquet(_out(args) / "diagnostic_preds.parquet")
    cov = oe.coverage(store)
    report = {"schema": "opportunity-efficiency-report/v1", "version": oe.OE_VERSION,
              "created_at": datetime.now(timezone.utc).isoformat(), "scoring_fingerprint": scoring.fingerprint,
              "scoring_compatibility": oe.scoring_compatibility(scoring.weights), "ifp2_config_hash": cfg.hash(),
              "rules": oe.OE_DECISION_RULES, "candidates": [c.__dict__ for c in oe.CANDIDATES],
              "target_coverage": cov,
              "development": {"seasons": oe.DEVELOPMENT, "weeks": len(dev_weeks), "best_lambda": best,
                              "report": dev_rep, "selected": chosen},
              "diagnostic": diag_rep, "decisions": decisions, "selected_decision": decisions[chosen],
              "component_validity": validity, "efficiency_shrinkage": shrink,
              "prospective": "no frozen prospective weeks with outcomes exist yet for this method (2026 week 6 "
                             "is not final); nothing here is confirmation evidence",
              "runtime_seconds": round(time.time() - t0, 1)}
    apath = _out(args) / f"research_artifact_{oe.OE_VERSION}.json"
    apath.unlink(missing_ok=True)       # an earlier run's artifact never outlives a rerun that rejects it
    if decisions[chosen]["outcome"] == "continue_research":
        cand = next(c for c in oe.CANDIDATES if c.name == chosen)
        cut = int(store["key"].max()) + 1
        art = oe.fit_artifact(store, cut, cand, {p: {"opp": lo, "eff": le} for p, (lo, le) in best[chosen].items()},
                              cfg, report["created_at"], scoring.fingerprint, cov,
                              {"development": dev_rep["ALL"]["metrics"], "decision": decisions[chosen]})
        apath.write_text(json.dumps(art.to_dict(), default=str))
        report["artifact_hash"] = art.hash()
    (_out(args) / "report.json").write_text(json.dumps(ch.round_floats(report), indent=1, default=str))
    return {"selected": chosen, "decision": decisions[chosen], "best_lambda": best,
            "coverage": dev_rep["population"], "dev_mae": {n: dev_rep["ALL"]["metrics"][n]["mae"] for n in models},
            "diag_mae": {w: {n: r["ALL"]["metrics"][n]["mae"] for n in models} for w, r in diag_rep.items()},
            "runtime_seconds": report["runtime_seconds"]}


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run"])
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"),
                    help="directory containing pbp_exits/ (weekly_learning.py sync-exits)")
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--workers", type=int, default=max(1, min(4, (os.cpu_count() or 2) - 1)))
    args = ap.parse_args(argv)
    print(json.dumps({"run": cmd_run}[args.command](args), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
