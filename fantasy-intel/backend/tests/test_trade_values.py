"""Trade values board (issue #184, GET /api/trades/values, contract trade-values/v1). Synthetic inputs only: the E2E
fixture league (scripts/build_e2e_db.py) copied per test, with invented projection snapshots added where a test needs
one. Nothing is written to a provider.

What is pinned here:
  * every value is the trade analyzer's own asset value (trade_assets.AssetBoard over the analyzer's state and window),
    labelled a Fantasy Intel model estimate, with its calculation, version and projection source;
  * missing is never 0: a missing week makes the value unavailable, a missing prior snapshot makes the change
    unavailable;
  * week-over-week change comes only from a stored snapshot made for an earlier week, with its two measured parts.
"""
from __future__ import annotations

import shutil

import duckdb
import pytest
from fastapi.testclient import TestClient

from golden.golden import E2E_SEASON, build_e2e_db
from test_independent_projections import live  # noqa: F401  (fixture used by `stored`)
from test_projection_source import stored  # noqa: F401  (fixture)

EARLIER_DAY = "2026-09-02"            # synced before week 2's first kickoff (2026-09-02 17:00 UTC): a week-2 forecast
SAME_WEEK_DAY = "2026-10-01"          # after week 2's kickoff, before week 3's: a forecast for the window's own week


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return build_e2e_db(tmp_path_factory.mktemp("trade_values") / "e2e.duckdb")


@pytest.fixture
def db_path(built, tmp_path, monkeypatch):
    path = tmp_path / "league.duckdb"
    shutil.copy(built, path)
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    monkeypatch.setenv("ESPN_SEASON", E2E_SEASON)
    for name in ("FANTASY_API_KEY", "MULTILEAGUE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    return path


def get(path: str = "/api/trades/values") -> dict:
    from app.main import app
    r = TestClient(app).get(path)
    assert r.status_code == 200, r.text
    return r.json()


def by_id(d: dict) -> dict[int, dict]:
    return {p["espn_player_id"]: p for p in d["players"]}


def add_snapshot(path, day: str, synced: str, *, shift: dict[int, float] | None = None, skip: tuple[int, ...] = ()):
    """A stored ESPN snapshot copied from the current one: `shift` adds points to a player every week, `skip` leaves a
    player out (as a snapshot that never projected him)."""
    con = duckdb.connect(str(path))
    try:
        rows = con.execute("SELECT season, espn_player_id, scoring_period, projected_points FROM espn_projection_history")\
            .fetchall()
        con.executemany("INSERT INTO espn_projection_history VALUES (?, ?, ?, ?, ?, ?)",
                        [(day, s, pid, w, v + (shift or {}).get(pid, 0.0), synced) for s, pid, w, v in rows
                         if pid not in skip])
    finally:
        con.close()


def picked(d: dict, position: str, valued: bool = True) -> dict:
    """A deterministic row at a position with a positive value."""
    return next(p for p in sorted(d["players"], key=lambda x: x["espn_player_id"])
                if p["position"] == position and (p["value"] or 0) > 0) if valued else \
        next(p for p in d["players"] if p["position"] == position)


# ---------------------------------------------------------------------------------------------------- the value --

def test_every_value_is_the_trade_analyzers_asset_value_labelled_as_a_model_estimate(db_path):
    from app.analytics.trade_assets import AssetBoard
    from app.analytics.trade_data import load_league_state
    d = get()
    assert d["contract"] == "trade-values/v1" and d["label"] == "Fantasy Intel model estimate"
    assert "not a trade price" in d["note"]
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        state = load_league_state(con)
    finally:
        con.close()
    window = state.window()
    board = AssetBoard(state, window)
    rostered = {p.espn_player_id for ps in state.rosters.values() for p in ps}
    assert set(by_id(d)) == rostered                       # every rostered player, no free agent
    for p in d["players"]:
        if p["no_replacement_weeks"]:
            # the engine stands in 0.0 for an unmeasured replacement; the board never shows a value built on it
            assert p["value"] is None and board.profiles[p["espn_player_id"]]["par"] is not None
            continue
        assert p["value"] == board.profiles[p["espn_player_id"]]["par"]
        # the calculation shown week by week adds up to the value exactly, to the cent
        assert round(sum(x["adds"] for x in p["weekly"] if x["adds"] is not None), 2) == p["value"]
        assert [x["week"] for x in p["weekly"]] == list(window.weeks)
        assert p["projection_versions"] == ["ESPN"]
    b = d["basis"]
    assert b["projection_source"] == "espn" and b["projection_label"] == "ESPN weekly projections"
    assert b["projection_versions"] == ["ESPN"] and "model_version" not in b
    assert b["calculation_version"] == "tv-2" and b["window"]["weeks"] == list(window.weeks)
    assert "comparable only" in b["comparable_within"] and "kdst_streaming" not in b
    assert "streaming lineup baseline is not part of this value" in b["kdst"]
    assert b["window"]["regular_weeks"] + b["window"]["playoff_weeks"] == list(window.weeks)
    text = repr(d).lower()
    for claim in ("trade price:", "acceptance probability", "fantasypros", "expert consensus value"):
        assert claim not in text


def test_the_fixtures_kickers_and_defenses_have_no_measured_replacement_so_no_value(db_path):
    d = get()
    kdst = [p for p in d["players"] if p["position"] in ("K", "D/ST")]
    assert kdst and d["basis"]["no_replacement_positions"] == ["D/ST", "K"]
    for p in kdst:
        assert p["value"] is None and p["rank"] is None and p["position_rank"] is None
        assert any(u["code"] == "no_replacement" for u in p["uncertainty"])
        assert all(w["replacement"] is None and w["adds"] is None for w in p["weekly"] if w["status"] == "no_replacement")


def test_ranks_are_tie_aware_over_known_values_only(db_path):
    d = get()
    known = [p for p in d["players"] if p["value"] is not None]
    ranked = [p for p in known if p["rank"] is not None]
    for p in ranked:
        assert p["rank"] == 1 + sum(1 for o in ranked if o["value"] > p["value"] + 1e-6)
        assert p["rank_of"] == len(ranked)
    for p in known:
        same = [o for o in known if o["position"] == p["position"]]
        assert p["position_rank"] == 1 + sum(1 for o in same if o["value"] > p["value"] + 1e-6)


def test_a_missing_week_makes_the_value_unavailable_never_zero(db_path):
    d0 = get()
    victim = picked(d0, "WR")
    con = duckdb.connect(str(db_path))
    try:
        con.execute("DELETE FROM espn_player_weekly_points WHERE espn_player_id = ? AND scoring_period = ?",
                    [victim["espn_player_id"], d0["basis"]["window"]["start"] + 1])
    finally:
        con.close()
    d = get()
    p = by_id(d)[victim["espn_player_id"]]
    assert p["value"] is None and p["value_per_week"] is None and p["rank"] is None
    assert p["known_value"] is not None and p["known_value"] < victim["value"]
    assert p["missing_weeks"] == [d0["basis"]["window"]["start"] + 1]
    assert p["uncertainty"][0]["code"] == "missing_projection" and "never counted as 0" in p["uncertainty"][0]["text"]
    assert next(x for x in p["weekly"] if x["status"] == "missing")["adds"] is None
    assert d["summary"]["unvalued"] == d0["summary"]["unvalued"] + 1 and d["summary"]["valued"] == d0["summary"]["valued"] - 1
    before = by_id(d0)
    assert all((o["value"] is None) == (before[o["espn_player_id"]]["value"] is None)
               for o in d["players"] if o["espn_player_id"] != victim["espn_player_id"])


def test_rows_name_the_owner_and_my_team(db_path):
    d = get()
    teams = {t["team_id"]: t for t in d["teams"]}
    assert d["my_team_id"] == next(t["team_id"] for t in d["teams"] if t["is_mine"])
    for p in d["players"]:
        assert p["team_name"] == teams[p["team_id"]]["name"]
        assert p["on_my_team"] is (p["team_id"] == d["my_team_id"])


# --------------------------------------------------------------------------------------------------- the change --

def test_without_a_prior_snapshot_the_change_is_unavailable_never_zero(db_path):
    d = get()
    assert d["change"]["available"] is False and d["change"]["reason"]
    assert d["change"]["baseline"] is None
    assert all(p["change"] is None for p in d["players"])
    assert d["summary"]["changed"] == 0


def test_a_snapshot_for_an_earlier_week_gives_the_change_and_why(db_path):
    d0 = get()
    riser, faller = picked(d0, "WR"), picked(d0, "QB")
    add_snapshot(db_path, EARLIER_DAY, f"{EARLIER_DAY} 12:00:00",
                 shift={riser["espn_player_id"]: -2.0, faller["espn_player_id"]: 3.0})
    d = get()
    ch = d["change"]
    assert ch["available"] is True and ch["baseline"]["id"] == EARLIER_DAY and ch["baseline"]["cycle_week"] == 2
    assert ch["weeks"] == d["basis"]["window"]["weeks"]
    assert any("scoring" in n for n in ch["notes"])
    n = len(ch["weeks"])
    rows = by_id(d)
    up, down = rows[riser["espn_player_id"]]["change"], rows[faller["espn_player_id"]]["change"]
    assert up["projection_change"] == pytest.approx(2.0 * n) and up["delta"] > 0
    assert down["projection_change"] == pytest.approx(-3.0 * n) and down["delta"] < 0
    assert up["value_now"] == rows[riser["espn_player_id"]]["value"]
    assert up["delta"] == pytest.approx(up["value_now"] - up["value_before"], abs=0.011)
    # nobody else's projection moved, so their projection part is exactly 0 (a measured zero, not a missing one)
    others = [p["change"] for pid, p in rows.items() if pid not in (riser["espn_player_id"], faller["espn_player_id"])]
    assert all(c["projection_change"] == 0.0 for c in others)
    # no measurable replacement in either snapshot: unknown, never a 0.0 replacement change or a delta
    k = next(p for p in rows.values() if p["position"] == "K")["change"]
    assert k["delta"] is None and k["replacement_change"] is None and k["reason"]
    assert d["summary"]["changed"] == sum(1 for p in rows.values() if p["value"] is not None)


def test_a_snapshot_from_the_same_week_is_not_a_week_over_week_change(db_path):
    add_snapshot(db_path, SAME_WEEK_DAY, f"{SAME_WEEK_DAY} 12:00:00")
    d = get()
    assert d["change"]["available"] is False
    assert d["change"]["baseline"]["cycle_week"] == 3 and "earlier week" in d["change"]["reason"]
    assert all(p["change"] is None for p in d["players"])


def test_a_player_missing_from_the_earlier_snapshot_has_no_change(db_path):
    d0 = get()
    gone = picked(d0, "WR")
    add_snapshot(db_path, EARLIER_DAY, f"{EARLIER_DAY} 12:00:00", skip=(gone["espn_player_id"],))
    d = get()
    c = by_id(d)[gone["espn_player_id"]]["change"]
    assert c["delta"] is None and c["value_before"] is None and c["projection_change"] is None
    assert "earlier snapshot" in c["reason"]


def test_week_lists_are_written_as_ranges():
    from app.analytics.trade_values import _weeks
    assert _weeks([4]) == "week 4"
    assert _weeks(range(3, 18)) == "weeks 3-17"
    assert _weeks([3, 5, 7, 8, 9]) == "weeks 3, 5 and 7-9"


# ------------------------------------------------------------------------------------- synthetic, in memory --

def _board(state, *, teams=((1, "Team 1", True), (2, "Team 2", False))):
    from app.analytics.trade_values import build_trade_values
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE fantasy_teams (league_id INTEGER, season INTEGER, team_id INTEGER, name VARCHAR, "
                "is_mine BOOLEAN)")
    con.executemany("INSERT INTO fantasy_teams VALUES (1, 2026, ?, ?, ?)", list(teams))
    try:
        return build_trade_values(con, {"league_id": 1, "season": 2026, "name": "Synthetic", "current_week": 10}, state)
    finally:
        con.close()


def _small_slots():
    from app.analytics.lineup import lineup_slots
    return lineup_slots({"QB": 1, "RB": 1, "TE": 1, "K": 1})


def test_starter_level_kickers_with_no_projected_free_agent_have_no_value_and_no_rank():
    """F1: two teams start one K each, both rostered Ks are starters and no free-agent K is projected: there is no
    replacement level to measure. The engine keeps its 0.0 stand-in; the board says unknown."""
    from app.analytics.trade_assets import AssetBoard
    from app.analytics.trade_values import NO_REPLACEMENT
    from synthetic_v3 import league
    st = league({1: [(11, "QB", 20), (12, "RB", 12), (13, "TE", 8), (14, "K", 9)],
                 2: [(21, "QB", 18), (22, "RB", 10), (23, "TE", 6), (24, "K", 7)]},
                [(91, "QB", 12), (92, "RB", 5), (93, "TE", 3), (94, "K", None)], slots=_small_slots())
    assert AssetBoard(st, st.window()).profiles[14]["par"] == 36.0          # the analyzer's engine is unchanged
    rows = {p["espn_player_id"]: p for p in _board(st)["players"]}
    for pid in (14, 24):
        k = rows[pid]
        assert k["value"] is None and k["value_per_week"] is None and k["known_value"] == 0.0
        assert k["rank"] is None and k["position_rank"] is None and k["rank_reason"] == "No rank without a value"
        assert k["no_replacement_weeks"] == [10, 11, 12, 13]
        u = next(x for x in k["uncertainty"] if x["code"] == "no_replacement")
        assert u["text"].startswith(NO_REPLACEMENT)
        assert all(w["replacement"] is None and w["adds"] is None and w["status"] == "no_replacement" for w in k["weekly"])
    assert rows[11]["value"] == 32.0 and rows[11]["rank"] == 1               # QB 20 over the FA QB 12, four weeks


def test_league_structure_values_are_ranked_only_within_their_position():
    """F2: TE has no free agent but a third rostered TE, so its replacement is the league-structure level: valued and
    ranked within TE, never in the overall (cross-position) rank."""
    from app.analytics.trade_values import DIFFERENT_BASELINE
    from synthetic_v3 import league
    st = league({1: [(11, "QB", 20), (12, "RB", 12), (13, "TE", 8), (15, "TE", 2), (14, "K", 9)],
                 2: [(21, "QB", 18), (22, "RB", 10), (23, "TE", 6), (24, "K", 7)]},
                [(91, "QB", 12), (92, "RB", 5), (94, "K", 4)], slots=_small_slots())
    d = _board(st)
    rows = {p["espn_player_id"]: p for p in d["players"]}
    te = rows[13]
    assert te["value"] == 24.0 and te["position_rank"] == 1 and te["position_rank_of"] == 3
    assert te["rank"] is None and te["rank_reason"].startswith(DIFFERENT_BASELINE)
    assert any(u["code"] == "replacement_fallback" for u in te["uncertainty"])
    ranked = sorted((p for p in d["players"] if p["rank"] is not None), key=lambda p: p["rank"])
    assert {p["position"] for p in ranked} == {"QB", "RB", "K"}
    assert all(p["rank_of"] == len(ranked) for p in ranked)
    assert d["summary"]["ranked_overall"] == len(ranked)
    assert "each position's replacement" in d["basis"]["cross_position"]


def test_rounded_weekly_additions_sum_exactly_to_the_value():
    """Codex P2: each week's addition is rounded so the weeks add up to the value as shown."""
    from synthetic_v3 import league
    weekly = {10: 10.333, 11: 10.333, 12: 10.333, 13: 10.334}
    st = league({1: [(11, "QB", weekly), (12, "RB", 12), (13, "TE", 8), (14, "K", 9)],
                 2: [(21, "QB", 18), (22, "RB", 10), (23, "TE", 6), (24, "K", 7)]},
                [(91, "QB", 0.001), (92, "RB", 5), (93, "TE", 3), (94, "K", 4)], slots=_small_slots())
    qb = next(p for p in _board(st)["players"] if p["espn_player_id"] == 11)
    adds = [w["adds"] for w in qb["weekly"]]
    assert qb["value"] == 41.33 and round(sum(adds), 2) == qb["value"]
    assert sorted(adds) == [10.33, 10.33, 10.33, 10.34]


def test_week_versions_are_the_ones_served_and_a_different_version_is_never_compared():
    """Codex P1: each week names the origin and version actually served; a change is never computed across them."""
    from app.analytics.trade_assets import AssetBoard
    from app.analytics.trade_values import _calc, _change, _position_ranks
    from synthetic_v3 import league
    st = league({1: [(11, "QB", 20), (12, "RB", 12), (13, "TE", 8), (14, "K", 9)],
                 2: [(21, "QB", 18), (22, "RB", 10), (23, "TE", 6), (24, "K", 7)]},
                [(91, "QB", 12), (92, "RB", 5), (93, "TE", 3), (94, "K", 4)], slots=_small_slots())
    board = AssetBoard(st, st.window())
    p = next(x for x in st.rosters[1] if x.espn_player_id == 11)
    tags = {10: {11: ("ours", "ifp-2.1.0")}, 11: {11: ("ours", "ifp-2.2.0")}, 12: {11: ("espn", "ESPN")},
            13: {11: ("ours", "ifp-2.2.0")}}
    c = _calc(board, p, tags)
    assert [w["version"] for w in c["weekly"]] == ["ifp-2.1.0", "ifp-2.2.0", "ESPN", "ifp-2.2.0"]
    assert c["versions"] == ["ESPN", "ifp-2.1.0", "ifp-2.2.0"]
    before = _calc(board, p)
    ranks = _position_ranks(board, {11: c}, {11: p})
    ch = _change(p, board, board, c, before, ranks, ranks, "ifp-2.2.0")
    assert ch["delta"] is None and ch["value_before"] is None
    assert "weeks 10 and 12" in ch["reason"] and "never compared" in ch["reason"]


def test_league_projections_detail_names_the_run_kicker_model_and_provider_fallback(stored):
    """The served origin and version per player and week (projection_source.league_projections' detail), with the
    values unchanged by asking for it."""
    from app.analytics import projection_source as ps
    from test_projection_source import _kicker_run
    _kicker_run(stored, 5)
    plain = ps.league_projections(stored, 2026, [6], "independent")
    detail: dict = {}
    assert ps.league_projections(stored, 2026, [6], "independent", detail=detail) == plain
    six = detail[6]
    assert six[1] == ("ours", ps.FALLBACK_VERSION)                   # the run served is the previous model's
    assert six[4] == ("ours_zero", ps.FALLBACK_VERSION)
    assert six[5] == ("kicker_model", "kfp-1.1.0")
    assert six[7] == ("espn", "ESPN")                                # the models abstain: the provider's number
    assert set(six) == set(plain)
