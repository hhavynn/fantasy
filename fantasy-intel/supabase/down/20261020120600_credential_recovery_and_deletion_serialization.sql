-- Reverses 20261020120600_credential_recovery_and_deletion_serialization.sql: re-apply the earlier definitions.
-- Run migrations/20261020120400_account_cascade_and_credential_marking.sql (mark_credential) and
-- migrations/20261020120500_account_deletion_rules.sql (on_app_user_delete) again; both are idempotent.
SELECT 1;
