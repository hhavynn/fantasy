"use client";

import { useActionState, useState } from "react";
import { Notice } from "@/components/ui";
import { removeLeague } from "./actions";
import { IDLE } from "./state";
import styles from "./leagues.module.css";

/** Two steps, because removing a connection ends this account's access to the league in this app. */
export function RemoveButton({ leagueId, name }: { leagueId: string; name: string }) {
  const [state, action, pending] = useActionState(removeLeague.bind(null, leagueId), IDLE);
  const [confirming, setConfirming] = useState(false);

  if (!confirming) {
    return <button type="button" className={styles.danger} onClick={() => setConfirming(true)}>Remove connection</button>;
  }
  return (
    <form action={action} className={styles.confirm}>
      <p className={styles.confirmText}>Remove the connection to {name}? You can connect it again later.</p>
      <div className={styles.actions}>
        <button type="submit" className={styles.danger} disabled={pending}>{pending ? "Removing…" : "Yes, remove connection"}</button>
        <button type="button" className={styles.secondary} disabled={pending} onClick={() => setConfirming(false)}>Keep it</button>
      </div>
      {state.status === "error" && state.message ? <Notice tone="negative" title="Could not remove the connection">{state.message}</Notice> : null}
    </form>
  );
}
