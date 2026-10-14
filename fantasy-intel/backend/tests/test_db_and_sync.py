"""Database initialisation + idempotent ESPN sync."""

from app.db.connection import init_db, table_columns, upsert_rows
from app.espn import parse
from app.espn.sync import fetch_all, write_all

from espn_fixture import CURRENT, MY_SWID, FakeEspnClient, transactions_payload, tx

TABLES = ["sync_runs", "leagues", "fantasy_teams", "fantasy_players", "lineup_snapshots", "transactions",
          "transaction_items", "activity_messages", "nfl_players", "player_id_map", "player_week_usage",
          "player_scores", "manager_metrics", "manager_classifications", "waiver_recommendations"]


def counts(con):
    return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def test_init_db_creates_schema_and_is_rerunnable(con):
    init_db(con)  # second run must not fail
    for t in TABLES:
        assert table_columns(con, t), f"missing table {t}"


def test_upsert_replaces_by_key(con):
    row = {"espn_player_id": 1, "full_name": "A", "position": "RB"}
    upsert_rows(con, "fantasy_players", [row], ["espn_player_id"])
    upsert_rows(con, "fantasy_players", [{**row, "full_name": "B"}], ["espn_player_id"])
    assert con.execute("SELECT full_name FROM fantasy_players").fetchall() == [("B",)]


def test_espn_sync_is_idempotent(con):
    payloads = fetch_all(FakeEspnClient())
    write_all(con, payloads, swid=MY_SWID, my_team_id=None)
    first = counts(con)
    write_all(con, payloads, swid=MY_SWID, my_team_id=None)
    assert counts(con) == first
    assert first["transactions"] == 6
    assert first["fantasy_teams"] == 4
    # no duplicate keys anywhere
    assert con.execute("SELECT count(*) - count(DISTINCT (scoring_period, team_id, espn_player_id)) "
                       "FROM lineup_snapshots").fetchone()[0] == 0


def test_resync_replaces_changed_state(con):
    write_all(con, fetch_all(FakeEspnClient()), swid=MY_SWID, my_team_id=None)
    # A pending claim later fails, and team 4 drops a player in the current period.
    changed = transactions_payload(2)
    for t in changed["transactions"]:
        if t["id"] == "tx-3":
            t["status"] = "CANCELED"
    client = FakeEspnClient({2: changed})
    from espn_fixture import ROSTERS, roster_payload
    smaller = {k: list(v) for k, v in ROSTERS.items()}
    smaller[4] = smaller[4][:1]
    client.roster_for_period = lambda p: roster_payload(p, smaller)
    client.league = lambda: {**FakeEspnClient().league(), "teams": [
        {**t, "roster": next(r for r in roster_payload(CURRENT, smaller)["teams"] if r["id"] == t["id"])["roster"]}
        for t in FakeEspnClient().league()["teams"]]}
    write_all(con, fetch_all(client), swid=MY_SWID, my_team_id=None)
    assert con.execute("SELECT status FROM transactions WHERE transaction_id='tx-3'").fetchone()[0] == "CANCELED"
    assert con.execute("SELECT count(*) FROM transactions").fetchone()[0] == 6
    n4 = con.execute("SELECT count(*) FROM lineup_snapshots WHERE team_id=4 AND scoring_period=?", [CURRENT]).fetchone()[0]
    assert n4 == 1  # stale row for the dropped player is gone


def test_my_team_detected_from_swid_case_insensitive(con):
    write_all(con, fetch_all(FakeEspnClient()), swid=MY_SWID, my_team_id=None)
    assert con.execute("SELECT team_id FROM fantasy_teams WHERE is_mine").fetchall() == [(2,)]


def test_my_team_override(con):
    write_all(con, fetch_all(FakeEspnClient()), swid=None, my_team_id=3)
    assert con.execute("SELECT team_id FROM fantasy_teams WHERE is_mine").fetchall() == [(3,)]


def test_missing_points_are_null_not_zero(con):
    write_all(con, fetch_all(FakeEspnClient()), swid=MY_SWID, my_team_id=None)
    # current period has no actual stats in the fixture -> NULL
    nulls = con.execute("SELECT count(*) FROM lineup_snapshots WHERE scoring_period=? AND points IS NULL",
                        [CURRENT]).fetchone()[0]
    zeros = con.execute("SELECT count(*) FROM lineup_snapshots WHERE scoring_period=? AND points = 0",
                        [CURRENT]).fetchone()[0]
    assert nulls > 0 and zeros == 0


def test_parse_activity_trade_legs_and_waiver_failures(con):
    write_all(con, fetch_all(FakeEspnClient()), swid=MY_SWID, my_team_id=None)
    legs = con.execute("SELECT espn_player_id, from_team_id, to_team_id FROM activity_messages "
                       "WHERE message_type_id=244 ORDER BY espn_player_id").fetchall()
    assert legs == [(302, 1, 4), (303, 1, 4), (404, 4, 1)]
    failed = con.execute("SELECT team_id FROM transactions WHERE status LIKE 'FAILED%'").fetchall()
    assert failed == [(3,)]


def test_league_settings_parse():
    from espn_fixture import league_payload
    from datetime import datetime
    lg = parse.parse_league(league_payload(), swid=MY_SWID, my_team_id=None, synced_at=datetime(2026, 1, 1))
    assert lg["league"]["roster_slots"] == {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "D/ST": 1, "K": 1,
                                            "BE": 3, "IR": 1}
    assert lg["league"]["reception_points"] == 1.0
    assert lg["league"]["is_faab"] is True


def test_transaction_items_parse_keeps_source_ids():
    from datetime import datetime
    out = parse.parse_transactions({"transactions": [tx("x", 1, 1, 0, [{"type": "ADD", "playerId": 5, "toTeamId": 1}])]},
                                   league_id=1, season=2026, synced_at=datetime(2026, 1, 1))
    assert out["items"][0]["espn_player_id"] == 5 and out["transactions"][0]["transaction_id"] == "x"


def test_member_swids_are_never_stored(con, tmp_path, monkeypatch):
    from app.espn import sync as sync_mod
    monkeypatch.setattr(sync_mod, "DATA_DIR", tmp_path)
    payloads = fetch_all(FakeEspnClient())
    sync_mod.save_raw(payloads, "run1")
    write_all(con, payloads, swid=MY_SWID, my_team_id=None)
    raw_text = "".join(f.read_text() for f in (tmp_path / "raw" / "espn" / "run1").glob("*.json"))
    assert "abcd-1234" not in raw_text.lower()
    dump = str(con.execute("SELECT * FROM league_members").fetchall()) + \
        str(con.execute("SELECT owner_ids FROM fantasy_teams").fetchall())
    assert "abcd-1234" not in dump.lower() and "m_" in dump
    # ownership detection still worked because it ran before hashing
    assert con.execute("SELECT team_id FROM fantasy_teams WHERE is_mine").fetchall() == [(2,)]



def test_free_agent_current_week_projection_is_retained(con):
    """ESPN's FA pool payload already includes each free agent's projection for the
    requested period; it must be stored (previously only rostered players' were)."""
    from espn_fixture import CURRENT, SEASON, pool_payload
    pool = pool_payload()
    pool["players"][0]["player"]["stats"] = [
        {"seasonId": SEASON, "scoringPeriodId": CURRENT, "statSourceId": 1, "statSplitTypeId": 1, "appliedTotal": 11.5},
        {"seasonId": SEASON, "scoringPeriodId": 0, "statSourceId": 1, "statSplitTypeId": 0, "appliedTotal": 140.0},
        {"seasonId": SEASON, "scoringPeriodId": 0, "statSourceId": 1, "statSplitTypeId": 2, "appliedTotal": 150.0},
    ]
    # forward projection fetch fails for every week: the pool's own projection must still be kept
    client = FakeEspnClient(projections={w: RuntimeError("down") for w in range(1, 18)})
    client.player_pool = lambda period, limit=500: pool
    client.player_cards = lambda ids, m: {"players": []}
    write_all(con, fetch_all(client), swid=MY_SWID, my_team_id=None)
    fa_id = pool["players"][0]["player"]["id"]
    rows = con.execute("SELECT scoring_period, actual_points, projected_points FROM espn_player_weekly_points "
                       "WHERE espn_player_id = ? ORDER BY 1", [fa_id]).fetchall()
    assert rows == [(CURRENT, None, 11.5)]        # projection kept; no fake actual; season splits not stored as weeks


def test_pool_projection_never_overwrites_a_known_actual(con):
    from espn_fixture import CURRENT, SEASON, pool_payload
    pool = pool_payload()
    fa_id = pool["players"][0]["player"]["id"]
    pool["players"][0]["player"]["stats"] = [
        {"seasonId": SEASON, "scoringPeriodId": CURRENT - 1, "statSourceId": 1, "statSplitTypeId": 1, "appliedTotal": 9.0}]
    client = FakeEspnClient()
    client.player_pool = lambda period, limit=500: pool
    client.player_cards = lambda ids, m: {"players": [{"id": fa_id, "player": {"id": fa_id, "stats": [
        {"seasonId": SEASON, "scoringPeriodId": CURRENT - 1, "statSourceId": 0, "statSplitTypeId": 1, "appliedTotal": 4.0}]}}]}
    write_all(con, fetch_all(client), swid=MY_SWID, my_team_id=None)
    row = con.execute("SELECT actual_points, projected_points FROM espn_player_weekly_points WHERE espn_player_id = ? "
                      "AND scoring_period = ?", [fa_id, CURRENT - 1]).fetchone()
    assert row == (4.0, 9.0)
