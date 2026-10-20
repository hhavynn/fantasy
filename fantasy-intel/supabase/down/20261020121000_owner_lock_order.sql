-- Reverses 20261020121000_owner_lock_order.sql: the 0005 ensure_app_user and the 0002 store_credential.

CREATE OR REPLACE FUNCTION fi.ensure_app_user() RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid();
BEGIN
  -- A signature-valid token can outlive its account; such a caller is signed out, not a new user.
  IF NOT EXISTS (SELECT 1 FROM auth.users u WHERE u.id = v) THEN RAISE EXCEPTION 'sign_in_required'; END IF;
  INSERT INTO fi.app_users (id) VALUES (v) ON CONFLICT (id) DO UPDATE SET last_seen_at = now();
  RETURN v;
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
REVOKE ALL ON FUNCTION fi.ensure_app_user(), fi.store_credential(uuid, text, bytea, bytea, bytea, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.ensure_app_user(), fi.store_credential(uuid, text, bytea, bytea, bytea, text) TO authenticated;
