"""/v2 connect flows (Phase 3b): public import, private connect with cookies, the caller's credential, invites,
refresh, delete and the probe on an expired public league. ESPN is a fake; the drain dispatch is recorded."""
import json
import uuid

import pytest

from app.control_plane.credential_crypto import EncryptedCredential, PrivateKeyring, generate_keypair
from app.sync import espn_source
from cp_seed import U1, U2, U3, seed_league, seed_membership, seed_user
from test_leagues_api import bearer, env  # noqa: F401  (fixture)

S2 = "AEB" + "s2cookievalue" * 4
SWID = "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}"
OTHER_SWID = "{11111111-2222-3333-4444-555555555555}"
PRIV, PUB = generate_keypair()


def league_payload():
    return {"id": 4242, "seasonId": 2026, "settings": {"name": "Secret League"},
            "teams": [{"id": 1, "name": "Alpha", "owners": [SWID]}, {"id": 2, "name": "Bravo", "owners": [OTHER_SWID]}]}


class FakeEspn:
    def __init__(self):
        self.anonymous = "public"
        self.cookie_verdict = "ok"
        self.reads = []

    def probe(self, league_id, season):
        return self.anonymous

    def read_league(self, league_id, season, credential=None, *, views=("mSettings",), timeout=30):
        self.reads.append({"league": league_id, "season": season, "cookies": dict(credential or {})})
        if self.cookie_verdict != "ok":
            return self.cookie_verdict, None
        return "ok", league_payload()

    league_teams = staticmethod(espn_source.league_teams)


@pytest.fixture
def espn():
    return FakeEspn()


@pytest.fixture
def dispatched():
    return []


@pytest.fixture
def client(env, monkeypatch, espn, dispatched):
    from fastapi.testclient import TestClient

    from app.api import connect
    from app.main import app
    monkeypatch.setenv("MULTILEAGUE_CONNECT_ENABLED", "1")
    monkeypatch.setenv("CREDENTIALS_PUBKEY_V1", PUB)
    monkeypatch.setattr(connect, "dispatch_drain", lambda: dispatched.append(1) or True)
    app.dependency_overrides[connect.espn] = lambda: espn
    app.dependency_overrides[connect.drain_dispatcher] = lambda: (lambda: dispatched.append(1) or True)
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def users(pg):
    with pg.connect() as su:
        for u in (U1, U2, U3):
            seed_user(su, u)
        su.commit()


def one(pg, sql, params=()):
    with pg.connect() as su:
        return su.execute(sql, params).fetchone()


COOKIES = {"espn_s2": S2, "swid": SWID, "consent": True}


def test_everything_is_404_with_the_connect_flag_off(client, monkeypatch, users):
    monkeypatch.delenv("MULTILEAGUE_CONNECT_ENABLED")
    assert client.post("/v2/leagues/import", json={"league": "4242"}, headers=bearer(U1)).status_code == 404


def test_a_public_league_is_imported_pinned_and_queued(client, pg, users, espn, dispatched):
    r = client.post("/v2/leagues/import", json={"league": "https://fantasy.espn.com/football/league?leagueId=4242&seasonId=2026"},
                    headers=bearer(U1))
    assert r.status_code == 201, r.text
    lid = r.json()["league_id"]
    assert one(pg, "SELECT external_id, season, visibility FROM fi.leagues WHERE id = %s", [lid]) == ("4242", 2026, "public")
    assert one(pg, "SELECT kind, status FROM fi.sync_jobs WHERE league_id = %s", [lid]) == ("initial", "queued")
    assert one(pg, "SELECT count(*) FROM fi.league_pins WHERE league_id = %s", [lid]) == (1,)
    assert dispatched == [1], "the drain is started once the job is committed"
    assert espn.reads == [], "a public import never sends cookies"


@pytest.mark.parametrize("verdict, status", [("private", 409), ("missing", 404), ("unknown", 503)])
def test_import_answers_what_espn_said(client, users, espn, dispatched, verdict, status):
    espn.anonymous = verdict
    r = client.post("/v2/leagues/import", json={"league": "4242", "season": 2026}, headers=bearer(U1))
    assert r.status_code == status
    if verdict == "private":
        assert r.json()["needs_cookies"] is True
    assert dispatched == []


def test_import_needs_sign_in_and_a_league_id(client, users):
    assert client.post("/v2/leagues/import", json={"league": "4242"}).status_code == 401
    assert client.post("/v2/leagues/import", json={"league": "not a league"}, headers=bearer(U1)).status_code == 422


def test_a_private_connect_stores_encrypted_cookies_admin_and_the_owned_team(client, pg, users, espn, dispatched):
    espn.anonymous = "private"
    r = client.post("/v2/leagues/connect", json={"league": "4242", "season": 2026, **COOKIES}, headers=bearer(U1))
    assert r.status_code == 201, r.text
    body = r.json()
    assert S2 not in r.text and "AAAAAAAA" not in r.text, "cookies are never echoed"
    assert (body["visibility"], body["cookies"], body["team_id"]) == ("private", "stored", "1")
    assert body["teams"] == [{"team_id": "1", "name": "Alpha"}, {"team_id": "2", "name": "Bravo"}]
    lid = body["league_id"]
    assert one(pg, "SELECT role FROM fi.league_memberships WHERE league_id = %s AND user_id = %s", [lid, U1]) == ("admin",)
    assert one(pg, "SELECT team_external_id FROM fi.team_selections WHERE league_id = %s", [lid]) == ("1",)
    assert one(pg, "SELECT kind FROM fi.sync_jobs WHERE league_id = %s", [lid]) == ("initial",)
    row = one(pg, "SELECT id::text, ciphertext, nonce, aad_format, wrapped_data_key, wrap_key_version, owner_user_id::text "
                  "FROM fi.league_credentials WHERE league_id = %s", [lid])
    assert S2.encode() not in bytes(row[1])
    enc = EncryptedCredential(bytes(row[1]), bytes(row[2]), row[3], bytes(row[4]), row[5])
    assert PrivateKeyring({"v1": PRIV}).decrypt(enc, league_id=lid, credential_id=row[0], owner_user_id=row[6]) == \
        {"espn_s2": S2, "swid": SWID}
    assert espn.reads[0]["cookies"] == {"espn_s2": S2, "swid": SWID}
    assert dispatched == [1]
    assert one(pg, "SELECT count(*) FROM fi.audit_log WHERE meta::text LIKE %s", [f"%{S2[:12]}%"]) == (0,)


def test_connecting_a_league_that_is_public_imports_it_and_drops_the_cookies(client, pg, users, espn):
    r = client.post("/v2/leagues/connect", json={"league": "4242", **COOKIES}, headers=bearer(U1))
    assert r.status_code == 201 and r.json()["cookies"] == "not_needed"
    assert one(pg, "SELECT count(*) FROM fi.league_credentials") == (0,)
    assert espn.reads == []


def test_cookies_espn_refuses_store_nothing(client, pg, users, espn, dispatched):
    espn.anonymous, espn.cookie_verdict = "private", "private"
    r = client.post("/v2/leagues/connect", json={"league": "4242", **COOKIES}, headers=bearer(U1))
    assert r.status_code == 400
    assert one(pg, "SELECT count(*) FROM fi.leagues") == (0,) and dispatched == []


@pytest.mark.parametrize("body", [
    {"league": "4242", "espn_s2": S2, "swid": SWID},                       # no consent
    {"league": "4242", "espn_s2": S2, "swid": "not-a-swid", "consent": True},
    {"league": "4242", "espn_s2": ["list", S2], "swid": SWID, "consent": True},
])
def test_bad_input_is_refused_without_echoing_the_cookies(client, users, espn, body):
    espn.anonymous = "private"
    r = client.post("/v2/leagues/connect", json=body, headers=bearer(U1))
    assert r.status_code == 422 and S2 not in r.text and "AAAAAAAA" not in r.text
    r = client.post("/v2/leagues/connect", content=json.dumps(body)[:-3], headers={**bearer(U1), "content-type": "application/json"})
    assert r.status_code == 422 and S2 not in r.text


def test_a_second_owner_joins_as_viewer_without_replacing_the_credential(client, pg, users, espn, dispatched):
    espn.anonymous = "private"
    lid = client.post("/v2/leagues/connect", json={"league": "4242", **COOKIES}, headers=bearer(U1)).json()["league_id"]
    # U2's cookies own team 2
    r = client.post("/v2/leagues/connect", json={"league": "4242", "espn_s2": S2 + "x", "swid": OTHER_SWID,
                                                 "consent": True}, headers=bearer(U2))
    assert r.status_code == 201 and r.json()["cookies"] == "existing" and r.json()["team_id"] == "2"
    assert one(pg, "SELECT role FROM fi.league_memberships WHERE league_id = %s AND user_id = %s", [lid, U2]) == ("viewer",)
    assert one(pg, "SELECT owner_user_id::text FROM fi.league_credentials WHERE league_id = %s", [lid]) == (U1,)
    assert one(pg, "SELECT count(*) FROM fi.sync_jobs WHERE league_id = %s", [lid]) == (1,)
    # U3's cookies own no team: an invite is the way in
    r = client.post("/v2/leagues/connect", json={"league": "4242", "espn_s2": S2,
                                                 "swid": "{99999999-2222-3333-4444-555555555555}", "consent": True},
                    headers=bearer(U3))
    assert r.status_code == 403


@pytest.fixture
def private_league(pg, users):
    with pg.connect() as su:
        lid = seed_league(su, visibility="private", external_id="4242")
        seed_membership(su, U1, lid, "admin")
        seed_membership(su, U2, lid, "viewer")
        su.commit()
    return lid


def test_invites_are_created_by_admins_redeemed_once_and_revocable(client, pg, private_league):
    assert client.post(f"/v2/leagues/{private_league}/invites", headers=bearer(U2)).status_code == 403
    r = client.post(f"/v2/leagues/{private_league}/invites", headers=bearer(U1))
    assert r.status_code == 201
    token, invite_id = r.json()["token"], r.json()["invite_id"]
    listed = client.get(f"/v2/leagues/{private_league}/invites", headers=bearer(U1)).json()["invites"]
    assert [i["invite_id"] for i in listed] == [invite_id] and token not in json.dumps(listed)
    assert client.get(f"/v2/leagues/{private_league}", headers=bearer(U3)).status_code == 404
    assert client.post("/v2/invites/redeem", json={"token": token}, headers=bearer(U3)).json() == {"league_id": private_league}
    assert client.get(f"/v2/leagues/{private_league}", headers=bearer(U3)).json()["role"] == "viewer"
    assert client.post("/v2/invites/redeem", json={"token": token}, headers=bearer(U3)).status_code == 404, "single use"
    second = client.post(f"/v2/leagues/{private_league}/invites", headers=bearer(U1)).json()
    assert client.delete(f"/v2/leagues/{private_league}/invites/{second['invite_id']}", headers=bearer(U1)).status_code == 204
    with pg.connect() as su:
        seed_user(su, "44444444-4444-4444-8444-444444444444")
        su.commit()
    assert client.post("/v2/invites/redeem", json={"token": second["token"]},
                       headers=bearer("44444444-4444-4444-8444-444444444444")).status_code == 404


def test_a_member_stores_and_removes_their_own_credential(client, pg, private_league, espn, dispatched):
    r = client.put(f"/v2/leagues/{private_league}/credential", json=COOKIES, headers=bearer(U2))
    assert r.status_code == 200 and S2 not in r.text
    assert one(pg, "SELECT owner_user_id::text, status FROM fi.league_credentials") == (U2, "active")
    assert client.put(f"/v2/leagues/{private_league}/credential", json=COOKIES, headers=bearer(U1)).status_code == 409
    assert client.delete(f"/v2/leagues/{private_league}/credential", headers=bearer(U1)).status_code == 403
    assert client.delete(f"/v2/leagues/{private_league}/credential", headers=bearer(U2)).status_code == 204
    assert one(pg, "SELECT count(*) FROM fi.league_credentials") == (0,)


def test_refresh_is_queued_once_and_a_stale_league_says_why(client, pg, private_league, dispatched):
    assert client.post(f"/v2/leagues/{private_league}/sync", headers=bearer(U2)).json() == {"queued": True}
    assert client.post(f"/v2/leagues/{private_league}/sync", headers=bearer(U2)).json() == {"queued": False}
    assert dispatched == [1]
    with pg.connect() as su:
        su.execute("UPDATE fi.leagues SET status = 'stale' WHERE id = %s", [private_league])
        su.commit()
    r = client.post(f"/v2/leagues/{private_league}/sync", headers=bearer(U1))
    assert r.status_code == 409 and "paused" in r.json()["detail"]


def test_an_admin_deletes_a_private_league(client, pg, private_league):
    assert client.delete(f"/v2/leagues/{private_league}", headers=bearer(U2)).status_code == 403
    assert client.delete(f"/v2/leagues/{private_league}", headers=bearer(U1)).status_code == 204
    assert client.get(f"/v2/leagues/{private_league}", headers=bearer(U1)).status_code == 404


def test_a_404_on_an_expired_public_league_queues_one_probe_and_starts_the_drain(client, pg, users, dispatched):
    with pg.connect() as su:
        expired = seed_league(su, checked_at="25 hours")
        su.commit()
    for _ in range(3):
        r = client.get(f"/v2/leagues/{expired}")
        assert (r.status_code, r.json()) == (404, {"detail": "League not found."})
    assert one(pg, "SELECT count(*) FROM fi.sync_jobs WHERE league_id = %s AND kind = 'probe'", [expired]) == (1,)
    assert dispatched == [1]
    assert client.get("/v2/leagues/lg_" + "n" * 24).status_code == 404 and dispatched == [1]
