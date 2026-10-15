import type { ReactNode } from "react";
import styles from "./VisuallyHidden.module.css";

/** Text for assistive tech only (e.g. "Loading…" on a skeleton page). */
export function VisuallyHidden({ children }: { children: ReactNode }) {
  return <span className={styles.hidden}>{children}</span>;
}
