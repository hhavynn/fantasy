"""Projection-source comparison (analytics/projection_compare.py, GET /api/rankings/compare).

Reuses the synthetic league of tests/test_rankings.py (every number an invented test input; temp DuckDB only). Week 7:

    provider (ESPN) overall: Quinn QB 22 (#1), Alpha WR 20 (#2), Kilo RB 18 (#3), Bravo / Charlie WR 15 (T-#4), Golf WR
                             12 (#6), Tango TE 11 (#7), Yankee / Zulu RB 9 (T-#8), Foxtrot WR 8 (#10), Rex QB 7 (#11),
                             Uniform TE 6 (#12); Hotel WR on a bye; India WR has no week-7 row.
    Ours (expected points):  Alpha 19 (#1), Bravo 17 (#2), Charlie 14 (#3), Kilo 12 (#4), Tango 11 (#5), Quinn 5 (#6);
                             Hotel on the bye; every other player has no Ours row.
"""

import json
import shutil

import pytest
import test_rankings as tr
from fastapi.testclient import TestClient

from app.analytics import production_forecasts as pf
from app.analytics import projection_compare as pc
from app.analytics import projection_ranges as pr
from app.analytics import projection_source as ps
from app.analytics.rankings import RankingsUnavailable
from app.db.connection import connect, fetch_dicts, init_db

QUINN, ALPHA, BRAVO, CHARLIE, GOLF, HOTEL, INDIA, KILO = 301, 101, 102, 103, 107, 108, 109, 201


@pytest.fixture(autouse=True)
def fingerprint(monkeypatch):
    monkeypatch.setattr(ps, "_scoring", lambda con, season: ("fp", None))


@pytest.fixture(scope="module")
def template_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("compare_template") / "league.duckdb"
    c = connect(path)
    init_db(c)
    tr.build(c)
    c.execute("CHECKPOINT")
    c.close()
    return path


@pytest.fixture
def league(template_db, tmp_path):
    path = tmp_path / "league.duckdb"
    shutil.copyfile(template_db, path)
    c = connect(path)
    yield c
    c.close()


def get(con, **kw):
    kw.setdefault("limit", 300)
    return pc.build_compare(con, **kw)


def rows(body):
    return {p["espn_player_id"]: p for p in body["players"]}


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v)


def make_sleeper(con):
    con.execute("""UPDATE leagues SET settings_raw = '{"provider": "sleeper"}'""")


def reseal(con, run_id):
    run = fetch_dicts(con, "SELECT * FROM independent_projection_runs WHERE run_id = ?", [run_id])[0]
    stored = fetch_dicts(con, "SELECT * FROM independent_projection_rows WHERE run_id = ?", [run_id])
    con.execute("UPDATE independent_projection_runs SET content_sha256 = ? WHERE run_id = ?",
                [pf.content_sha256(run, stored), run_id])


# ============================================================================= ranks and deltas


def test_each_source_is_ranked_over_the_whole_pool_and_the_delta_is_provider_minus_ours(league):
    body = get(league, scope="overall")
    r = rows(body)
    q = r[QUINN]
    assert q["sources"]["provider"]["rank"]["overall"] == 1 and q["sources"]["ours"]["rank"]["overall"] == 6
    assert q["delta"] == {"status": "ok", "rank": -5, "points": -17.0, "points_per_week": -17.0,
                          "direction": "provider_higher", "tied": False, "relevant": True, "large": False}
    # cross-position: the same player has no disagreement within his own position (QB1 for both)
    q_pos = rows(get(league, scope="position"))[QUINN]
    assert q_pos["sources"]["provider"]["rank"]["position_label"] == "QB1"
    assert q_pos["delta"]["rank"] == 0 and q_pos["delta"]["direction"] == "same"     # a measured zero, both ranked
    assert body["counts"]["pool"] == tr.POOL and body["counts"]["compared"] == 6


def test_rank_ties_share_a_rank_and_are_flagged(league):
    r = rows(get(league))
    for pid in (BRAVO, CHARLIE):
        rank = r[pid]["sources"]["provider"]["rank"]
        assert rank["position"] == 2 and rank["position_tied"] and rank["overall"] == 4 and rank["overall_tied"]
        assert r[pid]["delta"]["tied"]
    assert r[BRAVO]["delta"]["rank"] == 0                  # T-WR2 for the provider, WR2 for Ours
    assert r[CHARLIE]["delta"]["rank"] == -1               # T-WR2 for the provider, WR3 for Ours
    assert not r[ALPHA]["delta"]["tied"]


def test_missing_sides_are_named_and_never_zero(league):
    r = rows(get(league))
    golf = r[GOLF]                                         # provider WR4, no Ours row
    assert golf["sources"]["ours"]["status"] == "no_estimate" and golf["sources"]["ours"]["value"] is None
    assert golf["delta"]["status"] == "ours_missing" and golf["delta"]["rank"] is None
    assert golf["delta"]["points"] is None
    hotel = r[HOTEL]                                       # bye: neither side ranks a bye
    assert hotel["sources"]["provider"]["status"] == "bye" and hotel["sources"]["ours"]["status"] == "bye"
    assert hotel["delta"]["status"] == "neither_ranked"
    india = r[INDIA]
    assert india["sources"]["provider"]["status"] == "missing"
    assert "No ESPN projection for week 7" == india["sources"]["provider"]["reason"]


def test_an_absent_provider_projection_is_unranked_and_does_not_shift_into_zero(league):
    league.execute("DELETE FROM espn_player_weekly_points WHERE espn_player_id = ? AND scoring_period = 7", [BRAVO])
    r = rows(get(league))
    bravo = r[BRAVO]
    assert bravo["sources"]["provider"]["value"] is None and bravo["sources"]["provider"]["rank"]["position"] is None
    assert bravo["sources"]["ours"]["rank"]["position"] == 2
    assert bravo["delta"]["status"] == "provider_missing" and bravo["delta"]["rank"] is None
    # Charlie is now WR2 alone for the provider
    assert r[CHARLIE]["sources"]["provider"]["rank"]["position"] == 2
    assert not r[CHARLIE]["sources"]["provider"]["rank"]["position_tied"]


def test_large_needs_rank_points_and_relevance(league, monkeypatch):
    monkeypatch.setitem(pc.LARGE_RANK, "overall", 5)
    body = get(league, scope="overall")
    r = rows(body)
    assert r[QUINN]["delta"]["large"]
    assert [x["espn_player_id"] for x in body["largest"]] == [QUINN]
    assert body["counts"]["large"] == 1 and body["counts"]["provider_higher"] == 1
    assert body["thresholds"]["large_rank"] == 5
    # a rank gap with a tiny points gap is not large
    monkeypatch.setattr(pc, "MIN_POINTS_PER_WEEK", 50.0)
    assert not rows(get(league, scope="overall"))[QUINN]["delta"]["large"]


# ============================================================================= sources and providers


def test_sources_block_names_only_sources_with_values(league):
    body = get(league)
    provider, ours = body["sources"]
    assert provider["key"] == "provider" and provider["label"] == "ESPN projections" and provider["eligible"]
    assert ours["label"] == f"Fantasy Intel projections ({ps.MODEL_VERSION})" and ours["available"]
    assert "consensus" in body["notes"][0]


def test_ours_unavailable_is_named_unavailable_with_the_engine_reason(league):
    league.execute("DELETE FROM independent_projection_rows")
    league.execute("DELETE FROM independent_projection_runs")
    body = get(league)
    ours = body["sources"][1]
    assert ours["label"] == "Unavailable" and not ours["available"] and ours["reason"]
    for p in body["players"]:
        assert p["sources"]["ours"]["value"] is None and p["sources"]["ours"]["rank"]["overall"] is None
        assert p["delta"]["status"] == "ours_unavailable" and p["delta"]["rank"] is None


def test_sleeper_league_has_no_provider_ranks_and_never_says_espn(league):
    make_sleeper(league)          # the ESPN rows stay in the file: the gate is the provider, not the data
    body = get(league)
    provider = body["sources"][0]
    assert provider["label"] == "Unavailable" and not provider["eligible"] and provider["ranked"] == 0
    assert provider["provider_name"] == "Sleeper" and "never 0" in provider["reason"]
    assert body["provider"] == {"key": "sleeper", "name": "Sleeper", "publishes_projections": False}
    for p in body["players"]:
        side = p["sources"]["provider"]
        assert side["value"] is None and side["rank"]["overall"] is None and side["status"] == "not_published"
        assert p["delta"]["status"] == "provider_not_published" and p["delta"]["rank"] is None
    assert body["counts"]["provider_missing"] == tr.POOL
    text = [s for s in strings({k: body[k] for k in ("sources", "notes", "provider", "ranges")}) if "ESPN" in s]
    text += [s for p in body["players"] for s in strings({"sources": p["sources"], "delta": p["delta"]}) if "ESPN" in s]
    assert text == []
    assert all("Sleeper projection" not in s for s in strings(body))


def test_sleeper_large_delta_explanations_say_the_provider_has_no_history(league, monkeypatch):
    make_sleeper(league)
    monkeypatch.setattr(pc, "LARGE_RANK", {"position": -1, "overall": -1})
    monkeypatch.setattr(pc, "MIN_POINTS_PER_WEEK", -1.0)
    monkeypatch.setattr(pc, "RELEVANT_PER_WEEK", -1.0)
    body = get(league)
    # every row is "ok" only when both are ranked, so nothing is large without a provider
    assert all(p["explanation"] is None for p in body["players"])


# ============================================================================= explanations


def test_large_deltas_carry_the_models_stored_drivers_and_history(league, monkeypatch):
    run_id = f"run-7-{tr.APPROVED}"
    explanation = [{"feature": "targets_l4", "value": 2.0, "training_mean": 6.0, "contribution": -3.2,
                    "text": "Targets per game 2.0 (training mean 6.0)"},
                   {"feature": "snap_share", "value": 0.4, "training_mean": 0.6, "contribution": -0.4, "text": "Snaps"},
                   {"feature": "headline", "value": None, "training_mean": None, "contribution": None, "text": "Note"},
                   {"feature": "opp", "value": 1.0, "training_mean": 1.0, "contribution": 1.1, "text": "Opponent"}]
    league.execute("""UPDATE independent_projection_rows SET explanation = ?, flags = ?, games_used = 6, p_play = 0.31,
                      latest_usage_season = 2026, latest_usage_week = 6 WHERE run_id = ? AND espn_player_id = ?""",
                   [json.dumps(explanation), json.dumps(["thin sample"]), run_id, QUINN])
    reseal(league, run_id)
    monkeypatch.setitem(pc.LARGE_RANK, "overall", 5)
    q = rows(get(league, scope="overall"))[QUINN]
    model = q["explanation"]["model"]
    assert model["available"] and model["week"] == 7 and model["inputs_recorded"] == 4
    assert [e["feature"] for e in model["inputs"]] == ["targets_l4", "opp", "snap_share"]       # by |contribution|
    assert model["flags"] == ["thin sample"] and model["p_play"] == 0.31 and model["games_used"] == 6
    assert model["run"]["run_id"] == run_id and model["run"]["model_version"] == ps.MODEL_VERSION
    assert "ESPN publishes no reasoning" in model["note"]
    hist = q["explanation"]["history"]
    assert set(hist) == {"ours", "provider"}
    for h in hist.values():               # one stored forecast per source in the fixture: no movement, with the reason
        assert h["available"] is False and h["reason"] and h["player"] is None
    # rows that are not large are not explained
    assert rows(get(league, scope="overall"))[ALPHA]["explanation"] is None


def test_mover_history_lists_the_players_move_when_two_forecasts_exist(league, monkeypatch):
    move = {"available": True, "source": "independent", "summary": {"compared": 1},
            "baseline": {"label": "Oct 7 (before week 6)", "taken_at": "2026-10-07T12:00:00+00:00", "run_ids": None},
            "current": {"label": "Oct 14 (before week 7)", "taken_at": "2026-10-14T12:00:00+00:00", "run_ids": None},
            "window": {"weeks": [7]},
            "players": [{"espn_player_id": QUINN, "before": 9.0, "after": 5.0, "delta": -4.0, "rank_before": 12,
                         "rank_after": 20, "direction": "down", "position": "QB", "nfl_team": "KC", "weeks": [],
                         "components": None}]}
    monkeypatch.setattr(pc.pm, "movement", lambda con, season, **kw: dict(move, source=kw["source"]))
    monkeypatch.setattr(pc.mover_evidence, "explain", lambda con, season, movers, **kw: {
        QUINN: {"headline": "Snap share fell", "signals": [{"kind": "usage", "label": "Snap share", "before": 0.8,
                                                            "after": 0.5, "unit": "share", "source": "nflverse snap counts",
                                                            "detail": "d", "key": "snap_share"}],
                "missing": [], "note": "Observed over the same period as the change. Not shown to be the cause."}})
    monkeypatch.setitem(pc.LARGE_RANK, "overall", 5)
    q = rows(get(league, scope="overall"))[QUINN]
    ours = q["explanation"]["history"]["ours"]
    assert ours["available"] and ours["baseline"] == "Oct 7 (before week 6)" and ours["weeks"] == [7]
    assert ours["player"]["listed"] and ours["player"]["delta"] == -4.0 and ours["player"]["rank_after"] == 20
    assert ours["player"]["evidence"]["headline"] == "Snap share fell"
    assert ours["player"]["evidence"]["signals"][0]["source"] == "nflverse snap counts"
    assert "Not shown to be the cause" in ours["player"]["evidence"]["note"]


# ============================================================================= ranges


def fake_ranges(week):
    tables = {"pin_id": 1, "content_hash": "h", "trained_through_key": 202606,
              "positions": {p: {"status": "validated"} for p in ("QB", "RB", "WR", "TE")}}
    return {**pr.describe(tables, None), "season": 2026, "week": week,
            "run": {"run_id": "r", "model_version": ps.MODEL_VERSION, "forecast_horizon": 0},
            "ranges": {ALPHA: {"position": "WR", "projection": 20.0, "floor": 8.1, "median": 18.4, "ceiling": 33.0}}}


def test_ranges_come_from_the_distribution_engine_with_horizon_basis_and_coverage(league, monkeypatch):
    monkeypatch.setattr(pc.pr, "for_week", lambda con, season, week: fake_ranges(week))
    for horizon in ("week", "ros"):
        body = get(league, horizon=horizon)
        r = rows(body)
        a = r[ALPHA]["range"]
        assert a["available"] and (a["floor"], a["median"], a["ceiling"]) == (8.1, 18.4, 33.0)
        assert a["week"] == 7 and "forecast horizon 0" in a["horizon"]
        assert "ifpd-0.1.0" in a["basis"] and "if he plays" in a["basis"]
        assert a["coverage"]["nominal"] == 0.8 and a["coverage"]["confirmation"]["coverage"] == 0.813
        assert "5,409 WR rows" in a["coverage"]["text"]
        assert not r[QUINN]["range"]["available"] and "QB" in r[QUINN]["range"]["reason"]      # QB is held back
        assert not r[BRAVO]["range"]["available"]                  # WR, but the engine served no range for him
        assert body["ranges"]["rest_of_season"]["available"] is False
        assert body["ranges"]["validation_source"].startswith("docs/research/fantasy_point_distributions.md")


def test_no_range_without_a_loaded_distribution(league):
    body = get(league)
    assert body["ranges"]["available"] is False and body["ranges"]["reason"]
    assert all(not p["range"]["available"] and p["range"]["reason"] for p in body["players"])


# ============================================================================= filters, sorting, validation


def test_filters_apply_after_ranking(league):
    body = get(league, position="WR")
    assert {p["position"] for p in body["players"]} == {"WR"}
    assert rows(body)[CHARLIE]["sources"]["provider"]["rank"]["overall"] == 4      # never renumbered by the filter
    assert [p["espn_player_id"] for p in get(league, q="alpha")["players"]] == [ALPHA]
    assert {p["roster"]["status"] for p in get(league, status="available")["players"]} <= {"FREE_AGENT", "WAIVERS"}
    shown = get(league, show="missing")
    assert shown["players"] and all(p["delta"]["status"] != "ok" for p in shown["players"])
    assert all(p["delta"]["status"] == "ok" for p in get(league, show="compared")["players"])
    assert get(league, show="large")["players"] == []


def test_sorting_puts_missing_deltas_last_in_both_directions(league):
    for d in ("asc", "desc"):
        body = get(league, sort="delta", dir=d, scope="overall")
        statuses = [p["delta"]["status"] for p in body["players"]]
        first_missing = statuses.index(next(s for s in statuses if s != "ok"))
        assert all(s != "ok" for s in statuses[first_missing:])
        deltas = [p["delta"]["rank"] for p in body["players"][:first_missing]]
        assert deltas == sorted(deltas, reverse=d == "desc")
    top = get(league, scope="overall")["players"][0]
    assert top["espn_player_id"] == QUINN                         # largest |delta| first by default
    assert get(league, sort="ours_rank")["sort"] == {"key": "ours_rank", "dir": "asc"}
    page = get(league, limit=2, offset=1)
    assert page["counts"]["returned"] == 2 and page["counts"]["shown"] == tr.POOL


@pytest.mark.parametrize("kw, word", [({"scope": "team"}, "scope"), ({"sort": "espn"}, "sort"), ({"show": "x"}, "show"),
                                      ({"position": "K"}, "position"), ({"horizon": "year"}, "horizon"),
                                      ({"dir": "up"}, "dir"), ({"limit": 0}, "limit"), ({"offset": -1}, "offset"),
                                      ({"status": "mine"}, "status")])
def test_invalid_parameters_name_themselves(league, kw, word):
    with pytest.raises(ValueError, match=word):
        get(league, **kw)


def test_unsynced_league_is_unavailable(tmp_path):
    c = connect(tmp_path / "empty.duckdb")
    init_db(c)
    try:
        with pytest.raises(RankingsUnavailable):
            pc.build_compare(c)
    finally:
        c.close()


def test_api_route(template_db, tmp_path, monkeypatch):
    path = tmp_path / "api.duckdb"
    shutil.copyfile(template_db, path)
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    for name in ("FANTASY_API_KEY", "MULTILEAGUE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    from app.main import app
    client = TestClient(app)
    r = client.get("/api/rankings/compare?scope=overall&position=WR&show=compared")
    assert r.status_code == 200
    body = r.json()
    assert body["contract"] == pc.CONTRACT and body["scope"] == "overall" and body["show"] == "compared"
    assert client.get("/api/rankings/compare?scope=team").status_code == 422
