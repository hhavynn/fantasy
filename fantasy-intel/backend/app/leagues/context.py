"""League context: the explicit, authorized scope every personalized computation runs under.

Rules this module encodes:
  * Fantasy platforms are named by `Provider`; a league is identified by (provider, external
    league id, season). External ids are always strings, so ESPN's 12345 and Sleeper's "12345"
    never compare equal across providers and Sleeper's 19-digit ids never lose precision.
  * The internal league id is opaque (`lg_` + random), never derived from the external id, so it
    leaks nothing about the provider league and cannot be guessed from it.
  * A `LeagueContext` is only produced by `issue_context`, which the tenancy store calls after it
    has checked that the principal holds an active connection to the league. Building one by hand
    raises; code that needs a context must ask the store for it.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities import ProviderCapabilities


class Provider(str, Enum):
    ESPN = "espn"
    SLEEPER = "sleeper"
    YAHOO = "yahoo"

    @classmethod
    def parse(cls, value: str) -> "Provider":
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            raise ValueError(f"Unknown fantasy provider {value!r}; expected one of "
                             f"{', '.join(p.value for p in cls)}") from None


class AccessMethod(str, Enum):
    """How a principal is allowed to read a league. Stored on the connection, not on the league."""
    PUBLIC = "public"            # provider serves the league without credentials (Sleeper; ESPN public leagues)
    OWNER_ENV = "owner_env"      # the deployment owner's ESPN cookies from the server environment (legacy)
    OAUTH = "oauth"              # provider OAuth token held in the credential vault (Yahoo; not enabled yet)


class Role(str, Enum):
    OWNER = "owner"              # connected the league; may change or remove the connection
    VIEWER = "viewer"            # granted read access by an owner (not exposed in Phase 1)


class SyncStatus(str, Enum):
    NEVER = "never"
    OK = "ok"
    FAILED = "failed"
    STALE = "stale"


# External league ids: ESPN digits, Sleeper digits (up to ~19), Yahoo "<game_key>.l.<league_id>".
_EXTERNAL_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_INTERNAL_ID = re.compile(r"lg_[A-Za-z0-9_-]{16,64}")
_USER_ID = re.compile(r"[A-Za-z0-9_.:@|-]{1,128}")


class LeagueAccessDenied(LookupError):
    """The principal may not see this league. Callers answer 404, never 403, so a probe cannot
    tell 'exists but not yours' from 'does not exist'."""


def check_external_league_id(provider: Provider, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"External league ids are strings, got {type(value).__name__} for {provider.value}")
    v = value.strip()
    if not _EXTERNAL_ID.fullmatch(v):
        raise ValueError(f"Malformed {provider.value} league id")
    if provider in (Provider.ESPN, Provider.SLEEPER) and not v.isdigit():
        raise ValueError(f"{provider.value} league ids are digits")
    return v


def check_internal_league_id(value: object) -> str:
    if not isinstance(value, str) or not _INTERNAL_ID.fullmatch(value):
        raise LeagueAccessDenied("unknown league")
    return value


def new_internal_league_id() -> str:
    return "lg_" + secrets.token_urlsafe(18)


@dataclass(frozen=True)
class LeagueKey:
    """The provider-natural identity of one league season. Unique across providers."""
    provider: Provider
    external_league_id: str
    season: int

    def __post_init__(self):
        if not isinstance(self.provider, Provider):
            raise TypeError("provider must be a Provider")
        object.__setattr__(self, "external_league_id",
                           check_external_league_id(self.provider, self.external_league_id))
        if not isinstance(self.season, int) or isinstance(self.season, bool) or not 1990 <= self.season <= 2100:
            raise ValueError("season must be an NFL season year")

    @property
    def namespaced(self) -> str:
        """`provider:external_id:season`, for logs and cache keys. Not a secret, not an auth token."""
        return f"{self.provider.value}:{self.external_league_id}:{self.season}"


@dataclass(frozen=True)
class Principal:
    """Who is asking. `user_id` comes from a verified user assertion, never from a query parameter."""
    user_id: str | None

    def __post_init__(self):
        if self.user_id is not None and not _USER_ID.fullmatch(self.user_id):
            raise ValueError("malformed user id")

    @property
    def is_authenticated(self) -> bool:
        return self.user_id is not None

    @classmethod
    def anonymous(cls) -> "Principal":
        return cls(None)


@dataclass(frozen=True)
class Freshness:
    sync_status: SyncStatus
    last_synced_at: datetime | None = None      # when we last fetched from the provider (UTC, naive)
    source_updated_at: datetime | None = None   # when the provider says the data last changed, if it says
    last_error: str | None = None               # sanitized, never contains credentials or raw responses


# Per-process key sealing the authorizing fields of every issued context. A context copied with
# dataclasses.replace() keeps its old seal, so changing its principal, league, role or access breaks it.
_SEAL_KEY = secrets.token_bytes(32)


def _seal(league_id: object, key: object, principal: object, role: object, access: object) -> str:
    parts = [str(league_id), getattr(key, "namespaced", repr(key)), str(getattr(principal, "user_id", None)),
             str(getattr(role, "value", role)), str(getattr(access, "value", access))]
    return hmac.new(_SEAL_KEY, "\x1f".join(parts).encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class LeagueContext:
    """An authorized (principal, league) pair. Obtain one from the tenancy store, never by hand."""
    league_id: str                               # internal opaque id
    key: LeagueKey
    principal: Principal
    role: Role
    access: AccessMethod
    name: str | None
    my_team_external_id: str | None              # the team this user analyses; None until they pick one
    freshness: Freshness
    capabilities: "ProviderCapabilities | None" = None
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        expected = _seal(self.league_id, self.key, self.principal, self.role, self.access)
        if not isinstance(self._issuer, str) or not hmac.compare_digest(self._issuer, expected):
            raise RuntimeError("LeagueContext is issued by an authorization check (tenancy store), not built by hand")
        check_internal_league_id(self.league_id)
        if not self.principal.is_authenticated:
            raise LeagueAccessDenied("anonymous principals hold no league context")

    @property
    def provider(self) -> Provider:
        return self.key.provider

    @property
    def season(self) -> int:
        return self.key.season

    @property
    def tenant_id(self) -> str:
        return self.principal.user_id  # type: ignore[return-value]  # authenticated, checked above


def issue_context(**kwargs) -> LeagueContext:
    """For the tenancy store's authorization check only (and tests of it)."""
    seal = _seal(kwargs.get("league_id"), kwargs.get("key"), kwargs.get("principal"), kwargs.get("role"),
                 kwargs.get("access"))
    return LeagueContext(**kwargs, _issuer=seal)
