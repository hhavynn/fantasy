import "server-only";
import { devUserId, encodeUserAssertion, usableSecret, ASSERTION_HEADER } from "./user-assertion";

// Server-only identity for backend calls. Nothing exported here returns the secret, and the only thing
// that leaves is the short-lived header value, which `apiRequest` puts on a server-to-backend request.
// Never pass either into props, HTML, cookies or browser storage.

/**
 * The signed-in user's id, or null. There is no identity provider yet, so this is null in production;
 * in development only, FANTASY_DEV_USER_ID stands in so the flow can be exercised end to end.
 * Async so a real session lookup (cookies, headers) can replace the body without changing callers.
 */
export async function currentUserId(): Promise<string | null> {
  return devUserId(process.env.NODE_ENV, process.env.FANTASY_DEV_USER_ID);
}

/** True when this server can sign assertions (a secret the backend would accept is configured). */
export function canSignAssertions(): boolean {
  return usableSecret(process.env.FANTASY_USER_ASSERTION_SECRET) !== null;
}

/** Headers that identify `userId` to the backend, or null when no usable secret is configured. */
export function userAssertionHeaders(userId: string): Record<string, string> | null {
  const secret = usableSecret(process.env.FANTASY_USER_ASSERTION_SECRET);
  if (!secret) return null;
  return { [ASSERTION_HEADER]: encodeUserAssertion({ sub: userId, secret }) };
}
