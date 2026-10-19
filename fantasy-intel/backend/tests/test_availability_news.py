"""Availability news (app.analytics.availability_news): parsing, the per-week states, the workload handoff override
and the live run's rows. Synthetic data only (the worlds of test_injury_workload and test_ros_projections)."""

from __future__ import annotations

import pytest

from app.analytics import availability_news as an
from app.analytics import injury_model as im
from app.analytics import injury_workload as iw
from app.analytics.independent_projection_store import build_live

import test_injury_workload as tiw
from test_injury_model import trained, with_table  # noqa: F401 - module-scoped fixture
from test_ros_projections import PLAYERS, PS_ID, W, by_id, participation_artifact

HEALTHY_WR, IR_WR = 1, 3


def entry(**kw) -> dict:
    base = {"season": 2026, "gsis_id": "00-X", "name": "X", "team": "AAA", "kind": "out", "from_week": 7,
            "return_window": [12, 16], "reported_on": "2026-10-19", "note": "test"}
    return {**base, **kw}


def news(*entries) -> list[an.Entry]:
    return an.parse({"schema": an.SCHEMA, "entries": list(entries)})


# ----------------------------------------------------------------------------- the file and the states


def test_the_reviewed_file_parses():
    assert all(e.kind in an.KINDS for e in an.load())


@pytest.mark.parametrize("bad, match", [
    ({"kind": "maybe"}, "kind"),
    ({"kind": "active"}, "only an 'out' entry"),
    ({"return_window": [6, 9]}, "must start at or after"),
    ({"return_window": [12, 10]}, "must start at or after"),
])
def test_invalid_entries_are_refused(bad, match):
    with pytest.raises(ValueError, match=match):
        news(entry(**bad))


def test_duplicate_entries_and_a_wrong_schema_are_refused():
    with pytest.raises(ValueError, match="more than one"):
        news(entry(), entry())
    with pytest.raises(ValueError, match="schema"):
        an.parse({"schema": "v0", "entries": []})


def test_out_with_a_window_ramps_over_team_games_and_skips_the_bye():
    e = news(entry())[0]
    games = [w for w in range(1, 19) if w != 11]
    assert an.state(e, 6, games) is None                               # before the news applies
    assert [an.state(e, w, games).p for w in (7, 10, 11)] == [0.0, 0.0, 0.0]
    assert [an.state(e, w, games).p for w in range(12, 17)] == [0.2, 0.4, 0.6, 0.8, 1.0]
    assert an.state(e, 17, games).kind == "returned" and an.state(e, 17, games).p == 1.0
    bye = [w for w in range(1, 19) if w != 14]                         # a bye inside the window is not a step
    assert [an.state(e, w, bye).p for w in (12, 13, 15, 16)] == [0.25, 0.5, 0.75, 1.0]
    assert "uniform" in an.state(e, 13, games).reason and "test" in an.state(e, 13, games).reason


def test_season_ending_and_active_entries():
    out, active = news(entry(gsis_id="00-A", return_window=None), entry(gsis_id="00-B", kind="active",
                                                                         return_window=None))
    assert an.state(out, 18, None).p == 0.0 and "rest of the season" in an.state(out, 18, None).reason
    assert an.state(active, 6, None) is None and an.state(active, 7, None).p == 1.0
    states = an.for_week([out, active], 2026, 9, {})
    assert set(states) == {"00-A", "00-B"} and an.for_week([out, active], 2025, 9, {}) == {}


# ----------------------------------------------------------------------------- workload handoff


def test_a_news_out_lead_back_hands_his_carries_to_the_room():
    src = tiw.world(range(1, 6))
    base = tiw.rows_by_id(im.handoff(src, tiw.S, 8, 6, return_rates={}))
    out = tiw.rows_by_id(im.handoff(src, tiw.S, 8, 6, return_rates={},
                                    news={tiw.STAR: an.NewsState(tiw.STAR, "out", 0.0, "out")}))
    half = tiw.rows_by_id(im.handoff(src, tiw.S, 8, 6, return_rates={},
                                     news={tiw.STAR: an.NewsState(tiw.STAR, "return_window", 0.5, "half")}))
    assert out[tiw.BACKUP]["ho_car_share"] > half[tiw.BACKUP]["ho_car_share"] > base[tiw.BACKUP]["ho_car_share"]
    assert half[tiw.BACKUP]["ho_scenarios"] == 1                       # a news probability mixes at any horizon
    assert half[tiw.BACKUP]["ho_car_share"] == pytest.approx(
        (out[tiw.BACKUP]["ho_car_share"] + base[tiw.BACKUP]["ho_car_share"]) / 2)
    assert out[tiw.BACKUP]["ho_top_absent"] == tiw.STAR


def test_a_reinstated_player_takes_his_claim_back():
    """STAR on the exempt list (EXE) for weeks 1-5: without news the room keeps his work; with news he returns with
    his previous-season role, which takes the share back from BACKUP."""
    exempt = {(tiw.STAR, w): ("AAA", "EXE") for w in range(1, 7)}
    src = tiw.world(range(1, 6), skip={tiw.STAR: tuple(range(1, 6))}, roster=exempt)
    tl = tiw.rows_by_id(iw.injury_timeline(src, tiw.S, 7, 6))
    assert tl[tiw.STAR]["status_class"] == iw.UNAVAILABLE
    base = tiw.rows_by_id(im.handoff(src, tiw.S, 7, 6, return_rates={}))
    back = tiw.rows_by_id(im.handoff(src, tiw.S, 7, 6, return_rates={},
                                     news={tiw.STAR: an.NewsState(tiw.STAR, "active", 1.0, "back")}))
    assert back[tiw.BACKUP]["ho_car_share"] < base[tiw.BACKUP]["ho_car_share"]
    assert back[tiw.STAR]["ho_car_share"] > back[tiw.BACKUP]["ho_car_share"]
    assert back[tiw.BACKUP]["ho_top_returning"] == tiw.STAR


# ----------------------------------------------------------------------------- live run rows


def gsis_of(pid: int) -> str:
    return PLAYERS[pid][2]


def test_live_rows_follow_the_news(trained):  # noqa: F811
    con, src, art = trained
    part = participation_artifact()
    tab = with_table(art, rate=0.25)
    entries = news(entry(gsis_id=gsis_of(HEALTHY_WR), team="AAA", return_window=[9, 10]),
                   entry(gsis_id=gsis_of(IR_WR), team="EEE", from_week=6, return_window=[7, 8]),
                   entry(gsis_id=gsis_of(PS_ID), team="CCC", kind="active", from_week=7, return_window=None))
    plain7 = by_id(build_live(con, src, tab, 2026, 7, participation=part, artifact_sha256="a" * 64,
                              projected_from_week=W)[1])
    meta, rows7 = build_live(con, src, tab, 2026, 7, participation=part, artifact_sha256="a" * 64,
                             projected_from_week=W, news=entries)
    r7 = by_id(rows7)
    # out before the window: withheld with the news note
    assert plain7[HEALTHY_WR]["coverage_status"] == "projected"
    assert r7[HEALTHY_WR]["coverage_status"] == "withheld_unavailable"
    assert "availability news" in r7[HEALTHY_WR]["missing_inputs"][0]
    # ESPN INJURY_RESERVE withholds every remaining week; a dated return window projects him at P(plays) x 0.5
    assert plain7[IR_WR]["coverage_status"] == "withheld_unavailable"
    x = r7[IR_WR]
    assert x["coverage_status"] == "projected" and x["participation"]["news_p_play"] == 0.5
    assert x["p_play"] == round(x["participation"]["base_p_play"] * 0.5, 4)
    assert x["expected_points"] == round(x["p_play"] * x["projected_points"], 2)
    assert any("availability news" in f for f in x["flags"])
    # practice squad (PRA) -> active from week 7: projected despite the weekly roster status
    assert plain7[PS_ID]["coverage_status"] == "withheld_unavailable"
    assert r7[PS_ID]["coverage_status"] == "projected" and r7[PS_ID]["p_play"] is not None
    # rows the news does not name are unchanged
    for pid in PLAYERS:
        if pid not in (HEALTHY_WR, IR_WR, PS_ID) and plain7[pid]["coverage_status"] == "projected":
            assert r7[pid]["p_play"] == plain7[pid]["p_play"]
    # after the window: assumed back at the model's own P(plays)
    r11 = by_id(build_live(con, src, tab, 2026, 11, participation=part, artifact_sha256="a" * 64,
                           projected_from_week=W, news=entries)[1])
    assert r11[HEALTHY_WR]["coverage_status"] == "projected" and r11[HEALTHY_WR]["participation"]["news_p_play"] == 1.0


def test_horizon_zero_espn_out_still_withholds_a_news_active_player(trained):  # noqa: F811
    con, src, art = trained
    out_rb = 2                                                         # ESPN OUT for week 6
    entries = news(entry(gsis_id=gsis_of(out_rb), team="DDD", kind="active", from_week=6, return_window=None))
    r = by_id(build_live(con, src, art, 2026, W, participation=participation_artifact(), artifact_sha256="a" * 64,
                         news=entries)[1])
    assert r[out_rb]["coverage_status"] == "withheld_unavailable"


def test_news_replaces_the_return_rate_table_for_an_out_at_the_cutoff_player(trained):  # noqa: F811
    """OUT_RB is Out at the cutoff: the table gives him 25% for week 7; an `active` entry says he is back, so his
    P(plays) is the undesignated estimate the table replaced."""
    con, src, art = trained
    out_rb = 2
    tab = with_table(art, rate=0.25)
    part = participation_artifact()
    plain = by_id(build_live(con, src, tab, 2026, 7, participation=part, artifact_sha256="a" * 64,
                             projected_from_week=W)[1])[out_rb]
    assert plain["p_play"] == 0.25
    back = by_id(build_live(con, src, tab, 2026, 7, participation=part, artifact_sha256="a" * 64, projected_from_week=W,
                            news=news(entry(gsis_id=gsis_of(out_rb), team="DDD", kind="active", return_window=None)))[1])
    assert back[out_rb]["p_play"] == plain["participation"]["undesignated_p_play"] != 0.25
