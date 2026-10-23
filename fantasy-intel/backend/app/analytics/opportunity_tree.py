"""Opportunity tree (issue #190, docs/opportunity_tree.md): GET /api/injury-impact/opportunity-tree.

Read-only. For every NFL team with an injured or returning starter this week it shows the position group around him
(a compact depth chart: depth rank, availability, recent snap / route / touch share, recent personnel changes) and one
scenario per starter:

  teammate_unavailable   a QB / RB / WR / TE the Injury Impact page lists (analytics/injury_impact.py, same context,
                         same statuses), except a questionable player who also missed the team's last game: for him the
                         change from the last game is his return, so he is a `teammate_returning` scenario instead.
  teammate_returning     a player with a real role who missed the team's last game while on the team (not released,
                         traded or retired on the newest weekly roster before this week) and is not designated out this
                         week. A return needs pregame evidence: listed on this week's NFL report without an Out /
                         Doubtful designation, a real provider designation (questionable / day to day), or, weaker, an
                         ACT / INA status on the newest weekly roster before this week. A player whose newest roster
                         status is a reserve list needs the NFL report listing. Without evidence the scenario abstains.
                         ESPN's default ACTIVE / NORMAL is no designation and never evidence of a return.

Every scenario carries its sample (team games with / without the starter) and, per teammate, the observed volume per
game in those games (injury_impact._teammates: a game he was available for but did not play counts as zero). A
teammate is named only when the data supports it:

  observed     >= MIN_SUPPORT_GAMES team games on both sides and an observed change of at least MIN_GAIN per game.
  hypothesis   only when there are fewer than MIN_SUPPORT_GAMES games without the starter: the next healthy player on
               the team's depth chart at his position, or a pro-rata share of at least HYPOTHESIS_SHARE and
               HYPOTHESIS_EXTRA per game. Always labelled a hypothesis: a depth chart or a pro-rata rule is not a
               confirmed role change.
  thin_history fewer than MIN_SUPPORT_GAMES games without him and no hypothesis: too few games to observe a change
               (never "the volume spread out", which needs MIN_SUPPORT_GAMES games).

Teammates who are out, doubtful, on IR or suspended themselves, whose availability is unknown, or who have left the
team are never named.

A scenario abstains (names nobody) when its inputs are unknown: the team's usage misses a game the schedule says it
played, the schedule is not synced, the team has no depth chart and the observed history is too thin, or a return has
no pregame evidence. No probabilities, rankings or role certainties are produced.

Ownership is the league's own (fantasy_players in this league's data): a player on another manager's roster is never
claimable, and where the provider does not tell waivers from free agents (Sleeper) a free agent is "available, claim
status unknown", never "no claim needed".
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime

from ..db.connection import fetch_dicts, table_columns, table_exists
from . import injury_impact as ii
from .availability import QUESTIONABLE
from .league_rules import PROVIDERS, provider_of

TREE_VERSION = "opportunity-tree.v1"
MIN_SUPPORT_GAMES = 2
MIN_GAIN = {"touches": 3.0, "targets": 3.0, "pass_attempts": 10.0}
HYPOTHESIS_SHARE = 0.25
HYPOTHESIS_EXTRA = 2.0
RECENT_GAMES = ii.POOL_WEEKS
MAX_PEERS = 6
# Real provider designations that say a player is being considered for the game. ESPN's ACTIVE / NORMAL is its default
# for every healthy player (availability.ESPN_STATUS): no designation, never evidence that an absent player is back.
_PROVIDER_DESIGNATIONS = ("QUESTIONABLE", "DAY_TO_DAY")
_NOT_BACK_REPORT = ("Out", "Doubtful")
_ROSTER_EVIDENCE = ("ACT", "INA")

METHOD = {
    "observed": "Observed: team games this season split by whether the starter played, while each teammate was on the "
                f"team and not on a reserve list. A teammate is named only with at least {MIN_SUPPORT_GAMES} games "
                f"on each side and a change of at least {MIN_GAIN['touches']:g} carries + targets (running backs), "
                f"{MIN_GAIN['targets']:g} targets (receivers) or {MIN_GAIN['pass_attempts']:g} pass attempts "
                "(quarterbacks) per game. Teammates who are out themselves, of unknown availability or no longer on "
                "the team are never named.",
    "hypothesis": f"Hypothesis: with fewer than {MIN_SUPPORT_GAMES} games without the starter there is no observed "
                  "relationship, so the next healthy player on the team's depth chart, or a pro-rata share of his "
                  f"recent volume of at least {HYPOTHESIS_SHARE:.0%} and {HYPOTHESIS_EXTRA:g} per game, can only be a "
                  "hypothesis. A depth chart, a coach's statement or a pro-rata rule is not a confirmed role change. "
                  "With no hypothesis either, the scenario says there are too few games to observe a change.",
    "abstain": "Abstain: nobody is named when the team's usage misses a game it played, the schedule has no past games "
               "for the team, the team has no depth chart and the history is thin, or a return has no pregame "
               "evidence (this week's NFL report, a provider designation, or the newest weekly roster). A provider's "
               "default active status is not evidence.",
    "shares": "Shares are over the games each player played among the team's last "
              f"{RECENT_GAMES}: snap share (snap counts), route share (only where an external route provider "
              "supplied values) and touch share (his carries + targets over the team's skill-player carries + "
              "targets in those games).",
}


def _num(v) -> float | None:
    return ii._num(v)


def _r(v: float | None, nd: int = 3) -> float | None:
    return ii._r(v, nd)


# ----------------------------------------------------------------------------- reads (one query each)


def _past_games(con, season: int, week: int) -> dict[str, set[int]] | None:
    """NFL team -> regular-season weeks it played before `week`; None when the schedule is not synced."""
    if not table_exists(con, "nfl_schedules"):
        return None
    cols = set(table_columns(con, "nfl_schedules"))
    if not {"season", "week", "home_team", "away_team"} <= cols:
        return None
    gt = "AND game_type = 'REG'" if "game_type" in cols else ""
    rows = con.execute(f"SELECT week, home_team, away_team FROM nfl_schedules WHERE season = ? AND week < ? {gt}",
                       [season, week]).fetchall()
    if not rows:
        return None
    out: dict[str, set[int]] = defaultdict(set)
    for w, h, a in rows:
        out[h].add(w)
        out[a].add(w)
    return out


def _routes(con, season: int, week: int) -> tuple[dict[tuple[str, int], float], list[str]]:
    """(gsis, week) -> route participation where an external provider supplied one, and the providers named."""
    if not table_exists(con, "player_week_usage"):
        return {}, []
    cols = set(table_columns(con, "player_week_usage"))
    if "route_participation" not in cols:
        return {}, []
    prov = "route_data_provider" if "route_data_provider" in cols else "NULL"
    rows = fetch_dicts(con, f"""SELECT gsis_id, week, route_participation, {prov} AS provider FROM player_week_usage
                               WHERE season = ? AND week <= ? AND gsis_id IS NOT NULL
                               AND route_participation IS NOT NULL""", [season, week])
    return ({(r["gsis_id"], r["week"]): float(r["route_participation"]) for r in rows},
            sorted({r["provider"] for r in rows if r["provider"]}))


def _provider(league: dict) -> str:
    """espn | sleeper, or unknown when the saved settings are missing, empty or unreadable (then waiver status is never
    assumed). The ESPN sync saves ESPN's own settings object (no provider key); the Sleeper sync marks its own."""
    raw = league.get("settings_raw")
    try:
        settings = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return "unknown"
    return provider_of(settings) if isinstance(settings, dict) and settings else "unknown"


def _newest_rosters(ctx: ii.ImpactContext) -> dict[str, tuple[int, str, str]]:
    """gsis -> (week, team, status) of his newest weekly-roster row before the current week. The week's own row (the
    game-day active / inactive decision) is never read. On a tie (traded that week) the row on a team wins."""
    out: dict[str, tuple[int, str, str]] = {}
    for (g, team), weeks in ctx.rosters.status.items():
        pre = [w for w in weeks if w < ctx.week]
        if not pre:
            continue
        w = max(pre)
        prev = out.get(g)
        if prev is None or w > prev[0] or (w == prev[0] and prev[2] in ii._OFF_TEAM and weeks[w] not in ii._OFF_TEAM):
            out[g] = (w, team, weeks[w])
    return out


def _left_team(newest: dict, g: str, team: str) -> bool:
    """Released, retired or traded on his newest weekly roster, or that roster is another team's."""
    n = newest.get(g)
    return n is not None and (n[2] in ii._OFF_TEAM or n[1] != team)


# ----------------------------------------------------------------------------- ownership


def ownership(roster: dict, waiver_status_known: bool) -> dict:
    """Whether the selected manager could add him, from this league's own roster data. `claim`:
    yours | opponent | rostered (team unknown) | waivers | free_agent | available_claim_unknown | unknown."""
    status = roster["status"]
    if roster["is_mine"]:
        return {"claim": "yours", "claimable": False, "needs_claim": None}
    if status == "ROSTERED":
        return {"claim": "opponent" if roster["fantasy_team"] else "rostered", "claimable": False, "needs_claim": None}
    if status == "WAIVERS":
        return {"claim": "waivers", "claimable": True, "needs_claim": True}
    if status == "FREE_AGENT":
        if waiver_status_known:
            return {"claim": "free_agent", "claimable": True, "needs_claim": False}
        return {"claim": "available_claim_unknown", "claimable": True, "needs_claim": None}
    return {"claim": "unknown", "claimable": None, "needs_claim": None}


# ----------------------------------------------------------------------------- per-team helpers


class _TeamView:
    """One NFL team's coverage, depth chart and recent shares."""

    def __init__(self, ctx: ii.ImpactContext, team: str, past: dict[str, set[int]] | None,
                 routes: dict[tuple[str, int], float], newest: dict[str, tuple[int, str, str]]):
        self.ctx, self.team, self.routes, self.newest = ctx, team, routes, newest
        self.t = ctx.teams.get(team)
        self.depth = {g: d for g, d in ctx.depth.items() if d["team"] == team}
        self.depth_as_of = max((d["as_of"] for d in self.depth.values()), default=None)
        played = set(self.t.weeks) if self.t else set()
        if past is None:
            self.coverage = {"status": "unverified", "missing_weeks": [],
                             "reason": "The NFL schedule is not synced, so whether every team game has usage is "
                                       "unknown."}
        elif team not in past:
            self.coverage = {"status": "unverified", "missing_weeks": [],
                             "reason": f"The synced NFL schedule has no past games for {team}, so whether every team "
                                       "game has usage is unknown."}
        else:
            missing = sorted(past.get(team, set()) - played)
            self.coverage = {"status": "gap" if missing else "complete", "missing_weeks": missing,
                             "reason": None if not missing else
                             f"No usage is synced for {team}'s {'game' if len(missing) == 1 else 'games'} in week"
                             f"{'' if len(missing) == 1 else 's'} {', '.join(str(w) for w in missing)}, so games with "
                             "and without a player cannot be counted."}
        self.recent_weeks = (self.t.weeks if self.t else [])[-RECENT_GAMES:]
        totals: dict[int, float] = defaultdict(float)
        if self.t:
            for rows in self.t.rows.values():
                for w, r in rows.items():
                    if w in self.recent_weeks and ii._played(r):
                        totals[w] += float((r.get("targets") or 0) + (r.get("carries") or 0))
        self.team_touches = totals

    @property
    def depth_known(self) -> bool:
        return bool(self.depth)

    def members(self, positions: tuple[str, ...]) -> list[str]:
        """Players at `positions` who belong to the team now: latest usage team or latest depth chart."""
        ctx = self.ctx
        out = {g for g, d in self.depth.items() if d["position"] in positions}
        if self.t:
            out |= {g for g in self.t.rows if ctx.team_of.get(g) == self.team and ctx.pos_of.get(g) in positions}
        return sorted(g for g in out if not self.left(g))

    def left(self, g: str) -> bool:
        return _left_team(self.newest, g, self.team)

    def shares(self, g: str) -> dict:
        """Snap, route and touch share over the games he played among the team's last games (None = no data)."""
        rows = {w: self.t.rows[g].get(w) for w in self.recent_weeks} if self.t else {}
        played = [w for w, r in rows.items() if ii._played(r)]
        snaps = [x for x in (_num(rows[w].get("offense_pct")) for w in played) if x is not None]
        routes = [self.routes[(g, w)] for w in played if (g, w) in self.routes]
        touches = sum(float((rows[w].get("targets") or 0) + (rows[w].get("carries") or 0)) for w in played)
        team = sum(self.team_touches.get(w, 0.0) for w in played)
        return {"team_games": len(self.recent_weeks), "games_played": len(played),
                "weeks": self.recent_weeks,
                "snap_share": _r(sum(snaps) / len(snaps)) if snaps else None,
                "route_share": _r(sum(routes) / len(routes)) if routes else None,
                "route_games": len(routes),
                "touch_share": _r(touches / team) if played and team > 0 else None,
                "touches_per_game": _r(touches / len(played), 2) if played else None}

    def personnel(self, g: str) -> list[dict]:
        """Recent personnel changes, as facts with their weeks (never a reason or a forecast)."""
        if not self.t or g not in self.t.rows:
            return []
        tenure = self.ctx.rosters.tenure(self.t, g)
        played = [w for w in tenure if ii._played(self.t.rows[g].get(w))]
        out: list[dict] = []
        recent = set(self.recent_weeks)
        if played and played[0] in recent and self.t.weeks and played[0] != self.t.weeks[0]:
            out.append({"kind": "first_game", "week": played[0], "games": None})
        if played:
            missed_tail = [w for w in tenure if w > played[-1]]
            if missed_tail:
                out.append({"kind": "missed", "week": missed_tail[0], "games": len(missed_tail)})
            else:
                before = [w for w in tenure if w < played[-1]]
                gap = 0
                for w in reversed(before):
                    if ii._played(self.t.rows[g].get(w)):
                        break
                    gap += 1
                if gap and played[-1] in recent:
                    out.append({"kind": "returned", "week": played[-1], "games": gap})
        weeks = self.ctx.rosters.status.get((g, self.team), {})
        if weeks:
            last = max(weeks)
            if weeks[last] in ii._RESERVE:
                out.append({"kind": "reserve_list", "week": last, "games": None, "roster_status": weeks[last]})
        return out


def _availability(ctx: ii.ImpactContext, g: str) -> dict:
    st = ctx.status_of.get(g)
    if st is None:
        return {"level": "UNKNOWN", "known": False, "sources": []}
    return {"level": st["status"], "known": True, "sources": st["sources"]}


def _person(ctx: ii.ImpactContext, view: _TeamView, g: str, wsk: bool) -> dict:
    p = ctx.people.get(g, {})
    d = view.depth.get(g)
    roster = ii._roster(p, ctx.teams_by_id)
    return {"gsis_id": g, "espn_player_id": p.get("espn_player_id"),
            "full_name": p.get("full_name") or (d or {}).get("name") or ctx.report_rows.get(g, {}).get("full_name") or g,
            "position": ctx.pos_of.get(g) or (d or {}).get("position"),
            "depth_rank": d["rank"] if d else None,
            "availability": _availability(ctx, g),
            "roster": roster, "ownership": ownership(roster, wsk)}


# ----------------------------------------------------------------------------- scenarios


def _subjects(ctx: ii.ImpactContext, entries: list[dict], newest: dict) -> list[tuple[str, str, str, dict]]:
    """(kind, gsis, position, extra) for every scenario starter. Unavailable from the Injury Impact entries; returning
    from players with a real role who missed the team's last game, still belong to the team on the newest weekly
    roster and are not designated out."""
    out = []
    returning_q = set()
    for e in entries:
        if e["status"]["level"] == QUESTIONABLE and e["absence"]["consecutive_team_games_missed"] > 0:
            returning_q.add(e["gsis_id"])
            continue
        out.append(("teammate_unavailable", e["gsis_id"], e["position"], {"entry": e}))
    listed = {e["gsis_id"] for e in entries} - returning_q
    for team, t in ctx.teams.items():
        for g in t.rows:
            if g in listed or ctx.team_of.get(g) != team or _left_team(newest, g, team):
                continue
            pos = ctx.pos_of.get(g)
            if pos not in ii.POSITIONS:
                continue
            st = ctx.status_of.get(g)
            if st is not None and st["status"] in ii.OUT_LEVELS:
                continue
            w = ii._workload(t, g, pos, ctx.attempts, ctx.trends.get(g), ctx.rosters)
            # A questionable player the Injury Impact page lists (a real role, or on a fantasy roster) stays a subject.
            if w is None or (not ii._meaningful(w, pos) and g not in returning_q):
                continue
            absence = ii._absence(w)
            if absence["consecutive_team_games_missed"] == 0:
                continue
            reserve = g in newest and newest[g][2] in ii._RESERVE
            out.append(("teammate_returning", g, pos, {"workload": w, "absence": absence, "team": team,
                                                       "reserve": reserve}))
    return out


def _return_evidence(ctx: ii.ImpactContext, g: str, provider_label: str, newest: dict,
                     reserve: bool) -> tuple[str, list[dict]]:
    """(certainty, evidence) for a return: if_he_plays (questionable), status_cleared (this week's NFL report or a
    provider designation), roster_only (only an ACT / INA weekly-roster status before this week: weaker, and only once
    this week's NFL report is published, so his absence from it means something) or unconfirmed. Off a reserve list
    only the NFL report listing counts."""
    ev = []
    rep = ctx.reports.get((g, ctx.week))
    if rep is not None and rep["report_status"] not in _NOT_BACK_REPORT:
        ev.append({"source": "nfl_report", "week": ctx.week, "report_status": rep["report_status"],
                   "practice_status": rep["practice_status"]})
    prov = (ctx.people.get(g, {}).get("injury_status") or "").upper()
    if not reserve and prov in _PROVIDER_DESIGNATIONS:
        ev.append({"source": "provider_status", "provider": provider_label, "status": prov})
    if ev:
        st = ctx.status_of.get(g)
        return ("if_he_plays" if st is not None and st["status"] == QUESTIONABLE else "status_cleared"), ev
    n = newest.get(g)
    if not reserve and n is not None and n[2] in _ROSTER_EVIDENCE and _report_published(ctx):
        return "roster_only", [{"source": "weekly_roster", "week": n[0], "status": n[2]}]
    return "unconfirmed", []


def _report_published(ctx: ii.ImpactContext) -> bool:
    """Whether any NFL injury report row for the current week is synced."""
    return any(w == ctx.week for _, w in ctx.reports)


def _usable(mates: list[dict], view: _TeamView, ctx: ii.ImpactContext) -> list[dict]:
    """Teammates who could be named: not out / doubtful / IR / suspended, availability known, still on the team."""
    return [m for m in mates if m["healthy"] and m["gsis_id"] in ctx.status_of and not view.left(m["gsis_id"])]


def _observed_gain(m: dict, unit: str) -> bool:
    w, wo, delta = m["observed"]["with"], m["observed"]["without"], m["observed"]["delta_per_game"]
    return bool(w and wo and delta is not None and w["games"] >= MIN_SUPPORT_GAMES
                and wo["games"] >= MIN_SUPPORT_GAMES and delta >= MIN_GAIN[unit])


def _effects_unavailable(mates: list[dict], n_without: int, unit: str, subject_rank: int | None,
                         subject_pos: str, view: _TeamView, ctx: ii.ImpactContext, wsk: bool) -> tuple[str, list]:
    """(outcome, named teammates) for an unavailable starter."""
    usable = _usable(mates, view, ctx)
    supported = [_effect(m, "observed", ["observed_gain"], view, ctx, wsk) for m in usable if _observed_gain(m, unit)]
    if supported:
        return "supported", supported
    if n_without >= MIN_SUPPORT_GAMES:
        return "no_supported_beneficiary", []
    if not view.depth_known:
        return "abstain", []
    reasons: dict[str, list[str]] = defaultdict(list)
    same = [m for m in usable if m["position"] == subject_pos and m["depth_rank"] is not None
            and (subject_rank is None or m["depth_rank"] > subject_rank)]
    nxt = min(same, key=lambda m: (m["depth_rank"], m["full_name"]), default=None)
    if nxt is not None:
        reasons[nxt["gsis_id"]].append("next_on_depth_chart")
    for m in usable:
        est = m.get("estimate") or {}
        if (est.get("share") or 0) >= HYPOTHESIS_SHARE and (est.get("extra_per_game") or 0) >= HYPOTHESIS_EXTRA:
            reasons[m["gsis_id"]].append("pro_rata_estimate")
    named = [_effect(m, "hypothesis", reasons[m["gsis_id"]], view, ctx, wsk) for m in usable if m["gsis_id"] in reasons]
    return ("hypothesis" if named else "thin_history"), named


def _effects_returning(mates: list[dict], n_without: int, unit: str, view: _TeamView, ctx: ii.ImpactContext,
                       wsk: bool) -> tuple[str, list]:
    named = [_effect(m, "observed", ["gained_while_out"], view, ctx, wsk)
             for m in _usable(mates, view, ctx) if _observed_gain(m, unit)]
    if named:
        return "supported", named
    return ("no_supported_beneficiary" if n_without >= MIN_SUPPORT_GAMES else "thin_history"), []


def _effect(m: dict, basis: str, reasons: list[str], view: _TeamView, ctx: ii.ImpactContext, wsk: bool) -> dict:
    est = m.get("estimate")
    return {**_person(ctx, view, m["gsis_id"], wsk), "full_name": m["full_name"], "position": m["position"],
            "basis": basis, "reasons": reasons, "observed": m["observed"],
            "estimate": None if not est else {"share": est["share"], "extra_per_game": est["extra_per_game"]}}


def _scenario(kind: str, g: str, pos: str, extra: dict, view: _TeamView, ctx: ii.ImpactContext, wsk: bool,
              provider_label: str) -> dict:
    t = view.t
    subject = _person(ctx, view, g, wsk)
    pool_positions, unit = (("QB",), "pass_attempts") if pos == "QB" else ii._POOL[pos]
    with_weeks, without_weeks = ii._split_weeks(t, g, ctx.rosters)
    if kind == "teammate_unavailable":
        e = extra["entry"]
        level = e["status"]["level"]
        certainty = "designated_out" if level in (ii.OUT, ii.IR, ii.SUSPENDED) else "if_he_sits"
        evidence = [{"source": "status", "level": level, "sources": e["status"]["sources"],
                     "nfl_report_status": e["status"]["nfl_report_status"],
                     "practice_status": e["status"]["practice_status"]}]
        absence = e["absence"]
        vacated = e["impact"]["vacated_per_game"] if pos != "QB" else None
    else:
        certainty, evidence = _return_evidence(ctx, g, provider_label, view.newest, extra["reserve"])
        absence = extra["absence"]
        vacated = None
    mates, n_without = ii._teammates(
        t, g, pos, team_of=ctx.team_of, pos_of=ctx.pos_of, depth=ctx.depth, status_of=ctx.status_of,
        people=ctx.people, attempts=ctx.attempts, espn=ctx.espn, teams_by_id=ctx.teams_by_id, vacated=vacated,
        unit=unit, pool_positions=pool_positions, rosters=ctx.rosters, limit=None)

    abstain = None
    if view.coverage["status"] != "complete":
        abstain = {"code": "coverage_" + view.coverage["status"], "text": view.coverage["reason"]}
    elif kind == "teammate_returning" and certainty == "unconfirmed":
        why = ("his newest weekly roster puts him on a reserve list and this week's NFL report does not list him"
               if extra["reserve"] else
               f"this week's NFL report is not synced yet, so a roster status alone is not enough, and there is no "
               f"{provider_label} designation" if not _report_published(ctx) else
               f"no NFL report entry clearing him, no {provider_label} designation and no active or inactive status on "
               "the newest weekly roster")
        abstain = {"code": "no_return_evidence",
                   "text": f"He missed the team's last game and nothing this week says he is back: {why}. A missing "
                           "designation, or a provider's default active status, is not a return."}
    if abstain is not None:
        outcome, effects = "abstain", []
    elif kind == "teammate_unavailable":
        outcome, effects = _effects_unavailable(mates, n_without, unit, subject["depth_rank"], pos, view, ctx, wsk)
        if outcome == "abstain":
            abstain = {"code": "depth_unknown",
                       "text": f"{view.team} has no depth chart synced and there "
                               f"{'is no game' if n_without == 0 else 'is one game'} without him, so there is no "
                               "basis to name a beneficiary."}
    else:
        outcome, effects = _effects_returning(mates, n_without, unit, view, ctx, wsk)
    return {"kind": kind, "subject": {**subject, "absence": absence}, "certainty": certainty, "evidence": evidence,
            "unit": unit, "unit_label": ii._UNIT_LABEL[unit],
            "sample": {"team_games_with": len(with_weeks), "team_games_without": len(without_weeks),
                       "weeks_without": without_weeks},
            "outcome": outcome, "abstain": abstain, "effects": effects}


# ----------------------------------------------------------------------------- payload


def build_opportunity_tree(con, *, team_id: int | None = None, now: datetime | None = None) -> dict:
    """The opportunity-tree payload. InjuryImpactUnavailable when the league is not synced or its week unknown;
    LookupError for a team_id outside the league."""
    ctx = ii.load_context(con, team_id=team_id, now=now)
    entries, _ = ii.impact_entries(ctx)
    provider = _provider(ctx.league)
    provider_label = PROVIDERS.get(provider, "the provider")
    wsk = provider == "espn"          # only a provider known to report waivers vs free agents
    newest = _newest_rosters(ctx)
    past = _past_games(con, ctx.season, ctx.week)
    routes, route_providers = _routes(con, ctx.season, ctx.week)

    by_team: dict[str, list] = defaultdict(list)
    for kind, g, pos, extra in _subjects(ctx, entries, newest):
        team = extra["entry"]["team"] if kind == "teammate_unavailable" else extra["team"]
        if team in ctx.teams:
            by_team[team].append((kind, g, pos, extra))

    teams = []
    for team, subjects in by_team.items():
        view = _TeamView(ctx, team, past, routes, newest)
        scenarios = [_scenario(k, g, pos, x, view, ctx, wsk, provider_label) for k, g, pos, x in subjects]
        scenarios.sort(key=lambda s: (s["kind"] != "teammate_unavailable", s["subject"]["position"],
                                      s["subject"]["depth_rank"] or 99, s["subject"]["full_name"]))
        groups = []
        for pos in sorted({s["subject"]["position"] for s in scenarios}, key=ii.POSITIONS.index):
            starters = {s["subject"]["gsis_id"] for s in scenarios if s["subject"]["position"] == pos}
            peers = []
            for g in view.members((pos,)):
                person = _person(ctx, view, g, wsk)
                sh = view.shares(g)
                if g not in starters and sh["games_played"] == 0 and (person["depth_rank"] or 99) > 3:
                    continue
                peers.append({**person, "starter_in_scenario": g in starters, "shares": sh,
                              "personnel": view.personnel(g)})
            if view.depth_known:
                peers.sort(key=lambda p: (p["depth_rank"] or 99, -(p["shares"]["snap_share"] or 0), p["full_name"]))
            else:
                peers.sort(key=lambda p: (-(p["shares"]["snap_share"] or 0), p["full_name"]))
            groups.append({"position": pos, "ordered_by": "depth_chart" if view.depth_known else "snap_share",
                           "peers": peers[:MAX_PEERS], "more": max(0, len(peers) - MAX_PEERS)})
        tf = ctx.fresh_of.get(team)
        teams.append({
            "team": team, "game": ii.team_game(ctx, team),
            "injury_freshness": None if tf is None else {"status": tf["status"], "reason": tf["reason"]},
            "depth_chart": {"known": view.depth_known, "as_of": view.depth_as_of},
            "coverage": view.coverage, "groups": groups, "scenarios": scenarios,
            "affects_my_roster": any(p["roster"]["is_mine"] for gr in groups for p in gr["peers"])
            or any(e["roster"]["is_mine"] for s in scenarios for e in s["effects"]),
        })
    teams.sort(key=lambda x: (x["game"]["started"] or bool(x["game"]["on_bye"]), not x["affects_my_roster"],
                              x["team"]))

    all_s = [s for x in teams for s in x["scenarios"]]
    counts = {"teams": len(teams),
              "unavailable": sum(1 for s in all_s if s["kind"] == "teammate_unavailable"),
              "returning": sum(1 for s in all_s if s["kind"] == "teammate_returning"),
              "supported": sum(1 for s in all_s if s["outcome"] == "supported"),
              "hypothesis": sum(1 for s in all_s if s["outcome"] == "hypothesis"),
              "no_supported_beneficiary": sum(1 for s in all_s if s["outcome"] == "no_supported_beneficiary"),
              "thin_history": sum(1 for s in all_s if s["outcome"] == "thin_history"),
              "abstained": sum(1 for s in all_s if s["outcome"] == "abstain")}
    notices = []
    if past is None:
        notices.append({"tone": "caution", "code": "no_schedule",
                        "text": "The NFL schedule for past weeks is not synced, so usage coverage cannot be checked and "
                                "every scenario abstains."})
    if not ctx.depth:
        notices.append({"tone": "caution", "code": "no_depth_charts",
                        "text": "No depth charts are synced: groups are ordered by recent snap share, and no "
                                "depth-chart hypothesis is made."})
    if not wsk:
        notices.append({"tone": "info", "code": "waiver_status_unknown",
                        "text": (f"{provider_label} does not report whether an unrostered player is on waivers, so "
                                 "whether adding one needs a claim is unknown.") if provider == "sleeper" else
                        ("The league's provider could not be read from its saved settings, so whether adding an "
                         "unrostered player needs a claim is unknown.")})
    return {
        "version": TREE_VERSION, "season": ctx.season, "week": ctx.week, "generated_at": ctx.now.isoformat() + "Z",
        "league_context": {"available": bool(ctx.teams_rows), "league_name": ctx.league["name"],
                           "my_team_id": ctx.mine, "perspective": "query" if team_id is not None else "owner_default",
                           "provider": provider, "waiver_status_known": wsk},
        "sources": {"usage_through_week": max((r["week"] for r in ctx.usage), default=None),
                    "depth_chart_as_of": max((d["as_of"] for d in ctx.depth.values()), default=None),
                    "schedule_synced": past is not None,
                    "weekly_rosters_synced": bool(ctx.rosters.covered),
                    "route_providers": route_providers},
        "method": {**METHOD, "thresholds": {"min_support_games": MIN_SUPPORT_GAMES, "min_gain": MIN_GAIN,
                                            "hypothesis_share": HYPOTHESIS_SHARE,
                                            "hypothesis_extra": HYPOTHESIS_EXTRA, "recent_games": RECENT_GAMES}},
        "counts": counts, "notices": notices, "teams": teams,
    }
