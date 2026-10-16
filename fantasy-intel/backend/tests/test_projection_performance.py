"""Published projection-performance report: the offline build (scripts/projection_performance.py), the write-once
store and the hosted reader (app.analytics.performance_report), the API route, and the synthetic fixture the frontend
tests read. Synthetic data only (the espn_benchmark test world); nothing here is a real result."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.analytics import espn_benchmark as eb
from app.analytics import performance_report as pr
from app.analytics import projection_source as ps
from app.analytics import start_sit_evaluation as sse
from app.analytics.forecast_backtest import PROMOTION_CRITERIA
from app.db.connection import init_db

from test_espn_benchmark import ESPN_ID, PLAYERS, SCORING, kickoff, make_world, naive
from test_start_sit_evaluation import cand

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
BACKEND = Path(__file__).resolve().parents[1]
FIXTURE = BACKEND.parent / "frontend" / "lib" / "fixtures" / "projection-performance.synthetic.json"
PROD, FALLBACK = ps.MODEL_VERSION, ps.FALLBACK_VERSION
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
UTC = timezone.utc

sys.path.insert(0, str(SCRIPTS))
pp = importlib.import_module("projection_performance")


def build(con, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("scoring", SCORING)
    kw.setdefault("config", eb.BenchmarkConfig(resamples=200))
    return pp.build_report(con, 2026, **kw)


def reader(version, weeks=None, *, shift=0.0, mutate=None):
    """A forecast reader over the world's (ifp-2-labelled) legacy runs, relabelled to `version`, limited to `weeks`,
    optionally shifted or mutated per row (None drops the row)."""
    def read(con, season, weeks_=None, *, model_version, scoring_fingerprint):
        rows, ex = eb.legacy_forecast_reader(con, season, weeks_, model_version=PROD,
                                             scoring_fingerprint=scoring_fingerprint)
        out = []
        for r in rows:
            if weeks is not None and r["week"] not in weeks:
                continue
            r = {**r, "model_version": version, "expected_points": r["expected_points"] + shift,
                 "projected_points": r["projected_points"] + shift / 0.9}
            r = mutate(r) if mutate else r
            if r is not None:
                out.append(r)
        return out, ex
    return read


POS_OF = {pid: pos for pid, _, _, pos in PLAYERS}


@pytest.fixture
def gate(monkeypatch):
    """A lowered per-position minimum so the small synthetic world can reach a verdict (the real bar is 300)."""
    monkeypatch.setitem(PROMOTION_CRITERIA, "espn_min_matched_per_position", 30)


# ----------------------------------------------------------------------------- empty and insufficient data


def test_empty_dataset_publishes_an_all_insufficient_report(tmp_path):
    con = make_world(tmp_path / "e.duckdb")
    con.execute("DELETE FROM espn_projection_history")
    con.execute("DELETE FROM independent_player_projections")
    p = build(con)
    assert p["weeks"]["with_espn_history"] == [] and p["weekly"] == []
    for pos, s in p["positions"].items():
        assert s["verdict"] == "insufficient_evidence" and s["statement"] == "Insufficient prospective evidence."
        assert s["paired_n"] == 0 and s["mae_diff"] is None and s["ci95"] is None
    assert p["status"]["verdict"] == "insufficient_evidence"
    assert p["start_sit"]["sets"] == 0 and p["start_sit"]["models"] == {}
    assert p["start_sit"]["warnings"] == ["No pregame decision set has been recorded yet, so there is no Start/Sit evidence."]
    assert pr.validate(p, season=2026) is None


def test_the_real_gate_keeps_every_small_position_insufficient(tmp_path):
    """300 matched observations per position: 6 final weeks of 8 QBs is far short, whatever the errors say."""
    p = build(make_world(tmp_path / "w.duckdb"))
    assert PROMOTION_CRITERIA["espn_min_matched_per_position"] == 300
    for pos, s in p["positions"].items():
        assert s["verdict"] == "insufficient_evidence" and s["required_n"] == 300
        assert "< 300 required" in s["verdict_reason"]
        assert s["ours"]["mae"] == pytest.approx(1.0) and s["espn"]["mae"] == pytest.approx(4.0)   # descriptive only
    assert p["positions"]["QB"]["paired_n"] == 8 * 6


def test_four_week_threshold(tmp_path, monkeypatch):
    monkeypatch.setitem(PROMOTION_CRITERIA, "espn_min_matched_per_position", 1)     # weeks are the only bar here
    con = make_world(tmp_path / "w.duckdb")
    three = build(con, readers={PROD: reader(PROD, {1, 2, 3})})
    assert all(s["verdict"] == "insufficient_evidence" and s["fully_final_weeks"] == 3
               and s["verdict_reason"] == "3 distinct weeks < 4 required" for s in three["positions"].values())
    four = build(con, readers={PROD: reader(PROD, {1, 2, 3, 4})})
    assert {s["verdict"] for s in four["positions"].values()} == {"ours_better"}
    assert all(s["ci95"][1] < 0 and s["statement"] == "Fantasy Intel was more accurate than ESPN."
               for s in four["positions"].values())


def test_insufficient_count_for_one_position_only(tmp_path, gate):
    """TE rows dropped after week 2: TE stays insufficient on its own count while the others clear the gate."""
    con = make_world(tmp_path / "w.duckdb")
    drop_te = lambda r: None if POS_OF[r["espn_player_id"]] == "TE" and r["week"] > 2 else r  # noqa: E731
    p = build(con, readers={PROD: reader(PROD, mutate=drop_te)})
    te = p["positions"]["TE"]
    assert te["verdict"] == "insufficient_evidence" and te["paired_n"] == 16 and "16 model-covered rows < 30" in te["verdict_reason"]
    assert p["positions"]["WR"]["verdict"] == "ours_better"
    # the TE rows without a forecast are ESPN fallback in the consumer view, never model evidence
    assert te["coverage"]["fallback_n"] == 8 * 4 and te["coverage"]["model_covered"] == 16


def test_bootstrap_ci_crossing_zero_is_no_established_difference(tmp_path, gate):
    """RB: worse than ESPN in odd weeks, better in even weeks: the paired difference averages out and its 95%
    week-clustered interval includes 0."""
    con = make_world(tmp_path / "w.duckdb")
    worse_odd = lambda r: ({**r, "expected_points": r["expected_points"] + 6} if POS_OF[r["espn_player_id"]] == "RB"
                           and r["week"] % 2 else r)  # noqa: E731
    p = build(con, readers={PROD: reader(PROD, {1, 2, 3, 4, 5, 6}, mutate=worse_odd)})
    rb = p["positions"]["RB"]
    assert rb["ci95"][0] < 0 < rb["ci95"][1]
    assert rb["verdict"] == "no_significant_difference" and rb["statement"] == "No statistically established difference."
    assert p["status"]["verdict"] == "no_significant_difference"     # positions that clear the gate disagree


def test_espn_better_is_reported_as_clearly_as_ours_better(tmp_path, gate):
    con = make_world(tmp_path / "w.duckdb")
    p = build(con, readers={PROD: reader(PROD, shift=7.0)})
    assert {s["verdict"] for s in p["positions"].values()} == {"espn_better"}
    assert all(s["ci95"][0] > 0 and s["statement"] == "ESPN was more accurate than Fantasy Intel."
               for s in p["positions"].values())
    assert p["status"]["verdict"] == "espn_better"


# ----------------------------------------------------------------------------- partial weeks, fallback, versions


def test_missing_week_6_evidence_is_preliminary_never_a_verdict(tmp_path, gate):
    con = make_world(tmp_path / "w.duckdb", skip_stats_teams=[(6, "CCC")])
    p = build(con)
    assert p["weeks"]["fully_final"] == [1, 2, 3, 4, 5] and p["weeks"]["partial"] == [6, 7]
    assert p["weeks"]["in_progress"] == [6, 7] and p["weeks"]["upcoming"] == []        # every synthetic week kicked off
    assert 6 in p["preliminary"]["weeks"] and p["preliminary"]["by_position"]["QB"]["n"] > 0
    assert not any(w["week"] in (6, 7) for w in p["weekly"])
    assert all(s["fully_final_weeks"] == 5 for s in p["positions"].values())
    assert p["positions"]["QB"]["observations"]["by_reason"]["week_not_fully_final"] > 0


def test_only_started_weeks_and_the_next_one_are_scored(tmp_path):
    """ESPN stores projections for every future week; those weeks are not scored (their rows could only be excluded)
    except the next one, whose pregame capture is under way."""
    con = make_world(tmp_path / "w.duckdb")
    p = build(con, now=datetime(2026, 9, 4, 12, tzinfo=UTC))       # weeks 1-3 kicked off, week 4 next
    assert p["weeks"]["with_espn_history"] == [1, 2, 3] and p["weeks"]["upcoming"] == [4]
    assert p["coverage"]["rows"] == 56 * 3                         # the next week is never in the scorecard ...
    assert [w["week"] for w in p["model_versions"]["served_now"]] == [1, 2, 3, 4]      # ... only in the version table
    assert [w["week"] for w in p["model_versions"]["pregame_by_week"][PROD]] == [1, 2, 3, 4]
    assert not any(w["week"] == 4 for w in p["weekly"])
    con.execute("DELETE FROM espn_projection_history WHERE scoring_period = 1")
    assert build(con, now=datetime(2026, 9, 4, 12, tzinfo=UTC))["weeks"]["with_espn_history"] == [2, 3]


def test_weekly_trend_adds_up_to_the_scorecard_and_names_the_model(tmp_path):
    p = build(make_world(tmp_path / "w.duckdb"))
    for pos, s in p["positions"].items():
        weeks = [w for w in p["weekly"] if w["position"] == pos]
        assert sum(w["n"] for w in weeks) == s["paired_n"] == p["comparisons"]["pure_independent"]["by_position"][pos]["n"]
        assert {w["model_version"] for w in weeks} == {PROD}
        assert all(w["ours_mae"] == pytest.approx(1.0) and w["espn_mae"] == pytest.approx(4.0) for w in weeks)
    lt = p["positions"]["WR"]["lead_time"]
    assert (lt["ours_median_h"], lt["espn_median_h"], lt["median_gap_h"]) == (24.0, 48.0, -24.0)
    assert lt["horizon_matched_share"] == 1.0          # |24 - 48| <= the 24 h window


def test_fallback_rows_are_counted_but_never_credited(tmp_path):
    con = make_world(tmp_path / "w.duckdb")
    abstain = lambda r: ({**r, "coverage_status": "abstain_no_history", "expected_points": None,
                          "projected_points": None} if POS_OF[r["espn_player_id"]] == "WR" and r["week"] <= 3 else r)  # noqa: E731
    p = build(con, readers={PROD: reader(PROD, mutate=abstain)})
    wr = p["positions"]["WR"]
    assert wr["paired_n"] == 24 * 3                                   # only the weeks the model projected
    assert wr["coverage"] == {"eligible_resolved": 24 * 6, "model_covered": 24 * 3, "fallback_n": 24 * 3,
                              "fallback_share": 0.5, "model_share": 0.5}
    assert wr["ours"]["mae"] == pytest.approx(1.0)                    # no ESPN number counted as ours
    cv = p["comparisons"]["consumer_visible"]["by_position"]["WR"]
    assert cv["n"] == 24 * 6 and cv["fallback_n"] == 24 * 3


def test_mixed_ifp1_ifp2_history_is_compared_only_on_weeks_both_forecast(tmp_path, gate):
    con = make_world(tmp_path / "w.duckdb")
    p = build(con, readers={PROD: reader(PROD, {3, 4, 5, 6}), FALLBACK: reader(FALLBACK, {1, 2, 3, 4}, shift=2.0)})
    mv = p["model_versions"]
    # the headline scores the production model only, on its own weeks
    assert {w["week"] for w in p["weekly"]} == {3, 4, 5, 6} and {w["model_version"] for w in p["weekly"]} == {PROD}
    assert [w["week"] for w in mv["pregame_by_week"][PROD] if w["scored"]] == [3, 4, 5, 6]
    assert [w["week"] for w in mv["pregame_by_week"][FALLBACK] if w["scored"]] == [1, 2, 3, 4]
    cmp_ = mv["comparison"]
    assert cmp_["versions"] == [PROD, FALLBACK] and cmp_["common_weeks"] == [3, 4] and cmp_["n"] == 56 * 2
    qb = cmp_["by_position"]["QB"]
    assert qb["weeks_covered"] == 2 and qb["verdict"] == "insufficient_evidence"   # two common weeks < 4
    assert qb["mae"][PROD] == pytest.approx(1.0) and qb["mae"]["espn"] == pytest.approx(4.0)
    assert qb["mae_diff"] < 0
    # a version that never had a pregame forecast leaves the comparison empty rather than borrowing weeks
    none = build(con, readers={FALLBACK: reader(FALLBACK, set())})
    assert none["model_versions"]["comparison"]["n"] == 0 and none["model_versions"]["comparison"]["common_weeks"] == []


def test_version_comparison_gate_and_direction(tmp_path, gate):
    con = make_world(tmp_path / "w.duckdb")
    p = build(con, readers={FALLBACK: reader(FALLBACK, shift=3.0)})
    by = p["model_versions"]["comparison"]["by_position"]
    assert {by[pos]["verdict"] for pos in ("QB", "RB", "WR", "TE")} == {"first_better"}   # production closer
    assert by["ALL"]["descriptive_verdict"] == "first_better" and "verdict" not in by["ALL"]


# ----------------------------------------------------------------------------- Start/Sit


def _season_cands(weeks, pairs_per_week, *, model_good=True):
    """Two RBs per roster and team-week, ESPN picks wrong and the model right (or the reverse)."""
    cands, sets = [], []
    for w in weeks:
        ko = datetime(2026, 9, 1, 17, tzinfo=UTC) + timedelta(weeks=w)
        sets.append({"season": 2026, "week": w})
        for t in range(pairs_per_week):
            good, bad = (12.0, 4.0)                     # player a scores more; whoever ranks a first is right
            espn = (5.0, 9.0) if model_good else (9.0, 5.0)
            ours = {"ifp2_production": (10.0, 5.0) if model_good else (5.0, 10.0),
                    "ifp1_production": (10.0, 5.0)}
            a = cand(2 * t + 1, "RB", good, {"espn": espn[0], "A1": 6.0, **{m: v[0] for m, v in ours.items()}},
                     week=w, ko=ko, tm=t)
            b = cand(2 * t + 2, "RB", bad, {"espn": espn[1], "A1": 7.0, **{m: v[1] for m, v in ours.items()}},
                     week=w, ko=ko, tm=t)
            cands += [a, b]
    return cands, sets


def test_start_sit_eight_week_gate():
    cfg = sse.EvalConfig(resamples=200)
    settings = {1: {"RB": 1, "BE": 5}}
    seven, sets7 = _season_cands(range(1, 8), 40)
    s7 = pp.start_sit_summary(sets7, 2026, seven, settings, cfg)["models"]["ifp2_production"]["same_position"]
    assert s7["decisions"] == 280 and s7["weeks"] == 7 and s7["verdict"] == "insufficient_evidence"
    eight, sets8 = _season_cands(range(1, 9), 40)
    out8 = pp.start_sit_summary(sets8, 2026, eight, settings, cfg)
    s8 = out8["models"]["ifp2_production"]["same_position"]
    assert s8["decisions"] == 320 and s8["weeks"] == 8 and s8["verdict"] == "better"
    assert s8["ours_accuracy"] == 1.0 and s8["espn_accuracy"] == 0.0
    assert s8["statement"] == "Fantasy Intel made better decisions than ESPN."
    worse, _ = _season_cands(range(1, 9), 40, model_good=False)
    sw = pp.start_sit_summary(sets8, 2026, worse, settings, cfg)["models"]["ifp2_production"]
    assert sw["same_position"]["verdict"] == "worse" and sw["lineups"]["verdict"] == "worse"
    assert out8["gate"] == {"min_decisions": 200, "min_weeks": 8} and out8["warnings"] == []


def test_start_sit_never_credits_fallback_choices():
    """A player the model did not project (the site showed ESPN) removes the decision for the model set."""
    cands, sets = _season_cands(range(1, 9), 30)
    for c in cands[::2]:
        del c["preds"]["ifp2_production"]        # one player of every pair: fallback
    out = pp.start_sit_summary(sets, 2026, cands, {1: {"RB": 1, "BE": 5}}, sse.EvalConfig(resamples=100))
    m = out["models"]["ifp2_production"]
    assert m["with_pregame_prediction"] == 240 and m["candidates"] == 480
    assert m["same_position"]["decisions"] == 0 and m["same_position"]["verdict"] == "insufficient_evidence"
    assert m["lineups"]["decisions"] == 0
    assert out["models"]["ifp1_production"]["same_position"]["decisions"] == 240
    # a model with no pregame prediction anywhere is unavailable, never backfilled
    for c in cands:
        c["preds"].pop("ifp2_production", None)
    gone = pp.start_sit_summary(sets, 2026, cands, {1: {"RB": 1, "BE": 5}}, sse.EvalConfig(resamples=100))
    assert gone["models"]["ifp2_production"]["evaluated"] is False
    assert "ifp2_production" in gone["models"]["ifp2_production"]["unavailable_reason"]


def test_start_sit_summary_from_recorded_decision_sets(tmp_path):
    """The full path: write-once decision sets on disk -> verified load -> outcomes from the world -> evaluation."""
    con = make_world(tmp_path / "w.duckdb")
    out = tmp_path / "start_sit"
    write_decision_sets(con, out, range(1, 6))
    p = build(con, decision_dir=out)
    ss = p["start_sit"]
    assert ss["sets"] == 5 and ss["weeks_recorded"] == [1, 2, 3, 4, 5]
    m = ss["models"]["ifp2_production"]
    assert m["evaluated"] and m["same_position"]["decisions"] > 0 and m["same_position"]["weeks"] == 5
    assert m["same_position"]["verdict"] == "insufficient_evidence"               # 5 weeks < 8
    assert ss["models"]["ifp1_production"]["evaluated"] is False                 # never recorded: not backfilled
    assert any("a verdict needs at least 200 decisions and 8 weeks" in w for w in ss["warnings"])


def write_decision_sets(con, out: Path, weeks) -> None:
    """Two fantasy rosters of 14 real world players per week, recorded 12 h before kickoff, with ESPN's and the
    production model's stored pregame numbers (A1 a point-in-time baseline)."""
    espn = {(w, pid): v for w, pid, v in con.execute(
        "SELECT scoring_period, espn_player_id, projected_points FROM espn_projection_history").fetchall()}
    ours = {(w, pid): v for w, pid, v in con.execute(
        "SELECT week, espn_player_id, expected_points FROM independent_player_projections").fetchall()}
    settings = {1: {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "BE": 7}}
    for w in weeks:
        ko = kickoff(w)
        rec = ko - timedelta(hours=12)
        cands = []
        for tm, teams in ((1, ("AAA", "BBB")), (2, ("CCC", "DDD"))):
            for pid, team, slot, pos in PLAYERS:
                if team not in teams:
                    continue
                cands.append({"league_id": 1, "fantasy_team_id": tm, "season": 2026, "week": w, "espn_player_id": pid,
                              "gsis_id": next(g for g, p in ESPN_ID.items() if p == pid), "position": pos,
                              "team": team, "kickoff": ko, "slot": "BE", "is_starter": False,
                              "roster_synced_at": ko - timedelta(hours=36),
                              "preds": {"espn": {"value": espn[(w, pid)], "captured_at": ko - timedelta(hours=48),
                                                 "point_in_time": False},
                                        "A1": {"value": espn[(w, pid)] + 0.5, "captured_at": None, "point_in_time": True},
                                        "ifp2_production": {"value": ours[(w, pid)], "captured_at": ko - timedelta(hours=24),
                                                            "point_in_time": False}}})
        ds = sse.decision_set(2026, w, cands, [], settings, rec, models=["espn", "A1", "ifp2_production"])
        sse.write_decision_set(out, ds)


# ----------------------------------------------------------------------------- store, reader, integrity


def fresh_db(tmp_path, name="r.duckdb"):
    import duckdb

    con = duckdb.connect(str(tmp_path / name))
    init_db(con)
    return con


def a_report(tmp_path, **kw):
    return build(make_world(tmp_path / "src.duckdb"), **kw)


def test_store_is_write_once_and_the_reader_picks_the_newest_verified(tmp_path):
    p = a_report(tmp_path)
    con = fresh_db(tmp_path)
    sha, created = pr.store_report(con, p)
    assert created and pr.store_report(con, p) == (sha, False)                    # identical: nothing written
    later = {**p, "generated_at": "2026-10-02T12:00:00+00:00"}
    sha2, _ = pr.store_report(con, later)
    got, got_sha, rejected = pr.latest(con, 2026)
    assert got_sha == sha2 and got == later and rejected == []
    assert pr.sha256_text(pr.canonical(got)) == got_sha
    assert pr.latest(con, 2025) == (None, None, [])


def test_corrupted_report_is_rejected_and_the_previous_one_served(tmp_path):
    p = a_report(tmp_path)
    con = fresh_db(tmp_path)
    good, _ = pr.store_report(con, p)
    bad, _ = pr.store_report(con, {**p, "generated_at": "2026-10-03T00:00:00+00:00"})
    text = con.execute("SELECT payload FROM projection_performance_reports WHERE report_sha256 = ?", [bad]).fetchone()[0]
    con.execute("UPDATE projection_performance_reports SET payload = ? WHERE report_sha256 = ?",
                [text.replace('"paired_n":48', '"paired_n":480'), bad])
    got, sha, rejected = pr.latest(con, 2026)
    assert sha == good and got == p
    assert rejected == [{"report_sha256": bad, "generated_at": "2026-10-03 00:00:00",
                         "reason": "content hash mismatch: the stored report was altered or damaged"}]
    body = pr.response(con, 2026, now=NOW)
    assert body["available"] and body["report_sha256"] == good and body["rejected"] == rejected
    # every row damaged: nothing is shown, and the page is told why
    con.execute("UPDATE projection_performance_reports SET payload = payload || ' '")
    body = pr.response(con, 2026, now=NOW)
    assert body["available"] is False and body["status"] == "integrity_failed" and body["report"] is None
    assert len(body["rejected"]) == 2 and "passed the integrity checks" in body["unavailable_reason"]


def test_a_report_with_a_hash_that_verifies_but_a_false_claim_is_rejected(tmp_path):
    """A correctly hashed row whose content breaks the published rules (forged verdict, unknown schema, an
    identifier, synthetic data) is never served."""
    p = a_report(tmp_path)
    con = fresh_db(tmp_path)
    forged = json.loads(json.dumps(p))
    forged["positions"]["QB"].update(verdict="ours_better", statement=pr.STATEMENTS["ours_better"], ci95=[-0.5, 0.4])
    text = pr.canonical(forged)
    con.execute("INSERT INTO projection_performance_reports VALUES (?, 2026, ?, '2026-10-05 00:00:00', ?)",
                [pr.sha256_text(text), pr.SCHEMA_VERSION, text])
    got, _, rejected = pr.latest(con, 2026)
    assert got is None and "without a 95% interval that excludes 0" in rejected[0]["reason"]
    with pytest.raises(ValueError, match="refusing to store"):
        pr.store_report(con, forged)
    assert pr.validate({**p, "schema_version": "projection-performance/v0"}).startswith("unknown report schema")
    leaky = json.loads(json.dumps(p))
    leaky["weekly"][0]["espn_player_id"] = 1000
    assert "identifier field" in pr.validate(leaky)
    assert pr.validate({**p, "synthetic": True}) == "synthetic test data is never served as real results"
    assert pr.validate({**p, "synthetic": True}, allow_synthetic=True) is None
    broken = json.loads(json.dumps(p))
    broken["positions"]["QB"] = "not an object"
    assert pr.validate(broken) == "the report is malformed (AttributeError)"
    broken = json.loads(json.dumps(p))
    broken["positions"]["QB"].update(verdict="ours_better", statement=pr.STATEMENTS["ours_better"], ci95=["a", "b"])
    assert pr.validate(broken).startswith("the report is malformed")
    wrong = json.loads(json.dumps(p))
    wrong["positions"]["RB"]["statement"] = "Fantasy Intel was more accurate than ESPN."
    assert pr.validate(wrong) == "RB: the statement does not match the verdict"


def test_stale_report(tmp_path):
    p = a_report(tmp_path)
    con = fresh_db(tmp_path)
    pr.store_report(con, p)
    body = pr.response(con, 2026, now=NOW + timedelta(hours=2))
    assert body["status"] == "ok" and body["stale"] is False
    old = pr.response(con, 2026, now=NOW + timedelta(days=9))
    assert old["status"] == "stale" and old["stale_reasons"][0].startswith("built 9 days ago")
    con.execute("INSERT INTO sync_runs (run_id, source, started_at, finished_at, status) "
                "VALUES ('espn-x', 'espn', '2026-10-01 13:00:00', '2026-10-01 13:05:00', 'ok')")
    later = pr.response(con, 2026, now=NOW + timedelta(hours=2))
    assert later["status"] == "stale" and later["stale_reasons"] == ["an ESPN sync finished after the report was built"]
    # a failed sync is not newer data
    con.execute("INSERT INTO sync_runs (run_id, source, started_at, finished_at, status) "
                "VALUES ('nfl-x', 'nfl', '2026-10-01 14:00:00', '2026-10-01 14:05:00', 'error')")
    assert not any("NFL" in r for r in pr.response(con, 2026, now=NOW + timedelta(hours=2))["stale_reasons"])


def test_no_report_yet(tmp_path):
    con = fresh_db(tmp_path)
    body = pr.response(con, 2026, now=NOW)
    assert body["available"] is False and body["status"] == "no_report" and body["report"] is None
    assert body["governance"]["espn_superiority_established"] is False and body["governance"]["start_sit_ready"] is False
    import duckdb

    bare = duckdb.connect()              # a hosted copy published before the table existed
    assert pr.response(bare, 2026, now=NOW)["status"] == "no_report"


def test_report_holds_aggregates_only(tmp_path):
    con = make_world(tmp_path / "w.duckdb")
    out = tmp_path / "ss"
    write_decision_sets(con, out, range(1, 3))
    p = build(con, decision_dir=out)
    text = pr.canonical(p)
    assert pr._forbidden(p) is None
    for pid, team, slot, _ in PLAYERS[:5]:
        assert f"-{team}-{slot}" not in text            # no GSIS ids of the synthetic players
    assert '"league_id"' not in text and '"fantasy_team_id"' not in text


# ----------------------------------------------------------------------------- CLI, refresh, API, hosted bundle


def test_publish_cli_inserts_one_verified_row(tmp_path, monkeypatch):
    db = tmp_path / "cli.duckdb"
    make_world(db).close()
    monkeypatch.setattr(pp, "league_scoring", lambda con, season: (99, SCORING))
    assert pp.main(["publish", "--season", "2026", "--db", str(db), "--decision-dir", str(tmp_path / "none")]) == 0
    import duckdb

    con = duckdb.connect(str(db), read_only=True)
    got, sha, rejected = pr.latest(con, 2026)
    assert got is not None and rejected == [] and got["synthetic"] is False and got["season"] == 2026
    assert con.execute("SELECT count(*) FROM projection_performance_reports").fetchone()[0] == 1
    con.close()
    assert pp.main(["show", "--season", "2026", "--db", str(tmp_path / "missing.duckdb")]) == 3


@pytest.mark.parametrize("token,report_code", [("tok", 0), ("tok", 3), ("", 0)])
def test_refresh_builds_the_report_before_publishing_and_never_fails_on_it(monkeypatch, tmp_path, token, report_code):
    import refresh_all as refresh

    assert refresh.REPORT not in refresh.plan(publish=True)      # not a chain step: it cannot stop the refresh
    monkeypatch.setenv("MOTHERDUCK_TOKEN", token)
    calls = []

    def fake_step(cmd, log, timeout=None):
        calls.append(Path(cmd[1]).name)
        return report_code if Path(cmd[1]).name == refresh.REPORT else 0

    monkeypatch.setattr(refresh, "run_step", fake_step)
    monkeypatch.setattr(refresh, "check_pregame", lambda log: None)
    monkeypatch.setattr(refresh, "LOG_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["refresh_all.py", "--skip-evidence"])
    assert refresh.main() == 0
    expected = [*refresh.STEPS, refresh.REPORT] + ([refresh.PUBLISH] if token else [])
    assert calls == expected
    log = next(tmp_path.glob("refresh-*.log")).read_text()
    assert ("WARNING: report:" in log) == (report_code != 0)


def test_api_route(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    db = tmp_path / "api.duckdb"
    con = fresh_db(tmp_path, "api.duckdb")
    p = a_report(tmp_path)
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db))
    monkeypatch.setenv("MOTHERDUCK_DATABASE", "")
    monkeypatch.setenv("FANTASY_API_KEY", "")
    from app.main import app

    client = TestClient(app)
    body = client.get("/api/projections/performance?season=2026").json()
    assert body["available"] is False and body["status"] == "no_report"
    import duckdb

    con = duckdb.connect(str(db))
    pr.store_report(con, p)
    synthetic = {**p, "synthetic": True, "label": pp.SYNTHETIC_LABEL, "generated_at": "2026-10-04T00:00:00+00:00"}
    pr.store_report(con, synthetic)
    con.close()
    body = client.get("/api/projections/performance?season=2026").json()
    assert body["available"] and body["report"]["synthetic"] is False      # the synthetic row is never served
    assert body["rejected"][0]["reason"] == "synthetic test data is never served as real results"
    monkeypatch.setenv("PROJECTION_PERFORMANCE_ALLOW_SYNTHETIC", "1")
    assert client.get("/api/projections/performance?season=2026").json()["report"]["synthetic"] is True


def test_hosted_bundle_serves_the_report_without_research_packages(tmp_path):
    db = tmp_path / "hosted.duckdb"
    con = fresh_db(tmp_path, "hosted.duckdb")
    pr.store_report(con, a_report(tmp_path))
    con.close()
    code = ("import sys\nfor m in ('polars', 'numpy', 'sklearn', 'scipy'):\n    sys.modules[m] = None\n"
            "import duckdb\nfrom app.analytics import performance_report as pr\nimport app.api.projection_performance\n"
            f"con = duckdb.connect({str(db)!r}, read_only=True)\n"
            "body = pr.response(con, 2026)\nassert body['available'], body\n"
            "bad = [m for m in ('app.analytics.espn_benchmark', 'app.analytics.forecast_backtest') if m in sys.modules]\n"
            "assert not bad, bad\nprint(body['report']['positions']['QB']['verdict'])")
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, check=True, capture_output=True, text=True)
    assert out.stdout.strip() == "insufficient_evidence"


# ----------------------------------------------------------------------------- frontend fixture


def synthetic_report(tmp_path, monkeypatch) -> dict:
    """The clearly labelled synthetic report the frontend tests and screenshots use: a lowered gate (30) so every
    verdict kind appears; week 6 partial; QB worse than ESPN, RB mixed, WR better with some fallback, TE too few
    weeks; ifp-1 forecasts for weeks 1-3; five weeks of recorded decision sets."""
    monkeypatch.setitem(PROMOTION_CRITERIA, "espn_min_matched_per_position", 30)
    con = make_world(tmp_path / "fixture.duckdb", skip_stats_teams=[(6, "CCC")])
    out = tmp_path / "fixture_ss"
    write_decision_sets(con, out, range(1, 6))

    def mutate(r):
        pos = POS_OF[r["espn_player_id"]]
        if pos == "QB":
            return {**r, "expected_points": r["expected_points"] + 6}
        if pos == "RB" and r["week"] % 2:
            return {**r, "expected_points": r["expected_points"] + 6}
        if pos == "WR" and r["week"] == 2 and r["espn_player_id"] % 3 == 0:
            return {**r, "coverage_status": "abstain_no_history", "expected_points": None, "projected_points": None}
        if pos == "TE" and r["week"] > 3:
            return None
        return r

    payload = pp.build_report(con, 2026, now=NOW, scoring=SCORING, config=eb.BenchmarkConfig(resamples=300),
                              readers={PROD: reader(PROD, mutate=mutate), FALLBACK: reader(FALLBACK, {1, 2, 3}, shift=1.5)},
                              decision_dir=out, synthetic=True)
    store = fresh_db(tmp_path, "fixture_store.duckdb")
    pr.store_report(store, payload)
    body = pr.response(store, 2026, now=NOW + timedelta(hours=1), allow_synthetic=True)
    return json.loads(json.dumps(body, default=str))


def test_frontend_fixture_is_the_builders_output(tmp_path, monkeypatch):
    """frontend/lib/fixtures/projection-performance.synthetic.json is exactly what the backend builds and serves
    for the synthetic world, so the page tests read real backend output. Regenerate with
    REGEN_PERFORMANCE_FIXTURE=1 uv run pytest tests/test_projection_performance.py -k fixture"""
    body = synthetic_report(tmp_path, monkeypatch)
    assert body["report"]["synthetic"] is True and body["report"]["label"] == pp.SYNTHETIC_LABEL
    verdicts = {p: s["verdict"] for p, s in body["report"]["positions"].items()}
    assert verdicts == {"QB": "espn_better", "RB": "no_significant_difference", "WR": "ours_better",
                        "TE": "insufficient_evidence"}
    text = json.dumps(body, indent=1, sort_keys=True) + "\n"
    if os.getenv("REGEN_PERFORMANCE_FIXTURE") == "1":
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(text)
    assert FIXTURE.exists(), "run with REGEN_PERFORMANCE_FIXTURE=1 to create the frontend fixture"
    assert json.loads(FIXTURE.read_text()) == json.loads(text), "fixture is stale: regenerate it (see docstring)"


def test_statements_cover_every_verdict():
    assert set(pr.VERDICTS) == set(eb.VERDICTS) == set(pr.STATEMENTS)
    assert set(pr.DECISION_VERDICTS) == set(sse.VERDICTS) == set(pr.DECISION_STATEMENTS)


# ----------------------------------------------------------------------------- forecast horizon (rest-of-season runs)


from test_espn_benchmark import FP, W5_PRE_A, W5_PRE_B, v8_world, with_participation  # noqa: E402
from test_independent_projections import live  # noqa: E402,F401  (fixture: schema-v8 database with a week-5 history)
from test_production_forecasts import store as _store  # noqa: E402


def _run_w5(con, src, created, values, *, horizon=0, version=PROD, sha="a" * 64):
    meta = {"model_version": version}
    if horizon:
        meta.update(forecast_horizon=horizon, projected_from_week=5 - horizon)
    run, _, _ = _store(con, src, created, week=5, sha=sha, mutate=with_participation(values), meta_over=meta)
    return run


def test_rest_of_season_runs_never_enter_the_scorecard(live, tmp_path):  # noqa: F811
    """A horizon-1 run for week 5 (made by last week's cycle, still before kickoff) changes nothing in the report: the
    paired observations, errors, coverage, lead times and verdicts come from the week's own horizon-0 run only, and
    the rest-of-season run is listed as excluded with #80's reason. With only a rest-of-season run, nothing is scored."""
    import shutil

    import duckdb

    con, src = v8_world(live)
    ros_only = _run_w5(con, src, W5_PRE_A, {1: 50.0, 6: 50.0, 11: 50.0}, horizon=1)
    p = build(con)
    assert all(s["paired_n"] == 0 for s in p["positions"].values()) and p["weekly"] == []
    assert {"run_id": ros_only["run_id"], "week": 5,
            "reason": "rest-of-season forecast (horizon 1), not evidence"} in p["coverage"]["excluded_runs"]

    _run_w5(con, src, W5_PRE_A - timedelta(hours=1), {1: 20.0})            # the week's pregame run (horizon 0)
    con.execute("CHECKPOINT")
    base_path = tmp_path / "base.duckdb"
    shutil.copy(live[0].execute("PRAGMA database_list").fetchall()[0][2], base_path)
    base = build(duckdb.connect(str(base_path)))
    later_ros = _run_w5(con, src, W5_PRE_B, {1: 99.0, 6: 99.0, 11: 99.0}, horizon=2)   # newer, still pregame
    p = build(con)
    assert sum(s["paired_n"] for s in p["positions"].values()) > 0
    for key in ("positions", "weekly", "comparisons", "preliminary"):
        assert p[key] == base[key], key
    reasons = {e["run_id"]: e["reason"] for e in p["coverage"]["excluded_runs"]}
    assert reasons[later_ros["run_id"]] == "rest-of-season forecast (horizon 2), not evidence"
    # the site shows the newest run of any horizon, labelled as such; the scorecard still scores horizon 0 only
    [served] = [w for w in p["model_versions"]["served_now"] if w["week"] == 5]
    assert served["served_version"] == PROD and served["forecast_horizon"] == 2 and "horizon 2" in served["horizon_note"]


def test_start_sit_credits_the_served_run_only_at_horizon_0(live):  # noqa: F811
    """ifp2_production in a decision set is exactly the run the site served for the week (projection_source.select_run)
    and only when that run is the week's own horizon-0 evidence run; a served rest-of-season run gives no value."""
    con, src = v8_world(live)
    served = pp.ssx.served_production_rows
    rows, ex = served(con, 2026, [5], FP)
    assert rows == [] and ex == [{"run_id": None, "week": 5, "reason": "no independent run served (ESPN shown)"}]
    h0 = _run_w5(con, src, W5_PRE_A, {1: 20.0})
    rows, ex = served(con, 2026, [5], FP)
    assert ex == [] and rows and {r["week"] for r in rows} == {5}
    assert next(r for r in rows if r["espn_player_id"] == 1)["expected_points"] == 20.0
    assert {r["run_created_at"] for r in rows} == {W5_PRE_A.replace(tzinfo=UTC)}
    # a newer rest-of-season run is what the site now serves for week 5: not evidence, so nothing is credited
    ros = _run_w5(con, src, W5_PRE_B, {1: 99.0}, horizon=1)
    rows, ex = served(con, 2026, [5], FP)
    assert rows == [] and ex == [{"run_id": ros["run_id"], "week": 5,
                                  "reason": "rest-of-season forecast (horizon 1), not evidence"}]
    assert h0["run_id"] != ros["run_id"]


def test_start_sit_never_credits_a_served_fallback_to_the_production_model(live):  # noqa: F811
    from app.analytics import production_pin

    con, src = v8_world(live)
    fb = _run_w5(con, src, W5_PRE_A, {1: 20.0}, version=FALLBACK, sha=production_pin.APPROVED_ARTIFACT_SHA256)
    rows, ex = pp.ssx.served_production_rows(con, 2026, [5], FP)
    assert rows == [] and ex == [{"run_id": fb["run_id"], "week": 5, "reason": f"the site served the fallback {FALLBACK}"}]
