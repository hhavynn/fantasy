"""ifp-2.2.0 backtest guard: the combined model against ifp-2.1.0 on identical player-weeks (read-only on both DBs).

    cd backend
    uv run python ../scripts/weekly_learning.py sync-depth            # nflverse depth charts (depth_tier input)
    uv run python ../scripts/ifp22_backtest.py build                  # store + component inputs (cached)
    uv run python ../scripts/ifp22_backtest.py run                    # walk-forward, leave-one-out, the guard
    uv run python ../scripts/ifp22_backtest.py participation          # QB P(plays) map (ipm-cal-0.1.0), unconditional

Walk-forward exactly as production trains (app.analytics.context_model.fit: ifp-2.1.0's training rows, frozen
penalties and recency, refitted every week on strictly earlier weeks; the QB start model refitted the same way). Per
position it predicts ifp-2.1.0 (`b21`), the combined candidate (`full`, every component of
context_model.CANDIDATE), every leave-one-out variant (`drop_<component>`) and every single addition
(`only_<component>`). Rule (owner-approved engineering safeguard, fixed before the run): a component whose
leave-one-out difference MAE(full) - MAE(drop_c) has a week-clustered 95% CI entirely above 0 on the evaluation window
makes the position clearly worse and is left out for that position; every neutral or better component ships. A second
pass re-checks the shipped set the same way. QB P(plays) (`participation`): unconditional MAE of P x points with the
walk-forward isotonic map vs the base P, same rule.

Outputs: <ifp-dir>/research/ifp22/ (git-ignored): store.parquet, candidates.parquet, preds.parquet, report.json.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import challenger_research as cr  # noqa: E402
import hgb_research as hr  # noqa: E402

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.analytics import challenger as ch  # noqa: E402
from app.analytics import context_model as cm  # noqa: E402
from app.analytics import injury_model as im  # noqa: E402
from app.analytics.forecast_backtest import build_store, metrics, week_bootstrap_mae_diff  # noqa: E402
from app.analytics.forecast_features import POSITIONS, key  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.connection import connect as db_connect  # noqa: E402

SEED = 20261019
RESAMPLES = 2000
EVALUATION = ((2023, 1), (2026, 18))      # confirmation 2023-24 + the 2025-26 replay, every fully final week
DEVELOPMENT = ((2021, 1), (2022, 18))     # reported only


def _out(args) -> Path:
    p = args.ifp_dir / "research" / "ifp22"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cons(args):
    live = db_connect(args.live_db, read_only=True)
    hist = db_connect(args.history_db, read_only=True) if args.history_db.exists() else None
    return live, hist


def build(args) -> tuple[pl.DataFrame, pl.DataFrame, dict]:
    """(store with every component column, start candidates, provenance), cached by an input fingerprint."""
    import weekly_learning as wlcli

    spath, cpath, meta = _out(args) / "store.parquet", _out(args) / "candidates.parquet", _out(args) / "store.meta.json"
    if getattr(args, "reuse", False) and spath.exists() and cpath.exists() and meta.exists():
        return pl.read_parquet(spath), pl.read_parquet(cpath), {**json.loads(meta.read_text()), "reused": True}
    src, scoring = hr._sources(args)
    weeks = hr.final_weeks(args, src)
    depth_dir = args.exits_dir / wlcli.DEPTH_DIR
    manifest = (depth_dir / "manifest.json").read_text() if (depth_dir / "manifest.json").exists() else ""
    fp = hashlib.sha256(json.dumps({"store": hr.input_fingerprint(args, src, scoring.fingerprint, weeks),
                                    "depth": manifest}, sort_keys=True).encode()).hexdigest()[:16]
    if spath.exists() and cpath.exists() and meta.exists() and not args.rebuild \
            and json.loads(meta.read_text()).get("fingerprint") == fp:
        return pl.read_parquet(spath), pl.read_parquet(cpath), json.loads(meta.read_text())
    t = time.time()
    live, hist = _cons(args)
    try:
        inputs = cm.load_inputs([hist, live], depth_dir, args.season)
        codes = wlcli.roster_codes(live, hist)
    finally:
        for c in (live, hist):
            if c is not None:
                c.close()
    store = build_store(src, weeks)
    t_store = time.time() - t
    store = cm.with_training_inputs(src, store, inputs, codes)
    t_inputs = time.time() - t - t_store
    cands = cm.start_candidates(src, inputs, [w for w in weeks if w[0] >= 2020])
    prov = {"fingerprint": fp, "rows": store.height, "weeks": len(weeks), "candidates": cands.height,
            "scoring_fingerprint": scoring.fingerprint, "depth_note": inputs.depth_note,
            "depth_coverage": {int(s): round(float(g["dc_tier"].is_not_null().mean()), 3)
                               for (s,), g in store.filter(pl.col("position").is_in(["QB", "RB", "WR"]))
                               .group_by("season") if s >= cm.DEPTH_FROM},
            "seconds": {"store": round(t_store, 1), "inputs": round(t_inputs, 1), "total": round(time.time() - t, 1)}}
    store.write_parquet(spath)
    cands.write_parquet(cpath)
    meta.write_text(json.dumps(prov, indent=1))
    return store, cands, prov


def _weeks(store: pl.DataFrame, window) -> list[tuple[int, int]]:
    (s0, w0), (s1, w1) = window
    return sorted({(int(s), int(w)) for s, w in store.select("season", "week").unique().iter_rows()
                   if key(s0, w0) <= key(int(s), int(w)) <= key(s1, w1)})


def variants(position: str) -> dict[str, tuple[str, ...]]:
    """name -> components of every model compared at the position."""
    cand = tuple(c for c in cm.CANDIDATE[position] if c in cm.FEATURE_COMPONENTS)
    out = {"b21": (), "full": cand}
    for c in cand:
        out[f"drop_{c}"] = tuple(x for x in cand if x != c)
        if len(cand) > 1:
            out[f"only_{c}"] = (c,)
    return out


def walk_forward(store: pl.DataFrame, cands: pl.DataFrame, weeks: list[tuple[int, int]], cfg,
                 var: dict[str, dict[str, tuple[str, ...]]] | None = None) -> pl.DataFrame:
    """pred_<variant> for every store row of `weeks` (null = abstention). Training rows strictly earlier (asserted)."""
    var = var or {p: variants(p) for p in POSITIONS}
    out = []
    for season, week in weeks:
        k = key(season, week)
        rows_k = store.filter(pl.col("key") == k)
        start = cm.fit_start(cands, season, week)
        probs = cm.start_probabilities(start, cands.filter(pl.col("key") == k))
        rows_k = cm.with_pi(rows_k, probs)
        for pos in POSITIONS:
            rows = rows_k.filter(pl.col("position") == pos)
            if rows.height == 0:
                continue
            ok = rows["games_recent"].fill_null(0).to_numpy() > 0
            cols = {}
            for name, comps in var[pos].items():
                models, n, fb = cm.fit(store, season, week, cfg, {pos: comps}, positions=(pos,))
                assert not n or store.filter((pl.col("key") < k)).height > 0
                p = cm.predict_points(models, fb, rows, pos, comps)
                cols[f"pred_{name}"] = np.where(ok, p, np.nan)
            out.append(rows.select("key", "season", "week", "gsis_id", "position", "team", "label", "pi_start")
                       .with_columns([pl.Series(c, v).fill_nan(None) for c, v in cols.items()]))
        print(f"  {season} w{week:02d}", file=sys.stderr, flush=True)
    return pl.concat(out, how="diagonal_relaxed")


def _compare(p: pl.DataFrame, a: str, b: str, seed: int) -> dict:
    d = week_bootstrap_mae_diff(p, f"pred_{a}", f"pred_{b}", RESAMPLES, seed, exact=True)
    return {"diff": None if d["diff"] is None else round(d["diff"], 4),
            "ci95": None if d["ci95"] is None else [round(v, 4) for v in d["ci95"]]}


def position_report(p: pl.DataFrame, pos: str, names: list[str], seed: int) -> dict:
    """Identical rows: every named model forecast the row and the label exists."""
    d = p.filter((pl.col("position") == pos) & pl.col("label").is_not_null()
                 & pl.all_horizontal([pl.col(f"pred_{n}").is_not_null() for n in names]))
    out = {"n": d.height, "weeks": d.select("key").n_unique(), "models": {}}
    y = d["label"].to_numpy()
    for n in names:
        e = d[f"pred_{n}"].to_numpy() - y
        out["models"][n] = {"mae": round(float(np.abs(e).mean()), 4), "rmse": round(float(np.sqrt((e ** 2).mean())), 4),
                            "bias": round(float((y - d[f"pred_{n}"].to_numpy()).mean()), 4)}
    out["full_vs_b21"] = _compare(d, "full", "b21", seed)
    out["leave_one_out"] = {n[5:]: _compare(d, "full", n, seed) for n in names if n.startswith("drop_")}
    out["single_addition"] = {n[5:]: _compare(d, n, "b21", seed) for n in names if n.startswith("only_")}
    return out


def guard(rep: dict) -> dict[str, list[str]]:
    """Components whose leave-one-out CI is entirely above 0 (the full model is clearly worse with them)."""
    return {pos: sorted(c for c, v in r["leave_one_out"].items() if v["ci95"] and v["ci95"][0] > 0)
            for pos, r in rep.items()}


def cmd_build(args) -> dict:
    store, cands, prov = build(args)
    return prov


def cmd_run(args) -> dict:
    t = time.time()
    store, cands, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    weeks = _weeks(store, EVALUATION)
    dev = _weeks(store, DEVELOPMENT) if args.development else []
    preds = walk_forward(store, cands, weeks + dev, cfg)
    preds.write_parquet(_out(args) / "preds.parquet")
    ev = preds.filter(pl.col("key") >= key(*EVALUATION[0]))
    rep = {pos: position_report(ev, pos, [f"{n}" for n in variants(pos)], SEED) for pos in POSITIONS}
    dropped = guard(rep)
    shipped = {pos: [c for c in variants(pos)["full"] if c not in dropped[pos]] for pos in POSITIONS}
    second = {}
    if any(dropped.values()):
        var = {pos: {"b21": (), "full": tuple(shipped[pos]),
                     **{f"drop_{c}": tuple(x for x in shipped[pos] if x != c) for c in shipped[pos]}} for pos in POSITIONS}
        p2 = walk_forward(store, cands, weeks, cfg, var)
        second = {pos: position_report(p2, pos, list(var[pos]), SEED) for pos in POSITIONS}
    out = {"schema": "ifp22-backtest/v1", "created_at": datetime.now(timezone.utc).isoformat(),
           "rule": "leave-one-out MAE(full) - MAE(drop_c), week-clustered 95% CI (whole weeks resampled, "
                   f"{RESAMPLES} draws, seed {SEED}); CI entirely above 0 -> component left out for the position",
           "evaluation_window": EVALUATION, "store": prov, "positions": rep, "dropped": dropped, "shipped": shipped,
           "second_pass": second, "runtime_seconds": round(time.time() - t, 1)}
    if dev:
        dv = preds.filter(pl.col("key") < key(*EVALUATION[0]))
        out["development"] = {pos: position_report(dv, pos, list(variants(pos)), SEED) for pos in POSITIONS}
    (_out(args) / "report.json").write_text(json.dumps(out, indent=1, default=str))
    return {k: out[k] for k in ("dropped", "shipped", "runtime_seconds")} | {
        "full_vs_b21": {p: rep[p]["full_vs_b21"] for p in POSITIONS}}


def cmd_participation(args) -> dict:
    """QB P(plays): ipm-1.0.0's walk-forward out-of-sample P (participation_calibration.py's cache) vs the same P
    through the expanding-window isotonic map refitted every week (the study's own walk-forward), times the
    full model's conditional QB points, against actual points (0 when he did not play)."""
    import participation_calibration as pcal_cli
    from app.analytics import participation_calibration as pc

    rp = _out(args) / "preds.parquet"
    if not rp.exists():
        raise SystemExit("run `ifp22_backtest.py run` first (conditional points)")
    ns = SimpleNamespace(models=args.ifp_dir, live_db=args.live_db, history_db=args.history_db, season=args.season)
    cache = sorted((args.ifp_dir / "research").glob("oos_base_*.parquet"), key=lambda f: f.stat().st_mtime)
    if not cache:
        raise SystemExit(f"no oos_base_*.parquet in {args.ifp_dir / 'research'}: run participation_calibration.py run "
                         f"--models {ns.models}")
    oos = pl.read_parquet(cache[-1])
    qb = oos.filter(pl.col("position") == cm.CAL_POSITION)
    keys = sorted(qb.filter(pl.col("key") >= key(*EVALUATION[0]))["key"].unique().to_list())
    cal = pc.walk_forward_calibration(qb, keys, methods=("none", "isotonic"), windows=("expanding",))
    pts = pl.read_parquet(rp).filter(pl.col("position") == "QB").select("key", "gsis_id", "pred_full")
    # every QB the participation model forecast (appeared or not): the full model's conditional points where the row is
    # in the conditional store (he appeared), else the study's frozen conditional points (`cond`, the column its own
    # unconditional gate used); actual points are 0 when he did not play
    d = (cal.join(pts, on=["key", "gsis_id"], how="left")
         .filter(pl.col("p_none").is_not_null() & pl.col("p_isotonic_expanding").is_not_null()
                 & pl.col("played").is_not_null() & pl.col("cond").is_not_null())
         .with_columns(pl.coalesce("pred_full", "cond").alias("_pts"),
                       pl.when(pl.col("played") == 1).then(pl.col("label")).otherwise(0.0).alias("_y"))
         .filter(pl.col("_y").is_not_null())
         .with_columns((pl.col("p_none") * pl.col("_pts")).alias("pred_base"),
                       (pl.col("p_isotonic_expanding") * pl.col("_pts")).alias("pred_cal"),
                       (pl.col("p_none") * pl.col("cond")).alias("pred_studybase"),
                       (pl.col("p_isotonic_expanding") * pl.col("cond")).alias("pred_studycal")))
    dd = d.drop("label").rename({"_y": "label"})
    y = dd["label"].to_numpy()
    out = {"n": dd.height, "played_share": round(float(dd["played"].mean()), 4), "weeks": dd.select("key").n_unique(),
           "conditional_points_from_full_model": int(dd["pred_full"].is_not_null().sum()), "oos_cache": cache[-1].name,
           "unconditional": {m: {"mae": round(float(np.abs(dd[f"pred_{m}"].to_numpy() - y).mean()), 4),
                                 "rmse": round(float(np.sqrt(((dd[f"pred_{m}"].to_numpy() - y) ** 2).mean())), 4),
                                 "bias": round(float((y - dd[f"pred_{m}"].to_numpy()).mean()), 4)}
                             for m in ("base", "cal", "studybase", "studycal")},
           "cal_vs_base": _compare(dd, "cal", "base", SEED),
           "study_points_cal_vs_base": _compare(dd, "studycal", "studybase", SEED)}
    pp, yy = dd["p_isotonic_expanding"].to_numpy(), dd["played"].cast(pl.Float64).to_numpy()
    pb = dd["p_none"].to_numpy()
    lg = lambda p: float(-(yy * np.log(np.clip(p, 1e-4, 1)) + (1 - yy) * np.log(np.clip(1 - p, 1e-4, 1))).mean())  # noqa: E731
    out["brier"] = {"base": round(float(((pb - yy) ** 2).mean()), 5), "cal": round(float(((pp - yy) ** 2).mean()), 5)}
    out["log_loss"] = {"base": round(lg(pb), 5), "cal": round(lg(pp), 5)}
    ci = out["cal_vs_base"]["ci95"]
    out["clearly_worse"] = bool(ci and ci[0] > 0)
    rep = json.loads((_out(args) / "report.json").read_text()) if (_out(args) / "report.json").exists() else {}
    rep["participation"] = out
    (_out(args) / "report.json").write_text(json.dumps(rep, indent=1, default=str))
    _ = pcal_cli  # the study's CLI builds the cache; imported so a missing module fails here, not mid-run
    return out


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["build", "run", "participation"])
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--reuse", action="store_true", help="reuse the cached store whatever its fingerprint (iteration)")
    ap.add_argument("--development", action="store_true", help="also report 2021-22 (not used by the guard)")
    args = ap.parse_args(argv)
    res = {"build": cmd_build, "run": cmd_run, "participation": cmd_participation}[args.command](args)
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
