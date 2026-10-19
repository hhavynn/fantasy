"""Penalty-erased opportunities (ifp-pe, research only): desc parsing, player identification and its
failure modes, the leave-one-out validation, point-in-time features, the candidates' plumbing and
isolation from production. Hand-built frames with invented values: these tests check behaviour,
not forecasting accuracy."""

from __future__ import annotations

import pathlib
import subprocess
import sys

import numpy as np
import polars as pl
import pytest

from app.analytics import challenger as ch
from app.analytics import penalty_erased as pe
from app.analytics.forecast_features import PANEL_COLUMNS, _panel_schema
from tests.test_roster_context import _store, prow

S = 2025
G = "2025_03_BUF_MIA"


# ----------------------------------------------------------------------------- parsing


@pytest.mark.parametrize("desc,expected", [
    ("(5:08) (Shotgun) 14-S.Howell pass incomplete deep left to 17-T.McLaurin. PENALTY on ARI-20-Ma.Wilson, "
     "Defensive Pass Interference, 37 yards, enforced at 50 - No Play.", ("target", 17, "T.McLaurin", "deep", 0)),
    ("(11:15) (Shotgun) 4-D.Watson pass short right to 16-K.Coutee to HOU 29 for 9 yards (23-J.Elliott). PENALTY on "
     "HOU-66-N.Martin, Offensive Holding, 10 yards, enforced at HOU 20 - No Play.", ("target", 16, "K.Coutee", "short", 0)),
    ("(11:02) 7-J.Brissett pass deep left intended for 2-A.Cooper INTERCEPTED by 25-X.Woods at CAR 0. PENALTY on "
     "CAR-24-C.Henderson, Defensive Pass Interference, 34 yards, enforced at CAR 35 - No Play.",
     ("target", 2, "A.Cooper", "deep", 0)),
    ("(6:45) (Shotgun) 26-L.Bell right tackle to NE 38 for no gain (25-T.Brooks). PENALTY on NYJ-84-R.Griffin, "
     "Offensive Holding, 10 yards, enforced at NE 38 - No Play.", ("carry", 26, "L.Bell", None, 0)),
    ("(9:14) (Shotgun) 7-G.Smith scrambles up the middle to DEN 12 for 7 yards. PENALTY on SEA-60-P.Haynes, Offensive "
     "Holding, 10 yards, enforced at DEN 19 - No Play.", ("carry", 7, "G.Smith", None, 1)),
    ("(6:55) 64-W.Clapp reported in as eligible. 28-L.Murray right guard to NO 37 for 3 yards. PENALTY on NO-60-P.Omameh, "
     "Offensive Holding, 9 yards, enforced at NO 34 - No Play.", ("carry", 28, "L.Murray", None, 0)),
    ("(8:29) 14-A.St. Brown right tackle to DET 45 for 6 yards. PENALTY on DET-1-X.Y, Illegal Shift, 5 yards, "
     "enforced at DET 39 - No Play.", ("carry", 14, "A.St. Brown", None, 0)),
    ("(9:37) 21-M.Ingram II left end to X 3 for 2 yards. PENALTY on BAL-1-X.Y, Offensive Holding, 10 yards - No Play.",
     ("carry", 21, "M.Ingram II", None, 0)),
    ("(5:08) 10-J.Herbert pass incomplete short middle to 84-K.Hill Jr. (23-M.Lattimore). PENALTY on X - No Play.",
     ("target", 84, "K.Hill Jr.", "short", 0)),
])
def test_parse_finds_the_intended_recipient(desc, expected):
    r = pe.parse_desc(desc)
    assert (r["kind"], r["jersey"], r["name"], r["depth"], r["scramble"]) == expected


@pytest.mark.parametrize("desc", [
    "(10:35) (Shotgun) PENALTY on BUF-88-D.Knox, False Start, 5 yards, enforced at BUF 42 - No Play.",
    "(6:30) 19-J.Flacco sacked at NYJ 20 for -6 yards (92-J.Madubuike). PENALTY on BAL-21-B.Stephens, Illegal Contact, "
    "5 yards, enforced at NYJ 26 - No Play.",
    "(:58) 10-J.Herbert kneels to LV 18 for -1 yards. PENALTY on LAC, Illegal Formation, 5 yards - No Play.",
    "(13:44) 10-D.Mills FUMBLES (Aborted) at HOU 42, and recovers at HOU 42. PENALTY on TEN-98-J.Simmons, Defensive "
    "Offside, 5 yards, enforced at HOU 42 - No Play.",
    "(11:03) 8-T.Daniel punts 53 yards to NO 4, Center-46-J.Weeks, fair catch by 11-D.Harris. PENALTY on HOU-34-T.Jones, "
    "Illegal Substitution, 5 yards, enforced at HOU 43 - No Play.",
    "Timeout #3 by LAC at 03:22.",
])
def test_snaps_without_an_attempted_target_or_carry_are_not_opportunities(desc):
    assert pe.parse_desc(desc) is None


def test_receiverless_pass_is_not_a_target():
    r = pe.parse_desc("(11:49) 8-K.Cousins pass incomplete short right. thrown away. PENALTY on MIN-56-G.Bradbury, "
                      "Ineligible Downfield Pass, 5 yards, enforced at MIN 10 - No Play.")
    assert r["kind"] == "pass_no_receiver" and r["jersey"] is None


def test_only_the_final_ruling_before_the_penalty_text_is_parsed():
    # a player named in the penalty text is never the recipient
    assert pe.parse_desc("(1:00) 9-D.Brees pass short right to 10-T.Smith for 5 yards, TOUCHDOWN NULLIFIED by Penalty. "
                         "PENALTY on NO-10-T.Smith, Offensive Pass Interference, 10 yards - No Play.")["jersey"] == 10
    # replay reversal: the play after the last REVERSED. is the ruling
    r = pe.parse_desc("(1:12) 8-K.Cousins pass short middle to 14-S.Diggs for 3 yards, TOUCHDOWN. The Replay Official "
                      "reviewed the play, and the play was REVERSED. (Shotgun) 8-K.Cousins pass short middle to "
                      "81-B.Johnson for 2 yards. PENALTY on MIN-81-B.Johnson, Offensive Pass Interference - No Play.")
    assert (r["jersey"], r["name"]) == (81, "B.Johnson")
    assert pe.final_segment("a. PENALTY on X. b. Penalty on Y") == "a. "


@pytest.mark.parametrize("name,last,ok", [
    ("J.Smith-Njigba", "Smith-Njigba", True), ("A.St. Brown", "St. Brown", True), ("Mi.Wilson", "Wilson", True),
    ("M.Ingram II", "Ingram", True), ("K.Hill Jr.", "Hill", True), ("T.McLaurin", "Brown", False), ("", "Brown", False),
])
def test_abbreviated_name_matches_surname(name, last, ok):
    assert pe.name_matches(name, last) is ok


# ----------------------------------------------------------------------------- identification


def play(play_id, desc, *, play_type="no_play", posteam="BUF", week=3, penalty_team="MIA",
         penalty_type="Defensive Holding", penalty_yards=5.0, two=0, rec=None, rush=None, air=None, pen=1, **kw):
    """One nflverse pbp row. `rec` / `rush`: (gsis_id, jersey, name) credited by nflverse (official plays only)."""
    r = {"game_id": G, "play_id": float(play_id), "season": S, "week": week, "season_type": "REG",
         "play_type": play_type, "posteam": posteam, "penalty_team": penalty_team, "penalty_type": penalty_type,
         "penalty_yards": penalty_yards, "desc": desc, "two_point_attempt": float(two), "penalty": float(pen),
         "pass_attempt": float(play_type == "pass"), "sack": 0.0, "qb_kneel": 0.0, "qb_spike": 0.0, "air_yards": air,
         "receiver_player_id": None, "receiver_jersey_number": None, "receiver_player_name": None,
         "rusher_player_id": None, "rusher_jersey_number": None, "rusher_player_name": None,
         "passer_player_id": None, "passer_jersey_number": None, "passer_player_name": None}
    for role, v in (("receiver", rec), ("rusher", rush)):
        if v:
            r.update({f"{role}_player_id": v[0], f"{role}_jersey_number": v[1], f"{role}_player_name": v[2]})
    r.update(kw)
    return r


PBP_SCHEMA = {"play_id": pl.Float64, "two_point_attempt": pl.Float64, "penalty": pl.Float64, "air_yards": pl.Float64,
              "receiver_jersey_number": pl.Int64, "rusher_jersey_number": pl.Int64, "passer_jersey_number": pl.Int64,
              "penalty_yards": pl.Float64}


def pbp(rows):
    return pl.DataFrame(rows, infer_schema_length=None).cast(PBP_SCHEMA, strict=False)


def rosters(rows=(("DIGGS", 14, "Diggs"), ("COOK", 4, "Cook"), ("KNOX", 88, "Knox"), ("SHAKIR", 10, "Shakir"))):
    return pl.DataFrame([{"season": S, "week": 3, "team": "BUF", "jersey_number": j, "gsis_id": g, "last_name": n}
                         for g, j, n in rows])


def test_derive_resolves_by_roster_and_by_the_games_official_plays():
    rows = [
        play(1, "(1:00) 17-J.Allen pass incomplete deep left to 14-S.Diggs. PENALTY on MIA-20-X.Y, Defensive Pass "
                "Interference, 31 yards, enforced at BUF 40 - No Play.", penalty_type=pe.DPI, penalty_yards=31.0),
        play(2, "(2:00) 4-J.Cook left end to MIA 30 for 4 yards. PENALTY on BUF-70-X.Y, Offensive Holding, 10 yards - "
                "No Play.", penalty_team="BUF", penalty_type="Offensive Holding"),
        # official plays: 14-S.Diggs and 26-D.Singletary are credited by nflverse in this game
        play(3, "(3:00) 17-J.Allen pass short left to 14-S.Diggs to MIA 20 for 5 yards.", play_type="pass",
             rec=("DIGGS", 14, "S.Diggs"), air=4.0, pen=0),
        play(4, "(4:00) 26-D.Singletary right end to MIA 10 for 3 yards.", play_type="run",
             rush=("SINGLETARY", 26, "D.Singletary"), pen=0),
        # 26 is not on the weekly roster: resolved from the game's own official play only
        play(5, "(5:00) 26-D.Singletary up the middle for 2 yards. PENALTY on MIA-90-X.Y, Defensive Offside - No Play."),
    ]
    out = pe.derive(pbp(rows), rosters())
    by = {r["play_id"]: r for r in out.iter_rows(named=True)}
    assert by[1.0]["gsis_id"] == "DIGGS" and by[1.0]["gsis_roster"] == by[1.0]["gsis_game"] == "DIGGS"
    assert by[1.0]["dpi"] == 1 and by[1.0]["air_proxy"] == 31.0 and by[1.0]["penalty_side"] == "defense"
    assert by[2.0]["kind"] == "carry" and by[2.0]["gsis_id"] == "COOK" and by[2.0]["penalty_side"] == "offense"
    assert by[2.0]["gsis_game"] is None                              # Cook has no official play in this game
    assert by[5.0]["gsis_id"] == "SINGLETARY" and by[5.0]["gsis_roster"] is None
    assert set(by) == {1.0, 2.0, 5.0}                                 # official plays are not erased plays


def test_unconfident_identifications_never_get_an_id():
    rows = [
        # roster jersey 88 is Knox; the desc names someone else
        play(1, "(1:00) 17-J.Allen pass short left to 88-Z.Jones for 5 yards. PENALTY on BUF - No Play."),
        # nobody wears 99
        play(2, "(1:00) 17-J.Allen pass short left to 99-Z.Jones for 5 yards. PENALTY on BUF - No Play."),
        # roster says 10 is Shakir, the game's official plays say 10 is another Shakir id: sources disagree
        play(3, "(1:00) 17-J.Allen pass short left to 10-K.Shakir for 5 yards. PENALTY on BUF - No Play."),
        play(4, "(4:00) 17-J.Allen pass to 10-K.Shakir for 9 yards.", play_type="pass", rec=("SHAKIR2", 10, "K.Shakir"),
             pen=0, air=6.0),
        # no offense recorded
        play(5, "(1:00) 17-J.Allen pass short left to 14-S.Diggs for 5 yards. PENALTY on BUF - No Play.", posteam=None),
    ]
    by = {r["play_id"]: r for r in pe.derive(pbp(rows), rosters()).iter_rows(named=True)}
    assert by[1.0]["status"] == "name_mismatch" and by[2.0]["status"] == "no_jersey_match"
    assert by[3.0]["status"] == "source_conflict" and by[5.0]["status"] == "no_posteam"
    assert all(by[k]["gsis_id"] is None for k in (1.0, 2.0, 3.0, 5.0))


def test_two_point_tries_are_excluded_even_when_the_flag_is_missing():
    rows = [play(1, "TWO-POINT CONVERSION ATTEMPT. 17-J.Allen pass to 14-S.Diggs is incomplete. ATTEMPT FAILS. PENALTY "
                    "on MIA-43-M.Davis, Defensive Pass Interference, 1 yard - No Play.", two=0),
            play(2, "17-J.Allen pass to 14-S.Diggs is complete. PENALTY on MIA - No Play.", two=1)]
    assert pe.derive(pbp(rows), rosters()).height == 0


def test_air_proxy_uses_dpi_yards_else_the_2019_depth_constants():
    rows = [play(1, "17-J.Allen pass incomplete deep right to 14-S.Diggs. PENALTY on MIA-1-X.Y, Defensive Pass "
                    "Interference, 22 yards - No Play.", penalty_type=pe.DPI, penalty_yards=22.0),
            play(2, "17-J.Allen pass incomplete deep right to 14-S.Diggs. PENALTY on MIA-1-X.Y, Defensive Holding - No Play."),
            play(3, "17-J.Allen pass short right to 14-S.Diggs for 3 yards. PENALTY on BUF-1-X.Y, Offensive Holding - No Play."),
            play(4, "17-J.Allen pass to 14-S.Diggs for 3 yards. PENALTY on BUF-1-X.Y, Offensive Holding - No Play.")]
    air = pe.derive(pbp(rows), rosters()).sort("play_id")["air_proxy"].to_list()
    assert air == [22.0, pe.AIR_PROXY["deep"], pe.AIR_PROXY["short"], pe.AIR_PROXY[None]]


def test_validation_on_official_plays_is_leave_one_out_for_the_game_source():
    rows = [
        # Diggs is credited twice in the game: the game source still sees him once the play itself is removed
        play(1, "(1:00) 17-J.Allen pass short left to 14-S.Diggs for 5 yards.", play_type="pass",
             rec=("DIGGS", 14, "S.Diggs"), air=5.0, pen=0),
        play(2, "(2:00) 17-J.Allen pass deep left to 14-S.Diggs for 25 yards.", play_type="pass",
             rec=("DIGGS", 14, "S.Diggs"), air=24.0, pen=0),
        # Knox is credited once: only the roster can identify him
        play(3, "(3:00) 17-J.Allen pass short middle to 88-D.Knox for 7 yards.", play_type="pass",
             rec=("KNOX", 88, "D.Knox"), air=3.0, pen=0),
        # a penalty play is outside the validation population (it still informs the game map)
        play(4, "(4:00) 17-J.Allen pass short middle to 14-S.Diggs for 7 yards. PENALTY on MIA, Defensive Holding, "
                "declined.", play_type="pass", rec=("DIGGS", 14, "S.Diggs"), air=3.0, pen=1),
    ]
    v = pe.validate_on_official(pbp(rows), rosters())
    assert v["n"] == 3 and v["combined_correct"] == 3 and v["combined_precision"] == 1.0
    assert v["roster_correct"] == 3 and v["game_correct"] == 2 and v["game_unresolved"] == 1


def test_team_totals_count_unresolved_plays_but_player_totals_only_confident_ones():
    rows = [play(1, "17-J.Allen pass incomplete deep left to 14-S.Diggs. PENALTY on MIA-1-X.Y, Defensive Pass "
                    "Interference, 30 yards - No Play.", penalty_type=pe.DPI, penalty_yards=30.0),
            play(2, "17-J.Allen pass short left to 99-Z.Jones for 5 yards. PENALTY on BUF-1-X.Y - No Play."),
            play(3, "4-J.Cook left end for 3 yards. PENALTY on BUF-1-X.Y, Offensive Holding - No Play."),
            play(4, "17-J.Allen pass short left to 14-S.Diggs to MIA 20 for 5 yards.", play_type="pass",
                 rec=("DIGGS", 14, "S.Diggs"), air=6.0, pen=0)]
    p = pbp(rows)
    pw, tw = pe.aggregate(pe.derive(p, rosters()), pe.team_air(p))
    d = {r["gsis_id"]: r for r in pw.iter_rows(named=True)}
    assert (d["DIGGS"]["pe_targets"], d["DIGGS"]["pe_dpi"], d["DIGGS"]["pe_air"]) == (1, 1, 30.0)
    assert (d["COOK"]["pe_carries"], d["COOK"]["pe_targets"]) == (1, 0) and "Z" not in d
    t = tw.row(0, named=True)
    assert (t["team"], t["team_pe_targets"], t["team_pe_carries"], t["team_air"]) == ("BUF", 2, 1, 6.0)
    assert t["team_pe_air"] == pytest.approx(30.0 + pe.AIR_PROXY["short"])


def test_identification_report_counts_by_status_and_season():
    rows = [play(1, "17-J.Allen pass short left to 14-S.Diggs for 5 yards. PENALTY on BUF - No Play."),
            play(2, "17-J.Allen pass short left to 99-Z.Jones for 5 yards. PENALTY on BUF - No Play."),
            play(3, "17-J.Allen pass incomplete short right. PENALTY on MIA - No Play.")]
    rep = pe.identification_report(pe.derive(pbp(rows), rosters()))
    assert rep["opportunities"] == 2 and rep["confident_rate"] == 0.5
    assert rep["by_status"] == {"confident": 1, "no_jersey_match": 1}
    assert rep["by_season"][S]["target"] == {"n": 2, "confident": 1} and rep["by_season"][S]["pass_no_receiver"] == 1


# ----------------------------------------------------------------------------- features


def _panel(weeks=range(1, 7)):
    rows = []
    for w in weeks:
        rows.append(prow(S, w, "WR1", "WR", "CHI", targets=8.0, tt=30.0, tr=25.0))
        rows.append(prow(S, w, "RB1", "RB", "CHI", targets=2.0, carries=15.0, tt=30.0, tr=25.0))
    return pl.DataFrame(rows).cast(_panel_schema(), strict=False).select(PANEL_COLUMNS)


def _erased(weeks=range(1, 7), wr_t=1, rb_c=2, team_t=2, team_c=3):
    pw = pl.DataFrame([r for w in weeks for r in (
        {"season": S, "week": w, "gsis_id": "WR1", "team": "CHI", "pe_targets": wr_t, "pe_carries": 0,
         "pe_air": 25.0 * wr_t, "pe_dpi": wr_t},
        {"season": S, "week": w, "gsis_id": "RB1", "team": "CHI", "pe_targets": 0, "pe_carries": rb_c, "pe_air": 0.0,
         "pe_dpi": 0})], schema=pe.PW_SCHEMA)
    tw = pl.DataFrame([{"season": S, "week": w, "team": "CHI", "team_pe_targets": team_t, "team_pe_carries": team_c,
                        "team_pe_air": 30.0, "team_air": 240.0} for w in weeks], schema=pe.TW_SCHEMA)
    return pw, tw


def _feats(panel, pw, tw, week=7):
    t = pl.DataFrame({"gsis_id": ["WR1", "RB1", "NEW"]})
    return {r["gsis_id"]: r for r in pe.features_for_week(panel, pw, tw, S, week, t).iter_rows(named=True)}


def test_adjusted_shares_add_erased_plays_to_numerator_and_denominator():
    f = _feats(_panel(), *_erased())
    wr, rb = f["WR1"], f["RB1"]
    assert wr["pe_tgt_pg"] == pytest.approx(1.0) and wr["pe_dpi_pg"] == pytest.approx(1.0)
    assert wr["pe_adj_target_share"] == pytest.approx((8 + 1) / (30 + 2))
    assert wr["pe_adj_targets_pg"] == pytest.approx(9.0) and wr["pe_adj_air_yards_pg"] == pytest.approx(64.0 + 25.0)
    assert wr["air_share"] == pytest.approx(64 / 240) and wr["pe_adj_air_share"] == pytest.approx((64 + 25) / 270)
    assert wr["pe_rate"] == pytest.approx(1 / 9) and wr["pe_window_opps"] == 5           # last 5 games only
    assert rb["pe_adj_rush_share"] == pytest.approx((15 + 2) / (25 + 3)) and rb["pe_car_pg"] == pytest.approx(2.0)
    assert rb["pe_rate"] == pytest.approx(2 / 19)
    assert all(f["NEW"][c] is None for c in pe.FEATURE_COLUMNS)                           # no window game: null


def test_no_erased_play_is_a_measured_zero_but_an_unparsed_team_week_is_null():
    panel = _panel()
    pw, tw = _erased(wr_t=0, rb_c=0, team_t=0, team_c=0)
    f = _feats(panel, pw.filter(pl.col("pe_targets") > 0), tw)      # parsed games, nothing erased
    assert f["WR1"]["pe_tgt_pg"] == 0.0 and f["WR1"]["pe_adj_target_share"] == pytest.approx(8 / 30)
    f = _feats(panel, pw.head(0), tw.head(0))                        # no game was parsed
    assert f["WR1"]["pe_tgt_pg"] is None and f["WR1"]["pe_adj_target_share"] is None
    # erased volume per game is over parsed games only; official per-game volume over every window game
    f = _feats(panel, *_erased(weeks=[6]))
    assert f["WR1"]["pe_tgt_pg"] == pytest.approx(1.0) and f["WR1"]["pe_adj_targets_pg"] == pytest.approx(9.0)


def test_week_w_and_later_plays_never_change_features():
    before = pe.features_for_week(_panel(), *_erased(), S, 4, pl.DataFrame({"gsis_id": ["WR1", "RB1"]}))
    pw, tw = _erased(wr_t=5, rb_c=5, team_t=9, team_c=9, weeks=range(4, 9))
    p0, t0 = _erased(weeks=range(1, 4))
    after = pe.features_for_week(_panel(range(1, 9)), pl.concat([p0, pw]), pl.concat([t0, tw]), S, 4,
                                 pl.DataFrame({"gsis_id": ["WR1", "RB1"]}))
    assert before.equals(after)


# ----------------------------------------------------------------------------- candidates


def test_candidate_a_replaces_official_opportunity_and_v_adds_erased_volume():
    cfg = ch.ChallengerConfig.load()
    for pos in ("RB", "WR", "TE"):
        base = cfg.features(pos)
        a, v = pe.candidate_features(base, pos, "A"), pe.candidate_features(base, pos, "V")
        assert len(a) == len(base) and set(base) - set(a) == set(pe.REPLACE[pos]) and set(pe.REPLACE[pos]) <= set(base)
        assert set(a) - set(base) == set(pe.REPLACE[pos].values())
        assert v[:len(base)] == base and v[len(base):] == pe.ADD[pos]
        assert all(f in pe.FEATURE_DOCS for f in [*pe.REPLACE[pos].values(), *pe.ADD[pos]])
    assert pe.candidate_features(cfg.features("QB"), "QB", "A") == cfg.features("QB")
    with pytest.raises(ValueError):
        pe.candidate_features([], "WR", "Z")


def _pe_store():
    rng = np.random.default_rng(7)
    n = 2080
    s = _store().with_columns(
        pl.Series("target_share", rng.uniform(0, 0.3, n)), pl.Series("rush_share", rng.uniform(0, 0.6, n)),
        pl.Series("targets_pg", rng.uniform(0, 9, n)), pl.Series("carries_pg", rng.uniform(0, 18, n)),
        pl.Series("air_yards_pg", rng.uniform(0, 90, n)))
    return s.with_columns(
        (pl.col("target_share") + pl.Series(rng.uniform(0, 0.02, n))).alias("pe_adj_target_share"),
        (pl.col("rush_share") + pl.Series(rng.uniform(0, 0.02, n))).alias("pe_adj_rush_share"),
        (pl.col("targets_pg") + pl.Series(rng.uniform(0, 1, n))).alias("pe_adj_targets_pg"),
        (pl.col("carries_pg") + pl.Series(rng.uniform(0, 1, n))).alias("pe_adj_carries_pg"),
        (pl.col("air_yards_pg") + pl.Series(rng.uniform(0, 20, n))).alias("pe_adj_air_yards_pg"),
        pl.Series("pe_tgt_pg", rng.uniform(0, 1, n)), pl.Series("pe_car_pg", rng.uniform(0, 1, n)),
        pl.Series("pe_air_pg", rng.uniform(0, 20, n)), pl.Series("pe_rate", rng.uniform(0, 0.2, n)))


def test_baseline_column_is_exactly_the_production_challenger_and_composite_follows_selection():
    cfg = ch.ChallengerConfig.load()
    store = _pe_store()
    weeks = [(2025, w) for w in (7, 8)]
    ours = pe.walk_forward(store, weeks, cfg, per_position={"WR": "A", "TE": "V"})
    theirs = ch.challenger_predictions(store, weeks, cfg)
    j = ours.join(theirs.rename({"pred_ifp2": "prod"}), on=["key", "gsis_id"]).join(
        store.select("key", "gsis_id", "position"), on=["key", "gsis_id"])
    assert j.height == ours.height == store.filter(pl.col("week") >= 7).height
    assert np.allclose(j["pred_ifp2"].cast(pl.Float64).fill_null(np.nan).to_numpy(),
                       j["prod"].cast(pl.Float64).fill_null(np.nan).to_numpy(), equal_nan=True)
    by = {pos: j.filter(pl.col("position") == pos) for pos in ("QB", "RB", "WR", "TE")}
    assert by["WR"]["pred_pe"].equals(by["WR"]["pred_A"]) and by["TE"]["pred_pe"].equals(by["TE"]["pred_V"])
    assert by["RB"]["pred_pe"].equals(by["RB"]["pred_ifp2"])
    assert by["QB"]["pred_A"].equals(by["QB"]["pred_ifp2"]) and by["QB"]["pred_V"].equals(by["QB"]["pred_ifp2"])
    assert not by["WR"]["pred_A"].equals(by["WR"]["pred_ifp2"]) and not by["RB"]["pred_V"].equals(by["RB"]["pred_ifp2"])
    assert j.filter(pl.col("gsis_id") == "p5")["pred_A"].null_count() == 2       # abstains with no recent game


def test_rules_are_pre_registered_and_official_scoring_is_untouched():
    assert pe.PE_STATUS == "research_only" and pe.EVAL_POSITIONS == ("RB", "WR", "TE")
    assert pe.SELECTION_MAX_DIFF == -0.010 and pe.BIAS_TOLERANCE == 0.15 and pe.MECHANISM_COHORT == "pe_heavy"
    assert set(pe.DECISION_RULES) >= {"selection", "confirmation", "outcome", "never"}
    assert "official scoring" in pe.DECISION_RULES["never"]
    # the module never touches labels or league scoring
    src = pathlib.Path(pe.__file__).read_text()
    assert "league_points" not in src and "league_scoring" not in src and '"label"' not in src
    # and writes nothing: outputs are the research script's, under the git-ignored research directory
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE TABLE", "WRITE_PARQUET", "WRITE_TEXT"):
        assert verb not in src.upper(), verb


def test_app_never_imports_the_research_module():
    backend = pathlib.Path(__file__).resolve().parents[1]
    code = "import sys, app.main; assert 'app.analytics.penalty_erased' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], cwd=backend, check=True)
    for path in (backend / "app").rglob("*.py"):
        if path.name != "penalty_erased.py":
            assert "penalty_erased" not in path.read_text(), path
