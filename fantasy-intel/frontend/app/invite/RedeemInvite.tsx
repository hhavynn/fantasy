"use client";

import { useState, useSyncExternalStore, useTransition } from "react";
import { Notice } from "@/components/ui";
import { redeemInvite, type ActionResult } from "../leagues/actions";

// The token is in the URL fragment (/invite#<token>): browsers never send a fragment to a server, so the token stays
// out of request logs until this page posts it once.
const noop = () => () => {};
const readHash = () => window.location.hash.replace(/^#/, "");

export function RedeemInvite() {
  const token = useSyncExternalStore(noop, readHash, () => "");
  const [busy, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);
  if (!token) {
    return <Notice tone="negative" title="This invite link is not complete">Copy the whole link from the person who invited you.</Notice>;
  }
  return (
    <div className="stack">
      <p>You were invited to a private league as a viewer.</p>
      <div>
        <button type="button" disabled={busy} onClick={() => start(async () => setResult(await redeemInvite(token)))}>
          {busy ? "Joining…" : "Join the league"}
        </button>
      </div>
      {result && !result.ok ? <Notice tone="negative" title="Could not join">{result.message}</Notice> : null}
    </div>
  );
}
