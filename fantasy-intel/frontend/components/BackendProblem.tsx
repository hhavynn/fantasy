import "server-only";
import type { ApiErrorKind } from "@/lib/api-errors";
import { isLocalRun } from "@/lib/deployment.server";
import { BackendProblemView } from "./BackendProblemView";

export { FirstRunGuide } from "./BackendProblemView";

/**
 * Explains a failed backend call in a server component: a local run shows the developer the backend's error and how
 * to start or sync it; a hosted deployment's visitors get plain words (lib/backend-problem.ts). Client components
 * render `BackendProblemView` instead, which shows the visitor wording since only the server knows it is local.
 */
export function BackendProblem({ error, kind = "unknown", status }: { error: string; kind?: ApiErrorKind; status?: number }) {
  return <BackendProblemView error={error} kind={kind} status={status} local={isLocalRun()} />;
}
