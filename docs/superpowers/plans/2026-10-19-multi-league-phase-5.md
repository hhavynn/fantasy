# Multi-league Phase 5: parity audit — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** every page works the same for an ESPN public league, an ESPN private league and a Sleeper league on the multi-league site (`/l/<lg>/…`), and the Phase 1a single-snapshot path can be retired once Havyn signs off.

**Architecture:** a browser parity matrix runs every page against the real multi-league stack, offline: a throwaway Postgres, a fake Supabase JWKS so tokens are verified as in production, and three league files published as production publishes them. Pages that assumed Los Subies are fixed where the matrix shows it. The classic path gets a flag (`CLASSIC_SITE_ENABLED`, default on) whose off position is the retirement. It is flipped only after a week of side-by-side use.

**Tech Stack:** Playwright, FastAPI, Postgres (tests/pg_harness.py), DuckDB, Next.js.

Master plan: [2026-10-19-multi-league-platform.md](2026-10-19-multi-league-platform.md) (Phase 5).

## Global Constraints

- Free tiers only.
- No engine rewrites.
- Fixes go only where a page assumed Los Subies.
- Every request is authorized server-side first. Unknown and unauthorized leagues are both 404.
- Owner-only research pages need a platform admin.
- Los Subies goldens stay green. Flags default to keeping today's behavior. The rollback path (the classic site) keeps working until the sign-off.
- No test hook in production code: the matrix authenticates with real signed tokens against a fake JWKS that exists only in the harness.

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Private league in the matrix | Use a real ES256 token signed by a key that lives only in the harness process. The harness serves the matching JWKS at a fake `SUPABASE_URL`, and the browser carries the `@supabase/ssr` session cookie for it. Both the API (`jwt_auth`) and the web server (`getClaims`) verify it exactly as on production | A bypass would be a production risk. This exercises the real path |
| Matrix shape | 3 leagues × every navigable page (owner-only research pages excluded), plus a manager and a player page. Checks per page: 200, one h1, no error state, no console error, and no "ESPN" for Sleeper. A signed-out visitor sees nothing of the private league. A member who is not an admin sees no owner research | The master plan's acceptance |
| Retiring Phase 1a | `CLASSIC_SITE_ENABLED`, default `1` | Off means: the API answers 404 on the classic `/api/*` routes (except `/health` and `/v2`); the refresh chain stops `publish_snapshot.py`; an unscoped page redirects to the viewer's last league, or to `/leagues`. Rollback is turning it back on, since the snapshot stays published until then. The master plan keeps the classic path until Havyn's sign-off |

## Status

Tasks 1–4 are built. The matrix is 68 of 68 locally. The first run found two things, both fixed: anonymous visitors hit an error instead of a team picker on three pages, and the private league needed a local-Supabase option for the harness. Task 5 waits on CI and on Havyn's week of side-by-side use.

## Tasks

### Task 1: The parity matrix harness
- `scripts/e2e_multileague.py` (Postgres, JWKS, three published leagues, `manifest.json`, then the API)
- `frontend/playwright.multileague.config.ts` (ports 8767/8768/3102) and `frontend/e2e/multileague.spec.ts`
- `npm run e2e:multileague`, plus a CI step. Data in `data/e2e-multileague/` (gitignored).

### Task 2: Fix what the matrix finds
Each failure gets the narrowest fix that removes a Los Subies assumption (a missing perspective, an ESPN-only query, a hard-coded team), with a unit test when the fix is in a helper.

### Task 3: Owner-only pages on the multi-league site
The nav hides Research lab and Projection accuracy unless `/v2/me` says platform admin. A direct visit shows the same "not found" notice as an unknown league, and the backend already answers 404.

### Task 4: The retirement switch
- **API:** with `CLASSIC_SITE_ENABLED=0`, a middleware answers 404 for classic `/api/*`. Test: classic route 404, `/v2` unaffected, `/health` 200.
- **Refresh chain:** with the flag off, `refresh_all.plan()` leaves out `publish_snapshot.py`. Test.
- **Web:** with the flag off and multi-league on, `proxyDecision` redirects an unscoped page to the last league cookie, or to `/leagues`; `/login`, `/auth`, `/invite` and `/leagues` stay unscoped. Unit test.

### Task 5: Acceptance
- The matrix is green in CI. No "coming soon" text anywhere (grep test).
- Havyn uses Los Subies on `/l/<lg>/` beside the classic site for a week, then signs off. Then flip `CLASSIC_SITE_ENABLED=0` (API, web and laptop).
