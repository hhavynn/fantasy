import Link from "@/components/LeagueLink";
import { Suspense } from "react";
import { BackendProblem } from "@/components/BackendProblem";
import { PageHeader, Section, EmptyState } from "@/components/ui";
import { api } from "@/lib/api";
import { providerName } from "@/lib/labels";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { currentLeague } from "@/lib/league-scope.server";
import { readProfileState } from "@/lib/player-tabs";
import { withSource } from "@/lib/projection-source";
import { getProjectionSource } from "@/lib/projection-source.server";
import type { ForwardProjection } from "@/lib/types";
import { rangeFor } from "@/lib/projection-ranges";
import { getRanges } from "@/lib/projection-ranges.server";
import { PlayerProfile } from "./PlayerProfile";
import { GameLogTab, MatchupsTab, OutlookSkeleton, OutlookTab, OverviewTab, UsageTab, type OutlookData } from "./ProfileTabs";
import { ResultsTable, SearchForm } from "./PlayerSearch";
import styles from "./players.module.css";
import type { Detail, PlayerListRow } from "./types";

export const metadata = { title: "Players" };

const LIMIT = 40;

export default async function PlayersPage({ searchParams }: PageProps<"/players">) {
  const sp = await searchParams;
  const q = typeof sp.q === "string" ? sp.q.trim() : "";
  // ?id= opens a profile; ?tab=, ?weeks= and ?cols= pick its tab, week window and game-log columns (lib/player-tabs.ts).
  const state = readProfileState(sp);
  const id = state?.id ?? null;
  const profile = await getLeagueProfile();

  if (state && id) {
    const detail = await api<Detail>(`/api/players/${id}`);
    if (!detail.ok) {
      return (
        <div className="stack">
          <PageHeader title="Players" actions={<SearchForm q={q} compact />} />
          {detail.status === 404 ? (
            <Section>
              <EmptyState title="Player not found" action={<Link href="/players">Back to player search</Link>}>
                No player in this league&apos;s {providerName(profile.provider)} pool has the id {id}.
              </EmptyState>
            </Section>
          ) : (
            <BackendProblem error={detail.error} kind={detail.kind} status={detail.status} />
          )}
        </div>
      );
    }
    const d = detail.data;
    const league = await currentLeague();
    const tab = state.tab;
    return (
      <PlayerProfile d={d} state={state} profile={profile} leagueId={league?.id ?? null}>
        {tab === "overview" && <OverviewTab d={d} state={state} profile={profile} />}
        {tab === "usage" && <UsageTab d={d} state={state} profile={profile} />}
        {tab === "game-log" && <GameLogTab d={d} state={state} profile={profile} />}
        {tab === "matchups" && <MatchupsTab d={d} state={state} profile={profile} />}
        {tab === "outlook" && (
          <Suspense fallback={<OutlookSkeleton />}>
            <OutlookTab d={d} profile={profile} load={loadOutlook(id, d)} />
          </Suspense>
        )}
      </PlayerProfile>
    );
  }

  const list = await api<PlayerListRow[]>(`/api/players?q=${encodeURIComponent(q)}&limit=${LIMIT}`);
  return (
    <div className="stack">
      <PageHeader
        eyebrow="Players"
        title="Players"
        meta="Usage beneath the box score: snaps, shares, expected points, red-zone work."
      >
        <SearchForm q={q} autoFocus />
        <p className={styles.hint}>
          Search by name; kickers and team defenses too. Open a player for week-by-week usage, the forward projection and the game log.
        </p>
        <dl className={styles.glossary}>
          <div><dt>PPR/g</dt><dd>Fantasy points scored per game</dd></div>
          <div><dt>xFP/g</dt><dd>Points an average player would expect from the same targets, carries and field position</dd></div>
          <div><dt>+/− xFP</dt><dd>The difference: production above or below what the role implies</dd></div>
        </dl>
      </PageHeader>
      {list.ok
        ? <ResultsTable rows={list.data} q={q} limit={LIMIT} provider={profile.provider} />
        : <BackendProblem error={list.error} kind={list.kind} status={list.status} />}
    </div>
  );
}

/**
 * The Outlook tab's forward view, fetched only when that tab is open. With the switch on "Ours", it comes from the
 * forward endpoint with source=independent (ESPN's series is unchanged in it, plus the `ours` block) with the player's
 * range; if that call fails the ESPN view from the profile stays.
 */
async function loadOutlook(id: string, d: Detail): Promise<OutlookData> {
  const base = d.forward_projection;
  if (!base || await getProjectionSource() !== "ours") return { f: base, range: null };
  const [fwd, ranges] = await Promise.all([
    api<ForwardProjection>(withSource(`/api/players/${id}/forward`, "ours")),
    getRanges(base.summary.as_of_week),
  ]);
  return fwd.ok ? { f: fwd.data, range: rangeFor(ranges, Number(id)) } : { f: base, range: null };
}
