"""V3 trade intelligence: flexible manual trades with exact forced drops, NFL-availability-aware
lineups (injury eligibility separate from ESPN status and fantasy IR), practical discovery with an
independent asset-balance diagnostic, the K/D/ST policy and heuristic larger shapes. Synthetic
inputs only; nothing here says anything about real Los Subies outcomes."""

import copy
from dataclasses import replace
from itertools import combinations

import duckdb
import pytest

from app.analytics import trades as trades_mod
from app.analytics.player_availability import (ELIGIBLE_UNCONFIRMED, INELIGIBLE, NO_WEEK_LEFT, RETURNED, UNKNOWN,
                                               derive, load_availability)
from app.analytics.team_lineups import known_total, project_team, project_week
from app.analytics.trade_assets import AssetBoard
from app.analytics.trade_discovery import SHAPES, TradeSearch, detail
from app.analytics.trade_practical import discover
from app.analytics.trades import (EvaluationUnavailable, TradeError, choose_forced_drops, evaluate_trade,
                                  legal_roster)

from synthetic_league import add_free_agents, synthetic_league
from synthetic_v3 import P, WEEKS, filler, league, market_league

KEYS = ("ros_delta", "regular_season_delta", "playoff_delta", "known_ros_delta")


def deltas(side):
    return {k: side["deltas"][k] for k in KEYS}


def brute_drops(state, before, after, incoming, window):
    """Reference forced-drop rule by full enumeration: the fewest drops, then the largest remaining
    known total, then the lowest ids, avoiding incoming players unless impossible."""
    pool = sorted(after, key=lambda p: p.espn_player_id)
    for k in range(1, len(pool) + 1):
        best = {}
        for combo in combinations(pool, k):
            rest = [p for p in after if p not in combo]
            if not legal_roster(rest, state, before):
                continue
            inc = any(p.espn_player_id in incoming for p in combo)
            key = (round(known_total(project_team(rest, state.slots, window, state.projections)), 9),
                   tuple(-p.espn_player_id for p in combo))
            if inc not in best or key > best[inc][0]:
                best[inc] = (key, combo)
        if best:
            return [p.espn_player_id for p in (best.get(False) or best[True])[1]]
    raise AssertionError("no legal roster")


def manual_side(state, team, sends, receives, window=None):
    """Independent recomputation of one side: roster swap, brute-force drops, lineups before/after."""
    window = window or state.window()
    roster = state.rosters[team]
    by = {p.espn_player_id: p for ps in state.rosters.values() for p in ps}
    pre = [p for p in roster if p.espn_player_id not in sends] + [replace(by[x], on_ir=False) for x in receives]
    drops = [] if legal_roster(pre, state, roster) else brute_drops(state, roster, pre, set(receives), window)
    after = [p for p in pre if p.espn_player_id not in drops]
    b = project_team(roster, state.slots, window, state.projections)
    a = project_team(after, state.slots, window, state.projections)
    return round(known_total(a) - known_total(b), 2), drops


# ------------------------------------------------------------ flexible manual trades --

def wide_league(capacity=13, limits=None):
    t1 = filler(1, 100, rb=(12, 10, 8, 5), wr=(13, 11, 9, 6), te=(8, 4))
    t2 = filler(2, 200, rb=(14, 9, 7, 3), wr=(12, 10, 7, 5), te=(9, 5))
    return league({1: t1, 2: t2}, capacity=capacity, limits=limits)


@pytest.mark.parametrize("a,b", [
    ([102, 103, 106], [205]),                          # 3-for-1
    ([102], [205, 206, 210]),                          # 1-for-3
    ([102, 106, 110], [202, 207]),                     # 3-for-2
    ([102, 106], [202, 207, 211]),                     # 2-for-3
    ([102, 106, 110], [202, 207, 211]),                # 3-for-3
    ([102, 103, 106, 107], [202, 203, 206, 207]),      # 4-for-4
    ([102, 103, 106, 107], [202, 206]),                # 4-for-2
])
def test_arbitrary_shapes_match_an_independent_recomputation(a, b):
    st = wide_league()
    res = evaluate_trade(st, 1, 2, a, b)
    assert res["trade"]["shape"] == f"{len(a)}-for-{len(b)}"
    for side, team, sends, gets in ((res["team_a"], 1, a, b), (res["team_b"], 2, b, a)):
        expect, drops = manual_side(st, team, sends, gets)
        assert side["deltas"]["known_ros_delta"] == expect
        assert [d["espn_player_id"] for d in side["roster"]["forced_drops"]] == drops
        assert side["roster"]["active_after"] <= st.active_capacity


def test_uneven_trade_needing_more_than_three_forced_drops_is_exact():
    st = wide_league()
    res = evaluate_trade(st, 1, 2, [104], [202, 203, 206, 207, 211])        # 1-for-5: four drops for team 1
    drops = [d["espn_player_id"] for d in res["team_a"]["roster"]["forced_drops"]]
    assert len(drops) == 4 and res["team_a"]["roster"]["issues_before_drops"]["over_capacity_by"] == 4
    expect, ref = manual_side(st, 1, [104], [202, 203, 206, 207, 211])
    assert drops == ref and res["team_a"]["deltas"]["known_ros_delta"] == expect
    search = res["team_a"]["roster"]["forced_drop_search"]
    assert search["drops"] == 4 and search["lineup_solves"] < search["candidate_sets"]   # the bound prunes


def test_position_limits_force_drops_at_that_position_and_are_reported():
    st = wide_league(capacity=20, limits={"RB": 4})
    res = evaluate_trade(st, 1, 2, [106], [202, 203])                       # two RBs in, nothing out at RB
    side = res["team_a"]
    assert side["roster"]["issues_before_drops"]["position_limits"] == [
        {"position": "RB", "limit": 4, "count_before_drops": 6, "over_by": 2}]
    assert {d["position"] for d in side["roster"]["forced_drops"]} == {"RB"}
    assert manual_side(st, 1, [106], [202, 203])[1] == [d["espn_player_id"] for d in side["roster"]["forced_drops"]]


def test_shared_drop_chooser_equals_brute_force_on_random_rosters():
    st = synthetic_league(n_teams=2, weeks=range(12, 16), seed=5)
    window = st.window()
    mine, theirs = st.rosters[1], st.rosters[2]
    for k in (1, 2, 3):
        gets = [replace(p, on_ir=False) for p in theirs[2:2 + k + 1]]
        pre = mine[1:] + gets
        ref = brute_drops(st, mine, pre, {p.espn_player_id for p in gets}, window)
        res = choose_forced_drops(pre, mine, st, {p.espn_player_id for p in gets},
                                  lambda ps: known_total(project_team(ps, st.slots, window, st.projections)))
        assert [p.espn_player_id for p in (res["best"].get(False) or res["best"][True])[1]] == ref


def test_negative_projection_solves_every_legal_drop_set():
    st = wide_league()
    st.projections[110] = {w: -3.0 for w in WEEKS}
    res = evaluate_trade(st, 1, 2, [102], [202, 203, 206])
    assert res["team_a"]["roster"]["forced_drop_search"]["method"] == "exact (every legal set solved)"
    assert manual_side(st, 1, [102], [202, 203, 206])[1] == [d["espn_player_id"] for d in res["team_a"]["roster"]["forced_drops"]]


def test_forced_drop_budget_reports_unavailable_instead_of_guessing(monkeypatch):
    st = wide_league()
    monkeypatch.setattr(trades_mod, "MAX_DROP_SETS", 10)
    with pytest.raises(EvaluationUnavailable, match="evaluation unavailable"):
        evaluate_trade(st, 1, 2, [104], [202, 203, 206, 207, 211])


@pytest.mark.parametrize("args,msg", [
    ((1, 1, [102], [103]), "different"),
    ((1, 2, [102, 102], [202]), "more than once"),
    ((1, 2, [102], [102]), "more than once"),
    ((1, 2, [202], [203]), "not on team 1"),
    ((1, 2, [101, 102, 103, 104, 105, 106, 107], [202]), "between 1 and 6"),
    ((1, 2, [], [202]), "between 1 and 6"),
])
def test_invalid_flexible_trades(args, msg):
    with pytest.raises(TradeError, match=msg):
        evaluate_trade(wide_league(), *args)


def test_per_side_limit_is_configurable():
    st = wide_league()
    with pytest.raises(TradeError, match="between 1 and 3"):
        evaluate_trade(st, 1, 2, [102, 103, 106, 107], [202], max_per_side=3)
    assert evaluate_trade(st, 1, 2, [102, 103, 106, 107], [202], max_per_side=4)["trade"]["shape"] == "4-for-1"


def test_traded_fantasy_ir_player_arrives_active_and_missing_projection_stays_unknown():
    st = wide_league()
    st.rosters[2].append(P(299, "RB", on_ir=True))
    st.projections[299] = {w: 11.0 for w in WEEKS}
    res = evaluate_trade(st, 1, 2, [104], [299])
    before = project_team(st.rosters[2], st.slots, st.window(), st.projections)
    assert all(299 in [x["player_id"] for x in w["ir"]] for w in before["weeks"])   # his old team never counted him
    assert res["team_b"]["deltas"]["known_ros_delta"] == manual_side(st, 2, [299], [104])[0]
    assert res["team_a"]["deltas"]["known_ros_delta"] == manual_side(st, 1, [104], [299])[0] > 0   # arrives active
    st.projections[202].pop(11)
    res = evaluate_trade(st, 1, 2, [102], [202])
    assert res["team_a"]["deltas"]["ros_delta"] is None and res["team_a"]["deltas"]["known_ros_delta"] is not None


# --------------------------------------------------------------------- NFL availability --

def etienne_rows(placed=5, through=5, code="R01"):
    return [{"week": w, "status": "ACT", "abbr": "A01", "team": "NO"} for w in range(1, placed)] + \
           [{"week": w, "status": "RES", "abbr": code, "team": "NO"} for w in range(placed, through + 1)]


def test_etienne_like_reserve_player_is_ineligible_until_week_9():
    rec = derive(espn_status="INJURY_RESERVE", on_ir=False, rows=etienne_rows(), team_games=list(range(1, 19)),
                 weeks=tuple(range(6, 18)), proj={w: 14.0 for w in range(6, 18)})
    assert rec["status"] == INELIGIBLE and rec["designation_since_week"] == 5
    assert rec["earliest_eligible_week"] == 9 and rec["unavailable_weeks"] == [6, 7, 8]
    assert rec["expected_return_week"] is None and rec["return_confirmed"] is False
    assert rec["warnings"][0] == ("Currently on NFL reserve/injured. Earliest eligible return week 9; actual return not "
                                  "confirmed. Near-term lineup contribution (weeks 6-8) is unavailable.")
    assert [x["week"] for x in rec["ignored_projection"]] == [6, 7, 8]
    assert "ESPN still projects 42.0 points in weeks 6-8" in rec["warnings"][1]


def test_bye_week_and_missing_schedule_and_gaps_in_the_eligibility_rule():
    games = [w for w in range(1, 19) if w != 7]                         # bye in week 7: games 5, 6, 8, 9 missed
    assert derive(espn_status=None, on_ir=False, rows=etienne_rows(), team_games=games, weeks=(6, 7, 8, 9, 10))[
        "earliest_eligible_week"] == 10
    lower = derive(espn_status=None, on_ir=False, rows=etienne_rows(), team_games=None, weeks=(6, 7, 8, 9, 10))
    assert lower["earliest_eligible_week"] == 9 and "no earlier than week 9" in lower["eligibility_basis"]
    gap = etienne_rows()[:3] + [{"week": 5, "status": "RES", "abbr": "R01", "team": "NO"}]   # no row for week 4
    assert derive(espn_status=None, on_ir=False, rows=gap, team_games=list(range(1, 19)), weeks=(6, 7, 8))[
        "designation_since_week"] == 4                                  # earliest possible start: conservative


def test_eligibility_states_are_kept_distinct():
    w = tuple(range(9, 18))
    unconfirmed = derive(espn_status="INJURY_RESERVE", on_ir=True, rows=etienne_rows(through=8),
                         team_games=list(range(1, 19)), weeks=w)
    assert unconfirmed["status"] == ELIGIBLE_UNCONFIRMED and unconfirmed["unavailable_weeks"] == []
    back = derive(espn_status="ACTIVE", on_ir=True, rows=etienne_rows(through=8) + [{"week": 9, "status": "ACT", "abbr": "A01"}],
                  team_games=list(range(1, 19)), weeks=w)
    assert back["status"] == RETURNED and back["return_confirmed"] and back["unavailable_weeks"] == []
    assert any("fantasy IR slot but not NFL-ineligible" in x for x in back["warnings"])
    espn_only = derive(espn_status="INJURY_RESERVE", on_ir=False, rows=[], team_games=None, weeks=w)
    assert espn_only["status"] == UNKNOWN and espn_only["unavailable_weeks"] == [] \
        and espn_only["earliest_eligible_week"] is None
    other = derive(espn_status=None, on_ir=False, rows=etienne_rows(code="R33"), team_games=list(range(1, 19)), weeks=w)
    assert other["status"] == UNKNOWN and other["unavailable_weeks"] == []
    late = derive(espn_status=None, on_ir=False, rows=etienne_rows(placed=15, through=15), team_games=list(range(1, 19)),
                  weeks=(16, 17))
    assert late["status"] == NO_WEEK_LEFT and late["unavailable_weeks"] == [16, 17]
    healthy = derive(espn_status="ACTIVE", on_ir=False, rows=[{"week": 5, "status": "ACT", "abbr": "A01"}],
                     team_games=list(range(1, 19)), weeks=w)
    assert healthy["status"] == "available" and healthy["warnings"] == []


def test_partial_schedule_is_never_read_as_missing_games():
    from app.analytics.player_availability import complete_schedules
    full = {"NO": [w for w in range(1, 19) if w != 11], "ATL": list(range(1, 19))}
    assert complete_schedules(full) == full
    gappy = {"NO": [w for w in range(1, 19) if w not in (6, 7)], "ATL": list(range(1, 19))}
    assert "NO" not in complete_schedules(gappy) and "ATL" in complete_schedules(gappy)
    assert complete_schedules({"NO": list(range(1, 11))}) == {}           # only weeks 1-10 loaded
    con = duckdb.connect()
    con.execute("CREATE TABLE fantasy_players (espn_player_id BIGINT)")
    con.execute("CREATE TABLE nfl_rosters_weekly (season INT, week INT, espn_id VARCHAR, status VARCHAR, "
                "status_description_abbr VARCHAR, team VARCHAR)")
    con.execute("INSERT INTO nfl_rosters_weekly VALUES (2026, 4, '299', 'ACT', 'A01', 'NO'), (2026, 15, '299', 'RES', 'R01', 'NO')")
    con.execute("CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR, home_team VARCHAR, away_team VARCHAR)")
    for w in range(1, 16):                                              # weeks 16-18 not loaded yet
        con.execute("INSERT INTO nfl_schedules VALUES (2026, ?, 'REG', 'NO', 'ATL')", [w])
    rec = load_availability(con, 2026, [P(299, "RB", team="NO")], (16, 17), {})[299]
    assert rec["status"] != NO_WEEK_LEFT                                  # absent rows prove nothing
    assert rec["earliest_eligible_week"] == 9 and rec["unavailable_weeks"] == []   # start 5 (gap) + 4 games


def test_unavailable_known_zero_and_missing_are_three_different_things():
    zero, out, missing = P(1, "RB"), P(2, "RB", team="NO"), P(3, "RB")
    out = replace(out, unavailable_weeks=frozenset({10}))
    proj = {1: {10: 0.0}, 2: {10: 14.0}}
    slots = ["RB", "RB"]
    wk = project_week([zero, out, missing], slots, 10, proj)
    assert wk["projection_complete"] is False and wk["missing_projection_ids"] == [3]   # missing: unknown
    assert wk["unavailable"] == [{"player_id": 2, "position": "RB", "points": 14.0}]     # kept, never counted
    assert wk["known_lineup_points"] == 0.0                                                # a measured zero starts
    wk2 = project_week([zero, out], slots, 10, proj)
    assert wk2["projection_complete"] is True and wk2["projected_lineup_points"] == 0.0


def injured_league(on_ir=False):
    """Team 2 owns an Etienne-like RB 'Etienne' (NFL IR through week 12 of weeks 10-13 window: eligible week 13)."""
    t1 = filler(1, 100, rb=(9, 8, 4), wr=(14, 13, 12, 11))
    t2 = filler(2, 200, rb=(10, 9, 3), wr=(9, 8, 4)) + [(299, "RB", 18, {"name": "Etienne", "on_ir": on_ir,
                                                                          "status": "INJURY_RESERVE"})]
    st = league({1: t1, 2: t2}, capacity=10)
    rec = derive(espn_status="INJURY_RESERVE", on_ir=on_ir, rows=etienne_rows(placed=9, through=9),
                 team_games=list(range(1, 19)), weeks=WEEKS, proj=st.projections[299])
    assert rec["earliest_eligible_week"] == 13 and rec["unavailable_weeks"] == [10, 11, 12]
    st.rosters[2] = [replace(p, unavailable_weeks=frozenset(rec["unavailable_weeks"])) if p.espn_player_id == 299 else p
                     for p in st.rosters[2]]
    st.availability = {299: rec}
    return st


@pytest.mark.parametrize("on_ir", [False, True])
def test_traded_nfl_ir_player_gives_nothing_in_ineligible_weeks_but_keeps_playoff_value(on_ir):
    st = injured_league(on_ir=on_ir)
    res = evaluate_trade(st, 1, 2, [106], [299])            # my WR4 for their injured RB
    weekly = {w["week"]: w for w in res["team_a"]["weekly"]}
    assert all(299 not in weekly[w]["entered"] for w in (10, 11, 12))           # cannot start while ineligible
    assert 299 in weekly[13]["entered"]                                           # eligible playoff week counts
    assert res["team_a"]["deltas"]["playoff_delta"] > 0 and res["team_a"]["deltas"]["regular_season_delta"] < 0
    assert any("Earliest eligible return week 13" in w for x in res["injury_warnings"] for w in x["warnings"])
    sc = {x["scenario"]: x for x in res["injury_scenarios"][0]["scenarios"]}
    assert sc["earliest_eligible_return"]["canonical"] and sc["earliest_eligible_return"]["return_week"] == 13
    assert sc["earliest_eligible_return"]["team_a"]["ros_delta"] == res["team_a"]["deltas"]["ros_delta"]
    assert sc["no_return"]["team_a"]["ros_delta"] < sc["earliest_eligible_return"]["team_a"]["ros_delta"]
    assert "probability" not in str(res["injury_scenarios"]).replace("No probability", "")
    # Before the trade his own team got nothing from him in weeks 10-12 either (bench slot or IR slot).
    before = {w["week"]: w for w in res["team_b"]["lineups"]["before"]}
    assert all(299 not in [a["player_id"] for a in before[w]["assignment"]] for w in (10, 11, 12))


def test_fantasy_ir_player_who_is_nfl_active_stays_in_his_slot_for_his_owner():
    st = injured_league(on_ir=True)
    rec = derive(espn_status="ACTIVE", on_ir=True, rows=[{"week": 9, "status": "ACT", "abbr": "A01"}],
                 team_games=list(range(1, 19)), weeks=WEEKS)
    st.rosters[2] = [replace(p, unavailable_weeks=frozenset()) if p.espn_player_id == 299 else p for p in st.rosters[2]]
    st.availability = {299: rec}
    team = project_team(st.rosters[2], st.slots, st.window(), st.projections)
    assert all(299 in [x["player_id"] for x in w["ir"]] for w in team["weeks"])   # IR slot held as synced


def test_healthy_trades_are_unchanged_by_the_availability_layer():
    st = wide_league()
    plain = evaluate_trade(st, 1, 2, [102, 106], [202])
    st2 = copy.deepcopy(st)
    st2.availability = {202: derive(espn_status="ACTIVE", on_ir=False, rows=[], team_games=None, weeks=WEEKS)}
    again = evaluate_trade(st2, 1, 2, [102, 106], [202])
    assert deltas(plain["team_a"]) == deltas(again["team_a"]) and deltas(plain["team_b"]) == deltas(again["team_b"])
    assert again["injury_scenarios"] == [] and again["injury_warnings"] == []


def test_discovery_matches_the_analyzer_with_an_injured_player():
    st = injured_league()
    ts = TradeSearch(st, 1, positions=None)
    rows = [ts.evaluate(c) for c in ts.cands if 299 in c[3]]
    rows = [r for r in rows if r]
    assert rows
    for r in rows:
        d = detail(st, r)
        assert d["consistent_with_manual_evaluator"], r["key"]


def test_availability_loader_reads_nflverse_rosters_schedule_and_espn_status():
    con = duckdb.connect()
    con.execute("CREATE TABLE fantasy_players (espn_player_id BIGINT, detail_synced_at TIMESTAMP)")
    con.execute("INSERT INTO fantasy_players VALUES (299, '2026-10-14 08:00:00'), (300, '2026-10-14 08:00:00')")
    con.execute("CREATE TABLE player_id_map (espn_player_id BIGINT, gsis_id VARCHAR)")
    con.execute("INSERT INTO player_id_map VALUES (299, '00-0001'), (300, '00-0002')")
    con.execute("CREATE TABLE nfl_rosters_weekly (season INT, week INT, game_type VARCHAR, gsis_id VARCHAR, espn_id VARCHAR, "
                "status VARCHAR, status_description_abbr VARCHAR, team VARCHAR)")
    for w in range(1, 6):
        con.execute("INSERT INTO nfl_rosters_weekly VALUES (2026, ?, 'REG', '00-0001', '299', ?, ?, 'NO')",
                    [w, "RES" if w == 5 else "ACT", "R01" if w == 5 else "A01"])
        con.execute("INSERT INTO nfl_rosters_weekly VALUES (2026, ?, 'REG', '00-0002', '300', 'ACT', 'A01', 'NO')", [w])
    con.execute("CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR, home_team VARCHAR, away_team VARCHAR)")
    for w in range(1, 19):
        if w != 11:
            con.execute("INSERT INTO nfl_schedules VALUES (2026, ?, 'REG', 'NO', 'ATL')", [w])
    players = [P(299, "RB", team="NO", status="INJURY_RESERVE"), P(300, "RB", team="NO", status="ACTIVE")]
    out = load_availability(con, 2026, players, tuple(range(6, 18)), {299: {w: 14.0 for w in range(6, 18)}})
    assert out[299]["status"] == INELIGIBLE and out[299]["earliest_eligible_week"] == 9
    assert out[299]["unavailable_weeks"] == [6, 7, 8] and "week 5" in out[299]["nfl_designation_source"]
    assert out[300]["status"] == "available" and out[300]["unavailable_weeks"] == []
    bare = duckdb.connect()
    bare.execute("CREATE TABLE fantasy_players (espn_player_id BIGINT)")
    assert load_availability(bare, 2026, players, (6, 7), {})[299]["status"] == UNKNOWN   # ESPN-only fallback


# ------------------------------------------------------------- recommendation quality --

def practical_rows(out):
    return [r for k in ("practical",) for r in out["sections"].get(k, [])]


def test_lopsided_two_strong_assets_for_one_is_not_a_practical_recommendation():
    st = market_league()
    r = evaluate_trade(st, 1, 2, [105], [204, 209])          # "Diggs" for "Jeanty" + "Bowers"
    assert r["team_a"]["deltas"]["ros_delta"] > 0 and r["team_b"]["deltas"]["ros_delta"] > 0   # both lineups gain
    board = AssetBoard(st, st.window())
    bal = board.package([105], [204, 209])
    assert bal["label"] == "they_surrender_multiple_strong" and bal["they_send_starter_caliber"] == 2
    ts = TradeSearch(st, 1, tuple(SHAPES), opponents=[2])
    out = discover(ts, "practical", opponent=2, limit=50)
    keys = [x["key"] for k in out["sections"] for x in out["sections"][k]]
    assert "1-for-2:2:105:204-209" not in keys and out["excluded"]["lopsided"] >= 1
    mutual = discover(ts, "mutual", opponent=2, limit=200)["sections"]["results"]
    row = next(x for x in mutual if x["key"] == "1-for-2:2:105:204-209")       # still visible, labelled
    assert row["asset_balance"]["label"] == "they_surrender_multiple_strong"
    assert any("uneven despite the lineup benefits" in c for c in row["rationale"]["concerns"])


def test_balanced_positional_need_exchange_leads_the_practical_list():
    st = market_league()
    ts = TradeSearch(st, 1, tuple(SHAPES) + ("2-for-2",))
    out = discover(ts, "practical", limit=6)
    top = practical_rows(out)[0]
    assert top["opponent_team_id"] == 3 and top["asset_balance"]["label"] == "balanced"
    assert {st_pos for st_pos in (ts.players[x].position for x in top["sends"])} == {"WR"}
    assert {ts.players[x].position for x in top["receives"]} == {"RB"}
    assert top["mine"]["ros_delta"] > 0 and top["theirs"]["ros_delta"] > 0 and top["consistent_with_manual_evaluator"]
    text = " ".join(top["rationale"]["points"])
    assert "You are short at RB" in text and "Team 3 is short at WR" in text and "Team 3 can spare" in text
    assert top["rationale"]["trade_type"]["kind"] in ("positional_exchange", "depth_for_starter")
    assert all(x["tier"] == "practical" and x["theirs"]["ros_delta"] > 0 for x in practical_rows(out))


def test_benched_top_asset_keeps_independent_asset_context():
    st = market_league()
    board = AssetBoard(st, st.window())
    bowers = board.profiles[209]
    team2 = project_team(st.rosters[2], st.slots, st.window(), st.projections)
    starts = sum(209 in [a["player_id"] for a in w["assignment"]] for w in team2["weeks"])
    assert starts == 0                                                   # never starts for his own team
    assert bowers["starter_caliber"] and bowers["par"] > 0 and bowers["position_rank"] == 2


def test_losing_an_essential_starter_or_leaving_a_hole_is_a_concern():
    st = market_league()
    st.rosters[3] = [p for p in st.rosters[3] if p.espn_player_id != 309] + [P(309, "TE")]   # their only TE
    ts = TradeSearch(st, 1, ("1-for-1",), opponents=[3], positions=None)
    out = discover(ts, "all", sort="my_gain", opponent=3, limit=200)
    row = next(x for x in out["sections"]["results"] if x["receives"] == [309])
    joined = " ".join(row["rationale"]["concerns"])
    assert "They surrender a player who currently starts in most weeks" in joined
    assert "Team 3 would have an empty TE slot" in joined


def test_needs_ranks_are_tie_aware_and_unknown_when_projections_are_missing():
    from app.analytics.trade_assets import league_needs
    st = league({t: filler(t, 100 * t) for t in (1, 2, 3, 4)})      # identical rosters: nobody is short
    needs = league_needs(st, st.window(), AssetBoard(st, st.window()))
    assert all(needs[t]["RB"]["league_rank"] == 1 and not needs[t]["RB"]["short"] for t in needs)
    st.projections.pop(408)                                          # team 4's only TE: no projection
    needs = league_needs(st, st.window(), AssetBoard(st, st.window()))
    assert needs[4]["TE"]["slot_points_per_week"] is None and needs[4]["TE"]["league_rank"] is None
    assert needs[4]["TE"]["incomplete"] and not needs[4]["TE"]["short"] and needs[1]["TE"]["teams"] == 3


def test_incomplete_projections_never_get_a_balanced_label():
    st = market_league()
    st.projections[302].pop(11)
    board = AssetBoard(st, st.window())
    bal = board.package([106], [302])
    assert bal["label"] == "insufficient_data" and bal["you_send_par"] is None
    ts = TradeSearch(st, 1, ("1-for-1",), opponents=[3])
    out = discover(ts, "practical", opponent=3, limit=50)
    assert all(302 not in x["receives"] for x in practical_rows(out))


def dst_league():
    """You have a terrible defense; Team 2 owns the only elite one. Free agents stream at 7/week."""
    t1 = filler(1, 100, rb=(12, 6, 5), wr=(13, 12, 11, 10), dst=1)
    t2 = filler(2, 200, rb=(13, 12, 11, 10), wr=(7, 6, 3), dst=15)
    t3 = filler(3, 300, rb=(11, 10, 9), wr=(12, 11, 4), dst=7)
    fas = [(901, "D/ST", 7), (902, "K", 7), (903, "RB", 4), (904, "WR", 5)]
    return league({1: t1, 2: t2, 3: t3}, fas, capacity=11)


def test_dst_driven_gains_no_longer_lead_discovery():
    st = dst_league()
    elite, kdst = 211, {110, 111, 210, 211, 309, 310}
    v2 = TradeSearch(st, 1, tuple(SHAPES), positions=None).top("my_gain", limit=5)["results"]
    assert all(elite in r["receives"] for r in v2)                       # V2 default order: all about the D/ST
    ts = TradeSearch(st, 1, tuple(SHAPES))                               # V3 default: QB/RB/WR/TE only
    assert all(p not in kdst for c in ts.cands for p in c[2] + c[3])
    out = discover(ts, "practical", limit=10)
    assert practical_rows(out) and all(not kdst & set(x["sends"] + x["receives"]) for x in practical_rows(out))
    allpos = ("QB", "RB", "WR", "TE", "K", "D/ST")
    with_kdst = discover(TradeSearch(st, 1, tuple(SHAPES), positions=allpos), "practical", limit=10)
    assert all(elite not in x["receives"] for x in practical_rows(with_kdst))   # wrecks their D/ST: not mutual
    upside = discover(TradeSearch(st, 1, tuple(SHAPES), positions=allpos), "upside", limit=3)
    row = upside["sections"]["results"][0]
    assert elite in row["receives"] and row["theirs"]["ros_delta"] < 0           # still explorable, labelled
    prof = next(x for x in row["asset_balance"]["they_send"] if x["espn_player_id"] == elite)
    assert prof["replacement_per_week"] == 7.0 and prof["par"] == 32.0           # 15 vs the 7-point streamer


def test_heuristic_shapes_are_exact_rows_with_non_exhaustive_coverage():
    st = synthetic_league(n_teams=6, weeks=range(12, 18))
    add_free_agents(st, 40)
    ts = TradeSearch(st, 1, ("1-for-1", "2-for-2", "3-for-1", "1-for-3"))
    h = ts.heuristic_top("min_side", None, None, 20)
    assert h["search"]["exhaustive"] is False and h["search"]["packages_evaluated"] > 0
    assert len(h["search"]["per_opponent"]) == 5 and "not a proven top list" in h["search"]["note"]
    assert h["results"] and {r["shape"] for r in h["results"]} <= {"2-for-2", "3-for-1", "1-for-3"}
    for r in h["results"][:6]:
        assert r["search_method"] == "heuristic_beam"
        assert detail(st, r)["consistent_with_manual_evaluator"], r["key"]
    exact = ts.top("min_side", limit=20)
    assert all(r["search_method"] == "exact" for r in exact["results"]) and exact["search"]["exhaustive"]


@pytest.mark.parametrize("phase,key", [("regular", "regular_season_delta"), ("playoff", "playoff_delta")])
def test_mutual_mode_uses_the_selected_phase(phase, key):
    st = market_league()
    ts = TradeSearch(st, 1, tuple(SHAPES))
    rows = discover(ts, "mutual", phase=phase, limit=40)["sections"]["results"]
    assert rows and all(r["mine"][key] > 0 and r["theirs"][key] > 0 for r in rows)
    every = [x for x in ts.top("min_side_" + ("regular" if phase == "regular" else "playoff"), limit=200)["results"]
             if x["mine"][key] > 0 and x["theirs"][key] > 0]
    assert [r["key"] for r in rows] == [x["key"] for x in every][:40]


def test_practical_rows_all_match_the_manual_analyzer_and_carry_no_probability():
    st = market_league()
    out = discover(TradeSearch(st, 1, tuple(SHAPES) + ("2-for-2", "3-for-1", "1-for-3")), "practical", limit=8)
    assert out["consistency_failures"] == 0 and out["consistency_checked"] > 0
    assert "probab" not in str(out["sections"]).lower().replace("no probability", "")


# ------------------------------------------------------------- regression tests: IR symmetry & ties --

def test_fantasy_ir_uncertainty_is_symmetric_between_anchor_and_opponent():
    from app.analytics.trade_assets import AssetBoard
    from app.analytics.trade_practical import classify, injury_flags

    st = injured_league(on_ir=True)
    board = AssetBoard(st, st.window())

    # Case A: Team 1 is anchor, RECEIVING fantasy-IR player 299 from Team 2
    ts1 = TradeSearch(st, 1, ("1-for-1",), opponents=[2], positions=None)
    cand_recv = next(c for c in ts1.cands if 299 in c[3])
    row_recv = ts1.evaluate(cand_recv)
    assert row_recv["mine"]["playoff_delta"] > 0 and row_recv["theirs"]["playoff_delta"] > 0
    bal_recv = board.package(row_recv["sends"], row_recv["receives"])
    inj_recv = injury_flags(st, row_recv["sends"] + row_recv["receives"])
    tier_recv, reasons_recv = classify(row_recv, bal_recv, inj_recv, "playoff", "standard")

    assert tier_recv == "speculative"
    assert any("not confirmed" in r or "not established" in r for r in reasons_recv)

    # Case B: Team 2 is anchor, SENDING fantasy-IR player 299 to Team 1 (same trade, reversed anchor)
    ts2 = TradeSearch(st, 2, ("1-for-1",), opponents=[1], positions=None)
    cand_send = next(c for c in ts2.cands if 299 in c[2] and cand_recv[2][0] in c[3])
    row_send = ts2.evaluate(cand_send)
    assert row_send["mine"]["playoff_delta"] == row_recv["theirs"]["playoff_delta"]
    assert row_send["theirs"]["playoff_delta"] == row_recv["mine"]["playoff_delta"]
    bal_send = board.package(row_send["sends"], row_send["receives"])
    inj_send = injury_flags(st, row_send["sends"] + row_send["receives"])
    tier_send, reasons_send = classify(row_send, bal_send, inj_send, "playoff", "standard")

    # Symmetrical: anchor sending the fantasy-IR player must ALSO be speculative
    assert tier_send == "speculative"
    assert any("not confirmed" in r or "not established" in r for r in reasons_send)


def test_fantasy_ir_player_with_confirmed_return_is_concerns_not_speculative():
    from app.analytics.trade_assets import AssetBoard
    from app.analytics.trade_practical import classify, injury_flags

    st = injured_league(on_ir=True)
    # Player was on IR, but is now activated on the NFL roster (return_confirmed=True, status=RETURNED)
    rec = derive(espn_status="ACTIVE", on_ir=True, rows=etienne_rows(through=8) + [{"week": 9, "status": "ACT", "abbr": "A01"}],
                 team_games=list(range(1, 19)), weeks=WEEKS)
    assert rec["status"] == "returned_from_reserve" and rec["return_confirmed"] is True
    st.availability[299] = rec
    st.rosters[2] = [replace(p, unavailable_weeks=frozenset()) if p.espn_player_id == 299 else p for p in st.rosters[2]]

    board = AssetBoard(st, st.window())

    # Anchor sends
    cand = next(c for c in TradeSearch(st, 2, ("1-for-1",), opponents=[1], positions=None).cands if 299 in c[2])
    row = TradeSearch(st, 2, ("1-for-1",), opponents=[1], positions=None).evaluate(cand)
    bal = board.package(row["sends"], row["receives"])
    inj = injury_flags(st, row["sends"] + row["receives"])
    tier, reasons = classify(row, bal, inj, "playoff", "standard")
    assert tier == "concerns"
    assert any("Activated from an NFL reserve list" in r for r in reasons)

    # Anchor receives (with strictness="off" to isolate injury tier from lopsided exclusion)
    cand_recv = next(c for c in TradeSearch(st, 1, ("1-for-1",), opponents=[2], positions=None).cands if 299 in c[3])
    row_recv = TradeSearch(st, 1, ("1-for-1",), opponents=[2], positions=None).evaluate(cand_recv)
    bal_recv = board.package(row_recv["sends"], row_recv["receives"])
    inj_recv = injury_flags(st, row_recv["sends"] + row_recv["receives"])
    tier_recv, reasons_recv = classify(row_recv, bal_recv, inj_recv, "playoff", "off")
    assert tier_recv == "concerns"
    assert any("Activated from an NFL reserve list" in r for r in reasons_recv)


def test_nfl_ineligible_on_normal_bench_slot_is_concerns_not_speculative():
    from app.analytics.trade_assets import AssetBoard
    from app.analytics.trade_practical import classify, injury_flags

    # Injured player on normal bench (on_ir=False) with verified ineligible weeks
    st = injured_league(on_ir=False)
    board = AssetBoard(st, st.window())
    row = {"mine": {"playoff_delta": 9.0}, "theirs": {"playoff_delta": 5.0}, "forced_drops": {"mine": [], "theirs": []}, "sends": [102], "receives": [299]}
    bal = {"label": "balanced", "text": "Asset packages appear relatively balanced"}
    inj = injury_flags(st, row["sends"] + row["receives"])
    assert any(f["status"] == "nfl_ineligible" and not f["fantasy_ir_slot"] for f in inj)

    tier, reasons = classify(row, bal, inj, "playoff", "standard")
    # Normal bench slot with verified ineligible weeks is a concern, not speculative
    assert tier == "concerns"
    assert any("Earliest eligible return week 13" in r for r in reasons)


def test_unconfirmed_return_and_missing_availability_are_speculative():
    from app.analytics.trade_assets import AssetBoard
    from app.analytics.trade_practical import classify, injury_flags

    # 1. ELIGIBLE_UNCONFIRMED on bench slot
    st = injured_league(on_ir=False)
    st.availability[299] = derive(espn_status="INJURY_RESERVE", on_ir=False, rows=etienne_rows(through=8),
                                  team_games=list(range(1, 19)), weeks=WEEKS)
    assert st.availability[299]["status"] == "eligible_return_unconfirmed"
    row = {"mine": {"playoff_delta": 9.0}, "theirs": {"playoff_delta": 5.0}, "forced_drops": {"mine": [], "theirs": []}, "sends": [102], "receives": [299]}
    bal = {"label": "balanced", "text": "Asset packages appear relatively balanced"}
    inj = injury_flags(st, row["sends"] + row["receives"])
    tier, reasons = classify(row, bal, inj, "playoff", "standard")
    assert tier == "speculative"
    assert any("his return is not established" in r for r in reasons)

    # 2. UNKNOWN availability (ESPN only, no NFL roster history)
    st2 = injured_league(on_ir=False)
    st2.availability[299] = derive(espn_status="INJURY_RESERVE", on_ir=False, rows=[], team_games=None, weeks=WEEKS)
    assert st2.availability[299]["status"] == "reserve_eligibility_unknown"
    inj2 = injury_flags(st2, row["sends"] + row["receives"])
    tier2, reasons2 = classify(row, bal, inj2, "playoff", "standard")
    assert tier2 == "speculative"
    assert any("earliest eligible week is unknown" in r for r in reasons2)


def test_fantasy_ir_trade_failing_mutual_improvement_is_excluded_one_sided():
    from app.analytics.trade_assets import AssetBoard
    from app.analytics.trade_practical import classify, injury_flags

    st = injured_league(on_ir=True)
    board = AssetBoard(st, st.window())
    # In regular season (weeks 10-12), player 299 is ineligible, so Team 1 delta is negative
    ts = TradeSearch(st, 1, ("1-for-1",), opponents=[2], positions=None)
    cand = next(c for c in ts.cands if 299 in c[3])
    row = ts.evaluate(cand)
    assert row["mine"]["regular_season_delta"] < 0
    bal = board.package(row["sends"], row["receives"])
    inj = injury_flags(st, row["sends"] + row["receives"])
    tier, reasons = classify(row, bal, inj, "regular", "standard")
    # Must be excluded_one_sided, NOT speculative
    assert tier == "excluded_one_sided"
    assert "your lineup does not improve in this phase" in reasons


def test_fantasy_ir_trade_with_lopsided_package_is_excluded_lopsided():
    from app.analytics.trade_assets import AssetBoard
    from app.analytics.trade_practical import classify, injury_flags

    st = injured_league(on_ir=True)
    board = AssetBoard(st, st.window())
    # Fabricate a balance that is significantly against them
    ts = TradeSearch(st, 1, ("1-for-1",), opponents=[2], positions=None)
    cand = next(c for c in ts.cands if 299 in c[3])
    row = ts.evaluate(cand)
    fake_bal = {"label": "significant_they_give_more", "text": "Significant asset imbalance: they surrender far more"}
    inj = injury_flags(st, row["sends"] + row["receives"])
    tier, reasons = classify(row, fake_bal, inj, "playoff", "standard")
    assert tier == "excluded_lopsided"
    assert "Significant asset imbalance" in reasons[0]


def test_tied_par_at_starter_cutoff_both_qualify_as_starter_caliber():
    from app.analytics.trade_assets import AssetBoard

    # 2 teams with slots ['QB', 'RB', 'RB', 'WR', 'WR', 'TE', 'K', 'D/ST', 'FLEX']
    # 2 RB fixed slots * 2 teams = 4 league starters at RB
    st = synthetic_league(n_teams=2, weeks=range(10, 14))
    st.free_agents = []
    st.rosters[1] = [P(101, "RB"), P(102, "RB"), P(103, "RB"), P(104, "RB"), P(105, "RB")]
    st.rosters[2] = [P(201, "WR")]
    # Player 104 and 105 have identical projections (thus identical PAR)
    for pid, score in [(101, 20.0), (102, 15.0), (103, 12.0), (104, 10.0), (105, 10.0)]:
        st.projections[pid] = {w: score for w in range(10, 14)}

    board = AssetBoard(st, st.window())
    pr104 = board.profiles[104]
    pr105 = board.profiles[105]

    # Both players must receive the exact same position_rank and starter_caliber status
    assert pr104["par"] == pr105["par"]
    assert pr104["position_rank"] == 4
    assert pr105["position_rank"] == 4
    assert pr104["starter_caliber"] is True
    assert pr105["starter_caliber"] is True
    # In total, 5 players qualify as starter-caliber at RB even though nominal starter count is 4
    rb_starters = [pr for pr in board.profiles.values() if pr["position"] == "RB" and pr["starter_caliber"]]
    assert len(rb_starters) == 5


def test_starter_caliber_independent_of_espn_player_ids_and_reordering():
    from app.analytics.trade_assets import AssetBoard

    # League A: original IDs
    st_a = synthetic_league(n_teams=2, weeks=range(10, 14))
    st_a.free_agents = [P(901, "RB")]
    st_a.projections[901] = {w: 4.0 for w in range(10, 14)}
    st_a.rosters[1] = [P(101, "RB"), P(102, "RB"), P(103, "RB"), P(104, "RB"), P(105, "RB")]
    st_a.rosters[2] = [P(201, "WR"), P(202, "WR")]
    for pid, score in [(101, 20.0), (102, 15.0), (103, 12.0), (104, 10.0), (105, 10.0), (201, 5.0), (202, 5.0)]:
        st_a.projections[pid] = {w: score for w in range(10, 14)}

    # League B: swap the IDs of the tied players (104 <-> 999)
    st_b = synthetic_league(n_teams=2, weeks=range(10, 14))
    st_b.free_agents = [P(901, "RB")]
    st_b.projections[901] = {w: 4.0 for w in range(10, 14)}
    st_b.rosters[1] = [P(101, "RB"), P(102, "RB"), P(103, "RB"), P(999, "RB"), P(105, "RB")]
    st_b.rosters[2] = [P(201, "WR"), P(202, "WR")]
    for pid, score in [(101, 20.0), (102, 15.0), (103, 12.0), (999, 10.0), (105, 10.0), (201, 5.0), (202, 5.0)]:
        st_b.projections[pid] = {w: score for w in range(10, 14)}

    board_a = AssetBoard(st_a, st_a.window())
    board_b = AssetBoard(st_b, st_b.window())

    assert board_a.profiles[104]["position_rank"] == board_b.profiles[999]["position_rank"]
    assert board_a.profiles[104]["starter_caliber"] == board_b.profiles[999]["starter_caliber"]
    assert board_a.profiles[105]["position_rank"] == board_b.profiles[105]["position_rank"]
    assert board_a.profiles[105]["starter_caliber"] == board_b.profiles[105]["starter_caliber"]

    # Package label with tied players must also be identical
    pkg_a = board_a.package([102], [104, 105])
    pkg_b = board_b.package([102], [999, 105])
    assert pkg_a["label"] == pkg_b["label"]
    assert pkg_a["they_send_starter_caliber"] == pkg_b["they_send_starter_caliber"] == 2
    assert pkg_a["label"] == "they_surrender_multiple_strong"


def test_tied_group_spanning_cutoff_and_entirely_above_or_below():
    from app.analytics.trade_assets import AssetBoard

    # 2 teams, 1 TE fixed slot * 2 teams = 2 TE league starters
    st = synthetic_league(n_teams=2, weeks=range(10, 14))
    st.free_agents = []

    # Scenario A: Tie spans cutoff (Player 1 has rank 1, Players 2 and 3 tied at rank 2 <= 2)
    st.rosters[1] = [P(301, "TE"), P(302, "TE"), P(303, "TE")]
    st.rosters[2] = [P(401, "WR")]
    for pid, score in [(301, 15.0), (302, 10.0), (303, 10.0), (401, 5.0)]:
        st.projections[pid] = {w: score for w in range(10, 14)}
    b_a = AssetBoard(st, st.window())
    assert b_a.profiles[301]["position_rank"] == 1 and b_a.profiles[301]["starter_caliber"] is True
    assert b_a.profiles[302]["position_rank"] == 2 and b_a.profiles[302]["starter_caliber"] is True
    assert b_a.profiles[303]["position_rank"] == 2 and b_a.profiles[303]["starter_caliber"] is True

    # Scenario B: Tie spanning multiple cutoff positions (all 3 tied at rank 1 <= 2)
    for pid in (301, 302, 303):
        st.projections[pid] = {w: 12.0 for w in range(10, 14)}
    b_b = AssetBoard(st, st.window())
    assert all(b_b.profiles[pid]["position_rank"] == 1 for pid in (301, 302, 303))
    assert all(b_b.profiles[pid]["starter_caliber"] is True for pid in (301, 302, 303))

    # Scenario C: Tied group entirely above cutoff (Cutoff = 2 TE starters. Both tied at rank 1 <= 2)
    for pid, score in [(301, 15.0), (302, 15.0), (303, 8.0)]:
        st.projections[pid] = {w: score for w in range(10, 14)}
    b_c = AssetBoard(st, st.window())
    assert b_c.profiles[301]["position_rank"] == 1 and b_c.profiles[301]["starter_caliber"] is True
    assert b_c.profiles[302]["position_rank"] == 1 and b_c.profiles[302]["starter_caliber"] is True
    assert b_c.profiles[303]["position_rank"] == 3 and b_c.profiles[303]["starter_caliber"] is False

    # Scenario D: Tied group entirely below cutoff (Cutoff = 2 TE starters. Tied at rank 3 > 2)
    st.rosters[1] = [P(301, "TE"), P(302, "TE"), P(303, "TE"), P(304, "TE")]
    for pid, score in [(301, 18.0), (302, 14.0), (303, 8.0), (304, 8.0)]:
        st.projections[pid] = {w: score for w in range(10, 14)}
    b_d = AssetBoard(st, st.window())
    assert b_d.profiles[301]["position_rank"] == 1 and b_d.profiles[301]["starter_caliber"] is True
    assert b_d.profiles[302]["position_rank"] == 2 and b_d.profiles[302]["starter_caliber"] is True
    assert b_d.profiles[303]["position_rank"] == 3 and b_d.profiles[303]["starter_caliber"] is False
    assert b_d.profiles[304]["position_rank"] == 3 and b_d.profiles[304]["starter_caliber"] is False


def test_par_difference_small_nonzero_not_tied():
    from app.analytics.trade_assets import AssetBoard

    st = synthetic_league(n_teams=2, weeks=range(10, 14))
    st.free_agents = []
    # 2 TE starters across league
    st.rosters[1] = [P(301, "TE"), P(302, "TE"), P(303, "TE")]
    st.rosters[2] = [P(401, "WR")]
    # Player 302 has tiny advantage over 303: 10.0001 vs 10.0 (full precision difference)
    st.projections[301] = {w: 15.0 for w in range(10, 14)}
    st.projections[302] = {w: 10.0001 for w in range(10, 14)}
    st.projections[303] = {w: 10.0 for w in range(10, 14)}
    st.projections[401] = {w: 5.0 for w in range(10, 14)}

    board = AssetBoard(st, st.window())
    assert board.profiles[302]["position_rank"] == 2
    assert board.profiles[302]["starter_caliber"] is True
    assert board.profiles[303]["position_rank"] == 3
    assert board.profiles[303]["starter_caliber"] is False


def test_incomplete_projections_par_unknown_and_insufficient_data():
    from app.analytics.trade_assets import AssetBoard

    st = synthetic_league(n_teams=2, weeks=range(10, 14))
    st.free_agents = []
    st.rosters[1] = [P(301, "TE")]
    st.rosters[2] = [P(401, "WR")]
    # Player 301 is missing week 12 projection
    st.projections[301] = {10: 15.0, 11: 15.0, 13: 15.0}
    st.projections[401] = {w: 10.0 for w in range(10, 14)}

    board = AssetBoard(st, st.window())
    p301 = board.profiles[301]
    assert p301["par"] is None
    assert p301["position_rank"] is None
    assert p301["starter_caliber"] is False

    pkg = board.package([301], [401])
    assert pkg["label"] == "insufficient_data"


def test_package_labels_remain_consistent_with_tied_players():
    from app.analytics.trade_assets import AssetBoard

    st = synthetic_league(n_teams=2, weeks=range(10, 14))
    st.free_agents = [P(901, "RB")]
    st.projections[901] = {w: 5.0 for w in range(10, 14)}
    st.rosters[1] = [P(101, "RB"), P(102, "RB")]
    st.rosters[2] = [P(201, "RB"), P(202, "RB"), P(203, "RB")]
    # Equal valued RBs
    for pid in (101, 102, 201, 202, 203):
        st.projections[pid] = {w: 15.0 for w in range(10, 14)}

    board = AssetBoard(st, st.window())
    # 1-for-1 between any two tied players is balanced
    pkg1 = board.package([101], [201])
    pkg2 = board.package([101], [202])
    assert pkg1["label"] == pkg2["label"] == "balanced"
    assert pkg1["ratio"] == pkg2["ratio"] == 1.0

    # Replacement level: projections lower than or equal to replacement baseline
    st_low = synthetic_league(n_teams=2, weeks=range(10, 14))
    st_low.free_agents = []
    st_low.rosters[1] = [P(101, "RB")]
    st_low.rosters[2] = [P(201, "RB")]
    st_low.projections[101] = {w: 0.5 for w in range(10, 14)}
    st_low.projections[201] = {w: 0.5 for w in range(10, 14)}
    board_low = AssetBoard(st_low, st_low.window())
    pkg_low = board_low.package([101], [201])
    assert pkg_low["label"] == "replacement_level"

    # Significant imbalance
    st_imb = synthetic_league(n_teams=2, weeks=range(10, 14))
    st_imb.free_agents = []
    st_imb.rosters[1] = [P(101, "RB")]
    st_imb.rosters[2] = [P(201, "RB")]
    st_imb.projections[101] = {w: 2.0 for w in range(10, 14)}
    st_imb.projections[201] = {w: 20.0 for w in range(10, 14)}
    board_imb = AssetBoard(st_imb, st_imb.window())
    pkg_imb = board_imb.package([101], [201])
    assert pkg_imb["label"] == "significant_they_give_more"


