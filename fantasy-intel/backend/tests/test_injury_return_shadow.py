"""scripts/injury_return_shadow.py: the pre-registered injury-exit candidates (ifp-irs-p1 split slope, ifp-irb-p1
returner bias; docs/research/injury_return_split.md): candidate columns, features, the frozen spec, the freeze rule,
write-once / idempotent ledgers, failure isolation, and the one-look gate. Synthetic data only; nothing writes a
database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys

import polars as pl
import pytest

from app.analytics import challenger as ch
from app.analytics import context_model as cm

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
UTC = timezone.utc
SUN = datetime(2026, 11, 1, 13, 0, tzinfo=UTC)          # 1h before a 10:00 ET (14:00 UTC) week-8 slate


@pytest.fixture(scope="module")
def irs():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("injury_return_shadow", SCRIPTS / "injury_return_shadow.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def args(irs, tmp_path):
    return irs.parse(["pregame", "--season", "2026", "--ifp-dir", str(tmp_path / "models"),
                      "--live-db", str(tmp_path / "live.duckdb"), "--history-db", str(tmp_path / "hist.duckdb"),
                      "--weekly-dir", str(tmp_path / "weekly"), "--exits-dir", str(tmp_path / "weekly")])


# ----------------------------------------------------------------------------- definition


def test_candidate_columns_split_returners_from_play_through_rows(irs):
    games = pl.DataFrame({"season": [2026] * 4, "week": [4, 5, 6, 7], "team": ["PHI"] * 4})
    rows = pl.DataFrame({
        "key": [202608] * 5, "gsis_id": ["ret", "thru", "norm", "qb", "nosnap"],
        "position": ["RB", "WR", "TE", "QB", "RB"], "last_team": ["PHI"] * 5,
        "last_season": [2026] * 5, "last_week": [4, 7, 7, 4, 5],
        "injury_exit_last": [1, 1, 0, 1, 1], "snap_trend": [-0.53, -0.4, 0.1, -0.5, None]})
    out = {r["gsis_id"]: r for r in irs.candidate_columns(rows, games).iter_rows(named=True)}
    assert (out["ret"]["games_missed"], out["ret"]["ret_cohort"]) == (3, True)
    assert out["ret"]["snap_trend_ret"] == pytest.approx(-0.53) and out["ret"]["ret_ind"] == 1.0
    assert (out["thru"]["games_missed"], out["thru"]["ret_cohort"]) == (0, False)      # play-through: untouched
    assert out["thru"]["snap_trend_ret"] == 0.0 and out["thru"]["ret_ind"] == 0.0
    assert not out["norm"]["ret_cohort"] and out["norm"]["snap_trend_ret"] == 0.0
    assert not out["qb"]["ret_cohort"] and out["qb"]["ret_ind"] == 0.0                 # QB is never in the cohort
    assert out["nosnap"]["ret_cohort"] and out["nosnap"]["snap_trend_ret"] == 0.0      # a null trend contributes 0


def test_models_are_ifp22_plus_only_their_added_columns(irs):
    cfg = ch.ChallengerConfig.load()
    for pos in irs.POSITIONS:
        base = cm.features(cfg, pos)
        assert irs.model_features(cfg, "base", pos) == base
        assert irs.model_features(cfg, irs.IRS, pos) == [*base, "snap_trend_ret"]
        assert irs.model_features(cfg, irs.IRB, pos) == [*base, "ret_ind"]
        assert irs.model_features(cfg, irs.BOTH, pos) == [*base, "snap_trend_ret", "ret_ind"]
    assert set(irs.CANDIDATES) == {irs.IRS, irs.IRB} and irs.BOTH not in irs.COHORT


def test_spec_is_frozen_once_and_a_changed_rule_is_refused(irs, args, monkeypatch):
    assert irs.frozen_spec(args, "fp", create=False) is None          # scoring never creates it
    s = irs.frozen_spec(args, "fp", create=True)
    assert s["first_week"] == [2026, 8] and s["look"] == [2026, 18] and s["rule"]["min_cohort"] == 40
    assert irs.frozen_spec(args, "fp", create=True)["frozen_at"] == s["frozen_at"]
    monkeypatch.setattr(irs, "MIN_COHORT", 20)
    with pytest.raises(SystemExit, match="different spec"):
        irs.frozen_spec(args, "fp", create=True)
    monkeypatch.undo()
    with pytest.raises(SystemExit):
        irs.frozen_spec(args, "other-scoring", create=False)


def test_predict_refuses_weeks_before_the_preregistered_start(irs, args):
    args.week = 7
    with pytest.raises(SystemExit, match="pre-registered from 2026 week 8"):
        irs.cmd_predict(args)


# ----------------------------------------------------------------------------- pregame / missed


def _kick(hours: float) -> dict:
    return {t: SUN + timedelta(hours=hours) for t in ("PHI", "DAL", "KC", "BUF")}


def _fake_predict(calls: list):
    def predict(a, season, week):
        calls.append(week)
        p = Path(a.ifp_dir) / "research" / "injury_return_shadow" / "ledger" / f"s{season}w{week:02d}-pregame.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"rows": []}))
        return {"ledger": str(p), "written": True}
    return predict


def test_pregame_freezes_once_when_the_slate_is_imminent(irs, args):
    calls = []
    early = irs.run_pregame(args, 2026, 8, now=SUN - timedelta(hours=20), kickoffs=_kick(1),
                            predict=_fake_predict(calls))
    assert early["status"] == "deferred" and calls == []
    rec = irs.run_pregame(args, 2026, 8, now=SUN, kickoffs=_kick(1), predict=_fake_predict(calls))
    assert rec["status"] == "frozen" and calls == [8] and Path(rec["record"]).exists()
    again = irs.run_pregame(args, 2026, 8, now=SUN, kickoffs=_kick(1), predict=_fake_predict(calls))
    assert again["status"] == "already_frozen" and calls == [8]


def test_pregame_before_week_8_and_failures_never_raise(irs, args):
    calls = []
    assert irs.run_pregame(args, 2026, 7, now=SUN, kickoffs=_kick(1),
                           predict=_fake_predict(calls))["status"] == "before_prospective_start"

    def boom(a, s, w):
        raise RuntimeError("nflverse unreachable")
    rec = irs.run_pregame(args, 2026, 9, now=SUN, kickoffs=_kick(1), predict=boom)
    assert rec["status"] == "failed" and "nflverse unreachable" in rec["warnings"][0] and calls == []
    args.write = False
    assert irs.run_pregame(args, 2026, 10, now=SUN, kickoffs=_kick(1), predict=boom)["status"] == "would_freeze"


def test_missed_week_is_recorded_once_and_never_before_the_start(irs, args):
    assert irs.check_missed(args, 2026, 7, now=SUN + timedelta(days=2), kickoffs=_kick(1)) is None
    assert irs.check_missed(args, 2026, 8, now=SUN, kickoffs=_kick(1)) is None       # not all kicked off
    rec = irs.check_missed(args, 2026, 8, now=SUN + timedelta(days=2), kickoffs=_kick(1))
    assert rec["week"] == 8
    assert (Path(args.ifp_dir) / "research" / "injury_return_shadow" / "missed" / "s2026w08.json").exists()


# ----------------------------------------------------------------------------- score + the one-look gate


def _world(irs, args, weeks, *, gain: float, overall_cost: float = 0.0):
    """Ledgers for `weeks` (2026) and the labeled store: per week 4 returners (RB/WR), 4 play-through rows and 12
    normal rows. Candidates are `gain` closer to the label on injury-exit rows and `overall_cost` worse elsewhere."""
    d = Path(args.ifp_dir) / "research" / "injury_return_shadow" / "ledger"
    d.mkdir(parents=True, exist_ok=True)
    lab = []
    for w in weeks:
        rows = []
        for i in range(20):
            pos = ("RB", "WR", "TE")[i % 3]
            ret, exit_ = i < 4, i < 8
            label = 10.0 + (i % 5)
            base = label + 2.0
            cand = base - gain if exit_ else base + overall_cost
            rows.append({"gsis_id": f"p{i}", "position": pos, "team": "PHI", "base_points": base,
                         **{f"{irs.SHORT[m]}_points": cand for m in irs.MODELS}, "ret": ret,
                         "inputs": {"injury_exit_last": 1 if exit_ else 0, "games_missed": 2 if ret else 0}})
            lab.append({"key": 202600 + w, "gsis_id": f"p{i}", "position": pos, "label": label})
        (d / f"s2026w{w:02d}-pregame.json").write_text(json.dumps({"season": 2026, "week": w, "rows": rows}))
    return pl.DataFrame(lab)


def test_gate_accumulates_before_the_look_and_never_decides_early(irs, args):
    store = _world(irs, args, range(8, 17), gain=1.0)                     # weeks 8-16 final: before the look
    res = irs.score(args, store, write=True)
    for n in irs.CANDIDATES:
        g = res["candidates"][n]["gate"]
        assert g["status"] == irs.ACCUMULATING and g["interim"] and all(g["checks"].values())
    assert res["candidates"][irs.IRS]["cohort"]["n"] == 9 * 8 and res["candidates"][irs.IRB]["cohort"]["n"] == 9 * 4
    assert not list((Path(args.ifp_dir) / "research" / "injury_return_shadow").glob("decision_*.json"))


def test_the_look_passes_once_and_the_decision_is_frozen(irs, args):
    store = _world(irs, args, range(8, 19), gain=1.0)                     # weeks 8-18: the look
    res = irs.score(args, store, write=True)
    g = res["candidates"][irs.IRS]["gate"]
    assert g["status"] == irs.PASS and all(g["checks"].values()) and g["weeks"] == 11
    assert res["candidates"][irs.IRS]["slices"]["play_through"]["diff"] == pytest.approx(-1.0)
    # the IRB cohort has 44 rows >= MIN_COHORT: also decided
    assert res["candidates"][irs.IRB]["gate"]["status"] == irs.PASS
    assert res["both"]["choice"]["ship"] in (irs.BOTH, irs.IRS, irs.IRB)
    # later, worse evidence never re-opens it
    store2 = _world(irs, args, range(8, 19), gain=-3.0)
    again = irs.score(args, store2, write=True)
    assert again["candidates"][irs.IRS]["gate"]["status"] == irs.PASS
    assert again["candidates"][irs.IRS]["gate"]["decided_at"] == g["decided_at"]


def test_the_look_fails_when_overall_mae_gets_worse(irs, args):
    store = _world(irs, args, range(8, 19), gain=1.0, overall_cost=1.0)
    g = irs.score(args, store, write=True)["candidates"][irs.IRS]["gate"]
    assert g["status"] == irs.FAIL and g["checks"]["cohort_ci_below_0"] and not g["checks"]["overall_not_worse"]


def test_the_look_is_deferred_while_the_minimums_are_unmet(irs, args):
    store = _world(irs, args, [8, 9, 10, 18], gain=1.0)                   # week 18 final but only 4 weeks scored
    g = irs.score(args, store, write=True)["candidates"][irs.IRS]["gate"]
    assert g["status"] == irs.ACCUMULATING and "4 of 8 weeks" in g["note"]


def test_run_score_writes_scores_and_a_decided_shadow_stops_freezing(irs, args):
    store = _world(irs, args, range(8, 19), gain=1.0)
    rec = irs.run_score(args, now=SUN, scorer=lambda a: irs.score(a, store, write=True))
    assert rec["status"] == "scored" and rec["gate"] == {irs.IRS: irs.PASS, irs.IRB: irs.PASS}
    saved = json.loads((Path(args.ifp_dir) / "research" / "injury_return_shadow" / "scores.json").read_text())
    assert saved["candidates"][irs.IRS]["gate"]["status"] == irs.PASS
    calls = []
    rec = irs.run_pregame(args, 2027, 1, now=SUN, kickoffs=_kick(1), predict=_fake_predict(calls))
    assert rec["status"] == "decided"
    assert calls == [] and irs.check_missed(args, 2027, 1, now=SUN + timedelta(days=2), kickoffs=_kick(1)) is None
