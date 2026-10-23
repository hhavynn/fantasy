"""Research only: frozen walk-forward backtest of the pace lab's estimates (issue #211; docs/research/pace_lab.md).

Nothing in the app imports this module (tests/test_pace_lab.py). It changes no projection, forecast, registry or
production table: it reads offense-games through pace_lab.load_games and returns a result dict, which
scripts/pace_lab_backtest.py writes to app/analytics/pace_lab_backtest.json for the lab page to display.

Design (fixed in SPEC before any real-data run; its SHA-256 is recorded with every result):
  * Targets: a team's run/pass plays in each regular-season game from week 2 on (overtime games excluded; a game whose
    overtime flag is not stored is counted and reported), and its neutral-situation pass rate (weighted by that game's
    neutral plays).
  * Point-in-time: every prediction for (season S, week W) uses only counted games of season S with week < W. The
    team must have at least one earlier volume game that season, so every model is scored on identical rows.
  * Baselines: the league mean to date and the team's season-to-date raw mean (raw rate for the neutral pass rate).
  * Candidates: the shrunk team mean (k = pace_lab.PRIOR_GAMES / PRIOR_NEUTRAL_PLAYS), shrunk team + shrunk opponent
    plays allowed - league mean (additive matchup), and the lab's script-mixture central estimate with the team's
    observed script mix to date (league mix when it has no classified game).
  * Scores: MAE (headline), RMSE, bias. Paired MAE differences against each baseline with a 95% bootstrap interval,
    resampling (season, week) clusters, 2,000 draws, fixed seed.
  * Verdict per candidate: "improves" only if its MAE is lower than BOTH baselines' and both intervals lie below 0;
    "worse" if its MAE is higher than the better baseline's and that interval lies above 0; otherwise "no clear
    difference".
  * Ranges: coverage of the lab's 10-90% script range, conditional on the realized script (does the range for the
    script that happened contain the game?), and of the pregame mixture range; the nominal share is 80%.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from types import MappingProxyType
from typing import Sequence

import numpy as np

from . import pace_lab as pl_
from .pace_lab import SCRIPTS, TeamGame

BACKTEST_VERSION = "pace-bt-0.1.0"
SEED = 20261023
BOOTSTRAP_DRAWS = 2000
MIN_WEEK = 2
PLAY_MODELS = ("league_mean", "team_to_date", "shrunk_team", "matchup_additive", "script_mixture")
PLAY_BASELINES = ("league_mean", "team_to_date")
PLAY_CANDIDATES = ("shrunk_team", "matchup_additive", "script_mixture")
NEUTRAL_MODELS = ("league_rate", "team_to_date", "shrunk_team")
NEUTRAL_BASELINES = ("league_rate", "team_to_date")
NEUTRAL_CANDIDATES = ("shrunk_team",)

SPEC = MappingProxyType({
    "version": BACKTEST_VERSION,
    "target_plays": "team run/pass plays per game (nfl_team_week.offense_plays), regular season, week >= 2, "
                    "overtime games excluded",
    "target_neutral": "team neutral pass rate per game, weighted by the game's neutral plays",
    "history": "counted games of the same season with week < target week; team needs >= 1 earlier volume game",
    "play_models": PLAY_MODELS, "play_baselines": PLAY_BASELINES, "play_candidates": PLAY_CANDIDATES,
    "neutral_models": NEUTRAL_MODELS, "neutral_baselines": NEUTRAL_BASELINES, "neutral_candidates": NEUTRAL_CANDIDATES,
    "priors": {"games": pl_.PRIOR_GAMES, "script_games": pl_.PRIOR_SCRIPT_GAMES,
               "neutral_plays": pl_.PRIOR_NEUTRAL_PLAYS, "pass_plays": pl_.PRIOR_PASS_PLAYS},
    "one_score": pl_.ONE_SCORE, "range_quantiles": pl_.RANGE_QUANTILES,
    "metric": "MAE (headline), RMSE, bias", "bootstrap": {"draws": BOOTSTRAP_DRAWS, "seed": SEED,
                                                          "cluster": "(season, week)", "interval": 0.95},
    "verdict": "improves: MAE below both baselines and both 95% intervals of the paired difference below 0; worse: "
               "MAE above the better baseline and that interval above 0; otherwise no clear difference",
})


def spec_hash() -> str:
    return hashlib.sha256(json.dumps(dict(SPEC), sort_keys=True, default=list).encode()).hexdigest()


def _shrunk(total: float, n: float, prior: float, k: float) -> float:
    return (total + k * prior) / (n + k)


class _History:
    """Aggregates of one season's counted games before the target week."""

    def __init__(self, games: Sequence[TeamGame]):
        self.games = list(games)
        vol = [g for g in self.games if g.volume_ok]
        self.league_mean = sum(g.plays for g in vol) / len(vol) if vol else None
        self.team_plays: dict[str, list[int]] = defaultdict(list)
        self.allowed: dict[str, list[int]] = defaultdict(list)
        for g in vol:
            self.team_plays[g.team].append(g.plays)
            if g.opponent:
                self.allowed[g.opponent].append(g.plays)
        nn = [g for g in self.games if g.counted and g.neutral_pass is not None]
        den = sum(g.neutral_plays for g in nn)
        self.league_neutral = sum(g.neutral_pass for g in nn) / den if den else None
        self.team_neutral: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for g in nn:
            acc = self.team_neutral[g.team]
            acc[0] += g.neutral_pass
            acc[1] += g.neutral_plays
        self.league = pl_.league_summary(self.games)
        self.pools = pl_.residual_pools(self.games)
        self.by_team: dict[str, list[TeamGame]] = defaultdict(list)
        for g in self.games:
            self.by_team[g.team].append(g)


def _play_record(h: _History, g: TeamGame) -> dict | None:
    if not g.volume_ok or h.league_mean is None or not h.team_plays.get(g.team):
        return None
    lm = h.league_mean
    tp = h.team_plays[g.team]
    shrunk = _shrunk(sum(tp), len(tp), lm, pl_.PRIOR_GAMES)
    opp = h.allowed.get(g.opponent, []) if g.opponent else []
    opp_shrunk = _shrunk(sum(opp), len(opp), lm, pl_.PRIOR_GAMES)
    team_games = h.by_team[g.team]
    overall = pl_.team_estimates(team_games, h.league)
    per_script = pl_.script_estimates(team_games, h.league, overall)
    mix, _ = pl_.observed_mix(team_games)
    if mix is None:
        mix = tuple(h.league["scripts"][s]["share"] or 0.0 for s in SCRIPTS)
    pre = pl_.mixture(mix, per_script, h.pools, None)["measures"]["plays"]
    rec = {"season": g.season, "week": g.week, "team": g.team, "actual": g.plays, "overtime_unknown": g.overtime is None,
           "pred": {"league_mean": lm, "team_to_date": sum(tp) / len(tp), "shrunk_team": shrunk,
                    "matchup_additive": shrunk + opp_shrunk - lm,
                    "script_mixture": pre["expected"]},
           "pre_range": (pre["low"], pre["high"]), "cond_range": None}
    if rec["pred"]["script_mixture"] is None:
        return None
    if g.script:
        one = tuple(1.0 if s == g.script else 0.0 for s in SCRIPTS)
        c = pl_.mixture(one, per_script, h.pools, None)["measures"]["plays"]
        rec["cond_range"] = (c["low"], c["high"])
    return rec


def _neutral_record(h: _History, g: TeamGame) -> dict | None:
    if g.neutral_pass is None or h.league_neutral is None:
        return None
    acc = h.team_neutral.get(g.team)
    if not acc or not acc[1]:
        return None
    num, den = acc
    return {"season": g.season, "week": g.week, "team": g.team, "actual": g.neutral_pass_rate,
            "weight": g.neutral_plays,
            "pred": {"league_rate": h.league_neutral, "team_to_date": num / den,
                     "shrunk_team": _shrunk(num, den, h.league_neutral, pl_.PRIOR_NEUTRAL_PLAYS)}}


def collect(games: Sequence[TeamGame]) -> tuple[list[dict], list[dict], dict]:
    """Walk forward through every season: week by week, predict from strictly earlier weeks, then add the week."""
    usable = [g for g in games if g.counted]
    by_sw: dict[tuple, list[TeamGame]] = defaultdict(list)
    for g in usable:
        by_sw[(g.season, g.week)].append(g)
    plays, neutral = [], []
    skipped = {"no_prior_team_game": 0, "overtime": 0, "week_1": 0}
    for season in sorted({s for s, _ in by_sw}):
        hist: list[TeamGame] = []
        for week in sorted(w for s, w in by_sw if s == season):
            targets = sorted(by_sw[(season, week)], key=lambda g: g.team)
            if week < MIN_WEEK or not hist:
                skipped["week_1"] += len(targets)
            else:
                h = _History(hist)
                for g in targets:
                    if g.overtime is True:
                        skipped["overtime"] += 1
                    r = _play_record(h, g)
                    if r is not None:
                        plays.append(r)
                    elif g.volume_ok:
                        skipped["no_prior_team_game"] += 1
                    n = _neutral_record(h, g)
                    if n is not None:
                        neutral.append(n)
            hist.extend(targets)
    return plays, neutral, skipped


def _clusters(records: Sequence[dict]) -> np.ndarray:
    keys = {k: i for i, k in enumerate(sorted({(r["season"], r["week"]) for r in records}))}
    return np.array([keys[(r["season"], r["week"])] for r in records], dtype=np.int64)


def _paired(err_a: np.ndarray, err_b: np.ndarray, weights: np.ndarray, clusters: np.ndarray,
            rng: np.random.Generator) -> dict:
    """Weighted mean of (a - b) and its cluster-bootstrap 95% interval."""
    diff = (err_a - err_b) * weights
    n_c = int(clusters.max()) + 1
    sum_d = np.bincount(clusters, weights=diff, minlength=n_c)
    sum_w = np.bincount(clusters, weights=weights, minlength=n_c)
    draws = rng.integers(0, n_c, size=(BOOTSTRAP_DRAWS, n_c))
    boot = sum_d[draws].sum(axis=1) / sum_w[draws].sum(axis=1)
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return {"mean": round(float(diff.sum() / weights.sum()), 4), "low": round(float(lo), 4), "high": round(float(hi), 4)}


def _score(records: Sequence[dict], models: Sequence[str], baselines: Sequence[str], candidates: Sequence[str],
           weighted: bool, rng: np.random.Generator) -> dict:
    if not records:
        return {"n": 0, "models": {}, "comparisons": {}, "verdicts": {}, "by_season": {}}
    actual = np.array([r["actual"] for r in records], dtype=float)
    w = np.array([r["weight"] for r in records], dtype=float) if weighted else np.ones(len(records))
    clusters = _clusters(records)
    errs, stats = {}, {}
    for m in models:
        pred = np.array([r["pred"][m] for r in records], dtype=float)
        e = pred - actual
        errs[m] = np.abs(e)
        stats[m] = {"mae": round(float((np.abs(e) * w).sum() / w.sum()), 4),
                    "rmse": round(float(np.sqrt((e ** 2 * w).sum() / w.sum())), 4),
                    "bias": round(float((e * w).sum() / w.sum()), 4)}
    comparisons, verdicts = {}, {}
    best = min(baselines, key=lambda b: stats[b]["mae"])
    for c in candidates:
        comparisons[c] = {b: _paired(errs[c], errs[b], w, clusters, rng) for b in baselines}
        below = all(stats[c]["mae"] < stats[b]["mae"] and comparisons[c][b]["high"] < 0 for b in baselines)
        above = stats[c]["mae"] > stats[best]["mae"] and comparisons[c][best]["low"] > 0
        verdicts[c] = "improves" if below else "worse" if above else "no clear difference"
    by_season = {}
    for s in sorted({r["season"] for r in records}):
        idx = np.array([r["season"] == s for r in records])
        by_season[str(s)] = {"n": int(idx.sum()),
                             **{m: round(float((errs[m][idx] * w[idx]).sum() / w[idx].sum()), 4) for m in models}}
    return {"n": len(records), "weight_total": round(float(w.sum()), 1) if weighted else None, "models": stats,
            "best_baseline": best, "comparisons": comparisons, "verdicts": verdicts, "by_season": by_season}


def _coverage(records: Sequence[dict], key: str) -> dict:
    lo, hi = pl_.RANGE_QUANTILES
    rows = [r for r in records if r[key] and None not in r[key]]
    inside = sum(1 for r in rows if r[key][0] <= r["actual"] <= r[key][1])
    return {"n": len(rows), "inside": inside, "share": round(inside / len(rows), 4) if rows else None,
            "nominal": round(hi - lo, 2)}


def walk_forward(games: Sequence[TeamGame]) -> dict:
    """The frozen backtest over every offense-game given. Deterministic: same games, same result."""
    rng = np.random.default_rng(SEED)
    plays, neutral, skipped = collect(games)
    return {
        "version": BACKTEST_VERSION, "spec_sha256": spec_hash(),
        "seasons": sorted({r["season"] for r in plays}),
        "plays": _score(plays, PLAY_MODELS, PLAY_BASELINES, PLAY_CANDIDATES, False, rng),
        "neutral_pass_rate": _score(neutral, NEUTRAL_MODELS, NEUTRAL_BASELINES, NEUTRAL_CANDIDATES, True, rng),
        "range_coverage": {"conditional_on_script": _coverage(plays, "cond_range"),
                           "pregame_mixture": _coverage(plays, "pre_range")},
        "skipped": skipped,
        "overtime_unknown_targets": sum(1 for r in plays if r["overtime_unknown"]),
    }
