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
"""

from __future__ import annotations

import hashlib
import json
import uuid

import polars as pl

from ..db.connection import fetch_dicts, insert_rows, table_exists, utcnow
from ..espn.constants import IR_SLOT
from .forecast_features import POSITIONS, Sources, features_for_week, key
from .independent_projections import Artifact, flags_for, missing_inputs, predict_frame
from .league_scoring import resolve_skill_scoring
from .player_availability import RESTRICTED, derive
from .production_forecasts import (HORIZON_NOTE, NEXT_GAME_ONLY, ROWS, RUNS, ImmutableRunError, annotate, content_sha256,
                                   horizon_of, team_kickoffs)

WITHHELD_ESPN = {"OUT", "INJURY_RESERVE", "SUSPENSION"}
LONG_TERM_ESPN = {"INJURY_RESERVE", "SUSPENSION"}   # withheld for every remaining week; OUT is the next game only
FUTURE_NO_REPORT = "future week: no injury report yet"
LONG_TERM_UNAVAILABLE = "long-term unavailable; return date unknown"
RESERVE_ROSTER = {"RES", "PUP", "NFI"}   # NFL reserve lists: player_availability.derive can date a return for these
ACTIVE_ROSTER = {"ACT", "INA"}     # nflverse weekly roster: active 53 (INA = game-day inactive that week)
SUPPORTED_MAPPING = "high"
# Row `model` label per fitted model name: ifp-1.0.0 selects B or C per position, ifp-2.0.0 fits "ifp2" ridges.
MODEL_LABELS = {"B": "opportunity", "C": "context", "ifp2": "ifp2_ridge"}


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
    The next unplayed week is the week after the latest week with any final game (a postponed or cancelled game
    in an otherwise final week does not block); with no final game this season it is the season's first week."""
    played = sources.games.filter((pl.col("season") == season) & pl.col("points_for").is_not_null())
    if played.height:
        nxt = int(played["week"].max()) + 1
    else:
        weeks = sources.games.filter(pl.col("season") == season)["week"]
        if weeks.len() == 0:
            return
        nxt = int(weeks.min())
    if cutoff_week > nxt:
        raise HorizonError(f"cutoff week {cutoff_week} is after the next unplayed week {nxt} of {season}: current "
                           f"injury and roster status describe week {nxt}'s game, not week {cutoff_week}'s; project "
                           f"week {cutoff_week} as a rest-of-season target (projected_from_week={nxt})")


def build_live(con, sources: Sources, artifact: Artifact, season: int, week: int,
               participation=None, artifact_sha256: str | None = None, *,
               projected_from_week: int | None = None) -> tuple[dict, list[dict]]:
    """Run metadata + one row per candidate. `sources` must contain the live season (and
    ideally the previous season from the history DB). `participation`: optional
    ParticipationArtifact; without it p_play and expected_points stay null. `artifact_sha256`: sha256 of
    the artifact file the caller loaded (scripts/backtest_independent_projections.py project), stored as
    the run's artifact_id so it can be compared with the approval registry's pinned sha.
    `projected_from_week`: W, the first unplayed week, when `week` is a later target week T (rest-of-season
    projection, horizon T - W; see the module docstring). None (or == week) is the normal horizon-0 run."""
    fw = week if projected_from_week is None else projected_from_week
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
        # Horizon >= 1: an NFL reserve-list player whose return derive() can date (reserve rule code -> earliest
        # eligible week) is withheld only for the weeks in unavailable_weeks and projected after them, even when ESPN
        # also lists INJURY_RESERVE; every other long-term status stays withheld for all remaining weeks.
        dated_return = bool(horizon and roster_status in RESERVE_ROSTER and avail["status"] in RESTRICTED
                            and avail["earliest_eligible_week"] is not None)
        # ESPN OUT is the next game only; INJURY_RESERVE / SUSPENSION last past it (horizon 0: all three withhold).
        espn_withheld = espn_status in (LONG_TERM_ESPN if horizon else WITHHELD_ESPN) \
            and not (dated_return and espn_status == "INJURY_RESERVE")
        if (avail["status"] in RESTRICTED and week in avail["unavailable_weeks"]) or espn_withheld:
            rec["coverage_status"] = "withheld_unavailable"
            rec["missing_inputs"] = [w for w in avail["warnings"]] or [f"ESPN status {c['injury_status']}"]
            if horizon and espn_withheld:
                rec["missing_inputs"] = [f"{LONG_TERM_UNAVAILABLE} (ESPN status {c['injury_status']})"]
            continue
        # (a dated return not yet reached was withheld above; from the eligible week on he is projected)
        if dated_return:
            rec["flags"].append(f"returns from reserve (eligible week {avail['earliest_eligible_week']}): return "
                                "not confirmed, assumed available")
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
    if targets:
        tdf = pl.DataFrame(targets).unique("gsis_id", keep="first")
        feats = features_for_week(sources, season, fw, tdf)    # feature week W; opponent / home-away are T's
        preds = {p["gsis_id"]: p for p in predict_frame(artifact, feats)}
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
    return meta, rows


def write_run(con, meta: dict, rows: list[dict]) -> dict:
    """Store one run, append-only. Returns {run_id, created, row_count, content_sha256}.

    * `meta["run_id"]` already stored with the same content -> that run, nothing written;
    * `meta["run_id"]` already stored with different content -> ImmutableRunError (history is never rewritten);
    * the NEWEST run of the same (season, week, model_version, scoring_fingerprint, artifact_id) has the same
      content_sha256 -> that run's id, nothing written (an idempotent retry; a revert to an older run's
      content after a change is a new run);
    * anything else (including a source correction that changes the forecasts) -> a NEW run.
    Stored rows are re-read inside the transaction and must reproduce the digest, else nothing is committed."""
    if not rows:
        raise ValueError("refusing to store a run with no rows")
    run_id, digest = meta["run_id"], content_sha256(meta, rows)
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
                result = {"run_id": run_id, "created": True}
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return {**result, "row_count": len(rows), "content_sha256": digest}


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
