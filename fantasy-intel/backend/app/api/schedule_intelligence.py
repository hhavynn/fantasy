"""Read-only schedule intelligence endpoints: no writes, no syncs, no provider calls.

Every route is on this one router, which app/main.py also mounts under /v2/leagues/{league_id} with `league_scope`
(authorization, the league's own files, 404 for an inaccessible league)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics.bye_planner import build_bye_planner
from ..analytics.kdst_streaming import (
    DEFAULT_WEEKS,
    MAX_MOVE_COST,
    MAX_MOVES_LIMIT,
    MAX_WEEKS,
    MIN_WEEKS,
    BadParam,
    build_kdst_streaming,
)
from ..analytics.schedule_intelligence import (
    NotFound,
    NotSynced,
    build_schedule_intelligence,
)
from ..analytics.sos_heatmap import BadParam as SosBadParam
from ..analytics.sos_heatmap import build_sos_heatmap
from ..db.connection import table_exists
from .deps import db
from .signal_provenance import Provenance, coverage, payload

router = APIRouter(prefix="/api", tags=["schedule intelligence"])


@router.get("/schedule-intelligence")
def schedule_intelligence(team_id: int | None = None,
                          position: str = Query("all", description="all | QB | RB | WR | TE"),
                          phase: str = Query("all", description="all | regular | playoffs"),
                          player_id: int | None = None):
    """Each remaining week's opponent and what that defense allowed in games completed before the week."""
    with db() as con:
        try:
            return build_schedule_intelligence(con, team_id=team_id, position=position.upper()
                                               if position.lower() != "all" else "all",
                                               phase=phase.lower(), player_id=player_id)
        except NotSynced as err:
            raise HTTPException(503, str(err))
        except NotFound as err:
            raise HTTPException(404, str(err))
        except ValueError as err:
            raise HTTPException(422, str(err))


@router.get("/schedule-intelligence/sos")
def sos_heatmap(position: str = Query("QB", description="QB | RB | WR | TE: one grid per position"),
                horizon: str = Query("ros", description="ros (rest of the regular season) | playoffs"),
                view: str = Query("teams", description="teams (NFL offenses) | roster | free_agents"),
                team_id: int | None = Query(None, description="Fantasy team for the roster view (defaults to yours)"),
                nfl_team: str | None = Query(None, max_length=3, description="Only this NFL team (e.g. KC)"),
                player_id: int | None = Query(None, description="One player, graded on his own position")):
    """Position-specific strength of schedule (contract sos-heatmap.v1): each remaining week's opponent defense graded
    on the position's primary metric against a neutral baseline, with its sample and the reason when it is not graded."""
    with db() as con:
        try:
            out = build_sos_heatmap(con, position=position, horizon=horizon.lower(), view=view.lower(),
                                    team_id=team_id, nfl_team=nfl_team, player_id=player_id)
        except NotSynced as err:
            raise HTTPException(503, str(err))
        except NotFound as err:
            raise HTTPException(404, str(err))
        except SosBadParam as err:       # only the parameter checks' own messages
            raise HTTPException(422, str(err))
        out["provenance"] = sos_heatmap_provenance(con, out)
        return out


def sos_heatmap_provenance(con, d: dict) -> dict:
    """The league (provider sync), the NFL schedule (coverage: horizon weeks with a loaded schedule) and the defensive
    evidence (coverage: scheduled matchups in view whose opponent could be graded)."""
    prov = Provenance(con)
    weeks = d["horizon"]["weeks"]
    missing = d["source"]["weeks_without_schedule"]
    schedule = prov.nflverse(
        "schedule", "NFL schedule and byes (nflverse)",
        cov=coverage(len(weeks) - len(missing), len(weeks), "horizon weeks with a loaded NFL schedule") if weeks else None,
        reason=None if d["schedule"]["available"] and not missing else
        ("No NFL schedule is stored." if not d["schedule"]["available"]
         else f"No NFL schedule is loaded for week {', '.join(map(str, missing))}."))
    cov = d["coverage"]
    defense = prov.nflverse(
        "defense", "Defensive play-by-play aggregates (nflverse)",
        cov=coverage(cov["graded"], cov["scheduled"], "scheduled matchups in view with a graded opponent")
        if cov["scheduled"] else None,
        reason=d["source"]["reason"] if not d["source"]["available"] else
        (f"Some opponents are not graded: fewer than {d['min_games']} presumed-complete games, or fewer than "
         f"{d['min_ranked_defenses']} defenses that can be ranked that week."
         if cov["graded"] < cov["scheduled"] else None))
    return payload(prov.league(), schedule, defense)


@router.get("/schedule-intelligence/bye-planner")
def bye_planner(team_id: int | None = Query(None, description="Fantasy team to plan for (defaults to yours)")):
    """For each remaining week through the final scoring period: whether the roster can fill every starting slot,
    byes, verified NFL absences and what is unknown (contract bye-planner.v1)."""
    with db() as con:
        try:
            out = build_bye_planner(con, team_id=team_id)
        except NotSynced as err:
            raise HTTPException(503, str(err))
        except NotFound as err:
            raise HTTPException(404, str(err))
        out["provenance"] = bye_planner_provenance(con, out)
        return out


@router.get("/schedule-intelligence/kdst-streaming")
def kdst_streaming(team_id: int | None = Query(None, description="Fantasy team to plan for (defaults to yours)"),
                   weeks: int = Query(DEFAULT_WEEKS, ge=MIN_WEEKS, le=MAX_WEEKS, description="Weeks to plan"),
                   max_moves: int | None = Query(None, ge=0, le=MAX_MOVES_LIMIT,
                                                 description="Most add/drops per position (default: no limit)"),
                   move_cost: float = Query(0.0, ge=0, le=MAX_MOVE_COST,
                                            description="Points charged per add/drop")):
    """A one-slot K and D/ST streaming plan for the next 2-4 actionable weeks from the league's free agents: this week's
    best streamer, the week-by-week plan, alternatives, gaps and the data horizon (contract kdst-streaming.v1)."""
    with db() as con:
        try:
            out = build_kdst_streaming(con, team_id=team_id, weeks=weeks, max_moves=max_moves, move_cost=move_cost)
        except NotSynced as err:
            raise HTTPException(503, str(err))
        except NotFound as err:
            raise HTTPException(404, str(err))
        except BadParam as err:          # only the parameter checks' own messages, never an internal error's
            raise HTTPException(422, str(err))
        out["provenance"] = kdst_streaming_provenance(con, out)
        return out


def kdst_streaming_provenance(con, d: dict) -> dict:
    """The rosters and free-agent status (provider sync), the weekly projections the plan reads (coverage: candidate
    weeks with a projection over the weeks considered) and the NFL schedule behind byes and opponents."""
    prov = Provenance(con)
    pos = [p for p in d["positions"] if p["status"] != "not_started"]
    with_game = sum(w["candidates_with_game"] for p in pos for w in p["weeks"])
    projected = sum(w["projected_candidates"] for p in pos for w in p["weeks"])
    cov = coverage(projected, with_game, "free-agent weeks not on a bye that have a projection") if with_game else None
    source = "independent" if d["projection_source"] == "Ours" else "espn"
    detail = {"available": projected > 0, "run": None,
              "reason": None if projected else "No projections are stored for the planned weeks."} \
        if source == "independent" else None
    weeks = d["window"]["weeks"]
    missing = d["schedule"]["weeks_missing"]
    schedule = prov.nflverse(
        "schedule", "NFL schedule and byes (nflverse)",
        cov=coverage(len(weeks) - len(missing), len(weeks), "planned weeks with a loaded NFL schedule") if weeks else None,
        reason=None if d["schedule"]["available"] and not missing else
        ("No NFL schedule is stored." if not d["schedule"]["available"]
         else f"No NFL schedule is loaded for week {', '.join(map(str, missing))}."))
    return payload(prov.league(), prov.projections(source, detail, cov=cov), schedule)


def bye_planner_provenance(con, d: dict) -> dict:
    """The roster (provider sync), the NFL schedule (coverage: planned weeks with a loaded schedule) and the NFL
    weekly rosters behind the reserve-list availability."""
    prov = Provenance(con)
    weeks = d["window"]["weeks"]
    loaded = len(d["schedule"]["weeks_loaded"])
    missing = d["schedule"]["weeks_missing"]
    schedule = prov.nflverse(
        "schedule", "NFL schedule and byes (nflverse)",
        cov=coverage(loaded, len(weeks), "planned weeks with a loaded NFL schedule") if weeks else None,
        reason=None if d["schedule"]["available"] and not missing else
        ("No NFL schedule is stored." if not d["schedule"]["available"]
         else f"No NFL schedule is loaded for week {', '.join(map(str, missing))}."))
    rosters = prov.nflverse(
        "injuries", "NFL reserve lists (nflverse weekly rosters)",
        reason=None if table_exists(con, "nfl_rosters_weekly") else
        "No NFL weekly rosters are stored, so no reserve-list absence can be verified.")
    return payload(prov.league(), schedule, rosters)
