import { api } from "@/lib/api";
import { tradeEvidenceRouteResponse } from "@/lib/api-routes";

/**
 * One trade's evidence for the discovery table, loaded when its "Why this trade" panel opens. The browser cannot
 * reach the backend (API_URL is server-only), so this proxies `GET /api/trades/discover/evidence` through `api()`,
 * which attaches the hosted-mode API key on the server. Only the discovery query parameters and the row key are
 * forwarded, and backend errors are not passed through verbatim (lib/api-routes.ts, lib/route-error.ts). The site
 * password gate in `proxy.ts` covers this route like every page.
 */
export async function GET(request: Request) {
  return tradeEvidenceRouteResponse(new URL(request.url).searchParams, (path) => api(path));
}
