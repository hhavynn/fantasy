import Link from "@/components/LeagueLink";
import { BackendProblem } from "@/components/BackendProblem";
import {
  EmptyState, Glossary, Method, Notice, PageHeader, Section,
} from "@/components/ui";
import { api } from "@/lib/api";
import { when } from "@/lib/format";
import { providerName } from "@/lib/labels";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { plural } from "@/lib/plural";
import { getProjectionSource } from "@/lib/projection-source.server";
import {
  PAGE_SIZE, parseRankingsFilters, rankingsApiPath, rankingsHref, type RankingsFilters, type RankingsResponse,
} from "@/lib/rankings";
import type { SearchParams } from "@/lib/sort";
import { RankingsCards } from "./RankingsCards";
import { RankingsControls } from "./RankingsControls";
import { RankingsTable } from "./RankingsTable";
import styles from "./rankings.module.css";

export const metadata = { title: "Rankings" };

const QUESTION = "Who are the strongest fantasy options right now, and why?";

/** "12 on bye, 3 without a projection" for the unranked reasons that have players. */
function unrankedParts(u: RankingsResponse["counts"]["unranked"]): string[] {
  const parts: string[] = [];
  if (u.bye) parts.push(`${u.bye} on bye`);
  if (u.missing) parts.push(`${u.missing} without a projection`);
  if (u.incomplete) parts.push(`${u.incomplete} with an incomplete rest-of-season window`);
  if (u.no_ours) parts.push(`${u.no_ours} without an Ours estimate`);
  return parts;
}

function SourceNotices({ data, filters, providerProjections }: { data: RankingsResponse; filters: RankingsFilters; providerProjections: boolean }) {
  const ours = data.projection.ours;
  if (!providerProjections) {
    // Sleeper: no ESPN numbers exist, so nothing is ranked "by ESPN" and there is nothing beside Ours.
    if (data.projection.basis === "ours") {
      return (
        <Notice tone="info" title="Ranked by Fantasy Intel projections">
          Ours is Fantasy Intel&apos;s own projection model; this league&apos;s provider publishes no projections. A player with no Ours estimate is listed unranked.
        </Notice>
      );
    }
    return (
      <Notice tone="caution" title="Ours is unavailable here, so players are listed unranked">
        {ours?.reason ?? data.scoring.ours_reason ?? "Fantasy Intel has no usable independent projections for this window."}
      </Notice>
    );
  }
  if (data.projection.basis === "ours") {
    return (
      <Notice tone="info" title="Ranked by Fantasy Intel projections">
        Ours is Fantasy Intel&apos;s own projection model, with ESPN&apos;s number right beside it. A player with no Ours estimate is listed unranked, never ranked on ESPN&apos;s number.
      </Notice>
    );
  }
  if (filters.source === "ours") {
    return (
      <Notice tone="caution" title="Ours is unavailable here, so players are ranked by ESPN">
        {ours?.reason ?? data.scoring.ours_reason ?? "Fantasy Intel has no usable independent projections for this window."}
      </Notice>
    );
  }
  return null;
}

export default async function RankingsPage({ searchParams }: PageProps<"/rankings">) {
  const sp = (await searchParams) as SearchParams;
  const [source, profile] = await Promise.all([getProjectionSource(), getLeagueProfile()]);
  const espnOn = profile.providerProjections;
  const provider = providerName(profile.provider);
  const parsed = parseRankingsFilters(sp, source, espnOn);
  // No ESPN column to sort by where the provider publishes no projections: an old `sort=espn` link sorts by Ours.
  const filters: RankingsFilters = !espnOn && parsed.sort.key === "espn" ? { ...parsed, sort: { ...parsed.sort, key: "ours" } } : parsed;
  const res = await api<RankingsResponse>(rankingsApiPath(filters));

  if (!res.ok) {
    // The backend answers 503 when the league has not been synced yet (or its current week is unknown).
    const notSynced = res.status === 503 && res.kind === "http" && !/no database yet/i.test(res.error);
    return (
      <div className="stack">
        <PageHeader eyebrow="Players" title="Rankings" meta={QUESTION} />
        {notSynced ? (
          <Section>
            <EmptyState title="Your league isn't synced yet" action={<Link href="/data">Check data status</Link>}>
              <p>Rankings need your league&apos;s {provider} players, projections and current week. Run the {provider} sync, then reload this page.</p>
              <p className="faint">{res.error}</p>
            </EmptyState>
          </Section>
        ) : <BackendProblem error={res.error} kind={res.kind} status={res.status} />}
      </div>
    );
  }

  const data = res.data;
  const { counts, players } = data;
  const showOurs = data.projection.basis === "ours";
  const unranked = unrankedParts(counts.unranked);
  const unrankedTotal = Object.values(counts.unranked).reduce((a, b) => a + b, 0);
  const spNoOffset: SearchParams = Object.fromEntries(Object.entries(sp).filter(([k]) => k !== "offset"));
  const posName = filters.pos === "ALL" ? "All players" : filters.pos;
  const filtered = filters.pos !== "ALL" || filters.status !== "all" || filters.q !== "";
  const from = counts.matched === 0 || players.length === 0 ? 0 : filters.offset + 1;
  const to = filters.offset + players.length;
  const hasPrev = filters.offset > 0;
  const hasNext = filters.offset + players.length < counts.matched;
  const f = data.freshness;

  return (
    <div className="stack">
      <PageHeader eyebrow={`Players · Week ${data.week}`} title="Rankings" meta={QUESTION}
        actions={<Link className="chip" href="/rankings/compare">Compare sources</Link>} />

      <RankingsControls sp={sp} filters={filters} leagueAvailable={data.league_context.available} sourceChoice={espnOn} />

      <p className={styles.sourceLine}>
        <strong>{data.projection.label}.</strong>{" "}
        {espnOn ? (
          <>
            ESPN projections synced {when(f.espn_projections_synced_at)}
            {showOurs && f.ours_run_created_at && <>; Fantasy Intel run {when(f.ours_run_created_at)}</>}
          </>
        ) : <>Fantasy Intel run {when(f.ours_run_created_at)}</>}
        {f.player_scores_as_of_week !== null && <>; ratings and usage as of week {f.player_scores_as_of_week}</>}.
      </p>

      <SourceNotices data={data} filters={filters} providerProjections={espnOn} />

      {counts.pool > 0 && counts.ranked === 0 && (
        <Notice tone="caution" title="No player has a rank in this view">
          {unranked.length > 0 ? `${unranked.join(", ")}.` : "No player has a projection to rank by."} The players below are listed unranked.
        </Notice>
      )}

      <Section
        title={<>{posName} <span className={styles.count}>{counts.matched} {plural(counts.matched, "player")}{filtered ? " match" : ""}</span></>}
        subtitle={counts.pool > 0
          ? <>
            {counts.ranked} of {counts.pool} {plural(counts.pool, "player")} in the pool {plural(counts.ranked, "has a rank", "have a rank")}
            {unrankedTotal > 0 && counts.ranked > 0 && <>; {unranked.join(", ")} — listed last, unranked</>}.
          </>
          : undefined}
        actions={players.length > 0 ? <span className="faint">{from}–{to} of {counts.matched}</span> : undefined}
        flush={players.length > 0}
        footnote={<>
          {data.window.label}{data.window.note ? `: ${data.window.note}` : ""}. Ratings are 0–100.0 percentiles within a position
          (full PPR), not a trade price and not comparable to projected points.
          {data.notes.length > 0 && <>{" "}{data.notes.join(" ")}</>}
        </>}
      >
        {players.length > 0 ? (
          <>
            <RankingsTable rows={players} sort={filters.sort} sp={spNoOffset} showOurs={showOurs || !espnOn} windowLabel={data.window.label} basis={data.projection.basis} espnColumn={espnOn} />
            <RankingsCards rows={players} showOurs={showOurs} basis={data.projection.basis} espnColumn={espnOn} />
          </>
        ) : counts.pool === 0 ? (
          <EmptyState title="No players in the pool yet" action={<Link href="/data">Check data status</Link>}>
            Sync your {provider} league to load its player pool, then reload this page.
          </EmptyState>
        ) : counts.matched === 0 ? (
          <EmptyState title="No players match these filters" action={<Link className="chip reset" href="/rankings">Clear filters</Link>}>
            Try another position, roster status or spelling. Accents and capital letters are ignored.
          </EmptyState>
        ) : (
          <EmptyState title="No players on this page" action={<Link href={rankingsHref(spNoOffset, {})}>Back to the first page</Link>}>
            This view has {counts.matched} {plural(counts.matched, "player")}, fewer than the page asked for.
          </EmptyState>
        )}
      </Section>

      {(hasPrev || hasNext) && (
        <nav className={styles.pager} aria-label="Ranking pages">
          {hasPrev && <Link className="chip" scroll={false} href={rankingsHref(sp, { offset: filters.offset - PAGE_SIZE > 0 ? String(filters.offset - PAGE_SIZE) : null })}>Previous</Link>}
          <span className="sub">{from}–{to} of {counts.matched}</span>
          {hasNext && <Link className="chip" scroll={false} href={rankingsHref(sp, { offset: String(filters.offset + PAGE_SIZE) })}>Next</Link>}
        </nav>
      )}

      <Glossary terms={["Projected points", "ROS", "Ratings (0–100)", "xFP/g", "PPR/g", "Ours", "Bye", "—"]} providerProjections={profile.providerProjections} />
      <Method title="How rankings are calculated">
        <div className={styles.method}>
          <p>
            Players are ranked by <b>projected fantasy points</b> for the window shown, in your league&apos;s scoring: {espnOn
              ? <>ESPN&apos;s number as
            published by default, or Fantasy Intel&apos;s independent model when you opt in with the source switch.</>
              : <>Fantasy Intel&apos;s independent model (this league&apos;s provider publishes no projections).</>} Equal projections (to 0.01) share a rank, shown as T-WR3.
            Ranks are computed before the position, roster and name filters, so a filter never renumbers anyone.
          </p>
          <p>
            Players with no value to rank by come last and say why: on a bye, no projection, an incomplete rest-of-season window, or no Ours estimate.
            A bye is never treated as a projected 0.
          </p>
          <p>
            <b>Ratings</b> (overall, opportunity, production) are a separate, position-relative 0–100.0 percentile from recent play, in full PPR.
            A 90 running back and a 90 receiver are each near the top of their own position; a rating is not a trade price and is not comparable to projected points.
            Fewer than two games means no rating, shown as a dash. Standard deviation is the spread of weekly PPR points over games played, in raw points.
          </p>
          <p>Rest of season covers the remaining regular-season weeks, the same window for every player. A total is shown only when every week in it is projected (a bye week is
            not required).</p>
        </div>
      </Method>
    </div>
  );
}
