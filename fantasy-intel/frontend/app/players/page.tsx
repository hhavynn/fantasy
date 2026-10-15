import Link from "next/link";
import { BackendProblem } from "@/components/BackendProblem";
import { PageHeader, Section, EmptyState } from "@/components/ui";
import { api } from "@/lib/api";
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

  if (id) {
    const detail = await api<Detail>(`/api/players/${id}`);
    if (!detail.ok) {
      return (
        <div className="stack">
          <PageHeader title="Players" actions={<SearchForm q={q} compact />} />
          {detail.status === 404 ? (
            <Section>
              <EmptyState title="Player not found" action={<Link href="/players">Back to player search</Link>}>
                No player in this league&apos;s ESPN pool has the id {id}.
              </EmptyState>
            </Section>
          ) : (
            <BackendProblem error={detail.error} kind={detail.kind} status={detail.status} />
          )}
        </div>
      );
    }
    return <PlayerProfile d={detail.data} q={q} cols={cols} />;
  }

  const list = await api<PlayerListRow[]>(`/api/players?q=${encodeURIComponent(q)}&limit=${LIMIT}`);
  return (
    <div className="stack">
      <PageHeader
        title="Players"
        meta="Usage beneath the box score: snaps, shares, expected points, red-zone work."
      >
        <SearchForm q={q} autoFocus />
        <p className={styles.hint}>
          Search by name; kickers and team defenses too. Open a player for week-by-week usage, the forward projection and the game log.
        </p>
      </PageHeader>
      {list.ok
        ? <ResultsTable rows={list.data} q={q} limit={LIMIT} />
        : <BackendProblem error={list.error} kind={list.kind} status={list.status} />}
    </div>
  );
}
