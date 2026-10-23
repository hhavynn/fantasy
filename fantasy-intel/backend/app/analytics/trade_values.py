"""Trade values board (issue #184): one league-aware value per rostered player, read-only. Contract `trade-values/v1`.

The value is the asset value the trade analyzer and Trade Discovery already use (trade_assets.AssetBoard, reused
unchanged, over the same state and window as POST /api/trades/evaluate):

  value = sum over the remaining weeks the player is NFL-eligible of max(0, projection - replacement level)

in this league's projected fantasy points, where the replacement level at his position in a week is the best
available free agent's projection, or the league-structure level (the first player beyond the league's starters,
flex slots counted for the position that fills them) when no free agent there is projected. Nothing is weighted,
no star premium is added and positions are never compared on raw points. It is a Fantasy Intel model estimate: not
a trade price, not a market or expert consensus and not an acceptance estimate.

Missing is not zero: a player with no projection in some window week has no value (`value` None); the sum of his
known weeks is shown separately as `known_value`. Ranks are tie-aware (1 + players with a strictly higher value) and
count known values only.

Change since last week (`change`): computed only from a stored projection snapshot made for an EARLIER week than the
first week of the window (projection_movers' snapshot selection: ESPN's daily history, or a stored Fantasy Intel run
of the same production model and league scoring). The earlier value is recomputed with that snapshot's projections
and today's rosters, free agents and NFL availability over the weeks both forecasts cover, so the change is the
projection movement, never a roster move. No such snapshot: the change is unavailable, never 0. Why a value moved is
given as two measured parts over the same weeks: the player's own projection change and the replacement-level change
at his position. They need not add up to the change, because weeks at or below replacement count 0.
"""

from __future__ import annotations

from dataclasses import replace

from . import projection_movers as movers
from . import projection_source as ps
from .team_lineups import Window, projection, r
from .trade_assets import AssetBoard
from .trades import with_streaming

CONTRACT = "trade-values/v1"
CALCULATION_VERSION = "tv-1"
LABEL = "Fantasy Intel model estimate"
LEAGUE_STRUCTURE = "league-structure replacement (no free agent projected)"
NOTE = ("Fantasy Intel model estimate: projected fantasy points above a replacement-level player at the same position, "
        "in this league's scoring, over the remaining weeks. It is not a trade price, not a market value and not an "
        "expert ranking, and it does not estimate whether anyone would accept a trade.")
CALCULATION = ("Value = the sum, over the remaining weeks the player is NFL-eligible, of his projection minus the "
               "replacement level at his position that week, counting a week at or below replacement as 0. Replacement "
               "is the best available free agent's projection at the position that week; when no free agent there is "
               "projected, it is the first player beyond the league's starters at the position (flex slots counted "
               "for the position that fills them in each team's best projected lineup). A week with no projection "
               "makes the value unavailable; it is never counted as 0.")
COMPARABLE = ("Values are comparable only with each other on this board: the same league and scoring, the same "
              "projection source and version, the same remaining weeks and the same replacement rule. They are not "
              "comparable with another league, another projection source or a value from another week.")
CHANGE_METHOD = ("The earlier value uses the earlier stored projections with today's rosters, free agents and NFL "
                 "availability, over the weeks both forecasts cover, so the change is projection movement only. Its two "
                 "parts are measured over the same weeks: the player's own projection change and the change in the "
                 "replacement level at his position. They need not add up to the change, because weeks at or below "
                 "replacement count 0.")
ESPN_SCORING_NOTE = ("ESPN does not store the league scoring with each snapshot, so a scoring change between the two "
                     "snapshots could not be told apart from a change in the projections.")


def projection_name(source: str) -> str:
    return f"Fantasy Intel projections ({ps.MODEL_VERSION})" if source == "independent" else "ESPN weekly projections"


def _weeks(ws) -> str:
    """"week 4", "weeks 3-17", "weeks 3, 5 and 7-9": consecutive weeks are written as a range."""
    ws = sorted(set(ws))
    runs: list[list[int]] = []
    for w in ws:
        if runs and w == runs[-1][-1] + 1:
            runs[-1].append(w)
        else:
            runs.append([w])
    parts = [str(x[0]) if len(x) == 1 else f"{x[0]}-{x[-1]}" for x in runs]
    head = "week " if len(ws) == 1 else "weeks "
    return head + (parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f" and {parts[-1]}")


def _ranks(values: dict[int, float]) -> dict[int, int]:
    """Tie-aware competition rank (1 + count strictly higher, 1e-9 tolerance), like AssetBoard's position ranks."""
    vals = list(values.values())
    return {pid: 1 + sum(1 for o in vals if o > v + 1e-9) for pid, v in values.items()}


def _players(state) -> dict:
    return {p.espn_player_id: p for ps_ in state.rosters.values() for p in ps_}


def _raw_values(board: AssetBoard, players: dict) -> dict[int, float]:
    """Full-precision value per player with a complete value: AssetBoard's own sum, before rounding."""
    out = {}
    for pid, pr in board.profiles.items():
        if pr["par"] is None:
            continue
        base = board.base.get(pr["position"], {})
        out[pid] = sum(max(0.0, v - (base.get(w) or {}).get("level", 0.0)) for w in board.weeks
                       if w not in players[pid].unavailable_weeks
                       and (v := projection(board.state.projections, pid, w)) is not None)
    return out


def _weekly(board: AssetBoard, p) -> list[dict]:
    """The calculation week by week: projection, replacement level and what the week adds."""
    base = board.base.get(p.position, {})
    out = []
    for w in board.weeks:
        lvl = base.get(w) or {}
        v = projection(board.state.projections, p.espn_player_id, w)
        row = {"week": w, "phase": board.window.phase(w), "projection": r(v), "replacement": r(lvl.get("level")),
               "replacement_source": "league_structure" if lvl.get("source") == LEAGUE_STRUCTURE
               else "free_agent" if lvl else None, "adds": None, "status": "counted"}
        if w in p.unavailable_weeks:
            row["status"] = "nfl_ineligible"
        elif v is None:
            row["status"] = "missing"
        else:
            row["adds"] = r(max(0.0, v - lvl.get("level", 0.0)))
        out.append(row)
    return out


def _uncertainty(pr: dict, p, weekly: list[dict], source_name: str) -> list[dict]:
    out = []
    if pr["missing_weeks"]:
        out.append({"code": "missing_projection",
                    "text": f"No {source_name} projection for {_weeks(pr['missing_weeks'])}, so the value is "
                            f"unavailable; a missing week is never counted as 0."})
    if pr["no_eligible_week"]:
        out.append({"code": "no_eligible_week", "text": "No remaining week in which he is NFL-eligible."})
    elif pr["return_unconfirmed"] and pr["nfl_ineligible_weeks"]:
        out.append({"code": "return_unconfirmed",
                    "text": f"Not NFL-eligible in {_weeks(pr['nfl_ineligible_weeks'])} and his return is not "
                            f"confirmed; the value counts only the weeks after that."})
    elif pr["return_unconfirmed"]:
        out.append({"code": "return_unconfirmed", "text": "On an NFL reserve list: eligible, but not confirmed active."})
    elif pr["nfl_ineligible_weeks"]:
        out.append({"code": "nfl_ineligible",
                    "text": f"Not NFL-eligible in {_weeks(pr['nfl_ineligible_weeks'])}; those weeks add nothing."})
    if pr["eligibility_unknown"]:
        out.append({"code": "eligibility_unknown", "text": "NFL eligibility is unknown."})
    if p.on_ir:
        out.append({"code": "fantasy_ir", "text": "In a fantasy IR slot; the value assumes he plays every week he is "
                                                  "not NFL-restricted."})
    if p.on_taxi:
        out.append({"code": "fantasy_taxi", "text": "On the taxi squad; the value assumes he plays every week he is "
                                                    "not NFL-restricted."})
    fallback = [x["week"] for x in weekly if x["replacement_source"] == "league_structure" and x["status"] == "counted"]
    if fallback:
        out.append({"code": "replacement_fallback",
                    "text": f"No free agent at {p.position} is projected in {_weeks(fallback)}, so the league-structure "
                            f"replacement level is used there."})
    return out


def _baseline(con, state, raw_state, window: Window, streaming: bool) -> tuple[dict, AssetBoard | None, AssetBoard | None]:
    """(change block, earlier board, current board over the same weeks); boards None when there is no comparison."""
    source = state.projection_source
    block = {"available": False, "reason": None, "baseline": None, "weeks": [], "excluded_weeks": [], "notes": [],
             "method": CHANGE_METHOD, "source": source}
    if not window.weeks:
        block["reason"] = "No remaining weeks, so there is nothing to compare."
        return block, None, None
    ctx = (movers._ours_context(con, state.season, None) if source == "independent"
           else movers._espn_context(con, state.season, None))      # "last week's outlook", never reconstructed
    base = ctx.get("baseline")
    if base is None or not base.get("weeks"):
        block["reason"] = ctx.get("reason") or "No earlier projection snapshot is stored, so no change is shown."
        return block, None, None
    cycle = base.get("cycle_week")
    ref = {"id": base["id"], "label": base["label"], "taken_at": movers._iso(base["taken_at"]), "cycle_week": cycle,
           "model_version": base.get("model_version")}
    block["baseline"] = ref
    if cycle is None or cycle >= window.start:
        block["reason"] = (f"The latest earlier snapshot ({base['label']}) was made for week {window.start} or later, "
                           f"not for an earlier week, so there is no week-over-week change to show.")
        return block, None, None
    weeks = tuple(w for w in window.weeks if w in base["weeks"])
    block["excluded_weeks"] = [{"week": w, "reason": "not projected in the earlier snapshot"}
                               for w in window.weeks if w not in base["weeks"]]
    if not weeks:
        block["reason"] = "The earlier snapshot projects none of the remaining weeks, so no change is shown."
        return block, None, None
    proj: dict[int, dict[int, float]] = {}
    for pid, cells in base["values"].items():
        for w in weeks:
            c = cells.get(w)
            if c is not None and c.get("value") is not None:
                proj.setdefault(pid, {})[w] = float(c["value"])
    earlier = replace(raw_state, projections=proj)
    if streaming:
        earlier = with_streaming(earlier)
    sub = Window(weeks, window.last_regular)
    block.update(available=True, weeks=list(weeks))
    block["notes"] = list(ctx.get("notes") or [])
    if source != "independent":
        block["notes"].append(ESPN_SCORING_NOTE)
    return block, AssetBoard(earlier, sub), None if weeks == window.weeks else AssetBoard(state, sub)


def _change(p, now: AssetBoard, before: AssetBoard) -> dict:
    pid, pos = p.espn_player_id, p.position
    a, b = now.profiles.get(pid) or {}, before.profiles.get(pid) or {}
    out = {"value_before": b.get("par"), "value_now": a.get("par"), "delta": None, "reason": None,
           "projection_change": None, "replacement_change": None,
           "position_rank_before": b.get("position_rank"), "position_rank_now": a.get("position_rank")}
    if b.get("par") is None:
        out["reason"] = (f"No projection for {_weeks(b['missing_weeks'])} in the earlier snapshot."
                         if b.get("missing_weeks") else "No earlier value.")
    elif a.get("par") is None:
        out["reason"] = f"No current projection for {_weeks(a.get('missing_weeks') or [])}."
    else:
        out["delta"] = r(a["par"] - b["par"])
    diffs, repl = [], []
    for w in now.weeks:
        if w in p.unavailable_weeks:
            continue
        v1, v0 = projection(now.state.projections, pid, w), projection(before.state.projections, pid, w)
        diffs.append(None if v1 is None or v0 is None else v1 - v0)
        l1, l0 = (now.base.get(pos) or {}).get(w), (before.base.get(pos) or {}).get(w)
        repl.append(None if l1 is None or l0 is None else l1["level"] - l0["level"])
    if diffs and None not in diffs:
        out["projection_change"] = r(sum(diffs))
    if repl and None not in repl:
        out["replacement_change"] = r(sum(repl))
    return out


def build_trade_values(con, lg: dict, raw_state, *, streaming: bool = True, provenance: dict | None = None) -> dict:
    """The `trade-values/v1` payload for the loaded league state (trade_data.load_league_state, the league's default
    projections). `streaming` mirrors the trade analyzer's default (K and D/ST valued against weekly streaming)."""
    from ..db.connection import fetch_dicts
    state = with_streaming(raw_state) if streaming else raw_state
    window = state.window()
    source = state.projection_source
    source_name = projection_name(source)
    teams = fetch_dicts(con, "SELECT team_id, name, is_mine FROM fantasy_teams WHERE league_id = ? AND season = ? "
                             "ORDER BY team_id", [lg["league_id"], lg["season"]])
    mine = next((t["team_id"] for t in teams if t["is_mine"]), None)
    out = {
        "contract": CONTRACT, "label": LABEL, "note": NOTE, "season": state.season,
        "league": {"name": lg.get("name"), "current_week": lg.get("current_week")},
        "my_team_id": mine,
        "teams": [{"team_id": t["team_id"], "name": t["name"], "is_mine": bool(t["is_mine"])} for t in teams],
        "available": bool(window.weeks), "reason": None,
        "basis": None, "change": None, "players": [], "summary": None, "provenance": provenance,
    }
    reason = state.start_week_reason or {}
    if not window.weeks:
        out["reason"] = reason.get("warning") or "Every fantasy week has already kicked off, so no value remains."
        return out
    board = AssetBoard(state, window)
    change, before, now_sub = _baseline(con, state, raw_state, window, streaming)
    now_cmp = now_sub or board
    raw = _raw_values(board, _players(state))
    overall = _ranks(raw)
    n = len(window.weeks)
    rows = []
    for team_id in sorted(state.rosters):
        for p in sorted(state.rosters[team_id], key=lambda x: x.espn_player_id):
            pr = board.profiles[p.espn_player_id]
            weekly = _weekly(board, p)
            row = {
                "espn_player_id": p.espn_player_id, "name": p.name, "position": p.position, "pro_team": p.pro_team,
                "injury_status": p.injury_status, "team_id": team_id, "team_name": state.team_names.get(team_id),
                "on_my_team": mine is not None and team_id == mine,
                "value": pr["par"], "value_per_week": r(pr["par"] / n) if pr["par"] is not None else None,
                "value_regular": pr["par_regular"], "value_playoff": pr["par_playoff"],
                "known_value": pr["known_par"] if pr["par"] is None else None,
                "rank": overall.get(p.espn_player_id), "rank_of": len(raw),
                "position_rank": pr["position_rank"], "position_rank_of": pr["position_rank_of"],
                "league_starters_at_position": pr["league_starters_at_position"],
                "starter_caliber": pr["starter_caliber"],
                "weeks_counted": sum(1 for x in weekly if x["status"] == "counted"),
                "missing_weeks": pr["missing_weeks"], "nfl_ineligible_weeks": pr["nfl_ineligible_weeks"],
                "availability_status": pr["availability_status"],
                "uncertainty": _uncertainty(pr, p, weekly, source_name),
                "weekly": weekly,
                "change": _change(p, now_cmp, before) if before is not None else None,
            }
            rows.append(row)
    unvalued = [x for x in rows if x["value"] is None]
    fallback = sorted({pos for pos, per in board.base.items()
                       if any(v["source"] == LEAGUE_STRUCTURE for v in per.values())})
    out["players"] = rows
    out["change"] = change
    out["summary"] = {"players": len(rows), "valued": len(rows) - len(unvalued), "unvalued": len(unvalued),
                      "changed": sum(1 for x in rows if x["change"] and x["change"]["delta"] is not None)}
    out["basis"] = {
        "label": LABEL, "calculation_version": CALCULATION_VERSION, "unit": "projected points above replacement",
        "calculation": CALCULATION, "comparable_within": COMPARABLE,
        "projection_source": source, "projection_label": source_name,
        "model_version": ps.MODEL_VERSION if source == "independent" else None,
        "scoring": "Projected fantasy points in this league's scoring, as the league's projection source states them.",
        "window": {"weeks": list(window.weeks), "start": window.start, "final": window.final,
                   "last_regular_season_week": window.last_regular,
                   "regular_weeks": [w for w in window.weeks if window.phase(w) == "regular"],
                   "playoff_weeks": [w for w in window.weeks if window.phase(w) == "playoff"],
                   "uncertain": bool(reason.get("uncertain")), "warning": reason.get("warning")},
        "kdst_streaming": streaming,
        "league_starters": board.starters,
        "flex_note": board.reference["note"],
        "incomplete_team_weeks": board.reference["incomplete_team_weeks"],
        "replacement_fallback_positions": fallback,
        "rostered_only": "Rostered players only: a free agent is the replacement level itself.",
    }
    return out

