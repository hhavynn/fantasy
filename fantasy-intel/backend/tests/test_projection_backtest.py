"""Synthetic test fixtures only; calibration never manufactures evaluation data."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

UTC = timezone.utc
KICKOFF = datetime(2026, 9, 13, 17, tzinfo=UTC)  # 13:00 Eastern (DST)
AS_OF = datetime(2026, 9, 20, 17, tzinfo=UTC)


@pytest.fixture
def lab(con):
    con.execute("INSERT INTO leagues (league_id, season, current_week) VALUES (999, 2026, 3)")
    con.execute("""CREATE TABLE nfl_rosters_weekly (
        season INTEGER, week INTEGER, espn_id VARCHAR, gsis_id VARCHAR, team VARCHAR, position VARCHAR)""")
    con.execute("""CREATE TABLE nfl_schedules (
        season INTEGER, week INTEGER, game_type VARCHAR, game_id VARCHAR,
        gameday VARCHAR, gametime VARCHAR, home_team VARCHAR, away_team VARCHAR,
        home_score DOUBLE, away_score DOUBLE)""")
    con.execute("INSERT INTO nfl_schedules VALUES (2026, 1, 'REG', 'game1', '2026-09-13', '13:00', 'BUF', 'MIA', 20, 10)")
    con.execute("INSERT INTO sync_runs (run_id,source,started_at,finished_at,status) VALUES ('test_run','espn','2026-09-15 17:00:00','2026-09-15 17:01:00','ok')")
    archive = archive_root(con) / 'test_run'
    archive.mkdir(parents=True)
    (archive / 'league.json').write_text(json.dumps({'id':999,'seasonId':2026,'scoringPeriodId':3}))
    (archive / 'cards_00.json').write_text(json.dumps({'players':[]}))
    return con


def naive_utc(at):
    """Store as the repository does (app.db.connection.utcnow): naive UTC. DuckDB would
    otherwise convert a tz-aware value to the session's local time zone."""
    return at.astimezone(UTC).replace(tzinfo=None) if at else at


def player(con, pid=1, position="RB", actual=14.0, projected=10.0, hours=1):
    con.execute("INSERT INTO nfl_rosters_weekly VALUES (2026, 1, ?, ?, 'BUF', ?)", [str(pid), f"G{pid}", position])
    con.execute("INSERT INTO espn_player_weekly_points VALUES (2026, ?, 1, ?, 999, ?)", [pid, actual, naive_utc(KICKOFF + timedelta(days=2))])
    snapshot(con, pid, projected, KICKOFF - timedelta(hours=hours))
    path = archive_root(con) / 'test_run/cards_00.json'
    payload = json.loads(path.read_text())
    payload['players'].append({'player':{'id':pid,'stats':[{
        'seasonId':2026,'scoringPeriodId':1,'statSourceId':0,'statSplitTypeId':1,'appliedTotal':actual}]}})
    path.write_text(json.dumps(payload))


def archive_root(con):
    db_path = con.execute("SELECT path FROM duckdb_databases() WHERE path IS NOT NULL").fetchone()[0]
    return Path(db_path).parent / 'raw/espn'


def snapshot(con, pid, points, at):
    day = at.date() if at else KICKOFF.date()
    con.execute("INSERT OR REPLACE INTO espn_projection_history VALUES (?, 2026, ?, 1, ?, ?)", [day, pid, points, naive_utc(at)])


def report(con, **kwargs):
    from app.analytics.projection_backtest import backtest
    kwargs.setdefault("as_of", AS_OF)
    kwargs.setdefault("raw_archive_root", archive_root(con))
    return backtest(con, **kwargs)


def test_metrics_use_latest_pregame_player_week_not_latest_points(lab):
    player(lab, 1, "QB", actual=15, projected=20)
    player(lab, 2, "RB", actual=14, projected=10)
    snapshot(lab, 1, 40, KICKOFF - timedelta(days=2))
    result = report(lab)
    assert result["overall"]["n"] == 2
    assert result["overall"]["mae"] == 4.5
    assert result["overall"]["rmse"] == pytest.approx((41 / 2) ** 0.5)
    assert result["overall"]["bias"] == -0.5
    assert result["by_position"]["QB"]["bias"] == -5
    assert result["by_position"]["RB"]["bias"] == 4
    assert result["by_position"]["WR"]["n"] == 0
    assert result["counts"]["eligible_player_weeks"] == 2
    assert result["overall"]["status"] == "insufficient historical observations"


@pytest.mark.parametrize("hours", [0, -1])
def test_exact_kickoff_and_post_start_predictions_are_excluded(lab, hours):
    player(lab, hours=hours)
    result = report(lab)
    assert result["overall"]["n"] == 0
    assert result["counts"]["at_or_after_kickoff_snapshot_rows"] == 1
    assert result["exclusions"]["no_pregame_projection"] == 1


def test_valid_earlier_snapshot_survives_later_postgame_snapshot(lab):
    player(lab, hours=48)
    snapshot(lab, 1, 100, KICKOFF + timedelta(hours=1))
    result = report(lab)
    assert result["overall"]["mae"] == 4
    assert result["counts"]["pregame_snapshot_rows"] == 1
    assert result["counts"]["at_or_after_kickoff_snapshot_rows"] == 1


def test_measured_zero_negative_points_and_missing_stay_distinct(lab):
    player(lab, 1, actual=0, projected=0)
    player(lab, 2, actual=-2, projected=0)
    player(lab, 3, actual=None)
    player(lab, 4, projected=None)
    result = report(lab)
    assert result["overall"]["n"] == 2
    assert result["overall"]["mae"] == 1
    assert result["overall"]["bias"] == -1
    assert result["counts"]["actual_zero_player_weeks"] == 1
    assert result["exclusions"]["missing_actual"] == 1
    assert result["exclusions"]["missing_projection"] == 1


def test_current_team_is_not_used_for_traded_player(lab):
    player(lab)
    lab.execute("INSERT INTO fantasy_players (espn_player_id, position, pro_team) VALUES (1, 'QB', 'KC')")
    assert report(lab)["by_position"]["RB"]["n"] == 1


def test_current_player_metadata_cannot_replace_historical_relationship(lab):
    player(lab)
    lab.execute("DELETE FROM nfl_rosters_weekly")
    lab.execute("INSERT INTO fantasy_players (espn_player_id, position, pro_team) VALUES (1, 'RB', 'BUF')")
    result = report(lab)
    assert result["overall"]["n"] == 0
    assert result["exclusions"]["missing_historical_roster"] == 1


def test_ambiguous_historical_team_is_excluded(lab):
    player(lab)
    lab.execute("INSERT INTO nfl_rosters_weekly VALUES (2026, 1, '1', 'G1', 'MIA', 'RB')")
    assert report(lab)["exclusions"]["ambiguous_historical_roster"] == 1


def test_high_confidence_id_crosswalk_requires_matching_weekly_roster(lab):
    player(lab)
    lab.execute("UPDATE nfl_rosters_weekly SET espn_id=NULL")
    lab.execute("INSERT INTO player_id_map (espn_player_id,gsis_id,match_method,match_confidence) VALUES (1,'G1','espn_id:nfl_players','high')")
    assert report(lab)["overall"]["n"] == 1
    lab.execute("UPDATE player_id_map SET match_confidence='medium'")
    assert report(lab)["overall"]["n"] == 0


@pytest.mark.parametrize("mutation,reason", [
    ("UPDATE nfl_schedules SET gametime=NULL", "missing_kickoff"),
    ("UPDATE nfl_schedules SET gameday='bad'", "missing_kickoff"),
    ("UPDATE nfl_schedules SET home_score=NULL", "game_not_completed"),
    ("UPDATE nfl_schedules SET game_type='POST'", "missing_game"),
    ("UPDATE leagues SET current_week=1", "scoring_period_not_closed"),
    ("UPDATE espn_player_weekly_points SET synced_at=NULL", "actual_completion_unverified"),
    ("UPDATE espn_player_weekly_points SET synced_at='2026-09-13 19:00:00'", "actual_completion_unverified"),
    ("UPDATE espn_projection_history SET synced_at=NULL", "no_pregame_projection"),
    ("UPDATE nfl_rosters_weekly SET position='K'", "unsupported_position"),
])
def test_uncertain_observations_are_explained(lab, mutation, reason):
    player(lab)
    lab.execute(mutation)
    result = report(lab)
    assert result["overall"]["n"] == 0
    assert result["exclusions"][reason] == 1


def test_ambiguous_schedule_excluded(lab):
    player(lab)
    lab.execute("INSERT INTO nfl_schedules SELECT season,week,game_type,'game2',gameday,gametime,home_team,away_team,home_score,away_score FROM nfl_schedules")
    assert report(lab)["exclusions"]["ambiguous_game"] == 1


def test_missing_history_not_reconstructed_from_current_projection(lab):
    player(lab)
    lab.execute("DELETE FROM espn_projection_history")
    assert report(lab)["exclusions"]["missing_projection_history"] == 1


def test_horizons_deduplicate_and_pair_same_player_weeks(lab):
    player(lab, actual=12, projected=10)
    snapshot(lab, 1, 8, KICKOFF - timedelta(hours=48))
    snapshot(lab, 1, 6, KICKOFF - timedelta(hours=72))
    snapshot(lab, 1, 4, KICKOFF - timedelta(hours=168))
    player(lab, 2, actual=9, projected=8, hours=30)
    result = report(lab, min_samples=1)
    assert result["by_horizon"]["0-24h"]["n"] == 1
    assert result["by_horizon"]["24-72h"]["n"] == 2
    assert result["by_horizon"]["72-168h"]["n"] == 1
    assert result["by_horizon"]["168h+"]["n"] == 1
    pair = next(p for p in result["paired_horizon_comparisons"] if p["near"] == "0-24h" and p["far"] == "24-72h")
    assert pair["n"] == 1
    assert pair["near_metrics"]["mae"] == 2
    assert pair["far_metrics"]["mae"] == 4
    assert pair["far_minus_near_mae"] == 2


def test_projected_point_bands_are_pregame_not_based_on_actual(lab):
    player(lab, 1, actual=0, projected=10)
    player(lab, 2, actual=40, projected=9)
    result = report(lab)
    assert result["by_projected_points"]["higher"]["bias"] == -10
    assert result["by_projected_points"]["lower"]["bias"] == 31
    assert "proxy" in result["cohort_note"]


def test_no_schema_or_observations_returns_null_metrics(con):
    con.execute("DROP TABLE espn_projection_history")
    result = report(con)
    assert result["overall"]["n"] == 0
    assert result["overall"]["mae"] is None
    assert result["overall"]["rmse"] is None
    assert result["overall"]["bias"] is None
    assert "espn_projection_history" in " ".join(result["schema_issues"])


def test_future_snapshots_and_future_actual_refresh_are_excluded(lab):
    player(lab)
    lab.execute("UPDATE espn_player_weekly_points SET synced_at='2026-09-21 12:00:00'")
    assert report(lab)["exclusions"]["actual_unavailable_at_cutoff"] == 1


def test_nonfinite_scores_are_not_exported_as_nan(lab):
    player(lab, projected=float('nan'))
    result = report(lab)
    assert result["exclusions"]["invalid_projection"] == 1
    json.dumps(result, allow_nan=False)


def test_as_of_requires_a_timezone(lab):
    with pytest.raises(ValueError, match="timezone"):
        report(lab, as_of=datetime(2026, 9, 20))


@pytest.mark.parametrize("kwargs", [{"min_samples": 0}, {"high_projection_threshold": float('nan')}])
def test_invalid_reporting_parameters_rejected(lab, kwargs):
    with pytest.raises(ValueError):
        report(lab, **kwargs)


def test_aggregate_output_contains_no_player_records(lab):
    player(lab, pid=987654321)
    result = report(lab)
    rendered = json.dumps(result)
    assert '987654321' not in rendered
    assert 'G987654321' not in rendered
    assert result["counts"]["eligible_distinct_players"] == 1


def test_snapshot_quality_counts_do_not_require_known_kickoff(lab):
    player(lab, projected=None)
    lab.execute("UPDATE espn_projection_history SET synced_at=NULL")
    lab.execute("DELETE FROM nfl_rosters_weekly")
    result = report(lab)
    assert result["counts"]["missing_projection_snapshot_rows"] == 1
    assert result["counts"]["missing_snapshot_timestamp_rows"] == 1


def test_schedule_failure_counts_unclassifiable_snapshots(lab):
    player(lab)
    lab.execute("UPDATE nfl_schedules SET gametime=NULL")
    assert report(lab)["counts"]["snapshot_rows_without_resolved_kickoff"] == 1


def test_postseason_roster_cannot_supply_regular_season_team(lab):
    player(lab)
    lab.execute("ALTER TABLE nfl_rosters_weekly ADD COLUMN season_type VARCHAR DEFAULT 'POST'")
    assert report(lab)["exclusions"]["missing_historical_roster"] == 1


def test_winter_eastern_kickoff_is_utc_minus_five(lab):
    player(lab)
    lab.execute("UPDATE nfl_schedules SET gameday='2026-12-13'")
    lab.execute("UPDATE espn_projection_history SET synced_at='2026-12-13 17:30:00'")
    lab.execute("UPDATE espn_player_weekly_points SET synced_at='2026-12-15 12:00:00'")
    lab.execute("UPDATE sync_runs SET started_at='2026-12-15 12:00:00',finished_at='2026-12-15 12:01:00'")
    assert report(lab, as_of=datetime(2026, 12, 20, tzinfo=UTC))["overall"]["n"] == 1


def test_identical_snapshot_timestamps_with_conflicting_points_excluded(lab):
    player(lab)
    lab.execute("INSERT INTO espn_projection_history SELECT snapshot_date-1,season,espn_player_id,scoring_period,11,synced_at FROM espn_projection_history")
    assert report(lab)["exclusions"]["conflicting_pregame_snapshots"] == 1


def test_identical_repeated_snapshot_does_not_change_metrics(lab):
    player(lab)
    lab.execute("INSERT INTO espn_projection_history SELECT snapshot_date-1,season,espn_player_id,scoring_period,projected_points,synced_at FROM espn_projection_history")
    assert report(lab)["overall"]["n"] == 1
    assert report(lab)["overall"]["mae"] == 4


def test_ambiguous_dst_kickoff_is_excluded(lab):
    player(lab)
    lab.execute("UPDATE nfl_schedules SET gameday='2026-11-01',gametime='01:30'")
    assert report(lab)["exclusions"]["missing_kickoff"] == 1


def test_completion_cutoff_does_not_assume_an_in_progress_score_is_final(lab):
    player(lab)
    assert report(lab, as_of=KICKOFF+timedelta(hours=5))["exclusions"]["game_not_completed"] == 1


def test_player_mapping_from_other_week_is_not_reused(lab):
    player(lab)
    lab.execute("UPDATE nfl_rosters_weekly SET week=2")
    assert report(lab)["exclusions"]["missing_historical_roster"] == 1


def test_multiple_leagues_same_season_are_not_silently_combined(lab):
    player(lab)
    lab.execute("INSERT INTO leagues (league_id,season,current_week) VALUES (888,2026,3)")
    assert report(lab)["exclusions"]["ambiguous_league_context"] == 1


def test_season_filter_and_determinism(lab):
    player(lab)
    assert report(lab, season=2025)["counts"]["candidate_player_weeks"] == 0
    assert report(lab, season=2026) == report(lab, season=2026)


def test_reaching_sample_floor_is_not_a_claim_of_statistical_confidence(lab):
    player(lab)
    result = report(lab, min_samples=1)
    assert result["overall"]["status"] == "descriptive sample available"
    assert "not a statistical power calculation" in " ".join(result["warnings"])


def test_backtest_executes_on_a_read_only_connection(lab, tmp_path):
    import duckdb
    player(lab)
    lab.close()
    with duckdb.connect(str(tmp_path / 'test.duckdb'), read_only=True) as reader:
        assert report(reader)["overall"]["n"] == 1


def test_gsis_fallback_rejects_conflicting_explicit_weekly_espn_id(lab):
    player(lab)
    lab.execute("UPDATE nfl_rosters_weekly SET espn_id='2'")
    lab.execute("INSERT INTO player_id_map (espn_player_id,gsis_id,match_method,match_confidence) VALUES (1,'G1','espn_id:nfl_players','high')")
    result = report(lab)
    assert result["overall"]["n"] == 0
    assert result["exclusions"]["conflicting_id_crosswalk"] == 1


def test_projection_only_refresh_does_not_verify_partial_actual(lab):
    player(lab, actual=3)
    (archive_root(lab) / 'test_run/cards_00.json').unlink()
    lab.execute("UPDATE espn_player_weekly_points SET synced_at='2026-09-13 19:00:00'")
    assert report(lab)["overall"]["n"] == 0
    lab.execute("UPDATE espn_player_weekly_points SET synced_at='2026-09-15 19:00:00'")
    result = report(lab)
    assert result["overall"]["n"] == 0
    assert result["counts"]["completed_actual_player_weeks"] == 0
    assert result["exclusions"]["unverified_actual_provenance"] == 1


def test_matching_postgame_archive_is_required_even_with_fresh_row(lab):
    player(lab)
    assert report(lab, raw_archive_root=None)["exclusions"]["unverified_actual_provenance"] == 1


@pytest.mark.parametrize('mutation', [
    "UPDATE sync_runs SET status='error'",
    "UPDATE sync_runs SET source='analytics'",
    "UPDATE sync_runs SET started_at='2026-09-13 18:00:00'",
    "UPDATE sync_runs SET finished_at='2026-09-21 12:00:00'",
])
def test_unusable_sync_run_is_not_final_actual_evidence(lab, mutation):
    player(lab)
    lab.execute(mutation)
    assert report(lab)["exclusions"]["unverified_actual_provenance"] == 1


def test_archive_from_open_week_cannot_verify_final_actual(lab):
    player(lab)
    path = archive_root(lab) / 'test_run/league.json'
    path.write_text(json.dumps({'id':999,'seasonId':2026,'scoringPeriodId':1}))
    assert report(lab)["exclusions"]["unverified_actual_provenance"] == 1


def test_archive_actual_must_match_stored_value(lab):
    player(lab)
    lab.execute('UPDATE espn_player_weekly_points SET actual_points=3')
    assert report(lab)["exclusions"]["unverified_actual_provenance"] == 1


def test_other_league_archive_cannot_verify_stored_actual(lab):
    player(lab)
    path = archive_root(lab) / 'test_run/league.json'
    path.write_text(json.dumps({'id':888,'seasonId':2026,'scoringPeriodId':3}))
    assert report(lab)["exclusions"]["unverified_actual_provenance"] == 1


def test_malformed_archive_is_reported_without_raw_content(lab):
    player(lab)
    (archive_root(lab) / 'test_run/cards_00.json').write_text('private content, not JSON')
    result = report(lab)
    assert result['actual_provenance']['unreadable_files'] == 1
    assert result['overall']['n'] == 0
    assert 'private content' not in json.dumps(result)


def test_archive_traversal_run_id_is_not_followed(lab):
    player(lab)
    lab.execute("UPDATE sync_runs SET run_id='../test_run'")
    assert report(lab)['overall']['n'] == 0


def test_archived_roster_actual_can_verify_closed_week(lab):
    player(lab)
    cards = archive_root(lab) / 'test_run/cards_00.json'
    payload = json.loads(cards.read_text())
    cards.unlink()
    roster = {'teams':[{'roster':{'entries':[{'playerPoolEntry':payload['players'][0]}]}}]}
    (archive_root(lab) / 'test_run/roster_01.json').write_text(json.dumps(roster))
    assert report(lab)['overall']['n'] == 1


def test_archived_projection_is_not_actual_evidence(lab):
    player(lab)
    cards = archive_root(lab) / 'test_run/cards_00.json'
    payload = json.loads(cards.read_text())
    payload['players'][0]['player']['stats'][0]['statSourceId'] = 1
    cards.write_text(json.dumps(payload))
    assert report(lab)['exclusions']['unverified_actual_provenance'] == 1


def test_archive_symlink_escaping_root_is_not_read(lab, tmp_path):
    player(lab)
    outside = tmp_path / 'outside.json'
    outside.write_text('private outside content')
    cards = archive_root(lab) / 'test_run/cards_00.json'
    cards.unlink()
    cards.symlink_to(outside)
    result = report(lab)
    assert result['overall']['n'] == 0
    assert result['actual_provenance']['unreadable_files'] == 1
    assert 'private outside content' not in json.dumps(result)


def test_cli_missing_database_is_an_insufficient_report_without_creating_file(tmp_path):
    script = Path(__file__).resolve().parents[2] / 'scripts/backtest_espn_projections.py'
    db = tmp_path / 'absent.duckdb'
    result = subprocess.run([str(Path(__file__).resolve().parents[1] / '.venv/bin/python'), str(script), '--db', str(db)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'insufficient historical observations' in result.stdout
    assert not db.exists()


def test_cli_json_read_only_and_human_report(lab, tmp_path):
    player(lab)
    db = tmp_path / 'test.duckdb'
    # con fixture already uses this path; close writer before starting a reader process.
    lab.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    script = Path(__file__).resolve().parents[2] / 'scripts/backtest_espn_projections.py'
    base = [str(Path(__file__).resolve().parents[1] / '.venv/bin/python'), str(script), '--db', str(db), '--as-of', AS_OF.isoformat(), '--season', '2026']
    result = subprocess.run(base + ['--json'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed['overall']['n'] == 1
    assert parsed['overall']['bias'] == 4
    human = subprocess.run(base, capture_output=True, text=True)
    assert human.returncode == 0, human.stderr
    assert 'MAE' in human.stdout and 'RMSE' in human.stdout and 'actual - projected' in human.stdout
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
