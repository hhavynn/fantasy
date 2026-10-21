-- Reverses 20261022120000_launch_gates.sql: drops the account deletion function and the league cap, and restores
-- fi.ensure_app_user as 20261020121000_owner_lock_order.sql left it.

DROP FUNCTION IF EXISTS fi.delete_my_account();
DROP TRIGGER IF EXISTS league_memberships_cap ON fi.league_memberships;
DROP TRIGGER IF EXISTS league_pins_cap ON fi.league_pins;
DROP FUNCTION IF EXISTS fi.check_active_leagues();

CREATE OR REPLACE FUNCTION fi.ensure_app_user() RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid();
BEGIN
  PERFORM 1 FROM auth.users u WHERE u.id = v FOR KEY SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'sign_in_required'; END IF;
  INSERT INTO fi.app_users (id) VALUES (v) ON CONFLICT (id) DO UPDATE SET last_seen_at = now();
  RETURN v;
END $$;
REVOKE ALL ON FUNCTION fi.ensure_app_user() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.ensure_app_user() TO authenticated;
