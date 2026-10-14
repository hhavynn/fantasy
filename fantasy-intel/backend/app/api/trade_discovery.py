"""Read-only trade opportunity discovery endpoint (hypothetical; nothing is written to the database
or sent to ESPN). See analytics/trade_discovery.py and docs/v2_trade_discovery.md."""

from __future__ import annotations

from collections import OrderedDict

from fastapi import APIRouter, HTTPException, Query

from ..analytics.manager_scouting import build_manager_scouting
from ..analytics.trade_data import load_league_state
from ..analytics.trade_discovery import CATEGORIES, NOTE, SHAPES, SORTS, TradeSearch, detail
from ..analytics.trades import ASSUMPTIONS, LIMITATIONS, NoRemainingWeeksError, TradeError
from ..config import get_settings
from .deps import db, one, rows

router = APIRouter(prefix="/api/trades", tags=["trade discovery"])

# Stage A depends only on the database contents, the anchor, the shapes and the evaluation window,
# so a few searches are kept in memory; any database write changes the file's mtime and the key.
_CACHE: OrderedDict = OrderedDict()
_CACHE_SIZE = 4
NEED_HIGH_BAND = 0.66      # the manager-scouting "high need" display band (analytics/manager_scouting.py)


def _search(state, anchor: int, shapes: tuple[str, ...]) -> TradeSearch:
    path = get_settings().db_path
    reason = state.start_week_reason or {}
    key = (str(path), path.stat().st_mtime_ns, anchor, shapes, state.start_week, bool(reason.get("uncertain")))
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    ts = TradeSearch(state, anchor, shapes)
    _CACHE[key] = ts
    while len(_CACHE) > _CACHE_SIZE:
        _CACHE.popitem(last=False)
    return ts


def _manager_context(con, team_id: int) -> dict | None:
    """Secondary explanation from the stored manager-scouting evidence. Never an acceptance estimate."""
    try:
        rep = build_manager_scouting(con, team_id)
    except LookupError:
        return None
    if rep is None:
        return None
    th = rep["trade_history"]
    return {
        "needs": [{k: n.get(k) for k in ("position", "need_score", "starters_required", "rostered", "healthy")}
                  for n in rep["current_needs"]],
        "roster_construction": [p for p in rep["current_roster"]["positions"] if p["count"] is not None],
        "roster_scoring_period": rep["current_roster"]["scoring_period"],
        "trade_history": {"completed_trades": th["completed_trades"], "confidence": th["trade_behavior_confidence"],
                          "behavior": th["behavior"], "uneven_player_count_trades": th["uneven_player_count_trades"],
                          "rule": th["confidence_rule"]},
        "headlines": [h for h in rep["headlines"] if h["category"] in ("current", "historical", "trade_history")],
        "note": "Secondary context from stored manager scouting: current needs, roster construction and observed "
                "tendencies with their confidence labels. It explains why a trade may be worth raising; it does not "
                "predict acceptance.",
    }


def _need_notes(row: dict, ctx: dict | None, names: dict[int, dict]) -> list[str]:
    """Plain statements linking incoming positions to the opponent's stored current needs."""
    if not ctx:
        return []
    need = {n["position"]: n for n in ctx["needs"]}
    out = []
    for pid in row["sends"]:            # what the opponent receives
        pos = (names.get(pid) or {}).get("position")
        n = need.get(pos)
        if n and n["need_score"] is not None and n["need_score"] >= NEED_HIGH_BAND:
            out.append(f"{row['opponent_name']} has a stored {pos} need score of {n['need_score']:.2f} (manager "
                       f"scouting's high band is >= {NEED_HIGH_BAND}); current roster state, not an acceptance signal.")
    return out


@router.get("/discover")
def discover(team_id: int | None = None,
             shapes: str = Query(",".join(SHAPES), description="comma-separated: 1-for-1,2-for-1,1-for-2"),
             sort: str = Query("my_gain", description=" | ".join(SORTS)),
             category: str | None = None, opponent: int | None = None,
             limit: int = Query(20, ge=1, le=100), include_equivalents: bool = False):
    """Hypothetical trade opportunities for one team (default: yours), exact lineup impact for both sides."""
    with db() as con:
        lg = one(con, "SELECT league_id, season, name, current_week, final_scoring_period FROM leagues "
                      "ORDER BY synced_at DESC LIMIT 1")
        if not lg:
            raise HTTPException(503, "ESPN league not synced yet. Run scripts/sync_espn.py.")
        teams = rows(con, "SELECT team_id, name, abbrev, is_mine FROM fantasy_teams WHERE league_id=? AND season=? "
                          "ORDER BY team_id", [lg["league_id"], lg["season"]])
        if team_id is None:
            mine = [t for t in teams if t["is_mine"]]
            if not mine:
                raise HTTPException(422, "No team is marked as yours (is_mine); pass team_id.")
            team_id = mine[0]["team_id"]
        if team_id not in {t["team_id"] for t in teams}:
            raise HTTPException(404, "Unknown team")
        state = load_league_state(con, lg["season"])
        if state is None:
            raise HTTPException(503, "League current/final week unknown. Run scripts/sync_espn.py.")
        shape_list = tuple(s.strip() for s in shapes.split(",") if s.strip())
        try:
            ts = _search(state, team_id, shape_list)
            out = ts.top(sort, category or None, opponent, limit, include_equivalents)
            per_opp = []
            for opp in ts.opponents:
                best = ts.top("my_gain", None, opp, 1)["results"]
                mutual = ts.top("min_side", "MUTUAL_PROJECTED_IMPROVEMENT", opp, 1)["results"]
                per_opp.append({"opponent_team_id": opp, "team_name": state.team_names.get(opp),
                                "best_my_gain": best[0] if best else None,
                                "best_mutual": mutual[0] if mutual else None})
        except NoRemainingWeeksError as err:
            raise HTTPException(409, str(err))
        except TradeError as err:
            raise HTTPException(422, str(err))
        results = [detail(state, r) for r in out["results"]]
        names = {p.espn_player_id: {"name": p.name, "position": p.position, "pro_team": p.pro_team,
                                    "on_ir": p.on_ir, "injury_status": p.injury_status}
                 for ps in state.rosters.values() for p in ps}
        ctx = {opp: _manager_context(con, opp) for opp in sorted({r["opponent_team_id"] for r in results})}
        for r in results:
            r["manager_context"] = ctx.get(r["opponent_team_id"])
            r["need_notes"] = _need_notes(r, r["manager_context"], names)
        search = out["search"]
        search["consistency_checked"] = len(results)
        search["consistency_failures"] = sum(1 for r in results if not r["consistent_with_manual_evaluator"])
        return {
            "league": lg, "anchor": next(t for t in teams if t["team_id"] == team_id), "teams": teams,
            "evaluation": ts.evaluation(),
            "query": {"team_id": team_id, "shapes": ts.shapes, "sort": sort, "category": category,
                      "opponent": opponent, "limit": limit, "include_equivalents": include_equivalents},
            "results": results, "search": search, "opponents": per_opp, "names": names,
            "categories": CATEGORIES, "sorts": list(SORTS), "shapes": list(SHAPES),
            "note": NOTE, "assumptions": ASSUMPTIONS, "limitations": LIMITATIONS + [
                "Discovery covers 1-for-1, 2-for-1 and 1-for-2 trades with one other team; 2-for-2 and multi-team "
                "trades are not searched yet.",
                "Sorting by a projected gain is descriptive; it is not an estimate of which trade a manager would accept.",
            ],
        }
