"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, useTransition } from "react";
import { refreshLeague, type ActionResult } from "./actions";
import styles from "./leagues.module.css";

const POLL_MS = 10_000;

/**
 * Step 3's live part. The sync state itself is rendered on the server from GET /v2/leagues/{id}; while a sync is queued
 * or running this re-renders the page every 10 seconds (only while the tab is visible), so the steps move on and the
 * "Open your dashboard" link appears without a reload. A reload shows the same state: nothing lives only here.
 */
export function ProgressActions({ leagueId, polling, canRetry }: { leagueId: string; polling: boolean; canRetry: boolean }) {
  const router = useRouter();
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);

  useEffect(() => {
    if (!polling) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") router.refresh();
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [polling, router]);

  const retry = () => start(async () => {
    const r = await refreshLeague(leagueId);
    setResult(r.ok ? { ok: true, message: "Sync requested. This page updates by itself." } : r);
    router.refresh();
  });

  return (
    <div className={styles.actions}>
      {canRetry ? <button type="button" disabled={pending} onClick={retry}>{pending ? "Asking…" : "Try again"}</button> : null}
      <button type="button" className={styles.secondary} disabled={pending} onClick={() => start(() => router.refresh())}>
        Check now
      </button>
      {polling ? <p className={styles.hint} role="status">This page checks again every 10 seconds.</p> : null}
      {result?.message ? <p className={result.ok ? styles.ok : styles.fieldError} role="status">{result.message}</p> : null}
    </div>
  );
}
