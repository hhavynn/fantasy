"""Evidence recorded alongside a projection move (analytics/mover_evidence.py).

Temp DuckDB only (init_db); the nflverse tables are created the way the sync does, with the columns the
reader uses. Window = the two stored ESPN snapshots of the real database: 2026-10-15T06:46:06Z and
2026-10-16T04:02:26Z; week 6's Thursday game (DEN vs KC, 2026-10-16T00:15Z) is the one completed game
between them, while BUF vs SEA kicks off after the window."""

import copy
import re
from datetime import date, datetime, timedelta

import pytest

from app.analytics import mover_evidence as me
from app.db.connection import init_db

SEASON = 2026
FROM, TO = "2026-10-15T06:46:06Z", "2026-10-16T04:02:26Z"
WINDOW_WEEKS = list(range(7, 18))
CAUSAL = re.compile(r"\b(because|due to|led to|leads to|lead to|caused?|causes|causing|drove|driven|drives|"
                    r"results? in|resulted in|owing to|thanks to|reason|explained by|explains)\b", re.I)

# espn_player_id -> (name, position, team, gsis_id)
PLAYERS = {
    1: ("Wide One", "WR", "KC", "G1"),        # usage changes, every metric
    2: ("Run Back", "RB", "KC", "G2"),        # carries / backfield share + reserve roster status
    3: ("Tight End", "TE", "DEN", "G3"),      # depth chart rank 2 -> 1
    4: ("Unmapped", "WR", "DEN", None),       # no player_id_map row
    5: ("Sunday Guy", "WR", "BUF", "G5"),     # game kicks off after the window
    6: ("Quarterback", "QB", "DEN", "G6"),    # depth chart team DEN -> KC
    7: ("Injured Guy", "WR", "KC", "G7"),     # injury report Questionable -> Out
    8: ("Flat Guy", "WR", "KC", "G8"),        # every change below threshold
    9: ("Medium Guy", "WR", "KC", "G9"),      # medium-confidence link: not read
    10: ("Espn Q", "WR", "DEN", None),        # ESPN status refreshed inside the window
    11: ("Espn Old", "WR", "DEN", None),      # refreshed before the earlier snapshot
    12: ("Espn Late", "WR", "DEN", None),     # refreshed after the latest snapshot
    13: ("Practice Guy", "WR", "KC", "G13"),  # practice status only
    14: ("Cleared Guy", "WR", "KC", "G14"),   # Doubtful -> not listed
}


def sunday(week: int) -> date:
    return date(2026, 9, 13) + timedelta(days=7 * (week - 1))


def seed(con):
    init_db(con)
    con.execute("""CREATE TABLE nfl_schedules (game_id VARCHAR, season INTEGER, game_type VARCHAR, week INTEGER,
        gameday VARCHAR, gametime VARCHAR, away_team VARCHAR, away_score INTEGER, home_team VARCHAR, home_score INTEGER)""")
    con.execute("""CREATE TABLE nfl_injuries (season INTEGER, game_type VARCHAR, team VARCHAR, week INTEGER,
        gsis_id VARCHAR, report_status VARCHAR, practice_status VARCHAR)""")
    con.execute("""CREATE TABLE nfl_rosters_weekly (season INTEGER, game_type VARCHAR, team VARCHAR, week INTEGER,
        gsis_id VARCHAR, status VARCHAR, status_description_abbr VARCHAR)""")
    con.execute("""CREATE TABLE nfl_depth_charts (dt VARCHAR, team VARCHAR, gsis_id VARCHAR, pos_abb VARCHAR,
        pos_rank INTEGER, season INTEGER)""")
    for week in range(1, 10):
        for away, home in (("KC", "DEN"), ("SEA", "BUF")):
            day, clock, final = str(sunday(week)), "13:00", week <= 5
            if week == 6 and home == "DEN":
                day, clock, final = "2026-10-15", "20:15", True          # Thursday night: completed inside the window
            con.execute("INSERT INTO nfl_schedules VALUES (?, ?, 'REG', ?, ?, ?, ?, ?, ?, ?)",
                        [f"{week}_{away}_{home}", SEASON, week, day, clock, away, 20 if final else None, home,
                         23 if final else None])
    for pid, (name, pos, team, gsis) in PLAYERS.items():
        con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team) VALUES (?, ?, ?, ?)",
                    [pid, name, pos, team])
        if gsis:
            con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_confidence) VALUES (?, ?, ?)",
                        [pid, gsis, "medium" if pid == 9 else "high"])
    for pid, status, synced in ((10, "QUESTIONABLE", "2026-10-16 03:00:00"), (11, "OUT", "2026-10-14 09:00:00"),
                                (12, "OUT", "2026-10-17 09:00:00")):
        con.execute("UPDATE fantasy_players SET injury_status = ?, detail_synced_at = ? WHERE espn_player_id = ?",
                    [status, synced, pid])

    flat = dict(offense_pct=[.6] * 6, target_share=[.2] * 6, carries=[8] * 6, backfield_rush_share=[.5] * 6,
                route_participation=[.6] * 6, xfp=[8.0] * 6)
    spec = {
        "G1": dict(flat, offense_pct=[.40, .60, .62, .62, .64, .81], target_share=[.05, .20, .20, .20, .20, .28],
                   xfp=[3.0, 8.0, 8.0, 8.0, 8.0, 12.0], route_participation=[.5, .6, .6, .6, .6, .8]),
        "G2": dict(flat, carries=[10, 8, 8, 8, 8, 12], backfield_rush_share=[.6, .5, .5, .5, .5, .65]),
        "G8": dict(flat, offense_pct=[.6, .6, .6, .6, .6, .64], target_share=[.2, .2, .2, .2, .2, .22],
                   carries=[8, 8, 8, 8, 8, 9.5], backfield_rush_share=[.5, .5, .5, .5, .5, .57],
                   route_participation=[.6, .6, .6, .6, .6, .67], xfp=[8, 8, 8, 8, 8, 9.9]),
    }
    for gsis, team, pos in (("G1", "KC", "WR"), ("G2", "KC", "RB"), ("G3", "DEN", "TE"), ("G6", "DEN", "QB"),
                            ("G7", "KC", "WR"), ("G8", "KC", "WR"), ("G9", "KC", "WR"), ("G13", "KC", "WR"),
                            ("G14", "KC", "WR"), ("G5", "BUF", "WR")):
        v = spec.get(gsis, flat)
        if gsis == "G9":   # a medium-confidence link whose data would produce a signal if it were read
            v = dict(flat, offense_pct=[.3, .3, .3, .3, .3, .9])
        for i in range(6):
            week = i + 1
            if gsis == "G5" and week == 6:
                continue        # his week-6 game is after the window: forward rows are added below
            usage(con, gsis, team, pos, week, **{k: x[i] for k, x in v.items()})
    # forward rows: kickoff at/after the window end (BUF Sunday of week 6) and weeks 7, 8 (the compared weeks)
    for gsis, team, pos in (("G1", "KC", "WR"), ("G2", "KC", "RB"), ("G3", "DEN", "TE"), ("G7", "KC", "WR"),
                            ("G8", "KC", "WR"), ("G5", "BUF", "WR")):
        for week in (6, 7, 8):
            if gsis == "G5" or week > 6:
                usage(con, gsis, team, pos, week, offense_pct=.5, target_share=.2, carries=8, backfield_rush_share=.5,
                      route_participation=.6, xfp=5.0)
    # injuries / rosters: weeks 5 and 6 for the KC and DEN players, plus forward rows
    inj = [("KC", 5, "G7", "Questionable", "Limited Participation in Practice"), ("KC", 6, "G7", "Out", "Did Not Participate In Practice"),
           ("KC", 5, "G13", None, "Limited Participation in Practice"), ("KC", 6, "G13", None, "Did Not Participate In Practice"),
           ("KC", 5, "G14", "Doubtful", "Did Not Participate In Practice"),
           ("DEN", 5, "G3", None, "Full Participation in Practice"), ("DEN", 6, "G3", None, "Full Participation in Practice"),
           ("KC", 7, "G1", "Out", "Did Not Participate In Practice"),          # week 7: the compared window
           ("BUF", 6, "G5", "Out", "Did Not Participate In Practice")]         # BUF's report for a game after the window
    for team, week, gsis, rep, prac in inj:
        con.execute("INSERT INTO nfl_injuries VALUES (?, 'REG', ?, ?, ?, ?, ?)", [SEASON, team, week, gsis, rep, prac])
    for gsis, team in (("G1", "KC"), ("G2", "KC"), ("G7", "KC"), ("G8", "KC"), ("G13", "KC"), ("G14", "KC"),
                       ("G3", "DEN"), ("G6", "DEN"), ("G5", "BUF")):
        for week in (5, 6, 7):
            status, abbr = ("RES", "R01") if (gsis == "G2" and week >= 6) or (gsis == "G5" and week >= 6) else ("ACT", "A01")
            if gsis == "G5" and week == 6:
                status, abbr = "RES", "R01"
            con.execute("INSERT INTO nfl_rosters_weekly VALUES (?, 'REG', ?, ?, ?, ?, ?)", [SEASON, team, week, gsis, status, abbr])
    # depth charts: one snapshot before the earlier snapshot time, one between, one after the latest
    depth = [("2026-10-14T06:01:55Z", "DEN", "G3", "TE", 3), ("2026-10-15T06:02:57Z", "DEN", "G3", "TE", 2),
             ("2026-10-15T12:37:05Z", "DEN", "G3", "TE", 1), ("2026-10-16T12:00:00Z", "DEN", "G3", "TE", 5),
             ("2026-10-15T06:02:57Z", "DEN", "G6", "QB", 1), ("2026-10-15T12:37:05Z", "KC", "G6", "QB", 1),
             ("2026-10-15T06:02:57Z", "KC", "G1", "WR", 1), ("2026-10-15T12:37:05Z", "KC", "G1", "WR", 1)]
    for dt, team, gsis, pos, rank in depth:
        con.execute("INSERT INTO nfl_depth_charts VALUES (?, ?, ?, ?, ?, ?)", [dt, team, gsis, pos, rank, SEASON])


def usage(con, gsis, team, pos, week, **cols):
    names = ["season", "week", "gsis_id", "team", "position", *cols]
    con.execute(f"INSERT INTO player_week_usage ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                [SEASON, week, gsis, team, pos, *cols.values()])


@pytest.fixture
def db(con):
    seed(con)
    return con


def mover(pid, **kw):
    name, pos, team, gsis = PLAYERS[pid]
    return {"espn_player_id": pid, "name": name, "position": pos, "nfl_team": team, "gsis_id": gsis, "weeks": [],
            "components": None, "evidence": None, **kw}


def ev(con, pids=None, *, source="espn", movers=None, start=FROM, end=TO, **kw):
    movers = movers or [mover(p) for p in (pids or PLAYERS)]
    kw.setdefault("window_weeks", WINDOW_WEEKS)
    return me.explain(con, SEASON, movers, window_from=start, window_to=end, source=source, **kw)


def sig(evidence, key):
    found = [s for s in evidence["signals"] if s["key"] == key]
    assert len(found) == 1, (key, [s["key"] for s in evidence["signals"]])
    return found[0]


def keys(evidence):
    return [s["key"] for s in evidence["signals"]]


def texts(evidence):
    out = [evidence["headline"] or "", evidence["note"], *evidence["missing"]]
    for s in evidence["signals"]:
        out += [s["label"], s["detail"], s["source"], str(s["before"]), str(s["after"])]
    return out


# ----------------------------------------------------------------------------- usage


def test_usage_signals_state_before_after_weeks_and_source(db):
    e = ev(db)[1]
    assert keys(e) == ["snap_share", "target_share", "route_participation", "xfp"]    # by change / threshold
    snap = sig(e, "snap_share")
    assert (snap["kind"], snap["unit"], snap["source"]) == ("usage", "share", "nflverse snap counts")
    assert snap["before"] == pytest.approx(0.62) and snap["after"] == pytest.approx(0.81)
    assert snap["weeks_before"] == [2, 3, 4, 5] and snap["weeks_after"] == [6]       # the last four games, not week 1
    assert e["headline"] == "Snap share 81% in week 6, up from 62%"
    t = sig(e, "target_share")
    assert (t["before"], t["after"]) == (pytest.approx(0.20), pytest.approx(0.28))
    x = sig(e, "xfp")
    assert (x["before"], x["after"], x["unit"]) == (pytest.approx(8.0), pytest.approx(12.0), "points")
    r = sig(e, "route_participation")
    assert (r["before"], r["after"]) == (pytest.approx(0.6), pytest.approx(0.8))
    assert "carries" not in keys(e) and "backfield_share" not in keys(e)              # RB-only metrics
    assert e["information_window"] == {"from": FROM, "to": TO,
                                       "games": [{"week": 6, "team": "KC", "kickoff": "2026-10-16T00:15:00Z"}]}
    assert e["note"] == "Observed over the same period as the change. Not shown to be the cause."


def test_running_back_carries_and_backfield_share(db):
    e = ev(db, [2])[2]
    carries, share = sig(e, "carries"), sig(e, "backfield_share")
    assert (carries["before"], carries["after"], carries["unit"]) == (pytest.approx(8.0), pytest.approx(12.0), "count")
    assert (share["before"], share["after"]) == (pytest.approx(0.5), pytest.approx(0.65))
    assert carries["weeks_before"] == [2, 3, 4, 5] and carries["weeks_after"] == [6]


def test_changes_below_threshold_produce_no_usage_signal(db):
    e = ev(db, [8])[8]
    assert [s for s in e["signals"] if s["kind"] == "usage"] == []
    assert e["headline"] is None
    assert e["missing"]            # says what was checked


def test_threshold_is_inclusive_and_per_metric(db):
    db.execute("UPDATE player_week_usage SET offense_pct = 0.65 WHERE gsis_id = 'G8' AND week = 6")     # +0.05 over .60
    db.execute("UPDATE player_week_usage SET xfp = 10.0 WHERE gsis_id = 'G8' AND week = 6")             # +2.0 over 8.0
    db.execute("UPDATE player_week_usage SET target_share = 0.23 WHERE gsis_id = 'G8' AND week = 6")    # +0.03 over .20
    e = ev(db, [8])[8]
    assert {"snap_share", "xfp", "target_share"} <= set(keys(e))


def test_multi_game_window_averages_the_window_games(db):
    e = ev(db, [1], start="2026-10-10T00:00:00Z")[1]       # week 5 (Oct 11) and week 6 both fall in the window
    snap = sig(e, "snap_share")
    assert snap["weeks_after"] == [5, 6] and snap["weeks_before"] == [1, 2, 3, 4]
    assert snap["after"] == pytest.approx((0.64 + 0.81) / 2)
    assert e["headline"].startswith("Snap share averaged")
    assert [g["week"] for g in e["information_window"]["games"]] == [5, 6]


def test_missing_current_data_is_reported_not_zero_filled(db):
    db.execute("UPDATE player_week_usage SET offense_pct = NULL, xfp = NULL WHERE gsis_id = 'G1' AND week = 6")
    e = ev(db, [1])[1]
    assert "snap_share" not in keys(e) and "xfp" not in keys(e)
    assert "No snap data for week 6 yet" in e["missing"]
    assert "No expected-points (xFP) data for week 6 yet" in e["missing"]


# ----------------------------------------------------------------------------- availability


def test_injury_designation_change_is_an_availability_signal(db):
    e = ev(db, [7])[7]
    s = sig(e, "injury_designation")
    assert (s["kind"], s["before"], s["after"], s["unit"]) == ("availability", "Questionable", "Out", "text")
    assert (s["weeks_before"], s["weeks_after"], s["source"]) == ([5], [6], "nflverse injury reports")
    assert e["headline"] == "Listed Out on the week 6 injury report (Questionable in week 5)"


def test_designation_cleared_and_practice_only_changes(db):
    cleared = ev(db, [14])[14]
    s = sig(cleared, "injury_designation")
    assert (s["before"], s["after"]) == ("Doubtful", "Not listed")
    assert cleared["headline"] == "Not listed on the week 6 injury report, Doubtful in week 5"
    practice = ev(db, [13])[13]
    p = sig(practice, "practice_status")
    assert (p["before"], p["after"]) == ("limited practice", "did not practice")
    assert "injury_designation" not in keys(practice)


def test_reserve_roster_status_change_comes_first_in_the_headline(db):
    e = ev(db, [2])[2]
    assert keys(e)[0] == "roster_status"
    r = sig(e, "roster_status")
    assert (r["kind"], r["before"], r["after"], r["weeks_before"], r["weeks_after"]) == (
        "availability", "Active", "Reserve/Injured", [5], [6])
    assert e["headline"] == "Roster status Reserve/Injured in week 6, Active in week 5"   # not the carries change


def test_unchanged_roster_and_report_give_no_availability_signal(db):
    e = ev(db, [1])[1]
    assert not [s for s in e["signals"] if s["kind"] == "availability"]
    assert "No injury-report or roster-status change recorded between the snapshots" in e["missing"]


def test_espn_status_only_when_refreshed_inside_the_window(db):
    got = ev(db, [10, 11, 12])
    s = sig(got[10], "espn_injury_status")
    assert (s["kind"], s["before"], s["after"]) == ("availability", None, "Questionable")
    # ESPN keeps no status history: the status at the check is informational, never presented as a change
    assert got[10]["headline"] is None
    assert "no earlier ESPN status is stored" in s["detail"]
    assert "espn_injury_status" not in keys(got[11]) and "espn_injury_status" not in keys(got[12])
    assert any("refreshed after the latest snapshot" in m for m in got[12]["missing"])


def test_a_status_on_the_snapshot_boundary_second_is_read(db):
    db.execute("UPDATE fantasy_players SET detail_synced_at = '2026-10-16 04:02:26.977539' WHERE espn_player_id = 10")
    assert "espn_injury_status" in keys(ev(db, [10])[10])        # taken_at is stored truncated to the second


# ----------------------------------------------------------------------------- role


def test_depth_chart_rank_change_between_the_two_snapshot_times(db):
    e = ev(db, [3])[3]
    s = sig(e, "depth_rank")
    assert (s["kind"], s["before"], s["after"], s["source"]) == ("role", 2, 1, "nflverse depth charts")
    assert e["headline"] == "Depth chart TE1 on Oct 15, up from TE2 on Oct 15"
    assert "2026-10-16T12:00:00Z" not in " ".join(texts(e))        # the chart published after the window is never read


def test_team_change_between_depth_chart_snapshots(db):
    e = ev(db, [6])[6]
    s = sig(e, "team")
    assert (s["kind"], s["before"], s["after"]) == ("role", "DEN", "KC")
    assert e["headline"] == "Listed with KC, previously DEN"
    assert "depth_rank" not in keys(e)          # rank 1 -> 1


def test_unchanged_depth_chart_gives_no_role_signal(db):
    assert not [s for s in ev(db, [1])[1]["signals"] if s["kind"] == "role"]


# ----------------------------------------------------------------------------- schedule and model


def test_model_signals_only_for_the_independent_source(db):
    comps = {"availability": 1.2, "per_game": 10.3}
    weeks = [{"week": 9, "before": 0.0, "after": 6.0, "delta": 6.0, "bye": False, "status_before": "withheld_unavailable",
              "status_after": "projected", "horizon_before": 3, "horizon_after": 3},
             {"week": 7, "before": 8.0, "after": 8.0, "delta": 0.0, "bye": False, "status_before": "projected",
              "status_after": "projected", "horizon_before": 1, "horizon_after": 1}]
    ours = ev(db, movers=[mover(4, components=comps, weeks=weeks)], source="independent")[4]
    assert keys(ours) == ["per_game_component", "coverage_status", "availability_component"]
    assert ours["headline"] == "Per-game outlook moved the total by +10.3"
    a = sig(ours, "availability_component")
    assert (a["kind"], a["after"], a["unit"], a["detail"]) == ("model", 1.2, "points",
                                                                "Availability (the model's chance of appearing) moved the total by +1.2.")
    c = sig(ours, "coverage_status")
    assert (c["before"], c["after"], c["weeks_after"]) == ("withheld (unavailable)", "projected", [9])
    espn = ev(db, movers=[mover(4, components=comps, weeks=weeks)], source="espn")[4]
    assert not [s for s in espn["signals"] if s["kind"] == "model"] and espn["headline"] is None
    small = ev(db, movers=[mover(4, components={"availability": 0.02, "per_game": -0.04})], source="independent")[4]
    assert small["signals"] == []


def test_model_components_rank_below_recorded_data(db):
    e = ev(db, movers=[mover(1, components={"availability": 0.0, "per_game": 50.0})], source="independent")[1]
    assert e["headline"].startswith("Snap share")
    assert keys(e)[-1] == "per_game_component"


def test_opponent_change_between_the_two_runs_is_ours_only(db):
    for run, week, opp in (("early", 8, "BUF"), ("late", 8, "SEA"), ("early", 7, "DEN"), ("late", 7, "DEN")):
        db.execute("INSERT INTO independent_projection_rows (run_id, week, espn_player_id, opponent) VALUES (?, ?, 1, ?)",
                   [f"{run}-{week}", week, opp])
    weeks = [{"week": 7}, {"week": 8}]
    kw = dict(baseline_run_ids={7: "early-7", 8: "early-8"}, current_run_ids={"7": "late-7", "8": "late-8"})
    ours = ev(db, movers=[mover(1, weeks=weeks)], source="independent", **kw)[1]
    s = sig(ours, "opponent")
    assert (s["kind"], s["before"], s["after"], s["weeks_after"]) == ("schedule", "BUF", "SEA", [8])
    assert "opponent" not in keys(ev(db, movers=[mover(1, weeks=weeks)], source="espn", **kw)[1])


def test_bye_week_is_informational(db):
    e = ev(db, movers=[mover(4, weeks=[{"week": 9, "bye": True}, {"week": 10, "bye": False}])])[4]
    s = sig(e, "bye_week")
    assert (s["kind"], s["weeks_after"]) == ("schedule", [9])
    assert e["headline"] is None          # a bye is in both snapshots: informational, never the headline
    first = ev(db, movers=[mover(1, weeks=[{"week": 9, "bye": True}])])[1]
    assert keys(first)[-1] == "bye_week" and first["headline"].startswith("Snap share")    # after every real signal


# ----------------------------------------------------------------------------- nothing to say


def test_no_completed_game_between_the_snapshots(db):
    e = ev(db, [5])[5]
    assert e["signals"] == [] and e["headline"] is None
    assert "No completed game between the two snapshots" in e["missing"]
    assert e["information_window"]["games"] == []
    assert not [s for s in e["signals"] if s["kind"] == "usage"]


def test_a_game_that_started_without_a_final_score(db):
    db.execute("UPDATE nfl_schedules SET home_score = NULL, away_score = NULL WHERE week = 6 AND home_team = 'DEN'")
    e = ev(db, [1])[1]
    assert e["signals"] == [] and e["information_window"]["games"] == []
    assert "The week 6 game kicked off between the snapshots but was not over by the latest snapshot" in e["missing"]


def test_mover_without_a_high_confidence_nfl_id(db):
    got = ev(db, [4, 9])
    for pid in (4, 9):
        assert got[pid]["signals"] == [] and got[pid]["headline"] is None
        assert got[pid]["missing"][0].startswith("No high-confidence NFL ID for this player")
        assert got[pid]["note"] == me.NOTE


def test_player_who_did_not_appear_in_the_completed_game(db):
    db.execute("DELETE FROM player_week_usage WHERE gsis_id = 'G8' AND week = 6")
    e = ev(db, [8])[8]
    assert [s for s in e["signals"] if s["kind"] == "usage"] == []
    assert "No usage recorded for week 6 (not in the stored player data)" in e["missing"]
    assert e["information_window"]["games"][0]["week"] == 6


def test_missing_explanations_give_a_null_headline(db):
    for table in ("player_week_usage", "nfl_injuries", "nfl_rosters_weekly", "nfl_depth_charts"):
        db.execute(f"DELETE FROM {table}")
    got = ev(db, [1, 2, 3, 6, 7])
    for e in got.values():
        assert e["headline"] is None and e["signals"] == [] and e["missing"]
    db.execute("DROP TABLE nfl_depth_charts")
    db.execute("DROP TABLE nfl_injuries")
    db.execute("DROP TABLE nfl_schedules")
    assert all(e["headline"] is None for e in ev(db, [1, 5]).values())            # no crash on absent tables


def test_bad_window_is_rejected(db):
    with pytest.raises(ValueError):
        ev(db, [1], start=TO, end=FROM)


# ----------------------------------------------------------------------------- leakage


def snapshot(con, source="espn", **kw):
    movers = [mover(p, components={"availability": 1.0, "per_game": 2.0},
                    weeks=[{"week": 9, "bye": True, "status_before": "projected", "status_after": "projected"}])
              for p in PLAYERS]
    return ev(con, movers=movers, source=source, **kw)


def test_nothing_at_or_after_the_window_end_or_from_compared_weeks_is_read(db):
    before = snapshot(db)
    assert before[1]["signals"] and before[7]["signals"] and before[3]["signals"]
    late = "(week >= 7 OR (week = 6 AND team IN ('BUF', 'SEA')))"      # kickoff at/after the latest snapshot
    db.execute(f"UPDATE player_week_usage SET offense_pct = 0.01, target_share = 0.99, carries = 99, xfp = 99.0, "
               f"backfield_rush_share = 0.01, route_participation = 0.01 WHERE {late}")
    db.execute(f"UPDATE nfl_injuries SET report_status = 'Doubtful', practice_status = 'Limited Participation in Practice' WHERE {late}")
    db.execute(f"UPDATE nfl_rosters_weekly SET status = 'CUT', status_description_abbr = 'W03' WHERE {late}")
    db.execute("UPDATE nfl_schedules SET home_score = 1, away_score = 2 WHERE week >= 7 OR (week = 6 AND home_team = 'BUF')")
    db.execute("UPDATE nfl_depth_charts SET pos_rank = 9, team = 'ZZZ' WHERE dt > '2026-10-16T04:02:26Z'")
    db.execute("INSERT INTO nfl_depth_charts VALUES ('2026-10-16T05:00:00Z', 'ZZZ', 'G1', 'WR', 7, 2026)")
    assert snapshot(db) == before
    assert snapshot(db, source="independent") == snapshot(db, source="independent")
    # the control: the same kind of edit on a row INSIDE the window changes the answer
    db.execute("UPDATE player_week_usage SET offense_pct = 0.30 WHERE gsis_id = 'G1' AND week = 6")
    assert snapshot(db)[1] != before[1]


def test_rows_of_compared_weeks_are_never_read_even_when_their_game_is_final(db):
    base = ev(db, [1])[1]
    assert base["signals"]
    got = ev(db, [1], window_weeks=[6])[1]            # pretend week 6 is a compared week: its data is off limits
    assert got["signals"] == [] and got["information_window"]["games"] == []


def test_injury_rows_modified_after_the_window_are_not_read(db):
    db.execute("ALTER TABLE nfl_injuries ADD COLUMN date_modified TIMESTAMP")
    db.execute("UPDATE nfl_injuries SET date_modified = '2026-10-15 20:00:00'")
    assert sig(ev(db, [7])[7], "injury_designation")["after"] == "Out"
    db.execute("UPDATE nfl_injuries SET date_modified = '2026-10-17 20:00:00' WHERE gsis_id = 'G7' AND week = 6")
    late = ev(db, [7])[7]
    assert "injury_designation" not in keys(late)
    assert "No injury report recorded for KC in week 6" in late["missing"]


# ----------------------------------------------------------------------------- wording


def test_no_causal_wording_in_any_generated_text(db):
    movers = [mover(p, components={"availability": 1.2, "per_game": -3.4},
                    weeks=[{"week": 9, "bye": True, "status_before": "withheld_unavailable", "status_after": "projected",
                            "delta": 3.0}]) for p in PLAYERS]
    for source in ("espn", "independent"):
        got = ev(db, movers=movers, source=source)
        seen = 0
        for e in got.values():
            assert e["note"] == me.NOTE
            for text in (x for x in texts(e) if x != me.NOTE):          # the fixed note says "Not shown to be the cause": it is the spec's sentence
                assert not CAUSAL.search(text), text
                seen += 1
        assert seen > 50
        assert {s["kind"] for e in got.values() for s in e["signals"]} >= {"usage", "availability", "role", "schedule"}


# ----------------------------------------------------------------------------- attach


def result(db, **kw):
    ids = (1, 2, 7, 4)
    out = {"contract": "projection-movers/v1", "season": SEASON, "source": "espn", "available": True,
           "baseline": {"id": "2026-10-14", "taken_at": FROM}, "current": {"id": "2026-10-15", "taken_at": TO},
           "window": {"weeks": WINDOW_WEEKS, "excluded_weeks": []},
           "risers": [mover(1), mover(2)], "fallers": [mover(7), mover(4)],
           "players": [mover(p) for p in ids]}
    out.update(kw)
    return out


def test_attach_sets_evidence_on_risers_and_fallers_only(db):
    res = result(db)
    out = me.attach(db, SEASON, res)
    assert out is res
    for m in res["risers"] + res["fallers"]:
        assert set(m["evidence"]) == {"headline", "signals", "missing", "information_window", "note"}
    assert res["risers"][0]["evidence"]["headline"] == "Snap share 81% in week 6, up from 62%"
    assert res["fallers"][0]["evidence"]["headline"].startswith("Listed Out")
    assert res["fallers"][1]["evidence"]["headline"] is None
    assert all(m["evidence"] is None for m in res["players"])
    assert res["window"]["weeks"] == WINDOW_WEEKS


def test_players_keep_null_evidence_even_when_they_share_dicts_with_the_lists(db):
    top = [mover(1), mover(7)]
    res = result(db, risers=[top[0]], fallers=[top[1]], players=top + [mover(2)])      # the engine reuses the dicts
    me.attach(db, SEASON, res)
    assert res["risers"][0]["evidence"] and res["fallers"][0]["evidence"]
    assert all(m["evidence"] is None for m in res["players"])


def test_attach_uses_the_source_and_run_ids_of_the_result(db):
    comps = {"availability": 1.2, "per_game": 10.3}
    res = result(db, source="independent", risers=[mover(4, components=comps)], fallers=[],
                 baseline={"taken_at": FROM, "run_ids": {8: "early-8"}}, current={"taken_at": TO, "run_ids": {8: "late-8"}})
    me.attach(db, SEASON, res)
    assert keys(res["risers"][0]["evidence"])[:1] == ["per_game_component"]


def test_unavailable_result_is_returned_unchanged(db):
    res = {"available": False, "reason": "Fewer than two snapshots", "risers": [mover(1)], "fallers": [], "players": []}
    before = copy.deepcopy(res)
    assert me.attach(db, SEASON, res) is res
    assert res == before


def test_empty_lists_are_fine(db):
    res = result(db, risers=[], fallers=[])
    assert me.attach(db, SEASON, res) is res


# ----------------------------------------------------------------------------- query count


class Counting:
    def __init__(self, con):
        self.con, self.n = con, 0

    def execute(self, *a, **k):
        self.n += 1
        return self.con.execute(*a, **k)

    def __getattr__(self, name):
        return getattr(self.con, name)


def test_query_count_does_not_grow_with_the_mover_count(db):
    for i in range(100, 130):
        db.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team) VALUES (?, 'P', 'WR', 'KC')", [i])
        db.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_confidence) VALUES (?, ?, 'high')", [i, f"X{i}"])
        for week in range(1, 7):
            usage(db, f"X{i}", "KC", "WR", week, offense_pct=0.5 if week < 6 else 0.8, target_share=0.2)
    counts = {}
    for n, source in ((1, "espn"), (30, "espn"), (30, "independent")):
        movers = [{"espn_player_id": 100 + i, "name": "P", "position": "WR", "nfl_team": "KC", "weeks": [],
                   "components": {"availability": 1.0, "per_game": 1.0}} for i in range(n)]
        counting = Counting(db)
        got = me.explain(counting, SEASON, movers, window_from=FROM, window_to=TO, source=source, window_weeks=WINDOW_WEEKS,
                         baseline_run_ids={8: "early-8"}, current_run_ids={8: "late-8"})
        assert len(got) == n and got[100]["headline"] == "Snap share 80% in week 6, up from 50%"
        counts[(n, source)] = counting.n
    assert counts[(1, "espn")] == counts[(30, "espn")] <= 25, counts      # existence/column checks included
    assert counts[(30, "independent")] == counts[(1, "espn")] + 2, counts      # the run-row lookups, once


def test_a_game_still_being_played_at_the_latest_snapshot_is_not_read(db):
    # week 6 DEN-SEA kicked off 00:15 UTC; two hours later the game was not over, so none of it is evidence
    e = ev(db, [1], end="2026-10-16T02:15:00Z")[1]
    assert e["information_window"]["games"] == [] and not [s for s in e["signals"] if s["kind"] == "usage"]
    assert "The week 6 game kicked off between the snapshots but was not over by the latest snapshot" in e["missing"]
