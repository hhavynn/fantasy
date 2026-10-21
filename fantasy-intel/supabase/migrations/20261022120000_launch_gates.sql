-- Phase 6: launch gates (docs/superpowers/plans/2026-10-19-multi-league-phase-6.md, Task 2).
--   * Sign-up gate: the API sets the transaction-local `fi.signups_open` ('on' | 'off') from SIGNUPS_ENABLED on every
--     request; while it is 'off', fi.ensure_app_user refuses to create an account it has not seen before. Supabase
--     Auth can be called directly with the publishable key, so this is the gate that matters: a new auth user gets
--     no rows and no data access until sign-ups open. Unset (direct SQL, tests) keeps the old behavior.
--   * At most 25 leagues per account (pins and live memberships, counted once per league), on every insert path.
--   * Audit retention (privacy policy: one year): rows older than a year may be deleted, only by
--     fi.prune_audit_log(), which the drain's maintenance runs as fi_sync. Newer rows stay append-only.
--   * Account deletion by the account itself: deletes its own auth.users row; the Phase 1 cascade and the
--     fi.app_users delete rules do the rest (credentials crypto-erased, heir promoted, empty league soft-deleted).
-- Idempotent. Down-migration: supabase/down/20261022120000_launch_gates.sql.

CREATE OR REPLACE FUNCTION fi.ensure_app_user() RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid();
BEGIN
  -- First lock in the canonical order (account, app user, league, league's rows). Waits for an in-flight account
  -- deletion, after which the account is gone: a signature-valid token for it is signed out, not a new user.
  PERFORM 1 FROM auth.users u WHERE u.id = v FOR KEY SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'sign_in_required'; END IF;
  IF current_setting('fi.signups_open', true) = 'off' AND NOT EXISTS (SELECT 1 FROM fi.app_users a WHERE a.id = v) THEN
    RAISE EXCEPTION 'signups_closed';
  END IF;
  INSERT INTO fi.app_users (id) VALUES (v) ON CONFLICT (id) DO UPDATE SET last_seen_at = now();
  RETURN v;
END $$;
REVOKE ALL ON FUNCTION fi.ensure_app_user() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.ensure_app_user() TO authenticated;

-- 25 leagues per account. A league the account already has (pinned, or a live member) is never counted twice, so
-- re-importing or re-pinning it at the cap still works. The account's app-user row is locked first, so concurrent
-- inserts for one account count one after another (READ COMMITTED: each count sees the commits it waited for).
-- Every request path already holds that lock from fi.ensure_app_user's upsert, so the canonical lock order (account,
-- app user, league, league's rows) is unchanged.
CREATE OR REPLACE FUNCTION fi.check_active_leagues() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = '' AS $$
BEGIN
  PERFORM 1 FROM fi.app_users a WHERE a.id = NEW.user_id FOR UPDATE;
  IF EXISTS (SELECT 1 FROM fi.league_pins p WHERE p.user_id = NEW.user_id AND p.league_id = NEW.league_id)
     OR EXISTS (SELECT 1 FROM fi.league_memberships m
                WHERE m.user_id = NEW.user_id AND m.league_id = NEW.league_id AND m.revoked_at IS NULL) THEN
    RETURN NEW;
  END IF;
  IF (SELECT count(*) FROM (SELECT p.league_id FROM fi.league_pins p WHERE p.user_id = NEW.user_id
                            UNION
                            SELECT m.league_id FROM fi.league_memberships m
                            WHERE m.user_id = NEW.user_id AND m.revoked_at IS NULL) mine) >= 25 THEN
    RAISE EXCEPTION 'league_limit';
  END IF;
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION fi.check_active_leagues() FROM PUBLIC;

DROP TRIGGER IF EXISTS league_pins_cap ON fi.league_pins;
CREATE TRIGGER league_pins_cap BEFORE INSERT ON fi.league_pins
  FOR EACH ROW EXECUTE FUNCTION fi.check_active_leagues();
DROP TRIGGER IF EXISTS league_memberships_cap ON fi.league_memberships;
CREATE TRIGGER league_memberships_cap BEFORE INSERT ON fi.league_memberships
  FOR EACH ROW EXECUTE FUNCTION fi.check_active_leagues();

-- The caller deletes their own account. Only their own auth.users row: the id comes from the verified claims, never
-- from an argument. Deleting it takes the account lock first (canonical order); the cascade does the rest.
CREATE OR REPLACE FUNCTION fi.delete_my_account() RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid();
BEGIN
  DELETE FROM auth.users u WHERE u.id = v;
  IF NOT FOUND THEN RAISE EXCEPTION 'sign_in_required'; END IF;
END $$;
REVOKE ALL ON FUNCTION fi.delete_my_account() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.delete_my_account() TO authenticated;

-- The audit log stays append-only, except that a row older than a year may be deleted. Only the table owner holds
-- DELETE, so in practice the only delete is fi.prune_audit_log().
CREATE OR REPLACE FUNCTION fi.audit_log_immutable() RETURNS trigger LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
  IF TG_OP = 'DELETE' AND OLD.at < now() - interval '1 year' THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'fi.audit_log is append-only';
END $$;
REVOKE ALL ON FUNCTION fi.audit_log_immutable() FROM PUBLIC;

CREATE OR REPLACE FUNCTION fi.prune_audit_log() RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = '' AS $$
DECLARE n integer;
BEGIN
  DELETE FROM fi.audit_log a WHERE a.at < now() - interval '1 year';
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
REVOKE ALL ON FUNCTION fi.prune_audit_log() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.prune_audit_log() TO fi_sync;
