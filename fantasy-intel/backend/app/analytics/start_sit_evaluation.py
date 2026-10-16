"""Real fantasy-roster Start/Sit decision evaluation (research; read-only; changes no lineup, no
projection and no model).

Question: do a model's projections make better decisions on the rosters people actually hold than ESPN's
and than simple baselines? The decisions are the ones a manager faces on a real roster:

  a. same-roster, same-position pairs (who starts), accuracy and regret like forecast_backtest.pair_decisions
  b. cross-position pairs that compete for a flex slot of that league
  c. the whole skill lineup (QB/RB/WR/TE and flex slots): points of the model's best lineup against the
     hindsight-best lineup (regret in points: mean, p90, max) and against ESPN's best lineup
  d. close calls: pairs whose recent-average baseline points differ by at most CLOSE_PAIR_POINTS

Rules that keep it honest:
  * Equal population. A decision is scored only if EVERY compared model has a valid pregame prediction for
    EVERY player it involves; otherwise it is excluded and counted by reason (never imputed).
  * A prediction is pregame only if it was captured strictly before the player's own kickoff, or it is a
    point-in-time baseline built from earlier weeks only. A model with no such evidence is unavailable.
  * Outcomes follow the weekly-learning finality statuses: APPEARED scores actual league points,
    CONFIRMED_ABSENT scores 0 (these decisions are unconditional: a player who sits scores nothing), every
    other status is excluded and counted, never scored.
  * The roster is whatever lineup_snapshots held when it was last synced. That is pregame only for a row
    synced before its player's kickoff; otherwise it is `post_hoc_snapshot` and reported as such.
  * Nothing here uses the eventual outcome to decide who was eligible.

The decision layer is plain python (no DB, no polars, no numpy). SQL helpers (rosters, ESPN snapshots,
legacy production reader) use duckdb rows only; outcomes, the A1/A2 baselines and the frozen research
ledger are attached by scripts/start_sit_evaluation.py. See docs/start_sit_evaluation.md.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..db.connection import fetch_dicts, table_exists
from .lineup import FLEX_ELIGIBILITY, assign_lineup, eligible, lineup_slots

UTC = timezone.utc
SKILL = ("QB", "RB", "WR", "TE")
CLOSE_PAIR_POINTS = 3.0                      # same value as forecast_backtest.CLOSE_PAIR_POINTS (tested)
APPEARED, CONFIRMED_ABSENT = "appeared", "confirmed_absent"      # weekly-learning outcome statuses
REFERENCE_MODELS = ("espn", "A1")
VERDICTS = ("insufficient_evidence", "no_significant_difference", "better", "worse")
DECISION_SET_SCHEMA = "start-sit-decision-set/v1"
KNOWN_PAIR_NOTE = ("Context: the held-out Start/Sit pair accuracy of 64.7% (context model) vs 64.6% (recent "
                   "average) covers 1,012 same-roster pairs (forecast_backtest.league_roster_pairs). That is a "
                   "comparison of two of OUR models on one held-out set, not an edge over ESPN, and it is not "
                   "the decision layer measured here.")


# ----------------------------------------------------------------------------- time / numbers


def aware(value: Any) -> datetime | None:
    """Repository TIMESTAMPs are naive UTC; return an aware UTC datetime (None if unparseable)."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _round(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(x, nd)


# ----------------------------------------------------------------------------- candidate state
#
# A candidate is one roster player in one (league, fantasy team, season, week):
#   league_id, fantasy_team_id, season, week, espn_player_id, gsis_id, position, team (NFL team at forecast
#   time), kickoff (his own game, aware UTC or None), slot ('QB' ... 'BE', 'IR'), is_starter,
#   roster_synced_at, outcome_status, actual_points, and
#   preds: {model: {"value": float|None, "captured_at": datetime|None, "point_in_time": bool}}
# Key names follow CONTRACTS section 6 (season, week, gsis_id, espn_player_id, position, team, kickoff,
# outcome_status, actual_points) so the module can switch to the benchmark's evaluation rows.


def prediction(c: Mapping, model: str) -> tuple[float | None, str | None]:
    """(value, None) if `model` has a genuine pregame prediction for the candidate, else (None, reason)."""
    p = (c.get("preds") or {}).get(model)
    if not p:
        return None, f"no_prediction:{model}"
    v = _finite(p.get("value"))
    if v is None:
        return None, f"no_prediction:{model}"
    if p.get("point_in_time"):
        return v, None
    cap, ko = aware(p.get("captured_at")), aware(c.get("kickoff"))
    if cap is None:
        return None, f"unknown_capture_time:{model}"
    if ko is None:
        return None, f"unknown_kickoff:{model}"
    if cap >= ko:
        return None, f"post_kickoff_prediction:{model}"
    return v, None


def outcome(c: Mapping) -> tuple[float | None, str | None]:
    """(actual points, None) for a resolved outcome; CONFIRMED_ABSENT is 0.0; else (None, reason)."""
    status = c.get("outcome_status")
    if status == APPEARED:
        v = _finite(c.get("actual_points"))
        return (v, None) if v is not None else (None, "outcome_missing_points")
    if status == CONFIRMED_ABSENT:
        return 0.0, None
    return None, f"outcome_{status or 'unknown'}"


def candidate_state(c: Mapping, models: Sequence[str]) -> dict:
    """actual + every compared model's prediction, or the first reason the candidate cannot be used."""
    actual, why = outcome(c)
    if why:
        return {"ok": False, "reason": why}
    vals = {}
    for m in models:
        v, why = prediction(c, m)
        if why:
            return {"ok": False, "reason": why}
        vals[m] = v
    return {"ok": True, "actual": actual, "values": vals}


def roster_basis(cands: Iterable[Mapping]) -> str:
    """`pregame_snapshot` only if every roster row was synced strictly before its player's kickoff."""
    for c in cands:
        s, ko = aware(c.get("roster_synced_at")), aware(c.get("kickoff"))
        if s is None or ko is None or s >= ko:
            return "post_hoc_snapshot"
    return "pregame_snapshot"


# ----------------------------------------------------------------------------- decisions


@dataclass(frozen=True)
class EvalConfig:
    min_decisions: int = 200
    min_weeks: int = 8
    resamples: int = 2000
    seed: int = 20261016
    close_points: float = CLOSE_PAIR_POINTS
    close_basis: str = "A1"
    roster_policy: str = "complete"       # complete | valid_subset
    reference: str = "espn"

    def __post_init__(self):
        if self.roster_policy not in ("complete", "valid_subset"):
            raise ValueError("roster_policy must be 'complete' or 'valid_subset'")


def _team_weeks(cands: Iterable[Mapping]) -> dict[tuple, list[Mapping]]:
    groups: dict[tuple, dict] = defaultdict(dict)
    for c in cands:
        groups[(c.get("league_id"), c.get("fantasy_team_id"), c["season"], c["week"])].setdefault(
            c.get("espn_player_id", c.get("gsis_id")), c)      # one row per player per roster
    return {k: list(v.values()) for k, v in groups.items()}


def _pair_score(pi: float, pj: float, ai: float, aj: float) -> tuple[float, float]:
    best = max(ai, aj)
    if pi == pj:
        return 0.5, abs(ai - aj) / 2
    pick = ai if pi > pj else aj
    return float(pick == best), best - pick


def decisions(cands: Iterable[Mapping], models: Sequence[str], settings: Mapping[Any, Mapping[str, int]],
              config: EvalConfig = EvalConfig()) -> dict:
    """Pair rows (same-position and flex), lineup rows and the exclusion counts for ONE equal population:
    every decision here has a valid pregame prediction from every model in `models`."""
    models = list(models)
    pairs: list[dict] = []
    lineups: list[dict] = []
    ex = {"pairs": Counter(), "lineups": Counter(), "candidates": Counter(), "actual_ties_skipped": 0}
    for (lg, tm, season, week), roster in sorted(_team_weeks(cands).items(), key=lambda kv: str(kv[0])):
        slots_cfg = settings.get(lg)
        basis = roster_basis(roster)
        live, states = [], {}
        for c in roster:
            if c.get("slot") == "IR":
                ex["candidates"]["ir_slot"] += 1
            elif c.get("position") not in SKILL:
                ex["candidates"]["not_skill_position"] += 1
            else:
                live.append(c)
                states[id(c)] = candidate_state(c, models)
        counts = Counter(c["position"] for c in live)
        close_basis = {id(c): prediction(c, config.close_basis)[0] for c in live}
        for i in range(len(live)):
            for j in range(i + 1, len(live)):
                ci, cj = live[i], live[j]
                kind = "same_position" if ci["position"] == cj["position"] else "flex"
                if kind == "flex" and not (slots_cfg and any(
                        eligible(s, ci["position"]) and eligible(s, cj["position"])
                        for s, n in slots_cfg.items() if s in FLEX_ELIGIBILITY and n)):
                    continue                      # no flex slot both could fill: not a decision
                bad = next((st["reason"] for st in (states[id(ci)], states[id(cj)]) if not st["ok"]), None)
                if bad:
                    ex["pairs"][bad] += 1
                    continue
                si, sj = states[id(ci)], states[id(cj)]
                if si["actual"] == sj["actual"]:
                    ex["actual_ties_skipped"] += 1
                    continue
                bi, bj = close_basis[id(ci)], close_basis[id(cj)]
                row = {"season": season, "week": week, "league_id": lg, "fantasy_team_id": tm, "kind": kind,
                       "pair": tuple(sorted((str(ci.get("gsis_id")), str(cj.get("gsis_id"))))),
                       "positions": (ci["position"], cj["position"]), "roster_basis": basis,
                       "close": None if bi is None or bj is None else abs(bi - bj) <= config.close_points,
                       "contested": None if not slots_cfg or kind != "same_position" else
                       counts[ci["position"]] > int(slots_cfg.get(ci["position"], 0)),
                       "score": {}, "regret": {}}
                for m in models:
                    row["score"][m], row["regret"][m] = _pair_score(si["values"][m], sj["values"][m],
                                                                    si["actual"], sj["actual"])
                pairs.append(row)
        if slots_cfg:
            row = _lineup_row(live, states, models, slots_cfg, (lg, tm, season, week), basis, config, ex["lineups"])
            if row:
                lineups.append(row)
        else:
            ex["lineups"]["slot_settings_unknown"] += 1
    return {"pairs": pairs, "lineups": lineups, "exclusions": {k: dict(v) if isinstance(v, Counter) else v
                                                                for k, v in ex.items()}}


def skill_slots(roster_slots: Mapping[str, int]) -> list[str]:
    """Starting slots a skill-position model can fill: QB/RB/WR/TE and the flex slots (K, D/ST excluded)."""
    return [s for s in lineup_slots(dict(roster_slots)) if s not in ("K", "D/ST")]


def _lineup_row(live, states, models, slots_cfg, key, basis, config, excl: Counter) -> dict | None:
    slots = skill_slots(slots_cfg)
    if not slots:
        excl["no_skill_slots"] += 1
        return None
    bad = [states[id(c)]["reason"] for c in live if not states[id(c)]["ok"]]
    if bad and config.roster_policy == "complete":
        excl[bad[0]] += 1
        return None
    use = [c for c in live if states[id(c)]["ok"]]
    pid = lambda c: c.get("espn_player_id") if c.get("espn_player_id") is not None else c.get("gsis_id")  # noqa: E731
    actual = {pid(c): states[id(c)]["actual"] for c in use}

    def total(value_of):
        picks = assign_lineup([{"espn_player_id": pid(c), "position": c["position"], "value": value_of(c)} for c in use],
                              slots)
        ids = [p["player_id"] for p in picks if p["player_id"] is not None]
        return ids, sum(actual[i] for i in ids)

    hind_ids, hind = total(lambda c: states[id(c)]["actual"])
    if len(hind_ids) < len(slots):
        excl["roster_cannot_fill_slots"] += 1
        return None
    row = {"season": key[2], "week": key[3], "league_id": key[0], "fantasy_team_id": key[1], "roster_basis": basis,
           "slots": len(slots), "dropped_players": len(live) - len(use), "hindsight_points": hind,
           "points": {}, "regret": {}, "manager_points": None}
    for m in models:
        _, pts = total(lambda c, m=m: states[id(c)]["values"][m])
        row["points"][m], row["regret"][m] = pts, max(0.0, hind - pts)
    starters = [c for c in live if c.get("is_starter") and c.get("slot") not in ("BE", "IR")]
    if len(starters) == len(slots) and all(states[id(c)]["ok"] for c in starters):
        row["manager_points"] = sum(states[id(c)]["actual"] for c in starters)
    return row


# ----------------------------------------------------------------------------- statistics


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        raise ValueError("no values")
    pos = (len(sorted_vals) - 1) * q / 100.0
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def cluster_bootstrap(rows: Sequence[Mapping], value: Callable[[Mapping], float], *, resamples: int,
                      seed: int) -> dict:
    """Mean of `value` over rows with a 95% CI that resamples whole (season, week) clusters, because
    decisions of the same NFL week (all leagues, all rosters) share games and are correlated. Seeded and
    deterministic. No rows: everything None."""
    by: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        agg = by[(r["season"], r["week"])]
        agg[0] += value(r)
        agg[1] += 1
    clusters = [by[k] for k in sorted(by)]
    n = sum(c[1] for c in clusters)
    if not n:
        return {"mean": None, "ci95": None, "n": 0, "weeks": 0}
    point = sum(c[0] for c in clusters) / n
    rng = random.Random(seed)
    k, draws = len(clusters), []
    for _ in range(resamples):
        total = cnt = 0.0
        for _i in range(k):
            c = clusters[rng.randrange(k)]
            total += c[0]
            cnt += c[1]
        draws.append(total / cnt)
    draws.sort()
    return {"mean": point, "ci95": [_percentile(draws, 2.5), _percentile(draws, 97.5)], "n": n, "weeks": k}


def verdict(boot: Mapping, *, higher_is_better: bool, min_decisions: int, min_weeks: int) -> str:
    """insufficient_evidence below the minimum decisions or weeks; no_significant_difference when the 95%
    CI of (model - reference) covers 0; otherwise better/worse by the sign and the metric's direction."""
    if boot["n"] < min_decisions or boot["weeks"] < min_weeks or boot["ci95"] is None:
        return "insufficient_evidence"
    lo, hi = boot["ci95"]
    if lo <= 0 <= hi:
        return "no_significant_difference"
    positive = lo > 0
    return "better" if positive == higher_is_better else "worse"


def _tail(values: Sequence[float]) -> dict:
    if not values:
        return {"mean": None, "p90": None, "max": None}
    s = sorted(values)
    return {"mean": _round(sum(s) / len(s)), "p90": _round(_percentile(s, 90)), "max": _round(s[-1])}


def _pair_stats(rows: Sequence[Mapping], models: Sequence[str]) -> dict:
    out = {}
    for m in models:
        n = len(rows)
        out[m] = {"pairs": n,
                  "accuracy": _round(sum(r["score"][m] for r in rows) / n) if n else None,
                  "mean_regret": _round(sum(r["regret"][m] for r in rows) / n) if n else None}
    return out


def _comparisons(rows: Sequence[Mapping], models: Sequence[str], metric: Callable[[Mapping, str], float],
                 higher_is_better: bool, config: EvalConfig, references: Sequence[str]) -> dict:
    out = {}
    for m in models:
        for ref in references:
            if m == ref or ref not in models:
                continue
            boot = cluster_bootstrap(rows, lambda r, m=m, ref=ref: metric(r, m) - metric(r, ref),
                                     resamples=config.resamples, seed=config.seed)
            out.setdefault(m, {})[f"vs_{ref}"] = {
                "difference": _round(boot["mean"]), "ci95": None if boot["ci95"] is None else [
                    _round(boot["ci95"][0]), _round(boot["ci95"][1])], "n": boot["n"], "weeks": boot["weeks"],
                "verdict": verdict(boot, higher_is_better=higher_is_better, min_decisions=config.min_decisions,
                                   min_weeks=config.min_weeks)}
    return out


def evaluate(cands: Sequence[Mapping], models: Sequence[str], settings: Mapping[Any, Mapping[str, int]],
             config: EvalConfig = EvalConfig()) -> dict:
    """The full evaluation of ONE equal population (see `decisions`). `models` must include the reference
    models to be compared against (ESPN, A1); a model is only ever compared on decisions all models share."""
    models = list(models)
    d = decisions(cands, models, settings, config)
    same = [r for r in d["pairs"] if r["kind"] == "same_position"]
    flex = [r for r in d["pairs"] if r["kind"] == "flex"]
    refs = [r for r in REFERENCE_MODELS if r in models]
    sections = {"same_position": same, "flex": flex,
                "close_calls": [r for r in same + flex if r["close"]],
                "contested": [r for r in same if r["contested"]]}
    out: dict[str, Any] = {"models": models, "config": {
        "min_decisions": config.min_decisions, "min_weeks": config.min_weeks, "resamples": config.resamples,
        "seed": config.seed, "close_points": config.close_points, "close_basis": config.close_basis,
        "roster_policy": config.roster_policy}}
    for name, rows in sections.items():
        out[name] = {"decisions": len(rows), "weeks": len({(r["season"], r["week"]) for r in rows}),
                     "unique_player_pairs": len({(r["season"], r["week"], r["pair"]) for r in rows}),
                     "stats": _pair_stats(rows, models),
                     "accuracy_difference": _comparisons(rows, models, lambda r, m: r["score"][m], True, config, refs),
                     "regret_difference": _comparisons(rows, models, lambda r, m: r["regret"][m], False, config, refs)}
    lu = d["lineups"]
    out["lineups"] = {
        "decisions": len(lu), "weeks": len({(r["season"], r["week"]) for r in lu}),
        "by_roster_basis": dict(Counter(r["roster_basis"] for r in lu)),
        "hindsight_mean_points": _round(sum(r["hindsight_points"] for r in lu) / len(lu)) if lu else None,
        "regret": {m: _tail([r["regret"][m] for r in lu]) for m in models},
        "regret_difference": _comparisons(lu, models, lambda r, m: r["regret"][m], False, config, refs),
        # points won/lost against the reference lineup (positive = the model's lineup scored more)
        "points_vs_reference": {
            m: cluster_bootstrap(lu, lambda r, m=m: r["points"][m] - r["points"][config.reference],
                                 resamples=config.resamples, seed=config.seed)
            for m in models if m != config.reference and config.reference in models},
        "manager_lineup": _manager(lu, models)}
    for m, b in out["lineups"]["points_vs_reference"].items():
        out["lineups"]["points_vs_reference"][m] = {
            "difference": _round(b["mean"]), "ci95": None if b["ci95"] is None else [_round(b["ci95"][0]), _round(b["ci95"][1])],
            "n": b["n"], "weeks": b["weeks"],
            "verdict": verdict(b, higher_is_better=True, min_decisions=config.min_decisions, min_weeks=config.min_weeks)}
    out["pregame_roster_only"] = _pregame_roster_counts(d)
    out["exclusions"] = d["exclusions"]
    out["notes"] = [KNOWN_PAIR_NOTE,
                    "Lineups cover skill slots only (QB/RB/WR/TE and flex); K and D/ST are not modelled.",
                    "Decisions of one real roster are not independent of the same player on another roster or of "
                    "other decisions of the same NFL week; intervals resample whole weeks."]
    return out


def _manager(lineups: Sequence[Mapping], models: Sequence[str]) -> dict:
    """Reference only: the lineup actually set at the roster snapshot (a completed week's snapshot holds
    the final lineup, so this is NOT a pregame decision)."""
    have = [r for r in lineups if r["manager_points"] is not None]
    return {"lineups": len(have), "basis": "roster as last synced; not provably pregame",
            "mean_points_vs_manager": {m: _round(sum(r["points"][m] - r["manager_points"] for r in have) / len(have))
                                       for m in models} if have else {}}


def _pregame_roster_counts(d: Mapping) -> dict:
    pg = [r for r in d["lineups"] if r["roster_basis"] == "pregame_snapshot"]
    pp = [r for r in d["pairs"] if r["roster_basis"] == "pregame_snapshot"]
    return {"lineups": len(pg), "pairs": len(pp),
            "note": "decisions whose every roster row was synced before its player's kickoff"}


# ----------------------------------------------------------------------------- model sets


def availability(cands: Sequence[Mapping], models: Sequence[str]) -> dict:
    """Per model: candidates with a genuine pregame prediction. A model with none is `unavailable`
    (reported, never backfilled)."""
    out = {}
    for m in models:
        reasons: Counter = Counter()
        ok = 0
        for c in cands:
            v, why = prediction(c, m)
            if why:
                reasons[why.split(":")[0]] += 1
            else:
                ok += 1
        out[m] = {"candidates": len(cands), "with_pregame_prediction": ok, "unavailable": ok == 0,
                  "missing_reasons": dict(reasons)}
    return out


DEFAULT_MODEL_SETS = {
    "baselines": ["espn", "A1", "A2"],
    "ifp1_production": ["espn", "A1", "ifp1_production"],
    "ifp1_frozen": ["espn", "A1", "ifp1_frozen"],
    "ifp2_frozen": ["espn", "A1", "ifp2_frozen"],
}


def evaluate_sets(cands: Sequence[Mapping], settings: Mapping[Any, Mapping[str, int]],
                  model_sets: Mapping[str, Sequence[str]] = DEFAULT_MODEL_SETS,
                  config: EvalConfig = EvalConfig()) -> dict:
    """Each named set is its own equal population, so a sparse model (ifp-2) never shrinks the baselines'
    population. A set containing an unavailable model is reported as unavailable, not evaluated."""
    every = sorted({m for ms in model_sets.values() for m in ms})
    avail = availability(cands, every)
    sets = {}
    for name, ms in model_sets.items():
        missing = [m for m in ms if avail[m]["unavailable"]]
        sets[name] = ({"models": list(ms), "unavailable_models": missing, "evaluated": False}
                      if missing else {"evaluated": True, **evaluate(cands, ms, settings, config)})
    return {"availability": avail, "sets": sets}


# ----------------------------------------------------------------------------- SQL: rosters, ESPN, production


def league_settings(con, season: int) -> dict[int, dict]:
    """{league_id: {"roster_slots": {...}, "scoring_items": [...]}} from the leagues table."""
    if not table_exists(con, "leagues"):
        return {}
    out = {}
    for r in fetch_dicts(con, "SELECT league_id, roster_slots, scoring_items FROM leagues WHERE season = ?", [season]):
        def parse(x):
            return json.loads(x) if isinstance(x, str) else x
        out[r["league_id"]] = {"roster_slots": {k: int(v) for k, v in (parse(r["roster_slots"]) or {}).items()},
                               "scoring_items": parse(r["scoring_items"])}
    return out


def schedule_kickoffs(con, season: int) -> dict[tuple, datetime | None]:
    """(week, nfl team) -> kickoff (aware UTC; None when the Eastern clock is missing or ambiguous)."""
    from .projection_backtest import _kickoff

    if not table_exists(con, "nfl_schedules"):
        return {}
    out = {}
    for g in fetch_dicts(con, "SELECT week, gameday, gametime, home_team, away_team FROM nfl_schedules "
                              "WHERE season = ? AND game_type = 'REG'", [season]):
        ko = _kickoff(g)
        out[(g["week"], g["home_team"])] = out[(g["week"], g["away_team"])] = ko
    return out


def roster_candidates(con, season: int, weeks: Iterable[int] | None = None) -> list[dict]:
    """Roster players per (league, fantasy team, week) exactly as lineup_snapshots holds them (roster at
    snapshot; `roster_synced_at` says when). Identity follows the benchmark rule: a high-confidence ESPN to
    GSIS mapping without a conflict note, else gsis_id is None (unresolved identity, never guessed). The
    NFL team is the player's team in the nflverse weekly roster of that week (latest week <= W), falling
    back to ESPN's pro team."""
    if not table_exists(con, "lineup_snapshots"):
        return []
    wk = "" if weeks is None else f"AND s.scoring_period IN ({', '.join(str(int(w)) for w in weeks)})"
    rows = fetch_dicts(con, f"""
        SELECT s.league_id, s.team_id AS fantasy_team_id, s.season, s.scoring_period AS week, s.espn_player_id,
               s.lineup_slot AS slot, s.is_starter, s.synced_at AS roster_synced_at,
               m.gsis_id, m.match_confidence, m.notes, coalesce(m.espn_position, fp.position) AS position,
               fp.pro_team
        FROM lineup_snapshots s LEFT JOIN player_id_map m USING (espn_player_id)
        LEFT JOIN fantasy_players fp USING (espn_player_id)
        WHERE s.season = ? {wk} ORDER BY s.league_id, s.team_id, s.scoring_period, s.espn_player_id""", [season])
    teams: dict[str, list] = defaultdict(list)
    if table_exists(con, "nfl_rosters_weekly"):
        for r in fetch_dicts(con, "SELECT gsis_id, week, any_value(team) AS team FROM nfl_rosters_weekly "
                                  "WHERE season = ? AND gsis_id IS NOT NULL GROUP BY 1, 2 ORDER BY 2", [season]):
            teams[r["gsis_id"]].append((r["week"], r["team"]))
    kick = schedule_kickoffs(con, season)
    out = []
    for r in rows:
        trusted = r["gsis_id"] and r["match_confidence"] == "high" and "conflict" not in (r["notes"] or "").lower()
        gsis = r["gsis_id"] if trusted else None
        hist = [t for w, t in teams.get(gsis, []) if w <= r["week"]]
        team = hist[-1] if hist else r["pro_team"]
        out.append({"league_id": r["league_id"], "fantasy_team_id": r["fantasy_team_id"], "season": r["season"],
                    "week": r["week"], "espn_player_id": r["espn_player_id"], "gsis_id": gsis,
                    "position": r["position"], "team": team, "kickoff": kick.get((r["week"], team)),
                    "slot": r["slot"], "is_starter": r["is_starter"],
                    "roster_synced_at": aware(r["roster_synced_at"]), "preds": {}})
    return out


def espn_pregame_predictions(con, season: int, cands: Iterable[Mapping], *, as_of: datetime | None = None) -> dict:
    """{(week, espn_player_id): {"value", "captured_at"}}: the latest ESPN projection snapshot captured
    strictly before the player's OWN kickoff (and at/before `as_of` when given). A snapshot at or after
    kickoff is never used, whatever its value."""
    if not table_exists(con, "espn_projection_history"):
        return {}
    wanted = {(c["week"], c["espn_player_id"]): aware(c.get("kickoff")) for c in cands}
    snaps = defaultdict(list)
    for r in fetch_dicts(con, "SELECT scoring_period AS week, espn_player_id, projected_points, synced_at "
                              "FROM espn_projection_history WHERE season = ? AND projected_points IS NOT NULL", [season]):
        snaps[(r["week"], r["espn_player_id"])].append((aware(r["synced_at"]), float(r["projected_points"])))
    cutoff = aware(as_of)
    out = {}
    for key, ko in wanted.items():
        ok = [(t, v) for t, v in snaps.get(key, []) if t is not None and ko is not None and t < ko
              and (cutoff is None or t <= cutoff)]
        if ok:
            t, v = max(ok, key=lambda x: x[0])
            out[key] = {"value": v, "captured_at": t, "point_in_time": False}
    return out


def attach_model(cands: Sequence[dict], model: str, lookup: Mapping, key: Callable[[Mapping], Any]) -> int:
    """Set preds[model] on candidates from lookup[key(candidate)] ({"value", "captured_at", "point_in_time"})."""
    n = 0
    for c in cands:
        p = lookup.get(key(c))
        if p is not None:
            c.setdefault("preds", {})[model] = dict(p)
            n += 1
    return n


def legacy_forecast_reader(con, season: int, weeks: Iterable[int] | None = None, *, model_version: str,
                           scoring_fingerprint: str) -> tuple[list[dict], list[dict]]:
    """CONTRACTS section 5 reader over TODAY's tables (independent_projection_runs + independent_player_projections,
    one run per week, replaced by each new run). Rows come back only when the run was created strictly
    before the player's own kickoff, the model version and scoring fingerprint match, and the artifact
    predates the week's first kickoff; everything else is an exclusion. Replace with the immutable
    production store's `pregame_forecasts` once it lands (same signature)."""
    tables = ("independent_projection_runs", "independent_player_projections")
    if not all(table_exists(con, t) for t in tables):
        return [], [{"run_id": None, "week": None, "reason": "no_production_forecast_tables"}]
    kick = schedule_kickoffs(con, season)
    first: dict[int, datetime] = {}
    for (w, _t), ko in kick.items():
        if ko is not None and (w not in first or ko < first[w]):
            first[w] = ko
    wk = "" if weeks is None else f"AND p.week IN ({', '.join(str(int(w)) for w in weeks)})"
    rows, excl = [], Counter()
    for r in fetch_dicts(con, f"""
            SELECT p.*, r.model_version, r.scoring_fingerprint, r.artifact_created_at, r.created_at AS run_created_at
            FROM independent_player_projections p JOIN independent_projection_runs r USING (run_id)
            WHERE p.season = ? {wk}""", [season]):
        why = None
        ko = kick.get((r["week"], r["nfl_team"]))
        created, artifact = aware(r["run_created_at"]), aware(r["artifact_created_at"])
        if r["model_version"] != model_version:
            why = "model_version_mismatch"
        elif r["scoring_fingerprint"] != scoring_fingerprint:
            why = "scoring_fingerprint_mismatch"
        elif created is None or ko is None:
            why = "run_time_or_kickoff_unknown"
        elif created >= ko:
            why = "run_created_after_kickoff"
        elif artifact is None or r["week"] not in first or artifact >= first[r["week"]]:
            why = "artifact_not_before_first_kickoff"
        if why:
            excl[(r["run_id"], r["week"], why)] += 1
            continue
        rows.append({"season": season, "week": r["week"], "espn_player_id": r["espn_player_id"],
                     "gsis_id": r["gsis_id"], "position": r["position"], "run_id": r["run_id"],
                     "run_created_at": created, "model_version": r["model_version"], "artifact_id": None,
                     "artifact_created_at": artifact, "scoring_fingerprint": r["scoring_fingerprint"],
                     "nfl_team": r["nfl_team"], "kickoff": ko, "pregame": True,
                     "coverage_status": r["coverage_status"], "projected_points": r["projected_points"],
                     "p_play": r["p_play"], "expected_points": r["expected_points"]})
    return rows, [{"run_id": k[0], "week": k[1], "reason": k[2], "rows": n} for k, n in sorted(excl.items(), key=str)]


def production_lookup(rows: Iterable[Mapping]) -> dict:
    """(week, espn_player_id) -> unconditional expected points of the champion production run, with the
    run's creation time as the capture time. Rows without expected points abstain (no value)."""
    return {(r["week"], r["espn_player_id"]): {"value": r.get("expected_points"), "captured_at": r["run_created_at"],
                                               "point_in_time": False} for r in rows}


# ----------------------------------------------------------------------------- decision sets (prospective)


def refuse_post_kickoff(cands: Iterable[Mapping], now: datetime) -> tuple[list[dict], list[dict]]:
    """(kept, refused). A player whose own game has started (kickoff <= now) or whose kickoff is unknown is
    refused: a set recorded now cannot be a pregame record for him."""
    t, kept, refused = aware(now), [], []
    for c in cands:
        ko = aware(c.get("kickoff"))
        if ko is None:
            refused.append({"espn_player_id": c.get("espn_player_id"), "reason": "unknown_kickoff"})
        elif ko <= t:
            refused.append({"espn_player_id": c.get("espn_player_id"), "reason": "kicked_off"})
        else:
            kept.append(dict(c))
    return kept, refused


def _jsonable(x: Any) -> Any:
    if isinstance(x, datetime):
        return aware(x).isoformat()
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    return x


def _canonical(obj: Any) -> str:
    return json.dumps(_jsonable(obj), sort_keys=True, separators=(",", ":"), default=str)


def decision_set(season: int, week: int, cands: Sequence[Mapping], refused: Sequence[Mapping],
                 settings: Mapping, recorded_at: datetime, *, models: Sequence[str], unavailable: Sequence[str] = (),
                 meta: Mapping | None = None) -> dict:
    """A frozen pregame decision set: roster candidates with each model's pregame prediction and its
    capture time. No outcomes. `sha` covers the content and not `recorded_at`, so an identical re-record
    is the same set."""
    keep = ("league_id", "fantasy_team_id", "season", "week", "espn_player_id", "gsis_id", "position", "team",
            "kickoff", "slot", "is_starter", "roster_synced_at", "preds")
    rows = sorted(({k: c.get(k) for k in keep} for c in cands),
                  key=lambda r: (str(r["league_id"]), str(r["fantasy_team_id"]), str(r["espn_player_id"])))
    content = {"schema": DECISION_SET_SCHEMA, "season": season, "week": week, "models": sorted(models),
               "unavailable_models": sorted(unavailable),
               "slot_settings": {str(k): dict(v) for k, v in sorted(settings.items(), key=lambda kv: str(kv[0]))},
               "candidates": _jsonable(rows), "refused": sorted(({k: v for k, v in r.items()} for r in refused),
                                                                 key=lambda r: (str(r.get("espn_player_id")), r["reason"])),
               "meta": dict(meta or {})}
    sha = hashlib.sha256(_canonical(content).encode()).hexdigest()
    return {**content, "sha": sha, "recorded_at": aware(recorded_at).isoformat()}


def verify_decision_set(ds: Mapping) -> bool:
    content = {k: v for k, v in ds.items() if k not in ("sha", "recorded_at")}
    return hashlib.sha256(_canonical(content).encode()).hexdigest() == ds.get("sha")


def write_decision_set(out_dir: Path, ds: Mapping) -> tuple[Path, bool]:
    """Write-once, content-addressed file `s<season>w<week>-<sha16>.json`. An existing file is never
    changed (an identical re-record returns it with created=False)."""
    path = Path(out_dir) / "decision_sets" / f"s{ds['season']}w{int(ds['week']):02d}-{ds['sha'][:16]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8") as f:
            json.dump(ds, f, sort_keys=True, indent=1, default=str)
        return path, True
    except FileExistsError:
        return path, False


def load_decision_sets(out_dir: Path) -> list[dict]:
    """All verified decision sets under out_dir (a file whose content no longer matches its sha is
    skipped and reported by `unverified_decision_sets`)."""
    sets = []
    for f in sorted((Path(out_dir) / "decision_sets").glob("s*.json")):
        try:
            ds = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if ds.get("schema") == DECISION_SET_SCHEMA and verify_decision_set(ds):
            sets.append(ds)
    return sets


def candidates_from_sets(sets: Iterable[Mapping]) -> tuple[list[dict], dict]:
    """Candidates of recorded sets for scoring. Per (league, fantasy team, season, week, player) the set
    recorded latest but still strictly before the player's kickoff wins; later sets cannot rewrite a past
    one. Datetimes are restored; outcomes are attached afterwards."""
    best: dict[tuple, tuple] = {}
    settings: dict = {}
    for ds in sets:
        rec = aware(ds.get("recorded_at"))
        settings.update({int(k) if str(k).lstrip("-").isdigit() else k: v
                         for k, v in (ds.get("slot_settings") or {}).items()})
        for c in ds["candidates"]:
            ko = aware(c.get("kickoff"))
            if rec is None or ko is None or rec >= ko:
                continue
            k = (c["league_id"], c["fantasy_team_id"], c["season"], c["week"], c["espn_player_id"])
            if k not in best or rec > best[k][0]:
                best[k] = (rec, c)
    out = []
    for _, c in best.values():
        c = dict(c)
        c["kickoff"], c["roster_synced_at"] = aware(c.get("kickoff")), aware(c.get("roster_synced_at"))
        c["preds"] = {m: {**p, "captured_at": aware(p.get("captured_at"))} for m, p in (c.get("preds") or {}).items()}
        out.append(c)
    return out, settings
