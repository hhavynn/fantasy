"""League analyzer (read-only, contract league-analyzer/v1; issue #191): every roster's projected starters, bench depth
and positional needs side by side, position by position, with a compare-two-teams mode.

Nothing here is a new strength model. Each number comes from an existing engine, unchanged:
  * starters by slot group: the exact weekly optimizer (team_lineups.project_team) and team_value.position_strength
    (average projected points each team's optimal lineup gets from a slot group per week, and the rank in that group);
  * replacement level: team_lineups.replacement_market (the best available free agent's projection per slot group and
    week), the same frontier My Team Value and Power rankings use;
  * bench depth: power_rankings.replacement_options (best bench projection against the best free agent per position);
  * team context: power_rankings.build_power_rankings (power score and rank, rest of season);
  * bye conflicts: bye_planner.build_bye_planner (schedule facts, no projections), per compared team;
  * positional needs: manager_scouting.build_manager_scouting (stored need scores and roster composition);
  * caveats: league_compatibility (the Power rankings entry: same lineup engine) and trades.roster_legality.

Normalization. Raw points are never a grade. Each cell is the group's projected points per starting slot per week
minus the replacement level per slot (the best available free agent there that week): "points above replacement per
slot", comparable across positions and slot counts. Next to it, a league-relative score (power_rankings.scale: best 100,
worst 0 within the group) and the rank. Both are labelled; neither is a probability.

Unknown is never worst. A group's value is exact for a team-week when no rostered, lineup-relevant player whose
position can reach that group (directly or through a shared flex slot) lacks a projection. A team with such a player in
any week of the horizon is UNKNOWN at that group (with the reason) and is left out of that group's ranks and scale; the
other teams are compared on the same weeks. This is team_value's comparable-week rule ("no complete forecast is compared
with a partial one") applied per slot group instead of to the whole lineup, so one unprojected kicker does not blank
every position. A league whose starting slots no engine fills may take skill players (#253 G3, not separable) leaves
every group unverified; separable ones (IDP, P, HC) leave the filled groups exact and the totals partial, said in words.
"""

from __future__ import annotations

from statistics import median
from types import MappingProxyType

from .lineup import FLEX_ELIGIBILITY, separable, slots_left_out, unsupported_of
from .playoff_race import NotSynced, RaceData
from .power_rankings import build_power_rankings, replacement_options, scale
from .team_lineups import (
    NO_PROJECTIONS,
    STREAM_POSITIONS,
    RosterPlayer,
    Window,
    lineup_relevant,
    project_team,
    r,
    replacement_market,
    slot_filled,
    source_label,
)
from .team_value import NotFound, position_strength
from .trades import roster_legality, with_streaming

CONTRACT = "league-analyzer/v1"
HORIZONS = MappingProxyType({"ros": "Rest of season", "week": "This week"})
RANKED, UNKNOWN, UNVERIFIED = "ranked", "unknown", "unverified"
POWER_TOOL = "power_rankings"      # the compatibility entry whose engine (optimal-lineup totals, every position) is ours

UNITS = MappingProxyType({
    "above_replacement": "projected points per starting slot per week above the best available free agent",
    "score": "league-relative score within the slot group: best ranked team 100, worst 0 (all equal: 50)",
    "points": "projected points per week from the group's starting slots (evidence, not a grade)",
})
METHOD = (
    ("Each team's optimal lineup is re-optimized every week with the league's own slots; a slot group's points are what "
     "its starting slots get in that lineup (bench points never count)."),
    ("Points above replacement per slot: the group's points per week divided by its number of starting slots, minus the "
     "best available free agent's projection for that slot (averaged over the same weeks). Positions with different "
     "scoring levels and slot counts become comparable this way; raw totals are shown only as evidence."),
    ("The score is league-relative: within each slot group the best ranked team is 100 and the worst 0. It is not "
     "comparable across leagues; ranks are."),
    ("A team is unknown at a group when a rostered player who could start there has no projection in a week of the "
     "horizon: that team is left out of the group's ranks instead of being ranked last, and the reason is shown."),
    "Strongest and weakest positions leave out K and D/ST, which every team can stream from waivers.",
    "Power rank is the Power rankings composite for the rest of the season, unchanged.",
    ("Compare mode adds the bench (best bench projection against the best free agent per position), bye weeks from the "
     "NFL schedule and the stored positional need scores. Nothing here is a win probability or a trade grade."),
)


class SameTeam(ValueError):
    """Compare mode needs two different teams."""


# ------------------------------------------------------------------ helpers --

def groups_of(slots: list[str]) -> list[str]:
    """The league's slot groups in lineup order (each slot label once)."""
    return list(dict.fromkeys(slots))


def eligible_positions(group: str) -> tuple[str, ...]:
    return FLEX_ELIGIBILITY.get(group, (group,))


def reach(slots: list[str]) -> dict[str, frozenset[str]]:
    """slot group -> every position whose missing projection can change that group's optimal assignment: positions
    linked through a slot that accepts both (a flex slot ties RB, WR and TE together; OP adds QB)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for g in groups_of(slots):
        elig = eligible_positions(g)
        for p in elig:
            parent.setdefault(p, p)
        for p in elig[1:]:
            parent[find(p)] = find(elig[0])
    roots = {g: {find(p) for p in eligible_positions(g)} for g in groups_of(slots)}
    return {g: frozenset(p for p in parent if find(p) in roots[g]) for g in roots}


def check_teams(race: RaceData, team_id: int | None, vs: int | None) -> tuple[int | None, int | None]:
    """(focus, compare) after validation: an id that is not a team of this league is NotFound; the same team twice is
    SameTeam. The focus defaults to the team marked as yours (None for a visitor without one)."""
    meta = {t["team_id"] for t in race.teams}
    for tid in (team_id, vs):
        if tid is not None and tid not in meta:
            raise NotFound(f"Unknown team {tid}")
    focus = team_id if team_id is not None else next((t["team_id"] for t in race.teams if t["is_mine"]), None)
    if vs is not None and focus is None:
        raise NotFound("No team is marked as yours (is_mine); pass team_id to compare two teams.")
    if vs is not None and vs == focus:
        raise SameTeam("Pick two different teams to compare.")
    return focus, vs


def horizon_weeks(window: Window, horizon: str) -> list[int]:
    if not window.weeks:
        return []
    return [window.weeks[0]] if horizon == "week" else list(window.weeks)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _weeks_text(weeks: list[int]) -> str:
    """"week 7", "weeks 3–17", "weeks 3, 5–6": consecutive weeks as a range."""
    runs: list[list[int]] = []
    for w in sorted(set(weeks)):
        if runs and w == runs[-1][1] + 1:
            runs[-1][1] = w
        else:
            runs.append([w, w])
    text = ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in runs)
    return f"week {text}" if len(weeks) == 1 else f"weeks {text}"


# ------------------------------------------------------------- unknown cells --

def unknown_reasons(projections: dict[int, dict | None], rosters: dict[int, list[RosterPlayer]], slots: list[str],
                    weeks: list[int], source: str) -> dict[int, dict[str, str]]:
    """team -> group -> why that team's group value is unknown over `weeks` (absent = exact)."""
    by_group = reach(slots)
    groups = groups_of(slots)
    noun = "projection" if source == NO_PROJECTIONS else f"{source} projection"
    out: dict[int, dict[str, str]] = {}
    for t, p in projections.items():
        if p is None:
            out[t] = {g: "no roster snapshot" for g in groups}
            continue
        players = {q.espn_player_id: q for q in rosters.get(t) or []}
        missing: dict[str, dict[int, list[int]]] = {}        # position -> player -> weeks
        for w in p["weeks"]:
            if w["week"] not in weeks:
                continue
            for pid in w["missing_projection_ids"]:
                pos = players[pid].position if pid in players else None
                missing.setdefault(pos, {}).setdefault(pid, []).append(w["week"])
        for g in groups:
            hit = {pos: per for pos, per in missing.items() if pos in by_group[g]}
            if not hit:
                continue
            parts = []
            for pos, per in sorted(hit.items(), key=lambda kv: str(kv[0])):
                for pid, ws in sorted(per.items()):
                    name = players[pid].name if pid in players and players[pid].name else f"Player #{pid}"
                    parts.append(f"{name} ({pos}) has no {noun} in {_weeks_text(ws)}")
            out.setdefault(t, {})[g] = "; ".join(parts)
    return out


# ------------------------------------------------------------------ build --

def _replacement(market: dict, group: str, weeks: list[int]) -> tuple[float | None, list[int]]:
    """Average best-available projection for one slot of `group` over `weeks`, and the weeks without one."""
    by_week = {w["week"]: w["positions"].get(group) or {} for w in market["weeks"]}
    vals = {w: (by_week.get(w) or {}).get("best_available_projection") for w in weeks}
    missing = [w for w, v in vals.items() if v is None]
    return (None if missing or not weeks else sum(vals.values()) / len(weeks)), missing


def _starters(p: dict, group: str, weeks: list[int], names: dict[int, str]) -> list[dict]:
    """Who fills a group's slots over `weeks`: weeks started there and average projected points when started."""
    rows: dict[object, dict] = {}
    for w in p["weeks"]:
        if w["week"] not in weeks:
            continue
        for a in w["assignment"]:
            if a["slot"] != group or not slot_filled(a):
                continue
            key = a["player_id"] if a["player_id"] is not None else ("streamer", a.get("name"))
            row = rows.setdefault(key, {"espn_player_id": a["player_id"], "streamer": bool(a.get("streamer")),
                                        "name": a.get("name") if a.get("streamer") else names.get(a["player_id"]),
                                        "position": a.get("position"), "weeks": 0, "_points": 0.0})
            row["weeks"] += 1
            row["_points"] += a["points"] or 0.0
    out = [{**{k: v for k, v in x.items() if k != "_points"}, "average_points": r(x["_points"] / x["weeks"])}
           for x in rows.values()]
    return sorted(out, key=lambda x: (-x["weeks"], -(x["average_points"] or 0), str(x["name"])))


def _lineup_total(p: dict | None, weeks: list[int]) -> dict:
    if p is None:
        return {"projected_per_week": None, "known_per_week": None, "complete": False}
    ws = [w for w in p["weeks"] if w["week"] in weeks]
    complete = bool(ws) and all(w["projection_complete"] for w in ws)
    known = sum(w["known_lineup_points"] or 0 for w in ws)
    return {"projected_per_week": r(known / len(ws)) if complete else None,
            "known_per_week": r(known / len(ws)) if ws else None, "complete": complete}


def build_league_analyzer(race: RaceData, extras: dict | None = None, team_id: int | None = None,
                          vs: int | None = None, horizon: str = "ros", kdst_streaming: bool = True,
                          side: dict | None = None, compatibility: dict | None = None) -> dict:
    """Read-only. Raises NotFound / SameTeam for team ids, NotSynced without teams. `side` carries the compared teams'
    bye plans and scouting ({team_id: {"byes": ..., "scouting": ...}}, load_side_inputs); `compatibility` the
    league_compatibility payload (load_compatibility)."""
    if not race.teams:
        raise NotSynced("No fantasy teams synced for the league.")
    if horizon not in HORIZONS:
        raise ValueError(f"Unknown horizon {horizon!r}")
    focus, other = check_teams(race, team_id, vs)
    extras = extras or {}
    state = with_streaming(race.state) if kdst_streaming else race.state
    meta = {t["team_id"]: t for t in race.teams}
    team_ids = sorted(meta)
    window = state.window()
    weeks = horizon_weeks(window, horizon)
    slots, proj = state.slots, state.projections
    label = source_label(state.projection_source, proj)
    groups = groups_of(slots)
    unsupported = list(unsupported_of(slots))
    out = {
        "contract": CONTRACT,
        "league": {k: race.league.get(k) for k in ("league_id", "season", "name", "current_week",
                                                   "last_regular_season_week", "final_scoring_period")},
        "week": state.start_week, "focus_team_id": focus, "compare_team_id": other,
        "horizon": {"key": horizon, "label": HORIZONS[horizon], "weeks": weeks,
                    "options": [{"key": k, "label": v} for k, v in HORIZONS.items()],
                    "uncertain": bool((state.start_week_reason or {}).get("uncertain")),
                    "warning": (state.start_week_reason or {}).get("warning")},
        "kdst_streaming": kdst_streaming, "projection_source": label, "units": dict(UNITS), "method": list(METHOD),
        "freshness": {"league_synced_at": _iso(extras.get("league_synced_at")),
                      "projections_synced_at": _iso(extras.get("projections_synced_at"))},
        "compatibility": _compat_entry(compatibility),
    }
    if not weeks:
        return {**out, "empty": True, "groups": [], "teams": [], "compare": None, "caveats": [],
                "message": "No remaining fantasy weeks: every week has kicked off, so there is no lineup to compare."}

    projections = {t: (project_team(state.rosters[t], slots, window, proj) if state.rosters.get(t) else None)
                   for t in team_ids}
    names = {p.espn_player_id: p.name for ps in state.rosters.values() for p in ps}
    why = unknown_reasons(projections, state.rosters, slots, weeks, label)
    blanket = None
    unverified = bool(unsupported) and not separable(unsupported)
    if unverified:
        blanket = (f"Unverified: {slots_left_out(unsupported)} and may take players these groups would start, so no "
                   "group's lineup is established.")
    elif label == NO_PROJECTIONS:
        blanket = "This league has no projections for these weeks, so no lineup can be projected."
    market = replacement_market(state.free_agents, slots, window, proj, top_n=1, projection_source=label)

    cells: dict[int, dict[str, dict]] = {t: {} for t in team_ids}
    group_rows = []
    for g in groups:
        ok = {t: p for t, p in projections.items() if p is not None and blanket is None and g not in why.get(t, {})}
        strength = position_strength({t: ok.get(t) for t in team_ids}, slots, window, {"h": weeks})["h"][g] \
            if ok else {"ranks": {}, "average": {}}
        count = slots.count(g)
        repl, repl_missing = _replacement(market, g, weeks)
        per_slot = {t: v / count for t, v in strength["average"].items() if v is not None}
        scores = scale({t: round(v, 6) for t, v in per_slot.items()})
        med = median(per_slot.values()) if per_slot else None
        group_rows.append({
            "slot": g, "count": count, "eligible_positions": list(eligible_positions(g)),
            "streamable": g in STREAM_POSITIONS, "teams_ranked": len(per_slot),
            "replacement_per_slot": r(repl),
            "replacement_note": None if repl is not None else (
                f"No free agent at {g} has a projection in {_weeks_text(repl_missing)}, so the replacement level is "
                "unknown and points above replacement are not shown." if repl_missing else None),
            "league_median_per_slot": r(med), "league_best_per_slot": r(max(per_slot.values())) if per_slot else None,
            "unknown_teams": sorted(t for t in team_ids if t not in per_slot),
        })
        for t in team_ids:
            if t in per_slot:
                v = per_slot[t]
                cells[t][g] = {"status": RANKED, "points": strength["average"][t], "per_slot": r(v),
                               "above_replacement": r(v - repl) if repl is not None else None,
                               "vs_median": r(v - med) if med is not None else None, "score": scores[t],
                               "rank": strength["ranks"][t], "of": len(per_slot), "reason": None}
            else:
                reason = blanket or why.get(t, {}).get(g) or "not computed"
                cells[t][g] = {"status": UNVERIFIED if unverified else UNKNOWN, "points": None,
                               "per_slot": None, "above_replacement": None, "vs_median": None, "score": None,
                               "rank": None, "of": len(per_slot), "reason": reason}

    power = _power(race, extras, focus, kdst_streaming)
    rows = []
    for t in team_ids:
        m = meta[t]
        ranked = [(g, c) for g, c in cells[t].items() if c["status"] == RANKED and g not in STREAM_POSITIONS]
        # league-relative score first (rank alone reads "3rd of 4" like "3rd of 12"), then rank, then lineup order
        order = sorted(ranked, key=lambda gc: (-(gc[1]["score"] or 0), gc[1]["rank"], groups.index(gc[0])))
        legality = roster_legality(state, t)
        caveats = []
        if projections[t] is None:
            caveats.append({"code": "no_roster", "text": "No roster snapshot for this team, so nothing is projected."})
        n_unknown = sum(1 for c in cells[t].values() if c["status"] != RANKED)
        if projections[t] is not None and n_unknown and not blanket:
            caveats.append({"code": "unknown_positions",
                            "text": f"{_plural(n_unknown, 'slot group')} unknown: a rostered player has no projection."})
        if legality:
            caveats.append({"code": "roster_unverified", "text": legality["reason"]})
        pw = power.get(t) or {}
        rows.append({
            "team_id": t, "name": m["name"], "abbrev": m.get("abbrev"), "is_mine": bool(m["is_mine"]),
            "record": {"wins": m.get("wins"), "losses": m.get("losses"), "ties": m.get("ties") or 0},
            "power": {"rank": pw.get("rank"), "score": pw.get("power_score"),
                      "lineup_strength": pw.get("strength"), "unranked_reason": pw.get("unranked_reason")},
            "lineup": _lineup_total(projections[t], weeks),
            "positions": cells[t],
            # two of each when there are four or more ranked groups, one each with two or three, none with fewer
            "strongest": [g for g, _ in order[:2 if len(order) >= 4 else 1 if len(order) >= 2 else 0]],
            "weakest": [g for g, _ in order[::-1][:2 if len(order) >= 4 else 1 if len(order) >= 2 else 0]],
            "caveats": caveats,
        })
    rows.sort(key=lambda x: (x["power"]["rank"] is None, x["power"]["rank"] or 0, x["team_id"]))

    caveats = _league_caveats(unsupported, unverified, blanket, group_rows, cells, label, out["horizon"])
    compare = _compare(focus, other, projections, cells, state, market, weeks, names, side or {}, meta) \
        if other is not None else None
    return {**out, "empty": False, "groups": group_rows, "teams": rows, "compare": compare, "caveats": caveats,
            "message": None}


def _iso(t):
    return t.isoformat() if hasattr(t, "isoformat") else t


def _power(race: RaceData, extras: dict, focus: int | None, kdst_streaming: bool) -> dict[int, dict]:
    """Power rankings (rest of season, no previous edition) per team: rank, power score, lineup strength."""
    res = build_power_rankings(race, None, extras, team_id=focus, kdst_streaming=kdst_streaming)
    return {x["team_id"]: {"rank": x["rank"], "power_score": x["power_score"],
                           "strength": x["components"]["strength"]["value"], "unranked_reason": x["unranked_reason"]}
            for x in res.get("teams") or []}


def _compat_entry(compat: dict | None) -> dict | None:
    """The compatibility diagnostic's Power rankings entry (the same lineup engine), dimension by dimension."""
    if not compat:
        return None
    tool = next((x for x in compat.get("tools") or [] if x.get("tool") == POWER_TOOL), None)
    if tool is None:
        return None
    dims = {k: {f: tool[k].get(f) for f in ("status", "reason", "missing_input", "action")}
            for k in ("import", "roster", "scoring", "feature") if isinstance(tool.get(k), dict)}
    worst = next((s for s in ("unsupported", "unverified", "partial") if any(d["status"] == s for d in dims.values())),
                 "handled")
    return {"contract": compat.get("contract"), "tool": POWER_TOOL, "label": tool.get("label"), "status": worst,
            "dimensions": dims}


def _league_caveats(unsupported: list[str], unverified: bool, blanket: str | None, groups: list[dict], cells: dict,
                    label: str, horizon: dict) -> list[dict]:
    out = []
    if unverified:
        out.append({"code": "unsupported_slots", "status": "unverified", "text": blanket})
    elif unsupported:
        out.append({"code": "unsupported_slots", "status": "partial",
                    "text": f"Partial totals: {slots_left_out(unsupported)}. The slot groups shown are the ones the "
                            "engines fill and are exact; lineup totals leave the others out."})
    if label == NO_PROJECTIONS and not unverified:
        out.append({"code": "no_projections", "status": "unknown", "text": blanket})
    unknown = sum(1 for per in cells.values() for c in per.values() if c["status"] == UNKNOWN)
    if unknown and not blanket:
        out.append({"code": "unknown_cells", "status": "unknown",
                    "text": f"{_plural(unknown, 'team position')} unknown because a rostered player has no projection; "
                            "those are left out of the ranks, never ranked last."})
    for g in groups:
        if g["replacement_note"] and g["teams_ranked"]:      # a group nobody is ranked in has nothing to adjust
            out.append({"code": "replacement_unknown", "status": "partial", "text": g["replacement_note"]})
    if horizon.get("uncertain"):
        out.append({"code": "week_uncertain", "status": "partial",
                    "text": horizon.get("warning") or "The first remaining week could not be confirmed."})
    return out


# ------------------------------------------------------------------ compare --

def _bench(base: dict | None, market: dict, slots: list[str], weeks: list[int]) -> dict[str, dict]:
    if base is None:
        return {}
    res = replacement_options(base, market, slots, weeks)
    return {x["position"]: x for x in res["positions"]}


def _depth(roster: list[RosterPlayer], group: str, slots: list[str]) -> int:
    elig = eligible_positions(group)
    return sum(1 for p in roster if not p.reserved and p.position in elig and lineup_relevant(p.position, slots))


def _byes(plan: dict | None, weeks: list[int]) -> dict:
    """A team's bye planner, cut to the horizon: the weeks with a conflict (short, unconfirmed or two or more byes)."""
    if not plan or plan.get("error"):
        return {"available": False, "reason": (plan or {}).get("error") or "The bye planner is unavailable.",
                "weeks": [], "conflict_weeks": []}
    names = {p["espn_player_id"]: p["name"] for p in plan.get("players") or []}
    rows = []
    for w in plan.get("weeks") or []:
        if w["week"] not in weeks or not w["conflict"]:
            continue
        rows.append({"week": w["week"], "status": w["status"], "status_text": w["status_text"],
                     "short_slots": w["short_slots"], "unconfirmed_slots": w["unconfirmed_slots"],
                     "byes": [names.get(p) or f"Player #{p}" for p in w["byes"]],
                     "starters_on_bye": [names.get(p) or f"Player #{p}" for p in w["starters_on_bye"]]})
    sched = plan.get("schedule") or {}
    missing = [w for w in sched.get("weeks_missing") or [] if w in weeks]
    return {"available": True, "reason": None if sched.get("available", True) else "No NFL schedule is loaded.",
            "weeks": rows, "conflict_weeks": [x["week"] for x in rows], "schedule_missing_weeks": missing}


def _needs(scouting: dict | None) -> dict:
    if not scouting:
        return {"available": False, "positions": []}
    comp = {p["position"]: p for p in (scouting.get("current_roster") or {}).get("positions") or []}
    needs = {n["position"]: n for n in scouting.get("current_needs") or []}
    out = []
    for pos in [p for p in comp if p != "IR"]:
        n, c = needs.get(pos) or {}, comp[pos]
        out.append({"position": pos, "rostered": c.get("count"), "league_average": r(c.get("league_average")),
                    "comparison": c.get("league_comparison"), "need_score": r(n.get("need_score")),
                    "starters_required": n.get("starters_required"), "healthy": n.get("healthy")})
    return {"available": True, "positions": out,
            "scoring_period": (scouting.get("current_roster") or {}).get("scoring_period"),
            "note": "Stored need scores and the current roster snapshot from Manager scouting; current state, not a "
                    "preference or a forecast."}


def _compare(a: int, b: int, projections: dict, cells: dict, state, market: dict, weeks: list[int],
             names: dict[int, str], side: dict, meta: dict) -> dict:
    slots = state.slots
    bench = {t: _bench(projections[t], market, slots, weeks) for t in (a, b)}
    rows = []
    for g in groups_of(slots):
        ca, cb = cells[a][g], cells[b][g]
        both = ca["status"] == RANKED and cb["status"] == RANKED
        diff = r(ca["per_slot"] - cb["per_slot"]) if both else None
        leader = None if diff is None else "even" if abs(diff) < 0.005 else "a" if diff > 0 else "b"

        def one(t: int, c: dict, g: str = g) -> dict:
            p = projections[t]
            bn = bench[t].get(g)
            return {**c, "starters": _starters(p, g, weeks, names) if p is not None else [],
                    "bench": {k: bn[k] for k in ("best_bench", "best_free_agent", "edge", "has_backup")} if bn else None,
                    "depth": _depth(state.rosters.get(t) or [], g, slots)}
        rows.append({"slot": g, "count": slots.count(g), "streamable": g in STREAM_POSITIONS,
                     "a": one(a, ca), "b": one(b, cb), "difference_per_slot": diff, "leader": leader})
    byes = {t: _byes((side.get(t) or {}).get("byes"), weeks) for t in (a, b)}
    shared = sorted(set(byes[a]["conflict_weeks"]) & set(byes[b]["conflict_weeks"]))
    lead = {k: sum(1 for x in rows if x["leader"] == k and not x["streamable"]) for k in ("a", "b", "even")}
    unknown = [x["slot"] for x in rows if x["leader"] is None]
    caveats = []
    if unknown:
        caveats.append({"code": "unknown_positions",
                        "text": f"Not compared at {', '.join(unknown)}: at least one side is unknown there."})
    for t in (a, b):
        if not byes[t]["available"]:
            caveats.append({"code": "byes_unavailable", "text": f"{meta[t]['name']}: {byes[t]['reason']}"})
        elif byes[t].get("schedule_missing_weeks"):
            caveats.append({"code": "schedule_missing",
                            "text": f"No NFL schedule for {_weeks_text(byes[t]['schedule_missing_weeks'])}: byes there "
                                    "are not established."})
    return {
        "a": {"team_id": a, "name": meta[a]["name"], "is_mine": bool(meta[a]["is_mine"]),
              "lineup": _lineup_total(projections[a], weeks), "byes": byes[a],
              "needs": _needs((side.get(a) or {}).get("scouting"))},
        "b": {"team_id": b, "name": meta[b]["name"], "is_mine": bool(meta[b]["is_mine"]),
              "lineup": _lineup_total(projections[b], weeks), "byes": byes[b],
              "needs": _needs((side.get(b) or {}).get("scouting"))},
        "positions": rows, "leads": lead, "shared_conflict_weeks": shared, "caveats": caveats,
        "rule": "Each side's starters, bench and bye weeks over the same horizon and the same lineup rules; a position "
                "where either side is unknown is not compared.",
    }


# ------------------------------------------------------------------ loaders --

def load_side_inputs(con, team_ids: list[int]) -> dict:
    """Bye plans (bye_planner) and manager scouting for the compared teams; a failure is recorded, never raised."""
    from .bye_planner import build_bye_planner
    from .manager_scouting import build_manager_scouting
    out = {}
    for t in team_ids:
        try:
            byes = build_bye_planner(con, team_id=t)
        except LookupError as err:            # NotSynced / NotFound from the schedule engine
            byes = {"error": str(err)}
        try:
            scouting = build_manager_scouting(con, t)
        except LookupError:
            scouting = None
        out[t] = {"byes": byes, "scouting": scouting}
    return out


def load_compatibility(con) -> dict | None:
    from .league_compatibility import build_compatibility, load_facts
    from .legacy_league import latest_league
    lg = latest_league(con)
    if lg is None:
        return None
    return build_compatibility(lg, load_facts(con, lg))
