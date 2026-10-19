import { connection } from "next/server";
import { Badge, EmptyState, Notice, PageHeader, Section } from "@/components/ui";
import {
  NOT_ENABLED_REASON, buildProviderCards, dataOrigin, defaultSeason, describeMissing, freshness, multileagueUiEnabled,
  myTeamOption, providerLabel, type ProviderCard,
} from "@/lib/leagues";
import { loadLeaguesData, serverNow, type LeagueWithTeams, type LeaguesData } from "./load";
import { ConnectForm } from "./ConnectForm";
import { RemoveButton } from "./RemoveButton";
import { TeamPicker } from "./TeamPicker";
import styles from "./leagues.module.css";

export const metadata = { title: "Leagues" };

const HINTS: Record<string, { hint: string; note?: string }> = {
  sleeper: { hint: "Digits only: open the league on sleeper.com and copy the number after /leagues/ in the address bar.", note: "Public leagues by league id. No password or token is needed." },
  espn: { hint: "Digits only: open the league on fantasy.espn.com and copy the number after leagueId= in the address bar.", note: "Public leagues by league id. Private ESPN leagues cannot be connected here, and no cookies or passwords are requested." },
  yahoo: { hint: "", note: "Yahoo leagues need an OAuth sign-in with Yahoo." },
};

export default async function LeaguesPage() {
  // The flag and the signed-in user are per request, so never prerender this page.
  await connection();
  const enabled = multileagueUiEnabled(process.env.MULTILEAGUE_UI_ENABLED);
  const data = enabled ? await loadLeaguesData() : null;
  const now = serverNow();

  const cards = buildProviderCards(data?.providers ?? [], {
    flagEnabled: enabled,
    providersLoaded: !!data && data.providersProblem === null,
    canConnect: !!data && data.signedOutReason === null,
    signedOutReason: data?.signedOutReason ?? undefined,
  });

  return (
    <div className="stack">
      <PageHeader
        eyebrow="Connections"
        title="Leagues"
        meta={enabled
          ? "Connect leagues from other providers and choose your team in each. Prototype: the existing ESPN dashboard is unchanged."
          : "Which fantasy providers this dashboard can read, and how it gets its league today."}
      />

      {!enabled ? (
        <>
          <Notice tone="info" title="Connecting leagues from the browser is switched off in this deployment">
            This page is a prototype. Nothing can be connected or listed here, so no connect controls are shown. The rest of the dashboard works as before.
          </Notice>
          <Section title="How this dashboard gets its league" subtitle="The one setup that works today">
            <ol className={styles.steps}>
              <li>The deployment&apos;s owner sets <code>ESPN_LEAGUE_ID</code> (and the season, <code>ESPN_SEASON</code>) in <code>fantasy-intel/.env</code>. The id is the number after <code>leagueId=</code> in the league&apos;s fantasy.espn.com address.</li>
              <li>A private ESPN league also needs the owner&apos;s <code>espn_s2</code> and <code>SWID</code> cookies in the same file, as <code>ESPN_S2</code> and <code>SWID</code>. They are read from the environment only.</li>
              <li>The sync scripts import the league and NFL usage data; every page then reads from that import.</li>
            </ol>
            <p className={styles.plain}>The provider list below shows what each provider could supply once this prototype is enabled.</p>
          </Section>
        </>
      ) : null}
      {data?.providersProblem ? <Notice tone={data.providersProblem.tone} title={data.providersProblem.title}>{data.providersProblem.body}</Notice> : null}

      <Section title={enabled ? "Connect a league" : "Providers"} subtitle={enabled ? "Pick a provider. A provider that is switched off says why." : "What each provider supports, and why it is unavailable here."}>
        <ul className={styles.cards}>
          {cards.map((card) => <li key={card.provider}><ProviderCardView card={card} season={defaultSeason(new Date(now))} /></li>)}
        </ul>
      </Section>

      {enabled ? (
        <Section title="Connected leagues" subtitle="Leagues connected to your account.">
          <ConnectedLeagues enabled={enabled} data={data} now={now} />
        </Section>
      ) : null}
    </div>
  );
}

function ProviderCardView({ card, season }: { card: ProviderCard; season: number }) {
  const copy = HINTS[card.provider];
  const reasonId = `reason-${card.provider}`;
  return (
    <article className={styles.card} aria-labelledby={`provider-${card.provider}`}>
      <div className={styles.cardHead}>
        <h3 id={`provider-${card.provider}`} className={styles.cardTitle}>{card.label}</h3>
        <Badge value={card.state === "enabled" ? "Available" : "Unavailable"} tone={card.state === "enabled" ? "good" : ""} />
      </div>
      {copy?.note ? <p className={styles.desc}>{copy.note}</p> : null}

      {card.state === "enabled" ? (
        <ConnectForm provider={card.provider} label={card.label} defaultSeason={season} hint={copy?.hint ?? ""} />
      ) : (
        <div className={styles.disabledBlock}>
          <p id={reasonId} className={styles.reason}><span className={styles.reasonLabel}>Why unavailable:</span> {card.reason}</p>
        </div>
      )}

      {card.unavailable.length > 0 ? (
        <details className={styles.details}>
          <summary>{card.unavailable.length} {card.unavailable.length === 1 ? "thing" : "things"} {card.label} cannot supply</summary>
          <ul className={styles.capList}>
            {card.unavailable.map((c) => <li key={c.name}><b>{c.label}</b>: {c.supportLabel}{c.note ? `. ${c.note}` : ""}</li>)}
          </ul>
        </details>
      ) : null}
    </article>
  );
}

function ConnectedLeagues({ enabled, data, now }: { enabled: boolean; data: LeaguesData | null; now: number }) {
  if (!enabled || !data) {
    return <EmptyState title="Connected leagues are unavailable">{NOT_ENABLED_REASON}</EmptyState>;
  }
  if (data.signedOutReason) {
    return (
      <EmptyState title="Sign-in is not available yet">
        {data.signedOutReason} Connected leagues belong to an account, so none can be shown or added until sign-in exists.
      </EmptyState>
    );
  }
  if (data.leaguesProblem) {
    return <Notice tone={data.leaguesProblem.tone} title={data.leaguesProblem.title}>{data.leaguesProblem.body}</Notice>;
  }
  if (data.leagues.length === 0) {
    return <EmptyState title="No leagues connected yet">Use Connect a league above to add one.</EmptyState>;
  }
  return (
    <ul className={styles.leagues}>
      {data.leagues.map((item) => <li key={item.league.league_id}><LeagueCard item={item} now={now} /></li>)}
    </ul>
  );
}

function LeagueCard({ item, now }: { item: LeagueWithTeams; now: number }) {
  const { league, teams, teamsProblem } = item;
  const name = league.name?.trim() || "Unnamed league";
  const headingId = `league-${league.league_id}`;
  const origin = dataOrigin(league.data_origin);
  const fresh = freshness(league.sync, now);
  const missing = describeMissing(league.missing_capabilities, league.capabilities);
  const mine = myTeamOption(league, teams);
  const canPick = teams.length > 0 && !teamsProblem;

  return (
    <article className={styles.league} aria-labelledby={headingId}>
      <div className={styles.cardHead}>
        <h3 id={headingId} className={styles.cardTitle}>{name}</h3>
        <div className={styles.badges}>
          <Badge value={providerLabel(league.provider)} tone="" />
          <Badge value={`Season ${league.season}`} tone="" />
          <Badge value={origin.label} tone={origin.tone === "neutral" ? "" : origin.tone} title={origin.title} />
        </div>
      </div>

      <dl className={styles.facts}>
        <div>
          <dt>Data origin</dt>
          <dd>{origin.label}. {origin.title}.</dd>
        </div>
        <div>
          <dt>Sync</dt>
          <dd>
            <Badge value={fresh.statusLabel} tone={fresh.tone === "neutral" ? "" : fresh.tone} />{" "}
            {fresh.summary}
            {fresh.details.map((d) => <span key={d} className={styles.detailLine}>{d}</span>)}
          </dd>
        </div>
        <div>
          <dt>My team</dt>
          <dd>
            {mine ? mine.label : league.my_team_external_id ? `Team ${league.my_team_external_id} (not in the provider's current team list)` : "Not chosen yet"}
          </dd>
        </div>
        <div>
          <dt>League id</dt>
          <dd className="mono">{league.external_league_id}</dd>
        </div>
      </dl>

      <div className={styles.caps}>
        <h4 className={styles.subhead}>Not available for this league</h4>
        {missing.length === 0 ? (
          <p className={styles.plain}>The backend reports no missing capabilities.</p>
        ) : (
          <ul className={styles.capList}>
            {missing.map((c) => <li key={c.name}><b>{c.label}</b>: {c.supportLabel}{c.note ? `. ${c.note}` : ""}</li>)}
          </ul>
        )}
      </div>

      <div className={styles.manage}>
        {teamsProblem ? (
          <Notice tone={teamsProblem.tone} title={teamsProblem.title}>{teamsProblem.body}</Notice>
        ) : canPick ? (
          <TeamPicker leagueId={league.league_id} teams={teams} selected={league.my_team_external_id} />
        ) : (
          <p className={styles.plain}>
            {league.missing_capabilities.includes("teams")
              ? `${providerLabel(league.provider)} cannot list teams for this league, so a team cannot be chosen.`
              : "The provider returned no teams for this league, so a team cannot be chosen."}
          </p>
        )}
        <RemoveButton leagueId={league.league_id} name={name} />
      </div>
    </article>
  );
}
