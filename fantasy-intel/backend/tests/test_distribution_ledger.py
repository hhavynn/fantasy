"""scripts/distribution_ledger.py: the write-once pregame point-distribution ledger and its prospective coverage
check (research only). Synthetic artifacts, forecasts and outcomes; nothing here is a real result."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import polars as pl
import pytest

from app.analytics import point_distributions as pd_
from app.analytics import weekly_learning as wl

from test_point_distributions import _synthetic_oos

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
UTC = timezone.utc
KICK = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)          # Sunday of 2026 week 7
TNF = datetime(2026, 10, 23, 0, 15, tzinfo=UTC)           # its Thursday game
TUE = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)           # the learning cycle's freeze, after `predict`


@pytest.fixture(scope="module")
def dl():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("distribution_ledger", SCRIPTS / "distribution_ledger.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def artifact():
    choice = {"QB": {"method": "Bz"}, "RB": {"method": "R@300"}, "WR": {"method": "R@300"}, "TE": {"method": "R@300"}}
    status = {p: {"status": "validated"} for p in pd_.POSITIONS}
    return json.loads(json.dumps(pd_.fit_artifact(
        _synthetic_oos(), choice, status, through_key=202605, fingerprint="f" * 16, scoring_fingerprint="e" * 16,
        selection_sha="s" * 16,
        participation_status={"QB": "not_approved", "RB": "not_approved", "WR": "validated", "TE": "validated"})))


def write_artifact(dirpath: Path, art: dict, **override) -> Path:
    art = {**art, **override}
    if override:
        body = {k: v for k, v in art.items() if k != "content_hash"}
        art["content_hash"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    d = dirpath / "research" / "point_distributions"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"point_distributions_{pd_.DISTRIBUTION_VERSION}_{art['content_hash']}.json"
    path.write_text(json.dumps(art, sort_keys=True))
    return path


def wrow(gid, pos, team, kickoff=KICK, champ=10.0, chall=11.0, p=0.9, pregame=True):
    return {"gsis_id": gid, "position": pos, "team": team, "opponent": "ZZZ", "kickoff": kickoff.isoformat(),
            "pregame": pregame, "champion": {"points": champ, "low": None, "high": None, "expected_points": None},
            "challenger": {"points": chall, "low": None, "high": None, "expected_points": None}, "p_play": p}


def write_run(weekly: Path, created: datetime, rows: list[dict], season=2026, week=7) -> Path:
    shift = timedelta(days=7 * (week - 7))           # ROWS are week-7 games: move them to the run's week
    rows = [{**r, "kickoff": (datetime.fromisoformat(r["kickoff"]) + shift).isoformat()} for r in rows]
    run = {"schema": "forecast-ledger/v1", "run_id": f"pregame-s{season}w{week:02d}-{created:%Y%m%dT%H%M%S}-x",
           "created_at": created.isoformat(), "season": season, "week": week, "kind": "pregame", "rows": rows}
    d = weekly / "ledger" / f"s{season}w{week:02d}"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{run['run_id']}.json"
    path.write_text(json.dumps(run))
    return path


def args_for(dl, tmp_path, *extra):
    return dl.parse(["freeze", "--season", "2026", "--ifp-dir", str(tmp_path / "models"),
                     "--weekly-dir", str(tmp_path / "weekly"), "--live-db", str(tmp_path / "absent.duckdb"), *extra])


ROWS = [wrow("qb", "QB", "AAA", champ=18.0, chall=17.5), wrow("rb", "RB", "AAA", champ=12.0, p=0.8),
        wrow("wr", "WR", "BBB", champ=13.0, p=0.95), wrow("te", "TE", "BBB", champ=8.0, chall=None, p=None),
        wrow("k", "K", "BBB"), wrow("thu", "WR", "CCC", kickoff=TNF)]


@pytest.fixture
def world(tmp_path, artifact):
    write_artifact(tmp_path / "models", artifact)
    write_run(tmp_path / "weekly", TUE - timedelta(hours=1), ROWS)
    return tmp_path


def _files(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*"))
            if p.is_file()}


# ----------------------------------------------------------------------------- freeze


def test_freeze_writes_once_with_provenance_and_keeps_every_mean(dl, world, artifact):
    a = args_for(dl, world)
    weekly_before = _files(world / "weekly")
    rec = dl.run_freeze(a, 2026, 7, now=TUE)
    assert rec["status"] == "frozen", rec
    led = json.loads(Path(rec["ledger"]).read_text())
    assert led["schema"] == dl.SCHEMA and led["lifecycle"] == "research_only" and led["primary_mean"] == "ifp-1.0.0"
    assert led["artifact"]["content_hash"] == artifact["content_hash"] and led["artifact"]["pin_id"] == 1
    run_file = next((world / "weekly" / "ledger" / "s2026w07").glob("*.json"))
    assert led["source"]["weekly_ledger_runs"][0]["sha256"] == hashlib.sha256(run_file.read_bytes()).hexdigest()
    assert led["rows_sha256"] == hashlib.sha256(dl._canon(led["rows"]).encode()).hexdigest()
    rows = {r["gsis_id"]: r for r in led["rows"]}
    assert set(rows) == {"qb", "rb", "wr", "te", "thu"} and led["excluded"]["unsupported_position"] == 1   # no K
    for g, r in rows.items():
        d = r["distributions"]["ifp-1.0.0"]
        assert len(d["q"]) == len(pd_.TAUS)
        qf = pd_.QuantileFunction(tuple(pd_.TAUS.tolist()), tuple(d["q"]))
        assert qf.mean() == pytest.approx(d["mean"], abs=0.01)          # the frozen mean is the distribution's mean
        assert d["conditional"]["p10"] <= d["conditional"]["p50"] <= d["conditional"]["p90"]
    # unconditional only where the artifact approves participation (WR/TE), and never with a missing p_play
    assert "unconditional" in rows["wr"]["distributions"]["ifp-1.0.0"]
    assert "unconditional" not in rows["rb"]["distributions"]["ifp-1.0.0"]
    assert "unconditional" not in rows["qb"]["distributions"]["ifp-1.0.0"]
    assert "unconditional" not in rows["te"]["distributions"]["ifp-1.0.0"]
    assert rows["te"]["distributions"]["ifp-2.0.0"] == {"mean": None, "withheld": "no frozen mean"}
    assert rows["qb"]["distributions"]["ifp-2.0.0"]["mean"] == 17.5
    # idempotent: a later run (Thursday refresh, Sunday refresh) changes nothing
    first = Path(rec["ledger"]).read_bytes()
    again = dl.run_freeze(a, 2026, 7, now=KICK - timedelta(hours=2))
    assert again["status"] == "already_frozen" and Path(rec["ledger"]).read_bytes() == first
    assert _files(world / "weekly") == weekly_before                     # the weekly ledger is read, never written


def test_players_whose_game_started_are_never_frozen(dl, world):
    rec = dl.run_freeze(args_for(dl, world), 2026, 7, now=TNF + timedelta(hours=4))     # Friday, after TNF
    led = json.loads(Path(rec["ledger"]).read_text())
    assert "thu" not in {r["gsis_id"] for r in led["rows"]} and led["excluded"]["kicked_off"] == 1


def test_only_forecasts_made_before_now_and_before_kickoff_count(dl, world):
    write_run(world / "weekly", TUE + timedelta(days=1), [wrow("wr", "WR", "BBB", champ=20.0)])      # made later
    write_run(world / "weekly", KICK + timedelta(hours=1), [wrow("rb", "RB", "AAA", champ=30.0)])    # after kickoff
    rec = dl.run_freeze(args_for(dl, world), 2026, 7, now=TUE)
    rows = {r["gsis_id"]: r for r in json.loads(Path(rec["ledger"]).read_text())["rows"]}
    assert rows["wr"]["distributions"]["ifp-1.0.0"]["mean"] == 13.0 and rows["rb"]["distributions"]["ifp-1.0.0"]["mean"] == 12.0


def test_before_the_prospective_window_nothing_is_written(dl, world):
    rec = dl.run_freeze(args_for(dl, world), 2026, 5, now=TUE)
    assert rec["status"] == "before_prospective_start"
    assert not (world / "models" / "research" / "point_distributions" / "ledger").exists()


def test_without_an_artifact_it_warns_and_freezes_nothing(dl, tmp_path, capsys):
    write_run(tmp_path / "weekly", TUE - timedelta(hours=1), ROWS)
    rec = dl.run_freeze(args_for(dl, tmp_path), 2026, 7, now=TUE)
    assert rec["status"] == "no_artifact" and "point_distribution_research.py run" in rec["warnings"][0]
    assert not (tmp_path / "models").exists()
    code = dl.main(["freeze", "--season", "2026", "--week", "7", "--ifp-dir", str(tmp_path / "models"),
                    "--weekly-dir", str(tmp_path / "weekly"), "--live-db", str(tmp_path / "absent.duckdb"),
                    "--now", TUE.isoformat()])
    assert code == dl.EXIT_WARNINGS and "WARNING:" in capsys.readouterr().err


def test_no_pregame_forecast_yet_is_a_deferral_not_a_warning(dl, tmp_path, artifact):
    write_artifact(tmp_path / "models", artifact)
    rec = dl.run_freeze(args_for(dl, tmp_path), 2026, 7, now=TUE)
    assert rec["status"] == "deferred" and rec["warnings"] == []


def test_dry_run_decides_but_writes_nothing(dl, world):
    before = _files(world)
    rec = dl.run_freeze(args_for(dl, world, "--now", TUE.isoformat()), 2026, 7, now=TUE)
    assert rec["status"] == "would_freeze" and rec["rows"] == 5
    assert _files(world) == before                                         # not even the pin


def test_the_pin_is_kept_and_never_moves_to_a_newer_artifact(dl, world, artifact):
    dl.run_freeze(args_for(dl, world), 2026, 7, now=TUE)
    newer = write_artifact(world / "models", artifact, trained_through_key=202607)
    write_run(world / "weekly", TUE + timedelta(days=7), ROWS, week=8)
    rec = dl.run_freeze(args_for(dl, world), 2026, 8, now=TUE + timedelta(days=7))
    led = json.loads(Path(rec["ledger"]).read_text())
    assert led["artifact"]["pin_id"] == 1 and led["artifact"]["content_hash"] == artifact["content_hash"]
    # an explicit re-pin is recorded with its reason and used from then on
    out = dl.cmd_pin(dl.parse(["pin", "--ifp-dir", str(world / "models"), "--artifact", newer.name,
                               "--reason", "refit through week 7"]), TUE)
    assert out["pin"]["pin_id"] == 2 and out["pin"]["how"] == "explicit"
    write_run(world / "weekly", TUE + timedelta(days=14), ROWS, week=9)
    led9 = json.loads(Path(dl.run_freeze(args_for(dl, world), 2026, 9, now=TUE + timedelta(days=14))["ledger"]).read_text())
    assert led9["artifact"]["pin_id"] == 2 and led9["artifact"]["trained_through_key"] == 202607


def test_an_artifact_trained_on_the_week_itself_is_never_used(dl, tmp_path, artifact):
    write_artifact(tmp_path / "models", artifact, trained_through_key=202607)          # includes week 7
    write_run(tmp_path / "weekly", TUE - timedelta(hours=1), ROWS)
    rec = dl.run_freeze(args_for(dl, tmp_path), 2026, 7, now=TUE)
    assert rec["status"] == "no_artifact"                                              # never auto-pinned


def test_a_changed_artifact_is_refused(dl, world):
    dl.run_freeze(args_for(dl, world), 2026, 7, now=TUE)
    art = next((world / "models" / "research" / "point_distributions").glob("point_distributions_*.json"))
    art.write_text(art.read_text().replace('"research_only"', '"production"'))
    write_run(world / "weekly", TUE + timedelta(days=7), ROWS, week=8)
    rec = dl.run_freeze(args_for(dl, world), 2026, 8, now=TUE + timedelta(days=7))
    assert rec["status"] == "failed" and "changed since it was pinned" in rec["error"]
    assert not dl.week_path(args_for(dl, world), 2026, 8).exists()


def test_a_week_that_started_without_a_ledger_is_recorded_as_missed(dl, world):
    a = args_for(dl, world)
    ks = {"AAA": KICK, "BBB": KICK}
    assert dl.check_missed(a, 2026, 7, now=KICK - timedelta(hours=1), kickoffs=ks) is None       # not over yet
    rec = dl.check_missed(a, 2026, 7, now=KICK + timedelta(hours=4), kickoffs=ks)
    assert rec["week"] == 7 and (dl.ledger_dir(a) / "missed" / "s2026w07.json").exists()
    assert dl.check_missed(a, 2026, 5, now=KICK + timedelta(hours=4), kickoffs=ks) is None  # before the window
    assert dl.run_freeze(a, 2026, 7, now=TUE)["status"] == "frozen"     # a later freeze attempt is not refused...
    assert dl.check_missed(a, 2026, 7, now=KICK + timedelta(hours=4), kickoffs=ks) is None  # ...and is not missed


# ----------------------------------------------------------------------------- score


def _big_week(dl, tmp_path, artifact, n=450, season=2026, week=7):
    rng = np.random.default_rng(3)
    write_artifact(tmp_path / "models", artifact)
    rows = [wrow(f"{pos}{i}", pos, f"T{i % 8}", champ=float(rng.gamma(2.0, 4.0)), chall=None,
                 p=float(rng.uniform(0.6, 1.0))) for pos in pd_.POSITIONS for i in range(n)]
    write_run(tmp_path / "weekly", TUE - timedelta(hours=1), rows, season=season, week=week)
    a = args_for(dl, tmp_path)
    assert dl.run_freeze(a, season, week, now=TUE)["status"] == "frozen"
    return a


def _labeller(final=True, scale=1.0, seed=5, pending=()):
    """Outcomes drawn from each row's own frozen ifp-1.0.0 distribution (calibrated by construction), absences drawn
    from p_play; `scale` widens the outcomes around the mean (a miscalibrated world)."""
    def label(args, ledgers):
        rng = np.random.default_rng(seed)
        recs = []
        for i, led in enumerate(ledgers):
            for j, r in enumerate(led["rows"]):
                d = r["distributions"]["ifp-1.0.0"]
                qf = pd_.QuantileFunction(tuple(pd_.TAUS.tolist()), tuple(d["q"]))
                y = d["mean"] + scale * (qf.quantile(float(rng.uniform(0.005, 0.995))) - d["mean"])
                played = rng.uniform() < r["p_play"]
                status = wl.PENDING_GAME if r["gsis_id"] in pending else wl.APPEARED if played else wl.CONFIRMED_ABSENT
                recs.append({"season": led["season"], "week": led["week"], "key": led["season"] * 100 + led["week"],
                             "_i": i, "_j": j, "outcome_status": status,
                             "label": None if status == wl.PENDING_GAME else (y if played else 0.0)})
        return pl.DataFrame(recs), {(l["season"], l["week"]): final for l in ledgers}
    return label


def test_calibrated_outcomes_score_within_the_validation_bounds(dl, tmp_path, artifact):
    a = _big_week(dl, tmp_path, artifact, n=1000)
    rec = dl.run_score(a, labeller=_labeller(pending={"WR0", "WR1"}))
    assert rec["status"] == "scored", rec
    s = rec["scores"]
    assert s["weeks_scored"] == ["2026-07"] and s["artifacts"] == [artifact["content_hash"]]
    prim = s["by_mean"]["ifp-1.0.0"]["positions"]
    for pos in pd_.POSITIONS:
        c = prim[pos]["conditional"]
        assert 400 <= c["n"] < 1000 and c["coverage80"] == pytest.approx(0.8, abs=0.04), (pos, c)
        assert c["bounds"] == {"status": "within_bounds", "reasons": []}, (pos, c)
        assert c["weeks"] == 1 and c["coverage80_ci95"] is None             # one week: no week-clustered interval
    # WR/TE: unconditional ranges scored; RB conditional only; QB restricted (scored for monitoring, labelled so)
    assert prim["WR"]["unconditional"]["n"] == 998 and prim["WR"]["unconditional"]["coverage80"] == pytest.approx(0.8, abs=0.06)
    assert "withheld" in prim["RB"]["unconditional"] and "withheld" in prim["QB"]["unconditional"]
    assert prim["QB"]["policy"]["conditional"] == "restricted" and prim["RB"]["policy"]["conditional"] == "candidate"
    # the secondary mean is absent in this world: nothing is scored for it, nothing invented
    assert all(e["conditional"]["n"] == 0 for e in s["by_mean"]["ifp-2.0.0"]["positions"].values())
    stored = json.loads((dl.ledger_dir(a) / "scores.json").read_text())
    assert stored["weeks_scored"] == ["2026-07"]


def test_miscalibrated_outcomes_fall_outside_the_bounds(dl, tmp_path, artifact):
    a = _big_week(dl, tmp_path, artifact, n=600)
    c = dl.run_score(a, labeller=_labeller(scale=2.0))["scores"]["by_mean"]["ifp-1.0.0"]["positions"]["WR"]["conditional"]
    assert c["coverage80"] < 0.75 and c["bounds"]["status"] == "outside_bounds"


def test_weeks_that_are_not_fully_final_are_never_scored(dl, tmp_path, artifact):
    a = _big_week(dl, tmp_path, artifact, n=20)
    s = dl.run_score(a, labeller=_labeller(final=False))["scores"]
    assert s["weeks_scored"] == [] and s["weeks_not_final"] == ["2026-07"]
    assert all(e["conditional"]["n"] == 0 for e in s["by_mean"]["ifp-1.0.0"]["positions"].values())


def test_a_damaged_ledger_is_refused_not_scored(dl, tmp_path, artifact):
    a = _big_week(dl, tmp_path, artifact, n=5)
    p = dl.week_path(a, 2026, 7)
    led = json.loads(p.read_text())
    led["rows"][0]["distributions"]["ifp-1.0.0"]["q"][50] += 5.0
    p.write_text(json.dumps(led))
    rec = dl.run_score(a, labeller=_labeller())
    assert rec["status"] == "failed" and "do not match their recorded sha256" in rec["error"]


def test_real_outcome_labels_and_finality_on_a_synthetic_database(dl, tmp_path, artifact):
    """_labelled end to end (weekly_learning.label_outcomes + week_finality) on the synthetic nflverse world: a final
    week is scored with every player's real outcome; a week without results is not."""
    import json as _json

    from app.db.connection import connect, init_db
    from synthetic_forecast import SLOTS, TEAMS, add_league, add_participation, generate, gsis, write

    db = tmp_path / "live.duckdb"
    con = connect(db)
    init_db(con)
    write(con, add_participation(generate([2025], unfinished={2025: 10})))
    add_league(con, 2025, 10)
    con.execute("INSERT INTO sync_runs (run_id, source, started_at, finished_at, status, stats) VALUES "
                "('s', 'nfl', '2025-09-10 03:00:00', '2025-09-10 03:00:00', 'ok', ?)",
                [_json.dumps({"season": 2025, "nfl_schedules": 1})])
    con.close()
    write_artifact(tmp_path / "models", artifact)
    a = dl.parse(["score", "--season", "2025", "--ifp-dir", str(tmp_path / "models"), "--live-db", str(db)])
    for week in (5, 10):
        kick = datetime(2025, 9, min(28, week), 17, tzinfo=UTC)
        rows = [{**wrow(gsis(t, s), pos, t, kickoff=kick), "_run_id": "r"} for t in TEAMS for s, pos in SLOTS]
        body = {"season": 2025, "week": week, "artifact": {"content_hash": artifact["content_hash"]},
                "rows": dl.ledger_rows(artifact, rows)}
        body["rows_sha256"] = hashlib.sha256(dl._canon(body["rows"]).encode()).hexdigest()
        dl._write_once(dl.week_path(a, 2025, week), body, compact=True)
    s = dl.run_score(a)["scores"]
    assert s["weeks_scored"] == ["2025-05"] and s["weeks_not_final"] == ["2025-10"]
    resolved = s["outcomes"].get(wl.APPEARED, 0) + s["outcomes"].get(wl.CONFIRMED_ABSENT, 0)
    assert resolved >= len(TEAMS) * len(SLOTS)                       # every week-5 player resolved
    n = sum(e["conditional"]["n"] for e in s["by_mean"]["ifp-1.0.0"]["positions"].values())
    assert 0 < n == s["outcomes"][wl.APPEARED]                        # week 10 (no results) has no appearances


def test_the_app_never_imports_the_ledger():
    app = Path(__file__).resolve().parents[1] / "app"
    assert not [f for f in app.rglob("*.py") if "distribution_ledger" in f.read_text()]
