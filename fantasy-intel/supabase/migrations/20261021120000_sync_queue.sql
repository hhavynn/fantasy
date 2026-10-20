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

-- A provider that answered 429 is paused for every worker until this time (claims skip its slots).
ALTER TABLE fi.provider_slots ADD COLUMN IF NOT EXISTS paused_until timestamptz;

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
  -- ... and wakes up when someone views or pins it again. A league whose objects were purged has no version left:
  -- its first sync is queued here (scheduled refreshes only follow a published version).
  WITH woken AS (
    UPDATE fi.leagues l SET status = 'active', objects_purged_at = NULL
    WHERE l.deleted_at IS NULL AND l.status = 'dormant'
      AND (l.last_viewed_at > l.status_changed_at
           OR EXISTS (SELECT 1 FROM fi.league_pins p WHERE p.league_id = l.id AND p.created_at > l.status_changed_at))
    RETURNING l.id
  )
  INSERT INTO fi.sync_jobs (league_id, kind, idempotency_key)
  SELECT w.id, 'initial', 'initial:' || w.id FROM woken w
  WHERE NOT EXISTS (SELECT 1 FROM fi.league_snapshots s WHERE s.league_id = w.id)
  ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL AND status IN ('queued', 'running') DO NOTHING;
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
