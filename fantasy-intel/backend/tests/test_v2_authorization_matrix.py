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


def _walk(routes):
    """Every APIRoute, descending into included routers (FastAPI >= 0.140 wraps them in an _IncludedRouter)."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _walk(inner.routes)
        else:
            yield route


def _league_route_objects():
    from app.main import app
    return [r for r in _walk(app.routes) if getattr(r, "path", "").startswith("/v2/") and "{league_id}" in r.path]


def league_routes():
    from app.main import app
    out = sorted((method, r.path) for r in _league_route_objects() for method in r.methods - {"HEAD", "OPTIONS"})
    # cross-check against the public schema, so a route registered some other way cannot hide from the matrix
    schema = sorted((m.upper(), path) for path, ops in app.openapi()["paths"].items()
                    if path.startswith("/v2/") and "{league_id}" in path for m in ops)
    assert out == schema, (out, schema)
    assert len(out) >= 5, out          # Phase 1: league view, pin x2, perspective x2
    return out


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
    url = path.replace("{league_id}", leagues[state])
    r = client.request(method, url, headers=headers(actor), json=body_for(method, path))
    where = (method, path, state, actor)
    if actor not in VISIBLE[state]:
        assert (r.status_code, r.json()) == (404, {"detail": "League not found."}), where
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
    url = path.replace("{league_id}", "lg_" + "q" * 24)
    r = client.request(method, url, headers=headers(actor), json={"garbage": True})
    assert (r.status_code, r.json()) == (404, {"detail": "League not found."}), (method, path, actor)


def test_every_league_route_declares_the_authorizing_dependency():
    """Belt and braces for the behavioural checks above: every league route depends on league_tx directly."""
    routes = _league_route_objects()
    assert routes
    for route in routes:
        names = {d.call.__name__ for d in route.dependant.dependencies}
        assert "league_tx" in names, route.path


def test_every_v2_transaction_dependency_finishes_before_the_response():
    """request_tx commits on exit; with FastAPI's default request scope that would run after the response is sent."""
    from app.api import leagues_v2
    from app.main import app
    checked = 0
    for route in _walk(app.routes):
        if not getattr(route, "path", "").startswith("/v2/"):
            continue
        stack = list(route.dependant.dependencies)
        while stack:
            dep = stack.pop()
            if dep.call in (leagues_v2.request_tx, leagues_v2.league_tx):
                assert dep.scope == "function", (route.path, dep.call.__name__, dep.scope)
                checked += 1
            stack.extend(dep.dependencies)
    assert checked >= 7
