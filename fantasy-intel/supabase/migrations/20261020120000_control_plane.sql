-- Multi-league control plane (Phase 1). Schema `fi` is deliberately not `public`: Supabase's PostgREST serves
-- `public` to anyone holding the anon key, and nothing here may be reachable that way. Every grant is explicit.
-- Idempotent: safe to re-run. Down-migration: supabase/down/20261020120000_control_plane.sql.
-- Design: docs/superpowers/specs/2026-10-19-multi-league-platform-design.md §2–3, docs/multileague/ADR-0002.

CREATE SCHEMA IF NOT EXISTS fi;
REVOKE ALL ON SCHEMA fi FROM PUBLIC;

-- ---- roles -------------------------------------------------------------------------------------------
-- fi_api: the request path. NOINHERIT member of the two Supabase request roles; every request transaction does
-- SET LOCAL ROLE anon|authenticated, so RLS and auth.uid() apply. No grants of its own on fi.* tables.
-- fi_sync: GitHub Actions workers. Sync tables only; credentials only via fi.sync_credential (next migration).
-- Both are created NOLOGIN: the project owner sets LOGIN + a password out of band (never in a migration).
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fi_api') THEN
    CREATE ROLE fi_api NOLOGIN NOINHERIT NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fi_sync') THEN
    CREATE ROLE fi_sync NOLOGIN NOINHERIT NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END $$;
GRANT anon, authenticated TO fi_api;
GRANT USAGE ON SCHEMA fi TO anon, authenticated, fi_api, fi_sync;

-- ---- tables ------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fi.app_users (
  id                uuid PRIMARY KEY,                 -- auth.users.id = the JWT `sub`
  is_platform_admin boolean NOT NULL DEFAULT false,
  created_at        timestamptz NOT NULL DEFAULT now(),
  last_seen_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fi.leagues (
  id                    text PRIMARY KEY CHECK (id ~ '^lg_[A-Za-z0-9_-]{16,64}$'),
  provider              text NOT NULL CHECK (provider IN ('espn', 'sleeper')),
  external_id           text NOT NULL CHECK (external_id ~ '^[0-9]{1,64}$'),
  season                integer NOT NULL CHECK (season BETWEEN 1990 AND 2100),
  name                  text CHECK (name IS NULL OR length(name) <= 200),
  visibility            text NOT NULL CHECK (visibility IN ('public', 'private')),
  visibility_checked_at timestamptz,
  visibility_epoch      bigint NOT NULL DEFAULT 0,
  status                text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'stale', 'unavailable', 'dormant')),
  sync_fence            bigint NOT NULL DEFAULT 0,
  created_at            timestamptz NOT NULL DEFAULT now(),
  last_viewed_at        timestamptz,
  deleted_at            timestamptz,
  UNIQUE (provider, external_id, season)
);

CREATE TABLE IF NOT EXISTS fi.league_memberships (
  user_id    uuid NOT NULL REFERENCES fi.app_users(id) ON DELETE CASCADE,
  league_id  text NOT NULL REFERENCES fi.leagues(id) ON DELETE CASCADE,
  role       text NOT NULL CHECK (role IN ('admin', 'viewer')),
  created_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  PRIMARY KEY (user_id, league_id)
);
CREATE INDEX IF NOT EXISTS league_memberships_league ON fi.league_memberships (league_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS fi.league_pins (
  user_id    uuid NOT NULL REFERENCES fi.app_users(id) ON DELETE CASCADE,
  league_id  text NOT NULL REFERENCES fi.leagues(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, league_id)
);

CREATE TABLE IF NOT EXISTS fi.team_selections (
  user_id          uuid NOT NULL REFERENCES fi.app_users(id) ON DELETE CASCADE,
  league_id        text NOT NULL REFERENCES fi.leagues(id) ON DELETE CASCADE,
  team_external_id text NOT NULL CHECK (team_external_id ~ '^[A-Za-z0-9._:-]{1,64}$'),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, league_id)
);

CREATE TABLE IF NOT EXISTS fi.league_invites (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  league_id   text NOT NULL REFERENCES fi.leagues(id) ON DELETE CASCADE,
  token_hash  bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),   -- sha256(token); the token is shown once
  created_by  uuid NOT NULL REFERENCES fi.app_users(id) ON DELETE CASCADE,
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,
  redeemed_by uuid REFERENCES fi.app_users(id) ON DELETE SET NULL,
  redeemed_at timestamptz,
  revoked_at  timestamptz
);

CREATE TABLE IF NOT EXISTS fi.league_credentials (
  id                uuid PRIMARY KEY,
  league_id         text NOT NULL REFERENCES fi.leagues(id) ON DELETE CASCADE,
  owner_user_id     uuid NOT NULL REFERENCES fi.app_users(id) ON DELETE CASCADE,   -- account deletion crypto-erases
  status            text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspect', 'invalid', 'revoked')),
  ciphertext        bytea NOT NULL,
  nonce             bytea NOT NULL CHECK (octet_length(nonce) = 12),
  aad_format        text NOT NULL DEFAULT 'fi-cred-v1' CHECK (aad_format = 'fi-cred-v1'),
  wrapped_data_key  bytea NOT NULL,
  wrap_key_version  text NOT NULL CHECK (wrap_key_version ~ '^v[0-9]{1,4}$'),
  failure_count     integer NOT NULL DEFAULT 0,
  first_failed_at   timestamptz,
  last_validated_at timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  revoked_at        timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS league_credentials_one_live ON fi.league_credentials (league_id)
  WHERE status IN ('active', 'suspect');

CREATE TABLE IF NOT EXISTS fi.sync_jobs (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  league_id        text NOT NULL REFERENCES fi.leagues(id) ON DELETE CASCADE,
  kind             text NOT NULL CHECK (kind IN ('initial', 'scheduled', 'manual', 'probe', 'recompute', 'rewrap')),
  status           text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'dead')),
  attempt          integer NOT NULL DEFAULT 0,
  max_attempts     integer NOT NULL DEFAULT 5,
  run_after        timestamptz NOT NULL DEFAULT now(),
  lease_owner      text,
  lease_token      bigint,
  lease_expires_at timestamptz,
  heartbeat_at     timestamptz,
  provider_slot    integer,
  idempotency_key  text,
  error_code       text,
  error_detail     text,                                 -- redacted before it is written; never shown to request roles
  global_version   text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  started_at       timestamptz,
  finished_at      timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS sync_jobs_one_running_per_league ON fi.sync_jobs (league_id) WHERE status = 'running';
CREATE UNIQUE INDEX IF NOT EXISTS sync_jobs_idempotency ON fi.sync_jobs (idempotency_key)
  WHERE idempotency_key IS NOT NULL AND status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS sync_jobs_due ON fi.sync_jobs (run_after) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS sync_jobs_by_league ON fi.sync_jobs (league_id, created_at DESC);

CREATE TABLE IF NOT EXISTS fi.provider_slots (
  provider         text NOT NULL CHECK (provider IN ('espn', 'sleeper')),
  slot_no          integer NOT NULL,
  job_id           uuid REFERENCES fi.sync_jobs(id) ON DELETE SET NULL,
  lease_expires_at timestamptz,
  PRIMARY KEY (provider, slot_no)
);
CREATE OR REPLACE FUNCTION fi.seed_provider_slots() RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
  INSERT INTO fi.provider_slots (provider, slot_no)
  SELECT 'espn', g FROM generate_series(1, 2) g UNION ALL SELECT 'sleeper', g FROM generate_series(1, 4) g
  ON CONFLICT DO NOTHING
$$;
SELECT fi.seed_provider_slots();

CREATE TABLE IF NOT EXISTS fi.league_snapshots (
  league_id      text PRIMARY KEY REFERENCES fi.leagues(id) ON DELETE CASCADE,
  object_key     text NOT NULL,
  data_version   text NOT NULL CHECK (data_version ~ '^[A-Za-z0-9._-]{1,64}$'),
  fence          bigint NOT NULL,
  global_version text NOT NULL CHECK (global_version ~ '^[A-Za-z0-9._-]{1,64}$'),
  committed_at   timestamptz NOT NULL DEFAULT now(),
  -- The object key is derived from the league id and version only: never from anything a user typed.
  CONSTRAINT league_snapshots_key_matches_league CHECK (object_key = 'leagues/' || league_id || '/' || data_version || '.duckdb')
);

CREATE TABLE IF NOT EXISTS fi.league_snapshot_history (
  id             bigserial PRIMARY KEY,
  league_id      text NOT NULL REFERENCES fi.leagues(id) ON DELETE CASCADE,
  object_key     text NOT NULL,
  data_version   text NOT NULL,
  fence          bigint NOT NULL,
  global_version text NOT NULL,
  committed_at   timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS league_snapshot_history_league ON fi.league_snapshot_history (league_id, fence DESC);

CREATE TABLE IF NOT EXISTS fi.audit_log (
  id            bigserial PRIMARY KEY,
  at            timestamptz NOT NULL DEFAULT now(),
  actor_user_id uuid,
  action        text NOT NULL,
  target_type   text NOT NULL,
  target_id     text NOT NULL,
  meta          jsonb NOT NULL DEFAULT '{}'::jsonb        -- metadata only: never league data, names or secrets
);

-- ---- helper predicates (SECURITY DEFINER so anon never needs a grant on memberships) --------------------
CREATE OR REPLACE FUNCTION fi.is_member(p_league_id text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
  SELECT EXISTS (SELECT 1 FROM fi.league_memberships m
                 WHERE m.league_id = p_league_id AND m.user_id = auth.uid() AND m.revoked_at IS NULL)
$$;
CREATE OR REPLACE FUNCTION fi.is_admin(p_league_id text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
  SELECT EXISTS (SELECT 1 FROM fi.league_memberships m
                 WHERE m.league_id = p_league_id AND m.user_id = auth.uid() AND m.role = 'admin' AND m.revoked_at IS NULL)
$$;
REVOKE ALL ON FUNCTION fi.is_member(text), fi.is_admin(text), fi.seed_provider_slots() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.is_member(text), fi.is_admin(text) TO anon, authenticated;

-- ---- grants (column lists where a column must stay unreadable or unwritable) -----------------------------
REVOKE ALL ON ALL TABLES IN SCHEMA fi FROM PUBLIC, anon, authenticated, fi_api, fi_sync;
GRANT SELECT ON fi.leagues, fi.league_snapshots TO anon, authenticated;
GRANT UPDATE (last_viewed_at) ON fi.leagues TO anon, authenticated;
GRANT SELECT (id, league_id, kind, status, attempt, run_after, error_code, created_at, started_at, finished_at)
  ON fi.sync_jobs TO anon, authenticated;
GRANT SELECT, INSERT ON fi.app_users TO authenticated;
GRANT UPDATE (last_seen_at) ON fi.app_users TO authenticated;
GRANT SELECT ON fi.league_memberships TO authenticated;
GRANT SELECT, INSERT, DELETE ON fi.league_pins TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON fi.team_selections TO authenticated;
GRANT SELECT (id, league_id, created_by, created_at, expires_at, redeemed_at, revoked_at) ON fi.league_invites TO authenticated;
GRANT SELECT ON fi.leagues TO fi_sync;
GRANT UPDATE (visibility, visibility_checked_at, visibility_epoch, status, sync_fence) ON fi.leagues TO fi_sync;
GRANT SELECT, INSERT, UPDATE, DELETE ON fi.sync_jobs, fi.provider_slots TO fi_sync;
GRANT SELECT, INSERT, UPDATE ON fi.league_snapshots TO fi_sync;
GRANT SELECT, INSERT, DELETE ON fi.league_snapshot_history TO fi_sync;
GRANT USAGE, SELECT ON SEQUENCE fi.league_snapshot_history_id_seq TO fi_sync;

-- ---- row-level security --------------------------------------------------------------------------------
ALTER TABLE fi.app_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.leagues ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.league_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.league_pins ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.team_selections ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.league_invites ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.league_credentials ENABLE ROW LEVEL SECURITY;    -- no policies: nobody but the owner role reads it
ALTER TABLE fi.sync_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.provider_slots ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.league_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.league_snapshot_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.audit_log ENABLE ROW LEVEL SECURITY;             -- no policies: written by SECURITY DEFINER functions only

DROP POLICY IF EXISTS league_read ON fi.leagues;
CREATE POLICY league_read ON fi.leagues FOR SELECT TO anon, authenticated
  USING (deleted_at IS NULL AND (visibility = 'public' OR fi.is_member(id)));
DROP POLICY IF EXISTS league_touch ON fi.leagues;
CREATE POLICY league_touch ON fi.leagues FOR UPDATE TO anon, authenticated
  USING (deleted_at IS NULL AND (visibility = 'public' OR fi.is_member(id)))
  WITH CHECK (deleted_at IS NULL AND (visibility = 'public' OR fi.is_member(id)));
DROP POLICY IF EXISTS league_sync ON fi.leagues;
CREATE POLICY league_sync ON fi.leagues FOR ALL TO fi_sync USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS app_users_self_read ON fi.app_users;
CREATE POLICY app_users_self_read ON fi.app_users FOR SELECT TO authenticated USING (id = auth.uid());
DROP POLICY IF EXISTS app_users_self_insert ON fi.app_users;
CREATE POLICY app_users_self_insert ON fi.app_users FOR INSERT TO authenticated
  WITH CHECK (id = auth.uid() AND is_platform_admin = false);
DROP POLICY IF EXISTS app_users_self_update ON fi.app_users;
CREATE POLICY app_users_self_update ON fi.app_users FOR UPDATE TO authenticated USING (id = auth.uid()) WITH CHECK (id = auth.uid());

DROP POLICY IF EXISTS memberships_own ON fi.league_memberships;
CREATE POLICY memberships_own ON fi.league_memberships FOR SELECT TO authenticated USING (user_id = auth.uid());

DROP POLICY IF EXISTS pins_own ON fi.league_pins;
CREATE POLICY pins_own ON fi.league_pins FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid() AND EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = league_id));

DROP POLICY IF EXISTS selections_own ON fi.team_selections;
CREATE POLICY selections_own ON fi.team_selections FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid() AND EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = league_id));

DROP POLICY IF EXISTS invites_admin_read ON fi.league_invites;
CREATE POLICY invites_admin_read ON fi.league_invites FOR SELECT TO authenticated USING (fi.is_admin(league_id));

DROP POLICY IF EXISTS snapshots_visible ON fi.league_snapshots;
CREATE POLICY snapshots_visible ON fi.league_snapshots FOR SELECT TO anon, authenticated
  USING (EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = league_id));
DROP POLICY IF EXISTS snapshots_sync ON fi.league_snapshots;
CREATE POLICY snapshots_sync ON fi.league_snapshots FOR ALL TO fi_sync USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS snapshot_history_sync ON fi.league_snapshot_history;
CREATE POLICY snapshot_history_sync ON fi.league_snapshot_history FOR ALL TO fi_sync USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS jobs_visible ON fi.sync_jobs;
CREATE POLICY jobs_visible ON fi.sync_jobs FOR SELECT TO anon, authenticated
  USING (EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = league_id));
DROP POLICY IF EXISTS jobs_sync ON fi.sync_jobs;
CREATE POLICY jobs_sync ON fi.sync_jobs FOR ALL TO fi_sync USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS slots_sync ON fi.provider_slots;
CREATE POLICY slots_sync ON fi.provider_slots FOR ALL TO fi_sync USING (true) WITH CHECK (true);
