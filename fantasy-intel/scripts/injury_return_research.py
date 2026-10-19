"""Injury return vs snap_trend: ifp-2.2.0 against a narrowly scoped fix for players returning from an injury exit.

    cd backend
    uv run python ../scripts/ifp22_backtest.py build                   # the ifp-2.2.0 store (cached, reused here)
    uv run python ../scripts/injury_return_research.py horizons        # horizon 1-3 rows, built as production builds them
    uv run python ../scripts/injury_return_research.py run             # walk-forward, cohort + all rows, the rule
    uv run python ../scripts/injury_return_research.py example --gsis 00-0034844 --season 2026 --week 7

Question (2026-10-19). ifp-2.2.0 feeds the raw last-game `snap_trend` to its ridge even when that game was an injury
exit. A lead back who got hurt early in a game (Saquon Barkley, 2026 week 4) carries a large negative snap_trend, and
low recency usage averages, into every week until he plays again.

Cohort (fixed before the run): RB/WR/TE rows whose last appearance before the cutoff week W is an injury exit
(`injury_exit_last = 1`) AND whose last team played at least one game strictly between that appearance and W (he is
returning from an absence, not playing through it).

Variants (fixed before the run); every change touches cohort rows only, in training and in prediction alike:
  base  ifp-2.2.0 exactly as shipped (context_model.SHIPPED)
  T     snap_trend := the position's mean snap_trend over non-cohort rows of 2020-2022 (the training mean; the
        missing-indicator path of snap_trend_role is not used, it would merge the cohort with <2-game players)
  TU    T + the usage averages (USAGE) recomputed with the injury-exit game removed from the player's games, and
        opp_trend (the same last-game artifact for touches) := its non-cohort mean.  <- the candidate
  TUP   TU + the production averages (PRODUCTION) recomputed the same way (reported only)

Horizons: 0 = the store row (target week = cutoff week). h = 1..3: features as of cutoff W for a player who appears in
W + h, built exactly as independent_projection_store.project builds a rest-of-season row (features_for_week at W with
the target's opponent; handoff for target W + h at cutoff W with the walk-forward return-rate table; context with W's
report dropped; role and depth at W). The model is fitted on horizon-0 rows strictly before W, as production trains.

Rule (fixed before the run): TU ships iff, on the cohort (RB/WR/TE pooled, 2023-2025), MAE(TU) - MAE(base) at horizon
0 has a week-clustered 95% CI entirely below 0, its pooled horizon 1-3 difference is <= 0, and no position's all-row
horizon-0 difference has a CI entirely above 0. Otherwise it is dropped (no-gain candidates are not shipped).

Outputs: <ifp-dir>/research/injury_return/ (git-ignored): horizons.parquet, preds.parquet, report.json.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import challenger_research as cr  # noqa: E402
import ifp22_backtest as b22  # noqa: E402

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.analytics import challenger as ch  # noqa: E402
from app.analytics import context_model as cm  # noqa: E402
from app.analytics import injury_model as im  # noqa: E402
from app.analytics.forecast_backtest import week_bootstrap_mae_diff  # noqa: E402
from app.analytics.forecast_features import Sources, features_for_week, key, played_targets  # noqa: E402
from app.config import get_settings  # noqa: E402

SEED = 20261019
RESAMPLES = 2000
POS = ("RB", "WR", "TE")
HORIZONS = (1, 2, 3)
EVAL_FROM, EVAL_TO = key(2023, 1), key(2025, 18)     # primary window; 2026 weeks are reported separately
MEAN_WINDOW = (2020, 2022)                           # non-cohort rows the replacement means come from
USAGE = ("snap_pct", "carries_pg", "targets_pg", "xfp_recent", "rush_share", "target_share", "rz_opps_pg",
         "gl_carries_pg", "air_yards_pg")
PRODUCTION = ("pts_recent", "pts_l3", "pts_minus_xfp")
TRENDS = {"T": ("snap_trend",), "TU": ("snap_trend", "opp_trend"), "TUP": ("snap_trend", "opp_trend")}
RECOMPUTED = {"T": (), "TU": USAGE, "TUP": USAGE + PRODUCTION}
VARIANTS = ("base", "T", "TU", "TUP")
CANDIDATE = "TU"


def _out(args) -> Path:
    p = args.ifp_dir / "research" / "injury_return"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sources(args) -> Sources:
    import hgb_research as hr

    src, _ = hr._sources(args)
    return src


# ----------------------------------------------------------------------------- cohort and recomputed usage


def with_cohort(rows: pl.DataFrame, games: pl.DataFrame, cutoff: str = "key") -> pl.DataFrame:
    """`games_missed`: games of the player's last team strictly between his last appearance and the cutoff week;
    `ret_cohort`: injury_exit_last = 1 and games_missed >= 1."""
    g = games.select(pl.col("team").alias("last_team"), (pl.col("season") * 100 + pl.col("week")).alias("_gk")).unique()
    r = rows.select("gsis_id", cutoff, "last_team", (pl.col("last_season") * 100 + pl.col("last_week")).alias("_lk"))
    miss = (r.drop_nulls(["last_team", "_lk"]).join(g, on="last_team")
            .filter((pl.col("_gk") > pl.col("_lk")) & (pl.col("_gk") < pl.col(cutoff)))
            .group_by("gsis_id", cutoff).agg(pl.len().cast(pl.Int64).alias("games_missed")))
    out = rows.drop([c for c in ("games_missed", "ret_cohort") if c in rows.columns]).join(
        miss, on=["gsis_id", cutoff], how="left")
    return out.with_columns(pl.col("games_missed").fill_null(0)).with_columns(
        ((pl.col("injury_exit_last") == 1) & (pl.col("games_missed") >= 1) & pl.col("position").is_in(POS))
        .fill_null(False).alias("ret_cohort"))


def without_exit_game(src: Sources, rows: pl.DataFrame, season: int, week: int) -> pl.DataFrame:
    """(gsis_id, <col>_nx) for the cohort rows of one cutoff week: features_for_week with the player's last game (the
    injury exit) removed from his own games, nothing else changed."""
    coh = rows.filter(pl.col("ret_cohort"))
    cols = USAGE + PRODUCTION
    if coh.height == 0:
        return pl.DataFrame(schema={"gsis_id": pl.String, **{f"{c}_nx": pl.Float64 for c in cols}})
    p = src.panel
    drop = coh.select("gsis_id", pl.col("last_season").cast(p["season"].dtype).alias("season"),
                      pl.col("last_week").cast(p["week"].dtype).alias("week"))
    s2 = Sources(p.join(drop, on=["gsis_id", "season", "week"], how="anti"), src.games, src.defense, src.rosters,
                 src.injuries, src.exits)
    tg = coh.select("gsis_id", "position", "team", "opponent", pl.col("is_home").cast(pl.Boolean))
    f = features_for_week(s2, season, week, tg)
    return f.select("gsis_id", *[pl.col(c).cast(pl.Float64).alias(f"{c}_nx") for c in cols])


def add_recomputed(src: Sources, rows: pl.DataFrame, cutoff: str = "key") -> pl.DataFrame:
    parts = []
    for (k,), g in rows.filter(pl.col("ret_cohort")).group_by(cutoff, maintain_order=True):
        parts.append(without_exit_game(src, g, int(k) // 100, int(k) % 100).with_columns(
            pl.lit(int(k)).cast(rows[cutoff].dtype).alias(cutoff)))
    cols = [f"{c}_nx" for c in USAGE + PRODUCTION]
    rows = rows.drop([c for c in cols if c in rows.columns])
    if not parts:
        return rows.with_columns(*[pl.lit(None, dtype=pl.Float64).alias(c) for c in cols])
    return rows.join(pl.concat(parts), on=["gsis_id", cutoff], how="left")


def replacement_means(store: pl.DataFrame) -> dict[str, dict[str, float]]:
    """{position: {trend column: mean over non-cohort RB/WR/TE store rows of MEAN_WINDOW}} (before the evaluation)."""
    base = store.filter(pl.col("season").is_between(*MEAN_WINDOW) & ~pl.col("ret_cohort") & pl.col("label").is_not_null())
    return {p: {c: float(base.filter(pl.col("position") == p)[c].mean()) for c in ("snap_trend", "opp_trend")}
            for p in POS}


def apply_variant(rows: pl.DataFrame, variant: str, means: dict) -> pl.DataFrame:
    if variant == "base":
        return rows
    c = pl.col("ret_cohort")
    ex = [pl.when(c).then(pl.col("position").replace_strict({p: means[p][t] for p in POS}, default=None,
                                                             return_dtype=pl.Float64))
          .otherwise(pl.col(t)).alias(t) for t in TRENDS[variant]]
    ex += [pl.when(c & pl.col(f"{f}_nx").is_not_null()).then(pl.col(f"{f}_nx")).otherwise(pl.col(f)).alias(f)
           for f in RECOMPUTED[variant]]
    return rows.with_columns(ex)


# ----------------------------------------------------------------------------- horizon 1-3 rows


def horizon_rows(src: Sources, inputs, codes, season: int, fw: int, targets_by_h: dict[int, pl.DataFrame],
                 games_kick: pl.DataFrame) -> pl.DataFrame:
    """Rows of `targets_by_h` (h -> gsis_id, position, team, opponent, is_home[, label]) with features as of cutoff
    fw, exactly as independent_projection_store.project builds a rest-of-season row of ifp-2.2.0."""
    rates = {(c["position"], c["missed"], int(c["h"])): {"rate": float(c["rate"]), "n": int(c["n"])}
             for c in im.fit_return_table(src, season, fw)}
    free = cm._week_report_free(src, season, fw)
    out = []
    for h, tg in targets_by_h.items():
        if tg.height == 0:
            continue
        t = season * 100 + fw + h
        feats = features_for_week(src, season, fw, tg.select("gsis_id", "position", "team", "opponent", "is_home"))
        ho = im.handoff(src, season, fw + h, fw, return_rates=rates, roster_codes=codes)
        feats = feats.join(ho.select("gsis_id", *im.HANDOFF_FEATURES), on="gsis_id", how="left")
        t4 = feats.select("gsis_id", "position", "team", "opponent")
        feats = feats.join(cm.context_columns(free, inputs, season, fw, t4, "none"), on="gsis_id", how="left")
        feats = feats.join(cm.role_columns(free, season, fw, t4), on="gsis_id", how="left")
        dt = t4.select(pl.lit(season, dtype=pl.Int64).alias("season"), pl.lit(fw, dtype=pl.Int64).alias("week"),
                       "gsis_id", "position", "team")
        dc = cm.depth_columns(src, inputs, dt, games=games_kick)
        feats = feats.join(dc.select("gsis_id", cm.DEPTH_FEATURE), on="gsis_id", how="left")
        lab = tg.select("gsis_id", "label") if "label" in tg.columns else tg.select("gsis_id").with_columns(
            pl.lit(None, dtype=pl.Float64).alias("label"))
        out.append(cm._round(feats).join(lab, on="gsis_id", how="left").with_columns(
            pl.lit(key(season, fw), dtype=pl.Int64).alias("key"), pl.lit(t, dtype=pl.Int64).alias("tkey"),
            pl.lit(h, dtype=pl.Int64).alias("h")))
    return pl.concat(out, how="diagonal_relaxed") if out else pl.DataFrame()


def cmd_horizons(args) -> dict:
    t0 = time.time()
    store, _, _ = b22.build(SimpleNamespace(**{**vars(args), "reuse": True}))
    src = _sources(args)
    import weekly_learning as wlcli
    from app.db.connection import connect as db_connect

    live = db_connect(args.live_db, read_only=True)
    hist = db_connect(args.history_db, read_only=True) if args.history_db.exists() else None
    try:
        inputs = cm.load_inputs([hist, live], args.exits_dir / wlcli.DEPTH_DIR, args.season)
        codes = wlcli.roster_codes(live, hist)
    finally:
        for c in (live, hist):
            if c is not None:
                c.close()
    games_kick = cm._games(src)
    weeks = sorted({(int(s), int(w)) for s, w in store.select("season", "week").unique().iter_rows()})
    have = set(weeks)
    out = []
    for s, fw in weeks:
        if key(s, fw) < EVAL_FROM:
            continue
        by_h = {}
        for h in HORIZONS:
            if (s, fw + h) not in have:
                continue
            tg = played_targets(src, s, fw + h).filter(pl.col("position").is_in(POS))
            by_h[h] = tg.rename({"league_points": "label"})
        if by_h:
            out.append(horizon_rows(src, inputs, codes, s, fw, by_h, games_kick))
        print(f"  {s} w{fw:02d} {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
    hz = pl.concat(out, how="diagonal_relaxed")
    hz.write_parquet(_out(args) / "horizons.parquet")
    return {"rows": hz.height, "seconds": round(time.time() - t0, 1)}


# ----------------------------------------------------------------------------- walk-forward


def prepare(args):
    store, cands, _ = b22.build(SimpleNamespace(**{**vars(args), "reuse": True}))
    src = _sources(args)
    store = add_recomputed(src, with_cohort(store, src.games))
    hp = _out(args) / "horizons.parquet"
    hz = pl.read_parquet(hp) if hp.exists() else None
    if hz is not None:
        hz = add_recomputed(src, with_cohort(hz, src.games))
    return src, store, cands, hz


def walk_forward(store: pl.DataFrame, hz: pl.DataFrame | None, cfg, means: dict) -> pl.DataFrame:
    stores = {v: apply_variant(store, v, means) for v in VARIANTS}
    hzs = {v: apply_variant(hz, v, means) for v in VARIANTS} if hz is not None else None
    keys = sorted(k for k in store["key"].unique().to_list() if k >= EVAL_FROM)
    out = []
    for k in keys:
        s, w = k // 100, k % 100
        for pos in POS:
            comps = cm.SHIPPED[pos]
            frames = {"h0": stores["base"].filter((pl.col("key") == k) & (pl.col("position") == pos))
                      .with_columns(pl.col("key").cast(pl.Int64).alias("tkey"), pl.lit(0, dtype=pl.Int64).alias("h"))}
            if hzs is not None:
                frames["hz"] = hzs["base"].filter((pl.col("key") == k) & (pl.col("position") == pos))
            cols = {n: {} for n in frames}
            for v in VARIANTS:
                models, _, fb = cm.fit(stores[v], s, w, cfg, {pos: comps}, positions=(pos,))
                for n in frames:
                    src = (stores[v] if n == "h0" else hzs[v]).filter((pl.col("key") == k) & (pl.col("position") == pos))
                    if src.height == 0:
                        continue
                    assert src["gsis_id"].to_list() == frames[n]["gsis_id"].to_list()
                    p = cm.predict_points(models, fb, src, pos, comps)
                    ok = src["games_recent"].fill_null(0).to_numpy() > 0
                    cols[n][f"pred_{v}"] = np.where(ok, p, np.nan)
            for n, f in frames.items():
                if f.height == 0 or not cols[n]:
                    continue
                out.append(f.select(pl.col("key").cast(pl.Int64), "tkey", "h", "season", "gsis_id", "position",
                                    "label", "ret_cohort", "games_missed", "snap_trend", "injury_exit_last")
                           .with_columns([pl.Series(c, v).fill_nan(None) for c, v in cols[n].items()]))
        print(f"  {s} w{w:02d}", file=sys.stderr, flush=True)
    return pl.concat(out, how="diagonal_relaxed")


def _cmp(d: pl.DataFrame, a: str, b: str) -> dict:
    r = week_bootstrap_mae_diff(d, f"pred_{a}", f"pred_{b}", RESAMPLES, SEED, exact=True)
    return {"diff": None if r["diff"] is None else round(r["diff"], 4),
            "ci95": None if r["ci95"] is None else [round(x, 4) for x in r["ci95"]]}


def summary(d: pl.DataFrame) -> dict:
    d = d.filter(pl.col("label").is_not_null() & pl.all_horizontal([pl.col(f"pred_{v}").is_not_null() for v in VARIANTS]))
    y = d["label"].to_numpy()
    out = {"n": d.height, "weeks": d.select("key").n_unique(), "models": {}}
    for v in VARIANTS:
        e = d[f"pred_{v}"].to_numpy() - y
        out["models"][v] = {"mae": round(float(np.abs(e).mean()), 4) if d.height else None,
                            "mean_signed_error": round(float(e.mean()), 4) if d.height else None,
                            "mean_pred": round(float(d[f"pred_{v}"].mean()), 4) if d.height else None}
    out["mean_label"] = round(float(y.mean()), 4) if d.height else None
    out["vs_base"] = {v: _cmp(d, v, "base") for v in VARIANTS if v != "base"} if d.height else {}
    return out


def report(p: pl.DataFrame, lo: int, hi: int) -> dict:
    w = p.filter(pl.col("tkey").is_between(lo, hi))
    sets = {"h0": w.filter(pl.col("h") == 0), "h1-3": w.filter(pl.col("h") >= 1),
            **{f"h{h}": w.filter(pl.col("h") == h) for h in HORIZONS}}
    out = {}
    for name, d in sets.items():
        out[name] = {"cohort": {"ALL": summary(d.filter(pl.col("ret_cohort"))),
                                **{pos: summary(d.filter(pl.col("ret_cohort") & (pl.col("position") == pos))) for pos in POS}},
                     "all_rows": {"ALL": summary(d), **{pos: summary(d.filter(pl.col("position") == pos)) for pos in POS}},
                     "injury_exit_not_returning": summary(d.filter((pl.col("injury_exit_last") == 1) & ~pl.col("ret_cohort")))}
    return out


def decide(rep: dict) -> dict:
    c0 = rep["h0"]["cohort"]["ALL"]["vs_base"].get(CANDIDATE, {})
    c13 = rep["h1-3"]["cohort"]["ALL"]["vs_base"].get(CANDIDATE, {})
    worse = [pos for pos in POS if (rep["h0"]["all_rows"][pos]["vs_base"].get(CANDIDATE, {}).get("ci95") or [0])[0] > 0]
    gain0 = bool(c0.get("ci95") and c0["ci95"][1] < 0)
    gain13 = c13.get("diff") is not None and c13["diff"] <= 0
    return {"cohort_h0": c0, "cohort_h1_3": c13, "positions_clearly_worse_h0": worse,
            "ships": gain0 and gain13 and not worse,
            "why": ("cohort h0 CI entirely below 0" if gain0 else "cohort h0 CI not entirely below 0") + "; "
                   + ("h1-3 point <= 0" if gain13 else "h1-3 point > 0") + "; "
                   + (f"clearly worse at {worse}" if worse else "no position clearly worse")}


def cmd_run(args) -> dict:
    t0 = time.time()
    _, store, _, hz = prepare(args)
    if hz is None:
        raise SystemExit("run `injury_return_research.py horizons` first")
    cfg = ch.ChallengerConfig.load()
    means = replacement_means(store)
    preds = walk_forward(store, hz, cfg, means)
    preds.write_parquet(_out(args) / "preds.parquet")
    rep = report(preds, EVAL_FROM, EVAL_TO)
    out = {"schema": "injury-return-research/v1", "created_at": datetime.now(timezone.utc).isoformat(),
           "cohort": "injury_exit_last = 1 and the last team played >= 1 game between the exit and the cutoff",
           "variants": {"T": TRENDS["T"], "TU": {"trends": TRENDS["TU"], "recomputed": RECOMPUTED["TU"]},
                        "TUP": {"trends": TRENDS["TUP"], "recomputed": RECOMPUTED["TUP"]}},
           "replacement_means": means, "window": [EVAL_FROM, EVAL_TO], "report": rep, "decision": decide(rep),
           "exploratory_2026": report(preds, key(2026, 1), key(2026, 18)),
           "cohort_store_rows": int(store.filter(pl.col("ret_cohort")).height),
           "runtime_seconds": round(time.time() - t0, 1)}
    (_out(args) / "report.json").write_text(json.dumps(out, indent=1, default=str))
    return {"decision": out["decision"],
            "cohort_h0": {v: rep["h0"]["cohort"]["ALL"]["models"][v] for v in VARIANTS} | {"n": rep["h0"]["cohort"]["ALL"]["n"]},
            "runtime_seconds": out["runtime_seconds"]}


def cmd_example(args) -> dict:
    """One player's conditional points from cutoff (season, week) for the next weeks, base vs every variant (models
    fitted on store rows before the cutoff)."""
    src, store, _, _ = prepare(args)
    import weekly_learning as wlcli
    from app.db.connection import connect as db_connect

    live = db_connect(args.live_db, read_only=True)
    hist = db_connect(args.history_db, read_only=True) if args.history_db.exists() else None
    try:
        inputs = cm.load_inputs([hist, live], args.exits_dir / wlcli.DEPTH_DIR, args.season)
        codes = wlcli.roster_codes(live, hist)
    finally:
        for c in (live, hist):
            if c is not None:
                c.close()
    s, fw = args.season, args.week
    last = src.panel.filter((pl.col("gsis_id") == args.gsis) & (pl.col("season") * 100 + pl.col("week") < key(s, fw))
                            ).sort("season", "week").tail(1)
    team, pos = last["team"][0], last["position"][0]
    g = src.games.filter((pl.col("season") == s) & (pl.col("team") == team) & (pl.col("week") >= fw)).sort("week")
    by_h = {}
    for r in g.head(args.weeks).iter_rows(named=True):
        by_h[r["week"] - fw] = pl.DataFrame({"gsis_id": [args.gsis], "position": [pos], "team": [team],
                                             "opponent": [r["opponent"]], "is_home": [bool(r["is_home"])]})
    rows = horizon_rows(src, inputs, codes, s, fw, by_h, cm._games(src))
    rows = add_recomputed(src, with_cohort(rows, src.games))
    cfg = ch.ChallengerConfig.load()
    means = replacement_means(store)
    out = {"gsis_id": args.gsis, "team": team, "position": pos, "cutoff": [s, fw],
           "ret_cohort": bool(rows["ret_cohort"][0]), "games_missed": int(rows["games_missed"][0]),
           "snap_trend": rows["snap_trend"][0], "weeks": {}}
    for v in VARIANTS:
        models, _, fb = cm.fit(apply_variant(store, v, means), s, fw, cfg, {pos: cm.SHIPPED[pos]}, positions=(pos,))
        rv = apply_variant(rows, v, means)
        pts = cm.predict_points(models, fb, rv, pos, cm.SHIPPED[pos])
        m = models[pos]
        contrib = dict(zip(m.columns, m.contributions(rv.head(1))[0]))
        out[f"top_{v}"] = {c: round(float(x), 2) for c, x in sorted(contrib.items(), key=lambda kv: abs(kv[1]))[-8:]}
        for i, h in enumerate(rv["h"].to_list()):
            out["weeks"].setdefault(int(fw + h), {})[v] = round(float(pts[i]), 2)
    return out


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["horizons", "run", "example"])
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--week", type=int, default=None, help="example: the cutoff week")
    ap.add_argument("--weeks", type=int, default=4, help="example: how many team games from the cutoff")
    ap.add_argument("--gsis", default="00-0034844")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args(argv)
    res = {"horizons": cmd_horizons, "run": cmd_run, "example": cmd_example}[args.command](args)
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
