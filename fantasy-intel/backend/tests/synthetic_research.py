"""Synthetic WR / RB / TE usage for the Research Lab browser test (scripts/build_e2e_db.py). Every number
is an invented, deterministic test input: no real player, team-season or nflverse row is reproduced."""

from app.analytics.player_scores import build_player_scores
from app.db.connection import replace_where

TEAMS = ["KC", "BUF", "DAL", "SF", "DET", "MIA", "PHI", "CIN"]
WEEKS = 7
BYE = {"KC": 4, "DAL": 5, "SF": 6}
POOL = {"WR": 16, "RB": 12, "TE": 10}
# Synthetic RB 1 maps to the fixture league's ESPN player 201 so the profile link and photo path are exercised.
ESPN_LINKS = {"00-SYNRB01": 201}


def gsis(pos: str, i: int) -> str:
    return f"00-SYN{pos}{i:02d}"


def _row(pos: str, i: int, week: int) -> dict | None:
    team = TEAMS[i % len(TEAMS)]
    if BYE.get(team) == week or (i % 7 == 3 and week == 2):      # byes and one missed game per few players
        return None
    k = (i * 7 + week * 3) % 11 - 5                               # deterministic wobble in -5..5
    grow = week * 0.6 if i % 5 == 1 else 0.0                      # a few rising roles
    if pos == "RB":
        carries = max(0, 18 - i + k // 2 + int(grow))
        targets = max(0, 2 + (i % 4) + k // 3)
        xfp = round(0.55 * carries + 1.4 * targets + 0.5, 2)
        ppr = round(max(0.0, xfp + k * 0.9 - (2 if i % 3 == 0 else 0)), 2)
        return {"team": team, "position": "RB", "offense_pct": round(min(0.9, 0.35 + carries / 40), 3),
                "offense_snaps": 20 + carries, "targets": targets, "receptions": max(0, targets - 1), "air_yards": 0.0,
                "target_share": round(targets / 34, 4), "air_yards_share": 0.0, "carries": carries,
                "designed_rushes": carries, "rush_attempts": carries, "team_rush_attempts": 30,
                "team_rb_carries": 24, "backfield_rush_share": round(min(1.0, carries / 24), 4),
                "team_rush_share": round(carries / 30, 4), "rz_targets": targets // 3, "rz_carries": carries // 6,
                "gl_carries": carries // 12, "opportunities": carries + targets, "fantasy_points_ppr": ppr,
                "xfp": None if (week == WEEKS and i % 6 == 0) else xfp}
    base = 10 - i * 0.5 if pos == "WR" else 7 - i * 0.5
    targets = max(0, int(base + k / 2 + grow))
    air = float(targets * (9 + i % 6))
    xfp = round(1.65 * targets + air * 0.02, 2)
    ppr = round(max(0.0, xfp + k * 1.1 + (3 if i % 4 == 2 else 0)), 2)
    return {"team": team, "position": pos, "offense_pct": round(min(0.98, 0.5 + targets / 25), 3),
            "offense_snaps": 40 + targets, "targets": targets, "receptions": max(0, targets - 2), "air_yards": air,
            "target_share": round(targets / 34, 4), "air_yards_share": round(air / 300, 4), "carries": 0,
            "designed_rushes": 0, "rush_attempts": 0, "team_rush_attempts": 30, "team_rb_carries": 24,
            "rz_targets": targets // 4, "rz_carries": 0, "gl_carries": 0, "opportunities": targets,
            "fantasy_points_ppr": ppr, "xfp": xfp}


def write(con, season: int) -> dict:
    rows, players = [], []
    for pos, n in POOL.items():
        for i in range(1, n + 1):
            g = gsis(pos, i)
            players.append({"gsis_id": g, "display_name": f"Synthetic {pos} {i}", "position": pos,
                            "latest_team": TEAMS[i % len(TEAMS)]})
            for w in range(1, WEEKS + 1):
                r = _row(pos, i, w)
                if r:
                    rows.append({"season": season, "week": w, "gsis_id": g, "opponent": "OPP", **r})
    replace_where(con, "player_week_usage", rows, "season = ?", [season])
    con.execute("DELETE FROM nfl_players WHERE gsis_id LIKE '00-SYN%'")
    replace_where(con, "nfl_players", players, "gsis_id LIKE '00-SYN%'")
    for g, espn in ESPN_LINKS.items():
        con.execute("DELETE FROM player_id_map WHERE espn_player_id = ?", [espn])
        con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_method, match_confidence) "
                    "VALUES (?, ?, 'espn_id:nfl_players', 'high')", [espn, g])
    build_player_scores(con, season)
    return {"usage_rows": len(rows), "players": len(players)}
