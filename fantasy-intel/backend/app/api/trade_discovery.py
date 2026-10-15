"""Read-only trade opportunity discovery endpoint (hypothetical; nothing is written to the database
or sent to ESPN). See analytics/trade_discovery.py and docs/v2_trade_discovery.md."""

from __future__ import annotations

from collections import OrderedDict

from fastapi import APIRouter, HTTPException, Query

from ..analytics.manager_scouting import build_manager_scouting
from ..analytics.trade_data import load_league_state
from ..analytics import trade_practical as practical
from ..analytics.trade_alternatives import FreeAgencyBoard, for_evaluation as trade_vs_free_agency
from ..analytics.player_availability import NOTE as AVAILABILITY_NOTE
from ..analytics.trade_discovery import (ALL_SHAPES, CATEGORIES, HEURISTIC_SHAPES, NOTE, SHAPES, SKILL_POSITIONS, SORTS,
                                         TradeSearch)
from ..analytics.trade_practical import MODES, PHASES, STRICTNESS
from ..analytics.trades import ASSUMPTIONS, LIMITATIONS, NoRemainingWeeksError, TradeError
from ..config import get_settings
from .deps import db, one, rows

router = APIRouter(prefix="/api/trades", tags=["trade discovery"])

# Stage A depends only on the database contents, the anchor, the shapes and the evaluation window,
# so a few searches are kept in memory; any database write changes the file's mtime and the key.
_CACHE: OrderedDict = OrderedDict()
_CACHE_SIZE = 4
NEED_HIGH_BAND = 0.66      # the manager-scouting "high need" display band (analytics/manager_scouting.py)


def _search(state, anchor: int, shapes: tuple[str, ...], positions: tuple[str, ...]) -> TradeSearch:
    path = get_settings().db_path
    reason = state.start_week_reason or {}
    key = (str(path), path.stat().st_mtime_ns, anchor, shapes, positions, state.start_week,
           bool(reason.get("uncertain")))
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    ts = TradeSearch(state, anchor, shapes, positions=positions)
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


def _free_agency_evidence(row: dict) -> None:
    """Promote the anchor's trade-vs-free-agency verdict to the row's concerns when most of the gain is
    available without trading (majority rule, analytics/trade_alternatives), the comparison is unavailable
    or the trade loses; otherwise it stays in the row's own panel. Presentation only: tiers, ordering and
    canonical deltas are untouched."""
    tvf = row.get("trade_vs_free_agency") or {}
    if not tvf.get("available"):
        return
    me = tvf["team_a"]
    g = me["comparison"]["canonical_gain"]["ros_delta"]
    av = me["comparison"]["available_without_trade"]["ros_delta"]
    lead = me["statements"][0] if me["statements"] else None
    if lead is None:
        return
    if g is None or av is None or g <= 0 or 2 * av > g:
        row["rationale"]["concerns"] = [lead] + [c for c in row["rationale"]["concerns"] if c != lead]


DEFAULT_SHAPES = ("1-for-1", "2-for-1", "1-for-2", "2-for-2", "3-for-1", "1-for-3", "3-for-2", "2-for-3")


@router.get("/discover")
def discover(team_id: int | None = None,
             mode: str = Query("practical", description=" | ".join(MODES)),
             phase: str = Query("ros", description="ros | regular | playoff"),
             strictness: str = Query("standard", description=" | ".join(STRICTNESS)),
             include_kdst: bool = Query(False, description="also trade kickers and team defenses"),
             positions: str | None = Query(None, description="comma-separated tradeable positions (overrides include_kdst)"),
             shapes: str = Query(",".join(DEFAULT_SHAPES),
                                 description="comma-separated; exact: 1-for-1,2-for-1,1-for-2; heuristic: "
                                             + ",".join(HEURISTIC_SHAPES)),
             sort: str = Query("my_gain", description="'all' mode only: " + " | ".join(SORTS)),
             category: str | None = Query(None, description="'all' mode only"),
             opponent: int | None = None,
             limit: int = Query(10, ge=1, le=50), heuristic: bool = True):
    """Hypothetical trade opportunities for one team (default: yours). Every row is the exact lineup
    simulation for both teams; modes only decide which rows are shown and how they are explained."""
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
        if mode not in MODES or phase not in PHASES or strictness not in STRICTNESS:
            raise HTTPException(422, f"mode must be one of {', '.join(MODES)}; phase one of {', '.join(PHASES)}; "
                                     f"strictness one of {', '.join(STRICTNESS)}")
        if sort not in SORTS:
            raise HTTPException(422, f"sort must be one of {', '.join(SORTS)}")
        state = load_league_state(con, lg["season"])
        if state is None:
            raise HTTPException(503, "League current/final week unknown. Run scripts/sync_espn.py.")
        shape_list = tuple(x for x in ALL_SHAPES if x in {y.strip() for y in shapes.split(",") if y.strip()})
        if not shape_list or {y.strip() for y in shapes.split(",") if y.strip()} - set(ALL_SHAPES):
            raise HTTPException(422, f"shapes must be among {', '.join(ALL_SHAPES)}")
        pos = tuple(sorted({x.strip() for x in positions.split(",") if x.strip()})) if positions else \
            tuple(SKILL_POSITIONS) + (("K", "D/ST") if include_kdst else ())
        board = FreeAgencyBoard(state, state.window())
        try:
            ts = _search(state, team_id, shape_list, pos)
            out = practical.discover(ts, mode, phase, opponent, limit, strictness, sort, category or None, heuristic,
                                     board=board)
        except NoRemainingWeeksError as err:
            raise HTTPException(409, str(err))
        except (TradeError, ValueError) as err:
            raise HTTPException(422, str(err))
        names = {p.espn_player_id: {"name": p.name, "position": p.position, "pro_team": p.pro_team,
                                    "on_ir": p.on_ir, "injury_status": p.injury_status,
                                    "nfl_ineligible_weeks": sorted(p.unavailable_weeks)}
                 for ps in state.rosters.values() for p in ps}
        shown = [r for items in out["sections"].values() for r in items]
        ctx = {opp: _manager_context(con, opp) for opp in sorted({r["opponent_team_id"] for r in shown})}
        for r in shown:
            r["manager_context"] = ctx.get(r["opponent_team_id"])
            r["need_notes"] = _need_notes(r, r["manager_context"], names)
            r["trade_vs_free_agency"] = trade_vs_free_agency(
                state, {"team_a": r["mine_detail"], "team_b": r["theirs_detail"]}, team_id, r["opponent_team_id"],
                r["sends"], r["receives"], board=board, labels={team_id: "You"})
            _free_agency_evidence(r)
        search = dict(out["coverage"]["exact"] or {})
        search["consistency_checked"] = out["consistency_checked"]
        search["consistency_failures"] = out["consistency_failures"]
        kdst = any(p in pos for p in ("K", "D/ST"))
        return {
            "league": lg, "anchor": next(t for t in teams if t["team_id"] == team_id), "teams": teams,
            "evaluation": ts.evaluation(),
            "query": {"team_id": team_id, "mode": mode, "phase": phase, "strictness": strictness,
                      "positions": list(pos), "include_kdst": kdst, "shapes": list(shape_list), "sort": sort,
                      "category": category, "opponent": opponent, "limit": limit, "heuristic": heuristic},
            **{k: out[k] for k in ("mode_text", "sections", "tiers", "excluded", "pool_rule", "pool_size", "coverage",
                                   "opponents", "my_needs", "asset_note", "asset_thresholds")},
            "results": shown, "search": search, "names": names,
            "modes": MODES, "phases": list(PHASES), "strictness_options": STRICTNESS,
            "categories": CATEGORIES, "sorts": list(SORTS), "exact_shapes": list(SHAPES),
            "heuristic_shapes": list(HEURISTIC_SHAPES),
            "kdst_policy": ("Kickers and team defenses are left out of automatic discovery by default: ESPN's weekly "
                            "D/ST and K projections swing with matchups, both positions are routinely streamed from "
                            "free agents, and the lineup delta does not credit the other team with that streaming. "
                            "They stay fully available in the manual analyzer."
                            if not kdst else
                            "Kickers and team defenses are included. Their asset value is measured above the best "
                            "available free agent at the position each week (the streaming baseline)."),
            "availability_note": AVAILABILITY_NOTE,
            "note": NOTE, "assumptions": ASSUMPTIONS, "limitations": LIMITATIONS + [
                "Exact discovery covers 1-for-1, 2-for-1 and 1-for-2 trades with one other team (proven top lists). "
                "Larger shapes come from a bounded beam search: exact rows, not a proven top list. Multi-team trades "
                "are not searched.",
                "A roster need or asset balance is not a willingness to trade; nothing here estimates acceptance.",
            ],
        }
