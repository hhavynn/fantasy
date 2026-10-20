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
