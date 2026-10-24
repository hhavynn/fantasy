"""Unsupported-slot integrity (issue #253 phase 3, audit findings G2, G3 and G4). Synthetic data only.

  G2  Start/Sit: a starter in a slot no engine fills (IDP, punter, head coach) is held where he is, never counted in
      one total and missing from the other, so the projected gain compares like with like and is exact.
  G3  Every lineup total that leaves such a slot out is partial: never complete, never a full-lineup projection, the
      slots named. A comparison stays exact only when it cannot move a player who could start there.
  G4  Roster legality is `unverified`, never legal, where the roster count is unknown: rostered players missing from
      the player data (Sleeper stores no IDP players, so the active count shows phantom open spots), or no player count
      at all. Where every rostered player is stored (ESPN, P/HC/IDP included) the count is exact and kept; a count that
      is already over capacity stays illegal.

Fixtures (tests/compat_fixtures.py): M8 ESPN with LB and DL, M8p ESPN with P and HC, M8s Sleeper with DL, LB and
IDP_FLEX. All ids, names and scoring are invented.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import duckdb
import pytest

import compat_fixtures as cf
from app.analytics.add_drop import UNSUPPORTED_SLOT_PLAYER, AddDropContext, evaluate_add_drop
from app.analytics.claim_plan import UNVERIFIED as CLAIM_UNVERIFIED, Claim, evaluate_claim_plan
from app.analytics.faab import evaluate_faab
from app.analytics.lineup import LineupSlots, lineup_slots, separable, unsupported_slots
from app.analytics.matchup import load_matchup
from app.analytics.start_sit import load_start_sit_evaluation
from app.analytics.team_lineups import RosterPlayer, Window, compare, project_team, project_week
from app.analytics.trade_data import build_team_lineup_projections, load_league_state
from app.analytics.trades import ROSTER_UNVERIFIED, LeagueState, evaluate_trade, roster_legality
from app.db.connection import init_db

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)      # before any synthetic kickoff


@pytest.fixture
def league(tmp_path):
    opened = []

    def make(row: str):
        con = duckdb.connect(str(tmp_path / f"{row}-{len(opened)}.duckdb"))
        init_db(con)
        cf.import_row(con, cf.MATRIX[row])
        opened.append(con)
        return con
    yield make
    for c in opened:
        c.close()


# ---- the shared mechanism: lineup.lineup_slots ----------------------------------------------------------------------

def test_lineup_slots_name_the_starting_slots_no_engine_fills():
    slots = lineup_slots({"QB": 1, "LB": 2, "HC": 1, "P": 1, "BE": 5, "IR": 1, "TAXI": 2, "": 1, "FLEX": 1})
    assert isinstance(slots, LineupSlots)
    assert slots == ["QB", "FLEX"]                                  # unchanged for every consumer
    assert slots.unsupported == ("LB", "P", "HC")                   # each once; bench, IR, taxi and blank never
    assert separable(slots.unsupported)
    assert unsupported_slots({"QB": 1, "ER": 1, "LB": 0}) == ("ER",) and not separable(("ER",))
    assert lineup_slots(cf.STANDARD).unsupported == ()


# ---- G3: team_lineups totals are partial, comparisons exact only when they can be -----------------------------------

def _p(pid: int, pos: str) -> RosterPlayer:
    return RosterPlayer(pid, pos, "KC", f"P{pid}")


def test_a_week_with_an_unsupported_slot_is_partial_and_names_it():
    slots = lineup_slots({"QB": 1, "WR": 1, "LB": 1})
    proj = {1: {5: 20.0}, 2: {5: 12.0}, 3: {5: 9.0}}
    w = project_week([_p(1, "QB"), _p(2, "WR"), _p(3, "LB")], slots, 5, proj)
    assert w["known_lineup_points"] == 32.0                         # QB + WR; the LB is never a measured 0 or 9
    assert w["projected_lineup_points"] is None and w["projection_complete"] is False
    assert w["unsupported_slots"] == ["LB"] and w["missing_projection_ids"] == []
    assert [a["slot"] for a in w["assignment"]] == ["QB", "WR"]
    team = project_team([_p(1, "QB"), _p(2, "WR")], slots, Window((5,), None), proj)
    s = team["summary"]
    assert s["ros_projected_starting_points"] is None and s["ros_known_starting_points"] == 32.0
    assert s["projection_complete"] is False and s["unsupported_slots"] == ["LB"]
    assert s["missing_weeks"] == []                                # not a missing projection


def test_a_standard_week_keeps_its_exact_shape():
    w = project_week([_p(1, "QB")], lineup_slots({"QB": 1}), 5, {1: {5: 20.0}})
    assert w["projection_complete"] is True and w["projected_lineup_points"] == 20.0
    assert "unsupported_slots" not in w


def test_a_comparison_is_exact_only_when_no_player_who_could_start_there_moves():
    window = Window((5,), None)
    proj = {1: {5: 20.0}, 2: {5: 12.0}, 3: {5: 9.0}, 4: {5: 15.0}}
    lb = lineup_slots({"QB": 1, "WR": 1, "LB": 1})
    base = project_team([_p(1, "QB"), _p(2, "WR"), _p(3, "LB")], lb, window, proj)
    # a WR upgrade leaves the LB where he is: the LB slot adds the same to both lineups, so the delta is exact
    wr = compare(base, project_team([_p(1, "QB"), _p(4, "WR"), _p(3, "LB")], lb, window, proj), window)
    assert wr["ros_delta"] == 3.0 and wr["projection_complete"] and wr["unsupported_slots"] == ["LB"]
    # losing the LB changes the LB slot, which no engine values: unknown, never 0
    gone = compare(base, project_team([_p(1, "QB"), _p(2, "WR")], lb, window, proj), window)
    assert gone["ros_delta"] is None and gone["known_ros_delta"] == 0.0 and gone["weeks_incomplete"] == [5]
    # an unrecognised slot may take any position: no comparison is exact
    er = lineup_slots({"QB": 1, "WR": 1, "ER": 1})
    c = compare(project_team([_p(1, "QB"), _p(2, "WR")], er, window, proj),
                project_team([_p(1, "QB"), _p(4, "WR")], er, window, proj), window)
    assert c["ros_delta"] is None and c["known_ros_delta"] == 3.0


# ---- G2: Start/Sit ---------------------------------------------------------------------------------------------------

def _starters(con, team: int, week: int = cf.CURRENT) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for slot, pid in con.execute("SELECT lineup_slot, espn_player_id FROM lineup_snapshots WHERE team_id = ? AND "
                                 "scoring_period = ? AND is_starter", [team, week]).fetchall():
        out.setdefault(slot, []).append(pid)
    return out


@pytest.mark.parametrize("row, held", [("M8", ("DL", "LB")), ("M8p", ("P", "HC"))])
def test_start_sit_holds_starters_in_unsupported_slots_and_the_gain_is_exact(league, row, held):
    con = league(row)
    proj = {p.pid: cf.projection(cf.MATRIX[row], p.pid, cf.CURRENT) for p in cf.league_players(cf.MATRIX[row])}
    by_slot = _starters(con, 1)
    held_ids = sorted(pid for s in held for pid in by_slot[s])
    out = load_start_sit_evaluation(con, team_id=1, now=NOW)
    s = out["summary"]
    cov = s["lineup_coverage"]
    assert cov["status"] == "partial" and cov["unsupported_slots"] == list(held)
    assert sorted(h["player_id"] for h in cov["held_starters"]) == held_ids
    assert {h["player_id"]: h["projected_points"] for h in cov["held_starters"]} == {i: proj[i] for i in held_ids}
    # G3: no total is complete; the known sums cover the slots the engines fill only
    assert s["actual_projected_total"] is None and s["recommended_projected_total"] is None
    assert s["unconstrained_total"] is None
    supported = [pid for slot, ids in by_slot.items() if slot not in held for pid in ids]
    assert s["known_actual_total"] == round(sum(proj[i] for i in supported), 2)
    rec = out["recommended_lineup"]
    assert s["known_recommended_total"] == round(sum(r["projected_points"] for r in rec), 2)
    assert not {r["player_id"] for r in rec} & set(held_ids)
    assert not {r["player_id"] for r in out["actual_lineup"]} & set(held_ids)
    # G2: the gain compares like with like: never negative, exactly the difference of the two known sums
    assert s["projected_gain"] == round(s["known_recommended_total"] - s["known_actual_total"], 2) == 0.9
    old_rule = s["projected_gain"] - sum(proj[i] for i in held_ids)       # what counting the held starters gave
    assert old_rule < 0
    # F6: the totals are partial, so they are not complete; the gain needs only the projections it compares
    assert s["projection_complete"] is False and s["projections_complete"] is True
    assert s["status"] == "upgrade_available"


def test_start_sit_in_a_sleeper_idp_league_names_the_starters_it_cannot_see(league):
    con = league("M8s")
    cov = load_start_sit_evaluation(con, team_id=1, now=NOW)["summary"]["lineup_coverage"]
    assert cov["unsupported_slots"] == ["DL", "LB", "DP"]
    assert cov["held_starters"] == [] and cov["starters_not_in_player_data"] == 3


def test_all_locked_is_reported_with_held_starters_whatever_the_status(league, monkeypatch):
    """#165: `all_locked` counts every rostered player, held starters included, and is its own flag: the status reports
    incomplete_data or another state first, so the hub reads the flag instead of inferring it from counts."""
    from app.analytics import start_sit as ss
    con = league("M8")
    s = load_start_sit_evaluation(con, team_id=1, now=NOW)["summary"]
    assert s["all_locked"] is False and "lineup_coverage" in s
    monkeypatch.setattr(ss, "detect_player_locks", lambda c, season, week, teams, now: {
        t: ss.PlayerLock(team=t, status=ss.LOCK_LOCKED) for t in teams})
    s = load_start_sit_evaluation(con, team_id=1, now=NOW)["summary"]
    total = s["starters_count"] + s["bench_count"] + s["ir_count"]
    assert s["all_locked"] is True and "lineup_coverage" in s
    assert s["locked_players_count"] > total            # held starters are locked but in no count the hub could add
    # the case the flag exists for: a status other than all_locked (incomplete_data outranks it) with held starters
    sl = league("M8s")
    s = load_start_sit_evaluation(sl, team_id=1, now=NOW)["summary"]
    assert s["status"] == "incomplete_data" and s["all_locked"] is True and "lineup_coverage" in s


def test_a_standard_league_has_no_coverage_block(league):
    s = load_start_sit_evaluation(league("M1"), team_id=1, now=NOW)["summary"]
    assert "lineup_coverage" not in s and s["recommended_projected_total"] is not None


# ---- G3 on the other lineup consumers -------------------------------------------------------------------------------

def test_matchup_totals_are_partial_and_say_why(league):
    con = league("M8")
    build_team_lineup_projections(con, cf.SEASON)
    m = load_matchup(con, team_id=1, week=cf.CURRENT)
    assert m["unsupported_slots"] == ["DL", "LB"]
    me = m["me"]
    assert me["projection_complete"] is False and me["projected_points"] is None
    assert isinstance(me["known_points"], float) and me["known_points"] > 0
    assert any(n.startswith("DL, LB: no engine fills these slots") for n in m["notes"])
    assert not any("projection incomplete" in n for n in m["notes"])      # nothing is missing a projection


def test_trade_totals_are_partial_and_deltas_exact_only_when_they_can_be(league):
    con = league("M8")
    st = load_league_state(con, now=NOW)
    mine = {p.position: p.espn_player_id for p in st.rosters[1] if not p.on_ir}
    theirs = {p.position: p.espn_player_id for p in st.rosters[2] if not p.on_ir}
    t = evaluate_trade(st, 1, 2, [mine["WR"]], [theirs["WR"]], scenarios=False)
    a = t["team_a"]
    assert a["before"]["ros_projected_starting_points"] is None and a["before"]["unsupported_slots"] == ["DL", "LB"]
    assert a["deltas"]["ros_delta"] is not None and a["deltas"]["unsupported_slots"] == ["DL", "LB"]
    assert t["result"]["label"] != "INCOMPLETE_DATA"
    lb = evaluate_trade(st, 1, 2, [mine["LB"]], [theirs["WR"]], scenarios=False)
    assert lb["team_a"]["deltas"]["ros_delta"] is None and lb["result"]["label"] == "INCOMPLETE_DATA"


# ---- G4: roster legality ---------------------------------------------------------------------------------------------

def test_sleeper_idp_players_leave_phantom_spots_so_legality_is_unverified(league):
    con = league("M8s")
    st = load_league_state(con, now=NOW)
    assert st.active_capacity == 17 and st.players_missing == {1: 3, 2: 3, 3: 3, 4: 3}
    assert sum(1 for p in st.rosters[1] if not p.reserved) == 14         # 3 IDP starters invisible: 3 phantom spots
    leg = roster_legality(st, 1)
    assert leg["status"] == "unverified" and leg["code"] == ROSTER_UNVERIFIED
    assert leg["unsupported_slots"] == ["DL", "LB", "DP"] and leg["players_missing"] == 3
    fa = next(p for p in st.free_agents if p.position == "WR")
    ad = evaluate_add_drop(st, 1, fa.espn_player_id, None, AddDropContext(provider="Sleeper"))
    r = ad["roster"]
    assert ad["status"] == "evaluated"
    assert (r["legal"], r["drop_required"], r["open_spots_before"], r["open_spots_after"]) == (None, None, None, None)
    assert r["legality"]["status"] == "unverified"
    note = next(n for n in ad["notes"] if n["code"] == ROSTER_UNVERIFIED)
    assert "3 rostered players are not in the synced player data" in note["message"]
    assert note["message"].endswith("check your open roster spots in Sleeper before acting.")
    t = evaluate_trade(st, 1, 2, [st.rosters[1][0].espn_player_id],
                       [p.espn_player_id for p in st.rosters[2] if not p.reserved][:2], scenarios=False)
    ro = t["team_a"]["roster"]
    assert ro["forced_drops"] == [] and ro["legality"]["players_missing"] == 3
    assert ro["open_active_roster_spots"] is None and ro["open_active_roster_spots_before"] is None


def test_espn_stores_every_rostered_player_so_its_count_stays_exact(league):
    con = league("M8p")
    st = load_league_state(con, now=NOW)
    assert st.players_missing == {1: 0, 2: 0, 3: 0, 4: 0}               # ESPN stores P and HC players
    assert roster_legality(st, 1) is None
    mine = {p.position: p.espn_player_id for p in st.rosters[1] if not p.on_ir}
    fa = next(p for p in st.free_agents if p.position == "WR")
    swap = evaluate_add_drop(st, 1, fa.espn_player_id, mine["TE"], AddDropContext())
    r = swap["roster"]
    assert (r["legal"], r["drop_required"], r["open_spots_before"], r["open_spots_after"]) == (True, False, 0, 0)
    assert "legality" not in r and ROSTER_UNVERIFIED not in {n["code"] for n in swap["notes"]}
    full = evaluate_add_drop(st, 1, fa.espn_player_id, None, AddDropContext())
    assert full["roster"]["legal"] is False and full["status"] == "blocked"      # over capacity stays illegal
    assert [p["code"] for p in full["problems"]] == ["DROP_REQUIRED"]
    # dropping the punter: legal, but what he adds in the P slot is not modelled, and the note says so
    punt = evaluate_add_drop(st, 1, fa.espn_player_id, mine["P"], AddDropContext())
    assert punt["roster"]["legal"] is True
    note = next(n for n in punt["notes"] if n["code"] == UNSUPPORTED_SLOT_PLAYER)
    assert "can start only in P, HC, which no engine fills" in note["message"]
    t = evaluate_trade(st, 1, 2, [mine["WR"]], [p.espn_player_id for p in st.rosters[2]
                                                if p.position == "WR" and not p.reserved][:1], scenarios=False)
    ro = t["team_a"]["roster"]
    assert "legality" not in ro and ro["open_active_roster_spots"] == 0


def test_no_player_count_falls_back_to_unverified():
    """A league that starts slots no engine fills, with no count of the team's rostered players, is never `legal`."""
    state = LeagueState(season=2026, slots=lineup_slots({"QB": 1, "LB": 1, "BE": 2}), final_week=17, last_regular=14,
                        start_week=5, rosters={1: []}, free_agents=[], projections={}, active_capacity=4,
                        players_missing={})                 # loaded, but no lineup rows for team 1
    leg = roster_legality(state, 1)
    assert leg["status"] == "unverified" and leg["players_missing"] == 0 and leg["unsupported_slots"] == ["LB"]
    assert "hold no lineup rows for this team" in leg["reason"]
    assert roster_legality(replace(state, players_missing={1: 0}), 1) is None
    # F4: in every league, not only one with unsupported slots
    assert roster_legality(replace(state, slots=lineup_slots({"QB": 1, "BE": 2})), 1)["status"] == "unverified"
    # N2: the count is required, never silently assumed complete
    with pytest.raises(TypeError):
        LeagueState(season=2026, slots=state.slots, final_week=17, last_regular=14, start_week=5, rosters={1: []},
                    free_agents=[], projections={}, active_capacity=4)


@pytest.mark.parametrize("row, unverified", [("M8s", True), ("M8", False)])
def test_the_claim_planner_and_faab_say_why_a_claim_is_unverified(league, row, unverified):
    con = league(row)
    st = load_league_state(con, now=NOW)
    te = next(p for p in st.rosters[1] if p.position == "TE" and not p.reserved)
    fa = next(p for p in st.free_agents if p.position == "WR")
    ctx = AddDropContext(provider="ESPN", directory={fa.espn_player_id: {"full_name": fa.name},
                                                    te.espn_player_id: {"full_name": te.name}},
                         rules={"is_faab": True, "faab_budget": 100}, team_row={"faab_spent": 0})
    plan = evaluate_claim_plan(st, 1, [Claim(fa.espn_player_id, te.espn_player_id, 5)], ctx)
    c = plan["claims"][0]
    faab = evaluate_faab(plan, provider="espn", provider_label="ESPN", settings={}, team_row=ctx.team_row, teams=[],
                         observed_rows=None)
    if not unverified:                                  # ESPN stores the IDP players: the count is exact
        assert c["roster"]["legal"] is True and ROSTER_UNVERIFIED not in {x["code"] for x in c["reasons"]}
        return
    assert c["validation"] == CLAIM_UNVERIFIED and c["roster"]["legal"] is None
    reason = next(x for x in c["reasons"] if x["code"] == ROSTER_UNVERIFIED)
    assert reason["effect"] == CLAIM_UNVERIFIED and "Roster legality is unverified" in reason["message"]
    assert faab["claims"][0]["validation"] == CLAIM_UNVERIFIED
    assert ROSTER_UNVERIFIED in {n["code"] for n in faab["claims"][0]["notes"]}


def test_a_standard_league_keeps_legal_moves_legal(league):
    con = league("M1")
    st = load_league_state(con, now=NOW)
    assert roster_legality(st, 1) is None
    te = next(p for p in st.rosters[1] if p.position == "TE" and not p.reserved)
    fa = next(p for p in st.free_agents if p.position == "WR")
    ad = evaluate_add_drop(st, 1, fa.espn_player_id, te.espn_player_id, AddDropContext())
    assert ad["roster"]["legal"] is True and "legality" not in ad["roster"]


def test_trade_discovery_never_calls_a_delta_exact_that_compare_cannot(league):
    """An unrecognised slot (ESPN `ER`) may take any position: compare gives no exact delta, and neither does discovery's
    own bit-identical total."""
    from app.analytics.trade_discovery import _Totals
    con = league("M1")
    st = load_league_state(con, now=NOW)
    weeks = st.window().weeks
    roster = [p for p in st.rosters[1] if not p.reserved]
    ids = {p.espn_player_id for p in roster}
    args = (weeks, ids, {p.espn_player_id: p.position for p in roster}, {p.espn_player_id: p.pro_team for p in roster})
    assert all(_Totals(st, *args).complete(ids))
    er = lineup_slots({**cf.STANDARD, "ER": 1})
    assert er.unsupported == ("ER",)
    assert not any(_Totals(replace(st, slots=er), *args).complete(ids))
    lb = lineup_slots({**cf.STANDARD, "LB": 1})                   # separable: discovery trades no LB, totals exact
    assert all(_Totals(replace(st, slots=lb), *args).complete(ids))


# ---- review fixes: forced drops (F2), discovery parity (F1), unverified counts (F3), no count (F4), flags (F6) ---------

ENGINE = ("QB", "RB", "WR", "TE", "K", "D/ST")


@pytest.mark.parametrize("row", ["M8", "M8p"])
def test_a_forced_drop_never_picks_an_unvalued_starter(league, row):
    """2 WR for 1 RB: the team receiving two players must drop one. A player who starts only in an IDP/P/HC slot would
    cost "0.0" known points, so he is never the automatic choice; the chosen drop has a real cost."""
    st = load_league_state(league(row), now=NOW)
    mine = [p.espn_player_id for p in st.rosters[1] if p.position == "WR" and not p.reserved][:2]
    rb = next(p.espn_player_id for p in st.rosters[2] if p.position == "RB" and not p.reserved)
    t = evaluate_trade(st, 1, 2, mine, [rb], scenarios=False)
    ro = t["team_b"]["roster"]
    assert len(ro["forced_drops"]) == 1 and ro["forced_drops"][0]["position"] in ENGINE
    assert "unsupported_slot_player" not in ro["forced_drops"][0] and "forced_drop_unverified" not in ro
    assert isinstance(ro["forced_drop_known_lineup_cost"], float)
    fa = next(p for p in st.free_agents if p.position == "WR")
    sug = evaluate_add_drop(st, 1, fa.espn_player_id, None, AddDropContext())["roster"]["suggested_drop"]
    assert sug["available"] and sug["position"] in ENGINE and "unsupported_slot_player" not in sug


def test_an_unvalued_drop_is_used_only_when_nothing_else_is_legal_and_has_no_cost(league):
    from app.analytics.trades import resolve_roster
    st = load_league_state(league("M8"), now=NOW)
    before = list(st.rosters[1])
    new_lb = RosterPlayer(79_999, "LB", "KC", "FX LB 79999")
    # the league allows one LB: only an LB drop makes the roster legal
    tight = replace(st, position_limits={"LB": 1}, active_capacity=st.active_capacity + 5)
    res = resolve_roster(tight, st.window(), before, before + [new_lb], {new_lb.espn_player_id})
    drop = res["forced_drops"][0]
    assert drop["position"] == "LB" and drop["unsupported_slot_player"] is True
    assert res["lineup_cost"] is None and res["known_lineup_cost"] is None              # never a 0.0
    assert res["unvalued_drop"]["status"] == "unverified" and res["unvalued_drop"]["code"] == UNSUPPORTED_SLOT_PLAYER
    assert "No other legal drop exists" in res["unvalued_drop"]["note"]


@pytest.mark.parametrize("row", ["M8", "M8p"])
def test_discovery_never_drops_an_unvalued_player_and_matches_the_evaluator(league, row):
    from app.analytics.trade_discovery import TradeSearch, detail
    st = load_league_state(league(row), now=NOW)
    pos = {p.espn_player_id: p.position for ps in st.rosters.values() for p in ps}
    ts = TradeSearch(st, 1, ("2-for-1",))
    rows = [r for c in ts.cands if (r := ts.evaluate(c))]
    assert rows
    dropped = [x for r in rows for x in r["forced_drops"]["theirs"] + r["forced_drops"]["mine"]]
    assert dropped and all(pos[x] in ENGINE for x in dropped)
    for r in [r for r in rows if r["forced_drops"]["theirs"]][:3]:
        assert detail(st, r)["consistent_with_manual_evaluator"]


def test_discovery_reports_no_exact_delta_when_an_unvalued_player_leaves(league, monkeypatch):
    """F1: with the unvalued-player guard disabled (the pre-fix forced drop), the opponent's drop of his starting LB
    changes a slot no engine fills: stage B must not call that delta exact, exactly as the evaluator does not."""
    from app.analytics import trades
    from app.analytics.trade_discovery import TradeSearch, detail
    monkeypatch.setattr(trades, "unvalued_ids", lambda players, state: frozenset())
    st = load_league_state(league("M8"), now=NOW)
    pos = {p.espn_player_id: p.position for ps in st.rosters.values() for p in ps}
    ts = TradeSearch(st, 1, ("2-for-1",))
    rows = [r for c in ts.cands if (r := ts.evaluate(c))
            and any(pos[x] not in ENGINE for x in r["forced_drops"]["theirs"])]
    assert len(rows) == 21
    assert all(r["theirs"]["ros_delta"] is None for r in rows)
    for r in rows[:3]:
        d = detail(st, r)
        assert d["consistent_with_manual_evaluator"] and d["theirs_detail"]["deltas"]["ros_delta"] is None


def test_an_unverified_roster_shows_no_count(league):
    st = load_league_state(league("M8s"), now=NOW)
    t = evaluate_trade(st, 1, 2, [st.rosters[1][0].espn_player_id],
                       [p.espn_player_id for p in st.rosters[2] if not p.reserved][:2], scenarios=False)
    ro = t["team_a"]["roster"]
    assert (ro["active_before"], ro["active_after"]) == (None, None)
    iss = ro["issues_before_drops"]
    assert (iss["active_before_drops"], iss["over_capacity_by"]) == (None, None)
    fa = next(p for p in st.free_agents if p.position == "WR")
    r = evaluate_add_drop(st, 1, fa.espn_player_id, None, AddDropContext(provider="Sleeper"))["roster"]
    assert (r["active_before"], r["active_after"], r["open_spots_before"], r["open_spots_after"]) == (None,) * 4
    assert r["issues"]["active_before_drops"] is None and r["issues"]["over_capacity_by"] is None


def test_a_standard_team_with_no_roster_count_is_never_legal(league):
    con = league("M1")
    con.execute("DELETE FROM lineup_snapshots WHERE team_id = 1 AND scoring_period = ?", [cf.CURRENT])
    st = load_league_state(con, now=NOW)
    assert 1 not in st.players_missing and st.players_missing[2] == 0
    fa = next(p for p in st.free_agents if p.position == "WR")
    r = evaluate_add_drop(st, 1, fa.espn_player_id, None, AddDropContext())["roster"]
    assert r["legal"] is None and r["open_spots_after"] is None and r["legality"]["status"] == "unverified"
    assert evaluate_add_drop(st, 2, fa.espn_player_id, None, AddDropContext())["roster"].get("legality") is None


@pytest.mark.parametrize("row, separable_slots", [("M8", True)])
def test_what_if_totals_are_not_complete_when_partial(league, row, separable_slots):
    from app.analytics.lineup_what_if import evaluate_what_if
    out = evaluate_what_if(league(row), team_id=1, now=NOW)
    t = out["totals"]
    assert t["complete"] is False and t["recommended"]["complete"] is False
    assert t["recommended"]["total"] is None and t["recommended"]["projections_complete"] is True
    assert t["delta"] == 0.0 and t["lineup_coverage"]["unsupported_slots"] == ["DL", "LB"]


def test_an_unrecognised_slot_is_not_called_exact_on_start_sit(tmp_path):
    con = duckdb.connect(str(tmp_path / "er.duckdb"))
    init_db(con)
    cf.import_row(con, cf.MATRIX["M8"])
    con.execute("""UPDATE leagues SET roster_slots = '{"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "ER": 1,
                   "K": 1, "D/ST": 1, "BE": 5, "IR": 1}'""")
    reason = load_start_sit_evaluation(con, team_id=1, now=NOW)["summary"]["lineup_coverage"]["reason"]
    assert "not measured against the best full lineup" in reason
    con.close()


# ---- F5: the reason names the slots, not a missing projection ---------------------------------------------------------

def test_power_rankings_team_value_and_playoff_race_name_the_left_out_slots(league):
    from app.analytics.playoff_race import _side as race_side
    from app.analytics.power_rankings import edition
    from app.analytics.team_value import league_ranks
    st = load_league_state(league("M8"), now=NOW)
    teams = sorted(st.rosters)
    ed = edition(st, [], teams, st.team_names, None, detail=False)
    assert ed["unsupported_slots"] == ["DL", "LB"] and ed["comparable_weeks"] == []
    for t in teams:
        assert ed["teams"][t]["unranked_reason"] == "no comparable week: lineup totals leave out DL, LB, slots no " \
                                                    "engine fills"
    window = st.window()
    projected = {t: project_team(st.rosters[t], st.slots, window, st.projections) for t in teams}
    ranks = league_ranks(projected, window, st.team_names)
    assert ranks["excluded_weeks"] and all(w["reason"] == "lineup totals leave out DL, LB, slots no engine fills"
                                           for w in ranks["excluded_weeks"])
    side = race_side(projected[1], [window.weeks[0]], {})
    assert side["projected_points"] is None and side["gap"] == "lineup totals leave out DL, LB, slots no engine fills"


# ---- N1: no phantom open spot in team value or lineup projections ------------------------------------------------------

def test_team_value_and_lineup_projections_show_no_phantom_open_spot(league, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.analytics.team_value import build_team_value, load_team_value_inputs
    con = league("M8s")
    race, extras = load_team_value_inputs(con, now=NOW)
    tv = build_team_value(race, extras, team_id=1, trades=False)
    text = repr(tv)
    assert "free addition into an open spot" not in text.lower()
    assert "Free addition into an open active roster spot" not in text
    found = [v for v in _walk(tv) if isinstance(v, dict) and "open_active_roster_spots" in v and "realistic" in v]
    assert found and all(v["open_active_roster_spots"] is None and v["realistic"] is False
                         and v["legality"]["status"] == "unverified" for v in found)
    # the lineup-projection marginal values (IR activation) get no open-spot count either
    db = tmp_path / "m8s.duckdb"
    c2 = duckdb.connect(str(db))
    init_db(c2)
    cf.import_row(c2, cf.MATRIX["M8s"])
    c2.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db))
    for name in ("FANTASY_API_KEY", "MULTILEAGUE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    from app.main import app
    r = TestClient(app, raise_server_exceptions=False).get("/api/teams/1/lineup-projection")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["legality"]["status"] == "unverified"
    for m in body["marginal_values"]:
        ir = m.get("ir_activation_sensitivity")
        assert ir is None or ir["open_active_roster_spots"] is None


def _walk(x):
    yield x
    if isinstance(x, dict):
        for v in x.values():
            yield from _walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk(v)


def test_a_loaded_league_state_always_counts_every_team(league):
    st = load_league_state(league("M1"), now=NOW)
    assert isinstance(st.players_missing, dict) and set(st.players_missing) == set(st.rosters)
