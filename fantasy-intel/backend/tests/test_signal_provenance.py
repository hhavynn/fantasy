"""Signal provenance (signal-provenance.v1, app/api/signal_provenance.py): the derived status never says "current"
without a known capture time, a newer failed sync or a re-synced roster marks a signal stale whatever its age,
missing values count as missing (never 0), a requested-but-unavailable Ours run is reported as the ESPN numbers the
page actually shows, and every headline endpoint carries the block without league ids or file paths. Built on the
E2E fixture league; every number is an invented test input."""

import json
import shutil
from datetime import datetime, timedelta, timezone

import duckdb
import pytest
from fastapi.testclient import TestClient

from app.api import signal_provenance as sp
from golden.golden import E2E_SEASON, build_e2e_db

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
KEYS = {"signal", "label", "source", "captured_at", "snapshot_version", "coverage", "reason_unavailable",
        "stale_reason", "stale_after_hours"}


def sig(**kw):
    base = dict(key="projections", label="x", source="espn", captured_at=NOW - timedelta(hours=2), snapshot_version="v")
    base.update(kw)
    return sp.signal(base.pop("key"), base.pop("label"), base.pop("source"), base.pop("captured_at"),
                     base.pop("snapshot_version"), **base)


# ---- the descriptor and its derived status ------------------------------------------------------------------------

def test_every_key_is_always_present_and_timestamps_are_utc_z():
    s = sig(captured_at=datetime(2026, 10, 22, 10, 0))           # naive = UTC, as the syncs write it
    assert set(s) == KEYS
    assert s["captured_at"] == "2026-10-22T10:00:00Z"
    assert sp.iso_utc("2026-10-22T10:00:00+02:00") == "2026-10-22T08:00:00Z"


@pytest.mark.parametrize("raw", [None, "", "not a time", "yesterday"])
def test_an_unknown_or_unparseable_time_is_unknown_never_now(raw):
    s = sig(captured_at=raw)
    assert s["captured_at"] is None
    assert sp.status(s, NOW) == "unknown"


def test_status_precedence():
    assert sp.status(sig(), NOW) == "current"
    assert sp.status(sig(captured_at=NOW - timedelta(hours=37)), NOW) == "stale"
    assert sp.status(sig(stale_reason="newer sync failed"), NOW) == "stale"
    assert sp.status(sig(coverage=sp.coverage(8, 9, "starters with a projection")), NOW) == "partial"
    assert sp.status(sig(coverage=sp.coverage(0, 9, "starters with a projection")), NOW) == "unavailable"
    assert sp.status(sig(reason_unavailable="no reports"), NOW) == "unavailable"
    assert sp.status(sig(reason_unavailable="1 missing", coverage=sp.coverage(8, 9, "s")), NOW) == "partial"
    # unavailable outranks an unknown time; an unknown time outranks partial coverage
    assert sp.status(sig(captured_at=None, reason_unavailable="no run"), NOW) == "unavailable"
    assert sp.status(sig(captured_at=None, coverage=sp.coverage(8, 9, "s")), NOW) == "unknown"


def test_missing_counts_are_not_zero():
    assert sp.coverage(None, 9, "s") is None and sp.coverage(3, None, "s") is None
    lineup = [{"player_id": 1, "points": 12.5}, {"player_id": 2, "points": None}, {"player_id": 3, "points": 0.0},
              {"player_id": 4, "points": None, "on_bye": True}, {"player_id": None, "points": None}]
    cov = sp.projected_coverage(lineup, "points")
    # 0.0 is a measured zero (counted); None is missing (not counted); byes and empty slots are not expected
    assert cov == {"available": 2, "total": 3, "label": "starters with a projection"}
    assert sp.projected_coverage([], "points") is None
    assert sp.merge_coverage(None, None) is None
    assert sp.merge_coverage(cov, sp.coverage(4, 4, "x")) == {"available": 6, "total": 7, "label": cov["label"]}


# ---- source conflicts ---------------------------------------------------------------------------------------------

def _prov(sync, provider="espn"):
    return sp.Provenance(duckdb.connect(), sync=sync, provider=provider)


def test_a_newer_failed_sync_marks_the_league_stale_whatever_its_age():
    ok, failed = NOW - timedelta(hours=1), NOW - timedelta(minutes=10)
    p = _prov({"last_success": {"espn": ok}, "latest_runs": [{"source": "espn", "status": "error", "finished_at": failed}]})
    league = p.league()
    assert league["captured_at"] == sp.iso_utc(ok)
    assert "did not succeed" in league["stale_reason"]
    assert sp.status(league, NOW) == "stale"
    # a success after the failure clears it
    p = _prov({"last_success": {"espn": NOW}, "latest_runs": [{"source": "espn", "status": "ok", "finished_at": NOW}]})
    assert p.league()["stale_reason"] is None and sp.status(p.league(), NOW) == "current"


def test_a_league_never_synced_is_unknown_not_live():
    p = _prov({"last_success": {}, "latest_runs": [{"source": "espn", "status": "error", "finished_at": NOW}]})
    league = p.league()
    assert league["captured_at"] is None and "has not succeeded" in league["stale_reason"]
    assert sp.status(league, NOW) == "unknown"


def test_ours_requested_but_unavailable_reports_the_espn_numbers_shown():
    p = _prov({"last_success": {"espn": NOW - timedelta(hours=3)}, "latest_runs": []})
    s = p.projections("independent", {"available": False, "reason": "No run for week 3", "run": None})
    assert s["source"] == "espn" and s["label"] == "ESPN weekly projections"
    assert s["captured_at"] == sp.iso_utc(NOW - timedelta(hours=3))


def test_ours_available_uses_the_run_time_not_the_espn_sync():
    p = _prov({"last_success": {"espn": NOW - timedelta(hours=1)}, "latest_runs": []})
    run_at = NOW - timedelta(hours=50)
    s = p.projections("independent", {"available": True, "run": {"created_at": run_at.isoformat()}})
    assert s["source"] == "fantasy_intel" and s["captured_at"] == sp.iso_utc(run_at)
    assert sp.status(s, NOW) == "stale"


def test_a_mixed_ours_signal_says_so_and_takes_its_oldest_run():
    """Players the run abstains on keep ESPN's numbers and kickers/D/STs come from their own runs: the label names the
    mix and the age is the oldest run shown, never the newest."""
    p = _prov({"last_success": {"espn": NOW}, "latest_runs": []})
    main, kicker = NOW - timedelta(hours=2), NOW - timedelta(hours=40)
    s = p.projections("independent", {"available": True, "run": {"created_at": main.isoformat()},
                                      "counts": {"ours": 150, "ours_zero": 3, "espn": 12},
                                      "kicker_run": {"created_at": kicker.isoformat()}, "dst_run": None})
    assert s["source"] == "fantasy_intel" and s["captured_at"] == sp.iso_utc(kicker)
    assert "ESPN's numbers for 12 players" in s["label"] and "kickers from their own runs" in s["label"]
    assert sp.status(s, NOW) == "stale"
    # a pure run keeps the plain label
    s = p.projections("independent", {"available": True, "run": {"created_at": main.isoformat()},
                                      "counts": {"ours": 150, "ours_zero": 0, "espn": 0}})
    assert s["label"] == "Fantasy Intel weekly projections (Ours)" and s["captured_at"] == sp.iso_utc(main)


def test_a_league_without_provider_projections_and_no_run_is_unavailable():
    p = _prov({"last_success": {"sleeper": NOW}, "latest_runs": []}, provider="sleeper")
    s = p.projections(None, {"available": False, "reason": "No run for week 3", "run": None})
    assert s["source"] == "fantasy_intel" and s["reason_unavailable"] == "No run for week 3"
    assert sp.status(s, NOW) == "unavailable"
    assert "ESPN" not in json.dumps(p.league()) and "ESPN" not in json.dumps(s)


def test_espn_projections_with_missing_starters_are_partial_and_say_why():
    p = _prov({"last_success": {"espn": NOW}, "latest_runs": []})
    s = p.projections("espn", cov=sp.coverage(7, 9, "starters with a projection"))
    assert "2 of 9 have no projection" in s["reason_unavailable"] and "not 0" in s["reason_unavailable"]
    assert sp.status(s, NOW) == "partial"


def test_analysis_never_computed_is_unavailable_and_an_undated_one_is_unknown():
    p = _prov({"last_success": {}, "latest_runs": []})
    assert sp.status(p.analysis("Waivers", None, reason="No waiver analysis is stored."), NOW) == "unavailable"
    assert sp.status(p.analysis("Waivers", None), NOW) == "unknown"


# ---- the endpoints --------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    return build_e2e_db(tmp_path_factory.mktemp("prov") / "e2e.duckdb")


@pytest.fixture
def client_for(e2e, tmp_path, monkeypatch):
    def make(mutate=None):
        path = tmp_path / "copy.duckdb"
        shutil.copy(e2e, path)
        if mutate:
            con = duckdb.connect(str(path))
            try:
                mutate(con)
            finally:
                con.close()
        monkeypatch.setenv("FANTASY_DB_PATH", str(path))
        monkeypatch.setenv("ESPN_SEASON", E2E_SEASON)
        for name in ("FANTASY_API_KEY", "MULTILEAGUE_ENABLED"):
            monkeypatch.delenv(name, raising=False)
        from app.main import app
        return TestClient(app, raise_server_exceptions=False), path
    return make


HEADLINE = ["/api/overview", "/api/matchup", "/api/start-sit", "/api/waivers", "/api/trades/options",
            "/api/trades/discover/context", "/api/players/101"]


def test_every_headline_endpoint_carries_the_contract_without_ids_or_paths(client_for):
    client, path = client_for()
    for url in HEADLINE:
        r = client.get(url)
        assert r.status_code == 200, (url, r.text[:200])
        prov = r.json()["provenance"]
        assert prov["contract"] == sp.CONTRACT and prov["signals"], url
        assert prov["signals"][0]["signal"] == "league", url
        for s in prov["signals"]:
            assert set(s) == KEYS, url
            assert s["source"] in ("espn", "sleeper", "nflverse", "fantasy_intel"), url
            assert s["snapshot_version"] and s["snapshot_version"].startswith("local:"), url
        text = json.dumps(prov)
        assert str(path) not in text and str(path.parent) not in text, url
        assert '"999"' not in text and ": 999" not in text, url          # the fixture's provider league id


def test_a_failed_espn_sync_after_the_last_success_is_flagged_on_every_page(client_for):
    def fail(con):
        con.execute("INSERT INTO sync_runs (run_id, source, started_at, finished_at, status, message, stats) "
                    "VALUES ('espn-fail', 'espn', now()::TIMESTAMP + INTERVAL 1 HOUR, now()::TIMESTAMP + INTERVAL 1 HOUR, "
                    "'error', 'boom', '{}')")
    client, _ = client_for(fail)
    for url in ("/api/overview", "/api/start-sit", "/api/waivers"):
        league = client.get(url).json()["provenance"]["signals"][0]
        assert league["stale_reason"] and sp.status(league) == "stale", url


def test_a_missing_starter_projection_is_partial_not_zero(client_for):
    client, _ = client_for()
    d = client.get("/api/start-sit").json()
    starter = d["actual_lineup"][0]["player_id"]

    def drop(con):
        con.execute("UPDATE espn_player_weekly_points SET projected_points = NULL WHERE espn_player_id = ?", [starter])
    client, _ = client_for(drop)
    d = client.get("/api/start-sit").json()
    proj = next(s for s in d["provenance"]["signals"] if s["signal"] == "projections")
    assert proj["coverage"]["available"] == proj["coverage"]["total"] - 1
    assert sp.status(proj) in ("partial", "stale", "unknown")      # never "current" with a hole
    assert proj["reason_unavailable"]
    assert "(0.0 pts)" not in json.dumps(d["decisions"])          # explanations say "no projection", not 0.0


def test_overview_usage_signal_covers_the_rosters_market_signals(client_for):
    """The Overview's trade decisions restate the roster's market signals: the usage signal says how many of the
    rostered skill players have one; an unclassified player counts as missing, never as a signal."""
    client, _ = client_for()
    d = client.get("/api/overview").json()
    usage = next(s for s in d["provenance"]["signals"] if s["signal"] == "usage")
    assert usage["source"] == "nflverse"
    skill = [p for p in d["roster"] if p["position"] in ("QB", "RB", "WR", "TE")]
    assert skill, "the fixture roster has skill players"
    assert usage["coverage"] == {"available": sum(1 for p in skill if p["market_signal"]), "total": len(skill),
                                 "label": "rostered QB, RB, WR and TE with a market signal"}
    if usage["coverage"]["available"] == 0:
        assert sp.status(usage) == "unavailable"
