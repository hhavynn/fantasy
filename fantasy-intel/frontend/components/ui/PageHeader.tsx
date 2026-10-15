import type { ReactNode } from "react";
import { cx } from "@/lib/cx";
import styles from "./PageHeader.module.css";

/**
 * The page's `h1` with an optional muted meta line and a right-aligned actions slot
 * (buttons, a selector). Wraps on phones: actions drop below the title. `children` render
 * as a full-width row under the header (tabs, filters).
 */
export function PageHeader({ title, meta, actions, children, className }: {
  title: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <header className={cx(styles.header, className)}>
      <div className={styles.text}>
        <h1 className={styles.title}>{title}</h1>
        {meta ? <p className={styles.meta}>{meta}</p> : null}
      </div>
      {actions ? <div className={styles.actions}>{actions}</div> : null}
      {children ? <div className={styles.extra}>{children}</div> : null}
    </header>
  );
}
