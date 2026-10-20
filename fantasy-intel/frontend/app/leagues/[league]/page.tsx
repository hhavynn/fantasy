import Link from "next/link";
import { notFound } from "next/navigation";
import { connection } from "next/server";
import { Badge, Notice, PageHeader, Section } from "@/components/ui";
import { apiRequest } from "@/lib/api";
import { authConfig } from "@/lib/auth-config";
import { leaguePath } from "@/lib/league-scope";
import { isLeagueRef, leagueName, leagueState, multileagueUiEnabled, parseMyLeague } from "@/lib/my-leagues";
import { currentUser } from "@/lib/supabase/server";
import { LeagueActions } from "../LeagueActions";
import { CookieSettings, DeleteLeague, Invites, TeamForm } from "./forms";
import styles from "../leagues.module.css";

export const metadata = { title: "League settings" };

type Team = { team_id: number; name: string };

export default async function LeagueSettings({ params }: { params: Promise<{ league: string }> }) {
  await connection();
  const { league: id } = await params;
  if (!multileagueUiEnabled(process.env.MULTILEAGUE_UI_ENABLED) || !isLeagueRef(id)) notFound();
  const res = await apiRequest(`/v2/leagues/${id}`);
  const league = res.ok ? parseMyLeague(res.data) : null;
  if (!league) notFound();
  const user = authConfig(process.env) ? await currentUser() : null;
  const teamsRes = await apiRequest<{ teams: Team[] }>(`/v2/leagues/${id}/api/teams`);
  const teams = teamsRes.ok && teamsRes.data ? teamsRes.data.teams.map((t) => ({ id: String(t.team_id), name: t.name })) : [];
  const invites = league.role === "admin"
    ? await apiRequest<{ invites: { invite_id: string; expires_at: string }[] }>(`/v2/leagues/${id}/invites`) : null;
  const state = leagueState(league);
  return (
    <div className="stack">
      <PageHeader eyebrow="League settings" title={leagueName(league)}
                  meta={<Link href={leaguePath(id, "/")} prefetch={false}>Open the league</Link>} />
      <Section title="Sync">
        <p className={styles.plain}><Badge value={state.label} tone={state.tone === "neutral" ? "" : state.tone} /> {state.detail}</p>
        {user ? <LeagueActions leagueId={id} pinned={league.pinned} member={league.role !== null} /> : null}
      </Section>
      <Section title="Your team" subtitle="A preference only: every page analyses the league from this team. You can view any team.">
        {!user ? (
          <p className={styles.plain}><Link href={`/login?next=/leagues/${id}`} prefetch={false}>Sign in</Link> to save your team. Until then, pick a team on any page.</p>
        ) : teams.length === 0 ? (
          <p className={styles.plain}>The team list appears after the first sync.</p>
        ) : (
          <TeamForm leagueId={id} teams={teams} selected={league.teamId} />
        )}
      </Section>
      {user && league.visibility === "private" && league.role ? (
        <Section title="Your ESPN cookies" subtitle="Stored encrypted; the website itself cannot read them back. Only the sync job uses them, to read this league.">
          <CookieSettings leagueId={id} />
        </Section>
      ) : null}
      {invites ? (
        <Section title="Invites" subtitle="A link lets one person join as a viewer. It works once, for 7 days, and needs them to sign in.">
          {invites.ok ? <Invites leagueId={id} open={invites.data?.invites ?? []} /> : <Notice tone="negative" title="Invites could not be loaded">{invites.error}</Notice>}
        </Section>
      ) : null}
      {league.role === "admin" && league.visibility === "private" ? (
        <Section title="Delete this league" subtitle="Removes it for every member. Its data is deleted after 7 days.">
          <DeleteLeague leagueId={id} name={leagueName(league)} />
        </Section>
      ) : null}
    </div>
  );
}
