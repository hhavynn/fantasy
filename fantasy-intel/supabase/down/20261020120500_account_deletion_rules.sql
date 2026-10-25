-- Reverses 20261020120500_account_deletion_rules.sql.
DROP TRIGGER IF EXISTS app_user_delete_rules ON fi.app_users;
DROP FUNCTION IF EXISTS fi.on_app_user_delete(), fi.current_account_exists();
