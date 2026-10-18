"""Research only: returning high-volume teammates (WR1 / TE1 / RB1 coming back) on top of ifp-2.

Nothing in the app imports this module (tests/test_returning_context.py). It is a standalone
challenger (`ifp-ret`), separate from ifp-ctx and ifp-ctx-p1, which it neither imports into nor
changes. ifp-1.0.0, ifp-2.0.0, the model registry, production runs, ledgers, Start/Sit, trades and
waivers are untouched. See docs/research/returning_teammate_features.md.

Why. The ifp-ctx study found that the *returning* half of its teammate group was its most consistent
signal (top target competitor returning: WR MAE -0.11 to -0.19 in every window), while the vacated
half (redistributing an absent player's volume) improved the mean but worsened MAE. This candidate
keeps only the returning half and restricts it to the teammates that matter: the team's WR1, TE1
and RB1 (its highest share-when-playing player at each position).

Definitions (fixed before the first backtest; constants are never tuned):
  * Role holders. Per team, among players not off the team, the WR and the TE with the highest
    target share when playing and the RB with the highest carry share when playing. "Share when
    playing" is over the player's last ROLE_WINDOW games for the team, current season first, then
    the previous season (so a WR1 hurt in week 2 keeps his role). >= ROLE_MIN_GAMES games needed.
    A player with no current-season game for the team needs current-season roster evidence.
  * Returning. A role holder j (not the player himself) is returning for player g when he missed
    some of g's window games (the team games g played among the team's last TEAM_WINDOW, current
    season, before W, as in roster_context) and is expected back in W:
      - available or Questionable on the W report and not off the team. If he missed the team's
        last game he needs pregame evidence: listed on the W report or an active roster status
        (ACT/INA) before W. Unlike roster_context there is NO staleness cut-off: a long absence is
        the case this candidate is for.
      - or, coming off a reserve list: non-active roster status before W but listed on the W report
        without an Out/Doubtful designation (a practice window opened). Flagged `ret_from_reserve`.
  * Features (g's row). `ret_tgt_reclaim` = sum over returning role holders of target share when
    playing x fraction of g's window games missed (`ret_car_reclaim` for carries).
    `ret_tgt_loss_pg` = (s - s / (1 + reclaim)) x team targets per game over g's window, s = g's
    window target share: the targets per game g cedes if the returners take back their share in
    proportion (`ret_car_loss_pg` for carries). Vacated volume is NOT used.
  * Without a week-W report (report_mode "week" and none ingested) every feature is null, never 0.
    No returning role holder is a measured 0.

Leakage contract (tested): only games strictly before (S, W), roster statuses of weeks before W and
the pregame week-W injury report (forecast_features.load_injuries drops rows modified at or after
kickoff where nflverse records date_modified, 2019-2024; 2025+ rows are untimestamped). The week-W
roster status (ACT/INA game-day decision) and box score are never read.

No fantasy-point adjustment is made here: any effect on points is a ridge coefficient fitted
walk-forward on ifp-2's own spec (features, frozen penalty, recency weights).
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np
import polars as pl

from .challenger import ChallengerConfig, fit_linear, train_rows
from .forecast_features import POSITIONS, Sources, key
from .independent_projections import MIN_TRAIN_ROWS, RidgeModel
from .roster_context import ACTIVE_ROSTER, TEAM_WINDOW, UNAVAILABLE, _state

RET_VERSION = "ret-0.1.0"           # features
RET_CHALLENGER_VERSION = "ifp-ret-0.1.0"
RET_STATUS = "research_only"
WINDOWS = {"development": (2021, 2022), "confirmation": (2023, 2024), "exploratory": (2025, 2026)}

ROLE_WINDOW = 5        # a teammate's share when playing: his last <= 5 games for the team
ROLE_MIN_GAMES = 2     # games needed to hold a role
ROLES = {"WR": 0, "TE": 0, "RB": 1}   # role position -> index of the opportunity it is ranked on (0 targets, 1 carries)

RET_DOCS: dict[str, str] = {
    "ret_tgt_reclaim": "Target share returning role holders (WR1/TE1/RB1, not the player) take back: their target share "
                       "when playing x the fraction of the player's window games they missed",
    "ret_car_reclaim": "ret_tgt_reclaim for carries",
    "ret_tgt_loss_pg": "Targets per game the player cedes to returning role holders: (s - s / (1 + ret_tgt_reclaim)) x "
                       "team targets per game, s = the player's window target share",
    "ret_car_loss_pg": "ret_tgt_loss_pg for carries",
    "ret_wr1": "1 if the team's WR1 (not the player) is returning (cohort flag, not a feature)",
    "ret_te1": "1 if the team's TE1 is returning (cohort flag)",
    "ret_rb1": "1 if the team's RB1 is returning (cohort flag)",
    "ret_from_reserve": "1 if a returning role holder comes off a reserve list on report evidence (cohort flag)",
    "ret_back_now": "1 if a returning role holder missed the team's last game, i.e. comes back this week (cohort flag)",
    "ret_missed_max": "Largest fraction of the player's window games missed by a returning role holder (cohort flag)",
    "ret_returners": "Comma-joined gsis ids of the returning role holders (explanations only)",
}
FEATURE_COLUMNS = ("ret_tgt_reclaim", "ret_car_reclaim", "ret_tgt_loss_pg", "ret_car_loss_pg")
FLAG_COLUMNS = ("ret_wr1", "ret_te1", "ret_rb1", "ret_from_reserve", "ret_back_now")
RET_COLUMNS = (*FEATURE_COLUMNS, *FLAG_COLUMNS, "ret_missed_max", "ret_returners")

# The single candidate group per position (fixed before any backtest). Few features on purpose.
GROUP: dict[str, list[str]] = {
    "QB": ["ret_tgt_reclaim"],
    "RB": ["ret_car_loss_pg", "ret_tgt_loss_pg", "ret_car_reclaim"],
    "WR": ["ret_tgt_loss_pg", "ret_tgt_reclaim"],
    "TE": ["ret_tgt_loss_pg", "ret_tgt_reclaim"],
}
CANDIDATE = "R"

# Pre-registered (committed with the code, before the first development run). Same thresholds as
# context_challenger.DECISION_RULES; the mechanism cohort is the one this group exists for.
DECISION_RULES = {
    "selection": "development 2021-2022 only, per position: R is adopted if MAE(R) - MAE(ifp2) <= -0.010 with a "
                 "week-clustered bootstrap 95% CI upper bound < 0 and |bias| not worse by more than 0.15; otherwise the "
                 "position keeps ifp2. The composite of the per-position choices is 'ret'.",
    "confirmation": "2023-2024, run once, frozen composite vs ifp2 on identical rows, adopted positions only: (1) pooled "
                    "MAE diff < 0 with CI upper < 0; (2) no adopted position significantly worse (CI lower > 0); "
                    "(3) pooled RMSE not worse by > 0.5%; (4) |bias| not worse by > 0.15; (5) starters-pool MAE not "
                    "worse by > 1%; (6) Start/Sit pair accuracy not lower by > 0.5 pp; (7) mechanism: the MAE "
                    "difference is < 0 on each adopted position's cohort of rows whose role-holder competitor comes back this "
                    "week (ret_back_now: missed the team's last game). ret_any is reported, not gating.",
    "outcome": "all pass -> 'prospective_shadow' (a recommendation to pre-register a frozen pregame ledger; it changes "
               "nothing by itself); any fail, or nothing adopted -> 'retain_baseline'.",
    "exploratory": "2025-2026 replay is reported only: already inspected by earlier work, and 2025-26 injury reports "
                   "carry no modification timestamp (retrospective).",
    "descriptive": "ifp-ctx candidate B (full teammate group) is reported alongside on the same rows as a comparator; "
                   "it selects nothing.",
    "never": "no promotion, no registry change, no change to ifp-ctx-p1; historical replays never establish promotion "
             "readiness",
}
SELECTION_MAX_DIFF = -0.010
BIAS_TOLERANCE = 0.15
MECHANISM_COHORT = "ret_back_now"


# ----------------------------------------------------------------------------- features


def _schema() -> dict:
    s = {c: pl.Float64 for c in (*FEATURE_COLUMNS, "ret_missed_max")}
    s.update({c: pl.Int8 for c in FLAG_COLUMNS})
    return {"gsis_id": pl.String, **s, "ret_returners": pl.String}


def returning_for_week(sources: Sources, season: int, week: int, targets: pl.DataFrame,
                       report_mode: str = "week") -> pl.DataFrame:
    """Returning-role-holder features for `targets` (gsis_id, team) in week (S, W). Rows without a
    current-season window for that team (week 1, a new player) are null."""
    schema = _schema()
    rows = {g: {"gsis_id": g, **{c: None for c in schema if c != "gsis_id"}}
            for g in targets["gsis_id"].to_list()}
    want = set(zip(targets["gsis_id"].to_list(), targets["team"].to_list()))
    if targets.height:
        for (g, team), rec in _returning(sources, season, week, report_mode).items():
            if (g, team) in want:
                rows[g].update(rec)
    return pl.DataFrame(list(rows.values()), schema=schema, orient="row")


def _returning(sources: Sources, season: int, week: int, report_mode: str) -> dict[tuple[str, str], dict]:
    st = _state(sources, season, week, report_mode)
    ps = sources.panel.filter(((pl.col("season") == season) & (pl.col("week") < week)) | (pl.col("season") == season - 1))
    cur = ps.filter(pl.col("season") == season)
    if cur.height == 0:
        return {}
    tot = (ps.group_by("team", "season", "week").agg(
        pl.col("team_targets").drop_nulls().first().alias("tt"), pl.col("team_rush_attempts").drop_nulls().first().alias("tr"),
        pl.col("targets").sum().alias("st"), pl.col("carries").sum().alias("sc")))
    totals = {(t, key(s, w)): (tt if tt is not None else s1, tr if tr is not None else s2)
              for t, s, w, tt, tr, s1, s2 in tot.select("team", "season", "week", "tt", "tr", "st", "sc").iter_rows()}
    games: dict[str, dict[str, dict[int, tuple]]] = defaultdict(lambda: defaultdict(dict))
    for g, t, s, w, pos, tg, ca in ps.select("gsis_id", "team", "season", "week", "position", "targets",
                                             "carries").iter_rows():
        games[t][g][key(s, w)] = (pos, tg or 0.0, ca or 0.0)
    windows: dict[str, list[int]] = defaultdict(list)
    for t, k in sorted((x for x in totals if x[1] // 100 == season), key=lambda x: -x[1]):
        if len(windows[t]) < TEAM_WINDOW:
            windows[t].append(k)

    def share_when_playing(t, j) -> tuple[float, float, int]:
        ks = sorted(games[t][j], reverse=True)[:ROLE_WINDOW]
        tt = sum(totals[(t, k)][0] or 0 for k in ks)
        tr = sum(totals[(t, k)][1] or 0 for k in ks)
        return (sum(games[t][j][k][1] for k in ks) / tt if tt else 0.0,
                sum(games[t][j][k][2] for k in ks) / tr if tr else 0.0, len(ks))

    report_missing = st.report_mode == "week" and st.report is None
    out: dict[tuple[str, str], dict] = {}
    for team, win in windows.items():
        avail = {j: st.availability(j, team) for j in games[team]}
        # role holders: highest share when playing per role position among players not off the team
        roles: dict[str, str] = {}
        best: dict[str, tuple] = {}
        for j, played in games[team].items():
            pos_j = played[max(played)][0]
            if pos_j not in ROLES or avail[j] == "gone":
                continue
            if not any(k // 100 == season for k in played) and st.roster.get(j, ("",))[0] != team:
                continue      # prior-season player only: needs current-season roster evidence for this team
            sh = share_when_playing(team, j)
            if sh[2] < ROLE_MIN_GAMES:
                continue
            rank = (sh[ROLES[pos_j]], j)
            if pos_j not in best or rank > best[pos_j]:
                best[pos_j], roles[pos_j] = rank, j
        for g, played_g in games[team].items():
            mine = [k for k in win if k in played_g]
            if not mine:
                continue
            rec: dict = {}
            out[(g, team)] = rec
            if report_missing:
                continue      # report-dependent quantities unknown: null, never 0
            tt = sum(totals[(team, k)][0] or 0 for k in mine)
            tr = sum(totals[(team, k)][1] or 0 for k in mine)
            n = len(mine)
            rt = rc = 0.0
            missed_max, from_reserve, back_now, returners = 0.0, 0, 0, []
            flags = {p: 0 for p in ROLES}
            for pos_j, j in roles.items():
                if j == g:
                    continue
                played = games[team][j]
                missed = (n - sum(k in played for k in mine)) / n
                if missed == 0:
                    continue
                a = avail[j]
                rep = (st.report or {}).get(j)
                rep = (rep or "").strip().lower()
                if a == "inactive":
                    if not (st.listed(j) and rep not in ("out", "doubtful")):
                        continue
                    from_reserve = 1
                elif a in UNAVAILABLE:
                    continue
                elif win[0] not in played and not (st.listed(j) or st.roster.get(j, ("", ""))[1] in ACTIVE_ROSTER):
                    continue      # absent last game with no pregame evidence he is back
                sj_t, sj_c, _ = share_when_playing(team, j)
                rt += sj_t * missed
                rc += sj_c * missed
                missed_max = max(missed_max, missed)
                back_now = max(back_now, int(win[0] not in played))
                flags[pos_j] = 1
                returners.append(j)
            s_t = sum(played_g[k][1] for k in mine) / tt if tt else None
            s_c = sum(played_g[k][2] for k in mine) / tr if tr else None
            rec.update(ret_tgt_reclaim=rt if tt else None, ret_car_reclaim=rc if tr else None,
                       ret_tgt_loss_pg=None if s_t is None else (s_t - s_t / (1.0 + rt)) * tt / n,
                       ret_car_loss_pg=None if s_c is None else (s_c - s_c / (1.0 + rc)) * tr / n,
                       ret_wr1=flags["WR"], ret_te1=flags["TE"], ret_rb1=flags["RB"], ret_from_reserve=from_reserve,
                       ret_back_now=back_now, ret_missed_max=missed_max, ret_returners=",".join(sorted(returners)) or None)
    return out


# ----------------------------------------------------------------------------- challenger


def candidate_features(cfg: ChallengerConfig, position: str, candidate: str | None) -> list[str]:
    """ifp-2's features for the position, plus the returning group for candidate 'R'."""
    feats = list(cfg.features(position))
    if candidate == CANDIDATE:
        feats += [f for f in GROUP[position] if f not in feats]
    return feats


def fit_candidate(train: pl.DataFrame, cfg: ChallengerConfig, position: str, candidate: str | None,
                  target_key: int) -> RidgeModel:
    spec = cfg.positions[position]
    return fit_linear(train, position, candidate_features(cfg, position, candidate), spec["lam"],
                      half_life_weeks=spec.get("half_life_weeks"), target_key=target_key,
                      name=f"ret_{candidate or 'ifp2'}")


def walk_forward(store: pl.DataFrame, weeks: list[tuple[int, int]], cfg: ChallengerConfig,
                 per_position: dict[str, str] | None = None) -> pl.DataFrame:
    """pred_ifp2 and pred_R per store row of `weeks` (null = abstention: no recent game or fewer
    than MIN_TRAIN_ROWS training rows). `per_position` adds pred_ret (the composite: 'R' or 'ifp2'
    per position). Training rows strictly earlier (asserted)."""
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
            fitted: dict[str, np.ndarray] = {}
            for tag in ("ifp2", CANDIDATE):
                fitted[tag] = (np.full(rows.height, np.nan) if train.height < MIN_TRAIN_ROWS else
                               np.where(ok, fit_candidate(train, cfg, pos, None if tag == "ifp2" else tag, k)
                                        .predict(rows), np.nan))
            cols = {"pred_ifp2": fitted["ifp2"], f"pred_{CANDIDATE}": fitted[CANDIDATE]}
            if per_position is not None:
                cols["pred_ret"] = fitted[per_position.get(pos, "ifp2")]
            out.append(rows.select("key", "gsis_id").with_columns(
                [pl.Series(c, v).fill_nan(None) for c, v in cols.items()]))
    return pl.concat(out, how="diagonal_relaxed") if out else pl.DataFrame()


def cohorts() -> dict[str, pl.Expr]:
    """Returning-role cohorts (pregame), split by the returning player's role, plus the reference
    cohorts of the ifp-ctx study that matter for this question."""
    from .forecast_backtest import starters_pool_expr

    any_ret = (pl.col("ret_wr1") == 1) | (pl.col("ret_te1") == 1) | (pl.col("ret_rb1") == 1)
    return {
        "ret_any": any_ret,
        "ret_none": ~any_ret,
        "ret_wr1": pl.col("ret_wr1") == 1,
        "ret_te1": pl.col("ret_te1") == 1,
        "ret_rb1": pl.col("ret_rb1") == 1,
        "ret_back_now": pl.col("ret_back_now") == 1,
        "ret_from_reserve": pl.col("ret_from_reserve") == 1,
        "ret_long_absence": any_ret & (pl.col("ret_missed_max") >= 0.6),
        "top_tgt_competitor_returning": pl.col("top_tgt_comp_return") == 1,
        "top_tgt_competitor_out": pl.col("top_tgt_comp_out") == 1,
        "starters_pool": starters_pool_expr(),
        "early_or_low_sample": (pl.col("week") <= 4) | (pl.col("games_recent") < 3),
    }


__all__ = ["RET_VERSION", "RET_CHALLENGER_VERSION", "RET_STATUS", "RET_DOCS", "GROUP", "DECISION_RULES",
           "returning_for_week", "candidate_features", "fit_candidate", "walk_forward", "cohorts"]
