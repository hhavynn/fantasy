"""Weekly learning pipeline: retrain frozen configurations on every completed week, freeze pregame
forecasts, and measure them once the games are final.

Cycle for target week W (scripts/weekly_learning.py, docs "Weekly learning and the ifp-2 challenger"):
  1. readiness   every REG game before W is final and its box scores, snaps and xFP are synced;
                 W has not kicked off (else only a clearly labeled retrospective run is possible)
  2. train       champion = ifp-1.0.0 frozen selection refit (exactly what `fit` does) and challenger
                 = frozen ifp-2.0.0 config, both on store rows with (season, week) < W. Nothing is
                 re-tuned: penalties, features and intervals come from committed configuration.
  3. predict     every nflverse ACT/INA roster player on a team playing in W; both models, the
                 trailing baselines and P(appears) from the existing participation artifact
  4. ledger      one immutable JSON file per run (never overwritten); a rerun with identical
                 inputs is a no-op, a changed input (e.g. a late injury report) adds a new run
  5. evaluate    per player, the latest run created BEFORE his kickoff by artifacts whose data
                 cutoff precedes W; retrospective runs are reported separately and never counted.
                 Each forecast row gets an explicit outcome status (`label_outcomes`): only a
                 player whose OWN game is final with complete box scores and snaps is resolved
                 (appeared, or a confirmed zero), and only fully final weeks are evidence

Artifacts are content-addressed (sha256 of the artifact without its timestamp), so retraining on
identical inputs reproduces the same id, and every artifact records its training seasons, latest
included week, data hash, configuration hash and scoring fingerprint.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

import polars as pl

from . import challenger as ch
from .forecast_backtest import (PAIR_POOL, PROMOTION_CRITERIA, STARTERS_POOL_DEFINITION, _arr, build_store,
                                completed_weeks, metrics, pair_decisions, starters_pool_expr, week_bootstrap_mae_diff)
from .forecast_features import POSITIONS, Sources, features_for_week, key
from .independent_projections import (ARTIFACT_SCHEMA, MODEL_VERSION, Artifact, baseline_predictions, fit_final,
                                      flags_for, interval_for, predict_frame)

LEDGER_SCHEMA = "forecast-ledger/v1"
MIN_XFP_COVERAGE = 0.5        # share of a completed week's appearances with nflverse expected points
CHALLENGER_STATUS = {
    pos: {"status": "shadow", "start_sit_ready": False, "validated_for": None,
          "intervals_published": pos != "QB",
          "reasons": ["research challenger in shadow mode: never read by lineups, start/sit, trades or waivers",
                      "confirmation 2023-24 passed CONFIRMATION_RULES; SHADOW_PROMOTION_CRITERIA not yet met"]
                     + (["80% interval coverage 74.0% (confirmation) is outside 75-85%: intervals withheld"]
                        if pos == "QB" else [])}
    for pos in POSITIONS}


# Outcome of one frozen forecast row (`label_outcomes`). Only the first two are labels; every other
# status is excluded from conditional AND unconditional metrics (never a zero-point outcome).
APPEARED = "appeared"                    # own game final, team data complete, player in the panel
CONFIRMED_ABSENT = "confirmed_absent"    # own game final, team data complete, player absent -> 0 points
PENDING_GAME = "pending_game"            # own game not final yet (another game of the week may be)
MISSING_INGESTION = "missing_ingestion"  # own game final but box scores/snaps not synced, or the game the
                                         # forecast saw (row kickoff) is missing from the current schedule
BYE = "bye"                              # no game scheduled for his team, at forecast time or now
UNRESOLVED_IDENTITY = "unresolved_identity"  # no/unknown gsis_id, or his box score/roster row is another team's
OUTCOME_STATUSES = (APPEARED, CONFIRMED_ABSENT, PENDING_GAME, MISSING_INGESTION, BYE, UNRESOLVED_IDENTITY)
RESOLVED = (APPEARED, CONFIRMED_ABSENT)
FINALITY_RULE = ("a player's outcome is resolved only when HIS team's game is presumed final (both scores in "
                 "nflverse schedules AND the data fetched at least 8 h after kickoff, the "
                 "schedule_intelligence rule: nflverse has no game-status field and a score may be live) AND "
                 "that team's box scores and snap counts for the week are synced; then he appeared (league "
                 "points) or is a confirmed zero. A week is prospective evidence only when every scheduled game "
                 "is final, every team's data is complete, and every game the forecasts saw is still scheduled.")


def game_presumed_final(points_for, points_against, kickoff: datetime | None,
                        fetched_at: datetime | None) -> tuple[bool, str | None]:
    """The conservative completion rule of schedule_intelligence.game_completion: nflverse schedules
    have no game-status field and do not say whether a score can be live, so a game is presumed final
    only when both scores are present AND the data was fetched at least FINAL_AFTER_KICKOFF (after a
    FETCH_MARGIN download margin) after its kickoff. `fetched_at`: start of the nflverse sync that
    wrote the schedule (None: unknown, never presumed final). Returns (final, reason if not)."""
    from .schedule_intelligence import FETCH_MARGIN, FINAL_AFTER_KICKOFF

    if points_for is None or points_against is None:
        return False, "no score"
    if kickoff is None:
        return False, "score present but kickoff unknown (it may be live)"
    if fetched_at is None:
        return False, "score present but the time it was fetched is unknown (it may be live)"
    lag = _utc(fetched_at) - FETCH_MARGIN - kickoff
    if lag < FINAL_AFTER_KICKOFF:
        return False, (f"score fetched {lag.total_seconds() / 3600:.1f} h after kickoff (< "
                       f"{FINAL_AFTER_KICKOFF.total_seconds() / 3600:.0f} h): it may be from a game in progress")
    return True, None


class NotReady(Exception):
    """Required data is missing, stale, or the target week has already started."""


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str, allow_nan=False).encode()).hexdigest()[:16]


def team_kickoffs(sources: Sources, season: int, week: int) -> dict[str, datetime]:
    from .projection_backtest import _kickoff

    out = {}
    for g in sources.games.filter((pl.col("season") == season) & (pl.col("week") == week)).iter_rows(named=True):
        ko = _kickoff(g)
        if ko is not None:
            out[g["team"]] = ko
    return out


# ----------------------------------------------------------------------------- readiness


def readiness(sources: Sources, season: int, week: int, now: datetime, fetched_at: datetime | None = None,
              con=None) -> dict:
    """Is every input for a week-W forecast present and final? Errors block training/prediction.
    `fetched_at`: start of the nflverse sync that wrote the schedule; a scored game counts as final
    only under game_presumed_final (unknown fetch time: never).
    `con`: the live database. When given, injury-retrieval freshness (injury_freshness.readiness_notes)
    is added to `warnings` and under `injury_freshness`. These are informational only: they never add an
    error, change `ok`/`pregame`, or reach a model input, injury probability or participation output."""
    errors, warnings = [], []
    now = _utc(now)
    games = sources.games.filter(pl.col("season") == season)
    before = games.filter(pl.col("week") < week)
    unfinished = before.filter(pl.col("points_for").is_null())
    if unfinished.height:
        wk = sorted(set(unfinished["week"].to_list()))
        errors.append(f"{unfinished.height // 2 or 1} game(s) before week {week} are not final (weeks {wk}); "
                      "a week enters training only when every game is final")
    live = {}
    from .projection_backtest import _kickoff
    for g in before.filter(pl.col("points_for").is_not_null() & pl.col("is_home")).iter_rows(named=True):
        ok, why = game_presumed_final(g["points_for"], g["points_against"], _kickoff(g), fetched_at)
        if not ok:
            live.setdefault(why, []).append(f"w{g['week']} {g['opponent']}@{g['team']}")
    for why, gs in live.items():
        errors.append(f"{len(gs)} scored game(s) before week {week} cannot be presumed final ({why}): "
                      f"{', '.join(gs[:6])}{' ...' if len(gs) > 6 else ''}; rerun sync_nfl.py after the games end")
    target = games.filter(pl.col("week") == week)
    if target.height == 0:
        errors.append(f"no week-{week} schedule for {season}")
    panel = sources.panel.filter((pl.col("season") == season) & (pl.col("week") < week))
    checks = {}
    for w in sorted(set(before.filter(pl.col("points_for").is_not_null())["week"].to_list())):
        played = set(before.filter((pl.col("week") == w) & pl.col("points_for").is_not_null())["team"].to_list())
        rows = panel.filter(pl.col("week") == w)
        stats = set(rows.filter(pl.col("has_stats"))["team"].to_list())
        snaps = set(rows.filter(pl.col("offense_pct").is_not_null())["team"].to_list())
        xcov = float(rows["xfpl"].is_not_null().mean()) if rows.height else 0.0
        checks[w] = {"teams_played": len(played), "teams_with_box_scores": len(played & stats),
                     "teams_with_snaps": len(played & snaps), "xfp_coverage": round(xcov, 3)}
        if played - stats:
            errors.append(f"week {w}: no nflverse box scores for {sorted(played - stats)} (run sync_nfl.py)")
        if played - snaps:
            errors.append(f"week {w}: no snap counts for {sorted(played - snaps)}")
        if xcov < MIN_XFP_COVERAGE:
            errors.append(f"week {w}: nflverse expected points cover {xcov:.0%} of appearances (< "
                          f"{MIN_XFP_COVERAGE:.0%}); ff_opportunity is probably not published yet")
    exits_through = None
    if sources.exits is not None:
        ex = sources.exits.filter(pl.col("season") == season)
        exits_through = int(ex["week"].max()) if ex.height else None
    last_done = max(checks) if checks else None
    if last_done is not None and (exits_through is None or exits_through < last_done):
        warnings.append(f"play-by-play injury evidence covers {season} through week {exits_through}; later games "
                        "are classified from injury reports only (run weekly_learning.py sync-exits)")
    if sources.injuries is None or sources.injuries.filter((pl.col("season") == season)
                                                           & (pl.col("week") == week)).height == 0:
        warnings.append(f"no nflverse week-{week} injury report synced yet (participation falls back to ESPN status)")
    kick = team_kickoffs(sources, season, week)
    first = min(kick.values()) if kick else None
    started = sorted(t for t, k in kick.items() if k <= now)
    report = {"season": season, "week": week, "checked_at": now.isoformat(), "ok": not errors, "errors": errors,
              "warnings": warnings, "completed_weeks": checks, "pbp_exits_through_week": exits_through,
              "first_kickoff": first.isoformat() if first else None,
              "pregame": bool(first and now < first), "teams_already_kicked_off": started}
    if con is not None:
        from .injury_freshness import readiness_notes
        notes = readiness_notes(con, season, week, now)
        warnings.extend(notes["warnings"])
        report["injury_freshness"] = {k: v for k, v in notes.items() if k != "warnings"}
    return report


# ----------------------------------------------------------------------------- training


def training_store(sources: Sources, season: int, week: int) -> pl.DataFrame:
    """Labeled point-in-time rows for every FINAL week strictly before (season, week)."""
    final = sources.games.filter(pl.col("points_for").is_not_null()).select("season", "week").unique()
    final_weeks = {(int(s), int(w)) for s, w in final.iter_rows()}
    all_weeks = {(int(s), int(w)) for s, w in sources.games.select("season", "week").unique().iter_rows()}
    open_weeks = {sw for sw in all_weeks if sources.games.filter(
        (pl.col("season") == sw[0]) & (pl.col("week") == sw[1]) & pl.col("points_for").is_null()).height}
    weeks = [sw for sw in completed_weeks(sources, list(range(ch.TRAIN_START, season + 1)))
             if key(*sw) < key(season, week) and sw in final_weeks and sw not in open_weeks]
    store = build_store(sources, weeks)
    if store.height:
        assert store["key"].max() < key(season, week), "training row at or after the target week"
    return store


def store_hash(store: pl.DataFrame, columns: list[str]) -> str:
    cols = ["key", "gsis_id", "label", *sorted(c for c in set(columns) if c in store.columns)]
    return hashlib.sha256(store.select(cols).sort("key", "gsis_id").write_csv().encode()).hexdigest()[:16]


def _provenance(role: str, store: pl.DataFrame, rows: dict, season: int, week: int, cfg_hash: str,
                scoring_fp: str, data_hash: str, retrospective: bool, exits_through) -> dict:
    last = int(store["key"].max()) if store.height else None
    return {"role": role, "target": [season, week], "data_cutoff_key": last,
            "latest_included_week": None if last is None else [last // 100, last % 100],
            "training_seasons": sorted(int(s) for s in store["season"].unique().to_list() if s >= ch.TRAIN_START),
            "train_start_season": ch.TRAIN_START, "rows": rows, "data_hash": data_hash, "config_hash": cfg_hash,
            "scoring_fingerprint": scoring_fp, "retrospective": retrospective,
            "pbp_exits_through_week": exits_through,
            "cutoff_rule": "labeled rows of FINAL games with (season, week) < target; features from earlier games only"}


def train(sources: Sources, season: int, week: int, *, ifp_selection: dict, ifp_report: dict,
          cfg: ch.ChallengerConfig, scoring_desc: dict, created_at: str, retrospective: bool = False,
          store: pl.DataFrame | None = None) -> tuple[Artifact, Artifact]:
    """Champion (ifp-1.0.0 frozen selection) and challenger (ifp-2.0.0 frozen config) for week W."""
    store = training_store(sources, season, week) if store is None else store
    exits_through = None
    if sources.exits is not None and sources.exits.filter(pl.col("season") == season).height:
        exits_through = int(sources.exits.filter(pl.col("season") == season)["week"].max())
    # champion: identical to `backtest_independent_projections.py fit` on this cutoff
    champ = fit_final(store, ifp_selection, ifp_report, scoring_desc, created_at, ch.TRAIN_START)
    champ_cols = [c for m in champ.models.values() for c in m.features]
    champ.config["provenance"] = _provenance(
        "champion", store, {p: m.n_train for p, m in champ.models.items()}, season, week,
        ch.config_hash(champ.config["selection"]), scoring_desc["fingerprint"], store_hash(store, champ_cols),
        retrospective, exits_through)
    # challenger
    models, rows = {}, {}
    k = key(season, week)
    for pos in POSITIONS:
        spec = cfg.spec(pos)
        tr = ch.train_rows(store, k, pos)
        if tr.height < ch.MIN_TRAIN_ROWS:
            continue
        models[pos] = ch.fit_linear(tr, pos, cfg.features(pos), spec.lam[pos], loss=spec.loss, huber_c=spec.huber_c,
                                    half_life_weeks=spec.half_life_weeks, target_key=k, name="ifp2")
        rows[pos] = tr.height
    chal_cols = [c for p in POSITIONS for c in cfg.features(p)]
    status = {p: dict(CHALLENGER_STATUS[p]) for p in POSITIONS}
    for p in POSITIONS:
        if p not in models:
            status[p] = {"status": "insufficient_data", "reasons": ["too few training rows"], "start_sit_ready": False}
    chal = Artifact(version=ch.CHALLENGER_VERSION, created_at=created_at, scoring_fingerprint=scoring_desc["fingerprint"],
                    scoring=scoring_desc,
                    config={"challenger": asdict(cfg), "config_hash": cfg.hash(),
                            "provenance": _provenance("challenger", store, rows, season, week, cfg.hash(),
                                                      scoring_desc["fingerprint"], store_hash(store, chal_cols),
                                                      retrospective, exits_through)},
                    models=models, intervals={p: cfg.intervals[p] for p in models if status[p].get("intervals_published")},
                    status=status, evaluation={"confirmation_rules": ch.CONFIRMATION_RULES,
                                               "shadow_promotion_criteria": ch.SHADOW_PROMOTION_CRITERIA})
    return champ, chal


PRODUCTION_STATUS = {
    pos: {"status": "production", "start_sit_ready": False, "validated_for": None, "intervals_published": pos != "QB",
          "reasons": ["production model by owner decision (approval kind owner_override)"]
                     + (["80% interval coverage 74.0% (ifp-2 confirmation) is outside 75-85%: intervals withheld"]
                        if pos == "QB" else [])}
    for pos in POSITIONS}


def train_production(sources: Sources, season: int, week: int, *, cfg: ch.ChallengerConfig, scoring_desc: dict,
                     created_at: str, retrospective: bool = False, store: pl.DataFrame | None = None,
                     roster_codes: pl.DataFrame | None = None, after_kickoff: bool = False) -> Artifact:
    """The production model ifp-2.1.0 for week W (app.analytics.injury_model): ifp-2.0.0's frozen spec and training
    rows plus the iwh-0.1.1 handoff features, and the return-rate table fitted on episodes before W. Nothing is
    tuned. `after_kickoff`: the week already kicked off but this is not a replay (a model introduced mid-week, trained
    on exactly the pregame cutoff data); recorded in provenance, and every evidence reader excludes its runs for W
    (artifact fitted after the week's first kickoff)."""
    from . import injury_model as im

    store = training_store(sources, season, week) if store is None else store
    store = im.with_training_handoff(sources, store, roster_codes)
    exits_through = None
    if sources.exits is not None and sources.exits.filter(pl.col("season") == season).height:
        exits_through = int(sources.exits.filter(pl.col("season") == season)["week"].max())
    models, rows = im.fit(store, season, week, cfg)
    cells = im.fit_return_table(sources, season, week)
    cols = [c for p in POSITIONS for c in im.features(cfg, p)]
    status = {p: dict(PRODUCTION_STATUS[p]) for p in POSITIONS}
    for p in POSITIONS:
        if p not in models:
            status[p] = {"status": "insufficient_data", "reasons": ["too few training rows"], "start_sit_ready": False}
    prov = _provenance("production", store, rows, season, week, im.config_hash(cfg), scoring_desc["fingerprint"],
                       store_hash(store, cols), retrospective, exits_through)
    prov.update(handoff_hash=im.handoff_hash(store), trained_after_first_kickoff=bool(after_kickoff),
                roster_codes=roster_codes is not None)
    return Artifact(version=im.MODEL_VERSION, created_at=created_at, scoring_fingerprint=scoring_desc["fingerprint"],
                    scoring=scoring_desc,
                    config={im.CONFIG_KEY: im.artifact_config(cfg, cells), "config_hash": im.config_hash(cfg),
                            "provenance": prov},
                    models=models, intervals={p: cfg.intervals[p] for p in models if status[p].get("intervals_published")},
                    status=status, evaluation={"return_rates": im.return_table_summary(cells),
                                               "research": "docs/research/injury_workload_handoff.md"})


def train_context(sources: Sources, season: int, week: int, *, cfg: ch.ChallengerConfig, scoring_desc: dict,
                  created_at: str, inputs, participation_map: dict | None, participation_note: str = "",
                  retrospective: bool = False, store: pl.DataFrame | None = None,
                  roster_codes: pl.DataFrame | None = None, after_kickoff: bool = False) -> Artifact:
    """The production model ifp-2.2.0 for week W (app.analytics.context_model): ifp-2.1.0 whole (training rows,
    handoff features, return-rate table) plus the shipped research-shadow components: their features, the QB start
    model fitted on team-weeks before W, and the frozen QB P(plays) map. Nothing is tuned. `inputs`:
    context_model.Inputs (ContextSources and depth snapshots); `after_kickoff` as in train_production."""
    from . import context_model as cm
    from . import injury_model as im

    store = training_store(sources, season, week) if store is None else store
    store = cm.with_training_inputs(sources, store, inputs, roster_codes)
    exits_through = None
    if sources.exits is not None and sources.exits.filter(pl.col("season") == season).height:
        exits_through = int(sources.exits.filter(pl.col("season") == season)["week"].max())
    models, rows, fallback = cm.fit(store, season, week, cfg)
    start = None
    if cm.uses_scenarios("QB"):
        weeks = sorted({(int(s), int(w)) for s, w in store.select("season", "week").unique().iter_rows()})
        start = cm.fit_start(cm.start_candidates(sources, inputs, [sw for sw in weeks if key(*sw) < key(season, week)]),
                             season, week)
    cells = im.fit_return_table(sources, season, week)
    cols = [c for p in POSITIONS for c in cm.features(cfg, p)]
    status = {p: dict(PRODUCTION_STATUS[p]) for p in POSITIONS}
    for p in POSITIONS:
        if p not in models:
            status[p] = {"status": "insufficient_data", "reasons": ["too few training rows"], "start_sit_ready": False}
    prov = _provenance("production", store, rows, season, week, cm.config_hash(cfg), scoring_desc["fingerprint"],
                       store_hash(store, cols), retrospective, exits_through)
    prov.update(handoff_hash=im.handoff_hash(store), inputs_hash=cm.inputs_hash(store),
                trained_after_first_kickoff=bool(after_kickoff), roster_codes=roster_codes is not None,
                depth_snapshots=inputs.depth is not None)
    pmap = participation_map if cm.QB_PARTICIPATION in cm.SHIPPED[cm.CAL_POSITION] else None
    ctx_cfg = cm.artifact_config(cfg, start_model=start, fallback=fallback, participation_map=pmap,
                                 participation_note=participation_note, depth_note=inputs.depth_note)
    return Artifact(version=cm.MODEL_VERSION, created_at=created_at, scoring_fingerprint=scoring_desc["fingerprint"],
                    scoring=scoring_desc,
                    config={im.CONFIG_KEY: im.artifact_config(cfg, cells), cm.CONFIG_KEY: ctx_cfg,
                            "config_hash": cm.config_hash(cfg), "provenance": prov},
                    models=models, intervals={p: cfg.intervals[p] for p in models if status[p].get("intervals_published")},
                    status=status, evaluation={"return_rates": im.return_table_summary(cells),
                                               "research": "docs/research/ifp22_promotion.md"})


def artifact_id(art: Artifact) -> str:
    d = art.to_dict()
    d.pop("created_at", None)
    return _sha(ch.round_floats(d, 10))


def write_once(path: Path, text: str) -> bool:
    """Create `path` with `text`, never overwriting: the text goes to a temporary file in the same
    directory and is hard-linked into place, so an interrupted write never leaves a partial file at
    `path` and a retry starts clean. Returns False if `path` already exists."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".partial")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        return True
    finally:
        os.unlink(tmp)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as err:
        raise NotReady(f"{path} is unreadable ({err.__class__.__name__}); it was probably left by an interrupted "
                       "write of an older version. Move it out of the directory (never edit it) and rerun.") from err


def save_artifact(art: Artifact, root: Path) -> tuple[str, Path, bool]:
    """Content-addressed, write-once. Returns (id, path, created). Same content -> same file."""
    aid = artifact_id(art)
    s, w = art.config["provenance"]["target"]
    path = Path(root) / "artifacts" / art.version / f"s{s}w{w:02d}-{aid}.json"
    if path.exists():
        _read_json(path)
        return aid, path, False
    created = write_once(path, json.dumps(art.to_dict(), indent=1, default=str, allow_nan=False))
    return aid, path, created


def register_saved_artifact(con, art: Artifact, path: Path, root: Path, source: str = "learning_cycle") -> tuple[str, bool]:
    """Register the artifact stored at `path` as approved for its model version in the live database
    (approved_artifacts, write-once). Only an artifact saved through `save_artifact` qualifies: `path` must be
    the content-addressed file `root/artifacts/<version>/s<season>w<week>-<artifact_id>.json` of exactly this
    artifact. Returns (sha256 of the file bytes, created); that sha is what `project` records as the run's
    `artifact_id`. (The file name carries `artifact_id(art)`, a digest of the artifact content without its
    timestamp; the registered sha is of the file bytes, the digest the model registry pins.)"""
    from .independent_projection_store import register_approved_artifact

    path = Path(path)
    if (art.config.get("provenance") or {}).get("retrospective"):
        raise NotReady(f"{path.name} is a retrospective replay; only pregame artifacts can be approved")
    s, w = art.config["provenance"]["target"]
    expected = Path(root) / "artifacts" / art.version / f"s{s}w{w:02d}-{artifact_id(art)}.json"
    if path.resolve() != expected.resolve() or not path.is_file():
        raise NotReady(f"{path} is not the write-once store file of this artifact ({expected}); only artifacts "
                       "saved through save_artifact can be registered as approved")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    created = register_approved_artifact(
        con, model_version=art.version, artifact_sha256=sha, target_season=int(s), target_week=int(w),
        training_cutoff_key=art.config["provenance"].get("data_cutoff_key"), artifact_created_at=str(art.created_at),
        source=source)
    return sha, created


def backfill_store_artifacts(con, root: Path, version: str, scoring_fp: str | None = None,
                             source: str = "learning_cycle_backfill", skip: tuple = ()) -> dict:
    """Register every artifact of `version` already present in the local write-once store
    (`root/artifacts/<version>/s<season>w<week>-<artifact_id>.json`), so the first cycle after the approved_artifacts
    table appears covers the runs those artifacts already made. Only store files qualify: the file must load
    and pass the artifact checks (schema, scoring when given, data cutoff before its target week, not a
    retrospective replay), be of `version`, and be exactly the content-addressed store file of the artifact it
    holds (register_saved_artifact). Anything else is skipped and listed, never registered. Idempotent.
    `skip`: store files the caller registers itself (with its own source).
    Returns {registered, existing, skipped: [(file name, why)]}."""
    out = {"registered": 0, "existing": 0, "skipped": []}
    for f in sorted((Path(root) / "artifacts" / version).glob("s*w*-*.json")):
        if f.resolve() in {Path(p).resolve() for p in skip}:
            continue
        try:
            art = load_artifact(f)
            prov = art.config.get("provenance") or {}
            s, w = prov["target"]
            if art.version != version:
                raise NotReady(f"holds a {art.version} artifact")
            check_artifact(art, int(s), int(w), scoring_fp if scoring_fp is not None else art.scoring_fingerprint)
            _, created = register_saved_artifact(con, art, f, root, source)
        except (NotReady, ValueError, KeyError, TypeError) as err:
            out["skipped"].append((f.name, str(err)[:120]))
            continue
        out["registered" if created else "existing"] += 1
    return out


def load_artifact(path: Path) -> Artifact:
    return Artifact.from_dict(_read_json(path))


def latest_artifact(root: Path, version: str, season: int, week: int, scoring_fp: str, *,
                    artifact_week: int | None = None) -> tuple[Artifact, str, Path]:
    """The most recently created artifact for (version, season, week); refuses a scoring mismatch
    or an artifact whose data cutoff is not strictly before the target week.

    `artifact_week` (rest-of-season projections): the week W whose artifact makes a LATER target week T = `week`.
    The files searched are W's (s{season}wWW-*.json); the cutoff rule is checked against T and also against W
    itself (the artifact must be one W's own training could have produced). The default (None) is the artifact
    of the target week, as before."""
    src_week = week if artifact_week is None else artifact_week
    if src_week > week:
        raise NotReady(f"the artifact of week {src_week} cannot project the earlier week {week}")
    files = sorted((Path(root) / "artifacts" / version).glob(f"s{season}w{src_week:02d}-*.json"))
    if not files:
        raise NotReady(f"no {version} artifact for {season} week {src_week}; run train --week {src_week} first")
    arts = [(load_artifact(f), f) for f in files]
    art, path = max(arts, key=lambda t: t[0].created_at)
    check_artifact(art, season, week, scoring_fp)
    if src_week != week:
        check_artifact(art, season, src_week, scoring_fp)
    return art, path.stem.split("-", 1)[1], path


def check_artifact(art: Artifact, season: int, week: int, scoring_fp: str) -> None:
    if art.schema != ARTIFACT_SCHEMA:
        raise NotReady(f"artifact schema {art.schema!r} is not {ARTIFACT_SCHEMA!r}")
    if art.scoring_fingerprint != scoring_fp:
        raise NotReady(f"{art.version} artifact was trained under scoring {art.scoring_fingerprint}, league scoring "
                       f"is {scoring_fp}")
    prov = art.config.get("provenance") or {}
    cut = prov.get("data_cutoff_key")
    if cut is None or cut >= key(season, week):
        raise NotReady(f"{art.version} artifact data cutoff {cut} is not before {season} week {week}")


# ----------------------------------------------------------------------------- prediction ledger


def predict(sources: Sources, champion: Artifact, challenger: Artifact, season: int, week: int, now: datetime, *,
            artifact_ids: dict, participation=None, con=None, kind: str = "pregame",
            readiness_report: dict | None = None, participation_sources: tuple | None = None,
            sources_manifest: dict | None = None, report_times: dict | None = None) -> dict:
    """One ledger run: every ACT/INA roster player on a team playing in W (no ESPN inputs).
    `participation_sources`: (Sources, fallback teams) for P(appears), as captured; otherwise the
    ESPN-status fallback is computed from `con`. `sources_manifest`: the source_provenance snapshot
    the run used (part of the run's inputs). `report_times`: injury-report timestamp coverage."""
    from .participation import live_population, predict_week
    from .source_provenance import injury_evidence

    now = _utc(now)
    pop = live_population(sources, season, week)
    feats = features_for_week(sources, season, week, pop) if pop.height else pl.DataFrame()
    rows = []
    if feats.height:
        base = {r["gsis_id"]: r for r in baseline_predictions(feats).select("gsis_id", "pred_A1", "pred_A2")
                .iter_rows(named=True)}
        champ = {r["gsis_id"]: r for r in predict_frame(champion, feats)}
        chal = {r["gsis_id"]: r for r in predict_frame(challenger, feats)}
        parts = {}
        if participation is not None:
            psources, fallback = sources, set()
            if participation_sources is not None:
                psources, fallback = participation_sources
            elif con is not None:
                from .independent_projection_store import espn_designation_fallback
                psources, fallback = espn_designation_fallback(con, sources, season, week)
            parts = predict_week(psources, participation, season, week, fallback_teams=fallback)
        evidence = injury_evidence(sources, season, week, feats["gsis_id"].to_list(), report_times)
        kick = team_kickoffs(sources, season, week)
        for f in feats.sort("gsis_id").iter_rows(named=True):
            g = f["gsis_id"]
            c, x, q = champ.get(g, {}), chal.get(g, {}), parts.get(g)
            ko = kick.get(f["team"])
            p_play = None if q is None else q["p_play"]

            def block(r, p_play=p_play):
                pts = r.get("model_points")
                return {"points": pts, "low": r.get("interval_low"), "high": r.get("interval_high"),
                        "expected_points": None if pts is None or p_play is None else round(pts * p_play, 2)}

            rows.append({
                "gsis_id": g, "position": f["position"], "team": f["team"], "opponent": f["opponent"],
                "kickoff": ko.isoformat() if ko else None, "pregame": bool(ko and now < ko),
                "champion": block(c), "challenger": {**block(x), "explanation": (x.get("explanation") or [])[:4]},
                "pred_A1": base[g]["pred_A1"], "pred_A2": base[g]["pred_A2"], "p_play": p_play,
                "workload": {k: f.get(k) for k in ("games_recent", "h_games_recent", "inj_exits_recent",
                                                   "last_injury_exit", "last_role_low", "last_blowout_low",
                                                   "xfpl_eb", "cur_h_games", "prev_h_games", "prev_xfpl_pg",
                                                   "team_changed", "prev_team_changed")},
                "flags": flags_for(f), "injury_evidence": evidence.get(g)})
    content = {"season": season, "week": week, "kind": kind, "artifacts": artifact_ids,
               "participation": None if participation is None else participation.version,
               "rows": [{k: v for k, v in r.items() if k != "pregame"} for r in rows]}
    if sources_manifest is not None:
        content["sources"] = sources_manifest
    inputs_hash = _sha(ch.round_floats(content, 8))
    return {"schema": LEDGER_SCHEMA, "run_id": f"{kind}-s{season}w{week:02d}-{now:%Y%m%dT%H%M%S}-{inputs_hash[:8]}",
            "created_at": now.isoformat(), "inputs_hash": inputs_hash, "readiness": readiness_report,
            **content, "rows": rows}


def participation_sources_from(sources: Sources, extra: dict, manifest: dict, season: int, week: int):
    """Rebuild the (Sources, fallback teams) P(appears) used, from a capture. With an ESPN-status
    fallback the captured week-W designations replace the nflverse ones, exactly as
    independent_projection_store.espn_designation_fallback built them (it carries no exits)."""
    from .source_provenance import PARTICIPATION_INJURIES

    teams = set(manifest.get("meta", {}).get("espn_status_fallback_teams") or [])
    if not teams or PARTICIPATION_INJURIES not in extra:
        return sources, set()
    keep = sources.injuries.filter(~((pl.col("season") == season) & (pl.col("week") == week)))
    inj = pl.concat([keep, extra[PARTICIPATION_INJURIES].cast(keep.schema, strict=False).select(keep.columns)])
    return Sources(sources.panel, sources.games, sources.defense, sources.rosters, inj), teams


def rebuild_run(root: Path, run: dict, champion: Artifact, challenger: Artifact, participation=None) -> dict:
    """Recompute a ledger run from its persisted source snapshots and its artifacts only (never
    the current database). Equal rows and inputs_hash prove what the forecast knew."""
    from . import source_provenance as sp

    info = run.get("sources")
    if not info:
        raise NotReady(f"run {run['run_id']} has no source snapshot (made before provenance capture); "
                       "it cannot be rebuilt exactly")
    manifest = sp.load_manifest(root, run["season"], run["week"], info["manifest_id"])
    sources, extra = sp.reconstruct(root, manifest)
    psrc = participation_sources_from(sources, extra, manifest, run["season"], run["week"]) \
        if participation is not None else None
    return predict(sources, champion, challenger, run["season"], run["week"], datetime.fromisoformat(run["created_at"]),
                   artifact_ids=run["artifacts"], participation=participation, kind=run["kind"],
                   readiness_report=run.get("readiness"), participation_sources=psrc, sources_manifest=info,
                   report_times=manifest.get("meta", {}).get("injury_report_timestamps"))


def write_run(run: dict, root: Path) -> tuple[Path, bool]:
    """Write-once. A run whose inputs_hash already exists for that week is not written again."""
    d = Path(root) / "ledger" / f"s{run['season']}w{run['week']:02d}"
    d.mkdir(parents=True, exist_ok=True)
    for f in sorted(d.glob("*.json")):
        old = _read_json(f)
        if old.get("inputs_hash") == run["inputs_hash"] and old.get("kind") == run["kind"]:
            return f, False
    path = d / f"{run['run_id']}.json"
    if not write_once(path, json.dumps(run, indent=1, default=str, allow_nan=False)):
        raise FileExistsError(f"ledger run {path} already exists with different inputs")
    return path, True


def load_ledger(root: Path) -> list[dict]:
    return [_read_json(f) for f in sorted((Path(root) / "ledger").glob("s*/*.json"))]


# ----------------------------------------------------------------------------- prospective evaluation


def report_subject(runs: list[dict], kind: str, scoring_fp: str | None) -> dict:
    """Which champion and challenger versions the `kind` runs of the ledger actually used, read
    from each run's recorded artifacts (never from the current constants). A ledger mixing
    versions, or a run without artifact identities, gives versions None (the promotion gate then
    refuses the report as evidence for any candidate)."""
    used = [r for r in runs if r.get("kind") == kind]
    out = {"kind": kind, "scoring_fingerprint": scoring_fp, "runs": len(used)}
    for role in ("champion", "challenger"):
        vs = sorted({str(((r.get("artifacts") or {}).get(role) or {}).get("version")) for r in used})
        out[f"{role}_versions_seen"] = vs
        out[f"{role}_version"] = vs[0] if len(vs) == 1 and vs[0] != "None" else None
    return out


def frozen_rows(runs: list[dict], kind: str = "pregame") -> pl.DataFrame:
    """Per player-week, the latest row created strictly before his kickoff by a run of `kind`
    (pregame runs only count rows flagged pregame at creation)."""
    best: dict[tuple, tuple] = {}
    for run in runs:
        if run.get("kind") != kind:
            continue
        created = datetime.fromisoformat(run["created_at"])
        for r in run["rows"]:
            if kind == "pregame" and not r.get("pregame"):
                continue
            if kind == "pregame" and r.get("kickoff") and created >= datetime.fromisoformat(r["kickoff"]):
                continue
            k = (run["season"], run["week"], r["gsis_id"])
            if k not in best or created > best[k][0]:
                best[k] = (created, run["run_id"], r)
    recs = []
    for (s, w, g), (_, rid, r) in best.items():
        recs.append({"season": s, "week": w, "key": key(s, w), "gsis_id": g, "position": r["position"],
                     "team": r.get("team"), "kickoff": r.get("kickoff"), "run_id": rid,
                     "pred_champion": r["champion"]["points"], "pred_challenger": r["challenger"]["points"],
                     "low_champion": r["champion"]["low"], "high_champion": r["champion"]["high"],
                     "low_challenger": r["challenger"]["low"], "high_challenger": r["challenger"]["high"],
                     "exp_champion": r["champion"]["expected_points"], "exp_challenger": r["challenger"]["expected_points"],
                     "pred_A1": r["pred_A1"], "pred_A2": r["pred_A2"], "p_play": r["p_play"],
                     "last_injury_exit": (r.get("workload") or {}).get("last_injury_exit")})
    schema = {"season": pl.Int64, "week": pl.Int64, "key": pl.Int64, "gsis_id": pl.String, "position": pl.String,
              "team": pl.String, "kickoff": pl.String, "run_id": pl.String,
              **{c: pl.Float64 for c in ("pred_champion", "pred_challenger", "low_champion", "high_champion",
                                         "low_challenger", "high_challenger", "exp_champion", "exp_challenger",
                                         "pred_A1", "pred_A2", "p_play")}, "last_injury_exit": pl.Int64}
    return pl.DataFrame(recs, schema=schema) if recs else pl.DataFrame(schema=schema)


def team_game_finality(sources: Sources, season: int, week: int, fetched_at: datetime | None = None) -> pl.DataFrame:
    """One row per team SCHEDULED in (season, week): is its game presumed final (game_presumed_final,
    `fetched_at` = when the schedule was fetched), and are its box scores and snap counts synced? A
    team is `complete` only when all three hold. The game of one team says nothing about another's:
    a final Thursday game never makes a Monday game final."""
    from .projection_backtest import _kickoff

    games = sources.games.filter((pl.col("season") == season) & (pl.col("week") == week))
    panel = sources.panel.filter((pl.col("season") == season) & (pl.col("week") == week))
    stats = set(panel.filter(pl.col("has_stats"))["team"].to_list())
    snaps = set(panel.filter(pl.col("offense_pct").is_not_null())["team"].to_list())
    recs = []
    for g in games.iter_rows(named=True):
        final, _ = game_presumed_final(g["points_for"], g["points_against"], _kickoff(g), fetched_at)
        box, snp = g["team"] in stats, g["team"] in snaps
        recs.append({"season": season, "week": week, "team": g["team"], "final": final, "has_box_scores": box,
                     "has_snaps": snp, "complete": final and box and snp})
    schema = {"season": pl.Int64, "week": pl.Int64, "team": pl.String, "final": pl.Boolean,
              "has_box_scores": pl.Boolean, "has_snaps": pl.Boolean, "complete": pl.Boolean}
    return pl.DataFrame(recs, schema=schema).unique("team", keep="first") if recs else pl.DataFrame(schema=schema)


def week_finality(sources: Sources, season: int, week: int, fetched_at: datetime | None = None) -> dict:
    """Is the whole NFL week final? Only such weeks count toward promotion evidence."""
    t = team_game_finality(sources, season, week, fetched_at)
    pending = sorted(t.filter(~pl.col("final"))["team"].to_list())
    missing = sorted(t.filter(pl.col("final") & ~pl.col("complete"))["team"].to_list())
    return {"scheduled_teams": t.height, "final_teams": int(t["final"].sum()) if t.height else 0,
            "complete_teams": int(t["complete"].sum()) if t.height else 0,
            "pending_teams": pending, "missing_ingestion_teams": missing,
            "fully_final": t.height > 0 and not pending and not missing}


def label_outcomes(rows: pl.DataFrame, sources: Sources, fetched_at: dict | None = None) -> pl.DataFrame:
    """Attach `outcome_status` (OUTCOME_STATUSES) and `label` to frozen forecast rows. `label` is
    league points for APPEARED, 0.0 for CONFIRMED_ABSENT and null for every unresolved status, so
    an unfinished, unsynced or unmatched player can never become a zero-point outcome. A team the
    forecast saw scheduled (row kickoff) but the current schedule lacks is MISSING_INGESTION, never a
    bye. `fetched_at`: {season: when its schedule was fetched} (see game_presumed_final)."""
    if rows.height == 0:
        return rows.with_columns(pl.lit(None, pl.String).alias("outcome_status"), pl.lit(None, pl.Float64).alias("label"))
    weeks = rows.select("season", "week").unique().sort("season", "week")
    fin = pl.concat([team_game_finality(sources, int(s), int(w), (fetched_at or {}).get(int(s)))
                     for s, w in weeks.iter_rows()])
    panel = (sources.panel.join(weeks, on=["season", "week"], how="semi")
             .select("season", "week", "gsis_id", pl.col("team").alias("_panel_team"),
                     pl.col("league_points").alias("_points"))
             .unique(["season", "week", "gsis_id"], keep="first"))
    j = (rows.join(fin.select("season", "week", "team", "final", "complete"), on=["season", "week", "team"], how="left")
         .join(panel, on=["season", "week", "gsis_id"], how="left"))
    known, other_team = pl.lit(True), pl.col("_panel_team").is_not_null() & (pl.col("_panel_team") != pl.col("team"))
    if sources.rosters is not None:
        ids = sources.rosters.select("season", "gsis_id").unique()
        j = j.join(ids.with_columns(pl.lit(True).alias("_on_roster")), on=["season", "gsis_id"], how="left")
        wk = (sources.rosters.join(weeks, on=["season", "week"], how="semi")
              .select("season", "week", "gsis_id", pl.col("team").alias("_roster_team"))
              .unique(["season", "week", "gsis_id"], keep="first"))
        j = j.join(wk, on=["season", "week", "gsis_id"], how="left")
        known = pl.col("_on_roster").fill_null(False) | pl.col("_panel_team").is_not_null()
        # the week's roster puts him on another team (e.g. traded after the forecast): not his game
        other_team = other_team | (pl.col("_panel_team").is_null() & pl.col("_roster_team").is_not_null()
                                   & (pl.col("_roster_team") != pl.col("team")))
    no_id = pl.col("gsis_id").is_null() | (pl.col("gsis_id").str.strip_chars() == "") | pl.col("team").is_null()
    status = (pl.when(no_id | ~known).then(pl.lit(UNRESOLVED_IDENTITY))
              .when(pl.col("final").is_null() & pl.col("kickoff").is_not_null()).then(pl.lit(MISSING_INGESTION))
              .when(pl.col("final").is_null()).then(pl.lit(BYE))
              .when(~pl.col("final")).then(pl.lit(PENDING_GAME))
              .when(other_team).then(pl.lit(UNRESOLVED_IDENTITY))
              .when(~pl.col("complete")).then(pl.lit(MISSING_INGESTION))
              .when(pl.col("_panel_team").is_not_null()).then(pl.lit(APPEARED))
              .otherwise(pl.lit(CONFIRMED_ABSENT)))
    j = j.with_columns(status.alias("outcome_status"))
    j = j.with_columns(pl.when(pl.col("outcome_status") == APPEARED).then(pl.col("_points"))
                       .when(pl.col("outcome_status") == CONFIRMED_ABSENT).then(pl.lit(0.0))
                       .otherwise(pl.lit(None, pl.Float64)).alias("label"))
    tmp = ("final", "complete", "_panel_team", "_points", "_on_roster", "_roster_team")
    return j.drop([c for c in tmp if c in j.columns])


def _status_counts(df: pl.DataFrame) -> dict:
    counts = dict(df.group_by("outcome_status").len().iter_rows()) if df.height else {}
    return {s: int(counts.get(s, 0)) for s in OUTCOME_STATUSES}


def _position_reports(j: pl.DataFrame, seed: int, *, bootstrap: bool = True) -> dict:
    """Conditional (APPEARED) and unconditional (APPEARED + CONFIRMED_ABSENT) metrics per position.
    `j` must come from label_outcomes; unresolved rows are dropped here, never zero-filled. The
    starters pool is ranked over ALL frozen pregame rows of a week (who appears is not known
    pregame), so an inactive top player is never backfilled by a lower-ranked one."""
    j = j.with_columns(starters_pool_expr().alias("_starter"))
    cond = j.filter(pl.col("outcome_status") == APPEARED)
    resolved = j.filter(pl.col("outcome_status").is_in(RESOLVED))
    assert resolved["label"].null_count() == 0, "a resolved outcome without a label"
    out = {}
    for pos in [*POSITIONS, "ALL"]:
        g = cond if pos == "ALL" else cond.filter(pl.col("position") == pos)
        g = g.filter(pl.col("pred_champion").is_not_null() & pl.col("pred_challenger").is_not_null())
        a = _arr(g, "label")
        rep = {"n": g.height, "metrics": {m: metrics(a, _arr(g, f"pred_{m}"))
                                          for m in ("champion", "challenger", "A1", "A2")}}
        if bootstrap and g.select("key").n_unique() >= 2:
            rep["challenger_minus_champion_mae"] = week_bootstrap_mae_diff(g, "pred_challenger", "pred_champion",
                                                                           1000, seed)
        for m in ("champion", "challenger"):
            iv = g.filter(pl.col(f"low_{m}").is_not_null())
            if iv.height:
                hit = iv.filter((pl.col("label") >= pl.col(f"low_{m}")) & (pl.col("label") <= pl.col(f"high_{m}"))).height
                rep.setdefault("interval80_coverage", {})[m] = round(hit / iv.height, 4)
        sp = g.filter(pl.col("_starter"))
        rep["starters_pool"] = {"n": sp.height, "definition": STARTERS_POOL_DEFINITION,
                                "ranked_among": "every frozen pregame forecast of the week, before appearance "
                                                "is known; MAE on the pool members who appeared",
                                "mae": {m: metrics(_arr(sp, "label"), _arr(sp, f"pred_{m}"))["mae"]
                                        for m in ("champion", "challenger")}}
        ie = g.filter(pl.col("last_injury_exit") == 1)
        rep["after_injury_exit"] = {"n": ie.height, **{m: metrics(_arr(ie, "label"), _arr(ie, f"pred_{m}"))["mae"]
                                                         for m in ("champion", "challenger")}}
        u = resolved.filter(pl.col("exp_champion").is_not_null() & pl.col("exp_challenger").is_not_null())
        u = u if pos == "ALL" else u.filter(pl.col("position") == pos)
        ua = _arr(u, "label")
        rep["unconditional"] = {"n": u.height, **{m: metrics(ua, _arr(u, f"exp_{m}")) for m in ("champion", "challenger")}}
        out[pos] = rep
    return out


def evaluate(runs: list[dict], sources: Sources, kind: str = "pregame", seed: int = 20261014,
             fetched_at: dict | None = None) -> dict:
    """Frozen forecasts vs finalized outcomes (FINALITY_RULE). Conditional: players who appeared
    (label = league points). Unconditional: appeared + confirmed absences (0). Rows whose own game
    is not final, whose team data is not synced, on a bye, or with an unresolved identity are
    excluded and counted in `outcomes`. Only fully final weeks are evidence (`weeks`, metrics,
    shadow_status); resolved rows of partly final weeks are reported under `preliminary` only.
    `fetched_at`: {season: when its nflverse schedule was fetched}; without it no game is presumed
    final (game_presumed_final), so nothing is evidence."""
    rows = label_outcomes(frozen_rows(runs, kind), sources, fetched_at)
    wk = sorted({(int(s), int(w)) for s, w in rows.select("season", "week").unique().iter_rows()}) if rows.height else []
    fin = {sw: week_finality(sources, *sw, (fetched_at or {}).get(sw[0])) for sw in wk}
    for (s, w), f in fin.items():
        # a game the forecasts saw that the schedule no longer lists keeps the week open (never a bye)
        gone = rows.filter((pl.col("season") == s) & (pl.col("week") == w) & pl.col("kickoff").is_not_null()
                           & (pl.col("outcome_status") == MISSING_INGESTION))["team"].unique().to_list()
        f["forecast_teams_missing_from_schedule"] = sorted(t for t in gone if t not in f["missing_ingestion_teams"])
        f["fully_final"] = f["fully_final"] and not f["forecast_teams_missing_from_schedule"]
    final_weeks = [sw for sw in wk if fin[sw]["fully_final"]]
    in_final = pl.col("key").is_in([key(s, w) for s, w in final_weeks])
    evidence, partial = rows.filter(in_final), rows.filter(~in_final)
    out = {"kind": kind, "weeks": final_weeks, "finality_rule": FINALITY_RULE,
           "finality": {f"{s}-{w:02d}": fin[(s, w)] for s, w in wk},
           "outcomes": {"evidence": _status_counts(evidence), "not_yet_evidence": _status_counts(partial)},
           "positions": {}}
    prelim_weeks = [sw for sw in wk if sw not in final_weeks]
    if prelim_weeks:
        resolved = partial.filter(pl.col("outcome_status").is_in(RESOLVED))
        out["preliminary"] = {
            "note": "PRELIMINARY: weeks that are not fully final. Never counted as prospective weeks and never "
                    "used by shadow_status or any promotion decision; unresolved players are excluded, not zero.",
            "weeks": prelim_weeks, "n_resolved": resolved.height, "n_unresolved": partial.height - resolved.height,
            "positions": _position_reports(partial, seed, bootstrap=False) if resolved.height else {}}
    if not final_weeks:
        out["note"] = "no frozen forecast is in a fully final week yet"
        out["shadow_status"] = shadow_status(out)
        out["promotion_blockers"] = promotion_blockers(out["shadow_status"])
        return out
    out["positions"] = _position_reports(evidence, seed)
    cond = evidence.filter(pl.col("outcome_status") == APPEARED)
    both = cond.filter(pl.col("pred_champion").is_not_null() & pl.col("pred_challenger").is_not_null()
                       & pl.col("pred_A1").is_not_null())
    if both.height:
        out["pairs"] = pair_decisions(both, ["pred_champion", "pred_challenger"], pool=PAIR_POOL)
    out["shadow_status"] = shadow_status(out)
    out["promotion_blockers"] = promotion_blockers(out["shadow_status"])
    return out


PASS, FAIL, INSUFFICIENT = "pass", "fail", "insufficient_data"
# Readiness (from the evidence) and lifecycle (what consumers read) are separate. Evaluation can
# reach `eligible_for_review` at most; nothing here changes the lifecycle of any model.
READINESS = ("insufficient_evidence", "failed_evaluation", "eligible_for_review")
LIFECYCLE = {
    "research_shadow": "trained and forecast weekly, never read by lineups, start/sit, trades, waivers, matchups "
                       "or the website (ifp-2.0.0 in every position)",
    "approved_production": "explicitly approved by the user AND wired into consumers by a reviewed code change; "
                           "never set by an evaluation",
}
SAMPLE_CRITERIA = ("frozen_pregame_evidence", "prospective_weeks", "matched_appearances")


def _criterion(name: str, observed, required, result: str, explanation: str) -> dict:
    return {"name": name, "observed": observed, "required": required, "result": result, "explanation": explanation}


def shadow_criteria(report: dict, pos: str) -> list[dict]:
    """Every SHADOW_PROMOTION_CRITERIA condition for one position as an auditable result. Missing
    evidence is `insufficient_data`, never a pass."""
    g = ch.SHADOW_GATE
    r = (report.get("positions") or {}).get(pos) or {}
    kind, weeks, n = report.get("kind"), len(report.get("weeks") or []), r.get("n") or 0
    out = [_criterion("frozen_pregame_evidence", kind, g["evidence_kind"],
                      PASS if kind == g["evidence_kind"] else FAIL,
                      "frozen pregame ledger rows (latest run before each player's kickoff)" if kind == g["evidence_kind"]
                      else f"{kind!r} runs are replays made after kickoff and are never prospective evidence"),
           _criterion("prospective_weeks", weeks, f">= {g['min_prospective_weeks']} fully final weeks",
                      PASS if weeks >= g["min_prospective_weeks"] else INSUFFICIENT,
                      f"{weeks} prospective weeks {'>=' if weeks >= g['min_prospective_weeks'] else '<'} "
                      f"{g['min_prospective_weeks']} (only fully final weeks count)"),
           _criterion("matched_appearances", n, f">= {g['min_matched_appearances_per_position']}",
                      PASS if n >= g["min_matched_appearances_per_position"] else INSUFFICIENT,
                      f"{n} matched appearances {'>=' if n >= g['min_matched_appearances_per_position'] else '<'} "
                      f"{g['min_matched_appearances_per_position']} (both models forecast, player appeared)")]

    diff = r.get("challenger_minus_champion_mae") or {}
    ci = diff.get("ci95")
    if not ci or ci[1] is None:
        out.append(_criterion("mae_vs_champion", None, "95% CI upper bound < 0", INSUFFICIENT,
                              "no week-clustered bootstrap CI of challenger - champion MAE (needs paired rows in "
                              ">= 2 fully final weeks)"))
    else:
        ok = ci[1] < g["mae_ci95_upper_below"]
        out.append(_criterion("mae_vs_champion", {"diff": diff.get("diff"), "ci95": ci, "n": n},
                              "95% CI upper bound < 0", PASS if ok else FAIL,
                              f"challenger - champion MAE {diff.get('diff')} with CI {ci} "
                              + ("excludes 0" if ok else "does not exclude 0") + " (identical observations)"))

    m = r.get("metrics") or {}
    bc, bh = (m.get("challenger") or {}).get("bias"), (m.get("champion") or {}).get("bias")
    tol = g["bias_tolerance_points"]
    if bc is None or bh is None:
        out.append(_criterion("bias_guard", None, f"|bias| <= |champion bias| + {tol}", INSUFFICIENT,
                              "bias of the challenger or the champion is missing"))
    else:
        ok = abs(bc) <= abs(bh) + tol + 1e-9
        out.append(_criterion("bias_guard", {"challenger": bc, "champion": bh}, f"|bias| <= |champion bias| + {tol}",
                              PASS if ok else FAIL,
                              f"|challenger bias| {abs(bc):.3f} vs |champion bias| {abs(bh):.3f} + {tol}"
                              + ("" if ok else ": worse by more than the tolerance")))

    sp = r.get("starters_pool") or {}
    sc, sh = (sp.get("mae") or {}).get("challenger"), (sp.get("mae") or {}).get("champion")
    req = f"MAE <= champion's + {g['starters_pool_mae_tolerance']:g}"
    if not sp.get("n") or sc is None or sh is None:
        out.append(_criterion("starters_pool_mae", None, req, INSUFFICIENT,
                              "no starters-pool MAE for both models (" + g["starters_pool"] + ")"))
    else:
        ok = sc <= sh + g["starters_pool_mae_tolerance"] + 1e-9
        out.append(_criterion("starters_pool_mae", {"challenger": sc, "champion": sh, "n": sp["n"]}, req,
                              PASS if ok else FAIL,
                              f"starters-pool MAE {sc} vs champion {sh} on {sp['n']} rows"
                              + ("" if ok else ": challenger is worse")))

    lo, hi = g["interval_coverage"]
    cov = (r.get("interval80_coverage") or {}).get("challenger")
    if cov is None:
        withheld = not CHALLENGER_STATUS.get(pos, {}).get("intervals_published", True)
        why = (f"challenger {pos} intervals are withheld by policy (confirmation coverage outside {lo}-{hi}); "
               "there is no coverage evidence, so this criterion cannot pass (a policy limitation, not an exemption)"
               if withheld else "no challenger 80% interval observations")
        out.append(_criterion("interval_coverage", None, [lo, hi], INSUFFICIENT, why))
    else:
        ok = lo <= cov <= hi
        out.append(_criterion("interval_coverage", cov, [lo, hi], PASS if ok else FAIL,
                              f"80% interval coverage {cov} "
                              + ("within" if ok else ("below (undercoverage)" if cov < lo else "above (overcoverage)"))
                              + f" {lo}-{hi}"))
    return out


def readiness_from(criteria: list[dict]) -> str:
    res = {c["name"]: c["result"] for c in criteria}
    if any(res.get(k) != PASS for k in SAMPLE_CRITERIA):
        return "insufficient_evidence"
    if any(v == FAIL for v in res.values()):
        return "failed_evaluation"
    if any(v != PASS for v in res.values()):
        return "insufficient_evidence"
    return "eligible_for_review"


def shadow_status(report: dict) -> dict:
    """Progress toward SHADOW_PROMOTION_CRITERIA, fail-closed. `status` is the readiness
    (READINESS); `eligible_for_review` is the most an evaluation can yield. The lifecycle stays
    `research_shadow`: nothing here changes which model any consumer reads."""
    out = {}
    for pos in POSITIONS:
        crit = shadow_criteria(report, pos)
        blocking = [c["explanation"] for c in crit if c["result"] != PASS]
        out[pos] = {"status": readiness_from(crit), "lifecycle": "research_shadow", "production_approved": False,
                    "criteria": crit, "blocking": blocking, "reasons": blocking,
                    "production_gate": {
                        "criteria": "forecast_backtest.PROMOTION_CRITERIA", "status": "not_evaluated",
                        "explanation": "consumer-facing use additionally needs a held-out evaluation meeting "
                                       f"PROMOTION_CRITERIA (>= {PROMOTION_CRITERIA['min_relative_mae_improvement_vs_best_trailing_baseline']:.0%} "
                                       "MAE improvement vs the best trailing baseline with CI below 0; ESPN rule "
                                       "for Start/Sit) and explicit user approval; the weekly evaluation does not "
                                       "evaluate it"}}
    return out


def promotion_blockers(status: dict) -> dict:
    """Answer 'what prevents ifp-2 from replacing ifp-1?' per position from shadow_status."""
    return {pos: {"status": s["status"], "lifecycle": s["lifecycle"],
                  "blocking": s["blocking"] + ["explicit user approval and the separate PROMOTION_CRITERIA "
                                               "production gate (never automatic)"]}
            for pos, s in status.items()}


__all__ = ["report_subject", "readiness", "train", "save_artifact", "register_saved_artifact", "backfill_store_artifacts", "load_artifact", "write_once", "latest_artifact", "predict",
           "write_run", "load_ledger", "rebuild_run", "evaluate", "label_outcomes", "team_game_finality", "week_finality", "shadow_status",
           "shadow_criteria", "promotion_blockers", "NotReady", "MODEL_VERSION", "interval_for"]
