-- Reverses 20261020120300_preference_policies_recheck_visibility.sql: back to the 0001 policies.
DROP POLICY IF EXISTS pins_own ON fi.league_pins;
CREATE POLICY pins_own ON fi.league_pins FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid() AND EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = league_id));

DROP POLICY IF EXISTS selections_own ON fi.team_selections;
CREATE POLICY selections_own ON fi.team_selections FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid() AND EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = league_id));
