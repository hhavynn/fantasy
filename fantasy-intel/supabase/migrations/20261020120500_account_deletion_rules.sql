-- Account deletion follows the spec's rules (design §2 "Account deletion") before the cascade removes the user's rows.
-- Deleting a Supabase account deletes its auth.users row, which cascades to fi.app_users (migration 0005); this
-- BEFORE DELETE trigger runs first, while the user's memberships and credentials still exist:
--   * a private league whose live credential this user owned becomes `stale` (sync paused, members keep read access);
--   * in each private league where this user is the last admin, the longest-standing remaining member becomes admin
--     (audit-logged); with no members left, the league is soft-deleted (its objects are collected later).
-- Public leagues are unaffected. Also: fi.current_account_exists() lets the API sign out a signature-valid token
-- whose account is gone, on every route. Idempotent. Down-migration: supabase/down/20261020120500_account_deletion_rules.sql.

CREATE OR REPLACE FUNCTION fi.on_app_user_delete() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE r record; v_heir uuid;
BEGIN
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

DROP TRIGGER IF EXISTS app_user_delete_rules ON fi.app_users;
CREATE TRIGGER app_user_delete_rules BEFORE DELETE ON fi.app_users FOR EACH ROW EXECUTE FUNCTION fi.on_app_user_delete();

CREATE OR REPLACE FUNCTION fi.current_account_exists() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
  SELECT EXISTS (SELECT 1 FROM auth.users u WHERE u.id = auth.uid())
$$;
REVOKE ALL ON FUNCTION fi.current_account_exists() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.current_account_exists() TO authenticated;
