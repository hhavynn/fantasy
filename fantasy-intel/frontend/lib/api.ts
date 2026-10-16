// Server-side fetch helper for the local FastAPI backend (GET, or POST when a body is given).
const API_URL = process.env.API_URL ?? "http://127.0.0.1:8000";
// Hosted mode: the backend only answers requests carrying its FANTASY_API_KEY.
const AUTH: Record<string, string> = process.env.API_KEY ? { "X-API-Key": process.env.API_KEY } : {};

/**
 * What went wrong, so the UI can say something useful:
 * - offline: the backend did not answer (not running, wrong port)
 * - schema: the backend answered 503 because the database schema is older than the code
 * - http: the backend answered with an error status (detail is the backend's own message)
 * - unknown: anything else (e.g. a response that is not JSON)
 */
export type ApiErrorKind = "offline" | "schema" | "http" | "unknown";

export type ApiResult<T> =
  | { ok: true; data: T; error: null }
  | { ok: false; data: null; error: string; kind?: ApiErrorKind; status?: number };

/** Classify a non-2xx backend response from its `detail` message (the stale-schema 503 names the database schema). */
export function classifyHttpError(detail: string): "schema" | "http" {
  return /database schema/i.test(detail) ? "schema" : "http";
}

export async function api<T>(path: string, body?: unknown): Promise<ApiResult<T>> {
  const init: RequestInit = body === undefined
    ? { cache: "no-store", headers: AUTH }
    : { cache: "no-store", method: "POST", headers: { ...AUTH, "Content-Type": "application/json" }, body: JSON.stringify(body) };
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, init);
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
    return { ok: false, data: null, error: detail, kind: classifyHttpError(detail), status: res.status };
  }
  try {
    return { ok: true, data: (await res.json()) as T, error: null };
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
 * Like `api()` but with any method and extra headers. A 204 (or other empty 2xx) answers `data: null`.
 * Same result shape and error kinds as `api()`, but `error` carries the backend's own `detail` text.
 */
export async function apiRequest<T = null>(path: string, options: ApiRequestOptions = {}): Promise<ApiResult<T>> {
  const { method = options.body === undefined ? "GET" : "POST", body, headers = {} } = options;
  const init: RequestInit = {
    cache: "no-store",
    method,
    headers: {
      ...headers,
      ...AUTH,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  };
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, init);
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
