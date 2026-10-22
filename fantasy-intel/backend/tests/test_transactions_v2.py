"""The transactions feed under /v2/leagues/{lg}: authorized by league_scope like every legacy page (private league
activity is 404 to anyone who cannot see the league, indistinguishable from an unknown league)."""
from cp_seed import U1, U2, seed_membership, seed_user
from league_fixtures import publish_local
from test_league_scope import client, e2e, served  # noqa: F401  (fixtures)
from test_leagues_api import bearer, env  # noqa: F401  (fixture)

NOT_FOUND = (404, {"detail": "League not found."})


def test_private_league_activity_is_hidden_from_everyone_but_members(pg, served, client, e2e, tmp_path):  # noqa: F811
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="private")
    with pg.connect() as con:
        seed_user(con, U1)
        seed_user(con, U2)
        seed_membership(con, U1, pub.league_id, "viewer")
        con.commit()
    path = f"/v2/leagues/{pub.league_id}/api/transactions?type=trade&manager=4"
    anon = client.get(path)
    assert (anon.status_code, anon.json()) == NOT_FOUND
    stranger = client.get(path, headers=bearer(U2))
    assert (stranger.status_code, stranger.json()) == NOT_FOUND
    unknown = client.get("/v2/leagues/lg_" + "z" * 24 + "/api/transactions", headers=bearer(U1))
    assert (unknown.status_code, unknown.json()) == NOT_FOUND
    member = client.get(path, headers=bearer(U1))
    assert member.status_code == 200, member.text
    assert member.headers["cache-control"] == "private, no-store"
    assert [e["kind"] for e in member.json()["events"]] == ["trade"]
    # a filter value the league does not have is the route's own 404, only after authorization
    assert client.get(f"/v2/leagues/{pub.league_id}/api/transactions?manager=99", headers=bearer(U1)).status_code == 404
    assert client.get(f"/v2/leagues/{pub.league_id}/api/transactions?manager=99").json() == NOT_FOUND[1]
