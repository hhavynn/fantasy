"""League context: the explicit, authorized scope every personalized computation runs under.

Rules this module encodes:
  * Fantasy platforms are named by `Provider`; a league is identified by (provider, external
    league id, season). External ids are always strings, so ESPN's 12345 and Sleeper's "12345"
    never compare equal across providers and Sleeper's 19-digit ids never lose precision.
  * The internal league id is opaque (`lg_` + random), never derived from the external id, so it
    leaks nothing about the provider league and cannot be guessed from it.
  * Who may see which league is decided by the Supabase control plane (app/control_plane/authz.py),
    never here: this module only names things. `Principal` carries the verified token's claims.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from enum import Enum


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
    """How a sync job reads a league from its provider."""
    PUBLIC = "public"            # provider serves the league without credentials (Sleeper; ESPN public leagues)
    COOKIES = "cookies"          # a member's consented ESPN cookies, decrypted only inside a sync job
    OAUTH = "oauth"              # provider OAuth token held in the credential vault (Yahoo; not enabled yet)


# External league ids: ESPN digits, Sleeper digits (up to ~19), Yahoo "<game_key>.l.<league_id>".
_EXTERNAL_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def check_external_league_id(provider: Provider, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"External league ids are strings, got {type(value).__name__} for {provider.value}")
    v = value.strip()
    if not _EXTERNAL_ID.fullmatch(v):
        raise ValueError(f"Malformed {provider.value} league id")
    if provider in (Provider.ESPN, Provider.SLEEPER) and not v.isdigit():
        raise ValueError(f"{provider.value} league ids are digits")
    return v


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


ANON_CLAIMS = '{"role": "anon"}'
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class Principal:
    """Who is asking. `user_id` is the verified Supabase JWT `sub` (a uuid), never a query parameter.
    `claims_json` is the verified claim set as compact JSON; the control plane sets it as the transaction-local
    `request.jwt.claims`, so Postgres's auth.uid() and the RLS policies see exactly what the API verified. It is
    excluded from repr() and comparisons."""
    user_id: str | None
    claims_json: str = field(default=ANON_CLAIMS, repr=False, compare=False)

    def __post_init__(self):
        if self.user_id is not None and not _UUID.fullmatch(self.user_id):
            raise ValueError("malformed user id")

    @property
    def is_authenticated(self) -> bool:
        return self.user_id is not None

    @classmethod
    def anonymous(cls) -> "Principal":
        return cls(None)

    @classmethod
    def from_claims(cls, claims: dict) -> "Principal":
        sub = claims.get("sub") if isinstance(claims, dict) else None
        if not isinstance(sub, str) or not _UUID.fullmatch(sub.lower()):
            raise ValueError("claims must carry a uuid subject")
        return cls(sub.lower(), json.dumps(claims, separators=(",", ":"), sort_keys=True, default=str))
