"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { ACCOUNT_MARKER_KEY, purgeOwnedKeys, reconcileAccount, type PageAccount } from "@/lib/local-data";

const local = () => window.localStorage;
const session = () => window.sessionStorage;

/**
 * Clears this app's account and league data when the signed-in account ends without "Sign out" (expiry, account
 * deletion) or changes on this browser (lib/local-data.ts reconcileAccount). `account` is the server's fingerprint of
 * the signed-in account, null when signed out, or undefined when the session could not be verified (nothing is
 * cleared then). When another tab signs this account out or in as someone else, a signed-in tab clears its own
 * sessionStorage (the other tab already cleared localStorage) and re-renders, so it stops showing or saving the old
 * account's data.
 */
export function AccountStorageGuard({ account }: { account: PageAccount }) {
  const router = useRouter();
  useEffect(() => {
    reconcileAccount(local, session, account);
    if (!account) return;
    const changed = (event: StorageEvent) => {
      if (event.key !== ACCOUNT_MARKER_KEY && event.key !== null) return;
      if (event.key !== null && event.newValue === account) return;
      purgeOwnedKeys([session]);
      router.refresh();
    };
    window.addEventListener("storage", changed);
    return () => window.removeEventListener("storage", changed);
  }, [account, router]);
  return null;
}
