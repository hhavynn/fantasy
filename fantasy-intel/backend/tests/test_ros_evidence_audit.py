"""The prospective evidence audit (scripts/evidence_audit.py) counts horizon-0 runs only: rest-of-season runs neither
extend the audited weeks nor stand in for (or mislabel) a week's missing pregame run."""

from __future__ import annotations

from datetime import datetime, timezone

from app.analytics.independent_projection_store import league_scoring, write_run
from app.db.connection import connect

from test_evidence_health import PLAYERS, SAT, _assess, _ts, world  # noqa: F401

UTC = timezone.utc


def _ros_run(con, week, horizon, created):
    _, scoring = league_scoring(con, 2026)
    rows = [{"season": 2026, "week": week, "espn_player_id": pid, "gsis_id": f"00-{pid}", "position": pos,
             "nfl_team": team, "opponent": None, "is_home": None, "projected_points": 10.0, "interval_low": None,
             "interval_high": None, "model": "m", "coverage_status": "projected", "games_used": 5,
             "latest_usage_season": 2026, "latest_usage_week": 5, "missing_inputs": [], "flags": [], "explanation": {},
             "p_play": 0.9, "expected_points": 9.0, "participation": None} for pid, (pos, team) in PLAYERS.items()]
    meta = {"run_id": f"ros-w{week}-h{horizon}", "season": 2026, "week": week, "model_version": "ifp-2.0.0",
            "scoring_fingerprint": scoring.fingerprint, "artifact_id": "art-ifp-2.0.0",
            "artifact_created_at": "2026-10-13T09:00:00+00:00", "data_through_season": 2026, "data_through_week": 4,
            "model_status": {}, "created_at": _ts(created), "forecast_horizon": horizon,
            "projected_from_week": week - horizon}
    return write_run(con, meta, rows)


def test_rest_of_season_runs_do_not_expand_the_audit_or_change_the_cause(world):  # noqa: F811
    before = _assess(world, SAT)
    con = connect(world.db)
    created = datetime(2026, 10, 13, 14, tzinfo=UTC)          # last week's cycle: weeks 6..17 from W=5, before TNF
    for week in range(6, 18):
        _ros_run(con, week, week - 5, created)
    con.close()
    after = _assess(world, SAT)
    assert [w["week"] for w in after["weeks"]] == [w["week"] for w in before["weeks"]]
    for b, a in zip(before["weeks"], after["weeks"]):
        assert a["production"]["runs"] == b["production"]["runs"]
        assert a["missing"] == b["missing"]
