"""Prospective shadow of the pre-registered QB-only scenario candidate `ifp-ctx-p2q` (research only; see
docs/research/qb_scenario_mixture.md section 8 and docs/research/ifp_ctx_shadow_automation.md).

`ifp-ctx-p2q` is ifp-ctx-p2's S2 at QB (the calibrated start-probability mixture of qb_scenarios.py) and ifp-2 at
RB/WR/TE. The historical windows cannot settle it (qb_scenario_mixture.md section 7), so it is judged only on
write-once pregame ledgers frozen from 2026 week 7 on, under p1's prospective gate. The model is qb_scenarios.py,
unchanged and called through qb_scenario_research.py's own build by qb_scenario_forecast.py (so the forecast is exactly
the study's S2); neither p2 file is edited here (both are bound into p2's frozen selection).

    cd backend
    uv run python ../scripts/qb_scenario_shadow.py pregame [--week W]   # gated write-once pregame ledger for week W
    uv run python ../scripts/qb_scenario_shadow.py score                # gate + start-probability calibration
    uv run python ../scripts/qb_scenario_shadow.py cycle   [--week W]   # score, missed-week check, then pregame
    uv run python ../scripts/qb_scenario_shadow.py status
    uv run python ../scripts/qb_scenario_shadow.py predict --week W     # the ledger now (no freeze rule; manual)
    uv run python ../scripts/qb_scenario_shadow.py pregame --week W --now 2026-10-25T12:00Z   # gate only (dry run)

Who runs it (no new scheduler), exactly like ctx_shadow.py:
  * refresh_all.py's read-only evidence stage runs `pregame` for the upcoming week on every refresh;
  * learning_cycle.py runs `cycle` as a non-fatal stage after the production run (Tue-Thu).

Pre-registration. The spec below (candidate, start model constants, gate, rules) is frozen write-once on first use
into frozen_ifp-ctx-p2q.json, bound to the ifp-2 config hash, league scoring, the roster-context version and the
sha256 of the forecast-building code (qb_scenario_forecast.FORECAST_CODE: qb_scenarios.py, qb_scenario_research.py
and qb_scenario_forecast.py). A later run whose spec or binding differs is refused, and a ledger whose recorded code
hash differs never counts, so the candidate cannot be re-selected or edited once prospective evidence exists. This
file only schedules and scores; editing it does not touch the frozen numbers. The freeze rule (when a week's ledger is written)
is ctx_shadow.py's, unchanged: the start model reads the same nflverse week-W designations as p1.

Writes only under <ifp-dir>/research/qb_scenario_shadow/ (git-ignored): the frozen spec, ledger/ (write-once, one file
per week), runs/ (write-once), freeze/ and missed/ (write-once), _research/ (cache: p1's feature store and the p2
candidates, rebuilt from the databases) and prospective.json (derived, replaced). Opens the databases read-only and
never touches research/context/ (p1), research/qb_scenarios/ (the p2 study), the live tables, the ifp-1/ifp-2
artifacts, the weekly ledger, the registry, the API or the frontend.

Exit codes: 0 ok (frozen, already frozen, deferred, nothing to do); 3 ok but with warnings; 1 a step failed.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import challenger_research as cr  # noqa: E402
import ctx_shadow as cs  # noqa: E402  (the freeze rule, injury snapshot and target-week helpers, unchanged)

from app.config import get_settings  # noqa: E402

CANDIDATE = "ifp-ctx-p2q"
RUN_SCHEMA = "ifp-ctx-p2q-shadow-run/v1"
LEDGER_SCHEMA = "ifp-ctx-p2q-ledger/v1"
SPEC_SCHEMA = "ifp-ctx-p2q-spec/v1"
FIRST_PROSPECTIVE = (2026, 7)       # pre-registered: only ledgers frozen from 2026 week 7 on count
PER_POSITION = {"QB": "S2", "RB": "ifp2", "WR": "ifp2", "TE": "ifp2"}
GATED_POSITIONS = ("QB",)
EXIT_OK, EXIT_FAILED, EXIT_WARNINGS = 0, 1, 3

RULES = {
    "candidate": "QB: ifp-ctx-p2 S2 (qb_scenarios.py, unchanged: ifp-2 spec + s_start + qb_self_epa, forecast "
                 "pi x f(start) + (1 - pi) x f(no start), ifp-2 when the team-week has no candidate). RB/WR/TE: ifp-2, "
                 "so only QB rows are frozen and gated",
    "evidence": "write-once pregame ledgers from 2026 week 7 on, frozen by ctx_shadow.py's freeze rule; a week counts "
                "once it is fully final (in the feature store with labels); never backfilled or re-forecast",
    "gate": "p1's prospective gate (challenger.SHADOW_GATE) on QB, S2 vs the ifp-2 refit frozen in the same ledger, "
            "identical rows: >= 8 fully final prospective weeks, >= 400 matched QB appearances, week-clustered 95% CI "
            "of the MAE difference with upper bound < 0, |bias(S2)| <= |bias(ifp-2)| + 0.15, starters-pool MAE not "
            "worse",
    "calibration": "reported, never gating: start-probability Brier / log loss / top-1 accuracy per team-week and "
                   "candidate-level reliability (qb_scenario_research.py's definitions) against the realized starter, "
                   "with p1's committed expected QB (hard) as the comparator; P(start | appears) Brier on QBs who "
                   "appeared",
    "honesty": "frozen before the p2 study's results are revisited; a gate pass is a reason to review, never a "
               "promotion. Production (projection_source.py, participation.py) is not changed by any outcome here",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def shadow_dir(args) -> Path:
    return Path(args.ifp_dir) / "research" / "qb_scenario_shadow"


def ledger_path(args, season: int, week: int) -> Path:
    return shadow_dir(args) / "ledger" / f"s{season}w{week:02d}-pregame.json"


def spec_path(args) -> Path:
    return shadow_dir(args) / f"frozen_{CANDIDATE}.json"


def _research_ns(args) -> SimpleNamespace:
    """Arguments for qb_scenario_research.build / store: its cache goes under OUR directory (_research/), never the
    p2 study's research/qb_scenarios/ or p1's research/context/."""
    return SimpleNamespace(ifp_dir=shadow_dir(args) / "_research", exits_dir=args.exits_dir, live_db=args.live_db,
                           history_db=args.history_db, season=args.season, rebuild=False)


def _sha_text(text: bytes) -> str:
    return hashlib.sha256(text).hexdigest()


def forecast_code_hash() -> str:
    """sha256 of the forecast-building code the frozen spec is bound to: qb_scenarios.py (the p2 model),
    qb_scenario_research.py (the study's store / candidate build) and qb_scenario_forecast.py (this week's forecast)."""
    import qb_scenario_forecast as qsf
    return qsf.forecast_code_hash()


# ----------------------------------------------------------------------------- the frozen spec


# What a frozen p2q spec is bound to (besides its own constants): any difference refuses every later predict/score.
FROZEN_BINDING = ("ifp2_config_hash", "scoring_fingerprint", "context_version", "forecast_code_hash")


def spec_now(cfg_hash: str, scoring_fp: str) -> dict:
    """The pre-registered spec as this code defines it (pure apart from reading qb_scenarios.py)."""
    import qb_scenarios as qs
    from app.analytics import challenger as ch
    from app.analytics import roster_context as rc

    spec = {"schema": SPEC_SCHEMA, "version": CANDIDATE, "status": "research_only", "derived_from": qs.P2_VERSION,
            "per_position": PER_POSITION, "gated_positions": list(GATED_POSITIONS),
            "qb_group": qs.QB2_GROUP["QB"], "first_prospective": list(FIRST_PROSPECTIVE),
            "start_model": {"version": qs.START_MODEL_VERSION, "features": qs.START_FEATURES, "l2": qs.START_L2,
                            "train_start": qs.START_TRAIN_START, "roster_member": list(qs.ROSTER_MEMBER),
                            "db_scale": qs.DB_SCALE, "relief_att_scale": qs.RELIEF_ATT_SCALE},
            "gate": {k: ch.SHADOW_GATE[k] for k in ("evidence_kind", "min_prospective_weeks",
                                                    "min_matched_appearances_per_position", "mae_ci95_upper_below",
                                                    "bias_tolerance_points", "starters_pool_mae_tolerance")},
            "freeze_rule": {"ready_share": cs.READY_SHARE, "slate_share": cs.SLATE_SHARE,
                            "deadline_hours": cs.DEADLINE_HOURS},
            "rules": RULES, "ifp2_config_hash": cfg_hash, "scoring_fingerprint": scoring_fp,
            "context_version": rc.CONTEXT_VERSION, "forecast_code_hash": forecast_code_hash()}
    spec["config_hash"] = _sha_text(json.dumps(spec, sort_keys=True, default=str).encode())[:16]
    return spec


def freeze_spec(path: Path, spec: dict, now: datetime, provenance: dict | None = None) -> dict:
    """Write the spec once; afterwards every run must match it exactly. Returns the frozen spec. Raises
    RuntimeError, naming what changed, when the spec or its binding differs (the file is never rewritten)."""
    from app.analytics.weekly_learning import write_once

    frozen = {**spec, "frozen_at": now.isoformat(), "provenance": provenance or {}}
    if not write_once(path, json.dumps(frozen, indent=1, default=str)):
        prev = json.loads(path.read_text())
        if prev.get("config_hash") != spec["config_hash"]:
            changed = [k for k in FROZEN_BINDING if prev.get(k) != spec.get(k)]
            changed += [k for k in spec if k not in FROZEN_BINDING and k != "config_hash" and prev.get(k) != spec[k]]
            raise RuntimeError(f"{path.name} was frozen at {prev.get('frozen_at')} with a different spec or binding "
                               f"({', '.join(changed) or 'config_hash'}); it is never overwritten. Restore the frozen "
                               "code / configuration, or pre-register a new candidate under a new name.")
        return prev
    return frozen


def _p2_study_provenance(args) -> dict:
    """The p2 study's frozen selection, if present: recorded (not bound) so the spec names the study it came from."""
    p = Path(args.ifp_dir) / "research" / "qb_scenarios" / "frozen_selection_ifp-ctx-p2.json"
    if not p.exists():
        return {"p2_frozen_selection": None}
    sel = json.loads(p.read_text())
    return {"p2_frozen_selection": {"sha256": _sha_text(p.read_bytes()), "config_hash": sel.get("config_hash"),
                                    "per_position": sel.get("per_position"), "frozen_at": sel.get("frozen_at")}}


def load_spec(args, cfg_hash: str, scoring_fp: str, now: datetime | None = None) -> dict:
    return freeze_spec(spec_path(args), spec_now(cfg_hash, scoring_fp), now or _utcnow(), _p2_study_provenance(args))


# ----------------------------------------------------------------------------- predict (the write-once ledger)


def _frame_sha(df) -> str | None:
    import hgb_research as hr
    return None if df is None else hr._frame_hash(df)


def cmd_predict(args) -> dict:
    """Frozen pregame QB forecasts (S2 and the ifp-2 refit, same run, same rows) for one week, written once, plus each
    open team's start-probability candidates. Teams that have kicked off are excluded (never backdated)."""
    import polars as pl

    import context_research as ctxr
    import hgb_research as hr
    import qb_scenario_forecast as qsf
    import qb_scenario_research as qsr
    from app.analytics import challenger as ch
    from app.analytics.forecast_features import key
    from app.analytics.weekly_learning import team_kickoffs, write_once

    season, week = args.season, args.week
    lpath = ledger_path(args, season, week)
    if lpath.exists():
        led = json.loads(lpath.read_text())
        return {"ledger": str(lpath), "written": False, "created_at": led["created_at"], "rows": len(led["rows"])}
    if (season, week) < FIRST_PROSPECTIVE:
        raise RuntimeError(f"prospective evidence starts {FIRST_PROSPECTIVE[0]} week {FIRST_PROSPECTIVE[1]}")
    ns = _research_ns(args)
    st, cands, prov = qsr.build(ns)                 # p1's store + p2 candidates of every fully final week (cached)
    cfg = ch.ChallengerConfig.load()
    spec = load_spec(args, cfg.hash(), prov["scoring_fingerprint"])
    if lpath.exists():                              # another process froze it during the slow build
        led = json.loads(lpath.read_text())
        return {"ledger": str(lpath), "written": False, "created_at": led["created_at"], "rows": len(led["rows"])}
    src, _ = hr._sources(ns)
    ctx = ctxr.ctx_sources(ns)
    kick = team_kickoffs(src, season, week)
    now = _utcnow()                                 # after the slow build: never select on a stale clock
    open_teams = sorted(t for t, ko in kick.items() if now < ko)
    if not open_teams:
        raise RuntimeError(f"every week-{week} game has kicked off: no pregame forecast possible")
    inj = None if src.injuries is None else src.injuries.filter((pl.col("season") == season) & (pl.col("week") == week))
    if (inj is None or inj.height == 0) and not args.allow_no_report:
        raise RuntimeError(f"the week-{week} injury report is not ingested yet (run sync_nfl.py first): a ledger is "
                           "write-once, so forecasts without it would be frozen degraded. --allow-no-report overrides.")
    cutoff = key(season, week)
    rows, cand_rows, start_model = qsf.forecast(ns, st, cands, cfg, src, ctx, season, week, open_teams, kick)
    # re-check every kickoff at write time: no row in a "pregame" ledger is ever written after its kickoff
    written_at = _utcnow()
    still_open = {t for t in open_teams if written_at < kick[t]}
    if not still_open:
        raise RuntimeError(f"every remaining week-{week} game kicked off while forecasting: nothing written")
    rows = [r for r in rows if r["team"] in still_open]
    cand_rows = [c for c in cand_rows if c["team"] in still_open]
    ledger = {"schema": LEDGER_SCHEMA, "version": CANDIDATE, "kind": "pregame", "season": season, "week": week,
              "created_at": written_at.isoformat(), "config_hash": spec["config_hash"],
              "spec_frozen_at": spec["frozen_at"], "training_cutoff_key": cutoff,
              "forecast_code_hash": spec["forecast_code_hash"],
              "store_fingerprint": prov["fingerprint"], "p2_build_fingerprint": prov["p2_build_fingerprint"],
              "teams_excluded_already_started": sorted(set(kick) - still_open),
              "start_model": start_model,
              "sources": {"injury_report_rows": None if inj is None else inj.height,
                          "injury_report_sha": _frame_sha(inj), "injury_report_timestamped": False,
                          "rosters_sha": _frame_sha(src.rosters), "panel_sha": _frame_sha(src.panel),
                          "starters_sha": _frame_sha(ctx.starters), "qb_stats_sha": _frame_sha(ctx.qb_stats),
                          "shadow_script_sha256": _sha_text(Path(__file__).read_bytes())},
              "candidates": cand_rows, "rows": rows}
    written = write_once(lpath, json.dumps(ledger, indent=1, default=str))
    return {"ledger": str(lpath), "written": written, "rows": len(rows), "created_at": ledger["created_at"],
            "forecast_rows": sum(r["s2_points"] is not None for r in rows), "open_teams": len(still_open)}


# ----------------------------------------------------------------------------- prospective scoring


def ledger_files(args) -> list[Path]:
    d = shadow_dir(args) / "ledger"
    return sorted(d.glob("s*-pregame.json")) if d.exists() else []


def counted(led: dict, spec: dict) -> bool:
    """A ledger is evidence only if it is a prospective week, was frozen under this spec and by the bound forecast code,
    and after the spec."""
    return ((led["season"], led["week"]) >= tuple(spec["first_prospective"])
            and led.get("config_hash") == spec["config_hash"]
            and led.get("forecast_code_hash") == spec["forecast_code_hash"]
            and datetime.fromisoformat(led["created_at"]) >= datetime.fromisoformat(spec["frozen_at"]))


def matched_weeks(p, positions) -> dict[str, int]:
    """Per position, the fully final weeks with at least one matched appearance: labelled, both forecasts present (the
    rows ctxr.report scores). An empty or unmatched ledger never counts toward the week requirement."""
    import polars as pl

    common = p.filter(pl.col("label").is_not_null() & pl.col("pred_ctx").is_not_null() & pl.col("pred_ifp2").is_not_null())
    return {pos: common.filter(pl.col("position") == pos).select("key").n_unique() for pos in positions}


def gate(r: dict, weeks: dict[str, int], spec: dict) -> dict:
    """context_research.cmd_prospective's gate (p1's), on the gated positions of `r` (ctxr.report of ifp2 vs ctx).
    `weeks`: matched_weeks per position."""
    g = spec["gate"]
    out = {}
    for pos in spec["gated_positions"]:
        x = r.get(pos) or {"n": 0}
        ci = (x.get("minus_ifp2") or {}).get("ctx", {}).get("ci95") if x["n"] else None
        sp = (x.get("cohorts") or {}).get("starters_pool") or {"n": 0}
        checks = {"weeks": weeks.get(pos, 0) >= g["min_prospective_weeks"],
                  "appearances": x["n"] >= g["min_matched_appearances_per_position"],
                  "mae_ci_upper_below_0": ci is not None and ci[1] < g["mae_ci95_upper_below"],
                  "bias": x["n"] > 0 and abs(x["metrics"]["ctx"]["bias"]) <= abs(x["metrics"]["ifp2"]["bias"])
                  + g["bias_tolerance_points"],
                  "starters_pool": sp["n"] > 0 and sp["ctx"]["mae"] <= sp["ifp2"]["mae"]
                  + g["starters_pool_mae_tolerance"]}
        out[pos] = {"checks": checks, "status": "meets_gate" if all(checks.values()) else (
            "insufficient_data" if not (checks["weeks"] and checks["appearances"]) else "fails_gate")}
    return out


def start_calibration(cand: "pl.DataFrame") -> dict:
    """Prospective start-probability calibration (qb_scenario_research.py's definitions). `cand`: one row per frozen
    candidate with p_start, pi_start, p1_expected, first_game, started (null: realized starter unknown), appeared."""
    import polars as pl

    import qb_scenario_research as qsr

    if cand.filter(pl.col("started").is_not_null()).height == 0:
        return {"team_weeks": 0}
    c = cand.with_columns(pl.col("p1_expected").cast(pl.Float64).alias("p_p1_hard"))
    out = {"p2": qsr._team_week_scores(c, "p_start"),
           "p1_hard": {k: v for k, v in qsr._team_week_scores(c, "p_p1_hard").items() if k != "reliability"}}
    fg = c.filter(pl.col("first_game"))
    out["first_game_only"] = ({k: v for k, v in qsr._team_week_scores(fg, "p_start").items() if k != "reliability"}
                              if fg.filter(pl.col("started").is_not_null()).height else {"team_weeks": 0})
    ap = c.filter((pl.col("appeared") == 1) & pl.col("started").is_not_null() & pl.col("pi_start").is_not_null())
    out["pi_given_appearance"] = {"n": ap.height, **({"brier": round(float(((ap["pi_start"] - ap["started"]) ** 2)
                                                                         .mean()), 4),
                                                      "reliability": qsr._reliability(ap["pi_start"].to_numpy(),
                                                                                      ap["started"].to_numpy())}
                                                     if ap.height else {})}
    return out


def realized(led: dict, starters, panel) -> list[dict]:
    """The ledger's candidates with the realized outcome: started (null when the team's starter is unknown) and
    appeared. `starters` / `panel`: polars frames of the week (team, qb_id) / (gsis_id, team)."""
    actual = dict(starters.select("team", "qb_id").iter_rows())
    played = set(zip(panel["gsis_id"].to_list(), panel["team"].to_list()))
    return [{**c, "key": led["season"] * 100 + led["week"],
             "started": None if actual.get(c["team"]) is None else float(c["gsis_id"] == actual[c["team"]]),
             "appeared": float((c["gsis_id"], c["team"]) in played)} for c in led.get("candidates", [])]


def cmd_prospective(args) -> dict:
    """Score every counted ledger of a fully final week: the gate (S2 vs same-run ifp-2), the frozen production ifp-2
    on identical rows, and the start-probability calibration. Writes prospective.json (replaced)."""
    import polars as pl

    import context_research as ctxr
    import hgb_research as hr
    import qb_scenario_research as qsr
    from app.analytics import challenger as ch

    ns = _research_ns(args)
    st, prov = qsr.store(ns)
    spec = load_spec(args, ch.ChallengerConfig.load().hash(), prov["scoring_fingerprint"])
    parts, cand, weeks, skipped = [], [], [], []
    src = ctx = None
    for path in ledger_files(args):
        led = json.loads(path.read_text())
        if not counted(led, spec):
            skipped.append({"ledger": path.name, "reason": "before the prospective start, another spec, or older "
                                                           "than the frozen spec"})
            continue
        k = led["season"] * 100 + led["week"]
        if not st.filter(pl.col("key") == k).height:     # week not fully final yet
            continue
        if src is None:
            src, _ = hr._sources(ns)
            ctx = ctxr.ctx_sources(ns)
        prod = cs._production_rows(args.weekly_dir, led["season"], led["week"])
        parts.append(pl.DataFrame([{"key": k, "gsis_id": r["gsis_id"], "pred_ifp2": r["ifp2_points"],
                                    "pred_ctx": r["s2_points"], "pred_production": prod.get(r["gsis_id"])}
                                   for r in led["rows"]],
                                  schema={"key": pl.Int64, "gsis_id": pl.String, "pred_ifp2": pl.Float64,
                                          "pred_ctx": pl.Float64, "pred_production": pl.Float64}))
        wk = (pl.col("season") == led["season"]) & (pl.col("week") == led["week"])
        cand += realized(led, ctx.starters.filter(wk).unique("team", keep="first"), src.panel.filter(wk))
        weeks.append({"season": led["season"], "week": led["week"], "ledger": path.name, "rows": len(led["rows"]),
                      "production_rows": len(prod)})
    out = {"candidate": CANDIDATE, "config_hash": spec["config_hash"], "scored_weeks": len(weeks), "weeks": weeks,
           "skipped": skipped, **prov}
    if not parts:
        out.update(status="insufficient_data", gate={p: {"status": "insufficient_data"} for p in GATED_POSITIONS})
    else:
        p = st.join(pl.concat(parts), on=["key", "gsis_id"], how="inner")
        r = ctxr.report(p, ["ifp2", "ctx"])
        mw = matched_weeks(p, spec["gated_positions"])
        out.update(status="scored", report=r, matched_weeks=mw, gate=gate(r, mw, spec))
        both = p.filter(pl.col("label").is_not_null() & pl.col("pred_ctx").is_not_null()
                        & pl.col("pred_production").is_not_null())
        out["alongside_production"] = cs._metrics([(None, None, *t) for t in both.select(
            "label", "pred_ctx", "pred_ifp2", "pred_production").iter_rows()])
        cf = pl.DataFrame(cand, schema={"key": pl.Int64, "team": pl.String, "gsis_id": pl.String,
                                        "p_start": pl.Float64, "pi_start": pl.Float64, "rank": pl.Int64,
                                        "first_game": pl.Boolean, "p1_expected": pl.Float64,
                                        "p1_confidence": pl.String, "prev_start": pl.Float64,
                                        "questionable": pl.Float64, "doubtful": pl.Float64,
                                        "unavailable": pl.Float64, "started": pl.Float64, "appeared": pl.Float64},
                          orient="row") if cand else None
        out["start_calibration"] = start_calibration(cf) if cf is not None else {"team_weeks": 0}
    path = shadow_dir(args) / "prospective.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    cs._replace_json(path, out)
    return {k: out[k] for k in ("status", "scored_weeks", "gate") if k in out} | {
        "pooled_qb": (out.get("report") or {}).get("QB", {}).get("minus_ifp2"),
        "start_calibration": {k: v for k, v in (out.get("start_calibration") or {}).get("p2", {}).items()
                              if k != "reliability"}}


# ----------------------------------------------------------------------------- scheduling (ctx_shadow.py's contract)


def _record(args, rec: dict, now: datetime) -> Path:
    path = (shadow_dir(args) / "runs"
            / f"s{rec['season']}w{rec['week']:02d}-{now:%Y%m%dT%H%M%SZ}-{rec['kind']}-{os.getpid()}.json")
    if args.write:
        cs._write_once(path, rec)
    return path


def run_pregame(args, season: int, week: int, *, now: datetime | None = None, snapshot=None, predict=None) -> dict:
    """Freeze week W's ledger once, when ctx_shadow.py's freeze rule allows. Never raises: every outcome is returned
    and, unless dry, recorded write-once under runs/."""
    now = now or _utcnow()
    snapshot = snapshot or cs.injury_snapshot
    predict = predict or (lambda a, s, w: cmd_predict(SimpleNamespace(**{**vars(a), "season": s, "week": w})))
    rec = {"schema": RUN_SCHEMA, "kind": "pregame", "candidate": CANDIDATE, "season": season, "week": week,
           "started_at": now.isoformat(), "dry_run": not args.write, "warnings": [], "notes": []}
    lpath = ledger_path(args, season, week)
    try:
        if (season, week) < FIRST_PROSPECTIVE:
            rec.update(status="before_prospective_start",
                       reason=f"prospective evidence starts {FIRST_PROSPECTIVE[0]} week {FIRST_PROSPECTIVE[1]}")
            return rec
        if lpath.exists():
            rec.update(status="already_frozen", ledger=str(lpath), ledger_sha256=cs._sha_file(lpath))
            return rec                                  # idempotent: no record, no predict, nothing rewritten
        diag = snapshot(args, season, week, now)
        dec = cs.freeze_decision(diag["teams"])
        rec["injury"] = {"summary": diag["summary"], "nflverse_writer": diag.get("nflverse_writer"),
                         "log_present": diag.get("log_present"), "upcoming_issues": diag.get("upcoming_issues")}
        rec["decision"] = {k: v for k, v in dec.items() if k not in ("warnings", "notes")}
        rec["warnings"] += dec["warnings"]
        rec["notes"] += dec["notes"]
        if not diag.get("log_present"):
            rec["warnings"].append("no injury_retrievals log: no injury retrieval has ever been verified")
        if dec["action"] != "freeze":
            rec["status"] = "deferred" if dec["action"] == "defer" else "nothing_open"
        elif not args.write:
            rec["status"] = "would_freeze"
        else:
            res = predict(args, season, week)
            rec["predict"] = res
            written = bool(res.get("written"))
            path = Path(res.get("ledger") or lpath)
            rec.update(status="frozen" if written else "already_frozen", ledger=str(path),
                       ledger_sha256=cs._sha_file(path) if path.exists() else None)
            if written:
                cs._write_once(shadow_dir(args) / "freeze" / f"s{season}w{week:02d}.json", {
                    "schema": "ifp-ctx-p2q-shadow-freeze/v1", "candidate": CANDIDATE, "season": season, "week": week,
                    "ledger": path.name, "ledger_sha256": rec["ledger_sha256"],
                    "ledger_created_at": res.get("created_at"), "decided_at": now.isoformat(),
                    "decision": rec["decision"], "degraded": dec["degraded"], "warnings": rec["warnings"],
                    "injury": rec["injury"]})
    except (Exception, SystemExit) as err:     # noqa: BLE001  recorded, never raised: the callers must not fail
        rec.update(status="failed", error=f"{err.__class__.__name__}: {err}")
        rec["warnings"].append(f"pregame shadow forecast for week {week} failed: {err}")
    rec["finished_at"] = _utcnow().isoformat()
    rec["record"] = str(_record(args, rec, now))
    return rec


def check_missed(args, season: int, week: int, *, now: datetime | None = None, kickoffs=None) -> dict | None:
    """A prospective week whose games have all kicked off without a ledger is recorded write-once under missed/."""
    if week < 1 or (season, week) < FIRST_PROSPECTIVE or ledger_path(args, season, week).exists():
        return None
    now = now or _utcnow()
    if kickoffs is None:
        import duckdb
        from app.analytics.injury_freshness import scheduled_kickoffs
        con = duckdb.connect(str(args.live_db), read_only=True)
        try:
            kickoffs = scheduled_kickoffs(con, season, week)
        finally:
            con.close()
    known = [k for k in kickoffs.values() if k is not None]
    if not known or max(known) > now:
        return None
    rec = {"candidate": CANDIDATE, "season": season, "week": week, "last_kickoff": max(known).isoformat(),
           "recorded_at": now.isoformat(),
           "reason": f"no {CANDIDATE} pregame ledger was frozen before the week's games; nothing is backdated"}
    if args.write:
        cs._write_once(shadow_dir(args) / "missed" / f"s{season}w{week:02d}.json", rec)
    return rec


def run_score(args, *, now: datetime | None = None, prospective=None) -> dict:
    now = now or _utcnow()
    prospective = prospective or cmd_prospective
    rec = {"schema": RUN_SCHEMA, "kind": "score", "candidate": CANDIDATE, "season": args.season, "week": 0,
           "started_at": now.isoformat(), "dry_run": not args.write, "warnings": [], "notes": []}
    ledgers = ledger_files(args)
    try:
        if not ledgers:
            rec.update(status="no_ledgers", notes=[f"no {CANDIDATE} ledger frozen yet: nothing to score"])
            return rec
        if not args.write:
            rec.update(status="would_score", ledgers=[p.name for p in ledgers])
            return rec
        rec["prospective"] = prospective(args)
        rec["status"] = "scored"
    except (Exception, SystemExit) as err:     # noqa: BLE001
        rec.update(status="failed", error=f"{err.__class__.__name__}: {err}")
        rec["warnings"].append(f"scoring the {CANDIDATE} ledgers failed: {err}")
    rec["finished_at"] = _utcnow().isoformat()
    rec["record"] = str(_record(args, rec, now))
    return rec


def _summary(rec: dict) -> dict:
    keep = ("kind", "season", "week", "status", "reason", "ledger", "ledger_sha256", "error", "record", "warnings",
            "notes")
    out = {k: rec[k] for k in keep if k in rec}
    if "decision" in rec:
        out["decision"] = {k: rec["decision"][k] for k in ("action", "degraded", "reason", "open_teams",
                                                           "ready_teams", "imminent_teams")}
    if rec.get("kind") == "score" and rec.get("status") == "scored":
        out["gate"] = {p: g["status"] for p, g in (rec["prospective"].get("gate") or {}).items()}
        out["scored_weeks"] = rec["prospective"].get("scored_weeks")
        out["start_calibration"] = rec["prospective"].get("start_calibration")
    return out


def cmd_pregame(args, now: datetime) -> dict:
    week = args.week if args.week is not None else cs._target_week(args, now)
    if week is None:
        return {"kind": "pregame", "status": "no_upcoming_week", "warnings": [], "notes": []}
    return _summary(run_pregame(args, args.season, week, now=now))


def cmd_score(args, now: datetime) -> dict:
    return _summary(run_score(args, now=now))


def cmd_cycle(args, now: datetime) -> dict:
    """learning_cycle.py's stage: score the frozen weeks, record a missed previous week, then try the target week's
    pregame freeze (usually deferred on Tue-Thu)."""
    week = args.week if args.week is not None else cs._target_week(args, now)
    out = {"score": _summary(run_score(args, now=now))}
    prev = week - 1 if week else cs._last_week(args)
    missed = check_missed(args, args.season, prev, now=now) if prev else None
    out["missed"] = missed
    out["pregame"] = (_summary(run_pregame(args, args.season, week, now=now)) if week
                      else {"status": "no_upcoming_week", "warnings": [], "notes": []})
    out["warnings"] = [*out["score"]["warnings"], *out["pregame"]["warnings"]]
    if missed:
        out["warnings"].append(f"week {missed['week']} started without a frozen {CANDIDATE} ledger (recorded as missed)")
    out["status"] = "failed" if "failed" in (out["score"]["status"], out["pregame"]["status"]) else "ok"
    return out


def cmd_status(args, now: datetime) -> dict:
    d = shadow_dir(args)
    runs = sorted((d / "runs").glob("*.json"), key=lambda p: p.stat().st_mtime) if (d / "runs").exists() else []
    pro = d / "prospective.json"
    return {"spec": json.loads(spec_path(args).read_text()) if spec_path(args).exists() else None,
            "ledgers": [p.name for p in ledger_files(args)],
            "freeze_snapshots": sorted(p.name for p in (d / "freeze").glob("*.json")),
            "missed": sorted(p.name for p in (d / "missed").glob("*.json")),
            "latest_run": json.loads(runs[-1].read_text()) if runs else None,
            "prospective": json.loads(pro.read_text()) if pro.exists() else None}


def parse(argv=None):
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["pregame", "score", "cycle", "status", "predict"])
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--week", type=int, help="default: the upcoming week (earliest with a game still to kick off)")
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    ap.add_argument("--weekly-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"),
                    help="the weekly learning output holding the frozen production ifp-2 ledger")
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--allow-no-report", action="store_true", help="predict: freeze forecasts without the week's report")
    ap.add_argument("--now", help="evaluate the freeze rule at this ISO time; implies --dry-run")
    ap.add_argument("--dry-run", action="store_true", help="decide and print only: no forecast, no record")
    args = ap.parse_args(argv)
    if args.command == "predict" and args.week is None:
        ap.error("predict needs --week")
    args.write = not (args.dry_run or args.now)
    return args


def main(argv=None) -> int:
    args = parse(argv)
    now = _utcnow()
    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    if args.command == "predict":
        print(json.dumps(cmd_predict(args), indent=1, default=str))
        return EXIT_OK
    cmds = {"pregame": cmd_pregame, "score": cmd_score, "cycle": cmd_cycle, "status": cmd_status}
    out = cmds[args.command](args, now)
    print(json.dumps(out, indent=1, default=str))
    if args.command == "status":
        return EXIT_OK
    for w in out.get("warnings", []):
        print(f"WARNING: {CANDIDATE} shadow: {w}", file=sys.stderr)
    if out.get("status") == "failed":
        return EXIT_FAILED
    return EXIT_WARNINGS if out.get("warnings") else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
