"""/v2 end to end: the feature flag, Supabase-token identity, server-side authorization and the preference routes.
Runs against the throwaway Postgres (fixture `pg`) with the API's ControlPlane pointed at it."""
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from cp_seed import U1, U2, seed_league, seed_membership, seed_snapshot, seed_user

URL = "https://example-project.supabase.co"
KEY = ec.generate_private_key(ec.SECP256R1())
LID = "lg_" + "a" * 24


def _jwks_client():
    client = jwt.PyJWKClient(URL + "/auth/v1/.well-known/jwks.json", cache_keys=True)
    client.fetch_data = lambda: {"keys": [{**jwt.algorithms.ECAlgorithm.to_jwk(KEY.public_key(), as_dict=True),
                                           "kid": "k", "alg": "ES256", "use": "sig"}]}
    return client


def token(uid: str, **over) -> str:
    now = int(time.time())
    claims = {"iss": URL + "/auth/v1", "sub": uid, "aud": "authenticated", "role": "authenticated",
              "iat": now, "exp": now + 600, **over}
    return jwt.encode(claims, KEY, algorithm="ES256", headers={"kid": "k"})


def bearer(uid: str, **over) -> dict:
    return {"Authorization": f"Bearer {token(uid, **over)}"}


@pytest.fixture
def env(pg, monkeypatch):
    import app.control_plane.jwt_auth as jwt_auth
    from app.control_plane import pg as cp_pg
    monkeypatch.setenv("MULTILEAGUE_ENABLED", "1")
    monkeypatch.setenv("SUPABASE_URL", URL)
    monkeypatch.setenv("SUPABASE_DB_URL", pg.dsn_for("fi_api"))
    monkeypatch.delenv("FANTASY_API_KEY", raising=False)
    client = _jwks_client()
    monkeypatch.setattr(jwt_auth, "jwks_client", lambda url: client)
    monkeypatch.setattr(cp_pg, "_PLANES", {})
    yield
    for plane in cp_pg._PLANES.values():
        plane.close()


@pytest.fixture
def client(env):
    from app.main import app
    return TestClient(app)


@pytest.fixture
def world(pg):
    with pg.connect() as con:
        seed_user(con, U1)
        seed_user(con, U2)
        pub = seed_league(con, visibility="public", name="Public League")
        priv = seed_league(con, visibility="private", checked_at=None, name="Private League")
        seed_membership(con, U1, priv, "admin")
        seed_snapshot(con, pub)
        con.commit()
    return {"pub": pub, "priv": priv}


ALL_ROUTES = [("GET", "/v2/providers"), ("GET", "/v2/me"), ("GET", "/v2/leagues"), ("GET", f"/v2/leagues/{LID}"),
              ("PUT", f"/v2/leagues/{LID}/pin"), ("DELETE", f"/v2/leagues/{LID}/pin"),
              ("PUT", f"/v2/leagues/{LID}/perspective"), ("DELETE", f"/v2/leagues/{LID}/perspective")]


@pytest.mark.parametrize("flag", ["", "0", "false", None])
@pytest.mark.parametrize("method,path", ALL_ROUTES)
def test_flag_off_means_404_on_every_v2_route(client, monkeypatch, flag, method, path):
    if flag is None:
        monkeypatch.delenv("MULTILEAGUE_ENABLED")
    else:
        monkeypatch.setenv("MULTILEAGUE_ENABLED", flag)
    r = client.request(method, path, headers=bearer(U1), json={"team_external_id": "1"} if method == "PUT" else None)
    assert (r.status_code, r.json()) == (404, {"detail": "Not Found"})


def test_flag_off_never_opens_postgres(client, monkeypatch):
    from app.control_plane import pg as cp_pg
    monkeypatch.setenv("MULTILEAGUE_ENABLED", "0")
    monkeypatch.setenv("SUPABASE_DB_URL", "host=/nonexistent user=fi_api dbname=nope")
    assert client.get("/v2/leagues", headers=bearer(U1)).status_code == 404
    assert client.get(f"/v2/leagues/{LID}").status_code == 404
    assert client.get("/health").status_code == 200
    assert cp_pg._PLANES == {}


def test_providers_is_public_but_a_bad_token_is_still_401(client):
    assert client.get("/v2/providers").status_code == 200
    assert client.get("/v2/providers", headers={"Authorization": "Bearer nope.nope.nope"}).status_code == 401
    assert client.get("/v2/providers", headers=bearer(U1, aud="anon")).status_code == 401


def test_anonymous_rules(client, world):
    assert client.get("/v2/me").status_code == 401
    assert client.get("/v2/leagues").status_code == 401
    assert client.get(f"/v2/leagues/{world['pub']}").status_code == 200
    assert client.get(f"/v2/leagues/{world['priv']}").status_code == 404
    assert client.put(f"/v2/leagues/{world['pub']}/pin").status_code == 401          # visible, but a preference needs sign-in
    assert client.put(f"/v2/leagues/{world['priv']}/pin").status_code == 404         # invisible: never 401
    assert client.put(f"/v2/leagues/{world['pub']}/perspective", json={"team_external_id": "6"}).status_code == 401


def test_me_creates_the_user_row_and_lists_pins(client, world):
    uid = str(uuid.uuid4())
    first = client.get("/v2/me", headers=bearer(uid)).json()
    assert first == {"user_id": uid, "is_platform_admin": False, "pins": [], "created_at": first["created_at"]}
    assert client.put(f"/v2/leagues/{world['pub']}/pin", headers=bearer(uid)).status_code == 204
    assert client.get("/v2/me", headers=bearer(uid)).json()["pins"] == [world["pub"]]
    assert [lg["league_id"] for lg in client.get("/v2/leagues", headers=bearer(uid)).json()["leagues"]] == [world["pub"]]
    assert client.delete(f"/v2/leagues/{world['pub']}/pin", headers=bearer(uid)).status_code == 204
    assert client.get("/v2/leagues", headers=bearer(uid)).json() == {"leagues": []}


def test_league_view_shape_and_membership(client, world):
    body = client.get(f"/v2/leagues/{world['pub']}", headers=bearer(U2)).json()
    assert body == {"league_id": world["pub"], "provider": "espn", "external_league_id": body["external_league_id"],
                    "season": 2026, "name": "Public League", "visibility": "public", "status": "active", "role": None,
                    "pinned": False, "perspective_team_id": None, "sync_state": "ready",
                    "data": {"data_version": "v1", "global_version": "g1", "committed_at": body["data"]["committed_at"]}}
    assert "object_key" not in body and "leagues/" not in str(body)
    priv = client.get(f"/v2/leagues/{world['priv']}", headers=bearer(U1)).json()
    assert priv["role"] == "admin" and priv["sync_state"] == "no_data" and priv["data"] is None
    assert client.get(f"/v2/leagues/{world['priv']}", headers=bearer(U2)).status_code == 404
    leagues = client.get("/v2/leagues", headers=bearer(U1)).json()["leagues"]
    assert [lg["league_id"] for lg in leagues] == [world["priv"]]


def test_a_pinned_league_that_turns_private_drops_out_of_the_list(client, world, pg):
    assert client.put(f"/v2/leagues/{world['pub']}/pin", headers=bearer(U2)).status_code == 204
    with pg.connect() as su:
        su.execute("UPDATE fi.leagues SET visibility = 'private', visibility_epoch = visibility_epoch + 1 WHERE id = %s",
                   [world["pub"]])
        su.commit()
    assert client.get("/v2/leagues", headers=bearer(U2)).json() == {"leagues": []}
    assert client.get(f"/v2/leagues/{world['pub']}", headers=bearer(U2)).status_code == 404


def test_perspective_is_a_preference_only(client, world):
    r = client.put(f"/v2/leagues/{world['pub']}/perspective", json={"team_external_id": "99"}, headers=bearer(U2))
    assert r.status_code == 200 and r.json()["perspective_team_id"] == "99"      # never verified against a roster
    assert client.get(f"/v2/leagues/{world['pub']}", headers=bearer(U1)).json()["perspective_team_id"] is None
    for bad in ({"team_external_id": "6; DROP"}, {"team_external_id": 6}, {"team": "6"}, {}, {"team_external_id": "6", "x": 1}):
        assert client.put(f"/v2/leagues/{world['pub']}/perspective", json=bad, headers=bearer(U2)).status_code == 422, bad
    assert client.delete(f"/v2/leagues/{world['pub']}/perspective", headers=bearer(U2)).status_code == 204
    assert client.get(f"/v2/leagues/{world['pub']}", headers=bearer(U2)).json()["perspective_team_id"] is None
    # a 404 league is a 404 even with a bad body: authorization runs before validation
    assert client.put(f"/v2/leagues/{world['priv']}/perspective", json={"x": 1}, headers=bearer(U2)).status_code == 404


@pytest.mark.parametrize("league_id", ["lg_" + "z" * 24, "lg_short", "not-an-id", "lg_" + "a" * 65])
def test_unknown_and_malformed_ids_are_the_same_404(client, world, league_id):
    r = client.get(f"/v2/leagues/{league_id}", headers=bearer(U1))
    assert (r.status_code, r.json()) == (404, {"detail": "League not found."})


def test_control_plane_outage_is_503_not_404(client, world, monkeypatch):
    from app.control_plane import pg as cp_pg
    monkeypatch.setenv("SUPABASE_DB_URL", "host=/nonexistent user=fi_api dbname=nope connect_timeout=1")
    monkeypatch.setattr(cp_pg, "_PLANES", {})
    r = client.get(f"/v2/leagues/{world['pub']}")
    assert r.status_code == 503 and r.json() == {"detail": "The control plane is unavailable."}


def test_responses_are_private_and_uncached(client, world):
    assert client.get(f"/v2/leagues/{world['pub']}").headers["cache-control"] == "private, no-store"
    assert client.get("/v2/me", headers=bearer(U1)).headers["cache-control"] == "private, no-store"


def test_legacy_routes_and_service_auth_are_unchanged_with_the_flag_on(client, monkeypatch, tmp_path):
    monkeypatch.setenv("FANTASY_API_KEY", "k" * 40)
    monkeypatch.setenv("FANTASY_DB_PATH", str(tmp_path / "missing.duckdb"))
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/overview", headers={"X-API-Key": "k" * 40}).status_code == 503   # no db: same as before
    assert client.get("/v2/providers", headers={"X-API-Key": "k" * 40}).status_code == 200


def test_a_commit_that_fails_after_the_handler_is_a_503_not_a_success(client, world, monkeypatch):
    """The request transaction commits when the dependency's context exits. That must happen before the response is
    sent (dependency scope "function"), so a commit failure becomes a 503 instead of a 200/204 for a rolled-back
    write."""
    from contextlib import contextmanager

    from app.control_plane import pg as cp_pg
    real = cp_pg.ControlPlane.request_tx

    @contextmanager
    def failing_commit(self, principal):
        with real(self, principal) as con:
            yield con
        raise cp_pg.ControlPlaneUnavailable("commit failed")

    monkeypatch.setattr(cp_pg.ControlPlane, "request_tx", failing_commit)
    r = client.put(f"/v2/leagues/{world['pub']}/pin", headers=bearer(U1))
    assert (r.status_code, r.json()) == (503, {"detail": "The control plane is unavailable."})
    r = client.get(f"/v2/leagues/{world['pub']}")
    assert r.status_code == 503
