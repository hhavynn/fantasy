import { Section, Skeleton } from "@/components/ui";
import { buildActionItems, type LeagueInput, type RosterInput, type StartSitInput, type WaiverInput } from "@/lib/action-items";
import type { ApiResult } from "@/lib/api";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { currentLeague } from "@/lib/league-scope.server";
import type { Matchup } from "@/lib/matchup-types";
import type { ProjectionSource } from "@/lib/projection-source";
import { signalsOf, type ProvenancePayload, type WithProvenance } from "@/lib/signal-provenance";
import { buildWeeklyHub, type DecisionContext } from "@/lib/weekly-decisions";
import { ActionItems, NextStep } from "./ActionItems";
import { MatchupCard } from "./MatchupCard";
import styles from "./home.module.css";

/** What the band reads from /api/overview, which the page has already loaded (null when it failed). */
export type OverviewInput = { roster: RosterInput[]; league: LeagueInput & { current_week: number }; provenance?: ProvenancePayload | null };

/**
 * The weekly decision hub: the one next step (a Verdict), then the matchup card beside the other decisions grouped by
 * area. It waits on three endpoints the rest of the Overview does not need, so the page renders it inside a Suspense
 * boundary and the header, stat strip and roster stream first. Each source failing degrades only its own part (see
 * `buildActionItems`), and every decision carries the provenance of the payload it came from (`buildWeeklyHub`).
 */
export async function WeekBand({ matchup, startSit, waivers, overview, team, projection }: {
  matchup: Promise<ApiResult<Matchup>>;
  startSit: Promise<ApiResult<StartSitInput & WithProvenance>>;
  waivers: Promise<ApiResult<{ players: WaiverInput[]; total: number } & WithProvenance>>;
  overview: OverviewInput | null;
  /** The analyzed team (`?team=`), null for the owner's default. */
  team: string | null;
  projection: ProjectionSource;
}) {
  const [m, s, w, profile, league] = await Promise.all([matchup, startSit, waivers, getLeagueProfile(), currentLeague()]);
  const week = m.ok ? m.data.week : s.ok ? s.data.scoring_period : overview?.league.current_week ?? null;
  const ctx: DecisionContext = { league: league?.id ?? null, team, week, projectionSource: projection };
  const actions = buildActionItems({
    provider: profile.provider,
    roster: overview?.roster ?? null,
    league: overview?.league ?? null,
    matchup: m.ok ? m.data : null,
    startSit: s.ok ? s.data : null,
    waivers: w.ok ? w.data : null,
  });
  const hub = buildWeeklyHub(actions, ctx, {
    overview: signalsOf(overview?.provenance),
    matchup: m.ok ? signalsOf(m.data.provenance) : [],
    startSit: s.ok ? signalsOf(s.data.provenance) : [],
    waivers: w.ok ? signalsOf(w.data.provenance) : [],
  });
  return (
    <div className={styles.hub}>
      <NextStep hub={hub} />
      <div className={styles.band}>
        <MatchupCard result={m} ctx={ctx} />
        <ActionItems hub={hub} />
      </div>
    </div>
  );
}

/** Same footprint as the hub while it loads. */
export function WeekBandSkeleton() {
  return (
    <div className={styles.hub} role="status" aria-busy="true" aria-label="Loading this week">
      <div className={styles.nextSkeleton}>
        <Skeleton w="20%" h={10} />
        <Skeleton w="55%" h={20} />
        <Skeleton w="40%" h={12} />
      </div>
      <div className={styles.band}>
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
    </div>
  );
}
