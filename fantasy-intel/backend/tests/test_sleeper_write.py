"""A Sleeper league written into the league tables (app/sleeper/write.py)."""

import json
from pathlib import Path

import duckdb
import pytest

from app.db.connection import fetch_dicts, init_db
from app.db.league_split import split_database
from app.sleeper.write import SYNTHETIC_BASE, SleeperPayloads, pool_players, write_sleeper

FIX = Path(__file__).resolve().parents[1] / "app" / "leagues" / "providers" / "fixture_data" / "sleeper"
LEAGUE = FIX / "leagues" / "1180000000000000001"


def _j(path: Path):
    return json.loads(path.read_text())


def payloads(**over) -> SleeperPayloads:
    rosters = _j(LEAGUE / "rosters.json")
    players = _j(FIX / "players.json")
    # a played week 5: roster 2 started its starters and had one more player on the bench (4046, who scored 3.3)
    r2 = next(r for r in rosters if r["roster_id"] == 2)
    starters = [s for s in r2["starters"] if s != "0"]
    week5 = [{"roster_id": 2, "matchup_id": 1, "points": 88.4, "starters": r2["starters"],
              "players": [*r2["players"], "4046"],
              "players_points": {**{s: 10.0 + i for i, s in enumerate(starters)}, "4046": 3.3}},
             {"roster_id": 1, "matchup_id": 1, "points": 90.0, "starters": [], "players": [], "players_points": {}}]
    p = SleeperPayloads(league=_j(LEAGUE / "league.json"), users=_j(LEAGUE / "users.json"), rosters=rosters,
                        state=_j(FIX / "state.json"),
                        matchups={5: week5, 6: _j(LEAGUE / "matchups_6.json"), 7: _j(LEAGUE / "matchups_7.json")},
                        transactions={6: _j(LEAGUE / "transactions_6.json")}, players=players)
    for k, v in over.items():
        setattr(p, k, v)
    return p


@pytest.fixture
def con(tmp_path):
    c = duckdb.connect(str(tmp_path / "s.duckdb"))
    init_db(c)
    c.execute(f"CREATE TABLE nfl_ff_playerids AS SELECT * FROM read_csv_auto('{FIX / 'ff_playerids.csv'}')")
    yield c
    try:
        c.close()
    except Exception:
        pass


def test_the_league_and_its_teams_are_written_in_the_shared_tables(con):
    stats = write_sleeper(con, payloads())
    lg = fetch_dicts(con, "SELECT * FROM leagues")[0]
    assert lg["league_id"] == 1180000000000000001 and lg["season"] == 2026 and lg["current_week"] == 6
    settings = json.loads(lg["settings_raw"])
    assert settings["provider"] == "sleeper" and settings["sleeper_league_id"] == "1180000000000000001"
    items = json.loads(lg["scoring_items"])
    assert {"statId": 53, "points": 0.5, "pointsOverrides": {}} in items
    assert json.loads(lg["roster_slots"])["OP"] == 1
    assert con.execute("SELECT count(*) FROM fantasy_teams").fetchone() == (4,)
    assert con.execute("SELECT count(*) FROM league_members").fetchone()[0] == len(_j(LEAGUE / "users.json"))
    assert stats["warnings"] == [] or all("injury" in w or "scoring" in w for w in stats["warnings"])
    assert con.execute("SELECT source, status FROM sync_runs").fetchall() == [("sleeper", "ok")]


def test_players_are_keyed_by_espn_id_and_an_unmapped_one_gets_a_synthetic_id(con):
    write_sleeper(con, payloads())
    rows = {r["full_name"]: r for r in fetch_dicts(con, "SELECT * FROM fantasy_players")}
    assert rows["Quinn Arbor"]["espn_player_id"] == 3139477
    assert rows["Rory Bend"]["injury_status"] == "QUESTIONABLE"
    dsts = [r for r in rows.values() if r["position"] == "D/ST"]
    assert dsts and all(-16034 <= r["espn_player_id"] <= -16001 for r in dsts)
    synthetic = [r for r in rows.values() if r["espn_player_id"] <= -SYNTHETIC_BASE]
    assert all(r["espn_player_id"] < -16100 for r in synthetic)
    assert {r["availability_status"] for r in rows.values()} <= {"ONTEAM", "FREEAGENT"}
    assert all(r["percent_owned"] is None for r in rows.values())


def test_played_weeks_keep_sleepers_own_points_and_lineups(con):
    write_sleeper(con, payloads())
    starters = con.execute("SELECT count(*), sum(points) FROM lineup_snapshots WHERE scoring_period = 5 AND team_id = 2 "
                           "AND is_starter").fetchone()
    assert starters[0] == 6 and starters[1] == pytest.approx(sum(10.0 + i for i in range(6)))
    bench = fetch_dicts(con, "SELECT * FROM lineup_snapshots WHERE scoring_period = 5 AND NOT is_starter")
    assert [b["lineup_slot_id"] for b in bench] == [20] * len(bench) and bench
    pts = con.execute("SELECT count(*) FROM espn_player_weekly_points WHERE scoring_period = 5 "
                      "AND actual_points IS NOT NULL").fetchone()[0]
    assert pts == 7
    assert con.execute("SELECT count(*) FROM espn_player_weekly_points WHERE projected_points IS NOT NULL"
                       ).fetchone() == (0,)
    m = fetch_dicts(con, "SELECT * FROM matchups WHERE matchup_period = 5")[0]
    assert (m["home_team_id"], m["away_team_id"], m["winner"]) == (1, 2, "HOME")
    future = fetch_dicts(con, "SELECT * FROM matchups WHERE matchup_period = 7")
    assert future and all(f["home_points"] is None and f["winner"] == "UNDECIDED" for f in future)


def test_the_current_week_lineups_come_from_the_rosters(con):
    write_sleeper(con, payloads())
    cur = fetch_dicts(con, "SELECT * FROM lineup_snapshots WHERE scoring_period = 6 AND team_id = 2")
    assert any(r["is_starter"] and r["lineup_slot"] == "D/ST" for r in cur)
    assert len({r["espn_player_id"] for r in cur}) == len(cur)


def test_transactions_and_counters(con):
    write_sleeper(con, payloads())
    tx = fetch_dicts(con, "SELECT * FROM transactions")
    assert tx and all(t["transaction_id"].startswith("sleeper:") for t in tx)
    assert {t["type"] for t in tx} <= {"FREEAGENT", "WAIVER", "TRADE_ACCEPT", "ROSTER"}
    items = fetch_dicts(con, "SELECT * FROM transaction_items")
    assert items and {i["item_type"] for i in items} <= {"ADD", "DROP", "TRADE"}


def test_a_rerun_is_idempotent(con):
    write_sleeper(con, payloads())
    before = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("fantasy_players", "lineup_snapshots", "matchups", "transactions", "espn_player_weekly_points")}
    write_sleeper(con, payloads())
    assert before == {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before}


def test_the_payloads_round_trip_and_the_pool_keeps_rostered_and_active_players():
    p = payloads()
    back = SleeperPayloads.from_json(json.loads(json.dumps(p.to_json())))
    assert back == p
    rostered = {"4046"}
    pool = pool_players({"4046": {"position": "QB", "active": False}, "1": {"position": "RB", "active": True, "team": "KC"},
                         "2": {"position": "RB", "active": True, "team": None}, "KC": {"position": "DEF"},
                         "3": {"position": "LB", "active": True, "team": "KC"}}, rostered)
    assert set(pool) == {"4046", "1", "KC"}


def test_a_written_sleeper_league_splits_like_any_league(con, tmp_path):
    write_sleeper(con, payloads())
    path = Path(con.execute("SELECT path FROM duckdb_databases() WHERE database_name = current_database()").fetchone()[0])
    con.execute("CHECKPOINT")
    con.close()
    out = split_database(path, tmp_path / "lg.duckdb", tmp_path / "gl.duckdb", stamp="2026-10-21T00:00:00",
                         league=(1180000000000000001, 2026))
    assert out.league_id == 1180000000000000001
