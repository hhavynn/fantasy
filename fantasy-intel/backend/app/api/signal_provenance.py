"""Signal provenance: where each signal behind a page's headline recommendation comes from, how old it is and how
complete (issue #168). One small contract, ``signal-provenance.v1``, that a response carries additively as
``provenance``; its TypeScript twin is ``frontend/lib/signal-provenance.ts`` (keep the two in step).

    provenance = {"contract": "signal-provenance.v1", "signals": [Signal, ...]}

    Signal (every key always present):
      signal              stable key: "league" | "projections" | "injuries" | "usage" | "analysis"
      label               what the signal is, in plain words
      source              who produced it: "espn" | "sleeper" | "nflverse" | "fantasy_intel"; null when unknown
      captured_at         ISO-8601 UTC ("...Z") when the source data was captured: a successful sync's finish, a model
                          run's creation, an analysis' computation. null when unknown, never the request time.
      snapshot_version    opaque id of the immutable data the answer was read from (the league or global file version
                          of a league-scoped request; the published stamp or local file version in legacy mode); null
                          when unknown. Compare for equality only. Never a league id, a path or a secret.
      coverage            {"available": n, "total": m, "label": "starters with a projection"} or null (not measured).
                          A missing value counts as missing, never as 0.
      reason_unavailable  why the signal is missing or incomplete; null when complete
      stale_reason        set when the data is known to be superseded whatever its age (the source's newest sync
                          failed, the roster was re-synced after the analysis ran); null otherwise
      stale_after_hours   the age past which the signal must be called stale

The status is derived by the reader when it shows the signal (``status()`` here, ``provenanceStatus()`` in the
frontend), never stored in the response: league-scoped responses are cached per data version, so a stored "current"
would outlive its truth. Precedence, first match wins:

    unavailable  reason_unavailable and nothing covered (no coverage measured, or coverage.available == 0 of > 0)
    unknown      no captured_at: the age is unknown
    stale        stale_reason, or older than stale_after_hours
    partial      coverage.available < coverage.total, or reason_unavailable with something covered
    current      none of the above

Everything here reads data the handler already loaded or cheap metadata (one sync_runs aggregate, the scope's
versions); no dashboard joins. The research pipeline's forecast-snapshot provenance is a different concern
(frozen forecast snapshots).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Iterable

from ..config import get_settings
from ..db.connection import table_exists
from .deps import LEAGUE_SCOPE, rows

CONTRACT = "signal-provenance.v1"
STALE_AFTER_HOURS = 36          # frontend/lib/sync-status.ts STALE_HOURS: the same line as the header's sync pill
STATUSES = ("current", "partial", "stale", "unknown", "unavailable")
_NAMES = {"espn": "ESPN", "sleeper": "Sleeper", "nfl": "NFL data (nflverse)", "analytics": "analysis"}

# A 'running' row with no finish time is left behind by a sync that died (SyncRun.start writes it, finish() replaces
# it); a live sync holds the DuckDB lock, so the API cannot even read during one. Report it as 'interrupted', and keep
# it out of the "latest run" per source so it neither shows as running nor as failed.
INTERRUPTED = "(status = 'running' AND finished_at IS NULL)"


def sync_status(con) -> dict:
    """{last_success: {source: finished_at}, latest_runs: [{source, status, message, finished_at}]} from sync_runs."""
    if not table_exists(con, "sync_runs"):
        return {"last_success": {}, "latest_runs": []}
    out = {}
    for r in rows(con, """
        SELECT source, max(finished_at) AS last_ok FROM sync_runs WHERE status = 'ok' GROUP BY 1"""):
        out[r["source"]] = r["last_ok"]
    last = rows(con, f"""
        SELECT source, status, message, finished_at FROM (
          SELECT *, row_number() OVER (PARTITION BY source ORDER BY started_at DESC) rn FROM sync_runs
          WHERE NOT {INTERRUPTED}) WHERE rn = 1""")
    return {"last_success": out, "latest_runs": last}


def iso_utc(value: Any) -> str | None:
    """A timestamp as ISO-8601 UTC with "Z"; naive values are UTC (as the syncs write them). Empty or unparseable
    values are None: an unknown time stays unknown."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip()
        try:
            dt = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def coverage(available: int | None, total: int | None, label: str) -> dict | None:
    """{"available", "total", "label"}, or None when either count is unknown (not measured is not zero)."""
    if available is None or total is None:
        return None
    total = max(0, int(total))
    return {"available": max(0, min(int(available), total)), "total": total, "label": label}


def signal(key: str, label: str, source: str | None, captured_at: Any, snapshot_version: str | None, *,
           coverage: dict | None = None, reason_unavailable: str | None = None, stale_reason: str | None = None,
           stale_after_hours: int = STALE_AFTER_HOURS) -> dict:
    return {"signal": key, "label": label, "source": source, "captured_at": iso_utc(captured_at),
            "snapshot_version": snapshot_version, "coverage": coverage, "reason_unavailable": reason_unavailable or None,
            "stale_reason": stale_reason or None, "stale_after_hours": int(stale_after_hours)}


def payload(*signals: dict | None) -> dict:
    return {"contract": CONTRACT, "signals": [s for s in signals if s is not None]}


def status(sig: dict, now: datetime | None = None) -> str:
    """The derived status of one signal at `now` (see the module docstring)."""
    cov = sig.get("coverage")
    covered = cov["available"] if cov and cov.get("total") else None
    if sig.get("reason_unavailable") and not covered:
        return "unavailable"
    if cov and cov.get("total") and cov["available"] == 0:
        return "unavailable"
    captured = iso_utc(sig.get("captured_at"))
    if captured is None:
        return "unknown"
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_hours = (now - datetime.fromisoformat(captured.replace("Z", "+00:00"))).total_seconds() / 3600
    if sig.get("stale_reason") or age_hours > sig.get("stale_after_hours", STALE_AFTER_HOURS):
        return "stale"
    if (cov and cov["available"] < cov["total"]) or sig.get("reason_unavailable"):
        return "partial"
    return "current"


def snapshot_versions(con) -> tuple[str | None, str | None]:
    """(league data version, global data version) of what this request reads. A league-scoped request names its
    league and global file versions (the same values as the X-FI-Data-Version / X-FI-Global-Version headers); the
    legacy single-league API names its publish stamp, or the local file's version (never its path)."""
    scope = LEAGUE_SCOPE.get()
    if scope is not None:
        return scope.data_version, scope.global_version
    settings = get_settings()
    if settings.snapshot_enabled or settings.motherduck_database:
        try:
            row = con.execute("SELECT value FROM schema_meta WHERE key = 'published_at'").fetchone()
        except Exception:   # noqa: BLE001 - an old copy without schema_meta: the version is unknown
            row = None
        stamp = f"published:{row[0]}" if row and row[0] else None
        return stamp, stamp
    try:
        stamp = f"local:{settings.db_path.stat().st_mtime_ns}"
    except OSError:
        stamp = None
    return stamp, stamp


def league_provider(con) -> str:
    """The league's provider key ("espn" or "sleeper"): the scope's, else the synced league settings'."""
    scope = LEAGUE_SCOPE.get()
    if scope is not None:
        return scope.provider or "espn"
    from ..analytics.projection_source import default_source
    return "sleeper" if default_source(con) == "independent" else "espn"


class Provenance:
    """The per-request facts every signal of one response shares: sync runs and snapshot versions, read once."""

    def __init__(self, con, *, sync: dict | None = None, provider: str | None = None):
        self.sync = sync if sync is not None else sync_status(con)
        self.league_version, self.global_version = snapshot_versions(con)
        self.provider = provider or league_provider(con)

    def last_ok(self, source: str):
        return (self.sync.get("last_success") or {}).get(source)

    def failed_since_ok(self, source: str) -> str | None:
        """A stale_reason when the source's newest run did not succeed after its last success (a conflict: the data
        shown is older than the newest attempt to refresh it)."""
        latest = next((r for r in self.sync.get("latest_runs") or [] if r.get("source") == source), None)
        if latest is None or str(latest.get("status") or "").lower() in ("ok", "success"):
            return None
        failed, ok = iso_utc(latest.get("finished_at")), iso_utc(self.last_ok(source))
        if ok is not None and failed is not None and ok > failed:
            return None
        name = _NAMES.get(source, source)
        return (f"The newest {name} sync did not succeed; this is the last successful one." if ok else
                f"The {name} sync has not succeeded yet.")

    def league(self, lg: dict | None = None) -> dict:
        name = _NAMES.get(self.provider, self.provider)
        captured = self.last_ok(self.provider) or (lg or {}).get("synced_at")
        return signal("league", f"{name} rosters, lineups and results", self.provider, captured, self.league_version,
                      stale_reason=self.failed_since_ok(self.provider))

    def projections(self, source: str | None, detail: dict | None = None, *, captured_at: Any = None,
                    cov: dict | None = None, horizon: str = "weekly") -> dict:
        """The projections a page shows. `source` is the backend's ("espn" | "independent" | None = the league's
        default); `detail` the response's projection_source block. A requested independent run that is not
        available falls back to ESPN's numbers where the provider publishes them (the page shows those), and is
        unavailable otherwise."""
        ours = source == "independent" or (source is None and self.provider != "espn")
        if ours and detail is not None and detail.get("available"):
            return self._ours(detail, captured_at, cov, horizon)
        if ours and self.provider != "espn":
            reason = (detail or {}).get("reason") or "No Fantasy Intel projection run is stored for this week yet."
            return signal("projections", f"Fantasy Intel {horizon} projections (Ours)", "fantasy_intel", None,
                          self.league_version, coverage=cov, reason_unavailable=reason)
        missing = None if cov is None or cov["available"] >= cov["total"] else \
            f"{cov['total'] - cov['available']} of {cov['total']} have no projection; they count as missing, not 0."
        return signal("projections", f"ESPN {horizon} projections", "espn", captured_at or self.last_ok("espn"),
                      self.league_version, coverage=cov, reason_unavailable=missing,
                      stale_reason=self.failed_since_ok("espn"))

    def _ours(self, detail: dict, captured_at: Any, cov: dict | None, horizon: str) -> dict:
        """An Ours signal can be mixed: players the run abstains on keep ESPN's numbers, and kickers and D/STs come from
        their own runs. The label says so, and the age is the OLDEST of the runs shown, so a mix never looks newer
        than its oldest part."""
        runs = [detail.get("run") or {}, detail.get("kicker_run") or {}, detail.get("dst_run") or {}]
        times = [t for t in (iso_utc(r.get("created_at")) for r in runs if r) if t is not None]
        main = iso_utc((detail.get("run") or {}).get("created_at"))
        when = min(times) if times and main is not None else (main or captured_at)
        parts = []
        espn_n = int(((detail.get("counts") or {}).get("espn")) or 0)
        if espn_n and self.provider == "espn":
            parts.append(f"ESPN's numbers for {espn_n} {'player' if espn_n == 1 else 'players'} Ours does not cover")
        if detail.get("kicker_run") or detail.get("dst_run"):
            own = " and ".join(k for k, key in (("kickers", "kicker_run"), ("D/STs", "dst_run")) if detail.get(key))
            parts.append(f"{own} from their own runs")
        label = f"Fantasy Intel {horizon} projections (Ours)" + (f", with {'; '.join(parts)}" if parts else "")
        return signal("projections", label, "fantasy_intel", when, self.league_version, coverage=cov)

    def nflverse(self, key: str, label: str, *, cov: dict | None = None, reason: str | None = None) -> dict:
        return signal(key, label, "nflverse", self.last_ok("nfl"), self.global_version, coverage=cov,
                      reason_unavailable=reason, stale_reason=self.failed_since_ok("nfl"))

    def injuries(self, con, season: int | None, *, cov: dict | None = None) -> dict:
        reason = None
        if not table_exists(con, "nfl_injuries"):
            reason = "No NFL injury or practice reports are stored."
        elif season is not None and not con.execute("SELECT count(*) FROM nfl_injuries WHERE season = ?",
                                                     [season]).fetchone()[0]:
            reason = f"No NFL injury or practice reports are stored for {season} yet."
        return self.nflverse("injuries", "NFL injury and practice reports (nflverse)", cov=cov, reason=reason)

    def analysis(self, label: str, computed_at: Any, *, cov: dict | None = None, stale_reason: str | None = None,
                 reason: str | None = None) -> dict:
        return signal("analysis", label, "fantasy_intel", computed_at, self.league_version, coverage=cov,
                      reason_unavailable=reason,
                      stale_reason=stale_reason or self.failed_since_ok("analytics"))


def projected_coverage(players: Iterable[dict], value_key: str, label: str = "starters with a projection") -> dict | None:
    """How many of `players` (a lineup) have a projection under `value_key`; players on bye are not expected to have
    one, and empty slots are not players. None when the lineup is unknown."""
    expected = [p for p in players if p.get("player_id") is not None and p.get("on_bye") is not True]
    if not expected:
        return None
    return coverage(sum(1 for p in expected if p.get(value_key) is not None), len(expected), label)


def merge_coverage(*parts: dict | None) -> dict | None:
    known = [p for p in parts if p]
    if not known:
        return None
    return coverage(sum(p["available"] for p in known), sum(p["total"] for p in known), known[0]["label"])


def trade_provenance(con, lg: dict, state, start_week: int | None = None) -> dict:
    """League data, the rest-of-season projections the trade engines read (the loaded league state's own, so no
    second read) and NFL availability. Coverage: active rostered players with a projection for any evaluated week.
    The served Ours run's creation time is not kept by the league state, so for Ours it stays unknown rather than
    guessed."""
    prov = Provenance(con)
    weeks = list(state.window(start_week).weeks)
    active = [p for ps in state.rosters.values() for p in ps if not p.on_ir]
    cov = coverage(sum(1 for p in active if any((state.projections.get(p.espn_player_id) or {}).get(w) is not None
                                                for w in weeks)), len(active), "rostered players with a projection") \
        if weeks and active else None
    detail = {"available": bool(state.projections), "run": None,
              "reason": None if state.projections else "No projections are stored for the remaining weeks."} \
        if state.projection_source == "independent" else None
    proj = prov.projections(state.projection_source, detail, cov=cov, horizon="rest-of-season")
    return payload(prov.league(lg), proj, prov.injuries(con, state.season))
