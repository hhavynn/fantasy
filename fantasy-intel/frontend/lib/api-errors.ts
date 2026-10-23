// Pure helpers about backend failures, importable from client components (lib/api.ts is server-side).

/**
 * What went wrong, so the UI can say something useful:
 * - offline: the backend did not answer (not running, wrong port)
 * - schema: the backend answered 503 because the database schema is older than the code
 * - http: the backend answered with an error status (detail is the backend's own message)
 * - unknown: anything else (e.g. a response that is not JSON)
 */
export type ApiErrorKind = "offline" | "schema" | "http" | "unknown";

/** True when the backend says no league has been imported yet (a first run): a setup step, not a failure. */
export function isFirstRun(detail: string | null | undefined): boolean {
  return !!detail && /no database yet/i.test(detail);
}

/** True for a league whose first sync has not published yet: the API's 409 "First sync in progress." on league routes. */
export function isFirstSync(status: number | undefined, detail: string | null | undefined): boolean {
  return status === 409 && !!detail && /first sync in progress/i.test(detail);
}

/**
 * A hosted deployment (on Vercel, or a dashboard holding the backend's service key, README "Hosting") rather than a
 * local run. Hosted visitors never see the internal backend address or the start-the-server instructions. Pass the
 * two variables explicitly: in a client bundle both are undefined (a local run's wording).
 */
export function isHostedDeployment(env: { API_KEY?: string; VERCEL?: string }): boolean {
  return Boolean(env.API_KEY || env.VERCEL);
}
