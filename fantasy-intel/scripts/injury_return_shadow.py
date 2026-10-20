"""Prospective shadow of two pre-registered fixes for injury-exit players on top of ifp-2.2.0 (research only; see
docs/research/injury_return_split.md, which fixed everything below before any run):

  ifp-irs-p1  split snap_trend slope: + snap_trend_ret = snap_trend if returner else 0      RB, WR, TE
  ifp-irb-p1  returner bias: + ret_ind = 1 if returner else 0 (per-position ridge, so position x returner)
  both        both columns (reported only; used only if both candidates pass)

A returner is an RB/WR/TE whose last appearance was an injury exit (`injury_exit_last = 1`) and whose last team played
at least one game since (`games_missed >= 1`; injury_return_research.with_cohort). A play-through row is an injury
exit with `games_missed = 0`.

    cd backend
    uv run python ../scripts/injury_return_shadow.py prepare                # build the store cache
    uv run python ../scripts/injury_return_shadow.py pregame [--week W]     # gated write-once pregame ledger for week W
    uv run python ../scripts/injury_return_shadow.py predict --week W       # freeze now (no gate; still write-once)
    uv run python ../scripts/injury_return_shadow.py score                  # score fully final frozen weeks, the gate
    uv run python ../scripts/injury_return_shadow.py cycle   [--week W]     # score, missed-week check, then pregame
    uv run python ../scripts/injury_return_shadow.py status
    uv run python ../scripts/injury_return_shadow.py pregame --week W --now 2026-11-01T12:00Z   # gate only (dry run)

Who runs it (no new scheduler), exactly like scripts/signal_shadows.py:
  * refresh_all.py's read-only evidence stage runs `pregame` for the upcoming week on every refresh.
  * learning_cycle.py runs `cycle` as a non-fatal RESEARCH_SHADOWS stage after the production run (Tue-Thu).
  * shadow_promotion_check.py reads scores.json; a pass is a "PROMOTION READY" line and a promotion-queue entry.

Freeze rule (signal_shadows'): the week's ledger is frozen at the first run at which at least SLATE_SHARE of the teams
that have not kicked off do so within DEADLINE_HOURS. Teams that already kicked off are excluded. Features are built as
production builds a horizon-0 row (features_for_week, injury_model.augment, context_model.augment on the cutoff week's
designations with the ESPN status fallback). ifp-2.2.0 (`base`) and the candidates are fitted in the same run on the
same training rows (challenger.train_rows) with ifp-2.2.0's frozen penalty, loss and recency; nothing is searched.

Gate (pre-registered, one look): at the first scoring with the LOOK week fully final and at least MIN_WEEKS weeks
and MIN_COHORT cohort rows scored, a candidate passes iff (1) on its cohort (RB/WR/TE pooled) MAE(candidate) -
MAE(base) has a week-clustered 95% CI entirely below 0, (2) the overall RB/WR/TE difference is <= 0 and (3) no
position's all-row CI lies entirely above 0. The decision is written once (decision_<candidate>.json) and never
re-evaluated. Before the look the status is `accumulating`.

Writes only under <ifp-dir>/research/injury_return_shadow/ (git-ignored): store.parquet (cache), frozen/spec.json,
ledger/ (write-once), runs/ and missed/ (write-once records), decision_<candidate>.json (write-once) and scores.json
(derived, replaced). It opens the databases read-only and never touches the live database tables, the production
artifacts, the weekly ledger, the registry, the API or the frontend.

Exit codes: 0 ok (frozen, already frozen, deferred, nothing to do); 3 ok but with warnings (a missed week, a failed
step recorded as a warning); 1 a step failed (recorded, nothing half-written).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import challenger_research as cr  # noqa: E402  (path defaults shared with the other research scripts)

from app.config import get_settings  # noqa: E402

SCHEMA = "ifp-injury-return-shadow-run/v1"
LEDGER_SCHEMA = "ifp-injury-return-shadow-ledger/v1"
SHADOW = "injury_return_shadow"
DOC = "docs/research/injury_return_split.md"
IRS, IRB, BOTH = "ifp-irs-p1", "ifp-irb-p1", "both"
CANDIDATES = (IRS, IRB)                       # judged, each on its own
BOTH_CANDIDATE = "ifp-irsb-p1"                # the promotion name of `both` (shadow_promotion_check, rule 1.6)
MODELS = (IRS, IRB, BOTH)                     # frozen in the ledger
SHORT = {IRS: "irs", IRB: "irb", BOTH: "both"}      # ledger columns: <short>_points
EXTRA = {IRS: ("snap_trend_ret",), IRB: ("ret_ind",), BOTH: ("snap_trend_ret", "ret_ind")}
COHORT = {IRS: "injury_exit", IRB: "returner"}
POSITIONS = ("RB", "WR", "TE")
FIRST_PROSPECTIVE = (2026, 8)       # pre-registered: week 7's main slate kicked off before the pre-registration
LOOK = (2026, 18)                   # the one look: once this week is fully final (later, if the minimums are unmet)
MIN_WEEKS = 8
MIN_COHORT = 40
RESAMPLES = 2000
SEED = 20261019
SLATE_SHARE = 0.5
DEADLINE_HOURS = 6.0
INPUT_COLUMNS = ("injury_exit_last", "games_missed", "snap_trend", "games_recent")
PASS, FAIL, ACCUMULATING = "passes", "fails", "accumulating"
BASE_MOVED = "base moved"           # frozen_spec's refusal: the shadow cannot continue without a person deciding
EXIT_OK, EXIT_FAILED, EXIT_WARNINGS = 0, 1, 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def out_dir(args) -> Path:
    return Path(args.ifp_dir) / "research" / SHADOW


def ledger_path(args, season: int, week: int) -> Path:
    return out_dir(args) / "ledger" / f"s{season}w{week:02d}-pregame.json"


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _write_once(path: Path, obj) -> bool:
    from app.analytics.weekly_learning import write_once
    return write_once(path, json.dumps(obj, indent=1, default=str))


def _replace_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    os.replace(tmp, path)


def _sha_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ----------------------------------------------------------------------------- definition


def candidate_columns(rows, games):
    """`rows` plus games_missed, ret_cohort (injury_return_research.with_cohort: RB/WR/TE, injury exit, >= 1 team game
    missed since), snap_trend_ret and ret_ind. `rows` needs key, gsis_id, position, last_team, last_season, last_week,
    injury_exit_last and snap_trend."""
    import injury_return_research as irr
    import polars as pl

    out = irr.with_cohort(rows, games)
    ret = pl.col("ret_cohort")
    return out.with_columns(
        pl.when(ret).then(pl.col("snap_trend").cast(pl.Float64).fill_null(0.0)).otherwise(0.0).alias("snap_trend_ret"),
        ret.cast(pl.Float64).alias("ret_ind"))


def model_features(cfg, name: str, position: str) -> list[str]:
    """ifp-2.2.0's features for the position (context_model.SHIPPED), plus the model's added columns."""
    from app.analytics import context_model as cm

    base = cm.features(cfg, position)
    return base + [c for c in EXTRA[name] if c not in base] if name != "base" else base


def base_identity(cfg) -> dict:
    """The ifp-2.2.0 base the candidates extend, narrowed to what changes its RB/WR/TE forecasts: context_model's and
    injury_model's definitions without their whole-config hashes and QB-only parts, plus each RB/WR/TE model's
    features, shipped components and fit (penalty, loss, Huber threshold, recency, usage prior). An unrelated
    challenger-config edit (a QB change, another shadow) leaves it unchanged. Frozen in the spec: a later run on a
    moved base is refused, and no ledger of another base is pooled."""
    from app.analytics import context_model as cm
    from app.analytics import injury_model as im

    drop = {"base_config_hash", "features", "components", "guard_dropped", "qb_scenario_code_sha256",
            "participation_calibration"}
    positions = {}
    for p in POSITIONS:
        sp = cfg.spec(p)
        positions[p] = {"features": cm.features(cfg, p), "components": list(cm.SHIPPED[p]),
                        "guard_dropped": cm.GUARD_DROPPED.get(p, {}), "lam": sp.lam[p], "loss": sp.loss,
                        "huber_c": sp.huber_c, "half_life_weeks": sp.half_life_weeks,
                        "usage_prior_games": sp.usage_prior_games}
    ident = {"context_model": {k: v for k, v in cm.config(cfg).items() if k not in drop},
             "injury_model": {k: v for k, v in im.config(cfg).items() if k not in drop}, "positions": positions,
             "usage_prior_games": cfg.usage_prior_games}
    return {"base_version": cm.MODEL_VERSION, "rb_wr_te_hash": _sha(ident)}


def spec(scoring_fp: str, base: dict) -> dict:
    """The pre-registered definition (everything a forecast depends on except data, the ifp-2.2.0 base included),
    hashed."""
    s = {"version": SHADOW, "status": "research_only", "doc": DOC, "positions": list(POSITIONS),
         "candidates": {n: {"added": list(EXTRA[n]), "cohort": COHORT.get(n), "judged": n in CANDIDATES}
                        for n in MODELS},
         "definitions": {"returner": "injury_exit_last = 1 and games_missed >= 1 (RB/WR/TE)",
                         "play_through": "injury_exit_last = 1 and games_missed = 0",
                         "games_missed": "games of the last team strictly between the last appearance and the week",
                         "snap_trend_ret": "snap_trend if returner else 0", "ret_ind": "1 if returner else 0"},
         "fit": "ifp-2.2.0's per-position features, training rows, penalty, loss and recency (same run, same rows)",
         "first_week": list(FIRST_PROSPECTIVE), "look": list(LOOK),
         "rule": {"min_weeks": MIN_WEEKS, "min_cohort": MIN_COHORT, "resamples": RESAMPLES, "seed": SEED,
                  "cohort_ci_entirely_below_0": True, "overall_diff_at_most_0": True,
                  "no_position_ci_entirely_above_0": True},
         "freeze_rule": {"slate_share": SLATE_SHARE, "deadline_hours": DEADLINE_HOURS},
         "scoring_fingerprint": scoring_fp, "base": dict(base)}
    s["config_hash"] = _sha(s)
    return s


def frozen_spec(args, scoring_fp: str, base: dict, *, create: bool) -> dict | None:
    """The spec, frozen write-once by the first `predict`; any later run with another spec, league scoring or
    ifp-2.2.0 base is refused. `score` never creates it (scoring cannot precede the pre-registered spec)."""
    s = spec(scoring_fp, base)
    path = out_dir(args) / "frozen" / "spec.json"
    if create:
        _write_once(path, {**s, "frozen_at": _utcnow().isoformat()})
    if not path.exists():
        return None
    prev = json.loads(path.read_text())
    if prev.get("base") != s["base"]:
        raise SystemExit(f"{path.name} was frozen on base {prev.get('base')}, this run's base is {s['base']}: the "
                         f"ifp-2.2.0 {BASE_MOVED}; refusing")
    if prev["config_hash"] != s["config_hash"]:
        raise SystemExit(f"{path.name} was frozen with a different spec or league scoring; refusing")
    return prev


def check_ledger_bases(args, ledgers: list[dict]) -> None:
    """Refuse to pool a ledger whose base differs from the frozen spec's (or any ledger without a frozen spec)."""
    path = out_dir(args) / "frozen" / "spec.json"
    if not ledgers:
        return
    if not path.exists():
        raise SystemExit("ledgers exist but no frozen spec: refusing to score")
    base = json.loads(path.read_text()).get("base") or {}
    bad = [f"s{x['season']}w{x['week']:02d}" for x in ledgers if {k: x.get(k) for k in base} != base]
    if bad:
        raise SystemExit(f"ledgers {', '.join(bad)} were frozen on another ifp-2.2.0 base than the spec's {base}; "
                         "refusing to pool them")


# ----------------------------------------------------------------------------- store


def _inputs(args):
    import weekly_learning as wlcli
    from app.analytics import context_model as cm
    from app.db.connection import connect as db_connect

    live = db_connect(args.live_db, read_only=True)
    hist = db_connect(args.history_db, read_only=True) if args.history_db.exists() else None
    try:
        inputs = cm.load_inputs([hist, live], args.exits_dir / wlcli.DEPTH_DIR, args.season)
        return inputs, wlcli.roster_codes(live, hist)
    finally:
        for c in (live, hist):
            if c is not None:
                c.close()


def build(args):
    """(sources, scoring, store, provenance): ifp-2.2.0's training store of every fully final week (build_store plus
    context_model.with_training_inputs, as ifp22_backtest.py builds it) plus the candidate columns, cached by an input
    fingerprint (sources, weeks, scoring, code, depth snapshots)."""
    import hgb_research as hr
    import polars as pl
    import weekly_learning as wlcli
    from app.analytics import context_model as cm
    from app.analytics.forecast_backtest import build_store

    src, scoring = hr._sources(args)
    weeks = hr.final_weeks(args, src)
    manifest = args.exits_dir / wlcli.DEPTH_DIR / "manifest.json"
    fp = _sha({"store": hr.input_fingerprint(args, src, scoring.fingerprint, weeks),
               "depth": manifest.read_text() if manifest.exists() else ""})
    path, meta = out_dir(args) / "store.parquet", out_dir(args) / "store.meta.json"
    prov = {"fingerprint": fp, "scoring_fingerprint": scoring.fingerprint, "weeks": len(weeks),
            "latest_week_key": max((s * 100 + w for s, w in weeks), default=None)}
    if path.exists() and meta.exists() and not getattr(args, "rebuild", False) \
            and json.loads(meta.read_text()).get("fingerprint") == fp:
        return src, scoring, pl.read_parquet(path), prov
    inputs, codes = _inputs(args)
    prov_inputs = (inputs, codes)
    store = cm.with_training_inputs(src, build_store(src, weeks), inputs, codes)
    store = candidate_columns(store, src.games)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    store.write_parquet(tmp)
    os.replace(tmp, path)
    _replace_json(meta, {**prov, "rows": store.height})     # refresh's pregame and the cycle can overlap
    return src, scoring, store, {**prov, "loaded_inputs": prov_inputs}   # in memory only: reused by predict


# ----------------------------------------------------------------------------- predict (the freeze)


def fit_models(store, season: int, week: int, cfg) -> tuple[dict, dict]:
    """({position: {"base" | candidate: RidgeModel}}, {position: training rows}) on identical rows strictly before W."""
    from app.analytics import challenger as ch
    from app.analytics import context_model as cm
    from app.analytics.forecast_features import key

    k = key(season, week)
    models, n = {}, {}
    for pos in POSITIONS:
        tr = ch.train_rows(store, k, pos)
        assert tr.height == 0 or tr["key"].max() < k, "training row at or after the target week"
        n[pos] = tr.height
        if tr.height < ch.MIN_TRAIN_ROWS:
            continue
        sp = cfg.spec(pos)
        models[pos] = {m: ch.fit_linear(tr, pos, model_features(cfg, m, pos), sp.lam[pos], loss=sp.loss,
                                        huber_c=sp.huber_c, half_life_weeks=sp.half_life_weeks, target_key=k,
                                        name=cm.MODEL_NAME) for m in ("base", *MODELS)}
    return models, n


def augment_artifact(cfg, src, season: int, week: int, scoring, models: dict, inputs):
    """An ifp-2.2.0 Artifact carrying the configs injury_model.augment and context_model.augment read (the same-run
    base models; no QB start model: QB is not forecast here)."""
    from app.analytics import context_model as cm
    from app.analytics import injury_model as im
    from app.analytics.independent_projections import Artifact

    return Artifact(version=cm.MODEL_VERSION, created_at=_utcnow().isoformat(), scoring_fingerprint=scoring.fingerprint,
                    scoring=scoring.describe(),
                    config={im.CONFIG_KEY: im.artifact_config(cfg, im.fit_return_table(src, season, week)),
                            cm.CONFIG_KEY: cm.artifact_config(cfg, start_model=None, fallback={},
                                                              participation_map=None, participation_note="",
                                                              depth_note=inputs.depth_note),
                            "config_hash": cm.config_hash(cfg)},
                    models={p: v["base"] for p, v in models.items()}, intervals={}, status={}, evaluation={})


def live_features(args, src, art, inputs, codes, season: int, week: int, teams: list[str], now: datetime):
    """Pregame horizon-0 rows of week W for the open teams' RB/WR/TE, built as independent_projection_store.build_live
    builds them (cutoff-week designations with the ESPN status fallback), plus the candidate columns."""
    import polars as pl
    from app.analytics import context_model as cm
    from app.analytics import injury_model as im
    from app.analytics.forecast_features import features_for_week
    from app.analytics.independent_projection_store import espn_designation_fallback
    from app.analytics.participation import live_population
    from app.db.connection import connect as db_connect

    pop = live_population(src, season, week).filter(pl.col("team").is_in(teams) & pl.col("position").is_in(POSITIONS))
    if pop.height == 0:
        return pop, {}
    con = db_connect(args.live_db, read_only=True)
    try:
        hsrc, fallback = espn_designation_fallback(con, src, season, week)
    finally:
        con.close()
    feats = features_for_week(src, season, week, pop)
    feats = im.augment(art, hsrc, season, week, week, feats, roster_codes=codes)
    feats = cm.augment(art, hsrc, inputs, season, week, week, feats, as_of=now)
    return candidate_columns(feats, src.games), {"espn_fallback_teams": sorted(fallback)}


def cmd_predict(args, now: datetime | None = None) -> dict:
    """Frozen pregame forecasts (base and every model, same run, same rows) for one week, written once. Teams that
    have kicked off are excluded (never backdated)."""
    import polars as pl
    from app.analytics import challenger as ch
    from app.analytics import context_model as cm
    from app.analytics.weekly_learning import team_kickoffs

    season, week = args.season, args.week
    if (season, week) < FIRST_PROSPECTIVE:
        raise SystemExit(f"{SHADOW} is pre-registered from {FIRST_PROSPECTIVE[0]} week {FIRST_PROSPECTIVE[1]}; "
                         f"refusing a ledger for {season} week {week}")
    lpath = ledger_path(args, season, week)
    if lpath.exists():
        led = json.loads(lpath.read_text())
        return {"ledger": str(lpath), "written": False, "created_at": led["created_at"], "rows": len(led["rows"])}
    src, scoring, store, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    base = base_identity(cfg)
    sp = frozen_spec(args, scoring.fingerprint, base, create=True)
    kick = team_kickoffs(src, season, week)
    clock = (lambda: now) if now is not None else _utcnow       # `now`: a fixed clock (tests)
    now = clock()                                # after the slow build: never select on a stale clock
    open_teams = sorted(t for t, ko in kick.items() if now < ko)
    if not open_teams:
        raise SystemExit(f"every week-{week} game has kicked off: no pregame forecast possible")
    models, n_train = fit_models(store, season, week, cfg)
    inputs, codes = prov.get("loaded_inputs") or _inputs(args)
    art = augment_artifact(cfg, src, season, week, scoring, models, inputs)
    feats, live_prov = live_features(args, src, art, inputs, codes, season, week, open_teams, now)
    rows = []
    for pos in POSITIONS:
        g = feats.filter(pl.col("position") == pos) if feats.height else feats
        if g.height == 0 or pos not in models:
            continue
        preds = {m: mdl.predict(g) for m, mdl in models[pos].items()}
        for i, rec in enumerate(g.iter_rows(named=True)):
            played = (rec.get("games_recent") or 0) > 0
            row = {"gsis_id": rec["gsis_id"], "position": pos, "team": rec["team"],
                   "kickoff": kick[rec["team"]].isoformat(),
                   "base_points": round(float(preds["base"][i]), 4) if played else None,
                   **{f"{SHORT[m]}_points": round(float(preds[m][i]), 4) if played else None for m in MODELS},
                   "ret": bool(rec["ret_cohort"]),
                   "inputs": {c: rec.get(c) for c in INPUT_COLUMNS}}
            rows.append(row)
    # re-check every kickoff at write time: no row in a "pregame" ledger is ever written after its kickoff
    written_at = clock()
    still_open = {t for t in open_teams if written_at < kick[t]}
    rows = [r for r in rows if r["team"] in still_open]
    if not still_open:
        raise SystemExit(f"every remaining week-{week} game kicked off while forecasting: nothing written")
    ledger = {"schema": LEDGER_SCHEMA, "kind": "pregame", "season": season, "week": week,
              "created_at": written_at.isoformat(), "spec_hash": sp["config_hash"],
              **base,
              "training_cutoff_key": season * 100 + week, "training_rows": n_train,
              "store_fingerprint": prov["fingerprint"],
              "teams_excluded_already_started": sorted(set(kick) - still_open),
              "sources": live_prov, "rows": rows}
    written = _write_once(lpath, ledger)
    return {"ledger": str(lpath), "written": written, "rows": len(rows),
            "returners": sum(r["ret"] for r in rows),
            "injury_exits": sum((r["inputs"].get("injury_exit_last") or 0) == 1 for r in rows),
            "open_teams": len(still_open)}


# ----------------------------------------------------------------------------- freeze rule + pregame


def freeze_decision(kickoffs: dict, now: datetime) -> dict:
    """signal_shadows' freeze rule (same constants), on {team: kickoff (aware UTC) | None}. Pure."""
    import signal_shadows as ss
    return ss.freeze_decision(kickoffs, now, slate_share=SLATE_SHARE, deadline_hours=DEADLINE_HOURS)


def scheduled_kickoffs(args, season: int, week: int) -> dict:
    import signal_shadows as ss
    return ss.scheduled_kickoffs(args, season, week)


def default_predict(args, season: int, week: int) -> dict:
    args.week = week
    return cmd_predict(args)


def _record(args, rec: dict, now: datetime) -> Path:
    path = (out_dir(args) / "runs" / f"s{rec['season']}w{rec['week']:02d}-{now:%Y%m%dT%H%M%SZ}-{rec['kind']}-"
            f"{os.getpid()}-{uuid.uuid4().hex[:8]}.json")
    if args.write:
        _write_once(path, rec)
    return path


def decided(args) -> dict[str, dict]:
    out = {}
    for n in CANDIDATES:
        p = out_dir(args) / f"decision_{n}.json"
        if p.exists():
            out[n] = json.loads(p.read_text())
    return out


def run_pregame(args, season: int, week: int, *, now: datetime | None = None, kickoffs=None, predict=None) -> dict:
    """Freeze week W's pregame ledger once, when the freeze rule allows. Never raises: every outcome (including a
    failure) is returned and, unless dry, recorded write-once under runs/."""
    now = now or _utcnow()
    predict = predict or default_predict
    rec = {"schema": SCHEMA, "kind": "pregame", "candidates": list(CANDIDATES), "season": season, "week": week,
           "started_at": now.isoformat(), "dry_run": not args.write, "warnings": [], "notes": []}
    lpath = ledger_path(args, season, week)
    try:
        if (season, week) < FIRST_PROSPECTIVE:
            rec.update(status="before_prospective_start",
                       reason=f"prospective evidence starts {FIRST_PROSPECTIVE[0]} week {FIRST_PROSPECTIVE[1]}")
            return rec                                  # not recorded: nothing was attempted
        if len(decided(args)) == len(CANDIDATES):
            rec.update(status="decided", reason="every candidate has its one-look decision: nothing more to freeze")
            return rec
        if lpath.exists():
            rec.update(status="already_frozen", ledger=str(lpath), ledger_sha256=_sha_file(lpath))
            return rec                                  # idempotent: no record, nothing rewritten
        kick = kickoffs if kickoffs is not None else scheduled_kickoffs(args, season, week)
        dec = freeze_decision(kick, now)
        rec["decision"] = {k: v for k, v in dec.items() if k not in ("warnings", "notes")}
        rec["warnings"] += dec["warnings"]
        rec["notes"] += dec["notes"]
        if dec["action"] != "freeze":
            rec["status"] = "deferred" if dec["action"] == "defer" else "nothing_open"
        elif not args.write:
            rec["status"] = "would_freeze"
        else:
            res = predict(args, season, week)
            rec["predict"] = res
            path = Path(res.get("ledger") or lpath)
            rec.update(status="frozen" if res.get("written") else "already_frozen", ledger=str(path),
                       ledger_sha256=_sha_file(path) if path.exists() else None)
    except BaseException as err:      # noqa: BLE001  recorded, never raised (SystemExit: predict's refusals)
        if isinstance(err, KeyboardInterrupt):
            raise
        rec.update(status="failed", error=f"{err.__class__.__name__}: {err}")
        if BASE_MOVED in str(err):
            rec.update(status="base_moved", required_action=(
                f"ifp-2.2.0's RB/WR/TE definition changed since {SHADOW} was pre-registered ({err}). No further "
                f"ledger is frozen. Decide in {DOC}: end the shadow and judge it on the weeks frozen so far, or "
                "pre-register a new one on the new base."))
            rec["warnings"].append(f"ACTION REQUIRED: {rec['required_action']}")
        else:
            rec["warnings"].append(f"pregame shadow forecast for week {week} failed: {err}")
    rec["finished_at"] = _utcnow().isoformat()
    rec["record"] = str(_record(args, rec, now))
    return rec


def check_missed(args, season: int, week: int, *, now: datetime | None = None, kickoffs=None) -> dict | None:
    """A prospective week whose games have all kicked off without a frozen ledger is recorded write-once under
    missed/ (never backfilled). Returns the record, or None."""
    if week < 1 or (season, week) < FIRST_PROSPECTIVE or ledger_path(args, season, week).exists() \
            or len(decided(args)) == len(CANDIDATES):
        return None
    now = now or _utcnow()
    kickoffs = kickoffs if kickoffs is not None else scheduled_kickoffs(args, season, week)
    known = [k for k in kickoffs.values() if k is not None]
    if not known or max(known) > now:
        return None
    rec = {"candidates": list(CANDIDATES), "season": season, "week": week, "last_kickoff": max(known).isoformat(),
           "recorded_at": now.isoformat(),
           "reason": "no injury-return shadow ledger was frozen before the week's games; nothing is backdated"}
    if args.write:
        _write_once(out_dir(args) / "missed" / f"s{season}w{week:02d}.json", rec)
    return rec


# ----------------------------------------------------------------------------- score + the gate


def ledger_frame(ledgers: list[dict]):
    """Every ledger row as (key, gsis_id, ledger_position, pred_base, pred_<short>..., ret, injury_exit)."""
    import polars as pl

    recs = []
    for led in ledgers:
        k = led["season"] * 100 + led["week"]
        for r in led["rows"]:
            recs.append({"key": k, "gsis_id": r["gsis_id"], "ledger_position": r["position"],
                         "pred_base": r.get("base_points"),
                         **{f"pred_{SHORT[m]}": r.get(f"{SHORT[m]}_points") for m in MODELS},
                         "ret": bool(r.get("ret")),
                         "injury_exit": (r.get("inputs") or {}).get("injury_exit_last") == 1})
    schema = {"key": pl.Int64, "gsis_id": pl.String, "ledger_position": pl.String, "pred_base": pl.Float64,
              **{f"pred_{SHORT[m]}": pl.Float64 for m in MODELS}, "ret": pl.Boolean, "injury_exit": pl.Boolean}
    return pl.DataFrame(recs, schema=schema)


def _diff(d, col: str) -> dict:
    from app.analytics.forecast_backtest import week_bootstrap_mae_diff

    if d.height == 0:
        return {"n": 0, "weeks": 0, "diff": None, "ci95": None}
    r = week_bootstrap_mae_diff(d, col, "pred_base", RESAMPLES, SEED, exact=True)
    y = d["label"].to_numpy()
    # unrounded: the gate and the both-choice compare these exact values (round only when presenting them)
    return {"n": d.height, "weeks": d.select("key").n_unique(),
            "mae_base": float(abs(d["pred_base"].to_numpy() - y).mean()),
            "mae_candidate": float(abs(d[col].to_numpy() - y).mean()),
            "bias_base": float((d["pred_base"].to_numpy() - y).mean()),
            "bias_candidate": float((d[col].to_numpy() - y).mean()),
            "diff": None if r["diff"] is None else float(r["diff"]),
            "ci95": None if r["ci95"] is None else [float(x) for x in r["ci95"]]}


def cohort_expr(name: str):
    import polars as pl
    return pl.col("ret") if COHORT.get(name) == "returner" else pl.col("injury_exit")


def evaluate(p, name: str) -> dict:
    """One model on identical rows (frozen pregame, base and the model both forecast, a label): cohort, overall,
    per position, plus the play-through / returner slices (reported only)."""
    import polars as pl

    col = f"pred_{SHORT[name]}"
    d = p.filter(pl.col(col).is_not_null() & pl.col("pred_base").is_not_null() & pl.col("label").is_not_null()
                 & pl.col("position").is_in(list(POSITIONS)))
    out = {"weeks": d.select("key").n_unique(), "overall": _diff(d, col),
           "positions": {pos: _diff(d.filter(pl.col("position") == pos), col) for pos in POSITIONS},
           "slices": {"play_through": _diff(d.filter(pl.col("injury_exit") & ~pl.col("ret")), col),
                      "returner": _diff(d.filter(pl.col("ret")), col),
                      **{f"returner_{pos}": _diff(d.filter(pl.col("ret") & (pl.col("position") == pos)), col)
                         for pos in POSITIONS}}}
    if name in COHORT:
        out["cohort"] = {"name": COHORT[name], **_diff(d.filter(cohort_expr(name)), col)}
    return out


def checks(ev: dict) -> dict[str, bool]:
    c, o = ev["cohort"], ev["overall"]
    return {"cohort_ci_below_0": bool(c["ci95"] and c["ci95"][1] < 0),
            "overall_not_worse": o["diff"] is not None and o["diff"] <= 0,
            "no_position_clearly_worse": not any((v["ci95"] or [0, 0])[0] > 0 for v in ev["positions"].values())}


def look_due(latest_final_key: int | None, ev: dict) -> tuple[bool, str]:
    """(due, why): the one look happens at the first scoring with LOOK fully final and both minimums met."""
    lk = LOOK[0] * 100 + LOOK[1]
    if latest_final_key is None or latest_final_key < lk:
        return False, f"before the look ({LOOK[0]} week {LOOK[1]} not fully final)"
    if ev["weeks"] < MIN_WEEKS:
        return False, f"look deferred: {ev['weeks']} of {MIN_WEEKS} weeks scored"
    if ev["cohort"]["n"] < MIN_COHORT:
        return False, f"look deferred: {ev['cohort']['n']} of {MIN_COHORT} cohort rows"
    return True, "look"


def gate(args, ev: dict, name: str, latest_final_key: int | None, now: datetime, *, write: bool,
         both_ev: dict | None = None) -> dict:
    """The candidate's status: its frozen decision if one exists, else the one look if due (written once, with the
    `both` evidence of that same scoring for the both-choice), else accumulating with interim checks (never
    decisive)."""
    path = out_dir(args) / f"decision_{name}.json"
    if path.exists():
        d = json.loads(path.read_text())
        return {"status": d["status"], "checks": d["checks"], "weeks": d["weeks"], "decided_at": d["decided_at"],
                "decision": str(path)}
    c = checks(ev)
    due, why = look_due(latest_final_key, ev)
    if not due:
        return {"status": ACCUMULATING, "checks": c, "weeks": ev["weeks"], "note": why, "interim": True}
    status = PASS if all(c.values()) else FAIL
    rec = {"candidate": name, "status": status, "checks": c, "weeks": ev["weeks"], "decided_at": now.isoformat(),
           "evidence": ev, "both_evidence": both_ev, "latest_final_key": latest_final_key}
    if write:
        _write_once(path, rec)
    return {"status": status, "checks": c, "weeks": ev["weeks"], "decided_at": rec["decided_at"], "decision": str(path)}


def both_choice(singles: dict, both_ev: dict, statuses: dict) -> dict | None:
    """docs rule 1.6, when both candidates pass: `both` only if it meets rules 1-3 on each cohort and its overall MAE
    is no higher than the better single's; otherwise the single with the lower overall MAE. `singles`: each
    candidate's evidence; `both_ev`: the `both` evidence (with its per-cohort differences)."""
    if not all(statuses[n] == PASS for n in CANDIDATES):
        return None
    ok = all(checks({**both_ev, "cohort": both_ev["cohorts"][n]}).values() for n in CANDIDATES)
    best = min(CANDIDATES, key=lambda n: singles[n]["overall"]["mae_candidate"])
    if ok and both_ev["overall"]["mae_candidate"] <= singles[best]["overall"]["mae_candidate"]:
        return {"ship": BOTH, "added": list(EXTRA[BOTH])}
    return {"ship": best, "added": list(EXTRA[best])}


def frozen_choice(args, now: datetime, *, write: bool) -> dict | None:
    """The both-choice, decided once: when every candidate has its decision, from decision-time evidence only (each
    candidate's own decision record, and the `both` evidence saved with the later of the two decisions), written
    once to decision_both.json; every later scoring and the promotion check read that file. None before."""
    path = out_dir(args) / "decision_both.json"
    if path.exists():
        return json.loads(path.read_text())
    recs = decided(args)
    if len(recs) < len(CANDIDATES):
        return None
    last = max(recs.values(), key=lambda r: (r["decided_at"], r["candidate"]))
    choice = both_choice({n: r["evidence"] for n, r in recs.items()}, last.get("both_evidence"),
                         {n: r["status"] for n, r in recs.items()})
    rec = {"choice": choice, "decided_at": now.isoformat(), "statuses": {n: r["status"] for n, r in recs.items()},
           "both_evidence_from": last["candidate"],
           "decisions": {n: str(out_dir(args) / f"decision_{n}.json") for n in recs}}
    if write:
        _write_once(path, rec)
    return rec


def score(args, store=None, *, now: datetime | None = None, write: bool = False) -> dict:
    """Score every frozen week that is fully final (in the store with labels); apply the gate."""
    import polars as pl

    now = now or _utcnow()
    paths = sorted((out_dir(args) / "ledger").glob("s*-pregame.json"))
    if store is None:
        _, _, store, _ = build(args)
    lab = store.filter(pl.col("label").is_not_null())
    latest = int(lab["key"].max()) if lab.height else None
    lab = lab.select(pl.col("key").cast(pl.Int64), "gsis_id", "position", "label")
    ledgers = [json.loads(x.read_text()) for x in paths]
    check_ledger_bases(args, ledgers)
    p = lab.join(ledger_frame(ledgers), on=["key", "gsis_id"], how="inner")
    evs = {n: evaluate(p, n) for n in MODELS}
    both = p.filter(pl.col("pred_both").is_not_null() & pl.col("pred_base").is_not_null())
    evs[BOTH]["cohorts"] = {n: {"name": COHORT[n], **_diff(both.filter(cohort_expr(n)), "pred_both")}
                            for n in CANDIDATES}
    gates = {n: gate(args, evs[n], n, latest, now, write=write, both_ev=evs[BOTH]) for n in CANDIDATES}
    frozen = frozen_choice(args, now, write=write)
    out = {"ledgers": [x.name for x in paths], "scored_weeks": sorted(set(p["key"].to_list())),
           "latest_final_key": latest, "look": list(LOOK), "candidates": {}}
    for n in CANDIDATES:
        out["candidates"][n] = {**evs[n], "gate": gates[n]}
    out["both"] = {**evs[BOTH], "candidate": BOTH_CANDIDATE, "reported_only": True, "choice": None if frozen is None else frozen["choice"],
                   "choice_decided_at": None if frozen is None else frozen["decided_at"]}
    return out


def run_score(args, *, now: datetime | None = None, scorer=None) -> dict:
    """Score the frozen ledgers (scores.json, replaced; a due decision written once). Never raises; recorded
    write-once under runs/."""
    now = now or _utcnow()
    scorer = scorer or (lambda a: score(a, now=now, write=True))
    rec = {"schema": SCHEMA, "kind": "score", "candidates": list(CANDIDATES), "season": args.season, "week": 0,
           "started_at": now.isoformat(), "dry_run": not args.write, "warnings": [], "notes": []}
    ledgers = sorted((out_dir(args) / "ledger").glob("s*-pregame.json"))
    try:
        if not ledgers:
            rec.update(status="no_ledgers", notes=["no injury-return shadow ledger frozen yet: nothing to score"])
            return rec                                  # not recorded: nothing was attempted
        if not args.write:
            rec.update(status="would_score", ledgers=[x.name for x in ledgers])
            return rec
        res = scorer(args)
        rec["gate"] = {n: r["gate"]["status"] for n, r in res["candidates"].items()}
        rec["scored_weeks"] = len(res["scored_weeks"])
        rec["status"] = "scored"
        _replace_json(out_dir(args) / "scores.json", {"scored_at": now.isoformat(), **res})
    except BaseException as err:      # noqa: BLE001
        if isinstance(err, KeyboardInterrupt):
            raise
        rec.update(status="failed", error=f"{err.__class__.__name__}: {err}")
        rec["warnings"].append(f"scoring the injury-return shadow ledgers failed: {err}")
    rec["finished_at"] = _utcnow().isoformat()
    rec["record"] = str(_record(args, rec, now))
    return rec


# ----------------------------------------------------------------------------- commands


def _summary(rec: dict) -> dict:
    keep = ("kind", "season", "week", "status", "reason", "ledger", "ledger_sha256", "error", "record", "warnings",
            "notes", "gate", "scored_weeks", "required_action")
    out = {k: rec[k] for k in keep if k in rec}
    if "decision" in rec:
        out["decision"] = {k: rec["decision"][k] for k in ("action", "reason", "open_teams", "imminent_teams")}
    return out


def cmd_prepare(args, now: datetime) -> dict:
    _, _, store, prov = build(args)
    return {"kind": "prepare", "status": "ok", "warnings": [], "notes": [], "store_rows": store.height,
            "fingerprint": prov["fingerprint"]}


def cmd_pregame(args, now: datetime) -> dict:
    import ctx_shadow as cs
    week = args.week if args.week is not None else cs._target_week(args, now)
    if week is None:
        return {"kind": "pregame", "status": "no_upcoming_week", "warnings": [], "notes": []}
    return _summary(run_pregame(args, args.season, week, now=now))


def cmd_score(args, now: datetime) -> dict:
    return _summary(run_score(args, now=now))


def cmd_cycle(args, now: datetime) -> dict:
    """learning_cycle.py's stage: score the frozen weeks, record a missed previous week, then try the target week's
    pregame freeze (usually deferred on Tue-Thu)."""
    import ctx_shadow as cs
    week = args.week if args.week is not None else cs._target_week(args, now)
    out = {"score": _summary(run_score(args, now=now))}
    ps, pw = previous_week(args, week)
    missed = check_missed(args, ps, pw, now=now) if pw else None
    out["missed"] = missed
    out["pregame"] = (_summary(run_pregame(args, args.season, week, now=now)) if week
                      else {"status": "no_upcoming_week", "warnings": [], "notes": []})
    out["warnings"] = [*out["score"]["warnings"], *out["pregame"]["warnings"]]
    if missed:
        out["warnings"].append(f"week {missed['week']} started without a frozen injury-return ledger "
                               "(recorded as missed)")
    if out["pregame"].get("required_action"):
        out["required_action"] = out["pregame"]["required_action"]
    out["status"] = "failed" if {"failed", "base_moved"} & {out["score"]["status"], out["pregame"]["status"]} \
        else "ok"
    return out


def last_reg_week(args, season: int) -> int | None:
    from app.db.connection import connect as db_connect

    con = db_connect(args.live_db, read_only=True)
    try:
        row = con.execute("SELECT max(week) FROM nfl_schedules WHERE season = ? AND game_type = 'REG'",
                          [season]).fetchone()
    finally:
        con.close()
    return None if row is None or row[0] is None else int(row[0])


def previous_week(args, week: int | None, *, last=None) -> tuple[int, int | None]:
    """(season, week) before the target: week - 1; at week 1 the previous season's last REG week; with no target
    week left (after the season's last week has started) that last week. `last`: season -> last REG week (tests)."""
    last = last or (lambda s: last_reg_week(args, s))
    if week is None:
        return args.season, last(args.season)
    if week > 1:
        return args.season, week - 1
    return args.season - 1, last(args.season - 1)


def cmd_status(args, now: datetime) -> dict:
    d = out_dir(args)
    runs = sorted((d / "runs").glob("*.json"), key=lambda x: x.stat().st_mtime) if (d / "runs").exists() else []
    scores = d / "scores.json"
    return {"frozen_spec": (d / "frozen" / "spec.json").exists(),
            "ledgers": sorted(x.name for x in (d / "ledger").glob("s*-pregame.json")),
            "missed": sorted(x.name for x in (d / "missed").glob("*.json")),
            "decisions": {n: v["status"] for n, v in decided(args).items()},
            "latest_run": json.loads(runs[-1].read_text()) if runs else None,
            "gate": ({n: r["gate"]["status"] for n, r in json.loads(scores.read_text())["candidates"].items()}
                     if scores.exists() else None)}


def parse(argv=None):
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["prepare", "pregame", "predict", "score", "cycle", "status"])
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--week", type=int, help="default: the upcoming week (earliest with a game still to kick off)")
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    ap.add_argument("--weekly-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"),
                    help="the weekly learning output (accepted for the RESEARCH_SHADOWS contract)")
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--rebuild", action="store_true", help="rebuild the store cache")
    ap.add_argument("--now", help="evaluate the freeze rule at this ISO time; implies --dry-run")
    ap.add_argument("--dry-run", action="store_true", help="decide and print only: no forecast or record")
    args = ap.parse_args(argv)
    args.write = not (args.dry_run or args.now)
    if args.command == "predict" and args.week is None:
        ap.error("predict needs --week")
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
    cmds = {"prepare": cmd_prepare, "pregame": cmd_pregame, "score": cmd_score, "cycle": cmd_cycle,
            "status": cmd_status}
    out = cmds[args.command](args, now)
    print(json.dumps(out, indent=1, default=str))
    if args.command == "status":
        return EXIT_OK
    for w in out.get("warnings", []):
        print(f"WARNING: injury-return shadow: {w}", file=sys.stderr)
    if out.get("required_action"):
        print(f"ACTION REQUIRED: injury-return shadow: {out['required_action']}", file=sys.stderr)
    if out.get("status") == "failed":
        return EXIT_FAILED
    return EXIT_WARNINGS if out.get("warnings") else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
