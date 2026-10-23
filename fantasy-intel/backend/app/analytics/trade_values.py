"""Trade values board (issue #184): one league-aware value per rostered player, read-only. Contract `trade-values/v1`.

The value is the asset value the trade analyzer and Trade Discovery use (points above replacement), read from
trade_assets.AssetBoard's own replacement levels over the analyzer's window, with one stricter rule:

  value = sum over the remaining weeks the player is NFL-eligible of max(0, projection - replacement level)

in this league's projected fantasy points. The replacement level at his position in a week is the best available free
agent's projection, or the league-structure level (the first player beyond the league's starters, flex slots counted
for the position that fills them) when no free agent there is projected. When neither exists (AssetBoard's
`measured` False, where the engines use 0.0) the replacement level is UNKNOWN here: that week adds nothing known and
the value is unavailable, never a value above 0. Otherwise the value equals AssetBoard's `par` exactly. Nothing is
weighted and no star premium is added. It is a Fantasy Intel model estimate: not a trade price, not a market or expert
consensus and not an acceptance estimate. K and D/ST are valued against the best available free agent each week like
every position; the analyzer's weekly streaming lineup baseline is not part of the value.

Missing is not zero: a player with no projection in some window week has no value (`value` None); the sum of his
known weeks is shown separately as `known_value`. Position ranks are tie-aware (1 + players with a strictly higher
value) among known values. The overall (cross-position) rank compares only players whose every counted week is
measured against a free agent, so values above different replacement baselines are never ranked together.

Each week's `adds` is rounded so the weeks sum exactly to the value as shown (largest remainder, to the cent). Each
week also names the projection's origin and version as served (ESPN, or for Fantasy Intel's projections the weekly
model's run version, the kicker or D/ST model, or the provider's number where the models abstain).

Change since last week (`change`): computed only from a stored projection snapshot made for an EARLIER week than the
first week of the window (projection_movers' snapshot selection: ESPN's daily history, or a stored Fantasy Intel run
of the same production model and league scoring). The earlier value is recomputed with that snapshot's projections
and today's rosters, free agents and NFL availability over the weeks both forecasts cover, so the change is the
projection movement, never a roster move. A Fantasy Intel player whose current projection in a compared week comes
from another source or version than the snapshot has no change. No such snapshot: the change is unavailable, never
0. Why a value moved is given as two measured parts over the same weeks: the player's own projection change and the
replacement-level change at his position. They need not add up to the change, because weeks at or below
replacement count 0.
"""

from __future__ import annotations

import math
from dataclasses import replace

from . import projection_movers as movers
from . import projection_source as ps
from .team_lineups import Window, lineup_relevant, projection, r
from .trade_assets import AssetBoard

CONTRACT = "trade-values/v1"
CALCULATION_VERSION = "tv-2"
LABEL = "Fantasy Intel model estimate"
LEAGUE_STRUCTURE = "league-structure replacement (no free agent projected)"
NO_REPLACEMENT = "No replacement level: no projected free agent and no rostered player beyond the starters"
DIFFERENT_BASELINE = "Different replacement baseline"
NOTE = ("Fantasy Intel model estimate: projected fantasy points above a replacement-level player at the same position, "
        "in this league's scoring, over the remaining weeks. It is not a trade price, not a market value and not an "
        "expert ranking, and it does not estimate whether anyone would accept a trade.")
CALCULATION = ("Value = the sum, over the remaining weeks the player is NFL-eligible, of his projection minus the "
               "replacement level at his position that week, counting a week at or below replacement as 0. Replacement "
               "is the best available free agent's projection at the position that week; when no free agent there is "
               "projected, it is the first player beyond the league's starters at the position (flex slots counted "
               "for the position that fills them in each team's best projected lineup). A week with no projection, or "
               "with no measurable replacement level, makes the value unavailable; it is never counted as 0. Each "
               "week's addition is rounded so the weeks add up exactly to the value shown.")
COMPARABLE = ("Values are comparable only with each other on this board: the same league and scoring, the same "
              "projections, the same remaining weeks and the same replacement rule. Values across positions compare "
              "points above each position's replacement. They are not comparable with another league, another "
              "projection source or a value from another week.")
KDST = ("Kickers and D/STs are valued like every position, against the best available free agent each week; the trade "
        "analyzer's weekly streaming lineup baseline is not part of this value.")
CHANGE_METHOD = ("The earlier value uses the earlier stored projections with today's rosters, free agents and NFL "
                 "availability, over the weeks both forecasts cover, so the change is projection movement only. Its two "
                 "parts are measured over the same weeks: the player's own projection change and the change in the "
                 "replacement level at his position. They need not add up to the change, because weeks at or below "
                 "replacement count 0.")
ESPN_SCORING_NOTE = ("ESPN does not store the league scoring with each snapshot, so a scoring change between the two "
                     "snapshots could not be told apart from a change in the projections.")
ESPN_TAG = ("espn", "ESPN")


def projection_name(source: str) -> str:
    return "Fantasy Intel projections" if source == "independent" else "ESPN weekly projections"


def _short(source: str) -> str:
    return "Fantasy Intel" if source == "independent" else "ESPN"


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


def _measured(lvl: dict | None) -> bool:
    return lvl is not None and lvl.get("measured", True)


def _apportion(raws: list[float], total_cents: int) -> list[float]:
    """Round each addend to the cent so they sum to `total_cents` exactly (largest remainder; ties by order)."""
    floors = [math.floor(x * 100 + 1e-9) for x in raws]
    order = sorted(range(len(raws)), key=lambda i: (-(raws[i] * 100 - floors[i]), i))
    for i in order[:max(0, total_cents - sum(floors))]:
        floors[i] += 1
    return [f / 100 for f in floors]


def _tag(tags: dict | None, pid: int, w: int) -> tuple[str, str | None]:
    return ESPN_TAG if tags is None else (tags.get(w) or {}).get(pid, ESPN_TAG)


def _calc(board: AssetBoard, p, tags: dict | None = None) -> dict:
    """One player's value on one board, week by week (module docstring)."""
    base = board.base.get(p.position, {})
    relevant = lineup_relevant(p.position, board.state.slots) and bool(p.pro_team)
    weekly, raws, missing, unmeasured, structure = [], [], [], [], []
    phase = {"regular": 0.0, "playoff": 0.0}
    for w in board.weeks:
        lvl = base.get(w)
        v = projection(board.state.projections, p.espn_player_id, w)
        measured = _measured(lvl)
        row = {"week": w, "phase": board.window.phase(w), "projection": r(v),
               "origin": None, "version": None,
               "replacement": r(lvl["level"]) if measured else None,
               "replacement_source": None if not measured else
               "league_structure" if lvl.get("source") == LEAGUE_STRUCTURE else "free_agent",
               "adds": None, "status": "counted"}
        if v is not None:
            row["origin"], row["version"] = _tag(tags, p.espn_player_id, w)
        if w in p.unavailable_weeks:
            row["status"] = "nfl_ineligible"
        elif v is None:
            row["status"] = "missing" if relevant else "not_counted"
            if relevant:
                missing.append(w)
        elif not measured:
            row["status"] = "no_replacement"
            unmeasured.append(w)
        else:
            gain = max(0.0, v - lvl["level"])
            raws.append((len(weekly), gain))
            if row["phase"] in phase:
                phase[row["phase"]] += gain
            if row["replacement_source"] == "league_structure":
                structure.append(w)
        weekly.append(row)
    total = sum(g for _, g in raws)
    complete = not missing and not unmeasured
    value = r(total) if complete else None
    adds = _apportion([g for _, g in raws], round(value * 100)) if complete else [r(g) for _, g in raws]
    for (i, _), a in zip(raws, adds):
        weekly[i]["adds"] = a
    return {"value": value, "raw": total if complete else None, "known": None if complete else r(total),
            "regular": r(phase["regular"]) if complete else None, "playoff": r(phase["playoff"]) if complete else None,
            "missing": missing, "unmeasured": unmeasured, "structure": structure, "weekly": weekly,
            "counted": len(raws), "versions": sorted({x["version"] for x in weekly if x["version"]})}


def _position_ranks(board: AssetBoard, calcs: dict[int, dict], players: dict) -> dict[int, dict]:
    by_pos: dict[str, dict[int, float]] = {}
    for pid, c in calcs.items():
        if c["raw"] is not None:
            by_pos.setdefault(players[pid].position, {})[pid] = c["raw"]
    out = {}
    for pos, vals in by_pos.items():
        starters = board.starters.get(pos)
        for pid, rank in _ranks(vals).items():
            out[pid] = {"position_rank": rank, "position_rank_of": len(vals),
                        "starter_caliber": starters is not None and rank <= starters}
    return out


def _uncertainty(pr: dict, p, c: dict, source: str) -> list[dict]:
    out = []
    if c["missing"]:
        out.append({"code": "missing_projection",
                    "text": f"No {_short(source)} projection for {_weeks(c['missing'])}, so the value is unavailable; "
                            f"a missing week is never counted as 0."})
    if c["unmeasured"]:
        out.append({"code": "no_replacement",
                    "text": f"{NO_REPLACEMENT} at {p.position} in {_weeks(c['unmeasured'])}, so the value is "
                            f"unavailable; an unmeasured replacement level is never counted as 0."})
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
    if c["structure"]:
        out.append({"code": "replacement_fallback",
                    "text": f"No free agent at {p.position} is projected in {_weeks(c['structure'])}, so the "
                            f"league-structure replacement level is used there and the value is not ranked against "
                            f"other positions (different replacement baseline)."})
    if len(c["versions"]) > 1:
        out.append({"code": "mixed_versions",
                    "text": f"His weekly projections come from more than one source or version "
                            f"({', '.join(c['versions'])})."})
    return out


def _baseline(con, state, window: Window) -> tuple[dict, AssetBoard | None, AssetBoard | None]:
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
    block["baseline"] = {"id": base["id"], "label": base["label"], "taken_at": movers._iso(base["taken_at"]),
                         "cycle_week": cycle, "model_version": base.get("model_version")}
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
    sub = Window(weeks, window.last_regular)
    block.update(available=True, weeks=list(weeks))
    block["notes"] = list(ctx.get("notes") or [])
    if source != "independent":
        block["notes"].append(ESPN_SCORING_NOTE)
    return block, AssetBoard(replace(state, projections=proj), sub), \
        None if weeks == window.weeks else AssetBoard(state, sub)


def _change(p, now: AssetBoard, before: AssetBoard, a: dict, b: dict, ranks_now: dict, ranks_before: dict,
            baseline_version: str | None) -> dict:
    pid, pos = p.espn_player_id, p.position
    out = {"value_before": b["value"], "value_now": a["value"], "delta": None, "reason": None,
           "projection_change": None, "replacement_change": None,
           "position_rank_before": (ranks_before.get(pid) or {}).get("position_rank"),
           "position_rank_now": (ranks_now.get(pid) or {}).get("position_rank")}
    other = sorted({x["week"] for x in a["weekly"] if x["version"] and baseline_version
                    and x["version"] != baseline_version})
    if other:
        used = sorted({x["version"] for x in a["weekly"] if x["week"] in other})
        out["reason"] = (f"His current projection for {_weeks(other)} comes from {', '.join(used)}, not "
                         f"{baseline_version} like the earlier snapshot; different sources or versions are never "
                         f"compared.")
        out["value_before"] = None
        return out
    if b["value"] is None:
        out["reason"] = (f"No projection for {_weeks(b['missing'])} in the earlier snapshot." if b["missing"] else
                         f"{NO_REPLACEMENT} in {_weeks(b['unmeasured'])} with the earlier projections."
                         if b["unmeasured"] else "No earlier value.")
    elif a["value"] is None:
        why = a["missing"] or a["unmeasured"]
        out["reason"] = f"No current value: {('no projection' if a['missing'] else NO_REPLACEMENT.lower())} in {_weeks(why)}."
    else:
        out["delta"] = r(a["value"] - b["value"])
    diffs, repl = [], []
    for w in now.weeks:
        if w in p.unavailable_weeks:
            continue
        v1, v0 = projection(now.state.projections, pid, w), projection(before.state.projections, pid, w)
        diffs.append(None if v1 is None or v0 is None else v1 - v0)
        l1, l0 = (now.base.get(pos) or {}).get(w), (before.base.get(pos) or {}).get(w)
        repl.append(l1["level"] - l0["level"] if _measured(l1) and _measured(l0) else None)
    if diffs and None not in diffs:
        out["projection_change"] = r(sum(diffs))
    if repl and None not in repl:
        out["replacement_change"] = r(sum(repl))
    return out


def _projection_tags(con, state, window: Window) -> dict | None:
    """Per week and player, the origin and version actually served (projection_source.league_projections' own detail,
    the same selection load_league_state made). None for ESPN projections (one source, no versions)."""
    if state.projection_source != "independent":
        return None
    detail: dict = {}
    ps.league_projections(con, state.season, window.weeks, "independent", detail=detail)
    return detail


def build_trade_values(con, lg: dict, state, *, provenance: dict | None = None) -> dict:
    """The `trade-values/v1` payload for the loaded league state (trade_data.load_league_state, the league's default
    projections), over the trade analyzer's window."""
    from ..db.connection import fetch_dicts
    window = state.window()
    source = state.projection_source
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
    players = {p.espn_player_id: p for ps_ in state.rosters.values() for p in ps_}
    tags = _projection_tags(con, state, window)
    board = AssetBoard(state, window)
    calcs = {pid: _calc(board, p, tags) for pid, p in players.items()}
    pos_ranks = _position_ranks(board, calcs, players)
    comparable = {pid: c["raw"] for pid, c in calcs.items() if c["raw"] is not None and not c["structure"]}
    overall = _ranks(comparable)
    change, before, now_sub = _baseline(con, state, window)
    cmp = {}
    if before is not None:
        now_b = now_sub or board
        now_c = calcs if now_sub is None else {pid: _calc(now_b, p, tags) for pid, p in players.items()}
        before_c = {pid: _calc(before, p) for pid, p in players.items()}
        r_now = pos_ranks if now_sub is None else _position_ranks(now_b, now_c, players)
        r_before = _position_ranks(before, before_c, players)
        version = (change["baseline"] or {}).get("model_version") if source == "independent" else None
        cmp = {pid: _change(p, now_b, before, now_c[pid], before_c[pid], r_now, r_before, version)
               for pid, p in players.items()}
    n = len(window.weeks)
    rows = []
    for team_id in sorted(state.rosters):
        for p in sorted(state.rosters[team_id], key=lambda x: x.espn_player_id):
            pid, c, pr = p.espn_player_id, calcs[p.espn_player_id], board.profiles[p.espn_player_id]
            pos_rank = pos_ranks.get(pid) or {}
            rank_reason = None
            if c["value"] is None:
                rank_reason = "No rank without a value"
            elif c["structure"]:
                rank_reason = (f"{DIFFERENT_BASELINE}: league-structure replacement in {_weeks(c['structure'])}, so "
                               f"it is ranked only within {p.position}")
            rows.append({
                "espn_player_id": pid, "name": p.name, "position": p.position, "pro_team": p.pro_team,
                "injury_status": p.injury_status, "team_id": team_id, "team_name": state.team_names.get(team_id),
                "on_my_team": mine is not None and team_id == mine,
                "value": c["value"], "value_per_week": r(c["raw"] / n) if c["raw"] is not None else None,
                "value_regular": c["regular"], "value_playoff": c["playoff"], "known_value": c["known"],
                "rank": overall.get(pid), "rank_of": len(comparable), "rank_reason": rank_reason,
                "position_rank": pos_rank.get("position_rank"), "position_rank_of": pos_rank.get("position_rank_of"),
                "league_starters_at_position": board.starters.get(p.position),
                "starter_caliber": bool(pos_rank.get("starter_caliber")),
                "weeks_counted": c["counted"], "missing_weeks": c["missing"], "no_replacement_weeks": c["unmeasured"],
                "nfl_ineligible_weeks": pr["nfl_ineligible_weeks"], "availability_status": pr["availability_status"],
                "projection_versions": c["versions"],
                "uncertainty": _uncertainty(pr, p, c, source),
                "weekly": c["weekly"],
                "change": cmp.get(pid),
            })
    unvalued = [x for x in rows if x["value"] is None]
    fallback = sorted({pos for pos, per in board.base.items()
                       if any(v["source"] == LEAGUE_STRUCTURE and v.get("measured", True) for v in per.values())})
    unmeasured = sorted({pos for pos, per in board.base.items() if any(not _measured(v) for v in per.values())})
    # Every version that produced a projection this board read: rostered players and the free agents that set the
    # replacement levels, over the window.
    if tags is None:
        versions = ["ESPN"]
    else:
        pool = set(players) | {p.espn_player_id for p in state.free_agents}
        versions = sorted({v for w in window.weeks for pid, (_, v) in (tags.get(w) or {}).items() if pid in pool and v})
    out["players"] = rows
    out["change"] = change
    out["summary"] = {"players": len(rows), "valued": len(rows) - len(unvalued), "unvalued": len(unvalued),
                      "ranked_overall": len(comparable),
                      "changed": sum(1 for x in rows if x["change"] and x["change"]["delta"] is not None)}
    out["basis"] = {
        "label": LABEL, "calculation_version": CALCULATION_VERSION, "unit": "projected points above replacement",
        "calculation": CALCULATION, "comparable_within": COMPARABLE,
        "cross_position": "Values across positions compare points above each position's replacement.",
        "projection_source": source, "projection_label": projection_name(source), "projection_versions": versions,
        "scoring": "Projected fantasy points in this league's scoring, as the league's projection source states them.",
        "window": {"weeks": list(window.weeks), "start": window.start, "final": window.final,
                   "last_regular_season_week": window.last_regular,
                   "regular_weeks": [w for w in window.weeks if window.phase(w) == "regular"],
                   "playoff_weeks": [w for w in window.weeks if window.phase(w) == "playoff"],
                   "uncertain": bool(reason.get("uncertain")), "warning": reason.get("warning")},
        "kdst": KDST,
        "league_starters": board.starters,
        "flex_note": board.reference["note"],
        "incomplete_team_weeks": board.reference["incomplete_team_weeks"],
        "replacement_fallback_positions": fallback,
        "no_replacement_positions": unmeasured,
        "rostered_only": "Rostered players only: a free agent is the replacement level itself.",
    }
    return out
