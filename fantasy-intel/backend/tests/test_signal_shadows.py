"""The three pre-registered prospective shadows (ifp-fr-p1, ifp-dct-p1, ifp-awd-p1) and scripts/signal_shadows.py:
the decline-only role, point-in-time FTN / depth capture, frozen specs, the freeze rule, write-once / idempotent
ledgers, failure isolation and scoring with challenger.SHADOW_GATE. Synthetic data only; nothing writes a database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import random
import sys

import polars as pl
import pytest

from app.analytics import adaptive_workload as aw
from app.analytics import challenger as ch
from app.analytics import usage_signals as us
from app.analytics import weekly_learning as wl

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
UTC = timezone.utc
SUN = datetime(2026, 10, 25, 13, 0, tzinfo=UTC)          # 1h before a 10:00 ET (14:00 UTC) week-7 slate


@pytest.fixture(scope="module")
def ss():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("signal_shadows", SCRIPTS / "signal_shadows.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def args(ss, tmp_path):
    return ss.parse(["pregame", "--season", "2026", "--ifp-dir", str(tmp_path / "models"),
                     "--live-db", str(tmp_path / "live.duckdb"), "--history-db", str(tmp_path / "hist.duckdb"),
                     "--weekly-dir", str(tmp_path / "weekly"), "--exits-dir", str(tmp_path / "weekly")])


# ----------------------------------------------------------------------------- decline-only role (ifp-awd-p1)


def _g(week, xfpl, cls="normal", season=2025):
    return {"season": season, "week": week, "team": "AAA", "xfpl": xfpl, "f": 1.0, "info_class": cls}


def _role(games, cfg):
    rows = sorted(games, key=lambda r: (r["season"], r["week"]), reverse=True)
    return aw._role_one(rows, 2025, 10.0, "AAA", cfg)


def test_decline_only_follows_a_persistent_drop_exactly_like_the_adaptive_blend():
    games = [_g(1, 12), _g(2, 12), _g(3, 12), _g(4, 4, "role_low"), _g(5, 4)]
    both, dec = _role(games, aw.RoleConfig()), _role(games, aw.DECLINE_CONFIG)
    assert dec["aw_streak"] == both["aw_streak"] == -2 and dec["aw_r"] == both["aw_r"] == -0.5
    assert dec["aw_role"] == pytest.approx(both["aw_role"]) and dec["aw_role"] < dec["aw_eb"]


def test_decline_only_never_blends_toward_a_streak_above_the_role():
    games = [_g(1, 8), _g(2, 8), _g(3, 8), _g(4, 20), _g(5, 21)]
    both, dec = _role(games, aw.RoleConfig()), _role(games, aw.DECLINE_CONFIG)
    assert both["aw_streak"] == 2 and both["aw_role"] > both["aw_eb"]
    assert (dec["aw_streak"], dec["aw_r"]) == (0, 0.0) and dec["aw_role"] == dec["aw_eb"] == both["aw_eb"]


def test_decline_only_role_is_never_above_the_established_role():
    rng = random.Random(3)
    for _ in range(400):
        games = [_g(w, rng.choice([None, round(rng.uniform(0, 25), 1)]),
                    rng.choice(["normal", "role_low", "unknown", "blowout_low", "injury_exit_pbp"])) for w in range(1, 8)]
        r = _role(games, aw.DECLINE_CONFIG)
        if r["aw_eb"] is not None:
            assert r["aw_role"] <= r["aw_eb"] + 1e-9 and r["aw_streak"] <= 0


def test_decline_candidate_is_ifp2_with_only_xfpl_eb_swapped():
    cfg = ch.ChallengerConfig.load()
    assert aw.awd_features("QB", cfg) == cfg.features("QB")
    for pos in aw.AWD_POSITIONS:
        feats = aw.awd_features(pos, cfg)
        assert "xfpl_eb" not in feats and feats == [("aw_role_d" if f == "xfpl_eb" else f) for f in cfg.features(pos)]
    assert aw.DECLINE_CONFIG.tag() != aw.RoleConfig().tag()
    assert all(not c.decline_only for c in aw.CONFIGS.values())          # the rejected study is unchanged
    assert "AW_D" not in [c.name for c in aw.CANDIDATES]


# ----------------------------------------------------------------------------- FTN / depth pre-registration + capture


def test_prospective_signal_candidates_match_the_research_follow_up_plan():
    assert us.PROSPECTIVE["ifp-fr-p1"]["features"] == ["fr_team_share", "fr_own_share"]
    assert us.PROSPECTIVE["ifp-fr-p1"]["positions"] == ("RB", "WR")
    assert us.PROSPECTIVE["ifp-dct-p1"]["features"] == ["dc_tier"]
    assert us.PROSPECTIVE["ifp-dct-p1"]["positions"] == ("QB", "RB", "WR")
    rejected = {"dc_tier_change", "dc_vacancy", "dc_snap_gap", "dc_promoted", "dc_demoted", "fr_tpg", "des_tpg",
                "chk_tpg", "fr_share_trend", "fr_minus_ts"}
    assert not rejected & {f for p in us.PROSPECTIVE.values() for f in p["features"]}
    cfg = ch.ChallengerConfig.load()
    for pos in ch.POSITIONS:
        assert not {"fr_team_share", "fr_own_share", "dc_tier", "aw_role_d"} & set(cfg.features(pos))


def _plays(week: int, game: str, codes: list[tuple[str, str]]) -> list[dict]:
    return [{"season": 2026, "week": week, "season_type": "REG", "game_id": game, "play_id": float(i),
             "posteam": "AAA", "play_type": "pass", "pass_attempt": 1, "sack": 0, "two_point_attempt": 0,
             "receiver_player_id": r, "air_yards": 8.0} for i, (r, _) in enumerate(codes, start=1)]


def _ftn_rows(week: int, game: str, codes, pulled: datetime) -> list[dict]:
    return [{"nflverse_game_id": game, "nflverse_play_id": i, "season": 2026, "week": week, "read_thrown": c,
             "date_pulled": pulled} for i, (_, c) in enumerate(codes, start=1)]


def test_live_ftn_features_use_only_charting_pulled_before_the_forecast():
    w5 = [("r1", "1"), ("r1", "1"), ("r1", "2"), ("r2", "1")]
    w6 = [("r1", "1"), ("r1", "1"), ("r1", "1"), ("r2", "CHK")]
    pbp = pl.DataFrame(_plays(5, "2026_05_AAA_BBB", w5) + _plays(6, "2026_06_AAA_CCC", w6))
    as_of = datetime(2026, 10, 18, 12, tzinfo=UTC)
    ftn = pl.DataFrame(_ftn_rows(5, "2026_05_AAA_BBB", w5, as_of - timedelta(days=4))
                       + _ftn_rows(6, "2026_06_AAA_CCC", w6, as_of + timedelta(hours=3))).with_columns(
        pl.col("nflverse_play_id").cast(pl.Int32))
    panel = pl.DataFrame({"season": [2026] * 4, "week": [5, 5, 6, 6], "gsis_id": ["r1", "r2", "r1", "r2"],
                          "position": ["WR"] * 4, "team": ["AAA"] * 4})
    targets = pl.DataFrame({"gsis_id": ["r1", "r2"], "position": ["WR", "WR"]})
    f, prov = us.live_ftn_features(pbp, ftn, panel, 2026, 7, targets, as_of)
    assert prov["previous_week_games_not_available"] == ["2026_06_AAA_CCC"]      # week 6 not pulled yet
    assert prov["ftn_rows_excluded_not_yet_pulled"] == 4
    assert dict(zip(f["gsis_id"], f["ftn_games"])) == {"r1": 1, "r2": 1}         # week 6 counts nothing, never zero
    only5, _ = us.live_ftn_features(pbp.filter(pl.col("week") == 5), ftn.filter(pl.col("week") == 5), panel,
                                    2026, 7, targets, as_of)
    assert f.sort("gsis_id").equals(only5.sort("gsis_id"))
    later, prov2 = us.live_ftn_features(pbp, ftn, panel, 2026, 7, targets, as_of + timedelta(days=1))
    assert prov2["previous_week_games_not_available"] == [] and later["ftn_games"].to_list() == [2, 2]


def _depth(dt: str, ranks: dict[str, int]) -> list[dict]:
    return [{"dt": dt, "team": "AAA", "pos_grp": "3WR 1TE", "pos_abb": "QB", "gsis_id": g, "pos_rank": r}
            for g, r in ranks.items()]


def test_live_depth_features_never_read_a_snapshot_after_the_freeze():
    raw = pl.DataFrame(_depth("2026-10-24T08:00:00Z", {"q1": 1, "q2": 2}) + _depth("2026-10-25T14:00:00Z", {"q1": 2, "q2": 1}))
    games = pl.DataFrame({"season": [2026], "week": [7], "team": ["AAA"],
                          "kickoff": [datetime(2026, 10, 25, 17, 0, tzinfo=UTC)]})
    tgt = pl.DataFrame({"season": [2026, 2026], "week": [7, 7], "gsis_id": ["q1", "q2"], "position": ["QB", "QB"],
                        "team": ["AAA", "AAA"]})
    dc, prov = us.live_depth_features(raw, games, tgt, 1.5, datetime(2026, 10, 25, 13, 0, tzinfo=UTC))
    assert dict(zip(dc["gsis_id"], dc["dc_tier"])) == {"q1": 1.0, "q2": 2.0}     # the 14:00 snapshot did not exist
    assert prov["latest_snapshot"].startswith("2026-10-24 08:00")
    dc, _ = us.live_depth_features(raw, games, tgt, 1.5, datetime(2026, 10, 25, 15, 0, tzinfo=UTC))
    assert dict(zip(dc["gsis_id"], dc["dc_tier"])) == {"q1": 2.0, "q2": 1.0}


# ----------------------------------------------------------------------------- specs


def test_candidate_features_and_frozen_specs(ss, args):
    cfg = ch.ChallengerConfig.load()
    fr, dct, awd = (ss.candidate_features(cfg, n) for n in ss.CANDIDATES)
    assert sorted(fr) == ["RB", "WR"] and fr["WR"] == [*cfg.features("WR"), "fr_team_share", "fr_own_share"]
    assert sorted(dct) == ["QB", "RB", "WR"] and dct["QB"] == [*cfg.features("QB"), "dc_tier"]
    assert sorted(awd) == ["RB", "TE", "WR"]
    specs = ss.frozen_specs(args, cfg, "fp1")
    assert set(specs) == set(ss.CANDIDATES) and all(s["ifp2_config_hash"] == cfg.hash() for s in specs.values())
    assert specs[ss.FR]["fit"]["WR"]["lam"] == cfg.positions["WR"]["lam"]
    assert ss.frozen_specs(args, cfg, "fp1") == specs                             # idempotent, never rewritten
    with pytest.raises(SystemExit, match="different spec"):
        ss.frozen_specs(args, cfg, "fp2")                                          # other league scoring
    other = ch.ChallengerConfig(**{**cfg.__dict__, "positions": {**cfg.positions, "WR": {**cfg.positions["WR"],
                                                                                         "lam": 0.3}}})
    with pytest.raises(SystemExit, match="different spec"):
        ss.frozen_specs(args, other, "fp1")                                        # a changed ifp-2 penalty


# ----------------------------------------------------------------------------- freeze rule


def _kick(n_early: int, n_late: int, hours_early: float = 1.0, hours_late: float = 48.0) -> dict:
    return {**{f"E{i}": SUN + timedelta(hours=hours_early) for i in range(n_early)},
            **{f"L{i}": SUN + timedelta(hours=hours_late) for i in range(n_late)}}


def test_freeze_rule(ss):
    assert ss.freeze_decision(_kick(0, 28), SUN)["action"] == "defer"              # Thursday-like: far away
    assert ss.freeze_decision(_kick(2, 26), SUN)["action"] == "defer"              # only TNF is imminent
    d = ss.freeze_decision(_kick(20, 6), SUN)
    assert d["action"] == "freeze" and d["imminent_teams"] == 20
    started = {"S0": SUN - timedelta(hours=60), "S1": SUN - timedelta(hours=60)}
    assert ss.freeze_decision({**started, **_kick(14, 12)}, SUN)["open_teams"] == 26
    assert ss.freeze_decision(started, SUN)["action"] == "nothing_open"
    u = ss.freeze_decision({**_kick(20, 6), "UNK": None}, SUN)
    assert u["action"] == "freeze" and "UNK" in u["warnings"][0]


# ----------------------------------------------------------------------------- pregame (fakes)


def fake_predict(calls, abstained=None):
    def predict(args, season, week):
        calls.append(week)
        path = args.ifp_dir / "research" / "signal_shadows" / "ledger" / f"s{season}w{week:02d}-pregame.json"
        written = wl.write_once(path, json.dumps({"season": season, "week": week, "rows": []}))
        return {"ledger": str(path), "written": written, "rows": 0, "abstained": abstained or {}}
    return predict


def fake_fetch(calls, warnings=()):
    def f(args, season):
        calls.append(season)
        return {"snapshots": {"depth_2026": {"sha": "abc", "rows": 1, "fetched": True}}, "warnings": list(warnings)}
    return f


def _runs(args):
    d = args.ifp_dir / "research" / "signal_shadows" / "runs"
    return sorted(d.glob("*.json")) if d.exists() else []


def test_pregame_before_the_prospective_window_does_nothing(ss, args):
    rec = ss.run_pregame(args, 2026, 6, now=SUN, kickoffs=_kick(20, 6), fetcher=lambda *a: pytest.fail("fetched"),
                         predict=lambda *a: pytest.fail("forecast"))
    assert rec["status"] == "before_prospective_start" and not _runs(args)


def test_pregame_defers_far_from_the_slate_and_freezes_once_when_it_is_imminent(ss, args):
    f, p = [], []
    rec = ss.run_pregame(args, 2026, 7, now=SUN, kickoffs=_kick(2, 26), fetcher=fake_fetch(f), predict=fake_predict(p))
    assert rec["status"] == "deferred" and f == p == [] and len(_runs(args)) == 1
    rec = ss.run_pregame(args, 2026, 7, now=SUN, kickoffs=_kick(20, 6), fetcher=fake_fetch(f, ["pbp_2026 fetch failed"]),
                         predict=fake_predict(p, {"ifp-fr-p1": "raw ftn/pbp snapshots incomplete"}))
    assert rec["status"] == "frozen" and f == [2026] and p == [7] and rec["ledger_sha256"]
    assert "pbp_2026 fetch failed" in rec["warnings"] and any("ifp-fr-p1 abstained" in w for w in rec["warnings"])
    assert sorted(json.loads(r.read_text())["status"] for r in _runs(args)) == ["deferred", "frozen"]
    again = ss.run_pregame(args, 2026, 7, now=SUN + timedelta(minutes=30), kickoffs=_kick(20, 6),
                           fetcher=fake_fetch(f), predict=fake_predict(p))
    assert again["status"] == "already_frozen" and p == [7] and f == [2026] and len(_runs(args)) == 2


def test_pregame_failure_is_recorded_never_raised(ss, args):
    def refuse(args, season, week):
        raise SystemExit("every week-7 game has kicked off: no pregame forecast possible")

    rec = ss.run_pregame(args, 2026, 7, now=SUN, kickoffs=_kick(20, 6), fetcher=fake_fetch([]), predict=refuse)
    assert rec["status"] == "failed" and "kicked off" in rec["error"] and rec["warnings"]
    assert [json.loads(r.read_text())["status"] for r in _runs(args)] == ["failed"]
    assert not ss.ledger_path(args, 2026, 7).exists()


def test_dry_run_decides_only(ss, tmp_path):
    a = ss.parse(["pregame", "--season", "2026", "--ifp-dir", str(tmp_path / "m"), "--now", SUN.isoformat()])
    rec = ss.run_pregame(a, 2026, 7, now=SUN, kickoffs=_kick(20, 6), fetcher=lambda *x: pytest.fail("fetched"),
                         predict=lambda *x: pytest.fail("forecast"))
    assert rec["status"] == "would_freeze" and not (tmp_path / "m").exists()


def test_missed_week_is_recorded_once_and_never_backfilled(ss, args):
    started = {t: SUN - timedelta(hours=1) for t in ("AAA", "BBB")}
    rec = ss.check_missed(args, 2026, 7, now=SUN, kickoffs=started)
    assert rec and (args.ifp_dir / "research" / "signal_shadows" / "missed" / "s2026w07.json").exists()
    assert ss.check_missed(args, 2026, 6, now=SUN, kickoffs=started) is None           # before the window
    assert ss.check_missed(args, 2026, 8, now=SUN, kickoffs=_kick(20, 6)) is None      # not started yet
    fake_predict([])(args, 2026, 9)
    assert ss.check_missed(args, 2026, 9, now=SUN, kickoffs=started) is None          # frozen in time


# ----------------------------------------------------------------------------- raw snapshots


def test_fetch_downloads_history_once_refreshes_the_season_and_survives_failures(ss, args):
    calls = []

    def loader(family, season):
        calls.append((family, season))
        return pl.DataFrame({"x": [season, len(calls)]})

    out = ss.fetch(args, 2026, loader=loader)
    assert len(calls) == 5 + 5 + 2 and not out["warnings"] and all(ss.sources_complete(args, 2026).values())
    calls.clear()
    ss.fetch(args, 2026, refresh_current=False, loader=loader)
    assert calls == []
    out = ss.fetch(args, 2026, loader=loader)
    assert sorted(calls) == [("depth", 2026), ("ftn", 2026), ("pbp", 2026)]
    assert out["snapshots"]["ftn_2026"]["fetched"] and not out["snapshots"]["ftn_2025"]["fetched"]

    def down(family, season):
        raise OSError("nflverse unreachable")

    out = ss.fetch(args, 2026, loader=down)
    assert len(out["warnings"]) == 3 and all("using the stored snapshot" in w for w in out["warnings"])
    assert out["snapshots"]["depth_2026"]["sha"]                                   # the stored one is kept
    out = ss.fetch(args, 2027, loader=down)
    assert any("ftn_2027" in w and "no snapshot stored" in w for w in out["warnings"])
    assert not ss.sources_complete(args, 2027)[ss.FR] and ss.sources_complete(args, 2027)[ss.AWD]


# ----------------------------------------------------------------------------- scoring and the gate


def _evidence(weeks=8, n=60, better=True, positions=("RB", "WR"), wr_rows=30):
    """Identical rows of ifp-2 and a candidate. Candidate errors: 80% within +-0.5 and 20% at +-3 (MAE 1.0,
    exactly 80% inside a +-2.5 band) vs ifp-2 at +-1.5; signs alternate, so both are unbiased."""
    rng = random.Random(5)
    rows = []
    for w in range(1, weeks + 1):
        for pos in positions:
            for i in range(n if pos == "RB" else wr_rows):
                y = rng.uniform(2, 20)
                s = 1 if i % 2 else -1
                e = (0.5 if i % 5 else 3.0) * s if better else (3.0 if i % 5 else 0.5) * s
                rows.append({"key": 202600 + w, "season": 2026, "week": w, "gsis_id": f"{pos}{i}", "position": pos,
                             "label": y, "pred_A1": float(n - i), "pred_ifp2": y + 1.5 * s, "pred_fr": y + e})
    return pl.DataFrame(rows)


BANDS = {p: {"bands": [{"low": None, "high": None, "q_low": -2.5, "q_high": 2.5}]} for p in ("QB", "RB", "WR", "TE")}


def test_gate_uses_the_shadow_gate_criteria(ss):
    res = ss.gate_report(_evidence(), ss.FR, ["RB", "WR"], BANDS)
    rb = res["report"]["positions"]["RB"]
    assert rb["n"] == 480 and rb["interval80_coverage"]["challenger"] == 0.8
    assert rb["challenger_minus_champion_mae"]["ci95"][1] < ch.SHADOW_GATE["mae_ci95_upper_below"]
    assert res["gate"]["RB"]["status"] == "eligible_for_review" and not res["gate"]["RB"]["blocking"]
    assert res["gate"]["WR"]["status"] == "insufficient_evidence"                  # 240 < 400 matched rows
    worse = ss.gate_report(_evidence(better=False), ss.FR, ["RB"], BANDS)
    assert worse["gate"]["RB"]["status"] == "failed_evaluation"
    few = ss.gate_report(_evidence(weeks=3), ss.FR, ["RB"], BANDS)
    assert few["gate"]["RB"]["status"] == "insufficient_evidence"                  # < 8 fully final weeks
    assert {c["name"] for c in few["gate"]["RB"]["criteria"]} == {
        "frozen_pregame_evidence", "prospective_weeks", "matched_appearances", "mae_vs_champion", "bias_guard",
        "starters_pool_mae", "interval_coverage"}


def test_score_joins_frozen_ledgers_to_fully_final_weeks_only(ss, args):
    ev = _evidence()
    cfg = ch.ChallengerConfig.load()
    ss.frozen_specs(args, cfg, "fp1")
    for w in range(1, 10):                       # week 9 is frozen but not final (absent from the store)
        rows = [{"gsis_id": r["gsis_id"], "position": r["position"], "team": "AAA", "ifp2_points": r["pred_ifp2"],
                 "fr_points": r["pred_fr"] if r["position"] in ("RB", "WR") else None, "dct_points": None,
                 "awd_points": r["pred_ifp2"], "inputs": {"ftn_games": 3.0, "aw_streak_d": -2 if r["gsis_id"] == "RB1"
                                                          else 0}}
                for r in ev.filter(pl.col("week") == min(w, 8)).iter_rows(named=True)]
        wl.write_once(ss.ledger_path(args, 2026, w), json.dumps({"season": 2026, "week": w, "rows": rows}))
    out = ss.score(args, store=ev.drop("pred_ifp2", "pred_fr"))
    assert out["scored_weeks"] == list(range(202601, 202609))
    fr = out["candidates"][ss.FR]
    assert fr["report"]["positions"]["RB"]["n"] == 480 and fr["cohort"]["n"] == 480 + 240
    assert out["candidates"][ss.DCT]["report"]["positions"]["QB"]["n"] == 0        # never forecast: no evidence
    assert out["candidates"][ss.DCT]["gate"]["QB"]["status"] == "insufficient_evidence"
    awd = out["candidates"][ss.AWD]
    assert awd["report"]["positions"]["RB"]["challenger_minus_champion_mae"]["diff"] == 0.0
    assert awd["cohort"]["name"] == "decline_regime" and awd["cohort"]["n"] == 8
    assert fr["alongside_production"] == {"n": 0}                                   # no weekly ledger here


def test_run_score_records_and_isolates_failures(ss, args):
    assert ss.run_score(args, now=SUN)["status"] == "no_ledgers" and not _runs(args)
    fake_predict([])(args, 2026, 7)

    def boom(a):
        raise MemoryError("store")

    rec = ss.run_score(args, now=SUN, scorer=boom)
    assert rec["status"] == "failed" and "MemoryError" in rec["error"] and len(_runs(args)) == 1
    rec = ss.run_score(args, now=SUN, scorer=lambda a: {"scored_weeks": [202607], "candidates": {
        ss.FR: {"gate": {"RB": {"status": "insufficient_evidence"}}}}})
    assert rec["status"] == "scored" and rec["gate"] == {ss.FR: {"RB": "insufficient_evidence"}}
    assert json.loads((args.ifp_dir / "research" / "signal_shadows" / "scores.json").read_text())["scored_weeks"]


# ----------------------------------------------------------------------------- end to end on the synthetic world


def _raw_sources(frames: dict, pulled_after: datetime) -> dict[str, pl.DataFrame]:
    """FTN / play-by-play / depth files shaped like nflverse's, from the synthetic 2025 season: one target play per
    target, read codes cycling "1", "2", "CHK"; week 9's charting is pulled only after `pulled_after`."""
    st = frames["nfl_weekly_stats"].filter((pl.col("season") == 2025) & (pl.col("targets") > 0))
    sched = frames["nfl_schedules"].filter(pl.col("season") == 2025)
    gid = {}
    for r in sched.iter_rows(named=True):
        gid[(r["week"], r["home_team"])] = gid[(r["week"], r["away_team"])] = r["game_id"]
    pbp, ftn = [], []
    for r in st.iter_rows(named=True):
        g = gid[(r["week"], r["team"])]
        for t in range(r["targets"]):
            pid = len(pbp) + 1
            pbp.append({"game_id": g, "play_id": float(pid), "season": 2025, "week": r["week"], "season_type": "REG",
                        "posteam": r["team"], "play_type": "pass", "pass_attempt": 1, "sack": 0, "two_point_attempt": 0,
                        "receiver_player_id": r["player_id"], "air_yards": 8.0})
            ftn.append({"nflverse_game_id": g, "nflverse_play_id": pid, "season": 2025, "week": r["week"],
                        "read_thrown": ("1", "2", "CHK")[pid % 3],
                        "date_pulled": pulled_after + timedelta(hours=2) if r["week"] == 9
                        else datetime(2025, 9, 1, tzinfo=UTC)})
    from synthetic_forecast import SLOTS, TEAMS, gsis
    depth = []
    for w in range(1, 11):
        for team in TEAMS:
            for slot, pos in SLOTS:
                rank = int(slot[-1]) if slot[-1].isdigit() else 1
                depth.append({"dt": f"2025-09-{w:02d}T06:00:00Z", "team": team, "pos_grp": "3WR 1TE", "pos_abb": pos,
                              "gsis_id": gsis(team, slot), "pos_rank": rank})
    return {"pbp": pl.DataFrame(pbp), "ftn": pl.DataFrame(ftn).with_columns(pl.col("nflverse_play_id").cast(pl.Int32)),
            "depth": pl.DataFrame(depth)}


def test_predict_freezes_all_candidates_point_in_time_and_write_once(ss, tmp_path, monkeypatch):
    from synthetic_forecast import add_league, add_participation, generate, write
    from app.db.connection import connect, init_db

    frames = add_participation(generate([2024, 2025], unfinished={2025: 10}))
    sched = frames["nfl_schedules"]
    first = sched.filter((pl.col("season") == 2025) & (pl.col("week") == 10))["game_id"][0]
    frames["nfl_schedules"] = sched.with_columns(pl.when(pl.col("game_id") == first).then(pl.lit("09:30"))
                                                 .otherwise(pl.col("gametime")).alias("gametime"))
    live, hist = tmp_path / "live.duckdb", tmp_path / "history.duckdb"
    for path, fr, source in ((live, frames, "nfl"), (hist, generate([2024]), "nfl_history")):
        con = connect(path)
        init_db(con)
        write(con, fr)
        con.execute("INSERT INTO sync_runs (run_id, source, started_at, finished_at, status, stats) VALUES "
                    "(?, ?, '2025-09-10 03:00:00', '2025-09-10 03:00:00', 'ok', ?)",
                    [f"synthetic-{source}", source, json.dumps({"season": 2025, "nfl_schedules": 1})])
        if path == live:
            add_league(con, 2025, 10)
        con.close()
    now = datetime(2025, 9, 10, 15, 0, tzinfo=UTC)          # the 09:30 ET game has started, the 13:00 ET ones not
    monkeypatch.setattr(ss, "FETCH_FROM", {"ftn": 2025, "pbp": 2025, "depth": 2025})
    a = ss.parse(["predict", "--season", "2025", "--week", "10", "--ifp-dir", str(tmp_path / "models"),
                  "--live-db", str(live), "--history-db", str(hist),
                  "--exits-dir", str(tmp_path / "weekly"), "--weekly-dir", str(tmp_path / "weekly")])
    raw = _raw_sources(frames, now)
    ss.fetch(a, 2025, loader=lambda family, season: raw[family])
    res = ss.cmd_predict(a, now=now)
    assert res["written"] and not res["abstained"] and res["open_teams"] == 6
    led = json.loads(ss.ledger_path(a, 2025, 10).read_text())
    assert len(led["teams_excluded_already_started"]) == 2
    assert {r["team"] for r in led["rows"]}.isdisjoint(led["teams_excluded_already_started"])
    by = {}
    for r in led["rows"]:
        by.setdefault(r["position"], []).append(r)
    assert all(r["fr_points"] is None for p in ("QB", "TE") for r in by[p])          # outside the candidate
    assert all(r["dct_points"] is None for r in by["TE"]) and all(r["awd_points"] is None for r in by["QB"])
    for pos, name in (("WR", "fr"), ("QB", "dct"), ("RB", "awd")):
        got = [r for r in by[pos] if r[f"{name}_points"] is not None]
        assert got and any(abs(r[f"{name}_points"] - r["ifp2_points"]) > 1e-6 for r in got)
    assert all(r["inputs"]["dc_tier"] in (1.0, 2.0, 3.0) for r in by["QB"])
    assert led["sources"]["ftn"]["previous_week_games_not_available"]               # week 9 charting not pulled yet
    assert led["sources"]["depth"]["latest_snapshot"].startswith("2025-09-10 06:00")
    assert set(led["sources"]["raw_snapshots"]) == {"ftn_2025", "pbp_2025", "depth_2025"}
    assert set(led["candidates"]) == set(ss.CANDIDATES) and "CC-BY-SA" in led["license"]
    assert all(datetime.fromisoformat(r["kickoff"]) > datetime.fromisoformat(led["created_at"]) for r in led["rows"])
    again = ss.cmd_predict(a, now=now)
    assert again["written"] is False and json.loads(ss.ledger_path(a, 2025, 10).read_text()) == led


def test_the_app_never_imports_the_shadow_automation():
    root = Path(__file__).resolve().parents[1] / "app"
    for path in root.rglob("*.py"):
        text = path.read_text()
        assert "import signal_shadows" not in text and "from signal_shadows" not in text, path
    src = (root / "analytics" / "projection_source.py").read_text()
    assert not any(n in src for n in ("ifp-fr-p1", "ifp-dct-p1", "ifp-awd-p1", "adaptive_workload", "usage_signals"))
