"""D/ST projection research (dfp-r1): fetch ESPN D/ST history, reconcile league scoring, run the backtest.

    cd backend
    uv run python ../scripts/dst_research.py fetch [--refresh]          # ESPN D/ST weekly stats + projections
    uv run python ../scripts/dst_research.py reconcile                  # league D/ST scoring vs ESPN's points
    uv run python ../scripts/dst_research.py evaluate                   # selection, test, prospective report

Research only: no command writes either database (both are opened read-only) or changes any projection.
`fetch` reads ESPN's public read API (league-default scoring; the raw statistics are rescored under the
league's own rules) and caches one JSON per season under data/raw/espn_dst (git-ignored); `evaluate` writes
<out>/dst_evaluation.json and <out>/dst_predictions.csv (<out> defaults to data/models/dst, git-ignored).
Results: docs/research/dst_projection_model.md.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401

import numpy as np

from app.analytics import dst_research as dr
from app.analytics.kicker_scoring import league_items
from app.config import REPO_ROOT, get_settings
from app.db.connection import connect, fetch_dicts, table_exists

FIRST_SEASON = 2019            # burn-in: history only (ratings need earlier games)
FIRST_ROW_SEASON = 2020        # first season whose rows feed the stack (fitted on earlier seasons only)
VALIDATION = (2021, 2022)      # every choice below is made here
TEST = (2023, 2024, 2025)      # reported once with the frozen choices
HORIZONS = (1, 2, 3, 4)
GRID = [dict(half_life=h, offseason_gap=g, k=k) for h in (4.0, 8.0, 16.0) for g in (6.0, 18.0) for k in (3.0, 6.0, 12.0)]
BLEND_WEIGHTS = (0.0, 0.25, 0.5, 0.75, 1.0)
STREAM_POOL_EXCLUDE = 8        # streaming pool: each week's D/STs outside ESPN's top 8 projections
ESPN_URL = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leaguedefaults/3"
            "?view=kona_player_info")
ESPN_FILTER = {"players": {"filterSlotIds": {"value": [16]}, "filterStatsForSourceIds": {"value": [0, 1]},
                           "filterStatsForSplitTypeIds": {"value": [1]},
                           "sortPercOwned": {"sortPriority": 1, "sortAsc": False}, "limit": 50}}


def _path(env: str, default: str) -> Path:
    raw = Path(os.getenv(env) or default)
    return raw if raw.is_absolute() else REPO_ROOT / raw


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (float, np.floating)):
        o = float(o)
        return None if (o != o or o in (float("inf"), float("-inf"))) else o
    if isinstance(o, np.integer):
        return int(o)
    return o


# ----------------------------------------------------------------------------- data


def cmd_fetch(args) -> dict:
    import requests

    args.cache.mkdir(parents=True, exist_ok=True)
    out = {}
    for season in range(FIRST_SEASON, args.season + 1):
        path = args.cache / f"espn_dst_{season}.json"
        if path.exists() and not args.refresh and season != args.season:
            out[season] = "cached"
            continue
        r = requests.get(ESPN_URL.format(season=season), headers={"x-fantasy-filter": json.dumps(ESPN_FILTER)},
                         timeout=60)
        r.raise_for_status()
        payload = r.json()
        payload["_fetched_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(payload))
        parsed = dr.parse_espn_dst(payload, season)
        out[season] = {k: len(v) for k, v in parsed.items()}
    return out


def load_espn(cache: Path, seasons) -> dict:
    out = {}
    for s in seasons:
        path = cache / f"espn_dst_{s}.json"
        if not path.exists():
            raise SystemExit(f"{path} missing: run `fetch` first")
        out[s] = dr.parse_espn_dst(json.loads(path.read_text()), s)
    return out


def _cons(args):
    live = connect(args.live_db, read_only=True)
    hist = connect(args.history_db, read_only=True) if args.history_db.exists() else None
    return live, hist


def load_rows(args, scoring):
    live, hist = _cons(args)
    sched, tw = [], []
    for con in (c for c in (hist, live) if c):
        sched += fetch_dicts(con, """SELECT season, week, game_type, home_team, away_team, home_score, away_score,
                                     spread_line, total_line, location FROM nfl_schedules
                                     WHERE season BETWEEN ? AND ?""", [FIRST_SEASON, args.season])
        if table_exists(con, "nfl_team_week"):
            tw += fetch_dicts(con, "SELECT season, week, team, dropbacks, hit_or_sack FROM nfl_team_week")
    seen, uniq = set(), []
    for g in sched:   # the live DB repeats nothing from history, but never double-count a game
        k = (g["season"], g["week"], g["home_team"], g["away_team"])
        if k not in seen:
            seen.add(k)
            uniq.append(g)
    espn = load_espn(args.cache, range(FIRST_SEASON, args.season + 1))
    rows = dr.build_games(uniq, tw, espn, scoring)
    return live, hist, rows


# ----------------------------------------------------------------------------- reconcile


def cmd_reconcile(args) -> dict:
    live, _ = _cons(args)
    items = league_items(live, args.season)
    scoring = dr.resolve_dst_scoring(items)
    espn = load_espn(args.cache, [args.season])[args.season]
    by_team = {r["espn_player_id"]: dr.norm_team(r["pro_team"]) for r in fetch_dicts(
        live, "SELECT espn_player_id, pro_team FROM fantasy_players WHERE position = 'D/ST'")}
    stored = fetch_dicts(live, """SELECT e.espn_player_id, e.scoring_period AS week, e.actual_points,
                                  e.projected_points FROM espn_player_weekly_points e
                                  JOIN fantasy_players f USING (espn_player_id)
                                  WHERE f.position = 'D/ST' AND e.season = ?""", [args.season])
    act = {"n": 0, "exact": 0, "mismatches": []}
    proj = {"n": 0, "exact": 0, "max_abs_diff": 0.0}
    for r in stored:
        team = by_team.get(r["espn_player_id"])
        a = espn["actual"].get((team, r["week"]))
        if r["actual_points"] is not None and a is not None:
            got = scoring.points(a)
            act["n"] += 1
            if abs(got - r["actual_points"]) < 0.05:
                act["exact"] += 1
            else:
                act["mismatches"].append({"team": team, "week": r["week"], "espn": r["actual_points"],
                                          "computed": got})
        p = espn["espn"].get((team, r["week"]))
        if r["projected_points"] is not None and p is not None:
            got = scoring.points(p)
            proj["n"] += 1
            d = abs(got - r["projected_points"])
            proj["exact"] += d < 0.05
            proj["max_abs_diff"] = max(proj["max_abs_diff"], round(d, 4))
    # the pregame snapshots (espn_projection_history) are the league-scored projections the app stores
    snaps = fetch_dicts(live, """SELECT h.espn_player_id, h.scoring_period AS week, h.projected_points, h.synced_at
                                 FROM espn_projection_history h JOIN fantasy_players f USING (espn_player_id)
                                 WHERE f.position = 'D/ST' AND h.season = ? AND h.snapshot_date =
                                 (SELECT max(snapshot_date) FROM espn_projection_history WHERE season = ?)""",
                        [args.season, args.season])
    raw = _raw_capture_projections(args, args.season)
    snap = {"n": 0, "exact": 0, "max_abs_diff": 0.0, "raw_capture_dir": raw.get("_dir")}
    for r in snaps:
        p = raw.get((by_team.get(r["espn_player_id"]), r["week"]))
        if p is None or r["projected_points"] is None:
            continue
        d = abs(scoring.points(p) - r["projected_points"])
        snap["n"] += 1
        snap["exact"] += d < 0.05
        snap["max_abs_diff"] = max(snap["max_abs_diff"], round(d, 4))
    # tier definitions and points allowed vs the scoreboard, all seasons
    _, _, rows = load_rows(args, scoring)
    fin = [r for r in rows if r["final"]]
    tiers_ok = sum(r["pa_tier"] == r["espn_pa_tier"] and r["ya_tier"] == r["espn_ya_tier"] for r in fin)
    return {"scoring": scoring.describe(), "actual_points_vs_espn": {**act, "mismatches": act["mismatches"][:20]},
            "stored_projection_vs_rescored_espn_projection": proj,
            "latest_raw_capture_projection_vs_snapshot": snap,
            "tier_definitions": {"rows": len(fin), "match": tiers_ok}}


def _raw_capture_projections(args, season: int) -> dict:
    """ESPN projected D/ST statistics from the most recent raw ESPN capture (data/raw/espn/*/projections_*.json)."""
    root = args.raw_espn or args.cache.parent / "espn"
    dirs = sorted(d for d in root.glob("espn-*") if d.is_dir()) if root.exists() else []
    out: dict = {}
    for d in reversed(dirs):
        files = sorted(d.glob("projections_*.json"))
        if not files:
            continue
        for f in files:
            parsed = dr.parse_espn_dst(json.loads(f.read_text()), season)
            for k, v in parsed["espn"].items():
                out.setdefault(k, v)
        out["_dir"] = d.name
        break
    return out


# ----------------------------------------------------------------------------- evaluate


def features_by_horizon(rows, hist, teams, cfg, horizons, cache=None):
    cache = {} if cache is None else cache
    return {h: dr.rating_features(rows, hist, teams, cfg, h, cache) for h in horizons}


def walk_forward(rows, feats, scoring, cfg, seasons) -> list[dict]:
    """Project each season with a stack fitted on FIRST_ROW_SEASON..season-1; returns merged row dicts."""
    out = []
    for s in seasons:
        tr = [i for i, r in enumerate(rows) if FIRST_ROW_SEASON <= r["season"] < s]
        te = [i for i, r in enumerate(rows) if r["season"] == s]
        if not tr or not te:
            continue
        preds = dr.project_season([rows[i] for i in tr], [feats[i] for i in tr], [rows[i] for i in te],
                                  [feats[i] for i in te], scoring, cfg)
        for i, p in zip(te, preds):
            out.append({**rows[i], **{k: v for k, v in feats[i].items() if not k.startswith("tau_")}, **p})
    return out


def _mae(rows, col):
    v = [abs(r["points"] - r[col]) for r in rows if r["final"] and r.get(col) is not None]
    return float(np.mean(v)) if v else float("nan")


def select(rows, hist, teams, scoring) -> dict:
    grid = []
    for g in GRID:
        cfg = dr.DConfig(**g)
        f = dr.rating_features(rows, hist, teams, cfg, 1)
        p = walk_forward(rows, f, scoring, cfg, VALIDATION)
        grid.append({**g, "mae": round(_mae(p, "pred"), 4)})
    grid.sort(key=lambda r: r["mae"])
    best = {k: grid[0][k] for k in ("half_life", "offseason_gap", "k")}
    return {"grid": grid, "best": best}


def _with(rows, name, values):
    for r, v in zip(rows, values):
        r[name] = v


def cmd_evaluate(args) -> dict:
    live, hist_con = _cons(args)
    items = league_items(live, args.season)
    scoring = dr.resolve_dst_scoring(items)
    if not scoring.supported:
        raise SystemExit(f"league D/ST scoring not supported: {scoring.describe()}")
    live.close()
    _, _, rows = load_rows(args, scoring)
    rows = [r for r in rows if r["final"] or r["season"] == args.season]
    teams = sorted({r["team"] for r in rows} | {r["opp"] for r in rows})
    hist = dr.history_arrays(rows, teams)
    eval_rows = [r for r in rows if r["season"] >= FIRST_ROW_SEASON]
    rep: dict = {"model_version": dr.MODEL_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(),
                 "scoring": scoring.describe(),
                 "protocol": {"burn_in": FIRST_SEASON, "first_row_season": FIRST_ROW_SEASON,
                              "validation": list(VALIDATION), "test": list(TEST), "prospective": args.season,
                              "horizons": list(HORIZONS), "stream_pool_excludes_espn_top": STREAM_POOL_EXCLUDE},
                 "sample": {str(s): {"rows": sum(r["season"] == s for r in rows),
                                     "final": sum(r["season"] == s and r["final"] for r in rows),
                                     "espn_projected": sum(r["season"] == s and r.get("espn_points") is not None
                                                           for r in rows),
                                     "with_line": sum(r["season"] == s and r["implied_opp"] is not None for r in rows)}
                            for s in range(FIRST_SEASON, args.season + 1)}}

    # --- 1. selection on validation only
    sel = select(eval_rows, hist, teams, scoring)
    base = dr.DConfig(**sel["best"])
    variants = {"frozen (market, no pressure)": base, "ratings only (no market)": dr.replace_cfg(base, market=False),
                "+ pressure (QB hits + sacks)": dr.replace_cfg(base, pressure=True),
                "no rare-event extra shrinkage": dr.replace_cfg(base, rare_k_mult=1.0),
                "no opponent adjustment": dr.replace_cfg(base, opponent_adjust=False)}
    vres = {}
    for name, cfg in variants.items():
        f = dr.rating_features(eval_rows, hist, teams, cfg, 1)
        p = walk_forward(eval_rows, f, scoring, cfg, VALIDATION)
        ev = dr.evaluate([r for r in p if r["final"]], {"model": "pred"}, resamples=0)
        vres[name] = ev["methods"]["model"]
    rep["selection"] = {**sel, "variants_validation": vres}
    pressure_helps = vres["+ pressure (QB hits + sacks)"]["mae"] < vres["frozen (market, no pressure)"]["mae"]
    frozen = dr.replace_cfg(base, pressure=pressure_helps)
    rep["selection"]["frozen_config"] = frozen.to_dict()

    # --- 2. features at every horizon with the frozen config (market only at horizon 1)
    cache: dict = {}
    feats = features_by_horizon(eval_rows, hist, teams, frozen, HORIZONS, cache)
    seasons = list(VALIDATION) + list(TEST) + [args.season]
    preds = {("market", 1): walk_forward(eval_rows, feats[1], scoring, frozen, seasons)}
    ratings_cfg = dr.replace_cfg(frozen, market=False)
    for h in HORIZONS:
        preds[("ratings", h)] = walk_forward(eval_rows, feats[h], scoring, ratings_cfg, seasons)
    main = preds[("market", 1)]
    index = {(r["key"], r["team"]): r for r in main}
    for (kind, h), pr in preds.items():
        for r in pr:
            index[(r["key"], r["team"])][f"pred_{kind}_h{h}"] = r["pred"]

    # blend weight with ESPN, chosen on validation
    val = [r for r in main if r["season"] in VALIDATION and r["final"] and r.get("espn_points") is not None]
    blend = {str(w): round(float(np.mean([abs(r["points"] - (w * r["pred"] + (1 - w) * r["espn_points"]))
                                           for r in val])), 4) for w in BLEND_WEIGHTS}
    w_best = float(min(blend, key=blend.get))
    rep["selection"]["blend_validation_mae"] = blend
    rep["selection"]["blend_weight_on_model"] = w_best
    for r in main:
        r["pred_blend"] = (w_best * r["pred"] + (1 - w_best) * r["espn_points"]) if r.get("espn_points") is not None else None

    methods = {"model (market, h1)": "pred", "model (ratings only, h1)": "pred_ratings_h1",
               f"blend {w_best:g} model + {1 - w_best:g} ESPN": "pred_blend", "ESPN": "espn_points",
               "naive: team trailing D/ST pts": "naive_team", "naive: opponent allows D/ST pts": "opp_allowed"}

    # --- 3. intervals from validation residuals
    vr = [r for r in main if r["season"] in VALIDATION and r["final"]]
    bands = dr.residual_bands(np.array([r["pred"] for r in vr]), np.array([r["points"] for r in vr]))
    rep["intervals_from_validation"] = bands

    def block(rs, label):
        fin = [r for r in rs if r["final"]]
        out = {"label": label, "all_rows_model": dr.evaluate(fin, {"model": "pred"}, resamples=0),
               "common_support": dr.evaluate(fin, methods, baseline="ESPN")}
        sup = [r for r in fin if r.get("espn_points") is not None]
        # streaming pool: drop each week's top-N by ESPN projection (the D/STs a league has rostered)
        pool = []
        for k in sorted({r["key"] for r in sup}):
            wk = sorted((r for r in sup if r["key"] == k), key=lambda r: -r["espn_points"])
            pool += wk[STREAM_POOL_EXCLUDE:]
        out["streaming_pool"] = dr.evaluate(pool, methods, baseline="ESPN", top_k=(1, 3, 5))
        out["per_season"] = {str(s): {m: {"mae": v["mae"], "spearman": v["within_week_spearman"],
                                          "bias": v["bias_actual_minus_pred"]}
                                      for m, v in dr.evaluate([r for r in sup if r["season"] == s], methods,
                                                              resamples=0)["methods"].items()}
                             for s in sorted({r["season"] for r in sup})}
        hz = {f"model h{h} (ratings only)": f"pred_ratings_h{h}" for h in HORIZONS}
        hz["model h1 (market)"] = "pred"
        hz["ESPN (final weekly projection)"] = "espn_points"
        out["horizons"] = {m: {k: v[k] for k in ("mae", "bias_actual_minus_pred", "within_week_spearman",
                                                 "pairwise_order_accuracy")}
                           for m, v in dr.evaluate(sup, hz, resamples=0)["methods"].items()}
        out["horizons_n"] = len(sup)
        out["interval80_coverage"] = dr.coverage(bands, np.array([r["pred"] for r in fin]),
                                                 np.array([r["points"] for r in fin]))
        out["components"] = _components(sup)
        out["touchdowns"] = _touchdowns(sup)
        out["tiers"] = {"points_allowed": {"model": dr.tier_calibration([r["p_pa_tier"] for r in sup],
                                                                         [r["pa_tier"] for r in sup], dr.PA_TIERS),
                                           "ESPN": dr.tier_calibration([r["espn_p_pa_tier"] for r in sup],
                                                                       [r["pa_tier"] for r in sup], dr.PA_TIERS)},
                        "yards_allowed": {"model": dr.tier_calibration([r["p_ya_tier"] for r in sup],
                                                                        [r["ya_tier"] for r in sup], dr.YA_TIERS),
                                          "ESPN": dr.tier_calibration([r["espn_p_ya_tier"] for r in sup],
                                                                      [r["ya_tier"] for r in sup], dr.YA_TIERS)}}
        return out

    # --- ablations and line-timing sensitivity, reported on validation AND test side by side (nothing chosen here)
    abl = {"frozen": frozen, "ratings only (no market)": ratings_cfg,
           "market only (line, no ratings)": dr.replace_cfg(frozen, ratings=False),
           "+ pressure (QB hits + sacks)": dr.replace_cfg(frozen, pressure=True),
           "no opponent adjustment": dr.replace_cfg(frozen, opponent_adjust=False),
           "no team ratings (line + league means)": dr.replace_cfg(frozen, iterations=0, k=1e9)}
    rep["ablations"] = {}
    for name, cfg in abl.items():
        same = (cfg.iterations, cfg.k, cfg.opponent_adjust) == (frozen.iterations, frozen.k, frozen.opponent_adjust)
        f = feats[1] if same else dr.rating_features(eval_rows, hist, teams, cfg, 1)
        p = walk_forward(eval_rows, f, scoring, cfg, list(VALIDATION) + list(TEST))
        rep["ablations"][name] = {
            split: {k: v for k, v in dr.evaluate([r for r in p if r["final"] and r["season"] in ss], {"m": "pred"},
                                                 resamples=0)["methods"]["m"].items()
                    if k in ("mae", "bias_actual_minus_pred", "within_week_spearman", "pairwise_order_accuracy")}
            for split, ss in (("validation", VALIDATION), ("test", TEST))}
    rep["line_timing_sensitivity"] = line_sensitivity(eval_rows, feats[1], scoring, frozen)

    rep["validation"] = block([r for r in main if r["season"] in VALIDATION], "validation 2021-2022")
    rep["test"] = block([r for r in main if r["season"] in TEST], "test 2023-2025 (held out)")
    rep["prospective"] = block([r for r in main if r["season"] == args.season], f"prospective {args.season}")
    rep["decision"] = decide(rep)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "dst_evaluation.json"
    path.write_text(json.dumps(_clean(rep), indent=2, sort_keys=True))
    cols = ["season", "week", "team", "opp", "home", "implied_opp", "margin", "final", "points", "espn_points",
            "pred", "pred_blend", *[f"pred_ratings_h{h}" for h in HORIZONS], "naive_team", "opp_allowed",
            "lambda_td", "espn_lambda_td", "td_points", "e_sacks", "sacks", "espn_sacks", "e_ints", "ints",
            "espn_ints", "e_fr", "fr", "espn_fr", "e_pa", "pa", "espn_pa", "e_ya", "ya", "espn_ya"]
    with open(args.out / "dst_predictions.csv", "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(cols)
        for r in main:
            wr.writerow([_clean(r.get(c)) for c in cols])
    return {"report": str(path), "decision": rep["decision"],
            "test_common_support": {m: {k: v[k] for k in ("mae", "within_week_spearman", "bias_actual_minus_pred")}
                                    for m, v in rep["test"]["common_support"]["methods"].items()},
            "test_vs_espn": rep["test"]["common_support"].get("vs_ESPN")}


LINE_NOISE_SD = (0.0, 1.0, 2.0, 3.0)   # points of N(0, sd) noise on BOTH the spread and the total


def line_sensitivity(rows, feats, scoring, cfg, seeds=(1, 2, 3)) -> dict:
    """nflverse lines are closing lines; a Tuesday streaming decision sees earlier ones. Perturb the spread and
    the total of the projected (test) rows only, keep the stack trained on closing lines, and re-score: how much
    of the edge over ESPN survives a less informed line? Typical open-to-close moves are 1-2 points."""
    out = {}
    test_idx = [i for i, r in enumerate(rows) if r["season"] in TEST]
    for sd in LINE_NOISE_SD:
        maes, rhos = [], []
        for seed in (seeds if sd else seeds[:1]):
            rng = np.random.default_rng(seed)
            noisy = [dict(r) for r in rows]
            for i in test_idx:
                r = noisy[i]
                if r["implied_opp"] is None:
                    continue
                total = 2 * r["implied_opp"] + r["margin"] + rng.normal(0, sd)
                margin = r["margin"] + rng.normal(0, sd)
                r["margin"], r["implied_opp"] = margin, (total - margin) / 2
            p = [r for r in walk_forward(noisy, feats, scoring, cfg, TEST) if r["final"] and r.get("espn_points") is not None]
            ev = dr.evaluate(p, {"m": "pred", "ESPN": "espn_points"}, resamples=0)
            maes.append(ev["methods"]["m"]["mae"] - ev["methods"]["ESPN"]["mae"])
            rhos.append(ev["methods"]["m"]["within_week_spearman"] - ev["methods"]["ESPN"]["within_week_spearman"])
        out[str(sd)] = {"mae_minus_espn": round(float(np.mean(maes)), 4),
                        "spearman_minus_espn": round(float(np.mean(rhos)), 4), "seeds": len(maes)}
    return out


def _components(rows) -> dict:
    out = {}
    for c in ("sacks", "ints", "fr", "pa", "ya"):
        y = np.array([r[c] for r in rows])
        out[c] = {}
        for name, col in (("model", "e_" + c), ("ESPN", "espn_" + c)):
            p = np.array([r[col] for r in rows])
            out[c][name] = {"mae": round(float(np.mean(np.abs(y - p))), 4), "bias": round(float(np.mean(y - p)), 4),
                            "corr": round(float(np.corrcoef(p, y)[0, 1]), 4)}
    return out


def _touchdowns(rows) -> dict:
    y_any = np.array([r["any_td"] for r in rows])
    m_p = np.array([r["p_any_td"] for r in rows])
    e_p = 1 - np.exp(-np.array([r["espn_lambda_td"] for r in rows]))
    pts = np.array([r["points"] for r in rows])
    tdp = np.array([r["td_points"] for r in rows])
    no_td = pts - tdp

    def mae_no_td(col, tdcol):
        return round(float(np.mean(np.abs(no_td - (np.array([r[col] for r in rows])
                                                    - np.array([r[tdcol] for r in rows]))))), 4)

    return {"actual_td_rate": round(float(y_any.mean()), 4),
            "td_points_share_of_actual_variance": round(float(1 - np.var(no_td) / np.var(pts)), 4)
            if np.var(pts) else None,
            "model": dr.td_calibration(m_p, y_any), "ESPN": dr.td_calibration(e_p, y_any),
            "mae_excluding_td_points": {"model": mae_no_td("pred", "pred_td_points"),
                                        "ESPN": mae_no_td("espn_points", "espn_td_points")}}


# Pre-registered adoption criteria (docs/research/dst_projection_model.md, "Adoption criteria").
CRITERIA = {"test_mae_ci90_upper_below": 0.0, "test_spearman_ci90_lower_above": -0.02, "max_abs_bias": 0.5,
            "prospective_mae_margin": 0.25}


def decide(rep) -> dict:
    t = rep["test"]["common_support"]
    out = {"criteria": CRITERIA, "candidates": {}}
    for name in t["methods"]:
        if name == "ESPN" or name.startswith("naive"):
            continue
        cmp = t["vs_ESPN"][name]
        m = t["methods"][name]
        pro = rep["prospective"]["common_support"]
        pro_ok = (pro["n"] == 0 or name not in pro.get("vs_ESPN", {})
                  or pro["vs_ESPN"][name]["mae_diff"] <= CRITERIA["prospective_mae_margin"])
        checks = {"mae_better_than_espn": cmp["mae_diff_ci90"][1] < CRITERIA["test_mae_ci90_upper_below"],
                  "ranking_not_worse": cmp["spearman_diff_ci90"][0] > CRITERIA["test_spearman_ci90_lower_above"],
                  "bias_ok": abs(m["bias_actual_minus_pred"]) <= CRITERIA["max_abs_bias"],
                  "prospective_not_worse": pro_ok}
        out["candidates"][name] = {"checks": checks, "passes": all(checks.values())}
    passing = [n for n, c in out["candidates"].items() if c["passes"]]
    out["recommendation"] = ("adopt as a shadow challenger: " + passing[0]) if passing else "do not adopt"
    return out


def main(argv=None) -> int:
    s = get_settings()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("fetch", "reconcile", "evaluate"))
    ap.add_argument("--season", type=int, default=s.nfl_season)
    ap.add_argument("--live-db", type=Path, default=s.db_path)
    ap.add_argument("--history-db", type=Path, default=_path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--cache", type=Path, default=_path("DST_ESPN_CACHE", "data/raw/espn_dst"))
    ap.add_argument("--out", type=Path, default=_path("DST_RESEARCH_DIR", "data/models/dst"))
    ap.add_argument("--raw-espn", type=Path, default=None, help="reconcile: raw ESPN capture root (data/raw/espn)")
    ap.add_argument("--refresh", action="store_true", help="fetch: re-download cached finished seasons too")
    args = ap.parse_args(argv)
    fn = {"fetch": cmd_fetch, "reconcile": cmd_reconcile, "evaluate": cmd_evaluate}[args.command]
    print(json.dumps(_clean(fn(args)), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
