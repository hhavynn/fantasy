"""Batched waiver-only adapter over stored ESPN/NFL data. Never fetches remotely."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..db.connection import fetch_dicts, table_columns, table_exists
from ..espn.constants import FANTASY_POSITIONS, IR_SLOT
from .lineup import assign_lineup, lineup_slots
from .availability import ESPN_STATUS, availability
from .projections import load_schedule
from .waiver_roster import object_value, roster_constraints, projected_players
from .team_lineups import STREAM_POSITIONS, STREAM_RANK

# 15 from each independent evidence lane, deduplicated, max 60 per position.
# Ownership only supplies coverage for players without other evidence, never an entry threshold.
LANE_SIZE = 15
FRESHNESS_HOURS = 72  # cautious snapshot threshold, not an injury forecast


def stale_snapshot(stamp, now: datetime) -> bool:
    if stamp is None:
        return True
    if isinstance(stamp, str):
        stamp = datetime.fromisoformat(stamp)
    return (now - stamp.replace(tzinfo=stamp.tzinfo or timezone.utc)).total_seconds() > FRESHNESS_HOURS * 3600


def comparable_free_agents(candidate: dict, values: dict, pool: list[dict], ctx: dict, reports: dict | None = None) -> list[dict]:
    cv = values.get(candidate["espn_player_id"])
    w = ctx["weeks"][0] if ctx["weeks"] else None
    candidate_points = ctx["projections"].get(candidate["espn_player_id"], {}).get(w)
    options = []
    for p in pool:
        if p["position"] != candidate["position"] or p["availability_status"] != "FREEAGENT" or p["espn_player_id"] == candidate["espn_player_id"]:
            continue
        if (p.get("injury_status") or "").upper() not in ESPN_STATUS or stale_snapshot(p.get("detail_synced_at"), ctx["now"]):
            continue
        if availability(p["injury_status"], None, (reports or {}).get(p.get("gsis_id")))["weight"] != 1:
            continue
        if w in ctx["covered"] and p.get("pro_team") and (p["pro_team"], w) not in ctx["games"]:
            continue
        pv = values.get(p["espn_player_id"])
        points = ctx["projections"].get(p["espn_player_id"], {}).get(w)
        by_projection = points is not None and candidate_points is not None and points >= candidate_points - 0.5
        by_value = cv and cv.value is not None and pv and pv.value is not None and pv.value >= cv.value - 0.5
        if by_projection if candidate_points is not None else by_value:
            options.append((points if by_projection else -1, pv.value if pv and pv.value is not None else -1, -p["espn_player_id"], p))
    return [{"name": p["full_name"], "espn_player_id": p["espn_player_id"]}
            for *_, p in sorted(options, key=lambda o: o[:3], reverse=True)]


def comparable_free_agent(candidate: dict, values: dict, pool: list[dict], ctx: dict, reports: dict | None = None) -> dict | None:
    choices = comparable_free_agents(candidate, values, pool, ctx, reports)
    return choices[0] if choices else None


def select_candidates(pool: list[dict], values: dict, projections: dict, depth: dict, weeks: list[int]) -> tuple[list[dict], dict]:
    selected, counts = [], {}
    for pos in FANTASY_POSITIONS:
        group = [c for c in pool if c["position"] == pos]

        def quality(c):
            v = values.get(c["espn_player_id"])
            return v.value if v and v.value is not None else -1

        lanes = [
            sorted(group, key=lambda c: (-max((projections.get(c["espn_player_id"], {}).get(w, -1) for w in weeks), default=-1), c["espn_player_id"])),
            sorted(group, key=lambda c: ((depth.get(c.get("gsis_id")) or {}).get("rank") or 99,
                                        -quality(c), c["espn_player_id"])),
            sorted(group, key=lambda c: (-quality(c), c["espn_player_id"])),
            sorted(group, key=lambda c: (-(c.get("percent_owned") or 0), c["espn_player_id"])),
        ]
        # Fill duplicate lane places with the next ranked candidate, up to 60 total.
        ids = set()
        for lane in lanes:
            added = 0
            for c in lane:
                if c["espn_player_id"] in ids:
                    continue
                selected.append(c)
                ids.add(c["espn_player_id"])
                added += 1
                if added == LANE_SIZE:
                    break
        counts[pos] = {"eligible": len(group), "evaluated": len(ids), "excluded_by_limit": len(group) - len(ids)}
    return selected, {"eligible": len(pool), "evaluated": len(selected), "excluded_by_limit": len(pool) - len(selected),
                      "positions": counts, "per_position_limit": 4 * LANE_SIZE,
                      "method": "15 each: forward projection, depth-chart role, historical value, ownership; deduplicated lanes fill to 60/position",
                      "limitation": "Coverage is the stored ESPN pool, not the entire NFL universe. Sync truncation may omit players."}


def load_waiver_context(con, league: dict, now: datetime | None = None, source: str | None = None) -> dict:
    """`source`: None = the league's default (projection_source.default_source). Ours values carry their run's
    creation time; the 72-hour staleness rule is for daily provider snapshots and does not apply to them."""
    from .projection_source import league_projections, normalize
    now = now or datetime.now(timezone.utc)
    source = normalize(source, con, league.get("season"))
    current, final = league.get("current_week"), league.get("final_scoring_period")
    actionable = current <= final if current and final else None
    start = current or 1
    warnings = []
    # A scoring period already underway is not a full-week acquisition opportunity.
    # Use stored kickoff dates only; no reuse/change of trade availability semantics.
    if table_exists(con, "nfl_schedules") and {"gameday", "gametime"} <= set(table_columns(con, "nfl_schedules")):
        games = fetch_dicts(con, "SELECT gameday, gametime FROM nfl_schedules WHERE season=? AND week=? AND game_type='REG'",
                            [league["season"], start])
        kicks, invalid_kickoff = [], False
        for g in games:
            if g["gameday"] and g["gametime"]:
                try:
                    kick = datetime.fromisoformat(f"{g['gameday']}T{g['gametime']}").replace(tzinfo=ZoneInfo("America/New_York"))
                except (ValueError, TypeError):
                    invalid_kickoff = True
                    continue
                kicks.append(kick)
            else:
                invalid_kickoff = True
        if kicks and min(kicks) <= now:
            start += 1
            warnings.append("Current scoring period has started; full-week projection comparisons begin next week. Check individual player locks in ESPN.")
        elif invalid_kickoff and actionable is not False:
            actionable = None
        if invalid_kickoff:
            warnings.append("Some stored kickoff dates/times are missing or malformed; current-week game-lock timing is incomplete.")
    weeks = list(range(start, final + 1)) if final and start <= final and actionable is not False else []
    if final and start > final:
        actionable = False
    if actionable is None:
        warnings.append("Actionable scoring window is unknown; final scoring period/current week is missing.")
    projections, synced, projection_stamps = defaultdict(dict), {}, defaultdict(dict)
    stale_count = 0
    if weeks and source != "espn":
        for pid, by_week in league_projections(con, league["season"], weeks, source).items():
            for w, (v, stamp) in by_week.items():
                projections[pid][w] = v
                projection_stamps[pid][w] = stamp
                synced[pid] = max(synced.get(pid) or stamp, stamp) if stamp else synced.get(pid)
    elif weeks:
        for r in fetch_dicts(con, """SELECT espn_player_id, scoring_period, projected_points, synced_at
                                    FROM espn_player_weekly_points WHERE season=? AND scoring_period BETWEEN ? AND ?
                                    AND projected_points IS NOT NULL""", [league["season"], weeks[0], weeks[-1]]):
            pid, w = r["espn_player_id"], r["scoring_period"]
            projection_stamps[pid][w] = r["synced_at"]
            if stale_snapshot(r["synced_at"], now):
                stale_count += 1
                continue
            projections[pid][w] = r["projected_points"]
            synced[pid] = max(synced.get(pid) or r["synced_at"], r["synced_at"])
    if stale_count:
        warnings.append(f"{stale_count} undated or older-than-72-hour projected player-weeks excluded from forward comparisons.")
    games, covered = load_schedule(con, league["season"], weeks)
    if weeks and set(weeks) - covered:
        warnings.append("NFL schedule coverage is incomplete; bye coverage and individual game locks cannot be fully verified.")
    lines = {(r["team"], r["week"]): r for r in fetch_dicts(con, "SELECT * FROM team_game_lines WHERE season=?", [league["season"]])}
    offenses = {r["team"]: r for r in fetch_dicts(con, "SELECT * FROM team_context WHERE season=? AND through_week < ?", [league["season"], start])}
    defenses = {r["defteam"]: r for r in fetch_dicts(con, "SELECT * FROM defense_context WHERE season=? AND through_week < ?", [league["season"], start])}
    raw = object_value(league.get("settings_raw"))
    acq = raw.get("acquisitionSettings") or {}
    # Only explicit strings are decoded. Numeric ESPN enum values aren't guessed.
    rule = str(acq.get("waiverOrderType") or "").upper()
    rule = "ROLLING" if rule in ("ROLLING", "MOVE_TO_LAST_AFTER_CLAIM", "MOVE_TO_LAST_AFTER_SUCCESSFUL_CLAIM") else "RESET" if rule in ("RESET", "INVERSE_STANDINGS") else "UNKNOWN"
    rules = {"is_faab": league.get("is_faab"), "faab_budget": league.get("faab_budget"), "waiver_rule": rule}
    if league.get("reception_points") != 1:
        warnings.append("Historical player value and xFP use PPR-based usage; they are not recalibrated to this league. ESPN projections use league scoring.")
    constraints = roster_constraints(league)
    return {"weeks": weeks, "actionable": actionable, "projections": dict(projections), "projection_synced": synced,
            "projection_stamps": dict(projection_stamps), "stale_projection_count": stale_count,
            "projection_source": source,
            "games": games, "covered": covered, "lines": lines, "offenses": offenses, "defenses": defenses,
            "constraints": constraints, "rules": rules,
            "warnings": warnings, "now": now}


def attach_roster_evidence(state: dict, roster: list[dict], ctx: dict) -> None:
    by_id = {r["espn_player_id"]: r for r in roster}
    state["weeks"] = ctx["weeks"]
    for p in state["players"]:
        raw = by_id[p["espn_player_id"]]
        p["projections"] = ctx["projections"].get(p["espn_player_id"], {})
        p["bye_weeks"] = [w for w in ctx["weeks"] if w in ctx["covered"] and raw.get("pro_team")
                          and (raw["pro_team"], w) not in ctx["games"]]
        p["detail_synced_at"] = raw.get("detail_synced_at")


def streaming_evidence(c: dict, state: dict, ctx: dict, pool: list[dict]) -> dict:
    slots = lineup_slots({c["position"]: int(state["roster_slots"].get(c["position"], 0))})
    weeks = []
    for w in ctx["weeks"][:3]:
        lineup = assign_lineup(projected_players(state["players"], w), slots)
        # For multiple starters compare to the weakest required starter, not the strongest.
        known = [a["value"] for a in lineup if a["player_id"] is not None]
        relevant = [p for p in state["players"] if p["position"] == c["position"] and p.get("lineup_slot_id") != IR_SLOT]
        complete = all(p.get("projections", {}).get(w) is not None or w in p.get("bye_weeks", []) for p in relevant)
        incumbent = min(known) if len(known) == len(slots) else 0 if complete else None
        if not complete:
            incumbent = None
        weeks.append({"week": w, "points": ctx["projections"].get(c["espn_player_id"], {}).get(w),
                      "synced_at": ctx["projection_stamps"].get(c["espn_player_id"], {}).get(w),
                      "opponent": (ctx["games"].get((c.get("pro_team"), w)) or (None,))[0],
                      "bye": w in ctx["covered"] and bool(c.get("pro_team")) and (c["pro_team"], w) not in ctx["games"],
                      "incumbent_points": incumbent})
    first = weeks[0] if weeks else {}
    w = first.get("week")
    alternatives = [ctx["projections"].get(p["espn_player_id"], {}).get(w) for p in pool
                    if p["position"] == c["position"] and p["espn_player_id"] != c["espn_player_id"]]
    alternatives = [p for p in alternatives if p is not None]
    opp = first.get("opponent")
    return {"weeks": weeks, "incumbent_points": first.get("incumbent_points"),
            "alternative_points": max(alternatives) if alternatives else None,
            "opponent_implied_total": ctx["lines"].get((opp, w), {}).get("implied_total"),
            "team_implied_total": ctx["lines"].get((c.get("pro_team"), w), {}).get("implied_total"),
            "opponent_offense": ctx["offenses"].get(opp), "defense_performance": ctx["defenses"].get(c.get("pro_team")),
            "projection_source": ("Fantasy Intel's own projections (Ours), in league scoring"
                                  if ctx.get("projection_source") == "independent"
                                  else "Stored ESPN league-scoring projections"),
            "projection_synced_at": ctx["projection_synced"].get(c["espn_player_id"])}


def streaming_floor(pool: list[dict], ctx: dict, roster_slots: dict, rank: int = STREAM_RANK) -> dict:
    """K/D/ST streaming baseline for the forward comparisons (team_lineups.STREAM_NOTE): per position the
    league starts and per week after the upcoming one, the rank-th best ESPN projection among available
    players at the position (the weakest projected one when fewer exist). A player on a covered NFL bye that
    week never sets it, exactly as the forward lineups never start a player on his bye (bye_weeks).
    The upcoming week is left out:
    that week's pickup is the streaming decision itself (streaming_evidence), so holding a K or D/ST is
    credited only for later weeks it beats the stream."""
    out: dict[str, dict[int, float]] = {}
    for pos in STREAM_POSITIONS:
        if not int((roster_slots or {}).get(pos, 0)):
            continue
        for w in ctx["weeks"][1:]:
            vals = sorted((v for p in pool if p["position"] == pos
                           and not (w in ctx.get("covered", ()) and p.get("pro_team")
                                   and (p["pro_team"], w) not in ctx.get("games", {}))
                           and (v := ctx["projections"].get(p["espn_player_id"], {}).get(w)) is not None),
                          reverse=True)
            if vals and vals[min(rank, len(vals)) - 1] > 0:
                out.setdefault(pos, {})[w] = vals[min(rank, len(vals)) - 1]
    return out
