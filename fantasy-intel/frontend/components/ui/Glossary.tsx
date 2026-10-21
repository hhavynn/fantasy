import { glossaryText, type GlossaryTerm } from "@/lib/glossary";
import { cx } from "@/lib/cx";
import styles from "./Glossary.module.css";

/**
 * "Terms on this page": a collapsible list of plain-language definitions for the abbreviations and metrics a page
 * shows. Touch screens cannot hover, so a definition never lives only in a tooltip. Closed by default.
 */
export function Glossary({ terms, title = "Terms on this page", className, providerProjections = true }: {
  terms: readonly GlossaryTerm[]; title?: string; className?: string;
  /** False for a league whose provider publishes no projections (Sleeper): projection terms then say every number is Ours. */
  providerProjections?: boolean;
}) {
  return (
    <details className={cx(styles.glossary, className)}>
      <summary className={styles.summary}>{title}</summary>
      <dl className={styles.list}>
        {terms.map((t) => (
          <div key={t} className={styles.item}>
            <dt>{t}</dt>
            <dd>{glossaryText(t, providerProjections)}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
