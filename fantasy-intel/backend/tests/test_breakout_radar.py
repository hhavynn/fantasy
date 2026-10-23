"""Breakout Radar (issue #212, research only): deterministic states, point-in-time cuts, labels and the
walk-forward evaluation against naive recent-volume baselines, on synthetic rows."""

import pathlib
import subprocess
import sys

import pytest

from app.analytics import breakout_radar as br
from app.analytics.breakout_radar import Game, label, state_at


def games(values, team="KC", start=1, skip=()):
    out, week = [], start
    for v in values:
        while week in skip:
            week += 1
        out.append(Game(week, team, v))
        week += 1
    return out


def rows(pid, values, position="WR", season=2024, team="KC", start=1, skip=()):
    return [{"season": season, "week": g.week, "gsis_id": pid, "position": position, "team": g.team, "xfp": g.value}
            for g in games(values, team, start, skip)]


# ---------------------------------------------------------------------------------------------------- states

def test_two_straight_games_well_above_his_level_are_emerging():
    st = state_at(games([5, 5, 5, 5, 10, 10]), 6)
    assert (st.state, st.run, st.base) == ("emerging", 2, 5.0)


def test_three_or_more_are_sustained():
    assert state_at(games([5, 5, 5, 5, 10, 10, 10]), 7).state == "sustained"


def test_a_single_game_spike_is_not_a_role_change():
    st = state_at(games([5, 5, 5, 5, 5, 10]), 6)
    assert (st.state, st.run) == ("steady", 1)


def test_the_bar_is_relative_for_big_roles_and_absolute_for_small_ones():
    # 20 xfp: the bar is +50% (30); 1 xfp: the bar is +3 (4), never "doubled from 1 to 2".
    assert state_at(games([20, 20, 20, 20, 29, 29]), 6).state == "steady"
    assert state_at(games([20, 20, 20, 20, 30, 30]), 6).state == "emerging"
    assert state_at(games([1, 1, 1, 1, 2.5, 2.5]), 6).state == "steady"
    assert state_at(games([1, 1, 1, 1, 4, 4]), 6).state == "emerging"


def test_two_straight_games_well_below_are_fading():
    assert state_at(games([12, 12, 12, 12, 6, 6]), 6).state == "fading"
    assert state_at(games([12, 12, 12, 12, 6, 12]), 6).state == "steady"


@pytest.mark.parametrize("gs,week,reason", [
    (games([5, 5, 10]), 3, "too_few_games"),                                  # a rookie's first games
    (games([5, 5, 5, 5, 10, 10]), 8, "no_recent_game"),                       # out for two weeks (injury, IR)
    (games([5, 5, None, 5, 10, 10]), 6, "missing_metric"),                     # unknown xfp is never a zero
    (games([5, 5, 5, 5], team="NYJ") + games([10, 10], team="KC", start=5), 6, "team_change"),   # traded
])
def test_insufficient_says_why(gs, week, reason):
    st = state_at(gs, week)
    assert (st.state, st.reason) == ("insufficient", reason)


def test_windows_are_games_played_so_a_bye_is_not_a_zero():
    gs = games([5, 5, 5, 5, 10, 10], skip=(5,))           # bye in week 5: games in weeks 1-4, 6, 7
    assert [g.week for g in gs] == [1, 2, 3, 4, 6, 7]
    assert state_at(gs, 7).state == "emerging"
    assert state_at(gs, 5).state == "steady"              # as of the bye week, his last game was week 4


def test_no_look_ahead_later_games_never_change_a_state():
    past = games([5, 5, 5, 5, 10, 10])
    future_a = past + [Game(7, "KC", 0.0), Game(8, "KC", 40.0)]
    future_b = past + [Game(7, "NYJ", None)]
    for gs in (future_a, future_b):
        assert state_at(gs, 6) == state_at(past, 6)


# ---------------------------------------------------------------------------------------------------- labels

def test_labels_need_half_the_horizon_played_and_never_count_a_missing_game():
    assert label([Game(7, "KC", 10)], 5.0, 2) == "elevated"
    assert label([], 5.0, 2) is None                                     # did not play: unlabelled, not "fell"
    assert label([Game(7, "KC", 10)], 5.0, 4) is None                    # 1 of 4 weeks
    assert label([Game(7, "KC", 10), Game(8, "KC", None)], 5.0, 2) is None
    assert label([Game(7, "KC", 6), Game(8, "KC", 6)], 5.0, 2) == "neither"
    assert label([Game(7, "KC", 6), Game(8, "KC", 7)], 12.0, 2) == "fell"


# ---------------------------------------------------------------------------------------------------- radar

def test_radar_lists_every_player_with_his_state_as_of_the_week():
    data = rows("A", [5, 5, 5, 5, 10, 10, 12]) + rows("B", [8, 8, 8], position="RB") + rows("Q", [20] * 6, position="QB")
    out = br.radar(data, 2024, 6)
    assert [(r["gsis_id"], r["state"], r["reason"]) for r in out] == [("B", "insufficient", "too_few_games"),
                                                                      ("A", "emerging", None)]


def test_postseason_rows_never_enter_a_window():
    data = rows("A", [5] * 18) + [{"season": 2024, "week": 19, "gsis_id": "A", "position": "WR", "team": "KC", "xfp": 30}]
    assert br.player_games(data)[(2024, "A")][-1].week == 18


# ---------------------------------------------------------------------------------------------------- evaluation

def _panel():
    # A: rises at weeks 5-6 and stays up (a real role change). B: rises at weeks 5-6, then back to normal (noise).
    # C: a one-game spike at week 6, then normal. D: flat.
    return (rows("A", [5, 5, 5, 5, 10, 10, 10, 10, 10, 10]) + rows("B", [5, 5, 5, 5, 10, 10, 5, 5, 5, 5])
            + rows("C", [5, 5, 5, 5, 5, 12, 5, 5, 5, 5]) + rows("D", [5] * 10))


def test_evaluation_counts_hits_and_false_alarms_with_their_denominators():
    s = br.evaluate(_panel(), horizons=(2,))["horizons"][2]["all"]
    assert "rows" not in s                              # aggregates only
    assert s["labelled"] == s["eligible"] - s["unlabelled"]
    em = s["emerging"]
    # Week 6: A and B are emerging; A stays up (hit), B falls back (false alarm). Week 7: nobody is emerging
    # (A is sustained, B has dropped).
    assert (em["flags"], em["hits"]) == (2, 1)
    assert em["precision"] == 0.5
    assert em["positives"] + em["negatives"] == s["labelled"]
    assert em["false_positive_rate"] == pytest.approx(1 / em["negatives"])


def test_the_naive_baselines_flag_on_the_same_player_weeks():
    s = br.evaluate(_panel(), horizons=(2,))["horizons"][2]["all"]
    # spike: every last game >= the bar (A and B at weeks 5, 6; C at week 6; A while sustained)
    assert s["baseline_spike"]["flags"] >= s["emerging"]["flags"]
    # matched volume: exactly as many flags as the radar, per (season, week, position)
    assert s["baseline_matched_volume"]["flags"] == s["emerging"]["flags"]


def test_matched_volume_takes_the_largest_last_game_increases():
    r = [{"season": 1, "week": 6, "position": "WR", "state": st, "increase": inc}
         for st, inc in (("emerging", 1.0), ("steady", 9.0), ("steady", 3.0))]
    assert br._matched_volume(r) == [False, True, False]


def test_evaluation_reports_coverage_and_insufficient_reasons():
    data = _panel() + rows("R", [5, 12], start=6)                    # a late rookie: too few games all along
    s = br.evaluate(data, horizons=(2,))["horizons"][2]["all"]
    assert s["insufficient"]["too_few_games"] >= 1
    assert 0 < s["coverage"] < 1
    assert s["player_weeks"] == s["eligible"] + sum(s["insufficient"].values())


def test_positions_are_reported_apart_and_together():
    data = _panel() + rows("T", [5, 5, 5, 5, 10, 10, 10, 10, 10, 10], position="TE")
    h = br.evaluate(data, horizons=(2,))["horizons"][2]
    assert set(h) == {"WR", "TE", "all"}
    assert h["all"]["emerging"]["flags"] == h["WR"]["emerging"]["flags"] + h["TE"]["emerging"]["flags"]


def test_thresholds_are_reported_with_the_result():
    ev = br.evaluate(_panel(), horizons=(2,))
    assert ev["status"] == "research_only" and ev["metric"] == "xfp"
    assert ev["thresholds"] == {"abs_up": 3.0, "rel_up": 0.5, "abs_down": 3.0, "rel_down": 0.33,
                                "recent": 2, "base_min": 2, "base_max": 4}


# ---------------------------------------------------------------------------------------------------- isolation

def test_research_module_is_never_imported_by_the_app():
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    for path in root.rglob("*.py"):
        if path.name == "breakout_radar.py":
            continue
        assert "breakout_radar" not in path.read_text(), path
    code = "import sys, app.main; assert 'app.analytics.breakout_radar' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], cwd=root.parent, check=True)


def test_the_module_reads_rows_it_is_given_and_opens_no_database():
    text = (pathlib.Path(br.__file__)).read_text()
    for word in ("duckdb", "connect(", "fetch_dicts", "execute(", "open("):
        assert word not in text, word


def test_the_precision_gap_is_paired_and_resamples_whole_weeks():
    s = br.evaluate(_panel(), horizons=(2,))["horizons"][2]["all"]
    gap = s["emerging_minus_matched_volume"]
    assert gap["difference"] == pytest.approx(s["emerging"]["precision"] - s["baseline_matched_volume"]["precision"])
    assert gap["ci95"][0] <= gap["difference"] <= gap["ci95"][1]
    assert gap["cuts"] == len({(2024, w) for w in range(4, 9)})
    again = br.evaluate(_panel(), horizons=(2,))["horizons"][2]["all"]["emerging_minus_matched_volume"]
    assert again == gap                                              # seeded: the same interval every run
