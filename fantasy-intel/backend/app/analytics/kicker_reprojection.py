"""Final-designation re-projection of the kicker week run (kfp-*; docs/independent_projection_engine.md,
"Final-designation re-projection", kicker section).

The learning cycle stores the week's kicker run Tue-Thu (stage_kicker, horizon 0), before the first kickoff. Five
kicker inputs can still move after it: the betting line (kfp-1.1.0's team implied total, posted or moved late), the nflverse game designation (Friday's final report; kicker_live.participation
turns it into p_play), the ESPN status (OUT / IR / SUSPENSION withholds a kicker), the NFL weekly roster (a weekend
signing or release changes the team's expected kicker) and the NWS forecast (shown with the row; not a model input of
kfp-1.x). This module builds a LATER kicker run of the same week under the skill-position re-projection's rules:

  * it never modifies a stored run: the result is a NEW run written with kicker_store.write_run (append-only,
    content-hashed). kicker_store.select_run picks the newest verified run, so consumers see it without any history
    being rewritten; nothing here touches a ledger or an evidence reader;
  * the base is the verified horizon-0 kfp run consumers would pick under the league's kicker scoring (its own model
    version, kicker_store.CONSUMER_VERSIONS order), and the re-projection must load the artifact file whose sha256 is
    that run's artifact_id and write under that same version: only the inputs above can move a number, never a
    different fit;
  * a team whose kickoff is at or before the new run's created_at, whose kickoff is unknown, or whose game is missing
    from the refreshed schedule is NOT re-projected: its kickers' base rows are carried byte-for-byte plus the CARRIED
    flag (write_run stores them with pregame = false); a frozen kicker without a base row is dropped;
  * when the re-projected rows equal the base run's nothing is written (status "unchanged"). Weather retrieval
    bookkeeping (retrieved_at, age_hours, ...) is not a change: a forecast re-read with the same values writes nothing.

Local only (polars, the model packages): the hosted API never imports this module.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path

from . import injury_freshness as fresh
from . import kicker_store as ks
from .kicker_live import build_run
from .production_forecasts import team_kickoffs

SCHEMA = "kfp-final-reprojection/v1"
CARRIED = "kickoff passed before this re-projection: row carried unchanged from run {run_id}"
CARRIED_PREFIX = CARRIED.split("{")[0]
WRITTEN, UNCHANGED = "written", "unchanged"
WEATHER_BOOKKEEPING = ("retrieved_at", "issued_at", "age_hours", "stale", "reason")


class ReprojectRefused(ValueError):
    """The re-projection cannot run (no base run, no artifact file with the base run's sha, nothing still pregame)."""


def in_progress_week(con, season: int, now: datetime) -> int | None:
    """The REG week of the next team kickoff after `now`: the week being played (after Thursday night) or the next
    one (after Monday night). None when no known kickoff of the season is ahead."""
    t = fresh.naive_utc(now)
    ahead = [(fresh.naive_utc(ko), week) for (week, _), ko in team_kickoffs(con, season).items()
             if fresh.naive_utc(ko) > t]
    return min(ahead)[1] if ahead else None


def base_run(con, season: int, week: int, scoring_fingerprint: str) -> dict | None:
    """{run, rows}: the horizon-0 kicker run of the week consumers would pick (kicker_store.CONSUMER_VERSIONS in
    order, newest first within a version) that the artifact gate accepts and whose rows verify against its content
    hash (kicker_store.verified_runs), or None. The re-projection keeps that run's own model version."""
    for version in ks.CONSUMER_VERSIONS:
        for run, rows, problem in ks.verified_runs(con, season, week, model_version=version,
                                                   scoring_fingerprint=scoring_fingerprint):
            if problem is None and int(run.get("forecast_horizon") or 0) == 0:
                return {"run": run, "rows": rows}
    return None


def check_base(base: dict | None, season: int, week: int) -> str:
    """The base run's artifact sha256 (the file the re-projection must load), or ReprojectRefused."""
    if base is None:
        raise ReprojectRefused(f"no verified kfp week-{week} kicker run of {season} to re-project: the "
                               "learning cycle stores it Tue-Thu (stage_kicker); a week without one is never backfilled")
    sha = base["run"].get("artifact_id") or ""
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ReprojectRefused(f"base run {base['run']['run_id']} has no artifact file sha256 ({sha or 'none'})")
    return sha


def artifact_file(directory: Path, sha: str) -> Path:
    """The kfp artifact file in `directory` whose sha256 is `sha` (the base run's), never a newer fit."""
    for f in sorted(Path(directory).glob("kfp-*.json")):
        if hashlib.sha256(f.read_bytes()).hexdigest() == sha:
            return f
    raise ReprojectRefused(f"no kfp artifact file with sha256 {sha[:12]} in {directory}: the base run's artifact must "
                           "be re-used, never replaced")


def kicked_off(con, season: int, week: int, at) -> tuple[set[str], set[str], dict]:
    """(teams whose kickoff is at or before `at`, teams with an unknown kickoff, {team: kickoff}) for the week's
    scheduled games (teams on a bye have no game and are absent)."""
    t = fresh.naive_utc(at)
    sched = fresh.scheduled_kickoffs(con, season, week)
    unknown = {team for team, ko in sched.items() if ko is None}
    started = {team for team, ko in sched.items() if ko is not None and fresh.naive_utc(ko) <= t}
    return started, unknown, sched


def _json(v):
    return json.loads(v) if isinstance(v, (str, bytes, bytearray)) else v


def carried_row(row: dict, run_id: str) -> dict:
    """A base-run row as stored (every row column), with the CARRIED flag naming the run it was first made in.
    A row already carried by an earlier re-projection keeps its original flag (no second one)."""
    out = {c: row.get(c) for c in ks.ROW_COLUMNS}
    flags = list(_json(row.get("flags")) or [])
    if not any(isinstance(f, str) and f.startswith(CARRIED_PREFIX) for f in flags):
        flags.append(CARRIED.format(run_id=run_id))
    out["flags"] = flags
    return out


def merge(base: dict, rows: list[dict], frozen_teams: set[str]) -> tuple[list[dict], dict]:
    """(rows to store, partition). A kicker is frozen when his team in the base run OR in the new rows is in
    `frozen_teams`: his base row is carried; a frozen kicker without a base row is dropped (he would need a
    projection made after his kickoff). Every other kicker gets his re-projected row."""
    base_rows = {int(r["espn_player_id"]): r for r in base["rows"]}
    frozen = {pid for pid, r in base_rows.items() if r.get("nfl_team") in frozen_teams}
    frozen |= {int(r["espn_player_id"]) for r in rows if r.get("nfl_team") in frozen_teams}
    fresh_rows = [r for r in rows if int(r["espn_player_id"]) not in frozen]
    carried = [carried_row(base_rows[pid], base["run"]["run_id"]) for pid in sorted(frozen) if pid in base_rows]
    dropped = sorted(pid for pid in frozen if pid not in base_rows)
    return fresh_rows + carried, {"reprojected": len(fresh_rows), "carried": len(carried), "dropped_post_kickoff":
                                  len(dropped), "frozen_player_ids": sorted(frozen)}


def comparable(row: dict) -> dict:
    """A row's content for the "anything changed?" test: every stored column, minus the weather retrieval
    bookkeeping (context.weather times/staleness and the forecast sentence that quotes them). The forecast values and
    status stay, so a new forecast is a change and a re-read of the same one is not."""
    out = {c: ks._canon(c, row.get(c)) for c in ks.ROW_COLUMNS}
    ctx = out.get("context")
    if isinstance(ctx, dict) and isinstance(ctx.get("weather"), dict):
        out["context"] = {**ctx, "weather": {k: v for k, v in ctx["weather"].items() if k not in WEATHER_BOOKKEEPING}}
    out["explanation"] = [e for e in (out.get("explanation") or [])
                          if not (isinstance(e, dict) and e.get("feature") == "weather_forecast")]
    return out


def changes(base: dict, rows: list[dict], frozen_ids: set[int]) -> dict:
    """Aggregate differences between the base run and the re-projection, for the report (no player names)."""
    before = {int(r["espn_player_id"]): r for r in base["rows"] if int(r["espn_player_id"]) not in frozen_ids}
    after = {int(r["espn_player_id"]): r for r in rows if int(r["espn_player_id"]) not in frozen_ids}

    def num(v):
        return None if v is None else round(float(v), 2)

    both = before.keys() & after.keys()
    moved = [pid for pid in both if num(before[pid].get("expected_points")) != num(after[pid].get("expected_points"))]
    status = [pid for pid in both if before[pid].get("coverage_status") != after[pid].get("coverage_status")]
    delta = sum(abs((num(after[p].get("expected_points")) or 0.0) - (num(before[p].get("expected_points")) or 0.0))
                for p in moved)
    by_team: dict[str, int] = {}
    for pid in sorted(set(moved) | set(status)):
        team = after[pid].get("nfl_team") or "?"
        by_team[team] = by_team.get(team, 0) + 1
    return {"expected_points_changed": len(moved), "coverage_status_changed": len(status),
            "sum_abs_expected_points_change": round(delta, 2), "players_added": len(after.keys() - before.keys()),
            "players_removed": len(before.keys() - after.keys()), "changed_by_team": by_team}


def build(con, frames: dict, artifact, season: int, week: int, *, base: dict, artifact_sha256: str,
          now: datetime | None = None, http_get=None, fetch_weather: bool = True) -> dict:
    """Re-project `week` with the base run's artifact and return {meta, rows, base_run_id, status, ...} for `store`.
    Writes nothing but weather snapshots (kicker_live.build_run). `status` is UNCHANGED when every re-projected row
    equals the base run's (`comparable`)."""
    sha = check_base(base, season, week)
    if artifact_sha256 != sha:
        raise ReprojectRefused(f"artifact file sha256 {artifact_sha256[:12]} is not the base run's ({sha[:12]}): a "
                               "re-projection reuses the base run's approved artifact, never a newer fit")
    if artifact.version != base["run"]["model_version"]:
        raise ReprojectRefused(f"artifact {artifact.version} is not the base run's model {base['run']['model_version']}")
    meta, rows = build_run(con, frames, artifact, season, week, artifact_sha256=artifact_sha256, now=now,
                           http_get=http_get, fetch_weather=fetch_weather)
    if meta["scoring_fingerprint"] != base["run"].get("scoring_fingerprint"):
        raise ReprojectRefused("the league's kicker scoring no longer matches the base run's: nothing to re-project")
    started, unknown, sched = kicked_off(con, season, week, meta["created_at"])
    # A team that had a game in the base run but is absent from the refreshed schedule (a transient feed gap) has an
    # unprovable kickoff too: freeze and carry it like an explicit unknown, never re-project it as a bye.
    missing = {r["nfl_team"] for r in base["rows"] if r.get("opponent") and r.get("nfl_team")} - set(sched)
    unknown = unknown | missing
    if sched and not (set(sched) - started - unknown):
        raise ReprojectRefused(f"every week-{week} game has kicked off (or has no known kickoff): nothing is still "
                               "pregame, so there is nothing to re-project")
    stored, part = merge(base, rows, started | unknown)
    frozen_ids = set(part.pop("frozen_player_ids"))
    # the run-level weather summary describes the rows actually stored (carried rows keep their base forecast)
    states = [(_json(r.get("context")) or {}).get("weather", {}).get("status") for r in stored if r.get("context")]
    meta = {**meta, "weather_summary": {s: states.count(s) for s in sorted({s for s in states if s})}}
    new = {int(r["espn_player_id"]): comparable(r) for r in stored if int(r["espn_player_id"]) not in frozen_ids}
    old = {int(r["espn_player_id"]): comparable(r) for r in base["rows"] if int(r["espn_player_id"]) not in frozen_ids}
    return {"meta": meta, "rows": stored, "base_run_id": base["run"]["run_id"],
            "base_run_created_at": str(base["run"]["created_at"]), "status": UNCHANGED if new == old else WRITTEN,
            "kicked_off_teams": sorted(started), "unknown_kickoff_teams": sorted(unknown),
            "missing_from_schedule_teams": sorted(missing), **part, "changes": changes(base, stored, frozen_ids)}


def store(con, built: dict) -> dict:
    """Write the re-projection as a NEW run (write_run: append-only; an identical retry is a no-op), unless `build`
    found nothing changed. Returns {status, run_id, created, row_count, content_sha256}."""
    if built["status"] == UNCHANGED:
        return {"status": UNCHANGED, "run_id": built["base_run_id"], "created": False}
    return {"status": WRITTEN, **ks.write_run(con, built["meta"], built["rows"])}
