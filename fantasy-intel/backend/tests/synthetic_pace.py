"""Synthetic team-week play-by-play aggregates, drives and schedules for the pace lab (tests/test_pace_lab.py and the
browser test database, scripts/build_e2e_db.py). Every number is an invented, deterministic test input: no real
team-season or nflverse row is reproduced.

Built in: three game scripts (one-score, won by 9+, lost by 9+), one overtime game per season, one final game with no
team-week row (missing play-by-play), one game with no neutral-situation play, and a relocated franchise: in the first
season LV is listed as OAK in every source."""

from __future__ import annotations

import pyarrow as pa
from synthetic_nfl import TEAMS, rounds

from app.db.connection import table_columns, table_exists

WEEKS = 17
OT_GAME = (5, 0)            # (week, pair index): the overtime game of each season
MISSING = (7, "KC")         # (week, team): final game, no team-week or drive row
NO_NEUTRAL = (9, "BUF")     # (week, team): a game with no neutral-situation play


def _listed(team: str, season: int, first: int) -> str:
    return "OAK" if team == "LV" and season == first else team


def _speed(team: str) -> int:
    return 56 + (TEAMS.index(team) * 7) % 15          # base plays per game 56..70


def _score(season: int, week: int, i: int) -> tuple[int, int]:
    k = (season * 3 + week * 5 + i * 7) % 6
    return [(24, 21), (31, 10), (13, 27), (20, 20), (35, 14), (17, 26)][k]


def games(seasons: tuple[int, ...]) -> list[dict]:
    """One row per (season, week, game) with both teams' stats."""
    out = []
    for season in seasons:
        for week, pairs in enumerate(rounds(TEAMS, WEEKS), start=1):
            for i, (home, away) in enumerate(pairs):
                hs, as_ = _score(season, week, i)
                ot = (week, i) == OT_GAME
                if ot:
                    hs, as_ = 23, 20
                out.append({"season": season, "week": week, "home": home, "away": away, "hs": hs, "as": as_,
                            "ot": ot, "game_id": f"{season}_{week:02d}_{away}_{home}"})
    return out


def team_row(season: int, week: int, team: str, opp: str, pf: int, pa: int, ot: bool) -> dict:
    margin = pf - pa
    shift = 4 if margin < -8 else -3 if margin > 8 else 0
    wobble = (season + week * 3 + TEAMS.index(team)) % 5 - 2
    plays = _speed(team) + shift + wobble + (12 if ot else 0)
    rate = 0.52 + (TEAMS.index(team) % 8) * 0.02 + (0.08 if margin < -8 else -0.07 if margin > 8 else 0.0)
    dropbacks = round(plays * rate)
    neutral = 0 if (week, team) == NO_NEUTRAL else 24 + (week + TEAMS.index(team)) % 10
    nrate = round(0.50 + (TEAMS.index(team) % 6) * 0.03, 3)
    return {"season": season, "week": week, "team": team, "opponent": opp, "offense_plays": plays,
            "dropbacks": dropbacks, "team_designed_rushes": plays - dropbacks, "team_rush_attempts": plays - dropbacks,
            "team_targets": round(dropbacks * 0.86), "neutral_plays": neutral,
            "neutral_pass_rate": nrate if neutral else None, "proe": round((rate - 0.58) * 100, 1),
            "proe_plays": plays}


def _bulk_insert(con, table: str, rows: list[dict]) -> None:
    cols = list(rows[0])
    con.register("_pace_rows", pa.table({c: [r[c] for r in rows] for c in cols}))
    try:
        con.execute(f"INSERT INTO {table} ({', '.join(cols)}) SELECT {', '.join(cols)} FROM _pace_rows")
    finally:
        con.unregister("_pace_rows")


def write(con, seasons: tuple[int, ...] = (2024, 2025)) -> dict:
    """Writes the seasons' rows; any other season's rows are left alone."""
    first = seasons[0]
    if not table_exists(con, "nfl_schedules"):
        con.execute("""CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR, game_id VARCHAR,
                       gameday VARCHAR, gametime VARCHAR, home_team VARCHAR, away_team VARCHAR,
                       home_score INT, away_score INT, overtime INT)""")
    sched_cols = set(table_columns(con, "nfl_schedules"))
    tw_cols = set(table_columns(con, "nfl_team_week"))
    lo, hi = seasons[0], seasons[-1]
    for t in ("nfl_schedules", "nfl_team_week", "nfl_team_game_drives"):
        con.execute(f"DELETE FROM {t} WHERE season BETWEEN ? AND ?", [lo, hi])
    sched, tw, drv = [], [], []
    for g in games(seasons):
        s, w = g["season"], g["week"]
        row = {"season": s, "week": w, "game_type": "REG", "game_id": g["game_id"],
               "gameday": f"{s}-09-{min(w, 28):02d}", "gametime": "13:00",
               "home_team": _listed(g["home"], s, first), "away_team": _listed(g["away"], s, first),
               "home_score": g["hs"], "away_score": g["as"], "overtime": int(g["ot"])}
        sched.append({k: v for k, v in row.items() if k in sched_cols})
        for team, opp, pf, pa, home in ((g["home"], g["away"], g["hs"], g["as"], True),
                                        (g["away"], g["home"], g["as"], g["hs"], False)):
            if (w, team) == MISSING:
                continue
            r = team_row(s, w, team, opp, pf, pa, g["ot"])
            r["team"], r["opponent"] = _listed(team, s, first), _listed(opp, s, first)
            tw.append({k: v for k, v in r.items() if k in tw_cols})
            drives = 11 + (w + TEAMS.index(team)) % 3 + (2 if g["ot"] else 0)
            drv.append([s, w, g["game_id"], r["team"], r["opponent"], home, drives, r["offense_plays"], float(pf)])
    drive_cols = ["season", "week", "game_id", "team", "opponent", "is_home", "drives", "plays", "points"]
    for table, rows in (("nfl_schedules", sched), ("nfl_team_week", tw),
                        ("nfl_team_game_drives", [dict(zip(drive_cols, r)) for r in drv])):
        _bulk_insert(con, table, rows)
    n_tw = len(tw)
    return {"team_weeks": n_tw, "seasons": list(seasons)}
