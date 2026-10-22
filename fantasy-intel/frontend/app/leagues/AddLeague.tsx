"use client";

import Link from "next/link";
import { useActionState, useId } from "react";
import { Notice } from "@/components/ui";
import { CONSENT_TEXT } from "@/lib/my-leagues";
import { addLeague, connectPrivate, type AddState } from "./actions";
import styles from "./leagues.module.css";

const IDLE: AddState = { status: "idle", message: null };
// Browser-side checks that mirror lib/my-leagues.ts's parsers (the server checks again): a bad id is caught before a
// request is made. The pattern attribute must match the whole value.
const ESPN_PATTERN = String.raw`\s*(\d{1,12}|\S*[?&]leagueId=\d{1,12}\S*)\s*`;
const SLEEPER_PATTERN = String.raw`\s*(\d{6,20}|\S*sleeper\.(com|app)/leagues/\d{6,20}\S*)\s*`;
const SEASON_PATTERN = String.raw`\s*\d{4}\s*`;

/**
 * Step 2 of adding a league, one form per host. Success redirects to the sync progress (or, for an ESPN league that
 * ESPN says is private, to the cookie step), so these forms only ever show a problem and keep what was typed.
 */
export function EspnForm({ defaultSeason }: { defaultSeason: number }) {
  const [state, action, pending] = useActionState(addLeague, IDLE);
  const id = useId();
  return (
    <form action={action} className={styles.form}>
      <input type="hidden" name="provider" value="espn" />
      <div className={styles.field}>
        <label htmlFor={`${id}-league`}>ESPN league id or address</label>
        <input id={`${id}-league`} name="league" type="text" autoComplete="off" autoCapitalize="off" spellCheck={false}
               required pattern={ESPN_PATTERN}
               title="The number after leagueId= in the ESPN address, or the whole address"
               defaultValue={state.input} key={`l-${state.input ?? ""}`} aria-describedby={`${id}-league-hint`}
               aria-invalid={state.next === "fix_input" || undefined} />
        <p id={`${id}-league-hint`} className={styles.hint}>
          Open the league on fantasy.espn.com and paste the address, or just the number after <code>leagueId=</code>.
        </p>
      </div>
      <div className={styles.field}>
        <label htmlFor={`${id}-season`}>Season</label>
        <input id={`${id}-season`} name="season" type="text" inputMode="numeric" autoComplete="off" pattern={SEASON_PATTERN}
               title="A year, for example 2026" defaultValue={state.season || defaultSeason} key={`s-${state.season ?? ""}`}
               className={styles.season} />
      </div>
      <div className={styles.actions}>
        <button type="submit" disabled={pending}>{pending ? "Checking ESPN…" : "Add league"}</button>
      </div>
      <Problem state={state} />
    </form>
  );
}

export function SleeperForm() {
  const [state, action, pending] = useActionState(addLeague, IDLE);
  const id = useId();
  return (
    <form action={action} className={styles.form}>
      <input type="hidden" name="provider" value="sleeper" />
      <div className={styles.field}>
        <label htmlFor={`${id}-sleeper`}>Sleeper league id or address</label>
        <input id={`${id}-sleeper`} name="league" type="text" autoComplete="off" autoCapitalize="off" spellCheck={false}
               required pattern={SLEEPER_PATTERN}
               title="The long number after /leagues/ in the sleeper.com address, or the whole address"
               defaultValue={state.input} key={`l-${state.input ?? ""}`} aria-describedby={`${id}-sleeper-hint`}
               aria-invalid={state.next === "fix_input" || undefined} />
        <p id={`${id}-sleeper-hint`} className={styles.hint}>
          Open the league on sleeper.com and paste the address, or just the long number after <code>/leagues/</code>.
          Sleeper leagues are public, so no cookies are needed.
        </p>
      </div>
      <div className={styles.actions}>
        <button type="submit" disabled={pending}>{pending ? "Checking Sleeper…" : "Add league"}</button>
      </div>
      <Problem state={state} />
    </form>
  );
}

/** The cookie step for an ESPN league that ESPN reported as private. The consent text and fields are unchanged. */
export function CookieForm({ league, season }: { league: string; season: number }) {
  const [state, action, pending] = useActionState(connectPrivate, IDLE);
  const id = useId();
  return (
    <form action={action} className={styles.form}>
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
        <input type="checkbox" name="consent" required />
        <span>{CONSENT_TEXT} See the <Link href="/privacy" prefetch={false} target="_blank" rel="noopener">Privacy policy</Link>.</span>
      </label>
      <div className={styles.actions}>
        <button type="submit" disabled={pending}>{pending ? "Checking with ESPN…" : "Connect privately"}</button>
        <Link href="/leagues?host=espn" prefetch={false} className={styles.linkButton}>Use another league</Link>
      </div>
      <Problem state={state} />
    </form>
  );
}

/** A failed attempt: what happened (the backend's own words), and what to do next. */
function Problem({ state }: { state: AddState }) {
  if (state.status !== "error" || !state.message) return null;
  return (
    <Notice tone="negative" title={state.title ?? "Could not add the league"}>
      <p className={styles.problemText}>{state.message}</p>
      {state.hint ? <p className={styles.problemText}>{state.hint}</p> : null}
      {state.next === "sign_in" ? (
        <p className={styles.problemText}><Link href="/login?next=/leagues" prefetch={false}>Sign in</Link>, then try again.</p>
      ) : state.next === "manage" ? (
        <p className={styles.problemText}><a href="#your-leagues">Go to your leagues</a></p>
      ) : state.next === "invite" ? (
        <p className={styles.problemText}>An invite link works once, for 7 days; open it while signed in and the league appears in your list.</p>
      ) : null}
    </Notice>
  );
}
