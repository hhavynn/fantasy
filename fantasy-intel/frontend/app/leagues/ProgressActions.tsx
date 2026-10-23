"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, useTransition } from "react";
import { useLeagueHref } from "@/components/useAppPathname";
import { refreshLeague, unpinLeague, type ActionResult } from "./actions";
import styles from "./leagues.module.css";

const POLL_MS = 10_000;

/**
 * Step 3's live part. The sync state itself is rendered on the server from GET /v2/leagues/{id}; while a sync is queued
 * or running this re-renders the page every 10 seconds (only while the tab is visible), so the steps move on and the
 * "Open your dashboard" link appears without a reload. A reload shows the same state: nothing lives only here.
 * `pinned`: the league is in the visitor's list, so it can be taken off it here (the way out of a sync that will not
 * finish): unpinned, it leaves the list; a member stays a member. Nothing changes at the provider.
 */
export function ProgressActions({ leagueId, polling, canRetry, pinned = false, member = false }:
  { leagueId: string; polling: boolean; canRetry: boolean; pinned?: boolean; member?: boolean }) {
  const router = useRouter();
  const leagueHref = useLeagueHref();
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

  const remove = () => start(async () => {
    const r = await unpinLeague(leagueId);
    if (!r.ok) { setResult(r); return; }
    router.push(leagueHref("/leagues"));
  });

  return (
    <div className={styles.actions}>
      {canRetry ? <button type="button" disabled={pending} onClick={retry}>{pending ? "Asking…" : "Try again"}</button> : null}
      <button type="button" className={styles.secondary} disabled={pending} onClick={() => start(() => router.refresh())}>
        Check now
      </button>
      {pinned ? (
        <button type="button" className={styles.secondary} disabled={pending} onClick={remove}>
          {member ? "Unpin this league" : "Remove from my list"}
        </button>
      ) : null}
      {polling ? <p className={styles.hint} role="status">This page checks again every 10 seconds.</p> : null}
      {result?.message ? <p className={result.ok ? styles.ok : styles.fieldError} role="status">{result.message}</p> : null}
    </div>
  );
}
