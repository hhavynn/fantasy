-- Reverses 20261020120400_account_cascade_and_credential_marking.sql: drops the auth.users link and the
-- credential-bound mark_credential. Re-apply migrations/20261020120100_control_plane_functions.sql afterwards to
-- restore the earlier ensure_app_user / redeem_invite / 3-argument mark_credential.
ALTER TABLE fi.app_users DROP CONSTRAINT IF EXISTS app_users_auth_user_fk;
DROP FUNCTION IF EXISTS fi.mark_credential(uuid, bigint, uuid, text);
