import "server-only";
import { isHostedDeployment } from "./api-errors";

/**
 * True for a local run (no API_KEY and not on Vercel), read on the server where both variables are real. Local-only
 * developer help (backend error text, the start-the-server and sync commands) shows only when this says so. A client
 * bundle sees neither variable, so client code never decides this itself; `server-only` keeps this module out of it.
 */
export function isLocalRun(): boolean {
  return !isHostedDeployment({ API_KEY: process.env.API_KEY, VERCEL: process.env.VERCEL });
}
