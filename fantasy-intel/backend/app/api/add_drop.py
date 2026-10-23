"""Read-only add/drop scenario simulator API (issue #178): GET /api/waivers/add-drop.

Analysis only. There is no write endpoint and nothing is sent to ESPN or Sleeper: the request names at most one
player to add and one to drop, and the answer is computed from the league's synced files (app/analytics/add_drop.py,
on the trade analyzer's league state and lineup simulation). League-scoped through mount_legacy_routers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query

from ..analytics.add_drop import AddDropContext, AddDropError, evaluate_add_drop
from ..analytics.league_rules import build_league_rules
from ..analytics.legacy_league import latest_league
from ..analytics.projections import load_schedule
from ..analytics.start_sit import detect_player_locks
from ..analytics.trade_data import load_league_state
from ..analytics.trades import with_streaming
from ..analytics.waiver_data import waiver_rules
from ..db.connection import fetch_dicts
from .deps import db
from .perspective import resolve_team
from .signal_provenance import Provenance, coverage, payload

router = APIRouter(prefix="/api", tags=["waivers"])

# ESPN player ids are positive; team defenses are negative (-16002). Anything outside this is not a player id.
ID_BOUND = 10**12
_PROVIDERS = {"espn": "ESPN", "sleeper": "Sleeper"}


@router.get("/waivers/add-drop")
def get_add_drop(
    team_id: int | None = Query(None, ge=0, le=999_999, description="Fantasy team to simulate for (defaults to the owner's team)"),
    add: int | None = Query(None, ge=-ID_BOUND, le=ID_BOUND, description="Player to add: a free agent or a player on waivers"),
    drop: int | None = Query(None, ge=-ID_BOUND, le=ID_BOUND, description="Player to drop from the team's roster"),
):
    """One add/drop pair (add-only and drop-only too) valued with the trade analyzer's exact weekly lineup
    simulation: current-week and rest-of-season lineup deltas, positional depth, byes, roster capacity and the
    acquisition rules. Read-only: nothing is stored, claimed or sent anywhere."""
    if add is not None and add == drop:
        raise HTTPException(422, "The same player cannot be both added and dropped.")

    def known(directory: dict) -> None:
        for role, pid in (("add", add), ("drop", drop)):
            if pid is not None and pid not in directory:
                raise HTTPException(422, f"The {role} player ({pid}) is not in this league's player data.")

    with db() as con:
        sim = load_simulation(con, team_id, [x for x in (add, drop) if x is not None], known)
        try:
            out = evaluate_add_drop(sim.state, sim.team_id, add, drop, sim.ctx)
        except AddDropError as err:
            raise HTTPException(422, str(err))
        except LookupError as err:
            raise HTTPException(404, str(err))
        return sim.finish(con, out)


@dataclass
class Simulation:
    """What one add/drop or claim-plan request evaluates against: the league row, the team, the trade analyzer's
    league state (with K and D/ST streaming) and the simulator's context for the requested players."""
    lg: dict
    perspective: dict | None
    team_id: int | None
    state: Any
    ctx: AddDropContext

    def finish(self, con, out: dict) -> dict:
        out["league"] = {k: self.lg.get(k) for k in ("league_id", "season", "name", "current_week", "final_scoring_period")}
        out["perspective"] = self.perspective
        out["provenance"] = _provenance(con, self.lg, self.state, self.team_id)
        return out


def load_simulation(con, team_id: int | None, ids: list[int], check: Callable[[dict], None] | None = None) -> Simulation:
    """Load everything the add/drop simulator reads for `team_id` (None: the owner's team) and the requested player
    ids. `check` sees the requested players' directory before the league state is loaded (to refuse unknown ids)."""
    lg = latest_league(con)
    if not lg:
        raise HTTPException(503, "League not synced yet. Run scripts/sync_espn.py.")
    perspective = resolve_team(con, lg, team_id)          # 404 for a team outside this league
    ids = sorted(set(ids))
    directory = {r["espn_player_id"]: r for r in fetch_dicts(
        con, f"""SELECT espn_player_id, full_name, position, pro_team, injury_status, availability_status
                 FROM fantasy_players WHERE espn_player_id IN ({", ".join("?" for _ in ids)})""", ids)} if ids else {}
    if check is not None:
        check(directory)
    now = datetime.now(timezone.utc)
    state = load_league_state(con, now=now)
    if state is None:
        raise HTTPException(503, "League current/final week unknown. Run scripts/sync_espn.py.")
    state = with_streaming(state)            # as the trade analyzer: K and D/ST valued against weekly streaming
    rules = build_league_rules(lg)
    provider = _PROVIDERS.get(rules["source"]["provider"], "ESPN")
    window = state.window()
    games, covered = load_schedule(con, state.season, list(window.weeks))
    tid = perspective["team_id"] if perspective else None
    team_row = (fetch_dicts(con, "SELECT * FROM fantasy_teams WHERE league_id = ? AND season = ? AND team_id = ?",
                            [lg["league_id"], lg["season"], tid]) or [{}])[0] if tid is not None else {}
    wanted = set(ids)
    teams = sorted({p.pro_team for p in ([q for ps in state.rosters.values() for q in ps] + state.free_agents)
                    if p.espn_player_id in wanted}, key=str)
    locks = detect_player_locks(con, state.season, int(lg["current_week"]), teams, now) if teams else {}
    ctx = AddDropContext(provider=provider, team_row=team_row, rules=waiver_rules(lg), waivers=rules["waivers"],
                         directory=directory, locks=locks, lock_week=int(lg["current_week"]), games=games,
                         covered=covered, evaluated_at=now.isoformat())
    return Simulation(lg, perspective, tid, state, ctx)


def _provenance(con, lg: dict, state, team_id: int | None) -> dict:
    """League data, the rest-of-season projections the simulation reads (coverage: this team's active players with a
    projection in some evaluated week) and NFL injury reports, as the trade analyzer reports them."""
    prov = Provenance(con)
    weeks = list(state.window().weeks)
    active = [p for p in state.rosters.get(team_id, []) if not p.reserved] if team_id is not None else []
    cov = coverage(sum(1 for p in active if any((state.projections.get(p.espn_player_id) or {}).get(w) is not None
                                                for w in weeks)), len(active), "active players with a projection") \
        if weeks and active else None
    detail = {"available": bool(state.projections), "run": None,
              "reason": None if state.projections else "No projections are stored for the remaining weeks."} \
        if state.projection_source == "independent" else None
    proj = prov.projections(state.projection_source, detail, cov=cov, horizon="rest-of-season")
    return payload(prov.league(lg), proj, prov.injuries(con, state.season))
