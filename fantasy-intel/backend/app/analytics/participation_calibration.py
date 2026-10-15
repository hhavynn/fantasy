"""Research: temporally honest post-hoc calibration of P(appears) (participation `ipm-1.0.0`).

SHADOW ONLY. Nothing in lineups, Start/Sit, trades, waivers, the API, the weekly learning pipeline or
the ESPN/Ours switch reads this module; the production participation artifact is unchanged.

Protocol (docs/research/participation_calibration.md):
  1. Base predictions are OUT-OF-SAMPLE: for every week j the frozen ipm-1.0.0 logistic model
     (frozen penalty per position) is refit on participation rows with key < j and predicts week j
     (participation.walk_forward). In-sample fitted probabilities are never calibrated.
  2. For a test week k, each calibrator is fitted only on those out-of-sample predictions with
     key < k, whose outcomes were known before k ("expanding": every earlier week from the first
     out-of-sample week; "rolling": the last ROLLING_WEEKS weeks). Nothing from week k or later.
  3. Selection (SELECTION_RULES) uses development seasons only; later seasons are reported as
     diagnostics. 2025-2026 were already inspected by the ipm-1.0.0 work: they are NOT an untouched
     holdout and never decide anything here.

Methods (1-D maps of the base probability; injury designations are never hand-tuned):
  none          the uncalibrated base probability (reference)
  platt         logistic regression of the outcome on logit(p)
  platt_shrunk  Platt with a Gaussian prior centred on the identity map (a = 0, b = 1)
  isotonic      pool-adjacent-violators with every block >= ISO_MIN_BLOCK rows; outside the
                calibration data's range the edge block's rate is used (no extrapolation)
A position with fewer than MIN_CAL_ROWS calibration rows, or a single outcome class, falls back to
the base probability. Probabilities are clipped to [P_CLIP, 1 - P_CLIP] like participation.log_loss.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math

import numpy as np
import polars as pl

from .forecast_features import POSITIONS
from .participation import (P_CLIP, PARTICIPATION_VERSION, brier, log_loss, point_metrics, prob_metrics,
                            reliability, week_bootstrap)

CAL_VERSION = "ipm-cal-0.1.0"
CAL_SCHEMA = "participation-calibration/v1"
METHODS = ("none", "platt", "platt_shrunk", "isotonic")
WINDOWS = ("expanding", "rolling")
ROLLING_WEEKS = 36            # about two regular seasons
MIN_CAL_ROWS = 300            # per position; fewer -> base probability
ISO_MIN_BLOCK = 50            # every isotonic block holds at least this many calibration rows
SHRINK_PRIOR = 25.0           # platt_shrunk: Gaussian prior precision on (a, b - 1)

# Pre-registered before any calibrated probability was evaluated (development seasons only).
SELECTION_RULES = {
    "min_development_rows_per_position": 400,
    "log_loss_ci_upper_below_zero": True,       # week-clustered bootstrap, candidate - base
    "brier_ci_upper_below_zero": True,
    "max_relative_unconditional_mae_worsening": 0.005,   # expected points (p * conditional) vs base
    "log_loss_better_in_every_development_season": True,
    "choose": "lowest development log loss among candidates meeting every rule; otherwise the base",
    "never": "a selection is a shadow research recommendation; nothing is deployed",
}


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, P_CLIP, 1 - P_CLIP)
    return np.log(p / (1 - p))


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -35.0, 35.0)))


# ----------------------------------------------------------------------------- calibrators


@dataclass
class Calibrator:
    method: str
    params: dict = field(default_factory=dict)
    n: int = 0
    fallback: str | None = None          # why the base probability is used instead

    def apply(self, p) -> np.ndarray:
        """Calibrated probabilities. Non-finite input (no base prediction) stays NaN."""
        p = np.asarray(p, dtype=float)
        out = np.full(p.shape, np.nan)
        ok = np.isfinite(p)
        if not ok.any():
            return out
        q = p[ok]
        if self.method == "none" or self.fallback:
            out[ok] = q
            return out
        if self.method in ("platt", "platt_shrunk"):
            r = _sigmoid(self.params["a"] + self.params["b"] * _logit(q))
        elif self.method == "isotonic":
            r = np.interp(np.clip(q, P_CLIP, 1 - P_CLIP), self.params["x"], self.params["y"])
        else:
            raise ValueError(f"unknown calibration method {self.method!r}")
        out[ok] = np.clip(r, P_CLIP, 1 - P_CLIP)
        return out

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Calibrator":
        return Calibrator(**d)


def _fit_platt(p: np.ndarray, y: np.ndarray, prior: float) -> dict:
    """Newton steps on NLL + prior/2 * (a^2 + (b - 1)^2) (prior ~ 0 for plain Platt)."""
    z = np.column_stack([np.ones(len(p)), _logit(p)])
    beta = np.array([0.0, 1.0])
    center = np.array([0.0, 1.0])
    pen = np.full(2, max(prior, 1e-6))
    converged = False
    for _ in range(100):
        q = _sigmoid(z @ beta)
        grad = z.T @ (y - q) - pen * (beta - center)
        hess = (z * np.clip(q * (1 - q), 1e-9, None)[:, None]).T @ z + np.diag(pen)
        step = np.linalg.solve(hess, grad)
        beta = beta + step
        if np.max(np.abs(step)) < 1e-10:
            converged = True
            break
    return {"a": float(beta[0]), "b": float(beta[1]), "prior": float(prior), "converged": converged}


def _fit_isotonic(p: np.ndarray, y: np.ndarray, min_block: int) -> dict:
    """Pool-adjacent-violators on rows sorted by p (ties kept together), then adjacent blocks are
    merged until every block has >= min_block rows. Merging adjacent monotone blocks keeps the
    map monotone. Knots: block mean p -> block outcome rate."""
    order = np.lexsort((y, p))
    p, y = p[order], y[order]
    blocks: list[list[float]] = []            # [sum_p, sum_y, n]
    for pi, yi in zip(p, y):
        blocks.append([pi, yi, 1.0])
        while len(blocks) > 1 and blocks[-2][1] / blocks[-2][2] >= blocks[-1][1] / blocks[-1][2]:
            sp, sy, n = blocks.pop()
            blocks[-1][0] += sp
            blocks[-1][1] += sy
            blocks[-1][2] += n
    while len(blocks) > 1 and min(b[2] for b in blocks) < min_block:
        i = min(range(len(blocks)), key=lambda j: (blocks[j][2], j))
        if i == 0:
            j = 1
        elif i == len(blocks) - 1:
            j = i - 1
        else:   # merge with the neighbour whose rate is closer (smaller distortion)
            r = blocks[i][1] / blocks[i][2]
            j = i - 1 if abs(blocks[i - 1][1] / blocks[i - 1][2] - r) <= abs(blocks[i + 1][1] / blocks[i + 1][2] - r) else i + 1
        a, b = sorted((i, j))
        blocks[a] = [blocks[a][k] + blocks[b][k] for k in range(3)]
        del blocks[b]
    i = 1
    while i < len(blocks):          # identical knot positions (ties in p) are pooled: np.interp needs increasing x
        if blocks[i][0] / blocks[i][2] - blocks[i - 1][0] / blocks[i - 1][2] < 1e-12:
            blocks[i - 1] = [blocks[i - 1][k] + blocks[i][k] for k in range(3)]
            del blocks[i]
        else:
            i += 1
    xs = [b[0] / b[2] for b in blocks]
    ys = [min(max(b[1] / b[2], P_CLIP), 1 - P_CLIP) for b in blocks]
    return {"x": [float(v) for v in xs], "y": [float(v) for v in ys], "blocks": len(blocks),
            "block_n": [int(b[2]) for b in blocks], "min_block": min_block}


def fit_calibrator(p, y, method: str, min_rows: int = MIN_CAL_ROWS) -> Calibrator:
    """Fit one calibration map on (base probability, outcome) pairs from EARLIER weeks."""
    if method not in METHODS:
        raise ValueError(f"unknown calibration method {method!r}")
    p, y = np.asarray(p, dtype=float), np.asarray(y, dtype=float)
    ok = np.isfinite(p) & np.isfinite(y)
    p, y = p[ok], y[ok]
    n = int(len(p))
    if method == "none":
        return Calibrator("none", {}, n)
    if n < min_rows:
        return Calibrator(method, {}, n, fallback=f"{n} calibration rows < {min_rows}")
    if y.min() == y.max():
        return Calibrator(method, {}, n, fallback="calibration rows have a single outcome class")
    if method == "platt":
        return Calibrator(method, _fit_platt(p, y, 0.0), n)
    if method == "platt_shrunk":
        return Calibrator(method, _fit_platt(p, y, SHRINK_PRIOR), n)
    return Calibrator(method, _fit_isotonic(p, y, ISO_MIN_BLOCK), n)


# ----------------------------------------------------------------------------- walk-forward


def candidate_columns(methods=METHODS, windows=WINDOWS) -> list[str]:
    return ["p_none"] + [f"p_{m}_{w}" for m in methods if m != "none" for w in windows]


def calibration_history(oos: pl.DataFrame, position: str, k: int, window: str) -> pl.DataFrame:
    """Out-of-sample base predictions with outcomes known before week k (key < k)."""
    if window not in WINDOWS:
        raise ValueError(f"unknown window {window!r}")
    h = oos.filter((pl.col("position") == position) & (pl.col("key") < k) & pl.col("p_model").is_not_null())
    if window == "rolling" and h.height:
        keep = sorted(h["key"].unique().to_list())[-ROLLING_WEEKS:]
        h = h.filter(pl.col("key").is_in(keep))
    assert h.height == 0 or h["key"].max() < k, "calibration row at or after the target week"
    return h


def walk_forward_calibration(oos: pl.DataFrame, test_keys: list[int], methods=METHODS,
                             windows=WINDOWS) -> pl.DataFrame:
    """Calibrated probabilities for every row of `test_keys`. `oos` holds out-of-sample base
    predictions `p_model` (participation.walk_forward), `played`, `position`, `key`. Each map is
    refit per (week, position) on calibration_history only."""
    out = []
    for k in sorted(test_keys):
        week = oos.filter(pl.col("key") == k)
        for pos in POSITIONS:
            rows = week.filter(pl.col("position") == pos)
            if rows.height == 0:
                continue
            base = rows["p_model"].cast(pl.Float64).fill_null(np.nan).to_numpy()
            cols = {"p_none": base}
            for w in windows:
                h = calibration_history(oos, pos, k, w)
                hp = h["p_model"].to_numpy() if h.height else np.array([])
                hy = h["played"].cast(pl.Float64).to_numpy() if h.height else np.array([])
                for m in methods:
                    if m == "none":
                        continue
                    cal = fit_calibrator(hp, hy, m)
                    cols[f"p_{m}_{w}"] = cal.apply(base)
                    cols[f"fallback_{m}_{w}"] = np.array([cal.fallback is not None] * rows.height)
            out.append(rows.with_columns([pl.Series(c, v).fill_nan(None) if v.dtype.kind == "f" else pl.Series(c, v)
                                          for c, v in cols.items()]))
    return pl.concat(out, how="diagonal_relaxed") if out else pl.DataFrame()


# ----------------------------------------------------------------------------- evaluation


def report_cohorts(injury_report_teams: set | None = None) -> dict[str, pl.Expr]:
    """Cohorts on pregame facts. `injury_report_teams`: {(season, week, team)} with any week-W
    injury-report row; a team without a published report is NOT "no designation"."""
    des = (pl.col("inj_out") == 1) | (pl.col("inj_doubtful") == 1) | (pl.col("inj_questionable") == 1)
    out = {
        "designated_questionable": pl.col("inj_questionable") == 1,
        "designated_doubtful_or_out": (pl.col("inj_doubtful") == 1) | (pl.col("inj_out") == 1),
        "returning_from_reserve": pl.col("reserve_since_last_game") == 1,
        "prior_injury_exit": pl.col("injury_exit_last") == 1,
        "week_1": pl.col("week") == 1,
        "early_season_weeks_1_4": pl.col("week") <= 4,
        "midseason_weeks_5_13": (pl.col("week") >= 5) & (pl.col("week") <= 13),
        "late_season_weeks_14_plus": pl.col("week") >= 14,
        "low_history_lt3_games": pl.col("games_recent") < 3,
    }
    if injury_report_teams is not None:
        out["no_designation_report_published"] = ~des.fill_null(False) & pl.col("_team_report")
        out["no_team_injury_report"] = ~pl.col("_team_report")
    else:
        out["no_designation"] = ~des.fill_null(False)
    return out


def with_report_flag(df: pl.DataFrame, injury_report_teams: set | None) -> pl.DataFrame:
    if injury_report_teams is None or df.height == 0:
        return df
    keys = pl.DataFrame([{"season": s, "week": w, "team": t} for s, w, t in sorted(injury_report_teams)],
                        schema={"season": pl.Int64, "week": pl.Int64, "team": pl.String})
    return df.join(keys.with_columns(pl.lit(True).alias("_team_report")), on=["season", "week", "team"],
                   how="left").with_columns(pl.col("_team_report").fill_null(False))


def candidate_report(rows: pl.DataFrame, col: str, seed: int, resamples: int = 1000, base: str = "p_none") -> dict:
    """Probability scores, calibration and unconditional points of `col`, and week-clustered paired
    differences vs `base` on the SAME rows (both columns non-null, conditional estimate present)."""
    r = rows.filter(pl.col(col).is_not_null() & pl.col(base).is_not_null() & pl.col("cond").is_not_null())
    if r.height == 0:
        return {"n": 0}
    y = r["played"].cast(pl.Float64).to_numpy()
    p, b = r[col].to_numpy(), r[base].to_numpy()
    cond, label = r["cond"].to_numpy(), r["label"].to_numpy()
    pc, bc = np.clip(p, P_CLIP, 1 - P_CLIP), np.clip(b, P_CLIP, 1 - P_CLIP)
    d = r.with_columns(pl.Series("_bc", (p - y) ** 2), pl.Series("_bb", (b - y) ** 2),
                       pl.Series("_lc", -(y * np.log(pc) + (1 - y) * np.log(1 - pc))),
                       pl.Series("_lb", -(y * np.log(bc) + (1 - y) * np.log(1 - bc))),
                       pl.Series("_uc", np.abs(label - p * cond)), pl.Series("_ub", np.abs(label - b * cond)))
    rel = reliability(y, p)
    return {"n": r.height, "probability": prob_metrics(y, p), "ece": rel["ece"],
            "max_gap_bins_n_ge_100": rel["max_gap_bins_n_ge_min"], "reliability": rel["bins"],
            "unconditional_points": point_metrics(label, p * cond),
            "minus_base_brier": week_bootstrap(d, "_bc", "_bb", resamples, seed),
            "minus_base_log_loss": week_bootstrap(d, "_lc", "_lb", resamples, seed),
            "minus_base_unconditional_mae": week_bootstrap(d, "_uc", "_ub", resamples, seed)}


def compare(preds: pl.DataFrame, columns: list[str], seed: int, cohorts: dict[str, pl.Expr] | None = None,
            resamples: int = 1000) -> dict:
    """Per position: every candidate on identical rows (all candidate columns non-null)."""
    out = {}
    common = preds.filter(pl.all_horizontal([pl.col(c).is_not_null() for c in columns]) & pl.col("cond").is_not_null())
    for pos in [*POSITIONS, "ALL"]:
        g = common if pos == "ALL" else common.filter(pl.col("position") == pos)
        rep = {"n": g.height, "candidates": {c: candidate_report(g, c, seed, resamples) for c in columns}}
        rep["by_season"] = {int(s): {c: {"n": h.height, **{k: v for k, v in prob_metrics(
            h["played"].cast(pl.Float64).to_numpy(), h[c].to_numpy()).items() if k in ("brier", "log_loss")}}
            for c in columns} for (s,), h in g.group_by(["season"], maintain_order=True)}
        if cohorts and pos != "ALL":
            rep["cohorts"] = {}
            for name, expr in cohorts.items():
                h = g.filter(expr.fill_null(False))
                rep["cohorts"][name] = {"n": h.height, **({c: _cohort_metrics(h, c) for c in columns} if h.height else {})}
        out[pos] = rep
    return out


def _cohort_metrics(h: pl.DataFrame, c: str) -> dict:
    y = h["played"].cast(pl.Float64).to_numpy()
    p = h[c].to_numpy()
    return {"played_rate": round(float(y.mean()), 4), "mean_p": round(float(p.mean()), 4),
            "brier": round(brier(y, p), 5), "log_loss": round(log_loss(y, p), 5),
            "unconditional_mae": point_metrics(h["label"].to_numpy(), p * h["cond"].to_numpy())["mae"]}


def select(dev: dict, columns: list[str]) -> dict:
    """Apply SELECTION_RULES to a development-season `compare` report. Base unless a candidate
    passes every rule."""
    r = SELECTION_RULES
    out = {}
    for pos in POSITIONS:
        rep = dev.get(pos) or {}
        checks = {}
        for c in columns:
            if c == "p_none":
                continue
            cr = rep.get("candidates", {}).get(c) or {}
            reasons = []
            if cr.get("n", 0) < r["min_development_rows_per_position"]:
                reasons.append(f"{cr.get('n', 0)} development rows < {r['min_development_rows_per_position']}")
            else:
                ll, br = cr["minus_base_log_loss"]["ci95"], cr["minus_base_brier"]["ci95"]
                if not (ll and ll[1] < 0):
                    reasons.append(f"log-loss difference CI {ll} does not exclude 0")
                if not (br and br[1] < 0):
                    reasons.append(f"Brier difference CI {br} does not exclude 0")
                um, ub = cr["unconditional_points"]["mae"], rep["candidates"]["p_none"]["unconditional_points"]["mae"]
                if ub and (um - ub) / ub > r["max_relative_unconditional_mae_worsening"]:
                    reasons.append(f"unconditional MAE {um} worse than base {ub} by more than "
                                   f"{r['max_relative_unconditional_mae_worsening']:.1%}")
                worse = [s for s, v in rep.get("by_season", {}).items() if v[c]["log_loss"] >= v["p_none"]["log_loss"]]
                if worse:
                    reasons.append(f"log loss not better than base in development season(s) {sorted(worse)}")
            checks[c] = {"passes": not reasons, "reasons": reasons,
                         "log_loss": (cr.get("probability") or {}).get("log_loss")}
        passing = [c for c, v in checks.items() if v["passes"]]
        chosen = min(passing, key=lambda c: (checks[c]["log_loss"], c)) if passing else "p_none"
        out[pos] = {"selected": chosen, "candidates": checks}
    return out


def parse_column(col: str) -> tuple[str, str | None]:
    if col == "p_none":
        return "none", None
    _, method_window = col.split("_", 1)
    method, window = method_window.rsplit("_", 1)
    return method, window


# ----------------------------------------------------------------------------- shadow artifact


@dataclass
class CalibrationArtifact:
    version: str
    created_at: str
    base_model: dict                       # participation version, penalties, base artifact identity
    calibrators: dict[str, Calibrator]     # position -> fitted map (method "none" = base kept)
    training_cutoff_key: int | None
    sample_counts: dict[str, int]
    fingerprints: dict                     # scoring fingerprint, source data hash
    validation: dict                       # development selection + diagnostic summary
    status: str = "shadow"
    schema: str = CAL_SCHEMA

    def to_dict(self) -> dict:
        d = asdict(self)
        d["calibrators"] = {p: c.to_dict() for p, c in self.calibrators.items()}
        return d

    @staticmethod
    def from_dict(d: dict) -> "CalibrationArtifact":
        if d.get("schema") != CAL_SCHEMA:
            raise ValueError(f"unsupported calibration artifact schema {d.get('schema')!r}")
        if d.get("status") != "shadow":
            raise ValueError("participation calibration artifacts are research/shadow only")
        d = dict(d)
        d["calibrators"] = {p: Calibrator.from_dict(c) for p, c in d["calibrators"].items()}
        return CalibrationArtifact(**d)

    def apply(self, position: str, p) -> np.ndarray:
        cal = self.calibrators.get(position)
        return np.asarray(p, dtype=float) if cal is None else cal.apply(p)


def fit_artifact(oos: pl.DataFrame, selection: dict, *, created_at: str, base_model: dict,
                 fingerprints: dict, validation: dict) -> CalibrationArtifact:
    """Fit each position's selected map on every out-of-sample base prediction with an outcome
    (window as selected). Positions whose selection is the base keep method "none"."""
    cals, counts = {}, {}
    cutoff = int(oos["key"].max()) + 1 if oos.height else None
    for pos in POSITIONS:
        method, window = parse_column(selection.get(pos, {}).get("selected", "p_none"))
        h = calibration_history(oos, pos, cutoff, window or "expanding") if cutoff else pl.DataFrame()
        hp = h["p_model"].to_numpy() if h.height else np.array([])
        hy = h["played"].cast(pl.Float64).to_numpy() if h.height else np.array([])
        cal = fit_calibrator(hp, hy, method)
        cal.params = {**cal.params, "window": window}
        cals[pos], counts[pos] = cal, int(len(hp))
    return CalibrationArtifact(CAL_VERSION, created_at, {"participation_version": PARTICIPATION_VERSION, **base_model},
                               cals, None if cutoff is None else cutoff - 1, counts, fingerprints, validation)


__all__ = ["Calibrator", "fit_calibrator", "walk_forward_calibration", "calibration_history", "compare",
           "select", "CalibrationArtifact", "fit_artifact", "METHODS", "WINDOWS", "SELECTION_RULES", "CAL_VERSION"]
