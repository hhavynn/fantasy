"use client";

import Link from "@/components/LeagueLink";
import { useEffect } from "react";
import { BackendProblemView } from "@/components/BackendProblemView";
import { Notice, PageHeader } from "@/components/ui";
import type { ApiErrorKind } from "@/lib/api";
import styles from "./route-state.module.css";

/** Production builds redact server error messages, so classify from the text when it survives. */
function kindOf(message: string): ApiErrorKind {
  if (/cannot reach the backend/i.test(message)) return "offline";
  if (/database schema/i.test(message)) return "schema";
  return "unknown";
}

export default function RouteError({ error, retry }: { error: Error & { digest?: string }; retry: () => void }) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  const kind = kindOf(error.message);
  return (
    <div className="stack">
      <PageHeader title="Something went wrong" meta="This page failed to load. Nothing was changed; the dashboard is read-only." />
      {kind === "unknown" ? (
        <Notice tone="negative" title="This page couldn't be rendered.">
          <p>{error.message}</p>
          {error.digest ? <p className={styles.digest}>Reference {error.digest}</p> : null}
        </Notice>
      ) : (
        <BackendProblemView kind={kind} error={error.message} />
      )}
      <div className={styles.actions}>
        <button type="button" onClick={() => retry()}>Try again</button>
        <Link href="/data">Open Data quality</Link>
      </div>
    </div>
  );
}
