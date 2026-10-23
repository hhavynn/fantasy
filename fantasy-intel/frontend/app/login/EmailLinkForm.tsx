"use client";

import Link from "@/components/LeagueLink";
import { useActionState, useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Notice } from "@/components/ui";
import { authHref, type LinkIntent, type LinkState } from "@/lib/auth-flow";
import { sendSignupLink } from "../signup/actions";
import { sendLink } from "./actions";
import styles from "./auth.module.css";

const IDLE: LinkState = { status: "idle", message: null };

const COPY = {
  signin: { submit: "Email me a sign-in link", agree: "By signing in" },
  signup: { submit: "Email me a link to create my account", agree: "By creating an account" },
} as const;

/**
 * The one email-link form behind Sign in and Sign up: the same field, pending state and messages, with each intent's
 * own server action (only Sign up can create an account). While a request is in flight the button is disabled and a
 * second submit is ignored; once sent, the form gives way to "Check your email" until the visitor asks again.
 */
export function EmailLinkForm({ intent, next }: { intent: LinkIntent; next: string }) {
  const [state, action, pending] = useActionState(intent === "signup" ? sendSignupLink : sendLink, IDLE);
  // Controlled, so the address survives an error (React resets an uncontrolled form after its action runs).
  const [email, setEmail] = useState("");
  const [dismissed, setDismissed] = useState<LinkState | null>(null);
  const sentRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const editing = useRef(false);
  const id = useId();
  const sent = state.status === "sent" && state !== dismissed;

  useEffect(() => {
    if (sent) sentRef.current?.focus();
    else if (editing.current) inputRef.current?.focus();
    editing.current = false;
  }, [sent]);

  if (sent) {
    return (
      <div ref={sentRef} tabIndex={-1} className={styles.sent} role="status">
        <Notice tone="positive" title="Check your email">
          <p>{state.message}</p>
        </Notice>
        <p className={styles.hint}>
          Nothing after a few minutes? Check your spam folder, or{" "}
          <button type="button" className={styles.textButton}
                  onClick={() => { editing.current = true; setDismissed(state); }}>
            ask again or use a different address
          </button>.
        </p>
      </div>
    );
  }

  const onSubmit = (e: FormEvent<HTMLFormElement>) => {
    if (pending) e.preventDefault();      // a second Enter or click while the first request is still running
  };
  const error = state.status === "error" && state !== dismissed ? state : null;
  const closed = state.status === "closed";
  const errorId = `${id}-error`;
  const hintId = `${id}-hint`;
  const emailError = error?.field === "email";
  return (
    <form action={action} onSubmit={onSubmit} className={styles.form} aria-busy={pending || undefined} noValidate>
      <input type="hidden" name="next" value={next} />
      <div className={styles.field}>
        <label htmlFor={`${id}-email`}>Email</label>
        <input ref={inputRef} id={`${id}-email`} name="email" type="email" inputMode="email" autoComplete="email" required
               spellCheck={false} autoCapitalize="none" value={email} onChange={(e) => setEmail(e.target.value)}
               aria-invalid={emailError || undefined}
               aria-describedby={emailError ? `${errorId} ${hintId}` : hintId} />
        <p id={hintId} className={styles.hint}>
          No password: we email you a one-time link. Open it in this browser, within an hour.
        </p>
      </div>
      {error ? (
        <div id={errorId}>
          <Notice tone="negative" title={emailError ? "Check the email address" : "The link was not sent"}>{error.message}</Notice>
        </div>
      ) : null}
      {closed ? (
        <Notice tone="info" title="Sign-ups are not open yet">
          Nothing was sent. If you already have an account, <Link href={authHref("/login", next)} prefetch={false}>sign in</Link>.
        </Notice>
      ) : null}
      <div className={styles.actions}>
        <button type="submit" disabled={pending || closed}>{pending ? "Sending…" : COPY[intent].submit}</button>
      </div>
      <p className={styles.hint}>
        {COPY[intent].agree} you agree to the <Link href="/terms" prefetch={false}>Terms</Link> and
        the <Link href="/privacy" prefetch={false}>Privacy policy</Link>.
      </p>
    </form>
  );
}
