import type { ReactNode } from "react";
import { Notice } from "@/components/ui";
import type { ApiErrorKind } from "@/lib/api";
import styles from "./BackendProblem.module.css";

/** A terminal command block. Paths are relative to the repository's `fantasy-intel/` folder. */
function Commands({ children }: { children: string }) {
  return <pre className={styles.commands}><code>{children}</code></pre>;
}

const START_BACKEND = "cd backend\nuv run uvicorn app.main:app --reload";
const SYNC = "cd backend\nuv run python ../scripts/sync_nfl.py\nuv run python ../scripts/sync_espn.py";

function FirstRun() {
  return (
    <>
      <p className={styles.firstRun}>
        <b>First run?</b> Copy <code>.env.example</code> to <code>.env</code> and fill it in, then import your league. Both syncs are safe to repeat.
      </p>
      <Commands>{SYNC}</Commands>
    </>
  );
}

/**
 * Explains a failed backend call and what to do about it, by `kind` (see `ApiErrorKind`).
 * Pass the fields of a failed `api()` result. Safe in server and client components.
 */
export function BackendProblem({ error, kind = "unknown", status }: {
  error: string;
  kind?: ApiErrorKind;
  status?: number;
}) {
  let title: string;
  let body: ReactNode;

  if (kind === "offline") {
    title = "Can't reach the backend";
    body = (
      <>
        <p>The dashboard reads everything from the local API, and it isn&apos;t answering. Start it in a terminal, then reload this page.</p>
        <Commands>{START_BACKEND}</Commands>
        <p className={styles.detail}>{error}</p>
        <FirstRun />
      </>
    );
  } else if (kind === "schema") {
    title = "The database schema is out of date";
    body = (
      <>
        <p>{error}</p>
        <p>Run both syncs to migrate and rebuild, then reload this page:</p>
        <Commands>{SYNC}</Commands>
      </>
    );
  } else if (kind === "http") {
    title = status ? `The backend returned an error (${status})` : "The backend returned an error";
    body = (
      <>
        <p>{error}</p>
        {/no database yet/i.test(error) && <FirstRun />}
        {/busy/i.test(error) && <p>A sync usually holds the database for a few seconds. Reload shortly.</p>}
      </>
    );
  } else {
    title = "Something went wrong";
    body = <p>{error}</p>;
  }

  return <Notice tone="negative" title={title}>{body}</Notice>;
}
