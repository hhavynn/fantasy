-- Canonical lock order for the control plane, matching account deletion: the account (auth.users), then the app user
-- (fi.app_users), then the league, then the league's rows (credentials, memberships, invites, pins, perspectives).
-- ensure_app_user takes the account lock first, and store_credential now calls it before locking the league, so a
-- member re-entering cookies while their account is deleted waits and is then signed out instead of deadlocking.
-- Idempotent. Down-migration: supabase/down/20261020121000_owner_lock_order.sql.

CREATE OR REPLACE FUNCTION fi.ensure_app_user() RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid();
BEGIN
  -- First lock in the canonical order (account, app user, league, league's rows). Waits for an in-flight account
  -- deletion, after which the account is gone: a signature-valid token for it is signed out, not a new user.
  PERFORM 1 FROM auth.users u WHERE u.id = v FOR KEY SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'sign_in_required'; END IF;
  INSERT INTO fi.app_users (id) VALUES (v) ON CONFLICT (id) DO UPDATE SET last_seen_at = now();
  RETURN v;
END $$;

CREATE OR REPLACE FUNCTION fi.store_credential(p_credential_id uuid, p_league_id text, p_ciphertext bytea, p_nonce bytea,
                                               p_wrapped_data_key bytea, p_wrap_key_version text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.ensure_app_user(); v_owner uuid;   -- the owner rows first, then the league (canonical order)
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
REVOKE ALL ON FUNCTION fi.ensure_app_user(), fi.store_credential(uuid, text, bytea, bytea, bytea, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.ensure_app_user(), fi.store_credential(uuid, text, bytea, bytea, bytea, text) TO authenticated;
