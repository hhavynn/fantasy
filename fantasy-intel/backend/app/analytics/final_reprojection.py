"""Final-designation re-projection of the production week run (docs/independent_projection_engine.md,
"Final-designation re-projection").

The learning cycle stores the week's production run (projection_source.MODEL_VERSION, horizon 0) Tue-Thu, before the
first kickoff.
The final game designations (nflverse injury report: Out / Doubtful / Questionable) land on Friday, after that
run, so it keeps mid-week injury inputs (a player listed Questionable by ESPN on Wednesday and Out on Friday's
report keeps his Wednesday p_play). This module builds a LATER run of the same week with the same approved
artifact, so expected_points reflects the final designations:

  * it never modifies a stored run: the result is a NEW run written with independent_projection_store.write_run
    (append-only, content-hashed). projection_source picks the newest verified run, so consumers see it without
    any history being rewritten; nothing here touches the weekly-learning ledger or any evidence reader;
  * the base is the newest verified horizon-0 run of the model under the league's scoring, and the re-projection
    must use that run's artifact file (same sha256): only the inputs that changed since (injury report, rosters,
    ESPN status) can move a number, never a different model;
  * a team whose kickoff is at or before the new run's created_at (or whose kickoff is unknown) is NOT
    re-projected: its players' rows are carried byte-for-byte from the base run, plus the CARRIED flag. A row
    recomputed after kickoff could never be a pregame forecast; a carried row is stored with pregame = false
    (write_run annotates it), so production_forecasts.pregame_forecasts keeps reading the base run's row for that
    player, and the consumer view keeps showing the pregame number, labeled;
  * when the re-projected rows equal the base run's rows nothing is written (status "unchanged");
  * injury retrieval times come from injury_freshness.team_freshness and are reported only: as everywhere else,
    freshness never gates or changes a forecast.

Local only (polars, the model packages): the hosted API never imports this module.
"""

from __future__ import annotations

from datetime import datetime
import json

from ..db.connection import table_exists
from . import injury_freshness as fresh
from . import production_forecasts as pf
from .independent_projection_store import build_live, write_run

SCHEMA = "ifp-final-reprojection/v1"
CARRIED = "kickoff passed before this re-projection: row carried unchanged from run {run_id}"
CARRIED_PREFIX = CARRIED.split("{")[0]
WRITTEN, UNCHANGED = "written", "unchanged"


class ReprojectRefused(ValueError):
    """The re-projection cannot run (no base run, a base run without a file-sha artifact, nothing still pregame)."""


def in_progress_week(con, season: int, now: datetime) -> int | None:
    """The REG week of the next team kickoff after `now`: the week being played (after Thursday night) or the next
    one (after Monday night). None when no known kickoff of the season is ahead."""
    t = fresh.naive_utc(now)
    ahead = [(fresh.naive_utc(ko), week) for (week, _), ko in pf.team_kickoffs(con, season).items()
             if fresh.naive_utc(ko) > t]
    return min(ahead)[1] if ahead else None


def base_run(con, season: int, week: int, model_version: str, scoring_fingerprint: str) -> dict | None:
    """{run, rows}: the newest horizon-0 run of the week that the artifact gate accepts and whose rows verify against
    its content hash (production_forecasts.verified_runs), or None."""
    for run, rows, problem in pf.verified_runs(con, season, week, model_version=model_version,
                                               scoring_fingerprint=scoring_fingerprint):
        if problem is None and pf.horizon_of(run) == 0:
            return {"run": run, "rows": rows}
    return None


def check_base(base: dict | None, season: int, week: int, model_version: str) -> str:
    """The base run's artifact sha256 (the file the re-projection must load), or ReprojectRefused."""
    if base is None:
        raise ReprojectRefused(f"no verified {model_version} week-{week} run of {season} to re-project: the learning "
                               "cycle stores it Tue-Thu (learning_cycle.py run); a week without one is never backfilled")
    sha = base["run"].get("artifact_id") or ""
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ReprojectRefused(f"base run {base['run']['run_id']} has no artifact file sha256 ({sha or 'none'}); "
                               "only a run made from a stored, approved artifact file can be re-projected")
    return sha


def check_participation(base: dict, participation) -> None:
    """The re-projection must use the participation model the base run recorded (its participation_version), or
    none when the base used none: rebuilding without it would store rows with no p_play / expected_points as the
    newest verified run, and the consumer view would fall back to ESPN for those players."""
    recorded = base["run"].get("participation_version")
    used = getattr(participation, "version", None) if participation is not None else None
    if recorded != used:
        raise ReprojectRefused(
            f"base run {base['run']['run_id']} used participation model {recorded or 'none'}, but the re-projection "
            f"would use {used or 'none'}: the base run's participation file must be available and reused")


def kicked_off(con, season: int, week: int, at) -> tuple[set[str], set[str], dict]:
    """(teams whose kickoff is at or before `at`, teams with an unknown kickoff, {team: kickoff}) for the week's
    scheduled games (injury_freshness.scheduled_kickoffs; teams on a bye have no game and are absent)."""
    t = fresh.naive_utc(at)
    sched = fresh.scheduled_kickoffs(con, season, week)
    unknown = {team for team, ko in sched.items() if ko is None}
    started = {team for team, ko in sched.items() if ko is not None and fresh.naive_utc(ko) <= t}
    return started, unknown, sched


def carried_row(row: dict, run_id: str) -> dict:
    """A base-run row as stored (every row column), with the CARRIED flag naming the run it was first made in.
    A row already carried by an earlier re-projection keeps its original flag (no second one)."""
    out = {c: row.get(c) for c in pf.ROW_COLUMNS}
    flags = row.get("flags")
    flags = json.loads(flags) if isinstance(flags, (str, bytes, bytearray)) else list(flags or [])
    if not any(isinstance(f, str) and f.startswith(CARRIED_PREFIX) for f in flags):
        flags.append(CARRIED.format(run_id=run_id))
    out["flags"] = flags
    return out


def merge(base: dict, rows: list[dict], frozen_teams: set[str]) -> tuple[list[dict], dict]:
    """(rows to store, partition). A player is frozen when his team in the base run OR in the new rows is in
    `frozen_teams`: his base row is carried; a frozen player without a base row is dropped (he would need a
    projection made after his kickoff). Every other player gets his re-projected row."""
    base_rows = {int(r["espn_player_id"]): r for r in base["rows"]}
    frozen = {pid for pid, r in base_rows.items() if r.get("nfl_team") in frozen_teams}
    frozen |= {int(r["espn_player_id"]) for r in rows if r.get("nfl_team") in frozen_teams}
    fresh_rows = [r for r in rows if int(r["espn_player_id"]) not in frozen]
    carried = [carried_row(base_rows[pid], base["run"]["run_id"]) for pid in sorted(frozen) if pid in base_rows]
    dropped = sorted(pid for pid in frozen if pid not in base_rows)
    return fresh_rows + carried, {"reprojected": len(fresh_rows), "carried": len(carried), "dropped_post_kickoff":
                                  len(dropped), "frozen_player_ids": sorted(frozen)}


def changes(base: dict, rows: list[dict], frozen_ids: set[int]) -> dict:
    """Aggregate differences between the base run and the re-projection, for the report (no player names)."""
    before = {int(r["espn_player_id"]): r for r in base["rows"] if int(r["espn_player_id"]) not in frozen_ids}
    after = {int(r["espn_player_id"]): r for r in rows if int(r["espn_player_id"]) not in frozen_ids}

    def num(v):
        return None if v is None else round(float(v), 2)

    moved = [pid for pid in before.keys() & after.keys()
             if num(before[pid].get("expected_points")) != num(after[pid].get("expected_points"))]
    status = [pid for pid in before.keys() & after.keys()
              if before[pid].get("coverage_status") != after[pid].get("coverage_status")]
    delta = sum(abs((num(after[p].get("expected_points")) or 0.0) - (num(before[p].get("expected_points")) or 0.0))
                for p in moved)
    by_team: dict[str, int] = {}
    for pid in sorted(set(moved) | set(status)):
        team = after[pid].get("nfl_team") or "?"
        by_team[team] = by_team.get(team, 0) + 1
    return {"expected_points_changed": len(moved), "coverage_status_changed": len(status),
            "sum_abs_expected_points_change": round(delta, 2), "players_added": len(after.keys() - before.keys()),
            "players_removed": len(before.keys() - after.keys()), "changed_by_team": by_team}


def freshness(con, season: int, week: int, at, base_created_at, sched: dict) -> dict:
    """Per-team injury retrieval times (injury_freshness.team_freshness) and whether each team's evidence was
    retrieved after the base run was made. Informational only: never a gate, never a model input."""
    try:
        if not table_exists(con, fresh.TABLE):
            return {"available": False, "reason": f"{fresh.LOG_UNAVAILABLE} (no {fresh.TABLE} table)"}
        teams = fresh.team_freshness(con, season, week, at, kickoffs=sched)
    except Exception as err:      # noqa: BLE001 - diagnostics never fail the re-projection
        return {"available": False, "reason": f"{fresh.LOG_UNAVAILABLE} ({type(err).__name__})"}
    base_t = fresh.naive_utc(base_created_at)
    out = []
    for t in teams:
        got = fresh.naive_utc(t["evidence_retrieved_at"])
        out.append({k: t[k] for k in ("team", "kickoff", "game_started", "source_used", "status", "reason",
                                      "evidence_retrieved_at", "provider_published_at", "pregame_label",
                                      "nflverse_designation_synced")}
                   | {"retrieved_after_base_run": bool(got and base_t and got > base_t)})
    return {"available": True, "base_run_created_at": None if base_t is None else base_t.isoformat(),
            "summary": fresh.summarize(teams),
            "teams_with_evidence_after_base_run": sorted(t["team"] for t in out if t["retrieved_after_base_run"]),
            "teams": out}


def build(con, sources, artifact, season: int, week: int, *, base: dict, participation=None,
          artifact_sha256: str, context_inputs=None, news: list | None = None) -> dict:
    """Read-only. Re-project `week` with the base run's artifact and return {meta, rows, base_run_id, status, ...}
    for `store`. `status` is UNCHANGED when every re-projected row equals the base run's (nothing to write)."""
    sha = check_base(base, season, week, artifact.version)
    if artifact_sha256 != sha:
        raise ReprojectRefused(f"artifact file sha256 {artifact_sha256[:12]} is not the base run's ({sha[:12]}): a "
                               "re-projection reuses the base run's approved artifact, never a newer model")
    check_participation(base, participation)
    captured: dict = {}
    meta, rows = build_live(con, sources, artifact, season, week, participation=participation,
                            artifact_sha256=artifact_sha256, context_inputs=context_inputs, news=news,
                            inputs_out=captured)
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
    fresh_new = [r for r in stored if int(r["espn_player_id"]) not in frozen_ids]
    fresh_base = [r for r in base["rows"] if int(r["espn_player_id"]) not in frozen_ids]
    same = pf.content_sha256(meta, fresh_new) == pf.content_sha256(base["run"], fresh_base)
    # a frozen row is the base run's, not this build's: it keeps no inputs (write_run also matches each row's content)
    captured["rows"] = [r for r in captured.get("rows") or [] if int(r["espn_player_id"]) not in frozen_ids]
    return {"meta": meta, "rows": stored, "inputs": captured, "base_run_id": base["run"]["run_id"],
            "base_run_created_at": str(base["run"]["created_at"]), "status": UNCHANGED if same else WRITTEN,
            "kicked_off_teams": sorted(started), "unknown_kickoff_teams": sorted(unknown),
            "missing_from_schedule_teams": sorted(missing), **part,
            "changes": changes(base, stored, frozen_ids),
            "injury_freshness": freshness(con, season, week, meta["created_at"], base["run"]["created_at"], sched)}


def store(con, built: dict) -> dict:
    """Write the re-projection as a NEW run (write_run: append-only; an identical retry is a no-op), unless `build`
    found nothing changed. Returns {status, run_id, created, row_count, content_sha256}."""
    if built["status"] == UNCHANGED:
        return {"status": UNCHANGED, "run_id": built["base_run_id"], "created": False}
    return {"status": WRITTEN, **write_run(con, built["meta"], built["rows"], inputs=built.get("inputs"))}
