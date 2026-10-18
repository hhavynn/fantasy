"""Build and publish the projection-performance report (offline; see docs/projection_performance.md).

    cd backend
    uv run python ../scripts/projection_performance.py show    --season 2026 [--json]
    uv run python ../scripts/projection_performance.py publish --season 2026

The report is the scorecard behind the Projection performance page: per position, the production model
(projection_source.MODEL_VERSION) against ESPN's pregame projection on fully final NFL weeks, with the evidence gate,
per-week errors, lead times, coverage and exclusions, the production-vs-fallback model-version comparison and the
Start/Sit decision evaluation of the recorded pregame decision sets. Every statistic comes from the research code
that already owns it (app.analytics.espn_benchmark for accuracy, app.analytics.start_sit_evaluation for decisions);
this script only selects, checks and packages their aggregates. It never trains, syncs, promotes or changes a
projection.

`show` opens the database read-only and prints the report. `publish` builds the same report from a read-only
connection, then inserts it as ONE write-once row of projection_performance_reports (app.analytics.performance_report:
canonical JSON + sha256, never updated or deleted). scripts/publish_motherduck.py copies the
table to the hosted database with every other table in one transaction, and the hosted API serves the newest row that
verifies. scripts/refresh_all.py runs `publish` after the analytics step and before the MotherDuck publish.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import _bootstrap  # noqa: F401
import duckdb

from app.analytics import espn_benchmark as eb
from app.analytics import performance_report as pr
from app.analytics import production_forecasts as pf
from app.analytics import projection_source as ps
from app.analytics import start_sit_evaluation as sse
from app.analytics.forecast_backtest import PROMOTION_CRITERIA
from app.analytics.forecast_features import POSITIONS, load_sources
from app.analytics.independent_projection_store import league_scoring
from app.config import REPO_ROOT, get_settings
from app.db.connection import table_exists

import start_sit_evaluation as ssx       # sibling script: outcome labels for recorded decision sets

PRODUCTION, FALLBACK = ps.MODEL_VERSION, ps.FALLBACK_VERSION
# Start/Sit model sets judged on the page: one per production model version, never pooled, each with ESPN and A1.
DECISION_SETS = {"ifp2_production": PRODUCTION, "ifp1_production": FALLBACK}
SYNTHETIC_LABEL = "SYNTHETIC TEST FIXTURE: invented data for tests and screenshots, not a real result"

METHODOLOGY = [
    "Only forecasts made before each player's own kickoff count, from the immutable run store; a forecast made or "
    "rerun after kickoff is never scored, and nothing is reconstructed after the fact.",
    "Only fully final NFL weeks are evidence. Player-weeks of weeks still in progress are listed as preliminary, "
    "without a verdict.",
    "Only each week's own pregame forecast (horizon 0) is scored. Rest-of-season forecasts made weeks earlier "
    "(horizon 1 or more) are never mixed in, even when the site shows one for a week without a newer run.",
    "Unconditional comparison (the headline): every resolved player-week the model projected; a player who did not "
    "play scores 0, as in a real lineup. The conditional comparison (players who appeared) is reported separately.",
    "Paired: ESPN and the model are scored on exactly the same player-weeks. Players the model could not project "
    "show ESPN's number on the site; those fallback rows are counted but never credited to the model.",
    "MAE difference = MAE(ours) - MAE(ESPN): negative means our projections were closer. The interval resamples whole "
    "NFL weeks, because player-weeks of one week are correlated; an interval that includes 0 means no established "
    "difference.",
    "Mean signed error = mean(actual - projection): positive means the projection was too low on average.",
]


def _round(x, nd=4):
    return None if x is None else round(float(x), nd)


def _position_cells(card: dict, pos: str) -> dict:
    """Rows, scored rows and exclusions by reason for one position, summed over the scorecard's weeks."""
    rows = scored = 0
    reasons: dict[str, int] = {}
    for cell in (card.get("coverage") or {}).get("by_week_position", []):
        if cell["position"] != pos:
            continue
        rows += cell["rows"]
        scored += cell["scored"]
        for k, v in cell["exclusions"].items():
            reasons[k] = reasons.get(k, 0) + v
    order = [r for r in eb.EXCLUSIONS if r in reasons] + sorted(r for r in reasons if r not in eb.EXCLUSIONS)
    return {"rows": rows, "scored": scored, "excluded": rows - scored, "by_reason": {r: reasons[r] for r in order}}


def _empty_group() -> dict:
    blank = {"n": 0, "mae": None, "rmse": None, "bias": None}
    return {"n": 0, "n_model": 0, "weeks_covered": 0, "ours": blank, "espn": blank, "mae_diff": None, "ci95": None,
            "verdict": "insufficient_evidence", "verdict_reason": "no scorecard"}


def position_summary(card: dict, pos: str, leads: dict, cfg: eb.BenchmarkConfig) -> dict:
    comps = card.get("comparisons") or {}
    group = lambda name: (comps.get(name) or {}).get("by_position", {}).get(pos) or _empty_group()  # noqa: E731
    pure, cons, cond = group("pure_independent"), group("consumer_visible"), group("conditional")
    need = PROMOTION_CRITERIA["espn_min_matched_per_position"]
    return {
        "model_version": card.get("model_version", PRODUCTION),
        "verdict": pure["verdict"], "verdict_reason": pure["verdict_reason"],
        "statement": pr.STATEMENTS[pure["verdict"]],
        "fully_final_weeks": pure["weeks_covered"], "required_weeks": cfg.min_weeks, "required_n": need,
        "paired_n": pure["n"], "n_model": pure["n_model"],
        "ours": pure["ours"], "espn": pure["espn"], "mae_diff": pure["mae_diff"], "ci95": pure["ci95"],
        "coverage": {"eligible_resolved": cons["n"], "model_covered": cons["n_model"],
                     "fallback_n": cons.get("fallback_n", 0), "fallback_share": cons.get("fallback_share"),
                     "model_share": _round(cons["n_model"] / cons["n"]) if cons["n"] else None},
        "lead_time": leads.get(pos) or {},
        "conditional": {"n": cond["n"], "weeks_covered": cond["weeks_covered"], "ours": cond["ours"],
                        "espn": cond["espn"], "mae_diff": cond["mae_diff"], "ci95": cond["ci95"],
                        "verdict": cond["verdict"], "verdict_reason": cond["verdict_reason"],
                        "statement": pr.STATEMENTS[cond["verdict"]]},
        "observations": _position_cells(card, pos),
    }


def _version_weeks(rows: list[dict], cfg: eb.BenchmarkConfig) -> list[dict]:
    """Per week: player-weeks where this version's verified pregame forecast is paired with a pregame ESPN projection
    (whatever the outcome), and how many of them were scored (resolved, fully final week)."""
    pure, _ = eb.population(rows, "pure_independent", cfg)
    out = []
    for week in sorted({r["week"] for r in rows}):
        fc = sum(1 for r in rows if r["week"] == week and r["run_id"] is not None and r["ours_origin"] != eb.ESPN)
        sc = sum(1 for r in pure if r["week"] == week)
        if fc or sc:
            out.append({"week": week, "paired_pregame_forecasts": fc, "scored": sc})
    return out


def served_by_week(con, season: int, weeks: list[int]) -> list[dict]:
    """What the site shows NOW for each week (projection_source.select_run, the consumer rule). A week shown with
    the fallback or with ESPN is labeled; accuracy evidence still counts only pregame rows (the comparison above)."""
    out = []
    for w in weeks:
        try:
            sel = ps.select_run(con, season, w)
        except Exception as err:          # a database without the consumer tables: say so, never guess
            out.append({"week": w, "served_version": None, "fallback": False, "note": f"not available ({err!r})"})
            continue
        run = sel["run"]
        out.append({"week": w, "served_version": run["model_version"] if run else None, "fallback": sel["fallback"],
                    # the consumer may serve a rest-of-season run (horizon k > 0); it is never accuracy evidence
                    "forecast_horizon": pf.horizon_of(run) if run else None,
                    "horizon_note": pf.horizon_note(run) if run else None,
                    "note": None if run else "ESPN shown: no eligible independent run"})
    return out


def _decision_section(ev: dict, model: str, section: str) -> dict:
    s = ev[section]
    diff = (s["accuracy_difference"].get(model) or {}).get("vs_espn") or {}
    verdict = diff.get("verdict", "insufficient_evidence")
    return {"decisions": s["decisions"], "weeks": s["weeks"], "unique_player_pairs": s["unique_player_pairs"],
            "ours_accuracy": s["stats"][model]["accuracy"], "espn_accuracy": s["stats"]["espn"]["accuracy"],
            "difference": diff.get("difference"), "ci95": diff.get("ci95"), "verdict": verdict,
            "statement": pr.DECISION_STATEMENTS[verdict]}


def start_sit_summary(sets: list[dict], season: int, cands: list[dict], settings: dict,
                      cfg: sse.EvalConfig = sse.EvalConfig()) -> dict:
    """Recorded pregame decision sets only (rosters and predictions frozen before kickoff), and within them only the
    team-weeks whose every roster row was synced before its player's kickoff. A decision counts only when ESPN, A1
    and the model all have a pregame prediction for every player in it: a player the model did not project (the site
    shows ESPN there) removes the decision, so a fallback choice is never credited to the model."""
    usable = [d for d in sets if d.get("season") == season and not d.get("audit_only")]
    weeks = sorted({d["week"] for d in usable})
    out = {"source": "recorded pregame decision sets", "sets": len(usable),
           "audit_only_ignored": sum(1 for d in sets if d.get("season") == season and d.get("audit_only")),
           "weeks_recorded": weeks, "gate": {"min_decisions": cfg.min_decisions, "min_weeks": cfg.min_weeks},
           "population": "team-weeks whose every roster row was synced before its player's kickoff",
           "models": {}, "warnings": []}
    if not usable:
        out["warnings"].append("No pregame decision set has been recorded yet, so there is no Start/Sit evidence.")
        return out
    res = sse.evaluate_sets(cands, settings, {k: ["espn", "A1", k] for k in DECISION_SETS}, cfg)
    for model, version in DECISION_SETS.items():
        s, avail = res["sets"][model], res["availability"][model]
        entry = {"model_version": version, "evaluated": s["evaluated"],
                 "with_pregame_prediction": avail["with_pregame_prediction"], "candidates": avail["candidates"]}
        if not s["evaluated"]:
            entry["unavailable_reason"] = (f"no pregame prediction recorded for "
                                           f"{', '.join(s['unavailable_models'])}")
            out["models"][model] = entry
            continue
        ev = s["pregame_roster_only"]
        lu = ev["lineups"]
        rd = (lu["regret_difference"].get(model) or {}).get("vs_espn") or {}
        lineup_verdict = rd.get("verdict", "insufficient_evidence")
        entry.update({
            "same_position": _decision_section(ev, model, "same_position"),
            "flex": _decision_section(ev, model, "flex"),
            "close_calls": _decision_section(ev, model, "close_calls"),
            "lineups": {"decisions": lu["decisions"], "weeks": lu["weeks"],
                        "ours_mean_regret": lu["regret"][model]["mean"], "espn_mean_regret": lu["regret"]["espn"]["mean"],
                        "regret_difference": rd.get("difference"), "ci95": rd.get("ci95"), "verdict": lineup_verdict,
                        "statement": pr.DECISION_STATEMENTS[lineup_verdict]},
        })
        n, w = entry["same_position"]["decisions"], entry["same_position"]["weeks"]
        if n < cfg.min_decisions or w < cfg.min_weeks:
            out["warnings"].append(f"{version}: {n} same-position decisions over {w} weeks; a verdict needs at least "
                                   f"{cfg.min_decisions} decisions and {cfg.min_weeks} weeks.")
        out["models"][model] = entry
    return out


def _decision_inputs(decision_dir: Path | None, season: int, sources, fetched) -> tuple[list[dict], list[dict], dict]:
    sets = sse.load_decision_sets(decision_dir) if decision_dir is not None and Path(decision_dir).exists() else []
    season_sets = [d for d in sets if d.get("season") == season]
    cands, settings = sse.candidates_from_sets(season_sets)
    ssx.attach_outcomes(cands, sources, fetched if isinstance(fetched, dict) or fetched is None else {season: fetched})
    return sets, cands, settings


def week_scope(con, season: int, now: datetime) -> tuple[list[int] | None, list[int]]:
    """(weeks to score, the next week). Scored: weeks with stored ESPN projections whose first kickoff has passed;
    ESPN stores projections for every future week, whose rows could only be excluded (no roster team, no game).
    The next week (its pregame capture is under way) appears only in the model-version table, never in the
    scorecard. Without a schedule every week with ESPN history is scored (None)."""
    firsts = pf.first_kickoffs(con, season)
    if not firsts:
        return None, []
    history = {r[0] for r in con.execute("SELECT DISTINCT scoring_period FROM espn_projection_history WHERE season = ?",
                                         [season]).fetchall()} if table_exists(con, "espn_projection_history") else set()
    started = sorted(w for w, k in firsts.items() if k <= now and w in history)
    ahead = [w for w, k in firsts.items() if k > now and w in history]
    return started, [min(ahead)] if ahead else []


def build_report(con, season: int, *, now: datetime, readers: dict | None = None,
                 config: eb.BenchmarkConfig | None = None, scoring=None, sources=None, fetched_at=None,
                 decision_dir: Path | None = None, synthetic: bool = False,
                 decision_config: sse.EvalConfig | None = None) -> dict:
    """The report (validated, aggregates only). `readers` {model_version: forecast reader} and the inputs after it
    are injectable for tests; by default every reader is production_forecasts.pregame_forecasts."""
    cfg = config or eb.BenchmarkConfig()
    readers = readers or {}
    if scoring is None:
        _, scoring = league_scoring(con, season)
    if sources is None and scoring.supported:
        sources = load_sources(con, scoring, [season])
    if fetched_at is None and scoring.supported:
        from app.analytics.schedule_intelligence import nfl_fetch_times
        fetched_at = nfl_fetch_times(con, season)["schedules"]
    shared = {"scoring": scoring, "sources": sources, "fetched_at": fetched_at}
    scope, upcoming = week_scope(con, season, now)

    card, rows = eb.scorecard_and_rows(con, season, scope, forecast_reader=readers.get(PRODUCTION), config=cfg,
                                       model_version=PRODUCTION, **shared)
    next_rows, fb_rows = [], []
    if card["available"]:
        version_weeks = card["weeks"] + upcoming
        fb_rows, _ = eb.evaluation_rows(con, season, version_weeks, forecast_reader=readers.get(FALLBACK),
                                        model_version=FALLBACK, **shared)
        if upcoming:
            next_rows, _ = eb.evaluation_rows(con, season, upcoming, forecast_reader=readers.get(PRODUCTION),
                                              model_version=PRODUCTION, **shared)
    leads = eb.lead_times(rows, cfg) if rows else {}
    positions = {pos: position_summary(card, pos, leads, cfg) for pos in POSITIONS}
    weekly = [{**w, "model_version": PRODUCTION} for w in eb.weekly_errors(rows, "pure_independent", cfg)]
    for pos in POSITIONS:          # the trend must add up to the scorecard, or nothing is published
        if sum(w["n"] for w in weekly if w["position"] == pos) != positions[pos]["paired_n"]:
            raise RuntimeError(f"{pos}: weekly rows do not add up to the scorecard's paired observations")

    pure = (card.get("comparisons") or {}).get("pure_independent") or {}
    pre = card.get("preliminary") or {}
    pre_pure = ((pre.get("comparisons") or {}).get("pure_independent") or {}).get("by_position") or {}
    sets, cands, settings = (_decision_inputs(decision_dir, season, sources, fetched_at) if scoring.supported
                             else ([], [], {}))
    cov = card.get("coverage") or {}
    payload = {
        "schema_version": pr.SCHEMA_VERSION, "synthetic": bool(synthetic), "season": season,
        "generated_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "data_as_of": pr.latest_sync_times(con),
        "production": {**ps.governance(PRODUCTION), "benchmarked_version": PRODUCTION},
        "evidence_gate": {"min_weeks": cfg.min_weeks,
                          "min_matched_per_position": PROMOTION_CRITERIA["espn_min_matched_per_position"],
                          "interval": "95% bootstrap resampling whole NFL weeks", "resamples": cfg.resamples,
                          "seed": cfg.seed, "horizon_window_h": cfg.horizon_window_h,
                          "start_sit_min_decisions": (decision_config or sse.EvalConfig()).min_decisions,
                          "start_sit_min_weeks": (decision_config or sse.EvalConfig()).min_weeks},
        "weeks": {"with_espn_history": card.get("weeks", []), "fully_final": card.get("fully_final_weeks", []),
                  "partial": card.get("partial_weeks", []), "in_progress": card.get("partial_weeks", []),
                  "upcoming": upcoming},
        "benchmark": {"schema_version": card["schema_version"], "available": card["available"],
                      "unavailable_reason": card["unavailable_reason"], "forecast_source": card.get("forecast_source"),
                      "content_sha256": eb.content_sha(eb.to_json(card))},
        "status": {"verdict": pure.get("verdict", "insufficient_evidence"),
                   "verdict_reason": pure.get("verdict_reason", card.get("unavailable_reason")),
                   "position_verdicts": pure.get("position_verdicts", {p: "insufficient_evidence" for p in POSITIONS})},
        "positions": positions,
        "comparisons": card.get("comparisons") or {},
        "weekly": weekly,
        "preliminary": {"weeks": pre.get("weeks", []), "note": pre.get("note"),
                        "by_position": {p: {"n": g["n"], "ours": g["ours"], "espn": g["espn"]}
                                        for p, g in pre_pure.items()}},
        "model_versions": {
            "production": PRODUCTION, "fallback": FALLBACK,
            "served_now": served_by_week(con, season, card.get("weeks", []) + upcoming),
            "pregame_by_week": {PRODUCTION: _version_weeks(rows + next_rows, cfg), FALLBACK: _version_weeks(fb_rows, cfg)},
            "comparison": eb.version_comparison({PRODUCTION: rows, FALLBACK: fb_rows}, cfg),
        },
        "start_sit": start_sit_summary(sets, season, cands, settings, decision_config or sse.EvalConfig()),
        "coverage": {"rows": cov.get("rows", 0), "scored": cov.get("scored", 0),
                     "by_exclusion_reason": cov.get("by_exclusion_reason", {}),
                     "by_outcome_status": cov.get("by_outcome_status", {}),
                     "scored_by_origin": cov.get("scored_by_origin", {}),
                     "by_week_position": cov.get("by_week_position", []),
                     "forecast_runs_excluded_by_reason": cov.get("forecast_runs_excluded_by_reason", {}),
                     "excluded_runs": card.get("reader_exclusions", [])},
        "notes": METHODOLOGY,
    }
    if synthetic:
        payload["label"] = SYNTHETIC_LABEL
    payload = json.loads(pr.canonical(json.loads(json.dumps(payload, default=str))))   # plain JSON types only
    problem = pr.validate(payload, season=season, allow_synthetic=synthetic)
    if problem:
        raise RuntimeError(f"the built report does not verify: {problem}")
    return payload


def _open_ro(path: Path):
    if not Path(path).exists():
        raise FileNotFoundError(f"database {path} not found")
    return duckdb.connect(str(path), read_only=True)


def _decision_dir() -> Path:
    raw = Path(os.getenv("START_SIT_EVAL_DIR") or "data/models/start_sit")
    return raw if raw.is_absolute() else REPO_ROOT / raw


def build_from_db(db_path: Path, season: int, decision_dir: Path | None) -> dict:
    con = _open_ro(db_path)
    try:
        return build_report(con, season, now=datetime.now(timezone.utc), decision_dir=decision_dir)
    finally:
        con.close()


def summary_line(p: dict) -> str:
    pos = ", ".join(f"{k} {v['verdict']} (n {v['paired_n']}, {v['fully_final_weeks']} wk)" for k, v in p["positions"].items())
    return (f"season {p['season']}: fully final weeks {p['weeks']['fully_final']}, partial {p['weeks']['partial']}; "
            f"{pos}; start/sit sets {p['start_sit']['sets']}")


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("show", "publish"):
        p = sub.add_parser(name)
        p.add_argument("--season", type=int, default=settings.nfl_season)
        p.add_argument("--db", type=Path, default=settings.db_path)
        p.add_argument("--decision-dir", type=Path, default=_decision_dir(),
                       help="start_sit_evaluation output dir (decision sets live in DIR/decision_sets)")
    sub.choices["show"].add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        payload = build_from_db(args.db, args.season, args.decision_dir)
    except (FileNotFoundError, ValueError, RuntimeError) as err:
        print(f"NOT BUILT: {err}", file=sys.stderr)
        return 3
    if args.command == "show":
        print(json.dumps(payload, indent=1, sort_keys=True) if args.json else summary_line(payload))
        return 0
    from app.db.connection import open_db

    with open_db(args.db) as con:          # one INSERT, after the read-only build has closed its connection
        sha, created = pr.store_report(con, payload)
    print(json.dumps({"report_sha256": sha, "created": created, "summary": summary_line(payload)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
