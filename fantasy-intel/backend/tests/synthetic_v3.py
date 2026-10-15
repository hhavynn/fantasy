"""Hand-built synthetic leagues for the V3 trade tests (pure, in memory; invented players only)."""

from __future__ import annotations

from app.analytics.lineup import lineup_slots
from app.analytics.team_lineups import RosterPlayer
from app.analytics.trades import LeagueState

SLOTS = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "D/ST": 1})
WEEKS = (10, 11, 12, 13)        # 10-12 regular season, 13 playoffs


def P(pid: int, pos: str, name: str | None = None, team: str = "KC", on_ir: bool = False, status: str | None = None):
    return RosterPlayer(pid, pos, team, name or f"{pos} {pid}", on_ir=on_ir, injury_status=status)


def league(teams: dict[int, list[tuple]], fas: list[tuple] = (), capacity: int | None = None, limits=None,
           weeks=WEEKS, last_regular: int = 12, slots=SLOTS) -> LeagueState:
    """teams: {team_id: [(pid, pos, points | {week: points} | None, {extra RosterPlayer kwargs})]}; a flat
    number is the same projection every week; None = no projection."""
    rosters, proj = {}, {}

    def add(row):
        pid, pos, pts, *rest = row
        kw = rest[0] if rest else {}
        if pts is not None:
            proj[pid] = dict(pts) if isinstance(pts, dict) else {w: float(pts) for w in weeks}
        return P(pid, pos, **kw)
    for t, rows in teams.items():
        rosters[t] = [add(r) for r in rows]
    free = [add(r) for r in fas]
    cap = capacity or max(sum(1 for p in ps if not p.on_ir) for ps in rosters.values())
    return LeagueState(season=2026, slots=slots, final_week=weeks[-1], last_regular=last_regular, start_week=weeks[0],
                       rosters=rosters, free_agents=free, projections=proj, active_capacity=cap,
                       position_limits=limits or {}, team_names={t: f"Team {t}" for t in rosters},
                       first_projection_week=weeks[0])


def filler(team: int, base: int, qb=15, rb=(9, 8, 4), wr=(9, 8, 4), te=(6,), k=7, dst=7) -> list[tuple]:
    """An ordinary roster (ids base+1...)."""
    rows, i = [], base
    for pos, vals in (("QB", (qb,)), ("RB", rb), ("WR", wr), ("TE", te), ("K", (k,)), ("D/ST", (dst,))):
        for v in vals:
            i += 1
            rows.append((i, pos, v))
    return rows


def market_league(dst_swing: bool = True) -> LeagueState:
    """Four teams built for recommendation-quality tests.

      Team 1 (you): weak RBs, deep WRs (WR surplus), elite WR "Diggs" 18, weak D/ST.
      Team 2: deep RB and TE benches; "Jeanty" RB 16 and "Bowers" TE 13 barely start for them.
      Team 3: deep RBs (RB surplus), weak WRs: a natural WR-for-RB partner.
      Team 4: two strong team defenses (one would be a big projected D/ST upgrade for you).
    Free agents give every position a real replacement level."""
    t1 = [(101, "QB", 20), (102, "RB", 7), (103, "RB", 6), (104, "RB", 2),
          (105, "WR", 18, {"name": "Diggs"}), (106, "WR", 15), (107, "WR", 14), (108, "WR", 13), (109, "WR", 12),
          (110, "TE", 6), (111, "K", 8), (112, "D/ST", 2)]
    t2 = [(201, "QB", 19), (202, "RB", 18), (203, "RB", 17), (204, "RB", 16, {"name": "Jeanty"}),
          (205, "WR", 10), (206, "WR", 9), (207, "WR", 4), (208, "TE", 14), (209, "TE", 13, {"name": "Bowers"}),
          (210, "K", 8), (211, "D/ST", 7)]
    t3 = [(301, "QB", 18), (302, "RB", 15), (303, "RB", 14), (304, "RB", 13), (305, "RB", 12),
          (306, "WR", 6), (307, "WR", 5), (308, "WR", 3), (309, "TE", 7), (310, "K", 7), (311, "D/ST", 6)]
    swing = {10: 14.0, 11: 4.0, 12: 14.0, 13: 4.0} if dst_swing else 9.0
    swing2 = {10: 4.0, 11: 14.0, 12: 4.0, 13: 14.0} if dst_swing else 9.0
    t4 = filler(4, 400) + [(450, "D/ST", swing), (451, "D/ST", swing2)]
    fas = [(901, "QB", 12), (902, "RB", 5), (903, "WR", 6), (904, "TE", 4), (905, "K", 7), (906, "D/ST", 6)]
    return league({1: t1, 2: t2, 3: t3, 4: t4}, fas, capacity=13)
