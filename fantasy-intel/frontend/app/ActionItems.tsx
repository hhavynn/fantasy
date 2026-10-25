import Link from "@/components/LeagueLink";
import { Notice, ProvenanceBadge, Section, ToneIcon, Verdict } from "@/components/ui";
import { joinNames } from "@/lib/action-items";
import type { SignalProvenance } from "@/lib/signal-provenance";
import { hubVerdict, staleNote, type WeeklyDecision, type WeeklyHub } from "@/lib/weekly-decisions";
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
 * says why (`hubVerdict`): nothing loaded, some sources did not load or called their data incomplete (not an
 * all-clear), only things to know, or nothing flagged.
 */
export function NextStep({ hub }: { hub: WeeklyHub }) {
  const { primary: p, context } = hub;
  const week = context.week ? `Week ${context.week}` : "This week";
  if (p) {
    const stale = staleNote(p);
    return (
      <Verdict tone={p.tone} eyebrow={`Next step · ${week} · ${AREA[p.category]}`} headline={p.title} className={styles.next}
               actions={<Link className={styles.itemLink} href={p.href}>{p.cta} →</Link>}>
        <p>{p.reason}</p>
        {stale ? <p>{stale}</p> : null}
        <Evidence signals={p.evidence.signals} />
      </Verdict>
    );
  }
  const v = hubVerdict(hub)!;
  return (
    <Verdict tone={v.tone} eyebrow={week} headline={v.headline} className={styles.next}>
      <p>{v.body}</p>
      {v.showSignals ? <Evidence signals={hub.signals} /> : null}
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
  const { primary, groups, more, unavailable, partial, context } = hub;
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
      {primary && partial.length > 0 ? (
        <Notice tone="caution" className={styles.unavailable} title={`${joinNames(partial.map((x) => x.source))} data is incomplete`}>
          {partial.map((x) => x.reason).join(" ")}
        </Notice>
      ) : null}
      <ToolShortcuts ctx={context} />
    </Section>
  );
}
