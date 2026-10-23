"""Read-only Fantasy Research Lab endpoints: no writes, no syncs, no ESPN calls. Metrics cover the whole
NFL WR / RB / TE pool from nflverse data; ESPN league data only supplies optional profile/trade links.

Game splits (issue #210) read only the global nflverse tables (usage, weekly rosters, schedules); every input is in the
query string, so the league-scoped response cache keys on all of them."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query

from ..analytics.game_splits import SplitsError, SplitsNotFound, build_game_splits, build_split_players, latest_season
from ..analytics.game_splits import validate as validate_splits
from ..analytics.research import ResearchError, build_opportunity, build_player_research
from .deps import db

router = APIRouter(prefix="/api/research", tags=["research"])


@router.get("/opportunity")
def opportunity(
    position: Literal["WR", "RB", "TE"] = Query(..., description="Positional pool"),
    window: Literal["season", "last3", "last5"] = Query("season", description="Player's qualifying games used"),
    season: int | None = Query(None, ge=1999, le=2100, description="Default: the synced season"),
    min_games: int | None = Query(None, ge=1, le=18, description="Minimum qualifying games in the window"),
    min_volume: float | None = Query(None, ge=0, le=40,
                                     description="Minimum targets/G (WR, TE) or carries+targets/G (RB)"),
    team: str | None = Query(None, min_length=2, max_length=3, description="NFL team abbreviation"),
):
    """Expected opportunity (xFP/G) vs production (PPR/G, FPOE/G) for every qualifying player at the
    position, with position-specific workload metrics, momentum, percentiles and data coverage."""
    with db() as con:
        try:
            return build_opportunity(con, season, position, window, min_games, min_volume,
                                     team.upper() if team else None)
        except ResearchError as err:
            raise HTTPException(422, str(err))


@router.get("/game-splits")
def game_splits(
    player: str = Query(..., min_length=4, max_length=20, description="nflverse (GSIS) id of a WR, RB or TE"),
    teammate: str | None = Query(None, min_length=4, max_length=20,
                                 description="nflverse id of a QB, RB, WR or TE teammate; omit to list teammates"),
    season_from: int | None = Query(None, ge=1999, le=2100, description="Default: the newest season with usage"),
    week_from: int | None = Query(None, ge=1, le=18),
    season_to: int | None = Query(None, ge=1999, le=2100),
    week_to: int | None = Query(None, ge=1, le=18),
    as_of: str | None = Query(None, min_length=10, max_length=10,
                              description="YYYY-MM-DD: only games played before this date count"),
):
    """The player's games with the teammate on the field against games without him, over an inclusive window of at
    most three seasons. Unknown teammate status is excluded (by reason), never counted as without; a split with
    fewer than three games in either group withholds its differences. Descriptive only: no forecast or causal
    claim."""
    with db() as con:
        try:
            q = validate_splits(player, teammate, season_from, week_from, season_to, week_to, as_of,
                                latest_season(con))
            return build_game_splits(con, q)
        except SplitsError as err:
            raise HTTPException(422, str(err))
        except SplitsNotFound as err:
            raise HTTPException(404, str(err))


@router.get("/game-splits/players")
def game_split_players(season: int | None = Query(None, ge=1999, le=2100,
                                                  description="Default: the newest season with usage")):
    """WR, RB and TE with a played game in the season, for the game-splits player picker."""
    with db() as con:
        return build_split_players(con, season)


@router.get("/players/{gsis_id}")
def player(gsis_id: str = Path(..., min_length=4, max_length=20),
           season: int | None = Query(None, ge=1999, le=2100)):
    """One player's season / last-3 / last-5 windows, momentum and a weekly timeline in which byes,
    missed games and missing source rows stay distinct from measured zeros."""
    with db() as con:
        try:
            out = build_player_research(con, gsis_id, season)
        except ResearchError as err:
            raise HTTPException(422, str(err))
        if out is None:
            raise HTTPException(404, "No WR, RB or TE usage stored for this player and season")
        return out
