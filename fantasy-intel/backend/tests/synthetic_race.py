"""Controlled synthetic leagues for Playoff Race Lab tests (pure, in memory). Every number is an
invented test input chosen so standings, all-play and projected margins are known exactly."""

from __future__ import annotations

from app.analytics.lineup import lineup_slots
from app.analytics.playoff_race import RaceData
from app.analytics.projections import last_regular_season_week
from app.analytics.team_lineups import RosterPlayer
from app.analytics.trades import LeagueState

SLOTS = lineup_slots({"QB": 1, "RB": 1, "FLEX": 1})


def round_robin(team_ids: list[int], periods: int) -> dict[int, list[tuple[int, int]]]:
    """Circle method: period -> [(home, away)]."""
    t = list(team_ids)
    out = {}
    for p in range(1, periods + 1):
        out[p] = [(t[i], t[-1 - i]) if (p + i) % 2 else (t[-1 - i], t[i]) for i in range(len(t) // 2)]
        t = [t[0]] + [t[-1]] + t[1:-1]
    return out


def roster(tid: int, strength: float, weeks, extra: dict | None = None):
    """QB projected `strength`, RB 10, WR 5 (FLEX), bench RB 1: weekly optimal total = strength + 15."""
    players = [RosterPlayer(tid * 100 + 1, "QB", "KC", f"QB {tid}"),
               RosterPlayer(tid * 100 + 2, "RB", "BUF", f"RB {tid}"),
               RosterPlayer(tid * 100 + 3, "WR", "DET", f"WR {tid}"),
               RosterPlayer(tid * 100 + 4, "RB", "PHI", f"Bench RB {tid}")]
    proj = {tid * 100 + 1: {w: float(strength) for w in weeks}, tid * 100 + 2: {w: 10.0 for w in weeks},
            tid * 100 + 3: {w: 5.0 for w in weeks}, tid * 100 + 4: {w: 1.0 for w in weeks}}
    for pid, by_week in (extra or {}).items():
        proj.setdefault(pid, {}).update(by_week)
    return players, proj


def race(n_teams: int = 6, reg_periods: int = 8, spots: int | None = 4, start_week: int | None = 5,
         final_week: int = 10, strength: dict[int, float] | None = None, scores: dict | None = None,
         sched: dict | None = None, schedule: dict | None = None, overrides_matchups=None, mine: int = 1,
         seeds: bool = True, divisions: dict[int, int] | None = None, records: dict | None = None,
         rosters_missing: set[int] = frozenset(), projection_patch: dict | None = None,
         current_period: int | None = None, in_progress: set[int] = frozenset()) -> RaceData:
    """Teams 1..n. Periods < start_week are final with `scores[(period, team)]` (default: lower team id
    scores more). Later periods are undecided; periods in `in_progress` (before start_week) are under
    way with live points and no winner. ESPN records default to the final matchups."""
    team_ids = list(range(1, n_teams + 1))
    strength = strength or {t: 20.0 + t for t in team_ids}
    sched = sched if sched is not None else {"matchupPeriodLength": 1,
                                             "matchupPeriods": {str(p): [p] for p in range(1, final_week + 1)}}
    pairs = schedule or round_robin(team_ids, reg_periods)
    weeks = range(start_week or final_week + 1, final_week + 1)
    rosters, proj = {}, {}
    for t in team_ids:
        ps, pj = roster(t, strength[t], weeks)
        rosters[t] = [] if t in rosters_missing else ps
        proj.update(pj)
    for pid, by_week in (projection_patch or {}).items():
        for w, v in by_week.items():
            if v is None:
                proj.get(pid, {}).pop(w, None)
            else:
                proj.setdefault(pid, {})[w] = v
    matchups, mid = [], 0
    scores = scores or {}
    final_periods = [p for p in pairs if (start_week is None or p < start_week) and p not in in_progress]
    for p in sorted(pairs):
        for h, a in pairs[p]:
            mid += 1
            row = {"matchup_id": mid, "matchup_period": p, "home_team_id": h, "away_team_id": a,
                   "home_points": None, "away_points": None, "winner": "UNDECIDED", "playoff_tier": "NONE"}
            if p in final_periods:
                hp = scores.get((p, h), 100.0 + 10 * (n_teams - h))
                ap = scores.get((p, a), 100.0 + 10 * (n_teams - a))
                row.update(home_points=hp, away_points=ap,
                           winner="HOME" if hp > ap else "AWAY" if ap > hp else "TIE")
            if p in in_progress:
                row.update(home_points=40.0 + h, away_points=35.0 + a)
            matchups.append(row)
    for extra in overrides_matchups or []:
        matchups.append({"home_points": None, "away_points": None, "winner": "UNDECIDED", "playoff_tier": "NONE",
                         **extra})
    rec = {t: [0, 0, 0] for t in team_ids}
    for m in matchups:
        if m["winner"] in ("HOME", "AWAY", "TIE") and m["playoff_tier"] == "NONE" and m["home_team_id"] and m["away_team_id"]:
            for tid, side in ((m["home_team_id"], "HOME"), (m["away_team_id"], "AWAY")):
                rec[tid][2 if m["winner"] == "TIE" else 0 if m["winner"] == side else 1] += 1
    rec.update(records or {})
    pf = {t: sum((m["home_points"] if m["home_team_id"] == t else m["away_points"]) or 0 for m in matchups
                 if t in (m["home_team_id"], m["away_team_id"]) and m["winner"] != "UNDECIDED") for t in team_ids}
    order = sorted(team_ids, key=lambda t: (-(rec[t][0] + rec[t][2] / 2) / max(1, sum(rec[t])), t))
    teams = [{"team_id": t, "name": f"Team {t}", "abbrev": f"T{t}", "is_mine": t == mine,
              "division_id": (divisions or {}).get(t, 0), "wins": rec[t][0], "losses": rec[t][1], "ties": rec[t][2],
              "points_for": pf[t], "points_against": None, "playoff_seed": order.index(t) + 1 if seeds else None}
             for t in team_ids]
    last_regular = last_regular_season_week(reg_periods, final_week, sched)
    state = LeagueState(season=2026, slots=SLOTS, final_week=final_week, last_regular=last_regular,
                        start_week=start_week, rosters=rosters, free_agents=[], projections=proj, active_capacity=4,
                        team_names={t: f"Team {t}" for t in team_ids}, league_id=1, first_projection_week=start_week)
    league = {"league_id": 1, "season": 2026, "name": "Synthetic", "current_week": start_week,
              "current_matchup_period": current_period or start_week, "final_scoring_period": final_week,
              "reg_season_matchups": reg_periods, "playoff_teams": spots, "last_regular_season_week": last_regular}
    return RaceData(league=league, schedule_settings=sched, rules_raw={}, teams=teams, matchups=matchups,
                    state=state, window={"reason": "synthetic", "uncertain": False})
