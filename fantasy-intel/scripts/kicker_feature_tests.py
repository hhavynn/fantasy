"""Pre-registered backtest of two candidate kicker features against kfp-1.0.0 (the implied total became production kfp-1.1.0).

    uv run --project backend python scripts/kicker_feature_tests.py [--out data/models/kicker/feature_tests.json]

Candidates (each added alone to production, in both the FG-try and PAT-try Poisson models):
  * implied: the team's implied points from the closing line, (total + spread) / 2 for the home team and
    (total - spread) / 2 for the away team (nflverse spread_line is positive when the home team is favored).
  * fourth: the team's shrunk 4th-down go rate inside FG range (o_fourth_go_rate, already computed point-in-time).

Decision rule, fixed before running: a candidate is adopted only if
  1. its validation (2021-22) MAE is lower than production's, and
  2. its held-out test (2023-25) paired MAE difference vs production has a 95% week-cluster bootstrap CI entirely
     below zero.
Otherwise it is dropped. Weekly Spearman and FG-try deviance are reported as diagnostics only.

Note: backtests use closing lines; a live run uses the line at projection time, which is noisier.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import _bootstrap  # noqa: F401

import numpy as np
import polars as pl

import kicker_projections as kp
from app.analytics import kicker_backtest as kb
from app.analytics import kicker_model as km

km.LAMBDA_FEATURES["fourth"] = ["o_fourth_go_rate"]

# kfp-1.0.0's configuration (the "line" group is the implied total, built by kb.build_dataset since kfp-1.1.0)
BASE = replace(kb.PRODUCTION, groups=("offense", "defense", "stadium"), fg_groups=())
CANDIDATES = {
    "production": BASE,
    "implied": replace(BASE, groups=BASE.groups + ("line",), fg_groups=("line",)),
    "fourth": replace(BASE, groups=BASE.groups + ("fourth",), fg_groups=("fourth",)),
}



def fg_deviance(df: pl.DataFrame) -> float:
    y, mu = df["fg_att"].cast(pl.Float64).to_numpy(), np.maximum(df["pred_lam_fg"].to_numpy(), 1e-9)
    return float(2 * np.mean(np.where(y > 0, y * np.log(np.maximum(y, 1e-9) / mu), 0) - (y - mu)))


def run(data: dict, scoring, seasons) -> dict[str, pl.DataFrame]:
    sf = kb.season_frames(data, BASE, range(kb.FIRST_ROW_SEASON, max(seasons) + 1))
    return {name: pl.concat([kb.evaluate_season(data, cfg, scoring, s, sf) for s in seasons], how="diagonal_relaxed")
            for name, cfg in CANDIDATES.items()}


def summarize(preds: dict[str, pl.DataFrame]) -> dict:
    base = preds["production"].sort("season", "week", "game_id", "team")
    y = base["label"].to_numpy()
    clusters = (base["season"] * 100 + base["week"]).to_numpy()
    out = {}
    for name, df in preds.items():
        df = df.sort("season", "week", "game_id", "team")
        assert df["label"].to_list() == base["label"].to_list()
        row = {**kb._metrics(df["pred"].to_numpy(), y), "fg_try_deviance": round(fg_deviance(df), 5),
               "weekly_spearman": kb.ranking_metrics(df, "pred")["spearman_mean"],
               "pred_sd": round(float(df["pred"].std()), 3)}
        if name != "production":
            row["vs_production"] = kb._paired_ci(y - df["pred"].to_numpy(), y - base["pred"].to_numpy(), clusters)
        out[name] = row
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live-db", type=Path, default=kp._path("FANTASY_DB_PATH", "data/fantasy.duckdb"))
    ap.add_argument("--history-db", type=Path, default=kp._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    _live, _hist, _items, scoring, frames = kp._load(SimpleNamespace(**vars(args)))
    data = kb.build_dataset(frames, scoring, BASE)
    missing = data["rows"].filter(pl.col("completed") & pl.col("o_implied").is_null()).height

    report = {"rule": "adopt iff validation MAE < production AND test paired MAE diff CI95 upper < 0",
              "completed_rows_without_line": missing,
              "validation": summarize(run(data, scoring, kb.VALIDATION)),
              "test": summarize(run(data, scoring, kb.TEST))}
    decisions = {}
    for name in CANDIDATES:
        if name == "production":
            continue
        v_ok = report["validation"][name]["mae"] < report["validation"]["production"]["mae"]
        t_ok = report["test"][name]["vs_production"]["ci95"][1] < 0
        decisions[name] = "adopt" if v_ok and t_ok else "drop"
    report["decisions"] = decisions
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
