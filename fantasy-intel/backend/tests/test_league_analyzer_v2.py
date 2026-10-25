"""The league analyzer under /v2/leagues/{lg} (issue #191): authorized by league_scope like every legacy page. A private
league's matrix is 404 to anyone who cannot see the league (indistinguishable from an unknown league), every parameter
is validated only after authorization, and team ids are this league's own teams. Synthetic E2E fixture league only."""
from cp_seed import U1, U2, seed_membership, seed_user
from league_fixtures import publish_local
from test_league_scope import client, e2e, served  # noqa: F401  (fixtures)
from test_leagues_api import bearer, env  # noqa: F401  (fixture)

NOT_FOUND = (404, {"detail": "League not found."})


def test_private_league_matrix_is_hidden_from_everyone_but_members(pg, served, client, e2e, tmp_path):  # noqa: F811
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="private")
    with pg.connect() as con:
        seed_user(con, U1)
        seed_user(con, U2)
        seed_membership(con, U1, pub.league_id, "viewer")
        con.commit()
    base = f"/v2/leagues/{pub.league_id}/api/league-analyzer"
    path = f"{base}?team_id=2&vs=1&horizon=week"
    for headers in ({}, bearer(U2)):                       # anonymous, signed-in stranger
        res = client.get(path, headers=headers)
        assert (res.status_code, res.json()) == NOT_FOUND
    unknown = client.get("/v2/leagues/lg_" + "z" * 24 + "/api/league-analyzer", headers=bearer(U1))
    assert (unknown.status_code, unknown.json()) == NOT_FOUND
    member = client.get(path, headers=bearer(U1))
    assert member.status_code == 200, member.text
    assert member.headers["cache-control"] == "private, no-store"
    body = member.json()
    assert body["contract"] == "league-analyzer/v1" and body["compare"]["b"]["team_id"] == 1
    assert {t["team_id"] for t in body["teams"]} >= {1, 2}
    # malformed or foreign parameters are authorized first: a stranger never learns the league exists from a 422/404
    for bad in ("horizon=season", "vs=abc", "team_id=99", "vs=99", "team_id=2&vs=2"):
        bad_path = f"{base}?{bad}"
        assert (client.get(bad_path).status_code, client.get(bad_path).json()) == NOT_FOUND
        assert client.get(bad_path, headers=bearer(U2)).json() == NOT_FOUND[1]
        for who in ({}, bearer(U2)):                     # anonymous and stranger: nothing beyond the plain 404
            assert "x-fi-error-code" not in client.get(bad_path, headers=who).headers
    for query, status, code in (("team_id=99", 404, "unknown_team"), ("team_id=2&vs=99", 404, "unknown_vs"),
                                ("team_id=98&vs=99", 404, "unknown_teams"), ("team_id=2&vs=2", 422, "same_team")):
        res = client.get(f"{base}?{query}", headers=bearer(U1))
        assert (res.status_code, res.headers.get("x-fi-error-code")) == (status, code), query
    assert client.get(f"{base}?horizon=season", headers=bearer(U1)).status_code == 422
    assert client.get(f"{base}?vs=abc", headers=bearer(U1)).status_code == 422
    assert client.get(f"{base}?team_id=99", headers=bearer(U1)).status_code == 404     # not a team of this league
    assert client.get(f"{base}?team_id=2&vs=2", headers=bearer(U1)).status_code == 422


def test_public_league_matrix_answers_anonymous_visitors_without_a_team(pg, served, client, e2e, tmp_path):  # noqa: F811
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="public")
    res = client.get(f"/v2/leagues/{pub.league_id}/api/league-analyzer")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["teams"] and body["compare"] is None
    assert res.headers["cache-control"] == "private, no-store"
    # a shared compare link opened without a team: the dashboard's needsTeam() answer (pick a team), not "unknown team"
    shared = client.get(f"/v2/leagues/{pub.league_id}/api/league-analyzer?vs=1")
    assert shared.status_code == 404 and "No team is marked as yours" in shared.json()["detail"]
    assert shared.headers["x-fi-error-code"] == "no_own_team"
