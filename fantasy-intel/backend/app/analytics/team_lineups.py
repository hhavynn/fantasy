"""Weekly optimal projected lineups, the league replacement market and roster marginal value.

Pure functions (no database). See docs/v2_trade_evaluator.md.

  * ESPN's weekly projection (espn_player_weekly_points.projected_points) is the only point input,
    unmodified: no xFP / role / matchup / schedule / injury / market multipliers.
  * Every remaining fantasy week is optimised separately with the league's real roster slots and
    the exact FLEX assignment in analytics/lineup.py.
  * Missing is not zero. A projected 0 (bye, injury) is a real value. A player with no projection
    is unknown: he never enters the lineup as a 0. If a lineup-relevant rostered player who has a
    current NFL team lacks a projection, the team-week is incomplete and its canonical total is
    None (`known_lineup_points` is still reported, labelled as known-only). A teamless player
    (no NFL team) legitimately has no projection and does not poison the week.
  * Bench points are never value: only starters count toward a lineup total.
  * A player currently in an IR slot (on_ir) stays rostered but is inactive: he never enters the
    canonical lineup and his projection (missing or not) never affects completeness, until a future
    sync shows ESPN moved him off IR. His ESPN projection is kept and reported; what he would add if
    activated for free is a separate, deliberately optimistic `ir_activation_sensitivity`.
  * A Sleeper taxi-squad player (on_taxi; analytics/reserve_slots.py) is held the same way: rostered and owned,
    never in the canonical lineup, no active roster spot, never counted against completeness. He is listed
    under `taxi` (only for a team that has one, so an ESPN week's shape is unchanged). A traded taxi player
    arrives in an active roster spot (arrives_active()), like a traded IR player.
  * NFL availability is separate from the fantasy IR slot. A week in a player's `unavailable_weeks`
    (verified NFL-ineligible: reserve list with the minimum-games rule not yet met, see
    analytics/player_availability.py) is treated like an IR week for that week only: he cannot
    start, his ESPN projection is kept and reported under `unavailable`, and it is never a measured
    0 and never makes the week incomplete. Outside those weeks nothing is assumed about his health.
  * Starting slots no engine fills (IDP, punter, head coach, unrecognised: lineup.LineupSlots.unsupported) are left
    out of every lineup, never filled with a 0. A week of such a league is partial: `projection_complete` False,
    `projected_lineup_points` None, the slots named under `unsupported_slots` (issue #253 G3). A comparison of two
    lineups is still exact when every one of those slots takes only positions the engines never start and both rosters
    hold the same such players (compare): the slots the engines leave out then contribute the same to both.
  * K / D/ST streaming baseline (opt-in, see StreamingProjections). Kickers and team defenses are
    streamed from free agents week to week, so holding one rostered K or D/ST all season is not the
    real alternative. When the projections carry a streaming baseline, each week a "waiver
    streamer" can fill the K and D/ST slots at that week's baseline: the STREAM_RANK-th best
    available free agent's ESPN projection at the position. A rostered K/D/ST starts only when he
    projects at least that much, so acquiring one is worth only what he adds above streaming and
    losing one costs only the gap down to it. The streamer uses no roster spot, is never a bench
    player and never a starter id (assignment row: player_id None, "streamer": True). Plain dict
    projections (the default) leave every result exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .lineup import (FIXED_SLOTS, FLEX_SLOTS, assign_lineup, eligible, positions_of, separable, slot_positions,
                     unsupported_of)

PROJECTION_SOURCE = "ESPN"
NO_PROJECTIONS = "Unavailable"      # as start_sit.NO_PROJECTIONS


def source_label(source: str | None, projections: Projections | None = None) -> str:
    """The projection source a response names: ESPN's stored projections, or Ours (Fantasy Intel's own model) for a
    league without provider projections (multi-league Phase 4b; projection_source.default_source). With the state's
    `projections`, a state that holds no number at all is named NO_PROJECTIONS, never a source: an Ours state with no
    verified run (or whose every row abstains, #254 L2, #253 S8), and an ESPN state with no stored ESPN projection
    (#254 phase 3). Under the league's default source an Ours state holds no provider number either: a league defaults
    to Ours only when its provider publishes no projections."""
    if projections is not None and not any(
            v is not None for by_week in projections.values() for v in by_week.values()):
        return NO_PROJECTIONS
    return "Ours" if source == "independent" else PROJECTION_SOURCE
FA_STATUSES = ("FREEAGENT", "WAIVERS")
NDIGITS = 2
ACTIVATION_DROP_NOTE = ("Activation would require a roster move/drop that is not modeled in this "
                        "sensitivity scenario.")

Projections = dict[int, dict[int, float]]     # espn_player_id -> week -> ESPN projected points (absent = unknown)


@dataclass(frozen=True)
class RosterPlayer:
    espn_player_id: int
    position: str | None
    pro_team: str | None = None        # current NFL team; None = teamless
    name: str | None = None
    on_ir: bool = False                # sits in an IR slot (does not use an active roster spot)
    injury_status: str | None = None
    unavailable_weeks: frozenset[int] = frozenset()   # verified NFL-ineligible weeks (not a fantasy slot)
    on_taxi: bool = False              # Sleeper taxi squad: rostered, never starts, no active roster spot
    eligible_slots: tuple[int, ...] | None = None   # provider slot ids (lineup.positions_of); None = primary only

    @property
    def reserved(self) -> bool:
        """In an IR or taxi slot: never starts and does not use an active roster spot."""
        return self.on_ir or self.on_taxi


def arrives_active(p: RosterPlayer) -> RosterPlayer:
    """A player acquired by trade arrives in an active roster spot: never in IR, never on the taxi squad."""
    return replace(p, on_ir=False, on_taxi=False)


def slot_flags(p: RosterPlayer) -> dict:
    """`on_taxi` for a response row, only for a taxi player (ESPN rows keep their exact shape)."""
    return {"on_taxi": True} if p.on_taxi else {}


@dataclass(frozen=True)
class Window:
    """The remaining fantasy weeks being evaluated. last_regular None = split unknown."""
    weeks: tuple[int, ...]
    last_regular: int | None

    def phase(self, week: int) -> str | None:
        if self.last_regular is None:
            return None
        return "regular" if week <= self.last_regular else "playoff"

    @property
    def start(self) -> int | None:
        return self.weeks[0] if self.weeks else None

    @property
    def final(self) -> int | None:
        return self.weeks[-1] if self.weeks else None


def r(x: float | None) -> float | None:
    return None if x is None else round(x + 0.0, NDIGITS) + 0.0   # + 0.0 turns -0.0 into 0.0


def lineup_relevant(player, slots: list[str]) -> bool:
    """Can this player fill any starting slot in this league? `player` is a position (None = unknown) or a player with
    `position` and optionally `eligible_slots`, so a player listed at a second position the slots take counts exactly
    as the lineup solver starts him (#253 G11): his missing projection leaves the total unknown, never complete."""
    return any(eligible(s, player) for s in set(slots))


def projection(proj: Projections, pid: int, week: int) -> float | None:
    v = proj.get(pid, {}).get(week)
    return None if v is None else float(v)


def can_play(p: RosterPlayer, week: int) -> bool:
    """Not in a fantasy IR or taxi slot and not verified NFL-ineligible that week."""
    return not p.reserved and week not in p.unavailable_weeks


# ------------------------------------------------------------ K / D/ST streaming baseline --

STREAM_POSITIONS = ("K", "D/ST")
# The baseline is the 3rd-best available free agent, not the best: other managers stream too, waiver
# order decides who gets the top option, and nobody knows every future matchup projection in advance.
STREAM_RANK = 3
# Synthetic ids for the per-week streamer inside the optimizer only. Far above any ESPN id, so a real
# player wins every tie (assign_lineup breaks ties by ascending id). Never returned as a starter id.
STREAMER_IDS = {"K": 10**12 + 1, "D/ST": 10**12 + 2}
STREAMER_NAMES = {"K": "Waiver streamer (K)", "D/ST": "Waiver streamer (D/ST)"}
STREAM_NOTE = ("K and D/ST are valued against weekly streaming: each week either slot can be filled from free agents "
               f"at that week's streaming baseline (the #{STREAM_RANK} available player's ESPN projection at the "
               "position). A rostered kicker or defense counts only in weeks he projects at least that much, so trading "
               "for or adding one is worth what he adds above streaming, and losing one costs only the gap down to it. "
               "The streamer is a weekly add/drop: it uses no extra roster spot in this model.")


class StreamingProjections(dict):
    """ESPN projections (unchanged, the same mapping) that also carry a per-week K/D/ST streaming baseline.

    `stream[position][week]` is the baseline in points; `stream_reference` names the free agent who sets it.
    Every function taking `proj` reads the baseline through stream_levels(), so one opt-in at the state
    level (trades.with_streaming) changes the trade evaluator, discovery and team value consistently."""

    def __init__(self, base: Projections, levels: dict[str, dict[int, float]], reference: dict, rank: int):
        super().__init__(base)
        self.stream = levels
        self.stream_reference = reference
        self.stream_rank = rank


def stream_levels(proj: Projections) -> dict[str, dict[int, float]]:
    return getattr(proj, "stream", None) or {}


def streaming_baseline(free_agents: list[RosterPlayer], slots: list[str], weeks, proj: Projections,
                       rank: int = STREAM_RANK) -> tuple[dict, dict]:
    """Per position the league starts (K, D/ST) and per week: the rank-th best ESPN projection among
    available players who can play that week (the weakest one when fewer are projected). No level when
    nobody is projected or the level is not positive: a missing projection is never a 0 streamer."""
    levels: dict[str, dict[int, float]] = {}
    reference: dict[str, dict[int, dict]] = {}
    for pos in STREAM_POSITIONS:
        if pos not in slots:
            continue
        for w in weeks:
            scored = sorted(((v, p) for p in free_agents if p.position == pos and w not in p.unavailable_weeks
                             and (v := projection(proj, p.espn_player_id, w)) is not None),
                            key=lambda t: (-t[0], t[1].espn_player_id))
            if not scored:
                continue
            k = min(rank, len(scored))
            v, p = scored[k - 1]
            if v <= 0:
                continue
            levels.setdefault(pos, {})[w] = v
            reference.setdefault(pos, {})[w] = {"espn_player_id": p.espn_player_id, "name": p.name,
                                                "pro_team": p.pro_team, "points": v, "rank": k,
                                                "available_projected": len(scored)}
    return levels, reference


def with_stream(proj: Projections, free_agents: list[RosterPlayer], slots: list[str], weeks,
                rank: int = STREAM_RANK) -> StreamingProjections:
    base = dict(proj)          # never stack a baseline on a baseline
    levels, reference = streaming_baseline(free_agents, slots, weeks, base, rank)
    return StreamingProjections(base, levels, reference, rank)


def stream_summary(proj: Projections, window: Window) -> dict:
    """What the API reports about the baseline in use (enabled False = raw ESPN lineups)."""
    if not isinstance(proj, StreamingProjections):
        return {"enabled": False, "positions": []}
    ref = proj.stream_reference
    return {"enabled": True, "rank": proj.stream_rank, "note": STREAM_NOTE,
            "positions": [{"position": pos, "weeks": [{"week": w, "phase": window.phase(w), **ref[pos][w]}
                                                      for w in window.weeks if w in ref.get(pos, {})]}
                          for pos in STREAM_POSITIONS if pos in ref]}


# ------------------------------------------------------------------ one team-week --

def project_week(players: list[RosterPlayer], slots: list[str], week: int, proj: Projections,
                 phase: str | None = None) -> dict:
    """Optimal lineup for one week from ESPN projections. Only players with a projection can start;
    current-IR players never start and are listed (with their projection) under `ir`, taxi-squad players the same
    under `taxi`; players verified NFL-ineligible this week are listed (with ESPN's projection, which is not counted)
    under `unavailable`."""
    known, missing, teamless, ir, taxi, unavailable = [], [], [], [], [], []
    for p in players:
        v = projection(proj, p.espn_player_id, week)
        if p.on_ir:
            ir.append({"player_id": p.espn_player_id, "position": p.position, "points": v})
        elif p.on_taxi:
            taxi.append({"player_id": p.espn_player_id, "position": p.position, "points": v})
        elif week in p.unavailable_weeks:
            unavailable.append({"player_id": p.espn_player_id, "position": p.position, "points": v})
        elif v is not None:
            known.append({"espn_player_id": p.espn_player_id, "position": p.position, "value": v,
                          "eligible_slots": p.eligible_slots})
        elif not lineup_relevant(p, slots):
            continue
        elif p.pro_team:
            missing.append(p.espn_player_id)
        else:
            teamless.append(p.espn_player_id)
    streamers = {STREAMER_IDS[pos]: pos for pos, per in stream_levels(proj).items()
                 if pos in slots and per.get(week) is not None}
    pool = known + [{"espn_player_id": sid, "position": pos, "value": stream_levels(proj)[pos][week]}
                    for sid, pos in streamers.items()]
    assignment = assign_lineup(pool, slots)
    starters = {a["player_id"] for a in assignment if a["player_id"] is not None}
    total = sum(a["value"] for a in assignment if a["player_id"] is not None)
    unsupported = unsupported_of(slots)
    # A slot no engine fills leaves the total partial: the known total is never presented as the lineup's (#253 G3).
    complete = not missing and not unsupported
    return {
        "week": week,
        "phase": phase,
        "projection_complete": complete,
        **({"unsupported_slots": list(unsupported)} if unsupported else {}),
        "known_lineup_points": r(total),
        "projected_lineup_points": r(total) if complete else None,
        "assignment": [_assignment_row(a, streamers) for a in assignment],
        "bench": ([{"player_id": k["espn_player_id"], "position": k["position"], "points": k["value"]}
                   for k in sorted(known, key=lambda k: (-k["value"], k["espn_player_id"]))
                   if k["espn_player_id"] not in starters]
                  + [{"player_id": pid, "position": None, "points": None} for pid in sorted(missing + teamless)]),
        "ir": sorted(ir, key=lambda x: x["player_id"]),
        **({"taxi": sorted(taxi, key=lambda x: x["player_id"])} if taxi else {}),
        "unavailable": sorted(unavailable, key=lambda x: x["player_id"]),
        "missing_projection_ids": sorted(missing),
        "teamless_unprojected_ids": sorted(teamless),
        "_total": total,             # unrounded known total, for exact comparisons
        "_projections_complete": not missing,
        # active players who can fill no slot the engines fill (the candidates for the unsupported slots)
        "_offslot": frozenset(p.espn_player_id for p in players if not p.reserved
                              and not lineup_relevant(p, slots)) if unsupported else frozenset(),
    }


def _assignment_row(a: dict, streamers: dict[int, str]) -> dict:
    if a["player_id"] in streamers:
        return {"slot": a["slot"], "player_id": None, "position": a["position"], "points": a["value"],
                "is_flex": a["is_flex"], "streamer": True, "name": STREAMER_NAMES[streamers[a["player_id"]]]}
    return {"slot": a["slot"], "player_id": a["player_id"], "position": a["position"],
            "points": a["value"] if a["player_id"] is not None else None, "is_flex": a["is_flex"]}


def slot_filled(a: dict) -> bool:
    """An assignment row holds a rostered player or the K/D/ST waiver streamer (not an empty slot)."""
    return a["player_id"] is not None or bool(a.get("streamer"))


def starters(week: dict) -> set[int]:
    return {a["player_id"] for a in week["assignment"] if a["player_id"] is not None}


# ------------------------------------------------------------------- a whole team --

def _part(weeks: list[dict]) -> tuple[float | None, float, int, int]:
    known = sum(w["_total"] for w in weeks)
    done = sum(1 for w in weeks if w["projection_complete"])
    return (r(known) if done == len(weeks) else None), r(known), len(weeks), done


def summarize_team(weeks: list[dict], window: Window) -> dict:
    unsupported = next((w["unsupported_slots"] for w in weeks if w.get("unsupported_slots")), None)
    ros = _part(weeks)
    if window.last_regular is None:
        reg = po = (None, None, None, None)
    else:
        reg = _part([w for w in weeks if w["week"] <= window.last_regular])
        po = _part([w for w in weeks if w["week"] > window.last_regular])
    return {
        "evaluation_start_week": window.start, "final_week": window.final,
        "last_regular_season_week": window.last_regular,
        "ros_projected_starting_points": ros[0], "ros_known_starting_points": ros[1],
        "regular_season_projected_starting_points": reg[0], "regular_season_known_starting_points": reg[1],
        "playoff_projected_starting_points": po[0], "playoff_known_starting_points": po[1],
        "ros_weeks_expected": ros[2], "ros_weeks_complete": ros[3],
        "missing_weeks": [w["week"] for w in weeks if w["missing_projection_ids"]],
        "missing_players": sorted({pid for w in weeks for pid in w["missing_projection_ids"]}),
        "projection_complete": ros[3] == ros[2],
        **({"unsupported_slots": unsupported} if unsupported else {}),
    }


def project_team(players: list[RosterPlayer], slots: list[str], window: Window, proj: Projections) -> dict:
    weeks = [project_week(players, slots, w, proj, window.phase(w)) for w in window.weeks]
    return {"weeks": weeks, "summary": summarize_team(weeks, window)}


def known_total(team: dict) -> float:
    return sum(w["_total"] for w in team["weeks"])


def public_weeks(team: dict) -> list[dict]:
    return [{k: v for k, v in w.items() if not k.startswith("_")} for w in team["weeks"]]


# ------------------------------------------------------------------- comparisons --

def _sum_delta(rows: list[dict]) -> float | None:
    if any(x["delta"] is None for x in rows):
        return None
    return r(sum(x["_delta"] for x in rows))


def delta_exact(b: dict, a: dict) -> bool:
    """Is after - before of two week lineups known exactly? Both sides must have every projection they need. In a league
    with unsupported starting slots (whose totals are partial) the delta is still exact when those slots take only
    positions the engines never start (lineup.separable) and both rosters hold the same such players: the left-out
    slots then contribute the same to both lineups."""
    if not (b.get("_projections_complete", b["projection_complete"]) and a.get("_projections_complete",
                                                                               a["projection_complete"])):
        return False
    unsupported = b.get("unsupported_slots") or a.get("unsupported_slots")
    if not unsupported:
        return True
    return separable(unsupported) and b.get("_offslot", frozenset()) == a.get("_offslot", frozenset())


def compare(before: dict, after: dict, window: Window) -> dict:
    """Week-by-week and aggregate starting-lineup delta (after - before). A week's canonical delta
    is None unless it is exact (delta_exact); an aggregate is None unless every week in it is."""
    rows = []
    unsupported = None
    for b, a in zip(before["weeks"], after["weeks"]):
        unsupported = unsupported or b.get("unsupported_slots") or a.get("unsupported_slots")
        complete = delta_exact(b, a)
        d = a["_total"] - b["_total"]
        rows.append({"week": b["week"], "phase": b["phase"],
                     "before": b["projected_lineup_points"], "after": a["projected_lineup_points"],
                     "known_before": b["known_lineup_points"], "known_after": a["known_lineup_points"],
                     "delta": r(d) if complete else None, "known_delta": r(d), "projection_complete": complete,
                     "_delta": d})
    split = window.last_regular is not None
    ros = _sum_delta(rows)
    done = [x for x in rows if x["delta"] is not None]
    out = {
        "ros_delta": ros,
        "regular_season_delta": _sum_delta([x for x in rows if x["phase"] == "regular"]) if split else None,
        "playoff_delta": _sum_delta([x for x in rows if x["phase"] == "playoff"]) if split else None,
        "known_ros_delta": r(sum(x["_delta"] for x in rows)),
        "average_weekly_delta": r(sum(x["_delta"] for x in rows) / len(rows)) if ros is not None and rows else None,
        "weeks_improved": sum(1 for x in done if x["delta"] > 0),
        "weeks_worsened": sum(1 for x in done if x["delta"] < 0),
        "weeks_unchanged": sum(1 for x in done if x["delta"] == 0),
        "weeks_incomplete": [x["week"] for x in rows if x["delta"] is None],
        "projection_complete": ros is not None,
        **({"unsupported_slots": unsupported} if unsupported else {}),
        "weeks": [{k: v for k, v in x.items() if not k.startswith("_")} for x in rows],
    }
    return out


def without(players: list[RosterPlayer], ids) -> list[RosterPlayer]:
    ids = set(ids)
    return [p for p in players if p.espn_player_id not in ids]


def roster_marginal_value(players: list[RosterPlayer], pid: int, slots: list[str], window: Window,
                          proj: Projections, base: dict | None = None,
                          open_active_roster_spots: int | None = None) -> dict:
    """Projected starting points this player creates for THIS team: optimal lineup with him minus
    optimal lineup without him, week by week. Bench weeks contribute exactly 0, and so does a
    current-IR player (he cannot occupy a lineup now; his upside is `ir_activation_sensitivity`) or a taxi player."""
    base = base or project_team(players, slots, window, proj)
    me = next(p for p in players if p.espn_player_id == pid)
    rest = project_team(without(players, [pid]), slots, window, proj)
    c = compare(rest, base, window)
    started = [w["week"] for w in base["weeks"] if pid in starters(w)]
    out = {
        "espn_player_id": pid,
        "on_ir": me.on_ir,
        **slot_flags(me),
        "roster_marginal_lineup_points": c["ros_delta"],
        "regular_season_marginal_lineup_points": c["regular_season_delta"],
        "playoff_marginal_lineup_points": c["playoff_delta"],
        "known_marginal_lineup_points": c["known_ros_delta"],
        "weeks_started": started, "weeks_expected": len(window.weeks),
        "projection_complete": c["projection_complete"],
        "weeks": [{"week": w["week"], "phase": w["phase"], "marginal": w["delta"], "known_marginal": w["known_delta"],
                   "started": w["week"] in started} for w in c["weeks"]],
    }
    if me.on_ir:
        out["ir_activation_sensitivity"] = ir_activation_sensitivity(players, pid, slots, window, proj,
                                                                     open_active_roster_spots, base)
    return out


# --------------------------------------------------------------- IR activation sensitivity --

IR_HOLD_NOTE = ("Current IR status is held constant for this hypothetical evaluation. This does not predict when a "
                "player will return. Activation upside is shown separately and may require a roster move.")


def activate(players: list[RosterPlayer], pid: int) -> list[RosterPlayer]:
    return [replace(p, on_ir=False) if p.espn_player_id == pid else p for p in players]


def ir_activation_sensitivity(players: list[RosterPlayer], pid: int, slots: list[str], window: Window,
                              proj: Projections, open_active_roster_spots: int | None,
                              base: dict | None = None) -> dict:
    """What a current-IR player would add to THIS team's optimal lineups if he were activated for
    free: same roster with him active, minus the canonical roster. Deliberately optimistic (the
    roster move an activation may need is not chosen) and never part of a canonical value."""
    base = base or project_team(players, slots, window, proj)
    act = project_team(activate(players, pid), slots, window, proj)
    c = compare(base, act, window)
    started = [w["week"] for w in act["weeks"] if pid in starters(w)]
    weekly = {w: projection(proj, pid, w) for w in window.weeks}
    me = next(p for p in players if p.espn_player_id == pid)
    blocked = sorted(w for w in window.weeks if w in me.unavailable_weeks)
    has_open = None if open_active_roster_spots is None else open_active_roster_spots > 0
    return {
        "label": "IR activation sensitivity (hypothetical free activation; not canonical)",
        "assumption": IR_HOLD_NOTE,
        "espn_player_id": pid,
        "weekly_projection": weekly,
        "projected_weeks": [w for w, v in weekly.items() if v is not None and v > 0 and w not in blocked],
        "nfl_ineligible_weeks": blocked,
        "unprojected_weeks": [w for w, v in weekly.items() if v is None],
        "weeks_would_start": started,
        "lineup_points_upside": c["ros_delta"], "known_lineup_points_upside": c["known_ros_delta"],
        "regular_season_upside": c["regular_season_delta"], "playoff_upside": c["playoff_delta"],
        "projection_complete": c["projection_complete"],
        "open_active_roster_spots": open_active_roster_spots,
        "open_active_roster_spot": has_open,
        "roster_move_note": None if has_open else ACTIVATION_DROP_NOTE,
        "weeks": [{"week": w["week"], "phase": w["phase"], "upside": w["delta"], "known_upside": w["known_delta"],
                   "would_start": w["week"] in started} for w in c["weeks"]],
    }


# --------------------------------------------------------------- replacement market --

def market_groups(slots: list[str]) -> list[tuple[str, tuple[str, ...]]]:
    """(group, eligible positions): every fixed position the league starts, plus each flex slot
    type it actually has (FLEX = RB/WR/TE, RB/WR, WR/TE, OP) with its real eligibility."""
    present = set(slots)
    out = [(s, (s,)) for s in FIXED_SLOTS if s in present]
    out += [(s, slot_positions(s)) for s in FLEX_SLOTS if s in present]
    return out


def replacement_market(free_agents: list[RosterPlayer], slots: list[str], window: Window, proj: Projections,
                       top_n: int = 5, projection_source: str = PROJECTION_SOURCE) -> dict:
    """The league's available-player frontier per week and position: only players whose ESPN
    availability is FREEAGENT/WAIVERS (the caller supplies them), only ESPN's projections. An
    unprojected free agent counts toward availability but never gets a manufactured projection."""
    groups = market_groups(slots)
    weeks = []
    for w in window.weeks:
        per = {}
        for g, elig in groups:
            pool = [p for p in free_agents if p.position in elig and w not in p.unavailable_weeks]
            scored = sorted(((projection(proj, p.espn_player_id, w), p) for p in pool
                             if projection(proj, p.espn_player_id, w) is not None),
                            key=lambda t: (-t[0], t[1].espn_player_id))
            top = [{"espn_player_id": p.espn_player_id, "name": p.name, "position": p.position,
                    "pro_team": p.pro_team, "points": v} for v, p in scored[:top_n]]
            per[g] = {"available_count": len(pool), "projected_count": len(scored),
                      "unprojected_count": len(pool) - len(scored),
                      "best_available": top[0] if top else None,
                      "best_available_projection": top[0]["points"] if top else None,
                      "top_available": top}
        weeks.append({"week": w, "phase": window.phase(w), "positions": per})
    return {"label": "replacement market (available-player frontier)", "statuses": list(FA_STATUSES),
            "pool_size": len(free_agents), "groups": [{"group": g, "eligible_positions": list(e)} for g, e in groups],
            "weeks": weeks, "projection_source": projection_source}


def _pareto(cands: list[RosterPlayer], weeks: tuple[int, ...], proj: Projections) -> list[RosterPlayer]:
    """Drop candidates weakly dominated week-by-week by another candidate at the same position who can fill every
    position he can (lineup.positions_of: a WR also listed at RB is never dropped for a WR-only player, #253 G11).
    Exact: the dominating candidate fits every slot the other fits, and the optimal lineup total is non-decreasing in
    one player's weekly value, so a dominated candidate can never produce a higher total (ties keep the lower id,
    matching the final tie-break)."""
    out = []
    by_pos: dict[str | None, list] = {}
    for c in cands:
        # A verified NFL-ineligible week contributes nothing, exactly like a 0 (he cannot start).
        by_pos.setdefault(c.position, []).append((c, tuple(0.0 if w in c.unavailable_weeks else proj[c.espn_player_id][w]
                                                           for w in weeks), positions_of(c)))
    for items in by_pos.values():
        items.sort(key=lambda t: (-sum(t[1]), t[0].espn_player_id))
        kept: list = []
        for c, v, pos in items:
            if not any(kpos >= pos and all(kv[i] >= v[i] for i in range(len(v))) for _, kv, kpos in kept):
                kept.append((c, v, pos))
        out += [c for c, _, _ in kept]
    return out


def replacement_positions(position: str | None, slots: list[str]) -> set[str]:
    """Positions that can fill at least one starting slot this position can fill (an RB can be
    replaced by an RB, or by a WR/TE through FLEX; a QB by a QB, or OP-eligible players)."""
    own = {s for s in set(slots) if eligible(s, position)}
    return {pos for s in own for pos in slot_positions(s)}


def best_fa_addition(players: list[RosterPlayer], free_agents: list[RosterPlayer], slots: list[str],
                     window: Window, proj: Projections, positions: set[str] | None = None) -> dict:
    """The single available player whose addition maximises this roster's remaining starting-lineup
    total (optionally only among `positions`). Hypothetical only: waiver availability is not
    guaranteed. Only free agents ESPN projects in every remaining week are scored (no projection is
    ever manufactured)."""
    rostered = {p.espn_player_id for p in players}
    pool = [p for p in free_agents if p.espn_player_id not in rostered and lineup_relevant(p, slots)
            and (positions is None or p.position in positions)]
    scored = [p for p in pool if all(projection(proj, p.espn_player_id, w) is not None for w in window.weeks)]
    base = project_team(players, slots, window, proj)
    best, best_team = None, None
    for c in _pareto(scored, window.weeks, proj):
        team = project_team(players + [c], slots, window, proj)
        key = (round(known_total(team), 9), -c.espn_player_id)
        if best is None or key > best[0]:
            best, best_team = (key, c), team
    if best is None:
        return {"player": None, "candidates": len(scored), "excluded_unprojected": len(pool) - len(scored)}
    c = best[1]
    gain = compare(base, best_team, window)
    return {
        "label": "hypothetical FA replacement",
        "player": {"espn_player_id": c.espn_player_id, "name": c.name, "position": c.position, "pro_team": c.pro_team},
        "lineup_points_added": gain["ros_delta"], "known_lineup_points_added": gain["known_ros_delta"],
        "regular_season_points_added": gain["regular_season_delta"], "playoff_points_added": gain["playoff_delta"],
        "weeks_started": [w["week"] for w in best_team["weeks"] if c.espn_player_id in starters(w)],
        "team_after": best_team["summary"],
        "projection_complete": gain["projection_complete"],
        "candidates": len(scored), "excluded_unprojected": len(pool) - len(scored),
    }


def replacement_context(players: list[RosterPlayer], pid: int, free_agents: list[RosterPlayer], slots: list[str],
                        window: Window, proj: Projections, base: dict | None = None) -> dict:
    """If this player disappeared and the team could add one available player eligible for a slot he
    could fill: who, how many lineup points come back, and the residual loss versus the original
    roster. (Unrestricted, an unrelated empty slot elsewhere would win: that is a roster-hole fill,
    not a replacement.) Never part of a trade delta."""
    base = base or project_team(players, slots, window, proj)
    rest_players = without(players, [pid])
    rest = project_team(rest_players, slots, window, proj)
    me = next(p for p in players if p.espn_player_id == pid)
    eligible_positions = replacement_positions(me.position, slots)
    if me.reserved:
        return {"label": "hypothetical FA replacement", "espn_player_id": pid,
                "lineup_points_lost_without_player": compare(rest, base, window)["ros_delta"],
                "eligible_positions": sorted(eligible_positions), "replacement": None, "replacement_position": None,
                "lineup_points_recovered": None, "known_lineup_points_recovered": None,
                "residual_loss_vs_original": None, "known_residual_loss_vs_original": None,
                "candidates": 0, "excluded_unprojected": 0, "projection_complete": True,
                "note": ("Current IR player" if me.on_ir else "Taxi-squad player")
                + ": he starts no canonical lineup and frees no active roster spot, so there is nothing to replace."}
    fa = best_fa_addition(rest_players, free_agents, slots, window, proj, eligible_positions)
    out = {"label": "hypothetical FA replacement", "espn_player_id": pid,
           "lineup_points_lost_without_player": compare(rest, base, window)["ros_delta"],
           "eligible_positions": sorted(eligible_positions),
           "replacement": fa["player"], "replacement_position": (fa["player"] or {}).get("position"),
           "lineup_points_recovered": fa.get("lineup_points_added"),
           "known_lineup_points_recovered": fa.get("known_lineup_points_added"),
           "residual_loss_vs_original": None, "known_residual_loss_vs_original": None,
           "candidates": fa["candidates"], "excluded_unprojected": fa["excluded_unprojected"],
           "note": "Waiver availability is not guaranteed; never included in the canonical trade delta."}
    if fa["player"] is not None:
        after = project_team(rest_players + [next(p for p in free_agents
                                                   if p.espn_player_id == fa["player"]["espn_player_id"])],
                             slots, window, proj)
        loss = compare(after, base, window)
        out["residual_loss_vs_original"] = loss["ros_delta"]
        out["known_residual_loss_vs_original"] = loss["known_ros_delta"]
    out["projection_complete"] = out["residual_loss_vs_original"] is not None or fa["player"] is None
    return out
