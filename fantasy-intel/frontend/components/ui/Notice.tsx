import type { ReactNode } from "react";
import { cx } from "@/lib/cx";
import styles from "./Notice.module.css";

export type NoticeTone = "info" | "caution" | "negative" | "positive";

/** One glyph per tone, so meaning never depends on colour alone. */
function Icon({ tone }: { tone: NoticeTone }) {
  const common = { width: 20, height: 20, viewBox: "0 0 20 20", fill: "none", stroke: "currentColor", strokeWidth: 1.6, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true, className: styles.icon } as const;
  switch (tone) {
    case "caution":
      return <svg {...common}><path d="M10 2.8 18 16.5H2L10 2.8Z" /><path d="M10 8v4" /><path d="M10 14.2v.1" /></svg>;
    case "negative":
      return <svg {...common}><circle cx="10" cy="10" r="7.5" /><path d="m7.2 7.2 5.6 5.6M12.8 7.2l-5.6 5.6" /></svg>;
    case "positive":
      return <svg {...common}><circle cx="10" cy="10" r="7.5" /><path d="m6.6 10.2 2.5 2.5 4.4-5" /></svg>;
    default:
      return <svg {...common}><circle cx="10" cy="10" r="7.5" /><path d="M10 9.2v4.6" /><path d="M10 6.2v.1" /></svg>;
  }
}

/** An inline message: info, caution, negative (errors) or positive. `title` is the bold lead line. */
export function Notice({ tone = "info", title, className, children }: {
  tone?: NoticeTone;
  title?: ReactNode;
  className?: string;
  children?: ReactNode;
}) {
  return (
    <div className={cx(styles.notice, styles[tone], className)} role={tone === "negative" ? "alert" : undefined}>
      <Icon tone={tone} />
      <div className={styles.content}>
        {title ? <p className={styles.title}>{title}</p> : null}
        {children ? <div className={styles.body}>{children}</div> : null}
      </div>
    </div>
  );
}

// Legacy wrappers: same props and spacing (16px below) as the old global `.callout`.

/** @deprecated Use `<Notice tone="negative" title="…">`. */
export function ErrorBox({ error }: { error: string }) {
  return <Notice tone="negative" title="Couldn't load data." className={styles.legacy}>{error}</Notice>;
}

/** @deprecated Use `<Notice tone="caution">`. */
export function Callout({ children }: { children: ReactNode }) {
  return <Notice tone="caution" className={styles.legacy}>{children}</Notice>;
}
