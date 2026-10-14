// Server-side fetch helper for the local FastAPI backend.
const API_URL = process.env.API_URL ?? "http://127.0.0.1:8000";

export type ApiResult<T> = { ok: true; data: T; error: null } | { ok: false; data: null; error: string };

export async function api<T>(path: string): Promise<ApiResult<T>> {
  try {
    const res = await fetch(`${API_URL}${path}`, { cache: "no-store" });
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        if (body?.detail) detail = String(body.detail);
      } catch {}
      return { ok: false, data: null, error: detail };
    }
    return { ok: true, data: (await res.json()) as T, error: null };
  } catch {
    return {
      ok: false,
      data: null,
      error: `Cannot reach the backend at ${API_URL}. Start it with: cd backend && uv run uvicorn app.main:app --reload`,
    };
  }
}
