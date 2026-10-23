"""Game splits (issue #210) on the synthetic SPL world (tests/synthetic_splits.py): with/without eligibility, unknown
status excluded by reason (never "without"), byes, a traded teammate, a quarterback change, a partial game, low-n
abstention, an ambiguous roster listing, the look-ahead cutoff, input limits and the HTTP surface. Every row is an
invented test input."""

import duckdb
import pytest
from fastapi.testclient import TestClient

import synthetic_splits as S
from app.analytics import game_splits as gs
from app.analytics.game_splits import SplitsError, SplitsNotFound, build_game_splits, build_split_players, validate
from app.db.connection import connect, init_db


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    init_db(c)
    S.write(c)
    yield c
    c.close()


def split(con, teammate=S.WR2, player=S.SUBJECT, **kw):
    args = {"season_from": S.SEASON, "week_from": None, "season_to": None, "week_to": None, "as_of": None, **kw}
    q = validate(player, teammate, args["season_from"], args["week_from"], args["season_to"], args["week_to"],
                 args["as_of"], S.SEASON)
    return build_game_splits(con, q)


def by_week(out) -> dict[int, dict]:
    return {w["week"]: w for w in out["weeks"]}


# ---------------------------------------------------------------- eligibility --

def test_with_and_without_follow_the_stored_evidence(con):
    out = split(con)
    weeks = by_week(out)
    assert out["status"] == "ok" and out["version"] == "game-splits.v1"
    assert [w for w, r in weeks.items() if r["outcome"] == "with"] == [1, 2, 3, 4, 12]
    assert {w: r["reason"] for w, r in weeks.items() if r["outcome"] == "without"} == {
        5: "inactive", 6: "reserve", 8: "reserve", 9: "reserve"}
    assert out["groups"]["without"]["reasons"] == {"inactive": 1, "reserve": 3}
    # the group's weeks are exactly the drilldown rows marked with that outcome: the counts can be reproduced
    for g in ("with", "without"):
        listed = [{"season": r["season"], "week": r["week"]} for r in out["weeks"] if r["outcome"] == g]
        assert out["groups"][g]["weeks"] == listed and out["counts"][g] == len(listed)
        assert out["groups"][g]["metrics"]["games"] == len(listed)
    assert len(out["weeks"]) == out["window"]["weeks_listed"] == 18
    assert sum(out["counts"][k] for k in ("with", "without", "excluded", "not_counted")) == 18


def test_unknown_status_is_excluded_with_a_reason_and_never_without(con):
    out = split(con)
    weeks = by_week(out)
    assert (weeks[10]["outcome"], weeks[10]["reason"]) == ("excluded", "partial_game")     # 10% vs his usual 85%
    assert weeks[10]["teammate"]["reference_share"] == pytest.approx(0.85)
    assert (weeks[11]["outcome"], weeks[11]["reason"]) == ("excluded", "active_no_record")  # active, no record at all
    assert out["counts"]["excluded_by_reason"] == {"active_no_record": 1, "partial_game": 1}
    assert any("not counted as games without him" in c for c in out["cautions"])
    unknown = {"ambiguous_roster", "active_no_record", "snap_record_missing", "partial_game", "no_roster_record",
               "roster_data_missing"}
    assert unknown.isdisjoint(out["groups"]["without"]["reasons"])


@pytest.mark.parametrize("change, reason", [
    # no row for him that week, although SPL's roster is stored: unknown
    ("DELETE FROM nfl_rosters_weekly WHERE gsis_id = '00-SPLWR02' AND week = 5", "no_roster_record"),
    # no roster data for SPL at all that week: unknown
    ("DELETE FROM nfl_rosters_weekly WHERE team = 'SPL' AND week = 5", "roster_data_missing"),
])
def test_missing_roster_evidence_is_never_an_absence(con, change, reason):
    con.execute(change)
    out = split(con)
    w5 = by_week(out)[5]
    assert (w5["outcome"], w5["reason"]) == ("excluded", reason)
    assert out["counts"]["without"] == 3


def test_an_appearance_without_a_snap_record_cannot_be_with(con):
    con.execute("UPDATE player_week_usage SET offense_pct = NULL, offense_snaps = NULL, stats_source = 'stats_only' "
                "WHERE gsis_id = '00-SPLWR02' AND week = 2")
    w2 = by_week(split(con))[2]
    assert (w2["outcome"], w2["reason"]) == ("excluded", "snap_record_missing")


def test_a_measured_zero_offensive_snaps_is_without(con):
    con.execute("UPDATE player_week_usage SET offense_snaps = 0, offense_pct = 0.0, targets = 0, opportunities = 0, "
                "stats_source = 'snaps_only' WHERE gsis_id = '00-SPLWR02' AND week = 3")
    w3 = by_week(split(con))[3]
    assert (w3["outcome"], w3["reason"]) == ("without", "no_offensive_snaps")


def test_a_bye_week_is_neither_with_nor_without(con):
    out = split(con)
    w7 = by_week(out)[7]
    assert (w7["outcome"], w7["reason"]) == ("not_counted", "bye")
    assert all(x["week"] != 7 for g in ("with", "without") for x in out["groups"][g]["weeks"])
    assert out["counts"]["not_counted_by_reason"]["bye"] == 1


def test_a_traded_teammate_is_without_only_while_on_another_team(con):
    out = split(con, teammate=S.TE)
    weeks = by_week(out)
    assert {w: r["reason"] for w, r in weeks.items() if r["outcome"] == "without"} == {
        1: "other_team", 2: "other_team", 3: "other_team"}
    assert out["counts"]["with"] == 8 and out["teammate"]["teams_with_player"] == ["SPL"]
    assert any("another team" in c for c in out["cautions"])


def test_a_player_who_never_shared_a_team_gives_no_samples(con):
    con.execute("UPDATE nfl_rosters_weekly SET team = 'OTH' WHERE gsis_id = '00-SPLTE01'")
    con.execute("UPDATE player_week_usage SET team = 'OTH' WHERE gsis_id = '00-SPLTE01'")
    out = split(con, teammate=S.TE)
    assert out["counts"]["with"] == out["counts"]["without"] == 0
    assert out["counts"]["not_counted_by_reason"]["not_teammates"] == 11
    assert out["status"] == "low_n" and out["differences"] is None


def test_a_quarterback_change_is_shown_as_environment_not_hidden(con):
    out = split(con, teammate=S.QB2)
    assert (out["counts"]["with"], out["counts"]["without"]) == (5, 6)
    assert by_week(out)[1]["reason"] == "inactive"                       # the backup was inactive while QB1 started
    env = out["groups"]
    assert [q["name"] for q in env["with"]["environment"]["primary_qbs"]] == ["Split Backup"]
    assert [q["name"] for q in env["without"]["environment"]["primary_qbs"]] == ["Split Starter"]
    assert any("More than one quarterback" in c for c in out["cautions"])
    assert out["teammate"]["position"] == "QB"


def test_environment_and_metrics_are_position_appropriate(con):
    out = split(con)
    keys = [m["key"] for m in out["metrics"]]
    assert "air_yards_share" in keys and "backfield_rush_share" not in keys
    w = out["groups"]["with"]
    assert w["metrics"]["routes_pg"] is not None and w["environment"]["home_games"] == 5
    assert w["environment"]["team_points_pg"] == pytest.approx((21 + 22 + 23 + 24 + 32) / 5)
    assert set(out["differences"]) == set(keys)
    assert out["differences"]["targets_pg"] == pytest.approx(
        out["groups"]["without"]["metrics"]["targets_pg"] - w["metrics"]["targets_pg"], abs=1e-4)


# ---------------------------------------------------------------- abstention --

def test_low_n_withholds_differences(con):
    out = split(con, week_to=6)          # weeks 1-6: four with, two without
    assert out["status"] == "low_n" and out["differences"] is None
    assert out["abstention"]["reason"] == "low_n" and "with: 4, without: 2" in out["abstention"]["message"]
    assert out["groups"]["without"]["metrics"]["games"] == 2         # the groups themselves are still shown


def test_no_probability_forecast_or_causal_claim_in_the_output(con):
    text = str(split(con)).lower()
    for banned in ("probability", "confidence", "p-value", "significan", "will score", "projection", "causes",
                   "because of him"):
        assert banned not in text, banned
    assert any("not a causal" in c for c in gs.CONFOUNDING) and any("not a forecast" in c for c in gs.CONFOUNDING)


# --------------------------------------------------------- identity, cutoff --

def test_an_ambiguous_roster_listing_is_excluded(con):
    con.execute("INSERT INTO nfl_rosters_weekly VALUES (2025, 5, 'REG', 'OTH', '00-SPLWR02', 'WR', 'ACT', 'x')")
    w5 = by_week(split(con))[5]
    assert (w5["outcome"], w5["reason"]) == ("excluded", "ambiguous_roster")


def test_an_appearance_resolves_a_double_listing(con):
    con.execute("INSERT INTO nfl_rosters_weekly VALUES (2025, 2, 'REG', 'OTH', '00-SPLWR02', 'WR', 'INA', 'x')")
    assert by_week(split(con))[2]["outcome"] == "with"


def test_an_ambiguous_espn_mapping_gives_no_profile_link(con):
    con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_method, match_confidence) VALUES "
                "(901, '00-SPLWR01', 'x', 'high'), (902, '00-SPLWR01', 'x', 'high'), (903, '00-SPLWR02', 'x', 'high')")
    out = split(con)
    assert (out["player"]["espn_player_id"], out["player"]["mapping_status"]) == (None, "ambiguous")
    assert (out["teammate"]["espn_player_id"], out["teammate"]["mapping_status"]) == (903, "verified")


def test_the_cutoff_reads_nothing_after_it(con):
    before = split(con, as_of="2025-10-01")          # weeks 1-4 are played before 1 October
    assert [r["week"] for r in before["weeks"] if r["outcome"] == "with"] == [1, 2, 3, 4]
    assert all(r["reason"] == "after_cutoff" for r in before["weeks"] if 5 <= r["week"] <= 12 and r["week"] != 7)
    # rewriting every later game (a trade, new snap shares, other statuses) leaves the answer unchanged
    con.execute("UPDATE player_week_usage SET offense_pct = 0.05, team = 'OTH' WHERE week >= 5")
    con.execute("UPDATE nfl_rosters_weekly SET status = 'RES', team = 'OTH' WHERE week >= 5")
    assert split(con, as_of="2025-10-01") == before


def test_the_partial_game_reference_uses_eligible_games_only(con):
    # Before week 12 is played, week 10's reference rests on weeks 1-4 and 10: still a partial game.
    out = split(con, as_of="2025-11-20")
    assert by_week(out)[10]["teammate"]["reference_games"] == 5
    # Only two earlier games with a snap record: too few to call week 10 partial, so it counts as with.
    con.execute("UPDATE player_week_usage SET offense_pct = NULL, offense_snaps = NULL, stats_source = 'stats_only' "
                "WHERE gsis_id = '00-SPLWR02' AND week IN (1, 2, 3)")
    w10 = by_week(split(con, as_of="2025-11-20"))[10]
    assert w10["teammate"]["reference_games"] == 2 and w10["outcome"] == "with"


def test_the_partial_game_reference_ignores_games_the_player_did_not_count(con):
    # Without xFP for the player in weeks 1-3, those games are not samples, so the teammate's snap shares there must
    # not feed the baseline: only weeks 4 and 10 remain (two games), too few to call week 10 partial. Counting the
    # out-of-sample weeks would give five games and exclude week 10 as partial.
    con.execute("UPDATE player_week_usage SET xfp = NULL WHERE gsis_id = '00-SPLWR01' AND week IN (1, 2, 3)")
    out = split(con, as_of="2025-11-20")
    w10 = by_week(out)[10]
    assert [by_week(out)[w]["reason"] for w in (1, 2, 3)] == ["subject_metrics_missing"] * 3
    assert w10["teammate"]["reference_games"] == 2 and w10["teammate"]["reference_share"] == pytest.approx(0.475)
    assert w10["outcome"] == "with"
    # the same holds when the player missed the games outright
    con.execute("DELETE FROM player_week_usage WHERE gsis_id = '00-SPLWR01' AND week IN (1, 2, 3)")
    assert by_week(split(con, as_of="2025-11-20"))[10]["teammate"]["reference_games"] == 2


def test_a_game_that_is_not_final_is_not_counted(con):
    con.execute("UPDATE nfl_schedules SET home_score = NULL, away_score = NULL WHERE week = 12 AND home_team = 'SPL'")
    w12 = by_week(split(con))[12]
    assert (w12["outcome"], w12["reason"]) == ("not_counted", "not_final")


def test_results_and_cache_keys_are_deterministic(con):
    a, b = split(con), split(con)
    assert a == b
    assert a["cache_key"] == ("game-splits.v1|player=00-SPLWR01|teammate=00-SPLWR02|from=2025w1|to=2025w18|as_of=-")
    assert split(con, as_of="2025-10-01")["cache_key"].endswith("as_of=2025-10-01")


# ---------------------------------------------------------------- validation --

@pytest.mark.parametrize("args, message", [
    (("bad id!", None, 2025, None, None, None, None), "player"),
    ((S.SUBJECT, "x", 2025, None, None, None, None), "teammate"),
    ((S.SUBJECT, S.SUBJECT, 2025, None, None, None, None), "different"),
    ((S.SUBJECT, S.WR2, 2022, None, 2025, None, None), "at most 3 seasons"),
    ((S.SUBJECT, S.WR2, 2025, 0, None, None, None), "week_from"),
    ((S.SUBJECT, S.WR2, 2025, None, None, 19, None), "week_to"),
    ((S.SUBJECT, S.WR2, 2025, 10, 2025, 9, None), "starts after"),
    ((S.SUBJECT, S.WR2, 2025, None, None, None, "10/01/2025"), "YYYY-MM-DD"),
    ((S.SUBJECT, S.WR2, 2025, None, None, None, "2025-02-30"), "valid date"),
])
def test_validation(args, message):
    with pytest.raises(SplitsError, match=message):
        validate(*args, 2025)


def test_defaults_and_multi_season_windows():
    q = validate(S.SUBJECT, None, None, None, None, None, None, 2025)
    assert (q.season_from, q.week_from, q.season_to, q.week_to) == (2025, 1, 2025, 18)
    q = validate(S.SUBJECT, None, 2023, 10, 2025, 4, None, 2025)
    assert q.contains(2024, 1) and not q.contains(2023, 9) and not q.contains(2025, 5)
    with pytest.raises(SplitsError, match="season must be given"):
        validate(S.SUBJECT, None, None, None, None, None, None, None)


def test_unknown_players_and_positions(con):
    with pytest.raises(SplitsNotFound):
        split(con, player="00-NOBODY1")
    with pytest.raises(SplitsNotFound):
        split(con, teammate="00-NOBODY2")
    with pytest.raises(SplitsError, match="WR, RB and TE"):
        split(con, player=S.QB1, teammate=S.WR2)


def test_teammate_options_and_player_list(con):
    out = split(con, teammate=None)
    assert out["status"] == "needs_teammate"
    assert [o["gsis_id"] for o in out["teammate_options"]] == [S.QB1, S.QB2, S.WR2, S.TE]
    players = build_split_players(con, S.SEASON)
    assert [p["gsis_id"] for p in players["players"]] == [S.SUBJECT, S.TE, S.WR2]   # by name
    assert players["seasons"] == [2025]


def test_missing_tables_answer_no_data():
    c = duckdb.connect(":memory:")
    init_db(c)
    S.write(c)
    c.execute("DROP TABLE nfl_rosters_weekly")
    out = split(c)
    assert out["status"] == "no_data" and out["missing_tables"] == ["nfl_rosters_weekly"]
    assert build_split_players(c, 2025)["status"] == "no_data"


# ---------------------------------------------------------------------- HTTP --

@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "splits.duckdb"
    c = connect(path)
    init_db(c)
    S.write(c)
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    return TestClient(app)


def test_api_answers_and_limits(client):
    base = {"player": S.SUBJECT, "teammate": S.WR2, "season_from": 2025}
    ok = client.get("/api/research/game-splits", params=base)
    assert ok.status_code == 200 and ok.json()["counts"]["with"] == 5
    assert client.get("/api/research/game-splits", params={"player": S.SUBJECT}).json()["status"] == "needs_teammate"
    for bad in [{"player": "x"}, {**base, "week_to": 19}, {**base, "season_from": 1990}, {**base, "as_of": "2025-1-1"},
                {**base, "teammate": S.SUBJECT}, {**base, "season_from": 2020, "season_to": 2025},
                {**base, "player": "a" * 21}, {}]:
        assert client.get("/api/research/game-splits", params=bad).status_code == 422, bad
    assert client.get("/api/research/game-splits", params={**base, "player": "00-NOBODY1"}).status_code == 404
    players = client.get("/api/research/game-splits/players", params={"season": 2025})
    assert players.status_code == 200 and len(players.json()["players"]) == 3
    assert client.get("/api/research/game-splits/players", params={"season": 1}).status_code == 422


@pytest.mark.parametrize("status", [None, "", "  "])
def test_a_blank_roster_status_is_unknown_never_without(con, status):
    con.execute("UPDATE nfl_rosters_weekly SET status = ? WHERE gsis_id = ? AND week = 5", [status, S.WR2])
    w5 = by_week(split(con))[5]
    assert (w5["outcome"], w5["reason"]) == ("excluded", "roster_record_incomplete")


def test_a_roster_row_without_a_team_is_unknown_and_does_not_crash(con):
    # beside the INA row: a NULL status next to a real one used to raise in sorted(); a NULL team read as other_team
    con.execute("INSERT INTO nfl_rosters_weekly VALUES (2025, 5, 'REG', 'SPL', '00-SPLWR02', 'WR', NULL, 'x')")
    con.execute("INSERT INTO nfl_rosters_weekly VALUES (2025, 6, 'REG', NULL, '00-SPLWR02', 'WR', 'ACT', 'x')")
    con.execute("UPDATE nfl_rosters_weekly SET team = NULL WHERE gsis_id = ? AND week = 8", [S.WR2])
    con.execute("INSERT INTO nfl_rosters_weekly VALUES (2025, 9, 'REG', NULL, '00-SPLWR01', 'WR', 'ACT', 'x')")
    weeks = by_week(split(con))
    for w in (5, 6, 8):
        assert (weeks[w]["outcome"], weeks[w]["reason"]) == ("excluded", "roster_record_incomplete"), w
    assert weeks[9]["outcome"] == "without"           # the subject's own NULL-team row does not move his team


def test_a_lowercase_status_is_read_as_the_code(con):
    con.execute("UPDATE nfl_rosters_weekly SET status = ' ina ' WHERE gsis_id = ? AND week = 5", [S.WR2])
    w5 = by_week(split(con))[5]
    assert (w5["outcome"], w5["reason"]) == ("without", "inactive")


def test_group_counts_and_metrics_read_the_same_games(con):
    # a week listed at another position is not in the WR/RB/TE metric rows, so it cannot be in the group count
    con.execute("UPDATE player_week_usage SET position = 'QB' WHERE gsis_id = ? AND week = 1", [S.SUBJECT])
    out = split(con)
    assert by_week(out)[1]["reason"] == "subject_position_not_covered"
    for g in ("with", "without"):
        assert out["groups"][g]["games"] == out["groups"][g]["metrics"]["games"]


def test_the_stored_through_week_respects_the_cutoff(con):
    assert split(con)["window"]["usage_through_week"] == 12
    assert split(con, as_of="2025-10-01")["window"]["usage_through_week"] == 4
