# Multi-league Phase 3: Sync jobs on GitHub Actions + ESPN connect flows — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** League files are built and published by fenced, Postgres-queued sync jobs that run in GitHub Actions, and people can sign in, import a public ESPN league, connect a private one with their cookies, and invite viewers. Los Subies' ESPN data syncs through jobs with Havyn's private credential.

**Architecture:** `fi.sync_jobs` stays the authoritative queue. A drain (`scripts/sync_drain.py`, run by `.github/workflows/sync-drain.yml` every 15 minutes and on `workflow_dispatch`) does the following:
- claims jobs in one transaction: candidate row, per-league lock, provider slot, fence;
- heartbeats every 30 s;
- builds the league file on the runner: current global file plus the league's previous file, then ESPN `fetch_all` → `write_all` → `run_analytics`, then `split_database`;
- uploads it and moves the pointer with the spec's fenced upsert, using the job's fence;
- records the outcome per the spec's failure table.

The API registers leagues and stores credentials through SECURITY DEFINER functions. It enqueues jobs and triggers the workflow with a token that can only dispatch Actions. Next.js gains Supabase sign-in (email one-time code) and the Connect, invite and credential pages.

**Tech Stack:** Python 3.12, psycopg 3 (`fi_sync`), DuckDB, `requests` + `espn_api`, `cryptography` (existing `credential_crypto`), GitHub Actions, Supabase Auth (`@supabase/ssr`), Next.js 16, pytest, node --test, Playwright.

Master plan: [2026-10-19-multi-league-platform.md](2026-10-19-multi-league-platform.md). Spec: [2026-10-19-multi-league-platform-design.md](../specs/2026-10-19-multi-league-platform-design.md) §2–§3. Previous phases: [phase-1](2026-10-19-multi-league-phase-1.md), [phase-2](2026-10-19-multi-league-phase-2.md).

## Global Constraints

- Free tiers only: standard GitHub-hosted runners on this public repo, R2 free, Supabase free, Vercel Hobby. Supabase Auth's built-in email sender (a few emails per hour) is enough until sign-ups open (Phase 6 decides custom SMTP).
- Flags default off.
  - `MULTILEAGUE_ENABLED`, `MULTILEAGUE_UI_ENABLED`: from earlier phases.
  - `MULTILEAGUE_CONNECT_ENABLED` (API): import, connect, invite and credential routes.
  - `SYNC_DRAIN_ENABLED` (repository variable read by the workflow): with it unset, the scheduled drain exits at once.
  - With every flag off, production behaves as Phase 2 left it.
- The workflow runs only on `schedule` and `workflow_dispatch`, never on `pull_request`. Its secrets are:
  - `FI_SYNC_DB_URL`;
  - `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (an Object Read & Write token for the `fantasy-intel` bucket);
  - `CREDENTIALS_PRIVKEY_V1`.

  The Supabase service-role key is never used.
- Public logs. The drain prints only opaque ids (`lg_…`, job uuids), durations, counts and error codes. Every line goes through a redacting printer that drops any line containing a sensitive string seen in the run: team, member and player names, the ESPN league id, cookies. Decrypted cookies are `::add-mask::`ed before use.
- ESPN cookies are used only for GETs whose URL is under `…/seasons/<season>/segments/0/leagues/<id>` or `…/leagueHistory/<id>` for that league. Every other ESPN request (pro players, the control request) is sent without cookies.
- Ambiguous provider errors never revoke credentials or delete data. Only the spec §3.6 confirmation rule (≥3 authenticated failures over ≥6 h, with a successful control request in the same run) marks a credential `invalid` or a league `unavailable`.
- The only step that makes data visible is the fenced upsert (`… WHERE fi.league_snapshots.fence < EXCLUDED.fence`), carrying the fence the job got at claim time.
- No engine rewrites. The runner calls the existing `fetch_all`, `write_all` and `run_analytics` unchanged.

## Decisions taken in this plan

| Topic | Decision | Why |
|---|---|---|
| Two PRs | **3a** (Tasks 1–9): queue, worker, workflow, global pointer, Los Subies job path. **3b** (Tasks 10–15): migrations and API for import, connect, invites and credentials; sign-in; Connect UI; end to end. Both live under this plan. | Each PR is reviewable and has its own tests; Phase 4 starts only after 3b is merged and every Phase 3 acceptance criterion holds |
| What a job builds from | Working DB = a copy of the current global file + the league's previous league file (tables the job does not rebuild carry forward) + fresh ESPN data + `run_analytics`, then `split_database`. The runner's split global is discarded; the pointer names the global version the job read | Same engines and tables as the laptop path. The laptop's Ours, kicker, D/ST and report tables survive job publishes until Phase 4 moves Ours to the runner |
| Current global version | New single-row table `fi.global_snapshot(global_version, committed_at)`, written by the laptop publisher (`fi_sync`). Jobs read it | A job must know which global file to build against; Phase 7 moves global publishing to Actions |
| Los Subies | Synced by jobs with Havyn's private credential (connected in 3b). The laptop keeps publishing Los Subies after its own refresh until Phase 4 (both publishers are fenced) | The laptop's Ours and research steps read its local ESPN data. Removing the launchd ESPN step moves to Phase 4, together with Ours |
| Workflow trigger from the API | `workflow_dispatch` with a fine-grained token that has **Actions: write** only (`GITHUB_DISPATCH_TOKEN` on Vercel) | `repository_dispatch` needs Contents: write, which could also push code |
| Parallelism | One drain run at a time (`concurrency: sync-drain`, not cancelled); inside a run, two worker processes drain the queue. `provider_slots` (ESPN 2, Sleeper 4) cap provider load | GitHub keeps one pending run per group; slots are the race-safe cap |
| Raw provider data | `raw/<lg>/<data_version>.json.gz` in R2 (member SWIDs scrubbed, as `save_raw` does). `recompute` jobs rebuild from it without fetching | Spec §3.7; same private bucket |
| Control request (§3.6) | Cookie-less GET of ESPN's season `players` endpoint (public) | Proves ESPN is reachable without touching another league |
| Sign-in | Supabase email one-time code via `@supabase/ssr`; the Next.js server forwards the access token as `Authorization: Bearer` | Free; no OAuth app to create. FastAPI already verifies these JWTs (Phase 1) |
| Timers | The drain's maintenance step (once per run) marks dormant leagues, expires stale and private-flipped leagues, finishes soft deletes and enqueues due refreshes and probes | No extra infrastructure; runs every 15 min |

## File structure

| File | Responsibility |
|---|---|
| `supabase/migrations/20261021120000_sync_queue.sql` (+ `down/`) | `fi.global_snapshot`; `leagues.status_changed_at`; `fi_sync` grants for maintenance (`name`, `deleted_at`, `status_changed_at`); `fi.retire_league_objects_due()` view for the drain |
| `supabase/migrations/20261021130000_connect_flows.sql` (+ `down/`) | `fi.import_public_league`, `fi.connect_private_league`, `fi.request_sync`, `fi.request_probe`, `fi.delete_league` (admin soft delete) |
| `backend/app/sync/queue.py` | `enqueue`, `claim`, `heartbeat`, `complete`, `fail`, `release_slot`: the §3.3–3.4 SQL |
| `backend/app/sync/failures.py` | Pure classification: an exception and its context → `Retry(delay)`, `Dead`, `Suspect`, `ConfirmInvalid`, `ConfirmUnavailable`, `Restrict` |
| `backend/app/sync/espn_source.py` | Builds an `EspnClient` with cookies scoped to the league URL allowlist; the cookie-less control and visibility probes |
| `backend/app/sync/raw_store.py` | `EspnPayloads` ⇄ gzip JSON (`dump_payloads`, `load_payloads`) |
| `backend/app/sync/build.py` | `build_league_file(...)`: working DB from global + previous league file, ESPN write, analytics, split |
| `backend/app/sync/publish.py` | `publish_built(...)`: upload the league object and raw payloads, fenced upsert with the job's fence, history, collect |
| `backend/app/sync/redact.py` | `Printer`: the only output path of the drain; drops lines containing sensitive strings |
| `backend/app/sync/worker.py` | `run_job(...)` per kind (initial/scheduled/manual/recompute/probe), heartbeat thread, credential decrypt |
| `backend/app/sync/maintenance.py` | Scheduled and probe enqueueing, dormancy, retention, soft-delete completion |
| `scripts/sync_drain.py` | CLI: N worker processes, 20-minute budget, maintenance once |
| `.github/workflows/sync-drain.yml` | Schedule, `workflow_dispatch`, concurrency, secrets, masked env, log audit |
| `backend/app/db/league_publish.py` | `publish_league` (laptop) also writes `fi.global_snapshot` |
| `backend/app/api/connect.py` | 3b: `/v2/leagues/import`, `/v2/leagues/connect`, invites, credential, `/sync` routes; `dispatch_drain()` |
| `frontend/lib/supabase/*.ts`, `frontend/app/login/*` | 3b: sign-in |
| `frontend/app/leagues/*`, `frontend/app/l/[league]/settings/*` | 3b: Connect, invites, credentials, sync status |

## Tasks

The task sections below are appended as each task lands, with the shipped files verbatim (the same convention as Phase 2).

**3a**
1. Migration: global pointer, status timestamps, `fi_sync` maintenance grants.
2. `app/sync/queue.py`. Tests: reclaim in place; heartbeat loss; slot cap under 10 concurrent claimers; duplicate enqueue collapses; backoff schedule; `dead` after 5 attempts.
3. `app/sync/failures.py`. Tests: every row of spec §3.6; the confirmation rule (3 failures over 6 h *and* a control success).
4. `app/sync/espn_source.py` + `raw_store.py`. Tests: the cookie allowlist refuses other URLs; the control request carries no cookies; payloads round-trip; SWIDs are scrubbed.
5. `app/sync/build.py`. Tests: the fixture league built by a job equals the laptop path's league file table by table; previous-version tables carry forward; a failure leaves nothing.
6. `app/sync/publish.py` + `league_publish` global pointer. Tests: stale fence publishes nothing; a new league's first publish is visible; racing first publishes keep the higher fence; a failure before the swap leaves the pointer unchanged.
7. `app/sync/redact.py` + `app/sync/worker.py`. Tests: a full drain of the fixture prints no team, member or player name, ESPN id or cookie; lease loss aborts without publishing; a 401 becomes `suspect`, never `invalid`, without confirmation.
8. `app/sync/maintenance.py`. Tests: scheduled enqueue skips dormant leagues; probe due after 6 h; private probe restricts at once (`visibility_epoch + 1`); dormancy and retention timers; soft delete finished after 7 days.
9. `scripts/sync_drain.py` + `.github/workflows/sync-drain.yml`. Tests: the drain CLI on the fixture end to end (DirStore + local Postgres); the workflow has no `pull_request` trigger and masks secrets (static check).

**3b**
10. Migration: connect functions.
11. `app/api/connect.py`. Tests: authorization matrix for the new routes; cookies validated by one fetch and never echoed; invite create, redeem and revoke; dispatch called after commit and its failure tolerated.
12. Frontend sign-in (`@supabase/ssr`). Tests: unit (session → bearer), E2E with the flag off unchanged.
13. Frontend Connect, invites, credentials and sync status. Tests: unit + Playwright against a mocked API.
14. Los Subies through jobs: runbook to connect the credential, a local golden check against the job-built file.
15. End to end on production: public import visible within 5 min; flip private → 404 within one probe; private connect → invite → viewer.


### Task 1: Queue migration (global pointer, confirmation rules, retention timers)

**Files:**
- Create: `fantasy-intel/supabase/migrations/20261021120000_sync_queue.sql`
- Create: `fantasy-intel/supabase/down/20261021120000_sync_queue.sql`
- Create: `fantasy-intel/backend/tests/test_sync_queue_schema.py`
- Modify: `fantasy-intel/backend/app/db/league_split.py`
- Modify: `fantasy-intel/backend/app/db/league_publish.py`
- Modify: `fantasy-intel/backend/tests/pg_harness.py`
- Modify: `fantasy-intel/backend/tests/test_league_publish.py`
- Modify: `fantasy-intel/backend/tests/test_league_split.py`
- Modify: `fantasy-intel/backend/tests/test_control_plane_rls.py`
- Modify: `fantasy-intel/supabase/README.md`

**Interfaces:**
- Produces: table fi.global_snapshot(global_version, content_hash, committed_at); fi.record_credential_failure(job uuid, lease bigint, credential uuid, control_ok bool) -> text; fi.record_league_missing(job, lease, control_ok) -> text; fi.record_league_found(job, lease); fi.league_timers() -> setof league_id; fi.league_purged(league_id).
- Produces: league_split.content_hash(path) -> str; league_publish.publish_global(global_path, global_version, writer, sync_con) -> str; league_publish.commit_league(writer, sync_con, league_id, league_path, league_version, global_version, fence, keep=3) -> (applied, deleted).
- Why content hashes: DuckDB does not write identical bytes for identical content, so the Phase 2 reuse by file hash almost never matched (test_republishing… failed 5 runs in 6).

- [ ] **Step 1: Write the failing tests**

`fantasy-intel/backend/tests/test_sync_queue_schema.py`:

```python
"""Phase 3 queue support in Postgres: the global pointer, status timestamps, the §3.6 confirmation rules and the
retention timers (supabase/migrations/20261021120000_sync_queue.sql)."""
import uuid

import psycopg
import pytest

from cp_seed import U1, U2, as_role, seed_league, seed_membership, seed_snapshot, seed_user

CT, NONCE, WRAPPED = b"\x01" * 48, b"\x02" * 12, b"\x03" * 384
GV = "20261021T120000Z-" + "a" * 24 + "-1000"


def sync(pg):
    return psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None)


@pytest.fixture
def private(pg):
    """A private league with U1's credential and a running job leased with token 7."""
    cid = str(uuid.uuid4())
    with pg.connect() as su:
        seed_user(su, U1)
        lid = seed_league(su, visibility="private")
        seed_membership(su, U1, lid, "admin")
        su.commit()
    with pg.connect(user="fi_api") as con, as_role(con, "authenticated", U1) as c:
        c.execute("SELECT fi.store_credential(%s, %s, %s, %s, %s, 'v1')", [cid, lid, CT, NONCE, WRAPPED])
    with pg.connect() as su:
        job = su.execute("""INSERT INTO fi.sync_jobs (league_id, kind, status, lease_token, lease_expires_at)
                            VALUES (%s, 'scheduled', 'running', 7, now() + interval '2 minutes') RETURNING id::text""",
                         [lid]).fetchone()[0]
        su.commit()
    return {"league": lid, "cid": cid, "job": job}


def age_first_failure(pg, cid, hours):
    with pg.connect() as su:
        su.execute("UPDATE fi.league_credentials SET first_failed_at = now() - %s * interval '1 hour' WHERE id = %s",
                   [hours, cid])
        su.commit()


FAIL = "SELECT fi.record_credential_failure(%s, %s, %s, %s)"


def test_a_credential_is_invalid_only_after_three_failures_over_six_hours_with_a_control_success(pg, private):
    j, cid = private["job"], private["cid"]
    with sync(pg) as con:
        assert con.execute(FAIL, [j, 7, cid, True]).fetchone()[0] == "suspect"
        assert con.execute(FAIL, [j, 7, cid, True]).fetchone()[0] == "suspect"
        assert con.execute(FAIL, [j, 7, cid, True]).fetchone()[0] == "suspect", "3 failures but within 6 hours"
        con.commit()
    age_first_failure(pg, cid, 7)
    with sync(pg) as con:
        assert con.execute(FAIL, [j, 7, cid, False]).fetchone()[0] == "suspect", "ESPN unreachable: never confirm"
        assert con.execute(FAIL, [j, 6, cid, True]).fetchone()[0] is None, "a stale lease marks nothing"
        assert con.execute(FAIL, [j, 7, cid, True]).fetchone()[0] == "invalid"
        con.commit()
    with pg.connect() as su:
        assert su.execute("SELECT status FROM fi.leagues WHERE id = %s", [private["league"]]).fetchone() == ("stale",)
        assert su.execute("SELECT count(*) FROM fi.league_credentials").fetchone() == (1,), "never deleted"


def test_a_success_resets_the_failure_count(pg, private):
    j, cid = private["job"], private["cid"]
    with sync(pg) as con:
        con.execute(FAIL, [j, 7, cid, True])
        con.execute(FAIL, [j, 7, cid, True])
        con.execute("SELECT fi.mark_credential(%s, 7, %s, 'active')", [j, cid])
        con.commit()
    age_first_failure(pg, cid, 7)
    with sync(pg) as con:
        assert con.execute(FAIL, [j, 7, cid, True]).fetchone()[0] == "suspect"
        con.commit()


MISSING = "SELECT fi.record_league_missing(%s, %s, %s)"


def test_a_league_is_unavailable_only_after_three_404s_over_six_hours_with_a_control_success(pg, private):
    j, lid = private["job"], private["league"]
    with sync(pg) as con:
        for _ in range(3):
            assert con.execute(MISSING, [j, 7, True]).fetchone()[0] == "active"
        con.commit()
    with pg.connect() as su:
        su.execute("UPDATE fi.leagues SET missing_since = now() - interval '7 hours' WHERE id = %s", [lid])
        su.commit()
    with sync(pg) as con:
        assert con.execute(MISSING, [j, 7, False]).fetchone()[0] == "active"
        assert con.execute(MISSING, [j, 7, True]).fetchone()[0] == "unavailable"
        con.execute("SELECT fi.record_league_found(%s, %s)", [j, 7])
        con.commit()
    with pg.connect() as su:
        assert su.execute("SELECT missing_count, missing_since FROM fi.leagues WHERE id = %s", [lid]).fetchone() == (0, None)


def test_status_changes_are_timestamped(pg):
    with pg.connect() as su:
        lid = seed_league(su)
        su.execute("UPDATE fi.leagues SET status_changed_at = now() - interval '3 days' WHERE id = %s", [lid])
        su.execute("UPDATE fi.leagues SET name = 'x' WHERE id = %s", [lid])
        assert su.execute("SELECT status_changed_at < now() - interval '2 days' FROM fi.leagues WHERE id = %s",
                          [lid]).fetchone() == (True,), "only a status change moves it"
        su.execute("UPDATE fi.leagues SET status = 'dormant' WHERE id = %s", [lid])
        assert su.execute("SELECT status_changed_at > now() - interval '1 minute' FROM fi.leagues WHERE id = %s",
                          [lid]).fetchone() == (True,)


def test_the_global_pointer_is_written_by_fi_sync_only(pg):
    with sync(pg) as con:
        con.execute("INSERT INTO fi.global_snapshot (global_version, content_hash) VALUES (%s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET global_version = EXCLUDED.global_version, committed_at = now()",
                    [GV, "b" * 24])
        con.commit()
        assert con.execute("SELECT global_version FROM fi.global_snapshot").fetchone() == (GV,)
        with pytest.raises(psycopg.errors.CheckViolation):
            con.execute("INSERT INTO fi.global_snapshot (id, global_version, content_hash) VALUES (false, %s, %s)",
                        [GV, "b" * 24])
    with pg.connect(user="fi_api") as con:
        for role in ("anon", "authenticated"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), as_role(con, role, U1 if role != "anon" else None) as c:
                c.execute("SELECT * FROM fi.global_snapshot")


TIMERS = "SELECT league_id FROM fi.league_timers() ORDER BY 1"


def test_timers_mark_unwatched_public_leagues_dormant_and_wake_them_on_a_view(pg):
    with pg.connect() as su:
        seed_user(su, U2)
        old = seed_league(su)
        pinned = seed_league(su)
        fresh = seed_league(su)
        su.execute("UPDATE fi.leagues SET created_at = now() - interval '40 days' WHERE id IN (%s, %s)", [old, pinned])
        su.execute("INSERT INTO fi.league_pins (user_id, league_id) VALUES (%s, %s)", [U2, pinned])
        su.commit()
    with sync(pg) as con:
        assert con.execute(TIMERS).fetchall() == []
        con.commit()
    with pg.connect() as su:
        status = dict(su.execute("SELECT id, status FROM fi.leagues").fetchall())
        assert (status[old], status[pinned], status[fresh]) == ("dormant", "active", "active")
        su.execute("UPDATE fi.leagues SET last_viewed_at = now() WHERE id = %s", [old])
        su.commit()
    with sync(pg) as con:
        con.execute(TIMERS)
        con.commit()
    with pg.connect() as su:
        assert su.execute("SELECT status FROM fi.leagues WHERE id = %s", [old]).fetchone() == ("active",)


def test_timers_return_leagues_whose_objects_are_due_for_deletion(pg):
    with pg.connect() as su:
        long_dormant = seed_league(su, status="dormant")
        recently_dormant = seed_league(su, status="dormant")
        stale = seed_league(su, visibility="private", status="stale")
        flipped = seed_league(su, visibility="private", status="unavailable")
        deleted = seed_league(su)
        for lid, days in ((long_dormant, 91), (recently_dormant, 10), (stale, 61), (flipped, 31)):
            su.execute("UPDATE fi.leagues SET status_changed_at = now() - %s * interval '1 day' WHERE id = %s", [days, lid])
        su.execute("UPDATE fi.leagues SET deleted_at = now() - interval '8 days' WHERE id = %s", [deleted])
        for lid in (long_dormant, recently_dormant, stale, flipped, deleted):
            seed_snapshot(su, lid)
        su.commit()
    with sync(pg) as con:
        due = {r[0] for r in con.execute(TIMERS).fetchall()}
        con.commit()
    assert due == {long_dormant, deleted}, "stale/flipped leagues are soft-deleted first and purged 7 days later"
    with pg.connect() as su:
        gone = dict(su.execute("SELECT id, deleted_at IS NOT NULL FROM fi.leagues").fetchall())
        assert gone[stale] and gone[flipped] and not gone[recently_dormant]
    with sync(pg) as con:
        con.execute("SELECT fi.league_purged(%s)", [long_dormant])
        con.commit()
        assert {r[0] for r in con.execute(TIMERS).fetchall()} == {deleted}, "purged once"
    with pg.connect() as su:
        assert su.execute("SELECT count(*) FROM fi.league_snapshots WHERE league_id = %s", [long_dormant]).fetchone() == (0,)
        assert su.execute("SELECT objects_purged_at IS NOT NULL FROM fi.leagues WHERE id = %s",
                          [long_dormant]).fetchone() == (True,)


def test_a_private_league_with_a_live_credential_is_never_expired(pg, private):
    with pg.connect() as su:
        su.execute("UPDATE fi.leagues SET status = 'stale' WHERE id = %s", [private["league"]])
        su.execute("UPDATE fi.leagues SET status_changed_at = now() - interval '90 days' WHERE id = %s", [private["league"]])
        su.commit()
    with sync(pg) as con:
        con.execute(TIMERS)
        con.commit()
    with pg.connect() as su:
        assert su.execute("SELECT deleted_at FROM fi.leagues WHERE id = %s", [private["league"]]).fetchone() == (None,)


@pytest.mark.parametrize("fn", ["record_credential_failure", "record_league_missing", "league_timers", "league_purged"])
def test_new_functions_are_for_fi_sync_only(pg, fn):
    with pg.connect() as con:
        grantees = {r[0] for r in con.execute(
            "SELECT grantee FROM information_schema.routine_privileges WHERE routine_schema = 'fi' AND routine_name = %s",
            [fn]).fetchall()}
    assert grantees - {"postgres"} == {"fi_sync"}, grantees


def test_record_credential_failure_locks_the_league_before_the_credential(pg):
    with pg.connect() as con:
        src = con.execute("SELECT pg_get_functiondef('fi.record_credential_failure(uuid,bigint,uuid,boolean)'::regprocedure)"
                          ).fetchone()[0]
    body = " ".join(src.split())
    lock = body.find("FROM fi.leagues")
    assert lock != -1 and "FOR UPDATE" in body[lock:lock + 200]
    assert lock < body.find("UPDATE fi.league_credentials")
```

- [ ] **Step 2: Run them and see them fail**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_sync_queue_schema.py -q
```
Expected: FAIL (functions and table do not exist).

- [ ] **Step 3: Implement**

`fantasy-intel/supabase/migrations/20261021120000_sync_queue.sql`:

```sql
-- Phase 3a: what the GitHub Actions sync workers need beyond the Phase 1 queue tables.
--   * fi.global_snapshot: the global file every new league build reads, and its content hash (written by the laptop
--     publisher as fi_sync).
--   * leagues.status_changed_at (kept by a trigger), missing_count / missing_since, objects_purged_at.
--   * fi.record_credential_failure / fi.record_league_missing: the spec §3.6 confirmation rule, decided in one place.
--   * fi.league_timers / fi.league_purged: dormancy and retention (spec §2), run by the drain's maintenance step.
-- Idempotent. Down-migration: supabase/down/20261021120000_sync_queue.sql.

CREATE TABLE IF NOT EXISTS fi.global_snapshot (
  id             boolean PRIMARY KEY DEFAULT true CHECK (id),
  global_version text NOT NULL CHECK (global_version ~ '^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{24}-[1-9][0-9]{0,11}$'),
  -- league_split.content_hash of the file: an unchanged global is recognised by content, not by its bytes
  content_hash   text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{24}$'),
  committed_at   timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE fi.global_snapshot ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON fi.global_snapshot FROM PUBLIC, anon, authenticated, fi_api;
GRANT SELECT, INSERT, UPDATE ON fi.global_snapshot TO fi_sync;
DROP POLICY IF EXISTS global_snapshot_sync ON fi.global_snapshot;
CREATE POLICY global_snapshot_sync ON fi.global_snapshot FOR ALL TO fi_sync USING (true) WITH CHECK (true);

ALTER TABLE fi.leagues ADD COLUMN IF NOT EXISTS status_changed_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE fi.leagues ADD COLUMN IF NOT EXISTS missing_count integer NOT NULL DEFAULT 0;
ALTER TABLE fi.leagues ADD COLUMN IF NOT EXISTS missing_since timestamptz;
ALTER TABLE fi.leagues ADD COLUMN IF NOT EXISTS objects_purged_at timestamptz;
-- The worker refreshes the league's name from the provider (shown in the switcher).
GRANT UPDATE (name) ON fi.leagues TO fi_sync;

CREATE OR REPLACE FUNCTION fi.leagues_status_changed() RETURNS trigger LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
  IF NEW.status IS DISTINCT FROM OLD.status THEN
    NEW.status_changed_at := now();
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS leagues_status_changed ON fi.leagues;
CREATE TRIGGER leagues_status_changed BEFORE UPDATE OF status ON fi.leagues
  FOR EACH ROW EXECUTE FUNCTION fi.leagues_status_changed();

-- Spec §3.6: an authenticated 401/403 is ambiguous. The credential is `suspect` until >= 3 failures spanning >= 6 h
-- *and* a control request in the same run succeeded (ESPN reachable); only then `invalid` and the league `stale`.
-- Never deletes anything. Returns the credential's new status, or NULL when this lease no longer holds the job or
-- the credential is not the live one.
CREATE OR REPLACE FUNCTION fi.record_credential_failure(p_job_id uuid, p_lease_token bigint, p_credential_id uuid,
                                                        p_control_ok boolean)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v_league text; v_count integer; v_since timestamptz; v_status text;
BEGIN
  SELECT j.league_id INTO v_league FROM fi.sync_jobs j
  WHERE j.id = p_job_id AND j.status = 'running' AND j.lease_token = p_lease_token AND j.lease_expires_at > now()
  FOR UPDATE;
  IF v_league IS NULL THEN RETURN NULL; END IF;
  -- Lock order everywhere: league, then credential.
  PERFORM 1 FROM fi.leagues l WHERE l.id = v_league FOR UPDATE;
  UPDATE fi.league_credentials SET failure_count = failure_count + 1, first_failed_at = coalesce(first_failed_at, now())
  WHERE id = p_credential_id AND league_id = v_league AND status IN ('active', 'suspect')
  RETURNING failure_count, first_failed_at INTO v_count, v_since;
  IF v_count IS NULL THEN RETURN NULL; END IF;
  v_status := CASE WHEN v_count >= 3 AND v_since <= now() - interval '6 hours' AND p_control_ok
                   THEN 'invalid' ELSE 'suspect' END;
  UPDATE fi.league_credentials SET status = v_status WHERE id = p_credential_id;
  IF v_status = 'invalid' THEN
    UPDATE fi.leagues SET status = 'stale' WHERE id = v_league;
  END IF;
  PERFORM fi.audit(NULL, 'credential.' || v_status, 'league', v_league,
                   jsonb_build_object('job_id', p_job_id, 'credential_id', p_credential_id, 'failures', v_count));
  RETURN v_status;
END $$;

-- The same rule for "league not found" (404): `unavailable` after >= 3 over >= 6 h with a control success. The data
-- is kept (normal retention only). Returns the league's status, or NULL when the lease is gone.
CREATE OR REPLACE FUNCTION fi.record_league_missing(p_job_id uuid, p_lease_token bigint, p_control_ok boolean)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v_league text; v_count integer; v_since timestamptz; v_status text;
BEGIN
  SELECT j.league_id INTO v_league FROM fi.sync_jobs j
  WHERE j.id = p_job_id AND j.status = 'running' AND j.lease_token = p_lease_token AND j.lease_expires_at > now()
  FOR UPDATE;
  IF v_league IS NULL THEN RETURN NULL; END IF;
  UPDATE fi.leagues SET missing_count = missing_count + 1, missing_since = coalesce(missing_since, now())
  WHERE id = v_league RETURNING missing_count, missing_since, status INTO v_count, v_since, v_status;
  IF v_count >= 3 AND v_since <= now() - interval '6 hours' AND p_control_ok AND v_status <> 'unavailable' THEN
    UPDATE fi.leagues SET status = 'unavailable' WHERE id = v_league;
    v_status := 'unavailable';
    PERFORM fi.audit(NULL, 'league.unavailable', 'league', v_league, jsonb_build_object('job_id', p_job_id));
  END IF;
  RETURN v_status;
END $$;

CREATE OR REPLACE FUNCTION fi.record_league_found(p_job_id uuid, p_lease_token bigint)
RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path = '' AS $$
  UPDATE fi.leagues l SET missing_count = 0, missing_since = NULL
  FROM fi.sync_jobs j
  WHERE j.id = p_job_id AND j.status = 'running' AND j.lease_token = p_lease_token AND j.lease_expires_at > now()
    AND l.id = j.league_id AND (l.missing_count <> 0 OR l.missing_since IS NOT NULL)
$$;

-- Dormancy and retention (spec §2). Applies the status transitions and returns the leagues whose stored objects
-- are due for deletion; the drain deletes them and then calls fi.league_purged. Every deletion waits for a
-- soft delete at least 7 days old, or for 90 days of dormancy.
CREATE OR REPLACE FUNCTION fi.league_timers() RETURNS TABLE (league_id text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
  -- A public league nobody pinned or viewed for 30 days stops syncing.
  UPDATE fi.leagues l SET status = 'dormant'
  WHERE l.deleted_at IS NULL AND l.visibility = 'public' AND l.status = 'active'
    AND coalesce(l.last_viewed_at, l.created_at) < now() - interval '30 days'
    AND NOT EXISTS (SELECT 1 FROM fi.league_pins p WHERE p.league_id = l.id);
  -- ... and wakes up when someone views or pins it again (its next sync rebuilds purged objects).
  UPDATE fi.leagues l SET status = 'active', objects_purged_at = NULL
  WHERE l.deleted_at IS NULL AND l.status = 'dormant'
    AND (l.last_viewed_at > l.status_changed_at
         OR EXISTS (SELECT 1 FROM fi.league_pins p WHERE p.league_id = l.id AND p.created_at > l.status_changed_at));
  -- A private league with no live credential: stale for 60 days, or private on the provider after a public import
  -- (unavailable) for 30 days, is deleted. A live credential always keeps it.
  UPDATE fi.leagues l SET deleted_at = now()
  WHERE l.deleted_at IS NULL AND l.visibility = 'private'
    AND ((l.status = 'stale' AND l.status_changed_at < now() - interval '60 days')
         OR (l.status = 'unavailable' AND l.status_changed_at < now() - interval '30 days'))
    AND NOT EXISTS (SELECT 1 FROM fi.league_credentials c WHERE c.league_id = l.id AND c.status IN ('active', 'suspect'));
  RETURN QUERY
    SELECT l.id FROM fi.leagues l
    WHERE l.objects_purged_at IS NULL
      AND (l.deleted_at < now() - interval '7 days'
           OR (l.deleted_at IS NULL AND l.status = 'dormant' AND l.status_changed_at < now() - interval '90 days'));
END $$;

CREATE OR REPLACE FUNCTION fi.league_purged(p_league_id text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
  PERFORM 1 FROM fi.leagues l WHERE l.id = p_league_id FOR UPDATE;
  DELETE FROM fi.league_snapshot_history WHERE league_id = p_league_id;
  DELETE FROM fi.league_snapshots WHERE league_id = p_league_id;
  UPDATE fi.leagues SET objects_purged_at = now() WHERE id = p_league_id;
  PERFORM fi.audit(NULL, 'league.purged', 'league', p_league_id, '{}'::jsonb);
END $$;

REVOKE ALL ON FUNCTION fi.record_credential_failure(uuid, bigint, uuid, boolean), fi.record_league_missing(uuid, bigint, boolean),
  fi.record_league_found(uuid, bigint), fi.league_timers(), fi.league_purged(text), fi.leagues_status_changed() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.record_credential_failure(uuid, bigint, uuid, boolean), fi.record_league_missing(uuid, bigint, boolean),
  fi.record_league_found(uuid, bigint), fi.league_timers(), fi.league_purged(text) TO fi_sync;
```

`fantasy-intel/supabase/down/20261021120000_sync_queue.sql`:

```sql
-- Reverses 20261021120000_sync_queue.sql.

DROP FUNCTION IF EXISTS fi.league_purged(text);
DROP FUNCTION IF EXISTS fi.league_timers();
DROP FUNCTION IF EXISTS fi.record_league_found(uuid, bigint);
DROP FUNCTION IF EXISTS fi.record_league_missing(uuid, bigint, boolean);
DROP FUNCTION IF EXISTS fi.record_credential_failure(uuid, bigint, uuid, boolean);
DROP TRIGGER IF EXISTS leagues_status_changed ON fi.leagues;
DROP FUNCTION IF EXISTS fi.leagues_status_changed();
REVOKE UPDATE (name) ON fi.leagues FROM fi_sync;
ALTER TABLE fi.leagues DROP COLUMN IF EXISTS objects_purged_at;
ALTER TABLE fi.leagues DROP COLUMN IF EXISTS missing_since;
ALTER TABLE fi.leagues DROP COLUMN IF EXISTS missing_count;
ALTER TABLE fi.leagues DROP COLUMN IF EXISTS status_changed_at;
DROP TABLE IF EXISTS fi.global_snapshot;
```

In `app/db/league_split.py` add `content_hash` (sum of DuckDB row hashes per table, plus names and columns, leaving out `schema_meta.published_at`). In `app/db/league_publish.py` split `publish_league` into `publish_global` (reuse the current global object when its content hash is unchanged, else upload it and upsert `fi.global_snapshot`) and `commit_league` (upload, fenced upsert, history, collect); `_collect` also keeps the current global. Add `global_snapshot` to `PgDatabase.FI_TABLES`; `fi_sync` may now update `leagues.name` (the RLS test checks `external_id`, `deleted_at` and `last_viewed_at` instead).

- [ ] **Step 4: Run them and see them pass**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_sync_queue_schema.py tests/test_league_publish.py tests/test_league_split.py tests/test_control_plane_rls.py tests/test_control_plane_functions.py -q
```
Expected: all pass; test_republishing… passes on repeated runs.

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/supabase/migrations/20261021120000_sync_queue.sql fantasy-intel/supabase/down/20261021120000_sync_queue.sql fantasy-intel/backend/tests/test_sync_queue_schema.py fantasy-intel/backend/app/db/league_split.py fantasy-intel/backend/app/db/league_publish.py fantasy-intel/backend/tests/pg_harness.py fantasy-intel/backend/tests/test_league_publish.py fantasy-intel/backend/tests/test_league_split.py fantasy-intel/backend/tests/test_control_plane_rls.py fantasy-intel/supabase/README.md
git commit -m "Phase 3 task 1: queue migration (global pointer, §3.6 confirmation rules, retention timers)"
```


### Task 2: The sync queue

**Files:**
- Create: `fantasy-intel/backend/app/sync/__init__.py`
- Create: `fantasy-intel/backend/app/sync/queue.py`
- Create: `fantasy-intel/backend/tests/test_sync_queue.py`

**Interfaces:**
- Produces: Claim(job_id, league_id, kind, provider, fence, attempt, max_attempts, slot); enqueue(con, league_id, kind, *, key, run_after=None) -> job id | None; claim(con, owner, *, providers=('espn','sleeper')) -> Claim | None; heartbeat(con, claim) -> bool; complete(con, claim, *, global_version=None) -> bool; fail(con, claim, code, *, retry, detail=None, retry_after=None) -> bool; BACKOFF; LEASE.

- [ ] **Step 1: Write the failing tests**

`fantasy-intel/backend/tests/test_sync_queue.py`:

```python
"""The Postgres queue (spec §3.3–3.4): claim/reclaim in place, per-league lock, provider slots, fence, heartbeat,
retries with backoff, duplicate enqueue collapse."""
import threading
from datetime import timedelta

import psycopg
import pytest

from app.sync import queue
from cp_seed import seed_league


def sync(pg):
    return psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None)


def su_one(pg, sql, params=()):
    with pg.connect() as su:
        return su.execute(sql, params).fetchone()


@pytest.fixture
def lg(pg):
    with pg.connect() as su:
        lid = seed_league(su)
        su.commit()
    return lid


def test_enqueue_collapses_duplicates_while_queued_or_running(pg, lg):
    with sync(pg) as con:
        first = queue.enqueue(con, lg, "manual", key=f"manual:{lg}")
        assert first and queue.enqueue(con, lg, "manual", key=f"manual:{lg}") is None
        claim = queue.claim(con, "w1")
        assert claim.job_id == first
        assert queue.enqueue(con, lg, "manual", key=f"manual:{lg}") is None, "still running"
        assert queue.complete(con, claim)
        assert queue.enqueue(con, lg, "manual", key=f"manual:{lg}") is not None, "a finished job frees the key"


def test_claim_takes_a_fence_and_a_slot_and_a_league_runs_once(pg, lg):
    with sync(pg) as con:
        queue.enqueue(con, lg, "initial", key=f"initial:{lg}")
        queue.enqueue(con, lg, "manual", key=f"manual:{lg}")
        c = queue.claim(con, "w1")
        assert (c.league_id, c.kind, c.provider, c.attempt) == (lg, "initial", "espn", 1)
        assert c.fence == su_one(pg, "SELECT sync_fence FROM fi.leagues WHERE id = %s", [lg])[0] == 1
        assert su_one(pg, "SELECT job_id::text FROM fi.provider_slots WHERE provider = 'espn' AND slot_no = %s",
                      [c.slot]) == (c.job_id,)
        assert queue.claim(con, "w2") is None, "the league already has a running job"


def test_an_expired_lease_is_reclaimed_in_place_with_a_new_fence(pg, lg):
    with sync(pg) as con:
        queue.enqueue(con, lg, "initial", key=f"initial:{lg}")
        old = queue.claim(con, "w1")
        con.execute("UPDATE fi.sync_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", [old.job_id])
        con.commit()
        new = queue.claim(con, "w2")
        assert new.job_id == old.job_id and new.fence > old.fence and new.attempt == 2
        assert su_one(pg, "SELECT count(*) FROM fi.sync_jobs WHERE league_id = %s", [lg]) == (1,)
        assert not queue.heartbeat(con, old), "the superseded worker loses its lease"
        assert queue.heartbeat(con, new)
        assert not queue.complete(con, old)
        assert su_one(pg, "SELECT status, lease_owner FROM fi.sync_jobs WHERE id = %s", [new.job_id]) == ("running", "w2")


def test_heartbeat_extends_the_lease_and_the_slot(pg, lg):
    with sync(pg) as con:
        queue.enqueue(con, lg, "initial", key=f"initial:{lg}")
        c = queue.claim(con, "w1")
        con.execute("UPDATE fi.sync_jobs SET lease_expires_at = now() + interval '5 seconds' WHERE id = %s", [c.job_id])
        con.commit()
        assert queue.heartbeat(con, c)
    left = su_one(pg, "SELECT extract(epoch FROM lease_expires_at - now()) FROM fi.sync_jobs WHERE id = %s", [c.job_id])[0]
    slot = su_one(pg, "SELECT extract(epoch FROM lease_expires_at - now()) FROM fi.provider_slots WHERE job_id = %s",
                  [c.job_id])[0]
    assert 100 < left <= 120 and 100 < slot <= 120


def test_the_provider_slot_cap_holds_under_ten_concurrent_claimers(pg):
    with pg.connect() as su:
        leagues = [seed_league(su) for _ in range(10)]
        su.commit()
    with sync(pg) as con:
        for lid in leagues:
            queue.enqueue(con, lid, "initial", key=f"initial:{lid}")
    barrier, claims, errors = threading.Barrier(10), [], []

    def worker(n):
        try:
            with sync(pg) as con:
                barrier.wait()
                c = queue.claim(con, f"w{n}")
                if c:
                    claims.append(c)
        except Exception as err:   # noqa: BLE001 - surfaced below
            errors.append(err)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert len(claims) == 2, "ESPN has two slots"
    assert len({c.slot for c in claims}) == 2
    assert su_one(pg, "SELECT count(*) FROM fi.sync_jobs WHERE status = 'running'") == (2,)


def test_a_slot_held_past_its_lease_is_reused(pg):
    with pg.connect() as su:
        a, b, c = (seed_league(su) for _ in range(3))
        su.commit()
    with sync(pg) as con:
        for lid in (a, b, c):
            queue.enqueue(con, lid, "initial", key=f"initial:{lid}")
        first, second = queue.claim(con, "w1"), queue.claim(con, "w2")
        assert first and second and queue.claim(con, "w3") is None
        con.execute("UPDATE fi.provider_slots SET lease_expires_at = now() - interval '1 second' WHERE job_id = %s",
                    [first.job_id])
        con.execute("UPDATE fi.sync_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", [first.job_id])
        con.commit()
        again = queue.claim(con, "w3")
        assert again is not None and again.slot == first.slot


def test_failures_retry_with_backoff_then_die(pg, lg):
    with sync(pg) as con:
        queue.enqueue(con, lg, "scheduled", key=f"scheduled:{lg}")
        delays = []
        for attempt in range(1, 6):
            c = queue.claim(con, "w1")
            assert c.attempt == attempt
            assert queue.fail(con, c, "espn_5xx", retry=True)
            status, wait = su_one(pg, "SELECT status, extract(epoch FROM run_after - now()) FROM fi.sync_jobs WHERE id = %s",
                                  [c.job_id])
            if attempt < 5:
                assert status == "queued"
                delays.append(wait)
                con.execute("UPDATE fi.sync_jobs SET run_after = now() WHERE id = %s", [c.job_id])
                con.commit()
            else:
                assert status == "dead"
        for wait, base in zip(delays, queue.BACKOFF):
            assert base.total_seconds() * 0.75 <= wait <= base.total_seconds() * 1.25
        assert su_one(pg, "SELECT count(*) FROM fi.provider_slots WHERE job_id IS NOT NULL") == (0,), "slots released"


def test_a_non_retryable_failure_is_final_and_the_detail_is_stored_redacted(pg, lg):
    with sync(pg) as con:
        queue.enqueue(con, lg, "scheduled", key=f"scheduled:{lg}")
        c = queue.claim(con, "w1")
        assert queue.fail(con, c, "parse_error", retry=False, detail="KeyError at write_all")
    assert su_one(pg, "SELECT status, error_code, error_detail FROM fi.sync_jobs") == ("dead", "parse_error",
                                                                                      "KeyError at write_all")


def test_a_queued_job_waits_for_its_run_after_and_deleted_leagues_are_skipped(pg):
    with pg.connect() as su:
        later, gone = seed_league(su), seed_league(su, deleted=True)
        su.commit()
    with sync(pg) as con:
        queue.enqueue(con, later, "scheduled", key=f"scheduled:{later}", run_after=timedelta(minutes=5))
        queue.enqueue(con, gone, "scheduled", key=f"scheduled:{gone}")
        assert queue.claim(con, "w1") is None


def test_claim_respects_the_lock_order_league_before_job(pg):
    """Account deletion locks a league and then (via its cascade) the league's jobs; a claim takes the same order."""
    import inspect
    src = inspect.getsource(queue.claim)
    assert src.index("FROM fi.leagues") < src.index("FROM fi.sync_jobs j WHERE j.id")
```

- [ ] **Step 2: Run them and see them fail**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_sync_queue.py -q
```
Expected: FAIL (No module named app.sync.queue).

- [ ] **Step 3: Implement**

`fantasy-intel/backend/app/sync/queue.py`:

```python
"""The sync queue in Postgres (spec §3.3–3.4), used by the GitHub Actions workers as `fi_sync`.

claim: one transaction picks a due job (or a running job whose lease expired: reclaimed in place, the same row),
locks its league, takes a free provider slot, and increments the league's fence, which becomes the job's lease
token. The fence is the only thing that lets a worker publish (app/db/league_publish.commit_league); a superseded
worker's heartbeat and completion find a different token and do nothing.

Lock order follows the control plane's: the league row, then the league's own rows (here the job), then the slot.
Every function commits on the connection it is given.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta

LEASE = timedelta(minutes=2)
BACKOFF = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=20), timedelta(hours=1), timedelta(hours=3))
JITTER = 0.2
CANDIDATES = 8          # due jobs looked at per claim attempt (skipping locked or slot-less ones)


@dataclass(frozen=True)
class Claim:
    job_id: str
    league_id: str
    kind: str
    provider: str
    fence: int
    attempt: int
    max_attempts: int
    slot: int


def enqueue(con, league_id: str, kind: str, *, key: str, run_after: timedelta | None = None) -> str | None:
    """Queue a job unless one with the same idempotency key is queued or running. Returns the new job's id, or None
    when it collapsed into an existing one."""
    with con.transaction():
        row = con.execute("""
            INSERT INTO fi.sync_jobs (league_id, kind, idempotency_key, run_after)
            VALUES (%s, %s, %s, now() + %s)
            ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL AND status IN ('queued', 'running')
            DO NOTHING RETURNING id::text""", [league_id, kind, key, run_after or timedelta(0)]).fetchone()
    return row[0] if row else None


_DUE = """
    SELECT j.id::text, j.league_id, l.provider FROM fi.sync_jobs j JOIN fi.leagues l ON l.id = j.league_id
    WHERE l.deleted_at IS NULL AND (
          (j.status = 'queued' AND j.run_after <= now()
           AND NOT EXISTS (SELECT 1 FROM fi.sync_jobs r WHERE r.league_id = j.league_id AND r.status = 'running'))
       OR (j.status = 'running' AND j.lease_expires_at < now()))
    ORDER BY j.run_after, j.created_at
    LIMIT %s"""


def claim(con, owner: str, *, providers: tuple[str, ...] = ("espn", "sleeper")) -> Claim | None:
    """Claim one due job, or return None when nothing is due or no provider slot is free."""
    full: set[str] = set()                      # providers with no free slot in this attempt
    for job_id, league_id, provider in con.execute(_DUE, [CANDIDATES]).fetchall():
        con.rollback()
        if provider in full or provider not in providers:
            continue
        with con.transaction():
            # Lock order: the league first (account deletion locks it before cascading to its jobs) ...
            if con.execute("SELECT 1 FROM fi.leagues WHERE id = %s AND deleted_at IS NULL FOR UPDATE SKIP LOCKED",
                           [league_id]).fetchone() is None:
                continue
            # ... then the job, re-checked under the lock: another worker may have taken it meanwhile.
            job = con.execute("""
                SELECT j.kind, j.attempt, j.max_attempts FROM fi.sync_jobs j WHERE j.id = %s AND (
                      (j.status = 'queued' AND j.run_after <= now()
                       AND NOT EXISTS (SELECT 1 FROM fi.sync_jobs r WHERE r.league_id = j.league_id AND r.status = 'running'))
                   OR (j.status = 'running' AND j.lease_expires_at < now()))
                FOR UPDATE SKIP LOCKED""", [job_id]).fetchone()
            if job is None:
                continue
            # Provider cap: row locks on the slots make it race-safe (counting running rows would not be).
            slot = con.execute("""
                SELECT slot_no FROM fi.provider_slots
                WHERE provider = %s AND (job_id IS NULL OR job_id = %s OR lease_expires_at < now())
                ORDER BY slot_no FOR UPDATE SKIP LOCKED LIMIT 1""", [provider, job_id]).fetchone()
            if slot is None:            # leaving the block commits a transaction that changed nothing
                full.add(provider)
                continue
            fence = con.execute("UPDATE fi.leagues SET sync_fence = sync_fence + 1 WHERE id = %s RETURNING sync_fence",
                                [league_id]).fetchone()[0]
            con.execute("""
                UPDATE fi.sync_jobs SET status = 'running', lease_owner = %s, lease_token = %s,
                       lease_expires_at = now() + %s, heartbeat_at = now(), attempt = attempt + 1, provider_slot = %s,
                       started_at = coalesce(started_at, now()), error_code = NULL, error_detail = NULL
                WHERE id = %s""", [owner, fence, LEASE, slot[0], job_id])
            con.execute("UPDATE fi.provider_slots SET job_id = %s, lease_expires_at = now() + %s "
                        "WHERE provider = %s AND slot_no = %s", [job_id, LEASE, provider, slot[0]])
            return Claim(job_id, league_id, job[0], provider, fence, job[1] + 1, job[2], slot[0])
    con.rollback()
    return None


def heartbeat(con, c: Claim) -> bool:
    """Extend the lease and the slot. False: the lease is lost (reclaimed by another worker); abort, never publish."""
    with con.transaction():
        n = con.execute("""UPDATE fi.sync_jobs SET lease_expires_at = now() + %s, heartbeat_at = now()
                           WHERE id = %s AND lease_token = %s AND status = 'running'""", [LEASE, c.job_id, c.fence]).rowcount
        if n:
            con.execute("UPDATE fi.provider_slots SET lease_expires_at = now() + %s "
                        "WHERE provider = %s AND slot_no = %s AND job_id = %s", [LEASE, c.provider, c.slot, c.job_id])
    return n == 1


def _finish(con, c: Claim, sql: str, params: list) -> bool:
    with con.transaction():
        n = con.execute(sql + " WHERE id = %s AND lease_token = %s AND status = 'running'",
                        [*params, c.job_id, c.fence]).rowcount
        if n:
            con.execute("UPDATE fi.provider_slots SET job_id = NULL, lease_expires_at = NULL WHERE job_id = %s",
                        [c.job_id])
    return n == 1


def complete(con, c: Claim, *, global_version: str | None = None) -> bool:
    return _finish(con, c, """UPDATE fi.sync_jobs SET status = 'succeeded', finished_at = now(), error_code = NULL,
                              error_detail = NULL, lease_expires_at = NULL, global_version = coalesce(%s, global_version)""",
                   [global_version])


def backoff(attempt: int) -> timedelta:
    base = BACKOFF[min(attempt, len(BACKOFF)) - 1]
    return base * (1 + random.uniform(-JITTER, JITTER))


def fail(con, c: Claim, code: str, *, retry: bool, detail: str | None = None, retry_after: timedelta | None = None) -> bool:
    """Record a failure. `retry`: queue the job again after the backoff for this attempt (or `retry_after`, e.g. a
    Retry-After), until max_attempts, then `dead`. Not retryable: `dead` at once. `detail` must already be redacted
    (app/sync/redact.py)."""
    if retry and c.attempt < c.max_attempts:
        delay = retry_after or backoff(c.attempt)
        return _finish(con, c, """UPDATE fi.sync_jobs SET status = 'queued', run_after = now() + %s, error_code = %s,
                                  error_detail = %s, lease_expires_at = NULL, lease_owner = NULL""",
                       [delay, code, detail])
    return _finish(con, c, """UPDATE fi.sync_jobs SET status = 'dead', finished_at = now(), error_code = %s,
                              error_detail = %s, lease_expires_at = NULL""", [code, detail])
```

- [ ] **Step 4: Run them and see them pass**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_sync_queue.py -q
```
Expected: 10 passed (repeat the concurrency test a few times: 2 claims every run).

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/backend/app/sync/__init__.py fantasy-intel/backend/app/sync/queue.py fantasy-intel/backend/tests/test_sync_queue.py
git commit -m "Phase 3 task 2: the sync queue (claim/reclaim in place, slots, fence, heartbeat, backoff)"
```


### Task 3: Failure classification

**Files:**
- Create: `fantasy-intel/backend/app/sync/failures.py`
- Create: `fantasy-intel/backend/tests/test_sync_failures.py`

**Interfaces:**
- Produces: Outcome(action in retry|auth|private|missing|dead, code, pause_provider=False); classify(err, *, authenticated, stage='build') -> Outcome. Codes are fixed strings; the exception message is never kept.

- [ ] **Step 1: Write the failing tests**

`fantasy-intel/backend/tests/test_sync_failures.py`:

```python
"""Failure classification (spec §3.6): what a job does with each kind of error."""
import psycopg
import pytest
import requests
from espn_api.requests.espn_requests import ESPNAccessDenied, ESPNInvalidLeague, ESPNUnknownError

from app.analytics.validation import ValidationError
from app.db.r2 import R2Error
from app.sync.failures import Outcome, classify


@pytest.mark.parametrize("err, authenticated, expected", [
    (requests.ConnectionError("dns"), False, Outcome("retry", "network")),
    (requests.Timeout("slow"), True, Outcome("retry", "network")),
    (ESPNUnknownError("ESPN returned an HTTP 503"), False, Outcome("retry", "provider_5xx")),
    (RuntimeError("ESPN returned HTTP 502"), False, Outcome("retry", "provider_5xx")),
    (ESPNUnknownError("ESPN returned an HTTP 429"), False, Outcome("retry", "provider_429", pause_provider=True)),
    (R2Error(503, "SlowDown", "k"), False, Outcome("retry", "storage")),
    (R2Error(None, "ChecksumMismatch", "k"), False, Outcome("retry", "storage")),
    (psycopg.OperationalError("conn reset"), False, Outcome("retry", "control_plane")),
    # an authenticated 401/403 is ambiguous: the credential is suspect, the job retries
    (ESPNAccessDenied("League 1 cannot be accessed with the provided credentials"), True, Outcome("auth", "provider_auth")),
    (ESPNUnknownError("ESPN returned an HTTP 403"), True, Outcome("auth", "provider_auth")),
    # without cookies it is the provider saying the league is private: restrict (safe direction)
    (ESPNAccessDenied("espn_s2 and swid are required"), False, Outcome("private", "provider_private")),
    (ESPNInvalidLeague("League 1 does not exist"), True, Outcome("missing", "provider_not_found")),
    (ESPNInvalidLeague("League 1 does not exist"), False, Outcome("missing", "provider_not_found")),
    (KeyError("teams"), False, Outcome("dead", "parse_error")),
    (TypeError("NoneType"), False, Outcome("dead", "parse_error")),
    (ValidationError([]), False, Outcome("dead", "validation_error")),
    (ZeroDivisionError(), False, Outcome("retry", "unexpected")),
])
def test_every_row_of_the_failure_table(err, authenticated, expected):
    assert classify(err, authenticated=authenticated) == expected


def test_parse_errors_are_only_final_when_they_come_from_our_parsing():
    """A KeyError raised by the HTTP layer (a malformed response mid-transfer) is still a provider problem."""
    assert classify(KeyError("x"), authenticated=False, stage="fetch") == Outcome("retry", "provider_response")
    assert classify(KeyError("x"), authenticated=False, stage="build") == Outcome("dead", "parse_error")


def test_the_outcome_never_carries_the_error_message():
    out = classify(ESPNAccessDenied("League 123456 cannot be accessed"), authenticated=True)
    assert "123456" not in repr(out)
```

- [ ] **Step 2: Run them and see them fail**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_sync_failures.py -q
```
Expected: FAIL (No module named app.sync.failures).

- [ ] **Step 3: Implement**

`fantasy-intel/backend/app/sync/failures.py`:

```python
"""Spec §3.6 failure classification: what a sync job does with an error. Pure: no I/O.

    retry    network, 5xx, 429 (also pauses the provider), R2 / Postgres transient, anything unknown: backoff, then dead
    auth     authenticated 401/403 on a private league: ambiguous; Postgres decides suspect vs invalid
             (fi.record_credential_failure: >= 3 over >= 6 h with a control success), the job retries meanwhile
    private  401/403 without cookies: the provider says the league is private; restrict it now (the safe direction)
    missing  404 league not found: Postgres decides when it is `unavailable` (same rule); data is kept
    dead     our parsing or validation failed: retrying cannot help; the previous data stays served

An Outcome holds a fixed error code and never the exception's message: provider messages carry league ids and the
drain's logs are public.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import requests

_HTTP = re.compile(r"HTTP (\d{3})")


@dataclass(frozen=True)
class Outcome:
    action: str                 # retry | auth | private | missing | dead
    code: str
    pause_provider: bool = False


def _status(err: BaseException) -> int | None:
    m = _HTTP.search(str(err))
    return int(m[1]) if m else None


def classify(err: BaseException, *, authenticated: bool, stage: str = "build") -> Outcome:
    """`authenticated`: the request carried the league's cookies. `stage`: "fetch" (talking to the provider),
    "build" (our parsing, writing and analytics) or "publish"."""
    from espn_api.requests.espn_requests import ESPNAccessDenied, ESPNInvalidLeague, ESPNUnknownError

    from ..analytics.validation import ValidationError
    from ..db.r2 import R2Error

    if isinstance(err, (requests.ConnectionError, requests.Timeout)):
        return Outcome("retry", "network")
    if isinstance(err, ESPNInvalidLeague):
        return Outcome("missing", "provider_not_found")
    status = _status(err) if isinstance(err, (ESPNUnknownError, RuntimeError)) else None
    if isinstance(err, ESPNAccessDenied) or status in (401, 403):
        return Outcome("auth", "provider_auth") if authenticated else Outcome("private", "provider_private")
    if status == 429:
        return Outcome("retry", "provider_429", pause_provider=True)
    if status is not None and status >= 500:
        return Outcome("retry", "provider_5xx")
    if isinstance(err, R2Error):
        return Outcome("retry", "storage")
    try:
        import psycopg
        if isinstance(err, psycopg.OperationalError):
            return Outcome("retry", "control_plane")
    except ImportError:   # pragma: no cover - psycopg is a dependency of the sync group
        pass
    if isinstance(err, ValidationError):
        return Outcome("dead", "validation_error")
    if isinstance(err, (KeyError, TypeError, ValueError, IndexError)):
        return Outcome("retry", "provider_response") if stage == "fetch" else Outcome("dead", "parse_error")
    return Outcome("retry", "unexpected")
```

- [ ] **Step 4: Run them and see them pass**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_sync_failures.py -q
```
Expected: 19 passed.

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/backend/app/sync/failures.py fantasy-intel/backend/tests/test_sync_failures.py
git commit -m "Phase 3 task 3: failure classification (spec §3.6), codes only, never messages"
```


### Task 4: Scoped ESPN access and the raw payload store

**Files:**
- Create: `fantasy-intel/backend/app/sync/espn_source.py`
- Create: `fantasy-intel/backend/app/sync/raw_store.py`
- Create: `fantasy-intel/backend/tests/test_sync_espn_source.py`
- Modify: `fantasy-intel/backend/app/espn/parse.py`

**Interfaces:**
- Produces: espn_source.cookie_allowed(url, league_id, season) -> bool; espn_client(league_id, season, credential|None) context manager -> EspnClient; control_request(season) -> bool; probe(league_id, season) -> 'public'|'private'|'missing'|'unknown'; FANTASY.
- Produces: raw_store.scrub(payloads) -> payloads; dump_payloads(payloads) -> bytes (gzip JSON, SWIDs hashed); load_payloads(bytes) -> EspnPayloads; FORMAT.
- Changes: parse.anon_member is idempotent (an existing 'm_<12 hex>' id is returned unchanged).

- [ ] **Step 1: Write the failing tests**

`fantasy-intel/backend/tests/test_sync_espn_source.py`:

```python
"""ESPN access for sync jobs: cookies reach only the league's own read endpoint; the control request and the
visibility probe carry none; raw payloads round-trip with every member SWID scrubbed."""
import gzip
import json

import pytest
import requests

from app.espn.parse import anon_member
from app.sync import espn_source, raw_store
from app.sync.espn_source import FANTASY, cookie_allowed

LG, SEASON = 4242, 2026
CRED = {"espn_s2": "S2SECRETVALUE", "swid": "{AAAA-BBBB}"}


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.headers = status, body if body is not None else {}, {}

    def json(self):
        return self._body


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_get(url, params=None, headers=None, cookies=None, timeout=None, **kw):
        seen.append({"url": url, "cookies": cookies, "timeout": timeout})
        return Resp(200, {"id": LG, "seasonId": SEASON, "players": []} if "/leagues/" in url else [])

    monkeypatch.setattr(requests, "get", fake_get)
    return seen


@pytest.mark.parametrize("url, ok", [
    (f"{FANTASY}/seasons/{SEASON}/segments/0/leagues/{LG}", True),
    (f"{FANTASY}/seasons/{SEASON}/segments/0/leagues/{LG}/communication/", True),
    (f"{FANTASY}/leagueHistory/{LG}?seasonId={SEASON}", True),
    (f"{FANTASY}/seasons/{SEASON}/segments/0/leagues/{LG}9", False),          # another league sharing a prefix
    (f"{FANTASY}/seasons/{SEASON + 1}/segments/0/leagues/{LG}", False),       # another season
    (f"{FANTASY}/seasons/{SEASON}/players", False),                            # pro players: public, no cookies
    (f"https://evil.example/apis/v3/games/ffl/seasons/{SEASON}/segments/0/leagues/{LG}", False),
])
def test_the_cookie_allowlist(url, ok):
    assert cookie_allowed(url, LG, SEASON) is ok


def test_a_private_client_sends_cookies_only_to_the_league_endpoint(calls):
    with espn_source.espn_client(LG, SEASON, CRED) as client:
        client.league()
        client.pro_players()
    league, players = calls
    assert "/leagues/4242" in league["url"] and league["cookies"] == {"espn_s2": "S2SECRETVALUE", "SWID": "{AAAA-BBBB}"}
    assert players["url"].endswith("/players") and players["cookies"] is None
    assert all(c["timeout"] for c in calls), "every ESPN request has a timeout"


def test_the_scope_is_removed_after_the_job(calls):
    from espn_api.requests import espn_requests

    from app.espn import client as client_mod
    with espn_source.espn_client(LG, SEASON, None):
        pass
    assert espn_requests.requests is requests and client_mod.requests is requests


def test_control_and_probe_never_carry_cookies(calls, monkeypatch):
    assert espn_source.control_request(SEASON) is True
    assert espn_source.probe(LG, SEASON) == "public"
    assert all(c["cookies"] is None for c in calls)
    for status, verdict in ((401, "private"), (403, "private"), (404, "missing"), (500, "unknown")):
        monkeypatch.setattr(requests, "get", lambda *a, status=status, **k: Resp(status))
        assert espn_source.probe(LG, SEASON) == verdict

    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(requests, "get", boom)
    assert espn_source.probe(LG, SEASON) == "unknown" and espn_source.control_request(SEASON) is False


def test_member_ids_hash_once():
    once = anon_member("{AAAA-BBBB}")
    assert once.startswith("m_") and anon_member(once) == once


def test_raw_payloads_round_trip_with_swids_scrubbed():
    from datetime import datetime

    from app.espn.sync import EspnPayloads
    p = EspnPayloads(league={"id": LG, "members": [{"id": "{AAAA-BBBB}"}], "teams": [{"owners": ["{AAAA-BBBB}"]}]},
                     rosters={1: {"r": 1}}, transactions={2: {"t": 2}}, activity=[{"a": 1}], projections={3: {"p": 3}},
                     projection_weeks=[3], projection_failures={4: "ESPN projections week 4 failed"},
                     fetch_started_at=datetime(2026, 10, 21, 12, 0), fetched_at=datetime(2026, 10, 21, 12, 1))
    blob = raw_store.dump_payloads(p)
    assert b"AAAA-BBBB" not in gzip.decompress(blob)
    back = raw_store.load_payloads(blob)
    assert back.league["members"][0]["id"] == anon_member("{AAAA-BBBB}")
    assert (back.rosters, back.transactions, back.projections) == ({1: {"r": 1}}, {2: {"t": 2}}, {3: {"p": 3}})
    assert back.projection_failures == {4: "ESPN projections week 4 failed"} and back.projection_weeks == [3]
    assert back.fetched_at == datetime(2026, 10, 21, 12, 1) and back.warnings == []
    assert json.loads(gzip.decompress(blob))["format"] == raw_store.FORMAT
```

- [ ] **Step 2: Run them and see them fail**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_sync_espn_source.py -q
```
Expected: FAIL (No module named app.sync.espn_source).

- [ ] **Step 3: Implement**

`fantasy-intel/backend/app/sync/espn_source.py`:

```python
"""ESPN access for sync jobs (spec §2 "Credential security").

A private league's cookies are account-wide session cookies. While a job runs, `espn_client` swaps the `requests`
module that espn_api and app.espn.client use for a scoped stand-in: a request keeps its cookies only when its URL
is that league's own read endpoint (`…/seasons/<season>/segments/0/leagues/<id>…` or `…/leagueHistory/<id>…`);
every other request (pro players, cards on another path) goes out without them. Every request gets a timeout.

`control_request` and `probe` never carry cookies: the control request shows ESPN is reachable (spec §3.6) and the
probe reads a league's visibility as an anonymous visitor would (spec §2 "Dynamic visibility").
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import requests

FANTASY = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
TIMEOUT = 60


def _prefixes(league_id: int, season: int) -> tuple[str, ...]:
    return (f"{FANTASY}/seasons/{season}/segments/0/leagues/{league_id}", f"{FANTASY}/leagueHistory/{league_id}")


def cookie_allowed(url: str, league_id: int, season: int) -> bool:
    base = url.split("?", 1)[0]
    return any(base == p or base.startswith(p + "/") for p in _prefixes(league_id, season))


class _ScopedRequests:
    """The `requests` module, except that cookies reach only the league's endpoint and nothing waits forever."""

    def __init__(self, league_id: int, season: int):
        self.league_id, self.season = league_id, season

    def get(self, url, *args, cookies=None, timeout=None, **kwargs):
        if cookies and not cookie_allowed(url, self.league_id, self.season):
            cookies = None
        return requests.get(url, *args, cookies=cookies, timeout=timeout or TIMEOUT, **kwargs)

    def __getattr__(self, name):
        return getattr(requests, name)


@contextmanager
def espn_client(league_id: int, season: int, credential: dict | None) -> Iterator:
    """An EspnClient for one league; `credential` = {"espn_s2", "swid"} for a private league, None for a public one.
    The worker runs one job at a time per process, so the module swap is process-wide only for that job."""
    from espn_api.requests import espn_requests

    from ..espn import client as client_mod
    scoped = _ScopedRequests(int(league_id), int(season))
    saved = espn_requests.requests, client_mod.requests
    espn_requests.requests = client_mod.requests = scoped
    try:
        cred = credential or {}
        yield client_mod.EspnClient(int(league_id), int(season), cred.get("espn_s2"), cred.get("swid"))
    finally:
        espn_requests.requests, client_mod.requests = saved


def control_request(season: int) -> bool:
    """A cookie-less GET of ESPN's public player list: True when ESPN answered normally."""
    try:
        r = requests.get(f"{FANTASY}/seasons/{int(season)}/players", params={"view": "players_wl"},
                         headers={"x-fantasy-filter": '{"filterActive":{"value":true}}'}, timeout=30)
        return r.status_code == 200
    except requests.RequestException:
        return False


def probe(league_id: int, season: int) -> str:
    """The league as an anonymous visitor sees it: "public", "private" (401/403), "missing" (404) or "unknown"
    (anything else, including a network error: callers keep a league closed on "unknown")."""
    try:
        r = requests.get(_prefixes(int(league_id), int(season))[0], params={"view": "mSettings"}, timeout=30)
    except requests.RequestException:
        return "unknown"
    return {200: "public", 401: "private", 403: "private", 404: "missing"}.get(r.status_code, "unknown")
```

`fantasy-intel/backend/app/sync/raw_store.py`:

```python
"""Raw provider payloads kept next to each league version (`raw/<lg>/<data_version>.json.gz`, spec §3.7), so a
`recompute` job can rebuild the league file against a new global file without fetching. Member SWIDs are hashed
before anything is serialised (app/espn/parse.scrub_member_ids); the job writes from the scrubbed payloads too, so
a fresh build and a recompute produce the same rows."""

from __future__ import annotations

import gzip
import json
from datetime import datetime
from typing import Any

from ..espn.parse import scrub_member_ids
from ..espn.sync import EspnPayloads

FORMAT = "espn-payloads-v1"
_INT_KEYED = ("rosters", "transactions", "projections", "projection_failures")
_TIMES = ("fetch_started_at", "fetched_at")


def scrub(p: EspnPayloads) -> EspnPayloads:
    """The payloads with every SWID-shaped string replaced by its member hash (in place; returns `p`)."""
    for name in ("league", "pro_players", "pool", "draft"):
        setattr(p, name, scrub_member_ids(getattr(p, name)))
    for name in ("rosters", "transactions", "projections"):
        setattr(p, name, {k: scrub_member_ids(v) for k, v in getattr(p, name).items()})
    p.activity = scrub_member_ids(p.activity)
    p.cards = scrub_member_ids(p.cards)
    return p


def dump_payloads(p: EspnPayloads) -> bytes:
    scrub(p)
    doc: dict[str, Any] = {"format": FORMAT}
    for name in ("league", "pro_players", "pool", "draft", "activity", "cards", "projection_weeks"):
        doc[name] = getattr(p, name)
    for name in _INT_KEYED:
        doc[name] = {str(k): v for k, v in getattr(p, name).items()}
    for name in _TIMES:
        value = getattr(p, name)
        doc[name] = value.isoformat() if value is not None else None
    return gzip.compress(json.dumps(doc, separators=(",", ":")).encode(), mtime=0)


def load_payloads(blob: bytes) -> EspnPayloads:
    doc = json.loads(gzip.decompress(blob))
    if doc.get("format") != FORMAT:
        raise ValueError(f"unknown raw payload format {doc.get('format')!r}")
    p = EspnPayloads(league=doc["league"])
    for name in ("pro_players", "pool", "draft"):
        setattr(p, name, doc.get(name))
    p.activity, p.cards = doc.get("activity") or [], doc.get("cards") or []
    p.projection_weeks = doc.get("projection_weeks") or []
    for name in _INT_KEYED:
        setattr(p, name, {int(k): v for k, v in (doc.get(name) or {}).items()})
    for name in _TIMES:
        setattr(p, name, datetime.fromisoformat(doc[name]) if doc.get(name) else None)
    return p
```

In `app/espn/parse.py`, `anon_member` returns an id matching `^m_[0-9a-f]{12}$` unchanged.

- [ ] **Step 4: Run them and see them pass**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_sync_espn_source.py tests/test_league_providers_espn.py tests/test_db_and_sync.py -q
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/backend/app/sync/espn_source.py fantasy-intel/backend/app/sync/raw_store.py fantasy-intel/backend/tests/test_sync_espn_source.py fantasy-intel/backend/app/espn/parse.py
git commit -m "Phase 3 task 4: scoped ESPN access for jobs (cookie allowlist, control request, probe) and raw payload store"
```


### Task 5: Build a league file on the runner

**Files:**
- Create: `fantasy-intel/backend/app/sync/build.py`
- Create: `fantasy-intel/backend/tests/test_sync_build.py`
- Modify: `fantasy-intel/backend/app/espn/sync.py`

**Interfaces:**
- Consumes: raw_store.scrub, split_database, LEAGUE_TABLES, run_analytics.
- Produces: Built(league_path, league_version, external_id, season, name); build_league_file(work_dir, *, global_path, previous, payloads, owner_swid, stamp) -> Built; league_tables_equal(a, b, *, ignore_columns, ignore_tables) -> list[str]; espn.sync.write_synced(con, payloads, *, swid, my_team_id, before_write=None) -> stats (the write half of run_espn_sync, unchanged).

- [ ] **Step 1: Write the failing tests**

`fantasy-intel/backend/tests/test_sync_build.py`:

```python
"""A league file built by a sync job equals what the laptop path produces from the same data (spec §3.5)."""
import shutil
from types import SimpleNamespace

import duckdb
import pytest

from app.db.connection import open_db
from app.db.league_split import split_database
from app.espn.sync import fetch_all, run_espn_sync
from app.sync.build import build_league_file, league_tables_equal
from espn_fixture import MY_SWID, SEASON, FakeEspnClient
from golden.golden import build_e2e_db

# wall-clock stamps and per-run ids: different on every run of either path
VOLATILE = frozenset({"synced_at", "detail_synced_at", "projection_synced_at", "run_id", "sync_run_id", "created_at", "computed_at", "started_at", "finished_at",
                      "retrieved_at", "fetch_started_at", "generated_at", "updated_at", "snapshot_date", "as_of", "retrieval_id", "recorded_at"})
IGNORE = frozenset({"sync_runs", "schema_meta"})


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    """The E2E fixture league split into its league and global files: what is published before the job runs."""
    d = tmp_path_factory.mktemp("build")
    db = build_e2e_db(d / "e2e.duckdb")
    out = split_database(db, d / "prev.duckdb", d / "global.duckdb", stamp="2026-10-21T10:00:00",
                         global_stamp="content-addressed")
    return {"db": db, "prev": out.league_path, "global": out.global_path, "dir": d}


def laptop_refresh(base, tmp_path):
    """What the laptop does: sync into the full database, run analytics, split."""
    from app.analytics.pipeline import run_analytics
    db = tmp_path / "laptop.duckdb"
    shutil.copy(base["db"], db)
    run_espn_sync(lambda: open_db(db), SimpleNamespace(swid=MY_SWID, my_team_id=None), client=FakeEspnClient(),
                  save_raw_payloads=False)
    with open_db(db) as con:
        run_analytics(con, SEASON)
    return split_database(db, tmp_path / "laptop_league.duckdb", tmp_path / "laptop_global.duckdb",
                          stamp="2026-10-21T12:00:00").league_path


def test_a_job_build_equals_the_laptop_refresh_table_by_table(base, tmp_path):
    reference = laptop_refresh(base, tmp_path)
    work = tmp_path / "job"
    work.mkdir()
    built = build_league_file(work, global_path=base["global"], previous=base["prev"],
                              payloads=fetch_all(FakeEspnClient()), owner_swid=MY_SWID, stamp="2026-10-21T12:00:00")
    assert (built.external_id, built.season) == (999, SEASON)
    assert league_tables_equal(reference, built.league_path, ignore_columns=VOLATILE, ignore_tables=IGNORE) == []
    con = duckdb.connect(str(built.league_path), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM fantasy_teams WHERE is_mine").fetchone() == (1,), \
            "the credential owner's team is the stored default perspective, as on the laptop"
        assert "MY_SWID" not in str(con.execute("SELECT * FROM league_members").fetchall())
    finally:
        con.close()


def test_tables_the_job_does_not_rebuild_carry_forward(base, tmp_path):
    con = duckdb.connect(str(base["prev"]), read_only=True)
    try:
        before = con.execute("SELECT count(*) FROM projection_performance_reports").fetchone()[0], \
            con.execute("SELECT count(*) FROM independent_projection_runs").fetchone()[0]
    finally:
        con.close()
    work = tmp_path / "job"
    work.mkdir()
    built = build_league_file(work, global_path=base["global"], previous=base["prev"],
                              payloads=fetch_all(FakeEspnClient()), owner_swid=None, stamp="2026-10-21T12:00:00")
    con = duckdb.connect(str(built.league_path), read_only=True)
    try:
        assert (con.execute("SELECT count(*) FROM projection_performance_reports").fetchone()[0],
                con.execute("SELECT count(*) FROM independent_projection_runs").fetchone()[0]) == before
        assert con.execute("SELECT count(*) FROM fantasy_teams WHERE is_mine").fetchone() == (0,), "public: no owner"
    finally:
        con.close()


def test_a_first_build_needs_no_previous_file(base, tmp_path):
    work = tmp_path / "job"
    work.mkdir()
    built = build_league_file(work, global_path=base["global"], previous=None, payloads=fetch_all(FakeEspnClient()),
                              owner_swid=None, stamp="2026-10-21T12:00:00")
    con = duckdb.connect(str(built.league_path), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM fantasy_teams").fetchone()[0] == 4
        assert {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()} >= {"leagues", "matchups"}
    finally:
        con.close()
    assert sorted(p.name for p in work.iterdir()) == ["league.duckdb"], "only the league file is left"


def test_a_failed_build_raises_and_publishes_nothing(base, tmp_path):
    payloads = fetch_all(FakeEspnClient())
    del payloads.league["id"]
    work = tmp_path / "job"
    work.mkdir()
    with pytest.raises(KeyError):
        build_league_file(work, global_path=base["global"], previous=base["prev"], payloads=payloads, owner_swid=None,
                          stamp="2026-10-21T12:00:00")
    assert not (work / "league.duckdb").exists()
```

- [ ] **Step 2: Run them and see them fail**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_sync_build.py -q
```
Expected: FAIL (No module named app.sync.build).

- [ ] **Step 3: Implement**

`fantasy-intel/backend/app/sync/build.py`:

```python
"""Build one league's file on the runner (spec §3.5), from payloads already fetched (no network here).

    working DB = a copy of the current global file
               + the league's previous league file (same season): tables a job does not rebuild carry forward
               + the ESPN payloads (write_synced: write_all + injury retrieval log), member SWIDs scrubbed
               + run_analytics (the laptop's pipeline, unchanged)
    -> split_database -> the league file. The runner's copy of the global tables is discarded: readers attach the
       published global file the job read, whose version the pointer names.

Any exception leaves nothing behind but the temporary directory the caller owns; nothing is visible until the
fenced pointer swap (app/db/league_publish.commit_league).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import duckdb

from ..db.connection import connect, init_db
from ..db.league_split import LEAGUE_TABLES, split_database
from ..espn.parse import anon_member
from ..espn.sync import EspnPayloads, write_synced
from .raw_store import scrub


@dataclass(frozen=True)
class Built:
    league_path: Path
    league_version: str
    external_id: int
    season: int
    name: str | None


def _q(path: Path) -> str:
    return str(path).replace("'", "''")


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _carry_forward(con, previous: Path, season: int) -> int:
    """Copy the previous league file's rows into the working DB (by column name). A previous file from another
    season starts the league over: a new season is a new league on the provider."""
    con.execute(f"ATTACH '{_q(previous)}' AS prev (READ_ONLY)")
    try:
        row = con.execute("SELECT max(season) FROM prev.main.leagues").fetchone()
        if not row or row[0] != season:
            return 0
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'prev' AND schema_name = 'main'").fetchall()]
        copied = 0
        for t in sorted(set(tables) & LEAGUE_TABLES):
            con.execute(f"INSERT INTO main.{_ident(t)} BY NAME SELECT * FROM prev.main.{_ident(t)}")
            copied += 1
        return copied
    finally:
        con.execute("DETACH prev")


def build_league_file(work_dir: Path, *, global_path: Path, previous: Path | None, payloads: EspnPayloads,
                      owner_swid: str | None, stamp: str) -> Built:
    """`owner_swid`: the credential owner's SWID for a private league (marks their team as the stored default
    perspective, as on the laptop), None for a public one. `stamp`: the version's UTC time (ISO)."""
    from ..analytics.pipeline import run_analytics

    work_dir = Path(work_dir)
    work = work_dir / "work.duckdb"
    shutil.copyfile(global_path, work)
    scrub(payloads)
    season = int(payloads.league["seasonId"])
    con = connect(work)
    try:
        init_db(con)
        if previous is not None:
            _carry_forward(con, Path(previous), season)
        write_synced(con, payloads, swid=anon_member(owner_swid) if owner_swid else None, my_team_id=None)
        run_analytics(con, season)
        con.execute("CHECKPOINT")
    finally:
        con.close()
    league_out = work_dir / "league.duckdb"
    out = split_database(work, league_out, work_dir / "global-unused.duckdb", stamp=stamp,
                         league=(int(payloads.league["id"]), season))
    (work_dir / "global-unused.duckdb").unlink(missing_ok=True)
    work.unlink(missing_ok=True)
    name = (payloads.league.get("settings") or {}).get("name")
    return Built(out.league_path, out.league_version, int(payloads.league["id"]), season, name)


def league_tables_equal(a: Path, b: Path, *, ignore_columns: frozenset[str], ignore_tables: frozenset[str]) -> list[str]:
    """Tables whose rows differ between two league files, ignoring the given columns and tables (tests and the local
    job-vs-laptop check use it)."""
    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{_q(a)}' AS a (READ_ONLY)")
        con.execute(f"ATTACH '{_q(b)}' AS b (READ_ONLY)")
        names = {d: {r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = ? AND schema_name = 'main'", [d]).fetchall()}
            for d in ("a", "b")}
        differ = sorted((names["a"] ^ names["b"]) - ignore_tables)
        for t in sorted((names["a"] & names["b"]) - ignore_tables):
            cols = [r[0] for r in con.execute(
                "SELECT column_name FROM duckdb_columns() WHERE database_name = 'a' AND schema_name = 'main' "
                "AND table_name = ? ORDER BY column_index", [t]).fetchall() if r[0] not in ignore_columns]
            sel = ", ".join(_ident(c) for c in cols)
            diff = con.execute(f"SELECT count(*) FROM ((SELECT {sel} FROM a.main.{_ident(t)} EXCEPT ALL "
                               f"SELECT {sel} FROM b.main.{_ident(t)}) UNION ALL (SELECT {sel} FROM b.main.{_ident(t)} "
                               f"EXCEPT ALL SELECT {sel} FROM a.main.{_ident(t)}))").fetchone()[0]
            if diff:
                differ.append(t)
        return differ
    finally:
        con.close()
```

In `app/espn/sync.py`, move the body after `fetch_all` in `run_espn_sync` into `write_synced` and call it.

- [ ] **Step 4: Run them and see them pass**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_sync_build.py tests/test_db_and_sync.py -q
```
Expected: all pass: a job build equals the laptop refresh table by table (timestamps and run ids masked).

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/backend/app/sync/build.py fantasy-intel/backend/tests/test_sync_build.py fantasy-intel/backend/app/espn/sync.py
git commit -m "Phase 3 task 5: build a league file on the runner (global + previous version + payloads + analytics)"
```


### Task 6: Publish a job's build with its claim fence

**Files:**
- Create: `fantasy-intel/backend/app/sync/publish.py`
- Create: `fantasy-intel/backend/tests/test_sync_publish.py`
- Modify: `fantasy-intel/backend/app/db/league_publish.py`

**Interfaces:**
- Consumes: commit_league (Task 1), Claim (Task 2), Built (Task 5).
- Produces: raw_key(league_id, data_version) -> 'raw/<lg>/<v>.json.gz'; publish_job(writer, sync_con, claim, built, *, raw, global_version) -> (applied, deleted). _collect deletes a version's raw payloads with its league object; DirWriter.keys lists every file.

- [ ] **Step 1: Write the failing tests**

`fantasy-intel/backend/tests/test_sync_publish.py`:

```python
"""A job's publish is the fenced pointer swap with the fence it got at claim time (spec §3.4): stale fences publish
nothing, a new league's first publish inserts its pointer, racing first publishes keep the higher fence, and a
failure before the swap changes nothing visible."""
import psycopg
import pytest

from app.db.league_files import league_key
from app.db.league_publish import DirWriter
from app.sync import queue
from app.sync.build import Built
from app.sync.publish import publish_job, raw_key
from cp_seed import seed_league

GV = "20261021T100000Z-" + "a" * 24 + "-100"


def sync(pg):
    return psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None)


def pointer(pg, lid):
    with pg.connect() as su:
        return su.execute("SELECT data_version, fence FROM fi.league_snapshots WHERE league_id = %s", [lid]).fetchone()


def fake_built(tmp_path, n: int) -> Built:
    path = tmp_path / f"league{n}.duckdb"
    path.write_bytes(b"x" * (100 + n))
    return Built(path, f"20261021T12000{n}Z-{n:024x}-{100 + n}", 999, 2026, "League")


@pytest.fixture
def lg(pg):
    with pg.connect() as su:
        lid = seed_league(su)
        su.commit()
    return lid


def claim_one(con, lid, kind="initial"):
    queue.enqueue(con, lid, kind, key=f"{kind}:{lid}")
    return queue.claim(con, "w1")


def test_a_new_leagues_first_publish_inserts_its_pointer_and_raw_payloads(pg, lg, tmp_path):
    writer = DirWriter(tmp_path / "objects")
    with sync(pg) as con:
        c = claim_one(con, lg)
        built = fake_built(tmp_path, 1)
        applied, _ = publish_job(writer, con, c, built, raw=b"payloads", global_version=GV)
    assert applied and pointer(pg, lg) == (built.league_version, c.fence)
    assert (tmp_path / "objects" / league_key(lg, built.league_version)).exists()
    assert (tmp_path / "objects" / raw_key(lg, built.league_version)).read_bytes() == b"payloads"


def test_a_stale_fence_publishes_nothing_and_removes_its_objects(pg, lg, tmp_path):
    writer = DirWriter(tmp_path / "objects")
    with sync(pg) as con:
        old = claim_one(con, lg)
        con.execute("UPDATE fi.sync_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", [old.job_id])
        con.commit()
        new = queue.claim(con, "w2")
        first = fake_built(tmp_path, 2)
        assert publish_job(writer, con, new, first, raw=b"new", global_version=GV)[0]
        late = fake_built(tmp_path, 3)
        applied, _ = publish_job(writer, con, old, late, raw=b"old", global_version=GV)
    assert not applied and pointer(pg, lg) == (first.league_version, new.fence)
    assert not (tmp_path / "objects" / league_key(lg, late.league_version)).exists()
    assert not (tmp_path / "objects" / raw_key(lg, late.league_version)).exists()


def test_racing_first_publishes_keep_the_higher_fence(pg, lg, tmp_path):
    """Two workers both hold an initial job's fence (the first lost its lease mid-build); whichever commits last,
    the pointer ends on the higher fence."""
    writer = DirWriter(tmp_path / "objects")
    with sync(pg) as con:
        low = claim_one(con, lg)
        con.execute("UPDATE fi.sync_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", [low.job_id])
        con.commit()
        high = queue.claim(con, "w2")
        assert high.fence > low.fence
        hb = fake_built(tmp_path, 4)
        lb = fake_built(tmp_path, 5)
        assert publish_job(writer, con, high, hb, raw=None, global_version=GV)[0]
        assert not publish_job(writer, con, low, lb, raw=None, global_version=GV)[0]
    assert pointer(pg, lg) == (hb.league_version, high.fence)


def test_a_failure_before_the_swap_leaves_the_pointer_unchanged(pg, lg, tmp_path):
    writer = DirWriter(tmp_path / "objects")
    with sync(pg) as con:
        c = claim_one(con, lg)
        first = fake_built(tmp_path, 6)
        publish_job(writer, con, c, first, raw=None, global_version=GV)
        queue.complete(con, c)
        c2 = claim_one(con, lg, "manual")

    class Broken(DirWriter):
        def put(self, key, path):
            raise OSError("disk full")

    with sync(pg) as con, pytest.raises(OSError):
        publish_job(Broken(tmp_path / "objects"), con, c2, fake_built(tmp_path, 7), raw=b"r", global_version=GV)
    assert pointer(pg, lg) == (first.league_version, c.fence)


def test_raw_payloads_follow_their_league_version_out(pg, lg, tmp_path):
    writer = DirWriter(tmp_path / "objects")
    with sync(pg) as con:
        versions = []
        for n in range(5):
            c = claim_one(con, lg, "manual")
            b = fake_built(tmp_path, n)
            publish_job(writer, con, c, b, raw=b"r%d" % n, global_version=GV)
            queue.complete(con, c)
            versions.append(b.league_version)
    kept = sorted(p.name for p in (tmp_path / "objects" / "raw" / lg).iterdir())
    assert kept == sorted(f"{v}.json.gz" for v in versions[-3:])
```

- [ ] **Step 2: Run them and see them fail**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_sync_publish.py -q
```
Expected: FAIL (No module named app.sync.publish).

- [ ] **Step 3: Implement**

`fantasy-intel/backend/app/sync/publish.py`:

```python
"""Publish what a sync job built: its raw payloads, then the league file through the spec's fenced upsert with the
fence the job got at claim time (app/db/league_publish.commit_league). A job holding an older fence than the
published one changes nothing visible and its objects are deleted again."""

from __future__ import annotations

import tempfile
from pathlib import Path

from ..db.league_files import league_key
from ..db.league_publish import ObjectWriter, commit_league
from .build import Built
from .queue import Claim


def raw_key(league_id: str, data_version: str) -> str:
    league_key(league_id, data_version)            # validates both
    return f"raw/{league_id}/{data_version}.json.gz"


def publish_job(writer: ObjectWriter, sync_con, claim: Claim, built: Built, *, raw: bytes | None,
                global_version: str) -> tuple[bool, tuple[str, ...]]:
    """Upload the raw payloads (when given) and the league file, then move the pointer if `claim.fence` is still the
    newest. Returns (applied, keys collected)."""
    if raw is not None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.json.gz"
            path.write_bytes(raw)
            writer.put(raw_key(claim.league_id, built.league_version), path)
    return commit_league(writer, sync_con, claim.league_id, built.league_path, built.league_version, global_version,
                         claim.fence)
```

- [ ] **Step 4: Run them and see them pass**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_sync_publish.py tests/test_league_publish.py -q
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/backend/app/sync/publish.py fantasy-intel/backend/tests/test_sync_publish.py fantasy-intel/backend/app/db/league_publish.py
git commit -m "Phase 3 task 6: publish a job's build with its claim fence; raw payloads live and die with their version"
```


### Task 7: The worker and the redacting printer

**Files:**
- Create: `fantasy-intel/backend/app/sync/redact.py`
- Create: `fantasy-intel/backend/app/sync/worker.py`
- Create: `fantasy-intel/backend/tests/test_sync_worker.py`
- Modify: `fantasy-intel/backend/app/db/league_publish.py`

**Interfaces:**
- Consumes: Tasks 2–6; fi.sync_credential, fi.mark_credential, Task 1's functions; credential_crypto.PrivateKeyring.
- Produces: Printer(stream=None, *, github=None) with mask(), sensitive(), sensitive_from(), clean(), __call__; Context(sync_dsn, objects, printer, owner, keyring, client, control, probe, heartbeat_s, work_root); run_job(ctx, con, claim) -> outcome code; drain(ctx, *, budget_s, providers=('espn',)) -> [(job, league, outcome, seconds)]; private_keyring_from_env(); connect(dsn). R2Writer and DirWriter gain fetch(key, dest, *, sha256_prefix, size) and get_bytes(key).

- [ ] **Step 1: Write the failing tests**

`fantasy-intel/backend/tests/test_sync_worker.py`:

```python
"""A sync job end to end on the fixture league: fetch (fake ESPN) -> build -> fenced publish, the private-cookie path,
the §3.6 outcomes, lease loss, probes, and the public-log audit (spec §3.8)."""
import io
import uuid
from contextlib import contextmanager

import psycopg
import pytest
from espn_api.requests.espn_requests import ESPNAccessDenied

from app.control_plane.credential_crypto import PrivateKeyring, PublicKeyring, generate_keypair
from app.db.league_files import global_key
from app.db.league_publish import DirWriter
from app.db.league_split import content_hash, split_database
from app.sync import queue
from app.sync.redact import Printer
from app.sync.worker import Context, drain
from cp_seed import U1, as_role, seed_league, seed_membership, seed_user
from espn_fixture import MY_SWID, PLAYERS, FakeEspnClient
from golden.golden import build_e2e_db

COOKIES = {"espn_s2": "S2-SECRET-COOKIE-VALUE", "swid": MY_SWID}


def sync(pg):
    return psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None)


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("worker")
    db = build_e2e_db(d / "e2e.duckdb")
    out = split_database(db, d / "league.duckdb", d / "global.duckdb", stamp="2026-10-21T10:00:00",
                         global_stamp="content-addressed")
    return out


@pytest.fixture
def world(pg, files, tmp_path):
    objects = DirWriter(tmp_path / "objects")
    objects.put(global_key(files.global_version), files.global_path)
    with sync(pg) as con:
        con.execute("INSERT INTO fi.global_snapshot (global_version, content_hash) VALUES (%s, %s)",
                    [files.global_version, content_hash(files.global_path)])
        con.commit()
    out = io.StringIO()
    calls = []

    @contextmanager
    def client(external_id, season, credential, *, fake=None):
        calls.append({"external_id": external_id, "season": season, "credential": dict(credential or {})})
        yield fake or FakeEspnClient()

    ctx = Context(sync_dsn=pg.dsn_for("fi_sync"), objects=objects, printer=Printer(out, github=False), owner="run1-w1",
                  client=client, control=lambda season: True, probe=lambda ext, season: "public", heartbeat_s=0.05,
                  work_root=tmp_path)
    return {"ctx": ctx, "out": out, "calls": calls, "objects": tmp_path / "objects"}


def pointer(pg, lid):
    with pg.connect() as su:
        return su.execute("SELECT data_version, global_version FROM fi.league_snapshots WHERE league_id = %s",
                          [lid]).fetchone()


def job(pg, lid):
    with pg.connect() as su:
        return su.execute("SELECT status, error_code, attempt FROM fi.sync_jobs WHERE league_id = %s "
                          "ORDER BY created_at DESC LIMIT 1", [lid]).fetchone()


def league_row(pg, lid):
    with pg.connect() as su:
        return su.execute("SELECT visibility, visibility_epoch, status, name FROM fi.leagues WHERE id = %s", [lid]).fetchone()


def enqueue(pg, lid, kind="initial"):
    with sync(pg) as con:
        queue.enqueue(con, lid, kind, key=f"{kind}:{lid}")


def test_a_public_league_is_fetched_built_and_published(pg, world, files):
    with pg.connect() as su:
        lid = seed_league(su, external_id="999", name=None)
        su.commit()
    enqueue(pg, lid)
    done = drain(world["ctx"], budget_s=120)
    assert [d[2] for d in done] == ["published"]
    dv, gv = pointer(pg, lid)
    assert gv == files.global_version
    assert (world["objects"] / "leagues" / lid / f"{dv}.duckdb").exists()
    assert (world["objects"] / "raw" / lid / f"{dv}.json.gz").exists()
    assert job(pg, lid)[0] == "succeeded"
    assert league_row(pg, lid)[3] == "Fixture League", "the name is refreshed from the provider"
    assert world["calls"][0] == {"external_id": 999, "season": 2026, "credential": {}}


def test_the_drain_log_names_no_league_data(pg, world, capfd):
    """Spec §3.8 audit: only opaque ids, durations and codes reach the public log."""
    with pg.connect() as su:
        lid = seed_league(su, external_id="999")
        su.commit()
    enqueue(pg, lid)
    drain(world["ctx"], budget_s=120)
    captured = world["out"].getvalue() + "".join(capfd.readouterr())
    assert f"league {lid}" in world["out"].getvalue()
    forbidden = ["Fixture League", "999", *(f"Team {t}" for t in range(1, 5)), *(n for n, _, _ in PLAYERS.values()),
                 COOKIES["espn_s2"]]
    leaked = [f for f in forbidden if f in world["out"].getvalue()]
    assert leaked == [], leaked
    assert "S2-SECRET" not in captured


@pytest.fixture
def private(pg, world):
    """A private league whose admin U1 stored their cookies (encrypted to a fresh keypair)."""
    priv_pem, pub_pem = generate_keypair()
    world["ctx"].keyring = PrivateKeyring({"v1": priv_pem})
    cid = str(uuid.uuid4())
    with pg.connect() as su:
        seed_user(su, U1)
        lid = seed_league(su, external_id="999", visibility="private")
        seed_membership(su, U1, lid, "admin")
        su.commit()
    enc = PublicKeyring({"v1": pub_pem}).encrypt(COOKIES, league_id=lid, credential_id=cid, owner_user_id=U1)
    with pg.connect(user="fi_api") as con, as_role(con, "authenticated", U1) as c:
        c.execute("SELECT fi.store_credential(%s, %s, %s, %s, %s, %s)",
                  [cid, lid, enc.ciphertext, enc.nonce, enc.wrapped_data_key, enc.wrap_key_version])
    return {"league": lid, "cid": cid}


def test_a_private_league_syncs_with_its_owners_cookies(pg, world, private):
    import duckdb
    enqueue(pg, private["league"])
    assert [d[2] for d in drain(world["ctx"], budget_s=120)] == ["published"]
    assert world["calls"][0]["credential"] == COOKIES
    dv, _ = pointer(pg, private["league"])
    con = duckdb.connect(str(world["objects"] / "leagues" / private["league"] / f"{dv}.duckdb"), read_only=True)
    try:
        assert con.execute("SELECT team_id FROM fantasy_teams WHERE is_mine").fetchall() == [(2,)], "the owner's team"
    finally:
        con.close()
    assert "S2-SECRET" not in world["out"].getvalue()


def test_an_authenticated_401_makes_the_credential_suspect_and_retries(pg, world, private):
    class Denied(FakeEspnClient):
        def league(self):
            raise ESPNAccessDenied("League 999 cannot be accessed with the provided credentials")

    @contextmanager
    def client(external_id, season, credential):
        yield Denied()

    world["ctx"].client = client
    enqueue(pg, private["league"])
    assert [d[2] for d in drain(world["ctx"], budget_s=120)] == ["provider_auth:suspect"]
    assert job(pg, private["league"])[:2] == ("queued", "provider_auth")
    with pg.connect() as su:
        assert su.execute("SELECT status FROM fi.league_credentials").fetchone() == ("suspect",)
        assert su.execute("SELECT status FROM fi.leagues WHERE id = %s", [private["league"]]).fetchone() == ("active",)
    assert pointer(pg, private["league"]) is None


def test_an_anonymous_401_restricts_a_public_league_at_once(pg, world):
    with pg.connect() as su:
        lid = seed_league(su, external_id="999", epoch=3)
        su.commit()

    class Denied(FakeEspnClient):
        def league(self):
            raise ESPNAccessDenied("espn_s2 and swid are required")

    @contextmanager
    def client(external_id, season, credential):
        yield Denied()

    world["ctx"].client = client
    enqueue(pg, lid)
    assert [d[2] for d in drain(world["ctx"], budget_s=120)] == ["provider_private"]
    assert league_row(pg, lid)[:3] == ("private", 4, "unavailable")


def test_a_lost_lease_publishes_nothing(pg, world):
    with pg.connect() as su:
        lid = seed_league(su, external_id="999")
        su.commit()

    @contextmanager
    def client(external_id, season, credential):
        with pg.connect() as su:      # another worker reclaimed the job meanwhile
            su.execute("UPDATE fi.sync_jobs SET lease_token = lease_token + 100 WHERE league_id = %s", [lid])
            su.commit()
        yield FakeEspnClient()

    world["ctx"].client = client
    enqueue(pg, lid)
    assert [d[2] for d in drain(world["ctx"], budget_s=120)] == ["lease_lost"]
    assert pointer(pg, lid) is None
    assert not (world["objects"] / "leagues" / lid).exists()


@pytest.mark.parametrize("verdict, expected", [
    ("public", ("public", 0, "active")),
    ("private", ("private", 1, "unavailable")),
])
def test_a_probe_confirms_or_restricts(pg, world, verdict, expected):
    with pg.connect() as su:
        lid = seed_league(su, external_id="999", checked_at="7 hours")
        su.commit()
    world["ctx"].probe = lambda ext, season: verdict
    enqueue(pg, lid, "probe")
    drain(world["ctx"], budget_s=60)
    assert league_row(pg, lid)[:3] == expected
    with pg.connect() as su:
        fresh = su.execute("SELECT visibility_checked_at > now() - interval '1 minute' FROM fi.leagues WHERE id = %s",
                           [lid]).fetchone()[0]
    assert fresh
    assert job(pg, lid)[0] == "succeeded"


def test_a_probe_that_cannot_tell_keeps_the_league_as_it_was_and_retries(pg, world):
    with pg.connect() as su:
        lid = seed_league(su, external_id="999", checked_at="30 hours")
        su.commit()
    world["ctx"].probe = lambda ext, season: "unknown"
    enqueue(pg, lid, "probe")
    drain(world["ctx"], budget_s=60)
    assert job(pg, lid)[:2] == ("queued", "probe_unknown")
    with pg.connect() as su:
        assert su.execute("SELECT visibility_checked_at < now() - interval '29 hours' FROM fi.leagues WHERE id = %s",
                          [lid]).fetchone() == (True,), "an expired check stays expired: non-members keep getting 404"


def test_recompute_rebuilds_from_the_stored_raw_payloads_without_fetching(pg, world):
    with pg.connect() as su:
        lid = seed_league(su, external_id="999")
        su.commit()
    enqueue(pg, lid)
    drain(world["ctx"], budget_s=120)
    first = pointer(pg, lid)[0]
    world["calls"].clear()
    enqueue(pg, lid, "recompute")
    assert [d[2] for d in drain(world["ctx"], budget_s=120)] == ["published"]
    assert world["calls"] == [], "no provider request"
    assert pointer(pg, lid)[0] != first
```

- [ ] **Step 2: Run them and see them fail**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_sync_worker.py -q
```
Expected: FAIL (No module named app.sync.worker).

- [ ] **Step 3: Implement**

`fantasy-intel/backend/app/sync/redact.py`:

```python
"""The drain's only output path (spec §3.8: GitHub Actions logs are public).

Workers print opaque ids (`lg_…`, job uuids), durations, counts and error codes, nothing else. As a second line of
defence, every string the run learns is sensitive (team, member and player names, the provider's league id, cookies)
is registered here, and a line containing one is replaced before it is printed. Cookies are also `::add-mask::`ed
in Actions, so GitHub itself hides them if anything slips past.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Iterable, TextIO

REDACTED = "[line withheld: contained league data]"
NAME_KEYS = frozenset({"name", "nickname", "location", "abbrev", "fullName", "firstName", "lastName", "displayName",
                       "teamName"})
MIN_LEN = 3            # shorter strings (abbreviations like "NY") would withhold ordinary lines


class Printer:
    def __init__(self, stream: TextIO | None = None, *, github: bool | None = None):
        self.stream = stream or sys.stdout
        self.github = bool(os.environ.get("GITHUB_ACTIONS")) if github is None else github
        self._sensitive: set[str] = set()

    def mask(self, value: str | None) -> None:
        """A secret (cookie): masked by GitHub and withheld here."""
        if value:
            if self.github:
                self.stream.write(f"::add-mask::{value}\n")
            self._sensitive.add(value)

    def sensitive(self, values: Iterable[Any]) -> None:
        for v in values:
            if isinstance(v, (str, int)) and len(str(v)) >= MIN_LEN:
                self._sensitive.add(str(v))

    def sensitive_from(self, payload: Any) -> None:
        """Register every name-like string in a provider payload (walks dicts and lists)."""
        stack = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in NAME_KEYS and isinstance(v, str):
                        self.sensitive([v])
                    elif isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(node, list):
                stack.extend(node)

    def clean(self, line: str) -> str:
        return REDACTED if any(s in line for s in self._sensitive) else line

    def __call__(self, *parts: Any) -> None:
        self.stream.write(self.clean(" ".join(str(p) for p in parts)) + "\n")
        self.stream.flush()
```

`fantasy-intel/backend/app/sync/worker.py`:

```python
"""Run one claimed sync job (spec §3.3–3.7). Called by scripts/sync_drain.py inside GitHub Actions.

    probe                 anonymous visibility check (restrict at once on "private"; restore only after a public answer)
    initial / scheduled / manual
                          fetch (cookies only for a private league's own endpoint) -> build -> publish (fenced)
    recompute             the previous version's raw payloads against the current global file, no fetch

A heartbeat thread (its own connection) extends the lease every 30 s; when the lease is lost the job stops at the
next stage and publishes nothing. Outcomes follow app/sync/failures.py; nothing here deletes a credential or data.
Every line printed goes through the redacting Printer and carries only ids, durations and codes.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import psycopg

from ..db.connection import utcnow
from ..db.league_files import global_key, league_key
from ..db.league_split import VERSION_RE
from . import espn_source, queue
from .build import build_league_file
from .failures import classify
from .publish import publish_job, raw_key
from .raw_store import dump_payloads, load_payloads
from .redact import Printer

HEARTBEAT_S = 30


@dataclass
class Context:
    sync_dsn: str
    objects: Any                                  # ObjectWriter with fetch/get_bytes (R2Writer or DirWriter)
    printer: Printer
    owner: str                                    # lease owner: "<run id>-<worker n>", never a hostname
    keyring: Any = None                           # credential_crypto.PrivateKeyring, None when no key is configured
    client: Callable = espn_source.espn_client    # (external_id, season, credential) -> context manager of a client
    control: Callable[[int], bool] = espn_source.control_request
    probe: Callable[[int, int], str] = espn_source.probe
    heartbeat_s: float = HEARTBEAT_S
    work_root: Path | None = None
    _globals: dict = field(default_factory=dict)  # version -> local path, shared by this worker's jobs


def private_keyring_from_env(environ=os.environ):
    from ..control_plane.credential_crypto import PrivateKeyring
    keys = {f"v{m[1]}": v for k, v in environ.items() if (m := re.fullmatch(r"CREDENTIALS_PRIVKEY_V(\d{1,4})", k)) and v}
    return PrivateKeyring(keys) if keys else None


def connect(dsn: str):
    return psycopg.connect(dsn, prepare_threshold=None, connect_timeout=15)


class _Lease:
    """Heartbeats on its own connection; `lost` is set when the lease is gone."""

    def __init__(self, ctx: Context, claim: queue.Claim):
        self.ctx, self.claim = ctx, claim
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            with connect(self.ctx.sync_dsn) as con:
                while not self._stop.wait(self.ctx.heartbeat_s):
                    if not queue.heartbeat(con, self.claim):
                        self.lost.set()
                        return
        except Exception:   # noqa: BLE001 - a heartbeat we cannot send is a lease we cannot trust
            self.lost.set()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(5)

    def check(self, stage: str) -> None:
        if self.lost.is_set():
            raise LeaseLost(stage)


class LeaseLost(Exception):
    pass


def _fetch_version(objects, key: str, version: str, dest: Path) -> Path:
    m = VERSION_RE.fullmatch(version)
    if m is None:
        raise ValueError("malformed version")
    objects.fetch(key, dest, sha256_prefix=m["sha"], size=int(m["bytes"]))
    return dest


def _global_file(ctx: Context, version: str) -> Path:
    path = ctx._globals.get(version)
    if path is None or not path.exists():
        root = Path(tempfile.mkdtemp(prefix="fi-global-", dir=ctx.work_root))
        path = _fetch_version(ctx.objects, global_key(version), version, root / "global.duckdb")
        ctx._globals[version] = path
    return path


@contextmanager
def _credential(ctx: Context, con, claim: queue.Claim, league_id: str):
    """The decrypted cookies of a private league's live credential, masked before use; (None, None) if none."""
    from ..control_plane.credential_crypto import EncryptedCredential
    row = con.execute("SELECT credential_id::text, owner_user_id::text, ciphertext, nonce, aad_format, wrapped_data_key, "
                      "wrap_key_version FROM fi.sync_credential(%s, %s)", [claim.job_id, claim.fence]).fetchone()
    con.rollback()
    if row is None:
        yield None, None
        return
    if ctx.keyring is None:
        raise RuntimeError("no credential private key configured")
    cookies = ctx.keyring.decrypt(EncryptedCredential(bytes(row[2]), bytes(row[3]), row[4], bytes(row[5]), row[6]),
                                  league_id=league_id, credential_id=row[0], owner_user_id=row[1])
    ctx.printer.mask(cookies["espn_s2"])
    ctx.printer.mask(cookies["swid"])
    try:
        yield cookies, row[0]
    finally:
        cookies.clear()


def run_job(ctx: Context, con, claim: queue.Claim) -> str:
    """Run `claim` to an outcome and record it. Returns a short outcome code (printed by the drain)."""
    lg = con.execute("SELECT provider, external_id, season, visibility FROM fi.leagues WHERE id = %s",
                     [claim.league_id]).fetchone()
    con.rollback()
    provider, external_id, season, visibility = lg
    ctx.printer.sensitive([external_id])
    if provider != "espn":
        queue.fail(con, claim, "provider_unsupported", retry=False)
        return "provider_unsupported"
    if claim.kind == "probe":
        return _run_probe(ctx, con, claim, int(external_id), season, visibility)
    stage, credential_id = "fetch", None
    try:
        with _Lease(ctx, claim) as lease, _credential(ctx, con, claim, claim.league_id) as (cookies, credential_id):
            if visibility == "private" and cookies is None:
                queue.fail(con, claim, "no_credential", retry=False)
                return "no_credential"
            gv = con.execute("SELECT global_version FROM fi.global_snapshot").fetchone()
            prev = con.execute("SELECT data_version FROM fi.league_snapshots WHERE league_id = %s",
                               [claim.league_id]).fetchone()
            con.rollback()
            if gv is None:
                queue.fail(con, claim, "no_global", retry=True)
                return "no_global"
            with tempfile.TemporaryDirectory(dir=ctx.work_root) as tmp:
                tmp = Path(tmp)
                stage = "fetch"
                global_path = _global_file(ctx, gv[0])
                previous = _fetch_version(ctx.objects, league_key(claim.league_id, prev[0]), prev[0],
                                          tmp / "previous.duckdb") if prev else None
                if claim.kind == "recompute" and prev:
                    payloads = load_payloads(ctx.objects.get_bytes(raw_key(claim.league_id, prev[0])))
                else:
                    with ctx.client(int(external_id), season, cookies) as client:
                        from ..espn.sync import fetch_all
                        payloads = fetch_all(client)
                ctx.printer.sensitive_from(payloads.league)
                ctx.printer.sensitive_from(payloads.pro_players)
                ctx.printer.sensitive_from(payloads.pool)
                lease.check("fetch")
                stage = "build"
                raw = dump_payloads(payloads)
                work = tmp / "work"
                work.mkdir()
                built = build_league_file(work, global_path=global_path, previous=previous, payloads=payloads,
                                          owner_swid=cookies["swid"] if cookies else None,
                                          stamp=utcnow().isoformat(timespec="seconds"))
                lease.check("build")
                if not queue.heartbeat(con, claim):
                    raise LeaseLost("publish")
                stage = "publish"
                applied, _ = publish_job(ctx.objects, con, claim, built, raw=raw, global_version=gv[0])
            _after_success(con, claim, visibility, credential_id, built.name)
            queue.complete(con, claim, global_version=gv[0])
            return "published" if applied else "superseded"
    except LeaseLost:
        return "lease_lost"
    except Exception as err:   # noqa: BLE001 - every failure is classified and recorded; nothing escapes to the log
        con.rollback()
        return _record_failure(ctx, con, claim, err, stage, credential_id, season, visibility)


def _after_success(con, claim: queue.Claim, visibility: str, credential_id: str | None, name: str | None) -> None:
    with con.transaction():
        con.execute("SELECT fi.record_league_found(%s, %s)", [claim.job_id, claim.fence])
        if name:
            con.execute("UPDATE fi.leagues SET name = %s WHERE id = %s AND name IS DISTINCT FROM %s",
                        [name[:200], claim.league_id, name[:200]])
        if visibility == "public":   # a successful anonymous fetch is also a fresh visibility check
            con.execute("UPDATE fi.leagues SET visibility_checked_at = now() WHERE id = %s", [claim.league_id])
    if credential_id:
        con.execute("SELECT fi.mark_credential(%s, %s, %s, 'active')", [claim.job_id, claim.fence, credential_id])
        con.commit()


def _restrict(con, league_id: str, has_credential: bool) -> None:
    """The provider says private: restrict now (one transaction, epoch + 1); a public import nobody can read
    privately becomes unavailable. Data is kept (retention decides)."""
    with con.transaction():
        con.execute("""UPDATE fi.leagues SET visibility = 'private', visibility_epoch = visibility_epoch + 1,
                              visibility_checked_at = now(),
                              status = CASE WHEN %s THEN status ELSE 'unavailable' END
                       WHERE id = %s AND visibility = 'public'""", [has_credential, league_id])


def _record_failure(ctx: Context, con, claim: queue.Claim, err: BaseException, stage: str, credential_id: str | None,
                    season: int, visibility: str) -> str:
    out = classify(err, authenticated=credential_id is not None, stage=stage)
    if out.action == "auth":
        status = con.execute("SELECT fi.record_credential_failure(%s, %s, %s, %s)",
                             [claim.job_id, claim.fence, credential_id, ctx.control(season)]).fetchone()[0]
        con.commit()
        queue.fail(con, claim, out.code, retry=status != "invalid")
        return f"{out.code}:{status}"
    if out.action == "private":
        _restrict(con, claim.league_id, has_credential=False)
        queue.fail(con, claim, out.code, retry=False)
        return out.code
    if out.action == "missing":
        status = con.execute("SELECT fi.record_league_missing(%s, %s, %s)",
                             [claim.job_id, claim.fence, ctx.control(season)]).fetchone()[0]
        con.commit()
        queue.fail(con, claim, out.code, retry=status != "unavailable")
        return f"{out.code}:{status}"
    queue.fail(con, claim, out.code, retry=out.action == "retry", detail=f"{stage}:{type(err).__name__}")
    return out.code


def _run_probe(ctx: Context, con, claim: queue.Claim, external_id: int, season: int, visibility: str) -> str:
    verdict = ctx.probe(external_id, season)
    if verdict == "public":
        with con.transaction():
            # Restoring public access needs this successful anonymous fetch; a league someone connected privately
            # (it has a live credential) is never made public by a probe.
            has_cred = con.execute("SELECT count(*) FROM fi.sync_credential(%s, %s)",
                                   [claim.job_id, claim.fence]).fetchone()[0] > 0
            if not has_cred:
                con.execute("""UPDATE fi.leagues SET visibility_checked_at = now(),
                                      visibility_epoch = visibility_epoch + CASE WHEN visibility = 'private' THEN 1 ELSE 0 END,
                                      status = CASE WHEN visibility = 'private' AND status = 'unavailable' THEN 'active'
                                                    ELSE status END,
                                      visibility = 'public'
                               WHERE id = %s""", [claim.league_id])
            con.execute("SELECT fi.record_league_found(%s, %s)", [claim.job_id, claim.fence])
        queue.complete(con, claim)
        return "probe_public"
    if verdict == "private":
        has_cred = con.execute("SELECT count(*) FROM fi.sync_credential(%s, %s)", [claim.job_id, claim.fence]).fetchone()[0] > 0
        con.rollback()
        _restrict(con, claim.league_id, has_credential=has_cred)
        queue.complete(con, claim)
        return "probe_private"
    if verdict == "missing":
        status = con.execute("SELECT fi.record_league_missing(%s, %s, %s)",
                             [claim.job_id, claim.fence, ctx.control(season)]).fetchone()[0]
        con.commit()
        queue.complete(con, claim)
        return f"probe_missing:{status}"
    queue.fail(con, claim, "probe_unknown", retry=True)   # keeps the league closed until a probe succeeds
    return "probe_unknown"


def drain(ctx: Context, *, budget_s: float, providers: tuple[str, ...] = ("espn",)) -> list[tuple[str, str, str, float]]:
    """Claim and run jobs until none is due or the budget is used. Returns (job id, league id, outcome, seconds)."""
    done = []
    deadline = time.monotonic() + budget_s
    with connect(ctx.sync_dsn) as con:
        while time.monotonic() < deadline:
            claim = queue.claim(con, ctx.owner, providers=providers)
            if claim is None:
                break
            started = time.monotonic()
            outcome = run_job(ctx, con, claim)
            secs = time.monotonic() - started
            ctx.printer(f"job {claim.job_id} league {claim.league_id} kind {claim.kind} attempt {claim.attempt} "
                        f"-> {outcome} in {secs:.1f}s")
            done.append((claim.job_id, claim.league_id, outcome, secs))
    return done
```

- [ ] **Step 4: Run them and see them pass**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_sync_worker.py -q
```
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/backend/app/sync/redact.py fantasy-intel/backend/app/sync/worker.py fantasy-intel/backend/tests/test_sync_worker.py fantasy-intel/backend/app/db/league_publish.py
git commit -m "Phase 3 task 7: the sync worker and the redacting printer"
```


### Task 8: Maintenance

**Files:**
- Create: `fantasy-intel/backend/app/sync/maintenance.py`
- Create: `fantasy-intel/backend/tests/test_sync_maintenance.py`

**Interfaces:**
- Produces: refresh_interval(now=None) -> timedelta (1 h Thu/Sun/Mon US Eastern, else 6 h); enqueue_due(con) -> {'scheduled','probe','recompute'}; purge_expired(con, objects) -> [league ids].

- [ ] **Step 1: Write the failing tests**

`fantasy-intel/backend/tests/test_sync_maintenance.py`:

```python
"""The drain's maintenance step: scheduled refreshes, visibility probes, recomputes after a new global file, and the
deletion of objects whose retention ran out (spec §2, §3.2, §3.7)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest

from app.db.league_publish import DirWriter
from app.sync import maintenance
from cp_seed import seed_league, seed_snapshot

GV_OLD = "20261020T100000Z-" + "a" * 24 + "-100"
GV_NEW = "20261021T100000Z-" + "b" * 24 + "-100"
ET = ZoneInfo("America/New_York")


def sync(pg):
    return psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None)


def jobs(pg):
    with pg.connect() as su:
        return sorted(su.execute("SELECT league_id, kind FROM fi.sync_jobs WHERE status = 'queued'").fetchall())


def set_global(pg, gv):
    with sync(pg) as con:
        con.execute("INSERT INTO fi.global_snapshot (global_version, content_hash) VALUES (%s, %s) ON CONFLICT (id) "
                    "DO UPDATE SET global_version = EXCLUDED.global_version", [gv, "c" * 24])
        con.commit()


def committed(pg, lid, hours_ago):
    with pg.connect() as su:
        su.execute("UPDATE fi.league_snapshots SET committed_at = now() - %s * interval '1 hour' WHERE league_id = %s",
                   [hours_ago, lid])
        su.commit()


@pytest.mark.parametrize("weekday, interval", [(datetime(2026, 10, 22, 12, tzinfo=ET), 1),    # Thursday
                                               (datetime(2026, 10, 25, 12, tzinfo=ET), 1),    # Sunday
                                               (datetime(2026, 10, 21, 12, tzinfo=ET), 6)])   # Wednesday
def test_refresh_interval_is_shorter_on_game_days(weekday, interval):
    assert maintenance.refresh_interval(weekday).total_seconds() == interval * 3600


def test_due_leagues_get_a_scheduled_refresh_once(pg, monkeypatch):
    monkeypatch.setattr(maintenance, "refresh_interval", lambda now=None: timedelta(hours=6))
    set_global(pg, GV_OLD)
    with pg.connect() as su:
        due, fresh, dormant = seed_league(su), seed_league(su), seed_league(su, status="dormant")
        seed_league(su)                               # no version yet: its initial job is the user's, not ours
        for lid in (due, fresh, dormant):
            seed_snapshot(su, lid, data_version=f"v{lid[-4:]}", global_version=GV_OLD)
        su.commit()
    committed(pg, due, 7)
    committed(pg, dormant, 7)
    with sync(pg) as con:
        first, again = maintenance.enqueue_due(con), maintenance.enqueue_due(con)
    assert jobs(pg) == [(due, "scheduled")]
    assert first["scheduled"] == 1 and again["scheduled"] == 0, "idempotent"


def test_public_leagues_are_probed_every_six_hours(pg):
    set_global(pg, GV_OLD)
    with pg.connect() as su:
        old = seed_league(su, checked_at="7 hours")
        recent = seed_league(su, checked_at="1 hour")
        never = seed_league(su, checked_at=None)
        private = seed_league(su, visibility="private", checked_at="30 hours")
        su.commit()
    with sync(pg) as con:
        maintenance.enqueue_due(con)
    assert jobs(pg) == sorted([(old, "probe"), (never, "probe")])


def test_a_new_global_file_queues_a_recompute_for_each_active_league(pg):
    set_global(pg, GV_OLD)
    with pg.connect() as su:
        a, b = seed_league(su, checked_at="now()"), seed_league(su, checked_at="now()")
        for lid in (a, b):
            seed_snapshot(su, lid, data_version=f"v{lid[-4:]}", global_version=GV_OLD)
        su.commit()
    with sync(pg) as con:
        assert maintenance.enqueue_due(con)["recompute"] == 0
    set_global(pg, GV_NEW)
    with sync(pg) as con:
        assert maintenance.enqueue_due(con)["recompute"] == 2
    assert jobs(pg) == sorted([(a, "recompute"), (b, "recompute")])


def test_expired_leagues_lose_their_objects(pg, tmp_path):
    objects = DirWriter(tmp_path / "objects")
    (tmp_path / "f").write_bytes(b"x")
    with pg.connect() as su:
        gone = seed_league(su, deleted=True)
        kept = seed_league(su)
        su.execute("UPDATE fi.leagues SET deleted_at = now() - interval '8 days' WHERE id = %s", [gone])
        seed_snapshot(su, gone)
        su.commit()
    for lid in (gone, kept):
        objects.put(f"leagues/{lid}/v1.duckdb", tmp_path / "f")
        objects.put(f"raw/{lid}/v1.json.gz", tmp_path / "f")
    with sync(pg) as con:
        assert maintenance.purge_expired(con, objects) == [gone]
        assert maintenance.purge_expired(con, objects) == []
    assert objects.keys(f"leagues/{gone}/") == [] and objects.keys(f"raw/{gone}/") == []
    assert objects.keys(f"leagues/{kept}/") == [f"leagues/{kept}/v1.duckdb"]
```

- [ ] **Step 2: Run them and see them fail**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_sync_maintenance.py -q
```
Expected: FAIL (No module named app.sync.maintenance).

- [ ] **Step 3: Implement**

`fantasy-intel/backend/app/sync/maintenance.py`:

```python
"""The drain's maintenance step, run once per drain before the workers start (spec §2 retention, §3.2 dispatch,
§3.7 recompute). Everything here only enqueues jobs or deletes objects whose retention ran out in Postgres
(fi.league_timers); it never decides on provider data.

    scheduled   an active league with a published version older than its refresh interval (1 h on NFL game days
                Thu/Sun/Mon US Eastern, 6 h otherwise), staggered by a few minutes per league
    probe       a public league whose anonymous visibility check is older than 6 h (spec §2 "Dynamic visibility")
    recompute   an active league whose version was built against an older global file than the current one
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import queue

GAME_DAYS = {3, 6, 0}                 # Thursday, Sunday, Monday (datetime.weekday())
PROBE_EVERY = timedelta(hours=6)
STAGGER_MIN = 10
EASTERN = ZoneInfo("America/New_York")


def refresh_interval(now: datetime | None = None) -> timedelta:
    now = (now or datetime.now(timezone.utc)).astimezone(EASTERN)
    return timedelta(hours=1) if now.weekday() in GAME_DAYS else timedelta(hours=6)


def _stagger(league_id: str) -> timedelta:
    return timedelta(minutes=int(hashlib.sha256(league_id.encode()).hexdigest(), 16) % STAGGER_MIN)


_NO_OPEN_JOB = ("NOT EXISTS (SELECT 1 FROM fi.sync_jobs j WHERE j.league_id = l.id "
                "AND j.status IN ('queued', 'running'))")


def enqueue_due(con) -> dict:
    """Queue the scheduled refreshes, probes and recomputes that are due. Idempotent (keys collapse repeats)."""
    counts = {"scheduled": 0, "probe": 0, "recompute": 0}
    current = con.execute("SELECT global_version FROM fi.global_snapshot").fetchone()
    due = con.execute(f"""
        SELECT l.id FROM fi.leagues l JOIN fi.league_snapshots s ON s.league_id = l.id
        WHERE l.deleted_at IS NULL AND l.status = 'active' AND s.committed_at < now() - %s AND {_NO_OPEN_JOB}
        ORDER BY s.committed_at""", [refresh_interval()]).fetchall()
    probes = con.execute(f"""
        SELECT l.id FROM fi.leagues l
        WHERE l.deleted_at IS NULL AND l.visibility = 'public' AND l.status IN ('active', 'unavailable')
          AND (l.visibility_checked_at IS NULL OR l.visibility_checked_at < now() - %s) AND {_NO_OPEN_JOB}
        ORDER BY l.visibility_checked_at NULLS FIRST""", [PROBE_EVERY]).fetchall()
    stale = con.execute(f"""
        SELECT l.id FROM fi.leagues l JOIN fi.league_snapshots s ON s.league_id = l.id
        WHERE l.deleted_at IS NULL AND l.status = 'active' AND s.global_version <> %s AND {_NO_OPEN_JOB}""",
                        [current[0] if current else ""]).fetchall() if current else []
    con.rollback()
    scheduled = {r[0] for r in due}
    for (lid,) in due:
        counts["scheduled"] += queue.enqueue(con, lid, "scheduled", key=f"scheduled:{lid}", run_after=_stagger(lid)) is not None
    for (lid,) in probes:
        counts["probe"] += queue.enqueue(con, lid, "probe", key=f"probe:{lid}") is not None
    for (lid,) in stale:
        if lid not in scheduled:          # a refresh builds against the current global file anyway
            counts["recompute"] += queue.enqueue(con, lid, "recompute", key=f"recompute:{lid}") is not None
    return counts


def purge_expired(con, objects) -> list[str]:
    """Delete the objects of leagues whose retention ran out (fi.league_timers), then mark them purged."""
    due = [r[0] for r in con.execute("SELECT league_id FROM fi.league_timers()").fetchall()]
    con.commit()
    for lid in due:
        for prefix in (f"leagues/{lid}/", f"raw/{lid}/"):
            for key in objects.keys(prefix):
                objects.delete(key)
        con.execute("SELECT fi.league_purged(%s)", [lid])
        con.commit()
    return due
```

- [ ] **Step 4: Run them and see them pass**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_sync_maintenance.py -q
```
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/backend/app/sync/maintenance.py fantasy-intel/backend/tests/test_sync_maintenance.py
git commit -m "Phase 3 task 8: maintenance (scheduled refreshes, probes, recomputes, retention purge)"
```


### Task 9: Drain CLI, workflow and public-log audit

**Files:**
- Create: `fantasy-intel/scripts/sync_drain.py`
- Create: `fantasy-intel/scripts/audit_drain_log.py`
- Create: `.github/workflows/sync-drain.yml`
- Create: `fantasy-intel/backend/tests/test_sync_drain_cli.py`

**Interfaces:**
- Consumes: worker.drain, maintenance.
- Produces: scripts/sync_drain.py main(argv) -> exit code; scripts/audit_drain_log.py audit(lines) -> problems; workflow 'Sync drain' (schedule */15, workflow_dispatch; runs only when vars.SYNC_DRAIN_ENABLED == 'true').

- [ ] **Step 1: Write the failing tests**

`fantasy-intel/backend/tests/test_sync_drain_cli.py`:

```python
"""scripts/sync_drain.py end to end on the fixture league (local object directory, test Postgres), the public-log
audit (scripts/audit_drain_log.py) and the workflow's guard rails (.github/workflows/sync-drain.yml)."""
import importlib.util
import logging
import sys
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest

from app.db.league_files import global_key
from app.db.league_publish import DirWriter
from app.db.league_split import content_hash, split_database
from app.sync import espn_source, queue
from cp_seed import seed_league
from espn_fixture import FakeEspnClient
from golden.golden import build_e2e_db

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "fantasy-intel" / "scripts"


def load(name):
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(SCRIPTS))


@pytest.fixture(autouse=True)
def _restore_logging():
    """The drain switches library logging off for its whole process; give it back to the rest of the suite."""
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("drain")
    return split_database(build_e2e_db(d / "e2e.duckdb"), d / "league.duckdb", d / "global.duckdb",
                          stamp="2026-10-21T10:00:00", global_stamp="content-addressed")


def test_the_drain_publishes_and_its_whole_output_passes_the_audit(pg, files, tmp_path, monkeypatch, capsys):
    objects = DirWriter(tmp_path / "objects")
    objects.put(global_key(files.global_version), files.global_path)
    with psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None) as con:
        con.execute("INSERT INTO fi.global_snapshot (global_version, content_hash) VALUES (%s, %s)",
                    [files.global_version, content_hash(files.global_path)])
        con.commit()
    with pg.connect() as su:
        lid = seed_league(su, external_id="999")
        su.commit()
    with psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None) as con:
        queue.enqueue(con, lid, "initial", key=f"initial:{lid}")

    @contextmanager
    def fake_client(external_id, season, credential):
        yield FakeEspnClient()

    monkeypatch.setattr(espn_source, "espn_client", fake_client)
    monkeypatch.setenv("FI_SYNC_DB_URL", pg.dsn_for("fi_sync"))
    monkeypatch.setenv("LEAGUE_FILES_DIR", str(tmp_path / "objects"))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    drain = load("sync_drain")
    assert drain.main(["--workers", "1", "--budget-min", "2"]) == 0
    out = capsys.readouterr()
    lines = (out.out + out.err).splitlines()
    assert any(line.startswith("maintenance: ") for line in lines)
    assert any(f"league {lid} kind initial attempt 1 -> published" in line for line in lines)
    assert load("audit_drain_log").audit(lines) == []
    with pg.connect() as su:
        assert su.execute("SELECT count(*) FROM fi.league_snapshots WHERE league_id = %s", [lid]).fetchone() == (1,)


def test_an_unconfigured_drain_does_nothing(monkeypatch, capsys):
    monkeypatch.delenv("FI_SYNC_DB_URL", raising=False)
    assert load("sync_drain").main(["--workers", "1"]) == 0
    assert load("audit_drain_log").audit(capsys.readouterr().out.splitlines()) == []


@pytest.mark.parametrize("line", [
    "{11111111-2222-3333-4444-555555555555}",
    "espn_s2=AEabc",
    "AE" + "x" * 60,
    "GET https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/segments/0/leagues/1",
    "Traceback (most recent call last):",
    "Team Tacos beat Team Burritos",
])
def test_the_audit_fails_on_anything_but_known_drain_lines(line):
    assert load("audit_drain_log").audit([line]) != []


def test_the_workflow_never_runs_for_pull_requests_and_is_off_by_default():
    wf = (REPO / ".github" / "workflows" / "sync-drain.yml").read_text()
    on = wf[wf.index("\non:"):wf.index("\npermissions:")]
    assert "pull_request" not in on and "push" not in on
    assert "schedule:" in on and "workflow_dispatch:" in on
    assert "vars.SYNC_DRAIN_ENABLED == 'true'" in wf
    assert "persist-credentials: false" in wf
    assert "contents: read" in wf and "write" not in wf.split("permissions:")[1].split("concurrency:")[0]
    assert "SERVICE_ROLE" not in wf.upper().replace("-", "_")
    assert "audit_drain_log.py" in wf and "if: always()" in wf
```

- [ ] **Step 2: Run them and see them fail**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_sync_drain_cli.py -q
```
Expected: FAIL (scripts do not exist).

- [ ] **Step 3: Implement**

`fantasy-intel/scripts/sync_drain.py`:

```python
"""Drain the multi-league sync queue (run by .github/workflows/sync-drain.yml; spec §3).

    cd backend && uv run python ../scripts/sync_drain.py [--workers 2] [--budget-min 18] [--no-maintenance]

Environment: FI_SYNC_DB_URL (fi_sync), SNAPSHOT_BUCKET + R2_* (a read/write token) or LEAGUE_FILES_DIR (local),
CREDENTIALS_PRIVKEY_V<n> (private leagues). Runs the maintenance step once (retention purge, due refreshes, probes,
recomputes), then N worker processes claim jobs until the queue is empty or the budget is used.

The log is public: library logging is switched off and every line goes through app/sync/redact.Printer (ids,
durations, counts and error codes only). Job failures are recorded in Postgres, not raised: the exit code is
non-zero only when the drain itself could not run.
"""

import argparse
import logging
import multiprocessing
import os
import sys
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401


def objects_from_env():
    from app.config import get_settings
    from app.db.league_publish import DirWriter, R2Writer
    settings = get_settings()
    if settings.r2_configured:
        return R2Writer(settings.r2_config())
    files_dir = os.environ.get("LEAGUE_FILES_DIR", "").strip()
    if not files_dir:
        raise SystemExit("configure R2 (SNAPSHOT_BUCKET, R2_*) or LEAGUE_FILES_DIR")
    return DirWriter(Path(files_dir))


def make_context(n: int):
    from app.sync import espn_source
    from app.sync.redact import Printer
    from app.sync.worker import Context, private_keyring_from_env
    run = os.environ.get("GITHUB_RUN_ID", "local")
    return Context(sync_dsn=os.environ["FI_SYNC_DB_URL"], objects=objects_from_env(), printer=Printer(),
                   owner=f"{run}-w{n}", keyring=private_keyring_from_env(), client=espn_source.espn_client,
                   control=espn_source.control_request, probe=espn_source.probe,
                   work_root=Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()))


def worker(n: int, budget_s: float) -> int:
    logging.disable(logging.CRITICAL)
    from app.sync.redact import Printer
    from app.sync.worker import drain
    try:
        done = drain(make_context(n), budget_s=budget_s)
    except Exception as err:   # noqa: BLE001 - the type only: messages may carry connection strings or ids
        Printer()(f"worker {n} stopped: {type(err).__name__}")
        return 1
    Printer()(f"worker {n} ran {len(done)} job(s)")
    return 0


def maintenance() -> int:
    from app.sync import maintenance as m
    from app.sync.redact import Printer
    from app.sync.worker import connect
    out = Printer()
    try:
        with connect(os.environ["FI_SYNC_DB_URL"]) as con:
            purged = m.purge_expired(con, objects_from_env())
            counts = m.enqueue_due(con)
    except Exception as err:   # noqa: BLE001
        out(f"maintenance failed: {type(err).__name__}")
        return 1
    out(f"maintenance: purged {len(purged)} league(s); queued {counts['scheduled']} scheduled, {counts['probe']} probe, "
        f"{counts['recompute']} recompute")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--budget-min", type=float, default=18)
    ap.add_argument("--no-maintenance", action="store_true")
    args = ap.parse_args(argv)
    logging.disable(logging.CRITICAL)
    if not os.environ.get("FI_SYNC_DB_URL", "").strip():
        print("sync drain not configured (FI_SYNC_DB_URL unset): nothing to do")
        return 0
    failed = 0 if args.no_maintenance else maintenance()
    budget_s = args.budget_min * 60
    if args.workers <= 1:
        return failed or worker(1, budget_s)
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(args.workers) as pool:
        codes = pool.starmap(worker, [(n, budget_s) for n in range(1, args.workers + 1)])
    return failed or max(codes)


if __name__ == "__main__":
    sys.exit(main())
```

`fantasy-intel/scripts/audit_drain_log.py`:

```python
"""Fail when a sync drain log holds anything that looks like a credential or provider data (spec §3.8).

    python3 scripts/audit_drain_log.py <drain.log>

The drain prints opaque ids, durations, counts and codes only (app/sync/redact.py). This second check runs in the
workflow on the captured log: a SWID-shaped GUID, an espn_s2-like token, a cookie name with a value, an ESPN league
URL, or any line that is not one of the drain's known shapes fails the run. Standard library only.
"""

import re
import sys

ALLOWED = [
    re.compile(r"^sync drain not configured .*$"),
    re.compile(r"^maintenance: purged \d+ league\(s\); queued \d+ scheduled, \d+ probe, \d+ recompute$"),
    re.compile(r"^maintenance failed: [A-Za-z_]+$"),
    re.compile(r"^job [0-9a-f-]{36} league lg_[A-Za-z0-9_-]{16,64} kind [a-z]+ attempt \d+ -> [a-z0-9_:]+ in \d+\.\ds$"),
    re.compile(r"^worker \d+ ran \d+ job\(s\)$"),
    re.compile(r"^worker \d+ stopped: [A-Za-z_]+$"),
    re.compile(r"^\[line withheld: contained league data\]$"),
    re.compile(r"^::add-mask::.*$"),
]
FORBIDDEN = [
    re.compile(r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}"),   # SWID
    re.compile(r"AE[A-Za-z0-9%+/=]{40,}"),                                                          # espn_s2-like
    re.compile(r"(espn_s2|SWID)\s*[=:]", re.I),
    re.compile(r"fantasy\.espn\.com", re.I),
]


def audit(lines: list[str]) -> list[str]:
    problems = []
    for n, raw in enumerate(lines, 1):
        line = raw.rstrip("\n")
        if not line.strip() or line.startswith("::add-mask::"):
            continue
        if any(p.search(line) for p in FORBIDDEN):
            problems.append(f"line {n}: credential- or provider-shaped text")
        elif not any(p.match(line) for p in ALLOWED):
            problems.append(f"line {n}: not a known drain line")
    return problems


def main() -> int:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as f:
        problems = audit(f.readlines())
    for p in problems:
        print(p)                       # line numbers only: never echo the offending text into the public log
    print(f"drain log audit: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
```

`.github/workflows/sync-drain.yml`:

```yaml
# Multi-league sync drain (docs/superpowers/plans/2026-10-19-multi-league-phase-3.md, spec §3).
# Never runs on pull requests (forked code must never see these secrets). Off until the repository variable
# SYNC_DRAIN_ENABLED is "true". The log is public: the drain prints ids, durations and codes only, and the audit step
# fails the run if anything credential- or provider-shaped appears.
name: Sync drain

on:
  schedule:
    - cron: "*/15 * * * *"
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: sync-drain
  cancel-in-progress: false

jobs:
  drain:
    name: Drain the sync queue
    if: github.repository == 'hhavynn/fantasy' && vars.SYNC_DRAIN_ENABLED == 'true'
    runs-on: ubuntu-latest
    timeout-minutes: 25
    defaults:
      run:
        shell: bash
        working-directory: fantasy-intel/backend
    env:
      TZ: UTC
      PYTHONUNBUFFERED: "1"
      PYTHONWARNINGS: ignore
      FI_SYNC_DB_URL: ${{ secrets.FI_SYNC_DB_URL }}
      SNAPSHOT_BUCKET: fantasy-intel
      R2_ACCOUNT_ID: ${{ secrets.R2_ACCOUNT_ID }}
      R2_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}
      R2_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}
      CREDENTIALS_PRIVKEY_V1: ${{ secrets.CREDENTIALS_PRIVKEY_V1 }}

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          persist-credentials: false

      - name: Set up Python 3.12
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Set up uv
        uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true
          cache-dependency-glob: "fantasy-intel/backend/uv.lock"

      - name: Install the sync dependencies
        run: uv sync --frozen --no-dev --group sync

      - name: Drain
        run: uv run --no-sync python ../scripts/sync_drain.py --workers 2 --budget-min 18 2>&1 | tee "$RUNNER_TEMP/drain.log"

      - name: Audit the public log
        if: always()
        run: python3 ../scripts/audit_drain_log.py "$RUNNER_TEMP/drain.log"
```

- [ ] **Step 4: Run them and see them pass**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_sync_drain_cli.py -q
```
Expected: 9 passed; the drain's whole output passes the audit.

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/scripts/sync_drain.py fantasy-intel/scripts/audit_drain_log.py .github/workflows/sync-drain.yml fantasy-intel/backend/tests/test_sync_drain_cli.py
git commit -m "Phase 3 task 9: drain CLI, sync-drain workflow (schedule + dispatch, off by default) and the public-log audit"
```

## Acceptance (master plan, Phase 3)

- Sync tests: reclaim in place, heartbeat loss aborts, a stale fence publishes nothing, a new league's first publish becomes visible, racing first publishes keep the higher fence, the slot cap holds under 10 concurrent claimers, duplicate enqueue collapses, a failure before the swap leaves the pointer unchanged, an ambiguous 403/404 never invalidates.
- Golden tests still green with Los Subies produced by the job path.
- Public-log audit: a full drain run's log contains no team names, player names, cookies or league ids beyond the opaque `lg_` id (automated in the workflow and in tests).
- End to end: connect a public ESPN league → data visible once the dispatched run finishes (target < 5 min); flip it private on ESPN → access 404 within one probe; private connect → invite → viewer access.

## Rollback

- Unset `SYNC_DRAIN_ENABLED` (or disable the workflow) and `MULTILEAGUE_CONNECT_ENABLED`. League files keep their last version, and the laptop keeps publishing Los Subies as in Phase 2.
- Down-migrations restore the Phase 2 schema.
