-- Reverses 20261022130000_usage_samples.sql.

DROP FUNCTION IF EXISTS fi.dead_jobs(interval);
DROP FUNCTION IF EXISTS fi.usage_overview();
DROP FUNCTION IF EXISTS fi.usage_counters();
DROP TABLE IF EXISTS fi.usage_samples;
