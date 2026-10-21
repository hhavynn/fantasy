import { BackendProblem } from "@/components/BackendProblem";
import { Section, Skeleton, SkeletonStatStrip, SkeletonTable } from "@/components/ui";
import type { ApiResult } from "@/lib/api";
import { getLeagueProfile } from "@/lib/league-profile.server";
import type { TradeResultV3 } from "@/lib/trade-types";
import { TradeResultView } from "./TradeResult";
import styles from "./builder.module.css";

/**
 * The evaluation for the URL selection. The exact lineup simulation takes a moment, so the page renders
 * this inside a Suspense boundary: the builder is usable while the result streams in.
 */
export async function EvaluatedTrade({ result }: { result: Promise<ApiResult<TradeResultV3>> }) {
  const [r, profile] = await Promise.all([result, getLeagueProfile()]);
  if (!r.ok) return <BackendProblem error={r.error} kind={r.kind} status={r.status} />;
  return <TradeResultView r={r.data} provider={profile.provider} />;
}

/** Placeholder for the result while the simulation runs. */
export function EvaluatedTradeSkeleton() {
  return (
    <div className={styles.resultSkeleton} role="status" aria-busy="true" aria-label="Evaluating the trade">
      <SkeletonStatStrip n={4} />
      <Section title={<Skeleton w={160} h={14} />} flush>
        <SkeletonTable rows={4} cols={4} />
      </Section>
    </div>
  );
}
