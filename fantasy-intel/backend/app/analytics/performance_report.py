"""Published projection-performance report: the hosted reader (SQL and the standard library only).

The scorecard behind the Projection performance page is computed OFFLINE by scripts/projection_performance.py,
which reuses the research-only prospective ESPN comparison and the recorded Start/Sit decision sets (polars and
numpy, which the hosted API bundle does not ship). It stores the finished report as ONE row of
`projection_performance_reports`: the canonical JSON text and the sha256 of exactly those bytes, inserted in a
single statement (never updated or deleted). `scripts/publish_motherduck.py` copies every table in one transaction,
so the hosted copy holds either the previous report or the new one, never part of one.

Nothing here computes a statistic. `latest` picks the newest row of a season that verifies (hash, schema, season,
structure, not synthetic) and says why any newer row was rejected; `staleness` says when the report may no longer
reflect the data (a sync finished after it was built, or it is older than STALE_AFTER). The page shows the numbers
the offline build computed, unchanged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

from ..db.connection import fetch_dicts, table_exists

TABLE = "projection_performance_reports"
SCHEMA_VERSION = "projection-performance/v1"
API_SCHEMA_VERSION = "projection-performance-api/v1"
POSITIONS = ("QB", "RB", "WR", "TE")
STALE_AFTER = timedelta(days=8)          # the scheduled refresh runs Thu, Sun, Mon and Tue
DATA_SOURCES = ("espn", "nfl")           # sync_runs sources whose newer data makes a report stale

# Per-position accuracy verdicts of the offline comparison and the one sentence the page may say about each.
VERDICTS = ("insufficient_evidence", "no_significant_difference", "ours_better", "espn_better")
STATEMENTS = {
    "insufficient_evidence": "Insufficient prospective evidence.",
    "no_significant_difference": "No statistically established difference.",
    "ours_better": "Fantasy Intel was more accurate than ESPN.",
    "espn_better": "ESPN was more accurate than Fantasy Intel.",
}
# Start/Sit decision verdicts (the offline decision evaluation: model minus ESPN, direction already applied).
DECISION_VERDICTS = ("insufficient_evidence", "no_significant_difference", "better", "worse")
DECISION_STATEMENTS = {
    "insufficient_evidence": "Insufficient prospective evidence.",
    "no_significant_difference": "No statistically established difference.",
    "better": "Fantasy Intel made better decisions than ESPN.",
    "worse": "ESPN made better decisions than Fantasy Intel.",
}

REQUIRED_KEYS = ("schema_version", "synthetic", "season", "generated_at", "data_as_of", "production", "evidence_gate",
                 "weeks", "benchmark", "status", "positions", "comparisons", "weekly", "preliminary", "model_versions",
                 "start_sit", "coverage", "notes")
# Never in a published report: identifiers of players, rosters, leagues or fantasy teams.
FORBIDDEN_KEYS = frozenset({"espn_player_id", "gsis_id", "player_id", "player_name", "full_name", "league_id",
                            "fantasy_team_id", "team_id", "owner_ids", "pair"})


def canonical(payload: dict) -> str:
    """The exact text stored and hashed: sorted keys, compact separators, no NaN or infinity."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _forbidden(obj, path: str = "") -> str | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in FORBIDDEN_KEYS:
                return f"{path}{k}"
            hit = _forbidden(v, f"{path}{k}.")
            if hit:
                return hit
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hit = _forbidden(v, f"{path}{i}.")
            if hit:
                return hit
    return None


def validate(payload, *, season: int | None = None, allow_synthetic: bool = False) -> str | None:
    """Why `payload` must not be served, or None. Checks structure and the claims the page relies on, never
    recomputes a statistic. A malformed payload is rejected, never an error."""
    try:
        return _validate(payload, season, allow_synthetic)
    except (TypeError, AttributeError, KeyError, IndexError) as err:
        return f"the report is malformed ({type(err).__name__})"


def _validate(payload, season: int | None, allow_synthetic: bool) -> str | None:
    if not isinstance(payload, dict):
        return "the report is not a JSON object"
    missing = [k for k in REQUIRED_KEYS if k not in payload]
    if missing:
        return f"the report lacks {', '.join(missing)}"
    if payload["schema_version"] != SCHEMA_VERSION:
        return f"unknown report schema {payload['schema_version']!r} (this API reads {SCHEMA_VERSION})"
    if season is not None and payload["season"] != season:
        return f"the report is for season {payload['season']}, not {season}"
    if payload["synthetic"] is not False and not allow_synthetic:
        return "synthetic test data is never served as real results"
    positions = payload["positions"]
    if not isinstance(positions, dict) or set(positions) != set(POSITIONS):
        return "the report must carry exactly the positions QB, RB, WR and TE"
    for pos, p in positions.items():
        problem = _verdict_problem(pos, p)
        if problem:
            return problem
    split = payload.get("rb_volume_split")          # optional: reports built before the split stay valid
    if split is not None:
        for name, comp in split["comparisons"].items():
            for group, g in comp["by_group"].items():
                problem = _verdict_problem(f"RB {group} ({name})", g)
                if problem:
                    return problem
    hit = _forbidden(payload)
    if hit:
        return f"the report contains an identifier field ({hit}); only aggregates may be published"
    return None


def _verdict_problem(label: str, p: dict) -> str | None:
    if p.get("verdict") not in VERDICTS:
        return f"{label}: unknown verdict {p.get('verdict')!r}"
    if p.get("statement") != STATEMENTS[p["verdict"]]:
        return f"{label}: the statement does not match the verdict"
    ci = p.get("ci95")
    if p["verdict"] in ("ours_better", "espn_better"):
        # a directional claim needs an interval on one side of zero (the offline build decided on the unrounded
        # bounds, so a displayed bound may round to 0.0)
        if not (isinstance(ci, list) and len(ci) == 2 and (ci[1] <= 0 if p["verdict"] == "ours_better" else ci[0] >= 0)):
            return f"{label}: a {p['verdict']} verdict without a 95% interval that excludes 0"
    return None


def _naive_utc(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(v, datetime):
        return None
    return v.astimezone(timezone.utc).replace(tzinfo=None) if v.tzinfo else v


def latest_sync_times(con) -> dict[str, str | None]:
    """{source: latest finished_at of an ok sync, ISO naive UTC} for DATA_SOURCES (the build records these as
    `data_as_of`; the reader compares them with the database it serves)."""
    out: dict[str, str | None] = dict.fromkeys(DATA_SOURCES)
    if not table_exists(con, "sync_runs"):
        return out
    for r in fetch_dicts(con, "SELECT source, max(finished_at) AS t FROM sync_runs WHERE status = 'ok' "
                              "AND source IN (SELECT unnest(?)) GROUP BY source", [list(DATA_SOURCES)]):
        t = _naive_utc(r["t"])
        out[r["source"]] = None if t is None else t.isoformat(timespec="seconds")
    return out


def staleness(payload: dict, current_syncs: dict[str, str | None], now: datetime) -> list[str]:
    """Reasons the report may not reflect the served data (empty: current)."""
    reasons = []
    built = _naive_utc(payload.get("generated_at"))
    now = _naive_utc(now)
    if built is None:
        reasons.append("the report has no build time")
    elif now - built > STALE_AFTER:
        reasons.append(f"built {int((now - built).total_seconds() // 86400)} days ago (the scheduled refresh rebuilds "
                       f"it several times a week)")
    seen = payload.get("data_as_of") or {}
    for src in DATA_SOURCES:
        cur, then = _naive_utc(current_syncs.get(src)), _naive_utc(seen.get(src))
        if cur is not None and (then is None or cur > then):
            reasons.append(f"an {'NFL' if src == 'nfl' else 'ESPN'} sync finished after the report was built")
    return reasons


def store_report(con, payload: dict) -> tuple[str, bool]:
    """Insert the report once (offline build only). Returns (report_sha256, created). Refuses a payload that would
    not verify, so a bad build can never become the newest row."""
    problem = validate(payload, allow_synthetic=True)
    if problem:
        raise ValueError(f"refusing to store the report: {problem}")
    text = canonical(payload)
    sha = sha256_text(text)
    before = con.execute(f"SELECT count(*) FROM {TABLE} WHERE report_sha256 = ?", [sha]).fetchone()[0]
    con.execute(f"INSERT INTO {TABLE} (report_sha256, season, schema_version, generated_at, payload) "
                f"VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                [sha, payload["season"], payload["schema_version"], _naive_utc(payload["generated_at"]), text])
    return sha, before == 0


def latest(con, season: int, *, allow_synthetic: bool = False) -> tuple[dict | None, str | None, list[dict]]:
    """(payload, report_sha256, rejected newer rows [{report_sha256, generated_at, reason}]) for the newest row of
    the season that verifies; (None, None, rejected) when none does."""
    if not table_exists(con, TABLE):
        return None, None, []
    rejected: list[dict] = []
    for r in fetch_dicts(con, f"SELECT report_sha256, schema_version, generated_at, payload FROM {TABLE} "
                              "WHERE season = ? ORDER BY generated_at DESC, report_sha256", [season]):
        text = r["payload"]
        when = None if r["generated_at"] is None else str(r["generated_at"])
        if not isinstance(text, str) or sha256_text(text) != r["report_sha256"]:
            rejected.append({"report_sha256": r["report_sha256"], "generated_at": when,
                             "reason": "content hash mismatch: the stored report was altered or damaged"})
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            rejected.append({"report_sha256": r["report_sha256"], "generated_at": when, "reason": "not valid JSON"})
            continue
        problem = validate(payload, season=season, allow_synthetic=allow_synthetic)
        if problem is None and r["schema_version"] != payload["schema_version"]:
            problem = "the stored schema column disagrees with the report"
        if problem:
            rejected.append({"report_sha256": r["report_sha256"], "generated_at": when, "reason": problem})
            continue
        return payload, r["report_sha256"], rejected
    return None, None, rejected


def response(con, season: int, *, now: datetime | None = None, allow_synthetic: bool = False) -> dict:
    """The API body: the verified report (unchanged) with its integrity and freshness status, plus the production
    governance so the page never implies validation even without a report."""
    from .projection_source import MODEL_VERSION, governance

    payload, sha, rejected = latest(con, season, allow_synthetic=allow_synthetic)
    base = {"schema_version": API_SCHEMA_VERSION, "season": season, "governance": governance(MODEL_VERSION),
            "rejected": rejected, "report_sha256": sha, "report": payload, "stale": False, "stale_reasons": []}
    if payload is None:
        reason = ("No projection-performance report has been published for this season yet. It is built offline "
                  "by the scheduled refresh (scripts/projection_performance.py) and published with the database.")
        if rejected:
            reason = (f"No published projection-performance report for this season passed the integrity checks "
                      f"({len(rejected)} rejected), so no figures are shown.")
        return {**base, "available": False, "status": "integrity_failed" if rejected else "no_report",
                "unavailable_reason": reason}
    reasons = staleness(payload, latest_sync_times(con), now or datetime.now(timezone.utc))
    return {**base, "available": True, "status": "stale" if reasons else "ok", "unavailable_reason": None,
            "stale": bool(reasons), "stale_reasons": reasons}
