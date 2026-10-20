"""Multi-league API (/v2). Feature-flagged: every route answers 404 unless MULTILEAGUE_ENABLED is on.

Identity is the Supabase access token verified in app/control_plane/jwt_auth.py; the service API key is never a user.
Every league route authorizes in Postgres first (app/control_plane/authz.py, via the `league_tx` dependency):
unknown, someone else's, deleted, expired-public and malformed league ids are the same 404, never 403. Only then does
a mutation check sign-in (401). Pins and perspective are preferences: never verified against league data, never
granting anything. Phase 2 adds the league-file read routes; Phase 3 adds connect, invites and credentials on top of
app/control_plane/mutations.py.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Protocol

from psycopg.errors import InsufficientPrivilege

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr

from ..config import get_settings
from ..control_plane import mutations
from ..control_plane.authz import Access, LeagueNotFound, authorize_league_in, check_league_id
from ..control_plane.jwt_auth import current_principal
from ..control_plane.pg import ControlPlaneUnavailable, get_control_plane
from ..leagues.capabilities import ProviderCapabilities
from ..leagues.context import Principal, Provider

log = logging.getLogger(__name__)
NOT_FOUND = "League not found."
SIGN_IN = "Sign in to use this endpoint."
UNAVAILABLE = "The control plane is unavailable."
NO_STORE = {"Cache-Control": "private, no-store"}


def require_enabled() -> None:
    """Router-level gate, evaluated before every other dependency, so a flag-off deployment answers 404 even for a
    garbage Authorization header and never opens Postgres."""
    if not get_settings().multileague_enabled:
        raise HTTPException(404, "Not Found")


router = APIRouter(prefix="/v2", dependencies=[Depends(require_enabled)])


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
    try:
        with get_control_plane().request_tx(principal) as con:
            yield con, principal
    except ControlPlaneUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None


REQUEST_TX = Depends(request_tx, scope="function")


def valid_league_id(league_id: str) -> str:
    """The path parameter's shape, checked before any Postgres work: a malformed id never takes a pool slot and is
    the same 404 even while the control plane is down."""
    try:
        return check_league_id(league_id)
    except LeagueNotFound:
        raise HTTPException(404, NOT_FOUND) from None


def league_tx(league_id: str = Depends(valid_league_id), tx=REQUEST_TX) -> tuple[Any, Access]:
    """Authorize FIRST. Everything a league route does happens inside this transaction, after this check.
    `league_id` is declared before `tx`, so FastAPI validates it before request_tx opens a connection."""
    con, principal = tx
    try:
        return con, authorize_league_in(con, principal, league_id)
    except LeagueNotFound:
        raise HTTPException(404, NOT_FOUND) from None


@contextmanager
def _still_visible() -> Iterator[None]:
    """A preference write after `league_tx` authorized. Statements run at READ COMMITTED, so a sync can make the
    league private (or revoke the membership) in between; the write's RLS check then denies it. That is the same
    answer authorization would now give: 404. Any other permission error is a bug and stays a 500."""
    try:
        yield
    except InsufficientPrivilege as err:
        if "row-level security" not in str(err):
            raise
        raise HTTPException(404, NOT_FOUND) from None


def _signed_in(access: Access) -> Principal:
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
    con, principal = tx
    try:
        return _json(mutations.me(con, principal))
    except mutations.SignInRequired:
        raise HTTPException(401, SIGN_IN) from None


@router.get("/leagues")
def list_leagues(tx=REQUEST_TX):
    con, principal = tx
    try:
        ids = mutations.list_my_leagues(con, principal)
    except mutations.SignInRequired:
        raise HTTPException(401, SIGN_IN) from None
    out = []
    for league_id in ids:
        try:
            out.append(authorize_league_in(con, principal, league_id).to_json())
        except LeagueNotFound:
            continue     # pinned, then went private or expired: it simply drops out of the list
    return _json({"leagues": out})


@router.get("/leagues/{league_id}")
def get_league(ltx=Depends(league_tx, scope="function")):
    _, access = ltx
    return _json(access.to_json())


def _write_and_recheck(con, access: Access, write) -> Access:
    """Run a preference write for an authorized caller, then authorize again in the same transaction.

    Statements run at READ COMMITTED, so a sync can make the league private (or revoke the membership) after
    `league_tx` authorized. An insert or update then fails its RLS check (`_still_visible`); a delete has no such
    check, so the re-authorization after the write is what notices. Either way the answer is the route's 404 and
    the HTTPException rolls the transaction back."""
    principal = _signed_in(access)
    with _still_visible():
        write(principal)
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
    con, access = ltx
    _write_and_recheck(con, access, lambda principal: mutations.set_pin(con, principal, access.league_id, True))
    return _no_content()


@router.delete("/leagues/{league_id}/pin", status_code=204)
def unpin_league(ltx=Depends(league_tx, scope="function")):
    con, access = ltx
    _write_and_recheck(con, access, lambda principal: mutations.set_pin(con, principal, access.league_id, False))
    return _no_content()


@router.put("/leagues/{league_id}/perspective")
def set_perspective(body: PerspectiveBody, ltx=Depends(league_tx, scope="function")):
    """A plain `def`, so FastAPI runs it (and its Postgres work) in the thread pool, never on the event loop.
    `league_tx` raises its 404 before the body is validated, so an invisible league never reveals a 422."""
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
    con, access = ltx
    _write_and_recheck(con, access, lambda principal: mutations.set_perspective(con, principal, access.league_id, None))
    return _no_content()
