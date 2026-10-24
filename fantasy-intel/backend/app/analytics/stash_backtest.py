"""Stash planner as-of backtest (#218 plan item 5; research only, app.main never imports it).

The pre-registered design is docs/research/stash_backtest.md: synthetic 12-team snake-draft leagues, an as-of shrunk
rate projection with known byes, and four arms per team-week (PLAN = plan_stashes' top stash, B1 = projected-total
greedy, B2 = this-week waiver default, NONE), each scored by the realized change in exact optimal lineups over the
four later weeks with actual points. Every function here is pure: scripts/stash_backtest_research.py loads nflverse
and calls them.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .lineup import lineup_slots
from .marginal_utility import scenario
from .stash_planner import plan_stashes
from .team_lineups import RosterPlayer, Window

SLOTS = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1})
TEAMS, ROUNDS = 12, 15
CAPS = {"QB": 2, "RB": 6, "WR": 6, "TE": 2}
SHRINK_GAMES = 3
MIN_GAMES_NO_PRIOR = 2
DRAFT_SIGMA = 0.25
DECISION_WEEKS = range(3, 14)
HORIZON = 4
LAST_REGULAR = 17
SEED = 218
RESAMPLES = 2000


@dataclass(frozen=True)
class Season:
    """One season's inputs, keyed by integer player ids. `games[pid]` is a list of (week, team, points) for weeks
    played; `prior_ppg` / `prior_total` the previous season's PPR points per game and total, `prior_team` the team of
    his last previous-season game; `byes[team]` the weeks that team has no game; `position[pid]` and `name[pid]`."""
    season: int
    games: dict
    prior_ppg: dict
    prior_total: dict
    byes: dict
    position: dict
    name: dict
    prior_team: dict = field(default_factory=dict)


def asof_projections(s: Season, w: int, weeks) -> dict:
    """pid -> {week: projection} for weeks >= w, from games strictly before `w` (see the pre-registration): 0 on the
    team's bye, otherwise (points before w + 3 * prior ppg) / (games before w + 3); no prior season needs 2 games.
    His team is the one of his last game before `w`; with none this season yet, the one of his last previous-season
    game (as of `w`, never a later game of this season)."""
    out = {}
    for pid in s.position:
        before = [g for g in s.games.get(pid, []) if g[0] < w]
        prior = s.prior_ppg.get(pid)
        if prior is None and len(before) < MIN_GAMES_NO_PRIOR:
            continue                                    # unprojected: unknown, never 0
        if prior is None:
            rate = sum(g[2] for g in before) / len(before)
        else:
            rate = (sum(g[2] for g in before) + SHRINK_GAMES * prior) / (len(before) + SHRINK_GAMES)
        team = max(before)[1] if before else s.prior_team.get(pid)
        if team is None:
            continue                                    # no game before w in either season: no as-of team
        out[pid] = {k: 0.0 if k in s.byes.get(team, ()) else round(rate, 4) for k in weeks}
    return out


def actual_points(s: Season, pids, weeks) -> dict:
    """pid -> {week: actual PPR points}, 0 for a week he did not play."""
    out = {}
    for pid in pids:
        by_week = {g[0]: g[2] for g in s.games.get(pid, [])}
        out[pid] = {k: float(by_week.get(k, 0.0)) for k in weeks}
    return out


def snake_draft(s: Season, league: int) -> list[list[int]]:
    """12 rosters of 15 by snake draft over preseason value (prior-season total x lognormal noise, seeded per season
    and league), within the position caps. Players without a prior season are not drafted."""
    rng = random.Random(f"{SEED}-{s.season}-{league}")
    value = {pid: total * rng.lognormvariate(0.0, DRAFT_SIGMA) for pid, total in sorted(s.prior_total.items())
             if pid in s.position}
    board = sorted(value, key=lambda p: (-value[p], p))
    rosters: list[list[int]] = [[] for _ in range(TEAMS)]
    taken: set[int] = set()
    for rnd in range(ROUNDS):
        order = range(TEAMS) if rnd % 2 == 0 else range(TEAMS - 1, -1, -1)
        for t in order:
            counts = {pos: sum(1 for p in rosters[t] if s.position[p] == pos) for pos in CAPS}
            pick = next((p for p in board if p not in taken and counts[s.position[p]] < CAPS[s.position[p]]), None)
            if pick is not None:
                rosters[t].append(pick)
                taken.add(pick)
    return rosters


def _player(s: Season, pid: int) -> RosterPlayer:
    return RosterPlayer(pid, s.position[pid], pro_team="NFL", name=s.name.get(pid, str(pid)))


def realized_gain(s: Season, roster: list[RosterPlayer], weeks, drop: int | None, add: RosterPlayer) -> float:
    """The exact change in optimal lineups over `weeks` with actual points: roster - drop + add, minus roster."""
    pids = [p.espn_player_id for p in roster] + [add.espn_player_id]
    w = Window(tuple(weeks), LAST_REGULAR)
    res = scenario(roster, SLOTS, w, actual_points(s, pids, weeks), remove=[drop] if drop is not None else (),
                   add=[add])
    return res["_ros_exact"]


def decide(s: Season, roster_ids: list[int], drafted: set[int], w: int) -> dict:
    """One team-week: the four arms' picks and realized later-week gains (None when PLAN finds no stash)."""
    hweeks = list(range(w, w + HORIZON + 1))
    later = hweeks[1:]
    proj = asof_projections(s, w, hweeks)
    roster = [_player(s, p) for p in roster_ids]
    pool = [_player(s, p) for p in sorted(proj) if p not in drafted]
    status = {p.espn_player_id: "FREEAGENT" for p in pool}
    plan = plan_stashes(roster, SLOTS, Window(tuple(hweeks), LAST_REGULAR), proj, free_agents=pool,
                        free_agent_status=status, active_capacity=len(roster), horizon=HORIZON,
                        projection_source="as-of shrunk rate (research)")
    row = {"season": s.season, "week": w, "status": plan["status"], "stashes": len(plan["stashes"])}
    drop = (plan.get("drop") or {}).get("espn_player_id")
    if not pool or (drop is None and plan.get("drop_basis") != "open_spot"):
        return row | {"plan": None}                     # drop_unknown: the planner abstains, nothing is compared
    b1 = max(pool, key=lambda p: (sum(proj[p.espn_player_id][k] for k in later), -p.espn_player_id))
    pick_up_now = {x["espn_player_id"] for x in plan["pick_up_now"]}
    if not plan["stashes"]:                             # pre-registered: R_B1 where PLAN found no stash
        return row | {"plan": None, "b1": b1.espn_player_id, "drop": drop,
                      "r_b1": realized_gain(s, roster, later, drop, b1), "b1_pick_up_now": b1.espn_player_id in
                      pick_up_now}
    top = plan["stashes"][0]
    by_id = {p.espn_player_id: p for p in pool}
    b2 = max(pool, key=lambda p: (proj[p.espn_player_id][w], -p.espn_player_id))
    # post-hoc, not deciding (review): a random projected free agent with the same drop
    rnd = random.Random(f"{SEED}-{s.season}-{w}-{','.join(map(str, roster_ids))}").choice(pool)
    picks = {"plan": by_id[top["espn_player_id"]], "b1": b1, "b2": b2, "rand": rnd}
    gains = {arm: realized_gain(s, roster, later, drop, f) for arm, f in picks.items()}
    return row | {"plan": top["espn_player_id"], "b1": b1.espn_player_id, "b2": b2.espn_player_id, "drop": drop,
                  "r_plan": gains["plan"], "r_b1": gains["b1"], "r_b2": gains["b2"], "r_rand": gains["rand"],
                  "b1_pick_up_now": b1.espn_player_id in pick_up_now,
                  "projected_later": top["later_delta"], "projected_net": top["horizon_delta"]}


def run_season(s: Season, leagues: int = 10) -> list[dict]:
    rows = []
    for lg in range(leagues):
        rosters = snake_draft(s, lg)
        drafted = {p for r in rosters for p in r}
        for team, ids in enumerate(rosters):
            for w in DECISION_WEEKS:
                rows.append(decide(s, ids, drafted, w) | {"league": lg, "team": team})
    return rows


# ---- the pre-registered evaluation ---------------------------------------------------------------------------------

def _cluster_ci(values: list[tuple], seed: int = SEED, resamples: int = RESAMPLES) -> tuple[float, float, float]:
    """(mean, 2.5%, 97.5%) of the mean of `value` over rows, resampling clusters with replacement. `values` is a list
    of (cluster key, value)."""
    clusters: dict = {}
    for key, v in values:
        clusters.setdefault(key, []).append(v)
    keys = sorted(clusters)
    total = [v for k in keys for v in clusters[k]]
    mean = sum(total) / len(total)
    rng = random.Random(seed)
    stats = []
    for _ in range(resamples):
        pick = [clusters[keys[rng.randrange(len(keys))]] for _ in keys]
        flat = [v for c in pick for v in c]
        stats.append(sum(flat) / len(flat))
    stats.sort()
    lo, hi = int(0.025 * resamples), resamples - 1 - int(0.025 * resamples)
    return mean, stats[lo], stats[hi]


def evaluate(rows: list[dict]) -> dict:
    """The pre-registered decision (docs/research/stash_backtest.md) and the reported, non-deciding statistics."""
    comp = [r for r in rows if r.get("plan") is not None]
    out = {"decisions": len(rows), "with_stash": len(comp), "seasons": sorted({r["season"] for r in rows})}
    if not comp:
        return out | {"outcome": "no evidence", "reason": "PLAN found no stash"}
    clusterings = {"week": lambda r: (r["season"], r["week"]), "team": lambda r: (r["season"], r["league"], r["team"])}
    stats = {}
    for name, fn in (("r_plan", lambda r: r["r_plan"]), ("d1", lambda r: r["r_plan"] - r["r_b1"]),
                     ("d2", lambda r: r["r_plan"] - r["r_b2"])):
        stats[name] = {c: _cluster_ci([(key(r), fn(r)) for r in comp]) for c, key in clusterings.items()}
    above = lambda name: all(lo > 0 for _, lo, _ in stats[name].values())          # noqa: E731
    below = lambda name: all(hi < 0 for _, _, hi in stats[name].values())          # noqa: E731
    if above("r_plan") and above("d1"):
        outcome = "supported"
    elif below("d1"):
        outcome = "baseline better"
    else:
        outcome = "no evidence"
    none_rows = [r for r in rows if r.get("plan") is None and "r_b1" in r]
    xs, ys = [r["projected_later"] for r in comp], [r["r_plan"] for r in comp]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    ranked = sorted(comp, key=lambda r: r["projected_later"])
    q = len(ranked) // 5
    bins = [ranked[i * q:(i + 1) * q] if i < 4 else ranked[4 * q:] for i in range(5)]
    out |= {"abstained_drop_unknown": sum(r.get("status") == "drop_unknown" for r in rows),
            "calibration": {"slope": sxy / sxx if sxx else None,
                            "correlation": sxy / (sxx * syy) ** 0.5 if sxx and syy else None,
                            "quintiles": [{"projected": sum(r["projected_later"] for r in b) / len(b),
                                           "realized": sum(r["r_plan"] for r in b) / len(b), "n": len(b)}
                                          for b in bins if b]},
            "no_stash_r_b1": {"n": len(none_rows), "mean": (sum(r["r_b1"] for r in none_rows) / len(none_rows))
                              if none_rows else None},
            "post_hoc": {"b1_is_pick_up_now": sum(bool(r.get("b1_pick_up_now")) for r in comp) / len(comp),
                         "r_rand": {c: _cluster_ci([(key(r), r["r_rand"]) for r in comp])
                                    for c, key in clusterings.items()},
                         "plan_minus_rand": {c: _cluster_ci([(key(r), r["r_plan"] - r["r_rand"]) for r in comp])
                                             for c, key in clusterings.items()},
                         "clusters": {c: len({key(r) for r in comp}) for c, key in clusterings.items()}},
            "outcome": outcome, "stats": stats,
            "precision": sum(r["r_plan"] > 0 for r in comp) / len(comp),
            "mean_projected_later": sum(r["projected_later"] for r in comp) / len(comp),
            "mean_projected_net": sum(r["projected_net"] for r in comp) / len(comp),
            "by_season": {s: {"n": len(rs := [r for r in comp if r["season"] == s]),
                              "r_plan": sum(r["r_plan"] for r in rs) / len(rs),
                              "d1": sum(r["r_plan"] - r["r_b1"] for r in rs) / len(rs),
                              "d2": sum(r["r_plan"] - r["r_b2"] for r in rs) / len(rs)}
                          for s in out["seasons"] if any(r["season"] == s for r in comp)},
            "no_stash_team_weeks": len(none_rows)}
    return out
