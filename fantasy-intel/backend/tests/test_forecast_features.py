"""Point-in-time feature pipeline: leakage, joins, zero-vs-missing, cold start. Synthetic data only."""

import polars as pl
import pytest

from app.analytics.forecast_features import (FEATURE_DOCS, Sources, features_for_week, load_panel, load_sources,
                                             played_targets)
from app.analytics.league_scoring import (UnsupportedScoring, points, resolve_skill_scoring)

from synthetic_forecast import SCORING_ITEMS, database, gsis, generate, write

SCORING = resolve_skill_scoring(SCORING_ITEMS)
SEASONS = [2024, 2025]


@pytest.fixture
def sources(tmp_path):
    con = database(tmp_path / "f.duckdb", SEASONS)
    src = load_sources(con, SCORING)
    con.close()
    return src


def _mutate_from(src: Sources, season: int, week: int, factor: float = 1000.0) -> Sources:
    """Corrupt every row at or after (season, week): box scores, xFP, snaps, scores, defense."""
    at = (pl.col("season") * 100 + pl.col("week")) >= season * 100 + week
    num = [c for c, t in src.panel.schema.items() if t == pl.Float64]
    panel = src.panel.with_columns([pl.when(at).then(pl.col(c) * factor + 999).otherwise(pl.col(c)).alias(c) for c in num])
    games = src.games.with_columns(pl.when(at).then(pl.col("points_for") + 500).otherwise(pl.col("points_for")).alias("points_for"))
    defense = src.defense.with_columns(pl.when(at).then(pl.col("pass_epa_sum") + 500).otherwise(pl.col("pass_epa_sum")).alias("pass_epa_sum"),
                                       pl.when(at).then(pl.col("rush_epa_sum") - 500).otherwise(pl.col("rush_epa_sum")).alias("rush_epa_sum"))
    return Sources(panel, games, defense)


def _features(src, season, week):
    t = played_targets(src, season, week).drop("league_points")
    return features_for_week(src, season, week, t).sort("gsis_id")


def test_no_future_or_current_week_leakage(sources):
    """Corrupting the target week and every later week (including other players, scores and
    defensive rows) must not change a single feature value."""
    clean = _features(sources, 2025, 8)
    leaky = _features(_mutate_from(sources, 2025, 8), 2025, 8)
    assert clean.height > 40
    assert clean.equals(leaky)


def test_features_are_bit_reproducible(sources):
    assert _features(sources, 2025, 8).equals(_features(sources, 2025, 8))


def test_previous_week_does_change_features(sources):
    """Sanity: the guard is a cutoff, not a frozen pipeline - week W-1 data is used."""
    clean = _features(sources, 2025, 8)
    moved = _features(_mutate_from(sources, 2025, 7), 2025, 8)
    assert not clean.equals(moved)


def test_defense_context_is_cut_off_before_target_week(sources):
    """Opponent features use only the opponent's games before week W of the same season."""
    clean = _features(sources, 2025, 6)
    later_defense = Sources(sources.panel, sources.games, sources.defense.with_columns(
        pl.when(pl.col("week") >= 6).then(pl.col("pass_epa_sum") + 100).otherwise(pl.col("pass_epa_sum"))))
    assert clean["opp_pass_epa"].equals(_features(later_defense, 2025, 6)["opp_pass_epa"])
    earlier_defense = Sources(sources.panel, sources.games, sources.defense.with_columns(
        pl.when((pl.col("week") == 5) & (pl.col("season") == 2025) & (pl.col("defteam") == "AAA"))
        .then(pl.col("pass_epa_sum") + 100).otherwise(pl.col("pass_epa_sum"))))
    assert not clean["opp_pass_epa"].equals(_features(earlier_defense, 2025, 6)["opp_pass_epa"])


def test_week_one_opponent_has_no_current_season_data(sources):
    f = _features(sources, 2025, 1)
    assert (f["opp_games"] == 0).all() and (f["opp_pos_allowed"] == 0).all()
    assert (f["games_season"] == 0).all() and f["pts_season"].is_null().all()
    assert f["pts_prev_season"].is_not_null().all()   # last season still informs player features


def test_no_cross_player_contamination(sources):
    target = gsis("AAA", "WR1")
    other = gsis("BBB", "RB1")
    bumped = Sources(sources.panel.with_columns(
        pl.when(pl.col("gsis_id") == other).then(pl.col("targets") + 50).otherwise(pl.col("targets"))),
        sources.games, sources.defense)
    cols = ["targets_pg", "pts_recent", "xfp_recent", "snap_pct", "carries_pg", "air_yards_pg"]
    a = _features(sources, 2025, 9).filter(pl.col("gsis_id") == target).select(cols)
    b = _features(bumped, 2025, 9).filter(pl.col("gsis_id") == target).select(cols)
    assert a.equals(b)
    c = _features(bumped, 2025, 9).filter(pl.col("gsis_id") == other)["targets_pg"][0]
    assert c > _features(sources, 2025, 9).filter(pl.col("gsis_id") == other)["targets_pg"][0] + 40


def test_player_week_joins_use_ids_not_positions(tmp_path):
    con = database(tmp_path / "j.duckdb", [2025])
    con.execute("UPDATE nfl_snap_counts SET offense_pct = 0.123 WHERE pfr_player_id = ? AND week = 3",
                [f"pfr-{gsis('CCC', 'TE')}"])
    con.execute("UPDATE nfl_ff_opportunity SET total_fantasy_points_exp = 77.7 WHERE player_id = ? AND week = 3",
                [gsis("CCC", "TE")])
    panel = load_panel(con, SCORING)
    row = panel.filter((pl.col("gsis_id") == gsis("CCC", "TE")) & (pl.col("week") == 3))
    assert row["offense_pct"][0] == 0.123 and row["xfp"][0] == 77.7
    others = panel.filter((pl.col("week") == 3) & (pl.col("gsis_id") != gsis("CCC", "TE")))
    assert (others["offense_pct"] != 0.123).all() and (others["xfp"] != 77.7).all()
    assert panel.select("season", "week", "gsis_id").is_duplicated().sum() == 0


def test_true_zero_versus_missing(tmp_path):
    con = database(tmp_path / "z.duckdb", [2025])
    snap_only = gsis("DDD", "WR3")
    con.execute("DELETE FROM nfl_weekly_stats WHERE player_id = ? AND week = 4", [snap_only])
    con.execute("DELETE FROM nfl_pbp_player_week WHERE player_id = ? AND week = 4", [snap_only])
    con.execute("DELETE FROM nfl_team_week WHERE team = 'EEE' AND week = 4")      # play-by-play missing for EEE
    con.execute("DELETE FROM nfl_pbp_player_week WHERE team = 'EEE' AND week = 4")
    panel = load_panel(con, SCORING)
    r = panel.filter((pl.col("gsis_id") == snap_only) & (pl.col("week") == 4))
    assert r["has_stats"][0] is False and r["league_points"][0] == 0.0 and r["targets"][0] == 0.0
    assert r["rz_targets"][0] == 0.0           # team play-by-play exists: no row = measured zero
    e = panel.filter((pl.col("team") == "EEE") & (pl.col("week") == 4))
    assert e["rz_targets"].is_null().all()      # team play-by-play missing: unknown, not zero
    con.execute("DROP TABLE nfl_ff_opportunity")
    src = load_sources(con, SCORING)
    f = _features(src, 2025, 6)
    assert f["xfp_recent"].is_null().all() and (f["xfp_missing"] == 1).all()


def test_rookie_and_low_history_players(sources):
    rookie = "00-ROOKIE"
    row = sources.panel.filter((pl.col("gsis_id") == gsis("AAA", "WR3")) & (pl.col("season") == 2025)
                               & (pl.col("week") == 10)).with_columns(pl.lit(rookie).alias("gsis_id"),
                                                                      pl.lit(30.0).alias("league_points"))
    src = Sources(pl.concat([sources.panel, row]), sources.games, sources.defense)
    t = pl.DataFrame({"gsis_id": [rookie], "position": ["WR"], "team": ["AAA"], "opponent": ["BBB"], "is_home": [True]})
    debut = features_for_week(src, 2025, 10, t)
    assert debut["games_recent"][0] == 0 and debut["pts_recent_raw"][0] is None
    after = features_for_week(src, 2025, 11, t)
    assert after["games_recent"][0] == 1 and after["pts_recent_raw"][0] == 30.0
    # one 30-point game is shrunk toward the WR mean, not taken at face value
    assert after["pts_recent"][0] < 30.0 * 0.5


def test_sharply_changing_workload(sources):
    p = gsis("FFF", "RB2")
    jump = sources.panel.with_columns(
        pl.when((pl.col("gsis_id") == p) & (pl.col("season") == 2025) & (pl.col("week") == 7))
        .then(pl.lit(1.0)).otherwise(pl.col("offense_pct")).alias("offense_pct"),
        pl.when((pl.col("gsis_id") == p) & (pl.col("season") == 2025) & (pl.col("week") == 7))
        .then(pl.lit(30.0)).otherwise(pl.col("carries")).alias("carries"))
    f = _features(Sources(jump, sources.games, sources.defense), 2025, 8).filter(pl.col("gsis_id") == p)
    assert f["snap_trend"][0] > 0.2 and f["opp_trend"][0] > 15


def test_every_model_feature_is_documented(sources):
    from app.analytics.independent_projections import FEATURE_SETS

    f = _features(sources, 2025, 5)
    for sets in FEATURE_SETS.values():
        for feats in sets.values():
            assert set(feats) <= set(FEATURE_DOCS) and set(feats) <= set(f.columns)


# ----------------------------------------------------------------------------- scoring


def test_league_scoring_matches_components():
    row = {"passing_yards": 250, "passing_tds": 2, "passing_interceptions": 1, "rushing_yards": 20, "rushing_tds": 0,
           "receiving_yards": 0, "receiving_tds": 0, "receptions": 0, "fumbles_lost_total": 1,
           "passing_2pt_conversions": 1, "rushing_2pt_conversions": 0, "receiving_2pt_conversions": 0,
           "special_teams_tds": 0, "fumble_recovery_tds": 0}
    assert points(row, SCORING) == pytest.approx(10 + 8 - 2 + 2 - 2 + 2)
    assert points({**row, "passing_yards": None}, SCORING) is None   # unknown is never zero


@pytest.mark.parametrize("extra,reason", [
    ({"statId": 37, "points": 3.0, "pointsOverrides": {}}, "not implemented"),             # 100-yd rushing bonus
    ({"statId": 53, "points": 0.5, "pointsOverrides": {"6": 1.5}}, "slot-specific"),       # TE premium
    ({"statId": 58, "points": 0.25, "pointsOverrides": {}}, "not implemented"),            # points per target
])
def test_unsupported_custom_scoring_is_refused(extra, reason, tmp_path):
    s = resolve_skill_scoring([i for i in SCORING_ITEMS if i["statId"] != extra["statId"]] + [extra])
    assert not s.supported and reason in " ".join(u["reason"] for u in s.unsupported)
    con = database(tmp_path / "u.duckdb", [2025])
    with pytest.raises(UnsupportedScoring):
        load_panel(con, s)


def test_half_ppr_changes_fingerprint_and_labels(tmp_path):
    half = resolve_skill_scoring([{**i, "points": 0.5} if i["statId"] == 53 else i for i in SCORING_ITEMS])
    assert half.supported and half.fingerprint != SCORING.fingerprint
    con = database(tmp_path / "h.duckdb", [2025])
    full, hp = load_panel(con, SCORING), load_panel(con, half)
    diff = (full["league_points"] - hp["league_points"])
    assert (diff - 0.5 * full["receptions"]).abs().max() < 1e-9


def test_kick_and_punt_return_tds_must_match():
    items = [{**i, "points": 4.0} if i["statId"] == 101 else i for i in SCORING_ITEMS]
    assert not resolve_skill_scoring(items).supported


def test_dst_and_kicker_rules_do_not_score_offense():
    assert "defensive0PointsAllowed" not in str(SCORING.weights)
    assert set(SCORING.weights) == {"passing_yards", "passing_tds", "passing_2pt_conversions", "passing_interceptions",
                                    "rushing_yards", "rushing_tds", "rushing_2pt_conversions", "receiving_yards",
                                    "receiving_tds", "receiving_2pt_conversions", "receptions", "fumble_recovery_tds",
                                    "fumbles_lost_total", "special_teams_tds"}
    assert [u["stat_id"] for u in SCORING.unmodeled] == [103]


def test_generate_is_deterministic():
    a, b = generate([2025], seed=3), generate([2025], seed=3)
    assert all(a[k].equals(b[k]) for k in a)


def test_sources_concat_prefers_last_part_for_shared_season(tmp_path):
    con1 = database(tmp_path / "a.duckdb", [2024, 2025], seed=1)
    con2 = database(tmp_path / "b.duckdb", [2025], seed=2)
    hist, live = load_sources(con1, SCORING), load_sources(con2, SCORING)
    both = Sources.concat([hist, live])
    assert both.panel.filter(pl.col("season") == 2025).equals(live.panel.filter(pl.col("season") == 2025))
    assert both.panel.filter(pl.col("season") == 2024).height == hist.panel.filter(pl.col("season") == 2024).height


def test_write_helper_keeps_schema_tables(tmp_path):
    con = database(tmp_path / "w.duckdb", [2025])
    write(con, generate([2025], seed=9))
    assert con.execute("SELECT count(*) FROM nfl_players").fetchone()[0] == 56
