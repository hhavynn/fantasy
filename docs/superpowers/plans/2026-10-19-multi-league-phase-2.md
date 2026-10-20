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

