-- Reverses 20261020120900_redeem_invite_lock_order.sql: the 0008 redeem_invite and the 0002 promote_admin.

CREATE OR REPLACE FUNCTION fi.redeem_invite(p_token_hash bytea) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.ensure_app_user(); v_inv fi.league_invites%ROWTYPE;
BEGIN
  SELECT * INTO v_inv FROM fi.league_invites WHERE token_hash = p_token_hash FOR UPDATE;
  IF v_inv.id IS NULL OR v_inv.redeemed_at IS NOT NULL OR v_inv.revoked_at IS NOT NULL OR v_inv.expires_at <= now() THEN
    RAISE EXCEPTION 'invite_invalid';
  END IF;
  -- Lock the league and re-check it under the lock (READ COMMITTED re-evaluates the WHERE on the newest row version).
  PERFORM 1 FROM fi.leagues l WHERE l.id = v_inv.league_id AND l.deleted_at IS NULL FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'invite_invalid'; END IF;
  -- Viewer only. An existing admin keeps admin; a revoked member comes back as a viewer.
  INSERT INTO fi.league_memberships (user_id, league_id, role) VALUES (v, v_inv.league_id, 'viewer')
  ON CONFLICT (user_id, league_id) DO UPDATE
    SET role = CASE WHEN fi.league_memberships.revoked_at IS NULL THEN fi.league_memberships.role ELSE 'viewer' END,
        revoked_at = NULL;
  UPDATE fi.league_invites SET redeemed_by = v, redeemed_at = now() WHERE id = v_inv.id;
  PERFORM fi.audit(v, 'invite.redeem', 'league', v_inv.league_id, jsonb_build_object('invite_id', v_inv.id));
  RETURN v_inv.league_id;
END $$;
REVOKE ALL ON FUNCTION fi.redeem_invite(bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.redeem_invite(bytea) TO authenticated;

CREATE OR REPLACE FUNCTION fi.promote_admin(p_league_id text, p_user uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid();
BEGIN
  IF NOT fi.is_admin(p_league_id) THEN RAISE EXCEPTION 'not_admin'; END IF;
  UPDATE fi.league_memberships SET role = 'admin' WHERE league_id = p_league_id AND user_id = p_user AND revoked_at IS NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'not_a_member'; END IF;
  PERFORM fi.audit(v, 'membership.promote', 'league', p_league_id, jsonb_build_object('user_id', p_user));
END $$;
REVOKE ALL ON FUNCTION fi.promote_admin(text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.promote_admin(text, uuid) TO authenticated;
