"""Projection-source comparison: where the league provider's projections and Fantasy Intel's own (Ours) rank a player
differently, what the engines record about the larger gaps, and the floor / ceiling range the distribution engine
serves. Answers GET /api/rankings/compare (contract `projection-compare/v1`).

Read-only and SQL + stdlib only. Nothing here fits, trains, syncs or writes; every number comes from an existing
engine:

  ranks      analytics/rankings.rank_pool (the Rankings page's own pool, windows and projection values). Each source is
             ranked over the WHOLE pool with rankings.competition_ranks ("1, 2, 2, 4"; equal to 0.01 ties), overall
             (QB, RB, WR and TE together, in league scoring) and within the position, before any filter.
  drivers    the Ours row's own stored `explanation` (the model's largest input contributions), flags, missing inputs
             and participation estimate, with the run that produced it (as GET /api/projections/independent serves
             them). The provider publishes no reasoning, so nothing is said about why the provider differs.
  history    analytics/projection_movers.movement between the two newest stored forecasts of each source, and
             analytics/mover_evidence.explain for what was recorded over the same period ("not shown to be the cause").
  ranges     analytics/projection_ranges.for_week: the served next-week Ours run only (forecast horizon 0, the horizon
             the ranges were validated at), RB / WR / TE only. There is no rest-of-season range.

Sources. "provider" is the league's own provider's projections (ESPN). A provider that publishes no projections
(Sleeper) is listed as not eligible and every provider rank is unavailable: never a 0 and never a rank. "ours" is
Fantasy Intel's model; it is unavailable (with the engine's reason) when no verified run covers the window. A source
with no value in the window is never named as the source of a number. Neither source is an expert consensus.

A missing rank is never 0: a delta exists only when both sources rank the player, and every other row says which side
is missing (`delta.status`). A delta is "large" when both ranks differ by at least LARGE_RANK places in the chosen
scope, the projections differ by at least MIN_POINTS_PER_WEEK points a week and either projects RELEVANT_PER_WEEK points a
week or more (projection movers' own relevance floor). Only large rows on the returned page are explained, at most
EXPLAIN_MAX, so the number of queries stays bounded.
"""

from __future__ import annotations

import json

from . import forward_ours, mover_evidence
from . import projection_movers as pm
from . import projection_ranges as pr
from . import projection_source as ps
from . import rankings as rk
from .legacy_league import latest_league

CONTRACT = "projection-compare/v1"
SCOPES = ("position", "overall")
SORT_KEYS = ("disagreement", "delta", "points", "provider_rank", "ours_rank", "name")
SHOW = ("all", "large", "compared", "missing")
MAX_LIMIT = 300
EXPLAIN_MAX = 20
LARGE_RANK = {"position": 6, "overall": 15}
MIN_POINTS_PER_WEEK = 1.0
RELEVANT_PER_WEEK = pm.THRESHOLDS["relevant_per_week"]
TOP_INPUTS = 3
TOP_SIGNALS = 3
_ASC_DEFAULT = {"provider_rank", "ours_rank", "name"}
PROVIDER_NAMES = {"espn": "ESPN", "sleeper": "Sleeper"}
CONSENSUS_NOTE = ("Neither column is an expert consensus. The provider column is the league provider's own projection "
                  "as published; Ours is Fantasy Intel's own model.")


def _provider(con) -> tuple[str, str, bool]:
    """(provider key, display name, publishes projections) for the league, as GET /api/league-profile reads them."""
    lg = latest_league(con, ["settings_raw"])
    raw = (lg or {}).get("settings_raw")
    settings = json.loads(raw) if isinstance(raw, str) else (raw or {})
    key = str(settings.get("provider")) if isinstance(settings, dict) and settings.get("provider") else "espn"
    return key, PROVIDER_NAMES.get(key, key.title()), ps.default_source(con) == "espn"


def _validate(position, horizon, scope, sort, dir, q, status, show, limit, offset) -> tuple:
    pos = str(position or "ALL").upper()
    if pos != "ALL" and pos not in rk.POSITIONS:
        raise ValueError(f"position must be one of ALL, {', '.join(rk.POSITIONS)}")
    hor = str(horizon or "").lower()
    if hor not in rk.HORIZONS:
        raise ValueError(f"horizon must be one of {', '.join(rk.HORIZONS)}")
    sc = str(scope or "").lower()
    if sc not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    key = str(sort or "").lower()
    if key not in SORT_KEYS:
        raise ValueError(f"sort must be one of {', '.join(SORT_KEYS)}")
    if dir is None or dir == "":
        direction = "asc" if key in _ASC_DEFAULT else "desc"
    else:
        direction = str(dir).lower()
        if direction not in ("asc", "desc"):
            raise ValueError("dir must be one of asc, desc")
    stat = str(status or "").lower()
    if stat not in rk.STATUSES:
        raise ValueError(f"status must be one of {', '.join(rk.STATUSES)}")
    sh = str(show or "").lower()
    if sh not in SHOW:
        raise ValueError(f"show must be one of {', '.join(SHOW)}")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer between 1 and {MAX_LIMIT}")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be an integer >= 0")
    return pos, hor, sc, key, direction, rk.fold(str(q or "").strip()), stat, sh, limit, offset


def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def _rank_block(overall: tuple, position: tuple, pos: str | None) -> dict:
    (o, o_tied), (p, p_tied) = overall, position
    return {"overall": o, "overall_tied": o_tied, "position": p, "position_tied": p_tied,
            "position_label": f"{pos}{p}" if p is not None else None}


def _provider_status(x: dict, horizon: str, eligible: bool, name: str, window_label: str) -> tuple[str, str | None]:
    if not eligible:
        return "not_published", f"{name} publishes no projections"
    if x["projection"]["espn"] is not None:
        return "ok", None
    if horizon == "week" and x["projection"]["bye_weeks"]:
        return "bye", "On bye"
    if horizon == "week":
        return "missing", f"No {name} projection for {window_label.lower()}"
    return "incomplete", f"No {name} projection for every week of {window_label.lower()}"


def _ours_status(x: dict, horizon: str, available: bool, reason: str | None) -> tuple[str, str | None]:
    if not available:
        return "unavailable", reason or "No Fantasy Intel projection run covers this window"
    if x["projection"]["ours"] is not None:
        return "ok", None
    if horizon == "week" and x["projection"]["bye_weeks"]:
        return "bye", "On bye"
    return "no_estimate", "No Fantasy Intel (Ours) estimate"


def _delta(provider: dict, ours: dict, scope: str, weeks: int) -> dict:
    """Provider rank minus Ours rank in the scope (positive: Ours ranks him higher) and Ours minus provider points.
    None with the missing side named when either is unranked; never 0 for a missing rank."""
    pr_rank, o_rank = provider["rank"][scope], ours["rank"][scope]
    tied = bool(provider["rank"][f"{scope}_tied"] or ours["rank"][f"{scope}_tied"])
    if provider["status"] == "not_published":
        status = "provider_not_published"
    elif ours["status"] == "unavailable":
        status = "ours_unavailable"
    elif pr_rank is None and o_rank is None:
        status = "neither_ranked"
    elif pr_rank is None:
        status = "provider_missing"
    elif o_rank is None:
        status = "ours_missing"
    else:
        status = "ok"
    if status != "ok":
        return {"status": status, "rank": None, "points": None, "points_per_week": None, "direction": None,
                "tied": tied, "relevant": False, "large": False}
    rank = pr_rank - o_rank
    points = round(ours["value"] - provider["value"], 2)
    per_week = round(points / weeks, 2)
    relevant = max(ours["value"], provider["value"]) / weeks >= RELEVANT_PER_WEEK
    large = relevant and abs(rank) >= LARGE_RANK[scope] and abs(per_week) >= MIN_POINTS_PER_WEEK
    direction = "ours_higher" if rank > 0 else "provider_higher" if rank < 0 else "same"
    return {"status": "ok", "rank": rank, "points": points, "points_per_week": per_week, "direction": direction,
            "tied": tied, "relevant": relevant, "large": large}


def _best_rank(x: dict, scope: str) -> tuple:
    ranks = [r for r in (x["sources"]["provider"]["rank"][scope], x["sources"]["ours"]["rank"][scope]) if r is not None]
    return (not ranks, min(ranks) if ranks else 0, rk.fold(x["full_name"]), x["espn_player_id"])


def _sort_value(x: dict, key: str, scope: str):
    d = x["delta"]
    return {"disagreement": lambda: None if d["rank"] is None else (abs(d["rank"]), abs(d["points"])),
            "delta": lambda: d["rank"], "points": lambda: d["points"],
            "provider_rank": lambda: x["sources"]["provider"]["rank"][scope],
            "ours_rank": lambda: x["sources"]["ours"]["rank"][scope],
            "name": lambda: rk.fold(x["full_name"])}[key]()


def _ordered(rows: list[dict], key: str, direction: str, scope: str) -> list[dict]:
    """Nulls last in both directions; equal values keep the best-rank order (stable sorts)."""
    base = sorted(rows, key=lambda x: _best_rank(x, scope))
    present = [x for x in base if _sort_value(x, key, scope) is not None]
    absent = [x for x in base if _sort_value(x, key, scope) is None]
    present.sort(key=lambda x: _sort_value(x, key, scope), reverse=direction == "desc")
    return present + absent


def _shown(x: dict, show: str) -> bool:
    status = x["delta"]["status"]
    return (show == "all" or (show == "large" and x["delta"]["large"]) or (show == "compared" and status == "ok")
            or (show == "missing" and status != "ok"))


# ----------------------------------------------------------------------------- ranges


def _ranges(con, season: int, week: int) -> tuple[dict, dict]:
    """(response block, ranges by player) from the distribution engine for the week's served Ours run."""
    got = pr.for_week(con, season, week)
    positions = {}
    for pos, p in got["positions"].items():
        positions[pos] = {**p, "validation": pr.VALIDATION.get(pos) if p["served"] else None}
    block = {"available": got["available"], "reason": got["reason"], "version": got["version"],
             "coverage": got["coverage"], "levels": got["levels"], "conditional_on": got["conditional_on"],
             "definition": got["definition"], "positions": positions, "artifact": got["artifact"], "run": got["run"],
             "validation_source": pr.VALIDATION_SOURCE, "week": week,
             "horizon": f"Week {week}, the next-week forecast (forecast horizon 0)",
             "rest_of_season": {"available": False,
                                "reason": "The distribution engine is validated for next-week forecasts only (forecast "
                                          "horizon 0), so it gives no rest-of-season range."}}
    return block, got["ranges"]


def _row_range(x: dict, block: dict, ranges: dict, rows_by_pid: dict) -> dict:
    pos = x["position"]
    p = block["positions"].get(pos) or {}
    if not block["available"]:
        return {"available": False, "reason": block["reason"]}
    if not p.get("served"):
        return {"available": False, "reason": p.get("reason") or f"No point-distribution model for {pos}"}
    got = ranges.get(x["espn_player_id"])
    if got is None:
        return {"available": False, "reason": "No Ours projection for this player in the served run, so no range"}
    row = rows_by_pid.get(x["espn_player_id"]) or {}
    v = p.get("validation") or {}
    conf, replay = v.get("confirmation") or {}, v.get("replay") or {}
    return {"available": True, "floor": got["floor"], "median": got["median"], "ceiling": got["ceiling"],
            "centre": got["projection"], "week": block["week"], "horizon": block["horizon"],
            "basis": f"{block['version']} point distribution around the Ours projection, if he plays (P10 / P50 / P90)",
            "coverage": {"nominal": block["coverage"], "confirmation": conf or None, "replay": replay or None,
                         "text": (f"80% nominal; measured {conf['coverage']:.3f} on {conf['rows']:,} {pos} rows "
                                  f"({conf['window']}) and {replay['coverage']:.3f} on the {replay.get('kind', '')} {replay['window']} replay")
                         if conf and replay else "80% nominal"},
            "sample": {"games_used": row.get("games_used"),
                       "latest_usage": ({"season": row["latest_usage_season"], "week": row["latest_usage_week"]}
                                        if row.get("latest_usage_week") is not None else None)}}


# ----------------------------------------------------------------------------- explanations


def _model_drivers(x: dict, state: dict | None, week: int, provider_name: str, eligible: bool) -> dict:
    """The Ours row's own stored drivers for `week`, with the run that produced them. Nothing is inferred."""
    if not state or not state.get("available"):
        return {"available": False, "reason": "Ours is unavailable for this window, so there are no model inputs"}
    sel = (state.get("selected") or {}).get(week) or {}
    run = sel.get("run")
    row = (state.get("by_pid") or {}).get(week, {}).get(x["espn_player_id"])
    if run is None or row is None:
        return {"available": False, "reason": f"The served run for week {week} has no row for this player"}
    inputs = [e for e in (_json(row.get("explanation")) or []) if isinstance(e, dict)]
    inputs.sort(key=lambda e: (e.get("contribution") is None, -abs(e.get("contribution") or 0.0)))
    part = _json(row.get("participation")) or {}
    why_not = (f"{provider_name} publishes no reasoning with its projections" if eligible
               else f"{provider_name} publishes no projections")
    return {
        "available": True, "week": week,
        "inputs": [{"feature": e.get("feature"), "text": e.get("text"), "contribution": e.get("contribution"),
                    "value": e.get("value"), "training_mean": e.get("training_mean")} for e in inputs[:TOP_INPUTS]],
        "inputs_recorded": len(inputs),
        "flags": [str(f) for f in (_json(row.get("flags")) or [])],
        "missing_inputs": [str(m) for m in (_json(row.get("missing_inputs")) or [])],
        "coverage_status": row.get("coverage_status"),
        "p_play": row.get("p_play"), "participation_status": part.get("status") if isinstance(part, dict) else None,
        "games_used": row.get("games_used"),
        "latest_usage": ({"season": row["latest_usage_season"], "week": row["latest_usage_week"]}
                         if row.get("latest_usage_week") is not None else None),
        "run": {"run_id": run["run_id"], "model_version": run["model_version"], "created_at": str(run["created_at"])
                if run.get("created_at") is not None else None,
                "data_through": ({"season": run.get("data_through_season"), "week": run.get("data_through_week")}
                                 if run.get("data_through_week") is not None else None),
                "forecast_horizon": run.get("horizon"), "artifact_check": run.get("artifact_check")},
        "note": (f"The model's own largest input contributions for week {week}, as stored with the run. Only Ours is "
                 f"explained: {why_not}."),
    }


def _signal(s: dict) -> dict:
    return {k: s.get(k) for k in ("kind", "label", "before", "after", "unit", "source", "detail")}


def _movers(con, season: int, source: str, view: str, pids: list[int], label: str) -> dict:
    """The two newest stored forecasts of a source: each listed player's move and what was recorded alongside it."""
    try:
        res = pm.movement(con, season, source=source, view=view, limit=pm.MAX_LIMIT)
    except ValueError as err:
        return {"available": False, "label": label, "reason": str(err), "players": {}}
    if not res.get("available"):
        return {"available": False, "label": label, "reason": res.get("reason"), "players": {}}
    listed = {int(m["espn_player_id"]): m for m in res.get("players") or []}
    found = [listed[p] for p in pids if p in listed]
    base, cur = res["baseline"], res["current"]
    evidence = mover_evidence.explain(
        con, season, found, window_from=base["taken_at"], window_to=cur["taken_at"], source=res["source"],
        window_weeks=res["window"]["weeks"], baseline_run_ids=base.get("run_ids"),
        current_run_ids=cur.get("run_ids")) if found else {}
    capped = res["summary"]["compared"] > len(listed)
    players = {}
    for pid in pids:
        m = listed.get(pid)
        if m is None:
            players[pid] = {"listed": False, "text": (
                "Not among the largest moves listed, or not comparable between the two forecasts" if capped else
                "No comparable move: one of the two forecasts has no value for him in the compared weeks")}
            continue
        ev = evidence.get(pid) or {}
        players[pid] = {"listed": True, "before": m["before"], "after": m["after"], "delta": m["delta"],
                        "rank_before": m["rank_before"], "rank_after": m["rank_after"], "direction": m["direction"],
                        "evidence": {"headline": ev.get("headline"),
                                     "signals": [_signal(s) for s in (ev.get("signals") or [])[:TOP_SIGNALS]],
                                     "missing": (ev.get("missing") or [])[:2], "note": ev.get("note")}}
    return {"available": True, "label": label, "reason": None, "baseline": base["label"], "current": cur["label"],
            "weeks": res["window"]["weeks"], "players": players}


# ----------------------------------------------------------------------------- payload


def build_compare(con, *, position: str = "ALL", horizon: str = "week", scope: str = "position",
                  sort: str = "disagreement", dir: str | None = None, q: str = "", status: str = "all",
                  show: str = "all", limit: int = 100, offset: int = 0) -> dict:
    """The `projection-compare/v1` payload (module docstring). ValueError naming the parameter for a bad value;
    rankings.RankingsUnavailable when the league is not synced or its current week is unknown."""
    position, horizon, scope, sort_key, direction, needle, status, show, limit, offset = _validate(
        position, horizon, scope, sort, dir, q, status, show, limit, offset)
    provider_key, provider_name, eligible = _provider(con)
    pool = rk.rank_pool(con, horizon=horizon, source="independent")
    season, win, players, state = pool["season"], pool["window"], pool["players"], pool["ours"]
    weeks, first, label = win["weeks"], win["first"], win["label"]
    ours_on = pool["ours_on"]
    ours_reason = (state or {}).get("reason")
    n = len(weeks)

    provider_values = {x["espn_player_id"]: x["projection"]["espn"] if eligible else None for x in players}
    ours_values = {x["espn_player_id"]: x["projection"]["ours"] if ours_on else None for x in players}
    ranks = {}
    for src, values in (("provider", provider_values), ("ours", ours_values)):
        overall = rk.competition_ranks(values)
        by_pos = {pos: rk.competition_ranks({x["espn_player_id"]: values[x["espn_player_id"]] for x in players
                                             if x["position"] == pos}) for pos in rk.POSITIONS}
        ranks[src] = {x["espn_player_id"]: _rank_block(overall[x["espn_player_id"]],
                                                       by_pos[x["position"]][x["espn_player_id"]], x["position"])
                      for x in players}

    rows = []
    for x in players:
        pid = x["espn_player_id"]
        p_status, p_reason = _provider_status(x, horizon, eligible, provider_name, label)
        o_status, o_reason = _ours_status(x, horizon, ours_on, ours_reason)
        provider = {"value": provider_values[pid], "status": p_status, "reason": p_reason, "rank": ranks["provider"][pid]}
        ours = {"value": ours_values[pid], "status": o_status, "reason": o_reason, "rank": ranks["ours"][pid],
                "origin": x["projection"]["ours_origin"] if ours_on else None}
        rows.append({"espn_player_id": pid, "gsis_id": x["gsis_id"], "full_name": x["full_name"],
                     "position": x["position"], "pro_team": x["pro_team"], "availability": x["availability"],
                     "game": x["game"], "roster": x["roster"], "bye_weeks": x["projection"]["bye_weeks"],
                     "sources": {"provider": provider, "ours": ours},
                     "delta": _delta(provider, ours, scope, n), "range": None, "explanation": None})

    matched = [x for x in rows if rk.matches(x, position, needle, status)]
    shown = [x for x in matched if _shown(x, show)]
    page = _ordered(shown, sort_key, direction, scope)[offset:offset + limit]

    range_block, ranges = _ranges(con, season, first)
    served_week_rows = {}
    if state and state.get("available"):
        served_week_rows = (state.get("by_pid") or {}).get(first, {})
    for x in page:
        x["range"] = _row_range(x, range_block, ranges, served_week_rows)

    explain = [x for x in page if x["delta"]["large"]][:EXPLAIN_MAX]
    history = {}
    if explain:
        pids = [x["espn_player_id"] for x in explain]
        view = "ros" if horizon == "ros" else "week"
        history["ours"] = _movers(con, season, "independent", view, pids, pm.OURS_LABEL)
        history["provider"] = (_movers(con, season, "espn", view, pids, f"{provider_name} projections") if eligible
                               else {"available": False, "label": "Unavailable", "players": {},
                                     "reason": f"{provider_name} publishes no projections, so there is no provider "
                                               "forecast history."})
        for x in explain:
            pid = x["espn_player_id"]
            x["explanation"] = {
                "model": _model_drivers(x, state, first, provider_name, eligible),
                "history": {k: {"label": h["label"], "available": h["available"], "reason": h["reason"],
                                "baseline": h.get("baseline"), "current": h.get("current"), "weeks": h.get("weeks"),
                                **({"player": h["players"].get(pid)} if h["available"] else {"player": None})}
                            for k, h in history.items()},
            }

    def count(items, pred):
        return sum(1 for x in items if pred(x))

    compared = [x for x in matched if x["delta"]["status"] == "ok"]
    large = [x for x in matched if x["delta"]["large"]]
    largest = sorted(large, key=lambda x: (-abs(x["delta"]["rank"]), -abs(x["delta"]["points"]),
                                           rk.fold(x["full_name"]), x["espn_player_id"]))[:3]
    provider_ranked = count(rows, lambda x: x["sources"]["provider"]["rank"]["overall"] is not None)
    ours_ranked = count(rows, lambda x: x["sources"]["ours"]["rank"]["overall"] is not None)

    if not eligible:
        provider_reason = (f"{provider_name} publishes no projections, so there is no provider ranking to compare. "
                           "Provider ranks are unavailable here, never 0.")
    elif provider_ranked == 0:
        provider_reason = f"No {provider_name} projection is stored for {label.lower()}."
    else:
        provider_reason = None
    ours_label = (forward_ours.label_for(state["versions"]) if ours_on else None)
    sources = [
        {"key": "provider", "kind": "provider", "provider": provider_key, "provider_name": provider_name,
         "label": f"{provider_name} projections" if provider_ranked else "Unavailable",
         "eligible": eligible, "available": provider_ranked > 0, "reason": provider_reason, "ranked": provider_ranked,
         "synced_at": rk.newest(pool["espn_synced"]) if eligible else None,
         "description": (f"{provider_name}'s own projection as published, in league scoring" if provider_ranked
                         else None)},
        {"key": "ours", "kind": "model", "label": ours_label if ours_ranked else "Unavailable", "eligible": True,
         "available": ours_ranked > 0, "ranked": ours_ranked,
         "reason": (None if ours_ranked else ours_reason or "No Fantasy Intel projection run covers this window"),
         "model_version": state["served"] if ours_on else None, "model_versions": state["versions"] if ours_on else [],
         "run_ids": [r["run_id"] for r in state["runs"]] if ours_on else [],
         "run_created_at": rk.newest(r["created_at"] for r in state["runs"]) if ours_on else None,
         "experimental": bool(ours_on and state.get("experimental")),
         "note": state.get("reason") if ours_on else None,
         "governance_note": ps.governance(state["served"])["status_note"] if ours_on else None,
         "description": ("Fantasy Intel's own model (expected points, league scoring); not an expert consensus"
                         if ours_ranked else None)},
    ]
    # rankings._window's note says which source ranks a window past the regular season in ESPN's words; a Sleeper
    # league's response would reword it into a false "Sleeper ranks ...". Compare states it in its own, provider-aware words.
    window = dict(win)
    if win["note"]:
        window["note"] = (
            f"The end of the regular season is unknown or already past, so {provider_name} totals cover {label.lower()} "
            "and Ours has no rest-of-season total for this window." if eligible else
            f"The end of the regular season is unknown or already past, so Ours has no rest-of-season total for this "
            f"window, and {provider_name} publishes no projections.")
    notes = [CONSENSUS_NOTE]
    if window["note"]:
        notes.append(window["note"])
    if horizon == "ros":
        notes.append("Rest-of-season totals count a week only when every required week has a value; a partial sum "
                     "is never ranked.")
    if not pool["covered"]:
        notes.append("The NFL schedule is not synced for this window, so byes are unknown.")
    if not pool["teams"]:
        notes.append("League rosters are not synced, so roster status is unknown.")
    if explain and len([x for x in page if x["delta"]["large"]]) > EXPLAIN_MAX:
        notes.append(f"Only the first {EXPLAIN_MAX} large disagreements on this page are explained.")

    return {
        "contract": CONTRACT, "season": season, "week": pool["window_state"].current, "horizon": horizon,
        "position": position, "scope": scope, "show": show, "roster_status": status, "q": q or "",
        "sort": {"key": sort_key, "dir": direction},
        "window": window, "rosters_synced": bool(pool["teams"]),
        "provider": {"key": provider_key, "name": provider_name, "publishes_projections": eligible},
        "sources": sources,
        "thresholds": {"large_rank": LARGE_RANK[scope], "min_points_per_week": MIN_POINTS_PER_WEEK,
                       "relevant_per_week": RELEVANT_PER_WEEK, "explain_max": EXPLAIN_MAX},
        "ranges": range_block,
        "counts": {"pool": len(rows), "matched": len(matched), "shown": len(shown), "returned": len(page),
                   "compared": len(compared), "large": len(large),
                   "ours_higher": count(large, lambda x: x["delta"]["direction"] == "ours_higher"),
                   "provider_higher": count(large, lambda x: x["delta"]["direction"] == "provider_higher"),
                   "provider_missing": count(matched, lambda x: x["delta"]["status"] in
                                             ("provider_missing", "provider_not_published")),
                   "ours_missing": count(matched, lambda x: x["delta"]["status"] in ("ours_missing", "ours_unavailable")),
                   "neither_ranked": count(matched, lambda x: x["delta"]["status"] == "neither_ranked")},
        "largest": [{"espn_player_id": x["espn_player_id"], "full_name": x["full_name"], "position": x["position"],
                     "rank": x["delta"]["rank"], "points": x["delta"]["points"], "direction": x["delta"]["direction"]}
                    for x in largest],
        "notes": notes,
        "players": page,
    }
