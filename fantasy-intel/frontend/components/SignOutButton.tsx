"use client";

import { purgeOwnedKeys } from "@/lib/local-data";

/**
 * The account menu's "Sign out" submit button. Before the form's sign-out action runs, it clears this app's per-league
 * browser data (saved views, palette recents, waiver shortlist, claim plan, trade scenarios; lib/local-data.ts) so the
 * next account on this browser starts clean. The purge never throws, so blocked storage cannot stop the sign-out.
 */
export function SignOutButton() {
  return (
    <button type="submit" role="menuitem" className="account-signout"
      onClick={() => purgeOwnedKeys([() => window.localStorage, () => window.sessionStorage])}>
      Sign out
    </button>
  );
}
