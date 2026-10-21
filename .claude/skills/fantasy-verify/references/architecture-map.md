# Repository Architecture Map & Developer Reference

This compact reference guides targeted code exploration, subsystem navigation, and handoff checkpoints.

---

## 1. Subsystem Directory Layout

| Subsystem | Primary Path | Description | Key Test Suites |
|-----------|--------------|-------------|-----------------|
| **Control Plane** | `backend/app/control_plane/` | Supabase authz, JWKS JWT validation, mutations, crypto envelope | `test_jwt_auth.py`, `test_authorize_league.py`, `test_v2_authorization_matrix.py`, `test_control_plane_rls.py`, `test_control_plane_functions.py` |
| **Migrations** | `supabase/migrations/` | Supabase SQL migrations (schema `fi`), RLS policies, down migrations | `test_pg_harness.py`, `test_control_plane_pg.py`, `test_control_plane_rls.py`, `test_control_plane_mutations.py` |
| **Storage & Cache** | `backend/app/db/` | DuckDB connection lifecycle, R2 SigV4 download/upload, `/tmp` LRU cache | `test_league_files.py`, `test_league_conn.py`, `test_snapshot.py`, `test_r2.py` |
| **Sync Engine** | `backend/app/sync/` | GitHub Actions worker, lease reclaim, provider slot fencing, build | `test_sync_worker.py`, `test_sync_queue.py`, `test_sync_publish.py`, `test_sync_build.py` |
| **Analytics & Engines**| `backend/app/analytics/` | Trade evaluator v3, start/sit, kickers, injury, projections, re-scoring | `test_trades.py`, `test_start_sit.py`, `test_kicker_projections.py`, `test_injury_model.py`, `test_golden_legacy.py` |
| **API Routers** | `backend/app/api/` | Legacy & `/v2` FastAPI routers, tenant routing, league scoping | `test_api.py`, `test_connect_api.py`, `test_league_scope_audit.py`, `test_v2_authorization_matrix.py` |
| **Frontend** | `frontend/` | Next.js 16 (React 19), components, league switcher, perspectives | `npm test`, `npm run lint`, `npm run typecheck`, `npm run build` |
| **Scripts** | `scripts/` | Verification, database seeding, golden verification, E2E harnesses | `test_agent_check.py`, `test_static_checks.py`, `golden_check.py` |

---

## 2. Authoritative Design Documents

Before modifying core subsystems or starting new phases, consult these authoritative documents:

- **ADR-0001: League Context and Tenancy**: `docs/multileague/ADR-0001-league-context-and-tenancy.md`
- **ADR-0002: Supabase Control Plane**: `docs/multileague/ADR-0002-supabase-control-plane.md`
- **Threat Model**: `docs/multileague/threat-model.md`
- **Provider Capability Matrix**: `docs/multileague/provider-capability-matrix.md`
- **Multi-League Platform Master Plan**: `docs/superpowers/plans/2026-10-19-multi-league-platform.md`
- **Multi-League System Spec**: `docs/superpowers/specs/2026-10-19-multi-league-platform-design.md`

---

## 3. Known Dangerous Architectural Boundaries

1. **Private League Indistinguishability (401 vs 404)**
   - Unauthenticated requests → `401`.
   - Authenticated non-members requesting private leagues → `404` (never `403`).
   - Leaking existence of private leagues to non-members is a security violation.

2. **Database Concurrency & Row Locking**
   - Modifying league status, visibility, invites, or credentials requires row locks (`SELECT ... FOR UPDATE` on `fi.leagues`).
   - Lock ordering: Always lock `fi.leagues` before child rows (`fi.league_invites`, `fi.league_memberships`) to prevent deadlocks.

3. **Postgres Connection Pool Claims Isolation**
   - Never set persistent claims across connection checkouts in poolers.
   - Use transaction-local claims `set_config('request.jwt.claims', ..., true)`.

4. **Async FastAPI Route Blocking I/O**
   - Never call `duckdb.connect`, `open_db`, `psycopg.connect`, or heavy synchronous queries inside `async def` routes without `asyncio.to_thread`.
   - Declare synchronous routes with standard `def` so FastAPI handles threadpooling.

5. **Worker Lease Fencing**
   - Sync workers must verify `lease_token` on every transition.
   - A superseded worker must publish nothing to R2 and never advance `fi.leagues.sync_fence`.

6. **Log Redaction**
   - GitHub Actions logs are public. Never log `espn_s2`, `swid`, private keys, passwords, or raw DSNs.

---

## 4. Phase / Task Handoff Checkpoint Template

For long multi-phase tasks or agent session handoffs, record a concise markdown checkpoint:

```markdown
### Implementation Handoff Checkpoint

- **Branch / Worktree:** `h/<feature-branch>` (`.worktrees/<feature-dir>`)
- **Current Phase:** Phase X (Task Y / Z)
- **Verified Commit SHA:** `<40-character-git-sha>`
- **Passed Gates:**
  - [x] Static checks: clean (0 errors)
  - [x] Affected tests: X passed in Ys
  - [x] Security suite: 15/15 passed
- **Active Blockers / Open Decisions:** None
- **Key Files Modified:**
  - `path/to/file1.py`
  - `path/to/file2.sql`
- **Next Concrete Action:**
  - Run `python fantasy-intel/scripts/agent_check.py affected` after implementing Step N.
```
*(Do NOT include credentials, session cookies, raw tokens, or sensitive diagnostics in handoff files).*
