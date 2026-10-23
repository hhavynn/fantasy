"""GET /v2/portfolio (issue #209): one signed-in user's player exposure across their own leagues.

Synthetic data only (invented leagues, teams and players). Cross-tenant guarantees first: the league list is the
caller's own after authorization, a league id in the query is ignored, anonymous is 401, and two users sharing a league
each see only their own saved team's players. Then the identity rules (a player is joined across leagues only on
id-based evidence; ambiguous and duplicate mappings stay apart), the honesty rules (a missing projection or bye week is
unknown, never 0; stale data keeps its own timestamp) and the bounds (1, 5 and 25 leagues; the cap; the budget)."""
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.api import portfolio
from app.api.portfolio import aggregate, link, resolve, split
from cp_seed import U1, U2
from test_league_scope import client, e2e, served  # noqa: F401  (fixtures)
from test_leagues_api import env  # noqa: F401  (fixture)

# ---- pure helpers: synthetic leagues ---------------------------------------------------------------------------------

LEAGUE_IDS = [f"lg_{'portfolio' + str(i):0>24}" for i in range(40)]


def _p(pid, name, *, pos="WR", team="AAA", gsis=None, method=None, notes=None, starter=True, slot="WR", reserve=False,
       injury=None, proj=12.5, origin="provider", bye=9, bye_known=True):
    return {"player_id": pid, "name": name, "position": pos, "nfl_team": team, "slot": slot, "is_starter": starter,
            "reserve": reserve, "injury_status": injury, "projection": proj,
            "projection_origin": None if proj is None else origin, "bye_week": bye, "bye_known": bye_known,
            "map": {"gsis_id": gsis, "match_method": method, "match_confidence": None, "notes": notes}}


def _section(i, players, *, provider="espn", week=8, final=17, synced="2026-10-20T12:00:00Z", state="ready",
             season=2026, team_name=None):
    return {"league": {"league_id": LEAGUE_IDS[i], "provider": provider, "name": f"Invented League {i}",
                       "season": season},
            "state": state, "reason": None, "stale": False, "team": {"team_id": 3, "name": team_name or f"Team {i}"},
            "week": week, "final_week": final, "synced_at": synced, "_players": players}


def _by_name(players):
    return {p["name"]: p for p in players}


def _exposure(i, p, provider="espn"):
    return {**p, "league_id": LEAGUE_IDS[i], "provider": provider}


VERIFIED = dict(gsis="00-0000001", method="espn_id:nfl_players")


# ---- identity -----------------------------------------------------------------------------------------------------------

def test_a_player_is_joined_across_providers_only_on_a_verified_gsis_link():
    sections = [
        _section(0, [_p(101, "Avery Example", **VERIFIED)]),
        _section(1, [_p(101, "Avery Example", gsis="00-0000001", method="espn_id:ff_playerids")], provider="sleeper"),
        # same name and team, no id link: never joined by name
        _section(2, [_p(-(10_000_000 + 77), "Avery Example")], provider="sleeper"),
        _section(3, [_p(202, "Blake Sample", gsis="00-0000002", method="name_position_team")]),
    ]
    players, _, cov = aggregate(sections)
    joined = [p for p in players if p["key"] == "gsis:00-0000001"]
    assert len(joined) == 1 and joined[0]["leagues"] == 2 and joined[0]["of"] == 4
    assert joined[0]["identity"] == {"status": "verified", "gsis_id": "00-0000001", "reason": None}
    alone = [p for p in players if p["name"] == "Avery Example" and p["key"] != "gsis:00-0000001"]
    assert len(alone) == 1 and alone[0]["leagues"] == 1 and alone[0]["identity"]["status"] == "unverified"
    name_only = _by_name(players)["Blake Sample"]
    assert name_only["identity"]["gsis_id"] is None and "name only" in name_only["identity"]["reason"]
    assert cov["players"] == 3 and cov["exposures"] == 4


def test_name_only_matches_are_never_linked_even_with_the_same_gsis():
    a = _exposure(0, _p(301, "Casey Placeholder", gsis="00-0000003", method="name_position_team"))
    b = _exposure(1, _p(302, "Casey Placeholder", gsis="00-0000003", method="name_position_team"), "sleeper")
    resolve([a, b])
    assert a["key"] != b["key"] and a["identity"]["gsis_id"] is None and b["identity"]["gsis_id"] is None


def test_a_crosswalk_the_map_marks_conflicting_stays_apart():
    a = _exposure(0, _p(401, "Drew Dummy", gsis="00-0000004", method="espn_id:nfl_players",
                        notes="Conflicting crosswalks: 00-0000004 (nfl_players), 00-0000009 (ff_playerids)"))
    b = _exposure(1, _p(401, "Drew Dummy", gsis="00-0000004", method="espn_id:nfl_players"), "sleeper")
    resolve([a, b])
    assert a["identity"]["status"] == "conflict" and a["identity"]["gsis_id"] is None
    assert b["identity"]["status"] == "verified"                    # the other league's own evidence still stands
    assert a["key"] != b["key"]


def test_one_espn_player_is_one_row_when_only_one_espn_league_marks_his_crosswalk_conflicting():
    """#300 review: one ESPN id in two ESPN leagues, one league's map conflicting and the other's verified, is one ESPN
    player. Both join by the ESPN id (no NFL link), so he is never two rows or counted twice on a bye."""
    conflicting = _p(401, "Drew Dummy", gsis="00-0000004", method="espn_id:nfl_players",
                     notes="Conflicting crosswalks: 00-0000004 (nfl_players), 00-0000009 (ff_playerids)")
    players, weeks, coverage = aggregate([_section(0, [conflicting]),
                                          _section(1, [_p(401, "Drew Dummy", gsis="00-0000004",
                                                          method="espn_id:nfl_players")])])
    assert len(players) == 1 and coverage["players"] == 1
    drew = players[0]
    assert drew["key"] == "espn:401" and drew["leagues"] == 2 and drew["identity"]["gsis_id"] is None
    bye = next(w for w in weeks if w["week"] == 9)["bye"]
    assert bye["players"] == 1 and bye["names"].count("Drew Dummy") == 1


def test_one_player_id_linked_to_two_gsis_ids_is_kept_apart():
    a = _exposure(0, _p(501, "Emery Fake", gsis="00-0000005", method="espn_id:nfl_players"))
    b = _exposure(1, _p(501, "Emery Fake", gsis="00-0000006", method="espn_id:nfl_players"), "sleeper")
    resolve([a, b])
    assert {a["identity"]["status"], b["identity"]["status"]} == {"conflict"}
    assert a["key"] != b["key"] and "gsis" not in a["key"] and "gsis" not in b["key"]


def test_two_player_ids_reaching_one_gsis_are_a_duplicate_mapping_kept_apart():
    a = _exposure(0, _p(601, "Finley Test", gsis="00-0000007", method="espn_id:nfl_players"))
    b = _exposure(1, _p(602, "Finley Test Jr", gsis="00-0000007", method="espn_id:ff_playerids"))
    c = _exposure(2, _p(603, "Other", gsis="00-0000008", method="espn_id:nfl_players"))
    resolve([a, b, c])
    assert a["identity"]["status"] == b["identity"]["status"] == "conflict"
    assert a["key"] == "espn:601" and b["key"] == "espn:602"         # each stays its own ESPN player
    assert c["key"] == "gsis:00-0000008"


def test_within_one_league_a_duplicate_mapping_never_double_counts():
    players, _, _ = aggregate([_section(0, [_p(701, "Gale A", gsis="00-0000010", method="espn_id:nfl_players"),
                                            _p(702, "Gale B", gsis="00-0000010", method="espn_id:nfl_players")])])
    assert sorted(p["leagues"] for p in players) == [1, 1]
    assert all(p["identity"]["status"] == "conflict" for p in players)


def test_espn_leagues_join_by_espn_id_and_take_a_gsis_only_from_espn_evidence():
    a = _exposure(0, _p(801, "Harper Mock", **{"gsis": "00-0000011", "method": "espn_id:nfl_players"}))
    b = _exposure(1, _p(801, "Harper Mock"))                              # this league's map has no row for him
    c = _exposure(2, _p(802, "Indy Stub"))
    d = _exposure(3, _p(802, "Indy Stub"))
    e = _exposure(4, _p(802, "Indy Stub"), "sleeper")                     # Sleeper's ESPN-space id is a translation
    resolve([a, b, c, d, e])
    assert a["key"] == b["key"] == "gsis:00-0000011"
    assert b["identity"]["status"] == "verified" and "another" in b["identity"]["reason"]
    assert c["key"] == d["key"] == "espn:802" and c["identity"]["status"] == "espn_id"
    assert e["key"].startswith("league:") and e["identity"]["status"] == "unverified"


def test_team_defenses_join_by_nfl_team_only_with_a_defense_id():
    a = _exposure(0, _p(-16001, "Invented D/ST", pos="D/ST", team="AAA"))
    b = _exposure(1, _p(-16001, "Invented D/ST", pos="D/ST", team="AAA"), "sleeper")
    c = _exposure(2, _p(-(10_000_000 + 5), "Synthetic Guy", pos="WR", team="AAA"), "sleeper")
    resolve([a, b, c])
    assert a["key"] == b["key"] == "dst:AAA" and a["identity"]["status"] == "team_defense"
    assert c["key"].startswith("league:")                                   # never a defense, never joined


def test_link_rejects_a_malformed_gsis_without_failing():
    assert link(_p(901, "Jules", gsis="not-a-gsis", method="espn_id:nfl_players"))[0] == "unverified"


# ---- exact counts, unknowns and weeks ------------------------------------------------------------------------------------

def test_counts_are_exact_and_missing_values_stay_unknown():
    s0 = _section(0, [_p(101, "Kai Fixture", **VERIFIED, proj=None, starter=False),
                      _p(102, "Lee Fixture", bye=None, bye_known=False)])
    s1 = _section(1, [_p(101, "Kai Fixture", **VERIFIED, proj=0.0, origin="model")], provider="sleeper")
    s2 = _section(2, [], state="needs_team")
    players, weeks, cov = aggregate([s0, s1, s2])
    kai, lee = _by_name(players)["Kai Fixture"], _by_name(players)["Lee Fixture"]
    assert (kai["leagues"], kai["of"], kai["teams"], kai["starters"]) == (2, 2, 2, 1)
    assert kai["projections"] == {"known": 1, "missing": 1}
    by_league = {e["league_id"]: e for e in kai["exposures"]}
    assert by_league[LEAGUE_IDS[0]]["projection"] is None                  # unknown, not 0
    assert by_league[LEAGUE_IDS[1]]["projection"] == 0.0                   # the model's measured zero stays a zero
    assert lee["bye_week"] is None and lee["bye_reason"]
    assert cov == {"leagues_ready": 2, "leagues_total": 3, "exposures": 3, "players": 2,
                   "identity": {"verified": 1, "espn_id": 1}, "projections_known": 2, "projections_missing": 1,
                   "bye_unknown": 1}
    nine = next(w for w in weeks if w["week"] == 9)
    assert nine["bye"]["exposures"] == 2 and nine["bye"]["players"] == 1 and nine["bye"]["unknown"] == 1


def test_bye_concentration_counts_active_players_in_leagues_still_playing():
    s0 = _section(0, [_p(1, "Ari", bye=10, starter=True), _p(2, "Bo", bye=10, starter=False),
                      _p(3, "Cy", bye=10, reserve=True, slot="IR"), _p(4, "Di", bye=11)], week=9, final=10)
    s1 = _section(1, [_p(5, "Ed", bye=11), _p(6, "Fi", bye=10)], week=9, final=14)
    _, weeks, _ = aggregate([s0, s1])
    by = {w["week"]: w for w in weeks}
    assert [w["week"] for w in weeks] == list(range(9, 15))
    assert by[10]["bye"] == {"exposures": 3, "players": 3, "leagues": 2, "starters": 2, "unknown": 0,
                             "names": ["Ari", "Bo", "Fi"]}                 # Cy is on IR: not lineup-relevant
    assert by[11]["leagues_playing"] == 1 and by[11]["bye"]["names"] == ["Ed"]   # league 0's season ended at week 10


def test_injury_designations_are_counted_only_in_each_leagues_own_week():
    s0 = _section(0, [_p(1, "Gus", injury="OUT"), _p(2, "Hal", injury="ACTIVE"), _p(3, "Ida", injury="QUESTIONABLE")],
                  week=8)
    s1 = _section(1, [_p(1, "Gus", injury="OUT")], week=7, synced="2026-10-13T12:00:00Z")   # an older snapshot
    players, weeks, _ = aggregate([s0, s1])
    by = {w["week"]: w for w in weeks}
    assert by[7]["injuries"] == {"exposures": 1, "players": 1, "leagues": 1, "leagues_reporting": 1}
    assert by[8]["injuries"] == {"exposures": 2, "players": 2, "leagues": 1, "leagues_reporting": 1}
    assert by[9]["injuries"] is None                                       # never projected forward
    gus = _by_name(players)["Gus"]                                           # one ESPN id in two ESPN leagues
    assert gus["leagues"] == 2 and gus["injury"] == {"designated": 2, "statuses": ["OUT"]}


def test_each_exposure_keeps_its_own_leagues_timestamp():
    s0 = _section(0, [_p(101, "Jo", **VERIFIED)], synced="2026-10-20T12:00:00Z")
    s1 = _section(1, [_p(101, "Jo", **VERIFIED)], synced="2026-09-01T12:00:00Z")
    players, _, _ = aggregate([s0, s1])
    stamps = {e["league_id"]: e["synced_at"] for e in players[0]["exposures"]}
    assert stamps == {LEAGUE_IDS[0]: "2026-10-20T12:00:00Z", LEAGUE_IDS[1]: "2026-09-01T12:00:00Z"}
    assert players[0]["exposures"][0]["league_id"] == LEAGUE_IDS[0]         # newest data first


def test_a_stale_snapshot_reads_as_stale_in_its_provenance():
    from app.api.signal_provenance import payload, signal, status
    now = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
    old = signal("league", "Your roster", "espn", now - timedelta(days=5), "v1")
    fresh = signal("league", "Your roster", "espn", now - timedelta(hours=2), "v1")
    degraded = signal("league", "Your roster", "espn", now - timedelta(hours=2), "v1", stale_reason="older files")
    assert [status(s, now) for s in payload(old, fresh, degraded)["signals"]] == ["stale", "current", "stale"]


@pytest.mark.parametrize("n", [1, 5, 25])
def test_one_five_and_twenty_five_leagues_aggregate_exactly_and_quickly(n):
    shared = [_p(1000 + k, f"Shared {k}", gsis=f"00-{1000 + k:07d}", method="espn_id:nfl_players") for k in range(8)]
    sections = []
    for i in range(n):
        own = [_p(5000 + i * 10 + k, f"Own {i}-{k}") for k in range(8)]
        sections.append(_section(i, [dict(p) for p in shared] + own, provider="espn" if i % 2 else "sleeper"))
    t0 = time.perf_counter()
    players, weeks, cov = aggregate(sections)
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, elapsed
    assert cov["leagues_ready"] == n and cov["exposures"] == 16 * n
    shared_rows = [p for p in players if p["name"].startswith("Shared")]
    assert len(shared_rows) == 8 and all(p["leagues"] == n and p["of"] == n for p in shared_rows)
    assert len(players) == 8 + 8 * n and weeks


# ---- bounds ---------------------------------------------------------------------------------------------------------------

def _access(i, season=2026, member=True):
    return SimpleNamespace(league_id=LEAGUE_IDS[i], season=season, provider="espn", is_member=member,
                           to_json=lambda i=i, season=season: {"league_id": LEAGUE_IDS[i], "season": season,
                                                               "provider": "espn", "name": f"Invented League {i}"})


def test_only_the_newest_season_is_combined_and_the_cap_is_listed():
    granted = [_access(i) for i in range(27)] + [_access(30, season=2025)]
    read, excluded = split(granted, 25)
    assert [a.league_id for a in read] == LEAGUE_IDS[:25]
    assert sorted((x["league"]["league_id"], x["reason"]) for x in excluded) == sorted(
        [(LEAGUE_IDS[25], "over_limit"), (LEAGUE_IDS[26], "over_limit"), (LEAGUE_IDS[30], "other_season")])


def test_a_public_league_the_caller_only_follows_is_listed_not_counted():
    """A pinned public league with no membership is a viewing perspective, not a team the caller manages: it is
    excluded with its reason and never counted, and it never decides which season is combined."""
    granted = [_access(0), _access(1, member=False), _access(2, season=2027, member=False)]
    read, excluded = split(granted, 25)
    assert [a.league_id for a in read] == [LEAGUE_IDS[0]]
    assert sorted((x["league"]["league_id"], x["reason"]) for x in excluded) == sorted(
        [(LEAGUE_IDS[1], "not_member"), (LEAGUE_IDS[2], "not_member")])
    assert split([_access(1, member=False)], 25) == ([], [{"league": _access(1).to_json(), "reason": "not_member"}])


@pytest.fixture
def _fresh_pool(monkeypatch):
    from app.api import action_center
    monkeypatch.setattr(action_center, "_POOLS", {})
    monkeypatch.setattr(action_center, "_FLIGHTS", {})
    monkeypatch.setattr(action_center, "_ADMISSION", {})
    monkeypatch.setattr(action_center, "_HOLDERS", {})


def test_at_most_twenty_five_leagues_are_read_with_bounded_concurrency(monkeypatch, _fresh_pool):
    import asyncio
    import threading
    monkeypatch.setenv("PORTFOLIO_MAX_LEAGUES", "100")                      # clamped to 25 whatever the environment says
    lock, live, peak, seen = threading.Lock(), [0], [0], []

    def fake(access):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            seen.append(access.league_id)
        time.sleep(0.02)
        with lock:
            live[0] -= 1
        return {"league": access.to_json(), "state": "not_ready", "reason": "x", "stale": False}

    monkeypatch.setattr(portfolio, "evaluate_league", fake)
    body, degraded = asyncio.run(portfolio.build(SimpleNamespace(user_id="u"), [_access(i) for i in range(30)]))
    assert body["limit"] == 25 and len(body["leagues"]) == 25 and len(seen) == 25
    assert body["excluded"]["count"] == 5 and peak[0] <= portfolio.CONCURRENCY and degraded is False
    assert body["players"] == [] and body["coverage"]["leagues_ready"] == 0


def test_a_league_past_the_budget_is_reported_not_dropped(monkeypatch, _fresh_pool):
    import asyncio
    monkeypatch.setenv("PORTFOLIO_BUDGET_S", "1")

    def fake(access):
        if access.league_id == LEAGUE_IDS[1]:
            time.sleep(2.0)
        return {"league": access.to_json(), "state": "needs_team", "reason": "x", "stale": False}

    monkeypatch.setattr(portfolio, "evaluate_league", fake)
    t0 = time.monotonic()
    body, _ = asyncio.run(portfolio.build(SimpleNamespace(user_id="u"), [_access(0), _access(1)]))
    assert time.monotonic() - t0 < 1.9
    by = {s["league"]["league_id"]: s for s in body["leagues"]}
    assert by[LEAGUE_IDS[1]]["state"] == "not_ready" and by[LEAGUE_IDS[1]]["reason"] == portfolio.TOO_SLOW
    assert by[LEAGUE_IDS[0]]["state"] == "needs_team"


def test_nothing_league_identifying_is_logged_when_a_league_fails(caplog):
    class Boom:
        league_id = LEAGUE_IDS[3]
        provider = "espn"

        @property
        def snapshot(self):
            raise RuntimeError(f"secret {LEAGUE_IDS[3]} Invented League 3")

        def to_json(self):
            return {"league_id": self.league_id}

    with caplog.at_level("WARNING"):
        out = portfolio.evaluate_league(Boom())
    assert out["state"] == "unavailable"
    assert LEAGUE_IDS[3] not in caplog.text and "Invented" not in caplog.text and "RuntimeError" in caplog.text


# ---- through the API: authorization and isolation ----------------------------------------------------------------------

URL = "/v2/portfolio"


def _api_member(pg, uid, league_id, role="viewer"):
    from cp_seed import seed_membership, seed_user
    with pg.connect() as con:
        seed_user(con, uid)
        seed_membership(con, uid, league_id, role)
        con.commit()


def _api_pin(pg, uid, league_id):
    from cp_seed import seed_user
    with pg.connect() as con:
        seed_user(con, uid)
        con.execute("INSERT INTO fi.league_pins (user_id, league_id) VALUES (%s, %s)", [uid, league_id])
        con.commit()


def _api_team(pg, uid, league_id, team):
    with pg.connect() as con:
        con.execute("INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, %s)",
                    [uid, league_id, str(team)])
        con.commit()


def _roster_ids(db, team) -> set[int]:
    import duckdb
    con = duckdb.connect(str(db), read_only=True)
    try:
        lid, season, week = con.execute("SELECT league_id, season, current_week FROM leagues").fetchone()
        return {r[0] for r in con.execute("SELECT espn_player_id FROM lineup_snapshots WHERE league_id = ? AND "
                                          "season = ? AND scoring_period = ? AND team_id = ?",
                                          [lid, season, week, team]).fetchall()}
    finally:
        con.close()


def _other_team(db, team) -> int:
    import duckdb
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute("SELECT min(team_id) FROM fantasy_teams WHERE team_id <> ?", [team]).fetchone()[0]
    finally:
        con.close()


def _exposure_ids(body, league_id):
    return {e["player_id"] for p in body["players"] for e in p["exposures"] if e["league_id"] == league_id}


@pytest.fixture
def _fresh_api(_fresh_pool):
    yield


def test_anonymous_and_unusable_tokens_are_401_and_flag_off_is_404(client, monkeypatch, _fresh_api):
    from test_leagues_api import bearer
    assert (client.get(URL).status_code, client.get(URL).json()) == (401, {"detail": "Sign in to use this endpoint."})
    bad = {"Authorization": "Bearer " + "x" * 40 + "." + "y" * 40 + "." + "z" * 40}
    assert client.get(URL, headers=bad).status_code == 401
    monkeypatch.setenv("MULTILEAGUE_ENABLED", "0")
    assert client.get(URL, headers=bearer(U1)).status_code == 404


def test_league_ids_in_the_request_are_ignored_and_a_non_members_league_never_appears(
        pg, served, client, e2e, tmp_path, _fresh_api):
    from league_fixtures import publish_local
    from test_leagues_api import bearer
    from test_league_scope import my_team
    other = publish_local(pg, e2e, served, tmp_path / "o", visibility="private", name="Someone Else's")
    _api_member(pg, U2, other.league_id)
    _api_team(pg, U2, other.league_id, my_team(e2e))
    with pg.connect() as con:
        from cp_seed import seed_user
        seed_user(con, U1)
        con.commit()
    r = client.get(f"{URL}?league_id={other.league_id}&leagues={other.league_id}&league={other.league_id}",
                   headers=bearer(U1))
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store"
    body = r.json()
    assert body["leagues"] == [] and body["players"] == [] and body["excluded"]["count"] == 0
    assert other.league_id not in r.text and "Someone Else" not in r.text


def test_two_users_sharing_a_league_see_only_their_own_rosters_exposure(pg, served, client, e2e, tmp_path, _fresh_api):
    from league_fixtures import publish_local
    from test_leagues_api import bearer
    from test_league_scope import my_team
    shared = publish_local(pg, e2e, served, tmp_path / "s", visibility="private", name="Shared Invented")
    mine, theirs = my_team(e2e), _other_team(e2e, my_team(e2e))
    for uid, team in ((U1, mine), (U2, theirs)):
        _api_member(pg, uid, shared.league_id)
        _api_team(pg, uid, shared.league_id, team)
    a, b = client.get(URL, headers=bearer(U1)).json(), client.get(URL, headers=bearer(U2)).json()
    assert _exposure_ids(a, shared.league_id) == _roster_ids(e2e, mine) != set()
    assert _exposure_ids(b, shared.league_id) == _roster_ids(e2e, theirs) != set()
    assert all(e["team_id"] == mine for p in a["players"] for e in p["exposures"])
    assert all(e["team_id"] == theirs for p in b["players"] for e in p["exposures"])
    assert a["leagues"][0]["team"]["team_id"] == mine and b["leagues"][0]["team"]["team_id"] == theirs


def test_the_same_roster_in_two_leagues_counts_each_player_twice_with_each_leagues_data(
        pg, served, client, e2e, tmp_path, _fresh_api):
    import duckdb
    from league_fixtures import publish_local
    from test_leagues_api import bearer
    from test_league_scope import my_team
    from app.api.signal_provenance import iso_utc
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="private", name="Invented A")
    b = publish_local(pg, e2e, served, tmp_path / "b", visibility="public", name="Invented B", external_id="4242")
    c = publish_local(pg, e2e, served, tmp_path / "c", visibility="public", name="Invented C", external_id="4343")
    _api_member(pg, U1, a.league_id)
    _api_member(pg, U1, b.league_id)
    _api_pin(pg, U1, c.league_id)                               # followed only: listed, never counted
    for lg in (a, b, c):
        _api_team(pg, U1, lg.league_id, my_team(e2e))
    r = client.get(URL, headers=bearer(U1))
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["contract"] == "portfolio.v1" and [s["state"] for s in body["leagues"]] == ["ready", "ready"]
    assert [(x["league"]["league_id"], x["reason"]) for x in body["excluded"]["leagues"]] == [(c.league_id, "not_member")]
    roster = _roster_ids(e2e, my_team(e2e))
    assert body["coverage"]["exposures"] == 2 * len(roster)
    joined = [p for p in body["players"] if p["identity"]["status"] in ("verified", "team_defense", "espn_id")]
    assert joined and all(p["leagues"] == 2 and p["of"] == 2 and p["teams"] == 2 for p in joined)
    assert all(p["leagues"] == 1 for p in body["players"] if p["identity"]["status"] in ("unverified", "conflict"))
    con = duckdb.connect(str(e2e), read_only=True)
    synced = iso_utc(con.execute("SELECT synced_at FROM leagues").fetchone()[0])
    con.close()
    for s in body["leagues"]:
        assert s["synced_at"] == synced and s["versions"]["data_version"] in (a.data_version, b.data_version)
        assert s["provenance"]["contract"] == "signal-provenance.v1"
        assert s["league"]["league_id"] not in str(s["provenance"])
        assert s["projection"]["known"] + (s["roster_size"] - s["projection"]["known"]) == len(roster)
    for p in body["players"]:                                   # a missing projection is null, never 0 by default
        for e in p["exposures"]:
            assert e["projection"] is None or e["projection_origin"] in ("provider", "model")
    assert {w["week"] for w in body["weeks"]} and body["weeks"][0]["week"] == body["leagues"][0]["week"]


def test_a_degraded_read_is_marked_stale(pg, served, client, e2e, tmp_path, _fresh_api):
    import psycopg
    from app.db.league_files import league_key
    from app.db.league_publish import DirWriter, publish_league
    from league_fixtures import publish_local
    from test_leagues_api import bearer
    from test_league_scope import my_team
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="private", name="Slow Invented")
    _api_member(pg, U1, pub.league_id)
    _api_team(pg, U1, pub.league_id, my_team(e2e))
    assert client.get(URL, headers=bearer(U1)).json()["leagues"][0]["stale"] is False
    with psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None) as sync:
        res = publish_league(e2e, DirWriter(served), sync, pub.league_id, stamp="2026-10-21T12:00:00")
    (served / league_key(pub.league_id, res.data_version)).unlink()
    r = client.get(URL, headers=bearer(U1))
    s = r.json()["leagues"][0]
    assert s["state"] == "ready" and s["stale"] is True and r.headers["x-fi-degraded"] == "stale-files"
    sig = next(x for x in s["provenance"]["signals"] if x["signal"] == "league")
    assert sig["stale_reason"] and sig["snapshot_version"] == pub.data_version
    assert all(e["stale"] for p in r.json()["players"] for e in p["exposures"])


def test_twenty_five_leagues_are_read_and_the_rest_listed_through_the_api(pg, client, served, monkeypatch, _fresh_api):
    """25 is also the control plane's per-user league limit, so the API is driven with 25 leagues, then with a lower
    cap and one league moved to last season to see both exclusions."""
    from cp_seed import seed_league
    from test_leagues_api import bearer
    with pg.connect() as con:
        ids = [seed_league(con, visibility="private", checked_at=None, name=f"Invented {i}") for i in range(25)]
        con.commit()
    for lg in ids:
        _api_member(pg, U1, lg)
    t0 = time.monotonic()
    body = client.get(URL, headers=bearer(U1)).json()
    assert time.monotonic() - t0 < 10
    assert body["limit"] == 25 and len(body["leagues"]) == 25 and body["excluded"]["count"] == 0
    assert body["season"] == 2026 and sorted(s["league"]["league_id"] for s in body["leagues"]) == sorted(ids)
    assert all(s["state"] == "not_ready" and s["reason"] for s in body["leagues"])       # no snapshot: said in words
    with pg.connect() as con:
        con.execute("UPDATE fi.leagues SET season = 2025 WHERE id = %s", [ids[0]])
        con.commit()
    monkeypatch.setenv("PORTFOLIO_MAX_LEAGUES", "20")
    body = client.get(URL, headers=bearer(U1)).json()
    assert body["limit"] == 20 and len(body["leagues"]) == 20
    reasons = sorted(x["reason"] for x in body["excluded"]["leagues"])
    assert reasons == ["other_season"] + ["over_limit"] * 4
    shown = [s["league"]["league_id"] for s in body["leagues"]] + [x["league"]["league_id"] for x in body["excluded"]["leagues"]]
    assert sorted(shown) == sorted(ids)


def test_an_espn_and_a_sleeper_league_join_only_on_verified_ids_and_keep_their_own_words(
        pg, served, client, e2e, tmp_path, monkeypatch, _fresh_api):
    import importlib.util
    import re
    import sys
    from pathlib import Path
    from league_fixtures import publish_local
    from test_leagues_api import bearer
    from test_league_scope import my_team
    monkeypatch.setenv("SLEEPER_SOURCE", "live")
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location("build_e2e_sleeper", scripts / "build_e2e_sleeper.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sleeper_db = tmp_path / "sleeper.duckdb"
        mod.build(sleeper_db)
    finally:
        sys.path.remove(str(scripts))
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="public", name="Invented ESPN")
    s = publish_local(pg, sleeper_db, served, tmp_path / "s", visibility="public", name="Invented Sleeper",
                      provider="sleeper", stamp="2026-10-20T13:00:00")
    for lg, team in ((a, my_team(e2e)), (s, 1)):
        _api_member(pg, U1, lg.league_id)
        _api_team(pg, U1, lg.league_id, team)
    r = client.get(URL, headers=bearer(U1))
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    by = {x["league"]["provider"]: x for x in body["leagues"]}
    assert by["espn"]["state"] == by["sleeper"]["state"] == "ready", [x["reason"] for x in body["leagues"]]
    assert re.findall(r'"([^"]*ESPN[^"]*)"', str({k: by["sleeper"][k] for k in ("projection", "provenance", "reason")})) == []
    for p in body["players"]:
        providers = {e["provider"] for e in p["exposures"]}
        assert len({e["league_id"] for e in p["exposures"]}) == len(p["exposures"]) == p["leagues"]   # one per league
        if providers == {"espn", "sleeper"}:                   # a cross-provider row rests on an id, never a name
            assert p["identity"]["status"] in ("verified", "team_defense"), p["identity"]
        for e in p["exposures"]:
            if e["provider"] == "sleeper":
                assert e["projection_origin"] != "provider"   # Sleeper publishes no projections
                assert e["identity"]["status"] != "espn_id"   # Sleeper's ESPN-space ids are translations
