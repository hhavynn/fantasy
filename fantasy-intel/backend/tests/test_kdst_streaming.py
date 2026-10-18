"""K / D/ST streaming baseline (team_lineups.StreamingProjections, trades.with_streaming): kickers and
team defenses are valued against weekly free-agent streaming, opt-in, consistently across the lineup
engine, the trade evaluator, discovery's fast solver and the waiver forward comparisons. Synthetic
inputs only."""

from dataclasses import replace

import pytest

from app.analytics.team_lineups import (STREAM_NOTE, STREAMER_NAMES, StreamingProjections, project_team,
                                        project_week, starters, stream_summary, streaming_baseline)
from app.analytics.trade_assets import AssetBoard, team_needs
from app.analytics.trade_discovery import SHAPES, TradeSearch, detail
from app.analytics.trades import evaluate_trade, streaming_enabled, with_streaming
from app.analytics.waiver_data import streaming_floor
from app.analytics.waiver_roster import forward_effect

from synthetic_v3 import SLOTS, WEEKS, filler, league

# filler(team, base): base+1 QB 15 | +2..+4 RB 9, 8, 4 | +5..+7 WR 9, 8, 4 | +8 TE 6 | +9 K | +10 D/ST.
# Free agents: D/ST 9, 6, 5, 2 (the #3 baseline is 5); K 8, 7, 6.5 (baseline 6.5).
FAS = [(901, "D/ST", 9), (902, "D/ST", 6), (903, "D/ST", 5), (904, "D/ST", 2),
       (911, "K", 8), (912, "K", 7), (913, "K", 6.5)]


def kdst_league():
    """Team 1: weak D/ST 3, K 7. Team 2: elite D/ST 10, K 9. Team 3: ordinary."""
    return league({1: filler(1, 100, k=7, dst=3), 2: filler(2, 200, k=9, dst=10), 3: filler(3, 300)}, FAS)


def slot_row(week, slot):
    return next(a for a in week["assignment"] if a["slot"] == slot)


# ------------------------------------------------------------------- baseline --

def test_baseline_is_the_third_best_available_projection_each_week():
    st = kdst_league()
    levels, ref = streaming_baseline(st.free_agents, st.slots, WEEKS, st.projections)
    assert levels == {"D/ST": {w: 5.0 for w in WEEKS}, "K": {w: 6.5 for w in WEEKS}}
    assert ref["D/ST"][10] == {"espn_player_id": 903, "name": "D/ST 903", "pro_team": "KC", "points": 5.0,
                               "rank": 3, "available_projected": 4}
    # Fewer projected than the rank: the weakest projected one. Unavailable weeks and missing projections
    # never count, and a non-positive baseline is no streamer at all (never a manufactured 0).
    st.projections[902] = {10: 6.0}
    st.free_agents = [replace(p, unavailable_weeks=frozenset({11})) if p.espn_player_id == 901 else p
                      for p in st.free_agents if p.espn_player_id not in (903, 904)]
    levels, ref = streaming_baseline(st.free_agents, st.slots, WEEKS, st.projections)
    assert levels["D/ST"] == {10: 6.0, 12: 9.0, 13: 9.0}            # week 11: 901 out, 902 unprojected
    assert ref["D/ST"][10]["rank"] == 2 and ref["D/ST"][12]["rank"] == 1
    st.projections[901] = {w: 0.0 for w in WEEKS}
    assert "D/ST" not in streaming_baseline(st.free_agents, st.slots, (12,), st.projections)[0]


def test_only_positions_the_league_starts_get_a_baseline():
    st = kdst_league()
    no_k = [s for s in SLOTS if s != "K"]
    assert set(streaming_baseline(st.free_agents, no_k, WEEKS, st.projections)[0]) == {"D/ST"}


# --------------------------------------------------------------- lineup engine --

def test_streamer_fills_a_weak_slot_and_a_better_rostered_player_still_starts():
    st = with_streaming(kdst_league())
    week = project_week(st.rosters[1], st.slots, 10, st.projections)
    dst, k = slot_row(week, "D/ST"), slot_row(week, "K")
    assert dst == {"slot": "D/ST", "player_id": None, "position": "D/ST", "points": 5.0, "is_flex": False,
                   "streamer": True, "name": STREAMER_NAMES["D/ST"]}
    assert k["player_id"] == 109 and k["points"] == 7.0 and "streamer" not in k     # 7 beats the 6.5 stream
    assert 110 in {b["player_id"] for b in week["bench"]}           # the 3-point D/ST sits behind the stream
    assert all(b["player_id"] is not None for b in week["bench"])   # the streamer is never a bench player
    assert None not in starters(week) and 110 not in starters(week)
    raw = project_week(st.rosters[1], st.slots, 10, dict(st.projections))
    assert week["_total"] - raw["_total"] == pytest.approx(2.0)     # 5 - 3 at D/ST, nothing else changes
    assert all("streamer" not in a for a in raw["assignment"])


def test_a_rostered_player_wins_a_tie_with_the_streamer():
    st = kdst_league()
    st.projections[110] = {w: 5.0 for w in WEEKS}
    week = project_week(st.rosters[1], st.slots, 10, with_streaming(st).projections)
    assert slot_row(week, "D/ST")["player_id"] == 110


def test_opt_in_never_mutates_the_state_and_never_stacks():
    st = kdst_league()
    before = {pid: dict(per) for pid, per in st.projections.items()}
    on = with_streaming(st)
    assert isinstance(on.projections, StreamingProjections) and streaming_enabled(on)
    assert type(st.projections) is dict and st.projections == before and not streaming_enabled(st)
    assert dict(on.projections) == before                            # the ESPN numbers themselves are untouched
    assert with_streaming(on).projections.stream == on.projections.stream
    summary = stream_summary(on.projections, on.window())
    assert summary["enabled"] and summary["rank"] == 3 and summary["note"] == STREAM_NOTE
    assert [p["position"] for p in summary["positions"]] == ["K", "D/ST"]
    assert stream_summary(st.projections, st.window()) == {"enabled": False, "positions": []}


# --------------------------------------------------------------------- trades --

def test_trading_for_a_defense_is_worth_only_its_edge_over_streaming():
    st = kdst_league()
    # Team 1 swaps its 3-point D/ST for Team 2's 10-point D/ST, 4 weeks.
    raw = evaluate_trade(st, 1, 2, [110], [210], scenarios=False)
    assert raw["team_a"]["deltas"]["ros_delta"] == 28.0 and raw["team_b"]["deltas"]["ros_delta"] == -28.0
    assert raw["kdst_streaming"]["enabled"] is False
    on = evaluate_trade(with_streaming(st), 1, 2, [110], [210], scenarios=False)
    # Gain: 10 over the 5-point stream, not over the 3-point defense they would no longer start.
    # Loss: Team 2 streams a 5-point defense instead of starting the 3-point one it receives.
    assert on["team_a"]["deltas"]["ros_delta"] == 20.0 and on["team_b"]["deltas"]["ros_delta"] == -20.0
    assert on["kdst_streaming"]["enabled"] is True and STREAM_NOTE in on["assumptions"]
    assert not any(a.startswith("Each team's current roster is assumed") for a in on["assumptions"])
    after = on["team_b"]["lineups"]["after"][0]
    assert slot_row(after, "D/ST")["streamer"] is True


def test_losing_your_only_defense_costs_the_gap_to_streaming_not_the_whole_slot():
    st = kdst_league()
    # Team 2 sends its elite (and only) D/ST for Team 3's 4-point bench WR.
    raw = evaluate_trade(st, 2, 3, [210], [307], scenarios=False)
    on = evaluate_trade(with_streaming(st), 2, 3, [210], [307], scenarios=False)
    raw_dst = sum(w["points"] or 0 for wk in raw["team_a"]["lineups"]["after"] for w in wk["assignment"]
                  if w["slot"] == "D/ST")
    assert raw_dst == 0.0                                           # raw: an empty D/ST slot all four weeks
    assert raw["team_a"]["deltas"]["ros_delta"] == -40.0 and on["team_a"]["deltas"]["ros_delta"] == -20.0
    assert on["team_a"]["deltas"]["ros_delta"] - raw["team_a"]["deltas"]["ros_delta"] == pytest.approx(4 * 5.0)
    assert on["team_a"]["fit_notes"] == raw["team_a"]["fit_notes"]


def test_discovery_totals_equal_the_evaluator_with_streaming():
    st = with_streaming(kdst_league())
    ts = TradeSearch(st, 1, tuple(SHAPES), positions=None)
    for t, vec in ts.base.items():
        assert list(vec) == [w["_total"] for w in project_team(st.rosters[t], st.slots, st.window(),
                                                                st.projections)["weeks"]]
    rows = ts.top("my_gain", limit=8)["results"]
    assert rows
    for row in rows:
        assert detail(st, row)["consistent_with_manual_evaluator"], row["key"]
    # The elite defense is now worth 5/week to Team 1 over streaming (it was 7/week over its own defense).
    elite = next(r for r in TradeSearch(kdst_league(), 1, ("1-for-1",), positions=None)
                 .top("my_gain", limit=50)["results"] if r["sends"] == [110] and r["receives"] == [210])
    streamed = next(r for r in TradeSearch(st, 1, ("1-for-1",), positions=None).top("my_gain", limit=50)["results"]
                    if r["sends"] == [110] and r["receives"] == [210])
    assert elite["mine"]["ros_delta"] == 28.0 and streamed["mine"]["ros_delta"] == 20.0


def test_team_needs_count_the_streamer_at_its_slot():
    st = with_streaming(kdst_league())
    needs = team_needs(st, st.window(), 1, AssetBoard(st, st.window()))
    assert needs["D/ST"]["slot_points_per_week"] == 5.0              # the stream, not the benched 3 (nor 0)
    raw = kdst_league()
    assert team_needs(raw, raw.window(), 1, AssetBoard(raw, raw.window()))["D/ST"]["slot_points_per_week"] == 3.0


# -------------------------------------------------------------------- waivers --

def _p(pid, pos, pts):
    return {"espn_player_id": pid, "position": pos, "lineup_slot_id": 0, "available": True,
            "projections": {w: pts for w in (10, 11, 12)}}


def test_waiver_forward_gain_counts_later_weeks_only_above_the_stream():
    slots = ["D/ST"]
    mine = [_p(1, "D/ST", 3.0)]
    after = mine + [_p(2, "D/ST", 6.0)]
    raw = forward_effect(mine, after, slots, [10, 11, 12])
    assert raw["total_gain"] == 9.0
    stream = {"D/ST": {11: 5.0, 12: 5.0}}                           # week 10 is the streaming decision itself
    on = forward_effect(mine, after, slots, [10, 11, 12], stream)
    assert [g["gain"] for g in on["weeks"]] == [3.0, 1.0, 1.0] and on["total_gain"] == 5.0
    assert all(g["holes_after"] == 0 for g in on["weeks"])


def test_waiver_streaming_floor_skips_the_upcoming_week():
    pool = [{"espn_player_id": i, "position": "D/ST"} for i in (1, 2, 3, 4)] + \
           [{"espn_player_id": 9, "position": "K"}]
    ctx = {"weeks": [10, 11, 12], "projections": {1: {10: 9, 11: 9, 12: 9}, 2: {10: 7, 11: 7, 12: 4},
                                                   3: {11: 6, 12: 3}, 4: {11: 1}, 9: {11: 8}}}
    floor = streaming_floor(pool, ctx, {"D/ST": 1, "K": 1})
    assert floor == {"D/ST": {11: 6, 12: 3}, "K": {11: 8}}
    assert streaming_floor(pool, ctx, {"D/ST": 1, "K": 0}) == {"D/ST": {11: 6, 12: 3}}


def test_waiver_streaming_floor_ignores_players_on_a_covered_bye():
    pool = [{"espn_player_id": i, "position": "D/ST", "pro_team": t} for i, t in ((1, "NYJ"), (2, "BUF"), (3, "KC"))]
    ctx = {"weeks": [10, 11], "projections": {1: {11: 9}, 2: {11: 7}, 3: {11: 6}},
           "covered": {10, 11}, "games": {("BUF", 11): ("MIA",), ("KC", 11): ("DEN",)}}
    # NYJ has no week-11 game: its stored 9 never counts, so the #2 option is 6, not 7.
    assert streaming_floor(pool, ctx, {"D/ST": 1}, rank=2) == {"D/ST": {11: 6}}
    assert streaming_floor(pool, {**ctx, "covered": {10}}, {"D/ST": 1}, rank=2) == {"D/ST": {11: 7}}   # not covered
