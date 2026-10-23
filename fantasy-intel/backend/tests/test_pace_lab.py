"""Pace and game-script lab (issue #211): synthetic fixtures only. Overtime, neutral and no-play drives, missing team
data, a franchise move across seasons, low-sample shrinkage, scenario arithmetic, the frozen backtest's determinism
and point-in-time discipline, the API contract and the owner-only league mount."""

import json
import re
from pathlib import Path

import duckdb
import polars as pl
import pytest

import synthetic_pace
from app.analytics import pace_backtest as bt
from app.analytics import pace_lab as pl_
from app.analytics.pace_lab import PaceLabError, PaceQuery, TeamGame, build_pace_lab
from app.db.connection import init_db

APP = Path(__file__).resolve().parent.parent / "app"


# ----------------------------------------------------------------------------- tiny hand-built databases


def _db(tmp_path, overtime_column=True):
    con = duckdb.connect(str(tmp_path / "pace.duckdb"))
    init_db(con)
    ot = ", overtime INT" if overtime_column else ""
    con.execute(f"""CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR, game_id VARCHAR,
                    gameday VARCHAR, home_team VARCHAR, away_team VARCHAR, home_score INT, away_score INT{ot},
                    spread_line DOUBLE, total_line DOUBLE)""")
    return con


def _game(con, season, week, home, away, hs, as_, ot=0, gameday=None):
    cols = [c[0] for c in con.execute("DESCRIBE nfl_schedules").fetchall()]
    row = {"season": season, "week": week, "game_type": "REG", "game_id": f"{season}_{week:02d}_{away}_{home}",
           "gameday": gameday or f"{season}-09-{week:02d}", "home_team": home, "away_team": away, "home_score": hs,
           "away_score": as_, "overtime": ot, "spread_line": -99.0, "total_line": 999.0}
    row = {k: v for k, v in row.items() if k in cols}
    con.execute(f"INSERT INTO nfl_schedules ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                list(row.values()))


def _tw(con, season, week, team, opp, plays, dropbacks=None, neutral=30, nrate=0.6):
    dropbacks = round(plays * 0.6) if dropbacks is None else dropbacks
    con.execute("INSERT INTO nfl_team_week (season, week, team, opponent, offense_plays, dropbacks, "
                "team_designed_rushes, team_targets, neutral_plays, neutral_pass_rate) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [season, week, team, opp, plays, dropbacks, plays - dropbacks, round(dropbacks * 0.85), neutral,
                 nrate if neutral else None])


def _dr(con, season, week, team, drives, plays):
    con.execute("INSERT INTO nfl_team_game_drives (season, week, game_id, team, drives, plays) VALUES (?,?,?,?,?,?)",
                [season, week, f"g{season}{week}{team}", team, drives, plays])


def _pair(con, season, week, home, away, hs, as_, hp, ap, ot=0, **kw):
    _game(con, season, week, home, away, hs, as_, ot)
    _tw(con, season, week, home, away, hp, **kw)
    _tw(con, season, week, away, home, ap, **kw)
    _dr(con, season, week, home, 11, hp)
    _dr(con, season, week, away, 11, ap)


# ----------------------------------------------------------------------------- overtime


def test_overtime_games_leave_per_game_volume_but_stay_in_per_play_rates(tmp_path):
    con = _db(tmp_path)
    _pair(con, 2025, 1, "KC", "BUF", 24, 20, 60, 60)
    _pair(con, 2025, 2, "KC", "BUF", 27, 24, 90, 88, ot=1, dropbacks=60)
    out = build_pace_lab(con, PaceQuery(team="KC"))
    s = out["team"]["summary"]
    assert s["plays_per_game"]["observed"] == 60.0 and s["plays_per_game"]["n"] == 1
    assert s["plays_per_game"]["excluded"] == 1 and s["overtime_games"] == 1
    assert s["pass_rate"]["n"] == 150                                   # both games' plays are in the rate
    assert out["coverage"]["overtime"] == 2 and out["league"]["plays_per_game"] == 60.0
    assert [g["status"] for g in out["team"]["games"]] == ["ok", "overtime"]


def test_without_a_stored_overtime_flag_overtime_is_unknown_and_reported(tmp_path):
    con = _db(tmp_path, overtime_column=False)
    _pair(con, 2025, 1, "KC", "BUF", 24, 20, 60, 60)
    _pair(con, 2025, 2, "KC", "BUF", 27, 24, 90, 88)
    out = build_pace_lab(con, PaceQuery(team="KC"))
    assert out["source"]["overtime_recorded"] is False
    assert out["coverage"]["overtime_unknown"] == 4 and out["coverage"]["overtime"] == 0
    assert out["team"]["summary"]["plays_per_game"]["n"] == 2


# ----------------------------------------------------------------------------- neutral and no-play drives


def test_a_game_with_no_neutral_play_is_missing_not_a_zero_pass_rate(tmp_path):
    con = _db(tmp_path)
    _pair(con, 2025, 1, "KC", "BUF", 24, 20, 60, 60)
    _pair(con, 2025, 2, "KC", "BUF", 45, 3, 60, 60, neutral=0)        # a blowout from the first drive
    out = build_pace_lab(con, PaceQuery(team="KC"))
    kc = out["team"]
    assert kc["games"][1]["neutral_pass_rate"] is None and kc["games"][1]["neutral_plays"] == 0
    npr = kc["summary"]["neutral_pass_rate"]
    assert npr["observed"] == 0.6 and npr["n"] == 30 and npr["missing"] == 1
    assert out["coverage"]["no_neutral_play"] == 2
    assert out["league"]["neutral_pass_rate"] == 0.6                   # not dragged toward 0


COLS = ["season_type", "week", "game_id", "play_id", "posteam", "defteam", "play_type", "two_point_attempt",
        "qb_kneel", "qb_scramble", "pass_attempt", "sack", "qb_hit", "qb_dropback", "pass", "rusher_player_id",
        "receiver_player_id", "yardline_100", "yards_gained", "success", "epa", "pass_oe", "wp",
        "half_seconds_remaining", "fixed_drive", "fixed_drive_result", "home_team", "location", "roof", "surface",
        "temp", "wind", "stadium_id", "field_goal_result", "kick_distance", "extra_point_result",
        "two_point_conv_result", "down", "interception", "fumble_lost", "posteam_score_post", "kicker_player_id"]
STR = {"season_type", "game_id", "posteam", "defteam", "play_type", "rusher_player_id", "receiver_player_id",
       "fixed_drive_result", "home_team", "location", "roof", "surface", "stadium_id", "field_goal_result",
       "extra_point_result", "two_point_conv_result", "kicker_player_id"}


def _play(i, team, opp, drive, play_type, wp=0.5, **kw):
    base = {c: None for c in COLS}
    base.update(season_type="REG", week=1, game_id="G1", play_id=float(i), posteam=team, defteam=opp,
                play_type=play_type, two_point_attempt=0.0, qb_kneel=0.0, qb_scramble=0.0, pass_attempt=0.0, sack=0.0,
                qb_hit=0.0, qb_dropback=0.0, **{"pass": 0.0}, yardline_100=50.0, yards_gained=3.0, success=0.0,
                epa=0.1, wp=wp, half_seconds_remaining=900.0, fixed_drive=float(drive), fixed_drive_result="Punt",
                home_team="KC", location="Home", down=1.0, interception=0.0, fumble_lost=0.0, posteam_score_post=0.0)
    if play_type == "run":
        base["rusher_player_id"] = "RB1"
    if play_type == "pass":
        base.update(pass_attempt=1.0, qb_dropback=1.0, receiver_player_id="WR1", **{"pass": 1.0})
    base.update(kw)
    return base


def test_no_play_drives_and_no_neutral_plays_through_the_real_derivations(tmp_path):
    """Synthetic play-by-play through the sync's own derive_pbp and derive_team_drives: KC's drives are real; BUF's only
    possessions are a kickoff it returned and a penalty-only snap (no scrimmage play), and every BUF play is outside
    the neutral window. BUF must read as no drive and no neutral pass rate, never as 0."""
    from app.nfl.kicking_pbp import derive_team_drives
    from app.nfl.sync import derive_pbp
    plays = [_play(1, "KC", "BUF", 1, "run"), _play(2, "KC", "BUF", 1, "pass"), _play(3, "KC", "BUF", 1, "pass"),
             _play(4, "BUF", "KC", 2, "kickoff"), _play(5, "BUF", "KC", 2, "no_play", wp=0.97),
             _play(6, "BUF", "KC", 3, "pass", wp=0.02), _play(7, "BUF", "KC", 3, "run", wp=0.02)]
    pbp = pl.DataFrame(plays, schema={c: (pl.String if c in STR else pl.Float64) for c in COLS})
    _, team_rows, _ = derive_pbp(pbp, 2025, {"RB1": "RB", "WR1": "WR"})
    drive_rows = derive_team_drives(pbp, 2025)
    con = _db(tmp_path)
    _game(con, 2025, 1, "KC", "BUF", 14, 3)
    for r in team_rows:
        con.execute("INSERT INTO nfl_team_week (season, week, team, opponent, offense_plays, dropbacks, "
                    "team_designed_rushes, team_targets, neutral_plays, neutral_pass_rate) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [2025, r["week"], r["team"], r["opponent"], r["offense_plays"], r["dropbacks"],
                     r["team_designed_rushes"], r["team_targets"], r["neutral_plays"], r["neutral_pass_rate"]])
    for r in drive_rows:
        con.execute("INSERT INTO nfl_team_game_drives (season, week, game_id, team, drives, plays) VALUES (?,?,?,?,?,?)",
                    [2025, r["week"], r["game_id"], r["team"], r["drives"], r["plays"]])
    by = {r["team"]: r for r in drive_rows}
    assert by["KC"]["drives"] == 1 and by["BUF"]["drives"] == 1        # drive 2 (kickoff + penalty) is not a drive
    out = build_pace_lab(con, PaceQuery(team="BUF"))
    g = out["team"]["games"][0]
    assert g["plays"] == 2 and g["drives"] == 1 and g["plays_per_drive"] == 2.0
    assert g["neutral_plays"] == 0 and g["neutral_pass_rate"] is None
    assert out["team"]["summary"]["neutral_pass_rate"]["observed"] is None
    kc = build_pace_lab(con, PaceQuery(team="KC"))["team"]["games"][0]
    assert kc["neutral_plays"] == 3 and kc["neutral_pass_rate"] == pytest.approx(2 / 3, abs=1e-3)


def test_a_game_without_a_drive_row_has_no_plays_per_drive(tmp_path):
    con = _db(tmp_path)
    _pair(con, 2025, 1, "KC", "BUF", 24, 20, 60, 60)
    con.execute("DELETE FROM nfl_team_game_drives WHERE team = 'BUF'")
    out = build_pace_lab(con, PaceQuery(team="BUF"))
    assert out["team"]["games"][0]["drives"] is None and out["team"]["games"][0]["plays_per_drive"] is None
    s = out["team"]["summary"]
    assert s["drives_per_game"]["n"] == 0 and s["drives_per_game"]["observed"] is None
    assert s["plays_per_drive"]["observed"] is None and out["coverage"]["no_drive"] == 1


# ----------------------------------------------------------------------------- missing team data


def test_a_final_game_without_team_data_is_missing_and_unscored_rows_do_not_count(tmp_path):
    con = _db(tmp_path)
    _pair(con, 2025, 1, "KC", "BUF", 24, 20, 60, 60)
    _game(con, 2025, 2, "KC", "BUF", 30, 10)                          # final, no play-by-play stored
    _game(con, 2025, 3, "KC", "BUF", None, None)                      # scheduled, not played
    _tw(con, 2025, 3, "KC", "BUF", 99)                                 # a stray row for an unscored game
    out = build_pace_lab(con, PaceQuery(team="KC"))
    rows = out["team"]["games"]
    assert [r["week"] for r in rows] == [1, 2] and rows[1]["status"] == "missing_pbp" and rows[1]["plays"] is None
    s = out["team"]["summary"]
    assert s["plays_per_game"]["observed"] == 60.0 and s["plays_per_game"]["missing"] == 1
    assert s["missing_pbp"] == 1
    assert out["coverage"]["missing_pbp"] == 2 and out["coverage"]["not_final"] == 1


def test_a_game_dated_after_the_last_sync_is_not_presumed_final(tmp_path):
    con = _db(tmp_path)
    _pair(con, 2025, 1, "KC", "BUF", 24, 20, 60, 60)
    _pair(con, 2025, 2, "KC", "BUF", 10, 7, 30, 30)
    con.execute("UPDATE nfl_schedules SET gameday = '2025-09-14' WHERE week = 2")
    con.execute("INSERT INTO sync_runs (run_id, source, started_at, finished_at, status) "
                "VALUES ('r', 'nfl', '2025-09-14 18:00:00', '2025-09-14 18:30:00', 'ok')")
    out = build_pace_lab(con, PaceQuery(team="KC"))
    assert out["coverage"]["not_final"] == 2 and out["team"]["summary"]["plays_per_game"]["n"] == 1


def test_no_stored_play_by_play_is_no_data(tmp_path):
    con = duckdb.connect(str(tmp_path / "empty.duckdb"))
    init_db(con)
    out = build_pace_lab(con, PaceQuery())
    assert out["status"] == "no_data" and out["contract"] == pl_.CONTRACT
    assert out["backtest"]["status"] == "frozen"


# ----------------------------------------------------------------------------- multi-season franchise change


def test_a_relocated_franchise_pools_across_seasons_under_one_code(tmp_path):
    con = duckdb.connect(str(tmp_path / "s.duckdb"))
    init_db(con)
    synthetic_pace.write(con, (2024, 2025))
    assert con.execute("SELECT count(*) FROM nfl_team_week WHERE team = 'OAK' AND season = 2024").fetchone()[0] > 0
    out = build_pace_lab(con, PaceQuery(team="LV", span=2))
    assert out["query"]["seasons"] == [2024, 2025] and "OAK" not in out["teams"]
    assert out["team"]["listed_as"] == ["LV", "OAK"]
    assert [list(p) for p in out["source"]["franchise_codes"]] == [["OAK", "LV"]]
    assert out["team"]["summary"]["games"] == 34
    assert all(r["opponent"] != "OAK" for t in ("KC", "BUF") for r in
               build_pace_lab(con, PaceQuery(team=t, span=2))["team"]["games"])
    one = build_pace_lab(con, PaceQuery(season=2025, team="LV"))
    assert one["team"]["summary"]["games"] == 17 and one["source"]["franchise_codes"] == []
    with pytest.raises(PaceLabError):
        build_pace_lab(con, PaceQuery(team="OAK", season=2023))


def test_schedule_codes_and_play_by_play_codes_join_when_they_differ(tmp_path):
    """Real nflverse 2019: schedules say OAK while the team-week rows already say LV."""
    con = _db(tmp_path)
    _game(con, 2019, 1, "OAK", "DEN", 24, 16)
    _tw(con, 2019, 1, "LV", "DEN", 64)
    _tw(con, 2019, 1, "DEN", "LV", 58)
    out = build_pace_lab(con, PaceQuery(team="LV"))
    g = out["team"]["games"][0]
    assert g["status"] == "ok" and g["margin"] == 8 and g["script"] == "competitive" and g["listed_as"] == "OAK"
    assert out["coverage"]["missing_pbp"] == 0 and out["coverage"]["not_final"] == 0


# ----------------------------------------------------------------------------- shrinkage


def test_low_samples_shrink_toward_the_league_with_the_stated_prior():
    one = pl_.mean_estimate([80.0], 60.0, pl_.PRIOR_GAMES, "games")
    assert one["observed"] == 80.0 and one["estimate"] == 64.0 and one["data_weight"] == 0.2
    none = pl_.mean_estimate([], 60.0, pl_.PRIOR_GAMES, "games")
    assert none["observed"] is None and none["estimate"] == 60.0 and none["data_weight"] == 0.0
    many = pl_.mean_estimate([80.0] * 36, 60.0, pl_.PRIOR_GAMES, "games")
    assert many["estimate"] == 78.0 and many["data_weight"] == 0.9
    rate = pl_.rate_estimate(30, 30, 0.55, pl_.PRIOR_NEUTRAL_PLAYS, "neutral plays")
    assert rate["observed"] == 1.0 and rate["estimate"] == pytest.approx((30 + 150 * 0.55) / 180, abs=1e-3)
    assert pl_.rate_estimate(0, 0, 0.55, 150, "x")["observed"] is None


def test_a_team_with_one_game_in_the_window_is_mostly_prior(tmp_path):
    con = duckdb.connect(str(tmp_path / "s.duckdb"))
    init_db(con)
    synthetic_pace.write(con, (2025,))
    out = build_pace_lab(con, PaceQuery(team="KC", through_week=1))
    p = out["team"]["summary"]["plays_per_game"]
    assert p["n"] == 1 and p["data_weight"] == 0.2
    assert abs(p["estimate"] - p["prior"]) < abs(p["observed"] - p["prior"])
    s = out["team"]["scripts"]
    unseen = [k for k, v in s.items() if v["games"] == 0]
    assert unseen and all(s[k]["plays_per_game"]["observed"] is None and s[k]["plays_per_game"]["data_weight"] == 0
                          for k in unseen)


def test_windows_and_opponent_filters(tmp_path):
    con = duckdb.connect(str(tmp_path / "s.duckdb"))
    init_db(con)
    synthetic_pace.write(con, (2025,))
    assert build_pace_lab(con, PaceQuery(team="DET", window="last4"))["team"]["summary"]["games"] == 4
    last8 = build_pace_lab(con, PaceQuery(team="DET", window="last8", through_week=10))
    assert [g["week"] for g in last8["team"]["games"]] == list(range(3, 11))
    rival = build_pace_lab(con, PaceQuery(team="DET"))["team"]["games"][4]["opponent"]
    opp = build_pace_lab(con, PaceQuery(team="DET", opponent=rival))
    assert all(g["opponent"] == rival for g in opp["team"]["games"]) and opp["team"]["games"]
    m = opp["matchup"]
    assert m["opponent"] == rival and m["head_to_head_games"] == len(opp["team"]["games"]) and m["additive_plays"] == pytest.approx(
        m["team_plays_per_game"]["estimate"] + m["opponent_plays_allowed"]["estimate"] - m["league_plays_per_game"],
        abs=0.11)
    with pytest.raises(PaceLabError):
        build_pace_lab(con, PaceQuery(team="DET", opponent="DET"))


# ----------------------------------------------------------------------------- scenario arithmetic


def _scripts(plays, rates):
    return {s: {"plays_per_game": {"estimate": p}, "pass_rate": {"estimate": r}} for s, p, r in
            zip(pl_.SCRIPTS, plays, rates)}


def test_scenario_central_estimates_are_weighted_sums():
    per = _scripts((60.0, 56.0, 70.0), (0.5, 0.4, 0.7))
    pools = {s: [-2.0, 0.0, 2.0] for s in pl_.SCRIPTS}
    mix = pl_.normalize_mix((50, 25, 25))
    assert mix == (0.5, 0.25, 0.25)
    out = pl_.mixture(mix, per, pools, 0.8)["measures"]
    assert out["plays"]["expected"] == pytest.approx(0.5 * 60 + 0.25 * 56 + 0.25 * 70, abs=0.05)
    assert out["dropbacks"]["expected"] == pytest.approx(0.5 * 30 + 0.25 * 22.4 + 0.25 * 49, abs=0.05)
    assert out["designed_rushes"]["expected"] == pytest.approx(0.5 * 30 + 0.25 * 33.6 + 0.25 * 21, abs=0.05)
    assert out["targets"]["expected"] == pytest.approx(out["dropbacks"]["expected"] * 0.8, abs=0.05)
    pure = pl_.mixture((0.0, 1.0, 0.0), per, pools, 0.8)["measures"]["plays"]
    assert (pure["expected"], pure["low"], pure["high"]) == (56.0, 54.0, 58.0)


def test_scenario_weights_are_normalized_and_validated():
    assert pl_.normalize_mix((2, 1, 1)) == (0.5, 0.25, 0.25)
    for bad in ((0, 0, 0), (-1, 1, 1), (1, 1)):
        with pytest.raises(PaceLabError):
            pl_.normalize_mix(bad)


def test_a_weighted_script_without_a_spread_has_no_range_but_keeps_its_center():
    per = _scripts((60.0, 56.0, 70.0), (0.5, 0.4, 0.7))
    out = pl_.mixture((0.5, 0.5, 0.0), per, {"competitive": [-1.0, 1.0], "leading": [], "trailing": []}, None)
    assert out["measures"]["plays"]["expected"] == 58.0 and out["measures"]["plays"]["low"] is None
    assert out["measures"]["targets"]["expected"] is None and "leading" in out["range_reason"]


def test_weighted_quantile():
    pts = [(1.0, 0.1), (2.0, 0.4), (3.0, 0.4), (4.0, 0.1)]
    assert pl_.weighted_quantile(pts, 0.1) == 1.0 and pl_.weighted_quantile(pts, 0.5) == 2.0
    assert pl_.weighted_quantile(pts, 0.9) == 3.0 and pl_.weighted_quantile([], 0.5) is None


def test_the_scenario_changes_against_the_observed_mix_and_says_it_is_hypothetical(tmp_path):
    con = duckdb.connect(str(tmp_path / "s.duckdb"))
    init_db(con)
    synthetic_pace.write(con, (2025,))
    base = build_pace_lab(con, PaceQuery(team="SEA"))["scenario"]
    assert base["hypothetical"] is True and base["input_mix"] is None and base["baseline_source"] == "observed"
    assert base["mix"] == base["baseline_mix"] and base["change"]["plays"]["expected"] == 0.0
    trail = build_pace_lab(con, PaceQuery(team="SEA", mix=(0, 0, 100)))["scenario"]
    lead = build_pace_lab(con, PaceQuery(team="SEA", mix=(0, 100, 0)))["scenario"]
    assert trail["mix"] == [0.0, 0.0, 1.0]
    assert trail["result"]["measures"]["plays"]["expected"] > lead["result"]["measures"]["plays"]["expected"]
    assert trail["result"]["measures"]["dropbacks"]["expected"] > lead["result"]["measures"]["dropbacks"]["expected"]
    r = trail["result"]["measures"]["plays"]
    assert r["low"] <= r["expected"] <= r["high"]
    assert trail["change"]["plays"]["expected"] == pytest.approx(
        r["expected"] - trail["baseline"]["measures"]["plays"]["expected"], abs=0.11)


# ----------------------------------------------------------------------------- backtest


def _games(tmp_path, seasons=(2024, 2025)):
    con = duckdb.connect(str(tmp_path / "bt.duckdb"))
    init_db(con)
    synthetic_pace.write(con, seasons)
    games, _ = pl_.load_games(con, seasons[0], seasons[-1])
    con.close()
    return games


def test_the_backtest_is_deterministic(tmp_path):
    games = _games(tmp_path)
    a, b = bt.walk_forward(games), bt.walk_forward(list(reversed(games)))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["spec_sha256"] == bt.spec_hash() and a["plays"]["n"] > 0 and a["neutral_pass_rate"]["n"] > 0
    assert set(a["plays"]["verdicts"].values()) <= {"improves", "worse", "no clear difference"}


def test_the_backtest_never_sees_the_week_it_predicts(tmp_path):
    """Changing week 10's outcomes changes nothing predicted for weeks up to 10, and the targets of week 10 only."""
    games = _games(tmp_path, (2025,))
    changed = [TeamGame(**{**g.__dict__, "plays": g.plays + 40}) if g.week == 10 and g.plays else g for g in games]
    a, _, _ = bt.collect(games)
    b, _, _ = bt.collect(changed)
    pa = {(r["team"], r["week"]): r for r in a}
    pb = {(r["team"], r["week"]): r for r in b}
    for key, r in pa.items():
        if key[1] <= 10:
            assert pb[key]["pred"] == r["pred"], key
            assert pb[key]["actual"] == r["actual"] + (40 if key[1] == 10 else 0)
    assert any(pb[k]["pred"] != r["pred"] for k, r in pa.items() if k[1] == 11)


def test_backtest_skips_week_one_and_overtime_targets(tmp_path):
    games = _games(tmp_path, (2025,))
    plays, _, skipped = bt.collect(games)
    assert min(r["week"] for r in plays) == 2 and skipped["week_1"] == 32 and skipped["overtime"] == 2
    assert all(r["actual"] < 80 for r in plays)


def test_the_committed_result_matches_the_frozen_spec():
    """A changed spec must be a new version with a new result, never a silent rerun under the old one."""
    data = json.loads(pl_.BACKTEST_FILE.read_text())
    assert data["spec_sha256"] == bt.spec_hash() and data["version"] == bt.BACKTEST_VERSION
    assert data["plays"]["verdicts"]["shrunk_team"] in {"improves", "worse", "no clear difference"}


# ----------------------------------------------------------------------------- isolation and honesty guards


def test_no_betting_market_is_read():
    for name in ("pace_lab.py", "pace_backtest.py"):
        text = (APP / "analytics" / name).read_text()
        code = re.sub(r'"""[\s\S]*?"""', "", text)                       # docstrings may name what is NOT read
        for needle in ("spread_line", "total_line", "moneyline", "odds", "team_game_lines", "implied_total"):
            assert needle not in code, (name, needle)


def test_the_select_lists_never_include_betting_columns(tmp_path):
    con = _db(tmp_path)
    _pair(con, 2025, 1, "KC", "BUF", 24, 20, 60, 60)
    out = json.dumps(build_pace_lab(con, PaceQuery(team="KC")))
    assert "999" not in out and "-99" not in out


def test_the_app_never_imports_the_backtest_or_forecast_paths():
    for f in APP.rglob("*.py"):
        if f.name == "pace_backtest.py":
            continue
        text = f.read_text()
        assert not re.search(r"import pace_backtest|pace_backtest import|import .*\bpace_backtest\b", text), f
    lab = (APP / "analytics" / "pace_lab.py").read_text() + (APP / "api" / "pace_lab.py").read_text()
    for needle in ("projection", "production_forecasts", "model_registry", "start_sit", "waiver", "trade"):
        assert f"import {needle}" not in lab and f".{needle} import" not in lab, needle


# ----------------------------------------------------------------------------- API (classic site)


@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    path = tmp_path / "api.duckdb"
    con = duckdb.connect(str(path))
    init_db(con)
    synthetic_pace.write(con, (2024, 2025))
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    for name in ("FANTASY_API_KEY", "MULTILEAGUE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def test_api_contract_and_validation(api):
    r = api.get("/api/pace-lab")
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["contract"] == "pace-lab.v1" and body["status"] == "ok" and body["team"] is None
    assert len(body["overview"]) == 32 and body["backtest"]["status"] == "frozen"
    r = api.get("/api/pace-lab", params={"team": "lv", "seasons": 2, "mix_competitive": 20, "mix_leading": 60,
                                          "mix_trailing": 20, "opponent": "kc", "window": "last8"})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["team"]["team"] == "LV" and body["scenario"]["mix"] == [0.2, 0.6, 0.2]
    assert body["matchup"]["opponent"] == "KC"
    for params, status in (({"team": "ZZZ"}, 422), ({"team": "K1"}, 422), ({"season": 2019}, 422),
                           ({"mix_leading": 50}, 422), ({"opponent": "KC"}, 422), ({"window": "last2"}, 422),
                           ({"team": "KC", "mix_leading": 0, "mix_trailing": 0, "mix_competitive": 0}, 422),
                           ({"seasons": 4}, 422), ({"through_week": 19}, 422)):
        assert api.get("/api/pace-lab", params=params).status_code == status, params


# ----------------------------------------------------------------------------- the league mount (owner-only)


def test_pace_lab_under_a_league_is_owner_only_and_authorizes_before_validating(pg, served, client, e2e, tmp_path):
    from cp_seed import U1, U2, seed_membership, seed_user
    from league_fixtures import publish_local
    from test_leagues_api import bearer
    pub = publish_local(pg, e2e, served, tmp_path / "w")
    with pg.connect() as c:
        seed_user(c, U1)
        seed_membership(c, U1, pub.league_id, "admin")
        seed_user(c, U2, admin=True)
        c.commit()
    url = f"/v2/leagues/{pub.league_id}/api/pace-lab"
    assert client.get(url).status_code == 404
    assert client.get(url, headers=bearer(U1)).status_code == 404               # a league admin is not enough
    assert client.get(url + "?team=K1&window=bogus", headers=bearer(U1)).status_code == 404   # before validation
    assert client.get(f"/v2/leagues/lg_{'q' * 24}/api/pace-lab?team=K1").status_code == 404
    r = client.get(url + "?team=KC", headers=bearer(U2))
    assert r.status_code == 200, r.text[:300]
    assert r.json()["contract"] == "pace-lab.v1" and r.json()["team"]["team"] == "KC"
    assert r.headers["cache-control"] == "private, no-store"
    assert client.get(url + "?team=K1", headers=bearer(U2)).status_code == 422


from test_league_scope import client, e2e, served  # noqa: E402,F401  (fixtures for the league mount test)
from test_leagues_api import env  # noqa: E402,F401
