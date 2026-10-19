import { GLOSSARY, type GlossaryTerm } from "@/lib/glossary";
import { cx } from "@/lib/cx";
import styles from "./Glossary.module.css";

/**
 * "Terms on this page": a collapsible list of plain-language definitions for the abbreviations and metrics a page
 * shows. Touch screens cannot hover, so a definition never lives only in a tooltip. Closed by default.
 */
export function Glossary({ terms, title = "Terms on this page", className }: { terms: readonly GlossaryTerm[]; title?: string; className?: string }) {
  return (
    <details className={cx(styles.glossary, className)}>
      <summary className={styles.summary}>{title}</summary>
      <dl className={styles.list}>
        {terms.map((t) => (
          <div key={t} className={styles.item}>
            <dt>{t}</dt>
            <dd>{GLOSSARY[t]}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
