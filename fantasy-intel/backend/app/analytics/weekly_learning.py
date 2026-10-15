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
                 cutoff precedes W; retrospective runs are reported separately and never counted

Artifacts are content-addressed (sha256 of the artifact without its timestamp), so retraining on
identical inputs reproduces the same id, and every artifact records its training seasons, latest
included week, data hash, configuration hash and scoring fingerprint.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import polars as pl

from . import challenger as ch
from .forecast_backtest import (PAIR_POOL, _arr, build_store, completed_weeks, metrics, pair_decisions,
                                week_bootstrap_mae_diff)
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


def readiness(sources: Sources, season: int, week: int, now: datetime) -> dict:
    """Is every input for a week-W forecast present and final? Errors block training/prediction."""
    errors, warnings = [], []
    now = _utc(now)
    games = sources.games.filter(pl.col("season") == season)
    before = games.filter(pl.col("week") < week)
    unfinished = before.filter(pl.col("points_for").is_null())
    if unfinished.height:
        wk = sorted(set(unfinished["week"].to_list()))
        errors.append(f"{unfinished.height // 2 or 1} game(s) before week {week} are not final (weeks {wk}); "
                      "a week enters training only when every game is final")
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
    return {"season": season, "week": week, "checked_at": now.isoformat(), "ok": not errors, "errors": errors,
            "warnings": warnings, "completed_weeks": checks, "pbp_exits_through_week": exits_through,
            "first_kickoff": first.isoformat() if first else None,
            "pregame": bool(first and now < first), "teams_already_kicked_off": started}


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


def artifact_id(art: Artifact) -> str:
    d = art.to_dict()
    d.pop("created_at", None)
    return _sha(ch.round_floats(d, 10))


def save_artifact(art: Artifact, root: Path) -> tuple[str, Path, bool]:
    """Content-addressed, write-once. Returns (id, path, created). Same content -> same file."""
    aid = artifact_id(art)
    s, w = art.config["provenance"]["target"]
    path = Path(root) / "artifacts" / art.version / f"s{s}w{w:02d}-{aid}.json"
    if path.exists():
        return aid, path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x") as f:
        f.write(json.dumps(art.to_dict(), indent=1, default=str, allow_nan=False))
    return aid, path, True


def latest_artifact(root: Path, version: str, season: int, week: int, scoring_fp: str) -> tuple[Artifact, str, Path]:
    """The most recently created artifact for (version, season, week); refuses a scoring mismatch
    or an artifact whose data cutoff is not strictly before the target week."""
    files = sorted((Path(root) / "artifacts" / version).glob(f"s{season}w{week:02d}-*.json"))
    if not files:
        raise NotReady(f"no {version} artifact for {season} week {week}; run train --week {week} first")
    arts = [(Artifact.from_dict(json.loads(f.read_text())), f) for f in files]
    art, path = max(arts, key=lambda t: t[0].created_at)
    check_artifact(art, season, week, scoring_fp)
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
            readiness_report: dict | None = None) -> dict:
    """One ledger run: every ACT/INA roster player on a team playing in W (no ESPN inputs)."""
    from .participation import live_population, predict_week

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
            if con is not None:
                from .independent_projection_store import espn_designation_fallback
                psources, fallback = espn_designation_fallback(con, sources, season, week)
            parts = predict_week(psources, participation, season, week, fallback_teams=fallback)
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
                "flags": flags_for(f)})
    content = {"season": season, "week": week, "kind": kind, "artifacts": artifact_ids,
               "participation": None if participation is None else participation.version,
               "rows": [{k: v for k, v in r.items() if k != "pregame"} for r in rows]}
    inputs_hash = _sha(ch.round_floats(content, 8))
    return {"schema": LEDGER_SCHEMA, "run_id": f"{kind}-s{season}w{week:02d}-{now:%Y%m%dT%H%M%S}-{inputs_hash[:8]}",
            "created_at": now.isoformat(), "inputs_hash": inputs_hash, "readiness": readiness_report,
            **content, "rows": rows}


def write_run(run: dict, root: Path) -> tuple[Path, bool]:
    """Write-once. A run whose inputs_hash already exists for that week is not written again."""
    d = Path(root) / "ledger" / f"s{run['season']}w{run['week']:02d}"
    d.mkdir(parents=True, exist_ok=True)
    for f in sorted(d.glob("*.json")):
        old = json.loads(f.read_text())
        if old.get("inputs_hash") == run["inputs_hash"] and old.get("kind") == run["kind"]:
            return f, False
    path = d / f"{run['run_id']}.json"
    with open(path, "x") as fh:
        fh.write(json.dumps(run, indent=1, default=str, allow_nan=False))
    return path, True


def load_ledger(root: Path) -> list[dict]:
    return [json.loads(f.read_text()) for f in sorted((Path(root) / "ledger").glob("s*/*.json"))]


# ----------------------------------------------------------------------------- prospective evaluation


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
        recs.append({"season": s, "week": w, "key": key(s, w), "gsis_id": g, "position": r["position"], "run_id": rid,
                     "pred_champion": r["champion"]["points"], "pred_challenger": r["challenger"]["points"],
                     "low_champion": r["champion"]["low"], "high_champion": r["champion"]["high"],
                     "low_challenger": r["challenger"]["low"], "high_challenger": r["challenger"]["high"],
                     "exp_champion": r["champion"]["expected_points"], "exp_challenger": r["challenger"]["expected_points"],
                     "pred_A1": r["pred_A1"], "pred_A2": r["pred_A2"], "p_play": r["p_play"],
                     "last_injury_exit": (r.get("workload") or {}).get("last_injury_exit")})
    schema = {"season": pl.Int64, "week": pl.Int64, "key": pl.Int64, "gsis_id": pl.String, "position": pl.String,
              "run_id": pl.String, **{c: pl.Float64 for c in ("pred_champion", "pred_challenger", "low_champion",
                                                             "high_champion", "low_challenger", "high_challenger",
                                                             "exp_champion", "exp_challenger", "pred_A1", "pred_A2",
                                                             "p_play")}, "last_injury_exit": pl.Int64}
    return pl.DataFrame(recs, schema=schema) if recs else pl.DataFrame(schema=schema)


def evaluate(runs: list[dict], sources: Sources, kind: str = "pregame", seed: int = 20261014) -> dict:
    """Frozen forecasts vs completed outcomes. Conditional: players who appeared (label = league
    points). Unconditional: every forecast row of a final game (0 when he did not appear)."""
    rows = frozen_rows(runs, kind)
    final = sources.games.filter(pl.col("points_for").is_not_null()).select("season", "week", "team").unique()
    pops = []
    for (s, w), g in rows.group_by(["season", "week"], maintain_order=True) if rows.height else []:
        if final.filter((pl.col("season") == s) & (pl.col("week") == w)).height == 0:
            continue
        pops.append(g)
    if not pops:
        return {"kind": kind, "weeks": [], "note": "no frozen forecast has a final outcome yet", "positions": {}}
    rows = pl.concat(pops)
    labels = sources.panel.select("season", "week", "gsis_id", pl.col("league_points").alias("label"))
    j = rows.join(labels, on=["season", "week", "gsis_id"], how="left")
    cond = j.filter(pl.col("label").is_not_null())
    out = {"kind": kind, "weeks": sorted({(int(s), int(w)) for s, w in j.select("season", "week").unique().iter_rows()}),
           "positions": {}}
    for pos in [*POSITIONS, "ALL"]:
        g = cond if pos == "ALL" else cond.filter(pl.col("position") == pos)
        g = g.filter(pl.col("pred_champion").is_not_null() & pl.col("pred_challenger").is_not_null())
        a = _arr(g, "label")
        rep = {"n": g.height, "metrics": {m: metrics(a, _arr(g, f"pred_{m}"))
                                          for m in ("champion", "challenger", "A1", "A2")}}
        if g.select("key").n_unique() >= 2:
            rep["challenger_minus_champion_mae"] = week_bootstrap_mae_diff(g, "pred_challenger", "pred_champion",
                                                                           1000, seed)
        for m in ("champion", "challenger"):
            iv = g.filter(pl.col(f"low_{m}").is_not_null())
            if iv.height:
                hit = iv.filter((pl.col("label") >= pl.col(f"low_{m}")) & (pl.col("label") <= pl.col(f"high_{m}"))).height
                rep.setdefault("interval80_coverage", {})[m] = round(hit / iv.height, 4)
        ie = g.filter(pl.col("last_injury_exit") == 1)
        rep["after_injury_exit"] = {"n": ie.height, **{m: metrics(_arr(ie, "label"), _arr(ie, f"pred_{m}"))["mae"]
                                                         for m in ("champion", "challenger")}}
        u = j.filter(pl.col("exp_champion").is_not_null() & pl.col("exp_challenger").is_not_null())
        u = u if pos == "ALL" else u.filter(pl.col("position") == pos)
        ua = u["label"].fill_null(0.0).to_numpy().astype(float)
        rep["unconditional"] = {"n": u.height, **{m: metrics(ua, _arr(u, f"exp_{m}")) for m in ("champion", "challenger")}}
        out["positions"][pos] = rep
    both = cond.filter(pl.col("pred_champion").is_not_null() & pl.col("pred_challenger").is_not_null()
                       & pl.col("pred_A1").is_not_null())
    if both.height:
        out["pairs"] = pair_decisions(both, ["pred_champion", "pred_challenger"], pool=PAIR_POOL)
    out["shadow_status"] = shadow_status(out)
    return out


def shadow_status(report: dict) -> dict:
    """Progress toward SHADOW_PROMOTION_CRITERIA. Never promotes; 'eligible_for_review' at most."""
    c = ch.SHADOW_PROMOTION_CRITERIA
    weeks = len(report.get("weeks", []))
    out = {}
    for pos in POSITIONS:
        r = report["positions"].get(pos) or {}
        reasons = []
        if weeks < c["min_prospective_weeks"]:
            reasons.append(f"{weeks} prospective weeks < {c['min_prospective_weeks']}")
        if r.get("n", 0) < c["min_matched_appearances_per_position"]:
            reasons.append(f"{r.get('n', 0)} matched appearances < {c['min_matched_appearances_per_position']}")
        ci = (r.get("challenger_minus_champion_mae") or {}).get("ci95")
        if not ci or ci[1] >= 0:
            reasons.append(f"challenger - champion MAE CI {ci} does not exclude 0")
        m = r.get("metrics") or {}
        if m.get("challenger", {}).get("bias") is not None and \
                abs(m["challenger"]["bias"]) > abs(m["champion"]["bias"]) + 0.15:
            reasons.append("bias worse than the champion's by more than 0.15")
        cov = (r.get("interval80_coverage") or {}).get("challenger")
        lo, hi = c["interval_coverage"]
        if cov is not None and not lo <= cov <= hi:
            reasons.append(f"interval coverage {cov} outside {lo}-{hi}")
        out[pos] = {"status": "eligible_for_review" if not reasons else "shadow", "reasons": reasons}
    return out


__all__ = ["readiness", "train", "save_artifact", "latest_artifact", "predict", "write_run", "load_ledger",
           "evaluate", "NotReady", "MODEL_VERSION", "interval_for"]
