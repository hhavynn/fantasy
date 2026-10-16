"""Calibrated fantasy-point distributions around the existing mean projections (research only).

Nothing in the app imports this module (tests/test_point_distributions.py asserts it). It never
changes a mean forecast: every distribution here is "the published mean plus a residual
distribution", centred so that its mean IS the published mean. See
docs/research/fantasy_point_distributions.md.

Statistical meanings (one player-week, league points X):

  conditional_mean      E[X | appears]  = the existing projected_points (ifp-1.0.0 model C)
  p_play                P(appears)      = the existing ipm-1.0.0 participation probability
  expected_points       E[X]            = p_play * conditional_mean (exact; a mixture identity)
  conditional_pN        N-th percentile of X given an appearance
  unconditional_pN      N-th percentile of the MIXTURE: X = 0 with probability 1 - p_play (no
                        appearance), otherwise a draw from the conditional distribution.
                        Not p_play * conditional_pN (that identity holds for means only).
  probability_at_least  P(X >= t), conditional or unconditional.

Representation. A conditional distribution is a non-decreasing quantile function Q(u) given at
knots TAUS (0.01 ... 0.99), linear between knots and constant beyond the outer knots (so the
outermost 1% of each tail sits at the outer knot). Outcomes may be negative (QB interceptions,
fumbles); a flat stretch of Q is a point mass. The unconditional distribution adds a point mass
1 - p_play at exactly 0, and its quantiles are computed from the mixture CDF
F(x) = (1 - p) * 1[x >= 0] + p * F_c(x), never by scaling conditional quantiles.

Methods (walk-forward: the residual pool for forecast week W holds out-of-sample residuals of
weeks strictly before W only):

  A  existing empirical bands: residual quantiles by prediction tercile, low end floored at 0
     (forecast_backtest.interval_bands / independent_projections.interval_for), refit walk-forward
  N  normal: mean +/- position residual SD x z (symmetric baseline)
  B  position residual distribution (all pool residuals of the position)
  C  forecast-magnitude conditioned: residuals of the k pool rows nearest in forecast rank
  F  C with 36-week half-life recency weights (rolling recalibration)
  D  linear quantile regression on the mean plus a few pregame volatility inputs,
     rearranged (sorted) so quantiles never cross
  E  quantile gradient boosting: not fitted (see the report: rejected before fitting)

Every method except A is centred (the residual quantile function has mean exactly 0).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np
import polars as pl

from .forecast_backtest import starters_pool_expr
from .forecast_features import POSITIONS

DISTRIBUTION_VERSION = "ifpd-0.1.0"
ARTIFACT_SCHEMA = "point-distribution-artifact/v1"
TAUS = np.round(np.arange(1, 100) / 100, 2)
SUMMARY_TAUS = (0.1, 0.5, 0.9)
THRESHOLDS = (10.0, 15.0, 20.0)
LOW_RESULT = 5.0                       # "unusually low result": P(X < 5)
WINDOWS = {"development": (2021, 2022), "confirmation": (2023, 2024), "exploratory": (2025, 2026)}
POOL_START = 2020                      # first season whose out-of-sample residuals enter a pool
WEEKS_PER_SEASON = 18
HALF_LIFE_WEEKS = 36.0                 # method F (the ifp-2 recency half-life)
NN_GRID = (300, 1000, 3000)            # method C/F window sizes (chosen on development only)
QR_TAUS = np.round(np.arange(0.02, 0.99, 0.04), 2)
QR_RIDGE = 1e-4
QR_ITERS = 60
# Method D inputs besides the mean (all pregame; missing values imputed with the pool mean + indicator)
QR_FEATURES = {"QB": ["pts_std", "carries_pg"], "RB": ["pts_std", "targets_pg"],
               "WR": ["pts_std", "adot"], "TE": ["pts_std", "adot"]}
SIMPLICITY = ("B", "N", "Bz", "C", "Cz", "R", "F", "Fz", "D", "Dz", "Dr")   # tie-break order (simplest first)
FULL_METHODS = SIMPLICITY
DIAGNOSTIC_METHODS = ("L",)    # not mean-consistent: reported to measure what keeping the mean costs
# Round 2 (added after round-1 DEVELOPMENT results only, before any later window was scored): "z"
# variants respect the support floor. Outcomes pile up at exactly 0 (21-40% of RB/WR/TE appearances)
# and below-zero outcomes are rare, so a centred residual distribution puts probability on
# impossible scores for low forecasts. A z-variant censors the distribution at the floor L and shifts
# it so its mean is still exactly the published mean.
FLOOR_NEGATIVE_SHARE = 0.02    # L = 0 when < 2% of pool outcomes are negative, else the pool's 1st percentile
# Round 3 (after round-2 development results only): a constant shift moves the cluster of zero-point
# outcomes off exactly zero. "R" scales the OUTCOME distribution of the k pool rows nearest in
# forecast so its mean equals the published mean (zeros stay exactly zero, skew is kept); "Dr"
# scales the quantile-regression outcome quantiles the same way. "L" (diagnostic) is the raw local
# outcome distribution with its own mean.
RATIO_MIN_MEAN = 0.1           # below this local mean, scaling is unstable: shift instead

# Pre-registered before any development result was computed.
SELECTION_RULE = {
    "window": "development 2021-2022 (walk-forward; pools of out-of-sample residuals from 2020 on)",
    "eligible": "full-distribution methods (SIMPLICITY: B, N, Bz, C@k, Cz@k, F@k, Fz@k, D, Dz) whose development "
                "80% coverage is in [0.75, 0.85] and whose P10 and P90 tail rates are each in [0.07, 0.13] "
                "(randomized-PIT rates, exact with point masses)",
    "score": "CRPS (99-quantile approximation: 2 x mean pinball loss over TAUS), per position",
    "choice": "lowest development CRPS; a method earlier in SIMPLICITY replaces it when its CRPS is within 0.5% "
              "of the lowest",
    "rounds": "round 1: A, N, B, C@k, F@k, D; round 2 (after round-1 development results only): the support-floor "
              "z-variants and randomized-PIT calibration. No later window was scored before round 2 was frozen.",
    "coverage": [0.75, 0.85], "tail_rate": [0.07, 0.13], "tie_tolerance": 0.005,
}
VALIDATION_CRITERIA = {
    "window": "confirmation 2023-2024, frozen selection, run once",
    "min_rows": 400,
    "coverage80": [0.75, 0.85],
    "p10_below_rate": [0.07, 0.13],
    "p90_above_rate": [0.07, 0.13],
    "p50_mid_rate": [0.45, 0.55],
    "starters_pool_coverage80": [0.75, 0.85],
    "magnitude_tercile_coverage80": [0.72, 0.88],
    "crps_vs_existing_bands": "CRPS not more than 0.5% worse than method A (existing bands, walk-forward) on the "
                              "same rows",
    "crps_tolerance": 0.005,
    "outcome": "all pass -> conditional percentiles 'validated' for the position (research); any fail -> "
               "'withheld' with reasons. Never a production change.",
}
UNCONDITIONAL_CRITERIA = {
    "requires": "conditional distribution validated AND ipm-1.0.0 participation status 'validated' for the position",
    "window": "participation held-out 2025 + 2026 (2023-2024 is the participation model's validation window)",
    "coverage80": [0.75, 0.85],
    "absence_calibration_tolerance": 0.02,
}

# ipm-1.0.0 statuses as published (docs/independent_projection_engine.md, "Participation results");
# the PR #54 isotonic layer stays shadow-only and is not used here.
PARTICIPATION_STATUS = {"QB": "experimental", "RB": "experimental", "WR": "validated", "TE": "validated"}

_NORMAL = NormalDist()


# ----------------------------------------------------------------------------- quantile functions


def _mean_weights(taus: np.ndarray) -> np.ndarray:
    """Weights w with mean(Q) = values @ w for a piecewise-linear Q with constant tails."""
    t = np.asarray(taus, dtype=float)
    w = np.zeros(len(t))
    if len(t) == 1:
        return np.ones(1)
    gaps = np.diff(t)
    w[:-1] += gaps / 2
    w[1:] += gaps / 2
    w[0] += t[0]
    w[-1] += 1 - t[-1]
    return w


MEAN_WEIGHTS = _mean_weights(TAUS)


@dataclass(frozen=True)
class QuantileFunction:
    """Non-decreasing quantile function: linear between (taus[i], values[i]), constant beyond the
    outer knots. taus may include 0 and 1 (then that tail has no point mass)."""
    taus: tuple[float, ...]
    values: tuple[float, ...]

    def __post_init__(self):
        t, v = np.asarray(self.taus, float), np.asarray(self.values, float)
        if t.ndim != 1 or len(t) != len(v) or len(t) == 0:
            raise ValueError("taus and values must be equal-length 1-d sequences")
        if np.any(np.diff(t) <= 0) or t[0] < 0 or t[-1] > 1:
            raise ValueError("taus must be strictly increasing within [0, 1]")
        if np.any(np.diff(v) < -1e-9) or not np.all(np.isfinite(v)):
            raise ValueError("quantile values must be finite and non-decreasing (crossing quantiles)")

    @property
    def _t(self) -> np.ndarray:
        return np.asarray(self.taus, float)

    @property
    def _v(self) -> np.ndarray:
        return np.asarray(self.values, float)

    def quantile(self, u: float) -> float:
        """Q(u) = inf{x : F(x) >= u} for u in (0, 1)."""
        if not 0 < u < 1:
            raise ValueError("u must be in (0, 1)")
        return float(np.interp(u, self._t, self._v))

    def cdf(self, x: float) -> float:
        """P(X <= x)."""
        return float(cdf_right(self._v[None, :], self._t, x)[0])

    def cdf_left(self, x: float) -> float:
        """P(X < x)."""
        return float(cdf_left(self._v[None, :], self._t, x)[0])

    def prob_at_least(self, t: float) -> float:
        return 1.0 - self.cdf_left(t)

    def mean(self) -> float:
        return float(self._v @ _mean_weights(self._t))


def cdf_right(values: np.ndarray, taus: np.ndarray, x) -> np.ndarray:
    """Row-wise P(X <= x) for quantile functions given as rows of `values` at knots `taus`.
    `x` is a scalar or one value per row."""
    v = np.atleast_2d(values)
    t = np.asarray(taus, float)
    x = np.broadcast_to(np.asarray(x, float), (v.shape[0],))
    j = (v <= x[:, None]).sum(1)                 # knots at or below x (values are sorted)
    out = np.empty(v.shape[0])
    lo, hi = j == 0, j == v.shape[1]
    out[lo] = 0.0
    out[hi] = 1.0
    mid = ~(lo | hi)
    if mid.any():
        jj = j[mid]
        rows = np.nonzero(mid)[0]
        v0, v1 = v[rows, jj - 1], v[rows, jj]
        out[mid] = t[jj - 1] + (x[mid] - v0) / (v1 - v0) * (t[jj] - t[jj - 1])
    return out


def cdf_left(values: np.ndarray, taus: np.ndarray, x) -> np.ndarray:
    """Row-wise P(X < x)."""
    v = np.atleast_2d(values)
    t = np.asarray(taus, float)
    x = np.broadcast_to(np.asarray(x, float), (v.shape[0],))
    j = (v < x[:, None]).sum(1)                  # knots strictly below x
    out = np.empty(v.shape[0])
    lo, hi = j == 0, j == v.shape[1]
    out[lo] = 0.0
    out[hi] = 1.0
    mid = ~(lo | hi)
    if mid.any():
        jj = j[mid]
        rows = np.nonzero(mid)[0]
        v0, v1 = v[rows, jj - 1], v[rows, jj]
        out[mid] = t[jj - 1] + (x[mid] - v0) / (v1 - v0) * (t[jj] - t[jj - 1])
    return out


def quantile_rows(values: np.ndarray, taus: np.ndarray, u: np.ndarray) -> np.ndarray:
    """Row-wise Q(u_i) (u one value per row)."""
    v = np.atleast_2d(values)
    t = np.asarray(taus, float)
    u = np.broadcast_to(np.asarray(u, float), (v.shape[0],))
    j = np.clip(np.searchsorted(t, u, side="right"), 1, len(t) - 1)
    rows = np.arange(v.shape[0])
    w = np.clip((u - t[j - 1]) / (t[j] - t[j - 1]), 0.0, 1.0)
    out = v[rows, j - 1] + w * (v[rows, j] - v[rows, j - 1])
    out = np.where(u <= t[0], v[:, 0], out)
    return np.where(u >= t[-1], v[:, -1], out)


# ----------------------------------------------------------------------------- mixtures


@dataclass(frozen=True)
class Mixture:
    """Unconditional distribution: 0 points with probability 1 - p_play (no appearance), else a
    draw from `conditional`."""
    p_play: float
    conditional: QuantileFunction

    def __post_init__(self):
        if not (0.0 <= self.p_play <= 1.0) or math.isnan(self.p_play):
            raise ValueError("p_play must be in [0, 1]")

    def mean(self) -> float:
        return self.p_play * self.conditional.mean()

    def cdf(self, x: float) -> float:
        return (1 - self.p_play) * (1.0 if x >= 0 else 0.0) + self.p_play * self.conditional.cdf(x)

    def cdf_left(self, x: float) -> float:
        return (1 - self.p_play) * (1.0 if x > 0 else 0.0) + self.p_play * self.conditional.cdf_left(x)

    def prob_at_least(self, t: float) -> float:
        return 1.0 - self.cdf_left(t)

    def quantile(self, u: float) -> float:
        c = self.conditional
        return float(mixture_quantile(np.asarray(c.values, float)[None, :], np.asarray(c.taus, float),
                                      np.array([self.p_play]), u)[0])


def mixture_quantile(values: np.ndarray, taus: np.ndarray, p: np.ndarray, u: float) -> np.ndarray:
    """Row-wise generalized inverse of F(x) = (1 - p) 1[x >= 0] + p F_c(x) at level u.

    Below zero the mixture follows the conditional distribution scaled by p; the point mass at 0
    is (1 - p) plus any conditional mass at exactly 0; above it the conditional again."""
    v = np.atleast_2d(values)
    p = np.asarray(p, float)
    a = cdf_left(v, taus, 0.0)                    # P_c(X < 0)
    b = cdf_right(v, taus, 0.0)                   # P_c(X <= 0)
    below, at_zero_top = p * a, (1 - p) + p * b
    with np.errstate(divide="ignore", invalid="ignore"):
        u_low = np.where(p > 0, u / p, 0.5)
        u_high = np.where(p > 0, (u - (1 - p)) / p, 0.5)
    q_low = quantile_rows(v, taus, np.clip(u_low, 1e-12, 1 - 1e-12))
    q_high = quantile_rows(v, taus, np.clip(u_high, 1e-12, 1 - 1e-12))
    return np.where(u <= below, q_low, np.where(u <= at_zero_top, 0.0, q_high))


def summarize(conditional: QuantileFunction | None, conditional_mean: float | None, p_play: float | None,
              *, conditional_status: str, unconditional_ok: bool, withheld_reason: str | None = None) -> dict:
    """Consumer-shaped research summary. Percentiles are None unless the position's distribution is
    validated; unconditional ones additionally need p_play and an approved participation status.
    expected_points = p_play * conditional_mean is reported whenever both exist (mean identity)."""
    cond_ok = conditional is not None and conditional_status == "validated"
    out = {"conditional_mean": conditional_mean, "p_play": p_play,
           "expected_points": None if conditional_mean is None or p_play is None else p_play * conditional_mean,
           "conditional": None, "unconditional": None, "distribution_status": conditional_status,
           "withheld_reason": withheld_reason if not cond_ok else None,
           "semantics": {"conditional": "percentiles of points given an appearance",
                         "unconditional": "percentiles of the mixture with 0 points when he does not appear",
                         "mean_is_not_median": True}}
    if not cond_ok:
        out["withheld_reason"] = withheld_reason or f"conditional distribution {conditional_status}"
        return out
    out["conditional"] = {f"p{int(u * 100)}": conditional.quantile(u) for u in SUMMARY_TAUS} | {
        f"prob_at_least_{t:g}": conditional.prob_at_least(t) for t in THRESHOLDS} | {
        f"prob_below_{LOW_RESULT:g}": conditional.cdf_left(LOW_RESULT)}
    if p_play is None:
        out["unconditional_withheld_reason"] = "no participation estimate (p_play missing): never assumed 1"
    elif not unconditional_ok:
        out["unconditional_withheld_reason"] = "participation probability not validated for this position"
    else:
        mix = Mixture(p_play, conditional)
        out["unconditional"] = {f"p{int(u * 100)}": mix.quantile(u) for u in SUMMARY_TAUS} | {
            f"prob_at_least_{t:g}": mix.prob_at_least(t) for t in THRESHOLDS} | {
            f"prob_below_{LOW_RESULT:g}": mix.cdf_left(LOW_RESULT), "prob_no_appearance": 1 - p_play}
    return out


def prob_greater_independent(a: QuantileFunction | Mixture, b: QuantileFunction | Mixture, grid: int = 400) -> float:
    """P(X_a > X_b) ASSUMING INDEPENDENT OUTCOMES (ties count one half). Players on the same team
    or in the same game are not independent; callers must label the result accordingly."""
    us = (np.arange(grid) + 0.5) / grid
    xb = np.array([b.quantile(u) for u in us])
    pa_gt = np.array([1 - a.cdf(x) for x in xb])
    pa_eq = np.array([a.cdf(x) - a.cdf_left(x) for x in xb])
    return float(np.mean(pa_gt + 0.5 * pa_eq))


# ----------------------------------------------------------------------------- data helpers


def add_inputs(df: pl.DataFrame) -> pl.DataFrame:
    """Derived pregame inputs of method D: aDOT = air yards per target over the recent window."""
    return df.with_columns(pl.when(pl.col("targets_pg") > 0).then(pl.col("air_yards_pg") / pl.col("targets_pg"))
                           .otherwise(None).alias("adot"))


def check_frozen_inputs(selection: dict, participation_selection: dict, scoring_fingerprint: str,
                        selection_sha: str) -> None:
    """Refuse frozen selections made under other league scoring or from another selection.json."""
    if selection.get("scoring_fingerprint") != scoring_fingerprint:
        raise ValueError(f"selection.json was made under scoring {selection.get('scoring_fingerprint')}, the league "
                         f"now scores {scoring_fingerprint}; rerun backtest_independent_projections.py select")
    if participation_selection.get("ifp_selection_sha") != selection_sha:
        raise ValueError("participation_selection.json was made with another selection.json; rerun participation-select")
    if participation_selection.get("scoring_fingerprint") not in (None, scoring_fingerprint):
        raise ValueError("participation_selection.json was made under different league scoring")


def fully_final_weeks(games: pl.DataFrame, panel: pl.DataFrame, weeks: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Weeks whose every scheduled game has a final score AND every scheduled team has synced box
    scores. A partly played or partly synced week is never used (its unplayed players would
    otherwise look like non-participants)."""
    out = []
    for s, w in weeks:
        g = games.filter((pl.col("season") == s) & (pl.col("week") == w))
        if g.height == 0 or g["points_for"].null_count() or g["points_against"].null_count():
            continue
        synced = set(panel.filter((pl.col("season") == s) & (pl.col("week") == w))["team"].unique().to_list())
        if set(g["team"].to_list()) <= synced:
            out.append((s, w))
    return out


# ----------------------------------------------------------------------------- residual methods


def wquantiles(x: np.ndarray, w: np.ndarray | None, taus: np.ndarray) -> np.ndarray:
    """Weighted quantiles with Hazen plotting positions (equal weights = np.quantile 'hazen')."""
    order = np.argsort(x, kind="stable")
    xs = x[order]
    ws = np.ones(len(xs)) if w is None else w[order]
    cw = np.cumsum(ws)
    pos = (cw - ws / 2) / cw[-1]
    return np.interp(taus, pos, xs)


def center(rq: np.ndarray) -> np.ndarray:
    """Shift each residual quantile function so its mean is exactly 0."""
    return rq - (rq @ MEAN_WEIGHTS)[:, None]


def _age_weights(keys: np.ndarray, target_key: int, half_life: float) -> np.ndarray:
    idx = (keys // 100) * WEEKS_PER_SEASON + keys % 100
    t = (target_key // 100) * WEEKS_PER_SEASON + target_key % 100
    return 0.5 ** ((t - idx) / half_life)


def _qr_design(df: pl.DataFrame, position: str, means: dict | None) -> tuple[np.ndarray, dict]:
    cols = ["mean"] + QR_FEATURES[position]
    raw = {c: df[c].cast(pl.Float64).to_numpy().astype(float) for c in cols}
    if means is None:
        means = {c: float(np.nanmean(raw[c])) if np.isfinite(raw[c]).any() else 0.0 for c in cols}
        sds = {c: float(np.nanstd(raw[c])) or 1.0 for c in cols}
        means = {"mean": means, "sd": sds}
    parts = [np.ones(df.height)]
    for c in cols:
        x = raw[c]
        miss = ~np.isfinite(x)
        x = np.where(miss, means["mean"][c], x)
        parts.append((x - means["mean"][c]) / (means["sd"][c] or 1.0))
        if c != "mean":
            parts.append(miss.astype(float))
    return np.column_stack(parts), means


def fit_quantile_regression(x: np.ndarray, y: np.ndarray, taus: np.ndarray, iters: int = QR_ITERS,
                            beta0: np.ndarray | None = None) -> np.ndarray:
    """Linear quantile regression for each tau by iteratively reweighted least squares on the
    check loss (tiny ridge on non-intercept terms for stability). Returns (len(taus), p). A warm
    start (`beta0`, e.g. last week's fit) needs fewer iterations."""
    n, p = x.shape
    ridge = QR_RIDGE * n * np.eye(p)
    ridge[0, 0] = 0.0
    if beta0 is None:
        beta = np.tile(np.linalg.lstsq(x, y, rcond=None)[0], (len(taus), 1))
    else:
        beta, iters = beta0.copy(), max(iters // 4, 10)
    for _ in range(iters):
        for i, t in enumerate(taus):
            r = y - x @ beta[i]
            w = np.where(r >= 0, t, 1 - t) / np.maximum(np.abs(r), 1e-3)
            xw = x * w[:, None]
            beta[i] = np.linalg.solve(xw.T @ x + ridge, xw.T @ y)
    return beta


def _interp_taus(q: np.ndarray, src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Row-wise linear interpolation of quantiles from taus `src` to `dst` (constant extension)."""
    j = np.clip(np.searchsorted(src, dst, side="right"), 1, len(src) - 1)
    w = np.clip((dst - src[j - 1]) / (src[j] - src[j - 1]), 0.0, 1.0)
    return q[:, j - 1] + w * (q[:, j] - q[:, j - 1])


def support_floor(labels: np.ndarray) -> float:
    """Lower support of the conditional distribution from the pool's outcomes (pregame-safe)."""
    if len(labels) == 0 or np.mean(labels < 0) < FLOOR_NEGATIVE_SHARE:
        return 0.0
    return float(np.quantile(labels, 0.01))


def floor_preserving_mean(m: np.ndarray, rq: np.ndarray, floor: float, iters: int = 60) -> np.ndarray:
    """Censor m + rq at `floor` and shift by c <= 0 (bisection) so the censored quantile function
    still has mean exactly m. Returns the new residual quantiles. A forecast at or below the floor
    becomes a point mass at the floor."""
    m = np.asarray(m, float)
    lo = floor - m - rq.max(1) - 1.0
    hi = np.zeros(len(m))

    def mean_at(c):
        return np.maximum(m[:, None] + rq + c[:, None], floor) @ MEAN_WEIGHTS

    for _ in range(iters):
        mid = (lo + hi) / 2
        above = mean_at(mid) > m
        hi = np.where(above, mid, hi)
        lo = np.where(above, lo, mid)
    c = (lo + hi) / 2
    out = np.maximum(m[:, None] + rq + c[:, None], floor)
    # exact mean: spread the bisection remainder over the uncensored knots
    gap = m - out @ MEAN_WEIGHTS
    free = out > floor
    wfree = (free * MEAN_WEIGHTS).sum(1)
    out = np.where(free, out + (np.divide(gap, wfree, out=np.zeros_like(gap), where=wfree > 0))[:, None], out)
    out = np.where((m <= floor)[:, None], floor, out)
    return out - m[:, None]


def _scale_outcomes(m: np.ndarray, yq: np.ndarray) -> np.ndarray:
    """Outcome quantiles yq rescaled so their mean is exactly m (shifted when the local mean is
    too small to scale). Returns residual quantiles (q - m)."""
    mu = yq @ MEAN_WEIGHTS
    ok = mu > RATIO_MIN_MEAN
    q = np.where(ok[:, None], yq * (m / np.where(ok, mu, 1.0))[:, None], yq + (m - mu)[:, None])
    return q - m[:, None]


def _local_outcomes(pool: pl.DataFrame, test: pl.DataFrame, k: int | None, scale: bool) -> np.ndarray:
    pm = pool["mean"].to_numpy().astype(float)
    py = pool["label"].to_numpy().astype(float)
    tm = test["mean"].to_numpy().astype(float)
    n = len(py)
    k = min(k or NN_GRID[0], n)
    order = np.argsort(pm, kind="stable")
    sm, sy = pm[order], py[order]
    lo = np.clip(np.searchsorted(sm, tm) - k // 2, 0, n - k)
    yq = np.vstack([wquantiles(sy[a:a + k], None, TAUS) for a in lo]) if len(tm) else np.zeros((0, len(TAUS)))
    return _scale_outcomes(tm, yq) if scale else yq - tm[:, None]


def method_residual_quantiles(method: str, pool: pl.DataFrame, test: pl.DataFrame, position: str,
                              target_key: int, k: int | None = None, state: dict | None = None) -> np.ndarray:
    """Residual quantile functions (rows = test rows, columns = TAUS) of one method, fitted on
    `pool` (out-of-sample residuals of earlier weeks, one position). Centred except A."""
    if method in ("R", "L"):
        return _local_outcomes(pool, test, k, scale=method == "R")
    if method == "Dr":
        rq = method_residual_quantiles("Draw", pool, test, position, target_key, k, state)
        tm = test["mean"].to_numpy().astype(float)
        return _scale_outcomes(tm, rq + tm[:, None])
    if method.endswith("z"):
        rq = method_residual_quantiles(method[:-1], pool, test, position, target_key, k, state)
        return floor_preserving_mean(test["mean"].to_numpy().astype(float), rq,
                                     support_floor(pool["label"].to_numpy().astype(float)))
    pm = pool["mean"].to_numpy().astype(float)
    pr = (pool["label"] - pool["mean"]).to_numpy().astype(float)
    tm = test["mean"].to_numpy().astype(float)
    n = len(pr)
    if method == "N":
        return np.tile(float(np.std(pr)) * np.array([_NORMAL.inv_cdf(u) for u in TAUS]), (len(tm), 1))
    if method == "B":
        return center(np.tile(wquantiles(pr, None, TAUS), (len(tm), 1)))
    if method == "A":
        cut1, cut2 = np.quantile(pm, [1 / 3, 2 / 3])
        bands = [pr[pm < cut1], pr[(pm >= cut1) & (pm < cut2)], pr[pm >= cut2]]
        rq = [wquantiles(b, None, TAUS) if len(b) else np.zeros(len(TAUS)) for b in bands]
        which = np.where(tm < cut1, 0, np.where(tm < cut2, 1, 2))
        return np.vstack([rq[i] for i in which])
    if method in ("C", "F"):
        k = min(k or NN_GRID[0], n)
        order = np.argsort(pm, kind="stable")
        sm, sr = pm[order], pr[order]
        w_all = _age_weights(pool["key"].to_numpy()[order], target_key, HALF_LIFE_WEEKS) if method == "F" else None
        idx = np.searchsorted(sm, tm)
        lo = np.clip(idx - k // 2, 0, n - k)
        out = np.empty((len(tm), len(TAUS)))
        for i, (m, a) in enumerate(zip(tm, lo)):
            r = sr[a:a + k]
            if a + k == n and m > np.median(sm[a:a + k]) > 0:
                # sparse upper edge: no pool forecasts around m; widen (never narrow) proportionally
                r = r * max(1.0, m / float(np.median(sm[a:a + k])))
            out[i] = wquantiles(r, None if w_all is None else w_all[a:a + k], TAUS)
        return center(out)
    if method in ("D", "Draw"):
        x, stats = _qr_design(pool, position, None)
        beta = fit_quantile_regression(x, pool["label"].to_numpy().astype(float), QR_TAUS,
                                       beta0=None if state is None else state.get(position))
        if state is not None:
            state[position] = beta
        xt, _ = _qr_design(test, position, stats)
        q = np.sort(xt @ beta.T, axis=1)                         # rearrangement: no crossing
        rq = _interp_taus(q, QR_TAUS, TAUS) - tm[:, None]
        return rq if method == "Draw" else center(rq)
    raise ValueError(f"unknown method {method}")


def method_columns(selection: dict | None = None) -> list[tuple[str, str, int | None]]:
    """(column name, method, k) for every development candidate (or the frozen per-position set)."""
    cols = [("A", "A", None), ("N", "N", None), ("B", "B", None), ("D", "D", None), ("Bz", "Bz", None),
            ("Dz", "Dz", None)]
    cols += [(f"{m}@{k}", m, k) for m in ("C", "F", "Cz", "Fz", "R", "L") for k in NN_GRID]
    cols += [("Dr", "Dr", None)]
    return cols


def walk_forward_quantiles(oos: pl.DataFrame, test_keys: list[int], columns: list[tuple[str, str, int | None]],
                           pool_start: int = POOL_START, targets: pl.DataFrame | None = None
                           ) -> tuple[pl.DataFrame, dict[str, np.ndarray]]:
    """For every test week k and position: pool = out-of-sample appearance rows with key < k (from
    pool_start), fit each method on the pool and predict the week's `targets` rows (default: the
    appearance rows themselves; e.g. roster rows for unconditional work). Returns the test rows
    (in output order) and {column: residual quantile matrix}."""
    oos = oos.filter(pl.col("mean").is_not_null()).sort("key", "position", "gsis_id")
    if "played" in oos.columns:                   # residual pools are appearances only
        oos = oos.filter(pl.col("played") == 1)
    targets = oos if targets is None else targets.filter(pl.col("mean").is_not_null()).sort("key", "position", "gsis_id")
    frames, mats = [], {c: [] for c, _, _ in columns}
    state: dict = {}
    for k in sorted(test_keys):
        for pos in POSITIONS:
            test = targets.filter((pl.col("key") == k) & (pl.col("position") == pos))
            if test.height == 0:
                continue
            pool = oos.filter((pl.col("key") < k) & (pl.col("position") == pos) & (pl.col("season") >= pool_start)
                              & pl.col("label").is_not_null())
            assert pool.height == 0 or pool["key"].max() < k, "residual pool row at or after the forecast week"
            if pool.height < 300:
                continue
            frames.append(test)
            for col, method, kk in columns:
                mats[col].append(method_residual_quantiles(method, pool, test, pos, k, kk, state.setdefault(col, {})))
    if not frames:
        return pl.DataFrame(), {c: np.zeros((0, len(TAUS))) for c in mats}
    return pl.concat(frames, how="diagonal_relaxed"), {c: np.vstack(v) for c, v in mats.items()}


def outcome_quantiles(df: pl.DataFrame, rq: np.ndarray, floor_zero: bool = False) -> np.ndarray:
    q = df["mean"].to_numpy().astype(float)[:, None] + rq
    q = np.maximum.accumulate(q, axis=1)          # numerical safety; methods are already monotone
    return np.maximum(q, 0.0) if floor_zero else q


# ----------------------------------------------------------------------------- scoring


def pinball(y: np.ndarray, q: np.ndarray, tau: float) -> np.ndarray:
    d = y - q
    return np.where(d >= 0, tau * d, (tau - 1) * d)


def row_losses(y: np.ndarray, q: np.ndarray) -> dict[str, np.ndarray]:
    """Per-row losses: pinball at P10/P50/P90 and CRPS (99-quantile approximation)."""
    out = {f"pinball_p{int(t * 100)}": pinball(y, q[:, _ti(t)], t) for t in SUMMARY_TAUS}
    out["crps"] = 2 * np.mean(np.column_stack([pinball(y, q[:, i], t) for i, t in enumerate(TAUS)]), axis=1)
    return out


def _ti(t: float) -> int:
    return int(np.argmin(np.abs(TAUS - t)))


def pit_at_most(lo: np.ndarray, hi: np.ndarray, a: float) -> np.ndarray:
    """P(PIT <= a) for the randomized PIT, uniform on [F(y-), F(y)] (exact with point masses)."""
    width = hi - lo
    with np.errstate(divide="ignore", invalid="ignore"):
        frac = np.clip((a - lo) / width, 0.0, 1.0)
    return np.where(width > 1e-12, frac, (hi <= a).astype(float))


def calibration(y: np.ndarray, q: np.ndarray) -> dict:
    """Calibration of the P10/P50/P90 quantiles.

    Tail rates use the randomized PIT, so a point mass (e.g. 0 points) is handled exactly:
    `p10_below_rate` = P(PIT <= 0.1), `p90_above_rate` = P(PIT > 0.9), `coverage80` = their
    complement. For a continuous distribution these are P(y < P10), P(y > P90) and P(P10 <= y <= P90).
    `inclusive_coverage80` is the plain count of outcomes inside [P10, P90] (what a reader of the
    range sees; a point mass at an end makes it larger than 80% by construction)."""
    q10, q50, q90 = (q[:, _ti(t)] for t in SUMMARY_TAUS)
    n = len(y)
    if n == 0:
        return {"n": 0}
    lo = cdf_left(q, TAUS, y)
    hi = cdf_right(q, TAUS, y)
    below, mid, upto90 = (pit_at_most(lo, hi, a) for a in (0.1, 0.5, 0.9))
    edges = np.linspace(0, 1, 11)
    cum = np.array([pit_at_most(lo, hi, a).mean() for a in edges])
    hist = np.diff(cum)
    return {"n": int(n), "p10_below_rate": float(below.mean()), "p90_above_rate": float(1 - upto90.mean()),
            "p50_mid_rate": float(mid.mean()), "coverage80": float(upto90.mean() - below.mean()),
            "inclusive_coverage80": float(np.mean((y >= q10) & (y <= q90))), "width80": float(np.mean(q90 - q10)),
            "pit_decile_max_dev": float(np.max(np.abs(hist - 0.1)))}


def threshold_calibration(y: np.ndarray, q: np.ndarray, thresholds=THRESHOLDS) -> dict:
    out = {}
    for t in thresholds:
        p = 1 - cdf_left(q, TAUS, t)
        o = (y >= t).astype(float)
        out[f">={t:g}"] = {"predicted": float(p.mean()), "observed": float(o.mean()),
                          "brier": float(np.mean((p - o) ** 2)), "reliability": reliability(o, p)}
    p = cdf_left(q, TAUS, LOW_RESULT)
    o = (y < LOW_RESULT).astype(float)
    out[f"<{LOW_RESULT:g}"] = {"predicted": float(p.mean()), "observed": float(o.mean()),
                               "brier": float(np.mean((p - o) ** 2)), "reliability": reliability(o, p)}
    return out


def reliability(o: np.ndarray, p: np.ndarray, bins: int = 5, min_n: int = 50) -> list[dict]:
    edges = np.linspace(0, 1, bins + 1)
    out = []
    for i in range(bins):
        m = (p >= edges[i]) & ((p < edges[i + 1]) if i < bins - 1 else (p <= 1))
        if m.sum() >= min_n:
            out.append({"bin": [round(edges[i], 2), round(edges[i + 1], 2)], "n": int(m.sum()),
                        "predicted": round(float(p[m].mean()), 4), "observed": round(float(o[m].mean()), 4)})
    return out


def week_bootstrap(keys: np.ndarray, diff: np.ndarray, resamples: int = 1000, seed: int = 20261014) -> dict:
    """Mean of `diff` with a week-clustered bootstrap 95% CI (whole weeks resampled together)."""
    uk, inv = np.unique(keys, return_inverse=True)
    if len(uk) < 2:
        return {"mean": float(diff.mean()) if len(diff) else None, "ci95": None, "weeks": int(len(uk))}
    sums, cnts = np.bincount(inv, diff), np.bincount(inv)
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(uk), size=(resamples, len(uk)))
    stats = sums[draws].sum(1) / cnts[draws].sum(1)
    return {"mean": float(diff.mean()), "ci95": [float(np.quantile(stats, 0.025)), float(np.quantile(stats, 0.975))],
            "weeks": int(len(uk))}


# ----------------------------------------------------------------------------- cohorts


def cohort_exprs() -> dict[str, pl.Expr]:
    """Pregame cohorts (every input is known before kickoff)."""
    return {
        "starters_pool": starters_pool_expr(),
        "after_injury_exit": pl.col("last_injury_exit") == 1,
        "after_role_drop": pl.col("last_role_low") == 1,
        "returning_after_absence_ge2wk": pl.col("weeks_since_last") >= 2,
        "team_changed": pl.col("team_changed") == 1,
        "low_sample_lt3_games": pl.col("games_recent") < 3,
        "late_season_wk13plus": pl.col("week") >= 13,
        "late_season_role_change": (pl.col("week") >= 13)
                                   & ((pl.col("last_role_low") == 1) | (pl.col("snap_trend").abs() >= 0.2)),
    }


def magnitude_terciles(df: pl.DataFrame) -> pl.Series:
    """Per-position tercile (0/1/2) of the mean forecast within `df` (a pregame quantity)."""
    return df.with_columns(
        (pl.col("mean").rank("ordinal").over("position") * 3 / (pl.len().over("position") + 1)).floor()
        .clip(0, 2).cast(pl.Int8).alias("_t"))["_t"]


def evaluate_block(df: pl.DataFrame, qs: dict[str, np.ndarray], baseline: str | None = "A",
                   seed: int = 20261014, with_cohorts: bool = True) -> dict:
    """Metrics of every method on the SAME rows (df row order = matrix row order)."""
    y = df["label"].to_numpy().astype(float)
    keys = df["key"].to_numpy()
    losses = {m: row_losses(y, q) for m, q in qs.items()}
    out = {"n": int(df.height), "weeks": int(len(np.unique(keys))), "methods": {}}
    for m, q in qs.items():
        rep = {k: float(v.mean()) for k, v in losses[m].items()} | calibration(y, q)
        rep["implied_mean_minus_published"] = float(np.mean(q @ MEAN_WEIGHTS - df["mean"].to_numpy()))
        if baseline and baseline in qs and m != baseline:
            rep["crps_minus_" + baseline] = week_bootstrap(keys, losses[m]["crps"] - losses[baseline]["crps"], seed=seed)
        out["methods"][m] = rep
    if with_cohorts and df.height:
        flags = df.select(**cohort_exprs())
        terc = magnitude_terciles(df).to_numpy()
        groups = {name: flags[name].fill_null(False).to_numpy() for name in flags.columns}
        groups |= {f"magnitude_tercile_{i}": terc == i for i in range(3)}
        out["cohorts"] = {}
        for name, mask in groups.items():
            if mask.sum() < 30:
                continue
            out["cohorts"][name] = {m: {"n": int(mask.sum()), "crps": float(losses[m]["crps"][mask].mean())}
                                    | {k: v for k, v in calibration(y[mask], q[mask]).items() if k != "n"}
                                    for m, q in qs.items()}
    return out


# ----------------------------------------------------------------------------- selection & status


def choose(dev: dict) -> dict:
    """Apply SELECTION_RULE to development metrics {position: evaluate_block output}."""
    r = SELECTION_RULE
    out = {}
    for pos, block in dev.items():
        cand = {}
        for col, rep in block["methods"].items():
            base = col.split("@")[0]
            if base not in FULL_METHODS:
                continue
            ok = (r["coverage"][0] <= rep["coverage80"] <= r["coverage"][1]
                  and r["tail_rate"][0] <= rep["p10_below_rate"] <= r["tail_rate"][1]
                  and r["tail_rate"][0] <= rep["p90_above_rate"] <= r["tail_rate"][1])
            if ok:
                cand[col] = rep["crps"]
        if not cand:
            out[pos] = {"method": None, "reason": "no development candidate met the calibration bar"}
            continue
        best = min(cand, key=lambda c: (cand[c], c))
        near = [c for c in cand if cand[c] <= cand[best] * (1 + r["tie_tolerance"])]
        pick = min(near, key=lambda c: (SIMPLICITY.index(c.split("@")[0]), cand[c], c))
        out[pos] = {"method": pick, "lowest_crps": best, "development_crps": cand[pick],
                    "eligible": sorted(cand), "reason": "lowest CRPS" if pick == best else
                    f"simpler than {best} within {r['tie_tolerance']:.1%} CRPS"}
    return out


def validation_status(block: dict, method: str) -> dict:
    """VALIDATION_CRITERIA for one position on the confirmation window."""
    c = VALIDATION_CRITERIA
    rep = block["methods"][method]
    checks = []

    def rng(name, value, bounds):
        ok = value is not None and bounds[0] <= value <= bounds[1]
        checks.append({"name": name, "observed": None if value is None else round(value, 4),
                       "required": bounds, "result": "pass" if ok else "fail"})

    checks.append({"name": "min_rows", "observed": block["n"], "required": c["min_rows"],
                   "result": "pass" if block["n"] >= c["min_rows"] else "fail"})
    rng("coverage80", rep["coverage80"], c["coverage80"])
    rng("p10_below_rate", rep["p10_below_rate"], c["p10_below_rate"])
    rng("p90_above_rate", rep["p90_above_rate"], c["p90_above_rate"])
    rng("p50_mid_rate", rep["p50_mid_rate"], c["p50_mid_rate"])
    sp = block.get("cohorts", {}).get("starters_pool", {}).get(method)
    rng("starters_pool_coverage80", None if sp is None else sp["coverage80"], c["starters_pool_coverage80"])
    for i in range(3):
        t = block.get("cohorts", {}).get(f"magnitude_tercile_{i}", {}).get(method)
        rng(f"magnitude_tercile_{i}_coverage80", None if t is None else t["coverage80"], c["magnitude_tercile_coverage80"])
    a = block["methods"].get("A")
    ok = a is not None and rep["crps"] <= a["crps"] * (1 + c["crps_tolerance"])
    checks.append({"name": "crps_vs_existing_bands", "observed": round(rep["crps"], 4),
                   "required": None if a is None else round(a["crps"] * (1 + c["crps_tolerance"]), 4),
                   "result": "pass" if ok else "fail"})
    failed = [x for x in checks if x["result"] != "pass"]
    return {"status": "validated" if not failed else "withheld", "method": method, "checks": checks,
            "reasons": [f"{x['name']}: observed {x['observed']}, required {x['required']}" for x in failed]}


# ----------------------------------------------------------------------------- unconditional


def unconditional_block(df: pl.DataFrame, q_cond: np.ndarray, seed: int = 20261014) -> dict:
    """Roster rows with p (P(appears)), played (0/1) and label (points, 0 when absent).
    Evaluates the mixture's P10/P50/P90, zero-mass calibration and threshold probabilities."""
    if df.height == 0:
        return {"n": 0}
    y = df["label"].to_numpy().astype(float)
    p = df["p"].to_numpy().astype(float)
    played = df["played"].to_numpy().astype(float)
    qs = {u: mixture_quantile(q_cond, TAUS, p, u) for u in SUMMARY_TAUS}
    q10, q50, q90 = qs[0.1], qs[0.5], qs[0.9]
    p_le0 = (1 - p) + p * cdf_right(q_cond, TAUS, 0.0)
    out = {"n": int(df.height), "weeks": int(df["key"].n_unique()),
           "absence": {"predicted": float(np.mean(1 - p)), "observed": float(np.mean(1 - played))},
           "at_or_below_zero": {"predicted": float(p_le0.mean()), "observed": float(np.mean(y <= 0))},
           **_mixture_pit_rates(y, p, q_cond),
           "inclusive_coverage80": float(np.mean((y >= q10) & (y <= q90))), "width80": float(np.mean(q90 - q10)),
           "expected_points_mae": float(np.mean(np.abs(y - p * df["mean"].to_numpy()))),
           "pinball_p10": float(pinball(y, q10, 0.1).mean()), "pinball_p50": float(pinball(y, q50, 0.5).mean()),
           "pinball_p90": float(pinball(y, q90, 0.9).mean()), "thresholds": {}}
    for t in THRESHOLDS:
        pr = p * (1 - cdf_left(q_cond, TAUS, t))
        o = (y >= t).astype(float)
        out["thresholds"][f">={t:g}"] = {"predicted": float(pr.mean()), "observed": float(o.mean()),
                                         "brier": float(np.mean((pr - o) ** 2)), "reliability": reliability(o, pr)}
    pr = (1 - p) + p * cdf_left(q_cond, TAUS, LOW_RESULT)
    o = (y < LOW_RESULT).astype(float)
    out["thresholds"][f"<{LOW_RESULT:g}"] = {"predicted": float(pr.mean()), "observed": float(o.mean()),
                                             "brier": float(np.mean((pr - o) ** 2)), "reliability": reliability(o, pr)}
    out["absence_reliability"] = reliability(1 - played, 1 - p, bins=10)
    # the naive (wrong) construction, for contrast: conditional percentiles times p_play
    naive10, naive90 = p * outcome_col(q_cond, 0.1), p * outcome_col(q_cond, 0.9)
    out["naive_scaled_percentiles"] = {"inclusive_coverage80": float(np.mean((y >= naive10) & (y <= naive90))),
                                       "share_above_p90": float(np.mean(y > naive90)),
                                       "share_below_p10": float(np.mean(y < naive10))}
    return out


def _mixture_pit_rates(y: np.ndarray, p: np.ndarray, q_cond: np.ndarray) -> dict:
    lo = (1 - p) * (y > 0) + p * cdf_left(q_cond, TAUS, y)
    hi = (1 - p) * (y >= 0) + p * cdf_right(q_cond, TAUS, y)
    below, mid, upto90 = (pit_at_most(lo, hi, a) for a in (0.1, 0.5, 0.9))
    return {"p10_below_rate": float(below.mean()), "p90_above_rate": float(1 - upto90.mean()),
            "p50_mid_rate": float(mid.mean()), "coverage80": float(upto90.mean() - below.mean())}


def outcome_col(q: np.ndarray, tau: float) -> np.ndarray:
    return q[:, _ti(tau)]


# ----------------------------------------------------------------------------- pairs & dependence


def pair_frame(df: pl.DataFrame, by: list[str], max_rows: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Index pairs (i < j) of rows sharing the `by` columns (df row order)."""
    d = df.with_row_index("_i").select("_i", *by)
    left = d.join(d, on=by, suffix="_b").filter(pl.col("_i") < pl.col("_i_b"))
    return left["_i"].to_numpy(), left["_i_b"].to_numpy()


def relation(df: pl.DataFrame, i: np.ndarray, j: np.ndarray) -> np.ndarray:
    team, opp = df["team"].to_numpy(), df["opponent"].to_numpy()
    same_team = team[i] == team[j]
    same_game = same_team | (team[i] == opp[j])
    return np.where(same_team, "same_team", np.where(same_game, "opponents", "different_games"))


def residual_dependence(df: pl.DataFrame) -> dict:
    """Pearson correlation of (actual - mean) between players of the same week, by relation and
    position pair. Measures the dependence that independent pairwise probabilities ignore."""
    r = (df["label"] - df["mean"]).to_numpy().astype(float)
    i, j = pair_frame(df, ["key"])
    rel = relation(df, i, j)
    pos = df["position"].to_numpy()
    pp = np.array(["-".join(sorted(t)) for t in zip(pos[i], pos[j])])
    out = {}
    for name in ("same_team", "opponents", "different_games"):
        m = rel == name
        out[name] = {"pairs": int(m.sum()), "corr": _corr(r[i][m], r[j][m]), "by_positions": {}}
        for key_ in sorted(set(pp[m])):
            mm = m & (pp == key_)
            if mm.sum() >= 200:
                out[name]["by_positions"][key_] = {"pairs": int(mm.sum()), "corr": _corr(r[i][mm], r[j][mm])}
    return out


def _corr(a: np.ndarray, b: np.ndarray) -> float | None:
    if len(a) < 3 or a.std() == 0 or b.std() == 0:
        return None
    return round(float(np.corrcoef(a, b)[0, 1]), 4)


def pairwise_calibration(df: pl.DataFrame, q: np.ndarray, grid: int = 25) -> dict:
    """Same-position, same-week pairs in the starters pool: P(A > B) assuming independence vs the
    realized order, by relation (same team / opponents / different games). Ties skipped."""
    d = df.with_columns(starters_pool_expr().alias("_sp"), pl.int_range(pl.len()).alias("_row"))
    sp = d.filter(pl.col("_sp"))
    rows = sp["_row"].to_numpy()
    i, j = pair_frame(sp, ["key", "position"])
    gi, gj = rows[i], rows[j]
    y = df["label"].to_numpy().astype(float)
    keep = y[gi] != y[gj]
    gi, gj, i, j = gi[keep], gj[keep], i[keep], j[keep]
    us = (np.arange(grid) + 0.5) / grid
    xb = np.column_stack([quantile_rows(q[gj], TAUS, np.full(len(gj), u)) for u in us])
    prob = np.zeros(len(gi))
    for c in range(grid):
        hi = cdf_right(q[gi], TAUS, xb[:, c])
        lo = cdf_left(q[gi], TAUS, xb[:, c])
        prob += (1 - hi) + 0.5 * (hi - lo)
    prob /= grid
    won = (y[gi] > y[gj]).astype(float)
    rel = relation(sp, i, j)
    out = {"assumption": f"independent outcomes (dependence not modeled); approximate, {grid}-point grid"}
    for name in ("all", "same_team", "opponents", "different_games"):
        m = np.ones(len(prob), bool) if name == "all" else rel == name
        if m.sum() == 0:
            continue
        mean_ok = np.mean((prob[m] > 0.5) == (won[m] == 1))
        out[name] = {"pairs": int(m.sum()), "brier": float(np.mean((prob[m] - won[m]) ** 2)),
                     "pick_accuracy": float(mean_ok), "reliability": reliability(won[m], prob[m], bins=10, min_n=100)}
    return out


def volatility_signal(df: pl.DataFrame, bins: int = 10) -> dict:
    """Does a player's own recent volatility (pts_std, pregame) predict the spread of his outcome
    beyond what the mean forecast already implies? Within each mean-forecast decile (per position),
    players are split into pts_std terciles; reports the realized 10-90 outcome range and the mean
    absolute residual of the low vs high volatility tercile, pooled over deciles."""
    out = {}
    for pos in POSITIONS:
        g = df.filter((pl.col("position") == pos) & pl.col("pts_std").is_not_null())
        if g.height < 300:
            continue
        g = g.with_columns((pl.col("mean").rank("ordinal") * bins / (pl.len() + 1)).floor().alias("_d"))
        g = g.with_columns((pl.col("pts_std").rank("ordinal").over("_d") * 3 / (pl.len().over("_d") + 1)).floor()
                           .alias("_v"), (pl.col("label") - pl.col("mean")).abs().alias("_ar"))
        rows = []
        for v in (0, 2):
            t = g.filter(pl.col("_v") == v)
            ranges = []
            for lst in t.group_by("_d").agg(pl.col("label")).sort("_d")["label"].to_list():
                a = np.asarray(lst, float)
                if len(a) >= 10:
                    ranges.append(float(np.quantile(a, 0.9) - np.quantile(a, 0.1)))
            rows.append({"n": t.height, "mean_abs_residual": float(t["_ar"].mean()), "outcome_range_10_90": float(np.mean(ranges)),
                         "mean_forecast": float(t["mean"].mean()), "mean_pts_std": float(t["pts_std"].mean())})
        out[pos] = {"low_volatility_tercile": rows[0], "high_volatility_tercile": rows[1],
                    "range_ratio_high_over_low": rows[1]["outcome_range_10_90"] / max(rows[0]["outcome_range_10_90"], 1e-9)}
    return out


# ----------------------------------------------------------------------------- decision scenarios

DECISION_RULE = {
    "population": "same-position, same-week pairs of starters-pool players (top PAIR_POOL by the recent average "
                  "A1) on the participation roster rows; close calls only: |expected_points_a - expected_points_b| "
                  "<= 1.5",
    "needs": "per position, the points a manager needs from the slot: favorite = 40th percentile, underdog = 85th "
             "percentile of starters-pool appearance outcomes in 2020-2022 (earlier than every evaluated week)",
    "ev_rule": "start the player with the higher expected_points",
    "risk_rule": "start the player with the higher unconditional P(points >= need)",
    "success": "the started player's realized points (0 when he did not appear) >= need",
    "never": "needs, rules and the population are fixed before outcomes; no tuning on the evaluated weeks",
    "close_points": 1.5, "need_quantiles": {"favorite": 0.40, "underdog": 0.85},
}


def scenario_needs(appearance_rows: pl.DataFrame) -> dict[str, dict[str, float]]:
    d = appearance_rows.filter(starters_pool_expr())
    q = DECISION_RULE["need_quantiles"]
    return {pos: {s: float(np.quantile(d.filter(pl.col("position") == pos)["label"].to_numpy(), v))
                  for s, v in q.items()} for pos in POSITIONS}


def decision_scenarios(df: pl.DataFrame, q_cond: np.ndarray, needs: dict, seed: int = 20261014) -> dict:
    """Controlled scenario analysis (not a historical win rate): EV rule vs risk rule."""
    d = df.with_columns(starters_pool_expr().alias("_sp"), pl.int_range(pl.len()).alias("_row"))
    sp = d.filter(pl.col("_sp"))
    rows = sp["_row"].to_numpy()
    i, j = pair_frame(sp, ["key", "position"])
    gi, gj = rows[i], rows[j]
    p = df["p"].to_numpy().astype(float)
    m = df["mean"].to_numpy().astype(float)
    ev = p * m
    close = np.abs(ev[gi] - ev[gj]) <= DECISION_RULE["close_points"]
    gi, gj = gi[close], gj[close]
    y = df["label"].to_numpy().astype(float)
    keys = df["key"].to_numpy()[gi]
    pos = df["position"].to_numpy()[gi]
    w80 = outcome_col(q_cond, 0.9) - outcome_col(q_cond, 0.1)
    ratio = np.maximum(w80[gi], w80[gj]) / np.maximum(np.minimum(w80[gi], w80[gj]), 1e-9)
    out = {"rule": DECISION_RULE, "needs": needs, "pairs": int(len(gi)), "scenarios": {}}
    for scen in ("favorite", "underdog"):
        need = np.array([needs[x][scen] for x in pos])
        pa = p[gi] * (1 - cdf_left(q_cond[gi], TAUS, need)) + (1 - p[gi]) * (0 >= need)
        pb = p[gj] * (1 - cdf_left(q_cond[gj], TAUS, need)) + (1 - p[gj]) * (0 >= need)
        ev_pick_a = ev[gi] >= ev[gj]
        risk_pick_a = pa >= pb
        y_ev = np.where(ev_pick_a, y[gi], y[gj])
        y_risk = np.where(risk_pick_a, y[gi], y[gj])
        s_ev, s_risk = (y_ev >= need).astype(float), (y_risk >= need).astype(float)
        dis = ev_pick_a != risk_pick_a
        rep = {"pairs": int(len(gi)), "disagreements": int(dis.sum()),
               "success_ev": float(s_ev.mean()) if len(s_ev) else None,
               "success_risk": float(s_risk.mean()) if len(s_risk) else None,
               "points_ev": float(y_ev.mean()) if len(y_ev) else None,
               "points_risk": float(y_risk.mean()) if len(y_risk) else None}
        if dis.sum() >= 20:
            rep["on_disagreements"] = {"success_risk_minus_ev": week_bootstrap(keys[dis], (s_risk - s_ev)[dis], seed=seed),
                                       "points_risk_minus_ev": week_bootstrap(keys[dis], (y_risk - y_ev)[dis], seed=seed),
                                       "predicted_success_gain": float(np.mean(np.abs(pa - pb)[dis]))}
        for label, mask in (("variance_ratio_ge_1.25", ratio >= 1.25),):
            mm = mask & dis
            rep[label] = {"pairs": int(mask.sum()), "disagreements": int(mm.sum())}
            if mm.sum() >= 20:
                rep[label]["success_risk_minus_ev"] = week_bootstrap(keys[mm], (s_risk - s_ev)[mm], seed=seed)
        out["scenarios"][scen] = rep
    return out


# ----------------------------------------------------------------------------- frozen artifact


def m_grid(pool_means: np.ndarray) -> np.ndarray:
    hi = float(np.ceil(max(pool_means.max(), 1.0) * 2) / 2) + 5.0
    return np.round(np.arange(0.0, hi + 1e-9, 0.5), 2)


def fit_artifact(oos: pl.DataFrame, choice: dict, statuses: dict, *, through_key: int, fingerprint: str,
                 scoring_fingerprint: str, selection_sha: str, participation_status: dict) -> dict:
    """Frozen research artifact: per position, the chosen method's residual quantile table fitted on
    every out-of-sample residual through `through_key`. C/F/B/N are stored as tables over a grid
    of mean forecasts (interpolated at use); D as coefficients."""
    pool_all = oos.filter(pl.col("mean").is_not_null() & pl.col("label").is_not_null()
                          & (pl.col("key") <= through_key) & (pl.col("season") >= POOL_START))
    positions = {}
    for pos in POSITIONS:
        pick = choice.get(pos, {}).get("method")
        st = statuses.get(pos, {})
        entry = {"method": pick, "status": st.get("status", "withheld"), "reasons": st.get("reasons", []),
                 "unconditional": "validated" if (st.get("status") == "validated"
                                                  and participation_status.get(pos) == "validated") else "withheld"}
        pool = pool_all.filter(pl.col("position") == pos)
        if pick is None or pool.height < 300:
            entry["table"] = None
            positions[pos] = entry
            continue
        base, _, k = pick.partition("@")
        target = through_key + 1
        if base in ("D", "Dz", "Dr"):
            x, stats = _qr_design(pool, pos, None)
            beta = fit_quantile_regression(x, pool["label"].to_numpy().astype(float), QR_TAUS)
            entry["table"] = {"kind": "quantile_regression", "post": base, "taus": QR_TAUS.tolist(),
                              "features": ["mean"] + QR_FEATURES[pos], "standardization": stats,
                              "floor": support_floor(pool["label"].to_numpy().astype(float)),
                              "beta": np.round(beta, 8).tolist()}
        else:
            grid = m_grid(pool["mean"].to_numpy())
            test = pl.DataFrame({"mean": grid})
            rq = method_residual_quantiles(base, pool, test, pos, target, int(k) if k else None)
            entry["table"] = {"kind": "mean_grid", "mean_grid": grid.tolist(), "residual_quantiles": np.round(rq, 6).tolist()}
        entry["pool_rows"] = pool.height
        positions[pos] = entry
    body = {"schema": ARTIFACT_SCHEMA, "version": DISTRIBUTION_VERSION, "taus": TAUS.tolist(),
            "input_fingerprint": fingerprint, "scoring_fingerprint": scoring_fingerprint,
            "ifp_selection_sha": selection_sha, "trained_through_key": through_key,
            "mean_model": "ifp-1.0.0 context model C (frozen selection.json penalties), unchanged",
            "participation_model": "ipm-1.0.0 (unchanged; the PR #54 calibration layer is not used)",
            "selection_rule": SELECTION_RULE, "validation_criteria": VALIDATION_CRITERIA,
            "unconditional_criteria": UNCONDITIONAL_CRITERIA, "positions": positions,
            "lifecycle": "research_only"}
    body["content_hash"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return body


def artifact_quantiles(artifact: dict, position: str, mean: float, features: dict | None = None) -> QuantileFunction | None:
    """Conditional quantile function for one forecast from the frozen artifact (None when the
    position has no fitted table). Status is NOT checked here; `artifact_summary` enforces it."""
    entry = artifact["positions"].get(position) or {}
    tab = entry.get("table")
    if not tab:
        return None
    taus = np.asarray(artifact["taus"], float)
    if tab["kind"] == "mean_grid":
        grid = np.asarray(tab["mean_grid"], float)
        rq = np.asarray(tab["residual_quantiles"], float)
        g = float(np.clip(mean, grid[0], grid[-1]))
        j = int(np.clip(np.searchsorted(grid, g, side="right"), 1, len(grid) - 1))
        w = (g - grid[j - 1]) / (grid[j] - grid[j - 1])
        r = rq[j - 1] + w * (rq[j] - rq[j - 1])
    else:
        row = pl.DataFrame({"mean": [float(mean)], **{f: [(features or {}).get(f)] for f in QR_FEATURES[position]}},
                           schema={"mean": pl.Float64, **{f: pl.Float64 for f in QR_FEATURES[position]}})
        x, _ = _qr_design(row, position, tab["standardization"])
        q = np.sort(x @ np.asarray(tab["beta"], float).T, axis=1)
        m = np.array([float(mean)])
        raw = _interp_taus(q, np.asarray(tab["taus"], float), taus) - m[:, None]
        if tab["post"] == "Dr":
            r = _scale_outcomes(m, raw + m[:, None])[0]
        elif tab["post"] == "Dz":
            r = floor_preserving_mean(m, center(raw), tab["floor"])[0]
        else:
            r = center(raw)[0]
    r = r - float(r @ _mean_weights(taus))        # interpolation keeps the mean at 0; re-centre exactly
    return QuantileFunction(tuple(taus.tolist()), tuple(np.maximum.accumulate(mean + r).tolist()))


def artifact_summary(artifact: dict, position: str, conditional_mean: float | None, p_play: float | None,
                     features: dict | None = None) -> dict:
    """Withholding-aware summary for one player-week from the frozen artifact."""
    entry = artifact["positions"].get(position)
    if entry is None:
        return summarize(None, conditional_mean, p_play, conditional_status="unsupported", unconditional_ok=False,
                         withheld_reason=f"no distribution model for {position}")
    qf = None if conditional_mean is None else artifact_quantiles(artifact, position, conditional_mean, features)
    reason = "; ".join(entry.get("reasons") or []) or None
    return summarize(qf, conditional_mean, p_play, conditional_status=entry["status"],
                     unconditional_ok=entry.get("unconditional") == "validated", withheld_reason=reason)


__all__ = ["DISTRIBUTION_VERSION", "QuantileFunction", "Mixture", "mixture_quantile", "summarize",
           "prob_greater_independent", "walk_forward_quantiles", "evaluate_block", "choose", "validation_status",
           "unconditional_block", "fit_artifact", "artifact_summary"]
