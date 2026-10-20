-- Reverses 20261020120000_control_plane.sql. Run only when rolling Phase 1 back entirely
-- (after 20261020120100_control_plane_functions.sql has been reversed, or let the CASCADE take the functions).
DROP SCHEMA IF EXISTS fi CASCADE;
REVOKE anon, authenticated FROM fi_api;
DROP ROLE IF EXISTS fi_api;
DROP ROLE IF EXISTS fi_sync;
