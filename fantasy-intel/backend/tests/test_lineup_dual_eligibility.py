"""Players the provider lists at two positions (issue #253 G11, PR 2 of 3: the solver).

lineup.assign_lineup tries each position of such a player (his primary first) under the one-position solver's rules and
keeps the best total, so a bench WR also listed at RB starts at RB when that scores more, never otherwise. Unknown or
malformed eligibility is the primary position only. Every engine that solves lineups reaches the same answer: start/sit,
team lineups (trades, add/drop, team value), the what-if, the trade engine's totals-only solver, waiver slot groups and
bye-week coverage. The rows: tests/compat_fixtures.py `dual_row` (team 1's bench WR, also listed at RB, projects 11.3:
between the weakest starting RB and the weakest starting WR every week).
"""

import random
import types
from datetime import datetime, timezone

import duckdb
import pytest

import compat_fixtures as cf
from app.analytics import lineup
from app.analytics.bye_planner import match_slots
from app.analytics.lineup import (MAX_DUAL_CHOICES, LineupTooLargeError, assign_lineup, eligible, lineup_slots,
                                  lineup_total, parse_eligible_slots, positions_of)
from app.analytics.lineup_totals import LineupTotals
from app.analytics.lineup_what_if import INELIGIBLE, apply_move, build_board
from app.analytics.start_sit import load_start_sit_evaluation
from app.analytics.team_lineups import known_total, project_team
from app.analytics.trade_data import load_league_state
from app.analytics.waiver_roster import competing_positions
from app.db.connection import init_db

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)      # before any synthetic kickoff
RB_WR = [2, 4, 20, 21]                                     # ESPN eligibleSlots: RB and WR, bench, IR
IDS = {"QB": 0, "RB": 2, "WR": 4, "TE": 6, "K": 17, "D/ST": 16}
POS = list(IDS)


def player(pid, pos, value, slots=None, **kw):
    return {"espn_player_id": pid, "position": pos, "value": value, **({"eligible_slots": slots} if slots else {}), **kw}


def starts(assignment) -> dict:
    return {a["player_id"]: a["slot"] for a in assignment if a["player_id"] is not None}


def brute(players, slots) -> float:
    """The best total over every legal lineup (a slot stays empty only when no eligible player is left)."""
    best = None
    pool = [p for p in players if p.get("available", True)]

    def rec(i, used, total):
        nonlocal best
        if i == len(slots):
            best = total if best is None or total > best else best
            return
        options = [p for p in pool if p["espn_player_id"] not in used and eligible(slots[i], p)]
        if not options:
            rec(i + 1, used, total)
        for p in options:
            rec(i + 1, used | {p["espn_player_id"]}, total + (p.get("value") or 0.0))
    rec(0, frozenset(), 0.0)
    return best


def random_pool(rng, dual_share=0.25, low=0.0, size=(1, 9), max_dual=None):
    out, duals = [], 0
    for pid in range(1, rng.randint(*size) + 1):
        pos = rng.choice(POS)
        extra = [IDS[rng.choice(["QB", "RB", "WR", "TE"])]] if rng.random() < dual_share else []
        if extra and max_dual is not None and duals >= max_dual:
            extra = []
        duals += bool(extra)
        out.append(player(pid, pos, rng.choice([0.0, 2.0, 5.0, round(rng.uniform(low, 20), 2)]),
                          [IDS[pos], *extra, 20] if extra else None))
    return out


CONFIGS = [{"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "D/ST": 1}, {"RB": 1, "WR": 1, "TE": 1},
           {"QB": 1, "RB": 1, "WR": 2, "OP": 1}, {"RB": 2, "WR": 1, "RB/WR": 1, "WR/TE": 1},
           {"QB": 2, "RB": 1, "WR": 1, "TE": 1, "FLEX": 1}]


# ---- the solver ---------------------------------------------------------------------------------------------------------

def test_a_player_listed_at_two_positions_starts_where_he_scores_and_keeps_his_primary_position():
    slots = lineup_slots({"RB": 1, "WR": 1})
    pool = [player(1, "RB", 10.0, RB_WR), player(2, "RB", 9.0), player(3, "WR", 1.0)]
    out = assign_lineup(pool, slots)
    assert starts(out) == {2: "RB", 1: "WR"} and lineup_total(out) == 19.0
    assert next(a for a in out if a["player_id"] == 1)["position"] == "RB"        # position stays single (principle 2)
    # the JSON text DuckDB returns for the column reads the same
    assert starts(assign_lineup([{**pool[0], "eligible_slots": "[2, 4, 20, 21]"}, *pool[1:]], slots)) == {2: "RB", 1: "WR"}


@pytest.mark.parametrize("evidence", [None, [2, 20, 21], [2, 3, 5, 23, 7, 20, 21], "[2, 4", '{"4": 1}', [4.0, "4", True]])
def test_without_provider_evidence_a_player_is_primary_only(evidence):
    slots = lineup_slots({"RB": 1, "WR": 1})
    out = assign_lineup([player(1, "RB", 10.0, evidence), player(2, "RB", 9.0), player(3, "WR", 1.0)], slots)
    assert starts(out) == {1: "RB", 3: "WR"}                                        # flex/bench ids name slots only


def test_in_superflex_a_quarterback_also_listed_at_te_frees_the_op_slot():
    slots = lineup_slots({"QB": 1, "TE": 1, "OP": 1})
    pool = [player(1, "QB", 20.0), player(2, "QB", 18.0, [0, 6, 20]), player(3, "QB", 15.0), player(4, "TE", 5.0)]
    out = assign_lineup(pool, slots)
    assert starts(out) == {1: "QB", 2: "TE", 3: "OP"} and lineup_total(out) == 53.0
    assert lineup_total(assign_lineup([{k: v for k, v in p.items() if k != "eligible_slots"} for p in pool], slots)) == 43.0


def test_a_tie_keeps_the_everyone_primary_lineup():
    slots = lineup_slots({"RB": 1, "WR": 1})
    out = assign_lineup([player(1, "RB", 10.0, RB_WR), player(2, "RB", 1.0), player(3, "WR", 1.0)], slots)
    assert starts(out) == {1: "RB", 3: "WR"}                       # RB at WR would total the same 11.0: no move


def test_the_answer_is_the_exhaustive_optimum_without_negative_values():
    rng = random.Random(253)
    for _ in range(2000):
        slots = lineup_slots(rng.choice(CONFIGS))
        pool = random_pool(rng)
        try:
            out = assign_lineup(pool, slots)
        except LineupTooLargeError:
            continue
        ids = [a["player_id"] for a in out if a["player_id"] is not None]
        assert len(ids) == len(set(ids))
        by = {p["espn_player_id"]: p for p in pool}
        assert all(eligible(a["slot"], by[a["player_id"]]) for a in out if a["player_id"] is not None)
        assert lineup_total(out) == pytest.approx(brute(pool, slots), abs=1e-9), (slots, pool, out)


def test_with_negative_values_every_start_is_legal_and_never_below_the_primary_only_lineup(monkeypatch):
    rng = random.Random(11)
    for _ in range(800):
        slots = lineup_slots(rng.choice(CONFIGS))
        pool = random_pool(rng, low=-3.0)
        try:
            out = assign_lineup(pool, slots)
        except LineupTooLargeError:
            continue
        by = {p["espn_player_id"]: p for p in pool}
        assert all(eligible(a["slot"], by[a["player_id"]]) for a in out if a["player_id"] is not None)
        primary = assign_lineup([{k: v for k, v in p.items() if k != "eligible_slots"} for p in pool], slots)
        assert lineup_total(out) >= lineup_total(primary) - 1e-9


def test_a_pool_without_dual_players_gets_exactly_the_one_position_solver(monkeypatch):
    rng = random.Random(5)
    cases = [(lineup_slots(rng.choice(CONFIGS)), random_pool(rng, dual_share=0.0, low=-2.0)) for _ in range(500)]
    got = [assign_lineup(pool, slots) for slots, pool in cases]
    monkeypatch.setattr(lineup, "DUAL_ELIGIBILITY", False)
    assert got == [assign_lineup(pool, slots) for slots, pool in cases]


def test_turning_dual_eligibility_off_reads_every_player_at_his_primary_position(monkeypatch):
    monkeypatch.setattr(lineup, "DUAL_ELIGIBILITY", False)
    rng = random.Random(9)
    for _ in range(300):
        slots = lineup_slots(rng.choice(CONFIGS))
        pool = random_pool(rng, dual_share=0.5)
        assert assign_lineup(pool, slots) == assign_lineup(
            [{k: v for k, v in p.items() if k != "eligible_slots"} for p in pool], slots)


def test_more_position_choices_than_the_bound_are_refused_never_guessed():
    slots = lineup_slots({"RB": 2, "WR": 2})
    n = MAX_DUAL_CHOICES.bit_length()                              # 2**n combinations > MAX_DUAL_CHOICES
    pool = [player(i, "RB", 10.0 - i, RB_WR) for i in range(1, n + 1)]
    with pytest.raises(LineupTooLargeError):
        assign_lineup(pool, slots)
    assign_lineup(pool[:-1], slots)                                # 2**(n-1) = MAX_DUAL_CHOICES: solved


# ---- the trade engine's totals-only solver ------------------------------------------------------------------------------

def test_the_totals_only_solver_matches_assign_lineup_bit_for_bit_with_dual_players():
    rng = random.Random(1)
    weeks = (1, 2, 3)
    for cfg in [cf.FX17, cf.STANDARD, cf.NO_FLEX]:
        slots = lineup_slots(cfg)
        for _ in range(150):
            pool = random_pool(rng, dual_share=0.2, size=(1, 24), max_dual=4)
            pos = {p["espn_player_id"]: p["position"] for p in pool}
            elig = {p["espn_player_id"]: parse_eligible_slots(p.get("eligible_slots")) for p in pool}
            proj = {pid: {w: rng.choice([0.0, 3.0, round(rng.uniform(0, 25), 2)]) for w in weeks} for pid in pos}
            state = types.SimpleNamespace(slots=slots, projections=proj)
            T = LineupTotals(state, weeks, set(pos), pos, {p: "X" for p in pos}, eligibility=elig)
            dual = any(lineup.dual_positions({"position": pos[p], "eligible_slots": e}, slots) for p, e in elig.items())
            assert T.general == dual or T.general
            ids = sorted(pos)
            for i, w in enumerate(weeks):
                ref = lineup_total(assign_lineup([{"espn_player_id": p, "position": pos[p], "value": proj[p][w],
                                                   "eligible_slots": elig[p]} for p in ids], slots))
                assert T.week(ids, i) == ref


# ---- waivers: slot groups ---------------------------------------------------------------------------------------------

def test_a_dual_player_joins_the_slot_groups_of_his_positions():
    slots = lineup_slots(cf.NO_FLEX)
    assert competing_positions(slots, {"WR"}) == {"WR"}                                   # no flex: WR stands alone
    roster = [player(1, "WR", 5.0, [4, 2, 20])]
    assert competing_positions(slots, {"WR"}, roster) == {"WR", "RB"}
    assert competing_positions(slots, positions_of(roster[0])) == {"WR", "RB"}


# ---- bye-week coverage ------------------------------------------------------------------------------------------------

def test_coverage_counts_a_dual_player_at_his_second_position():
    slots = ["RB", "WR"]
    assert match_slots(slots, [(1, "WR", True), (2, "WR", True)]) == [None, 1]           # primary only: RB uncovered
    assert match_slots(slots, [(1, "WR", True, (4, 2, 20)), (2, "WR", True)]) == [1, 2]


# ---- engines on an imported league ------------------------------------------------------------------------------------

@pytest.fixture
def league(tmp_path):
    opened = []

    def make(row):
        con = duckdb.connect(str(tmp_path / f"{len(opened)}.duckdb"))
        init_db(con)
        cf.import_row(con, row)
        opened.append(con)
        return con
    yield make
    for c in opened:
        c.close()


DUAL_ROWS = [cf.dual_row("G11a", "espn", cf.NO_FLEX)]
# FX-17 (OP + three FLEX): the FLEX slots already take a WR, so the listing changes no lineup here; every engine still
# takes the dual path for team 1 and must agree with the evaluator.
FX17_DUAL = cf.dual_row("G11f", "espn", cf.FX17, teams=6)


def _dual_pid(row) -> int:
    return row.dual[0][0]


@pytest.mark.parametrize("row", DUAL_ROWS, ids=lambda r: r.id)
def test_the_provider_listing_is_stored_and_read_as_provider_evidence(league, row):
    con = league(row)
    stored = con.execute("SELECT position, eligible_slots FROM fantasy_players WHERE espn_player_id = ?",
                         [_dual_pid(row)]).fetchone()
    assert stored[0] == "WR" and positions_of({"position": stored[0], "eligible_slots": stored[1]}) == {"WR", "RB"}
    others = con.execute("SELECT position, eligible_slots FROM fantasy_players WHERE espn_player_id <> ?",
                         [_dual_pid(row)]).fetchall()
    assert all(positions_of({"position": p, "eligible_slots": e}) == ({p} if p else set()) for p, e in others)


def test_a_sleeper_player_listed_at_two_fantasy_positions_keeps_both(league):
    row = cf.dual_row("G11s", "sleeper", cf.FX17, teams=6)
    con = league(row)
    pos, slots = con.execute("SELECT position, eligible_slots FROM fantasy_players WHERE espn_player_id = ?",
                             [_dual_pid(row)]).fetchone()
    assert positions_of({"position": pos, "eligible_slots": slots}) == {"WR", "RB"}
    state = load_league_state(con, now=NOW)
    mine = next(p for p in state.rosters[1] if p.espn_player_id == _dual_pid(row))
    assert positions_of(mine) == {"WR", "RB"}


@pytest.mark.parametrize("row", DUAL_ROWS, ids=lambda r: r.id)
def test_start_sit_recommends_starting_him_at_rb(league, row):
    con = league(row)
    out = load_start_sit_evaluation(con, team_id=1, now=NOW)
    rec = {r["player_id"]: r for r in out["recommended_lineup"]}
    assert rec[_dual_pid(row)]["slot"] == "RB" and rec[_dual_pid(row)]["position"] == "WR"
    actual = {r["player_id"] for r in out["actual_lineup"] if r.get("player_id") is not None}
    assert _dual_pid(row) not in actual                                     # on the bench: Start/Sit says start him
    assert any(_dual_pid(row) in {p["id"] for p in d["start_players"]} for d in out["decisions"])


@pytest.mark.parametrize("row", DUAL_ROWS, ids=lambda r: r.id)
def test_team_lineups_start_him_every_week_and_gain_exactly_his_edge(league, row, monkeypatch):
    con = league(row)
    state = load_league_state(con, now=NOW)
    players, window = state.rosters[1], state.window()
    proj = project_team(players, state.slots, window, state.projections)
    for week in proj["weeks"]:
        assert starts(week["assignment"]).get(_dual_pid(row)) == "RB", week["week"]
    monkeypatch.setattr(lineup, "DUAL_ELIGIBILITY", False)
    before = project_team(players, state.slots, window, state.projections)
    for wk, old in zip(proj["weeks"], before["weeks"]):
        assert _dual_pid(row) not in starts(old["assignment"])
        weakest_rb = min(a["points"] for a in old["assignment"] if a["slot"] == "RB")
        assert wk["known_lineup_points"] == pytest.approx(old["known_lineup_points"] + 11.3 - weakest_rb, abs=0.011)


@pytest.mark.parametrize("row", [*DUAL_ROWS, FX17_DUAL], ids=lambda r: r.id)
def test_the_trade_engine_totals_equal_the_evaluators(league, row):
    con = league(row)
    state = load_league_state(con, now=NOW)
    window = state.window()
    for team, players in state.rosters.items():
        active = [p for p in players if not p.reserved]
        T = LineupTotals(state, window.weeks, {p.espn_player_id for p in players},
                         {p.espn_player_id: p.position for p in players}, {p.espn_player_id: p.pro_team for p in players},
                         eligibility={p.espn_player_id: p.eligible_slots for p in players})
        assert T.general is (team == 1)                       # only team 1 has a dual player: others keep the fast fill
        ref = project_team(players, state.slots, window, state.projections)
        assert sum(T.vec([p.espn_player_id for p in active])) == pytest.approx(known_total(ref), abs=1e-9)


@pytest.mark.parametrize("row", DUAL_ROWS, ids=lambda r: r.id)
def test_the_what_if_lets_him_into_an_rb_slot_and_refuses_a_te_slot(league, row, monkeypatch):
    con = league(row)
    cap: dict = {}
    load_start_sit_evaluation(con, team_id=1, now=NOW, capture=cap)
    board = build_board(cap)
    pid = _dual_pid(row)
    home = next(sid for sid, x in board.baseline.items() if x == pid)          # recommended at an RB slot
    rb = next(s.slot_id for s in board.slots if s.slot_type == "RB" and s.slot_id != home)
    te = next(s.slot_id for s in board.slots if s.slot_type == "TE")
    assert board.slot(home).slot_type == "RB"
    state, problem = apply_move(board, dict(board.baseline), rb, pid)       # the other RB slot: a legal swap
    assert problem is None and state[rb] == pid and state[home] == board.baseline[rb]
    _, problem = apply_move(board, dict(board.baseline), te, pid)
    assert problem["code"] == INELIGIBLE
    monkeypatch.setattr(lineup, "DUAL_ELIGIBILITY", False)
    _, problem = apply_move(board, dict(board.baseline), rb, pid)
    assert problem["code"] == INELIGIBLE                      # primary only, as before G11


def test_the_trade_method_says_deltas_follow_the_second_position_only_where_a_player_has_one(league):
    from app.analytics.trades import DUAL_NOTE, assumptions
    assert DUAL_NOTE in assumptions(load_league_state(league(DUAL_ROWS[0]), now=NOW))
    assert DUAL_NOTE not in assumptions(load_league_state(league(cf.MATRIX["M1"]), now=NOW))


def test_the_stash_bound_never_cuts_a_free_agent_who_gains_only_through_his_second_position():
    from app.analytics import stash_planner as sp
    from app.analytics.team_lineups import RosterPlayer, Window
    slots = lineup_slots(cf.NO_FLEX)
    win = Window((1, 2, 3, 4, 5, 6, 7), 5)
    roster = [RosterPlayer(pid, pos, pro_team="KC", name=f"P{pid}") for pid, pos in
              ((1, "QB"), (2, "RB"), (3, "RB"), (4, "RB"), (5, "WR"), (6, "WR"), (7, "WR"), (8, "TE"), (9, "K"), (10, "D/ST"))]
    values = {1: 20.0, 2: 15.0, 3: 12.0, 4: 9.0, 5: 14.0, 6: 13.0, 7: 3.0, 8: 8.0, 9: 7.0, 10: 6.0, 40: 12.5}
    pr = {pid: {w: v for w in win.weeks} for pid, v in values.items()}
    for listed, gains in ((None, False), ((4, 2), True)):         # WR only: 12.5 < every WR starter; RB too: > RB 12.0
        fa = RosterPlayer(40, "WR", pro_team="KC", name="P40", eligible_slots=listed)
        res = sp.plan_stashes(roster, slots, win, pr, free_agents=[fa], free_agent_status={40: "FREEAGENT"},
                              active_capacity=len(roster), horizon=2, projection_source="ESPN")
        found = [x["espn_player_id"] for x in res["pick_up_now"] + res["stashes"]]
        assert (found == [40]) is gains, res["counts"]
