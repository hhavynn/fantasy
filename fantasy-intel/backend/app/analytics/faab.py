"""League-aware FAAB bid guidance (issue #179). Pure: no database, no provider call, nothing is bid or submitted.

Step one is a capability audit: which FAAB, budget, bid-history, processing and priority fields the league's provider
supplied in the last sync. Anything it did not supply stays unknown, and the guidance that needs it is withheld with
the reason. Nothing here invents a bid, a market price, another manager's budget or a probability of winning a claim.

What the guidance is built from, all from the synced league:

  * the claim plan (claim_plan.evaluate_claim_plan, itself the add/drop simulator's scenario per claim): whether the
    claim needs a bid, whether the synced data says it is valid, and its rest-of-season lineup gain for this team and
    this drop (exact optimal lineups, so it already measures player opportunity against team need);
  * the remaining FAAB budget (league budget minus this team's spend), only when both are synced. An unknown budget or
    spend is never taken as 0, and then no range is given;
  * the weeks left in the evaluation window;
  * the league's executed waiver claims and their winning bids, only when the provider stored them. They are shown as
    observed facts beside the heuristic, never blended into it. Failed, cancelled and pending claims are private to
    the manager who made them and are never read.

The bid range is a heuristic with explicit inputs (method below), labelled as such. Every range is in whole currency
units and never exceeds the known remaining budget; the combined check (several bids against one budget, claims that
compete for the same drop or open spot) is evaluated across the plan, not claim by claim.

Method:
  urgency   from the rest-of-season lineup gain per remaining week: high >= HIGH_PER_WEEK, medium >= MEDIUM_PER_WEEK,
            low above 0, none at or below 0. A gain that is not fully known (a missing projection) gives no urgency;
  cap       remaining budget x max(share, 1 / weeks left): `share` is the manager's own input (the most of the remaining
            budget one claim should take), raised to an even split when few weeks remain, since FAAB has no use after
            the final week;
  range     the urgency's band of that cap (BANDS), rounded down to whole units, then raised to the synced minimum bid
            when one is known and affordable.
"""

from __future__ import annotations

import math
from itertools import combinations
from statistics import median
from typing import Any

from .league_rules import HANDLED, PARTIAL, UNSUPPORTED
from .claim_plan import INVALID, MAX_CLAIMS

CONTRACT = "waiver-faab/v1"
DEFAULT_SHARE = 25          # percent of the remaining budget one claim may take, before the even-split floor
SHARE_CHOICES = (10, 25, 50, 100)
HIGH_PER_WEEK = 3.0         # projected starting points per remaining week
MEDIUM_PER_WEEK = 1.0       # waiver_decisions / add_drop.MEANINGFUL_PER_WEEK: a meaningful forward gain
BANDS = {"high": (0.5, 1.0), "medium": (0.2, 0.5), "low": (0.0, 0.2)}
MIN_OBSERVED = 3            # winning bids at a position before the range is compared with them
RECENT_BIDS = 10

# Per-claim guidance statuses.
RANGE = "range"                     # a heuristic range is given
NO_BID_NEEDED = "no_bid_needed"     # a free agent: added without a claim
NO_GAIN = "no_gain"                 # the simulation shows no lineup gain: no bid is suggested
VALUE_UNKNOWN = "value_unknown"     # a projection is missing, so the gain is not fully known
NOT_EVALUATED = "not_evaluated"     # the simulator could not evaluate the claim (unknown player, no team)
BLOCKED = "blocked"                 # the synced data says the claim is not valid
BUDGET_EXHAUSTED = "budget_exhausted"
UNAVAILABLE = "unavailable"         # the league-level inputs are missing (not FAAB, unknown budget, no weeks left)

# Audit row statuses.
SUPPLIED, MISSING, NOT_SUPPLIED, NOT_APPLICABLE = "supplied", "missing", "not_supplied", "not_applicable"

# Where each provider keeps the field (raw provider names, for the audit's `source`).
SOURCES = {
    "espn": {
        "acquisition_type": "acquisitionSettings.isUsingAcquisitionBudget",
        "faab_budget": "acquisitionSettings.acquisitionBudget",
        "faab_spent": "teams[].transactionCounter.acquisitionBudgetSpent",
        "faab_remaining": "derived: league budget minus your spend",
        "league_spend": "teams[].transactionCounter.acquisitionBudgetSpent (every team)",
        "minimum_bid": "acquisitionSettings.minimumBid",
        "observed_bids": "transactions[].bidAmount (executed waiver claims)",
        "processing": "acquisitionSettings.waiverProcessDays",
        "next_deadline": None,
        "waiver_priority": "teams[].waiverRank",
        "waiver_status": "players[].status (WAIVERS or FREEAGENT)",
    },
    "sleeper": {
        "acquisition_type": "settings.waiver_type (2 is FAAB)",
        "faab_budget": "settings.waiver_budget",
        "faab_spent": "rosters[].settings.waiver_budget_used",
        "faab_remaining": "derived: league budget minus your spend",
        "league_spend": "rosters[].settings.waiver_budget_used (every team)",
        "minimum_bid": "settings.waiver_bid_min",
        "observed_bids": "transactions[].settings.waiver_bid (completed waiver claims)",
        "processing": "settings.waiver_day_of_week (not interpreted)",
        "next_deadline": None,
        "waiver_priority": "rosters[].settings.waiver_position",
        "waiver_status": None,
    },
}
LABELS = {
    "acquisition_type": "Claim type (FAAB or waiver priority)",
    "faab_budget": "League FAAB budget",
    "faab_spent": "Your FAAB spent",
    "faab_remaining": "Your remaining FAAB",
    "league_spend": "Other teams' FAAB spent",
    "minimum_bid": "Minimum bid",
    "observed_bids": "League winning bids",
    "processing": "Claim processing days",
    "next_deadline": "Next claim deadline",
    "waiver_priority": "Your waiver priority (FAAB tie-break)",
    "waiver_status": "Whether a player is on waivers",
}


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _money(v: float) -> str:
    return f"${v:g}"


def minimum_bid(settings: dict, provider: str) -> int | None:
    """The synced minimum bid, or None when the provider's settings do not carry it (unknown, never 0)."""
    if provider == "sleeper":
        v = _int(settings.get("waiver_bid_min"))
    else:
        acq = settings.get("acquisitionSettings") if isinstance(settings.get("acquisitionSettings"), dict) else {}
        v = _int(acq.get("minimumBid"))
    return v if v is not None and v >= 0 else None


def observed_bids(rows: list[dict], budget: float | None) -> dict:
    """Winning bids from executed waiver claims (one row per claim: bid_amount, position, scoring_period, player).
    A stored 0 is a measured 0; a claim without a stored amount is counted as missing, never as 0."""
    with_bid = [r for r in rows if _num(r.get("bid_amount")) is not None]
    out: dict[str, Any] = {"claims": len(rows), "with_bid": len(with_bid), "without_bid": len(rows) - len(with_bid)}

    def stats(bids: list[float]) -> dict:
        med = median(bids)
        return {"count": len(bids), "median": med, "min": min(bids), "max": max(bids),
                "median_share_pct": round(100 * med / budget, 1) if budget else None}
    bids = [float(r["bid_amount"]) for r in with_bid]
    out["overall"] = stats(bids) if bids else None
    by_pos: dict[str, list[float]] = {}
    for r in with_bid:
        if r.get("position"):
            by_pos.setdefault(r["position"], []).append(float(r["bid_amount"]))
    out["by_position"] = {pos: stats(v) for pos, v in sorted(by_pos.items())}
    recent = sorted(with_bid, key=lambda r: (-(r.get("scoring_period") or 0), -float(r["bid_amount"]),
                                              str(r.get("transaction_id"))))[:RECENT_BIDS]
    # Who bid is left out: the amounts are the league's public transaction log, the guidance needs no team.
    out["recent"] = [{"week": r.get("scoring_period"), "espn_player_id": r.get("espn_player_id"),
                      "name": r.get("full_name"), "position": r.get("position"), "bid": float(r["bid_amount"])}
                     for r in recent]
    return out


def capability_audit(provider: str, rules: dict, facts: dict | None, min_bid: int | None, observed: dict | None,
                     league_spend: dict, waiver_status_known: bool) -> dict:
    """Which fields the provider supplied in the synced data, and the capability flags that gate the guidance.
    `rules` is claim_plan.plan_rules (unknown stays None); `observed` is observed_bids or None when no transactions
    table was synced."""
    src = SOURCES.get(provider, SOURCES["espn"])
    kind = rules["acquisition"]
    faab = kind == "FAAB"
    rows = []

    def row(field: str, status: str, value: Any = None, display: str | None = None, reason: str | None = None):
        # The provider field is named as the source only when it supplied a value (never for a missing one).
        rows.append({"field": field, "label": LABELS[field], "status": status, "value": value, "display": display,
                     "source": src.get(field) if status == SUPPLIED else None, "reason": reason})
    na = "Not used: claims are decided by waiver priority, not FAAB." if kind == "PRIORITY" else \
        "Not used: whether claims are bid with FAAB is not in the synced settings."
    if kind == "UNKNOWN":
        row("acquisition_type", MISSING, reason="The synced settings do not say whether claims are bid with FAAB or "
                                                "decided by waiver priority.")
    else:
        row("acquisition_type", SUPPLIED, kind, "FAAB bidding" if faab else "Waiver priority")
    budget, spent, remaining = rules["faab_budget"], rules["faab_spent"], rules["faab_remaining"]
    if not faab:
        for f in ("faab_budget", "faab_spent", "faab_remaining", "league_spend", "minimum_bid", "observed_bids"):
            row(f, NOT_APPLICABLE, reason=na)
    else:
        row("faab_budget", SUPPLIED if budget is not None else MISSING, budget,
            _money(budget) if budget is not None else None,
            None if budget is not None else "The synced settings do not include the FAAB budget.")
        row("faab_spent", SUPPLIED if spent is not None else MISSING, spent,
            _money(spent) if spent is not None else None,
            None if spent is not None else "The synced team data does not include FAAB spent so far.")
        row("faab_remaining", SUPPLIED if remaining is not None else MISSING, remaining,
            _money(remaining) if remaining is not None else None,
            None if remaining is not None else "Derived from the budget and your spend; one of them is not synced.")
        n, k = league_spend["teams"], league_spend["known"]
        row("league_spend", SUPPLIED if n and k == n else MISSING,
            {"teams": n, "known": k}, f"{k} of {n} teams" if n else None,
            None if n and k == n else "Not every other team's FAAB spend is in the synced team data.")
        row("minimum_bid", SUPPLIED if min_bid is not None else MISSING, min_bid,
            _money(min_bid) if min_bid is not None else None,
            None if min_bid is not None else "The synced settings do not include a minimum bid, so whether a $0 bid "
                                            "is accepted is unknown.")
        if observed is None:
            row("observed_bids", MISSING, reason="No league transactions are in the synced data.")
        elif observed["with_bid"]:
            row("observed_bids", SUPPLIED, observed["with_bid"],
                f"{observed['with_bid']} executed waiver claim{'s' if observed['with_bid'] != 1 else ''}",
                (f"{observed['without_bid']} executed waiver claims have no stored bid." if observed["without_bid"]
                 else None))
        elif observed["claims"]:
            row("observed_bids", NOT_SUPPLIED, reason=f"{observed['claims']} executed waiver claims are synced, but "
                                                      "none carries a bid amount.")
        else:
            row("observed_bids", MISSING, reason="No waiver claim has been processed this season in the synced data.")
    proc = (facts or {}).get("process_days") or {}
    row("processing", SUPPLIED if proc.get("value") is not None else MISSING, proc.get("value"), proc.get("display"),
        None if proc.get("value") is not None else proc.get("reason") or "Not in the synced settings.")
    row("next_deadline", NOT_SUPPLIED, reason="The synced data does not include when claims next process; check the "
                                              "provider before the deadline.")
    prio = rules.get("waiver_priority_any")
    row("waiver_priority", SUPPLIED if prio is not None else MISSING, prio, f"#{prio}" if prio is not None else None,
        "The synced settings do not say how tied bids are broken." if prio is not None
        else "The synced team data does not include waiver priority.")
    row("waiver_status", SUPPLIED if waiver_status_known else NOT_SUPPLIED,
        reason=None if waiver_status_known else "The sync records every unrostered player as a free agent, so whether "
                                                 "an add needs a claim (and a bid) is unknown.")

    reasons = []
    if kind == "PRIORITY":
        guidance = "not_applicable"
        reasons.append("This league decides claims by waiver priority, so there is nothing to bid.")
    elif kind == "UNKNOWN":
        guidance = "unavailable"
        reasons.append("Whether this league bids with FAAB is not in the synced settings.")
    elif remaining is None:
        guidance = "unavailable"
        reasons.append("Your remaining FAAB is unknown (the budget or your spend is not synced); it is not assumed "
                       "to be 0.")
    else:
        guidance = "available"
    if not faab:
        obs = "not_applicable" if kind == "PRIORITY" else "unavailable"
    elif observed is None:
        obs = "unavailable"
    elif observed["with_bid"]:
        obs = "available"
    elif observed["claims"]:
        obs = "unavailable"
        reasons.append("The synced waiver claims carry no bid amounts, so there is no league bid history.")
    else:
        obs = "none_yet"
        reasons.append("No waiver claim has been processed this season, so there is no league bid history yet.")
    flag = (HANDLED if guidance == "available" and obs == "available" else
            PARTIAL if guidance == "available" else UNSUPPORTED)
    return {"flag": flag, "guidance": guidance, "observed_bids": obs, "reasons": reasons, "fields": rows}


def urgency(gain: float | None, weeks: int) -> str | None:
    if gain is None or weeks <= 0:
        return None
    per = gain / weeks
    return "high" if per >= HIGH_PER_WEEK else "medium" if per >= MEDIUM_PER_WEEK else "low" if per > 0 else "none"


def bid_range(remaining: float, weeks: int, share: int, tier: str, min_bid: int | None) -> dict | None:
    """The heuristic range for one claim, never above `remaining`. None when no affordable bid exists."""
    frac = max(share / 100, 1 / weeks)
    cap = remaining * min(1.0, frac)
    lo, hi = BANDS[tier]
    low, high = math.floor(cap * lo + 1e-9), math.floor(cap * hi + 1e-9)
    if min_bid is not None:
        if min_bid > remaining:
            return None
        low, high = max(low, min_bid), max(high, min_bid)
    high = min(high, math.floor(remaining + 1e-9))
    low = min(low, high)
    return {"low": low, "high": high, "cap": round(cap, 2), "cap_share_pct": round(100 * min(1.0, frac), 1),
            "band_pct": [round(100 * lo), round(100 * hi)]}


def _relation(rng: dict, stats: dict | None) -> str | None:
    if not stats or stats["count"] < MIN_OBSERVED:
        return None
    m = stats["median"]
    return "below" if rng["high"] < m else "above" if rng["low"] > m else "around"


_BID_CODES = {"BID_MISSING", "BID_NOT_USED", "BID_RULES_UNKNOWN", "BID_FREE_AGENT", "BID_OVER_REMAINING",
              "BID_OVER_BUDGET_LEFT", "BUDGET_UNKNOWN", "BUDGET_EXHAUSTED"}


def _claim(c: dict, cap: dict, remaining: float | None, weeks: int, share: int, min_bid: int | None,
           observed: dict | None, provider: str) -> dict:
    add = c.get("add") or {}
    pos = add.get("position")
    ros = ((c.get("impact") or {}).get("rest_of_season") or {})
    gain = ros.get("ros_delta")
    out: dict[str, Any] = {
        "rank": c["rank"], "add": {k: add.get(k) for k in ("espn_player_id", "name", "position")},
        "drop": c["request"]["drop"], "claim_kind": c["claim_kind"], "validation": c["validation"],
        "status": None, "urgency": None, "range": None, "reason": None, "notes": [],
        "basis": {"ros_gain": gain, "known_ros_gain": ros.get("known_ros_delta"), "weeks": weeks,
                  "gain_per_week": round(gain / weeks, 2) if gain is not None and weeks else None,
                  "share_pct": share},
        "observed": None,
    }
    problems = [r for r in c["reasons"] if r["effect"] == INVALID and r["code"] not in _BID_CODES]
    if c["validation"] == INVALID:
        out["status"] = BLOCKED
        out["reason"] = (problems[0]["message"] if problems else "The synced data says this claim is not valid.") + \
            " No bid is suggested."
        return out
    if c["claim_kind"] == "free_agent":
        out["status"], out["reason"] = NO_BID_NEEDED, f"A free agent is added in {provider} without a claim or a bid."
        return out
    if cap["guidance"] != "available":
        out["status"], out["reason"] = UNAVAILABLE, " ".join(cap["reasons"][:1]) or "There is no FAAB guidance for this claim."
        return out
    if c["simulation_status"] != "evaluated":
        why = next((r["message"] for r in c["reasons"] if r["effect"] != "info" and r["code"] not in _BID_CODES), None)
        out["status"] = NOT_EVALUATED
        out["reason"] = (why or "The add/drop simulator could not evaluate this claim.") + " No bid is suggested."
        return out
    if c["claim_kind"] is None:
        out["notes"].append({"code": "AVAILABILITY_UNKNOWN",
                             "message": f"{provider} does not report whether he is on waivers; if he is a free agent "
                                        "he needs no bid."})
    stats = (observed or {}).get("by_position", {}).get(pos) if observed else None
    if stats:
        out["observed"] = {"position": pos, **stats}
    if weeks <= 0:
        out["status"], out["reason"] = UNAVAILABLE, "No scoring weeks remain, so a claim cannot change a lineup."
        return out
    tier = urgency(gain, weeks)
    out["urgency"] = tier
    if tier is None:
        known = ros.get("known_ros_delta")
        out["status"] = VALUE_UNKNOWN
        out["reason"] = ("A projection is missing in some remaining week, so the lineup gain is not fully known"
                         + (f" (the known weeks alone add {known:+.1f} points)" if isinstance(known, (int, float)) else "")
                         + ". An unknown gain is not 0: no range is given.")
        return out
    if tier == "none":
        out["status"] = NO_GAIN
        out["reason"] = "The simulation shows no lineup gain over the remaining weeks, so no bid is suggested."
        return out
    if remaining is not None and remaining <= 0:
        out["status"] = BUDGET_EXHAUSTED
        if min_bid == 0:
            out["range"] = {"low": 0, "high": 0, "cap": 0.0, "cap_share_pct": 0.0, "band_pct": [0, 0]}
            out["reason"] = "Your FAAB is spent: only a $0 bid is possible."
        elif min_bid is not None:
            out["reason"] = f"Your FAAB is spent and the minimum bid is {_money(min_bid)}: no bid is possible."
        else:
            out["reason"] = (f"Your FAAB is spent: whether {provider} accepts a $0 bid is not in the synced "
                             "settings.")
        return out
    rng = bid_range(remaining, weeks, share, tier, min_bid)
    if rng is None:
        out["status"] = BUDGET_EXHAUSTED
        out["reason"] = f"The minimum bid ({_money(min_bid)}) is more than your remaining FAAB."
        return out
    out["status"], out["range"] = RANGE, rng
    if rng["low"] == 0 and min_bid is None:
        out["notes"].append({"code": "ZERO_BID_UNKNOWN",
                             "message": f"Whether {provider} accepts a $0 bid is not in the synced settings."})
    if out["observed"] is not None:
        out["observed"]["relation"] = _relation(rng, stats)
    return out


def _max_spend(highs: dict[int, int], groups: list[tuple[list[int], int]]) -> int:
    """The most the ranged claims can spend if they all win where they can: claims that share a drop or the open
    roster spots cannot all go through (each group allows at most `capacity` of its claims). Exact, over subsets."""
    ranks = sorted(highs)
    best = 0
    for n in range(len(ranks), -1, -1):
        for subset in combinations(ranks, n):
            s = set(subset)
            if all(len(s & set(g)) <= capacity for g, capacity in groups):
                best = max(best, sum(highs[r] for r in subset))
        if best == sum(highs.values()):
            break
    return best


def combined(claims: list[dict], conflicts: list[dict], remaining: float | None) -> dict:
    """Several bids against one budget: the most the suggested ranges could spend together (claims that share a drop
    or an open spot cannot all win), whether that fits the remaining budget, and each range's top scaled to fit if not.
    Claims without a range are not counted, and the total says so (an unknown bid is not 0)."""
    ranged = {c["rank"]: c["range"]["high"] for c in claims if c["status"] == RANGE}
    groups = []
    for k in conflicts:
        if k["kind"] == "shared_drop":
            groups.append((k["kind"], list(k["ranks"]), 1))
        elif k["kind"] == "open_spots" and k.get("open_spots") is not None:
            groups.append((k["kind"], list(k["ranks"]), int(k["open_spots"])))
    groups = [(kind, g, cap) for kind, g, cap in groups if len(set(g) & set(ranged)) > cap]
    unranged = [c["rank"] for c in claims if c["status"] in (VALUE_UNKNOWN, NOT_EVALUATED)]
    if not ranged:
        return {"ranged_claims": 0, "high_total": None, "max_spend_if_won": None, "remaining": remaining,
                "fits": None, "complete": not unranged, "unranged_ranks": unranged, "exclusive_groups": [],
                "scaled": False}
    spend = _max_spend(ranged, [(g, cap) for _, g, cap in groups])
    fits = spend <= remaining if remaining is not None else None
    factor = remaining / spend if fits is False and spend else 1.0
    for c in claims:
        if c["status"] == RANGE:
            hi = math.floor(c["range"]["high"] * factor + 1e-9) if fits is False else c["range"]["high"]
            c["if_all_win"] = {"low": min(c["range"]["low"], hi), "high": hi}
    return {"ranged_claims": len(ranged), "high_total": sum(ranged.values()), "max_spend_if_won": spend,
            "remaining": remaining, "fits": fits, "complete": not unranged, "unranged_ranks": unranged,
            "exclusive_groups": [{"kind": kind, "ranks": g, "capacity": cap} for kind, g, cap in groups],
            "scaled": fits is False}


def league_spend(teams: list[dict], team_id: int | None, budget: float | None) -> dict:
    """Other teams' synced FAAB spend: how many are known, and the most any of them has left (only when every one is
    known: a partial maximum would understate it). No team is named."""
    others = [t for t in teams if t.get("team_id") != team_id]
    known = [_num(t.get("faab_spent")) for t in others if _num(t.get("faab_spent")) is not None]
    complete = bool(others) and len(known) == len(others)
    return {"teams": len(others), "known": len(known),
            "max_remaining": max(0.0, max(budget - s for s in known)) if complete and budget is not None else None}


def evaluate_faab(plan: dict, *, provider: str, provider_label: str, settings: dict, team_row: dict,
                  teams: list[dict], observed_rows: list[dict] | None, share: int = DEFAULT_SHARE,
                  waiver_status_known: bool = True) -> dict:
    """FAAB guidance for a checked claim plan (claim_plan.evaluate_claim_plan, bids ignored). `observed_rows`: one
    row per executed waiver claim this season, or None when the league has no synced transactions table."""
    if not 1 <= share <= 100:
        raise ValueError("share is a whole percent from 1 to 100")
    if len(plan["claims"]) > MAX_CLAIMS:
        raise ValueError(f"A plan holds at most {MAX_CLAIMS} claims.")
    rules = dict(plan["rules"])
    rules["waiver_priority_any"] = team_row.get("waiver_rank")
    faab = rules["acquisition"] == "FAAB"
    min_bid = minimum_bid(settings, provider) if faab else None
    budget = _num(rules["faab_budget"])
    remaining = _num(rules["faab_remaining"])
    observed = observed_bids(observed_rows, budget) if faab and observed_rows is not None else None
    spend = league_spend(teams, plan["team"]["id"], budget) if faab else {"teams": 0, "known": 0, "max_remaining": None}
    cap = capability_audit(provider, rules, {"process_days": rules.get("process_days")}, min_bid, observed, spend,
                           waiver_status_known)
    weeks = len(plan["evaluation"]["weeks"] or [])
    claims = [_claim(c, cap, remaining, weeks, share, min_bid, observed if cap["observed_bids"] == "available" else None,
                     provider_label) for c in plan["claims"]]
    totals = combined(claims, plan["summary"]["conflicts"], remaining) if cap["guidance"] == "available" else None
    return {
        "contract": CONTRACT,
        "submitted": False,
        "status": "evaluated" if plan["claims"] else "empty",
        "team": plan["team"],
        "evaluation": plan["evaluation"],
        "projection_source": plan["projection_source"],
        "capability": cap,
        "inputs": {"share_pct": share, "share_choices": list(SHARE_CHOICES), "default_share_pct": DEFAULT_SHARE,
                   "high_per_week": HIGH_PER_WEEK, "medium_per_week": MEDIUM_PER_WEEK,
                   "bands_pct": {k: [round(100 * a), round(100 * b)] for k, (a, b) in BANDS.items()},
                   "min_observed": MIN_OBSERVED},
        "budget": {"acquisition": rules["acquisition"], "budget": budget if faab else None,
                   "spent": _num(rules["faab_spent"]) if faab else None, "remaining": remaining if faab else None,
                   "minimum_bid": min_bid, "weeks_left": weeks,
                   "even_split_per_week": round(remaining / weeks, 2) if faab and remaining is not None and weeks
                   else None,
                   "other_teams": spend},
        "observed": observed if cap["observed_bids"] == "available" else None,
        "claims": claims,
        "combined": totals,
        "assumptions": _assumptions(provider_label, share),
        "limitations": LIMITATIONS,
    }


def _assumptions(provider: str, share: int) -> list[str]:
    return [
        f"Not submitted: nothing is bid, claimed or sent to {provider}. Enter any bid in {provider} yourself.",
        "Ranges are a heuristic, not a market price or a prediction: urgency comes from the add/drop simulator's "
        f"rest-of-season lineup gain per remaining week (high at {HIGH_PER_WEEK:g} or more projected points, medium "
        f"at {MEDIUM_PER_WEEK:g} or more, low above 0).",
        f"Budget share: one claim may take up to {share}% of your remaining FAAB (your input), or an even split of it "
        "over the remaining weeks when that is larger. High urgency is 50-100% of that cap, medium 20-50%, low 0-20%, "
        "rounded down to whole dollars.",
        "Every range stays within your remaining FAAB from the last sync; an unknown budget or spend gives no range.",
        "League winning bids are shown as observed, beside the heuristic, and never change it. Losing and pending bids "
        "are private to each manager and are not read.",
    ]


LIMITATIONS = [
    "Other managers' bids, needs and strategies are unknown; no claim-success probability is given.",
    "Claims you have already entered in the provider, and any spend since the last sync, are not visible here.",
    "Projections can be wrong, and the lineup gain assumes the rest of the roster stays as it is.",
]
