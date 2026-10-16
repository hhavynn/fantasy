"""Real fantasy-roster Start/Sit decision evaluation (research; read-only for the databases).

    cd backend
    uv run python ../scripts/start_sit_evaluation.py report --season 2026 [--weeks 1-9] [--source both] [--json]
    uv run python ../scripts/start_sit_evaluation.py record --season 2026 --week 7 [--out DIR]

`report` scores decisions on the rosters in lineup_snapshots (roster at snapshot; labeled pregame only where
every row was synced before its player's kickoff) and on recorded decision sets, for ESPN, the A1/A2
baselines, the frozen research ledger and the production forecasts (ifp-2.0.0 production and the ifp-1.0.0
fallback, one model set per version), each on an equal population.
Models with no genuine pregame evidence are listed as unavailable, never backfilled.

`record` freezes this week's pregame decision set (rosters + every model's pregame predictions, no
outcomes) as a write-once content-addressed JSON under OUT/decision_sets/, so evidence accumulates week by
week. Players whose game has already kicked off are refused. Default OUT: data/models/start_sit
(git-ignored). Neither command writes to a database.

The decision layer is app/analytics/start_sit_evaluation.py; docs/start_sit_evaluation.md explains it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _bootstrap  # noqa: F401
import duckdb
import polars as pl

from app.analytics import start_sit_evaluation as sse
from app.analytics import weekly_learning as wl
from app.analytics.forecast_features import POSITIONS, Sources, features_for_week, load_sources
from app.analytics.independent_projection_store import league_scoring
from app.analytics.independent_projections import MODEL_VERSION, baseline_predictions
from app.analytics.league_scoring import resolve_skill_scoring
from app.analytics.projection_source import MODEL_VERSION as PRODUCTION_VERSION
from app.config import REPO_ROOT, get_settings

MODELS = ("espn", "A1", "A2", "ifp1_production", "ifp2_production", "ifp1_frozen", "ifp2_frozen")
# Production forecasts per model version, each read with production_forecasts' pregame rules (never pooled):
# ifp1_production = the retired previous production model (the fallback), ifp2_production = the production model.
PRODUCTION_MODELS = {"ifp1_production": MODEL_VERSION, "ifp2_production": PRODUCTION_VERSION}


def _path(env: str, default: str) -> Path:
    raw = Path(os.getenv(env) or default)
    return raw if raw.is_absolute() else REPO_ROOT / raw


def _ro(path: Path):
    return duckdb.connect(str(path), read_only=True) if path is not None and Path(path).exists() else None


def _wall_clock() -> datetime:
    """The real current time (tests replace it). `record` stamps this, never --now."""
    return datetime.now(timezone.utc)


def _now(args) -> datetime:
    if not args.now:
        return _wall_clock()
    t = datetime.fromisoformat(args.now)
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


# ----------------------------------------------------------------------------- candidate building


def load_context(live, hist, season: int):
    _, scoring = league_scoring(live, season)
    scoring.require_supported()
    parts = [load_sources(c, scoring) for c in (hist, live) if c is not None]
    return Sources.concat(parts), scoring


def comparable_leagues(live, season: int, scoring) -> tuple[dict, dict]:
    """(slot settings of leagues scored like `scoring`, {league_id: reason} for the rest)."""
    keep, skipped = {}, {}
    for lid, s in sse.league_settings(live, season).items():
        if resolve_skill_scoring(s["scoring_items"]).fingerprint != scoring.fingerprint:
            skipped[lid] = "scoring_fingerprint_differs"
        elif not s["roster_slots"]:
            skipped[lid] = "roster_slots_unknown"
        else:
            keep[lid] = s["roster_slots"]
    return keep, skipped


def attach_outcomes(cands: list[dict], src: Sources, fetched_at: dict | None) -> None:
    """Outcome status + league points through weekly_learning.label_outcomes (contract section 4). A
    candidate without a trusted GSIS id is UNRESOLVED_IDENTITY; nothing is scored from the future."""
    rows = {}
    for c in cands:
        if c.get("gsis_id") and c.get("team"):
            rows[(c["season"], c["week"], c["gsis_id"])] = {
                "season": c["season"], "week": c["week"], "gsis_id": c["gsis_id"], "team": c["team"],
                "kickoff": None if c.get("kickoff") is None else sse.aware(c["kickoff"]).isoformat()}
    labeled = {}
    if rows:
        schema = {"season": pl.Int64, "week": pl.Int64, "gsis_id": pl.String, "team": pl.String, "kickoff": pl.String}
        out = wl.label_outcomes(pl.DataFrame(list(rows.values()), schema=schema), src, fetched_at)
        labeled = {(r["season"], r["week"], r["gsis_id"]): (r["outcome_status"], r["label"])
                   for r in out.iter_rows(named=True)}
    for c in cands:
        st, pts = labeled.get((c["season"], c["week"], c.get("gsis_id")), ("unresolved_identity", None))
        c["outcome_status"], c["actual_points"] = st, pts


def baseline_lookup(src: Sources, cands: list[dict]) -> dict:
    """A1 (recent average) and A2 (season average) per (season, week, gsis_id), built from games strictly
    before the week (forecast_features.features_for_week) so they are pregame by construction."""
    out = {"A1": {}, "A2": {}}
    wanted = {(c["season"], c["week"]): set() for c in cands}
    for c in cands:
        if c.get("gsis_id") and c.get("position") in POSITIONS and c.get("team"):
            wanted[(c["season"], c["week"])].add((c["gsis_id"], c["position"], c["team"]))
    for (season, week), players in sorted(wanted.items()):
        games = {r["team"]: r for r in src.games.filter((pl.col("season") == season) & (pl.col("week") == week))
                 .iter_rows(named=True)}
        targets = [{"gsis_id": g, "position": p, "team": t, "opponent": games[t]["opponent"],
                    "is_home": bool(games[t]["is_home"])} for g, p, t in sorted(players) if t in games]
        if not targets:
            continue
        feats = baseline_predictions(features_for_week(src, season, week, pl.DataFrame(targets).unique("gsis_id")))
        for r in feats.iter_rows(named=True):
            for name, col in (("A1", "pred_A1"), ("A2", "pred_A2")):
                if r.get(col) is not None:
                    out[name][(season, week, r["gsis_id"])] = {"value": r[col], "captured_at": None,
                                                                "point_in_time": True}
    return out


def ledger_lookup(ledger_dir: Path | None) -> dict:
    """Frozen research-ledger expected points (ifp-1 refit as champion, ifp-2 challenger), only rows
    weekly_learning.frozen_rows marks pregame; captured_at is the run's creation time."""
    out = {"ifp1_frozen": {}, "ifp2_frozen": {}}
    if ledger_dir is None or not Path(ledger_dir).exists():
        return out
    runs = wl.load_ledger(Path(ledger_dir))
    created = {r["run_id"]: datetime.fromisoformat(r["created_at"]) for r in runs}
    for r in wl.frozen_rows(runs, "pregame").iter_rows(named=True):
        cap = created[r["run_id"]]
        k = (r["season"], r["week"], r["gsis_id"])
        out["ifp1_frozen"][k] = {"value": r["exp_champion"], "captured_at": cap, "point_in_time": False}
        out["ifp2_frozen"][k] = {"value": r["exp_challenger"], "captured_at": cap, "point_in_time": False}
    return out


def attach_predictions(cands: list[dict], live, src: Sources, scoring, season: int, *, ledger_dir: Path | None,
                       forecast_reader=None, as_of: datetime | None = None) -> dict:
    """Set preds[...] for every model on the candidates (a model with nothing stays absent). Returns counts."""
    gkey = lambda c: (c["season"], c["week"], c.get("gsis_id"))  # noqa: E731
    counts = {}
    espn = sse.espn_pregame_predictions(live, season, cands, as_of=as_of)
    counts["espn"] = sse.attach_model(cands, "espn", espn, lambda c: (c["week"], c["espn_player_id"]))
    for name, lookup in baseline_lookup(src, cands).items():
        counts[name] = sse.attach_model(cands, name, lookup, gkey)
    for name, lookup in ledger_lookup(ledger_dir).items():
        counts[name] = sse.attach_model(cands, name, lookup, gkey)
    reader, source = (forecast_reader, "injected reader") if forecast_reader is not None else sse.forecast_reader_for(live)
    counts["production_source"] = source
    for name, version in PRODUCTION_MODELS.items():
        rows, excluded = ([], []) if reader is None else reader(
            live, season, sorted({c["week"] for c in cands}) or None, model_version=version,
            scoring_fingerprint=scoring.fingerprint)
        # rows without expected points abstain (the consumer shows ESPN there): no value, never credited to the model
        counts[name] = sse.attach_model(cands, name, sse.production_lookup(rows),
                                        lambda c: (c["week"], c["espn_player_id"]))
        if name == "ifp1_production":
            counts["production_reader_exclusions"] = len(excluded)
            counts["production_exclusions"] = excluded            # {run_id, week, reason}, passed through unchanged
        else:
            counts[f"{name}_reader_exclusions"] = len(excluded)
            counts[f"{name}_exclusions"] = excluded
    return counts


def build_report_candidates(live, hist, src, scoring, season: int, weeks, ledger_dir, fetched_at,
                            forecast_reader=None) -> tuple[list[dict], dict, dict]:
    settings, skipped = comparable_leagues(live, season, scoring)
    cands = [c for c in sse.roster_candidates(live, season, weeks) if c["league_id"] in settings]
    attach_outcomes(cands, src, fetched_at)
    counts = attach_predictions(cands, live, src, scoring, season, ledger_dir=ledger_dir, forecast_reader=forecast_reader)
    return cands, settings, {"skipped_leagues": skipped, "attached": counts}


# ----------------------------------------------------------------------------- commands


def _weeks(arg: str | None) -> list[int] | None:
    if not arg:
        return None
    if "-" in arg:
        a, b = arg.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in arg.split(",")]


def _config(args) -> sse.EvalConfig:
    return sse.EvalConfig(min_decisions=args.min_decisions, min_weeks=args.min_weeks, resamples=args.resamples,
                          roster_policy=args.roster_policy)


def cmd_report(args) -> dict:
    live = _ro(args.live_db)
    if live is None:
        raise SystemExit(f"live database {args.live_db} not found")
    hist = _ro(args.history_db)
    src, scoring = load_context(live, hist, args.season)
    from app.analytics.schedule_intelligence import nfl_fetch_times
    fetched = {args.season: nfl_fetch_times(live, args.season)["schedules"]}
    cfg = _config(args)
    out = {"season": args.season, "note": sse.KNOWN_PAIR_NOTE}
    if args.source in ("snapshot", "both"):
        cands, settings, info = build_report_candidates(live, hist, src, scoring, args.season, _weeks(args.weeks),
                                                        args.ledger_dir, fetched)
        out["snapshot"] = {"basis": "lineup_snapshots roster at snapshot; predictions pregame only", "candidates": len(cands),
                           **info, **sse.evaluate_sets(cands, settings, config=cfg)}
    if args.source in ("recorded", "both"):
        sets = sse.load_decision_sets(args.out)
        cands, settings = sse.candidates_from_sets(sets)
        attach_outcomes(cands, src, fetched)
        out["recorded"] = {"basis": "decision sets frozen before kickoff", "sets": len(sets),
                           "audit_only_sets_ignored": sum(1 for d in sets if d.get("audit_only")),
                           "candidates": len(cands),
                           **sse.evaluate_sets(cands, settings, config=cfg)}
    return out


def _render_set(lines: list, sname: str, s: dict, label: str) -> None:
    lines.append(f"  [{sname}] {label}: same-position pairs {s['same_position']['decisions']} "
                 f"(weeks {s['same_position']['weeks']}), flex pairs {s['flex']['decisions']}, "
                 f"close calls {s['close_calls']['decisions']}, lineups {s['lineups']['decisions']}")
    for m in s["models"]:
        st, lr = s["same_position"]["stats"][m], s["lineups"]["regret"][m]
        lines.append(f"      {m:16} accuracy {st['accuracy']}  regret/pair {st['mean_regret']}  "
                     f"lineup regret mean {lr['mean']} p90 {lr['p90']} max {lr['max']}")
        for ref, c in (s["same_position"]["accuracy_difference"].get(m) or {}).items():
            lines.append(f"        accuracy {ref}: {c['difference']} CI {c['ci95']}  {c['verdict']}")
        for ref, c in (s["lineups"]["regret_difference"].get(m) or {}).items():
            lines.append(f"        lineup regret {ref}: {c['difference']} CI {c['ci95']}  {c['verdict']}")
    lines.append(f"      exclusions: pairs {s['exclusions']['pairs']}  lineups {s['exclusions']['lineups']}")


def render(rep: dict) -> str:
    lines = [rep["note"], ""]
    for name in ("snapshot", "recorded"):
        sec = rep.get(name)
        if not sec:
            continue
        lines.append(f"== {name}: {sec['basis']} ({sec['candidates']} roster players)")
        att = sec.get("attached") or {}
        if att:
            lines.append(f"  production ifp-1 source: {att['production_source']}  "
                         f"({att['production_reader_exclusions']} run exclusions: "
                         f"{sorted({e['reason'].split(':')[0] for e in att['production_exclusions']})})")
        for m, a in sec["availability"].items():
            lines.append(f"  {m:16} pregame predictions {a['with_pregame_prediction']}/{a['candidates']}"
                         + ("  UNAVAILABLE" if a["unavailable"] else ""))
        for sname, s in sec["sets"].items():
            if not s["evaluated"]:
                lines.append(f"  [{sname}] not evaluated: unavailable {', '.join(s['unavailable_models'])}")
                continue
            _render_set(lines, sname, s, "POOLED roster snapshots (pregame + post-hoc)")
            _render_set(lines, sname, s["pregame_roster_only"], "PREGAME-ROSTER-ONLY")
    return "\n".join(lines)


def cmd_record(args) -> tuple[dict, int]:
    live = _ro(args.live_db)
    if live is None:
        raise SystemExit(f"live database {args.live_db} not found")
    hist = _ro(args.history_db)
    wall = _wall_clock()
    now = _now(args)
    overridden = now < wall - timedelta(seconds=60)
    if now > wall + timedelta(seconds=60):
        raise SystemExit("--now is in the future of the real clock; refusing")
    cutoff = min(now, wall)                 # nothing synced or captured after this may enter the set
    src, scoring = load_context(live, hist, args.season)
    settings, skipped = comparable_leagues(live, args.season, scoring)
    cands = [c for c in sse.roster_candidates(live, args.season, [args.week]) if c["league_id"] in settings]
    kept, refused = sse.refuse_post_kickoff(cands, cutoff)
    kept, late = sse.drop_future_evidence(kept, cutoff)
    refused += late
    if not kept:
        return {"recorded": False, "reason": "no roster row is both pregame (game not started) and synced before now",
                "refused": len(refused), "skipped_leagues": skipped}, 2
    counts = attach_predictions(kept, live, src, scoring, args.season, ledger_dir=args.ledger_dir, as_of=cutoff)
    kept, _ = sse.drop_future_evidence(kept, cutoff)       # predictions captured after the cutoff are removed
    avail = sse.availability(kept, MODELS)
    meta = {"scoring_fingerprint": scoring.fingerprint, "model_version": MODEL_VERSION,
            "production_models": PRODUCTION_MODELS, "attached": counts}
    if overridden:
        meta["clock_override"] = {"requested_now": now.isoformat(), "wall_clock": wall.isoformat()}
    ds = sse.decision_set(args.season, args.week, kept, refused, settings, wall, models=MODELS,
                          unavailable=[m for m, a in avail.items() if a["unavailable"]], meta=meta,
                          audit_only=overridden)
    path, created = sse.write_decision_set(args.out, ds)
    return {"recorded": True, "created": created, "path": str(path), "sha": ds["sha"], "players": len(kept),
            "refused": len(refused), "unavailable_models": ds["unavailable_models"], "audit_only": overridden,
            "skipped_leagues": skipped}, 0


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("report", "record"):
        p = sub.add_parser(name)
        p.add_argument("--season", type=int, default=settings.nfl_season)
        p.add_argument("--live-db", "--db", dest="live_db", type=Path, default=settings.db_path)
        p.add_argument("--history-db", type=Path, default=_path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
        p.add_argument("--out", type=Path, default=_path("START_SIT_EVAL_DIR", "data/models/start_sit"),
                       help="decision sets live in OUT/decision_sets")
        p.add_argument("--ledger-dir", type=Path, default=_path("WEEKLY_MODEL_DIR", "data/models/weekly"),
                       help="weekly-learning ledger (frozen ifp-1/ifp-2 pregame rows); missing = those models unavailable")
        p.add_argument("--now", help="ISO time to treat as now (testing and audits)")
    r = sub.choices["report"]
    r.add_argument("--weeks", help="e.g. 1-9 or 3,5")
    r.add_argument("--source", choices=["snapshot", "recorded", "both"], default="both")
    r.add_argument("--json", action="store_true")
    r.add_argument("--min-decisions", type=int, default=sse.EvalConfig.min_decisions)
    r.add_argument("--min-weeks", type=int, default=sse.EvalConfig.min_weeks)
    r.add_argument("--resamples", type=int, default=sse.EvalConfig.resamples)
    r.add_argument("--roster-policy", choices=["complete", "valid_subset"], default="complete")
    sub.choices["record"].add_argument("--week", type=int, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "report":
        rep = cmd_report(args)
        print(json.dumps(rep, indent=1, default=str) if args.json else render(rep))
        return 0
    res, code = cmd_record(args)
    print(json.dumps(res, indent=1, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
