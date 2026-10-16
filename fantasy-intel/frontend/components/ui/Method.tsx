import type { ReactNode } from "react";
import { cx } from "@/lib/cx";
import styles from "./Method.module.css";

/** Collapsible "how it's calculated" block. Closed by default; the explanation stays one click away. */
export function Method({ title = "How it's calculated", className, children }: {
  title?: string;
  className?: string;
  children?: ReactNode;
}) {
  return (
    <details className={cx(styles.method, className)}>
      <summary className={styles.summary}>{title}</summary>
      <div className={styles.body}>{children}</div>
    </details>
  );
}
