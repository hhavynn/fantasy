# Supabase control plane

Postgres schema `fi` in the Supabase project `fantasy-intel` (ref `kdikrcmvfuzqsgahqaxf`, us-east-1). It holds the
users, leagues, memberships, pins, perspectives, invites, encrypted credentials, the sync queue and the snapshot
pointers. Design: [ADR-0002](../../docs/multileague/ADR-0002-supabase-control-plane.md).

| Path | What |
|---|---|
| `migrations/20261020120000_control_plane.sql` | Schema `fi`, roles `fi_api` / `fi_sync`, tables, grants, RLS |
| `migrations/20261020120100_control_plane_functions.sql` | SECURITY DEFINER functions, append-only audit trigger |
| `migrations/20261020120200_audit_trigger_search_path.sql` | Pins `search_path` on the audit trigger (security advisor) |
| `migrations/20261020120300_preference_policies_recheck_visibility.sql` | Pins and perspectives are reachable only while the league is visible, for every command (DELETE has no WITH CHECK) |
| `migrations/20261020120400_account_cascade_and_credential_marking.sql` | `fi.app_users` cascades from `auth.users` (account deletion crypto-erases credentials); `mark_credential` is bound to the fetched credential and holds the job lease |
| `migrations/20261020120500_account_deletion_rules.sql` | Account deletion: private leagues whose credential it owned go stale; the last admin's seat passes to the longest-standing member, or a memberless league is soft-deleted; `fi.current_account_exists()` |
| `migrations/20261020120600_credential_recovery_and_deletion_serialization.sql` | A validated replacement credential revives a stale league; concurrent account deletions serialize per league |
| `migrations/20261020120700_redeem_invite_locks_league.sql` | Invite redemption locks and re-checks the league, so it cannot join a league an account deletion is removing |
| `migrations/20261020120800_credential_lock_order.sql` | One lock order (league, then credential) for every credential path, so account deletion cannot deadlock with a sync or revoke |
| `migrations/20261020120900_redeem_invite_lock_order.sql` | Invite redemption locks the league before the invite (same order as account deletion) and re-validates under lock |
| `migrations/20261020121000_owner_lock_order.sql` | `ensure_app_user` locks the account first; `store_credential` secures its owner before the league |
| `runbook-los-subies.sql` | One-time registration of Los Subies (private, no members yet) |
| `down/*.sql` | Down-migrations, applied in reverse order to roll Phase 1 back; each restores the previous definitions |

Each migration runs once, in filename order. Re-running the whole chain in order is safe (every statement is `IF NOT EXISTS`, `CREATE OR REPLACE` or `DROP POLICY IF EXISTS`, and `backend/tests/test_pg_harness.py` checks it), but re-running one older migration on its own is not: later migrations redefine some of its functions and policies, and the older definitions would come back. To repair a function or policy, re-run the latest migration that defines it, or the whole chain.

**Lock order.** Every function takes row locks in the order account deletion does: the account (`auth.users`),
then the app user (`fi.app_users`), then the league (`fi.leagues`), then the league's own rows (credentials,
memberships, invites, pins, perspectives). A new function that breaks this order can deadlock with account
deletion; `backend/tests/test_control_plane_functions.py` checks the order in each installed definition.

## Applying migrations

Use either of these:

- From a Claude session with the Supabase connector: `apply_migration` with the file's contents. Name it after the
  file, without the timestamp.
- With the Supabase CLI:

```bash
supabase link --project-ref kdikrcmvfuzqsgahqaxf
```

```bash
supabase db push
```

Afterwards, check the security advisors. Two INFO notes are expected and intended: `fi.audit_log` and `fi.league_credentials` have RLS on with no policies, which denies every request role. No WARN should remain.

## One-time: give the service roles a password

The migration creates `fi_api` and `fi_sync` as `NOLOGIN`. A password never goes in a migration or in git. In the
Supabase SQL editor, as `postgres`, run the following with two different random values of 32+ characters:

```sql
ALTER ROLE fi_api  WITH LOGIN PASSWORD '<random>';
ALTER ROLE fi_sync WITH LOGIN PASSWORD '<another random>';
```

The API's `SUPABASE_DB_URL` (Vercel project `fantasy-intel-api`) is the **transaction pooler** connection string
from the dashboard's Connect panel. Use user `fi_api.kdikrcmvfuzqsgahqaxf`, port 6543 and `sslmode=require`. The
code turns prepared statements off, as transaction mode requires. The sync workers' URL (Phase 3) uses `fi_sync`
the same way and lives only in GitHub Actions secrets.

Never deploy the service-role key, and never give a role `BYPASSRLS`.

## Tests

`backend/tests/pg_harness.py` applies `backend/tests/pg_shim.sql` and these migrations to a throwaway Postgres.
The shim stands in for Supabase's `anon` / `authenticated` roles and the `auth.uid()` / `auth.jwt()` helpers. The
throwaway Postgres is either `FI_TEST_PG_DSN` (CI: a `postgres:17` service) or a local `initdb` cluster in a temp
dir, which needs `brew install postgresql`.
