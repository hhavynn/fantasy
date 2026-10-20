-- Reverses 20261020120800_credential_lock_order.sql: the 0007 mark_credential and the 0002 revoke_credential.

CREATE OR REPLACE FUNCTION fi.mark_credential(p_job_id uuid, p_lease_token bigint, p_credential_id uuid, p_status text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v_league text; v_marked uuid;
BEGIN
  IF p_status NOT IN ('active', 'suspect', 'invalid') THEN RAISE EXCEPTION 'bad_status'; END IF;
  -- Hold the job row for the rest of the transaction: a reclaim (which updates this row) cannot interleave.
  SELECT j.league_id INTO v_league FROM fi.sync_jobs j
  WHERE j.id = p_job_id AND j.status = 'running' AND j.lease_token = p_lease_token AND j.lease_expires_at > now()
  FOR UPDATE;
  IF v_league IS NULL THEN RETURN false; END IF;
  -- Only the credential this worker fetched (via sync_credential), and only while it is still live.
  UPDATE fi.league_credentials SET
    status = p_status,
    failure_count = CASE WHEN p_status = 'active' THEN 0 ELSE failure_count + 1 END,
    first_failed_at = CASE WHEN p_status = 'active' THEN NULL ELSE coalesce(first_failed_at, now()) END,
    last_validated_at = CASE WHEN p_status = 'active' THEN now() ELSE last_validated_at END
  WHERE id = p_credential_id AND league_id = v_league AND status IN ('active', 'suspect')
  RETURNING id INTO v_marked;
  IF v_marked IS NULL THEN RETURN false; END IF;
  IF p_status = 'invalid' THEN
    UPDATE fi.leagues SET status = 'stale' WHERE id = v_league;
  ELSIF p_status = 'active' THEN
    -- A validated (replacement) credential ends a pause caused by a lost one. Only `stale` is revived: `unavailable`
    -- (league gone on ESPN) and `dormant` (nobody looking) have other causes.
    UPDATE fi.leagues SET status = 'active' WHERE id = v_league AND status = 'stale';
  END IF;
  PERFORM fi.audit(NULL, 'credential.' || p_status, 'league', v_league,
                   jsonb_build_object('job_id', p_job_id, 'credential_id', p_credential_id));
  RETURN true;
END $$;
REVOKE ALL ON FUNCTION fi.mark_credential(uuid, bigint, uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.mark_credential(uuid, bigint, uuid, text) TO fi_sync;

CREATE OR REPLACE FUNCTION fi.revoke_credential(p_league_id text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid(); v_id uuid;
BEGIN
  DELETE FROM fi.league_credentials WHERE league_id = p_league_id AND owner_user_id = v RETURNING id INTO v_id;
  IF v_id IS NULL THEN RAISE EXCEPTION 'not_owner'; END IF;
  IF NOT EXISTS (SELECT 1 FROM fi.league_credentials WHERE league_id = p_league_id AND status IN ('active', 'suspect')) THEN
    UPDATE fi.leagues SET status = 'stale' WHERE id = p_league_id AND visibility = 'private';
  END IF;
  PERFORM fi.audit(v, 'credential.revoke', 'league', p_league_id, jsonb_build_object('credential_id', v_id));
END $$;
REVOKE ALL ON FUNCTION fi.revoke_credential(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.revoke_credential(text) TO authenticated;
