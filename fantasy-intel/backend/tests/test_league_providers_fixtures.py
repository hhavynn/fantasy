"""The fixture transport and data: offline, URL-addressed, and in step with tests/fixtures/sleeper."""

import json
from pathlib import Path

import pytest

from app.leagues.providers.fixtures import FIXTURE_DIR, FixtureTransport, fixture_transport, load_fixture_id_map
from app.sleeper.client import BASE_URL, Response
from test_league_providers_support import SLEEPER_L1, SLEEPER_L2, no_network, clean_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("no_network")

TESTS_FIXTURES = Path(__file__).parent / "fixtures" / "sleeper"


def get(t, path):
    return t(BASE_URL + path, 5)


def test_fixture_dir_lives_inside_the_application_package():
    """Vercel excludes tests/**: fixture mode must not depend on it."""
    assert "app" in FIXTURE_DIR.parts and "tests" not in FIXTURE_DIR.parts and FIXTURE_DIR.is_dir()


def test_shared_files_match_the_recorded_sleeper_fixtures():
    """The base league's files are copies of tests/fixtures/sleeper (the architect's recorded shapes)."""
    for rel in ("league.json", "users.json", "rosters.json"):
        assert (FIXTURE_DIR / "leagues" / SLEEPER_L1 / rel).read_bytes() == (TESTS_FIXTURES / rel).read_bytes(), rel
    assert (FIXTURE_DIR / "state.json").read_bytes() == (TESTS_FIXTURES / "state.json").read_bytes()
    base = json.loads((TESTS_FIXTURES / "players.json").read_text())
    ours = json.loads((FIXTURE_DIR / "players.json").read_text())
    assert all(ours[k] == v for k, v in base.items()), "players only gain entries (the three Mike Williams)"
    assert {k for k in ours} - set(base) == {"7001", "7002", "7003"}
    base_csv = (TESTS_FIXTURES / "ff_playerids.csv").read_text()
    assert (FIXTURE_DIR / "ff_playerids.csv").read_text().startswith(base_csv)


def test_transport_serves_by_url_and_records_calls():
    t = fixture_transport()
    assert t.is_fixture and isinstance(t, FixtureTransport)
    r = get(t, f"/league/{SLEEPER_L1}")
    assert r.status == 200 and json.loads(r.body)["league_id"] == SLEEPER_L1
    assert json.loads(get(t, f"/league/{SLEEPER_L2}").body)["name"].startswith("Fixture Redraft")
    assert json.loads(get(t, "/state/nfl").body)["season"] == "2026"
    assert isinstance(json.loads(get(t, f"/league/{SLEEPER_L1}/matchups/6").body), list)
    assert t.calls == [f"/league/{SLEEPER_L1}", f"/league/{SLEEPER_L2}", "/state/nfl",
                       f"/league/{SLEEPER_L1}/matchups/6"]
    assert t.league_ids() == [SLEEPER_L1, SLEEPER_L2]


def test_unknown_things_answer_like_sleeper_and_never_raise():
    t = fixture_transport()
    assert get(t, "/league/999").body == b"null" and get(t, "/league/999/rosters").body == b"null"
    assert get(t, f"/league/{SLEEPER_L1}/matchups/12").body == b"[]"
    assert get(t, "/user/nobody").body == b"null"
    assert get(t, "/definitely/not/a/route").status == 404


def test_transport_refuses_other_hosts_and_path_tricks():
    t = fixture_transport()
    with pytest.raises(AssertionError):
        t("https://example.com/v1/state/nfl", 5)
    for evil in ("/user/..%2F..%2Fstate", "/user/../state/nfl", f"/league/{SLEEPER_L1}/../../../etc/passwd"):
        assert get(t, evil).status in (404, 200) and b"root:" not in get(t, evil).body


def test_fixture_id_map_loads_like_the_real_one():
    m = load_fixture_id_map()
    assert m.by_sleeper["4046"][0].gsis_id == "00-0033873"
    assert {e.gsis_id for e in m.by_sleeper["2133"]} == {"00-0031000", "00-0031001"}
    assert "7003" not in m.by_sleeper and m.by_sleeper["7001"][0].name == m.by_sleeper["7002"][0].name


def test_fixture_data_is_invented_not_recorded_from_real_leagues():
    text = "".join(p.read_text() for p in FIXTURE_DIR.rglob("*.json"))
    assert "Fixture" in text and "7000000000" in text
