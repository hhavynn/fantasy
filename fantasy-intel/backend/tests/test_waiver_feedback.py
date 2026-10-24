"""waiver-feedback.v1 (app/analytics/waiver_feedback.py, issue #346) on hand-built data: the cutoff, which capture and
roster are as of it, the hindsight lineup gain and its regret bound, and every exclusion. No database."""
from datetime import UTC, datetime, timedelta

import pytest

from app.analytics import waiver_feedback as wf

SUNDAY = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)          # week 1's only kickoff; week k is 7 days later each


def kick(week):
    return SUNDAY + timedelta(days=7 * (week - 1))


def data(**kw):
    """Team 1 starts QB 1 and RB 2 (5 a week), benches RB 3 (1 a week). Free agents: RB 10 (12 a week, archived as
    CLAIM with drop RB 3) and RB 11 (20 a week, archived as WAIT). Waivers process on Wednesdays."""
    d = wf.WaiverData(league={"league_id": 7, "season": 2026, "name": "L", "final_scoring_period": 17},
                      team={"team_id": 1, "name": "Mine"}, roster_slots={"QB": 1, "RB": 1, "BE": 2},
                      process_days=[2], week=4,
                      schedule_settings={"matchupPeriods": {str(p): [p] for p in range(1, 18)}})
    d.matchups = [{"matchup_period": p, "home_team_id": 1, "away_team_id": 2, "home_points": 1, "away_points": 0,
                   "winner": "HOME", "playoff_tier": None, "matchup_id": p} for p in range(1, 7)]
    d.kickoffs = {(w, t): kick(w) for w in range(1, 18) for t in ("KC", "BUF")}
    d.games = set(d.kickoffs)                                 # every scheduled game, its kickoff known or not
    d.players = {1: {"full_name": "QB", "position": "QB", "pro_team": "KC"},
                 2: {"full_name": "RB2", "position": "RB", "pro_team": "KC"},
                 3: {"full_name": "RB3", "position": "RB", "pro_team": "BUF"},
                 10: {"full_name": "RB10", "position": "RB", "pro_team": "KC"},
                 11: {"full_name": "RB11", "position": "RB", "pro_team": "BUF"}}
    pts = {1: 20.0, 2: 5.0, 3: 1.0, 10: 12.0, 11: 20.0}
    d.outcomes = {(w, p): v for w in range(1, 18) for p, v in pts.items()}
    d.captures = capture(2, datetime(2026, 9, 21, 13, tzinfo=UTC))
    for k, v in kw.items():
        setattr(d, k, v)
    if "rosters" not in kw:                                   # each capture archived with the roster QB 1, RB 2, RB 3
        d.rosters = roster(*{r["snapshot_id"] for r in d.captures})
    return d


def roster(*snapshot_ids, players=(1, 2, 3)):
    return [{"snapshot_id": s, "espn_player_id": p, "lineup_slot_id": 0} for s in snapshot_ids for p in players]


def capture(week, at, claim=10, wait=11, drop=3, status="FREEAGENT"):
    common = {"week": week, "captured_at": at, "snapshot_id": f"s:{week}:{at.isoformat()}", "content_sha256": "h",
              "projection_source": "espn", "projection_version": "v", "scoring_fingerprint": "f",
              "rule_version": "waiver-archive.v1", "archive_k": 25, "horizon_weeks": 3, "roster_synced_at": None,
              "availability_status": status}
    return [{**common, "espn_player_id": claim, "rank": 1, "recommendation": "CLAIM", "suggested_drop_id": drop},
            {**common, "espn_player_id": wait, "rank": 2, "recommendation": "WAIT", "suggested_drop_id": drop}]


def flat(*caps):
    return [r for c in caps for r in c]


def test_the_cutoff_is_the_processing_day_starting_in_utc_plus_14_or_the_first_kickoff():
    c, why = wf.cutoff(data(), 2)
    # week 1 ends Sunday 13 Sep 17:00 UTC; the next Wednesday (16 Sep) starts at 15 Sep 10:00 UTC in UTC+14
    assert why is None and c["cutoff"] == datetime(2026, 9, 15, 10, tzinfo=UTC) and c["processing_day"] == "2026-09-16"
    assert c["window_end"] == datetime(2026, 9, 17, 12, tzinfo=UTC)             # 16 Sep ends at 17 Sep 12:00 UTC
    d = data(process_days=[5])                                                  # Saturdays: after next Sunday's game?
    d.kickoffs = {**d.kickoffs, (2, "KC"): datetime(2026, 9, 17, 0, 15, tzinfo=UTC)}   # a Thursday game in week 2
    c, _ = wf.cutoff(d, 2)
    assert c["cutoff"] == datetime(2026, 9, 17, 0, 15, tzinfo=UTC)                     # the first kickoff comes first


@pytest.mark.parametrize("week, team", [(2, "KC"), (1, "BUF")])
def test_a_boundary_game_with_an_unknown_kickoff_leaves_the_cutoff_unknown(week, team):
    """Codex #353: an unknown kickoff of week W (it could be a Thursday game before the processing day) or of week
    W-1 (it could be the last game) leaves the other games' extremes unproven: no cutoff, never a later one."""
    d = data(process_days=[5])
    del d.kickoffs[(week, team)]                                              # still scheduled: in d.games
    assert wf.cutoff(d, 2) == (None, "kickoff_unknown")
    assert wf.week_feedback(d, 2)["status"] == "kickoff_unknown"
    d.games.discard((week, team))                                             # not scheduled at all: a bye
    assert wf.cutoff(d, 2)[1] is None


@pytest.mark.parametrize("change, status", [
    ({"process_days": None}, "schedule_unknown"),
    ({"kickoffs": {}}, "kickoff_unknown"),
])
def test_without_a_known_cutoff_the_week_is_ungraded(change, status):
    out = wf.week_feedback(data(**change), 2)
    assert out["status"] == status and out["claims"] == [] and out["message"] == wf.WEEK_STATUS_TEXT[status]


def test_week_one_has_no_waiver_run_to_grade():
    assert wf.week_feedback(data(), 1)["status"] == "no_prior_week"


def test_the_gain_is_the_hindsight_lineup_difference_and_regret_is_bounded_by_the_archive():
    out = wf.week_feedback(data(captures=flat(capture(2, datetime(2026, 9, 14, 13, tzinfo=UTC)))), 2)
    assert out["status"] == "graded" and out["horizon"] == [2, 3, 4]
    (claim,) = out["claims"]
    assert claim["player"]["espn_player_id"] == 10 and claim["drop"]["espn_player_id"] == 3
    assert claim["gain"] == 21.0 and claim["verdict"] == "helped"              # (12 - 5) a week, for 3 weeks
    assert claim["regret_lower_bound"] == 24.0                                 # RB 11: (20 - 5) x 3 - 21
    assert out["capture"]["snapshot_id"].startswith("s:2:")
    assert out["roster"]["snapshot_id"] == f"waiver_roster_snapshots:{out['capture']['snapshot_id']}"


def test_only_a_capture_taken_before_the_cutoff_counts_and_the_latest_one_wins():
    cut = datetime(2026, 9, 15, 10, tzinfo=UTC)
    early, later = capture(2, cut - timedelta(hours=5)), capture(2, cut - timedelta(hours=1), claim=11, wait=10)
    at_cut = capture(2, cut, claim=3, wait=2)                                  # taken at the cutoff: never used
    out = wf.week_feedback(data(captures=flat(early, later, at_cut)), 2)
    assert [c["player"]["espn_player_id"] for c in out["claims"]] == [11]       # the latest capture before the cutoff
    out = wf.week_feedback(data(captures=flat(at_cut, capture(2, cut + timedelta(minutes=1)))), 2)
    assert out["status"] == "no_archive" and out["claims"] == []


def test_the_roster_is_the_one_archived_with_the_capture():
    """Codex #353: roster_history keeps one snapshot a day, which a later sync that day replaces, so the grade reads the
    roster archived with the capture: never another capture's, and none archived is no_roster."""
    cut = datetime(2026, 9, 15, 10, tzinfo=UTC)
    early, later = capture(2, cut - timedelta(hours=5)), capture(2, cut - timedelta(hours=1))
    rosters = roster(early[0]["snapshot_id"]) + roster(later[0]["snapshot_id"], players=(1, 2))   # RB 3 since dropped
    out = wf.week_feedback(data(captures=flat(early, later), rosters=rosters), 2)
    assert out["excluded"] == {"drop_not_on_roster": 1} and out["roster"]["snapshot_id"].endswith(later[0]["snapshot_id"])
    out = wf.week_feedback(data(captures=flat(early, later), rosters=roster(early[0]["snapshot_id"])), 2)
    assert out["status"] == "no_roster"


def test_a_missing_outcome_is_unknown_but_a_known_bye_scores_zero():
    cap = flat(capture(2, datetime(2026, 9, 14, 13, tzinfo=UTC)))
    d = data(captures=cap)
    del d.outcomes[(3, 10)]                                                    # RB 10's team plays week 3: unknown
    out = wf.week_feedback(d, 2)
    assert out["status"] == "excluded" and out["excluded"] == {"unknown_outcome": 1} and out["claims"] == []
    d = data(captures=cap)
    del d.outcomes[(3, 10)]
    d.kickoffs = {k: v for k, v in d.kickoffs.items() if k != (3, "KC")}      # KC on bye in week 3 (RB 2, RB 10)
    d.games.discard((3, "KC"))
    del d.outcomes[(3, 2)], d.outcomes[(3, 1)]
    (claim,) = wf.week_feedback(d, 2)["claims"]
    # +7 in weeks 2 and 4; in KC's bye week RB 3 (BUF, dropped) would have started for 1, and nobody replaces him
    assert claim["gain"] == 13.0


def test_a_scheduled_game_with_an_unknown_kickoff_is_not_a_bye():
    """Codex #353: KC plays in week 3 but its kickoff time is unknown (absent from the kickoffs). RB 10's missing
    outcome is then unknown, never a bye scored 0."""
    d = data(captures=flat(capture(2, datetime(2026, 9, 14, 13, tzinfo=UTC))))
    del d.outcomes[(3, 10)]
    d.kickoffs = {k: v for k, v in d.kickoffs.items() if k != (3, "KC")}      # the game stays in d.games
    out = wf.week_feedback(d, 2)
    assert out["excluded"] == {"unknown_outcome": 1} and out["claims"] == []


def test_a_player_another_team_added_in_the_waiver_window_is_excluded():
    cap = flat(capture(2, datetime(2026, 9, 14, 13, tzinfo=UTC)))
    d = data(captures=cap, adds=[{"espn_player_id": 10, "team_id": 2, "at": datetime(2026, 9, 16, 11, tzinfo=UTC)}])
    assert wf.week_feedback(d, 2)["excluded"] == {"taken_by_other_team": 1}
    mine = data(captures=cap, adds=[{"espn_player_id": 10, "team_id": 1, "at": datetime(2026, 9, 16, 11, tzinfo=UTC)}])
    assert wf.week_feedback(mine, 2)["status"] == "graded"                     # your own add is no exclusion
    late = data(captures=cap, adds=[{"espn_player_id": 10, "team_id": 2, "at": datetime(2026, 9, 18, tzinfo=UTC)}])
    assert wf.week_feedback(late, 2)["status"] == "graded"                     # after the window: still graded


def test_an_ineligible_status_or_a_drop_off_the_roster_is_excluded():
    at = datetime(2026, 9, 14, 13, tzinfo=UTC)
    assert wf.week_feedback(data(captures=capture(2, at, status="ONTEAM")), 2)["excluded"] == {"not_eligible": 1}
    assert wf.week_feedback(data(captures=capture(2, at, drop=99)), 2)["excluded"] == {"drop_not_on_roster": 1}


def test_a_horizon_that_is_not_complete_grades_nothing():
    d = data(captures=flat(capture(4, datetime(2026, 9, 28, 13, tzinfo=UTC))))
    d.matchups = [m for m in d.matchups if m["matchup_period"] <= 5]           # weeks 4 and 5 done, 6 not
    assert wf.week_feedback(d, 4)["status"] == "horizon_incomplete"


def test_the_response_states_its_definitions_and_reads_only_the_viewers_team():
    body = wf.build_waiver_feedback(data(week=2, captures=flat(capture(2, datetime(2026, 9, 14, 13, tzinfo=UTC)))))
    assert body["schema"] == body["rule_version"] == "waiver-feedback.v1"
    assert set(body["definitions"]) == set(wf.GRADE_DEFINITIONS) and body["exclusion_reasons"] == dict(wf.EXCLUSION_TEXT)
    assert body["status"] == "ok" and body["summary"]["helped"] == 1 and body["team"]["team_id"] == 1
    assert wf.build_waiver_feedback(data(team=None))["status"] == "no_team"
