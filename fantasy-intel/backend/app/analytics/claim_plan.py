"""Waiver claim planner (issue #180): checks an ordered list of claims against the synced league. Pure: no database,
no provider call, nothing is submitted.

The plan itself lives on the manager's device (the Waivers shortlist, in claim order, each with an optional drop and,
where the league bids with FAAB, an optional bid). This module only answers what the synced data says about it:

  * every claim is the add/drop simulator's own scenario (add_drop.evaluate_add_drop, on the trade analyzer's lineup
    engine): who is available, whether the drop is on this roster, roster capacity and position limits, IR-slot
    blocks, kickoff locks, the suggested drop when the roster is full and the lineup impact. No second lineup
    computation;
  * claims depend on each other: two claims dropping the same player, or more add-only claims than open roster spots,
    cannot all go through. Order decides which one is the later claim, and the later one is flagged;
  * bids are checked only where the provider supplies the rules: a FAAB league with a known budget and spend. No bid is
    ever suggested, and a bid in a league that does not bid (or whose rules are unknown) is not confirmed;
  * waiver processing rules (claim type, period, processing days) are reported as synced, or as unknown with the
    reason.

Validation is three-valued and conservative: "legal" only when the simulator evaluated the claim, the roster stays
legal and nothing the claim depends on is unknown; "unverified" when something cannot be checked (an unknown player,
unknown rules, a lock or an undroppable list we cannot see, a dependency on an earlier claim); "invalid" when the
synced data says the provider would refuse it. Freshness of the synced data is derived by the reader from the
response's signal provenance (league-scoped responses are cached per data version), so a stale sync downgrades a
"legal" claim on the page, never here. Nothing here predicts whether a claim succeeds.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .add_drop import (
    ADD_INJURY,
    ADD_NOT_AVAILABLE,
    ADD_ON_ROSTER,
    ADD_ROSTERED,
    DROP_NOT_ON_ROSTER,
    DROP_REQUIRED,
    DROP_RESERVED,
    GAME_LOCKED,
    IR_ELIGIBILITY_UNKNOWN,
    IR_SLOT_INELIGIBLE,
    LOCK_UNKNOWN,
    NFL_INELIGIBLE,
    NO_REMAINING_WEEKS,
    NO_TEAM,
    NOT_LINEUP_ELIGIBLE,
    ROSTER_UNVERIFIED,
    UNDROPPABLE_LIST,
    UNSUPPORTED_SLOT_PLAYER,
    WAIVER_CLAIM,
    AddDropContext,
    evaluate_add_drop,
)
from .trades import LeagueState

CONTRACT = "waiver-claim-plan/v1"
MAX_CLAIMS = 12
# A bid is whole currency units; anything larger is not a bid any provider accepts.
MAX_BID = 100_000

LEGAL, UNVERIFIED, INVALID = "legal", "unverified", "invalid"
INFO = "info"
_RANK = {LEGAL: 0, UNVERIFIED: 1, INVALID: 2}

# Claim-level codes added by the planner (the simulator's own codes are passed through).
UNKNOWN_PLAYER = "UNKNOWN_PLAYER"
SAME_PLAYER = "SAME_PLAYER"
DUPLICATE_ADD = "DUPLICATE_ADD"
SHARED_DROP = "SHARED_DROP"
OPEN_SPOTS = "OPEN_SPOTS"
POSITION_LIMIT = "POSITION_LIMIT"
AVAILABILITY_UNKNOWN = "AVAILABILITY_UNKNOWN"
FREE_AGENT = "FREE_AGENT"
RULES_UNKNOWN = "RULES_UNKNOWN"
BID_NOT_USED = "BID_NOT_USED"
BID_RULES_UNKNOWN = "BID_RULES_UNKNOWN"
BID_FREE_AGENT = "BID_FREE_AGENT"
BID_MISSING = "BID_MISSING"
BID_OVER_REMAINING = "BID_OVER_REMAINING"
BID_OVER_BUDGET_LEFT = "BID_OVER_BUDGET_LEFT"
BUDGET_UNKNOWN = "BUDGET_UNKNOWN"
BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
PRIORITY_UNKNOWN = "PRIORITY_UNKNOWN"

# How each simulator code bears on whether the claim is legal.
_PROBLEM_EFFECT = {ADD_ON_ROSTER: INVALID, ADD_ROSTERED: INVALID, ADD_NOT_AVAILABLE: INVALID,
                   DROP_NOT_ON_ROSTER: INVALID, DROP_REQUIRED: INVALID, NO_TEAM: UNVERIFIED,
                   NO_REMAINING_WEEKS: UNVERIFIED}
# Every simulator note is mapped; an unmapped (new) code fails safe as unverified.
_NOTE_EFFECT = {IR_SLOT_INELIGIBLE: INVALID, IR_ELIGIBILITY_UNKNOWN: UNVERIFIED, GAME_LOCKED: UNVERIFIED,
                LOCK_UNKNOWN: UNVERIFIED,
                UNDROPPABLE_LIST: UNVERIFIED, ROSTER_UNVERIFIED: UNVERIFIED, WAIVER_CLAIM: INFO, NFL_INELIGIBLE: INFO,
                NOT_LINEUP_ELIGIBLE: INFO, UNSUPPORTED_SLOT_PLAYER: INFO,
                ADD_INJURY: INFO, DROP_RESERVED: INFO}


class ClaimPlanError(ValueError):
    """A plan that cannot be read at all (too many claims, a malformed claim)."""


@dataclass(frozen=True)
class Claim:
    add: int
    drop: int | None = None
    bid: int | None = None


def parse_claim(text: str) -> Claim:
    """`ADD[:DROP[:BID]]`, empty parts for none: "301", "301:107", "301::12", "301:107:12"."""
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3 or not parts[0]:
        raise ClaimPlanError(f"A claim is ADD[:DROP[:BID]], got {text!r}.")

    def num(s: str, what: str, lo: int, hi: int) -> int | None:
        if s == "":
            return None
        try:
            v = int(s)
        except ValueError:
            raise ClaimPlanError(f"The {what} in claim {text!r} is not a whole number.") from None
        if not lo <= v <= hi:
            raise ClaimPlanError(f"The {what} in claim {text!r} is out of range.")
        return v

    ids = 10**12
    add = num(parts[0], "player id", -ids, ids)
    drop = num(parts[1], "drop id", -ids, ids) if len(parts) > 1 else None
    bid = num(parts[2], "bid", 0, MAX_BID) if len(parts) > 2 else None
    return Claim(add, drop, bid)          # type: ignore[arg-type] - add is never empty


def _money(v: float) -> str:
    return f"${v:g}"


def _item(code: str, message: str, effect: str, player_id: int | None = None) -> dict:
    return {"code": code, "message": message, "effect": effect, "player_id": player_id}


def _who(card: dict | None, pid: int | None, directory: dict) -> str:
    if card and card.get("name"):
        return card["name"]
    row = directory.get(pid) or {}
    return row.get("full_name") or f"Player {pid}"


def _plain(row: dict | None, pid: int) -> dict:
    row = row or {}
    return {"espn_player_id": pid, "name": row.get("full_name"), "position": row.get("position"),
            "pro_team": row.get("pro_team"), "injury_status": row.get("injury_status"),
            "roster_status": "unknown" if not row else "not_available",
            "availability_status": row.get("availability_status")}


def _card(card: dict | None) -> dict | None:
    if card is None:
        return None
    keep = ("espn_player_id", "name", "position", "pro_team", "injury_status", "roster_status", "availability_status",
            "team_id", "team_name", "on_ir", "on_taxi")
    return {k: card.get(k) for k in keep}


def plan_rules(ctx: AddDropContext) -> dict:
    """The acquisition rules the plan is checked against, as the provider supplies them (unknown stays unknown)."""
    facts = ctx.waivers or {}
    is_faab = ctx.rules.get("is_faab")
    budget = ctx.rules.get("faab_budget") if is_faab is True else None
    spent = ctx.team_row.get("faab_spent") if is_faab is True else None
    remaining = max(0, budget - spent) if budget is not None and spent is not None else None
    priority = ctx.team_row.get("waiver_rank") if is_faab is False else None
    kind = "FAAB" if is_faab is True else "PRIORITY" if is_faab is False else "UNKNOWN"
    unknown = []
    if kind == "UNKNOWN":
        unknown.append({"rule": "acquisition", "reason": "The synced settings do not say whether claims are bid with "
                                                         "FAAB or decided by waiver priority."})
    if kind == "FAAB" and budget is None:
        unknown.append({"rule": "faab_budget", "reason": "The synced settings do not include the FAAB budget."})
    if kind == "FAAB" and budget is not None and spent is None:
        unknown.append({"rule": "faab_spent", "reason": "The synced team data does not include FAAB spent so far."})
    if kind == "PRIORITY" and priority is None:
        unknown.append({"rule": "waiver_priority", "reason": "The synced team data does not include waiver priority."})
    for key in ("type", "period", "process_days"):
        f = facts.get(key)
        if not f or f.get("value") is None:
            unknown.append({"rule": key, "reason": (f or {}).get("reason") or "Not in the synced settings."})
    return {"acquisition": kind, "is_faab": is_faab, "faab_budget": budget, "faab_spent": spent,
            "faab_remaining": remaining, "waiver_priority": priority,
            "waiver_type": facts.get("type"), "waiver_period": facts.get("period"),
            "process_days": facts.get("process_days"), "unknown": unknown}


def _bid_items(claim: Claim, available_as: str | None, rules: dict, provider: str) -> list[dict]:
    """What the synced rules say about this claim's bid. Never a suggested amount."""
    out = []
    kind, bid, remaining = rules["acquisition"], claim.bid, rules["faab_remaining"]
    if available_as == "FREEAGENT":
        if bid is not None:
            out.append(_item(BID_FREE_AGENT, "A free agent is added without a claim, so the bid is not used.", INFO,
                             claim.add))
        return out
    if available_as != "WAIVERS":
        return out                      # not available: the simulator already says why
    if kind == "UNKNOWN":
        out.append(_item(RULES_UNKNOWN, f"Whether this claim is bid with FAAB or decided by waiver priority is not in "
                                        f"the synced settings; check {provider}.", UNVERIFIED, claim.add))
        if bid is not None:
            out.append(_item(BID_RULES_UNKNOWN, "The bid cannot be checked: the league's FAAB rules are unknown.",
                             UNVERIFIED, claim.add))
        return out
    if kind == "PRIORITY":
        if bid is not None:
            out.append(_item(BID_NOT_USED, "This league decides claims by waiver priority, not FAAB bids: remove the "
                                           "bid.", UNVERIFIED, claim.add))
        if rules["waiver_priority"] is None:
            out.append(_item(PRIORITY_UNKNOWN, "Your waiver priority is not in the synced team data.", INFO, claim.add))
        return out
    # FAAB
    if bid is None:
        out.append(_item(BID_MISSING, "No bid entered: a FAAB claim needs one.", UNVERIFIED, claim.add))
    elif remaining is None:
        out.append(_item(BUDGET_UNKNOWN, "The bid cannot be checked: the remaining FAAB budget is not in the synced "
                                         "data.", UNVERIFIED, claim.add))
    elif bid > remaining:
        out.append(_item(BID_OVER_REMAINING, f"The bid ({_money(bid)}) is more than your remaining FAAB ({_money(remaining)}).",
                         INVALID, claim.add))
    elif remaining == 0:
        out.append(_item(BUDGET_EXHAUSTED, f"Your FAAB is spent: whether {provider} accepts $0 claims is not in the "
                                           "synced settings.", UNVERIFIED, claim.add))
    return out


def _validation(items: list[dict], evaluated: bool, legal_roster: bool | None) -> str:
    worst = max((_RANK[i["effect"]] for i in items if i["effect"] in _RANK), default=0)
    if not evaluated or legal_roster is not True:
        worst = max(worst, _RANK[UNVERIFIED])
    return {v: k for k, v in _RANK.items()}[worst]


def _impact(ad: dict) -> dict | None:
    imp = ad.get("impact")
    if not imp:
        return None
    fw = imp.get("first_week") or None
    ros = imp.get("rest_of_season") or {}
    return {"first_week": {k: fw.get(k) for k in ("week", "delta", "known_delta", "projection_complete")} if fw else None,
            "rest_of_season": {k: ros.get(k) for k in ("ros_delta", "known_ros_delta", "weeks_improved",
                                                       "weeks_worsened", "weeks_incomplete", "projection_complete")}}


def evaluate_claim_plan(state: LeagueState, team_id: int | None, claims: list[Claim],
                        ctx: AddDropContext | None = None) -> dict:
    """Check `claims` (in claim order) for `team_id`. Never mutates `state`; nothing is submitted anywhere."""
    ctx = ctx or AddDropContext()
    if len(claims) > MAX_CLAIMS:
        raise ClaimPlanError(f"A plan holds at most {MAX_CLAIMS} claims.")
    if team_id is not None and team_id not in state.rosters:
        raise LookupError(f"Team {team_id} is not in this league.")
    rules = plan_rules(ctx)
    base = evaluate_add_drop(state, team_id, None, None, ctx)       # the pickers, window and roster before any claim
    open_before = base["roster"]["open_spots_before"]
    out_claims: list[dict] = []
    seen_adds: dict[int, int] = {}
    drops_used: dict[int, int] = {}
    active_before = base["roster"]["active_before"]
    spot_ranks: list[int] = []                  # claims that take an open active spot (net active gain)
    roster_now = state.rosters.get(team_id) or [] if team_id is not None else []
    pos_before = Counter(p.position for p in roster_now)
    pos_net: Counter = Counter()                # net players per position if every earlier claim succeeds
    pos_ranks: dict[str, list[int]] = {}
    spent_if_won = 0
    bid_ranks: list[int] = []                   # claims whose bid is checked as a FAAB bid and fits the budget
    for rank, claim in enumerate(claims, start=1):
        items: list[dict] = []
        ad = None
        add_card = drop_card = None
        if claim.add in seen_adds:
            items.append(_item(DUPLICATE_ADD, f"This player is already claim {seen_adds[claim.add]}.", INVALID,
                               claim.add))
        elif claim.drop is not None and claim.drop == claim.add:
            items.append(_item(SAME_PLAYER, "The same player cannot be both added and dropped.", INVALID, claim.add))
        elif claim.add not in ctx.directory:
            items.append(_item(UNKNOWN_PLAYER, "This player is not in this league's synced player data, so the claim "
                                               "cannot be checked.", UNVERIFIED, claim.add))
            add_card = _plain(None, claim.add)
        elif claim.drop is not None and claim.drop not in ctx.directory:
            items.append(_item(UNKNOWN_PLAYER, "The drop is not in this league's synced player data, so the claim "
                                               "cannot be checked.", UNVERIFIED, claim.drop))
            add_card = _plain(ctx.directory.get(claim.add), claim.add)
            drop_card = _plain(None, claim.drop)
        else:
            ad = evaluate_add_drop(state, team_id, claim.add, claim.drop, ctx)
            add_card, drop_card = ad["players"]["add"], ad["players"]["drop"]
            items += [_item(p["code"], p["message"], _PROBLEM_EFFECT.get(p["code"], INVALID), p["player_id"])
                      for p in ad["problems"]]
            items += [_item(n["code"], n["message"], _NOTE_EFFECT.get(n["code"], UNVERIFIED), n["player_id"])
                      for n in ad["notes"]]
        seen_adds.setdefault(claim.add, rank)
        available_as = state.free_agent_status.get(claim.add) if ad is not None and ad["players"]["add"] and \
            ad["players"]["add"]["roster_status"] in ("free_agent", "waivers") else None
        if available_as is not None and not ctx.waiver_status_known:
            # The provider does not say whether he is on waivers: never "no claim needed"; a bid is checked as a bid.
            items.append(_item(AVAILABILITY_UNKNOWN, f"{ctx.provider} does not report whether "
                                                     f"{_who(add_card, claim.add, ctx.directory)} is on waivers or a "
                                                     "free agent, so whether this add needs a claim is unknown.",
                               UNVERIFIED, claim.add))
            available_as, bid_as = None, "WAIVERS"
        else:
            bid_as = available_as
        if available_as == "FREEAGENT":
            items.append(_item(FREE_AGENT, f"{_who(add_card, claim.add, ctx.directory)} is a free agent: he can be "
                                           f"added in {ctx.provider} without a claim, so this place in the order is "
                                           "your preference only.", INFO, claim.add))
        items += _bid_items(claim, bid_as, rules, ctx.provider)

        # ---- dependencies on earlier claims (order decides which is the later one) ----
        evaluated_ok = ad is not None and ad["status"] == "evaluated"
        if claim.drop is not None and evaluated_ok:
            if claim.drop in drops_used:
                items.append(_item(SHARED_DROP, f"Claim {drops_used[claim.drop]} also drops "
                                                f"{_who(drop_card, claim.drop, ctx.directory)}: if it succeeds, this "
                                                "claim needs another drop.", UNVERIFIED, claim.drop))
            else:
                drops_used[claim.drop] = rank
        roster = (ad or {}).get("roster") or {}
        if evaluated_ok and active_before is not None and roster.get("active_after") is not None \
                and roster["active_after"] > active_before:
            # a net active gain (an add-only claim, or one whose drop is in an IR or taxi slot) takes an open spot
            spot_ranks.append(rank)
            if open_before is not None and len(spot_ranks) > open_before:
                items.append(_item(OPEN_SPOTS, _open_spots_text(open_before, spot_ranks[:-1]), UNVERIFIED,
                                   claim.add))
        if evaluated_ok and state.position_limits:
            delta: Counter = Counter()
            if add_card and add_card.get("position"):
                delta[add_card["position"]] += 1
            if claim.drop is not None and drop_card and drop_card.get("position"):
                delta[drop_card["position"]] -= 1
            pos_net.update(delta)
            for pos, n in delta.items():
                lim = state.position_limits.get(pos)
                if n > 0:
                    earlier = list(pos_ranks.get(pos, []))
                    pos_ranks.setdefault(pos, []).append(rank)
                    if lim is not None and pos_before[pos] + pos_net[pos] > max(lim, pos_before[pos]) and earlier:
                        items.append(_item(POSITION_LIMIT, f"The league allows at most {lim} {pos}, and claim"
                                                           f"{'s' if len(earlier) > 1 else ''} "
                                                           f"{', '.join(map(str, earlier))} also "
                                                           f"add{'' if len(earlier) > 1 else 's'} one: if "
                                                           f"{'they succeed' if len(earlier) > 1 else 'it succeeds'}, "
                                                           f"this claim needs a drop at {pos}.", UNVERIFIED, claim.add))
        if rules["acquisition"] == "FAAB" and bid_as == "WAIVERS" and claim.bid is not None and evaluated_ok \
                and (rules["faab_remaining"] is None or claim.bid <= rules["faab_remaining"]):
            bid_ranks.append(rank)              # a bid above the remaining budget is invalid: never in the total
        if rules["acquisition"] == "FAAB" and bid_as == "WAIVERS" and claim.bid is not None \
                and evaluated_ok and rules["faab_remaining"] is not None and claim.bid <= rules["faab_remaining"]:
            left = rules["faab_remaining"] - spent_if_won
            if claim.bid > left:
                items.append(_item(BID_OVER_BUDGET_LEFT, f"If the earlier bids all win, {_money(max(0, left))} of your FAAB "
                                                         f"remains, less than this bid; how {ctx.provider} handles "
                                                         "that is not in the synced settings.", UNVERIFIED, claim.add))
            spent_if_won += claim.bid

        out_claims.append({
            "rank": rank,
            "request": {"add": claim.add, "drop": claim.drop, "bid": claim.bid},
            "validation": _validation(items, evaluated_ok, roster.get("legal")),
            "reasons": items,
            "add": _card(add_card),
            "drop": _card(drop_card),
            "claim_kind": "waiver_claim" if available_as == "WAIVERS" else "free_agent" if available_as == "FREEAGENT"
            else None,
            "simulation_status": ad["status"] if ad else None,
            "roster": {k: roster.get(k) for k in ("active_after", "open_spots_after", "legal", "drop_required")},
            "suggested_drop": roster.get("suggested_drop"),
            "impact": _impact(ad) if ad else None,
        })

    counts = {v: sum(1 for c in out_claims if c["validation"] == v) for v in (LEGAL, UNVERIFIED, INVALID)}
    bids = [c["request"]["bid"] for c in out_claims if c["rank"] in bid_ranks]
    total_bid = sum(bids) if rules["acquisition"] == "FAAB" and bids else None
    shared = {}
    for c in out_claims:
        if c["request"]["drop"] is not None and c["simulation_status"] == "evaluated":
            shared.setdefault(c["request"]["drop"], []).append(c["rank"])
    conflicts = [{"kind": "shared_drop", "player_id": pid, "ranks": ranks} for pid, ranks in shared.items()
                 if len(ranks) > 1]
    if open_before is not None and len(spot_ranks) > open_before:
        conflicts.append({"kind": "open_spots", "player_id": None, "ranks": spot_ranks, "open_spots": open_before})
    for pos, ranks in sorted(pos_ranks.items()):
        lim = state.position_limits.get(pos)
        if lim is not None and len(ranks) > 1 and pos_before[pos] + pos_net[pos] > max(lim, pos_before[pos]):
            conflicts.append({"kind": "position_limit", "player_id": None, "position": pos, "limit": lim,
                              "ranks": ranks})
    return {
        "contract": CONTRACT,
        "submitted": False,
        "status": "evaluated" if claims else "empty",
        "max_claims": MAX_CLAIMS,
        "team": base["team"],
        "evaluation": base["evaluation"],
        "projection_source": base["projection_source"],
        "rules": rules,
        "roster": {"active_capacity": base["roster"]["active_capacity"],
                   "position_limits": base["roster"]["position_limits"],
                   "active_before": base["roster"]["active_before"], "open_spots_before": open_before,
                   "drop_options": base["options"]["drop"]},
        "claims": out_claims,
        "summary": {"claims": len(out_claims), **counts, "total_bid": total_bid,
                    # if every counted bid wins; unknown (never negative) when they cannot all win
                    "remaining_after_bids": (rules["faab_remaining"] - total_bid)
                    if total_bid is not None and rules["faab_remaining"] is not None
                    and total_bid <= rules["faab_remaining"] else None,
                    "conflicts": conflicts},
        "assumptions": _assumptions(ctx.provider),
        "limitations": LIMITATIONS,
    }


def _assumptions(provider: str) -> list[str]:
    return [
        f"Not submitted: nothing is claimed, added, dropped or bid, and nothing is sent to {provider}. Enter the claims "
        f"in {provider} yourself.",
        "Each claim is checked as its own move against the current roster with the add/drop simulator; claims that "
        "depend on the same drop or on the same open roster spot are flagged on the later claim.",
        "A claim is shown as passing its checks only when every rule it depends on is in the synced data; anything "
        "unknown leaves it unconfirmed.",
        "Bids are checked against the synced FAAB budget and spend. No bid is suggested.",
    ]


LIMITATIONS = [
    "Whether a claim succeeds depends on other teams' claims, priority and processing order, which are not predicted.",
    "Undroppable-list restrictions and commissioner rules are not modeled.",
    "Kickoff locks and availability are as of the last sync and the time of the check.",
]


def _open_spots_text(open_spots: int, earlier: list[int]) -> str:
    spots = "no open roster spot" if open_spots == 0 else f"{open_spots} open roster spot{'s' if open_spots != 1 else ''}"
    many = len(earlier) > 1
    return (f"You have {spots}, and claim{'s' if many else ''} {', '.join(map(str, earlier))} "
            f"{'also take one' if many else 'also takes one'}: if {'they succeed' if many else 'it succeeds'}, this "
            "claim needs a drop that frees an active spot.")
