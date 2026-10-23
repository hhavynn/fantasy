"""A synthetic game-splits world (issue #210): one invented team, SPL, in an invented 2025 season. Every id, name,
team code and number is a made-up test input; no real player, game or nflverse row is reproduced.

  * 00-SPLWR01  the subject WR, on SPL every week; SPL has a bye in week 7.
  * 00-SPLWR02  a teammate WR: plays weeks 1-4 and 12, game-day inactive in week 5, on the reserve list in weeks 6, 8
                and 9, a partial game in week 10 (10% of the snaps against his usual 85%), and active with no box-score
                or snap record in week 11 (unknown: excluded, never "without").
  * 00-SPLTE01  a TE traded from OTH to SPL after week 3 (weeks 1-3 are "without": on another team).
  * 00-SPLQB01 / 00-SPLQB02  SPL's quarterbacks: QB1 starts weeks 1-6, QB2 weeks 8-12 (a quarterback change).

The scripts/build_e2e_db.py fixture writes it next to the 2026 league so the browser test has a full split; the unit
tests (tests/test_game_splits.py) build it alone and then change single rows for each edge case.
"""

from app.db.connection import replace_where

SEASON = 2025
WEEKS = 12
BYE = 7
TEAM, OTHER = "SPL", "OTH"
OPPONENTS = ["RIV", "FOE", "VSX", "OTH"]
SUBJECT, WR2, TE, QB1, QB2 = "00-SPLWR01", "00-SPLWR02", "00-SPLTE01", "00-SPLQB01", "00-SPLQB02"
NAMES = {SUBJECT: ("Split Receiver", "WR"), WR2: ("Split Wingman", "WR"), TE: ("Split Traded End", "TE"),
         QB1: ("Split Starter", "QB"), QB2: ("Split Backup", "QB")}
WR2_PLAYS = {1, 2, 3, 4, 10, 12}
WR2_STATUS = {5: "INA", 6: "RES", 8: "RES", 9: "RES", 11: "ACT"}


def _usage(week: int, gsis: str, team: str, position: str, *, pct: float | None, targets: int, carries: int = 0,
           xfp: float | None = None, ppr: float | None = None, routes: int | None = None) -> dict:
    snaps = None if pct is None else int(round(pct * 64))
    return {"season": SEASON, "week": week, "gsis_id": gsis, "team": team, "opponent": opponent(team, week),
            "position": position, "offense_snaps": snaps, "offense_pct": pct, "team_offense_snaps": 64,
            "targets": targets, "receptions": max(0, targets - 2), "air_yards": float(targets * 10), "carries": carries,
            "target_share": round(targets / 34, 4), "air_yards_share": round(targets * 10 / 300, 4),
            "team_rush_attempts": 26, "team_rb_carries": 20, "rz_targets": targets // 4, "rz_carries": 0,
            "gl_carries": 0, "opportunities": targets + carries, "routes": routes,
            "xfp": xfp, "fantasy_points_ppr": ppr,
            "stats_source": "stats+snaps" if pct is not None else "stats_only"}


def opponent(team: str, week: int) -> str:
    if team == TEAM:
        return OPPONENTS[week % len(OPPONENTS)] if OPPONENTS[week % len(OPPONENTS)] != OTHER else "FOE"
    return "XOT"


def usage_rows() -> list[dict]:
    rows = []
    for w in range(1, WEEKS + 1):
        if w == BYE:
            continue
        alone = w not in WR2_PLAYS or w in (10, 11)
        k = (w * 5) % 7 - 3                                        # deterministic wobble in -3..3
        targets = (10 if alone else 7) + k // 2
        xfp = round(1.6 * targets + 2.0, 2)
        ppr = round(xfp + k * 0.8, 2)
        rows.append(_usage(w, SUBJECT, TEAM, "WR", pct=0.9, targets=targets, xfp=xfp, ppr=ppr, routes=30 + targets))
        if w in WR2_PLAYS:
            pct = 0.1 if w == 10 else 0.85
            rows.append(_usage(w, WR2, TEAM, "WR", pct=pct, targets=1 if w == 10 else 6, xfp=8.0, ppr=9.0))
        rows.append(_usage(w, TE, TEAM if w > 3 else OTHER, "TE", pct=0.7, targets=4, xfp=6.0, ppr=6.5))
        qb = QB1 if w < BYE else QB2
        rows.append(_usage(w, qb, TEAM, "QB", pct=1.0, targets=0, carries=3, xfp=None, ppr=18.0))
    return rows


def roster_rows() -> list[dict]:
    rows = []
    for w in range(1, WEEKS + 1):
        for g in (SUBJECT, QB1, QB2):
            status = "ACT" if (g != QB2 or w >= BYE) else "INA"
            rows.append({"season": SEASON, "week": w, "game_type": "REG", "team": TEAM, "gsis_id": g,
                         "position": NAMES[g][1], "status": status, "full_name": NAMES[g][0]})
        rows.append({"season": SEASON, "week": w, "game_type": "REG", "team": TEAM if w > 3 else OTHER, "gsis_id": TE,
                     "position": "TE", "status": "ACT", "full_name": NAMES[TE][0]})
        status = WR2_STATUS.get(w, "ACT")
        rows.append({"season": SEASON, "week": w, "game_type": "REG", "team": TEAM, "gsis_id": WR2,
                     "position": "WR", "status": status, "full_name": NAMES[WR2][0]})
    return rows


def schedule_rows() -> list[dict]:
    rows = []
    for w in range(1, WEEKS + 1):
        day = f"2025-{9 + (w - 1) // 4:02d}-{1 + ((w - 1) % 4) * 7:02d}"
        if w != BYE:
            rows.append({"season": SEASON, "week": w, "game_type": "REG", "game_id": f"{SEASON}_{w:02d}_{opponent(TEAM, w)}_{TEAM}",
                         "gameday": day, "gametime": "13:00", "home_team": TEAM, "away_team": opponent(TEAM, w),
                         "home_score": 20 + w, "away_score": 17})
        rows.append({"season": SEASON, "week": w, "game_type": "REG", "game_id": f"{SEASON}_{w:02d}_XOT_{OTHER}",
                     "gameday": day, "gametime": "13:00", "home_team": OTHER, "away_team": "XOT",
                     "home_score": 14, "away_score": 21})
    return rows


ROSTER_DDL = """CREATE TABLE IF NOT EXISTS nfl_rosters_weekly (season INT, week INT, game_type VARCHAR, team VARCHAR,
    gsis_id VARCHAR, position VARCHAR, status VARCHAR, full_name VARCHAR)"""
SCHEDULE_DDL = """CREATE TABLE IF NOT EXISTS nfl_schedules (season INT, week INT, game_type VARCHAR, game_id VARCHAR,
    gameday VARCHAR, gametime VARCHAR, home_team VARCHAR, away_team VARCHAR, home_score INT, away_score INT)"""


def _insert(con, table: str, rows: list[dict]) -> None:
    for r in rows:
        con.execute(f"INSERT INTO {table} ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})", list(r.values()))


def write(con) -> dict:
    """Write the world into a database that already has the app schema (player_week_usage, nfl_players)."""
    usage = usage_rows()
    replace_where(con, "player_week_usage", usage, "season = ? AND gsis_id LIKE '00-SPL%'", [SEASON])
    con.execute("DELETE FROM nfl_players WHERE gsis_id LIKE '00-SPL%'")
    replace_where(con, "nfl_players", [{"gsis_id": g, "display_name": n, "position": p, "latest_team": TEAM}
                                       for g, (n, p) in sorted(NAMES.items())], "gsis_id LIKE '00-SPL%'")
    con.execute(ROSTER_DDL)
    con.execute("DELETE FROM nfl_rosters_weekly WHERE gsis_id LIKE '00-SPL%'")
    _insert(con, "nfl_rosters_weekly", roster_rows())
    con.execute(SCHEDULE_DDL)
    con.execute("DELETE FROM nfl_schedules WHERE season = ? AND (home_team IN ('SPL', 'OTH') OR away_team IN ('SPL', 'OTH'))",
                [SEASON])
    _insert(con, "nfl_schedules", schedule_rows())
    return {"usage_rows": len(usage)}
