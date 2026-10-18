"""Injury timeline, established healthy role and workload handoff (research only): the mandatory Week 6 / Week 7
scenarios, point-in-time safety, missing data and isolation from production.

Hand-built frames with invented values. STAR, BACKUP, RB3 ... are labels for a hypothetical backfield (a lead
back, his primary replacement, a depth back); no assertion depends on a real player's outcome or on an exact
point total."""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys

import polars as pl
import pytest

from app.analytics import injury_challenger as ic
from app.analytics import injury_workload as iw
from app.analytics.forecast_features import INJURY_SCHEMA, PANEL_COLUMNS, ROSTER_SCHEMA, Sources, _panel_schema

S = 2026
TEAMS = ("AAA", "BBB", "CCC", "DDD")
OPP = {"AAA": "BBB", "BBB": "AAA", "CCC": "DDD", "DDD": "CCC"}
STAR, BACKUP, RB3, WR1, WR2, WR3, TE1, TE2, QB = ("00-STAR", "00-BACKUP", "00-RB3", "00-WR1", "00-WR2", "00-WR3",
                                                 "00-TE1", "00-TE2", "00-QB")
# gsis -> (position, targets, carries, snap share) of a normal healthy game for team AAA
ROOM = {STAR: ("RB", 4, 18, 0.72), BACKUP: ("RB", 1, 5, 0.25), RB3: ("RB", 1, 2, 0.08),
        WR1: ("WR", 9, 0, 0.92), WR2: ("WR", 6, 0, 0.85), WR3: ("WR", 4, 0, 0.6),
        TE1: ("TE", 5, 0, 0.85), TE2: ("TE", 2, 0, 0.3), QB: ("QB", 0, 3, 1.0)}
TEAM_TARGETS, TEAM_RUSH = 32.0, 28.0


def prow(season, week, gid, pos, team, targets, carries, snap, tt=TEAM_TARGETS, tr=TEAM_RUSH):
    return {"season": season, "week": week, "gsis_id": gid, "position": pos, "team": team, "opponent": OPP[team],
            "has_stats": True, "has_snaps": True, "league_points": 0.6 * carries + 1.6 * targets,
            "pass_attempts": 0.0, "carries": float(carries), "targets": float(targets), "receptions": 0.6 * targets,
            "air_yards": 8.0 * targets, "passing_yards": 0.0, "rushing_yards": 4.0 * carries,
            "receiving_yards": 7.0 * targets, "offense_pct": snap, "xfp": 0.6 * carries + 1.6 * targets,
            "rz_targets": 0.0, "rz_carries": 0.0, "gl_carries": round(carries / 10), "designed_rushes": float(carries),
            "team_targets": tt, "team_rush_attempts": tr, "team_rb_carries": tr - 3, "td_points": 0.0,
            "xfpl": 0.6 * carries + 1.6 * targets, "xfpl_td": 0.5, "team_plays": 62.0}


def world(played=range(1, 6), *, last_week=8, skip=None, usage=None, injuries=(), roster=None, prev_season=True,
          byes=None) -> Sources:
    """Team AAA plus three opponents; season S weeks `played` are final, later weeks up to `last_week` scheduled.
    `skip`: {gsis: weeks absent}. `usage`: {(gsis, week): (targets, carries, snap)} overrides. `injuries`: report
    rows. `roster`: {(gsis, week): (team, status)} overrides (default AAA/ACT). `byes`: {team: week}."""
    skip, usage, roster, byes = skip or {}, usage or {}, roster or {}, byes or {}
    panel, ros, games = [], [], []
    seasons = [(S - 1, range(1, 18)), (S, played)] if prev_season else [(S, played)]
    for season, weeks in seasons:
        for w in weeks:
            if byes.get("AAA") == w and season == S:
                continue
            for g, (pos, t, c, sn) in ROOM.items():
                if season == S and w in skip.get(g, ()):
                    continue
                t2, c2, sn2 = usage.get((g, w), (t, c, sn)) if season == S else (t, c, sn)
                team = roster.get((g, w), ("AAA", "ACT"))[0] if season == S else "AAA"
                if team != "AAA" and season == S:
                    continue
                panel.append(prow(season, w, g, pos, "AAA", t2, c2, sn2))
            for team in ("BBB", "CCC", "DDD"):
                panel.append(prow(season, w, f"00-{team}RB", "RB", team, 3, 15, 0.6))
                panel.append(prow(season, w, f"00-{team}WR", "WR", team, 8, 0, 0.9))
    for w in range(1, last_week + 1):
        for home, away in (("AAA", "BBB"), ("CCC", "DDD")):
            if byes.get("AAA") == w and "AAA" in (home, away):
                continue
            final = w in played
            for t, o, h in ((home, away, True), (away, home, False)):
                games.append({"season": S, "week": w, "game_id": f"{S}_{w}_{away}_{home}", "team": t, "opponent": o,
                              "is_home": h, "gameday": f"{S}-10-{w:02d}", "gametime": "13:00",
                              "points_for": 20.0 if final else None, "points_against": 17.0 if final else None})
    for w in range(1, last_week + 1):
        if w not in played and w > max(played, default=0) + 1:
            continue                    # roster rows exist only for weeks up to the next unplayed one
        for g, (pos, *_rest) in ROOM.items():
            team, status = roster.get((g, w), ("AAA", "ACT"))
            ros.append({"season": S, "week": w, "gsis_id": g, "team": team, "position": pos, "status": status})
    panel_df = pl.DataFrame(panel).cast(_panel_schema(), strict=False).select(PANEL_COLUMNS)
    games_df = pl.DataFrame(games).cast({"season": pl.Int64, "week": pl.Int64, "points_for": pl.Float64,
                                         "points_against": pl.Float64})
    return Sources(panel_df, games_df, pl.DataFrame(), pl.DataFrame(ros, schema=ROSTER_SCHEMA),
                   pl.DataFrame(list(injuries), schema=INJURY_SCHEMA))


def inj(gid, status, week, team="AAA", practice=None, injury="Hamstring"):
    return {"season": S, "week": week, "gsis_id": gid, "team": team, "report_status": status,
            "practice_status": practice, "injury": injury, "not_injury": False}


def report(week, *rows):
    """A published report for AAA in `week`: the given rows plus one unrelated listing so the team has rows."""
    return [*rows, inj(TE2, None, week, practice="Full Participation in Practice", injury="Ankle")]


def rows_by_id(df: pl.DataFrame) -> dict[str, dict]:
    return {r["gsis_id"]: r for r in df.iter_rows(named=True)}


def rb_pool_total(ho: pl.DataFrame, tl: pl.DataFrame) -> float:
    avail = set(tl.filter((pl.col("team") == "AAA") & (pl.col("position") == "RB")
                          & (pl.col("status_class") != iw.UNAVAILABLE))["gsis_id"].to_list())
    return float(ho.filter(pl.col("gsis_id").is_in(list(avail)))["ho_car_share"].sum())


# ----------------------------------------------------------------------------- Test A: lead back OUT in week 6


def scenario_a(backup_absence_usage=None):
    """STAR played weeks 1-5 and is ruled Out for week 6. Optionally BACKUP already started a game without STAR."""
    usage, skip, inj_rows = {}, {}, report(6, inj(STAR, "Out", 6, practice="Did Not Participate In Practice"))
    if backup_absence_usage is not None:     # STAR missed week 3 (Out) and BACKUP carried the load
        skip = {STAR: (3,)}
        usage = {(BACKUP, 3): backup_absence_usage}
        inj_rows += [inj(STAR, "Out", 3)]
    return world(range(1, 6), skip=skip, usage=usage, injuries=inj_rows)


def test_a_lead_back_out_is_unavailable_and_his_role_is_remembered():
    src = scenario_a()
    tl = rows_by_id(iw.injury_timeline(src, S, 6))
    assert tl[STAR]["designation"] == "out" and tl[STAR]["status_class"] == iw.UNAVAILABLE
    assert tl[STAR]["confidence"] == "report" and tl[STAR]["report_week"] == 6
    roles = rows_by_id(iw.established_roles(src, S, 6))
    assert roles[STAR]["role_car_share"] > 0.55            # a lead-back claim (18 of 28 carries), kept in context
    assert roles[STAR]["role_car_share"] > 3 * roles[BACKUP]["role_car_share"]
    text = " ".join(iw.explain(tl[STAR], None)["text"])
    assert "Not expected to play" in text and "Out" in text


def test_a_replacements_inherit_the_vacated_work_without_a_mechanical_100_percent():
    src = scenario_a()
    tl = iw.injury_timeline(src, S, 6)
    ho = rows_by_id(iw.workload_handoff(src, S, 6, timeline=tl))
    b, r3 = ho[BACKUP], ho[RB3]
    assert b["ho_car_share"] > 2.5 * b["ho_base_car_share"]           # a meaningful rise for the primary backup
    assert b["ho_carries"] > b["ho_base_car_share"] * b["ho_team_rush_pg"] + 5
    assert r3["ho_car_share"] > r3["ho_base_car_share"]               # the depth back also gains (committee) ...
    assert b["ho_car_share"] > r3["ho_car_share"]                     # ... but less than the established backup
    assert b["ho_car_share"] < b["ho_group_share"]                    # nobody takes 100% of the RB work
    assert b["ho_top_absent"] == STAR and b["ho_lead_out"] == 1 and b["ho_shift_pg"] > 0
    # the RB budget is internally consistent: available RBs split exactly the group's share of team rushes
    assert rb_pool_total(iw.workload_handoff(src, S, 6, timeline=tl), tl) == pytest.approx(b["ho_group_share"], rel=1e-9)
    text = " ".join(iw.explain(rows_by_id(tl)[BACKUP], b, names={STAR: "STAR"})["text"])
    assert "expanded role while STAR is unavailable" in text


def test_a_the_backup_rise_comes_from_evidence_not_a_constant():
    """Same Out designation; the only difference is an earlier game the team played without STAR, in which BACKUP
    took 20 carries. That evidence raises BACKUP's expected share; nothing is keyed on a player name."""
    plain = rows_by_id(iw.workload_handoff(scenario_a(), S, 6))
    seen = rows_by_id(iw.workload_handoff(scenario_a(backup_absence_usage=(2, 20, 0.75)), S, 6))
    assert seen[BACKUP]["ho_absence_games"] == 1 and plain[BACKUP]["ho_absence_games"] == 0
    assert seen[BACKUP]["ho_car_share"] > plain[BACKUP]["ho_car_share"]
    assert seen[RB3]["ho_car_share"] < plain[RB3]["ho_car_share"]


# ----------------------------------------------------------------------------- Test B: confirmed return in week 7


def scenario_b(week7_report=True, questionable=False):
    """STAR Out in week 6 (missed it), BACKUP carried 20 times. Week 7: credible pregame evidence he is back."""
    rows = [inj(STAR, "Out", 6, practice="Did Not Participate In Practice")]
    if week7_report:
        rows += report(7, inj(STAR, "Questionable" if questionable else None, 7,
                              practice="Limited Participation in Practice" if questionable
                              else "Full Participation in Practice"))
    return world(range(1, 7), skip={STAR: (6,)}, usage={(BACKUP, 6): (3, 20, 0.8), (RB3, 6): (1, 5, 0.2)},
                 injuries=rows)


def test_b_returning_starter_reclaims_his_role_and_the_backup_gives_it_back():
    src = scenario_b()
    tl = rows_by_id(iw.injury_timeline(src, S, 7))
    assert tl[STAR]["status_class"] == iw.RETURNING and tl[STAR]["missed_team_games"] == 1
    assert tl[STAR]["missed_injured"] == 1 and tl[STAR]["return_evidence"] == "listed_without_designation"
    w6 = rows_by_id(iw.workload_handoff(scenario_a(), S, 6))
    w7 = rows_by_id(iw.workload_handoff(src, S, 7))
    assert w7[STAR]["ho_car_share"] > 0.55                              # back toward his healthy lead share
    assert w7[BACKUP]["ho_car_share"] < 0.5 * w6[BACKUP]["ho_car_share"]  # the temporary share is gone
    assert w7[BACKUP]["ho_shift_pg"] < 0                                # his 20-carry week no longer extrapolated
    assert w7[BACKUP]["ho_lead_returning"] == 1 and w7[BACKUP]["ho_top_returning"] == STAR
    tlf = iw.injury_timeline(src, S, 7)
    assert rb_pool_total(iw.workload_handoff(src, S, 7, timeline=tlf), tlf) == pytest.approx(w7[STAR]["ho_group_share"])
    text = " ".join(iw.explain(tl[BACKUP], w7[BACKUP], names={STAR: "STAR"})["text"])
    assert "STAR is back from injury" in text


def test_b_a_missed_game_is_not_a_zero_workload_game():
    """STAR's healthy role is the same before and after the missed week (the panel has no week-6 row for him,
    and the role averages appearances only)."""
    before = rows_by_id(iw.established_roles(scenario_a(), S, 6))[STAR]
    after = rows_by_id(iw.established_roles(scenario_b(), S, 7))[STAR]
    assert after["role_car_share"] == pytest.approx(before["role_car_share"])
    assert after["role_games"] == before["role_games"]


def test_b_limited_return_keeps_the_uncertainty_label():
    tl = rows_by_id(iw.injury_timeline(scenario_b(questionable=True), S, 7))
    assert tl[STAR]["designation"] == "questionable" and tl[STAR]["status_class"] == iw.UNCERTAIN
    assert tl[STAR]["return_evidence"] == "questionable"


# ----------------------------------------------------------------------------- Test C: week 7 status unknown


def test_c_week6_out_is_neither_proof_of_absence_nor_of_recovery_in_week7():
    src = world(range(1, 6), injuries=report(6, inj(STAR, "Out", 6)))     # forecast made at the week-6 cutoff
    tl = rows_by_id(iw.injury_timeline(src, S, 7, cutoff_week=6))
    s = tl[STAR]
    assert s["designation"] == "future_unknown" and s["horizon"] == 1
    assert s["status_class"] == iw.UNCERTAIN and "no week-7 report" in s["status_reason"]
    assert s["report_week"] == 6 and s["report_status"] == "Out"           # the evidence is labeled with its week
    expl = iw.explain(s, None)
    assert expl["horizon"] == 1 and any("no injury report exists yet" in x for x in expl["text"])
    assert not any("Returning" in x for x in expl["text"])                  # a speculative return is not "confirmed"
    # without an empirical return-rate table nothing is invented: status quo, labeled (0 scenarios)
    sq = rows_by_id(iw.workload_handoff(src, S, 7, cutoff_week=6))
    assert sq[BACKUP]["ho_scenarios"] == 0
    # with a fitted table the backup's expected share is the mixture of the two scenarios
    rates = {("RB", "0", 1): {"rate": 0.6, "n": 100}}
    mix = rows_by_id(iw.workload_handoff(src, S, 7, cutoff_week=6, return_rates=rates))
    back = rows_by_id(iw.workload_handoff(world(range(1, 6), injuries=report(6, inj(STAR, None, 6))), S, 7, cutoff_week=6))
    assert mix[BACKUP]["ho_scenarios"] == 1
    assert back[BACKUP]["ho_car_share"] < mix[BACKUP]["ho_car_share"] < sq[BACKUP]["ho_car_share"]
    expected = 0.6 * back[BACKUP]["ho_car_share"] + 0.4 * sq[BACKUP]["ho_car_share"]
    assert mix[BACKUP]["ho_car_share"] == pytest.approx(expected, rel=1e-6)


def test_c_return_rates_are_fitted_point_in_time():
    """Episodes are counted only when their outcome game is before through_key."""
    rows = [inj(STAR, "Out", 3), *report(3)]
    src = world(range(1, 6), skip={STAR: (3,)}, injuries=rows)
    early = iw.fit_return_rates(src, through_key=S * 100 + 4)
    late = iw.fit_return_rates(src, through_key=S * 100 + 6)
    assert ("ALL", "ALL", 1) not in early                      # the week-4 outcome is not before week 4
    assert late[("ALL", "ALL", 1)] == {"rate": 1.0, "n": 1}     # STAR played week 4
    assert late[("ALL", "ALL", 2)]["n"] == 1


# ----------------------------------------------------------------------------- Test D: long absence (weeks 3-6)


def scenario_d():
    """STAR led the backfield last season and in weeks 1-2, missed weeks 3-6 (Out each week) while BACKUP took
    over (20 carries), and is on the week-7 report without a game designation."""
    rows = [inj(STAR, "Out", w, practice="Did Not Participate In Practice", injury="Quadricep") for w in (3, 4, 5, 6)]
    rows += report(7, inj(STAR, None, 7, practice="Full Participation in Practice", injury="Quadricep"))
    usage = {(BACKUP, w): (3, 20, 0.85) for w in (3, 4, 5, 6)}
    return world(range(1, 7), skip={STAR: (3, 4, 5, 6)}, usage=usage, injuries=rows)


def test_d_long_absence_keeps_the_established_lead_role():
    src = scenario_d()
    tl = rows_by_id(iw.injury_timeline(src, S, 7))
    s = tl[STAR]
    assert s["missed_team_games"] == 4 and s["missed_injured"] == 4 and s["status_class"] == iw.RETURNING
    assert (s["last_healthy_season"], s["last_healthy_week"]) == (S, 2)
    roles = rows_by_id(iw.established_roles(src, S, 7))
    assert roles[STAR]["role_games"] == iw.HEALTHY_WINDOW              # weeks 1-2 + last season, no zero rows
    assert roles[STAR]["role_car_share"] > 0.55                        # not reclassified as a low-volume back
    ho = rows_by_id(iw.workload_handoff(src, S, 7, timeline=iw.injury_timeline(src, S, 7)))
    assert ho[STAR]["ho_car_share"] > ho[BACKUP]["ho_car_share"]       # the lead expectation recovers
    assert ho[BACKUP]["ho_shift_pg"] < -5                              # BACKUP's four 20-carry games do not persist
    text = " ".join(iw.explain(s, ho[STAR])["text"])
    assert "Historical healthy workload retained despite 4 missed games" in text


def test_d_participation_risk_stays_separate_from_the_role():
    """Same history, but week 7 is projected from the week-6 cutoff: availability is uncertain, the role is not
    reduced (the handoff is conditional on his appearing)."""
    src = scenario_d()
    tl = rows_by_id(iw.injury_timeline(src, S, 8, cutoff_week=7))
    assert tl[STAR]["status_class"] == iw.AVAILABLE                    # week-7 report says no designation
    early = world(range(1, 6), skip={STAR: (3, 4, 5)}, usage={(BACKUP, w): (3, 20, 0.85) for w in (3, 4, 5)},
                  injuries=[inj(STAR, "Out", w) for w in (3, 4, 5, 6)])
    t6 = rows_by_id(iw.injury_timeline(early, S, 7, cutoff_week=6))
    assert t6[STAR]["status_class"] == iw.UNCERTAIN
    ho = rows_by_id(iw.workload_handoff(early, S, 7, cutoff_week=6))
    healthy = rows_by_id(iw.workload_handoff(world(range(1, 6)), S, 7, cutoff_week=6))
    # conditional on his appearing, most of the healthy lead share survives the replacement's run (each row is
    # conditional on ITS player appearing: BACKUP's row is the status-quo scenario without STAR, labeled 0 scenarios)
    assert ho[STAR]["ho_car_share"] > 0.8 * healthy[STAR]["ho_car_share"]
    assert ho[STAR]["ho_participation"] == 1.0 and ho[BACKUP]["ho_scenarios"] == 0


# ----------------------------------------------------------------------------- additional scenarios


def test_doubtful_lead_leaves_the_pool_and_questionable_stays():
    d = rows_by_id(iw.workload_handoff(world(injuries=report(6, inj(STAR, "Doubtful", 6))), S, 6))
    q = rows_by_id(iw.workload_handoff(world(injuries=report(6, inj(STAR, "Questionable", 6))), S, 6))
    assert d[BACKUP]["ho_lead_out"] == 1 and q[BACKUP]["ho_lead_out"] == 0
    assert d[BACKUP]["ho_car_share"] > 2 * q[BACKUP]["ho_car_share"]
    tl = rows_by_id(iw.injury_timeline(world(injuries=report(6, inj(STAR, "Questionable", 6))), S, 6))
    assert tl[STAR]["status_class"] == iw.UNCERTAIN


def test_multiple_unavailable_rbs_and_an_injured_replacement():
    src = world(injuries=report(6, inj(STAR, "Out", 6), inj(BACKUP, "Out", 6)))
    tl = iw.injury_timeline(src, S, 6)
    ho = rows_by_id(iw.workload_handoff(src, S, 6, timeline=tl))
    assert ho[RB3]["ho_car_share"] == pytest.approx(ho[RB3]["ho_group_share"])   # the only RB left takes the room
    assert ho[RB3]["ho_vacated"] > 0.9
    # replacement hurt a week later: STAR out in 6 (BACKUP started), BACKUP out in 7 too
    src2 = world(range(1, 7), skip={STAR: (6,)}, usage={(BACKUP, 6): (3, 20, 0.8)},
                 injuries=[inj(STAR, "Out", 6), *report(7, inj(STAR, "Out", 7), inj(BACKUP, "Out", 7))])
    tl2 = iw.injury_timeline(src2, S, 7)
    ho2 = iw.workload_handoff(src2, S, 7, timeline=tl2)
    assert rb_pool_total(ho2, tl2) == pytest.approx(rows_by_id(ho2)[RB3]["ho_group_share"])


def test_returning_wr1_suppresses_temporary_wr2_gains():
    out5 = world(range(1, 5), injuries=report(5, inj(WR1, "Out", 5)))
    back6 = world(range(1, 6), skip={WR1: (5,)}, usage={(WR2, 5): (10, 0, 0.95)},
                  injuries=[inj(WR1, "Out", 5), *report(6, inj(WR1, None, 6, practice="Full Participation in Practice"))])
    a = rows_by_id(iw.workload_handoff(out5, S, 5))
    b = rows_by_id(iw.workload_handoff(back6, S, 6))
    assert a[WR2]["ho_tgt_share"] > a[WR2]["ho_base_tgt_share"]
    assert b[WR2]["ho_tgt_share"] < a[WR2]["ho_tgt_share"] and b[WR2]["ho_lead_returning"] == 1
    assert b[TE1]["ho_tgt_share"] == pytest.approx(b[TE1]["ho_base_tgt_share"])   # the TE group is unaffected


def test_te1_out_moves_targets_within_the_te_group():
    ho = rows_by_id(iw.workload_handoff(world(injuries=report(6, inj(TE1, "Out", 6))), S, 6))
    assert ho[TE2]["ho_tgt_share"] > 2 * ho[TE2]["ho_base_tgt_share"] and ho[TE2]["ho_lead_out"] == 1


def test_a_genuine_role_change_is_not_undone():
    """No injury: STAR's last four games were a reduced role (healthy, no listing). The established role follows
    his healthy games, so the reduction counts; a healthy scratch lowers his claim through participation."""
    cut = {(STAR, w): (2, 6, 0.3) for w in (2, 3, 4, 5)}
    cut.update({(BACKUP, w): (2, 16, 0.6) for w in (2, 3, 4, 5)})
    src = world(usage=cut)
    base = rows_by_id(iw.established_roles(world(), S, 6))[STAR]["role_car_share"]
    assert rows_by_id(iw.established_roles(src, S, 6))[STAR]["role_car_share"] < base - 0.15
    scratch = world(skip={STAR: (4, 5)}, injuries=report(6))   # missed two games, never on a report: role decision
    tl = rows_by_id(iw.injury_timeline(scratch, S, 6))
    assert tl[STAR]["missed_injured"] == 0 and tl[STAR]["status_class"] == iw.AVAILABLE
    ho = rows_by_id(iw.workload_handoff(scratch, S, 6))
    assert ho[STAR]["ho_participation"] == pytest.approx(0.5)
    assert ho[STAR]["ho_car_share"] < rows_by_id(iw.workload_handoff(world(), S, 6))[STAR]["ho_car_share"]


def test_starter_changing_teams_leaves_the_old_room():
    moved = {(STAR, w): ("BBB", "ACT") for w in (5, 6)}
    src = world(roster=moved)
    tl = rows_by_id(iw.injury_timeline(src, S, 6))
    assert tl[STAR]["team"] == "BBB"
    ho = rows_by_id(iw.workload_handoff(src, S, 6))
    assert ho[STAR]["ho_team"] == "BBB"
    assert ho[BACKUP]["ho_car_share"] > 2 * rows_by_id(iw.workload_handoff(world(), S, 6))[BACKUP]["ho_car_share"]


def test_ir_minimum_window_dates_eligibility_and_nothing_more():
    roster = {(STAR, w): ("AAA", "RES") for w in (3, 4, 5, 6)}
    codes = pl.DataFrame([{"season": S, "week": w, "gsis_id": STAR, "abbr": "R01"} for w in (3, 4, 5, 6)])
    src = world(range(1, 6), skip={STAR: (3, 4, 5)}, roster=roster, last_week=10)
    t6 = rows_by_id(iw.injury_timeline(src, S, 6, roster_codes=codes))[STAR]
    assert t6["nfl_reserve"] == 1 and t6["reserve_earliest_week"] == 7       # 4 team games (3-6) must be missed
    assert t6["status_class"] == iw.UNAVAILABLE and "not eligible before week 7" in t6["status_reason"]
    t8 = rows_by_id(iw.injury_timeline(src, S, 8, cutoff_week=6, roster_codes=codes))[STAR]
    assert t8["status_class"] == iw.UNCERTAIN and "return not confirmed" in t8["status_reason"]
    no_code = rows_by_id(iw.injury_timeline(src, S, 8, cutoff_week=6))[STAR]
    assert no_code["reserve_earliest_week"] is None and no_code["status_class"] == iw.UNAVAILABLE


def test_bye_weeks_are_not_missed_games():
    src = world(range(1, 6), byes={"AAA": 3}, skip={STAR: (5,)}, injuries=[inj(STAR, "Out", 5), *report(6)])
    t = rows_by_id(iw.injury_timeline(src, S, 6))[STAR]
    assert t["missed_team_games"] == 1 and t["missed_injured"] == 1


def test_missing_or_stale_report_is_labeled_never_read_as_healthy():
    src = world(range(1, 6), skip={STAR: (5,)}, injuries=[inj(STAR, "Out", 5)])     # no week-6 rows for AAA
    t = rows_by_id(iw.injury_timeline(src, S, 6))
    assert t[STAR]["designation"] == "no_report" and t[STAR]["status_class"] == iw.UNCERTAIN
    assert t[STAR]["confidence"] == "roster_only" and t[BACKUP]["status_class"] == iw.AVAILABLE
    none = Sources(src.panel, src.games, src.defense, src.rosters, None)
    assert rows_by_id(iw.injury_timeline(none, S, 6))[STAR]["designation"] == "no_report"


def test_missing_roster_row_and_unknown_depth():
    """A player with box scores but no roster row stays on his panel team; a roster player with no history gets
    the newcomer prior (a small claim, never zero and never a starter's)."""
    src = world()
    ros = src.rosters.filter(pl.col("gsis_id") != RB3)
    new = pl.DataFrame([{"season": S, "week": 5, "gsis_id": "00-NEW", "team": "AAA", "position": "RB", "status": "ACT"}],
                       schema=ROSTER_SCHEMA)
    src2 = Sources(src.panel, src.games, src.defense, pl.concat([ros, new]), src.injuries)
    tl = rows_by_id(iw.injury_timeline(src2, S, 6))
    assert tl[RB3]["team"] == "AAA" and tl[RB3]["roster_status"] is None
    ho = rows_by_id(iw.workload_handoff(src2, S, 6))
    assert 0 < ho["00-NEW"]["ho_car_share"] < ho[BACKUP]["ho_car_share"]


def test_fantasy_ir_is_not_an_input():
    assert not any("fantasy" in c or "espn" in c for c in iw.TIMELINE_SCHEMA)
    assert not any("fantasy" in c or "espn" in c for c in iw.HANDOFF_SCHEMA)


def test_point_in_time_later_reports_and_games_change_nothing():
    """A forecast for week 8 made at the week-6 cutoff: week-6+ box scores, week-6+ roster rows and week-7/8
    reports (published after the cutoff) do not change the timeline or the handoff."""
    base = world(range(1, 6), injuries=report(6, inj(STAR, "Out", 6)))
    later = world(range(1, 8), skip={STAR: (6, 7)}, usage={(BACKUP, 6): (4, 25, 0.9)},
                  injuries=[*report(6, inj(STAR, "Out", 6)), *report(7, inj(STAR, "Out", 7)),
                            *report(8, inj(STAR, None, 8))])
    for f in (lambda s: iw.injury_timeline(s, S, 8, cutoff_week=6),
              lambda s: iw.workload_handoff(s, S, 8, cutoff_week=6)):
        a, b = f(base).sort("gsis_id"), f(later).sort("gsis_id")
        assert a.equals(b)


def test_reruns_are_identical():
    src = scenario_d()
    assert iw.workload_handoff(src, S, 7).equals(iw.workload_handoff(src, S, 7))
    assert iw.injury_timeline(src, S, 7).equals(iw.injury_timeline(src, S, 7))


def test_cutoff_after_target_is_refused():
    with pytest.raises(ValueError):
        iw.injury_timeline(world(), S, 6, cutoff_week=7)


# ----------------------------------------------------------------------------- governance and isolation


def test_preregistered_rules_and_features_exist():
    assert set(ic.DECISION_RULES) >= {"development", "confirmation", "outcome", "never"}
    assert "promotion" in ic.DECISION_RULES["never"]
    for feats in ic.GROUP.values():
        assert all(f in iw.HANDOFF_SCHEMA for f in feats)
    assert ic.GROUP["QB"] == []


def test_research_modules_write_nothing():
    for mod in (iw, ic):
        text = pathlib.Path(mod.__file__).read_text().upper()
        for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE TABLE", "WRITE_PARQUET", "WRITE_TEXT"):
            assert verb not in text, (mod.__name__, verb)


# Only the production model ifp-2.1.0 (injury_model) and the offline pipeline that trains and stores it import the
# handoff modules; the hosted API (app.main) never loads them.
PIPELINE_IMPORTERS = {"injury_workload.py", "injury_challenger.py", "injury_model.py", "independent_projection_store.py",
                      "weekly_learning.py"}


def test_only_the_model_pipeline_imports_the_injury_modules():
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    for path in root.rglob("*.py"):
        if path.name in PIPELINE_IMPORTERS:
            continue
        tree = ast.parse(path.read_text())
        names = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
            {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        assert not {x for x in names if re.search(r"injury_(workload|challenger|model)$", x)}, path
    code = ("import sys, app.main; bad = [m for m in ('app.analytics.injury_workload', 'app.analytics.injury_challenger', "
            "'app.analytics.injury_model') if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)")
    assert subprocess.run([sys.executable, "-c", code], cwd=root.parent, capture_output=True).returncode == 0
