-- Reverses 20261021130000_connect_flows.sql.

DROP FUNCTION IF EXISTS fi.delete_league(text);
DROP FUNCTION IF EXISTS fi.request_probe(text);
DROP FUNCTION IF EXISTS fi.request_sync(text);
DROP FUNCTION IF EXISTS fi.start_private_sync(text);
DROP FUNCTION IF EXISTS fi.connect_private_league(text, text, integer, text, boolean);
DROP FUNCTION IF EXISTS fi.import_public_league(text, text, integer, text);
DROP FUNCTION IF EXISTS fi.queue_initial(text);
DROP FUNCTION IF EXISTS fi.check_league_quota(uuid);
DROP INDEX IF EXISTS fi.audit_log_actor_action;
