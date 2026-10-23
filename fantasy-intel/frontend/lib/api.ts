// Server-side fetch helper for the local FastAPI backend (GET, or POST when a body is given).
import type { ApiErrorKind } from "./api-errors.ts";
import { DEGRADED_HEADER, recordTiming } from "./api-timing.ts";
import { LEAGUE_HEADER, scopeApiPath } from "./league-scope.ts";

export { isFirstRun, type ApiErrorKind } from "./api-errors.ts";

const API_URL = process.env.API_URL ?? "http://127.0.0.1:8000";

/**
 * The league this server request is scoped to (proxy.ts sets the header for /l/<lg>/... pages), or null. Outside a
 * Next request (unit tests) there are no request headers: unscoped.
 */
export async function requestLeague(): Promise<string | null> {
  try {
    const { headers } = await import("next/headers");
    return (await headers()).get(LEAGUE_HEADER);
  } catch {
    return null;
  }
}

/** `/api/...` paths go to the league's /v2 mount when the page is league-scoped; everything else is unchanged. */
export async function scopedPath(path: string): Promise<string> {
  return scopeApiPath(path, await requestLeague());
}
// Hosted mode: the backend only answers requests carrying its FANTASY_API_KEY.
const AUTH: Record<string, string> = process.env.API_KEY ? { "X-API-Key": process.env.API_KEY } : {};

/**
 * The signed-in visitor's Supabase access token for /v2 calls (the API verifies it and authorizes the league), or
 * nothing: legacy paths never carry identity, and signed out (or sign-in off, or a unit test) adds no header.
 */
async function userAuth(path: string): Promise<Record<string, string>> {
  if (!path.startsWith("/v2/")) return {};
  try {
    const { accessToken } = await import("./supabase/server");
    const token = await accessToken();
    return token ? { Authorization: `Bearer ${token}` } : {};
  } catch {
    return {};
  }
}

export type ApiResult<T> =
  /** `stale`: the backend served this league's previous files (it could not load the newest in time): data may be older. */
  | { ok: true; data: T; error: null; stale?: true }
  /** `code`: the route's machine-readable case (the X-FI-Error-Code header) when it names one. */
  | { ok: false; data: null; error: string; kind?: ApiErrorKind; status?: number; code?: string };

/** One timed backend call (API_TIMING; lib/api-timing.ts), or the thrown network error. */
async function timedFetch(path: string, init: RequestInit): Promise<Response> {
  const started = performance.now();
  try {
    const res = await fetch(`${API_URL}${path}`, init);
    recordTiming(init.method ?? "GET", path, res.status, performance.now() - started, res.headers.get("server-timing"));
    return res;
  } catch (err) {
    recordTiming(init.method ?? "GET", path, "offline", performance.now() - started);
    throw err;
  }
}

/** A route's machine-readable error case (e.g. the league analyzer's unknown_vs), beside its readable `detail`. */
export const ERROR_CODE_HEADER = "x-fi-error-code";

const staleMark = (res: Response): { stale?: true } => (res.headers.get(DEGRADED_HEADER) ? { stale: true } : {});

/** Classify a non-2xx backend response from its `detail` message (the stale-schema 503 names the database schema). */
export function classifyHttpError(detail: string): "schema" | "http" {
  return /database schema/i.test(detail) ? "schema" : "http";
}

export async function api<T>(path: string, body?: unknown): Promise<ApiResult<T>> {
  path = await scopedPath(path);
  const auth = { ...(await userAuth(path)), ...AUTH };
  const init: RequestInit = body === undefined
    ? { cache: "no-store", headers: auth }
    : { cache: "no-store", method: "POST", headers: { ...auth, "Content-Type": "application/json" }, body: JSON.stringify(body) };
  let res: Response;
  try {
    res = await timedFetch(path, init);
  } catch {
    return {
      ok: false,
      data: null,
      kind: "offline",
      error: `Cannot reach the backend at ${API_URL}. Start it with: cd backend && uv run uvicorn app.main:app --reload`,
    };
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const payload = await res.json();
      if (payload?.detail) detail = String(payload.detail);
    } catch {}
    const code = res.headers.get(ERROR_CODE_HEADER);
    return { ok: false, data: null, error: detail, kind: classifyHttpError(detail), status: res.status, ...(code ? { code } : {}) };
  }
  try {
    return { ok: true, data: (await res.json()) as T, error: null, ...staleMark(res) };
  } catch {
    return { ok: false, data: null, kind: "unknown", status: res.status, error: `The backend at ${API_URL} returned a response that is not valid JSON.` };
  }
}

export type ApiRequestOptions = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  /** Extra request headers (for example a user assertion). The service API key always wins over these. */
  headers?: Record<string, string>;
};

/** FastAPI `detail` is a string for most errors and a list of {msg} objects for request validation (422). */
export function formatDetail(detail: unknown): string | null {
  if (typeof detail === "string") return detail || null;
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => (typeof d === "string" ? d : typeof d?.msg === "string" ? d.msg : "")).filter(Boolean);
    return parts.length ? parts.join("; ") : null;
  }
  return null;
}

/**
 * The headers of an apiRequest: the caller's own, then the signed-in visitor's token, then the service API key (which
 * always wins). A caller that names its own Authorization (the email callback, holding a session the cookies may not
 * carry yet) keeps it: the visitor's token is not added over it.
 */
export function requestHeaders(explicit: Record<string, string>, user: Record<string, string>, service: Record<string, string>,
                               json: boolean): Record<string, string> {
  const ownAuth = Object.keys(explicit).some((k) => k.toLowerCase() === "authorization");
  return { ...explicit, ...(ownAuth ? {} : user), ...service, ...(json ? { "Content-Type": "application/json" } : {}) };
}

/**
 * Like `api()` but with any method and extra headers. A 204 (or other empty 2xx) answers `data: null`.
 * Same result shape and error kinds as `api()`, but `error` carries the backend's own `detail` text.
 */
export async function apiRequest<T = null>(path: string, options: ApiRequestOptions = {}): Promise<ApiResult<T>> {
  const { method = options.body === undefined ? "GET" : "POST", body, headers = {} } = options;
  path = await scopedPath(path);
  const init: RequestInit = {
    cache: "no-store",
    method,
    headers: requestHeaders(headers, await userAuth(path), AUTH, body !== undefined),
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  };
  let res: Response;
  try {
    res = await timedFetch(path, init);
  } catch {
    return {
      ok: false,
      data: null,
      kind: "offline",
      error: `Cannot reach the backend at ${API_URL}. Start it with: cd backend && uv run uvicorn app.main:app --reload`,
    };
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const found = formatDetail((await res.json())?.detail);
      if (found) detail = found;
    } catch {}
    return { ok: false, data: null, error: detail, kind: classifyHttpError(detail), status: res.status };
  }
  if (res.status === 204) return { ok: true, data: null as T, error: null };
  try {
    return { ok: true, data: (await res.json()) as T, error: null };
  } catch {
    return { ok: false, data: null, kind: "unknown", status: res.status, error: `The backend at ${API_URL} returned a response that is not valid JSON.` };
  }
}
