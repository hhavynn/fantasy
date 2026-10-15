"""Read-only weekly matchup API router."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics.matchup import LeagueNotSynced, TeamNotFound, WeekOutOfRange, load_matchup
from .deps import db

router = APIRouter(prefix="/api", tags=["matchup"])


@router.get("/matchup")
def get_matchup(
    week: int | None = Query(None, description="Fantasy week / matchup period (defaults to the current league week)"),
    team_id: int | None = Query(None, description="Fantasy team ID (defaults to the user's team)"),
):
    """One team against its opponent for one week.

    Response:
      league          {league_id, season, name, current_week}
      week, phase     "regular" | "playoff" | null
      matchup_id      null when the team has no matchup row that week
      matchup_period  the ESPN matchup period the week belongs to (null when none); period_weeks lists its
                      scoring weeks when it spans several (actual points then cover the whole period)
      is_bye_week     true only when other teams play that week and this one does not
      weeks_available scoring weeks whose matchup period has a row for the team
      status          "upcoming" | "in_progress" | "final" | "unknown" (rule: analytics/matchup.py)
      me, opponent    TeamSide, opponent is null with no opponent (bye, or pairings not set yet)
      notes           data caveats worth showing next to the numbers

    TeamSide: {team_id, name, abbrev, record {wins, losses, ties} (to date, not as of that week), standing,
      actual_points (null until the matchup has started), projected_points (null unless the projection is
      complete), known_points (optimal lineup from the players that do have a projection), projection_complete,
      lineup [{slot, player_id, name, position, pro_team, points, is_flex, injury_status, on_bye}] (the optimal
      projected lineup), bench [{player_id, name, position, pro_team, points, injury_status, on_bye}],
      actual_lineup / actual_bench (the lineup set in ESPN, from lineup_snapshots; null when no snapshot;
      points are null until the matchup has started)}.

    404 for an unknown team, 422 for a week outside the season, 503 when no league is synced.
    """
    with db() as con:
        try:
            return load_matchup(con, team_id=team_id, week=week)
        except TeamNotFound as err:
            raise HTTPException(404, str(err))
        except WeekOutOfRange as err:
            raise HTTPException(422, str(err))
        except LeagueNotSynced as err:
            raise HTTPException(503, str(err))
