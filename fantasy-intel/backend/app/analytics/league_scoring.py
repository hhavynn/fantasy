"""League fantasy scoring for offensive skill players (QB/RB/WR/TE) from nflverse box scores.

ESPN league `scoring_items` are translated into weights on nflverse `load_player_stats`
columns. Only rules that are implemented AND verified are accepted; any other rule that
can award points to a QB/RB/WR/TE (yardage bonuses, half-PPR via a different stat, TE
premium, first downs, ...) makes the configuration UNSUPPORTED and callers must refuse to
produce league-unit points rather than silently returning mislabeled numbers.

ESPN semantics (verified against stored ESPN actuals, docs/independent_projection_engine.md):
  * `points` applies to every lineup slot; `pointsOverrides` replaces it for one slot id.
    Slot 16 is D/ST, so a rule whose only non-zero value is the slot-16 override never
    scores for an offensive player.
  * ESPN stat 72 (lostFumbles) counts every lost fumble, including special-teams returns, so
    it maps to nflverse `fumbles_lost_total` (not the sum of the sack/rush/receiving splits).

Kicker statistics are not applicable to QB/RB/WR/TE. D/ST-only or defensive-return events
(e.g. interception-return TD) cannot be observed for offensive players in nflverse player
stats; they are listed as `unmodeled` and their effect is measured by reconciliation, never
assumed to be zero.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

DST_SLOT = "16"
SKILL_POSITIONS = ("QB", "RB", "WR", "TE")

# ESPN statId -> (nflverse player-stats column, label). Each is a direct one-to-one count.
DIRECT = {
    3: ("passing_yards", "passing yards"),
    4: ("passing_tds", "passing TD"),
    19: ("passing_2pt_conversions", "passing 2-pt conversion"),
    20: ("passing_interceptions", "interception thrown"),
    24: ("rushing_yards", "rushing yards"),
    25: ("rushing_tds", "rushing TD"),
    26: ("rushing_2pt_conversions", "rushing 2-pt conversion"),
    42: ("receiving_yards", "receiving yards"),
    43: ("receiving_tds", "receiving TD"),
    44: ("receiving_2pt_conversions", "receiving 2-pt conversion"),
    53: ("receptions", "reception"),
    63: ("fumble_recovery_tds", "fumble recovered for TD"),
    72: ("fumbles_lost_total", "fumble lost (all phases)"),
}
# Kick and punt return TDs are one nflverse column; they are supported only when equal.
RETURN_TD_STATS = (101, 102)
RETURN_TD_COLUMN = "special_teams_tds"

KICKER_ONLY = {74, 77, 80, 85, 86, 88, 198, 201, 214, 215, 216, 217, 218, 219, 220, 221, 222,
               # the rest of kicker_scoring's FG/PAT items, its synthetic short-range buckets and the unimplemented
               # kicker rule of translated items (app/sleeper/scoring.py)
               75, 76, 78, 79, 81, 82, 83, 84, 87, 199, 200, 202, 203, 1080, 1081, 1082, 1083, 1084, 1085, 1099}
# Defensive / D/ST events with a non-zero base value. Offensive players essentially never
# record them, and nflverse player stats cannot attribute them, so they are unmodeled.
UNMODELED_DEFENSIVE = {93: "blocked kick return TD", 103: "interception return TD",
                       104: "fumble return TD", 206: "defensive 2-pt return", 209: "1-pt safety"}

COMPONENT_COLUMNS = tuple(sorted({c for c, _ in DIRECT.values()} | {RETURN_TD_COLUMN}))

# Counts ESPN has no statId for but other providers score (Sleeper: first downs, attempts, completions, carries).
# Synthetic ids above 2000 (never an ESPN id), used only by translated scoring items (app/sleeper/scoring.py). They
# are not in COMPONENT_COLUMNS, which the independent model's training reads: a league scoring them is re-scored
# from the box-score mix (app/analytics/rescore.py), which reads MIX_COLUMNS.
EXTRA_DIRECT = {
    2001: ("passing_first_downs", "passing first down"),
    2002: ("rushing_first_downs", "rushing first down"),
    2003: ("receiving_first_downs", "receiving first down"),
    2004: ("attempts", "pass attempt"),
    2005: ("completions", "pass completion"),
    2006: ("carries", "rush attempt"),
}
MIX_COLUMNS = tuple(sorted(set(COMPONENT_COLUMNS) | {c for c, _ in EXTRA_DIRECT.values()}))


class UnsupportedScoring(ValueError):
    """The league awards offensive points through a rule this module does not implement."""


@dataclass(frozen=True)
class SkillScoring:
    weights: dict[str, float]                      # nflverse column -> points per unit
    unsupported: list[dict] = field(default_factory=list)
    unmodeled: list[dict] = field(default_factory=list)
    ignored: list[dict] = field(default_factory=list)
    # position -> nflverse column -> points per unit on top of `weights` (a TE premium). ESPN never sends these
    # (its position rules are slot overrides, unsupported); translated provider items do (`positionPoints`).
    position_weights: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def supported(self) -> bool:
        return not self.unsupported

    @property
    def fingerprint(self) -> str:
        """Stable hash of the effective weights: a model trained under one fingerprint is
        only valid for leagues with the same offensive scoring."""
        flat = sorted((k, round(v, 6)) for k, v in self.weights.items() if v)
        by_pos = sorted((p, sorted((k, round(v, 6)) for k, v in w.items() if v))
                        for p, w in self.position_weights.items() if any(w.values()))
        # position weights enter the hash only when present, so every ESPN fingerprint is unchanged
        payload = json.dumps([flat, by_pos] if by_pos else flat)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def weights_for(self, position: str | None) -> dict[str, float]:
        extra = self.position_weights.get(position or "", {})
        if not extra:
            return self.weights
        return {c: self.weights.get(c, 0.0) + extra.get(c, 0.0) for c in set(self.weights) | set(extra)}

    def describe(self) -> dict:
        out = {"supported": self.supported, "fingerprint": self.fingerprint,
               "weights": {k: v for k, v in sorted(self.weights.items()) if v},
               "unsupported": self.unsupported, "unmodeled": self.unmodeled}
        if self.position_weights:
            out["position_weights"] = {p: {k: v for k, v in sorted(w.items()) if v}
                                       for p, w in sorted(self.position_weights.items())}
        return out

    def require_supported(self) -> "SkillScoring":
        if not self.supported:
            names = ", ".join(f"stat {u['stat_id']} ({u['reason']})" for u in self.unsupported)
            raise UnsupportedScoring(f"League offensive scoring uses unsupported rules: {names}")
        return self


def _offense_value(item: dict) -> float:
    """Points a non-D/ST slot earns per unit. ESPN overrides are keyed by lineup slot id; only
    D/ST (16) overrides are understood. Any other override makes the value ambiguous."""
    overrides = {str(k): v for k, v in (item.get("pointsOverrides") or {}).items()}
    other = {k: v for k, v in overrides.items() if k != DST_SLOT}
    if other:
        raise UnsupportedScoring(f"stat {item.get('statId')}: slot-specific overrides {sorted(other)} "
                                 "(position-specific scoring such as TE premium) are not supported")
    return float(item.get("points") or 0.0)


def resolve_skill_scoring(scoring_items: list[dict] | None) -> SkillScoring:
    if not scoring_items:
        return SkillScoring(weights={}, unsupported=[{"stat_id": None, "reason": "no scoring settings synced"}])
    weights: dict[str, float] = {}
    position_weights: dict[str, dict[str, float]] = {}
    unsupported, unmodeled, ignored = [], [], []
    returns: dict[int, float] = {}
    direct = {**DIRECT, **EXTRA_DIRECT}
    for item in scoring_items:
        sid = item.get("statId")
        if item.get("isReverseItem"):
            unsupported.append({"stat_id": sid, "reason": "reverse scoring item"})
            continue
        try:
            value = _offense_value(item)
        except UnsupportedScoring as err:
            unsupported.append({"stat_id": sid, "reason": str(err)})
            continue
        extra = {str(p): float(v) for p, v in (item.get("positionPoints") or {}).items() if v}
        if extra and sid not in direct:
            unsupported.append({"stat_id": sid, "reason": "position-specific points on a statistic not implemented"})
            continue
        for pos, pts in extra.items():
            col = direct[sid][0]
            position_weights.setdefault(pos, {})[col] = position_weights.get(pos, {}).get(col, 0.0) + pts
        if sid in direct:
            col = direct[sid][0]
            weights[col] = weights.get(col, 0.0) + value
        elif sid in RETURN_TD_STATS:
            returns[sid] = value
        elif sid in KICKER_ONLY:
            ignored.append({"stat_id": sid, "reason": "kicker statistic (not applicable to QB/RB/WR/TE)"})
        elif value == 0.0:
            ignored.append({"stat_id": sid, "reason": "no points for offensive slots (D/ST override only)"})
        elif sid in UNMODELED_DEFENSIVE:
            unmodeled.append({"stat_id": sid, "points": value, "reason": UNMODELED_DEFENSIVE[sid]})
        else:
            unsupported.append({"stat_id": sid, "points": value,
                                "reason": "rule awards offensive points but is not implemented"})
    if returns:
        values = {returns.get(s, 0.0) for s in RETURN_TD_STATS}
        if len(values) == 1:
            weights[RETURN_TD_COLUMN] = values.pop()
        else:
            unsupported.append({"stat_id": list(RETURN_TD_STATS),
                                "reason": "kick and punt return TDs score differently; nflverse combines them"})
    return SkillScoring(weights=weights, unsupported=unsupported, unmodeled=unmodeled, ignored=ignored,
                        position_weights=position_weights)


def points(row: dict, scoring: SkillScoring, position: str | None = None) -> float | None:
    """League points for one nflverse player-stats row (of a player at `position`, for position weights). None if
    any weighted component is missing (unknown is never treated as zero)."""
    total = 0.0
    for col, w in scoring.weights_for(position).items():
        if not w:
            continue
        v = row.get(col)
        if v is None:
            return None
        total += w * float(v)
    return round(total, 4)


def points_sql(scoring: SkillScoring, alias: str = "", position_col: str | None = None) -> str:
    """SQL expression computing league points from nflverse columns (NULL if any input is NULL). Position weights
    need `position_col` (the row's position column) and refuse to be silently dropped without it."""
    prefix = f"{alias}." if alias else ""

    def total(weights: dict[str, float]) -> str:
        terms = [f"CAST({w!r} AS DOUBLE) * {prefix}{col}" for col, w in sorted(weights.items()) if w]
        return " + ".join(terms) if terms else "NULL::DOUBLE"

    if not any(any(w.values()) for w in scoring.position_weights.values()):
        return total(scoring.weights)
    if position_col is None:
        raise UnsupportedScoring("position-specific scoring needs the row's position column")
    cases = " ".join(f"WHEN {prefix}{position_col} = '{p}' THEN {total(scoring.weights_for(p))}"
                     for p in sorted(scoring.position_weights))
    return f"(CASE {cases} ELSE {total(scoring.weights)} END)"


def reconcile_with_espn(con, season: int, scoring: SkillScoring, tolerance: float = 0.05) -> dict:
    """Compare league points computed from nflverse stats with ESPN's stored actual points for
    the same player-weeks (high-confidence ESPN->GSIS id mappings only). Aggregates only."""
    from ..db.connection import table_columns, table_exists

    needed = {"nfl_weekly_stats", "espn_player_weekly_points", "player_id_map"}
    if not all(table_exists(con, t) for t in needed):
        return {"status": "unavailable", "reason": "missing tables", "by_position": {}}
    missing = [c for c in scoring.weights if c not in set(table_columns(con, "nfl_weekly_stats"))]
    if missing:
        return {"status": "unavailable", "reason": f"nflverse columns missing: {missing}", "by_position": {}}
    expr = points_sql(scoring, "s")
    rows = con.execute(f"""
        SELECT s.position, {expr} AS computed, e.actual_points AS espn
        FROM nfl_weekly_stats s
        JOIN player_id_map m ON m.gsis_id = s.player_id AND m.match_confidence = 'high'
        JOIN espn_player_weekly_points e ON e.espn_player_id = m.espn_player_id
             AND e.season = s.season AND e.scoring_period = s.week
        WHERE s.season = ? AND s.season_type = 'REG' AND e.actual_points IS NOT NULL
          AND s.position IN ('QB', 'RB', 'WR', 'TE')""", [season]).fetchall()
    by_pos: dict[str, dict] = {}
    for pos, computed, espn in rows:
        b = by_pos.setdefault(pos, {"n": 0, "exact": 0, "abs_diff_sum": 0.0, "max_abs_diff": 0.0})
        if computed is None:
            b.setdefault("uncomputable", 0)
            b["uncomputable"] += 1
            continue
        d = abs(computed - espn)
        b["n"] += 1
        b["exact"] += d < tolerance
        b["abs_diff_sum"] += d
        b["max_abs_diff"] = max(b["max_abs_diff"], round(d, 4))
    total = sum(b["n"] for b in by_pos.values())
    exact = sum(b["exact"] for b in by_pos.values())
    for b in by_pos.values():
        b["exact_rate"] = round(b["exact"] / b["n"], 4) if b["n"] else None
        b["mean_abs_diff"] = round(b.pop("abs_diff_sum") / b["n"], 4) if b["n"] else None
    rate = exact / total if total else None
    status = ("unavailable" if not total else "verified" if rate >= RECONCILIATION_MIN_EXACT_RATE else "mismatch")
    return {"status": status, "season": season, "n": total, "exact": exact,
            "exact_rate": round(rate, 4) if rate is not None else None, "tolerance": tolerance,
            "min_exact_rate": RECONCILIATION_MIN_EXACT_RATE, "by_position": by_pos}


RECONCILIATION_MIN_EXACT_RATE = 0.98
