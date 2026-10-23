// What BackendProblem (components/BackendProblem.tsx) says about a failed backend call, as data so node's test runner
// covers it. Safe by default: the backend's raw text, the first-run setup guide, the start-the-server command and the
// sync commands appear only when `local` is true, which only the server wrapper sets (from lib/deployment.server.ts).
// A render without the flag (a client component such as app/error.tsx, or a hosted deployment) gets fixed words, plus
// the backend's own message for a 404, 409 or 422 (the statuses lib/route-error.ts forwards).
import { isFirstRun, isFirstSync, type ApiErrorKind } from "./api-errors.ts";

export const START_BACKEND = "cd backend\nuv run uvicorn app.main:app --reload";
export const SYNC = "cd backend\nuv run python ../scripts/sync_nfl.py\nuv run python ../scripts/sync_espn.py";

export type ProblemBlock =
  | { type: "text"; text: string }
  | { type: "commands"; text: string }
  | { type: "detail"; text: string }
  | { type: "first-run" };

export type ProblemCopy =
  | { variant: "first-run" }
  | { variant: "first-sync" }
  | { variant: "notice"; title: string; blocks: ProblemBlock[] };

export type ProblemInput = { error: string; kind?: ApiErrorKind; status?: number; local?: boolean };

const SHOWN_DETAIL = new Set([404, 409, 422]);
const BUSY = /database is busy/i;
const text = (t: string): ProblemBlock => ({ type: "text", text: t });

export function backendProblemCopy({ error, kind = "unknown", status, local = false }: ProblemInput): ProblemCopy {
  // The first-run guide is operator setup (.env, cookie settings, sync commands): a local run only. A hosted backend
  // without a database is a 503 like any other for visitors. The first-sync notice is fixed visitor copy.
  if (local && kind === "http" && isFirstRun(error)) return { variant: "first-run" };
  if (isFirstSync(status, error)) return { variant: "first-sync" };
  return local ? localCopy(error, kind, status) : visitorCopy(error, kind, status);
}

/** A local run: the developer sees the error and the command that fixes it. */
function localCopy(error: string, kind: ApiErrorKind, status: number | undefined): ProblemCopy {
  if (kind === "offline") {
    return { variant: "notice", title: "Can't reach the backend", blocks: [
      text("The dashboard reads everything from the local API, and it isn't answering. Start it in a terminal, then reload this page."),
      { type: "commands", text: START_BACKEND }, { type: "detail", text: error }, { type: "first-run" },
    ] };
  }
  if (kind === "schema") {
    return { variant: "notice", title: "The database schema is out of date", blocks: [
      text(error), text("Run both syncs to migrate and rebuild, then reload this page:"), { type: "commands", text: SYNC },
    ] };
  }
  if (kind === "http") {
    const blocks = [text(error)];
    if (/busy/i.test(error)) blocks.push(text("A sync usually holds the database for a few seconds. Reload shortly."));
    return { variant: "notice", title: status ? `The backend returned an error (${status})` : "The backend returned an error", blocks };
  }
  return { variant: "notice", title: "Something went wrong", blocks: [text(error)] };
}

/** Everyone else: no address, host, command, stack or backend text beyond a 404/409/422 message. */
function visitorCopy(error: string, kind: ApiErrorKind, status: number | undefined): ProblemCopy {
  if (kind === "offline") {
    return { variant: "notice", title: "The league data is not answering",
      blocks: [text("The dashboard could not load its data. Try again in a minute.")] };
  }
  if (kind === "http" && BUSY.test(error)) {
    return { variant: "notice", title: "The league data is busy",
      blocks: [text("A sync usually holds the data for a few seconds. Reload shortly.")] };
  }
  if (kind === "http" && status !== undefined && SHOWN_DETAIL.has(status)) {
    return { variant: "notice", title: "This could not be loaded", blocks: [text(error)] };
  }
  return { variant: "notice", title: "The league data is not available right now",
    blocks: [text("The dashboard could not load this page's data. Try again in a few minutes.")] };
}
