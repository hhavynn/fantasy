"""Deterministic synthetic ESPN-style leagues for trade-discovery tests and benchmarks (pure,
in memory: never written to any database)."""

from __future__ import annotations

import random

from app.analytics.lineup import lineup_slots
from app.analytics.team_lineups import RosterPlayer
from app.analytics.trades import LeagueState

SLOTS = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "D/ST": 1})
NFL = ["ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
       "LV", "LAC", "LAR", "MIA", "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SF", "SEA", "TB", "TEN", "WSH"]
ROSTER = ["QB", "QB", "RB", "RB", "RB", "RB", "RB", "WR", "WR", "WR", "WR", "WR", "TE", "TE", "K", "D/ST"]
MEAN = {"QB": (14, 23), "RB": (4, 18), "WR": (4, 18), "TE": (3, 12), "K": (6, 9), "D/ST": (4, 9)}


SMALL_ROSTER = ["QB", "RB", "RB", "RB", "WR", "WR", "WR", "TE", "K", "D/ST"]


def synthetic_league(n_teams: int = 12, weeks: range = range(6, 18), last_regular: int = 14, seed: int = 7,
                     ir_per_team: int = 1, capacity: int | None = None, roster: list[str] | None = None,
                     slots: list[str] | None = None) -> LeagueState:
    """`n_teams` rosters of `roster` (default 16 players) plus `ir_per_team` RBs in IR slots, ESPN-like
    weekly projections with a real bye (0) per NFL team, and IR players projected 0 for three weeks."""
    roster = roster or ROSTER
    capacity = capacity or len(roster)
    rng = random.Random(seed)
    bye = {t: rng.choice(range(5, 15)) for t in NFL}
    rosters, proj, pid = {}, {}, 1000
    for team in range(1, n_teams + 1):
        players = []
        for i, pos in enumerate(roster + ["RB"] * ir_per_team):
            pid += 1
            nfl = rng.choice(NFL)
            on_ir = i >= len(roster)
            lo, hi = MEAN[pos]
            base = rng.uniform(lo, hi)
            proj[pid] = {w: (0.0 if bye[nfl] == w else round(max(0.0, base + rng.uniform(-3, 3)), 2)) for w in weeks}
            if on_ir:
                proj[pid] = {w: (0.0 if w < weeks.start + 3 else v) for w, v in proj[pid].items()}
            players.append(RosterPlayer(pid, pos, nfl, f"{pos} {pid}", on_ir=on_ir,
                                        injury_status="INJURY_RESERVE" if on_ir else "ACTIVE"))
        rosters[team] = players
    return LeagueState(season=2026, slots=slots or SLOTS, final_week=weeks[-1], last_regular=last_regular,
                       start_week=weeks[0], rosters=rosters, free_agents=[], projections=proj,
                       active_capacity=capacity, position_limits={},
                       team_names={t: f"Team {t}" for t in rosters}, first_projection_week=weeks[0])


def add_free_agents(state: LeagueState, n: int = 60, seed: int = 11, start_id: int = 50000,
                    scale: float = 0.6) -> LeagueState:
    """`n` free agents with ESPN-like weekly projections at `scale` of the rostered means (in place)."""
    rng = random.Random(seed)
    weeks = range(state.window().start or state.final_week, state.final_week + 1)
    out = []
    for i in range(n):
        pos = rng.choice(["QB", "RB", "RB", "WR", "WR", "TE", "K", "D/ST"])
        pid = start_id + i
        lo, hi = MEAN[pos]
        base = rng.uniform(lo, hi) * scale
        state.projections[pid] = {w: round(max(0.0, base + rng.uniform(-2, 2)), 2) for w in weeks}
        out.append(RosterPlayer(pid, pos, rng.choice(NFL), f"FA {pos} {pid}"))
    state.free_agents = [*state.free_agents, *out]
    return state
