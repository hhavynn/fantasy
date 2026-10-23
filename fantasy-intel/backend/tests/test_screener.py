"""Advanced player screener (issue #226): typed grammar, precedence, units, missing data, caps, ownership, tenancy.

Every number is a synthetic test input: no real player, team-season or nflverse row is reproduced."""

import json
from types import MappingProxyType

import pytest
from fastapi.testclient import TestClient

from app.analytics import screener as sc
from app.db.connection import connect, fetch_dicts, init_db, replace_where

SEASON = 2026


def usage(gsis, week, pos="WR", team="KC", **kw):
    """A played game with every input known unless overridden (None = missing, never 0)."""
    row = {"season": SEASON, "week": week, "gsis_id": gsis, "team": team, "opponent": "DEN", "position": pos,
           "offense_snaps": 50, "offense_pct": 0.8, "targets": 6, "receptions": 4, "air_yards": 60.0,
           "carries": 0, "designed_rushes": 0, "rush_attempts": 0, "team_rush_attempts": 25, "team_rb_carries": 20,
           "rz_targets": 1, "rz_carries": 0, "fantasy_points_ppr": 12.0, "xfp": 10.0, "routes": None,
           "route_participation": None}
    row.update(kw)
    row.setdefault("opportunities", None)
    if "opportunities" not in kw:
        row["opportunities"] = (row["targets"] or 0) + (row["carries"] or 0)
    return row


def seed(con, rows, names=None, team_targets=None, id_map=(), pool=(), teams=(), provider="espn"):
    replace_where(con, "player_week_usage", rows, "true")
    con.execute("""CREATE TABLE IF NOT EXISTS nfl_weekly_stats (season INT, season_type VARCHAR, week INT,
                   team VARCHAR, player_id VARCHAR, targets INT, receiving_air_yards DOUBLE)""")
    con.execute("DELETE FROM nfl_weekly_stats")
    tt = team_targets or {}
    for team, week in sorted({(r["team"], r["week"]) for r in rows}):
        tgt, air = tt.get((team, week), (30, 300.0))
        con.execute("INSERT INTO nfl_weekly_stats VALUES (?, 'REG', ?, ?, 'TEAM', ?, ?)", [SEASON, week, team, tgt, air])
    names = names or {}
    replace_where(con, "nfl_players", [{"gsis_id": g, "display_name": names.get(g, f"Player {g}")}
                                       for g in sorted({r["gsis_id"] for r in rows})], "true")
    replace_where(con, "player_id_map", [{"espn_player_id": e, "gsis_id": g, "match_confidence": c,
                                          "match_method": "espn_id:nfl_players"} for e, g, c in id_map], "true")
    replace_where(con, "leagues", [{"league_id": 77, "season": SEASON, "name": "Synthetic league",
                                    "settings_raw": json.dumps({"provider": provider}),
                                    "synced_at": "2026-10-20 12:00:00"}], "true")
    replace_where(con, "fantasy_teams", [{"league_id": 77, "season": SEASON, "team_id": t, "name": n, "is_mine": m}
                                         for t, n, m in teams], "true")
    replace_where(con, "fantasy_players", [{"espn_player_id": e, "full_name": f"League {e}", "position": "WR",
                                            "availability_status": s, "on_team_id": t,
                                            "synced_at": "2026-10-20 12:00:00"} for e, s, t in pool], "true")


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "s.duckdb")
    init_db(c)
    yield c
    c.close()


def run(con, **kw):
    return sc.build_screen(con, sc.validate(**kw))


def names(out):
    return [r["gsis_id"] for r in out["rows"]]


def incomplete_ids(out):
    return sorted(r["gsis_id"] for r in out["incomplete"])


# ------------------------------------------------------------------------------------------- grammar and precedence --

def test_and_binds_inside_a_group_and_groups_are_ored(con):
    # a = targets >= 8, b = snap share >= 90%, c = xFP/G >= 20
    seed(con, [usage("A", 1, targets=9, offense_pct=0.95, xfp=5.0),     # a and b      -> group 1
               usage("B", 1, targets=9, offense_pct=0.50, xfp=5.0),     # a only       -> nothing
               usage("C", 1, targets=2, offense_pct=0.50, xfp=25.0),    # c only       -> group 2
               usage("D", 1, targets=2, offense_pct=0.95, xfp=5.0)])    # b only       -> nothing
    out = run(con, filters=["1:targets_pg:gte:8", "1:snap_share:gte:90", "2:xfp_pg:gte:20"], sort="name",
              direction="asc")
    assert names(out) == ["A", "C"]
    assert out["expression"] == "(Tgt/G ≥ 8 AND Snap % ≥ 90%) OR (xFP/G ≥ 20)"
    assert out["counts"]["not_matched"] == 2
    # the same filters all in one group: AND only
    out = run(con, filters=["1:targets_pg:gte:8", "1:snap_share:gte:90", "1:xfp_pg:gte:20"])
    assert names(out) == []
    # filter order inside the canonical query is by group, then as given
    assert run(con, filters=["2:xfp_pg:gte:20", "1:targets_pg:gte:8"])["query"]["f"] == \
        ["1:targets_pg:gte:8", "2:xfp_pg:gte:20"]


def test_operators_are_strict_or_inclusive_as_named(con):
    seed(con, [usage("A", 1, targets=5), usage("B", 1, targets=6), usage("C", 1, targets=4)])
    assert sorted(names(run(con, filters=["1:targets_pg:gte:5"]))) == ["A", "B"]
    assert sorted(names(run(con, filters=["1:targets_pg:gt:5"]))) == ["B"]
    assert sorted(names(run(con, filters=["1:targets_pg:lte:5"]))) == ["A", "C"]
    assert sorted(names(run(con, filters=["1:targets_pg:lt:5"]))) == ["C"]


# ------------------------------------------------------------------------------------------------------- units --

def test_share_filters_take_percent_and_return_percent(con):
    # team targets 40 per game: 10 targets = 25%, 6 targets = 15%
    seed(con, [usage("A", 1, targets=10), usage("A", 2, targets=10), usage("B", 1, targets=6), usage("B", 2, targets=6)],
         team_targets={("KC", 1): (40, 400.0), ("KC", 2): (40, 400.0)})
    out = run(con, filters=["1:target_share:gte:20"])
    assert names(out) == ["A"]
    cell = out["rows"][0]["metrics"]["target_share"]
    assert cell == {"value": 25.0, "n": 2, "of": 2}
    # a fraction typed as the value is read as 0.25 %: everyone clears it (the unit is the metric's, not a guess)
    assert sorted(names(run(con, filters=["1:target_share:gte:0.25"]))) == ["A", "B"]
    meta = {m["key"]: m for m in out["metrics"]}
    assert meta["target_share"]["unit"] == "percent" and meta["target_share"]["unit_label"] == "%"
    assert meta["targets_pg"]["unit"] == "per_game" and meta["xfp_pg"]["unit"] == "points_per_game"


def test_shares_are_ratios_of_sums_not_means_of_ratios(con):
    seed(con, [usage("A", 1, targets=10), usage("A", 2, targets=2)],
         team_targets={("KC", 1): (20, 200.0), ("KC", 2): (40, 400.0)})
    # sum 12 / sum 60 = 20%; the mean of the weekly ratios would be (50% + 5%) / 2 = 27.5%
    assert run(con)["rows"][0]["metrics"]["target_share"]["value"] == 20.0


def test_per_game_means_and_points_vs_expected_use_the_same_games(con):
    seed(con, [usage("A", 1, fantasy_points_ppr=20.0, xfp=10.0), usage("A", 2, fantasy_points_ppr=6.0, xfp=None)])
    m = run(con)["rows"][0]["metrics"]
    assert m["ppr_pg"] == {"value": 13.0, "n": 2, "of": 2}
    assert m["xfp_pg"] == {"value": 10.0, "n": 1, "of": 2}
    assert m["fpoe_pg"] == {"value": 10.0, "n": 1, "of": 2}      # paired: week 1 only, never 6 - 0


def test_value_bounds_follow_the_metric_unit():
    with pytest.raises(sc.ScreenerError, match="0 to 100"):
        sc.validate(filters=["1:target_share:gte:101"])
    with pytest.raises(sc.ScreenerError, match="-40 to 40"):
        sc.validate(filters=["1:fpoe_pg:lte:-41"])
    assert sc.validate(filters=["1:fpoe_pg:lte:-2.5"]).filters[0].value == -2.5


# ------------------------------------------------------------------------------------------ missing is not zero --

def test_a_missing_metric_is_unknown_never_zero(con):
    seed(con, [usage("A", 1, xfp=None), usage("A", 2, xfp=None), usage("B", 1, xfp=3.0), usage("B", 2, xfp=3.0)])
    # "xFP/G <= 5": a zero-filled A would match; A is reported as incomplete instead
    out = run(con, filters=["1:xfp_pg:lte:5"])
    assert names(out) == ["B"] and incomplete_ids(out) == ["A"]
    assert out["incomplete"][0]["missing"] == ["xfp_pg"]
    assert out["counts"]["incomplete"] == 1 and out["counts"]["matched"] == 1
    # with no filter on it, A is shown with the metric missing and the reason, and lists it as incomplete
    row = next(r for r in run(con)["rows"] if r["gsis_id"] == "A")
    assert row["metrics"]["xfp_pg"]["value"] is None and row["metrics"]["xfp_pg"]["n"] == 0
    assert "xfp" in row["metrics"]["xfp_pg"]["reason"]
    assert {"metric": "xfp_pg", "n": 0, "of": 2, "state": "missing"} in row["incomplete"]


def test_unknown_or_true_is_true_and_unknown_and_false_is_false(con):
    seed(con, [usage("A", 1, route_participation=None, targets=9),      # route unknown, targets true
               usage("B", 1, route_participation=None, targets=2)])     # route unknown, targets false
    # OR: A matches through its known group and still lists the missing metric
    out = run(con, filters=["1:route_participation:gte:70", "2:targets_pg:gte:8"])
    assert names(out) == ["A"] and incomplete_ids(out) == ["B"]
    assert any(g["metric"] == "route_participation" for g in out["rows"][0]["incomplete"])
    # AND: a false condition decides the group, so B is simply not matched, A is undecidable
    out = run(con, filters=["1:route_participation:gte:70", "1:targets_pg:gte:8"])
    assert names(out) == [] and incomplete_ids(out) == ["A"] and out["counts"]["not_matched"] == 1


def test_a_partly_covered_metric_is_evaluated_on_its_games_and_flagged(con):
    seed(con, [usage("A", 1, route_participation=0.9, routes=30), usage("A", 2, route_participation=None),
               usage("A", 3, route_participation=0.7, routes=25)])
    out = run(con, filters=["1:route_participation:gte:75"])
    row = out["rows"][0]
    assert row["metrics"]["route_participation"] == {"value": 80.0, "n": 2, "of": 3}
    assert {"metric": "route_participation", "n": 2, "of": 3, "state": "partial"} in row["incomplete"]


def test_min_routes_never_reads_missing_routes_as_zero(con):
    seed(con, [usage("A", 1, routes=30), usage("A", 2, routes=30),       # 60 known
               usage("B", 1, routes=20), usage("B", 2, routes=None),     # 20 known + unknown: undecidable at 50
               usage("C", 1, routes=10), usage("C", 2, routes=10),       # 20 known: below
               usage("D", 1, routes=None), usage("D", 2, routes=None)])  # no route data at all: undecidable
    out = run(con, min_routes=50)
    assert names(out) == ["A"] and incomplete_ids(out) == ["B", "D"]
    assert out["counts"]["below_min_routes"] == 1 and out["counts"]["routes_unknown"] == 2
    # B's known 20 already clears 15: decided without the missing game
    assert sorted(names(run(con, min_routes=15))) == ["A", "B", "C"]


def test_a_week_without_a_row_is_not_a_game(con):
    seed(con, [usage("A", 1, targets=10), usage("A", 3, targets=10),
               usage("A", 2, offense_snaps=0, offense_pct=None, targets=0, opportunities=0)])   # inactive: no game
    row = run(con)["rows"][0]
    assert row["games"] == 2 and row["weeks"] == [1, 3] and row["metrics"]["targets_pg"]["value"] == 10.0


def test_a_zero_denominator_share_is_missing_not_zero(con):
    seed(con, [usage("A", 1, pos="RB", rush_attempts=0, team_rush_attempts=0)])
    cell = run(con)["rows"][0]["metrics"]["team_rush_share"]
    assert cell["value"] is None and "0" in cell["reason"]


def test_a_column_an_older_file_lacks_is_missing(con):
    seed(con, [usage("A", 1)])
    con.execute("ALTER TABLE player_week_usage DROP COLUMN route_participation")
    row = run(con)["rows"][0]
    assert row["metrics"]["route_participation"]["value"] is None


# ------------------------------------------------------------------------------------------ cross-position --

def test_a_metric_a_position_lacks_is_false_for_it_not_unknown(con):
    seed(con, [usage("WR1", 1, targets=9), usage("RB1", 1, pos="RB", designed_rushes=15, team_rb_carries=20,
                                                  targets=1)])
    out = run(con, filters=["1:backfield_share:gte:50"])
    assert names(out) == ["RB1"] and out["incomplete"] == []
    assert out["counts"]["not_matched"] == 1
    # the WR still matches through another group
    out = run(con, filters=["1:backfield_share:gte:50", "2:targets_pg:gte:8"], sort="name", direction="asc")
    assert names(out) == ["RB1", "WR1"]
    wr = next(r for r in out["rows"] if r["gsis_id"] == "WR1")
    assert "backfield_share" not in wr["metrics"]
    assert wr["conditions"][0] == {"filter": "1:backfield_share:gte:50", "result": "not_applicable"}


def test_a_filter_no_selected_position_has_is_rejected():
    with pytest.raises(sc.ScreenerError, match="not defined for WR, TE"):
        sc.validate(positions=["WR", "TE"], filters=["1:backfield_share:gte:50"])
    with pytest.raises(sc.ScreenerError, match="not defined"):
        sc.validate(positions=["RB"], filters=["1:air_yards_share:gte:20"])


def test_positions_are_validated_and_pooled_by_latest_position(con):
    seed(con, [usage("X", 1, pos="RB"), usage("X", 2, pos="WR")])
    assert names(run(con, positions=["WR"])) == ["X"] and names(run(con, positions=["RB"])) == []
    for bad in (["QB"], ["WR", "RB", "TE", "WR"], ["wr"]):
        with pytest.raises(sc.ScreenerError, match="pos"):
            sc.validate(positions=bad)


# -------------------------------------------------------------------------------------------- weeks and samples --

def test_out_of_range_weeks_are_rejected(con):
    seed(con, [usage("A", w) for w in range(1, 6)])
    with pytest.raises(sc.ScreenerError, match="latest week with data"):
        run(con, week_to=6)
    with pytest.raises(sc.ScreenerError, match="latest week with data"):
        run(con, week_from=6)
    for kw in ({"week_from": 0}, {"week_to": 19}, {"week_from": "x"}, {"week_from": 4, "week_to": 2},
               {"week_from": "-1"}, {"week_from": "1.5"}):
        with pytest.raises(sc.ScreenerError):
            run(con, **kw)


def test_week_range_and_last_games_are_exact(con):
    seed(con, [usage("A", w, targets=w) for w in range(1, 7)])
    row = run(con, week_from=2, week_to=5)["rows"][0]
    assert row["weeks"] == [2, 3, 4, 5] and row["metrics"]["targets_pg"]["value"] == 3.5
    row = run(con, week_to=5, last=3)["rows"][0]
    assert row["weeks"] == [3, 4, 5] and row["metrics"]["targets_pg"]["value"] == 4.0
    out = run(con, week_from=2, week_to=4)
    assert out["data"]["window"] == {"from": 2, "to": 4, "last": None}
    assert out["data"]["cutoffs"]["targets_pg"] == 4 and out["data"]["cutoffs"]["route_participation"] is None


def test_min_games_is_a_sample_threshold(con):
    seed(con, [usage("A", 1), usage("A", 2), usage("B", 1)])
    out = run(con, min_games=2)
    assert names(out) == ["A"] and out["counts"]["below_min_games"] == 1 and out["counts"]["pool"] == 2


# -------------------------------------------------------------------------------------------------- sorting --

def test_sort_is_total_stable_and_missing_last_both_ways(con):
    rows = [usage(g, 1, targets=t, xfp=x) for g, t, x in
            (("D", 5, 9.0), ("A", 5, 9.0), ("C", 7, None), ("B", 5, 12.0), ("E", 5, 9.0))]
    seed(con, rows, names={"D": "Same", "A": "Same", "E": "Alpha"})
    desc = run(con, sort="xfp_pg", direction="desc")
    asc = run(con, sort="xfp_pg", direction="asc")
    # ties on 9.0 break on the name ("Alpha" < "Same"), then the id (A < D); the missing value is last either way
    assert names(desc) == ["B", "E", "A", "D", "C"]
    assert names(asc) == ["E", "A", "D", "B", "C"]
    assert all(names(run(con, sort="xfp_pg", direction="desc")) == names(desc) for _ in range(3))
    assert names(run(con, sort="name", direction="asc")) == ["E", "B", "C", "A", "D"]
    assert names(run(con, sort="name", direction="desc")) == ["D", "A", "C", "B", "E"]


# ---------------------------------------------------------------------------------------------- caps and limits --

def test_filter_group_and_result_caps(con):
    seed(con, [usage(f"P{i:02d}", 1, targets=i % 20) for i in range(30)])
    with pytest.raises(sc.ScreenerError, match=f"at most {sc.MAX_FILTERS} filters"):
        sc.validate(filters=["1:targets_pg:gte:1"] * (sc.MAX_FILTERS + 1))
    assert len(sc.validate(filters=["1:targets_pg:gte:1"] * sc.MAX_FILTERS).filters) == sc.MAX_FILTERS
    with pytest.raises(sc.ScreenerError, match="group"):
        sc.validate(filters=[f"{sc.MAX_GROUPS + 1}:targets_pg:gte:1"])
    for bad in (0, sc.MAX_LIMIT + 1, "ten"):
        with pytest.raises(sc.ScreenerError, match="limit"):
            sc.validate(limit=bad)
    out = run(con, limit=10)
    assert len(out["rows"]) == 10 and out["truncated"] is True and out["counts"]["matched"] == 30
    assert out["counts"]["shown"] == 10
    assert run(con, limit=50)["truncated"] is False


def test_the_incomplete_list_is_capped_and_counted(con):
    seed(con, [usage(f"P{i:03d}", 1, xfp=None) for i in range(sc.INCOMPLETE_CAP + 5)])
    out = run(con, filters=["1:xfp_pg:gte:1"])
    assert len(out["incomplete"]) == sc.INCOMPLETE_CAP and out["incomplete_truncated"] is True
    assert out["counts"]["incomplete"] == sc.INCOMPLETE_CAP + 5


# ------------------------------------------------------------------------------------------------- injection --

@pytest.mark.parametrize("token", [
    "1:xfp_pg:gte:1 OR 1=1", "1:xfp_pg:gte:1;DROP TABLE player_week_usage", "1:xfp_pg';--:gte:1",
    "1:__class__:gte:1", "1:xfp_pg:gte:1e9", "1:xfp_pg:gte:nan", "1:xfp_pg:gte:inf", "1:xfp_pg:between:1",
    "1:XFP_PG:gte:1", "1:xfp_pg:gte:", "a:xfp_pg:gte:1", "1:xfp_pg:gte:1:2", "1:xfp_pg:gte:0x10", " 1:xfp_pg:gte:1",
    "1:xfp_pg:gte:1\n", "1:xfp_pg:gte:１", "1:(SELECT 1):gte:1", "1:xfp_pg:gte:12345.6",
])
def test_injection_and_malformed_filters_are_rejected(token):
    with pytest.raises(sc.ScreenerError):
        sc.validate(filters=[token])


@pytest.mark.parametrize("kw", [{"sort": "xfp_pg; DROP TABLE x"}, {"sort": "metrics"}, {"direction": "up"},
                                {"owner": "everyone"}, {"min_games": "1 OR 1=1"}, {"last": 9}, {"min_routes": 0},
                                {"season": 1900}])
def test_other_parameters_are_allowlisted(kw):
    with pytest.raises(sc.ScreenerError):
        sc.validate(**kw)


def test_the_data_is_untouched_after_hostile_requests(tmp_path, monkeypatch):
    db = tmp_path / "h.duckdb"
    c = connect(db)
    init_db(c)
    seed(c, [usage("A", 1)])
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db))
    for name in ("FANTASY_API_KEY", "MULTILEAGUE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    from app.main import app
    api = TestClient(app, raise_server_exceptions=False)
    for q in ("f=1:xfp_pg:gte:1;DROP TABLE player_week_usage", "sort=1;DELETE FROM nfl_players",
              "pos=WR' OR '1'='1", "from=1;--", "f=" + "1:xfp_pg:gte:1&f=" * 20 + "1:xfp_pg:gte:1",
              "owner=" + "a" * 5000, "limit=1000"):
        r = api.get(f"/api/screener?{q}")
        assert r.status_code == 422, (q, r.text[:200])
    r = api.get("/api/screener")
    assert r.status_code == 200 and [x["gsis_id"] for x in r.json()["rows"]] == ["A"]


# ------------------------------------------------------------------------------------------------- percentiles --

def test_percentiles_are_position_relative_and_need_a_pool(con):
    rows = [usage(f"W{i}", 1, targets=i) for i in range(1, 6)] + [usage(f"T{i}", 1, pos="TE", targets=i)
                                                                    for i in range(1, 4)]
    seed(con, rows)
    out = run(con)
    by = {r["gsis_id"]: r["metrics"]["targets_pg"] for r in out["rows"]}
    assert by["W5"]["percentile"] == 90 and by["W5"]["pool"] == 5 and by["W1"]["percentile"] == 10
    assert all("percentile" not in by[f"T{i}"] for i in range(1, 4))       # 3 TEs: undefined, not computed
    # the pool is the sample-qualified position pool, so filters never move a percentile
    out = run(con, filters=["1:targets_pg:gte:5"], positions=["WR"])
    assert out["rows"][0]["metrics"]["targets_pg"]["percentile"] == 90


def test_ties_share_the_mid_rank(con):
    seed(con, [usage(f"W{i}", 1, targets=5) for i in range(5)])
    assert {r["metrics"]["targets_pg"]["percentile"] for r in run(con)["rows"]} == {50}


# --------------------------------------------------------------------------------------------------- ownership --

def _owned(con, provider="espn"):
    seed(con, [usage(g, 1, targets=t) for g, t in (("R", 9), ("F", 8), ("W", 7), ("U", 6), ("M", 5), ("Z", 4),
                                                   ("N", 3))],
         id_map=[(101, "R", "high"), (102, "F", "high"), (103, "W", "high"), (104, "M", "high"), (105, "Z", "high"),
                 (106, "Z", "high"), (107, "N", "medium")],
         pool=[(101, "ONTEAM", 1), (102, "FREEAGENT", None), (103, "WAIVERS", None), (104, "ONTEAM", 2),
               (105, "FREEAGENT", None), (106, "FREEAGENT", None), (107, "FREEAGENT", None)],
         teams=[(1, "Mine", True), (2, "Rival", False)], provider=provider)


def test_ownership_is_annotated_and_unknown_is_never_available(con):
    _owned(con)
    out = run(con, sort="targets_pg")
    own = {r["gsis_id"]: r["ownership"] for r in out["rows"]}
    assert own["R"]["status"] == "rostered" and own["R"]["is_mine"] and own["R"]["team_name"] == "Mine"
    assert own["M"]["status"] == "rostered" and not own["M"]["is_mine"] and own["M"]["team_name"] == "Rival"
    assert (own["F"]["status"], own["F"]["label"], own["F"]["available"]) == ("free_agent", "Free agent", True)
    assert own["W"]["status"] == "waivers" and own["W"]["available"]
    # unmapped (U), ambiguous (Z: two high-confidence league ids) and a medium-confidence name match (N): unknown
    for g in ("U", "Z", "N"):
        assert own[g]["status"] == "unknown" and own[g]["available"] is False and own[g]["label"] == sc.UNKNOWN_OWNERSHIP
        assert own[g]["reason"]
    assert out["counts"]["ownership_unknown"] == 3
    assert out["ownership"]["my_team_id"] == 1 and out["ownership"]["provider"] == "espn"


def test_owner_filters_exclude_unknown_and_count_it(con):
    _owned(con)
    out = run(con, owner="available")
    assert sorted(names(out)) == ["F", "W"] and out["counts"]["ownership_excluded"] == 5
    out = run(con, owner="rostered")
    assert sorted(names(out)) == ["M", "R"]


def test_a_provider_without_a_waiver_state_says_not_rostered(con):
    _owned(con, provider="sleeper")
    own = {r["gsis_id"]: r["ownership"] for r in run(con)["rows"]}
    assert own["F"]["status"] == "not_rostered" and own["F"]["label"] == "Not rostered" and own["F"]["available"]
    assert "Free agent" not in json.dumps(run(con), default=str)


def test_without_a_league_pool_every_row_is_unknown(con):
    seed(con, [usage("A", 1)])
    con.execute("DROP TABLE fantasy_players")
    out = run(con)
    assert out["ownership"]["available"] is False
    assert out["rows"][0]["ownership"]["status"] == "unknown" and out["rows"][0]["ownership"]["available"] is False
    assert run(con, owner="available")["rows"] == []


def test_no_espn_wording_reaches_a_response(con):
    """app/api/provider_copy.py would rewrite it for Sleeper leagues; the screener never says it at all."""
    _owned(con)
    assert "ESPN" not in json.dumps(run(con), default=str)


# -------------------------------------------------------------------------------------------------- registry --

def test_registry_tables_are_immutable_and_typed():
    assert isinstance(sc.METRICS, tuple) and isinstance(sc.METRIC_BY_KEY, MappingProxyType)
    assert isinstance(sc.OPS, MappingProxyType) and isinstance(sc.PRESETS, tuple)
    for p in sc.PRESETS:
        assert isinstance(p, MappingProxyType) and isinstance(p["query"], MappingProxyType)
        assert all(not isinstance(v, list) for v in p["query"].values())
    keys = [m.key for m in sc.METRICS]
    assert len(keys) == len(set(keys))
    for m in sc.METRICS:
        assert set(m.positions) <= set(sc.POSITIONS) and m.lo < m.hi and m.unit in sc.UNIT_LABEL
        assert set(m.inputs) <= set(sc.USAGE_COLUMNS) | {"team_targets", "team_air_yards"}
        assert m.denominator and m.source and m.definition
    # air-yard share: nflverse air yards (already ingested); route data only from an authorized import
    assert sc.METRIC_BY_KEY["air_yards_share"].source == sc.NFLVERSE_STATS
    assert "authorized" in sc.METRIC_BY_KEY["route_participation"].source


def test_every_preset_is_a_valid_screen(con):
    seed(con, [usage("A", w) for w in range(1, 5)])
    assert len(sc.PRESETS) >= 5
    for p in sc.presets():
        q = p["query"]
        parsed = sc.validate(positions=q.get("pos"), last=q.get("last"), min_games=q.get("min_games"),
                             filters=q.get("f"), owner=q.get("owner"), sort=q.get("sort"), direction=q.get("dir"))
        assert len(parsed.filters) >= 1
        assert sc.build_screen(con, parsed)["status"] in ("ok", "empty")


def test_the_api_echoes_the_canonical_query_and_registry(tmp_path, monkeypatch):
    db = tmp_path / "a.duckdb"
    c = connect(db)
    init_db(c)
    seed(c, [usage("A", w, targets=8) for w in range(1, 4)])
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db))
    for name in ("FANTASY_API_KEY", "MULTILEAGUE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    from app.main import app
    api = TestClient(app, raise_server_exceptions=False)
    r = api.get("/api/screener?pos=WR&from=1&to=3&last=2&f=1:targets_pg:gte:7.50&sort=targets_pg&limit=5")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["contract"] == sc.CONTRACT and d["season"] == SEASON
    assert d["query"] == {"season": None, "pos": ["WR"], "from": 1, "to": 3, "last": 2, "min_games": 1,
                          "min_routes": None, "f": ["1:targets_pg:gte:7.5"], "owner": "any", "sort": "targets_pg",
                          "dir": "desc", "limit": 5}
    assert d["rows"][0]["weeks"] == [2, 3]
    m = api.get("/api/screener/metrics").json()
    assert [x["key"] for x in m["metrics"]] == [x.key for x in sc.METRICS]
    assert m["limits"]["max_filters"] == sc.MAX_FILTERS and len(m["presets"]) >= 5


def test_the_screen_reads_one_bounded_parameterized_query(con, monkeypatch):
    """Every value reaches DuckDB as a bound parameter; the SQL text is built from constants only."""
    seed(con, [usage("A", 1)])
    seen = []
    real = sc.fetch_dicts

    def spy(c, sql, params=()):
        seen.append((sql, list(params)))
        return real(c, sql, params)
    monkeypatch.setattr(sc, "fetch_dicts", spy)
    run(con, positions=["WR"], filters=["1:targets_pg:gte:5"], week_to=1)
    run(con, positions=["RB", "TE"], filters=["2:xfp_pg:lt:3", "1:snap_share:gte:10"], week_from=1, last=2,
        sort="name", owner="available")
    usage_sql = [(s, p) for s, p in seen if "FROM player_week_usage u" in s]
    assert len(usage_sql) == 2 and usage_sql[0][0] == usage_sql[1][0]      # one constant SQL text for any screen
    assert usage_sql[0][1] == [SEASON, SEASON, 1, 1]
    assert all("targets_pg" not in s and "gte" not in s and "xfp_pg" not in s for s, _ in seen)
    assert fetch_dicts(con, "SELECT count(*) AS n FROM player_week_usage")[0]["n"] == 1
