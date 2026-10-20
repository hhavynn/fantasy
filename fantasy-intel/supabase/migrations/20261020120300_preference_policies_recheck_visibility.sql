-- Preference rows (pins, perspective) are reachable only while their league is visible to the caller, for every
-- command. Postgres applies no WITH CHECK to DELETE, so the visibility test must be in USING as well; otherwise a
-- caller who lost access (league turned private, membership revoked) could still delete or rewrite them.
-- `fi.leagues` is itself under RLS, so the EXISTS sees exactly what the caller may see. Idempotent.
-- Down-migration: supabase/down/20261020120300_preference_policies_recheck_visibility.sql.

DROP POLICY IF EXISTS pins_own ON fi.league_pins;
CREATE POLICY pins_own ON fi.league_pins FOR ALL TO authenticated
  USING (user_id = auth.uid() AND EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = league_id))
  WITH CHECK (user_id = auth.uid() AND EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = league_id));

DROP POLICY IF EXISTS selections_own ON fi.team_selections;
CREATE POLICY selections_own ON fi.team_selections FOR ALL TO authenticated
  USING (user_id = auth.uid() AND EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = league_id))
  WITH CHECK (user_id = auth.uid() AND EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = league_id));
