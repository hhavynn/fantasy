"""Multi-league API (/v2). Feature-flagged: every route answers 404 unless MULTILEAGUE_ENABLED is on.

Identity is the signed user assertion (app/leagues/auth.py), never the service API key. League routes
only ever see a LeagueContext issued by the tenancy store for the verified principal; an unknown, someone
else's, revoked, malformed or anonymous league id is the same 404 (never 403). Settings and teams are
served from stored snapshots only: a read never calls a provider.

Connecting is the one path that talks to a provider. It reports a provider that cannot do the job as 501
with the capability reason, a private league as 422, and a provider failure as 404/502/503 with a sanitized
message; it never stores a fake success.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool

from ..config import get_settings
from ..leagues.auth import current_principal
from ..leagues.capabilities import Capability, CapabilityUnavailable, ProviderCapabilities, Support
from ..leagues.context import AccessMethod, LeagueAccessDenied, LeagueContext, LeagueKey, Principal, Provider
from ..leagues.credentials import CredentialError, OwnerEnvCredentials
from ..leagues.providers.base import LeagueAccess, LeagueNotFound, ProviderError, ProviderUnavailable
from ..leagues.store import TenancyStore, sanitize_error
from ..leagues.views import (
    league_view, provenance_to_json, rosters_to_json, settings_to_json, teams_to_json,
)

log = logging.getLogger(__name__)

NEEDED = (Capability.LEAGUE_SETTINGS, Capability.TEAMS, Capability.ROSTERS)
PRIVATE_MESSAGE = ("This league is private; private leagues need an authorized connection method "
                   "that is not available yet.")
_CONNECT_FIELDS = {"provider", "external_league_id", "season"}


def require_enabled() -> None:
    """Router-level gate, evaluated before every other dependency, so a flag-off deployment answers 404
    even for a garbage assertion header."""
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


_STORES: dict[str, TenancyStore] = {}


def get_tenancy_store() -> TenancyStore:
    """One store per database file (its lock serialises this process's writes)."""
    path = get_settings().tenancy_db_path
    store = _STORES.get(str(path))
    if store is None:
        store = _STORES.setdefault(str(path), TenancyStore(path))
    return store


def authenticated_principal(principal: Principal = Depends(current_principal)) -> Principal:
    if not principal.is_authenticated:
        raise HTTPException(401, "Sign in to use this endpoint.")
    return principal


def _context_or_404(store: TenancyStore, principal: Principal, league_id: str) -> LeagueContext:
    try:
        return store.authorize(principal, league_id)
    except LeagueAccessDenied:
        raise HTTPException(404, "League not found.") from None


def _view(store: TenancyStore, ctx: LeagueContext) -> dict:
    return league_view(ctx, data_origin=store.league_meta(ctx).data_origin)


async def _json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Request body must be a JSON object.") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "Request body must be a JSON object.")
    return body


# ---- routes ---------------------------------------------------------------------------------------

@router.get("/providers")
def list_providers(registry: ProviderRegistry = Depends(provider_registry),
                   _principal: Principal = Depends(current_principal)):
    """Public information about what each provider can do in this deployment. Anonymous is fine, but a
    present-and-invalid assertion is still a 401 (same rule as every other /v2 route)."""
    return {"providers": [c.as_dict() for c in registry.all_capabilities()]}


@router.get("/leagues")
def list_leagues(principal: Principal = Depends(authenticated_principal),
                 store: TenancyStore = Depends(get_tenancy_store)):
    return {"leagues": [_view(store, ctx) for ctx in store.list_contexts(principal)]}


@router.post("/leagues", status_code=201)
async def connect_league(request: Request, principal: Principal = Depends(authenticated_principal),
                         store: TenancyStore = Depends(get_tenancy_store),
                         registry: ProviderRegistry = Depends(provider_registry)):
    body = await _json_object(request)
    if set(body) - _CONNECT_FIELDS:
        # Includes any attempt to send cookies or tokens: they are never accepted, in any field.
        raise HTTPException(400, "Unexpected field in request body; send provider, external_league_id and season.")
    try:
        provider = Provider.parse(body.get("provider"))
        season = body.get("season")
        if isinstance(season, bool) or not isinstance(season, int):
            raise ValueError("season is required and must be an integer")
        key = LeagueKey(provider, body.get("external_league_id"), season)  # type: ignore[arg-type]
    except (TypeError, ValueError) as err:
        raise HTTPException(400, str(err)) from None
    return await run_in_threadpool(_connect_flow, principal, key, store, registry)


def _unavailable(caps: ProviderCapabilities, owner_access: bool) -> str | None:
    """The reason this deployment cannot read a league from the provider, or None. With the owner's own
    credentials in hand a REQUIRES_AUTH capability is usable (that is what the credentials are for)."""
    if not caps.enabled:
        return caps.reason or f"{caps.provider.value} is not enabled in this deployment."
    for cap in NEEDED:
        state = caps.state(cap)
        if state.available or (owner_access and state.support is Support.REQUIRES_AUTH):
            continue
        note = f" - {state.note}" if state.note else ""
        return f"{caps.provider.value} cannot provide {cap.value}: {state.support.value}{note}"
    return None


def _connect_flow(principal: Principal, key: LeagueKey, store: TenancyStore, registry: ProviderRegistry) -> dict:
    settings = get_settings()
    try:
        adapter = registry.get_provider(key.provider)
    except ProviderUnavailable as err:
        raise HTTPException(501, sanitize_error(err)) from None
    except ProviderError as err:
        raise HTTPException(503, sanitize_error(err)) from None

    owner_access = (settings.owner_user_id is not None and principal.user_id == settings.owner_user_id
                    and key.provider is Provider.ESPN and settings.has_espn_credentials)
    reason = _unavailable(adapter.capabilities(), owner_access)
    if reason is not None:
        raise HTTPException(501, reason)   # truthful: not enabled / not supported, never a fake success

    try:
        if owner_access:
            access = LeagueAccess(AccessMethod.OWNER_ENV, OwnerEnvCredentials().for_owner(principal, key.provider))
        else:
            access = LeagueAccess(AccessMethod.PUBLIC)
    except CredentialError as err:
        raise HTTPException(501, str(err)) from None

    try:
        sourced_settings = adapter.fetch_settings(key, access)
        league_settings = sourced_settings.value
        if access.method is AccessMethod.PUBLIC and league_settings.is_public is not True:
            raise HTTPException(422, PRIVATE_MESSAGE)    # checked before fetching anything else
        sourced_teams = adapter.fetch_teams(key, access)
        sourced_rosters = adapter.fetch_rosters(key, access)
        if league_settings.key != key or any(t.key != key for t in sourced_teams.value):
            raise ProviderError("The provider returned data for a different league.")
    except HTTPException:
        raise
    except CapabilityUnavailable as err:
        raise HTTPException(501, str(err)) from None
    except Exception as err:   # noqa: BLE001 - provider code is untrusted; nothing unsanitized may escape
        message = sanitize_error(err)
        log.warning("league connect failed for %s: %s", key.namespaced, message)
        # Only a user already connected to this league may mark it failed: a stranger's failed attempt must
        # not change what the real members see.
        existing = store.context_for_key(principal, key)
        if existing is not None:
            store.record_sync_failure(existing, err)
        if isinstance(err, LeagueNotFound):
            raise HTTPException(404, message) from None
        raise HTTPException(503 if isinstance(err, ProviderUnavailable) else 502, message) from None

    prov = sourced_settings.provenance
    origin = "fixture" if any(s.provenance.source.startswith("fixture:")
                              for s in (sourced_settings, sourced_teams, sourced_rosters)) else "live"
    try:
        ctx = store.connect(principal, key, access.method, name=league_settings.name,
                            is_public=league_settings.is_public, data_origin=origin)
    except PermissionError:
        raise HTTPException(422, PRIVATE_MESSAGE) from None
    store.save_snapshot(ctx, "settings", settings_to_json(league_settings), prov)
    store.save_snapshot(ctx, "teams", teams_to_json(sourced_teams.value), sourced_teams.provenance)
    store.save_snapshot(ctx, "rosters", rosters_to_json(sourced_rosters.value), sourced_rosters.provenance)
    store.record_sync_success(ctx, fetched_at=prov.fetched_at, source_updated_at=prov.source_updated_at)
    return _view(store, store.authorize(principal, ctx.league_id))


@router.get("/leagues/{league_id}")
def get_league(league_id: str, principal: Principal = Depends(current_principal),
               store: TenancyStore = Depends(get_tenancy_store)):
    return _view(store, _context_or_404(store, principal, league_id))


def _snapshot_or_404(store: TenancyStore, ctx: LeagueContext, kind: str):
    try:
        found = store.load_snapshot(ctx, kind)
    except LeagueAccessDenied:
        raise HTTPException(404, "League not found.") from None
    if found is None:
        raise HTTPException(404, f"No {kind} have been synced for this league yet.")
    return found


@router.get("/leagues/{league_id}/settings")
def get_league_settings(league_id: str, principal: Principal = Depends(current_principal),
                        store: TenancyStore = Depends(get_tenancy_store)):
    ctx = _context_or_404(store, principal, league_id)
    payload, prov = _snapshot_or_404(store, ctx, "settings")
    return {"league_id": ctx.league_id, **payload, "provenance": provenance_to_json(prov)}


@router.get("/leagues/{league_id}/teams")
def get_league_teams(league_id: str, principal: Principal = Depends(current_principal),
                     store: TenancyStore = Depends(get_tenancy_store)):
    ctx = _context_or_404(store, principal, league_id)
    payload, prov = _snapshot_or_404(store, ctx, "teams")
    return {"league_id": ctx.league_id, "teams": payload, "provenance": provenance_to_json(prov)}


@router.put("/leagues/{league_id}/my-team")
async def set_my_team(league_id: str, request: Request, principal: Principal = Depends(current_principal),
                      store: TenancyStore = Depends(get_tenancy_store)):
    ctx = await run_in_threadpool(_context_or_404, store, principal, league_id)   # 404 before any body validation
    body = await _json_object(request)
    team_id = body.get("external_team_id")
    if set(body) != {"external_team_id"} or not isinstance(team_id, str):
        raise HTTPException(400, "Send {\"external_team_id\": \"<team id as a string>\"}.")

    def apply() -> dict:
        try:
            updated = store.set_my_team(ctx, team_id)
        except LeagueAccessDenied:
            raise HTTPException(404, "League not found.") from None
        except ValueError as err:
            raise HTTPException(422, str(err)) from None
        return _view(store, updated)

    return await run_in_threadpool(apply)


@router.delete("/leagues/{league_id}", status_code=204)
def disconnect_league(league_id: str, principal: Principal = Depends(current_principal),
                      store: TenancyStore = Depends(get_tenancy_store)):
    store.revoke(_context_or_404(store, principal, league_id))
    return Response(status_code=204)
