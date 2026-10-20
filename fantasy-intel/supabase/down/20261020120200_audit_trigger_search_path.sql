-- Reverses 20261020120200_audit_trigger_search_path.sql: the 0002 trigger function (no pinned search_path).
CREATE OR REPLACE FUNCTION fi.audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'fi.audit_log is append-only';
END $$;
REVOKE ALL ON FUNCTION fi.audit_log_immutable() FROM PUBLIC;
