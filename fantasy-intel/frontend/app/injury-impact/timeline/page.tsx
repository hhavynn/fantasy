import Link from "@/components/LeagueLink";
import type { ReactNode } from "react";
import { BackendProblem } from "@/components/BackendProblem";
import { PlayerIdentity } from "@/components/PlayerIdentity";
import { TeamPerspective, UnknownTeam } from "@/components/TeamPerspective";
import { Badge, EmptyState, Method, Notice, PageHeader, Section, Stat, StatStrip } from "@/components/ui";
import { api } from "@/lib/api";
import {
  eventTimeText, formatEt, injuryImpactHref, levelBadge, newestFirst, PRACTICE_REASON, profileHref, relationText,
  signalText, SOURCE_TYPE_LABEL, type AvailabilityTimelineResponse, type TimelineEvent, type TimelinePlayer,
} from "@/lib/availability-timeline";
import { providerName } from "@/lib/labels";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { plural } from "@/lib/plural";
import type { SearchParams } from "@/lib/sort";
import { isUnknownTeam, readTeamSelection, selectedTeamId, teamParamFrom, withTeam, withTeamId } from "@/lib/team-perspective";
import styles from "./timeline.module.css";

export const metadata = { title: "Availability" };

const PATH = "/injury-impact/timeline";
const QUESTION = "Every injury report on this roster, in order, with its source and time.";

function Shell({ sp, current, week, children }: { sp: SearchParams; current: number | null; week?: number; children: ReactNode }) {
  return (
    <div className="stack">
      <PageHeader eyebrow={week ? `My team · Week ${week}` : "My team"} title="Availability" meta={QUESTION}
        actions={<TeamPerspective path={PATH} sp={sp} current={current} />} />
      {children}
    </div>
  );
}

function SourceTag({ type }: { type: TimelineEvent["source"]["type"] }) {
  return <span className={`${styles.tag} ${styles[type]}`}>{SOURCE_TYPE_LABEL[type]}</span>;
}

function SourceLinks({ e }: { e: TimelineEvent }) {
  const links = [e.source.url, ...e.source.urls].filter((u): u is string => !!u && /^https?:\/\//.test(u));
  if (links.length === 0) return <span className={styles.cite}>{e.source.label}</span>;
  return (
    <span className={styles.cite}>
      {e.source.label}
      {links.map((u, i) => (
        <span key={u}>{" · "}<a href={u} target="_blank" rel="noopener noreferrer nofollow">{links.length > 1 ? `report ${i + 1}` : "source"}</a></span>
      ))}
    </span>
  );
}

function EventRow({ e, events, prov }: { e: TimelineEvent; events: TimelineEvent[]; prov: string }) {
  const rel = relationText(e, events);
  const replaced = e.relation === "superseded" || e.relation === "earlier_week" || e.relation === "duplicate";
  return (
    <li className={replaced ? styles.replaced : undefined} data-kind={e.kind} data-relation={e.relation}>
      <div className={styles.eventHead}>
        <SourceTag type={e.source.type} />
        <strong className={styles.eventTitle}>{e.title}</strong>
        {rel && <span className={styles.relation}>{rel}</span>}
        {e.after_kickoff && <span className={styles.late}>Modified after kickoff</span>}
      </div>
      <p className={styles.eventDetail}>{e.detail}</p>
      <p className={styles.eventMeta}>
        {e.at ? <time dateTime={e.at}>{eventTimeText(e, prov)}</time> : <span>{eventTimeText(e, prov)}</span>}
        {" · "}<SourceLinks e={e} />
        {e.source.type === "provider" && <>{" · "}<Link href="/data">Sync status</Link></>}
      </p>
    </li>
  );
}

function PlayerCard({ p, team, prov }: { p: TimelinePlayer; team: string | null; prov: string }) {
  const h = p.headline;
  const badge = levelBadge(h.level);
  const reports = p.events.filter((e) => e.kind !== "kickoff").length;
  const ko = formatEt(p.game.kickoff);
  return (
    <article className={styles.player} data-player={p.espn_player_id} data-level={h.level}>
      <div className={styles.playerHead}>
        <PlayerIdentity id={p.espn_player_id} name={p.full_name} position={p.position} team={p.team}
          meta={p.lineup_slot ? <span className={styles.slot}>{p.lineup_slot}</span> : undefined} />
        <dl className={styles.facts}>
          <div>
            <dt>Status</dt>
            <dd>
              <span className={styles.level}>{badge && <Badge value={badge} />} {h.label}</span>
              <span className={styles.sub}>{h.text}</span>
            </dd>
          </div>
          <div>
            <dt>Practice</dt>
            <dd>
              {p.practice.code === "unknown"
                ? <span className={styles.unknown}>Unknown</span>
                : <span>{p.practice.label}{p.practice.code !== "OTHER" ? ` (${p.practice.code})` : ""}</span>}
              <span className={styles.sub}>{p.practice.code === "unknown"
                ? (PRACTICE_REASON[p.practice.reason ?? ""] ?? "Not reported.")
                : "Final practice on the report, not each day."}</span>
            </dd>
          </div>
          <div>
            <dt>Game</dt>
            <dd>
              {p.game.on_bye ? <span>Bye</span> : p.game.opponent
                ? <span>vs {p.game.opponent}{p.game.started ? " · kicked off" : ""}</span>
                : <span className={styles.unknown}>Unknown</span>}
              {ko && !p.game.on_bye && <span className={styles.sub}><time dateTime={p.game.kickoff ?? undefined}>{ko}</time></span>}
            </dd>
          </div>
        </dl>
      </div>
      {h.disagreement && <p className={styles.flag}><strong>Sources disagree.</strong> {h.disagreement.replace(/^Sources disagree: /, "")}</p>}
      {h.conflicting_reports && <p className={styles.flag}><strong>Conflicting report rows.</strong> Two rows this week disagree and carry no time; both are listed below.</p>}
      <p className={styles.links}>
        <Link href={profileHref(p)}>Player page</Link>
        {" · "}<Link href={withTeam(injuryImpactHref(p), team)}>Injury impact</Link>
        {" · "}<Link href={withTeam("/start-sit", team)}>Lineup advice (Start/Sit)</Link>
      </p>
      <details className={styles.timeline}>
        <summary>Timeline <span className={styles.count}>{reports} {plural(reports, "report")}</span></summary>
        {p.events.length === 0
          ? <p className={styles.sub}>No report from any source in this window.</p>
          : <ol className={styles.events}>{newestFirst(p.events).map((e) => <EventRow key={e.id} e={e} events={p.events} prov={prov} />)}</ol>}
      </details>
    </article>
  );
}

export default async function AvailabilityPage({ searchParams }: PageProps<"/injury-impact/timeline">) {
  const sp = (await searchParams) as SearchParams;
  const sel = readTeamSelection(sp);
  if (sel.kind === "invalid") return <Shell sp={sp} current={null}><UnknownTeam path={PATH} sel={sel} /></Shell>;
  const [res, profile] = await Promise.all([
    api<AvailabilityTimelineResponse>(withTeamId("/api/injury-impact/availability-timeline", sel)), getLeagueProfile(),
  ]);
  const prov = providerName(profile.provider);
  if (isUnknownTeam(res, sel)) {
    return <Shell sp={sp} current={null}><UnknownTeam path={PATH} sel={sel} detail={res.ok ? null : res.error} /></Shell>;
  }
  if (!res.ok) {
    const notSynced = res.status === 503 && res.kind === "http" && !/no database yet/i.test(res.error);
    return (
      <Shell sp={sp} current={selectedTeamId(sel)}>
        {notSynced ? (
          <Section>
            <EmptyState title="Your league isn't synced yet" action={<Link href="/data">Check data status</Link>}>
              <p>The availability timeline needs your league&apos;s current week and roster. Run the {prov} and NFL syncs, then reload this page.</p>
              <p className="faint">{res.error}</p>
            </EmptyState>
          </Section>
        ) : <BackendProblem error={res.error} kind={res.kind} status={res.status} />}
      </Shell>
    );
  }

  const data = res.data;
  const c = data.counts;
  const team = teamParamFrom(sp);
  const signals = data.players.filter((p) => p.high_signal);
  const nflSource = data.sources.find((s) => s.key === "nfl_report");

  return (
    <Shell sp={sp} current={data.league_context.my_team_id} week={data.week}>
      <div className={styles.sourceLine}>
        <p>
          <strong>Week {data.week}{data.league_context.team_name ? ` · ${data.league_context.team_name}` : ""}.</strong>{" "}
          Reports from week {data.window.from_week} to {data.window.to_week}. Official NFL reports and {prov}&apos;s status are kept apart from
          third-party interpretation; times are US Eastern.
          {nflSource?.retrieved_at && <> NFL report last retrieved <time dateTime={nflSource.retrieved_at}>{formatEt(nflSource.retrieved_at)}</time>.</>}
        </p>
        <p className="faint">
          Lineup advice already accounts for these statuses: <Link href={withTeam("/start-sit", team)}>Start/Sit</Link>.
          Whose role grows when a player sits: <Link href={withTeam("/injury-impact", team)}>Injury impact</Link>.
        </p>
      </div>

      <StatStrip compact>
        <Stat label="Designated" value={c.designated} note={`of ${c.players} ${plural(c.players, "player")}`} />
        <Stat label="Changes this week" value={c.high_signal} />
        <Stat label="Practice reported" value={c.practice_known} note="final practice only" />
        <Stat label="Unknown" value={c.unknown} note="no report: not confirmed healthy" />
      </StatStrip>

      {data.notices.length > 0 && (
        <div className={styles.notices}>
          {data.notices.map((n) => <Notice key={n.code} tone={n.tone}>{n.text}</Notice>)}
        </div>
      )}

      {data.players.length === 0 ? (
        <Section>
          <EmptyState title="No roster to follow" action={<Link href="/data">Check data status</Link>}>
            <p>No players are on this team&apos;s roster for week {data.week} in the synced data.</p>
          </EmptyState>
        </Section>
      ) : (
        <div data-testid="availability-digest" className="stack">
          <Section title={<>Changes this week <span className={styles.count}>{signals.length}</span></>}
            subtitle="New, changed or dropped NFL report listings, returns to the active roster, and provider designations with no report yet.">
            {signals.length === 0
              ? <p className={styles.sub}>No high-signal change on this roster in week {data.week}.</p>
              : (
                <ul className={styles.signals}>
                  {signals.map((p) => (
                    <li key={p.espn_player_id}>
                      <PlayerIdentity id={p.espn_player_id} name={p.full_name} position={p.position} team={p.team}
                        meta={levelBadge(p.headline.level) ? <Badge value={levelBadge(p.headline.level)} /> : undefined} />
                      <span className={styles.sub}>{signalText(p)}</span>
                    </li>
                  ))}
                </ul>
              )}
          </Section>
          <Section title={<>Roster <span className={styles.count}>{c.players} {plural(c.players, "player")}</span></>}
            subtitle={`Most severe first; unknown means no report this week, never healthy.${c.team_defenses_skipped ? " Team defenses have no injury report and are left out." : ""}`}>
            <div className={styles.players}>
              {data.players.map((p) => <PlayerCard key={p.espn_player_id} p={p} team={team} prov={prov} />)}
            </div>
          </Section>
        </div>
      )}

      <Section title="Sources" subtitle="Only feeds the app already syncs. Nothing here is scraped or paid for.">
        <ul className={styles.sources}>
          {data.sources.map((s) => (
            <li key={s.key}>
              <SourceTag type={s.type} /> <strong>{s.label}</strong>
              {s.url && <>{" · "}<a href={s.url} target="_blank" rel="noopener noreferrer nofollow">dataset</a></>}
              {s.retrieved_at && <> · retrieved <time dateTime={s.retrieved_at}>{formatEt(s.retrieved_at)}</time></>}
              <span className={styles.sub}>{s.text}</span>
            </li>
          ))}
        </ul>
      </Section>

      <Method title="How the availability timeline works">
        <div className={styles.method}>
          {Object.entries(data.method).map(([k, v]) => <p key={k}>{v}</p>)}
        </div>
      </Method>
    </Shell>
  );
}
