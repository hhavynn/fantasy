"""Live week-W independent projections: build targets from the live DB, apply a frozen artifact,
persist rows for the read-only API. Never trains; never touches ESPN projection storage.

Guards (each produces an explicit coverage_status and a NULL projection, never a zero):
  unsupported_scoring    league offensive scoring differs from the artifact's training scoring
  unsupported_position   K and D/ST (no credible independent model in V1)
  uncertain_id_mapping   no high-confidence ESPN->GSIS crosswalk (a wrong id would borrow
                         another player's history)
  bye                    the player's team has no game in week W
  withheld_unavailable   NFL-ineligible for week W (player_availability rule) or ESPN status
                         OUT / INJURY_RESERVE / SUSPENSION
  abstain_no_history     no NFL game in the lookback window (rookie debut, long absence)
  no_model               the artifact has no model for the position
"""

from __future__ import annotations

import json
import uuid

import polars as pl

from ..db.connection import fetch_dicts, replace_where, table_exists, upsert_rows, utcnow
from ..espn.constants import IR_SLOT
from .forecast_features import POSITIONS, Sources, features_for_week, key
from .independent_projections import Artifact, flags_for, missing_inputs, predict_frame
from .league_scoring import resolve_skill_scoring
from .player_availability import RESTRICTED, derive

WITHHELD_ESPN = {"OUT", "INJURY_RESERVE", "SUSPENSION"}
SUPPORTED_MAPPING = "high"


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


def build_live(con, sources: Sources, artifact: Artifact, season: int, week: int) -> tuple[dict, list[dict]]:
    """Run metadata + one row per candidate. `sources` must contain the live season (and
    ideally the previous season from the history DB)."""
    _, scoring = league_scoring(con, season)
    scoring_ok = scoring.supported and scoring.fingerprint == artifact.scoring_fingerprint
    games = sources.games.filter((pl.col("season") == season) & (pl.col("week") == week))
    slate = {r["team"]: r for r in games.iter_rows(named=True)}
    team_weeks: dict[str, list[int]] = {}
    for r in sources.games.filter(pl.col("season") == season).iter_rows(named=True):
        team_weeks.setdefault(r["team"], []).append(int(r["week"]))
    rosters = _roster_rows(con, season, week)
    on_ir = _fantasy_ir(con, season, week)
    rows, targets = [], []
    for c in candidates(con, season, week):
        rec = {"season": season, "week": week, "espn_player_id": c["espn_player_id"], "gsis_id": c["gsis_id"],
               "position": c["position"], "projected_points": None, "interval_low": None, "interval_high": None,
               "model": None, "games_used": None, "latest_usage_season": None, "latest_usage_week": None,
               "missing_inputs": [], "flags": [], "explanation": [], "nfl_team": c["pro_team"], "opponent": None,
               "is_home": None}
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
        if (avail["status"] in RESTRICTED and week in avail["unavailable_weeks"]) \
                or (c["injury_status"] or "").upper() in WITHHELD_ESPN:
            rec["coverage_status"] = "withheld_unavailable"
            rec["missing_inputs"] = [w for w in avail["warnings"]] or [f"ESPN status {c['injury_status']}"]
            continue
        if (c["injury_status"] or "").upper() in ("QUESTIONABLE", "DOUBTFUL"):
            rec["flags"].append(f"ESPN injury status {c['injury_status']}: projection assumes he plays")
        rec["coverage_status"] = "pending"
        targets.append({"gsis_id": c["gsis_id"], "position": c["position"], "team": team,
                        "opponent": game["opponent"], "is_home": bool(game["is_home"])})
    preds: dict[str, dict] = {}
    if targets:
        tdf = pl.DataFrame(targets).unique("gsis_id", keep="first")
        feats = features_for_week(sources, season, week, tdf)
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
                   model={"B": "opportunity", "C": "context"}[artifact.models[rec["position"]].model],
                   explanation=p["explanation"])
    before = sources.panel.filter(pl.col("season") * 100 + pl.col("week") < key(season, week))
    last = before.select(pl.col("season").max(), pl.col("week").filter(
        pl.col("season") == pl.col("season").max()).max()).row(0) if before.height else (None, None)
    synced = fetch_dicts(con, "SELECT max(finished_at) AS t FROM sync_runs WHERE source = 'nfl' AND status = 'ok'") \
        if table_exists(con, "sync_runs") else [{"t": None}]
    meta = {"run_id": f"ifp-{season}w{week}-{utcnow():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}", "season": season,
            "week": week, "model_version": artifact.version, "scoring_fingerprint": scoring.fingerprint if scoring.supported else None,
            "artifact_created_at": artifact.created_at, "data_through_season": last[0], "data_through_week": last[1],
            "nfl_synced_at": synced[0]["t"], "model_status": artifact.status, "evaluation": artifact.evaluation,
            "created_at": utcnow()}
    for rec in rows:
        rec["run_id"] = meta["run_id"]
    return meta, rows


def write_live(con, meta: dict, rows: list[dict]) -> int:
    con.execute("BEGIN TRANSACTION")
    try:
        upsert_rows(con, "independent_projection_runs", [meta], ["run_id"])
        n = replace_where(con, "independent_player_projections", rows, "season = ? AND week = ?",
                          [meta["season"], meta["week"]])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return n
