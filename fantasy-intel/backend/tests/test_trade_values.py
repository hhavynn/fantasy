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
    from app.analytics.trades import with_streaming
    d = get()
    assert d["contract"] == "trade-values/v1" and d["label"] == "Fantasy Intel model estimate"
    assert "not a trade price" in d["note"]
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        state = with_streaming(load_league_state(con))
    finally:
        con.close()
    window = state.window()
    board = AssetBoard(state, window)
    rostered = {p.espn_player_id for ps in state.rosters.values() for p in ps}
    assert set(by_id(d)) == rostered                       # every rostered player, no free agent
    for p in d["players"]:
        assert p["value"] == board.profiles[p["espn_player_id"]]["par"]
        assert p["position_rank"] == board.profiles[p["espn_player_id"]]["position_rank"]
        # the calculation shown week by week adds up to the value
        assert round(sum(x["adds"] for x in p["weekly"] if x["adds"] is not None), 1) == round(p["value"], 1)
        assert [x["week"] for x in p["weekly"]] == list(window.weeks)
    b = d["basis"]
    assert b["projection_source"] == "espn" and b["projection_label"] == "ESPN weekly projections"
    assert b["calculation_version"] == "tv-1" and b["window"]["weeks"] == list(window.weeks)
    assert "comparable only" in b["comparable_within"] and b["kdst_streaming"] is True
    assert b["window"]["regular_weeks"] + b["window"]["playoff_weeks"] == list(window.weeks)
    text = repr(d).lower()
    for claim in ("trade price:", "acceptance probability", "fantasypros", "expert consensus value"):
        assert claim not in text


def test_ranks_are_tie_aware_over_known_values_only(db_path):
    d = get()
    known = [p for p in d["players"] if p["value"] is not None]
    for p in known:
        assert p["rank"] == 1 + sum(1 for o in known if o["value"] > p["value"] + 1e-6)
        assert p["rank_of"] == len(known)


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
    assert d["summary"]["unvalued"] == 1 and d["summary"]["valued"] == d0["summary"]["valued"] - 1
    assert all(o["value"] is not None for o in d["players"] if o["espn_player_id"] != victim["espn_player_id"])


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
    assert d["summary"]["changed"] == len(rows)


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
