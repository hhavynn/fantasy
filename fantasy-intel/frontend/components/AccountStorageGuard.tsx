"use client";

import { useEffect } from "react";
import { ACCOUNT_MARKER_KEY, purgeOwnedKeys, reconcileAccount } from "@/lib/local-data";

const local = () => window.localStorage;
const session = () => window.sessionStorage;

/**
 * Clears this app's account and league data when the signed-in account ends without "Sign out" (expiry, account
 * deletion) or changes on this browser (lib/local-data.ts reconcileAccount). `account` is the server's fingerprint of
 * the signed-in account, or null when signed out. When another tab changes the account, this tab clears its own
 * sessionStorage too; the tab that saw the change already cleared localStorage.
 */
export function AccountStorageGuard({ account }: { account: string | null }) {
  useEffect(() => {
    reconcileAccount(local, session, account);
    const changed = (event: StorageEvent) => {
      if (event.key !== ACCOUNT_MARKER_KEY && event.key !== null) return;
      if (event.key !== null && event.newValue === account) return;
      purgeOwnedKeys([session]);
    };
    window.addEventListener("storage", changed);
    return () => window.removeEventListener("storage", changed);
  }, [account]);
  return null;
}
