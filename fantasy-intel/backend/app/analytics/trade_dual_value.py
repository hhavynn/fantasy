"""Dual-value trade assessment: why a trade is or is not sensible for BOTH managers. Pure (no database).
See docs/trade_discovery_dual_value.md.

Three signals are kept apart and never added together:

  A. asset value       ESPN points above replacement per traded player (trade_assets.AssetBoard): what
                       each manager gives up as an asset, independent of whether he starts for his
                       current team. Package labels and thresholds are trade_assets' own.
  B. lineup benefit    the canonical rest-of-season optimal-lineup simulation (trades.evaluate_trade,
                       reused verbatim through trade_discovery.detail): starters only, forced drops
                       resolved, missing projections never 0.
  C. plausibility      six explicit checks (lineup gain for both, asset balance, positional fit,
                       depth cost, the anchor's free-agent alternative, uncertainty), each pass /
                       concern / fail / unknown with its reason, and a verdict derived from them by
                       the fixed rule in `verdict`.

Ranking (`order_key`) is a conservative policy over those parts: verdict tier first, then Pareto
fronts over (smaller-side lineup gain, asset-package ratio, certainty). Pareto dominance compares each
axis only with itself, so fantasy points, a dimensionless ratio and a count are never mixed into one
score. Nothing here is an acceptance estimate.
"""

from __future__ import annotations

from .trade_assets import AssetBoard

PHASE_KEYS = {"ros": "ros_delta", "regular": "regular_season_delta", "playoff": "playoff_delta"}
PASS, CONCERN, FAIL, UNKNOWN, NOT_APPLICABLE = "pass", "concern", "fail", "unknown", "not_applicable"

# Verdicts in display/ranking order (a lower index ranks first). Text placeholders are filled by `verdict`.
VERDICTS = {
    "sensible": "Sensible for both managers: both starting lineups improve, the asset packages are comparable and "
                "nothing major is given up.",
    "sensible_with_concerns": "Both starting lineups improve, with concerns to weigh.",
    "unlikely_mutual": "Both starting lineups improve, but the asset packages are lopsided: unlikely to be mutually "
                       "attractive.",
    "fair_no_lineup_gain": "Asset-fair exchange, but {who} starting lineup does not improve.",
    "one_sided": "One-sided: {who} starting lineup does not improve and the asset packages are not balanced.",
    "incomplete": "Incomplete: missing projections or asset data, so it is not ranked against complete trades.",
}
VERDICT_ORDER = {v: i for i, v in enumerate(VERDICTS)}
CRITERIA = {
    "lineup_gain": "Both projected starting lineups improve (starters only; bench points never count).",
    "asset_balance": "The asset packages are reasonably balanced (points above replacement).",
    "positional_fit": "Each side receives a player who starts at least half of the remaining weeks.",
    "depth_cost": "No starter-caliber player is forced off a roster and no starting slot is left empty.",
    "free_agent_alternative": "The trade adds more than the best available free agent would (your side).",
    "uncertainty": "No injury, eligibility or missing-projection uncertainty on the traded players.",
}
IMBALANCE = {"balanced": "none", "replacement_level": "none", "they_give_more": "moderate",
             "you_give_more": "moderate", "significant_they_give_more": "significant",
             "significant_you_give_more": "significant", "they_surrender_multiple_strong": "significant",
             "you_surrender_multiple_strong": "significant", "insufficient_data": None}
RANKING_POLICY = (
    "Trades are ordered by verdict (sensible, sensible with concerns, unlikely to be mutually attractive, asset-fair "
    "without a lineup gain, one-sided, incomplete), then by Pareto front over three separately measured axes: the "
    "smaller side's projected starting-lineup gain (points), the asset-package ratio (smaller package as a share of "
    "the larger, points above replacement) and certainty (fewer injury and missing-data flags). A trade is in a later "
    "front only when another trade is at least as good on all three and better on one. Within a front the view's "
    "own measure decides (e.g. the smaller side's gain), then the exact search order. No axis is converted into "
    "another and nothing estimates whether a manager would accept.")


def _delta(side: dict, phase: str):
    return side["deltas"][PHASE_KEYS[phase]]


def _starts(u: dict) -> int:
    return len(u["weeks_started"])


def possessive(label: str) -> str:
    return {"You": "your", "They": "their"}.get(label, f"{label}'s")


def _name(x: dict | None, pid: int | None = None) -> str:
    return (x or {}).get("name") or f"#{pid if pid is not None else (x or {}).get('espn_player_id')}"


# --------------------------------------------------------------------------------- A. asset --

def _player_asset(board: AssetBoard, pid: int, from_label: str, to_label: str, before: dict | None,
                  after: dict | None, n: int) -> dict:
    pr = board.profiles.get(pid) or {}
    sb = _starts(before) if before else None          # starts for his current team (before the trade)
    sa = _starts(after) if after else None            # starts for the receiving team (after; None = forced drop)
    par = pr.get("par")
    row = {k: pr.get(k) for k in ("espn_player_id", "name", "position", "par", "known_par", "par_regular", "par_playoff",
                                  "position_rank", "position_rank_of", "league_starters_at_position",
                                  "starter_caliber", "missing_weeks", "nfl_ineligible_weeks", "replacement_per_week")}
    row.update(espn_player_id=pid, from_side=from_label, to_side=to_label, starts_before=sb, starts_after=sa, weeks=n,
               role_before=_role(sb, n), role_after=_role(sa, n))
    # A strong bench asset keeps its asset value even when it would not start for the receiver.
    row["bench_asset"] = bool(par is not None and par > 0 and sa is not None and sa * 2 < n)
    return row


def _role(starts: int | None, n: int) -> str | None:
    if starts is None or not n:
        return None
    return "starter" if starts * 2 >= n else "part-time" if starts else "bench"


def asset_block(board: AssetBoard, mine: dict, theirs: dict, sends: list[int], receives: list[int],
                labels: tuple[str, str]) -> dict:
    you, them = labels
    n = len(mine["weekly"])
    pkg = board.package(sends, receives)
    out_me = {u["espn_player_id"]: u for u in mine["outgoing_usage"]}
    in_me = {u["espn_player_id"]: u for u in mine["incoming_usage"]}
    out_them = {u["espn_player_id"]: u for u in theirs["outgoing_usage"]}
    in_them = {u["espn_player_id"]: u for u in theirs["incoming_usage"]}
    send_rows = [_player_asset(board, p, you, them, out_me.get(p), in_them.get(p), n) for p in sends]
    recv_rows = [_player_asset(board, p, them, you, out_them.get(p), in_me.get(p), n) for p in receives]

    def best(rows):
        known = [x for x in rows if x["par"] is not None]
        if len(known) != len(rows) or not known:
            return None
        b = max(known, key=lambda x: (x["par"], -x["espn_player_id"]))
        return {k: b[k] for k in ("espn_player_id", "name", "position", "par", "position_rank", "starter_caliber")}

    lab = pkg["label"]
    imbalance = IMBALANCE.get(lab)
    favours = None
    if imbalance in ("moderate", "significant"):
        favours = you if lab.startswith(("they_", "significant_they_")) else them
    return {
        "label": lab, "text": pkg["text"], "ratio": pkg["ratio"], "imbalance": imbalance, "favours": favours,
        "you_send_par": pkg["you_send_par"], "they_send_par": pkg["they_send_par"],
        "per_week": pkg.get("per_week"), "caveats": pkg["caveats"],
        "you_send": send_rows, "you_receive": recv_rows,
        "best_asset_surrendered": {"you": best(send_rows), "them": best(recv_rows)},
        "bench_assets": [x["espn_player_id"] for x in send_rows + recv_rows if x["bench_asset"]],
        "depth": {"you": _depth(board, mine, len(receives) - len(sends), send_rows),
                  "them": _depth(board, theirs, len(sends) - len(receives), recv_rows)},
        "note": pkg["note"],
    }


def _depth(board: AssetBoard, side: dict, net_players: int, outgoing: list[dict]) -> dict:
    """Roster depth cost for one side: the players it ends with, its forced drops valued as assets,
    and the asset value it surrenders from players who did not start for it most weeks."""
    drops = []
    for d in side["forced_drops"]:
        pr = board.profiles.get(d["espn_player_id"]) or {}
        drops.append({"espn_player_id": d["espn_player_id"], "name": d.get("name") or pr.get("name"),
                      "position": d.get("position") or pr.get("position"), "par": pr.get("par"),
                      "starter_caliber": bool(pr.get("starter_caliber"))})
    drop_par = None if any(x["par"] is None for x in drops) else round(sum(x["par"] for x in drops), 2)
    bench_out = [x for x in outgoing if x["role_before"] != "starter"]
    bench_par = None if any(x["par"] is None for x in bench_out) else round(sum(x["par"] for x in bench_out), 2)
    kind = "consolidates" if net_players < 0 else "adds depth" if net_players > 0 else "even"
    return {"net_players": net_players, "kind": kind, "forced_drops": drops, "forced_drop_par": drop_par,
            "forced_drop_lineup_cost": side.get("forced_drop_lineup_cost"),
            "outgoing_non_starter_par": bench_par,
            "open_roster_spots_after": side.get("open_active_roster_spots"),
            "empty_slots_after": side.get("empty_slots_after") or []}


# ------------------------------------------------------------------------------- B. lineup --

def _side_lineup(side: dict, label: str, n: int) -> dict:
    pts = (("ros", "ros_projected_starting_points"), ("regular", "regular_season_projected_starting_points"),
           ("playoff", "playoff_projected_starting_points"))
    d = side["deltas"]
    promoted = [{"espn_player_id": u["espn_player_id"], "name": u["name"], "position": u["position"],
                 "weeks": u["weeks_started"], "incoming": True} for u in side["incoming_usage"] if u["weeks_started"]]
    promoted += [{"espn_player_id": x["espn_player_id"], "name": x["name"], "position": x["position"],
                  "weeks": x["starts_gained"], "incoming": False} for x in side["displaced"] if x["starts_gained"]]
    displaced = [{"espn_player_id": x["espn_player_id"], "name": x["name"], "position": x["position"],
                  "weeks": x["starts_lost"]} for x in side["displaced"] if x["starts_lost"]]
    return {
        "label": label, "team_id": side["team_id"], "team_name": side["team_name"],
        "before": {k: side["before"][v] for k, v in pts}, "after": {k: side["after"][v] for k, v in pts},
        "delta": {"ros": d["ros_delta"], "regular": d["regular_season_delta"], "playoff": d["playoff_delta"],
                  "known_ros": d["known_ros_delta"]},
        "average_weekly_delta": d["average_weekly_delta"],
        "weeks_improved": d["weeks_improved"], "weeks_worsened": d["weeks_worsened"],
        "weeks_unchanged": d["weeks_unchanged"],
        "promoted": promoted, "displaced": displaced,
        "forced_drops": [{"espn_player_id": x["espn_player_id"], "name": x.get("name"), "position": x.get("position")}
                         for x in side["forced_drops"]],
        "forced_drop_lineup_cost": side.get("forced_drop_lineup_cost"),
        "missing": {"weeks": side["incomplete"]["missing_weeks"], "players": side["incomplete"]["missing_players"],
                    "weeks_incomplete": d["weeks_incomplete"]},
        "weekly": side["weekly"],
        "key_change": _key_change(side, label, n),
    }


def _key_change(side: dict, label: str, n: int) -> str:
    """The single most important projected lineup change for one side, in words."""
    inc = sorted(side["incoming_usage"], key=lambda u: (-_starts(u), u["espn_player_id"]))
    lost = sorted((x for x in side["displaced"] if x["starts_lost"]), key=lambda x: (-len(x["starts_lost"]), x["espn_player_id"]))
    out = sorted(side["outgoing_usage"], key=lambda u: (-_starts(u), u["espn_player_id"]))
    whose = possessive(label)
    if inc and _starts(inc[0]):
        u = inc[0]
        text = f"{_name(u)} ({u['position']}) starts {_starts(u)} of {n} weeks in {whose} lineup"
        if lost:
            x = lost[0]
            text += f", replacing {_name(x)} ({x['position']}) in {len(x['starts_lost'])}"
        elif out and _starts(out[0]):
            text += f" in place of {_name(out[0])} ({out[0]['position']})"
        return text + "."
    if out and _starts(out[0]):
        u = out[0]
        return f"{whose.capitalize()} lineup loses {_name(u)} ({u['position']}), who started {_starts(u)} of {n} weeks; " \
               f"no incoming player starts."
    return f"No incoming player starts in {whose} lineup; the change is depth only."


def lineup_block(mine: dict, theirs: dict, labels: tuple[str, str]) -> dict:
    n = len(mine["weekly"])
    you, them = _side_lineup(mine, labels[0], n), _side_lineup(theirs, labels[1], n)
    return {"you": you, "them": them, "weeks": n,
            "basis": "Only projected starters count toward the lineup delta; bench players are depth and asset value, "
                     "shown separately, never added as starting points."}


# ------------------------------------------------------------------------- C. plausibility --

def _check(criterion: str, status: str, text: str) -> dict:
    return {"criterion": criterion, "status": status, "text": text}


def checks(mine: dict, theirs: dict, asset: dict, injuries: list[dict], phase: str, fa: dict | None,
           labels: tuple[str, str]) -> list[dict]:
    you, them = labels
    n = len(mine["weekly"])
    out = []
    m, t = _delta(mine, phase), _delta(theirs, phase)
    span = {"ros": "rest of season", "regular": "regular season", "playoff": "fantasy playoffs"}[phase]
    if m is None or t is None:
        out.append(_check("lineup_gain", UNKNOWN, f"Missing ESPN projections: no complete {span} lineup total."))
    elif m > 0 and t > 0:
        out.append(_check("lineup_gain", PASS, f"Both starting lineups improve ({span}: {you} {m:+.1f}, {them} {t:+.1f})."))
    else:
        losers = [lab for lab, v in ((you, m), (them, t)) if v <= 0]
        text = " and ".join(possessive(x) for x in losers) + f" starting lineup does not improve ({span}: {m:+.1f} / {t:+.1f})."
        out.append(_check("lineup_gain", FAIL, text[0].upper() + text[1:]))

    imb = asset["imbalance"]
    if imb is None:
        out.append(_check("asset_balance", UNKNOWN, asset["text"] + "."))
    elif imb == "none":
        out.append(_check("asset_balance", PASS, asset["text"] + "."))
    else:
        out.append(_check("asset_balance", FAIL if imb == "significant" else CONCERN,
                          f"{asset['text']} ({asset['you_send_par']:.0f} vs {asset['they_send_par']:.0f} points above "
                          "replacement)."))

    fit = []
    for lab, side in ((you, mine), (them, theirs)):
        best = max((_starts(u) for u in side["incoming_usage"]), default=0)
        fit.append((lab, best))
    weak = [(lab, k) for lab, k in fit if k * 2 < n]
    if not weak:
        out.append(_check("positional_fit", PASS, "Each side receives a player who starts at least half the remaining weeks."))
    else:
        none = [lab for lab, k in weak if k == 0]
        text = "; ".join(f"{lab}: the best incoming player starts {k} of {n} weeks" for lab, k in weak) + "."
        out.append(_check("positional_fit", FAIL if none else CONCERN, text))

    big, small, unknown = [], [], []
    for lab, dep in ((you, asset["depth"]["you"]), (them, asset["depth"]["them"])):
        for d in dep["forced_drops"]:
            who = f"{lab} must drop {_name(d)} ({d['position']})"
            if d["starter_caliber"]:
                big.append(f"{who}, a starter-caliber player ({d['par']:.0f} above replacement)")
            elif d["par"] is None:
                unknown.append(f"{who} (asset value unknown)")
            elif d["par"] > 0:
                small.append(f"{who} ({d['par']:.0f} above replacement)")
        if dep["empty_slots_after"]:
            weeks = sorted({h["week"] for h in dep["empty_slots_after"]})
            big.append(f"{lab} would have an empty starting slot in week(s) {', '.join(map(str, weeks))}")
    if big:
        out.append(_check("depth_cost", FAIL, "; ".join(big) + "."))
    elif small or unknown:
        out.append(_check("depth_cost", CONCERN if small else UNKNOWN, "; ".join(small + unknown) + "."))
    else:
        out.append(_check("depth_cost", PASS, "No forced drop of a player with asset value and no empty starting slot."))

    if fa is None:
        out.append(_check("free_agent_alternative", NOT_APPLICABLE, "No free-agent comparison for this view."))
    elif fa.get("trade_advantage") is None:
        out.append(_check("free_agent_alternative", UNKNOWN,
                          "The free-agent comparison is unavailable (no free agent helps, or projections are missing)."))
    elif fa["trade_advantage"] <= 0 or fa.get("has_concern"):
        out.append(_check("free_agent_alternative", FAIL if fa["trade_advantage"] <= 0 else CONCERN,
                          fa.get("lead") or "Most of this gain is available from free agency."))
    else:
        out.append(_check("free_agent_alternative", PASS,
                          f"Even after the best free-agent move, the trade still adds {fa['trade_advantage']:+.1f} for you."))

    missing = sorted(set(mine["incomplete"]["missing_players"]) | set(theirs["incomplete"]["missing_players"]))
    if injuries:
        out.append(_check("uncertainty", CONCERN, " ".join(f["warnings"][0] for f in injuries if f.get("warnings"))
                          or "A traded player has an injury or eligibility flag."))
    elif missing:
        out.append(_check("uncertainty", UNKNOWN, f"{len(missing)} rostered player(s) lack ESPN projections in some week."))
    else:
        out.append(_check("uncertainty", PASS, "No injury, eligibility or projection gaps on the traded players."))
    return out


def verdict(cs: list[dict], labels: tuple[str, str], lineup_losers: list[str]) -> tuple[str, str]:
    """The fixed rule from checks to a verdict (docs/trade_discovery_dual_value.md, "Verdicts")."""
    st = {c["criterion"]: c["status"] for c in cs}
    if st["lineup_gain"] == UNKNOWN or st["asset_balance"] == UNKNOWN:
        v = "incomplete"
    elif st["lineup_gain"] == PASS:
        if st["asset_balance"] == FAIL:
            v = "unlikely_mutual"
        elif any(st[k] in (CONCERN, FAIL, UNKNOWN) for k in CRITERIA if k not in ("lineup_gain",)):
            v = "sensible_with_concerns"
        else:
            v = "sensible"
    elif st["asset_balance"] == PASS:
        v = "fair_no_lineup_gain"
    else:
        v = "one_sided"
    who = " and ".join(possessive(x) for x in lineup_losers) or "a"
    return v, VERDICTS[v].format(who=who)


def assess(mine: dict, theirs: dict, sends: list[int], receives: list[int], board: AssetBoard,
           injuries: list[dict] | None = None, phase: str = "ros", fa: dict | None = None,
           labels: tuple[str, str] = ("You", "They")) -> dict:
    """The dual-value assessment of one evaluated trade.

    `mine` / `theirs` are trade_discovery._side_detail views of trades.evaluate_trade's team_a /
    team_b (the anchor sends `sends` and receives `receives`); `fa` is the anchor's free-agent
    comparison (trade_practical._anchor_opportunity_cost) or None. The canonical deltas are copied,
    never recomputed."""
    if phase not in PHASE_KEYS:
        raise ValueError(f"phase must be one of {', '.join(PHASE_KEYS)}")
    injuries = injuries or []
    asset = asset_block(board, mine, theirs, sends, receives, labels)
    lineup = lineup_block(mine, theirs, labels)
    cs = checks(mine, theirs, asset, injuries, phase, fa, labels)
    m, t = _delta(mine, phase), _delta(theirs, phase)
    losers = [lab for lab, v in zip(labels, (m, t)) if v is not None and v <= 0]
    v, text = verdict(cs, labels, losers)
    downside = next((c["text"] for c in cs if c["status"] == FAIL), None) or \
        next((c["text"] for c in cs if c["status"] == CONCERN), None)
    flags = len(injuries) + len(set(mine["incomplete"]["missing_players"]) | set(theirs["incomplete"]["missing_players"]))
    return {
        "phase": phase,
        "asset": asset,
        "lineup": lineup,
        "plausibility": {"verdict": v, "verdict_text": text, "checks": cs, "main_concern": downside,
                         "criteria": CRITERIA},
        "rank_axes": {"lineup_min_side": None if m is None or t is None else min(m, t),
                      "asset_ratio": asset["ratio"], "certainty": -flags},
        "most_important_change": lineup["you"]["key_change"],
        "projection_source": "espn",
    }


# -------------------------------------------------------------------------------- ranking --

AXES = ("lineup_min_side", "asset_ratio", "certainty")


def _dominates(a: dict, b: dict) -> bool:
    return all(a[k] >= b[k] for k in AXES) and any(a[k] > b[k] for k in AXES)


def pareto_fronts(axes: list[dict]) -> list[int | None]:
    """Front index (0 = not dominated) for each axes dict; None when an axis is unknown (missing
    data is never treated as 0, so such a row is not placed on any front)."""
    known = [i for i, a in enumerate(axes) if all(a.get(k) is not None for k in AXES)]
    front: list[int | None] = [None] * len(axes)
    left, level = set(known), 0
    while left:
        top = {i for i in left if not any(_dominates(axes[j], axes[i]) for j in left if j != i)}
        for i in top:
            front[i] = level
        left -= top
        level += 1
    return front


def rank(rows: list[dict], primary, tiebreak) -> list[dict]:
    """Rows carrying `dual_value`, ordered by verdict, Pareto front, then `primary(row)` (descending,
    None last), then `tiebreak(row)` (the exact search order). Adds `dual_value.rank` to each row."""
    fronts = pareto_fronts([x["dual_value"]["rank_axes"] for x in rows])
    for x, f in zip(rows, fronts):
        x["dual_value"]["rank"] = {"front": f, "policy": "verdict, then Pareto front, then the view's measure"}

    def key(x):
        dv = x["dual_value"]
        p = primary(x)
        f = dv["rank"]["front"]
        return (VERDICT_ORDER[dv["plausibility"]["verdict"]], f is None, f or 0, p is None, -(p or 0.0), tiebreak(x))
    return sorted(rows, key=key)


# ------------------------------------------------------------- the one entry point for both views --

def anchor_fa_context(fa_board, state, anchor: int) -> dict | None:
    """The anchor's best free-agent acquisition, computed once per view (trade_practical's rule)."""
    if fa_board is None:
        return None
    alt = fa_board.best_acquisition(anchor, state.rosters[anchor], kinds=("free_agent",))
    fa_player = next((p for p in fa_board.pool if p.espn_player_id == alt["player"]["espn_player_id"]), None) \
        if alt.get("player") else None
    return {"alt": alt, "fa_player": fa_player, "before": fa_board.team(state.rosters[anchor]),
            "players": {p.espn_player_id: p for ps in state.rosters.values() for p in ps}}


def for_evaluation(state, window, anchor: int, sends: list[int], receives: list[int], mine: dict, theirs: dict,
                   *, phase: str = "ros", asset_board: AssetBoard | None = None, fa_board=None,
                   fa_context: dict | None = None, labels: tuple[str, str] = ("You", "They")) -> dict:
    """assess() with the league-level inputs resolved the same way for the manual analyzer
    (POST /api/trades/evaluate) and Trade Discovery: identical inputs give an identical assessment.
    `mine`/`theirs` are trade_discovery._side_detail views of evaluate_trade's two sides."""
    from .trade_practical import _anchor_opportunity_cost, injury_flags   # trade_practical imports this module
    board = asset_board or AssetBoard(state, window)
    ctx = fa_context if fa_context is not None else anchor_fa_context(fa_board, state, anchor)
    fa = None
    if ctx is not None and fa_board is not None:
        row = {"mine": {"ros_delta": mine["deltas"]["ros_delta"]}, "sends": list(sends), "receives": list(receives)}
        fa = _anchor_opportunity_cost(fa_board, anchor, row, ctx["alt"], ctx["fa_player"], ctx["before"],
                                      ctx["players"], window)
    return assess(mine, theirs, list(sends), list(receives), board, injury_flags(state, list(sends) + list(receives)),
                  phase, fa, labels)
