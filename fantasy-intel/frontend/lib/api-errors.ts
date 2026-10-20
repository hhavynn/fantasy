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
