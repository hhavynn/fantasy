"""Underlying-value vs box-score model (transparent heuristics, no ML).

For every skill player with games this season we store:
  opportunity_basis        "xfp" (nflverse expected fantasy points) or "targets+carries"
                           when xFP is unavailable -- percentiles are computed per basis
  opportunity_score  0-10  position percentile of recent opportunity per game (see basis)
  opportunity_tier         HIGH / MEDIUM / LOW from ABSOLUTE per-position thresholds
  production_score   0-10  position percentile of recent PPR points/game
  role_trend               UP / FLAT / DOWN / INSUFFICIENT (composite of usage deltas)
  fp_minus_xfp             season PPR/game minus xFP/game -> value-gap label
  production_sustainability ABOVE_EXPECTATION / ALIGNED / BELOW_EXPECTATION / INSUFFICIENT:
                           is box-score production running above or below the opportunity
                           baseline? This is evidence of regression risk, NOT a trade call:
                           xFP is an average-player model and elite players can beat it
                           persistently (talent, QB, target type, YAC).
  market_signal            BUY / SELL / HOLD / WATCH / MONITOR: is there an actionable
                           fantasy market mismatch? Conservative by design (HOLD over a
                           false SELL). See classify() for the rules.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Iterable

from ..db.connection import fetch_dicts, replace_where, utcnow
from .trends import TREND_METRICS, compute_trend, composite_trend

RECENT_GAMES = 3
MIN_GAMES_CLASSIFY = 3
MIN_GAMES_PERCENTILE_POOL = 2
GAP_ABS = 2.5     # PPR points/game
GAP_REL = 0.20    # of xFP/game

# Absolute opportunity tiers: (HIGH >=, MEDIUM >=) per basis and position.
#   xfp: expected PPR points per game; targets+carries: opportunities per game.
TIER_THRESHOLDS: dict[str, dict[str, tuple[float, float]]] = {
    "xfp": {"QB": (17.0, 13.0), "RB": (12.0, 7.0), "WR": (12.0, 7.0), "TE": (9.0, 5.0)},
    "targets+carries": {"RB": (15.0, 9.0), "WR": (7.0, 4.0), "TE": (5.0, 3.0)},
}


def opportunity_tier(basis: str, position: str | None, recent_per_game: float | None) -> str | None:
    t = TIER_THRESHOLDS.get(basis, {}).get(position or "")
    if t is None or recent_per_game is None:
        return None
    return "HIGH" if recent_per_game >= t[0] else "MEDIUM" if recent_per_game >= t[1] else "LOW"


def meaningful_production(position: str | None, fp_pg: float | None) -> bool:
    """Box score is fantasy-relevant: PPR/game at or above the position's MEDIUM xFP tier."""
    t = TIER_THRESHOLDS["xfp"].get(position or "")
    return t is not None and fp_pg is not None and fp_pg >= t[1]


def percentile_scores(values: dict[str, float]) -> dict[str, float]:
    """Map id -> 0..10 percentile rank (ties share the average rank)."""
    if not values:
        return {}
    ordered = sorted(values.values())
    n = len(ordered)
    out = {}
    for key, v in values.items():
        below = sum(1 for x in ordered if x < v)
        equal = sum(1 for x in ordered if x == v)
        out[key] = round(10.0 * (below + 0.5 * equal) / n, 1) if n > 1 else 5.0
    return out


def played(r: dict) -> bool:
    return bool((r.get("offense_snaps") or 0) > 0 or (r.get("opportunities") or 0) > 0)


def value_gap(fp_pg: float | None, xfp_pg: float | None, n_xfp: int) -> str:
    if fp_pg is None or xfp_pg is None or n_xfp < MIN_GAMES_CLASSIFY:
        return "INSUFFICIENT"
    margin = max(GAP_ABS, GAP_REL * xfp_pg)
    diff = fp_pg - xfp_pg
    if diff >= margin:
        return "PRODUCTION_EXCEEDS_USAGE"
    if diff <= -margin:
        return "USAGE_EXCEEDS_PRODUCTION"
    return "ALIGNED"


SUSTAINABILITY = {
    "PRODUCTION_EXCEEDS_USAGE": "ABOVE_EXPECTATION",
    "USAGE_EXCEEDS_PRODUCTION": "BELOW_EXPECTATION",
    "ALIGNED": "ALIGNED",
    "INSUFFICIENT": "INSUFFICIENT",
}


def production_sustainability(gap: str) -> str:
    """Box-score production vs the opportunity (xFP) baseline. Regression evidence only."""
    return SUSTAINABILITY.get(gap, "INSUFFICIENT")


def classify(games: int, role_trend: str, gap: str, tier: str | None, position: str | None = None,
             fp_pg: float | None = None) -> tuple[str, list[str]]:
    """Market signal from (absolute opportunity tier, role trend, production sustainability).

    Rules (pure, deterministic, conservative -- HOLD is preferred to a false SELL):
      * < 3 games or no tier                        -> MONITOR
      * an INSUFFICIENT role trend never drives a SELL (unknown is not "not rising")
      * LOW tier:    above expectation + fantasy-relevant box score -> SELL (sell-high);
                     role UP -> WATCH; else HOLD
      * MEDIUM/HIGH: role UP + production not above expectation     -> BUY
                     below expectation + role not DOWN              -> BUY
      * HIGH tier is never SELL while its recent (absolute) role is still HIGH: above-expectation
        production or a short-term role dip means regression risk, not a sell. If the role truly
        collapses, the recent tier drops to MEDIUM and the MEDIUM rules apply.
      * MEDIUM:      above expectation + role FLAT/DOWN + fantasy-relevant box score -> SELL;
                     role DOWN + fantasy-relevant box score                          -> SELL
      * otherwise HOLD
    """
    if games < MIN_GAMES_CLASSIFY:
        return "MONITOR", [f"Only {games} game(s) played; need {MIN_GAMES_CLASSIFY} to classify."]
    if tier is None:
        return "MONITOR", ["No opportunity tier available for this position/basis."]
    productive = meaningful_production(position, fp_pg)
    sustain = production_sustainability(gap)
    trend_known = role_trend in ("UP", "FLAT", "DOWN")
    above, below = sustain == "ABOVE_EXPECTATION", sustain == "BELOW_EXPECTATION"
    regression_note = ("Production is running above the opportunity (xFP) baseline, so some efficiency "
                       "regression is possible; xFP is an average-player model and strong players can beat it.")

    if tier == "LOW":
        if above and productive:
            return "SELL", ["Productive box score on a small role (TD/efficiency driven): sell-high candidate."]
        if role_trend == "UP":
            return "WATCH", ["Role is rising but still small in absolute terms: watch, don't buy yet."]
        return "HOLD", ["Small role; nothing actionable."]

    if role_trend == "UP" and not above:
        return "BUY", [f"{tier.title()} role that is still growing, and production isn't outrunning it."]
    if below and role_trend != "DOWN":
        return "BUY", ["Expected points from his role exceed his actual points: production should catch up if the role holds."]

    if tier == "HIGH":
        if above and role_trend == "UP":
            return "HOLD", ["High-volume role is still growing, but production already outruns it: hold, don't chase.",
                            regression_note]
        if above:
            return "HOLD", ["Elite/high-volume role remains intact; current production is above expectation, so some "
                            "efficiency regression is possible without making the player a sell.", regression_note]
        if role_trend == "DOWN":
            return "HOLD", ["Usage has dipped from its earlier level but the recent role is still high-volume: "
                            "monitor for a sustained decline before selling."]
        return "HOLD", ["High-volume role and production are consistent with each other."]

    # MEDIUM
    if above and role_trend in ("FLAT", "DOWN") and productive:
        return "SELL", ["Scoring well above what a mid-sized role normally produces, and the role isn't growing: "
                        "sell-high candidate.", regression_note]
    if role_trend == "DOWN" and productive:
        return "SELL", ["Box score still looks good but a mid-sized role is shrinking."]
    if above:
        return "HOLD", ["Production is above expectation but the evidence for selling is incomplete "
                        f"(role trend {role_trend.lower() if trend_known else 'not yet measurable'})."]
    return "HOLD", ["Role and production are consistent with each other."]


def blended_value(opportunity: float | None, production: float | None, trend_score: float | None) -> float | None:
    if opportunity is None and production is None:
        return None
    opp = opportunity if opportunity is not None else production
    prod = production if production is not None else opportunity
    v = 0.75 * opp + 0.25 * prod + 1.0 * (trend_score or 0.0)
    return round(max(0.0, min(10.0, v)), 2)


def score_players(usage_rows: Iterable[dict], season: int) -> list[dict]:
    by_player: dict[str, list[dict]] = defaultdict(list)
    for r in usage_rows:
        if played(r):
            by_player[r["gsis_id"]].append(r)
    for rows in by_player.values():
        rows.sort(key=lambda r: r["week"])

    summary: dict[str, dict] = {}
    for gsis, rows in by_player.items():
        recent = rows[-RECENT_GAMES:]
        position = rows[-1].get("position")
        xfp_recent = [r["xfp"] for r in recent if r.get("xfp") is not None]
        xfp_all = [r["xfp"] for r in rows if r.get("xfp") is not None]
        fp_all = [r["fantasy_points_ppr"] for r in rows if r.get("fantasy_points_ppr") is not None]
        fp_with_xfp = [r["fantasy_points_ppr"] for r in rows
                       if r.get("xfp") is not None and r.get("fantasy_points_ppr") is not None]
        summary[gsis] = {
            "position": position,
            "rows": rows,
            "games": len(rows),
            "opp_metric": mean(xfp_recent) if xfp_recent else (
                mean((r.get("opportunities") or 0) for r in recent)),
            "opp_basis": "xfp" if xfp_recent else "targets+carries",
            "prod_metric": mean(r["fantasy_points_ppr"] for r in recent if r.get("fantasy_points_ppr") is not None)
            if any(r.get("fantasy_points_ppr") is not None for r in recent) else None,
            "fp_pg": mean(fp_all) if fp_all else None,
            "xfp_pg": mean(xfp_all) if xfp_all else None,
            "fp_pg_matched": mean(fp_with_xfp) if fp_with_xfp else None,
            "n_xfp": len(xfp_all),
        }

    # Position-relative percentiles among players with enough games.
    opp_pct: dict[str, float] = {}
    prod_pct: dict[str, float] = {}
    for pos in {s["position"] for s in summary.values()}:
        pool = {g: s for g, s in summary.items() if s["position"] == pos and s["games"] >= MIN_GAMES_PERCENTILE_POOL}
        for basis in ("xfp", "targets+carries"):
            opp_pct.update(percentile_scores({g: s["opp_metric"] for g, s in pool.items() if s["opp_basis"] == basis}))
        prod_pct.update(percentile_scores({g: s["prod_metric"] for g, s in pool.items() if s["prod_metric"] is not None}))

    now = utcnow()
    out = []
    for gsis, s in summary.items():
        rows, pos = s["rows"], s["position"]
        trends = [compute_trend(m, [(r["week"], r.get(m)) for r in rows], thr) for m, thr in TREND_METRICS.get(pos, [])]
        trend_score, trend_label = composite_trend(trends)
        gap_fp = s["fp_pg_matched"]
        gap = value_gap(gap_fp, s["xfp_pg"], s["n_xfp"])
        opp, prod = opp_pct.get(gsis), prod_pct.get(gsis)
        tier = opportunity_tier(s["opp_basis"], pos, s["opp_metric"])
        cls, reasons = classify(s["games"], trend_label, gap, tier, pos, s["fp_pg"])
        for t in trends:
            if t.label in ("UP", "DOWN"):
                reasons.append(f"{t.metric} {t.label.lower()}: {_fmt(t.baseline_avg, t.metric)} -> {_fmt(t.recent_avg, t.metric)} "
                               f"(weeks {t.baseline_weeks} vs {t.recent_weeks})")
        if gap_fp is not None and s["xfp_pg"] is not None:
            reasons.append(f"PPR {gap_fp:.1f}/g vs expected {s['xfp_pg']:.1f}/g ({gap_fp - s['xfp_pg']:+.1f}).")
        if opp is None and s["games"] < MIN_GAMES_PERCENTILE_POOL:
            reasons.append("Too few games for a position percentile.")
        last = rows[-1]
        out.append({
            "season": season,
            "gsis_id": gsis,
            "espn_player_id": None,
            "position": pos,
            "as_of_week": last["week"],
            "games": s["games"],
            "opportunity_basis": s["opp_basis"],
            "opportunity_tier": tier,
            "role_trend": trend_label,
            "market_signal": cls,
            "opportunity_score": opp,
            "production_score": prod,
            "usage_trend_score": round(trend_score, 3) if trend_score is not None else None,
            "trend_label": trend_label,
            "fp_per_game": round(s["fp_pg"], 2) if s["fp_pg"] is not None else None,
            "xfp_per_game": round(s["xfp_pg"], 2) if s["xfp_pg"] is not None else None,
            "fp_minus_xfp": round(gap_fp - s["xfp_pg"], 2) if gap_fp is not None and s["xfp_pg"] is not None else None,
            "value_gap_label": gap,
            "classification": cls,
            "value_score": blended_value(opp, prod, trend_score),
            "components": {
                "opportunity_basis": s["opp_basis"],
                "production_sustainability": production_sustainability(gap),
                "tier_thresholds": TIER_THRESHOLDS.get(s["opp_basis"], {}).get(pos),
                "recent_opportunity_per_game": round(s["opp_metric"], 2) if s["opp_metric"] is not None else None,
                "recent_ppr_per_game": round(s["prod_metric"], 2) if s["prod_metric"] is not None else None,
                "recent_weeks": [r["week"] for r in rows[-RECENT_GAMES:]],
                "games_with_xfp": s["n_xfp"],
                "trends": [t.as_dict() for t in trends],
                "value_formula": "0.75*opportunity + 0.25*production + 1.0*trend (clamped 0-10)",
            },
            "reasons": reasons,
            "computed_at": now,
        })
    return out


def _fmt(v: float | None, metric: str) -> str:
    if v is None:
        return "n/a"
    if metric in ("offense_pct", "target_share", "team_rush_share", "backfield_rush_share"):
        return f"{100 * v:.0f}%"
    return f"{v:.1f}"


def build_player_scores(con, season: int) -> dict:
    usage = fetch_dicts(con, "SELECT * FROM player_week_usage WHERE season = ?", [season])
    rows = score_players(usage, season)
    espn = {r["gsis_id"]: r["espn_player_id"] for r in fetch_dicts(
        con, "SELECT gsis_id, min(espn_player_id) espn_player_id FROM player_id_map WHERE gsis_id IS NOT NULL GROUP BY 1")}
    for r in rows:
        r["espn_player_id"] = espn.get(r["gsis_id"])
    replace_where(con, "player_scores", rows, "season = ?", [season])
    return {"players_scored": len(rows)}
