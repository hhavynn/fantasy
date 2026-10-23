"""Trade counterparty scouting under /v2/leagues/{lg} (issue #215): authorized by league_scope before any validation or
file read; a private league's patterns are 404 to anyone who cannot see it, exactly like an unknown league; two leagues
with colliding team ids each name only their own managers."""
from cp_seed import U1, U2, seed_membership, seed_user
from league_fixtures import publish_local
from test_league_scope import MARK, client, e2e, e2e_b, served  # noqa: F401  (fixtures)
from test_leagues_api import bearer, env  # noqa: F401  (fixture)

NOT_FOUND = (404, {"detail": "League not found."})


def test_counterparty_patterns_are_scoped_to_the_permitted_league(pg, served, client, e2e, e2e_b, tmp_path):  # noqa: F811
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="private", name="A")
    b = publish_local(pg, e2e_b, served, tmp_path / "b", visibility="private", name="B",
                      stamp="2026-10-20T13:00:00", external_id="1999")      # same team and player ids as A
    with pg.connect() as con:
        seed_user(con, U1)
        seed_user(con, U2)
        seed_membership(con, U1, a.league_id, "viewer")
        seed_membership(con, U2, b.league_id, "viewer")
        con.commit()
    path = "/api/managers/4/counterparty"
    for who in (None, bearer(U2)):                       # anonymous, and a signed-in member of another league
        r = client.get(f"/v2/leagues/{a.league_id}{path}", headers=who)
        assert (r.status_code, r.json()) == NOT_FOUND
    unknown = client.get("/v2/leagues/lg_" + "z" * 24 + path, headers=bearer(U1))
    assert (unknown.status_code, unknown.json()) == NOT_FOUND

    ra = client.get(f"/v2/leagues/{a.league_id}{path}", headers=bearer(U1))
    rb = client.get(f"/v2/leagues/{b.league_id}{path}", headers=bearer(U2))
    assert ra.status_code == rb.status_code == 200, (ra.text[:200], rb.text[:200])
    assert ra.headers["cache-control"] == "private, no-store"
    assert MARK not in ra.text
    a_partners = [p["name"] for t in ra.json()["trades"] for p in t["partners"]]
    b_partners = [p["name"] for t in rb.json()["trades"] for p in t["partners"]]
    assert a_partners and all(MARK not in n for n in a_partners)
    assert b_partners and all(n.startswith(MARK) for n in b_partners)
    assert ra.json()["team"]["name"] != rb.json()["team"]["name"] and rb.json()["team"]["name"].startswith(MARK)
    # the same counts: B is a renamed copy of A
    assert ra.json()["summary"] == rb.json()["summary"]

    # authorization before validation: a malformed or unknown team never confirms a private league exists
    for bad in ("/api/managers/abc/counterparty", "/api/managers/99/counterparty"):
        for who in (None, bearer(U2)):
            r = client.get(f"/v2/leagues/{a.league_id}{bad}", headers=who)
            assert (r.status_code, r.json()) == NOT_FOUND
    assert client.get(f"/v2/leagues/{a.league_id}/api/managers/abc/counterparty", headers=bearer(U1)).status_code == 422
    r = client.get(f"/v2/leagues/{a.league_id}/api/managers/99/counterparty", headers=bearer(U1))
    assert (r.status_code, r.json()) == (404, {"detail": "Unknown team"})
