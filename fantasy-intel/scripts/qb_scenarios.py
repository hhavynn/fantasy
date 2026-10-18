"""Research only: calibrated starting-QB probabilities and two-QB scenario projections (ifp-ctx-p2).

See docs/research/qb_teammate_context.md, section 12. Nothing in the app imports this file (it
lives in scripts/ and is tested in backend/tests/test_qb_scenarios.py). It reads the frozen
ifp-ctx-p1 modules (app.analytics.roster_context / context_challenger) and never changes them:
p1's definition, features, outputs and ledgers are untouched.

What p2 changes relative to p1's QB group (everything else is ifp-2's spec, as in p1):

1. **Start probability instead of a committed starter.** Every plausible QB of a team-week is a
   candidate. A conditional logit (softmax within the team-week) on pregame evidence gives each a
   start probability. It is refitted walk-forward on strictly earlier team-weeks, labelled with the
   realized starter of those past games. p1's evidence class enters as features, so the logit
   calibrates p1's ordinal classes instead of replacing them.
2. **Week 1 / first game is no longer "unknown".** When the team has no game this season, the
   candidates are the QBs on its week-W roster (team membership only: the ACT/INA game-day
   distinction is not used, and practice-squad / reserve QBs are not candidates), and last season's
   starts for the team and last season's dropbacks carry the evidence.
3. **Scenario models are trained on realized scenarios and mixed with the probabilities.**
   * QB rows: the projection is fitted with `s_start` = "this QB started" (realized, training weeks
     only). A forecast mixes the start and no-start projections with pi = P(start | appears) =
     p / (p + (1 - p) r), where r is the walk-forward relief-appearance rate of non-starters (the
     label population is QBs who appeared).
   * RB/WR/TE rows: qb_epa_delta / qb_cpoe_delta / qb_changed are computed for the realized starter
     in training, and a forecast mixes the projections under the two most likely starters, weighted
     by their renormalized start probabilities.
   With a linear model the mixture equals plugging in the expected feature values. The 0 floor of
   the projection is the only nonlinearity, so it is done as an explicit mixture of projections.

Fixed before any p2 points backtest (constants below). No penalty or hyperparameter is searched.
"""

from __future__ import annotations

from collections import Counter

import numpy as np
import polars as pl

import _bootstrap  # noqa: F401

from app.analytics import roster_context as rc
from app.analytics.challenger import ChallengerConfig, fit_linear, train_rows
from app.analytics.context_challenger import GROUPS
from app.analytics.forecast_features import POSITIONS, Sources, key
from app.analytics.independent_projections import MIN_TRAIN_ROWS

P2_VERSION = "ifp-ctx-p2"
P2_STATUS = "research_only"
START_MODEL_VERSION = "qbstart-0.1.0"
START_TRAIN_START = 2020        # first season of start-model training team-weeks (as ifp-2's TRAIN_START)
START_L2 = 1.0                  # ridge on standardized coefficients: only guards against separation
SCENARIO_TOP_K = 2              # receivers: the two most likely starters
ROSTER_MEMBER = ("ACT", "INA")  # week-W roster membership (week 1 / first game only); status itself unused
DB_SCALE = 500.0                # dropbacks scale for the experience features
RELIEF_ATT_SCALE = 30.0

START_FEATURES = [
    "prev_start",                 # started the team's last game this season
    "season_start_share",         # starts for the team this season / team games this season
    "prev_season_start_share",    # starts for the team last season / team games last season
    "prev_season_db",             # dropbacks last season, any team / 500, capped at 1.5
    "first_game_x_prev_share",    # team has no game yet this season x prev_season_start_share
    "first_game_x_prev_db",       # team has no game yet this season x prev_season_db
    "relief_att",                 # attempts in the team's last game when he did not start / 30, capped at 1
    "takeover",                   # out-threw the starter with >= 10 attempts last game
    "p1_expected",                # p1's expected starter (any class)
    "p1_expected_probable",       # ... with class probable
    "p1_expected_uncertain",      # ... with class uncertain
    "p1_alt",                     # p1's alternative QB
    "questionable", "doubtful", "unavailable",   # week-W report / roster before W (out, inactive)
    "qb_epa_rel",                 # shrunk EPA/dropback minus the replacement-level prior
    "no_history",                 # no passing history in the profile window
]

# QB-group features per position. QB: s_start is the realized start in training and the scenario
# value at forecast time. Others: p1's names, recomputed for the realized / scenario starter.
QB2_GROUP = {"QB": ["s_start", "qb_self_epa"], "RB": list(GROUPS["qb"]["RB"]), "WR": list(GROUPS["qb"]["WR"]),
             "TE": list(GROUPS["qb"]["TE"])}
RECEIVER_QB_FEATURES = ["qb_epa_delta", "qb_cpoe_delta", "qb_changed"]
assert all(QB2_GROUP[p] == RECEIVER_QB_FEATURES for p in ("RB", "WR", "TE"))

# S2: the scenario QB group. SC2: S2 + p1's teammate group (unchanged). S2h (ablation, descriptive only):
# the same models with a hard top-1 starter instead of the mixture.
P2_CANDIDATES = {"S2": ("qb2",), "SC2": ("qb2", "tm")}
ABLATIONS = {"S2h": ("qb2",)}
MECHANISM_GROUPS = {"qb2": "qb", "tm": "tm"}

P2_RULES = {
    "pre_registration": "start-model features, penalty, candidate sets, scenario rule and the selection / confirmation "
                        "rules below were fixed in this file before the first p2 points backtest; the start model's "
                        "calibration was inspected first (it uses no points labels)",
    "selection": "development 2021-2022 only, per position, candidates S2 and SC2, exactly p1's rule: MAE(candidate) - "
                 "MAE(ifp2) <= -0.010 with week-clustered 95% CI upper < 0 and |bias| not worse by more than 0.15; the "
                 "lowest-MAE eligible candidate is adopted, else ifp2. The composite is 'ctx2'.",
    "confirmation": "2023-2024, p1's checks c1-c7 on the adopted positions (c7: changed expected QB cohort for qb2; top "
                    "target competitor out / RB1 out for tm)",
    "extra_bias_check": "reported, not gating: |bias| guard in every window and on weeks 1-4",
    "exploratory": "2025-2026 reported only (untimestamped injury reports)",
    "honesty": "p2 was designed after the p1 bias decomposition, which looked at all three windows, and 2023-24 has now "
               "been evaluated by this study family three times: a pass is a reason to collect prospective evidence, "
               "never a promotion",
}


# ----------------------------------------------------------------------------- candidates


def _status(st, pid: str, team: str, roster_member: bool) -> str:
    """available | questionable | doubtful | out | inactive | gone. A week-W roster member (first
    game only) is on the team by that evidence; only the week-W report can then lower his status."""
    if not roster_member:
        return st.availability(pid, team)
    if st.report is not None:
        s = (st.report.get(pid) or "").strip().lower()
        if s in ("out", "doubtful", "questionable"):
            return s
    return "available"


def start_candidates(sources: Sources, ctx: rc.ContextSources, season: int, week: int,
                     teams: list[str] | None = None) -> pl.DataFrame:
    """One row per (team, candidate QB) for week (S, W) with START_FEATURES, the candidate's shrunk
    profile (prof_epa, prof_cpoe), and two postgame columns used ONLY for training and evaluation:
    `started` (null when the team's realized starter is unknown) and `appeared`.

    Pregame inputs: games strictly before W, the week-W injury report, roster weeks before W, and
    (only when the team has no game yet this season) week-W roster team membership."""
    st = rc._state(sources, season, week, "week")
    past = ctx.starters.filter((pl.col("season") == season) & (pl.col("week") < week)).sort("week")
    if teams is None:     # the week's schedule (a pregame fact)
        teams = sorted(set(ctx.starters.filter((pl.col("season") == season) & (pl.col("week") == week))["team"]
                           .to_list()))
    exp = rc.expected_starters(sources, ctx, season, week, teams, "week", state=st)
    profiles, prior = rc.qb_profiles(ctx, season, week)
    games_this = Counter(past["team"].to_list())
    starts = Counter((t, q) for t, q in past.select("team", "qb_id").iter_rows() if q is not None)
    last = {t: (w, q) for t, w, q in past.select("team", "week", "qb_id").iter_rows()}
    prevs = ctx.starters.filter(pl.col("season") == season - 1)
    games_prev = Counter(prevs["team"].to_list())
    starts_prev = Counter((t, q) for t, q in prevs.select("team", "qb_id").iter_rows() if q is not None)
    qs = ctx.qb_stats
    db_prev = Counter()
    for gid, a, s in qs.filter(pl.col("season") == season - 1).select("gsis_id", "attempts", "sacks").iter_rows():
        db_prev[gid] += (a or 0.0) + (s or 0.0)
    cur = qs.filter((pl.col("season") == season) & (pl.col("week") < week))
    panel_qb = sources.panel.filter((pl.col("season") == season) & (pl.col("week") < week)
                                    & (pl.col("position") == "QB"))
    roster_prev: dict[str, set] = {}
    week_roster: dict[str, set] = {}
    if sources.rosters is not None:
        r = sources.rosters.filter((pl.col("season") == season) & (pl.col("position") == "QB"))
        for gid in set(r.filter(pl.col("week") < week)["gsis_id"].to_list()):
            t, s = st.roster[gid]          # the latest roster week before W (any position row of that QB)
            if s in ROSTER_MEMBER:
                roster_prev.setdefault(t, set()).add(gid)
        for gid, t in r.filter((pl.col("week") == week) & pl.col("status").is_in(list(ROSTER_MEMBER))).select(
                "gsis_id", "team").iter_rows():
            week_roster.setdefault(t, set()).add(gid)
    actual = dict(ctx.starters.filter((pl.col("season") == season) & (pl.col("week") == week))
                  .select("team", "qb_id").iter_rows())
    played = sources.panel.filter((pl.col("season") == season) & (pl.col("week") == week))
    appeared = set(zip(played["gsis_id"].to_list(), played["team"].to_list()))

    rows = []
    for team in teams:
        first_game = games_this[team] == 0
        if first_game:
            pool = set(week_roster.get(team, ()))
            member = True
        else:
            pool = (set(panel_qb.filter(pl.col("team") == team)["gsis_id"].to_list())
                    | set(cur.filter(pl.col("team") == team)["gsis_id"].to_list())
                    | roster_prev.get(team, set()))
            member = False
        lw, prev = last.get(team, (None, None))
        if prev is not None:
            pool.add(prev)
        att = {}
        if lw is not None:
            att = {g: (a or 0.0) for g, a in cur.filter((pl.col("week") == lw) & (pl.col("team") == team))
                   .select("gsis_id", "attempts").iter_rows()}
        e = exp.get(team, {})
        act = actual.get(team)
        for g in sorted(pool):
            status = _status(st, g, team, member)
            if status == "gone":
                continue
            prof = rc.profile_of(profiles, prior, g)
            share_prev = starts_prev[(team, g)] / games_prev[team] if games_prev[team] else 0.0
            dbp = min(db_prev[g] / DB_SCALE, 1.5)
            relief = 0.0 if g == prev else min(att.get(g, 0.0) / RELIEF_ATT_SCALE, 1.0)
            is_exp = g == e.get("expected_qb_id")
            rows.append({
                "season": season, "week": week, "key": key(season, week), "team": team, "gsis_id": g,
                "prev_start": float(g == prev), "season_start_share": starts[(team, g)] / games_this[team]
                if games_this[team] else 0.0, "prev_season_start_share": share_prev, "prev_season_db": dbp,
                "first_game_x_prev_share": share_prev if first_game else 0.0,
                "first_game_x_prev_db": dbp if first_game else 0.0, "relief_att": relief,
                "takeover": float(g != prev and att.get(g, 0.0) >= rc.TAKEOVER_ATTEMPTS
                                  and att.get(g, 0.0) > att.get(prev, 0.0)),
                "p1_expected": float(is_exp),
                "p1_expected_probable": float(is_exp and e.get("qb_confidence") == "probable"),
                "p1_expected_uncertain": float(is_exp and e.get("qb_confidence") == "uncertain"),
                "p1_alt": float(g == e.get("alt_qb_id")),
                "questionable": float(status == "questionable"), "doubtful": float(status == "doubtful"),
                "unavailable": float(status in ("out", "inactive")),
                "qb_epa_rel": prof["epa"] - prior["epa"], "no_history": float(prof["dropbacks"] == 0),
                "prof_epa": prof["epa"], "prof_cpoe": prof["cpoe"], "first_game": first_game,
                "p1_confidence": e.get("qb_confidence", "unknown"),
                "started": None if act is None else float(g == act), "appeared": float((g, team) in appeared),
            })
    schema = {"season": pl.Int64, "week": pl.Int64, "key": pl.Int64, "team": pl.String, "gsis_id": pl.String,
              **{f: pl.Float64 for f in START_FEATURES}, "prof_epa": pl.Float64, "prof_cpoe": pl.Float64,
              "first_game": pl.Boolean, "p1_confidence": pl.String, "started": pl.Float64, "appeared": pl.Float64}
    return pl.DataFrame(rows, schema=schema, orient="row") if rows else pl.DataFrame(schema=schema)


def realized_and_recent(sources: Sources, ctx: rc.ContextSources, season: int, week: int,
                        players: list[str]) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(team frame, player frame) for week (S, W). Team frame: the REALIZED starter of each team's
    week-W game and his pregame profile (training labels only). Player frame: each player's recent
    starting-QB environment (p1's definition: last <= 5 games, as-of-W profiles)."""
    profiles, prior = rc.qb_profiles(ctx, season, week)
    act = ctx.starters.filter((pl.col("season") == season) & (pl.col("week") == week)).unique("team", keep="first")
    trows = []
    for t, q in act.select("team", "qb_id").iter_rows():
        prof = rc.profile_of(profiles, prior, q)
        trows.append({"key": key(season, week), "team": t, "act_qb_id": q, "act_epa": prof and prof["epa"],
                      "act_cpoe": prof and prof["cpoe"]})
    recent = rc._recent_qb_env(sources, ctx, season, week, profiles, prior)
    prows = [{"key": key(season, week), "gsis_id": g, "rec_primary": recent.get(g, {}).get("primary"),
              "rec_epa": recent.get(g, {}).get("epa"), "rec_cpoe": recent.get(g, {}).get("cpoe")} for g in players]
    ts = {"key": pl.Int64, "team": pl.String, "act_qb_id": pl.String, "act_epa": pl.Float64, "act_cpoe": pl.Float64}
    ps = {"key": pl.Int64, "gsis_id": pl.String, "rec_primary": pl.String, "rec_epa": pl.Float64, "rec_cpoe": pl.Float64}
    return pl.DataFrame(trows, schema=ts, orient="row"), pl.DataFrame(prows, schema=ps, orient="row")


# ----------------------------------------------------------------------------- start model


def _groups(df: pl.DataFrame) -> np.ndarray:
    """Dense group index per row for (key, team); rows must be sorted by (key, team)."""
    k = df["key"].to_numpy()
    t = df["team"].to_numpy()
    new = np.ones(len(k), dtype=bool)
    new[1:] = (k[1:] != k[:-1]) | (t[1:] != t[:-1])
    return np.cumsum(new) - 1


def _softmax(s: np.ndarray, gid: np.ndarray, n: int) -> np.ndarray:
    m = np.full(n, -np.inf)
    np.maximum.at(m, gid, s)
    e = np.exp(s - m[gid])
    den = np.zeros(n)
    np.add.at(den, gid, e)
    return e / den[gid]


def fit_start_model(c: pl.DataFrame, l2: float = START_L2, features: list[str] = START_FEATURES) -> dict:
    """Conditional logit on candidate rows (team-weeks whose realized starter is exactly one of the
    candidates; others are excluded). Standardized features, ridge on coefficients, Newton steps."""
    c = c.filter(pl.col("started").is_not_null()).sort("key", "team", "gsis_id")
    ok = c.group_by("key", "team").agg(pl.col("started").sum().alias("n1")).filter(pl.col("n1") == 1)
    c = c.join(ok.select("key", "team"), on=["key", "team"], how="semi").sort("key", "team", "gsis_id")
    if c.height == 0:
        return {"features": features, "mean": [0.0] * len(features), "std": [1.0] * len(features),
                "beta": [0.0] * len(features), "groups": 0, "version": START_MODEL_VERSION}
    x = c.select(features).to_numpy().astype(float)
    mu, sd = x.mean(0), x.std(0)
    sd[sd < 1e-9] = 1.0
    z = (x - mu) / sd
    y = c["started"].to_numpy()
    gid = _groups(c)
    n = int(gid.max()) + 1
    beta = np.zeros(z.shape[1])
    for _ in range(100):
        p = _softmax(z @ beta, gid, n)
        xbar = np.zeros((n, z.shape[1]))
        np.add.at(xbar, gid, p[:, None] * z)
        grad = z[y == 1].sum(0) - xbar.sum(0) - l2 * beta
        hess = (z.T @ (p[:, None] * z)) - xbar.T @ xbar + l2 * np.eye(z.shape[1])
        step = np.linalg.solve(hess, grad)
        beta += step
        if np.max(np.abs(step)) < 1e-8:
            break
    return {"features": list(features), "mean": mu.tolist(), "std": sd.tolist(), "beta": beta.tolist(), "groups": n,
            "version": START_MODEL_VERSION}


def predict_start(model: dict, c: pl.DataFrame) -> pl.DataFrame:
    """Adds p_start (softmax within each team-week; sums to 1 over its candidates)."""
    if c.height == 0:
        return c.with_columns(pl.lit(None, pl.Float64).alias("p_start"))
    c = c.sort("key", "team", "gsis_id")
    z = (c.select(model["features"]).to_numpy().astype(float) - np.array(model["mean"])) / np.array(model["std"])
    gid = _groups(c)
    return c.with_columns(pl.Series("p_start", _softmax(z @ np.array(model["beta"]), gid, int(gid.max()) + 1)))


def relief_rates(c: pl.DataFrame) -> dict[int, float]:
    """P(appears | did not start) by the candidate's p_start rank in his team-week (1, 2, 3 = 3+),
    from training rows (p_start in-sample). Used to turn p_start into P(start | appears)."""
    r = (c.filter(pl.col("started").is_not_null())
         .with_columns(pl.col("p_start").rank("ordinal", descending=True).over("key", "team").clip(1, 3)
                       .cast(pl.Int64).alias("rank"))
         .filter(pl.col("started") == 0).group_by("rank").agg(pl.col("appeared").mean()))
    out = {1: 0.5, 2: 0.1, 3: 0.02}   # only used if a rank has no training rows (never in the real data)
    out.update({int(k): float(v) for k, v in r.iter_rows()})
    return out


def pi_given_appearance(p: float | None, r: float) -> float | None:
    """P(start | appears) = p / (p + (1 - p) r): appearing is either starting or a relief/garbage-time
    appearance. p = 1 -> 1, p = 0 -> 0, increasing in p, decreasing in r."""
    if p is None:
        return None
    den = p + (1.0 - p) * r
    return 0.0 if den <= 0 else p / den


def with_pi(c: pl.DataFrame, rates: dict[int, float]) -> pl.DataFrame:
    c = c.with_columns(pl.col("p_start").rank("ordinal", descending=True).over("key", "team").clip(1, 3)
                       .cast(pl.Int64).alias("rank"))
    p, rk = c["p_start"].to_list(), c["rank"].to_list()
    return c.with_columns(pl.Series("pi_start", [pi_given_appearance(a, rates[b]) for a, b in zip(p, rk)],
                                    dtype=pl.Float64))


def walk_forward_start(cands: pl.DataFrame, weeks: list[tuple[int, int]],
                       train_start: int = START_TRAIN_START) -> tuple[pl.DataFrame, dict]:
    """p_start and pi_start for every candidate of `weeks`, each week from a model fitted on
    strictly earlier team-weeks (asserted). Also returns the fitted models by week key."""
    out, models = [], {}
    for season, week in weeks:
        k = key(season, week)
        train = cands.filter((pl.col("key") < k) & (pl.col("season") >= train_start))
        assert train.height == 0 or train["key"].max() < k, "start-model training row at or after the test week"
        test = cands.filter(pl.col("key") == k)
        if test.height == 0:
            continue
        m = fit_start_model(train)
        rates = relief_rates(predict_start(m, train)) if train.height else relief_rates(train.with_columns(
            pl.lit(0.0).alias("p_start")))
        models[k] = {**m, "relief_rates": rates, "train_team_weeks": m["groups"]}
        out.append(with_pi(predict_start(m, test), rates))
    return (pl.concat(out) if out else cands.head(0)), models


# ----------------------------------------------------------------------------- scenario features


def scenario_columns(store: pl.DataFrame, cands: pl.DataFrame, teams: pl.DataFrame,
                     recent: pl.DataFrame) -> pl.DataFrame:
    """Adds the p2 columns to `store` rows (key, gsis_id, team, position):

    * training (realized starter): s_start (QB rows), a_qb_epa_delta / a_qb_cpoe_delta / a_qb_changed;
    * forecast: pi_start (QB rows; 0 for a QB who is not a candidate of a team-week that has
      candidates; null when the team-week has none) and pi_hard (1 for the top candidate);
      for receivers, scenarios 1..K (the K most likely starters) with weights w1..wK (renormalized
      p_start) and features s{k}_qb_epa_delta / s{k}_qb_cpoe_delta / s{k}_qb_changed.

    `cands` needs p_start / pi_start (from walk_forward_start)."""
    st = store.join(teams, on=["key", "team"], how="left").join(recent, on=["key", "gsis_id"], how="left")
    st = st.with_columns(
        pl.when(pl.col("act_qb_id").is_null()).then(None).otherwise((pl.col("gsis_id") == pl.col("act_qb_id"))
                                                                    .cast(pl.Float64)).alias("s_start"),
        (pl.col("act_epa") - pl.col("rec_epa")).alias("a_qb_epa_delta"),
        (pl.col("act_cpoe") - pl.col("rec_cpoe")).alias("a_qb_cpoe_delta"),
        pl.when(pl.col("act_qb_id").is_null() | pl.col("rec_primary").is_null()).then(None)
          .otherwise((pl.col("act_qb_id") != pl.col("rec_primary")).cast(pl.Float64)).alias("a_qb_changed"))
    if "pi_start" in cands.columns:
        has = cands.select("key", "team").unique().with_columns(pl.lit(True).alias("_has"))
        top = cands.sort("p_start", "gsis_id", descending=[True, False]).unique(["key", "team"], keep="first")
        st = (st.join(has, on=["key", "team"], how="left")
              .join(cands.select("key", "team", "gsis_id", "pi_start"), on=["key", "team", "gsis_id"], how="left")
              .join(top.select("key", "team", pl.col("gsis_id").alias("_top")), on=["key", "team"], how="left")
              .with_columns(pl.when(pl.col("_has").is_null()).then(None).otherwise(pl.col("pi_start").fill_null(0.0))
                            .alias("pi_start"),
                            pl.when(pl.col("_has").is_null()).then(None).otherwise(
                                (pl.col("gsis_id") == pl.col("_top")).cast(pl.Float64)).alias("pi_hard"))
              .drop("_has", "_top"))
        ranked = (cands.sort(["key", "team", "p_start", "gsis_id"], descending=[False, False, True, False])
                  .group_by("key", "team", maintain_order=True).head(SCENARIO_TOP_K)
                  .with_columns(pl.int_range(pl.len()).over("key", "team").alias("k"),
                                (pl.col("p_start") / pl.col("p_start").sum().over("key", "team")).alias("w")))
        for k in range(SCENARIO_TOP_K):
            sk = ranked.filter(pl.col("k") == k).select("key", "team", pl.col("gsis_id").alias(f"s{k + 1}_qb"),
                                                        pl.col("prof_epa").alias(f"_e{k}"),
                                                        pl.col("prof_cpoe").alias(f"_c{k}"),
                                                        pl.col("w").alias(f"w{k + 1}"))
            st = st.join(sk, on=["key", "team"], how="left").with_columns(
                (pl.col(f"_e{k}") - pl.col("rec_epa")).alias(f"s{k + 1}_qb_epa_delta"),
                (pl.col(f"_c{k}") - pl.col("rec_cpoe")).alias(f"s{k + 1}_qb_cpoe_delta"),
                pl.when(pl.col(f"s{k + 1}_qb").is_null() | pl.col("rec_primary").is_null()).then(None)
                  .otherwise((pl.col(f"s{k + 1}_qb") != pl.col("rec_primary")).cast(pl.Float64))
                  .alias(f"s{k + 1}_qb_changed")).drop(f"_e{k}", f"_c{k}")
    return st


# ----------------------------------------------------------------------------- fitting and mixing


def p2_features(cfg: ChallengerConfig, position: str, candidate: str | None) -> list[str]:
    """ifp-2's features plus the candidate's groups (qb2 = QB2_GROUP, tm = p1's teammate group)."""
    feats = list(cfg.features(position))
    groups = {**P2_CANDIDATES, **ABLATIONS}.get(candidate or "", ())
    for g in groups:
        add = QB2_GROUP[position] if g == "qb2" else GROUPS[g][position]
        feats += [f for f in add if f not in feats]
    return feats


def training_frame(train: pl.DataFrame, position: str) -> pl.DataFrame:
    """Training rows with the QB group set to the REALIZED starter's values."""
    if position == "QB":
        return train      # s_start already holds the realized start
    return train.with_columns([pl.col(f"a_{f}").alias(f) for f in RECEIVER_QB_FEATURES])


def mix_predict(model, rows: pl.DataFrame, position: str, hard: bool = False) -> np.ndarray:
    """Scenario mixture. QB rows: pi x f(start) + (1 - pi) x f(no start); null pi -> NaN (the caller
    falls back to ifp-2). Receivers: sum_k w_k x f(scenario k); a team-week without candidates uses
    null QB features (the model's missing-value path, as in p1)."""
    if position == "QB":
        pi = rows["pi_hard" if hard else "pi_start"].cast(pl.Float64).fill_null(np.nan).to_numpy()
        f1 = model.predict(rows.with_columns(pl.lit(1.0).alias("s_start")))
        f0 = model.predict(rows.with_columns(pl.lit(0.0).alias("s_start")))
        return pi * f1 + (1.0 - pi) * f0
    k_used = 1 if hard else SCENARIO_TOP_K
    total = np.zeros(rows.height)
    wsum = np.zeros(rows.height)
    for k in range(1, k_used + 1):
        w = (rows[f"w{k}"].cast(pl.Float64).fill_null(0.0).to_numpy() if f"w{k}" in rows.columns
             else np.zeros(rows.height))
        if hard:
            w = np.where(w > 0, 1.0, 0.0)
        f = model.predict(rows.with_columns([pl.col(f"s{k}_{c}").alias(c) for c in RECEIVER_QB_FEATURES]))
        total += w * f
        wsum += w
    none = model.predict(rows.with_columns([pl.lit(None, pl.Float64).alias(c) for c in RECEIVER_QB_FEATURES]))
    return np.where(wsum > 0, total / np.where(wsum > 0, wsum, 1.0), none)


def fit_p2(train: pl.DataFrame, cfg: ChallengerConfig, position: str, candidate: str, target_key: int):
    spec = cfg.positions[position]
    return fit_linear(training_frame(train, position), position, p2_features(cfg, position, candidate), spec["lam"],
                      half_life_weeks=spec.get("half_life_weeks"), target_key=target_key, name=f"ctx2_{candidate}")


def walk_forward_p2(store: pl.DataFrame, weeks: list[tuple[int, int]], cfg: ChallengerConfig,
                    candidates=tuple(P2_CANDIDATES), ablations=tuple(ABLATIONS),
                    per_position: dict[str, str] | None = None) -> pl.DataFrame:
    """pred_<candidate> (and pred_ctx2 for `per_position`) per store row of `weeks`. Same training
    rows as ifp-2 / p1 (challenger.train_rows: strictly earlier weeks, asserted). `store` needs the
    scenario_columns. A QB row whose team-week has no candidate gets ifp-2's prediction."""
    from app.analytics.context_challenger import fit_candidate

    out = []
    for season, week in weeks:
        k = key(season, week)
        for pos in POSITIONS:
            rows = store.filter((pl.col("key") == k) & (pl.col("position") == pos))
            if rows.height == 0:
                continue
            train = train_rows(store, k, pos)
            assert train.height == 0 or train["key"].max() < k, "training row at or after the test week"
            ok = rows["games_recent"].fill_null(0).to_numpy() > 0
            names = list(candidates) + list(ablations) + (["ctx2"] if per_position is not None else [])
            cols = {}
            if train.height < MIN_TRAIN_ROWS:
                cols = {f"pred_{n}": np.full(rows.height, np.nan) for n in names}
            else:
                base = fit_candidate(train, cfg, pos, None, k).predict(rows)
                fitted = {}
                for n in names:
                    cand = per_position.get(pos, "ifp2") if n == "ctx2" else n
                    if cand == "ifp2":
                        cols[f"pred_{n}"] = np.where(ok, base, np.nan)
                        continue
                    gcand = cand if cand in P2_CANDIDATES else "S2"     # S2h reuses S2's fit
                    if gcand not in fitted:
                        fitted[gcand] = fit_p2(train, cfg, pos, gcand, k)
                    pred = mix_predict(fitted[gcand], rows, pos, hard=(cand in ABLATIONS))
                    pred = np.where(np.isnan(pred), base, pred)
                    cols[f"pred_{n}"] = np.where(ok, pred, np.nan)
            out.append(rows.select("key", "gsis_id").with_columns(
                [pl.Series(c, v).fill_nan(None) for c, v in cols.items()]))
    return pl.concat(out, how="diagonal_relaxed") if out else pl.DataFrame()


__all__ = ["P2_VERSION", "START_FEATURES", "P2_CANDIDATES", "P2_RULES", "start_candidates", "realized_and_recent",
           "fit_start_model", "predict_start", "relief_rates", "pi_given_appearance", "walk_forward_start",
           "scenario_columns", "p2_features", "mix_predict", "walk_forward_p2"]
