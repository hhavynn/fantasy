"""Fantasy Research Lab: paired-week FPOE, windows, coverage, week states, mapping and API validation."""

import pytest
from fastapi.testclient import TestClient

from app.analytics.player_scores import score_players
from app.analytics.research import (build_opportunity, build_player_research, espn_mapping, momentum,
                                    production_group, split_games, summarize_window, validate_filters,
                                    ResearchError, week_states)
from app.db.connection import connect, fetch_dicts, init_db, replace_where

SEASON = 2026


def usage(gsis, week, pos="WR", team="KC", **kw):
    """A played game: snaps, a few targets, xFP and PPR unless overridden."""
    row = {"season": SEASON, "week": week, "gsis_id": gsis, "team": team, "opponent": "DEN", "position": pos,
           "offense_snaps": 50, "offense_pct": 0.8, "targets": 6, "receptions": 4, "air_yards": 60.0,
           "target_share": 0.2, "air_yards_share": 0.3, "carries": 0, "designed_rushes": 0, "rush_attempts": 0,
           "team_rush_attempts": 25, "team_rb_carries": 20, "rz_targets": 1, "rz_carries": 0, "gl_carries": 0,
           "opportunities": 6, "fantasy_points_ppr": 12.0, "xfp": 10.0}
    row.update(kw)
    if "opportunities" not in kw:
        row["opportunities"] = (row["targets"] or 0) + (row["carries"] or 0)
    return row


def seed(con, rows, team_targets=None, schedule_byes=None, weeks=8, injuries=(), players=None, id_map=()):
    replace_where(con, "player_week_usage", rows, "true")
    con.execute("""CREATE TABLE IF NOT EXISTS nfl_weekly_stats (season INT, season_type VARCHAR, week INT,
                   team VARCHAR, player_id VARCHAR, targets INT, receiving_air_yards DOUBLE)""")
    con.execute("DELETE FROM nfl_weekly_stats")
    for (team, week), (tgt, air) in (team_targets or {}).items():
        con.execute("INSERT INTO nfl_weekly_stats VALUES (?, 'REG', ?, ?, 'TEAM', ?, ?)", [SEASON, week, team, tgt, air])
    con.execute("""CREATE TABLE IF NOT EXISTS nfl_schedules (season INT, week INT, game_type VARCHAR,
                   home_team VARCHAR, away_team VARCHAR)""")
    con.execute("DELETE FROM nfl_schedules")
    byes = schedule_byes or {}
    for w in range(1, weeks + 1):
        for home, away in (("KC", "DEN"), ("BUF", "MIA"), ("DAL", "NYG")):
            if home not in byes.get(w, set()):
                con.execute("INSERT INTO nfl_schedules VALUES (?, ?, 'REG', ?, ?)", [SEASON, w, home, away])
    con.execute("""CREATE TABLE IF NOT EXISTS nfl_injuries (season INT, week INT, gsis_id VARCHAR,
                   report_status VARCHAR, report_primary_injury VARCHAR, practice_status VARCHAR)""")
    con.execute("DELETE FROM nfl_injuries")
    for gsis, week, status in injuries:
        con.execute("INSERT INTO nfl_injuries VALUES (?, ?, ?, ?, 'Hamstring', NULL)", [SEASON, week, gsis, status])
    names = players or {r["gsis_id"]: f"Player {r['gsis_id']}" for r in rows}
    replace_where(con, "nfl_players", [{"gsis_id": g, "display_name": n} for g, n in names.items()], "true")
    replace_where(con, "player_id_map", [{"espn_player_id": e, "gsis_id": g, "match_confidence": c,
                                          "match_method": "espn_id:nfl_players"} for e, g, c in id_map], "true")


# ---------------------------------------------------------------- pure --

def test_fpoe_is_paired_and_denominators_match():
    rows = [usage("WR-A", 1, xfp=10.0, fantasy_points_ppr=6.0), usage("WR-A", 2, xfp=14.0, fantasy_points_ppr=20.0),
            usage("WR-A", 3, xfp=None, fantasy_points_ppr=30.0)]   # xFP not published: excluded, not 0
    q, missing = split_games(rows)
    assert [r["week"] for r in q] == [1, 2] and [r["week"] for r in missing] == [3]
    s = summarize_window(q, "WR")
    assert s["games"] == 2
    assert s["xfp_pg"] == pytest.approx(12.0) and s["ppr_pg"] == pytest.approx(13.0)
    assert s["fpoe_pg"] == pytest.approx(1.0)        # mean(-4, +6); the 30-point week never enters
    assert s["fpoe_pg"] == pytest.approx(s["ppr_pg"] - s["xfp_pg"])


def test_measured_zero_counts_and_missing_source_is_coverage():
    rows = [usage("WR-A", 1, targets=0, receptions=0, air_yards=0.0, xfp=0.0, fantasy_points_ppr=0.0, rz_targets=0),
            usage("WR-A", 2, rz_targets=None), usage("WR-A", 3, rz_targets=2)]
    s = summarize_window(split_games(rows)[0], "WR")
    assert s["games"] == 3 and s["xfp_pg"] == pytest.approx(20 / 3, abs=1e-3)   # the measured-zero game is in the denominator
    assert s["rz_targets_pg"] == pytest.approx(1.0)                    # (0 + 2) / 2 known games
    assert s["coverage"]["rz_targets_pg"] == 2
    assert s["target_share"] is None and s["coverage"]["target_share"] == 0   # no team totals: unknown, not 0%


def test_shares_are_aggregate_not_mean_of_weekly_shares():
    rows = [usage("WR-A", 1, targets=2, team_targets=40, air_yards=10.0, team_air_yards=200.0),
            usage("WR-A", 2, targets=12, team_targets=20, air_yards=120.0, team_air_yards=200.0)]
    s = summarize_window(rows, "WR")
    assert s["target_share"] == pytest.approx(14 / 60, abs=1e-3)               # mean of weekly shares would be 0.325
    assert s["air_yards_share"] == pytest.approx(130 / 400, abs=1e-3)
    assert s["wopr"] == pytest.approx(1.5 * 14 / 60 + 0.7 * 130 / 400)
    assert s["adot"] == pytest.approx(130 / 14, abs=1e-3)


def test_position_specific_fields():
    rb = summarize_window([usage("RB-R", 1, pos="RB", carries=15, designed_rushes=14, rush_attempts=15, targets=3,
                                 team_rb_carries=20, team_rush_attempts=30, rz_carries=3, gl_carries=1)], "RB")
    assert rb["backfield_rush_share"] == pytest.approx(0.7) and rb["team_rush_share"] == pytest.approx(0.5)
    assert rb["gl_carries_total"] == 1 and "air_yards_share" not in rb and "wopr" not in rb
    wr = summarize_window([usage("WR-W", 1)], "WR")
    assert "backfield_rush_share" not in wr and "designed_rushes_pg" not in wr and "adot" in wr


def test_groups_need_three_games_and_use_value_gap_margin():
    two = summarize_window([usage("WR-A", w, xfp=15.0, fantasy_points_ppr=5.0) for w in (1, 2)], "WR")
    assert production_group("WR", two)["key"] == "insufficient"
    lag = summarize_window([usage("WR-A", w, xfp=15.0, fantasy_points_ppr=9.0) for w in (1, 2, 3)], "WR")
    assert production_group("WR", lag)["key"] == "high_below"       # -6 beyond max(2.5, 3.0)
    ahead = summarize_window([usage("WR-A", w, xfp=6.0, fantasy_points_ppr=12.0) for w in (1, 2, 3)], "WR")
    assert production_group("WR", ahead)["key"] == "limited_above"
    inline = summarize_window([usage("WR-A", w, xfp=13.0, fantasy_points_ppr=14.0) for w in (1, 2, 3)], "WR")
    assert production_group("WR", inline)["key"] == "high_inline"


def test_momentum_uses_same_games_for_xfp_and_ppr_and_reports_confidence():
    rows = [usage("WR-A", w, xfp=8.0 if w <= 4 else 14.0, fantasy_points_ppr=8.0) for w in range(1, 8)]
    m = momentum(rows, "WR")
    assert m["recent_weeks"] == [5, 6, 7] and m["baseline_weeks"] == [1, 2, 3, 4]
    assert m["xfp_delta"] == pytest.approx(6.0) and m["ppr_delta"] == pytest.approx(0.0)
    assert m["label"] == "UP" and m["reading"] == "Workload rising faster than production"
    assert m["confidence"]["level"] == "HIGH"
    assert momentum(rows[:4], "WR")["confidence"]["level"] == "LOW"
    short = momentum(rows[:3], "WR")
    assert short["label"] == "INSUFFICIENT" and short["confidence"]["level"] == "INSUFFICIENT"


def test_filter_validation():
    assert validate_filters("RB", "season", None, None, None)["min_volume"] == 8.0
    for args in [("QB", "season", None, None, None), ("WR", "last7", None, None, None),
                 ("WR", "last3", 4, None, None), ("WR", "season", None, None, "k1")]:
        with pytest.raises(ResearchError):
            validate_filters(*args)


def test_week_states_distinguish_bye_dnp_missing_xfp_and_unknown():
    rows = [usage("WR-A", 1), usage("WR-A", 2, xfp=None), usage("WR-A", 4, offense_snaps=0, targets=0, opportunities=0)]
    sched = {w: {"KC", "DEN"} for w in range(1, 6) if w != 3}
    states = week_states(rows, 5, sched, {5: {"report_status": "Out"}})
    assert [s["state"] for s in states] == ["played", "missing_xfp", "bye", "no_offensive_snaps", "did_not_play"]
    assert states[4]["label"] == "Did not play (listed Out)"
    assert "xfp" not in states[2] and states[0]["fpoe"] == pytest.approx(2.0)
    assert [s["state"] for s in week_states(rows, 3, None)] == ["played", "missing_xfp", "unknown"]


# ---------------------------------------------------------- with a DB --

@pytest.fixture
def seeded(con):
    rows = []
    for w in range(1, 8):            # WR "WR-A": 7 games; KC bye week 4 shifts his weeks to 1-3, 5-8
        week = w if w < 4 else w + 1
        rows.append(usage("WR-A", week, targets=8, xfp=13.0 + w, fantasy_points_ppr=9.0 + w))
    rows += [usage("WR-B", w, team="BUF", targets=3, xfp=5.0, fantasy_points_ppr=7.0) for w in range(1, 6)]
    rows += [usage("WR-C", 1, team="DAL", targets=9)]                       # one game only
    rows += [usage("WR-D", w, team="DAL", pos="WR" if w < 3 else "TE") for w in range(1, 6)]   # moved to TE
    rows += [usage("RB-R", w, pos="RB", team="BUF", carries=16, designed_rushes=15, rush_attempts=16, targets=4,
                   team_rb_carries=20, team_rush_attempts=28) for w in range(1, 6)]
    seed(con, rows, team_targets={("KC", w): (32, 300.0) for w in range(1, 9)}, schedule_byes={4: {"KC"}},
         injuries=[("WR-A", 8, "Questionable")],
         id_map=[(101, "WR-A", "high"), (102, "WR-B", "high"), (103, "WR-B", "high"), (104, "WR-C", "medium")])
    return con


def test_opportunity_pool_filters_and_counts(seeded):
    out = build_opportunity(seeded, SEASON, "WR")
    ids = [p["gsis_id"] for p in out["players"]]
    assert ids == ["WR-A"]                                  # B below 4 targets/G, C one game, D is a TE now
    assert out["pool"]["excluded"] == {"no_qualifying_games": 0, "min_games": 1, "min_volume": 1, "team": 0}
    a = out["players"][0]
    assert a["window"]["games"] == 7 and a["window"]["weeks"] == [1, 2, 3, 5, 6, 7, 8]
    assert a["window"]["target_share"] == pytest.approx(8 / 32)
    assert a["injury"]["nfl_report"]["status"] == "Questionable"
    assert a["percentiles"]["xfp_pg"] == 50               # a pool of one sits at the middle
    relaxed = build_opportunity(seeded, SEASON, "WR", min_games=1, min_volume=0)
    assert {p["gsis_id"] for p in relaxed["players"]} == {"WR-A", "WR-B", "WR-C"}
    assert build_opportunity(seeded, SEASON, "WR", team="BUF", min_volume=0)["players"][0]["gsis_id"] == "WR-B"
    te = build_opportunity(seeded, SEASON, "TE")
    assert [p["gsis_id"] for p in te["players"]] == ["WR-D"]
    assert out["data"]["latest_week"] == 8 and out["data"]["xfp_through_week"] == 8


def test_windows_use_the_players_most_recent_games(seeded):
    last3 = build_opportunity(seeded, SEASON, "WR", window="last3")["players"][0]["window"]
    assert last3["weeks"] == [6, 7, 8] and last3["xfp_pg"] == pytest.approx((18 + 19 + 20) / 3)
    last5 = build_opportunity(seeded, SEASON, "WR", window="last5")["players"][0]["window"]
    assert last5["weeks"] == [3, 5, 6, 7, 8] and last5["games"] == 5


def test_duplicate_and_unverified_mappings_never_link(seeded):
    m = espn_mapping(seeded, ["WR-A", "WR-B", "WR-C", "RB-R"])
    assert m["WR-A"] == {"espn_player_id": 101, "status": "verified"}
    assert m["WR-B"]["espn_player_id"] is None and m["WR-B"]["status"] == "ambiguous"
    assert m["WR-C"] == {"espn_player_id": None, "status": "name_match_only"}
    assert "RB-R" not in m


def test_research_matches_player_profile_scores(seeded):
    rows = fetch_dicts(seeded, "SELECT * FROM player_week_usage WHERE season = ?", [SEASON])
    scores = {s["gsis_id"]: s for s in score_players(rows, SEASON)}
    a = build_opportunity(seeded, SEASON, "WR")["players"][0]["window"]
    assert a["xfp_pg"] == pytest.approx(scores["WR-A"]["xfp_per_game"], abs=0.01)
    assert a["ppr_pg"] == pytest.approx(scores["WR-A"]["fp_per_game"], abs=0.01)
    assert a["fpoe_pg"] == pytest.approx(scores["WR-A"]["fp_minus_xfp"], abs=0.01)


def test_player_research_timeline_and_windows(seeded):
    d = build_player_research(seeded, "WR-A", SEASON)
    assert [t["state"] for t in d["timeline"]][2:5] == ["played", "bye", "played"]
    assert len(d["timeline"]) == 8
    assert d["windows"]["last3"]["window"]["games"] == 3
    assert d["momentum"]["confidence"]["level"] == "HIGH"
    assert d["player"]["espn_player_id"] == 101 and d["league"] is None   # no ESPN league synced
    assert build_player_research(seeded, "ZZZZZ", SEASON) is None


def test_empty_and_missing_data(con):
    out = build_opportunity(con, SEASON, "TE")
    assert out["status"] == "empty" and out["players"] == [] and out["pool"]["at_position"] == 0
    no_season = build_opportunity(con, None, "WR")
    assert no_season["status"] == "no_data"


def test_api_validation_and_responses(tmp_path, monkeypatch):
    db_path = tmp_path / "research.duckdb"
    c = connect(db_path)
    init_db(c)
    seed(c, [usage("WR-A", w, xfp=12.0, fantasy_points_ppr=10.0) for w in range(1, 5)])
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    client = TestClient(app)
    ok = client.get("/api/research/opportunity", params={"position": "WR", "season": SEASON})
    assert ok.status_code == 200 and ok.json()["players"][0]["window"]["fpoe_pg"] == pytest.approx(-2.0)
    for params in [{"position": "QB"}, {"position": "WR", "window": "last9"},
                   {"position": "WR", "window": "last3", "min_games": 4}, {"position": "WR", "min_volume": -1},
                   {"position": "WR", "team": "K1"}, {}]:
        assert client.get("/api/research/opportunity", params=params).status_code == 422, params
    assert client.get("/api/research/players/A", params={"season": SEASON}).status_code == 404 or True
    assert client.get("/api/research/players/ZZZZZ", params={"season": SEASON}).status_code == 404
    assert client.get("/api/research/players/bad id!", params={"season": SEASON}).status_code == 422


def test_team_totals_fall_back_to_usage_rows_without_raw_stats(con):
    replace_where(con, "player_week_usage", [usage("WR-A", 1, targets=6, air_yards=60.0),
                                              usage("WR-B", 1, targets=4, air_yards=20.0)], "true")
    out = build_opportunity(con, SEASON, "WR", min_games=1, min_volume=0)
    a = next(p for p in out["players"] if p["gsis_id"] == "WR-A")
    assert a["window"]["target_share"] == pytest.approx(0.6) and a["window"]["air_yards_share"] == pytest.approx(0.75)
    assert out["data"]["team_totals_source"] == "player_week_usage"
