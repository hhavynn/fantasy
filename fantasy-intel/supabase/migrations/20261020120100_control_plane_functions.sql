-- SECURITY DEFINER functions for mutations RLS cannot express. Each one checks auth.uid() itself, pins search_path,
-- and is executable only by the role named at the bottom. Errors are raised with short machine-readable messages
-- that the API maps to 401/404/409 without echoing details. Idempotent (CREATE OR REPLACE).
-- Down-migration: supabase/down/20261020120100_control_plane_functions.sql.

CREATE OR REPLACE FUNCTION fi.audit(p_actor uuid, p_action text, p_target_type text, p_target_id text, p_meta jsonb DEFAULT '{}'::jsonb)
RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path = '' AS $$
  INSERT INTO fi.audit_log (actor_user_id, action, target_type, target_id, meta)
  VALUES (p_actor, p_action, p_target_type, p_target_id, coalesce(p_meta, '{}'::jsonb))
$$;
REVOKE ALL ON FUNCTION fi.audit(uuid, text, text, text, jsonb) FROM PUBLIC;

CREATE OR REPLACE FUNCTION fi.audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'fi.audit_log is append-only';
END $$;
DROP TRIGGER IF EXISTS audit_log_immutable ON fi.audit_log;
CREATE TRIGGER audit_log_immutable BEFORE UPDATE OR DELETE ON fi.audit_log FOR EACH ROW EXECUTE FUNCTION fi.audit_log_immutable();

CREATE OR REPLACE FUNCTION fi.require_uid() RETURNS uuid LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := auth.uid();
BEGIN
  IF v IS NULL THEN RAISE EXCEPTION 'sign_in_required'; END IF;
  RETURN v;
END $$;
REVOKE ALL ON FUNCTION fi.require_uid() FROM PUBLIC;

CREATE OR REPLACE FUNCTION fi.ensure_app_user() RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid();
BEGIN
  INSERT INTO fi.app_users (id) VALUES (v) ON CONFLICT (id) DO UPDATE SET last_seen_at = now();
  RETURN v;
END $$;

CREATE OR REPLACE FUNCTION fi.create_invite(p_league_id text, p_token_hash bytea, p_ttl interval DEFAULT interval '7 days')
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid(); v_id uuid;
BEGIN
  IF NOT fi.is_admin(p_league_id) THEN RAISE EXCEPTION 'not_admin'; END IF;
  IF p_ttl IS NULL OR p_ttl <= interval '0' OR p_ttl > interval '7 days' THEN RAISE EXCEPTION 'ttl must be within 7 days'; END IF;
  IF NOT EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = p_league_id AND l.deleted_at IS NULL) THEN RAISE EXCEPTION 'not_admin'; END IF;
  INSERT INTO fi.league_invites (league_id, token_hash, created_by, expires_at)
  VALUES (p_league_id, p_token_hash, v, now() + p_ttl) RETURNING id INTO v_id;
  PERFORM fi.audit(v, 'invite.create', 'league', p_league_id, jsonb_build_object('invite_id', v_id));
  RETURN v_id;
END $$;

CREATE OR REPLACE FUNCTION fi.revoke_invite(p_invite_id uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid(); v_league text;
BEGIN
  SELECT league_id INTO v_league FROM fi.league_invites WHERE id = p_invite_id FOR UPDATE;
  IF v_league IS NULL OR NOT fi.is_admin(v_league) THEN RAISE EXCEPTION 'not_admin'; END IF;
  UPDATE fi.league_invites SET revoked_at = coalesce(revoked_at, now()) WHERE id = p_invite_id;
  PERFORM fi.audit(v, 'invite.revoke', 'league', v_league, jsonb_build_object('invite_id', p_invite_id));
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

CREATE OR REPLACE FUNCTION fi.promote_admin(p_league_id text, p_user uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid();
BEGIN
  IF NOT fi.is_admin(p_league_id) THEN RAISE EXCEPTION 'not_admin'; END IF;
  UPDATE fi.league_memberships SET role = 'admin' WHERE league_id = p_league_id AND user_id = p_user AND revoked_at IS NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'not_a_member'; END IF;
  PERFORM fi.audit(v, 'membership.promote', 'league', p_league_id, jsonb_build_object('user_id', p_user));
END $$;

CREATE OR REPLACE FUNCTION fi.demote_admin(p_league_id text, p_user uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid(); v_admins integer;
BEGIN
  IF NOT fi.is_admin(p_league_id) THEN RAISE EXCEPTION 'not_admin'; END IF;
  PERFORM 1 FROM fi.leagues WHERE id = p_league_id FOR UPDATE;      -- serialise last-admin checks per league
  SELECT count(*) INTO v_admins FROM fi.league_memberships WHERE league_id = p_league_id AND role = 'admin' AND revoked_at IS NULL;
  IF v_admins <= 1 THEN RAISE EXCEPTION 'last_admin'; END IF;
  UPDATE fi.league_memberships SET role = 'viewer' WHERE league_id = p_league_id AND user_id = p_user AND role = 'admin' AND revoked_at IS NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'not_a_member'; END IF;
  PERFORM fi.audit(v, 'membership.demote', 'league', p_league_id, jsonb_build_object('user_id', p_user));
END $$;

CREATE OR REPLACE FUNCTION fi.store_credential(p_credential_id uuid, p_league_id text, p_ciphertext bytea, p_nonce bytea,
                                               p_wrapped_data_key bytea, p_wrap_key_version text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid(); v_owner uuid;
BEGIN
  IF NOT fi.is_member(p_league_id) THEN RAISE EXCEPTION 'not_a_member'; END IF;
  PERFORM 1 FROM fi.leagues WHERE id = p_league_id AND deleted_at IS NULL FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'not_a_member'; END IF;
  SELECT owner_user_id INTO v_owner FROM fi.league_credentials WHERE league_id = p_league_id AND status IN ('active', 'suspect');
  IF v_owner IS NOT NULL AND v_owner <> v THEN RAISE EXCEPTION 'credential_exists'; END IF;
  -- The caller's previous credential (any status) is destroyed in the same transaction: ciphertext and wrapped key go.
  DELETE FROM fi.league_credentials WHERE league_id = p_league_id AND owner_user_id = v;
  INSERT INTO fi.league_credentials (id, league_id, owner_user_id, ciphertext, nonce, wrapped_data_key, wrap_key_version, last_validated_at)
  VALUES (p_credential_id, p_league_id, v, p_ciphertext, p_nonce, p_wrapped_data_key, p_wrap_key_version, now());
  PERFORM fi.audit(v, 'credential.store', 'league', p_league_id,
                   jsonb_build_object('credential_id', p_credential_id, 'wrap_key_version', p_wrap_key_version));
  RETURN p_credential_id;
END $$;

CREATE OR REPLACE FUNCTION fi.revoke_credential(p_league_id text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid(); v_id uuid;
BEGIN
  DELETE FROM fi.league_credentials WHERE league_id = p_league_id AND owner_user_id = v RETURNING id INTO v_id;
  IF v_id IS NULL THEN RAISE EXCEPTION 'not_owner'; END IF;
  IF NOT EXISTS (SELECT 1 FROM fi.league_credentials WHERE league_id = p_league_id AND status IN ('active', 'suspect')) THEN
    UPDATE fi.leagues SET status = 'stale' WHERE id = p_league_id AND visibility = 'private';
  END IF;
  PERFORM fi.audit(v, 'credential.revoke', 'league', p_league_id, jsonb_build_object('credential_id', v_id));
END $$;

CREATE OR REPLACE FUNCTION fi.sync_credential(p_job_id uuid, p_lease_token bigint)
RETURNS TABLE (credential_id uuid, league_id text, owner_user_id uuid, ciphertext bytea, nonce bytea, aad_format text,
               wrapped_data_key bytea, wrap_key_version text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
  SELECT c.id, c.league_id, c.owner_user_id, c.ciphertext, c.nonce, c.aad_format, c.wrapped_data_key, c.wrap_key_version
  FROM fi.sync_jobs j JOIN fi.league_credentials c ON c.league_id = j.league_id
  WHERE j.id = p_job_id AND j.status = 'running' AND j.lease_token = p_lease_token AND j.lease_expires_at > now()
    AND c.status IN ('active', 'suspect')
$$;

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

REVOKE ALL ON FUNCTION fi.ensure_app_user(), fi.create_invite(text, bytea, interval), fi.revoke_invite(uuid), fi.redeem_invite(bytea),
  fi.promote_admin(text, uuid), fi.demote_admin(text, uuid), fi.store_credential(uuid, text, bytea, bytea, bytea, text),
  fi.revoke_credential(text), fi.sync_credential(uuid, bigint), fi.mark_credential(uuid, bigint, text), fi.audit_log_immutable() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.ensure_app_user(), fi.create_invite(text, bytea, interval), fi.revoke_invite(uuid), fi.redeem_invite(bytea),
  fi.promote_admin(text, uuid), fi.demote_admin(text, uuid), fi.store_credential(uuid, text, bytea, bytea, bytea, text),
  fi.revoke_credential(text) TO authenticated;
GRANT EXECUTE ON FUNCTION fi.sync_credential(uuid, bigint), fi.mark_credential(uuid, bigint, text) TO fi_sync;
