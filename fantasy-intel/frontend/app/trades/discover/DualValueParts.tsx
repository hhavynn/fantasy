import { Badge, VisuallyHidden } from "@/components/ui";
import {
  CRITERION_SHORT, checkStatusInfo, checkTitle, concernLabel, orderedChecks, verdictInfo,
} from "@/lib/trade-dual-value";
import type { DVCheck, DVCheckBrief } from "@/lib/trade-types";
import styles from "./discover.module.css";

// Small presentational pieces shared by the results table (server) and the evidence panel (client): no state, no fetching.

/** The verdict as a badge; the backend's descriptive sentence is its title. Never a grade or a probability. */
export function VerdictBadge({ verdict, text }: { verdict: string | null | undefined; text?: string | null }) {
  const v = verdictInfo(verdict);
  return <Badge value={v.label} tone={v.tone} title={text ?? undefined} />;
}

/**
 * The six plausibility checks as compact chips. Each chip carries a glyph and a word (read by assistive tech), so the
 * status never depends on colour; the title is the criterion's meaning (the check's own reason in the evidence panel).
 */
export function CheckChips({ checks, criteria }: { checks: readonly DVCheckBrief[] | null | undefined; criteria?: Record<string, string> | null }) {
  const list = orderedChecks(checks);
  if (list.length === 0) return null;
  return (
    <ul className={styles.chips} aria-label="Plausibility checks">
      {list.map((c) => {
        const s = checkStatusInfo(c.status);
        return (
          <li key={c.criterion} className={`${styles.chip} ${s.tone ? styles[`chip_${s.tone}`] : ""}`} title={checkTitle(c, criteria)}>
            <span aria-hidden="true" className={styles.chipGlyph}>{s.glyph}</span>
            <span>{CRITERION_SHORT[c.criterion] ?? c.criterion.replaceAll("_", " ")}</span>
            <VisuallyHidden>: {s.label}</VisuallyHidden>
          </li>
        );
      })}
    </ul>
  );
}

/** "Downside:" or "Fairness concern:" followed by the backend's `main_concern`; nothing when there is none. */
export function MainConcern({ text, checks }: { text: string | null | undefined; checks: readonly (DVCheck | DVCheckBrief)[] | null | undefined }) {
  const label = concernLabel(checks, text);
  if (!label || !text) return null;
  return <p className={styles.concern}><b>{label}:</b> {text}</p>;
}
