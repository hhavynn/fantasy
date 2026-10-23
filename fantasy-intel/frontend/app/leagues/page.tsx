import Link from "@/components/LeagueLink";
import { connection } from "next/server";
import { Suspense } from "react";
import { Badge, EmptyState, Notice, PageHeader, Section } from "@/components/ui";
import { apiRequest } from "@/lib/api";
import { authConfig } from "@/lib/auth-config";
import { authHref } from "@/lib/auth-flow";
import { signupGateNow } from "@/lib/email-link.server";
import { providerName } from "@/lib/labels";
import { leaguePath } from "@/lib/league-scope";
import { dataAsOf } from "@/lib/league-switcher";
import {
  defaultSeason, leagueName, leagueState, multileagueUiEnabled, parseMyLeague, parseMyLeagues, type MyLeague,
} from "@/lib/my-leagues";
import { hostHref, onboardingView, parseProviders, progressHref, type OnboardingView, type ProviderInfo } from "@/lib/onboarding";
import { MIN_LEAGUES } from "@/lib/action-center";
import { currentUser } from "@/lib/supabase/server";
import { ActionCenter, ActionCenterSkeleton } from "./ActionCenter";
import { CookieForm, EspnForm, SleeperForm } from "./AddLeague";
import { LeagueActions } from "./LeagueActions";
import { DemoEntry, EspnPrivateExplainer, HostCards, Stepper, SyncProgress } from "./Onboarding";
import styles from "./leagues.module.css";

export const metadata = { title: "Leagues" };

type SearchParams = Record<string, string | string[] | undefined>;

export default async function LeaguesPage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  // The flag and the signed-in user are per request, so never prerender this page.
  await connection();
  const enabled = multileagueUiEnabled(process.env.MULTILEAGUE_UI_ENABLED);
  if (!enabled) return <SwitchedOff />;
  const view = onboardingView(await searchParams);
  const auth = authConfig(process.env);
  const [user, providers, signupOpen] = await Promise.all([
    auth ? currentUser() : Promise.resolve(null),
    // What each host can do here: public, read from the backend's capability registry (null when it cannot answer).
    apiRequest("/v2/providers").then((r) => (r.ok ? parseProviders(r.data) : null)),
    // Offer "Create an account" only while both launch gates are open (no API call while this app keeps them closed).
    auth?.signupsOpen ? signupGateNow().then((g) => g.open) : Promise.resolve(false),
  ]);
  return (
    <div className="stack">
      <PageHeader eyebrow="Connections" title="Leagues"
                  meta="Add an ESPN or Sleeper league to analyse it with every page of this site. Public leagues need no cookies; anyone can view them from any team's perspective." />
      {user ? <SignedIn view={view} providers={providers} /> : <Guest view={view} providers={providers} signupOpen={signupOpen} />}
    </div>
  );
}

/** The URL a signed-out visitor returns to after signing in: the step they were on. */
function stepHref(view: OnboardingView): string {
  if (view.step === "details") return hostHref(view.host);
  if (view.step === "progress") return progressHref(view.leagueId);
  if (view.step === "private") return hostHref("espn");
  return "/leagues";
}

const signIn = (next: string) => `/login?next=${encodeURIComponent(next)}`;

function Guest({ view, providers, signupOpen }: { view: OnboardingView; providers: ProviderInfo[] | null; signupOpen: boolean }) {
  const next = stepHref(view);
  return (
    <>
      {signupOpen ? (
        <Notice tone="info" title="Sign in or create an account to add leagues">
          Viewing a public league or the demo needs no account: open its link. To add a league, save your team or
          connect a private league, <Link href={signIn(next)} prefetch={false}>sign in</Link> or{" "}
          <Link href={authHref("/signup", next)} prefetch={false}>create an account</Link>. Both use an emailed link, no
          password.
        </Notice>
      ) : (
        <Notice tone="info" title="Sign in to add leagues">
          Viewing a public league needs no account: open its link. To add a league, save your team or connect a private
          league, <Link href={signIn(next)} prefetch={false}>sign in</Link>. Sign-ups for new accounts are not open yet.
        </Notice>
      )}
      <Section title="See it before you connect" subtitle="Everything in the demo is invented, so you can look around freely.">
        <DemoEntry />
      </Section>
      <Section title="Connecting a league takes three steps" subtitle="Choose the host, paste the league's address, then wait a few minutes for the first sync.">
        <HostCards providers={providers} chooseHref={(host) => signIn(hostHref(host))} />
      </Section>
    </>
  );
}

async function SignedIn({ view, providers }: { view: OnboardingView; providers: ProviderInfo[] | null }) {
  const [res, focus] = await Promise.all([
    apiRequest("/v2/leagues"),
    view.step === "progress" ? apiRequest(`/v2/leagues/${view.leagueId}`) : Promise.resolve(null),
  ]);
  const leagues = res.ok ? parseMyLeagues(res.data) : [];
  const focused = focus?.ok ? parseMyLeague(focus.data) : null;
  // The all-leagues action center (issue #166) leads the page for a manager with two or more leagues, above the list
  // that switches between them; it streams in behind its skeleton while each league is checked on the server.
  const center = view.step === "choose" && res.ok && leagues.length >= MIN_LEAGUES;
  return (
    <>
      {center ? (
        <Suspense fallback={<ActionCenterSkeleton />}>
          <ActionCenter />
        </Suspense>
      ) : null}
      {view.step === "progress" ? (
        <Section title="Add a league" subtitle="Step 3 of 3: the first sync. You can leave this page; the league keeps syncing.">
          <div className={styles.flow}>
            <Stepper current={3} />
            {focused ? <SyncProgress league={focused} note={view.note} /> : (
              <Notice tone="caution" title="This league is not available to you">
                It may have been removed, or it is private and you are not a member.{" "}
                <Link href="/leagues" prefetch={false}>Add a league</Link> or pick one of yours below.
              </Notice>
            )}
          </div>
        </Section>
      ) : (
        <AddSteps view={view} providers={providers} />
      )}
      {res.ok && leagues.length === 0 && view.step !== "progress" ? (
        <Section title="Not ready to connect yet?" subtitle="A made-up league shows what you get.">
          <DemoEntry />
        </Section>
      ) : null}
      <Section title="Your leagues" subtitle="Leagues you added, pinned or were invited to." id="your-leagues">
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

function AddSteps({ view, providers }: { view: Exclude<OnboardingView, { step: "progress" }>; providers: ProviderInfo[] | null }) {
  if (view.step === "choose") {
    return (
      <Section title="Add a league" subtitle="Step 1 of 3: where does your league live?">
        <div className={styles.flow}>
          <Stepper current={1} />
          <HostCards providers={providers} chooseHref={hostHref} />
        </div>
      </Section>
    );
  }
  if (view.step === "private") {
    return (
      <Section title="Add a league" subtitle="Step 2 of 3: a private ESPN league.">
        <div className={styles.flow}>
          <Stepper current={2} />
          <EspnPrivateExplainer league={view.league} season={view.season} />
          <CookieForm league={view.league} season={view.season} />
        </div>
      </Section>
    );
  }
  const info = providers?.find((p) => p.provider === view.host) ?? null;
  const name = providerName(view.host);
  return (
    <Section title="Add a league" subtitle={`Step 2 of 3: your ${name} league.`}
             actions={<Link href="/leagues" prefetch={false}>Change host</Link>}>
      <div className={styles.flow}>
        <Stepper current={2} />
        {info && !info.enabled ? (
          <Notice tone="caution" title={`${name} is not available in this deployment`}>
            {info.reason ?? "It is switched off here."} <Link href="/leagues" prefetch={false}>Choose another host</Link>.
          </Notice>
        ) : view.host === "sleeper" ? <SleeperForm /> : <EspnForm defaultSeason={defaultSeason(new Date())} />}
      </div>
    </Section>
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
          {/* Until its first sync has published, a league's pages answer "first sync in progress": link to its progress. */}
          <Link href={league.syncState === "ready" ? leaguePath(league.id, "/") : progressHref(league.id)} prefetch={false}>{name}</Link>
        </h3>
        <div className={styles.badges}>
          {league.provider !== "espn" ? <Badge value={providerName(league.provider)} tone="" /> : null}
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
