"use client";

import { useActionState, useId, useState, type FormEvent } from "react";
import { Notice } from "@/components/ui";
import { validateConnect, type ConnectErrors, type ProviderId } from "@/lib/leagues";
import { connectLeague } from "./actions";
import { IDLE } from "./state";
import styles from "./leagues.module.css";

/** League id and season for one provider. The server action re-validates and the backend decides. */
export function ConnectForm({ provider, label, defaultSeason, hint }: {
  provider: ProviderId;
  label: string;
  defaultSeason: number;
  hint: string;
}) {
  const [state, action, pending] = useActionState(connectLeague, IDLE);
  const [clientErrors, setClientErrors] = useState<ConnectErrors | null>(null);
  const id = useId();
  const errors = clientErrors ?? state.fieldErrors ?? {};

  function check(e: FormEvent<HTMLFormElement>) {
    const data = new FormData(e.currentTarget);
    const result = validateConnect({
      leagueId: String(data.get("external_league_id") ?? ""),
      season: String(data.get("season") ?? ""),
    });
    if (result.ok) { setClientErrors(null); return; }
    e.preventDefault();
    setClientErrors(result.errors);
  }

  return (
    <form action={action} onSubmit={check} noValidate className={styles.form}>
      <input type="hidden" name="provider" value={provider} />
      <div className={styles.field}>
        <label htmlFor={`${id}-league`}>{label} league id</label>
        <input
          id={`${id}-league`} name="external_league_id" type="text" inputMode="numeric" autoComplete="off"
          autoCapitalize="off" spellCheck={false} required
          aria-invalid={errors.leagueId ? true : undefined}
          aria-describedby={`${id}-league-hint${errors.leagueId ? ` ${id}-league-err` : ""}`}
        />
        <p id={`${id}-league-hint`} className={styles.hint}>{hint}</p>
        {errors.leagueId ? <p id={`${id}-league-err`} className={styles.fieldError}>Error: {errors.leagueId}</p> : null}
      </div>
      <div className={styles.field}>
        <label htmlFor={`${id}-season`}>Season</label>
        <input
          id={`${id}-season`} name="season" type="text" inputMode="numeric" autoComplete="off" required
          defaultValue={defaultSeason} className={styles.season}
          aria-invalid={errors.season ? true : undefined}
          aria-describedby={errors.season ? `${id}-season-err` : undefined}
        />
        {errors.season ? <p id={`${id}-season-err`} className={styles.fieldError}>Error: {errors.season}</p> : null}
      </div>
      <div className={styles.actions}>
        <button type="submit" disabled={pending}>{pending ? "Connecting…" : `Connect ${label}`}</button>
      </div>
      {state.status === "error" && state.message ? <Notice tone="negative" title="Could not connect the league">{state.message}</Notice> : null}
      {state.status === "ok" && state.message ? <p className={styles.ok} role="status">{state.message}</p> : null}
    </form>
  );
}
