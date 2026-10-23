"""Read-only trade opportunity discovery endpoint (hypothetical; nothing is written to the database
or sent to ESPN). See analytics/trade_discovery.py and docs/v2_trade_discovery.md.

Targeted discovery (issue #182, docs "Targeted discovery"): every discovery endpoint also takes `target_player`,
`target_position`, `need` (weakest | bye) and, in `mode=targeted`, `weeks`; `/discover/targets` lists what can be
targeted and how the roster needs resolve. All optional: without them every response is what it was."""

from __future__ import annotations

import threading
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException, Query

from ..analytics.bye_planner import build_bye_planner
from ..analytics.manager_scouting import build_manager_scouting
from ..analytics.schedule_intelligence import NotFound, NotSynced
from ..analytics.trade_assets import AssetBoard, league_needs
from ..analytics.trade_data import load_league_state
from ..analytics import trade_practical as practical
from ..analytics import trade_ratings
from ..analytics.trade_alternatives import FreeAgencyBoard, for_evaluation as trade_vs_free_agency
from ..analytics.player_availability import NOTE as AVAILABILITY_NOTE
from ..analytics.trade_discovery import (ALL_SHAPES, CATEGORIES, HEURISTIC_SHAPES, NOTE, SHAPES, SKILL_POSITIONS, SORTS,
                                         TradeSearch, TradeTarget, evaluation)
from ..analytics.trade_practical import MODES, PHASES, STRICTNESS
from ..analytics.team_lineups import STREAMER_IDS, lineup_relevant, stream_summary
from ..analytics.trades import (LIMITATIONS, NoRemainingWeeksError, TradeError, assumptions, streaming_enabled,
                                with_streaming)
from .deps import data_version, db, one, rows
from .signal_provenance import trade_provenance

router = APIRouter(prefix="/api/trades", tags=["trade discovery"])

# Stage A depends only on the database contents, the anchor, the shapes and the evaluation window,
# so a few searches are kept in memory; any database write (or hosted publish) changes data_version and the key.
_CACHE: OrderedDict = OrderedDict()
_CACHE_SIZE = 4
# The finished view (every row with its evidence) for the split page endpoints: the table summary and each
# row's evidence come from the same computation, so the evidence a row shows is the one it was ranked with.
_VIEWS: OrderedDict = OrderedDict()
_VIEWS_SIZE = 8
_VIEWS_LOCK = threading.Lock()       # rows' evidence requests arrive in parallel
NEED_HIGH_BAND = 0.66      # the manager-scouting "high need" display band (analytics/manager_scouting.py)
# Targeted discovery (issue #182).
TARGET_POSITIONS = ("QB", "RB", "WR", "TE", "K", "D/ST")
NEEDS = {
    "weakest": "Upgrade your weakest starting position: the position whose starters rank lowest in the league in projected "
               "points per slot (below the league median).",
    "bye": "Cover your first bye or lineup-coverage conflict week (bye planner): your gain is judged over that week.",
}
MAX_WEEKS_PARAM = 7                  # "17-18": the longest valid `weeks` value
_NEED_CACHE: OrderedDict = OrderedDict()      # resolved roster needs per data version, league, team and need
_NEED_CACHE_SIZE = 16
_NEED_LOCK = threading.Lock()


def _search(con, state, anchor: int, shapes: tuple[str, ...], positions: tuple[str, ...],
            focus: tuple[int, ...] | None = None) -> TradeSearch:
    reason = state.start_week_reason or {}
    # League identity and the anchor team are in every key: a search for one team is never served for another.
    key = (data_version(con), state.league_id, state.season, anchor, shapes, positions, state.start_week, bool(reason.get("uncertain")),
           streaming_enabled(state), focus)
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    ts = TradeSearch(state, anchor, shapes, positions=positions, focus_weeks=focus)
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

# Row fields the results table shows; the rest of a row (lineup detail, free-agency comparison, asset tables,
# manager context, notes) is its evidence, served one row at a time by /discover/evidence.
SUMMARY_FIELDS = ("key", "shape", "search_method", "anchor_team_id", "opponent_team_id", "opponent_name", "sends",
                  "receives", "mine", "theirs", "combined_ros_delta", "min_side_ros_delta", "categories", "tier",
                  "consistent_with_manual_evaluator")
SUMMARY_ASSET_FIELDS = ("label", "text", "note", "ratio", "you_send_par", "they_send_par")
DUAL_ASSET_FIELDS = ("label", "text", "ratio", "imbalance", "favours", "you_send_par", "they_send_par")
DUAL_LINEUP_FIELDS = ("before", "after", "delta", "average_weekly_delta")
EVIDENCE_FIELDS = ("key", "search_method", "opponent_name", "anchor_team_id", "opponent_team_id", "sends", "receives",
                   "trade_vs_free_agency", "injury_scenarios", "manager_context", "need_notes")


class DiscoverParams:
    """The query parameters shared by every discovery endpoint."""

    def __init__(self, team_id: int | None = None,
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
                 limit: int = Query(10, ge=1, le=50), heuristic: bool = True,
                 kdst_streaming: bool = Query(True, description="value K and D/ST against weekly free-agent "
                                                                "streaming (false: hold current K/D/ST all season)"),
                 target_player: int | None = Query(None, ge=1, le=2_147_483_647,
                                                   description="receive this player (on another roster in this league)"),
                 target_position: str | None = Query(None, max_length=4,
                                                     description="receive at least one player at this position: "
                                                                 + " | ".join(TARGET_POSITIONS)),
                 need: str | None = Query(None, max_length=8, description="a roster need: " + " | ".join(NEEDS)),
                 weeks: str | None = Query(None, max_length=MAX_WEEKS_PARAM,
                                           description="'targeted' mode only: judge your gain over week N or weeks A-B")):
        self.team_id, self.mode, self.phase, self.strictness = team_id, mode, phase, strictness
        self.include_kdst, self.positions, self.shapes, self.sort = include_kdst, positions, shapes, sort
        self.category, self.opponent, self.limit, self.heuristic = category, opponent, limit, heuristic
        self.kdst_streaming = kdst_streaming
        self.target_player, self.target_position, self.need, self.weeks = target_player, target_position, need, weeks


class _Resolved:
    """Validated parameters plus the league they apply to."""

    def __init__(self, lg, teams, team_id, state, shape_list, pos, target=None, focus=None, target_info=None):
        self.lg, self.teams, self.team_id, self.state, self.shape_list, self.pos = lg, teams, team_id, state, shape_list, pos
        self.target: TradeTarget | None = target          # what the anchor receives (None: no target filter)
        self.focus: tuple[int, ...] | None = focus         # focus weeks of a targeted horizon (None: the phase)
        self.target_info: dict = target_info or _no_target()


def _no_target() -> dict:
    return {"active": False, "player": None, "position": None, "need": None, "weeks": None}


def _parse_weeks(raw: str, weeks: tuple[int, ...]) -> tuple[int, ...]:
    """'N' or 'A-B' (A <= B), every week inside the evaluation window."""
    a, sep, b = raw.strip().partition("-")
    try:
        lo, hi = int(a), int(b) if sep else int(a)
    except ValueError:
        raise HTTPException(422, "weeks must be a week number or a range such as 7-9")
    out = tuple(range(lo, hi + 1))
    if not out or any(w not in weeks for w in out):
        raise HTTPException(422, f"weeks must be inside the evaluation window (weeks {weeks[0]}-{weeks[-1]})"
                                 if weeks else "No evaluation weeks remain.")
    return out


def _need_cached(con, state, team_id: int, need: str, compute):
    key = (data_version(con), state.league_id, state.season, team_id, need, state.start_week)
    with _NEED_LOCK:
        if key in _NEED_CACHE:
            _NEED_CACHE.move_to_end(key)
            return _NEED_CACHE[key]
    val = compute()
    with _NEED_LOCK:
        _NEED_CACHE[key] = val
        while len(_NEED_CACHE) > _NEED_CACHE_SIZE:
            _NEED_CACHE.popitem(last=False)
    return val


def _weakest(con, state, team_id: int) -> dict:
    """need=weakest: the anchor's lowest-ranked starting position among those strictly below the league median of
    projected points per slot (trade_assets.league_needs, the same needs the discovery rationale cites)."""
    def compute():
        window = state.window()
        mine = league_needs(state, window, AssetBoard(state, window))[team_id]
        short = [(p, n) for p, n in mine.items() if n.get("short") and n.get("league_rank") is not None]
        if not short:
            unknown = [p for p, n in mine.items() if n.get("incomplete")]
            return {"kind": "weakest", "resolved": False, "position": None, "week": None,
                    "reason": "None of your starting positions is below the league median of projected points per slot"
                              + (f"; unknown for {', '.join(unknown)} (missing projections)" if unknown else "")
                              + ", so no position filter is applied."}
        order = {p: i for i, p in enumerate(TARGET_POSITIONS)}
        pos, n = max(short, key=lambda x: (x[1]["league_rank"], -order.get(x[0], 99)))
        return {"kind": "weakest", "resolved": True, "position": pos, "week": None,
                "reason": f"Your {pos} starters rank {n['league_rank']} of {n['teams']} in projected points per slot "
                          "(below the league median)."}
    return _need_cached(con, state, team_id, "weakest", compute)


def _bye(con, state, team_id: int) -> dict:
    """need=bye: the anchor's first bye or coverage conflict week in the evaluation window (bye planner, bye-planner.v1)."""
    def compute():
        try:
            plan = build_bye_planner(con, team_id)
        except (NotSynced, NotFound) as err:
            return {"kind": "bye", "resolved": False, "position": None, "week": None,
                    "reason": f"The bye planner is unavailable ({err}), so the rest-of-season horizon is used."}
        window = set(state.window().weeks)
        # A week the roster cannot fill (a fact), a bye cluster or a current starter on bye; a week that is only
        # unconfirmed (unknown availability) is not a bye problem and is never chosen.
        week = next((w for w in plan["weeks"] if w["week"] in window
                     and (w["status"] == "short" or w["bye_cluster"] or w["starters_on_bye"])), None)
        if week is None:
            return {"kind": "bye", "resolved": False, "position": None, "week": None,
                    "reason": "The bye planner finds no short week, bye cluster or starter on bye in the remaining "
                              "weeks, so the rest-of-season horizon is used."}
        if week["status"] == "short":
            k = week["uncovered"]
            why = (f"{k} starting {'slot' if k == 1 else 'slots'} ({', '.join(week['short_slots'])}) cannot be filled "
                   "from your roster")
        elif week["bye_cluster"]:
            why = f"{len(week['byes'])} of your players are on bye"
        else:
            k = len(week["starters_on_bye"])
            why = "one of your current starters is on bye" if k == 1 else f"{k} of your current starters are on bye"
        return {"kind": "bye", "resolved": True, "position": None, "week": week["week"],
                "reason": f"Week {week['week']}: {why} (bye planner)."}
    return _need_cached(con, state, team_id, "bye", compute)


def _resolve_target(con, p: DiscoverParams, state, team_id: int, teams: list[dict]):
    """Validate the targeted-discovery parameters; (target, focus weeks, echo block, extra tradeable positions)."""
    if p.target_position is not None and p.target_position not in TARGET_POSITIONS:
        raise HTTPException(422, f"target_position must be one of {', '.join(TARGET_POSITIONS)}")
    if p.need is not None and p.need not in NEEDS:
        raise HTTPException(422, f"need must be one of {', '.join(NEEDS)}")
    if p.need == "weakest" and p.target_position is not None:
        raise HTTPException(422, "need=weakest chooses the position itself; leave target_position out")
    if (p.weeks is not None or p.need == "bye") and p.mode != "targeted":
        raise HTTPException(422, "weeks and need=bye set a horizon, which only the targeted mode uses")
    if p.need == "bye" and p.weeks is not None:
        raise HTTPException(422, "need=bye chooses the week itself; leave weeks out")
    if not any(x is not None for x in (p.target_player, p.target_position, p.need, p.weeks)):
        return None, None, _no_target()
    player = None
    if p.target_player is not None:
        owner = next(((t, x) for t, ps in sorted(state.rosters.items()) for x in ps
                      if x.espn_player_id == p.target_player), None)
        if owner is None or owner[0] == team_id:
            raise HTTPException(422, f"Player {p.target_player} is not on another team's roster in this league.")
        if p.opponent is not None and p.opponent != owner[0]:
            raise HTTPException(422, "target_player is on another team than the chosen opponent")
        x = owner[1]
        if p.target_position is not None and x.position != p.target_position:
            raise HTTPException(422, "target_player does not play target_position")
        player = {"espn_player_id": x.espn_player_id, "name": x.name, "position": x.position, "pro_team": x.pro_team,
                  "team_id": owner[0], "team_name": next((t["name"] for t in teams if t["team_id"] == owner[0]), None)}
    position, need, focus = p.target_position, None, None
    live = state.start_week is not None              # without a window the search answers 409 itself
    if p.need == "weakest" and live:
        need = _weakest(con, state, team_id)
        position = need["position"]
    if p.weeks is not None and live:
        focus = _parse_weeks(p.weeks, state.window().weeks)
    elif p.need == "bye" and live:
        need = _bye(con, state, team_id)
        focus = (need["week"],) if need["resolved"] else None
    target = TradeTarget(player["espn_player_id"] if player else None, position) \
        if player is not None or position is not None else None
    info = {"active": target is not None or focus is not None, "player": player, "position": position, "need": need,
            "weeks": list(focus) if focus else None}
    return target, focus, info

def _resolve(con, p: DiscoverParams) -> _Resolved:
    lg = one(con, "SELECT league_id, season, name, current_week, final_scoring_period FROM leagues "
                  "ORDER BY synced_at DESC LIMIT 1")
    if not lg:
        raise HTTPException(503, "ESPN league not synced yet. Run scripts/sync_espn.py.")
    teams = rows(con, "SELECT team_id, name, abbrev, is_mine FROM fantasy_teams WHERE league_id=? AND season=? "
                      "ORDER BY team_id", [lg["league_id"], lg["season"]])
    team_id = p.team_id
    if team_id is None:
        mine = [t for t in teams if t["is_mine"]]
        if not mine:
            raise HTTPException(422, "No team is marked as yours (is_mine); pass team_id.")
        team_id = mine[0]["team_id"]
    if team_id not in {t["team_id"] for t in teams}:
        raise HTTPException(404, f"Team {team_id} is not in {lg['name'] or 'this league'} ({lg['season']}).")
    if p.mode not in MODES or p.phase not in PHASES or p.strictness not in STRICTNESS:
        raise HTTPException(422, f"mode must be one of {', '.join(MODES)}; phase one of {', '.join(PHASES)}; "
                                 f"strictness one of {', '.join(STRICTNESS)}")
    if p.sort not in SORTS:
        raise HTTPException(422, f"sort must be one of {', '.join(SORTS)}")
    state = load_league_state(con, lg["season"])
    if state is None:
        raise HTTPException(503, "League current/final week unknown. Run scripts/sync_espn.py.")
    if p.kdst_streaming:
        state = with_streaming(state)
    shape_list = tuple(x for x in ALL_SHAPES if x in {y.strip() for y in p.shapes.split(",") if y.strip()})
    if not shape_list or {y.strip() for y in p.shapes.split(",") if y.strip()} - set(ALL_SHAPES):
        raise HTTPException(422, f"shapes must be among {', '.join(ALL_SHAPES)}")
    pos = tuple(sorted({x.strip() for x in p.positions.split(",") if x.strip()})) if p.positions else \
        tuple(SKILL_POSITIONS) + (("K", "D/ST") if p.include_kdst else ())
    target, focus, info = _resolve_target(con, p, state, team_id, teams)
    if target is not None:
        # A target's position is always tradeable (e.g. a kicker without include_kdst).
        wanted = {target.position} | ({info["player"]["position"]} if info["player"] else set())
        pos = tuple(sorted(set(pos) | {x for x in wanted if x}))
    return _Resolved(lg, teams, team_id, state, shape_list, pos, target, focus, info)


def _context(r: _Resolved, p: DiscoverParams, ev: dict) -> dict:
    """What the page header and filters need: the league, the teams, the window and the echoed query."""
    return {
        "league": r.lg, "anchor": next(t for t in r.teams if t["team_id"] == r.team_id), "teams": r.teams,
        "evaluation": ev,
        "query": {"team_id": r.team_id, "mode": p.mode, "phase": p.phase, "strictness": p.strictness,
                  "positions": list(r.pos), "include_kdst": any(x in r.pos for x in ("K", "D/ST")),
                  "shapes": list(r.shape_list), "sort": p.sort, "category": p.category, "opponent": p.opponent,
                  "limit": p.limit, "heuristic": p.heuristic, "kdst_streaming": p.kdst_streaming,
                  "target_player": p.target_player, "target_position": p.target_position, "need": p.need,
                  "weeks": p.weeks},
        "target": r.target_info, "target_positions": list(TARGET_POSITIONS), "needs": NEEDS,
        "mode_text": MODES[p.mode],
        "modes": MODES, "phases": list(PHASES), "strictness_options": STRICTNESS,
        "categories": CATEGORIES, "sorts": list(SORTS), "exact_shapes": list(SHAPES),
        "heuristic_shapes": list(HEURISTIC_SHAPES),
    }


def _kdst_policy(traded: bool, streaming: bool) -> str:
    stream = ("Every lineup, before and after a trade, can stream K and D/ST from free agents each week, so a "
              "kicker or defense counts only for what it adds above that weekly streaming baseline."
              if streaming else
              "The K/D/ST streaming baseline is off: each team is assumed to start its current K and D/ST all "
              "season, so gaining or losing one can look much larger than it is.")
    if not traded:
        return ("Kickers and team defenses are left out of automatic discovery by default: ESPN's weekly D/ST and K "
                "projections swing with matchups and both positions are routinely streamed from free agents. They "
                "stay fully available in the manual analyzer. " + stream)
    return ("Kickers and team defenses are included. Their asset value is measured above the best available free "
            "agent at the position each week. " + stream)


def _build(con, r: _Resolved, p: DiscoverParams) -> dict:
    state, team_id, pos = r.state, r.team_id, r.pos
    board = FreeAgencyBoard(state, state.window())
    try:
        ts = _search(con, state, team_id, r.shape_list, pos, r.focus)
        anchor = next(t for t in r.teams if t["team_id"] == team_id)
        anchor_label = "You" if anchor.get("is_mine") else (anchor.get("name") or f"Team {team_id}")
        opponent = p.opponent if p.opponent is not None or not r.target_info["player"] else r.target_info["player"]["team_id"]
        out = practical.discover(ts, p.mode, p.phase, opponent, p.limit, p.strictness, p.sort, p.category or None,
                                 p.heuristic, board=board, anchor_label=anchor_label, target=r.target)
    except NoRemainingWeeksError as err:
        raise HTTPException(409, str(err))
    except (TradeError, ValueError) as err:
        raise HTTPException(422, str(err))
    names = {pl.espn_player_id: {"name": pl.name, "position": pl.position, "pro_team": pl.pro_team,
                                 "on_ir": pl.on_ir, **({"on_taxi": True} if pl.on_taxi else {}),
                                 "injury_status": pl.injury_status, "nfl_ineligible_weeks": sorted(pl.unavailable_weeks)}
             for ps in state.rosters.values() for pl in ps}
    shown = [x for items in out["sections"].values() for x in items]
    ctx = {opp: _manager_context(con, opp) for opp in sorted({x["opponent_team_id"] for x in shown})}
    for x in shown:
        x["manager_context"] = ctx.get(x["opponent_team_id"])
        x["need_notes"] = _need_notes(x, x["manager_context"], names)
        x["trade_vs_free_agency"] = trade_vs_free_agency(
            state, {"team_a": x["mine_detail"], "team_b": x["theirs_detail"]}, team_id, x["opponent_team_id"],
            x["sends"], x["receives"], board=board, labels={team_id: "You"})
        _free_agency_evidence(x)
    search = dict(out["coverage"]["exact"] or {})
    search["consistency_checked"] = out["consistency_checked"]
    search["consistency_failures"] = out["consistency_failures"]
    kdst = any(x in pos for x in ("K", "D/ST"))
    return {
        **_context(r, p, ts.evaluation()),
        **{k: out[k] for k in ("mode_text", "sections", "tiers", "excluded", "pool_rule", "pool_size", "coverage",
                               "opponents", "my_needs", "asset_note", "asset_thresholds", "lens", "lens_note",
                               "ranking_policy", "verdicts", "criteria", "projection_source", "horizon")},
        "results": shown, "search": search, "names": names,
        "kdst_policy": _kdst_policy(kdst, streaming_enabled(state)),
        "kdst_streaming": stream_summary(state.projections, state.window()),
        "availability_note": AVAILABILITY_NOTE,
        "note": NOTE, "assumptions": assumptions(state), "limitations": LIMITATIONS + [
            "Exact discovery covers 1-for-1, 2-for-1 and 1-for-2 trades with one other team (proven top lists). "
            "Larger shapes come from a bounded beam search: exact rows, not a proven top list. Multi-team trades "
            "are not searched.",
            "A roster need or asset balance is not a willingness to trade; nothing here estimates acceptance.",
            "A target (player, position, roster need or horizon) only narrows which trades are searched and shown; it "
            "never changes how a trade is valued.",
            "Lenses rank a labelled candidate pool drawn from the exact search; the ranking does not change which "
            "trades the exact search proves.",
        ],
    }


def _view(con, r: _Resolved, p: DiscoverParams) -> dict:
    """The full discovery payload, memoized per database version, evaluation window and query. Treat as read-only."""
    reason = r.state.start_week_reason or {}
    key = (data_version(con), r.lg["league_id"], r.lg["season"], r.team_id, r.shape_list, r.pos, r.state.start_week, bool(reason.get("uncertain")),
           reason.get("reason"), reason.get("warning"), p.mode, p.phase, p.strictness, p.sort, p.category, p.opponent,
           p.limit, p.heuristic, p.kdst_streaming, p.target_player, p.target_position, p.need, p.weeks,
           None if r.target is None else r.target.key, r.focus)
    with _VIEWS_LOCK:
        if key in _VIEWS:
            _VIEWS.move_to_end(key)
            return _VIEWS[key]
    view = _build(con, r, p)
    with _VIEWS_LOCK:
        _VIEWS[key] = view
        while len(_VIEWS) > _VIEWS_SIZE:
            _VIEWS.popitem(last=False)
    return view


def _dual_value_summary(dv: dict) -> dict:
    """The table's cut of a row's dual_value (docs/trade_discovery_dual_value.md, "API payload")."""
    pl, a, lu = dv["plausibility"], dv["asset"], dv["lineup"]
    return {"verdict": pl["verdict"], "verdict_text": pl["verdict_text"], "main_concern": pl["main_concern"],
            "front": (dv.get("rank") or {}).get("front"),
            "you_change": lu["you"]["key_change"], "them_change": lu["them"]["key_change"],
            "asset": {k: a[k] for k in DUAL_ASSET_FIELDS},
            "lineup": {side: {k: lu[side][k] for k in DUAL_LINEUP_FIELDS} for side in ("you", "them")},
            "checks": [{"criterion": c["criterion"], "status": c["status"]} for c in pl["checks"]]}


def _weekly_names(dv: dict, names: dict[int, dict], traded: tuple = ()) -> dict[int, dict]:
    """{id: {name, position}} for every traded player (bench assets included, which the ratings table names)
    and every player who enters or leaves a starting lineup in dual_value's weekly changes, so the page can name
    them (the K / D/ST streamer placeholders included)."""
    streamers = {pid: pos for pos, pid in STREAMER_IDS.items()}
    out = {pid: {"name": (names.get(pid) or {}).get("name"), "position": (names.get(pid) or {}).get("position")}
           for pid in traded}
    for side in ("you", "them"):
        for w in dv["lineup"][side]["weekly"]:
            for pid in (*w["entered"], *w["exited"]):
                if pid in streamers:
                    out[pid] = {"name": f"{streamers[pid]} streamer (free agent)", "position": streamers[pid]}
                else:
                    n = names.get(pid) or {}
                    out[pid] = {"name": n.get("name"), "position": n.get("position")}
    return out


def _summary_row(x: dict) -> dict:
    mc = x.get("manager_context")
    return {**{k: x[k] for k in SUMMARY_FIELDS}, **({"target_fit": x["target_fit"]} if "target_fit" in x else {}),
            "dual_value_summary": _dual_value_summary(x["dual_value"]),
            "asset_balance": {k: x["asset_balance"][k] for k in SUMMARY_ASSET_FIELDS},
            "rationale": {"trade_type": x["rationale"]["trade_type"], "concerns": x["rationale"]["concerns"]},
            "manager_context": None if mc is None else {"trade_history": {
                k: mc["trade_history"][k] for k in ("completed_trades", "confidence")}}}


def _evidence(x: dict, ratings: dict, names: dict[int, dict]) -> dict:
    ab, mine, theirs = x["asset_balance"], x["mine_detail"], x["theirs_detail"]
    return {**{k: x[k] for k in EVIDENCE_FIELDS},
            "dual_value": x["dual_value"], "ratings": trade_ratings.evidence(ratings, x["sends"], x["receives"]),
            "names": _weekly_names(x["dual_value"], names, (*x["sends"], *x["receives"])),
            "rationale": {"points": x["rationale"]["points"], "concerns": x["rationale"]["concerns"]},
            "asset_balance": {k: ab[k] for k in ("you_send", "they_send", "caveats")},
            "mine_detail": {k: mine[k] for k in ("team_id", "team_name", "fit_notes")},
            "theirs_detail": {k: theirs[k] for k in ("team_id", "team_name", "fit_notes")}}


@router.get("/discover")
def discover(p: DiscoverParams = Depends()):
    """Hypothetical trade opportunities for one team (default: yours). Every row is the exact lineup
    simulation for both teams; modes only decide which rows are shown and how they are explained."""
    with db() as con:
        r = _resolve(con, p)
        return _build(con, r, p)


@router.get("/discover/context")
def discover_context(p: DiscoverParams = Depends()):
    """The page header and filters for a discovery query, without running the search: league, teams, the
    evaluation window and the validated query echo (the same fields /discover returns)."""
    with db() as con:
        r = _resolve(con, p)
        if r.state.start_week is None:       # the same 409 the search raises (analytics/trade_discovery.py)
            reason = (r.state.start_week_reason or {}).get("reason") or "no actionable week"
            raise HTTPException(409, f"No remaining fantasy scoring periods ({reason}); there are no trades to discover.")
        return {**_context(r, p, evaluation(r.state, r.state.window())),
                "provenance": trade_provenance(con, r.lg, r.state)}


@router.get("/discover/summary")
def discover_summary(p: DiscoverParams = Depends()):
    """/discover with each row cut to what the results table shows. Every number, tier, concern and caveat is the
    same computation; a row's evidence comes from /discover/evidence. `names` covers only the players shown."""
    with db() as con:
        r = _resolve(con, p)
        view = _view(con, r, p)
    sections = {k: [_summary_row(x) for x in items] for k, items in view["sections"].items()}
    shown = {pid for items in view["sections"].values() for x in items for pid in x["sends"] + x["receives"]}
    shown |= {pid for o in view["opponents"] if o["best"] for pid in o["best"]["sends"] + o["best"]["receives"]}
    return {**{k: v for k, v in view.items() if k not in ("sections", "results", "names")},
            "sections": sections, "names": {pid: n for pid, n in view["names"].items() if pid in shown}}


@router.get("/discover/evidence")
def discover_evidence(key: str, p: DiscoverParams = Depends()):
    """The evidence for one row of a discovery view (same query parameters plus the row's `key`): why the trade
    is listed, its concerns, the free-agency comparison, the asset table, lineup fit and manager context, the full
    dual-value assessment, the 0-100 ratings (display only) and the names of the players in its weekly changes."""
    with db() as con:
        r = _resolve(con, p)
        view = _view(con, r, p)
        row = next((x for x in view["results"] if x["key"] == key), None)
        if row is None:
            raise HTTPException(404, "That trade is not in this discovery view")
        ratings = trade_ratings.load_ratings(con, r.lg["season"], row["sends"] + row["receives"])
    return _evidence(row, ratings, view["names"])


@router.get("/discover/targets")
def discover_targets(p: DiscoverParams = Depends()):
    """What targeted discovery can aim at for one team, without running a search: every player on another roster in
    this league (with his team and position), the positions this league starts, and how the two roster needs resolve
    right now (`weakest`: the lowest-ranked starting position below the league median; `bye`: the first bye or
    coverage conflict week from the bye planner). Unresolved needs say why; nothing is inferred."""
    with db() as con:
        r = _resolve(con, p)
        state = r.state
        if state.start_week is None:
            reason = (state.start_week_reason or {}).get("reason") or "no actionable week"
            raise HTTPException(409, f"No remaining fantasy scoring periods ({reason}); there are no trades to discover.")
        names = {t["team_id"]: t["name"] for t in r.teams}
        order = {pos: i for i, pos in enumerate(TARGET_POSITIONS)}
        players = sorted(({"espn_player_id": x.espn_player_id, "name": x.name, "position": x.position,
                           "pro_team": x.pro_team, "team_id": t, "team_name": names.get(t), "on_ir": x.on_ir,
                           **({"on_taxi": True} if x.on_taxi else {}), "injury_status": x.injury_status}
                          for t, ps in state.rosters.items() if t != r.team_id for x in ps
                          if x.position in TARGET_POSITIONS and lineup_relevant(x.position, state.slots)),
                         key=lambda x: (x["team_id"], order.get(x["position"], 99), x["name"] or "", x["espn_player_id"]))
        return {**_context(r, p, evaluation(state, state.window())),
                "players": players,
                "positions": [x for x in TARGET_POSITIONS if lineup_relevant(x, state.slots)],
                "need_resolution": {"weakest": _weakest(con, state, r.team_id), "bye": _bye(con, state, r.team_id)},
                "provenance": trade_provenance(con, r.lg, state)}
