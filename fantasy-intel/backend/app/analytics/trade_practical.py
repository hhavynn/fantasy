"""Discovery modes and explanations on top of the exact trade search. Pure (no database).

Modes never change a trade's canonical valuation (trades.evaluate_trade); they decide which exact
rows are shown, in what order, and with which evidence:

  practical  (default) both projected starting lineups improve in the chosen phase, the asset
             packages are not lopsided against the other manager (trade_assets), and injured or
             incompletely projected packages are separated out. Ordered by the smaller side's
             canonical gain: the trade has to be useful to both managers.
  mutual     every exact trade in which both lineups improve, by the smaller side's gain, with the
             asset diagnostic shown but not used to filter.
  upside     exploratory: largest gain for you, whatever it costs the other manager; their losses
             and asset imbalances are labelled prominently.
  all        advanced: any sort and category over the full candidate set.

Ordering is always by canonical deltas (no hidden bonus points for names, positions or player
counts), and no row carries an acceptance probability.
"""

from __future__ import annotations

from dataclasses import replace

from .player_availability import ELIGIBLE_UNCONFIRMED, INELIGIBLE, NO_WEEK_LEFT, UNKNOWN
from .team_lineups import compare
from .trade_assets import AGAINST_THEM, NOTE as ASSET_NOTE, AssetBoard, league_needs
from .trade_discovery import TradeSearch, _rank_key, detail
from .trades import TradeError

MODES = {
    "practical": "Practical trades: both lineups improve, packages not lopsided against them, injured or incomplete "
                 "packages set apart.",
    "mutual": "Mutual improvement: both projected starting lineups gain (complete projections).",
    "upside": "Maximum upside (exploratory): largest gain for you; their losses are labelled.",
    "all": "All candidates (advanced): any sort or category over the whole search.",
}
PHASES = {"ros": ("min_side", "ros_delta"), "regular": ("min_side_regular", "regular_season_delta"),
          "playoff": ("min_side_playoff", "playoff_delta")}
STRICTNESS = {
    "strict": "Leave out any package in which they give up more asset value.",
    "standard": "Leave out packages with a significant imbalance against them (or multiple strong assets for fewer).",
    "off": "No asset filter (labels still shown).",
}
PRACTICAL_POOL = 100
# Display diversity in the practical tier: near-identical variants (same manager, same players received)
# crowd out other ideas, so at most PER_IDEA per (manager, players received) and PER_MANAGER per manager
# are listed there; the rest are counted, never re-ranked, and remain in the other modes.
PER_IDEA = 2
PER_MANAGER = 4
TIERS = {
    "practical": "Practical: both lineups improve and the packages are not lopsided",
    "concerns": "Both lineups improve, with concerns to weigh",
    "speculative": "Speculative or incomplete: depends on missing projections or an unconfirmed return",
}


def _phase_delta(side: dict, phase: str):
    return side[PHASES[phase][1]]


FANTASY_IR = "fantasy_ir_slot"


def injury_flags(state, pids) -> list[dict]:
    """Availability concerns for traded players. A player in a fantasy IR slot is flagged even when
    no NFL record restricts him: his current team gets no lineup value from him (IR held as synced),
    while the receiving team is credited with ESPN's projections from the first eligible week."""
    on_ir = {p.espn_player_id for ps in state.rosters.values() for p in ps if p.on_ir}
    out = []
    for pid in pids:
        rec = (state.availability or {}).get(pid)
        if rec and (rec["status"] in (INELIGIBLE, ELIGIBLE_UNCONFIRMED, NO_WEEK_LEFT, UNKNOWN) or rec["warnings"]):
            out.append({"espn_player_id": pid, "status": rec["status"], "warnings": rec["warnings"],
                        "earliest_eligible_week": rec["earliest_eligible_week"],
                        "return_confirmed": rec["return_confirmed"], "fantasy_ir_slot": pid in on_ir})
        elif pid in on_ir:
            out.append({"espn_player_id": pid, "status": FANTASY_IR, "earliest_eligible_week": None,
                        "return_confirmed": False, "fantasy_ir_slot": True,
                        "warnings": ["In a fantasy IR slot: worth no lineup points to his current team (IR held as "
                                     "synced) but counted from his first eligible week for the receiving team. His return "
                                     "is not established."]})
    return out


def _anchor_opportunity_cost(board, anchor: int, row: dict, alt: dict, fa,
                             before_team: dict, names: dict, window) -> dict:
    """Evaluate whether the anchor manager's candidate trade has a replacement opportunity-cost concern
    under the majority rule (available_without_trade > 0.5 * canonical_gain or trade_advantage <= 0)."""
    g = row["mine"]["ros_delta"]
    if g is None or g <= 0 or alt.get("player") is None or fa is None:
        return {"has_concern": False, "trade_advantage": g, "available_without_trade": 0.0, "lead": None}
    # Incoming players are active after the trade (as in trades.side_lineups), even if they sat in a
    # fantasy IR slot on their old team; otherwise scenario E would leave them out of the lineup.
    incoming = [replace(names[x], on_ir=False) for x in row["receives"] if x in names]
    c_team = alt["_team"]
    try:
        combined = board.trade_with(anchor, [p for p in board.state.rosters[anchor] if p.espn_player_id not in row["sends"]] + incoming,
                                    set(row["receives"]), fa)
    except TradeError:
        return {"has_concern": False, "trade_advantage": None, "available_without_trade": None, "lead": None}
    if not combined.get("available") or combined.get("team") is None:
        return {"has_concern": False, "trade_advantage": None, "available_without_trade": None, "lead": None}
    e_team = combined["team"]
    t = compare(c_team, e_team, window)["ros_delta"]
    if t is None:
        return {"has_concern": False, "trade_advantage": None, "available_without_trade": None, "lead": None}
    av = round(g - t, 9)
    p_info = alt["player"]
    p_name = p_info.get("name") or f"#{p_info.get('espn_player_id')}"
    p_pos = p_info.get("position", "")
    a_gain = alt["gain"]["ros_delta"] or 0.0
    if t <= 0:
        lead = f"All of this projected gain is available without trading: adding {p_name} ({p_pos}) gives {a_gain:+.1f}, and with that move made the trade adds {t:+.1f}."
        return {"has_concern": True, "trade_advantage": t, "available_without_trade": av, "lead": lead}
    if 2 * av > g:
        lead = f"Most of this projected gain is available without trading: adding {p_name} ({p_pos}) gives {a_gain:+.1f}; with that move made, the trade adds only {t:+.1f} of its {g:+.1f}."
        return {"has_concern": True, "trade_advantage": t, "available_without_trade": av, "lead": lead}
    return {"has_concern": False, "trade_advantage": t, "available_without_trade": av, "lead": None}


def classify(row: dict, balance: dict, injuries: list[dict], phase: str, strictness: str,
             opp_cost: dict | None = None) -> tuple[str, list[str]]:
    """(tier, reasons): practical | concerns | speculative | excluded_one_sided | excluded_lopsided."""
    m, t = _phase_delta(row["mine"], phase), _phase_delta(row["theirs"], phase)
    reasons = []
    if m is None or t is None:
        return "speculative", ["incomplete ESPN projections: no canonical total for this phase"]
    if m <= 0:
        return "excluded_one_sided", ["your lineup does not improve in this phase"]
    if t <= 0:
        return "excluded_one_sided", ["their lineup does not improve in this phase"]
    lab = balance["label"]
    against = AGAINST_THEM + (("they_give_more",) if strictness == "strict" else ())
    if strictness != "off" and lab in against:
        return "excluded_lopsided", [balance["text"]]
    receives = set(row["receives"])
    unsure = [f for f in injuries if f["status"] in (UNKNOWN, ELIGIBLE_UNCONFIRMED)
              or (f["fantasy_ir_slot"] and f["espn_player_id"] in receives)]
    if lab == "insufficient_data" or unsure:
        reasons += [balance["text"]] if lab == "insufficient_data" else []
        reasons += [w for f in unsure for w in f["warnings"][:1]]
        return "speculative", reasons
    if lab in ("they_give_more", "significant_you_give_more", "you_surrender_multiple_strong", "you_give_more",
               *AGAINST_THEM):
        reasons.append(balance["text"])
    reasons += [f["warnings"][0] for f in injuries if (f["status"] in (INELIGIBLE, NO_WEEK_LEFT) or f["fantasy_ir_slot"])
                and f["warnings"]]
    if row["forced_drops"]["mine"] or row["forced_drops"]["theirs"]:
        reasons.append("requires a forced drop")
    if opp_cost and opp_cost.get("has_concern"):
        lead = opp_cost.get("lead")
        if lead and lead not in reasons:
            reasons.append(lead)
    if lab in ("they_give_more", "significant_you_give_more", "you_surrender_multiple_strong") or \
            any(f["status"] in (INELIGIBLE, NO_WEEK_LEFT) or f["fantasy_ir_slot"] for f in injuries) or \
            (opp_cost and opp_cost.get("has_concern")):
        return "concerns", reasons
    return "practical", reasons


# ------------------------------------------------------------------------------ rationale --

def _starts(u: dict) -> int:
    return len(u["weeks_started"])


def rationale(d: dict, needs: dict, board: AssetBoard, names: dict, n: int) -> dict:
    """Evidence-backed statements for one fully evaluated row (discovery.detail output)."""
    me, them = d["mine_detail"], d["theirs_detail"]
    me_name, them_name = "You", d["opponent_name"] or "They"
    points, concerns = [], []
    pos = lambda pid: (names.get(pid) or {}).get("position")   # noqa: E731
    nm = lambda pid: (names.get(pid) or {}).get("name") or f"#{pid}"   # noqa: E731
    for u in me["incoming_usage"]:
        k = _starts(u)
        if k * 2 >= n:
            points.append(f"You gain a projected {u['position']} starter: {u['name']} starts {k} of {n} weeks for you.")
        elif k:
            points.append(f"{u['name']} ({u['position']}) would start {k} of {n} weeks for you.")
        else:
            concerns.append(f"{u['name']} ({u['position']}) would not start for you in any remaining week.")
    for u in them["incoming_usage"]:
        k = _starts(u)
        if k * 2 >= n:
            points.append(f"Their {u['position']} position improves: {u['name']} starts {k} of {n} weeks for them.")
        elif k:
            points.append(f"{u['name']} ({u['position']}) would start {k} of {n} weeks for them.")
        else:
            concerns.append(f"They receive {u['name']} ({u['position']}), who would not start for them.")
    m, t = me["deltas"]["ros_delta"], them["deltas"]["ros_delta"]
    if m is not None and t is not None and m > 0 and t > 0:
        points.append(f"Both teams' projected starting-lineup totals improve (you {m:+.1f}, them {t:+.1f}).")
    if t is not None and t < 0:
        concerns.append(f"Their projected starting lineup loses {abs(t):.1f} points over the rest of the season.")
    if m is not None and m < 0:
        concerns.append(f"Your projected starting lineup loses {abs(m):.1f} points over the rest of the season.")
    for u in them["outgoing_usage"]:
        if _starts(u) * 2 > n:
            concerns.append(f"They surrender a player who currently starts in most weeks: {u['name']} "
                            f"({_starts(u)} of {n}).")
    for u in me["outgoing_usage"]:
        if _starts(u) * 2 > n:
            points.append(f"You give up a regular starter: {u['name']} ({_starts(u)} of {n} weeks).")
    bal = d["asset_balance"]
    if bal["label"] not in ("balanced", "replacement_level", None):
        if m is not None and t is not None and m > 0 and t > 0:
            concerns.append(f"The asset packages appear uneven despite the lineup benefits: {bal['text'].lower()}.")
        else:
            concerns.append(bal["text"] + ".")
    mine_need, opp_need = needs.get(d["anchor_team_id"], {}), needs.get(d["opponent_team_id"], {})
    for pid in d["receives"]:
        p, nd = pos(pid), mine_need.get(pos(pid))
        if nd and nd.get("short"):
            points.append(f"You are short at {p}: your {p} starters rank {nd['league_rank']} of {nd['teams']} "
                          "in projected points per slot.")
    for pid in d["sends"]:
        p, nd = pos(pid), opp_need.get(pos(pid))
        if nd and nd.get("short"):
            points.append(f"{them_name} is short at {p}: their {p} starters rank {nd['league_rank']} of {nd['teams']}. "
                          "A roster need is not a willingness to trade.")
        if pid in (mine_need.get(pos(pid)) or {}).get("surplus", []):
            points.append(f"{nm(pid)} is surplus for you: starter-caliber at {p} but starts at most half your weeks.")
    for pid in d["receives"]:
        if pid in (opp_need.get(pos(pid)) or {}).get("surplus", []):
            points.append(f"{them_name} can spare {nm(pid)}: starter-caliber at {pos(pid)} but starts at most half "
                          "their weeks.")
    for label, side in ((me_name, me), (them_name, them)):
        holes = side.get("empty_slots_after") or []
        if holes:
            weeks = sorted({h["week"] for h in holes})
            slots = sorted({h["slot"] for h in holes})
            concerns.append(f"{label} would have an empty {'/'.join(slots)} slot in week(s) {', '.join(map(str, weeks))} "
                            "after the trade (no eligible player left).")
        if side["forced_drops"]:
            concerns.append(f"{label} must drop {', '.join(x['name'] or str(x['espn_player_id']) for x in side['forced_drops'])} "
                            "to stay within roster limits.")
    for f in d.get("injury_flags", []):
        concerns += f["warnings"][:1]
    if me["incomplete"]["missing_weeks"] or them["incomplete"]["missing_weeks"]:
        concerns.append("Some ESPN projections are missing; affected weeks have no canonical total.")
    return {"trade_type": trade_type(d, names, n), "points": list(dict.fromkeys(points)),
            "concerns": list(dict.fromkeys(concerns))}


def trade_type(d: dict, names: dict, n: int) -> dict:
    me, them = d["mine_detail"], d["theirs_detail"]
    ps = sorted({(names.get(x) or {}).get("position") for x in d["sends"]} - {None})
    pr = sorted({(names.get(x) or {}).get("position") for x in d["receives"]} - {None})
    if len(d["sends"]) != len(d["receives"]):
        fewer = "you" if len(d["receives"]) < len(d["sends"]) else "they"
        kind, text = "consolidation", f"Consolidation: {fewer} turn depth into fewer, stronger players."
    elif all(_starts(u) * 2 <= n for u in me["outgoing_usage"]) and any(_starts(u) * 2 >= n for u in me["incoming_usage"]):
        kind, text = "depth_for_starter", "Depth for a starter: you send players who mostly sit for one who starts."
    elif all(_starts(u) * 2 <= n for u in them["outgoing_usage"]) and any(_starts(u) * 2 >= n for u in them["incoming_usage"]):
        kind, text = "depth_for_starter", "Depth for a starter: they send players who mostly sit for one who starts."
    elif ps != pr:
        kind, text = "positional_exchange", f"Positional exchange: {'/'.join(ps)} for {'/'.join(pr)}."
    else:
        kind, text = "like_for_like", f"Like-for-like: {'/'.join(ps)} for {'/'.join(pr)}."
    return {"kind": kind, "text": text}


# ---------------------------------------------------------------------------- the modes --

def _pool(ts: TradeSearch, sort: str, category: str | None, opponent: int | None, limit: int,
          heuristic: bool) -> tuple[list[dict], dict, dict | None]:
    exact = ts.top(sort, category, opponent, limit)
    rows = list(exact["results"])
    heur = None
    if heuristic and ts.heuristic_shapes:
        heur = ts.heuristic_top(sort, category, opponent, limit)
        keys = {r["key"] for r in rows}
        rows += [r for r in heur["results"] if r["key"] not in keys]
    rows.sort(key=_rank_key(sort))
    return rows, exact["search"], heur["search"] if heur else None


def discover(ts: TradeSearch, mode: str = "practical", phase: str = "ros", opponent: int | None = None,
             limit: int = 10, strictness: str = "standard", sort: str | None = None, category: str | None = None,
             heuristic: bool = True, board: FreeAgencyBoard | None = None) -> dict:
    """Rows for one mode, assessed and explained. Every shown row is re-run through the manual
    evaluator (detail) and carries consistent_with_manual_evaluator."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {', '.join(PHASES)}")
    if strictness not in STRICTNESS:
        raise ValueError(f"strictness must be one of {', '.join(STRICTNESS)}")
    state, window = ts.state, ts.window
    asset_board = AssetBoard(state, window)
    needs = league_needs(state, window, asset_board)
    names = {p.espn_player_id: {"name": p.name, "position": p.position} for ps in state.rosters.values() for p in ps}
    n = len(window.weeks)
    phase_sort = PHASES[phase][0]
    mutual = "MUTUAL_PROJECTED_IMPROVEMENT"
    if mode == "practical":
        rows, exact_search, heur_search = _pool(ts, phase_sort, mutual if phase == "ros" else None, opponent,
                                                PRACTICAL_POOL, heuristic)
        pool_rule = (f"the exact top {PRACTICAL_POOL} by smaller-side {phase} gain"
                     + (" among mutual-improvement trades" if phase == "ros" else "")
                     + (" plus the heuristic larger-shape rows" if heur_search else ""))
    elif mode == "mutual":
        if phase == "ros":
            rows, exact_search, heur_search = _pool(ts, phase_sort, mutual, opponent, max(limit, 1), heuristic)
        else:
            # Mutual in the selected phase: both phase deltas positive, i.e. a positive smaller-side phase
            # gain. The exact top K by that sort, filtered to positive values, is the exact top K of them.
            rows, exact_search, heur_search = _pool(ts, phase_sort, None, opponent, max(limit, 1), heuristic)
            rows = [x for x in rows if (_phase_delta(x["mine"], phase) or 0) > 0 and (_phase_delta(x["theirs"], phase) or 0) > 0]
        pool_rule = f"exact smaller-side {phase} gain among trades in which both lineups improve in that phase"
    elif mode == "upside":
        rows, exact_search, heur_search = _pool(ts, "my_gain", None, opponent, max(limit, 1), heuristic)
        pool_rule = "exact largest rest-of-season gain for you"
    else:
        rows, exact_search, heur_search = _pool(ts, sort or "my_gain", category, opponent, max(limit, 1), heuristic)
        pool_rule = f"exact {sort or 'my_gain'} order" + (f" in {category}" if category else "")

    fa_board = board
    if fa_board is None and getattr(state, "free_agents", None):
        from .trade_alternatives import FreeAgencyBoard
        fa_board = FreeAgencyBoard(state, window)

    alt_anchor = fa_board.best_acquisition(ts.anchor, state.rosters[ts.anchor], kinds=("free_agent",)) if fa_board else {}
    fa_anchor = next((p for p in fa_board.pool if p.espn_player_id == alt_anchor["player"]["espn_player_id"]), None) \
        if fa_board and alt_anchor.get("player") else None
    player_map = {p.espn_player_id: p for ps in state.rosters.values() for p in ps} if fa_board else {}
    before_anchor = fa_board.team(state.rosters[ts.anchor]) if fa_board else {}

    assessed = []
    for row in rows:
        bal = asset_board.package(row["sends"], row["receives"])
        inj = injury_flags(state, row["sends"] + row["receives"])
        opp_cost = _anchor_opportunity_cost(fa_board, ts.anchor, row, alt_anchor, fa_anchor, before_anchor,
                                           player_map, window) if fa_board else None
        tier, reasons = classify(row, bal, inj, phase, strictness, opp_cost=opp_cost)
        assessed.append({**row, "asset_balance": bal, "injury_flags": inj, "tier": tier, "tier_reasons": reasons,
                         "opp_cost_advantage": opp_cost.get("trade_advantage") if opp_cost else None})
    excluded = {"one_sided": 0, "lopsided": 0, "similar_variants": 0}
    sections: dict[str, list] = {}
    if mode == "practical":
        def _practical_sort_key(x):
            # trade_advantage is a rest-of-season figure: only comparable with the ros phase.
            adv = x.get("opp_cost_advantage") if phase == "ros" else None
            if adv is None:
                adv = _phase_delta(x["mine"], phase) or 0.0
            theirs = _phase_delta(x["theirs"], phase) or 0.0
            inc_min = min(adv, theirs)
            return (-round(inc_min, 9), _rank_key(phase_sort)(x))

        practical_cands = [x for x in assessed if x["tier"] == "practical"]
        practical_cands.sort(key=_practical_sort_key)
        other_cands = [x for x in assessed if x["tier"] != "practical"]
        ordered_assessed = practical_cands + other_cands

        ideas, managers = {}, {}
        for x in ordered_assessed:
            if x["tier"].startswith("excluded"):
                excluded["one_sided" if x["tier"] == "excluded_one_sided" else "lopsided"] += 1
                continue
            if x["tier"] == "practical":
                idea = (x["opponent_team_id"], tuple(x["receives"]))
                if ideas.get(idea, 0) >= PER_IDEA or managers.get(x["opponent_team_id"], 0) >= PER_MANAGER:
                    excluded["similar_variants"] += 1
                    continue
                ideas[idea] = ideas.get(idea, 0) + 1
                managers[x["opponent_team_id"]] = managers.get(x["opponent_team_id"], 0) + 1
            sections.setdefault(x["tier"], []).append(x)
        caps = {"practical": limit, "concerns": max(3, limit // 2), "speculative": max(3, limit // 2)}
        sections = {k: sections.get(k, [])[:caps[k]] for k in TIERS}
    else:
        sections = {"results": assessed[:limit]}
    shown = []
    for k, items in sections.items():
        full = []
        for x in items:
            d = detail(state, x)
            d.update({k2: x[k2] for k2 in ("asset_balance", "injury_flags", "tier", "tier_reasons", "search_method")})
            d["rationale"] = rationale(d, needs, asset_board, names, n)
            full.append(d)
        sections[k] = full
        shown += full
    per_opp = []
    for opp in ts.opponents:
        best = next((x for x in assessed if x["opponent_team_id"] == opp and
                     (mode != "practical" or x["tier"] == "practical")), None)
        per_opp.append({"opponent_team_id": opp, "team_name": state.team_names.get(opp),
                        "best": None if best is None else {k: best[k] for k in ("key", "shape", "sends", "receives",
                                                                                "mine", "theirs", "tier", "search_method")}
                        | {"asset_label": best["asset_balance"]["label"]},
                        "needs": {p: {k: v for k, v in nd.items() if k != "surplus"} | {"surplus": nd["surplus"]}
                                  for p, nd in needs[opp].items()}})
    return {
        "mode": mode, "mode_text": MODES[mode], "phase": phase, "strictness": strictness,
        "sections": sections, "tiers": TIERS if mode == "practical" else {"results": MODES[mode]},
        "excluded": excluded, "pool_rule": pool_rule, "pool_size": len(rows),
        "diversity_rule": (f"Practical tier: at most {PER_IDEA} variants per manager and set of players received, and "
                           f"{PER_MANAGER} per manager; similar variants are counted, not re-ranked.")
        if mode == "practical" else None,
        "coverage": {"exact": exact_search, "heuristic": heur_search,
                     "exact_shapes": ts.shapes, "heuristic_shapes": ts.heuristic_shapes,
                     "positions": list(ts.positions) if ts.positions else None},
        "opponents": per_opp, "my_needs": needs[ts.anchor],
        "consistency_checked": len(shown),
        "consistency_failures": sum(1 for x in shown if not x["consistent_with_manual_evaluator"]),
        "asset_note": ASSET_NOTE,
        "asset_thresholds": asset_board.package([], [])["thresholds"],
    }
