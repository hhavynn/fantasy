"""Synthetic leagues for My Team Value tests and timing (pure, in memory). Invented inputs only."""

from __future__ import annotations

import random

from app.analytics.playoff_race import RaceData
from app.analytics.team_lineups import RosterPlayer

import synthetic_race as sr
from synthetic_league import MEAN, NFL, synthetic_league


def free_agents(state, n: int, weeks, seed: int = 11, start_id: int = 50000) -> list[RosterPlayer]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        pos = rng.choice(["QB", "RB", "RB", "WR", "WR", "TE", "K", "D/ST"])
        pid = start_id + i
        lo, hi = MEAN[pos]
        base = rng.uniform(lo, hi) * 0.6
        state.projections[pid] = {w: round(max(0.0, base + rng.uniform(-2, 2)), 2) for w in weeks}
        out.append(RosterPlayer(pid, pos, rng.choice(NFL), f"FA {pos} {pid}"))
    return out


def value_race(n_teams: int = 12, start: int = 6, final: int = 17, last_regular: int = 14, seed: int = 7,
               fa_count: int = 60, spots: int = 6, mine: int = 1, **league_kw) -> RaceData:
    """synthetic_league rosters (16 players + 1 IR RB each), `fa_count` free agents, a round-robin
    regular season (periods before `start` final), and ESPN-like team rows."""
    weeks = range(start, final + 1)
    state = synthetic_league(n_teams, weeks=weeks, last_regular=last_regular, seed=seed, **league_kw)
    state.league_id = 1
    state.free_agents = free_agents(state, fa_count, weeks)
    team_ids = sorted(state.rosters)
    pairs = sr.round_robin(team_ids, last_regular)
    matchups, mid, rec = [], 0, {t: [0, 0, 0] for t in team_ids}
    rng = random.Random(seed + 2)
    for p in range(1, last_regular + 1):
        for h, a in pairs[p]:
            mid += 1
            row = {"matchup_id": mid, "matchup_period": p, "home_team_id": h, "away_team_id": a,
                   "home_points": None, "away_points": None, "winner": "UNDECIDED", "playoff_tier": "NONE"}
            if p < start:
                hp, ap = round(rng.uniform(80, 140), 1), round(rng.uniform(80, 140), 1)
                row.update(home_points=hp, away_points=ap, winner="HOME" if hp > ap else "AWAY")
                rec[h][0 if hp > ap else 1] += 1
                rec[a][0 if ap > hp else 1] += 1
            matchups.append(row)
    order = sorted(team_ids, key=lambda t: (-rec[t][0], t))
    teams = [{"team_id": t, "name": f"Team {t}", "abbrev": f"T{t}", "is_mine": t == mine, "division_id": 0,
              "wins": rec[t][0], "losses": rec[t][1], "ties": 0, "points_for": None, "points_against": None,
              "playoff_seed": order.index(t) + 1} for t in team_ids]
    league = {"league_id": 1, "season": 2026, "name": "Synthetic", "current_week": start, "current_matchup_period": start,
              "final_scoring_period": final, "reg_season_matchups": last_regular, "playoff_teams": spots,
              "last_regular_season_week": last_regular}
    sched = {"matchupPeriodLength": 1, "matchupPeriods": {str(p): [p] for p in range(1, final + 1)}}
    return RaceData(league=league, schedule_settings=sched, rules_raw={}, teams=teams, matchups=matchups,
                    state=state, window={"reason": "synthetic", "uncertain": False})
