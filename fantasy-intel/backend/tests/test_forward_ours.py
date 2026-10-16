"""Opt-in "ours" series on the player forward view (analytics/forward_ours.py, GET /players/{id}/forward).

Temp DuckDB only. The stored runs are inserted with the store's schema (one run per target week, as the
rest-of-season cycle writes them); ESPN stays the default and its output must not change."""

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.analytics import projection_source as ps
from app.analytics.forward_ours import LABEL, ours_forward, player_weeks
from app.analytics.projections import forward_weeks, league_window, load_schedule, player_forward, summarize
from app.db.connection import connect, init_db

SEASON, CURRENT, FINAL, LAST_REG = 2026, 6, 17, 14
KC_BYE = 9
CREATED = datetime(2026, 10, 14, 12, 0)
APPROVED = "sha-approved"
PLAYERS = {1: ("Ace WR", "WR", "KC"), 2: ("Kicker K", "K", "KC"), 3: ("Zed RB", "RB", "BUF")}


def ours_value(week: int, pid: int = 1) -> float:
    return round(10.0 + week + pid / 10, 2)


def populate(con, *, run_weeks=range(CURRENT, LAST_REG + 1), horizon_col=True, skip=None):
    """League window 6..17 (regular season ends 14), ESPN projections for every week, a schedule with a
    KC bye in week 9, and one run per week in `run_weeks` (ifp-2.0.0, scoring fingerprint 'fp')."""
    con.execute("""INSERT INTO leagues (league_id, season, name, current_week, final_scoring_period,
                   reg_season_matchups, scoring_items, settings_raw, synced_at)
                   VALUES (1, ?, 'L', ?, ?, ?, '[]', '{}', '2026-10-14 00:00:00')""",
                [SEASON, CURRENT, FINAL, LAST_REG])
    for pid, (name, pos, team) in PLAYERS.items():
        con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team) VALUES (?, ?, ?, ?)",
                    [pid, name, pos, team])
        for w in range(CURRENT, FINAL + 1):
            con.execute("INSERT INTO espn_player_weekly_points VALUES (?, ?, ?, NULL, ?, '2026-10-14 00:00:00')",
                        [SEASON, pid, w, 5.0 + pid])
    con.execute("""CREATE TABLE IF NOT EXISTS nfl_schedules (season INT, week INT, game_type VARCHAR, game_id VARCHAR,
                   home_team VARCHAR, away_team VARCHAR, spread_line DOUBLE, total_line DOUBLE)""")
    for w in range(1, 19):
        home, away = ("KC", "BUF") if w != KC_BYE else ("BUF", "DEN")
        con.execute("INSERT INTO nfl_schedules VALUES (?, ?, 'REG', ?, ?, ?, NULL, NULL)", [SEASON, w, f"g{w}", home, away])
    # the week-6 ifp-2.0.0 artifact is registered, with a data cutoff before week 6: it covers every later week too
    con.execute("INSERT INTO approved_artifacts VALUES (?, ?, ?, ?, ?, NULL, 'learning_cycle', '2026-10-14 00:00:00')",
                [ps.MODEL_VERSION, APPROVED, SEASON, CURRENT, SEASON * 100 + CURRENT - 1])
    if horizon_col:
        con.execute("ALTER TABLE independent_projection_runs ADD COLUMN IF NOT EXISTS forecast_horizon INTEGER")
        con.execute("ALTER TABLE independent_projection_runs ADD COLUMN IF NOT EXISTS projected_from_week INTEGER")
    for w in run_weeks:
        add_run(con, w, skip=(skip or {}).get(w), horizon_col=horizon_col)


def add_run(con, week, *, version=ps.MODEL_VERSION, created=CREATED, skip=None, horizon_col=True, artifact=APPROVED):
    """One run for `week` with a row per player: WR 1 projected (or bye on KC's bye week), K 2 unsupported,
    RB 3 projected. `skip`: player ids left out of the run's rows."""
    rows = []
    for pid, (_, pos, team) in PLAYERS.items():
        if pid in (skip or ()):
            continue
        if pos == "K":
            rows.append((pid, "unsupported_position", None))
        elif team == "KC" and week == KC_BYE:
            rows.append((pid, "bye", None))
        else:
            rows.append((pid, "projected", ours_value(week, pid)))
    run_id = f"run-{version}-{week}-{created:%H%M}-{artifact}"
    horizon = week - CURRENT
    cols = "run_id, season, week, model_version, scoring_fingerprint, model_status, created_at, row_count, artifact_id"
    vals = [run_id, SEASON, week, version, "fp", json.dumps({"WR": {"status": "experimental"}}), created, len(rows), artifact]
    if horizon_col:
        cols += ", forecast_horizon, projected_from_week"
        vals += [horizon, CURRENT]
    con.execute(f"INSERT INTO independent_projection_runs ({cols}) VALUES ({', '.join('?' * len(vals))})", vals)
    for pid, status, exp in rows:
        con.execute("INSERT INTO independent_projection_rows (run_id, season, week, espn_player_id, coverage_status, "
                    "expected_points) VALUES (?, ?, ?, ?, ?, ?)", [run_id, SEASON, week, pid, status, exp])
    return run_id


@pytest.fixture(autouse=True)
def fingerprint(monkeypatch):
    monkeypatch.setattr(ps, "_scoring_fingerprint", lambda con, season: "fp")


def ours_for(con, pid=1, source="independent"):
    return player_forward(con, SEASON, pid, PLAYERS[pid][2], source=source)


def by_week(block):
    return {e["week"]: e for e in block["weeks"]}


# ----------------------------------------------------------------------------- the series


def test_ours_value_per_remaining_regular_season_week(con):
    populate(con)
    out = ours_for(con)
    o = out["ours"]
    assert o["available"] and o["label"] == LABEL and "experimental beyond next week" in o["label"]
    assert o["label"] == "Fantasy Intel projections (ifp-2.0.0); experimental beyond next week"
    assert [e["week"] for e in o["weeks"]] == list(range(CURRENT, LAST_REG + 1))   # regular season only
    w = by_week(o)
    for week in (6, 7, 12, 14):
        assert w[week]["value"] == ours_value(week) and w[week]["origin"] == "ours"
        assert w[week]["model_version"] == ps.MODEL_VERSION and w[week]["run_id"]
        assert w[week]["run_created_at"] == str(CREATED)
        assert w[week]["horizon"] == week - CURRENT and w[week]["projected_from_week"] == CURRENT
    assert w[6]["experimental"] is False and w[7]["experimental"] is True and w[14]["experimental"] is True
    # ESPN's series is untouched next to it
    assert [e["week"] for e in out["weeks"]] == list(range(CURRENT, FINAL + 1))
    assert out["weeks"][0]["espn_projected_points"] == 6.0


def test_bye_week_is_ours_zero_and_the_total_is_complete(con):
    populate(con)
    o = ours_for(con)["ours"]
    bye = by_week(o)[KC_BYE]
    assert bye["origin"] == "ours_zero" and bye["value"] == 0.0 and bye["on_bye"] is True
    assert o["ros_complete"] and o["fallback_weeks"] == [] and o["missing_weeks"] == []
    assert o["ros_projected_points"] == round(sum(ours_value(w) for w in range(CURRENT, LAST_REG + 1) if w != KC_BYE), 2)
    assert (o["ros_weeks_expected"], o["ros_weeks_available"]) == (8, 8)       # the bye is not expected
    assert o["run_created_at"] == str(CREATED)


def test_schedule_bye_without_a_run_row_does_not_block_the_total(con):
    """The bye comes from the NFL schedule; a run that has no row for him that week is not a gap."""
    populate(con, skip={KC_BYE: (1,)})
    o = ours_for(con)["ours"]
    bye = by_week(o)[KC_BYE]
    assert bye["origin"] == "bye" and bye["value"] is None
    assert o["ros_complete"] and o["ros_projected_points"] is not None


def test_a_week_without_a_run_falls_back_to_espn_and_the_total_is_null(con):
    populate(con, run_weeks=[w for w in range(CURRENT, LAST_REG + 1) if w != 11])
    o = ours_for(con)["ours"]
    assert o["available"]
    w11 = by_week(o)[11]
    assert w11["origin"] == "espn" and w11["value"] == 6.0 and w11["run_id"] is None
    assert o["fallback_weeks"] == [11] and o["missing_weeks"] == [11]
    assert o["ros_complete"] is False and o["ros_projected_points"] is None
    assert (o["ros_weeks_expected"], o["ros_weeks_available"]) == (8, 7)


def test_a_player_the_model_does_not_cover_is_espn_in_every_week(con):
    populate(con)
    o = ours_for(con, pid=2)["ours"]            # K: no independent model
    assert {e["origin"] for e in o["weeks"] if e["week"] != KC_BYE} == {"espn"}
    assert o["ros_projected_points"] is None and not o["ros_complete"]
    assert o["fallback_weeks"] == [w for w in range(CURRENT, LAST_REG + 1) if w != KC_BYE]


def test_a_player_missing_from_a_run_is_espn_for_that_week(con):
    populate(con, skip={8: (1,)})
    o = ours_for(con)["ours"]
    assert by_week(o)[8]["origin"] == "espn" and o["ros_projected_points"] is None


def test_previous_model_weeks_are_labeled_and_never_counted_in_the_ifp2_total(con):
    populate(con, run_weeks=[])
    for w in range(CURRENT, LAST_REG + 1):
        add_run(con, w, version=ps.FALLBACK_VERSION)
    o = ours_for(con)["ours"]
    assert o["available"] and ps.FALLBACK_VERSION in o["reason"]
    assert {e["model_version"] for e in o["weeks"]} == {ps.FALLBACK_VERSION}
    assert o["model_versions"] == [ps.FALLBACK_VERSION] and o["mixed_models"] is False
    assert "ifp-1.0.0" in o["label"] and "ifp-2.0.0" not in o["label"]
    assert o["ros_projected_points"] is None and o["ros_complete"] is False       # not an ifp-2.0.0 total
    assert o["other_model_weeks"] == list(range(CURRENT, LAST_REG + 1))
    assert by_week(o)[7]["value"] == ours_value(7)                                # the weekly numbers still show


def test_mixed_model_weeks_are_labeled_and_hide_the_total(con):
    populate(con, run_weeks=[w for w in range(CURRENT, LAST_REG + 1) if w != CURRENT])
    add_run(con, CURRENT, version=ps.FALLBACK_VERSION)         # the current week only has the previous model
    o = ours_for(con)["ours"]
    w = by_week(o)
    assert w[6]["model_version"] == ps.FALLBACK_VERSION and w[7]["model_version"] == ps.MODEL_VERSION
    assert o["model_versions"] == [ps.FALLBACK_VERSION, ps.MODEL_VERSION] and o["mixed_models"] is True
    assert "ifp-1.0.0" in o["label"] and "ifp-2.0.0" in o["label"]
    assert o["model_version"] == ps.MODEL_VERSION               # the production model, for the per-week badge
    assert o["ros_projected_points"] is None and o["ros_complete"] is False
    assert o["other_model_weeks"] == [6] and o["missing_weeks"] == [6]
    assert "Mixed models" in o["reason"] and "week 6" in o["reason"]
    # once the production run exists for the week, the label and the total are ifp-2.0.0 again
    add_run(con, CURRENT, created=CREATED + timedelta(hours=1))
    o = ours_for(con)["ours"]
    assert o["model_versions"] == [ps.MODEL_VERSION] and not o["mixed_models"] and o["ros_complete"]
    assert o["label"] == LABEL and o["reason"] is None


def test_newest_run_of_a_week_wins(con):
    populate(con)
    add_run(con, 8, created=CREATED + timedelta(hours=1))   # a later cycle re-ran week 8
    con.execute("UPDATE independent_projection_rows SET expected_points = 99.0 WHERE week = 8 AND espn_player_id = 1 "
                "AND run_id LIKE '%1300%'")
    o = ours_for(con)["ours"]
    assert by_week(o)[8]["value"] == 99.0 and o["run_created_at"] == str(CREATED + timedelta(hours=1))


def test_runs_without_horizon_columns_still_work(con):
    populate(con, horizon_col=False)
    w = by_week(ours_for(con)["ours"])
    assert w[7]["horizon"] == 0 and w[7]["projected_from_week"] is None         # NULL horizon = 0 (contract)
    assert w[6]["experimental"] is False and w[7]["experimental"] is False


def test_no_future_runs_means_unavailable_with_a_reason(con):
    populate(con, run_weeks=[])
    out = ours_for(con)
    o = out["ours"]
    assert o["available"] is False and "No ifp-2.0.0 projection run" in o["reason"]
    assert o["weeks"] == [] and o["ros_projected_points"] is None
    assert out["summary"]["ros_projected_points"] is not None      # ESPN is still shown in full


# ----------------------------------------------------------------------------- batching


class Counting:
    """Counts execute() calls on a connection."""
    def __init__(self, con):
        self.con, self.n = con, 0

    def execute(self, *a, **k):
        self.n += 1
        return self.con.execute(*a, **k)

    def __getattr__(self, name):
        return getattr(self.con, name)


def check_equal_to_the_switch(con, pids=(1, 2, 3)):
    """The batched per-player weeks equal projection_source.weekly() for each week (value, origin, run, reason)."""
    weeks = list(range(CURRENT, LAST_REG + 1))
    counting = Counting(con)
    got = {pid: player_weeks(counting, SEASON, pid, weeks) for pid in pids}
    assert counting.n <= len(pids) * 15, counting.n
    for week in weeks:
        ref = ps.weekly(con, SEASON, week, "independent")
        for pid in pids:
            g = got[pid][week]
            assert g["origin"] == ref["origin"].get(pid, ps.ESPN) and g["value"] == ref["values"].get(pid), (pid, week)
            assert (g["run"] or {}).get("run_id") == (ref["run"] or {}).get("run_id"), (pid, week)
            assert g["reason"] == (ref["reason"] if ref["available"] else None), (pid, week, g["reason"], ref["reason"])
            if g["run"]:
                verified = {"verified": True, "unverified": False}.get(g["run"]["artifact_check"])
                assert ref["run"].get("artifact_verified") == verified, (pid, week)
    return got


def test_queries_are_bounded_and_equal_the_per_week_switch(con):
    populate(con, run_weeks=[w for w in range(CURRENT, LAST_REG + 1) if w != 11])   # week 11: ESPN fallback
    add_run(con, 8, version=ps.FALLBACK_VERSION, created=CREATED + timedelta(hours=2))   # older-model, newer run
    add_run(con, 10, created=CREATED + timedelta(hours=1))                                # a re-run wins
    # a newest run whose rows were lost is skipped, like production_forecasts.latest_run
    lost = add_run(con, 12, created=CREATED + timedelta(hours=3))
    con.execute("DELETE FROM independent_projection_rows WHERE run_id = ?", [lost])
    got = check_equal_to_the_switch(con)
    assert got[1][12]["run"]["artifact_check"] == "verified"        # a week-12 run served by the week-6 artifact
    one = Counting(con)
    ours_forward(one, SEASON, 1, espn_baseline(con)["weeks"], source="independent")
    assert one.n < 15, one.n                      # was ~12 per week before batching


def test_unregistered_ifp2_artifact_falls_back_like_the_switch(con):
    populate(con, run_weeks=[])
    for w in range(CURRENT, LAST_REG + 1):
        add_run(con, w, version=ps.FALLBACK_VERSION)
    add_run(con, 7, artifact="rogue", created=CREATED + timedelta(hours=1))          # ifp-2.0.0 but not registered
    add_run(con, 8, artifact=APPROVED, created=CREATED + timedelta(hours=1))          # registered
    got = check_equal_to_the_switch(con)
    assert got[1][7]["run"]["model_version"] == ps.FALLBACK_VERSION and "not approved" in got[1][7]["reason"]
    assert got[1][8]["run"]["model_version"] == ps.MODEL_VERSION and got[1][8]["reason"] is None
    o = ours_for(con)["ours"]
    assert by_week(o)[7]["model_version"] == ps.FALLBACK_VERSION and by_week(o)[8]["artifact_verified"] is True
    assert "not approved" in o["reason"] and o["ros_projected_points"] is None


def test_only_unregistered_runs_means_unavailable_and_says_not_approved(con):
    populate(con, run_weeks=[])
    for w in range(CURRENT, LAST_REG + 1):
        add_run(con, w, artifact="rogue")
    check_equal_to_the_switch(con)
    o = ours_for(con)["ours"]
    assert o["available"] is False and "not approved" in o["reason"]


def test_artifact_registered_for_a_later_cutoff_does_not_cover_an_earlier_week(con):
    """The cutoff rule: a run's week must lie strictly after the artifact's training cutoff."""
    populate(con, run_weeks=[w for w in range(CURRENT, LAST_REG + 1) if w != 8])
    con.execute("INSERT INTO approved_artifacts VALUES (?, 'late', ?, 10, ?, NULL, 'learning_cycle', NULL)",
                [ps.MODEL_VERSION, SEASON, SEASON * 100 + 9])
    add_run(con, 8, artifact="late")         # week 8 <= cutoff week 9: not covered
    add_run(con, 12, artifact="late", created=CREATED + timedelta(hours=1))      # week 12 > 9: covered
    got = check_equal_to_the_switch(con)
    assert got[1][8]["run"] is None and got[1][12]["run"]["artifact_id"] == "late"


def test_a_database_with_no_registered_artifacts_fails_open_and_says_unverified(con):
    populate(con)
    con.execute("DELETE FROM approved_artifacts")
    got = check_equal_to_the_switch(con)
    assert got[1][7]["run"]["artifact_check"] == "unverified"
    o = ours_for(con)["ours"]
    assert o["available"] and all(e["artifact_verified"] is False for e in o["weeks"])
    assert "could not be verified" in o["reason"] and o["reason"].count("could not be verified") == 1   # deduped
    assert o["ros_complete"] and o["ros_projected_points"] is not None            # still served (fail open)


def test_a_missing_approved_artifacts_table_is_read_unverified(con):
    populate(con)
    con.execute("DROP TABLE approved_artifacts")
    got = check_equal_to_the_switch(con)
    assert got[1][7]["run"]["artifact_check"] == "unverified"


# ----------------------------------------------------------------------------- ESPN stays the default


def espn_baseline(con, pid=1):
    """The response as it was before the ours block existed, rebuilt from its parts."""
    from app.analytics.projections import BASELINE_NOTE, PROJECTION_SOURCE, _projections
    window = league_window(con, SEASON)
    p = _projections(con, window, pid).get(pid, {"weeks": {}, "synced_at": None})
    games, covered = load_schedule(con, SEASON, window.weeks)
    weeks = forward_weeks(window, PLAYERS[pid][2], p["weeks"], games, covered)
    return {"source": PROJECTION_SOURCE, "note": BASELINE_NOTE, "summary": summarize(window, weeks),
            "projection_synced_at": p["synced_at"], "weeks": weeks, "schedule_available": bool(covered)}


def test_espn_output_is_identical_with_source_espn_absent_or_runs_stored(con):
    populate(con)
    base = json.dumps(espn_baseline(con), default=str, sort_keys=True)
    for out in (player_forward(con, SEASON, 1, "KC"), ours_for(con, source="espn"), ours_for(con, source="ESPN")):
        assert "ours" not in out and json.dumps(out, default=str, sort_keys=True) == base
    with_ours = ours_for(con)
    assert {k: v for k, v in with_ours.items() if k != "ours"} == espn_baseline(con)    # only `ours` is added


def test_ours_forward_computes_nothing_unless_independent(con):
    populate(con)
    assert ours_forward(con, SEASON, 1, espn_baseline(con)["weeks"], source="espn") is None
    with pytest.raises(ValueError):
        ours_forward(con, SEASON, 1, [], source="vegas")


# ----------------------------------------------------------------------------- the route


@pytest.fixture
def api(tmp_path, monkeypatch):
    path = tmp_path / "api.duckdb"
    c = connect(path)
    init_db(c)
    populate(c)
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    return TestClient(app)


def test_forward_route_defaults_to_espn_and_validates_source(api):
    default = api.get("/api/players/1/forward")
    assert default.status_code == 200 and "ours" not in default.json()
    assert api.get("/api/players/1/forward", params={"source": "espn"}).json() == default.json()
    mine = api.get("/api/players/1/forward", params={"source": "independent"}).json()
    assert mine["ours"]["available"] and mine["ours"]["ros_complete"]
    assert {k: v for k, v in mine.items() if k != "ours"} == default.json()
    assert api.get("/api/players/1/forward", params={"source": "vegas"}).status_code == 422
    assert api.get("/api/players/999/forward", params={"source": "independent"}).status_code == 404
    # the player detail page's embedded forward view stays ESPN
    assert "ours" not in api.get("/api/players/1").json()["forward_projection"]
