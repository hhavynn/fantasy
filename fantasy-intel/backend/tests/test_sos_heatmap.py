"""Strength-of-schedule heatmap (issue #188): per-position grids built from schedule intelligence's own evidence, the
neutral baseline and differences, the ROS / fantasy-playoff horizons from league settings (and what happens when they
are unknown), no grade for an under-sampled defense or a thin ranking pool, no opponent without a schedule or a team,
the roster / free-agent / NFL-team / player filters, and a read-only API. Synthetic inputs only (tests/synthetic_nfl.py)."""

import pytest
from fastapi.testclient import TestClient

from app.analytics.schedule_intelligence import MIN_RANKED_DEFENSES, build_schedule_intelligence
from app.analytics.sos_heatmap import (MAX_FREE_AGENTS, BadParam, build_sos_heatmap, horizon_weeks, summarize)

import synthetic_nfl as nfl
from test_schedule_intelligence import NOW, WEEK, _bye_pair
from test_schedule_intelligence import setup as si_setup     # aliased: a module-level `setup` is a pytest hook name


def heat(con, **kw):
    return build_sos_heatmap(con, now=NOW, **kw)


def row(res: dict, team: str) -> dict:
    return next(r for r in res["rows"] if r["nfl_team"] == team)


def cell(r: dict, week: int) -> dict:
    return next(c for c in r["cells"] if c["week"] == week)


def _games_before(week: int) -> dict[str, int]:
    """Presumed-complete games per defense before `week` in si_setup: weeks 1-7 final, half of week 8 final."""
    final8 = {h for h, _ in nfl.schedule(18)[WEEK][:8]}
    done8 = {t for h, a in nfl.schedule(18, _bye_pair())[WEEK] if h in final8 for t in (h, a)}
    return {t: (WEEK - 1) + (1 if week > WEEK and t in done8 else 0) for t in nfl.TEAMS}


# --------------------------------------------------------------------- grids --

def test_each_position_has_its_own_grid_from_schedule_intelligence_evidence(tmp_path):
    _, con = si_setup(tmp_path)
    qb, rb = heat(con, position="QB"), heat(con, position="RB")
    assert qb["contract"] == "sos-heatmap.v1" and qb["position"] == "QB" and rb["position"] == "RB"
    assert qb["metric"]["key"] == "pass_epa_per_play" and rb["metric"]["key"] == "rush_epa_per_play"
    assert [r["nfl_team"] for r in qb["rows"]] == sorted(nfl.TEAMS) and all(r["kind"] == "team" for r in qb["rows"])
    assert qb["horizon"]["weeks"] == list(range(WEEK + 1, 15))            # first actionable week through week 14
    # Every cell is schedule intelligence's own answer for that opponent and week: no second opponent model.
    si = build_schedule_intelligence(con, player_id=102, now=NOW)["players"][0]          # Bravo QB, BUF
    checked = 0
    for w in si["weeks"]:
        if w["week"] not in qb["horizon"]["weeks"]:
            continue
        c = cell(row(qb, "BUF"), w["week"])
        assert c["status"] == w["status"] and c["label"] == w["label"]
        if w["status"] == "bye":
            assert "opponent" not in c and "rank" not in c
            continue
        p = next(m for m in w["evidence"]["metrics"] if m["metric"] == "pass_epa_per_play")
        assert (c["opponent"], c["home"], c["rank"], c["value"], c["plays"], c["games"]) == \
            (w["opponent"], w["home"], p["rank"], p["value"], p["denominator"], w["evidence"]["games"])
        checked += 1
    assert checked == len(qb["horizon"]["weeks"]) - 1                       # one bye (week 10)
    # Comparable only within a position: the synthetic pass and rush orders are reversed, so the same opponent ranks
    # at opposite ends of the QB and RB grids.
    a, b = cell(row(qb, "BUF"), 9), cell(row(rb, "BUF"), 9)
    assert a["opponent"] == b["opponent"] and a["rank"] + b["rank"] == len(nfl.TEAMS) + 1
    assert "same pass-defense metric" in qb["notes"][0] and "different metric" in rb["notes"][0]
    con.close()


def test_qb_wr_and_te_share_one_pass_defense_ranking_and_say_so(tmp_path):
    """QB, WR and TE are graded on the same metric (schedule_intelligence.PRIMARY_METRIC), so their grids must be
    identical in everything graded, and the copy must say so rather than claim a different metric per position."""
    _, con = si_setup(tmp_path)
    grids = {p: heat(con, position=p) for p in ("QB", "WR", "TE", "RB")}
    graded = ("opponent", "label", "rank", "ranked_defenses", "value", "baseline", "delta", "games", "plays")
    strip = lambda g: [[{k: c.get(k) for k in graded} for c in r["cells"]] for r in g["rows"]]   # noqa: E731
    assert strip(grids["QB"]) == strip(grids["WR"]) == strip(grids["TE"])
    assert sum(1 for r in grids["QB"]["rows"] for c in r["cells"] if c.get("rank") is not None) > 100
    assert strip(grids["RB"]) != strip(grids["QB"])
    for p in ("QB", "WR", "TE"):
        g = grids[p]
        assert g["metric"]["key"] == "pass_epa_per_play" and g["metric"]["positions"] == ["QB", "WR", "TE"]
        note = g["notes"][0]
        assert "The QB, WR and TE grids all rank opponents on the same pass-defense metric" in note
        assert "EPA allowed per eligible pass play" in note and "rank opponents identically" in note
        assert "Other positions use other metrics" not in note and "Comparable only within" not in note
    assert grids["RB"]["metric"]["positions"] == ["RB"] and "different metric from the QB, WR and TE grids" in grids["RB"]["notes"][0]
    con.close()


def test_wr_and_te_cells_carry_target_share_as_context_only(tmp_path):
    _, con = si_setup(tmp_path)
    qb, wr, te = heat(con, position="QB"), heat(con, position="WR"), heat(con, position="TE")
    assert qb["context_metric"] is None and all("context" not in c for r in qb["rows"] for c in r["cells"])
    assert wr["context_metric"]["key"] == "wr_target_share" and te["context_metric"]["key"] == "te_target_share"
    c = next(c for c in row(wr, "BUF")["cells"] if c["status"] == "scheduled")
    games = c["games"]
    i = nfl.TEAMS.index(c["opponent"])
    assert c["context"]["denominator"] == 30 * games and c["context"]["numerator"] == (15 + i % 4) * games
    assert c["context"]["value"] == pytest.approx((15 + i % 4) / 30)
    assert "context only" in wr["notes"][0] and "does not change the grade" in wr["notes"][0]
    assert (c["label"], c["rank"]) == (cell(row(qb, "BUF"), c["week"])["label"], cell(row(qb, "BUF"), c["week"])["rank"])
    con.close()


def test_neutral_baseline_is_the_pooled_value_and_each_cell_shows_its_difference(tmp_path):
    _, con = si_setup(tmp_path)
    res = heat(con)
    for b in res["baselines"]:
        games = _games_before(b["week"])
        expected = sum(games[t] * 40 * nfl.pass_rate(t) for t in nfl.TEAMS) / sum(games[t] * 40 for t in nfl.TEAMS)
        assert b["value"] == pytest.approx(expected) and b["ranked_defenses"] == len(nfl.TEAMS) and b["reason"] is None
    signs = set()
    for r in res["rows"]:
        for c in r["cells"]:
            if c["status"] != "scheduled":
                continue
            assert c["baseline"] == next(b["value"] for b in res["baselines"] if b["week"] == c["week"])
            assert c["delta"] == pytest.approx(c["value"] - c["baseline"])
            if c["label"] == "permissive":
                assert c["delta"] > 0
                signs.add("+")
            if c["label"] == "tougher":
                assert c["delta"] < 0
                signs.add("-")
    assert signs == {"+", "-"}
    con.close()


def test_row_tallies_count_weeks_and_average_only_graded_ranks():
    cells = [{"label": "permissive", "status": "scheduled", "rank": 2, "delta": 0.1},
             {"label": "tougher", "status": "scheduled", "rank": 30, "delta": -0.1},
             {"label": "insufficient", "status": "scheduled", "rank": None, "delta": None},
             {"label": "bye", "status": "bye"},
             {"label": "unavailable", "status": "schedule_missing"}]
    s = summarize(cells)
    assert s["weeks"] == 5 and s["games"] == 3 and s["graded_weeks"] == 2 and s["average_rank"] == 16.0
    assert s["counts"]["permissive"] == 1 and s["counts"]["insufficient"] == 1 and s["counts"]["bye"] == 1
    assert summarize([{"label": "insufficient", "status": "scheduled", "rank": None}])["average_rank"] is None


# ------------------------------------------------------------------ horizons --

def test_horizons_come_from_league_settings_and_unknown_settings_are_said(tmp_path):
    _, con = si_setup(tmp_path)
    po = heat(con, horizon="playoffs", position="WR")
    assert po["horizon"]["weeks"] == [15, 16, 17] and po["horizon"]["known"] and po["horizon"]["source"] == "league settings"
    assert {c["phase"] for r in po["rows"] for c in r["cells"]} == {"playoffs"}
    assert all(c["week"] in (15, 16, 17) for r in po["rows"] for c in r["cells"])
    con.execute("UPDATE leagues SET reg_season_matchups = NULL")
    ros = heat(con)
    assert ros["horizon"]["known"] is False and ros["horizon"]["weeks"] == list(range(WEEK + 1, 18))
    assert "do not establish where the regular season ends" in ros["horizon"]["reason"]
    po = heat(con, horizon="playoffs")
    assert po["horizon"]["known"] is False and po["horizon"]["weeks"] == [] and po["rows"]
    assert all(r["cells"] == [] for r in po["rows"]) and "fantasy-playoff weeks" in po["horizon"]["reason"]
    con.close()


def test_horizon_weeks_edges():
    assert horizon_weeks("ros", 16, 17, 14)["weeks"] == [] and "ended with week 14" in horizon_weeks("ros", 16, 17, 14)["reason"]
    assert horizon_weeks("playoffs", 16, 17, 14)["weeks"] == [16, 17]
    assert horizon_weeks("playoffs", None, 17, 14)["weeks"] == [] and "No remaining" in horizon_weeks("playoffs", None, 17, 14)["reason"]
    none = horizon_weeks("playoffs", 3, 17, 17)
    assert none["weeks"] == [] and none["playoff_weeks"] == [] and "no fantasy-playoff weeks" in none["reason"]


# ------------------------------------------------------------- no false grades --

def test_under_sampled_defense_gets_no_grade_rank_or_difference(tmp_path):
    _, con = si_setup(tmp_path)
    # KC, the most permissive pass defense, keeps only two presumed-complete games.
    con.execute("DELETE FROM nfl_defense_week WHERE season = 2026 AND defteam = 'KC' AND week > 2")
    res = heat(con)
    vs_kc = [c for r in res["rows"] for c in r["cells"] if c.get("opponent") == "KC"]
    assert vs_kc
    for c in vs_kc:
        assert c["label"] == "insufficient" and c["rank"] is None and c["delta"] is None
        assert c["games"] == 2 and "only 2 presumed-complete game" in c["reason"]
    graded = [c for r in res["rows"] for c in r["cells"] if c.get("rank") is not None]
    assert graded and all(c["opponent"] != "KC" for c in graded) and min(c["rank"] for c in graded) == 1
    assert res["coverage"]["graded"] == len(graded) < res["coverage"]["scheduled"]
    con.close()


def test_too_few_rankable_defenses_grade_nobody_and_have_no_baseline(tmp_path):
    _, con = si_setup(tmp_path)
    gone = nfl.TEAMS[: len(nfl.TEAMS) - MIN_RANKED_DEFENSES + 1]          # leaves 20 rankable defenses
    con.execute(f"DELETE FROM nfl_defense_week WHERE season = 2026 AND defteam IN ({', '.join('?' * len(gone))})", gone)
    res = heat(con)
    assert all(b["value"] is None and "(< 21)" in b["reason"] for b in res["baselines"])
    scheduled = [c for r in res["rows"] for c in r["cells"] if c["status"] == "scheduled"]
    assert scheduled and all(c["label"] == "insufficient" and c["rank"] is None and c["delta"] is None
                             for c in scheduled)
    sampled = [c for c in scheduled if c["opponent"] not in gone]
    assert sampled and all("defenses can be ranked" in c["reason"] for c in sampled)
    assert all(r["summary"]["average_rank"] is None for r in res["rows"])
    con.close()


def test_no_defensive_data_names_no_source(tmp_path):
    _, con = si_setup(tmp_path, defense=False)
    res = heat(con)
    assert res["source"]["available"] is False and res["source"]["name"] is None and res["source"]["reason"]
    assert all(c.get("rank") is None and c.get("value") is None for r in res["rows"] for c in r["cells"])
    con.close()


def test_missing_schedule_week_and_unknown_team_never_invent_an_opponent(tmp_path):
    _, con = si_setup(tmp_path)
    con.execute("DELETE FROM nfl_schedules WHERE season = 2026 AND week = 12")
    con.execute("UPDATE fantasy_players SET pro_team = NULL WHERE espn_player_id = 209")
    res = heat(con)
    for r in res["rows"]:
        c = cell(r, 12)
        assert c["status"] == "schedule_missing" and "opponent" not in c and "week 12" in c["reason"]
    assert res["source"]["weeks_without_schedule"] == [12]
    fa = heat(con, position="RB", view="free_agents")
    r = next(x for x in fa["rows"] if x["player"]["espn_player_id"] == 209)
    assert r["nfl_team"] is None and all(c["status"] == "team_unknown" and "opponent" not in c for c in r["cells"])
    assert any("No current NFL team" in n for n in r["notes"])
    con.close()


# ------------------------------------------------------------------- filters --

def test_roster_free_agent_team_and_player_filters(tmp_path):
    _, con = si_setup(tmp_path)
    teams = heat(con, position="RB")
    roster = heat(con, position="RB", view="roster")                    # default: my team (2)
    assert roster["query"]["team_id"] == 2 and roster["query"]["view"] == "roster"
    assert [r["player"]["espn_player_id"] for r in roster["rows"]] == [204, 205, 206, 207]   # by name
    assert all(r["player"]["fantasy_team_id"] == 2 for r in roster["rows"])
    assert [r["player"]["on_ir"] for r in roster["rows"]] == [False, False, False, True]
    for r in roster["rows"]:
        assert r["cells"] == row(teams, r["nfl_team"])["cells"]           # the player's NFL team's row
    other = heat(con, position="QB", view="roster", team_id=1)
    assert [r["player"]["name"] for r in other["rows"]] == ["Alpha QB"] and other["team"]["team_id"] == 1

    fa = heat(con, position="RB", view="free_agents")
    assert [r["player"]["espn_player_id"] for r in fa["rows"]] == [209]
    assert fa["rows"][0]["player"]["fantasy_team_id"] is None and fa["rows"][0]["player"]["availability_status"] == "FREEAGENT"
    assert fa["free_agent_limit"] == MAX_FREE_AGENTS and fa["rows_total"] == 1

    one = heat(con, nfl_team="kc", position="TE")
    assert [r["nfl_team"] for r in one["rows"]] == ["KC"] and one["query"]["nfl_team"] == "KC"
    assert heat(con, position="RB", view="roster", nfl_team="SF")["rows"][0]["player"]["espn_player_id"] == 205

    p = heat(con, player_id=102, position="RB")                          # a QB is graded on the QB metric only
    assert p["position"] == "QB" and p["query"]["view"] == "player" and p["metric"]["key"] == "pass_epa_per_play"
    assert [r["player"]["espn_player_id"] for r in p["rows"]] == [102] and p["rows"][0]["player"]["fantasy_team_id"] == 2
    con.close()


def test_free_agents_with_a_team_first_then_ownership_then_points_and_capped(tmp_path, monkeypatch):
    _, con = si_setup(tmp_path)
    # A provider without ownership shares (Sleeper) still gets a meaningful order; a player with no NFL team comes last.
    for pid, name, team, owned, pts in ((901, "Aaa RB", None, 90.0, 100.0), (902, "Bbb RB", "KC", None, 50.0),
                                        (903, "Ccc RB", "BUF", None, 80.0), (904, "Ddd RB", "SF", 10.0, None)):
        con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team, availability_status, "
                    "on_team_id, percent_owned, season_points) VALUES (?, ?, 'RB', ?, 'FREEAGENT', 0, ?, ?)",
                    [pid, name, team, owned, pts])
    fa = heat(con, position="RB", view="free_agents")
    assert [r["player"]["espn_player_id"] for r in fa["rows"]] == [209, 904, 903, 902, 901]
    assert fa["rows_total"] == fa["rows_shown"] == 5 and "ownership share" in fa["free_agent_order"]
    assert fa["free_agent_order_keys"] == ["nfl_team", "percent_owned", "season_points", "name"]
    monkeypatch.setattr("app.analytics.sos_heatmap.MAX_FREE_AGENTS", 2)
    capped = heat(con, position="RB", view="free_agents")
    assert [r["player"]["espn_player_id"] for r in capped["rows"]] == [209, 904]
    assert capped["rows_total"] == 5 and capped["rows_shown"] == 2
    assert heat(con, position="RB")["free_agent_order"] is None
    con.close()


def test_sleeper_shaped_free_agents_never_claim_an_ownership_order(tmp_path):
    """The Sleeper sync writes percent_owned NULL for every player: the order must not name ownership."""
    _, con = si_setup(tmp_path)
    con.execute("UPDATE fantasy_players SET percent_owned = NULL")
    con.execute("UPDATE fantasy_players SET season_points = NULL WHERE espn_player_id = 209")
    con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team, availability_status, on_team_id, "
                "percent_owned, season_points) VALUES (905, 'Zed RB', 'RB', 'SF', 'FREEAGENT', 0, NULL, 40.0)")
    fa = heat(con, position="RB", view="free_agents")
    assert [r["player"]["espn_player_id"] for r in fa["rows"]] == [905, 209]
    assert fa["free_agent_order_keys"] == ["season_points", "name"]
    assert "ownership" not in fa["free_agent_order"] and fa["free_agent_order"].endswith("Ordered by season fantasy points, then name.")
    con.execute("UPDATE fantasy_players SET season_points = NULL")
    bare = heat(con, position="RB", view="free_agents")
    assert bare["free_agent_order_keys"] == ["name"] and "points" not in bare["free_agent_order"].split("Ordered by")[1]
    con.close()


def test_bad_parameters(tmp_path):
    _, con = si_setup(tmp_path)
    for kw in ({"position": "K"}, {"horizon": "all"}, {"view": "league"}, {"nfl_team": "K C1"}, {"nfl_team": "ZZZ"},
               {"player_id": -16002}):
        with pytest.raises(BadParam):
            heat(con, **kw)
    con.close()


# ----------------------------------------------------------------------- API --

def test_api_is_read_only_with_provenance(tmp_path, monkeypatch):
    db_path, con = si_setup(tmp_path)
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    api = TestClient(app)
    mtime = db_path.stat().st_mtime_ns
    res = api.get("/api/schedule-intelligence/sos")
    assert res.status_code == 200
    body = res.json()
    assert body["contract"] == "sos-heatmap.v1" and body["position"] == "QB" and body["query"]["view"] == "teams"
    signals = {s["signal"]: s for s in body["provenance"]["signals"]}
    assert set(signals) == {"league", "schedule", "defense"}
    assert signals["defense"]["source"] == "nflverse" and signals["defense"]["coverage"]["total"] > 0
    assert api.get("/api/schedule-intelligence/sos", params={"position": "te", "horizon": "PLAYOFFS"}).json()["position"] == "TE"
    assert api.get("/api/schedule-intelligence/sos", params={"view": "roster", "team_id": 99}).status_code == 404
    assert api.get("/api/schedule-intelligence/sos", params={"player_id": 123456}).status_code == 404
    for params in ({"position": "K"}, {"horizon": "x"}, {"view": "x"}, {"nfl_team": "ZZZ"}, {"nfl_team": "LONG"}):
        assert api.get("/api/schedule-intelligence/sos", params=params).status_code == 422, params
    text = res.text.lower()
    assert "fantasypros" not in text and "probability" not in text
    assert db_path.stat().st_mtime_ns == mtime
