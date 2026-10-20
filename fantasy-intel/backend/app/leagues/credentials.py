"""Credential value types. Storage and encryption live in app/control_plane/credential_crypto.py (envelope
encryption; the API holds only public keys) and the fi.league_credentials table (docs/multileague/ADR-0002).

Secrets travel inside `Secret`/`EspnCookies`, whose repr/str never show values, so an accidental log line,
exception message or f-string cannot leak them.
"""

from __future__ import annotations


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
