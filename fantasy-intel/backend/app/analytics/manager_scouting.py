"""Read-only presentation of stored manager evidence; never runs the metrics engine.

Current roster = current league scoring-period snapshot (IR also counts by position).
Acquisition ratios use weighted league add shares: >=5 team adds, >=20 league
adds with known positions, >=2 observed teams, and a nonzero position share.
Trade behavior sample confidence: <5 completed trades INSUFFICIENT, 5–9 LOW,
10–19 MEDIUM, >=20 HIGH. This describes sample sufficiency, never a prediction.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from math import isclose
from statistics import mean

from ..db.connection import fetch_dicts
from ..espn.constants import IR_SLOT
from .manager_loader import load_league, load_trades
from .manager_metrics import DIMENSIONS, POSITIONS, STREAM_POSITIONS, pos_key

WAIVER_DIMENSIONS = (
    "waiver_aggression", "roster_churn", "hold_patience", "quick_drops",
    "faab_spending", "rookie_interest", "injury_response",
)
RATIO_RULE = "Ratio requires >=5 team adds, >=20 league adds with known positions, >=2 teams and a nonzero league position share."
TRADE_RULE = "Completed-trade sample only: <5 INSUFFICIENT; 5–9 LOW; 10–19 MEDIUM; >=20 HIGH. No trade preferences inferred."


def _json(value):
    return json.loads(value) if isinstance(value, str) else (value or {})


def _signal(dimension, metrics, classes):
    """Surface stored labels only when their underlying evidence still agrees."""
    cfg = DIMENSIONS[dimension]
    c = classes.get(dimension, {})
    m = metrics.get(cfg["metric"], {})
    status = "CONSISTENT"
    if not c:
        status = "MISSING_CLASSIFICATION"
    elif not m:
        status = "MISSING_METRIC"
    else:
        cv, mv = c.get("value"), m.get("value")
        same_value = cv is None and mv is None or cv is not None and mv is not None and isclose(cv, mv, rel_tol=1e-9, abs_tol=1e-12)
        dates = (c.get("computed_at"), m.get("computed_at"))
        if (not same_value or c.get("sample_size") != m.get("sample_size")
                or all(dates) and dates[0] != dates[1]):
            status = "MISMATCHED"
    valid = status == "CONSISTENT"
    stored_rule = c.get("rule") or f"No stored classification; needs sample >= {cfg['min']}."
    rule = stored_rule if valid else f"{status}: stored classification and metric evidence cannot be paired. Run analytics to refresh. Stored rule: {stored_rule}"
    return {
        "dimension": dimension, "title": cfg["title"], "metric": cfg["metric"],
        "classification": c.get("label", "INSUFFICIENT") if valid else "INSUFFICIENT",
        "value": m.get("value"), "numerator": m.get("numerator"), "denominator": m.get("denominator"),
        "sample_size": m.get("sample_size") or 0, "min_sample": c.get("min_sample", cfg["min"]),
        "league_mean": c.get("league_mean"), "league_std": c.get("league_std"), "z_score": c.get("z_score"),
        "confidence": c.get("confidence", "INSUFFICIENT") if valid else "INSUFFICIENT",
        "evidence_status": status, "stored_classification": c or None, "rule": rule,
        "description": m.get("description"), "computed_at": m.get("computed_at"),
    }


def _current_roster(con, league, team_id):
    snapshots = fetch_dicts(con, """
        SELECT l.team_id, fp.position, l.lineup_slot_id, l.synced_at
        FROM lineup_snapshots l LEFT JOIN fantasy_players fp USING (espn_player_id)
        WHERE l.league_id=? AND l.season=? AND l.scoring_period=?
          AND l.team_id IN (SELECT team_id FROM fantasy_teams WHERE league_id=? AND season=?)
    """, [league["league_id"], league["season"], league["current_week"], league["league_id"], league["season"]])
    counts = defaultdict(Counter)
    dates = []
    unknown = Counter()
    for r in snapshots:
        tid = r["team_id"]
        counts[tid][r["position"]] += 1
        if r["lineup_slot_id"] == IR_SLOT:
            counts[tid]["IR"] += 1
        if r["position"] not in POSITIONS:
            unknown[tid] += 1
        if tid == team_id and r["synced_at"]:
            dates.append(r["synced_at"])
    available = team_id in counts
    league_team_samples = len(counts)
    composition = []
    for pos in [*POSITIONS, "IR"]:
        avg = mean(c[pos] for c in counts.values()) if counts else None
        count = counts[team_id][pos] if available else None
        comparison = "UNKNOWN"
        if count is not None and avg is not None and len(counts) >= 2:
            comparison = "ABOVE" if count > avg else "BELOW" if count < avg else "AT"
        composition.append({"position": pos, "count": count, "league_average": avg,
                            "league_comparison": comparison})
    return {"scoring_period": league["current_week"], "available": available,
            "league_team_samples": league_team_samples,
            "positions": composition, "unknown_position_count": unknown[team_id] if available else None,
            "synced_at": max(dates) if dates else None,
            "note": "Current scoring-period snapshot only. IR is a subset of the position counts, not an additional position. Missing snapshots are unknown."}


def _position_affinity(metrics, classes, all_metrics, current, needs):
    out = []
    current_counts = {r["position"]: r["count"] for r in current["positions"]}
    need_by_pos = {r["position"]: r for r in needs}
    for pos in ("QB", "RB", "WR", "TE"):
        k = pos_key(pos)
        m = metrics.get(f"{k}_add_share", {})
        league_rows = [r for r in all_metrics if r["metric"] == f"{k}_add_share"
                       and r["denominator"] is not None and r["denominator"] > 0
                       and r["numerator"] is not None]
        total = sum(r["denominator"] for r in league_rows)
        league_share = sum(r["numerator"] for r in league_rows) / total if total else None
        ratio = None
        if (m.get("sample_size") or 0) >= 5 and total >= 20 and len(league_rows) >= 2 and league_share:
            ratio = m["value"] / league_share if m.get("value") is not None else None
        avg_metric = metrics.get(f"avg_{k}s_rostered", {})
        league_rosters = [r["value"] for r in all_metrics if r["metric"] == f"avg_{k}s_rostered"
                          and r["value"] is not None and (r["sample_size"] or 0) >= 2]
        out.append({
            **_signal(f"{k}_interest", metrics, classes), "position": pos,
            "add_count": metrics.get(f"{k}_adds", {}).get("value", m.get("numerator")),
            "add_share": m.get("value"), "league_add_share": league_share,
            "league_add_sample": total, "league_team_samples": len(league_rows),
            "relative_ratio": ratio, "ratio_rule": RATIO_RULE,
            "average_rostered": avg_metric.get("value"), "roster_history_sample": avg_metric.get("sample_size") or 0,
            "league_average_rostered": mean(league_rosters) if league_rosters else None,
            "current_rostered": current_counts[pos], "current_need_score": need_by_pos.get(pos, {}).get("need_score"),
        })
    return out


def _trade_history(con, league_id, season, team_id):
    trades = [t for t in load_trades(con, league_id, season)
              if any(team_id in (leg["from_team_id"], leg["to_team_id"]) for leg in t["legs"])]
    names = {r["espn_player_id"]: r["full_name"] for r in fetch_dicts(con, "SELECT espn_player_id, full_name FROM fantasy_players")}
    summaries = []
    for t in trades:
        def players(direction):
            return [{"espn_player_id": leg["espn_player_id"], "name": names.get(leg["espn_player_id"]),
                     "from_team_id": leg["from_team_id"], "to_team_id": leg["to_team_id"]}
                    for leg in t["legs"] if leg[direction] == team_id]
        sent, received = players("from_team_id"), players("to_team_id")
        summaries.append({"trade_id": t["trade_id"], "date": t["date"], "sources": t["sources"],
                          "source_ids": t["source_ids"], "players_sent": sent, "players_received": received,
                          "unequal_player_counts": len(sent) != len(received)})
    summaries.sort(key=lambda t: (t["date"].isoformat() if t["date"] else "", t["trade_id"]), reverse=True)
    n = len(summaries)
    confidence = "INSUFFICIENT" if n < 5 else "LOW" if n < 10 else "MEDIUM" if n < 20 else "HIGH"
    return {"completed_trades": n, "players_sent": sum(len(t["players_sent"]) for t in summaries),
            "players_received": sum(len(t["players_received"]) for t in summaries),
            "uneven_player_count_trades": sum(t["unequal_player_counts"] for t in summaries),
            "recent_trades": summaries[:10], "trade_behavior_confidence": confidence, "behavior": "UNKNOWN",
            "confidence_rule": TRADE_RULE,
            "note": "Observed completed trades only, deduplicated across activity and transaction sources. ESPN may omit trade legs; counts describe visible evidence. Unequal counts do not establish a trade preference."}


def _headlines(affinity, waiver, streaming, current, needs, trades, historical_rb):
    headlines = []
    def add(text, category, evidence, confidence):
        headlines.append({"text": text, "category": category, "evidence": evidence, "confidence": confidence})
    for s in waiver:
        if (s["classification"] not in ("HIGH", "LOW") or s["value"] is None
                or s["sample_size"] < s["min_sample"]):
            continue
        text = {
            ("waiver_aggression", "HIGH"): "Adds players more often than the league's typical manager.",
            ("waiver_aggression", "LOW"): "Adds players less often than the league's typical manager.",
            ("quick_drops", "HIGH"): "Cuts recent pickups at an above-average rate.",
            ("hold_patience", "HIGH"): "Completed player holds last longer than the league average.",
            ("rookie_interest", "HIGH"): "Rookies account for an above-average share of identified pickups.",
            ("injury_response", "HIGH"): "An above-average share of pickups coincides with same-position injury absences.",
        }.get((s["dimension"], s["classification"]))
        # Relative prose requires the actual stored league comparison to agree.
        mu = s["league_mean"]
        relative_ok = mu is not None and (s["value"] > mu if s["classification"] == "HIGH" else s["value"] < mu)
        if text and relative_ok:
            add(text, "historical", [f"{s['metric']} = {s['value']:.3f}; league mean {mu:.3f}.",
                f"Sample {s['sample_size']}; minimum {s['min_sample']}. {s['rule']}"], s["confidence"])
    for p in affinity:
        if p["classification"] == "HIGH" and p["relative_ratio"] is not None and p["relative_ratio"] > 1:
            add(f"Adds {p['position']}s at {p['relative_ratio']:.1f}x the league rate.", "historical",
                [f"{p['add_count']:g} of {p['denominator']:g} known-position adds were {p['position']} ({p['add_share']:.0%}) vs league {p['league_add_share']:.0%}.",
                 f"League sample {p['league_add_sample']:g} adds across {p['league_team_samples']} teams. {p['ratio_rule']}"], p["confidence"])
    if (historical_rb["classification"] == "HIGH" and historical_rb["value"] is not None
            and historical_rb["league_mean"] is not None and historical_rb["sample_size"] >= historical_rb["min_sample"]):
        if historical_rb["value"] > historical_rb["league_mean"]:
            add("Historically rosters more RBs than the league average.", "historical",
                [f"Average {historical_rb['value']:.1f} RBs vs league {historical_rb['league_mean']:.1f}; {historical_rb['sample_size']} weekly snapshots.",
                 historical_rb["rule"]], historical_rb["confidence"])
    for s in streaming:
        if (s["classification"] == "HIGH" and s["sample_size"] >= s["min_sample"]
                and s["turnover_changes"] is not None and s["turnover_transitions"]
                and s["distinct_starters"] is not None and s["pickups"] is not None):
            add(f"Has rotated starting {s['position']} in {s['turnover_changes']:g} of {s['turnover_transitions']:g} observed transitions.",
                "historical", [f"{s['distinct_starters']:g} distinct starters; {s['pickups']:g} pickups.", s["rule"]], s["confidence"])
    if current["available"] and current["league_team_samples"] >= 2:
        for p in current["positions"]:
            if p["position"] in ("RB", "WR") and p["count"] >= p["league_average"] + 1:
                add(f"Currently holds {p['count']} {p['position']}s, above the league average.", "current",
                    [f"Week {current['scoring_period']} snapshot: {p['count']} vs league average {p['league_average']:.1f} across {current['league_team_samples']} teams; includes IR."], "HIGH")
    for n in needs:
        if (n["starters_required"] or 0) > 0 and n["need_score"] is not None and n["need_score"] >= 0.66:
            add(f"{n['position']} need is currently high.", "current",
                [f"Stored need score {n['need_score']:.3f}; {n['healthy']} healthy of {n['rostered']} rostered; effective availability {n['effective_available']}.",
                 "High display band is >=0.66. The stored need formula is unchanged; this is current state, not a preference."], "CURRENT_STATE")
    trade_word = "trade" if trades["completed_trades"] == 1 else "trades"
    add(f"Has completed {trades['completed_trades']} observed {trade_word}; trade preference is unknown.", "trade_history",
        [f"{trades['players_sent']} players sent; {trades['players_received']} received; {trades['uneven_player_count_trades']} trades had unequal player counts.",
         trades["confidence_rule"]], trades["trade_behavior_confidence"])
    return headlines


def build_manager_scouting(con, team_id: int) -> dict | None:
    """Compute a display response on demand without writes or metric recomputation.

    Returns None for an unknown team. Raises LookupError if no league is synced.
    Callers own the connection; the HTTP router opens it read-only.
    """
    lg = load_league(con)
    if lg is None:
        raise LookupError("ESPN league not synced yet.")
    league_id, season = lg["league_id"], lg["season"]
    team_rows = fetch_dicts(con, """
        SELECT team_id,name,owner_names,wins,losses,ties,points_for,waiver_rank,faab_spent,
               acquisitions,drops,is_mine,synced_at FROM fantasy_teams
        WHERE league_id=? AND season=? AND team_id=?
    """, [league_id, season, team_id])
    if not team_rows:
        return None
    team = team_rows[0]
    all_metrics = fetch_dicts(con, """SELECT * FROM manager_metrics WHERE league_id=? AND season=?
        AND team_id IN (SELECT team_id FROM fantasy_teams WHERE league_id=? AND season=?)""",
        [league_id, season, league_id, season])
    metrics = {r["metric"]: r for r in all_metrics if r["team_id"] == team_id}
    classes = {r["dimension"]: r for r in fetch_dicts(con, """
        SELECT * FROM manager_classifications WHERE league_id=? AND season=? AND team_id=?
    """, [league_id, season, team_id])}
    needs = fetch_dicts(con, """SELECT * FROM team_position_needs WHERE league_id=? AND season=? AND team_id=?
        ORDER BY need_score DESC NULLS LAST, position""", [league_id, season, team_id])
    for n in needs:
        n["details"] = _json(n["details"])
        n["league_starter_average"] = n["details"].get("league_avg_starter_value")
    current = _current_roster(con, lg, team_id)
    affinity = _position_affinity(metrics, classes, all_metrics, current, needs)
    waiver = [_signal(d, metrics, classes) for d in WAIVER_DIMENSIONS if d != "faab_spending" or lg["is_faab"]]
    streaming = []
    for pos in STREAM_POSITIONS:
        k = pos_key(pos)
        m = metrics.get(f"starter_turnover_{k}", {})
        streaming.append({**_signal(f"{k}_streaming", metrics, classes), "position": pos,
            "distinct_starters": metrics.get(f"distinct_starters_{k}", {}).get("value"),
            "starter_turnover": m.get("value"), "turnover_changes": m.get("numerator"),
            "turnover_transitions": m.get("denominator"), "pickups": metrics.get(f"{k}_adds", {}).get("value")})
    trades = _trade_history(con, league_id, season, team_id)
    dates = [m["computed_at"] for m in metrics.values() if m["computed_at"]]
    rb_history = _signal("rb_hoarding", metrics, classes)
    return {
        "league": {k: lg[k] for k in ("league_id", "season", "name", "current_week", "synced_at")},
        "identity": {**team, "completed_trades": trades["completed_trades"],
            "observed_adds": metrics.get("adds_total", {}).get("value"),
            "observed_drops": metrics.get("drops_total", {}).get("value"),
            "is_faab": lg["is_faab"], "faab_budget": lg["faab_budget"],
            "faab_remaining": max(0, lg["faab_budget"] - team["faab_spent"])
                if lg["is_faab"] and lg["faab_budget"] is not None and team["faab_spent"] is not None else None},
        "current_roster": current, "position_affinity": affinity, "waiver_style": waiver,
        "streaming_style": streaming, "current_needs": needs, "trade_history": trades,
        "headlines": _headlines(affinity, waiver, streaming, current, needs, trades, rb_history),
        "evidence": {"metrics_computed_at": min(dates) if dates else None,
            "metrics_available": bool(metrics), "classifications_available": bool(classes),
            "position_ratio_rule": RATIO_RULE, "trade_confidence_rule": TRADE_RULE,
            "streaming_definition": "Streaming measures rotation of actual starters. Pickups alone do not establish streaming. Transitions follow the existing engine's consecutive observed snapshots; missing weeks may span gaps.",
            "notes": ["Historical labels and confidence come from the stored manager engine; no formulas are recomputed.",
                      "ESPN acquisition/drop counters and observed metric counts can differ because source coverage differs.",
                      "Roster snapshots, metrics and needs can have different update times; each section exposes its source time.",
                      "Current need scores and starter values are stored analytics outputs, not new forward projections."]},
    }
