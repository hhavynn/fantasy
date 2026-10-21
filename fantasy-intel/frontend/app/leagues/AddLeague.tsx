"use client";

import Link from "next/link";
import { useActionState, useId, useState } from "react";
import { Notice } from "@/components/ui";
import { leaguePath } from "@/lib/league-scope";
import { CONSENT_TEXT, type LeagueProviderChoice } from "@/lib/my-leagues";
import { addLeague, connectPrivate, type AddState } from "./actions";
import styles from "./leagues.module.css";

const IDLE: AddState = { status: "idle", message: null };

const PROVIDERS: { value: LeagueProviderChoice; label: string }[] = [
  { value: "espn", label: "ESPN" },
  { value: "sleeper", label: "Sleeper" },
];

/**
 * ESPN: step 1 tries the league as a public one; when ESPN says it is private, step 2 asks for the visitor's cookies.
 * Sleeper leagues are always public: one step, no cookies.
 */
export function AddLeague({ defaultSeason }: { defaultSeason: number }) {
  const [state, action, pending] = useActionState(addLeague, IDLE);
  const [provider, setProvider] = useState<LeagueProviderChoice>("espn");
  const id = useId();
  if (state.status === "needs_cookies" && state.league && state.season) {
    return <CookieForm league={state.league} season={state.season} intro={state.message} />;
  }
  return (
    <form action={action} className={styles.form}>
      <fieldset className={styles.providers}>
        <legend>Provider</legend>
        {PROVIDERS.map((p) => (
          <label key={p.value} className={styles.provider}>
            <input type="radio" name="provider" value={p.value} checked={provider === p.value}
                   onChange={() => setProvider(p.value)} /> {p.label}
          </label>
        ))}
      </fieldset>
      {provider === "sleeper" ? <SleeperFields id={id} /> : <EspnFields id={id} defaultSeason={defaultSeason} />}
      <div className={styles.actions}>
        <button type="submit" disabled={pending}>
          {pending ? (provider === "sleeper" ? "Checking Sleeper…" : "Checking ESPN…") : "Add league"}
        </button>
      </div>
      <Outcome state={state} />
    </form>
  );
}

function SleeperFields({ id }: { id: string }) {
  return (
    <div className={styles.field}>
      <label htmlFor={`${id}-sleeper`}>Sleeper league id or address</label>
      <input id={`${id}-sleeper`} name="league" type="text" autoComplete="off" autoCapitalize="off"
             spellCheck={false} required aria-describedby={`${id}-sleeper-hint`} />
      <p id={`${id}-sleeper-hint`} className={styles.hint}>
        Open the league on sleeper.com and paste the address, or just the long number after <code>/leagues/</code>.
        Sleeper leagues are public, so no cookies are needed.
      </p>
    </div>
  );
}

function EspnFields({ id, defaultSeason }: { id: string; defaultSeason: number }) {
  return (
    <>
      <div className={styles.field}>
        <label htmlFor={`${id}-league`}>ESPN league id or address</label>
        <input id={`${id}-league`} name="league" type="text" autoComplete="off" autoCapitalize="off" spellCheck={false}
               required aria-describedby={`${id}-league-hint`} />
        <p id={`${id}-league-hint`} className={styles.hint}>
          Open the league on fantasy.espn.com and paste the address, or just the number after <code>leagueId=</code>.
        </p>
      </div>
      <div className={styles.field}>
        <label htmlFor={`${id}-season`}>Season</label>
        <input id={`${id}-season`} name="season" type="text" inputMode="numeric" autoComplete="off"
               defaultValue={defaultSeason} className={styles.season} />
      </div>
    </>
  );
}

function CookieForm({ league, season, intro }: { league: string; season: number; intro: string | null }) {
  const [state, action, pending] = useActionState(connectPrivate, IDLE);
  const id = useId();
  if (state.status === "ok") return <Outcome state={state} />;
  const message = state.status === "idle" ? intro : state.message;
  return (
    <form action={action} className={styles.form}>
      {message ? <Notice tone={state.status === "idle" ? "info" : "negative"} title="A private league">{message}</Notice> : null}
      <input type="hidden" name="league" value={league} />
      <input type="hidden" name="season" value={season} />
      <details className={styles.details}>
        <summary>Where to find the cookies</summary>
        <ol className={styles.steps}>
          <li>Sign in to fantasy.espn.com in this browser.</li>
          <li>Open the browser&apos;s developer tools, then Application (Chrome) or Storage (Firefox, Safari), then Cookies for espn.com.</li>
          <li>Copy the values of <code>espn_s2</code> and <code>SWID</code>.</li>
        </ol>
      </details>
      <div className={styles.field}>
        <label htmlFor={`${id}-s2`}>espn_s2</label>
        <input id={`${id}-s2`} name="espn_s2" type="password" autoComplete="off" spellCheck={false} required />
      </div>
      <div className={styles.field}>
        <label htmlFor={`${id}-swid`}>SWID</label>
        <input id={`${id}-swid`} name="swid" type="password" autoComplete="off" spellCheck={false} required />
      </div>
      <label className={styles.consent}>
        <input type="checkbox" name="consent" required /> {CONSENT_TEXT}
      </label>
      <div className={styles.actions}>
        <button type="submit" disabled={pending}>{pending ? "Checking with ESPN…" : "Connect privately"}</button>
      </div>
    </form>
  );
}

function Outcome({ state }: { state: AddState }) {
  if (state.status === "error" && state.message) {
    return <Notice tone="negative" title="Could not add the league">{state.message}</Notice>;
  }
  if (state.status === "ok") {
    return (
      <p className={styles.ok} role="status">
        {state.message} {state.leagueId ? <Link href={leaguePath(state.leagueId, "/")} prefetch={false}>Open it</Link> : null}
      </p>
    );
  }
  return null;
}
