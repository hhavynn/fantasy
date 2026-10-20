-- 1. Account deletion crypto-erases. fi.app_users mirrors auth.users; deleting the Supabase account deletes the
--    mirror row, which cascades to the user's credentials (ciphertext + wrapped data key), memberships, pins,
--    perspectives and created invites. A still-valid token for a deleted account cannot recreate the mirror row.
-- 2. A sync worker marks only the credential it fetched, and only while it still holds the job's lease: the job row
--    is locked while the lease is checked, so a concurrent reclaim either finishes first (and the stale token no
--    longer matches) or waits for this function. A member who re-enters cookies mid-job keeps the new credential.
-- Idempotent. Down-migration: supabase/down/20261020120400_account_cascade_and_credential_marking.sql.

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'app_users_auth_user_fk' AND conrelid = 'fi.app_users'::regclass) THEN
    ALTER TABLE fi.app_users ADD CONSTRAINT app_users_auth_user_fk
      FOREIGN KEY (id) REFERENCES auth.users(id) ON DELETE CASCADE;
  END IF;
END $$;

CREATE OR REPLACE FUNCTION fi.ensure_app_user() RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid();
BEGIN
  -- A signature-valid token can outlive its account; such a caller is signed out, not a new user.
  IF NOT EXISTS (SELECT 1 FROM auth.users u WHERE u.id = v) THEN RAISE EXCEPTION 'sign_in_required'; END IF;
  INSERT INTO fi.app_users (id) VALUES (v) ON CONFLICT (id) DO UPDATE SET last_seen_at = now();
  RETURN v;
END $$;

CREATE OR REPLACE FUNCTION fi.redeem_invite(p_token_hash bytea) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.ensure_app_user(); v_inv fi.league_invites%ROWTYPE;
BEGIN
  SELECT * INTO v_inv FROM fi.league_invites WHERE token_hash = p_token_hash FOR UPDATE;
  IF v_inv.id IS NULL OR v_inv.redeemed_at IS NOT NULL OR v_inv.revoked_at IS NOT NULL OR v_inv.expires_at <= now()
     OR NOT EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = v_inv.league_id AND l.deleted_at IS NULL) THEN
    RAISE EXCEPTION 'invite_invalid';
  END IF;
  -- Viewer only. An existing admin keeps admin; a revoked member comes back as a viewer.
  INSERT INTO fi.league_memberships (user_id, league_id, role) VALUES (v, v_inv.league_id, 'viewer')
  ON CONFLICT (user_id, league_id) DO UPDATE
    SET role = CASE WHEN fi.league_memberships.revoked_at IS NULL THEN fi.league_memberships.role ELSE 'viewer' END,
        revoked_at = NULL;
  UPDATE fi.league_invites SET redeemed_by = v, redeemed_at = now() WHERE id = v_inv.id;
  PERFORM fi.audit(v, 'invite.redeem', 'league', v_inv.league_id, jsonb_build_object('invite_id', v_inv.id));
  RETURN v_inv.league_id;
END $$;

DROP FUNCTION IF EXISTS fi.mark_credential(uuid, bigint, text);
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
  IF p_status = 'invalid' THEN UPDATE fi.leagues SET status = 'stale' WHERE id = v_league; END IF;
  PERFORM fi.audit(NULL, 'credential.' || p_status, 'league', v_league,
                   jsonb_build_object('job_id', p_job_id, 'credential_id', p_credential_id));
  RETURN true;
END $$;

REVOKE ALL ON FUNCTION fi.mark_credential(uuid, bigint, uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.mark_credential(uuid, bigint, uuid, text) TO fi_sync;
REVOKE ALL ON FUNCTION fi.ensure_app_user(), fi.redeem_invite(bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.ensure_app_user(), fi.redeem_invite(bytea) TO authenticated;
