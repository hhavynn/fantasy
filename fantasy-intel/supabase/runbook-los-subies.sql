-- One-time registration of Los Subies in the control plane (multi-league Phase 2). Applied 2026-10-20 through the
-- Supabase connector as the project owner. Private, with no members: invisible to everyone until Havyn signs in and
-- is made its admin (Phase 3), and it has no snapshot pointer until the first scripts/publish_leagues.py run.
-- Idempotent: the unique (provider, external_id, season) key makes a second run a no-op.
INSERT INTO fi.leagues (id, provider, external_id, season, name, visibility, status)
VALUES ('lg_KxH_Y1Y56aM7qnCCseKvs1sU', 'espn', '854268537', 2026, 'Los Subies', 'private', 'active')
ON CONFLICT (provider, external_id, season) DO NOTHING;

-- After Havyn's first sign-in (Phase 3), make the account the league's admin with its own team as perspective:
--   INSERT INTO fi.app_users (id) VALUES ('<auth.users id>') ON CONFLICT DO NOTHING;
--   INSERT INTO fi.league_memberships (user_id, league_id, role) VALUES ('<auth.users id>', 'lg_KxH_Y1Y56aM7qnCCseKvs1sU', 'admin');
--   INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES ('<auth.users id>', 'lg_KxH_Y1Y56aM7qnCCseKvs1sU', '<team id>');
