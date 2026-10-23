"""Read-only FAAB bid guidance API (issue #179): GET /api/waivers/faab?claims=ADD:DROP,...&share=25

The capability audit of the league's synced FAAB data, the league's observed winning bids, and a heuristic bid range
for each claim of the manager's device-local plan (app/analytics/faab.py over the claim planner and the add/drop
simulator). Analysis only: there is no write endpoint, nothing is stored, bid or sent to ESPN or Sleeper.
League-scoped through mount_legacy_routers.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics.claim_plan import MAX_CLAIMS, ClaimPlanError, evaluate_claim_plan, parse_claim
from ..analytics.faab import DEFAULT_SHARE, evaluate_faab
from ..analytics.league_rules import build_league_rules
from ..analytics.waiver_roster import object_value
from ..db.connection import fetch_dicts, table_exists
from .add_drop import load_simulation
from .deps import db

router = APIRouter(prefix="/api", tags=["waivers"])


def observed_rows(con, lg: dict) -> list[dict] | None:
    """One row per executed waiver claim this season, with its stored bid and the added player. Failed, cancelled and
    pending claims are private to the manager who made them and are never read. None: no transactions are synced."""
    if not table_exists(con, "transactions") or not table_exists(con, "transaction_items"):
        return None
    rows = fetch_dicts(con, """
        SELECT t.transaction_id, t.scoring_period, t.bid_amount,
               min(ti.espn_player_id) AS espn_player_id, count(ti.espn_player_id) AS adds
        FROM transactions t
        LEFT JOIN transaction_items ti ON ti.transaction_id = t.transaction_id AND ti.item_type = 'ADD'
        WHERE t.league_id = ? AND t.season = ? AND t.type = 'WAIVER' AND t.status = 'EXECUTED'
        GROUP BY t.transaction_id, t.scoring_period, t.bid_amount
        ORDER BY t.transaction_id""", [lg["league_id"], lg["season"]])
    ids = sorted({r["espn_player_id"] for r in rows if r["adds"] == 1 and r["espn_player_id"] is not None})
    players = {p["espn_player_id"]: p for p in fetch_dicts(
        con, f"""SELECT espn_player_id, full_name, position FROM fantasy_players
                 WHERE espn_player_id IN ({", ".join("?" for _ in ids)})""", ids)} if ids else {}
    out = []
    for r in rows:
        # A claim adding more than one player (or none) has no single position: counted overall only.
        p = players.get(r["espn_player_id"]) if r["adds"] == 1 else None
        out.append({"transaction_id": r["transaction_id"], "scoring_period": r["scoring_period"],
                    "bid_amount": r["bid_amount"], "espn_player_id": p and r["espn_player_id"],
                    "full_name": p and p.get("full_name"), "position": p and p.get("position")})
    return out


@router.get("/waivers/faab")
def get_faab(
    team_id: int | None = Query(None, ge=0, le=999_999, description="Fantasy team the guidance is for (defaults to the owner's team)"),
    claims: str = Query("", max_length=MAX_CLAIMS * 45,
                        description="Claims in priority order, comma-separated, each ADD[:DROP] (no bid: the guidance never depends on one)"),
    share: int = Query(DEFAULT_SHARE, ge=1, le=100,
                       description="Your assumption: the most of your remaining FAAB one claim may take, in percent"),
):
    """FAAB bid guidance for an ordered claim plan: what the provider supplied (capability audit), the league's
    observed winning bids, and a labelled heuristic range per claim that never exceeds the known remaining budget,
    checked across the plan. Read-only: nothing is stored, bid or sent anywhere.

    The plan is one parameter, as on the claim planner: the league-scoped response cache keys on the sorted query
    pairs, and the plan and the share are both part of that key."""
    parts = claims.split(",") if claims else []
    if len(parts) > MAX_CLAIMS:
        raise HTTPException(422, f"A plan holds at most {MAX_CLAIMS} claims.")
    if any(len(c) > 40 for c in parts):
        raise HTTPException(422, "A claim is ADD[:DROP].")
    try:
        plan = [parse_claim(c) for c in parts]
    except ClaimPlanError as err:
        raise HTTPException(422, str(err))
    if any(p.count(":") > 1 for p in parts):
        raise HTTPException(422, "A claim is ADD[:DROP]: guidance never depends on a bid.")
    ids = [x for c in plan for x in (c.add, c.drop) if x is not None]
    with db() as con:
        sim = load_simulation(con, team_id, ids)
        try:
            checked = evaluate_claim_plan(sim.state, sim.team_id, plan, sim.ctx)
        except ClaimPlanError as err:
            raise HTTPException(422, str(err))
        except LookupError as err:
            raise HTTPException(404, str(err))
        provider = build_league_rules(sim.lg)["source"]["provider"]
        teams = fetch_dicts(con, "SELECT team_id, faab_spent FROM fantasy_teams WHERE league_id = ? AND season = ?",
                            [sim.lg["league_id"], sim.lg["season"]])
        out = evaluate_faab(checked, provider=provider, provider_label=sim.ctx.provider,
                            settings=object_value(sim.lg.get("settings_raw")), team_row=sim.ctx.team_row, teams=teams,
                            observed_rows=observed_rows(con, sim.lg), share=share,
                            waiver_status_known=sim.ctx.waiver_status_known)
        return sim.finish(con, out)
