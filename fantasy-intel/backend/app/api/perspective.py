"""League-wide team perspective: which fantasy team a read-only view is computed for.

Anyone viewing the shared league can analyze any of its teams (`?team_id=`). Without one, views keep the owner's
team (fantasy_teams.is_mine, from ESPN_MY_TEAM_ID / SWID) as before. The selection never writes anything: is_mine is
the sync's record of whose ESPN account this is, not the viewer's choice.
"""

from __future__ import annotations

from fastapi import HTTPException

from ..db.connection import table_exists
from .deps import one, rows


def league_teams(con, league_id: int, season: int) -> list[dict]:
    """Every team of the league with its roster-snapshot freshness for `week` (the league's current week)."""
    return rows(con, """
        SELECT t.team_id, t.name, t.abbrev, coalesce(t.is_mine, false) AS is_owner_default, r.roster_synced_at,
               coalesce(r.players, 0) AS roster_players
        FROM fantasy_teams t
        JOIN leagues lg ON lg.league_id = t.league_id AND lg.season = t.season
        LEFT JOIN (SELECT league_id, season, team_id, max(synced_at) AS roster_synced_at, count(*) AS players
                   FROM lineup_snapshots l
                   WHERE l.scoring_period = (SELECT current_week FROM leagues x
                                             WHERE x.league_id = l.league_id AND x.season = l.season)
                   GROUP BY 1, 2, 3) r
          ON r.league_id = t.league_id AND r.season = t.season AND r.team_id = t.team_id
        WHERE t.league_id = ? AND t.season = ?
        ORDER BY t.team_id""", [league_id, season])


def resolve_team(con, league: dict, team_id: int | None) -> dict | None:
    """The team a view is for, as a `perspective` block, or None when no team was asked for and the owner's team is
    unknown. A requested team must belong to this league and season (404 otherwise): a stale or deleted id from an
    old shared link never silently falls back to another team."""
    teams = league_teams(con, league["league_id"], league["season"])
    if team_id is None:
        team = next((t for t in teams if t["is_owner_default"]), None)
        source = "owner_default"
    else:
        team = next((t for t in teams if t["team_id"] == team_id), None)
        source = "query"
        if team is None:
            raise HTTPException(404, f"Team {team_id} is not in {league.get('name') or 'this league'} "
                                     f"({league['season']}). Pick a team from the list.")
    if team is None:
        return None
    return {**team, "source": source, "league_id": league["league_id"], "season": league["season"],
            "league_synced_at": league.get("synced_at")}


def waiver_table(con, league: dict, perspective: dict | None) -> tuple[str, list]:
    """(table SQL with alias w, extra params) holding this perspective's waiver rows.

    team_waiver_recommendations has every team; a database built before it existed (or a hosted copy not yet
    republished) only has the owner's rows in waiver_recommendations, so the owner default still works there and
    any other team gets a clear 503 instead of the owner's advice under another name."""
    if perspective is None or perspective["source"] == "owner_default":
        return "waiver_recommendations w", []
    has_team_rows = table_exists(con, "team_waiver_recommendations") and one(
        con, "SELECT 1 AS x FROM team_waiver_recommendations WHERE league_id=? AND season=? LIMIT 1",
        [league["league_id"], league["season"]])
    if has_team_rows:
        return "team_waiver_recommendations w", [perspective["team_id"]]
    if perspective["is_owner_default"]:
        return "waiver_recommendations w", []
    raise HTTPException(503, "Waiver suggestions for other teams have not been computed yet. Run the analytics "
                             "refresh (scripts/compute_analytics.py) to evaluate waivers for every team.")
