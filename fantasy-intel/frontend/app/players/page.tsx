import Link from "next/link";
import { BackendProblem } from "@/components/BackendProblem";
import { PageHeader, Section, EmptyState } from "@/components/ui";
import { api } from "@/lib/api";
import { providerName } from "@/lib/labels";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { withSource } from "@/lib/projection-source";
import { getProjectionSource } from "@/lib/projection-source.server";
import type { ForwardProjection } from "@/lib/types";
import { rangeFor, type PlayerRange } from "@/lib/projection-ranges";
import { getRanges } from "@/lib/projection-ranges.server";
import { PlayerProfile } from "./PlayerProfile";
import { ResultsTable, SearchForm } from "./PlayerSearch";
import styles from "./players.module.css";
import type { Detail, PlayerListRow } from "./types";

export const metadata = { title: "Players" };

const LIMIT = 40;

export default async function PlayersPage({ searchParams }: PageProps<"/players">) {
  const sp = await searchParams;
  const q = typeof sp.q === "string" ? sp.q.trim() : "";
  const id = typeof sp.id === "string" && /^-?\d+$/.test(sp.id) ? sp.id : null;
  const cols = sp.cols === "all" ? "all" : "core";
  const profile = await getLeagueProfile();

  if (id) {
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
    // With the switch on "Ours", the forward view comes from the forward endpoint with source=independent (ESPN's
    // series is unchanged in it, plus the `ours` block); if that call fails the ESPN view from the profile stays.
    let d = detail.data;
    let range: PlayerRange | null = null;
    if (await getProjectionSource() === "ours" && d.forward_projection) {
      const [fwd, ranges] = await Promise.all([
        api<ForwardProjection>(withSource(`/api/players/${id}/forward`, "ours")),
        getRanges(d.forward_projection.summary.as_of_week),
      ]);
      if (fwd.ok) {
        d = { ...d, forward_projection: fwd.data };
        range = rangeFor(ranges, Number(id));
      }
    }
    return <PlayerProfile d={d} q={q} cols={cols} range={range} profile={profile} />;
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
