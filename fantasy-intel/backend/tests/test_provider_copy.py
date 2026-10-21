"""A Sleeper league's answers use Sleeper's wording (app/api/provider_copy.py): every route against the synthetic
Sleeper league (scripts/build_e2e_sleeper.py) answers without an error, never names ESPN and never attributes a
projection to Sleeper (it publishes none: a Sleeper league's projections are Ours)."""
import importlib.util
import re
import sys
from pathlib import Path

import duckdb
import pytest

from app.api import provider_copy

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture(scope="module")
def sleeper_db(tmp_path_factory):
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location("build_e2e_sleeper", SCRIPTS / "build_e2e_sleeper.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        path = tmp_path_factory.mktemp("sleeper") / "e2e-sleeper.duckdb"
        mod.build(path)
    finally:
        sys.path.remove(str(SCRIPTS))
    return path


@pytest.fixture(scope="module")
def client(sleeper_db):
    from fastapi.testclient import TestClient

    mp = pytest.MonkeyPatch()
    mp.setenv("FANTASY_DB_PATH", str(sleeper_db))
    mp.setenv("ESPN_SEASON", "2026")
    from app.main import app
    yield TestClient(app)
    mp.undo()


def test_every_route_answers_in_sleepers_words(client, sleeper_db):
    from golden.golden import cases

    con = duckdb.connect(str(sleeper_db), read_only=True)
    try:
        cs = cases(client.app, con, client)
    finally:
        con.close()
    errors, espn, sleeper_projection = [], [], []
    for c in cs:
        r = client.get(c.path)
        if r.status_code >= 400:
            if c.path.startswith(("/api/research/players/None", "/api/trades/discover/evidence")):
                continue                    # the fixture has no research player / no discovered trade to explain
            errors.append((c.path, r.status_code))
            continue
        if not provider_copy.applies(c.path.split("?")[0]):
            continue
        espn += [(c.path, m) for m in re.findall(r'"([^"]*ESPN[^"]*)"', r.text)]
        sleeper_projection += [(c.path, m) for m in re.findall(r"Sleeper(?:'s)? (?:weekly )?projections?", r.text)]
    assert errors == []
    assert espn == []
    assert sleeper_projection == []


def test_the_league_profile_and_default_source(client):
    assert client.get("/api/league-profile").json() == {"provider": "sleeper", "default_source": "independent",
                                                         "provider_projections": False}
    body = client.get("/api/start-sit").json()
    assert body["projection_source_detail"]["source"] == "independent"


def test_projection_wording_becomes_ours_and_provider_facts_become_sleeper():
    t = provider_copy.text
    assert t("Projections are ESPN's weekly projections, unmodified; lineups are exact") == \
        "Projections are Fantasy Intel's own projections (Ours), in this league's scoring; lineups are exact"
    assert t("Next week projection (ESPN)") == "Next week projection (Ours)"
    assert t("Questionable (ESPN)") == "Questionable (Sleeper)"
    assert t("Actual records are ESPN's.") == "Actual records are Sleeper's."
    assert t("ESPN") == "Ours"
    assert provider_copy.rewrite({"a": ["ESPN winner", 3], "ESPN key": None}) == {"a": ["Sleeper result", 3],
                                                                                 "ESPN key": None}
