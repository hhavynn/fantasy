"use client";

import { useActionState, useId } from "react";
import { deleteAccount, type DeleteState } from "./actions";
import { CONFIRM_WORD } from "./confirm";
import styles from "../leagues/leagues.module.css";

const IDLE: DeleteState = { ok: false, message: null };

export function DeleteAccount() {
  const [state, action, pending] = useActionState(deleteAccount, IDLE);
  const id = useId();
  return (
    <form action={action} className={styles.confirm}>
      <p className={styles.confirmText}>
        This deletes your account now. Your ESPN cookies are erased at once, and you leave every league. A private
        league with no members left is deleted, and its data goes 7 days later. Public leagues stay for everyone else.
      </p>
      <div className={styles.field}>
        <label htmlFor={`${id}-confirm`}>Type {CONFIRM_WORD} to confirm</label>
        <input id={`${id}-confirm`} name="confirm" autoComplete="off" spellCheck={false} required />
      </div>
      <div className={styles.actions}>
        <button type="submit" className={styles.danger} disabled={pending}>{pending ? "Deleting…" : "Delete my account"}</button>
      </div>
      {state.message ? <p className={styles.fieldError} role="status">{state.message}</p> : null}
    </form>
  );
}
