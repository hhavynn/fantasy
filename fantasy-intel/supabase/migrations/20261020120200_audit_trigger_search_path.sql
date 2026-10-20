-- Pin search_path on the audit trigger function too (Supabase security advisor: function_search_path_mutable).
-- Every function in schema fi now runs with search_path = ''. Down-migration: none needed (same body, no setting).
CREATE OR REPLACE FUNCTION fi.audit_log_immutable() RETURNS trigger LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
  RAISE EXCEPTION 'fi.audit_log is append-only';
END $$;
REVOKE ALL ON FUNCTION fi.audit_log_immutable() FROM PUBLIC;
