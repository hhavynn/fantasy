"""Research: injury timeline + workload handoff challenger (ifp-iwh, research only; see
docs/research/injury_workload_handoff.md). Offline, read-only on both databases.

    cd backend
    uv run python ../scripts/injury_workload_research.py build      # ifp-2 store + handoff/timeline columns (cached)
    uv run python ../scripts/injury_workload_research.py develop    # ifp-iwh-0.1.0 only (refused under 0.1.1)
    uv run python ../scripts/injury_workload_research.py evaluate   # ifp-iwh-0.1.0 only (refused under 0.1.1)
    uv run python ../scripts/injury_workload_research.py ros        # empirical return rates vs ROS p_play
    uv run python ../scripts/injury_workload_research.py cases      # named live player-weeks (week 6 / 7 of 2026)
    uv run python ../scripts/injury_workload_research.py predict --week W   # ifp-iwh-0.1.1 pregame ledger (write-once)
    uv run python ../scripts/injury_workload_research.py prospective        # score ledgers of fully final weeks

ifp-iwh-0.1.0 was adjudicated once (retain_baseline); its code is commit d6fed66. ifp-iwh-0.1.1 is judged only
on write-once pregame ledgers from 2026 week 7 (injury_prospective.PROSPECTIVE_RULES), so `develop` and
`evaluate` refuse to run under it: re-running either would re-adjudicate a historical window.

Outputs: <ifp-dir>/research/injury_workload/ (git-ignored). Never writes ifp-1/ifp-2 artifacts, the weekly
ledger, the registry or any production table.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import challenger_research as cr  # noqa: E402
import context_research as cxr  # noqa: E402
import hgb_research as hr  # noqa: E402

import duckdb  # noqa: E402
import polars as pl  # noqa: E402

from app.analytics import challenger as ch  # noqa: E402
from app.analytics import injury_challenger as ic  # noqa: E402
from app.analytics import injury_prospective as ip  # noqa: E402
from app.analytics import injury_workload as iw  # noqa: E402
from app.analytics.forecast_backtest import pair_decisions  # noqa: E402
from app.analytics.forecast_features import POSITIONS  # noqa: E402
from app.config import get_settings  # noqa: E402

HO_COLS = [c for c in iw.HANDOFF_SCHEMA if c != "gsis_id"]
TL_COLS = ["designation", "status_class", "missed_team_games", "missed_injured", "practice_status", "return_evidence"]


def _out(args) -> Path:
    p = args.ifp_dir / "research" / "injury_workload"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _codes(args) -> pl.DataFrame | None:
    parts = []
    for path in (args.history_db, args.live_db):
        if path.exists():
            con = duckdb.connect(str(path), read_only=True)
            try:
                c = iw.load_roster_codes(con)
            finally:
                con.close()
            if c is not None:
                parts.append(c)
    return pl.concat(parts).unique(["season", "week", "gsis_id"]) if parts else None


def _code_hash() -> str:
    h = hashlib.sha256()
    for f in (iw.__file__,):
        h.update(Path(f).read_bytes())
    return h.hexdigest()[:16]


def build(args) -> tuple[pl.DataFrame, dict]:
    """The ifp-2 feature store of the context study (same builder, same fully final weeks) plus, per week, the
    handoff (ho_*) and timeline (tl_*) columns and the week's actual carry/target shares (evaluation only).
    Cached by the context store fingerprint + this module's code hash."""
    store, prov = cxr.build(args)
    fp = hashlib.sha256(json.dumps({"store": prov["fingerprint"], "code": _code_hash()}).encode()).hexdigest()[:16]
    path, meta = _out(args) / "store.parquet", _out(args) / "store.meta.json"
    if path.exists() and meta.exists() and not args.rebuild and json.loads(meta.read_text())["fingerprint"] == fp:
        return pl.read_parquet(path), {**prov, "iwh_fingerprint": fp}
    src, _ = hr._sources(args)
    codes = _codes(args)
    t = time.time()
    weeks = sorted({(int(s), int(w)) for s, w in store.select("season", "week").unique().iter_rows()})
    parts = []
    for i, (season, week) in enumerate(weeks):
        tl = iw.injury_timeline(src, season, week, roster_codes=codes)
        ho = iw.workload_handoff(src, season, week, timeline=tl, roster_codes=codes)
        part = (tl.select("gsis_id", *[pl.col(c).alias(f"tl_{c}") for c in TL_COLS])
                .join(ho, on="gsis_id", how="full", coalesce=True).with_columns(pl.lit(season * 100 + week).alias("key")))
        parts.append(part)
        if i % 20 == 0:
            print(f"  handoff {season} w{week}: {time.time() - t:.0f}s", file=sys.stderr)
    extra = pl.concat(parts, how="diagonal_relaxed")
    actual = src.panel.select((pl.col("season") * 100 + pl.col("week")).alias("key"), "gsis_id",
                              pl.when(pl.col("team_rush_attempts") > 0).then(pl.col("carries") / pl.col("team_rush_attempts"))
                              .alias("act_car_share"),
                              pl.when(pl.col("team_targets") > 0).then(pl.col("targets") / pl.col("team_targets"))
                              .alias("act_tgt_share"))
    store = store.join(extra, on=["key", "gsis_id"], how="left").join(actual, on=["key", "gsis_id"], how="left")
    store.write_parquet(path)
    meta.write_text(json.dumps({"fingerprint": fp, "rows": store.height, "weeks": len(weeks),
                                "build_seconds": round(time.time() - t, 1), "roster_codes": codes is not None}))
    return store, {**prov, "iwh_fingerprint": fp}


# ----------------------------------------------------------------------------- reports


def _ci(d: dict) -> list | None:
    return None if d.get("ci95") is None else [round(x, 4) for x in d["ci95"]]


def report(p: pl.DataFrame) -> dict:
    """Identical-row comparison, per position and pooled, with every cohort (MAE, RMSE, bias, MSE/MAE diff CIs,
    share errors where measurable)."""
    lab = p.filter(pl.col("label").is_not_null() & pl.col("pred_ifp2").is_not_null() & pl.col("pred_iwh").is_not_null())
    out = {"population": {"common": lab.height, "weeks": lab.select("key").n_unique()}}
    flt = ic.cohorts()
    for pos in [*POSITIONS, "ALL"]:
        g = lab if pos == "ALL" else lab.filter(pl.col("position") == pos)
        r = {"base": ic.point_metrics(g, "pred_ifp2"), "cand": ic.point_metrics(g, "pred_iwh")}
        mse, mae = ic.week_bootstrap_diff(g, "pred_iwh", "pred_ifp2"), ic.week_bootstrap_diff(
            g, "pred_iwh", "pred_ifp2", loss="absolute")
        r.update(mse_diff=round(mse["diff"], 4) if mse["diff"] is not None else None, mse_ci=_ci(mse),
                 mae_diff=round(mae["diff"], 4) if mae["diff"] is not None else None, mae_ci=_ci(mae), cohorts={})
        for c, e in flt.items():
            cg = g.filter(e.fill_null(False))
            if cg.height == 0:
                continue
            rr = {"n": cg.height, "base": ic.point_metrics(cg, "pred_ifp2"), "cand": ic.point_metrics(cg, "pred_iwh")}
            if cg.height >= 30 and cg.select("key").n_unique() >= 5:
                m1 = ic.week_bootstrap_diff(cg, "pred_iwh", "pred_ifp2")
                m2 = ic.week_bootstrap_diff(cg, "pred_iwh", "pred_ifp2", loss="absolute")
                rr.update(mse_diff=round(m1["diff"], 4), mse_ci=_ci(m1), mae_diff=round(m2["diff"], 4), mae_ci=_ci(m2))
            share = _share_errors(cg)
            if share:
                rr["share_mae"] = share
            rr["actual_mean_points"] = float(cg["label"].mean())
            r["cohorts"][c] = rr
        out[pos] = r
    out["pairs"] = pair_decisions(lab, ["pred_iwh", "pred_ifp2"])
    return out


def _share_errors(g: pl.DataFrame) -> dict | None:
    """Mean absolute error of the week's actual share: handoff expected share vs the player's raw last-5 share
    (rush_share / target_share, ifp-2's own inputs). Carries for RBs, targets for everyone."""
    out = {}
    rb = g.filter((pl.col("position") == "RB") & pl.col("act_car_share").is_not_null() & pl.col("ho_car_share").is_not_null()
                  & pl.col("rush_share").is_not_null())
    if rb.height >= 20:
        out["carry_share"] = {"n": rb.height,
                              "handoff": float((rb["act_car_share"] - rb["ho_car_share"]).abs().mean()),
                              "last5": float((rb["act_car_share"] - rb["rush_share"]).abs().mean())}
    t = g.filter(pl.col("act_tgt_share").is_not_null() & pl.col("ho_tgt_share").is_not_null()
                 & pl.col("target_share").is_not_null() & (pl.col("position") != "QB"))
    if t.height >= 20:
        out["target_share"] = {"n": t.height,
                               "handoff": float((t["act_tgt_share"] - t["ho_tgt_share"]).abs().mean()),
                               "last5": float((t["act_tgt_share"] - t["target_share"]).abs().mean())}
    return out or None


def _preds(store: pl.DataFrame, weeks, cfg, adopted=None) -> pl.DataFrame:
    t = time.time()
    preds = ic.walk_forward(store, weeks, cfg, adopted)
    print(f"walk-forward {len(weeks)} weeks: {time.time() - t:.0f}s", file=sys.stderr)
    return store.filter(pl.col("key").is_in([s * 100 + w for s, w in weeks])).join(preds, on=["key", "gsis_id"])


def _frozen_path(args) -> Path:
    return _out(args) / f"frozen_adoption_{ic.HISTORICAL_VERSION}.json"


def _historical_only(command: str) -> None:
    if ic.IWH_VERSION != ic.HISTORICAL_VERSION:
        raise SystemExit(f"`{command}` adjudicates historical windows and ran once for {ic.HISTORICAL_VERSION} (code: "
                         f"commit d6fed66). {ic.IWH_VERSION} is judged only prospectively: use `predict --week W` "
                         "and `prospective`.")


def cmd_build(args) -> dict:
    store, prov = build(args)
    s = store.filter(pl.col("season") >= ch.TRAIN_START)
    cov = {int(k[0]): {"ho_exp_xfpl_null": round(float(g.filter(pl.col("position") != "QB")["ho_exp_xfpl"].is_null().mean()), 4),
                       "rows": g.height} for k, g in s.group_by("season", maintain_order=True)}
    sizes = {c: int(s.filter(e.fill_null(False)).height) for c, e in ic.cohorts().items() if c != "starters_pool"}
    out = {"rows": s.height, **prov, "coverage": cov, "cohort_sizes": sizes}
    (_out(args) / "build_report.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def cmd_develop(args) -> dict:
    _historical_only("develop")
    store, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    dev = _preds(store, ch.window_weeks(store, ic.WINDOWS["development"]), cfg)
    adopted, why = {}, {}
    for pos in POSITIONS:
        if not ic.GROUP[pos]:
            adopted[pos] = False
            continue
        g = dev.filter(pl.col("position") == pos)
        mse = ic.week_bootstrap_diff(g, "pred_iwh", "pred_ifp2")
        mae = ic.week_bootstrap_diff(g, "pred_iwh", "pred_ifp2", loss="absolute")
        b, c = ic.point_metrics(g, "pred_ifp2"), ic.point_metrics(g, "pred_iwh")
        ok = (mse["diff"] < 0 and mse["ci95"][1] < 0 and mae["diff"] <= ic.DEV_MAX_MAE_DIFF
              and abs(c["bias"]) <= abs(b["bias"]) + ic.BIAS_TOLERANCE)
        adopted[pos] = bool(ok)
        why[pos] = {"mse": mse, "mae": mae, "base": b, "cand": c}
    frozen = {"version": ic.IWH_VERSION, "status": ic.IWH_STATUS, "selected_on": ic.WINDOWS["development"],
              "adopted": adopted, "group": ic.GROUP, "rules": ic.DECISION_RULES, "ifp2_config_hash": cfg.hash(),
              "workload_version": iw.WORKLOAD_VERSION, "scoring_fingerprint": prov["scoring_fingerprint"],
              "store_fingerprint": prov["iwh_fingerprint"]}
    frozen["config_hash"] = hashlib.sha256(json.dumps({k: v for k, v in frozen.items() if k != "rules"},
                                                      sort_keys=True, default=str).encode()).hexdigest()[:16]
    frozen["frozen_at"] = datetime.now(timezone.utc).isoformat()
    path = _frozen_path(args)
    if not hr._write_once(path, frozen):
        prev = json.loads(path.read_text())
        if prev["config_hash"] != frozen["config_hash"]:
            raise SystemExit(f"{path} already frozen with a different adoption ({prev['adopted']})")
    rep = report(dev)
    (_out(args) / "development.json").write_text(json.dumps({"report": rep, "selection": why, **prov}, indent=1,
                                                            default=str))
    return {"adopted": adopted, "selection": why, "pooled": {k: rep["ALL"][k] for k in
                                                             ("base", "cand", "mse_diff", "mse_ci", "mae_diff", "mae_ci")}}


def cmd_evaluate(args) -> dict:
    _historical_only("evaluate")
    store, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    frozen = json.loads(_frozen_path(args).read_text())
    if frozen["ifp2_config_hash"] != cfg.hash() or frozen["scoring_fingerprint"] != prov["scoring_fingerprint"]:
        raise SystemExit("frozen adoption was made against a different ifp-2 config or league scoring")
    adopted = frozen["adopted"]
    out = {"frozen": {k: frozen[k] for k in ("adopted", "config_hash", "frozen_at")}, **prov}
    path = _out(args) / "evaluation.json"
    for name in ("confirmation", "exploratory"):
        p = _preds(store, ch.window_weeks(store, ic.WINDOWS[name]), cfg, adopted)
        p.select("key", "gsis_id", "position", "label", "pred_ifp2", "pred_iwh").write_parquet(
            _out(args) / f"{name}_preds.parquet")
        out[name] = {"report": report(p), "verdict": ic.verdict(p, [k for k, v in adopted.items() if v])}
        # descriptive only: the candidate fitted for every skill position regardless of adoption
        allp = _preds(store, ch.window_weeks(store, ic.WINDOWS[name]), cfg, None)
        out[name]["unadopted_descriptive"] = {pos: {k: report(allp)[pos][k] for k in ("base", "cand", "mse_diff", "mse_ci",
                                                                                      "mae_diff", "mae_ci")}
                                              for pos in ("RB", "WR", "TE")}
    if path.exists() and not args.rebuild:
        print(f"note: {path} existed; overwritten (confirmation is deterministic given the frozen adoption)",
              file=sys.stderr)
    path.write_text(json.dumps(out, indent=1, default=str))
    return {n: {"verdict": out[n]["verdict"], "ALL": {k: out[n]["report"]["ALL"][k] for k in
                                                       ("base", "cand", "mse_diff", "mse_ci", "mae_diff", "mae_ci")}}
            for n in ("confirmation", "exploratory")}


# ----------------------------------------------------------------------------- rest of season


def cmd_ros(args) -> dict:
    """Horizon >= 1: (1) the empirical return-rate table fitted on 2019-2022 episodes; (2) for players designated
    Out/Doubtful at a cutoff week W of 2023-2024, the actual appearance rate in the team's next games vs that table
    (calibration of the table on unseen seasons). The current ROS path treats the same players as undesignated."""
    src, _ = hr._sources(args)
    rates = iw.fit_return_rates(src, through_key=2023 * 100 + 1, seasons=(2019, 2020, 2021, 2022))
    test = iw.fit_return_rates(src, through_key=2025 * 100 + 1, seasons=(2023, 2024))
    rows = []
    for k in sorted(rates, key=lambda x: (x[2], str(x[0]), x[1])):
        if k[0] == "ALL":
            rows.append({"position": k[0], "missed_bucket": k[1], "h": k[2], "fit_rate": round(rates[k]["rate"], 3),
                         "fit_n": rates[k]["n"], "test_rate": round(test.get(k, {}).get("rate", float("nan")), 3),
                         "test_n": test.get(k, {}).get("n", 0)})
    out = {"table": rows, "by_position_h1": {k[0]: {"bucket": k[1], "rate": round(v["rate"], 3), "n": v["n"]}
                                             for k, v in rates.items() if k[2] == 1 and k[0] != "ALL"}}
    out["ros_p_play_calibration"] = _ros_calibration(args, src, rates)
    (_out(args) / "ros_return_rates.json").write_text(json.dumps(
        {"fit": {"|".join(map(str, k)): v for k, v in rates.items()},
         "test": {"|".join(map(str, k)): v for k, v in test.items()}, "summary": out}, indent=1))
    return out


def _ros_calibration(args, src, rates: dict) -> dict:
    """For players designated Out/Doubtful at cutoff week W (2023-2024), P(appears in week W+1) as the current
    rest-of-season path computes it (participation.predict_week with feature_week=W: no week-W+1 report, so
    undesignated) vs the empirical return-rate table (fitted on 2019-2022) vs what happened. Players whose team is
    on a bye in W+1 are skipped. Descriptive: the participation artifact's own training seasons may overlap."""
    from app.analytics.participation import ParticipationArtifact, predict_week

    part_path = args.ifp_dir / "participation_ipm-1.0.0.json"
    if not part_path.exists() or src.injuries is None:
        return {"note": "participation artifact or injury reports unavailable"}
    part = ParticipationArtifact.from_dict(json.loads(part_path.read_text()))
    apps = set(src.panel.select("season", "week", "gsis_id").iter_rows())
    rows = []
    for season in (2023, 2024):
        for w in range(2, 18):
            tl = iw.injury_timeline(src, season, w)
            hurt = tl.filter(pl.col("designation").is_in(["out", "doubtful"]) & pl.col("position").is_in(list(iw.SKILL)))
            if hurt.height == 0:
                continue
            nxt = src.games.filter((pl.col("season") == season) & (pl.col("week") == w + 1))
            playing = set(nxt["team"].to_list())
            pp = predict_week(src, part, season, w + 1, feature_week=w)
            for r in hurt.iter_rows(named=True):
                if r["team"] not in playing or r["gsis_id"] not in pp:
                    continue
                rows.append({"position": r["position"], "missed": iw.missed_bucket(r["missed_team_games"]),
                             "p_ros": pp[r["gsis_id"]]["p_play"], "p_rate": iw._return_rate(rates, r, 1),
                             "appeared": int((season, w + 1, r["gsis_id"]) in apps)})
    if not rows:
        return {"n": 0}
    df = pl.DataFrame(rows)

    def summ(g):
        y = g["appeared"].to_numpy()
        return {"n": g.height, "actual_rate": round(float(y.mean()), 3),
                "mean_p_current_ros": round(float(g["p_ros"].mean()), 3),
                "mean_p_return_table": round(float(g["p_rate"].mean()), 3),
                "brier_current_ros": round(float(((g["p_ros"].to_numpy() - y) ** 2).mean()), 4),
                "brier_return_table": round(float(((g["p_rate"].to_numpy() - y) ** 2).mean()), 4)}

    return {"all": summ(df), "by_missed_bucket": {b[0]: summ(g) for b, g in df.group_by("missed")},
            "by_position": {p[0]: summ(g) for p, g in df.group_by("position")}}


# ----------------------------------------------------------------------------- live cases


def cmd_cases(args) -> dict:
    """The named investigation cases on the live data: production ifp-2 rows already stored (old) next to the
    challenger fitted on the same training rows (new), for 2026 week 6 (cutoff 6) and week 7 (cutoff 6, horizon 1,
    with and without the empirical return-rate scenarios)."""
    from app.analytics.forecast_features import features_for_week
    from app.analytics.participation import ParticipationArtifact, predict_week

    store, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    src, _ = hr._sources(args)
    codes = _codes(args)
    live = duckdb.connect(str(args.live_db), read_only=True)
    names = dict(live.execute("SELECT gsis_id, display_name FROM nfl_players").fetchall())
    who = {"00-0034844": "Saquon Barkley", "00-0037258": "Dameon Pierce", "00-0038120": "Breece Hall",
           "00-0039794": "Braelon Allen", "00-0039746": "Will Shipley", "00-0039798": "Isaiah Davis"}
    rates = iw.fit_return_rates(src, through_key=2026 * 100 + 6)
    part_path = args.ifp_dir / "participation_ipm-1.0.0.json"
    part = ParticipationArtifact.from_dict(json.loads(part_path.read_text())) if part_path.exists() else None
    stored = {}
    for r in live.execute("""
            SELECT r.week, m.gsis_id, r.projected_points, r.p_play, r.expected_points, r.coverage_status
            FROM independent_projection_rows r JOIN player_id_map m USING (espn_player_id)
            JOIN (SELECT run_id FROM independent_projection_runs WHERE season = 2026 AND model_version = 'ifp-2.0.0'
                  QUALIFY row_number() OVER (PARTITION BY week ORDER BY created_at DESC) = 1) k USING (run_id)
            WHERE m.gsis_id IN ({})""".format(",".join(f"'{g}'" for g in who))).fetchall():
        stored[(r[0], r[1])] = {"projected_points": r[2], "p_play": r[3], "expected_points": r[4], "coverage": r[5]}
    out = {}
    k6 = 2026 * 100 + 6
    for target, label, rr in ((6, "w6", None), (7, "w7_status_quo", None), (7, "w7_return_scenarios", rates)):
        tl = iw.injury_timeline(src, 2026, target, 6, roster_codes=codes)
        ho = iw.workload_handoff(src, 2026, target, 6, timeline=tl, return_rates=rr, roster_codes=codes)
        games = src.games.filter((pl.col("season") == 2026) & (pl.col("week") == target)).select("team", "opponent",
                                                                                                    "is_home")
        tgt = (tl.filter(pl.col("gsis_id").is_in(list(who))).select("gsis_id", "position", "team").join(games, on="team"))
        feats = features_for_week(src, 2026, 6, tgt).with_columns(pl.lit(2026).alias("season"), pl.lit(6).alias("week"))
        feats = feats.join(ho, on="gsis_id", how="left").with_columns(pl.lit(k6).alias("key"))
        train_store = store
        res = {}
        for pos in ("RB",):
            train = ch.train_rows(train_store, k6, pos)
            rows = feats.filter(pl.col("position") == pos)
            base = ic.fit_candidate(train, cfg, pos, False, k6).predict(rows)
            cand = ic.fit_candidate(train, cfg, pos, True, k6).predict(rows)
            for i, r in enumerate(rows.iter_rows(named=True)):
                t = tl.filter(pl.col("gsis_id") == r["gsis_id"]).to_dicts()[0]
                h = ho.filter(pl.col("gsis_id") == r["gsis_id"]).to_dicts()
                res[who[r["gsis_id"]]] = {
                    "status": t["status_class"], "reason": t["status_reason"], "designation": t["designation"],
                    "stored_ifp2": stored.get((target, r["gsis_id"])), "refit_ifp2_conditional": round(float(base[i]), 2),
                    "iwh_conditional": round(float(cand[i]), 2),
                    "handoff": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in (h[0] if h else {}).items()
                                if k in ("ho_car_share", "ho_base_car_share", "ho_carries", "ho_targets", "ho_exp_xfpl",
                                         "ho_shift_pg", "ho_absence_games", "ho_scenarios")},
                    "explanation": iw.explain(t, h[0] if h else None, names=names)["text"]}
        if part is not None and target == 6:
            pp = predict_week(src, part, 2026, 6)
            for g, n in who.items():
                if n in res and g in pp:
                    res[n]["p_play_now"] = pp[g]["p_play"]
        out[label] = res
    (_out(args) / "cases.json").write_text(json.dumps(out, indent=1, default=str))
    return out


# ----------------------------------------------------------------------------- prospective ledger (ifp-iwh-0.1.1)


def _ledger_dir(args) -> Path:
    return _out(args) / "ledger"


def prospective_spec(cfg_hash: str, scoring_fp: str) -> dict:
    """The pre-registered prospective spec (injury_prospective.PROSPECTIVE_*), hashed. Any edit to the rules,
    thresholds, gate or cohort code, adoption, feature group, handoff version or the ifp-2 config/scoring changes
    config_hash."""
    import inspect

    gate_code = hashlib.sha256("".join([inspect.getsource(ip), inspect.getsource(ic.cohorts),
                                        repr(ic.MECHANISM_COHORTS)]).encode()).hexdigest()[:16]
    spec = {"version": ic.IWH_VERSION, "status": ic.IWH_STATUS, "workload_version": iw.WORKLOAD_VERSION,
            "gate_code": gate_code,
            "adopted": ip.PROSPECTIVE_ADOPTED, "group": ic.GROUP, "rules": ip.PROSPECTIVE_RULES,
            "thresholds": ip.PROSPECTIVE_THRESHOLDS, "first_week": list(ip.PROSPECTIVE_FIRST_WEEK),
            "ifp2_config_hash": cfg_hash, "scoring_fingerprint": scoring_fp}
    spec["config_hash"] = hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return spec


def frozen_prospective(args, cfg_hash: str, scoring_fp: str, *, create: bool) -> dict:
    """The prospective spec, frozen write-once by the first `predict`; every later run must match it exactly.
    `prospective` never creates it (scoring cannot precede the pre-registration)."""
    spec = prospective_spec(cfg_hash, scoring_fp)
    path = _out(args) / f"frozen_{ic.IWH_VERSION}.json"
    if not path.exists():
        if not create:
            raise SystemExit(f"{path.name} does not exist: no {ic.IWH_VERSION} ledger has been written yet")
        hr._write_once(path, {**spec, "frozen_at": datetime.now(timezone.utc).isoformat()})
    prev = json.loads(path.read_text())
    if prev["config_hash"] != spec["config_hash"]:
        raise SystemExit(f"{path.name} was frozen with a different spec, rules, ifp-2 config or scoring "
                         f"({prev['config_hash']} != {spec['config_hash']}); refusing")
    return prev


def check_prospective_week(season: int, week: int) -> None:
    if (season, week) < tuple(ip.PROSPECTIVE_FIRST_WEEK):
        raise SystemExit(f"{ic.IWH_VERSION} is pre-registered from {ip.PROSPECTIVE_FIRST_WEEK[0]} week "
                         f"{ip.PROSPECTIVE_FIRST_WEEK[1]}; refusing a ledger for {season} week {week}")


def open_teams(kickoffs: dict, now: datetime) -> list[str]:
    """Teams whose week-W game has not kicked off at `now` (a team without a known kickoff is never open)."""
    return sorted(t for t, ko in kickoffs.items() if now < ko)


def require_report(injuries: pl.DataFrame | None, season: int, week: int, teams: list[str]) -> pl.DataFrame:
    """The week's injury report rows; refuses (SystemExit) unless every team to be forecast has rows. The ledger is
    write-once, so forecasts made before the report is ingested would be frozen degraded (the timeline would read
    every team as `no_report`). There is no override."""
    rep = None if injuries is None else injuries.filter((pl.col("season") == season) & (pl.col("week") == week))
    have = set() if rep is None else set(rep["team"].drop_nulls().to_list())
    missing = sorted(set(teams) - have)
    if rep is None or rep.height == 0 or missing:
        raise SystemExit(f"the {season} week-{week} injury report is not ingested"
                         + (f" for {', '.join(missing)}" if rep is not None and rep.height and missing else "")
                         + " (run sync_nfl.py first); a pregame ledger is write-once, so nothing was written")
    return rep


def check_reused_artifact(art: dict, config_hash: str, cutoff: int, store_fp: str, name: str) -> None:
    """An interrupted run's artifact is reused only if it was fitted under the same frozen spec, cutoff and training
    store; otherwise the ledger would pair old models with current source hashes."""
    if (art["config_hash"] != config_hash or art["training_cutoff_key"] != cutoff
            or art["store_fingerprint"] != store_fp):
        raise SystemExit(f"{name} was built under a different configuration, cutoff or training store (historical "
                         "inputs changed since): move it aside to refit")


def require_forecasts(rows: list[dict], week: int) -> None:
    """A write-once ledger is never frozen without a single forecast (missing or empty roster ingest)."""
    if not any(r["iwh_points"] is not None for r in rows):
        raise SystemExit(f"no week-{week} forecast rows (roster ingest missing or empty?): nothing written")


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def ledger_rows(feats: pl.DataFrame, models: dict, tl: dict[str, dict], kick: dict, names: dict | None = None) -> list[dict]:
    """One frozen row per player: both models' points on the same feature row (null = abstention: no game in
    the lookback or no model), the timeline/handoff columns the prospective cohorts read, and the explanation."""
    rows = []
    for pos in POSITIONS:
        g = feats.filter(pl.col("position") == pos)
        m = models.get(pos)
        if g.height == 0:
            continue
        ok = g["games_recent"].fill_null(0).to_numpy() > 0
        base = m["ifp2"].predict(g) if m else None
        cand = m["iwh"].predict(g) if m else None
        for i, rec in enumerate(g.iter_rows(named=True)):
            b = float(base[i]) if m is not None and ok[i] else None
            c = float(cand[i]) if m is not None and ok[i] else None
            t = tl.get(rec["gsis_id"])
            h = {k: rec.get(k) for k in HO_COLS}
            rows.append({"gsis_id": rec["gsis_id"], "position": pos, "team": rec["team"],
                         "kickoff": kick[rec["team"]].isoformat(), "games_recent": rec.get("games_recent"),
                         "ifp2_points": b, "iwh_points": c,
                         **{f"tl_{k}": (t or {}).get(k) for k in TL_COLS}, "tl_status_reason": (t or {}).get("status_reason"),
                         **h, "team_changed": rec.get("team_changed"), "weeks_since_last": rec.get("weeks_since_last"),
                         "explanation": None if t is None or pos == "QB" else
                         iw.explain(t, {"gsis_id": rec["gsis_id"], **h}, names=names, baseline_points=b,
                                    challenger_points=c)["text"]})
    return rows


def cmd_predict(args) -> dict:
    """Frozen pregame forecasts (ifp2 and ifp-iwh-0.1.1, same run, same rows) for one week, written once. Teams
    that have kicked off are excluded (never backdated); refused while the week's injury report is not ingested."""
    from app.analytics.forecast_features import features_for_week
    from app.analytics.independent_projections import MIN_TRAIN_ROWS
    from app.analytics.participation import live_population
    from app.analytics.weekly_learning import team_kickoffs

    season, week = args.season, args.week
    check_prospective_week(season, week)
    lpath = _ledger_dir(args) / f"s{season}w{week:02d}-pregame.json"
    if lpath.exists():
        led = json.loads(lpath.read_text())
        return {"ledger": str(lpath), "written": False, "created_at": led["created_at"], "rows": len(led["rows"])}
    src, _ = hr._sources(args)
    kick = team_kickoffs(src, season, week)
    teams = open_teams(kick, datetime.now(timezone.utc))
    if not teams:
        raise SystemExit(f"every week-{week} game has kicked off: no pregame forecast possible")
    require_report(src.injuries, season, week, teams)      # before the slow build: fail fast
    store, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    frozen = frozen_prospective(args, cfg.hash(), prov["scoring_fingerprint"], create=True)
    codes = _codes(args)
    now = datetime.now(timezone.utc)          # after the slow build: never select on a stale clock
    teams = open_teams(kick, now)
    if not teams:
        raise SystemExit(f"every week-{week} game kicked off during the build: nothing written")
    inj = require_report(src.injuries, season, week, teams)
    cutoff = season * 100 + week
    n_train, fitted = {}, {}
    for pos in POSITIONS:
        train = ch.train_rows(store, cutoff, pos)
        assert train.height == 0 or train["key"].max() < cutoff, "training row at or after the forecast week"
        n_train[pos] = train.height
        if train.height < MIN_TRAIN_ROWS:
            continue
        base = ic.fit_candidate(train, cfg, pos, False, cutoff)
        use = bool(ic.GROUP[pos]) and frozen["adopted"].get(pos, False)
        fitted[pos] = {"ifp2": base, "iwh": ic.fit_candidate(train, cfg, pos, True, cutoff) if use else base}
    art = {"schema": "ifp-iwh-research-artifact/v1", "version": ic.IWH_VERSION, "status": ic.IWH_STATUS,
           "workload_version": iw.WORKLOAD_VERSION, "training_cutoff_key": cutoff, "config_hash": frozen["config_hash"],
           "created_at": now.isoformat(), "store_fingerprint": prov["iwh_fingerprint"], "training_rows": n_train,
           "models": {p: {k: m.to_dict() for k, m in v.items()} for p, v in fitted.items()}}
    apath = _out(args) / "artifacts" / f"{ic.IWH_VERSION}-s{season}w{week:02d}.json"
    if not hr._write_once(apath, art):
        art = json.loads(apath.read_text())        # an interrupted earlier run's artifact is reused, never refit
        check_reused_artifact(art, frozen["config_hash"], cutoff, prov["iwh_fingerprint"], apath.name)
        fitted = {p: {k: ch.RidgeModel.from_dict(d) for k, d in v.items()} for p, v in art["models"].items()}
    pop = live_population(src, season, week).filter(pl.col("team").is_in(teams))
    feats = features_for_week(src, season, week, pop)
    tl = iw.injury_timeline(src, season, week, roster_codes=codes)
    ho = iw.workload_handoff(src, season, week, timeline=tl, roster_codes=codes)
    feats = feats.join(ho, on="gsis_id", how="left")
    rows = ledger_rows(feats, fitted, {r["gsis_id"]: r for r in tl.iter_rows(named=True)}, kick)
    # fitting and feature building take time: re-check every kickoff at write time and drop any team whose game
    # has started since, so no row in a "pregame" ledger is ever written after its kickoff
    written_at = datetime.now(timezone.utc)
    still_open = set(open_teams(kick, written_at)) & set(teams)
    rows = [r for r in rows if r["team"] in still_open]
    if not still_open:
        raise SystemExit(f"every remaining week-{week} game kicked off while forecasting: nothing written")
    require_forecasts(rows, week)
    season_inj = src.injuries.filter((pl.col("season") == season) & (pl.col("week") <= week))
    ledger = {"schema": "ifp-iwh-research-ledger/v1", "version": ic.IWH_VERSION, "status": ic.IWH_STATUS,
              "workload_version": iw.WORKLOAD_VERSION, "kind": "pregame", "season": season, "week": week,
              "created_at": written_at.isoformat(), "config_hash": frozen["config_hash"], "artifact": apath.name,
              "training_cutoff_key": cutoff, "teams_excluded_already_started": sorted(set(kick) - still_open),
              "sources": {"injury_report_rows": inj.height,
                          "injury_report_designations": int(inj["report_status"].is_not_null().sum()),
                          "injury_report_sha": hr._frame_hash(inj), "injury_report_timestamped": False,
                          "injuries_season_through_week_sha": hr._frame_hash(season_inj),
                          "rosters_sha": hr._frame_hash(src.rosters), "panel_sha": hr._frame_hash(src.panel),
                          "games_sha": hr._frame_hash(src.games), "roster_codes_sha": hr._frame_hash(codes),
                          "store_fingerprint": prov["iwh_fingerprint"], "scoring_fingerprint": prov["scoring_fingerprint"],
                          "code_hash": hr.code_hash()},
              "rows_sha": _sha(rows), "rows": rows}
    written = hr._write_once(lpath, ledger)
    return {"ledger": str(lpath), "written": written, "rows": len(rows),
            "forecast_rows": sum(r["iwh_points"] is not None for r in rows), "open_teams": len(still_open),
            "teams_excluded_already_started": ledger["teams_excluded_already_started"]}


FROZEN_ROW_COLS = [*[f"tl_{c}" for c in TL_COLS], *HO_COLS, "team_changed", "weeks_since_last"]


def matched_rows(store: pl.DataFrame, ledgers: list[dict], config_hash: str) -> tuple[pl.DataFrame | None, dict]:
    """Ledger rows of fully final weeks (weeks present in the store) under `config_hash`, joined to the store's
    labels on identical (week, player) rows. Cohort columns come from the frozen ledger, never recomputed."""
    parts, status = [], {}
    final = set(store["key"].unique().to_list())
    for led in ledgers:
        k = led["season"] * 100 + led["week"]
        if led.get("config_hash") != config_hash:
            status[k] = "other_config"
        elif _sha(led["rows"]) != led.get("rows_sha"):
            status[k] = "rows_sha_mismatch"
        elif k not in final:
            status[k] = "not_final"
        elif not led["rows"]:
            status[k] = "empty"
        else:
            status[k] = "scored"
            df = pl.DataFrame([{"key": k, "gsis_id": r["gsis_id"], "position": r["position"],
                                "pred_ifp2": r["ifp2_points"], "pred_iwh": r["iwh_points"],
                                **{c: r.get(c) for c in FROZEN_ROW_COLS}} for r in led["rows"]],
                              infer_schema_length=None)
            parts.append(df.with_columns(pl.col("key").cast(pl.Int64), pl.col("pred_ifp2").cast(pl.Float64),
                                         pl.col("pred_iwh").cast(pl.Float64)))
    if not parts:
        return None, status
    frozen = pl.concat(parts, how="diagonal_relaxed")
    keep = [c for c in store.columns if c not in frozen.columns or c in ("key", "gsis_id")]
    return store.select(keep).join(frozen, on=["key", "gsis_id"], how="inner"), status


def cmd_prospective(args) -> dict:
    """Score the write-once ledgers of fully final weeks with the pre-registered gate (ip.prospective_gate)."""
    store, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    frozen = frozen_prospective(args, cfg.hash(), prov["scoring_fingerprint"], create=False)
    paths = sorted(_ledger_dir(args).glob("s*-pregame.json")) if _ledger_dir(args).exists() else []
    p, status = matched_rows(store, [json.loads(x.read_text()) for x in paths], frozen["config_hash"])
    if p is None:
        return {"ledgers": len(paths), "weeks": status, "scored_weeks": 0, "outcome": "insufficient_data"}
    gate = ip.prospective_gate(p)
    out = {"version": ic.IWH_VERSION, "config_hash": frozen["config_hash"], "weeks": status, "gate": gate,
           "report": report(p), **prov}
    (_out(args) / f"prospective_{ic.IWH_VERSION}.json").write_text(json.dumps(out, indent=1, default=str))
    return {"weeks": status, "scored_weeks": gate["weeks"], "outcome": gate["outcome"],
            "per_position": {pos: {"n": v["n"], "checks": v["checks"]} for pos, v in gate["per_position"].items()},
            "pooled_checks": gate["pooled_checks"]}


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["build", "develop", "evaluate", "ros", "cases", "predict", "prospective"])
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--week", type=int)
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args(argv)
    if args.command == "predict" and args.week is None:
        ap.error("predict needs --week")
    cmds = {"build": cmd_build, "develop": cmd_develop, "evaluate": cmd_evaluate, "ros": cmd_ros, "cases": cmd_cases,
            "predict": cmd_predict, "prospective": cmd_prospective}
    print(json.dumps(cmds[args.command](args), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
