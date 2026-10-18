"""Week-specific injury timeline, established healthy role and team workload handoff.

Feeds the production model ifp-2.1.0 (app.analytics.injury_model, with the iwh-0.1.1 participation fix:
`listing_absence=True`) and the research challenger `ifp-iwh` (scripts/injury_workload_research.py, iwh-0.1.0,
the defaults here, kept byte-identical so the evaluated research reproduces). ifp-1.0.0 and ifp-2.0.0 never read it.
The hosted API never imports it (numpy/polars). See docs/research/injury_workload_handoff.md for every definition
and the evidence.

Three layers, each point-in-time for a forecast of (season S, target week T) made at cutoff week W <= T:

1. `injury_timeline`: one row per player on a team's roster picture. Facts, not forecasts: the cutoff
   week's report designation, roster status before W, last appearance, last healthy appearance, team games
   missed since (and how many of those were injury-related), reserve eligibility where a rule dates it,
   return evidence, and a status class. A designation is week-specific: the week-W report describes week W's
   game only. For T > W (horizon >= 1) nothing describes week T, so a currently injured player is
   `uncertain`, never assumed out and never assumed healthy.
2. `established_roles`: each player's healthy-game role (carry share, target share, goal-line share) over his
   last <= HEALTHY_WINDOW healthy games in the lookback (current + previous season). A missed game is not a
   row (the panel only holds appearances) and an injury-exit game is not a healthy game, so neither lowers
   the role. Sample size is handled by shrinking toward the newcomer prior (the share of a player's first
   games for a new team, measured from the same pre-cutoff data).
3. `workload_handoff`: per team and target week, the expected opportunity of every available RB/WR/TE:

       expected carries = team rush attempts per game x RB group share of rushes x player's share of the
                          available RB claims (adjusted by the team's own games without the absent players)
       expected targets = team targets per game x position group share of targets x share of available claims

   A player's claim is his established share x his recent participation (games played OR missed injured, over
   the team's recent games): an injury absence keeps the claim, a healthy scratch or a new demotion lowers it.
   Unavailable players' claims leave the pool and the rest absorb them in proportion to their claims, then
   toward what they actually received in the team's earlier games without those players (shrunk by
   ABSENCE_SHRINK_GAMES). A returning starter re-enters the pool with his established claim, which takes the
   temporary share back from the replacements. Nobody gets the vacated volume mechanically: a committee splits
   it, a lone qualified backup can take most of it.

Leakage contract (tested):
  * Only games strictly before the cutoff week W: box scores, snaps, team totals (Sources.panel), roster
    statuses of weeks < W, and the week-W pregame injury report (rows modified at/after kickoff are dropped by
    forecast_features.load_injuries; 2025+ rows carry no timestamp and are retrospective).
  * Never read: week-W (or later) box scores, the week-W roster status (the game-day inactive decision), depth
    charts, betting lines.
  * Unknown stays null; nothing here is a fantasy-point adjustment. Any effect on points is a coefficient
    fitted walk-forward by the challenger.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import polars as pl

from ..db.connection import table_columns, table_exists
from .forecast_features import Sources, classify_games, key

WORKLOAD_VERSION = "iwh-0.1.0"          # the evaluated research version (defaults below)
WORKLOAD_VERSION_FIXED = "iwh-0.1.1"    # + listing_absence: the pre-registered fix of section 7.3 (production)

# Fixed before any backtest; derivations in docs/research/injury_workload_handoff.md.
HEALTHY_WINDOW = 8           # established role: the last <= 8 healthy games in the lookback
LOOKBACK_SEASONS = 1         # current season + this many previous seasons (same as forecast_features)
ROLE_SHRINK_GAMES = 1.0      # established share shrinks toward the newcomer prior by this many games
TEAM_WINDOW = 4              # recent team games for the participation factor
TEAM_SHRINK_GAMES = 3.0      # team rush attempts / targets per game shrink toward the league
GROUP_SHRINK_GAMES = 4.0     # a position group's share of team volume shrinks toward the league
ABSENCE_SHRINK_GAMES = 2.0   # share in earlier games without the absent players shrinks toward proportional
MATERIAL_CLAIM = 0.10        # an unavailable teammate with at least this established share defines an absence
ABSENCE_COVERAGE = 0.70      # ... largest such claims first, until they cover 70% of the absent claims
XFPL_SHRINK_GAMES = 3.0      # player expected-points-per-opportunity multiplier shrinks toward 1
MAX_SCENARIO_PLAYERS = 3     # horizon >= 1: uncertain teammates enumerated as scenarios (largest claims first)
NEWCOMER_FALLBACK = {"RB": (0.05, 0.03, 0.05), "WR": (0.0, 0.06, 0.0), "TE": (0.0, 0.04, 0.0)}
LEAD_SHARE = {"RB": 0.40, "WR": 0.18, "TE": 0.12}   # cohort labels only: established share of a lead back / WR1 / TE1

SKILL = ("RB", "WR", "TE")
ACTIVE_ROSTER = ("ACT", "INA")
RESERVE_ROSTER = ("RES", "PUP", "NFI")
UNAVAILABLE_DESIGNATIONS = ("out", "doubtful")

# Status classes: a fact-based ordinal label, never a probability.
AVAILABLE, RETURNING, UNCERTAIN, UNAVAILABLE, UNKNOWN = (
    "available", "available_returning", "uncertain", "unavailable", "unknown")
STATUS_CLASSES = (AVAILABLE, RETURNING, UNCERTAIN, UNAVAILABLE, UNKNOWN)

TIMELINE_DOCS: dict[str, str] = {
    "designation": "week-specific: out | doubtful | questionable | listed (practice report, no game status) | "
                   "not_listed (report published, player absent from it) | no_report (team has no week-W rows) | "
                   "future_unknown (target week after the cutoff: no report can exist yet)",
    "report_week": "the week whose report the designation comes from (the cutoff week W); null without one",
    "roster_status": "latest nflverse weekly roster status before the cutoff week (ACT, INA, RES, ...)",
    "nfl_reserve": "1 if that status is a reserve list (RES/PUP/NFI)",
    "reserve_earliest_week": "earliest NFL-eligible week under the minimum-games rule (player_availability) when "
                             "the reserve code is known; null otherwise (no return date is ever invented)",
    "last_app_season/week": "the player's last appearance (box-score row or offensive snaps) before W",
    "last_healthy_season/week": "his last appearance that was not an injury exit",
    "last_game_injury_exit": "1 if the last appearance is classified injury_exit (forecast_features.classify_games)",
    "missed_team_games": "his current team's final games this season after his last appearance, before W",
    "missed_injured": "of those, games he was designated Out/Doubtful or on a reserve list for (injury absences); "
                      "iwh-0.1.1 (listing_absence): any injury listing that week",
    "return_evidence": "pregame evidence after missed games: listed_without_designation | questionable | "
                       "activated_from_reserve | none",
    "status_class": "available | available_returning | uncertain | unavailable | unknown (see status_reason)",
    "confidence": "report (week-W report published for his team) | roster_only | none",
}


# ----------------------------------------------------------------------------- optional source


def load_roster_codes(con, seasons: list[int] | None = None) -> pl.DataFrame | None:
    """nflverse GSIS roster codes (status_description_abbr: R01 Reserve/Injured, R04 PUP, ...) per player-week.
    Optional: without them a reserve stint has no dated earliest return (player_availability RESERVE_OTHER)."""
    need = {"season", "week", "gsis_id", "status_description_abbr"}
    if not table_exists(con, "nfl_rosters_weekly") or not need <= set(table_columns(con, "nfl_rosters_weekly")):
        return None
    where = f"AND season IN ({', '.join(str(int(s)) for s in seasons)})" if seasons else ""
    df = pl.from_arrow(con.execute(f"""
        SELECT season::INT AS season, week::INT AS week, gsis_id, any_value(status_description_abbr) AS abbr
        FROM nfl_rosters_weekly WHERE gsis_id IS NOT NULL {where} GROUP BY 1, 2, 3""").arrow())
    schema = {"season": pl.Int64, "week": pl.Int64, "gsis_id": pl.String, "abbr": pl.String}
    return df.cast(schema, strict=False) if df.height else pl.DataFrame(schema=schema)


# ----------------------------------------------------------------------------- shared pre-cutoff state


@dataclass
class _Cut:
    season: int
    target: int
    cutoff: int
    panel: pl.DataFrame              # lookback panel before the cutoff, with gclass/healthy and team totals
    report: dict[str, dict] | None   # gsis -> cutoff-week injury row (None: no report used at all)
    report_teams: set[str]           # teams with any cutoff-week report row
    roster_last: dict[str, dict]     # gsis -> latest roster row (season S, week < W)
    roster_rows: dict[str, list[dict]]   # gsis -> roster rows (season S, week < W), ascending
    team_games: dict[str, list[int]]     # team -> final game weeks this season before W, ascending
    schedule: dict[str, list[int]]       # team -> all scheduled REG weeks this season (for reserve windows)
    listed: dict[tuple[str, int], str]   # (gsis, week) -> report_status for weeks <= W (injury listings only)
    played: dict[str, set[int]]          # gsis -> weeks of this season (before W) he appeared in
    listing_absence: bool = False        # iwh-0.1.1: any injury listing makes a missed game an injury absence

    def injured_absence(self, g: str, week: int, roster_row: dict | None) -> bool:
        """Was a missed game an injury absence? Designated Out/Doubtful (iwh-0.1.0) or, with listing_absence
        (iwh-0.1.1), on that week's report with ANY injury listing; or on a reserve list that week."""
        listed = (g, week) in self.listed if self.listing_absence \
            else self.listed.get((g, week)) in UNAVAILABLE_DESIGNATIONS
        return listed or (roster_row or {}).get("status") in RESERVE_ROSTER


def _cut(sources: Sources, season: int, target: int, cutoff: int | None,
         roster_codes: pl.DataFrame | None = None, listing_absence: bool = False) -> _Cut:
    w = target if cutoff is None else cutoff
    if w > target:
        raise ValueError(f"cutoff week {w} is after the target week {target}")
    ck = key(season, w)
    p = sources.panel.filter((pl.col("season") * 100 + pl.col("week") < ck)
                             & (pl.col("season") >= season - LOOKBACK_SEASONS))
    p = classify_games(p, sources, season, w) if p.height else p.with_columns(
        pl.lit(None, dtype=pl.String).alias("gclass"), pl.lit(None, dtype=pl.Boolean).alias("healthy"))
    # team totals per team-game: nflverse play-by-play totals when present, else the sum of player rows
    tot = p.group_by("season", "week", "team").agg(
        pl.col("team_targets").drop_nulls().first().alias("_tt"), pl.col("team_rush_attempts").drop_nulls().first()
        .alias("_tr"), pl.col("targets").sum().alias("_st"), pl.col("carries").sum().alias("_sc"),
        pl.col("gl_carries").drop_nulls().sum().alias("_tgl"), pl.col("gl_carries").is_not_null().any().alias("_glok"))
    tot = tot.with_columns(pl.coalesce("_tt", "_st").alias("tm_targets"), pl.coalesce("_tr", "_sc").alias("tm_rush"),
                           pl.when(pl.col("_glok")).then(pl.col("_tgl")).otherwise(None).alias("tm_gl"))
    p = p.join(tot.select("season", "week", "team", "tm_targets", "tm_rush", "tm_gl"), on=["season", "week", "team"],
               how="left").with_columns(
        pl.when(pl.col("tm_rush") > 0).then(pl.col("carries") / pl.col("tm_rush")).otherwise(None).alias("car_sh"),
        pl.when(pl.col("tm_targets") > 0).then(pl.col("targets") / pl.col("tm_targets")).otherwise(None).alias("tgt_sh"),
        pl.when(pl.col("tm_gl") > 0).then(pl.col("gl_carries") / pl.col("tm_gl")).otherwise(None).alias("gl_sh"))
    report, report_teams = None, set()
    listed: dict[tuple[str, int], str] = {}
    if sources.injuries is not None:
        inj = sources.injuries.filter((pl.col("season") == season) & (pl.col("week") <= w))
        cur = inj.filter(pl.col("week") == w)
        report = {r["gsis_id"]: r for r in cur.iter_rows(named=True)}
        report_teams = set(cur["team"].drop_nulls().to_list())
        for g, wk, st, ni in inj.select("gsis_id", "week", "report_status", "not_injury").iter_rows():
            if not ni:
                listed[(g, int(wk))] = (st or "").strip().lower()
    roster_rows: dict[str, list[dict]] = defaultdict(list)
    if sources.rosters is not None:
        r = sources.rosters.filter((pl.col("season") == season) & (pl.col("week") < w))
        if roster_codes is not None and roster_codes.height:
            r = r.join(roster_codes, on=["season", "week", "gsis_id"], how="left")
        else:
            r = r.with_columns(pl.lit(None, dtype=pl.String).alias("abbr"))
        for row in r.sort("week").iter_rows(named=True):
            roster_rows[row["gsis_id"]].append(row)
    roster_last = {g: rows[-1] for g, rows in roster_rows.items()}
    games = sources.games.filter(pl.col("season") == season)
    team_games: dict[str, list[int]] = defaultdict(list)
    schedule: dict[str, list[int]] = defaultdict(list)
    for t, wk, pf in games.select("team", "week", "points_for").sort("week").iter_rows():
        schedule[t].append(int(wk))
        if wk < w and pf is not None:
            team_games[t].append(int(wk))
    played: dict[str, set[int]] = defaultdict(set)
    for g, wk in p.filter(pl.col("season") == season).select("gsis_id", "week").iter_rows():
        played[g].add(int(wk))
    return _Cut(season, target, w, p, report, report_teams, roster_last, dict(roster_rows), dict(team_games),
                dict(schedule), listed, dict(played), listing_absence)


# ----------------------------------------------------------------------------- 1. injury timeline


TIMELINE_SCHEMA = {
    "gsis_id": pl.String, "position": pl.String, "team": pl.String, "season": pl.Int64, "target_week": pl.Int64,
    "cutoff_week": pl.Int64, "horizon": pl.Int64, "designation": pl.String, "report_week": pl.Int64,
    "report_status": pl.String, "practice_status": pl.String, "injury": pl.String, "roster_status": pl.String,
    "roster_week": pl.Int64, "nfl_reserve": pl.Int8, "reserve_earliest_week": pl.Int64,
    "last_app_season": pl.Int64, "last_app_week": pl.Int64, "last_app_team": pl.String,
    "last_healthy_season": pl.Int64, "last_healthy_week": pl.Int64, "last_game_injury_exit": pl.Int8,
    "missed_team_games": pl.Int64, "missed_injured": pl.Int64, "prev_game_out": pl.Int8,
    "return_evidence": pl.String, "status_class": pl.String, "status_reason": pl.String, "confidence": pl.String,
}


def _reserve_earliest(rows: list[dict], schedule: list[int] | None) -> int | None:
    """Earliest NFL-eligible week from player_availability's minimum-games rule (only with a rule code)."""
    from .player_availability import RULE_CODES, derive

    if not rows or rows[-1].get("abbr") not in RULE_CODES:
        return None
    rec = derive(espn_status=None, on_ir=False, rows=[{"week": r["week"], "status": r["status"], "abbr": r.get("abbr"),
                                                       "team": r["team"]} for r in rows],
                 team_games=schedule, weeks=[])
    return rec["earliest_eligible_week"]


def _timeline_rows(cut: _Cut) -> list[dict]:
    p = cut.panel
    horizon = cut.target - cut.cutoff
    last = p.sort(["season", "week"], descending=True).unique("gsis_id", keep="first")
    last_h = p.filter(pl.col("healthy")).sort(["season", "week"], descending=True).unique("gsis_id", keep="first")
    lh = {g: (s, w) for g, s, w in last_h.select("gsis_id", "season", "week").iter_rows()}
    seen = {r["gsis_id"]: r for r in last.iter_rows(named=True)}
    ids = set(seen) | {g for g, r in cut.roster_last.items() if r.get("position") in (*SKILL, "QB")}
    out = []
    for g in sorted(ids):
        app = seen.get(g)
        ros = cut.roster_last.get(g)
        team = (ros or {}).get("team") or (app or {}).get("team")
        pos = (app or {}).get("position") or (ros or {}).get("position")
        if team is None or pos not in (*SKILL, "QB"):
            continue
        rs = (ros or {}).get("status")
        rec = {c: None for c in TIMELINE_SCHEMA}
        rec.update(gsis_id=g, position=pos, team=team, season=cut.season, target_week=cut.target,
                   cutoff_week=cut.cutoff, horizon=horizon, roster_status=rs, roster_week=(ros or {}).get("week"),
                   nfl_reserve=None if rs is None else int(rs in RESERVE_ROSTER))
        if app is not None:
            rec.update(last_app_season=app["season"], last_app_week=app["week"], last_app_team=app["team"],
                       last_game_injury_exit=int(app.get("gclass") == "injury_exit"))
        if g in lh:
            rec.update(last_healthy_season=lh[g][0], last_healthy_week=lh[g][1])
        # team games missed since his last appearance (current team, this season, before the cutoff)
        tg = cut.team_games.get(team, [])
        after = [w for w in tg if app is None or app["season"] < cut.season or w > app["week"]]
        rec["missed_team_games"] = len(after)
        rows = cut.roster_rows.get(g, [])
        by_week = {r["week"]: r for r in rows}
        inj = sum(1 for w in after if cut.injured_absence(g, w, by_week.get(w)))
        rec["missed_injured"] = inj
        prev = tg[-1] if tg else None
        rec["prev_game_out"] = None if prev is None else int(cut.listed.get((g, prev)) in UNAVAILABLE_DESIGNATIONS)
        if rs in RESERVE_ROSTER:
            rec["reserve_earliest_week"] = _reserve_earliest(rows, cut.schedule.get(team))
        # --- designation: the cutoff week's report describes the cutoff week's game only
        rep = (cut.report or {}).get(g)
        if horizon:
            rec["designation"] = "future_unknown"
        elif cut.report is None:
            rec["designation"] = "no_report"
        elif team not in cut.report_teams:
            rec["designation"] = "no_report"
        else:
            st = ((rep or {}).get("report_status") or "").strip().lower()
            rec["designation"] = st if st in ("out", "doubtful", "questionable") else ("listed" if rep else "not_listed")
        if rep is not None:
            rec.update(report_week=cut.cutoff, report_status=rep.get("report_status"),
                       practice_status=rep.get("practice_status"), injury=rep.get("injury"))
        cur_st = ((rep or {}).get("report_status") or "").strip().lower()
        missed = rec["missed_team_games"] > 0
        was_reserve = any(r["status"] in RESERVE_ROSTER for r in rows)
        if missed:
            if rs in ACTIVE_ROSTER and was_reserve:
                rec["return_evidence"] = "activated_from_reserve"
            elif rep is not None and cur_st == "questionable":
                rec["return_evidence"] = "questionable"
            elif rep is not None and cur_st not in UNAVAILABLE_DESIGNATIONS:
                rec["return_evidence"] = "listed_without_designation"
            else:
                rec["return_evidence"] = "none"
        rec["confidence"] = ("report" if rec["designation"] not in ("no_report", "future_unknown")
                             else "roster_only" if rs is not None else "none")
        rec["status_class"], rec["status_reason"] = _status(rec, cur_st, horizon)
        out.append(rec)
    return out


def _status(rec: dict, cur_st: str, horizon: int) -> tuple[str, str]:
    rs, d, target = rec["roster_status"], rec["designation"], rec["target_week"]
    missed, inj = rec["missed_team_games"] or 0, rec["missed_injured"] or 0
    if rs is not None and rs not in ACTIVE_ROSTER:
        if rs in RESERVE_ROSTER:
            earliest = rec["reserve_earliest_week"]
            if earliest is not None and target < earliest:
                return UNAVAILABLE, f"NFL reserve list: not eligible before week {earliest}"
            if horizon and earliest is not None:
                return UNCERTAIN, f"NFL reserve list: eligible from week {earliest}, return not confirmed"
            if d in ("listed", "questionable"):
                return UNCERTAIN, "on a reserve list before this week but on this week's report without Out"
            return UNAVAILABLE, "NFL reserve list (no activation before the cutoff)"
        return UNAVAILABLE, f"not on the NFL active roster (status {rs})"
    if horizon:
        back = rec["return_evidence"] not in (None, "none")
        if cur_st in UNAVAILABLE_DESIGNATIONS or (missed and inj and not back):
            return UNCERTAIN, (f"injured as of the week-{rec['cutoff_week']} cutoff; no week-{target} report exists "
                               "yet (return neither assumed nor ruled out)")
        return AVAILABLE, f"no injury evidence as of the week-{rec['cutoff_week']} cutoff (no week-{target} report yet)"
    if d in UNAVAILABLE_DESIGNATIONS:
        return UNAVAILABLE, f"designated {d.capitalize()} for week {target}"
    if d == "questionable":
        return UNCERTAIN, f"designated Questionable for week {target}"
    if d in ("listed", "not_listed"):
        if missed and not inj and rec["return_evidence"] != "activated_from_reserve":
            return AVAILABLE, (f"missed {missed} team game(s) without an injury listing (a role or roster decision, "
                               f"not an injury); no week-{target} game designation")
        if missed:
            return RETURNING, (f"missed {missed} team game(s){' injured' if inj else ''}; on the week-{target} report "
                               f"without an Out/Doubtful designation" if d == "listed" else
                               f"missed {missed} team game(s); not on the published week-{target} report")
        return AVAILABLE, f"no week-{target} game designation"
    # no week-W report for his team
    if missed and (inj or rec["prev_game_out"]):
        return UNCERTAIN, f"missed {missed} team game(s) injured; no week-{target} report synced yet"
    if missed:
        return UNKNOWN, f"missed {missed} team game(s) without an injury listing; no week-{target} report synced yet"
    return AVAILABLE, f"played his team's last game; no week-{target} report synced yet"


def injury_timeline(sources: Sources, season: int, target_week: int, cutoff_week: int | None = None,
                    roster_codes: pl.DataFrame | None = None, listing_absence: bool = False) -> pl.DataFrame:
    """Player-by-week injury timeline for (season, target_week) as known at `cutoff_week` (default: the target
    week itself, horizon 0). One row per QB/RB/WR/TE with a lookback appearance or a roster row before W.
    `listing_absence`: the iwh-0.1.1 rule for `missed_injured` (_Cut.injured_absence)."""
    cut = _cut(sources, season, target_week, cutoff_week, roster_codes, listing_absence)
    rows = _timeline_rows(cut)
    return pl.DataFrame(rows, schema=TIMELINE_SCHEMA, orient="row") if rows else pl.DataFrame(schema=TIMELINE_SCHEMA)


# ----------------------------------------------------------------------------- 2. established healthy role


def _newcomer_prior(p: pl.DataFrame) -> dict[str, tuple[float, float, float]]:
    """Mean (carry, target, goal-line) share in a player's first game for a team he had not played for in
    the lookback, from week 2 on (mid-season additions and promotions), per position. Pre-cutoff data only."""
    if p.height == 0:
        return dict(NEWCOMER_FALLBACK)
    first = (p.sort(["season", "week"]).group_by(["gsis_id", "team"], maintain_order=True).first()
             .filter(pl.col("week") >= 2))
    out = {}
    for pos in SKILL:
        g = first.filter(pl.col("position") == pos)
        fb = NEWCOMER_FALLBACK[pos]
        vals = []
        for i, c in enumerate(("car_sh", "tgt_sh", "gl_sh")):
            s = g[c].drop_nulls() if g.height else pl.Series([], dtype=pl.Float64)
            vals.append(float(s.mean()) if s.len() >= 20 else fb[i])
        out[pos] = tuple(vals)
    return out


ROLE_SCHEMA = {"gsis_id": pl.String, "role_games": pl.Int64, "role_car_share": pl.Float64,
               "role_tgt_share": pl.Float64, "role_gl_share": pl.Float64, "role_snap": pl.Float64,
               "role_xfpl_pg": pl.Float64, "role_opp_pg": pl.Float64, "role_other_team_games": pl.Int64}


def _roles(cut: _Cut, teams: dict[str, str]) -> tuple[pl.DataFrame, dict]:
    p = cut.panel
    prior = _newcomer_prior(p)
    h = (p.filter(pl.col("healthy") & pl.col("position").is_in(SKILL)).sort(["season", "week"], descending=True)
         .group_by("gsis_id", maintain_order=True).head(HEALTHY_WINDOW))
    cur = pl.DataFrame({"gsis_id": list(teams), "_cur_team": list(teams.values())}, schema={"gsis_id": pl.String,
                                                                                           "_cur_team": pl.String})
    agg = h.join(cur, on="gsis_id", how="left").group_by("gsis_id").agg(
        pl.len().alias("role_games"), pl.col("position").first().alias("_pos"),
        pl.col("car_sh").drop_nulls().sum().alias("_cs"), pl.col("car_sh").drop_nulls().len().alias("_cn"),
        pl.col("tgt_sh").drop_nulls().sum().alias("_ts"), pl.col("tgt_sh").drop_nulls().len().alias("_tn"),
        pl.col("gl_sh").drop_nulls().sum().alias("_gs"), pl.col("gl_sh").drop_nulls().len().alias("_gn"),
        pl.col("offense_pct").drop_nulls().mean().alias("role_snap"),
        pl.col("xfpl").drop_nulls().mean().alias("role_xfpl_pg"),
        (pl.col("carries") + pl.col("targets")).mean().alias("role_opp_pg"),
        (pl.col("team") != pl.col("_cur_team")).fill_null(False).sum().alias("role_other_team_games"))
    pri = pl.DataFrame({"_pos": list(prior), "_pc": [v[0] for v in prior.values()], "_pt": [v[1] for v in prior.values()],
                        "_pg": [v[2] for v in prior.values()]})
    k = ROLE_SHRINK_GAMES
    agg = agg.join(pri, on="_pos", how="left").with_columns(
        ((pl.col("_cs") + pl.col("_pc") * k) / (pl.col("_cn") + k)).alias("role_car_share"),
        ((pl.col("_ts") + pl.col("_pt") * k) / (pl.col("_tn") + k)).alias("role_tgt_share"),
        ((pl.col("_gs") + pl.col("_pg") * k) / (pl.col("_gn") + k)).alias("role_gl_share"))
    return agg.select(list(ROLE_SCHEMA)).cast(ROLE_SCHEMA), prior


def established_roles(sources: Sources, season: int, week: int) -> pl.DataFrame:
    """Healthy-game role per player as of week W (see the module docstring, layer 2)."""
    cut = _cut(sources, season, week, None)
    teams = {r["gsis_id"]: r["team"] for r in _timeline_rows(cut)}
    return _roles(cut, teams)[0]


# ----------------------------------------------------------------------------- 3. workload handoff


HANDOFF_SCHEMA = {
    "gsis_id": pl.String, "ho_team": pl.String, "ho_car_share": pl.Float64, "ho_tgt_share": pl.Float64,
    "ho_gl_share": pl.Float64, "ho_carries": pl.Float64, "ho_targets": pl.Float64, "ho_exp_xfpl": pl.Float64,
    "ho_base_car_share": pl.Float64, "ho_base_tgt_share": pl.Float64, "ho_shift_pg": pl.Float64,
    "ho_vacated": pl.Float64, "ho_returning": pl.Float64, "ho_absence_games": pl.Int64, "ho_claim": pl.Float64,
    "ho_participation": pl.Float64, "ho_team_rush_pg": pl.Float64, "ho_team_tgt_pg": pl.Float64,
    "ho_group_share": pl.Float64, "ho_top_absent": pl.String, "ho_top_absent_share": pl.Float64,
    "ho_top_returning": pl.String, "ho_self_returning": pl.Int8, "ho_scenarios": pl.Int64,
    "ho_lead_out": pl.Int8, "ho_lead_returning": pl.Int8, "ho_is_lead": pl.Int8,
}


def _team_volume(p: pl.DataFrame, season: int) -> dict[str, tuple[float, float]]:
    """Team rush attempts and targets per game: this season shrunk toward last season's team rate, itself shrunk
    toward the league (TEAM_SHRINK_GAMES each). With no game at all: the league rate."""
    tg = p.select("season", "week", "team", "tm_rush", "tm_targets").unique(["season", "week", "team"])
    out = {}
    lg = {}
    for s in (season - 1, season):
        x = tg.filter(pl.col("season") == s)
        lg[s] = (float(x["tm_rush"].mean()) if x.height else None, float(x["tm_targets"].mean()) if x.height else None)
    lg_all = (float(tg["tm_rush"].mean()) if tg.height else 26.0, float(tg["tm_targets"].mean()) if tg.height else 33.0)
    k = TEAM_SHRINK_GAMES
    stats = {(t, s): (n, r, tt) for t, s, n, r, tt in tg.group_by("team", "season").agg(
        pl.len(), pl.col("tm_rush").sum(), pl.col("tm_targets").sum()).iter_rows()}
    for t in set(tg["team"].to_list()):
        prior = []
        for i in range(2):
            base = lg.get(season - 1, (None, None))[i] or lg_all[i]
            n, r, tt = stats.get((t, season - 1), (0, 0.0, 0.0))
            prev = ((r if i == 0 else tt) + base * k) / (n + k)
            n2, r2, tt2 = stats.get((t, season), (0, 0.0, 0.0))
            prior.append(((r2 if i == 0 else tt2) + prev * k) / (n2 + k))
        out[t] = (prior[0], prior[1])
    out["__league__"] = lg_all
    return out


def _group_shares(p: pl.DataFrame, season: int) -> dict[tuple[str, str], float]:
    """(team, metric) -> share of team volume going to a position group: ("RB", carries), (pos, targets).
    This season's team rate shrunk toward the league rate of the lookback by GROUP_SHRINK_GAMES."""
    out = {}
    tg = p.group_by("season", "week", "team").agg(
        pl.col("tm_rush").first(), pl.col("tm_targets").first(),
        pl.col("carries").filter(pl.col("position") == "RB").sum().alias("RB_car"),
        *[pl.col("targets").filter(pl.col("position") == pos).sum().alias(f"{pos}_tgt") for pos in SKILL])
    k = GROUP_SHRINK_GAMES
    lg = {"RB_car": float(tg["RB_car"].sum() / tg["tm_rush"].sum()) if tg.height and tg["tm_rush"].sum() else 0.8}
    for pos in SKILL:
        c = f"{pos}_tgt"
        lg[c] = float(tg[c].sum() / tg["tm_targets"].sum()) if tg.height and tg["tm_targets"].sum() else 0.3
    cur = tg.filter(pl.col("season") == season)
    for t, grp in cur.group_by("team"):
        n = grp.height
        for c, den in (("RB_car", "tm_rush"), *[(f"{pos}_tgt", "tm_targets") for pos in SKILL]):
            per = (grp[c] / grp[den]).fill_nan(None).drop_nulls()
            out[(t[0], c)] = (float(per.sum()) + lg[c] * k) / (per.len() + k) if n else lg[c]
    out.update({("__league__", c): v for c, v in lg.items()})
    return out


def _xfpl_rates(p: pl.DataFrame) -> dict[str, tuple[float, float, float]]:
    """Per position: (league points of expected value per carry, per target, mean fitted per healthy game) from a
    no-intercept least-squares fit of healthy-game xfpl on carries and targets (pre-cutoff rows only)."""
    out = {}
    for pos in SKILL:
        g = p.filter(pl.col("healthy") & (pl.col("position") == pos) & pl.col("xfpl").is_not_null())
        if g.height < 30:
            out[pos] = (0.6, 1.5, 6.0)
            continue
        x = np.column_stack([g["carries"].to_numpy(), g["targets"].to_numpy()])
        y = g["xfpl"].to_numpy()
        coef, *_ = np.linalg.lstsq(x, y, rcond=None)
        a, b = float(max(coef[0], 0.0)), float(max(coef[1], 0.0))
        out[pos] = (a, b, float((x @ np.array([a, b])).mean()) or 1.0)
    return out


def _participation(cut: _Cut, g: str, team: str) -> float:
    """Share of the team's last <= TEAM_WINDOW final games the player either played or missed injured (designated
    Out/Doubtful, or on a reserve list that week), counting only weeks he was on the team's roster when roster
    rows exist. An injury absence keeps the role; a healthy absence (scratch, demotion) lowers it.

    With cut.listing_absence (iwh-0.1.1, docs section 7.3) a missed game also counts as an injury absence when the player
    was on that week's report with ANY injury listing (Questionable, or the practice report without a game status),
    so a Questionable-and-inactive game is not read as a role absence. iwh-0.1.0 (False) counted Out/Doubtful only."""
    weeks = cut.team_games.get(team, [])[-TEAM_WINDOW:]
    if not weeks:
        return 1.0
    played = cut.played.get(g, set())
    rows = {r["week"]: r for r in cut.roster_rows.get(g, [])}
    num = den = 0
    for w in weeks:
        r = rows.get(w)
        if rows and (r is None or r["team"] != team or r["status"] not in (*ACTIVE_ROSTER, *RESERVE_ROSTER)):
            continue      # not on this team's roster that week (practice squad, another team): not counted
        den += 1
        if w in played or cut.injured_absence(g, w, r):
            num += 1
    return num / den if den else 1.0


def _injury_absent(row: dict) -> bool:
    return (row.get("designation") in UNAVAILABLE_DESIGNATIONS or row.get("nfl_reserve") == 1
            or (row.get("missed_injured") or 0) > 0 or row.get("status_class") == UNCERTAIN)


def _key_absentees(claims: dict[str, float], absent: set[str]) -> list[str]:
    """The absent players whose claims define "the team without them": largest material claims first until they
    cover ABSENCE_COVERAGE of the absent claims (requiring EVERY absent player to have missed a game would leave
    almost no evidence when a third-stringer is also hurt)."""
    mat = sorted((a for a in absent if claims.get(a, 0.0) >= MATERIAL_CLAIM), key=lambda a: (-claims[a], a))
    total = sum(claims.get(a, 0.0) for a in absent)
    out, acc = [], 0.0
    for a in mat:
        out.append(a)
        acc += claims[a]
        if acc >= ABSENCE_COVERAGE * total:
            break
    return sorted(out)


def _allocate(claims: dict[str, float], pool: set[str], group_share: float,
              absence: dict[str, tuple[int, float]]) -> dict[str, float]:
    """Expected shares of team volume for the players in `pool`: proportional to claims (summing to
    group_share), then each shrunk toward his share in the team's earlier games without the absent players
    (n games, ABSENCE_SHRINK_GAMES), then renormalised to group_share."""
    tot = sum(claims[i] for i in pool)
    if tot <= 0 or not pool:
        return {i: 0.0 for i in pool}
    prop = {i: group_share * claims[i] / tot for i in pool}
    k = ABSENCE_SHRINK_GAMES
    raw = {i: ((absence[i][0] * absence[i][1] + k * prop[i]) / (absence[i][0] + k)) if i in absence else prop[i]
           for i in pool}
    s = sum(raw.values())
    return {i: group_share * v / s for i, v in raw.items()} if s > 0 else prop


def _team_game_shares(cut: _Cut, team: str, metric: str) -> tuple[list[tuple[int, dict[str, float]]], dict[str, int]]:
    """The team's lookback games as [(key, {player: share})] (ascending) and each player's first game key for it."""
    tp = cut.panel.filter(pl.col("team") == team).select((pl.col("season") * 100 + pl.col("week")).alias("k"),
                                                         "gsis_id", metric).sort("k")
    games: dict[int, dict[str, float]] = defaultdict(dict)
    first: dict[str, int] = {}
    for k, g, v in tp.iter_rows():
        games[k][g] = v
        first.setdefault(g, k)
    return sorted(games.items()), first


def _room_evidence(games: list[tuple[int, dict[str, float]]], first: dict[str, int], pool: set[str],
                   present: set[str], absent: list[str]) -> dict[str, tuple[int, float]]:
    """Per pool player: (games, mean share) over the team's lookback games that look like this week's room: every
    OTHER material present player played, none of the key absentees did (counted from the first game any of them
    played for the team: earlier games describe a different room), and he played. This is how the team actually
    split the work without the absent players and with the present ones, e.g. a backup's share in the games his
    lead missed, or his share once the lead is back."""
    start = min((first[a] for a in absent if a in first), default=None)
    acc: dict[str, list[float]] = defaultdict(list)
    for k, sh in games:
        if (start is not None and k < start) or any(a in sh for a in absent):
            continue
        for i in pool:
            v = sh.get(i)
            if v is None or any(p not in sh for p in present if p != i):
                continue
            acc[i].append(v)
    return {i: (len(v), sum(v) / len(v)) for i, v in acc.items() if v}


def workload_handoff(sources: Sources, season: int, target_week: int, cutoff_week: int | None = None, *,
                     timeline: pl.DataFrame | None = None, return_rates: dict | None = None,
                     roster_codes: pl.DataFrame | None = None, listing_absence: bool = False) -> pl.DataFrame:
    """Expected opportunities of every RB/WR/TE in week T (layer 3 of the module docstring), one row per player.

    Horizon 0: Out/Doubtful/reserve/inactive players leave the pool; Questionable players stay (conditional on
    the projected player appearing, a Questionable teammate usually plays; flagged in the timeline).
    Horizon >= 1 (T after the cutoff): a player injured at the cutoff is `uncertain`. With `return_rates`
    ({(position, missed_bucket, horizon): rate} from `fit_return_rates`, an empirical frequency fitted on earlier
    seasons) the expected shares are the probability-weighted mixture over his presence/absence scenarios;
    without it he stays out of the pool and every row reports ho_scenarios = 0 (status quo, labeled).
    `listing_absence`: the iwh-0.1.1 rule (_Cut.injured_absence: participation and missed_injured); False is the
    evaluated iwh-0.1.0."""
    cut = _cut(sources, season, target_week, cutoff_week, roster_codes, listing_absence)
    tl = timeline if timeline is not None else pl.DataFrame(_timeline_rows(cut), schema=TIMELINE_SCHEMA, orient="row")
    tl = tl.filter(pl.col("position").is_in(SKILL))
    if tl.height == 0:
        return pl.DataFrame(schema=HANDOFF_SCHEMA)
    teams = dict(zip(tl["gsis_id"].to_list(), tl["team"].to_list()))
    roles, newcomer = _roles(cut, teams)
    role = {r["gsis_id"]: r for r in roles.iter_rows(named=True)}
    vol = _team_volume(cut.panel, season)
    gs = _group_shares(cut.panel, season)
    rates = _xfpl_rates(cut.panel)
    # raw last-5 opportunities per game (any games), for ho_shift_pg
    recent = (cut.panel.sort(["season", "week"], descending=True).group_by("gsis_id", maintain_order=True)
              .head(5).group_by("gsis_id").agg((pl.col("carries") + pl.col("targets")).mean().alias("opp5")))
    opp5 = dict(zip(recent["gsis_id"].to_list(), recent["opp5"].to_list()))
    healthy = cut.panel.filter(pl.col("healthy") & pl.col("xfpl").is_not_null()).sort(["season", "week"], descending=True) \
        .group_by("gsis_id", maintain_order=True).head(HEALTHY_WINDOW)
    mult_in = {g: (xs, cs, ts, n) for g, xs, cs, ts, n in healthy.group_by("gsis_id").agg(
        pl.col("xfpl").sum(), pl.col("carries").sum(), pl.col("targets").sum(), pl.len()).iter_rows()}
    info = {r["gsis_id"]: r for r in tl.iter_rows(named=True)}
    horizon = target_week - cut.cutoff
    out = []
    for team, grp in tl.group_by("team"):
        team = team[0]
        members = grp["gsis_id"].to_list()
        rush_pg, tgt_pg = vol.get(team, vol["__league__"])
        claims = {"car": {}, "tgt": {}, "gl": {}}
        part = {}
        for g in members:
            r = role.get(g)
            part[g] = _participation(cut, g, team)
            if r is None:   # no healthy game in the lookback: the newcomer prior (no role yet for a scratch to cut)
                cs, ts, gls = newcomer[info[g]["position"]]
                part[g] = 1.0
            else:
                cs, ts, gls = r["role_car_share"], r["role_tgt_share"], r["role_gl_share"]
            claims["car"][g], claims["tgt"][g], claims["gl"][g] = cs * part[g], ts * part[g], gls * part[g]
        status = {g: info[g]["status_class"] for g in members}
        out_now = {g for g in members if status[g] == UNAVAILABLE}
        uncertain = {g for g in members if status[g] == UNCERTAIN}
        if not horizon:
            uncertain_mix = set()          # horizon 0: Questionable players stay in the pool (see docstring)
        else:
            uncertain_mix = uncertain if return_rates is not None else set()
            out_now |= (uncertain - uncertain_mix)
        returning_now = {g for g in members if status[g] == RETURNING}
        # unavailable for a non-injury reason (practice squad, released, exempt...) or out for TEAM_WINDOW+ team games
        # (long-term reserve): not part of the recent room, so not in the everyone-available baseline either (a
        # recently injured player is)
        not_in_room = {g for g in out_now if not _injury_absent(info[g])
                       or (info[g]["missed_team_games"] or 0) >= TEAM_WINDOW}
        room_games = {"car": _team_game_shares(cut, team, "car_sh"), "tgt": _team_game_shares(cut, team, "tgt_sh")}
        cache: dict[tuple, tuple[dict, dict]] = {}

        def room(metric, cl, pool, absent, gshare, games, first):
            k = (metric, pool, absent, gshare)
            if k not in cache:
                present = {i for i in pool if cl[i] >= MATERIAL_CLAIM}
                ev = _room_evidence(games, first, set(pool), present, sorted(absent))
                cache[k] = (_allocate(cl, set(pool), gshare, ev), ev)
            return cache[k]

        for g in members:
            pos = info[g]["position"]
            groups = {"car": [m for m in members if info[m]["position"] == "RB"] if pos == "RB" else [],
                      "tgt": [m for m in members if info[m]["position"] == pos]}
            res = {}
            n_abs_used = 0
            scen_n = 0
            for metric, mem in groups.items():
                if not mem:
                    res[metric] = (None, None)
                    continue
                gshare = gs.get((team, "RB_car" if metric == "car" else f"{pos}_tgt"),
                                gs[("__league__", "RB_car" if metric == "car" else f"{pos}_tgt")])
                cl = claims[metric]
                mix = sorted([m for m in uncertain_mix if m in mem and m != g], key=lambda m: -cl[m])[:MAX_SCENARIO_PLAYERS]
                fixed_out = (out_now | (uncertain_mix - set(mix))) & set(mem)
                fixed_out.discard(g)
                scenarios = [((), 1.0)]
                for m in mix:
                    p = _return_rate(return_rates, info[m], horizon)
                    scenarios = [(s + ((m, True),), w * p) for s, w in scenarios] + \
                                [(s + ((m, False),), w * (1 - p)) for s, w in scenarios]
                scen_n = max(scen_n, len(mix))
                games, first = room_games[metric]
                exp = 0.0
                for s, wgt in scenarios:
                    absent = set(fixed_out) | {m for m, back in s if not back}
                    pool = (set(mem) - absent) | {g}
                    alloc, ev = room(metric, cl, frozenset(pool), frozenset(_key_absentees(cl, absent)), gshare,
                                     games, first)
                    if g in ev and absent:
                        n_abs_used = max(n_abs_used, ev[g][0])
                    exp += wgt * alloc.get(g, 0.0)
                base, _ = room(metric, cl, frozenset((set(mem) - not_in_room) | {g}), frozenset(), gshare, games, first)
                res[metric] = (exp, base.get(g, 0.0), gshare)
            car, tgt = res["car"], res["tgt"]
            rec = {c: None for c in HANDOFF_SCHEMA}
            rec.update(gsis_id=g, ho_team=team, ho_team_rush_pg=rush_pg, ho_team_tgt_pg=tgt_pg,
                       ho_claim=claims["car" if pos == "RB" else "tgt"][g], ho_participation=part[g],
                       ho_absence_games=n_abs_used, ho_scenarios=scen_n,
                       ho_self_returning=int(status[g] == RETURNING or (horizon > 0 and status[g] == UNCERTAIN)))
            if car[0] is not None:
                rec.update(ho_car_share=car[0], ho_base_car_share=car[1], ho_carries=car[0] * rush_pg)
            rec.update(ho_tgt_share=tgt[0], ho_base_tgt_share=tgt[1], ho_targets=tgt[0] * tgt_pg,
                       ho_group_share=(car[2] if car[0] is not None else tgt[2]))
            # goal-line share follows the carry reallocation ratio (no separate goal-line pool: too sparse)
            r = role.get(g)
            if car[0] is not None and r is not None and car[1]:
                rec["ho_gl_share"] = min(1.0, r["role_gl_share"] * part[g] * car[0] / car[1])
            a, b, avg = rates[pos]
            xs, cs, ts, n = mult_in.get(g, (0.0, 0.0, 0.0, 0))
            mult = (xs + XFPL_SHRINK_GAMES * avg) / ((a * cs + b * ts) + XFPL_SHRINK_GAMES * avg)
            rec["ho_exp_xfpl"] = mult * (a * (rec["ho_carries"] or 0.0) + b * rec["ho_targets"])
            if g in opp5:
                rec["ho_shift_pg"] = (rec["ho_carries"] or 0.0) + rec["ho_targets"] - opp5[g]
            # who is out / returning in his opportunity group (largest established claim first)
            key_metric = "car" if pos == "RB" else "tgt"
            mem = groups[key_metric]
            cl = claims[key_metric]
            tot = sum(cl[m] for m in mem) or None
            gone = sorted((m for m in mem if m != g and m in out_now | uncertain_mix), key=lambda m: -cl[m])
            back = sorted((m for m in mem if m != g and m in returning_now), key=lambda m: -cl[m])
            if tot:
                rec["ho_vacated"] = sum(cl[m] for m in gone) / tot
                rec["ho_returning"] = sum(cl[m] for m in back) / tot
            if gone:
                rec.update(ho_top_absent=gone[0], ho_top_absent_share=role.get(gone[0], {}).get(
                    "role_car_share" if pos == "RB" else "role_tgt_share"))
            if back:
                rec["ho_top_returning"] = back[0]
            lead_share = LEAD_SHARE[pos]
            share_of = (lambda m: (role.get(m) or {}).get("role_car_share" if pos == "RB" else "role_tgt_share") or 0.0)
            leads = [m for m in mem if share_of(m) >= lead_share]
            rec["ho_is_lead"] = int(g in leads)
            rec["ho_lead_out"] = int(any(m in out_now for m in leads if m != g))
            rec["ho_lead_returning"] = int(any(m in returning_now for m in leads if m != g))
            out.append(rec)
    return pl.DataFrame(out, schema=HANDOFF_SCHEMA, orient="row").sort("gsis_id")


# ----------------------------------------------------------------------------- horizon >= 1: empirical return rates


def missed_bucket(missed: int | None) -> str:
    m = missed or 0
    return "0" if m == 0 else "1" if m == 1 else "2-3" if m <= 3 else "4+"


RETURN_MAX_HORIZON = 4      # the table's horizons (team games after the cutoff); a later game uses the last one
RETURN_MIN_N = 30           # a thinner cell pools over positions, then over missed-game buckets


def return_rate_cell(rates: dict | None, position: str | None, missed: int | None, horizon: int) -> tuple | None:
    """The table cell used for (position, games already missed, h team games after the cutoff): the key and its
    {rate, n}, or None when no cell has RETURN_MIN_N episodes. h is capped at the table's largest horizon."""
    hs = [k[2] for k in (rates or {})]
    h = min(max(horizon, 1), max(hs) if hs else RETURN_MAX_HORIZON)
    b = missed_bucket(missed)
    for k in ((position, b, h), ("ALL", b, h), ("ALL", "ALL", h)):
        cell = (rates or {}).get(k)
        if cell and cell["n"] >= RETURN_MIN_N:
            return k, cell
    return None


def _return_rate(rates: dict | None, row: dict, horizon: int) -> float:
    """Empirical P(appears in the team's h-th game after the cutoff | injured at the cutoff), by position and
    games already missed; pooled over positions, then over buckets, when a cell is thin. Never a fixed guess:
    without a fitted table the caller keeps the player out (status quo) instead."""
    hit = return_rate_cell(rates, row.get("position"), row.get("missed_team_games"), min(max(horizon, 1), 4))
    return hit[1]["rate"] if hit else 0.0


def fit_return_rates(sources: Sources, through_key: int, seasons: tuple[int, ...] | None = None,
                     max_horizon: int = RETURN_MAX_HORIZON, active_roster_only: bool = False) -> dict:
    """{(position|'ALL', missed bucket|'ALL', h): {rate, n}}: among players designated Out or Doubtful for their
    team's week-W game (with a lookback appearance), the share who appeared in the team's h-th game after W
    (h = 1..max_horizon, byes skipped). Only episodes whose outcome game is strictly before `through_key` count, so
    a table fitted for a forecast made at week W uses no outcome at or after W.

    `active_roster_only` (ros-avail-0.1.1, docs/research/ros_availability.md): an episode counts only if the player's
    latest weekly roster row with week <= W is ACT/INA, the population a rest-of-season run applies the table to
    (reserve-list players are withheld and never get a rest-of-season p_play; mixing them in under-predicts returns)."""
    if sources.injuries is None:
        return {}
    inj = sources.injuries.filter(pl.col("report_status").str.to_lowercase().is_in(list(UNAVAILABLE_DESIGNATIONS))
                                  & ~pl.col("not_injury").fill_null(False))
    if seasons is not None:
        inj = inj.filter(pl.col("season").is_in(list(seasons)))
    games = sources.games.filter(pl.col("points_for").is_not_null())
    sched: dict[tuple[int, str], list[int]] = defaultdict(list)
    for s, t, w in games.select("season", "team", "week").sort("week").iter_rows():
        sched[(int(s), t)].append(int(w))
    apps = set(sources.panel.select("season", "week", "gsis_id").iter_rows())
    roster: dict[tuple[int, str], list[tuple[int, str]]] = defaultdict(list)
    if active_roster_only and sources.rosters is not None:
        for s, w, g, st in sources.rosters.select("season", "week", "gsis_id", "status").sort("week").iter_rows():
            roster[(int(s), g)].append((int(w), st))

    def active_at(s: int, g: str, w: int) -> bool:
        rows = [st for wk, st in roster.get((s, g), []) if wk <= w]
        return bool(rows) and rows[-1] in ACTIVE_ROSTER
    first_app: dict[str, int] = {}
    pos_of: dict[str, str] = {}
    for s, w, g, p in sources.panel.select("season", "week", "gsis_id", "position").sort("season", "week").iter_rows():
        first_app.setdefault(g, key(s, w))
        pos_of[g] = p
    cnt: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    for s, w, g, t in inj.select("season", "week", "gsis_id", "team").iter_rows():
        if g not in pos_of or pos_of[g] not in SKILL or first_app[g] >= key(s, w):
            continue
        if active_roster_only and not active_at(int(s), g, int(w)):
            continue
        tw = sched.get((int(s), t), [])
        later = [x for x in tw if x > w]
        prior = [x for x in tw if x < w]
        played = [x for x in prior if (s, x, g) in apps]
        missed = len([x for x in prior if not played or x > max(played)]) if prior else 0
        for h in range(1, max_horizon + 1):
            if len(later) < h or key(s, later[h - 1]) >= through_key:
                break
            hit = int((s, later[h - 1], g) in apps)
            for k in ((pos_of[g], missed_bucket(missed), h), ("ALL", missed_bucket(missed), h), ("ALL", "ALL", h)):
                cnt[k][0] += hit
                cnt[k][1] += 1
    return {k: {"rate": v[0] / v[1], "n": v[1]} for k, v in cnt.items() if v[1]}


# ----------------------------------------------------------------------------- explanation metadata


def explain(timeline_row: dict, handoff_row: dict | None, *, names: dict[str, str] | None = None,
            baseline_points: float | None = None, challenger_points: float | None = None) -> dict:
    """Explanation metadata for one player-week (research only). Names evidence, never causes: a teammate is
    named only when he is unavailable/returning AND the handoff moved this player's expected share."""
    names = names or {}
    t, h = timeline_row, handoff_row or {}
    horizon = t.get("horizon") or 0
    share_key = "ho_car_share" if t.get("position") == "RB" else "ho_tgt_share"
    base_key = "ho_base_car_share" if t.get("position") == "RB" else "ho_base_tgt_share"
    cur, base = h.get(share_key), h.get(base_key)
    delta = None if cur is None or base is None else cur - base
    unit = "carries" if t.get("position") == "RB" else "targets"
    who = lambda g: names.get(g, g)  # noqa: E731
    lines = []
    st = t.get("status_class")
    if st == UNAVAILABLE:
        lines.append(f"Not expected to play: {t.get('status_reason')}.")
    elif st == UNCERTAIN:
        lines.append(f"Availability uncertain: {t.get('status_reason')}.")
    elif st == RETURNING:
        lines.append(f"Returning: {t.get('status_reason')}. Healthy workload baseline retained; missed games are "
                     "not counted as zero-workload games.")
    if (t.get("missed_team_games") or 0) >= 2 and st != UNAVAILABLE and t.get("last_healthy_week") is not None:
        lines.append(f"Historical healthy workload retained despite {t['missed_team_games']} missed games "
                     f"(last healthy game {t['last_healthy_season']} week {t['last_healthy_week']}).")
    if delta is not None and h.get("ho_top_absent") and delta >= 0.03 and st != UNAVAILABLE:
        lines.append(f"Potential expanded role while {who(h['ho_top_absent'])} is unavailable: expected share "
                     f"{cur:.0%} of team {unit} vs {base:.0%} with everyone available, from prior usage and the "
                     "remaining competition.")
    if h.get("ho_top_returning") and st not in (UNAVAILABLE,) and (h.get("ho_returning") or 0) > 0:
        lines.append(f"{who(h['ho_top_returning'])} is back from injury: temporary workload gained in his absence "
                     "does not carry over.")
    if horizon:
        lines.append(f"Projection {horizon} week(s) ahead: no injury report exists yet for that week."
                     + (f" Teammate return scenarios weighted by historical return rates ({h['ho_scenarios']})."
                        if h.get("ho_scenarios") else ""))
    exp_opps = None if h.get("ho_targets") is None else (h.get("ho_carries") or 0.0) + h["ho_targets"]
    return {
        "model": WORKLOAD_VERSION, "status": "research_only",
        "injury": {k: t.get(k) for k in ("designation", "report_status", "practice_status", "injury", "roster_status",
                                          "nfl_reserve", "reserve_earliest_week", "status_class", "status_reason",
                                          "confidence")},
        "last_healthy_game": None if t.get("last_healthy_week") is None else
        {"season": t["last_healthy_season"], "week": t["last_healthy_week"]},
        "missed_team_games": t.get("missed_team_games"), "return_evidence": t.get("return_evidence"),
        "healthy_baseline": {"carry_share": h.get("ho_base_car_share"), "target_share": h.get("ho_base_tgt_share")},
        "expected_role": {"carry_share": h.get("ho_car_share"), "target_share": h.get("ho_tgt_share"),
                          "carries": h.get("ho_carries"), "targets": h.get("ho_targets"), "opportunities": exp_opps,
                          "goal_line_share": h.get("ho_gl_share")},
        "teammate_change": {"absent": h.get("ho_top_absent"), "returning": h.get("ho_top_returning"),
                            "vacated_claim_share": h.get("ho_vacated"), "returning_claim_share": h.get("ho_returning")},
        "points": {"baseline": baseline_points, "challenger": challenger_points},
        "horizon": horizon, "text": lines,
        "caveats": ["Research model, not production. Expected shares are estimates from prior usage, not "
                    "guarantees of touches.", "Conditional on the player appearing; P(appears) is reported separately."],
    }


__all__ = ["WORKLOAD_VERSION", "TIMELINE_DOCS", "STATUS_CLASSES", "injury_timeline", "established_roles",
           "workload_handoff", "fit_return_rates", "load_roster_codes", "explain"]
