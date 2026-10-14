"""Manager tendency engine.

Objective, countable behaviours only (adds, drops, trades, lineups, holds).
Labels are league-relative and always stored with the raw numbers, the league
mean/std they were compared to, the sample size and a confidence level.
A label is INSUFFICIENT when the sample is below the dimension's minimum.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean, pstdev
from typing import Any

POSITIONS = ["QB", "RB", "WR", "TE", "K", "D/ST"]
STREAM_POSITIONS = ["QB", "TE", "K", "D/ST"]
Z_HIGH = 0.75
QUICK_DROP_DAYS = 7


def pos_key(pos: str | None) -> str:
    return (pos or "unk").lower().replace("/", "")


@dataclass
class ManagerInputs:
    league_id: int
    season: int
    current_week: int
    roster_spots: int
    is_faab: bool
    faab_budget: float | None
    teams: list[dict]
    adds: list[dict] = field(default_factory=list)       # team_id, espn_player_id, position, date, period, is_rookie, injury_context
    drops: list[dict] = field(default_factory=list)      # team_id, espn_player_id, position, date, period
    trades: list[dict] = field(default_factory=list)     # trade_id, date, legs[{espn_player_id, from_team_id, to_team_id}]
    lineups: list[dict] = field(default_factory=list)    # period, team_id, espn_player_id, position, lineup_slot_id, is_starter
    failed_claims: list[dict] = field(default_factory=list)  # team_id, period
    failed_claims_visible: bool = False
    now: datetime | None = None


def _metric(team_id, metric, value, num=None, den=None, n=None, unit="", desc=""):
    return {"team_id": team_id, "metric": metric, "value": value, "numerator": num,
            "denominator": den, "sample_size": n, "unit": unit, "description": desc}


def _rate(num: float, den: float) -> float | None:
    return num / den if den else None


def hold_lengths(adds: list[dict], drops: list[dict]) -> dict[int, dict]:
    """Per team: completed hold lengths (days) for added players, and still-held count."""
    drops_by_key: dict[tuple, list[datetime]] = defaultdict(list)
    for d in drops:
        if d.get("date"):
            drops_by_key[(d["team_id"], d["espn_player_id"])].append(d["date"])
    for v in drops_by_key.values():
        v.sort()
    out: dict[int, dict] = defaultdict(lambda: {"completed": [], "still_held": 0, "undated": 0})
    for a in adds:
        team = a["team_id"]
        if not a.get("date"):
            out[team]["undated"] += 1
            continue
        later = [d for d in drops_by_key.get((team, a["espn_player_id"]), []) if d >= a["date"]]
        if later:
            out[team]["completed"].append({
                "espn_player_id": a["espn_player_id"],
                "days": (later[0] - a["date"]).total_seconds() / 86400.0,
                "added": a["date"], "dropped": later[0]})
        else:
            out[team]["still_held"] += 1
    return out


def starter_turnover(lineups: list[dict], team_id: int, position: str) -> tuple[int, int]:
    """(# week transitions where the set of starters at `position` changed, # transitions)."""
    by_period: dict[int, frozenset] = {}
    for r in lineups:
        if r["team_id"] == team_id and r["is_starter"] and r.get("position") == position:
            by_period.setdefault(r["period"], frozenset())
            by_period[r["period"]] = by_period[r["period"]] | {r["espn_player_id"]}
    periods = sorted(p for p, s in by_period.items() if s)
    changes = sum(1 for a, b in zip(periods, periods[1:]) if by_period[a] != by_period[b])
    return changes, max(0, len(periods) - 1)


def compute_metrics(inp: ManagerInputs) -> list[dict]:
    weeks = max(1, inp.current_week)
    now = inp.now or datetime.utcnow()
    adds_by_team = defaultdict(list)
    for a in inp.adds:
        adds_by_team[a["team_id"]].append(a)
    drops_by_team = defaultdict(list)
    for d in inp.drops:
        drops_by_team[d["team_id"]].append(d)
    holds = hold_lengths(inp.adds, inp.drops)
    failed = Counter(f["team_id"] for f in inp.failed_claims)

    trade_counts: Counter = Counter()
    two_for_one: Counter = Counter()
    sent: Counter = Counter()
    received: Counter = Counter()
    for t in inp.trades:
        teams = {leg["from_team_id"] for leg in t["legs"]} | {leg["to_team_id"] for leg in t["legs"]}
        teams.discard(None)
        out_counts = Counter(leg["from_team_id"] for leg in t["legs"])
        for team in teams:
            trade_counts[team] += 1
            n_out = out_counts.get(team, 0)
            n_in = sum(1 for leg in t["legs"] if leg["to_team_id"] == team)
            sent[team] += n_out
            received[team] += n_in
            if n_out != n_in:
                two_for_one[team] += 1

    periods_by_team = defaultdict(set)
    pos_counts = defaultdict(lambda: defaultdict(int))  # team -> (period,pos) -> n
    for r in inp.lineups:
        periods_by_team[r["team_id"]].add(r["period"])
        pos_counts[r["team_id"]][(r["period"], r.get("position"))] += 1

    rows: list[dict] = []
    for team in inp.teams:
        tid = team["team_id"]
        adds, drops = adds_by_team[tid], drops_by_team[tid]
        n_adds, n_drops, n_trades = len(adds), len(drops), trade_counts[tid]
        m = [
            _metric(tid, "weeks_observed", weeks, unit="weeks", desc="Scoring periods elapsed this season"),
            _metric(tid, "adds_total", n_adds, n=n_adds, unit="players", desc="Executed free-agent/waiver adds"),
            _metric(tid, "drops_total", n_drops, n=n_drops, unit="players", desc="Executed drops"),
            _metric(tid, "transactions_per_week", (n_adds + n_drops + n_trades) / weeks, n_adds + n_drops + n_trades,
                    weeks, weeks, "moves/week", "Adds + drops + trades per scoring period"),
            _metric(tid, "adds_per_week", n_adds / weeks, n_adds, weeks, weeks, "adds/week", "Adds per scoring period"),
            _metric(tid, "drops_per_week", n_drops / weeks, n_drops, weeks, weeks, "drops/week", "Drops per scoring period"),
            _metric(tid, "roster_churn_rate", _rate(n_adds, inp.roster_spots), n_adds, inp.roster_spots, weeks,
                    "roster turnovers", "Adds divided by non-IR roster spots (1.0 = a full roster's worth of adds)"),
        ]
        known_pos = [a for a in adds if a.get("position")]
        pos_counter = Counter(a["position"] for a in known_pos)
        for pos in POSITIONS:
            k = pos_key(pos)
            m.append(_metric(tid, f"{k}_adds", pos_counter[pos], n=pos_counter[pos], unit="adds",
                             desc=f"{pos} players added"))
            m.append(_metric(tid, f"{k}_add_share", _rate(pos_counter[pos], len(known_pos)), pos_counter[pos],
                             len(known_pos), len(known_pos), "share", f"Share of adds that were {pos}"))
        n_periods = len(periods_by_team[tid])
        for pos in ["QB", "RB", "WR", "TE"]:
            total = sum(v for (p, ps), v in pos_counts[tid].items() if ps == pos)
            m.append(_metric(tid, f"avg_{pos_key(pos)}s_rostered", _rate(total, n_periods), total, n_periods,
                             n_periods, "players", f"Average {pos}s on roster per weekly snapshot"))
        for pos in STREAM_POSITIONS:
            k = pos_key(pos)
            m.append(_metric(tid, f"stream_{k}_rate", pos_counter[pos] / weeks, pos_counter[pos], weeks, weeks,
                             "adds/week", f"{pos} pickups per scoring period"))
            ch, tr = starter_turnover(inp.lineups, tid, pos)
            m.append(_metric(tid, f"starter_turnover_{k}", _rate(ch, tr), ch, tr, tr, "share",
                             f"Share of week-to-week transitions where the starting {pos} changed"))
        rookie_known = [a for a in adds if a.get("is_rookie") is not None and a.get("position") != "D/ST"]
        n_rookie = sum(1 for a in rookie_known if a["is_rookie"])
        m.append(_metric(tid, "rookie_add_rate", _rate(n_rookie, len(rookie_known)), n_rookie, len(rookie_known),
                         len(rookie_known), "share", "Share of adds (with known NFL id) that were rookies"))
        h = holds[tid]
        days = [c["days"] for c in h["completed"]]
        m.append(_metric(tid, "average_player_hold_length", mean(days) if days else None, sum(days) if days else 0,
                         len(days), len(days), "days",
                         f"Mean days between adding and dropping a player (completed holds only; {h['still_held']} still rostered)"))
        mature = [a for a in adds if a.get("date") and a["date"] <= now - timedelta(days=QUICK_DROP_DAYS)]
        quick = sum(1 for c in h["completed"] if c["days"] <= QUICK_DROP_DAYS
                    and c["added"] <= now - timedelta(days=QUICK_DROP_DAYS))
        m.append(_metric(tid, "quick_drop_rate", _rate(quick, len(mature)), quick, len(mature), len(mature), "share",
                         f"Share of adds (older than {QUICK_DROP_DAYS}d) dropped within {QUICK_DROP_DAYS} days"))
        inj_known = [a for a in adds if a.get("injury_context") is not None]
        n_inj = sum(1 for a in inj_known if a["injury_context"])
        m.append(_metric(tid, "injury_replacement_rate", _rate(n_inj, len(inj_known)), n_inj, len(inj_known),
                         len(inj_known), "share",
                         "Share of adds made while a same-position player on the roster was Out/Doubtful/IR that week"))
        m += [
            _metric(tid, "trade_count", n_trades, n=weeks, unit="trades", desc="Completed trades involving this team"),
            _metric(tid, "two_for_one_trade_count", two_for_one[tid], n=n_trades, unit="trades",
                    desc="Trades where this team sent and received a different number of players"),
            _metric(tid, "players_traded_away", sent[tid], n=n_trades, unit="players", desc="Players sent in trades"),
            _metric(tid, "players_received_in_trades", received[tid], n=n_trades, unit="players",
                    desc="Players received in trades"),
            _metric(tid, "ir_moves", team.get("move_to_ir"), n=weeks, unit="moves", desc="ESPN move-to-IR counter"),
        ]
        if inp.is_faab:
            spent = team.get("faab_spent")
            m.append(_metric(tid, "faab_spent_pct", _rate(spent or 0, inp.faab_budget or 0), spent,
                             inp.faab_budget, weeks, "share", "Share of FAAB budget spent"))
        if inp.failed_claims_visible:
            m.append(_metric(tid, "failed_claims", failed[tid], n=weeks, unit="claims",
                             desc="Unsuccessful waiver claims visible in ESPN transactions"))
        rows += m
    for r in rows:
        r.update(league_id=inp.league_id, season=inp.season)
    return rows


# dimension -> (metric, min_sample, mode, absolute high floor, description)
DIMENSIONS: dict[str, dict[str, Any]] = {
    "waiver_aggression": dict(metric="adds_per_week", min=3, mode="z", floor=0.5, title="Waiver aggression"),
    "roster_churn": dict(metric="roster_churn_rate", min=3, mode="z", floor=0.25, title="Roster churn"),
    "quick_drops": dict(metric="quick_drop_rate", min=4, mode="z", floor=0.25, title="Quick-drop tendency"),
    "hold_patience": dict(metric="average_player_hold_length", min=4, mode="z", floor=None, title="Patience with adds"),
    "qb_interest": dict(metric="qb_add_share", min=5, mode="z", floor=0.15, title="QB add interest"),
    "rb_interest": dict(metric="rb_add_share", min=5, mode="z", floor=0.25, title="RB add interest"),
    "wr_interest": dict(metric="wr_add_share", min=5, mode="z", floor=0.25, title="WR add interest"),
    "te_interest": dict(metric="te_add_share", min=5, mode="z", floor=0.15, title="TE add interest"),
    "rb_hoarding": dict(metric="avg_rbs_rostered", min=2, mode="z", floor=None, title="RB hoarding"),
    "qb_streaming": dict(metric="stream_qb_rate", min=3, mode="z", floor=0.2, title="QB streaming"),
    "te_streaming": dict(metric="stream_te_rate", min=3, mode="z", floor=0.2, title="TE streaming"),
    "dst_streaming": dict(metric="stream_dst_rate", min=3, mode="z", floor=0.3, title="D/ST streaming"),
    "k_streaming": dict(metric="stream_k_rate", min=3, mode="z", floor=0.3, title="K streaming"),
    "injury_response": dict(metric="injury_replacement_rate", min=5, mode="z", floor=0.2, title="Injury replacement"),
    "rookie_interest": dict(metric="rookie_add_rate", min=5, mode="z", floor=0.2, title="Rookie interest"),
    "trade_activity": dict(metric="trade_count", min=3, mode="abs", thresholds=(1, 2), title="Trade activity"),
    "faab_spending": dict(metric="faab_spent_pct", min=3, mode="z", floor=0.2, title="FAAB spending"),
}


def confidence(n: int | None, min_n: int) -> str:
    n = n or 0
    if n < min_n:
        return "LOW"
    if n < 2 * min_n:
        return "MEDIUM"
    return "HIGH"


def classify_metrics(metric_rows: list[dict]) -> list[dict]:
    by_metric: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in metric_rows:
        by_metric[r["metric"]][r["team_id"]] = r
    out = []
    for dim, cfg in DIMENSIONS.items():
        rows = by_metric.get(cfg["metric"])
        if not rows:
            continue
        eligible = [r["value"] for r in rows.values()
                    if r["value"] is not None and (r["sample_size"] or 0) >= cfg["min"]]
        mu = mean(eligible) if eligible else None
        sd = pstdev(eligible) if len(eligible) > 1 else None
        for tid, r in rows.items():
            n, v = r["sample_size"] or 0, r["value"]
            z = None
            if v is None or n < cfg["min"]:
                label = "INSUFFICIENT"
                rule = f"Needs sample >= {cfg['min']} (have {n})."
            elif cfg["mode"] == "abs":
                lo, hi = cfg["thresholds"]
                label = "HIGH" if v >= hi else "MEDIUM" if v >= lo else "LOW"
                rule = f"Absolute: LOW < {lo} <= MEDIUM < {hi} <= HIGH"
            else:
                if sd and sd > 1e-9:
                    z = (v - mu) / sd
                    label = "HIGH" if z >= Z_HIGH else "LOW" if z <= -Z_HIGH else "MEDIUM"
                else:
                    label = "MEDIUM"
                floor = cfg.get("floor")
                if label == "HIGH" and floor is not None and v < floor:
                    label = "MEDIUM"
                if v == 0 and cfg.get("floor") is not None:
                    label = "LOW"
                rule = (f"League-relative: HIGH if >= {Z_HIGH} SD above league mean"
                        + (f" and value >= {floor}" if floor is not None else "")
                        + f"; LOW if <= {Z_HIGH} SD below" + ("; zero is always LOW" if floor is not None else ""))
            out.append({
                "league_id": r["league_id"], "season": r["season"], "team_id": tid,
                "dimension": dim, "label": label, "metric": cfg["metric"], "value": v,
                "league_mean": mu, "league_std": sd, "z_score": z, "sample_size": n,
                "min_sample": cfg["min"], "confidence": confidence(n, cfg["min"]), "rule": rule,
            })
    return out
