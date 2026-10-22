import type { ReactNode } from "react";
import { cx } from "@/lib/cx";
import styles from "./PageHeader.module.css";

/**
 * The page's `h1` with an optional muted meta line and a right-aligned actions slot
 * (buttons, a selector). Wraps on phones: actions drop below the title. `children` render
 * as a full-width row under the header (tabs, filters).
 */
export function PageHeader({ eyebrow, title, meta, provenance, actions, children, className }: {
  /** Small uppercase context line above the title (section or week). */
  eyebrow?: ReactNode;
  title: ReactNode;
  meta?: ReactNode;
  /** Under the meta line: where the page's headline data comes from and how old it is (`ProvenanceBadge`). */
  provenance?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <header className={cx(styles.header, className)}>
      <div className={styles.text}>
        {eyebrow ? <p className={styles.eyebrow}>{eyebrow}</p> : null}
        <h1 className={styles.title}>{title}</h1>
        {meta ? <p className={styles.meta}>{meta}</p> : null}
        {provenance ? <div className={styles.provenance}>{provenance}</div> : null}
      </div>
      {actions ? <div className={styles.actions}>{actions}</div> : null}
      {children ? <div className={styles.extra}>{children}</div> : null}
    </header>
  );
}
