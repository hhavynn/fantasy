"""Multi-league API (/v2). Feature-flagged: every route answers 404 unless MULTILEAGUE_ENABLED is on.

Identity is the Supabase access token verified in app/control_plane/jwt_auth.py; the service API key is never a user.
Every league route authorizes in Postgres first (app/control_plane/authz.py, via the `league_tx` dependency):
unknown, someone else's, deleted, expired-public and malformed league ids are the same 404, never 403. Only then does
a mutation check sign-in (401). Pins and perspective are preferences: never verified against league data, never
granting anything. Phase 2 adds the league-file read routes; Phase 3 adds connect, invites and credentials on top of
app/control_plane/mutations.py.

The control-plane modules (Postgres driver, JWT, cryptography) are imported inside the dependencies, not at module
load: app.main imports this router unconditionally, and legacy cold starts must not pay for code only /v2 uses.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr

from ..config import get_settings
from ..leagues.capabilities import ProviderCapabilities
from ..leagues.context import Principal, Provider

log = logging.getLogger(__name__)
NOT_FOUND = "League not found."
SIGN_IN = "Sign in to use this endpoint."
SIGNUPS_CLOSED = "Sign-ups are not open yet."
LEAGUE_LIMIT = "You already follow 25 leagues. Unpin one first."
UNAVAILABLE = "The control plane is unavailable."
NO_STORE = {"Cache-Control": "private, no-store"}


def require_enabled() -> None:
    """Router-level gate, evaluated before every other dependency, so a flag-off deployment answers 404 even for a
    garbage Authorization header and never opens Postgres."""
    if not get_settings().multileague_enabled:
        raise HTTPException(404, "Not Found")


router = APIRouter(prefix="/v2", dependencies=[Depends(require_enabled)])


def current_principal(request: Request) -> Principal:
    """The verified caller (app/control_plane/jwt_auth.py), imported on first use."""
    from ..control_plane.jwt_auth import current_principal as verify
    return verify(request)


# ---- dependencies (override in tests via app.dependency_overrides) --------------------------------

class ProviderRegistry(Protocol):
    def get_provider(self, provider: Provider) -> Any: ...
    def all_capabilities(self) -> list[ProviderCapabilities]: ...


def provider_registry() -> ProviderRegistry:
    try:
        from ..leagues.providers import registry
    except ImportError:
        raise HTTPException(503, "The provider registry is not available in this deployment.") from None
    return registry   # type: ignore[return-value]  # the module exposes the two functions


def request_tx(principal: Principal = Depends(current_principal)) -> Iterator[tuple[Any, Principal]]:
    """One Postgres transaction per request, as the request's role (anon | authenticated) with its verified claims.
    Always declared through REQUEST_TX (scope "function"): the transaction commits when this generator resumes, and
    that must happen before the response is sent, so a failed commit is a 503 rather than a 200 for a rolled-back
    write."""
    from ..control_plane.pg import AccountGone, ControlPlaneUnavailable, get_control_plane
    try:
        with get_control_plane().request_tx(principal) as con:
            yield con, principal
    except AccountGone:          # a signature-valid token can outlive its Supabase account: signed out everywhere
        raise HTTPException(401, SIGN_IN) from None
    except ControlPlaneUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None


REQUEST_TX = Depends(request_tx, scope="function")


def valid_league_id(league_id: str) -> str:
    """The path parameter's shape, checked before any Postgres work: a malformed id never takes a pool slot and is
    the same 404 even while the control plane is down."""
    from ..control_plane.authz import LeagueNotFound, check_league_id
    try:
        return check_league_id(league_id)
    except LeagueNotFound:
        raise HTTPException(404, NOT_FOUND) from None


def league_tx(league_id: str = Depends(valid_league_id), tx=REQUEST_TX) -> tuple:
    """Authorize FIRST. Everything a league route does happens inside this transaction, after this check.
    `league_id` is declared before `tx`, so FastAPI validates it before request_tx opens a connection."""
    from ..control_plane.authz import LeagueNotFound, authorize_league_in
    con, principal = tx
    try:
        return con, authorize_league_in(con, principal, league_id)
    except LeagueNotFound:
        from .connect import nudge_probe
        nudge_probe(principal, league_id)      # an expired public league starts a probe; the answer stays 404
        raise HTTPException(404, NOT_FOUND) from None


def _signed_in(access) -> Principal:
    if not access.principal.is_authenticated:
        raise HTTPException(401, SIGN_IN)
    return access.principal


def _json(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=NO_STORE)


def _no_content() -> Response:
    return Response(status_code=204, headers=NO_STORE)


# ---- routes ---------------------------------------------------------------------------------------

@router.get("/providers")
def list_providers(registry: ProviderRegistry = Depends(provider_registry),
                   _principal: Principal = Depends(current_principal)):
    """Public information about what each provider can do in this deployment. Anonymous is fine, but a
    present-and-invalid token is still a 401 (same rule as every other /v2 route)."""
    return {"providers": [c.as_dict() for c in registry.all_capabilities()]}


@router.get("/me")
def me(tx=REQUEST_TX):
    from ..control_plane import mutations
    con, principal = tx
    try:
        return _json(mutations.me(con, principal))
    except mutations.SignInRequired:
        raise HTTPException(401, SIGN_IN) from None
    except mutations.SignupsClosed:
        raise HTTPException(403, SIGNUPS_CLOSED) from None


@router.delete("/me", status_code=204)
def delete_me(tx=REQUEST_TX):
    """Deletes the caller's account (Phase 6): their ESPN cookies are crypto-erased in this transaction, their
    memberships, pins and team choices go, and a private league left without members is soft-deleted (its files
    are deleted 7 days later by the drain's maintenance)."""
    from ..control_plane import mutations
    con, principal = tx
    try:
        mutations.delete_my_account(con, principal)
    except mutations.SignInRequired:
        raise HTTPException(401, SIGN_IN) from None
    return _no_content()


@router.get("/admin/usage")
def admin_usage(tx=REQUEST_TX):
    """Free-tier usage (Phase 6): the latest sample per metric and its 7-day max, written hourly by the platform
    monitor. Platform admins only; everyone else, anonymous included, gets the same 404 as an unknown route."""
    con, principal = tx
    if not principal.is_authenticated:
        raise HTTPException(404, "Not Found")
    row = con.execute("SELECT is_platform_admin FROM fi.app_users WHERE id = %s", [principal.user_id]).fetchone()
    if not (row and row[0]):
        raise HTTPException(404, "Not Found")
    rows = con.execute("SELECT metric, used, quota, unit, sampled_at, max_used_7d FROM fi.usage_overview()").fetchall()
    return _json({"metrics": [
        {"metric": m, "used": used, "quota": quota, "unit": unit, "sampled_at": at.isoformat(), "max_used_7d": peak,
         "percent": round(used / quota * 100, 1) if quota else None}
        for m, used, quota, unit, at, peak in rows]})


@router.get("/leagues")
def list_leagues(tx=REQUEST_TX):
    from ..control_plane import mutations
    from ..control_plane.authz import authorize_leagues_in
    con, principal = tx
    try:
        ids = mutations.list_my_leagues(con, principal)
    except mutations.SignInRequired:
        raise HTTPException(401, SIGN_IN) from None
    # One query for the whole list; a league that was pinned and then went private or expired simply drops out.
    return _json({"leagues": [a.to_json() for a in authorize_leagues_in(con, principal, ids)]})


def _my_granted_leagues(principal: Principal) -> list:
    """The caller's own leagues (memberships and pins) that authorization grants, in one short transaction."""
    from ..control_plane import mutations
    from ..control_plane.authz import authorize_leagues_in
    from ..control_plane.pg import AccountGone, ControlPlaneUnavailable, get_control_plane
    try:
        with get_control_plane().request_tx(principal) as con:
            return authorize_leagues_in(con, principal, mutations.list_my_leagues(con, principal))
    except (mutations.SignInRequired, AccountGone):
        raise HTTPException(401, SIGN_IN) from None
    except ControlPlaneUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None


@router.get("/action-center")
async def action_center(principal: Principal = Depends(current_principal)):
    """The signed-in caller's decisions across their own leagues (issue #166; app/api/action_center.py). User-scoped,
    like GET /v2/leagues: the league list is enumerated and authorized here, in one short transaction (in the thread
    pool) that is closed before any league file is read, and never taken from the request. A league the caller cannot
    see is simply absent. Anonymous is 401 (this is the caller's own list); an unusable token was already 401 in
    `current_principal`. Async on purpose: while the leagues are evaluated on the action center's own bounded pool,
    this request holds no thread-pool token that other routes need."""
    from fastapi.concurrency import run_in_threadpool

    from . import action_center as center
    if not principal.is_authenticated:
        raise HTTPException(401, SIGN_IN)
    granted = await run_in_threadpool(_my_granted_leagues, principal)
    try:
        body, degraded = await center.serve(principal, granted)
    except center.Busy:
        raise HTTPException(503, center.BUSY, headers={**NO_STORE, "Retry-After": str(center.RETRY_AFTER_S)}) from None
    headers = {**NO_STORE, **({"X-FI-Degraded": "stale-files"} if degraded else {})}
    return JSONResponse(body, headers=headers)


@router.get("/leagues/{league_id}")
def get_league(ltx=Depends(league_tx, scope="function")):
    _, access = ltx
    return _json(access.to_json())


def _write_and_recheck(con, access, write):
    """Run a preference write for an authorized caller, then authorize again in the same transaction.

    Statements run at READ COMMITTED, so a sync can make the league private (or revoke the membership) after
    `league_tx` authorized. The write runs in a savepoint: if Postgres refuses it, the savepoint rolls back and the
    league is authorized again; when the caller no longer sees it, that refusal was the race and the answer is the
    route's 404, otherwise the error is a real bug and propagates (500). A delete has no RLS check that could refuse
    it, so the re-authorization after every write is what notices there. No error text is parsed."""
    from psycopg.errors import InsufficientPrivilege

    from ..control_plane import mutations
    from ..control_plane.authz import LeagueNotFound, authorize_league_in
    principal = _signed_in(access)
    try:
        with con.transaction():                      # a savepoint inside the request transaction
            write(principal)
    except mutations.SignInRequired:                  # a signature-valid token whose Supabase account is gone
        raise HTTPException(401, SIGN_IN) from None
    except mutations.SignupsClosed:
        raise HTTPException(403, SIGNUPS_CLOSED) from None
    except mutations.LeagueLimit:
        raise HTTPException(409, LEAGUE_LIMIT) from None
    except InsufficientPrivilege:
        try:
            authorize_league_in(con, principal, access.league_id)
        except LeagueNotFound:
            raise HTTPException(404, NOT_FOUND) from None
        raise
    try:
        return authorize_league_in(con, principal, access.league_id)
    except LeagueNotFound:
        raise HTTPException(404, NOT_FOUND) from None


class PerspectiveBody(BaseModel):
    """`{"team_external_id": "<team id as a string>"}`, nothing else. A preference: never checked against a roster."""
    model_config = ConfigDict(extra="forbid")
    team_external_id: StrictStr


@router.put("/leagues/{league_id}/pin", status_code=204)
def pin_league(ltx=Depends(league_tx, scope="function")):
    from ..control_plane import mutations
    con, access = ltx
    _write_and_recheck(con, access, lambda principal: mutations.set_pin(con, principal, access.league_id, True))
    return _no_content()


@router.delete("/leagues/{league_id}/pin", status_code=204)
def unpin_league(ltx=Depends(league_tx, scope="function")):
    from ..control_plane import mutations
    con, access = ltx
    _write_and_recheck(con, access, lambda principal: mutations.set_pin(con, principal, access.league_id, False))
    return _no_content()


@router.put("/leagues/{league_id}/perspective")
def set_perspective(body: PerspectiveBody, ltx=Depends(league_tx, scope="function")):
    """A plain `def`, so FastAPI runs it (and its Postgres work) in the thread pool, never on the event loop.
    `league_tx` raises its 404 before the body is validated, so an invisible league never reveals a 422."""
    from ..control_plane import mutations
    con, access = ltx
    try:
        refreshed = _write_and_recheck(
            con, access, lambda principal: mutations.set_perspective(con, principal, access.league_id,
                                                                     body.team_external_id))
    except ValueError as err:
        raise HTTPException(422, str(err)) from None
    return _json(refreshed.to_json())


@router.delete("/leagues/{league_id}/perspective", status_code=204)
def clear_perspective(ltx=Depends(league_tx, scope="function")):
    from ..control_plane import mutations
    con, access = ltx
    _write_and_recheck(con, access, lambda principal: mutations.set_perspective(con, principal, access.league_id, None))
    return _no_content()
