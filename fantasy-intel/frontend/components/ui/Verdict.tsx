import type { ReactNode } from "react";
import { cx } from "@/lib/cx";
import { Icon, type NoticeTone } from "./Notice";
import styles from "./Verdict.module.css";

/**
 * The answer a decision page opens with: one headline sentence, a line of context, and the two or three numbers
 * that back it (`VerdictFigure`s). Decision first; the evidence and method follow in the page. The tone is carried
 * by an icon and the headline wording, never by colour alone. The headline is the section's `h2`.
 */
export function Verdict({ tone = "info", eyebrow, headline, children, figures, actions, className }: {
  tone?: NoticeTone;
  /** Small context above the headline (the week, the team). */
  eyebrow?: ReactNode;
  headline: ReactNode;
  /** One or two short sentences under the headline. */
  children?: ReactNode;
  /** `VerdictFigure`s: the numbers behind the headline. */
  figures?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <section className={cx(styles.verdict, styles[tone], className)}>
      <div className={styles.head}>
        <Icon tone={tone} className={styles.icon} />
        <div className={styles.text}>
          {eyebrow ? <p className={styles.eyebrow}>{eyebrow}</p> : null}
          <h2 className={styles.headline}>{headline}</h2>
          {children ? <div className={styles.body}>{children}</div> : null}
        </div>
        {actions ? <div className={styles.actions}>{actions}</div> : null}
      </div>
      {figures ? <dl className={styles.figures}>{figures}</dl> : null}
    </section>
  );
}

/** One number in a `Verdict`. `emphasis` sets the key figure larger; `tone` tints the value, so pair it with a sign. */
export function VerdictFigure({ label, value, note, emphasis = false, tone }: {
  label: ReactNode;
  value: ReactNode;
  note?: ReactNode;
  emphasis?: boolean;
  tone?: "positive" | "negative" | "caution";
}) {
  return (
    <div className={cx(styles.figure, emphasis && styles.emphasis)}>
      <dt className={styles.label}>{label}</dt>
      <dd className={cx(styles.value, tone && styles[`v-${tone}`])}>{value}</dd>
      {note ? <dd className={styles.note}>{note}</dd> : null}
    </div>
  );
}
