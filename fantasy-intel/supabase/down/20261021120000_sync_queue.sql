-- Reverses 20261021120000_sync_queue.sql.

DROP FUNCTION IF EXISTS fi.league_purged(text);
DROP FUNCTION IF EXISTS fi.league_timers();
DROP FUNCTION IF EXISTS fi.record_league_found(uuid, bigint);
DROP FUNCTION IF EXISTS fi.record_league_missing(uuid, bigint, boolean);
DROP FUNCTION IF EXISTS fi.record_credential_failure(uuid, bigint, uuid, boolean);
DROP TRIGGER IF EXISTS leagues_status_changed ON fi.leagues;
DROP FUNCTION IF EXISTS fi.leagues_status_changed();
REVOKE UPDATE (name) ON fi.leagues FROM fi_sync;
ALTER TABLE fi.leagues DROP COLUMN IF EXISTS objects_purged_at;
ALTER TABLE fi.leagues DROP COLUMN IF EXISTS missing_since;
ALTER TABLE fi.leagues DROP COLUMN IF EXISTS missing_count;
ALTER TABLE fi.leagues DROP COLUMN IF EXISTS status_changed_at;
DROP TABLE IF EXISTS fi.global_snapshot;
