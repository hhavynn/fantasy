"""Pass-direction profile (issue #198 step 2, research only): cells and unknowns, point-in-time predictions, shrinkage,
the >= MIN_CELL abstention (unadjusted, never 0) and the pre-registered decision rule, on synthetic data."""
import hashlib
import pathlib
import random
import subprocess
import sys

import polars as pl
import pytest

from app.analytics import pass_direction as pd_


def _pbp(rows):
    base = {"season_type": "REG", "two_point_attempt": 0, "pass_attempt": 1, "sack": 0, "play_type": "pass"}
    return pl.DataFrame([{**base, **r} for r in rows])


def test_cells_and_unknowns_are_counted_never_assigned():
    pbp = _pbp([
        {"season": 2024, "week": 1, "game_id": "g", "posteam": "A", "defteam": "B", "pass_location": "left", "pass_length": "short"},
        {"season": 2024, "week": 1, "game_id": "g", "posteam": "A", "defteam": "B", "pass_location": None, "pass_length": "deep"},
        {"season": 2024, "week": 1, "game_id": "g", "posteam": "A", "defteam": "B", "pass_location": "right", "pass_length": "deep", "sack": 1},
        {"season": 2024, "week": 1, "game_id": "g", "posteam": "A", "defteam": "B", "pass_location": "middle", "pass_length": "deep", "two_point_attempt": 1},
        {"season": 2024, "week": 1, "game_id": "g", "posteam": "A", "defteam": "B", "pass_location": "middle", "pass_length": "deep", "season_type": "POST"},
    ])
    att, counts = pd_.attempts(pbp)
    assert att["cell"].to_list() == ["left_short"]
    assert counts == {"pass_attempts": 2, "known": 1, "unknown_cell": 1}
    assert len(pd_.CELLS) == 6


def _world(n_teams=8, weeks=18, per_game=40, def_effect=0.0, seed=1, season=2024):
    """Round-robin-ish schedule. Every offense throws from a team-specific mix; defenses with def_effect>0 push throws
    into one favourite cell (a real allowed tendency)."""
    rng = random.Random(seed)
    teams = [f"T{i}" for i in range(n_teams)]
    off_mix = {t: [rng.random() + 0.5 for _ in pd_.CELLS] for t in teams}
    def_fav = {t: rng.randrange(len(pd_.CELLS)) for t in teams}
    rows = []
    for w in range(1, weeks + 1):
        order = teams[:]
        rng.shuffle(order)
        for i in range(0, n_teams, 2):
            a, b = order[i], order[i + 1]
            for pos, dfn in ((a, b), (b, a)):
                mix = list(off_mix[pos])
                mix[def_fav[dfn]] *= 1 + def_effect
                for _ in range(per_game):
                    cell = rng.choices(pd_.CELLS, weights=mix)[0]
                    rows.append({"season": season, "week": w, "game_id": f"{season}_{w}_{a}_{b}", "posteam": pos,
                                 "defteam": dfn, "cell": cell})
    return pl.DataFrame(rows)


def test_predictions_are_point_in_time():
    att = _world()
    p = pd_.predict(att, seasons=(2024, 2024), weeks=(6, 6))
    later = att.with_columns(pl.when(pl.col("week") >= 6).then(pl.lit("left_deep")).otherwise(pl.col("cell")).alias("cell"))
    q = pd_.predict(later, seasons=(2024, 2024), weeks=(6, 6))
    cols = [c for c in p.columns if c.startswith(("league_", "offense_", "combined_"))]
    assert p.select(cols).equals(q.select(cols))            # week 6+ results never feed week 6's predictions
    assert p["week"].unique().to_list() == [6]


def test_shares_are_shrunk_toward_league_and_never_zero():
    lg = dict.fromkeys(pd_.CELLS, 1 / 6)
    counts = dict.fromkeys(pd_.CELLS, 0)
    counts["left_short"] = 100
    s = pd_.shrunk_share(counts, lg)
    assert s["left_short"] == pytest.approx((100 + pd_.SHRINK / 6) / (100 + pd_.SHRINK))
    assert all(v > 0 for v in s.values()) and sum(s.values()) == pytest.approx(1)
    assert pd_.league_share(pl.DataFrame({"cell": ["left_short"]}))["right_deep"] > 0


def test_defense_adjustment_abstains_below_min_cell_and_never_fills_zero():
    lg = dict.fromkeys(pd_.CELLS, 1 / 6)
    off = {c: v for c, v in zip(pd_.CELLS, (0.3, 0.2, 0.2, 0.1, 0.1, 0.1))}
    dfs = {c: v for c, v in zip(pd_.CELLS, (0.5, 0.1, 0.1, 0.1, 0.1, 0.1))}
    few = dict.fromkeys(pd_.CELLS, pd_.MIN_CELL - 1)
    comb, n = pd_.combined_share(off, dfs, few, lg)
    assert n == 0 and comb == pytest.approx(off)                      # unadjusted, exactly the offense share
    some = {**few, "left_short": pd_.MIN_CELL}
    comb, n = pd_.combined_share(off, dfs, some, lg)
    assert n == 1 and comb["left_short"] > off["left_short"]
    assert all(v > 0 for v in comb.values()) and sum(comb.values()) == pytest.approx(1)


def test_a_real_defense_tendency_passes_and_no_tendency_fails():
    real = pd_.evaluate(pd_.predict(_world(def_effect=1.5, seed=3), seasons=(2024, 2024)))
    assert real["decision"] == "pass"
    assert real["gaps"]["combined_minus_offense"]["difference"] < 0
    assert real["abstention"]["mean_adjusted_cells"] > 0
    none = pd_.evaluate(pd_.predict(_world(def_effect=0.0, seed=3), seasons=(2024, 2024)))
    assert none["decision"] == "fail"


def test_intervals_are_clustered_two_ways_seeded_and_the_rule_is_hashed():
    out = pd_.evaluate(pd_.predict(_world(def_effect=1.5, seed=3), seasons=(2024, 2024)))
    g = out["gaps"]["combined_minus_offense"]
    assert g["by_week"]["clusters"] == 14 and g["by_defense_season"]["clusters"] == 8
    assert pd_.evaluate(pd_.predict(_world(def_effect=1.5, seed=3), seasons=(2024, 2024)))["gaps"] == out["gaps"]
    assert out["decision_rule_sha256"] == hashlib.sha256(pd_.DECISION_RULE.encode()).hexdigest()
    assert "no defender is named or paired" in out["label"]
    assert set(out["diagnostics"]["game_share_mae"]) == {"league", "offense", "combined"}


def test_passes_needs_both_clusterings():
    ok, no = {"ci95": [-0.2, -0.1]}, {"ci95": [-0.2, 0.01]}
    assert pd_._passes({"by_week": ok, "by_defense_season": ok})
    assert not pd_._passes({"by_week": ok, "by_defense_season": no})
    assert not pd_._passes({"by_week": no, "by_defense_season": ok})


def test_no_data_says_so():
    assert pd_.evaluate(pl.DataFrame())["decision"] == "no_data"


def test_research_module_is_never_imported_by_the_app_and_writes_nothing():
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    for path in root.rglob("*.py"):
        if path.name != "pass_direction.py":
            assert "pass_direction" not in path.read_text(), path
    code = "import sys, app.main; assert 'app.analytics.pass_direction' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], cwd=root.parent, check=True)
    text = pathlib.Path(pd_.__file__).read_text()
    for word in ("INSERT", "UPDATE ", "write_parquet", "write_csv", "open(", "connect("):
        assert word not in text, word
