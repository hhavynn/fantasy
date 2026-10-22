import type { ReactNode } from "react";
import { cx } from "@/lib/cx";
import { PageContext } from "./PageContext";
import styles from "./PageHeader.module.css";

/**
 * The page header and action toolbar every decision page repeats (Lineup, Waivers, Trades, Players, League): context
 * eyebrow, `h1`, muted meta line, provenance, and a right-aligned actions slot (the team picker, week toggles, a link
 * to the sibling tool). Wraps on phones: actions drop below the title. `children` render as a full-width row under
 * the header (tabs, filters). Without an `eyebrow` the page's group from the nav registry stands in, so no page
 * header opens without saying where it is.
 */
export function PageHeader({ eyebrow, title, meta, provenance, actions, children, className }: {
  /** Small uppercase context line above the title (section or week); defaults to the page's nav group. */
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
        {eyebrow ? <p className={styles.eyebrow}>{eyebrow}</p> : <PageContext className={styles.eyebrow} />}
        <h1 className={styles.title}>{title}</h1>
        {meta ? <p className={styles.meta}>{meta}</p> : null}
        {provenance ? <div className={styles.provenance}>{provenance}</div> : null}
      </div>
      {actions ? <div className={styles.actions}>{actions}</div> : null}
      {children ? <div className={styles.extra}>{children}</div> : null}
    </header>
  );
}
