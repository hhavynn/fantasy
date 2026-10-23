"""Live week-W independent projections: build targets from the live DB, apply a frozen artifact,
persist rows for the read-only API. Never trains; never touches ESPN projection storage.

Storage is append-only (docs/independent_projection_engine.md, "Live runs and abstention"): `write_run`
stores a run and its rows under their own run_id in `independent_projection_runs` /
`independent_projection_rows`, never deletes or updates a committed run, returns the existing run for an
identical retry, and raises if a run_id would receive different content. Each run records
`artifact_id` (sha256 of the artifact FILE bytes, the digest the approval registry pins), a
`content_sha256` over its canonicalised meta and rows, and its `row_count`.

Guards (each produces an explicit coverage_status and a NULL projection, never a zero):
  unsupported_scoring    league offensive scoring differs from the artifact's training scoring
  unsupported_position   K and D/ST (no credible independent model in V1)
  uncertain_id_mapping   no high-confidence ESPN->GSIS crosswalk (a wrong id would borrow
                         another player's history)
  bye                    the player's team has no game in week W
  withheld_unavailable   NFL-ineligible for week W (player_availability rule), ESPN status
                         OUT / INJURY_RESERVE / SUSPENSION, or latest NFL weekly roster status not
                         ACT/INA (exempt, suspended, practice squad, reserve, released)
  abstain_no_history     no NFL game in the lookback window (rookie debut, long absence)
  no_model               the artifact has no model for the position

With a participation artifact (app.analytics.participation), each projected row also gets
p_play = P(appears in week W) and expected_points = p_play * projected_points (unconditional).
projected_points stays the estimate CONDITIONAL on appearing.

Rest-of-season runs (`build_live(..., projected_from_week=W)` for a target week T > W, horizon h = T - W): the
SAME week-W artifact projects every remaining regular-season week, "as of now, against the future opponent".
Player, usage and recency features are computed with feature week W (so weeks_since_last and every recency
feature stay in the training distribution); only the opponent, home/away, bye and NFL reserve weeks come from
T's schedule. Availability for h >= 1: NFL reserve designations apply per week (player_availability.derive
unavailable_weeks); ESPN INJURY_RESERVE / SUSPENSION and a non-ACT/INA roster status withhold EVERY remaining week
("long-term unavailable; return date unknown"); ESPN OUT / DOUBTFUL / QUESTIONABLE and the ESPN designation
fallback describe the next game only and apply to h = 0 ONLY. Participation sees no week-T injury report and gives
an undesignated p_play. Rows are flagged "future week: no injury report yet" and "experimental"; horizon-0
output is byte-identical to a run made before this feature existed.

An ifp-2.1.0 artifact (app.analytics.injury_model) adds two inputs and nothing else: the workload-handoff features
of the projected week (as known at the cutoff week, with the cutoff week's designations incl. the ESPN status
fallback; at h >= 1 injured teammates' returns are weighted by the artifact's return-rate table), and, at h >= 1, a
player designated Out/Doubtful at the cutoff gets the table's empirical return rate as P(plays) instead of the
undesignated estimate (flagged; the replaced estimate is kept in `participation`). ifp-1.0.0 / ifp-2.0.0 runs are
unchanged.

Availability news (app.analytics.availability_news, `news=` entries; the production `project` command passes the
reviewed file): a player the news puts out for week T is withheld with the news note; one it puts back (reinstated,
in or after a reported return window) is projected although a stale weekly roster status or a long-term ESPN status
says otherwise (an NFL reserve-rule week and a horizon-0 ESPN OUT still withhold); in a return window P(plays) is
the model's estimate times the window probability. At horizon 0 an "out" entry yields to the synced data when the
week's nflverse report (published for his team) has no Out/Doubtful for him, his week-W roster row is ACT and ESPN
does not list him OUT / IR / suspended: the row is flagged availability_news.OVERRIDDEN. The workload handoff sees the same states, so teammates absorb
or return the volume. Training never reads it.

An ifp-2.2.0 artifact (app.analytics.context_model) keeps all of that and adds the shipped research-shadow
components as known at the cutoff week (QB start probabilities and the scenario mixture, QB and teammate context,
the pregame depth tier from snapshots made before the run, the decline-only role); the frozen QB P(plays) map applies
at horizon 0 only while its component ships (the backtest guard left it out). The caller passes
context_model.Inputs (`context_inputs`).
"""

from __future__ import annotations

import hashlib
import json
import uuid

import polars as pl

from ..db.connection import fetch_dicts, insert_rows, table_exists, utcnow
from ..espn.constants import IR_SLOT
from .forecast_features import POSITIONS, Sources, features_for_week, key
from .independent_projections import Artifact, RidgeModel, design_matrix, flags_for, missing_inputs, predict_frame
from .league_scoring import resolve_skill_scoring
from . import availability_news as an
from .player_availability import RESTRICTED, derive
from .production_forecasts import (HORIZON_NOTE, INPUTS_VERSION, NEXT_GAME_ONLY, ROWS, RUNS, ImmutableRunError, annotate,
                                   content_sha256, cycle_bases, horizon_of, row_fingerprint, team_kickoffs, write_inputs,
                                   write_models)

WITHHELD_ESPN = {"OUT", "INJURY_RESERVE", "SUSPENSION"}
LONG_TERM_ESPN = {"INJURY_RESERVE", "SUSPENSION"}   # withheld for every remaining week; OUT is the next game only
FUTURE_NO_REPORT = "future week: no injury report yet"
LONG_TERM_UNAVAILABLE = "long-term unavailable; return date unknown"
RESERVE_ROSTER = {"RES", "PUP", "NFI"}   # NFL reserve lists: player_availability.derive can date a return for these
ACTIVE_ROSTER = {"ACT", "INA"}     # nflverse weekly roster: active 53 (INA = game-day inactive that week)
SUPPORTED_MAPPING = "high"
# Row `model` label per fitted model name: ifp-1.0.0 selects B or C per position, ifp-2.0.0 fits "ifp2" ridges,
# ifp-2.1.0 "ifp21" ridges (ifp2 + the injury workload handoff).
MODEL_LABELS = {"B": "opportunity", "C": "context", "ifp2": "ifp2_ridge", "ifp21": "ifp21_injury_aware_ridge",
                "ifp22": "ifp22_context_ridge"}


def league_scoring(con, season: int):
    rows = fetch_dicts(con, "SELECT league_id, scoring_items FROM leagues WHERE season = ? ORDER BY synced_at DESC",
                       [season]) if table_exists(con, "leagues") else []
    if not rows:
        return None, resolve_skill_scoring(None)
    items = rows[0]["scoring_items"]
    return rows[0]["league_id"], resolve_skill_scoring(json.loads(items) if isinstance(items, str) else items)


def candidates(con, season: int, week: int) -> list[dict]:
    """ESPN players worth reporting for week W: rostered in the league, or projected by ESPN for W."""
    return fetch_dicts(con, """
        SELECT fp.espn_player_id, fp.full_name, fp.position, fp.pro_team, fp.injury_status, fp.on_team_id,
               m.gsis_id, m.match_method, m.match_confidence, m.notes AS mapping_notes
        FROM fantasy_players fp LEFT JOIN player_id_map m USING (espn_player_id)
        WHERE fp.position IN ('QB', 'RB', 'WR', 'TE', 'K', 'D/ST')
          AND (coalesce(fp.on_team_id, 0) > 0 OR EXISTS (
                SELECT 1 FROM espn_player_weekly_points e WHERE e.espn_player_id = fp.espn_player_id
                AND e.season = ? AND e.scoring_period = ? AND e.projected_points IS NOT NULL))
        ORDER BY fp.espn_player_id""", [season, week])


def _roster_rows(con, season: int, week: int) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if not table_exists(con, "nfl_rosters_weekly"):
        return out
    for r in fetch_dicts(con, """
            SELECT gsis_id, week, status, status_description_abbr AS abbr, team FROM nfl_rosters_weekly
            WHERE season = ? AND week <= ? AND (game_type = 'REG' OR game_type IS NULL) AND gsis_id IS NOT NULL
            ORDER BY gsis_id, week""", [season, week]):
        out.setdefault(r["gsis_id"], []).append(r)
    return out


def _fantasy_ir(con, season: int, week: int) -> set[int]:
    if not table_exists(con, "lineup_snapshots"):
        return set()
    rows = fetch_dicts(con, """
        SELECT espn_player_id FROM lineup_snapshots WHERE season = ? AND lineup_slot_id = ? AND scoring_period =
          (SELECT max(scoring_period) FROM lineup_snapshots WHERE season = ? AND scoring_period <= ?)""",
                       [season, IR_SLOT, season, week])
    return {r["espn_player_id"] for r in rows}


ESPN_DESIGNATION = {"OUT": "Out", "DOUBTFUL": "Doubtful", "QUESTIONABLE": "Questionable"}


def espn_designation_fallback(con, sources: Sources, season: int, week: int) -> tuple[Sources, set[str]]:
    """Live only. The participation model reads the nflverse week-W injury report, which is often
    not synced until late in the week. For a team with no nflverse week-W designation yet, use the
    ESPN injury status (OUT/DOUBTFUL/QUESTIONABLE, high-confidence id mapping) as its designation.
    This is injury STATUS, never an ESPN projection; affected rows are flagged. Teams with a synced
    nflverse designation keep nflverse only."""
    from .forecast_features import INJURY_SCHEMA

    if sources.rosters is None or not all(table_exists(con, t) for t in ("fantasy_players", "player_id_map")):
        return sources, set()
    inj = sources.injuries if sources.injuries is not None else pl.DataFrame(schema=INJURY_SCHEMA)
    synced = set(inj.filter((pl.col("season") == season) & (pl.col("week") == week)
                            & pl.col("report_status").is_not_null())["team"].unique().to_list())
    teams = {r["gsis_id"]: r["team"] for r in sources.rosters.filter(
        (pl.col("season") == season) & (pl.col("week") <= week)).sort("week").iter_rows(named=True)}
    espn = fetch_dicts(con, """
        SELECT m.gsis_id, upper(fp.injury_status) AS status FROM fantasy_players fp
        JOIN player_id_map m USING (espn_player_id)
        WHERE m.match_confidence = 'high' AND m.gsis_id IS NOT NULL
          AND upper(fp.injury_status) IN ('OUT', 'DOUBTFUL', 'QUESTIONABLE')""")
    add = {r["gsis_id"]: ESPN_DESIGNATION[r["status"]] for r in espn
           if teams.get(r["gsis_id"]) and teams[r["gsis_id"]] not in synced}
    if not add:
        return sources, set()
    current = inj.filter((pl.col("season") == season) & (pl.col("week") == week) & pl.col("gsis_id").is_in(list(add)))
    practice = {r["gsis_id"]: r for r in current.iter_rows(named=True)}
    rows = pl.DataFrame([{"season": season, "week": week, "gsis_id": g, "team": teams[g], "report_status": st,
                          "practice_status": (practice.get(g) or {}).get("practice_status"),
                          "injury": (practice.get(g) or {}).get("injury"), "not_injury": False}
                         for g, st in sorted(add.items())], schema=INJURY_SCHEMA)
    keep = inj.filter(~((pl.col("season") == season) & (pl.col("week") == week) & pl.col("gsis_id").is_in(list(add))))
    return (Sources(sources.panel, sources.games, sources.defense, sources.rosters, pl.concat([keep, rows])),
            {teams[g] for g in add})


def artifact_digest(artifact: Artifact) -> str:
    """Fallback artifact id when the caller has no artifact file: sha256 of the canonical artifact JSON,
    prefixed so it can never be mistaken for a file digest (the registry pins sha256 of file bytes)."""
    return "dict:" + hashlib.sha256(json.dumps(artifact.to_dict(), sort_keys=True, default=str).encode()).hexdigest()


class HorizonError(ValueError):
    """A run's cutoff week is not the first unplayed week: its "current" inputs describe an earlier game."""


def check_horizon(sources: Sources, season: int, cutoff_week: int) -> None:
    """Refuse a run whose cutoff (feature) week lies beyond the next unplayed week. The ESPN injury status, the
    ESPN designation fallback and the latest roster row describe the NEXT game only; a horizon-0 run for week 7
    made while week 6 is unplayed would apply week 6's OUT/DOUBTFUL to week 7 (an injury carried forward a
    week). Such a target week is a rest-of-season target: pass projected_from_week = the first unplayed week.

    The next unplayed week is the earliest week with an outstanding game, judged over the whole slate (a final
    Thursday game does not end its week). A game without a final score is outstanding unless a game with a
    strictly later scheduled kickoff (week, gameday, gametime) is already final: then it was postponed or
    cancelled and does not block. No wall clock is read. With every game final nothing is left to refuse."""
    g = sources.games.filter(pl.col("season") == season)
    if g.height == 0:
        return
    g = g.with_columns(pl.concat_str([pl.col("week").cast(pl.String).str.zfill(3), pl.col("gameday").fill_null(""),
                                      pl.col("gametime").fill_null("")], separator=" ").alias("_kick"))
    finals = g.filter(pl.col("points_for").is_not_null())
    last_final = finals["_kick"].max() if finals.height else None
    pending = g.filter(pl.col("points_for").is_null())
    if last_final is not None:
        pending = pending.filter(pl.col("_kick") >= last_final)    # a game in the same slot is concurrent
    if pending.height == 0:
        return
    nxt = int(pending["week"].min())
    if cutoff_week > nxt:
        raise HorizonError(f"cutoff week {cutoff_week} is after the next unplayed week {nxt} of {season}: current "
                           f"injury and roster status describe week {nxt}'s game, not week {cutoff_week}'s; project "
                           f"week {cutoff_week} as a rest-of-season target (projected_from_week={nxt})")


def build_live(con, sources: Sources, artifact: Artifact, season: int, week: int,
               participation=None, artifact_sha256: str | None = None, *,
               projected_from_week: int | None = None, context_inputs=None,
               news: list | None = None, inputs_out: dict | None = None) -> tuple[dict, list[dict]]:
    """Run metadata + one row per candidate. `sources` must contain the live season (and
    ideally the previous season from the history DB). `participation`: optional
    ParticipationArtifact; without it p_play and expected_points stay null. `artifact_sha256`: sha256 of
    the artifact file the caller loaded (scripts/backtest_independent_projections.py project), stored as
    the run's artifact_id so it can be compared with the approval registry's pinned sha.
    `projected_from_week`: W, the first unplayed week, when `week` is a later target week T (rest-of-season
    projection, horizon T - W; see the module docstring). None (or == week) is the normal horizon-0 run.
    `context_inputs`: app.analytics.context_model.Inputs, required for an ifp-2.2.0 artifact (context_model.load_inputs).
    `news`: availability_news entries (module docstring, "Availability news"); None or [] changes nothing.
    `inputs_out`: a dict to receive the projected rows' full point-model inputs (`capture_inputs`), for
    `write_run(..., inputs=)`; None (the default) captures nothing."""
    fw = week if projected_from_week is None else projected_from_week
    context = _context_model(artifact)
    if context is not None and context_inputs is None:
        raise ValueError(f"an {artifact.version} artifact needs context_inputs (app.analytics.context_model.load_inputs)")
    if fw > week:
        raise ValueError(f"projected_from_week {fw} is after the target week {week}")
    horizon = week - fw
    check_horizon(sources, season, fw)
    _, scoring = league_scoring(con, season)
    scoring_ok = scoring.supported and scoring.fingerprint == artifact.scoring_fingerprint
    games = sources.games.filter((pl.col("season") == season) & (pl.col("week") == week))
    slate = {r["team"]: r for r in games.iter_rows(named=True)}
    team_weeks: dict[str, list[int]] = {}
    for r in sources.games.filter(pl.col("season") == season).iter_rows(named=True):
        team_weeks.setdefault(r["team"], []).append(int(r["week"]))
    # roster and fantasy-IR state as of the cutoff week W, whatever the target week (a replay of a past W must not
    # read rows posted since; live, nothing exists after W anyway)
    rosters = _roster_rows(con, season, fw)
    on_ir = _fantasy_ir(con, season, fw)
    # the EFFECTIVE news states: the loop below drops an entry a hard availability gate or (horizon 0) the synced
    # report contradicts, and only what survives reaches the rows' P(plays) and the workload handoff
    nstate = an.for_week(news, season, week, team_weeks) if news else {}
    report = _week_report(sources, season, week) if nstate and not horizon else None
    rows, targets = [], []
    for c in candidates(con, season, week):
        rec = {"season": season, "week": week, "espn_player_id": c["espn_player_id"], "gsis_id": c["gsis_id"],
               "position": c["position"], "projected_points": None, "interval_low": None, "interval_high": None,
               "model": None, "games_used": None, "latest_usage_season": None, "latest_usage_week": None,
               "missing_inputs": [], "flags": [], "explanation": [], "nfl_team": c["pro_team"], "opponent": None,
               "is_home": None, "p_play": None, "expected_points": None, "participation": None}
        rows.append(rec)
        if c["position"] not in POSITIONS:
            rec["coverage_status"] = "unsupported_position"
            continue
        if not scoring_ok:
            rec["coverage_status"] = "unsupported_scoring"
            continue
        notes = (c.get("mapping_notes") or "").lower()
        if not c["gsis_id"] or c["match_confidence"] != SUPPORTED_MAPPING or "conflict" in notes:
            rec["coverage_status"] = "uncertain_id_mapping"
            rec["missing_inputs"] = [f"ESPN->NFL id mapping is {c['match_confidence'] or 'missing'}"
                                     f"{' with conflicting crosswalks' if 'conflict' in notes else ''}"]
            continue
        hist = rosters.get(c["gsis_id"], [])
        team = hist[-1]["team"] if hist else c["pro_team"]
        if hist and c["pro_team"] and team != c["pro_team"]:
            rec["flags"].append(f"NFL roster team {team} differs from ESPN team {c['pro_team']}; NFL roster used")
        rec["nfl_team"] = team
        game = slate.get(team)
        if game is None:
            rec["coverage_status"] = "bye"
            continue
        rec["opponent"], rec["is_home"] = game["opponent"], bool(game["is_home"])
        avail = derive(espn_status=c["injury_status"], on_ir=c["espn_player_id"] in on_ir, rows=hist,
                       team_games=team_weeks.get(team), weeks=[week])
        if c["espn_player_id"] in on_ir:
            rec["flags"].append("in a fantasy IR slot")
        espn_status = (c["injury_status"] or "").upper()
        roster_status = (hist[-1].get("status") or "").upper() if hist else None
        if horizon:
            rec["flags"] += [FUTURE_NO_REPORT, HORIZON_NOTE.format(k=horizon)]
        ns = nstate.get(c["gsis_id"])
        if ns is not None and ns.p < 1 and report is not None and _synced_clear(report, c["gsis_id"], team, hist, week,
                                                                                espn_status):
            # Horizon 0: the week's synced report and active roster say he plays; a stale "out" entry must not
            # withhold him (the teammates' handoff drops the entry too).
            rec["flags"].append(f"{an.OVERRIDDEN} (week-{week} injury report has no Out/Doubtful for him and the "
                                f"week-{week} roster is ACT); remove the entry: {ns.reason}")
            nstate.pop(c["gsis_id"])
            ns = None
        if ns is not None and ns.p <= 0:
            rec["coverage_status"] = "withheld_unavailable"
            rec["missing_inputs"] = [ns.reason]
            continue
        news_back = ns is not None      # the news dates his availability for this week (p > 0)
        # Horizon >= 1: an NFL reserve-list player whose return derive() can date (reserve rule code -> earliest
        # eligible week) is withheld only for the weeks in unavailable_weeks and projected after them, even when ESPN
        # also lists INJURY_RESERVE; every other long-term status stays withheld for all remaining weeks.
        dated_return = bool(horizon and roster_status in RESERVE_ROSTER and avail["status"] in RESTRICTED
                            and avail["earliest_eligible_week"] is not None)
        # ESPN OUT is the next game only; INJURY_RESERVE / SUSPENSION last past it (horizon 0: all three withhold).
        espn_withheld = espn_status in (LONG_TERM_ESPN if horizon else WITHHELD_ESPN) \
            and not (dated_return and espn_status == "INJURY_RESERVE") and not (horizon and news_back)
        if (avail["status"] in RESTRICTED and week in avail["unavailable_weeks"]) or espn_withheld:
            if ns is not None:
                # a hard constraint (NFL reserve-rule week, horizon-0 ESPN OUT / IR / suspension) beats positive news:
                # the entry is dropped, so the teammates' handoff keeps the timeline's own status for him too
                nstate.pop(c["gsis_id"])
                rec["flags"].append(f"availability news not applied: {ns.reason}")
            rec["coverage_status"] = "withheld_unavailable"
            rec["missing_inputs"] = [w for w in avail["warnings"]] or [f"ESPN status {c['injury_status']}"]
            if horizon and espn_withheld:
                rec["missing_inputs"] = [f"{LONG_TERM_UNAVAILABLE} (ESPN status {c['injury_status']})"]
            continue
        # (a dated return not yet reached was withheld above; from the eligible week on he is projected)
        if news_back:
            rec["flags"].append(ns.reason)
        if dated_return:
            rec["flags"].append(f"returns from reserve (eligible week {avail['earliest_eligible_week']}): return "
                                "not confirmed, assumed available")
        elif news_back:
            pass        # the news puts him on the active roster whatever the synced weekly roster says
        elif roster_status and roster_status not in ACTIVE_ROSTER:
            # Exempt, suspended, practice squad, released, reserve of any kind: not on the active roster.
            rec["coverage_status"] = "withheld_unavailable"
            rec["missing_inputs"] = [f"not on the NFL active roster (weekly roster status {roster_status}, "
                                     f"week {hist[-1]['week']})"]
            if horizon:
                rec["missing_inputs"].insert(0, LONG_TERM_UNAVAILABLE)
            continue
        if horizon:
            if espn_status in WITHHELD_ESPN | {"QUESTIONABLE", "DOUBTFUL"}:
                rec["flags"].append(f"ESPN injury status {c['injury_status']} {NEXT_GAME_ONLY}; it is "
                                    f"not applied to week {week}")
        elif espn_status in ("QUESTIONABLE", "DOUBTFUL"):
            rec["flags"].append(f"ESPN injury status {c['injury_status']}: projection assumes he plays")
        rec["coverage_status"] = "pending"
        targets.append({"gsis_id": c["gsis_id"], "position": c["position"], "team": team,
                        "opponent": game["opponent"], "is_home": bool(game["is_home"])})
    preds: dict[str, dict] = {}
    feats = None
    injury = _injury_model(artifact)
    hsources = codes = None
    if injury is not None:   # cutoff-week designations as P(plays) sees them (nflverse, else the ESPN status fallback)
        hsources, _ = espn_designation_fallback(con, sources, season, fw)
        codes = _roster_codes(con, season)
    if targets:
        tdf = pl.DataFrame(targets).unique("gsis_id", keep="first")
        feats = features_for_week(sources, season, fw, tdf)    # feature week W; opponent / home-away are T's
        if injury is not None:
            feats = injury.augment(artifact, hsources, season, week, fw, feats, roster_codes=codes, news=nstate)
        if context is not None:
            feats = context.augment(artifact, hsources if hsources is not None else sources, context_inputs, season,
                                    week, fw, feats, as_of=utcnow())
        preds = {p["gsis_id"]: p for p in (context.predict_frame if context is not None else predict_frame)(artifact, feats)}
    for rec in rows:
        if rec.get("coverage_status") != "pending":
            continue
        p = preds.get(rec["gsis_id"])
        if p is None or artifact.models.get(rec["position"]) is None:
            rec["coverage_status"] = "no_model"
            continue
        rec.update(games_used=p.get("games_recent"), latest_usage_season=p.get("last_season"),
                   latest_usage_week=p.get("last_week"), missing_inputs=missing_inputs(p),
                   flags=rec["flags"] + flags_for(p))
        if p["model_points"] is None:
            rec["coverage_status"] = "abstain_no_history"
            continue
        rec.update(coverage_status="projected", projected_points=p["model_points"],
                   interval_low=p["interval_low"], interval_high=p["interval_high"],
                   model=MODEL_LABELS.get(artifact.models[rec["position"]].model, artifact.models[rec["position"]].model),
                   explanation=p["explanation"])
    parts: dict[str, dict] = {}
    if participation is not None:
        from .participation import predict_week
        if horizon:      # no injury report and no ESPN fallback for a future week: undesignated p_play
            parts = predict_week(sources, participation, season, week, feature_week=fw)
        else:
            psources, fallback = espn_designation_fallback(con, sources, season, week)
            parts = predict_week(psources, participation, season, week, fallback_teams=fallback)
    for rec in rows:
        if participation is None or rec.get("coverage_status") != "projected":
            continue
        q = parts.get(rec["gsis_id"])
        if q is None:
            rec["participation"] = {"p_play": None, "model_version": participation.version,
                                    "note": "no ACT/INA NFL weekly roster row for this season: no participation estimate"}
            continue
        rec.update(p_play=q["p_play"], expected_points=round(q["p_play"] * rec["projected_points"], 2), participation=q)
    if injury is not None and participation is not None and horizon:
        # ifp-2.1.0: Out/Doubtful at the cutoff -> the empirical return rate, not the undesignated estimate
        ros = injury.ros_p_play(artifact, hsources, season, fw, week, rows,
                                injured=injury.cutoff_injured(hsources, season, fw, codes))
        for rec in rows:
            q = ros.get(rec["gsis_id"]) if rec.get("coverage_status") == "projected" else None
            if q is None:
                continue
            q = {**q, "undesignated_p_play": (rec.get("participation") or {}).get("p_play"),
                 "undesignated_model_version": participation.version}
            rec.update(p_play=q["p_play"], expected_points=round(q["p_play"] * rec["projected_points"], 2),
                       participation=q, flags=rec["flags"] + q["flags"])
    if context is not None and participation is not None:
        context.calibrate_rows(artifact, rows, horizon)     # ifp-2.2.0: the frozen QB P(plays) map, horizon 0
    if nstate and participation is not None:
        _apply_news_p_play(rows, nstate)
    before = sources.panel.filter(pl.col("season") * 100 + pl.col("week") < key(season, fw))
    last = before.select(pl.col("season").max(), pl.col("week").filter(
        pl.col("season") == pl.col("season").max()).max()).row(0) if before.height else (None, None)
    synced = fetch_dicts(con, "SELECT max(finished_at) AS t FROM sync_runs WHERE source = 'nfl' AND status = 'ok'") \
        if table_exists(con, "sync_runs") else [{"t": None}]
    meta = {"run_id": f"ifp-{season}w{week}-{utcnow():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}", "season": season,
            "week": week, "model_version": artifact.version, "scoring_fingerprint": scoring.fingerprint if scoring.supported else None,
            "artifact_id": artifact_sha256 or artifact_digest(artifact), "artifact_created_at": artifact.created_at,
            "data_through_season": last[0], "data_through_week": last[1],
            "nfl_synced_at": synced[0]["t"], "model_status": artifact.status, "evaluation": artifact.evaluation,
            "participation_version": participation.version if participation is not None else None,
            "participation_status": participation.status if participation is not None else None,
            "participation_evaluation": participation.evaluation if participation is not None else None,
            "forecast_horizon": horizon, "projected_from_week": fw,
            "created_at": utcnow()}
    for rec in rows:
        rec["run_id"] = meta["run_id"]
    if inputs_out is not None:
        inputs_out.clear()
        inputs_out.update(capture_inputs(artifact, feats, rows, context, meta["artifact_id"]))
    return meta, rows


def capture_inputs(artifact: Artifact, feats: pl.DataFrame | None, rows: list[dict], context, artifact_id: str) -> dict:
    """The full point-model inputs of every projected row (production_forecasts "forecast inputs"): {models, rows}.
    Each row is the raw design row its point went through, exactly as predict_frame applied it: an ifp-2.2.0 QB with a
    start candidate is the start/no-start mixture (the start-share column holds the share), one without goes through
    the artifact's fallback model; every other row through its position's model. `rows[i]["row"]` fingerprints the
    finished forecast row, so `write_run` stores inputs only beside the very row they were captured for."""
    out: dict = {"models": [], "rows": []}
    if feats is None or feats.height == 0:
        return out
    mixture, fallback = False, {}
    if context is not None:
        cfgk = artifact.config.get(context.CONFIG_KEY) or {}
        comps = tuple((cfgk.get("components") or {}).get("QB") or ())
        mixture = artifact.models.get("QB") is not None and context.uses_scenarios("QB", comps)
        fallback = {p: RidgeModel.from_dict(d) for p, d in (cfgk.get("fallback_models") or {}).items()}
    projected = {r["gsis_id"]: r for r in rows if r.get("coverage_status") == "projected" and r.get("gsis_id")}
    groups: dict[tuple[str, str], list[str]] = {}
    for g, r in projected.items():
        kind = "main"
        if mixture and r["position"] == "QB":
            pi = feats.filter(pl.col("gsis_id") == g)["pi_start"] if "pi_start" in feats.columns else None
            if pi is None or pi.len() != 1 or pi[0] is None:
                if fallback.get("QB") is None:
                    continue             # no start candidate and no fallback: the point is not a model output
                kind = "fallback"
        groups.setdefault((r["position"], kind), []).append(g)
    for (pos, kind), ids in sorted(groups.items()):
        model = fallback.get(pos) if kind == "fallback" else artifact.models.get(pos)
        if model is None:
            continue
        frame = feats.filter(pl.col("gsis_id").is_in(ids))
        mix_col = "s_start" if mixture and pos == "QB" and kind == "main" and "s_start" in model.columns else None
        if mix_col is not None:          # the mixture is linear in the start share: store the share as its value
            frame = frame.with_columns(pl.col("pi_start").cast(pl.Float64).alias("s_start"))
        values, imputed = design_matrix(model.columns, model.means, frame)
        out["models"].append({"artifact_id": artifact_id, "position": pos, "model_kind": kind,
                              "model_version": artifact.version, "columns": list(model.columns),
                              "means": [float(v) for v in model.means], "stds": [float(v) for v in model.stds],
                              "coef": [float(v) for v in model.coef], "intercept": float(model.intercept)})
        for i, g in enumerate(frame["gsis_id"].to_list()):
            r = projected[g]
            out["rows"].append({"espn_player_id": int(r["espn_player_id"]), "artifact_id": artifact_id, "position": pos,
                                "model_kind": kind, "input_values": [float(v) for v in values[i]],
                                "imputed": [bool(v) for v in imputed[i]], "mixture_column": mix_col,
                                "inputs_version": INPUTS_VERSION, "row": row_fingerprint(r)})
    return out


def _week_report(sources: Sources, season: int, week: int) -> dict | None:
    """The week's synced nflverse injury report as {team: {gsis_id: report_status}} (teams with any row), or None."""
    if sources.injuries is None:
        return None
    out: dict = {}
    cur = sources.injuries.filter((pl.col("season") == season) & (pl.col("week") == week))
    for g, t, st in cur.select("gsis_id", "team", "report_status").iter_rows():
        if t is not None:
            out.setdefault(t, {})[g] = (st or "").strip().lower()
    return out


def _synced_clear(report: dict, gsis_id: str, team: str, hist: list[dict], week: int, espn_status: str) -> bool:
    """Horizon-0 evidence that he plays: his team's week report is synced without Out/Doubtful for him, his week's
    weekly roster row is ACT, and ESPN does not list him OUT / IR / suspended."""
    if team not in report or report[team].get(gsis_id) in ("out", "doubtful"):
        return False
    if not hist or hist[-1]["week"] != week or (hist[-1].get("status") or "").upper() != "ACT":
        return False
    return espn_status not in WITHHELD_ESPN


def _apply_news_p_play(rows: list[dict], nstate: dict) -> None:
    """P(plays) of a projected row the availability news speaks for: the model's estimate times the news probability
    (1 outside a return window). The news is more specific than the return-rate table, so a row whose estimate the
    table replaced uses the undesignated estimate it replaced. A player the participation model has no estimate for (no ACT/INA weekly roster row
    yet, e.g. reinstated from the exempt list) gets the median estimate of this run's projected rows at his position
    without an injury flag on the participation model, labelled as such."""
    typical: dict[str, float] = {}
    for pos in {r["position"] for r in rows}:
        ps = sorted(r["p_play"] for r in rows if r["position"] == pos and r.get("coverage_status") == "projected"
                    and r.get("p_play") is not None and r["gsis_id"] not in nstate
                    and (r.get("participation") or {}).get("model_version") != "return-rate-table")
        if ps:
            typical[pos] = ps[len(ps) // 2]
    for rec in rows:
        ns = nstate.get(rec.get("gsis_id"))
        if ns is None or rec.get("coverage_status") != "projected":
            continue
        q = dict(rec.get("participation") or {})
        base = q["undesignated_p_play"] if q.get("undesignated_p_play") is not None else rec.get("p_play")
        if base is None:
            base = typical.get(rec["position"])
            if base is None:
                continue
            q["base_p_play_source"] = f"median P(plays) of this run's projected {rec['position']} rows (no estimate of his own)"
        p = round(base * ns.p, 4)
        q.update(news_p_play=ns.p, base_p_play=base, p_play=p, availability_news=an.NEWS_VERSION)
        rec.update(p_play=p, expected_points=round(p * rec["projected_points"], 2), participation=q)


def _injury_model(artifact):
    """app.analytics.injury_model for an ifp-2.1.0 artifact (its config carries "injury_model"), else None: other
    versions never import it."""
    if not isinstance((artifact.config or {}).get("injury_model"), dict):
        return None
    from . import injury_model as im

    return im


def _context_model(artifact):
    """app.analytics.context_model for an ifp-2.2.0 artifact (its config carries "context_model"), else None."""
    if not isinstance((artifact.config or {}).get("context_model"), dict):
        return None
    from . import context_model as cm

    return cm


def _roster_codes(con, season: int):
    from .injury_workload import load_roster_codes

    return load_roster_codes(con, [season])


def write_run(con, meta: dict, rows: list[dict], inputs: dict | None = None) -> dict:
    """Store one run, append-only. Returns {run_id, created, row_count, content_sha256}.

    * `meta["run_id"]` already stored with the same content -> that run, nothing written;
    * `meta["run_id"]` already stored with different content -> ImmutableRunError (history is never rewritten);
    * the NEWEST run of the same (season, week, model_version, scoring_fingerprint, artifact_id) has the same
      content_sha256 -> that run's id, nothing written (an idempotent retry; a revert to an older run's
      content after a change is a new run);
    * anything else (including a source correction that changes the forecasts) -> a NEW run.
    Stored rows are re-read inside the transaction and must reproduce the digest, else nothing is committed.
    `inputs` (from `build_live(..., inputs_out=)`) are stored with a NEW run only, beside the rows they were captured
    for; they never change the run's digest, and a retry that returns an existing run writes none."""
    if not rows:
        raise ValueError("refusing to store a run with no rows")
    run_id, digest = meta["run_id"], content_sha256(meta, rows)
    prepared, inputs_error = _prepare_inputs(inputs, rows) if inputs else (None, None)
    con.execute("BEGIN TRANSACTION")
    try:
        same = fetch_dicts(con, f"SELECT run_id, content_sha256, row_count FROM {RUNS} WHERE run_id = ?", [run_id])
        if same:
            if same[0]["content_sha256"] != digest or same[0]["row_count"] != len(rows):
                raise ImmutableRunError(f"run {run_id} is already stored with different content; "
                                        "committed runs are write-once")
            result = {"run_id": run_id, "created": False}
        else:
            # Only the NEWEST stored run of this (week, model, scoring, artifact) can be "the same": a later
            # run that reverts to an older run's content (A, B, back to A) is a new computation and must be
            # newest, or consumers would keep serving B.
            twin = fetch_dicts(con, f"""
                SELECT run_id, content_sha256 FROM {RUNS} WHERE season = ? AND week = ? AND model_version = ?
                  AND scoring_fingerprint IS NOT DISTINCT FROM ? AND artifact_id IS NOT DISTINCT FROM ?
                  AND coalesce(forecast_horizon, 0) = ?
                ORDER BY created_at DESC, run_id DESC LIMIT 1""",
                               [meta["season"], meta["week"], meta["model_version"], meta.get("scoring_fingerprint"),
                                meta.get("artifact_id"), horizon_of(meta)])
            twin = [t for t in twin if t["content_sha256"] == digest]
            if twin:
                result = {"run_id": twin[0]["run_id"], "created": False}
            else:
                kickoffs = team_kickoffs(con, meta["season"], meta["week"])
                stamped = annotate([{**r, "run_id": run_id} for r in rows], meta["season"], meta["week"],
                                   meta.get("created_at") or utcnow(), kickoffs)
                insert_rows(con, RUNS, [{**meta, "content_sha256": digest, "row_count": len(rows),
                                         "migrated_from_legacy": False}])
                insert_rows(con, ROWS, stamped)
                back = fetch_dicts(con, f"SELECT * FROM {ROWS} WHERE run_id = ?", [run_id])
                if len(back) != len(rows) or content_sha256(meta, back) != digest:
                    raise RuntimeError(f"run {run_id}: stored rows do not reproduce the content digest")
                if prepared is not None:
                    _store_inputs(con, meta, prepared)
                result = {"run_id": run_id, "created": True}
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    out = {**result, "row_count": len(rows), "content_sha256": digest}
    if inputs:
        out["inputs_stored"] = len(prepared["rows"]) if prepared is not None and result["created"] else 0
        if inputs_error:
            out["inputs_error"] = inputs_error
    return out


def _prepare_inputs(captured: dict, rows: list[dict]) -> tuple[dict | None, str | None]:
    """The captured inputs to store, before the run's transaction: only those of rows whose content is still the
    captured row's (a row a caller replaced after `build_live`, e.g. a re-projection's frozen row, keeps none), with
    the models they use. A malformed capture is dropped with its reason, so it can never block the forecasts."""
    try:
        fp = {int(r["espn_player_id"]): row_fingerprint(r) for r in rows}
        keep = [{k: v for k, v in r.items() if k != "row"} for r in captured.get("rows") or []
                if fp.get(int(r["espn_player_id"])) == r.get("row")]
        for r in keep:
            if len(r["input_values"]) != len(r["imputed"]):
                raise ValueError(f"player {r['espn_player_id']}: values and imputed flags differ in length")
        used = {(r["artifact_id"], r["position"], r["model_kind"]) for r in keep}
        models = [m for m in captured.get("models") or [] if (m["artifact_id"], m["position"], m["model_kind"]) in used]
        if {(m["artifact_id"], m["position"], m["model_kind"]) for m in models} != used:
            raise ValueError("an input row names a model the capture does not hold")
        return {"rows": keep, "models": models}, None
    except (KeyError, TypeError, ValueError) as err:
        return None, f"{type(err).__name__}: {err}"


def _store_inputs(con, meta: dict, prepared: dict) -> None:
    """A newly stored run's prepared inputs (inside write_run's transaction); a later target week of a cycle is stored
    as its changes against the cycle's earlier whole row."""
    if not prepared["rows"]:
        return
    write_models(con, prepared["models"])
    write_inputs(con, meta["run_id"], prepared["rows"],
                 cycle_bases(con, meta, [int(r["espn_player_id"]) for r in prepared["rows"]]))


APPROVED_SOURCES = ("learning_cycle", "manual_pin", "learning_cycle_backfill")


def register_approved_artifact(con, *, model_version: str, artifact_sha256: str, target_season: int | None,
                               target_week: int | None, training_cutoff_key: int | None = None,
                               artifact_created_at: str | None = None, source: str) -> bool:
    """Record an artifact as approved for `model_version` in the write-once approved_artifacts table.
    INSERT ... ON CONFLICT DO NOTHING on (model_version, artifact_sha256): returns True if the row was
    created, False if it already existed unchanged (an existing row is never updated or deleted, whatever
    the arguments). Malformed input raises instead of being stored or dropped silently."""
    if not model_version or not isinstance(artifact_sha256, str) or len(artifact_sha256) != 64 \
            or any(c not in "0123456789abcdef" for c in artifact_sha256):
        raise ValueError("an approved artifact needs a model version and the lowercase hex sha256 of the artifact file")
    if source not in APPROVED_SOURCES:
        raise ValueError(f"source must be one of {', '.join(APPROVED_SOURCES)}")
    before = con.execute("SELECT count(*) FROM approved_artifacts").fetchone()[0]
    con.execute("INSERT INTO approved_artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                [model_version, artifact_sha256, target_season, target_week, training_cutoff_key,
                 artifact_created_at, source, utcnow()])
    return con.execute("SELECT count(*) FROM approved_artifacts").fetchone()[0] > before


def write_live(con, meta: dict, rows: list[dict]) -> int:
    """Compatibility wrapper around `write_run`; returns the number of rows in the run."""
    return write_run(con, meta, rows)["row_count"]
