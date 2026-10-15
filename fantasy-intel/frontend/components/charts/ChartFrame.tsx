import type { CSSProperties, ReactNode } from "react";
import { cx } from "@/lib/cx";
import { phoneFont } from "./shared";
import styles from "./charts.module.css";

/**
 * Shared chart shell: a figure holding an optional legend and a responsive SVG. The SVG scales
 * with its container (`viewBox`, `width: 100%`) and is capped at 1.3x its natural `width` so text
 * never balloons on wide screens. Callers draw in the `width` x `height` coordinate space.
 */
export function ChartFrame({ width, height, label, legend, className, children }: {
  width: number;
  height: number;
  /** Screen-reader summary of the whole chart. */
  label: string;
  legend?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  const vars = { maxWidth: Math.round(width * 1.3), "--t-phone": `${phoneFont(width)}px` } as CSSProperties;
  return (
    <figure className={cx(styles.figure, className)} style={vars}>
      {legend}
      <svg className={styles.svg} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label}>
        <title>{label}</title>
        {children}
      </svg>
    </figure>
  );
}

/** Colour swatch + label list for charts with more than one series. */
export function Legend({ items }: { items: { id: string; label: string; color: string }[] }) {
  return (
    <ul className={styles.legend}>
      {items.map((item) => (
        <li key={item.id} className={styles.legendItem}>
          <span className={styles.swatch} style={{ background: item.color }} aria-hidden="true" />
          {item.label}
        </li>
      ))}
    </ul>
  );
}
