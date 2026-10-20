"""Shared helpers for tests/test_league_providers*.py (no tests here)."""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from app.leagues.context import AccessMethod, LeagueKey, Provider
from app.leagues.providers.base import LeagueAccess

SLEEPER_L1 = "1180000000000000001"       # dynasty, FLEX + SUPER_FLEX, FAAB, rec 0.5 (tests/fixtures/sleeper)
SLEEPER_L2 = "1180000000000000002"       # redraft, FLEX only, half PPR + TE premium, rolling waivers
SEASON = 2026

PUBLIC = LeagueAccess(AccessMethod.PUBLIC)
S2_SECRET = "AEB-very-secret-espn-s2-cookie-value"
SWID_SECRET = "{11111111-SECRET-SWID-2222}"


def owner_access() -> LeagueAccess:
    return LeagueAccess(AccessMethod.COOKIES, SimpleNamespace(espn_s2=S2_SECRET, swid=SWID_SECRET))


def skey(league_id: str = SLEEPER_L1, season: int = SEASON) -> LeagueKey:
    return LeagueKey(Provider.SLEEPER, league_id, season)


@pytest.fixture
def no_network(monkeypatch):
    """Any socket use in a test is a failure: provider tests run on fixtures only."""
    def refuse(*a, **k):
        raise AssertionError("network access attempted in an offline provider test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture
def clean_env(monkeypatch):
    """No provider flags or ESPN credentials from the developer's environment / .env file."""
    import os
    for name in ("SLEEPER_SOURCE", "ESPN_PUBLIC_IMPORT_ENABLED", "YAHOO_CLIENT_ID", "ESPN_S2", "SWID",
                 "MULTILEAGUE_ENABLED", "CREDENTIALS_WRAP_KEY_VERSION",
                 *[k for k in os.environ if k.startswith("CREDENTIALS_PUBKEY_V")]):
        monkeypatch.delenv(name, raising=False)
    from app.leagues.providers.registry import reset_providers
    reset_providers()
    yield monkeypatch
    reset_providers()
