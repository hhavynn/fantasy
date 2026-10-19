"""NFL Savant xFP vs nflverse ff_opportunity: research only. Offline; read-only on every input.

    cd backend
    uv run python ../scripts/savant_xfp_research.py --pbp-dir DIR --savant-dir DIR [--out DIR]

Inputs (none of them are committed to the repo):
  --pbp-dir     cache of nflverse play-by-play (pbp_<season>.parquet, 2016-2025); missing seasons are fetched
                with nflreadpy (the same source as sync_nfl_history.py).
  --savant-dir  NFL Savant Fantasy-page CSV exports, downloaded by hand from nflsavant.com/fantasy
                (PPR, all players): nflsavant-fantasy-ppr-2025.csv (full season) and
                nflsavant-fantasy-ppr-2025-wk10.csv (week 10 only).
  history DB    nfl_ff_opportunity + nfl_weekly_stats (NFL_HISTORY_DB_PATH or data/nfl_history.duckdb).
  preds dir     ifp-2 backtest predictions written by scripts/context_research.py
                (<model dir>/research/context/{confirmation,exploratory}_preds.parquet).

What it does (docs/research/savant_xfp_comparison.md):
  1. `replica`: rebuilds Savant's published situation-bucket xFP/xTD from play-by-play.
  2. Agreement of Savant, the replica and nflverse xFP on Savant's 2025 season and week-10 exports.
  3. Next-week test (2019-2022 fit, 2023-2025 test): recent-game averages of each xFP as OLS features.
  4. Engine test: does the replica-minus-nflverse gap explain ifp-2's backtest errors out of sample?
Prints aggregates only.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import _bootstrap  # noqa: F401
import duckdb
import numpy as np
import polars as pl

from app.config import REPO_ROOT
from app.nfl.mapping import normalize_name

SEASONS = range(2016, 2026)          # bucket-rate pool ("a decade-plus of play-by-play")
PANEL_SEASONS = range(2019, 2026)    # nfl_ff_opportunity coverage in the history DB
TRAIN, TEST = (2019, 2022), (2023, 2025)
RECENT = 5                           # trailing games, as ifp-2's xfpl_recent
POS = ("QB", "RB", "WR", "TE")
BOOT = 1000

# Savant's published buckets: targets by throw depth x field position, carries by down x distance x field
# position. Their exact edges are not published; these are ours (goal-line yards kept separate because
# "a carry from the 1 is worth about three points").
AY_EDGES = [0, 5, 10, 15, 20, 30]            # air yards: <0, 0-4, 5-9, 10-14, 15-19, 20-29, 30+ (+ null)
YL_EDGES = [2, 3, 6, 11, 21, 41, 61, 81]     # yardline_100: 1, 2, 3-5, 6-10, 11-20, 21-40, 41-60, 61-80, 81-99
DIST_EDGES = [2, 4, 7, 11]                   # ydstogo: 1, 2-3, 4-6, 7-10, 11+


def _bin(col: str, edges: list[int]) -> pl.Expr:
    e = pl.lit(len(edges))
    for i, edge in reversed(list(enumerate(edges))):
        e = pl.when(pl.col(col) < edge).then(pl.lit(i)).otherwise(e)
    return pl.when(pl.col(col).is_null()).then(pl.lit(-1)).otherwise(e)


PBP_COLS = ["season", "week", "season_type", "game_id", "play_id", "posteam", "down", "ydstogo", "yardline_100",
            "air_yards", "pass_attempt", "rush_attempt", "sack", "qb_scramble", "qb_kneel", "qb_spike",
            "two_point_attempt", "passer_player_id", "receiver_player_id", "rusher_player_id", "td_player_id",
            "fumbled_1_player_id", "fumble_lost", "complete_pass", "passing_yards", "receiving_yards", "rushing_yards",
            "pass_touchdown", "rush_touchdown", "interception"]


def load_pbp(pbp_dir: Path) -> pl.DataFrame:
    """nflverse play-by-play, cached per season in pbp_dir (fetched with nflreadpy when missing)."""
    pbp_dir.mkdir(parents=True, exist_ok=True)
    for season in SEASONS:
        path = pbp_dir / f"pbp_{season}.parquet"
        if not path.exists():
            import nflreadpy as nfl
            raw = nfl.load_pbp([season])
            raw.select([c for c in PBP_COLS if c in raw.columns]).write_parquet(path)
    frames = [pl.read_parquet(pbp_dir / f"pbp_{s}.parquet") for s in SEASONS]
    p = pl.concat(frames, how="diagonal_relaxed").filter(pl.col("season_type") == "REG")
    f0 = lambda c: pl.col(c).fill_null(0)  # noqa: E731
    return p.with_columns(
        _bin("air_yards", AY_EDGES).alias("ay_bin"), _bin("yardline_100", YL_EDGES).alias("yl_bin"),
        _bin("ydstogo", DIST_EDGES).alias("dist_bin"), pl.col("down").fill_null(0).cast(pl.Int8).alias("dn"),
        *[f0(c).cast(pl.Float64) for c in ("complete_pass", "receiving_yards", "passing_yards", "rushing_yards",
                                           "pass_touchdown", "rush_touchdown", "interception", "fumble_lost",
                                           "sack", "qb_kneel", "qb_spike", "two_point_attempt",
                                           "pass_attempt", "rush_attempt")])


def replica(pbp: pl.DataFrame, rates_through: int | None = None) -> pl.DataFrame:
    """Player-week xFP (PPR) and xTD from league-average outcomes per situation bucket. Sacks, kneels,
    spikes and two-point tries are excluded on both sides (Savant excludes sacks and sack fumbles).
    `rates_through` limits the seasons the bucket rates are estimated from (leakage check)."""
    live = (pl.col("sack") == 0) & (pl.col("qb_kneel") == 0) & (pl.col("qb_spike") == 0) & (pl.col("two_point_attempt") == 0)
    tgt = pbp.filter(live & (pl.col("pass_attempt") == 1) & pl.col("receiver_player_id").is_not_null()).with_columns(
        pl.col("receiver_player_id").alias("pid"),
        (pl.col("pass_touchdown") * (pl.col("td_player_id") == pl.col("receiver_player_id")).fill_null(False)).alias("td"),
        (pl.col("fumble_lost") * (pl.col("fumbled_1_player_id") == pl.col("receiver_player_id")).fill_null(False)).alias("fl"),
        (pl.col("complete_pass") + 0.1 * pl.col("receiving_yards")).alias("pts_ny"))
    pas = pbp.filter(live & (pl.col("pass_attempt") == 1) & pl.col("passer_player_id").is_not_null()).with_columns(
        pl.col("passer_player_id").alias("pid"), pl.col("pass_touchdown").alias("td"), pl.col("interception").alias("fl"),
        (0.04 * pl.col("passing_yards")).alias("pts_ny"))
    car = pbp.filter(live & (pl.col("rush_attempt") == 1) & pl.col("rusher_player_id").is_not_null()).with_columns(
        pl.col("rusher_player_id").alias("pid"),
        (pl.col("rush_touchdown") * (pl.col("td_player_id") == pl.col("rusher_player_id")).fill_null(False)).alias("td"),
        (pl.col("fumble_lost") * (pl.col("fumbled_1_player_id") == pl.col("rusher_player_id")).fill_null(False)).alias("fl"),
        (0.1 * pl.col("rushing_yards")).alias("pts_ny"))
    parts = []
    for frame, keys, td_w in ((tgt, ["ay_bin", "yl_bin"], 6.0), (pas, ["ay_bin", "yl_bin"], 4.0),
                              (car, ["dn", "dist_bin", "yl_bin"], 6.0)):
        pool = frame if rates_through is None else frame.filter(pl.col("season") <= rates_through)
        rates = pool.group_by(keys).agg(pl.col("pts_ny").mean().alias("e_ny"), pl.col("td").mean().alias("e_td"),
                                        pl.col("fl").mean().alias("e_fl"))
        parts.append(frame.join(rates, on=keys, how="left").select(
            "season", "week", "pid",
            (pl.col("e_ny") + td_w * pl.col("e_td") - 2.0 * pl.col("e_fl")).alias("svx"),
            pl.col("e_td").alias("svxtd")))
    return (pl.concat(parts).group_by("season", "week", "pid")
            .agg(pl.col("svx").sum(), pl.col("svxtd").sum()).rename({"pid": "gsis_id"}))


def panel(con, rep: pl.DataFrame) -> pl.DataFrame:
    """REG player-weeks (QB/RB/WR/TE with a box-score row) with actual PPR, nflverse xFP/xTD and the replica."""
    yrs = ", ".join(str(s) for s in PANEL_SEASONS)
    stats = pl.from_arrow(con.execute(f"""
        SELECT player_id AS gsis_id, player_display_name AS name, season::INT AS season, week::INT AS week, team,
               CASE WHEN position = 'FB' THEN 'RB' ELSE position END AS position, fantasy_points_ppr AS fp
        FROM nfl_weekly_stats WHERE season_type = 'REG' AND season IN ({yrs})""").arrow())
    opp = pl.from_arrow(con.execute(f"""
        SELECT player_id AS gsis_id, season::INT AS season, week::INT AS week,
               sum(total_fantasy_points_exp) AS nfx, sum(total_touchdown_exp) AS nfxtd
        FROM nfl_ff_opportunity WHERE season IN ({yrs}) GROUP BY 1, 2, 3""").arrow())
    rep = rep.with_columns(pl.col("season").cast(pl.Int32), pl.col("week").cast(pl.Int32))
    return (stats.filter(pl.col("position").is_in(POS))
            .join(opp, on=["gsis_id", "season", "week"], how="left")
            .join(rep, on=["gsis_id", "season", "week"], how="left")
            .with_columns(pl.col("svx").fill_null(0.0), pl.col("svxtd").fill_null(0.0))
            # compare on the same games: a week without an nflverse row has no replica value either
            .with_columns([pl.when(pl.col("nfx").is_null()).then(None).otherwise(pl.col(c)).alias(c)
                           for c in ("svx", "svxtd")])
            .sort("gsis_id", "season", "week"))


# ----------------------------------------------------------------------------- agreement with the exports


def read_savant(path: Path) -> pl.DataFrame:
    df = pl.read_csv(path, comment_prefix="#", encoding="utf8-lossy")
    return df.rename({c: c.lstrip("﻿") for c in df.columns})


def match(sav: pl.DataFrame, names: pl.DataFrame) -> pl.DataFrame:
    """Savant rows -> gsis_id by normalized name + position (+ team when ambiguous)."""
    names = names.with_columns(pl.col("name").map_elements(normalize_name, return_dtype=pl.String).alias("nk"))
    sav = sav.with_columns(pl.col("Player").map_elements(normalize_name, return_dtype=pl.String).alias("nk"))
    cand = sav.join(names.select("nk", "position", "team", "gsis_id").unique(), left_on=["nk", "Pos"],
                    right_on=["nk", "position"], how="left")
    n = cand.group_by("Rank").agg(pl.col("gsis_id").n_unique().alias("n"))
    cand = cand.join(n, on="Rank")
    one = cand.filter(pl.col("n") <= 1)
    many = cand.filter((pl.col("n") > 1) & (pl.col("team") == pl.col("Team")))
    return pl.concat([one, many]).unique("Rank").drop("team", "n")


def _agree(df: pl.DataFrame, a: str, b: str) -> dict:
    d = df.drop_nulls([a, b])
    return {"n": d.height, "r": round(float(np.corrcoef(d[a], d[b])[0, 1]), 4),
            "mean_diff": round(float((d[a] - d[b]).mean()), 3),
            "mad": round(float((d[a] - d[b]).abs().mean()), 3)}


def agreement(p: pl.DataFrame, sav_dir: Path) -> dict:
    out = {}
    names = p.filter(pl.col("season") == 2025).select("gsis_id", "name", "position", "team")
    for label, fname, week in (("season_2025", "nflsavant-fantasy-ppr-2025.csv", None),
                               ("week10_2025", "nflsavant-fantasy-ppr-2025-wk10.csv", 10)):
        sav = match(read_savant(sav_dir / fname), names)
        rows = p.filter((pl.col("season") == 2025) & (pl.col("week") == week if week else pl.lit(True)))
        ours = rows.group_by("gsis_id").agg(pl.col("nfx").sum(), pl.col("nfxtd").sum(), pl.col("svx").sum(),
                                           pl.col("svxtd").sum(), pl.col("fp").sum(), pl.len().alias("g"))
        j = sav.join(ours, on="gsis_id", how="left").with_columns(
            (pl.col("RZ Opp") / pl.col("G")).alias("rz_pg"))
        res = {"rows": sav.height, "matched": int(j["nfx"].is_not_null().sum())}
        j = j.drop_nulls("nfx")
        res["all"] = {"savant_vs_nflverse": _agree(j, "xFP", "nfx"), "savant_vs_replica": _agree(j, "xFP", "svx"),
                      "replica_vs_nflverse": _agree(j, "svx", "nfx"),
                      "xtd_savant_vs_nflverse": _agree(j, "xTD", "nfxtd"), "xtd_savant_vs_replica": _agree(j, "xTD", "svxtd"),
                      "fp_savant_vs_nflverse_ppr": _agree(j, "FP", "fp")}
        for pos in POS:
            d = j.filter(pl.col("Pos") == pos)
            res[pos] = {"savant_vs_nflverse": _agree(d, "xFP", "nfx"), "savant_vs_replica": _agree(d, "xFP", "svx"),
                        "xtd_savant_vs_nflverse": _agree(d, "xTD", "nfxtd"),
                        "gap_pct": round(float((d["xFP"].sum() - d["nfx"].sum()) / d["nfx"].sum() * 100), 2)}
        if week is None:  # red-zone roles: Savant's red-zone opportunities per game, terciles within RB/WR/TE
            q = pl.col("rz_pg").rank("ordinal").over("Pos") / pl.len().over("Pos")
            nonqb = j.filter(pl.col("Pos") != "QB").with_columns(
                pl.when(q <= 1 / 3).then(pl.lit("low")).when(q <= 2 / 3).then(pl.lit("mid")).otherwise(pl.lit("high"))
                .alias("rz_t"))
            res["rz_terciles"] = {f"{pos}_{t}": {
                "n": d.height, "gap_per_game": round(float(((d["xFP"] - d["nfx"]) / d["G"]).mean()), 3),
                "xtd_gap": round(float((d["xTD"] - d["nfxtd"]).mean()), 3)}
                for (pos, t), d in sorted(nonqb.group_by("Pos", "rz_t"), key=lambda x: (x[0][0], str(x[0][1])))}
        out[label] = res
    return out


# ----------------------------------------------------------------------------- next-week tests


def trailing(p: pl.DataFrame) -> pl.DataFrame:
    """Each player-week gets means of the previous <= RECENT games he played (any season in the panel)."""
    cols = {"fp": "fp_r", "nfx": "nfx_r", "svx": "svx_r", "nfxtd": "nfxtd_r", "svxtd": "svxtd_r"}
    p = p.sort("gsis_id", "season", "week")
    exprs = [pl.col(c).shift(1).rolling_mean(RECENT, min_samples=1).over("gsis_id").alias(a) for c, a in cols.items()]
    return p.with_columns(*exprs, pl.col("fp").shift(1).over("gsis_id").is_not_null().alias("has_prev"))


def _x(df: pl.DataFrame, feats: list[str]) -> np.ndarray:
    return df.select(feats).to_numpy() if feats else np.zeros((df.height, 0))


def _ols(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    x1 = np.column_stack([np.ones(len(x)), x])
    return np.linalg.lstsq(x1, y, rcond=None)[0]


def _pred(beta: np.ndarray, x: np.ndarray) -> np.ndarray:
    return np.column_stack([np.ones(len(x)), x]) @ beta


def _boot_ci(err_a: np.ndarray, err_b: np.ndarray, groups: np.ndarray, seed: int = 7) -> list[float]:
    """95% CI of mean(|err_b|) - mean(|err_a|), resampling whole weeks."""
    rng = np.random.default_rng(seed)
    uniq, inv = np.unique(groups, return_inverse=True)
    da = np.bincount(inv, np.abs(err_b) - np.abs(err_a)); n = np.bincount(inv)
    stats = []
    for _ in range(BOOT):
        w = np.bincount(rng.integers(0, len(uniq), len(uniq)), minlength=len(uniq))
        stats.append((w * da).sum() / (w * n).sum())
    return [round(float(np.quantile(stats, 0.025)), 4), round(float(np.quantile(stats, 0.975)), 4)]


MODELS = {
    "A_fp": ["fp_r"],
    "B_fp+nflverse": ["fp_r", "nfx_r"],
    "C_fp+savant": ["fp_r", "svx_r"],
    "D_fp+both(=gap)": ["fp_r", "nfx_r", "svx_r"],
    "B2_fp+nflverse+xtd": ["fp_r", "nfx_r", "nfxtd_r"],
    "E_fp+both+xtd": ["fp_r", "nfx_r", "svx_r", "nfxtd_r", "svxtd_r"],
    "F_nflverse_only": ["nfx_r"],
    "G_savant_only": ["svx_r"],
}


def next_week(p: pl.DataFrame) -> dict:
    # Eligibility uses only pregame facts: a previous game and prior xFP coverage (nfx_r). The target
    # week's own nfl_ff_opportunity row is postgame, so uncovered / zero-opportunity weeks stay in.
    d = trailing(p).filter(pl.col("has_prev") & pl.col("nfx_r").is_not_null())
    out = {}
    for pos in (*POS, "ALL"):
        rows = d if pos == "ALL" else d.filter(pl.col("position") == pos)
        tr = rows.filter(pl.col("season").is_between(*TRAIN)); te = rows.filter(pl.col("season").is_between(*TEST))
        res, errs = {"n_train": tr.height, "n_test": te.height}, {}
        for name, feats in MODELS.items():
            if pos == "ALL":  # pooled: per-position fits, errors concatenated
                e = []
                for q in POS:
                    a, b = tr.filter(pl.col("position") == q), te.filter(pl.col("position") == q)
                    beta = _ols(a.select(feats).to_numpy(), a["fp"].to_numpy())
                    e.append(b["fp"].to_numpy() - _pred(beta, b.select(feats).to_numpy()))
                err = np.concatenate(e)
            else:
                beta = _ols(tr.select(feats).to_numpy(), tr["fp"].to_numpy())
                err = te["fp"].to_numpy() - _pred(beta, te.select(feats).to_numpy())
                if name == "D_fp+both(=gap)":
                    res["D_coef"] = {f: round(float(c), 3) for f, c in zip(["const", *feats], beta)}
            errs[name] = err
            res[name] = round(float(np.abs(err).mean()), 4)
        groups = (pl.concat([te.filter(pl.col("position") == q) for q in POS]) if pos == "ALL" else te)
        g = (groups["season"] * 100 + groups["week"]).to_numpy()
        res["D_minus_B_ci"] = _boot_ci(errs["B_fp+nflverse"], errs["D_fp+both(=gap)"], g)
        res["C_minus_B_ci"] = _boot_ci(errs["B_fp+nflverse"], errs["C_fp+savant"], g)
        res["E_minus_B2_ci"] = _boot_ci(errs["B2_fp+nflverse+xtd"], errs["E_fp+both+xtd"], g)
        res["G_minus_F_ci"] = _boot_ci(errs["F_nflverse_only"], errs["G_savant_only"], g)
        out[pos] = res
    return out


def engine(p: pl.DataFrame, preds_dir: Path) -> dict:
    """ifp-2 backtest errors (label - pred_ifp2) vs the trailing replica-minus-nflverse gap. Fit a per-position
    linear correction on one window and score it on the other (2023-24 <-> 2025). Features for a forecast
    week come from the player's last <= RECENT games strictly before it (as-of join), played or not that week,
    drawn only from ifp-2's lookback: the forecast's current and previous season (filtered BEFORE the rolling
    mean, so no game from two or more seasons back enters a window). A missing gap is 0 on both sides."""
    def feats_for(season: int) -> pl.DataFrame:
        t = (p.filter(pl.col("season").is_between(season - 1, season)).sort("gsis_id", "season", "week")
             .with_columns([pl.col(c).rolling_mean(RECENT, min_samples=1).over("gsis_id").alias(c + "_r")
                            for c in ("nfx", "svx", "nfxtd", "svxtd")])
             .with_columns((pl.col("season") * 100 + pl.col("week")).alias("feat_key"),
                           (pl.col("svx_r") - pl.col("nfx_r")).alias("gap_r"),
                           (pl.col("svxtd_r") - pl.col("nfxtd_r")).alias("gaptd_r")))
        return t.select("feat_key", "gsis_id", "gap_r", "gaptd_r").sort("feat_key")

    win = {}
    for name in ("confirmation", "exploratory"):
        pr = pl.read_parquet(preds_dir / f"{name}_preds.parquet").filter(pl.col("key") < 202600)
        pr = (pr.filter(pl.col("pred_ifp2").is_not_null() & pl.col("label").is_not_null())
              .with_columns((pl.col("key") - 1).alias("before"), (pl.col("key") // 100).alias("season")))
        parts = [g.sort("before").join_asof(feats_for(season), left_on="before", right_on="feat_key", by="gsis_id",
                                            strategy="backward")
                 for (season,), g in pr.group_by("season")]
        win[name] = pl.concat(parts).with_columns((pl.col("label") - pl.col("pred_ifp2")).alias("resid"))
    out = {w: {"rows": d.height, "with_gap": int(d["gap_r"].is_not_null().sum()),
               "ifp2_mae": round(float(d["resid"].abs().mean()), 4)} for w, d in win.items()}
    for fit_w, test_w in (("confirmation", "exploratory"), ("exploratory", "confirmation")):
        res, ea_intercept = {}, None
        for fs in ([], ["gap_r"], ["gap_r", "gaptd_r"]):
            errs_b, errs_a, groups, coefs = [], [], [], {}
            for pos in POS:
                a, b = (win[w].filter(pl.col("position") == pos).with_columns(
                    pl.col("gap_r").fill_null(0.0), pl.col("gaptd_r").fill_null(0.0)) for w in (fit_w, test_w))
                beta = _ols(_x(a, fs), a["resid"].to_numpy())
                coefs[pos] = [round(float(c), 3) for c in beta]
                adj = _pred(beta, _x(b, fs))
                base = b["resid"].to_numpy()
                errs_b.append(base); errs_a.append(base - adj); groups.append(b["key"].to_numpy())
            eb, ea, g = map(np.concatenate, (errs_b, errs_a, groups))
            r = {"ifp2_mae": round(float(np.abs(eb).mean()), 4), "corrected_mae": round(float(np.abs(ea).mean()), 4),
                 "delta_ci": _boot_ci(eb, ea, g), "coefs": coefs}
            if ea_intercept is None:
                ea_intercept = ea  # control: a per-position level shift only, no gap
            else:
                r["vs_intercept_only_ci"] = _boot_ci(ea_intercept, ea, g)
            res["+".join(fs) or "intercept_only"] = r
        out[f"fit_{fit_w}_test_{test_w}"] = res
    both = pl.concat(win.values()).drop_nulls("gap_r")
    out["corr_resid_gap"] = {pos: round(float(np.corrcoef(d["resid"], d["gap_r"])[0, 1]), 4)
                             for (pos,), d in sorted(both.group_by("position"))}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbp-dir", type=Path, required=True, help="play-by-play cache (outside the repo)")
    ap.add_argument("--savant-dir", type=Path, required=True)
    ap.add_argument("--history-db", type=Path,
                    default=Path(os.getenv("NFL_HISTORY_DB_PATH") or REPO_ROOT / "data" / "nfl_history.duckdb"))
    ap.add_argument("--rates-through", type=int, help="estimate bucket rates from seasons <= this (leakage check)")
    ap.add_argument("--preds-dir", type=Path, default=REPO_ROOT / "data" / "models" / "research" / "context")
    a = ap.parse_args()
    con = duckdb.connect(str(a.history_db), read_only=True)
    p = panel(con, replica(load_pbp(a.pbp_dir), a.rates_through))
    print(json.dumps({"agreement": agreement(p, a.savant_dir), "next_week": next_week(p),
                      "engine": engine(p, a.preds_dir)}, indent=1))


if __name__ == "__main__":
    main()
