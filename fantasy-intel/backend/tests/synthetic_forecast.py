"""Synthetic nflverse-shaped data for independent-projection tests. Invented values only: these
fixtures test code paths and leakage guards; they say nothing about real forecasting accuracy."""

from __future__ import annotations

import json
import random

import polars as pl

from app.db.connection import connect, init_db

TEAMS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]
SLOTS = [("QB", "QB"), ("RB1", "RB"), ("RB2", "RB"), ("WR1", "WR"), ("WR2", "WR"), ("WR3", "WR"), ("TE", "TE")]
WEEKS = 17

# A league: full PPR, 4-pt passing TD, -2 INT / fumble lost, 2-pt, return TDs; kicker and D/ST rules.
SCORING_ITEMS = [
    {"statId": 3, "points": 0.04, "pointsOverrides": {}}, {"statId": 4, "points": 4.0, "pointsOverrides": {}},
    {"statId": 19, "points": 2.0, "pointsOverrides": {}}, {"statId": 20, "points": -2.0, "pointsOverrides": {}},
    {"statId": 24, "points": 0.1, "pointsOverrides": {}}, {"statId": 25, "points": 6.0, "pointsOverrides": {}},
    {"statId": 26, "points": 2.0, "pointsOverrides": {}}, {"statId": 42, "points": 0.1, "pointsOverrides": {}},
    {"statId": 43, "points": 6.0, "pointsOverrides": {}}, {"statId": 44, "points": 2.0, "pointsOverrides": {}},
    {"statId": 53, "points": 1.0, "pointsOverrides": {}}, {"statId": 63, "points": 6.0, "pointsOverrides": {}},
    {"statId": 72, "points": -2.0, "pointsOverrides": {}}, {"statId": 101, "points": 6.0, "pointsOverrides": {"16": 6.0}},
    {"statId": 102, "points": 6.0, "pointsOverrides": {"16": 6.0}}, {"statId": 80, "points": 3.0, "pointsOverrides": {}},
    {"statId": 86, "points": 1.0, "pointsOverrides": {}}, {"statId": 89, "points": 0.0, "pointsOverrides": {"16": 5.0}},
    {"statId": 103, "points": 6.0, "pointsOverrides": {"16": 6.0}},
]


def gsis(team: str, slot: str) -> str:
    return f"00-{team}-{slot}"


def rounds(weeks: int) -> list[list[tuple[str, str]]]:
    t = list(TEAMS)
    out = []
    for r in range(weeks):
        out.append([(t[i], t[-1 - i]) if (r + i) % 2 == 0 else (t[-1 - i], t[i]) for i in range(len(t) // 2)])
        t = [t[0]] + [t[-1]] + t[1:-1]
    return out


def generate(seasons: list[int], seed: int = 7, iid_noise: bool = False, weeks: int = WEEKS,
             unfinished: dict[int, int] | None = None) -> dict[str, pl.DataFrame]:
    """Frames for every table the forecast pipeline reads. `iid_noise`: box scores are pure noise,
    independent of player identity and history (any honest model must look mediocre).
    `unfinished`: {season: first week without results} (later weeks are schedule-only)."""
    rng = random.Random(seed)
    skill = {gsis(t, s): rng.uniform(0.5, 1.5) for t in TEAMS for s, _ in SLOTS}
    stats, snaps, xfp, pbp, team_week, defense, sched = [], [], [], [], [], [], []
    for season in seasons:
        last_final = (unfinished or {}).get(season, weeks + 1)
        for week, games in enumerate(rounds(weeks), start=1):
            for gi, (home, away) in enumerate(games):
                final = week < last_final
                hs, as_ = (rng.randint(10, 35), rng.randint(10, 35)) if final else (None, None)
                sched.append({"season": season, "week": week, "game_type": "REG", "game_id": f"{season}_{week:02d}_{away}_{home}",
                              "gameday": f"{season}-09-{min(28, week):02d}" if week <= 17 else f"{season}-12-01",
                              "gametime": "13:00", "home_team": home, "away_team": away, "home_score": hs,
                              "away_score": as_, "result": None if hs is None else hs - as_})
                if not final:
                    continue
                for team, opp in ((home, away), (away, home)):
                    tt = tr = trb = 0
                    for slot, pos in SLOTS:
                        g = gsis(team, slot)
                        k = 1.0 if iid_noise else skill[g]
                        tgt = 0 if pos == "QB" else max(0, int(rng.gauss({"RB": 3, "WR": 6, "TE": 4}[pos] * k, 2)))
                        car = max(0, int(rng.gauss({"QB": 4, "RB": 12 if slot == "RB1" else 5}.get(pos, 0) * k, 2))) if pos in ("QB", "RB") else 0
                        rec = int(tgt * 0.65)
                        att = max(10, int(rng.gauss(34 * k, 5))) if pos == "QB" else 0
                        row = {"player_id": g, "position": pos, "season": season, "week": week, "season_type": "REG",
                               "team": team, "opponent_team": opp, "attempts": att, "completions": int(att * 0.64),
                               "passing_yards": att * rng.uniform(5, 9) if att else 0.0,
                               "passing_tds": rng.choice([0, 1, 1, 2, 3]) if att else 0,
                               "passing_interceptions": rng.choice([0, 0, 1]) if att else 0,
                               "passing_2pt_conversions": 0, "carries": car,
                               "rushing_yards": car * rng.uniform(2, 6), "rushing_tds": int(rng.random() < 0.05 * car),
                               "rushing_2pt_conversions": 0, "targets": tgt, "receptions": rec,
                               "receiving_yards": rec * rng.uniform(6, 14), "receiving_tds": int(rng.random() < 0.1 * rec),
                               "receiving_2pt_conversions": 0, "receiving_air_yards": tgt * 8.0,
                               "fumbles_lost_total": int(rng.random() < 0.03), "special_teams_tds": 0,
                               "fumble_recovery_tds": 0}
                        stats.append(row)
                        snaps.append({"pfr_player_id": f"pfr-{g}", "season": season, "week": week, "game_type": "REG",
                                      "team": team, "opponent": opp, "offense_snaps": 60,
                                      "offense_pct": round(min(1.0, 0.5 + 0.3 * k * rng.random()), 3)})
                        xfp.append({"player_id": g, "season": season, "week": week,
                                    "total_fantasy_points_exp": round(0.9 * tgt + 0.6 * car + 0.3 * att, 3)})
                        pbp.append({"season": season, "week": week, "player_id": g, "team": team,
                                    "rush_attempts": car, "designed_rushes": car, "targets": tgt,
                                    "rz_targets": int(tgt * 0.15), "rz_carries": int(car * 0.2), "gl_carries": int(car * 0.05),
                                    "inside10_targets": 0, "inside10_carries": 0})
                        tt += tgt
                        tr += car
                        trb += car if pos == "RB" else 0
                    team_week.append({"season": season, "week": week, "team": team, "opponent": opp,
                                      "team_targets": tt, "team_rush_attempts": tr, "team_designed_rushes": tr,
                                      "team_rb_carries": trb, "offense_plays": tt + tr + 5})
                    defense.append({"season": season, "week": week, "defteam": opp, "opponent": team,
                                    "pass_plays": 35, "pass_epa_sum": round(rng.gauss(0, 4), 3), "rush_plays": 25,
                                    "rush_epa_sum": round(rng.gauss(0, 3), 3)})
    players = [{"gsis_id": gsis(t, s), "display_name": f"{t} {s}", "position": p, "latest_team": t,
                "pfr_id": f"pfr-{gsis(t, s)}"} for t in TEAMS for s, p in SLOTS]
    return {"nfl_weekly_stats": pl.DataFrame(stats), "nfl_snap_counts": pl.DataFrame(snaps),
            "nfl_ff_opportunity": pl.DataFrame(xfp), "nfl_pbp_player_week": pl.DataFrame(pbp),
            "nfl_team_week": pl.DataFrame(team_week), "nfl_defense_week": pl.DataFrame(defense),
            "nfl_schedules": pl.DataFrame(sched), "nfl_players": pl.DataFrame(players)}


SCHEMA_TABLES = {"nfl_pbp_player_week", "nfl_team_week", "nfl_defense_week", "nfl_players"}


def write(con, frames: dict[str, pl.DataFrame], skip: tuple[str, ...] = ()) -> None:
    """Raw nflverse tables are created from the frames (as the sync does); schema tables are inserted."""
    for name, df in frames.items():
        if name in skip:
            continue
        con.register("_f", df.to_arrow())
        if name in SCHEMA_TABLES:
            con.execute(f"DELETE FROM {name}")
            con.execute(f"INSERT INTO {name} BY NAME SELECT * FROM _f")
        else:
            con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _f")
        con.unregister("_f")


def database(path, seasons: list[int], skip: tuple[str, ...] = (), **kw):
    con = connect(path)
    init_db(con)
    write(con, generate(seasons, **kw), skip=skip)
    return con


def add_league(con, season: int, current_week: int, scoring_items=None) -> None:
    con.execute("INSERT INTO leagues (league_id, season, name, current_week, final_scoring_period, scoring_items, "
                "synced_at) VALUES (99, ?, 'Synthetic', ?, 17, ?, '2026-10-01 00:00:00')",
                [season, current_week, json.dumps(scoring_items or SCORING_ITEMS)])


def add_participation(frames: dict[str, pl.DataFrame], seed: int = 11, p_out: float = 0.06,
                      p_questionable: float = 0.08) -> dict[str, pl.DataFrame]:
    """Weekly rosters + injury reports, and remove the box scores of players who did not appear.
    Each team also carries a backup QB ("QB2") who plays the whole game only when the starter is
    Out and otherwise takes a few garbage-time snaps a quarter of the time. Like nflverse, the
    week-W roster marks a designated player who sat INA (a game-day inactive); a healthy backup
    who did not play stays ACT. Injury rows carry date_modified two days before kickoff."""
    from datetime import datetime, timedelta, timezone

    rng = random.Random(seed)
    stats, snaps = frames["nfl_weekly_stats"], frames["nfl_snap_counts"]
    sched = {(r["season"], r["week"], t): r for r in frames["nfl_schedules"].iter_rows(named=True)
             for t in (r["home_team"], r["away_team"])}
    removed, added_stats, added_snaps, roster, injuries = set(), [], [], [], []
    for (season, week, team), g in stats.group_by(["season", "week", "team"], maintain_order=True):
        game = sched[(season, week, team)]
        kick = datetime.fromisoformat(f"{game['gameday']}T17:00:00+00:00")
        qb_out = False
        for slot, pos in SLOTS:
            gid = gsis(team, slot)
            r = rng.random()
            status, plays, practice = None, True, "Full Participation in Practice"
            if r < p_out:
                status, plays, practice = "Out", False, "Did Not Participate In Practice"
            elif r < p_out + p_questionable:
                status, practice = "Questionable", "Limited Participation in Practice"
                plays = rng.random() < 0.6
            if status:
                injuries.append({"season": season, "game_type": "REG", "team": team, "week": week, "gsis_id": gid,
                                 "position": pos, "report_status": status, "report_primary_injury": "Knee",
                                 "practice_primary_injury": "Knee", "practice_status": practice,
                                 "date_modified": kick - timedelta(days=2)})
            if not plays:
                removed.add((season, week, gid))
                qb_out |= slot == "QB"
            roster.append({"season": season, "week": week, "gsis_id": gid, "team": team, "position": pos,
                           "game_type": "REG", "status": "ACT" if plays else "INA",
                           "status_description_abbr": "A01"})
        backup = gsis(team, "QB2")
        qb_row = g.filter(pl.col("position") == "QB").to_dicts()[0]
        if qb_out:
            added_stats.append({**qb_row, "player_id": backup})
            added_snaps.append({"pfr_player_id": f"pfr-{backup}", "season": season, "week": week, "game_type": "REG",
                                "team": team, "opponent": qb_row["opponent_team"], "offense_snaps": 60, "offense_pct": 0.97})
        elif rng.random() < 0.25:
            added_snaps.append({"pfr_player_id": f"pfr-{backup}", "season": season, "week": week, "game_type": "REG",
                                "team": team, "opponent": qb_row["opponent_team"], "offense_snaps": 3, "offense_pct": 0.05})
        roster.append({"season": season, "week": week, "gsis_id": backup, "team": team, "position": "QB",
                       "game_type": "REG", "status": "ACT", "status_description_abbr": "A01"})
    gone = pl.DataFrame([{"season": s, "week": w, "gid": g} for s, w, g in removed],
                        schema={"season": pl.Int64, "week": pl.Int64, "gid": pl.String})
    out = dict(frames)
    for name, col, fn in (("nfl_weekly_stats", "player_id", lambda c: pl.col(c)),
                          ("nfl_ff_opportunity", "player_id", lambda c: pl.col(c)),
                          ("nfl_pbp_player_week", "player_id", lambda c: pl.col(c)),
                          ("nfl_snap_counts", "pfr_player_id", lambda c: pl.col(c).str.strip_prefix("pfr-"))):
        df = frames[name].with_columns(fn(col).alias("gid"), pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64))
        out[name] = df.join(gone, on=["season", "week", "gid"], how="anti").drop("gid")
    out["nfl_weekly_stats"] = pl.concat([out["nfl_weekly_stats"], pl.DataFrame(added_stats).cast(
        out["nfl_weekly_stats"].schema)]) if added_stats else out["nfl_weekly_stats"]
    out["nfl_snap_counts"] = pl.concat([out["nfl_snap_counts"], pl.DataFrame(added_snaps).cast(
        out["nfl_snap_counts"].schema)]) if added_snaps else out["nfl_snap_counts"]
    out["nfl_rosters_weekly"] = pl.DataFrame(roster)
    out["nfl_injuries"] = pl.DataFrame(injuries).with_columns(pl.col("date_modified").dt.convert_time_zone("UTC"))
    players = frames["nfl_players"].to_dicts()
    players += [{"gsis_id": gsis(t, "QB2"), "display_name": f"{t} QB2", "position": "QB", "latest_team": t,
                 "pfr_id": f"pfr-{gsis(t, 'QB2')}"} for t in TEAMS]
    out["nfl_players"] = pl.DataFrame(players)
    return out


def participation_database(path, seasons: list[int], seed: int = 7, **kw):
    con = connect(path)
    init_db(con)
    write(con, add_participation(generate(seasons, seed=seed), **kw))
    return con
