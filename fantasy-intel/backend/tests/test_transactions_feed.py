"""The league activity feed (app/analytics/transactions_feed.py, GET /api/transactions) on synthetic ESPN and Sleeper
leagues: one event per move, each trade once, nothing invented, filters and pages."""
from datetime import datetime, timedelta

import pytest

from app.analytics import transactions_feed as tf
from app.analytics.transactions_feed import Leg, Record
from app.espn.sync import fetch_all, write_all

from espn_fixture import DAY, MY_SWID, T0, FakeEspnClient, tx

# ------------------------------------------------------------------ ESPN --


def _trade(tid, team, day, items, ttype="TRADE_ACCEPT", related=None, proposed_day=None, status="EXECUTED"):
    t = tx(tid, team, 2, day, items, ttype, status=status)
    if related:
        t["relatedTransactionId"] = related
    if proposed_day is not None:
        t["proposedDate"] = T0 + proposed_day * DAY
    return t


def _leg(pid, frm, to, kind="TRADE"):
    return {"type": kind, "playerId": pid, "fromTeamId": frm, "toTeamId": to}


ESPN_TX = {
    1: [
        # add + drop pair: one waiver event (FAAB bid shown), proposed (claim) and processed times differ
        {**tx("w-1", 1, 1, 2, [_leg(203, -1, 1, "ADD"), _leg(209, 1, -1, "DROP")], "WAIVER", bid=12),
         "proposedDate": T0 + 1 * DAY},
        tx("fa-1", 2, 1, 2, [_leg(206, -1, 2, "ADD")], bid=0),                     # FA pickup: no bid shown
        tx("drop-1", 3, 1, 3, [_leg(208, 3, -1, "DROP")]),                         # plain drop
        tx("fail-1", 3, 1, 3, [_leg(209, -1, 3, "ADD")], "WAIVER", status="FAILED_INVALIDPLAYERSOURCE", bid=5),
        tx("pend-1", 4, 1, 3, [_leg(405, -1, 4, "ADD")], "WAIVER", status="PENDING", bid=3),
        tx("lineup-1", 2, 1, 3, [{"type": "LINEUP", "playerId": 207, "fromLineupSlotId": 20, "toLineupSlotId": 21}],
           "ROSTER"),
    ],
    2: [
        # one multi-player trade stored three times: accept (with a drop to make room), uphold (same legs, related to
        # the accept) and the activity feed (below); the proposal itself is not an executed move
        _trade("tr-accept", 1, 9, [_leg(302, 1, 4), _leg(303, 1, 4), _leg(404, 4, 1), _leg(305, 4, -1, "DROP")],
               proposed_day=8),
        _trade("tr-uphold", 4, 10, [_leg(302, 1, 4), _leg(303, 1, 4), _leg(404, 4, 1)], "TRADE_UPHOLD",
               related="tr-accept"),
        _trade("tr-offer", 1, 8, [_leg(302, 1, 4)], "TRADE_PROPOSAL"),
        # a trade whose items the provider left out for this team: its legs come from the activity feed
        _trade("tr-itemless", 2, 12, []),
        _trade("tr-veto", 3, 12, [_leg(208, 3, 2)], "TRADE_VETO"),
    ],
}


class FeedEspnClient(FakeEspnClient):
    def transactions(self, period):
        return {"transactions": [dict(t) for t in ESPN_TX.get(period, [])]}

    def activity(self, offset=0, size=50):
        if offset:
            return {"topics": []}
        return {"topics": [
            {"id": "topic-1", "date": T0 + 10 * DAY, "messages": [
                {"id": "m1", "messageTypeId": 244, "targetId": 302, "from": 1, "to": 4},
                {"id": "m2", "messageTypeId": 244, "targetId": 303, "from": 1, "to": 4},
                {"id": "m3", "messageTypeId": 244, "targetId": 404, "from": 4, "to": 1}]},
            {"id": "topic-2", "date": T0 + 12 * DAY + 3_600_000, "messages": [
                {"id": "m4", "messageTypeId": 244, "targetId": 205, "from": 2, "to": 3},
                {"id": "m5", "messageTypeId": 244, "targetId": 401, "from": 3, "to": 2}]},
            # a trade seen only in the activity feed
            {"id": "topic-3", "date": T0 + 20 * DAY, "messages": [
                {"id": "m6", "messageTypeId": 244, "targetId": 304, "from": 2, "to": 3}]},
            # adds and drops in the activity feed duplicate stored transactions: never a second event
            {"id": "topic-4", "date": T0 + 2 * DAY, "messages": [
                {"id": "m7", "messageTypeId": 180, "targetId": 203, "to": 1, "from": 12},
                {"id": "m8", "messageTypeId": 179, "targetId": 209, "to": -1, "from": 1}]},
        ]}


@pytest.fixture
def espn(con):
    write_all(con, fetch_all(FeedEspnClient()), swid=MY_SWID, my_team_id=None)
    return con


def _by_kind(feed, kind):
    return [e for e in feed["events"] if e["kind"] == kind]


def _legs(event):
    return sorted((leg["action"], leg["espn_player_id"], (leg["from_team"] or {}).get("team_id"),
                   (leg["to_team"] or {}).get("team_id")) for leg in event["legs"])


def test_espn_trades_appear_once_with_every_leg(espn):
    feed = tf.build_transactions_feed(espn)
    trades = _by_kind(feed, "trade")
    assert len(trades) == 3
    multi = next(t for t in trades if "tr-accept" in t["sources"]["transactions"])
    assert multi["sources"] == {"transactions": ["tr-accept", "tr-uphold"], "activity": ["topic-1"]}
    assert _legs(multi) == [("drop", 305, 4, None), ("trade", 302, 1, 4), ("trade", 303, 1, 4), ("trade", 404, 4, 1)]
    assert [t["team_id"] for t in multi["teams"]] == [1, 4]
    assert multi["week"] == 2
    # processed when upheld, proposed when offered
    assert multi["processed_at"] == tf._iso(datetime(1970, 1, 1) + timedelta(milliseconds=T0 + 10 * DAY))
    assert multi["proposed_at"] == tf._iso(datetime(1970, 1, 1) + timedelta(milliseconds=T0 + 8 * DAY))
    itemless = next(t for t in trades if "tr-itemless" in t["sources"]["transactions"])
    assert itemless["sources"]["activity"] == ["topic-2"]
    assert _legs(itemless) == [("trade", 205, 2, 3), ("trade", 401, 3, 2)]
    only_feed = next(t for t in trades if t["sources"]["transactions"] == [])
    assert only_feed["event_id"] == "act:topic-3" and only_feed["week"] is None
    assert _legs(only_feed) == [("trade", 304, 2, 3)]
    # no player appears twice in one trade, nothing from the proposal or the veto
    for t in trades:
        assert len({leg["espn_player_id"] for leg in t["legs"]}) == len(t["legs"])
    assert not any("tr-offer" in e["sources"]["transactions"] or "tr-veto" in e["sources"]["transactions"]
                   for e in feed["events"])


def test_espn_adds_and_drops_are_one_event_per_move_and_only_executed(espn):
    feed = tf.build_transactions_feed(espn)
    ids = {e["event_id"] for e in feed["events"]}
    assert {"tx:w-1", "tx:fa-1", "tx:drop-1"} <= ids
    assert not ids & {"tx:fail-1", "tx:pend-1", "tx:lineup-1"}
    waiver = next(e for e in feed["events"] if e["event_id"] == "tx:w-1")
    assert waiver["kind"] == "waiver" and _legs(waiver) == [("add", 203, None, 1), ("drop", 209, 1, None)]
    assert waiver["bid_amount"] == 12          # the fixture league uses FAAB
    assert waiver["proposed_at"] is not None and waiver["proposed_at"] < waiver["processed_at"]
    fa = next(e for e in feed["events"] if e["event_id"] == "tx:fa-1")
    assert fa["kind"] == "free_agent" and fa["bid_amount"] is None and fa["proposed_at"] is None
    assert next(e for e in feed["events"] if e["event_id"] == "tx:drop-1")["kind"] == "drop"
    assert feed["summary"]["total"] == len(feed["events"]) == 6
    # the add/drop activity messages never become events of their own
    assert not any(e["event_id"] == "act:topic-4" for e in feed["events"])


def test_espn_bids_are_hidden_outside_faab_leagues(espn):
    espn.execute("UPDATE leagues SET is_faab = false")
    feed = tf.build_transactions_feed(espn)
    assert all(e["bid_amount"] is None for e in feed["events"])


def test_filters_are_combinable_and_options_list_what_is_stored(espn):
    feed = tf.build_transactions_feed(espn)
    assert feed["options"]["types"] == ["trade", "waiver", "free_agent", "drop"]
    assert feed["options"]["weeks"] == [1, 2]
    assert [m["team_id"] for m in feed["options"]["managers"]] == [1, 2, 3, 4]
    assert [e["event_id"] for e in tf.build_transactions_feed(espn, manager=4)["events"]] == ["tx:tr-accept"]
    te = tf.build_transactions_feed(espn, position="TE")
    assert {e["event_id"] for e in te["events"]} == {"tx:tr-accept", "tx:tr-itemless"}
    wk1 = tf.build_transactions_feed(espn, week=1, kind="free_agent")
    assert [e["event_id"] for e in wk1["events"]] == ["tx:fa-1"]
    assert wk1["summary"] == {"total": 6, "matching": 1, "by_type": {"free_agent": 1}, "undated": 0,
                              "unplaced_week": 0}
    with pytest.raises(tf.UnknownManager):
        tf.build_transactions_feed(espn, manager=99)


def test_pages_follow_the_cursor_without_gaps_or_repeats(espn):
    seen, cursor = [], None
    while True:
        feed = tf.build_transactions_feed(espn, cursor=cursor, limit=4)
        seen += [e["event_id"] for e in feed["events"]]
        cursor = feed["page"]["next_cursor"]
        if cursor is None:
            break
    full = [e["event_id"] for e in tf.build_transactions_feed(espn)["events"]]
    assert seen == full and len(set(seen)) == len(seen)
    second = tf.build_transactions_feed(espn, cursor=full[3], limit=4)
    assert second["page"]["has_prev"] and second["page"]["prev_cursor"] is None and second["page"]["offset"] == 4
    with pytest.raises(tf.StaleCursor):
        tf.build_transactions_feed(espn, cursor="tx:gone")


def test_sorting_by_date_week_and_type(espn):
    newest = [e["processed_at"] for e in tf.build_transactions_feed(espn)["events"]]
    assert newest == sorted(newest, reverse=True)
    oldest = [e["processed_at"] for e in tf.build_transactions_feed(espn, direction="asc")["events"]]
    assert oldest == sorted(oldest)
    weeks = [e["week"] for e in tf.build_transactions_feed(espn, sort="week", direction="asc")["events"]]
    assert weeks == [1, 1, 1, 2, 2, None]       # a trade seen only in the activity feed has no week: last
    kinds = [e["kind"] for e in tf.build_transactions_feed(espn, sort="type", direction="asc")["events"]]
    assert kinds == sorted(kinds, key=tf.KINDS.index)


def test_after_the_move_reads_stored_lineups_only(espn):
    # The fixture stores every team's lineup for periods 1 and 2; current week 3. Golf RB (203) joined team 1 in
    # week 1 and started both weeks (10 points each).
    feed = tf.build_transactions_feed(espn)
    waiver = next(e for e in feed["events"] if e["event_id"] == "tx:w-1")
    add = next(leg for leg in waiver["legs"] if leg["action"] == "add")
    assert add["after"] == {"weeks": 2, "first_week": 1, "last_week": 2, "on_roster": 2, "started": 2,
                            "starter_points": 20.0}
    assert next(leg for leg in waiver["legs"] if leg["action"] == "drop")["after"] is None
    # The synthetic rosters never moved the traded players: a measured 0 of 1 stored week, no starter points.
    trade = next(e for e in feed["events"] if e["event_id"] == "tx:tr-accept")
    traded = next(leg for leg in trade["legs"] if leg["espn_player_id"] == 302)
    assert traded["after"] == {"weeks": 1, "first_week": 2, "last_week": 2, "on_roster": 0, "started": 0,
                               "starter_points": None}
    assert next(leg for leg in trade["legs"] if leg["action"] == "drop")["after"] is None
    # A trade seen only in the activity feed has no week, so nothing to measure from.
    only_feed = next(e for e in feed["events"] if e["event_id"] == "act:topic-3")
    assert all(leg["after"] is None for leg in only_feed["legs"])


# ---------------------------------------------------------------- Sleeper --


def _sleeper_tx(tid, kind, adds, drops, *, status="complete", rosters=(1,), bid=None, picks=(), faab=(), day=0):
    return {"type": kind, "transaction_id": tid, "status": status, "status_updated": 1760000000000 + day * 86400000,
            "created": 1760000000000 + day * 86400000 - 3600000, "roster_ids": list(rosters), "adds": adds,
            "drops": drops, "consenter_ids": list(rosters), "draft_picks": list(picks), "waiver_budget": list(faab),
            "leg": 6, "creator": "700000000000000101", "metadata": None,
            "settings": {"waiver_bid": bid, "seq": 0} if bid is not None else None}


SLEEPER_TX = {6: [
    # 2-for-1 trade with a draft pick: Sleeper stores each player as an add to one roster and a drop from the other
    _sleeper_tx("9101", "trade", {"6794": 2, "7564": 2, "9509": 1}, {"6794": 1, "7564": 1, "9509": 2},
                rosters=(1, 2), picks=[{"season": "2027", "round": 2, "roster_id": 1, "owner_id": 2,
                                        "previous_owner_id": 1}], day=1),
    _sleeper_tx("9102", "free_agent", {"4984": 1}, {"12500": 1}, day=2),               # add + drop pair
    _sleeper_tx("9103", "waiver", {"8130": 2}, None, rosters=(2,), bid=17, day=3),
    _sleeper_tx("9104", "waiver", {"5850": 1}, None, status="failed", bid=99, day=3),
    _sleeper_tx("9105", "waiver", {"11632": 1}, None, status="pending", bid=40, day=4),
    _sleeper_tx("9106", "commissioner", None, {"10229": 2}, rosters=(2,), day=4),
]}


def _write_sleeper(con, transactions):
    from app.sleeper.write import write_sleeper
    from test_sleeper_write import FIX, payloads
    con.execute(f"CREATE TABLE nfl_ff_playerids AS SELECT * FROM read_csv_auto('{FIX / 'ff_playerids.csv'}')")
    write_sleeper(con, payloads(transactions=transactions))
    return con


@pytest.fixture
def sleeper(con):
    return _write_sleeper(con, SLEEPER_TX)


PICK = {"season": "2027", "round": 1, "roster_id": 2, "owner_id": 1, "previous_owner_id": 2}
FAAB = {"sender": 1, "receiver": 2, "amount": 10, "leg": 6}


def test_a_sleeper_asset_only_trade_stays_separate_from_a_player_trade_an_hour_later(con):
    # Regression (PR #231 review): an item-less transaction row used to be unioned with any trade of the same team
    # within 24 hours, so this picks-and-FAAB-only trade and the player trade became one event.
    _write_sleeper(con, {6: [
        _sleeper_tx("9201", "trade", None, None, rosters=(1, 2), picks=[PICK], faab=[FAAB], day=1),
        {**_sleeper_tx("9202", "trade", {"6794": 2}, {"6794": 1}, rosters=(1, 2), day=1),
         "status_updated": 1760000000000 + 86400000 + 3600000},
    ]})
    trades = _by_kind(tf.build_transactions_feed(con), "trade")
    assert sorted(t["event_id"] for t in trades) == ["tx:sleeper:9201", "tx:sleeper:9202"]
    assets = next(t for t in trades if t["event_id"] == "tx:sleeper:9201")
    assert assets["legs"] == [] and assets["unlisted_assets"] == ["FAAB", "draft picks"]
    assert _names(next(t for t in trades if t["event_id"] == "tx:sleeper:9202")) == [("trade", "Rory Bend", 1, 2)]


def test_a_sleeper_asset_only_trade_lists_every_participant(con):
    # Regression (PR #231 review): the writer keeps only roster_ids[0] in team_id, so a trade with no player legs
    # listed one team and the manager filter missed it for the other.
    _write_sleeper(con, {6: [_sleeper_tx("9301", "trade", None, None, rosters=(1, 2), picks=[PICK], day=1)]})
    feed = tf.build_transactions_feed(con)
    (trade,) = feed["events"]
    assert trade["team_ids"] == [1, 2] and [t["team_id"] for t in trade["teams"]] == [1, 2]
    for team in (1, 2):
        assert [e["event_id"] for e in tf.build_transactions_feed(con, manager=team)["events"]] == ["tx:sleeper:9301"]
    assert tf.build_transactions_feed(con, manager=3)["events"] == []
    assert trade["legs"] == [] and trade["unlisted_assets"] == ["draft picks"]   # flagged, never itemized


def _names(event):
    return sorted((leg["action"], leg["full_name"], (leg["from_team"] or {}).get("team_id"),
                   (leg["to_team"] or {}).get("team_id")) for leg in event["legs"])


def test_sleeper_mirrored_trade_halves_become_one_trade(sleeper):
    feed = tf.build_transactions_feed(sleeper)
    trades = _by_kind(feed, "trade")
    assert len(trades) == 1
    assert _names(trades[0]) == [("trade", "Mo Gaines", 2, 1), ("trade", "Rory Bend", 1, 2),
                                 ("trade", "Wes Crane", 1, 2)]
    assert trades[0]["unlisted_assets"] == ["draft picks"]     # stored, but never listed as a player
    assert trades[0]["week"] == 6 and trades[0]["proposed_at"] is not None


def test_sleeper_moves_bids_and_scope(sleeper):
    feed = tf.build_transactions_feed(sleeper)
    by_id = {e["event_id"]: e for e in feed["events"]}
    assert set(by_id) == {"tx:sleeper:9101", "tx:sleeper:9102", "tx:sleeper:9103", "tx:sleeper:9106"}
    assert _names(by_id["tx:sleeper:9102"]) == [("add", "Jo Ellis", None, 1), ("drop", "Taxi Rookie", 1, None)]
    assert by_id["tx:sleeper:9102"]["kind"] == "free_agent" and by_id["tx:sleeper:9102"]["bid_amount"] is None
    assert by_id["tx:sleeper:9103"]["kind"] == "waiver" and by_id["tx:sleeper:9103"]["bid_amount"] == 17
    assert by_id["tx:sleeper:9106"]["kind"] == "commissioner"
    assert _names(by_id["tx:sleeper:9106"]) == [("drop", "Kai Hollis", 2, None)]
    assert all(e["processed_at"].endswith("Z") for e in feed["events"])


# ------------------------------------------------------------------- pure --


def _rec(sid, legs, when, source="transactions", team=None, related=None):
    return Record(source, sid, "TRADE_ACCEPT" if source == "transactions" else "TRADE", team, 5, when, None,
                  related, legs=legs)


def test_the_same_player_moved_again_weeks_later_is_a_separate_trade():
    t0 = datetime(2026, 10, 1)
    a = _rec("a", [Leg("trade", 1, 1, 2)], t0)
    b = _rec("b", [Leg("trade", 1, 1, 2)], t0 + timedelta(days=tf.TRADE_WINDOW_DAYS + 1))
    c = _rec("c", [Leg("trade", 1, 1, 2)], t0 + timedelta(days=1), source="activity")
    groups = sorted(sorted(r.source_id for r in g) for g in tf.merge_trades([a, b, c]))
    assert groups == [["a", "c"], ["b"]]


def test_rows_pointing_at_the_same_unloaded_proposal_are_one_trade():
    t0 = datetime(2026, 10, 1)
    accept = _rec("acc", [], t0, team=1, related="proposal")
    uphold = _rec("uph", [], t0 + timedelta(days=2), team=2, related="proposal")
    other = _rec("x", [], t0 + timedelta(days=2), team=3)
    groups = sorted(sorted(r.source_id for r in g) for g in tf.merge_trades([accept, uphold, other]))
    assert groups == [["acc", "uph"], ["x"]]


def test_an_itemless_trade_row_without_a_match_is_kept_not_invented():
    t0 = datetime(2026, 10, 1)
    lone = _rec("lone", [], t0, team=3)
    other = _rec("x", [Leg("trade", 1, 1, 2)], t0 + timedelta(hours=2))
    events = tf.build_events([lone, other], [], is_faab=False)
    assert sorted(len(e["legs"]) for e in events) == [0, 1]


def test_an_itemless_transaction_row_never_absorbs_another_transaction_trade():
    # Regression (PR #231 review): only an activity-feed trade may give an item-less row its legs.
    t0 = datetime(2026, 10, 1)
    itemless = _rec("espn-itemless", [], t0, team=1)
    other = _rec("espn-player", [Leg("trade", 7, 1, 2)], t0 + timedelta(hours=1))
    feed = _rec("topic", [Leg("trade", 8, 1, 3)], t0 + timedelta(hours=3), source="activity")
    groups = sorted(sorted(r.source_id for r in g) for g in tf.merge_trades([itemless, other]))
    assert groups == [["espn-itemless"], ["espn-player"]]
    groups = sorted(sorted(r.source_id for r in g) for g in tf.merge_trades([itemless, other, feed]))
    assert groups == [["espn-itemless", "topic"], ["espn-player"]]
    sleeper = _rec("sleeper:1", [], t0, team=1)
    assert len(tf.merge_trades([sleeper, feed])) == 2          # Sleeper rows are never matched to the feed


def test_trade_items_with_one_side_each_are_joined_per_player():
    items = [{"item_type": "TRADE", "espn_player_id": 7, "from_team_id": 1, "to_team_id": None},
             {"item_type": "TRADE", "espn_player_id": 7, "from_team_id": None, "to_team_id": 2},
             {"item_type": "TRADE", "espn_player_id": 8, "from_team_id": 2, "to_team_id": None}]
    legs = tf._trade_legs(items, 1, {1, 2})
    assert [leg.key() for leg in legs] == [("trade", 7, 1, 2), ("drop", 8, 2, None)]


def test_no_completed_week_since_the_move_means_no_after_annotation():
    cov = {(1, 1), (1, 2)}
    snaps = {(1, 9, 2): {"is_starter": False, "points": 3.0}}
    assert tf.after_move(3, 1, 9, current_week=4, coverage=cov, snaps=snaps) is None
    assert tf.after_move(2, 1, 9, current_week=4, coverage=cov, snaps=snaps) == {
        "weeks": 1, "first_week": 2, "last_week": 2, "on_roster": 1, "started": 0, "starter_points": None}
    assert tf.after_move(None, 1, 9, current_week=4, coverage=cov, snaps=snaps) is None


# -------------------------------------------------------------------- API --


def test_the_route_is_mounted_under_v2_with_league_scope():
    from app.api.league_scope import league_scope
    from app.api.routing import iter_routes
    from app.main import app
    paths = {r.path: r for r in iter_routes(app)}
    assert "/api/transactions" in paths
    v2 = paths["/v2/leagues/{league_id}/api/transactions"]
    assert league_scope in v2.dependency_calls()


def test_the_api_validates_and_answers(espn, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    espn.execute("CHECKPOINT")
    db_path = espn.execute("SELECT path FROM duckdb_databases() WHERE database_name = current_database()").fetchone()[0]
    espn.close()
    monkeypatch.setenv("FANTASY_DB_PATH", db_path)
    for name in ("FANTASY_API_KEY", "MULTILEAGUE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    from app.main import app
    client = TestClient(app)
    ok = client.get("/api/transactions?type=trade&position=WR&sort=week&dir=asc&limit=2")
    assert ok.status_code == 200 and ok.json()["page"]["returned"] == 2
    assert client.get("/api/transactions?manager=99").status_code == 404
    assert client.get("/api/transactions?cursor=tx:nope").status_code == 400
    assert client.get("/api/transactions?season=2019").status_code == 404        # a season this league never synced
    assert client.get("/api/transactions?season=2026").json()["query"]["season"] == 2026
    for bad in ("type=pick", "position=DL", "week=0", "limit=500", "sort=team", "dir=up", "season=abc", "season=1900"):
        assert client.get(f"/api/transactions?{bad}").status_code == 422, bad


# ------------------------------------------------------------ #193 follow-ups --

def _copy_tx(con, src: str, new: str, **sql):
    """Store a copy of transaction `src` (and its items) as `new`; `sql` maps transaction columns to SQL literals (test
    constants, never input). Items are keyed by transaction id only."""
    repl = ", ".join([f"'{new}' AS transaction_id", *(f"{v} AS {k}" for k, v in sql.items())])
    con.execute(f"INSERT INTO transactions SELECT * REPLACE ({repl}) FROM transactions WHERE transaction_id = ?", [src])
    con.execute(f"INSERT INTO transaction_items SELECT * REPLACE ('{new}' AS transaction_id) FROM transaction_items "
                f"WHERE transaction_id = ?", [src])


def test_an_earlier_synced_season_is_readable_and_never_mixed_into_the_latest(espn):
    """A league file keeps every season it synced (upserts keyed by season): the feed reads the latest by default and
    any stored season on request, never both at once, and a season that is not stored is refused, not guessed."""
    espn.execute("INSERT INTO leagues SELECT * REPLACE (2025 AS season, synced_at - INTERVAL 300 DAY AS synced_at) "
                 "FROM leagues")
    espn.execute("INSERT INTO fantasy_teams SELECT * REPLACE (2025 AS season) FROM fantasy_teams")
    _copy_tx(espn, "fa-1", "fa-2025", season="2025")
    latest = tf.build_transactions_feed(espn)
    assert latest["league"]["season"] == 2026 and latest["options"]["seasons"] == [2026, 2025]
    assert "tx:fa-2025" not in {e["event_id"] for e in latest["events"]}
    old = tf.build_transactions_feed(espn, season=2025)
    assert old["league"]["season"] == 2025 and old["query"]["season"] == 2025
    assert [e["event_id"] for e in old["events"]] == ["tx:fa-2025"]
    assert tf.build_transactions_feed(espn, season=2026)["page"]["total"] == latest["page"]["total"]
    with pytest.raises(tf.UnknownSeason):
        tf.build_transactions_feed(espn, season=2019)


def test_a_move_by_an_unknown_or_missing_team_is_kept_without_inventing_one(espn):
    """A stored move whose team is not one of the league's (or is missing) still appears, with no team named: nothing
    is attributed to a team the league does not have."""
    _copy_tx(espn, "fa-1", "fa-foreign", team_id="9")
    espn.execute("UPDATE transaction_items SET to_team_id = 9 WHERE transaction_id = 'fa-foreign'")
    _copy_tx(espn, "fa-1", "fa-noteam", team_id="NULL")
    espn.execute("UPDATE transaction_items SET to_team_id = NULL WHERE transaction_id = 'fa-noteam'")
    events = {e["event_id"]: e for e in tf.build_transactions_feed(espn, limit=100)["events"]}
    for eid in ("tx:fa-foreign", "tx:fa-noteam"):
        e = events[eid]
        assert e["teams"] == [] or all(t is None for t in e["teams"]), eid
        assert all(leg["to_team"] is None for leg in e["legs"]), eid
    # filtering by a real manager never pulls them in
    for manager in (1, 2, 3, 4):
        ids = {e["event_id"] for e in tf.build_transactions_feed(espn, manager=manager, limit=100)["events"]}
        assert not ids & {"tx:fa-foreign", "tx:fa-noteam"}, manager


def test_a_waiver_batch_at_one_time_has_a_fixed_order_and_pages_one_by_one(espn):
    """Claims processed at the same instant have a fixed order (event id; oldest-first is its exact reverse), so paging
    one at a time returns each exactly once."""
    for n in (1, 2, 3):
        _copy_tx(espn, "w-1", f"wb-{n}")
    batch = ["tx:wb-1", "tx:wb-2", "tx:wb-3", "tx:w-1"]
    newest = [e["event_id"] for e in tf.build_transactions_feed(espn, limit=100)["events"] if e["event_id"] in batch]
    oldest = [e["event_id"] for e in tf.build_transactions_feed(espn, direction="asc", limit=100)["events"]
              if e["event_id"] in batch]
    assert sorted(newest) == sorted(batch) and len(newest) == 4
    assert newest == sorted(batch) and oldest == newest[::-1]     # ties by event id; oldest-first is the exact reverse
    seen, cursor = [], None
    while True:
        feed = tf.build_transactions_feed(espn, cursor=cursor, limit=1)
        seen += [e["event_id"] for e in feed["events"]]
        cursor = feed["page"]["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == tf.build_transactions_feed(espn)["page"]["total"]
    assert [x for x in seen if x in batch] == newest


def test_a_cursor_from_before_new_moves_arrived_never_repeats_or_skips_silently(espn):
    """Page 1 is read, then a newer move is stored: page 2 from the old cursor continues after that event without
    repeating it (a newer move sorts first under newest-first, so it is on page 1 of a fresh read, not a repeat)."""
    first = tf.build_transactions_feed(espn, limit=2)
    page1 = [e["event_id"] for e in first["events"]]
    _copy_tx(espn, "fa-1", "fa-new", process_date="TIMESTAMP '2030-01-01 00:00:00'")
    second = tf.build_transactions_feed(espn, cursor=first["page"]["next_cursor"], limit=100)
    page2 = [e["event_id"] for e in second["events"]]
    assert not set(page1) & set(page2)
    assert "tx:fa-new" not in page2
    assert tf.build_transactions_feed(espn, limit=1)["events"][0]["event_id"] == "tx:fa-new"
    # a cursor naming a move that is no longer stored is refused, never answered from a guessed position
    espn.execute("DELETE FROM transaction_items WHERE transaction_id = 'fa-1'")
    espn.execute("DELETE FROM transactions WHERE transaction_id = 'fa-1'")
    with pytest.raises(tf.StaleCursor):
        tf.build_transactions_feed(espn, cursor="tx:fa-1")
