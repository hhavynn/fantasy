import Link from "next/link";
import { connection } from "next/server";
import { Badge, EmptyState, Notice, PageHeader, Section } from "@/components/ui";
import { apiRequest } from "@/lib/api";
import { authConfig } from "@/lib/auth-config";
import { leaguePath } from "@/lib/league-scope";
import { dataAsOf } from "@/lib/league-switcher";
import { defaultSeason, leagueName, leagueState, multileagueUiEnabled, parseMyLeagues, type MyLeague } from "@/lib/my-leagues";
import { currentUser } from "@/lib/supabase/server";
import { AddLeague } from "./AddLeague";
import { LeagueActions } from "./LeagueActions";
import styles from "./leagues.module.css";

export const metadata = { title: "Leagues" };

export default async function LeaguesPage() {
  // The flag and the signed-in user are per request, so never prerender this page.
  await connection();
  const enabled = multileagueUiEnabled(process.env.MULTILEAGUE_UI_ENABLED);
  if (!enabled) return <SwitchedOff />;
  const user = authConfig(process.env) ? await currentUser() : null;
  return (
    <div className="stack">
      <PageHeader eyebrow="Connections" title="Leagues"
                  meta="Add an ESPN league to analyse it with every page of this site. Public leagues need no cookies; anyone can view them from any team's perspective." />
      {user ? <SignedIn /> : (
        <Notice tone="info" title="Sign in to add leagues">
          Viewing a public league needs no account: open its link. To add a league, save your team or connect a private
          league, <Link href="/login?next=/leagues" prefetch={false}>sign in</Link>.
        </Notice>
      )}
    </div>
  );
}

async function SignedIn() {
  const res = await apiRequest("/v2/leagues");
  const leagues = res.ok ? parseMyLeagues(res.data) : [];
  return (
    <>
      <Section title="Add an ESPN league" subtitle="Public leagues are read without cookies. A private league asks for yours next.">
        <AddLeague defaultSeason={defaultSeason(new Date())} />
      </Section>
      <Section title="Your leagues" subtitle="Leagues you added, pinned or were invited to.">
        {!res.ok ? (
          <Notice tone="negative" title="Your leagues could not be loaded">{res.error}</Notice>
        ) : leagues.length === 0 ? (
          <EmptyState title="No leagues yet">Add one above, or open a league link someone shared.</EmptyState>
        ) : (
          <ul className={styles.leagues}>
            {leagues.map((l) => <li key={l.id}><LeagueCard league={l} /></li>)}
          </ul>
        )}
      </Section>
    </>
  );
}

function LeagueCard({ league }: { league: MyLeague }) {
  const state = leagueState(league);
  const asOf = dataAsOf(league.committedAt, "UTC");
  const name = leagueName(league);
  const headingId = `league-${league.id}`;
  return (
    <article className={styles.league} aria-labelledby={headingId}>
      <div className={styles.cardHead}>
        <h3 id={headingId} className={styles.cardTitle}>
          <Link href={leaguePath(league.id, "/")} prefetch={false}>{name}</Link>
        </h3>
        <div className={styles.badges}>
          <Badge value={`Season ${league.season}`} tone="" />
          <Badge value={league.visibility === "private" ? "Private" : "Public"} tone="" />
          {league.role ? <Badge value={league.role === "admin" ? "Admin" : "Member"} tone="" /> : null}
        </div>
      </div>
      <dl className={styles.facts}>
        <div>
          <dt>Status</dt>
          <dd><Badge value={state.label} tone={state.tone === "neutral" ? "" : state.tone} /> {state.detail}</dd>
        </div>
        <div>
          <dt>Data</dt>
          <dd>{asOf ? `${asOf} UTC` : "Not synced yet"}</dd>
        </div>
      </dl>
      <div className={styles.manage}>
        <LeagueActions leagueId={league.id} pinned={league.pinned} member={league.role !== null} />
        <Link href={`/leagues/${league.id}`} prefetch={false}>Settings</Link>
      </div>
    </article>
  );
}

function SwitchedOff() {
  return (
    <div className="stack">
      <PageHeader eyebrow="Connections" title="Leagues"
                  meta="Which fantasy providers this dashboard can read, and how it gets its league today." />
      <Notice tone="info" title="Connecting leagues from the browser is switched off in this deployment">
        This page is a prototype. Nothing can be connected or listed here, so no connect controls are shown. The rest of the dashboard works as before.
      </Notice>
      <Section title="How this dashboard gets its league" subtitle="The one setup that works today">
        <ol className={styles.steps}>
          <li>The deployment&apos;s owner sets <code>ESPN_LEAGUE_ID</code> (and the season, <code>ESPN_SEASON</code>) in <code>fantasy-intel/.env</code>. The id is the number after <code>leagueId=</code> in the league&apos;s fantasy.espn.com address.</li>
          <li>A private ESPN league also needs the owner&apos;s <code>espn_s2</code> and <code>SWID</code> cookies in the same file, as <code>ESPN_S2</code> and <code>SWID</code>. They are read from the environment only.</li>
          <li>The sync scripts import the league and NFL usage data; every page then reads from that import.</li>
        </ol>
      </Section>
    </div>
  );
}
