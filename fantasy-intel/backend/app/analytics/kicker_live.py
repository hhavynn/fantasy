"""Live week-W kicker projections: apply a frozen kfp artifact to point-in-time features, one row per ESPN kicker.

Never trains. Guards (each an explicit coverage_status with a NULL projection, never a silent zero):
  unsupported_scoring    the league's kicker scoring is unsupported, differs from the artifact's, or failed ESPN
                         reconciliation this season
  uncertain_id_mapping   no high-confidence ESPN -> GSIS mapping
  no_nfl_team            not on an NFL active roster (free agent / released)
  bye                    his team has no game in week W                       (a credible 0)
  withheld_unavailable   ESPN OUT / IR / SUSPENSION, or NFL roster status not ACT/INA   (a credible 0)
  not_expected_kicker    on a roster, but another kicker is his team's expected kicker (backup, practice squad,
                         a departed kicker): he is never given that team's opportunities
  ambiguous_kicker       his team's kicker cannot be identified (two active kickers and no game evidence)
  projected              conditional points + p_play + expected_points

Expected kicker of a team for week W (`expected_kickers`): the kicker of the team's latest completed game if he is
still on its NFL roster as ACT/INA; else the single ACT kicker on the latest weekly roster (a new signing, flagged);
else the depth chart's first kicker; else none.

Cold start: a kicker with no kick in the data gets skill 0 (league average) and the league 50+ share, labeled.
Rest-of-season (projected_from_week W < week T): features as of W, opponent/venue of T, no weather (none is
fetched beyond the next game), flagged experimental.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

import numpy as np
import polars as pl

from ..db.connection import fetch_dicts, table_columns, table_exists, utcnow
from . import kicker_model as km
from . import kicker_weather as kw
from .kicker_backtest import build_dataset
from .kicker_scoring import KickerScoring, league_items, reconcile_with_espn, resolve_kicker_scoring
from .kicker_venues import registry, resolve
from .projection_backtest import _kickoff

WITHHELD_ESPN = {"OUT", "INJURY_RESERVE", "SUSPENSION"}
ACTIVE_ROSTER = {"ACT", "INA"}
EXPERIMENTAL = "rest-of-season kicker forecast (horizon {k}): experimental, not validated beyond the next week"


def candidates(con, season: int, week: int) -> list[dict]:
    return fetch_dicts(con, """
        SELECT fp.espn_player_id, fp.full_name, fp.pro_team, fp.injury_status, fp.on_team_id,
               m.gsis_id, m.match_confidence, m.notes AS mapping_notes
        FROM fantasy_players fp LEFT JOIN player_id_map m USING (espn_player_id)
        WHERE fp.position = 'K'
          AND (coalesce(fp.on_team_id, 0) > 0 OR EXISTS (
                SELECT 1 FROM espn_player_weekly_points e WHERE e.espn_player_id = fp.espn_player_id
                AND e.season = ? AND e.scoring_period = ? AND e.projected_points IS NOT NULL))
        ORDER BY fp.espn_player_id""", [season, week])


OFF_ROSTER = "NOT_ON_LATEST_ROSTER"


def _latest_rosters(con, season: int, week: int) -> dict[str, dict]:
    """gsis -> latest weekly roster row (week <= W) this season. nflverse weekly rosters drop a released player, so a
    player whose latest row is older than the newest roster week is marked OFF_ROSTER (his old ACT row is history)."""
    if not table_exists(con, "nfl_rosters_weekly"):
        return {}
    out: dict[str, dict] = {}
    for r in fetch_dicts(con, """SELECT gsis_id, week, team, status, position FROM nfl_rosters_weekly
                                 WHERE season = ? AND week <= ? AND gsis_id IS NOT NULL ORDER BY week""", [season, week]):
        out[r["gsis_id"]] = r
    newest = max((r["week"] for r in out.values()), default=None)
    for r in out.values():
        if r["week"] < newest:
            r["status"] = OFF_ROSTER
    return out


def _depth_kickers(con, season: int) -> dict[str, str]:
    if not table_exists(con, "nfl_depth_charts") or "pos_abb" not in table_columns(con, "nfl_depth_charts"):
        return {}
    rows = fetch_dicts(con, """SELECT team, gsis_id FROM nfl_depth_charts WHERE season = ? AND pos_abb IN ('K', 'PK')
                               AND pos_rank = 1 AND dt = (SELECT max(dt) FROM nfl_depth_charts WHERE season = ?)""",
                       [season, season])
    return {r["team"]: r["gsis_id"] for r in rows if r["gsis_id"]}


def expected_kickers(tg: pl.DataFrame, rosters: dict[str, dict], depth: dict[str, str], season: int,
                     week: int) -> dict[str, dict]:
    """team -> {gsis_id | None, basis, flags}. Uses games completed before W and the roster as of W."""
    done = tg.filter(pl.col("completed") & (pl.col("season") * 100 + pl.col("week") < season * 100 + week)
                     & pl.col("game_kicker_id").is_not_null()).sort(["season", "week"])
    last = {r["team"]: r for r in done.group_by("team").agg(pl.col("game_kicker_id").last(), pl.col("season").last(),
                                                             pl.col("week").last()).iter_rows(named=True)}
    active: dict[str, list[str]] = {}
    for g, r in rosters.items():
        if r.get("position") == "K" and (r.get("status") or "").upper() == "ACT":
            active.setdefault(r["team"], []).append(g)
    out = {}
    for team in set(last) | set(active) | set(depth):
        prev = last.get(team)
        lk = prev["game_kicker_id"] if prev else None
        lr = rosters.get(lk) if lk else None
        if lk and lr and lr["team"] == team and (lr.get("status") or "").upper() in ACTIVE_ROSTER:
            out[team] = {"gsis_id": lk, "basis": f"kicked the team's last game ({prev['season']} week {prev['week']})",
                         "flags": []}
        elif len(active.get(team, [])) == 1:
            g = active[team][0]
            out[team] = {"gsis_id": g, "basis": "the only active kicker on the team's latest NFL weekly roster",
                         "flags": [] if g == lk else ["new kicker for this team: the previous kicker is no longer on "
                                                       "its active roster"]}
        elif depth.get(team):
            out[team] = {"gsis_id": depth[team], "basis": "first kicker on the team's latest depth chart",
                         "flags": ["identified from the depth chart only"]}
        else:
            out[team] = {"gsis_id": None, "basis": f"{len(active.get(team, []))} active kickers and no game evidence",
                         "flags": []}
    return out


def participation(con, artifact: km.KickerArtifact, gsis: str, season: int, week: int) -> tuple[float, str]:
    """P(kicks | expected kicker) from the artifact's empirical table by pregame designation."""
    table = artifact.participation or {}
    status = None
    if table_exists(con, "nfl_injuries"):
        r = fetch_dicts(con, """SELECT any_value(report_status) AS s FROM nfl_injuries WHERE season = ? AND week = ?
                                AND gsis_id = ?""", [season, week, gsis])
        status = (r[0]["s"] if r else None)
    keyname = (status or "none").lower()
    entry = table.get(keyname) or table.get("none") or {"p": 0.97}
    return float(entry["p"]), keyname


def _explain(art: km.KickerArtifact, scoring: KickerScoring, lam_fg, lam_pat, skill, long_share, env, base_pts,
             row: dict, league_pat: float) -> list[dict]:
    """Counterfactual decomposition: each item is the change in projected points when that model input is set to
    its league-average value, so every sentence is backed by the model's own arithmetic."""
    conv = art.conversion
    neutral_env = {k: 0.0 for k in env}
    if "elev" in env:
        neutral_env["elev"] = 0.2
    items = []

    def delta(**kw_):
        args = {"lam_fg": lam_fg, "lam_pat": lam_pat, "skill": skill, "long_share": long_share, "env": env, **kw_}
        return base_pts - km.expected_kicking(conv, scoring, args["lam_fg"], args["lam_pat"], args["skill"],
                                              args["long_share"], args["env"])["points"]

    d = delta(lam_pat=league_pat)
    items.append({"feature": "pat_opportunity", "contribution": round(d, 2),
                  "text": (f"touchdown volume: {lam_pat:.2f} expected PAT tries vs a league-average {league_pat:.2f} "
                           f"(team offense {row.get('o_pat_att', float('nan')):.2f} PAT/game, opponent allows "
                           f"{row.get('d_pat_att', float('nan')):.2f})")})
    d = delta(skill=0.0)
    p45 = float(conv.p_make(np.array([45.0]), env, skill)[0])
    p45l = float(conv.p_make(np.array([45.0]), env, 0.0)[0])
    items.append({"feature": "kicker_accuracy", "contribution": round(d, 2),
                  "text": f"kicker accuracy: {p45:.0%} make chance at 45 yards vs {p45l:.0%} for a league-average kicker"
                          f" (shrunk estimate from {row.get('k_kicks', 0):.0f} weighted kicks)"})
    d = delta(long_share=conv.league_long_share)
    items.append({"feature": "long_range_share", "contribution": round(d, 2),
                  "text": f"long-range usage: {long_share:.0%} of his FG tries projected from 50+ yards vs "
                          f"{conv.league_long_share:.0%} league-wide"})
    if env:
        d = delta(env=neutral_env)
        roof = row.get("roof_class") or "unknown"
        where = {"fixed": "indoors (fixed roof)", "retractable": "under a retractable roof (position unknown)",
                 "open_air": "outdoors"}.get(roof, "at an unknown venue")
        elev = row.get("elev_km")
        items.append({"feature": "venue", "contribution": round(d, 2),
                      "text": f"venue: {row.get('venue_name') or 'unknown'}, {where}"
                              + (f", {elev * 1000:.0f} m elevation" if elev is not None and elev >= 0.5 else "")})
    items.sort(key=lambda x: -abs(x["contribution"]))
    return items


def _headline(points: float, league_median: float, items: list[dict]) -> str:
    diff = points - league_median
    tone = ("Strong kicking outlook" if diff >= 1.0 else "Weak kicking outlook" if diff <= -1.0 else "Average kicking outlook")
    ups = [i for i in items if i["contribution"] >= 0.2]
    downs = [i for i in items if i["contribution"] <= -0.2]
    parts = []
    if ups:
        parts.append("helped by " + " and ".join(i["feature"].replace("_", " ") for i in ups[:2]))
    if downs:
        parts.append("held back by " + " and ".join(i["feature"].replace("_", " ") for i in downs[:2]))
    return f"{tone}: {points:.1f} projected vs a {league_median:.1f} median" + (f" ({'; '.join(parts)})" if parts else "")


def build_run(con, frames: dict, artifact: km.KickerArtifact, season: int, week: int, *, artifact_sha256: str,
              now: datetime | None = None, projected_from_week: int | None = None, http_get=None,
              fetch_weather: bool = True) -> tuple[dict, list[dict]]:
    now = now or datetime.now(timezone.utc)
    fw = week if projected_from_week is None else projected_from_week
    if fw > week:
        raise ValueError(f"projected_from_week {fw} is after the target week {week}")
    horizon = week - fw
    cfg = artifact.cfg
    items = league_items(con, season)
    scoring = resolve_kicker_scoring(items)
    scoring_reason = None
    recon = None
    if not scoring.supported:
        scoring_reason = "league kicker scoring is not supported: " + "; ".join(u["reason"] for u in scoring.unsupported)
    elif scoring.fingerprint != artifact.scoring_fingerprint:
        scoring_reason = "league kicker scoring differs from the scoring the artifact was trained with"
    else:
        recon = reconcile_with_espn(con, season, scoring, items)
        if recon["status"] == "mismatch":
            scoring_reason = (f"computed kicker points do not reconcile with ESPN this season "
                              f"({recon['exact']}/{recon['n']} exact)")
    targets = km.target_rows(frames["schedule"], season, [week])
    data = build_dataset(frames, scoring, cfg, targets)
    feats = data["rows"].filter((~pl.col("completed")) & (pl.col("season") == season) & (pl.col("week") == week))
    if horizon:
        # features as of the cutoff week W: recompute the target rows with week W's history only
        cut = {**frames, "kicks": frames["kicks"].filter(pl.col("season") * 100 + pl.col("week") < season * 100 + fw),
               "teams": frames["teams"].filter(pl.col("season") * 100 + pl.col("week") < season * 100 + fw)}
        data = build_dataset(cut, scoring, cfg, targets)
        feats = data["rows"].filter((~pl.col("completed")) & (pl.col("week") == week))
    by_team = {r["team"]: r for r in feats.iter_rows(named=True)}
    tg = km.team_games(frames, scoring)
    rosters = _latest_rosters(con, season, fw)
    expected = expected_kickers(tg, rosters, _depth_kickers(con, season), season, fw)
    skills = km.kicker_skill(data["conv"].filter(pl.col("season") * 100 + pl.col("week") < season * 100 + fw),
                             artifact.conversion, season, cfg)
    sk = {r["kicker_player_id"]: r for r in skills.filter(pl.col("week") == min(fw, 22)).iter_rows(named=True)}
    sched = {g["game_id"]: g for g in frames["schedule"].filter((pl.col("season") == season)
                                                                & (pl.col("week") == week)).iter_rows(named=True)}
    reg = registry()
    lam_pat_league = float(np.exp(artifact.lambda_pat.intercept))   # PAT tries at the training-average inputs
    league_median = artifact.evaluation.get("league_median_points") or 7.6
    weather_cache: dict[str, dict] = {}
    rows = []
    for c in candidates(con, season, week):
        rec = {"season": season, "week": week, "espn_player_id": c["espn_player_id"], "gsis_id": c["gsis_id"],
               "nfl_team": c["pro_team"], "opponent": None, "is_home": None, "venue_id": None,
               "coverage_status": None, "projected_points": None, "interval_low": None, "interval_high": None,
               "p_play": None, "expected_points": None, "stat_line": None, "context": None, "explanation": [],
               "flags": [], "missing_inputs": []}
        rows.append(rec)
        if horizon:
            rec["flags"].append(EXPERIMENTAL.format(k=horizon))
        if scoring_reason:
            rec.update(coverage_status="unsupported_scoring", missing_inputs=[scoring_reason])
            continue
        if not c["gsis_id"] or c["match_confidence"] != "high" or "conflict" in (c.get("mapping_notes") or "").lower():
            rec.update(coverage_status="uncertain_id_mapping",
                       missing_inputs=[f"ESPN->NFL id mapping is {c['match_confidence'] or 'missing'}"])
            continue
        ros = rosters.get(c["gsis_id"])
        status = (ros or {}).get("status")
        team = ros["team"] if ros else None
        if team and c["pro_team"] and team != c["pro_team"]:
            rec["flags"].append(f"NFL roster team {team} differs from ESPN team {c['pro_team']}; NFL roster used")
        rec["nfl_team"] = team or c["pro_team"]
        espn_status = (c["injury_status"] or "").upper()
        if ros is None or (status or "").upper() not in ACTIVE_ROSTER | {"RES", "PUP", "NFI", "SUS", "EXE"}:
            if espn_status in WITHHELD_ESPN:
                rec.update(coverage_status="withheld_unavailable", missing_inputs=[f"ESPN status {c['injury_status']}"])
            else:
                rec.update(coverage_status="no_nfl_team",
                           missing_inputs=["not on an NFL active roster (latest weekly roster)"])
            continue
        if (status or "").upper() not in ACTIVE_ROSTER:
            rec.update(coverage_status="withheld_unavailable",
                       missing_inputs=[f"not on the NFL active roster (weekly roster status {status})"])
            continue
        game = by_team.get(team)
        if game is None:
            rec["coverage_status"] = "bye"
            continue
        rec.update(opponent=game["opponent"], is_home=bool(game["is_home"]), venue_id=game.get("venue_id"))
        if espn_status in (WITHHELD_ESPN if not horizon else {"INJURY_RESERVE", "SUSPENSION"}):
            rec.update(coverage_status="withheld_unavailable", missing_inputs=[f"ESPN status {c['injury_status']}"])
            continue
        exp = expected.get(team) or {"gsis_id": None, "basis": "no kicker evidence", "flags": []}
        if exp["gsis_id"] is None:
            rec.update(coverage_status="ambiguous_kicker", missing_inputs=[f"team kicker unknown: {exp['basis']}"])
            continue
        if exp["gsis_id"] != c["gsis_id"]:
            rec.update(coverage_status="not_expected_kicker",
                       missing_inputs=[f"{team}'s expected kicker is another player ({exp['basis']})"])
            continue
        rec["flags"] += exp["flags"]
        s = sk.get(c["gsis_id"])
        skill = float(s["k_skill"]) if s else 0.0
        long_share = float(s["k_long_share"]) if s else artifact.conversion.league_long_share
        kicks = float(s["k_kicks"]) if s else 0.0
        if s is None or kicks < 1:
            rec["flags"].append("cold start: no kicking history in the data, league-average accuracy and range assumed")
        elif kicks < 30:
            rec["flags"].append(f"small kicking sample ({kicks:.0f} weighted kicks): accuracy shrunk strongly toward "
                                "the league average")
        row = {**game, "k_skill": skill, "k_long_share": long_share, "k_kicks": kicks}
        g = sched.get(game["game_id"]) or {}
        venue = resolve(g, reg) if g else {"venue_id": None, "roof_class": None, "notes": ["game not in schedule"]}
        rec["flags"] += venue.get("notes") or []
        kickoff = _kickoff(g) if g else None
        # weather: next game only, outdoor US venues; shown with its freshness; a model input only if the artifact
        # was trained with the weather group (kfp-1.0.0 is not)
        wx = {"status": "not_applicable", "reason": "rest-of-season week: no forecast is fetched"} if horizon else None
        if wx is None:
            gid = game["game_id"]
            if gid not in weather_cache:
                weather_cache[gid] = kw.game_forecast(con, g, venue, kickoff, now, http_get=http_get,
                                                      allow_fetch=fetch_weather)
            wx = weather_cache[gid]
        uses_weather = "weather" in cfg.groups
        if uses_weather and wx.get("values"):
            row["wx_wind"] = float(wx["values"].get("wind_mph_mean") or km.OUTDOOR_WIND_FILL)
            row["wx_cold"] = float((wx["values"].get("temp_f_mean") or 99) < km.COLD_F)
        elif uses_weather:
            row["wx_wind"], row["wx_cold"] = (km.OUTDOOR_WIND_FILL if venue.get("outdoor") else 0.0), 0.0
            rec["missing_inputs"].append("weather forecast unavailable: average outdoor conditions assumed")
        frame = pl.DataFrame([row])
        lam_fg = float(artifact.lambda_fg.predict(frame)[0])
        lam_pat = float(artifact.lambda_pat.predict(frame)[0])
        env = km.env_of(row, cfg.groups)
        e = km.expected_kicking(artifact.conversion, scoring, lam_fg, lam_pat, skill, long_share, env)
        pts = round(e["points"], 2)
        iv = km.interval_for(artifact.intervals, pts)
        p_play, designation = participation(con, artifact, c["gsis_id"], season, week)
        if horizon:
            p_play, designation = float((artifact.participation.get("none") or {"p": 0.97})["p"]), "none (future week)"
        explanation = _explain(artifact, scoring, lam_fg, lam_pat, skill, long_share, env, e["points"], row, lam_pat_league)
        if wx.get("values"):
            v = wx["values"]
            explanation.append({"feature": "weather_forecast", "contribution": 0.0 if not uses_weather else None,
                                "text": f"forecast: {v.get('wind_mph_mean')} mph wind (gusts {v.get('gust_mph_max')}), "
                                        f"{v.get('temp_f_mean')}F, {v.get('precip_prob_max')}% precipitation chance "
                                        f"(NWS, retrieved {wx.get('retrieved_at')}); "
                                        + ("a model input" if uses_weather else
                                           f"not a model input in {artifact.version}: retrospective tests did not show "
                                           "an accuracy gain")})
        rec.update(coverage_status="projected", projected_points=pts,
                   interval_low=None if iv is None else round(iv[0], 2),
                   interval_high=None if iv is None else round(iv[1], 2), p_play=round(p_play, 4),
                   expected_points=round(p_play * pts, 2), stat_line=e["stat_line"],
                   explanation=[{"feature": "headline", "contribution": None,
                                 "text": _headline(pts, league_median, explanation)}] + explanation)
        rec["context"] = {
            "expected_kicker_basis": exp["basis"], "designation": designation,
            "team": {k: _r(game.get(k)) for k in ("o_fg_att", "o_pat_att", "o_fg_range", "o_rz_td_rate", "o_points",
                                                  "o_games")},
            "opponent": {k: _r(game.get(k)) for k in ("d_fg_att", "d_pat_att", "d_fg_range", "d_rz_td_rate", "d_points",
                                                      "d_games")},
            "kicker": {"skill_logit": _r(skill), "long_share": _r(long_share), "weighted_kicks": _r(kicks)},
            "venue": {"venue_id": venue.get("venue_id"), "name": venue.get("name"), "roof": venue.get("roof_status"),
                      "elevation_m": venue.get("elevation_m"), "source": venue.get("source"),
                      "kickoff_utc": kickoff.isoformat() if kickoff else None},
            "weather": {**{k: wx.get(k) for k in ("status", "reason", "provider", "retrieved_at", "issued_at", "stale")},
                        "values": wx.get("values"), "used_by_model": bool(uses_weather and wx.get("values"))},
            "model_inputs": {"lambda_fg_groups": list(cfg.lambda_fg_groups), "lambda_pat_groups": list(cfg.groups),
                             "conversion_groups": [g for g in cfg.groups if g in km.CONVERSION_FEATURES]}}
    synced = fetch_dicts(con, "SELECT max(finished_at) AS t FROM sync_runs WHERE source = 'nfl' AND status = 'ok'") \
        if table_exists(con, "sync_runs") else [{"t": None}]
    done = frames["teams"].filter(pl.col("season") * 100 + pl.col("week") < season * 100 + fw)
    last = done.sort(["season", "week"]).tail(1).select("season", "week").row(0) if done.height else (None, None)
    wx_states = [r["context"]["weather"]["status"] for r in rows if r.get("context")]
    meta = {"run_id": f"kfp-{season}w{week}-{utcnow():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}", "season": season,
            "week": week, "model_version": artifact.version, "scoring_fingerprint": scoring.fingerprint if scoring.supported else None,
            "scoring": {**scoring.describe(), "reconciliation": None if recon is None else
                        {k: recon.get(k) for k in ("status", "n", "exact", "exact_rate", "season", "reason")}},
            "artifact_id": artifact_sha256, "artifact_created_at": artifact.created_at,
            "data_through_season": last[0], "data_through_week": last[1], "nfl_synced_at": synced[0]["t"],
            "model_status": artifact.status, "evaluation": artifact.evaluation,
            "weather_summary": {s: wx_states.count(s) for s in sorted(set(wx_states))},
            "forecast_horizon": horizon, "projected_from_week": fw, "created_at": utcnow()}
    for r in rows:
        r["run_id"] = meta["run_id"]
    return meta, rows


def _r(v, nd=3):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return None if f != f else round(f, nd)


def artifact_sha(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def load_artifact(path) -> km.KickerArtifact:
    return km.KickerArtifact.from_dict(json.loads(open(path).read()))
