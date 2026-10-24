"""The stash planner as-of backtest (app/analytics/stash_backtest.py, #218 item 5, research only) on synthetic seasons:
the as-of projection sees only earlier games, byes are 0, unprojected players are excluded, the draft respects caps and
is deterministic, realized gains are the exact lineup evaluator on actual points, and the pre-registered rule decides
as written (docs/research/stash_backtest.md)."""
import pathlib
import subprocess
import sys

import pytest

from app.analytics import stash_backtest as sb
from app.analytics.marginal_utility import scenario
from app.analytics.team_lineups import RosterPlayer, Window


def season(**kw):
    base = dict(season=2024, games={}, prior_ppg={}, prior_total={}, byes={}, position={}, name={})
    base.update(kw)
    return sb.Season(**base)


def test_the_projection_uses_only_earlier_games_and_byes_are_zero():
    s = season(games={1: [(1, "KC", 10.0), (2, "KC", 20.0), (3, "KC", 90.0)]}, prior_ppg={1: 12.0},
               byes={"KC": frozenset({5})}, position={1: "WR"})
    p = sb.asof_projections(s, 3, [3, 4, 5, 6])[1]
    rate = (10.0 + 20.0 + 3 * 12.0) / (2 + 3)                 # week 3's 90 points are not seen at week 3
    assert p == {3: rate, 4: rate, 5: 0.0, 6: rate}


def test_without_a_prior_season_two_games_are_needed_else_unprojected():
    s = season(games={1: [(1, "KC", 8.0)], 2: [(1, "BUF", 6.0), (2, "BUF", 10.0)]}, position={1: "RB", 2: "RB"})
    p = sb.asof_projections(s, 3, [3, 4])
    assert 1 not in p and p[2] == {3: 8.0, 4: 8.0}            # one game is not enough: unknown, never 0


def test_the_team_is_the_one_he_last_played_for():
    s = season(games={1: [(1, "KC", 10.0), (2, "BUF", 10.0)]}, prior_ppg={1: 10.0},
               byes={"KC": frozenset({4}), "BUF": frozenset({5})}, position={1: "WR"})
    p = sb.asof_projections(s, 3, [3, 4, 5])[1]
    assert p[4] == 10.0 and p[5] == 0.0


def test_with_no_game_before_w_the_team_is_last_seasons_never_a_later_one():
    """Review #218: a player who has not played yet (or never plays this season) keeps his previous-season team as of
    w; his first game later this season (for another team) must not decide his bye."""
    s = season(games={1: [(8, "KC", 30.0)]}, prior_ppg={1: 12.0, 2: 9.0}, position={1: "QB", 2: "RB"},
               prior_team={1: "NYJ", 2: "CIN"}, byes={"KC": frozenset({4}), "NYJ": frozenset({5}),
                                                      "CIN": frozenset({6})})
    p = sb.asof_projections(s, 3, [3, 4, 5, 6])
    assert p[1] == {3: 12.0, 4: 12.0, 5: 0.0, 6: 12.0}       # NYJ's bye (week 5), not KC's (week 4)
    assert p[2] == {3: 9.0, 4: 9.0, 5: 9.0, 6: 0.0}           # never plays this season: still projected, as of w
    assert sb.actual_points(s, [2], [3, 4])[2] == {3: 0.0, 4: 0.0}


def big_season():
    pos = ["QB"] * 30 + ["RB"] * 80 + ["WR"] * 90 + ["TE"] * 30
    position = {i + 1: p for i, p in enumerate(pos)}
    prior = {pid: 300.0 - pid for pid in position}
    return season(position=position, prior_total=prior, prior_ppg={p: v / 17 for p, v in prior.items()})


def test_the_draft_is_deterministic_and_respects_the_caps():
    s = big_season()
    a, b = sb.snake_draft(s, 0), sb.snake_draft(s, 0)
    assert a == b and a != sb.snake_draft(s, 1)
    assert len(a) == sb.TEAMS and all(len(r) == sb.ROUNDS for r in a)
    assert len({p for r in a for p in r}) == sb.TEAMS * sb.ROUNDS
    for r in a:
        for pos, cap in sb.CAPS.items():
            assert sum(1 for p in r if s.position[p] == pos) <= cap


def test_the_realized_gain_is_the_exact_lineup_difference_on_actual_points():
    s = season(games={1: [(5, "A", 20.0)], 2: [(5, "B", 10.0), (6, "B", 10.0)], 3: [(5, "C", 30.0), (6, "C", 1.0)]},
               position={1: "QB", 2: "RB", 3: "RB"})
    roster = [RosterPlayer(1, "QB", pro_team="NFL"), RosterPlayer(2, "RB", pro_team="NFL")]
    add = RosterPlayer(3, "RB", pro_team="NFL")
    gain = sb.realized_gain(s, roster, [5, 6], None, add)
    direct = scenario(roster, sb.SLOTS, Window((5, 6), 17), sb.actual_points(s, [1, 2, 3], [5, 6]), add=[add])
    assert gain == direct["_ros_exact"] == 31.0               # RB2 empty: he starts both weeks


def test_the_plan_arm_covers_a_bye_and_the_baselines_are_scored_the_same_way():
    position = {1: "QB", 2: "RB", 3: "RB", 4: "WR", 5: "WR", 6: "TE", 7: "WR", 8: "RB",   # roster
                20: "RB", 21: "WR", 22: "QB"}                                           # free agents
    teams = {1: "A", 2: "B", 3: "C", 4: "A", 5: "A", 6: "A", 7: "A", 8: "A", 20: "D", 21: "A", 22: "A"}
    ppg = {1: 20, 2: 15, 3: 14, 4: 14, 5: 12, 6: 9, 7: 9, 8: 2, 20: 8, 21: 4, 22: 16}
    games = {p: [(w, teams[p], float(ppg[p])) for w in range(1, 10) if not (teams[p] == "B" and w == 6)]
             for p in position}
    s = season(games=games, prior_ppg={p: float(v) for p, v in ppg.items()}, position=position,
               byes={"B": frozenset({6})})
    row = sb.decide(s, [1, 2, 3, 4, 5, 6, 7, 8], {1, 2, 3, 4, 5, 6, 7, 8}, 4)
    # RB 20 (8) starts only in RB 2's week-6 bye (WR 7 holds FLEX at 9), in place of RB 8 (2), the cheapest drop
    assert row["plan"] == 20 and row["drop"] == 8 and row["r_plan"] == 6.0
    # B1 takes the highest projected total, QB 22, who never starts over QB 1: only the drop's week-6 cost remains
    assert row["b1"] == 22 and row["r_b1"] == -2.0


def rows(diffs, plan_gain=5.0):
    return [{"season": 2024, "week": 3 + i % 10, "league": i % 3, "team": i % 12, "plan": 1, "r_plan": plan_gain,
             "r_b1": plan_gain - d, "r_b2": plan_gain, "r_rand": 0.0, "projected_later": 4.0 + i % 7,
             "projected_net": 3.0}
            for i, d in enumerate(diffs)]


@pytest.mark.parametrize("diffs, gain, outcome", [
    ([2.0, 3.0, 1.5, 2.5] * 30, 5.0, "supported"),
    ([-2.0, -3.0, -1.5, -2.5] * 30, 5.0, "baseline better"),
    ([2.0, -2.0, 1.0, -1.0] * 30, 5.0, "no evidence"),
    ([2.0, 3.0, 1.5, 2.5] * 30, -1.0, "no evidence"),           # beats B1 but loses to doing nothing
])
def test_the_pre_registered_rule_decides_as_written(diffs, gain, outcome):
    assert sb.evaluate(rows(diffs, gain))["outcome"] == outcome


def test_the_app_never_imports_the_backtest():
    backend = pathlib.Path(__file__).resolve().parents[1]
    code = "import sys, app.main; assert 'app.analytics.stash_backtest' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], cwd=backend, check=True)
    for path in (backend / "app").rglob("*.py"):
        if path.name != "stash_backtest.py":
            assert "stash_backtest" not in path.read_text(), path


def test_the_draft_does_not_depend_on_who_plays_later_this_season():
    """Codex #347: player ids, and so the draft's seeded noise draws, come from preseason information only. Removing
    one player's week-10 game this season, with last season unchanged, leaves every drafted roster the same."""
    import importlib.util
    scripts = pathlib.Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("stash_backtest_research", scripts / "stash_backtest_research.py")
    research = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(research)
    pos = ["QB"] * 30 + ["RB"] * 80 + ["WR"] * 90 + ["TE"] * 30
    players = [(f"00-{i:04d}", p) for i, p in enumerate(pos)]

    def row(season, week, gsis, p, pts):
        return {"season": season, "season_type": "REG", "week": week, "player_id": gsis, "position": p, "team": "KC",
                "fantasy_points_ppr": pts, "player_display_name": gsis}
    prior = [row(2023, w, g, p, 5.0 + (i * 7) % 13) for i, (g, p) in enumerate(players) for w in (1, 2)]
    late = row(2024, 10, players[150][0], players[150][1], 12.0)
    current = [late] + [row(2024, w, g, p, 8.0) for g, p in players[::3] for w in (1, 2, 3)]
    sched = [{"season": 2024, "game_type": "REG", "week": w, "home_team": "KC", "away_team": "BUF"} for w in range(1, 18)]

    def drafts(stats):
        s = research.build([2024], stats, sched)[0]
        return [[[s.name[p] for p in r] for r in sb.snake_draft(s, lg)] for lg in range(3)]   # names are gsis ids
    assert drafts(current + prior) == drafts([r for r in current if r is not late] + prior)
