// Server-side fetch helper for the local FastAPI backend (GET, or POST when a body is given).
const API_URL = process.env.API_URL ?? "http://127.0.0.1:8000";

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
    ? { cache: "no-store" }
    : { cache: "no-store", method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
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
