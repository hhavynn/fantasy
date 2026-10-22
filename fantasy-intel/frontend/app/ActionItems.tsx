import { TeamLink as Link } from "@/components/TeamLink";
import { EmptyState, Notice, ProvenanceBadge, Section, ToneIcon } from "@/components/ui";
import type { ActionList } from "@/lib/action-items";
import type { SignalProvenance } from "@/lib/signal-provenance";
import { ToolShortcuts } from "./ToolShortcuts";
import styles from "./home.module.css";

/**
 * Prioritised things to do this week. The list only selects what the backend already recommends; each item
 * links to the page with the evidence. A failed source is named, and "lineup is set" is only claimed when
 * every source loaded.
 */
export function ActionItems({ list, week, provenance = [] }: { list: ActionList; week: number | null; provenance?: readonly SignalProvenance[] }) {
  const { items, more, unavailable } = list;
  const allDown = unavailable.length === 4;
  return (
    <Section title="Before kickoff" subtitle={week ? `Week ${week} · what to change, in priority order` : "What to change, in priority order"} flush className={styles.actionCard}
             actions={items.length > 0 ? <span className={styles.count}>{items.length} {items.length === 1 ? "item" : "items"}</span> : undefined}>
      {items.length > 0 ? (
        <ul className={styles.items}>
          {items.map((it) => (
            <li key={it.id} className={styles.item}>
              <ToneIcon tone={it.tone} className={`${styles.icon} ${styles[it.tone]}`} />
              <div>
                <p className={styles.itemTitle}>{it.title}</p>
                <p className={styles.itemReason}>{it.reason}</p>
              </div>
              <Link className={styles.itemLink} href={it.href}>{it.cta} →</Link>
            </li>
          ))}
        </ul>
      ) : allDown ? null : unavailable.length === 0 ? (
        <EmptyState title="Lineup is set">
          No swaps, unavailable starters or waiver pickups are flagged{week ? ` for week ${week}` : ""}.
        </EmptyState>
      ) : (
        <EmptyState title="Nothing flagged in what loaded">The checks below could not run, so this is not an all-clear.</EmptyState>
      )}
      {more.swaps > 0 || more.waivers > 0 ? (
        <p className={styles.more}>
          {more.swaps > 0 ? <><Link href="/start-sit">{more.swaps} more Start/Sit {more.swaps === 1 ? "move" : "moves"}</Link></> : null}
          {more.swaps > 0 && more.waivers > 0 ? " · " : null}
          {more.waivers > 0 ? <><Link href="/waivers">{more.waivers} more waiver {more.waivers === 1 ? "pickup" : "pickups"}</Link></> : null}
        </p>
      ) : null}
      {unavailable.length > 0 ? (
        <Notice tone="caution" className={styles.unavailable} title={allDown ? "Action items unavailable" : `Couldn't check ${unavailable.join(", ")}`}>
          {allDown ? "None of the sources behind this list loaded." : "Items from those sources are missing from this list."}
        </Notice>
      ) : null}
      {provenance.length > 0 ? <div className={styles.provenance}><ProvenanceBadge signals={provenance} /></div> : null}
      <ToolShortcuts />
    </Section>
  );
}
