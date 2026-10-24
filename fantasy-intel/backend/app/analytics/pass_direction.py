"""Research only: does a defense's allowed pass-direction profile predict where opponents throw against it next game,
beyond the league average and the offense's own tendency? (issue #198 step 2; docs/research/pass_direction.md)

Nothing in the app imports this module (tests/test_pass_direction.py); only scripts/pass_direction_research.py does. It
writes nothing and changes no forecast, page or table.

Cells are play-by-play `pass_location` (left, middle, right) x `pass_length` (short, deep): six cells, measured on
regular-season pass attempts with both fields known. An attempt with either field unknown is counted as unknown and
left out of every share; it is never assigned a cell. The throw direction is from the offense's side and says nothing
about which defender covered whom: no defender is ever named or paired (docs/research/defensive_matchup_sources.md).

For each attempt, from same-season games strictly before its week:

  league    the league-wide cell share (one pseudo-attempt per cell, so no cell is ever 0).
  offense   the offense's own share, shrunk toward league with SHRINK pseudo-attempts.
  combined  offense x (defense allowed share / league share), the defense share shrunk the same way. The adjustment is
            applied only to cells where the defense has at least MIN_CELL prior attempts; every other cell keeps the
            offense share (abstention: unadjusted, never 0). Renormalized over the six cells.

DECISION_RULE is fixed here before the first run on real data.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import math
import random

import numpy as np
import polars as pl

PD_VERSION = "pass-direction-0.1.0"
PD_STATUS = "research_only"
LOCATIONS = ("left", "middle", "right")
LENGTHS = ("short", "deep")
CELLS = tuple(f"{loc}_{ln}" for ln in LENGTHS for loc in LOCATIONS)
SHRINK = 100.0                 # pseudo-attempts toward the league share, for offense and defense alike
MIN_CELL = 30                  # prior defense attempts in a cell before its adjustment is used
EVAL_SEASONS = (2022, 2025)
EVAL_WEEKS = (5, 18)
BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED = 2000, 198
LABEL = "Unadjusted for opponent strength. Throw direction from the offense's side; no defender is named or paired."

DECISION_RULE = (
    "On regular-season pass attempts with known pass_location and pass_length, seasons 2022-2025, weeks 5-18, each "
    "predicted from same-season games strictly before its week: the defense's pass-direction profile adds signal only "
    "if mean per-attempt log loss of the combined prediction minus that of the offense prediction, and of the combined "
    "prediction minus that of the league prediction, both have a 95% paired bootstrap interval entirely below 0 under "
    "BOTH clusterings (by season-week and by season-defense; 2000 resamples, seed 198). Defense adjustments are used "
    "only in cells with at least 30 prior defense attempts; elsewhere the cell is unadjusted, never 0. Every result is "
    "reported whatever the outcome. No route or page follows without a passing result and the merge captain's go."
)


# ----------------------------------------------------------------------------- attempts


def attempts(pbp: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
    """Regular-season pass attempts (no sacks, no two-point tries) with their cell. Returns (known attempts, counts)
    where counts has the attempts left out because a field is unknown."""
    pa = pbp.filter((pl.col("season_type") == "REG") & (pl.col("two_point_attempt").fill_null(0) == 0)
                    & (pl.col("pass_attempt").fill_null(0) == 1) & (pl.col("sack").fill_null(0) == 0)
                    & (pl.col("play_type") == "pass"))
    known = pl.col("pass_location").is_in(list(LOCATIONS)) & pl.col("pass_length").is_in(list(LENGTHS))
    out = pa.filter(known).select(
        pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64), "game_id", "posteam", "defteam",
        (pl.col("pass_location") + "_" + pl.col("pass_length")).alias("cell"))
    return out, {"pass_attempts": pa.height, "known": out.height, "unknown_cell": pa.height - out.height}


# ----------------------------------------------------------------------------- predictions


def _counts(prior: pl.DataFrame, key: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(CELLS, 0))
    for team, cell, n in prior.group_by(key, "cell").len().iter_rows():
        out[team][cell] = n
    return out


def league_share(prior: pl.DataFrame) -> dict[str, float]:
    n = dict.fromkeys(CELLS, 1)                       # one pseudo-attempt per cell: never 0
    for cell, k in prior.group_by("cell").len().iter_rows():
        n[cell] += k
    total = sum(n.values())
    return {c: n[c] / total for c in CELLS}


def shrunk_share(counts: dict[str, int], league: dict[str, float]) -> dict[str, float]:
    total = sum(counts.values())
    return {c: (counts[c] + SHRINK * league[c]) / (total + SHRINK) for c in CELLS}


def combined_share(offense: dict[str, float], defense: dict[str, float], def_counts: dict[str, int],
                   league: dict[str, float]) -> tuple[dict[str, float], int]:
    """Offense share times the defense's allowed/league ratio in cells with >= MIN_CELL defense attempts; other cells
    unadjusted. Renormalized. Returns (shares, adjusted cells)."""
    raw, adjusted = {}, 0
    for c in CELLS:
        if def_counts[c] >= MIN_CELL:
            raw[c] = offense[c] * defense[c] / league[c]
            adjusted += 1
        else:
            raw[c] = offense[c]
    total = sum(raw.values())
    return {c: raw[c] / total for c in CELLS}, adjusted


def predict(att: pl.DataFrame, seasons: tuple[int, int] = EVAL_SEASONS, weeks: tuple[int, int] = EVAL_WEEKS
            ) -> pl.DataFrame:
    """One row per evaluated attempt: the probability each prediction gave its actual cell, the predicted share
    vectors (for per-game error) and the number of defense-adjusted cells. Point in time: week w uses weeks < w of the
    same season only."""
    rows = []
    for (season, week), cur in att.filter(pl.col("season").is_between(*seasons)
                                          & pl.col("week").is_between(*weeks)).group_by(
            "season", "week", maintain_order=True):
        prior = att.filter((pl.col("season") == season) & (pl.col("week") < week))
        lg = league_share(prior)
        off_n, def_n = _counts(prior, "posteam"), _counts(prior, "defteam")
        cache: dict[tuple[str, str], tuple] = {}
        for game_id, pos, dfn, cell in cur.select("game_id", "posteam", "defteam", "cell").iter_rows():
            if (pos, dfn) not in cache:
                off = shrunk_share(off_n[pos], lg)
                dfs = shrunk_share(def_n[dfn], lg)
                comb, adjusted = combined_share(off, dfs, def_n[dfn], lg)
                cache[(pos, dfn)] = (off, comb, adjusted)
            off, comb, adjusted = cache[(pos, dfn)]
            rows.append({"season": season, "week": week, "game_id": game_id, "posteam": pos, "defteam": dfn,
                         "cell": cell, "p_league": lg[cell], "p_offense": off[cell], "p_combined": comb[cell],
                         "adjusted_cells": adjusted,
                         **{f"league_{c}": lg[c] for c in CELLS}, **{f"offense_{c}": off[c] for c in CELLS},
                         **{f"combined_{c}": comb[c] for c in CELLS}})
    return pl.DataFrame(rows)


# ----------------------------------------------------------------------------- evaluation


def _percentile(sorted_draws: list[float], q: float) -> float:
    pos = q * (len(sorted_draws) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_draws) - 1)
    return sorted_draws[lo] + (sorted_draws[hi] - sorted_draws[lo]) * (pos - lo)


def _ll_gap(rows: list[dict], a: str, b: str) -> dict:
    """Mean log loss of a minus b (negative: a is better), with 95% paired bootstrap intervals clustered by
    (season, week) and by (season, defense)."""
    out: dict = {"difference": None}
    for name, key in (("by_week", lambda r: (r["season"], r["week"])),
                      ("by_defense_season", lambda r: (r["season"], r["defteam"]))):
        cs: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0.0, 0])
        for r in rows:
            c = cs[key(r)]
            c[0] += -math.log(r[a])
            c[1] += -math.log(r[b])
            c[2] += 1
        cl = [cs[k] for k in sorted(cs)]

        def gap(sample):
            n = sum(c[2] for c in sample)
            return (sum(c[0] for c in sample) - sum(c[1] for c in sample)) / n

        out["difference"] = gap(cl)
        rng = random.Random(BOOTSTRAP_SEED)
        draws = sorted(gap([cl[rng.randrange(len(cl))] for _ in cl]) for _ in range(BOOTSTRAP_RESAMPLES))
        out[name] = {"ci95": [_percentile(draws, 0.025), _percentile(draws, 0.975)], "clusters": len(cl)}
    return out


def _passes(gap: dict) -> bool:
    return all(gap[k]["ci95"][1] < 0 for k in ("by_week", "by_defense_season"))


def game_share_mae(preds: pl.DataFrame) -> dict[str, float]:
    """Not deciding: mean absolute error between each (game, offense)'s realized cell shares and the predicted shares."""
    out = {}
    g = preds.group_by("game_id", "posteam")
    realized = g.agg([(pl.col("cell") == c).mean().alias(f"r_{c}") for c in CELLS])
    for name in ("league", "offense", "combined"):
        pred = g.agg([pl.col(f"{name}_{c}").first().alias(f"p_{c}") for c in CELLS])
        j = realized.join(pred, on=["game_id", "posteam"])
        err = np.mean([np.abs(j[f"r_{c}"].to_numpy() - j[f"p_{c}"].to_numpy()) for c in CELLS])
        out[name] = float(err)
    return out


def evaluate(preds: pl.DataFrame) -> dict:
    out = {"version": PD_VERSION, "status": PD_STATUS, "label": LABEL, "decision_rule": DECISION_RULE,
           "decision_rule_sha256": hashlib.sha256(DECISION_RULE.encode()).hexdigest(),
           "cells": list(CELLS), "shrink": SHRINK, "min_cell": MIN_CELL}
    if preds.is_empty():
        return {**out, "attempts": 0, "decision": "no_data"}
    rows = preds.select("season", "week", "defteam", "p_league", "p_offense", "p_combined").to_dicts()
    gaps = {"combined_minus_offense": _ll_gap(rows, "p_combined", "p_offense"),
            "combined_minus_league": _ll_gap(rows, "p_combined", "p_league")}
    ll = {k: float(-np.mean(np.log(preds[f"p_{k}"].to_numpy()))) for k in ("league", "offense", "combined")}
    adj = preds["adjusted_cells"].to_numpy()
    return {**out, "attempts": preds.height, "games": preds["game_id"].n_unique(),
            "weeks": preds.select("season", "week").n_unique(),
            "log_loss": ll, "gaps": gaps,
            "abstention": {"mean_adjusted_cells": float(adj.mean()), "share_no_adjustment": float((adj == 0).mean()),
                           "share_all_cells_adjusted": float((adj == len(CELLS)).mean())},
            "diagnostics": {"game_share_mae": game_share_mae(preds)},     # not deciding
            "decision": "pass" if all(_passes(g) for g in gaps.values()) else "fail"}
