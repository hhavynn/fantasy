"""Read-only, aggregate ESPN forecast audit. Never called by production analytics.

See docs/projection_calibration.md for eligibility, timestamp and sampling limits.
The input connection may be read-only; only SELECT queries are issued here.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from itertools import combinations
import json
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from ..db.connection import fetch_dicts

UTC = timezone.utc
EASTERN = ZoneInfo("America/New_York")
POSITIONS = ("QB", "RB", "WR", "TE")
HORIZONS = ("0-24h", "24-72h", "72-168h", "168h+")
COHORT_NOTE = (
    "Higher/lower pregame projected points are a scoring-volume proxy, not measured "
    "NFL workload or verified starter status. The configurable cutoff is descriptive, "
    "not a calibration factor. Compare positions separately."
)


def _utc(value) -> datetime | None:
    """Repository TIMESTAMP values are naive UTC (app.db.connection.utcnow)."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _number(value) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _kickoff(game: dict) -> datetime | None:
    """nflverse gameday + gametime is Eastern, including overseas games."""
    try:
        day = date.fromisoformat(str(game["gameday"]))
        clock = time.fromisoformat(str(game["gametime"]))
        if clock.tzinfo is not None:
            return None  # This schema promises a local Eastern clock, not an offset.
        local = datetime.combine(day, clock, tzinfo=EASTERN)
        # Refuse ambiguous/nonexistent clocks instead of guessing a DST fold.
        if local.utcoffset() != local.replace(fold=1).utcoffset():
            return None
        if local.astimezone(UTC).astimezone(EASTERN).replace(tzinfo=None) != local.replace(tzinfo=None):
            return None
        return local.astimezone(UTC)
    except (KeyError, TypeError, ValueError):
        return None


def _metrics(observations: list[dict], min_samples: int) -> dict:
    n = len(observations)
    errors = [r["actual"] - r["projected"] for r in observations]
    return {
        "n": n,
        "mae": math.fsum(abs(e) for e in errors) / n if n else None,
        "rmse": math.sqrt(math.fsum(e * e for e in errors) / n) if n else None,
        "bias": math.fsum(errors) / n if n else None,
        "status": "descriptive sample available" if n >= min_samples else "insufficient historical observations",
    }


def _table(con, name: str, required: set[str], issues: list[str], *, optional=False, season=None) -> list[dict]:
    if con is None:
        if not optional:
            issues.append(f"{name}: database unavailable")
        return []
    columns = {r["column_name"] for r in fetch_dicts(con,
        "SELECT column_name FROM information_schema.columns WHERE table_schema='main' AND table_name=?", [name])}
    if not required <= columns:
        if not optional:
            issues.append(f"{name}: missing columns {', '.join(sorted(required - columns))}")
        return []
    # name is supplied only by the fixed table list below, never user input.
    where = " WHERE season = ?" if season is not None and "season" in columns else ""
    return fetch_dicts(con, f'SELECT * FROM "{name}"{where}', [season] if where else [])


def _key(row: dict) -> tuple:
    return row["season"], row["espn_player_id"], row["scoring_period"]


def _horizon(hours: float) -> str:
    if hours < 24:
        return HORIZONS[0]
    if hours < 72:
        return HORIZONS[1]
    return HORIZONS[2] if hours < 168 else HORIZONS[3]


def _archive_actuals(con, root: Path | None, cutoff: datetime) -> tuple[dict, dict]:
    """Verify values in existing raw captures from successful, closed-week syncs.

    A shared weekly-points timestamp is insufficient: projection-only refreshes
    can change it. The archived league response must already have advanced past
    the actual's week when fetched. Never infer capture time from file mtime.
    """
    evidence = defaultdict(list)
    audit = {"archived_runs_read": 0, "unreadable_files": 0, "available": False}
    if root is None or not Path(root).is_dir():
        return evidence, audit
    root = Path(root).resolve()
    runs = _table(con, "sync_runs", {"run_id", "source", "status", "started_at", "finished_at"}, [], optional=True)

    def read(path):
        if not path.resolve().is_relative_to(root):
            audit["unreadable_files"] += 1
            return {}
        try:
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeError, ValueError):
            audit["unreadable_files"] += 1
            return {}

    def as_dict(value):
        return value if isinstance(value, dict) else {}

    def actual_entries(payload):
        wrappers = payload.get("players", [])
        if isinstance(wrappers, list):
            for wrapper in wrappers:
                yield as_dict(as_dict(wrapper).get("player"))
        teams = payload.get("teams", [])
        if isinstance(teams, list):
            for team in teams:
                entries = as_dict(as_dict(team).get("roster")).get("entries", [])
                if isinstance(entries, list):
                    for entry in entries:
                        yield as_dict(as_dict(as_dict(entry).get("playerPoolEntry")).get("player"))

    for run in runs:
        started, finished = _utc(run["started_at"]), _utc(run["finished_at"])
        run_id = str(run["run_id"])
        if (run["source"] != "espn" or run["status"] != "ok" or started is None or finished is None
                or finished < started or finished > cutoff or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id)):
            continue
        directory = root / run_id
        if not directory.is_dir() or not directory.resolve().is_relative_to(root):
            continue
        league = read(directory / "league.json")
        year, league_id, closed_before = league.get("seasonId"), league.get("id"), league.get("scoringPeriodId")
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in (year, league_id, closed_before)):
            continue
        audit["archived_runs_read"] += 1
        audit["available"] = True
        files = sorted(p for p in directory.iterdir()
                       if re.fullmatch(r"(?:cards|roster)_\d+\.json|pool\.json", p.name))
        for path in files:
            for player in actual_entries(read(path)):
                pid, stats = player.get("id"), player.get("stats", [])
                if not isinstance(pid, int) or not isinstance(stats, list):
                    continue
                for stat in stats:
                    stat = as_dict(stat)
                    week, points = stat.get("scoringPeriodId"), _number(stat.get("appliedTotal"))
                    if (stat.get("seasonId") == year and stat.get("statSourceId") == 0
                            and stat.get("statSplitTypeId") == 1 and isinstance(week, int)
                            and 0 < week < closed_before and points is not None):
                        evidence[(year, pid, week)].append((points, started, league_id))
    return evidence, audit


def backtest(con, *, season: int | None = None, as_of: datetime | None = None,
             min_samples: int = 30, high_projection_threshold: float = 10.0,
             raw_archive_root: Path | None = None) -> dict:
    """Return aggregate metrics and exclusions, without player-level output or writes.

    One latest valid pregame forecast per (season, player, scoring period) is used
    overall. Each horizon bin independently chooses its latest pregame forecast;
    pairwise comparisons use the intersection of player-weeks, not unpaired pools.
    as_of is an eligibility cutoff, NOT a replay of historical database revisions.
    """
    if not isinstance(min_samples, int) or isinstance(min_samples, bool) or min_samples < 1:
        raise ValueError("min_samples must be a positive integer")
    if _number(high_projection_threshold) is None or high_projection_threshold < 0:
        raise ValueError("high_projection_threshold must be finite and nonnegative")
    as_of = as_of or datetime.now(UTC)
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    as_of = as_of.astimezone(UTC)
    issues: list[str] = []

    def load(name, columns, *, optional=False):
        return _table(con, name, set(columns.split()), issues, optional=optional, season=season)

    history = load("espn_projection_history", "season espn_player_id scoring_period projected_points synced_at")
    actuals = load("espn_player_weekly_points", "season espn_player_id scoring_period actual_points synced_at")
    rosters = load("nfl_rosters_weekly", "season week gsis_id team position")
    schedules = load("nfl_schedules", "season week game_type game_id gameday gametime home_team away_team home_score away_score")
    leagues = load("leagues", "season league_id current_week")
    crosswalk = load("player_id_map", "espn_player_id gsis_id match_method match_confidence", optional=True)
    actual_evidence, provenance_audit = _archive_actuals(con, raw_archive_root, as_of)

    historical = defaultdict(list)
    by_gsis = defaultdict(list)
    for row in rosters:
        if "season_type" in row and row["season_type"] != "REG":
            continue
        base = row["season"], row["week"]
        by_gsis[(*base, row["gsis_id"])].append(row)
        try:
            historical[(*base, int(row.get("espn_id")))].append(row)
        except (TypeError, ValueError):
            pass
    mappings = defaultdict(set)
    for row in crosswalk:
        if (row["match_confidence"] == "high" and str(row["match_method"]).startswith("espn_id:")
                and row["gsis_id"] and "conflict" not in str(row.get("notes") or "").lower()):
            mappings[row["espn_player_id"]].add(row["gsis_id"])

    games = defaultdict(list)
    for row in schedules:
        if row["game_type"] == "REG":
            for team in (row["home_team"], row["away_team"]):
                games[(row["season"], row["week"], team)].append(row)
    league_context = defaultdict(list)
    for row in leagues:
        league_context[row["season"]].append(row)
    snapshots = defaultdict(list)
    scores = defaultdict(list)
    for row in history:
        snapshots[_key(row)].append(row)
    for row in actuals:
        scores[_key(row)].append(row)

    counts = Counter({name: 0 for name in (
        "history_snapshot_rows", "history_player_weeks", "candidate_player_weeks",
        "actual_nonnull_player_weeks", "actual_zero_player_weeks", "completed_actual_player_weeks",
        "pregame_snapshot_rows", "at_or_after_kickoff_snapshot_rows", "snapshot_rows_after_cutoff",
        "missing_snapshot_timestamp_rows", "missing_projection_snapshot_rows", "invalid_projection_snapshot_rows",
        "eligible_player_weeks", "eligible_distinct_players", "eligible_distinct_weeks", "eligible_distinct_games",
        "snapshot_rows_without_resolved_kickoff",
    )})
    counts.update(history_snapshot_rows=len(history), history_player_weeks=len(snapshots))
    # Structural quality is observable even if a player cannot be linked to a game.
    for row in history:
        captured = _utc(row["synced_at"])
        counts["missing_projection_snapshot_rows"] += row["projected_points"] is None
        counts["invalid_projection_snapshot_rows"] += row["projected_points"] is not None and _number(row["projected_points"]) is None
        counts["missing_snapshot_timestamp_rows"] += captured is None
        counts["snapshot_rows_after_cutoff"] += captured is not None and captured > as_of
    keys = sorted(scores.keys() | snapshots.keys())
    counts["candidate_player_weeks"] = len(keys)
    exclusions = Counter()
    observations: list[dict] = []
    horizon_rows = {h: {} for h in HORIZONS}

    for key in keys:
        year, pid, week = key
        actual_values = {_number(r["actual_points"]) for r in scores[key] if r["actual_points"] is not None}
        if actual_values - {None}:
            counts["actual_nonnull_player_weeks"] += 1
        if 0.0 in actual_values:
            counts["actual_zero_player_weeks"] += 1
        rows = historical[(year, week, pid)]
        if not rows and len(mappings[pid]) == 1:
            rows = by_gsis[(year, week, next(iter(mappings[pid])))]
            conflicting = False
            for row in rows:
                explicit_id = row.get("espn_id")
                if explicit_id is not None and explicit_id != "":
                    try:
                        conflicting |= int(explicit_id) != pid
                    except (TypeError, ValueError):
                        conflicting = True
            if conflicting:
                exclusions["conflicting_id_crosswalk"] += 1
                continue
        identities = {(r["gsis_id"], r["team"], r["position"]) for r in rows}
        if not identities or any(not team or not position for _, team, position in identities):
            exclusions["missing_historical_roster"] += 1
            continue
        if len(identities) != 1:
            exclusions["ambiguous_historical_roster"] += 1
            continue
        _, team, position = next(iter(identities))
        if position not in POSITIONS:
            exclusions["unsupported_position"] += 1
            continue
        candidates = games[(year, week, team)]
        if len(candidates) != 1:
            exclusions["missing_game" if not candidates else "ambiguous_game"] += 1
            continue
        game = candidates[0]
        kickoff = _kickoff(game)
        if kickoff is None:
            exclusions["missing_kickoff"] += 1
            continue

        valid = []
        for row in snapshots[key]:
            captured = _utc(row["synced_at"])
            points = _number(row["projected_points"])
            if captured is None or captured > as_of:
                continue
            if captured >= kickoff:
                counts["at_or_after_kickoff_snapshot_rows"] += 1
            else:
                counts["pregame_snapshot_rows"] += 1
                if points is not None:
                    valid.append((captured, points))

        if not actual_values:
            exclusions["missing_actual"] += 1
            continue
        if None in actual_values:
            exclusions["invalid_actual"] += 1
            continue
        if len(actual_values) != 1:
            exclusions["conflicting_actual_scores"] += 1
            continue
        if (any(_number(game[x]) is None for x in ("home_score", "away_score"))
                or as_of < kickoff + timedelta(hours=24)):
            exclusions["game_not_completed"] += 1
            continue
        context = league_context[year]
        if len(context) != 1 or context[0]["current_week"] is None:
            exclusions["missing_league_context" if not context else "ambiguous_league_context"] += 1
            continue
        if week >= context[0]["current_week"]:
            exclusions["scoring_period_not_closed"] += 1
            continue
        actual_times = [_utc(r["synced_at"]) for r in scores[key] if r["actual_points"] is not None]
        if any(t is None or t < kickoff + timedelta(hours=24) for t in actual_times):
            exclusions["actual_completion_unverified"] += 1
            continue
        if any(t > as_of for t in actual_times):
            exclusions["actual_unavailable_at_cutoff"] += 1
            continue
        if not any(value == next(iter(actual_values)) and capture >= kickoff + timedelta(hours=24)
                   and league_id == context[0]["league_id"]
                   for value, capture, league_id in actual_evidence[key]):
            exclusions["unverified_actual_provenance"] += 1
            continue
        counts["completed_actual_player_weeks"] += 1
        if not snapshots[key]:
            exclusions["missing_projection_history"] += 1
            continue
        if all(r["projected_points"] is None for r in snapshots[key]):
            exclusions["missing_projection"] += 1
            continue
        if all(_number(r["projected_points"]) is None for r in snapshots[key]):
            exclusions["invalid_projection"] += 1
            continue
        if not valid:
            exclusions["no_pregame_projection"] += 1
            continue
        by_time = defaultdict(set)
        for captured, points in valid:
            by_time[captured].add(points)
        if any(len(values) != 1 for values in by_time.values()):
            exclusions["conflicting_pregame_snapshots"] += 1
            continue
        base = {"key": key, "actual": next(iter(actual_values)), "position": position, "game": game["game_id"]}
        captured, projected = max(valid)
        observations.append({**base, "projected": projected})
        for horizon in HORIZONS:
            choices = [(t, p) for t, p in valid if _horizon((kickoff - t).total_seconds() / 3600) == horizon]
            if choices:
                horizon_rows[horizon][key] = {**base, "projected": max(choices)[1]}

    counts["eligible_player_weeks"] = len(observations)
    counts["eligible_distinct_players"] = len({(r["key"][0], r["key"][1]) for r in observations})
    counts["eligible_distinct_weeks"] = len({(r["key"][0], r["key"][2]) for r in observations})
    counts["eligible_distinct_games"] = len({(r["key"][0], r["game"]) for r in observations})
    counts["snapshot_rows_without_resolved_kickoff"] = (len(history) - counts["missing_snapshot_timestamp_rows"]
        - counts["snapshot_rows_after_cutoff"] - counts["pregame_snapshot_rows"] - counts["at_or_after_kickoff_snapshot_rows"])

    def bands(rows):
        return {name: _metrics([r for r in rows if (r["projected"] >= high_projection_threshold) == higher], min_samples)
                for name, higher in (("higher", True), ("lower", False))}

    paired = []
    for near, far in combinations(HORIZONS, 2):
        shared = sorted(horizon_rows[near].keys() & horizon_rows[far].keys())
        near_metrics = _metrics([horizon_rows[near][k] for k in shared], min_samples)
        far_metrics = _metrics([horizon_rows[far][k] for k in shared], min_samples)
        paired.append({"near": near, "far": far, "n": len(shared), "near_metrics": near_metrics,
                       "far_metrics": far_metrics,
                       "far_minus_near_mae": far_metrics["mae"] - near_metrics["mae"] if shared else None})
    return {
        "as_of": as_of.isoformat(), "season": season, "min_samples": min_samples,
        "high_projection_threshold": high_projection_threshold,
        "overall": _metrics(observations, min_samples),
        "by_position": {p: _metrics([r for r in observations if r["position"] == p], min_samples) for p in POSITIONS},
        "by_projected_points": bands(observations),
        "by_position_projected_points": {p: bands([r for r in observations if r["position"] == p]) for p in POSITIONS},
        "by_horizon": {h: _metrics(list(horizon_rows[h].values()), min_samples) for h in HORIZONS},
        "paired_horizon_comparisons": paired,
        "counts": dict(counts), "exclusions": dict(sorted(exclusions.items())),
        "schema_issues": issues, "cohort_note": COHORT_NOTE, "actual_provenance": provenance_audit,
        "warnings": [
            "Metrics are descriptive, not evidence of a reliable calibration adjustment. The sample floor is a reporting guard, not a statistical power calculation.",
            "Repeated players and shared games create correlated errors; counts are not independent trials. Horizon bins overlap and must not be summed. Use paired horizon counts for comparisons.",
            "Daily history keeps the latest sync only; overwritten pregame snapshots cannot be recovered. synced_at is capture time, not ESPN's forecast publication time.",
            "Completion additionally requires a matching actual value in a successful archived ESPN sync whose captured league payload already closed that week. A projection-only row timestamp refresh cannot verify a final actual. Missing archives exclude observations; later stat corrections may change results.",
            "Historical team/position comes only from that week's NFL roster via an ID crosswalk. Current teams, names and calendar-only kickoff guesses are never used.",
            "as_of filters stored timestamps and completion boundaries; this is not a historical database replay. Multi-season aggregate errors can mix scoring settings; prefer a single season.",
        ],
    }
