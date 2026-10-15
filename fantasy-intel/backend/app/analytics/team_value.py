"""My Team Value dashboard (read-only): how strong a roster is against the league, which players
really move its starting lineups, where projection sits unused on the bench, where an upgrade helps
most, and how that interacts with the Playoff Race Lab. See docs/v2_team_value.md.

Everything forward-looking reuses the canonical engine unchanged:
  * ESPN weekly projections and current rosters (trade_data.load_league_state, via the race data);
  * the exact weekly optimizer (team_lineups.project_team) and roster_marginal_value (with its
    ir_activation_sensitivity for current-IR players);
  * replacement_market and best_fa_addition for free agents;
  * TradeSearch / detail (trade discovery, checked against the manual evaluator) for trades;
  * playoff_race.build_playoff_race for standings, remaining matchups and margins.
Nothing here adds a weight, a confidence, an injury-return estimate or an acceptance estimate.

Rules kept from the engine: missing is not zero (an incomplete week has no canonical total); bench
points never count; current-IR players are inactive and their upside is shown separately; regular
season and playoffs are separate windows. Two additions of this module:
  * League ranks compare teams only on comparable weeks: weeks in which EVERY ranked team's lineup
    projection is complete. A team without a roster snapshot is not ranked (never a 0).
  * Marginal values are per player and contextual. They are never summed into a team value.
"""

from __future__ import annotations

import json
from datetime import datetime

from ..db.connection import fetch_dicts, table_exists
from .lineup import FLEX_ELIGIBILITY, eligible
from .playoff_race import RaceData, build_playoff_race, load_race_data
from .team_lineups import (RosterPlayer, best_fa_addition, compare, project_team, projection, r,
                           replacement_market, roster_marginal_value, starters, without)
from .trade_discovery import TradeSearch, detail
from .trades import TradeError

PHASES = ("all", "regular", "playoff")
DROP_CANDIDATES = 3          # add/drop search: the active players whose removal costs the least
TRADE_PREVIEW = 3            # rows per trade preview list (1-for-1 only, for speed)
WAIVER_ROWS = 5              # stored waiver recommendations re-checked on the canonical engine

RANK_RULE = ("Teams are ranked on comparable weeks only: remaining weeks in which every ranked team's optimal-lineup "
             "projection is complete. A week where any team is missing a projection is left out for everyone, so no "
             "complete forecast is compared with a partial one. Teams without a roster snapshot are not ranked.")
MARGINAL_NOTE = ("A player's marginal value is this roster's optimal starting-lineup total with him minus without him, "
                 "week by week (bench weeks add 0). It depends on the rest of the roster, so marginal values are not "
                 "additive and are not a trade currency: never add them up.")
DEPTH_NOTE = ("Depth is measured by explicit removal scenarios: the roster without one player, re-optimized every week. "
              "This shows who covers him today; it is not a prediction of injuries.")
CONSOLIDATION_NOTE = ("A player with large unused projection is not automatically a sell: his projected points may be "
                      "worth much more in another team's lineup. Trade discovery evaluates both sides exactly.")


class NotFound(LookupError):
    """Unknown team, or no team marked as yours."""


# ------------------------------------------------------------------- inputs --

def load_extras(con, race: RaceData) -> dict:
    """Read-only side inputs: bye weeks, stored waiver recommendations, freshness timestamps."""
    st, season = race.state, race.state.season
    ids = sorted({p.espn_player_id for ps in st.rosters.values() for p in ps})
    byes = {}
    if ids and table_exists(con, "player_ros_projections"):
        ph = ", ".join("?" * len(ids))
        for row in fetch_dicts(con, f"SELECT espn_player_id, bye_weeks FROM player_ros_projections WHERE season = ? "
                                    f"AND espn_player_id IN ({ph})", [season, *ids]):
            b = row["bye_weeks"]
            b = json.loads(b) if isinstance(b, str) else b
            if b is not None:
                byes[row["espn_player_id"]] = [int(x) for x in b]
    waivers = fetch_dicts(con, """SELECT espn_player_id, position, recommendation, waiver_score, suggested_drop_id
                                  FROM waiver_recommendations WHERE league_id = ? AND season = ?
                                    AND recommendation IN ('CLAIM', 'WAIT')
                                  ORDER BY waiver_score DESC NULLS LAST, espn_player_id LIMIT ?""",
                          [st.league_id, season, WAIVER_ROWS]) if table_exists(con, "waiver_recommendations") else []
    league_synced = fetch_dicts(con, "SELECT synced_at FROM leagues WHERE league_id = ? AND season = ?",
                                [st.league_id, season])
    proj_synced = fetch_dicts(con, """SELECT max(synced_at) AS t FROM espn_player_weekly_points
                                      WHERE season = ? AND projected_points IS NOT NULL""", [season]) \
        if table_exists(con, "espn_player_weekly_points") else []
    return {"bye_weeks": byes, "waivers": waivers,
            "league_synced_at": league_synced[0]["synced_at"] if league_synced else None,
            "projections_synced_at": proj_synced[0]["t"] if proj_synced else None}


def load_team_value_inputs(con, now: datetime | None = None) -> tuple[RaceData, dict]:
    race = load_race_data(con, now)
    return race, load_extras(con, race)


# ------------------------------------------------------------------ helpers --

def _iso(t):
    return t.isoformat() if hasattr(t, "isoformat") else t


def _phase_weeks(window, phase: str) -> list[int]:
    if phase == "all":
        return list(window.weeks)
    return [w for w in window.weeks if window.phase(w) == phase]


def _sum(weeks: list[dict]) -> float | None:
    return r(sum(w["projected_lineup_points"] for w in weeks)) if weeks else None


def _rank(values: dict[int, float]) -> dict[int, int]:
    ordered = sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))
    out, prev, rank = {}, None, 0
    for i, (tid, v) in enumerate(ordered, start=1):
        if v != prev:
            rank, prev = i, v
        out[tid] = rank
    return out


def _slot_groups(slots: list[str]) -> list[str]:
    seen = []
    for s in slots:
        if s not in seen:
            seen.append(s)
    return seen


def raw_projection(proj, pid: int, weeks: list[int]) -> dict:
    """A player's own ESPN projection over weeks: total only when every week is projected."""
    vals = {w: projection(proj, pid, w) for w in weeks}
    known = [v for v in vals.values() if v is not None]
    missing = [w for w, v in vals.items() if v is None]
    return {"points": r(sum(known)) if not missing else None, "known_points": r(sum(known)),
            "missing_weeks": missing}


# -------------------------------------------------------------- league ranks --

def league_ranks(teams: dict[int, dict | None], window, team_names: dict[int, str]) -> dict:
    """Totals and ranks per phase on comparable weeks (every ranked team complete)."""
    ranked = {t: p for t, p in teams.items() if p is not None}
    incomplete = {w: sorted(t for t, p in ranked.items()
                            if not next(x for x in p["weeks"] if x["week"] == w)["projection_complete"])
                  for w in window.weeks}
    comparable = [w for w in window.weeks if not incomplete[w]]
    out = {"rule": RANK_RULE, "teams_ranked": sorted(ranked), "teams_unranked": sorted(t for t, p in teams.items()
                                                                                     if p is None),
           "excluded_weeks": [{"week": w, "incomplete_teams": [team_names.get(t, str(t)) for t in ts]}
                              for w, ts in incomplete.items() if ts],
           "phases": {}}
    for phase in PHASES:
        weeks = [w for w in _phase_weeks(window, phase) if w in comparable]
        totals = {t: sum(x["projected_lineup_points"] for x in p["weeks"] if x["week"] in weeks)
                  for t, p in ranked.items()} if weeks else {}
        ranks = _rank({t: round(v, 6) for t, v in totals.items()}) if weeks else {}
        out["phases"][phase] = {
            "weeks": weeks, "phase_weeks": _phase_weeks(window, phase),
            "teams": [{"team_id": t, "name": team_names.get(t), "total": r(totals[t]),
                       "average_weekly": r(totals[t] / len(weeks)), "rank": ranks[t]}
                      for t in sorted(totals, key=lambda t: (ranks[t], t))] if weeks else [],
        }
    return out


def position_strength(teams: dict[int, dict | None], slots: list[str], window, comparable: dict[str, list[int]]) -> dict:
    """Average projected points each team's optimal lineup gets from each slot group per comparable
    week, and the team's rank in that group (rank 1 = most points)."""
    groups = _slot_groups(slots)
    out = {}
    for phase, weeks in comparable.items():
        per: dict[str, dict[int, float]] = {g: {} for g in groups}
        for t, p in teams.items():
            if p is None or not weeks:
                continue
            for g in groups:
                pts = sum(a["value"] if "value" in a else a["points"] for w in p["weeks"] if w["week"] in weeks
                          for a in w["assignment"] if a["slot"] == g and a["player_id"] is not None)
                per[g][t] = pts / len(weeks)
        out[phase] = {g: {"ranks": _rank({t: round(v, 6) for t, v in per[g].items()}),
                          "average": {t: r(v) for t, v in per[g].items()}} for g in groups}
    return out


# ------------------------------------------------------------- bench reasons --

def bench_reason(p: RosterPlayer, week: dict, proj, byes: list[int] | None, names: dict[int, str]) -> dict:
    """Why a rostered player is not in this week's optimal lineup."""
    w = week["week"]
    v = projection(proj, p.espn_player_id, w)
    if p.on_ir:
        return {"code": "ir", "text": "in an IR slot: inactive under the canonical model"}
    if v is None:
        return {"code": "no_projection", "text": "no ESPN projection this week"}
    if v == 0:
        bye = byes is not None and w in byes
        return {"code": "bye" if bye else "zero_projection",
                "text": "NFL bye (projected 0)" if bye else "ESPN projects 0"}
    held = [a for a in week["assignment"] if a["player_id"] is not None and eligible(a["slot"], p.position)]
    fixed = [a for a in held if a["slot"] not in FLEX_ELIGIBILITY]
    flex = [a for a in held if a["slot"] in FLEX_ELIGIBILITY]
    def who(rows):
        return ", ".join(f"{names.get(a['player_id']) or a['player_id']} ({a['points']:.1f})"
                         for a in sorted(rows, key=lambda a: -(a["points"] or 0)))
    if flex:
        return {"code": "flex_competition", "text": f"{p.position} slots held by {who(fixed) or 'nobody'}; "
                                                    f"FLEX taken by {who(flex)} (he projects {v:.1f})"}
    return {"code": "outscored", "text": f"{p.position} slots held by {who(fixed)} (he projects {v:.1f})"}


# ---------------------------------------------------------- matchup effects --

def matchup_effects(race_res: dict, tid: int, my_after: dict, opp_after: dict[int, dict] | None = None) -> list[dict]:
    """Re-project my remaining upcoming matchups with a changed roster (and a changed trade partner),
    same rules: each side's optimized weekly totals over the matchup's weeks. Deterministic scenario."""
    opp_after = opp_after or {}
    by_week = {w["week"]: w for w in my_after["weeks"]}
    out = []
    for m in race_res["matchups"]["regular"]:
        if m["status"] != "upcoming" or tid not in (m["home_team_id"], m["away_team_id"]):
            continue
        home = m["home_team_id"] == tid
        mine_side, opp_side = (m["home_projection"], m["away_projection"]) if home else \
            (m["away_projection"], m["home_projection"])
        opp = m["away_team_id"] if home else m["home_team_id"]
        new_weeks = [by_week.get(w) for w in m["weeks"]]
        mine_new = r(sum(w["projected_lineup_points"] for w in new_weeks)) \
            if all(w is not None and w["projection_complete"] for w in new_weeks) else None
        if opp in opp_after:
            ow = {w["week"]: w for w in opp_after[opp]["weeks"]}
            ows = [ow.get(w) for w in m["weeks"]]
            opp_new = r(sum(w["projected_lineup_points"] for w in ows)) \
                if all(w is not None and w["projection_complete"] for w in ows) else None
        else:
            opp_new = opp_side["projected_points"]
        before = None if mine_side["projected_points"] is None or opp_side["projected_points"] is None \
            else r(mine_side["projected_points"] - opp_side["projected_points"])
        after = None if mine_new is None or opp_new is None else r(mine_new - opp_new)
        sign = lambda x: None if x is None else (1 if x > 0 else -1 if x < 0 else 0)   # noqa: E731
        out.append({"matchup_id": m["matchup_id"], "matchup_period": m["matchup_period"], "weeks": m["weeks"],
                    "opponent_id": opp, "opponent_name": m["away_name"] if home else m["home_name"],
                    "margin_before": before, "margin_after": after,
                    "projected_result_before": {1: "win", -1: "loss", 0: "even", None: None}[sign(before)],
                    "projected_result_after": {1: "win", -1: "loss", 0: "even", None: None}[sign(after)],
                    # flips: the projected winner changes sides; changes: any change, including to/from even.
                    "flips": sign(before) is not None and sign(after) is not None and sign(before) * sign(after) == -1,
                    "changes_projected_result": sign(before) is not None and sign(after) is not None
                    and sign(before) != sign(after)})
    return out


# -------------------------------------------------------------------- build --

def build_team_value(race: RaceData, extras: dict, team_id: int | None = None, trades: bool = True) -> dict:
    """Read-only. Raises NotFound."""
    state = race.state
    teams_meta = {t["team_id"]: t for t in race.teams}
    tid = team_id
    if tid is None:
        mine = [t for t in race.teams if t["is_mine"]]
        if not mine:
            raise NotFound("No team is marked as yours (is_mine); pass team_id.")
        tid = mine[0]["team_id"]
    if tid not in teams_meta:
        raise NotFound(f"Unknown team {tid}")
    window = state.window()
    roster = state.rosters.get(tid) or []
    names = {p.espn_player_id: p.name for ps in state.rosters.values() for p in ps}
    names.update({p.espn_player_id: p.name for p in state.free_agents})
    race_res = build_playoff_race(race, team_id=tid)
    me_race = next(t for t in race_res["teams"] if t["team_id"] == tid)
    out = {"team": {k: teams_meta[tid][k] for k in ("team_id", "name", "abbrev", "is_mine")},
           "teams": [{"team_id": t["team_id"], "name": t["name"], "is_mine": bool(t["is_mine"])} for t in race.teams],
           "league": race_res["league"],
           "window": {"weeks": list(window.weeks), "first_actionable_week": state.start_week,
                      "last_regular_season_week": window.last_regular, "final_week": state.final_week,
                      "reason": (state.start_week_reason or {}).get("reason"),
                      "uncertain": bool((state.start_week_reason or {}).get("uncertain")),
                      "warning": (state.start_week_reason or {}).get("warning")},
           "freshness": {"league_synced_at": _iso(extras.get("league_synced_at")),
                         "projections_synced_at": _iso(extras.get("projections_synced_at"))},
           "standing": {"record": me_race["record"], "espn_seed": me_race["espn_seed"],
                        "points_for": me_race["points_for"], "points_against": me_race["points_against"],
                        "current": me_race["current"], "projected": me_race["projected"],
                        "playoff_spots": race_res["league"]["playoff_teams"]},
           "notes": [MARGINAL_NOTE, DEPTH_NOTE, CONSOLIDATION_NOTE,
                     "Projections are ESPN's weekly projections, unmodified; lineups are the exact weekly optimum with "
                     "the league's slots. A projected 0 is real; a missing projection is unknown and never 0."]}
    if not window.weeks:
        out.update(empty=True, message="No remaining fantasy scoring periods: every week has kicked off.")
        return out
    if not roster:
        out.update(empty=True, message="This team has no roster snapshot, so nothing can be projected.")
        return out
    slots, proj = state.slots, state.projections

    # A. overview -------------------------------------------------------------
    projections = {t: (project_team(state.rosters[t], slots, window, proj) if state.rosters.get(t) else None)
                   for t in sorted(teams_meta)}
    base = projections[tid]
    s = base["summary"]
    ranks = league_ranks(projections, window, {t: m["name"] for t, m in teams_meta.items()})
    my_rank = {ph: next((x for x in v["teams"] if x["team_id"] == tid), None) for ph, v in ranks["phases"].items()}
    missing_names = [names.get(p) or str(p) for p in s["missing_players"]]
    phase_split = {}
    for ph in PHASES:
        ws = [w for w in base["weeks"] if w["week"] in _phase_weeks(window, ph)]
        complete = all(w["projection_complete"] for w in ws)
        phase_split[ph] = {"weeks": [w["week"] for w in ws],
                           "projected_points": _sum(ws) if complete and ws else None,
                           "known_points": r(sum(w["known_lineup_points"] for w in ws)) if ws else None,
                           "average_weekly": r(sum(w["projected_lineup_points"] for w in ws) / len(ws))
                           if complete and ws else None,
                           "incomplete_weeks": [w["week"] for w in ws if not w["projection_complete"]],
                           "league_rank": my_rank[ph]}
    out["overview"] = {"summary": s, "phases": phase_split, "missing_players": missing_names,
                       "weekly": [{"week": w["week"], "phase": w["phase"], "projected_lineup_points": w["projected_lineup_points"],
                                   "known_lineup_points": w["known_lineup_points"], "projection_complete": w["projection_complete"],
                                   "league_median": _median([p["weeks"][i]["projected_lineup_points"] for p in projections.values()
                                                             if p is not None and p["weeks"][i]["projection_complete"]])
                                   if all(p is None or p["weeks"][i]["projection_complete"] for p in projections.values()) else None}
                                  for i, w in enumerate(base["weeks"])],
                       "league_ranks": ranks}

    # B. player marginal value -----------------------------------------------
    open_spots = max(0, state.active_capacity - sum(1 for p in roster if not p.on_ir))
    byes = extras.get("bye_weeks", {})
    players, rests = [], {}
    for p in sorted(roster, key=lambda p: p.espn_player_id):
        mv = roster_marginal_value(roster, p.espn_player_id, slots, window, proj, base, open_spots)
        started = set(mv["weeks_started"])
        raw = {ph: raw_projection(proj, p.espn_player_id, _phase_weeks(window, ph)) for ph in PHASES}
        bench = []
        for w in base["weeks"]:
            if w["week"] in started:
                continue
            bench.append({"week": w["week"], "projection": projection(proj, p.espn_player_id, w["week"]),
                          **bench_reason(p, w, proj, byes.get(p.espn_player_id), names)})
        unused = [b["projection"] for b in bench if b["projection"] and not p.on_ir]
        if started:
            rests[p.espn_player_id] = project_team(without(roster, [p.espn_player_id]), slots, window, proj)
        players.append({
            "espn_player_id": p.espn_player_id, "name": p.name, "position": p.position, "pro_team": p.pro_team,
            "injury_status": p.injury_status, "on_ir": p.on_ir,
            "espn_projected": raw,
            "marginal": {"all": mv["roster_marginal_lineup_points"], "regular": mv["regular_season_marginal_lineup_points"],
                         "playoff": mv["playoff_marginal_lineup_points"], "known_all": mv["known_marginal_lineup_points"]},
            "weeks_started": mv["weeks_started"], "weeks_benched": [b["week"] for b in bench],
            "weeks_expected": mv["weeks_expected"], "projection_complete": mv["projection_complete"],
            "weekly": mv["weeks"], "bench": bench,
            "bench_reason_counts": _counts(bench),
            "unused_projected_points": r(sum(unused)) if unused else 0.0,
            "started_points": r(sum(projection(proj, p.espn_player_id, w) or 0 for w in started)),
            "ir_activation_sensitivity": mv.get("ir_activation_sensitivity"),
        })
    players.sort(key=lambda x: (-(x["marginal"]["known_all"] or 0), x["espn_player_id"]))
    by_pid = {x["espn_player_id"]: x for x in players}

    # C. positional strength & depth -----------------------------------------
    comparable = {ph: v["weeks"] for ph, v in ranks["phases"].items()}
    strength = position_strength(projections, slots, window, comparable)
    groups = _slot_groups(slots)
    pos_rows = []
    for g in groups:
        occupants = sorted({a["player_id"] for w in base["weeks"] for a in w["assignment"]
                            if a["slot"] == g and a["player_id"] is not None})
        removal = []
        for pid in occupants:
            rest = rests.get(pid)
            fill = {}
            if rest is not None:
                for bw, rw in zip(base["weeks"], rest["weeks"]):
                    for q in starters(rw) - starters(bw):
                        fill.setdefault(q, []).append(bw["week"])
            pm = by_pid[pid]
            loss = pm["marginal"]["all"]
            removal.append({"espn_player_id": pid, "name": pm["name"], "weeks_in_slot": sorted(
                w["week"] for w in base["weeks"] for a in w["assignment"] if a["slot"] == g and a["player_id"] == pid),
                "lineup_points_lost": loss, "known_lineup_points_lost": pm["marginal"]["known_all"],
                "lineup_points_lost_by_phase": {"all": loss, "regular": pm["marginal"]["regular"],
                                                "playoff": pm["marginal"]["playoff"]},
                "own_started_points": pm["started_points"],
                "recovered_by_roster": None if loss is None else r(pm["started_points"] - loss),
                "fill_ins": [{"espn_player_id": q, "name": names.get(q), "weeks": ws} for q, ws in sorted(fill.items())]})
        removal.sort(key=lambda x: (-(x["known_lineup_points_lost"] or 0), x["espn_player_id"]))
        pos_rows.append({"slot": g, "count": slots.count(g),
                         "strength": {ph: {"average": strength[ph][g]["average"].get(tid),
                                           "rank": strength[ph][g]["ranks"].get(tid),
                                           "teams_ranked": len(strength[ph][g]["ranks"]),
                                           "league_best": max(strength[ph][g]["average"].values(), default=None)}
                                      for ph in PHASES},
                         "removal_scenarios": removal,
                         "largest_single_loss": removal[0] if removal else None})
    coverage = {}
    for row in pos_rows:
        for rem in row["removal_scenarios"]:
            for f in rem["fill_ins"]:
                c = coverage.setdefault(f["espn_player_id"], {"espn_player_id": f["espn_player_id"], "name": f["name"],
                                                              "covers": []})
                c["covers"].append({"for": rem["name"], "slot": row["slot"], "weeks": f["weeks"]})
    ranked_groups = [x for x in pos_rows if x["strength"]["all"]["rank"] is not None]
    out["positions"] = {
        "slots": pos_rows,
        "strongest": [x["slot"] for x in sorted(ranked_groups, key=lambda x: (x["strength"]["all"]["rank"], x["slot"]))][:2],
        "weakest": [x["slot"] for x in sorted(ranked_groups, key=lambda x: (-x["strength"]["all"]["rank"], x["slot"]))][:2],
        "bench_coverage": sorted(coverage.values(), key=lambda c: (-len(c["covers"]), c["espn_player_id"])),
        "note": DEPTH_NOTE,
    }

    # D. bench value & consolidation -----------------------------------------
    cands = [x for x in players if not x["on_ir"] and x["unused_projected_points"] > 0]
    cands.sort(key=lambda x: (-x["unused_projected_points"], x["espn_player_id"]))
    out["bench"] = {"players": [{k: x[k] for k in ("espn_player_id", "name", "position", "unused_projected_points",
                                                   "started_points", "weeks_started", "weeks_benched",
                                                   "bench_reason_counts")} | {"marginal": x["marginal"]["all"],
                                                                              "known_marginal": x["marginal"]["known_all"],
                                                                              "espn_projected": x["espn_projected"]["all"]}
                                for x in cands],
                    "consolidation_candidates": [x["espn_player_id"] for x in cands[:5]],
                    "ir_players": [x["espn_player_id"] for x in players if x["on_ir"]],
                    "note": CONSOLIDATION_NOTE}

    # E. upgrades --------------------------------------------------------------
    market = replacement_market(state.free_agents, slots, window, proj, top_n=1)
    gaps = []
    for bw, mw in zip(base["weeks"], market["weeks"]):
        if not bw["projection_complete"]:
            continue
        for g in groups:
            held = [a for a in bw["assignment"] if a["slot"] == g]
            weakest = min(held, key=lambda a: (a["points"] if a["player_id"] is not None else -1))
            best = (mw["positions"].get(g) or {}).get("best_available")
            mine_pts = weakest["points"] if weakest["player_id"] is not None else 0.0
            if best and best["points"] > mine_pts:
                gaps.append({"week": bw["week"], "phase": bw["phase"], "slot": g,
                             "current_player_id": weakest["player_id"], "current_name": names.get(weakest["player_id"]),
                             "current_points": weakest["points"], "best_available": best,
                             "gap": r(best["points"] - mine_pts)})
    gaps.sort(key=lambda x: (-x["gap"], x["week"], x["slot"]))
    free = best_fa_addition(roster, state.free_agents, slots, window, proj)
    fa_by_id = {p.espn_player_id: p for p in state.free_agents}
    free_out = {**{k: free.get(k) for k in ("player", "lineup_points_added", "known_lineup_points_added",
                                            "regular_season_points_added", "playoff_points_added", "weeks_started",
                                            "candidates", "excluded_unprojected", "projection_complete")},
                "open_active_roster_spots": open_spots,
                "realistic": open_spots > 0,
                "label": ("Free addition into an open active roster spot" if open_spots > 0 else
                          "Optimistic: no open active roster spot, so this addition would require a drop (see add/drop)")}
    drops = sorted([x for x in players if not x["on_ir"]],
                   key=lambda x: ((x["marginal"]["known_all"] or 0), (x["espn_projected"]["all"]["known_points"] or 0),
                                  x["espn_player_id"]))[:DROP_CANDIDATES]
    add_drop = []
    for d in drops:
        rest_players = without(roster, [d["espn_player_id"]])
        fa = best_fa_addition(rest_players, state.free_agents, slots, window, proj)
        if fa["player"] is None:
            continue
        after = project_team(rest_players + [fa_by_id[fa["player"]["espn_player_id"]]], slots, window, proj)
        net = compare(base, after, window)
        add_drop.append({"add": fa["player"], "drop": {"espn_player_id": d["espn_player_id"], "name": d["name"],
                                                       "position": d["position"]},
                         "ros_delta": net["ros_delta"], "regular_season_delta": net["regular_season_delta"],
                         "playoff_delta": net["playoff_delta"], "known_ros_delta": net["known_ros_delta"],
                         "projection_complete": net["projection_complete"],
                         "drop_marginal": d["marginal"]["known_all"], "_after": after})
    # Best net gain first; on equal gains, drop the player who contributes less now.
    add_drop.sort(key=lambda x: (-(x["known_ros_delta"] or 0), x["drop_marginal"] or 0, x["drop"]["espn_player_id"],
                                 x["add"]["espn_player_id"]))
    waiver_checks = []
    if teams_meta[tid]["is_mine"]:
        for w in extras.get("waivers", []):
            add = fa_by_id.get(w["espn_player_id"])
            drop = next((p for p in roster if p.espn_player_id == w["suggested_drop_id"]), None)
            row = {"add_id": w["espn_player_id"], "add_name": names.get(w["espn_player_id"]), "position": w["position"],
                   "recommendation": w["recommendation"], "waiver_score": w["waiver_score"],
                   "drop_id": w["suggested_drop_id"], "drop_name": drop.name if drop else None}
            if add is None:
                waiver_checks.append({**row, "status": "no longer a free agent or waiver player"})
                continue
            after_players = (without(roster, [drop.espn_player_id]) if drop else list(roster)) + [add]
            if drop is None and open_spots == 0:
                row["status"] = "optimistic: no open active roster spot and no suggested drop"
            else:
                row["status"] = "add/drop" if drop else "free addition into an open spot"
            net = compare(base, project_team(after_players, slots, window, proj), window)
            waiver_checks.append({**row, "ros_delta": net["ros_delta"], "known_ros_delta": net["known_ros_delta"],
                                  "regular_season_delta": net["regular_season_delta"], "playoff_delta": net["playoff_delta"]})
    trade_rows, trade_note = [], None
    if trades:
        try:
            ts = TradeSearch(state, tid, ("1-for-1",))
            seen = set()
            for sort, cat in (("min_side", "MUTUAL_PROJECTED_IMPROVEMENT"), ("my_gain", None)):
                for row in ts.top(sort, cat, None, TRADE_PREVIEW)["results"]:
                    key = (row["opponent_team_id"], tuple(row["sends"]), tuple(row["receives"]))
                    if key in seen:
                        continue
                    seen.add(key)
                    d = detail(state, row)
                    trade_rows.append({"list": "mutual" if cat else "my_gain", "opponent_team_id": d["opponent_team_id"],
                                       "opponent_name": d["opponent_name"], "sends": d["sends"], "receives": d["receives"],
                                       "send_names": [names.get(x) for x in d["sends"]],
                                       "receive_names": [names.get(x) for x in d["receives"]],
                                       "mine": {k: d["mine"][k] for k in ("ros_delta", "regular_season_delta", "playoff_delta")},
                                       "theirs": {k: d["theirs"][k] for k in ("ros_delta", "regular_season_delta", "playoff_delta")},
                                       "forced_drops": d["forced_drops"], "categories": d.get("categories", []),
                                       "consistent_with_manual_evaluator": d["consistent_with_manual_evaluator"],
                                       "evaluator_link": d["manual_evaluator_link"]})
        except TradeError as err:
            trade_note = str(err)
    out["upgrades"] = {"slot_gaps": gaps[:12], "free_addition": free_out,
                       "add_drop": [{k: v for k, v in x.items() if k != "_after"} for x in add_drop],
                       "waiver_checks": waiver_checks, "trade_preview": trade_rows, "trade_note": trade_note,
                       "trade_preview_rule": "1-for-1 trades only (fast preview); the full search is on Trade discovery. "
                                             "Deltas are the manual evaluator's exact lineup deltas. No acceptance estimate.",
                       "waiver_rule": "Waiver scores are the waiver page's heuristic; the deltas next to them are this "
                                      "roster's exact lineup change for that add (and its suggested drop)."}

    # F. playoff race ------------------------------------------------------------
    best_ad = add_drop[0] if add_drop else None
    effects = {"add_drop": None, "trade": None}
    if best_ad:
        effects["add_drop"] = {"add": best_ad["add"], "drop": best_ad["drop"],
                               "matchups": matchup_effects(race_res, tid, best_ad["_after"])}
    top_trade = next((t for t in trade_rows if t["mine"]["ros_delta"] and t["mine"]["ros_delta"] > 0), None)
    if top_trade:
        opp = top_trade["opponent_team_id"]
        mine_after = [p for p in roster if p.espn_player_id not in top_trade["sends"]] + \
            [p for p in state.rosters[opp] if p.espn_player_id in top_trade["receives"]]
        opp_after = [p for p in state.rosters[opp] if p.espn_player_id not in top_trade["receives"]] + \
            [p for p in roster if p.espn_player_id in top_trade["sends"]]
        forced = top_trade["forced_drops"]
        mine_after = without(mine_after, forced.get("mine", []))
        opp_after = without(opp_after, forced.get("theirs", []))
        effects["trade"] = {"opponent_team_id": opp, "sends": top_trade["sends"], "receives": top_trade["receives"],
                            "matchups": matchup_effects(race_res, tid, project_team(mine_after, slots, window, proj),
                                                        {opp: project_team(opp_after, slots, window, proj)})}
    my_games = [m for m in me_race["remaining"]]
    out["playoff_race"] = {"standing_now": me_race["current"], "standing_projected": me_race["projected"],
                           "cutline": race_res["scenario"]["cutline"], "remaining": my_games,
                           "close_games": [g for g in my_games if g["close"]],
                           "leverage": race_res["scenario"]["leverage"].get(str(tid), []),
                           "upgrade_effects": effects,
                           "rule": "Matchup effects re-project both lineups with the same rules (deterministic scenario); "
                                   "they are not win probabilities."}
    out["players"] = players
    return out


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    v = sorted(vals)
    n = len(v)
    return r(v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2)


def _counts(bench: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for b in bench:
        out[b["code"]] = out.get(b["code"], 0) + 1
    return out
