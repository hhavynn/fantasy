-- Phase 3b: registering leagues and asking for syncs, from the signed-in user's request (spec §2, §3.2).
-- The API decides what it can verify (the league's visibility from an anonymous fetch, the caller's cookies from one
-- authenticated fetch) before calling these; each function re-checks auth.uid() and the league's state under lock.
-- Lock order everywhere: the account (ensure_app_user), the app user, the league, then the league's own rows.
-- Idempotent. Down-migration: supabase/down/20261021130000_connect_flows.sql.

CREATE INDEX IF NOT EXISTS audit_log_actor_action ON fi.audit_log (actor_user_id, action, at DESC);

-- 20 registrations per account per day: a public import or private connect queues provider work.
CREATE OR REPLACE FUNCTION fi.check_league_quota(p_user uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = '' AS $$
BEGIN
  IF (SELECT count(*) FROM fi.audit_log a WHERE a.actor_user_id = p_user AND a.action IN ('league.import', 'league.connect')
      AND a.at > now() - interval '1 day') >= 20 THEN
    RAISE EXCEPTION 'league_quota';
  END IF;
END $$;

-- Internal: the first sync of a league that has no published version yet (no grant to request roles).
CREATE OR REPLACE FUNCTION fi.queue_initial(p_league_id text) RETURNS void LANGUAGE sql SECURITY DEFINER
SET search_path = '' AS $$
  INSERT INTO fi.sync_jobs (league_id, kind, idempotency_key)
  SELECT p_league_id, 'initial', 'initial:' || p_league_id
  WHERE NOT EXISTS (SELECT 1 FROM fi.league_snapshots s WHERE s.league_id = p_league_id)
  ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL AND status IN ('queued', 'running') DO NOTHING
$$;

-- Public import: the API has just read the league without cookies. Registers it once (unique provider/id/season),
-- pins it for the caller and queues its first sync. A league registered privately is never imported as public.
CREATE OR REPLACE FUNCTION fi.import_public_league(p_provider text, p_external_id text, p_season integer, p_name text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.ensure_app_user(); v_id text; v_vis text; v_deleted timestamptz;
BEGIN
  PERFORM fi.check_league_quota(v);
  INSERT INTO fi.leagues (id, provider, external_id, season, name, visibility, visibility_checked_at)
  VALUES ('lg_' || replace(gen_random_uuid()::text, '-', ''), p_provider, p_external_id, p_season, left(p_name, 200),
          'public', now())
  ON CONFLICT (provider, external_id, season) DO NOTHING;
  SELECT l.id, l.visibility, l.deleted_at INTO v_id, v_vis, v_deleted FROM fi.leagues l
  WHERE l.provider = p_provider AND l.external_id = p_external_id AND l.season = p_season FOR UPDATE;
  IF v_vis <> 'public' OR v_deleted IS NOT NULL THEN RAISE EXCEPTION 'league_unavailable'; END IF;
  -- A re-import wakes a dormant league (its next sync rebuilds purged objects) and counts as a fresh check.
  UPDATE fi.leagues SET visibility_checked_at = now(),
         status = CASE WHEN status = 'dormant' THEN 'active' ELSE status END,
         objects_purged_at = CASE WHEN status = 'dormant' THEN NULL ELSE objects_purged_at END
  WHERE id = v_id;
  INSERT INTO fi.league_pins (user_id, league_id) VALUES (v, v_id) ON CONFLICT DO NOTHING;
  PERFORM fi.queue_initial(v_id);
  PERFORM fi.audit(v, 'league.import', 'league', v_id, '{}'::jsonb);
  RETURN v_id;
END $$;

-- Private connect: the API has validated the caller's cookies with one fetch (and seen that the league is not
-- public). A new league makes the caller its admin; an existing private league admits the caller as a viewer only
-- when their own cookies show them owning a team in it (p_owns_team, checked by the API), never as admin. The
-- credential is stored next (fi.store_credential) in the same transaction, then fi.queue_initial.
CREATE OR REPLACE FUNCTION fi.connect_private_league(p_provider text, p_external_id text, p_season integer, p_name text,
                                                     p_owns_team boolean)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.ensure_app_user(); v_id text; v_vis text; v_deleted timestamptz; v_new boolean;
BEGIN
  PERFORM fi.check_league_quota(v);
  INSERT INTO fi.leagues (id, provider, external_id, season, name, visibility, visibility_checked_at)
  VALUES ('lg_' || replace(gen_random_uuid()::text, '-', ''), p_provider, p_external_id, p_season, left(p_name, 200),
          'private', now())
  ON CONFLICT (provider, external_id, season) DO NOTHING
  RETURNING true INTO v_new;
  SELECT l.id, l.visibility, l.deleted_at INTO v_id, v_vis, v_deleted FROM fi.leagues l
  WHERE l.provider = p_provider AND l.external_id = p_external_id AND l.season = p_season FOR UPDATE;
  IF v_deleted IS NOT NULL THEN RAISE EXCEPTION 'league_unavailable'; END IF;
  IF coalesce(v_new, false) THEN
    INSERT INTO fi.league_memberships (user_id, league_id, role) VALUES (v, v_id, 'admin');
  ELSIF v_vis = 'public' THEN
    RAISE EXCEPTION 'league_public';
  ELSIF NOT fi.is_member(v_id) THEN
    IF NOT p_owns_team THEN RAISE EXCEPTION 'not_a_league_member'; END IF;
    INSERT INTO fi.league_memberships (user_id, league_id, role) VALUES (v, v_id, 'viewer')
    ON CONFLICT (user_id, league_id) DO UPDATE SET revoked_at = NULL, role = 'viewer';
  END IF;
  PERFORM fi.audit(v, 'league.connect', 'league', v_id, jsonb_build_object('new', coalesce(v_new, false)));
  RETURN v_id;
END $$;

-- The last step of a private connect, after fi.store_credential in the same transaction: a member of a league with a
-- live credential queues its first sync.
CREATE OR REPLACE FUNCTION fi.start_private_sync(p_league_id text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = '' AS $$
BEGIN
  PERFORM fi.ensure_app_user();
  PERFORM 1 FROM fi.leagues l WHERE l.id = p_league_id AND l.deleted_at IS NULL FOR UPDATE;
  IF NOT FOUND OR NOT fi.is_member(p_league_id) THEN RAISE EXCEPTION 'league_unavailable'; END IF;
  IF NOT EXISTS (SELECT 1 FROM fi.league_credentials c WHERE c.league_id = p_league_id AND c.status IN ('active', 'suspect')) THEN
    RAISE EXCEPTION 'no_credential';
  END IF;
  PERFORM fi.queue_initial(p_league_id);
END $$;

-- A member (private) or anyone signed in (public) asks for a refresh: at most one manual sync per league per 15 min.
-- Returns the queued job's id, or NULL when one is already queued or ran recently.
CREATE OR REPLACE FUNCTION fi.request_sync(p_league_id text) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = '' AS $$
DECLARE v uuid := fi.ensure_app_user(); v_vis text; v_status text; v_job uuid;
BEGIN
  SELECT l.visibility, l.status INTO v_vis, v_status FROM fi.leagues l
  WHERE l.id = p_league_id AND l.deleted_at IS NULL FOR UPDATE;
  IF v_vis IS NULL OR (v_vis = 'private' AND NOT fi.is_member(p_league_id)) THEN RAISE EXCEPTION 'league_unavailable'; END IF;
  -- stale (the cookies stopped working) or unavailable (gone on the provider): a refresh cannot help
  IF v_status NOT IN ('active', 'dormant') THEN RAISE EXCEPTION 'sync_paused'; END IF;
  IF EXISTS (SELECT 1 FROM fi.sync_jobs j WHERE j.league_id = p_league_id AND j.kind = 'manual'
             AND j.created_at > now() - interval '15 minutes') THEN
    RETURN NULL;
  END IF;
  UPDATE fi.leagues SET status = 'active' WHERE id = p_league_id AND status = 'dormant';
  INSERT INTO fi.sync_jobs (league_id, kind, idempotency_key) VALUES (p_league_id, 'manual', 'manual:' || p_league_id)
  ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL AND status IN ('queued', 'running') DO NOTHING
  RETURNING id INTO v_job;
  PERFORM fi.audit(v, 'league.sync_requested', 'league', p_league_id, '{}'::jsonb);
  RETURN v_job;
END $$;

-- Spec §2: the first viewer after a public league's visibility check expired triggers a probe (at most one per
-- league per 5 minutes). True when a probe was queued; only the API sees the answer (it then starts the drain), the
-- caller always gets the same 404.
CREATE OR REPLACE FUNCTION fi.request_probe(p_league_id text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = '' AS $$
DECLARE v_job uuid;
BEGIN
  IF EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = p_league_id AND l.deleted_at IS NULL AND l.visibility = 'public'
             AND (l.visibility_checked_at IS NULL OR l.visibility_checked_at < now() - interval '24 hours'))
     AND NOT EXISTS (SELECT 1 FROM fi.sync_jobs j WHERE j.league_id = p_league_id AND j.kind = 'probe'
                     AND j.created_at > now() - interval '5 minutes') THEN
    INSERT INTO fi.sync_jobs (league_id, kind, idempotency_key) VALUES (p_league_id, 'probe', 'probe:' || p_league_id)
    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL AND status IN ('queued', 'running') DO NOTHING
    RETURNING id INTO v_job;
  END IF;
  RETURN v_job IS NOT NULL;
END $$;

-- An admin deletes a private league: soft delete now, objects deleted 7 days later (fi.league_timers). Public
-- leagues are platform-managed: users only unpin them.
CREATE OR REPLACE FUNCTION fi.delete_league(p_league_id text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = '' AS $$
DECLARE v uuid := fi.ensure_app_user(); v_vis text;
BEGIN
  SELECT l.visibility INTO v_vis FROM fi.leagues l WHERE l.id = p_league_id AND l.deleted_at IS NULL FOR UPDATE;
  IF v_vis IS NULL OR NOT fi.is_admin(p_league_id) THEN RAISE EXCEPTION 'not_admin'; END IF;
  IF v_vis <> 'private' THEN RAISE EXCEPTION 'league_public'; END IF;
  UPDATE fi.leagues SET deleted_at = now() WHERE id = p_league_id;
  PERFORM fi.audit(v, 'league.delete', 'league', p_league_id, '{}'::jsonb);
END $$;

REVOKE ALL ON FUNCTION fi.check_league_quota(uuid), fi.queue_initial(text),
  fi.import_public_league(text, text, integer, text), fi.connect_private_league(text, text, integer, text, boolean),
  fi.start_private_sync(text), fi.request_sync(text), fi.request_probe(text), fi.delete_league(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.import_public_league(text, text, integer, text),
  fi.connect_private_league(text, text, integer, text, boolean), fi.start_private_sync(text), fi.request_sync(text),
  fi.delete_league(text) TO authenticated;
GRANT EXECUTE ON FUNCTION fi.request_probe(text) TO anon, authenticated;
