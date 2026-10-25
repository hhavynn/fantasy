// Browser storage this app keeps per league or per account, cleared at sign-out (STATUS 1t) so the next account on the
// same browser never sees the previous one's data. Only keys this app owns and that hold account or league data are
// touched, by exact prefix or exact key; never `clear()`, never another key on the origin.
//
// Cleared at sign-out, and on the first page after the signed-in account ends without one (expiry, deletion) or
// changes (another account signs in on this browser): see reconcileAccount. Cleared: saved views, palette recents (and the legacy browser-wide list), the waiver shortlist, the claim plan, trade
// scenarios and the scenario being edited, the recently viewed leagues (ids and names), and the per-tab team choice and
// tab memory (team ids and league pages). Kept: device preferences that name no account or league, the theme
// (`fantasy-intel:theme`) and the collapsed rail (`fantasy-intel:rail`). No React and no `@/` imports, so node --test
// covers it. (The last-viewed league cookie, fi_league, is deleted by the sign-out and delete-account actions.)
import { hasSessionCookie } from "./auth-config.ts";

/** Every per-league store's key prefix (each module's key builder: lib/saved-views.ts, lib/palette.ts, lib/waiver-shortlist.ts,
 *  lib/claim-plan.ts, lib/trade-scenarios.ts). lib/local-data.test.ts checks each builder's keys start with one of them. */
export const OWNED_PREFIXES = [
  "fi:saved-views:v1:",
  "fi:palette-recent:v1:",
  "fi:waiver-shortlist:v1:",
  "fi:waiver-claim-plan:v1:",
  "fi:trade-scenarios:v1:",
  "fi:trade-scenario-editing:v1:",
  // The analyzed team per league, per tab (components/useTeam.ts teamStorageKey).
  "fantasy-intel:team:",
] as const;

/**
 * Which account this browser's data belongs to: an opaque fingerprint of the signed-in account (never its id or email),
 * written while signed in, so a later page that is signed out or signed in as someone else knows to clear.
 */
export const ACCOUNT_MARKER_KEY = "fantasy-intel:account";

/** Exact owned keys that hold account or league data. */
export const OWNED_KEYS = [
  ACCOUNT_MARKER_KEY,
  "fantasy-intel:palette-recent",   // the browser-wide palette recents before #200 (player holders, statuses)
  "fantasy-intel:leagues",          // recently viewed leagues: ids and names (lib/league-switcher.ts)
  "fantasy-intel:team",             // the analyzed team on the classic site, per tab (components/useTeam.ts)
  "fantasy-intel:tab-memory",       // the last page per nav group, per tab, with its league and team (components/Nav.tsx)
] as const;

/** Device preferences this app owns that name no account or league: deliberately kept at sign-out. */
export const KEPT_PREFERENCES = ["fantasy-intel:theme", "fantasy-intel:rail"] as const;

export function isOwnedKey(key: string | null): key is string {
  return key !== null && (OWNED_PREFIXES.some((p) => key.startsWith(p)) || (OWNED_KEYS as readonly string[]).includes(key));
}

type StorageLike = Pick<Storage, "key" | "removeItem"> & { readonly length: number };

/**
 * Remove this app's owned keys from each storage; anything else is left alone. Never throws: blocked or failing storage
 * (private windows, site settings) is skipped, so sign-out always proceeds. Returns how many keys were removed.
 */
export function purgeOwnedKeys(storages: readonly (() => StorageLike | null | undefined)[]): number {
  let removed = 0;
  for (const get of storages) {
    try {
      const storage = get();
      if (!storage) continue;
      // Collect first: removing while indexing would skip keys.
      const keys: string[] = [];
      for (let i = 0; i < storage.length; i++) {
        const k = storage.key(i);
        if (isOwnedKey(k)) keys.push(k);
      }
      for (const k of keys) {
        try {
          storage.removeItem(k);
          removed++;
        } catch {
          // One key that cannot be removed does not stop the rest.
        }
      }
    } catch {
      // Storage unavailable: nothing to clear here.
    }
  }
  return removed;
}

type MarkerStorage = StorageLike & Pick<Storage, "getItem" | "setItem">;

/** The account a page was rendered for: its fingerprint, null when signed out, undefined when it is not known. */
export type PageAccount = string | null | undefined;

/**
 * What the account guard is told on a page with no verified user. With a Supabase session cookie still present the
 * session could not be verified (Supabase unreachable, its keys not fetched yet): unknown, so nothing is purged. A
 * real sign-out, expiry or revocation removes the cookies, so the next page is signed out (null) and purges.
 */
export function signedOutAccount(cookieNames: readonly string[]): null | undefined {
  return hasSessionCookie(cookieNames) ? undefined : null;
}

/**
 * Called on every page with the account it was rendered for (PageAccount). When this browser's data belongs to another
 * account, or to one that is no longer signed in, it is purged first; then it is marked as this account's. Data
 * written while never signed in has no marker and is left alone; an unknown account changes nothing. Never throws.
 * Returns whether anything was purged.
 */
export function reconcileAccount(
  local: () => MarkerStorage | null | undefined, session: () => StorageLike | null | undefined, account: PageAccount,
): boolean {
  if (account === undefined) return false;
  let store: MarkerStorage | null | undefined;
  let owner: string | null;
  try {
    store = local();
    if (!store) return false;
    owner = store.getItem(ACCOUNT_MARKER_KEY);
  } catch {
    return false;
  }
  const purge = owner !== null && owner !== account;
  if (purge) purgeOwnedKeys([() => store, session]);
  if (account !== null && owner !== account) {
    try { store.setItem(ACCOUNT_MARKER_KEY, account); } catch { /* unmarked: the sign-out button still purges */ }
  }
  return purge;
}
