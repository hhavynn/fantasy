import Link from "next/link";
import { TeamLink } from "@/components/TeamLink";
import { UnknownTeam } from "@/components/TeamPerspective";
import { EmptyState, PageHeader, Section } from "@/components/ui";
import { api } from "@/lib/api";
import { cardHref, ordinal, PATH, type PowerRankings } from "@/lib/power-rankings";
import { isUnknownTeam, readTeamSelection } from "@/lib/team-perspective";
import { TeamCard } from "../TeamCard";
import { Unavailable } from "../Unavailable";
import styles from "../power.module.css";

export const metadata = { title: "Power rankings team card" };

// A clean, screenshot-ready card for one team's power ranking, with a chip row to switch teams. The URL is the
// share link: /power-rankings/<team id>.

export default async function PowerCardPage({ params }: PageProps<"/power-rankings/[team]">) {
  const { team } = await params;
  const sel = readTeamSelection({ team });
  const header = <PageHeader eyebrow={<TeamLink href={PATH}>Power rankings</TeamLink>} title="Team card" />;
  if (sel.kind !== "team") {
    return <div className="stack">{header}<UnknownTeam path={PATH} sel={sel} /></div>;
  }
  const res = await api<PowerRankings>(`/api/power-rankings?team_id=${sel.id}`);
  if (!res.ok) {
    if (isUnknownTeam(res, sel)) return <div className="stack">{header}<UnknownTeam path={PATH} sel={sel} detail={res.error} /></div>;
    return <Unavailable error={res.error} kind={res.kind} status={res.status} />;
  }
  const d = res.data;
  const t = d.teams.find((x) => x.team_id === sel.id);
  return (
    <div className="stack">
      <PageHeader
        eyebrow={<TeamLink href={PATH}>{d.week ? `Power rankings · Week ${d.week}` : "Power rankings"}</TeamLink>}
        title="Team card"
        meta="One team's power ranking on a single card. Share this page's link, or take a screenshot of the card."
      />
      {d.empty ? (
        <Section>
          <EmptyState title="Nothing to rank yet" action={<TeamLink href="/playoff-race">See the standings</TeamLink>}>
            <p>{d.message ?? "No team has a complete lineup projection."}</p>
          </EmptyState>
        </Section>
      ) : t ? (
        <div className={styles.cardStage}>
          <TeamCard d={d} team={t} />
        </div>
      ) : (
        <UnknownTeam path={PATH} sel={sel} detail={d.message} />
      )}
      {!d.empty && d.teams.length > 1 && (
        <nav aria-label="Other team cards" className={styles.switcher}>
          <div className="filters">
            {d.teams.map((x) => (
              <Link key={x.team_id} href={cardHref(x.team_id)} className="chip" aria-current={x.team_id === sel.id ? "page" : undefined}>
                {x.rank ? `${ordinal(x.rank)} · ` : ""}{x.name}
              </Link>
            ))}
          </div>
        </nav>
      )}
    </div>
  );
}
