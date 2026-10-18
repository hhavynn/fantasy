import Link from "next/link";
import { BackendProblem } from "@/components/BackendProblem";
import { EmptyState, PageHeader, Section } from "@/components/ui";
import type { ApiErrorKind } from "@/lib/api";

export const QUESTION = "Which rosters are strongest right now, and who moved since last week?";

export function Unavailable({ error, kind, status }: { error: string; kind?: ApiErrorKind; status?: number }) {
  // The backend answers 503 when the league has not been synced yet; anything else is a backend problem.
  const notSynced = status === 503 && kind === "http" && !/no database yet/i.test(error);
  return (
    <div className="stack">
      <PageHeader eyebrow="League" title="Power rankings" meta={QUESTION} />
      {notSynced ? (
        <Section>
          <EmptyState title="Your league isn't synced yet" action={<Link href="/data">Check data status</Link>}>
            <p>Power rankings need your league&apos;s rosters, ESPN projections and matchups. Run the ESPN sync, then reload this page.</p>
            <p className="faint">{error}</p>
          </EmptyState>
        </Section>
      ) : <BackendProblem error={error} kind={kind} status={status} />}
    </div>
  );
}
