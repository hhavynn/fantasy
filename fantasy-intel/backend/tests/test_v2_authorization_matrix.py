"""Every /v2 route with a {league_id} is enumerated from the live router and driven through the spec's matrix:
{anonymous, viewer, admin, non-member} x {public, private, stale, visibility-expired, flipped-private, deleted}.
A new league route that forgets `league_tx` fails here: for an unknown league id it would answer something other
than 404 (a 401, 422 or 200)."""
import pytest

from cp_seed import U1, U2, U3, seed_league, seed_membership, seed_user
from test_leagues_api import bearer, client, env  # noqa: F401  (fixtures)

STATES = {
    "public":          dict(visibility="public"),
    "private":         dict(visibility="private", checked_at=None),
    "stale":           dict(visibility="private", status="stale", checked_at=None),
    "expired":         dict(visibility="public", checked_at="25 hours"),
    "flipped_private": dict(visibility="private", epoch=1, checked_at="1 hour"),
    "deleted":         dict(visibility="public", deleted=True),
}
VISIBLE = {   # state -> actors who get past authorization
    "public": {"anon", "viewer", "admin", "stranger"}, "private": {"viewer", "admin"}, "stale": {"viewer", "admin"},
    "expired": {"viewer", "admin"}, "flipped_private": set(), "deleted": set(),
}
ACTORS = {"anon": None, "viewer": U2, "admin": U1, "stranger": U3}


def headers(actor):
    uid = ACTORS[actor]
    return bearer(uid) if uid else {}


def _league_route_infos(include_mounted: bool = True):
    from app.api.routing import iter_routes
    from app.main import app
    return [r for r in iter_routes(app) if r.path.startswith("/v2/leagues/{league_id}")
            and (include_mounted or not r.path.startswith(LEGACY_MOUNT))]


LEGACY_MOUNT = "/v2/leagues/{league_id}/api/"


def league_routes():
    """The /v2 router's own league routes, cross-checked against the OpenAPI schema. The legacy routers mounted under
    /v2/leagues/{league_id}/api/ are covered by tests/test_league_scope.py (they need league files to answer)."""
    from app.main import app
    out = sorted((m, r.path) for r in _league_route_infos(include_mounted=False) for m in r.methods)
    schema = sorted((m.upper(), path) for path, ops in app.openapi()["paths"].items()
                    if path.startswith("/v2/") and "{league_id}" in path for m in ops)
    assert out == schema, (out, schema)
    assert len(out) >= 5, out          # Phase 1: league view, pin x2, perspective x2
    return out


# Phase 1's preference routes: a visible signed-in caller succeeds. The Phase 3 connect routes (invites, credential,
# refresh, delete) have their own outcomes for a visible caller (403 for a viewer, 409, 422 for an empty body…):
# here they must only never be the 404 an invisible league gets, never a 5xx, and always 401 when anonymous.
PREFERENCE_ROUTES = {("GET", "/v2/leagues/{league_id}"), ("PUT", "/v2/leagues/{league_id}/pin"),
                     ("DELETE", "/v2/leagues/{league_id}/pin"), ("PUT", "/v2/leagues/{league_id}/perspective"),
                     ("DELETE", "/v2/leagues/{league_id}/perspective")}


@pytest.fixture(autouse=True)
def _connect_routes_on(monkeypatch):
    monkeypatch.setenv("MULTILEAGUE_CONNECT_ENABLED", "1")


def body_for(method, path):
    return {"team_external_id": "6"} if method == "PUT" and path.endswith("/perspective") else None


@pytest.fixture
def leagues(pg):
    with pg.connect() as con:
        for u in (U1, U2, U3):
            seed_user(con, u)
        ids = {name: seed_league(con, **kw) for name, kw in STATES.items()}
        for name in STATES:
            if name != "flipped_private":       # a public import that flipped private has no members
                seed_membership(con, U1, ids[name], "admin")
                seed_membership(con, U2, ids[name], "viewer")
        con.commit()
    return ids


@pytest.mark.parametrize("method,path", league_routes())
@pytest.mark.parametrize("state", list(STATES))
@pytest.mark.parametrize("actor", list(ACTORS))
def test_matrix(client, leagues, method, path, state, actor):
    url = path.replace("{league_id}", leagues[state]).replace("{invite_id}", "00000000-0000-4000-8000-000000000000")
    r = client.request(method, url, headers=headers(actor), json=body_for(method, path))
    where = (method, path, state, actor)
    if actor not in VISIBLE[state]:
        assert (r.status_code, r.json()) == (404, {"detail": "League not found."}), where
    elif (method, path) not in PREFERENCE_ROUTES:
        if actor == "anon":
            assert (r.status_code, r.json()) == (401, {"detail": "Sign in to use this endpoint."}), where
        else:
            assert r.status_code != 404 and r.status_code < 500, (where, r.status_code, r.text[:200])
    elif method == "GET":
        assert r.status_code == 200, where
        assert r.json()["role"] == {"viewer": "viewer", "admin": "admin"}.get(actor), where
    elif actor == "anon":
        assert (r.status_code, r.json()) == (401, {"detail": "Sign in to use this endpoint."}), where
    else:
        assert r.status_code in (200, 204), where


@pytest.mark.parametrize("method,path", league_routes())
@pytest.mark.parametrize("actor", list(ACTORS))
def test_unknown_league_is_404_on_every_route_before_anything_else(client, leagues, method, path, actor):
    url = path.replace("{league_id}", "lg_" + "q" * 24).replace("{invite_id}", "00000000-0000-4000-8000-000000000000")
    r = client.request(method, url, headers=headers(actor), json={"garbage": True})
    assert (r.status_code, r.json()) == (404, {"detail": "League not found."}), (method, path, actor)


def test_every_league_route_declares_an_authorizing_dependency():
    """Every route under /v2/leagues/{league_id} runs league_tx (the /v2 router) or league_scope (the legacy mount),
    and both authorize before anything else."""
    from app.api import leagues_v2
    from app.api.league_scope import league_scope
    routes = _league_route_infos()
    assert len(routes) > 40
    for r in routes:
        calls = r.dependency_calls()
        assert leagues_v2.league_tx in calls or league_scope in calls, r.path
        assert leagues_v2.require_enabled in calls or r.path.startswith("/v2/leagues/{league_id}") and \
            not r.path.startswith(LEGACY_MOUNT), r.path


def test_every_v2_transaction_dependency_finishes_before_the_response():
    """request_tx commits on exit; with FastAPI's default request scope that would run after the response is sent."""
    from app.api import leagues_v2
    from app.api.routing import iter_routes
    from app.main import app
    checked = 0
    for info in iter_routes(app):
        if not info.path.startswith("/v2/"):
            continue
        route = info.route
        stack = list(route.dependant.dependencies)
        while stack:
            dep = stack.pop()
            if dep.call in (leagues_v2.request_tx, leagues_v2.league_tx):
                assert dep.scope == "function", (route.path, dep.call.__name__, dep.scope)
                checked += 1
            stack.extend(dep.dependencies)
    assert checked >= 7


# The all-leagues action center (issue #166) is user-scoped: no {league_id}, so it is not in league_routes(). Its league
# list must be exactly what the same matrix grants each actor among the leagues they belong to: never a league in a
# state they cannot see (flipped private, deleted), nothing at all for a stranger, and 401 for the anonymous caller.
ACTION_CENTER_EXPECTED = {"viewer": {"public", "private", "stale", "expired"}, "admin": {"public", "private", "stale", "expired"},
                          "stranger": set()}


@pytest.mark.parametrize("actor", list(ACTORS))
def test_action_center_lists_exactly_the_leagues_the_matrix_grants(client, leagues, actor, monkeypatch):
    from app.api import action_center
    monkeypatch.setattr(action_center, "_CACHES", {})
    r = client.get("/v2/action-center", headers=headers(actor))
    if actor == "anon":
        assert (r.status_code, r.json()) == (401, {"detail": "Sign in to use this endpoint."})
        return
    assert r.status_code == 200, r.text[:200]
    names = {v: k for k, v in leagues.items()}
    listed = [s["league"]["league_id"] for s in r.json()["leagues"]] + [x["league_id"] for x in r.json()["more"]["leagues"]]
    assert {names[i] for i in listed} == ACTION_CENTER_EXPECTED[actor]
    for hidden in set(STATES) - ACTION_CENTER_EXPECTED[actor]:
        assert leagues[hidden] not in r.text, (actor, hidden)
    # no league here has a published snapshot: each says so instead of showing a roster
    assert all(s["state"] == "not_ready" and s["overview"] is None for s in r.json()["leagues"])
