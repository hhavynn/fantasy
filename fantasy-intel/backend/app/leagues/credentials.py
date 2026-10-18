"""Credential access. Phase 1 stores NO user credentials.

* `CredentialVault` is the interface a future encrypted store (KMS-backed) will implement. Every call is
  checked against the requesting principal AND league, so a ref id leaked from one tenant is useless to
  another. No encryption is implemented here and no dependency was added for one.
* `DisabledVault` is the only vault wired today: it refuses to store or return anything.
* `OwnerEnvCredentials` is the legacy path: the deployment owner's own ESPN cookies from the server
  environment, released only to the owner on an OWNER_ENV connection. Users can never supply cookies.
* Secrets travel inside `Secret`/`EspnCookies`, whose repr/str never show values, so an accidental
  log line, exception message or f-string cannot leak them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import get_settings
from .context import AccessMethod, LeagueContext, Principal, Provider


class CredentialError(RuntimeError):
    pass


class CredentialStorageUnavailable(CredentialError):
    """No vault (or no server credential) is configured. The message is safe to show."""


class CredentialAccessDenied(CredentialError, PermissionError):
    """The principal/league may not use this credential. Callers treat it like any other denial."""


class Secret:
    """Holds one secret string. `reveal()` is the only way out; repr/str/format never show it,
    and it cannot be pickled or compared by value."""
    __slots__ = ("_value",)

    def __init__(self, value: str):
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        return self._value

    def __setattr__(self, name, value):
        raise AttributeError("Secret is immutable")

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __reduce__(self):
        raise TypeError("Secret cannot be serialized")


class EspnCookies:
    """The owner's espn_s2 + SWID. Plain class (not a dataclass) so asdict()/repr() cannot expose them."""
    __slots__ = ("_s2", "_swid")

    def __init__(self, espn_s2: str, swid: str):
        object.__setattr__(self, "_s2", espn_s2)
        object.__setattr__(self, "_swid", swid)

    @property
    def espn_s2(self) -> str:
        return self._s2

    @property
    def swid(self) -> str:
        return self._swid

    def as_cookies(self) -> dict[str, str]:
        """For the ESPN HTTP client only. Never log the result."""
        return {"espn_s2": self._s2, "SWID": self._swid}

    def __setattr__(self, name, value):
        raise AttributeError("EspnCookies is immutable")

    def __repr__(self) -> str:
        return "EspnCookies(<redacted>)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __reduce__(self):
        raise TypeError("EspnCookies cannot be serialized")


class CredentialVault(ABC):
    """Secret storage keyed by `ref_id`. Implementations must verify that the ref was created by
    `ctx.principal` for `ctx.league_id` before returning or revoking it; a mismatch raises
    CredentialAccessDenied (never returns another tenant's secret)."""

    @abstractmethod
    def put(self, ctx: LeagueContext, ref_id: str, secret: Secret) -> None: ...

    @abstractmethod
    def get(self, ctx: LeagueContext, ref_id: str) -> Secret: ...

    @abstractmethod
    def revoke(self, ctx: LeagueContext, ref_id: str) -> None: ...


class DisabledVault(CredentialVault):
    _MESSAGE = "No credential encryption key / KMS is configured; storing user credentials is disabled."

    def put(self, ctx: LeagueContext, ref_id: str, secret: Secret) -> None:
        raise CredentialStorageUnavailable(self._MESSAGE)

    def get(self, ctx: LeagueContext, ref_id: str) -> Secret:
        raise CredentialStorageUnavailable(self._MESSAGE)

    def revoke(self, ctx: LeagueContext, ref_id: str) -> None:
        return None     # nothing is ever stored, so there is nothing to destroy


class OwnerEnvCredentials:
    """Server-env ESPN cookies for the deployment owner only (AccessMethod.OWNER_ENV)."""

    @staticmethod
    def _owner_may_use(principal: Principal, provider: Provider) -> None:
        owner = get_settings().owner_user_id
        if owner is None or principal.user_id != owner or provider is not Provider.ESPN:
            raise CredentialAccessDenied("Server-held credentials are not available to this user.")

    @staticmethod
    def _cookies() -> EspnCookies:
        s = get_settings()
        if not s.has_espn_credentials:
            raise CredentialStorageUnavailable("ESPN_S2 / SWID are not configured on the server.")
        return EspnCookies(s.espn_s2, s.swid)

    def for_owner(self, principal: Principal, provider: Provider) -> EspnCookies:
        """Before a connection exists (the connect flow fetches first)."""
        self._owner_may_use(principal, provider)
        return self._cookies()

    def for_context(self, ctx: LeagueContext) -> EspnCookies:
        """Only for a context whose connection was made with OWNER_ENV by the owner."""
        if ctx.access is not AccessMethod.OWNER_ENV:
            raise CredentialAccessDenied("This connection does not use server-held credentials.")
        self._owner_may_use(ctx.principal, ctx.provider)
        return self._cookies()
