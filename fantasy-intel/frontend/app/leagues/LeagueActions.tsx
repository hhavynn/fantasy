"use client";

import { useState, useTransition } from "react";
import { pinLeague, refreshLeague, unpinLeague, type ActionResult } from "./actions";
import styles from "./leagues.module.css";

/** Refresh and pin/unpin for one league card. The server action re-checks everything; this only reports back. */
export function LeagueActions({ leagueId, pinned, member }: { leagueId: string; pinned: boolean; member: boolean }) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);
  const run = (fn: () => Promise<ActionResult>, done: string) => start(async () => {
    const r = await fn();
    setResult(r.ok ? { ok: true, message: done } : r);
  });
  return (
    <div className={styles.actions}>
      <button type="button" disabled={pending} onClick={() => run(() => refreshLeague(leagueId), "Refresh requested.")}>Refresh</button>
      {pinned ? (
        <button type="button" className={styles.secondary} disabled={pending}
                onClick={() => run(() => unpinLeague(leagueId), member ? "Unpinned (you are still a member)." : "Removed from your list.")}>
          {member ? "Unpin" : "Remove from my list"}
        </button>
      ) : (
        <button type="button" className={styles.secondary} disabled={pending} onClick={() => run(() => pinLeague(leagueId), "Pinned.")}>Pin</button>
      )}
      {result?.message ? <p className={result.ok ? styles.ok : styles.fieldError} role="status">{result.message}</p> : null}
    </div>
  );
}
