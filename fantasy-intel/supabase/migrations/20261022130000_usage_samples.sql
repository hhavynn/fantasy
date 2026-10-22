-- Phase 6 platform monitor (docs/superpowers/plans/2026-10-19-multi-league-phase-6.md, Task 4).
--   * fi.usage_samples: hourly free-tier usage samples, written by the GitHub Actions monitor as fi_sync and pruned
--     by it after 90 days. Metric names and units are constrained to short identifiers (the monitor's output is
--     public; a sample can never carry a name).
--   * fi.usage_counters(): database size and account count, for fi_sync (which cannot read fi.app_users).
--   * fi.usage_overview(): the latest sample per metric and its 7-day max, for platform admins only.
--   * fi.dead_jobs(interval): dead sync jobs in a window (ids, kind and error code only), for fi_sync.
-- Idempotent. Down-migration: supabase/down/20261022130000_usage_samples.sql.

CREATE TABLE IF NOT EXISTS fi.usage_samples (
  id         bigserial PRIMARY KEY,
  metric     text NOT NULL CHECK (metric ~ '^[a-z0-9_]{1,64}$'),
  used       double precision NOT NULL CHECK (used >= 0),
  quota      double precision CHECK (quota > 0),
  unit       text NOT NULL CHECK (unit ~ '^[a-z0-9_]{1,32}$'),
  sampled_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS usage_samples_metric_time ON fi.usage_samples (metric, sampled_at DESC);
ALTER TABLE fi.usage_samples ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON fi.usage_samples FROM PUBLIC, anon, authenticated, fi_api;
GRANT SELECT, INSERT, DELETE ON fi.usage_samples TO fi_sync;
GRANT USAGE, SELECT ON SEQUENCE fi.usage_samples_id_seq TO fi_sync;
-- fi_sync only: no policy for anon or authenticated, so request roles see nothing even if granted by mistake.
DROP POLICY IF EXISTS usage_samples_sync ON fi.usage_samples;
CREATE POLICY usage_samples_sync ON fi.usage_samples FOR ALL TO fi_sync USING (true) WITH CHECK (true);

CREATE OR REPLACE FUNCTION fi.usage_counters() RETURNS TABLE (db_bytes bigint, accounts bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
  SELECT pg_catalog.pg_database_size(pg_catalog.current_database()), (SELECT count(*) FROM fi.app_users)
$$;

-- Platform admins only. Anyone else (anonymous included) gets the same error, and the API answers 404 before it
-- ever calls this.
CREATE OR REPLACE FUNCTION fi.usage_overview()
RETURNS TABLE (metric text, used double precision, quota double precision, unit text, sampled_at timestamptz,
               max_used_7d double precision)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid();
BEGIN
  IF NOT EXISTS (SELECT 1 FROM fi.app_users u WHERE u.id = v AND u.is_platform_admin) THEN
    RAISE EXCEPTION 'not_found' USING ERRCODE = '42501';
  END IF;
  RETURN QUERY
    SELECT DISTINCT ON (s.metric) s.metric, s.used, s.quota, s.unit, s.sampled_at,
           (SELECT max(m.used) FROM fi.usage_samples m
            WHERE m.metric = s.metric AND m.sampled_at >= now() - interval '7 days')
    FROM fi.usage_samples s
    ORDER BY s.metric, s.sampled_at DESC;
END $$;

-- Spec §3.6: alert on `dead`. Ids, kind and error code only: never a league name or the error detail.
CREATE OR REPLACE FUNCTION fi.dead_jobs(p_since interval)
RETURNS TABLE (job_id uuid, league_id text, kind text, error_code text, finished_at timestamptz)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' AS $$
  SELECT j.id, j.league_id, j.kind, j.error_code, coalesce(j.finished_at, j.started_at, j.created_at)
  FROM fi.sync_jobs j
  WHERE j.status = 'dead' AND coalesce(j.finished_at, j.started_at, j.created_at) >= now() - p_since
  ORDER BY coalesce(j.finished_at, j.started_at, j.created_at) DESC, j.id
$$;

REVOKE ALL ON FUNCTION fi.usage_counters(), fi.usage_overview(), fi.dead_jobs(interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.usage_counters(), fi.dead_jobs(interval) TO fi_sync;
GRANT EXECUTE ON FUNCTION fi.usage_overview() TO authenticated;
