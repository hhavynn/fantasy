"""NFL data contract: PBP filters, pressure, FTN, injuries, lines, context keys, validation."""

import polars as pl
import pytest

from app.analytics.availability import latest_injuries
from app.analytics.context import implied_totals, rank_generous, summarize_defense, summarize_team
from app.analytics.validation import (check_lines, check_pbp_player, check_target_share_sums, check_team_week,
                                      check_usage, describe_ftn_read_codes)
from app.db.connection import SCHEMA_VERSION, init_db, schema_version
from app.nfl.sync import derive_pbp

COLS = ["season_type", "week", "game_id", "play_id", "posteam", "defteam", "play_type", "two_point_attempt",
        "qb_kneel", "qb_scramble", "pass_attempt", "sack", "qb_hit", "qb_dropback", "pass", "rusher_player_id",
        "receiver_player_id", "yardline_100", "yards_gained", "success", "epa", "pass_oe", "wp",
        "half_seconds_remaining"]


def play(i, **kw):
    base = dict(season_type="REG", week=1, game_id="G1", play_id=float(i), posteam="PHI", defteam="DAL",
                play_type="run", two_point_attempt=0.0, qb_kneel=0.0, qb_scramble=0.0, pass_attempt=0.0,
                sack=0.0, qb_hit=0.0, qb_dropback=0.0, **{"pass": 0.0}, rusher_player_id=None,
                receiver_player_id=None, yardline_100=50.0, yards_gained=3.0, success=0.0, epa=0.1,
                pass_oe=None, wp=0.5, half_seconds_remaining=900.0)
    base.update(kw)
    return base


def rb_run(i, yl=50.0, **kw):
    return play(i, rusher_player_id="RB1", yardline_100=yl, **kw)


def pbp_frame(plays):
    return pl.DataFrame(plays, schema={c: (pl.String if c in ("season_type", "game_id", "posteam", "defteam",
                                                             "play_type", "rusher_player_id", "receiver_player_id")
                                           else pl.Float64) for c in COLS})


POS = {"RB1": "RB", "RB2": "RB", "QB1": "QB", "WR1": "WR", "TE1": "TE"}


def derive(plays, ftn=None):
    pr, tr, dr = derive_pbp(pbp_frame(plays), 2026, POS, ftn=ftn)
    return {r["player_id"]: r for r in pr}, {r["team"]: r for r in tr}, {r["defteam"]: r for r in dr}


def sample_plays():
    return [
        rb_run(1), rb_run(2, yl=4.0, success=1.0), rb_run(3, yl=15.0, yards_gained=12.0, success=1.0),
        play(4, rusher_player_id="RB2"),
        play(5, rusher_player_id="QB1", qb_scramble=1.0, qb_dropback=1.0, yardline_100=3.0, **{"pass": 1.0}),
        play(6, play_type="qb_kneel", qb_kneel=1.0, rusher_player_id="QB1"),
        play(7, rusher_player_id="RB1", two_point_attempt=1.0, yardline_100=2.0),
        play(8, play_type="pass", pass_attempt=1.0, qb_dropback=1.0, receiver_player_id="WR1",
             yardline_100=18.0, **{"pass": 1.0}),
        play(9, play_type="pass", pass_attempt=1.0, qb_dropback=1.0, sack=1.0, qb_hit=1.0, **{"pass": 1.0}),
        play(10, play_type="pass", pass_attempt=1.0, qb_dropback=1.0, qb_hit=1.0, receiver_player_id="TE1",
             **{"pass": 1.0}),
        play(11, play_type="pass", pass_attempt=1.0, qb_dropback=1.0, receiver_player_id="RB1", **{"pass": 1.0}),
    ]


def test_kneels_and_two_point_tries_are_excluded():
    players, teams, _ = derive(sample_plays())
    assert players["RB1"]["rush_attempts"] == 3          # 2-pt run excluded
    assert "QB1" in players and players["QB1"]["rush_attempts"] == 1   # the kneel is not a rush
    assert teams["PHI"]["team_rush_attempts"] == 5       # 3 RB1 + 1 RB2 + 1 scramble; no kneel, no 2-pt


def test_team_rush_share_vs_backfield_share_and_scrambles():
    players, teams, _ = derive(sample_plays())
    t = teams["PHI"]
    assert t["team_scrambles"] == 1 and t["team_designed_rushes"] == 4
    assert t["team_rb_carries"] == 4                     # the QB scramble never enters the backfield
    assert players["RB1"]["rush_attempts"] / t["team_rush_attempts"] == pytest.approx(3 / 5)
    assert players["RB1"]["designed_rushes"] / t["team_rb_carries"] == pytest.approx(3 / 4)
    assert players["QB1"]["scrambles"] == 1 and players["QB1"]["designed_rushes"] == 0


def test_goal_line_and_red_zone_ignore_scrambles():
    players, teams, _ = derive(sample_plays())
    assert players["RB1"]["gl_carries"] == 1 and players["RB1"]["rz_carries"] == 2
    assert players["QB1"]["gl_carries"] == 0             # scramble at the 3 is not goal-line RB work
    assert teams["PHI"]["team_rz_carries"] == 2
    assert players["RB1"]["explosive_runs"] == 1 and players["RB1"]["designed_rush_success"] == 2


def test_hit_or_sack_never_double_counts():
    _, teams, _ = derive(sample_plays())
    t = teams["PHI"]
    # dropbacks: scramble, target, sack(+hit), hit, target = 5; the sack also has qb_hit = 1
    assert t["dropbacks"] == 5 and t["sacks"] == 1
    assert t["hit_or_sack"] == 2                         # not 3 (qb_hit + sack would be 3)
    assert t["hit_or_sack_rate"] == pytest.approx(2 / 5) and t["sack_rate"] == pytest.approx(1 / 5)


def test_targets_and_red_zone_targets():
    players, teams, _ = derive(sample_plays())
    assert teams["PHI"]["team_targets"] == 3             # the sack is not a target
    assert players["WR1"]["rz_targets"] == 1 and players["TE1"]["targets"] == 1


def test_defense_aggregates_use_defteam_week_keys():
    plays = sample_plays() + [rb_run(20, posteam="DAL", defteam="PHI", week=1, game_id="G1"),
                              rb_run(21, posteam="PHI", defteam="NYG", week=2, game_id="G2", epa=-0.5)]
    _, teams, defense = derive(plays)
    assert set(defense) == {"DAL", "PHI", "NYG"}
    assert defense["DAL"]["opponent"] == "PHI" and defense["PHI"]["rush_plays"] == 1
    assert defense["DAL"]["rb_targets"] == 1 and defense["DAL"]["te_targets"] == 1 and defense["DAL"]["wr_targets"] == 1
    # PHI offense appears in two different weeks -> two team-week rows, never merged
    _, tr, _ = derive_pbp(pbp_frame(plays), 2026, POS)
    assert sorted((r["team"], r["week"]) for r in tr if r["team"] == "PHI") == [("PHI", 1), ("PHI", 2)]


def _ftn(codes):
    return pl.DataFrame({"nflverse_game_id": ["G1"] * len(codes), "nflverse_play_id": [8, 10, 11][:len(codes)],
                         "read_thrown": codes},
                        schema={"nflverse_game_id": pl.String, "nflverse_play_id": pl.Int32, "read_thrown": pl.String})


def test_ftn_raw_read_codes_are_counted_without_semantic_names():
    players, teams, _ = derive(sample_plays(), ftn=_ftn(["1", "DES", "0"]))
    assert players["WR1"]["ftn_read1_targets"] == 1 and teams["PHI"]["team_ftn_read1_targets"] == 1
    assert players["TE1"]["designed_targets"] == 1
    assert players["RB1"]["ftn_read0_targets"] == 1            # "0" is kept as a raw code, not dropped as "uncoded"
    assert players["RB1"]["ftn_charted_targets"] == 1           # every coded target counts as charted
    assert not any("first_read" in k for r in players.values() for k in r)   # no authoritative "first read" naming
    assert not any("first_read" in k for k in teams["PHI"])


def test_ftn_uncharted_games_are_null_not_zero():
    players, teams, _ = derive(sample_plays(), ftn=None)
    assert players["WR1"]["ftn_read1_targets"] is None and teams["PHI"]["team_ftn_read1_targets"] is None
    assert players["WR1"]["ftn_read0_targets"] is None and players["TE1"]["designed_targets"] is None


def test_ftn_semantics_flagged_unverified():
    from app.nfl.sync import FTN_ASSUMED_PRIMARY_READ_CODE, FTN_READ_SEMANTICS_STATUS, FTN_SEMANTICS_REFERENCE
    assert FTN_READ_SEMANTICS_STATUS == "UNVERIFIED" and FTN_ASSUMED_PRIMARY_READ_CODE == "1"
    assert FTN_SEMANTICS_REFERENCE.endswith("nflreadr/issues/325")


def test_ftn_validation_is_descriptive_not_a_semantic_assertion():
    # Either distribution must produce the same kind of note: never an error, never "verified".
    for counts in ({"0": 17, "1": 560, "2": 130, "DES": 100, "CHK": 130, "SD": 63},
                   {"0": 560, "1": 17, "2": 130, "DES": 100, "CHK": 130, "SD": 63}):
        issues = describe_ftn_read_codes(counts)
        assert len(issues) == 1 and issues[0].severity == "warning"
        assert issues[0].check_name == "ftn_read_thrown_semantics_unresolved"
        assert "UNVERIFIED" in issues[0].detail and "issues/325" in issues[0].detail
        assert "verified" not in issues[0].detail.replace("UNVERIFIED", "")
        assert issues[0].examples[0]["1"] == pytest.approx(counts["1"] / sum(counts.values()), abs=1e-3)
    assert describe_ftn_read_codes({}) == []


def test_ftn_code_counts_cannot_exceed_targets():
    p = {"season": 2026, "week": 1, "player_id": "p", "gl_carries": 0, "rz_carries": 0, "designed_rushes": 0,
         "rush_attempts": 0, "rz_targets": 0, "targets": 3, "ftn_charted_targets": 3, "ftn_read0_targets": 0,
         "ftn_read1_targets": 3, "ftn_read2_targets": 1, "designed_targets": 0, "checkdown_targets": 0,
         "scramble_drill_targets": 0}
    assert {e.check_name for e in check_pbp_player([p])} == {"ftn_codes_gt_targets"}


def test_latest_same_week_injury_report_wins(con):
    con.execute("""CREATE TABLE nfl_injuries (season INT, week INT, gsis_id VARCHAR, report_status VARCHAR,
                   report_primary_injury VARCHAR, practice_status VARCHAR, date_modified TIMESTAMP)""")
    con.execute("""INSERT INTO nfl_injuries VALUES
        (2026, 6, 'P1', 'Questionable', 'Ankle', 'Limited Participation in Practice', '2026-10-15 16:00:00'),
        (2026, 6, 'P1', 'Out',          'Ankle', 'Did Not Participate In Practice', '2026-10-16 16:00:00'),
        (2026, 6, 'P1', NULL,           'Ankle', 'Full Participation in Practice',  '2026-10-14 16:00:00'),
        (2026, 5, 'P1', 'Doubtful',     'Ankle', 'Did Not Participate In Practice', '2026-10-30 16:00:00')""")
    latest = latest_injuries(con, 2026)
    assert latest["P1"]["week"] == 6 and latest["P1"]["report_status"] == "Out"
    per_week = latest_injuries(con, 2026, per_week=True)
    assert per_week[("P1", 5)]["report_status"] == "Doubtful"


def test_injury_ties_without_timestamp_are_deterministic(con):
    con.execute("""CREATE TABLE nfl_injuries (season INT, week INT, gsis_id VARCHAR, report_status VARCHAR,
                   report_primary_injury VARCHAR, practice_status VARCHAR)""")
    con.execute("""INSERT INTO nfl_injuries VALUES (2026, 6, 'P1', 'Questionable', 'Knee', 'Limited'),
                                                   (2026, 6, 'P1', 'Doubtful', 'Knee', 'DNP')""")
    assert latest_injuries(con, 2026)["P1"]["report_status"] == "Doubtful"   # conservative tie-break


def test_implied_totals_spread_sign():
    home, away = implied_totals(44.5, 3.0)        # SEA (home) -3 vs NE: positive spread = home favored
    assert (home, away) == (23.75, 20.75) and home > away
    home, away = implied_totals(47.5, -3.0)       # away favored
    assert away > home and home + away == 47.5
    assert implied_totals(None, 3.0) == (None, None)


def test_implied_total_validation():
    good = [{"game_id": "g", "is_home": True, "spread_line": 3.0, "total_line": 44.5, "implied_total": 23.75},
            {"game_id": "g", "is_home": False, "spread_line": 3.0, "total_line": 44.5, "implied_total": 20.75}]
    assert check_lines(good) == []
    bad = [dict(good[0], implied_total=20.75), dict(good[1], implied_total=23.75)]   # sign flipped
    assert check_lines(bad)[0].severity == "error"


def test_team_and_defense_summaries_are_sums_not_means_of_rates():
    rows = [{"proe": 10.0, "proe_plays": 10, "neutral_pass_rate": 0.6, "neutral_plays": 10, "sacks": 1,
             "dropbacks": 10, "hit_or_sack": 2, "pfr_pressures": None},
            {"proe": -10.0, "proe_plays": 30, "neutral_pass_rate": 0.4, "neutral_plays": 30, "sacks": 4,
             "dropbacks": 40, "hit_or_sack": 8, "pfr_pressures": 12}]
    s = summarize_team(rows)
    assert s["proe"] == pytest.approx(-5.0) and s["sack_rate"] == pytest.approx(5 / 50)
    assert s["pfr_pressure_rate"] == pytest.approx(12 / 40)   # weeks without PFR excluded from the denominator
    d = summarize_defense([{"pass_plays": 30, "pass_epa_sum": 3.0, "rush_plays": 20, "rush_epa_sum": 2.0,
                            "rush_success": 10, "explosive_runs": 2, "targets": 25, "rb_targets": 5, "te_targets": 5},
                           {"pass_plays": 10, "pass_epa_sum": -1.0, "rush_plays": 0, "rush_epa_sum": 0.0,
                            "rush_success": 0, "explosive_runs": 0, "targets": 5, "rb_targets": 1, "te_targets": 0}])
    assert d["pass_epa_allowed"] == pytest.approx(2 / 40) and d["rb_target_share_allowed"] == pytest.approx(0.2)
    assert d["games"] == 2
    assert rank_generous({"A": 0.1, "B": -0.2, "C": None}) == {"A": 1, "B": 2, "C": None}


def test_team_target_shares_sum_to_one():
    assert check_target_share_sums([{"team": "PHI", "week": 1, "share_sum": 1.0}]) == []
    assert check_target_share_sums([{"team": "PHI", "week": 1, "share_sum": 0.8}])[0].severity == "warning"


def test_hard_validation_failures():
    errs = check_usage([{"season": 2026, "week": 1, "gsis_id": "a", "offense_pct": 1.2, "target_share": 0.2,
                         "receptions": 5, "targets": 4}])
    assert {e.check_name for e in errs} == {"offense_pct_range", "receptions_gt_targets"}
    assert all(e.severity == "error" for e in errs)
    dup = check_usage([{"season": 2026, "week": 1, "gsis_id": "a"}] * 2)
    assert dup[0].check_name == "usage_duplicate_keys"
    p = {"season": 2026, "week": 1, "player_id": "p", "gl_carries": 3, "rz_carries": 2, "designed_rushes": 2,
         "rush_attempts": 2, "rz_targets": 0, "targets": 0, "ftn_charted_targets": None}
    assert {e.check_name for e in check_pbp_player([p])} == {"gl_gt_rz_carries"}
    t = {"season": 2026, "week": 1, "team": "PHI", "hit_or_sack": 6, "sacks": 1, "dropbacks": 5,
         "sack_rate": 0.2, "hit_or_sack_rate": 1.2, "team_rb_carries": 1, "team_designed_rushes": 2,
         "proe": 0, "proe_plays": 50, "offense_plays": 60}
    names = {e.check_name for e in check_team_week([t])}
    assert {"hit_or_sack_bounds", "pressure_rate_range"} <= names


def test_strange_values_are_warnings_not_errors():
    w = check_usage([{"season": 2026, "week": 1, "gsis_id": "a", "xfp": -1.0, "air_yards_share": 1.4}])
    assert w and all(i.severity == "warning" for i in w)


def test_schema_v2_to_v3_rebuilds_renamed_ftn_tables(tmp_path):
    from app.db.connection import connect
    c = connect(tmp_path / "v2.duckdb")
    c.execute("CREATE TABLE schema_meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
    c.execute("INSERT INTO schema_meta VALUES ('schema_version', '2')")
    c.execute("CREATE TABLE nfl_pbp_player_week (season INT, week INT, player_id VARCHAR, first_read_targets INT)")
    c.execute("CREATE TABLE manager_metrics (league_id BIGINT, x INT)")   # unchanged in v3: must survive
    init_db(c)
    cols = [r[0] for r in c.execute("SELECT column_name FROM information_schema.columns "
                                    "WHERE table_name='nfl_pbp_player_week'").fetchall()]
    assert "ftn_read1_targets" in cols and "first_read_targets" not in cols
    assert "x" in [r[0] for r in c.execute("SELECT column_name FROM information_schema.columns "
                                           "WHERE table_name='manager_metrics'").fetchall()]
    assert schema_version(c) == 3
    c.close()


def test_schema_migration_rebuilds_only_derived_tables(tmp_path):
    from app.db.connection import connect
    c = connect(tmp_path / "old.duckdb")
    c.execute("CREATE TABLE player_week_usage (season INT, week INT, gsis_id VARCHAR, rush_share DOUBLE)")
    c.execute("CREATE TABLE leagues (league_id BIGINT, season INT)")
    c.execute("INSERT INTO leagues VALUES (1, 2026)")
    init_db(c)
    cols = [r[0] for r in c.execute("SELECT column_name FROM information_schema.columns "
                                    "WHERE table_name='player_week_usage'").fetchall()]
    assert "backfield_rush_share" in cols and "rush_share" not in cols
    assert c.execute("SELECT count(*) FROM leagues").fetchone()[0] == 1      # raw data kept
    assert schema_version(c) == SCHEMA_VERSION
    c.close()
