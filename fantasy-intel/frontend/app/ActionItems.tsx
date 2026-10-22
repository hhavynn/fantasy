import Link from "next/link";
import { Notice, ProvenanceBadge, Section, ToneIcon, Verdict } from "@/components/ui";
import { joinNames } from "@/lib/action-items";
import type { SignalProvenance } from "@/lib/signal-provenance";
import type { WeeklyDecision, WeeklyHub } from "@/lib/weekly-decisions";
import { ToolShortcuts } from "./ToolShortcuts";
import styles from "./home.module.css";

const AREA: Record<WeeklyDecision["category"], string> = { lineup: "Lineup", injury: "Lineup", waivers: "Waivers", trade: "Trades", league: "League" };
const STATUS_TEXT: Record<WeeklyDecision["status"], string> = { actionable: "", informational: "Worth knowing", locked: "Locked", unavailable: "Can't verify" };

/** The evidence behind one decision: the provenance badge of its source, or an explicit unknown when there is none. */
function Evidence({ signals }: { signals: readonly SignalProvenance[] }) {
  if (signals.length === 0) {
    return <p className={styles.evidenceUnknown} title="The source sent no provenance block (an older backend)">Evidence age unknown</p>;
  }
  return <ProvenanceBadge signals={signals} className={styles.evidence} />;
}

/**
 * The one next step, as the page's Verdict: the highest-priority decision that can be acted on now. Without one it
 * says why: nothing loaded, some sources did not load (not an all-clear), only things to know, or nothing flagged.
 */
export function NextStep({ hub }: { hub: WeeklyHub }) {
  const { primary: p, unavailable, groups, context } = hub;
  const week = context.week ? `Week ${context.week}` : "This week";
  if (p) {
    return (
      <Verdict tone={p.tone} eyebrow={`Next step · ${week} · ${AREA[p.category]}`} headline={p.title} className={styles.next}
               actions={<Link className={styles.itemLink} href={p.href}>{p.cta} →</Link>}>
        <p>{p.reason}</p>
        <Evidence signals={p.evidence.signals} />
      </Verdict>
    );
  }
  if (unavailable.length === 4) {
    return (
      <Verdict tone="negative" eyebrow={week} headline="Before-kickoff checks unavailable" className={styles.next}>
        <p>None of the sources behind the decisions loaded ({joinNames(unavailable)}), so nothing is recommended.</p>
      </Verdict>
    );
  }
  if (unavailable.length > 0) {
    return (
      <Verdict tone="caution" eyebrow={week} headline="Nothing to act on in what loaded" className={styles.next}>
        <p>Couldn&apos;t check {joinNames(unavailable)}, so this is not an all-clear.</p>
        <Evidence signals={hub.signals} />
      </Verdict>
    );
  }
  if (groups.length > 0) {
    return (
      <Verdict tone="info" eyebrow={week} headline="No moves flagged before kickoff" className={styles.next}>
        <p>Nothing below asks for a change; it is worth knowing before kickoff.</p>
        <Evidence signals={hub.signals} />
      </Verdict>
    );
  }
  return (
    <Verdict tone="positive" eyebrow={week} headline="Lineup is set" className={styles.next}>
      <p>No swaps, unavailable starters, waiver pickups or trade signals are flagged{context.week ? ` for week ${context.week}` : ""}.</p>
      <Evidence signals={hub.signals} />
    </Verdict>
  );
}

function DecisionRow({ d }: { d: WeeklyDecision }) {
  const status = STATUS_TEXT[d.status];
  return (
    <li className={styles.item} data-status={d.status}>
      <ToneIcon tone={d.tone} className={`${styles.icon} ${styles[d.tone]}`} />
      <div className={styles.itemBody}>
        <p className={styles.itemTitle}>{d.title}</p>
        <p className={styles.itemReason}>{d.reason}</p>
        {status ? <p className={styles.itemStatus}><strong>{status}</strong>{d.statusReason ? `: ${d.statusReason}` : ""}</p> : null}
        <Evidence signals={d.evidence.signals} />
      </div>
      <Link className={styles.itemLink} href={d.href}>{d.cta} →</Link>
    </li>
  );
}

/**
 * The other decisions of the week, grouped by area (Lineup, Waivers, Trades, League) in priority order, at most four
 * beside the next step. Each restates what its source page recommends and links there in the same league, team and
 * week; a failed source is named, and overflow links to the page that holds it.
 */
export function ActionItems({ hub }: { hub: WeeklyHub }) {
  const { primary, groups, more, unavailable, context } = hub;
  const count = groups.reduce((n, g) => n + g.decisions.length, 0);
  return (
    <Section title="Before kickoff" flush className={styles.actionCard}
             subtitle={`${context.week ? `Week ${context.week} · ` : ""}${primary ? "after the next step, " : ""}in priority order`}
             actions={count > 0 ? <span className={styles.count}>{count} {count === 1 ? "decision" : "decisions"}</span> : undefined}>
      {count > 0 ? (
        groups.map((g) => (
          <div key={g.key} className={styles.group}>
            <h3 className={styles.groupLabel}>{g.label}</h3>
            <ul className={styles.items}>
              {g.decisions.map((d) => <DecisionRow key={d.id} d={d} />)}
            </ul>
          </div>
        ))
      ) : primary ? (
        <p className={styles.none}>{unavailable.length > 0 ? "Nothing else is flagged in what loaded." : "Nothing else is flagged."}</p>
      ) : null}
      {more.length > 0 ? (
        <p className={styles.more}>
          {more.map((x, i) => (
            <span key={x.key}>{i > 0 ? " · " : null}<Link href={x.href}>{x.label}</Link></span>
          ))}
        </p>
      ) : null}
      {primary && unavailable.length > 0 ? (
        <Notice tone="caution" className={styles.unavailable} title={`Couldn't check ${joinNames(unavailable)}`}>
          Decisions from those sources are missing from this list.
        </Notice>
      ) : null}
      <ToolShortcuts ctx={context} />
    </Section>
  );
}
