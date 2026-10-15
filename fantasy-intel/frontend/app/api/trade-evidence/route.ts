import { api } from "@/lib/api";
import { evidencePath } from "@/lib/trade-discovery";
import type { DEvidence } from "@/lib/trade-types";

/**
 * One trade's evidence for the discovery table, loaded when its "Why this trade" panel opens. The browser cannot
 * reach the backend (API_URL is server-only), so this proxies `GET /api/trades/discover/evidence` through `api()`,
 * which attaches the hosted-mode API key on the server. Only the discovery query parameters and the row key are
 * forwarded. The site password gate in `proxy.ts` covers this route like every page.
 */
export async function GET(request: Request) {
  const path = evidencePath(new URL(request.url).searchParams);
  if (!path) return Response.json({ error: "A row key is required." }, { status: 400 });
  const res = await api<DEvidence>(path);
  if (!res.ok) return Response.json({ error: res.error, kind: res.kind ?? "unknown" }, { status: res.status ?? 502 });
  return Response.json(res.data);
}
