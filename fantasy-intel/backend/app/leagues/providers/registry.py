"""Provider registry: the one place that turns deployment settings into adapters.

    get_provider(provider)  -> the LeagueProvider for that platform under the current settings
    all_capabilities()      -> every provider's ProviderCapabilities, for GET /v2/providers

Settings are read through `app.config.get_settings()` on every call (flags default OFF), and adapters are
cached per relevant setting values, so a flag change in the environment takes effect on the next call while
repeat calls reuse the Sleeper client's caches. Adapters hold no tenant state (credentials arrive per call).
"""

from __future__ import annotations

import threading

from ...config import get_settings
from ..capabilities import ProviderCapabilities
from ..context import Provider
from .base import LeagueProvider

_lock = threading.Lock()
_cache: dict[tuple, LeagueProvider] = {}


def _signature(provider: Provider) -> tuple:
    s = get_settings()
    if provider is Provider.SLEEPER:
        return (provider, str(getattr(s, "sleeper_source", "disabled") or "disabled").strip().lower())
    if provider is Provider.ESPN:
        return (provider, bool(getattr(s, "espn_public_import_enabled", False)), bool(s.has_espn_credentials))
    if provider is Provider.YAHOO:
        return (provider, bool((getattr(s, "yahoo_client_id", None) or "").strip()))
    raise ValueError(f"no adapter for provider {provider!r}")


def _build(provider: Provider, sig: tuple) -> LeagueProvider:
    if provider is Provider.SLEEPER:
        from .sleeper import SleeperProvider
        return SleeperProvider(source=sig[1])
    if provider is Provider.ESPN:
        from .espn import EspnProvider
        return EspnProvider(public_import_enabled=sig[1], owner_credentials_configured=sig[2])
    from .yahoo import YahooProvider
    return YahooProvider(client_id="present" if sig[1] else "", _use_settings=False)


def get_provider(provider: Provider) -> LeagueProvider:
    if not isinstance(provider, Provider):
        provider = Provider.parse(provider)
    sig = _signature(provider)
    with _lock:
        if sig not in _cache:
            for stale in [k for k in _cache if k[0] is provider]:
                del _cache[stale]
            _cache[sig] = _build(provider, sig)
        return _cache[sig]


def all_capabilities() -> list[ProviderCapabilities]:
    return [get_provider(p).capabilities() for p in Provider]


def data_origin(provider: Provider) -> str | None:
    """"live" | "fixture" | None (adapter disabled): the `data_origin` a LeagueView reports for this provider."""
    return getattr(get_provider(provider), "data_origin", None)


def reset_providers() -> None:
    """Drop cached adapters (tests)."""
    with _lock:
        _cache.clear()
