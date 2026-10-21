"""SLEEPER_SOURCE=disabled (the Phase 4b rollback switch): a visible Sleeper league answers "temporarily unavailable";
authorization still runs first (an invisible league stays 404) and ESPN leagues are unaffected."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import league_scope


@pytest.fixture
def resolve(monkeypatch):
    from app.control_plane import authz, pg

    seen = {}

    @contextmanager
    def tx(principal):
        yield None

    monkeypatch.setattr(pg, "get_control_plane", lambda: SimpleNamespace(request_tx=tx))

    def authorize(con, principal, league_id):
        if league_id == "lg_hidden":
            raise authz.LeagueNotFound(league_id)
        return SimpleNamespace(provider=seen["provider"], snapshot=None, sync_state="queued", league_id=league_id)
    monkeypatch.setattr(authz, "authorize_league_in", authorize)
    monkeypatch.setattr("app.api.connect.nudge_probe", lambda *a: None)

    def run(provider, league_id="lg_visible"):
        seen["provider"] = provider
        with pytest.raises(HTTPException) as err:
            league_scope._resolve(SimpleNamespace(is_authenticated=False), league_id, None)
        return err.value.status_code, err.value.detail
    return run


def test_disabled_sleeper_leagues_are_temporarily_unavailable(resolve, monkeypatch):
    monkeypatch.delenv("SLEEPER_SOURCE", raising=False)
    assert resolve("sleeper") == (503, "Sleeper leagues are temporarily unavailable.")
    assert resolve("espn")[0] == 409                      # unaffected: its first sync is in progress
    assert resolve("sleeper", "lg_hidden")[0] == 404      # authorization first


def test_live_sleeper_leagues_resolve_like_any_league(resolve, monkeypatch):
    monkeypatch.setenv("SLEEPER_SOURCE", "live")
    assert resolve("sleeper")[0] == 409
