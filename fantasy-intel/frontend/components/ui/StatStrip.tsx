import type { ReactNode } from "react";
import { cx } from "@/lib/cx";
import styles from "./StatStrip.module.css";

export type StatTone = "positive" | "negative" | "caution";

/**
 * One surface holding a row of `Stat`s with hairline dividers; wraps to extra rows on narrow screens.
 * `compact` is for supporting context under a page's main answer: smaller numbers, and three per row on phones.
 */
export function StatStrip({ children, className, compact = false }: { children: ReactNode; className?: string; compact?: boolean }) {
  return <dl className={cx(styles.strip, compact && styles.compact, className)}>{children}</dl>;
}

/**
 * A headline number. `value` is set in the display face. `tone` tints the value only, so pair it
 * with a sign or a word in `value`/`note` (a +/− or "behind"); colour is never the only signal.
 */
export function Stat({ label, value, note, tone }: {
  label: ReactNode;
  value: ReactNode;
  note?: ReactNode;
  tone?: StatTone;
}) {
  return (
    <div className={styles.stat}>
      <dt className={styles.label}>{label}</dt>
      <dd className={cx(styles.value, tone && styles[tone])}>{value}</dd>
      {note ? <dd className={styles.note}>{note}</dd> : null}
    </div>
  );
}
