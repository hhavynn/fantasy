"""Trade discovery on deep leagues (FX-17, tests/compat_fixtures.py): the totals-only solver's fast paths are exact
(bit-identical totals, the same forced drops), the larger-shape beam's work budget is deterministic and disclosed, and
a deep league's discovery finishes within a bounded amount of work.

Before these changes one GET /api/trades/discover on FX-17 took ~450 s (~95% in forced-drop resolution on 27-player
rosters); see docs/v2_trade_discovery.md "Deep leagues"."""

import random
import time
import types
from dataclasses import replace

import duckdb
import pytest
from fastapi.testclient import TestClient

import compat_fixtures as cf
from app.analytics import trade_discovery as td
from app.analytics import trades
from app.analytics.lineup import assign_lineup, lineup_slots, lineup_total
from app.analytics.lineup_totals import LineupTotals
from app.analytics.team_lineups import known_total, project_team
from app.db.connection import init_db
from synthetic_league import SMALL_ROSTER, synthetic_league

ALL = ("1-for-1", "2-for-1", "1-for-2", "2-for-2", "3-for-1", "1-for-3", "3-for-2", "2-for-3")
NESTED = [cf.FX17, {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "OP": 1, "K": 1, "D/ST": 1},
          {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "RB/WR": 1, "FLEX": 2, "OP": 1}]
CONFIGS = NESTED + [cf.STANDARD, {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1},
                    {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "RB/WR": 1, "WR/TE": 1}]     # last: assign_lineup path
POSITIONS = ["QB", "RB", "WR", "TE", "K", "D/ST"]
WEEKS = (1, 2, 3, 4)


def random_totals(rng, cfg, negative=False, exact_negative=False, size=(1, 30)):
    slots = lineup_slots(cfg)
    pos = {pid: rng.choice(POSITIONS) for pid in range(1, rng.randint(*size) + 1)}
    # coarse values make exact ties common; some weeks are missing
    proj = {pid: {w: rng.choice([float(rng.randint(0, 9)), 4.0, 0.0, round(rng.uniform(0, 25), 2)])
                  for w in WEEKS if rng.random() > 0.1} for pid in pos}
    if negative:
        proj[1] = {w: -1.5 for w in WEEKS}
    state = types.SimpleNamespace(slots=slots, projections=proj)
    return LineupTotals(state, WEEKS, set(pos), pos, {p: "X" for p in pos}, exact_negative=exact_negative), pos, proj


def reference_total(ids, pos, proj, slots) -> float:
    """What team_lineups.project_week sums: assign_lineup over the players with a value, week by week."""
    return sum(lineup_total(assign_lineup([{"espn_player_id": p, "position": pos[p], "value": proj[p][w]}
                                           for p in ids if w in proj[p]], slots)) for w in WEEKS)


# ---- the solver's fast paths are exact ------------------------------------------------------------------------------

def test_nested_flex_lineups_use_the_fast_fill_and_match_assign_lineup_bit_for_bit():
    rng = random.Random(17)
    for cfg in NESTED:
        for _ in range(600):
            T, pos, proj = random_totals(rng, cfg)
            assert T.nested is not None and not T.general
            ids = [p for p in pos if rng.random() < 0.8]
            assert sum(T.vec(ids)) == reference_total(ids, pos, proj, T.slots)


def test_removal_and_incremental_totals_equal_a_full_solve_bit_for_bit():
    rng = random.Random(23)
    for cfg in CONFIGS:
        for exact_negative in (False, True):
            for trial in range(60):
                negative = exact_negative and trial % 10 == 0     # the evaluator's mode keeps assign_lineup's rule
                T, pos, proj = random_totals(rng, cfg, negative, exact_negative)
                ids = list(pos)
                rt = T.removal_total(ids)
                for _ in range(15):
                    gone = set(rng.sample(ids, rng.randint(1, min(3, len(ids)))))
                    rest = [p for p in ids if p not in gone]
                    got = rt.without(gone)
                    assert got == sum(T.vec(rest)) == rt(rest)
                    if exact_negative or not negative:
                        assert got == reference_total(rest, pos, proj, T.slots)
                if len(ids) < 2:
                    continue
                mine, other = ids[:len(ids) // 2], ids[len(ids) // 2:]
                base = T.base(mine)
                for _ in range(8):
                    out, add = rng.sample(mine, min(2, len(mine))), rng.sample(other, rng.randint(0, min(2, len(other))))
                    new = [p for p in mine if p not in out] + add
                    assert T.vec_change(base, out, add) == T.vec(new)
                    start = T.profile_change(base, out, add)
                    fresh, started = T.removal_total(new), T.removal_total(new, start=start)
                    gone = set(rng.sample(new, min(2, len(new))))
                    assert started.without(gone) == fresh.without(gone)


@pytest.mark.parametrize("flex_values,flex_counted", [((-1.5,), False), ((-0.6, -0.6), False), ((-0.5,), True),
                                                       ((-0.4, -0.4), True)])
def test_discovery_totals_match_the_evaluator_when_only_negative_players_can_fill_flex(flex_values, flex_counted):
    """STATUS 1r: a single-FLEX league whose FLEX can only be filled by negative projections. assign_lineup (the
    evaluator, team_lineups.project_week) leaves FLEX empty when the best fill is worth -1 or less and starts it in
    (-1, 0); discovery's totals (no exact_negative) now agree in both cases, also through removals and edits."""
    cfg = {"QB": 1, "RB": 1, "WR": 1, "TE": 1, "FLEX": len(flex_values), "K": 1}
    slots = lineup_slots(cfg)
    pos = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 6: "QB"}
    proj = {1: {1: 20.0}, 2: {1: 12.0}, 3: {1: 11.0}, 4: {1: 8.0}, 5: {1: 7.0}, 6: {1: 15.0}}
    for i, v in enumerate(flex_values):             # the only flex-eligible players left after the fixed slots
        pos[10 + i], proj[10 + i] = "RB", {1: v}
    state = types.SimpleNamespace(slots=slots, projections=proj)
    weeks = (1,)
    discovery = LineupTotals(state, weeks, set(pos), pos, {p: "X" for p in pos})
    evaluator = LineupTotals(state, weeks, set(pos), pos, {p: "X" for p in pos}, exact_negative=True)
    ids = list(pos)

    def reference(players):
        return sum(lineup_total(assign_lineup([{"espn_player_id": p, "position": pos[p], "value": proj[p][w]}
                                               for p in players if w in proj[p]], slots)) for w in weeks)
    fixed = 20.0 + 12.0 + 11.0 + 8.0 + 7.0
    assert reference(ids) == fixed + (sum(flex_values) if flex_counted else 0.0)     # the -1 rule, pinned
    assert sum(discovery.vec(ids)) == sum(evaluator.vec(ids)) == reference(ids)
    rt = discovery.removal_total(ids)
    for gone in ({6}, {10}, {1, 6}):
        rest = [p for p in ids if p not in gone]
        assert rt.without(gone) == sum(discovery.vec(rest)) == reference(rest)
    base = discovery.base([1, 2, 3, 4, 5])
    assert sum(discovery.vec_change(base, [], [p for p in ids if p >= 10])) == reference(ids[:5] + [p for p in ids if p >= 10])


def test_a_standard_week_with_mild_negatives_keeps_the_fast_fill():
    """The STATUS 1r routing costs nothing where the -1 rule cannot apply: flex-eligible negatives summing above -1, or
    negatives only at non-flex positions (a D/ST), keep the single-flex fill."""
    slots = lineup_slots(cf.STANDARD)
    pos = {1: "RB", 2: "D/ST", 3: "WR"}
    proj = {1: {1: -0.3}, 2: {1: -4.0}, 3: {1: 5.0}}
    T = LineupTotals(types.SimpleNamespace(slots=slots, projections=proj), (1,), set(pos), pos, {p: "X" for p in pos})
    assert T.exact_week == [False]
    proj[1] = {1: -1.2}
    T = LineupTotals(types.SimpleNamespace(slots=slots, projections=proj), (1,), set(pos), pos, {p: "X" for p in pos})
    assert T.exact_week == [True]


def test_the_single_removal_bound_is_the_exact_single_cost():
    """RemovalTotal.single_bound: one solve per week and position (starters at one position are interchangeable in the
    lineup matroid) gives each player's single-removal cost; float rounding only."""
    rng = random.Random(5)
    for cfg in CONFIGS[:5]:
        for _ in range(80):
            T, pos, _ = random_totals(rng, cfg, size=(2, 30))
            rt = T.removal_total(list(pos))
            assert rt.bound is not None
            for p in pos:
                exact = max(0.0, rt.total - rt.without({p}))
                assert abs(rt.single_bound(types.SimpleNamespace(espn_player_id=p, reserved=False)) - exact) < 1e-9


def _drop_cases(seed: int, slots, n: int = 120):
    st = synthetic_league(n_teams=4, weeks=range(10, 18), roster=SMALL_ROSTER, slots=slots, seed=seed)
    st = replace(st, position_limits={"RB": 4, "QB": 2} if seed % 2 else {})
    ts = td.TradeSearch(st, 1, ("1-for-1",), positions=None)
    rng = random.Random(seed)
    for _ in range(n):
        a, b = rng.sample(sorted(st.rosters), 2)
        gets = rng.sample([p.espn_player_id for p in st.rosters[b]], rng.randint(1, 3))
        gives = rng.sample([p.espn_player_id for p in st.rosters[a]], rng.randint(0, 1))
        pre = [p for p in st.rosters[a] if p.espn_player_id not in gives] + \
              [replace(ts.players[x], on_ir=False, on_taxi=False) for x in gets]
        if not trades.legal_roster(pre, st, st.rosters[a]):
            yield st, ts, a, pre, set(gets)


def _choice(res) -> dict:
    return {g: (x[0], [p.espn_player_id for p in x[1]]) for g, x in res["best"].items()}


@pytest.mark.parametrize("cfg", [NESTED[1], cf.STANDARD])
def test_forced_drops_are_the_same_with_the_incremental_hooks(cfg):
    """choose_forced_drops picks the same drops whether it rebuilds every candidate roster with `total` or uses
    RemovalTotal's `total_without` and `single_bound`; only the work differs."""
    n = 0
    for seed in range(4):
        for st, ts, a, pre, incoming in _drop_cases(seed, lineup_slots(cfg)):
            T = ts.T

            def plain(players, T=T):
                return sum(T.vec([p.espn_player_id for p in players if not p.reserved]))
            old = trades.choose_forced_drops(pre, st.rosters[a], st, incoming, plain, T.negative)
            rt = T.removal_total([p.espn_player_id for p in pre if not p.reserved])
            new = trades.choose_forced_drops(pre, st.rosters[a], st, incoming, lambda ps, rt=rt: rt(
                [p.espn_player_id for p in ps if not p.reserved]), T.negative, rt.dropping, rt.bound)
            assert _choice(new) == _choice(old) and new["drops"] == old["drops"]
            assert new["solves"] <= old["solves"]
            n += 1
    assert n > 100


def test_the_solve_cap_refuses_the_same_rosters_with_and_without_the_hooks(monkeypatch):
    """MAX_DROP_SOLVES charges the single-removal costs as len(pool) solves on both paths, so discovery (single_bound)
    never lists a trade whose forced drops the evaluator (exact singles) refuses as EvaluationUnavailable."""
    refused = agreed = 0
    for st, ts, a, pre, incoming in _drop_cases(1, lineup_slots(cf.STANDARD), n=60):
        T = ts.T
        rt = T.removal_total([p.espn_player_id for p in pre if not p.reserved])

        def outcome(hooks, rt=rt, T=T, st=st, a=a, pre=pre, incoming=incoming):
            def total(ps):
                return rt([p.espn_player_id for p in ps if not p.reserved])
            try:
                extra = (rt.dropping, rt.bound) if hooks else ()
                return _choice(trades.choose_forced_drops(pre, st.rosters[a], st, incoming, total, T.negative, *extra))
            except trades.EvaluationUnavailable:
                return "unavailable"
        plain = trades.choose_forced_drops(pre, st.rosters[a], st, incoming,
                                           lambda ps, rt=rt: rt([p.espn_player_id for p in ps if not p.reserved]),
                                           T.negative)
        with monkeypatch.context() as m:
            m.setattr(trades, "MAX_DROP_SOLVES", plain["solves"] - 1)       # the evaluator needs one solve too many
            assert outcome(False) == outcome(True) == "unavailable"
            refused += 1
        with monkeypatch.context() as m:
            m.setattr(trades, "MAX_DROP_SOLVES", plain["solves"])           # just enough for both
            assert outcome(False) == outcome(True) != "unavailable"
            agreed += 1
    assert refused > 20 and agreed > 20


def test_the_evaluator_still_resolves_forced_drops_on_project_team_totals():
    """trades.resolve_roster searches on LineupTotals now; its choice is what the project_team search gave,
    including weeks with a negative projection (assign_lineup's rule, exact_negative)."""
    slots = lineup_slots(NESTED[1])
    for seed in range(3):
        for st, ts, a, pre, incoming in _drop_cases(seed, slots, 40):
            if seed == 2:                      # a negative projection on one roster
                pid = pre[0].espn_player_id
                st = replace(st, projections={**st.projections, pid: {w: -2.0 for w in st.projections.get(pid, {})}})
            window = st.window()
            got = trades.resolve_roster(st, window, st.rosters[a], pre, incoming)
            negative = any(v < 0 for p in pre for w, v in st.projections.get(p.espn_player_id, {}).items()
                           if w in window.weeks)
            want = trades.choose_forced_drops(pre, st.rosters[a], st, incoming,
                                              lambda ps, st=st, w=window: known_total(
                                                  project_team(ps, st.slots, w, st.projections)),
                                              negative)
            chosen = (want["best"].get(False) or want["best"][True])[1]
            assert [d["espn_player_id"] for d in got["forced_drops"]] == [p.espn_player_id for p in chosen]


# ---- the beam's work budget -----------------------------------------------------------------------------------------

def _small_state():
    slots = lineup_slots(NESTED[1])
    return synthetic_league(n_teams=4, weeks=range(10, 18), roster=SMALL_ROSTER, slots=slots, seed=3)


def test_a_beam_below_its_budget_reports_nothing_new():
    b = td.TradeSearch(_small_state(), 1, ALL).beam()
    assert "work_budget" not in b and all("work_budget" not in x for x in b["per_opponent"])
    s = td.TradeSearch(_small_state(), 1, ALL).heuristic_top("my_gain", None, None, 10)["search"]
    assert "work_budget" not in s and "work budget" not in s["note"]


def test_a_beam_cut_short_by_its_budget_says_so_and_is_deterministic(monkeypatch):
    st = _small_state()
    full = td.TradeSearch(st, 1, ALL, beam_budget=None).beam()
    monkeypatch.setattr(td, "BEAM_WORK_PER_OPPONENT", 40)
    ts = td.TradeSearch(st, 1, ALL)
    out = ts.heuristic_top("my_gain", None, None, 50)
    s, b = out["search"], ts.beam()
    wb = s["work_budget"]
    assert s["exhaustive"] is False and wb == b["work_budget"]
    assert wb["per_opponent"] == 40 and wb["opponents_cut"] and wb["packages_evaluated"] < wb["packages_generated"]
    assert f"evaluated {wb['packages_evaluated']:,} of the {wb['packages_generated']:,} packages" in s["note"]
    assert "Unevaluated packages may be better" in s["note"]
    for x in b["per_opponent"]:
        if x["opponent_team_id"] in wb["opponents_cut"]:
            assert x["work_budget"]["packages_not_evaluated"] > 0
            assert x["work_budget"]["packages_generated"] == x["evaluated"] + x["work_budget"]["packages_not_evaluated"]
    assert b["evaluated"] < full["evaluated"]
    keys = {r["key"] for r in b["rows"]}
    assert keys <= {r["key"] for r in full["rows"]}            # every listed row is still exact
    # Cached packages are charged what they cost when first solved: a search whose cache another beam already warmed
    # stops at exactly the same packages.
    warm = td.TradeSearch(st, 1, ALL)
    warm.beam(td.TradeTarget(position="WR"))
    assert [r["key"] for r in warm.beam()["rows"]] == [r["key"] for r in b["rows"]]
    assert warm.beam()["work_budget"] == b["work_budget"]


def test_concurrent_requests_on_a_cached_search_get_the_same_budgeted_beam(monkeypatch):
    """The API serves one cached TradeSearch to concurrent requests (FastAPI runs them in a thread pool). A package's
    cost must be its own work, not the shared counter's growth while it ran (which includes the other requests'
    work), or a beam stops at a point that depends on timing (the class of bug Codex found on #279). Every beam built
    on a shared search while exact searches run beside it equals the same beam built alone."""
    import sys
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(td, "BEAM_WORK_PER_OPPONENT", 40)
    st = _small_state()
    targets = [None, td.TradeTarget(position="WR"), td.TradeTarget(position="RB"), td.TradeTarget(position="TE")]

    def seen(b):
        return [r["key"] for r in b["rows"]], b["evaluated"], b.get("work_budget")
    want = [seen(td.TradeSearch(st, 1, ALL).beam(t)) for t in targets]
    assert all(w[2] for w in want)                       # the budget binds in every one of them
    jobs = [("beam", i) for i in range(len(targets))] + [("top", s) for s in ("my_gain", "combined", "min_side")]
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)                           # switch threads often, so the calls really interleave
    try:
        for _ in range(3):
            shared = td.TradeSearch(st, 1, ALL)

            def run(job):
                kind, x = job
                return seen(shared.beam(targets[x])) if kind == "beam" else shared.top(x, limit=10)
            with ThreadPoolExecutor(8) as pool:
                got = list(pool.map(run, jobs))
            assert got[:len(targets)] == want
    finally:
        sys.setswitchinterval(interval)


# ---- FX-17 end to end -----------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def fx17(tmp_path_factory):
    path = tmp_path_factory.mktemp("fx17") / "fx17.duckdb"
    con = duckdb.connect(str(path))
    init_db(con)
    cf.import_row(con, cf.MATRIX["M2"])
    con.close()
    return path


def _client(monkeypatch, path):
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    for name in ("FANTASY_API_KEY", "MULTILEAGUE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def test_fx17_discovery_finishes_within_a_bounded_amount_of_work_and_discloses_the_budget(fx17, monkeypatch):
    from app.api import trade_discovery as api
    api._CACHE.clear()
    api._VIEWS.clear()
    t0 = time.perf_counter()
    r = _client(monkeypatch, fx17).get("/api/trades/discover/summary?team_id=1")
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200, r.text
    body = r.json()
    he = body["coverage"]["heuristic"]
    assert he["exhaustive"] is False and he["work_budget"]["opponents_cut"]
    assert "work budget" in he["note"] and "Unevaluated packages may be better" in he["note"]
    # the exact search is untouched by the budget: still a proven top list
    assert body["coverage"]["exact"]["exhaustive"] is True
    assert body["search"]["consistency_failures"] == 0
    ts = next(iter(api._CACHE.values()))
    # Deterministic bounds (measured: 107,888 lineup totals in exact sides and forced-drop sets, 491,820 week solves; the
    # first version took ~450 s). The beam spends at most its budget per opponent plus the package that crosses it.
    per_opp: dict[int, int] = {}
    for key, w in ts.pair_work.items():
        per_opp[int(key.split(":")[1])] = per_opp.get(int(key.split(":")[1]), 0) + w
    assert max(per_opp.values()) <= td.BEAM_WORK_PER_OPPONENT + max(ts.pair_work.values())
    assert ts.work < 160_000 and ts.T.solves < 750_000
    assert elapsed < 120             # ~450 s before; ~10-15 s on a 4-core container; generous for CI


def test_fx17_team_value_trade_preview_is_fast(fx17, monkeypatch):
    t0 = time.perf_counter()
    r = _client(monkeypatch, fx17).get("/api/my-team/value?team_id=1")
    assert r.status_code == 200, r.text
    assert r.json()["upgrades"]["trade_preview"] is not None
    assert time.perf_counter() - t0 < 60               # ~19 s before, ~2-3 s now


@pytest.mark.parametrize("mode", ["all", "mutual_upgrades"])
def test_a_row_the_evaluator_abstains_on_is_left_out_and_counted_not_a_failed_response(monkeypatch, mode):
    """If the manual evaluator abstains on one listed trade (EvaluationUnavailable: its forced-drop resolution is over
    a budget), the discovery view leaves that row out and says so (`excluded.evaluation_unavailable`); every other row is
    exactly what it was, and a view with no abstention has no new key."""
    from app.analytics import trade_practical as practical
    st = _small_state()
    before = practical.discover(td.TradeSearch(st, 1, ALL), mode, limit=8)
    assert "evaluation_unavailable" not in before["excluded"]
    shown = [x["key"] for items in before["sections"].values() for x in items]
    assert len(shown) >= 3
    victim, real = shown[1], practical.detail

    def detail(state, row):
        if row["key"] == victim:
            raise trades.EvaluationUnavailable("exact forced-drop resolution needs more than 5,000 lineup solves")
        return real(state, row)
    monkeypatch.setattr(practical, "detail", detail)
    after = practical.discover(td.TradeSearch(st, 1, ALL), mode, limit=8)
    assert after["excluded"]["evaluation_unavailable"] == 1
    kept = [x["key"] for items in after["sections"].values() for x in items]
    assert victim not in kept and set(shown) - {victim} <= set(kept)
    assert all(o["best"] is None or o["best"]["key"] != victim for o in after["opponents"])


CAP = 14        # MAX_DROP_SOLVES below: _small_state's 11-13 player rosters after a trade get 1-3 solves, many need more


def test_a_package_that_abstains_at_the_solve_cap_is_charged_the_work_it_did(monkeypatch):
    """Codex P1 on #277: choose_forced_drops raised EvaluationUnavailable after real work, and the beam charged only the
    side's initial unit, so repeated abstaining packages slipped past the work budget. The exception now carries the
    solves made (`solves`: every solve before the refused one), the side charges them, and the cached cost is the same
    on a second search."""
    st = _small_state()
    monkeypatch.setattr(trades, "MAX_DROP_SOLVES", CAP)
    ts = td.TradeSearch(st, 1, ALL, beam_budget=None)
    ts.beam()
    abstained = [k for k, (_, _, status) in ts.pairs.items() if status == "excluded_evaluation_unavailable"]
    assert len(abstained) >= 5
    side = td.TradeSearch(st, 1, ALL, beam_budget=None)
    for key in abstained:
        opp, sends, receives = int(key.split(":")[1]), *(
            tuple(int(x) for x in part.split("-")) for part in key.split(":")[2:4])
        # the anchor side's own cost when it finishes, else the abstention: one unit plus every solve the cap allowed
        # (the cap charges the single-removal costs as one solve per player of the post-trade roster)
        mine_pool = len(st.rosters[1]) - len(sends) + len(receives)
        try:
            spent: list[int] = []
            side._side(1, sends, receives, spent)
            theirs_pool = len(st.rosters[opp]) - len(receives) + len(sends)
            want = sum(spent) + 1 + CAP - theirs_pool
        except trades.EvaluationUnavailable:
            want = 1 + CAP - mine_pool
        assert ts.pair_work[key] == want > 1, key
    again = td.TradeSearch(st, 1, ALL, beam_budget=None)
    again.beam()
    assert {k: again.pair_work[k] for k in abstained} == {k: ts.pair_work[k] for k in abstained}


def test_abstaining_packages_spend_the_beam_budget(monkeypatch):
    """With the solve cap low, the beam's packages abstain often; each is charged its consumed work, so the per-manager
    budget binds where charging one unit per abstention would not, and a warmed search cuts at the same point."""
    st = _small_state()
    monkeypatch.setattr(trades, "MAX_DROP_SOLVES", CAP)
    free = td.TradeSearch(st, 1, ALL, beam_budget=None)
    full = free.beam()
    abstained = [k for k, (_, _, status) in free.pairs.items() if status == "excluded_evaluation_unavailable"]
    assert abstained and all(free.pair_work[k] > 1 for k in abstained)
    per_opp: dict[int, tuple[int, int]] = {}         # charged work, and the same work with one unit per abstention
    for k, w in free.pair_work.items():
        opp = int(k.split(":")[1])
        cheap = 1 if k in abstained else w
        a, b = per_opp.get(opp, (0, 0))
        per_opp[opp] = (a + w, b + cheap)
    opp, (charged, cheap) = max(per_opp.items(), key=lambda kv: kv[1][0] - kv[1][1])
    assert charged > cheap
    monkeypatch.setattr(td, "BEAM_WORK_PER_OPPONENT", cheap + 1)     # binds only because abstentions are charged
    cut = td.TradeSearch(st, 1, ALL).beam()
    assert opp in cut["work_budget"]["opponents_cut"] and cut["evaluated"] < full["evaluated"]
    warm = td.TradeSearch(st, 1, ALL)
    warm.beam(td.TradeTarget(position="WR"))
    assert warm.beam()["work_budget"] == cut["work_budget"]
    assert [r["key"] for r in warm.beam()["rows"]] == [r["key"] for r in cut["rows"]]
