"""Breakout Radar (issue #212, research only): deterministic states, point-in-time cuts, labels and the
walk-forward evaluation against naive recent-volume baselines, on synthetic rows."""

import pathlib
import subprocess
import sys

import pytest

from app.analytics import breakout_radar as br
from app.analytics.breakout_radar import Game, label, state_at


def games(values, team="KC", start=1, skip=(), position="WR"):
    out, week = [], start
    for v in values:
        while week in skip:
            week += 1
        out.append(Game(week, team, v, position))
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
    (games([5, 5, 5, 5], team=None) + games([10, 10], start=5), 6, "missing_team"),
    (games([5, 5, 5, 5], position="FB") + games([10, 10], start=5), 6, "position_change"),
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


def test_a_game_at_another_position_is_a_game_played_never_a_gap():
    data = rows("A", [5, 5, 5, 5]) + rows("A", [10], position="FB", start=5) + rows("A", [10], start=6)
    gs = br.player_games(data)[(2024, "A")]
    assert [g.week for g in gs] == [1, 2, 3, 4, 5, 6]
    assert state_at(gs, 6).reason == "position_change"


def test_position_is_taken_as_of_the_week_never_from_a_later_row():
    data = rows("A", [5, 5, 5, 5, 10, 10]) + rows("A", [5], position="QB", start=7)
    assert [r["position"] for r in br.radar(data, 2024, 6)] == ["WR"]
    assert br.radar(data, 2024, 7) == []                      # as of week 7 he is listed QB: not on the radar


def test_a_longer_run_counts_even_when_a_shorter_one_does_not():
    # k=1's baseline holds the raised games (5, 12, 10, 10 -> 9.25, bar 13.9), k=4's is 5, 5 (bar 8).
    st = state_at(games([5, 5, 12, 10, 10, 10]), 6)
    assert (st.state, st.run, st.base) == ("sustained", 4, 8.0)          # B = base(2) = mean(5, 5, 12, 10)


def test_emerging_does_not_need_the_last_game_alone_to_clear_its_own_bar():
    # k=1: base(5, 5, 5, 15) = 7.5, bar 11.25 > 8; k=2: base 5, bar 8: both 15 and 8 clear it.
    assert state_at(games([5, 5, 5, 5, 15, 8]), 6).state == "emerging"


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


def test_the_precision_gap_is_paired_and_clustered_by_week_and_by_player_season():
    s = br.evaluate(_panel(), horizons=(2,))["horizons"][2]["all"]
    for name, base in (("emerging_minus_matched_volume", "baseline_matched_volume"),
                       ("emerging_minus_matched_two_games", "baseline_matched_two_games")):
        gap = s[name]
        assert gap["difference"] == pytest.approx(s["emerging"]["precision"] - s[base]["precision"])
        for scheme in ("by_week", "by_player_season"):
            lo, hi = gap[scheme]["ci95"]
            assert lo <= gap["difference"] <= hi
        assert gap["by_week"]["clusters"] == len({(2024, w) for w in range(4, 9)})
        assert gap["by_player_season"]["clusters"] == 4
    again = br.evaluate(_panel(), horizons=(2,))["horizons"][2]["all"]
    assert again["emerging_minus_matched_volume"] == s["emerging_minus_matched_volume"]      # seeded


def test_the_bootstrap_is_paired_identical_flags_have_a_zero_interval():
    r = [{"season": 2024, "week": w, "gsis_id": p, "label": y}
         for w, p, y in ((4, "A", "elevated"), (4, "B", "neither"), (5, "A", "elevated"), (6, "C", "neither"))]
    flags = [True, True, False, True]
    gap = br._paired_precision_gap(r, flags, flags)
    assert gap["difference"] == 0 and gap["by_week"]["ci95"] == [0, 0] and gap["by_player_season"]["ci95"] == [0, 0]


def test_percentiles_interpolate():
    assert br._percentile([0.0, 1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert br._percentile([0.0, 10.0], 0.25) == 2.5


def test_the_two_game_baseline_ranks_by_the_same_two_games_the_radar_reads():
    r = [{"season": 1, "week": 6, "position": "WR", "state": st, "increase": inc, "increase2": inc2}
         for st, inc, inc2 in (("emerging", 1.0, 5.0), ("steady", 9.0, 4.0), ("steady", 3.0, 6.0))]
    assert br._matched_volume(r, "increase2") == [False, False, True]


def test_matched_volume_is_per_position_cell():
    r = [{"season": 1, "week": 6, "position": p, "state": st, "increase": inc}
         for p, st, inc in (("WR", "emerging", 1.0), ("WR", "steady", 9.0), ("RB", "steady", 8.0), ("RB", "steady", 7.0))]
    assert br._matched_volume(r) == [False, True, False, False]       # RB has no emerging flag, so no RB flag


def test_labels_use_only_weeks_after_w_up_to_w_plus_h_and_the_loop_stays_in_season():
    # 10 weeks; at H=4 the last cut is week 6 (6 + 4 = 10). A huge game in week 11 of another player sets the season's
    # last week to 11 but must not let A's cuts read past his own W + H.
    data = rows("A", [5, 5, 5, 5, 10, 10, 5, 5, 5, 40])
    s = br.evaluate(data, horizons=(4,))["horizons"][4]["all"]
    # cuts 4..6 only: week 6's label reads weeks 7-10 (5, 5, 5, 40: mean 13.75 >= 8, elevated).
    assert s["labelled"] == 3
    em = s["emerging"]
    assert (em["flags"], em["hits"]) == (1, 1)


def test_unlabelled_player_weeks_are_counted_per_state_and_kept_out_of_every_rate():
    data = rows("A", [5, 5, 5, 5, 10, 10]) + rows("A", [10], start=9) + rows("D", [5] * 10)    # A misses weeks 7-8
    s = br.evaluate(data, horizons=(2,))["horizons"][2]["all"]
    assert s["unlabelled_by_state"].get("emerging") == 1             # week 6: no game in weeks 7-8
    assert s["unlabelled"] == sum(s["unlabelled_by_state"].values())
    for k in ("emerging", "baseline_spike", "baseline_matched_volume", "baseline_matched_two_games"):
        assert s[k]["positives"] + s[k]["negatives"] == s["labelled"]
