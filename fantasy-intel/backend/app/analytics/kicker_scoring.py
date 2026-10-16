"""League fantasy scoring for kickers (K) from nflverse kicking box scores.

Kept apart from league_scoring.py on purpose: that module scores QB/RB/WR/TE and lists every kicker
statistic as "not applicable", and its fingerprint (the identity of the ifp skill-position models)
must never change because a league edits a kicking rule. This module resolves ONLY what a player
in the K lineup slot earns from kicking, into a per-kick table:

    points(kick) = sum of every configured ESPN item whose definition the kick satisfies

ESPN kicking items (statId -> definition; espn_api.football.constant):
    80/81/82    FG made / attempted / missed, 0-39 yards       ("Under 40")
    77/78/79    FG made / attempted / missed, 40-49 yards
    198/199/200 FG made / attempted / missed, 50-59 yards
    201/202/203 FG made / attempted / missed, 60+ yards
    74/75/76    FG made / attempted / missed, 50+ yards        (overlaps 50-59 AND 60+)
    83/84/85    FG made / attempted / missed, any distance
    86/87/88    PAT made / attempted / missed

Overlap rule: items are additive, exactly as ESPN applies them. A 55-yard make in a league that
configures both 74 (50+) and 198 (50-59) earns both values. That is ESPN's documented behaviour,
not a double count by this code, but because it is a common configuration mistake the resolver
lists such overlaps in `notes`, and only reconciliation against ESPN's own actual points can
confirm a league. "Missed" includes blocked attempts unless `blocked_counts_as_miss` is False
(ESPN derives missed = attempted - made; verified by reconciliation, docs/kicker_projection_engine.md).

Value per slot: ESPN `points` applies to every lineup slot; `pointsOverrides` keyed by slot id
replaces it. Slot 17 is K, so a K override is honoured; overrides for other slots are irrelevant
to a kicker.

Anything else a K-slot player could earn (a fake-kick pass or rush, a 2-pt conversion, a return
TD) is recorded as `unmodeled`: it happens a handful of times a season league-wide, the model does
not project it, and reconciliation measures its effect. An item this module does not understand
that awards a K-slot player non-zero points makes the configuration UNSUPPORTED: callers must
abstain instead of returning points in the wrong units.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

K_SLOT = "17"
MAX_DISTANCE = 80          # yards; a kick beyond this has never been attempted in the NFL

# bucket -> inclusive (low, high) yards; high None = open-ended
BUCKETS: dict[str, tuple[int, int | None]] = {
    "0_39": (0, 39), "40_49": (40, 49), "50_59": (50, 59), "60_plus": (60, None),
    "50_plus": (50, None), "any": (0, None),
}
# statId -> (outcome, bucket); outcome "attempted" applies to made, missed and blocked kicks alike.
FG_ITEMS: dict[int, tuple[str, str]] = {
    80: ("made", "0_39"), 81: ("attempted", "0_39"), 82: ("missed", "0_39"),
    77: ("made", "40_49"), 78: ("attempted", "40_49"), 79: ("missed", "40_49"),
    198: ("made", "50_59"), 199: ("attempted", "50_59"), 200: ("missed", "50_59"),
    201: ("made", "60_plus"), 202: ("attempted", "60_plus"), 203: ("missed", "60_plus"),
    74: ("made", "50_plus"), 75: ("attempted", "50_plus"), 76: ("missed", "50_plus"),
    83: ("made", "any"), 84: ("attempted", "any"), 85: ("missed", "any"),
}
PAT_ITEMS: dict[int, str] = {86: "made", 87: "attempted", 88: "missed"}
# Offensive statistics (league_scoring.DIRECT and return TDs): a K-slot player earns them on a fake or a
# return. Never projected; scored in reconciliation with the skill-player weights.
OFFENSIVE = {3, 4, 19, 20, 24, 25, 26, 42, 43, 44, 53, 63, 72, 101, 102}
# Defensive / D/ST events that carry a non-zero BASE value (so a K slot would technically earn them).
UNMODELED_DEFENSIVE = {93: "blocked kick return TD", 103: "interception return TD", 104: "fumble return TD",
                       206: "defensive 2-pt return", 209: "1-pt safety"}

OUTCOMES = ("made", "missed", "blocked")


class UnsupportedKickerScoring(ValueError):
    """The league scores kickers through a rule this module does not implement."""


def _in_bucket(distance: int, bucket: str) -> bool:
    lo, hi = BUCKETS[bucket]
    return distance >= lo and (hi is None or distance <= hi)


@dataclass(frozen=True)
class KickerScoring:
    fg: dict[int, tuple[str, str, float]]           # statId -> (outcome, bucket, points)
    pat: dict[int, tuple[str, float]]               # statId -> (outcome, points)
    blocked_counts_as_miss: bool = True
    unsupported: list[dict] = field(default_factory=list)
    unmodeled: list[dict] = field(default_factory=list)
    ignored: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return not self.unsupported and bool(self.fg or self.pat)

    def fg_points(self, distance: int, outcome: str) -> float:
        """League points for one field-goal attempt of `distance` yards ending `outcome`
        (made | missed | blocked)."""
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown FG outcome {outcome!r}")
        d = int(distance)
        total = 0.0
        for kind, bucket, pts in self.fg.values():
            if not _in_bucket(d, bucket):
                continue
            if kind == "attempted" or kind == outcome or (
                    kind == "missed" and outcome == "blocked" and self.blocked_counts_as_miss):
                total += pts
        return round(total, 6)

    def pat_points(self, outcome: str) -> float:
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown PAT outcome {outcome!r}")
        total = 0.0
        for kind, pts in self.pat.values():
            if kind == "attempted" or kind == outcome or (
                    kind == "missed" and outcome == "blocked" and self.blocked_counts_as_miss):
                total += pts
        return round(total, 6)

    def table(self) -> dict:
        """Effective points per kick: FG by yard (10..MAX_DISTANCE) for each outcome, and PAT outcomes."""
        return {"fg": {o: [self.fg_points(d, o) for d in range(10, MAX_DISTANCE + 1)] for o in OUTCOMES},
                "pat": {o: self.pat_points(o) for o in OUTCOMES}}

    def segments(self) -> list[dict]:
        """The FG table compressed into distance ranges with identical points (for display)."""
        out: list[dict] = []
        for d in range(10, MAX_DISTANCE + 1):
            pts = {o: self.fg_points(d, o) for o in OUTCOMES}
            if out and out[-1]["points"] == pts:
                out[-1]["high"] = d
            else:
                out.append({"low": d, "high": d, "points": pts})
        if out:
            out[0]["low"] = 0
            out[-1]["high"] = None
        return out

    @property
    def fingerprint(self) -> str:
        """Hash of the EFFECTIVE per-kick points (not of the raw items), so two configurations that
        score every kick identically share a fingerprint. Prefixed 'k:' so it can never be confused
        with a skill-position fingerprint."""
        payload = json.dumps(self.table(), sort_keys=True, separators=(",", ":"))
        return "k:" + hashlib.sha256(payload.encode()).hexdigest()[:16]

    def describe(self) -> dict:
        return {"supported": self.supported, "fingerprint": self.fingerprint if self.supported else None,
                "fg_segments": self.segments(), "pat": self.table()["pat"],
                "blocked_counts_as_miss": self.blocked_counts_as_miss,
                "items": {str(k): {"outcome": o, "bucket": b, "points": p} for k, (o, b, p) in sorted(self.fg.items())}
                | {str(k): {"outcome": o, "bucket": "pat", "points": p} for k, (o, p) in sorted(self.pat.items())},
                "unsupported": self.unsupported, "unmodeled": self.unmodeled, "notes": self.notes}

    def require_supported(self) -> "KickerScoring":
        if not self.supported:
            names = ", ".join(f"stat {u['stat_id']} ({u['reason']})" for u in self.unsupported) or "no kicking rules"
            raise UnsupportedKickerScoring(f"League kicker scoring is not supported: {names}")
        return self


def _k_value(item: dict) -> float:
    overrides = {str(k): v for k, v in (item.get("pointsOverrides") or {}).items()}
    return float(overrides[K_SLOT] if K_SLOT in overrides else (item.get("points") or 0.0))


def resolve_kicker_scoring(scoring_items: list[dict] | None, *, blocked_counts_as_miss: bool = True) -> KickerScoring:
    if not scoring_items:
        return KickerScoring(fg={}, pat={}, unsupported=[{"stat_id": None, "reason": "no scoring settings synced"}])
    fg: dict[int, tuple[str, str, float]] = {}
    pat: dict[int, tuple[str, float]] = {}
    unsupported, unmodeled, ignored, notes = [], [], [], []
    for item in scoring_items:
        sid = item.get("statId")
        value = _k_value(item)
        if item.get("isReverseItem"):
            if value:
                unsupported.append({"stat_id": sid, "reason": "reverse scoring item"})
            continue
        if sid in FG_ITEMS:
            if value:
                outcome, bucket = FG_ITEMS[sid]
                fg[sid] = (outcome, bucket, value)
        elif sid in PAT_ITEMS:
            if value:
                pat[sid] = (PAT_ITEMS[sid], value)
        elif value == 0.0:
            ignored.append({"stat_id": sid, "reason": "no points for the K slot"})
        elif sid in OFFENSIVE:
            unmodeled.append({"stat_id": sid, "points": value,
                              "reason": "offensive statistic (fake kick / return): not projected"})
        elif sid in UNMODELED_DEFENSIVE:
            unmodeled.append({"stat_id": sid, "points": value, "reason": UNMODELED_DEFENSIVE[sid]})
        else:
            unsupported.append({"stat_id": sid, "points": value,
                                "reason": "rule awards K-slot points but is not implemented"})
    made_buckets = {b for o, b, _ in fg.values() if o == "made"}
    if "50_plus" in made_buckets and made_buckets & {"50_59", "60_plus"}:
        notes.append("both 'FG made 50+' and a 50-59/60+ made item are configured: a long make earns both, as "
                     "ESPN applies them; confirm with reconciliation")
    if not fg:
        unsupported.append({"stat_id": None, "reason": "the league awards no points for field goals"})
    return KickerScoring(fg=fg, pat=pat, blocked_counts_as_miss=blocked_counts_as_miss, unsupported=unsupported,
                         unmodeled=unmodeled, ignored=ignored, notes=notes)


# ----------------------------------------------------------------------------- box scores


def parse_distances(value) -> list[int]:
    """nflverse `fg_made_list` / `fg_missed_list` / `fg_blocked_list`: ';'-separated yards (None/'' = none)."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [int(v) for v in value if v is not None and str(v).strip() != ""]
    return [int(float(v)) for v in str(value).replace(",", ";").split(";") if v.strip()]


def box_score_kicks(row: dict) -> dict | None:
    """FG attempts [(distance, outcome)] and PAT outcome counts from one nflverse player-stats row.
    None when the row's FG lists are inconsistent with its FG counts (the per-kick table is then
    unknown, never guessed)."""
    made = parse_distances(row.get("fg_made_list"))
    missed = parse_distances(row.get("fg_missed_list"))
    blocked = parse_distances(row.get("fg_blocked_list"))
    counts = (row.get("fg_made") or 0, row.get("fg_missed") or 0, row.get("fg_blocked") or 0)
    if (len(made), len(missed), len(blocked)) != tuple(int(c) for c in counts):
        return None
    pat_blocked = int(row.get("pat_blocked") or 0)
    pat_made = int(row.get("pat_made") or 0)
    pat_att = int(row.get("pat_att") or 0)
    # nflverse: pat_att = made + missed + blocked; pat_missed counts only non-blocked misses
    pat_missed = max(pat_att - pat_made - pat_blocked, 0)
    return {"fg": [(d, "made") for d in made] + [(d, "missed") for d in missed] + [(d, "blocked") for d in blocked],
            "pat": {"made": pat_made, "missed": pat_missed, "blocked": pat_blocked}}


def kicking_points(row: dict, scoring: KickerScoring) -> float | None:
    kicks = box_score_kicks(row)
    if kicks is None:
        return None
    total = sum(scoring.fg_points(d, o) for d, o in kicks["fg"])
    total += sum(n * scoring.pat_points(o) for o, n in kicks["pat"].items())
    return round(total, 4)


# ----------------------------------------------------------------------------- reconciliation

RECONCILIATION_MIN_EXACT_RATE = 0.98
STAT_COLUMNS = ("fg_made", "fg_missed", "fg_blocked", "fg_made_list", "fg_missed_list", "fg_blocked_list",
                "pat_made", "pat_att", "pat_missed", "pat_blocked")


def league_items(con, season: int) -> list[dict] | None:
    """The live league's scoring items (legacy_league.latest_league: the one sanctioned single-league selector)."""
    from .legacy_league import latest_league

    row = latest_league(con, ["scoring_items"], season=season)
    if not row:
        return None
    items = row["scoring_items"]
    return json.loads(items) if isinstance(items, str) else items


def reconcile_with_espn(con, season: int, scoring: KickerScoring, scoring_items: list[dict] | None = None,
                        tolerance: float = 0.05) -> dict:
    """Compare kicker points computed from nflverse box scores (kicking via this resolver, plus any
    offensive statistic via the skill weights) with ESPN's stored actual points for the same K
    player-weeks (high-confidence ESPN->GSIS mappings only). Returns aggregates and every mismatch
    (player ids, week, both values): kicker box scores are public, and the list is how a rule is
    diagnosed. Weeks with ESPN points but no nflverse row (a kicker who dressed and never kicked)
    are counted separately, never assumed zero."""
    from ..db.connection import fetch_dicts, table_columns, table_exists
    from .league_scoring import resolve_skill_scoring

    needed = {"nfl_weekly_stats", "espn_player_weekly_points", "player_id_map", "fantasy_players"}
    if not all(table_exists(con, t) for t in needed):
        return {"status": "unavailable", "reason": "missing tables"}
    cols = set(table_columns(con, "nfl_weekly_stats"))
    if not set(STAT_COLUMNS) <= cols:
        return {"status": "unavailable", "reason": "nflverse kicking columns missing"}
    skill = resolve_skill_scoring(scoring_items) if scoring_items else None
    off_cols = sorted(c for c in (skill.weights if skill else {}) if c in cols)
    pairs = fetch_dicts(con, f"""
        SELECT e.espn_player_id, e.scoring_period AS week, e.actual_points AS espn, m.gsis_id,
               s.player_id IS NOT NULL AS has_stats, {', '.join('s.' + c for c in STAT_COLUMNS)}
               {''.join(', s.' + c for c in off_cols)}
        FROM espn_player_weekly_points e
        JOIN fantasy_players f ON f.espn_player_id = e.espn_player_id AND f.position = 'K'
        JOIN player_id_map m ON m.espn_player_id = e.espn_player_id AND m.match_confidence = 'high'
        LEFT JOIN nfl_weekly_stats s ON s.player_id = m.gsis_id AND s.season = e.season AND s.week = e.scoring_period
             AND s.season_type = 'REG'
        WHERE e.season = ? AND e.actual_points IS NOT NULL
        ORDER BY e.scoring_period, e.espn_player_id""", [season])
    n = exact = no_row = uncomputable = 0
    abs_sum, max_abs, mismatches, unmodeled_weeks = 0.0, 0.0, [], 0
    for p in pairs:
        if not p["has_stats"]:
            no_row += 1
            if abs(p["espn"]) >= tolerance:
                mismatches.append({"espn_player_id": p["espn_player_id"], "gsis_id": p["gsis_id"], "week": p["week"],
                                   "espn": p["espn"], "computed": None, "reason": "ESPN points but no nflverse row"})
            continue
        k = kicking_points(p, scoring)
        if k is None:
            uncomputable += 1
            continue
        off = sum(skill.weights[c] * float(p[c] or 0) for c in off_cols) if skill else 0.0
        unmodeled_weeks += off != 0
        computed = round(k + off, 4)
        d = abs(computed - p["espn"])
        n += 1
        exact += d < tolerance
        abs_sum += d
        max_abs = max(max_abs, d)
        if d >= tolerance:
            mismatches.append({"espn_player_id": p["espn_player_id"], "gsis_id": p["gsis_id"], "week": p["week"],
                               "espn": p["espn"], "computed": computed, "kicking": k, "offense": round(off, 4)})
    rate = exact / n if n else None
    status = "unavailable" if not n else "verified" if rate >= RECONCILIATION_MIN_EXACT_RATE else "mismatch"
    return {"status": status, "season": season, "fingerprint": scoring.fingerprint, "n": n, "exact": exact,
            "exact_rate": None if rate is None else round(rate, 4), "tolerance": tolerance,
            "min_exact_rate": RECONCILIATION_MIN_EXACT_RATE,
            "mean_abs_diff": round(abs_sum / n, 4) if n else None, "max_abs_diff": round(max_abs, 4),
            "espn_weeks_without_nflverse_row": no_row, "uncomputable_rows": uncomputable,
            "weeks_with_offensive_points": unmodeled_weeks, "mismatches": mismatches}
