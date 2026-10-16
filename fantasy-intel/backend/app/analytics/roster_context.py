"""Point-in-time expected-starting-QB, QB-quality and teammate-availability context (research only).

Nothing in the app imports this module (tests/test_roster_context.py). It feeds the research
challenger in app.analytics.context_challenger; ifp-1.0.0 and ifp-2.0.0 never read it.
See docs/research/qb_teammate_context.md for every definition and the evidence.

Leakage contract (tested in tests/test_roster_context.py):
  * A context row for (season S, week W) uses only games strictly before (S, W): box scores and
    snaps (Sources.panel), the starting QB of each PAST game (nfl_schedules home/away_qb_id, a
    postgame fact about that game), QB passing stats and weekly roster statuses of earlier weeks.
  * The only week-W input is the pregame injury report (Sources.injuries). forecast_features
    .load_injuries drops rows modified at or after kickoff where nflverse records date_modified
    (2019-2024). Later rows carry no timestamp, so features built from them are retrospective.
  * Never read: the week-W starting QB, week-W roster statuses (ACT/INA is the game-day inactive
    decision), depth charts (no publication time before 2025, upstream licence unverified) and
    betting lines (closing lines).
  * Unknown stays null, a measured zero stays zero. Without a week-W injury report the
    report-dependent quantities are null, never 0, and QB confidence is capped at "probable".

No fantasy-point adjustment is made here. These are inputs; any effect on projected points is a
coefficient fitted walk-forward by the challenger.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

import polars as pl

from ..db.connection import table_columns, table_exists
from .forecast_features import Sources, key

CONTEXT_VERSION = "ctx-0.1.1"   # 0.1.1: prior-season QBs need current-season roster evidence

# Fixed before any backtest; derivations in docs/research/qb_teammate_context.md.
QB_LOOKBACK_SEASONS = 2       # QB profile: current season + this many previous seasons
QB_SHRINK_DROPBACKS = 200.0   # EPA/dropback shrinks toward the replacement-level prior by this many dropbacks
QB_SHRINK_ATTEMPTS = 200.0    # CPOE shrinks by this many attempts
LOW_VOLUME_QB_SEASON = 200    # QB-seasons under this many dropbacks define the replacement-level prior
TAKEOVER_ATTEMPTS = 10        # another QB out-threw the starter with >= this many attempts in the team's last game
TEAM_WINDOW = 5               # team games in the teammate window (current season, before W)
STALE_WEEKS = 6               # a teammate whose last game for the team is older than this is not "returning"
MIN_CLAIM = 0.2               # floor of the remaining-claims normaliser (nearly whole room unavailable)
ENV_SHRINK_PLAYS = 150.0      # team PROE: plays
ENV_SHRINK_DROPBACKS = 100.0  # opponent pressure: dropbacks faced
ENV_SHRINK_TARGETS = 100.0    # opponent positional target share: targets faced

ACTIVE_ROSTER = ("ACT", "INA")
CONFIDENCE = ("supported", "probable", "uncertain", "unknown")
UNAVAILABLE = ("out", "doubtful", "gone", "inactive")
REPORT_MODES = ("week", "none")   # "none": horizon > 0, the target week's report does not exist yet

# name -> (meaning, provenance). All are pregame for week W.
CONTEXT_DOCS: dict[str, tuple[str, str]] = {
    "qb_confidence": ("Expected-starter evidence: supported (previous starter, not on the week-W report, no takeover "
                      "last game) | probable | uncertain | unknown", "nfl_schedules (past games) + nfl_injuries (W)"),
    "exp_qb_epa": (f"Expected starter's EPA per dropback (attempts + sacks), current + {QB_LOOKBACK_SEASONS} previous "
                   f"seasons before W, shrunk toward the replacement-level prior by {QB_SHRINK_DROPBACKS:g} dropbacks",
                   "nfl_weekly_stats passing_epa"),
    "exp_qb_cpoe": (f"Expected starter's CPOE, attempt-weighted, shrunk by {QB_SHRINK_ATTEMPTS:g} attempts",
                    "nfl_weekly_stats passing_cpoe"),
    "exp_qb_dropbacks": ("Dropbacks behind exp_qb_epa (0 = known QB with no passing history: the prior)", "derived"),
    "recent_qb_epa": ("Mean exp-style EPA/dropback (as of W) of the starting QBs in the player's last <=5 games",
                      "nfl_schedules + nfl_weekly_stats"),
    "qb_epa_delta": ("exp_qb_epa - recent_qb_epa: expected QB quality relative to the QBs behind the player's recent "
                     "stats (null if either is unknown)", "derived"),
    "qb_cpoe_delta": ("Expected starter's CPOE - mean CPOE of the starters in the player's last <=5 games", "derived"),
    "qb_changed": ("1 if the expected starter differs from the most frequent starter in the player's last <=5 games",
                   "derived"),
    "qb_is_expected": ("1 if this player is his team's expected starting QB (null when unknown)", "derived"),
    "qb_self_epa": ("This QB's own shrunk EPA/dropback (QB rows only)", "nfl_weekly_stats"),
    "tm_tgt_vacated": ("Share of team targets, in the team games the player played among its last <=5, that went to "
                       "teammates unavailable in W (Out/Doubtful on the W report, off the team, or a non-active roster "
                       "status before W)", "panel + nfl_injuries (W) + nfl_rosters_weekly (< W)"),
    "tm_tgt_returning": ("Target share reclaimed by available teammates who missed some of those games: their share "
                         "when playing x fraction missed", "panel + nfl_injuries (W) + nfl_rosters_weekly (< W)"),
    "tm_car_vacated": ("tm_tgt_vacated for carries / team rush attempts", "panel + availability"),
    "tm_car_returning": ("tm_tgt_returning for carries", "panel + availability"),
    "tm_tgt_gain_pg": ("Targets per game the player would gain if vacated (or reclaimed) volume were redistributed in "
                       "proportion to remaining claims: (s / (1 - vacated + returning) - s) x team targets per game",
                       "derived (team opportunity budget)"),
    "tm_car_gain_pg": ("tm_tgt_gain_pg for carries", "derived (team opportunity budget)"),
    "team_proe": (f"Team pass rate over expected this season before W, play-weighted, shrunk toward 0 by "
                  f"{ENV_SHRINK_PLAYS:g} plays", "nfl_team_week"),
    "opp_pressure": (f"Hit-or-sack rate the opponent's defense generated this season before W minus league, shrunk by "
                     f"{ENV_SHRINK_DROPBACKS:g} dropbacks", "nfl_team_week (opponents' offense rows)"),
    "opp_pos_tgt_share": (f"Share of targets the opponent allowed to this position this season before W minus league, "
                          f"shrunk by {ENV_SHRINK_TARGETS:g} targets", "nfl_defense_week"),
}


# ----------------------------------------------------------------------------- loading


@dataclass
class ContextSources:
    starters: pl.DataFrame               # season, week, team, qb_id (starting QB of that past game; may be null)
    qb_stats: pl.DataFrame               # season, week, gsis_id, team, attempts, sacks, epa, cpoe
    team_env: pl.DataFrame | None = None  # nfl_team_week offense rows
    def_targets: pl.DataFrame | None = None

    @staticmethod
    def concat(parts: list["ContextSources"]) -> "ContextSources":
        """Same rule as Sources.concat: a season present in several parts is kept from the LAST part."""
        seen: set[int] = set()
        keep: list[ContextSources] = []
        for part in reversed(parts):
            new = set(part.starters["season"].unique().to_list()) - seen
            seen |= new
            flt = pl.col("season").is_in(sorted(new))
            keep.append(ContextSources(part.starters.filter(flt), part.qb_stats.filter(flt),
                                       None if part.team_env is None else part.team_env.filter(flt),
                                       None if part.def_targets is None else part.def_targets.filter(flt)))
        keep.reverse()

        def opt(name):
            frames = [getattr(k, name) for k in keep if getattr(k, name) is not None]
            return pl.concat(frames) if frames else None

        return ContextSources(pl.concat([k.starters for k in keep]), pl.concat([k.qb_stats for k in keep]),
                              opt("team_env"), opt("def_targets"))


STARTER_SCHEMA = {"season": pl.Int64, "week": pl.Int64, "team": pl.String, "qb_id": pl.String}
QB_SCHEMA = {"season": pl.Int64, "week": pl.Int64, "gsis_id": pl.String, "team": pl.String, "attempts": pl.Float64,
             "sacks": pl.Float64, "epa": pl.Float64, "cpoe": pl.Float64}
ENV_SCHEMA = {"season": pl.Int64, "week": pl.Int64, "team": pl.String, "opponent": pl.String, "proe": pl.Float64,
              "proe_plays": pl.Float64, "dropbacks": pl.Float64, "hit_or_sack_rate": pl.Float64}
DEF_SCHEMA = {"season": pl.Int64, "week": pl.Int64, "defteam": pl.String, "targets": pl.Float64,
              "rb_targets": pl.Float64, "wr_targets": pl.Float64, "te_targets": pl.Float64}


def _where(seasons, col="season") -> str:
    return f"AND {col} IN ({', '.join(str(int(s)) for s in seasons)})" if seasons else ""


def load_context_sources(con, seasons: list[int] | None = None) -> ContextSources:
    w = _where(seasons)
    starters = pl.DataFrame(schema=STARTER_SCHEMA)
    if table_exists(con, "nfl_schedules") and {"home_qb_id", "away_qb_id"} <= set(table_columns(con, "nfl_schedules")):
        starters = pl.from_arrow(con.execute(f"""
            SELECT season, week, home_team AS team, home_qb_id AS qb_id FROM nfl_schedules WHERE game_type = 'REG' {w}
            UNION ALL
            SELECT season, week, away_team, away_qb_id FROM nfl_schedules WHERE game_type = 'REG' {w}""").arrow())
        starters = starters.cast(STARTER_SCHEMA, strict=False).select(list(STARTER_SCHEMA))
    qb = pl.DataFrame(schema=QB_SCHEMA)
    need = {"player_id", "attempts", "sacks_suffered", "passing_epa", "passing_cpoe", "position", "season_type"}
    if table_exists(con, "nfl_weekly_stats") and need <= set(table_columns(con, "nfl_weekly_stats")):
        qb = pl.from_arrow(con.execute(f"""
            SELECT season, week, player_id AS gsis_id, team, attempts, sacks_suffered AS sacks, passing_epa AS epa,
                   passing_cpoe AS cpoe
            FROM nfl_weekly_stats WHERE season_type = 'REG' AND position = 'QB' AND player_id IS NOT NULL {w}""").arrow())
        qb = qb.cast(QB_SCHEMA, strict=False).select(list(QB_SCHEMA))
    env = None
    if table_exists(con, "nfl_team_week") and {"proe", "proe_plays", "dropbacks", "hit_or_sack_rate", "opponent"} <= set(
            table_columns(con, "nfl_team_week")):
        env = pl.from_arrow(con.execute(f"SELECT season, week, team, opponent, proe, proe_plays, dropbacks, "
                                        f"hit_or_sack_rate FROM nfl_team_week WHERE true {w}").arrow())
        env = env.cast(ENV_SCHEMA, strict=False).select(list(ENV_SCHEMA))
    dt = None
    if table_exists(con, "nfl_defense_week") and {"targets", "rb_targets", "wr_targets", "te_targets"} <= set(
            table_columns(con, "nfl_defense_week")):
        dt = pl.from_arrow(con.execute(f"SELECT season, week, defteam, targets, rb_targets, wr_targets, te_targets "
                                       f"FROM nfl_defense_week WHERE true {w}").arrow())
        dt = dt.cast(DEF_SCHEMA, strict=False).select(list(DEF_SCHEMA))
    return ContextSources(starters, qb, env, dt)


# ----------------------------------------------------------------------------- pure helpers


def shrink(total: float, n: float, prior: float, k: float) -> float:
    """(total + prior * k) / (n + k): with n = 0 the estimate IS the prior (not a fabricated value)."""
    return (total + prior * k) / (n + k)


def redistribute(claims: dict[str, float], unavailable: set[str]) -> dict[str, float | None]:
    """Team opportunity budget: each AVAILABLE player's expected share is his claim divided by the
    sum of available claims, so the shares of available players sum to 1 and an unavailable player
    receives nothing (None). Claims: a present player's window share; a returning player adds his
    share-when-playing x fraction of games missed."""
    pool = sum(v for p, v in claims.items() if p not in unavailable)
    return {p: (None if p in unavailable or pool <= 0 else v / pool) for p, v in claims.items()}


def expected_share(share: float, vacated: float, returning: float) -> float:
    """One player's side of `redistribute`: his window shares sum to 1 with his teammates', the
    unavailable ones' claims (vacated) leave the pool and returning claims join it."""
    return share / max(1.0 - vacated + returning, MIN_CLAIM)


# ----------------------------------------------------------------------------- state as of week W


@dataclass
class _State:
    season: int
    week: int
    report: dict[str, str | None] | None     # gsis -> report_status for week W (None: no report used)
    roster: dict[str, tuple[str, str]]       # gsis -> (team, status) from the latest roster week < W this season
    last_team: dict[str, str]                # gsis -> team of the latest panel appearance before W (lookback)
    report_mode: str

    def availability(self, pid: str, team: str) -> str:
        """available | questionable | doubtful | out | gone | inactive (pregame for week W)."""
        r = self.roster.get(pid)
        if r is not None:
            if r[0] != team:
                return "gone"
            if r[1] not in ACTIVE_ROSTER:
                return "inactive"
        elif self.last_team.get(pid, team) != team:
            return "gone"
        if self.report is not None:
            st = (self.report.get(pid) or "").strip().lower()
            if st in ("out", "doubtful", "questionable"):
                return st
        return "available"

    def listed(self, pid: str) -> bool:
        return self.report is not None and pid in self.report


def _state(sources: Sources, season: int, week: int, report_mode: str) -> _State:
    if report_mode not in REPORT_MODES:
        raise ValueError(f"report_mode must be one of {REPORT_MODES}")
    cutoff = key(season, week)
    report = None
    if report_mode == "week" and sources.injuries is not None:
        rows = sources.injuries.filter((pl.col("season") == season) & (pl.col("week") == week))
        if rows.height:
            report = dict(zip(rows["gsis_id"].to_list(), rows["report_status"].to_list()))
    roster: dict[str, tuple[str, str]] = {}
    if sources.rosters is not None:
        r = (sources.rosters.filter((pl.col("season") == season) & (pl.col("week") < week))
             .sort("week", descending=True).unique("gsis_id", keep="first"))
        roster = {g: (t, s) for g, t, s in r.select("gsis_id", "team", "status").iter_rows()}
    p = (sources.panel.filter((pl.col("season") * 100 + pl.col("week") < cutoff) & (pl.col("season") >= season - 1))
         .sort(["season", "week"], descending=True).unique("gsis_id", keep="first"))
    return _State(season, week, report, roster, dict(zip(p["gsis_id"].to_list(), p["team"].to_list())), report_mode)


# ----------------------------------------------------------------------------- QB profiles


def qb_profiles(ctx: ContextSources, season: int, week: int) -> tuple[dict[str, dict], dict]:
    """Shrunk QB quality as of week W: {gsis: {epa, cpoe, dropbacks, attempts}} and the prior.
    The prior is the replacement level: pooled rates of QB-seasons with < LOW_VOLUME_QB_SEASON
    dropbacks in the completed seasons of the window, so a 15-attempt backup is pulled toward backups,
    not toward the league's starters."""
    cutoff = key(season, week)
    rows = ctx.qb_stats.filter((pl.col("season") * 100 + pl.col("week") < cutoff)
                               & (pl.col("season") >= season - QB_LOOKBACK_SEASONS))
    rows = rows.with_columns((pl.col("attempts").fill_null(0) + pl.col("sacks").fill_null(0)).alias("db"),
                             pl.col("cpoe").is_not_null().alias("_c"))
    agg = lambda by: rows.group_by(by).agg(  # noqa: E731
        pl.col("db").sum(), pl.col("epa").fill_null(0).sum().alias("epa_sum"),
        (pl.col("cpoe") * pl.col("attempts")).filter(pl.col("_c")).sum().alias("cpoe_sum"),
        pl.col("attempts").filter(pl.col("_c")).sum().alias("c_att"), pl.col("attempts").sum().alias("att"))
    per_qb = agg(["gsis_id"])
    # completed seasons only: a starter's partial current season is not "low volume"
    seasons = agg(["gsis_id", "season"])
    done = seasons.filter(pl.col("season") < season)
    low = done.filter(pl.col("db") < LOW_VOLUME_QB_SEASON)
    base = low if low["db"].sum() and low["c_att"].sum() else (done if done.height else seasons)

    def rate(df, num, den):
        d = float(df[den].sum()) if df.height else 0.0
        return float(df[num].sum()) / d if d else 0.0

    prior = {"epa": rate(base, "epa_sum", "db"), "cpoe": rate(base, "cpoe_sum", "c_att")}
    out = {}
    for g, db, es, cs, ca, att in per_qb.select("gsis_id", "db", "epa_sum", "cpoe_sum", "c_att", "att").iter_rows():
        out[g] = {"epa": shrink(es, db, prior["epa"], QB_SHRINK_DROPBACKS),
                  "cpoe": shrink(cs, ca, prior["cpoe"], QB_SHRINK_ATTEMPTS), "dropbacks": float(db),
                  "attempts": float(att)}
    return out, prior


def profile_of(profiles: dict, prior: dict, qb: str | None) -> dict | None:
    """A known QB's profile; a known QB with no passing history gets the prior with 0 dropbacks
    (the shrinkage estimate at n = 0); an unknown QB (None) gets None."""
    if qb is None:
        return None
    return profiles.get(qb) or {"epa": prior["epa"], "cpoe": prior["cpoe"], "dropbacks": 0.0, "attempts": 0.0}


# ----------------------------------------------------------------------------- expected starter


def expected_starters(sources: Sources, ctx: ContextSources, season: int, week: int, teams: list[str],
                      report_mode: str = "week", state: _State | None = None) -> dict[str, dict]:
    """Per team: expected starting QB for week W with an evidence level and the reason. No
    probability is invented: the confidence label is an ordinal evidence class whose empirical
    start rate is measured (and reported) by the research script, not assumed."""
    st = state or _state(sources, season, week, report_mode)
    cutoff = key(season, week)
    past = ctx.starters.filter((pl.col("season") == season) & (pl.col("week") < week)).sort("week")
    last_start = {t: (w, q) for t, w, q in past.select("team", "week", "qb_id").iter_rows()}
    starts = Counter((t, q) for t, q in past.select("team", "qb_id").iter_rows() if q is not None)
    qbs = ctx.qb_stats.filter((pl.col("season") * 100 + pl.col("week") < cutoff) & (pl.col("season") >= season - 1))
    db_team = Counter()
    for g, t, a, s in qbs.select("gsis_id", "team", "attempts", "sacks").iter_rows():
        db_team[(t, g)] += (a or 0) + (s or 0)
    pq = sources.panel.filter((pl.col("season") * 100 + pl.col("week") < cutoff) & (pl.col("season") >= season - 1)
                              & (pl.col("position") == "QB"))
    team_qbs: dict[str, set] = defaultdict(set)
    latest = pq.sort(["season", "week"], descending=True).unique("gsis_id", keep="first")
    for g, t, s in latest.select("gsis_id", "team", "season").iter_rows():
        if st.last_team.get(g) != t:
            continue
        # a QB last seen for the team in a PREVIOUS season (offseason departure, release, retirement) needs
        # current-season roster evidence; a missing roster row is not evidence he is still there
        if s < season and not (st.roster.get(g, ("", ""))[0] == t and st.roster[g][1] in ACTIVE_ROSTER):
            continue
        team_qbs[t].add(g)
    if sources.rosters is not None:   # QBs on a team's roster before W who never appeared (a new signing)
        r = sources.rosters.filter((pl.col("season") == season) & (pl.col("week") < week) & (pl.col("position") == "QB"))
        for g in set(r["gsis_id"].to_list()):
            if g in st.roster and st.roster[g][1] in ACTIVE_ROSTER:
                team_qbs[st.roster[g][0]].add(g)
    out = {}
    for team in teams:
        rec = {"team": team, "prev_starter_id": None, "prev_starter_status": None, "expected_qb_id": None,
               "qb_confidence": "unknown", "qb_reason": None, "alt_qb_id": None}
        out[team] = rec
        if team not in last_start:
            rec["qb_reason"] = "no_game_this_season"
            continue
        lw, prev = last_start[team]
        if prev is None:
            rec["qb_reason"] = "starter_id_missing"
            continue
        rec["prev_starter_id"] = prev
        status = st.availability(prev, team)
        rec["prev_starter_status"] = status
        last = qbs.filter((pl.col("season") == season) & (pl.col("week") == lw) & (pl.col("team") == team))
        att = {g: (a or 0.0) for g, a in last.select("gsis_id", "attempts").iter_rows()}
        reliever = max((g for g in att if g != prev), key=lambda g: (att[g], g), default=None)
        takeover = reliever is not None and att[reliever] >= TAKEOVER_ATTEMPTS and att[reliever] > att.get(prev, 0.0)
        # a returning incumbent: more starts this season than the previous starter, last start within
        # STALE_WEEKS, and pregame evidence that he is back (listed on the W report without an Out/Doubtful
        # designation, or inactive - not reserve - for the last game). Two credible starters: "uncertain"
        # either way (the measured start rate of this class is reported, never assumed).
        inc = max((g for (t, g), n in starts.items() if t == team and g != prev and n > starts[(team, prev)]
                   and max(w for t2, w, q in past.select("team", "week", "qb_id").iter_rows()
                           if t2 == team and q == g) >= week - STALE_WEEKS), key=lambda g: (starts[(team, g)], g),
                  default=None)
        if inc is not None and status in ("available", "questionable"):
            inc_status = st.availability(inc, team)
            back = st.listed(inc) or st.roster.get(inc, ("", ""))[1] == "INA"
            if inc_status == "available" and back:
                rec.update(expected_qb_id=inc, qb_confidence="uncertain", qb_reason="incumbent_returning",
                           alt_qb_id=prev)
                continue
            if inc_status == "questionable":
                rec.update(expected_qb_id=prev, qb_confidence="uncertain", qb_reason="incumbent_questionable",
                           alt_qb_id=inc)
                continue
        if status in ("available", "questionable"):
            rec["expected_qb_id"] = prev
            if status == "questionable":
                rec.update(qb_confidence="probable", qb_reason="previous_starter_questionable")
            elif takeover:
                rec.update(qb_confidence="uncertain", qb_reason="previous_starter_relieved_last_game",
                           alt_qb_id=reliever)
            elif st.report is None:
                rec.update(qb_confidence="probable", qb_reason="previous_starter_no_report")
            else:
                rec.update(qb_confidence="supported", qb_reason="previous_starter_not_listed")
            continue
        cands = [g for g in team_qbs.get(team, ()) if g != prev
                 and st.availability(g, team) in ("available", "questionable")]
        if not cands:
            rec["qb_reason"] = f"previous_starter_{status}_no_candidate"
            continue

        def evidence(g):
            return (att.get(g, 0.0) >= TAKEOVER_ATTEMPTS, starts[(team, g)], att.get(g, 0.0), db_team[(team, g)])

        cands.sort(key=lambda g: (tuple(-x for x in evidence(g)), g))
        top = cands[0]
        strong = evidence(top)[0] or evidence(top)[1] > 0
        tie = len(cands) > 1 and evidence(cands[1])[:2] == evidence(top)[:2]
        conf = "probable" if strong and not tie else "uncertain"
        if status == "doubtful" or st.availability(top, team) == "questionable":
            conf = "uncertain"     # a Doubtful starter sometimes plays; a Questionable backup may not
        if st.report is None and conf == "supported":
            conf = "probable"
        rec.update(expected_qb_id=top, qb_confidence=conf, qb_reason=f"previous_starter_{status}",
                   alt_qb_id=cands[1] if len(cands) > 1 else None)
    return out


# ----------------------------------------------------------------------------- features


QB_COLUMNS = ("expected_qb_id", "qb_confidence", "qb_reason", "prev_starter_status", "exp_qb_epa", "exp_qb_cpoe",
              "exp_qb_dropbacks", "recent_qb_id", "recent_qb_epa", "qb_epa_delta", "qb_cpoe_delta", "qb_changed",
              "qb_is_expected", "qb_self_epa")
TM_COLUMNS = ("tm_tgt_vacated", "tm_tgt_returning", "tm_car_vacated", "tm_car_returning", "tm_tgt_gain_pg",
              "tm_car_gain_pg", "tm_games", "top_tgt_comp_out", "top_tgt_comp_return", "rb1_out")
ENV_COLUMNS = ("team_proe", "opp_pressure", "opp_pos_tgt_share")


def _schema() -> dict:
    s = {c: pl.Float64 for c in (*QB_COLUMNS, *TM_COLUMNS, *ENV_COLUMNS)}
    s.update(expected_qb_id=pl.String, qb_confidence=pl.String, qb_reason=pl.String, prev_starter_status=pl.String,
             recent_qb_id=pl.String, qb_changed=pl.Int8, qb_is_expected=pl.Int8, top_tgt_comp_out=pl.Int8,
             top_tgt_comp_return=pl.Int8, rb1_out=pl.Int8, tm_games=pl.Int64)
    return {"gsis_id": pl.String, **s, "ctx_report_used": pl.Int8}


def context_for_week(sources: Sources, ctx: ContextSources, season: int, week: int, targets: pl.DataFrame,
                     report_mode: str = "week") -> pl.DataFrame:
    """Context features for `targets` (gsis_id, position, team, opponent) in week (S, W).
    `report_mode="none"` is for forecasts made before week W's report exists (horizon > 0):
    availability then rests on roster status before the forecast week only."""
    schema = _schema()
    if targets.height == 0:
        return pl.DataFrame(schema=schema)
    st = _state(sources, season, week, report_mode)
    teams = sorted(set(targets["team"].drop_nulls().to_list()))
    qbx = expected_starters(sources, ctx, season, week, teams, report_mode, state=st)
    profiles, prior = qb_profiles(ctx, season, week)
    recent = _recent_qb_env(sources, ctx, season, week, profiles, prior)
    tm = _teammates(sources, st, season, week)
    env = _environment(ctx, season, week)
    rows = []
    for g, pos, team, opp in targets.select("gsis_id", "position", "team", "opponent").iter_rows():
        q = qbx.get(team) or {}
        exp_id = q.get("expected_qb_id")
        prof = profile_of(profiles, prior, exp_id)
        rq = recent.get(g, {})
        r = {"gsis_id": g, **{c: None for c in schema if c != "gsis_id"}}
        r.update(expected_qb_id=exp_id, qb_confidence=q.get("qb_confidence", "unknown"), qb_reason=q.get("qb_reason"),
                 prev_starter_status=q.get("prev_starter_status"), recent_qb_id=rq.get("primary"),
                 recent_qb_epa=rq.get("epa"),
                 ctx_report_used=None if report_mode == "none" else int(st.report is not None))
        if prof is not None:
            r.update(exp_qb_epa=prof["epa"], exp_qb_cpoe=prof["cpoe"], exp_qb_dropbacks=prof["dropbacks"],
                     qb_is_expected=int(g == exp_id))
            if rq.get("epa") is not None:
                r["qb_epa_delta"] = prof["epa"] - rq["epa"]
            if rq.get("cpoe") is not None:
                r["qb_cpoe_delta"] = prof["cpoe"] - rq["cpoe"]
            if rq.get("primary") is not None:
                r["qb_changed"] = int(exp_id != rq["primary"])
        if pos == "QB":
            r["qb_self_epa"] = profile_of(profiles, prior, g)["epa"]
        r.update(tm.get((g, team), {}))
        e = env.get(team, {})
        r["team_proe"] = e.get("proe")
        o = env.get(opp, {})
        r["opp_pressure"] = o.get("def_pressure")
        r["opp_pos_tgt_share"] = o.get(f"def_{pos.lower()}_tgt_share") if pos != "QB" else None
        rows.append(r)
    return pl.DataFrame(rows, schema=schema, orient="row")


def _recent_qb_env(sources: Sources, ctx: ContextSources, season: int, week: int, profiles: dict,
                   prior: dict) -> dict[str, dict]:
    """Per player: the starting QBs of his last <= 5 games (any team, same lookback as
    forecast_features) and their mean CURRENT (as-of-W) profile."""
    from .forecast_features import PRIOR_SEASONS, RECENT_GAMES

    cutoff = key(season, week)
    p = (sources.panel.filter((pl.col("season") * 100 + pl.col("week") < cutoff)
                              & (pl.col("season") >= season - PRIOR_SEASONS))
         .sort(["season", "week"], descending=True).group_by("gsis_id", maintain_order=True).head(RECENT_GAMES)
         .join(ctx.starters, on=["season", "week", "team"], how="left"))
    out: dict[str, dict] = {}
    for g, grp in p.group_by("gsis_id", maintain_order=True):
        qs = grp["qb_id"].to_list()
        known = [q for q in qs if q is not None]
        if not known:
            out[g[0]] = {}
            continue
        cnt = Counter(known)
        top = max(cnt.values())
        primary = next(q for q in known if cnt[q] == top)          # ties: the most recent
        profs = [profile_of(profiles, prior, q) for q in known]
        out[g[0]] = {"primary": primary, "epa": sum(x["epa"] for x in profs) / len(profs),
                     "cpoe": sum(x["cpoe"] for x in profs) / len(profs)}
    return out


def _teammates(sources: Sources, st: _State, season: int, week: int) -> dict[tuple[str, str], dict]:
    """Teammate availability and budget features per (player, team), current season only."""
    ps = sources.panel.filter((pl.col("season") == season) & (pl.col("week") < week))
    if ps.height == 0:
        return {}
    tot = (ps.group_by("team", "week").agg(pl.col("team_targets").drop_nulls().first().alias("tt"),
                                            pl.col("team_rush_attempts").drop_nulls().first().alias("tr"),
                                            pl.col("targets").sum().alias("st"), pl.col("carries").sum().alias("sc")))
    totals = {(t, w): (tt if tt is not None else s1, tr if tr is not None else s2)
              for t, w, tt, tr, s1, s2 in tot.select("team", "week", "tt", "tr", "st", "sc").iter_rows()}
    windows: dict[str, list[int]] = defaultdict(list)
    for t, w in sorted(totals, key=lambda x: -x[1]):
        if len(windows[t]) < TEAM_WINDOW:
            windows[t].append(w)
    games: dict[str, dict[str, dict[int, tuple]]] = defaultdict(lambda: defaultdict(dict))
    for g, t, w, pos, tg, ca in ps.select("gsis_id", "team", "week", "position", "targets", "carries").iter_rows():
        games[t][g][w] = (pos, tg or 0.0, ca or 0.0)

    def share_when_playing(t, g):
        ws = sorted(games[t][g], reverse=True)[:TEAM_WINDOW]
        tt = sum(totals[(t, w)][0] or 0 for w in ws)
        tr = sum(totals[(t, w)][1] or 0 for w in ws)
        return (sum(games[t][g][w][1] for w in ws) / tt if tt else 0.0,
                sum(games[t][g][w][2] for w in ws) / tr if tr else 0.0)

    report_missing = st.report_mode == "week" and st.report is None
    out = {}
    for team, win in windows.items():
        avail = {g: st.availability(g, team) for g in games[team]}
        for g in games[team]:
            mine = [w for w in win if w in games[team][g]]
            if not mine:
                continue
            tt = sum(totals[(team, w)][0] or 0 for w in mine)
            tr = sum(totals[(team, w)][1] or 0 for w in mine)
            n = len(mine)
            rec = {"tm_games": n}
            out[(g, team)] = rec
            if report_missing:
                continue      # report-dependent quantities unknown: null, never 0
            s_t = sum(games[team][g][w][1] for w in mine) / tt if tt else None
            s_c = sum(games[team][g][w][2] for w in mine) / tr if tr else None
            vt = vc = rt = rc = 0.0
            top_present, top_val, top_ret_val, rb1, rb1_val = None, -1.0, -1.0, None, -1.0
            for j, played in games[team].items():
                if j == g:
                    continue
                inn = [w for w in mine if w in played]
                jt = sum(played[w][1] for w in inn)
                jc = sum(played[w][2] for w in inn)
                pos_j = next(iter(played.values()))[0]
                if inn and pos_j != "QB" and jt > top_val:
                    top_present, top_val = j, jt
                if inn and pos_j == "RB" and jc > rb1_val:
                    rb1, rb1_val = j, jc
                if avail[j] in UNAVAILABLE:
                    vt += jt
                    vc += jc
                    continue
                missed = (n - len(inn)) / n
                last_w = max(played)
                if missed == 0 or last_w < week - STALE_WEEKS:
                    continue
                if win[0] not in played and not (st.listed(j) or st.roster.get(j, ("", ""))[1] in ACTIVE_ROSTER):
                    continue      # absent last game with no pregame evidence he is back
                sj_t, sj_c = share_when_playing(team, j)
                rt += sj_t * missed
                rc += sj_c * missed
                if pos_j != "QB":
                    top_ret_val = max(top_ret_val, sj_t)
            rec["tm_tgt_vacated"] = vt / tt if tt else None
            rec["tm_car_vacated"] = vc / tr if tr else None
            rec["tm_tgt_returning"] = rt if tt else None
            rec["tm_car_returning"] = rc if tr else None
            if s_t is not None:
                rec["tm_tgt_gain_pg"] = (expected_share(s_t, rec["tm_tgt_vacated"], rt) - s_t) * tt / n
            if s_c is not None:
                rec["tm_car_gain_pg"] = (expected_share(s_c, rec["tm_car_vacated"], rc) - s_c) * tr / n
            rec["top_tgt_comp_out"] = None if top_present is None else int(avail[top_present] in UNAVAILABLE)
            rec["top_tgt_comp_return"] = int(top_ret_val >= 0 and (top_present is None or top_ret_val * tt >= top_val))
            rec["rb1_out"] = None if rb1 is None else int(avail[rb1] in UNAVAILABLE)
    return out


def _environment(ctx: ContextSources, season: int, week: int) -> dict[str, dict]:
    """Candidate D inputs: team PROE, the pressure a defense generates, positional target share a
    defense allows. This season before W; relative to the league; shrunk."""
    out: dict[str, dict] = defaultdict(dict)
    if ctx.team_env is not None:
        e = ctx.team_env.filter((pl.col("season") == season) & (pl.col("week") < week))
        if e.height:
            for t, s, n in e.group_by("team").agg((pl.col("proe") * pl.col("proe_plays")).sum(),
                                                  pl.col("proe_plays").sum()).iter_rows():
                out[t]["proe"] = shrink(s or 0.0, n or 0.0, 0.0, ENV_SHRINK_PLAYS)
            hits = e.with_columns((pl.col("hit_or_sack_rate") * pl.col("dropbacks")).alias("h")).filter(
                pl.col("h").is_not_null())
            lg = float(hits["h"].sum() / hits["dropbacks"].sum()) if hits.height and hits["dropbacks"].sum() else None
            if lg is not None:
                for d, h, n in hits.group_by("opponent").agg(pl.col("h").sum(), pl.col("dropbacks").sum()).iter_rows():
                    out[d]["def_pressure"] = shrink(h, n, lg, ENV_SHRINK_DROPBACKS) - lg
    if ctx.def_targets is not None:
        d = ctx.def_targets.filter((pl.col("season") == season) & (pl.col("week") < week))
        if d.height and d["targets"].sum():
            for pos in ("rb", "wr", "te"):
                lg = float(d[f"{pos}_targets"].sum() / d["targets"].sum())
                for t, x, n in d.group_by("defteam").agg(pl.col(f"{pos}_targets").sum(),
                                                         pl.col("targets").sum()).iter_rows():
                    out[t][f"def_{pos}_tgt_share"] = shrink(x or 0.0, n or 0.0, lg, ENV_SHRINK_TARGETS) - lg
    return out


__all__ = ["CONTEXT_VERSION", "CONTEXT_DOCS", "ContextSources", "load_context_sources", "context_for_week",
           "expected_starters", "qb_profiles", "redistribute", "expected_share", "shrink"]
