// The key and hash behind the one-minute repeat guard on email-link requests (lib/email-link.server.ts, the
// fi_link_recent cookie). Server only (node:crypto); no Next, so node --test covers it.
//
// The cookie must not let anyone who sees it test guesses of the address, so the hash is keyed: an HMAC with
// LINK_GUARD_SECRET, else with the hosted-mode API_KEY the server already keeps. Neither set (a local run): no key, and
// the guard is switched off rather than falling back to a plain, guessable hash. The guard is a convenience; Supabase's
// own answers are already the same for every address (lib/auth-flow.ts linkOutcome).

import { createHmac } from "node:crypto";

/** The guard's key: LINK_GUARD_SECRET, else API_KEY; at least 16 characters, else none (guard off). */
export function linkGuardSecret(env: Record<string, string | undefined>): string | null {
  for (const name of ["LINK_GUARD_SECRET", "API_KEY"] as const) {
    const value = (env[name] ?? "").trim();
    if (value.length >= 16) return value;
  }
  return null;
}

/** HMAC-SHA256 of the normalized address under the guard's key, as 32 hex characters. */
export function linkGuardHash(email: string, secret: string): string {
  return createHmac("sha256", secret).update(`fi_link_recent:${email.trim().toLowerCase()}`).digest("hex").slice(0, 32);
}
