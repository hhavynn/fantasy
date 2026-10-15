"""Point-in-time source provenance (app.analytics.source_provenance) on synthetic nflverse-shaped
data: frozen pregame forecasts can be rebuilt from their own snapshots, and later corrections or
post-kickoff updates never change what a past forecast knew."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import polars as pl
import pytest

from app.analytics import source_provenance as sp
from app.analytics import weekly_learning as wl
from app.analytics.forecast_features import load_sources
from app.analytics.independent_projection_store import league_scoring
from app.db.connection import connect
from test_learning_cycle import _make_db

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
UTC = timezone.utc
NOW = "2025-09-10T08:00:00+00:00"            # after the synthetic nflverse sync; week 10 kicks off 17:00 UTC
KICK10 = datetime(2025, 9, 10, 17, tzinfo=UTC)


@pytest.fixture(scope="module")
def cli():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("weekly_learning_cli", SCRIPTS / "weekly_learning.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    """Three seasons, so a rebuild from the two snapshotted seasons is checked against a forecast
    that could see an older one; plus a fitted participation artifact."""
    d = tmp_path_factory.mktemp("prov_base")
    _make_db(d / "live.duckdb", [2023, 2024, 2025], {2025: 10})
    _make_db(d / "history.duckdb", [2023, 2024], league=False)
    con = connect(d / "live.duckdb")
    _, scoring = league_scoring(con, 2025)
    src = load_sources(con, scoring)
    con.close()
    from app.analytics.participation import PARTICIPATION_VERSION, build_store, fit_final
    pstore = build_store(src, [(s, w) for s in (2024, 2025) for w in range(1, 10 if s == 2025 else 18)])
    sel = {"positions": {p: {"lambda": 0.01} for p in ("QB", "RB", "WR", "TE")}, "conditional_model": {}}
    part = fit_final(pstore, sel, {"status": {}, "positions": {}}, "2025-09-01", 2020)
    models = d / "models"
    models.mkdir()
    (models / f"participation_{PARTICIPATION_VERSION}.json").write_text(json.dumps(part.to_dict(), default=str))
    sel = {"positions": {p: {"primary": "C", "lambda": {"B": 0.01, "C": 0.01}, "intervals": {}}
                         for p in ("QB", "RB", "WR", "TE")},
           "protocol": {"train_start_season": 2020}, "scoring_fingerprint": scoring.fingerprint}
    (models / "selection.json").write_text(json.dumps(sel))
    sha = hashlib.sha256((models / "selection.json").read_bytes()).hexdigest()[:16]
    (models / "heldout_report.json").write_text(json.dumps({"status": {}, "positions": {}, "selection_sha": sha}))
    return d


@pytest.fixture
def env(base, tmp_path):
    for name in ("live.duckdb", "history.duckdb"):
        shutil.copy(base / name, tmp_path / name)
    shutil.copytree(base / "models", tmp_path / "models")
    return tmp_path


def _argv(env, *cmd, now=NOW):
    return [*cmd, "--season", "2025", "--now", now, "--live-db", str(env / "live.duckdb"),
            "--history-db", str(env / "history.duckdb"), "--ifp-dir", str(env / "models"), "--out", str(env / "out")]


def _forecast(cli, env, now=NOW) -> dict:
    assert cli.main(_argv(env, "train", "--week", "10", now=now)) == 0
    assert cli.main(_argv(env, "predict", "--week", "10", now=now)) == 0
    runs = [r for r in wl.load_ledger(env / "out") if r["kind"] == "pregame"]
    return max(runs, key=lambda r: r["created_at"])


def _sql(env, *stmts):
    con = connect(env / "live.duckdb")
    for s in stmts:
        con.execute(s)
    con.close()


def _arts(run):
    return [wl.load_artifact(run["artifacts"][k]["path"]) for k in ("champion", "challenger")]


def _participation(env):
    from app.analytics.participation import PARTICIPATION_VERSION, ParticipationArtifact
    return ParticipationArtifact.from_dict(json.loads((env / "models" / f"participation_{PARTICIPATION_VERSION}.json")
                                                      .read_text()))


def _digest(root: Path) -> dict:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def _live_sources(env):
    con = connect(env / "live.duckdb")
    _, scoring = league_scoring(con, 2025)
    src = load_sources(con, scoring)
    con.close()
    return src


# ----------------------------------------------------------------------------- frozen forecast reconstruction


def test_forecast_rebuilt_after_later_source_updates_matches_the_frozen_run(cli, env):
    run = _forecast(cli, env)
    assert run["sources"]["manifest_id"] and any(r["p_play"] is not None for r in run["rows"])
    m = sp.load_manifest(env / "out", 2025, 10, run["sources"]["manifest_id"])
    assert m["seasons"] == [2024, 2025]                 # what the feature pipeline reads (current + previous)
    frozen = {"artifacts": _digest(env / "out" / "artifacts"), "ledger": _digest(env / "out" / "ledger"),
              "snapshots": _digest(env / "out" / "snapshots")}
    # after the forecast: a stat correction for week 8 and a week-10 injury report updated after kickoff
    target = next(r for r in run["rows"] if r["p_play"] is not None and r["challenger"]["points"] is not None)
    _sql(env, "UPDATE nfl_weekly_stats SET receiving_yards = receiving_yards + 80, receptions = receptions + 4 "
              "WHERE season = 2025 AND week = 8",
         f"DELETE FROM nfl_injuries WHERE season = 2025 AND week = 10 AND gsis_id = '{target['gsis_id']}'",
         f"INSERT INTO nfl_injuries (season, game_type, team, week, gsis_id, position, report_status, "
         f"report_primary_injury, practice_primary_injury, practice_status, date_modified) VALUES (2025, 'REG', "
         f"'{target['team']}', 10, '{target['gsis_id']}', '{target['position']}', 'Out', 'Ankle', 'Ankle', "
         f"'Did Not Participate In Practice', NULL)")       # 2025-style row: no provider timestamp
    champ, chal = _arts(run)
    rebuilt = wl.rebuild_run(env / "out", run, champ, chal, participation=_participation(env))
    assert rebuilt["inputs_hash"] == run["inputs_hash"] and rebuilt["rows"] == run["rows"]
    # the current tables WOULD give a different forecast: exactly the leak the snapshots prevent
    now_src = _live_sources(env)
    live = wl.predict(now_src, champ, chal, 2025, 10, datetime.fromisoformat(run["created_at"]),
                      artifact_ids=run["artifacts"], participation=_participation(env))
    by_id = {r["gsis_id"]: r for r in live["rows"]}
    assert by_id[target["gsis_id"]]["p_play"] != target["p_play"]
    assert any(by_id[r["gsis_id"]]["challenger"]["points"] != r["challenger"]["points"] for r in run["rows"])
    # originals are untouched by the corrections and by the rebuild
    assert _digest(env / "out" / "artifacts") == frozen["artifacts"]
    assert _digest(env / "out" / "ledger") == frozen["ledger"]
    assert _digest(env / "out" / "snapshots") == frozen["snapshots"]


def test_corrected_source_adds_a_snapshot_and_keeps_the_original(cli, env):
    run1 = _forecast(cli, env)
    before = _digest(env / "out" / "snapshots")
    _sql(env, "UPDATE nfl_weekly_stats SET rushing_yards = rushing_yards + 40 WHERE season = 2025 AND week = 7")
    run2 = _forecast(cli, env, now="2025-09-10T10:00:00+00:00")
    assert run2["run_id"] != run1["run_id"] and run2["sources"]["manifest_id"] != run1["sources"]["manifest_id"]
    p1, p2 = run1["sources"]["parts"], run2["sources"]["parts"]
    assert p1["panel/s2025"] != p2["panel/s2025"]                     # the corrected season is a new part
    assert p1["panel/s2024"] == p2["panel/s2024"] and p1["injuries/s2025"] == p2["injuries/s2025"]   # stored once
    after = _digest(env / "out" / "snapshots")
    assert all(after[k] == v for k, v in before.items())              # nothing rewritten
    for run in (run1, run2):                                          # both forecasts still rebuild exactly
        champ, chal = _arts(run)
        assert wl.rebuild_run(env / "out", run, champ, chal, _participation(env))["rows"] == run["rows"]


def test_duplicate_snapshot_is_stored_once_and_keeps_the_first_capture_time(env, base):
    src = _live_sources(env)
    a = sp.capture(env / "out", src, 2025, 10, captured_at="2025-09-09T12:00:00+00:00")
    files = _digest(env / "out" / "snapshots")
    b = sp.capture(env / "out", src, 2025, 10, captured_at="2025-09-09T18:00:00+00:00")
    assert a == b and b["captured_at"] == "2025-09-09T12:00:00+00:00"
    assert _digest(env / "out" / "snapshots") == files
    assert len(list((env / "out" / "snapshots" / "manifests").rglob("*.json"))) == 1


# ----------------------------------------------------------------------------- pregame information state


def _listed_team(src) -> str:
    return sorted(src.injuries.filter((pl.col("season") == 2025) & (pl.col("week") == 10))["team"].unique())[0]


def _injury_rows(src, team, status):
    inj = src.injuries.filter((pl.col("season") == 2025) & (pl.col("week") == 10))
    one = inj.filter(pl.col("team") == team).sort("gsis_id").head(1)
    rest = src.injuries.filter(~((pl.col("season") == 2025) & (pl.col("week") == 10)
                                 & (pl.col("gsis_id") == one["gsis_id"][0])))
    return type(src)(src.panel, src.games, src.defense, src.rosters,
                     pl.concat([rest, one.with_columns(pl.lit(status).alias("report_status"))])), one["gsis_id"][0]


@pytest.fixture
def week10_injuries(tmp_path):
    """Week 10 is final here so it has injury reports to play with."""
    d = tmp_path
    _make_db(d / "w.duckdb", [2024, 2025], {2025: 11})
    con = connect(d / "w.duckdb")
    _, scoring = league_scoring(con, 2025)
    src = load_sources(con, scoring)
    con.close()
    return src


def test_report_captured_before_kickoff_is_what_was_known(week10_injuries, tmp_path):
    src = week10_injuries
    team = _listed_team(src)
    early, gid = _injury_rows(src, team, "Questionable")
    sp.capture(tmp_path, early, 2025, 10, captured_at=KICK10 - timedelta(days=1),
               retrieved_at={"injuries": {2025: KICK10 - timedelta(days=1, hours=2)}})
    rows, rep = sp.injuries_as_of(tmp_path, 2025, 10, {team: KICK10})
    assert rows.filter(pl.col("gsis_id") == gid)["report_status"].to_list() == ["Questionable"]
    assert rep["teams"][team]["known_by"] == (KICK10 - timedelta(days=1, hours=2)).isoformat()


def test_report_changed_after_kickoff_never_reaches_that_team(week10_injuries, tmp_path):
    src = week10_injuries
    team = _listed_team(src)
    early, gid = _injury_rows(src, team, "Questionable")
    late, _ = _injury_rows(src, team, "Out")
    sp.capture(tmp_path, early, 2025, 10, captured_at=KICK10 - timedelta(hours=20))
    sp.capture(tmp_path, late, 2025, 10, captured_at=KICK10 + timedelta(hours=1))      # after the team's kickoff
    rows, rep = sp.injuries_as_of(tmp_path, 2025, 10, {team: KICK10, "LATE": KICK10 + timedelta(hours=3),
                                                       "EARLY": KICK10 - timedelta(days=2)})
    assert rows.filter(pl.col("gsis_id") == gid)["report_status"].to_list() == ["Questionable"]
    # had this team kicked off later, the newer capture would have been the one known before it
    rows_late, _ = sp.injuries_as_of(tmp_path, 2025, 10, {team: KICK10 + timedelta(hours=3)})
    assert rows_late.filter(pl.col("gsis_id") == gid)["report_status"].to_list() == ["Out"]
    assert rep["teams"]["LATE"]["known_by"] == (KICK10 + timedelta(hours=1)).isoformat()
    # a team that played before any capture gets nothing, never today's table
    assert rep["no_pregame_snapshot"] == ["EARLY"] and rows.filter(pl.col("team") == "EARLY").height == 0


def test_source_without_date_modified_is_labeled_not_imputed(env):
    _sql(env, "UPDATE nfl_injuries SET date_modified = NULL WHERE season = 2025 AND week = 9")
    con = connect(env / "live.duckdb")
    cov = sp.injury_timestamp_coverage(con, 2025)
    assert cov["column_present"] and 0 < cov["with_date_modified"] < cov["rows"]
    assert sp.publication_time_status(cov) == "partial"
    con.execute("ALTER TABLE nfl_injuries DROP COLUMN date_modified")
    cov = sp.injury_timestamp_coverage(con, 2025)
    assert cov == {**cov, "column_present": False, "with_date_modified": None}
    assert sp.publication_time_status(cov) == "missing"
    _, scoring = league_scoring(con, 2025)
    src = load_sources(con, scoring)                       # rows kept: they cannot be filtered by time
    con.close()
    assert src.injuries.filter(pl.col("season") == 2025).height > 0
    m = sp.capture(env / "out", src, 2025, 10, captured_at=NOW, meta=sp.source_meta({2025: cov}))
    assert m["meta"]["injury_report_timestamps"]["2025"]["publication_time"] == "missing"
    assert m["parts"]["injuries/s2025"]["retrieved_at"] is None       # unknown stays unknown
    assert sp.known_by(m, "injuries/s2025").isoformat() == NOW       # we provably had it at capture time


def test_missing_player_identity_never_reaches_the_snapshot(env):
    _sql(env, "INSERT INTO nfl_injuries (season, game_type, team, week, gsis_id, position, report_status, "
              "date_modified) VALUES (2025, 'REG', 'AAA', 9, NULL, 'WR', 'Out', NULL)")
    con = connect(env / "live.duckdb")
    cov = sp.injury_timestamp_coverage(con, 2025)
    _, scoring = league_scoring(con, 2025)
    src = load_sources(con, scoring)
    con.close()
    assert cov["unmapped_rows"] == 1                               # reported, not silently matched
    m = sp.capture(env / "out", src, 2025, 10, captured_at=NOW, meta=sp.source_meta({2025: cov}))
    snap = sp.read_part(env / "out", "injuries", 2025, m["parts"]["injuries/s2025"]["hash"])
    assert snap["gsis_id"].null_count() == 0
    assert m["meta"]["injury_report_timestamps"]["2025"]["unmapped_rows"] == 1


def test_schema_drift_across_seasons_still_reconstructs(env):
    src = _live_sources(env)
    old = type(src)(src.panel, src.games.with_columns(pl.col("week").cast(pl.Int32)), src.defense,
                    src.rosters, src.injuries)                   # an older capture with another integer width
    a = sp.capture(env / "out", old, 2025, 10, captured_at="2025-09-01T00:00:00+00:00")
    b = sp.capture(env / "out", src, 2025, 10, captured_at=NOW)
    mixed = {**b, "parts": {**b["parts"], "games/s2024": a["parts"]["games/s2024"]}}
    rebuilt, _ = sp.reconstruct(env / "out", mixed)
    assert rebuilt.games["week"].dtype == pl.Int64
    assert rebuilt.games.filter(pl.col("season") == 2024).height == src.games.filter(pl.col("season") == 2024).height
    assert a["parts"]["exits/s2025"]["status"] == "unavailable" and rebuilt.exits is None


# ----------------------------------------------------------------------------- failures


def test_snapshot_retrieval_failure_is_explicit(cli, env):
    run = _forecast(cli, env)
    champ, chal = _arts(run)
    part = run["sources"]["parts"]["panel/s2025"]
    path = env / "out" / "snapshots" / "panel" / "s2025" / f"{part}.parquet"
    saved = path.read_bytes()
    path.unlink()
    with pytest.raises(sp.ProvenanceError, match="missing"):
        wl.rebuild_run(env / "out", run, champ, chal)
    path.write_bytes(saved[: len(saved) // 2])
    with pytest.raises(sp.ProvenanceError, match="unreadable|does not match"):
        wl.rebuild_run(env / "out", run, champ, chal)
    with pytest.raises(wl.NotReady, match="no source snapshot"):
        wl.rebuild_run(env / "out", {**run, "sources": None}, champ, chal)


def test_capture_failure_freezes_no_forecast(cli, env, monkeypatch):
    assert cli.main(_argv(env, "train", "--week", "10")) == 0

    def broken(self, *a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(pl.DataFrame, "write_parquet", broken)
    assert cli.main(_argv(env, "predict", "--week", "10")) == 3
    assert wl.load_ledger(env / "out") == []
    assert not [p for p in (env / "out").rglob("*.partial")]


def test_original_artifact_unchanged_by_later_sources(cli, env):
    run = _forecast(cli, env)
    arts = _digest(env / "out" / "artifacts")
    _sql(env, "UPDATE nfl_weekly_stats SET receiving_yards = receiving_yards + 5 WHERE season = 2025 AND week = 9")
    _forecast(cli, env, now="2025-09-10T12:00:00+00:00")             # retrains: a new artifact is added
    after = _digest(env / "out" / "artifacts")
    assert all(after[k] == v for k, v in arts.items()) and len(after) > len(arts)
    champ, chal = _arts(run)
    assert wl.rebuild_run(env / "out", run, champ, chal, _participation(env))["rows"] == run["rows"]


# ----------------------------------------------------------------------------- injury evidence quality


def test_injury_evidence_distinguishes_pbp_report_and_snap_drop(week10_injuries):
    src = week10_injuries
    ids = src.panel.filter(pl.col("season") == 2025)["gsis_id"].unique().to_list()
    ev = sp.injury_evidence(src, 2025, 10, ids, {"2025": {"publication_time": "partial"}})
    kinds = {v["evidence"] for v in ev.values()}
    assert kinds <= {sp.PBP_VERIFIED, sp.REPORT_LISTING, sp.SNAP_DROP_ONLY, sp.NO_DROP, sp.UNKNOWN}
    assert sp.PBP_VERIFIED not in kinds                                # no play-by-play evidence loaded
    drop = next(g for g, v in ev.items() if v["game_class"] in ("role_low", "blowout_low", "listed_low"))
    assert ev[drop]["evidence"] == sp.SNAP_DROP_ONLY                   # a low snap share alone is not an injury
    assert all(v["report_publication_time"] == "partial" for v in ev.values() if v["evidence"] == sp.REPORT_LISTING)
    s, w = ev[drop]["last_game"]
    exits = pl.DataFrame({"season": [s], "week": [w], "gsis_id": [drop], "injured_in_game": [1], "injury_sec": [100.0],
                          "last_touch_sec": [90.0], "team_last_sec": [3600.0]})
    ev2 = sp.injury_evidence(src.with_exits(exits), 2025, 10, [drop])
    assert ev2[drop]["evidence"] == sp.PBP_VERIFIED and ev2[drop]["pbp_covered"]


def test_ledger_rows_carry_injury_evidence(cli, env):
    run = _forecast(cli, env)
    with_ev = [r for r in run["rows"] if r["injury_evidence"]]
    assert with_ev and all(r["injury_evidence"]["evidence"] in (sp.PBP_VERIFIED, sp.REPORT_LISTING, sp.SNAP_DROP_ONLY,
                                                                 sp.NO_DROP, sp.UNKNOWN) for r in with_ev)
    m = sp.load_manifest(env / "out", 2025, 10, run["sources"]["manifest_id"])
    assert set(m["meta"]["injury_report_timestamps"]) == {"2024", "2025"}
