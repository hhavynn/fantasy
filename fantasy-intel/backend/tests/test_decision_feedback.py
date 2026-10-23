"""Decision feedback beside the weekly recap (issue #194, app/analytics/decision_feedback.py).

Hand-computed grade math on a tiny synthetic roster; as-of-time enforcement (a forecast or roster archived at or after
its cutoff is never used); missing outcomes excluded, never zero; opportunity-cost bounds; the no-archive week; the
Ours source (sealed runs only, created before kickoff); the read-only legacy route (own team only, any team_id is the
indistinguishable 404, no other team's roster in the answer); and the /v2 mount (authorization, own team only, no
cached answer shared with another perspective). Synthetic inputs only."""

import json
from datetime import UTC, date, datetime, timedelta, timezone

import duckdb
import pytest
from fastapi.testclient import TestClient

from app.analytics import decision_feedback as dfb
from app.analytics.decision_feedback import (
    FeedbackData,
    build_feedback,
    forecast,
    roster_snapshot,
    week_feedback,
)
from app.db.connection import connect, init_db
from test_leagues_api import env  # noqa: F401  (fixture)

SLOTS = {"QB": 1, "RB": 1, "WR": 1, "FLEX": 1, "BE": 4, "IR": 1}


def thu(w):            # AAA plays Thursday night (the week's first kickoff)
    return datetime(2026, 9, 4, 0, 20, tzinfo=UTC) + timedelta(days=7 * (w - 1))


def sun(w):
    return datetime(2026, 9, 6, 17, 0, tzinfo=UTC) + timedelta(days=7 * (w - 1))


def mon(w):            # DDD plays Monday night (the week's last kickoff)
    return datetime(2026, 9, 8, 0, 15, tzinfo=UTC) + timedelta(days=7 * (w - 1))


def kickoffs(weeks=range(1, 5)):
    out = {}
    for w in weeks:
        out.update({(w, "AAA"): thu(w), (w, "BBB"): sun(w), (w, "CCC"): sun(w), (w, "DDD"): mon(w)})
    return out


PLAYERS = {1: ("Ann QB", "QB", "AAA"), 2: ("Bea QB", "QB", "BBB"), 10: ("Cal RB", "RB", "BBB"),
           11: ("Dee RB", "RB", "CCC"), 12: ("Eli RB", "RB", "AAA"), 20: ("Fay WR", "WR", "CCC"),
           21: ("Gus WR", "WR", "BBB"), 30: ("Hal TE", "TE", "CCC"), 40: ("Ivy RB", "RB", "EEE"),
           50: ("Jo RB", "RB", "BBB"), 60: ("Kit K", "K", "BBB")}
ROSTER = [1, 2, 10, 11, 12, 20, 21, 30, 40, 50, 60]
PRE = datetime(2026, 9, 10, 12, 0)          # week 2: after week 1's Monday kickoff, before week 2's Thursday one
# week-2 ESPN snapshots (naive UTC, as stored): every player at PRE; Dee again after Thursday (still before his Sunday
# game: the latest pregame one counts); Eli after his own Thursday kickoff (never counts).
FORECAST = {1: 20.0, 2: 15.0, 10: 12.0, 11: 10.0, 12: 8.0, 20: 11.0, 21: 9.0, 30: 6.0, 50: 30.0, 60: 9.0}
LATE = {11: (datetime(2026, 9, 12, 12, 0), 14.0), 12: (datetime(2026, 9, 12, 12, 0), 99.0)}
ACTUAL = {1: 18.0, 2: 25.0, 10: 7.0, 11: 20.0, 12: None, 20: 15.0, 21: 12.0, 30: 4.0, 60: 7.0}
MINE = [(2, "QB", True), (11, "RB", True), (21, "WR", True), (10, "FLEX", True), (1, "BE", False), (20, "BE", False)]


def history(week=2, values=FORECAST, at=PRE, late=LATE):
    out = {}
    for pid, v in values.items():
        out.setdefault((week, pid), []).append({"value": v, "synced_at": at, "snapshot_date": str(at.date())})
    for pid, (t, v) in late.items():
        out.setdefault((week, pid), []).append({"value": v, "synced_at": t, "snapshot_date": str(t.date())})
    return out


def data(**over) -> FeedbackData:
    games = [{"matchup_id": p, "matchup_period": p, "home_team_id": 1, "away_team_id": 2, "home_points": 100.0,
              "away_points": 90.0, "winner": "HOME", "playoff_tier": "NONE"} for p in (1, 2, 3)]
    games.append({**games[0], "matchup_id": 4, "matchup_period": 4, "winner": "UNDECIDED"})
    kw = dict(league={"league_id": 7, "season": 2026, "name": "Hand"}, source="espn", default_source="espn",
              schedule_settings={"matchupPeriodLength": 1}, roster_slots=SLOTS, team={"team_id": 2, "name": "Mine"},
              matchups=games, week=2, kickoffs=kickoffs(),
              players={p: {"full_name": n, "position": pos, "pro_team": t} for p, (n, pos, t) in PLAYERS.items()},
              rosters=[{"snapshot_date": date(2026, 9, 10), "espn_player_id": p, "lineup_slot_id": 21 if p == 50 else 20,
                        "synced_at": PRE} for p in ROSTER],
              espn_history=history(), outcomes={(2, p): v for p, v in ACTUAL.items()},
              lineups={2: [{"espn_player_id": p, "lineup_slot": s, "is_starter": st} for p, s, st in MINE]})
    kw.update(over)
    return FeedbackData(**kw)


def by_slot(week):
    return {c["slot"]: c for c in week["calls"]}


# ------------------------------------------------------------------------------------------ as-of-time inputs


def test_only_forecasts_archived_before_the_players_own_kickoff_are_used():
    d = data()
    assert forecast(d, 2, 12)[0]["value"] == 8.0           # the 99.0 snapshot came after his Thursday kickoff
    assert forecast(d, 2, 11)[0]["value"] == 14.0          # after Thursday but before his own Sunday game: counts
    f = forecast(d, 2, 11)[0]["input"]
    assert f["captured_at"] == "2026-09-12T12:00:00+00:00" and f["kickoff"] == sun(2).isoformat()
    assert f["snapshot_id"] == "espn_projection_history:2026-09-12" and f["source"] == "ESPN"
    at_kickoff = data(espn_history={(2, 1): [{"value": 5.0, "synced_at": thu(2).replace(tzinfo=None), "snapshot_date": "x"}]})
    assert forecast(at_kickoff, 2, 1) == (None, "no_pregame_forecast")     # captured AT kickoff is not pregame
    assert forecast(d, 2, 40) == (None, "bye")                             # EEE has no game that week
    unknown = data(players={**d.players, 1: {"full_name": "Ann QB", "position": "QB", "pro_team": None}})
    assert forecast(unknown, 2, 1) == (None, "kickoff_unknown")


def test_a_team_change_can_only_make_a_forecast_less_eligible():
    # nflverse puts Fay (current team CCC, Sunday) on AAA that week (Thursday): the earlier kickoff is her cutoff.
    d = data(weekly_team={(2, 20): "AAA"},
             espn_history={(2, 20): [{"value": 11.0, "synced_at": datetime(2026, 9, 12, 12, 0), "snapshot_date": "d"}]})
    assert forecast(d, 2, 20) == (None, "no_pregame_forecast")


def test_the_roster_must_be_archived_between_the_previous_weeks_last_kickoff_and_the_weeks_first():
    meta, rows, why = roster_snapshot(data(), 2)
    assert why is None and len(rows) == len(ROSTER)
    assert meta == {"snapshot_id": "roster_history:2026-09-10", "captured_at": "2026-09-10T12:00:00+00:00",
                    "cutoff": thu(2).isoformat(), "source": "roster_history"}
    late = data(rosters=[{**r, "synced_at": datetime(2026, 9, 11, 1, 0)} for r in data().rosters])   # after Thursday
    assert roster_snapshot(late, 2)[0] is None
    early = data(rosters=[{**r, "synced_at": datetime(2026, 9, 7, 12, 0)} for r in data().rosters])  # before week 1 MNF
    assert roster_snapshot(early, 2)[0] is None
    # the latest qualifying snapshot wins
    two = data(rosters=data().rosters + [{"snapshot_date": date(2026, 9, 9), "espn_player_id": 1, "lineup_slot_id": 20,
                                          "synced_at": datetime(2026, 9, 9, 12, 0)}])
    assert roster_snapshot(two, 2)[0]["snapshot_id"] == "roster_history:2026-09-10"


def test_a_week_without_an_eligible_archive_says_so_and_never_grades():
    late = data(rosters=[{**r, "synced_at": datetime(2026, 9, 11, 1, 0)} for r in data().rosters])
    res = build_feedback(late)
    assert res["status"] == "no_archive" and res["message"] == "No archived recommendations for this week."
    w = res["weeks"][0]
    assert w["status"] == "no_archive" and w["calls"] == [] and w["reason"].startswith(dfb.NO_ARCHIVE)
    assert res["summary"]["graded"] == 0 and res["feedback"] == {"worked": [], "changed": [],
                                                                  "watch": {"above_forecast": [], "below_forecast": [], "notes": []}}
    # no forecast archived at all: also no archive (the roster alone is not a recommendation)
    none = build_feedback(data(espn_history={}))
    assert none["status"] == "no_archive" and none["weeks"][0]["coverage"]["decision_set"] == 0
    assert none["weeks"][0]["exclusions"]["no_pregame_forecast"] == 8


# -------------------------------------------------------------------------------------------------- grade math


def test_hand_computed_calls_verdicts_and_opportunity_cost_bounds():
    w = week_feedback(data(), 2)
    assert w["status"] == "graded"
    # Ivy (bye), Jo (IR slot), Kit (a kicker in a league without a K slot)
    assert w["exclusions"] == {"bye": 1, "reserve_slot": 1, "position_not_covered": 1}
    calls = by_slot(w)
    assert set(calls) == {"QB", "RB", "WR", "FLEX"}
    qb = calls["QB"]                       # Ann (20) over Bea (15): 18 vs 25
    assert (qb["start"]["espn_player_id"], qb["over"]["espn_player_id"]) == (1, 2)
    assert (qb["verdict"], qb["margin"], qb["forecast_gap"], qb["close_call"]) == ("missed", -7.0, 5.0, False)
    assert qb["opportunity_cost"] == {"lower": 7.0, "upper": 7.0, "upper_unknown": 0}
    rb = calls["RB"]                       # Dee (14) over Eli (8): Eli has no recorded points -> excluded, never 0
    assert (rb["start"]["espn_player_id"], rb["over"]["espn_player_id"]) == (11, 12)
    assert rb["excluded"] == "outcome_missing" and rb["verdict"] is None and rb["opportunity_cost"] is None
    wr = calls["WR"]                       # Fay (11) over Gus (9): 15 vs 12, a close call that worked
    assert (wr["verdict"], wr["margin"], wr["close_call"]) == ("correct", 3.0, True)
    assert wr["opportunity_cost"] == {"lower": 0.0, "upper": 0.0, "upper_unknown": 0}
    flex = calls["FLEX"]                   # Cal (12) over Gus (9): 7 vs 12; Eli (unknown) is also eligible
    assert (flex["start"]["espn_player_id"], flex["over"]["espn_player_id"]) == (10, 21)
    assert (flex["verdict"], flex["margin"], flex["forecast_gap"], flex["close_call"]) == ("missed", -5.0, 3.0, False)
    assert flex["opportunity_cost"] == {"lower": 5.0, "upper": None, "upper_unknown": 1}
    assert w["summary"] == {"calls": 4, "graded": 3, "excluded": 1, "correct": 1, "missed": 2, "push": 0,
                            "hit_rate": 0.333, "close_calls_missed": 0, "opportunity_cost_lower": 12.0,
                            "opportunity_cost_upper": None}
    # a missing decision-set outcome blocks the lineup grade instead of counting as zero
    assert w["lineup"]["complete"] is False and w["lineup"]["missing_outcomes"] == 1 and w["lineup"]["archived"] is None
    assert w["lineup"]["yours"] == 64.0 and w["lineup"]["yours_outside_decision_set"] == 0


def test_lineup_grades_and_bounds_once_every_outcome_is_recorded():
    d = data(outcomes={(2, p): v for p, v in {**ACTUAL, 12: 3.0}.items()})
    w = week_feedback(d, 2)
    calls = by_slot(w)
    assert (calls["RB"]["verdict"], calls["RB"]["margin"]) == ("correct", 17.0)
    assert calls["RB"]["opportunity_cost"] == {"lower": 0.0, "upper": 0.0, "upper_unknown": 0}
    assert calls["FLEX"]["opportunity_cost"] == {"lower": 5.0, "upper": 5.0, "upper_unknown": 0}   # best alt: Gus 12
    for c in calls.values():
        oc = c["opportunity_cost"]
        assert 0 <= oc["lower"] <= oc["upper"]
    lu = w["lineup"]
    assert (lu["complete"], lu["archived"], lu["best"], lu["archive_shortfall"]) == (True, 60.0, 72.0, 12.0)
    assert (lu["yours"], lu["archived_minus_yours"]) == (64.0, -4.0)
    assert w["summary"]["hit_rate"] == 0.5 and w["summary"]["opportunity_cost_upper"] == 12.0
    errors = {p["espn_player_id"]: p["error"] for p in w["players"]}
    assert errors[2] == 10.0 and errors[1] == -2.0 and len(errors) == 8
    expected = round(sum(abs(v) for v in errors.values()) / 8, 2)
    assert w["forecast_error"] == {"n": 8, "mae": expected, "bias": round(sum(errors.values()) / 8, 2)}


def test_a_push_counts_as_neither_correct_nor_missed():
    d = data(outcomes={(2, p): v for p, v in {**ACTUAL, 1: 25.0}.items()})
    qb = by_slot(week_feedback(d, 2))["QB"]
    assert (qb["verdict"], qb["margin"], qb["opportunity_cost"]["lower"]) == ("push", 0.0, 0.0)
    s = week_feedback(d, 2)["summary"]
    assert (s["correct"], s["missed"], s["push"], s["hit_rate"]) == (1, 1, 1, 0.5)


def test_what_worked_changed_and_watch_and_the_season_to_date():
    res = build_feedback(data())
    assert res["status"] == "ok" and res["scoring_periods"] == [2] and res["team"] == {"team_id": 2, "name": "Mine"}
    fb = res["feedback"]
    assert [c["slot"] for c in fb["worked"]] == ["WR"]
    assert [c["slot"] for c in fb["changed"]] == ["QB", "FLEX"]          # by lower-bound cost
    assert [p["espn_player_id"] for p in fb["watch"]["above_forecast"]] == [2, 11, 20]
    assert [p["espn_player_id"] for p in fb["watch"]["below_forecast"]] == [10, 1, 30]
    assert any("left ungraded" in n for n in fb["watch"]["notes"])
    season = res["season_to_date"]
    assert [(r["week"], r["status"]) for r in season["weeks"]] == [(1, "no_archive"), (2, "graded")]
    t = season["totals"]
    assert (t["weeks"], t["weeks_graded"], t["correct"], t["missed"], t["hit_rate"]) == (2, 1, 1, 2, 0.333)
    # no future leakage: week 3 (completed later) is never in a week-2 answer
    assert all(r["week"] <= 2 for r in season["weeks"])


def test_no_probability_or_letter_grade_is_ever_emitted():
    text = json.dumps(build_feedback(data()), default=str).lower()
    for word in ("probability", "confidence", "\"grade\"", "win_prob"):
        assert word not in text
    assert set(dfb.GRADE_DEFINITIONS) >= {"margin", "verdict", "hit_rate", "opportunity_cost", "lineup", "coverage",
                                          "exclusions"}


def test_no_team_and_no_completed_week():
    assert build_feedback(data(team=None))["status"] == "no_team"
    assert build_feedback(data(team=None))["weeks"] == []
    assert build_feedback(data(week=None))["status"] == "no_completed_week"


# --------------------------------------------------------------------------------------------- Ours source


def ours_row(pid, value, created, *, cov="projected", team=None):
    return {"week": 2, "espn_player_id": pid, "run_id": f"run-{pid}", "run_created_at": created,
            "model_version": "ifp-2.2.0", "scoring_fingerprint": "fp:x", "artifact_id": "a" * 64,
            "content_sha256": "c" * 64, "nfl_team": team or PLAYERS[pid][2], "kickoff": None,
            "coverage_status": cov, "expected_points": value}


def test_ours_uses_only_rows_created_before_kickoff_and_covers_skill_positions_only():
    pre, post = datetime(2026, 9, 10, 12, tzinfo=UTC), datetime(2026, 9, 12, 12, tzinfo=UTC)
    rows = {(2, p): ours_row(p, v, pre) for p, v in FORECAST.items() if p not in (12, 60)}
    rows[(2, 12)] = ours_row(12, 50.0, post)                 # made after Eli's Thursday kickoff: never evidence
    rows[(2, 30)] = ours_row(30, None, pre, cov="withheld_unavailable")   # Ours says he cannot score: a real 0
    d = data(source="independent", espn_history={}, ours=rows)
    assert forecast(d, 2, 12) == (None, "no_pregame_forecast")
    f, _ = forecast(d, 2, 30)
    assert f["value"] == 0.0 and f["input"]["snapshot_id"] == "run-30" and f["input"]["model_version"] == "ifp-2.2.0"
    w = week_feedback(d, 2)
    assert w["exclusions"]["position_not_covered"] == 1          # the kicker
    assert w["exclusions"]["no_pregame_forecast"] == 1           # Eli
    # Cal (12) starts at RB and Dee (10) at FLEX: no RB is left on the bench, so the RB slot has no call
    assert set(by_slot(w)) == {"QB", "WR", "FLEX"}
    assert all(c["start"]["input"]["content_sha256"] == "c" * 64 for c in w["calls"])
    assert build_feedback(d)["source"]["label"] == "Ours"


# ------------------------------------------------------------------------------------------ legacy route


def seed(db_path, *, mine=2, archive=True):
    con = connect(db_path)
    init_db(con)
    sched = {"scheduleSettings": {"matchupPeriodLength": 1, "matchupPeriods": {str(p): [p] for p in range(1, 6)}}}
    con.execute("INSERT INTO leagues (league_id, season, name, reg_season_matchups, final_scoring_period, roster_slots, "
                "settings_raw, synced_at) VALUES (7, 2026, 'Hand', 4, 5, ?, ?, now())", [json.dumps(SLOTS), json.dumps(sched)])
    for t in (1, 2):
        con.execute("INSERT INTO fantasy_teams (league_id, season, team_id, name, abbrev, is_mine) VALUES (7, 2026, ?, ?, ?, ?)",
                    [t, "Mine" if t == 2 else "Other Team", f"T{t}", t == mine])
    for p in (1, 2, 3):
        con.execute("INSERT INTO matchups VALUES (7, 2026, ?, ?, 1, 2, 100.0, 90.0, 'HOME', 'NONE')", [p, p])
    con.execute("INSERT INTO matchups VALUES (7, 2026, 4, 4, 1, 2, 1.0, 0.0, 'UNDECIDED', 'NONE')")
    con.execute("CREATE TABLE IF NOT EXISTS nfl_schedules (season INTEGER, week INTEGER, game_type VARCHAR, gameday VARCHAR, "
                "gametime VARCHAR, home_team VARCHAR, away_team VARCHAR)")
    for (w, team), ko in kickoffs().items():
        et = ko.astimezone(timezone(timedelta(hours=-4)))
        con.execute("INSERT INTO nfl_schedules (season, week, game_type, gameday, gametime, home_team, away_team) "
                    "VALUES (2026, ?, 'REG', ?, ?, ?, ?)", [w, et.date().isoformat(), et.strftime("%H:%M"), team, "Z" + team])
    for pid, (name, pos, team) in PLAYERS.items():
        con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team) VALUES (?, ?, ?, ?)",
                    [pid, name, pos, team])
    con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team) VALUES "
                "(99, 'Zed Otherteam', 'QB', 'BBB')")
    for pid, v in ACTUAL.items():
        con.execute("INSERT INTO espn_player_weekly_points (season, espn_player_id, scoring_period, actual_points) "
                    "VALUES (2026, ?, 2, ?)", [pid, v])
    con.execute("INSERT INTO espn_player_weekly_points (season, espn_player_id, scoring_period, actual_points) "
                "VALUES (2026, 99, 2, 40.0)")
    for pid, slot, starter in MINE:
        con.execute("INSERT INTO lineup_snapshots (league_id, season, scoring_period, team_id, espn_player_id, lineup_slot, "
                    "is_starter) VALUES (7, 2026, 2, 2, ?, ?, ?)", [pid, slot, starter])
    if archive:
        for pid in ROSTER:
            con.execute("INSERT INTO roster_history VALUES ('2026-09-10', 7, 2026, 2, ?, ?, ?)",
                        [pid, 21 if pid == 50 else 20, PRE])
        # the other team's archived roster: never part of team 2's answer
        con.execute("INSERT INTO roster_history VALUES ('2026-09-10', 7, 2026, 1, 99, 20, ?)", [PRE])
        for (w, pid), snaps in history().items():
            for s in snaps:
                con.execute("INSERT INTO espn_projection_history VALUES (?, 2026, ?, ?, ?, ?)",
                            [s["snapshot_date"], pid, w, s["value"], s["synced_at"]])
        con.execute("INSERT INTO espn_projection_history VALUES ('2026-09-10', 2026, 99, 2, 30.0, ?)", [PRE])
    con.close()


@pytest.fixture
def serve(tmp_path, monkeypatch):
    def _serve(**kw):
        db_path = tmp_path / "feedback.duckdb"
        seed(db_path, **kw)
        monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
        monkeypatch.setenv("ESPN_SEASON", "2026")
        monkeypatch.delenv("FANTASY_API_KEY", raising=False)
        from app.main import app
        return TestClient(app), db_path
    return _serve


def test_api_grades_the_owners_team_from_the_archive(serve):
    api, _ = serve()
    r = api.get("/api/weekly-recap/feedback", params={"week": 2})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["status"] == "ok" and res["team"] == {"team_id": 2, "name": "Mine"} and res["source"]["key"] == "espn"
    calls = {c["slot"]: c for c in res["weeks"][0]["calls"]}
    assert calls["QB"]["opportunity_cost"] == {"lower": 7.0, "upper": 7.0, "upper_unknown": 0}
    assert calls["RB"]["start"]["forecast"] == 14.0 and calls["RB"]["excluded"] == "outcome_missing"
    assert res["weeks"][0]["roster_input"]["snapshot_id"] == "roster_history:2026-09-10"
    assert "Zed Otherteam" not in r.text and "Other Team" not in r.text       # no other team's roster or name
    # the default week is the latest completed one (3): no roster was archived for it
    latest = api.get("/api/weekly-recap/feedback").json()
    assert latest["selected_week"] == 3 and latest["status"] == "no_archive"
    assert latest["message"] == "No archived recommendations for this week."


def test_api_refuses_any_team_and_validates_after(serve):
    api, _ = serve()
    for team in (1, 2, "x"):
        r = api.get("/api/weekly-recap/feedback", params={"week": 2, "team_id": team})
        assert (r.status_code, r.json()) == (404, {"detail": "League not found."})
    assert api.get("/api/weekly-recap/feedback", params={"source": "bogus"}).status_code == 422
    assert api.get("/api/weekly-recap/feedback", params={"week": 0}).status_code == 422
    assert api.get("/api/weekly-recap/feedback", params={"week": 4}).status_code == 404       # not final


def test_api_without_an_own_team_shows_nothing_of_anyones_roster(serve):
    api, _ = serve(mine=None)
    r = api.get("/api/weekly-recap/feedback", params={"week": 2})
    assert r.status_code == 200 and r.json()["status"] == "no_team" and r.json()["weeks"] == []
    for name in ("Ann QB", "Zed Otherteam", "Mine", "Other Team"):
        assert name not in r.text


def test_api_is_read_only(serve):
    api, db_path = serve()
    mtime = db_path.stat().st_mtime_ns
    for params in ({"week": 2}, {"week": 2, "source": "independent"}, {}):
        assert api.get("/api/weekly-recap/feedback", params=params).status_code == 200
    assert db_path.stat().st_mtime_ns == mtime


def test_api_independent_source_without_runs_is_an_honest_no_archive(serve):
    api, _ = serve()
    res = api.get("/api/weekly-recap/feedback", params={"week": 2, "source": "independent"}).json()
    assert res["source"]["label"] == "Ours" and res["status"] == "no_archive"
    assert res["weeks"][0]["exclusions"].get("position_not_covered") == 1


def _store_run(db_path, run_id, created, values):
    """One sealed production run (week 2) through the store's own writer: append-only, content-hashed."""
    from app.analytics.independent_projection_store import write_run
    from app.analytics.projection_source import MODEL_VERSION, _scoring
    con = connect(db_path)
    try:
        fp, _ = _scoring(con, 2026)
        meta = {"run_id": run_id, "season": 2026, "week": 2, "model_version": MODEL_VERSION, "scoring_fingerprint": fp,
                "artifact_created_at": "2026-09-01T00:00:00+00:00", "artifact_id": "b" * 64, "data_through_season": 2026,
                "data_through_week": 1, "model_status": {}, "evaluation": {}, "created_at": created}
        rows = [{"season": 2026, "week": 2, "espn_player_id": pid, "gsis_id": f"00-{pid:07d}", "position": PLAYERS[pid][1],
                 "nfl_team": PLAYERS[pid][2], "opponent": None, "is_home": True, "projected_points": v, "model": "context",
                 "coverage_status": "projected", "p_play": 1.0, "expected_points": v, "flags": [], "missing_inputs": [],
                 "explanation": []} for pid, v in values.items()]
        return write_run(con, meta, rows)
    finally:
        con.close()


def test_api_ours_reads_sealed_runs_made_before_each_kickoff(serve):
    from synthetic_forecast import SCORING_ITEMS
    api, db_path = serve()
    con = connect(db_path)
    con.execute("UPDATE leagues SET scoring_items = ?", [json.dumps(SCORING_ITEMS)])
    con.close()
    first = _store_run(db_path, "run-a", datetime(2026, 9, 10, 12, 0),
                       {p: FORECAST[p] for p in (1, 2, 10, 11, 12, 20, 21, 30)})
    # a later run, after Eli's Thursday kickoff but before the Sunday games: it counts for Sunday players only
    second = _store_run(db_path, "run-b", datetime(2026, 9, 12, 12, 0), {**{p: FORECAST[p] for p in (1, 2, 10, 20, 21, 30)},
                                                                         11: 14.0, 12: 99.0})
    res = api.get("/api/weekly-recap/feedback", params={"week": 2, "source": "independent"}).json()
    assert res["status"] == "ok" and res["source"]["versions"] == {"2": "ifp-2.2.0"}
    calls = {c["slot"]: c for c in res["weeks"][0]["calls"]}
    eli = calls["RB"]["over"]
    assert eli["espn_player_id"] == 12 and eli["forecast"] == 8.0 and eli["input"]["snapshot_id"] == "run-a"
    dee = calls["RB"]["start"]
    assert dee["forecast"] == 14.0 and dee["input"]["snapshot_id"] == "run-b"
    assert dee["input"]["content_sha256"] == second["content_sha256"] and dee["input"]["captured_at"].startswith("2026-09-12")
    # a stored row edited after the fact fails the run's content hash: that run is never evidence again
    con = connect(db_path)
    con.execute("UPDATE independent_projection_rows SET expected_points = 1.0 WHERE run_id = 'run-b' AND espn_player_id = 11")
    con.close()
    res = api.get("/api/weekly-recap/feedback", params={"week": 2, "source": "independent"}).json()
    dee = next(p for p in res["weeks"][0]["players"] if p["espn_player_id"] == 11)
    assert dee["input"]["snapshot_id"] == "run-a" and dee["forecast"] == 10.0
    assert dee["input"]["content_sha256"] == first["content_sha256"]


# -------------------------------------------------------------------------------------------- /v2 mount


@pytest.fixture(scope="module")
def e2e_archive(tmp_path_factory):
    """The E2E fixture league with an archived pregame roster and ESPN forecasts for week 2 (all week-2 games kick off
    2026-09-02 17:00 UTC; week 1's on 2026-09-01) for team 2 (the owner's) and team 3."""
    from golden.golden import build_e2e_db
    db = build_e2e_db(tmp_path_factory.mktemp("fb") / "e2e.duckdb")
    con = duckdb.connect(str(db))
    at = datetime(2026, 9, 2, 10, 0)
    for team in (2, 3):
        rows = con.execute("SELECT espn_player_id, lineup_slot_id FROM lineup_snapshots WHERE scoring_period = 2 "
                           "AND team_id = ?", [team]).fetchall()
        for pid, slot in rows:
            con.execute("INSERT INTO roster_history VALUES ('2026-09-02', 999, 2026, ?, ?, ?, ?)", [team, pid, slot, at])
            con.execute("INSERT OR REPLACE INTO espn_projection_history VALUES ('2026-09-02', 2026, ?, 2, ?, ?)",
                        [pid, 10.0 + (pid % 7), at])
    con.close()
    return db


def test_v2_own_team_only_with_indistinguishable_404s(pg, env, tmp_path, monkeypatch, e2e_archive):
    from cp_seed import U1, U2, U3, seed_membership, seed_user
    from league_fixtures import publish_local
    from test_leagues_api import bearer  # noqa: I001

    from app.api import deps, league_scope
    objects = tmp_path / "objects"
    monkeypatch.setenv("LEAGUE_FILES_DIR", str(objects))
    monkeypatch.setenv("ESPN_SEASON", "2026")
    monkeypatch.setattr(league_scope, "_CACHE", {})
    monkeypatch.setattr(league_scope, "_RESPONSES", {})
    monkeypatch.setattr(deps, "_LAYERED", {})
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    from app.main import app
    client = TestClient(app, raise_server_exceptions=False)
    priv = publish_local(pg, e2e_archive, objects, tmp_path / "w", visibility="private")
    pub = publish_local(pg, e2e_archive, objects, tmp_path / "p", visibility="public", name="Pub",
                        stamp="2026-10-20T13:00:00", external_id="1999")
    with pg.connect() as con:
        for u in (U1, U2, U3):
            seed_user(con, u)
        seed_membership(con, U1, priv.league_id, "admin")
        seed_membership(con, U2, priv.league_id, "viewer")
        for user, lg, team in ((U1, priv.league_id, "2"), (U2, priv.league_id, "3"), (U1, pub.league_id, "2")):
            con.execute("INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, %s)",
                        [user, lg, team])
        con.commit()
    url = f"/v2/leagues/{priv.league_id}/api/weekly-recap/feedback?week=2"
    own = client.get(url, headers=bearer(U1))
    assert own.status_code == 200, own.text
    assert own.headers["cache-control"] == "private, no-store"
    assert own.json()["status"] == "ok" and own.json()["team"]["team_id"] == 2
    other = client.get(url, headers=bearer(U2))                        # another member sees only their own team
    assert other.json()["team"]["team_id"] == 3 and other.json()["status"] == "ok"
    mine_ids = {p["espn_player_id"] for w in own.json()["weeks"] for p in w["players"]}
    theirs_ids = {p["espn_player_id"] for w in other.json()["weeks"] for p in w["players"]}
    assert mine_ids and theirs_ids and not mine_ids & theirs_ids
    not_found = {"detail": "League not found."}
    for headers, path in ((bearer(U2), url + "&team_id=2"), (bearer(U1), url + "&team_id=2"), ({}, url),
                          (bearer(U3), url), ({}, f"/v2/leagues/lg_{'z' * 24}/api/weekly-recap/feedback")):
        r = client.get(path, headers=headers)
        assert (r.status_code, r.json()) == (404, not_found), (path, r.text[:200])
    # public league: an anonymous visitor's URL team is never "their" team, and no cached answer reaches them
    purl = f"/v2/leagues/{pub.league_id}/api/weekly-recap/feedback?week=2"
    assert client.get(purl, headers=bearer(U1)).json()["team"]["team_id"] == 2
    anon = client.get(purl + "&team_id=2")
    assert (anon.status_code, anon.json()) == (404, not_found)
    nobody = client.get(purl)
    assert nobody.status_code == 200 and nobody.json()["status"] == "no_team" and nobody.json()["weeks"] == []
    stranger = client.get(purl, headers=bearer(U3))                   # signed in, no saved team here
    assert stranger.json()["status"] == "no_team"
