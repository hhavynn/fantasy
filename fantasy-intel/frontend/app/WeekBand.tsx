import { Section, Skeleton } from "@/components/ui";
import { buildActionItems, type StartSitInput, type WaiverInput } from "@/lib/action-items";
import type { ApiResult } from "@/lib/api";
import { getLeagueProfile } from "@/lib/league-profile.server";
import type { Matchup } from "@/lib/matchup-types";
import { mergeSignals, type WithProvenance } from "@/lib/signal-provenance";
import type { RosterPlayer } from "@/lib/types";
import { ActionItems } from "./ActionItems";
import { MatchupCard } from "./MatchupCard";
import styles from "./home.module.css";

/**
 * The "This week" band: matchup card beside the action list. It waits on three endpoints the rest of the
 * Overview does not need, so the page renders inside a Suspense boundary and the header, stat strip and
 * roster stream first. Each source failing degrades only its own part (see `buildActionItems`).
 */
export async function WeekBand({ matchup, startSit, waivers, roster, fallbackWeek }: {
  matchup: Promise<ApiResult<Matchup>>;
  startSit: Promise<ApiResult<StartSitInput & WithProvenance>>;
  waivers: Promise<ApiResult<{ players: WaiverInput[]; total: number } & WithProvenance>>;
  roster: RosterPlayer[] | null;
  fallbackWeek: number | null;
}) {
  const [m, s, w, profile] = await Promise.all([matchup, startSit, waivers, getLeagueProfile()]);
  const actions = buildActionItems({
    provider: profile.provider,
    roster,
    matchup: m.ok ? m.data : null,
    startSit: s.ok ? s.data : null,
    waivers: w.ok ? w.data : null,
  });
  const week = m.ok ? m.data.league.current_week : fallbackWeek;
  // The sources behind the action list, each signal once (the lineup, projections, injury reports, waiver analysis).
  const provenance = mergeSignals(m.ok ? m.data.provenance : null, s.ok ? s.data.provenance : null, w.ok ? w.data.provenance : null);
  return (
    <div className={styles.band}>
      <MatchupCard result={m} />
      <ActionItems list={actions} week={week} provenance={provenance} />
    </div>
  );
}

/** Same footprint as the band while it loads. */
export function WeekBandSkeleton() {
  return (
    <div className={styles.band} role="status" aria-busy="true" aria-label="Loading this week">
      <Section title="This week">
        <div className={styles.skeletonStack}>
          <Skeleton w="40%" h={12} />
          <Skeleton w="100%" h={36} radius={4} />
          <Skeleton w="100%" h={36} radius={4} />
          <Skeleton w="60%" h={14} />
        </div>
      </Section>
      <Section title="Before kickoff">
        <div className={styles.skeletonStack}>
          <Skeleton w="70%" h={14} />
          <Skeleton w="90%" h={12} />
          <Skeleton w="55%" h={12} />
        </div>
      </Section>
    </div>
  );
}
