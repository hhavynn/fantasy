"""Research-only FTN read-progression and depth-chart signals (app.analytics.usage_signals):
read-code semantics, play-id joins, missing / delayed charting, point-in-time depth snapshots,
identity and team changes, same-game leakage, scoring and selection checks, snapshot corrections,
cache invalidation, and isolation from ifp-1.0.0 / ifp-2.0.0."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pathlib
import random

import numpy as np
import polars as pl
import pytest

from app.analytics import challenger as ch
from app.analytics import usage_signals as us

UTC = timezone.utc


# ----------------------------------------------------------------------------- fixtures


def _pbp(plays: list[dict]) -> pl.DataFrame:
    base = {"season": 2023, "week": 1, "season_type": "REG", "game_id": "2023_01_AAA_BBB", "posteam": "AAA",
            "play_type": "pass", "pass_attempt": 1, "sack": 0, "two_point_attempt": 0, "receiver_player_id": "r1",
            "air_yards": 8.0}
    rows = [{**base, **p} for p in plays]
    return pl.DataFrame(rows).with_columns(pl.col("play_id").cast(pl.Float64))


def _ftn(rows: list[dict]) -> pl.DataFrame:
    base = {"nflverse_game_id": "2023_01_AAA_BBB", "season": 2023, "week": 1, "read_thrown": "1",
            "date_pulled": datetime(2023, 9, 13, tzinfo=UTC)}
    return pl.DataFrame([{**base, **r} for r in rows]).with_columns(pl.col("nflverse_play_id").cast(pl.Int32))


def _classes(pbp, ftn, semantics="empirical") -> dict:
    t, _ = us.classify_targets(pbp, ftn, semantics)
    return dict(zip(t["play_id"].to_list(), t["read_class"].to_list()))


# ----------------------------------------------------------------------------- read codes


def test_read_code_classification_both_semantics():
    emp = {c: us.classify_read(c, 2024, "empirical") for c in ("0", "1", "2", "CHK", "DES", "SD", " CHK", "XYZ", None)}
    assert emp == {"0": us.NO_READ, "1": us.FIRST, "2": us.LATER, "CHK": us.CHECKDOWN, "DES": us.DESIGNED,
                   "SD": us.SCRAMBLE, " CHK": us.CHECKDOWN, "XYZ": us.UNKNOWN, None: us.MISSING}
    dic = {c: us.classify_read(c, 2024, "dictionary") for c in ("0", "1", "2", None)}
    assert dic == {"0": us.FIRST, "1": us.SECOND, "2": us.THIRD_PLUS, None: us.MISSING}
    # the vectorized expression agrees with the scalar reference on every code and season
    codes = ["0", "1", "2", "CHK", "DES", "SD", " CHK", "", "XYZ", None]
    for sem in us.SEMANTICS:
        for season in (2022, 2023):
            df = pl.DataFrame({"read_thrown": codes, "season": [season] * len(codes)})
            got = df.select(us.read_class_expr(sem).alias("c"))["c"].to_list()
            assert got == [us.classify_read(c, season, sem) for c in codes], (sem, season)


def test_2022_primary_read_is_unavailable_never_zero():
    # dictionary reading: a 2022 NA target may be a primary read -> unavailable, and the team-game's
    # first-read counts (hence every first-read feature) are null, not zero
    assert us.classify_read(None, 2022, "dictionary") == us.PRIMARY_UNAVAILABLE
    assert us.classify_read(None, 2023, "dictionary") == us.MISSING
    assert us.classify_read(None, 2022, "empirical") == us.MISSING
    pbp = _pbp([{"play_id": 1, "season": 2022, "game_id": "g22"}, {"play_id": 2, "season": 2022, "game_id": "g22"}])
    ftn = _ftn([{"nflverse_game_id": "g22", "season": 2022, "nflverse_play_id": 1, "read_thrown": None},
                {"nflverse_game_id": "g22", "season": 2022, "nflverse_play_id": 2, "read_thrown": "1"}])
    t, _ = us.classify_targets(pbp, ftn, "dictionary")
    pr, tr = us.game_reads(t)
    panel = pl.DataFrame({"season": [2022], "week": [1], "gsis_id": ["r1"], "position": ["WR"], "team": ["AAA"]})
    a = us.appearance_reads(panel, pr, tr)
    assert a["ftn_usable"].to_list() == [False] and a["p_first"].to_list() == [None]
    f = us.ftn_features(a, 2022, 2, panel.select("gsis_id", "position"))
    assert f["fr_team_share"].to_list() == [None] and f["ftn_games"].to_list() == [0]
    # the empirical reading counts the same game: "1" is a first read, NA is missing (not a read)
    t2, _ = us.classify_targets(pbp, ftn, "empirical")
    pr2, tr2 = us.game_reads(t2)
    a2 = us.appearance_reads(panel, pr2, tr2)
    assert a2["p_first"].to_list() == [1] and a2["p_targets"].to_list() == [2]


def test_designed_checkdown_and_scramble_are_never_first_reads():
    pbp = _pbp([{"play_id": i} for i in range(1, 6)])
    ftn = _ftn([{"nflverse_play_id": 1, "read_thrown": "DES"}, {"nflverse_play_id": 2, "read_thrown": "CHK"},
                {"nflverse_play_id": 3, "read_thrown": "SD"}, {"nflverse_play_id": 4, "read_thrown": "1"},
                {"nflverse_play_id": 5, "read_thrown": "0"}])
    for sem in us.SEMANTICS:
        c = _classes(pbp, ftn, sem)
        assert c[1] == us.DESIGNED and c[2] == us.CHECKDOWN and c[3] == us.SCRAMBLE
        assert [p for p, v in c.items() if v == us.FIRST] == ([4] if sem == "empirical" else [5])
    t, _ = us.classify_targets(pbp, ftn, "empirical")
    pr, _ = us.game_reads(t)
    r = pr.row(0, named=True)
    assert (r[f"n_{us.FIRST}"], r[f"n_{us.DESIGNED}"], r["n_progression"], r["targets"]) == (1, 1, 1, 5)


def test_duplicate_and_mismatched_play_ids_are_excluded_not_guessed():
    pbp = _pbp([{"play_id": 1}, {"play_id": 2}, {"play_id": 3}])
    ftn = _ftn([{"nflverse_play_id": 1, "read_thrown": "1"}, {"nflverse_play_id": 1, "read_thrown": "2"},
                {"nflverse_play_id": 2, "read_thrown": "1", "week": 2},          # FTN says a different week
                {"nflverse_play_id": 3, "read_thrown": "1"},
                {"nflverse_play_id": 99, "read_thrown": "1"}])                   # no such play
    t, audit = us.classify_targets(pbp, ftn, "empirical")
    c = dict(zip(t["play_id"].to_list(), t["read_class"].to_list()))
    assert c == {1: us.DUPLICATE, 2: us.ID_MISMATCH, 3: us.FIRST}
    assert audit["duplicate_keys"] == 1 and audit["ftn_rows_without_pbp_play"] == 1


def test_missing_charting_stays_unknown():
    # game g2 has no FTN rows at all; game g1 is charted but one target has no code
    pbp = _pbp([{"play_id": 1, "game_id": "g1"}, {"play_id": 2, "game_id": "g1"},
                {"play_id": 1, "game_id": "g2", "week": 2}])
    ftn = _ftn([{"nflverse_game_id": "g1", "nflverse_play_id": 1, "read_thrown": "1"},
                {"nflverse_game_id": "g1", "nflverse_play_id": 2, "read_thrown": None}])
    t, _ = us.classify_targets(pbp, ftn, "empirical")
    c = {(g, p): v for g, p, v in t.select("game_id", "play_id", "read_class").iter_rows()}
    assert c == {("g1", 1): us.FIRST, ("g1", 2): us.MISSING, ("g2", 1): us.NOT_CHARTED}
    pr, tr = us.game_reads(t)
    panel = pl.DataFrame({"season": [2023, 2023], "week": [1, 2], "gsis_id": ["r1", "r1"], "position": ["WR", "WR"],
                          "team": ["AAA", "AAA"]})
    a = us.appearance_reads(panel, pr, tr).sort("week")
    assert a["p_first"].to_list() == [1, None]          # uncharted week 2: null, never 0
    f = us.ftn_features(a, 2023, 3, panel.select("gsis_id", "position").unique())
    assert f["ftn_games"].to_list() == [1]              # only the charted game counts
    # a player who appeared in a CHARTED game without a target has measured zeros
    panel2 = pl.concat([panel, pl.DataFrame({"season": [2023], "week": [1], "gsis_id": ["r9"], "position": ["WR"],
                                             "team": ["AAA"]})])
    a2 = us.appearance_reads(panel2, pr, tr).filter(pl.col("gsis_id") == "r9")
    assert a2["p_first"].to_list() == [0] and a2["p_targets"].to_list() == [0]


def test_delayed_charting_is_not_available_before_its_pull():
    pbp = _pbp([{"play_id": 1, "game_id": "g1"}, {"play_id": 1, "game_id": "g2", "week": 2}])
    ftn = _ftn([{"nflverse_game_id": "g1", "nflverse_play_id": 1, "date_pulled": datetime(2023, 9, 12, tzinfo=UTC)},
                {"nflverse_game_id": "g2", "nflverse_play_id": 1, "week": 2,
                 "date_pulled": datetime(2023, 9, 22, tzinfo=UTC)}])
    as_of = datetime(2023, 9, 21, tzinfo=UTC)
    avail = us.restrict_to_available(ftn, as_of)
    assert set(avail["nflverse_game_id"]) == {"g1"}
    t, _ = us.classify_targets(pbp, avail, "empirical")
    assert dict(zip(t["game_id"], t["read_class"])) == {"g1": us.FIRST, "g2": us.NOT_CHARTED}
    # a game partly pulled after the cutoff is not available either (all rows must precede it)
    part = pl.concat([ftn, _ftn([{"nflverse_game_id": "g1", "nflverse_play_id": 7,
                                  "date_pulled": datetime(2023, 9, 25, tzinfo=UTC)}])])
    assert us.restrict_to_available(part, as_of).height == 0


def test_same_game_charting_is_never_a_feature_input():
    rng = random.Random(4)
    rows = []
    for w in range(1, 7):
        for p in range(6):
            first = rng.randint(0, 6)
            rows.append({"season": 2024, "week": w, "gsis_id": f"r{p}", "position": "WR", "team": "AAA",
                         "ftn_usable": True, "p_targets": first + 2, "p_first": first, "p_des": 1, "p_chk": 0,
                         "t_targets": 40, "t_first": 22})
    a = pl.DataFrame(rows)
    targets = a.filter(pl.col("week") == 6).select("gsis_id", "position")
    f1 = us.ftn_features(a, 2024, 6, targets)
    # rewrite the target week's (and a later week's) charting: features must not move
    a2 = a.with_columns(pl.when(pl.col("week") >= 6).then(99).otherwise(pl.col("p_first")).alias("p_first"))
    a2 = pl.concat([a2, a2.filter(pl.col("week") == 6).with_columns(pl.lit(7, dtype=pl.Int64).alias("week"))])
    assert f1.equals(us.ftn_features(a2, 2024, 6, targets))
    # ...while changing an earlier week does
    a3 = a.with_columns(pl.when(pl.col("week") == 5).then(99).otherwise(pl.col("p_first")).alias("p_first"))
    assert not f1.equals(us.ftn_features(a3, 2024, 6, targets))


def test_shrinkage_pulls_sparse_players_toward_the_position_mean():
    rows = [{"season": 2024, "week": w, "gsis_id": g, "position": "WR", "team": "AAA", "ftn_usable": True,
             "p_targets": t, "p_first": fr, "p_des": 0, "p_chk": 0, "t_targets": 30, "t_first": 15}
            for w in range(1, 6) for g, t, fr in (("a", 6, 3), ("b", 6, 3))]
    rows.append({"season": 2024, "week": 5, "gsis_id": "c", "position": "WR", "team": "AAA", "ftn_usable": True,
                 "p_targets": 1, "p_first": 1, "p_des": 0, "p_chk": 0, "t_targets": 30, "t_first": 15})
    f = us.ftn_features(pl.DataFrame(rows), 2024, 6, pl.DataFrame({"gsis_id": ["a", "c"], "position": ["WR", "WR"]}))
    own = dict(zip(f["gsis_id"], f["fr_own_share"]))
    raw_c = 1.0
    mean_own = (3 * 10 + 1) / (6 * 10 + 1)
    assert mean_own < own["c"] < raw_c          # one 1-for-1 game is pulled most of the way back


# ----------------------------------------------------------------------------- depth charts


def _snap(dt: datetime, team: str, rows: list[tuple[str, str, int]]) -> list[dict]:
    return [{"dt": dt, "team": team, "position": pos, "gsis_id": g, "rank": r} for g, pos, r in rows]


def _games(kickoffs: dict[tuple[int, str], datetime], season=2025) -> pl.DataFrame:
    return pl.DataFrame([{"season": season, "week": w, "team": t, "kickoff": k} for (w, t), k in kickoffs.items()])


KO1, KO2 = datetime(2025, 9, 7, 17, tzinfo=UTC), datetime(2025, 9, 14, 17, tzinfo=UTC)


def test_point_in_time_snapshot_never_uses_a_later_one():
    snaps = pl.DataFrame(
        _snap(KO1 - timedelta(days=2), "AAA", [("rb1", "RB", 1), ("rb2", "RB", 2)])
        + _snap(KO1 - timedelta(hours=1), "AAA", [("rb2", "RB", 1), ("rb1", "RB", 2)])     # inside the 1.5 h margin
        + _snap(KO1 + timedelta(hours=3), "AAA", [("rb3", "RB", 1)]))                       # after kickoff
    games = _games({(1, "AAA"): KO1})
    targets = pl.DataFrame({"season": [2025, 2025], "week": [1, 1], "gsis_id": ["rb1", "rb2"], "position": ["RB", "RB"],
                            "team": ["AAA", "AAA"]})
    f, _ = us.depth_features(snaps, games, targets, cutoff_hours=1.5)
    assert dict(zip(f["gsis_id"], f["dc_tier"])) == {"rb1": 1.0, "rb2": 2.0}
    assert (f["snap_dt"] < f["dc_cutoff"]).all()
    # with no margin the 1-hour-before snapshot is pregame and is used
    f0, _ = us.depth_features(snaps, games, targets, cutoff_hours=0)
    assert dict(zip(f0["gsis_id"], f0["dc_tier"])) == {"rb1": 2.0, "rb2": 1.0}
    # a snapshot exactly at the cutoff is NOT before it
    exact = pl.DataFrame(_snap(KO1 - timedelta(hours=1.5), "AAA", [("rb1", "RB", 1)]))
    assert us.depth_features(exact, games, targets, cutoff_hours=1.5)[0]["dc_tier"].to_list() == [None, None]
    # stale snapshot (> MAX_STALE_DAYS) is not used
    stale = pl.DataFrame(_snap(KO1 - timedelta(days=us.MAX_STALE_DAYS + 1), "AAA", [("rb1", "RB", 1)]))
    assert us.depth_features(stale, games, targets, 1.5)[0]["dc_tier"].to_list() == [None, None]


def test_change_features_compare_with_the_previous_games_pregame_snapshot():
    snaps = pl.DataFrame(
        _snap(KO1 - timedelta(days=1), "AAA", [("rb1", "RB", 1), ("rb2", "RB", 2), ("rb3", "RB", 3)])
        + _snap(KO2 - timedelta(days=1), "AAA", [("rb2", "RB", 1), ("rb3", "RB", 2)]))     # rb1 left the chart
    games = _games({(1, "AAA"): KO1, (2, "AAA"): KO2})
    targets = pl.DataFrame({"season": [2025] * 2, "week": [2, 2], "gsis_id": ["rb2", "rb3"], "position": ["RB", "RB"],
                            "team": ["AAA", "AAA"]})
    f, _ = us.depth_features(snaps, games, targets, 1.5)
    r = {g: row for g, row in zip(f["gsis_id"], f.iter_rows(named=True))}
    assert r["rb2"]["dc_tier"] == 1 and r["rb2"]["dc_tier_change"] == -1 and r["rb2"]["dc_promoted"] == 1
    assert r["rb2"]["dc_vacancy"] == 1                 # rb1, ahead of him, is gone
    assert r["rb3"]["dc_tier"] == 2 and r["rb3"]["dc_tier_change"] == -1 and r["rb3"]["dc_promoted"] == 0
    assert r["rb3"]["dc_vacancy"] == 1                 # rb1 gone (rb2 still ahead)


def test_team_change_uses_the_forecast_team_chart():
    snaps = pl.DataFrame(_snap(KO1 - timedelta(days=1), "AAA", [("wr9", "WR", 1)])
                         + _snap(KO1 - timedelta(days=1), "BBB", [("wr1", "WR", 1)]))
    games = _games({(1, "AAA"): KO1, (1, "BBB"): KO1})
    # wr9 was traded to BBB: AAA's stale chart still lists him, BBB's does not yet
    targets = pl.DataFrame({"season": [2025], "week": [1], "gsis_id": ["wr9"], "position": ["WR"], "team": ["BBB"]})
    f, audit = us.depth_features(snaps, games, targets, 1.5)
    assert f["dc_off_chart"].to_list() == [1.0] and f["dc_tier"].to_list() == [3.0]
    assert audit["listed_only_by_other_team"] == 1


def test_incorrect_identity_is_null_not_a_rank():
    snaps = pl.DataFrame(_snap(KO1 - timedelta(days=1), "AAA", [("p1", "TE", 1)]))
    games = _games({(1, "AAA"): KO1})
    targets = pl.DataFrame({"season": [2025], "week": [1], "gsis_id": ["p1"], "position": ["WR"], "team": ["AAA"]})
    f, audit = us.depth_features(snaps, games, targets, 1.5)
    assert f["dc_tier"].to_list() == [None] and audit["identity_conflicts"] == 1
    # rows without a gsis id are dropped and counted; duplicates keep the best rank
    raw = pl.DataFrame({"dt": ["2025-09-06T07:00:00Z"] * 4, "team": ["AAA"] * 4, "pos_grp": ["3WR 1TE"] * 4,
                        "pos_abb": ["WR", "WR", "WR", "LT"], "gsis_id": ["w1", None, "w1", "ol"],
                        "pos_slot": [1, 2, 8, 3], "pos_rank": [4, 1, 2, 1]})
    norm, a = us.normalize_modern(raw)
    assert a["skill_rows_without_gsis"] == 1
    assert norm.select("gsis_id", "rank").to_dicts() == [{"gsis_id": "w1", "rank": 1}]


def test_tiers_follow_starter_counts():
    df = pl.DataFrame({"position": ["WR"] * 7 + ["RB"] * 3, "rank": [1, 2, 3, 4, 6, 7, 9, 1, 2, 3]})
    assert df.select(us.tier_expr().alias("t"))["t"].to_list() == [1, 1, 1, 2, 2, 3, 3, 1, 2, 3]


# ----------------------------------------------------------------------------- labels, scoring, selection


def test_outcome_labels_measured_zero_and_unknown():
    panel = pl.DataFrame({"season": [2024] * 5, "week": [1, 2, 3, 4, 4], "gsis_id": ["a", "a", "a", "a", "b"],
                          "team": ["T"] * 5, "targets": [5.0, 6, 4, 0, 3], "carries": [0.0] * 5,
                          "pass_attempts": [0.0] * 5, "offense_pct": [0.8, 0.8, 0.7, 0.3, 0.5],
                          "xfpl": [7.0, 8.0, 6.0, None, 4.0]})
    store = pl.DataFrame({"season": [2024, 2024], "week": [4, 4], "gsis_id": ["a", "b"], "label": [1.0, 2.0]})
    out = us.outcome_labels(store, panel).sort("gsis_id")
    a, b = out.row(0, named=True), out.row(1, named=True)
    assert a["y_opp"] == 0.0 and a["y_targets"] == 0          # covered team-week, no opportunity: measured zero
    assert a["y_role_down"] == 1.0 and a["y_role_up"] == 0.0  # 0.3 vs reference ~0.77
    assert b["y_opp"] == 4.0 and b["y_role_down"] is None      # no reference games
    assert out["label"].to_list() == [1.0, 2.0]                # league-scored label passes through untouched
    # opportunities but no expectation: unknown, never zero
    p2 = panel.with_columns(pl.when(pl.col("week") == 4).then(None).otherwise(pl.col("xfpl")).alias("xfpl"))
    out2 = us.outcome_labels(store, p2.with_columns(pl.lit("U").alias("team")).filter(pl.col("week") == 4))
    assert out2.sort("gsis_id")["y_opp"].to_list() == [None, None]


def test_scoring_mismatch_refuses_the_selection():
    with pytest.raises(ValueError, match="different league scoring"):
        us.check_selection({"scoring_fingerprint": "aaaa"}, "e165b59686110c7d")
    assert us.check_selection({"scoring_fingerprint": "e165b59686110c7d"}, "e165b59686110c7d")


# ----------------------------------------------------------------------------- snapshots & caches


def test_source_snapshot_correction_keeps_history(tmp_path):
    root = tmp_path / "raw"
    v1 = pl.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    e1 = us.write_snapshot(root, "ftn", 2025, v1, datetime(2025, 9, 10, tzinfo=UTC))
    fp1 = us.manifest_fingerprint(root, ("ftn",))
    assert us.write_snapshot(root, "ftn", 2025, v1.reverse())["sha"] == e1["sha"]       # row order is not content
    assert len(us.load_manifest(root)["history"]) == 0
    v2 = pl.DataFrame({"a": [1, 3], "b": ["x", "y"]})                                    # upstream correction
    e2 = us.write_snapshot(root, "ftn", 2025, v2, datetime(2025, 9, 20, tzinfo=UTC))
    assert e2["sha"] != e1["sha"] and (root / e1["file"]).exists() and (root / e2["file"]).exists()
    m = us.load_manifest(root)
    assert m["history"][0]["sha"] == e1["sha"] and m["current"]["ftn_2025"]["sha"] == e2["sha"]
    assert us.manifest_fingerprint(root, ("ftn",)) != fp1
    assert us.read_snapshot(root, "ftn", 2025).equals(v2)
    # a tampered file is refused
    v2.with_columns(pl.lit(9).alias("a")).write_parquet(root / e2["file"])
    with pytest.raises(ValueError, match="content hash"):
        us.read_snapshot(root, "ftn", 2025)


def test_research_cache_invalidates_when_any_input_changes():
    base = us.signal_cache_fingerprint("store1", "raw1", "empirical", 1.5)
    assert base == us.signal_cache_fingerprint("store1", "raw1", "empirical", 1.5)
    for changed in (("store2", "raw1", "empirical", 1.5), ("store1", "raw2", "empirical", 1.5),
                    ("store1", "raw1", "dictionary", 1.5), ("store1", "raw1", "empirical", 72.0)):
        assert us.signal_cache_fingerprint(*changed) != base
    a = pl.DataFrame({"x": [1, 2, 3]})
    assert us.frame_hash(a) == us.frame_hash(a.reverse()) and us.frame_hash(a) != us.frame_hash(a.head(2))
    assert us.frame_hash(pl.DataFrame({"x": [1]})) != us.frame_hash(pl.DataFrame({"y": [1]}))


# ----------------------------------------------------------------------------- experiments & isolation


def _store(weeks=8, players=80, seed=5) -> pl.DataFrame:
    rng = random.Random(seed)
    rows = []
    for w in range(1, weeks + 1):
        for i in range(players):
            x, z = rng.uniform(0, 10), rng.uniform(-1, 1)
            rows.append({"season": 2025, "week": w, "key": 202500 + w, "gsis_id": f"p{i}", "position": "WR",
                         "games_recent": 0 if i == 0 else 3, "x": x, "z": None if i % 5 == 0 else z,
                         "label": 1.2 * x + 2 * (z if i % 5 else 0) + rng.gauss(0, 1),
                         "y_role_down": float(rng.random() < 0.2 + 0.1 * (z if i % 5 else 0))})
    return pl.DataFrame(rows)


def test_walk_forward_trains_on_earlier_weeks_and_abstains_without_history():
    s = _store()
    p = us.walk(s, [(2025, 8)], "WR", us.FitSpec(("x", "z"), 0.01, None), "label")
    assert p.height == 80 and p.filter(pl.col("gsis_id") == "p0")["pred"].to_list() == [None]
    # the target week's labels are not used
    s2 = s.with_columns(pl.when(pl.col("week") == 8).then(999.0).otherwise(pl.col("label")).alias("label"))
    assert p.equals(us.walk(s2, [(2025, 8)], "WR", us.FitSpec(("x", "z"), 0.01, None), "label"))
    # identical to the ifp-2 estimator on the same rows and features
    m = ch.fit_linear(s.filter((pl.col("key") < 202508) & (pl.col("games_recent") > 0)), "WR", ["x", "z"], 0.01,
                      target_key=202508)
    rows = s.filter((pl.col("key") == 202508) & (pl.col("games_recent") > 0))
    np.testing.assert_allclose(m.predict(rows), p.filter(pl.col("pred").is_not_null())["pred"].to_numpy())
    lg = us.walk(s, [(2025, 8)], "WR", us.FitSpec(("x", "z"), 0.01, None), "y_role_down", kind="logistic")
    v = lg["pred"].drop_nulls().to_numpy()
    assert ((v > 0) & (v < 1)).all()


def test_comparison_uses_one_shared_population():
    df = pl.DataFrame({"key": [1, 1, 2, 2, 3], "label": [1.0, 2, 3, 4, 5], "a": [1.0, 2, 3, None, 5],
                       "b": [1.5, None, 3, 4, 5], "cov": [True, True, False, True, True]})
    r = us.compare_regression(df, "label", {"a": "a", "b": "b"}, "a", pl.col("cov"), resamples=50)
    assert r["population"] == 3 and r["abstained_or_unlabeled"] == 2
    assert r["all"]["metrics"]["a"]["n"] == r["all"]["metrics"]["b"]["n"] == 3
    assert r["covered"]["n"] + r["uncovered"]["n"] == 3


def test_classification_metrics():
    m = us.classification_metrics(np.array([0, 0, 1, 1.0]), np.array([0.1, 0.4, 0.35, 0.8]))
    assert m["auc"] == 0.75 and m["n"] == 4


def test_decision_requires_development_gain_and_no_replay_damage():
    def rep(d, lo, hi):
        return {"all": {"minus_baseline_mae": {"A": {"diff": d, "ci95": [lo, hi]}}}}
    dev = {"targets": rep(-0.02, -0.03, -0.01), "opp": rep(-0.01, -0.02, 0.0), "points": rep(-0.002, -0.01, 0.005)}
    ok = {"points": rep(0.001, -0.01, 0.01), "opp": rep(0.0, -0.01, 0.01)}
    assert us.decision(dev, ok, "A")["outcome"] == "continue_to_prospective_shadow"
    assert us.decision(dev, {**ok, "points": rep(0.02, 0.01, 0.03)}, "A")["outcome"] == "reject"
    assert us.decision({**dev, "targets": rep(-0.02, -0.03, 0.001)}, ok, "A")["outcome"] == "reject"
    assert us.decision({}, {}, "A")["outcome"] == "reject"


def test_frozen_ifp2_configuration_and_groups_are_unchanged():
    cfg = ch.ChallengerConfig.load()
    assert cfg.version == "ifp-2.0.0"
    assert cfg.hash() == "209809f9e63e88dd"
    for g in us.GROUPS.values():
        assert not {"label", "y_targets", "y_opp", *us.ROLE_LABELS} & set(g["features"])
        assert set(g["features"]) <= set(us.FTN_FEATURES) | set(us.DEPTH_FEATURES)
    for pos in ch.POSITIONS:
        assert not set(cfg.features(pos)) & (set(us.FTN_FEATURES) | set(us.DEPTH_FEATURES))


def test_research_module_is_never_imported_by_the_app():
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    for path in root.rglob("*.py"):
        if path.name == "usage_signals.py":
            continue
        assert "usage_signals" not in path.read_text(), path
