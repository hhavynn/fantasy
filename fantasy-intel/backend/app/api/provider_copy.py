"""What a Sleeper league's responses say instead of ESPN's wording (multi-league Phase 4b).

Engines and their notes were written for ESPN leagues ("Actual records are ESPN's", "ESPN projection is the forward
baseline"). For a Sleeper league those words are wrong in two different ways, so the rules below keep them apart:

  * projections: Sleeper publishes none; a Sleeper league's numbers are Fantasy Intel's own (Ours). A projection is
    never called Sleeper's;
  * provider facts (records, results, transactions, injury designations, the player pool): Sleeper's.

`rewrite` applies the rules to every string of a JSON payload, most specific first; the last rules replace any
remaining ESPN with Sleeper. ESPN leagues are never rewritten. The owner-only research endpoints (which compare Ours
with ESPN's own projections and say "not applicable" for a league without them) are left as they are.
tests/test_provider_copy.py runs every route against the Sleeper fixture: no ESPN survives and no projection is
attributed to Sleeper.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

OURS = "Fantasy Intel's own projections (Ours)"

RULES: list[tuple[re.Pattern, str]] = [(re.compile(p), r) for p, r in (
    # ---- rankings past the regular season (analytics/rankings.py): nothing ranks, a provider never does
    (r"so ESPN ranks the full window \(week (\d+) to (\d+)\) and Ours is unavailable",
     r"so nothing is ranked for weeks \1 to \2: Ours covers regular-season weeks only and this league's provider "
     r"publishes no projections"),
    (r"so ESPN ranks the remaining weeks \((\d+) to (\d+)\) and Ours is unavailable",
     r"so nothing is ranked for weeks \1 to \2: Ours covers regular-season weeks only and this league's provider "
     r"publishes no projections"),
    (r"; ranked on ESPN\.", "; nothing is ranked (this league's provider publishes no projections)."),
    # ---- projections: Ours
    (r"espn\.projected_points is ESPN's number as synced; it is shown for comparison only\.",
     "espn.projected_points is empty: this league's provider publishes no projections."),
    (r"it never uses ESPN projections", "it never uses provider projections"),
    (r"Compare ESPN with expected_points \(comparison\.expected_minus_espn\)",
     "There is no provider projection to compare with expected_points (comparison.expected_minus_espn)"),
    (r"ESPN projection is the forward fantasy-point baseline",
     "Fantasy Intel's own projection (Ours) is the forward fantasy-point baseline"),
    (r"ESPN(?:'s)? weekly projections, unmodified(?: \([^)]*\))?", OURS + ", in this league's scoring"),
    (r"ESPN league points and projections use this league's scoring",
     "Sleeper's league points and Ours projections use this league's scoring"),
    (r"ESPN's weekly D/ST and K projections", "the weekly D/ST and K projections"),
    (r"ESPN's projections", OURS),
    (r"ESPN's projection", "the Ours projection"),
    (r"ESPN's weekly projections", OURS),
    (r"ESPN weekly projections", "Ours weekly projections"),
    (r"ESPN weekly projection", "Ours weekly projection"),
    (r"ESPN weekly lineups", "Ours weekly lineups"),
    (r"ESPN-projected", "Ours-projected"),
    (r"ESPN projected_points", "Ours projected points"),
    (r"ESPN projections can be wrong", "Projections can be wrong"),
    (r"ESPN points above", "projected points above"),
    (r"ESPN projections for", "Ours projections for"),
    (r"no ESPN projection", "no Ours projection"),
    (r"ESPN has no projection", "Ours has no projection"),
    (r"ESPN rest-of-season", "Ours rest-of-season"),
    (r"ESPN projection", "Ours projection"),
    (r"projection \(ESPN\)", "projection (Ours)"),               # row labels: "Next week projection (ESPN)"
    (r"^ESPN$", "Ours"),                                        # a projection source label
    # ---- provider facts: Sleeper
    (r"no ESPN winner yet", "no Sleeper result yet"),
    (r"ESPN's winner field", "Sleeper's matchup results"),
    (r"ESPN winner", "Sleeper result"),
    (r"ESPN broke with a league tiebreak counts as ESPN records it", "Sleeper broke with a tiebreak counts as Sleeper records it"),
    (r"ESPN acquisition/drop counters", "Sleeper's transaction counts"),
    (r"ESPN move-to-IR counter", "move-to-IR count (Sleeper does not report one)"),
    (r"ESPN undroppable-list restrictions are not modeled\.", "Sleeper has no undroppable list."),
    (r"the usual ESPN head-to-head", "the usual head-to-head"),
    (r"ESPN current_week", "Sleeper's current week"),
    (r"the stored ESPN pool", "the stored Sleeper player pool"),
    (r"ESPN's matchup period schedule is not in the synced settings",
     "Sleeper's matchup schedule is one matchup per week"),
    # ---- anything left names the provider
    (r"ESPN's", "Sleeper's"),
    (r"ESPN", "Sleeper"),
)]
# owner-only research endpoints: they compare Ours with ESPN's own projections (the page says "not applicable")
UNTOUCHED = ("/api/projections/independent/promotion", "/api/projection-performance", "/api/research")


def text(s: str) -> str:
    if "ESPN" not in s:
        return s
    for pattern, repl in RULES:
        s = pattern.sub(repl, s)
        if "ESPN" not in s:
            break
    return s


# Keys whose values are data (names people and providers chose), never the site's wording.
DATA_KEYS = frozenset({"name", "full_name", "team_name", "owner_names", "display_name", "manager", "manager_name",
                       "player_name", "abbrev", "league_name", "nickname", "first_name", "last_name", "team"})


def rewrite(obj: Any, key: str | None = None) -> Any:
    if isinstance(obj, str):
        return obj if key in DATA_KEYS or (key or "").endswith("_name") else text(obj)
    if isinstance(obj, list):
        return [rewrite(v, key) for v in obj]
    if isinstance(obj, dict):
        return {k: rewrite(v, k) for k, v in obj.items()}
    return obj


def applies(path: str) -> bool:
    """`path` is the API path inside the league (`/api/...`)."""
    return path.startswith("/api/") and not path.startswith(UNTOUCHED)


def dumps(obj: Any) -> bytes:
    """As FastAPI's JSONResponse renders a body."""
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":")).encode("utf-8")


_CLASSIC: dict[tuple[str, float], str] = {}
_CLASSIC_LOCK = threading.Lock()


def classic_provider(db_path: Path) -> str:
    """The provider of the database the classic (unscoped) routes serve: its newest league's settings_raw.provider,
    read once per file version."""
    try:
        key = (str(db_path), Path(db_path).stat().st_mtime)
    except OSError:
        return "espn"
    with _CLASSIC_LOCK:
        if key in _CLASSIC:
            return _CLASSIC[key]
    provider = "espn"
    try:
        import duckdb

        from ..analytics.legacy_league import latest_league
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            row = latest_league(con, ["settings_raw"])
        finally:
            con.close()
        value = row["settings_raw"] if row else None
        raw = json.loads(value) if isinstance(value, str) else value
        if isinstance(raw, dict) and raw.get("provider"):
            provider = str(raw["provider"])
    except Exception:          # noqa: BLE001 - an unreadable or locked file keeps ESPN's wording
        pass
    with _CLASSIC_LOCK:
        _CLASSIC[key] = provider
    return provider


async def classic_copy(request, call_next):
    """The classic routes (one league per deployment, FANTASY_DB_PATH): a Sleeper database's answers get Sleeper's
    wording. /v2 routes are reworded in app/api/league_scope.add_scope_headers."""
    response = await call_next(request)
    path = request.url.path
    if (path.startswith("/v2") or not applies(path) or response.status_code != 200
            or not (response.media_type or response.headers.get("content-type", "")).startswith("application/json")):
        return response
    from ..config import get_settings
    settings = get_settings()
    if getattr(settings, "motherduck_database", None) or classic_provider(settings.db_path) == "espn":
        return response
    from fastapi import Response
    body = b"".join([chunk async for chunk in response.body_iterator])
    body = dumps(rewrite(json.loads(body)))
    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    return Response(content=body, status_code=200, headers=headers, media_type=response.media_type)
