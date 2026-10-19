"""Rest-of-season availability research (ros-avail-0.1.0): the pre-registered rules, the candidate p_play for
players injured at the cutoff, the evaluation rows, the metrics and the verdict, and isolation from production.

Hand-built frames with invented values (the backfield world of test_injury_workload)."""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import numpy as np
import polars as pl
import pytest

from app.analytics import ros_availability as ra

from test_injury_workload import BACKUP, S, STAR, inj, report, world

# Pre-registration: the rules were committed before the first evaluation run. Changing them must be deliberate
# (and documented as a new version), so the hash is pinned here.
FROZEN_RULES_SHA = "db1ad0df05ec5216"
FROZEN_NEXT_SHA = "f3e0772c4317ca39"   # ros-avail-0.1.1, pre-registered for prospective weeks only


def test_rules_are_frozen():
    assert ra.rules_sha() == FROZEN_RULES_SHA
    assert set(ra.DECISION_RULES["gates_per_horizon"]) == {"c1_brier", "c2_ece", "c3_max_bin_gap", "c4_ece_not_worse",
                                                            "c5_each_season", "c6_subgroups"}
    assert set(ra.DECISION_RULES["gates_ros_totals"]) >= {"r1_mae", "r2_bias"}
    assert ra.GATED_HORIZONS == (1, 2, 3, 4) and ra.TEST_SEASONS == (2023, 2024)
    assert "owner approval" in ra.DECISION_RULES["never"]
    assert ra.next_candidate_sha() == FROZEN_NEXT_SHA
    assert "prospective" in ra.NEXT_CANDIDATE["evaluation"]


# ----------------------------------------------------------------------------- candidate


def test_table_is_refit_per_season_from_earlier_seasons_only():
    src = world(range(1, 8), skip={STAR: (3,)}, injuries=[inj(STAR, "Out", 3), *report(3)])
    assert ra.table_for_season(src, S) == {}                 # season S episodes never feed a season-S forecast
    nxt = ra.table_for_season(src, S + 1)
    assert nxt[("ALL", "ALL", 1)] == {"rate": 1.0, "n": 1}   # STAR played week 4
    assert ra.table_for_season(src, S + 1, first_season=S + 1) == {}


def test_team_game_index_skips_byes():
    weeks = [1, 2, 3, 5, 6, 7]                               # bye in week 4
    assert ra.team_game_index(weeks, 3, 5) == 1
    assert ra.team_game_index(weeks, 3, 7) == 3
    assert ra.team_game_index(weeks, 3, 4) is None


def test_replacement_uses_the_fitted_cell_then_pools_and_reuses_h4():
    rates = {("RB", "1", 1): {"rate": 0.2, "n": 10}, ("ALL", "1", 1): {"rate": 0.35, "n": 200},
             ("RB", "0", 2): {"rate": 0.55, "n": 80}, ("RB", "0", 4): {"rate": 0.62, "n": 60},
             ("ALL", "ALL", 4): {"rate": 0.6, "n": 900}}
    assert ra.replacement_p(rates, "RB", 1, 1) == 0.35      # thin position cell -> pooled over positions
    assert ra.replacement_p(rates, "RB", 0, 2) == 0.55
    assert ra.replacement_p(rates, "RB", 0, 7) == 0.62      # h > 4 uses the h = 4 cell
    with pytest.raises(ValueError):
        ra.replacement_p({}, "RB", 0, 1)                     # never a guessed number


def test_only_players_out_or_doubtful_at_the_cutoff_are_replaced():
    rates = {("ALL", "ALL", h): {"rate": 0.4, "n": 500} for h in range(1, 5)}
    kw = {"position": "RB", "missed_team_games": 0, "h": 1, "rates": rates}
    assert ra.ros_p_play(0.93, designation="Out", **kw) == (0.4, "return_rate_table")
    assert ra.ros_p_play(0.93, designation="doubtful", **kw) == (0.4, "return_rate_table")
    for d in ("questionable", "listed", "not_listed", None):
        assert ra.ros_p_play(0.93, designation=d, **kw) == (0.93, "participation_undesignated")
    assert ra.ros_p_play(0.93, designation="out", **{**kw, "h": 0})[1] == "participation_undesignated"
    assert ra.ros_p_play(0.93, designation="out", **{**kw, "position": "QB"})[1] == "participation_undesignated"


# ----------------------------------------------------------------------------- evaluation rows


def test_cohort_scores_identical_rows_with_actual_outcomes():
    """STAR Out at week 5, misses weeks 5-6, back in week 7. Weeks 1-7 final, week 8 scheduled."""
    src = world(range(1, 8), last_week=8, skip={STAR: (5, 6)},
                injuries=[*report(5, inj(STAR, "Out", 5)), *report(6, inj(STAR, "Out", 6))])
    rates = {("ALL", "ALL", h): {"rate": 0.3 + 0.1 * h, "n": 500} for h in range(1, 5)}
    calls = []

    def current(w, t):
        calls.append((w, t))
        return {STAR: 0.9, BACKUP: 0.95}

    df = ra.cohort(src, S, current, rates, cutoffs=(5, 6))
    rows = {(r["cutoff_week"], r["h"]): r for r in df.iter_rows(named=True)}
    assert set(df["gsis_id"]) == {STAR}                      # only the player Out at the cutoff
    assert rows[(5, 1)]["target_week"] == 6 and rows[(5, 1)]["appeared"] == 0 and rows[(5, 1)]["points"] == 0.0
    assert rows[(5, 2)]["target_week"] == 7 and rows[(5, 2)]["appeared"] == 1 and rows[(5, 2)]["points"] > 0
    assert rows[(5, 2)]["p_candidate"] == pytest.approx(0.5) and rows[(5, 2)]["p_current"] == 0.9
    assert (5, 3) not in rows                                # week 8 is not final: outcome unknown, not scored
    assert rows[(6, 1)]["missed_team_games"] == 1 and rows[(6, 1)]["missed_bucket"] == "1"
    assert all(w < t for w, t in calls)


def test_cohort_skips_players_the_current_path_does_not_cover():
    src = world(range(1, 8), skip={STAR: (5, 6)}, injuries=report(5, inj(STAR, "Out", 5)))
    rates = {("ALL", "ALL", h): {"rate": 0.4, "n": 500} for h in range(1, 5)}
    assert ra.cohort(src, S, lambda w, t: {}, rates, cutoffs=(5,)).height == 0


def test_non_injury_absences_are_not_in_the_table_population():
    """fit_return_rates drops not_injury rows, so a personal absence designated Out is neither scored nor replaced."""
    personal = {**inj(STAR, "Out", 5, injury="Personal"), "not_injury": True}
    src = world(range(1, 8), skip={STAR: (5,)}, injuries=report(5, personal))
    rates = {("ALL", "ALL", h): {"rate": 0.4, "n": 500} for h in range(1, 5)}
    assert ra.non_injury_ids(src, S, 5) == {STAR}
    assert ra.cohort(src, S, lambda w, t: {STAR: 0.9}, rates, cutoffs=(5,)).height == 0
    assert ra.ros_p_play(0.9, designation="out", position="RB", missed_team_games=0, h=1, rates=rates,
                         not_injury=True) == (0.9, "participation_undesignated")


def test_a_player_traded_in_the_cutoff_week_follows_his_new_teams_schedule():
    """STAR's roster rows before week 5 say AAA; the week-5 roster (the current path's snapshot) says CCC. AAA is on
    a bye in week 6, CCC is not: his first game after the cutoff is CCC's week 6."""
    src = world(range(1, 8), roster={(STAR, 5): ("CCC", "ACT")}, byes={"AAA": 6},
                injuries=report(5, inj(STAR, "Out", 5, team="CCC")))
    assert ra.cutoff_teams(src, S, 5)[STAR] == "CCC" and ra.cutoff_teams(src, S, 4)[STAR] == "AAA"
    rates = {("ALL", "ALL", h): {"rate": 0.4, "n": 500} for h in range(1, 5)}
    df = ra.cohort(src, S, lambda w, t: {STAR: 0.9}, rates, cutoffs=(5,))
    first = df.filter(pl.col("h") == 1).to_dicts()[0]
    assert first["team"] == "CCC" and first["target_week"] == 6


def test_cohort_counts_byes_as_no_game():
    src = world(range(1, 8), skip={STAR: (5,)}, byes={"AAA": 6}, injuries=report(5, inj(STAR, "Out", 5)))
    rates = {("ALL", "ALL", h): {"rate": 0.4, "n": 500} for h in range(1, 5)}
    df = ra.cohort(src, S, lambda w, t: {STAR: 0.9}, rates, cutoffs=(5,))
    assert df.filter(pl.col("h") == 1)["target_week"].to_list() == [7]


# ----------------------------------------------------------------------------- metrics and verdict


def test_calibration_metrics():
    y = np.array([1, 0, 1, 0, 1, 0, 1, 0, 1, 0] * 10)
    good = ra.calibration(y, np.full(100, 0.5))
    bad = ra.calibration(y, np.full(100, 0.9))
    assert good["brier"] == pytest.approx(0.25) and good["ece"] == pytest.approx(0.0)
    assert bad["ece"] == pytest.approx(0.4) and bad["max_bin_gap"] == pytest.approx(0.4)
    assert bad["brier"] > good["brier"]


def test_cluster_diff_resamples_whole_cutoff_weeks():
    df = pl.DataFrame({"key": [1, 1, 2, 2, 3, 3], "a": [0.5] * 6, "b": [0.9] * 6, "y": [1, 0, 1, 0, 0, 0]})
    d = ra.cluster_diff(df, "a", "b", "y")
    assert d["diff"] < 0 and d["ci95"][0] <= d["diff"] <= d["ci95"][1] and d["n"] == 6


def _frame(p_cur, p_cand, y, seasons=(2023, 2024), h=(1, 2, 3, 4)):
    rows = []
    rng = np.random.default_rng(0)
    for s in seasons:
        for hh in h:
            for i in range(400):
                outcome = int(rng.random() < y)
                rows.append({"season": s, "cutoff_week": 2 + i % 16, "key": s * 100 + 2 + i % 16, "gsis_id": f"g{i}",
                             "position": ("RB", "WR", "TE")[i % 3], "team": "AAA", "designation": "out",
                             "missed_team_games": 0, "missed_bucket": "0", "h": hh, "target_week": 3 + i % 16 + hh,
                             "p_current": p_cur, "p_candidate": p_cand, "appeared": outcome,
                             "points": 10.0 * outcome, "conditional": 10.0})
    return pl.DataFrame(rows)


def test_gates_pass_for_a_calibrated_candidate_and_fail_for_a_worse_one():
    df = _frame(0.9, 0.4, 0.4)
    hr = {h: ra.horizon_report(df, h) for h in ra.GATED_HORIZONS}
    tot = ra.totals_report(ra.ros_totals(df))
    v = ra.gates(hr, tot)
    assert v["outcome"] == "propose_for_owner_review" and v["failed"] == []
    assert tot["candidate"]["mean_total"] == pytest.approx(16.0) and tot["current"]["mean_total"] == pytest.approx(36.0)
    worse = _frame(0.4, 0.9, 0.4)
    v2 = ra.gates({h: ra.horizon_report(worse, h) for h in ra.GATED_HORIZONS}, ra.totals_report(ra.ros_totals(worse)))
    assert v2["outcome"] == "retain_current" and "h1.c1_brier" in v2["failed"] and "ros_totals.r1_mae" in v2["failed"]


def test_ros_totals_need_every_game_and_a_conditional():
    df = _frame(0.9, 0.4, 0.4, seasons=(2023,), h=(1, 2, 3))
    assert ra.ros_totals(df).height == 0                     # only three of the four games
    df = _frame(0.9, 0.4, 0.4, seasons=(2023,)).with_columns(
        pl.when(pl.col("gsis_id") == "g0").then(None).otherwise(pl.col("conditional")).alias("conditional"))
    assert "g0" not in set(ra.ros_totals(df)["gsis_id"])


# ----------------------------------------------------------------------------- isolation


def test_research_module_writes_nothing():
    text = pathlib.Path(ra.__file__).read_text().upper()
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE TABLE", "WRITE_PARQUET", "WRITE_TEXT"):
        assert verb not in text, verb


def test_app_never_imports_the_ros_availability_research():
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    for path in root.rglob("*.py"):      # imports, not text: the production model cites the study by name
        if path.name != "ros_availability.py":
            tree = ast.parse(path.read_text())
            names = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
                {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
            assert not {x for x in names if x.endswith("ros_availability")}, path
    code = ("import sys, app.main; bad = 'app.analytics.ros_availability' in sys.modules; print(bad); "
            "sys.exit(1 if bad else 0)")
    assert subprocess.run([sys.executable, "-c", code], cwd=root.parent, capture_output=True).returncode == 0
