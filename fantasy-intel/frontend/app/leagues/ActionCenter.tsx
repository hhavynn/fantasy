import Link from "next/link";
import { Badge, Notice, ProvenanceBadge, Section, Skeleton, ToneIcon } from "@/components/ui";
import {
  buildActionCenter, kickoffText, parseActionCenter, shortVersion, untilText, type CenterCard,
} from "@/lib/action-center";
import { joinNames } from "@/lib/action-items";
import { apiRequest } from "@/lib/api";
import { providerName } from "@/lib/labels";
import { leaguePath } from "@/lib/league-scope";
import { dataAsOf } from "@/lib/league-switcher";
import { leagueName, leagueState } from "@/lib/my-leagues";
import { plural } from "@/lib/plural";
import type { WeeklyDecision } from "@/lib/weekly-decisions";
import styles from "./actionCenter.module.css";

const TITLE = "Across your leagues";
const SUBTITLE = "Lineup and injury decisions for this week, the soonest kickoff first. Waivers and trades stay on each league's pages.";
const STATUS_TEXT: Record<WeeklyDecision["status"], string> = { actionable: "", informational: "Worth knowing", locked: "Locked", unavailable: "Can't verify" };
const badgeTone = (tone: string) => (tone === "neutral" ? "" : tone);

/**
 * The all-leagues action center (issue #166): every league the signed-in manager follows, one card each, with the
 * decisions that league's Overview would lead with. GET /v2/action-center authorizes and evaluates on the server; the
 * cards say when a league's data is missing, late or not fresh instead of showing it as a clean roster.
 */
export async function ActionCenter() {
  const res = await apiRequest<unknown>("/v2/action-center");
  if (!res.ok) {
    return (
      <Section title={TITLE} subtitle={SUBTITLE} id="action-center">
        <Notice tone="negative" title="This week's decisions could not be loaded">
          {res.error} Your leagues are listed below; each league&apos;s Overview still has its own decisions.
        </Notice>
      </Section>
    );
  }
  const parsed = parseActionCenter(res.data);
  if (!parsed) {
    return (
      <Section title={TITLE} subtitle={SUBTITLE} id="action-center">
        <Notice tone="caution" title="This week's decisions could not be read">
          The backend answered in a format this page does not know. Each league&apos;s Overview still has its own decisions.
        </Notice>
      </Section>
    );
  }
  const view = buildActionCenter(parsed);
  // Lock states were computed when the backend evaluated the leagues, so "in 3h" counts from that moment too.
  const now = parsed.generatedAt;
  return (
    <Section title={TITLE} subtitle={SUBTITLE} id="action-center">
      <ol className={styles.cards}>
        {view.cards.map((c) => <li key={c.id}><LeagueCard card={c} now={now} /></li>)}
      </ol>
      {view.moreLabel ? (
        <div className={styles.more}>
          <p className={styles.moreText}>
            <strong>{view.moreLabel}.</strong> Only the first {parsed.limit} are checked at once; open one to see its decisions.
          </p>
          <ul className={styles.moreList}>
            {view.more.map((l) => (
              <li key={l.id}>
                <Link href={l.syncState === "ready" ? leaguePath(l.id, "/") : `/leagues?league=${l.id}`} prefetch={false}>{leagueName(l)}</Link>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Section>
  );
}

function LeagueCard({ card: c, now }: { card: CenterCard; now: number | null }) {
  const headingId = `center-${c.id}`;
  const sync = leagueState(c.league);
  const meta = [
    c.week ? `Week ${c.week}` : null,
    c.teamName,
    providerName(c.league.provider),
  ].filter(Boolean).join(" · ");
  const asOf = dataAsOf(c.committedAt, "UTC");
  return (
    <article className={styles.card} aria-labelledby={headingId} data-state={c.state}>
      <div className={styles.head}>
        <h3 id={headingId} className={styles.title}>
          <Link href={c.href} prefetch={false} title={c.name}>{c.name}</Link>
        </h3>
        <div className={styles.badges}>
          <Badge value={c.status.label} tone={badgeTone(c.status.tone)} title={c.status.detail} />
          {/* A league without a saved team still has its own sync state, said separately. */}
          {c.state === "needs_team" && sync.label !== c.status.label ? <Badge value={sync.label} tone={badgeTone(sync.tone)} title={sync.detail} /> : null}
        </div>
      </div>
      <p className={styles.meta}>{meta}</p>

      {c.state === "ready" ? <LockLine card={c} now={now} /> : <p className={styles.reason}>{c.reason ?? c.status.detail}</p>}
      {c.state === "ready" && c.status.label === "Not fresh" ? <p className={styles.reason}>{c.status.detail}</p> : null}

      {c.decisions.length > 0 ? (
        <ul className={styles.decisions}>
          {c.decisions.map(({ decision: d, deadline }) => <DecisionRow key={d.id} d={d} deadline={deadline} now={now} />)}
        </ul>
      ) : c.state === "ready" && c.unavailable.length === 0 ? (
        <p className={styles.none}>No lineup or injury decision is flagged{c.week ? ` for week ${c.week}` : ""}.</p>
      ) : null}
      {c.more ? <p className={styles.moreLink}><Link href={c.more.href}>{c.more.count} more {plural(c.more.count, "decision")} on Start/Sit</Link></p> : null}
      {c.unavailable.length > 0 ? (
        <Notice tone="caution" title={`Couldn't check ${joinNames(c.unavailable)}`}>
          Decisions from {c.unavailable.length === 1 ? "that source" : "those sources"} are missing here, so this is not an all-clear.
        </Notice>
      ) : null}

      <div className={styles.foot}>
        {c.state === "ready" ? (
          c.signals.length > 0 ? <ProvenanceBadge signals={c.signals} /> : <p className={styles.faint}>Evidence age unknown</p>
        ) : null}
        <p className={styles.faint}>
          {asOf ? `${asOf} UTC` : "Not synced yet"}
          {c.versions ? <> · <span title={`Data version ${c.versions.data_version}; NFL data version ${c.versions.global_version}`}>version {shortVersion(c.versions.data_version)}</span></> : null}
          {c.projections ? ` · ${c.projections} projections` : null}
        </p>
        <Link className={styles.cta} href={c.href} prefetch={false}>{c.cta} →</Link>
      </div>
    </article>
  );
}

function LockLine({ card: c, now }: { card: CenterCard; now: number | null }) {
  return (
    <div className={styles.lock}>
      <p className={styles.lockMain}>
        {c.nextKickoff !== null
          ? <>Next kickoff among your players: <strong>{kickoffText(c.nextKickoff)}</strong>{now !== null ? ` (${untilText(c.nextKickoff, now)})` : ""}</>
          : "No kickoff ahead among your players this week, or no kickoff time is synced."}
      </p>
      <p className={styles.faint}>
        Lineup lock: {c.lockRule}
        {c.lockUnknown > 0 ? ` ${c.lockUnknown} ${plural(c.lockUnknown, "starter's", "starters'")} lock state is unknown.` : ""}
      </p>
    </div>
  );
}

function DecisionRow({ d, deadline, now }: { d: WeeklyDecision; deadline: number | null; now: number | null }) {
  const status = STATUS_TEXT[d.status];
  return (
    <li className={styles.decision} data-status={d.status}>
      <ToneIcon tone={d.tone} className={`${styles.icon} ${styles[d.tone]}`} />
      <div className={styles.body}>
        <p className={styles.decisionTitle}>{d.title}</p>
        <p className={styles.decisionReason}>{d.reason}</p>
        {status ? <p className={styles.decisionStatus}><strong>{status}</strong>{d.statusReason ? `: ${d.statusReason}` : ""}</p> : null}
        {d.status === "actionable" && deadline !== null
          ? <p className={styles.deadline}>Before {kickoffText(deadline)}{now !== null ? ` (${untilText(deadline, now)})` : ""}</p> : null}
      </div>
      <Link className={styles.decisionLink} href={d.href}>{d.cta} →</Link>
    </li>
  );
}

/** Same footprint as the cards while the leagues are checked. */
export function ActionCenterSkeleton() {
  return (
    <Section title={TITLE} subtitle={SUBTITLE} id="action-center">
      <div className={styles.cards} role="status" aria-busy="true" aria-label="Checking your leagues">
        {[0, 1].map((i) => (
          <div key={i} className={`${styles.card} ${styles.skeletonCard}`}>
            <Skeleton w="50%" h={16} />
            <Skeleton w="35%" h={12} />
            <Skeleton w="80%" h={12} />
            <Skeleton w="100%" h={40} radius={4} />
          </div>
        ))}
      </div>
    </Section>
  );
}
