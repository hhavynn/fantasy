import { useId, type ReactNode } from "react";
import { cx } from "@/lib/cx";
import styles from "./Section.module.css";

/**
 * A content surface: header bar (`title` + `actions`), body, optional `footnote`.
 * `flush` removes the body padding so a table runs edge to edge. Without a `title` the
 * section has no header bar. The title is the section's `h2`.
 */
export function Section({ title, actions, footnote, flush = false, id, className, children }: {
  title?: ReactNode;
  actions?: ReactNode;
  footnote?: ReactNode;
  flush?: boolean;
  id?: string;
  className?: string;
  children?: ReactNode;
}) {
  const headingId = useId();
  const hasHead = title !== undefined && title !== null && title !== false;
  return (
    <section id={id} className={cx(styles.section, flush && styles.flush, className)} aria-labelledby={hasHead ? headingId : undefined}>
      {(hasHead || actions) && (
        <div className={styles.head}>
          {hasHead ? <h2 id={headingId} className={styles.title}>{title}</h2> : <span />}
          {actions ? <div className={styles.actions}>{actions}</div> : null}
        </div>
      )}
      <div className={styles.body}>{children}</div>
      {footnote ? <p className={styles.footnote}>{footnote}</p> : null}
    </section>
  );
}
