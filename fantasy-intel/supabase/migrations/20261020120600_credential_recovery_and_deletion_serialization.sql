-- Two corrections from review (design §2 "Credential lifecycle" and "Account deletion"):
--   1. mark_credential(..., 'active') returns a `stale` private league to `active` once a (replacement) credential
--      validates; `unavailable` and `dormant` are left alone.
--   2. on_app_user_delete locks the user's leagues (in id order) before deciding, so the last members deleting their
--      accounts concurrently cannot both skip the heir / memberless rules.
-- Idempotent. Down-migration: supabase/down/20261020120600_credential_recovery_and_deletion_serialization.sql.

CREATE OR REPLACE FUNCTION fi.mark_credential(p_job_id uuid, p_lease_token bigint, p_credential_id uuid, p_status text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v_league text; v_marked uuid;
BEGIN
  IF p_status NOT IN ('active', 'suspect', 'invalid') THEN RAISE EXCEPTION 'bad_status'; END IF;
  -- Hold the job row for the rest of the transaction: a reclaim (which updates this row) cannot interleave.
  SELECT j.league_id INTO v_league FROM fi.sync_jobs j
  WHERE j.id = p_job_id AND j.status = 'running' AND j.lease_token = p_lease_token AND j.lease_expires_at > now()
  FOR UPDATE;
  IF v_league IS NULL THEN RETURN false; END IF;
  -- Only the credential this worker fetched (via sync_credential), and only while it is still live.
  UPDATE fi.league_credentials SET
    status = p_status,
    failure_count = CASE WHEN p_status = 'active' THEN 0 ELSE failure_count + 1 END,
    first_failed_at = CASE WHEN p_status = 'active' THEN NULL ELSE coalesce(first_failed_at, now()) END,
    last_validated_at = CASE WHEN p_status = 'active' THEN now() ELSE last_validated_at END
  WHERE id = p_credential_id AND league_id = v_league AND status IN ('active', 'suspect')
  RETURNING id INTO v_marked;
  IF v_marked IS NULL THEN RETURN false; END IF;
  IF p_status = 'invalid' THEN
    UPDATE fi.leagues SET status = 'stale' WHERE id = v_league;
  ELSIF p_status = 'active' THEN
    -- A validated (replacement) credential ends a pause caused by a lost one. Only `stale` is revived: `unavailable`
    -- (league gone on ESPN) and `dormant` (nobody looking) have other causes.
    UPDATE fi.leagues SET status = 'active' WHERE id = v_league AND status = 'stale';
  END IF;
  PERFORM fi.audit(NULL, 'credential.' || p_status, 'league', v_league,
                   jsonb_build_object('job_id', p_job_id, 'credential_id', p_credential_id));
  RETURN true;
END $$;

REVOKE ALL ON FUNCTION fi.mark_credential(uuid, bigint, uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.mark_credential(uuid, bigint, uuid, text) TO fi_sync;

CREATE OR REPLACE FUNCTION fi.on_app_user_delete() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE r record; v_heir uuid;
BEGIN
  -- Serialize per league before deciding anything: two members deleting their accounts at once must not each see
  -- the other's membership and both skip the heir / memberless rules. Locks are taken in id order (no deadlock); at
  -- READ COMMITTED every statement below then sees what a concurrent deletion committed while this one waited.
  PERFORM 1 FROM fi.leagues l
  WHERE l.id IN (SELECT m.league_id FROM fi.league_memberships m WHERE m.user_id = OLD.id AND m.revoked_at IS NULL)
     OR l.id IN (SELECT c.league_id FROM fi.league_credentials c WHERE c.owner_user_id = OLD.id)
  ORDER BY l.id
  FOR UPDATE;

  UPDATE fi.leagues l SET status = 'stale'
  WHERE l.visibility = 'private' AND l.deleted_at IS NULL
    AND EXISTS (SELECT 1 FROM fi.league_credentials c
                WHERE c.league_id = l.id AND c.owner_user_id = OLD.id AND c.status IN ('active', 'suspect'));

  FOR r IN
    SELECT m.league_id FROM fi.league_memberships m JOIN fi.leagues l ON l.id = m.league_id
    WHERE m.user_id = OLD.id AND m.role = 'admin' AND m.revoked_at IS NULL
      AND l.visibility = 'private' AND l.deleted_at IS NULL
      AND NOT EXISTS (SELECT 1 FROM fi.league_memberships o
                      WHERE o.league_id = m.league_id AND o.user_id <> OLD.id AND o.role = 'admin' AND o.revoked_at IS NULL)
  LOOP
    SELECT o.user_id INTO v_heir FROM fi.league_memberships o
    WHERE o.league_id = r.league_id AND o.user_id <> OLD.id AND o.revoked_at IS NULL
    ORDER BY o.created_at, o.user_id LIMIT 1;
    IF v_heir IS NULL THEN
      UPDATE fi.leagues SET deleted_at = now() WHERE id = r.league_id;
      PERFORM fi.audit(NULL, 'league.delete_on_account_deletion', 'league', r.league_id, '{}'::jsonb);
    ELSE
      UPDATE fi.league_memberships SET role = 'admin' WHERE league_id = r.league_id AND user_id = v_heir;
      PERFORM fi.audit(NULL, 'membership.promote_on_account_deletion', 'league', r.league_id,
                       jsonb_build_object('user_id', v_heir));
    END IF;
  END LOOP;

  -- A private league whose only remaining members were viewers of a deleted non-admin keeps its admins; a league
  -- left with no active member at all (the deleted user was its only member) is soft-deleted as well.
  UPDATE fi.leagues l SET deleted_at = now()
  WHERE l.visibility = 'private' AND l.deleted_at IS NULL
    AND EXISTS (SELECT 1 FROM fi.league_memberships m WHERE m.league_id = l.id AND m.user_id = OLD.id AND m.revoked_at IS NULL)
    AND NOT EXISTS (SELECT 1 FROM fi.league_memberships o
                    WHERE o.league_id = l.id AND o.user_id <> OLD.id AND o.revoked_at IS NULL);

  PERFORM fi.audit(NULL, 'account.delete', 'user', OLD.id::text, '{}'::jsonb);
  RETURN OLD;
END $$;
REVOKE ALL ON FUNCTION fi.on_app_user_delete() FROM PUBLIC;
