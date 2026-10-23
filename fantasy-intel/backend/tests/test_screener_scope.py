"""The screener under /v2/leagues/{lg} (issue #226): authorization before validation and before any file is read,
inaccessible leagues indistinguishable from unknown ones, and ownership read only from the authorized league's file."""
from cp_seed import U1, U2, U3, seed_membership, seed_user
from league_fixtures import publish_local
from test_league_scope import MARK, client, e2e, e2e_b, served  # noqa: F401  (fixtures)
from test_leagues_api import bearer, env  # noqa: F401  (fixtures)

NOT_FOUND = (404, {"detail": "League not found."})
HOSTILE = ("", "?f=1:xfp_pg:gte:1;DROP%20TABLE%20x", "?to=99&limit=0", "?pos=QB&f=9:nope:eq:x", "?owner=" + "a" * 40, "?season=abc")
RB1 = "00-SYNRB01"            # synthetic research RB mapped to the fixture league's player 201 (tests/synthetic_research.py)


def test_unknown_and_inaccessible_leagues_are_the_same_404_before_any_validation(pg, served, client, e2e, tmp_path):
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="private")
    with pg.connect() as con:
        for u in (U1, U3):
            seed_user(con, u)
        seed_membership(con, U1, pub.league_id, "viewer")
        con.commit()
    for lg in (pub.league_id, "lg_" + "z" * 24):
        for path in ("/api/screener", "/api/screener/metrics"):
            for q in HOSTILE:
                for headers in ({}, bearer(U3)):                # anonymous, and a signed-in non-member
                    r = client.get(f"/v2/leagues/{lg}{path}{q}", headers=headers)
                    assert (r.status_code, r.json()) == NOT_FOUND, (lg, path, q, headers)
    assert not list((tmp_path / "tmp" / "fantasy-leagues").glob("*")), "nothing may be downloaded for a 404"
    # the member gets past authorization; only then is the screen validated
    ok = client.get(f"/v2/leagues/{pub.league_id}/api/screener", headers=bearer(U1))
    assert ok.status_code == 200 and ok.json()["contract"] == "screener/v1"
    bad = client.get(f"/v2/leagues/{pub.league_id}/api/screener?to=99", headers=bearer(U1))
    assert bad.status_code == 422
    assert ok.headers["cache-control"] == "private, no-store"


def test_ownership_comes_from_the_authorized_league_only(pg, served, client, e2e, e2e_b, tmp_path):
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="public", name="A")
    b = publish_local(pg, e2e_b, served, tmp_path / "b", visibility="private", name="B",
                      stamp="2026-10-20T13:00:00", external_id="1999")   # same player ids, team names marked
    with pg.connect() as con:
        seed_user(con, U2)
        seed_membership(con, U2, b.league_id, "viewer")
        con.commit()
    q = "/api/screener?pos=RB&limit=100"
    rb = client.get(f"/v2/leagues/{b.league_id}{q}", headers=bearer(U2))
    ra = client.get(f"/v2/leagues/{a.league_id}{q}", headers=bearer(U2))
    assert rb.status_code == ra.status_code == 200
    own_b = next(r["ownership"] for r in rb.json()["rows"] if r["gsis_id"] == RB1)
    own_a = next(r["ownership"] for r in ra.json()["rows"] if r["gsis_id"] == RB1)
    assert own_a["status"] == own_b["status"] == "rostered" and own_a["player_id"] == 201
    assert MARK in own_b["team_name"] and MARK not in ra.text
    # every other row's ownership is unknown in both leagues (no verified link), never available
    for r in ra.json()["rows"]:
        if r["gsis_id"] != RB1:
            assert r["ownership"]["status"] == "unknown" and r["ownership"]["available"] is False
    # B stays private: anonymous callers and non-members get the unknown-league 404, with or without a valid screen
    assert (client.get(f"/v2/leagues/{b.league_id}{q}").status_code, client.get(f"/v2/leagues/{b.league_id}{q}").json()) \
        == NOT_FOUND
    with pg.connect() as con:
        seed_user(con, U1)
        con.commit()
    r = client.get(f"/v2/leagues/{b.league_id}{q}", headers=bearer(U1))
    assert (r.status_code, r.json()) == NOT_FOUND
