"""Ratchet: the single-league "most recently synced league, globally" selectors in app/ must not grow.

The legacy analytics/API code picks its league with `SELECT ... FROM leagues ORDER BY synced_at DESC LIMIT 1`:
whichever league synced last, for every caller. That is only safe while one database holds one league for one
owner (docs/multileague/ADR-0001-league-context-and-tenancy.md, "Audit"). This test pins the exact set of
files that still contain such a selector. It fails when

  * a NEW selector is added (the count for a file goes up, or a new file appears)  -> use a LeagueContext, or
  * a selector is migrated away (the count goes down)                               -> lower the pin below.

It is deliberately a source scan, not a behavioural test: a global selector is correct-looking code, which is
exactly why it must be caught by a tripwire.
"""

import re
from collections import Counter
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
LEAGUES_PKG = APP / "leagues"
CONTROL_PLANE_PKG = APP / "control_plane"

# `ORDER BY synced_at DESC` with an optional table alias, any whitespace (SQL is often wrapped over lines).
_GLOBAL_LATEST = re.compile(r"ORDER\s+BY\s+(?:\w+\.)?synced_at\s+DESC", re.IGNORECASE)
# Two adjacent Python string literals / a `+` concatenation across lines: "... ORDER BY " "synced_at DESC".
_LITERAL_JOIN = re.compile(r"[\"']\s*(?:\+\s*)?\n\s*[rfbRFB]*[\"']")

# (path relative to app/, number of `ORDER BY synced_at DESC` sites). 20 audited sites in 17 files, plus the shared helper.
# NOTE for the architect: ADR-0001 says "21 sites"; its own bullet list sums to 20 and so does the source.
# independent_projection_store.py has no LIMIT 1 but is a global selector all the same (it takes the first row
# of `... WHERE season = ? ORDER BY synced_at DESC` to read the league's scoring rules).
PINNED_GLOBAL_SELECTORS = {
    "analytics/independent_projection_store.py": 1,
    # The shared helper: single-league features added after the audit call latest_league() instead of adding
    # their own selector, so this one site is where Phase 2b swaps in a LeagueContext lookup for all of them.
    "analytics/legacy_league.py": 1,
    "analytics/luck_lab.py": 1,
    "analytics/manager_loader.py": 1,
    "analytics/matchup.py": 1,
    "analytics/pipeline.py": 1,
    "analytics/projection_source.py": 1,
    "analytics/projections.py": 1,
    "analytics/promotion_tracker.py": 1,
    "analytics/research.py": 2,
    "analytics/schedule_intelligence.py": 1,
    "analytics/start_sit.py": 1,
    "analytics/trade_data.py": 1,
    "analytics/waivers.py": 1,
    "analytics/weekly_recap.py": 1,
    "api/independent_projections.py": 1,
    "api/routes.py": 3,
    "api/trade_discovery.py": 1,
}

HOW_TO_FIX = (
    "Do not add a global 'latest league' selector. League-specific code must take a LeagueContext "
    "(app/leagues/context.py, issued by TenancyStore.authorize) and filter by its league_id; see "
    "docs/multileague/ADR-0001-league-context-and-tenancy.md. A single-league owner feature that cannot take one "
    "yet calls app.analytics.legacy_league.latest_league() instead of its own selector. If you MIGRATED a legacy site off "
    "`ORDER BY synced_at DESC`, lower its count in PINNED_GLOBAL_SELECTORS (the ratchet only goes down)."
)


def _source(path: Path) -> str:
    return _LITERAL_JOIN.sub(" ", path.read_text(encoding="utf-8"))


def _count_sites(root: Path) -> Counter:
    found: Counter = Counter()
    for path in sorted(root.rglob("*.py")):
        n = len(_GLOBAL_LATEST.findall(_source(path)))
        if n:
            found[path.relative_to(APP).as_posix()] = n
    return found


def test_global_latest_league_selectors_match_the_pinned_set():
    found = _count_sites(APP)
    assert dict(found) == PINNED_GLOBAL_SELECTORS, (
        f"{HOW_TO_FIX}\n"
        f"  new or increased: {sorted((k, v) for k, v in found.items() if v > PINNED_GLOBAL_SELECTORS.get(k, 0))}\n"
        f"  removed or decreased: {sorted((k, v) for k, v in PINNED_GLOBAL_SELECTORS.items() if found.get(k, 0) < v)}"
    )


def test_the_pinned_total_is_what_the_audit_reports():
    # 20 here vs "21" in ADR-0001: recorded so the discrepancy is visible in the test, not only in a report.
    # +1 for analytics/legacy_league.py, the shared helper added after the audit.
    assert sum(PINNED_GLOBAL_SELECTORS.values()) == 21


def test_scanner_sees_wrapped_and_aliased_selectors():
    """The regex must not be fooled by line wrapping, aliasing, case, or string concatenation."""
    samples = [
        "SELECT 1 FROM leagues ORDER BY synced_at DESC LIMIT 1",
        "SELECT 1 FROM leagues\n   ORDER   BY\n   synced_at\n DESC\n LIMIT 1",
        "select 1 from leagues l order by l.synced_at desc limit 1",
        '"SELECT * FROM leagues"\n" ORDER BY synced_at DESC LIMIT 1"',
        "q = 'SELECT * FROM leagues' + \\\n     ' ORDER BY synced_at DESC'",
    ]
    for sample in samples:
        assert len(_GLOBAL_LATEST.findall(_LITERAL_JOIN.sub(" ", sample))) == 1, sample
    assert not _GLOBAL_LATEST.findall("ORDER BY synced_at ASC")
    assert not _GLOBAL_LATEST.findall("ORDER BY league_id, season DESC")


def test_no_module_under_app_leagues_has_a_global_latest_league_selector():
    offenders = _count_sites(LEAGUES_PKG)
    assert not offenders, f"{HOW_TO_FIX}\n  found in: {dict(offenders)}"


def test_the_tenancy_layer_never_reads_the_analytics_leagues_table():
    """app/leagues/** has its own tables; reading analytics `leagues` (one row per synced ESPN/Sleeper league,
    no tenant column) from there would be a global selector by another name."""
    pattern = re.compile(r"\b(?:FROM|JOIN)\s+leagues\b", re.IGNORECASE)
    offenders = [p.relative_to(APP).as_posix() for pkg in (LEAGUES_PKG, CONTROL_PLANE_PKG) for p in pkg.rglob("*.py")
                 if pattern.search(_source(p))]
    assert not offenders, f"{HOW_TO_FIX}\n  found in: {offenders}"


def test_the_tenancy_layer_does_not_open_the_analytics_database():
    """The analytics file (Settings.db_path / MotherDuck) is not a tenancy boundary; only tenancy_db_path is."""
    pattern = re.compile(r"(?<!tenancy_)\bdb_path\b|connect_motherduck|motherduck_database|api\.deps")
    offenders = {}
    for p in [*LEAGUES_PKG.rglob("*.py"), *CONTROL_PLANE_PKG.rglob("*.py")]:
        hits = sorted(set(pattern.findall(_source(p))))
        if hits:
            offenders[p.relative_to(APP).as_posix()] = hits
    assert not offenders, f"the tenancy layer must not touch the analytics database: {offenders}"


def test_league_access_is_decided_only_by_the_control_plane_authorizer():
    """`Access` carries a verified authorization decision and the snapshot pointer. Only app/control_plane/authz.py may
    build one, and only it and the /v2 router may call the authorizer (which runs inside the request's transaction)."""
    builders = sorted(p.relative_to(APP).as_posix() for p in APP.rglob("*.py")
                      if re.search(r"\bAccess\(", p.read_text("utf-8")))
    assert builders == ["control_plane/authz.py"], f"only authz.py may construct an Access; got {builders}"
    callers = sorted(p.relative_to(APP).as_posix() for p in APP.rglob("*.py")
                     if re.search(r"\bauthorize_league(?:_in)?\(", p.read_text("utf-8")))
    # api/league_scope.py authorizes the legacy routers mounted under /v2/leagues/{league_id} (Phase 2).
    assert callers == ["api/league_scope.py", "api/leagues_v2.py", "control_plane/authz.py"], callers


def test_the_control_plane_pool_is_only_constructed_by_get_control_plane():
    callers = sorted(p.relative_to(APP).as_posix() for p in APP.rglob("*.py")
                     if re.search(r"\bControlPlane\(", p.read_text("utf-8")))
    assert callers == ["control_plane/pg.py"], f"construct pools through get_control_plane(); got {callers}"


def test_legacy_routes_do_not_import_the_league_layer_or_the_control_plane():
    """The single-owner endpoints must not start depending on app.leagues or app.control_plane (they are only reached
    through the flag-gated /v2 router)."""
    offenders = []
    for p in sorted(APP.rglob("*.py")):
        rel = p.relative_to(APP).as_posix()
        if rel.startswith(("leagues/", "control_plane/")) or rel in ("api/leagues_v2.py", "api/league_scope.py",
                                                                      "main.py", "config.py"):
            continue
        if re.search(r"^\s*(?:from|import)\s+(?:app\.|\.+)?(?:\w+\.)*(?:leagues|control_plane)(?:\.|\s|$)",
                     p.read_text("utf-8"), re.M):
            offenders.append(rel)
    assert not offenders, f"legacy modules import app.leagues / app.control_plane: {offenders}"
