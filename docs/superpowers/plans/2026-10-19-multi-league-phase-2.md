# Multi-league Phase 2: League-file read path, Los Subies migrated — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve every existing page from per-league immutable DuckDB files (league file + shared global file) behind `authorize_league`, under `/v2/leagues/{lg}/…` and `/l/<lg>/…`, with Los Subies migrated and byte-identical to the legacy path.

**Architecture:** The publisher splits the analytics database into a global file (`global/<version>.duckdb`) and one league file per league (`leagues/<lg>/<version>.duckdb`) and moves the league's fenced pointer in `fi.league_snapshots`. A `/v2/leagues/{league_id}` mount of every legacy router runs an async `league_scope` dependency: authorize in Postgres → snapshot pointer → byte-bounded `/tmp` LRU of verified files → one in-memory DuckDB instance per (league file, global file) with both attached read-only → per-request cursor with `search_path = league, global` and a temporary `fantasy_teams` view whose `is_mine` marks the request's perspective team. `deps.db()` reads a ContextVar set by that dependency, so no engine changes. The frontend rewrites `/l/<lg>/<page>` to `/<page>` with an `x-fi-league` header that `lib/api.ts` turns into the `/v2/leagues/<lg>` prefix.

**Tech Stack:** FastAPI 0.143, DuckDB (ATTACH READ_ONLY, temp views), Cloudflare R2 (existing `app/db/r2.py`), Supabase Postgres (`fi_sync` for the pointer), Next.js 16 `proxy.ts`, pytest, node --test.

Master plan: [2026-10-19-multi-league-platform.md](2026-10-19-multi-league-platform.md). Spec: [2026-10-19-multi-league-platform-design.md](../specs/2026-10-19-multi-league-platform-design.md). Phase 1: [phase-1](2026-10-19-multi-league-phase-1.md).

## Global Constraints

- Free tiers only. `/tmp` LRU ≤ 350 MB (Vercel `/tmp` is 500 MB); R2 reads only (API token is read-only); no new paid service.
- `MULTILEAGUE_ENABLED` unset ⇒ every `/v2` path is a plain 404 and the Phase 1a single-snapshot path serves everything, unchanged. `MULTILEAGUE_UI_ENABLED` unset ⇒ no `/l/` routes and no cookie redirect.
- No engine rewrites. Engines receive the perspective team through the `fantasy_teams.is_mine` they already read (a per-request temp view) or the `team_id` query parameter they already accept; they read through the connection `deps.db()` returns.
- Authorization (`authorize_league_in`) runs before any league file is touched and before any cache lookup. Unknown and unauthorized are both 404. Public leagues need no sign-in.
- League responses are `Cache-Control: private, no-store` and carry `X-FI-Data-Version` and `X-FI-Global-Version` of the files actually read.
- Object keys are derived only from the authorized pointer (`fi.league_snapshots.object_key`, DB-constrained to `leagues/<league_id>/<data_version>.duckdb`) and `global/<global_version>.duckdb`; nothing user-supplied reaches a path.
- Downloaded files are verified: the version string ends in the first 24 hex chars of the file's sha256 and the bytes on disk must hash to it.
- Los Subies golden tests stay green from this phase on: legacy URLs and `/v2/leagues/{lg}/…` byte-identical (volatile fields masked).
- `test_league_scope_audit.py` ratchet unchanged or lower.
- No secrets in commits, logs, PR text or chat. Work happens in the worktree `.worktrees/multileague-phase-2` on branch `h/multileague-phase-2`.

## Decisions taken in this plan

| Topic | Decision | Why |
|---|---|---|
| Perspective seam | Per-request `CREATE TEMP VIEW fantasy_teams AS SELECT * REPLACE (team_id = <int> AS is_mine) FROM lg.main.fantasy_teams`; with no perspective, `false AS is_mine` | Every engine already derives "my team" from `is_mine` or `team_id`; zero engine edits (spike 2026-10-20: 0.1 ms per cursor) |
| Where the perspective comes from | `?team_id=` on the request (already accepted by the routes) > the signed-in user's saved `team_selections` > none | Spec: preference only, never verified |
| Global file version | Each league pointer names the global version it was built against (`league_snapshots.global_version`); the request attaches exactly that one | Consistent pair; responses report both |
| Integrity | `version = <UTC stamp>-<sha256[:24]>`; downloads re-hashed | No schema change needed for sha/size |
| Local development | `LEAGUE_FILES_DIR` serves objects from a directory instead of R2 | Local golden check on real Los Subies; frontend dev without R2 |
| No snapshot yet | 409 `{"detail": "First sync in progress.", "sync_state": …}` | Spec: never another league's data |
| Sign-in UI | Deferred to Phase 3 (Connect needs it first); Phase 2 perspective is URL + local storage | Master plan Phase 2 scope lists switcher + perspective picker only |
| Legacy URLs | With the UI flag on and a `fi_league` cookie, `/x` redirects to `/l/<cookie>/x`; without the cookie, unchanged | "Old URLs redirect to the viewer's last-viewed league" |

## File structure

| File | Responsibility |
|---|---|
| `backend/tests/golden/golden.py` | Route enumeration, param filling, volatile masking, record/compare helpers |
| `backend/tests/golden/e2e/*.json` | Committed golden responses for the E2E fixture league |
| `backend/tests/test_golden_legacy.py` | Legacy URLs match the committed goldens |
| `backend/tests/test_golden_v2.py` | `/v2/leagues/{lg}/…` matches the same goldens |
| `scripts/golden_check.py` | Local: real Los Subies, legacy vs v2, diff report |
| `backend/app/db/league_split.py` | `LEAGUE_TABLES`, `split_database()` |
| `backend/app/db/league_publish.py` | Build + upload global and league files, fenced pointer move |
| `scripts/publish_leagues.py` | CLI for the laptop / refresh_all |
| `backend/app/db/league_files.py` | `ObjectStore` (R2 or dir), `LeagueFileCache` (byte LRU, verified, atomic) |
| `backend/app/db/league_conn.py` | `LayeredDatabase` instances, `league_cursor()` |
| `backend/app/api/deps.py` | `LeagueScope` ContextVar; `db()` / `data_version()` league-aware |
| `backend/app/api/league_scope.py` | `league_scope` dependency, response cache, `/v2/leagues/{lg}` mount |
| `backend/app/main.py` | Mount the legacy routers a second time under the league prefix |
| `fantasy-intel/supabase/runbook-los-subies.sql` | One-time registration of Los Subies as a private league |
| `frontend/proxy.ts`, `frontend/lib/league-scope.ts` (+test) | `/l/<lg>` rewrite, cookie redirect |
| `frontend/lib/api.ts` | Prefix with `/v2/leagues/<lg>` when scoped |
| `frontend/components/LeagueSwitcher.tsx`, `lib/league-switcher.ts` (+test) | Switcher (local storage) + "data as of" |

---

### Task 1: Golden-response recorder and the legacy golden test (first, before any refactor)

**Files:**
- Create: `fantasy-intel/backend/tests/golden/__init__.py` (empty), `fantasy-intel/backend/tests/golden/golden.py`
- Create: `fantasy-intel/scripts/record_golden.py`
- Create: `fantasy-intel/backend/tests/test_golden_legacy.py`
- Create (generated): `fantasy-intel/backend/tests/golden/e2e/*.json` (52 files on the current router)

**Interfaces:**
- Produces: `Case(name, path)`, `legacy_get_routes(app)`, `cases(app, con, client=None) -> list[Case]`, `mask(value)`, `render(status, body) -> str`, `response_text(client, case, prefix="") -> str`, `build_e2e_db(path) -> Path`, `record(client, app, db_path, out_dir)`, `read_golden(out_dir, name) -> (path, body)`, `E2E_DIR`, `E2E_SEASON`. Task 6 calls `response_text(client, case, prefix="/v2/leagues/<lg>")`.

**Determinism (measured 2026-10-20):** two fixture builds and a cached repeat agree on all 52 cases once `run_id`, wall-clock stamps and trade-search diagnostics (`seconds`, `lineup_solves*`) are masked. Datetimes with a time part are masked; dates (game days) are data.

- [ ] **Step 1: Write the helpers** — `tests/golden/golden.py`:

```python
"""Golden responses for every legacy GET endpoint (Phase 2 of the multi-league plan).

`cases(app, con)` enumerates the live router's GET routes outside /v2, fills path parameters from the database (a
rostered player, a team, a GSIS id), and adds the perspective variant (`team_id`) for routes that accept one. `mask()`
replaces values that legitimately change between two builds of the same data (wall-clock timestamps, the data
version) with a placeholder, so two responses are compared on content only. Goldens are pretty-printed JSON with
sorted keys: a diff in review shows exactly which field changed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

GOLDEN_DIR = Path(__file__).resolve().parent
MASK = "<masked>"
# Keys whose values are wall-clock or build stamps, not data.
VOLATILE_KEYS = {"computed_at", "generated_at", "synced_at", "published_at", "data_version", "fetched_at",
                 "league_synced_at", "roster_synced_at", "last_synced_at", "refreshed_at", "built_at", "as_of",
                 "now", "server_time", "created_at", "updated_at", "checked_at", "run_at", "started_at",
                 "finished_at", "source_updated_at", "snapshot_at", "captured_at", "run_id"}
# Search diagnostics: wall time and work counters, which depend on what an in-process cache already held.
VOLATILE_PREFIXES = ("lineup_solves",)
VOLATILE_DIAGNOSTICS = {"seconds"}
# ISO-8601 datetimes with a time part (dates alone are data: game dates, weeks).
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")


@dataclass(frozen=True)
class Case:
    name: str          # file stem, stable and filesystem-safe
    path: str          # request path with query string, relative to the API root (e.g. /api/overview?team_id=2)


def _walk(routes):
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from _walk(inner.routes)
        else:
            yield r


def legacy_get_routes(app) -> list:
    out = []
    for r in _walk(app.routes):
        path = getattr(r, "path", "")
        if "GET" in getattr(r, "methods", set()) and not path.startswith(("/v2", "/docs", "/openapi", "/redoc")) \
                and path != "/health":
            out.append(r)
    return sorted(out, key=lambda r: r.path)


def _accepts(route, name: str) -> bool:
    return any(p.name == name for p in route.dependant.query_params)


def sample_ids(con) -> dict:
    """Path parameter values present in the database: two rostered players, the second team, a GSIS id."""
    q = lambda sql: (con.execute(sql).fetchone() or [None])[0]   # noqa: E731
    return {
        "espn_player_id": q("SELECT espn_player_id FROM lineup_snapshots ORDER BY espn_player_id LIMIT 1"),
        "other_player_id": q("SELECT DISTINCT espn_player_id FROM lineup_snapshots ORDER BY espn_player_id LIMIT 1 OFFSET 1"),
        "team_id": q("SELECT team_id FROM fantasy_teams ORDER BY team_id LIMIT 1 OFFSET 1"),
        "gsis_id": q("SELECT gsis_id FROM player_id_map WHERE gsis_id IS NOT NULL ORDER BY gsis_id LIMIT 1"),
    }


# Routes whose required query parameters cannot be inferred from the signature alone.
def _required_query(path: str, ids: dict, client) -> dict | None:
    if path == "/api/compare":
        return {"a": ids["espn_player_id"], "b": ids["other_player_id"]}
    if path == "/api/research/opportunity":
        return {"position": "WR"}
    if path == "/api/trades/discover/evidence":
        rows = client.get("/api/trades/discover").json() if client is not None else {}
        key = next((x["key"] for items in (rows.get("sections") or {}).values() for x in items), None)
        return {"key": key} if key else None
    return {}


def cases(app, con, client=None) -> list[Case]:
    """Every legacy GET case. `client` (a TestClient on the legacy API) resolves data-dependent parameters such as
    a trade-discovery row key; without it those routes are recorded with their default (validation) answer."""
    ids = sample_ids(con)
    out = []
    for r in legacy_get_routes(app):
        path = r.path
        for name, value in ids.items():
            path = path.replace("{" + name + "}", str(value))
        if "{" in path:
            raise AssertionError(f"no sample value for a path parameter in {r.path}")
        stem = re.sub(r"[^A-Za-z0-9]+", "_", r.path.strip("/")).strip("_") or "root"
        required = _required_query(r.path, ids, client)
        query = "?" + urlencode(required) if required else ""
        out.append(Case(stem, path + query))
        if _accepts(r, "team_id") and "{team_id}" not in r.path:
            sep = "&" if query else "?"
            out.append(Case(stem + "__team", path + query + sep + urlencode({"team_id": ids["team_id"]})))
    return out


def mask(value, key: str | None = None):
    if isinstance(value, dict):
        return {k: mask(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    if key is not None and (key in VOLATILE_KEYS or key in VOLATILE_DIAGNOSTICS or key.startswith(VOLATILE_PREFIXES)) \
            and value is not None:
        return MASK
    if isinstance(value, str) and _DATETIME.match(value):
        return MASK
    return value


def render(status: int, body) -> str:
    return json.dumps({"status": status, "body": mask(body)}, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def response_text(client, case: Case, prefix: str = "") -> str:
    r = client.get(prefix + case.path)
    try:
        body = r.json()
    except ValueError:
        body = {"non_json": r.text[:200]}
    return render(r.status_code, body)


E2E_DIR = GOLDEN_DIR / "e2e"
E2E_SEASON = "2026"


def build_e2e_db(path: Path) -> Path:
    """The E2E fixture league (scripts/build_e2e_db.py): synthetic, deterministic, no network."""
    import importlib.util
    import sys
    scripts = Path(__file__).resolve().parents[3] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location("build_e2e_db", scripts / "build_e2e_db.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.build(Path(path))
    finally:
        sys.path.remove(str(scripts))
    return Path(path)


def record(client, app, db_path: Path, out_dir: Path) -> list[Case]:
    """Write one golden file per case (and delete stale ones)."""
    import duckdb
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        cs = cases(app, con, client)
    finally:
        con.close()
    out_dir.mkdir(parents=True, exist_ok=True)
    keep = set()
    for c in cs:
        (out_dir / f"{c.name}.json").write_text(f"// {c.path}\n" + response_text(client, c))
        keep.add(f"{c.name}.json")
    for old in out_dir.glob("*.json"):
        if old.name not in keep:
            old.unlink()
    return cs


def read_golden(out_dir: Path, name: str) -> tuple[str, str]:
    """(path recorded with the golden, golden body text)."""
    text = (out_dir / f"{name}.json").read_text()
    first, _, body = text.partition("\n")
    return first.removeprefix("// "), body
```

- [ ] **Step 2: Write the recorder** — `scripts/record_golden.py`:

```python
"""Re-record the committed golden responses for the E2E fixture league (backend/tests/golden/e2e/).

    cd backend && uv run python ../scripts/record_golden.py

Run it only when a change to an endpoint's output is intended; the diff of tests/golden/e2e/ is the review of that
change. tests/test_golden_legacy.py and tests/test_golden_v2.py compare against these files.
"""
import os
import sys
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401

sys.path.insert(0, str(_bootstrap.BACKEND / "tests"))

from golden.golden import E2E_DIR, E2E_SEASON, build_e2e_db, record  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db = build_e2e_db(Path(tmp) / "e2e.duckdb")
        os.environ.update(FANTASY_DB_PATH=str(db), ESPN_SEASON=E2E_SEASON, FANTASY_API_KEY="", MULTILEAGUE_ENABLED="")
        from fastapi.testclient import TestClient
        from app.main import app
        cs = record(TestClient(app, raise_server_exceptions=False), app, db, E2E_DIR)
    print(f"recorded {len(cs)} golden responses in {E2E_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Write the failing test** — `tests/test_golden_legacy.py`:

```python
"""Every legacy GET endpoint answers exactly what tests/golden/e2e/ recorded for the E2E fixture league (volatile
stamps masked). A failure means an endpoint's output changed: if intended, re-record with
`uv run python ../scripts/record_golden.py` and review the diff; otherwise it is a regression."""
import difflib

import pytest
from fastapi.testclient import TestClient

from golden.golden import E2E_DIR, E2E_SEASON, build_e2e_db, cases, read_golden, response_text


@pytest.fixture(scope="module")
def e2e_db(tmp_path_factory):
    return build_e2e_db(tmp_path_factory.mktemp("golden") / "e2e.duckdb")


@pytest.fixture
def legacy_client(e2e_db, monkeypatch):
    monkeypatch.setenv("FANTASY_DB_PATH", str(e2e_db))
    monkeypatch.setenv("ESPN_SEASON", E2E_SEASON)
    for name in ("FANTASY_API_KEY", "MULTILEAGUE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def _diff(expected: str, actual: str) -> str:
    return "\n".join(list(difflib.unified_diff(expected.splitlines(), actual.splitlines(), "golden", "actual",
                                               lineterm="", n=2))[:60])


def test_every_legacy_get_route_has_a_golden(legacy_client, e2e_db):
    import duckdb
    from app.main import app
    con = duckdb.connect(str(e2e_db), read_only=True)
    try:
        names = {c.name for c in cases(app, con, legacy_client)}
    finally:
        con.close()
    recorded = {p.stem for p in E2E_DIR.glob("*.json")}
    assert names == recorded, f"missing: {sorted(names - recorded)}; stale: {sorted(recorded - names)}"


def test_legacy_responses_match_the_goldens(legacy_client):
    from golden.golden import Case
    failures = []
    for golden in sorted(E2E_DIR.glob("*.json")):
        path, expected = read_golden(E2E_DIR, golden.stem)
        actual = response_text(legacy_client, Case(golden.stem, path))
        if actual != expected:
            failures.append(f"{golden.stem} ({path}):\n{_diff(expected, actual)}")
    assert not failures, "\n\n".join(failures)
```

Run: `uv run pytest tests/test_golden_legacy.py -q` → FAIL (`missing: [...]`, no goldens yet).

- [ ] **Step 4: Record and re-run**

```bash
cd fantasy-intel/backend && uv run python ../scripts/record_golden.py && uv run pytest tests/test_golden_legacy.py -q
```
Expected: `recorded 52 golden responses`, then `2 passed`. Confirm determinism: re-run the recorder and `git status tests/golden/e2e` shows no change.

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/backend/tests/golden fantasy-intel/backend/tests/test_golden_legacy.py fantasy-intel/scripts/record_golden.py
git commit -m "Phase 2: golden responses for every legacy GET endpoint on the E2E fixture league"
```

### Task 2: Split the analytics database into a league file and a global file

**Files:**
- Create: `fantasy-intel/backend/app/db/league_split.py`
- Create: `fantasy-intel/backend/tests/test_league_split.py`

**Interfaces:**
- Produces: `LEAGUE_TABLES`, `GLOBAL_TABLES`, `BOTH_TABLES` (frozensets), `SplitResult(league_path, global_path, league_version, global_version, league_id, season, schema_version)`, `version_for(path, stamp) -> str` (`<YYYYMMDDTHHMMSS>Z-<sha256[:24]>`), `split_database(src, league_out, global_out, *, stamp, league=None) -> SplitResult`.
- Rule: global = league-independent facts only; anything keyed by an ESPN player id or computed in a league's scoring is a league table; an unclassified table is an error (test and runtime).

- [ ] **Step 1: Write the failing tests**

`fantasy-intel/backend/tests/test_league_split.py`:

```python
"""Splitting the analytics database into one league file and one global file (Phase 2)."""
import duckdb
import pytest

from app.db.league_split import GLOBAL_TABLES, LEAGUE_TABLES, BOTH_TABLES, split_database
from golden.golden import build_e2e_db


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    return build_e2e_db(tmp_path_factory.mktemp("split") / "e2e.duckdb")


def _tables(path):
    con = duckdb.connect(str(path), read_only=True)
    try:
        return {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'").fetchall()}
    finally:
        con.close()


def test_every_schema_table_is_classified_exactly_once(e2e):
    assert not (LEAGUE_TABLES & GLOBAL_TABLES) and not (BOTH_TABLES & (LEAGUE_TABLES | GLOBAL_TABLES))
    unclassified = _tables(e2e) - LEAGUE_TABLES - GLOBAL_TABLES - BOTH_TABLES
    assert not unclassified, f"classify these in app/db/league_split.py: {sorted(unclassified)}"


def test_split_reproduces_every_table_in_exactly_one_file(e2e, tmp_path):
    out = split_database(e2e, tmp_path / "lg.duckdb", tmp_path / "gl.duckdb", stamp="2026-10-20T12:00:00")
    league, glob = _tables(out.league_path), _tables(out.global_path)
    assert league == LEAGUE_TABLES | BOTH_TABLES and glob == GLOBAL_TABLES | BOTH_TABLES
    con = duckdb.connect()
    con.execute(f"ATTACH '{e2e}' AS src (READ_ONLY)")
    con.execute(f"ATTACH '{out.league_path}' AS lg (READ_ONLY)")
    con.execute(f"ATTACH '{out.global_path}' AS gl (READ_ONLY)")
    for t in sorted(_tables(e2e) - BOTH_TABLES):
        db = "lg" if t in LEAGUE_TABLES else "gl"
        assert con.execute(f'SELECT count(*) FROM (SELECT * FROM src.main."{t}" EXCEPT ALL SELECT * FROM {db}.main."{t}")').fetchone() == (0,), t
        assert con.execute(f'SELECT count(*) FROM src.main."{t}"').fetchone() == con.execute(f'SELECT count(*) FROM {db}.main."{t}"').fetchone(), t


def test_versions_carry_the_file_digest_and_the_stamp(e2e, tmp_path):
    from app.db.r2 import file_sha256
    out = split_database(e2e, tmp_path / "lg.duckdb", tmp_path / "gl.duckdb", stamp="2026-10-20T12:00:00")
    assert out.league_version == "20261020T120000Z-" + file_sha256(out.league_path)[:24]
    assert out.global_version == "20261020T120000Z-" + file_sha256(out.global_path)[:24]
    con = duckdb.connect(str(out.league_path), read_only=True)
    assert con.execute("SELECT value FROM schema_meta WHERE key = 'published_at'").fetchone() == ("2026-10-20T12:00:00",)
    con.close()


def test_league_tables_keep_only_the_requested_league(e2e, tmp_path):
    con = duckdb.connect(str(e2e), read_only=True)
    league_id, season = con.execute("SELECT league_id, season FROM leagues").fetchone()
    con.close()
    out = split_database(e2e, tmp_path / "lg.duckdb", tmp_path / "gl.duckdb", stamp="2026-10-20T12:00:00",
                         league=(league_id, season))
    lg = duckdb.connect(str(out.league_path), read_only=True)
    assert lg.execute("SELECT DISTINCT league_id, season FROM fantasy_teams").fetchall() == [(league_id, season)]
    lg.close()
    with pytest.raises(ValueError, match="not in"):
        split_database(e2e, tmp_path / "x.duckdb", tmp_path / "y.duckdb", stamp="2026-10-20T12:00:00", league=(1, 1999))


def test_a_database_with_two_leagues_is_refused_even_when_one_is_named(e2e, tmp_path):
    import shutil
    two = tmp_path / "two.duckdb"
    shutil.copy(e2e, two)
    con = duckdb.connect(str(two))
    con.execute("INSERT INTO leagues SELECT * REPLACE (league_id + 1 AS league_id) FROM leagues")
    con.close()
    for league in (None, con_league(e2e)):
        with pytest.raises(ValueError, match="more than one league"):
            split_database(two, tmp_path / "lg.duckdb", tmp_path / "gl.duckdb", stamp="2026-10-20T12:00:00",
                           league=league)


def con_league(path):
    con = duckdb.connect(str(path), read_only=True)
    try:
        return con.execute("SELECT league_id, season FROM leagues").fetchone()
    finally:
        con.close()
```

```bash
cd fantasy-intel/backend && uv run pytest tests/test_league_split.py -q
```
Expected: collection error: No module named app.db.league_split

- [ ] **Step 2: Implement the split**

`fantasy-intel/backend/app/db/league_split.py`:

```python
"""Split the analytics database into one league file and one global file (multi-league Phase 2).

The hosted API serves a league by attaching its league file and the global file it was built against, read-only,
with `search_path = league, global` (app/db/league_conn.py). This module decides which table lives where:

  * LEAGUE_TABLES hold one league's data, or values derived from its ESPN account or its scoring rules
    (ADR-0001 "Audit": fantasy_players availability, ESPN points and projections, Ours runs keyed by a scoring
    fingerprint, owner research built on them). A league file must never carry another league's rows.
  * GLOBAL_TABLES hold league-independent facts: nflverse statistics, schedules, contexts, external usage, weather,
    the model registry. Every league reads the same global file.
  * BOTH_TABLES exist in both files (`schema_meta`: the league file's copy wins through search_path).

A table missing from all three sets fails tests/test_league_split.py: a new table must be classified on purpose,
and when in doubt it is a league table (a misplaced league table in the global file would leak to every league).

Versions are `<UTC stamp>-<sha256[:24]>` of the written file, so a download can be verified against its name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from .r2 import file_sha256

LEAGUE_TABLES = frozenset({
    # one league's provider data
    "leagues", "league_members", "fantasy_teams", "fantasy_players", "matchups", "draft_picks", "lineup_snapshots",
    "roster_history", "transactions", "transaction_items", "activity_messages",
    # ESPN values in the league's scoring / from its ESPN account
    "espn_player_weekly_points", "espn_projection_history", "player_ros_projections", "player_ownership_history",
    "player_id_map", "player_scores", "injury_retrievals",
    # analytics computed for the league
    "waiver_recommendations", "team_waiver_recommendations", "team_position_needs", "team_week_lineup_projections",
    "team_ros_lineup_projections", "manager_metrics", "manager_classifications",
    # Ours, kicker and D/ST runs carry a scoring fingerprint: league scoring
    "independent_projection_runs", "independent_projection_rows", "independent_player_projections",
    "kicker_projection_runs", "kicker_projection_rows", "dst_projection_runs", "dst_projection_rows",
    # owner research and run bookkeeping of this database
    "projection_performance_reports", "model_replays", "sync_runs", "validation_issues",
})

GLOBAL_TABLES = frozenset({
    "nfl_players", "nfl_schedules", "nfl_defense_week", "nfl_kick_attempts", "nfl_pbp_player_week",
    "nfl_team_game_drives", "nfl_team_week", "player_week_usage", "player_context", "defense_context", "team_context",
    "team_game_lines", "weather_forecasts", "external_player_map", "external_player_week_usage",
    "external_player_week_usage_revisions", "external_usage_imports", "external_usage_issues", "approved_artifacts",
})

BOTH_TABLES = frozenset({"schema_meta"})


@dataclass(frozen=True)
class SplitResult:
    league_path: Path
    global_path: Path
    league_version: str
    global_version: str
    league_id: int
    season: int
    schema_version: int | None


def _q(path: Path) -> str:
    return str(path).replace("'", "''")


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def version_for(path: Path, stamp: str) -> str:
    compact = stamp.replace("-", "").replace(":", "").split(".")[0]
    return f"{compact}Z-{file_sha256(path)[:24]}"


def _columns(con, table: str) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT column_name FROM duckdb_columns() WHERE database_name = 'src' AND schema_name = 'main' "
        "AND table_name = ?", [table]).fetchall()}


def split_database(src: Path, league_out: Path, global_out: Path, *, stamp: str,
                   league: tuple[int, int] | None = None) -> SplitResult:
    """Write `league_out` (the league's tables) and `global_out` from `src`, both stamped
    `schema_meta.published_at = stamp`. The source must hold exactly one league; `league` = (league_id, season), when
    given, asserts which one. Rows of tables with a league column are filtered to it as defence in depth. Existing
    output files are replaced."""
    src, league_out, global_out = Path(src), Path(league_out), Path(global_out)
    for out in (league_out, global_out):
        out.unlink(missing_ok=True)
        Path(str(out) + ".wal").unlink(missing_ok=True)
    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{_q(src)}' AS src (READ_ONLY)")
        known = {r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'src' AND schema_name = 'main'").fetchall()}
        unclassified = known - LEAGUE_TABLES - GLOBAL_TABLES - BOTH_TABLES
        if unclassified:
            raise ValueError(f"unclassified tables (add them to app/db/league_split.py): {sorted(unclassified)}")
        leagues = con.execute("SELECT DISTINCT league_id, season FROM src.main.leagues ORDER BY 1, 2").fetchall()
        if len(leagues) != 1:
            # Several league tables (fantasy_players, ESPN points and projections) have no league column, so a
            # database holding two leagues cannot be split without mixing them. Each league is synced on its own.
            raise ValueError(f"{src} holds more than one league ({len(leagues)}); league tables without a league "
                             "column cannot be split safely")
        if league is not None and tuple(league) != tuple(leagues[0]):
            raise ValueError(f"league {league} is not in {src}")
        league = leagues[0]
        league_id, season = int(league[0]), int(league[1])
        schema = con.execute("SELECT value FROM src.main.schema_meta WHERE key = 'schema_version'").fetchone()

        for alias, out, tables in (("lg", league_out, LEAGUE_TABLES), ("gl", global_out, GLOBAL_TABLES)):
            con.execute(f"ATTACH '{_q(out)}' AS {alias}")
            con.execute(f"COPY FROM DATABASE src TO {alias} (SCHEMA)")      # tables with their exact types
            for t in sorted(known):
                if t in tables or t in BOTH_TABLES:
                    cols = _columns(con, t)
                    where, params = "", []
                    if alias == "lg" and {"league_id", "season"} <= cols:
                        where, params = " WHERE league_id = ? AND season = ?", [league_id, season]
                    elif alias == "lg" and "league_id" in cols:
                        where, params = " WHERE league_id = ?", [league_id]
                    con.execute(f"INSERT INTO {alias}.main.{_ident(t)} SELECT * FROM src.main.{_ident(t)}{where}",
                                params)
                else:
                    con.execute(f"DROP TABLE {alias}.main.{_ident(t)}")
            con.execute(f"DELETE FROM {alias}.main.schema_meta WHERE key = 'published_at'")
            con.execute(f"INSERT INTO {alias}.main.schema_meta VALUES ('published_at', ?)", [stamp])
            con.execute(f"CHECKPOINT {alias}")
            con.execute(f"DETACH {alias}")
    finally:
        con.close()
    return SplitResult(league_out, global_out, version_for(league_out, stamp), version_for(global_out, stamp),
                       league_id, season, int(schema[0]) if schema else None)
```

- [ ] **Step 3: Run the tests**

```bash
uv run pytest tests/test_league_split.py -q
```
Expected: 5 passed

- [ ] **Step 4: Commit**

```bash
git add fantasy-intel/backend/app/db/league_split.py fantasy-intel/backend/tests/test_league_split.py
git commit -m "Phase 2: split the analytics database into a league file and a global file"
```


### Task 3: League file cache (verified, atomic, downloaded once, byte-bounded LRU)

**Files:**
- Create: `fantasy-intel/backend/app/db/league_files.py`
- Create: `fantasy-intel/backend/tests/test_league_files.py`
- Modify: `fantasy-intel/backend/app/db/league_split.py (version = `<stamp>Z-<sha256[:24]>-<bytes>`, `VERSION_RE`)`
- Modify: `fantasy-intel/backend/app/db/r2.py (`download(..., expected_sha256_prefix=None)` + size check)`
- Modify: `fantasy-intel/backend/tests/test_league_split.py (version assertion)`

**Interfaces:**
- Consumes: `VERSION_RE` (Task 2), `r2.download(cfg, key, dest, size=, expected_sha256_prefix=)`.
- Produces: `league_key(league_id, version) -> str`, `global_key(version) -> str`, `ObjectKeyError`, `R2Store(cfg)`, `DirStore(root)`, `LeagueFileCache(store, cache_dir, *, max_bytes=350 MiB, on_evict=None)` with `.league_file(league_id, version) -> Path`, `.global_file(version) -> Path`, `.bytes_used`.
- Version carries the byte size so R2Store can use the 8-way ranged download from Phase 0b; downloads are verified against the sha256 prefix and size in the version.

- [ ] **Step 1: Write the failing tests**

`fantasy-intel/backend/tests/test_league_files.py`:

```python
"""The per-instance cache of league and global files: verified, atomic, downloaded once, byte-bounded."""
import hashlib
import threading

import pytest

from app.db.league_files import DirStore, LeagueFileCache, ObjectKeyError, R2Store, global_key, league_key
from app.db.r2 import R2Error
from fake_r2 import FakeR2

LG = "lg_" + "a" * 24


def blob(n: int, seed: int) -> bytes:
    return (hashlib.sha256(str(seed).encode()).digest() * (n // 32 + 1))[:n]


def version_of(data: bytes, stamp="20261020T120000Z") -> str:
    return f"{stamp}-{hashlib.sha256(data).hexdigest()[:24]}-{len(data)}"


@pytest.fixture
def r2():
    fake = FakeR2()
    yield fake
    fake.close()


def put(fake, key, data):
    fake.objects[key] = data


def test_keys_are_derived_only_from_validated_ids_and_versions():
    v = version_of(b"x" * 10)
    assert league_key(LG, v) == f"leagues/{LG}/{v}.duckdb"
    assert global_key(v) == f"global/{v}.duckdb"
    for bad_lg, bad_v in ((LG + "/../x", v), ("lg_short", v), (LG, "../../etc"), (LG, v + "/x"), (LG, "2026-x")):
        with pytest.raises(ObjectKeyError):
            league_key(bad_lg, bad_v)


def test_downloads_once_verifies_and_serves_the_same_path(r2, tmp_path):
    data = blob(5000, 1)
    v = version_of(data)
    put(r2, league_key(LG, v), data)
    cache = LeagueFileCache(R2Store(r2.config), tmp_path / "c", max_bytes=1 << 20)
    p1 = cache.league_file(LG, v)
    p2 = cache.league_file(LG, v)
    assert p1 == p2 and p1.read_bytes() == data
    assert [m for m, k, _ in r2.requests if m == "GET"] == ["GET"]
    assert not list((tmp_path / "c").glob("*.part"))


def test_concurrent_first_requests_download_once(r2, tmp_path):
    data = blob(200_000, 2)
    v = version_of(data)
    put(r2, league_key(LG, v), data)
    cache = LeagueFileCache(R2Store(r2.config), tmp_path / "c", max_bytes=10 << 20)
    paths, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        paths.append(cache.league_file(LG, v))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(set(paths)) == 1 and len([1 for m, k, _ in r2.requests if m == "GET"]) == 1


@pytest.mark.parametrize("tamper", ["content", "size"])
def test_a_corrupt_or_wrong_size_object_is_rejected_and_nothing_is_cached(r2, tmp_path, tamper):
    data = blob(5000, 3)
    v = version_of(data)
    put(r2, league_key(LG, v), data[:-1] + b"!" if tamper == "content" else data + b"extra")
    cache = LeagueFileCache(R2Store(r2.config), tmp_path / "c", max_bytes=1 << 20)
    with pytest.raises(R2Error):
        cache.league_file(LG, v)
    assert list((tmp_path / "c").iterdir()) == [] and cache.bytes_used == 0


def test_the_lru_never_exceeds_its_bound_under_a_20_league_mix(r2, tmp_path):
    import random
    rng = random.Random(7)
    leagues = []
    for i in range(20):
        lg = f"lg_{i:024d}"
        data = blob(rng.randint(30_000, 90_000), i)
        v = version_of(data)
        put(r2, league_key(lg, v), data)
        leagues.append((lg, v))
    bound = 300_000
    evicted = []
    cache = LeagueFileCache(R2Store(r2.config), tmp_path / "c", max_bytes=bound, on_evict=evicted.append)
    for _ in range(200):
        lg, v = rng.choice(leagues)
        p = cache.league_file(lg, v)
        assert p.exists()
        on_disk = sum(f.stat().st_size for f in (tmp_path / "c").iterdir())
        assert on_disk <= bound and cache.bytes_used == on_disk
    assert evicted, "the mix is larger than the bound, so something must have been evicted"


def test_a_file_larger_than_the_whole_cache_is_refused(r2, tmp_path):
    data = blob(5000, 4)
    v = version_of(data)
    put(r2, league_key(LG, v), data)
    with pytest.raises(ValueError, match="larger than the cache"):
        LeagueFileCache(R2Store(r2.config), tmp_path / "c", max_bytes=4000).league_file(LG, v)


def test_dir_store_serves_local_objects_with_the_same_checks(tmp_path):
    data = blob(4000, 5)
    v = version_of(data)
    src = tmp_path / "objects" / f"global/{v}.duckdb"
    src.parent.mkdir(parents=True)
    src.write_bytes(data)
    cache = LeagueFileCache(DirStore(tmp_path / "objects"), tmp_path / "c", max_bytes=1 << 20)
    assert cache.global_file(v).read_bytes() == data
    src.write_bytes(data[:-1] + b"?")
    bad = version_of(b"other")
    with pytest.raises(R2Error):
        cache.global_file(bad)
```

```bash
cd fantasy-intel/backend && uv run pytest tests/test_league_files.py -q
```
Expected: collection error (module missing)

- [ ] **Step 2: Extend `r2.download` with `expected_sha256_prefix` and a size check (inside the existing try, before `part.replace(dest)`)**

```bash
#   if expected_sha256_prefix and not digest.startswith(expected_sha256_prefix): raise R2Error(None, "ChecksumMismatch", key)
#   if size is not None and part.stat().st_size != size: raise R2Error(None, "SizeMismatch", key)
```

- [ ] **Step 3: Implement the cache**

`fantasy-intel/backend/app/db/league_files.py`:

```python
"""Per-instance cache of immutable league and global DuckDB files (multi-league Phase 2).

Objects are immutable: `leagues/<lg_id>/<version>.duckdb` and `global/<version>.duckdb`, where the version is
`<stamp>Z-<sha256[:24]>-<bytes>` (app/db/league_split.py). A version therefore never goes stale, and every download
is verified against its own name. Keys are built here from a validated league id and version only; nothing a
request supplies reaches a path or an object key.

The cache keeps files in `cache_dir` (Vercel: /tmp) under a byte bound (350 MB by default, /tmp is 500 MB).
Downloads go to a temporary name and are renamed into place, so a reader never sees a partial file; one download
per object at a time (other requests for the same object wait for it); least-recently-used files are evicted to
stay under the bound, and `on_evict(path)` lets the connection cache close what it had open on them.

The object store is R2 (read-only token) in production, or a local directory (`LEAGUE_FILES_DIR`) for development
and the local golden check.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Protocol

from . import r2
from .league_split import VERSION_RE

DEFAULT_MAX_BYTES = 350 << 20
_LEAGUE_ID = re.compile(r"lg_[A-Za-z0-9_-]{16,64}")


class ObjectKeyError(ValueError):
    """A league id or version that is not ours. Callers never get this from request input: authorization and the
    database constraints come first; it is defence in depth."""


def _check_version(version: str) -> re.Match:
    m = VERSION_RE.fullmatch(version) if isinstance(version, str) else None
    if m is None:
        raise ObjectKeyError("malformed version")
    return m


def league_key(league_id: str, version: str) -> str:
    if not isinstance(league_id, str) or not _LEAGUE_ID.fullmatch(league_id):
        raise ObjectKeyError("malformed league id")
    _check_version(version)
    return f"leagues/{league_id}/{version}.duckdb"


def global_key(version: str) -> str:
    _check_version(version)
    return f"global/{version}.duckdb"


class ObjectStore(Protocol):
    def fetch(self, key: str, dest: Path, *, sha256_prefix: str, size: int) -> None: ...


class R2Store:
    """Production: the private bucket, read with the API's Object Read token."""

    def __init__(self, cfg: r2.R2Config):
        self.cfg = cfg

    def fetch(self, key: str, dest: Path, *, sha256_prefix: str, size: int) -> None:
        r2.download(self.cfg, key, dest, size=size, expected_sha256_prefix=sha256_prefix)


class DirStore:
    """Development: objects laid out under a directory with the same keys."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def fetch(self, key: str, dest: Path, *, sha256_prefix: str, size: int) -> None:
        src = self.root / key
        if not src.is_file():
            raise r2.R2Error(404, "NoSuchKey", key)
        part = dest.with_name(dest.name + ".part")
        try:
            shutil.copyfile(src, part)
            if part.stat().st_size != size:
                raise r2.R2Error(None, "SizeMismatch", key)
            if not r2.file_sha256(part).startswith(sha256_prefix):
                raise r2.R2Error(None, "ChecksumMismatch", key)
            part.replace(dest)
        finally:
            part.unlink(missing_ok=True)


class LeagueFileCache:
    def __init__(self, store: ObjectStore, cache_dir: Path, *, max_bytes: int = DEFAULT_MAX_BYTES,
                 on_evict: Callable[[Path], None] | None = None):
        self.store, self.cache_dir, self.max_bytes = store, Path(cache_dir), int(max_bytes)
        self.on_evict = on_evict
        self._lock = threading.Lock()                       # guards _files and _downloading
        self._files: OrderedDict[str, tuple[Path, int]] = OrderedDict()   # key -> (path, bytes), LRU order
        self._downloading: dict[str, threading.Event] = {}
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for leftover in self.cache_dir.iterdir():          # a fresh index: files from an earlier process are dropped
            if leftover.is_file():
                leftover.unlink(missing_ok=True)

    @property
    def bytes_used(self) -> int:
        with self._lock:
            return sum(size for _, size in self._files.values())

    def league_file(self, league_id: str, version: str) -> Path:
        return self._get(league_key(league_id, version), version)

    def global_file(self, version: str) -> Path:
        return self._get(global_key(version), version)

    def _local_path(self, key: str) -> Path:
        return self.cache_dir / key.replace("/", "__")

    def _get(self, key: str, version: str) -> Path:
        m = _check_version(version)
        size = int(m["bytes"])
        if size > self.max_bytes:
            raise ValueError(f"{key} is larger than the cache ({size} > {self.max_bytes} bytes)")
        while True:
            with self._lock:
                hit = self._files.get(key)
                if hit is not None and hit[0].exists():
                    self._files.move_to_end(key)
                    return hit[0]
                waiting = self._downloading.get(key)
                if waiting is None:
                    done = self._downloading[key] = threading.Event()
                    break
            waiting.wait()                                   # someone else is fetching this object; then re-check
        path = self._local_path(key)
        try:
            self._make_room(size)
            self.store.fetch(key, path, sha256_prefix=m["sha"], size=size)
            with self._lock:
                self._files[key] = (path, size)
                self._files.move_to_end(key)
            self._make_room(0)
            return path
        finally:
            with self._lock:
                self._downloading.pop(key, None)
            done.set()

    def _make_room(self, incoming: int) -> None:
        """Evict least-recently-used files until `incoming` more bytes fit under the bound."""
        evicted = []
        with self._lock:
            used = sum(size for _, size in self._files.values())
            while self._files and used + incoming > self.max_bytes:
                _, (path, size) = self._files.popitem(last=False)
                used -= size
                evicted.append(path)
        for path in evicted:
            if self.on_evict is not None:
                self.on_evict(path)                          # close connections first, then free the space
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_league_files.py tests/test_league_split.py tests/test_r2.py tests/test_snapshot.py -q
```
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add -A fantasy-intel/backend/app/db fantasy-intel/backend/tests/test_league_files.py fantasy-intel/backend/tests/test_league_split.py
git commit -m "Phase 2: verified, byte-bounded league file cache"
```


### Task 4: Layered read-only connection with the perspective as `is_mine`

**Files:**
- Create: `fantasy-intel/backend/app/db/league_conn.py`
- Create: `fantasy-intel/backend/tests/test_league_conn.py`

**Interfaces:**
- Consumes: split files (Task 2) as fixtures.
- Produces: `perspective_team(raw) -> int | None`; `LayeredDatabases()` with `.cursor(league_path, global_path, *, perspective) -> duckdb cursor`, `.forget(path)` (the Task 3 cache's `on_evict`), `len()`.
- Measured (spike 2026-10-20): instance + two attaches ~3 ms; per-request cursor + view ~0.1 ms; config lock still allows per-cursor `SET search_path`; external access and ATTACH are blocked; a cursor outlives a dropped instance.

- [ ] **Step 1: Write the failing tests**

`fantasy-intel/backend/tests/test_league_conn.py`:

```python
"""The layered connection: league file + global file attached read-only, league first, perspective as is_mine."""
import duckdb
import pytest

from app.db.league_conn import LayeredDatabases, perspective_team
from app.db.league_split import split_database
from golden.golden import build_e2e_db


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("conn")
    e2e = build_e2e_db(d / "e2e.duckdb")
    out = split_database(e2e, d / "lg.duckdb", d / "gl.duckdb", stamp="2026-10-20T12:00:00")
    return out


def test_unqualified_names_resolve_league_first_then_global(files):
    dbs = LayeredDatabases()
    cur = dbs.cursor(files.league_path, files.global_path, perspective=None)
    assert cur.execute("SELECT count(*) FROM fantasy_teams").fetchone()[0] == 4          # league table
    assert cur.execute("SELECT count(*) FROM nfl_players").fetchone()[0] > 0             # global table
    assert cur.execute("SELECT value FROM schema_meta WHERE key='published_at'").fetchone() == ("2026-10-20T12:00:00",)


def test_the_perspective_team_is_the_only_is_mine_and_cursors_do_not_share_it(files):
    dbs = LayeredDatabases()
    a = dbs.cursor(files.league_path, files.global_path, perspective=2)
    b = dbs.cursor(files.league_path, files.global_path, perspective=3)
    none = dbs.cursor(files.league_path, files.global_path, perspective=None)
    mine = "SELECT team_id FROM fantasy_teams WHERE is_mine"
    assert a.execute(mine).fetchall() == [(2,)] and b.execute(mine).fetchall() == [(3,)]
    assert none.execute(mine).fetchall() == []
    assert len(dbs) == 1, "one instance per file pair"


def test_the_files_are_read_only_and_nothing_else_is_reachable(files, tmp_path):
    cur = LayeredDatabases().cursor(files.league_path, files.global_path, perspective=None)
    for sql in ("INSERT INTO lg.main.fantasy_teams SELECT * FROM lg.main.fantasy_teams",
                "CREATE TABLE lg.main.x (i INT)", f"ATTACH '{tmp_path / 'other.duckdb'}' AS other",
                f"SELECT * FROM read_csv('{tmp_path / 'x.csv'}')", "SET enable_external_access = true"):
        with pytest.raises(duckdb.Error):
            cur.execute(sql)


def test_dropping_an_instance_does_not_break_a_running_cursor(files):
    dbs = LayeredDatabases()
    cur = dbs.cursor(files.league_path, files.global_path, perspective=1)
    dbs.forget(files.league_path)
    assert len(dbs) == 0
    assert cur.execute("SELECT count(*) FROM fantasy_teams WHERE is_mine").fetchone() == (1,)


@pytest.mark.parametrize("raw,expected", [(None, None), ("6", 6), (6, 6), ("0", 0), ("abc", None), ("6; DROP", None),
                                          ("-1", None), ("99999999999", None), (True, None)])
def test_perspective_team_accepts_only_small_non_negative_integers(raw, expected):
    assert perspective_team(raw) == expected
```

```bash
cd fantasy-intel/backend && uv run pytest tests/test_league_conn.py -q
```
Expected: collection error (module missing)

- [ ] **Step 2: Implement**

`fantasy-intel/backend/app/db/league_conn.py`:

```python
"""Layered read-only connections for one league (multi-league Phase 2).

One in-memory DuckDB instance per (league file, global file) pair, with both attached READ_ONLY as `lg` and `gl`,
external access off and its configuration locked: nothing but those two files is readable, nothing is writable.
Each request gets its own cursor (a separate DuckDB connection to the instance) with
`search_path = lg.main, gl.main`, so every unqualified table name the engines use resolves to the league's copy
first and the shared global data second.

The perspective team reaches the engines through the column they already read: a cursor-local temporary view
`fantasy_teams` shadows the league table with `is_mine = (team_id = <perspective>)`, or `false` for every team when
the viewer has no perspective. No engine changes; another cursor never sees this one's view.
"""

from __future__ import annotations

import threading
from pathlib import Path

import duckdb

_MAX_TEAM_ID = 1_000_000


def perspective_team(raw) -> int | None:
    """The perspective as an integer team id, or None. Anything else (including injection attempts) is None: the
    perspective is a preference, never trusted input."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.isascii() and raw.isdigit() and len(raw) <= 7:
        value = int(raw)
    else:
        return None
    return value if 0 <= value < _MAX_TEAM_ID else None


def _q(path: Path) -> str:
    return str(path).replace("'", "''")


class LayeredDatabases:
    """The per-process set of open instances, keyed by (league path, global path)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._instances: dict[tuple[str, str], duckdb.DuckDBPyConnection] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._instances)

    def _instance(self, league_path: Path, global_path: Path) -> duckdb.DuckDBPyConnection:
        key = (str(league_path), str(global_path))
        with self._lock:
            inst = self._instances.get(key)
            if inst is None:
                inst = duckdb.connect(":memory:")
                inst.execute(f"ATTACH '{_q(league_path)}' AS lg (READ_ONLY)")
                inst.execute(f"ATTACH '{_q(global_path)}' AS gl (READ_ONLY)")
                inst.execute("SET enable_external_access = false")
                inst.execute("SET lock_configuration = true")
                self._instances[key] = inst
            return inst

    def cursor(self, league_path: Path, global_path: Path, *, perspective: int | None) -> duckdb.DuckDBPyConnection:
        cur = self._instance(league_path, global_path).cursor()
        cur.execute("SET search_path = 'lg.main,gl.main'")
        team = perspective_team(perspective)
        mine = "false" if team is None else f"(team_id = {team})"        # an int we produced: safe to inline
        cur.execute(f"CREATE TEMP VIEW fantasy_teams AS SELECT * REPLACE ({mine} AS is_mine) FROM lg.main.fantasy_teams")
        return cur

    def forget(self, path: Path) -> None:
        """Drop instances using `path` (the file cache evicted it). Cursors already handed out keep working: DuckDB
        keeps the instance alive until its last connection closes, then the file's space is freed."""
        p = str(path)
        with self._lock:
            for key in [k for k in self._instances if p in k]:
                del self._instances[key]
```

- [ ] **Step 3: Run the tests**

```bash
uv run pytest tests/test_league_conn.py -q
```
Expected: 13 passed

- [ ] **Step 4: Commit**

```bash
git add fantasy-intel/backend/app/db/league_conn.py fantasy-intel/backend/tests/test_league_conn.py
git commit -m "Phase 2: layered read-only league connection with the perspective as is_mine"
```


### Task 5: Serve every existing page for one league under /v2/leagues/{league_id}/api/...

**Files:**
- Create: `fantasy-intel/backend/app/api/league_scope.py`
- Create: `fantasy-intel/backend/app/api/routing.py`
- Create: `fantasy-intel/backend/tests/league_fixtures.py`
- Create: `fantasy-intel/backend/tests/test_league_scope.py`
- Modify: `fantasy-intel/backend/app/api/deps.py (`LeagueScope`, `LEAGUE_SCOPE`, `layered_databases()`, scope branch in `db()` and `data_version()`)`
- Modify: `fantasy-intel/backend/app/main.py (mount + `add_scope_headers` middleware)`
- Modify: `fantasy-intel/backend/tests/golden/golden.py (`legacy_get_routes` via `iter_routes`; mask whole diagnostic objects)`
- Modify: `fantasy-intel/backend/tests/golden/e2e/*.json (re-recorded: timings masked as objects)`
- Modify: `fantasy-intel/backend/tests/test_v2_authorization_matrix.py (shared enumerator; every /v2/leagues/{league_id} route runs league_tx or league_scope)`
- Modify: `fantasy-intel/backend/tests/test_league_scope_audit.py (league_scope.py is an allowed authorizer caller)`

**Interfaces:**
- Consumes: `authorize_league_in` (Phase 1), `LeagueFileCache` (Task 3), `LayeredDatabases` (Task 4), `perspective_team` (Task 4).
- Produces: `deps.LeagueScope(league_id, league_path, global_path, data_version, global_version, visibility_epoch, perspective)` with `.cache_key`; `deps.LEAGUE_SCOPE` (ContextVar); `league_scope.league_scope` (async dependency), `league_file_cache()`, `mount_legacy_routers(app, routers, require_enabled)`, `add_scope_headers` middleware, `LEAGUE_PREFIX`; `routing.iter_routes(app) -> RouteInfo(path, methods, route, include_dependencies, in_schema)` with `.dependency_calls()`.
- Perspective rule: "my team" (`is_mine`) = the signed-in viewer's saved team, else `?team_id=`, else none. This keeps the legacy owner-default semantics exactly (golden parity) and gives anonymous visitors their URL team everywhere.
- Errors: unknown/unauthorized 404 before any file is read; deleted account 401; no snapshot 409 `{detail, sync_state}`; control plane or file fetch down 503 with fixed text.

- [ ] **Step 1: Write the shared route enumerator**

`fantasy-intel/backend/app/api/routing.py`:

```python
"""Enumerate an app's HTTP routes with their full paths (FastAPI >= 0.140 keeps an included router's own paths and
records the include prefix and dependencies on a wrapper). Used by the golden harness and the authorization tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator


@dataclass(frozen=True)
class RouteInfo:
    path: str                 # full path, include prefixes applied
    methods: frozenset[str]
    route: Any                # the APIRoute
    include_dependencies: tuple  # dependencies added by include_router (outermost first)
    in_schema: bool

    def dependency_calls(self) -> set:
        """Every dependency callable this route runs: include-level, route-level and their sub-dependencies."""
        calls = {d.dependency for d in self.include_dependencies}
        stack = list(getattr(self.route, "dependant", None).dependencies) if hasattr(self.route, "dependant") else []
        while stack:
            dep = stack.pop()
            calls.add(dep.call)
            stack.extend(dep.dependencies)
        return calls


def iter_routes(app) -> Iterator[RouteInfo]:
    def walk(routes, prefix: str, deps: tuple, in_schema: bool):
        for r in routes:
            inner = getattr(r, "original_router", None)
            if inner is not None:
                ctx = r.include_context
                yield from walk(inner.routes, prefix + (ctx.prefix or ""), deps + tuple(ctx.dependencies or ()),
                                in_schema and ctx.include_in_schema is not False)
            elif hasattr(r, "methods") and r.methods:
                yield RouteInfo(prefix + r.path, frozenset(r.methods - {"HEAD", "OPTIONS"}), r, deps,
                                in_schema and getattr(r, "include_in_schema", True))
    yield from walk(app.routes, "", (), True)
```

- [ ] **Step 2: Write the test fixtures and the failing tests**

`fantasy-intel/backend/tests/test_league_scope.py`:

```python
"""Every existing page served for one league from its files: /v2/leagues/{lg}/api/... (Phase 2).

Golden parity (the E2E fixture league answers exactly the legacy goldens), authorization before any file is touched,
two leagues with colliding team and player ids never leak into each other, anonymous perspective from the URL."""
import shutil

import duckdb
import pytest
from fastapi.testclient import TestClient

from cp_seed import U1, U2, seed_membership, seed_user
from golden.golden import E2E_DIR, E2E_SEASON, Case, build_e2e_db, read_golden, response_text
from league_fixtures import publish_local
from test_leagues_api import bearer, env  # noqa: F401  (fixture)


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    return build_e2e_db(tmp_path_factory.mktemp("scope") / "e2e.duckdb")


MARK = "Zqleak"     # appears nowhere in the fixture league; B's team names carry it


@pytest.fixture(scope="module")
def e2e_b(tmp_path_factory, e2e):
    """League B: the same ids as A (colliding team and player ids), different names and scoring results."""
    b = tmp_path_factory.mktemp("scope_b") / "b.duckdb"
    shutil.copy(e2e, b)
    con = duckdb.connect(str(b))
    con.execute(f"UPDATE fantasy_teams SET name = '{MARK} ' || name")
    con.execute(f"UPDATE leagues SET name = 'League {MARK}'")
    con.execute("UPDATE espn_player_weekly_points SET actual_points = actual_points * 2")
    con.close()
    return b


def my_team(db) -> int:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute("SELECT team_id FROM fantasy_teams WHERE is_mine").fetchone()[0]
    finally:
        con.close()


@pytest.fixture
def served(env, pg, tmp_path, monkeypatch):   # noqa: F811
    """Fresh per-process caches pointed at a local object directory."""
    from app.api import deps, league_scope
    objects = tmp_path / "objects"
    monkeypatch.setenv("LEAGUE_FILES_DIR", str(objects))
    monkeypatch.setenv("ESPN_SEASON", E2E_SEASON)
    monkeypatch.setattr(league_scope, "_CACHE", {})
    monkeypatch.setattr(deps, "_LAYERED", {})
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    return objects


@pytest.fixture
def client(served):
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def test_the_fixture_league_matches_every_golden_through_v2(pg, served, client, e2e, tmp_path):
    """A signed-in member whose saved team is the legacy owner's team sees exactly the legacy answers."""
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="private")
    with pg.connect() as con:
        seed_user(con, U1)
        seed_membership(con, U1, pub.league_id, "admin")
        con.execute("INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, %s)",
                    [U1, pub.league_id, str(my_team(e2e))])
        con.commit()
    import difflib
    failures = []
    for golden in sorted(E2E_DIR.glob("*.json")):
        path, expected = read_golden(E2E_DIR, golden.stem)
        r = client.get(f"/v2/leagues/{pub.league_id}{path}", headers=bearer(U1))
        actual = response_text(_Fixed(r), Case(golden.stem, path))
        if actual != expected:
            diff = "\n".join(list(difflib.unified_diff(expected.splitlines(), actual.splitlines(), lineterm="", n=1))[:30])
            failures.append(f"{golden.stem}: {diff}")
    assert not failures, "\n\n".join(failures[:5]) + f"\n... {len(failures)} failing"


class _Fixed:
    """Adapts one already-made response to response_text()'s client.get()."""
    def __init__(self, response):
        self.response = response

    def get(self, _path):
        return self.response


def test_responses_are_private_and_name_the_versions_read(pg, served, client, e2e, tmp_path):
    pub = publish_local(pg, e2e, served, tmp_path / "w")
    r = client.get(f"/v2/leagues/{pub.league_id}/api/overview")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, no-store"
    assert (r.headers["x-fi-data-version"], r.headers["x-fi-global-version"]) == (pub.data_version, pub.global_version)


def test_an_anonymous_visitor_reads_a_public_league_from_any_perspective(pg, served, client, e2e, tmp_path):
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="public")
    for team in (1, 2, 3, 4):
        r = client.get(f"/v2/leagues/{pub.league_id}/api/overview?team_id={team}")
        assert r.status_code == 200, r.text
        assert r.json()["perspective"]["team_id"] == team
    # without a perspective the view has no "my team"; with ?team_id= that team is the visitor's default everywhere
    assert client.get(f"/v2/leagues/{pub.league_id}/api/overview").json()["perspective"] is None
    teams = client.get(f"/v2/leagues/{pub.league_id}/api/teams?team_id=3").json()["teams"]
    assert [t["team_id"] for t in teams if t["is_owner_default"]] == [3]


def test_private_leagues_and_unknown_ids_are_404_before_any_file_is_read(pg, served, client, e2e, tmp_path):
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="private")
    for lg in (pub.league_id, "lg_" + "z" * 24):
        r = client.get(f"/v2/leagues/{lg}/api/overview")
        assert (r.status_code, r.json()) == (404, {"detail": "League not found."})
    assert not list((tmp_path / "tmp" / "fantasy-leagues").glob("*")), "nothing may be downloaded for a 404"


def test_a_league_without_a_published_snapshot_is_first_sync_not_someone_elses_data(pg, served, client):
    from cp_seed import seed_job, seed_league
    with pg.connect() as con:
        lid = seed_league(con, visibility="public")
        seed_job(con, lid, status="running")
        con.commit()
    r = client.get(f"/v2/leagues/{lid}/api/overview")
    assert r.status_code == 409
    assert r.json() == {"detail": {"detail": "First sync in progress.", "sync_state": "first_sync_running"}}


def test_two_leagues_with_colliding_ids_never_leak(pg, served, client, e2e, e2e_b, tmp_path):
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="public", name="A")
    b = publish_local(pg, e2e_b, served, tmp_path / "b", visibility="private", name="B",
                      stamp="2026-10-20T13:00:00", external_id="1999")   # another ESPN league; same team/player ids
    with pg.connect() as con:
        seed_user(con, U1)                     # a real account, just not a member of B
        seed_user(con, U2)
        seed_membership(con, U2, b.league_id, "viewer")
        con.execute("INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, '2')",
                    [U2, b.league_id])
        con.commit()
    paths = sorted({read_golden(E2E_DIR, g.stem)[0] for g in E2E_DIR.glob("*.json")})
    alone = {p: client.get(f"/v2/leagues/{a.league_id}{p}").text for p in paths}
    for p in paths:                                   # interleave: B, then A, for every route
        rb = client.get(f"/v2/leagues/{b.league_id}{p}", headers=bearer(U2))
        assert rb.status_code == 200, (p, rb.text[:200])
        ra = client.get(f"/v2/leagues/{a.league_id}{p}")
        assert response_text(_Fixed(ra), Case("a", p)) == response_text(_Fixed(_Text(alone[p], ra.status_code)), Case("a", p)), p
        assert MARK not in ra.text, p
        assert MARK in rb.text or rb.json() == ra.json() or p.startswith("/api/research"), p
    # B is private: an anonymous caller, or a user who is not B's member, gets the same 404 as an unknown league
    assert client.get(f"/v2/leagues/{b.league_id}/api/overview").status_code == 404
    assert client.get(f"/v2/leagues/{b.league_id}/api/overview", headers=bearer(U1)).status_code == 404


class _Text:
    def __init__(self, text, status):
        self.text, self.status_code = text, status

    def json(self):
        import json
        return json.loads(self.text)
```

- [ ] **Step 3: Fixture helper**

`fantasy-intel/backend/tests/league_fixtures.py`:

```python
"""Register split league files in the test control plane and serve them from a local object directory."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from app.db.league_files import global_key, league_key
from app.db.league_split import split_database
from cp_seed import new_league_id, seed_league


@dataclass(frozen=True)
class Published:
    league_id: str
    data_version: str
    global_version: str


def publish_local(pg, src_db: Path, objects: Path, work: Path, *, visibility="public", name="League", checked_at="now()",
                  stamp="2026-10-20T12:00:00", league_id: str | None = None,
                  external_id: str | None = None) -> Published:
    """Split `src_db`, lay the two files out under `objects` with their real keys, register the league and point
    fi.league_snapshots at them (as the publisher does)."""
    work.mkdir(parents=True, exist_ok=True)
    out = split_database(src_db, work / "lg.duckdb", work / "gl.duckdb", stamp=stamp)
    with pg.connect() as con:
        lid = seed_league(con, league_id, visibility=visibility, name=name, checked_at=checked_at,
                          external_id=external_id or str(out.league_id))
        for key, path in ((league_key(lid, out.league_version), out.league_path),
                          (global_key(out.global_version), out.global_path)):
            dest = objects / key
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
        con.execute("INSERT INTO fi.league_snapshots (league_id, object_key, data_version, fence, global_version) "
                    "VALUES (%s, %s, %s, 1, %s)", [lid, league_key(lid, out.league_version), out.league_version,
                                                  out.global_version])
        con.commit()
    return Published(lid, out.league_version, out.global_version)
```

```bash
cd fantasy-intel/backend && uv run pytest tests/test_league_scope.py -q
```
Expected: 404s for every /v2/leagues/{lg}/api route (not mounted yet)

- [ ] **Step 4: Add the scope to deps.py: `LeagueScope`, `LEAGUE_SCOPE`, `layered_databases()`; in `db()` a scope yields `layered_databases().cursor(...)` after the schema-version check; `data_version()` returns `scope.cache_key`**

- [ ] **Step 5: Implement the dependency and the mount**

`fantasy-intel/backend/app/api/league_scope.py`:

```python
"""Serve the existing pages for one league: `/v2/leagues/{league_id}/api/...` (multi-league Phase 2).

Every legacy router is mounted a second time under that prefix with the `league_scope` dependency, which runs before
the handler and before any cache lookup:

  1. authorize the caller for the league in Postgres (app/control_plane/authz.py): unknown and unauthorized are 404;
  2. no published snapshot yet -> 409 "first sync in progress" (never another league's data);
  3. fetch the league's file and the global file it was built against into the per-instance cache (verified);
  4. pick the perspective ("my team"): the signed-in viewer's saved team, else `?team_id=`, else none;
  5. set the LEAGUE_SCOPE ContextVar, which `deps.db()` turns into a layered read-only cursor.

The handlers are unchanged. Responses carry `Cache-Control: private, no-store` and the versions actually read.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from ..config import get_settings
from .deps import LEAGUE_SCOPE, LeagueScope, layered_databases
from .leagues_v2 import NOT_FOUND, current_principal, valid_league_id

log = logging.getLogger(__name__)
FIRST_SYNC = "First sync in progress."
FILES_UNAVAILABLE = "This league's data could not be loaded. Try again shortly."
_CACHE: dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()


def league_file_cache():
    """One cache per process: R2 (read-only token) in production, LEAGUE_FILES_DIR locally."""
    from ..db.league_files import DEFAULT_MAX_BYTES, DirStore, LeagueFileCache, R2Store
    with _CACHE_LOCK:
        cache = _CACHE.get("files")
        if cache is None:
            settings = get_settings()
            files_dir = os.environ.get("LEAGUE_FILES_DIR", "").strip()
            store = DirStore(Path(files_dir)) if files_dir else R2Store(settings.r2_config())
            max_bytes = int(os.environ.get("LEAGUE_CACHE_MAX_BYTES") or DEFAULT_MAX_BYTES)
            cache = _CACHE["files"] = LeagueFileCache(
                store, Path(tempfile.gettempdir()) / "fantasy-leagues", max_bytes=max_bytes,
                on_evict=layered_databases().forget)
        return cache


def _resolve(principal, league_id: str, team_param: str | None) -> LeagueScope:
    from ..control_plane.authz import LeagueNotFound, authorize_league_in
    from ..control_plane.pg import AccountGone, ControlPlaneUnavailable, get_control_plane
    from ..db.league_conn import perspective_team
    from ..db.league_split import VERSION_RE
    from .leagues_v2 import SIGN_IN, UNAVAILABLE
    try:
        with get_control_plane().request_tx(principal) as con:
            access = authorize_league_in(con, principal, league_id)
    except LeagueNotFound:
        raise HTTPException(404, NOT_FOUND) from None
    except AccountGone:
        raise HTTPException(401, SIGN_IN) from None
    except ControlPlaneUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None
    if access.snapshot is None:
        raise HTTPException(409, {"detail": FIRST_SYNC, "sync_state": access.sync_state})
    snap = access.snapshot
    if not VERSION_RE.fullmatch(snap.data_version) or not VERSION_RE.fullmatch(snap.global_version):
        log.warning("league %s has a pointer in an unsupported format", league_id)
        raise HTTPException(503, FILES_UNAVAILABLE)
    cache = league_file_cache()
    try:
        league_path = cache.league_file(access.league_id, snap.data_version)
        global_path = cache.global_file(snap.global_version)
    except Exception as err:   # noqa: BLE001 - R2 errors, a missing object: never a 500 with details
        log.warning("league files unavailable for %s: %s", league_id, type(err).__name__)
        raise HTTPException(503, FILES_UNAVAILABLE) from None
    # "My team" (fantasy_teams.is_mine): the signed-in viewer's saved team, as the owner's team was on the legacy
    # site; an explicit ?team_id= stays an explicit choice the routes already handle. A viewer with no saved team
    # (anonymous, or not chosen yet) takes the URL's team as their perspective everywhere.
    chosen = perspective_team(access.perspective_team_id)
    if chosen is None and team_param is not None:
        chosen = perspective_team(team_param)
    return LeagueScope(access.league_id, league_path, global_path, snap.data_version, snap.global_version,
                       access.visibility_epoch, chosen)


async def league_scope(request: Request, league_id: str = Depends(valid_league_id),
                       principal=Depends(current_principal)):
    """Async on purpose: the ContextVar is set in the request's own context, which FastAPI copies into the thread
    pool that runs the (sync) legacy handler. The blocking work runs in the pool."""
    scope = await run_in_threadpool(_resolve, principal, league_id, request.query_params.get("team_id"))
    request.state.fi_scope = scope
    token = LEAGUE_SCOPE.set(scope)
    try:
        yield scope
    finally:
        LEAGUE_SCOPE.reset(token)


LEAGUE_PREFIX = "/v2/leagues/{league_id}"


def mount_legacy_routers(app, routers, require_enabled) -> None:
    """Mount each legacy router again under /v2/leagues/{league_id}. Not in the OpenAPI schema: the legacy entries
    already document the operations."""
    for r in routers:
        app.include_router(r, prefix=LEAGUE_PREFIX, include_in_schema=False,
                           dependencies=[Depends(require_enabled), Depends(league_scope, scope="function")])


async def add_scope_headers(request: Request, call_next):
    response = await call_next(request)
    scope = getattr(request.state, "fi_scope", None)
    if scope is not None:
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-FI-Data-Version"] = scope.data_version
        response.headers["X-FI-Global-Version"] = scope.global_version
    return response
```

- [ ] **Step 6: Mount in main.py after the legacy routers: `mount_legacy_routers(app, [<the 22 legacy routers>], require_enabled)` and `app.middleware("http")(add_scope_headers)`**

- [ ] **Step 7: Run the scope, golden, matrix and audit suites**

```bash
uv run pytest tests/test_league_scope.py tests/test_golden_legacy.py tests/test_v2_authorization_matrix.py tests/test_league_scope_audit.py tests/test_hosting.py -q
```
Expected: all pass; the 52 goldens are identical through /v2 for a member whose saved team is the legacy owner's team

- [ ] **Step 8: Commit**

```bash
git add -A fantasy-intel/backend
git commit -m "Phase 2: serve every existing page per league under /v2/leagues/{lg}/api"
```


### Task 6: Per-instance response cache, looked up only after authorization

**Files:**
- Modify: `fantasy-intel/backend/app/api/league_scope.py (`ResponseCache`, `response_cache()`, `CachedResponse`, `serve_cached`, lookup at the end of `league_scope`, store in `add_scope_headers`)`
- Modify: `fantasy-intel/backend/app/main.py (`app.add_exception_handler(CachedResponse, serve_cached)`)`
- Modify: `fantasy-intel/backend/tests/test_league_scope.py (three cache tests; the `served` fixture resets `_RESPONSES`)`

**Interfaces:**
- Key: `f"{scope.cache_key}|{path}|{sorted query}"`, where `scope.cache_key` = league, data version, global version, visibility epoch, perspective (spec §1). GET 200 only; bounded by `LEAGUE_RESPONSE_CACHE_BYTES` (default 32 MiB), one entry ≤ a quarter of it; LRU.
- Order: `league_scope` authorizes → resolves files → looks up the cache → on a hit raises `CachedResponse` (served by an exception handler without running the handler) → on a miss the middleware stores the body on the way out. Responses carry `X-FI-Cache: hit|miss`.

- [ ] **Step 1: Write the failing tests (appended to tests/test_league_scope.py)**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_league_scope.py -q -k "cache or authorization_runs_before"
```
Expected: FAIL: no response_cache

- [ ] **Step 2: Implement (final module)**

`fantasy-intel/backend/app/api/league_scope.py`:

```python
"""Serve the existing pages for one league: `/v2/leagues/{league_id}/api/...` (multi-league Phase 2).

Every legacy router is mounted a second time under that prefix with the `league_scope` dependency, which runs before
the handler and before any cache lookup:

  1. authorize the caller for the league in Postgres (app/control_plane/authz.py): unknown and unauthorized are 404;
  2. no published snapshot yet -> 409 "first sync in progress" (never another league's data);
  3. fetch the league's file and the global file it was built against into the per-instance cache (verified);
  4. pick the perspective ("my team"): the signed-in viewer's saved team, else `?team_id=`, else none;
  5. set the LEAGUE_SCOPE ContextVar, which `deps.db()` turns into a layered read-only cursor.

The handlers are unchanged. Responses carry `Cache-Control: private, no-store` and the versions actually read.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from ..config import get_settings
from .deps import LEAGUE_SCOPE, LeagueScope, layered_databases
from .leagues_v2 import NOT_FOUND, current_principal, valid_league_id

log = logging.getLogger(__name__)
FIRST_SYNC = "First sync in progress."
FILES_UNAVAILABLE = "This league's data could not be loaded. Try again shortly."
_CACHE: dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()


def league_file_cache():
    """One cache per process: R2 (read-only token) in production, LEAGUE_FILES_DIR locally."""
    from ..db.league_files import DEFAULT_MAX_BYTES, DirStore, LeagueFileCache, R2Store
    with _CACHE_LOCK:
        cache = _CACHE.get("files")
        if cache is None:
            settings = get_settings()
            files_dir = os.environ.get("LEAGUE_FILES_DIR", "").strip()
            store = DirStore(Path(files_dir)) if files_dir else R2Store(settings.r2_config())
            max_bytes = int(os.environ.get("LEAGUE_CACHE_MAX_BYTES") or DEFAULT_MAX_BYTES)
            cache = _CACHE["files"] = LeagueFileCache(
                store, Path(tempfile.gettempdir()) / "fantasy-leagues", max_bytes=max_bytes,
                on_evict=layered_databases().forget)
        return cache


def _resolve(principal, league_id: str, team_param: str | None) -> LeagueScope:
    from ..control_plane.authz import LeagueNotFound, authorize_league_in
    from ..control_plane.pg import AccountGone, ControlPlaneUnavailable, get_control_plane
    from ..db.league_conn import perspective_team
    from ..db.league_split import VERSION_RE
    from .leagues_v2 import SIGN_IN, UNAVAILABLE
    try:
        with get_control_plane().request_tx(principal) as con:
            access = authorize_league_in(con, principal, league_id)
    except LeagueNotFound:
        raise HTTPException(404, NOT_FOUND) from None
    except AccountGone:
        raise HTTPException(401, SIGN_IN) from None
    except ControlPlaneUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None
    if access.snapshot is None:
        raise HTTPException(409, {"detail": FIRST_SYNC, "sync_state": access.sync_state})
    snap = access.snapshot
    if not VERSION_RE.fullmatch(snap.data_version) or not VERSION_RE.fullmatch(snap.global_version):
        log.warning("league %s has a pointer in an unsupported format", league_id)
        raise HTTPException(503, FILES_UNAVAILABLE)
    cache = league_file_cache()
    try:
        league_path = cache.league_file(access.league_id, snap.data_version)
        global_path = cache.global_file(snap.global_version)
    except Exception as err:   # noqa: BLE001 - R2 errors, a missing object: never a 500 with details
        log.warning("league files unavailable for %s: %s", league_id, type(err).__name__)
        raise HTTPException(503, FILES_UNAVAILABLE) from None
    # "My team" (fantasy_teams.is_mine): the signed-in viewer's saved team, as the owner's team was on the legacy
    # site; an explicit ?team_id= stays an explicit choice the routes already handle. A viewer with no saved team
    # (anonymous, or not chosen yet) takes the URL's team as their perspective everywhere.
    chosen = perspective_team(access.perspective_team_id)
    if chosen is None and team_param is not None:
        chosen = perspective_team(team_param)
    return LeagueScope(access.league_id, league_path, global_path, snap.data_version, snap.global_version,
                       access.visibility_epoch, chosen)


# ---- per-instance response cache ------------------------------------------------------------------------------------

class ResponseCache:
    """Successful GET responses keyed by (scope cache key, path, sorted query): league, data and global versions,
    visibility epoch and perspective are all in the scope key, so two leagues, two versions or two perspectives never
    share an entry. Looked up only after authorization (inside `league_scope`). Bounded by bytes, LRU."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[int, bytes, str]] = OrderedDict()   # key -> (status, body, media type)
        self.hits = 0

    @property
    def bytes_used(self) -> int:
        with self._lock:
            return sum(len(body) for _, body, _ in self._entries.values())

    def statuses(self) -> list[int]:
        with self._lock:
            return [status for status, _, _ in self._entries.values()]

    def get(self, key: str):
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None:
                self._entries.move_to_end(key)
                self.hits += 1
            return hit

    def put(self, key: str, status: int, body: bytes, media_type: str) -> None:
        if status != 200 or len(body) > self.max_bytes // 4:          # one answer never takes most of the cache
            return
        with self._lock:
            self._entries[key] = (status, body, media_type)
            self._entries.move_to_end(key)
            used = sum(len(b) for _, b, _ in self._entries.values())
            while self._entries and used > self.max_bytes:
                _, (_, old, _) = self._entries.popitem(last=False)
                used -= len(old)


_RESPONSES: dict[str, ResponseCache] = {}


def response_cache() -> ResponseCache:
    with _CACHE_LOCK:
        cache = _RESPONSES.get("cache")
        if cache is None:
            cache = _RESPONSES["cache"] = ResponseCache(int(os.environ.get("LEAGUE_RESPONSE_CACHE_BYTES") or (32 << 20)))
        return cache


class CachedResponse(Exception):
    """Raised by league_scope on a cache hit (after authorization) to answer without running the handler."""

    def __init__(self, entry):
        self.entry = entry


async def serve_cached(request: Request, exc: CachedResponse) -> Response:
    status, body, media_type = exc.entry
    return Response(content=body, status_code=status, media_type=media_type, headers={"X-FI-Cache": "hit"})


def _response_key(scope: LeagueScope, request: Request) -> str | None:
    if request.method != "GET":
        return None
    query = "&".join(f"{k}={v}" for k, v in sorted(request.query_params.multi_items()))
    return f"{scope.cache_key}|{request.url.path}|{query}"


async def league_scope(request: Request, league_id: str = Depends(valid_league_id),
                       principal=Depends(current_principal)):
    """Async on purpose: the ContextVar is set in the request's own context, which FastAPI copies into the thread
    pool that runs the (sync) legacy handler. The blocking work runs in the pool."""
    scope = await run_in_threadpool(_resolve, principal, league_id, request.query_params.get("team_id"))
    request.state.fi_scope = scope
    key = _response_key(scope, request)
    if key is not None:
        hit = response_cache().get(key)
        if hit is not None:
            raise CachedResponse(hit)
        request.state.fi_cache_key = key
    token = LEAGUE_SCOPE.set(scope)
    try:
        yield scope
    finally:
        LEAGUE_SCOPE.reset(token)


LEAGUE_PREFIX = "/v2/leagues/{league_id}"


def mount_legacy_routers(app, routers, require_enabled) -> None:
    """Mount each legacy router again under /v2/leagues/{league_id}. Not in the OpenAPI schema: the legacy entries
    already document the operations."""
    for r in routers:
        app.include_router(r, prefix=LEAGUE_PREFIX, include_in_schema=False,
                           dependencies=[Depends(require_enabled), Depends(league_scope, scope="function")])


async def add_scope_headers(request: Request, call_next):
    """Private, uncached by browsers and proxies, naming the versions read; successful GETs go into the response
    cache on the way out."""
    response = await call_next(request)
    scope = getattr(request.state, "fi_scope", None)
    if scope is None:
        return response
    key = getattr(request.state, "fi_cache_key", None)
    if key is not None and response.status_code == 200 and "x-fi-cache" not in response.headers:
        body = b"".join([chunk async for chunk in response.body_iterator])
        response_cache().put(key, response.status_code, body, response.media_type or "application/json")
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        response = Response(content=body, status_code=response.status_code, headers=headers,
                            media_type=response.media_type)
        response.headers["X-FI-Cache"] = "miss"
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-FI-Data-Version"] = scope.data_version
    response.headers["X-FI-Global-Version"] = scope.global_version
    return response
```

- [ ] **Step 3: Run the suites**

```bash
uv run pytest tests/test_league_scope.py tests/test_golden_legacy.py tests/test_hosting.py tests/test_v2_authorization_matrix.py tests/test_league_scope_audit.py -q
```
Expected: all pass; interleaved A/B requests with identical route and params never share an entry; a league that turns private is 404 even with a cached public answer

- [ ] **Step 4: Commit**

```bash
git add -A fantasy-intel/backend
git commit -m "Phase 2: per-instance response cache keyed after authorization"
```


### Task 7: Publisher — upload immutable objects and move the fenced pointer

**Files:**
- Create: `fantasy-intel/backend/app/db/league_publish.py`
- Create: `fantasy-intel/backend/tests/test_league_publish.py`
- Create: `fantasy-intel/scripts/publish_leagues.py`
- Modify: `fantasy-intel/backend/app/db/league_split.py (`global_stamp=`: the global file is byte-identical for identical content)`
- Modify: `fantasy-intel/scripts/refresh_all.py (`PUBLISH_LEAGUES` after `PUBLISH_SNAPSHOT` when `LEAGUE_PUBLISH_ID` and `FI_SYNC_DB_URL` are set)`
- Modify: `fantasy-intel/backend/tests/test_refresh_all.py`
- Modify: `fantasy-intel/.env.example`
- Modify: `fantasy-intel/backend/tests/test_no_credential_leaks.py (`FI_SYNC_DB_URL` is scanned as a secret)`

**Interfaces:**
- Consumes: `split_database` (Task 2), `league_key`/`global_key` (Task 3), the Phase 1 `fi_sync` grants.
- Produces: `publish_league(src_db, writer, sync_con, league_id, *, stamp=None, keep=3) -> PublishResult(league_id, data_version, global_version, fence, applied, deleted)`; `R2Writer(cfg)`, `DirWriter(root)`; `GLOBAL_STAMP`.
- Pointer: `UPDATE fi.leagues SET sync_fence = sync_fence + 1 RETURNING` then the spec's fenced upsert (`... WHERE fi.league_snapshots.fence < EXCLUDED.fence RETURNING`); no row → `applied=False`, nothing visible changed, the object is collected. History keeps 3; objects not referenced by a pointer or history are deleted; the newest global object is always kept.
- Measured: identical global content splits to byte-identical files with a fixed internal stamp; the league file is not byte-deterministic (each publish is a new version anyway).

- [ ] **Step 1: Write the failing tests**

`fantasy-intel/backend/tests/test_league_publish.py`:

```python
"""Publishing a league: split, upload immutable objects, move the fenced pointer, keep three versions (Phase 2)."""
import shutil

import duckdb
import psycopg
import pytest

from app.db.league_files import global_key, league_key
from app.db.league_publish import R2Writer, publish_league
from cp_seed import seed_league
from fake_r2 import FakeR2
from golden.golden import build_e2e_db


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    return build_e2e_db(tmp_path_factory.mktemp("pub") / "e2e.duckdb")


@pytest.fixture
def r2():
    fake = FakeR2()
    yield fake
    fake.close()


@pytest.fixture
def league(pg):
    with pg.connect() as con:
        lid = seed_league(con, visibility="private", external_id="999")
        con.commit()
    return lid


def sync_con(pg):
    return psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None)


def pointer(pg, lid):
    with pg.connect() as con:
        return con.execute("SELECT object_key, data_version, fence, global_version FROM fi.league_snapshots "
                           "WHERE league_id = %s", [lid]).fetchone()


def touched(src, dst, n):
    shutil.copy(src, dst)
    con = duckdb.connect(str(dst))
    con.execute("UPDATE fantasy_teams SET name = name || ?", [f" v{n}"])
    con.close()
    return dst


def test_first_publish_uploads_both_files_and_inserts_the_pointer(pg, r2, league, e2e):
    with sync_con(pg) as con:
        res = publish_league(e2e, R2Writer(r2.config), con, league, stamp="2026-10-20T12:00:00")
    assert res.applied and res.fence == 1
    assert pointer(pg, league) == (league_key(league, res.data_version), res.data_version, 1, res.global_version)
    assert {league_key(league, res.data_version), global_key(res.global_version)} <= set(r2.objects)


def test_republishing_moves_the_pointer_and_keeps_three_versions(pg, r2, league, e2e, tmp_path):
    versions = []
    with sync_con(pg) as con:
        for n in range(5):
            res = publish_league(touched(e2e, tmp_path / f"v{n}.duckdb", n), R2Writer(r2.config), con, league,
                                 stamp=f"2026-10-20T12:0{n}:00")
            versions.append(res.data_version)
    assert pointer(pg, league)[1] == versions[-1] and pointer(pg, league)[2] == 5
    kept = sorted(k for k in r2.objects if k.startswith(f"leagues/{league}/"))
    assert kept == sorted(league_key(league, v) for v in versions[-3:])
    with pg.connect() as su:
        assert su.execute("SELECT count(*) FROM fi.league_snapshot_history WHERE league_id = %s", [league]).fetchone() == (3,)
    globals_ = [k for k in r2.objects if k.startswith("global/")]
    assert len(globals_) == 1, "unchanged global data is one object, uploaded once"


def test_a_superseded_publisher_changes_nothing_visible(pg, r2, league, e2e, tmp_path):
    with sync_con(pg) as con:
        first = publish_league(e2e, R2Writer(r2.config), con, league, stamp="2026-10-20T12:00:00")
    with pg.connect() as su:                 # a newer worker already published with a higher fence
        su.execute("UPDATE fi.league_snapshots SET fence = 99 WHERE league_id = %s", [league])
        su.execute("UPDATE fi.leagues SET sync_fence = 1 WHERE id = %s", [league])   # stale counter on this side
        su.commit()
    with sync_con(pg) as con:
        stale = publish_league(touched(e2e, tmp_path / "late.duckdb", 1), R2Writer(r2.config), con, league,
                               stamp="2026-10-20T12:05:00")
    assert not stale.applied
    assert pointer(pg, league)[1] == first.data_version
    assert league_key(league, stale.data_version) not in r2.objects, "the orphan is collected"


def test_the_publisher_role_cannot_touch_people_tables_and_unknown_leagues_are_refused(pg, r2, e2e):
    with sync_con(pg) as con:
        with pytest.raises(ValueError, match="not registered"):
            publish_league(e2e, R2Writer(r2.config), con, "lg_" + "q" * 24, stamp="2026-10-20T12:00:00")
    with sync_con(pg) as con, pytest.raises(psycopg.errors.InsufficientPrivilege):
        con.execute("SELECT count(*) FROM fi.league_memberships")
```

```bash
cd fantasy-intel/backend && uv run pytest tests/test_league_publish.py -q
```
Expected: collection error (module missing)

- [ ] **Step 2: Implement the publisher**

`fantasy-intel/backend/app/db/league_publish.py`:

```python
"""Publish one league's files and move its fenced pointer (multi-league Phase 2).

    split the analytics database -> upload global/<gv>.duckdb (skipped when that exact content is already there)
    -> upload leagues/<lg>/<dv>.duckdb -> one fi_sync transaction: take the next fence, fenced upsert of
    fi.league_snapshots, history row, keep three -> delete objects no pointer or history references.

The pointer moves only through the spec's fenced upsert (§3.4): a publisher holding an older fence than the one
already published changes nothing visible and its object is collected. Readers see the old pair of files or the new
pair, never a mix. Runs as `fi_sync` (never the service role); prints ids, versions and counts only.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import r2
from .connection import utcnow
from .league_files import global_key, league_key
from .league_split import SplitResult, split_database

KEEP = 3
GLOBAL_STAMP = "content-addressed"    # the global file's internal stamp: identical content, identical bytes

_UPSERT = """
INSERT INTO fi.league_snapshots (league_id, object_key, data_version, fence, global_version, committed_at)
VALUES (%s, %s, %s, %s, %s, now())
ON CONFLICT (league_id) DO UPDATE
  SET object_key = EXCLUDED.object_key, data_version = EXCLUDED.data_version, fence = EXCLUDED.fence,
      global_version = EXCLUDED.global_version, committed_at = EXCLUDED.committed_at
  WHERE fi.league_snapshots.fence < EXCLUDED.fence
RETURNING league_id
"""


class ObjectWriter(Protocol):
    def put(self, key: str, path: Path) -> None: ...
    def keys(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...


class R2Writer:
    """The bucket, with a read/write token (laptop or GitHub Actions only; the API's token is read-only)."""

    def __init__(self, cfg: r2.R2Config):
        self.cfg = cfg

    def put(self, key: str, path: Path) -> None:
        r2.put_file(self.cfg, key, path)

    def keys(self, prefix: str) -> list[str]:
        return r2.list_keys(self.cfg, prefix)

    def delete(self, key: str) -> None:
        r2.delete(self.cfg, key)


class DirWriter:
    """A local object directory (LEAGUE_FILES_DIR) with the same layout, for development."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def put(self, key: str, path: Path) -> None:
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(Path(path).read_bytes())
        tmp.replace(dest)

    def keys(self, prefix: str) -> list[str]:
        base = self.root / prefix
        return sorted(str(p.relative_to(self.root)) for p in base.rglob("*.duckdb")) if base.exists() else []

    def delete(self, key: str) -> None:
        (self.root / key).unlink(missing_ok=True)


@dataclass(frozen=True)
class PublishResult:
    league_id: str
    data_version: str
    global_version: str
    fence: int
    applied: bool          # False: a newer publish already holds the pointer; nothing visible changed
    deleted: tuple[str, ...]


def publish_league(src_db: Path, writer: ObjectWriter, sync_con, league_id: str, *, stamp: str | None = None,
                   keep: int = KEEP) -> PublishResult:
    """Publish `src_db` (one league's analytics database) as `league_id`. `sync_con` is a psycopg connection as
    fi_sync; this function commits on it."""
    stamp = stamp or utcnow().isoformat(timespec="seconds")
    row = sync_con.execute("SELECT id FROM fi.leagues WHERE id = %s AND deleted_at IS NULL", [league_id]).fetchone()
    sync_con.rollback()
    if row is None:
        raise ValueError(f"league {league_id} is not registered (or is deleted) in the control plane")
    with tempfile.TemporaryDirectory() as tmp:
        out = split_database(Path(src_db), Path(tmp) / "league.duckdb", Path(tmp) / "global.duckdb", stamp=stamp,
                             global_stamp=GLOBAL_STAMP)
        global_version = out.global_version
        content = global_version.split("-", 1)[1]                 # <sha256[:24]>-<bytes>
        existing = [k for k in writer.keys("global/") if k.endswith(f"-{content}.duckdb")]
        if existing:                                               # same global data already uploaded: reuse it
            global_version = sorted(existing)[0].removeprefix("global/").removesuffix(".duckdb")
        else:
            writer.put(global_key(global_version), out.global_path)
        lkey = league_key(league_id, out.league_version)
        writer.put(lkey, out.league_path)
        out = SplitResult(out.league_path, out.global_path, out.league_version, global_version, out.league_id,
                          out.season, out.schema_version)

    with sync_con.transaction():
        fence = sync_con.execute("UPDATE fi.leagues SET sync_fence = sync_fence + 1 WHERE id = %s RETURNING sync_fence",
                                 [league_id]).fetchone()[0]
        applied = sync_con.execute(_UPSERT, [league_id, lkey, out.league_version, fence, out.global_version]).fetchone()
        if applied is not None:
            sync_con.execute("INSERT INTO fi.league_snapshot_history (league_id, object_key, data_version, fence, "
                             "global_version, committed_at) VALUES (%s, %s, %s, %s, %s, now())",
                             [league_id, lkey, out.league_version, fence, out.global_version])
            sync_con.execute("""DELETE FROM fi.league_snapshot_history WHERE league_id = %s AND id NOT IN (
                                  SELECT id FROM fi.league_snapshot_history WHERE league_id = %s
                                  ORDER BY fence DESC, id DESC LIMIT %s)""", [league_id, league_id, keep])
    deleted = _collect(writer, sync_con, league_id)
    return PublishResult(league_id, out.league_version, out.global_version, fence, applied is not None, deleted)


def _collect(writer: ObjectWriter, sync_con, league_id: str) -> tuple[str, ...]:
    """Delete this league's objects that no pointer or history row references, and global objects no league
    references (the newest global object is always kept)."""
    with sync_con.transaction():
        league_refs = {r[0] for r in sync_con.execute(
            "SELECT object_key FROM fi.league_snapshots WHERE league_id = %s UNION "
            "SELECT object_key FROM fi.league_snapshot_history WHERE league_id = %s", [league_id, league_id])}
        global_refs = {global_key(r[0]) for r in sync_con.execute(
            "SELECT global_version FROM fi.league_snapshots UNION SELECT global_version FROM fi.league_snapshot_history")}
    deleted = []
    for key in writer.keys(f"leagues/{league_id}/"):
        if key not in league_refs:
            writer.delete(key)
            deleted.append(key)
    globals_ = sorted(writer.keys("global/"))
    for key in globals_[:-1]:
        if key not in global_refs:
            writer.delete(key)
            deleted.append(key)
    return tuple(deleted)
```

- [ ] **Step 3: CLI**

`fantasy-intel/scripts/publish_leagues.py`:

```python
"""Publish the local league as per-league files for the multi-league read path (Phase 2), after the syncs.

    cd backend && uv run python ../scripts/publish_leagues.py

Needs LEAGUE_PUBLISH_ID (the league's `lg_...` id in the control plane) and FI_SYNC_DB_URL (the fi_sync connection
string; never the service-role key). Objects go to R2 when SNAPSHOT_BUCKET/R2_* are set (read/write token), else to
LEAGUE_FILES_DIR. Prints ids, versions and counts only. The Phase 1a snapshot (publish_snapshot.py) keeps
publishing alongside until Phase 5 retires it.
"""
import os
import sys
from pathlib import Path

import _bootstrap  # noqa: F401

from app.config import get_settings
from app.db.league_publish import DirWriter, R2Writer, publish_league


def main() -> int:
    settings = get_settings()
    league_id = (os.getenv("LEAGUE_PUBLISH_ID") or "").strip()
    dsn = (os.getenv("FI_SYNC_DB_URL") or "").strip()
    if not league_id or not dsn:
        print("LEAGUE_PUBLISH_ID and FI_SYNC_DB_URL must both be set", file=sys.stderr)
        return 2
    files_dir = (os.getenv("LEAGUE_FILES_DIR") or "").strip()
    writer = R2Writer(settings.r2_config()) if settings.r2_configured else DirWriter(Path(files_dir)) if files_dir else None
    if writer is None:
        print("no object store: set the R2 settings or LEAGUE_FILES_DIR", file=sys.stderr)
        return 2
    import psycopg
    with psycopg.connect(dsn, prepare_threshold=None, connect_timeout=10) as con:
        res = publish_league(settings.db_path, writer, con, league_id)
    state = "published" if res.applied else "superseded by a newer publish (nothing changed)"
    print(f"{league_id}: {state}; data {res.data_version}; global {res.global_version}; fence {res.fence}; "
          f"collected {len(res.deleted)} old objects")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Hook into refresh_all: `PUBLISH_LEAGUES` joins `PUBLISHERS`; `plan(publish=True)` appends it after `PUBLISH_SNAPSHOT` only when both env vars are set (test in tests/test_refresh_all.py)**

- [ ] **Step 5: Run the tests**

```bash
uv run pytest tests/test_league_publish.py tests/test_league_split.py tests/test_refresh_all.py tests/test_no_credential_leaks.py -q
```
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add -A fantasy-intel
git commit -m "Phase 2: publish per-league files with a fenced pointer move"
```


### Task 8: Frontend — `/l/<lg>/<page>`, legacy redirect, scoped API calls, league switcher

**Files:**
- Create: `fantasy-intel/frontend/lib/league-scope.ts`
- Create: `fantasy-intel/frontend/lib/league-scope.test.ts`
- Create: `fantasy-intel/frontend/lib/league-scope.server.ts`
- Create: `fantasy-intel/frontend/lib/league-switcher.ts`
- Create: `fantasy-intel/frontend/lib/league-switcher.test.ts`
- Create: `fantasy-intel/frontend/components/useAppPathname.ts`
- Create: `fantasy-intel/frontend/components/LeagueSwitcher.tsx`
- Create: `fantasy-intel/frontend/components/LeagueSwitcher.module.css`
- Modify: `fantasy-intel/frontend/proxy.ts (apply `proxyDecision` after the site password)`
- Modify: `fantasy-intel/frontend/lib/api.ts (`scopedPath`: `/api/...` -> `/v2/leagues/<lg>/api/...` when the request carries `x-fi-league`)`
- Modify: `fantasy-intel/frontend/components/Nav.tsx, components/SyncPopover.tsx (`useAppPathname` instead of `usePathname`)`
- Modify: `fantasy-intel/frontend/components/useTeam.ts (per-league session key, explicit key in read/write)`
- Modify: `fantasy-intel/frontend/app/layout.tsx (`LeagueSwitcher` replaces the league chip when scoped)`
- Modify: `fantasy-intel/frontend/tsconfig.json (`allowImportingTsExtensions`: lib/api.ts is node-tested and imports a value from lib/league-scope.ts; `noEmit` is already on)`

**Interfaces:**
- Consumes: the backend mount `/v2/leagues/{lg}/api/...` (Task 5) and `GET /v2/leagues/{lg}` (Phase 1).
- Produces: `proxyDecision({enabled, pathname, search, cookie}) -> next | rewrite{path, league} | redirect{location} | clear{location}`, `splitLeaguePath`, `appPath`, `leaguePath`, `scopeApiPath`, `isLeagueId`, `LEAGUE_HEADER` (`x-fi-league`), `LEAGUE_COOKIE` (`fi_league`); `currentLeague() -> {id, name, committedAt} | null`; `useAppPathname()`, `useLeagueId()`; `rememberLeague`, `parseRecent`, `dataAsOf`.
- Behaviour with `MULTILEAGUE_UI_ENABLED=1`: `/l/<lg>/<page>` rewrites to `<page>` with the league header and sets the cookie; an unprefixed page with the cookie redirects into the league (every existing in-app link keeps working, one extra hop); `?league=clear` returns to the classic site; `/leagues`, `/api`, assets and Next internals are never redirected; a client-sent league header on an unscoped page is stripped. Flag unset: no change (E2E 228 passed / 16 skipped, same as main).
- Verified live (local stack: throwaway Postgres + the E2E fixture published as a public league + flags on): `/l/<lg>` renders the overview with "Fixture League · Data as of …"; `?team=2` + clicking Waivers lands on `/l/<lg>/waivers?team=2` with the tab highlighted; `?league=clear` returns to the classic site; an unknown league shows the backend's 404 "League not found". Noted for the Phase 5 parity audit: the no-default-team notice still mentions ESPN_MY_TEAM_ID / .env.

- [ ] **Step 1: Write the routing helpers' tests**

`fantasy-intel/frontend/lib/league-scope.test.ts`:

```ts
import assert from "node:assert/strict";
import test from "node:test";
import { appPath, leaguePath, proxyDecision, scopeApiPath, splitLeaguePath } from "./league-scope.ts";

const LG = "lg_" + "a".repeat(24);

test("splitLeaguePath recognises only our league ids", () => {
  assert.deepEqual(splitLeaguePath(`/l/${LG}/waivers`), { league: LG, path: "/waivers" });
  assert.deepEqual(splitLeaguePath(`/l/${LG}`), { league: LG, path: "/" });
  assert.deepEqual(splitLeaguePath("/l/lg_short/waivers"), { league: null, path: "/l/lg_short/waivers" });
  assert.deepEqual(splitLeaguePath("/waivers"), { league: null, path: "/waivers" });
  assert.equal(appPath(`/l/${LG}/players/12`), "/players/12");
});

test("leaguePath and scopeApiPath", () => {
  assert.equal(leaguePath(LG, "/"), `/l/${LG}`);
  assert.equal(leaguePath(LG, "/trades"), `/l/${LG}/trades`);
  assert.equal(scopeApiPath("/api/overview?team_id=2", LG), `/v2/leagues/${LG}/api/overview?team_id=2`);
  assert.equal(scopeApiPath("/api/overview", null), "/api/overview");
  assert.equal(scopeApiPath("/v2/providers", LG), "/v2/providers");
  assert.equal(scopeApiPath("/api/overview", "lg_bad"), "/api/overview");
});

test("with the flag off nothing changes", () => {
  assert.deepEqual(proxyDecision({ enabled: false, pathname: `/l/${LG}/waivers`, search: "", cookie: LG }), { kind: "next" });
  assert.deepEqual(proxyDecision({ enabled: false, pathname: "/waivers", search: "", cookie: LG }), { kind: "next" });
});

test("league URLs rewrite to the page with the league in a header", () => {
  assert.deepEqual(proxyDecision({ enabled: true, pathname: `/l/${LG}/waivers`, search: "?team=2" }),
    { kind: "rewrite", path: "/waivers?team=2", league: LG });
  assert.deepEqual(proxyDecision({ enabled: true, pathname: `/l/${LG}`, search: "" }), { kind: "rewrite", path: "/", league: LG });
  assert.deepEqual(proxyDecision({ enabled: true, pathname: "/l/lg_bad/waivers", search: "" }), { kind: "next" });
});

test("old URLs go to the last-viewed league, and ?league=clear returns to the classic site", () => {
  assert.deepEqual(proxyDecision({ enabled: true, pathname: "/trades", search: "?a=1", cookie: LG }),
    { kind: "redirect", location: `/l/${LG}/trades?a=1` });
  assert.deepEqual(proxyDecision({ enabled: true, pathname: "/", search: "", cookie: LG }),
    { kind: "redirect", location: `/l/${LG}` });
  assert.deepEqual(proxyDecision({ enabled: true, pathname: "/trades", search: "", cookie: null }), { kind: "next" });
  assert.deepEqual(proxyDecision({ enabled: true, pathname: "/trades", search: "?league=clear&x=1", cookie: LG }),
    { kind: "clear", location: "/trades?x=1" });
  assert.deepEqual(proxyDecision({ enabled: true, pathname: "/trades", search: "", cookie: "lg_bad" }), { kind: "next" });
});

test("assets, Next internals, route handlers and the connections page are never redirected", () => {
  for (const p of ["/_next/static/x.js", "/api/thing", "/leagues", "/leagues/x", "/favicon.ico", "/icon.svg", "/apple-icon", "/robots.txt"]) {
    assert.deepEqual(proxyDecision({ enabled: true, pathname: p, search: "", cookie: LG }), { kind: "next" }, p);
  }
});
```

```bash
cd fantasy-intel/frontend && node --experimental-strip-types --no-warnings=ExperimentalWarning --test lib/league-scope.test.ts
```
Expected: fails: module missing

- [ ] **Step 2: Implement the routing helpers**

`fantasy-intel/frontend/lib/league-scope.ts`:

```ts
// League scope for the multi-league site (Phase 2): `/l/<lg_id>/<page>` serves the existing page for that league.
// Pure functions only (no Next imports) so node --test covers them; proxy.ts and lib/api.ts apply them.
//
//   /l/<lg>/<page>?q   -> rewrite to /<page>?q, header x-fi-league: <lg>, cookie fi_league=<lg> (last viewed)
//   /<page>?q + cookie -> redirect to /l/<cookie>/<page>?q   ("old URLs go to the viewer's last-viewed league")
//   /<page>?league=clear -> clear the cookie, serve the classic (unscoped) site
// Everything else (assets, Next internals, the /leagues connections page) passes through untouched.

export const LEAGUE_HEADER = "x-fi-league";
export const LEAGUE_COOKIE = "fi_league";
export const CLEAR_PARAM = "league";
const LEAGUE_ID = /^lg_[A-Za-z0-9_-]{16,64}$/;
const UNSCOPED_PREFIXES = ["/_next", "/api", "/leagues", "/favicon", "/icon", "/apple-icon", "/robots", "/sitemap"];

export function isLeagueId(value: unknown): value is string {
  return typeof value === "string" && LEAGUE_ID.test(value);
}

/** `/l/<lg>/rest` -> { league: lg, path: "/rest" }; anything else -> { league: null, path }. */
export function splitLeaguePath(pathname: string): { league: string | null; path: string } {
  const m = /^\/l\/([^/]+)(\/.*)?$/.exec(pathname);
  if (!m || !isLeagueId(m[1])) return { league: null, path: pathname };
  return { league: m[1], path: m[2] || "/" };
}

/** The app path without a league prefix (for nav highlighting and other path matching). */
export function appPath(pathname: string): string {
  return splitLeaguePath(pathname).path;
}

export function leaguePath(league: string, path: string): string {
  const p = path.startsWith("/") ? path : `/${path}`;
  return `/l/${league}${p === "/" ? "" : p}`;
}

/** Prefix a backend path with the league's /v2 mount. Only the legacy /api routes are league-scoped. */
export function scopeApiPath(path: string, league: string | null | undefined): string {
  if (!league || !isLeagueId(league) || !path.startsWith("/api/")) return path;
  return `/v2/leagues/${league}${path}`;
}

function unscoped(path: string): boolean {
  return UNSCOPED_PREFIXES.some((p) => path === p || path.startsWith(`${p}/`) || path.startsWith(`${p}.`))
    || /\.[A-Za-z0-9]{2,5}$/.test(path);
}

export type ProxyDecision =
  | { kind: "next" }
  | { kind: "rewrite"; path: string; league: string }
  | { kind: "redirect"; location: string }
  | { kind: "clear"; location: string };

export function proxyDecision(args: { enabled: boolean; pathname: string; search: string; cookie?: string | null })
  : ProxyDecision {
  const { enabled, pathname, search, cookie } = args;
  if (!enabled) return { kind: "next" };
  const { league, path } = splitLeaguePath(pathname);
  if (league) return { kind: "rewrite", path: `${path}${search}`, league };
  if (pathname.startsWith("/l/") || pathname === "/l") return { kind: "next" };   // malformed id: the normal 404
  if (unscoped(pathname)) return { kind: "next" };
  const params = new URLSearchParams(search);
  if (params.get(CLEAR_PARAM) === "clear") {
    params.delete(CLEAR_PARAM);
    const rest = params.toString();
    return { kind: "clear", location: `${pathname}${rest ? `?${rest}` : ""}` };
  }
  if (isLeagueId(cookie)) return { kind: "redirect", location: `${leaguePath(cookie, pathname)}${search}` };
  return { kind: "next" };
}
```

- [ ] **Step 3: Apply them in the proxy**

`fantasy-intel/frontend/proxy.ts`:

```ts
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { LEAGUE_COOKIE, LEAGUE_HEADER, proxyDecision } from "./lib/league-scope";
import { basicAuthOk } from "./lib/site-auth";

// 1. Hosted mode: when SITE_PASSWORD is set, every page asks for it (browser Basic auth prompt). Locally unset: no-op.
// 2. Multi-league (MULTILEAGUE_UI_ENABLED=1): /l/<lg>/<page> serves <page> for that league (lib/league-scope.ts).
//    With the flag unset this does nothing, and the site behaves exactly as before.
const COOKIE_MAX_AGE = 60 * 60 * 24 * 180;

export function proxy(request: NextRequest) {
  const password = process.env.SITE_PASSWORD;
  if (password && !basicAuthOk(request.headers.get("authorization"), password)) {
    return new NextResponse("Password required", {
      status: 401,
      headers: { "WWW-Authenticate": 'Basic realm="fantasy-intel", charset="UTF-8"' },
    });
  }
  const decision = proxyDecision({
    enabled: process.env.MULTILEAGUE_UI_ENABLED === "1",
    pathname: request.nextUrl.pathname,
    search: request.nextUrl.search,
    cookie: request.cookies.get(LEAGUE_COOKIE)?.value ?? null,
  });
  switch (decision.kind) {
    case "rewrite": {
      const headers = new Headers(request.headers);
      headers.set(LEAGUE_HEADER, decision.league);
      const response = NextResponse.rewrite(new URL(decision.path, request.url), { request: { headers } });
      response.cookies.set(LEAGUE_COOKIE, decision.league, { path: "/", sameSite: "lax", maxAge: COOKIE_MAX_AGE, httpOnly: true });
      return response;
    }
    case "redirect":
      return NextResponse.redirect(new URL(decision.location, request.url), 307);
    case "clear": {
      const response = NextResponse.redirect(new URL(decision.location, request.url), 307);
      response.cookies.delete(LEAGUE_COOKIE);
      return response;
    }
    default: {
      // Never let a client send its own league header to an unscoped page.
      if (!request.headers.has(LEAGUE_HEADER)) return NextResponse.next();
      const headers = new Headers(request.headers);
      headers.delete(LEAGUE_HEADER);
      return NextResponse.next({ request: { headers } });
    }
  }
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
```

- [ ] **Step 4: Scope API calls in lib/api.ts: `path = await scopedPath(path)` at the top of `api()` and `apiRequest()`; `requestLeague()` reads the header via a dynamic `next/headers` import (null outside a request)**

- [ ] **Step 5: Strip the prefix in client path matching**

`fantasy-intel/frontend/components/useAppPathname.ts`:

```ts
"use client";

import { usePathname } from "next/navigation";
import { appPath, splitLeaguePath } from "@/lib/league-scope";

/** The page path without the `/l/<lg>` league prefix: what nav highlighting and page matching compare against. */
export function useAppPathname(): string {
  return appPath(usePathname());
}

/** The league the current page is scoped to (`/l/<lg>/...`), or null on the classic site. */
export function useLeagueId(): string | null {
  return splitLeaguePath(usePathname()).league;
}
```

- [ ] **Step 6: Per-league team memory**

`fantasy-intel/frontend/components/useTeam.ts`:

```ts
"use client";

import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useSyncExternalStore } from "react";
import { isTeamScoped, navTeam, parseStoredTeam, teamParamFrom } from "@/lib/team-perspective";
import { useAppPathname, useLeagueId } from "./useAppPathname";

// The last team analyzed on a team-scoped page, for this browser tab only (sessionStorage): a new tab or a shared
// link starts from the URL alone. Storage can be blocked; links then carry only the team in the current URL.
// Per league: team 2 of one league is not team 2 of another. The classic (unscoped) site keeps the original key.
const BASE_KEY = "fantasy-intel:team";
const listeners = new Set<() => void>();
const subscribe = (cb: () => void) => {
  listeners.add(cb);
  return () => { listeners.delete(cb); };
};

export function teamStorageKey(league: string | null): string {
  return league ? `${BASE_KEY}:${league}` : BASE_KEY;
}

function read(key: string): string | null {
  try {
    return parseStoredTeam(window.sessionStorage.getItem(key));
  } catch {
    return null;
  }
}

function write(key: string, team: string | null) {
  if (read(key) === team) return;
  try {
    if (team === null) window.sessionStorage.removeItem(key);
    else window.sessionStorage.setItem(key, team);
  } catch {
    return;
  }
  listeners.forEach((l) => l());
}

/**
 * The analyzed team that links from this page should keep (`withTeam`): the URL's `?team=` on a team-scoped page,
 * else the team last analyzed in this tab (in this league). Visiting a team-scoped page records its selection,
 * including the default (no `team`), so "Back to default" sticks.
 */
export function useTeam(): string | null {
  const key = teamStorageKey(useLeagueId());
  const pathname = useAppPathname();
  const urlTeam = teamParamFrom(useSearchParams());
  const scoped = isTeamScoped(pathname);
  const getSnapshot = useCallback(() => read(key), [key]);
  const last = useSyncExternalStore(subscribe, getSnapshot, () => null);
  useEffect(() => {
    if (scoped) write(key, urlTeam);
  }, [key, scoped, urlTeam]);
  return navTeam(pathname, urlTeam, last);
}
```

- [ ] **Step 7: Switcher helpers and tests**

`fantasy-intel/frontend/lib/league-switcher.ts`:

```ts
// The league switcher's recent-leagues list (local storage) and its labels. Pure, so node --test covers it.
import { isLeagueId } from "./league-scope.ts";

export type RecentLeague = { id: string; name: string };
export const RECENT_KEY = "fantasy-intel:leagues";
export const MAX_RECENT = 8;

/** Parse stored JSON defensively: anything malformed is dropped, never trusted. */
export function parseRecent(raw: string | null): RecentLeague[] {
  if (!raw) return [];
  try {
    const value = JSON.parse(raw);
    if (!Array.isArray(value)) return [];
    return value.filter((x): x is RecentLeague => !!x && isLeagueId(x.id) && typeof x.name === "string")
      .map((x) => ({ id: x.id, name: x.name.slice(0, 80) })).slice(0, MAX_RECENT);
  } catch {
    return [];
  }
}

/** Most recent first, no duplicates, bounded. */
export function rememberLeague(list: RecentLeague[], entry: RecentLeague): RecentLeague[] {
  if (!isLeagueId(entry.id)) return list;
  return [{ id: entry.id, name: entry.name.slice(0, 80) }, ...list.filter((x) => x.id !== entry.id)].slice(0, MAX_RECENT);
}

/** "Data as of Oct 20, 12:00" from the served version's commit time (ISO), or null. */
export function dataAsOf(committedAt: string | null | undefined, timeZone?: string): string | null {
  if (!committedAt) return null;
  const d = new Date(committedAt);
  if (Number.isNaN(d.getTime())) return null;
  return `Data as of ${d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone })}`;
}
```

- [ ] **Step 8: Switcher component**

`fantasy-intel/frontend/components/LeagueSwitcher.tsx`:

```ts
"use client";

import Link from "next/link";
import { useEffect, useSyncExternalStore } from "react";
import { leaguePath } from "@/lib/league-scope";
import { RECENT_KEY, dataAsOf, parseRecent, rememberLeague, type RecentLeague } from "@/lib/league-switcher";
import type { ScopedLeague } from "@/lib/league-scope.server";
import styles from "./LeagueSwitcher.module.css";

// Recently viewed leagues live in localStorage (per browser); storage can be blocked, then the menu lists only the
// current league's escape links.
const listeners = new Set<() => void>();
const subscribe = (cb: () => void) => {
  listeners.add(cb);
  return () => { listeners.delete(cb); };
};
let cachedRaw: string | null = null;
let cached: RecentLeague[] = [];
function readRecent(): RecentLeague[] {
  try {
    const raw = window.localStorage.getItem(RECENT_KEY);
    if (raw !== cachedRaw) {            // a stable snapshot while storage is unchanged
      cachedRaw = raw;
      cached = parseRecent(raw);
    }
    return cached;
  } catch {
    return cached;
  }
}
const EMPTY: RecentLeague[] = [];

/** Which league the site is showing, when its data is from, and the leagues recently viewed in this browser. */
export function LeagueSwitcher({ current }: { current: ScopedLeague }) {
  const recent = useSyncExternalStore(subscribe, readRecent, () => EMPTY);
  useEffect(() => {
    try {
      const next = rememberLeague(parseRecent(window.localStorage.getItem(RECENT_KEY)), { id: current.id, name: current.name });
      window.localStorage.setItem(RECENT_KEY, JSON.stringify(next));
    } catch {
      return;
    }
    listeners.forEach((l) => l());
  }, [current.id, current.name]);
  const asOf = dataAsOf(current.committedAt);
  return (
    <details className={styles.switcher}>
      <summary className={styles.summary} title={asOf ?? undefined}>
        <span className={styles.name}>{current.name}</span>
        {asOf ? <span className={styles.asof}>{asOf}</span> : null}
      </summary>
      <div className={styles.menu} role="menu">
        {recent.filter((l) => l.id !== current.id).map((l) => (
          <Link key={l.id} role="menuitem" href={leaguePath(l.id, "/")} className={styles.item}>{l.name}</Link>
        ))}
        <Link role="menuitem" href="/leagues" className={styles.item}>Leagues…</Link>
        {/* prefetch off: following this link clears the league cookie, which a prefetch must not do */}
        <Link role="menuitem" href="/?league=clear" prefetch={false} className={styles.item}>Classic site</Link>
      </div>
    </details>
  );
}
```

- [ ] **Step 9: Server helper for the layout**

`fantasy-intel/frontend/lib/league-scope.server.ts`:

```ts
import "server-only";
import { headers } from "next/headers";
import { apiRequest } from "./api";
import { LEAGUE_HEADER, isLeagueId } from "./league-scope";

export type ScopedLeague = { id: string; name: string; committedAt: string | null };

/** The league this page is scoped to (proxy.ts), with its name and served data time, or null on the classic site. */
export async function currentLeague(): Promise<ScopedLeague | null> {
  const id = (await headers()).get(LEAGUE_HEADER);
  if (!isLeagueId(id)) return null;
  const res = await apiRequest<{ name: string | null; data: { committed_at: string } | null }>(`/v2/leagues/${id}`);
  if (!res.ok || !res.data) return { id, name: res.ok || res.status !== 404 ? "League" : "League not found", committedAt: null };
  return { id, name: res.data.name || "League", committedAt: res.data.data?.committed_at ?? null };
}
```

- [ ] **Step 10: Verify**

```bash
npm run lint && npm run typecheck && npm test && npm run build && npm run e2e
```
Expected: clean; 557 unit tests; E2E 228 passed, 16 skipped

- [ ] **Step 11: Commit**

```bash
git add -A fantasy-intel/frontend
git commit -m "Phase 2: /l/<lg> league URLs, legacy redirect, scoped API calls, league switcher"
```


### Task 9: Los Subies migrated — real-data golden check, registration, docs

**Files:**
- Create: `fantasy-intel/scripts/golden_check.py`
- Create: `fantasy-intel/supabase/runbook-los-subies.sql`
- Modify: `fantasy-intel/backend/app/db/league_split.py (nflverse raw tables global; projection_range_quantiles league)`
- Modify: `fantasy-intel/backend/tests/test_league_split.py (every NFL-sync table is global; every schema.sql table is classified)`
- Modify: `fantasy-intel/README.md`
- Modify: `fantasy-intel/supabase/README.md`
- Modify: `docs/multileague/ADR-0002-supabase-control-plane.md`

**Interfaces:**
- `scripts/golden_check.py [db]`: copies the database, throwaway Postgres, publishes it as a private league, a member whose saved team is the database's `is_mine` team, compares every legacy GET case legacy vs `/v2/leagues/{lg}`; prints counts and case names only; exit 0 when all match.
- Result on real Los Subies (2026-10-20): 52 cases compared; 52 identical; 0 differ. The first run stopped on 14 tables the fixture does not have (raw nflverse datasets the NFL sync creates on the fly, and projection_range_quantiles) — the splitter refuses unclassified tables by design; they are now classified and guarded by tests.
- Los Subies is registered in project `fantasy-intel` as `lg_KxH_Y1Y56aM7qnCCseKvs1sU` (private, no members, no pointer until the first `publish_leagues.py` run, which needs Havyn's `FI_SYNC_DB_URL`).

- [ ] **Step 1: Write the check**

`fantasy-intel/scripts/golden_check.py`:

```python
"""Local check that a real league answers identically through the legacy path and the per-league path (Phase 2).

    cd backend && uv run python ../scripts/golden_check.py [path/to/fantasy.duckdb]

Copies the database (default: FANTASY_DB_PATH), starts a throwaway Postgres with the control-plane migrations,
publishes the copy as a private league into a temporary object directory, makes a test user a member whose saved
team is the database's own team (`is_mine`), then requests every legacy GET case both ways and compares them with
the same masking as the committed goldens. Prints counts and the names of differing cases, never response bodies
(the data is private). Needs `initdb`/`pg_ctl` (brew install postgresql). Exit 0 when every case matches.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401

sys.path.insert(0, str(_bootstrap.BACKEND / "tests"))


def main() -> int:
    from app.config import get_settings
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else get_settings().db_path
    if not src.exists():
        print(f"no database at {src}", file=sys.stderr)
        return 2
    import duckdb
    from fastapi.testclient import TestClient

    from cp_seed import U1, seed_membership, seed_user
    from golden.golden import cases, response_text
    from league_fixtures import publish_local
    from pg_harness import PgCluster, PgDatabase
    from test_leagues_api import URL, _jwks_client, bearer

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = tmp / "league.duckdb"
        shutil.copyfile(src, db)                       # never hold a lock on the live file
        con = duckdb.connect(str(db), read_only=True)
        mine = con.execute("SELECT team_id FROM fantasy_teams WHERE is_mine").fetchone()
        season = con.execute("SELECT season FROM leagues").fetchone()[0]
        con.close()
        cluster = PgCluster()
        cluster.start()
        try:
            pg = PgDatabase(cluster, name="fi_golden")
            pg.wait_ready()
            pg.create()
            pub = publish_local(pg, db, tmp / "objects", tmp / "work", visibility="private", name="League")
            with pg.connect() as c:
                seed_user(c, U1)
                seed_membership(c, U1, pub.league_id, "admin")
                if mine:
                    c.execute("INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, %s)",
                              [U1, pub.league_id, str(mine[0])])
                c.commit()
            os.environ.update(FANTASY_DB_PATH=str(db), ESPN_SEASON=str(season), FANTASY_API_KEY="",
                              MULTILEAGUE_ENABLED="1", SUPABASE_URL=URL, LEAGUE_FILES_DIR=str(tmp / "objects"),
                              SNAPSHOT_BUCKET="", SERVE_SNAPSHOT="")
            os.environ["SUPABASE_DB_URL"] = pg.dsn_for("fi_api")       # the throwaway cluster's fi_api role
            import app.control_plane.jwt_auth as jwt_auth
            client_jwks = _jwks_client()
            jwt_auth.jwks_client = lambda url: client_jwks
            import tempfile as tf
            tf.tempdir = str(tmp / "cache")
            (tmp / "cache").mkdir()
            from app.main import app
            client = TestClient(app, raise_server_exceptions=False)
            con = duckdb.connect(str(db), read_only=True)
            try:
                cs = cases(app, con, client)
            finally:
                con.close()
            headers = bearer(U1)

            class Scoped:
                def get(self, path):
                    return client.get(f"/v2/leagues/{pub.league_id}{path}", headers=headers)

            differ = [c.name for c in cs if response_text(client, c) != response_text(Scoped(), c)]
        finally:
            cluster.stop()
    print(f"{len(cs)} cases compared; {len(cs) - len(differ)} identical; {len(differ)} differ")
    for name in differ:
        print(f"  differs: {name}")
    return 1 if differ else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run it on the real database**

```bash
cd fantasy-intel/backend && uv run python ../scripts/golden_check.py
```
Expected: 52 cases compared; 52 identical; 0 differ

- [ ] **Step 3: Register Los Subies (Supabase connector, project owner)**

`fantasy-intel/supabase/runbook-los-subies.sql`:

```sql
-- One-time registration of Los Subies in the control plane (multi-league Phase 2). Applied 2026-10-20 through the
-- Supabase connector as the project owner. Private, with no members: invisible to everyone until Havyn signs in and
-- is made its admin (Phase 3), and it has no snapshot pointer until the first scripts/publish_leagues.py run.
-- Idempotent: the unique (provider, external_id, season) key makes a second run a no-op.
INSERT INTO fi.leagues (id, provider, external_id, season, name, visibility, status)
VALUES ('lg_KxH_Y1Y56aM7qnCCseKvs1sU', 'espn', '854268537', 2026, 'Los Subies', 'private', 'active')
ON CONFLICT (provider, external_id, season) DO NOTHING;

-- After Havyn's first sign-in (Phase 3), make the account the league's admin with its own team as perspective:
--   INSERT INTO fi.app_users (id) VALUES ('<auth.users id>') ON CONFLICT DO NOTHING;
--   INSERT INTO fi.league_memberships (user_id, league_id, role) VALUES ('<auth.users id>', 'lg_KxH_Y1Y56aM7qnCCseKvs1sU', 'admin');
--   INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES ('<auth.users id>', 'lg_KxH_Y1Y56aM7qnCCseKvs1sU', '<team id>');
```

- [ ] **Step 4: Docs and commit**

```bash
git add -A fantasy-intel docs
git commit -m "Phase 2: Los Subies golden-identical through the per-league path; registration; docs"
```


### Task 10: Prove acceptance, full suite + E2E, PR

| Criterion (master plan, Phase 2) | Evidence |
|---|---|
| Golden tests: every legacy GET endpoint byte-identical (volatile masked) via legacy URLs and `/v2/leagues/{lg}/…`, on the E2E fixture in CI | `tests/test_golden_legacy.py` (52 committed goldens), `tests/test_league_scope.py::test_the_fixture_league_matches_every_golden_through_v2` |
| … and on real Los Subies locally | `scripts/golden_check.py`: 52 compared, 52 identical, 0 differ (2026-10-20) |
| Two-league leak tests green for every route; interleaved-request cache test green | `test_two_leagues_with_colliding_ids_never_leak` (every golden route, B then A, colliding team and player ids); `test_responses_are_cached_per_league_version_and_perspective` |
| Anonymous visitor views a public fixture league from any perspective without signing in | `test_an_anonymous_visitor_reads_a_public_league_from_any_perspective`; live browser check (Task 8) |
| `/tmp` LRU never exceeds its bound under a 20-league mix; concurrent first requests download once | `tests/test_league_files.py` |
| `test_league_scope_audit.py` ratchet unchanged or lower | unchanged pins (no new `ORDER BY synced_at DESC`); the authorizer-caller pin adds `api/league_scope.py` on purpose |
| Flags off: production as Phase 1a left it | E2E 228 passed / 16 skipped (same as main); `/v2` 404 with the flag off; the snapshot publish keeps running |

- [ ] **Step 1:** `cd fantasy-intel/backend && uv run pytest -q` (full suite) and `cd ../frontend && npm run lint && npm run typecheck && npm test && npm run build && npm run e2e`.
- [ ] **Step 2:** Push `h/multileague-phase-2`, open the PR with the table above, wait for Codex's review (or `/code-review high` if Codex is out of quota), fix every finding, merge on green CI, record the Phase 2 status line in the master plan.

## Self-review

**Spec coverage.** Golden recorder first (Task 1). Global/league split (Task 2). Publisher writes `global/<version>` and `leagues/<lg>/<version>` and moves `league_snapshots` with the fenced upsert (Task 7). Request path: authorize → pointer → `/tmp` LRU ≤ 350 MB → league read-only + global read-only + `search_path` → engines (Tasks 3–5). Perspective team passed in (Task 4 view + Task 5 rule). `/l/<lg_id>/…`, legacy redirect, league switcher, anonymous perspective via URL and session storage, "data as of" from the served version (Task 8). Per-instance response cache keyed by league, data version, global version, visibility epoch, perspective, route, params, after authorization (Task 6). Sign-in UI is deferred to Phase 3 (Decision table).

**Placeholders.** None: every code step embeds the shipped file. The PR number is filled at merge.

**Type consistency.** `VERSION_RE` (Task 2) validates what `league_files` (Task 3) keys and `league_scope` (Task 5) checks; `LeagueScope.cache_key` (Task 5) is both `data_version()` for engine caches and the response-cache key prefix (Task 6); `publish_local` (tests) and `publish_league` (Task 7) write the same `league_snapshots` shape the API reads.

## Review fixes (PR #151)

The task sections above show the code as first written. Review then changed it as follows; the branch holds the final code.

- **League file cache.** Files a request uses are pinned until it ends. In-flight downloads reserve their bytes. A request that cannot fit gets `CacheFull`, answered as 503. Each process keeps its own directory.
- **First sync.** The 409 carries `X-FI-Sync-State`.
- **Layered DuckDB.** `memory_limit` (256 MB) and `threads` (2) are set before the configuration is locked.
- **Publisher.**
  - The fence is reserved when a publish starts. Of two overlapping publishers, the one that started later wins.
  - A publisher that loses deletes its own object.
  - Unreferenced league objects stamped after the current pointer, and unreferenced globals younger than a day, are kept.
- **Perspective.** An explicit `?team_id=` overrides the saved team, as the Decisions table says.
- **Owner-only pages.** Projection performance, Ours promotion and research are mounted with `require_platform_admin`. Everyone else gets 404 (spec §6).
- **Links.** Nav and command-palette links carry `/l/<lg>`, so a click skips the cookie redirect.
