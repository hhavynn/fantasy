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


### Task 10: Connect functions

**Files:**
- Create: `fantasy-intel/supabase/migrations/20261021130000_connect_flows.sql`
- Create: `fantasy-intel/supabase/down/20261021130000_connect_flows.sql`
- Create: `fantasy-intel/backend/tests/test_connect_functions.py`

**Interfaces:**
- Produces (SECURITY DEFINER, ensure_app_user first, then the league): fi.import_public_league(provider, external_id, season, name) -> text; fi.connect_private_league(provider, external_id, season, name, owns_team) -> text; fi.start_private_sync(league_id); fi.request_sync(league_id) -> uuid | NULL; fi.request_probe(league_id) -> boolean (anon too; only the API sees it); fi.delete_league(league_id). Internal: fi.check_league_quota (20 registrations per account per day), fi.queue_initial.

- [ ] **Step 1: Write the failing tests**

`fantasy-intel/backend/tests/test_connect_functions.py`:

```python
"""Phase 3b Postgres functions: public import, private connect, the first private sync, manual refresh, probe on
expiry, admin delete (supabase/migrations/20261021130000_connect_flows.sql)."""
import uuid

import pytest
from psycopg.errors import InsufficientPrivilege, RaiseException

from cp_seed import U1, U2, U3, as_role, seed_league, seed_membership, seed_snapshot, seed_user

CT, NONCE, WRAPPED = b"\x01" * 48, b"\x02" * 12, b"\x03" * 384
IMPORT = "SELECT fi.import_public_league('espn', %s, 2026, 'Name')"
CONNECT = "SELECT fi.connect_private_league('espn', %s, 2026, 'Name', %s)"


def call(con, role, uid, sql, params=()):
    with as_role(con, role, uid) as c:
        return c.execute(sql, params).fetchall()


def refused(con, role, uid, sql, params=(), match=None):
    with pytest.raises((RaiseException, InsufficientPrivilege), match=match):
        call(con, role, uid, sql, params)


@pytest.fixture
def users(pg):
    with pg.connect() as su:
        for u in (U1, U2, U3):
            seed_user(su, u)
        su.commit()


def jobs(pg, lid):
    with pg.connect() as su:
        return su.execute("SELECT kind, status FROM fi.sync_jobs WHERE league_id = %s ORDER BY created_at", [lid]).fetchall()


def test_import_registers_once_pins_and_queues_one_first_sync(pg, users):
    with pg.connect(user="fi_api") as con:
        a = call(con, "authenticated", U1, IMPORT, ["123"])[0][0]
        b = call(con, "authenticated", U2, IMPORT, ["123"])[0][0]
        refused(con, "anon", None, IMPORT, ["124"])
    assert a == b and a.startswith("lg_")
    assert jobs(pg, a) == [("initial", "queued")]
    with pg.connect() as su:
        assert su.execute("SELECT count(*) FROM fi.league_pins WHERE league_id = %s", [a]).fetchone() == (2,)
        assert su.execute("SELECT visibility, visibility_checked_at > now() - interval '1 minute' FROM fi.leagues "
                          "WHERE id = %s", [a]).fetchone() == ("public", True)


def test_a_league_connected_privately_is_never_imported_as_public(pg, users):
    with pg.connect(user="fi_api") as con:
        call(con, "authenticated", U1, CONNECT, ["555", True])
        refused(con, "authenticated", U2, IMPORT, ["555"], match="league_unavailable")


def test_import_wakes_a_dormant_league(pg, users):
    with pg.connect() as su:
        lid = seed_league(su, external_id="777", status="dormant")
        su.execute("UPDATE fi.leagues SET objects_purged_at = now() WHERE id = %s", [lid])
        su.commit()
    with pg.connect(user="fi_api") as con:
        call(con, "authenticated", U1, IMPORT, ["777"])
    with pg.connect() as su:
        assert su.execute("SELECT status, objects_purged_at FROM fi.leagues WHERE id = %s", [lid]).fetchone() == ("active", None)
    assert jobs(pg, lid) == [("initial", "queued")], "purged: no version left, so a first sync"


def test_imports_are_capped_per_account_per_day(pg, users):
    with pg.connect(user="fi_api") as con:
        for n in range(20):
            call(con, "authenticated", U1, IMPORT, [str(1000 + n)])
        refused(con, "authenticated", U1, IMPORT, ["2000"], match="league_quota")
        call(con, "authenticated", U2, IMPORT, ["2000"])


def test_private_connect_makes_the_first_user_admin_and_later_owners_viewers(pg, users):
    with pg.connect(user="fi_api") as con:
        lid = call(con, "authenticated", U1, CONNECT, ["888", False])[0][0]
        assert call(con, "authenticated", U2, CONNECT, ["888", True])[0][0] == lid
        refused(con, "authenticated", U3, CONNECT, ["888", False], match="not_a_league_member")
    with pg.connect() as su:
        roles = dict(su.execute("SELECT user_id::text, role FROM fi.league_memberships WHERE league_id = %s", [lid]).fetchall())
    assert roles == {U1: "admin", U2: "viewer"}


def test_private_connect_refuses_a_public_league(pg, users):
    with pg.connect(user="fi_api") as con:
        call(con, "authenticated", U1, IMPORT, ["999"])
        refused(con, "authenticated", U2, CONNECT, ["999", True], match="league_public")


def test_the_first_private_sync_needs_membership_and_a_live_credential(pg, users):
    with pg.connect(user="fi_api") as con:
        lid = call(con, "authenticated", U1, CONNECT, ["4321", False])[0][0]
        refused(con, "authenticated", U1, "SELECT fi.start_private_sync(%s)", [lid], match="no_credential")
        call(con, "authenticated", U1, "SELECT fi.store_credential(%s, %s, %s, %s, %s, 'v1')",
             [str(uuid.uuid4()), lid, CT, NONCE, WRAPPED])
        refused(con, "authenticated", U2, "SELECT fi.start_private_sync(%s)", [lid], match="league_unavailable")
        call(con, "authenticated", U1, "SELECT fi.start_private_sync(%s)", [lid])
        call(con, "authenticated", U1, "SELECT fi.start_private_sync(%s)", [lid])
    assert jobs(pg, lid) == [("initial", "queued")]


def test_manual_refresh_is_for_members_or_public_and_at_most_every_fifteen_minutes(pg, users):
    with pg.connect() as su:
        pub, priv = seed_league(su), seed_league(su, visibility="private")
        seed_membership(su, U1, priv, "viewer")
        su.commit()
    with pg.connect(user="fi_api") as con:
        assert call(con, "authenticated", U2, "SELECT fi.request_sync(%s)::text", [pub])[0][0]
        assert call(con, "authenticated", U3, "SELECT fi.request_sync(%s)::text", [pub])[0][0] is None, "already queued"
        refused(con, "authenticated", U2, "SELECT fi.request_sync(%s)", [priv], match="league_unavailable")
        assert call(con, "authenticated", U1, "SELECT fi.request_sync(%s)::text", [priv])[0][0]
        refused(con, "anon", None, "SELECT fi.request_sync(%s)", [pub])
    with pg.connect() as su:
        su.execute("UPDATE fi.sync_jobs SET status = 'succeeded' WHERE league_id = %s", [pub])
        su.commit()
    with pg.connect(user="fi_api") as con:
        assert call(con, "authenticated", U2, "SELECT fi.request_sync(%s)::text", [pub])[0][0] is None, "ran < 15 min ago"


def test_an_expired_public_league_is_probed_once_per_five_minutes(pg):
    with pg.connect() as su:
        expired, fresh, private = (seed_league(su, checked_at="25 hours"), seed_league(su),
                                   seed_league(su, visibility="private", checked_at="25 hours"))
        su.commit()
    with pg.connect(user="fi_api") as con:
        queued = [call(con, "anon", None, "SELECT fi.request_probe(%s)", [lid])[0][0]
                  for lid in (expired, expired, fresh, private, "lg_" + "z" * 24)]
    assert queued == [True, False, False, False, False]
    assert jobs(pg, expired) == [("probe", "queued")]
    assert jobs(pg, fresh) == [] and jobs(pg, private) == []


def test_only_an_admin_deletes_and_only_a_private_league(pg, users):
    with pg.connect() as su:
        priv, pub = seed_league(su, visibility="private"), seed_league(su)
        seed_membership(su, U1, priv, "admin")
        seed_membership(su, U2, priv, "viewer")
        seed_membership(su, U1, pub, "admin")
        su.commit()
    with pg.connect(user="fi_api") as con:
        refused(con, "authenticated", U2, "SELECT fi.delete_league(%s)", [priv], match="not_admin")
        refused(con, "authenticated", U1, "SELECT fi.delete_league(%s)", [pub], match="league_public")
        call(con, "authenticated", U1, "SELECT fi.delete_league(%s)", [priv])
    with pg.connect() as su:
        assert su.execute("SELECT deleted_at IS NOT NULL FROM fi.leagues WHERE id = %s", [priv]).fetchone() == (True,)
        assert su.execute("SELECT count(*) FROM fi.audit_log WHERE action = 'league.delete'").fetchone() == (1,)


def test_queue_initial_is_not_callable_by_request_roles(pg, users):
    with pg.connect() as su:
        lid = seed_league(su)
        seed_snapshot(su, lid)
        su.commit()
    with pg.connect(user="fi_api") as con:
        refused(con, "authenticated", U1, "SELECT fi.queue_initial(%s)", [lid])


@pytest.mark.parametrize("fn", ["import_public_league", "connect_private_league", "start_private_sync", "request_sync",
                                "delete_league"])
def test_new_functions_lock_the_account_before_the_league(pg, fn):
    with pg.connect() as con:
        src = con.execute("SELECT pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                          "WHERE n.nspname = 'fi' AND p.proname = %s", [fn]).fetchone()[0]
    body = " ".join(src.split())
    assert body.find("ensure_app_user") < body.find("FROM fi.leagues"), fn
```

- [ ] **Step 2: Run them and see them fail**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_connect_functions.py -q
```
Expected: FAIL (functions do not exist).

- [ ] **Step 3: Implement**

`fantasy-intel/supabase/migrations/20261021130000_connect_flows.sql`:

```sql
-- Phase 3b: registering leagues and asking for syncs, from the signed-in user's request (spec §2, §3.2).
-- The API decides what it can verify (the league's visibility from an anonymous fetch, the caller's cookies from one
-- authenticated fetch) before calling these; each function re-checks auth.uid() and the league's state under lock.
-- Lock order everywhere: the account (ensure_app_user), the app user, the league, then the league's own rows.
-- Idempotent. Down-migration: supabase/down/20261021130000_connect_flows.sql.

CREATE INDEX IF NOT EXISTS audit_log_actor_action ON fi.audit_log (actor_user_id, action, at DESC);

-- 20 registrations per account per day: a public import or private connect queues provider work.
CREATE OR REPLACE FUNCTION fi.check_league_quota(p_user uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = '' AS $$
BEGIN
  IF (SELECT count(*) FROM fi.audit_log a WHERE a.actor_user_id = p_user AND a.action IN ('league.import', 'league.connect')
      AND a.at > now() - interval '1 day') >= 20 THEN
    RAISE EXCEPTION 'league_quota';
  END IF;
END $$;

-- Internal: the first sync of a league that has no published version yet (no grant to request roles).
CREATE OR REPLACE FUNCTION fi.queue_initial(p_league_id text) RETURNS void LANGUAGE sql SECURITY DEFINER
SET search_path = '' AS $$
  INSERT INTO fi.sync_jobs (league_id, kind, idempotency_key)
  SELECT p_league_id, 'initial', 'initial:' || p_league_id
  WHERE NOT EXISTS (SELECT 1 FROM fi.league_snapshots s WHERE s.league_id = p_league_id)
  ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL AND status IN ('queued', 'running') DO NOTHING
$$;

-- Public import: the API has just read the league without cookies. Registers it once (unique provider/id/season),
-- pins it for the caller and queues its first sync. A league registered privately is never imported as public.
CREATE OR REPLACE FUNCTION fi.import_public_league(p_provider text, p_external_id text, p_season integer, p_name text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.ensure_app_user(); v_id text; v_vis text; v_deleted timestamptz;
BEGIN
  PERFORM fi.check_league_quota(v);
  INSERT INTO fi.leagues (id, provider, external_id, season, name, visibility, visibility_checked_at)
  VALUES ('lg_' || replace(gen_random_uuid()::text, '-', ''), p_provider, p_external_id, p_season, left(p_name, 200),
          'public', now())
  ON CONFLICT (provider, external_id, season) DO NOTHING;
  SELECT l.id, l.visibility, l.deleted_at INTO v_id, v_vis, v_deleted FROM fi.leagues l
  WHERE l.provider = p_provider AND l.external_id = p_external_id AND l.season = p_season FOR UPDATE;
  IF v_vis <> 'public' OR v_deleted IS NOT NULL THEN RAISE EXCEPTION 'league_unavailable'; END IF;
  -- A re-import wakes a dormant league (its next sync rebuilds purged objects) and counts as a fresh check.
  UPDATE fi.leagues SET visibility_checked_at = now(),
         status = CASE WHEN status = 'dormant' THEN 'active' ELSE status END,
         objects_purged_at = CASE WHEN status = 'dormant' THEN NULL ELSE objects_purged_at END
  WHERE id = v_id;
  INSERT INTO fi.league_pins (user_id, league_id) VALUES (v, v_id) ON CONFLICT DO NOTHING;
  PERFORM fi.queue_initial(v_id);
  PERFORM fi.audit(v, 'league.import', 'league', v_id, '{}'::jsonb);
  RETURN v_id;
END $$;

-- Private connect: the API has validated the caller's cookies with one fetch (and seen that the league is not
-- public). A new league makes the caller its admin; an existing private league admits the caller as a viewer only
-- when their own cookies show them owning a team in it (p_owns_team, checked by the API), never as admin. The
-- credential is stored next (fi.store_credential) in the same transaction, then fi.queue_initial.
CREATE OR REPLACE FUNCTION fi.connect_private_league(p_provider text, p_external_id text, p_season integer, p_name text,
                                                     p_owns_team boolean)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.ensure_app_user(); v_id text; v_vis text; v_deleted timestamptz; v_new boolean;
BEGIN
  PERFORM fi.check_league_quota(v);
  INSERT INTO fi.leagues (id, provider, external_id, season, name, visibility, visibility_checked_at)
  VALUES ('lg_' || replace(gen_random_uuid()::text, '-', ''), p_provider, p_external_id, p_season, left(p_name, 200),
          'private', now())
  ON CONFLICT (provider, external_id, season) DO NOTHING
  RETURNING true INTO v_new;
  SELECT l.id, l.visibility, l.deleted_at INTO v_id, v_vis, v_deleted FROM fi.leagues l
  WHERE l.provider = p_provider AND l.external_id = p_external_id AND l.season = p_season FOR UPDATE;
  IF v_deleted IS NOT NULL THEN RAISE EXCEPTION 'league_unavailable'; END IF;
  IF coalesce(v_new, false) THEN
    INSERT INTO fi.league_memberships (user_id, league_id, role) VALUES (v, v_id, 'admin');
  ELSIF v_vis = 'public' THEN
    RAISE EXCEPTION 'league_public';
  ELSIF NOT fi.is_member(v_id) THEN
    IF NOT p_owns_team THEN RAISE EXCEPTION 'not_a_league_member'; END IF;
    INSERT INTO fi.league_memberships (user_id, league_id, role) VALUES (v, v_id, 'viewer')
    ON CONFLICT (user_id, league_id) DO UPDATE SET revoked_at = NULL, role = 'viewer';
  END IF;
  PERFORM fi.audit(v, 'league.connect', 'league', v_id, jsonb_build_object('new', coalesce(v_new, false)));
  RETURN v_id;
END $$;

-- The last step of a private connect, after fi.store_credential in the same transaction: a member of a league with a
-- live credential queues its first sync.
CREATE OR REPLACE FUNCTION fi.start_private_sync(p_league_id text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = '' AS $$
BEGIN
  PERFORM fi.ensure_app_user();
  PERFORM 1 FROM fi.leagues l WHERE l.id = p_league_id AND l.deleted_at IS NULL FOR UPDATE;
  IF NOT FOUND OR NOT fi.is_member(p_league_id) THEN RAISE EXCEPTION 'league_unavailable'; END IF;
  IF NOT EXISTS (SELECT 1 FROM fi.league_credentials c WHERE c.league_id = p_league_id AND c.status IN ('active', 'suspect')) THEN
    RAISE EXCEPTION 'no_credential';
  END IF;
  PERFORM fi.queue_initial(p_league_id);
END $$;

-- A member (private) or anyone signed in (public) asks for a refresh: at most one manual sync per league per 15 min.
-- Returns the queued job's id, or NULL when one is already queued or ran recently.
CREATE OR REPLACE FUNCTION fi.request_sync(p_league_id text) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = '' AS $$
DECLARE v uuid := fi.ensure_app_user(); v_vis text; v_status text; v_job uuid;
BEGIN
  SELECT l.visibility, l.status INTO v_vis, v_status FROM fi.leagues l
  WHERE l.id = p_league_id AND l.deleted_at IS NULL FOR UPDATE;
  IF v_vis IS NULL OR (v_vis = 'private' AND NOT fi.is_member(p_league_id)) THEN RAISE EXCEPTION 'league_unavailable'; END IF;
  -- stale (the cookies stopped working) or unavailable (gone on the provider): a refresh cannot help
  IF v_status NOT IN ('active', 'dormant') THEN RAISE EXCEPTION 'sync_paused'; END IF;
  IF EXISTS (SELECT 1 FROM fi.sync_jobs j WHERE j.league_id = p_league_id AND j.kind = 'manual'
             AND j.created_at > now() - interval '15 minutes') THEN
    RETURN NULL;
  END IF;
  UPDATE fi.leagues SET status = 'active' WHERE id = p_league_id AND status = 'dormant';
  INSERT INTO fi.sync_jobs (league_id, kind, idempotency_key) VALUES (p_league_id, 'manual', 'manual:' || p_league_id)
  ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL AND status IN ('queued', 'running') DO NOTHING
  RETURNING id INTO v_job;
  PERFORM fi.audit(v, 'league.sync_requested', 'league', p_league_id, '{}'::jsonb);
  RETURN v_job;
END $$;

-- Spec §2: the first viewer after a public league's visibility check expired triggers a probe (at most one per
-- league per 5 minutes). True when a probe was queued; only the API sees the answer (it then starts the drain), the
-- caller always gets the same 404.
CREATE OR REPLACE FUNCTION fi.request_probe(p_league_id text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = '' AS $$
DECLARE v_job uuid;
BEGIN
  IF EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = p_league_id AND l.deleted_at IS NULL AND l.visibility = 'public'
             AND (l.visibility_checked_at IS NULL OR l.visibility_checked_at < now() - interval '24 hours'))
     AND NOT EXISTS (SELECT 1 FROM fi.sync_jobs j WHERE j.league_id = p_league_id AND j.kind = 'probe'
                     AND j.created_at > now() - interval '5 minutes') THEN
    INSERT INTO fi.sync_jobs (league_id, kind, idempotency_key) VALUES (p_league_id, 'probe', 'probe:' || p_league_id)
    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL AND status IN ('queued', 'running') DO NOTHING
    RETURNING id INTO v_job;
  END IF;
  RETURN v_job IS NOT NULL;
END $$;

-- An admin deletes a private league: soft delete now, objects deleted 7 days later (fi.league_timers). Public
-- leagues are platform-managed: users only unpin them.
CREATE OR REPLACE FUNCTION fi.delete_league(p_league_id text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = '' AS $$
DECLARE v uuid := fi.ensure_app_user(); v_vis text;
BEGIN
  SELECT l.visibility INTO v_vis FROM fi.leagues l WHERE l.id = p_league_id AND l.deleted_at IS NULL FOR UPDATE;
  IF v_vis IS NULL OR NOT fi.is_admin(p_league_id) THEN RAISE EXCEPTION 'not_admin'; END IF;
  IF v_vis <> 'private' THEN RAISE EXCEPTION 'league_public'; END IF;
  UPDATE fi.leagues SET deleted_at = now() WHERE id = p_league_id;
  PERFORM fi.audit(v, 'league.delete', 'league', p_league_id, '{}'::jsonb);
END $$;

REVOKE ALL ON FUNCTION fi.check_league_quota(uuid), fi.queue_initial(text),
  fi.import_public_league(text, text, integer, text), fi.connect_private_league(text, text, integer, text, boolean),
  fi.start_private_sync(text), fi.request_sync(text), fi.request_probe(text), fi.delete_league(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.import_public_league(text, text, integer, text),
  fi.connect_private_league(text, text, integer, text, boolean), fi.start_private_sync(text), fi.request_sync(text),
  fi.delete_league(text) TO authenticated;
GRANT EXECUTE ON FUNCTION fi.request_probe(text) TO anon, authenticated;
```

`fantasy-intel/supabase/down/20261021130000_connect_flows.sql`:

```sql
-- Reverses 20261021130000_connect_flows.sql.

DROP FUNCTION IF EXISTS fi.delete_league(text);
DROP FUNCTION IF EXISTS fi.request_probe(text);
DROP FUNCTION IF EXISTS fi.request_sync(text);
DROP FUNCTION IF EXISTS fi.start_private_sync(text);
DROP FUNCTION IF EXISTS fi.connect_private_league(text, text, integer, text, boolean);
DROP FUNCTION IF EXISTS fi.import_public_league(text, text, integer, text);
DROP FUNCTION IF EXISTS fi.queue_initial(text);
DROP FUNCTION IF EXISTS fi.check_league_quota(uuid);
DROP INDEX IF EXISTS fi.audit_log_actor_action;
```

- [ ] **Step 4: Run them and see them pass**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_connect_functions.py -q
```
Expected: 16 passed.

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/supabase/migrations/20261021130000_connect_flows.sql fantasy-intel/supabase/down/20261021130000_connect_flows.sql fantasy-intel/backend/tests/test_connect_functions.py
git commit -m "Phase 3 task 10: connect functions (public import, private connect, first private sync, refresh, probe, delete)"
```


### Task 11: Connect API

**Files:**
- Create: `fantasy-intel/backend/app/api/connect.py`
- Create: `fantasy-intel/backend/tests/test_connect_api.py`
- Modify: `fantasy-intel/backend/app/control_plane/mutations.py`
- Modify: `fantasy-intel/backend/app/config.py`
- Modify: `fantasy-intel/backend/app/sync/espn_source.py`
- Modify: `fantasy-intel/backend/app/api/leagues_v2.py`
- Modify: `fantasy-intel/backend/app/api/league_scope.py`
- Modify: `fantasy-intel/backend/app/main.py`
- Modify: `fantasy-intel/backend/tests/test_v2_authorization_matrix.py`
- Modify: `fantasy-intel/backend/tests/test_league_scope_audit.py`

**Interfaces:**
- Consumes: Task 10 functions through new mutations wrappers (import_public_league, connect_private_league, start_private_sync, request_sync, delete_league, request_probe, list_invites) and errors (LeagueUnavailable, LeaguePublic, LeagueQuota, NoCredential, NotLeagueOwner, SyncPaused); espn_source.read_league / league_teams (plain requests: the API has no espn_api).
- Produces: routes POST /v2/leagues/import, POST /v2/leagues/connect, PUT|DELETE /v2/leagues/{lg}/credential, GET|POST /v2/leagues/{lg}/invites, DELETE /v2/leagues/{lg}/invites/{id}, POST /v2/invites/redeem, POST /v2/leagues/{lg}/sync, DELETE /v2/leagues/{lg}; nudge_probe(principal, league_id) on every 404; dispatch_drain() (workflow_dispatch, Actions: write token). Settings: multileague_connect_enabled (MULTILEAGUE_CONNECT_ENABLED), github_dispatch_token (GITHUB_DISPATCH_TOKEN).

- [ ] **Step 1: Write the failing tests**

`fantasy-intel/backend/tests/test_connect_api.py`:

```python
"""/v2 connect flows (Phase 3b): public import, private connect with cookies, the caller's credential, invites,
refresh, delete and the probe on an expired public league. ESPN is a fake; the drain dispatch is recorded."""
import json
import uuid

import pytest

from app.control_plane.credential_crypto import EncryptedCredential, PrivateKeyring, generate_keypair
from app.sync import espn_source
from cp_seed import U1, U2, U3, seed_league, seed_membership, seed_user
from test_leagues_api import bearer, env  # noqa: F401  (fixture)

S2 = "AEB" + "s2cookievalue" * 4
SWID = "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}"
OTHER_SWID = "{11111111-2222-3333-4444-555555555555}"
PRIV, PUB = generate_keypair()


def league_payload():
    return {"id": 4242, "seasonId": 2026, "settings": {"name": "Secret League"},
            "teams": [{"id": 1, "name": "Alpha", "owners": [SWID]}, {"id": 2, "name": "Bravo", "owners": [OTHER_SWID]}]}


class FakeEspn:
    def __init__(self):
        self.anonymous = "public"
        self.cookie_verdict = "ok"
        self.reads = []

    def probe(self, league_id, season):
        return self.anonymous

    def read_league(self, league_id, season, credential=None, *, views=("mSettings",), timeout=30):
        self.reads.append({"league": league_id, "season": season, "cookies": dict(credential or {})})
        if self.cookie_verdict != "ok":
            return self.cookie_verdict, None
        return "ok", league_payload()

    league_teams = staticmethod(espn_source.league_teams)


@pytest.fixture
def espn():
    return FakeEspn()


@pytest.fixture
def dispatched():
    return []


@pytest.fixture
def client(env, monkeypatch, espn, dispatched):
    from fastapi.testclient import TestClient

    from app.api import connect
    from app.main import app
    monkeypatch.setenv("MULTILEAGUE_CONNECT_ENABLED", "1")
    monkeypatch.setenv("CREDENTIALS_PUBKEY_V1", PUB)
    monkeypatch.setattr(connect, "dispatch_drain", lambda: dispatched.append(1) or True)
    app.dependency_overrides[connect.espn] = lambda: espn
    app.dependency_overrides[connect.drain_dispatcher] = lambda: (lambda: dispatched.append(1) or True)
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def users(pg):
    with pg.connect() as su:
        for u in (U1, U2, U3):
            seed_user(su, u)
        su.commit()


def one(pg, sql, params=()):
    with pg.connect() as su:
        return su.execute(sql, params).fetchone()


COOKIES = {"espn_s2": S2, "swid": SWID, "consent": True}


def test_everything_is_404_with_the_connect_flag_off(client, monkeypatch, users):
    monkeypatch.delenv("MULTILEAGUE_CONNECT_ENABLED")
    assert client.post("/v2/leagues/import", json={"league": "4242"}, headers=bearer(U1)).status_code == 404


def test_a_public_league_is_imported_pinned_and_queued(client, pg, users, espn, dispatched):
    r = client.post("/v2/leagues/import", json={"league": "https://fantasy.espn.com/football/league?leagueId=4242&seasonId=2026"},
                    headers=bearer(U1))
    assert r.status_code == 201, r.text
    lid = r.json()["league_id"]
    assert one(pg, "SELECT external_id, season, visibility FROM fi.leagues WHERE id = %s", [lid]) == ("4242", 2026, "public")
    assert one(pg, "SELECT kind, status FROM fi.sync_jobs WHERE league_id = %s", [lid]) == ("initial", "queued")
    assert one(pg, "SELECT count(*) FROM fi.league_pins WHERE league_id = %s", [lid]) == (1,)
    assert dispatched == [1], "the drain is started once the job is committed"
    assert espn.reads == [], "a public import never sends cookies"


@pytest.mark.parametrize("verdict, status", [("private", 409), ("missing", 404), ("unknown", 503)])
def test_import_answers_what_espn_said(client, users, espn, dispatched, verdict, status):
    espn.anonymous = verdict
    r = client.post("/v2/leagues/import", json={"league": "4242", "season": 2026}, headers=bearer(U1))
    assert r.status_code == status
    if verdict == "private":
        assert r.json()["needs_cookies"] is True
    assert dispatched == []


def test_import_needs_sign_in_and_a_league_id(client, users):
    assert client.post("/v2/leagues/import", json={"league": "4242"}).status_code == 401
    assert client.post("/v2/leagues/import", json={"league": "not a league"}, headers=bearer(U1)).status_code == 422


def test_a_private_connect_stores_encrypted_cookies_admin_and_the_owned_team(client, pg, users, espn, dispatched):
    espn.anonymous = "private"
    r = client.post("/v2/leagues/connect", json={"league": "4242", "season": 2026, **COOKIES}, headers=bearer(U1))
    assert r.status_code == 201, r.text
    body = r.json()
    assert S2 not in r.text and "AAAAAAAA" not in r.text, "cookies are never echoed"
    assert (body["visibility"], body["cookies"], body["team_id"]) == ("private", "stored", "1")
    assert body["teams"] == [{"team_id": "1", "name": "Alpha"}, {"team_id": "2", "name": "Bravo"}]
    lid = body["league_id"]
    assert one(pg, "SELECT role FROM fi.league_memberships WHERE league_id = %s AND user_id = %s", [lid, U1]) == ("admin",)
    assert one(pg, "SELECT team_external_id FROM fi.team_selections WHERE league_id = %s", [lid]) == ("1",)
    assert one(pg, "SELECT kind FROM fi.sync_jobs WHERE league_id = %s", [lid]) == ("initial",)
    row = one(pg, "SELECT id::text, ciphertext, nonce, aad_format, wrapped_data_key, wrap_key_version, owner_user_id::text "
                  "FROM fi.league_credentials WHERE league_id = %s", [lid])
    assert S2.encode() not in bytes(row[1])
    enc = EncryptedCredential(bytes(row[1]), bytes(row[2]), row[3], bytes(row[4]), row[5])
    assert PrivateKeyring({"v1": PRIV}).decrypt(enc, league_id=lid, credential_id=row[0], owner_user_id=row[6]) == \
        {"espn_s2": S2, "swid": SWID}
    assert espn.reads[0]["cookies"] == {"espn_s2": S2, "swid": SWID}
    assert dispatched == [1]
    assert one(pg, "SELECT count(*) FROM fi.audit_log WHERE meta::text LIKE %s", [f"%{S2[:12]}%"]) == (0,)


def test_connecting_a_league_that_is_public_imports_it_and_drops_the_cookies(client, pg, users, espn):
    r = client.post("/v2/leagues/connect", json={"league": "4242", **COOKIES}, headers=bearer(U1))
    assert r.status_code == 201 and r.json()["cookies"] == "not_needed"
    assert one(pg, "SELECT count(*) FROM fi.league_credentials") == (0,)
    assert espn.reads == []


def test_cookies_espn_refuses_store_nothing(client, pg, users, espn, dispatched):
    espn.anonymous, espn.cookie_verdict = "private", "private"
    r = client.post("/v2/leagues/connect", json={"league": "4242", **COOKIES}, headers=bearer(U1))
    assert r.status_code == 400
    assert one(pg, "SELECT count(*) FROM fi.leagues") == (0,) and dispatched == []


@pytest.mark.parametrize("body", [
    {"league": "4242", "espn_s2": S2, "swid": SWID},                       # no consent
    {"league": "4242", "espn_s2": S2, "swid": "not-a-swid", "consent": True},
    {"league": "4242", "espn_s2": ["list", S2], "swid": SWID, "consent": True},
])
def test_bad_input_is_refused_without_echoing_the_cookies(client, users, espn, body):
    espn.anonymous = "private"
    r = client.post("/v2/leagues/connect", json=body, headers=bearer(U1))
    assert r.status_code == 422 and S2 not in r.text and "AAAAAAAA" not in r.text
    r = client.post("/v2/leagues/connect", content=json.dumps(body)[:-3], headers={**bearer(U1), "content-type": "application/json"})
    assert r.status_code == 422 and S2 not in r.text


def test_a_second_owner_joins_as_viewer_without_replacing_the_credential(client, pg, users, espn, dispatched):
    espn.anonymous = "private"
    lid = client.post("/v2/leagues/connect", json={"league": "4242", **COOKIES}, headers=bearer(U1)).json()["league_id"]
    # U2's cookies own team 2
    r = client.post("/v2/leagues/connect", json={"league": "4242", "espn_s2": S2 + "x", "swid": OTHER_SWID,
                                                 "consent": True}, headers=bearer(U2))
    assert r.status_code == 201 and r.json()["cookies"] == "existing" and r.json()["team_id"] == "2"
    assert one(pg, "SELECT role FROM fi.league_memberships WHERE league_id = %s AND user_id = %s", [lid, U2]) == ("viewer",)
    assert one(pg, "SELECT owner_user_id::text FROM fi.league_credentials WHERE league_id = %s", [lid]) == (U1,)
    assert one(pg, "SELECT count(*) FROM fi.sync_jobs WHERE league_id = %s", [lid]) == (1,)
    # U3's cookies own no team: an invite is the way in
    r = client.post("/v2/leagues/connect", json={"league": "4242", "espn_s2": S2,
                                                 "swid": "{99999999-2222-3333-4444-555555555555}", "consent": True},
                    headers=bearer(U3))
    assert r.status_code == 403


@pytest.fixture
def private_league(pg, users):
    with pg.connect() as su:
        lid = seed_league(su, visibility="private", external_id="4242")
        seed_membership(su, U1, lid, "admin")
        seed_membership(su, U2, lid, "viewer")
        su.commit()
    return lid


def test_invites_are_created_by_admins_redeemed_once_and_revocable(client, pg, private_league):
    assert client.post(f"/v2/leagues/{private_league}/invites", headers=bearer(U2)).status_code == 403
    r = client.post(f"/v2/leagues/{private_league}/invites", headers=bearer(U1))
    assert r.status_code == 201
    token, invite_id = r.json()["token"], r.json()["invite_id"]
    listed = client.get(f"/v2/leagues/{private_league}/invites", headers=bearer(U1)).json()["invites"]
    assert [i["invite_id"] for i in listed] == [invite_id] and token not in json.dumps(listed)
    assert client.get(f"/v2/leagues/{private_league}", headers=bearer(U3)).status_code == 404
    assert client.post("/v2/invites/redeem", json={"token": token}, headers=bearer(U3)).json() == {"league_id": private_league}
    assert client.get(f"/v2/leagues/{private_league}", headers=bearer(U3)).json()["role"] == "viewer"
    assert client.post("/v2/invites/redeem", json={"token": token}, headers=bearer(U3)).status_code == 404, "single use"
    second = client.post(f"/v2/leagues/{private_league}/invites", headers=bearer(U1)).json()
    assert client.delete(f"/v2/leagues/{private_league}/invites/{second['invite_id']}", headers=bearer(U1)).status_code == 204
    with pg.connect() as su:
        seed_user(su, "44444444-4444-4444-8444-444444444444")
        su.commit()
    assert client.post("/v2/invites/redeem", json={"token": second["token"]},
                       headers=bearer("44444444-4444-4444-8444-444444444444")).status_code == 404


def test_a_member_stores_and_removes_their_own_credential(client, pg, private_league, espn, dispatched):
    r = client.put(f"/v2/leagues/{private_league}/credential", json=COOKIES, headers=bearer(U2))
    assert r.status_code == 200 and S2 not in r.text
    assert one(pg, "SELECT owner_user_id::text, status FROM fi.league_credentials") == (U2, "active")
    assert client.put(f"/v2/leagues/{private_league}/credential", json=COOKIES, headers=bearer(U1)).status_code == 409
    assert client.delete(f"/v2/leagues/{private_league}/credential", headers=bearer(U1)).status_code == 403
    assert client.delete(f"/v2/leagues/{private_league}/credential", headers=bearer(U2)).status_code == 204
    assert one(pg, "SELECT count(*) FROM fi.league_credentials") == (0,)


def test_refresh_is_queued_once_and_a_stale_league_says_why(client, pg, private_league, dispatched):
    assert client.post(f"/v2/leagues/{private_league}/sync", headers=bearer(U2)).json() == {"queued": True}
    assert client.post(f"/v2/leagues/{private_league}/sync", headers=bearer(U2)).json() == {"queued": False}
    assert dispatched == [1]
    with pg.connect() as su:
        su.execute("UPDATE fi.leagues SET status = 'stale' WHERE id = %s", [private_league])
        su.commit()
    r = client.post(f"/v2/leagues/{private_league}/sync", headers=bearer(U1))
    assert r.status_code == 409 and "paused" in r.json()["detail"]


def test_an_admin_deletes_a_private_league(client, pg, private_league):
    assert client.delete(f"/v2/leagues/{private_league}", headers=bearer(U2)).status_code == 403
    assert client.delete(f"/v2/leagues/{private_league}", headers=bearer(U1)).status_code == 204
    assert client.get(f"/v2/leagues/{private_league}", headers=bearer(U1)).status_code == 404


def test_a_404_on_an_expired_public_league_queues_one_probe_and_starts_the_drain(client, pg, users, dispatched):
    with pg.connect() as su:
        expired = seed_league(su, checked_at="25 hours")
        su.commit()
    for _ in range(3):
        r = client.get(f"/v2/leagues/{expired}")
        assert (r.status_code, r.json()) == (404, {"detail": "League not found."})
    assert one(pg, "SELECT count(*) FROM fi.sync_jobs WHERE league_id = %s AND kind = 'probe'", [expired]) == (1,)
    assert dispatched == [1]
    assert client.get("/v2/leagues/lg_" + "n" * 24).status_code == 404 and dispatched == [1]
```

- [ ] **Step 2: Run them and see them fail**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_connect_api.py -q
```
Expected: FAIL (the routes do not exist).

- [ ] **Step 3: Implement**

`fantasy-intel/backend/app/api/connect.py`:

```python
"""Connect flows under /v2 (Phase 3b): import a public ESPN league, connect a private one with the caller's cookies,
invites, the caller's credential, manual refresh, admin delete. Behind MULTILEAGUE_CONNECT_ENABLED (and
MULTILEAGUE_ENABLED): with either off every route here is a plain 404.

Every league route authorizes first (leagues_v2.league_tx). Cookies:
  * are read from the request body by `secret_body`, which never echoes input in an error (FastAPI's 422 would);
  * are checked with one GET of the league's own ESPN endpoint, then encrypted to the credential public key
    (the API cannot decrypt them) and stored through fi.store_credential;
  * are never logged, returned or kept in an exception message.
After the request transaction committed work, a background task starts the sync drain with a fine-grained token
that can only dispatch Actions (the job row exists whatever that call does).
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from ..config import get_settings
from .leagues_v2 import NOT_FOUND, REQUEST_TX, SIGN_IN, _json, _no_content, _signed_in, league_tx, require_enabled

log = logging.getLogger(__name__)
REPO = "hhavynn/fantasy"
WORKFLOW = "sync-drain.yml"
CONSENT = ("I understand these cookies give access to my ESPN account. They are stored encrypted, used only to read "
           "this league, and stop working when I sign out of ESPN everywhere.")
BAD_BODY = "The request body is not valid."
ESPN_UNREACHABLE = "ESPN could not be reached. Try again in a few minutes."


def require_connect_enabled() -> None:
    if not get_settings().multileague_connect_enabled:
        raise HTTPException(404, "Not Found")


router = APIRouter(prefix="/v2", dependencies=[Depends(require_enabled), Depends(require_connect_enabled)])


# ---- input ------------------------------------------------------------------------------------------

async def secret_body(request: Request) -> dict:
    """The JSON object body; a malformed one is a fixed 422 that never repeats what was sent."""
    try:
        body = await request.json()
    except Exception:   # noqa: BLE001 - any decoding problem is the same answer
        raise HTTPException(422, BAD_BODY) from None
    if not isinstance(body, dict):
        raise HTTPException(422, BAD_BODY)
    return body


_DIGITS = re.compile(r"[0-9]{1,12}")
_COOKIE = re.compile(r"[A-Za-z0-9%+/=._-]{20,2000}")
_SWID = re.compile(r"\{?[0-9A-Fa-f-]{36}\}?")


def parse_league(value: Any, season: Any, default_season: int) -> tuple[str, int]:
    """A league id or an ESPN league URL (…?leagueId=123&seasonId=2026), and the season."""
    text = str(value or "").strip()
    url_season = None
    if not _DIGITS.fullmatch(text):
        q = parse_qs(urlparse(text).query)
        text = (q.get("leagueId") or [""])[0]
        url_season = (q.get("seasonId") or [None])[0]
    if not _DIGITS.fullmatch(text):
        raise HTTPException(422, "Enter an ESPN league id or the league's URL.")
    raw = season if season not in (None, "") else url_season
    try:
        year = int(raw) if raw not in (None, "") else int(default_season)
    except (TypeError, ValueError):
        raise HTTPException(422, "The season must be a year.") from None
    if not 1990 <= year <= 2100:
        raise HTTPException(422, "The season must be a year.")
    return text.lstrip("0") or "0", year


def parse_cookies(body: dict) -> dict:
    if body.get("consent") is not True:
        raise HTTPException(422, "Confirm that you understand what the cookies give access to.")
    s2, swid = body.get("espn_s2"), body.get("swid")
    if not isinstance(s2, str) or not _COOKIE.fullmatch(s2.strip()) or not isinstance(swid, str) \
            or not _SWID.fullmatch(swid.strip()):
        raise HTTPException(422, "Paste both ESPN cookies: espn_s2 and SWID.")
    swid = swid.strip()
    return {"espn_s2": s2.strip(), "swid": swid if swid.startswith("{") else "{" + swid.strip("{}") + "}"}


# ---- side effects (overridden in tests) -------------------------------------------------------------

def espn():
    """The ESPN reads this module uses; tests override it through app.dependency_overrides."""
    from ..sync import espn_source
    return espn_source


def dispatch_drain() -> bool:
    """Start the sync drain now (workflow_dispatch). Best effort: the scheduled drain picks the job up anyway."""
    import requests
    token = get_settings().github_dispatch_token
    if not token:
        return False
    try:
        r = requests.post(f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
                          json={"ref": "main"}, timeout=5,
                          headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28"})
    except requests.RequestException as err:
        log.warning("sync drain dispatch failed: %s", type(err).__name__)
        return False
    if r.status_code != 204:
        log.warning("sync drain dispatch answered HTTP %s", r.status_code)
    return r.status_code == 204


def drain_dispatcher():
    return dispatch_drain


def _keyring():
    from ..control_plane.credential_crypto import PublicKeyring
    keys = get_settings().credential_public_keys
    if not keys:
        raise HTTPException(503, "Private leagues are not available in this deployment.")
    return PublicKeyring(keys)


def _mutation_error(err) -> HTTPException:
    from ..control_plane import mutations as m
    status, detail = {
        m.SignInRequired: (401, SIGN_IN),
        m.NotAdmin: (403, "Only a league admin can do that."),
        m.NotAMember: (404, NOT_FOUND),
        m.NotOwner: (403, "Only the person who connected these cookies can remove them."),
        m.CredentialExists: (409, "Another member's cookies already sync this league."),
        m.LeagueUnavailable: (404, NOT_FOUND),
        m.LeaguePublic: (409, "This league is public on ESPN: import it without cookies."),
        m.LeagueQuota: (429, "You have added too many leagues today. Try again tomorrow."),
        m.NoCredential: (409, "Store your ESPN cookies first."),
        m.NotLeagueOwner: (403, "Your ESPN account does not own a team in this league. Ask a member for an invite."),
        m.InviteInvalid: (404, "This invite is not valid (used, expired or revoked)."),
        m.LastAdmin: (409, "A league needs at least one admin."),
        m.SyncPaused: (409, "Syncing is paused for this league: its ESPN cookies stopped working or ESPN no longer has "
                            "it. A member can store fresh cookies in the league's settings."),
    }.get(type(err), (400, "That could not be done."))
    return HTTPException(status, detail)


def _mutate(fn):
    from ..control_plane import mutations as m
    try:
        return fn()
    except m.ControlPlaneError as err:
        raise _mutation_error(err) from None


# ---- import / connect -------------------------------------------------------------------------------

@router.post("/leagues/import", status_code=201)
def import_league(background: BackgroundTasks, body: dict = Depends(secret_body), tx=REQUEST_TX,
                  source=Depends(espn), dispatch=Depends(drain_dispatcher)):
    """A public ESPN league: read it once without cookies, register it, pin it, queue its first sync."""
    from ..control_plane import mutations
    con, principal = tx
    if not principal.is_authenticated:
        raise HTTPException(401, SIGN_IN)
    external_id, season = parse_league(body.get("league"), body.get("season"), get_settings().season)
    verdict = source.probe(int(external_id), season)
    if verdict == "private":
        return _json({"detail": "This league is private on ESPN. Connect it with your ESPN cookies.",
                      "needs_cookies": True}, 409)
    if verdict == "missing":
        raise HTTPException(404, "ESPN has no league with that id for that season.")
    if verdict != "public":
        raise HTTPException(503, ESPN_UNREACHABLE)
    league_id = _mutate(lambda: mutations.import_public_league(con, principal, "espn", external_id, season))
    background.add_task(dispatch)
    return _json({"league_id": league_id, "visibility": "public"}, 201)


@router.post("/leagues/connect", status_code=201)
def connect_league(background: BackgroundTasks, body: dict = Depends(secret_body), tx=REQUEST_TX,
                   source=Depends(espn), dispatch=Depends(drain_dispatcher)):
    """A private ESPN league with the caller's cookies (consent required). A league that turns out to be public is
    imported instead and the cookies are dropped unused."""
    from ..control_plane import mutations
    con, principal = tx
    if not principal.is_authenticated:
        raise HTTPException(401, SIGN_IN)
    external_id, season = parse_league(body.get("league"), body.get("season"), get_settings().season)
    cookies = parse_cookies(body)
    anonymous = source.probe(int(external_id), season)
    if anonymous == "public":
        league_id = _mutate(lambda: mutations.import_public_league(con, principal, "espn", external_id, season))
        background.add_task(dispatch)
        return _json({"league_id": league_id, "visibility": "public", "cookies": "not_needed"}, 201)
    if anonymous == "unknown":
        raise HTTPException(503, ESPN_UNREACHABLE)
    verdict, payload = source.read_league(int(external_id), season, cookies, views=("mTeam", "mSettings"))
    if verdict == "private":
        raise HTTPException(400, "ESPN did not accept these cookies for this league.")
    if verdict == "missing":
        raise HTTPException(404, "ESPN has no league with that id for that season.")
    if verdict != "ok":
        raise HTTPException(503, ESPN_UNREACHABLE)
    teams = source.league_teams(payload, cookies["swid"])
    owned = next((t["team_id"] for t in teams if t["owned"]), None)
    name = ((payload.get("settings") or {}).get("name") or None)
    keyring = _keyring()
    league_id = _mutate(lambda: mutations.connect_private_league(con, principal, "espn", external_id, season, name,
                                                                 owned is not None))
    credential = "stored"
    try:
        with con.transaction():                     # a savepoint: a member joining an already-synced league
            cid = str(uuid.uuid4())
            enc = keyring.encrypt(cookies, league_id=league_id, credential_id=cid, owner_user_id=principal.user_id)
            mutations.store_credential(con, principal, league_id, enc, cid)
            mutations.start_private_sync(con, principal, league_id)
    except mutations.CredentialExists:
        credential = "existing"                     # never replaces another member's live credential
    except mutations.ControlPlaneError as err:
        raise _mutation_error(err) from None
    finally:
        cookies.clear()
    if owned is not None:
        _mutate(lambda: mutations.set_perspective(con, principal, league_id, owned))
    if credential == "stored":
        background.add_task(dispatch)
    return _json({"league_id": league_id, "visibility": "private", "cookies": credential, "team_id": owned,
                  "teams": [{"team_id": t["team_id"], "name": t["name"]} for t in teams]}, 201)


# ---- the caller's credential --------------------------------------------------------------------------

def signed_in_league(ltx=Depends(league_tx, scope="function")):
    """league_tx, then sign-in: an unknown league is 404 and an anonymous caller 401 before any body is read."""
    con, access = ltx
    return con, access, _signed_in(access)


@router.put("/leagues/{league_id}/credential")
def replace_credential(background: BackgroundTasks, sltx=Depends(signed_in_league),
                       body: dict = Depends(secret_body), source=Depends(espn),
                       dispatch=Depends(drain_dispatcher)):
    """Attach or replace the caller's own cookies (a member; only when no one else's credential is live)."""
    from ..control_plane import mutations
    con, access, principal = sltx
    cookies = parse_cookies(body)
    row = con.execute("SELECT external_id, season FROM fi.leagues WHERE id = %s", [access.league_id]).fetchone()
    verdict, _ = source.read_league(int(row[0]), row[1], cookies)
    if verdict == "private":
        raise HTTPException(400, "ESPN did not accept these cookies for this league.")
    if verdict != "ok":
        raise HTTPException(503, ESPN_UNREACHABLE)
    keyring = _keyring()
    try:
        cid = str(uuid.uuid4())
        enc = keyring.encrypt(cookies, league_id=access.league_id, credential_id=cid, owner_user_id=principal.user_id)
        _mutate(lambda: mutations.store_credential(con, principal, access.league_id, enc, cid))
        queued = _mutate(lambda: mutations.request_sync(con, principal, access.league_id))
    finally:
        cookies.clear()
    if queued:
        background.add_task(dispatch)
    return _json({"cookies": "stored"})


@router.delete("/leagues/{league_id}/credential", status_code=204)
def remove_credential(ltx=Depends(league_tx, scope="function")):
    from ..control_plane import mutations
    con, access = ltx
    principal = _signed_in(access)
    _mutate(lambda: mutations.revoke_credential(con, principal, access.league_id))
    return _no_content()


# ---- invites ----------------------------------------------------------------------------------------------

@router.get("/leagues/{league_id}/invites")
def invites(ltx=Depends(league_tx, scope="function")):
    from ..control_plane import mutations
    con, access = ltx
    principal = _signed_in(access)
    if access.role != "admin":
        raise HTTPException(403, "Only a league admin can do that.")
    return _json({"invites": mutations.list_invites(con, principal, access.league_id)})


@router.post("/leagues/{league_id}/invites", status_code=201)
def create_invite(ltx=Depends(league_tx, scope="function")):
    """A single-use, 7-day viewer invite. The token is returned once and only its hash is stored."""
    from ..control_plane import mutations
    con, access = ltx
    principal = _signed_in(access)
    invite_id, token = _mutate(lambda: mutations.create_invite(con, principal, access.league_id))
    return _json({"invite_id": invite_id, "token": token}, 201)


@router.delete("/leagues/{league_id}/invites/{invite_id}", status_code=204)
def revoke_invite(invite_id: uuid.UUID, ltx=Depends(league_tx, scope="function")):
    from ..control_plane import mutations
    con, access = ltx
    principal = _signed_in(access)
    _mutate(lambda: mutations.revoke_invite(con, principal, str(invite_id)))
    return _no_content()


@router.post("/invites/redeem")
def redeem_invite(body: dict = Depends(secret_body), tx=REQUEST_TX):
    from ..control_plane import mutations
    con, principal = tx
    if not principal.is_authenticated:
        raise HTTPException(401, SIGN_IN)
    league_id = _mutate(lambda: mutations.redeem_invite(con, principal, body.get("token")))
    return _json({"league_id": league_id})


# ---- refresh and delete ------------------------------------------------------------------------------------

@router.post("/leagues/{league_id}/sync", status_code=202)
def refresh(background: BackgroundTasks, ltx=Depends(league_tx, scope="function"),
            dispatch=Depends(drain_dispatcher)):
    from ..control_plane import mutations
    con, access = ltx
    principal = _signed_in(access)
    queued = _mutate(lambda: mutations.request_sync(con, principal, access.league_id))
    if queued:
        background.add_task(dispatch)
    return _json({"queued": queued is not None}, 202)


@router.delete("/leagues/{league_id}", status_code=204)
def delete(ltx=Depends(league_tx, scope="function")):
    from ..control_plane import mutations
    con, access = ltx
    principal = _signed_in(access)
    _mutate(lambda: mutations.delete_league(con, principal, access.league_id))
    return _no_content()


def nudge_probe(principal, league_id: str) -> None:
    """Spec §2: a 404 for a public league whose visibility check expired starts a probe (rate-limited in Postgres:
    one per league per 5 minutes). Its own short transaction (the request's own rolls back with the 404); never
    changes the answer, never raises. Called from the thread pool (league_tx, league_scope)."""
    settings = get_settings()
    if not (settings.multileague_connect_enabled and settings.supabase_db_url):
        return
    from ..control_plane import mutations
    from ..control_plane.pg import get_control_plane
    try:
        with get_control_plane().request_tx(principal) as con:
            queued = mutations.request_probe(con, league_id)
    except Exception as err:   # noqa: BLE001 - best effort; the caller gets its 404 regardless
        log.warning("probe request failed: %s", type(err).__name__)
        return
    if queued:
        dispatch_drain()
```

Mount `connect_router` in `app/main.py`; `leagues_v2.league_tx` and `league_scope._resolve` call `nudge_probe` before their 404; the authorization matrix turns the connect flag on and gives the new routes their own rule (404 when invisible, 401 when anonymous, never 404 or 5xx otherwise); the legacy-import audit exempts `api/connect.py` like `api/leagues_v2.py`.

- [ ] **Step 4: Run them and see them pass**

```bash
cd fantasy-intel/backend && uv run pytest tests/test_connect_api.py tests/test_v2_authorization_matrix.py tests/test_league_scope_audit.py -q
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/backend/app/api/connect.py fantasy-intel/backend/tests/test_connect_api.py fantasy-intel/backend/app/control_plane/mutations.py fantasy-intel/backend/app/config.py fantasy-intel/backend/app/sync/espn_source.py fantasy-intel/backend/app/api/leagues_v2.py fantasy-intel/backend/app/api/league_scope.py fantasy-intel/backend/app/main.py fantasy-intel/backend/tests/test_v2_authorization_matrix.py fantasy-intel/backend/tests/test_league_scope_audit.py
git commit -m "Phase 3 task 11: connect API (import, private connect, credential, invites, refresh, delete, probe on expiry)"
```


### Task 12: Sign-in

**Files:**
- Create: `fantasy-intel/frontend/lib/auth-config.ts`
- Create: `fantasy-intel/frontend/lib/auth-config.test.ts`
- Create: `fantasy-intel/frontend/lib/supabase/server.ts`
- Create: `fantasy-intel/frontend/lib/supabase/proxy.ts`
- Create: `fantasy-intel/frontend/lib/api-errors.ts`
- Create: `fantasy-intel/frontend/app/login/page.tsx`
- Create: `fantasy-intel/frontend/app/login/LoginForm.tsx`
- Create: `fantasy-intel/frontend/app/login/actions.ts`
- Create: `fantasy-intel/frontend/app/auth/callback/route.ts`
- Create: `fantasy-intel/frontend/components/AccountMenu.tsx`
- Modify: `fantasy-intel/frontend/proxy.ts`
- Modify: `fantasy-intel/frontend/lib/api.ts`
- Modify: `fantasy-intel/frontend/lib/league-scope.ts`
- Modify: `fantasy-intel/frontend/lib/league-scope.test.ts`
- Modify: `fantasy-intel/frontend/app/layout.tsx`
- Modify: `fantasy-intel/frontend/app/globals.css`
- Modify: `fantasy-intel/frontend/components/BackendProblem.tsx`
- Modify: `fantasy-intel/frontend/package.json`

**Interfaces:**
- Produces: authConfig(env) -> {url, key, signupsOpen} | null; safeNext(); hasSessionCookie(); siteOrigin(); supabaseServer(), accessToken(), currentUser(); refreshSession(request, response); sendLink / signOut server actions; /auth/callback (PKCE). lib/api.ts forwards the access token on /v2 calls only. /login, /auth and /invite are never league-scoped by the proxy.
- Dependencies: @supabase/ssr 0.12.7, @supabase/supabase-js 2.117.2 (exact, released over two weeks before use).

- [ ] **Step 1: Write the failing tests**

`fantasy-intel/frontend/lib/auth-config.test.ts`:

```ts
import assert from "node:assert/strict";
import test from "node:test";
import { EMAIL_RE, authConfig, hasSessionCookie, safeNext, siteOrigin } from "./auth-config.ts";

const ENV = { MULTILEAGUE_UI_ENABLED: "1", SUPABASE_URL: "https://kdikrcmvfuzqsgahqaxf.supabase.co/", SUPABASE_PUBLISHABLE_KEY: "sb_publishable_x" };

test("sign-in needs the UI flag and both Supabase settings; sign-ups stay closed unless opened", () => {
  assert.deepEqual(authConfig(ENV), { url: "https://kdikrcmvfuzqsgahqaxf.supabase.co", key: "sb_publishable_x", signupsOpen: false });
  assert.equal(authConfig({ ...ENV, MULTILEAGUE_UI_ENABLED: undefined }), null);
  assert.equal(authConfig({ ...ENV, SUPABASE_PUBLISHABLE_KEY: "" }), null);
  assert.equal(authConfig({ ...ENV, SUPABASE_URL: "https://evil.example" }), null);
  assert.equal(authConfig({ ...ENV, SIGNUPS_OPEN: "1" })?.signupsOpen, true);
});

test("after sign-in the redirect stays inside the site", () => {
  assert.equal(safeNext("/l/lg_abcdefghijklmnop/waivers"), "/l/lg_abcdefghijklmnop/waivers");
  for (const bad of [null, "", "https://evil.example", "//evil.example", "/\\evil.example", "leagues"]) {
    assert.equal(safeNext(bad), "/leagues", String(bad));
  }
});

test("only Supabase's session cookies count as a session", () => {
  assert.equal(hasSessionCookie(["fi_league", "sb-kdikrcmvfuzqsgahqaxf-auth-token"]), true);
  assert.equal(hasSessionCookie(["sb-kdikrcmvfuzqsgahqaxf-auth-token.0"]), true);
  assert.equal(hasSessionCookie(["fi_league", "sb-code-verifier", "theme"]), false);
});

test("the email link returns to the configured site, else to the request's host", () => {
  const headers = (h: Record<string, string>) => (n: string) => h[n] ?? null;
  assert.equal(siteOrigin(headers({ host: "x.vercel.app" }), { SITE_URL: "https://fantasy.example/" }), "https://fantasy.example");
  assert.equal(siteOrigin(headers({ "x-forwarded-host": "x.vercel.app", "x-forwarded-proto": "https" }), {}), "https://x.vercel.app");
  assert.equal(siteOrigin(headers({ host: "localhost:3000" }), {}), "http://localhost:3000");
});

test("email addresses are checked for shape only", () => {
  assert.ok(EMAIL_RE.test("havyn@example.com"));
  for (const bad of ["", "no-at", "a@b", "a b@c.de"]) assert.ok(!EMAIL_RE.test(bad), bad);
});
```

- [ ] **Step 2: Run them and see them fail**

```bash
cd fantasy-intel/frontend && npm test
```
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

`fantasy-intel/frontend/lib/auth-config.ts`:

```ts
// Sign-in configuration and the small pure decisions around it (Supabase Auth, email link). No Next, no network:
// node --test covers them. The Supabase client itself lives in lib/supabase/*.ts (server only).

export type AuthConfig = { url: string; key: string; signupsOpen: boolean };

/**
 * Sign-in is on only with the multi-league UI flag and both Supabase settings. The URL and the publishable key are
 * read on the server only (sign-in runs in server actions and the proxy), so nothing here reaches the browser.
 * New accounts are refused until SIGNUPS_OPEN=1 (Phase 6's launch gate): until then only existing users sign in.
 */
export function authConfig(env: Record<string, string | undefined>): AuthConfig | null {
  if (env.MULTILEAGUE_UI_ENABLED !== "1") return null;
  const url = (env.SUPABASE_URL ?? "").trim().replace(/\/+$/, "");
  const key = (env.SUPABASE_PUBLISHABLE_KEY ?? "").trim();
  if (!/^https:\/\/[a-z0-9-]+\.supabase\.co$/.test(url) || !key) return null;
  return { url, key, signupsOpen: env.SIGNUPS_OPEN === "1" };
}

/** Where to go after signing in: an in-app path only (never another origin), else the leagues page. */
export function safeNext(value: string | null | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return "/leagues";
  return value;
}

/** Supabase's session cookies are named sb-<project ref>-auth-token (possibly chunked: …-auth-token.0, .1). */
export function hasSessionCookie(names: readonly string[]): boolean {
  return names.some((n) => /^sb-[a-z0-9]+-auth-token(\.\d+)?$/.test(n));
}

/** The origin the email link must come back to: SITE_URL when set (production), else the request's own host. */
export function siteOrigin(get: (name: string) => string | null, env: Record<string, string | undefined>): string {
  const configured = (env.SITE_URL ?? "").trim().replace(/\/+$/, "");
  if (/^https?:\/\/[^/]+$/.test(configured)) return configured;
  const host = get("x-forwarded-host") ?? get("host") ?? "localhost:3000";
  const proto = get("x-forwarded-proto") ?? (host.startsWith("localhost") ? "http" : "https");
  return `${proto}://${host}`;
}

export const EMAIL_RE = /^[^\s@]{1,64}@[^\s@]{1,190}\.[^\s@]{2,}$/;
```

`fantasy-intel/frontend/lib/supabase/server.ts`:

```ts
import "server-only";
import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";
import { authConfig } from "../auth-config";

// The Supabase client for server components, server actions and route handlers. Sessions live in Supabase's own
// httpOnly cookies (set through Next's cookie store); the access token is forwarded to the API as a bearer token and
// verified there (backend/app/control_plane/jwt_auth.py). Null when sign-in is off.

export async function supabaseServer() {
  const config = authConfig(process.env);
  if (!config) return null;
  const store = await cookies();
  return createServerClient(config.url, config.key, {
    cookies: {
      getAll: () => store.getAll(),
      setAll: (list) => {
        try {
          for (const { name, value, options } of list) store.set(name, value, options);
        } catch {
          // A server component cannot set cookies; the proxy refreshes the session on the next request.
        }
      },
    },
  });
}

/** The signed-in user's access token, or null (signed out, sign-in off, or an unreadable session). */
export async function accessToken(): Promise<string | null> {
  const supabase = await supabaseServer();
  if (!supabase) return null;
  try {
    const { data } = await supabase.auth.getSession();
    return data.session?.access_token ?? null;
  } catch {
    return null;
  }
}

export type SignedInUser = { id: string; email: string | null };

/** The signed-in user from the verified token claims, or null. */
export async function currentUser(): Promise<SignedInUser | null> {
  const supabase = await supabaseServer();
  if (!supabase) return null;
  try {
    const { data } = await supabase.auth.getClaims();
    const claims = data?.claims;
    if (!claims?.sub || claims.is_anonymous) return null;
    return { id: String(claims.sub), email: typeof claims.email === "string" ? claims.email : null };
  } catch {
    return null;
  }
}
```

`fantasy-intel/frontend/lib/supabase/proxy.ts`:

```ts
import { createServerClient } from "@supabase/ssr";
import type { NextRequest, NextResponse } from "next/server";
import { authConfig, hasSessionCookie } from "../auth-config";

/**
 * Refresh an expiring Supabase session on the way through the proxy and copy the new cookies onto `response`.
 * Only requests that already carry a session cookie pay for it: anonymous visitors never reach Supabase here.
 */
export async function refreshSession(request: NextRequest, response: NextResponse): Promise<NextResponse> {
  const config = authConfig(process.env);
  if (!config || !hasSessionCookie(request.cookies.getAll().map((c) => c.name))) return response;
  const supabase = createServerClient(config.url, config.key, {
    cookies: {
      getAll: () => request.cookies.getAll(),
      setAll: (list) => {
        for (const { name, value, options } of list) {
          request.cookies.set(name, value);
          response.cookies.set(name, value, options);
        }
      },
    },
  });
  try {
    await supabase.auth.getClaims();      // refreshes the access token when it has expired
  } catch {
    // An unreachable Supabase must not take the site down: the page renders signed out.
  }
  return response;
}
```

`fantasy-intel/frontend/app/login/actions.ts`:

```ts
"use server";

import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { EMAIL_RE, authConfig, safeNext, siteOrigin } from "@/lib/auth-config";
import { supabaseServer } from "@/lib/supabase/server";

export type LoginState = { status: "idle" | "sent" | "error"; message: string | null };

const OFF = "Sign-in is not available in this deployment.";

/**
 * Email a one-time sign-in link. Until sign-ups open (SIGNUPS_OPEN=1, Phase 6) only existing accounts get one; the
 * answer is the same either way, so the form never tells anyone whether an address has an account.
 */
export async function sendLink(_prev: LoginState, form: FormData): Promise<LoginState> {
  const config = authConfig(process.env);
  const supabase = await supabaseServer();
  if (!config || !supabase) return { status: "error", message: OFF };
  const email = String(form.get("email") ?? "").trim().toLowerCase();
  if (!EMAIL_RE.test(email)) return { status: "error", message: "Enter your email address." };
  const h = await headers();
  const next = safeNext(String(form.get("next") ?? ""));
  const origin = siteOrigin((n) => h.get(n), process.env);
  const { error } = await supabase.auth.signInWithOtp({
    email,
    options: { shouldCreateUser: config.signupsOpen, emailRedirectTo: `${origin}/auth/callback?next=${encodeURIComponent(next)}` },
  });
  if (error && error.status === 429) return { status: "error", message: "Too many sign-in emails. Try again in a little while." };
  // Any other refusal (no such account while sign-ups are closed) looks the same as success.
  return { status: "sent", message: "If this address has an account, a sign-in link is on its way. It works once, in this browser." };
}

export async function signOut(): Promise<void> {
  const supabase = await supabaseServer();
  if (supabase) await supabase.auth.signOut();
  redirect("/");
}
```

`fantasy-intel/frontend/app/auth/callback/route.ts`:

```ts
import { NextResponse, type NextRequest } from "next/server";
import { safeNext } from "@/lib/auth-config";
import { supabaseServer } from "@/lib/supabase/server";

// The email link lands here with a one-time code (PKCE: the verifier is the cookie set when the link was requested,
// so a link only works in the browser that asked for it). The session cookies are set, then the visitor goes on.
export async function GET(request: NextRequest) {
  const url = request.nextUrl;
  const code = url.searchParams.get("code");
  const next = safeNext(url.searchParams.get("next"));
  const supabase = await supabaseServer();
  if (supabase && code) {
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (!error) return NextResponse.redirect(new URL(next, url.origin));
  }
  return NextResponse.redirect(new URL(`/login?error=1&next=${encodeURIComponent(next)}`, url.origin));
}
```

`fantasy-intel/frontend/components/AccountMenu.tsx`:

```tsx
import Link from "next/link";
import { signOut } from "@/app/login/actions";
import { authConfig } from "@/lib/auth-config";
import { currentUser } from "@/lib/supabase/server";

/** "Sign in" or the signed-in email with "Sign out"; nothing when sign-in is off. */
export async function AccountMenu() {
  if (!authConfig(process.env)) return null;
  const user = await currentUser();
  if (!user) {
    return <Link href="/login" prefetch={false} className="account-link">Sign in</Link>;
  }
  return (
    <details className="account-menu">
      <summary className="account-link" title={user.email ?? undefined}>Account</summary>
      <div className="account-pop" role="menu">
        {user.email ? <p className="faint account-email">{user.email}</p> : null}
        <Link role="menuitem" href="/leagues">Your leagues</Link>
        <form action={signOut}><button type="submit" role="menuitem" className="account-signout">Sign out</button></form>
      </div>
    </details>
  );
}
```

- [ ] **Step 4: Run them and see them pass**

```bash
cd fantasy-intel/frontend && npm run lint && npm run typecheck && npm test && npm run build
```
Expected: clean; the build lists /login, /auth/callback, /invite, /leagues, /leagues/[league].

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/frontend/lib/auth-config.ts fantasy-intel/frontend/lib/auth-config.test.ts fantasy-intel/frontend/lib/supabase/server.ts fantasy-intel/frontend/lib/supabase/proxy.ts fantasy-intel/frontend/lib/api-errors.ts fantasy-intel/frontend/app/login/page.tsx fantasy-intel/frontend/app/login/LoginForm.tsx fantasy-intel/frontend/app/login/actions.ts fantasy-intel/frontend/app/auth/callback/route.ts fantasy-intel/frontend/components/AccountMenu.tsx fantasy-intel/frontend/proxy.ts fantasy-intel/frontend/lib/api.ts fantasy-intel/frontend/lib/league-scope.ts fantasy-intel/frontend/lib/league-scope.test.ts fantasy-intel/frontend/app/layout.tsx fantasy-intel/frontend/app/globals.css fantasy-intel/frontend/components/BackendProblem.tsx fantasy-intel/frontend/package.json
git commit -m "Phase 3 tasks 12-13: sign-in (Supabase email link) and the connect / settings / invite pages"
```


### Task 13: Connect, settings and invite pages

**Files:**
- Create: `fantasy-intel/frontend/lib/my-leagues.ts`
- Create: `fantasy-intel/frontend/lib/my-leagues.test.ts`
- Create: `fantasy-intel/frontend/app/leagues/AddLeague.tsx`
- Create: `fantasy-intel/frontend/app/leagues/LeagueActions.tsx`
- Create: `fantasy-intel/frontend/app/leagues/[league]/page.tsx`
- Create: `fantasy-intel/frontend/app/leagues/[league]/forms.tsx`
- Create: `fantasy-intel/frontend/app/invite/page.tsx`
- Create: `fantasy-intel/frontend/app/invite/RedeemInvite.tsx`
- Modify: `fantasy-intel/frontend/app/leagues/page.tsx`
- Modify: `fantasy-intel/frontend/app/leagues/actions.ts`
- Modify: `fantasy-intel/frontend/app/leagues/leagues.module.css`

**Interfaces:**
- Consumes: Task 11 routes; Task 12 currentUser and the forwarded token.
- Produces: parseMyLeague(s), leagueState, leagueName, parseLeagueInput, validSeason, cleanCookies, CONSENT_TEXT; server actions addLeague, connectPrivate, refreshLeague, pinLeague, unpinLeague, saveTeam, replaceCookies, removeCookies, createInvite, revokeInvite, deleteLeague, redeemInvite. The Phase 1 prototype's connect components and lib/leagues.ts are removed; the flag-off page keeps its text.

- [ ] **Step 1: Write the failing tests**

`fantasy-intel/frontend/lib/my-leagues.test.ts`:

```ts
import assert from "node:assert/strict";
import test from "node:test";
import {
  cleanCookies, defaultSeason, leagueName, leagueState, multileagueUiEnabled, parseLeagueInput, parseMyLeagues,
  validSeason,
} from "./my-leagues.ts";

const ACCESS = {
  league_id: "lg_abcdefghijklmnop", provider: "espn", external_league_id: "4242", season: 2026, name: null,
  visibility: "private", status: "active", role: "admin", pinned: false, perspective_team_id: "3",
  sync_state: "ready", data: { data_version: "v", global_version: "g", committed_at: "2026-10-21T12:00:00Z" },
};

test("the flag is exactly 1", () => {
  assert.equal(multileagueUiEnabled("1"), true);
  for (const v of [undefined, "", "0", "true"]) assert.equal(multileagueUiEnabled(v), false);
});

test("the backend's league list becomes the page's view model; malformed rows are dropped", () => {
  const [l, ...rest] = parseMyLeagues({ leagues: [ACCESS, { league_id: 7 }, null] });
  assert.equal(rest.length, 0);
  assert.deepEqual(l, { id: "lg_abcdefghijklmnop", provider: "espn", externalId: "4242", season: 2026, name: null,
    visibility: "private", status: "active", role: "admin", pinned: false, teamId: "3", syncState: "ready",
    committedAt: "2026-10-21T12:00:00Z" });
  assert.equal(leagueName(l), "ESPN league 4242");
  assert.deepEqual(parseMyLeagues({}), []);
});

test("the league's state says what readers see and why", () => {
  assert.equal(leagueState({ status: "stale", syncState: "ready", visibility: "private" }).label, "Sync paused");
  assert.equal(leagueState({ status: "active", syncState: "first_sync_running", visibility: "public" }).tone, "info");
  assert.match(leagueState({ status: "unavailable", syncState: "ready", visibility: "private" }).detail, /privately/);
  assert.equal(leagueState({ status: "active", syncState: "ready", visibility: "public" }).label, "Up to date");
});

test("a league id or its ESPN URL", () => {
  assert.deepEqual(parseLeagueInput(" 4242 "), { leagueId: "4242", season: null });
  assert.deepEqual(parseLeagueInput("https://fantasy.espn.com/football/league?leagueId=4242&seasonId=2025"),
    { leagueId: "4242", season: 2025 });
  assert.equal(parseLeagueInput("https://fantasy.espn.com/football/"), null);
  assert.equal(validSeason("2026"), 2026);
  assert.equal(validSeason("26"), null);
  assert.equal(defaultSeason(new Date("2026-03-01T00:00:00Z")), 2025);
  assert.equal(defaultSeason(new Date("2026-10-01T00:00:00Z")), 2026);
});

test("cookies are trimmed and the SWID braced; anything else is refused", () => {
  const s2 = "AEB" + "x".repeat(40);
  assert.deepEqual(cleanCookies(` ${s2} `, "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"),
    { espn_s2: s2, swid: "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}" });
  assert.equal(cleanCookies("short", "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}"), null);
  assert.equal(cleanCookies(s2, "not-a-swid"), null);
});
```

- [ ] **Step 2: Run them and see them fail**

```bash
cd fantasy-intel/frontend && npm test
```
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

`fantasy-intel/frontend/lib/my-leagues.ts`:

```ts
// View-model logic for /leagues and a league's settings page (Phase 3): what the /v2 payloads mean for the UI.
// Pure functions only (no Next, no fetch, no environment) so node --test covers them. The backend is authoritative.

export const NOT_ENABLED_REASON = "Multi-league support is not enabled in this deployment.";

/** The feature flag is exactly "1": anything else (unset, "0", "true") is off. */
export function multileagueUiEnabled(value: string | undefined): boolean {
  return value === "1";
}

export type Tone = "good" | "warn" | "bad" | "info" | "neutral";

/** One league as GET /v2/leagues and GET /v2/leagues/{id} describe it (backend authz.Access.to_json). */
export type MyLeague = {
  id: string;
  provider: string;
  externalId: string;
  season: number;
  name: string | null;
  visibility: "public" | "private";
  status: string;                     // active | stale | unavailable | dormant
  role: "admin" | "viewer" | null;
  pinned: boolean;
  teamId: string | null;              // the saved perspective
  syncState: string;                  // ready | first_sync_queued | first_sync_running | first_sync_failed | no_data
  committedAt: string | null;         // when the served version was published
};

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v : null);

export function parseMyLeague(raw: unknown): MyLeague | null {
  if (!isRecord(raw)) return null;
  const { league_id, provider, external_league_id, season } = raw;
  if (typeof league_id !== "string" || typeof provider !== "string" || typeof external_league_id !== "string"
      || typeof season !== "number") return null;
  const data = isRecord(raw.data) ? raw.data : null;
  return {
    id: league_id, provider, externalId: external_league_id, season,
    name: str(raw.name),
    visibility: raw.visibility === "private" ? "private" : "public",
    status: str(raw.status) ?? "active",
    role: raw.role === "admin" || raw.role === "viewer" ? raw.role : null,
    pinned: raw.pinned === true,
    teamId: str(raw.perspective_team_id),
    syncState: str(raw.sync_state) ?? "no_data",
    committedAt: data ? str(data.committed_at) : null,
  };
}

export function parseMyLeagues(payload: unknown): MyLeague[] {
  const list = isRecord(payload) && Array.isArray(payload.leagues) ? payload.leagues : [];
  return list.map(parseMyLeague).filter((l): l is MyLeague => l !== null);
}

export function leagueName(l: Pick<MyLeague, "name" | "externalId">): string {
  return l.name?.trim() || `ESPN league ${l.externalId}`;
}

/** What a reader of this league sees right now, in words, and how worried to be. */
export function leagueState(l: Pick<MyLeague, "status" | "syncState" | "visibility">): { label: string; tone: Tone; detail: string } {
  if (l.status === "stale") {
    return { label: "Sync paused", tone: "warn",
      detail: "The ESPN cookies for this league stopped working. The last data stays readable; a member can store fresh cookies in Settings." };
  }
  if (l.status === "unavailable") {
    return { label: "Unavailable on ESPN", tone: "bad",
      detail: l.visibility === "private"
        ? "ESPN no longer shows this league publicly. A member can connect it privately with their cookies."
        : "ESPN no longer finds this league. The last data is kept for now." };
  }
  if (l.status === "dormant") {
    return { label: "Paused (unused)", tone: "neutral", detail: "Nobody opened this league for 30 days. Opening it resumes syncing." };
  }
  switch (l.syncState) {
    case "ready": return { label: "Up to date", tone: "good", detail: "Syncs every few hours, hourly on game days." };
    case "first_sync_queued": return { label: "First sync queued", tone: "info", detail: "Usually ready within a few minutes." };
    case "first_sync_running": return { label: "First sync running", tone: "info", detail: "Usually ready within a few minutes." };
    case "first_sync_failed": return { label: "First sync failed", tone: "bad", detail: "It is retried automatically; you can also refresh it." };
    default: return { label: "No data yet", tone: "neutral", detail: "Refresh to start a sync." };
  }
}

// ---- forms ----------------------------------------------------------------------------------------

export const MIN_SEASON = 1990;
export const MAX_SEASON = 2100;

/** The NFL season a fantasy league plays in: before August it is still last year's. */
export function defaultSeason(now: Date): number {
  return now.getUTCMonth() < 7 ? now.getUTCFullYear() - 1 : now.getUTCFullYear();
}

/** An ESPN league id, or the league page's URL (…leagueId=123…), and its season when the URL names one. */
export function parseLeagueInput(text: string): { leagueId: string; season: number | null } | null {
  const value = text.trim();
  if (/^\d{1,12}$/.test(value)) return { leagueId: value, season: null };
  const id = /[?&]leagueId=(\d{1,12})/.exec(value);
  if (!id) return null;
  const season = /[?&]seasonId=(\d{4})/.exec(value);
  return { leagueId: id[1], season: season ? Number(season[1]) : null };
}

export function validSeason(value: string): number | null {
  const n = Number(value);
  return Number.isInteger(n) && n >= MIN_SEASON && n <= MAX_SEASON ? n : null;
}

export const CONSENT_TEXT = "I understand these cookies give access to my ESPN account. They are stored encrypted, used only to read this league, and stop working when I sign out of ESPN everywhere.";

/** espn_s2 and SWID as pasted from the browser: trimmed, SWID braced. Null when either is missing or malformed. */
export function cleanCookies(s2: string, swid: string): { espn_s2: string; swid: string } | null {
  const a = s2.trim();
  const b = swid.trim().replace(/^\{?/, "{").replace(/\}?$/, "}");
  if (!/^[A-Za-z0-9%+/=._-]{20,2000}$/.test(a) || !/^\{[0-9A-Fa-f-]{36}\}$/.test(b)) return null;
  return { espn_s2: a, swid: b };
}

export function isLeagueRef(value: unknown): value is string {
  return typeof value === "string" && /^lg_[A-Za-z0-9_-]{16,64}$/.test(value);
}
```

`fantasy-intel/frontend/app/leagues/actions.ts`:

```ts
"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { apiRequest } from "@/lib/api";
import { authConfig } from "@/lib/auth-config";
import { cleanCookies, isLeagueRef, multileagueUiEnabled, NOT_ENABLED_REASON, parseLeagueInput, validSeason } from "@/lib/my-leagues";
import { currentUser } from "@/lib/supabase/server";

// Server actions are public POST endpoints: each re-checks the flag and the signed-in user on the server, and the
// backend authorizes again with the user's own token (lib/api.ts forwards it). Cookies are passed straight to the
// backend and never returned, logged or kept in state.

export type AddState = {
  status: "idle" | "ok" | "needs_cookies" | "error";
  message: string | null;
  leagueId?: string;
  league?: string;
  season?: number;
};

export type ActionResult = { ok: boolean; message: string | null };

const SIGN_IN = "Sign in to add or change leagues.";

async function gate(): Promise<string | null> {
  if (!multileagueUiEnabled(process.env.MULTILEAGUE_UI_ENABLED)) return NOT_ENABLED_REASON;
  if (!authConfig(process.env) || !(await currentUser())) return SIGN_IN;
  return null;
}

function readLeague(form: FormData): { league: string; season: number } | { error: string } {
  const parsed = parseLeagueInput(String(form.get("league") ?? ""));
  if (!parsed) return { error: "Enter the league id (the number after leagueId= in the ESPN address) or paste the address." };
  const season = parsed.season ?? validSeason(String(form.get("season") ?? ""));
  if (!season) return { error: "Enter the season as a year, for example 2026." };
  return { league: parsed.leagueId, season };
}

/** Step 1: try the league as a public one. ESPN saying "private" moves the form to step 2 (cookies). */
export async function addLeague(_prev: AddState, form: FormData): Promise<AddState> {
  const blocked = await gate();
  if (blocked) return { status: "error", message: blocked };
  const input = readLeague(form);
  if ("error" in input) return { status: "error", message: input.error };
  const res = await apiRequest<{ league_id: string }>("/v2/leagues/import", { body: input });
  if (res.ok && res.data) {
    revalidatePath("/leagues");
    return { status: "ok", message: "Added. The first sync usually takes a few minutes.", leagueId: res.data.league_id };
  }
  if (!res.ok && res.status === 409) {
    return { status: "needs_cookies", message: "This league is private on ESPN. To add it, paste your ESPN cookies below.", ...input };
  }
  return { status: "error", message: res.ok ? "The league could not be added." : res.error };
}

/** Step 2: a private league, with the visitor's own ESPN cookies and their explicit consent. */
export async function connectPrivate(_prev: AddState, form: FormData): Promise<AddState> {
  const blocked = await gate();
  if (blocked) return { status: "error", message: blocked };
  const input = readLeague(form);
  if ("error" in input) return { status: "error", message: input.error };
  if (form.get("consent") !== "on") return { status: "needs_cookies", message: "Tick the box to confirm you understand what the cookies give access to.", ...input };
  const cookies = cleanCookies(String(form.get("espn_s2") ?? ""), String(form.get("swid") ?? ""));
  if (!cookies) return { status: "needs_cookies", message: "Paste both cookies: espn_s2 (a long value) and SWID (in braces).", ...input };
  const res = await apiRequest<{ league_id: string; cookies: string }>("/v2/leagues/connect",
    { body: { ...input, ...cookies, consent: true } });
  if (res.ok && res.data) {
    revalidatePath("/leagues");
    const joined = res.data.cookies === "existing";
    return { status: "ok", leagueId: res.data.league_id,
      message: joined ? "Joined. This league already syncs with another member's cookies." : "Connected. The first sync usually takes a few minutes." };
  }
  return { status: "needs_cookies", message: res.ok ? "The league could not be connected." : res.error, ...input };
}

async function leagueCall(leagueId: string, path: string, method: "POST" | "PUT" | "DELETE", body?: unknown): Promise<ActionResult> {
  const blocked = await gate();
  if (blocked) return { ok: false, message: blocked };
  if (!isLeagueRef(leagueId)) return { ok: false, message: "Unknown league." };
  const res = await apiRequest(`/v2/leagues/${leagueId}${path}`, { method, body });
  revalidatePath("/leagues");
  revalidatePath(`/leagues/${leagueId}`);
  return res.ok ? { ok: true, message: null } : { ok: false, message: res.error };
}

export async function refreshLeague(leagueId: string): Promise<ActionResult> {
  return leagueCall(leagueId, "/sync", "POST", {});
}

export async function unpinLeague(leagueId: string): Promise<ActionResult> {
  return leagueCall(leagueId, "/pin", "DELETE");
}

export async function pinLeague(leagueId: string): Promise<ActionResult> {
  return leagueCall(leagueId, "/pin", "PUT", {});
}

export async function saveTeam(leagueId: string, _prev: ActionResult, form: FormData): Promise<ActionResult> {
  const team = String(form.get("team") ?? "");
  if (!team) return leagueCall(leagueId, "/perspective", "DELETE");
  if (!/^\d{1,9}$/.test(team)) return { ok: false, message: "Pick a team." };
  return leagueCall(leagueId, "/perspective", "PUT", { team_external_id: team });
}

export async function replaceCookies(leagueId: string, _prev: ActionResult, form: FormData): Promise<ActionResult> {
  if (form.get("consent") !== "on") return { ok: false, message: "Tick the box to confirm you understand what the cookies give access to." };
  const cookies = cleanCookies(String(form.get("espn_s2") ?? ""), String(form.get("swid") ?? ""));
  if (!cookies) return { ok: false, message: "Paste both cookies: espn_s2 (a long value) and SWID (in braces)." };
  return leagueCall(leagueId, "/credential", "PUT", { ...cookies, consent: true });
}

export async function removeCookies(leagueId: string): Promise<ActionResult> {
  return leagueCall(leagueId, "/credential", "DELETE");
}

export type InviteState = ActionResult & { link?: string };

export async function createInvite(leagueId: string, _prev: InviteState, _form: FormData): Promise<InviteState> {
  void _form;
  const blocked = await gate();
  if (blocked) return { ok: false, message: blocked };
  if (!isLeagueRef(leagueId)) return { ok: false, message: "Unknown league." };
  const res = await apiRequest<{ token: string }>(`/v2/leagues/${leagueId}/invites`, { method: "POST", body: {} });
  revalidatePath(`/leagues/${leagueId}`);
  if (!res.ok || !res.data) return { ok: false, message: res.ok ? "No invite was created." : res.error };
  // The token travels in the fragment: browsers never send it to a server, so it stays out of logs.
  return { ok: true, message: null, link: `/invite#${res.data.token}` };
}

export async function revokeInvite(leagueId: string, inviteId: string): Promise<ActionResult> {
  if (!/^[0-9a-f-]{36}$/.test(inviteId)) return { ok: false, message: "Unknown invite." };
  return leagueCall(leagueId, `/invites/${inviteId}`, "DELETE");
}

export async function deleteLeague(leagueId: string): Promise<ActionResult> {
  const result = await leagueCall(leagueId, "", "DELETE");
  if (result.ok) redirect("/leagues");
  return result;
}

export async function redeemInvite(token: string): Promise<ActionResult> {
  const blocked = await gate();
  if (blocked) return { ok: false, message: blocked };
  if (!/^[A-Za-z0-9_-]{16,64}$/.test(token)) return { ok: false, message: "This invite link is not complete." };
  const res = await apiRequest<{ league_id: string }>("/v2/invites/redeem", { body: { token } });
  if (!res.ok || !res.data) return { ok: false, message: res.ok ? "This invite is not valid." : res.error };
  redirect(`/l/${res.data.league_id}`);
}
```

`fantasy-intel/frontend/app/leagues/AddLeague.tsx`:

```tsx
"use client";

import Link from "next/link";
import { useActionState, useId } from "react";
import { Notice } from "@/components/ui";
import { leaguePath } from "@/lib/league-scope";
import { CONSENT_TEXT } from "@/lib/my-leagues";
import { addLeague, connectPrivate, type AddState } from "./actions";
import styles from "./leagues.module.css";

const IDLE: AddState = { status: "idle", message: null };

/** Step 1 tries the league as a public one; when ESPN says it is private, step 2 asks for the visitor's cookies. */
export function AddLeague({ defaultSeason }: { defaultSeason: number }) {
  const [state, action, pending] = useActionState(addLeague, IDLE);
  const id = useId();
  if (state.status === "needs_cookies" && state.league && state.season) {
    return <CookieForm league={state.league} season={state.season} intro={state.message} />;
  }
  return (
    <form action={action} className={styles.form}>
      <div className={styles.field}>
        <label htmlFor={`${id}-league`}>ESPN league id or address</label>
        <input id={`${id}-league`} name="league" type="text" autoComplete="off" autoCapitalize="off" spellCheck={false}
               required aria-describedby={`${id}-league-hint`} />
        <p id={`${id}-league-hint`} className={styles.hint}>
          Open the league on fantasy.espn.com and paste the address, or just the number after <code>leagueId=</code>.
        </p>
      </div>
      <div className={styles.field}>
        <label htmlFor={`${id}-season`}>Season</label>
        <input id={`${id}-season`} name="season" type="text" inputMode="numeric" autoComplete="off"
               defaultValue={defaultSeason} className={styles.season} />
      </div>
      <div className={styles.actions}>
        <button type="submit" disabled={pending}>{pending ? "Checking ESPN…" : "Add league"}</button>
      </div>
      <Outcome state={state} />
    </form>
  );
}

function CookieForm({ league, season, intro }: { league: string; season: number; intro: string | null }) {
  const [state, action, pending] = useActionState(connectPrivate, IDLE);
  const id = useId();
  if (state.status === "ok") return <Outcome state={state} />;
  const message = state.status === "idle" ? intro : state.message;
  return (
    <form action={action} className={styles.form}>
      {message ? <Notice tone={state.status === "idle" ? "info" : "negative"} title="A private league">{message}</Notice> : null}
      <input type="hidden" name="league" value={league} />
      <input type="hidden" name="season" value={season} />
      <details className={styles.details}>
        <summary>Where to find the cookies</summary>
        <ol className={styles.steps}>
          <li>Sign in to fantasy.espn.com in this browser.</li>
          <li>Open the browser&apos;s developer tools, then Application (Chrome) or Storage (Firefox, Safari), then Cookies for espn.com.</li>
          <li>Copy the values of <code>espn_s2</code> and <code>SWID</code>.</li>
        </ol>
      </details>
      <div className={styles.field}>
        <label htmlFor={`${id}-s2`}>espn_s2</label>
        <input id={`${id}-s2`} name="espn_s2" type="password" autoComplete="off" spellCheck={false} required />
      </div>
      <div className={styles.field}>
        <label htmlFor={`${id}-swid`}>SWID</label>
        <input id={`${id}-swid`} name="swid" type="password" autoComplete="off" spellCheck={false} required />
      </div>
      <label className={styles.consent}>
        <input type="checkbox" name="consent" required /> {CONSENT_TEXT}
      </label>
      <div className={styles.actions}>
        <button type="submit" disabled={pending}>{pending ? "Checking with ESPN…" : "Connect privately"}</button>
      </div>
    </form>
  );
}

function Outcome({ state }: { state: AddState }) {
  if (state.status === "error" && state.message) {
    return <Notice tone="negative" title="Could not add the league">{state.message}</Notice>;
  }
  if (state.status === "ok") {
    return (
      <p className={styles.ok} role="status">
        {state.message} {state.leagueId ? <Link href={leaguePath(state.leagueId, "/")} prefetch={false}>Open it</Link> : null}
      </p>
    );
  }
  return null;
}
```

`fantasy-intel/frontend/app/leagues/page.tsx`:

```tsx
import Link from "next/link";
import { connection } from "next/server";
import { Badge, EmptyState, Notice, PageHeader, Section } from "@/components/ui";
import { apiRequest } from "@/lib/api";
import { authConfig } from "@/lib/auth-config";
import { leaguePath } from "@/lib/league-scope";
import { dataAsOf } from "@/lib/league-switcher";
import { defaultSeason, leagueName, leagueState, multileagueUiEnabled, parseMyLeagues, type MyLeague } from "@/lib/my-leagues";
import { currentUser } from "@/lib/supabase/server";
import { AddLeague } from "./AddLeague";
import { LeagueActions } from "./LeagueActions";
import styles from "./leagues.module.css";

export const metadata = { title: "Leagues" };

export default async function LeaguesPage() {
  // The flag and the signed-in user are per request, so never prerender this page.
  await connection();
  const enabled = multileagueUiEnabled(process.env.MULTILEAGUE_UI_ENABLED);
  if (!enabled) return <SwitchedOff />;
  const user = authConfig(process.env) ? await currentUser() : null;
  return (
    <div className="stack">
      <PageHeader eyebrow="Connections" title="Leagues"
                  meta="Add an ESPN league to analyse it with every page of this site. Public leagues need no cookies; anyone can view them from any team's perspective." />
      {user ? <SignedIn /> : (
        <Notice tone="info" title="Sign in to add leagues">
          Viewing a public league needs no account: open its link. To add a league, save your team or connect a private
          league, <Link href="/login?next=/leagues" prefetch={false}>sign in</Link>.
        </Notice>
      )}
    </div>
  );
}

async function SignedIn() {
  const res = await apiRequest("/v2/leagues");
  const leagues = res.ok ? parseMyLeagues(res.data) : [];
  return (
    <>
      <Section title="Add an ESPN league" subtitle="Public leagues are read without cookies. A private league asks for yours next.">
        <AddLeague defaultSeason={defaultSeason(new Date())} />
      </Section>
      <Section title="Your leagues" subtitle="Leagues you added, pinned or were invited to.">
        {!res.ok ? (
          <Notice tone="negative" title="Your leagues could not be loaded">{res.error}</Notice>
        ) : leagues.length === 0 ? (
          <EmptyState title="No leagues yet">Add one above, or open a league link someone shared.</EmptyState>
        ) : (
          <ul className={styles.leagues}>
            {leagues.map((l) => <li key={l.id}><LeagueCard league={l} /></li>)}
          </ul>
        )}
      </Section>
    </>
  );
}

function LeagueCard({ league }: { league: MyLeague }) {
  const state = leagueState(league);
  const asOf = dataAsOf(league.committedAt, "UTC");
  const name = leagueName(league);
  const headingId = `league-${league.id}`;
  return (
    <article className={styles.league} aria-labelledby={headingId}>
      <div className={styles.cardHead}>
        <h3 id={headingId} className={styles.cardTitle}>
          <Link href={leaguePath(league.id, "/")} prefetch={false}>{name}</Link>
        </h3>
        <div className={styles.badges}>
          <Badge value={`Season ${league.season}`} tone="" />
          <Badge value={league.visibility === "private" ? "Private" : "Public"} tone="" />
          {league.role ? <Badge value={league.role === "admin" ? "Admin" : "Member"} tone="" /> : null}
        </div>
      </div>
      <dl className={styles.facts}>
        <div>
          <dt>Status</dt>
          <dd><Badge value={state.label} tone={state.tone === "neutral" ? "" : state.tone} /> {state.detail}</dd>
        </div>
        <div>
          <dt>Data</dt>
          <dd>{asOf ? `${asOf} UTC` : "Not synced yet"}</dd>
        </div>
      </dl>
      <div className={styles.manage}>
        <LeagueActions leagueId={league.id} pinned={league.pinned} member={league.role !== null} />
        <Link href={`/leagues/${league.id}`} prefetch={false}>Settings</Link>
      </div>
    </article>
  );
}

function SwitchedOff() {
  return (
    <div className="stack">
      <PageHeader eyebrow="Connections" title="Leagues"
                  meta="Which fantasy providers this dashboard can read, and how it gets its league today." />
      <Notice tone="info" title="Connecting leagues from the browser is switched off in this deployment">
        This page is a prototype. Nothing can be connected or listed here, so no connect controls are shown. The rest of the dashboard works as before.
      </Notice>
      <Section title="How this dashboard gets its league" subtitle="The one setup that works today">
        <ol className={styles.steps}>
          <li>The deployment&apos;s owner sets <code>ESPN_LEAGUE_ID</code> (and the season, <code>ESPN_SEASON</code>) in <code>fantasy-intel/.env</code>. The id is the number after <code>leagueId=</code> in the league&apos;s fantasy.espn.com address.</li>
          <li>A private ESPN league also needs the owner&apos;s <code>espn_s2</code> and <code>SWID</code> cookies in the same file, as <code>ESPN_S2</code> and <code>SWID</code>. They are read from the environment only.</li>
          <li>The sync scripts import the league and NFL usage data; every page then reads from that import.</li>
        </ol>
      </Section>
    </div>
  );
}
```

`fantasy-intel/frontend/app/leagues/[league]/page.tsx`:

```tsx
import Link from "next/link";
import { notFound } from "next/navigation";
import { connection } from "next/server";
import { Badge, Notice, PageHeader, Section } from "@/components/ui";
import { apiRequest } from "@/lib/api";
import { authConfig } from "@/lib/auth-config";
import { leaguePath } from "@/lib/league-scope";
import { isLeagueRef, leagueName, leagueState, multileagueUiEnabled, parseMyLeague } from "@/lib/my-leagues";
import { currentUser } from "@/lib/supabase/server";
import { LeagueActions } from "../LeagueActions";
import { CookieSettings, DeleteLeague, Invites, TeamForm } from "./forms";
import styles from "../leagues.module.css";

export const metadata = { title: "League settings" };

type Team = { team_id: number; name: string };

export default async function LeagueSettings({ params }: { params: Promise<{ league: string }> }) {
  await connection();
  const { league: id } = await params;
  if (!multileagueUiEnabled(process.env.MULTILEAGUE_UI_ENABLED) || !isLeagueRef(id)) notFound();
  const res = await apiRequest(`/v2/leagues/${id}`);
  const league = res.ok ? parseMyLeague(res.data) : null;
  if (!league) notFound();
  const user = authConfig(process.env) ? await currentUser() : null;
  const teamsRes = await apiRequest<{ teams: Team[] }>(`/v2/leagues/${id}/api/teams`);
  const teams = teamsRes.ok && teamsRes.data ? teamsRes.data.teams.map((t) => ({ id: String(t.team_id), name: t.name })) : [];
  const invites = league.role === "admin"
    ? await apiRequest<{ invites: { invite_id: string; expires_at: string }[] }>(`/v2/leagues/${id}/invites`) : null;
  const state = leagueState(league);
  return (
    <div className="stack">
      <PageHeader eyebrow="League settings" title={leagueName(league)}
                  meta={<Link href={leaguePath(id, "/")} prefetch={false}>Open the league</Link>} />
      <Section title="Sync">
        <p className={styles.plain}><Badge value={state.label} tone={state.tone === "neutral" ? "" : state.tone} /> {state.detail}</p>
        {user ? <LeagueActions leagueId={id} pinned={league.pinned} member={league.role !== null} /> : null}
      </Section>
      <Section title="Your team" subtitle="A preference only: every page analyses the league from this team. You can view any team.">
        {!user ? (
          <p className={styles.plain}><Link href={`/login?next=/leagues/${id}`} prefetch={false}>Sign in</Link> to save your team. Until then, pick a team on any page.</p>
        ) : teams.length === 0 ? (
          <p className={styles.plain}>The team list appears after the first sync.</p>
        ) : (
          <TeamForm leagueId={id} teams={teams} selected={league.teamId} />
        )}
      </Section>
      {user && league.visibility === "private" && league.role ? (
        <Section title="Your ESPN cookies" subtitle="Stored encrypted; the website itself cannot read them back. Only the sync job uses them, to read this league.">
          <CookieSettings leagueId={id} />
        </Section>
      ) : null}
      {invites ? (
        <Section title="Invites" subtitle="A link lets one person join as a viewer. It works once, for 7 days, and needs them to sign in.">
          {invites.ok ? <Invites leagueId={id} open={invites.data?.invites ?? []} /> : <Notice tone="negative" title="Invites could not be loaded">{invites.error}</Notice>}
        </Section>
      ) : null}
      {league.role === "admin" && league.visibility === "private" ? (
        <Section title="Delete this league" subtitle="Removes it for every member. Its data is deleted after 7 days.">
          <DeleteLeague leagueId={id} name={leagueName(league)} />
        </Section>
      ) : null}
    </div>
  );
}
```

`fantasy-intel/frontend/app/leagues/[league]/forms.tsx`:

```tsx
"use client";

import { useActionState, useId, useState, useTransition } from "react";
import { Notice } from "@/components/ui";
import { CONSENT_TEXT } from "@/lib/my-leagues";
import {
  createInvite, deleteLeague, removeCookies, replaceCookies, revokeInvite, saveTeam, type ActionResult, type InviteState,
} from "../actions";
import styles from "../leagues.module.css";

const IDLE: ActionResult = { ok: false, message: null };

function Result({ result, done }: { result: ActionResult; done?: string }) {
  if (result.ok && done) return <p className={styles.ok} role="status">{done}</p>;
  if (!result.ok && result.message) return <p className={styles.fieldError} role="status">{result.message}</p>;
  return null;
}

export function TeamForm({ leagueId, teams, selected }: { leagueId: string; teams: { id: string; name: string }[]; selected: string | null }) {
  const [state, action, pending] = useActionState(saveTeam.bind(null, leagueId), IDLE);
  const id = useId();
  return (
    <form action={action} className={styles.teamForm}>
      <div className={styles.field}>
        <label htmlFor={`${id}-team`}>Team</label>
        <select id={`${id}-team`} name="team" defaultValue={selected ?? ""}>
          <option value="">No team (neutral view)</option>
          {teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
        </select>
      </div>
      <button type="submit" disabled={pending}>{pending ? "Saving…" : "Save"}</button>
      <Result result={state} done="Saved." />
    </form>
  );
}

export function CookieSettings({ leagueId }: { leagueId: string }) {
  const [state, action, pending] = useActionState(replaceCookies.bind(null, leagueId), IDLE);
  const [removing, start] = useTransition();
  const [removed, setRemoved] = useState<ActionResult | null>(null);
  const id = useId();
  return (
    <div className={styles.form}>
      <form action={action} className={styles.form}>
        <p className={styles.plain}>Store or replace your own cookies, for example after signing out of ESPN everywhere.</p>
        <div className={styles.field}>
          <label htmlFor={`${id}-s2`}>espn_s2</label>
          <input id={`${id}-s2`} name="espn_s2" type="password" autoComplete="off" spellCheck={false} required />
        </div>
        <div className={styles.field}>
          <label htmlFor={`${id}-swid`}>SWID</label>
          <input id={`${id}-swid`} name="swid" type="password" autoComplete="off" spellCheck={false} required />
        </div>
        <label className={styles.consent}><input type="checkbox" name="consent" required /> {CONSENT_TEXT}</label>
        <div className={styles.actions}>
          <button type="submit" disabled={pending}>{pending ? "Checking with ESPN…" : "Store my cookies"}</button>
        </div>
        <Result result={state} done="Stored. The next sync uses them." />
      </form>
      <div className={styles.actions}>
        <button type="button" className={styles.danger} disabled={removing}
                onClick={() => start(async () => setRemoved(await removeCookies(leagueId)))}>Remove my cookies</button>
      </div>
      {removed ? <Result result={removed} done="Removed. Syncing pauses until someone stores cookies again." /> : null}
    </div>
  );
}

const NO_INVITE: InviteState = { ok: false, message: null };

export function Invites({ leagueId, open }: { leagueId: string; open: { invite_id: string; expires_at: string }[] }) {
  const [state, action, pending] = useActionState(createInvite.bind(null, leagueId), NO_INVITE);
  const [busy, start] = useTransition();
  const [revoked, setRevoked] = useState<ActionResult | null>(null);
  const link = state.ok && state.link && typeof window !== "undefined" ? `${window.location.origin}${state.link}` : state.link;
  return (
    <div className={styles.form}>
      <form action={action} className={styles.actions}>
        <button type="submit" disabled={pending}>{pending ? "Creating…" : "Create an invite link"}</button>
      </form>
      {state.ok && link ? (
        <Notice tone="info" title="Copy this link now: it is shown only once">
          <p className={styles.inviteLink}>{link}</p>
        </Notice>
      ) : <Result result={state} />}
      {open.length > 0 ? (
        <ul className={styles.inviteList}>
          {open.map((i) => (
            <li key={i.invite_id}>
              <span>Open invite, expires {new Date(i.expires_at).toISOString().slice(0, 10)}</span>
              <button type="button" className={styles.secondary} disabled={busy}
                      onClick={() => start(async () => setRevoked(await revokeInvite(leagueId, i.invite_id)))}>Revoke</button>
            </li>
          ))}
        </ul>
      ) : <p className={styles.plain}>No open invites.</p>}
      {revoked ? <Result result={revoked} done="Revoked." /> : null}
    </div>
  );
}

export function DeleteLeague({ leagueId, name }: { leagueId: string; name: string }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);
  if (!confirming) {
    return <button type="button" className={styles.danger} onClick={() => setConfirming(true)}>Delete league…</button>;
  }
  return (
    <div className={styles.confirm}>
      <p className={styles.confirmText}>Delete {name} for every member? This cannot be undone after 7 days.</p>
      <div className={styles.actions}>
        <button type="button" className={styles.danger} disabled={busy}
                onClick={() => start(async () => setResult(await deleteLeague(leagueId)))}>Delete</button>
        <button type="button" className={styles.secondary} onClick={() => setConfirming(false)}>Cancel</button>
      </div>
      {result ? <Result result={result} /> : null}
    </div>
  );
}
```

`fantasy-intel/frontend/app/invite/RedeemInvite.tsx`:

```tsx
"use client";

import { useState, useSyncExternalStore, useTransition } from "react";
import { Notice } from "@/components/ui";
import { redeemInvite, type ActionResult } from "../leagues/actions";

// The token is in the URL fragment (/invite#<token>): browsers never send a fragment to a server, so the token stays
// out of request logs until this page posts it once.
const noop = () => () => {};
const readHash = () => window.location.hash.replace(/^#/, "");

export function RedeemInvite() {
  const token = useSyncExternalStore(noop, readHash, () => "");
  const [busy, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);
  if (!token) {
    return <Notice tone="negative" title="This invite link is not complete">Copy the whole link from the person who invited you.</Notice>;
  }
  return (
    <div className="stack">
      <p>You were invited to a private league as a viewer.</p>
      <div>
        <button type="button" disabled={busy} onClick={() => start(async () => setResult(await redeemInvite(token)))}>
          {busy ? "Joining…" : "Join the league"}
        </button>
      </div>
      {result && !result.ok ? <Notice tone="negative" title="Could not join">{result.message}</Notice> : null}
    </div>
  );
}
```

- [ ] **Step 4: Run them and see them pass**

```bash
cd fantasy-intel/frontend && npm test
```
Expected: all pass; in the browser with the flag on, signed-out /leagues, /login and /invite render with no console errors.

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/frontend/lib/my-leagues.ts fantasy-intel/frontend/lib/my-leagues.test.ts fantasy-intel/frontend/app/leagues/AddLeague.tsx fantasy-intel/frontend/app/leagues/LeagueActions.tsx fantasy-intel/frontend/app/leagues/[league]/page.tsx fantasy-intel/frontend/app/leagues/[league]/forms.tsx fantasy-intel/frontend/app/invite/page.tsx fantasy-intel/frontend/app/invite/RedeemInvite.tsx fantasy-intel/frontend/app/leagues/page.tsx fantasy-intel/frontend/app/leagues/actions.ts fantasy-intel/frontend/app/leagues/leagues.module.css
git commit -m "Phase 3 tasks 12-13: sign-in (Supabase email link) and the connect / settings / invite pages"
```


### Task 14: Los Subies through the job path

**Files:**
- Modify: `fantasy-intel/scripts/golden_check.py`

**Interfaces:**
- Produces: scripts/golden_check.py --job-path [--paths]: one ESPN fetch applied the laptop way (legacy) and the job way (global published first, then app/sync/build.py, fenced publish), every legacy GET case compared; --paths prints differing key paths, never values.

- [ ] **Step 1: There is no unit test: this is a local check on real Los Subies (never committed data).**


- [ ] **Step 3: Implement**

`fantasy-intel/scripts/golden_check.py`:

```python
"""Local check that a real league answers identically through the legacy path and the per-league path (Phase 2).

    cd backend && uv run python ../scripts/golden_check.py [path/to/fantasy.duckdb] [--job-path]

Copies the database (default: FANTASY_DB_PATH), starts a throwaway Postgres with the control-plane migrations,
publishes the copy as a private league into a temporary object directory, makes a test user a member whose saved
team is the database's own team (`is_mine`), then requests every legacy GET case both ways and compares them with
the same masking as the committed goldens. Prints counts and the names of differing cases, never response bodies
(the data is private). Needs `initdb`/`pg_ctl` (brew install postgresql). Exit 0 when every case matches.

--job-path (Phase 3): the per-league side is built the way a sync job builds it. The league is fetched from ESPN once
(with this laptop's .env cookies), then the same payloads are applied twice: the laptop way (write + analytics into
the database copy, served by the legacy routes) and the job way (app/sync/build.py on the published global file and
the previous league file, member SWIDs scrubbed, published with a fence and served by /v2).
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401

sys.path.insert(0, str(_bootstrap.BACKEND / "tests"))


def job_build(pg, pub, db: Path, tmp: Path, settings, season: int) -> None:
    """Refresh `db` the laptop way and publish a job-built version of the same league from the same payloads."""
    import copy

    import psycopg

    from app.analytics.pipeline import run_analytics
    from app.db.connection import open_db, utcnow
    from app.db.league_publish import GLOBAL_STAMP, DirWriter, commit_league, publish_global
    from app.db.league_split import split_database
    from app.espn.sync import fetch_all, write_synced
    from app.sync import espn_source
    from app.sync.build import build_league_file
    cred = {"espn_s2": settings.espn_s2, "swid": settings.swid} if settings.has_espn_credentials else None
    with espn_source.espn_client(settings.league_id, season, cred) as client:
        payloads = fetch_all(client)
    with open_db(db) as con:
        write_synced(con, copy.deepcopy(payloads), swid=settings.swid, my_team_id=settings.my_team_id)
        run_analytics(con, season)
    objects = tmp / "objects"
    work = tmp / "job"
    work.mkdir()
    stamp = utcnow().isoformat(timespec="seconds")
    with psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None) as con:
        # The laptop's refresh also recomputed global tables: publish them first, as refresh_all does, so the job
        # builds against (and readers attach) the same global file the legacy side reads.
        split = split_database(db, work / "laptop-league.duckdb", work / "laptop-global.duckdb", stamp=stamp,
                               global_stamp=GLOBAL_STAMP)
        global_version = publish_global(split.global_path, split.global_version, DirWriter(objects), con)
        built = build_league_file(work, global_path=objects / "global" / f"{global_version}.duckdb",
                                  previous=objects / "leagues" / pub.league_id / f"{pub.data_version}.duckdb",
                                  payloads=payloads, owner_swid=settings.swid, stamp=stamp)
        fence = con.execute("UPDATE fi.leagues SET sync_fence = sync_fence + 1 WHERE id = %s RETURNING sync_fence",
                            [pub.league_id]).fetchone()[0]
        con.commit()
        applied, _ = commit_league(DirWriter(objects), con, pub.league_id, built.league_path, built.league_version,
                                   global_version, fence)
    if not applied:
        raise SystemExit("the job-built version was not published")
    print("per-league side: built by the sync job path")


def _differing_paths(a: str, b: str) -> list[str]:
    """JSON key paths whose values differ (paths only: never the private values)."""
    import json

    def load(text):
        return json.loads(text.split("\n", 1)[1] if text.startswith("//") else text)

    out = []

    def walk(x, y, path):
        if type(x) is not type(y):
            out.append(path or "$")
        elif isinstance(x, dict):
            for k in sorted(set(x) | set(y)):
                walk(x.get(k), y.get(k), f"{path}.{k}")
        elif isinstance(x, list):
            if len(x) != len(y):
                out.append(f"{path}[len]")
            for i, (u, v) in enumerate(zip(x, y)):
                walk(u, v, f"{path}[{i}]" if i < 1 else f"{path}[*]")
        elif x != y:
            out.append(path or "$")
    try:
        walk(load(a), load(b), "")
    except ValueError:
        return ["(not JSON)"]
    return sorted(set(out))


def main() -> int:
    from app.config import get_settings
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    job_path = "--job-path" in sys.argv[1:]
    settings = get_settings()
    src = Path(args[0]) if args else settings.db_path
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
            if job_path:
                job_build(pg, pub, db, tmp, settings, season)
            with pg.connect() as c:
                seed_user(c, U1, admin=True)       # the owner-only research routes need a platform admin
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

            differ = []
            for c in cs:
                legacy, scoped = response_text(client, c), response_text(Scoped(), c)
                if legacy != scoped:
                    differ.append(c.name)
                    if "--paths" in sys.argv[1:]:
                        print(f"  {c.name}: {', '.join(_differing_paths(legacy, scoped)[:8])}")
        finally:
            cluster.stop()
    print(f"{len(cs)} cases compared; {len(cs) - len(differ)} identical; {len(differ)} differ")
    for name in differ:
        print(f"  differs: {name}")
    return 1 if differ else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run them and see them pass**

```bash
cd fantasy-intel/backend && uv run python ../scripts/golden_check.py ../data/fantasy.duckdb --job-path
```
Expected: 52 cases compared; 52 identical; 0 differ (2026-10-20, after the fixes it found: runtime-created tables, roster order, perspective precedence).

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/scripts/golden_check.py
git commit -m "golden_check --job-path"
```

## Acceptance (master plan, Phase 3)

- Sync tests: reclaim in place, heartbeat loss aborts, a stale fence publishes nothing, a new league's first publish becomes visible, racing first publishes keep the higher fence, the slot cap holds under 10 concurrent claimers, duplicate enqueue collapses, a failure before the swap leaves the pointer unchanged, an ambiguous 403/404 never invalidates.
- Golden tests still green with Los Subies produced by the job path.
- Public-log audit: a full drain run's log contains no team names, player names, cookies or league ids beyond the opaque `lg_` id (automated in the workflow and in tests).
- End to end: connect a public ESPN league → data visible once the dispatched run finishes (target < 5 min); flip it private on ESPN → access 404 within one probe; private connect → invite → viewer access.

## Turning it on (production)

Each step is reversible. With a step undone, the flags it gates stay off.

1. **Supabase.**
   - Apply `20261021120000_sync_queue.sql` and `20261021130000_connect_flows.sql`.
   - Check the security advisors.
   - Havyn gives `fi_api` and `fi_sync` their LOGIN passwords (`supabase/README.md`).
   - In Auth, set the Site URL and add `<site>/auth/callback` (and `http://localhost:3000/auth/callback`) to the redirect URLs.
   - Havyn creates their own user under Authentication, then Users. Sign-ups stay closed until Phase 6.
2. **GitHub.**
   - Secrets: `FI_SYNC_DB_URL`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (Object Read & Write on `fantasy-intel`) and `CREDENTIALS_PRIVKEY_V1`.
   - Repository variable: `SYNC_DRAIN_ENABLED=true`.
3. **Vercel API (`fantasy-intel-api`).**
   - `SUPABASE_URL`, `SUPABASE_DB_URL` (fi_api) and `CREDENTIALS_PUBKEY_V1`.
   - `GITHUB_DISPATCH_TOKEN`: fine-grained, this repository, Actions: write only.
   - Then `MULTILEAGUE_ENABLED=1` and `MULTILEAGUE_CONNECT_ENABLED=1`.
4. **Vercel web.**
   - `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` (the publishable key; never the secret or service-role key).
   - `SITE_URL`, then `MULTILEAGUE_UI_ENABLED=1`.
5. **Los Subies.**
   - Havyn signs in once, and the runbook's admin and perspective rows are applied (`supabase/runbook-los-subies.sql`).
   - Havyn stores their cookies on `/leagues/lg_KxH_Y1Y56aM7qnCCseKvs1sU`, which queues a manual sync.
6. **The laptop.**
   - `LEAGUE_PUBLISH_ID=lg_KxH_Y1Y56aM7qnCCseKvs1sU` and `FI_SYNC_DB_URL` in `.env`, so `refresh_all` keeps publishing the global file and Los Subies' research tables.

## Rollback

- Unset `SYNC_DRAIN_ENABLED` (or disable the workflow) and `MULTILEAGUE_CONNECT_ENABLED`. League files keep their last version, and the laptop keeps publishing Los Subies as in Phase 2.
- Down-migrations restore the Phase 2 schema.
