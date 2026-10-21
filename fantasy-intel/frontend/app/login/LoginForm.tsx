"use client";

import Link from "next/link";
import { useActionState, useId } from "react";
import { Notice } from "@/components/ui";
import { sendLink, type LoginState } from "./actions";
import styles from "../leagues/leagues.module.css";

const IDLE: LoginState = { status: "idle", message: null };

export function LoginForm({ next }: { next: string }) {
  const [state, action, pending] = useActionState(sendLink, IDLE);
  const id = useId();
  if (state.status === "sent") {
    return <p className={styles.ok} role="status">{state.message}</p>;
  }
  return (
    <form action={action} className={styles.form}>
      <input type="hidden" name="next" value={next} />
      <div className={styles.field}>
        <label htmlFor={`${id}-email`}>Email</label>
        <input id={`${id}-email`} name="email" type="email" autoComplete="email" required spellCheck={false} />
        <p className={styles.hint}>We email you a one-time link. No password.</p>
      </div>
      <div className={styles.actions}>
        <button type="submit" disabled={pending}>{pending ? "Sending…" : "Email me a sign-in link"}</button>
      </div>
      <p className={styles.hint}>
        By signing in you agree to the <Link href="/terms" prefetch={false}>Terms</Link> and
        the <Link href="/privacy" prefetch={false}>Privacy policy</Link>.
      </p>
      {state.status === "error" && state.message ? <Notice tone="negative" title="Could not send the link">{state.message}</Notice> : null}
    </form>
  );
}
