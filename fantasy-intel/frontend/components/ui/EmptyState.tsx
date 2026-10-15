import type { ReactNode } from "react";
import { cx } from "@/lib/cx";
import styles from "./EmptyState.module.css";

/** Calm placeholder for a section with nothing to show. Say what is absent and, if useful, what to do. */
export function EmptyState({ title, children, action, className }: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cx(styles.empty, className)}>
      <svg className={styles.glyph} width="28" height="28" viewBox="0 0 28 28" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true">
        <circle cx="14" cy="14" r="10.5" strokeDasharray="3 4" />
        <path d="M9.5 14h9" />
      </svg>
      <p className={styles.title}>{title}</p>
      {children ? <div className={styles.body}>{children}</div> : null}
      {action ? <div className={styles.action}>{action}</div> : null}
    </div>
  );
}
