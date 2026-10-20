# Supabase control plane

Postgres schema `fi` in the Supabase project `fantasy-intel` (ref `kdikrcmvfuzqsgahqaxf`, us-east-1). It holds the
users, leagues, memberships, pins, perspectives, invites, encrypted credentials, the sync queue and the snapshot
pointers. Design: [ADR-0002](../../docs/multileague/ADR-0002-supabase-control-plane.md).

| Path | What |
|---|---|
| `migrations/20261020120000_control_plane.sql` | Schema `fi`, roles `fi_api` / `fi_sync`, tables, grants, RLS |
| `migrations/20261020120100_control_plane_functions.sql` | SECURITY DEFINER functions, append-only audit trigger |
| `migrations/20261020120200_audit_trigger_search_path.sql` | Pins `search_path` on the audit trigger (security advisor) |
| `down/*.sql` | Down-migrations, applied in reverse order to roll Phase 1 back |

Every migration is idempotent (`IF NOT EXISTS`, `CREATE OR REPLACE`, `DROP POLICY IF EXISTS`).

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
