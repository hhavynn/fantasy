-- Invite redemption serializes with account deletion (and anything else that changes the league row): it locks the
-- league row before adding the membership and re-checks that the league is live. A concurrent account deletion
-- that soft-deletes the league holds that lock (on_app_user_delete, migration 0007), so redemption waits for it and
-- then refuses with invite_invalid instead of joining a league that is being deleted.
-- Idempotent. Down-migration: supabase/down/20261020120700_redeem_invite_locks_league.sql.

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
