"use client";

import { useActionState, useId, useState, useTransition } from "react";
import { Notice } from "@/components/ui";
import { CONSENT_TEXT } from "@/lib/my-leagues";
import {
  createInvite, deleteLeague, removeCookies, replaceCookies, revokeInvite, saveTeam, type ActionResult, type InviteState,
} from "../actions";
import styles from "../leagues.module.css";

const IDLE: ActionResult = { ok: false, message: null };

function Result({ result, done }: { result: ActionResult; done?: string }) {
  if (result.ok && done) return <p className={styles.ok} role="status">{done}</p>;
  if (!result.ok && result.message) return <p className={styles.fieldError} role="status">{result.message}</p>;
  return null;
}

export function TeamForm({ leagueId, teams, selected }: { leagueId: string; teams: { id: string; name: string }[]; selected: string | null }) {
  const [state, action, pending] = useActionState(saveTeam.bind(null, leagueId), IDLE);
  const id = useId();
  return (
    <form action={action} className={styles.teamForm}>
      <div className={styles.field}>
        <label htmlFor={`${id}-team`}>Team</label>
        <select id={`${id}-team`} name="team" defaultValue={selected ?? ""}>
          <option value="">No team (neutral view)</option>
          {teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
        </select>
      </div>
      <button type="submit" disabled={pending}>{pending ? "Saving…" : "Save"}</button>
      <Result result={state} done="Saved." />
    </form>
  );
}

export function CookieSettings({ leagueId }: { leagueId: string }) {
  const [state, action, pending] = useActionState(replaceCookies.bind(null, leagueId), IDLE);
  const [removing, start] = useTransition();
  const [removed, setRemoved] = useState<ActionResult | null>(null);
  const id = useId();
  return (
    <div className={styles.form}>
      <form action={action} className={styles.form}>
        <p className={styles.plain}>Store or replace your own cookies, for example after signing out of ESPN everywhere.</p>
        <div className={styles.field}>
          <label htmlFor={`${id}-s2`}>espn_s2</label>
          <input id={`${id}-s2`} name="espn_s2" type="password" autoComplete="off" spellCheck={false} required />
        </div>
        <div className={styles.field}>
          <label htmlFor={`${id}-swid`}>SWID</label>
          <input id={`${id}-swid`} name="swid" type="password" autoComplete="off" spellCheck={false} required />
        </div>
        <label className={styles.consent}><input type="checkbox" name="consent" required /> {CONSENT_TEXT}</label>
        <div className={styles.actions}>
          <button type="submit" disabled={pending}>{pending ? "Checking with ESPN…" : "Store my cookies"}</button>
        </div>
        <Result result={state} done="Stored. The next sync uses them." />
      </form>
      <div className={styles.actions}>
        <button type="button" className={styles.danger} disabled={removing}
                onClick={() => start(async () => setRemoved(await removeCookies(leagueId)))}>Remove my cookies</button>
      </div>
      {removed ? <Result result={removed} done="Removed. Syncing pauses until someone stores cookies again." /> : null}
    </div>
  );
}

const NO_INVITE: InviteState = { ok: false, message: null };

export function Invites({ leagueId, open }: { leagueId: string; open: { invite_id: string; expires_at: string }[] }) {
  const [state, action, pending] = useActionState(createInvite.bind(null, leagueId), NO_INVITE);
  const [busy, start] = useTransition();
  const [revoked, setRevoked] = useState<ActionResult | null>(null);
  const link = state.ok && state.link && typeof window !== "undefined" ? `${window.location.origin}${state.link}` : state.link;
  return (
    <div className={styles.form}>
      <form action={action} className={styles.actions}>
        <button type="submit" disabled={pending}>{pending ? "Creating…" : "Create an invite link"}</button>
      </form>
      {state.ok && link ? (
        <Notice tone="info" title="Copy this link now: it is shown only once">
          <p className={styles.inviteLink}>{link}</p>
        </Notice>
      ) : <Result result={state} />}
      {open.length > 0 ? (
        <ul className={styles.inviteList}>
          {open.map((i) => (
            <li key={i.invite_id}>
              <span>Open invite, expires {new Date(i.expires_at).toISOString().slice(0, 10)}</span>
              <button type="button" className={styles.secondary} disabled={busy}
                      onClick={() => start(async () => setRevoked(await revokeInvite(leagueId, i.invite_id)))}>Revoke</button>
            </li>
          ))}
        </ul>
      ) : <p className={styles.plain}>No open invites.</p>}
      {revoked ? <Result result={revoked} done="Revoked." /> : null}
    </div>
  );
}

export function DeleteLeague({ leagueId, name }: { leagueId: string; name: string }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);
  if (!confirming) {
    return <button type="button" className={styles.danger} onClick={() => setConfirming(true)}>Delete league…</button>;
  }
  return (
    <div className={styles.confirm}>
      <p className={styles.confirmText}>Delete {name} for every member? This cannot be undone after 7 days.</p>
      <div className={styles.actions}>
        <button type="button" className={styles.danger} disabled={busy}
                onClick={() => start(async () => setResult(await deleteLeague(leagueId)))}>Delete</button>
        <button type="button" className={styles.secondary} onClick={() => setConfirming(false)}>Cancel</button>
      </div>
      {result ? <Result result={result} /> : null}
    </div>
  );
}
