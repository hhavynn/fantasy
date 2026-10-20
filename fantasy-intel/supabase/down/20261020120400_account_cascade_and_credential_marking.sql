-- Reverses 20261020120400_account_cascade_and_credential_marking.sql: drops the auth.users link and restores the
-- 0002 definitions of ensure_app_user, redeem_invite and the 3-argument mark_credential.
ALTER TABLE fi.app_users DROP CONSTRAINT IF EXISTS app_users_auth_user_fk;
DROP FUNCTION IF EXISTS fi.mark_credential(uuid, bigint, uuid, text);

CREATE OR REPLACE FUNCTION fi.ensure_app_user() RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid();
BEGIN
  INSERT INTO fi.app_users (id) VALUES (v) ON CONFLICT (id) DO UPDATE SET last_seen_at = now();
  RETURN v;
END $$;

CREATE OR REPLACE FUNCTION fi.redeem_invite(p_token_hash bytea) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid(); v_inv fi.league_invites%ROWTYPE;
BEGIN
  SELECT * INTO v_inv FROM fi.league_invites WHERE token_hash = p_token_hash FOR UPDATE;
  IF v_inv.id IS NULL OR v_inv.redeemed_at IS NOT NULL OR v_inv.revoked_at IS NOT NULL OR v_inv.expires_at <= now()
     OR NOT EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = v_inv.league_id AND l.deleted_at IS NULL) THEN
    RAISE EXCEPTION 'invite_invalid';
  END IF;
  INSERT INTO fi.app_users (id) VALUES (v) ON CONFLICT (id) DO UPDATE SET last_seen_at = now();
  -- Viewer only. An existing admin keeps admin; a revoked member comes back as a viewer.
  INSERT INTO fi.league_memberships (user_id, league_id, role) VALUES (v, v_inv.league_id, 'viewer')
  ON CONFLICT (user_id, league_id) DO UPDATE
    SET role = CASE WHEN fi.league_memberships.revoked_at IS NULL THEN fi.league_memberships.role ELSE 'viewer' END,
        revoked_at = NULL;
  UPDATE fi.league_invites SET redeemed_by = v, redeemed_at = now() WHERE id = v_inv.id;
  PERFORM fi.audit(v, 'invite.redeem', 'league', v_inv.league_id, jsonb_build_object('invite_id', v_inv.id));
  RETURN v_inv.league_id;
END $$;

CREATE OR REPLACE FUNCTION fi.mark_credential(p_job_id uuid, p_lease_token bigint, p_status text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v_league text; v_old text;
BEGIN
  IF p_status NOT IN ('active', 'suspect', 'invalid') THEN RAISE EXCEPTION 'bad_status'; END IF;
  SELECT j.league_id INTO v_league FROM fi.sync_jobs j
  WHERE j.id = p_job_id AND j.status = 'running' AND j.lease_token = p_lease_token AND j.lease_expires_at > now();
  IF v_league IS NULL THEN RETURN false; END IF;
  SELECT status INTO v_old FROM fi.league_credentials WHERE league_id = v_league AND status IN ('active', 'suspect') FOR UPDATE;
  IF v_old IS NULL THEN RETURN false; END IF;
  UPDATE fi.league_credentials SET
    status = p_status,
    failure_count = CASE WHEN p_status = 'active' THEN 0 ELSE failure_count + 1 END,
    first_failed_at = CASE WHEN p_status = 'active' THEN NULL ELSE coalesce(first_failed_at, now()) END,
    last_validated_at = CASE WHEN p_status = 'active' THEN now() ELSE last_validated_at END
  WHERE league_id = v_league AND status IN ('active', 'suspect');
  IF p_status = 'invalid' THEN UPDATE fi.leagues SET status = 'stale' WHERE id = v_league; END IF;
  PERFORM fi.audit(NULL, 'credential.' || p_status, 'league', v_league, jsonb_build_object('job_id', p_job_id));
  RETURN true;
END $$;

REVOKE ALL ON FUNCTION fi.ensure_app_user(), fi.redeem_invite(bytea), fi.mark_credential(uuid, bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.ensure_app_user(), fi.redeem_invite(bytea) TO authenticated;
GRANT EXECUTE ON FUNCTION fi.mark_credential(uuid, bigint, text) TO fi_sync;
