"""Research only: adaptive established-workload (role) estimator and the ifp-aw research challenger.

Nothing here changes ifp-1.0.0 or the frozen ifp-2.0.0 configuration, and nothing outside research
scripts imports it (tests/test_adaptive_workload.py). See docs/research/adaptive_workload.md.

Normal role vs expected actual workload. `aw_role` estimates the expected-league-points opportunity a
player would get in a game he plays from start to finish in his current coaching role. It is not his
expected workload next week: that also depends on whether he appears (participation model) and on
whether he finishes the game, which is not modelled here (no P(limited | appears) model: prior exits
do not predict another exit, docs/research/ifp2_weekly_learning_report.md section 2).

How a past game informs the role. Each pre-cutoff game gets an information class (from
forecast_features.classify_games plus play-by-play evidence) and an exposure fraction f in (0, 1]:
the share of a normal game the player was available for, as far as the evidence says.

  normal            no snap drop                                        f = 1
  role_low          snap drop, no injury / blowout evidence             f = 1   (taken at face value:
                    a genuine demotion is never discounted because usage fell)
  injury_exit_pbp   snap drop + play-by-play injury mention             f = e
  injury_exit_report snap drop + a new injury listing after it only     f = 1 - 0.5 (1 - e)
  blowout_low       snap drop in a game decided by >= 17 points         f = 1 - 0.5 (1 - e)
  listed_low        snap drop, listed both before and after (chronic)   f = 1 - 0.5 (1 - e)
  week18_low        any non-injury snap drop in the season's final regular-season week
                    (possible rest)                                     f = 1 - 0.5 (1 - e)
  unknown           no snap record or no reference snap share           f = 1

with e = clip(snap share / reference snap share, MIN_EXPOSURE, 1). An injury-shortened game thus keeps
its information in proportion to the part of the game the player was on the field: 4 carries in 16%
of a game is evidence for about 0.16 games at that player's per-snap rate, not a zero and not a full
game. A game without expected points (xfpl) carries no information (never zero-filled).

Established role (empirical Bayes, the concept of the ifp-2 `xfpl_eb` with exposure semantics):

  aw_eb = (k * P + sum_g o_g) / (k + sum_g f_g)        over this season's games with xfpl

where o_g is the game's expected league points, P the previous season's exposure-weighted role
(shrunk toward the position mean by SHRINK_GAMES; the position mean without a previous season) and
k = USAGE_PRIOR_GAMES (times TEAM_CHANGE_PRIOR_FACTOR after a team change, in the adaptive config).

Adaptive regime blend (aw_role): the most recent <= REGIME_GAMES face-value games (normal, role_low,
unknown; exits, blowouts and week-18 drops are skipped, they neither extend nor break a streak) are
compared with the established role computed WITHOUT them. If the latest n >= MIN_REGIME_GAMES of them
all deviate in the same direction by >= REGIME_DELTA (the largest such n, tested longest first), the
recent regime gets weight r = n / (n + REGIME_K)
(0.5 for two games, 0.6 for three):

  aw_role = (1 - r) * aw_eb + r * mean(o over the n streak games)

r is a fixed function of pregame evidence: it is never fitted to outcomes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json

import numpy as np
import polars as pl

from .challenger import (TRAIN_START, ChallengerConfig, cohorts as ch_cohorts, feature_set, fit_linear,
                         recency_weights, train_rows)
from .forecast_backtest import _arr, metrics, pair_decisions, week_bootstrap_mae_diff
from .forecast_features import (POSITIONS, PRIOR_SEASONS, SHRINK_GAMES, USAGE_PRIOR_GAMES, Sources,
                                classify_games, key)
from .independent_projections import LAMBDA_GRID, MIN_TRAIN_ROWS

AW_VERSION = "ifp-aw-0.1.0"
INFO_CLASSES = ("normal", "role_low", "injury_exit_pbp", "injury_exit_report", "blowout_low", "listed_low",
                "week18_low", "unknown")
# confidence that a low-usage game under-states the role (0 = take the game at face value)
EXPOSURE_CONFIDENCE = {"injury_exit_pbp": 1.0, "injury_exit_report": 0.5, "blowout_low": 0.5,
                       "listed_low": 0.5, "week18_low": 0.5}
FACE_VALUE = ("normal", "role_low", "unknown")     # games that can establish a new regime
MIN_EXPOSURE = 0.1
REGIME_GAMES = 3
MIN_REGIME_GAMES = 2      # one deviating game is noise, not a regime
REGIME_DELTA = 0.25
REGIME_K = 2.0
TEAM_CHANGE_PRIOR_FACTOR = 0.5
AW_COLUMNS = ("aw_eb", "aw_role", "aw_r", "aw_streak", "aw_info", "aw_prev_role", "aw_xfpl_missing_games",
              "aw_last_class")
_INT_COLUMNS, _STR_COLUMNS = ("aw_streak", "aw_xfpl_missing_games"), ("aw_last_class",)


@dataclass(frozen=True)
class RoleConfig:
    exposure: bool = True          # False: every game f = 1 (ablation; injury exits kept at face value)
    adaptive: bool = True          # False: aw_role = aw_eb
    team_change_factor: float = TEAM_CHANGE_PRIOR_FACTOR

    def tag(self) -> str:
        return f"e{int(self.exposure)}a{int(self.adaptive)}t{self.team_change_factor:g}"


# ----------------------------------------------------------------------------- game information


def final_regular_weeks(games: pl.DataFrame) -> dict[int, int]:
    """Last regular-season week of each season, from the schedule (REG games only)."""
    if games.height == 0:
        return {}
    return {int(s): int(w) for s, w in games.group_by("season").agg(pl.col("week").max()).iter_rows()}


def information_classes(classified: pl.DataFrame, exits: pl.DataFrame | None, final_weeks: dict[int, int],
                        config: RoleConfig = RoleConfig()) -> pl.DataFrame:
    """Add `info_class` and exposure fraction `f` to classify_games output (pre-cutoff games only).
    Play-by-play evidence describes completed games only; the caller passes pre-cutoff games."""
    keys = ["season", "week", "gsis_id"]
    c = classified
    if exits is not None and exits.height:
        ex = exits.select(*keys, (pl.col("injured_in_game") == 1).alias("_pbp")).unique(keys)
        c = c.join(ex, on=keys, how="left")
    else:
        c = c.with_columns(pl.lit(None, dtype=pl.Boolean).alias("_pbp"))
    last_week = pl.col("season").replace_strict(final_weeks, default=None, return_dtype=pl.Int64)
    week18 = (pl.col("week") == last_week).fill_null(False)
    cls = (pl.when(pl.col("gclass") == "injury_exit")
             .then(pl.when(pl.col("_pbp").fill_null(False)).then(pl.lit("injury_exit_pbp"))
                   .otherwise(pl.lit("injury_exit_report")))
           .when(pl.col("gclass").is_in(["role_low", "blowout_low", "listed_low"]) & week18).then(pl.lit("week18_low"))
           .when(pl.col("gclass") == "full").then(pl.lit("normal"))
           .otherwise(pl.col("gclass")))
    e = (pl.col("offense_pct") / pl.col("ref_snap")).clip(MIN_EXPOSURE, 1.0)
    conf = pl.col("info_class").replace_strict(EXPOSURE_CONFIDENCE, default=0.0, return_dtype=pl.Float64)
    f = (1.0 - conf * (1.0 - e.fill_null(1.0))) if config.exposure else pl.lit(1.0)
    return c.with_columns(cls.alias("info_class")).with_columns(f.alias("f")).drop("_pbp")


# ----------------------------------------------------------------------------- role estimate


def _role_one(rows: list[dict], season: int, prior_pos: float, team: str | None, cfg: RoleConfig) -> dict:
    """rows: one player's pre-cutoff games (newest first) with season, week, team, xfpl, f, info_class."""
    prev = [r for r in rows if r["season"] == season - 1]
    cur = [r for r in rows if r["season"] == season]
    prev_x = [r for r in prev if r["xfpl"] is not None]
    cur_x = [r for r in cur if r["xfpl"] is not None]
    prev_role = ((sum(r["xfpl"] for r in prev_x) + prior_pos * SHRINK_GAMES)
                 / (sum(r["f"] for r in prev_x) + SHRINK_GAMES)) if prev_x else None
    prior = prev_role if prev_role is not None else prior_pos
    changed = bool(prev) and team is not None and prev[0]["team"] is not None and prev[0]["team"] != team
    k = USAGE_PRIOR_GAMES * (cfg.team_change_factor if changed else 1.0)
    out = {"aw_prev_role": prev_role, "aw_info": float(sum(r["f"] for r in cur_x)),
           "aw_last_class": rows[0]["info_class"] if rows else None,
           "aw_xfpl_missing_games": len(cur) - len(cur_x), "aw_streak": 0, "aw_r": 0.0}
    if not cur_x and prev_role is None:
        out.update(aw_eb=None, aw_role=None)
        return out

    def eb(games):
        return (k * prior + sum(g["xfpl"] for g in games)) / (k + sum(g["f"] for g in games))

    out["aw_eb"] = eb(cur_x)
    out["aw_role"] = out["aw_eb"]
    if not cfg.adaptive:
        return out
    face = [r for r in cur_x if r["info_class"] in FACE_VALUE][:REGIME_GAMES]
    # The longest qualifying streak wins, tested longest first: a shorter candidate's baseline still
    # contains the other shifted games, so it can fail even when the longer regime is clear.
    streak, sign = 0, 0
    for n in range(len(face), 0, -1):
        ids = {(g["season"], g["week"]) for g in face[:n]}
        base = eb([g for g in cur_x if (g["season"], g["week"]) not in ids])
        devs = [g["xfpl"] / base - 1.0 if base > 0 else 0.0 for g in face[:n]]
        s = 1 if all(d >= REGIME_DELTA for d in devs) else -1 if all(d <= -REGIME_DELTA for d in devs) else 0
        if s:
            streak, sign = n, s
            break
    if streak >= MIN_REGIME_GAMES:
        r = streak / (streak + REGIME_K)
        recent = sum(g["xfpl"] for g in face[:streak]) / streak
        out.update(aw_r=r * sign, aw_role=(1 - r) * out["aw_eb"] + r * recent)
    out["aw_streak"] = streak * sign
    return out


def role_features_for_week(sources: Sources, season: int, week: int, targets: pl.DataFrame,
                           config: RoleConfig = RoleConfig()) -> pl.DataFrame:
    """Role features for `targets` (gsis_id, position, team) for NFL week (season, week). Uses only
    games strictly before (season, week): the cutoff is applied here, like features_for_week."""
    cutoff = key(season, week)
    panel = sources.panel.filter((pl.col("season") * 100 + pl.col("week") < cutoff)
                                 & (pl.col("season") >= season - PRIOR_SEASONS))
    schema = {"gsis_id": pl.String, **{c: (pl.Int64 if c in _INT_COLUMNS else pl.String if c in _STR_COLUMNS
                                            else pl.Float64) for c in AW_COLUMNS}}
    if targets.height == 0:
        return pl.DataFrame(schema=schema)
    exits = None if sources.exits is None else sources.exits.filter(pl.col("season") * 100 + pl.col("week") < cutoff)
    c = information_classes(classify_games(panel, sources, season, week), exits,
                            final_regular_weeks(sources.games), config)
    assert c.height == 0 or int((c["season"] * 100 + c["week"]).max()) < cutoff, "game at or after the cutoff"
    priors = {pos: (float(v) if v is not None else 0.0) for pos, v in
              panel.group_by("position").agg(pl.col("xfpl").drop_nulls().mean()).iter_rows()}
    games = {}
    wanted = set(targets["gsis_id"].to_list())
    for g in (c.filter(pl.col("gsis_id").is_in(list(wanted))).sort(["season", "week"], descending=True)
              .select("gsis_id", "season", "week", "team", "xfpl", "f", "info_class").iter_rows(named=True)):
        games.setdefault(g["gsis_id"], []).append(g)
    rows = []
    for t in targets.select("gsis_id", "position", "team").iter_rows(named=True):
        r = _role_one(games.get(t["gsis_id"], []), season, priors.get(t["position"], 0.0), t["team"], config)
        rows.append({"gsis_id": t["gsis_id"], **r})
    return pl.DataFrame(rows, schema=schema)


def build_role_store(sources: Sources, store: pl.DataFrame, config: RoleConfig = RoleConfig(),
                     suffix: str = "") -> pl.DataFrame:
    """Role features for every (key, gsis_id) of a challenger feature store."""
    parts = []
    for (k,), g in store.group_by(["key"], maintain_order=True):
        f = role_features_for_week(sources, int(k) // 100, int(k) % 100, g.select("gsis_id", "position", "team"), config)
        parts.append(f.with_columns(pl.lit(int(k), dtype=store["key"].dtype).alias("key")))
    out = pl.concat(parts) if parts else pl.DataFrame()
    return out.rename({c: f"{c}{suffix}" for c in AW_COLUMNS}) if suffix else out


# ----------------------------------------------------------------------------- research challenger


@dataclass(frozen=True)
class Candidate:
    name: str
    replace: dict          # ifp-2 feature -> research feature
    add: tuple = ()        # extra research features
    selectable: bool = True


# Pre-registered before any development result: the ifp-2 feature set (C + flags + xfpl_eb +
# previous-season core, 36-week half-life) with the established workload swapped or extended.
# QB keeps ifp-2's QB model (= C) in every candidate. Columns with suffix _x come from the
# exposure-only estimator (RoleConfig(adaptive=False, team_change_factor=1)), _n from the
# no-exposure ablation, unsuffixed from the full adaptive estimator.
CANDIDATES = (
    Candidate("AW_E", {"xfpl_eb": "aw_eb_x"}),
    Candidate("AW_B", {"xfpl_eb": "aw_role"}),
    Candidate("AW_BX", {"xfpl_eb": "aw_role"}, add=("aw_r", "aw_eb")),
    Candidate("AW_B_noexp", {"xfpl_eb": "aw_role_n"}, selectable=False),     # ablation: exposure off
)
CONFIGS = {"": RoleConfig(), "_x": RoleConfig(adaptive=False, team_change_factor=1.0),
           "_n": RoleConfig(exposure=False)}
HALF_LIFE_WEEKS = 36.0
DEVELOPMENT, DIAGNOSTIC = (2021, 2022), ((2023, 2024), (2025, 2026))

# Pre-registered decision rules (committed before the development run).
AW_DECISION_RULES = {
    "selection": "development 2021-2022 only: per candidate and position the ridge penalty with the lowest "
                 "MAE on LAMBDA_GRID; then the selectable candidate with the lowest pooled development MAE",
    "development_gain": "selected - ifp2 pooled MAE week-clustered 95% CI upper bound < 0 on development",
    "diagnostic_note": "2023-2026 were inspected by the ifp-1.0.0/ifp-2 work: reported, never used to choose, and "
                       "not independent proof; they can only reject",
    "reject_if_any (2023-2026 pooled, same rows, vs ifp2)": {
        "pooled_mae_worse": "MAE diff > 0",
        "position_significantly_worse": "any position CI lower bound > 0",
        "rmse_or_bias_without_decision_gain": "(RMSE worse by > 0.5% or |bias| worse by > 0.15) and pair accuracy "
                                              "not better by >= 0.5 pp",
        "injury_return_mae": "after-injury-exit cohort MAE worse by > 2% (whatever its bias does)",
        "demotion_overprojection": "outcome-defined genuine demotions: bias lower (more over-projection) by > 0.25 "
                                   "and MAE worse",
        "low_sample_instability": "limited-history (< 3 games) MAE worse by > 2%",
    },
    "outcome": "no rule fires -> 'research_candidate' (shadow research only, not in the weekly pipeline); "
               "otherwise 'rejected'",
    "never": "no promotion, no weekly-pipeline integration, no change to ifp-1.0.0 or ifp-2.0.0",
}


def candidate_features(cand: Candidate, position: str, ifp2: ChallengerConfig) -> list[str]:
    if position == "QB":
        return ifp2.features("QB")
    feats = [cand.replace.get(f, f) for f in feature_set("FEQ")[position]]
    return feats + [f for f in cand.add if f not in feats]


def candidate_half_life(position: str, ifp2: ChallengerConfig) -> float | None:
    """QB keeps ifp-2's QB model exactly (no recency weighting); RB/WR/TE use HALF_LIFE_WEEKS."""
    return ifp2.positions["QB"].get("half_life_weeks") if position == "QB" else HALF_LIFE_WEEKS


def walk_forward(store: pl.DataFrame, weeks: list[tuple[int, int]], name: str, features: dict[str, list[str]],
                 lams: dict[str, list[float]], half_life_weeks: dict[str, float | None] | None = None) -> pl.DataFrame:
    """challenger.walk_forward with explicit feature lists: pred_<name>@<lam> per penalty;
    `half_life_weeks` per position (None: unweighted). Training rows strictly before the test week
    (asserted)."""
    half_life_weeks = half_life_weeks or {}
    out = []
    for season, week in weeks:
        k = key(season, week)
        for pos in POSITIONS:
            rows = store.filter((pl.col("key") == k) & (pl.col("position") == pos))
            if rows.height == 0:
                continue
            train = train_rows(store, k, pos)
            assert train.height == 0 or train["key"].max() < k, "training row at or after the test week"
            cols = {}
            for lam in lams[pos]:
                col = f"pred_{name}@{lam:g}"
                if train.height < MIN_TRAIN_ROWS:
                    cols[col] = np.full(rows.height, np.nan)
                    continue
                m = fit_linear(train, pos, features[pos], lam, half_life_weeks=half_life_weeks.get(pos), target_key=k,
                               name=name)
                cols[col] = np.where(rows["games_recent"].to_numpy() > 0, m.predict(rows), np.nan)
            out.append(rows.select("key", "gsis_id").with_columns(
                [pl.Series(c, v).fill_nan(None) for c, v in cols.items()]))
    return pl.concat(out, how="diagonal_relaxed") if out else pl.DataFrame()


# ----------------------------------------------------------------------------- evaluation


def attach_evaluation_columns(store: pl.DataFrame, sources: Sources) -> pl.DataFrame:
    """Columns for evaluation cohorts only (never model inputs): `_final_week` (schedule), and the
    outcome-side `_target_xfpl`, `_target_exit` (target game classified with all information) and
    `_next_xfpl` (the player's next appearance in the same season)."""
    final = final_regular_weeks(sources.games)
    full = classify_games(sources.panel, sources, 9999, 1).select(
        "season", "week", "gsis_id", "xfpl", (pl.col("gclass") == "injury_exit").alias("_target_exit"))
    full = full.sort("gsis_id", "season", "week").with_columns(
        pl.when(pl.col("season").shift(-1).over("gsis_id") == pl.col("season"))
          .then(pl.col("xfpl").shift(-1).over("gsis_id")).otherwise(None).alias("_next_xfpl"))
    return (store.join(full.rename({"xfpl": "_target_xfpl"}), on=["season", "week", "gsis_id"], how="left")
            .with_columns(pl.col("_target_exit").fill_null(False),
                          pl.col("season").replace_strict(final, default=None, return_dtype=pl.Int64)
                          .alias("_final_week")))


def cohorts(df: pl.DataFrame) -> dict[str, pl.Expr]:
    """challenger.cohorts plus the workload cohorts of this study. All pregame except the ones
    prefixed outcome_ (defined with the target week's realized usage; diagnostics only). Needs the
    evaluation columns attach_evaluation_columns adds."""
    base = ch_cohorts(df)
    out = {k: v for k, v in base.items() if k != "high_projection_ge15"}
    rb = pl.col("position") == "RB"
    out.update({
        "rb_starters_pool": rb & base["starters_pool"],
        "rb_high_volume": rb & (pl.col("h_xfpl_pg").fill_null(0) >= 15),
        "injury_return": (pl.col("last_injury_exit") == 1) | ((pl.col("inj_exits_recent") >= 1)
                                                                & (pl.col("weeks_since_last") >= 2)),
        "multiple_early_exits": pl.col("inj_exits_recent") >= 2,
        "unexplained_role_reduction": pl.col("last_role_low") == 1,
        "breakout_regime": pl.col("aw_streak") >= 2,
        "decline_regime": pl.col("aw_streak") <= -2,
        "week18_target": pl.col("week") == pl.col("_final_week"),
        "after_week18_low": pl.col("aw_last_class") == "week18_low",
        # target game well below the pregame established workload without being an injury exit, and the
        # next appearance still low: a sustained demotion (classified with all information, outcome side)
        "outcome_genuine_demotion": (pl.col("_target_xfpl") < 0.6 * pl.col("xfpl_eb")) & ~pl.col("_target_exit")
                                    & (pl.col("_next_xfpl") < 0.7 * pl.col("xfpl_eb")),
        "outcome_breakout": (pl.col("_target_xfpl") > 1.5 * pl.col("xfpl_eb"))
                            & (pl.col("_next_xfpl") > 1.3 * pl.col("xfpl_eb")) & (pl.col("xfpl_eb") >= 5),
    })
    return out


def compare(preds: pl.DataFrame, models: dict[str, str], baseline: str = "ifp2", seed: int = 20261015,
            resamples: int = 1000, exact: bool = False) -> dict:
    """Identical rows (every model non-null), per position and pooled, week-clustered CIs vs baseline,
    cohorts (MAE, bias, n) and Start/Sit pairs (accuracy and regret)."""
    p = preds.with_columns([pl.col(c).alias(f"pred_{n}") for n, c in models.items() if c != f"pred_{n}"])
    cols = [f"pred_{n}" for n in models]
    p = p.filter(pl.all_horizontal([pl.col(c).is_not_null() for c in cols]) & pl.col("label").is_not_null())
    flt = cohorts(p)
    out = {}
    for pos in [*POSITIONS, "ALL"]:
        g = p if pos == "ALL" else p.filter(pl.col("position") == pos)
        a = _arr(g, "label")
        rep = {"n": g.height, "weeks": g.select("key").n_unique(),
               "metrics": {n: metrics(a, _arr(g, f"pred_{n}"), exact) for n in models},
               "minus_baseline_mae": {n: week_bootstrap_mae_diff(g, f"pred_{n}", f"pred_{baseline}", resamples, seed, exact)
                                      for n in models if n != baseline}}
        if pos == "ALL":
            rep["cohorts"] = {}
            for cname, expr in flt.items():
                c = g.filter(expr.fill_null(False))
                ac = _arr(c, "label")
                rep["cohorts"][cname] = {"n": c.height, **{n: metrics(ac, _arr(c, f"pred_{n}"), exact) for n in models}}
                if c.height >= 30:
                    rep["cohorts"][cname]["minus_baseline_mae"] = {
                        n: week_bootstrap_mae_diff(c, f"pred_{n}", f"pred_{baseline}", resamples, seed, exact)
                        for n in models if n != baseline}
        out[pos] = rep
    out["pairs"] = pair_decisions(p, cols, exact=exact)
    return out


def decision(dev: dict, diag: dict, cand: str, base: str = "ifp2") -> dict:
    """Apply AW_DECISION_RULES. `dev` / `diag`: compare() outputs on development / pooled diagnostic."""
    d = dev["ALL"]["minus_baseline_mae"][cand]
    a = diag["ALL"]
    m, b = a["metrics"][cand], a["metrics"][base]
    pa = diag["pairs"]["all"][f"pred_{cand}"]["accuracy"]
    pb = diag["pairs"]["all"][f"pred_{base}"]["accuracy"]

    def coh(name):
        return a["cohorts"][name][cand], a["cohorts"][name][base]

    inj, inj_b = coh("injury_return")
    dem, dem_b = coh("outcome_genuine_demotion")
    low, low_b = coh("limited_history_lt3_games")
    rejects = {
        "pooled_mae_worse": a["minus_baseline_mae"][cand]["diff"] > 0,
        "position_significantly_worse": any(diag[p]["minus_baseline_mae"][cand]["ci95"][0] > 0 for p in POSITIONS),
        "rmse_or_bias_without_decision_gain": ((m["rmse"] > b["rmse"] * 1.005 or abs(m["bias"]) > abs(b["bias"]) + 0.15)
                                               and pa < pb + 0.005),
        "injury_return_mae": inj["mae"] > inj_b["mae"] * 1.02,
        "demotion_overprojection": dem["bias"] < dem_b["bias"] - 0.25 and dem["mae"] > dem_b["mae"],
        "low_sample_instability": low["mae"] > low_b["mae"] * 1.02,
    }
    dev_gain = d["ci95"] is not None and d["ci95"][1] < 0
    outcome = "research_candidate" if dev_gain and not any(rejects.values()) else "rejected"
    return {"candidate": cand, "development_gain": dev_gain, "development_diff": d, "rejections": rejects,
            "outcome": outcome}


def config_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def research_config(cand: Candidate, lams: dict[str, float], ifp2: ChallengerConfig, status: str = "research") -> dict:
    """The frozen research challenger (separate version; never the ifp-2 config). `status` records the
    AW_DECISION_RULES outcome ("rejected" / "research_candidate"); it is never a production status."""
    if status not in ("research", "rejected", "research_candidate"):
        raise ValueError(f"not a research status: {status}")
    cfg = {"version": AW_VERSION, "status": status, "based_on": {"ifp2_version": ifp2.version,
                                                                      "ifp2_config_hash": ifp2.hash()},
           "candidate": asdict(cand), "lam": lams,
           "half_life_weeks": {p: candidate_half_life(p, ifp2) for p in POSITIONS},
           "role_configs": {s: asdict(c) for s, c in CONFIGS.items()},
           "constants": {"EXPOSURE_CONFIDENCE": EXPOSURE_CONFIDENCE, "MIN_EXPOSURE": MIN_EXPOSURE,
                         "REGIME_GAMES": REGIME_GAMES, "MIN_REGIME_GAMES": MIN_REGIME_GAMES, "REGIME_DELTA": REGIME_DELTA, "REGIME_K": REGIME_K,
                         "TEAM_CHANGE_PRIOR_FACTOR": TEAM_CHANGE_PRIOR_FACTOR, "USAGE_PRIOR_GAMES": USAGE_PRIOR_GAMES,
                         "SHRINK_GAMES": SHRINK_GAMES},
           "features": {p: candidate_features(cand, p, ifp2) for p in POSITIONS}}
    cfg["hash"] = config_hash(cfg)
    return cfg


__all__ = ["AW_VERSION", "RoleConfig", "role_features_for_week", "build_role_store", "CANDIDATES",
           "AW_DECISION_RULES", "compare", "decision", "LAMBDA_GRID", "TRAIN_START", "recency_weights"]
