// Encoding of the end-user assertion the backend expects in `X-Fantasy-User-Assertion`:
//   v1.<b64url(payload json)>.<b64url(hmac_sha256(secret, "v1." + b64url(payload)))>
// payload = {"sub": user id, "aud": "fantasy-intel-api", "iat": seconds, "exp": seconds}, exp - iat <= 600.
// Base64url has no padding. This module is pure (node:crypto only, no Next imports) so it can be unit
// tested; it takes the secret as an argument and never reads the environment. Server code gets the
// secret through `user-assertion.server.ts`, which is the only importer allowed to touch it.
import { createHmac } from "node:crypto";

export const ASSERTION_VERSION = "v1";
export const ASSERTION_AUDIENCE = "fantasy-intel-api";
export const ASSERTION_HEADER = "X-Fantasy-User-Assertion";
/** The backend treats a secret shorter than this as unset. */
export const MIN_SECRET_LENGTH = 32;
/** The backend rejects assertions that live longer than this. */
export const MAX_TTL_SECONDS = 600;
/** Short on purpose: one is minted per backend call. */
export const DEFAULT_TTL_SECONDS = 300;

// Same shape the backend accepts for a principal (app/leagues/context.py).
const USER_ID = /^[A-Za-z0-9_.:@|-]{1,128}$/;

export function isValidUserId(value: unknown): value is string {
  return typeof value === "string" && USER_ID.test(value);
}

/** The secret if the backend would accept it, else null (unset, blank or too short). */
export function usableSecret(secret: string | undefined | null): string | null {
  return typeof secret === "string" && secret.length >= MIN_SECRET_LENGTH ? secret : null;
}

/** The development-only user id: never in production, never when unset or malformed. */
export function devUserId(nodeEnv: string | undefined, configured: string | undefined | null): string | null {
  if (nodeEnv === "production") return null;
  const id = configured?.trim();
  return isValidUserId(id) ? id : null;
}

/** Base64url without padding. */
export function b64url(input: string | Uint8Array): string {
  return Buffer.from(input).toString("base64url");
}

export type EncodeOptions = {
  sub: string;
  secret: string;
  /** Seconds since the epoch; defaults to now. */
  now?: number;
  ttlSeconds?: number;
};

/** Build the header value. Throws on bad input; messages never include the secret. */
export function encodeUserAssertion({ sub, secret, now, ttlSeconds = DEFAULT_TTL_SECONDS }: EncodeOptions): string {
  if (!isValidUserId(sub)) throw new RangeError("Malformed user id for the user assertion.");
  if (usableSecret(secret) === null) throw new RangeError(`The assertion secret must be at least ${MIN_SECRET_LENGTH} characters.`);
  if (!Number.isInteger(ttlSeconds) || ttlSeconds <= 0 || ttlSeconds > MAX_TTL_SECONDS) {
    throw new RangeError(`The assertion lifetime must be 1 to ${MAX_TTL_SECONDS} seconds.`);
  }
  const iat = now ?? Math.floor(Date.now() / 1000);
  const payload = b64url(JSON.stringify({ sub, aud: ASSERTION_AUDIENCE, iat, exp: iat + ttlSeconds }));
  const signed = `${ASSERTION_VERSION}.${payload}`;
  const signature = createHmac("sha256", secret).update(signed).digest("base64url");
  return `${signed}.${signature}`;
}
