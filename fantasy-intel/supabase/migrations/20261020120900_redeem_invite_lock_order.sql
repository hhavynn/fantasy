-- redeem_invite takes its locks in the same order as account deletion: the league row first, then the invite.
-- on_app_user_delete locks the member's leagues and its created_by cascade then deletes their invites; migration 0008
-- locked the invite before the league, so a redemption overlapping its creator's account deletion could deadlock.
-- The invite is found without a lock, the league is locked and re-checked, then the invite is locked and re-validated.
-- promote_admin also locks the league before memberships, like demote_admin and account deletion.
-- Idempotent. Down-migration: supabase/down/20261020120900_redeem_invite_lock_order.sql.

CREATE OR REPLACE FUNCTION fi.redeem_invite(p_token_hash bytea) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.ensure_app_user(); v_league text; v_inv fi.league_invites%ROWTYPE;
BEGIN
  -- Lock order everywhere: league, then the rows that hang off it. Find the invite's league without locking...
  SELECT i.league_id INTO v_league FROM fi.league_invites i WHERE i.token_hash = p_token_hash;
  IF v_league IS NULL THEN RAISE EXCEPTION 'invite_invalid'; END IF;
  -- ...lock the league and re-check it is live (READ COMMITTED re-evaluates the WHERE on the newest row version)...
  PERFORM 1 FROM fi.leagues l WHERE l.id = v_league AND l.deleted_at IS NULL FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'invite_invalid'; END IF;
  -- ...then lock the invite and validate it again: it may have been redeemed, revoked or deleted meanwhile.
  SELECT * INTO v_inv FROM fi.league_invites WHERE token_hash = p_token_hash AND league_id = v_league FOR UPDATE;
  IF v_inv.id IS NULL OR v_inv.redeemed_at IS NOT NULL OR v_inv.revoked_at IS NOT NULL OR v_inv.expires_at <= now() THEN
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
REVOKE ALL ON FUNCTION fi.redeem_invite(bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.redeem_invite(bytea) TO authenticated;

CREATE OR REPLACE FUNCTION fi.promote_admin(p_league_id text, p_user uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid();
BEGIN
  IF NOT fi.is_admin(p_league_id) THEN RAISE EXCEPTION 'not_admin'; END IF;
  -- Lock order everywhere: league, then memberships (demote_admin and account deletion do the same).
  PERFORM 1 FROM fi.leagues WHERE id = p_league_id FOR UPDATE;
  UPDATE fi.league_memberships SET role = 'admin' WHERE league_id = p_league_id AND user_id = p_user AND revoked_at IS NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'not_a_member'; END IF;
  PERFORM fi.audit(v, 'membership.promote', 'league', p_league_id, jsonb_build_object('user_id', p_user));
END $$;
REVOKE ALL ON FUNCTION fi.promote_admin(text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.promote_admin(text, uuid) TO authenticated;
