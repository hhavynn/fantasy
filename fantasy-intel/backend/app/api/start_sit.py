"""Read-only Weekly Start/Sit Command Center API router."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics.lineup_what_if import MAX_PARAM_LEN, ScenarioSyntaxError, evaluate_what_if, parse_scenario
from ..analytics.start_sit import load_start_sit_evaluation
from .deps import db
from .signal_provenance import Provenance, coverage, payload

router = APIRouter(prefix="/api", tags=["start-sit"])


@router.get("/start-sit")
def get_start_sit(
    team_id: int | None = Query(None, description="Fantasy team ID (defaults to manager's team)"),
    week: int | None = Query(None, description="Fantasy scoring period / NFL week (defaults to current week)"),
    source: str | None = Query(None, description="Projection source: espn | independent (default: the league's own, espn when it has ESPN projections, else independent)"),
):
    """Compare manager's actual ESPN lineup against optimal lock-constrained lineup."""
    with db() as con:
        try:
            out = load_start_sit_evaluation(con, team_id=team_id, week=week, source=source)
        except LookupError as err:
            raise HTTPException(404, str(err))
        except ValueError as err:
            msg = str(err)
            if "outside valid fantasy season" in msg or msg.startswith("source must be"):
                raise HTTPException(422, msg)
            raise HTTPException(503, msg)
        out["provenance"] = start_sit_provenance(con, out, source)
        return out


@router.get("/start-sit/what-if")
def get_start_sit_what_if(
    team_id: int | None = Query(None, description="Fantasy team ID (defaults to manager's team)"),
    week: int | None = Query(None, description="Fantasy scoring period / NFL week (defaults to current week)"),
    source: str | None = Query(None, description="Projection source: espn | independent (as GET /api/start-sit)"),
    whatif: str | None = Query(None, max_length=MAX_PARAM_LEN,
                               description="Scenario: w<week>t<team>, then ~ and up to 20 moves SLOT_ID.PLAYER_ID"),
):
    """A what-if lineup against the Start/Sit recommendation: the moves are checked with the engine's own eligibility,
    kickoff locks and IR/taxi rules, and the totals are recomputed. Read-only: nothing is stored or sent anywhere."""
    try:
        scenario = parse_scenario(whatif)
    except ScenarioSyntaxError as err:
        raise HTTPException(422, str(err))
    with db() as con:
        try:
            out = evaluate_what_if(con, team_id=team_id, week=week, source=source, scenario=scenario)
        except LookupError as err:
            raise HTTPException(404, str(err))
        except ValueError as err:
            msg = str(err)
            if "outside valid fantasy season" in msg or msg.startswith("source must be"):
                raise HTTPException(422, msg)
            raise HTTPException(503, msg)
        scen = out["totals"]["scenario"]
        cov = coverage(scen["starters_with_projection"], scen["starters"], "starters with a projection") \
            if scen["starters"] > 0 else None
        out["provenance"] = start_sit_provenance(con, out, source, cov=cov)
        return out


def start_sit_provenance(con, d: dict, source: str | None, *, cov: dict | None = None) -> dict:
    """The lineup (provider sync), the week's projections (coverage: the lineup's starters with one, as the summary
    counts them, or `cov` for another lineup such as a what-if) and NFL injury and practice reports."""
    prov = Provenance(con)
    s, fresh = d.get("summary") or {}, d.get("freshness") or {}
    if cov is None:
        starters = s.get("starters_count")
        cov = coverage(starters - len(s.get("missing_projections") or []), starters, "starters with a projection") \
            if isinstance(starters, int) and starters > 0 else None
    league = prov.league({"synced_at": fresh.get("lineup_synced_at")})
    detail = d.get("projection_source_detail")
    proj = prov.projections(source, detail, captured_at=fresh.get("projection_synced_at"), cov=cov)
    return payload(league, proj, prov.injuries(con, (d.get("league") or {}).get("season")))
