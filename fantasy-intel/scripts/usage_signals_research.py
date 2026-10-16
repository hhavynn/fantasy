"""Research: FTN first-read targets and timestamped depth charts vs the ifp-2 challenger
(usage-signals research, research only; see docs/research/first_read_depth_chart_signals.md).
Offline and read-only on both databases; prints aggregates.

    cd backend
    uv run python ../scripts/usage_signals_research.py fetch    # raw nflverse FTN / depth / pbp snapshots
    uv run python ../scripts/usage_signals_research.py audit    # source coverage + read-code semantics audit
    uv run python ../scripts/usage_signals_research.py run      # experiments A / B / C -> report.json
    uv run python ../scripts/usage_signals_research.py ablation # depth group B: leave-one-out + cohorts

Inputs as in challenger_research.py (the ifp-1.0.0 selection.json in --ifp-dir, play-by-play exits
in --exits-dir/pbp_exits) plus the raw snapshots written by `fetch` (content-addressed, never
overwritten; a corrected upstream file becomes a new snapshot). Outputs go to
<ifp-dir>/research/usage_signals/ (git-ignored). Every cache and report carries an input fingerprint
and later commands refuse stale ones. Never writes the ifp-2 config, a production artifact or a ledger.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import challenger_research as cr  # noqa: E402

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.analytics import challenger as ch  # noqa: E402
from app.analytics import usage_signals as us  # noqa: E402
from app.analytics.forecast_backtest import build_store, completed_weeks, pair_decisions  # noqa: E402
from app.analytics.forecast_features import POSITIONS  # noqa: E402
from app.analytics.projection_backtest import _kickoff  # noqa: E402
from app.config import get_settings  # noqa: E402

PBP_COLUMNS = ["game_id", "play_id", "season", "week", "season_type", "posteam", "defteam", "play_type", "pass_attempt",
               "sack", "two_point_attempt", "receiver_player_id", "passer_player_id", "air_yards", "complete_pass",
               "qb_dropback"]
FTN_FROM, DEPTH_FROM = 2022, 2019
STORE_MODULES = ["app.analytics.challenger", "app.analytics.forecast_features", "app.analytics.forecast_backtest"]


def _out(args) -> Path:
    p = args.ifp_dir / "research" / "usage_signals"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _raw(args) -> Path:
    return _out(args) / "raw"


def _code_hash(mods: list[str]) -> str:
    return hashlib.sha256(b"".join(Path(sys.modules[m].__file__).read_bytes() for m in mods)).hexdigest()[:16]


# ----------------------------------------------------------------------------- fetch


def cmd_fetch(args) -> dict:
    import nflreadpy as nfl

    root, out = _raw(args), {}
    for s in range(FTN_FROM, args.season + 1):
        out[f"ftn_{s}"] = us.write_snapshot(root, "ftn", s, nfl.load_ftn_charting([s]))
    for s in range(DEPTH_FROM, args.season + 1):
        out[f"depth_{s}"] = us.write_snapshot(root, "depth", s, nfl.load_depth_charts([s]))
        p = nfl.load_pbp([s])
        out[f"pbp_{s}"] = us.write_snapshot(root, "pbp", s, p.select([c for c in PBP_COLUMNS if c in p.columns]))
    return {k: {"sha": v["sha"], "rows": v["rows"]} for k, v in out.items()}


def _load(args, family: str, seasons) -> pl.DataFrame:
    frames = []
    for s in seasons:
        df = us.read_snapshot(_raw(args), family, s)
        if df is None:
            raise SystemExit(f"no {family} snapshot for {s}: run `fetch` first")
        frames.append(df)
    return pl.concat(frames, how="diagonal_relaxed")


# ----------------------------------------------------------------------------- shared inputs


def _sources(args):
    return cr.sources(SimpleNamespace(live_db=args.live_db, history_db=args.history_db, season=args.season,
                                      out=args.exits_dir))


def _games(src) -> pl.DataFrame:
    g = src.games.with_columns(pl.struct("gameday", "gametime").map_elements(
        lambda r: _kickoff(r), return_dtype=pl.Datetime("us", "UTC")).alias("kickoff"))
    return g


def store_fingerprint(args, src, scoring_fp: str, weeks) -> str:
    parts = {"season": args.season, "weeks": weeks, "scoring": scoring_fp, "polars": pl.__version__,
             "code": _code_hash(STORE_MODULES),
             **{n: us.frame_hash(getattr(src, n)) for n in ("panel", "games", "defense", "injuries", "exits", "rosters")}}
    return us.fingerprint(parts)


def full_store(args, src, scoring_fp: str) -> tuple[pl.DataFrame, str]:
    """The ifp-2 feature store, cached and reused only when its input fingerprint still matches."""
    weeks = completed_weeks(src, list(range(ch.TRAIN_START, args.season + 1)))
    fp = store_fingerprint(args, src, scoring_fp, weeks)
    path = _out(args) / f"store-{fp}.parquet"
    if path.exists() and not args.rebuild:
        return pl.read_parquet(path), fp
    store = ch.add_last_game(build_store(src, weeks), src.panel)
    _write_once(path, store, {"fingerprint": fp, "rows": store.height, "weeks": len(weeks)})
    return store, fp


def _write_once(path: Path, df: pl.DataFrame, meta: dict) -> None:
    """Caches are content-addressed by their input fingerprint (the file name) and written
    atomically, so a concurrent run with other inputs can never swap a file under a reader."""
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    df.write_parquet(tmp)
    path.with_suffix(".meta.json").write_text(json.dumps(meta, default=str))
    tmp.replace(path)


def signal_fingerprint(args, store_fp: str) -> str:
    return us.signal_cache_fingerprint(store_fp, us.manifest_fingerprint(_raw(args), ("ftn", "depth", "pbp")),
                                       args.semantics, args.cutoff_hours)


def signal_store(args, src, store: pl.DataFrame, store_fp: str) -> tuple[pl.DataFrame, str, dict, Path]:
    """Store + FTN features + modern depth features + outcome labels, cached by fingerprint
    (signals-<fingerprint>.parquet: workers read exactly the file their run built)."""
    fp = signal_fingerprint(args, store_fp)
    path = _out(args) / f"signals-{fp}.parquet"
    meta = path.with_suffix(".meta.json")
    if path.exists() and meta.exists() and not args.rebuild:
        return pl.read_parquet(path), fp, json.loads(meta.read_text())["audit"], path
    seasons = range(FTN_FROM, args.season + 1)
    tg, ftn_audit = us.classify_targets(_load(args, "pbp", seasons), _load(args, "ftn", seasons), args.semantics)
    pr, tr = us.game_reads(tg)
    app = us.appearance_reads(src.panel, pr, tr)
    parts = []
    for (s, w), rows in store.filter(pl.col("season") >= FTN_FROM).group_by("season", "week", maintain_order=True):
        parts.append(us.ftn_features(app, int(s), int(w), rows.select("gsis_id", "position"))
                     .with_columns(pl.lit(int(s) * 100 + int(w)).cast(pl.Int64).alias("key")))
    ftn = pl.concat(parts)
    snaps, dnorm = us.normalize_modern(_load(args, "depth", range(us.MODERN_FROM, args.season + 1)))
    tgt = store.filter(pl.col("season") >= us.MODERN_FROM).select("season", "week", "gsis_id", "position", "team")
    dc, dc_audit = us.depth_features(snaps, _games(src), tgt, args.cutoff_hours,
                                     store.select("season", "week", "gsis_id", "snap_pct"))
    out = (store.join(ftn, on=["key", "gsis_id"], how="left")
           .join(dc.drop("snap_dt", "dc_cutoff"), on=["season", "week", "gsis_id"], how="left"))
    out = us.outcome_labels(out, src.panel)
    audit = {"ftn_targets": ftn_audit, "depth_normalize": dnorm, "depth_join": dc_audit}
    _write_once(path, out, {"fingerprint": fp, "rows": out.height, "semantics": args.semantics,
                            "cutoff_hours": args.cutoff_hours, "audit": audit})
    return out, fp, audit, path


def _check_selection(args, scoring_fp: str) -> dict:
    try:
        return us.check_selection(json.loads((args.ifp_dir / "selection.json").read_text()), scoring_fp)
    except ValueError as err:
        raise SystemExit(str(err)) from err


# ----------------------------------------------------------------------------- audit


def cmd_audit(args) -> dict:
    t0 = time.time()
    src, scoring = _sources(args)
    games = _games(src)
    seasons = list(range(FTN_FROM, args.season + 1))
    pbp, ftn = _load(args, "pbp", seasons), _load(args, "ftn", seasons)
    out: dict = {"created_at": datetime.now(timezone.utc).isoformat(), "scoring_fingerprint": scoring.fingerprint,
                 "raw_manifest": us.manifest_fingerprint(_raw(args), ("ftn", "depth", "pbp"))}
    # ---- FTN coverage and read codes
    reg = pbp.filter(pl.col("season_type") == "REG")
    reg_games = reg.select("season", "game_id").unique()
    ftn_games = ftn.select(pl.col("season").cast(pl.Int64), pl.col("nflverse_game_id").alias("game_id")).unique()
    cov = []
    for s in seasons:
        rg = set(reg_games.filter(pl.col("season") == s)["game_id"])
        fg = set(ftn_games.filter(pl.col("season") == s)["game_id"])
        cov.append({"season": s, "reg_games_pbp": len(rg), "ftn_games": len(fg), "reg_games_charted": len(rg & fg),
                    "ftn_games_not_reg": len(fg - rg)})
    out["ftn_game_coverage"] = cov
    tg, _ = us.classify_targets(pbp, ftn, "empirical")
    f = ftn.select(pl.col("nflverse_game_id").alias("game_id"), pl.col("nflverse_play_id").cast(pl.Int64).alias("play_id"),
                   pl.col("read_thrown").str.strip_chars().alias("code"), "is_screen_pass", "is_throw_away", "is_rpo")
    pj = pbp.select("season", "game_id", pl.col("play_id").cast(pl.Int64), "play_type", "complete_pass", "air_yards")
    ft = tg.join(f, on=["game_id", "play_id"], how="left").join(pj.drop("season", "air_yards"), on=["game_id", "play_id"], how="left")
    out["read_codes_on_targets"] = (ft.filter(pl.col("charted")).group_by("season", "code").agg(
        pl.len().alias("n"), pl.col("complete_pass").mean().round(3).alias("completion"),
        pl.col("air_yards").mean().round(2).alias("adot"), pl.col("is_screen_pass").mean().round(3).alias("screen"),
        pl.col("is_throw_away").mean().round(3).alias("throwaway"), pl.col("is_rpo").mean().round(3).alias("rpo"))
        .with_columns((pl.col("n") / pl.col("n").sum().over("season")).round(4).alias("share"))
        .sort("season", "n", descending=[False, True]).to_dicts())
    nt = f.join(pj, on=["game_id", "play_id"], how="left").join(
        tg.select("game_id", "play_id", pl.lit(True).alias("_t")), on=["game_id", "play_id"], how="left")
    out["read_codes_on_non_targets"] = (nt.filter(pl.col("_t").is_null() & pl.col("season").is_not_null())
                                        .group_by("season", "code").agg(pl.len().alias("n"),
                                                                        pl.col("play_type").mode().first().alias("top_play_type"))
                                        .sort("season", "n", descending=[False, True]).to_dicts())
    out["whitespace_codes"] = int((ftn["read_thrown"].str.strip_chars() != ftn["read_thrown"]).sum())
    sem = {}
    for s_name in us.SEMANTICS:
        t2, a2 = us.classify_targets(pbp, ftn, s_name)
        sem[s_name] = {"audit": a2, "by_season": t2.filter(pl.col("charted")).group_by("season", "read_class").len()
                       .with_columns((pl.col("len") / pl.col("len").sum().over("season")).round(4).alias("share"))
                       .sort("season", "len", descending=[False, True]).to_dicts()}
    out["semantics"] = sem
    out["ftn_pull_lag_hours"] = us.ftn_pull_lag(ftn, games.select("season", "week", "game_id", "kickoff"))
    out["ftn_date_pulled_by_season"] = (ftn.group_by("season").agg(
        pl.col("date_pulled").min().cast(pl.String).alias("min"), pl.col("date_pulled").max().cast(pl.String).alias("max"),
        pl.col("date_pulled").n_unique().alias("distinct")).sort("season").to_dicts())
    # descriptive: same-game first-read share vs same-game points (NOT a forecast)
    pr, tr = us.game_reads(tg)
    app = us.appearance_reads(src.panel, pr, tr).join(
        src.panel.select("season", "week", "gsis_id", "league_points", "targets", "team_targets"),
        on=["season", "week", "gsis_id"], how="left")
    d = app.filter(pl.col("ftn_usable") & pl.col("position").is_in(["WR", "TE", "RB"]) & (pl.col("t_first") > 0)
                   & (pl.col("team_targets") > 0))
    out["descriptive_same_game"] = {pos: {
        "n": g.height,
        "corr_first_read_share_points": round(float(np.corrcoef(g["p_first"] / g["t_first"], g["league_points"])[0, 1]), 4),
        "corr_target_share_points": round(float(np.corrcoef(g["targets"] / g["team_targets"], g["league_points"])[0, 1]), 4)}
        for (pos,), g in d.group_by(["position"])}
    # ---- depth charts
    dep = []
    for s in range(DEPTH_FROM, args.season + 1):
        raw = us.read_snapshot(_raw(args), "depth", s)
        if s >= us.MODERN_FROM:
            snaps, a = us.normalize_modern(raw)
            days = snaps.select(pl.col("dt").dt.date()).unique().height
            dep.append({"season": s, "schema": "modern (timestamped)", **a, "days_with_snapshot": days,
                        "teams": snaps["team"].n_unique(), "first": str(snaps["dt"].min()), "last": str(snaps["dt"].max())})
        else:
            leg = us.normalize_legacy(raw)
            dep.append({"season": s, "schema": "legacy (weekly, no publication time)", "rows": raw.height,
                        "skill_rows": leg.height, "skill_rows_without_gsis": int(raw.filter(
                            (pl.col("formation") == "Offense") & pl.col("gsis_id").is_null()).height),
                        "weeks": leg["week"].n_unique(), "teams": leg["team"].n_unique()})
    out["depth_coverage"] = dep
    store, store_fp = full_store(args, src, scoring.fingerprint)
    sig, sig_fp, sig_audit, _ = signal_store(args, src, store, store_fp)
    out["store_fingerprint"], out["signal_fingerprint"] = store_fp, sig_fp
    out["signal_join_audit"] = sig_audit
    modern = sig.filter(pl.col("season") >= us.MODERN_FROM)
    out["depth_availability_at_cutoff"] = (modern.group_by("season", "position").agg(
        pl.len().alias("rows"), pl.col("dc_tier").is_not_null().mean().round(4).alias("with_pregame_snapshot"),
        (pl.col("dc_off_chart") == 0).mean().round(4).alias("on_team_chart"),
        pl.col("dc_tier_change").is_not_null().mean().round(4).alias("with_change"),
        pl.col("dc_age_hours").median().round(1).alias("median_age_h"),
        pl.col("dc_age_hours").quantile(0.95).round(1).alias("p95_age_h")).sort("season", "position").to_dicts())
    out["ftn_feature_coverage"] = (sig.filter(pl.col("season") >= FTN_FROM).group_by("season", "position").agg(
        pl.len().alias("rows"), pl.col("fr_team_share").is_not_null().mean().round(4).alias("with_ftn"))
        .sort("season", "position").to_dicts())
    # legacy timing diagnostic: chart starter vs same-game / previous-game snap leader
    lead_pos = ["QB", "RB", "TE"]
    leg_rows = []
    for s in range(2021, us.MODERN_FROM):
        raw = us.read_snapshot(_raw(args), "depth", s)
        st = (raw.filter((pl.col("formation") == "Offense") & (pl.col("game_type") == "REG") & (pl.col("depth_team") == "1")
                         & pl.col("position").is_in(lead_pos) & (pl.col("depth_position") == pl.col("position")))
              .select(pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64), pl.col("club_code").alias("team"),
                      "position", "gsis_id"))
        leg_rows.append(st.filter(pl.len().over("season", "week", "team", "position") == 1))
    mod = (modern.filter(pl.col("position").is_in(lead_pos) & (pl.col("dc_tier") == 1))
           .select("season", "week", "team", "position", "gsis_id")
           .filter(pl.len().over("season", "week", "team", "position") == 1))
    out["depth_timing_diagnostic"] = {
        "legacy_2021_2024": us.starter_agreement(pl.concat(leg_rows), src.panel),
        "modern_pregame_snapshots": us.starter_agreement(mod, src.panel),
        "note": "share of chart starters (QB/RB/TE, unique starter) who led their position in snaps in the SAME game "
                "vs the team's PREVIOUS game. Modern = latest snapshot strictly before kickoff - cutoff."}
    out["runtime_seconds"] = round(time.time() - t0, 1)
    (_out(args) / "audit.json").write_text(json.dumps(ch.round_floats(out), indent=1, default=str))
    return {k: out[k] for k in ("ftn_game_coverage", "depth_coverage", "depth_availability_at_cutoff",
                                "depth_timing_diagnostic", "signal_join_audit", "runtime_seconds")}


# ----------------------------------------------------------------------------- run


def _job(payload):
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    path, weeks, pos, features, lam, hl, label, kind, col = payload
    store = pl.read_parquet(path)
    return us.walk(store, weeks, pos, us.FitSpec(tuple(features), lam, hl), label, kind, col)


def _weeks(sig: pl.DataFrame, seasons) -> list[tuple[int, int]]:
    return ch.window_weeks(sig, seasons)


def _redundancy(df: pl.DataFrame, feats: list[str], base: list[str]) -> dict:
    """Pearson correlations of each candidate with existing usage features, and the R^2 of
    regressing the candidate on all of them (1 = fully redundant)."""
    out = {}
    for (pos,), g in df.group_by(["position"]):
        rep = {}
        for f in feats:
            h = g.select([f, *base]).drop_nulls()
            if h.height < 50:
                continue
            y = h[f].to_numpy()
            x = np.column_stack([np.ones(h.height), *[h[b].to_numpy() for b in base]])
            beta = np.linalg.lstsq(x, y, rcond=None)[0]
            r2 = 1 - ((y - x @ beta) ** 2).sum() / ((y - y.mean()) ** 2).sum()
            rep[f] = {"n": h.height, "r2_on_existing": round(float(r2), 4),
                      "corr": {b: round(float(np.corrcoef(y, h[b].to_numpy())[0, 1]), 4) for b in base}}
        out[pos] = rep
    return out


def cmd_run(args) -> dict:
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing

    t0 = time.time()
    src, scoring = _sources(args)
    _check_selection(args, scoring.fingerprint)
    cfg = ch.ChallengerConfig.load()
    store, store_fp = full_store(args, src, scoring.fingerprint)
    sig, sig_fp, sig_audit, path = signal_store(args, src, store, store_fp)
    groups = {**us.GROUPS, **us.EXPLORATORY_GROUPS}
    jobs, meta = [], []
    windows = {}
    for g, spec in groups.items():
        src_name = spec["source"]
        for wname, seasons in us.WINDOWS[src_name].items():
            if g in us.EXPLORATORY_GROUPS and wname != "development":
                continue
            windows[(g, wname)] = _weeks(sig, seasons)
    labels = {**us.LABELS, **{r: r for r in us.ROLE_LABELS}}
    needed = {}
    for (g, wname), weeks in windows.items():
        for pos in groups[g]["positions"]:
            base = cfg.features(pos)
            spec = cfg.spec(pos)
            lam = spec.lam[pos]
            for lab_name, lab in labels.items():
                if lab_name == "targets" and pos == "QB":
                    continue
                kind = "logistic" if lab in us.ROLE_LABELS else "linear"
                l_ = us.ROLE_LAMBDA if kind == "logistic" else lam
                feats = list(dict.fromkeys([*base, *groups[g]["features"]]))
                for model, fs in (("ifp2", base), (g, feats)):
                    col = f"p_{model}_{lab_name}"
                    jk = (model, pos, lab_name, tuple(weeks))
                    if jk in needed:
                        continue
                    needed[jk] = col
                    jobs.append((str(path), weeks, pos, fs, l_, spec.half_life_weeks, lab, kind, col))
                    meta.append(jk)
    with ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_job, jobs))
    preds = dict(zip(meta, results))
    # sanity: our ifp-2 points walk-forward reproduces the frozen challenger's predictions exactly
    sanity = {}
    report_groups = {}
    keep_cols = ["key", "season", "week", "gsis_id", "position", "team", "label", "pred_A1", "y_targets", "y_opp",
                 *us.ROLE_LABELS, *us.FTN_FEATURES, "ftn_games", *us.DEPTH_FEATURES, "dc_age_hours"]
    pred_frames = []
    for (g, wname), weeks in windows.items():
        spec = groups[g]
        base = sig.filter(pl.col("key").is_in([s * 100 + w for s, w in weeks]) & pl.col("position").is_in(list(spec["positions"])))
        frame = base.select([c for c in keep_cols if c in base.columns])
        for (model, pos, lab_name, wk), p in preds.items():
            if tuple(weeks) != wk or pos not in spec["positions"] or model not in ("ifp2", g):
                continue
            col = f"p_{model}_{lab_name}"
            frame = frame.join(p, on=["key", "gsis_id"], how="left", suffix="_r")
            if f"{col}_r" in frame.columns:
                frame = frame.with_columns(pl.coalesce(col, f"{col}_r").alias(col)).drop(f"{col}_r")
        covered = (pl.col(spec["features"][0]).is_not_null() if spec["source"] != "both"
                   else pl.col("fr_team_share").is_not_null() & pl.col("dc_tier").is_not_null())
        rep = {"seasons": us.WINDOWS[spec["source"]][wname], "weeks": len(weeks), "label": spec["label"],
               "features": spec["features"], "positions": list(spec["positions"])}
        for lab_name, lab in us.LABELS.items():
            m = {"ifp2": f"p_ifp2_{lab_name}", g: f"p_{g}_{lab_name}"}
            if any(c not in frame.columns for c in m.values()):
                continue
            rep[lab_name] = {"pooled": us.compare_regression(frame.filter(pl.col("position") != "QB") if lab_name == "targets"
                                                             else frame, lab, m, "ifp2", covered)}
            for pos in spec["positions"]:
                if lab_name == "targets" and pos == "QB":
                    continue
                rep[lab_name][pos] = us.compare_regression(frame.filter(pl.col("position") == pos), lab, m, "ifp2", covered,
                                                           resamples=500)
        for lab in us.ROLE_LABELS:
            m = {"ifp2": f"p_ifp2_{lab}", g: f"p_{g}_{lab}"}
            d = frame.filter(pl.col(lab).is_not_null() & pl.all_horizontal([pl.col(c).is_not_null() for c in m.values()]))
            rep[lab] = {"population": d.height,
                        **{name: {mn: us.classification_metrics(sub[lab].to_numpy().astype(float),
                                                                sub[c].to_numpy().astype(float)) for mn, c in m.items()}
                           for name, sub in (("all", d), ("covered", d.filter(covered.fill_null(False))))}}
        # Start/Sit pairs on next-week points
        pp = frame.filter(pl.col("p_ifp2_points").is_not_null() & pl.col(f"p_{g}_points").is_not_null())
        rep["start_sit_pairs"] = pair_decisions(pp, ["p_ifp2_points", f"p_{g}_points"])
        report_groups[f"{g}:{wname}"] = rep
        pred_frames.append(frame.with_columns(pl.lit(g).alias("group"), pl.lit(wname).alias("window")))
        if g == "A" and wname == "development":
            chk = ch.challenger_predictions(sig, weeks, cfg).join(
                frame.select("key", "gsis_id", "p_ifp2_points"), on=["key", "gsis_id"], how="inner")
            diff = (chk["pred_ifp2"] - chk["p_ifp2_points"]).abs()
            sanity["ifp2_reproduced_max_abs_diff"] = None if chk.height == 0 else float(diff.max())
            sanity["ifp2_reproduced_rows"] = chk.height
    decisions = {}
    for g in us.GROUPS:
        dev = report_groups.get(f"{g}:development", {})
        rep = report_groups.get(f"{g}:inspected_replay", {})
        decisions[g] = us.decision({k: v["pooled"] for k, v in dev.items() if k in us.LABELS},
                                   {k: v["pooled"] for k, v in rep.items() if k in us.LABELS}, g)
    dev_rows = sig.filter(pl.col("season").is_in(list(us.WINDOWS["ftn"]["development"])) & pl.col("position").is_in(["RB", "WR", "TE"]))
    redundancy = {
        "ftn": _redundancy(dev_rows, us.FTN_FEATURES, ["target_share", "targets_pg", "air_yards_pg", "xfpl_recent",
                                                       "snap_pct", "opp_trend", "opp_share"]),
        "depth": _redundancy(sig.filter(pl.col("season") == 2025), ["dc_tier", "dc_tier_change", "dc_vacancy", "dc_snap_gap"],
                             ["snap_pct", "snap_trend", "opp_share", "xfpl_recent", "h_snap_pct"]),
    }
    preds_all = pl.concat(pred_frames, how="diagonal_relaxed")
    preds_path = _out(args) / f"predictions-{sig_fp}.parquet"
    preds_all.write_parquet(preds_path)
    report = {"schema": "usage-signals-report/v1", "version": us.SIGNALS_VERSION, "status": us.SIGNALS_STATUS,
              "created_at": datetime.now(timezone.utc).isoformat(), "semantics": args.semantics,
              "cutoff_hours": args.cutoff_hours, "scoring_fingerprint": scoring.fingerprint,
              "ifp2_config_hash": cfg.hash(), "store_fingerprint": store_fp, "signal_fingerprint": sig_fp,
              "rules": us.DECISION_RULES, "groups": {g: {k: (list(v) if isinstance(v, tuple) else v) for k, v in s.items()}
                                                     for g, s in groups.items()},
              "windows": {k: {w: list(s) for w, s in v.items()} for k, v in us.WINDOWS.items()},
              "sanity": sanity, "join_audit": sig_audit, "results": report_groups, "decisions": decisions,
              "redundancy": redundancy, "predictions_file": preds_path.name,
              "prospective": "no prospective frozen forecasts exist for these signals; 2025-2026 were inspected by "
                             "earlier work and are not a holdout",
              "runtime_seconds": round(time.time() - t0, 1)}
    (_out(args) / f"report_{args.semantics}_{args.cutoff_hours:g}h.json").write_text(
        json.dumps(ch.round_floats(report), indent=1, default=str))
    return summarize(report)


def cmd_ablation(args) -> dict:
    """Depth group B, development window only: leave-one-feature-out on next-week points and
    expected points, plus a cohort breakdown (from the stored predictions) of where B's gain comes
    from. Diagnostic: it cannot change the pre-registered decision."""
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing

    src, scoring = _sources(args)
    _check_selection(args, scoring.fingerprint)
    cfg = ch.ChallengerConfig.load()
    store, store_fp = full_store(args, src, scoring.fingerprint)
    sig, sig_fp, _, path = signal_store(args, src, store, store_fp)
    rep_path = _out(args) / f"report_{args.semantics}_{args.cutoff_hours:g}h.json"
    rep = json.loads(rep_path.read_text())
    if rep.get("signal_fingerprint") != sig_fp:
        raise SystemExit(f"{rep_path.name} was produced from different inputs: re-run `run`")
    weeks = _weeks(sig, us.WINDOWS["depth_modern"]["development"])
    feats = us.GROUPS["B"]["features"]
    variants = {"B": feats, **{f"B-{f}": [x for x in feats if x != f] for f in feats}}
    jobs, meta = [], []
    for name, fs in variants.items():
        for pos in POSITIONS:
            spec = cfg.spec(pos)
            for lab_name in ("points", "opp"):
                jobs.append((str(path), weeks, pos, list(dict.fromkeys([*cfg.features(pos), *fs])), spec.lam[pos],
                             spec.half_life_weeks, us.LABELS[lab_name], "linear", f"p_{name}_{lab_name}"))
                meta.append((name, lab_name))
    with ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        res = list(pool.map(_job, jobs))
    preds = pl.read_parquet(_out(args) / rep["predictions_file"]).filter(
        (pl.col("group") == "B") & (pl.col("window") == "development"))
    frame = preds
    for (name, lab_name), p in zip(meta, res):
        col = f"p_{name}_{lab_name}"
        frame = frame.join(p, on=["key", "gsis_id"], how="left", suffix="_r")
        if f"{col}_r" in frame.columns:
            frame = frame.with_columns(pl.coalesce(col, f"{col}_r").alias(col)).drop(f"{col}_r")
    out = {"window": us.WINDOWS["depth_modern"]["development"], "loo": {}}
    for lab_name in ("points", "opp"):
        models = {n: f"p_{n}_{lab_name}" for n in variants}
        models["ifp2"] = f"p_ifp2_{lab_name}"
        r = us.compare_regression(frame, us.LABELS[lab_name], models, "B", None, resamples=500)
        out["loo"][lab_name] = {n: r["all"]["metrics"][n]["mae"] for n in models}
    # cohorts: where does B's points gain come from? (same rows, pregame cohort definitions)
    inj = src.injuries.filter(pl.col("report_status").is_in(["Out", "Doubtful"])) if src.injuries is not None else None
    d = frame.filter(pl.col("p_ifp2_points").is_not_null() & pl.col("p_B_points").is_not_null() & pl.col("label").is_not_null())
    if inj is not None and src.rosters is not None:
        # a same-position teammate listed Out/Doubtful on the week's (pregame) injury report
        # injury-listed teammates usually did NOT appear (absent from the store): take them from rosters
        ros = src.rosters.select("season", "week", "team", "position", pl.col("gsis_id").alias("mate"))
        mates_out = (d.select("season", "week", "team", "position", "gsis_id")
                     .join(ros, on=["season", "week", "team", "position"]).filter(pl.col("mate") != pl.col("gsis_id"))
                     .join(inj.select("season", "week", pl.col("gsis_id").alias("mate")), on=["season", "week", "mate"])
                     .group_by("season", "week", "gsis_id").agg(pl.len().alias("mates_out")))
        d = d.join(mates_out, on=["season", "week", "gsis_id"], how="left").with_columns(pl.col("mates_out").fill_null(0))
    else:
        d = d.with_columns(pl.lit(None, dtype=pl.UInt32).alias("mates_out"))
    cohorts = {"all": pl.lit(True), "qb_backup": (pl.col("position") == "QB") & (pl.col("dc_tier") > 1),
               "qb_starter": (pl.col("position") == "QB") & (pl.col("dc_tier") == 1),
               "tier_changed": pl.col("dc_tier_change").fill_null(0) != 0, "vacancy": pl.col("dc_vacancy").fill_null(0) > 0,
               "chart_vs_snaps_conflict": pl.col("dc_snap_gap").fill_null(0) != 0,
               "teammate_out_or_doubtful": pl.col("mates_out") > 0,
               "none_of_the_above": (pl.col("dc_tier_change").fill_null(0) == 0) & (pl.col("dc_vacancy").fill_null(0) == 0)
               & (pl.col("dc_snap_gap").fill_null(0) == 0) & (pl.col("mates_out") == 0)}
    tot = float(((d["label"] - d["p_ifp2_points"]).abs() - (d["label"] - d["p_B_points"]).abs()).sum())
    out["cohorts"] = {}
    for name, e in cohorts.items():
        g = d.filter(e.fill_null(False))
        gain = float(((g["label"] - g["p_ifp2_points"]).abs() - (g["label"] - g["p_B_points"]).abs()).sum())
        out["cohorts"][name] = {"n": g.height, "mae_ifp2": round(float((g["label"] - g["p_ifp2_points"]).abs().mean()), 3)
                                if g.height else None,
                                "mae_B": round(float((g["label"] - g["p_B_points"]).abs().mean()), 3) if g.height else None,
                                "share_of_total_gain": round(gain / tot, 3) if tot else None}
    out["signal_fingerprint"] = sig_fp
    (_out(args) / f"ablation_{args.semantics}_{args.cutoff_hours:g}h.json").write_text(json.dumps(out, indent=1))
    return out


def summarize(report: dict) -> dict:
    out = {"decisions": {g: d["outcome"] for g, d in report["decisions"].items()}, "sanity": report["sanity"]}
    for name, rep in report["results"].items():
        g = name.split(":")[0]
        s = {}
        for lab in us.LABELS:
            if lab in rep:
                a = rep[lab]["pooled"]["all"]
                if a.get("n"):
                    s[lab] = {"n": a["n"], "mae_ifp2": a["metrics"]["ifp2"]["mae"], f"mae_{g}": a["metrics"][g]["mae"],
                              "diff": a["minus_baseline_mae"][g]}
        s["pairs"] = {m: v["accuracy"] for m, v in rep["start_sit_pairs"]["all"].items()}
        out[name] = s
    return out


def cmd_report(args) -> dict:
    """Print a stored report's summary, refusing one built from inputs that changed since."""
    src, scoring = _sources(args)
    rep = json.loads((_out(args) / f"report_{args.semantics}_{args.cutoff_hours:g}h.json").read_text())
    weeks = completed_weeks(src, list(range(ch.TRAIN_START, args.season + 1)))
    store_fp = store_fingerprint(args, src, scoring.fingerprint, weeks)
    if rep.get("store_fingerprint") != store_fp or rep.get("signal_fingerprint") != signal_fingerprint(args, store_fp):
        raise SystemExit("report was produced from different inputs: re-run `run`")
    return summarize(rep)


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["fetch", "audit", "run", "report", "ablation"])
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"),
                    help="directory containing pbp_exits/ (weekly_learning.py sync-exits)")
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--semantics", choices=list(us.SEMANTICS), default="empirical")
    ap.add_argument("--cutoff-hours", type=float, default=1.5,
                    help="depth snapshot must be strictly before kickoff minus this many hours")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--workers", type=int, default=max(1, min(4, (os.cpu_count() or 2) - 1)))
    args = ap.parse_args(argv)
    result = {"fetch": cmd_fetch, "audit": cmd_audit, "run": cmd_run, "report": cmd_report,
              "ablation": cmd_ablation}[args.command](args)
    print(json.dumps(result, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
