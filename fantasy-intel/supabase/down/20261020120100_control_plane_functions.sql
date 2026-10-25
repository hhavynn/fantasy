-- Reverses 20261020120100_control_plane_functions.sql.
DROP TRIGGER IF EXISTS audit_log_immutable ON fi.audit_log;
DROP FUNCTION IF EXISTS fi.mark_credential(uuid, bigint, text), fi.sync_credential(uuid, bigint), fi.revoke_credential(text),
  fi.store_credential(uuid, text, bytea, bytea, bytea, text), fi.demote_admin(text, uuid), fi.promote_admin(text, uuid),
  fi.redeem_invite(bytea), fi.revoke_invite(uuid), fi.create_invite(text, bytea, interval), fi.ensure_app_user(),
  fi.require_uid(), fi.audit_log_immutable(), fi.audit(uuid, text, text, text, jsonb);
