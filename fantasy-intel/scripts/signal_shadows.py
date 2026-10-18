"""Prospective shadows of three pre-registered research candidates on top of the frozen ifp-2 (research only;
see docs/research/signal_shadows.md):

  ifp-fr-p1   ifp-2 + FTN first-read team share / own share, RB and WR
              (docs/research/first_read_depth_chart_signals.md, follow-up plan)
  ifp-dct-p1  ifp-2 + the static pregame depth-chart tier dc_tier, QB, RB and WR (same document)
  ifp-awd-p1  ifp-2 with xfpl_eb replaced by the decline-only adaptive role, RB, WR and TE
              (docs/research/adaptive_workload.md section 9)

    cd backend
    uv run python ../scripts/signal_shadows.py prepare                # fetch missing raw snapshots + build the store
    uv run python ../scripts/signal_shadows.py pregame [--week W]     # gated write-once pregame ledger for week W
    uv run python ../scripts/signal_shadows.py predict --week W       # freeze now (no gate; still write-once)
    uv run python ../scripts/signal_shadows.py score                  # score fully final frozen weeks (SHADOW_GATE)
    uv run python ../scripts/signal_shadows.py cycle   [--week W]     # score, missed-week check, then pregame
    uv run python ../scripts/signal_shadows.py status
    uv run python ../scripts/signal_shadows.py pregame --week W --now 2026-10-25T12:00Z   # gate only (dry run)

Who runs it (no new scheduler), exactly like scripts/ctx_shadow.py:
  * refresh_all.py's read-only evidence stage runs `pregame` for the upcoming week on every refresh.
  * learning_cycle.py runs `cycle` as a non-fatal stage after the production run (Tue-Thu).

Freeze rule (fixed here, before any prospective week exists): the week's ledger is frozen at the first run at
which the main slate is imminent, i.e. at least SLATE_SHARE of the teams that have not kicked off do so within
DEADLINE_HOURS. Freezing late gives the depth chart its freshest pregame snapshot, the previous week's FTN
charting time to land (40-71 h after Monday night) and ifp-2's features the week's final injury report. Teams
that already kicked off are excluded, so Thursday (and early international) games usually fall outside the
ledger, as for ifp-ctx-p1. The current season's FTN, play-by-play and depth-chart files are re-fetched just
before the freeze; if a fetch fails, the last stored snapshot (also pregame) is used and a warning recorded.

ifp-2 and the three candidates are fitted in the SAME run on the same training rows (strictly earlier, fully
final weeks), with ifp-2's frozen penalties and recency half-life; nothing is searched. Each candidate's spec is
frozen write-once on first use (frozen/<candidate>.json) and every later run must match it.

Writes only under <ifp-dir>/research/signal_shadows/ (git-ignored): raw/ (content-addressed nflverse snapshots),
store.parquet (cache), frozen/, artifacts/ and ledger/ (write-once), runs/ and missed/ (write-once records) and
scores.json (derived, replaced). It opens the databases read-only and never touches the live database tables,
ifp-1/ifp-2 artifacts, the weekly ledger, the registry, the API or the frontend. The ledgers hold FTN-derived
numbers (CC-BY-SA 4.0): internal research input only, never published.

Exit codes: 0 ok (frozen, already frozen, deferred, nothing to do); 3 ok but with warnings (a source fetch
failed, a candidate abstained, a missed week); 1 a step failed (recorded, nothing half-written).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
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

SCHEMA = "ifp-signal-shadow-run/v1"
LEDGER_SCHEMA = "ifp-signal-shadow-ledger/v1"
SHADOW = "signal_shadows"
FR, DCT, AWD = "ifp-fr-p1", "ifp-dct-p1", "ifp-awd-p1"
CANDIDATES = (FR, DCT, AWD)
SHORT = {FR: "fr", DCT: "dct", AWD: "awd"}       # ledger column prefixes: <short>_points
FIRST_PROSPECTIVE = (2026, 7)       # pre-registered: only frozen forecasts from 2026 week 7 on count
SLATE_SHARE = 0.5
DEADLINE_HOURS = 6.0                # the pregame guard's lead window
FETCH_FROM = {"ftn": 2022, "pbp": 2022, "depth": 2025}    # FTN charting 2022+, modern depth charts 2025+
SOURCE_FAMILIES = {FR: ("ftn", "pbp"), DCT: ("depth",), AWD: ()}
INPUT_COLUMNS = ("games_recent", "xfpl_eb", "fr_team_share", "fr_own_share", "ftn_games", "dc_tier", "dc_off_chart",
                 "dc_age_hours", "aw_role_d", "aw_eb_d", "aw_r_d", "aw_streak_d", "aw_last_class_d")
SEED = 20261018
EXIT_OK, EXIT_FAILED, EXIT_WARNINGS = 0, 1, 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def out_dir(args) -> Path:
    return Path(args.ifp_dir) / "research" / SHADOW


def raw_dir(args) -> Path:
    return out_dir(args) / "raw"


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


# ----------------------------------------------------------------------------- frozen specs


def candidate_features(cfg, name: str) -> dict[str, list[str]]:
    """Per position the candidate's features (only the positions it forecasts): ifp-2's frozen features plus
    the candidate's group, or with xfpl_eb swapped for the decline-only role."""
    from app.analytics import adaptive_workload as aw
    from app.analytics import usage_signals as us
    from app.analytics.forecast_features import POSITIONS

    if name == AWD:
        return {p: aw.awd_features(p, cfg) for p in aw.AWD_POSITIONS}
    spec = us.PROSPECTIVE[name]
    return {p: [*cfg.features(p), *[f for f in spec["features"] if f not in cfg.features(p)]]
            for p in POSITIONS if p in spec["positions"]}


def candidate_spec(cfg, name: str, scoring_fp: str) -> dict:
    """The pre-registered spec (everything a forecast depends on except data), hashed."""
    from app.analytics import adaptive_workload as aw
    from app.analytics import usage_signals as us
    from app.analytics.forecast_features import RECENT_GAMES

    feats = candidate_features(cfg, name)
    fit = {p: {"lam": cfg.positions[p]["lam"], "half_life_weeks": cfg.positions[p].get("half_life_weeks"),
               "loss": cfg.positions[p].get("loss", "squared")} for p in feats}
    if name == AWD:
        extra = {"rules": aw.AWD_RULES, "role_config": asdict(aw.DECLINE_CONFIG),
                 "constants": {"EXPOSURE_CONFIDENCE": aw.EXPOSURE_CONFIDENCE, "MIN_EXPOSURE": aw.MIN_EXPOSURE,
                               "REGIME_GAMES": aw.REGIME_GAMES, "MIN_REGIME_GAMES": aw.MIN_REGIME_GAMES,
                               "REGIME_DELTA": aw.REGIME_DELTA, "REGIME_K": aw.REGIME_K}}
    else:
        p = us.PROSPECTIVE[name]
        extra = {"rules": us.PROSPECTIVE_RULES, "source": p["source"]}
        if p["source"] == "ftn":
            extra["constants"] = {"semantics": us.SEMANTICS[p["semantics"]], "RECENT_GAMES": RECENT_GAMES,
                                  "FTN_PRIOR_GAMES": us.FTN_PRIOR_GAMES, "FTN_PRIOR_TEAM_FIRST": us.FTN_PRIOR_TEAM_FIRST,
                                  "FTN_PRIOR_OWN_TARGETS": us.FTN_PRIOR_OWN_TARGETS, "prior_seasons": 1}
        else:
            extra["constants"] = {"cutoff_hours": p["cutoff_hours"], "MAX_STALE_DAYS": us.MAX_STALE_DAYS,
                                  "STARTERS": us.STARTERS, "OFFENSE_GROUPS": list(us.OFFENSE_GROUPS)}
    spec = {"version": name, "status": "research_only", "positions": sorted(feats), "features": feats, "fit": fit,
            "first_week": FIRST_PROSPECTIVE, "freeze_rule": {"slate_share": SLATE_SHARE, "deadline_hours": DEADLINE_HOURS},
            "ifp2_version": cfg.version, "ifp2_config_hash": cfg.hash(), "scoring_fingerprint": scoring_fp, **extra}
    spec["config_hash"] = _sha(spec)
    return spec


def frozen_specs(args, cfg, scoring_fp: str) -> dict[str, dict]:
    """Each candidate's spec, frozen write-once on first use; a later run with any other spec, ifp-2
    configuration or league scoring is refused."""
    out = {}
    for name in CANDIDATES:
        spec = candidate_spec(cfg, name, scoring_fp)
        path = out_dir(args) / "frozen" / f"{name}.json"
        if not _write_once(path, {**spec, "frozen_at": _utcnow().isoformat()}):
            prev = json.loads(path.read_text())
            if prev["config_hash"] != spec["config_hash"]:
                raise SystemExit(f"{path.name} was frozen with a different spec, ifp-2 config or scoring; refusing")
        out[name] = json.loads(path.read_text())
    return out


# ----------------------------------------------------------------------------- raw sources


def _nflverse(family: str, season: int):
    import nflreadpy as nfl
    import usage_signals_research as usr

    if family == "ftn":
        return nfl.load_ftn_charting([season])
    if family == "depth":
        return nfl.load_depth_charts([season])
    p = nfl.load_pbp([season])
    return p.select([c for c in usr.PBP_COLUMNS if c in p.columns])


def fetch(args, season: int, *, refresh_current: bool = True, loader=None) -> dict:
    """Content-addressed raw snapshots (usage_signals.write_snapshot): earlier seasons are fetched once (when no
    snapshot exists), the current season again when `refresh_current`. A failed fetch keeps the stored snapshot
    and is returned as a warning; it never raises."""
    from app.analytics import usage_signals as us

    loader = loader or _nflverse
    cur = us.load_manifest(raw_dir(args))["current"]
    out = {"snapshots": {}, "warnings": []}
    for family, first in FETCH_FROM.items():
        for s in range(first, season + 1):
            k = f"{family}_{s}"
            if k in cur and not (refresh_current and s == season):
                out["snapshots"][k] = {**cur[k], "fetched": False}
                continue
            try:
                e = us.write_snapshot(raw_dir(args), family, s, loader(family, s))
                out["snapshots"][k] = {**e, "fetched": True}
            except Exception as err:      # noqa: BLE001  network / upstream: keep the stored snapshot
                stored = us.load_manifest(raw_dir(args))["current"].get(k)
                out["snapshots"][k] = {**(stored or {}), "fetched": False, "error": f"{err.__class__.__name__}: {err}"}
                out["warnings"].append(f"{k} fetch failed ({err.__class__.__name__}: {err}); "
                                       + ("using the stored snapshot" if stored else "no snapshot stored"))
    return out


def _load_raw(args, family: str, seasons) -> "pl.DataFrame | None":
    import polars as pl
    from app.analytics import usage_signals as us

    frames = []
    for s in seasons:
        df = us.read_snapshot(raw_dir(args), family, s)
        if df is None:
            return None
        frames.append(df)
    return pl.concat(frames, how="diagonal_relaxed") if frames else None


def sources_complete(args, season: int) -> dict[str, bool]:
    """Per candidate: is every raw snapshot its training store needs present?"""
    from app.analytics import usage_signals as us

    cur = us.load_manifest(raw_dir(args))["current"]
    return {name: all(f"{fam}_{s}" in cur for fam in fams for s in range(FETCH_FROM[fam], season + 1))
            for name, fams in SOURCE_FAMILIES.items()}


# ----------------------------------------------------------------------------- store


def _games(src):
    import usage_signals_research as usr
    return usr._games(src)


def add_signals(args, store, src, season: int):
    """FTN (2022+, empirical semantics), modern depth (2025+, 1.5 h point-in-time) and decline-only role
    columns for every store row, exactly as the research built them. A candidate whose raw snapshots are
    incomplete gets null columns (it abstains at predict)."""
    import polars as pl
    from app.analytics import adaptive_workload as aw
    from app.analytics import usage_signals as us

    ok = sources_complete(args, season)
    if ok[FR]:
        seasons = range(FETCH_FROM["ftn"], season + 1)
        tg, _ = us.classify_targets(_load_raw(args, "pbp", seasons), _load_raw(args, "ftn", seasons), "empirical")
        pr, tr = us.game_reads(tg)
        app = us.appearance_reads(src.panel, pr, tr)
        parts = [us.ftn_features(app, int(s), int(w), rows.select("gsis_id", "position"))
                 .with_columns(pl.lit(int(s) * 100 + int(w)).cast(store["key"].dtype).alias("key"))
                 for (s, w), rows in store.filter(pl.col("season") >= FETCH_FROM["ftn"]).group_by(
                     "season", "week", maintain_order=True)]
        if parts:
            store = store.join(pl.concat(parts).select("key", "gsis_id", "fr_team_share", "fr_own_share", "ftn_games"),
                               on=["key", "gsis_id"], how="left")
    if ok[DCT]:
        snaps, _ = us.normalize_modern(_load_raw(args, "depth", range(FETCH_FROM["depth"], season + 1)))
        tgt = store.filter(pl.col("season") >= FETCH_FROM["depth"]).select("season", "week", "gsis_id", "position", "team")
        dc, _ = us.depth_features(snaps, _games(src), tgt, us.PROSPECTIVE[DCT]["cutoff_hours"],
                                  store.select("season", "week", "gsis_id", "snap_pct"))
        store = store.join(dc.select("season", "week", "gsis_id", "dc_tier", "dc_off_chart", "dc_age_hours"),
                           on=["season", "week", "gsis_id"], how="left")
    roles = aw.build_role_store(src, store, aw.DECLINE_CONFIG, aw.DECLINE_SUFFIX)
    store = store.join(roles, on=["key", "gsis_id"], how="left")
    for c, t in (("fr_team_share", pl.Float64), ("fr_own_share", pl.Float64), ("ftn_games", pl.Float64),
                 ("dc_tier", pl.Float64), ("dc_off_chart", pl.Float64), ("dc_age_hours", pl.Float64)):
        if c not in store.columns:
            store = store.with_columns(pl.lit(None, dtype=t).alias(c))
    return store


def build(args):
    """(sources, scoring, store, provenance). The store is the ifp-2 feature store of every fully final week
    plus the candidates' columns, cached by an input fingerprint (sources, weeks, scoring, code, raw snapshots)."""
    import hgb_research as hr
    import polars as pl
    from app.analytics import adaptive_workload as aw
    from app.analytics import challenger as ch
    from app.analytics import usage_signals as us
    from app.analytics.forecast_backtest import build_store

    src, scoring = hr._sources(args)
    weeks = hr.final_weeks(args, src)
    fp = _sha({"store": hr.input_fingerprint(args, src, scoring.fingerprint, weeks),
               "raw": us.manifest_fingerprint(raw_dir(args), tuple(FETCH_FROM)),
               "decline": asdict(aw.DECLINE_CONFIG), "complete": sources_complete(args, args.season)})
    path, meta = out_dir(args) / "store.parquet", out_dir(args) / "store.meta.json"
    prov = {"fingerprint": fp, "scoring_fingerprint": scoring.fingerprint, "weeks": len(weeks),
            "latest_week_key": max((s * 100 + w for s, w in weeks), default=None),
            "sources_complete": sources_complete(args, args.season)}
    if path.exists() and meta.exists() and not getattr(args, "rebuild", False) \
            and json.loads(meta.read_text()).get("fingerprint") == fp:
        return src, scoring, pl.read_parquet(path), prov
    store = ch.add_last_game(build_store(src, weeks), src.panel)
    store = add_signals(args, store, src, args.season)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    store.write_parquet(tmp)
    os.replace(tmp, path)
    meta.write_text(json.dumps({**prov, "rows": store.height}, default=str))
    return src, scoring, store, prov


# ----------------------------------------------------------------------------- predict (the freeze)


def _frame_sha(df) -> str | None:
    import hgb_research as hr
    return None if df is None else hr._frame_hash(df)


def _code_sha() -> str:
    root = Path(__file__).resolve().parents[1] / "backend" / "app" / "analytics"
    files = [root / f for f in ("usage_signals.py", "adaptive_workload.py", "challenger.py", "forecast_features.py",
                                "independent_projections.py")] + [Path(__file__)]
    return hashlib.sha256(b"".join(f.read_bytes() for f in files)).hexdigest()[:16]


def live_features(args, src, season: int, week: int, teams: list[str], now: datetime, ok: dict[str, bool]):
    """Pregame feature rows of week W for the open teams: ifp-2's features, the point-in-time FTN and depth
    features (as of `now`) and the decline-only role. Returns (frame, provenance)."""
    import polars as pl
    from app.analytics import adaptive_workload as aw
    from app.analytics import usage_signals as us
    from app.analytics.forecast_features import features_for_week
    from app.analytics.participation import live_population

    pop = live_population(src, season, week).filter(pl.col("team").is_in(teams))
    feats = features_for_week(src, season, week, pop)
    prov = {}
    if ok[FR]:
        seasons = range(max(FETCH_FROM["ftn"], season - 1), season + 1)      # this season and the previous one
        fr, prov["ftn"] = us.live_ftn_features(_load_raw(args, "pbp", seasons), _load_raw(args, "ftn", seasons),
                                               src.panel, season, week, feats.select("gsis_id", "position"), now)
        feats = feats.join(fr.select("gsis_id", "fr_team_share", "fr_own_share", "ftn_games"), on="gsis_id", how="left")
    if ok[DCT]:
        tgt = feats.select(pl.lit(season, dtype=pl.Int64).alias("season"), pl.lit(week, dtype=pl.Int64).alias("week"),
                           "gsis_id", "position", "team")
        g = _games(src).filter((pl.col("season") == season) & (pl.col("week") <= week))
        dc, prov["depth"] = us.live_depth_features(_load_raw(args, "depth", [season]), g, tgt,
                                                   us.PROSPECTIVE[DCT]["cutoff_hours"], now)
        feats = feats.join(dc.select("gsis_id", "dc_tier", "dc_off_chart", "dc_age_hours",
                                     pl.col("snap_dt").cast(pl.String).alias("dc_snapshot_dt")), on="gsis_id", how="left")
    role = aw.role_features_for_week(src, season, week, feats.select("gsis_id", "position", "team"), aw.DECLINE_CONFIG)
    feats = feats.join(role.rename({c: f"{c}{aw.DECLINE_SUFFIX}" for c in aw.AW_COLUMNS}), on="gsis_id", how="left")
    return feats, prov


def cmd_predict(args, now: datetime | None = None) -> dict:
    """Frozen pregame forecasts (ifp-2 and the three candidates, same run, same rows) for one week, written
    once. Teams that have kicked off are excluded (never backdated)."""
    import polars as pl
    from app.analytics import challenger as ch
    from app.analytics.forecast_features import POSITIONS
    from app.analytics.independent_projections import MIN_TRAIN_ROWS
    from app.analytics.usage_signals import FTN_LICENSE
    from app.analytics.weekly_learning import team_kickoffs

    season, week = args.season, args.week
    lpath = ledger_path(args, season, week)
    if lpath.exists():
        led = json.loads(lpath.read_text())
        return {"ledger": str(lpath), "written": False, "created_at": led["created_at"], "rows": len(led["rows"])}
    src, scoring, store, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    specs = frozen_specs(args, cfg, scoring.fingerprint)
    kick = team_kickoffs(src, season, week)
    clock = (lambda: now) if now is not None else _utcnow       # `now`: a fixed clock (tests)
    now = clock()                                # after the slow build: never select on a stale clock
    open_teams = sorted(t for t, ko in kick.items() if now < ko)
    if not open_teams:
        raise SystemExit(f"every week-{week} game has kicked off: no pregame forecast possible")
    cutoff = season * 100 + week
    ok = prov["sources_complete"]
    abstain = {n: f"raw {'/'.join(SOURCE_FAMILIES[n])} snapshots incomplete (run `prepare`)" for n in CANDIDATES
               if not ok[n]}
    models, n_train = {}, {}
    for pos in POSITIONS:
        train = ch.train_rows(store, cutoff, pos)
        assert train.height == 0 or train["key"].max() < cutoff
        n_train[pos] = train.height
        if train.height < MIN_TRAIN_ROWS:
            continue
        spec = cfg.spec(pos)

        def fit(features, name):
            return ch.fit_linear(train, pos, features, spec.lam[pos], loss=spec.loss, huber_c=spec.huber_c,
                                 half_life_weeks=spec.half_life_weeks, target_key=cutoff, name=name)

        models[pos] = {"ifp2": fit(cfg.features(pos), "ifp2")}
        for name in CANDIDATES:
            if name not in abstain and pos in specs[name]["features"]:
                models[pos][name] = fit(specs[name]["features"][pos], name)
    hashes = {n: s["config_hash"] for n, s in specs.items()}
    art = {"schema": "ifp-signal-shadow-artifact/v1", "candidates": hashes, "ifp2_config_hash": cfg.hash(),
           "training_cutoff_key": cutoff, "created_at": now.isoformat(), "store_fingerprint": prov["fingerprint"],
           "training_rows": n_train, "models": {p: {k: m.to_dict() for k, m in v.items()} for p, v in models.items()}}
    apath = out_dir(args) / "artifacts" / f"s{season}w{week:02d}.json"
    if not _write_once(apath, art):
        prev = json.loads(apath.read_text())        # an interrupted earlier run's artifact is reused, never refit
        if prev["candidates"] != hashes or prev["training_cutoff_key"] != cutoff:
            raise SystemExit(f"{apath.name} was built under a different configuration or cutoff")
        models = {p: {k: ch.RidgeModel.from_dict(d) for k, d in v.items() if k not in abstain}
                  for p, v in prev["models"].items()}
    feats, live_prov = live_features(args, src, season, week, open_teams, now, ok)
    rows = []
    for pos in POSITIONS:
        g = feats.filter(pl.col("position") == pos)
        if g.height == 0:
            continue
        preds = {k: m.predict(g) for k, m in models.get(pos, {}).items()}
        for i, rec in enumerate(g.iter_rows(named=True)):
            played = (rec.get("games_recent") or 0) > 0
            row = {"gsis_id": rec["gsis_id"], "position": pos, "team": rec["team"], "kickoff": kick[rec["team"]].isoformat(),
                   "ifp2_points": round(float(preds["ifp2"][i]), 4) if played and "ifp2" in preds else None}
            for name in CANDIDATES:
                v = preds.get(name)
                row[f"{SHORT[name]}_points"] = round(float(v[i]), 4) if played and v is not None else None
            row["inputs"] = {c: rec.get(c) for c in (*INPUT_COLUMNS, "dc_snapshot_dt") if c in rec}
            rows.append(row)
    # re-check every kickoff at write time: no row in a "pregame" ledger is ever written after its kickoff
    written_at = clock()
    still_open = {t for t in open_teams if written_at < kick[t]}
    rows = [r for r in rows if r["team"] in still_open]
    if not still_open:
        raise SystemExit(f"every remaining week-{week} game kicked off while forecasting: nothing written")
    inj = None if src.injuries is None else src.injuries.filter((pl.col("season") == season) & (pl.col("week") == week))
    ledger = {"schema": LEDGER_SCHEMA, "kind": "pregame", "season": season, "week": week,
              "created_at": written_at.isoformat(), "candidates": hashes, "abstained": abstain,
              "artifact": apath.name, "training_cutoff_key": cutoff, "store_fingerprint": prov["fingerprint"],
              "code_sha": _code_sha(), "teams_excluded_already_started": sorted(set(kick) - still_open),
              "license": FTN_LICENSE,
              "sources": {"injury_report_rows": None if inj is None else inj.height, "injury_report_sha": _frame_sha(inj),
                          "rosters_sha": _frame_sha(src.rosters), "panel_sha": _frame_sha(src.panel),
                          "raw_snapshots": fetch_state(args), **live_prov},
              "rows": rows}
    written = _write_once(lpath, ledger)
    return {"ledger": str(lpath), "written": written, "rows": len(rows), "abstained": abstain,
            "forecast_rows": {n: sum(r[f"{SHORT[n]}_points"] is not None for r in rows) for n in CANDIDATES},
            "open_teams": len(still_open)}


def fetch_state(args) -> dict:
    """The raw snapshots the store and the live features were built from (manifest `current`)."""
    from app.analytics import usage_signals as us
    return {k: {f: v.get(f) for f in ("sha", "file", "rows", "retrieved_at")}
            for k, v in sorted(us.load_manifest(raw_dir(args))["current"].items())}


# ----------------------------------------------------------------------------- freeze rule + pregame


def freeze_decision(kickoffs: dict, now: datetime, *, slate_share: float = SLATE_SHARE,
                    deadline_hours: float = DEADLINE_HOURS) -> dict:
    """The freeze rule above, on {team: kickoff (aware UTC) | None}. Pure.
    Returns {action: freeze | defer | nothing_open, reason, warnings, notes, counts...}."""
    known = {t: k for t, k in kickoffs.items() if k is not None}
    open_ = {t: k for t, k in known.items() if k > now}
    imminent = [t for t, k in open_.items() if (k - now).total_seconds() / 3600 <= deadline_hours]
    out = {"open_teams": len(open_), "imminent_teams": len(imminent), "started_teams": len(known) - len(open_),
           "unknown_kickoff": sorted(t for t, k in kickoffs.items() if k is None), "warnings": [], "notes": []}
    if out["unknown_kickoff"]:
        out["warnings"].append("unknown kickoff (excluded, never guessed): " + ", ".join(out["unknown_kickoff"]))
    if not open_:
        return {**out, "action": "nothing_open", "reason": "every game of the week has kicked off"}
    if len(imminent) >= slate_share * len(open_):
        return {**out, "action": "freeze",
                "reason": f"main slate imminent ({len(imminent)} of {len(open_)} open teams within {deadline_hours:g}h)"}
    out["notes"].append(f"{len(imminent)} of {len(open_)} open teams kick off within {deadline_hours:g}h (need "
                        f"{slate_share:.0%}); deferred to a later pregame run")
    return {**out, "action": "defer", "reason": "main slate not imminent"}


def scheduled_kickoffs(args, season: int, week: int) -> dict:
    import duckdb
    from app.analytics.injury_freshness import scheduled_kickoffs as sk

    con = duckdb.connect(str(args.live_db), read_only=True)
    try:
        return sk(con, season, week)
    finally:
        con.close()


def default_predict(args, season: int, week: int) -> dict:
    args.week = week
    return cmd_predict(args)


def _record(args, rec: dict, now: datetime) -> Path:
    # unique per attempt: two attempts in the same second (one process) must not collide on a write-once name
    path = (out_dir(args) / "runs" / f"s{rec['season']}w{rec['week']:02d}-{now:%Y%m%dT%H%M%SZ}-{rec['kind']}-"
            f"{os.getpid()}-{uuid.uuid4().hex[:8]}.json")
    if args.write:
        _write_once(path, rec)
    return path


def run_pregame(args, season: int, week: int, *, now: datetime | None = None, kickoffs=None, fetcher=None,
                predict=None) -> dict:
    """Freeze week W's pregame ledger once, when the freeze rule allows: refresh the current season's raw
    snapshots, then forecast. Never raises: every outcome (including a failure) is returned and, unless dry,
    recorded write-once under runs/."""
    now = now or _utcnow()
    fetcher = fetcher or (lambda a, s: fetch(a, s))
    predict = predict or default_predict
    rec = {"schema": SCHEMA, "kind": "pregame", "candidates": list(CANDIDATES), "season": season, "week": week,
           "started_at": now.isoformat(), "dry_run": not args.write, "warnings": [], "notes": []}
    lpath = ledger_path(args, season, week)
    try:
        if (season, week) < FIRST_PROSPECTIVE:
            rec.update(status="before_prospective_start",
                       reason=f"prospective evidence starts {FIRST_PROSPECTIVE[0]} week {FIRST_PROSPECTIVE[1]}")
            return rec                                  # not recorded: nothing was attempted
        if lpath.exists():
            rec.update(status="already_frozen", ledger=str(lpath), ledger_sha256=_sha_file(lpath))
            return rec                                  # idempotent: no record, no fetch, nothing rewritten
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
            got = fetcher(args, season)
            rec["fetch"] = {k: {f: v.get(f) for f in ("sha", "rows", "fetched", "error")}
                            for k, v in got["snapshots"].items()}
            rec["warnings"] += got["warnings"]
            res = predict(args, season, week)
            rec["predict"] = res
            for name, why in (res.get("abstained") or {}).items():
                rec["warnings"].append(f"{name} abstained this week: {why}")
            written = bool(res.get("written"))
            path = Path(res.get("ledger") or lpath)
            rec.update(status="frozen" if written else "already_frozen", ledger=str(path),
                       ledger_sha256=_sha_file(path) if path.exists() else None)
    except BaseException as err:      # noqa: BLE001  recorded, never raised (SystemExit: predict's refusals)
        if isinstance(err, KeyboardInterrupt):
            raise
        rec.update(status="failed", error=f"{err.__class__.__name__}: {err}")
        rec["warnings"].append(f"pregame shadow forecast for week {week} failed: {err}")
    rec["finished_at"] = _utcnow().isoformat()
    rec["record"] = str(_record(args, rec, now))
    return rec


def check_missed(args, season: int, week: int, *, now: datetime | None = None, kickoffs=None) -> dict | None:
    """A prospective week whose games have all kicked off without a frozen ledger is recorded write-once under
    missed/ (never backfilled). Returns the record, or None."""
    if week < 1 or (season, week) < FIRST_PROSPECTIVE or ledger_path(args, season, week).exists():
        return None
    now = now or _utcnow()
    kickoffs = kickoffs if kickoffs is not None else scheduled_kickoffs(args, season, week)
    known = [k for k in kickoffs.values() if k is not None]
    if not known or max(known) > now:
        return None
    rec = {"candidates": list(CANDIDATES), "season": season, "week": week, "last_kickoff": max(known).isoformat(),
           "recorded_at": now.isoformat(),
           "reason": "no signal-shadow pregame ledger was frozen before the week's games; nothing is backdated"}
    if args.write:
        _write_once(out_dir(args) / "missed" / f"s{season}w{week:02d}.json", rec)
    return rec


# ----------------------------------------------------------------------------- score


def ledger_frame(ledgers: list[dict]):
    """Every ledger row as (key, gsis_id, position, pred_ifp2, pred_<short>..., inputs)."""
    import polars as pl

    recs = []
    for led in ledgers:
        k = led["season"] * 100 + led["week"]
        for r in led["rows"]:
            recs.append({"key": k, "gsis_id": r["gsis_id"], "ledger_position": r["position"],
                         "pred_ifp2": r.get("ifp2_points"),
                         **{f"pred_{SHORT[n]}": r.get(f"{SHORT[n]}_points") for n in CANDIDATES},
                         "in_dc_tier": (r.get("inputs") or {}).get("dc_tier"),
                         "in_ftn_games": (r.get("inputs") or {}).get("ftn_games"),
                         "in_aw_streak_d": (r.get("inputs") or {}).get("aw_streak_d")})
    schema = {"key": pl.Int64, "gsis_id": pl.String, "ledger_position": pl.String, "pred_ifp2": pl.Float64,
              **{f"pred_{SHORT[n]}": pl.Float64 for n in CANDIDATES}, "in_dc_tier": pl.Float64,
              "in_ftn_games": pl.Float64, "in_aw_streak_d": pl.Float64}
    return pl.DataFrame(recs, schema=schema)


def gate_report(p, name: str, positions, intervals: dict) -> dict:
    """challenger.SHADOW_GATE for one candidate through weekly_learning.shadow_criteria (the criteria the ifp-2
    shadow is judged by), on identical rows: frozen pregame, both models forecast, the player appeared. The
    candidate's 80% interval is ifp-2's frozen band around its point (pre-registered)."""
    import polars as pl
    from app.analytics import weekly_learning as wl
    from app.analytics.forecast_backtest import _arr, metrics, starters_pool_expr, week_bootstrap_mae_diff
    from app.analytics.independent_projections import interval_for

    col = f"pred_{SHORT[name]}"
    d = p.filter(pl.col(col).is_not_null() & pl.col("pred_ifp2").is_not_null() & pl.col("label").is_not_null()
                 & pl.col("position").is_in(list(positions)))
    d = d.with_columns(starters_pool_expr().alias("_starter"),
                       pl.col(col).alias("pred_challenger"), pl.col("pred_ifp2").alias("pred_champion"))
    rep = {"kind": "pregame", "weeks": sorted(d["key"].unique().to_list()), "positions": {}}
    for pos in positions:
        g = d.filter(pl.col("position") == pos)
        a = _arr(g, "label")
        r = {"n": g.height, "weeks": g.select("key").n_unique(),
             "metrics": {"challenger": metrics(a, _arr(g, "pred_challenger"), True),
                         "champion": metrics(a, _arr(g, "pred_champion"), True)}}
        if g.select("key").n_unique() >= 2:
            r["challenger_minus_champion_mae"] = week_bootstrap_mae_diff(g, "pred_challenger", "pred_champion",
                                                                         1000, SEED, True)
        if g.height and intervals.get(pos):
            bands = [(interval_for(intervals, pos, q), y) for q, y in zip(g["pred_challenger"].to_list(),
                                                                          g["label"].to_list())]
            hits = [iv[0] <= y <= iv[1] for iv, y in bands if iv is not None]
            if hits:
                r["interval80_coverage"] = {"challenger": round(sum(hits) / len(hits), 4)}
        sp = g.filter(pl.col("_starter"))
        r["starters_pool"] = {"n": sp.height, "mae": {m: metrics(_arr(sp, "label"), _arr(sp, f"pred_{m}"), True)["mae"]
                                                      for m in ("challenger", "champion")}}
        rep["positions"][pos] = r
    gate = {}
    for pos in positions:
        crit = wl.shadow_criteria(rep, pos)
        gate[pos] = {"status": wl.readiness_from(crit), "criteria": crit,
                     "blocking": [c["explanation"] for c in crit if c["result"] != wl.PASS]}
    return {"report": rep, "gate": gate}


COHORTS = {FR: ("ftn_covered", lambda pl: pl.col("in_ftn_games") > 0),
           DCT: ("qb_backup_on_chart", lambda pl: (pl.col("position") == "QB") & (pl.col("in_dc_tier") > 1)),
           AWD: ("decline_regime", lambda pl: pl.col("in_aw_streak_d") <= -2)}


def score(args, store=None) -> dict:
    """Score every frozen week that is fully final (in the store with labels). Reads files only."""
    import polars as pl
    from app.analytics import challenger as ch
    from app.analytics.forecast_backtest import _arr, metrics

    paths = sorted((out_dir(args) / "ledger").glob("s*-pregame.json"))
    if store is None:
        _, _, store, _ = build(args)
    ledgers = [json.loads(p.read_text()) for p in paths]
    lab = store.filter(pl.col("label").is_not_null()).select("key", "season", "week", "gsis_id", "position", "label",
                                                             "pred_A1")
    p = lab.join(ledger_frame(ledgers), on=["key", "gsis_id"], how="inner")
    final = sorted(set(p["key"].to_list()))
    cfg = ch.ChallengerConfig.load()
    out = {"ledgers": [x.name for x in paths], "scored_weeks": final, "candidates": {}}
    for name in CANDIDATES:
        spec_path = out_dir(args) / "frozen" / f"{name}.json"
        positions = json.loads(spec_path.read_text())["positions"] if spec_path.exists() else []
        res = gate_report(p, name, positions, cfg.intervals)
        cname, expr = COHORTS[name]
        c = p.filter(expr(pl).fill_null(False) & pl.col(f"pred_{SHORT[name]}").is_not_null()
                     & pl.col("pred_ifp2").is_not_null())
        res["cohort"] = {"name": cname, "n": c.height,
                         "candidate": metrics(_arr(c, "label"), _arr(c, f"pred_{SHORT[name]}")),
                         "ifp2": metrics(_arr(c, "label"), _arr(c, "pred_ifp2")), "gating": False}
        res["alongside_production"] = alongside_production(args, p, name)
        out["candidates"][name] = res
    return out


def alongside_production(args, p, name: str) -> dict:
    """Cross-check on identical rows: the candidate, its same-run ifp-2 refit and the production ifp-2.0.0
    actually frozen before kickoff (the weekly ledger's earliest pregame run)."""
    import ctx_shadow as cs
    import polars as pl

    col = f"pred_{SHORT[name]}"
    rows = []
    for k in sorted(set(p["key"].to_list())):
        prod = cs._production_rows(args.weekly_dir, k // 100, k % 100)
        for r in p.filter((pl.col("key") == k) & pl.col(col).is_not_null() & pl.col("pred_ifp2").is_not_null()
                          ).iter_rows(named=True):
            if r["gsis_id"] in prod:
                rows.append((r["label"], r[col], r["pred_ifp2"], prod[r["gsis_id"]]))
    if not rows:
        return {"n": 0}
    out = {"n": len(rows)}
    for i, m in ((1, "candidate"), (2, "ifp2_same_run"), (3, "ifp2_production")):
        err = [r[i] - r[0] for r in rows]
        out[m] = {"mae": round(sum(abs(e) for e in err) / len(rows), 4), "bias": round(sum(err) / len(rows), 4)}
    return out


def run_score(args, *, now: datetime | None = None, scorer=None) -> dict:
    """Score the frozen ledgers (scores.json, replaced). Never raises; recorded write-once under runs/."""
    now = now or _utcnow()
    scorer = scorer or score
    rec = {"schema": SCHEMA, "kind": "score", "candidates": list(CANDIDATES), "season": args.season, "week": 0,
           "started_at": now.isoformat(), "dry_run": not args.write, "warnings": [], "notes": []}
    ledgers = sorted((out_dir(args) / "ledger").glob("s*-pregame.json"))
    try:
        if not ledgers:
            rec.update(status="no_ledgers", notes=["no signal-shadow ledger frozen yet: nothing to score"])
            return rec                                  # not recorded: nothing was attempted
        if not args.write:
            rec.update(status="would_score", ledgers=[x.name for x in ledgers])
            return rec
        res = scorer(args)
        rec["gate"] = {n: {p: g["status"] for p, g in r["gate"].items()} for n, r in res["candidates"].items()}
        rec["scored_weeks"] = len(res["scored_weeks"])
        rec["status"] = "scored"
        _replace_json(out_dir(args) / "scores.json", {"scored_at": now.isoformat(), **res})
    except BaseException as err:      # noqa: BLE001
        if isinstance(err, KeyboardInterrupt):
            raise
        rec.update(status="failed", error=f"{err.__class__.__name__}: {err}")
        rec["warnings"].append(f"scoring the signal-shadow ledgers failed: {err}")
    rec["finished_at"] = _utcnow().isoformat()
    rec["record"] = str(_record(args, rec, now))
    return rec


# ----------------------------------------------------------------------------- commands


def _summary(rec: dict) -> dict:
    keep = ("kind", "season", "week", "status", "reason", "ledger", "ledger_sha256", "error", "record", "warnings",
            "notes", "gate", "scored_weeks")
    out = {k: rec[k] for k in keep if k in rec}
    if "decision" in rec:
        out["decision"] = {k: rec["decision"][k] for k in ("action", "reason", "open_teams", "imminent_teams")}
    return out


def cmd_prepare(args, now: datetime) -> dict:
    """One-time / weekly warm-up (run by `cycle`): fetch missing raw snapshots (earlier seasons once, the
    current season again) and build the store cache, so the Sunday freeze only rebuilds incrementally."""
    got = fetch(args, args.season)
    _, _, store, prov = build(args)
    return {"kind": "prepare", "status": "ok", "warnings": got["warnings"], "notes": [],
            "store_rows": store.height, "sources_complete": prov["sources_complete"]}


def cmd_pregame(args, now: datetime) -> dict:
    import ctx_shadow as cs
    week = args.week if args.week is not None else cs._target_week(args, now)
    if week is None:
        return {"kind": "pregame", "status": "no_upcoming_week", "warnings": [], "notes": []}
    return _summary(run_pregame(args, args.season, week, now=now))


def cmd_score(args, now: datetime) -> dict:
    return _summary(run_score(args, now=now))


def cmd_cycle(args, now: datetime) -> dict:
    """learning_cycle.py's stage: score the frozen weeks, record a missed previous week, then try the target
    week's pregame freeze (usually deferred on Tue-Thu)."""
    import ctx_shadow as cs
    week = args.week if args.week is not None else cs._target_week(args, now)
    out = {"score": _summary(run_score(args, now=now))}
    prev = week - 1 if week else cs._last_week(args)
    missed = check_missed(args, args.season, prev, now=now) if prev else None
    out["missed"] = missed
    out["prepare"] = None
    if week and args.write and (args.season, week) >= FIRST_PROSPECTIVE and not ledger_path(args, args.season,
                                                                                               week).exists():
        try:        # warm-up: earlier seasons' raw snapshots once, so the Sunday freeze downloads one season only
            got = fetch(args, args.season, refresh_current=False)
            out["prepare"] = {"warnings": got["warnings"]}
        except Exception as err:      # noqa: BLE001
            out["prepare"] = {"warnings": [f"raw snapshot warm-up failed: {err}"]}
    out["pregame"] = (_summary(run_pregame(args, args.season, week, now=now)) if week
                      else {"status": "no_upcoming_week", "warnings": [], "notes": []})
    out["warnings"] = [*out["score"]["warnings"], *((out["prepare"] or {}).get("warnings") or []),
                       *out["pregame"]["warnings"]]
    if missed:
        out["warnings"].append(f"week {missed['week']} started without a frozen signal-shadow ledger (recorded as missed)")
    out["status"] = "failed" if "failed" in (out["score"]["status"], out["pregame"]["status"]) else "ok"
    return out


def cmd_status(args, now: datetime) -> dict:
    d = out_dir(args)
    runs = sorted((d / "runs").glob("*.json"), key=lambda x: x.stat().st_mtime) if (d / "runs").exists() else []
    scores = d / "scores.json"
    return {"frozen_specs": sorted(x.name for x in (d / "frozen").glob("*.json")),
            "ledgers": sorted(x.name for x in (d / "ledger").glob("s*-pregame.json")),
            "missed": sorted(x.name for x in (d / "missed").glob("*.json")),
            "latest_run": json.loads(runs[-1].read_text()) if runs else None,
            "gate": ({n: {p: g["status"] for p, g in r["gate"].items()}
                      for n, r in json.loads(scores.read_text())["candidates"].items()} if scores.exists() else None)}


def parse(argv=None):
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["prepare", "pregame", "predict", "score", "cycle", "status"])
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--week", type=int, help="default: the upcoming week (earliest with a game still to kick off)")
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    ap.add_argument("--weekly-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"),
                    help="the weekly learning output holding the frozen production ifp-2 ledger")
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--rebuild", action="store_true", help="rebuild the store cache")
    ap.add_argument("--now", help="evaluate the freeze rule at this ISO time; implies --dry-run")
    ap.add_argument("--dry-run", action="store_true", help="decide and print only: no fetch, forecast or record")
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
        print(f"WARNING: signal shadows: {w}", file=sys.stderr)
    if out.get("status") == "failed":
        return EXIT_FAILED
    return EXIT_WARNINGS if out.get("warnings") else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
